"""Performance and drift routes (spec 5.3 & 5.4)."""

from __future__ import annotations

from datetime import date
from typing import Any

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from app.core.db import get_db
from app.core.errors import NotFoundError
from app.core.money import s_cash
from app.engines.drift import compute_drift
from app.engines.performance import compute_performance
from app.models.entities import Account, Client, Mandate
from app.services.ledger import resolve_security

router = APIRouter(tags=["performance"])


@router.get("/clients/{id}/performance")
def get_performance(
    id: str,
    start: date | None = Query(None, description="Start date (default earliest activity)"),
    end: date | None = Query(None, description="End date (default today)"),
    benchmark_symbol: str | None = Query(None, description="Optional benchmark symbol"),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    client = db.get(Client, id)
    if client is None:
        raise NotFoundError(f"Client '{id}' not found.")
    today = end or date.today()

    bench_sec = None
    if benchmark_symbol:
        bench_sec = resolve_security(db, benchmark_symbol)

    perf = compute_performance(db, client_id=id, end=today, start=start, benchmark=bench_sec)
    ccy = client.base_currency

    points_out = []
    for p in perf.points:
        points_out.append(
            {
                "day": p.day.isoformat(),
                "close_value": s_cash(p.close_value, ccy),
                "start_value": s_cash(p.start_value, ccy),
                "external_flow": s_cash(p.external_flow, ccy),
                "daily_return": f"{p.daily_return:.8f}",
                "cumulative_return": f"{p.cumulative_return:.8f}",
                "carried_days": p.carried_days,
                "cash": s_cash(p.cash, ccy),
                "invested": s_cash(p.invested, ccy),
            }
        )

    bench_out = None
    if perf.benchmark:
        bench_out = {
            "symbol": perf.benchmark["symbol"],
            "total_return": f"{perf.benchmark['total_return']:.8f}",
            "points": [
                {
                    "day": pt["day"].isoformat(),
                    "value": f"{pt['value']:.4f}",
                    "cumulative_return": f"{pt['cumulative_return']:.8f}",
                }
                for pt in perf.benchmark["points"]
            ],
        }

    return {
        "client_id": client.id,
        "client_code": client.code,
        "base_currency": ccy,
        "start": perf.start.isoformat(),
        "end": perf.end.isoformat(),
        "twr": f"{perf.twr:.8f}",
        "annualized": f"{perf.annualized:.8f}",
        "max_drawdown": f"{perf.max_drawdown:.8f}",
        "max_carried_days": perf.max_carried_days,
        "points": points_out,
        "benchmark": bench_out,
    }


@router.get("/mandates/{id}/drift")
def get_drift(
    id: str,
    as_of: date | None = Query(None, description="Drift date (default today)"),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    mandate = db.get(Mandate, id)
    if mandate is None:
        raise NotFoundError(f"Mandate '{id}' not found.")
    today = as_of or date.today()
    drift = compute_drift(db, mandate_id=id, as_of=today)

    account = db.get(Account, drift.account_id)
    ccy = account.currency if account else "USD"

    rows_out = []
    for r in drift.rows:
        rows_out.append(
            {
                "key": r.key,
                "symbol": r.symbol,
                "security_id": r.security_id,
                "value": s_cash(r.value, ccy),
                "target_weight": f"{r.target_weight:.6f}",
                "actual_weight": f"{r.actual_weight:.6f}",
                "drift": f"{r.drift:.6f}",
                "breached": r.breached,
                "unassigned": r.unassigned,
            }
        )

    return {
        "mandate_id": drift.mandate_id,
        "account_id": drift.account_id,
        "as_of": drift.as_of.isoformat(),
        "total": s_cash(drift.total, ccy),
        "cash": s_cash(drift.cash, ccy),
        "tolerance": f"{drift.tolerance:.6f}",
        "version": drift.version,
        "max_carried_days": drift.carried_days,
        "rows": rows_out,
    }
