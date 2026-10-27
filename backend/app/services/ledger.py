"""Write-side domain services: idempotent ledger writes and audit entries."""

from __future__ import annotations

import hashlib
import json
from datetime import date
from decimal import Decimal
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.audit import write_audit
from app.core.errors import (
    DpmError,
    DuplicateFlowError,
    DuplicateTransactionError,
    FxRateMissingError,
    NotFoundError,
    OverSellError,
)
from app.core.money import D
from app.models.entities import Account, CashFlow, Security, Transaction


def canonical_key(payload: dict[str, Any]) -> str:
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")
    ).hexdigest()


def resolve_account(db: Session, account_id: str | None = None, account_code: str | None = None) -> Account:
    if account_id:
        account = db.get(Account, account_id)
    elif account_code:
        account = db.execute(
            select(Account).where(Account.account_code == account_code.strip().upper())
        ).scalar_one_or_none()
    else:
        raise NotFoundError("account_id or account_code is required.")
    if account is None:
        raise NotFoundError(
            f"Account '{account_id or account_code}' not found.",
            account_id=account_id,
            account_code=account_code,
        )
    return account


def resolve_security(db: Session, symbol: str) -> Security:
    security = db.execute(
        select(Security).where(Security.symbol == symbol.strip().upper())
    ).scalar_one_or_none()
    if security is None:
        raise NotFoundError(f"Security '{symbol}' not found.", symbol=symbol)
    return security


def _next_sequence(db: Session, model, account_id: str, day_field, day: date) -> int:
    row = db.execute(
        select(model.sequence)
        .where(model.account_id == account_id, day_field == day)
        .order_by(model.sequence.desc())
        .limit(1)
    ).scalar_one_or_none()
    return int(row or 0) + 1


def create_transaction(
    db: Session,
    *,
    account_id: str | None = None,
    account_code: str | None = None,
    trade_date: date,
    side: str,
    symbol: str,
    quantity: Decimal,
    unit_price: Decimal,
    fee: Decimal = Decimal("0"),
    fx_rate: Decimal | None = None,
    external_ref: str | None = None,
    sequence: int | None = None,
    currency: str | None = None,
) -> Transaction:
    account = resolve_account(db, account_id, account_code)
    security = resolve_security(db, symbol)
    side = side.upper()
    if side not in {"BUY", "SELL"}:
        raise DpmError(f"Invalid side '{side}'.", side=side)
    qty, price, fees = D(quantity), D(unit_price), D(fee)
    if qty <= 0:
        raise DpmError("quantity must be positive.")
    if price < 0 or fees < 0:
        raise DpmError("unit_price and fee cannot be negative.")

    if fx_rate is None:
        if (currency or security.currency).upper() == account.currency.upper():
            fx = Decimal("1")
        else:
            raise FxRateMissingError(
                f"fx_rate is required to post {security.symbol} in {currency or security.currency} "
                f"to account {account.account_code} ({account.currency}).",
                account_id=account.id,
                symbol=security.symbol,
            )
    else:
        fx = D(fx_rate)
    if fx <= 0:
        raise DpmError("fx_rate must be positive.")

    if side == "SELL":
        from app.engines.accounting import build_book

        book = build_book(db, account.id, trade_date)
        available = book.positions[security.id].quantity if security.id in book.positions else Decimal("0")
        if qty > available + Decimal("0.00000001"):
            raise OverSellError(
                f"Cannot sell {qty} {security.symbol} on {trade_date.isoformat()}: "
                f"only {available} available.",
                symbol=security.symbol,
                requested=str(qty),
                available=str(available),
                account_id=account.id,
            )

    supplied_sequence = sequence
    if sequence is None:
        sequence = _next_sequence(db, Transaction, account.id, Transaction.trade_date, trade_date)
    else:
        sequence = int(sequence)

    key = canonical_key(
        {
            "kind": "transaction",
            "account_id": account.id,
            "trade_date": trade_date.isoformat(),
            "sequence": sequence if supplied_sequence is not None else "auto",
            "side": side,
            "security_id": security.id,
            "quantity": str(qty),
            "unit_price": str(price),
            "fee": str(fees),
            "fx_rate": str(fx),
            "external_ref": external_ref or "",
            "currency": (currency or security.currency).upper(),
        }
    )
    duplicate = db.execute(select(Transaction).where(Transaction.idempotency_key == key)).scalar_one_or_none()
    if duplicate is not None:
        raise DuplicateTransactionError(
            f"Duplicate transaction: identical to {duplicate.id}.",
            transaction_id=duplicate.id,
            trade_date=duplicate.trade_date.isoformat(),
            side=duplicate.side,
            symbol=security.symbol,
        )

    row = Transaction(
        account_id=account.id,
        external_ref=external_ref or None,
        trade_date=trade_date,
        sequence=sequence,
        side=side,
        security_id=security.id,
        quantity=qty,
        unit_price=price,
        fee=fees,
        fx_rate=fx,
        status="active",
        idempotency_key=key,
    )
    db.add(row)
    db.flush()
    write_audit(
        db,
        entity_type="transaction",
        entity_id=row.id,
        action="create",
        payload={
            "account": account.account_code,
            "date": trade_date.isoformat(),
            "side": side,
            "symbol": security.symbol,
            "quantity": str(qty),
            "price": str(price),
            "fee": str(fees),
            "fx_rate": str(fx),
            "external_ref": external_ref,
        },
    )
    return row


def void_transaction(db: Session, transaction_id: str, reason: str) -> Transaction:
    row = db.get(Transaction, transaction_id)
    if row is None:
        raise NotFoundError(f"Transaction '{transaction_id}' not found.")
    if row.status != "active":
        from app.core.errors import OrderStateError

        raise OrderStateError(f"Transaction is already {row.status}.")
    row.status = "voided"
    row.void_reason = reason
    row.rev += 1
    db.flush()
    write_audit(
        db,
        entity_type="transaction",
        entity_id=row.id,
        action="void",
        payload={"reason": reason},
    )
    return row


def create_flow(
    db: Session,
    *,
    account_id: str,
    flow_date: date,
    type: str,
    amount: Decimal,
    security_id: str | None = None,
    external_ref: str | None = None,
    sequence: int | None = None,
) -> CashFlow:
    account = resolve_account(db, account_id)
    flow_type = type.lower()
    if flow_type not in {"deposit", "withdrawal", "dividend", "fee"}:
        raise DpmError(f"Invalid flow type '{type}'.", type=type)
    value = D(amount)
    if flow_type in {"deposit", "dividend"} and value < 0:
        raise DpmError(f"{flow_type} amount must be positive.")
    if flow_type in {"withdrawal", "fee"}:
        value = -abs(value)
    if flow_type == "dividend" and not security_id:
        raise DpmError("security_id is required for dividends.")
    if security_id is not None:
        if db.get(Security, security_id) is None:
            raise NotFoundError(f"Security '{security_id}' not found.")
    supplied_sequence = sequence
    if sequence is None:
        sequence = _next_sequence(db, CashFlow, account.id, CashFlow.flow_date, flow_date)
    else:
        sequence = int(sequence)
    key = canonical_key(
        {
            "kind": "flow",
            "account_id": account.id,
            "flow_date": flow_date.isoformat(),
            "sequence": sequence if supplied_sequence is not None else "auto",
            "type": flow_type,
            "security_id": security_id or "",
            "amount": str(value),
            "external_ref": external_ref or "",
        }
    )
    duplicate = db.execute(select(CashFlow).where(CashFlow.idempotency_key == key)).scalar_one_or_none()
    if duplicate is not None:
        raise DuplicateFlowError(
            f"Duplicate cash flow: identical to {duplicate.id}.",
            flow_id=duplicate.id,
        )
    row = CashFlow(
        account_id=account.id,
        external_ref=external_ref or None,
        flow_date=flow_date,
        sequence=sequence,
        type=flow_type,
        security_id=security_id,
        amount=value,
        idempotency_key=key,
    )
    db.add(row)
    db.flush()
    write_audit(
        db,
        entity_type="cash_flow",
        entity_id=row.id,
        action="create",
        payload={
            "account": account.account_code,
            "date": flow_date.isoformat(),
            "type": flow_type,
            "amount": str(value),
            "security_id": security_id,
            "external_ref": external_ref,
        },
    )
    return row


def create_dividend(
    db: Session,
    *,
    account_id: str,
    on_date: date,
    security_id: str,
    per_unit: Decimal,
    external_ref: str | None = None,
) -> CashFlow:
    from app.engines.accounting import build_book

    account = resolve_account(db, account_id)
    security = db.get(Security, security_id)
    if security is None:
        raise NotFoundError(f"Security '{security_id}' not found.")
    book = build_book(db, account.id, on_date)
    position = book.positions.get(security_id)
    qty = position.quantity if position else Decimal("0")
    if qty <= 0:
        raise DpmError(
            f"No {security.symbol} position on {on_date.isoformat()} to receive a dividend.",
            symbol=security.symbol,
        )
    amount = D(per_unit) * qty
    flow = create_flow(
        db,
        account_id=account.id,
        flow_date=on_date,
        type="dividend",
        amount=amount,
        security_id=security_id,
        external_ref=external_ref,
    )
    write_audit(
        db,
        entity_type="cash_flow",
        entity_id=flow.id,
        action="dividend",
        payload={
            "symbol": security.symbol,
            "per_unit": str(D(per_unit)),
            "quantity": str(qty),
            "amount": str(amount),
        },
    )
    return flow


def create_split(
    db: Session,
    *,
    on_date: date,
    symbol: str,
    ratio: Decimal,
) -> Any:
    from app.models.entities import CorporateAction

    security = resolve_security(db, symbol)
    value = D(ratio)
    if value <= 0:
        raise DpmError("Split ratio must be positive.")
    key = canonical_key(
        {
            "kind": "split",
            "security_id": security.id,
            "date": on_date.isoformat(),
            "ratio": str(value),
        }
    )
    duplicate = db.execute(
        select(CorporateAction).where(CorporateAction.idempotency_key == key)
    ).scalar_one_or_none()
    if duplicate is not None:
        from app.core.errors import DuplicateActionError

        raise DuplicateActionError(
            f"Duplicate split: identical to {duplicate.id}.",
            action_id=duplicate.id,
        )
    row = CorporateAction(
        security_id=security.id,
        action_date=on_date,
        type="split",
        ratio=value,
        idempotency_key=key,
    )
    db.add(row)
    db.flush()
    write_audit(
        db,
        entity_type="corporate_action",
        entity_id=row.id,
        action="create",
        payload={"symbol": security.symbol, "date": on_date.isoformat(), "ratio": str(value)},
    )
    return row
