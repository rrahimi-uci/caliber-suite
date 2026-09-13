"""Tests for the metadata-only platform Admin inventory (`P1-F`,
``routes/platform_admin_inventory.py``).

Names only who holds each config-driven global scope -- no project or
resource content is ever reachable through this route.
"""

from __future__ import annotations

from starlette.testclient import TestClient

PATH = "/ajax-api/2.0/mlflow/caliber/admin/platform-admins"


def test_approver_and_operator_lists_default_to_empty(client: TestClient) -> None:
    """The `client` fixture's `app_config` grants `@test` admin but leaves
    `approver_users`/`operator_users` at their empty-string default."""
    resp = client.get(PATH)
    assert resp.status_code == 200, resp.text
    data = resp.json()["data"]
    assert data["approver_users"] == []
    assert data["operator_users"] == []


def test_returns_all_three_configured_lists(client: TestClient) -> None:
    # `@test` (the fixture's default caller) stays in `admin_users` -- this
    # test is about the response shape, not about self-demoting the caller.
    client.app.state.config = client.app.state.config.model_copy(
        update={
            "admin_users": "@test, @alice, @bob",
            "approver_users": "@carol",
            "operator_users": "@dave, @erin",
        }
    )
    resp = client.get(PATH)
    assert resp.status_code == 200, resp.text
    data = resp.json()["data"]
    assert data["admin_users"] == ["@alice", "@bob", "@test"]
    assert data["approver_users"] == ["@carol"]
    assert data["operator_users"] == ["@dave", "@erin"]


def test_requires_admin_scope(client: TestClient) -> None:
    resp = client.get(PATH, headers={"X-CALIBER-User": "@viewer-only"})
    assert resp.status_code == 403


def test_operator_scope_alone_is_not_enough(client: TestClient) -> None:
    client.app.state.config = client.app.state.config.model_copy(
        update={"operator_users": "@op-only"}
    )
    resp = client.get(PATH, headers={"X-CALIBER-User": "@op-only"})
    assert resp.status_code == 403


def test_unauthenticated_is_401(client: TestClient) -> None:
    resp = client.get(PATH, headers={"X-CALIBER-User": ""})
    assert resp.status_code == 401
