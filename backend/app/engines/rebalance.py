"""Settlement-aware rebalancing engine (spec 5.5).

A preview never writes. Confirming persists the exact preview with a snapshot
of the last transaction and flow. Execution refuses expired or stale plans.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.errors import (
    InfeasiblePlanError,
    NotFoundError,
    PlanExpiredError,
    PlanStaleError,
)
from app.core.money import D, floor_shares, q8
from app.engines.accounting import build_book
from app.engines.drift import account_for_mandate
from app.engines.pricing import convert_amount, get_price
from app.engines.versioning import effective_version
from app.models.entities import Account, Mandate, Security

FEE_RATE = Decimal("0.0001")
EPS = Decimal("0.01")


@dataclass
class OrderDraft:
    security_id: str
    symbol: str
    side: str
    quantity: Decimal
    price: Decimal
    value: Decimal
    fee: Decimal
    reason: str
    settlement_date: date | None = None
    currency: str = "USD"


@dataclass
class PlanDraft:
    mandate_id: str
    account_id: str
    as_of: date
    execution_date: date
    version: int
    total: Decimal
    cash: Decimal
    cash_target: Decimal
    cash_after: Decimal
    available_cash: Decimal
    est_fee_bps: Decimal
    tolerance: Decimal
    orders: list[OrderDraft] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    infeasible: bool = False
    infeasible_detail: str | None = None
    snapshot_last_tx_id: str | None = None
    snapshot_last_tx_rev: int = 0
    snapshot_last_flow_id: str | None = None
    snapshot_last_flow_rev: int = 0

    @property
    def max_settlement_days(self) -> int:
        return 0


def _fee(value: Decimal, bps: Decimal) -> Decimal:
    return q8(abs(value) * bps / Decimal("10000"))


def _actual_weight(value: Decimal, total: Decimal) -> Decimal:
    return value / total if total else Decimal("0")


def _build_draft(
    db: Session,
    mandate_id: str,
    as_of: date,
    execution_date: date | None,
) -> PlanDraft:
    mandate = db.get(Mandate, mandate_id)
    if mandate is None:
        raise NotFoundError(f"Mandate '{mandate_id}' not found.")
    account = account_for_mandate(db, mandate_id)
    version = effective_version(db, mandate_id, as_of)
    book = build_book(db, account.id, as_of)

    weights = {str(k).upper(): D(v) for k, v in (version.weights_json or {}).items()}
    if "CASH" not in weights:
        weights["CASH"] = Decimal("0")
    securities = {s.id: s for s in db.execute(select(Security)).scalars().all()}
    by_symbol = {s.symbol.upper(): s for s in securities.values()}

    price_rows: dict[str, tuple[Decimal, int]] = {}
    for security_id, position in book.positions.items():
        security = securities.get(security_id)
        if security is None:
            continue
        price_rows[security_id] = get_price(db, security, as_of)
    for key in weights:
        if key == "CASH":
            continue
        security = by_symbol.get(key)
        if security is None:
            raise NotFoundError(f"Target symbol '{key}' is not defined in securities.")
        if security.id not in price_rows:
            price_rows[security.id] = get_price(db, security, as_of)

    current_values: dict[str, Decimal] = {}
    total = book.cash
    for security_id, position in book.positions.items():
        security = securities.get(security_id)
        if security is None:
            continue
        price, _ = price_rows[security_id]
        value = convert_amount(db, position.quantity * price, security.currency, account.currency, as_of)
        current_values[security_id] = value
        total += value

    cash_target = weights["CASH"] * total
    target_values: dict[str, Decimal] = {}
    for key, weight in weights.items():
        if key == "CASH":
            continue
        security = by_symbol[key]
        target_values[security.id] = weight * total

    draft = PlanDraft(
        mandate_id=mandate_id,
        account_id=account.id,
        as_of=as_of,
        execution_date=execution_date or as_of,
        version=version.version,
        total=total,
        cash=book.cash,
        cash_target=cash_target,
        cash_after=book.cash,
        available_cash=book.cash,
        est_fee_bps=settings.est_fee_bps,
        tolerance=version.drift_tolerance,
        snapshot_last_tx_id=book.last_tx_id,
        snapshot_last_tx_rev=book.last_tx_rev,
        snapshot_last_flow_id=book.last_flow_id,
        snapshot_last_flow_rev=book.last_flow_rev,
    )

    union = set(current_values) | set(target_values)
    sells: list[OrderDraft] = []
    buys: list[OrderDraft] = []
    max_settle = 0
    for security_id in union:
        security = securities.get(security_id)
        if security is None:
            continue
        price, _ = price_rows[security_id]
        price_account = convert_amount(db, price, security.currency, account.currency, as_of)
        current = current_values.get(security_id, Decimal("0"))
        target = target_values.get(security_id, Decimal("0"))
        diff = target - current
        reason_current = _actual_weight(current, total) * Decimal("100")
        reason_target = _actual_weight(target, total) * Decimal("100")
        if diff < 0:
            wanted = min(-diff, current)
            quantity = floor_shares(wanted / price_account) if price_account else Decimal("0")
            if quantity <= 0:
                continue
            value = quantity * price_account
            if value < settings.dust_value:
                continue
            fee = _fee(value, settings.est_fee_bps)
            settle = as_of + timedelta(days=security.settlement_days)
            max_settle = max(max_settle, security.settlement_days)
            sells.append(
                OrderDraft(
                    security_id=security.id,
                    symbol=security.symbol,
                    side="SELL",
                    quantity=quantity,
                    price=price,
                    value=value,
                    fee=fee,
                    reason=(f"overweight {reason_current:.2f}% vs target {reason_target:.2f}%"),
                    settlement_date=settle,
                    currency=security.currency,
                )
            )
        elif diff > settings.dust_value:
            quantity = floor_shares(diff / price_account) if price_account else Decimal("0")
            if quantity <= 0:
                continue
            value = quantity * price_account
            if value < settings.dust_value:
                continue
            fee = _fee(value, settings.est_fee_bps)
            buys.append(
                OrderDraft(
                    security_id=security.id,
                    symbol=security.symbol,
                    side="BUY",
                    quantity=quantity,
                    price=price,
                    value=value,
                    fee=fee,
                    reason=f"underweight {reason_current:.2f}% vs target {reason_target:.2f}%",
                    currency=security.currency,
                )
            )

    if execution_date is None:
        execution_date = as_of + timedelta(days=max_settle)
        draft.execution_date = execution_date

    available = book.cash + sum(
        (
            order.value - order.fee
            for order in sells
            if order.settlement_date and order.settlement_date <= execution_date
        ),
        Decimal("0"),
    )
    draft.available_cash = available

    # Sell side is fixed. Trim buys until the post-trade cash target is met.
    def cash_after() -> Decimal:
        return (
            book.cash
            + sum((order.value - order.fee for order in sells), Decimal("0"))
            - sum((order.value + order.fee for order in buys), Decimal("0"))
        )

    def trim_buys() -> None:
        nonlocal available
        # First respect cash that has actually settled by the execution date.
        while buys:
            deployable = sum((order.value + order.fee for order in buys), Decimal("0"))
            if deployable <= available + EPS:
                break
            excess = deployable - available
            buys.sort(key=lambda order: order.value, reverse=True)
            order = buys[0]
            per_share = (order.value + order.fee) / order.quantity if order.quantity else Decimal("0")
            new_qty = max(
                order.quantity - (excess / per_share).to_integral_value(rounding="ROUND_CEILING"),
                Decimal("0"),
            )
            if new_qty == order.quantity:
                new_qty = order.quantity - 1
            if new_qty <= 0:
                buys.pop(0)
                continue
            order.quantity = floor_shares(new_qty)
            order.value = order.quantity * convert_amount(
                db, order.price, order.currency, account.currency, as_of
            )
            order.fee = _fee(order.value, settings.est_fee_bps)
            if order.value < settings.dust_value:
                buys.pop(0)

        while buys and cash_after() < cash_target - EPS:
            shortfall = cash_target - cash_after()
            buys.sort(key=lambda order: order.value, reverse=True)
            order = buys[0]
            per_share = (order.value + order.fee) / order.quantity if order.quantity else Decimal("0")
            reduce_by = (shortfall / per_share).to_integral_value(rounding="ROUND_CEILING")
            new_qty = order.quantity - reduce_by
            if new_qty <= 0:
                buys.pop(0)
                continue
            order.quantity = floor_shares(new_qty)
            order.value = order.quantity * convert_amount(
                db, order.price, order.currency, account.currency, as_of
            )
            order.fee = _fee(order.value, settings.est_fee_bps)
            if order.value < settings.dust_value:
                buys.pop(0)

    trim_buys()
    sells.sort(key=lambda order: order.symbol)
    buys.sort(key=lambda order: (-order.value, order.symbol))
    draft.orders = sells + buys
    draft.cash_after = cash_after()

    if draft.cash_after < cash_target - EPS:
        draft.infeasible = True
        deficit = cash_target - draft.cash_after
        draft.infeasible_detail = (
            f"Cash after all executable orders would be {draft.cash_after:.2f}, "
            f"below the {cash_target:.2f} target by {deficit:.2f}. "
            "No further sellable overweight position or buy reduction can close the gap."
        )
        raise InfeasiblePlanError(draft.infeasible_detail, report=_draft_dict(draft))

    if draft.cash_after < 0:
        raise InfeasiblePlanError(
            f"Cash after orders would be negative ({draft.cash_after:.2f}).",
            report=_draft_dict(draft),
        )
    return draft


def _draft_dict(draft: PlanDraft) -> dict:
    return {
        "mandate_id": draft.mandate_id,
        "account_id": draft.account_id,
        "as_of": draft.as_of.isoformat(),
        "execution_date": draft.execution_date.isoformat(),
        "version": draft.version,
        "total": str(q8(draft.total)),
        "cash": str(q8(draft.cash)),
        "cash_target": str(q8(draft.cash_target)),
        "cash_after": str(q8(draft.cash_after)),
        "available_cash": str(q8(draft.available_cash)),
        "est_fee_bps": str(draft.est_fee_bps),
        "tolerance": str(draft.tolerance),
        "orders": [
            {
                "security_id": order.security_id,
                "symbol": order.symbol,
                "side": order.side,
                "quantity": str(q8(order.quantity)),
                "price": str(q8(order.price)),
                "value": str(q8(order.value)),
                "fee": str(q8(order.fee)),
                "reason": order.reason,
                "settlement_date": order.settlement_date.isoformat() if order.settlement_date else None,
            }
            for order in draft.orders
        ],
    }


def preview_plan(db: Session, mandate_id: str, as_of: date, execution_date: date | None = None) -> PlanDraft:
    return _build_draft(db, mandate_id, as_of, execution_date)


def find_latest_activity(
    db: Session, account: Account, exclude_external_refs: set[str] | None = None
) -> tuple[str | None, int, str | None, int]:
    from app.models.entities import CashFlow, Transaction

    tx_query = select(Transaction).where(Transaction.account_id == account.id, Transaction.status == "active")
    if exclude_external_refs:
        tx_query = tx_query.where(
            (Transaction.external_ref.is_(None)) | (Transaction.external_ref.not_in(exclude_external_refs))
        )
    tx = db.execute(
        tx_query.order_by(
            Transaction.trade_date.desc(), Transaction.sequence.desc(), Transaction.created_at.desc()
        ).limit(1)
    ).scalar_one_or_none()
    flow_query = select(CashFlow).where(CashFlow.account_id == account.id)
    if exclude_external_refs:
        flow_query = flow_query.where(
            (CashFlow.external_ref.is_(None)) | (CashFlow.external_ref.not_in(exclude_external_refs))
        )
    flow = db.execute(
        flow_query.order_by(
            CashFlow.flow_date.desc(), CashFlow.sequence.desc(), CashFlow.created_at.desc()
        ).limit(1)
    ).scalar_one_or_none()
    return (
        tx.id if tx else None,
        max(tx.rev, 0) if tx else 0,
        flow.id if flow else None,
        max(flow.rev, 0) if flow else 0,
    )


def ensure_plan_executable(db: Session, plan) -> None:
    from app.models.entities import RebalancePlan

    assert isinstance(plan, RebalancePlan)
    now = datetime.now(UTC)
    expires = plan.expires_at
    if expires.tzinfo is None:
        expires = expires.replace(tzinfo=UTC)
    if now > expires or plan.status == "expired":
        raise PlanExpiredError(
            f"Plan expired at {expires.isoformat()}; generate a fresh preview.",
            plan_id=plan.id,
        )
    if plan.status not in {"confirmed", "executing"}:
        raise PlanStaleError(f"Plan is '{plan.status}' and cannot be executed.")
    account = account_for_mandate(db, plan.mandate_id)
    own_refs = {order.id for order in plan.orders}
    last_tx_id, last_tx_rev, last_flow_id, last_flow_rev = find_latest_activity(db, account, own_refs)
    if (
        last_tx_id != plan.snapshot_last_tx_id
        or last_tx_rev != plan.snapshot_last_tx_rev
        or last_flow_id != plan.snapshot_last_flow_id
        or last_flow_rev != plan.snapshot_last_flow_rev
    ):
        raise PlanStaleError(
            "The book changed after this plan was generated. Preview the plan again.",
            plan_id=plan.id,
            expected_tx=plan.snapshot_last_tx_id,
            actual_tx=last_tx_id,
            expected_flow=plan.snapshot_last_flow_id,
            actual_flow=last_flow_id,
        )
