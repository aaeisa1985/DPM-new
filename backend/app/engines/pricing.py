"""Pricing, FX conversion and valuation engine."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import Decimal

from sqlalchemy import case, select
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.errors import FxRateMissingError, PriceUnavailableError, ValuationGapError
from app.core.money import D
from app.models.entities import FxHistory, PriceCache, PriceHistory, Security


@dataclass
class Valuation:
    total: Decimal
    by_security: dict[str, Decimal]
    max_carried_days: int


def get_price(db: Session, security: Security, on_date: date) -> tuple[Decimal, int]:
    row = db.execute(
        select(PriceHistory)
        .where(PriceHistory.security_id == security.id, PriceHistory.price_date <= on_date)
        .order_by(
            PriceHistory.price_date.desc(),
            case((PriceHistory.source == "manual", 0), else_=1),
        )
        .limit(1)
    ).scalar_one_or_none()
    if row is None:
        raise ValuationGapError(
            f"No closing price exists for {security.symbol} on or before {on_date.isoformat()}.",
            symbol=security.symbol,
            date=on_date.isoformat(),
            reason="no_history",
        )
    carried = (on_date - row.price_date).days
    if carried > settings.carry_forward_max:
        raise ValuationGapError(
            f"Price gap for {security.symbol}: last close {row.price_date.isoformat()} "
            f"is {carried} days before {on_date.isoformat()} "
            f"(max {settings.carry_forward_max}).",
            symbol=security.symbol,
            date=on_date.isoformat(),
            last_price_date=row.price_date.isoformat(),
            carried_days=carried,
        )
    return D(row.close), carried


def _latest_rate(db: Session, pair: str, on_date: date) -> Decimal | None:
    row = db.execute(
        select(FxHistory)
        .where(FxHistory.pair == pair, FxHistory.as_of <= on_date)
        .order_by(FxHistory.as_of.desc())
        .limit(1)
    ).scalar_one_or_none()
    return D(row.rate) if row else None


def convert_amount(db: Session, amount: Decimal, from_ccy: str, to_ccy: str, on_date: date) -> Decimal:
    src = from_ccy.upper()
    dst = to_ccy.upper()
    if src == dst:
        return D(amount)

    direct = _latest_rate(db, f"{src}{dst}", on_date)
    if direct is not None:
        return D(amount) * direct

    inverse = _latest_rate(db, f"{dst}{src}", on_date)
    if inverse is not None and inverse != 0:
        return D(amount) / inverse

    src_usd: Decimal | None = None
    if src == "USD":
        src_usd = Decimal("1")
    else:
        src_usd = _latest_rate(db, f"{src}USD", on_date)
        if src_usd is None:
            inv_src = _latest_rate(db, f"USD{src}", on_date)
            if inv_src not in (None, Decimal("0")):
                src_usd = Decimal("1") / inv_src

    usd_dst: Decimal | None = None
    if dst == "USD":
        usd_dst = Decimal("1")
    else:
        usd_dst = _latest_rate(db, f"USD{dst}", on_date)
        if usd_dst is None:
            inv_dst = _latest_rate(db, f"{dst}USD", on_date)
            if inv_dst not in (None, Decimal("0")):
                usd_dst = Decimal("1") / inv_dst

    if src_usd is not None and usd_dst is not None:
        return D(amount) * src_usd * usd_dst

    raise FxRateMissingError(
        f"Missing FX rate {src}->{dst} on or before {on_date.isoformat()}.",
        pair=f"{src}{dst}",
        date=on_date.isoformat(),
    )


def value_positions(
    db: Session,
    positions: dict,
    on_date: date,
    account_currency: str,
    prices: dict[str, tuple[Decimal, int]] | None = None,
) -> Valuation:
    by_security: dict[str, Decimal] = {}
    max_carried = 0
    total = Decimal("0")
    for security_id, position in positions.items():
        security = db.get(Security, security_id)
        if security is None:
            raise ValuationGapError(f"Security '{security_id}' not found.", security_id=security_id)
        if prices and security_id in prices:
            price, carried = prices[security_id]
        else:
            price, carried = get_price(db, security, on_date)
            if prices is not None:
                prices[security_id] = (price, carried)
        max_carried = max(max_carried, carried)
        local_value = D(position.quantity) * price
        value = convert_amount(db, local_value, security.currency, account_currency, on_date)
        by_security[security_id] = value
        total += value
    return Valuation(total=total, by_security=by_security, max_carried_days=max_carried)


def _cache_row(db: Session, security_id: str) -> PriceCache | None:
    return db.get(PriceCache, security_id)


def _is_fresh(fetched_at: datetime) -> bool:
    if fetched_at.tzinfo is None:
        fetched_at = fetched_at.replace(tzinfo=UTC)
    age = (datetime.now(UTC) - fetched_at).total_seconds()
    return age <= settings.price_ttl


def _save_daily_cache(
    db: Session,
    security: Security,
    raw_value: Decimal,
    source: str,
    on_date: date | None = None,
) -> Decimal:
    value = D(raw_value)
    if security.price_unit == "subunit" and D(security.divisor) != 0:
        value = value / D(security.divisor)

    now = datetime.now(UTC)
    cached = _cache_row(db, security.id)
    if cached is None:
        cached = PriceCache(security_id=security.id, price=value, fetched_at=now, source=source)
        db.add(cached)
    else:
        cached.price = value
        cached.fetched_at = now
        cached.source = source

    target_date = on_date or date.today()
    hist_row = db.get(PriceHistory, (security.id, target_date))
    if hist_row is None:
        hist_row = PriceHistory(
            security_id=security.id,
            price_date=target_date,
            close=value,
            source=source,
            fetched_at=now,
        )
        db.add(hist_row)
    elif hist_row.source != "manual":
        # Never overwrite manual price (Spec 4.10)
        hist_row.close = value
        hist_row.source = source
        hist_row.fetched_at = now

    db.flush()
    return value


def _is_us_or_crypto_or_yahoo_ticker(ticker: str, currency: str = "USD", asset_class: str = "equity") -> bool:
    t = ticker.strip().upper()
    ac = asset_class.strip().lower()
    if ac in {"crypto", "cryptocurrency", "fx", "forex"}:
        return True
    if t.endswith("-USD") or t.endswith("=X") or t.endswith(".US"):
        return True
    if "." not in t and currency.upper() == "USD":
        return True
    return False


def fetch_yahoo_batch(tickers: list[str]) -> dict[str, Decimal]:
    """Fetch closing/latest prices for multiple tickers in a single Yahoo Finance batch request."""
    cleaned = [t.strip().upper() for t in tickers if t.strip()]
    if not cleaned:
        return {}
    import yfinance as yf  # type: ignore

    results: dict[str, Decimal] = {}
    df = yf.download(cleaned, period="7d", progress=False, auto_adjust=False, threads=True)
    if df is None or len(df) == 0:
        return results

    close_df = df["Close"] if "Close" in df else df
    if len(cleaned) == 1:
        symbol = cleaned[0]
        series = close_df.dropna()
        if len(series) > 0:
            val = float(series.iloc[-1].item() if hasattr(series.iloc[-1], "item") else series.iloc[-1])
            if val > 0:
                results[symbol] = D(f"{val:.4f}")
    else:
        ffilled = close_df.ffill()
        last_row = ffilled.iloc[-1]
        for symbol in cleaned:
            if symbol in last_row:
                raw = last_row[symbol]
                val = float(raw.item() if hasattr(raw, "item") else raw)
                if val == val and val > 0:  # not NaN and positive
                    results[symbol] = D(f"{val:.4f}")
    return results


def fetch_eodhd_batch(tickers: list[str], api_key: str | None = None) -> dict[str, Decimal]:
    """Fetch multiple Gulf/non-US tickers in a SINGLE EODHD API request.

    Strictly refuses US stocks, Crypto, or FX tickers (those must use Yahoo Finance).
    """
    key = (api_key or settings.eodhd_api_key).strip()
    if not key:
        raise PriceUnavailableError("EODHD API key is not configured (DPM_EODHD_API_KEY).")

    cleaned = []
    for t in tickers:
        sym = t.strip().upper()
        if not sym:
            continue
        if _is_us_or_crypto_or_yahoo_ticker(sym):
            raise PriceUnavailableError(
                f"Ticker '{sym}' is a US/Crypto/FX symbol and must be fetched via Yahoo Finance, not EODHD.",
                symbol=sym,
            )
        cleaned.append(sym)

    if not cleaned:
        return {}

    import httpx

    primary = cleaned[0]
    params: dict[str, str] = {"api_token": key, "fmt": "json"}
    if len(cleaned) > 1:
        params["s"] = ",".join(cleaned[1:])

    url = f"https://eodhd.com/api/real-time/{primary}"
    resp = httpx.get(url, params=params, timeout=15.0)
    if resp.status_code != 200:
        raise PriceUnavailableError(
            f"EODHD batch request failed with HTTP {resp.status_code}: {resp.text[:200]}"
        )
    payload = resp.json()
    items = payload if isinstance(payload, list) else [payload]
    results: dict[str, Decimal] = {}
    for item in items:
        if not isinstance(item, dict):
            continue
        code = str(item.get("code", "")).upper()
        raw_close = item.get("close") or item.get("previousClose")
        try:
            if raw_close is not None and str(raw_close) != "NA":
                price_dec = D(str(raw_close))
                if price_dec > 0:
                    results[code] = price_dec
        except Exception:
            continue
    return results


def get_live_price(db: Session, security: Security, force: bool = False) -> Decimal:
    cached = _cache_row(db, security.id)
    if cached and not force and _is_fresh(cached.fetched_at):
        return D(cached.price)

    today = date.today()
    if not force:
        daily_row = db.get(PriceHistory, (security.id, today))
        if daily_row is not None:
            return D(daily_row.close)

    if security.price_source == "manual":
        if security.manual_price is not None:
            return D(security.manual_price)
        latest = db.execute(
            select(PriceHistory)
            .where(PriceHistory.security_id == security.id)
            .order_by(PriceHistory.price_date.desc())
            .limit(1)
        ).scalar_one_or_none()
        if latest:
            return D(latest.close)
        raise PriceUnavailableError(
            f"No manual price configured for {security.symbol}.", symbol=security.symbol
        )

    if settings.data_provider == "off":
        raise PriceUnavailableError(
            f"Live prices are disabled (DPM_DATA_PROVIDER=off) for {security.symbol}.",
            symbol=security.symbol,
        )

    if security.price_source == "eodhd":
        ticker_sym = (security.yahoo_ticker or security.symbol).strip().upper()
        if _is_us_or_crypto_or_yahoo_ticker(ticker_sym, security.currency, security.asset_class):
            raise PriceUnavailableError(
                f"EODHD is restricted to Gulf/regional equities; use 'yahoo' for {security.symbol}.",
                symbol=security.symbol,
            )
        batch = fetch_eodhd_batch([ticker_sym])
        if ticker_sym not in batch:
            raise PriceUnavailableError(
                f"EODHD returned no price for {security.symbol} ({ticker_sym}).",
                symbol=security.symbol,
            )
        return _save_daily_cache(db, security, batch[ticker_sym], source="eodhd", on_date=today)

    if security.price_source != "yahoo":
        raise PriceUnavailableError(
            f"Provider '{security.price_source}' is not implemented for {security.symbol}.",
            symbol=security.symbol,
        )

    try:  # Lazy import: yfinance/pandas are never imported at startup.
        import yfinance as yf  # type: ignore

        ticker = yf.Ticker(security.yahoo_ticker or security.symbol)
        price: float | None = None
        try:
            price = float(ticker.fast_info.last_price)
        except Exception:
            hist = ticker.history(period="5d")
            if len(hist):
                price = float(hist["Close"].dropna().iloc[-1])
        if price is None or price <= 0:
            raise ValueError("provider returned no price")
        raw_val = D(str(price))
    except Exception as exc:  # pragma: no cover - network path
        raise PriceUnavailableError(
            f"Live price unavailable for {security.symbol}: {exc}", symbol=security.symbol
        ) from exc

    return _save_daily_cache(db, security, raw_val, source="yahoo", on_date=today)


def sync_daily_prices(
    db: Session,
    symbols: list[str] | None = None,
    force: bool = False,
    on_date: date | None = None,
) -> dict:
    """Sync the daily price cache library for all or specified securities in single batch requests.

    - Serves already-fetched prices for ``on_date`` (default today) directly from SQLite (`price_history`)
      unless ``force=True``.
    - Groups all Yahoo-eligible securities into ONE single `yf.download` batch call.
    - Groups all EODHD Gulf securities into ONE single EODHD batch call (never calling EODHD for US/Crypto).
    """
    target_date = on_date or date.today()
    query = select(Security).order_by(Security.symbol)
    if symbols:
        upper_syms = [s.strip().upper() for s in symbols if s.strip()]
        query = query.where(Security.symbol.in_(upper_syms))
    secs = db.execute(query).scalars().all()

    from_cache: dict[str, str] = {}
    fetched_yahoo: dict[str, str] = {}
    fetched_eodhd: dict[str, str] = {}
    errors: dict[str, str] = {}

    yahoo_map: dict[str, Security] = {}
    eodhd_map: dict[str, Security] = {}

    for sec in secs:
        if sec.price_source == "manual":
            continue
        if not force:
            existing = db.get(PriceHistory, (sec.id, target_date))
            if existing is not None:
                from_cache[sec.symbol] = str(D(existing.close))
                continue

        provider_ticker = (sec.yahoo_ticker or sec.symbol).strip().upper()
        if sec.price_source == "eodhd" and not _is_us_or_crypto_or_yahoo_ticker(
            provider_ticker, sec.currency, sec.asset_class
        ):
            eodhd_map[provider_ticker] = sec
        else:
            yahoo_map[provider_ticker] = sec

    if yahoo_map:
        try:
            y_prices = fetch_yahoo_batch(list(yahoo_map.keys()))
            for ticker_key, sec in yahoo_map.items():
                if ticker_key in y_prices:
                    saved = _save_daily_cache(
                        db, sec, y_prices[ticker_key], source="yahoo", on_date=target_date
                    )
                    fetched_yahoo[sec.symbol] = str(saved)
                else:
                    errors[sec.symbol] = f"No price returned from Yahoo Finance for '{ticker_key}'"
        except Exception as exc:
            for sec in yahoo_map.values():
                errors[sec.symbol] = str(exc)

    if eodhd_map:
        try:
            e_prices = fetch_eodhd_batch(list(eodhd_map.keys()))
            for ticker_key, sec in eodhd_map.items():
                if ticker_key in e_prices:
                    saved = _save_daily_cache(
                        db, sec, e_prices[ticker_key], source="eodhd", on_date=target_date
                    )
                    fetched_eodhd[sec.symbol] = str(saved)
                else:
                    errors[sec.symbol] = f"No price returned from EODHD for '{ticker_key}'"
        except Exception as exc:
            for sec in eodhd_map.values():
                errors[sec.symbol] = str(exc)

    return {
        "date": target_date.isoformat(),
        "from_cache_count": len(from_cache),
        "fetched_yahoo_count": len(fetched_yahoo),
        "fetched_eodhd_count": len(fetched_eodhd),
        "from_cache": from_cache,
        "fetched_yahoo": fetched_yahoo,
        "fetched_eodhd": fetched_eodhd,
        "errors": errors,
    }


def record_manual_price(
    db: Session,
    security: Security,
    price_date: date,
    close: Decimal,
    currency: str | None = None,
) -> PriceHistory:
    value = D(close)
    if currency and currency.upper() != security.currency:
        value = convert_amount(db, value, currency.upper(), security.currency, price_date)
    if security.price_unit == "subunit" and D(security.divisor) != 0:
        value = value / D(security.divisor)

    row = db.get(PriceHistory, (security.id, price_date))
    if row is None:
        row = PriceHistory(
            security_id=security.id,
            price_date=price_date,
            close=value,
            source="manual",
        )
        db.add(row)
    else:
        row.close = value
        row.source = "manual"
        row.fetched_at = datetime.now(UTC)
    security.manual_price = value
    db.flush()
    return row


def record_fx(db: Session, pair: str, on_date: date, rate: Decimal, source: str = "manual") -> FxHistory:
    pair = pair.strip().upper()
    value = D(rate)
    if value <= 0:
        raise ValueError("FX rate must be positive")
    row = db.get(FxHistory, (pair, on_date))
    if row is None:
        row = FxHistory(pair=pair, as_of=on_date, rate=value, source=source)
        db.add(row)
    else:
        row.rate = value
        row.source = source
    db.flush()
    return row
