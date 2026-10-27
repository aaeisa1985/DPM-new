"""Backup and restore (VACUUM INTO + SHA-256 manifest)."""

from __future__ import annotations

import hashlib
import io
import json
import os
import sqlite3
import tempfile
import zipfile
from datetime import UTC, datetime
from pathlib import Path

from app.core.config import settings
from app.core.db import SCHEMA_VERSION
from app.core.errors import DpmError

SQLITE_HEADER = b"SQLite format 3\x00"


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _timestamp() -> str:
    return datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")


def create_backup(db_path: Path | None = None, backup_dir: Path | None = None) -> dict:
    db_file = db_path or settings.db_file
    folder = backup_dir or settings.backup_folder
    if not db_file.exists():
        raise DpmError(f"Database '{db_file}' does not exist.")
    folder.mkdir(parents=True, exist_ok=True)
    stamp = _timestamp()
    raw_name = f"dpm-{stamp}.db"
    raw_path = folder / raw_name
    if raw_path.exists():
        raw_path = folder / f"dpm-{stamp}-{os.getpid()}.db"
        raw_name = raw_path.name

    connection = sqlite3.connect(str(db_file))
    try:
        connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        escaped = str(raw_path).replace("'", "''")
        connection.execute(f"VACUUM INTO '{escaped}'")
        connection.commit()
    finally:
        connection.close()

    size = raw_path.stat().st_size
    checksum = sha256_file(raw_path)
    manifest = {
        "created_at": datetime.now(UTC).isoformat(),
        "schema_version": SCHEMA_VERSION,
        "filename": raw_name,
        "sha256": checksum,
        "size": size,
    }
    (folder / f"{raw_path.stem}.manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    zip_name = f"{raw_path.stem}.zip"
    zip_path = folder / zip_name
    with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.write(raw_path, raw_name)
        archive.writestr("manifest.json", json.dumps(manifest, indent=2))
    raw_path.unlink(missing_ok=True)
    manifest["zip_filename"] = zip_name
    manifest["zip_sha256"] = sha256_file(zip_path)
    return manifest


def read_backup_zip(data: bytes) -> tuple[dict, bytes]:
    try:
        archive = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile as exc:
        raise DpmError("Backup is not a valid ZIP archive.", code="backup_invalid") from exc
    with archive:
        names = archive.namelist()
        if "manifest.json" not in names:
            raise DpmError("Backup manifest.json is missing.", code="backup_invalid")
        manifest = json.loads(archive.read("manifest.json").decode("utf-8"))
        filename = manifest.get("filename")
        if not filename or "/" in filename or "\\" in filename or filename.startswith("."):
            raise DpmError("Backup manifest filename is invalid.", code="backup_invalid")
        if filename not in names:
            raise DpmError("Backup database file is missing.", code="backup_invalid")
        payload = archive.read(filename)
    if not payload.startswith(SQLITE_HEADER):
        raise DpmError("Backup file is not a SQLite database.", code="backup_invalid")
    expected = manifest.get("sha256")
    actual = sha256_bytes(payload)
    if expected != actual:
        raise DpmError(
            "Backup checksum does not match. Restore aborted before touching the database.",
            code="backup_checksum",
            expected=expected,
            actual=actual,
        )
    return manifest, payload


def restore_backup(data: bytes, db_path: Path | None = None, backup_dir: Path | None = None) -> dict:
    db_file = db_path or settings.db_file
    folder = backup_dir or settings.backup_folder
    manifest, payload = read_backup_zip(data)

    safety = None
    if db_file.exists():
        try:
            safety = create_backup(db_file, folder)
        except Exception:
            safety = None

    from app.core.db import get_engine

    try:
        get_engine().dispose()
    except Exception:
        pass

    db_file.parent.mkdir(parents=True, exist_ok=True)
    handle, temp_name = tempfile.mkstemp(prefix="dpm-restore-", dir=str(db_file.parent))
    os.close(handle)
    temp_path = Path(temp_name)
    try:
        temp_path.write_bytes(payload)
        os.replace(temp_path, db_file)
    finally:
        temp_path.unlink(missing_ok=True)
        for suffix in ("-wal", "-shm"):
            Path(str(db_file) + suffix).unlink(missing_ok=True)

    # The current engine was disposed; recreate the schema handle quietly.
    from app.core.db import init_db

    init_db(db_file)
    return {
        "restored": True,
        "restart_required": True,
        "manifest": manifest,
        "safety_backup": safety,
    }
