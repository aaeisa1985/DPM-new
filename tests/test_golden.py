"""Golden fixtures and reference scenarios calculated by hand (spec 9.2)."""

from datetime import date
from decimal import Decimal

from sqlalchemy.orm import Session

from app.engines.accounting import build_book
from app.engines.performance import compute_performance
from app.engines.pricing import record_manual_price
from app.engines.rebalance import preview_plan
from app.models.entities import MandateVersion
from app.services.ledger import (
    create_dividend,
    create_flow,
    create_transaction,
)


def test_golden_cash_balance_equation(test_db: Session, client_env: dict):
    """Spec 9.2 Cash Balance Reference Scenario:

    - opening_cash: 1000
    - deposit: +500
    - buy 10 shares @ 50, fee 5: -(500 + 5) = -505
    - sell 4 shares @ 55, fee 2: +(220 - 2) = +218
    - dividend: 0.5 per share on 6 remaining shares = +3.00

    Expected cash: 1000 + 500 - 505 + 218 + 3 = 1216.00 exactly.
    """
    db = test_db
    acc = client_env["account"]
    spy = client_env["spy"]

    acc.opening_cash = Decimal("1000.00")
    d1 = date(2026, 1, 1)
    d2 = date(2026, 1, 2)
    d3 = date(2026, 1, 3)
    d4 = date(2026, 1, 4)

    # Deposit 500
    create_flow(db, account_id=acc.id, flow_date=d1, type="deposit", amount=Decimal("500.00"))

    # Buy 10 @ 50, fee 5
    create_transaction(
        db,
        account_id=acc.id,
        trade_date=d2,
        side="BUY",
        symbol="SPY",
        quantity=Decimal("10"),
        unit_price=Decimal("50.00"),
        fee=Decimal("5.00"),
    )

    # Sell 4 @ 55, fee 2
    create_transaction(
        db,
        account_id=acc.id,
        trade_date=d3,
        side="SELL",
        symbol="SPY",
        quantity=Decimal("4"),
        unit_price=Decimal("55.00"),
        fee=Decimal("2.00"),
    )

    # Dividend 0.5 on remaining 6 shares
    create_dividend(
        db,
        account_id=acc.id,
        on_date=d4,
        security_id=spy.id,
        per_unit=Decimal("0.50"),
    )
    db.commit()

    book = build_book(db, acc.id, as_of=d4)

    # Hand-calculated reference balance:
    assert book.cash == Decimal("1216.00")
    assert book.positions[spy.id].quantity == Decimal("6")


def test_golden_twr_hand_calculated(test_db: Session, client_env: dict):
    """Spec 5.3 & 9.2:

    Day 1 close: 100.
    Day 2 deposit: 10 in the morning.
    Day 2 close: 112.2.
    Return = 112.2 / (100 + 10) - 1 = 0.02000000 (tolerance < 1e-8).
    """
    db = test_db
    client = client_env["client"]
    acc = client_env["account"]
    spy = client_env["spy"]

    acc.opening_cash = Decimal("0")
    d1 = date(2026, 1, 1)
    d2 = date(2026, 1, 2)

    create_flow(db, account_id=acc.id, flow_date=d1, type="deposit", amount=Decimal("100.00"))
    create_transaction(
        db,
        account_id=acc.id,
        trade_date=d1,
        side="BUY",
        symbol="SPY",
        quantity=Decimal("1"),
        unit_price=Decimal("100.00"),
    )
    record_manual_price(db, spy, d1, Decimal("100.00"))

    create_flow(db, account_id=acc.id, flow_date=d2, type="deposit", amount=Decimal("10.00"))
    record_manual_price(db, spy, d2, Decimal("102.20"))
    db.commit()

    perf = compute_performance(db, client_id=client.id, start=d1, end=d2)
    assert abs(perf.twr - Decimal("0.02")) < Decimal("1e-8")


def test_golden_rebalance_and_infeasibility(test_db: Session, client_env: dict):
    """Spec 9.2:

    Total 100,000, target cash 5%, target SPY 40% vs actual 50%.
    SELL order of exactly 20 shares @ 500, fee 10 bps.
    Infeasibility when cash is below target without sellable assets.
    """
    db = test_db
    mandate = client_env["mandate"]
    acc = client_env["account"]
    spy = client_env["spy"]
    qqq = client_env["qqq"]

    today = date(2026, 1, 10)
    record_manual_price(db, spy, today, Decimal("500.00"))
    record_manual_price(db, qqq, today, Decimal("100.00"))

    ver = db.get(MandateVersion, client_env["version"].id)
    ver.weights_json = {"CASH": "0.05", "SPY": "0.40", "QQQ": "0.55"}
    acc.opening_cash = Decimal("100000.00")
    create_transaction(
        db,
        account_id=acc.id,
        trade_date=date(2026, 1, 5),
        side="BUY",
        symbol="SPY",
        quantity=Decimal("100"),
        unit_price=Decimal("500.00"),
    )
    db.commit()

    draft = preview_plan(db, mandate_id=mandate.id, as_of=today)
    assert draft.orders[0].side == "SELL"
    assert draft.orders[0].quantity == Decimal("20")
    assert draft.orders[0].fee == Decimal("10.00")


def test_golden_infeasibility_1pct_vs_5pct(test_db: Session, client_env: dict):
    """Spec 9.2:

    Actual cash 1% (1,000), target cash 5% (5,000), and no sell orders available:
    InfeasiblePlanError is guaranteed.
    """
    import pytest

    from app.core.errors import InfeasiblePlanError
    from app.models.entities import Security

    db = test_db
    mandate = client_env["mandate"]
    acc = client_env["account"]

    today = date(2026, 1, 10)
    # High-priced lot where 1 share = 4,500 (> 4,000 overweight), so 0 shares can be sold
    brk = Security(
        symbol="BRK",
        name="High Unit Asset",
        asset_class="Equity",
        currency="USD",
        price_source="manual",
    )
    db.add(brk)
    db.flush()
    record_manual_price(db, brk, today, Decimal("4500.00"))

    ver = db.get(MandateVersion, client_env["version"].id)
    ver.weights_json = {"CASH": "0.05", "BRK": "0.95"}

    # Total = 100,000: 22 shares @ 4,500 = 99,000 (99%), cash = 1,000 (1%).
    # Target cash = 5% (5,000), target BRK = 95% (95,000).
    # Overweight = 4,000 < 1 share (4,500) -> floor_shares(4000 / 4500) = 0 -> no sell orders available!
    acc.opening_cash = Decimal("100000.00")
    create_transaction(
        db,
        account_id=acc.id,
        trade_date=date(2026, 1, 5),
        side="BUY",
        symbol="BRK",
        quantity=Decimal("22"),
        unit_price=Decimal("4500.00"),
    )
    db.commit()

    with pytest.raises(InfeasiblePlanError) as exc_info:
        preview_plan(db, mandate_id=mandate.id, as_of=today)
    assert "below the 5000.00 target" in exc_info.value.detail
