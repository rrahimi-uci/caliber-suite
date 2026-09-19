"""``ProjectSourceConnectionAPI`` -- a project's encrypted GitHub App
connection for its source binding (`P4-E`).

Mirrors ``test_resources_workspace_lifecycle.py``'s conventions for
``ProjectSourceAPI`` (the offline ``httpx.MockTransport`` stub pattern this
package's whole test suite uses). Pins three things ``routes/
workspace_source_connections.py`` itself guarantees:

* ``get``/``configure``/``revoke`` all decode the shared
  ``{"connection": {...} | None}`` envelope, including the unconfigured
  (``None``) case.
* ``configure`` sends every field the server's
  ``WorkspaceSourceConnectionConfigureRequest`` accepts, including the
  write-only secret fields (never read back -- there is no field for either
  on :class:`WorkspaceSourceConnection`).
* Every call is project-scoped (``X-CALIBER-Project``), and each of the four
  routes hits its own distinct, literal path -- the same property the SDK
  coverage gate (``docs-site/sdk_coverage.py``) statically scans for.
"""

from __future__ import annotations

import json as jsonlib
from typing import Any

import httpx

from caliber_sdk import CaliberClient
from caliber_sdk.models.workspace import (
    WorkspaceSourceConnection,
    WorkspaceSourceReconciliationResult,
)

BASE = "https://caliber.test"

_CONNECTION: dict[str, Any] = {
    "connection_id": "WSC-1",
    "source_id": "SRC-1",
    "project_id": "PRJ-1",
    "provider": "github",
    "app_id": "123456",
    "installation_id": "789012",
    "status": "active",
    "created_at": "2026-01-01T00:00:00Z",
    "updated_at": "2026-01-01T00:00:00Z",
}


def client_with(handler: Any) -> CaliberClient:
    http = httpx.Client(transport=httpx.MockTransport(handler))
    return CaliberClient(BASE, token="calpat_test", http_client=http)


def envelope(data: Any, *, status_code: int = 200) -> httpx.Response:
    return httpx.Response(status_code, json={"data": data})


# --- get ----------------------------------------------------------------


def test_get_decodes_a_configured_connection() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "GET"
        assert request.url.path.endswith("/projects/PRJ-1/source/connection")
        assert request.headers.get("X-CALIBER-Project") == "PRJ-1"
        return envelope({"connection": _CONNECTION})

    with client_with(handler) as caliber:
        connection = caliber.workspaces.source_connection.get("PRJ-1")

    assert connection == WorkspaceSourceConnection(
        connection_id="WSC-1",
        source_id="SRC-1",
        project_id="PRJ-1",
        provider="github",
        app_id="123456",
        installation_id="789012",
        status="active",
        created_at="2026-01-01T00:00:00Z",
        updated_at="2026-01-01T00:00:00Z",
    )


def test_get_with_no_connection_configured_decodes_none() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return envelope({"connection": None})

    with client_with(handler) as caliber:
        connection = caliber.workspaces.source_connection.get("PRJ-1")

    assert connection is None


# --- configure ------------------------------------------------------------


def test_configure_sends_every_field_and_decodes_the_response() -> None:
    seen: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["method"] = request.method
        seen["path"] = request.url.path.rsplit("/caliber", 1)[-1]
        seen["project_header"] = request.headers.get("X-CALIBER-Project")
        seen["body"] = jsonlib.loads(request.content)
        return envelope({"connection": _CONNECTION}, status_code=201)

    with client_with(handler) as caliber:
        connection = caliber.workspaces.source_connection.configure(
            "PRJ-1",
            app_id="123456",
            installation_id="789012",
            private_key="-----BEGIN PRIVATE KEY-----\nfake\n-----END PRIVATE KEY-----",
            webhook_secret="whsec_test",
        )

    assert seen["method"] == "PUT"
    assert seen["path"] == "/projects/PRJ-1/source/connection"
    assert seen["project_header"] == "PRJ-1"
    assert seen["body"] == {
        "provider": "github",
        "app_id": "123456",
        "installation_id": "789012",
        "private_key": "-----BEGIN PRIVATE KEY-----\nfake\n-----END PRIVATE KEY-----",
        "webhook_secret": "whsec_test",
    }
    assert connection is not None
    assert connection.connection_id == "WSC-1"
    # The response is a secret-free projection: it round-trips no field that
    # could carry the private key or webhook secret back.
    assert not hasattr(connection, "private_key")
    assert not hasattr(connection, "webhook_secret")


def test_configure_defaults_provider_to_github() -> None:
    seen: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["body"] = jsonlib.loads(request.content)
        return envelope({"connection": _CONNECTION}, status_code=201)

    with client_with(handler) as caliber:
        caliber.workspaces.source_connection.configure(
            "PRJ-1",
            app_id="123456",
            installation_id="789012",
            private_key="pk",
            webhook_secret="whsec",
        )

    assert seen["body"]["provider"] == "github"


# --- revoke -----------------------------------------------------------------


def test_revoke_posts_to_the_revoke_action_path() -> None:
    seen: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["method"] = request.method
        seen["path"] = request.url.path.rsplit("/caliber", 1)[-1]
        revoked = dict(_CONNECTION, status="revoked")
        return envelope({"connection": revoked})

    with client_with(handler) as caliber:
        connection = caliber.workspaces.source_connection.revoke("PRJ-1")

    assert seen["method"] == "POST"
    assert seen["path"] == "/projects/PRJ-1/source/connection:revoke"
    assert connection is not None
    assert connection.status == "revoked"


# --- reconcile_deliveries ----------------------------------------------------


def test_reconcile_deliveries_posts_to_its_own_action_path_and_decodes_the_result() -> None:
    seen: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["method"] = request.method
        seen["path"] = request.url.path.rsplit("/caliber", 1)[-1]
        return envelope(
            {
                "checked": 12,
                "missed_delivery_ids": ["d-1", "d-2"],
                "redelivery_requested_ids": ["d-1"],
            }
        )

    with client_with(handler) as caliber:
        result = caliber.workspaces.source_connection.reconcile_deliveries("PRJ-1")

    assert seen["method"] == "POST"
    assert seen["path"] == "/projects/PRJ-1/source/connection:reconcile-deliveries"
    assert result == WorkspaceSourceReconciliationResult(
        checked=12,
        missed_delivery_ids=["d-1", "d-2"],
        redelivery_requested_ids=["d-1"],
    )


def test_reconcile_deliveries_with_nothing_missed_decodes_empty_lists() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return envelope({"checked": 3, "missed_delivery_ids": [], "redelivery_requested_ids": []})

    with client_with(handler) as caliber:
        result = caliber.workspaces.source_connection.reconcile_deliveries("PRJ-1")

    assert result.checked == 3
    assert result.missed_delivery_ids == []
    assert result.redelivery_requested_ids == []


# --- the four routes are distinct -------------------------------------------


def test_the_four_operations_hit_four_distinct_method_path_pairs() -> None:
    """A regression guard against the classic copy-paste mistake of reusing
    one (method, path) pair for two different operations -- which the
    coverage gate's static scan would happily count as "covered" even though
    the server serves a distinct route. ``get``/``configure`` legitimately
    share one path (``GET``/``PUT /source/connection``) and differ only by
    verb; ``revoke``/``reconcile_deliveries`` each get their own literal
    ``:action`` path."""
    seen: list[tuple[str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append((request.method, request.url.path.rsplit("/caliber", 1)[-1]))
        if request.method == "GET":
            return envelope({"connection": _CONNECTION})
        if request.method == "PUT":
            return envelope({"connection": _CONNECTION}, status_code=201)
        if seen[-1][1].endswith(":revoke"):
            return envelope({"connection": dict(_CONNECTION, status="revoked")})
        return envelope({"checked": 0, "missed_delivery_ids": [], "redelivery_requested_ids": []})

    with client_with(handler) as caliber:
        api = caliber.workspaces.source_connection
        api.get("PRJ-1")
        api.configure(
            "PRJ-1",
            app_id="1",
            installation_id="2",
            private_key="pk",
            webhook_secret="whsec",
        )
        api.revoke("PRJ-1")
        api.reconcile_deliveries("PRJ-1")

    assert seen == [
        ("GET", "/projects/PRJ-1/source/connection"),
        ("PUT", "/projects/PRJ-1/source/connection"),
        ("POST", "/projects/PRJ-1/source/connection:revoke"),
        ("POST", "/projects/PRJ-1/source/connection:reconcile-deliveries"),
    ]
    assert len(set(seen)) == 4
