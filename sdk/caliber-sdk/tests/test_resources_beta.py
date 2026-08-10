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
from caliber_sdk.models import AriaPlan, CookbookRecipe, Job, McpServer, decode

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


def test_a_paused_aria_plan_needs_you() -> None:
    assert decode(AriaPlan, {"status": "paused"}).needs_you
    assert not decode(AriaPlan, {"status": "running"}).needs_you


def test_waiting_on_a_plan_stops_at_paused_rather_than_polling_past_it() -> None:
    """A paused plan is a resting state: nothing changes until someone answers."""
    states = iter(["planning", "paused"])

    def handler(request: httpx.Request) -> httpx.Response:
        return envelope({"plan_id": "PLAN-1", "goal": "g", "status": next(states)})

    with client_with(handler) as caliber:
        plan = caliber.aria.wait_for_plan("PLAN-1", interval=0.001, max_interval=0.001, timeout=5)

    assert plan.needs_you


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


def test_object_import_bridges_raw_storage_into_the_managed_registry() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path.endswith("/object/import")
        return envelope({"file_id": "F-1", "sha256": "a" * 64})

    with client_with(handler) as caliber:
        result = caliber.object_store.import_object("docs", "policy.md")

    assert result["file_id"] == "F-1"


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
