"""Mandate version selection."""

from __future__ import annotations

from datetime import date

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.errors import NotFoundError
from app.models.entities import MandateVersion


def effective_version(db: Session, mandate_id: str, on_date: date) -> MandateVersion:
    row = db.execute(
        select(MandateVersion)
        .where(MandateVersion.mandate_id == mandate_id)
        .where(MandateVersion.effective_from <= on_date)
        .order_by(MandateVersion.version.desc())
        .limit(1)
    ).scalar_one_or_none()
    if row is None:
        row = db.execute(
            select(MandateVersion)
            .where(MandateVersion.mandate_id == mandate_id)
            .order_by(MandateVersion.version.asc())
            .limit(1)
        ).scalar_one_or_none()
    if row is None:
        raise NotFoundError(f"Mandate '{mandate_id}' has no weights version.")
    return row


def current_version(db: Session, mandate_id: str) -> MandateVersion:
    row = db.execute(
        select(MandateVersion)
        .where(MandateVersion.mandate_id == mandate_id)
        .order_by(MandateVersion.version.desc())
        .limit(1)
    ).scalar_one_or_none()
    if row is None:
        raise NotFoundError(f"Mandate '{mandate_id}' has no weights version.")
    return row
