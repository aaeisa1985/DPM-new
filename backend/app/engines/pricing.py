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


def get_live_price(db: Session, security: Security, force: bool = False) -> Decimal:
    cached = _cache_row(db, security.id)
    if cached and not force and _is_fresh(cached.fetched_at):
        return D(cached.price)

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
            hist = ticker.history(period="1d", interval="1m")
            if len(hist):
                price = float(hist["Close"].iloc[-1])
        if price is None or price <= 0:
            raise ValueError("provider returned no price")
        value = D(str(price))
        if security.price_unit == "subunit" and D(security.divisor) != 0:
            value = value / D(security.divisor)
    except Exception as exc:  # pragma: no cover - network path
        raise PriceUnavailableError(
            f"Live price unavailable for {security.symbol}: {exc}", symbol=security.symbol
        ) from exc

    if cached is None:
        cached = PriceCache(security_id=security.id, price=value, source="yahoo")
        db.add(cached)
    else:
        cached.price = value
        cached.fetched_at = datetime.now(UTC)
        cached.source = "yahoo"
    db.flush()
    return value


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
