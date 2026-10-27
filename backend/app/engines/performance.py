"""Time-weighted return (TWR) and benchmark performance.

The engine replays the book once, day by day, keeping an in-memory lot state.
That keeps a two-year daily report fast without mutating or re-reading tables
on every date.

Start-of-day convention (spec 5.3):

    r(D) = close(D) / (close(D-1) + external_flows(D)) - 1

``close(D)`` is cash + marked-to-market positions at date ``D``. External
flows are deposits, withdrawals and account fees. Cash dividends are internal
and are therefore *not* added to the denominator.
"""

from __future__ import annotations

from bisect import bisect_right
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import Decimal

from sqlalchemy import false, select
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.errors import NotFoundError, ValuationGapError
from app.core.money import E8, D
from app.models.entities import (
    Account,
    CashFlow,
    Client,
    CorporateAction,
    FxHistory,
    MandateVersion,
    PriceHistory,
    Security,
    Transaction,
)

EXTERNAL_FLOW_TYPES = {"deposit", "withdrawal", "fee"}


@dataclass
class PriceSeries:
    dates: list[date] = field(default_factory=list)
    closes: list[Decimal] = field(default_factory=list)

    def at(self, day: date, symbol: str) -> tuple[Decimal, int]:
        index = bisect_right(self.dates, day) - 1
        if index < 0:
            raise ValuationGapError(
                f"No closing price exists for {symbol} on or before {day.isoformat()}.",
                symbol=symbol,
                date=day.isoformat(),
                reason="no_history",
            )
        carried = (day - self.dates[index]).days
        if carried > settings.carry_forward_max:
            raise ValuationGapError(
                f"Price gap for {symbol}: last close {self.dates[index].isoformat()} "
                f"is {carried} days before {day.isoformat()} "
                f"(max {settings.carry_forward_max}).",
                symbol=symbol,
                date=day.isoformat(),
                last_price_date=self.dates[index].isoformat(),
                carried_days=carried,
            )
        return self.closes[index], carried


def load_price_series(db: Session, securities: list[Security], end: date) -> dict[str, PriceSeries]:
    if not securities:
        return {}
    ids = [s.id for s in securities]
    rows = (
        db.execute(
            select(PriceHistory)
            .where(PriceHistory.security_id.in_(ids), PriceHistory.price_date <= end)
            .order_by(PriceHistory.security_id, PriceHistory.price_date, PriceHistory.source)
        )
        .scalars()
        .all()
    )
    by_sec: dict[str, dict[date, tuple[Decimal, str]]] = {}
    for row in rows:
        bucket = by_sec.setdefault(row.security_id, {})
        # Manual wins on the same date; rows are ordered manual -> yahoo -> openbb
        # alphabetically (manual < openbb < yahoo), so explicit priority is needed.
        existing = bucket.get(row.price_date)
        if existing is None or row.source == "manual" or existing[1] != "manual":
            bucket[row.price_date] = (D(row.close), row.source)
    result: dict[str, PriceSeries] = {}
    for security in securities:
        bucket = by_sec.get(security.id, {})
        ordered = sorted(bucket.items())
        result[security.id] = PriceSeries(dates=[d for d, _ in ordered], closes=[v[0] for _, v in ordered])
    return result


class FxBook:
    """In-memory FX lookup with direct / inverse / USD triangulation."""

    def __init__(self, db: Session, end: date) -> None:
        self._rows: dict[str, tuple[list[date], list[Decimal]]] = {}
        for row in db.execute(select(FxHistory).where(FxHistory.as_of <= end)).scalars():
            dates, rates = self._rows.setdefault(row.pair, ([], []))
            dates.append(row.as_of)
            rates.append(D(row.rate))
        for dates, rates in self._rows.values():
            pairs = sorted(zip(dates, rates), key=lambda item: item[0])
            dates[:] = [p[0] for p in pairs]
            rates[:] = [p[1] for p in pairs]
        self._cache: dict[tuple[str, str, date], Decimal] = {}

    def rate(self, pair: str, day: date) -> Decimal | None:
        row = self._rows.get(pair)
        if not row:
            return None
        index = bisect_right(row[0], day) - 1
        if index < 0:
            return None
        return row[1][index]

    def convert(self, amount: Decimal, src: str, dst: str, day: date) -> Decimal:
        src, dst = src.upper(), dst.upper()
        if src == dst:
            return D(amount)
        key = (src, dst, day)
        if key in self._cache:
            return D(amount) * self._cache[key]
        direct = self.rate(f"{src}{dst}", day)
        if direct is not None:
            self._cache[key] = direct
            return D(amount) * direct
        inverse = self.rate(f"{dst}{src}", day)
        if inverse not in (None, Decimal("0")):
            self._cache[key] = Decimal("1") / inverse
            return D(amount) / inverse
        src_usd: Decimal | None = None
        if src == "USD":
            src_usd = Decimal("1")
        else:
            src_usd = self.rate(f"{src}USD", day)
            if src_usd is None:
                inv_src = self.rate(f"USD{src}", day)
                if inv_src not in (None, Decimal("0")):
                    src_usd = Decimal("1") / inv_src

        usd_dst: Decimal | None = None
        if dst == "USD":
            usd_dst = Decimal("1")
        else:
            usd_dst = self.rate(f"USD{dst}", day)
            if usd_dst is None:
                inv_dst = self.rate(f"{dst}USD", day)
                if inv_dst not in (None, Decimal("0")):
                    usd_dst = Decimal("1") / inv_dst

        if src_usd is not None and usd_dst is not None:
            self._cache[key] = src_usd * usd_dst
            return D(amount) * src_usd * usd_dst
        from app.core.errors import FxRateMissingError

        raise FxRateMissingError(
            f"Missing FX rate {src}->{dst} on or before {day.isoformat()}.",
            pair=f"{src}{dst}",
            date=day.isoformat(),
        )


@dataclass
class AccountDay:
    day: date
    close_value: Decimal
    external_flow: Decimal
    carried_days: int
    cash: Decimal
    invested: Decimal


@dataclass
class PerformancePoint:
    day: date
    close_value: Decimal
    start_value: Decimal
    external_flow: Decimal
    daily_return: Decimal
    cumulative_return: Decimal
    carried_days: int
    cash: Decimal
    invested: Decimal


@dataclass
class PerformanceResult:
    start: date
    end: date
    twr: Decimal
    annualized: Decimal
    max_drawdown: Decimal
    points: list[PerformancePoint]
    max_carried_days: int
    benchmark: dict | None = None


def _version_for(versions: Sequence[MandateVersion], day: date) -> MandateVersion:
    selected = versions[0]
    for version in versions:
        if version.effective_from <= day:
            selected = version
        else:
            break
    return selected


def _account_days(
    db: Session,
    account: Account,
    start: date,
    end: date,
    prices: dict[str, PriceSeries],
    fx: FxBook,
    securities: dict[str, Security],
) -> list[AccountDay]:
    transactions = (
        db.execute(
            select(Transaction)
            .where(
                Transaction.account_id == account.id,
                Transaction.status == "active",
                Transaction.trade_date <= end,
            )
            .order_by(Transaction.trade_date, Transaction.sequence, Transaction.created_at, Transaction.id)
        )
        .scalars()
        .all()
    )
    flows = (
        db.execute(
            select(CashFlow)
            .where(CashFlow.account_id == account.id, CashFlow.flow_date <= end)
            .order_by(CashFlow.flow_date, CashFlow.sequence, CashFlow.created_at, CashFlow.id)
        )
        .scalars()
        .all()
    )
    held_ids = {t.security_id for t in transactions} | {f.security_id for f in flows if f.security_id}
    splits_query = (
        select(CorporateAction)
        .where(
            CorporateAction.security_id.in_(held_ids) if held_ids else false(),
            CorporateAction.type == "split",
            CorporateAction.action_date <= end,
        )
        .order_by(CorporateAction.action_date, CorporateAction.created_at)
    )
    splits = db.execute(splits_query).scalars().all()
    versions = (
        db.execute(
            select(MandateVersion)
            .where(MandateVersion.mandate_id == account.mandate_id)
            .order_by(MandateVersion.effective_from, MandateVersion.version)
        )
        .scalars()
        .all()
    )
    if not versions:
        raise NotFoundError(f"Mandate '{account.mandate_id}' has no weights version.")

    tx_by_day: dict[date, list[Transaction]] = {}
    for tx in transactions:
        tx_by_day.setdefault(tx.trade_date, []).append(tx)
    flow_by_day: dict[date, list[CashFlow]] = {}
    for flow in flows:
        flow_by_day.setdefault(flow.flow_date, []).append(flow)
    split_by_day: dict[date, list[CorporateAction]] = {}
    for split in splits:
        split_by_day.setdefault(split.action_date, []).append(split)

    lots: dict[str, list[list]] = {}
    cash = D(account.opening_cash)
    days: list[AccountDay] = []

    day = start
    while day <= end:
        for split in split_by_day.get(day, []):
            for lot in lots.get(split.security_id, []):
                if lot[2] < split.action_date:
                    lot[0] = lot[0] * D(split.ratio)
                    lot[1] = lot[1] / D(split.ratio)

        for tx in tx_by_day.get(day, []):
            security = securities.get(tx.security_id)
            if security is None:
                security = db.get(Security, tx.security_id)
                if security is None:
                    raise NotFoundError(f"Security '{tx.security_id}' not found.")
                securities[security.id] = security
            qty = D(tx.quantity)
            price = D(tx.unit_price)
            fee = D(tx.fee)
            factor = D(tx.fx_rate)
            if tx.side == "BUY":
                unit_cost = ((price + fee / qty) if qty else price) * factor
                lots.setdefault(tx.security_id, []).append([qty, unit_cost, tx.trade_date, tx.id])
                cash -= (qty * price + fee) * factor
            else:
                method = _version_for(versions, tx.trade_date).lot_method
                position = lots.get(tx.security_id, [])
                if method == "hifo":
                    ordered = sorted(position, key=lambda lot: (-lot[1], lot[2], lot[3]))
                else:
                    ordered = sorted(position, key=lambda lot: (lot[2], lot[3]))
                remaining = qty
                for lot in ordered:
                    if remaining <= E8:
                        break
                    take = min(lot[0], remaining)
                    lot[0] -= take
                    remaining -= take
                if remaining > E8:
                    from app.core.errors import OverSellError

                    available = qty - remaining
                    raise OverSellError(
                        f"SELL {security.symbol} {qty} exceeds available {available} "
                        f"on {tx.trade_date.isoformat()}.",
                        transaction_id=tx.id,
                        symbol=security.symbol,
                        requested=str(qty),
                        available=str(available),
                    )
                cash += (qty * price - fee) * factor
            lots[tx.security_id] = [lot for lot in lots.get(tx.security_id, []) if lot[0] > E8]

        external = Decimal("0")
        for flow in flow_by_day.get(day, []):
            cash += D(flow.amount)
            if flow.type in EXTERNAL_FLOW_TYPES:
                external += D(flow.amount)

        invested = Decimal("0")
        carried = 0
        for security_id, position in lots.items():
            qty = sum((lot[0] for lot in position), Decimal("0"))
            if qty <= E8:
                continue
            security = securities.get(security_id)
            if security is None:
                security = db.get(Security, security_id)
                if security is None:
                    continue
                securities[security_id] = security
            price, carried_days = prices[security_id].at(day, security.symbol)
            carried = max(carried, carried_days)
            local = qty * price
            invested += fx.convert(local, security.currency, account.currency, day)

        days.append(
            AccountDay(
                day=day,
                close_value=cash + invested,
                external_flow=external,
                carried_days=carried,
                cash=cash,
                invested=invested,
            )
        )
        day += timedelta(days=1)
    return days


def _max_drawdown(points: list[PerformancePoint]) -> Decimal:
    peak = Decimal("0")
    worst = Decimal("0")
    for point in points:
        value = point.close_value
        if value > peak:
            peak = value
        if peak > 0:
            drawdown = value / peak - Decimal("1")
            if drawdown < worst:
                worst = drawdown
    return worst


def _benchmark_series(
    db: Session, benchmark: Security, start: date, end: date, snapshot: dict[str, PriceSeries] | None = None
) -> dict[date, Decimal]:
    series = (snapshot or {}).get(benchmark.id)
    if series is None:
        series = load_price_series(db, [benchmark], end).get(benchmark.id)
    if series is None or not series.dates:
        raise ValuationGapError(
            f"No closing price exists for benchmark {benchmark.symbol}.",
            symbol=benchmark.symbol,
        )
    splits = (
        db.execute(
            select(CorporateAction)
            .where(
                CorporateAction.security_id == benchmark.id,
                CorporateAction.type == "split",
                CorporateAction.action_date <= end,
            )
            .order_by(CorporateAction.action_date)
        )
        .scalars()
        .all()
    )
    values: dict[date, Decimal] = {}
    day = start
    while day <= end:
        close, _ = series.at(day, benchmark.symbol)
        adjustment = Decimal("1")
        for split in splits:
            if split.action_date > day:
                adjustment *= D(split.ratio)
        values[day] = close / adjustment if adjustment else close
        day += timedelta(days=1)
    return values


def compute_performance(
    db: Session,
    *,
    client_id: str,
    end: date,
    start: date | None = None,
    benchmark: Security | None = None,
) -> PerformanceResult:
    accounts = (
        db.execute(select(Account).where(Account.client_id == client_id).order_by(Account.account_code))
        .scalars()
        .all()
    )
    if not accounts:
        raise NotFoundError(f"No account exists for client '{client_id}'.")
    client = db.get(Client, client_id)
    if client is None:
        raise NotFoundError(f"Client '{client_id}' not found.")
    base_currency = client.base_currency

    all_secs = {s.id: s for s in db.execute(select(Security)).scalars().all()}
    account_ids = [a.id for a in accounts]
    first_tx = db.execute(
        select(Transaction.trade_date)
        .where(Transaction.account_id.in_(account_ids), Transaction.status == "active")
        .order_by(Transaction.trade_date)
        .limit(1)
    ).scalar_one_or_none()
    first_flow = db.execute(
        select(CashFlow.flow_date)
        .where(CashFlow.account_id.in_(account_ids))
        .order_by(CashFlow.flow_date)
        .limit(1)
    ).scalar_one_or_none()
    candidates = [d for d in (first_tx, first_flow) if d is not None]
    if start is None:
        start = min(candidates) if candidates else end
    if start > end:
        raise NotFoundError("Performance start date is after the end date.")

    used_ids: set[str] = set()
    tx_secs = db.execute(
        select(Transaction.security_id).where(Transaction.account_id.in_(account_ids)).distinct()
    ).scalars()
    used_ids.update(tx_secs)
    flow_secs = db.execute(
        select(CashFlow.security_id)
        .where(CashFlow.account_id.in_(account_ids), CashFlow.security_id.is_not(None))
        .distinct()
    ).scalars()
    used_ids.update(flow_secs)
    if benchmark:
        used_ids.add(benchmark.id)
    securities = [all_secs[i] for i in used_ids if i in all_secs]
    prices = load_price_series(db, securities, end)
    fx = FxBook(db, end)
    for security in securities:
        prices.setdefault(security.id, PriceSeries())

    per_account = {
        account.id: _account_days(db, account, start, end, prices, fx, all_secs) for account in accounts
    }

    num_days = (end - start).days + 1
    combined_close: list[Decimal] = [Decimal("0")] * num_days
    combined_flow: list[Decimal] = [Decimal("0")] * num_days
    combined_cash: list[Decimal] = [Decimal("0")] * num_days
    combined_invested: list[Decimal] = [Decimal("0")] * num_days
    carried = [0] * num_days
    for account in accounts:
        rows = per_account[account.id]
        for row in rows:
            index = (row.day - start).days
            combined_close[index] += fx.convert(row.close_value, account.currency, base_currency, row.day)
            combined_flow[index] += fx.convert(row.external_flow, account.currency, base_currency, row.day)
            combined_cash[index] += fx.convert(row.cash, account.currency, base_currency, row.day)
            combined_invested[index] += fx.convert(row.invested, account.currency, base_currency, row.day)
            carried[index] = max(carried[index], row.carried_days)

    points: list[PerformancePoint] = []
    cumulative = Decimal("1")
    for index in range(num_days):
        day = start + timedelta(days=index)
        close_value = combined_close[index]
        external = combined_flow[index]
        if index == 0:
            start_value = close_value
            daily = Decimal("0")
        else:
            start_value = combined_close[index - 1] + external
            daily = (close_value / start_value - Decimal("1")) if start_value != 0 else Decimal("0")
        cumulative *= Decimal("1") + daily
        points.append(
            PerformancePoint(
                day=day,
                close_value=close_value,
                start_value=start_value,
                external_flow=external,
                daily_return=daily,
                cumulative_return=cumulative - Decimal("1"),
                carried_days=carried[index],
                cash=combined_cash[index],
                invested=combined_invested[index],
            )
        )

    twr = cumulative - Decimal("1")
    span_days = max((end - start).days, 1)
    annualized = (
        (Decimal("1") + twr) ** (Decimal(365) / Decimal(span_days)) - Decimal("1")
        if (Decimal("1") + twr) > 0
        else Decimal("-1")
    )

    bench_out = None
    if benchmark is not None:
        values = _benchmark_series(db, benchmark, start, end, prices)
        first = values[start]
        bench_points = []
        previous = first
        bench_cum = Decimal("1")
        for index in range(num_days):
            day = start + timedelta(days=index)
            last = values[day]
            daily = (last / previous - Decimal("1")) if previous else Decimal("0")
            previous = last
            if index == 0:
                daily = Decimal("0")
            bench_cum *= Decimal("1") + daily
            bench_points.append(
                {
                    "day": day,
                    "value": last / first * Decimal("100") if first else Decimal("0"),
                    "cumulative_return": bench_cum - Decimal("1"),
                }
            )
        bench_out = {
            "symbol": benchmark.symbol,
            "total_return": bench_cum - Decimal("1"),
            "points": bench_points,
        }

    return PerformanceResult(
        start=start,
        end=end,
        twr=twr,
        annualized=annualized,
        max_drawdown=_max_drawdown(points),
        points=points,
        max_carried_days=max(carried) if carried else 0,
        benchmark=bench_out,
    )
