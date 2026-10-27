"""Audit log routes with hash chain verification (spec 4.14, 8, 9)."""

from __future__ import annotations

import json
from typing import Any

from fastapi import APIRouter, Depends, Query
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.audit import verify_chain
from app.core.db import get_db
from app.models.entities import AuditLog

router = APIRouter(tags=["audit"])


@router.get("/audit")
def get_audit_log(
    limit: int = Query(100, ge=1, le=1000),
    offset: int = Query(0, ge=0),
    entity_type: str | None = Query(None, description="Filter by entity type"),
    db: Session = Depends(get_db),
) -> list[dict[str, Any]]:
    query = select(AuditLog).order_by(AuditLog.id.desc())
    if entity_type:
        query = query.where(AuditLog.entity_type == entity_type)
    query = query.offset(offset).limit(limit)
    rows = db.execute(query).scalars().all()

    result = []
    for r in rows:
        try:
            payload = json.loads(r.payload_json)
        except Exception:
            payload = r.payload_json
        result.append(
            {
                "id": r.id,
                "at": r.at.isoformat(),
                "actor": r.actor,
                "entity_type": r.entity_type,
                "entity_id": r.entity_id,
                "action": r.action,
                "payload": payload,
                "prev_hash": r.prev_hash,
                "row_hash": r.row_hash,
            }
        )
    return result


@router.get("/audit/verify")
def verify_audit_chain(db: Session = Depends(get_db)) -> dict[str, Any]:
    return verify_chain(db)
