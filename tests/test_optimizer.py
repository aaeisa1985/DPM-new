"""Tests for portfolio optimizer adapter (spec 5.6 & 9.1)."""

from datetime import date, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.errors import OptimizerError
from app.engines.optimizer import propose_weights
from app.engines.pricing import record_manual_price
from app.models.entities import MandateVersion


def test_optimizer_disabled_by_default(test_db: Session, client_env: dict):
    db = test_db
    mandate = client_env["mandate"]

    old_setting = settings.optimizer_enabled
    settings.optimizer_enabled = False
    try:
        with pytest.raises(OptimizerError) as exc_info:
            propose_weights(db, mandate_id=mandate.id)
        assert "optimiser is disabled" in exc_info.value.detail
    finally:
        settings.optimizer_enabled = old_setting


def test_optimizer_proposal_does_not_mutate_mandate_versions(test_db: Session, client_env: dict):
    db = test_db
    mandate = client_env["mandate"]
    spy = client_env["spy"]
    qqq = client_env["qqq"]

    old_setting = settings.optimizer_enabled
    settings.optimizer_enabled = True
    try:
        # Populate 60 days of synthetic prices for SPY and QQQ
        end_date = date.today()
        for i in range(60):
            d = end_date - timedelta(days=60 - i)
            p_spy = Decimal("400") + Decimal(str(i * 0.5))
            p_qqq = Decimal("300") + Decimal(str(i * 0.4))
            record_manual_price(db, spy, d, p_spy)
            record_manual_price(db, qqq, d, p_qqq)
        db.commit()

        initial_versions = (
            db.execute(select(MandateVersion).where(MandateVersion.mandate_id == mandate.id)).scalars().all()
        )
        initial_count = len(initial_versions)

        proposal = propose_weights(db, mandate_id=mandate.id, symbols=["SPY", "QQQ"])

        assert "weights" in proposal
        assert "SPY" in proposal["weights"]
        assert "QQQ" in proposal["weights"]

        # Crucial check: mandate versions in database must NOT be modified!
        post_versions = (
            db.execute(select(MandateVersion).where(MandateVersion.mandate_id == mandate.id)).scalars().all()
        )
        assert len(post_versions) == initial_count
    finally:
        settings.optimizer_enabled = old_setting


def test_optimizer_hrp_lite_fallback_and_error_isolation(test_db: Session, client_env: dict, monkeypatch):
    import sys

    from app.core.errors import NotFoundError
    from app.engines.optimizer import _hrp_lite
    from app.models.entities import Security

    db = test_db
    spy = client_env["spy"]
    qqq = client_env["qqq"]
    bnd = Security(symbol="BND", name="Bond ETF", asset_class="Bond", currency="USD", price_source="manual")
    db.add(bnd)
    db.commit()

    old_setting = settings.optimizer_enabled
    settings.optimizer_enabled = True
    try:
        # 1. Error: mandate not found
        with pytest.raises(NotFoundError):
            propose_weights(db, mandate_id="missing-mandate")

        # 2. Error: fewer than 2 symbols
        with pytest.raises(OptimizerError, match="At least two symbols"):
            propose_weights(db, mandate_id="", symbols=["SPY"])

        # 3. Error: symbol not found
        with pytest.raises(NotFoundError):
            propose_weights(db, mandate_id="", symbols=["SPY", "NONEXISTENT"])

        # 4. Error: insufficient price history (< 30 days)
        with pytest.raises(OptimizerError, match="Not enough shared price history"):
            propose_weights(db, mandate_id="", symbols=["SPY", "QQQ"])

        # 5. Populate 45 days of prices for 3 symbols (SPY, QQQ, BND) so _hrp_lite bisection
        # exercises cluster_var and multi-asset allocation when riskfolio is disabled/unavailable
        end_date = date.today()
        for i in range(45):
            d = end_date - timedelta(days=45 - i)
            record_manual_price(db, spy, d, Decimal("400") + Decimal(str((i % 5) * 1.2 + i * 0.3)))
            record_manual_price(db, qqq, d, Decimal("300") + Decimal(str((i % 7) * 1.5 + i * 0.2)))
            record_manual_price(db, bnd, d, Decimal("80") + Decimal(str((i % 3) * 0.4 + i * 0.05)))
        db.commit()

        # Force riskfolio import to fail so _hrp_lite fallback is guaranteed to run
        monkeypatch.setitem(sys.modules, "riskfolio", None)
        proposal = propose_weights(db, mandate_id="", symbols=["SPY", "QQQ", "BND"])
        assert proposal["method"] == "hrp-lite"
        total_w = sum(Decimal(v) for v in proposal["weights"].values())
        assert total_w == Decimal("1.00000000")

        # Also test 1-asset edge case in _hrp_lite directly (cov 0-dim branch)
        single = _hrp_lite([[0.01], [-0.01], [0.02]], ["SPY"])
        assert single["SPY"] == Decimal("1.00000000")
    finally:
        settings.optimizer_enabled = old_setting
