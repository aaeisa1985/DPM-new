"""Import, Export, Backup, and Health routes (spec 6.4, 7, 8)."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter, Depends, File, Query, Request, UploadFile
from fastapi.responses import FileResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.db import get_db
from app.core.errors import DpmError, ImportValidationError
from app.models.entities import SchemaVersion
from app.services.backup import create_backup, restore_backup
from app.services.csv_io import import_csv

router = APIRouter(tags=["io"])


async def _extract_csv_text(request: Request) -> str:
    content_type = request.headers.get("content-type", "").lower()
    if "multipart/form-data" in content_type:
        form = await request.form()
        upload = form.get("file")
        if upload is not None and hasattr(upload, "read"):
            raw = await upload.read()  # type: ignore[misc]
            return raw.decode("utf-8-sig", errors="replace")
        csv_field = form.get("csv_content")
        if isinstance(csv_field, str) and csv_field:
            return csv_field
    elif "application/json" in content_type:
        data = await request.json()
        if isinstance(data, dict) and data.get("csv_content"):
            return str(data["csv_content"])
    else:
        raw_body = await request.body()
        if raw_body:
            return raw_body.decode("utf-8-sig", errors="replace")
    raise ImportValidationError(
        "CSV content is required (either as json 'csv_content', multipart 'file', or raw text/csv)."
    )


async def _run_import(
    kind: str,
    dry_run: bool,
    request: Request,
    db: Session,
) -> dict[str, Any]:
    text = await _extract_csv_text(request)
    result = import_csv(db, kind=kind, content=text, dry_run=dry_run)
    if not dry_run:
        db.commit()
    return result


@router.post("/import/transactions")
async def import_transactions(
    request: Request,
    dry_run: bool = Query(True, description="Dry run mode (default true)"),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    return await _run_import("transactions", dry_run, request, db)


@router.post("/import/prices")
async def import_prices(
    request: Request,
    dry_run: bool = Query(True, description="Dry run mode (default true)"),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    return await _run_import("prices", dry_run, request, db)


@router.post("/import/fx")
async def import_fx(
    request: Request,
    dry_run: bool = Query(True, description="Dry run mode (default true)"),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    return await _run_import("fx", dry_run, request, db)


@router.post("/import/flows")
async def import_flows(
    request: Request,
    dry_run: bool = Query(True, description="Dry run mode (default true)"),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    return await _run_import("flows", dry_run, request, db)


@router.post("/import/splits")
async def import_splits(
    request: Request,
    dry_run: bool = Query(True, description="Dry run mode (default true)"),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    return await _run_import("splits", dry_run, request, db)


@router.get("/export/backup")
def export_backup(download: bool = Query(False, description="Direct file download")):
    manifest = create_backup()
    zip_path = settings.backup_folder / manifest["zip_filename"]
    if download:
        return FileResponse(
            path=str(zip_path),
            filename=manifest["zip_filename"],
            media_type="application/zip",
            headers={"X-DPM-Checksum": manifest["zip_sha256"]},
        )
    return manifest


@router.post("/import/backup")
async def import_backup(
    request: Request,
    file: UploadFile | None = File(None),
) -> dict[str, Any]:
    if file is not None:
        data = await file.read()
    else:
        data = await request.body()
    if not data:
        raise DpmError("Backup ZIP archive is required in request body or file upload.")
    return restore_backup(data)


@router.get("/health")
def health(db: Session = Depends(get_db)) -> dict[str, Any]:
    row = db.execute(select(SchemaVersion).limit(1)).scalar_one_or_none()
    version = row.version if row else "unknown"
    return {
        "status": "ok",
        "schema_version": version,
        "timestamp": datetime.now(UTC).isoformat(),
    }
