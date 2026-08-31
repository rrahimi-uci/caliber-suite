"""``client.openapi_integrations`` — request shape and response decoding."""

from __future__ import annotations

import json

import httpx

from caliber_sdk import CaliberClient

BASE = "https://caliber.test"


def client_with(handler: object) -> CaliberClient:
    http = httpx.Client(transport=httpx.MockTransport(handler))  # type: ignore[arg-type]
    return CaliberClient(BASE, token="calpat_x", http_client=http)


def test_create_posts_name_and_decodes_the_integration() -> None:
    seen: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["method"] = request.method
        seen["url"] = str(request.url)
        seen["body"] = json.loads(request.content)
        return httpx.Response(
            201,
            json={
                "data": {
                    "integration_id": "OAI-1",
                    "name": "Ticketing",
                    "status": "draft",
                    "visibility": "user",
                }
            },
        )

    with client_with(handler) as caliber:
        result = caliber.openapi_integrations.create("Ticketing", description="External ticket API")

    assert seen["method"] == "POST"
    assert seen["url"] == f"{BASE}/ajax-api/2.0/mlflow/caliber/openapi-integrations"
    assert seen["body"] == {"name": "Ticketing", "description": "External ticket API"}
    assert result.integration_id == "OAI-1"
    assert result.status == "draft"


def test_import_spec_dispatches_on_the_source_kwarg() -> None:
    bodies: list[dict[str, object]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        bodies.append(json.loads(request.content))
        return httpx.Response(201, json={"data": {"version_id": "OAIV-1"}})

    with client_with(handler) as caliber:
        caliber.openapi_integrations.import_spec("OAI-1", spec_text="openapi: 3.0.3")
        caliber.openapi_integrations.import_spec(
            "OAI-1", spec_url="https://example.com/openapi.json"
        )
        caliber.openapi_integrations.import_spec("OAI-1", spec_base64="b3BlbmFwaTogMy4wLjM=")

    assert bodies[0] == {"source_kind": "inline_text", "spec_text": "openapi: 3.0.3"}
    assert bodies[1] == {"source_kind": "url", "spec_url": "https://example.com/openapi.json"}
    assert bodies[2] == {"source_kind": "upload", "spec_base64": "b3BlbmFwaTogMy4wLjM="}


def test_list_operations_decodes_a_list_and_forwards_version_id() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.params["version_id"] == "OAIV-2"
        return httpx.Response(
            200,
            json={
                "data": [
                    {"operation_id": "OAIO-1", "operation_key": "GET /tickets", "method": "GET"},
                ]
            },
        )

    with client_with(handler) as caliber:
        operations = caliber.openapi_integrations.list_operations("OAI-1", version_id="OAIV-2")

    assert len(operations) == 1
    assert operations[0].operation_key == "GET /tickets"


def test_generate_tool_drafts_sends_selection_filters_not_just_ids() -> None:
    seen_body: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen_body.update(json.loads(request.content))
        return httpx.Response(201, json={"data": [{"draft_id": "OATD-1"}]})

    with client_with(handler) as caliber:
        drafts = caliber.openapi_integrations.generate_tool_drafts(
            "OAI-1", tags=["tickets"], methods=["GET"], group_as_pack=True
        )

    assert seen_body["tags"] == ["tickets"]
    assert seen_body["methods"] == ["GET"]
    assert seen_body["group_as_pack"] is True
    assert "operation_ids" not in seen_body
    assert drafts[0].draft_id == "OATD-1"


def test_preview_tool_draft_sends_input_and_returns_the_raw_result() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert json.loads(request.content) == {"input": {"path_params": {"ticket_id": "T-1"}}}
        return httpx.Response(
            200, json={"data": {"draft_id": "OATD-1", "result": {"status_code": 200}}}
        )

    with client_with(handler) as caliber:
        result = caliber.openapi_integrations.preview_tool_draft(
            "OAI-1", "OATD-1", input={"path_params": {"ticket_id": "T-1"}}
        )

    assert result["result"]["status_code"] == 200


def test_publish_tool_draft_returns_both_draft_and_tool() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            201,
            json={
                "data": {
                    "draft": {"draft_id": "OATD-1", "status": "published"},
                    "tool": {"tool_id": "TL-1", "execution_backend": "openapi_http"},
                }
            },
        )

    with client_with(handler) as caliber:
        result = caliber.openapi_integrations.publish_tool_draft("OAI-1", "OATD-1", version="1.0")

    assert result["draft"]["status"] == "published"
    assert result["tool"]["execution_backend"] == "openapi_http"


def test_list_dependencies_omits_none_params() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert dict(request.url.params) == {}
        return httpx.Response(200, json={"data": []})

    with client_with(handler) as caliber:
        caliber.openapi_integrations.list_dependencies("OAI-1")


def test_review_dependency_patches_status_and_notes() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "PATCH"
        assert json.loads(request.content) == {"status": "confirmed", "notes": "checked"}
        return httpx.Response(
            200, json={"data": {"dependency_id": "OADEP-1", "status": "confirmed"}}
        )

    with client_with(handler) as caliber:
        result = caliber.openapi_integrations.review_dependency(
            "OAI-1", "OADEP-1", status="confirmed", notes="checked"
        )

    assert result.status == "confirmed"


def test_graph_returns_the_raw_snapshot() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, json={"data": {"nodes": [{"id": "operation:OP-1"}], "edges": []}}
        )

    with client_with(handler) as caliber:
        snapshot = caliber.openapi_integrations.graph("OAI-1")

    assert snapshot["nodes"][0]["id"] == "operation:OP-1"


def test_raw_escape_hatch_reaches_the_same_transport() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"data": {"ok": True}})

    with client_with(handler) as caliber:
        result = caliber.openapi_integrations.raw.get("/openapi-integrations/OAI-1/tool-drafts")
    assert result.data == {"ok": True}
