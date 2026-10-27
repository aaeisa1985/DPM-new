"""Tests for pricing and FX engine (spec 5.2 & 9.1)."""

from datetime import date
from decimal import Decimal

import pytest
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.errors import FxRateMissingError, ValuationGapError
from app.engines.pricing import convert_amount, get_price, record_fx, record_manual_price
from app.models.entities import PriceHistory


def test_carry_forward_and_gap_error(test_db: Session, client_env: dict):
    db = test_db
    spy = client_env["spy"]

    # Record price on Jan 1
    record_manual_price(db, spy, date(2026, 1, 1), Decimal("500.00"))
    db.commit()

    # Jan 3 is 2 days later <= carry_forward_max (5 days) -> returns price and carried_days=2
    price, carried = get_price(db, spy, date(2026, 1, 3))
    assert price == Decimal("500.00")
    assert carried == 2

    # Jan 10 is 9 days later > 5 days -> ValuationGapError
    with pytest.raises(ValuationGapError) as exc_info:
        get_price(db, spy, date(2026, 1, 10))
    assert "Price gap" in exc_info.value.detail
    assert exc_info.value.extra.get("carried_days") == 9


def test_manual_price_priority_over_other_sources(test_db: Session, client_env: dict):
    db = test_db
    spy = client_env["spy"]
    same_date = date(2026, 2, 1)

    # Insert a yahoo price row first
    p_yahoo = PriceHistory(
        security_id=spy.id,
        price_date=same_date,
        close=Decimal("510.00"),
        source="yahoo",
    )
    db.add(p_yahoo)
    db.commit()

    # Now record a manual price on the same date: it must take precedence!
    record_manual_price(db, spy, same_date, Decimal("515.00"))
    db.commit()

    price, carried = get_price(db, spy, same_date)
    assert price == Decimal("515.00")
    assert carried == 0


def test_fx_conversion_rules(test_db: Session):
    db = test_db
    d = date(2026, 1, 1)

    # Direct rate: AEDUSD = 0.2723 (1 AED = 0.2723 USD)
    record_fx(db, "AEDUSD", d, Decimal("0.272294"))
    # Direct rate: EURUSD = 1.0850 (1 EUR = 1.0850 USD)
    record_fx(db, "EURUSD", d, Decimal("1.0850"))
    db.commit()

    # 1. Direct pair: 100 AED to USD
    val_usd = convert_amount(db, Decimal("100"), "AED", "USD", d)
    assert abs(val_usd - Decimal("27.2294")) < Decimal("0.0001")

    # 2. Inverse pair: 100 USD to AED (uses 1 / AEDUSD)
    val_aed = convert_amount(db, Decimal("27.2294"), "USD", "AED", d)
    assert abs(val_aed - Decimal("100")) < Decimal("0.001")

    # 3. Triangulation via USD: AED to EUR (AED -> USD -> EUR)
    # 100 AED * 0.272294 = 27.2294 USD; then 27.2294 USD / 1.0850 = 25.0962 EUR
    val_eur = convert_amount(db, Decimal("100"), "AED", "EUR", d)
    expected_eur = (Decimal("100") * Decimal("0.272294")) / Decimal("1.0850")
    assert abs(val_eur - expected_eur) < Decimal("0.0001")

    # 4. Missing FX rate raises FxRateMissingError
    with pytest.raises(FxRateMissingError) as exc_info:
        convert_amount(db, Decimal("100"), "JPY", "GBP", d)
    assert "Missing FX rate" in exc_info.value.detail


def test_get_live_price_manual_and_cache(test_db: Session, client_env: dict):
    from app.engines.pricing import get_live_price

    db = test_db
    spy = client_env["spy"]

    spy.price_source = "manual"
    spy.manual_price = Decimal("550.00")
    db.commit()

    price = get_live_price(db, spy)
    assert price == Decimal("550.00")


def test_get_live_price_disabled_provider(test_db: Session, client_env: dict):
    from app.core.errors import PriceUnavailableError
    from app.engines.pricing import get_live_price

    db = test_db
    spy = client_env["spy"]
    spy.price_source = "yahoo"
    db.commit()

    old_provider = settings.data_provider
    settings.data_provider = "off"
    try:
        with pytest.raises(PriceUnavailableError) as exc_info:
            get_live_price(db, spy)
        assert "Live prices are disabled" in exc_info.value.detail
    finally:
        settings.data_provider = old_provider


def test_pricing_edge_cases_and_subunit_and_yfinance_mock(test_db: Session, client_env: dict, monkeypatch):
    import sys
    import types
    from datetime import UTC, datetime

    from app.core.errors import PriceUnavailableError
    from app.engines.accounting import PositionState
    from app.engines.pricing import get_live_price, value_positions
    from app.models.entities import PriceCache, Security

    db = test_db
    spy = client_env["spy"]
    d = date(2026, 1, 5)

    # 1. get_price with no history at all -> ValuationGapError (reason="no_history")
    with pytest.raises(ValuationGapError) as exc_info:
        get_price(db, spy, d)
    assert exc_info.value.extra.get("reason") == "no_history"

    # 2. FX inverse triangulation legs: USDGBP and USDCHF stored (where src_usd uses 1/USDGBP)
    record_fx(db, "USDGBP", d, Decimal("0.80"))
    record_fx(db, "USDCHF", d, Decimal("0.90"))
    # Update existing FX row to cover update branch (lines 254-255)
    record_fx(db, "USDGBP", d, Decimal("0.80"))
    db.commit()

    # GBP -> CHF via 1/USDGBP * USDCHF
    converted = convert_amount(db, Decimal("80"), "GBP", "CHF", d)
    assert abs(converted - Decimal("90")) < Decimal("0.0001")

    # Invalid FX rate <= 0 raises ValueError
    with pytest.raises(ValueError, match="FX rate must be positive"):
        record_fx(db, "EURUSD", d, Decimal("0"))

    # 3. Subunit security & currency conversion in record_manual_price
    uk_sec = Security(
        symbol="VOD.L",
        name="Vodafone",
        asset_class="Equity",
        currency="GBP",
        price_source="manual",
        price_unit="subunit",
        divisor=Decimal("100"),
        settlement_days=2,
    )
    db.add(uk_sec)
    db.commit()

    # Record price in USD for a GBP subunit security (converts USD->GBP, then divides by 100)
    row = record_manual_price(db, uk_sec, d, Decimal("100"), currency="USD")
    db.commit()
    assert abs(row.close - Decimal("0.80")) < Decimal("0.0001")

    # 4. value_positions with cached prices dict and missing security
    prices_cache: dict[str, tuple[Decimal, int]] = {}
    pos_map = {
        uk_sec.id: PositionState(
            security_id=uk_sec.id, symbol="VOD.L", quantity=Decimal("10"), avg_cost=Decimal("0.8")
        )
    }
    val1 = value_positions(db, pos_map, d, "USD", prices=prices_cache)
    assert uk_sec.id in prices_cache
    val2 = value_positions(db, pos_map, d, "USD", prices=prices_cache)
    assert val1.total == val2.total

    with pytest.raises(ValuationGapError):
        value_positions(
            db,
            {
                "missing-sec": PositionState(
                    security_id="missing-sec", symbol="X", quantity=Decimal("1"), avg_cost=Decimal("1")
                )
            },
            d,
            "USD",
        )

    # 5. get_live_price branches:
    # 5a. Fresh PriceCache hit (with naive datetime to cover tzinfo branch)
    db.add(
        PriceCache(
            security_id=spy.id,
            price=Decimal("512.34"),
            fetched_at=datetime.now(UTC).replace(tzinfo=None),
            source="yahoo",
        )
    )
    db.commit()
    assert get_live_price(db, spy) == Decimal("512.34")

    # 5b. Manual security with manual_price=None falling back to PriceHistory, or raising PriceUnavailableError
    uk_sec.manual_price = None
    db.commit()
    assert abs(get_live_price(db, uk_sec, force=True) - Decimal("0.80")) < Decimal("0.0001")

    empty_sec = Security(
        symbol="EMPTY", name="Empty", asset_class="Equity", currency="USD", price_source="manual"
    )
    db.add(empty_sec)
    db.commit()
    with pytest.raises(PriceUnavailableError):
        get_live_price(db, empty_sec, force=True)

    # 5c. Unsupported price_source with data_provider="yahoo"
    old_provider = settings.data_provider
    settings.data_provider = "yahoo"
    try:
        empty_sec.price_source = "bloomberg"
        db.commit()
        with pytest.raises(PriceUnavailableError, match="not implemented"):
            get_live_price(db, empty_sec, force=True)

        # 5d. Mock yfinance for yahoo price_source (both new cache and updating existing cache)
        fake_yf = types.ModuleType("yfinance")

        class FakeTicker:
            def __init__(self, ticker_symbol: str):
                self.fast_info = types.SimpleNamespace(last_price=600.0)

        fake_yf.Ticker = FakeTicker  # type: ignore[attr-defined]
        monkeypatch.setitem(sys.modules, "yfinance", fake_yf)

        spy.price_source = "yahoo"
        spy.price_unit = "subunit"
        spy.divisor = Decimal("2")
        db.commit()

        live_val = get_live_price(db, spy, force=True)
        assert live_val == Decimal("300")

        # Also test new PriceCache creation when no cache row existed previously
        empty_sec.price_source = "yahoo"
        db.commit()
        assert get_live_price(db, empty_sec, force=True) == Decimal("600")
    finally:
        settings.data_provider = old_provider


def test_eodhd_batch_guardrails_and_daily_sync_cache(test_db: Session, client_env: dict, monkeypatch):
    import types

    import httpx
    import pandas as pd

    from app.core.errors import PriceUnavailableError
    from app.engines.pricing import fetch_eodhd_batch, fetch_yahoo_batch, get_live_price, sync_daily_prices
    from app.models.entities import Security

    db = test_db
    spy = client_env["spy"]

    # 1. Guardrail: EODHD must strictly refuse US stocks and Crypto
    old_key = settings.eodhd_api_key
    settings.eodhd_api_key = "test-key"
    try:
        with pytest.raises(PriceUnavailableError, match="must be fetched via Yahoo Finance"):
            fetch_eodhd_batch(["AAPL"])
        with pytest.raises(PriceUnavailableError, match="must be fetched via Yahoo Finance"):
            fetch_eodhd_batch(["BTC-USD"])

        # 2. Mock httpx.get for valid EODHD regional batch request (single HTTP call for multiple tickers)
        called_urls: list[str] = []

        def fake_get(url: str, params: dict | None = None, timeout: float = 15.0):
            called_urls.append(url)
            return types.SimpleNamespace(
                status_code=200,
                json=lambda: [
                    {"code": "COMI.EGX", "close": 82.50},
                    {"code": "EFID.EGX", "close": 29.10},
                ],
                text="ok",
            )

        monkeypatch.setattr(httpx, "get", fake_get)
        eod_res = fetch_eodhd_batch(["COMI.EGX", "EFID.EGX"])
        assert len(called_urls) == 1
        assert eod_res["COMI.EGX"] == Decimal("82.50")
        assert eod_res["EFID.EGX"] == Decimal("29.10")

        # 3. Mock yfinance.download for batch sync and verify daily cache hit on second call
        import sys

        fake_yf = types.ModuleType("yfinance")
        download_calls = []

        def fake_download(tickers, **kwargs):
            download_calls.append(tickers)
            return pd.DataFrame({t: [100.0, 105.25] for t in tickers})

        fake_yf.download = fake_download  # type: ignore[attr-defined]
        monkeypatch.setitem(sys.modules, "yfinance", fake_yf)

        spy.price_source = "yahoo"
        gulf_sec = Security(
            symbol="COMI.EGX",
            name="CIB Egypt",
            asset_class="equity",
            currency="EGP",
            price_source="eodhd",
        )
        db.add(gulf_sec)
        db.commit()

        # First sync: fetches from providers and populates daily price library
        res1 = sync_daily_prices(db, symbols=["SPY", "COMI.EGX"], force=True)
        db.commit()
        assert res1["fetched_yahoo_count"] == 1
        assert res1["fetched_eodhd_count"] == 1
        assert res1["from_cache_count"] == 0

        # Second sync on same day (force=False): 100% served from local daily price library!
        res2 = sync_daily_prices(db, symbols=["SPY", "COMI.EGX"], force=False)
        assert res2["from_cache_count"] == 2
        assert res2["fetched_yahoo_count"] == 0
        assert res2["fetched_eodhd_count"] == 0
        assert len(download_calls) == 1  # No extra network call made!

        # Also test single-ticker fetch_yahoo_batch
        single_res = fetch_yahoo_batch(["SPY"])
        assert single_res["SPY"] == Decimal("105.2500")

        # And get_live_price for eodhd security with force=True
        live_eod = get_live_price(db, gulf_sec, force=True)
        assert live_eod == Decimal("82.50")
    finally:
        settings.eodhd_api_key = old_key
