"""Tests for immutable audit log and hash chain (spec 4.14, 8 & 9.1)."""

import pytest
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.core.audit import verify_chain, write_audit


def test_sqlite_trigger_rejects_update_and_delete(test_db: Session):
    db = test_db

    row = write_audit(
        db,
        entity_type="test",
        entity_id="id-1",
        action="create",
        payload={"message": "hello"},
    )
    db.commit()

    # Attempting UPDATE on audit_log directly via SQL must trigger SQLite abort
    with pytest.raises(Exception) as exc_info:
        db.execute(text(f"UPDATE audit_log SET action = 'tampered' WHERE id = {row.id}"))
        db.commit()
    assert "audit_log is append-only" in str(exc_info.value)
    db.rollback()

    # Attempting DELETE on audit_log directly via SQL must trigger SQLite abort
    with pytest.raises(Exception) as exc_info:
        db.execute(text(f"DELETE FROM audit_log WHERE id = {row.id}"))
        db.commit()
    assert "audit_log is append-only" in str(exc_info.value)
    db.rollback()


def test_hash_chain_verification_and_tamper_detection(test_db: Session):
    db = test_db

    # Write 3 valid chained entries
    write_audit(db, entity_type="item", entity_id="1", action="create")
    write_audit(db, entity_type="item", entity_id="2", action="create")
    write_audit(db, entity_type="item", entity_id="3", action="create")
    db.commit()

    # Chain should be valid
    result = verify_chain(db)
    assert result["valid"] is True
    assert result["rows"] >= 3

    # Now simulate a raw low-level database tamper (temporarily dropping trigger to modify row_hash)
    db.execute(text("DROP TRIGGER IF EXISTS audit_log_no_update"))
    db.execute(text("UPDATE audit_log SET row_hash = 'tampered_fake_hash' WHERE id = 2"))
    db.commit()

    # Chain verification must now detect broken chain at row 2!
    broken_result = verify_chain(db)
    assert broken_result["valid"] is False
    assert broken_result["broken_at"] == 2


def test_alembic_migration_creates_triggers_and_schema_version(tmp_path):
    import sqlite3
    from pathlib import Path

    from alembic import command
    from alembic.config import Config

    db_file = tmp_path / "alembic_mig.db"
    cfg = Config(str(Path(__file__).resolve().parent.parent / "alembic.ini"))
    cfg.set_main_option("sqlalchemy.url", f"sqlite:///{db_file.as_posix()}")
    command.upgrade(cfg, "head")

    conn = sqlite3.connect(str(db_file))
    triggers = {
        row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='trigger'").fetchall()
    }
    assert "audit_log_no_update" in triggers
    assert "audit_log_no_delete" in triggers

    version = conn.execute("SELECT version FROM schema_version WHERE id=1").fetchone()
    assert version is not None and version[0] == "1.1.0"
    conn.close()
