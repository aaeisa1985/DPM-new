"""Tests for drift engine (spec 5.4 & 9.1)."""

from datetime import date
from decimal import Decimal

from sqlalchemy.orm import Session

from app.engines.drift import compute_drift
from app.engines.pricing import record_manual_price
from app.models.entities import Security
from app.services.ledger import create_transaction


def test_drift_calculation_and_breach(test_db: Session, client_env: dict):
    db = test_db
    mandate = client_env["mandate"]
    acc = client_env["account"]
    spy = client_env["spy"]
    qqq = client_env["qqq"]

    # Target weights from fixture: CASH: 0.10, SPY: 0.50, QQQ: 0.40, tolerance: 0.05
    today = date(2026, 1, 10)
    record_manual_price(db, spy, today, Decimal("100.00"))
    record_manual_price(db, qqq, today, Decimal("100.00"))

    # Initial cash is 1000.
    # Buy 5 SPY @ 100 -> Cost 500.
    # Buy 2 QQQ @ 100 -> Cost 200.
    # Remaining cash = 300.
    # Total portfolio = 500 (SPY) + 200 (QQQ) + 300 (CASH) = 1000.
    # Actual weights:
    # SPY: 500 / 1000 = 0.50 (target 0.50, drift = 0.00, breached = False)
    # QQQ: 200 / 1000 = 0.20 (target 0.40, drift = -0.20, breached = True because |-0.20| > 0.05)
    # CASH: 300 / 1000 = 0.30 (target 0.10, drift = +0.20, breached = True because |+0.20| > 0.05)
    create_transaction(
        db,
        account_id=acc.id,
        trade_date=today,
        side="BUY",
        symbol="SPY",
        quantity=Decimal("5"),
        unit_price=Decimal("100"),
    )
    create_transaction(
        db,
        account_id=acc.id,
        trade_date=today,
        side="BUY",
        symbol="QQQ",
        quantity=Decimal("2"),
        unit_price=Decimal("100"),
    )
    db.commit()

    drift_res = compute_drift(db, mandate_id=mandate.id, as_of=today)

    assert drift_res.total == Decimal("1000.00")
    by_key = {r.key: r for r in drift_res.rows}

    assert "SPY" in by_key
    assert "QQQ" in by_key
    assert "CASH" in by_key

    assert abs(by_key["SPY"].actual_weight - Decimal("0.50")) < Decimal("0.0001")
    assert not by_key["SPY"].breached

    assert abs(by_key["QQQ"].actual_weight - Decimal("0.20")) < Decimal("0.0001")
    assert by_key["QQQ"].breached is True

    assert abs(by_key["CASH"].actual_weight - Decimal("0.30")) < Decimal("0.0001")
    assert by_key["CASH"].breached is True


def test_unassigned_holding_tagged(test_db: Session, client_env: dict):
    db = test_db
    mandate = client_env["mandate"]
    acc = client_env["account"]

    # Create a 3rd security not in the mandate weights (e.g. GLD)
    gld = Security(
        symbol="GLD",
        name="SPDR Gold Trust",
        currency="USD",
        price_source="manual",
    )
    db.add(gld)
    db.commit()

    today = date(2026, 1, 15)
    record_manual_price(db, gld, today, Decimal("200.00"))
    create_transaction(
        db,
        account_id=acc.id,
        trade_date=today,
        side="BUY",
        symbol="GLD",
        quantity=Decimal("1"),
        unit_price=Decimal("200"),
    )
    db.commit()

    drift_res = compute_drift(db, mandate_id=mandate.id, as_of=today)
    by_key = {r.key: r for r in drift_res.rows}

    assert "GLD" in by_key
    assert by_key["GLD"].unassigned is True
    assert by_key["GLD"].target_weight == Decimal("0")
