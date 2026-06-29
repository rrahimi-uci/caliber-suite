"""Integration tests for the plan-to-build route.

``POST /workflow-versions/{id}/plan-build`` turns a plain-language goal into a
proposed workflow manifest, grounded in the registry, and returns a graph diff
for the same accept/reject UI the copilot uses. With the default ``fake``
provider the manifest comes back unchanged — a safe no-op — so the Plan tab
works without an LLM configured. A ``build_provider`` monkeypatch exercises the
real "workflow authored" and "bad proposal" paths deterministically.
"""

from __future__ import annotations

import pytest
from starlette.testclient import TestClient

from caliber.llm.fake import FakeLLMProvider
from caliber.llm.provider import LLMUsage, WorkflowEdit, WorkflowGenerationContext
from tests.workflow_helpers import (
    PREFIX,
    create_draft,
    create_workflow,
    make_manifest,
    register_demo_tools,
)


def _seed_version(client: TestClient) -> tuple[str, str]:
    wid = create_workflow(client)
    vid, _ = create_draft(client, wid, make_manifest(wid))
    return wid, vid


def _build(client: TestClient, vid: str, **body: object) -> object:
    return client.post(f"{PREFIX}/workflow-versions/{vid}/plan-build", json=body)


# --------------------------------------------------------------------------
# Default (fake provider): unchanged manifest, no footgun
# --------------------------------------------------------------------------


def test_plan_build_returns_unchanged_manifest_with_fake_provider(client: TestClient) -> None:
    _wid, vid = _seed_version(client)
    r = _build(client, vid, goal="a 3-step support triage workflow")
    assert r.status_code == 200, r.text
    data = r.json()["data"]
    # No-footgun: the proposal is the base, so the diff is empty and accepting
    # it is a safe no-op rather than scaffolding a canned graph.
    assert data["graph_diff"]["empty"] is True
    assert "No LLM" in data["summary"]
    assert data["valid"] is True


def test_plan_build_reports_grounding_from_registry(client: TestClient) -> None:
    register_demo_tools(client)
    _wid, vid = _seed_version(client)
    r = _build(client, vid, goal="answer support questions using the policy lookup")
    assert r.status_code == 200, r.text
    grounding = r.json()["data"]["grounding"]
    assert set(grounding) == {"tools", "skills", "eval_datasets"}
    # Registered demo tools are surfaced so the model authors real refs.
    assert "lookup_policy" in grounding["tools"]


def test_plan_build_uses_payload_manifest_as_base(client: TestClient) -> None:
    """An in-progress (unsaved) canvas passed in the body is the diff base."""
    wid, vid = _seed_version(client)
    edited = make_manifest(wid)
    edited["nodes"]["scratch"] = {"id": "scratch", "type": "note", "text": "wip"}
    r = _build(client, vid, goal="tidy up", manifest=edited)
    assert r.status_code == 200, r.text
    data = r.json()["data"]
    # Fake echoes the base back unchanged → the scratch note survives.
    assert "scratch" in data["proposed_manifest"]["nodes"]
    assert data["graph_diff"]["empty"] is True


# --------------------------------------------------------------------------
# Real generation path (monkeypatched provider): diff + validation surfaced
# --------------------------------------------------------------------------


def _provider_that(callable_):
    def _factory(_config: object) -> FakeLLMProvider:
        return FakeLLMProvider(gen_callable=callable_)

    return _factory


def test_plan_build_surfaces_an_authored_workflow(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    def _authors(ctx: WorkflowGenerationContext) -> tuple[WorkflowEdit, LLMUsage]:
        manifest = dict(ctx.manifest)
        nodes = dict(manifest["nodes"])
        nodes["triage"] = {"id": "triage", "type": "note", "text": "classify"}
        manifest["nodes"] = nodes
        return (
            WorkflowEdit(manifest=manifest, summary="Authored a triage step", rationale="why"),
            LLMUsage(input_tokens=12, output_tokens=6, cost_usd=0.002),
        )

    monkeypatch.setattr("caliber.routes.workflow_versions.build_provider", _provider_that(_authors))
    _wid, vid = _seed_version(client)
    r = _build(client, vid, goal="build a triage workflow")
    assert r.status_code == 200, r.text
    data = r.json()["data"]
    assert data["summary"] == "Authored a triage step"
    assert data["graph_diff"]["empty"] is False
    added = {n["id"] for n in data["graph_diff"]["added_nodes"]}
    assert "triage" in added
    assert "triage" in data["proposed_manifest"]["nodes"]
    assert data["valid"] is True
    assert data["usage"]["input_tokens"] == 12


def test_plan_build_passes_goal_to_provider(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The plain-language goal — not an instruction — reaches the provider."""
    seen: list[WorkflowGenerationContext] = []

    def _capture(ctx: WorkflowGenerationContext) -> tuple[WorkflowEdit, LLMUsage]:
        seen.append(ctx)
        return WorkflowEdit(manifest=dict(ctx.manifest), summary="", rationale=""), LLMUsage()

    monkeypatch.setattr("caliber.routes.workflow_versions.build_provider", _provider_that(_capture))
    _wid, vid = _seed_version(client)
    r = _build(client, vid, goal="summarize PDFs then extract entities")
    assert r.status_code == 200, r.text
    assert seen and seen[0].goal == "summarize PDFs then extract entities"


def test_plan_build_reports_invalid_proposal_without_blocking(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    def _adds_unresolved_tool(ctx: WorkflowGenerationContext) -> tuple[WorkflowEdit, LLMUsage]:
        manifest = dict(ctx.manifest)
        nodes = {k: dict(v) for k, v in manifest["nodes"].items()}
        nodes["agent"]["tools"] = ["ghost_tool"]
        manifest["nodes"] = nodes
        return WorkflowEdit(manifest=manifest, summary="use a ghost tool", rationale=""), LLMUsage()

    monkeypatch.setattr(
        "caliber.routes.workflow_versions.build_provider", _provider_that(_adds_unresolved_tool)
    )
    _wid, vid = _seed_version(client)
    r = _build(client, vid, goal="make the agent use ghost_tool")
    assert r.status_code == 200, r.text
    assert r.json()["data"]["valid"] is False


def test_plan_build_422_on_unparseable_proposal(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    def _garbage(_ctx: WorkflowGenerationContext) -> tuple[WorkflowEdit, LLMUsage]:
        return WorkflowEdit(manifest={"garbage": True}, summary="", rationale=""), LLMUsage()

    monkeypatch.setattr("caliber.routes.workflow_versions.build_provider", _provider_that(_garbage))
    _wid, vid = _seed_version(client)
    r = _build(client, vid, goal="produce garbage")
    assert r.status_code == 422


# --------------------------------------------------------------------------
# Request validation + auth
# --------------------------------------------------------------------------


def test_plan_build_400_on_empty_goal(client: TestClient) -> None:
    _wid, vid = _seed_version(client)
    r = _build(client, vid, goal="")
    assert r.status_code == 400


def test_plan_build_404_on_unknown_version(client: TestClient) -> None:
    r = _build(client, "wfv_does_not_exist", goal="hi")
    assert r.status_code == 404


def test_plan_build_requires_operator_scope(client: TestClient) -> None:
    _wid, vid = _seed_version(client)
    r = client.post(
        f"{PREFIX}/workflow-versions/{vid}/plan-build",
        json={"goal": "build something"},
        headers={"X-CALIBER-User": "@viewer"},
    )
    assert r.status_code == 403
