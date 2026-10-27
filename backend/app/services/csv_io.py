"""CSV import/export helpers.

Every import is dry-run by default. A dry run simulates the writes inside a
savepoint and always rolls back. A real import is all-or-nothing: the first
rejected row aborts the whole file.
"""

from __future__ import annotations

import csv
import io
from collections.abc import Callable
from datetime import date
from decimal import Decimal, InvalidOperation
from typing import Any

from sqlalchemy.orm import Session

from app.core.audit import write_audit
from app.core.errors import DpmError, ImportValidationError
from app.core.money import D
from app.engines.pricing import record_fx, record_manual_price
from app.services.ledger import (
    create_flow,
    create_split,
    create_transaction,
    resolve_account,
    resolve_security,
)

HEADERS: dict[str, list[str]] = {
    "transactions": [
        "date",
        "account_code",
        "external_ref",
        "side",
        "symbol",
        "quantity",
        "unit_price",
        "currency",
        "fee",
        "fx_rate",
        "sequence",
    ],
    "prices": ["date", "symbol", "close", "currency"],
    "fx": ["date", "pair", "rate"],
    "flows": ["date", "account_code", "external_ref", "type", "amount", "symbol"],
    "splits": ["date", "symbol", "ratio"],
}


def _clean(content: str) -> str:
    text = content.lstrip("\ufeff")
    return text.replace("\r\n", "\n").replace("\r", "\n")


def parse_csv(content: str, kind: str) -> list[dict[str, str]]:
    text = _clean(content).strip()
    if not text:
        raise ImportValidationError("CSV content is empty.")
    reader = csv.DictReader(io.StringIO(text))
    if reader.fieldnames is None:
        raise ImportValidationError("CSV has no header row.")
    fieldnames = [(name or "").strip().lower() for name in reader.fieldnames]
    required = HEADERS[kind]
    missing = [name for name in required if name not in fieldnames]
    if missing:
        raise ImportValidationError(
            f"CSV header is missing: {', '.join(missing)}.",
            kind=kind,
            missing=missing,
        )
    rows = []
    for raw in reader:
        row = {}
        for key, value in raw.items():
            if key is None:
                continue
            row[key.strip().lower()] = (value or "").strip()
        if not any(row.values()):
            continue
        rows.append(row)
    if not rows:
        raise ImportValidationError("CSV contains no data rows.")
    return rows


def _decimal(row: dict[str, str], field: str, required: bool = True) -> Decimal | None:
    raw = row.get(field, "")
    if raw == "":
        if required:
            raise ImportValidationError(f"'{field}' is required on this row.", field=field)
        return None
    try:
        return D(raw)
    except (InvalidOperation, ValueError) as exc:
        raise ImportValidationError(f"'{field}' is not a valid number: {raw}.", field=field) from exc


def _date(row: dict[str, str], field: str = "date") -> date:
    raw = row.get(field, "")
    if not raw:
        raise ImportValidationError(f"'{field}' is required on this row.", field=field)
    try:
        return date.fromisoformat(raw)
    except ValueError as exc:
        raise ImportValidationError(f"'{field}' must be ISO-8601 (YYYY-MM-DD): {raw}.", field=field) from exc


def _text(row: dict[str, str], field: str, required: bool = False) -> str | None:
    raw = row.get(field, "")
    if not raw:
        if required:
            raise ImportValidationError(f"'{field}' is required on this row.", field=field)
        return None
    return raw


def _handler_transactions(db: Session, row: dict[str, str]) -> dict[str, Any]:
    account_code = _text(row, "account_code", True)
    resolve_account(db, account_code=account_code)
    side = (_text(row, "side", True) or "").upper()
    tx = create_transaction(
        db,
        account_code=account_code,
        trade_date=_date(row),
        side=side,
        symbol=_text(row, "symbol", True) or "",
        quantity=_decimal(row, "quantity") or Decimal("0"),
        unit_price=_decimal(row, "unit_price") or Decimal("0"),
        fee=_decimal(row, "fee", False) or Decimal("0"),
        fx_rate=_decimal(row, "fx_rate", False),
        external_ref=_text(row, "external_ref"),
        sequence=int(_decimal(row, "sequence", False) or 0) or None,
        currency=_text(row, "currency"),
    )
    return {"id": tx.id, "symbol": row.get("symbol", "").upper(), "side": tx.side}


def _handler_prices(db: Session, row: dict[str, str]) -> dict[str, Any]:
    symbol = _text(row, "symbol", True) or ""
    security = resolve_security(db, symbol)
    price = record_manual_price(
        db, security, _date(row), _decimal(row, "close") or Decimal("0"), _text(row, "currency")
    )
    write_audit(
        db,
        entity_type="price_history",
        entity_id=f"{security.id}:{_date(row).isoformat()}",
        action="import",
        payload={"symbol": security.symbol, "date": _date(row).isoformat(), "close": str(price.close)},
    )
    return {"symbol": security.symbol, "date": _date(row).isoformat()}


def _handler_fx(db: Session, row: dict[str, str]) -> dict[str, Any]:
    pair = _text(row, "pair", True) or ""
    fx = record_fx(db, pair, _date(row), _decimal(row, "rate") or Decimal("0"))
    write_audit(
        db,
        entity_type="fx_history",
        entity_id=f"{fx.pair}:{fx.as_of.isoformat()}",
        action="import",
        payload={"pair": fx.pair, "date": fx.as_of.isoformat(), "rate": str(fx.rate)},
    )
    return {"pair": fx.pair, "date": fx.as_of.isoformat()}


def _handler_flows(db: Session, row: dict[str, str]) -> dict[str, Any]:
    account = resolve_account(db, account_code=_text(row, "account_code", True))
    symbol = _text(row, "symbol")
    security_id = resolve_security(db, symbol).id if symbol else None
    flow = create_flow(
        db,
        account_id=account.id,
        flow_date=_date(row),
        type=(_text(row, "type", True) or "").lower(),
        amount=_decimal(row, "amount") or Decimal("0"),
        security_id=security_id,
        external_ref=_text(row, "external_ref"),
    )
    return {"id": flow.id, "type": flow.type, "amount": str(flow.amount)}


def _handler_splits(db: Session, row: dict[str, str]) -> dict[str, Any]:
    action = create_split(
        db,
        on_date=_date(row),
        symbol=_text(row, "symbol", True) or "",
        ratio=_decimal(row, "ratio") or Decimal("0"),
    )
    return {"id": action.id, "symbol": row.get("symbol", "").upper()}


HANDLERS: dict[str, Callable[[Session, dict[str, str]], dict[str, Any]]] = {
    "transactions": _handler_transactions,
    "prices": _handler_prices,
    "fx": _handler_fx,
    "flows": _handler_flows,
    "splits": _handler_splits,
}


def import_csv(db: Session, kind: str, content: str, dry_run: bool = True) -> dict[str, Any]:
    if kind not in HANDLERS:
        raise ImportValidationError(f"Unknown import kind '{kind}'.", kind=kind)
    rows = parse_csv(content, kind)
    handler = HANDLERS[kind]
    accepted: list[dict[str, Any]] = []
    rejected: list[dict[str, Any]] = []

    if dry_run:
        savepoint = db.begin_nested()
        try:
            for index, row in enumerate(rows, start=2):
                try:
                    with db.begin_nested():
                        result = handler(db, row)
                    accepted.append({"row": index, **result})
                except DpmError as exc:
                    rejected.append({"row": index, "code": exc.code, "detail": exc.detail})
                except (ValueError, InvalidOperation, KeyError) as exc:
                    rejected.append({"row": index, "code": "invalid_row", "detail": str(exc)})
        finally:
            savepoint.rollback()
        return {
            "kind": kind,
            "dry_run": True,
            "total": len(rows),
            "accepted": accepted,
            "rejected": rejected,
            "committed": False,
        }

    try:
        for index, row in enumerate(rows, start=2):
            try:
                result = handler(db, row)
                accepted.append({"row": index, **result})
            except DpmError as exc:
                rejected.append({"row": index, "code": exc.code, "detail": exc.detail})
            except (ValueError, InvalidOperation, KeyError) as exc:
                rejected.append({"row": index, "code": "invalid_row", "detail": str(exc)})
        if rejected:
            db.rollback()
            raise ImportValidationError(
                f"{len(rejected)} row(s) rejected. Nothing was imported.",
                kind=kind,
                report={
                    "total": len(rows),
                    "accepted": accepted,
                    "rejected": rejected,
                },
            )
        write_audit(
            db,
            entity_type=f"import_{kind}",
            entity_id=kind,
            action="import",
            payload={"kind": kind, "rows": len(accepted)},
        )
        db.flush()
    except ImportValidationError:
        raise
    except Exception:
        db.rollback()
        raise
    return {
        "kind": kind,
        "dry_run": False,
        "total": len(rows),
        "accepted": accepted,
        "rejected": [],
        "committed": True,
    }
