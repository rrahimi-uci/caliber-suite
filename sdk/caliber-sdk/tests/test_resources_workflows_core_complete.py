"""Finishes ``workflows`` (import/export, calibration, deployments,
promotion listing, run stats, service-token management, session memory,
event triggers) and the one remaining ``services`` operation to 100%.

Every test pins the exact path and method, per the discipline established for
every prior wave.
"""

from __future__ import annotations

from typing import Any

import httpx

from caliber_sdk import CaliberClient

BASE = "https://caliber.test"


def client_with(handler: Any) -> CaliberClient:
    http = httpx.Client(transport=httpx.MockTransport(handler))
    return CaliberClient(BASE, token="calpat_test", http_client=http)


def envelope(data: Any, status: int = 200) -> httpx.Response:
    return httpx.Response(status, json={"data": data})


def _seen_path(request: httpx.Request) -> str:
    return f"{request.method} {request.url.path.rsplit('/caliber', 1)[-1]}"


# --- import / preview_import --------------------------------------------------


def test_import_workflow_and_preview_import_hit_distinct_documented_paths() -> None:
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(_seen_path(request))
        return envelope({"workflow_id": "WF-1", "name": "imported"})

    with client_with(handler) as caliber:
        caliber.workflows.preview_import(manifest_yaml="nodes: []")
        caliber.workflows.import_workflow(manifest_yaml="nodes: []", name="imported")

    assert seen == [
        "POST /workflows/import/preview",
        "POST /workflows/import",
    ]


def test_import_workflow_omits_unset_optional_fields() -> None:
    bodies: list[bytes] = []

    def handler(request: httpx.Request) -> httpx.Response:
        bodies.append(request.read())
        return envelope({"workflow_id": "WF-1"})

    with client_with(handler) as caliber:
        caliber.workflows.import_workflow(manifest={"nodes": []})

    assert bodies[0] == b'{"manifest":{"nodes":[]}}'


# --- calibration ---------------------------------------------------------------


def test_calibration_options_and_run_hit_the_documented_paths() -> None:
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(_seen_path(request))
        return envelope({})

    with client_with(handler) as caliber:
        caliber.workflows.calibration_options("WF-1")
        caliber.workflows.create_calibration_run("WF-1", agent_id="AGT-1")

    assert seen == [
        "GET /workflows/WF-1/calibration/options",
        "POST /workflows/WF-1/calibration/runs",
    ]


# --- deployments, promotions listing --------------------------------------------


def test_deployments_promote_rollback_and_list_promotions_hit_documented_paths() -> None:
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(_seen_path(request))
        return envelope({})

    with client_with(handler) as caliber:
        caliber.workflows.deployments("WF-1")
        caliber.workflows.promote_deployment("WF-1", "prod", version_id="WFV-2")
        caliber.workflows.rollback_deployment("WF-1", "prod")
        caliber.workflows.list_promotions("WF-1")

    assert seen == [
        "GET /workflows/WF-1/deployments",
        "POST /workflows/WF-1/deployments/prod/promote",
        "POST /workflows/WF-1/deployments/prod/rollback",
        "GET /workflows/WF-1/promotions",
    ]


def test_list_promotions_does_not_collide_with_the_promotions_sub_resource() -> None:
    """Regression guard: ``self.promotions`` (the WorkflowPromotionsAPI
    sub-resource, assigned in __init__) would silently shadow a method of
    the same name -- this asserts both stay independently reachable."""

    def handler(request: httpx.Request) -> httpx.Response:
        return envelope({})

    with client_with(handler) as caliber:
        assert callable(caliber.workflows.list_promotions)
        assert hasattr(caliber.workflows.promotions, "approve")
        assert hasattr(caliber.workflows.promotions, "reject")


# --- runs/stats, trigger, session memory ---------------------------------------


def test_runs_stats_trigger_and_session_memory_hit_documented_paths() -> None:
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(_seen_path(request))
        return envelope({})

    with client_with(handler) as caliber:
        caliber.workflows.runs_stats("WF-1")
        caliber.workflows.trigger("WF-1", event_name="order.created")
        caliber.workflows.session_memory("WF-1", session_id="sess-1")
        caliber.workflows.clear_session_memory("WF-1", session_id="sess-1")

    assert seen == [
        "GET /workflows/WF-1/runs/stats",
        "POST /workflows/WF-1/trigger",
        "GET /workflows/WF-1/session-memory",
        "DELETE /workflows/WF-1/session-memory",
    ]


def test_session_memory_sends_session_id_and_optional_node_id_as_params() -> None:
    captured: list[dict[str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(dict(request.url.params))
        return envelope({})

    with client_with(handler) as caliber:
        caliber.workflows.session_memory("WF-1", session_id="sess-1", node_id="node-2")

    assert captured[0] == {"session_id": "sess-1", "node_id": "node-2"}


# --- service token management + management-auth openapi + run status --------


def test_service_management_methods_hit_the_documented_paths() -> None:
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(_seen_path(request))
        return envelope(
            {"token_id": "SVT-1", "token": "cal_svc_..."},
            status=201 if request.method == "POST" else 200,
        )

    with client_with(handler) as caliber:
        caliber.workflows.services.management_openapi("WF-1")
        caliber.workflows.services.tokens("WF-1")
        caliber.workflows.services.create_token("WF-1", name="ci")
        caliber.workflows.services.revoke_token("WF-1", "SVT-1")
        caliber.workflows.services.run_status("WF-1", "RUN-1")

    assert seen == [
        "GET /workflows/WF-1/service/openapi.json",
        "GET /workflows/WF-1/service/tokens",
        "POST /workflows/WF-1/service/tokens",
        "DELETE /workflows/WF-1/service/tokens/SVT-1",
        "GET /services/WF-1/runs/RUN-1",
    ]
