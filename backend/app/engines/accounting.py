"""Double-entry-lite accounting engine.

Lots are always rebuilt from active transactions; there is no mutable position
table. Cash follows the single reference equation from the spec:

    cash = opening_cash + flows + sum(trade cash deltas)
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.errors import NotFoundError, OverSellError
from app.core.money import E8, D
from app.engines.versioning import effective_version
from app.models.entities import Account, CashFlow, CorporateAction, Security, Transaction


@dataclass
class Lot:
    transaction_id: str
    quantity: Decimal
    unit_cost: Decimal
    acquired: date


@dataclass
class PositionState:
    security_id: str
    symbol: str
    quantity: Decimal
    avg_cost: Decimal
    lots: list[Lot] = field(default_factory=list)


@dataclass
class BookState:
    account_id: str
    account_code: str
    currency: str
    cash: Decimal
    positions: dict[str, PositionState]
    realized: Decimal
    last_tx_id: str | None
    last_tx_rev: int
    last_flow_id: str | None
    last_flow_rev: int


def _symbol_map(db: Session, ids: set[str]) -> dict[str, str]:
    if not ids:
        return {}
    rows = db.execute(select(Security).where(Security.id.in_(ids))).scalars().all()
    return {s.id: s.symbol for s in rows}


def _apply_splits(lots: dict[str, list[Lot]], splits: list[CorporateAction], upto: date) -> None:
    for split in splits:
        if split.action_date > upto:
            continue
        for lot in lots.get(split.security_id, []):
            if lot.acquired < split.action_date:
                lot.quantity *= split.ratio
                lot.unit_cost /= split.ratio


def _consume(lots: list[Lot], quantity: Decimal, method: str) -> list[tuple[Lot, Decimal]]:
    if method == "hifo":
        ordered = sorted(lots, key=lambda lot: (-lot.unit_cost, lot.acquired, lot.transaction_id))
    else:
        ordered = sorted(lots, key=lambda lot: (lot.acquired, lot.transaction_id))
    remaining = quantity
    taken: list[tuple[Lot, Decimal]] = []
    for lot in ordered:
        if remaining <= E8:
            break
        take = min(lot.quantity, remaining)
        if take <= 0:
            continue
        taken.append((lot, take))
        lot.quantity -= take
        remaining -= take
    if remaining > E8:
        available = sum((lot.quantity for lot in lots), Decimal("0")) + sum(
            (t[1] for t in taken), Decimal("0")
        )
        raise OverSellError(
            f"Cannot sell {quantity} shares: only {available} available.",
            requested=str(quantity),
            available=str(available),
        )
    return taken


def build_book(db: Session, account_id: str, as_of: date | None = None) -> BookState:
    account = db.get(Account, account_id)
    if account is None:
        raise NotFoundError(f"Account '{account_id}' not found.")
    today = as_of or date.today()

    tx_query = (
        select(Transaction)
        .where(Transaction.account_id == account_id, Transaction.status == "active")
        .where(Transaction.trade_date <= today)
        .order_by(Transaction.trade_date, Transaction.sequence, Transaction.created_at, Transaction.id)
    )
    transactions = db.execute(tx_query).scalars().all()

    flow_query = (
        select(CashFlow)
        .where(CashFlow.account_id == account_id, CashFlow.flow_date <= today)
        .order_by(CashFlow.flow_date, CashFlow.sequence, CashFlow.created_at, CashFlow.id)
    )
    flows = db.execute(flow_query).scalars().all()

    securities = _symbol_map(
        db, {t.security_id for t in transactions} | {f.security_id for f in flows if f.security_id}
    )

    splits = (
        db.execute(
            select(CorporateAction)
            .where(CorporateAction.action_date <= today)
            .where(CorporateAction.type == "split")
            .order_by(CorporateAction.action_date, CorporateAction.created_at)
        )
        .scalars()
        .all()
    )

    lots: dict[str, list[Lot]] = {}
    cash = D(account.opening_cash)
    realized = Decimal("0")

    for flow in flows:
        cash += D(flow.amount)

    split_index = 0
    for tx in transactions:
        while split_index < len(splits) and splits[split_index].action_date <= tx.trade_date:
            split = splits[split_index]
            for lot in lots.get(split.security_id, []):
                if lot.acquired < split.action_date:
                    lot.quantity *= split.ratio
                    lot.unit_cost /= split.ratio
            split_index += 1

        symbol = securities.get(tx.security_id, tx.security_id)
        quantity = D(tx.quantity)
        price = D(tx.unit_price)
        fee = D(tx.fee)
        fx = D(tx.fx_rate)
        if tx.side == "BUY":
            unit_cost = (price + (fee / quantity)) * fx if quantity else price * fx
            lots.setdefault(tx.security_id, []).append(
                Lot(
                    transaction_id=tx.id,
                    quantity=quantity,
                    unit_cost=unit_cost,
                    acquired=tx.trade_date,
                )
            )
            cash -= (quantity * price + fee) * fx
        elif tx.side == "SELL":
            position_lots = lots.get(tx.security_id, [])
            available = sum((lot.quantity for lot in position_lots), Decimal("0"))
            if quantity > available + E8:
                raise OverSellError(
                    f"SELL {symbol} {quantity} exceeds available {available} at transaction {tx.id}.",
                    transaction_id=tx.id,
                    security_id=tx.security_id,
                    symbol=symbol,
                    requested=str(quantity),
                    available=str(available),
                )
            version = effective_version(db, account.mandate_id, tx.trade_date)
            taken = _consume(position_lots, quantity, version.lot_method)
            sell_unit = price * fx
            for lot, take in taken:
                realized += (sell_unit - lot.unit_cost) * take
            realized -= fee * fx
            cash += (quantity * price - fee) * fx
            lots[tx.security_id] = [lot for lot in position_lots if lot.quantity > E8]
        else:
            raise ValueError(f"Unknown transaction side '{tx.side}'.")

    # Apply splits that occurred after the last transaction but before as_of.
    while split_index < len(splits):
        split = splits[split_index]
        for lot in lots.get(split.security_id, []):
            if lot.acquired < split.action_date:
                lot.quantity *= split.ratio
                lot.unit_cost /= split.ratio
        split_index += 1

    positions: dict[str, PositionState] = {}
    for security_id, position_lots in lots.items():
        kept: list[Lot] = []
        for lot in position_lots:
            if lot.quantity <= E8:
                continue
            if lot.quantity < D(settings.lot_epsilon):
                realized -= lot.unit_cost * lot.quantity
                continue
            kept.append(lot)
        if not kept:
            continue
        total_qty = sum((lot.quantity for lot in kept), Decimal("0"))
        total_cost = sum((lot.unit_cost * lot.quantity for lot in kept), Decimal("0"))
        positions[security_id] = PositionState(
            security_id=security_id,
            symbol=securities.get(security_id, security_id),
            quantity=total_qty,
            avg_cost=total_cost / total_qty if total_qty else Decimal("0"),
            lots=kept,
        )
    lots = {sid: p.lots for sid, p in positions.items()}

    last_tx = transactions[-1] if transactions else None
    last_flow = flows[-1] if flows else None
    return BookState(
        account_id=account.id,
        account_code=account.account_code,
        currency=account.currency,
        cash=cash,
        positions=positions,
        realized=realized,
        last_tx_id=last_tx.id if last_tx else None,
        last_tx_rev=max((t.rev for t in transactions), default=0),
        last_flow_id=last_flow.id if last_flow else None,
        last_flow_rev=max((f.rev for f in flows), default=0),
    )
