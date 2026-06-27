"""Tests for GET /caliber/me — current-user identity for the SPA."""

from __future__ import annotations

from starlette.testclient import TestClient

PREFIX = "/ajax-api/2.0/mlflow/caliber"


def test_me_returns_admin_identity_for_default_user(client: TestClient) -> None:
    resp = client.get(f"{PREFIX}/me")
    assert resp.status_code == 200
    data = resp.json()["data"]
    assert data["user_id"] == "@test"
    assert data["is_admin"] is True
    assert "caliber.admin" in data["scopes"]
    # scopes are returned sorted for a stable response shape.
    assert data["scopes"] == sorted(data["scopes"])


def test_me_returns_viewer_only_for_non_admin_user(client: TestClient) -> None:
    resp = client.get(f"{PREFIX}/me", headers={"X-CALIBER-User": "@viewer"})
    assert resp.status_code == 200
    data = resp.json()["data"]
    assert data["user_id"] == "@viewer"
    assert data["is_admin"] is False
    assert data["scopes"] == ["caliber.viewer"]


def test_me_anonymous_has_no_scopes(client: TestClient) -> None:
    resp = client.get(f"{PREFIX}/me", headers={"X-CALIBER-User": ""})
    assert resp.status_code == 200
    data = resp.json()["data"]
    assert data["user_id"] == "anonymous"
    assert data["scopes"] == []
    assert data["is_admin"] is False
