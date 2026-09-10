"""Beta surfaces: integrations, data, operations, and the agentic loop.

The distinction these tests exist to pin: several of these resources can *stop*
without being *finished*, because they are waiting for a person. A waiter that
treated "paused" as transient would burn its whole timeout on the expected
outcome.
"""

from __future__ import annotations

import json as jsonlib
from typing import Any

import httpx

from caliber_sdk import CaliberClient
from caliber_sdk.models import (
    AriaPlan,
    CookbookRecipe,
    Job,
    McpServer,
    QualityReview,
    ReworkTask,
    decode,
)

BASE = "https://caliber.test"


def client_with(handler: Any) -> CaliberClient:
    http = httpx.Client(transport=httpx.MockTransport(handler))
    return CaliberClient(BASE, token="calpat_test", http_client=http)


def envelope(data: Any) -> httpx.Response:
    return httpx.Response(200, json={"data": data})


# --- stopped versus finished ----------------------------------------------


def test_a_job_awaiting_a_human_is_not_terminal_but_is_done_waiting() -> None:
    """``candidate_ready`` is the refinement loop's whole point.

    The job has stopped and will never advance on its own; treating it as
    "still running" is how a script silently drops the human decision.
    """
    ready = decode(Job, {"job_id": "RFN-1", "status": "candidate_ready"})
    assert ready.awaits_human
    assert not ready.is_terminal

    done = decode(Job, {"job_id": "RFN-1", "status": "applied"})
    assert done.is_terminal
    assert not done.awaits_human


def test_waiting_on_a_job_returns_when_it_stops_for_a_person() -> None:
    states = iter(["queued", "running", "candidate_ready"])

    def handler(request: httpx.Request) -> httpx.Response:
        return envelope({"job_id": "RFN-1", "status": next(states)})

    with client_with(handler) as caliber:
        job = caliber.jobs.wait("RFN-1", interval=0.001, max_interval=0.001, timeout=5)

    assert job.awaits_human
    assert job.status == "candidate_ready"


def test_request_changes_sends_notes_and_returns_the_job() -> None:
    sent: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        sent["path"] = request.url.path.rsplit("/caliber", 1)[-1]
        sent["body"] = jsonlib.loads(request.content)
        return envelope({"job_id": "RFN-1", "status": "running", "current_stage": "candidate"})

    with client_with(handler) as caliber:
        result = caliber.jobs.request_changes("RFN-1", "please cite the refund policy")

    assert sent["path"] == "/jobs/RFN-1/request-changes"
    assert sent["body"] == {"notes": "please cite the refund policy"}
    assert result["status"] == "running"


# --- rework tasks -----------------------------------------------------------


def test_rework_tasks_list_passes_status_and_assigned_to() -> None:
    seen: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["path"] = request.url.path.rsplit("/caliber", 1)[-1]
        seen["params"] = dict(request.url.params)
        return envelope(
            [
                {
                    "task_id": "RWT-1",
                    "job_id": "RFN-1",
                    "agent_id": "support-agent",
                    "failure_kind": "machine_gate",
                    "status": "open",
                }
            ]
        )

    with client_with(handler) as caliber:
        tasks = caliber.rework_tasks.list(status="open", assigned_to="@sarah")

    assert seen["path"] == "/rework-tasks"
    assert seen["params"] == {"status": "open", "assigned_to": "@sarah"}
    assert tasks == [
        ReworkTask(
            task_id="RWT-1",
            job_id="RFN-1",
            agent_id="support-agent",
            failure_kind="machine_gate",
            status="open",
        )
    ]


def test_rework_tasks_get_hits_the_detail_path() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path.endswith("/rework-tasks/RWT-1")
        return envelope({"task_id": "RWT-1", "status": "open"})

    with client_with(handler) as caliber:
        task = caliber.rework_tasks.get("RWT-1")

    assert task.task_id == "RWT-1"


def test_rework_tasks_claim_posts_with_no_body() -> None:
    seen: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["path"] = request.url.path.rsplit("/caliber", 1)[-1]
        seen["method"] = request.method
        return envelope({"task_id": "RWT-1", "status": "in_progress", "assigned_to": "@sarah"})

    with client_with(handler) as caliber:
        task = caliber.rework_tasks.claim("RWT-1")

    assert seen == {"path": "/rework-tasks/RWT-1/claim", "method": "POST"}
    assert task.status == "in_progress"


def test_rework_tasks_resolve_sends_optional_fields() -> None:
    sent: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        sent["path"] = request.url.path.rsplit("/caliber", 1)[-1]
        sent["body"] = jsonlib.loads(request.content)
        return envelope({"task_id": "RWT-1", "status": "resolved"})

    with client_with(handler) as caliber:
        task = caliber.rework_tasks.resolve(
            "RWT-1", resolution_job_id="RFN-2", resolution_notes="fixed via re-run"
        )

    assert sent["path"] == "/rework-tasks/RWT-1/resolve"
    assert sent["body"] == {"resolution_job_id": "RFN-2", "resolution_notes": "fixed via re-run"}
    assert task.status == "resolved"


def test_rework_tasks_reassign_sends_assigned_to() -> None:
    sent: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        sent["path"] = request.url.path.rsplit("/caliber", 1)[-1]
        sent["body"] = jsonlib.loads(request.content)
        return envelope({"task_id": "RWT-1", "status": "in_progress", "assigned_to": "@marcus"})

    with client_with(handler) as caliber:
        task = caliber.rework_tasks.reassign("RWT-1", "@marcus")

    assert sent["path"] == "/rework-tasks/RWT-1/reassign"
    assert sent["body"] == {"assigned_to": "@marcus"}
    assert task.assigned_to == "@marcus"


# --- quality reviews ---------------------------------------------------------


def test_quality_review_create_sends_decision_and_rationale() -> None:
    sent: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        sent["path"] = request.url.path.rsplit("/caliber", 1)[-1]
        sent["body"] = jsonlib.loads(request.content)
        return envelope(
            {
                "review_id": "QRV-1",
                "job_id": "RFN-1",
                "agent_id": "support-agent",
                "decision": "go",
                "rationale": "cites the refund policy correctly",
                "decided_by": "@qa",
            }
        )

    with client_with(handler) as caliber:
        review = caliber.quality_reviews.create(
            "RFN-1", decision="go", rationale="cites the refund policy correctly"
        )

    assert sent["path"] == "/jobs/RFN-1/quality-reviews"
    assert sent["body"] == {"decision": "go", "rationale": "cites the refund policy correctly"}
    assert review == QualityReview(
        review_id="QRV-1",
        job_id="RFN-1",
        agent_id="support-agent",
        decision="go",
        rationale="cites the refund policy correctly",
        decided_by="@qa",
    )


def test_quality_review_no_go_uses_the_same_create_call() -> None:
    """ "go" and "no_go" share one method -- the server, not the SDK, decides
    what a "no_go" does to the job."""
    sent: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        sent["body"] = jsonlib.loads(request.content)
        return envelope({"review_id": "QRV-2", "job_id": "RFN-1", "decision": "no_go"})

    with client_with(handler) as caliber:
        review = caliber.quality_reviews.create(
            "RFN-1", decision="no_go", rationale="misses a required disclaimer"
        )

    assert sent["body"] == {"decision": "no_go", "rationale": "misses a required disclaimer"}
    assert review.decision == "no_go"


def test_quality_review_list_hits_the_documented_path() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path.endswith("/jobs/RFN-1/quality-reviews")
        assert request.method == "GET"
        return envelope(
            [
                {"review_id": "QRV-2", "job_id": "RFN-1", "decision": "no_go"},
                {"review_id": "QRV-1", "job_id": "RFN-1", "decision": "go"},
            ]
        )

    with client_with(handler) as caliber:
        reviews = caliber.quality_reviews.list("RFN-1")

    assert [r.review_id for r in reviews] == ["QRV-2", "QRV-1"]


def test_a_paused_aria_plan_needs_you() -> None:
    assert decode(AriaPlan, {"status": "paused"}).needs_you
    assert not decode(AriaPlan, {"status": "running"}).needs_you


def test_waiting_on_a_plan_stops_at_paused_rather_than_polling_past_it() -> None:
    """A paused plan is a resting state: nothing changes until someone answers."""
    states = iter(["planning", "paused"])

    def handler(request: httpx.Request) -> httpx.Response:
        return envelope(
            {"plan": {"plan_id": "PLAN-1", "goal": "g", "status": next(states)}, "steps": []}
        )

    with client_with(handler) as caliber:
        detail = caliber.aria.wait_for_plan("PLAN-1", interval=0.001, max_interval=0.001, timeout=5)

    assert detail.plan.needs_you


def test_getting_a_plan_decodes_the_nested_detail_shape() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path.endswith("/aria/plans/PLAN-1")
        return envelope(
            {
                "plan": {"plan_id": "PLAN-1", "goal": "stabilize", "status": "draft"},
                "steps": [{"step_id": "STEP-1", "plan_id": "PLAN-1", "title": "Inspect"}],
            }
        )

    with client_with(handler) as caliber:
        detail = caliber.aria.get_plan("PLAN-1")

    assert detail.plan.plan_id == "PLAN-1"
    assert [step.step_id for step in detail.steps] == ["STEP-1"]


def test_listing_plan_interactions_hits_the_documented_path() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path.endswith("/aria/plans/PLAN-1/interactions")
        return envelope(
            [
                {
                    "interaction_id": "ASK-1",
                    "plan_id": "PLAN-1",
                    "step_id": "STEP-1",
                    "kind": "confirm",
                    "prompt": "Proceed?",
                    "status": "pending",
                }
            ]
        )

    with client_with(handler) as caliber:
        interactions = caliber.aria.interactions("PLAN-1")

    assert interactions[0].interaction_id == "ASK-1"


# --- integrations ---------------------------------------------------------


def test_mcp_connection_state_reports_history_not_reachability() -> None:
    """``is_connected`` says "we reached it once", not "it is up now".

    Reachability needs a probe, and ``test_connection`` is how you ask.
    """
    seen = decode(
        McpServer, {"server_id": "M-1", "last_connected_at": "2026-01-01", "connection_error": None}
    )
    assert seen.is_connected

    broken = decode(
        McpServer,
        {"server_id": "M-1", "last_connected_at": "2026-01-01", "connection_error": "refused"},
    )
    assert not broken.is_connected
    assert not decode(McpServer, {"server_id": "M-1"}).is_connected


def test_invoking_an_mcp_tool_goes_through_the_governed_path() -> None:
    """Routed through CALIBER, which is what makes policy and audit apply."""
    seen: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["path"] = request.url.path.rsplit("/caliber", 1)[-1]
        seen["body"] = jsonlib.loads(request.content)
        return envelope({"result": "ok"})

    with client_with(handler) as caliber:
        caliber.mcp_servers.invoke_tool("M-1", "search", {"q": "refund"})

    assert seen["path"] == "/mcp-servers/M-1/invoke-tool"
    assert seen["body"] == {"tool_name": "search", "arguments": {"q": "refund"}}


def test_mcp_tool_policy_update_uses_the_documented_path() -> None:
    seen: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["path"] = request.url.path.rsplit("/caliber", 1)[-1]
        seen["body"] = jsonlib.loads(request.content)
        return envelope({"policy": {"allowed": True}})

    with client_with(handler) as caliber:
        caliber.mcp_servers.update_tool_policy("M-1", "search", allowed=True)

    assert seen["path"] == "/mcp-servers/M-1/tools/search/policy"
    assert seen["body"] == {"allowed": True}


def test_mcp_tool_test_cases_and_calibration_use_the_tool_subroutes() -> None:
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(f"{request.method} {request.url.path.rsplit('/caliber', 1)[-1]}")
        return envelope({"ok": True})

    with client_with(handler) as caliber:
        caliber.mcp_servers.save_test_cases("M-1", "search", [{"name": "case", "input": {}}])
        caliber.mcp_servers.calibrate_tool("M-1", "search")

    assert seen == [
        "PUT /mcp-servers/M-1/tools/search/test-cases",
        "POST /mcp-servers/M-1/tools/search/calibrate",
    ]


def test_knowledge_base_extended_routes_hit_the_documented_paths() -> None:
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(f"{request.method} {request.url.path.rsplit('/caliber', 1)[-1]}")
        return envelope({})

    with client_with(handler) as caliber:
        caliber.knowledge_bases.versions("KB-1")
        caliber.knowledge_bases.create_version("KB-1", sources=[{"key": "docs/policy.md"}])
        caliber.knowledge_bases.activate_version("KB-1", "KBV-1")
        caliber.knowledge_bases.version("KBV-1")
        caliber.knowledge_bases.sync_version_to_age("KBV-1")
        caliber.knowledge_bases.sources("KBV-1")
        caliber.knowledge_bases.chunks("KBV-1", q="refund", source_key="docs/policy.md", limit=25)
        caliber.knowledge_bases.entities("KBV-1")
        caliber.knowledge_bases.relationships("KBV-1")
        caliber.knowledge_bases.graph("KBV-1", q="chargeback")
        caliber.knowledge_bases.run_events("KBR-1")
        caliber.knowledge_bases.test_runs("KB-1", limit=5)
        caliber.knowledge_bases.test_run("KBT-1")
        caliber.knowledge_bases.query(knowledge_base_id="KB-1", query="refund window")

    assert seen == [
        "GET /knowledge-bases/KB-1/versions",
        "POST /knowledge-bases/KB-1/versions",
        "POST /knowledge-bases/KB-1/versions/KBV-1/activate",
        "GET /knowledge-base-versions/KBV-1",
        "POST /knowledge-base-versions/KBV-1/age-sync",
        "GET /knowledge-base-versions/KBV-1/sources",
        "GET /knowledge-base-versions/KBV-1/chunks",
        "GET /knowledge-base-versions/KBV-1/entities",
        "GET /knowledge-base-versions/KBV-1/relationships",
        "GET /knowledge-base-versions/KBV-1/graph",
        "GET /knowledge-runs/KBR-1/events",
        "GET /knowledge-bases/KB-1/test-runs",
        "GET /knowledge/test-runs/KBT-1",
        "POST /knowledge/query",
    ]


def test_object_import_bridges_raw_storage_into_the_managed_registry() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path.endswith("/object/import")
        return envelope({"file_id": "F-1", "sha256": "a" * 64})

    with client_with(handler) as caliber:
        result = caliber.object_store.import_object("docs", "policy.md")

    assert result["file_id"] == "F-1"


def test_object_store_create_bucket_uses_the_server_field_name() -> None:
    sent: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        sent.update(jsonlib.loads(request.content))
        return envelope({"name": "docs"})

    with client_with(handler) as caliber:
        caliber.object_store.create_bucket("docs")

    assert sent == {"name": "docs"}


def test_object_store_listing_splits_objects_and_prefixes() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return envelope(
            {
                "bucket": "docs",
                "prefix": "",
                "prefixes": ["policies/"],
                "objects": [{"key": "readme.md", "size": 123}],
                "next_token": "next",
                "is_truncated": True,
            }
        )

    with client_with(handler) as caliber:
        objects = caliber.object_store.objects("docs")
        folders = caliber.object_store.folders("docs")
        listing = caliber.object_store.listing("docs")

    assert [obj.key for obj in objects] == ["readme.md"]
    assert folders == ["policies/"]
    assert listing["next_token"] == "next"


# --- releases -------------------------------------------------------------


def test_signoff_requires_a_rationale() -> None:
    """A signoff without a reason is not evidence of a decision."""
    sent: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        sent.update(jsonlib.loads(request.content))
        return envelope({"signoff_id": "S-1"})

    with client_with(handler) as caliber:
        caliber.releases.sign("RC-1", decision="go", rationale="all gates green")

    assert sent == {"decision": "go", "rationale": "all gates green"}


# --- cookbooks ------------------------------------------------------------


def test_cookbook_readiness_names_what_is_unmet() -> None:
    """The check list, not just the badge — a bare status names no cause."""
    recipe = decode(
        CookbookRecipe,
        {
            "id": "03",
            "title": "Policy-Safe Decision Tool",
            "readiness": {
                "status": "configuration_required",
                "checks": [
                    {"label": "Runtime approvals", "status": "configuration_required"},
                    {"label": "Model provider", "status": "ready"},
                ],
            },
        },
    )
    assert not recipe.is_ready
    assert [c["label"] for c in recipe.unmet_checks] == ["Runtime approvals"]


def test_a_ready_cookbook_has_no_unmet_checks() -> None:
    recipe = decode(CookbookRecipe, {"id": "02", "readiness": {"status": "ready", "checks": []}})
    assert recipe.is_ready
    assert recipe.unmet_checks == []


def test_cookbook_list_unwraps_the_recipes_key() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return envelope({"recipes": [{"id": "01", "title": "Intake"}], "catalog_version": "x"})

    with client_with(handler) as caliber:
        recipes = caliber.cookbooks.list()

    assert [r.id for r in recipes] == ["01"]


# --- secrets --------------------------------------------------------------


def test_secrets_are_write_only() -> None:
    """Listing returns names and metadata; the SDK exposes no value getter."""
    assert not hasattr(CaliberClient(BASE, token="t", http_client=httpx.Client()).secrets, "value")


# --- events ---------------------------------------------------------------


def test_event_stream_yields_raw_lines() -> None:
    """Unparsed on purpose: the event vocabulary grows, and a decoder would
    reject events added after this SDK shipped."""

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["accept"] == "text/event-stream"
        return httpx.Response(200, content=b"event: ping\ndata: {}\n\n")

    with client_with(handler) as caliber:
        lines = list(caliber.events.stream())

    assert "event: ping" in lines
