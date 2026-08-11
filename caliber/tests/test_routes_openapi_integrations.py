"""Route tests for governed OpenAPI integration drafts."""

from __future__ import annotations

import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session
from starlette.testclient import TestClient

from caliber.db.models import (
    CaliberOpenApiIntegration,
    CaliberOpenApiToolDraft,
    CaliberOpenApiIntegrationVersion,
    CaliberOpenApiOperation,
    CaliberToolRegistry,
)
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


def _mock_http(monkeypatch, handler) -> None:
    monkeypatch.setattr(
        "caliber.integrations.openapi.executor.build_client",
        lambda *args, **kwargs: httpx.Client(transport=httpx.MockTransport(handler)),
    )


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
