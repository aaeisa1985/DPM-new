"""Bootstrap routes: Clients, Mandates, Versions, Securities, Accounts, Flows, FX, Prices."""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from typing import Any

from fastapi import APIRouter, Depends, Query
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.schemas import (
    AccountCreate,
    ClientCreate,
    DividendCreate,
    FlowCreate,
    FxCreate,
    MandateCreate,
    PriceCreate,
    SecurityCreate,
    VersionCreate,
)
from app.api.serializers import (
    account_out,
    client_out,
    flow_out,
    mandate_out,
    security_out,
    version_out,
)
from app.core.audit import write_audit
from app.core.db import get_db
from app.core.errors import DpmError, NotFoundError
from app.core.money import s_cash, s_price
from app.engines.accounting import build_book
from app.engines.pricing import record_fx, record_manual_price, sync_daily_prices, value_positions
from app.models.entities import Account, Client, Mandate, MandateVersion, PriceHistory, Security
from app.services.ledger import create_dividend, create_flow, resolve_security

router = APIRouter(tags=["bootstrap"])


@router.get("/clients")
def list_clients(db: Session = Depends(get_db)) -> list[dict[str, Any]]:
    clients = db.execute(select(Client).order_by(Client.code)).scalars().all()
    today = date.today()
    result = []
    for client in clients:
        out = client_out(client)
        accounts = db.execute(select(Account).where(Account.client_id == client.id)).scalars().all()
        total_val = Decimal("0")
        for acc in accounts:
            try:
                book = build_book(db, acc.id, today)
                val = value_positions(db, book.positions, today, acc.currency)
                total_val += val.total + book.cash
            except Exception:
                pass
        out["total_value"] = s_cash(total_val, client.base_currency)
        out["accounts_count"] = len(accounts)
        result.append(out)
    return result


@router.post("/clients", status_code=201)
def create_client(payload: ClientCreate, db: Session = Depends(get_db)) -> dict[str, Any]:
    existing = db.execute(select(Client).where(Client.code == payload.code.upper())).scalar_one_or_none()
    if existing:
        raise DpmError(f"Client code '{payload.code}' already exists.", code="client_exists", status=409)
    client = Client(
        code=payload.code.upper(),
        display_name=payload.display_name,
        base_currency=payload.base_currency.upper(),
    )
    db.add(client)
    db.flush()
    write_audit(
        db,
        entity_type="client",
        entity_id=client.id,
        action="create",
        payload={"code": client.code, "display_name": client.display_name, "currency": client.base_currency},
    )
    db.commit()
    db.refresh(client)
    return client_out(client)


@router.get("/mandates")
def list_mandates(client_id: str | None = None, db: Session = Depends(get_db)) -> list[dict[str, Any]]:
    query = select(Mandate).order_by(Mandate.created_at.desc())
    if client_id:
        query = query.where(Mandate.client_id == client_id)
    mandates = db.execute(query).scalars().all()
    res = []
    for m in mandates:
        out = mandate_out(m)
        latest_ver = db.execute(
            select(MandateVersion)
            .where(MandateVersion.mandate_id == m.id)
            .order_by(MandateVersion.version.desc())
            .limit(1)
        ).scalar_one_or_none()
        out["current_version"] = latest_ver.version if latest_ver else None
        res.append(out)
    return res


@router.post("/mandates", status_code=201)
def create_mandate(
    payload: MandateCreate,
    force: bool = Query(False, description="Allow multiple active mandates if explicit"),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    client = db.get(Client, payload.client_id)
    if client is None:
        raise NotFoundError(f"Client '{payload.client_id}' not found.")
    if not force and payload.status == "active":
        active_existing = db.execute(
            select(Mandate).where(Mandate.client_id == client.id, Mandate.status == "active")
        ).scalar_one_or_none()
        if active_existing:
            raise DpmError(
                f"Client '{client.code}' already has an active mandate ({active_existing.name}). "
                "Use force=true to override.",
                code="mandate_conflict",
                status=409,
            )
    mandate = Mandate(
        client_id=payload.client_id,
        name=payload.name,
        status=payload.status,
    )
    db.add(mandate)
    db.flush()
    write_audit(
        db,
        entity_type="mandate",
        entity_id=mandate.id,
        action="create",
        payload={"client_id": client.id, "name": mandate.name, "status": mandate.status},
    )
    db.commit()
    db.refresh(mandate)
    return mandate_out(mandate)


@router.post("/mandates/{id}/versions", status_code=201)
def create_mandate_version(
    id: str,
    payload: VersionCreate,
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    mandate = db.get(Mandate, id)
    if mandate is None:
        raise NotFoundError(f"Mandate '{id}' not found.")
    last_ver = db.execute(
        select(MandateVersion.version)
        .where(MandateVersion.mandate_id == mandate.id)
        .order_by(MandateVersion.version.desc())
        .limit(1)
    ).scalar_one_or_none()
    new_version_num = (last_ver or 0) + 1

    benchmark_id = None
    if payload.benchmark_symbol:
        bench_sec = resolve_security(db, payload.benchmark_symbol)
        benchmark_id = bench_sec.id

    version = MandateVersion(
        mandate_id=mandate.id,
        version=new_version_num,
        effective_from=payload.effective_from,
        drift_tolerance=payload.drift_tolerance,
        lot_method=payload.lot_method,
        benchmark_id=benchmark_id,
        weights_json={k: str(v) for k, v in payload.weights.items()},
    )
    db.add(version)
    db.flush()
    write_audit(
        db,
        entity_type="mandate_version",
        entity_id=version.id,
        action="create",
        payload={
            "mandate_id": mandate.id,
            "version": version.version,
            "effective_from": version.effective_from.isoformat(),
            "weights": version.weights_json,
        },
    )
    db.commit()
    db.refresh(version)
    return version_out(version)


@router.get("/securities")
def list_securities(db: Session = Depends(get_db)) -> list[dict[str, Any]]:
    rows = db.execute(select(Security).order_by(Security.symbol)).scalars().all()
    return [security_out(s) for s in rows]


@router.post("/securities", status_code=201)
def create_security(payload: SecurityCreate, db: Session = Depends(get_db)) -> dict[str, Any]:
    existing = db.execute(
        select(Security).where(Security.symbol == payload.symbol.upper())
    ).scalar_one_or_none()
    if existing:
        raise DpmError(f"Security '{payload.symbol}' already exists.", code="security_exists", status=409)
    security = Security(
        symbol=payload.symbol.upper(),
        yahoo_ticker=payload.yahoo_ticker,
        name=payload.name,
        asset_class=payload.asset_class,
        currency=payload.currency.upper(),
        price_source=payload.price_source,
        price_unit=payload.price_unit,
        divisor=payload.divisor,
        settlement_days=payload.settlement_days,
        manual_price=payload.manual_price,
    )
    db.add(security)
    db.flush()
    write_audit(
        db,
        entity_type="security",
        entity_id=security.id,
        action="create",
        payload={"symbol": security.symbol, "currency": security.currency, "source": security.price_source},
    )
    db.commit()
    db.refresh(security)
    return security_out(security)


@router.get("/accounts")
def list_accounts(
    client_id: str | None = None,
    mandate_id: str | None = None,
    db: Session = Depends(get_db),
) -> list[dict[str, Any]]:
    query = select(Account).order_by(Account.account_code)
    if client_id:
        query = query.where(Account.client_id == client_id)
    if mandate_id:
        query = query.where(Account.mandate_id == mandate_id)
    accounts = db.execute(query).scalars().all()
    return [account_out(a) for a in accounts]


@router.post("/accounts", status_code=201)
def create_account(payload: AccountCreate, db: Session = Depends(get_db)) -> dict[str, Any]:
    client = db.get(Client, payload.client_id)
    if client is None:
        raise NotFoundError(f"Client '{payload.client_id}' not found.")
    mandate = db.get(Mandate, payload.mandate_id)
    if mandate is None:
        raise NotFoundError(f"Mandate '{payload.mandate_id}' not found.")
    existing_mandate_acc = db.execute(
        select(Account).where(Account.mandate_id == mandate.id)
    ).scalar_one_or_none()
    if existing_mandate_acc:
        raise DpmError(
            f"Mandate '{mandate.id}' already has account {existing_mandate_acc.account_code} (1-to-1 required).",
            code="mandate_account_conflict",
            status=409,
        )
    existing_code = db.execute(
        select(Account).where(Account.account_code == payload.account_code.upper())
    ).scalar_one_or_none()
    if existing_code:
        raise DpmError(
            f"Account code '{payload.account_code}' already exists.",
            code="account_code_conflict",
            status=409,
        )
    account = Account(
        account_code=payload.account_code.upper(),
        client_id=client.id,
        mandate_id=mandate.id,
        currency=payload.currency.upper(),
        opening_cash=payload.opening_cash,
    )
    db.add(account)
    db.flush()
    write_audit(
        db,
        entity_type="account",
        entity_id=account.id,
        action="create",
        payload={
            "account_code": account.account_code,
            "currency": account.currency,
            "opening_cash": str(account.opening_cash),
        },
    )
    db.commit()
    db.refresh(account)
    return account_out(account)


@router.post("/accounts/{id}/flows", status_code=201)
def add_flow(id: str, payload: FlowCreate, db: Session = Depends(get_db)) -> dict[str, Any]:
    account = db.get(Account, id)
    if account is None:
        raise NotFoundError(f"Account '{id}' not found.")
    flow = create_flow(
        db,
        account_id=account.id,
        flow_date=payload.flow_date,
        type=payload.type,
        amount=payload.amount,
        security_id=payload.security_id,
        external_ref=payload.external_ref,
        sequence=payload.sequence,
    )
    db.commit()
    db.refresh(flow)
    return flow_out(flow, account.currency)


@router.post("/accounts/{id}/dividends", status_code=201)
def add_dividend(id: str, payload: DividendCreate, db: Session = Depends(get_db)) -> dict[str, Any]:
    account = db.get(Account, id)
    if account is None:
        raise NotFoundError(f"Account '{id}' not found.")
    flow = create_dividend(
        db,
        account_id=account.id,
        on_date=payload.date,
        security_id=payload.security_id,
        per_unit=payload.per_unit,
        external_ref=payload.external_ref,
    )
    db.commit()
    db.refresh(flow)
    return flow_out(flow, account.currency)


@router.post("/fx", status_code=201)
def add_fx(payload: FxCreate, db: Session = Depends(get_db)) -> dict[str, Any]:
    fx = record_fx(db, payload.pair, payload.date, payload.rate, source="manual")
    write_audit(
        db,
        entity_type="fx_history",
        entity_id=f"{fx.pair}:{fx.as_of.isoformat()}",
        action="create",
        payload={"pair": fx.pair, "date": fx.as_of.isoformat(), "rate": str(fx.rate)},
    )
    db.commit()
    return {"pair": fx.pair, "date": fx.as_of.isoformat(), "rate": str(fx.rate), "source": fx.source}


@router.post("/prices/manual", status_code=201)
def add_manual_price(payload: PriceCreate, db: Session = Depends(get_db)) -> dict[str, Any]:
    security = resolve_security(db, payload.symbol)
    price_row = record_manual_price(
        db,
        security=security,
        price_date=payload.date,
        close=payload.close,
        currency=payload.currency,
    )
    write_audit(
        db,
        entity_type="price_history",
        entity_id=f"{security.id}:{payload.date.isoformat()}",
        action="create",
        payload={"symbol": security.symbol, "date": payload.date.isoformat(), "close": str(price_row.close)},
    )
    db.commit()
    return {
        "symbol": security.symbol,
        "date": price_row.price_date.isoformat(),
        "close": str(price_row.close),
        "source": price_row.source,
    }


@router.post("/prices/sync")
def sync_prices_endpoint(
    symbols: list[str] | None = Query(None, description="Optional list of symbols to sync"),
    force: bool = Query(False, description="Force refresh even if cached today"),
    as_of: date | None = Query(None, description="Target date (default today)"),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    result = sync_daily_prices(db, symbols=symbols, force=force, on_date=as_of)
    write_audit(
        db,
        entity_type="price_library",
        entity_id=result["date"],
        action="sync",
        payload={
            "from_cache_count": result["from_cache_count"],
            "fetched_yahoo_count": result["fetched_yahoo_count"],
            "fetched_eodhd_count": result["fetched_eodhd_count"],
        },
    )
    db.commit()
    return result


@router.get("/prices/daily")
def get_daily_price_library(
    as_of: date | None = Query(None, description="Date to inspect (default today)"),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    target = as_of or date.today()
    rows = db.execute(
        select(PriceHistory, Security)
        .join(Security, Security.id == PriceHistory.security_id)
        .where(PriceHistory.price_date == target)
        .order_by(Security.symbol)
    ).all()
    items = [
        {
            "symbol": sec.symbol,
            "name": sec.name,
            "currency": sec.currency,
            "close": s_price(ph.close),
            "source": ph.source,
            "price_date": ph.price_date.isoformat(),
            "fetched_at": ph.fetched_at.isoformat(),
        }
        for ph, sec in rows
    ]
    return {"date": target.isoformat(), "count": len(items), "prices": items}
