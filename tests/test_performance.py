"""Tests for TWR performance engine (spec 5.3, 9.1 & 9.2)."""

from datetime import date
from decimal import Decimal

import pytest
from sqlalchemy.orm import Session

from app.core.errors import ValuationGapError
from app.engines.performance import compute_performance
from app.engines.pricing import record_manual_price
from app.services.ledger import create_dividend, create_flow, create_transaction


def test_twr_golden_scenario_spec_5_3(test_db: Session, client_env: dict):
    """Spec 5.3:

    - Day 1 close = 100
    - Day 2 morning deposit = 10 (flow_date = Day 2)
    - Day 2 close = 112.2
    - Day 2 return = 112.2 / (100 + 10) - 1 = 2% (0.02 exactly)
    """
    db = test_db
    client = client_env["client"]
    acc = client_env["account"]
    spy = client_env["spy"]

    # Clear opening cash to match pure scenario
    acc.opening_cash = Decimal("0")
    db.commit()

    day1 = date(2026, 1, 1)
    day2 = date(2026, 1, 2)

    # Day 1: Deposit 100, buy 1 SPY @ 100
    create_flow(db, account_id=acc.id, flow_date=day1, type="deposit", amount=Decimal("100"))
    create_transaction(
        db,
        account_id=acc.id,
        trade_date=day1,
        side="BUY",
        symbol="SPY",
        quantity=Decimal("1"),
        unit_price=Decimal("100"),
    )
    record_manual_price(db, spy, day1, Decimal("100.00"))

    # Day 2: Morning deposit of 10. SPY closes at 102.20
    # Portfolio at Day 2 close: 1 SPY @ 102.20 + 10 cash = 112.20
    create_flow(db, account_id=acc.id, flow_date=day2, type="deposit", amount=Decimal("10"))
    record_manual_price(db, spy, day2, Decimal("102.20"))
    db.commit()

    perf = compute_performance(db, client_id=client.id, start=day1, end=day2)

    assert len(perf.points) == 2
    pt1 = perf.points[0]
    pt2 = perf.points[1]

    # Day 1
    assert pt1.day == day1
    assert pt1.close_value == Decimal("100.00")
    assert pt1.external_flow == Decimal("100.00")
    assert pt1.daily_return == Decimal("0")

    # Day 2
    assert pt2.day == day2
    assert pt2.start_value == Decimal("110.00")  # 100 (prev close) + 10 (Day 2 flow)
    assert pt2.close_value == Decimal("112.20")
    assert pt2.external_flow == Decimal("10.00")

    # Day 2 return: exactly 0.02 (2%)
    assert abs(pt2.daily_return - Decimal("0.02")) < Decimal("1e-8")
    assert abs(perf.twr - Decimal("0.02")) < Decimal("1e-8")


def test_internal_dividend_does_not_break_external_flow_window(test_db: Session, client_env: dict):
    db = test_db
    client = client_env["client"]
    acc = client_env["account"]
    spy = client_env["spy"]

    acc.opening_cash = Decimal("0")
    db.commit()

    d1 = date(2026, 1, 1)
    d2 = date(2026, 1, 2)

    create_flow(db, account_id=acc.id, flow_date=d1, type="deposit", amount=Decimal("1000"))
    create_transaction(
        db,
        account_id=acc.id,
        trade_date=d1,
        side="BUY",
        symbol="SPY",
        quantity=Decimal("10"),
        unit_price=Decimal("100"),
    )
    record_manual_price(db, spy, d1, Decimal("100.00"))

    # Day 2: Dividend of 5.00 per share (50 total cash added to book). SPY close drops to 95.00
    # Total close value = 10 * 95 + 50 = 1000.
    # Dividend is INTERNAL cash flow: external_flow must be 0!
    create_dividend(db, account_id=acc.id, on_date=d2, security_id=spy.id, per_unit=Decimal("5.00"))
    record_manual_price(db, spy, d2, Decimal("95.00"))
    db.commit()

    perf = compute_performance(db, client_id=client.id, start=d1, end=d2)
    pt2 = perf.points[1]

    # External flow on Day 2 is 0
    assert pt2.external_flow == Decimal("0")
    assert pt2.start_value == Decimal("1000.00")
    assert pt2.close_value == Decimal("1000.00")
    assert pt2.daily_return == Decimal("0")


def test_price_gap_triggers_valuation_gap_error(test_db: Session, client_env: dict):
    db = test_db
    client = client_env["client"]
    acc = client_env["account"]
    spy = client_env["spy"]

    d1 = date(2026, 1, 1)
    d15 = date(2026, 1, 15)

    create_flow(db, account_id=acc.id, flow_date=d1, type="deposit", amount=Decimal("100"))
    create_transaction(
        db,
        account_id=acc.id,
        trade_date=d1,
        side="BUY",
        symbol="SPY",
        quantity=Decimal("1"),
        unit_price=Decimal("100"),
    )
    # Price recorded on d1 only. Gap between Jan 1 and Jan 15 is 14 days > carry_forward_max (5).
    record_manual_price(db, spy, d1, Decimal("100.00"))
    db.commit()

    with pytest.raises(ValuationGapError) as exc_info:
        compute_performance(db, client_id=client.id, start=d1, end=d15)
    assert "Price gap" in exc_info.value.detail


def test_performance_with_benchmark(test_db: Session, client_env: dict):
    db = test_db
    client = client_env["client"]
    acc = client_env["account"]
    spy = client_env["spy"]
    qqq = client_env["qqq"]

    d1 = date(2026, 1, 1)
    d2 = date(2026, 1, 2)

    create_flow(db, account_id=acc.id, flow_date=d1, type="deposit", amount=Decimal("100"))
    create_transaction(
        db,
        account_id=acc.id,
        trade_date=d1,
        side="BUY",
        symbol="SPY",
        quantity=Decimal("1"),
        unit_price=Decimal("100"),
    )
    record_manual_price(db, spy, d1, Decimal("100.00"))
    record_manual_price(db, spy, d2, Decimal("105.00"))

    # QQQ as benchmark
    record_manual_price(db, qqq, d1, Decimal("300.00"))
    record_manual_price(db, qqq, d2, Decimal("306.00"))
    db.commit()

    perf = compute_performance(db, client_id=client.id, start=d1, end=d2, benchmark=qqq)
    assert perf.benchmark is not None
    assert perf.benchmark["symbol"] == "QQQ"
    assert len(perf.benchmark["points"]) == 2


def test_performance_start_after_end_error(test_db: Session, client_env: dict):
    from app.core.errors import NotFoundError

    with pytest.raises(NotFoundError):
        compute_performance(
            test_db,
            client_id=client_env["client"].id,
            start=date(2026, 2, 1),
            end=date(2026, 1, 1),
        )


def test_performance_fx_book_and_splits_and_sells_and_drawdown(test_db: Session, client_env: dict):
    from app.core.errors import FxRateMissingError, NotFoundError, OverSellError
    from app.engines.performance import FxBook, load_price_series
    from app.engines.pricing import record_fx
    from app.models.entities import Client, CorporateAction, MandateVersion

    db = test_db
    client = client_env["client"]
    acc = client_env["account"]
    spy = client_env["spy"]
    qqq = client_env["qqq"]

    d1 = date(2026, 1, 1)
    d2 = date(2026, 1, 2)
    d3 = date(2026, 1, 3)

    # 1. Test FxBook direct, inverse, triangulation, cache, and missing rate
    record_fx(db, "AEDUSD", d1, Decimal("0.272294"))
    record_fx(db, "USDEUR", d1, Decimal("0.92"))
    record_fx(db, "GBPUSD", d2, Decimal("1.25"))
    db.commit()

    fx = FxBook(db, d3)
    assert fx.convert(Decimal("100"), "USD", "USD", d1) == Decimal("100")
    assert abs(fx.convert(Decimal("100"), "AED", "USD", d1) - Decimal("27.2294")) < Decimal("0.0001")
    # Cache hit on second call
    assert abs(fx.convert(Decimal("100"), "AED", "USD", d1) - Decimal("27.2294")) < Decimal("0.0001")
    # Inverse
    assert abs(fx.convert(Decimal("27.2294"), "USD", "AED", d1) - Decimal("100")) < Decimal("0.001")
    # Triangulation AED -> EUR (uses AEDUSD and USDEUR)
    assert fx.convert(Decimal("100"), "AED", "EUR", d1) > 0
    # Triangulation EUR -> GBP on d2 (uses 1/USDEUR and 1/GBPUSD)
    assert fx.convert(Decimal("100"), "EUR", "GBP", d2) > 0
    # Missing before d1 or unknown pair
    assert fx.rate("GBPUSD", d1) is None
    with pytest.raises(FxRateMissingError):
        fx.convert(Decimal("100"), "JPY", "CAD", d1)

    # 2. load_price_series with empty list
    assert load_price_series(db, [], d3) == {}

    # 3. Full performance replay with split, FIFO/HIFO sells, multiple mandate versions, and drawdown
    ver2 = MandateVersion(
        mandate_id=client_env["mandate"].id,
        version=2,
        effective_from=d3,
        drift_tolerance=Decimal("0.05"),
        lot_method="hifo",
        weights_json={"CASH": "0.10", "SPY": "0.50", "QQQ": "0.40"},
    )
    db.add(ver2)

    create_transaction(
        db,
        account_id=acc.id,
        trade_date=d1,
        side="BUY",
        symbol="SPY",
        quantity=Decimal("10"),
        unit_price=Decimal("100"),
    )
    # 2-for-1 split on d2 (now 20 shares @ 50)
    db.add(
        CorporateAction(
            security_id=spy.id,
            action_date=d2,
            type="split",
            ratio=Decimal("2"),
            idempotency_key="perf-split-spy",
        )
    )
    # Sell 5 shares on d2 (FIFO from version 1) and 5 shares on d3 (HIFO from version 2)
    create_transaction(
        db,
        account_id=acc.id,
        trade_date=d2,
        side="SELL",
        symbol="SPY",
        quantity=Decimal("5"),
        unit_price=Decimal("55"),
    )
    create_transaction(
        db,
        account_id=acc.id,
        trade_date=d3,
        side="SELL",
        symbol="SPY",
        quantity=Decimal("5"),
        unit_price=Decimal("45"),
    )

    record_manual_price(db, spy, d1, Decimal("100"))
    record_manual_price(db, spy, d2, Decimal("55"))
    record_manual_price(db, spy, d3, Decimal("40"))  # Drop triggers negative drawdown!

    # Benchmark QQQ with a split on d2
    record_manual_price(db, qqq, d1, Decimal("200"))
    record_manual_price(db, qqq, d2, Decimal("102"))
    record_manual_price(db, qqq, d3, Decimal("104"))
    db.add(
        CorporateAction(
            security_id=qqq.id,
            action_date=d2,
            type="split",
            ratio=Decimal("2"),
            idempotency_key="perf-split-qqq",
        )
    )
    db.commit()

    # Run with start=None (auto-detects d1)
    perf = compute_performance(db, client_id=client.id, end=d3, start=None, benchmark=qqq)
    assert perf.start == d1
    assert perf.max_drawdown < Decimal("0")
    assert perf.benchmark is not None

    # 4. Error branches: non-existent client or client with no accounts
    with pytest.raises(NotFoundError):
        compute_performance(db, client_id="missing-client", end=d3)

    c_no_acc = Client(code="NO-ACC", display_name="No Account", base_currency="USD")
    db.add(c_no_acc)
    db.commit()
    with pytest.raises(NotFoundError):
        compute_performance(db, client_id=c_no_acc.id, end=d3)

    # 5. Oversell during performance replay (if a buy was voided)
    from app.models.entities import Transaction

    db.add(
        Transaction(
            account_id=acc.id,
            trade_date=d3,
            sequence=99,
            side="SELL",
            security_id=spy.id,
            quantity=Decimal("500"),
            unit_price=Decimal("40"),
            fee=Decimal("0"),
            fx_rate=Decimal("1"),
            status="active",
            idempotency_key="perf-oversell-tx",
        )
    )
    db.commit()
    with pytest.raises(OverSellError):
        compute_performance(db, client_id=client.id, start=d1, end=d3)
