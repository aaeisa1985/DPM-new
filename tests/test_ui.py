"""Test all Web UI routes, templates rendering, and form action endpoints."""

from fastapi.testclient import TestClient


def test_ui_pages_render_ok(api_client: TestClient):
    # 1. Dashboard
    res = api_client.get("/ui/dashboard")
    assert res.status_code == 200
    assert "Portfolio Overview" in res.text
    assert "theme-toggle-btn" in res.text

    # 2. Clients & Portfolios
    res = api_client.get("/ui/clients")
    assert res.status_code == 200
    assert "Create New Portfolio / Account" in res.text
    assert "Register New Client" in res.text

    # 3. Mandates & Drift
    res = api_client.get("/ui/mandates")
    assert res.status_code == 200
    assert "Investment Mandates" in res.text

    # 4. Rebalance
    res = api_client.get("/ui/rebalance")
    assert res.status_code == 200
    assert "Rebalance Plans" in res.text

    # 5. Data & Market
    res = api_client.get("/ui/data")
    assert res.status_code == 200
    assert "Sync Live Daily Market Prices" in res.text
    assert "CSV Data Importer" in res.text

    # 6. Audit
    res = api_client.get("/ui/audit")
    assert res.status_code == 200
    assert "Cryptographic Audit Trail" in res.text


def test_ui_create_client_and_portfolio_flow(api_client: TestClient):
    # Create client via UI action
    res = api_client.post(
        "/ui/actions/client",
        data={"code": "UI-CLI-1", "display_name": "UI Family Office", "base_currency": "AED"},
        follow_redirects=False,
    )
    assert res.status_code == 303
    assert res.headers["location"] == "/ui/clients"

    # Get client id from client directory
    c_res = api_client.get("/clients")
    assert c_res.status_code == 200
    client_obj = next(c for c in c_res.json() if c["code"] == "UI-CLI-1")
    client_id = client_obj["id"]

    # Create portfolio / account via UI action
    acc_res = api_client.post(
        "/ui/actions/account",
        data={
            "client_id": client_id,
            "account_code": "UI-ACC-101",
            "currency": "AED",
            "opening_cash": "250000.00",
            "mandate_name": "UAE Bluechip Strategy",
            "target_cash_pct": "15",
            "redirect_to": "/ui/clients",
        },
        follow_redirects=False,
    )
    assert acc_res.status_code == 303

    # Verify client page lists both client and account
    ui_res = api_client.get("/ui/clients")
    assert ui_res.status_code == 200
    assert "UI-CLI-1" in ui_res.text
    assert "UI-ACC-101" in ui_res.text
    assert "UAE Bluechip Strategy" in ui_res.text

    # Verify client detail page renders correctly with portfolio and cash
    detail_res = api_client.get(f"/ui/client/{client_id}")
    assert detail_res.status_code == 200
    assert "UI Family Office" in detail_res.text
    assert "UI-ACC-101" in detail_res.text
    assert "250000.00" in detail_res.text

    # Post a cash deposit flow via UI action
    flow_res = api_client.post(
        "/ui/actions/flow",
        data={
            "account_code": "UI-ACC-101",
            "flow_type": "deposit",
            "amount": "50000.00",
            "external_ref": "WIRE-TEST-001",
            "redirect_to": f"/ui/client/{client_id}",
        },
        follow_redirects=False,
    )
    assert flow_res.status_code == 303

    # Check updated cash in client detail
    detail_after_res = api_client.get(f"/ui/client/{client_id}")
    assert detail_after_res.status_code == 200
    assert "300000.00" in detail_after_res.text
