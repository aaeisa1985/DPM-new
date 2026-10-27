"""Shared pytest fixtures."""

from __future__ import annotations

from collections.abc import Generator
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.db import get_session, init_db
from app.main import app
from app.models.entities import Account, Client, Mandate, MandateVersion, Security


@pytest.fixture
def test_db(tmp_path: Path) -> Generator[Session, None, None]:
    db_file = tmp_path / "test_dpm.db"
    old_db = settings.db_path
    settings.db_path = str(db_file)
    init_db(db_file)
    with get_session() as session:
        yield session
    settings.db_path = old_db


@pytest.fixture
def client_env(test_db: Session) -> dict:
    """Standard environment: Client, Mandate, MandateVersion, Account, Security."""
    db = test_db
    c = Client(code="CLI-001", display_name="Alpha Client", base_currency="USD")
    db.add(c)
    db.flush()

    m = Mandate(client_id=c.id, name="Growth Mandate", status="active")
    db.add(m)
    db.flush()

    mv = MandateVersion(
        mandate_id=m.id,
        version=1,
        effective_from=date(2026, 1, 1),
        drift_tolerance=Decimal("0.05"),
        lot_method="fifo",
        weights_json={"CASH": "0.10", "SPY": "0.50", "QQQ": "0.40"},
    )
    db.add(mv)

    acc = Account(
        account_code="ACC-001",
        client_id=c.id,
        mandate_id=m.id,
        currency="USD",
        opening_cash=Decimal("1000.00"),
    )
    db.add(acc)

    s1 = Security(
        symbol="SPY",
        name="SPDR S&P 500 ETF",
        asset_class="equity",
        currency="USD",
        price_source="manual",
        settlement_days=1,
    )
    s2 = Security(
        symbol="QQQ",
        name="Invesco QQQ Trust",
        asset_class="equity",
        currency="USD",
        price_source="manual",
        settlement_days=1,
    )
    db.add_all([s1, s2])
    db.commit()

    return {
        "client": c,
        "mandate": m,
        "version": mv,
        "account": acc,
        "spy": s1,
        "qqq": s2,
    }


@pytest.fixture
def api_client(test_db: Session) -> TestClient:
    return TestClient(app, base_url="http://127.0.0.1:8000")
