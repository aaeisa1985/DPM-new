"""Tests for local security middleware (spec 8 & 9.1)."""

from fastapi.testclient import TestClient

from app.core.config import settings


def test_untrusted_host_rejected(api_client: TestClient):
    response = api_client.get(
        "/health",
        headers={"Host": "malicious.attacker.com"},
    )
    assert response.status_code == 403
    data = response.json()
    assert data["error"]["code"] == "host_rejected"


def test_untrusted_origin_on_write_rejected(api_client: TestClient):
    response = api_client.post(
        "/clients",
        json={"code": "EVIL", "display_name": "Evil Client", "base_currency": "USD"},
        headers={"Origin": "https://evil-site.com"},
    )
    assert response.status_code == 403
    data = response.json()
    assert data["error"]["code"] == "origin_rejected"


def test_trusted_origin_on_write_accepted(api_client: TestClient):
    response = api_client.post(
        "/clients",
        json={"code": "SEC-01", "display_name": "Secure Client", "base_currency": "USD"},
        headers={"Origin": "http://127.0.0.1:8000"},
    )
    assert response.status_code == 201


def test_local_token_enforcement(api_client: TestClient):
    old_token = settings.local_token
    settings.local_token = "secret123"
    try:
        # Write request without token -> 401
        res1 = api_client.post(
            "/clients",
            json={"code": "NO-TOKEN", "display_name": "No Token Client", "base_currency": "USD"},
        )
        assert res1.status_code == 401

        # Write request with wrong token -> 401
        res2 = api_client.post(
            "/clients",
            json={"code": "BAD-TOKEN", "display_name": "Bad Token Client", "base_currency": "USD"},
            headers={"X-DPM-Token": "wrong"},
        )
        assert res2.status_code == 401

        # Write request with correct token -> 201
        res3 = api_client.post(
            "/clients",
            json={"code": "GOOD-TOKEN", "display_name": "Good Token Client", "base_currency": "USD"},
            headers={"X-DPM-Token": "secret123"},
        )
        assert res3.status_code == 201
    finally:
        settings.local_token = old_token
