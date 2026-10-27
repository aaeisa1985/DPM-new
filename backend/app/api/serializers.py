"""Helpers to serialise domain objects with string money (D9)."""

from __future__ import annotations

from typing import Any

from app.core.money import s_cash, s_price, s_qty
from app.models.entities import (
    Account,
    CashFlow,
    Client,
    Mandate,
    MandateVersion,
    RebalanceOrder,
    RebalancePlan,
    Security,
    Transaction,
)


def client_out(client: Client) -> dict[str, Any]:
    return {
        "id": client.id,
        "code": client.code,
        "display_name": client.display_name,
        "base_currency": client.base_currency,
    }


def mandate_out(mandate: Mandate) -> dict[str, Any]:
    return {
        "id": mandate.id,
        "client_id": mandate.client_id,
        "name": mandate.name,
        "status": mandate.status,
    }


def version_out(v: MandateVersion) -> dict[str, Any]:
    return {
        "id": v.id,
        "mandate_id": v.mandate_id,
        "version": v.version,
        "effective_from": v.effective_from.isoformat(),
        "drift_tolerance": str(v.drift_tolerance),
        "lot_method": v.lot_method,
        "benchmark_id": v.benchmark_id,
        "weights": {k: str(val) for k, val in (v.weights_json or {}).items()},
    }


def account_out(a: Account) -> dict[str, Any]:
    return {
        "id": a.id,
        "account_code": a.account_code,
        "client_id": a.client_id,
        "mandate_id": a.mandate_id,
        "currency": a.currency,
        "opening_cash": s_cash(a.opening_cash, a.currency),
    }


def security_out(s: Security) -> dict[str, Any]:
    return {
        "id": s.id,
        "symbol": s.symbol,
        "yahoo_ticker": s.yahoo_ticker,
        "name": s.name,
        "asset_class": s.asset_class,
        "currency": s.currency,
        "price_source": s.price_source,
        "price_unit": s.price_unit,
        "divisor": str(s.divisor),
        "settlement_days": s.settlement_days,
        "manual_price": str(s.manual_price) if s.manual_price is not None else None,
    }


def tx_out(t: Transaction, symbol: str, currency: str) -> dict[str, Any]:
    return {
        "id": t.id,
        "account_id": t.account_id,
        "external_ref": t.external_ref,
        "date": t.trade_date.isoformat(),
        "sequence": t.sequence,
        "side": t.side,
        "security_id": t.security_id,
        "symbol": symbol,
        "quantity": s_qty(t.quantity),
        "unit_price": s_price(t.unit_price),
        "fee": s_cash(t.fee, currency),
        "fx_rate": s_price(t.fx_rate),
        "status": t.status,
        "void_reason": t.void_reason,
    }


def flow_out(f: CashFlow, currency: str) -> dict[str, Any]:
    return {
        "id": f.id,
        "account_id": f.account_id,
        "external_ref": f.external_ref,
        "date": f.flow_date.isoformat(),
        "sequence": f.sequence,
        "type": f.type,
        "security_id": f.security_id,
        "amount": s_cash(f.amount, currency),
    }


def order_out(o: RebalanceOrder, symbol: str, currency: str) -> dict[str, Any]:
    remaining = max(o.quantity - o.filled_quantity, 0)
    return {
        "order_id": o.id,
        "symbol": symbol,
        "side": o.side,
        "quantity": s_qty(o.quantity),
        "remaining_quantity": s_qty(remaining),
        "estimated_price": s_price(o.estimated_price),
        "estimated_value": s_cash(o.estimated_price * o.quantity, currency),
        "reason": o.reason,
        "status": o.status,
        "filled_quantity": s_qty(o.filled_quantity),
        "filled_price": s_price(o.filled_price),
        "filled_fee": s_cash(o.filled_fee, currency),
    }


def plan_out(plan: RebalancePlan, symbols: dict[str, str], currency: str) -> dict[str, Any]:
    return {
        "plan_id": plan.id,
        "mandate_id": plan.mandate_id,
        "status": plan.status,
        "created_at": plan.created_at.isoformat(),
        "expires_at": plan.expires_at.isoformat(),
        "cash_after": s_cash(plan.cash_after, currency),
        "cash_target": s_cash(plan.cash_target, currency),
        "est_fee_bps": str(plan.est_fee_bps),
        "snapshot_last_tx_id": plan.snapshot_last_tx_id,
        "snapshot_last_flow_id": plan.snapshot_last_flow_id,
        "orders": [order_out(o, symbols.get(o.security_id, "?"), currency) for o in plan.orders],
    }
