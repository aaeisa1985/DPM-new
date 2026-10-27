"""Tests for CSV import/export service (spec 7 & 9.1)."""

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.errors import ImportValidationError
from app.models.entities import Transaction
from app.services.csv_io import import_csv


def test_dry_run_writes_nothing(test_db: Session, client_env: dict):
    db = test_db
    csv_text = (
        "date,account_code,external_ref,side,symbol,quantity,unit_price,currency,fee,fx_rate,sequence\n"
        "2026-01-10,ACC-001,REF-1,BUY,SPY,10,100,USD,0,1,1\n"
    )

    result = import_csv(db, "transactions", csv_text, dry_run=True)

    assert result["dry_run"] is True
    assert result["committed"] is False
    assert len(result["accepted"]) == 1
    assert len(result["rejected"]) == 0

    # Ensure nothing was committed to DB
    tx_count = db.execute(select(Transaction)).scalars().all()
    assert len(tx_count) == 0


def test_real_import_commits_data(test_db: Session, client_env: dict):
    db = test_db
    csv_text = (
        "date,account_code,external_ref,side,symbol,quantity,unit_price,currency,fee,fx_rate,sequence\n"
        "2026-01-10,ACC-001,REF-1,BUY,SPY,10,100,USD,0,1,1\n"
    )

    result = import_csv(db, "transactions", csv_text, dry_run=False)
    db.commit()

    assert result["dry_run"] is False
    assert result["committed"] is True
    assert len(result["accepted"]) == 1

    tx_count = db.execute(select(Transaction)).scalars().all()
    assert len(tx_count) == 1
    assert tx_count[0].external_ref == "REF-1"


def test_bom_stripped_cleanly(test_db: Session, client_env: dict):
    db = test_db
    # Excel UTF-8 with BOM (\ufeff)
    bom_csv = (
        "\ufeffdate,account_code,external_ref,side,symbol,quantity,unit_price,currency,fee,fx_rate,sequence\n"
        "2026-01-10,ACC-001,REF-BOM,BUY,SPY,5,100,USD,0,1,1\n"
    )

    result = import_csv(db, "transactions", bom_csv, dry_run=True)
    assert result["total"] == 1
    assert len(result["accepted"]) == 1
    assert len(result["rejected"]) == 0


def test_all_or_nothing_abort_on_rejected_row(test_db: Session, client_env: dict):
    db = test_db
    # Row 2 is valid, Row 3 has unknown symbol XYZ
    csv_text = (
        "date,account_code,external_ref,side,symbol,quantity,unit_price,currency,fee,fx_rate,sequence\n"
        "2026-01-10,ACC-001,REF-1,BUY,SPY,10,100,USD,0,1,1\n"
        "2026-01-10,ACC-001,REF-2,BUY,XYZ,10,100,USD,0,1,2\n"
    )

    with pytest.raises(ImportValidationError) as exc_info:
        import_csv(db, "transactions", csv_text, dry_run=False)

    report = exc_info.value.extra.get("report", {})
    assert len(report.get("rejected", [])) == 1
    rej = report["rejected"][0]
    assert rej["row"] == 3  # Header is row 1, first data row is 2, second is 3!

    # Verify that Row 2 was NOT committed (all-or-nothing rollback)
    tx_count = db.execute(select(Transaction)).scalars().all()
    assert len(tx_count) == 0
