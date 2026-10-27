"""Tests for accounting engine (spec 5.1 & 9.1)."""

from datetime import date
from decimal import Decimal

import pytest
from sqlalchemy.orm import Session

from app.core.errors import OverSellError
from app.engines.accounting import build_book
from app.models.entities import CorporateAction, MandateVersion
from app.services.ledger import create_transaction, void_transaction


def test_buy_and_partial_sell(test_db: Session, client_env: dict):
    db = test_db
    acc = client_env["account"]
    spy = client_env["spy"]

    # Opening cash = 1000. Buy 10 SPY @ 50, fee 5.
    # Cost = 10 * 50 + 5 = 505. Unit cost = 50 + 5/10 = 50.5. Remaining cash = 495.
    tx1 = create_transaction(
        db,
        account_id=acc.id,
        trade_date=date(2026, 1, 10),
        side="BUY",
        symbol="SPY",
        quantity=Decimal("10"),
        unit_price=Decimal("50"),
        fee=Decimal("5"),
    )
    db.commit()

    book = build_book(db, acc.id, date(2026, 1, 10))
    assert book.cash == Decimal("495.00")
    assert "SPY" in [p.symbol for p in book.positions.values()]
    spy_pos = book.positions[spy.id]
    assert spy_pos.quantity == Decimal("10")
    assert spy_pos.avg_cost == Decimal("50.5")

    # Sell 4 SPY @ 60, fee 2. Cash proceeds = 4 * 60 - 2 = 238.
    # New cash = 495 + 238 = 733.
    # Realized = (60 - 50.5) * 4 - 2 = 38 - 2 = 36.
    tx2 = create_transaction(
        db,
        account_id=acc.id,
        trade_date=date(2026, 1, 15),
        side="SELL",
        symbol="SPY",
        quantity=Decimal("4"),
        unit_price=Decimal("60"),
        fee=Decimal("2"),
    )
    db.commit()

    book2 = build_book(db, acc.id, date(2026, 1, 15))
    assert book2.cash == Decimal("733.00")
    assert book2.positions[spy.id].quantity == Decimal("6")
    assert book2.realized == Decimal("36.00")


def test_oversell_failure(test_db: Session, client_env: dict):
    db = test_db
    acc = client_env["account"]

    create_transaction(
        db,
        account_id=acc.id,
        trade_date=date(2026, 1, 10),
        side="BUY",
        symbol="SPY",
        quantity=Decimal("5"),
        unit_price=Decimal("100"),
    )
    db.commit()

    initial_book = build_book(db, acc.id, date(2026, 1, 10))
    initial_cash = initial_book.cash

    # Selling 10 when only 5 are held must raise OverSellError
    with pytest.raises(OverSellError):
        create_transaction(
            db,
            account_id=acc.id,
            trade_date=date(2026, 1, 12),
            side="SELL",
            symbol="SPY",
            quantity=Decimal("10"),
            unit_price=Decimal("105"),
        )
    db.rollback()

    book_after = build_book(db, acc.id, date(2026, 1, 12))
    assert book_after.cash == initial_cash
    assert book_after.positions[client_env["spy"].id].quantity == Decimal("5")


def test_void_buy_causing_later_oversell(test_db: Session, client_env: dict):
    db = test_db
    acc = client_env["account"]
    spy = client_env["spy"]

    tx_buy = create_transaction(
        db,
        account_id=acc.id,
        trade_date=date(2026, 1, 10),
        side="BUY",
        symbol="SPY",
        quantity=Decimal("10"),
        unit_price=Decimal("50"),
    )
    tx_sell = create_transaction(
        db,
        account_id=acc.id,
        trade_date=date(2026, 1, 12),
        side="SELL",
        symbol="SPY",
        quantity=Decimal("8"),
        unit_price=Decimal("55"),
    )
    db.commit()

    # Both active -> book builds cleanly
    book = build_book(db, acc.id, date(2026, 1, 15))
    assert book.positions[spy.id].quantity == Decimal("2")

    # Void the buy transaction -> sell becomes oversold on subsequent build_book
    void_transaction(db, transaction_id=tx_buy.id, reason="Broker correction")
    db.commit()

    with pytest.raises(OverSellError) as exc_info:
        build_book(db, acc.id, date(2026, 1, 15))
    assert "exceeds available" in exc_info.value.detail


def test_stock_split_retroactivity(test_db: Session, client_env: dict):
    db = test_db
    acc = client_env["account"]
    spy = client_env["spy"]

    create_transaction(
        db,
        account_id=acc.id,
        trade_date=date(2026, 1, 10),
        side="BUY",
        symbol="SPY",
        quantity=Decimal("10"),
        unit_price=Decimal("100"),
    )
    # 2-for-1 split on Jan 15
    action = CorporateAction(
        security_id=spy.id,
        action_date=date(2026, 1, 15),
        type="split",
        ratio=Decimal("2"),
        idempotency_key="split-spy-2026-01-15",
    )
    db.add(action)
    db.commit()

    # Pre-split valuation
    book_pre = build_book(db, acc.id, date(2026, 1, 14))
    assert book_pre.positions[spy.id].quantity == Decimal("10")
    assert book_pre.positions[spy.id].avg_cost == Decimal("100")

    # Post-split valuation: quantity doubled, unit cost halved
    book_post = build_book(db, acc.id, date(2026, 1, 16))
    assert book_post.positions[spy.id].quantity == Decimal("20")
    assert book_post.positions[spy.id].avg_cost == Decimal("50")


def test_intraday_sequence_ordering(test_db: Session, client_env: dict):
    db = test_db
    acc = client_env["account"]
    spy = client_env["spy"]
    same_day = date(2026, 2, 1)

    # Buy with seq 1
    create_transaction(
        db,
        account_id=acc.id,
        trade_date=same_day,
        sequence=1,
        side="BUY",
        symbol="SPY",
        quantity=Decimal("10"),
        unit_price=Decimal("100"),
    )
    # Sell with seq 2
    create_transaction(
        db,
        account_id=acc.id,
        trade_date=same_day,
        sequence=2,
        side="SELL",
        symbol="SPY",
        quantity=Decimal("10"),
        unit_price=Decimal("110"),
    )
    db.commit()

    book = build_book(db, acc.id, same_day)
    assert spy.id not in book.positions
    assert book.realized == Decimal("100")


def test_hifo_lot_consumption(test_db: Session, client_env: dict):
    db = test_db
    acc = client_env["account"]
    spy = client_env["spy"]
    mandate = client_env["mandate"]

    # Set lot_method = hifo on mandate version
    ver = db.get(MandateVersion, client_env["version"].id)
    ver.lot_method = "hifo"
    db.commit()

    # Lot 1: 10 @ 50 (acquired Jan 1)
    create_transaction(
        db,
        account_id=acc.id,
        trade_date=date(2026, 1, 1),
        side="BUY",
        symbol="SPY",
        quantity=Decimal("10"),
        unit_price=Decimal("50"),
    )
    # Lot 2: 10 @ 80 (acquired Jan 5) - more expensive
    create_transaction(
        db,
        account_id=acc.id,
        trade_date=date(2026, 1, 5),
        side="BUY",
        symbol="SPY",
        quantity=Decimal("10"),
        unit_price=Decimal("80"),
    )
    # Sell 10 @ 90 on Jan 10
    # HIFO should consume the 80 cost lot first! Realized = (90 - 80) * 10 = 100.
    create_transaction(
        db,
        account_id=acc.id,
        trade_date=date(2026, 1, 10),
        side="SELL",
        symbol="SPY",
        quantity=Decimal("10"),
        unit_price=Decimal("90"),
    )
    db.commit()

    book = build_book(db, acc.id, date(2026, 1, 10))
    assert book.realized == Decimal("100")
    pos = book.positions[spy.id]
    assert pos.quantity == Decimal("10")
    assert pos.avg_cost == Decimal("50")  # The 50-cost lot remains


def test_accounting_account_not_found(test_db: Session):
    from app.core.errors import NotFoundError

    with pytest.raises(NotFoundError):
        build_book(test_db, "non-existent-account-id")


def test_accounting_dust_cleanup(test_db: Session, client_env: dict):
    db = test_db
    acc = client_env["account"]
    spy = client_env["spy"]

    # Buy 1 share
    create_transaction(
        db,
        account_id=acc.id,
        trade_date=date(2026, 1, 1),
        side="BUY",
        symbol="SPY",
        quantity=Decimal("1"),
        unit_price=Decimal("100"),
    )
    # Sell 0.999999995 shares (leaving 0.000000005 < lot_epsilon 1e-8)
    create_transaction(
        db,
        account_id=acc.id,
        trade_date=date(2026, 1, 2),
        side="SELL",
        symbol="SPY",
        quantity=Decimal("0.999999995"),
        unit_price=Decimal("100"),
    )
    db.commit()

    book = build_book(db, acc.id, date(2026, 1, 3))
    # Remaining tiny dust position should be dropped
    assert spy.id not in book.positions


def test_accounting_split_before_subsequent_trade_and_helpers(test_db: Session, client_env: dict):
    from app.core.config import settings
    from app.engines.accounting import Lot, _apply_splits, _consume
    from app.models.entities import Transaction

    db = test_db
    acc = client_env["account"]
    spy = client_env["spy"]

    # Buy on Jan 5, Split on Jan 10, Sell on Jan 15 (exercises split loop inside tx loop lines 147-152)
    create_transaction(
        db,
        account_id=acc.id,
        trade_date=date(2026, 1, 5),
        side="BUY",
        symbol="SPY",
        quantity=Decimal("10"),
        unit_price=Decimal("100"),
    )
    db.add(
        CorporateAction(
            security_id=spy.id,
            action_date=date(2026, 1, 10),
            type="split",
            ratio=Decimal("2"),
            idempotency_key="split-mid-tx",
        )
    )
    create_transaction(
        db,
        account_id=acc.id,
        trade_date=date(2026, 1, 15),
        side="SELL",
        symbol="SPY",
        quantity=Decimal("5"),
        unit_price=Decimal("60"),
    )
    db.commit()

    book = build_book(db, acc.id, date(2026, 1, 20))
    assert book.positions[spy.id].quantity == Decimal("15")

    # Test _apply_splits helper directly
    lot = Lot(
        transaction_id="t1", quantity=Decimal("10"), unit_cost=Decimal("100"), acquired=date(2026, 1, 1)
    )
    splits = [
        CorporateAction(
            security_id=spy.id,
            action_date=date(2026, 1, 5),
            type="split",
            ratio=Decimal("2"),
            idempotency_key="s1",
        ),
        CorporateAction(
            security_id=spy.id,
            action_date=date(2026, 2, 1),
            type="split",
            ratio=Decimal("3"),
            idempotency_key="s2",
        ),
    ]
    _apply_splits({spy.id: [lot]}, splits, date(2026, 1, 10))
    assert lot.quantity == Decimal("20")

    # Test _consume zero-lot skip and oversell error directly
    zero_lot = Lot(
        transaction_id="t0", quantity=Decimal("0"), unit_cost=Decimal("50"), acquired=date(2026, 1, 1)
    )
    with pytest.raises(OverSellError):
        _consume([zero_lot, lot], Decimal("25"), "fifo")

    # Test dust threshold between E8 and lot_epsilon (lines 209-210)
    old_eps = settings.lot_epsilon
    settings.lot_epsilon = Decimal("1.0")
    try:
        create_transaction(
            db,
            account_id=acc.id,
            trade_date=date(2026, 1, 21),
            side="SELL",
            symbol="SPY",
            quantity=Decimal("14.5"),
            unit_price=Decimal("60"),
        )
        db.commit()
        book_eps = build_book(db, acc.id, date(2026, 1, 22))
        assert spy.id not in book_eps.positions
    finally:
        settings.lot_epsilon = old_eps

    # Test invalid transaction side raising ValueError (line 191)
    bad_tx = Transaction(
        account_id=acc.id,
        trade_date=date(2026, 1, 25),
        sequence=1,
        side="HOLD",
        security_id=spy.id,
        quantity=Decimal("1"),
        unit_price=Decimal("50"),
        fee=Decimal("0"),
        fx_rate=Decimal("1"),
        status="active",
        idempotency_key="bad-side-idem",
    )
    db.add(bad_tx)
    db.commit()
    with pytest.raises(ValueError, match="Unknown transaction side"):
        build_book(db, acc.id, date(2026, 1, 26))
