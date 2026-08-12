"""Route tests for governed OpenAPI integration drafts."""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Any

import httpx
import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session
from starlette.testclient import TestClient

from caliber.config import CaliberConfig
from caliber.db.models import (
    CaliberAuditLog,
    CaliberOpenApiIntegration,
    CaliberOpenApiToolDraft,
    CaliberOpenApiIntegrationVersion,
    CaliberOpenApiOperation,
    CaliberToolRegistry,
)
from caliber.integrations.openapi import executor as executor_module
from tests.workflow_helpers import create_draft, create_workflow, make_manifest

PREFIX = "/ajax-api/2.0/mlflow/caliber"
BASE = f"{PREFIX}/openapi-integrations"

OPENAPI_SPEC = """
openapi: 3.0.3
info:
  title: Ticket API
  version: "2026-08-11"
  description: Manage tickets.
servers:
  - url: https://tickets.example.com
components:
  securitySchemes:
    bearerAuth:
      type: http
      scheme: bearer
security:
  - bearerAuth: []
paths:
  /tickets:
    get:
      operationId: listTickets
      summary: List tickets
      tags: [tickets]
      responses:
        "200":
          description: ok
    post:
      operationId: createTicket
      summary: Create ticket
      tags: [tickets]
      requestBody:
        required: true
        content:
          application/json:
            schema:
              type: object
              properties:
                title:
                  type: string
              required: [title]
      responses:
        "201":
          description: created
  /tickets/{ticket_id}:
    get:
      operationId: getTicket
      summary: Get one ticket
      tags: [tickets]
      parameters:
        - in: path
          name: ticket_id
          required: true
          schema:
            type: string
      responses:
        "200":
          description: ok
"""


def _create_integration(client: TestClient, **payload: object) -> dict[str, object]:
    body = {"name": "Ticketing", "description": "External ticket API"}
    body.update(payload)
    response = client.post(BASE, json=body)
    assert response.status_code == 201, response.text
    return response.json()["data"]


def _import_version(
    client: TestClient,
    integration_id: str,
    *,
    spec_text: str = OPENAPI_SPEC,
    source_ref: str = "inline://ticketing",
) -> dict[str, object]:
    response = client.post(
        f"{BASE}/{integration_id}/import",
        json={"source_kind": "inline_text", "source_ref": source_ref, "spec_text": spec_text},
    )
    assert response.status_code == 201, response.text
    return response.json()["data"]


@pytest.fixture
def app_config(app_config: CaliberConfig) -> CaliberConfig:
    """Allowlist the fake upstream so egress policy stays *on* for these tests.

    ``tickets.example.com`` does not resolve, and the executor refuses a host it
    cannot vet before it opens a socket. Overriding the config the way an operator
    would — rather than stubbing the guarded client away — is what makes these
    tests able to fail when the egress wiring regresses.
    """

    return app_config.model_copy(
        update={
            "egress_allowed_hosts": "tickets.example.com,127.0.0.1",
            "egress_allow_unresolvable_hosts": True,
        }
    )


def _mock_http(monkeypatch, handler) -> None:
    """Replace the transport, not the guarded client factory.

    Passing the real ``build_client`` through means the ``policy=`` contract, the
    pre-check, and the header redaction all still run; only the socket is faked.
    Replacing ``build_client`` itself (the previous approach) hid a missing
    required argument behind a green suite.
    """

    real_build_client = executor_module.build_client

    def _patched(*args, **kwargs):
        client = real_build_client(*args, **kwargs)
        client._transport = httpx.MockTransport(handler)
        return client

    monkeypatch.setattr("caliber.integrations.openapi.executor.build_client", _patched)


class _SpecHandler(BaseHTTPRequestHandler):
    """Serves whatever ``server.set_body(...)`` last set, over real loopback HTTP."""

    def do_GET(self) -> None:  # noqa: N802
        payload = self.server.body.encode("utf-8")  # type: ignore[attr-defined]
        self.send_response(200)
        self.send_header("Content-Type", "application/yaml")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def do_HEAD(self) -> None:  # noqa: N802
        self.send_response(200)
        self.send_header("Content-Type", "application/yaml")
        self.end_headers()

    def log_message(self, *args: Any) -> None:
        """Keep test output clean."""


class _SpecServer(HTTPServer):
    body: str = ""

    def set_body(self, body: str) -> None:
        self.body = body


@pytest.fixture
def openapi_spec_server_factory():
    """Factory for a real loopback HTTP server serving one OpenAPI spec.

    Real sockets, not a mocked transport: an import-from-URL route that drops
    its egress policy must fail here rather than pass behind a stub.
    """

    servers: list[_SpecServer] = []

    def _make(body: str) -> tuple[_SpecServer, str]:
        server = _SpecServer(("127.0.0.1", 0), _SpecHandler)
        server.set_body(body)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        servers.append(server)
        host, port = server.server_address[0], server.server_address[1]
        return server, f"http://{host}:{port}/openapi.yaml"

    yield _make

    for server in servers:
        server.shutdown()
        server.server_close()


@pytest.fixture
def openapi_spec_server(openapi_spec_server_factory) -> str:
    _server, url = openapi_spec_server_factory(OPENAPI_SPEC)
    return url


def test_create_openapi_integration_defaults_to_draft_user_visibility(client: TestClient) -> None:
    data = _create_integration(client)
    assert data["integration_id"].startswith("OAI-")
    assert data["status"] == "draft"
    assert data["visibility"] == "user"
    assert data["project_id"] is None


def test_create_openapi_integration_uses_active_project_when_present(client: TestClient) -> None:
    response = client.post(
        BASE,
        json={"name": "Ticketing", "description": "External ticket API"},
        headers={"X-CALIBER-Project": "PRJ-42"},
    )
    assert response.status_code == 201, response.text
    data = response.json()["data"]
    assert data["visibility"] == "project"
    assert data["project_id"] == "PRJ-42"


def test_import_openapi_version_persists_normalized_version_and_operations(
    client: TestClient,
    db_session: Session,
) -> None:
    integration = _create_integration(client)

    imported = _import_version(client, str(integration["integration_id"]))
    assert imported["version_id"].startswith("OAIV-")
    assert imported["operation_count"] == 3
    assert imported["title"] == "Ticket API"
    assert imported["auth_schemes"] == ["bearerAuth"]
    assert imported["server_urls"] == ["https://tickets.example.com"]
    assert imported["import_warnings"] == []

    row = db_session.get(CaliberOpenApiIntegration, integration["integration_id"])
    assert row is not None
    assert row.status == "review"
    assert row.last_imported_version_id == imported["version_id"]

    version = db_session.get(CaliberOpenApiIntegrationVersion, imported["version_id"])
    assert version is not None
    assert version.spec_sha256
    assert version.raw_document["info"]["title"] == "Ticket API"

    operations = (
        db_session.execute(
            select(CaliberOpenApiOperation).where(
                CaliberOpenApiOperation.integration_version_id == imported["version_id"]
            )
        )
        .scalars()
        .all()
    )
    assert len(operations) == 3
    create_ticket = next(row for row in operations if row.method == "POST")
    assert create_ticket.side_effect_level == "write"
    assert create_ticket.request_content_types == ["application/json"]
    assert create_ticket.response_statuses == ["201"]


def test_list_operations_uses_latest_imported_version(client: TestClient) -> None:
    integration = _create_integration(client)
    imported = _import_version(client, str(integration["integration_id"]))

    response = client.get(f"{BASE}/{integration['integration_id']}/operations")
    assert response.status_code == 200, response.text
    operations = response.json()["data"]
    assert len(operations) == 3
    assert {row["integration_version_id"] for row in operations} == {imported["version_id"]}
    assert {row["operation_key"] for row in operations} == {
        "GET /tickets",
        "POST /tickets",
        "GET /tickets/{ticket_id}",
    }


def test_duplicate_import_of_same_spec_is_rejected(client: TestClient) -> None:
    integration = _create_integration(client)
    _import_version(client, str(integration["integration_id"]))

    response = client.post(
        f"{BASE}/{integration['integration_id']}/import",
        json={
            "source_kind": "inline_text",
            "source_ref": "inline://ticketing",
            "spec_text": OPENAPI_SPEC,
        },
    )
    assert response.status_code == 409
    assert "already imported" in response.json()["detail"]


def test_invalid_openapi_import_returns_400(client: TestClient) -> None:
    integration = _create_integration(client)

    response = client.post(
        f"{BASE}/{integration['integration_id']}/import",
        json={"source_kind": "inline_text", "spec_text": "openapi: 2.0\npaths: {}"},
    )
    assert response.status_code == 400
    assert "OpenAPI version" in response.json()["detail"]


def test_user_scoped_integration_is_hidden_from_other_viewers(client: TestClient) -> None:
    integration = _create_integration(client)

    response = client.get(
        f"{BASE}/{integration['integration_id']}",
        headers={"X-CALIBER-User": "@viewer"},
    )
    assert response.status_code == 404


def test_generate_preview_and_publish_openapi_tool_draft(
    client: TestClient,
    db_session: Session,
    monkeypatch,
) -> None:
    integration = _create_integration(client)
    _import_version(client, str(integration["integration_id"]))
    operations = client.get(f"{BASE}/{integration['integration_id']}/operations").json()["data"]
    target = next(item for item in operations if item["operation_key"] == "GET /tickets/{ticket_id}")

    monkeypatch.setenv("OPENAPI_TICKET_TOKEN", "ticket-secret")

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url == "https://tickets.example.com/tickets/T-1"
        assert request.headers["Authorization"] == "Bearer ticket-secret"
        return httpx.Response(200, json={"ticket_id": "T-1", "status": "open"})

    _mock_http(monkeypatch, handler)

    generated = client.post(
        f"{BASE}/{integration['integration_id']}/tool-drafts/generate",
        json={
            "operation_ids": [target["operation_id"]],
            "auth_binding": {"kind": "bearer", "secret_ref": "env://OPENAPI_TICKET_TOKEN"},
            "allow_in_preview": True,
        },
    )
    assert generated.status_code == 201, generated.text
    draft = generated.json()["data"][0]
    assert draft["draft_id"].startswith("OATD-")
    assert draft["status"] == "ready"
    assert draft["secret_refs"] == ["env://OPENAPI_TICKET_TOKEN"]

    preview = client.post(
        f"{BASE}/{integration['integration_id']}/tool-drafts/{draft['draft_id']}/preview",
        json={"input": {"path_params": {"ticket_id": "T-1"}}},
    )
    assert preview.status_code == 200, preview.text
    assert preview.json()["data"]["result"]["json"]["ticket_id"] == "T-1"

    published = client.post(
        f"{BASE}/{integration['integration_id']}/tool-drafts/{draft['draft_id']}/publish",
        json={"version": "1.0"},
    )
    assert published.status_code == 201, published.text
    tool = published.json()["data"]["tool"]
    assert tool["execution_backend"] == "openapi_http"
    assert tool["module_path"] == "<openapi_http>"
    assert tool["callable_name"] == "invoke"

    runtime = client.post(
        f"{PREFIX}/tools/{tool['tool_id']}/test-run",
        json={"input": {"path_params": {"ticket_id": "T-1"}}},
    )
    assert runtime.status_code == 200, runtime.text
    runtime_data = runtime.json()["data"]
    assert runtime_data["error"] is None
    assert runtime_data["isolation"] == "inline_http"
    assert runtime_data["output"]["json"]["status"] == "open"

    draft_row = db_session.get(CaliberOpenApiToolDraft, draft["draft_id"])
    tool_row = db_session.get(CaliberToolRegistry, tool["tool_id"])
    assert draft_row is not None
    assert draft_row.published_tool_id == tool["tool_id"]
    assert tool_row is not None
    assert tool_row.execution_backend == "openapi_http"


def test_published_openapi_tool_runs_in_workflow_preview(client: TestClient, monkeypatch) -> None:
    integration = _create_integration(client)
    _import_version(client, str(integration["integration_id"]))
    operation_id = next(
        item["operation_id"]
        for item in client.get(f"{BASE}/{integration['integration_id']}/operations").json()["data"]
        if item["operation_key"] == "GET /tickets/{ticket_id}"
    )
    monkeypatch.setenv("OPENAPI_TICKET_TOKEN", "ticket-secret")

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url == "https://tickets.example.com/tickets/T-2"
        return httpx.Response(200, json={"ticket_id": "T-2", "status": "resolved"})

    _mock_http(monkeypatch, handler)

    draft = client.post(
        f"{BASE}/{integration['integration_id']}/tool-drafts/generate",
        json={
            "operation_ids": [operation_id],
            "auth_binding": {"kind": "bearer", "secret_ref": "env://OPENAPI_TICKET_TOKEN"},
            "allow_in_preview": True,
        },
    ).json()["data"][0]
    tool = client.post(
        f"{BASE}/{integration['integration_id']}/tool-drafts/{draft['draft_id']}/publish",
        json={"name": "get_ticket_tool", "version": "1.0"},
    ).json()["data"]["tool"]

    workflow_id = create_workflow(client, "OpenAPI Tool Workflow")
    manifest = make_manifest(workflow_id)
    del manifest["nodes"]["agent"]
    manifest["nodes"]["tool_lookup"] = {
        "id": "tool_lookup",
        "type": "tool",
        "tool_name": "get_ticket_tool",
        "inputs": {"input": {"type": "structured"}},
        "outputs": {
            "text": {"type": "string"},
            "result": {"type": "structured"},
            "metadata": {"type": "structured"},
        },
    }
    manifest["tools"] = {
        "get_ticket_tool": {
            "registry_ref": "tool.get_ticket_tool.v1",
            "version_constraint": ">=1.0,<2.0",
        }
    }
    manifest["edges"] = [
        {"id": "e1", "from": "start", "to": "tool_lookup", "map": {"msg": "input"}},
        {"id": "e2", "from": "tool_lookup", "to": "final", "map": {"text": "response"}},
    ]
    manifest["nodes"]["start"]["outputs"] = {"msg": {"type": "structured"}}

    version_id, _ = create_draft(client, workflow_id, manifest)
    preview = client.post(
        f"{PREFIX}/workflow-versions/{version_id}/preview-run",
        json={"input": {"path_params": {"ticket_id": "T-2"}}},
    )
    assert preview.status_code == 200, preview.text
    body = preview.json()["data"]
    assert body["status"] == "completed"
    assert "resolved" in str(body["output"])


def test_published_openapi_tool_calibrates_through_the_standard_tool_route(
    client: TestClient, monkeypatch
) -> None:
    integration = _create_integration(client)
    _import_version(client, str(integration["integration_id"]))
    operation_id = next(
        item["operation_id"]
        for item in client.get(f"{BASE}/{integration['integration_id']}/operations").json()["data"]
        if item["operation_key"] == "GET /tickets/{ticket_id}"
    )
    monkeypatch.setenv("OPENAPI_TICKET_TOKEN", "ticket-secret")

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url == "https://tickets.example.com/tickets/T-1"
        return httpx.Response(200, json={"ticket_id": "T-1", "status": "open"})

    _mock_http(monkeypatch, handler)

    draft = client.post(
        f"{BASE}/{integration['integration_id']}/tool-drafts/generate",
        json={
            "operation_ids": [operation_id],
            "auth_binding": {"kind": "bearer", "secret_ref": "env://OPENAPI_TICKET_TOKEN"},
            "allow_in_preview": True,
        },
    ).json()["data"][0]
    tool = client.post(
        f"{BASE}/{integration['integration_id']}/tool-drafts/{draft['draft_id']}/publish",
        json={"name": "get_ticket_calibrated", "version": "1.0"},
    ).json()["data"]["tool"]

    saved = client.put(
        f"{PREFIX}/tools/{tool['tool_id']}/test-cases",
        json={"test_cases": [{"name": "fetch-ticket", "input": {"path_params": {"ticket_id": "T-1"}}}]},
    )
    assert saved.status_code == 200, saved.text

    calibrated = client.post(f"{PREFIX}/tools/{tool['tool_id']}/calibrate")
    assert calibrated.status_code == 200, calibrated.text
    data = calibrated.json()["data"]
    assert data["pass_rate"] == 1.0
    assert data["cases"][0]["output"]["json"]["status"] == "open"


def test_preview_is_refused_when_the_draft_is_not_previewable(
    client: TestClient, monkeypatch
) -> None:
    """A live upstream call must not be reachable on scope alone.

    ``preview`` performs a real request. Before this gate existed, an operator could
    fire an approval-gated write through it without anyone approving anything, which
    made ``requires_approval`` advisory rather than enforced.
    """

    integration = _create_integration(client)
    _import_version(client, str(integration["integration_id"]))
    operations = client.get(f"{BASE}/{integration['integration_id']}/operations").json()["data"]
    target = next(item for item in operations if item["operation_key"] == "POST /tickets")
    monkeypatch.setenv("OPENAPI_TICKET_TOKEN", "ticket-secret")

    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(201, json={"ticket_id": "T-new"})

    _mock_http(monkeypatch, handler)

    draft = client.post(
        f"{BASE}/{integration['integration_id']}/tool-drafts/generate",
        json={
            "operation_ids": [target["operation_id"]],
            "auth_binding": {"kind": "bearer", "secret_ref": "env://OPENAPI_TICKET_TOKEN"},
        },
    ).json()["data"][0]
    assert draft["allow_in_preview"] is False
    assert draft["requires_approval"] is True  # a write operation

    refused = client.post(
        f"{BASE}/{integration['integration_id']}/tool-drafts/{draft['draft_id']}/preview",
        json={"input": {"body": {"title": "hello"}}},
    )
    assert refused.status_code == 409, refused.text
    assert "allow_in_preview" in refused.json()["detail"]
    assert calls == []  # nothing was sent upstream

    allowed = client.patch(
        f"{BASE}/{integration['integration_id']}/tool-drafts/{draft['draft_id']}",
        json={"allow_in_preview": True},
    )
    assert allowed.status_code == 200, allowed.text
    ran = client.post(
        f"{BASE}/{integration['integration_id']}/tool-drafts/{draft['draft_id']}/preview",
        json={"input": {"body": {"title": "hello"}}},
    )
    assert ran.status_code == 200, ran.text
    assert len(calls) == 1


def test_preview_is_audited_without_recording_payloads_or_secrets(
    client: TestClient, db_session: Session, monkeypatch
) -> None:
    integration = _create_integration(client)
    _import_version(client, str(integration["integration_id"]))
    operations = client.get(f"{BASE}/{integration['integration_id']}/operations").json()["data"]
    target = next(item for item in operations if item["operation_key"] == "GET /tickets/{ticket_id}")
    monkeypatch.setenv("OPENAPI_TICKET_TOKEN", "ticket-secret")

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"ticket_id": "T-1", "pii": "do-not-log"})

    _mock_http(monkeypatch, handler)

    draft = client.post(
        f"{BASE}/{integration['integration_id']}/tool-drafts/generate",
        json={
            "operation_ids": [target["operation_id"]],
            "auth_binding": {"kind": "bearer", "secret_ref": "env://OPENAPI_TICKET_TOKEN"},
            "allow_in_preview": True,
        },
    ).json()["data"][0]

    response = client.post(
        f"{BASE}/{integration['integration_id']}/tool-drafts/{draft['draft_id']}/preview",
        json={"input": {"path_params": {"ticket_id": "T-1"}}},
    )
    assert response.status_code == 200, response.text

    row = (
        db_session.execute(
            select(CaliberAuditLog)
            .where(CaliberAuditLog.action == "preview_openapi_tool_draft")
            .where(CaliberAuditLog.entity_id == draft["draft_id"])
        )
        .scalars()
        .one()
    )
    details = dict(row.details or {})
    assert details["operation_key"] == "GET /tickets/{ticket_id}"
    assert details["status_code"] == 200
    assert details["side_effect_level"] == "read"
    assert details["secret_refs"] == ["env://OPENAPI_TICKET_TOKEN"]
    # The reference is recorded; the resolved value and the response body are not.
    serialized = json.dumps(details)
    assert "ticket-secret" not in serialized
    assert "do-not-log" not in serialized


def test_failed_preview_is_still_audited(client: TestClient, db_session: Session, monkeypatch) -> None:
    integration = _create_integration(client)
    _import_version(client, str(integration["integration_id"]))
    operations = client.get(f"{BASE}/{integration['integration_id']}/operations").json()["data"]
    target = next(item for item in operations if item["operation_key"] == "GET /tickets/{ticket_id}")

    draft = client.post(
        f"{BASE}/{integration['integration_id']}/tool-drafts/generate",
        json={"operation_ids": [target["operation_id"]], "allow_in_preview": True},
    ).json()["data"][0]

    # No auth binding was configured for an operation the spec secures, and no
    # transport was stubbed: the call fails before any socket is opened.
    response = client.post(
        f"{BASE}/{integration['integration_id']}/tool-drafts/{draft['draft_id']}/preview",
        json={"input": {"path_params": {"ticket_id": "T-1"}}},
    )
    assert response.status_code == 400, response.text

    row = (
        db_session.execute(
            select(CaliberAuditLog)
            .where(CaliberAuditLog.action == "preview_openapi_tool_draft")
            .where(CaliberAuditLog.entity_id == draft["draft_id"])
        )
        .scalars()
        .one()
    )
    assert "error" in dict(row.details or {})


def test_import_persists_auto_wired_dependencies_and_a_graph_snapshot(
    client: TestClient, db_session: Session
) -> None:
    integration = _create_integration(client)
    imported = _import_version(client, str(integration["integration_id"]))

    deps = client.get(f"{BASE}/{integration['integration_id']}/dependencies")
    assert deps.status_code == 200, deps.text
    rows = deps.json()["data"]
    assert rows  # path-hierarchy + identifier-flow rules both fire on this spec
    assert {row["confidence"] for row in rows} <= {"high", "medium", "low"}
    assert any(row["status"] == "suggested" for row in rows)

    version = db_session.get(CaliberOpenApiIntegrationVersion, imported["version_id"])
    assert version is not None
    assert version.graph_snapshot is not None
    assert version.dependency_detected_at is not None
    assert version.graph_snapshot["summary"]["operation_count"] == 3


def test_dependencies_can_be_filtered_by_status(client: TestClient) -> None:
    integration = _create_integration(client)
    _import_version(client, str(integration["integration_id"]))

    suggested = client.get(
        f"{BASE}/{integration['integration_id']}/dependencies", params={"status": "suggested"}
    ).json()["data"]
    assert suggested
    assert all(row["status"] == "suggested" for row in suggested)


def test_confirming_a_suggested_dependency_updates_status_and_audits(
    client: TestClient, db_session: Session
) -> None:
    integration = _create_integration(client)
    _import_version(client, str(integration["integration_id"]))
    suggested = client.get(
        f"{BASE}/{integration['integration_id']}/dependencies", params={"status": "suggested"}
    ).json()["data"][0]

    response = client.patch(
        f"{BASE}/{integration['integration_id']}/dependencies/{suggested['dependency_id']}",
        json={"status": "confirmed", "notes": "verified with the vendor docs"},
    )
    assert response.status_code == 200, response.text
    data = response.json()["data"]
    assert data["status"] == "confirmed"
    assert data["confirmed_by"]
    assert data["confirmed_at"] is not None

    row = (
        db_session.execute(
            select(CaliberAuditLog).where(CaliberAuditLog.action == "review_openapi_dependency")
        )
        .scalars()
        .one()
    )
    assert dict(row.details or {})["status"] == "confirmed"


def test_rejecting_a_dependency_keeps_the_row_rather_than_deleting_it(client: TestClient) -> None:
    integration = _create_integration(client)
    _import_version(client, str(integration["integration_id"]))
    suggested = client.get(
        f"{BASE}/{integration['integration_id']}/dependencies", params={"status": "suggested"}
    ).json()["data"][0]

    response = client.patch(
        f"{BASE}/{integration['integration_id']}/dependencies/{suggested['dependency_id']}",
        json={"status": "rejected"},
    )
    assert response.status_code == 200, response.text
    assert response.json()["data"]["status"] == "rejected"

    still_there = client.get(f"{BASE}/{integration['integration_id']}/dependencies").json()["data"]
    assert any(row["dependency_id"] == suggested["dependency_id"] for row in still_there)


def test_auto_wired_dependency_cannot_be_reviewed(client: TestClient) -> None:
    integration = _create_integration(client)
    _import_version(client, str(integration["integration_id"]))
    auto_wired = client.get(
        f"{BASE}/{integration['integration_id']}/dependencies", params={"status": "auto_wired"}
    ).json()["data"]
    if not auto_wired:
        pytest.skip("no high-confidence dependency in the fixture spec")
    response = client.patch(
        f"{BASE}/{integration['integration_id']}/dependencies/{auto_wired[0]['dependency_id']}",
        json={"status": "confirmed"},
    )
    assert response.status_code == 409


def test_graph_route_serves_the_cached_snapshot(client: TestClient) -> None:
    integration = _create_integration(client)
    _import_version(client, str(integration["integration_id"]))

    response = client.get(f"{BASE}/{integration['integration_id']}/graph")
    assert response.status_code == 200, response.text
    graph = response.json()["data"]
    assert graph["integration_id"] == integration["integration_id"]
    assert any(node["type"] == "operation" for node in graph["nodes"])
    assert any(node["type"] == "dependency" for node in graph["nodes"])
    assert graph["summary"]["node_count"] == len(graph["nodes"])


def test_graph_reflects_a_published_tool(client: TestClient, monkeypatch) -> None:
    integration = _create_integration(client)
    _import_version(client, str(integration["integration_id"]))
    operations = client.get(f"{BASE}/{integration['integration_id']}/operations").json()["data"]
    target = next(item for item in operations if item["operation_key"] == "GET /tickets/{ticket_id}")
    monkeypatch.setenv("OPENAPI_TICKET_TOKEN", "ticket-secret")
    draft = client.post(
        f"{BASE}/{integration['integration_id']}/tool-drafts/generate",
        json={
            "operation_ids": [target["operation_id"]],
            "auth_binding": {"kind": "bearer", "secret_ref": "env://OPENAPI_TICKET_TOKEN"},
        },
    ).json()["data"][0]
    tool = client.post(
        f"{BASE}/{integration['integration_id']}/tool-drafts/{draft['draft_id']}/publish",
        json={"version": "1.0"},
    ).json()["data"]["tool"]

    graph = client.get(f"{BASE}/{integration['integration_id']}/graph").json()["data"]
    node_ids = {node["id"] for node in graph["nodes"]}
    assert f"published_tool:{tool['tool_id']}" in node_ids


def test_diff_route_reports_no_predecessor_for_the_first_version(client: TestClient) -> None:
    integration = _create_integration(client)
    imported = _import_version(client, str(integration["integration_id"]))

    response = client.post(
        f"{BASE}/{integration['integration_id']}/versions/{imported['version_id']}/diff"
    )
    assert response.status_code == 200, response.text
    data = response.json()["data"]
    assert data["from_version_id"] is None
    assert data["to_version_id"] == imported["version_id"]


def test_diff_route_reports_added_and_breaking_changes_between_versions(client: TestClient) -> None:
    integration = _create_integration(client)
    _import_version(client, str(integration["integration_id"]))

    changed_spec = OPENAPI_SPEC.replace(
        "      summary: Get one ticket\n",
        "      summary: Get one ticket\n      deprecated: true\n",
    ).replace(
        "  /tickets/{ticket_id}:\n",
        "  /tickets/{ticket_id}/archive:\n    post:\n      operationId: archiveTicket\n      responses:\n        \"200\":\n          description: ok\n  /tickets/{ticket_id}:\n",
    )
    second = _import_version(
        client, str(integration["integration_id"]), spec_text=changed_spec, source_ref="inline://v2"
    )

    response = client.post(
        f"{BASE}/{integration['integration_id']}/versions/{second['version_id']}/diff"
    )
    assert response.status_code == 200, response.text
    data = response.json()["data"]
    assert "POST /tickets/{ticket_id}/archive" in data["added"]
    assert any(row["operation_key"] == "GET /tickets/{ticket_id}" for row in data["changed"])
    assert any(row["reason"] == "operation was deprecated" for row in data["breaking"])


def test_validate_spec_source_reports_a_blocked_url(client: TestClient) -> None:
    integration = _create_integration(client)
    response = client.post(
        f"{BASE}/{integration['integration_id']}/validate-spec-source",
        json={"source_kind": "url", "spec_url": "http://169.254.169.254/latest/meta-data/"},
    )
    assert response.status_code == 200, response.text
    data = response.json()["data"]
    assert data["allowed"] is False
    assert data["reachable"] is False


def test_validate_spec_source_reports_a_reachable_allowed_url(
    client: TestClient, openapi_spec_server
) -> None:
    integration = _create_integration(client)
    response = client.post(
        f"{BASE}/{integration['integration_id']}/validate-spec-source",
        json={"source_kind": "url", "spec_url": openapi_spec_server},
    )
    assert response.status_code == 200, response.text
    data = response.json()["data"]
    assert data["allowed"] is True
    assert data["reachable"] is True


def test_import_from_a_url_source_fetches_over_guarded_egress(
    client: TestClient, openapi_spec_server, db_session: Session
) -> None:
    integration = _create_integration(client)
    response = client.post(
        f"{BASE}/{integration['integration_id']}/import",
        json={"source_kind": "url", "spec_url": openapi_spec_server},
    )
    assert response.status_code == 201, response.text
    data = response.json()["data"]
    assert data["source_kind"] == "url"
    assert data["source_ref"] == openapi_spec_server
    assert data["operation_count"] == 3


def test_import_from_a_blocked_url_is_rejected(client: TestClient) -> None:
    integration = _create_integration(client)
    response = client.post(
        f"{BASE}/{integration['integration_id']}/import",
        json={"source_kind": "url", "spec_url": "http://169.254.169.254/openapi.json"},
    )
    assert response.status_code == 400, response.text
    assert "blocked" in response.json()["detail"].lower()


def test_import_from_an_uploaded_base64_spec(client: TestClient) -> None:
    import base64

    integration = _create_integration(client)
    encoded = base64.b64encode(OPENAPI_SPEC.encode("utf-8")).decode("ascii")
    response = client.post(
        f"{BASE}/{integration['integration_id']}/import",
        json={"source_kind": "upload", "spec_base64": encoded, "source_ref": "ticket-api.yaml"},
    )
    assert response.status_code == 201, response.text
    data = response.json()["data"]
    assert data["source_kind"] == "upload"
    assert data["operation_count"] == 3


def test_reimport_refetches_and_diffs_against_the_previous_url_version(
    client: TestClient, openapi_spec_server_factory
) -> None:
    server, url = openapi_spec_server_factory(OPENAPI_SPEC)
    integration = _create_integration(client)
    client.post(
        f"{BASE}/{integration['integration_id']}/import",
        json={"source_kind": "url", "spec_url": url},
    )

    changed_spec = OPENAPI_SPEC + (
        "  /tickets/{ticket_id}/archive:\n"
        "    post:\n"
        "      operationId: archiveTicket\n"
        "      responses:\n"
        "        \"200\":\n"
        "          description: ok\n"
    )
    server.set_body(changed_spec)

    response = client.post(f"{BASE}/{integration['integration_id']}/reimport")
    assert response.status_code == 201, response.text
    data = response.json()["data"]
    assert data["version"]["operation_count"] == 4
    assert "POST /tickets/{ticket_id}/archive" in data["diff"]["added"]


def test_reimport_refuses_an_inline_sourced_integration(client: TestClient) -> None:
    integration = _create_integration(client)
    _import_version(client, str(integration["integration_id"]))

    response = client.post(f"{BASE}/{integration['integration_id']}/reimport")
    assert response.status_code == 409
    assert "inline_text" in response.json()["detail"]


def test_generate_tool_drafts_by_tag_and_method_selection(client: TestClient) -> None:
    integration = _create_integration(client)
    _import_version(client, str(integration["integration_id"]))

    response = client.post(
        f"{BASE}/{integration['integration_id']}/tool-drafts/generate",
        json={"tags": ["tickets"], "methods": ["GET"]},
    )
    assert response.status_code == 201, response.text
    drafts = response.json()["data"]
    assert len(drafts) == 2  # GET /tickets and GET /tickets/{ticket_id}
    assert all(draft["additional_operation_ids"] == [] for draft in drafts)


def test_generate_tool_pack_binds_multiple_operations_to_one_draft(
    client: TestClient, monkeypatch
) -> None:
    integration = _create_integration(client)
    _import_version(client, str(integration["integration_id"]))
    operations = client.get(f"{BASE}/{integration['integration_id']}/operations").json()["data"]
    list_op = next(item for item in operations if item["operation_key"] == "GET /tickets")
    get_op = next(item for item in operations if item["operation_key"] == "GET /tickets/{ticket_id}")
    monkeypatch.setenv("OPENAPI_TICKET_TOKEN", "ticket-secret")

    response = client.post(
        f"{BASE}/{integration['integration_id']}/tool-drafts/generate",
        json={
            "operation_ids": [list_op["operation_id"], get_op["operation_id"]],
            "group_as_pack": True,
            "auth_binding": {"kind": "bearer", "secret_ref": "env://OPENAPI_TICKET_TOKEN"},
        },
    )
    assert response.status_code == 201, response.text
    drafts = response.json()["data"]
    assert len(drafts) == 1
    pack = drafts[0]
    assert pack["operation_id"] == list_op["operation_id"]
    assert pack["additional_operation_ids"] == [get_op["operation_id"]]
    assert pack["execution_config"]["kind"] == "openapi_http_pack"
    assert {entry["operation_key"] for entry in pack["execution_config"]["operations"]} == {
        "GET /tickets",
        "GET /tickets/{ticket_id}",
    }
    assert pack["input_schema"]["oneOf"]


def test_tool_pack_executes_the_selected_bound_operation(client: TestClient, monkeypatch) -> None:
    integration = _create_integration(client)
    _import_version(client, str(integration["integration_id"]))
    operations = client.get(f"{BASE}/{integration['integration_id']}/operations").json()["data"]
    list_op = next(item for item in operations if item["operation_key"] == "GET /tickets")
    get_op = next(item for item in operations if item["operation_key"] == "GET /tickets/{ticket_id}")
    monkeypatch.setenv("OPENAPI_TICKET_TOKEN", "ticket-secret")

    def handler(request: httpx.Request) -> httpx.Response:
        if str(request.url).endswith("/tickets/T-1"):
            return httpx.Response(200, json={"ticket_id": "T-1"})
        return httpx.Response(200, json=[{"ticket_id": "T-1"}, {"ticket_id": "T-2"}])

    _mock_http(monkeypatch, handler)

    pack = client.post(
        f"{BASE}/{integration['integration_id']}/tool-drafts/generate",
        json={
            "operation_ids": [list_op["operation_id"], get_op["operation_id"]],
            "group_as_pack": True,
            "auth_binding": {"kind": "bearer", "secret_ref": "env://OPENAPI_TICKET_TOKEN"},
            "allow_in_preview": True,
        },
    ).json()["data"][0]

    get_call = client.post(
        f"{BASE}/{integration['integration_id']}/tool-drafts/{pack['draft_id']}/preview",
        json={"input": {"operation": "GET /tickets/{ticket_id}", "path_params": {"ticket_id": "T-1"}}},
    )
    assert get_call.status_code == 200, get_call.text
    assert get_call.json()["data"]["result"]["json"]["ticket_id"] == "T-1"

    list_call = client.post(
        f"{BASE}/{integration['integration_id']}/tool-drafts/{pack['draft_id']}/preview",
        json={"input": {"operation": "GET /tickets"}},
    )
    assert list_call.status_code == 200, list_call.text
    assert len(list_call.json()["data"]["result"]["json"]) == 2


_LINKED_SPEC = """
openapi: 3.0.3
info:
  title: Order API
  version: "1"
paths:
  /orders:
    post:
      operationId: createOrder
      responses:
        "201":
          description: created
          links:
            GetOrder:
              operationId: getOrder
  /orders/{order_id}:
    get:
      operationId: getOrder
      parameters:
        - in: path
          name: order_id
          required: true
          schema:
            type: string
      responses:
        "200":
          description: ok
"""


def test_openapi_link_produces_an_auto_wired_dependency_that_cannot_be_reviewed(
    client: TestClient,
) -> None:
    integration = _create_integration(client)
    _import_version(client, str(integration["integration_id"]), spec_text=_LINKED_SPEC, source_ref="inline://orders")

    auto_wired = client.get(
        f"{BASE}/{integration['integration_id']}/dependencies", params={"status": "auto_wired"}
    ).json()["data"]
    assert len(auto_wired) == 1
    row = auto_wired[0]
    assert row["dependency_type"] == "produces_identifier_for"
    assert row["confidence"] == "high"
    assert row["source"] == "openapi_link"

    response = client.patch(
        f"{BASE}/{integration['integration_id']}/dependencies/{row['dependency_id']}",
        json={"status": "confirmed"},
    )
    assert response.status_code == 409
