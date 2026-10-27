"""Tests for REST API endpoints and error format (spec 6 & 9.1)."""

from fastapi.testclient import TestClient


def test_unified_error_format_on_not_found(api_client: TestClient):
    response = api_client.get("/clients/non-existent-uuid/positions")
    assert response.status_code == 404
    data = response.json()
    assert "error" in data
    assert data["error"]["code"] == "not_found"
    assert "detail" in data["error"]


def test_string_money_and_3_dp_currency_formatting(api_client: TestClient, client_env: dict):
    # Create a client with base_currency = KWD (3 decimal places)
    res = api_client.post(
        "/clients",
        json={"code": "KWD-CLI", "display_name": "Kuwait Client", "base_currency": "KWD"},
    )
    assert res.status_code == 201
    client_data = res.json()
    client_id = client_data["id"]

    # Create mandate
    m_res = api_client.post(
        "/mandates",
        json={"client_id": client_id, "name": "Kuwait Mandate", "status": "active"},
    )
    assert m_res.status_code == 201
    mandate_id = m_res.json()["id"]

    # Create account with opening cash 500.5
    acc_res = api_client.post(
        "/accounts",
        json={
            "account_code": "ACC-KWD-1",
            "client_id": client_id,
            "mandate_id": mandate_id,
            "currency": "KWD",
            "opening_cash": "500.5",
        },
    )
    assert acc_res.status_code == 201
    acc_data = acc_res.json()

    # Cash must be a string formatted to 3 decimal places: "500.500"
    assert isinstance(acc_data["opening_cash"], str)
    assert acc_data["opening_cash"] == "500.500"


def test_idempotent_duplicate_transaction_rejected(api_client: TestClient, client_env: dict):
    tx_payload = {
        "account_code": "ACC-001",
        "date": "2026-01-10",
        "side": "BUY",
        "symbol": "SPY",
        "quantity": "10",
        "unit_price": "100.00",
        "fee": "5.00",
        "sequence": 1,
    }

    # First attempt: succeeds
    res1 = api_client.post("/transactions", json=tx_payload)
    assert res1.status_code == 201
    tx_id = res1.json()["id"]

    # Second identical attempt: fails with 409 duplicate_transaction
    res2 = api_client.post("/transactions", json=tx_payload)
    assert res2.status_code == 409
    err = res2.json()["error"]
    assert err["code"] == "duplicate_transaction"
    assert tx_id in err["detail"]


def test_health_endpoint(api_client: TestClient):
    response = api_client.get("/health")
    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "ok"
    assert data["schema_version"] == "1.1.0"
    assert "timestamp" in data


def test_full_api_and_ui_workflow(api_client: TestClient, client_env: dict):
    client_id = client_env["client"].id
    mandate_id = client_env["mandate"].id
    account_id = client_env["account"].id

    # 1. Bootstrap GET/POST endpoints
    assert api_client.get("/clients").status_code == 200
    assert api_client.get("/mandates").status_code == 200
    assert api_client.get("/securities").status_code == 200
    assert api_client.get("/accounts").status_code == 200

    sec_res = api_client.post(
        "/securities",
        json={"symbol": "AGG", "name": "US Aggregate Bond", "asset_class": "Bond", "currency": "USD"},
    )
    assert sec_res.status_code == 201

    v_res = api_client.post(
        f"/mandates/{mandate_id}/versions",
        json={
            "effective_from": "2026-01-01",
            "drift_tolerance": "0.05",
            "lot_method": "fifo",
            "weights": {"CASH": "0.10", "SPY": "0.50", "QQQ": "0.40"},
        },
    )
    assert v_res.status_code == 201

    assert (
        api_client.post(
            "/prices/manual",
            json={"symbol": "SPY", "date": "2026-01-10", "close": "500.00"},
        ).status_code
        == 201
    )
    assert (
        api_client.post(
            "/prices/manual",
            json={"symbol": "QQQ", "date": "2026-01-10", "close": "300.00"},
        ).status_code
        == 201
    )
    assert (
        api_client.post(
            "/fx",
            json={"pair": "EURUSD", "date": "2026-01-10", "rate": "1.08"},
        ).status_code
        == 201
    )
    assert (
        api_client.post(
            f"/accounts/{account_id}/flows",
            json={"flow_date": "2026-01-09", "type": "deposit", "amount": "5000.00"},
        ).status_code
        == 201
    )

    # 2. Book endpoints: transactions, dividends, splits, positions, void
    tx_res = api_client.post(
        "/transactions",
        json={
            "account_code": "ACC-001",
            "date": "2026-01-10",
            "side": "BUY",
            "symbol": "SPY",
            "quantity": "10",
            "unit_price": "500.00",
            "fee": "5.00",
        },
    )
    assert tx_res.status_code == 201
    tx_id = tx_res.json()["id"]

    div_res = api_client.post(
        f"/accounts/{account_id}/dividends",
        json={"date": "2026-01-10", "security_id": client_env["spy"].id, "per_unit": "1.00"},
    )
    assert div_res.status_code == 201

    split_res = api_client.post(
        "/actions/splits",
        json={"symbol": "AGG", "date": "2026-01-10", "ratio": "2"},
    )
    assert split_res.status_code == 201

    pos_res = api_client.get(f"/clients/{client_id}/positions?as_of=2026-01-10")
    assert pos_res.status_code == 200
    assert pos_res.json()["client_id"] == client_id

    # 3. Performance, Drift, Rebalance Preview & Confirm, Fill & Cancel
    perf_res = api_client.get(f"/clients/{client_id}/performance?start=2026-01-09&end=2026-01-10")
    assert perf_res.status_code == 200
    assert "twr" in perf_res.json()

    drift_res = api_client.get(f"/mandates/{mandate_id}/drift?as_of=2026-01-10")
    assert drift_res.status_code == 200

    prev_res = api_client.get(f"/mandates/{mandate_id}/rebalance/preview?as_of=2026-01-10")
    assert prev_res.status_code == 200

    conf_res = api_client.post(f"/mandates/{mandate_id}/rebalance/confirm?as_of=2026-01-10")
    assert conf_res.status_code == 201
    plan_data = conf_res.json()
    plan_id = plan_data["plan_id"]
    orders = plan_data["orders"]
    assert len(orders) >= 1

    # Partial fill first order, then cancel remaining or second order
    oid = orders[0]["order_id"]
    fill_res = api_client.post(
        f"/plans/{plan_id}/orders/{oid}/fill?fill_date=2026-01-10",
        json={"quantity": "1", "price": "300.00", "fee": "1.00"},
    )
    assert fill_res.status_code == 200

    if len(orders) > 1:
        oid2 = orders[1]["order_id"]
        cancel_res = api_client.post(
            f"/plans/{plan_id}/orders/{oid2}/cancel",
            json={"reason": "Operator skipped"},
        )
        assert cancel_res.status_code == 200

    assert api_client.get("/plans").status_code == 200

    # Void the initial transaction after plan tests
    void_res = api_client.post(f"/transactions/{tx_id}/void", json={"reason": "Test void"})
    assert void_res.status_code == 200

    # 4. CSV import dry_run, Backup export, and Audit endpoint
    imp_res = api_client.post(
        "/import/fx?dry_run=true",
        json={"csv_content": "date,pair,rate\n2026-01-11,AEDUSD,0.272294\n", "dry_run": True},
    )
    assert imp_res.status_code == 200
    assert imp_res.json()["dry_run"] is True

    bkp_res = api_client.get("/export/backup?download=true")
    assert bkp_res.status_code == 200
    assert bkp_res.headers["content-type"] == "application/zip"

    audit_res = api_client.get("/audit")
    assert audit_res.status_code == 200
    assert len(audit_res.json()) >= 1
    verify_res = api_client.get("/audit/verify")
    assert verify_res.status_code == 200
    assert verify_res.json()["valid"] is True

    # 5. UI routes
    for page in ("dashboard", "clients", "mandates", "rebalance", "data", "audit"):
        ui_res = api_client.get(f"/ui/{page}")
        assert ui_res.status_code == 200
