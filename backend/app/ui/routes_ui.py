"""Web UI routes rendering HTML templates (Jinja2)."""

from __future__ import annotations

import json
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.serializers import client_out, plan_out
from app.core.audit import verify_chain
from app.core.db import get_db
from app.core.errors import InfeasiblePlanError
from app.core.money import s_cash
from app.engines.accounting import build_book
from app.engines.drift import account_for_mandate
from app.engines.pricing import value_positions
from app.models.entities import Account, AuditLog, Client, Mandate, MandateVersion, RebalancePlan
from app.services.backup import restore_backup
from app.services.csv_io import import_csv
from app.services.plans import plan_symbols

TEMPLATES_DIR = Path(__file__).resolve().parent / "templates"
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))

router = APIRouter(include_in_schema=False)


def _clients_with_values(db: Session) -> list[dict[str, Any]]:
    clients = db.execute(select(Client).order_by(Client.code)).scalars().all()
    today = date.today()
    res = []
    for c in clients:
        out = client_out(c)
        accounts = db.execute(select(Account).where(Account.client_id == c.id)).scalars().all()
        total_val = Decimal("0")
        for acc in accounts:
            try:
                book = build_book(db, acc.id, today)
                val = value_positions(db, book.positions, today, acc.currency)
                total_val += val.total + book.cash
            except Exception:
                pass
        out["total_value"] = s_cash(total_val, c.base_currency)
        out["accounts_count"] = len(accounts)
        res.append(out)
    return res


def _accounts_with_details(db: Session, client_id: str | None = None) -> list[dict[str, Any]]:
    stmt = select(Account).order_by(Account.account_code)
    if client_id:
        stmt = stmt.where(Account.client_id == client_id)
    accounts = db.execute(stmt).scalars().all()
    today = date.today()
    res = []
    for acc in accounts:
        client = db.get(Client, acc.client_id)
        mandate = db.get(Mandate, acc.mandate_id)
        book_cash = acc.opening_cash
        total_val = acc.opening_cash
        positions_count = 0
        try:
            book = build_book(db, acc.id, today)
            val = value_positions(db, book.positions, today, acc.currency)
            book_cash = book.cash
            total_val = val.total + book.cash
            positions_count = len(book.positions)
        except Exception:
            pass
        res.append(
            {
                "id": acc.id,
                "account_code": acc.account_code,
                "client_id": acc.client_id,
                "client_name": client.display_name if client else "-",
                "client_code": client.code if client else "-",
                "mandate_id": acc.mandate_id,
                "mandate_name": mandate.name if mandate else "-",
                "currency": acc.currency,
                "opening_cash": s_cash(acc.opening_cash, acc.currency),
                "current_cash": s_cash(book_cash, acc.currency),
                "total_value": s_cash(total_val, acc.currency),
                "positions_count": positions_count,
            }
        )
    return res


@router.get("/")
def root_redirect():
    return RedirectResponse(url="/ui/dashboard", status_code=302)


@router.get("/ui/dashboard", response_class=HTMLResponse)
def page_dashboard(request: Request, db: Session = Depends(get_db)):
    clients = _clients_with_values(db)
    accounts = _accounts_with_details(db)
    mandates = db.execute(select(Mandate)).scalars().all()
    plans_rows = (
        db.execute(select(RebalancePlan).order_by(RebalancePlan.created_at.desc()).limit(5)).scalars().all()
    )
    plans = []
    for p in plans_rows:
        syms = plan_symbols(db, p)
        try:
            acc = account_for_mandate(db, p.mandate_id)
            ccy = acc.currency
        except Exception:
            ccy = "USD"
        plans.append(plan_out(p, syms, ccy))

    recent_audit_rows = db.execute(select(AuditLog).order_by(AuditLog.id.desc()).limit(10)).scalars().all()
    recent_audit = [
        {
            "id": a.id,
            "at": a.at.isoformat(),
            "entity_type": a.entity_type,
            "action": a.action,
            "prev_hash": a.prev_hash,
        }
        for a in recent_audit_rows
    ]
    verification = verify_chain(db)

    return templates.TemplateResponse(
        request,
        "dashboard.html",
        {
            "active_tab": "dashboard",
            "clients": clients,
            "accounts": accounts,
            "mandates": mandates,
            "plans": plans,
            "recent_audit": recent_audit,
            "audit_valid": verification["valid"],
            "audit_rows": verification["rows"],
        },
    )


@router.get("/ui/clients", response_class=HTMLResponse)
def page_clients(request: Request, db: Session = Depends(get_db)):
    clients = _clients_with_values(db)
    accounts = _accounts_with_details(db)
    mandates = db.execute(select(Mandate).order_by(Mandate.name)).scalars().all()
    return templates.TemplateResponse(
        request,
        "clients.html",
        {
            "active_tab": "clients",
            "clients": clients,
            "accounts": accounts,
            "mandates": mandates,
        },
    )


@router.get("/ui/client/{id}", response_class=HTMLResponse)
def page_client_detail(id: str, request: Request, db: Session = Depends(get_db)):
    from app.api.routes_book import get_client_positions
    from app.api.routes_perf import get_performance

    client = db.get(Client, id)
    if client is None:
        raise HTTPException(status_code=404, detail="Client not found")

    client_accounts = _accounts_with_details(db, client_id=id)
    mandates = db.execute(select(Mandate).where(Mandate.client_id == id)).scalars().all()

    positions = get_client_positions(id=id, as_of=None, db=db)
    perf = None
    try:
        perf = get_performance(id=id, start=None, end=None, benchmark_symbol=None, db=db)
    except Exception:
        pass

    return templates.TemplateResponse(
        request,
        "client_detail.html",
        {
            "active_tab": "clients",
            "client": client,
            "client_accounts": client_accounts,
            "mandates": mandates,
            "positions": positions,
            "perf": perf,
        },
    )


@router.get("/ui/mandates", response_class=HTMLResponse)
def page_mandates(request: Request, db: Session = Depends(get_db)):
    mandates = db.execute(select(Mandate).order_by(Mandate.name)).scalars().all()
    mandates_out = []
    for m in mandates:
        latest = db.execute(
            select(MandateVersion)
            .where(MandateVersion.mandate_id == m.id)
            .order_by(MandateVersion.version.desc())
            .limit(1)
        ).scalar_one_or_none()
        mandates_out.append(
            {
                "id": m.id,
                "name": m.name,
                "status": m.status,
                "current_version": latest.version if latest else None,
            }
        )

    return templates.TemplateResponse(
        request,
        "mandates.html",
        {
            "active_tab": "mandates",
            "mandates": mandates_out,
            "today": date.today().isoformat(),
            "selected_drift": None,
            "selected_mandate": None,
        },
    )


@router.get("/ui/mandate/{id}/drift", response_class=HTMLResponse)
def page_mandate_drift(id: str, request: Request, db: Session = Depends(get_db)):
    from app.api.routes_perf import get_drift

    mandate = db.get(Mandate, id)
    if mandate is None:
        raise HTTPException(status_code=404, detail="Mandate not found")

    mandates = db.execute(select(Mandate).order_by(Mandate.name)).scalars().all()
    mandates_out = []
    for m in mandates:
        latest = db.execute(
            select(MandateVersion)
            .where(MandateVersion.mandate_id == m.id)
            .order_by(MandateVersion.version.desc())
            .limit(1)
        ).scalar_one_or_none()
        mandates_out.append(
            {
                "id": m.id,
                "name": m.name,
                "status": m.status,
                "current_version": latest.version if latest else None,
            }
        )

    drift = get_drift(id=id, as_of=None, db=db)

    return templates.TemplateResponse(
        request,
        "mandates.html",
        {
            "active_tab": "mandates",
            "mandates": mandates_out,
            "today": date.today().isoformat(),
            "selected_drift": drift,
            "selected_mandate": mandate,
        },
    )


@router.get("/ui/rebalance", response_class=HTMLResponse)
def page_rebalance(
    request: Request,
    mandate_id: str | None = None,
    as_of: str | None = None,
    execution_date: str | None = None,
    action: str | None = None,
    db: Session = Depends(get_db),
):
    from app.api.routes_plans import get_rebalance_preview

    mandates = db.execute(select(Mandate).order_by(Mandate.name)).scalars().all()
    today_str = date.today().isoformat()
    selected_as_of = as_of or today_str
    selected_exec = execution_date or today_str

    preview = None
    preview_error = None
    if action == "preview" and mandate_id:
        try:
            d_as_of = date.fromisoformat(selected_as_of)
            d_exec = date.fromisoformat(selected_exec) if selected_exec else None
            preview = get_rebalance_preview(id=mandate_id, as_of=d_as_of, execution_date=d_exec, db=db)
        except InfeasiblePlanError as exc:
            preview_error = exc.detail
        except Exception as exc:
            preview_error = str(exc)

    plans_rows = db.execute(select(RebalancePlan).order_by(RebalancePlan.created_at.desc())).scalars().all()
    plans = []
    for p in plans_rows:
        syms = plan_symbols(db, p)
        try:
            acc = account_for_mandate(db, p.mandate_id)
            ccy = acc.currency
        except Exception:
            ccy = "USD"
        plans.append(plan_out(p, syms, ccy))

    return templates.TemplateResponse(
        request,
        "rebalance.html",
        {
            "active_tab": "rebalance",
            "mandates": mandates,
            "selected_mandate_id": mandate_id,
            "as_of": selected_as_of,
            "execution_date": selected_exec,
            "preview": preview,
            "preview_error": preview_error,
            "plans": plans,
        },
    )


@router.get("/ui/plan/{id}", response_class=HTMLResponse)
def page_plan_detail(id: str, request: Request, db: Session = Depends(get_db)):
    plan = db.get(RebalancePlan, id)
    if plan is None:
        raise HTTPException(status_code=404, detail="Plan not found")
    syms = plan_symbols(db, plan)
    acc = account_for_mandate(db, plan.mandate_id)
    return templates.TemplateResponse(
        request,
        "plan_detail.html",
        {
            "active_tab": "rebalance",
            "plan": plan_out(plan, syms, acc.currency),
        },
    )


@router.get("/ui/data", response_class=HTMLResponse)
def page_data(request: Request):
    return templates.TemplateResponse(
        request,
        "data.html",
        {
            "active_tab": "data",
            "import_result": None,
        },
    )


@router.get("/ui/audit", response_class=HTMLResponse)
def page_audit(request: Request, db: Session = Depends(get_db)):
    verification = verify_chain(db)
    rows_data = db.execute(select(AuditLog).order_by(AuditLog.id.desc()).limit(100)).scalars().all()
    rows = []
    for r in rows_data:
        try:
            payload = json.loads(r.payload_json)
        except Exception:
            payload = r.payload_json
        rows.append(
            {
                "id": r.id,
                "at": r.at.isoformat(),
                "actor": r.actor,
                "entity_type": r.entity_type,
                "action": r.action,
                "payload": payload,
                "prev_hash": r.prev_hash,
                "row_hash": r.row_hash,
            }
        )

    return templates.TemplateResponse(
        request,
        "audit.html",
        {
            "active_tab": "audit",
            "verification": verification,
            "rows": rows,
        },
    )


# ---------------- Action Endpoints for Web Forms ----------------


@router.post("/ui/actions/client")
def action_create_client(
    code: str = Form(...),
    display_name: str = Form(...),
    base_currency: str = Form("USD"),
    db: Session = Depends(get_db),
):
    from app.api.routes_bootstrap import create_client
    from app.api.schemas import ClientCreate

    create_client(
        payload=ClientCreate(code=code, display_name=display_name, base_currency=base_currency),
        db=db,
    )
    return RedirectResponse(url="/ui/clients", status_code=303)


@router.post("/ui/actions/account")
def action_create_account(
    client_id: str = Form(...),
    account_code: str = Form(...),
    currency: str = Form("USD"),
    opening_cash: str = Form("0.00"),
    mandate_name: str | None = Form(None),
    mandate_id: str | None = Form(None),
    target_cash_pct: str = Form("10"),
    redirect_to: str = Form("/ui/clients"),
    db: Session = Depends(get_db),
):
    from app.core.audit import write_audit

    client = db.get(Client, client_id)
    if client is None:
        raise HTTPException(status_code=404, detail="Client not found")

    m_id = mandate_id if mandate_id and mandate_id.strip() else None
    if not m_id:
        m_name = (mandate_name or "").strip() or f"{account_code.upper().strip()} Mandate"
        mandate = Mandate(client_id=client.id, name=m_name, status="active")
        db.add(mandate)
        db.flush()
        write_audit(
            db,
            entity_type="mandate",
            entity_id=mandate.id,
            action="create",
            payload={"client_id": client.id, "name": mandate.name, "status": mandate.status},
        )
        try:
            cash_val = Decimal(target_cash_pct or "10") / Decimal("100")
            if cash_val > 1 or cash_val < 0:
                cash_val = Decimal("0.10")
        except Exception:
            cash_val = Decimal("0.10")

        version = MandateVersion(
            mandate_id=mandate.id,
            version=1,
            effective_from=date.today(),
            drift_tolerance=Decimal("0.05"),
            lot_method="fifo",
            weights_json={"CASH": str(cash_val)},
        )
        db.add(version)
        db.flush()
        write_audit(
            db,
            entity_type="mandate_version",
            entity_id=version.id,
            action="create",
            payload={"mandate_id": mandate.id, "version": 1, "weights": version.weights_json},
        )
        m_id = mandate.id

    account = Account(
        account_code=account_code.strip().upper(),
        client_id=client.id,
        mandate_id=m_id,
        currency=currency.strip().upper(),
        opening_cash=Decimal(opening_cash or "0.00"),
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
    return RedirectResponse(url=redirect_to, status_code=303)


@router.post("/ui/actions/flow")
def action_create_flow(
    account_code: str = Form(...),
    flow_type: str = Form("deposit"),
    amount: str = Form(...),
    external_ref: str | None = Form(None),
    flow_date: str | None = Form(None),
    redirect_to: str = Form("/ui/clients"),
    db: Session = Depends(get_db),
):
    from app.services.ledger import create_flow, resolve_account

    account = resolve_account(db, account_code=account_code.strip().upper())
    d_flow = date.fromisoformat(flow_date) if flow_date else date.today()
    ref = (external_ref or "").strip() or f"UI-{flow_type.upper()}-{date.today().strftime('%Y%m%d%H%M%S')}"
    create_flow(
        db,
        account_id=account.id,
        flow_date=d_flow,
        type=flow_type,
        amount=Decimal(amount),
        external_ref=ref,
    )
    db.commit()
    return RedirectResponse(url=redirect_to, status_code=303)


@router.post("/ui/actions/sync-prices")
def action_sync_prices(
    symbols_text: str = Form(...),
    source: str = Form("auto"),
    force: str | None = Form(None),
    redirect_to: str = Form("/ui/data"),
    db: Session = Depends(get_db),
):
    from app.engines.pricing import sync_daily_prices

    syms = [
        s.strip().upper() for s in symbols_text.replace("\n", ",").replace(" ", ",").split(",") if s.strip()
    ]
    if syms:
        sync_daily_prices(db, symbols=syms, source=source, force=force == "true")
        db.commit()
    return RedirectResponse(url=f"{redirect_to}?synced=true", status_code=303)


@router.post("/ui/actions/version")
def action_create_version(
    mandate_id: str = Form(...),
    effective_from: str = Form(...),
    drift_tolerance: str = Form("0.05"),
    lot_method: str = Form("fifo"),
    weights_text: str = Form(...),
    db: Session = Depends(get_db),
):
    from app.api.routes_bootstrap import create_mandate_version
    from app.api.schemas import VersionCreate

    weights = json.loads(weights_text)
    payload = VersionCreate(
        effective_from=date.fromisoformat(effective_from),
        drift_tolerance=Decimal(drift_tolerance),
        lot_method=lot_method,  # type: ignore
        weights=weights,
    )
    create_mandate_version(id=mandate_id, payload=payload, db=db)
    return RedirectResponse(url=f"/ui/mandate/{mandate_id}/drift", status_code=303)


@router.post("/ui/actions/plan/confirm")
def action_confirm_plan(
    mandate_id: str = Form(...),
    as_of: str = Form(...),
    execution_date: str = Form(...),
    db: Session = Depends(get_db),
):
    from app.api.routes_plans import confirm_rebalance_plan

    d_as_of = date.fromisoformat(as_of) if as_of else None
    d_exec = date.fromisoformat(execution_date) if execution_date else None
    plan = confirm_rebalance_plan(id=mandate_id, as_of=d_as_of, execution_date=d_exec, db=db)
    return RedirectResponse(url=f"/ui/plan/{plan['plan_id']}", status_code=303)


@router.post("/ui/actions/order/fill")
def action_fill_order(
    plan_id: str = Form(...),
    order_id: str = Form(...),
    quantity: str = Form(...),
    price: str = Form(...),
    fee: str = Form("0.00"),
    db: Session = Depends(get_db),
):
    from app.api.routes_plans import fill_plan_order
    from app.api.schemas import FillCreate

    fill_plan_order(
        id=plan_id,
        oid=order_id,
        payload=FillCreate(quantity=Decimal(quantity), price=Decimal(price), fee=Decimal(fee)),
        fill_date=None,
        db=db,
    )
    return RedirectResponse(url=f"/ui/plan/{plan_id}", status_code=303)


@router.post("/ui/actions/order/cancel")
def action_cancel_order(
    plan_id: str = Form(...),
    order_id: str = Form(...),
    reason: str = Form(...),
    db: Session = Depends(get_db),
):
    from app.api.routes_plans import cancel_plan_order
    from app.api.schemas import CancelCreate

    cancel_plan_order(id=plan_id, oid=order_id, payload=CancelCreate(reason=reason), db=db)
    return RedirectResponse(url=f"/ui/plan/{plan_id}", status_code=303)


@router.post("/ui/actions/import")
async def action_import(
    request: Request,
    kind: str = Form(...),
    csv_file: UploadFile | None = File(None),
    csv_text: str = Form(""),
    dry_run: str | None = Form(None),
    db: Session = Depends(get_db),
):
    is_dry = dry_run == "true"
    content = ""
    if csv_file and csv_file.filename:
        raw = await csv_file.read()
        content = raw.decode("utf-8-sig", errors="replace")
    elif csv_text.strip():
        content = csv_text

    result = None
    if content:
        try:
            result = import_csv(db, kind=kind, content=content, dry_run=is_dry)
            if not is_dry:
                db.commit()
        except Exception as exc:
            result = {
                "kind": kind,
                "dry_run": is_dry,
                "total": 0,
                "accepted": [],
                "rejected": [{"row": 1, "code": "import_error", "detail": str(exc)}],
            }

    return templates.TemplateResponse(
        request,
        "data.html",
        {
            "active_tab": "data",
            "import_result": result,
        },
    )


@router.post("/ui/actions/restore")
async def action_restore(
    backup_file: UploadFile = File(...),
):
    data = await backup_file.read()
    restore_backup(data)
    return RedirectResponse(url="/ui/dashboard", status_code=303)
