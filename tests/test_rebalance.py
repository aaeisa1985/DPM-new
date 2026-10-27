"""Tests for rebalancing engine (spec 5.5, 9.1 & 9.2)."""

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy.orm import Session

from app.core.errors import InfeasiblePlanError, PlanExpiredError, PlanStaleError
from app.engines.pricing import record_manual_price
from app.engines.rebalance import preview_plan
from app.models.entities import MandateVersion
from app.services.ledger import create_transaction
from app.services.plans import fill_order, persist_plan


def test_rebalance_golden_scenario(test_db: Session, client_env: dict):
    """Spec 9.2:

    Total 100,000, cash target 5%, security target 40% vs actual 50%.
    SELL order calculated exactly, fee included, floored to whole shares.
    """
    db = test_db
    mandate = client_env["mandate"]
    acc = client_env["account"]
    spy = client_env["spy"]
    qqq = client_env["qqq"]

    today = date(2026, 1, 10)
    record_manual_price(db, spy, today, Decimal("500.00"))
    record_manual_price(db, qqq, today, Decimal("100.00"))

    # Mandate weights: CASH: 5%, SPY: 40%, QQQ: 55%
    ver = db.get(MandateVersion, client_env["version"].id)
    ver.weights_json = {"CASH": "0.05", "SPY": "0.40", "QQQ": "0.55"}
    ver.effective_from = date(2026, 1, 1)

    # Account: Opening cash 100,000.
    # Buy 100 SPY @ 500 = 50,000. Cash remaining = 50,000 (50%).
    # Total = 100,000. Actual SPY = 50%, target = 40% (overweight by 10,000).
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

    assert draft.total == Decimal("100000.00")
    assert draft.cash_target == Decimal("5000.00")

    # Order 1: SELL SPY (all sells come first)
    sell_order = draft.orders[0]
    assert sell_order.side == "SELL"
    assert sell_order.symbol == "SPY"
    # Exactly 20 shares (10,000 / 500 = 20)
    assert sell_order.quantity == Decimal("20")
    assert sell_order.price == Decimal("500.00")
    assert sell_order.value == Decimal("10000.00")
    # Fee at 10 bps of 10,000 = 10.00
    assert sell_order.fee == Decimal("10.00")

    # Order 2: BUY QQQ
    buy_order = draft.orders[1]
    assert buy_order.side == "BUY"
    assert buy_order.symbol == "QQQ"

    # Post-trade cash respects target cash >= 5,000
    assert draft.cash_after >= Decimal("5000.00")


def test_infeasible_plan_raised(test_db: Session, client_env: dict):
    """Spec 9.2:

    Actual cash 1%, target cash 50%, no sellable assets -> InfeasiblePlanError.
    """
    db = test_db
    mandate = client_env["mandate"]
    acc = client_env["account"]

    today = date(2026, 1, 10)
    # Mandate target is CASH 50%
    ver = db.get(MandateVersion, client_env["version"].id)
    ver.weights_json = {"CASH": "0.50", "SPY": "0.50"}

    # Cash is only 10, no assets held -> total = 10, target = 5. But wait, if only 10 cash,
    # target cash is 50% of 10 = 5, cash is 10 > 5!
    # To test actual cash < target cash without sellable assets:
    # Say account has 10 cash, but we hold a security that is UNDERWEIGHT (not overweight),
    # or target cash is 80%, and we have 10 cash and 90 of an underweight security (target 90%).
    # Total = 100. Target cash = 80. Current cash = 10. SPY target is 80, current is 90 (overweight).
    # If SPY is locked or has target 100%, and cash target is 20%: total = 110, cash target = 22, current cash = 10,
    # but SPY is underweight (target 100% vs current 90/110 = 81%) -> no SELL orders generated!
    ver.weights_json = {"CASH": "0.20", "SPY": "0.80"}
    acc.opening_cash = Decimal("10.00")
    record_manual_price(db, client_env["spy"], today, Decimal("100.00"))
    create_transaction(
        db,
        account_id=acc.id,
        trade_date=date(2026, 1, 5),
        side="BUY",
        symbol="SPY",
        quantity=Decimal("1"),
        unit_price=Decimal("100.00"),
    )
    db.commit()

    # Total = 10 (cash) + 100 (SPY) = 110. Target cash 20% = 22.
    # Current cash = 10 < 22 target cash.
    # SPY current value = 100 / 110 = 90.9% > 80% target (overweight by 12).
    # But 1 share @ 100 -> selling 1 share would sell 100, leaving 0 SPY.
    # If we want NO overweight assets:
    # Set weights: CASH = 0.50, SPY = 0.50. Total = 110. Target cash = 55.
    # Target SPY = 55. Current SPY = 100. Selling 1 share (the only share) reduces SPY to 0, which is below target!
    # Specifically, when starting cash is below target and no sell orders can satisfy the target:
    ver.weights_json = {"CASH": "0.95", "SPY": "0.05"}
    db.commit()

    # With only 1 share of 100 and 10 cash: total = 110. Target cash = 104.5.
    # Selling 1 share yields 100. Cash after = 110.
    # What if we have only CASH and no shares at all, but cash target is 100%? Total=10, cash=10 -> feasible.
    # What if cash is negative or post-order cash shortfall:
    # Say we have 10 cash, 1 share SPY @ 100. Target cash is 90% (99), but SPY target is 20% (22).
    # To be underweight in equity and have cash below target:
    # That's impossible if weights sum to 100%! Because sum(actual) = 1.0. If cash is underweight, something MUST be overweight!
    # UNLESS: the overweight asset is below DPM_DUST_VALUE!
    # For example: 50 dust overweight (< 100), so no SELL order is generated!
    # Then cash is underweight, but no SELL orders can be generated -> InfeasiblePlanError!
    acc.opening_cash = Decimal("10.00")
    record_manual_price(db, client_env["spy"], today, Decimal("50.00"))
    create_transaction(
        db,
        account_id=acc.id,
        trade_date=date(2026, 1, 5),
        side="BUY",
        symbol="SPY",
        quantity=Decimal("1"),
        unit_price=Decimal("50.00"),
    )
    # Total = 10 + 50 = 60.
    # Target: CASH 50% (30), SPY 50% (30).
    # Current cash = 10 (target 30, deficit 20).
    # Current SPY = 50 (target 30, overweight 20).
    # But overweight value is 20 < DPM_DUST_VALUE (100)!
    # Therefore, the dust sell order is dropped!
    # Since no sell order is generated, post-trade cash remains 10 < 30 target!
    # Result: InfeasiblePlanError!
    ver.weights_json = {"CASH": "0.50", "SPY": "0.50"}
    db.commit()

    with pytest.raises(InfeasiblePlanError) as exc_info:
        preview_plan(db, mandate_id=mandate.id, as_of=today)
    assert "below the" in exc_info.value.detail or "Infeasible" in exc_info.value.detail


def test_order_ordering_sells_before_buys(test_db: Session, client_env: dict):
    db = test_db
    mandate = client_env["mandate"]
    acc = client_env["account"]
    spy = client_env["spy"]
    qqq = client_env["qqq"]

    today = date(2026, 1, 10)
    record_manual_price(db, spy, today, Decimal("100.00"))
    record_manual_price(db, qqq, today, Decimal("100.00"))

    # Initial: 10,000 cash. Buy 80 SPY @ 100 = 8,000. Cash = 2,000.
    # Mandate target: CASH 10%, SPY 20%, QQQ 70%.
    # SPY is overweight (80% vs 20%), QQQ is underweight (0% vs 70%).
    # Must generate SELL SPY first, then BUY QQQ!
    ver = db.get(MandateVersion, client_env["version"].id)
    ver.weights_json = {"CASH": "0.10", "SPY": "0.20", "QQQ": "0.70"}
    acc.opening_cash = Decimal("10000.00")
    create_transaction(
        db,
        account_id=acc.id,
        trade_date=date(2026, 1, 5),
        side="BUY",
        symbol="SPY",
        quantity=Decimal("80"),
        unit_price=Decimal("100.00"),
    )
    db.commit()

    draft = preview_plan(db, mandate_id=mandate.id, as_of=today)

    sides = [o.side for o in draft.orders]
    assert "SELL" in sides and "BUY" in sides
    first_buy_idx = sides.index("BUY")
    last_sell_idx = len(sides) - 1 - sides[::-1].index("SELL")
    # All sells before all buys:
    assert last_sell_idx < first_buy_idx


def test_plan_stale_error_when_book_mutates(test_db: Session, client_env: dict):
    db = test_db
    mandate = client_env["mandate"]
    acc = client_env["account"]
    spy = client_env["spy"]

    today = date(2026, 1, 10)
    record_manual_price(db, spy, today, Decimal("100.00"))

    ver = db.get(MandateVersion, client_env["version"].id)
    ver.weights_json = {"CASH": "0.20", "SPY": "0.80"}
    acc.opening_cash = Decimal("10000.00")
    db.commit()

    draft = preview_plan(db, mandate_id=mandate.id, as_of=today)
    plan = persist_plan(db, draft)
    db.commit()

    # Now a new transaction is recorded on the account AFTER the plan was generated
    create_transaction(
        db,
        account_id=acc.id,
        trade_date=today,
        side="BUY",
        symbol="SPY",
        quantity=Decimal("1"),
        unit_price=Decimal("100.00"),
    )
    db.commit()

    # Attempting to fill an order from the now-stale plan must fail with PlanStaleError
    order = plan.orders[0]
    with pytest.raises(PlanStaleError) as exc_info:
        fill_order(
            db,
            plan_id=plan.id,
            order_id=order.id,
            quantity=Decimal("1"),
            price=Decimal("100.00"),
            fee=Decimal("0"),
        )
    assert "book changed after this plan was generated" in exc_info.value.detail


def test_plan_expired_error(test_db: Session, client_env: dict):
    db = test_db
    mandate = client_env["mandate"]
    acc = client_env["account"]
    spy = client_env["spy"]

    today = date(2026, 1, 10)
    record_manual_price(db, spy, today, Decimal("100.00"))

    ver = db.get(MandateVersion, client_env["version"].id)
    ver.weights_json = {"CASH": "0.20", "SPY": "0.80"}
    acc.opening_cash = Decimal("10000.00")
    db.commit()

    draft = preview_plan(db, mandate_id=mandate.id, as_of=today)
    plan = persist_plan(db, draft)

    # Force expiration date into the past
    plan.expires_at = datetime.now(UTC) - timedelta(minutes=10)
    db.commit()

    order = plan.orders[0]
    with pytest.raises(PlanExpiredError) as exc_info:
        fill_order(
            db,
            plan_id=plan.id,
            order_id=order.id,
            quantity=Decimal("1"),
            price=Decimal("100.00"),
            fee=Decimal("0"),
        )
    assert "Plan expired" in exc_info.value.detail


def test_rebalance_mandate_not_found_and_target_symbol_missing(test_db: Session, client_env: dict):
    from app.core.errors import NotFoundError

    with pytest.raises(NotFoundError):
        preview_plan(test_db, "non-existent-mandate-id", date.today())

    # Target symbol not in securities
    mandate = client_env["mandate"]
    ver = test_db.get(MandateVersion, client_env["version"].id)
    ver.weights_json = {"CASH": "0.10", "UNKNOWN_SYMBOL": "0.90"}
    test_db.commit()

    with pytest.raises(NotFoundError) as exc_info:
        preview_plan(test_db, mandate.id, date.today())
    assert "Target symbol 'UNKNOWN_SYMBOL' is not defined" in exc_info.value.detail


def test_rebalance_max_settlement_days_property(test_db: Session, client_env: dict):
    mandate = client_env["mandate"]
    today = date(2026, 1, 10)
    record_manual_price(test_db, client_env["spy"], today, Decimal("500.00"))
    record_manual_price(test_db, client_env["qqq"], today, Decimal("300.00"))
    test_db.commit()
    draft = preview_plan(test_db, mandate.id, today)
    assert draft.max_settlement_days == 0


def test_rebalance_settlement_trim_and_executed_status_and_versioning(test_db: Session, client_env: dict):
    from app.core.errors import NotFoundError
    from app.engines.rebalance import ensure_plan_executable
    from app.engines.versioning import current_version, effective_version
    from app.models.entities import Mandate

    db = test_db
    mandate = client_env["mandate"]
    acc = client_env["account"]
    spy = client_env["spy"]
    qqq = client_env["qqq"]

    today = date(2026, 1, 10)
    spy.settlement_days = 3
    record_manual_price(db, spy, today, Decimal("100.00"))
    record_manual_price(db, qqq, today, Decimal("100.00"))

    ver = db.get(MandateVersion, client_env["version"].id)
    # Omit CASH key so default weights["CASH"] = 0 branch (line 99) runs
    ver.weights_json = {"SPY": "0.20", "QQQ": "0.80"}
    acc.opening_cash = Decimal("10000.00")
    create_transaction(
        db,
        account_id=acc.id,
        trade_date=date(2026, 1, 5),
        side="BUY",
        symbol="SPY",
        quantity=Decimal("80"),
        unit_price=Decimal("100.00"),
    )
    db.commit()

    # Pass execution_date=today (before SPY's T+3 settlement), so available_cash is only 2,000,
    # forcing trim_buys() to trim the QQQ buy from 8,000 down to <= 2,000 (lines 247-266)!
    draft = preview_plan(db, mandate.id, today, execution_date=today)
    assert draft.available_cash == Decimal("2000.00")
    buy_orders = [o for o in draft.orders if o.side == "BUY"]
    assert len(buy_orders) == 1
    assert buy_orders[0].value + buy_orders[0].fee <= Decimal("2000.00")

    # Test ensure_plan_executable when plan status is already 'executed' (line 392)
    plan = persist_plan(db, draft)
    plan.status = "executed"
    db.commit()
    with pytest.raises(PlanStaleError, match="cannot be executed"):
        ensure_plan_executable(db, plan)

    # Test versioning fallback to earliest version when on_date < effective_from (line 23),
    # and NotFoundError when a mandate has no versions at all (lines 30, 42)
    v_early = effective_version(db, mandate.id, date(1999, 1, 1))
    assert v_early.id == ver.id

    empty_m = Mandate(client_id=client_env["client"].id, name="Empty Mandate")
    db.add(empty_m)
    db.commit()
    with pytest.raises(NotFoundError):
        effective_version(db, empty_m.id, today)
    with pytest.raises(NotFoundError):
        current_version(db, empty_m.id)
