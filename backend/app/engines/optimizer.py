"""Optional portfolio optimiser. Failure is isolated from the rest of DPM.

Heavy dependencies are imported inside this module only when the endpoint is
called. Riskfolio is used when ``DPM_OPTIMIZER_ENABLED=true`` and installed;
otherwise a dependency-free hierarchical risk-parity fallback ("hrp-lite")
is used. A proposal never writes a mandate version.
"""

from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.errors import NotFoundError, OptimizerError
from app.core.money import D
from app.models.entities import PriceHistory, Security


def _load_returns(db: Session, symbols: list[str], lookback_days: int) -> tuple[list[str], list[list[float]]]:
    try:
        import numpy as np  # type: ignore
    except Exception as exc:  # pragma: no cover
        raise OptimizerError("numpy is not available.", detail=str(exc)) from exc

    end = date.today()
    start = end - timedelta(days=lookback_days)
    result: dict[str, dict[date, Decimal]] = {}
    for symbol in symbols:
        security = db.execute(select(Security).where(Security.symbol == symbol.upper())).scalar_one_or_none()
        if security is None:
            raise NotFoundError(f"Security '{symbol}' not found.")
        rows = db.execute(
            select(PriceHistory)
            .where(
                PriceHistory.security_id == security.id,
                PriceHistory.price_date >= start,
                PriceHistory.price_date <= end,
            )
            .order_by(PriceHistory.price_date)
        ).scalars()
        series: dict[date, Decimal] = {}
        for row in rows:
            series[row.price_date] = D(row.close)
        result[symbol.upper()] = series

    common: set[date] | None = None
    for series in result.values():
        keys = set(series)
        common = keys if common is None else common & keys
    common_dates: list[date] = sorted(common or [])
    if len(common_dates) < 30:
        raise OptimizerError(
            "Not enough shared price history to optimise.",
            observations=len(common_dates),
            required=30,
        )
    names = list(result)
    matrix = []
    for day in common_dates:
        matrix.append([float(result[name][day]) for name in names])
    prices = np.asarray(matrix, dtype=float)
    returns = prices[1:] / prices[:-1] - 1.0
    mask = np.isfinite(returns).all(axis=1)
    returns = returns[mask]
    if returns.shape[0] < 30:
        raise OptimizerError("Not enough valid return observations.", observations=int(returns.shape[0]))
    return names, returns.tolist()


def _hrp_lite(returns: list[list[float]], names: list[str]) -> dict[str, Decimal]:

    import numpy as np  # type: ignore

    matrix = np.asarray(returns, dtype=float)
    cov = np.cov(matrix, rowvar=False)
    if np.ndim(cov) == 0:
        cov = np.array([[float(cov)]])
    vol = np.sqrt(np.diag(cov))
    vol[vol == 0] = 1e-12
    corr = cov / np.outer(vol, vol)
    np.fill_diagonal(corr, 1.0)
    corr = np.clip(corr, -1.0, 1.0)
    distance = np.sqrt(np.clip(0.5 * (1.0 - corr), 0.0, None))

    # Correlation-distance seriation gives the quasi-diagonal order that HRP
    # bisection expects. Greedy nearest-neighbour keeps this dependency free.
    unvisited = set(range(len(names)))
    ordered: list[int] = []
    current = min(unvisited, key=lambda index: (float(np.sum(distance[index])), names[index]))
    while unvisited:
        ordered.append(current)
        unvisited.discard(current)
        if not unvisited:
            break
        current = min(
            unvisited,
            key=lambda index: (float(distance[current][index]), names[index]),
        )

    def cluster_var(indexes: list[int]) -> float:
        sub = cov[np.ix_(indexes, indexes)]
        inverse = 1.0 / np.diag(sub)
        inverse = inverse / inverse.sum()
        return float(inverse @ sub @ inverse)

    def allocate(indexes: list[int]) -> dict[int, float]:
        if len(indexes) == 1:
            return {indexes[0]: 1.0}
        split = len(indexes) // 2
        left, right = indexes[:split], indexes[split:]
        left_var, right_var = cluster_var(left), cluster_var(right)
        denominator = left_var + right_var
        left_weight = 1.0 - (left_var / denominator) if denominator else 0.5
        weights = {}
        for key, value in allocate(left).items():
            weights[key] = value * left_weight
        for key, value in allocate(right).items():
            weights[key] = value * (1.0 - left_weight)
        return weights

    allocation = allocate(ordered)
    total = sum(allocation.values())
    # Clean tiny numerical noise and round to 8 dp.
    cleaned: dict[str, Decimal] = {}
    for index, weight in allocation.items():
        cleaned[names[index]] = (Decimal(str(weight)) / Decimal(str(total))).quantize(Decimal("0.00000001"))
    drift = Decimal("1") - sum(cleaned.values(), Decimal("0"))
    if cleaned:
        first = next(iter(cleaned))
        cleaned[first] += drift
    return cleaned


def propose_weights(
    db: Session, mandate_id: str, symbols: list[str] | None = None, lookback_days: int = 730
) -> dict:
    if not settings.optimizer_enabled:
        raise OptimizerError(
            "The optimiser is disabled. Set DPM_OPTIMIZER_ENABLED=true to enable it.",
        )
    if mandate_id:
        from app.engines.versioning import current_version
        from app.models.entities import Mandate

        mandate = db.get(Mandate, mandate_id)
        if mandate is None:
            raise NotFoundError(f"Mandate '{mandate_id}' not found.")
        version = current_version(db, mandate_id)
        symbols = symbols or [key for key in (version.weights_json or {}) if key != "CASH"]
        benchmark_id = version.benchmark_id
    else:
        benchmark_id = None
    symbols = [s.upper() for s in (symbols or [])]
    if len(symbols) < 2:
        raise OptimizerError("At least two symbols are required for an optimiser proposal.")

    names, returns = _load_returns(db, symbols, lookback_days)
    method = "hrp-lite"
    weights: dict[str, Decimal] | None = None
    try:
        import numpy as np  # type: ignore
        import pandas as pd  # type: ignore
        import riskfolio as rp  # type: ignore

        frame = pd.DataFrame(np.asarray(returns, dtype=float), columns=names)
        portfolio = rp.HCPortfolio(returns=frame)
        result = portfolio.optimization(
            model="HRP", codependence="pearson", rm="MV", rf=0, linkage="single", max_k=10
        )
        if result is not None:
            weights = {
                str(index).upper(): Decimal(str(result.iloc[i, 0])) for i, index in enumerate(result.index)
            }
            method = "riskfolio-hrp"
    except Exception:
        weights = None
    if weights is None:
        weights = _hrp_lite(returns, names)
    return {
        "mandate_id": mandate_id,
        "method": method,
        "symbols": names,
        "weights": {k: str(v) for k, v in weights.items()},
        "cash_weight": "0",
        "observations": len(returns),
        "lookback_days": lookback_days,
        "benchmark_id": benchmark_id,
        "note": "Proposal only. Saves nothing until you explicitly create a new mandate version.",
    }
