"""Immutable, hash-chained audit log (D10)."""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.entities import AuditLog

GENESIS = "0" * 64


def _canonical(payload: dict[str, Any]) -> str:
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)


def _row_payload(
    at: datetime, actor: str, entity_type: str, entity_id: str, action: str, payload_json: str
) -> dict[str, Any]:
    if at.tzinfo is None:
        at_utc = at.replace(tzinfo=UTC)
    else:
        at_utc = at.astimezone(UTC)
    return {
        "at": at_utc.isoformat(),
        "actor": actor,
        "entity_type": entity_type,
        "entity_id": entity_id,
        "action": action,
        "payload": payload_json,
    }


def write_audit(
    db: Session,
    *,
    entity_type: str,
    entity_id: str,
    action: str,
    payload: dict[str, Any] | None = None,
    actor: str = "local-operator",
) -> AuditLog:
    last = db.execute(select(AuditLog).order_by(AuditLog.id.desc()).limit(1)).scalar_one_or_none()
    prev_hash = last.row_hash if last else GENESIS
    at = datetime.now(UTC)
    payload_json = _canonical(payload or {})
    canonical = _canonical(_row_payload(at, actor, entity_type, entity_id, action, payload_json))
    row_hash = hashlib.sha256((prev_hash + "|" + canonical).encode("utf-8")).hexdigest()
    row = AuditLog(
        at=at,
        actor=actor,
        entity_type=entity_type,
        entity_id=entity_id,
        action=action,
        payload_json=payload_json,
        prev_hash=prev_hash,
        row_hash=row_hash,
    )
    db.add(row)
    db.flush()
    return row


def verify_chain(db: Session) -> dict[str, Any]:
    rows = db.execute(select(AuditLog).order_by(AuditLog.id.asc())).scalars().all()
    prev = GENESIS
    for row in rows:
        expected_prev = prev
        if row.prev_hash != expected_prev:
            return {"valid": False, "broken_at": row.id, "reason": "prev_hash mismatch"}
        canonical = _canonical(
            _row_payload(row.at, row.actor, row.entity_type, row.entity_id, row.action, row.payload_json)
        )
        expected = hashlib.sha256((row.prev_hash + "|" + canonical).encode("utf-8")).hexdigest()
        if row.row_hash != expected:
            return {"valid": False, "broken_at": row.id, "reason": "row_hash mismatch"}
        prev = row.row_hash
    return {"valid": True, "rows": len(rows), "head": prev}
