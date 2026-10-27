"""Tests for backup and restore service (spec 8 & 9.1)."""

import io
import json
import zipfile
from pathlib import Path

import pytest
from sqlalchemy import select

from app.core.config import settings
from app.core.db import get_session, init_db
from app.core.errors import DpmError
from app.models.entities import Client
from app.services.backup import create_backup, restore_backup


def test_backup_and_restore_cycle(tmp_path: Path):
    db_file = tmp_path / "live.db"
    backup_dir = tmp_path / "backups"
    settings.db_path = str(db_file)
    settings.backup_dir = str(backup_dir)

    init_db(db_file)

    # Insert test data into original DB
    with get_session() as session:
        c = Client(code="CLI-BACKUP", display_name="Backup Client", base_currency="AED")
        session.add(c)
        session.commit()

    # Create backup
    manifest = create_backup(db_path=db_file, backup_dir=backup_dir)
    zip_path = backup_dir / manifest["zip_filename"]
    assert zip_path.exists()
    backup_bytes = zip_path.read_bytes()

    # Create a fresh destination DB and restore into it
    restore_db_file = tmp_path / "restored.db"
    restore_result = restore_backup(backup_bytes, db_path=restore_db_file, backup_dir=backup_dir)

    assert restore_result["restored"] is True
    assert restore_result["manifest"]["filename"] == manifest["filename"]

    # Verify data in restored database
    init_db(restore_db_file)
    with get_session() as session:
        restored_client = session.execute(
            select(Client).where(Client.code == "CLI-BACKUP")
        ).scalar_one_or_none()
        assert restored_client is not None
        assert restored_client.display_name == "Backup Client"
        assert restored_client.base_currency == "AED"


def test_corrupted_checksum_rejected_before_touching_db(tmp_path: Path):
    db_file = tmp_path / "safety.db"
    backup_dir = tmp_path / "backups2"
    init_db(db_file)

    manifest = create_backup(db_path=db_file, backup_dir=backup_dir)
    zip_path = backup_dir / manifest["zip_filename"]

    # Tamper with the checksum in the manifest
    with zipfile.ZipFile(zip_path, "r") as archive:
        db_payload = archive.read(manifest["filename"])
        manifest_data = json.loads(archive.read("manifest.json").decode("utf-8"))

    manifest_data["sha256"] = "0" * 64  # Invalid fake SHA-256

    tampered_buf = io.BytesIO()
    with zipfile.ZipFile(tampered_buf, "w") as archive:
        archive.writestr("manifest.json", json.dumps(manifest_data))
        archive.writestr(manifest["filename"], db_payload)
    tampered_bytes = tampered_buf.getvalue()

    # Attempting to restore tampered archive must fail immediately
    with pytest.raises(DpmError) as exc_info:
        restore_backup(tampered_bytes, db_path=db_file, backup_dir=backup_dir)
    assert "checksum does not match" in exc_info.value.detail
