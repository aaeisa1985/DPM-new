"""Rebalance plan persistence and manual execution lifecycle (D1)."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.audit import write_audit
from app.core.config import settings
from app.core.errors import NotFoundError, OrderStateError
from app.core.money import D, q8
from app.engines.pricing import convert_amount
from app.engines.rebalance import PlanDraft, ensure_plan_executable
from app.models.entities import Account, RebalanceOrder, RebalancePlan, Security
from app.services.ledger import create_transaction


def persist_plan(db: Session, draft: PlanDraft) -> RebalancePlan:
    now = datetime.now(UTC)
    plan = RebalancePlan(
        mandate_id=draft.mandate_id,
        created_at=now,
        expires_at=now + timedelta(seconds=settings.plan_ttl),
        status="confirmed",
        snapshot_last_tx_id=draft.snapshot_last_tx_id,
        snapshot_last_tx_rev=draft.snapshot_last_tx_rev,
        snapshot_last_flow_id=draft.snapshot_last_flow_id,
        snapshot_last_flow_rev=draft.snapshot_last_flow_rev,
        cash_after=q8(draft.cash_after),
        cash_target=q8(draft.cash_target),
        est_fee_bps=draft.est_fee_bps,
    )
    db.add(plan)
    db.flush()
    for order in draft.orders:
        db.add(
            RebalanceOrder(
                plan_id=plan.id,
                security_id=order.security_id,
                side=order.side,
                quantity=q8(order.quantity),
                estimated_price=q8(order.price),
                reason=order.reason,
                status="pending",
                filled_quantity=Decimal("0"),
                filled_price=Decimal("0"),
                filled_fee=Decimal("0"),
            )
        )
    db.flush()
    db.refresh(plan)
    write_audit(
        db,
        entity_type="rebalance_plan",
        entity_id=plan.id,
        action="plan_confirm",
        payload={
            "mandate_id": draft.mandate_id,
            "orders": len(draft.orders),
            "cash_after": str(q8(draft.cash_after)),
            "cash_target": str(q8(draft.cash_target)),
        },
    )
    return plan


def _account_for_plan(db: Session, plan: RebalancePlan) -> Account:
    account = db.execute(select(Account).where(Account.mandate_id == plan.mandate_id)).scalar_one_or_none()
    if account is None:
        raise NotFoundError(f"Mandate '{plan.mandate_id}' has no account.")
    return account


def _refresh_plan_status(plan: RebalancePlan) -> None:
    statuses = {order.status for order in plan.orders}
    if statuses <= {"filled", "cancelled"}:
        plan.status = "executed"


def fill_order(
    db: Session,
    *,
    plan_id: str,
    order_id: str,
    quantity: Decimal,
    price: Decimal,
    fee: Decimal,
    fill_date: date | None = None,
) -> tuple[RebalanceOrder, dict]:
    plan = db.get(RebalancePlan, plan_id)
    if plan is None:
        raise NotFoundError(f"Plan '{plan_id}' not found.")
    order = db.get(RebalanceOrder, order_id)
    if order is None or order.plan_id != plan.id:
        raise NotFoundError(f"Order '{order_id}' not found on this plan.")
    ensure_plan_executable(db, plan)
    if order.status not in {"pending", "partial"}:
        raise OrderStateError(f"Order is already {order.status}.")
    remaining = D(order.quantity) - D(order.filled_quantity)
    qty, unit_price, fees = D(quantity), D(price), D(fee)
    if qty <= 0 or unit_price < 0 or fees < 0:
        raise OrderStateError("Fill quantity must be positive and price/fee non-negative.")
    if qty > remaining + Decimal("0.00000001"):
        raise OrderStateError(
            f"Fill quantity {qty} exceeds the remaining {remaining}.",
            remaining=str(remaining),
        )

    account = _account_for_plan(db, plan)
    security = db.get(Security, order.security_id)
    if security is None:
        raise NotFoundError(f"Security '{order.security_id}' not found.")
    day = fill_date or date.today()
    if security.currency.upper() == account.currency.upper():
        fx = Decimal("1")
    else:
        fx = convert_amount(db, Decimal("1"), security.currency, account.currency, day)

    tx = create_transaction(
        db,
        account_id=account.id,
        trade_date=day,
        side=order.side,
        symbol=security.symbol,
        quantity=qty,
        unit_price=unit_price,
        fee=fees,
        fx_rate=fx,
        external_ref=order.id,
        currency=security.currency,
    )

    old_qty = D(order.filled_quantity)
    new_qty = old_qty + qty
    order.filled_price = (
        q8(((D(order.filled_price) * old_qty) + (unit_price * qty)) / new_qty) if new_qty else Decimal("0")
    )
    order.filled_quantity = q8(new_qty)
    order.filled_fee = q8(D(order.filled_fee) + fees)
    order.status = "filled" if new_qty >= D(order.quantity) - Decimal("0.00000001") else "partial"
    plan.status = "executing"
    _refresh_plan_status(plan)
    db.flush()

    deviation = Decimal("0")
    if D(order.estimated_price):
        deviation = abs(unit_price - D(order.estimated_price)) / D(order.estimated_price)
    slippage_warning = deviation > settings.slippage_cap
    write_audit(
        db,
        entity_type="rebalance_order",
        entity_id=order.id,
        action="plan_execute",
        payload={
            "plan_id": plan.id,
            "side": order.side,
            "symbol": security.symbol,
            "quantity": str(qty),
            "price": str(unit_price),
            "fee": str(fees),
            "status": order.status,
            "transaction_id": tx.id,
            "slippage": str(q8(deviation)),
            "slippage_warning": slippage_warning,
        },
    )
    return order, {
        "transaction_id": tx.id,
        "deviation": q8(deviation),
        "slippage_warning": slippage_warning,
        "slippage_cap": settings.slippage_cap,
    }


def cancel_order(db: Session, *, plan_id: str, order_id: str, reason: str) -> RebalanceOrder:
    plan = db.get(RebalancePlan, plan_id)
    if plan is None:
        raise NotFoundError(f"Plan '{plan_id}' not found.")
    order = db.get(RebalanceOrder, order_id)
    if order is None or order.plan_id != plan.id:
        raise NotFoundError(f"Order '{order_id}' not found on this plan.")
    if order.status in {"filled", "cancelled"}:
        raise OrderStateError(f"Order is already {order.status}.")
    order.status = "cancelled"
    order.reason = f"{order.reason} | cancelled: {reason}".strip(" |")
    _refresh_plan_status(plan)
    db.flush()
    write_audit(
        db,
        entity_type="rebalance_order",
        entity_id=order.id,
        action="cancel",
        payload={"plan_id": plan.id, "reason": reason, "status": order.status},
    )
    return order


def plan_symbols(db: Session, plan: RebalancePlan) -> dict[str, str]:
    ids = {order.security_id for order in plan.orders}
    rows = db.execute(select(Security).where(Security.id.in_(ids))).scalars().all() if ids else []
    return {security.id: security.symbol for security in rows}
