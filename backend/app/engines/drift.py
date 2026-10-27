"""Target-versus-actual drift (spec 5.4)."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.errors import NotFoundError
from app.core.money import D
from app.engines.accounting import build_book
from app.engines.pricing import value_positions
from app.engines.versioning import effective_version
from app.models.entities import Account, Mandate, Security

TOLERANCE = Decimal("0.00000001")


@dataclass
class DriftRow:
    key: str
    security_id: str | None
    symbol: str
    value: Decimal
    target_weight: Decimal
    actual_weight: Decimal
    drift: Decimal
    breached: bool
    unassigned: bool = False


@dataclass
class DriftResult:
    mandate_id: str
    account_id: str
    as_of: date
    total: Decimal
    cash: Decimal
    tolerance: Decimal
    version: int
    carried_days: int
    rows: list[DriftRow]


def account_for_mandate(db: Session, mandate_id: str) -> Account:
    account = db.execute(select(Account).where(Account.mandate_id == mandate_id)).scalar_one_or_none()
    if account is None:
        raise NotFoundError(f"Mandate '{mandate_id}' has no account.")
    return account


def compute_drift(db: Session, mandate_id: str, as_of: date) -> DriftResult:
    mandate = db.get(Mandate, mandate_id)
    if mandate is None:
        raise NotFoundError(f"Mandate '{mandate_id}' not found.")
    account = account_for_mandate(db, mandate_id)
    version = effective_version(db, mandate_id, as_of)
    book = build_book(db, account.id, as_of)
    valuation = value_positions(db, book.positions, as_of, account.currency)
    total = valuation.total + book.cash

    weights = {str(k).upper(): D(v) for k, v in (version.weights_json or {}).items()}
    securities = {s.id: s for s in db.execute(select(Security)).scalars().all()}
    by_symbol = {s.symbol.upper(): s for s in securities.values()}

    rows: list[DriftRow] = []
    seen: set[str] = set()

    def add_row(
        key: str,
        symbol: str,
        security_id: str | None,
        value: Decimal,
        target: Decimal,
        unassigned: bool,
    ) -> None:
        actual = value / total if total != 0 else Decimal("0")
        drift = actual - target
        rows.append(
            DriftRow(
                key=key,
                security_id=security_id,
                symbol=symbol,
                value=value,
                target_weight=target,
                actual_weight=actual,
                drift=drift,
                breached=abs(drift) > version.drift_tolerance + TOLERANCE,
                unassigned=unassigned,
            )
        )

    for security_id, value in valuation.by_security.items():
        security = securities.get(security_id)
        if security is None:
            continue
        key = security.symbol.upper()
        seen.add(key)
        add_row(key, security.symbol, security.id, value, weights.get(key, Decimal("0")), key not in weights)

    for key, target in weights.items():
        if key == "CASH" or key in seen:
            continue
        security = by_symbol.get(key)
        add_row(
            key,
            security.symbol if security else key,
            security.id if security else None,
            Decimal("0"),
            target,
            False,
        )

    add_row("CASH", "CASH", None, book.cash, weights.get("CASH", Decimal("0")), False)
    rows.sort(key=lambda row: (row.key != "CASH", row.symbol))
    return DriftResult(
        mandate_id=mandate_id,
        account_id=account.id,
        as_of=as_of,
        total=total,
        cash=book.cash,
        tolerance=version.drift_tolerance,
        version=version.version,
        carried_days=valuation.max_carried_days,
        rows=rows,
    )
