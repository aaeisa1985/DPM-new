"""Rebalance planning and execution routes (spec 5.5)."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from typing import Any

from fastapi import APIRouter, Depends, Query
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.schemas import CancelCreate, FillCreate, OptimizeRequest
from app.api.serializers import order_out, plan_out
from app.core.config import settings
from app.core.db import get_db
from app.core.errors import NotFoundError
from app.core.money import s_cash, s_price, s_qty
from app.engines.drift import account_for_mandate
from app.engines.optimizer import propose_weights
from app.engines.rebalance import preview_plan
from app.models.entities import Mandate, RebalancePlan, Security
from app.services.plans import (
    cancel_order,
    fill_order,
    persist_plan,
    plan_symbols,
)

router = APIRouter(tags=["plans"])


@router.get("/mandates/{id}/rebalance/preview")
def get_rebalance_preview(
    id: str,
    as_of: date | None = Query(None, description="Valuation date (default today)"),
    execution_date: date | None = Query(None, description="Execution date"),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    mandate = db.get(Mandate, id)
    if mandate is None:
        raise NotFoundError(f"Mandate '{id}' not found.")
    today = as_of or date.today()
    draft = preview_plan(db, mandate_id=id, as_of=today, execution_date=execution_date)
    account = account_for_mandate(db, id)
    ccy = account.currency

    orders_out = []
    for idx, o in enumerate(draft.orders):
        orders_out.append(
            {
                "order_id": f"preview-{idx + 1}",
                "security_id": o.security_id,
                "symbol": o.symbol,
                "side": o.side,
                "quantity": s_qty(o.quantity),
                "estimated_price": s_price(o.price),
                "estimated_value": s_cash(o.value, ccy),
                "estimated_fee": s_cash(o.fee, ccy),
                "reason": o.reason,
                "settlement_date": o.settlement_date.isoformat() if o.settlement_date else None,
                "status": "pending",
            }
        )

    now = datetime.now(UTC)
    return {
        "mandate_id": draft.mandate_id,
        "account_id": draft.account_id,
        "currency": ccy,
        "status": "preview",
        "as_of": draft.as_of.isoformat(),
        "execution_date": draft.execution_date.isoformat(),
        "created_at": now.isoformat(),
        "expires_at": (now + timedelta(seconds=settings.plan_ttl)).isoformat(),
        "total": s_cash(draft.total, ccy),
        "cash": s_cash(draft.cash, ccy),
        "available_cash": s_cash(draft.available_cash, ccy),
        "cash_target": s_cash(draft.cash_target, ccy),
        "cash_after": s_cash(draft.cash_after, ccy),
        "est_fee_bps": str(draft.est_fee_bps),
        "tolerance": f"{draft.tolerance:.6f}",
        "orders": orders_out,
    }


@router.post("/mandates/{id}/rebalance/confirm", status_code=201)
def confirm_rebalance_plan(
    id: str,
    as_of: date | None = Query(None, description="Valuation date (default today)"),
    execution_date: date | None = Query(None, description="Execution date"),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    mandate = db.get(Mandate, id)
    if mandate is None:
        raise NotFoundError(f"Mandate '{id}' not found.")
    today = as_of or date.today()
    draft = preview_plan(db, mandate_id=id, as_of=today, execution_date=execution_date)
    plan = persist_plan(db, draft)
    db.commit()
    db.refresh(plan)
    account = account_for_mandate(db, id)
    symbols = plan_symbols(db, plan)
    return plan_out(plan, symbols, account.currency)


@router.post("/plans/{id}/orders/{oid}/fill")
def fill_plan_order(
    id: str,
    oid: str,
    payload: FillCreate,
    fill_date: date | None = Query(None, description="Fill date (default today)"),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    plan = db.get(RebalancePlan, id)
    if plan is None:
        raise NotFoundError(f"Plan '{id}' not found.")
    order, extra = fill_order(
        db,
        plan_id=id,
        order_id=oid,
        quantity=payload.quantity,
        price=payload.price,
        fee=payload.fee,
        fill_date=fill_date,
    )
    db.commit()
    db.refresh(order)
    db.refresh(plan)
    account = account_for_mandate(db, plan.mandate_id)
    security = db.get(Security, order.security_id)
    symbol = security.symbol if security else "?"
    return {
        "order": order_out(order, symbol, account.currency),
        "plan_status": plan.status,
        "transaction_id": extra["transaction_id"],
        "slippage": str(extra["deviation"]),
        "slippage_warning": extra["slippage_warning"],
        "slippage_cap": str(extra["slippage_cap"]),
    }


@router.post("/plans/{id}/orders/{oid}/cancel")
def cancel_plan_order(
    id: str,
    oid: str,
    payload: CancelCreate,
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    plan = db.get(RebalancePlan, id)
    if plan is None:
        raise NotFoundError(f"Plan '{id}' not found.")
    order = cancel_order(db, plan_id=id, order_id=oid, reason=payload.reason)
    db.commit()
    db.refresh(order)
    db.refresh(plan)
    account = account_for_mandate(db, plan.mandate_id)
    security = db.get(Security, order.security_id)
    symbol = security.symbol if security else "?"
    return {
        "order": order_out(order, symbol, account.currency),
        "plan_status": plan.status,
    }


@router.get("/plans")
def list_plans(
    mandate_id: str | None = Query(None, description="Filter by mandate ID"),
    db: Session = Depends(get_db),
) -> list[dict[str, Any]]:
    query = select(RebalancePlan).order_by(RebalancePlan.created_at.desc())
    if mandate_id:
        query = query.where(RebalancePlan.mandate_id == mandate_id)
    plans = db.execute(query).scalars().all()
    res = []
    for plan in plans:
        symbols = plan_symbols(db, plan)
        account = account_for_mandate(db, plan.mandate_id)
        res.append(plan_out(plan, symbols, account.currency))
    return res


@router.post("/mandates/{id}/optimize")
def optimize_mandate(
    id: str,
    payload: OptimizeRequest | None = None,
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    mandate = db.get(Mandate, id)
    if mandate is None:
        raise NotFoundError(f"Mandate '{id}' not found.")
    symbols = payload.symbols if payload else None
    lookback = payload.lookback_days if payload else 730
    return propose_weights(db, mandate_id=id, symbols=symbols, lookback_days=lookback)
