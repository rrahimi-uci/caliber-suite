"""Integration tests for the in-canvas copilot edit route (P4).

``POST /workflow-versions/{id}/copilot-edit`` turns a natural-language
instruction into a proposed manifest, grounded in the registry, and returns a
graph diff for an accept/reject UI. With the default ``fake`` provider the
manifest comes back unchanged — a safe no-op — so the dock works without an LLM
configured. A ``build_provider`` monkeypatch exercises the real "edit applied"
and "bad proposal" paths deterministically.
"""

from __future__ import annotations

import pytest
from starlette.testclient import TestClient

from caliber.llm.fake import FakeLLMProvider
from caliber.llm.provider import LLMUsage, WorkflowEdit, WorkflowEditContext
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


def _edit(client: TestClient, vid: str, **body: object) -> object:
    return client.post(f"{PREFIX}/workflow-versions/{vid}/copilot-edit", json=body)


# --------------------------------------------------------------------------
# Default (fake provider): unchanged manifest, no footgun
# --------------------------------------------------------------------------


def test_copilot_edit_returns_unchanged_manifest_with_fake_provider(client: TestClient) -> None:
    _wid, vid = _seed_version(client)
    r = _edit(client, vid, instruction="add a PII guardrail after the agent")
    assert r.status_code == 200, r.text
    data = r.json()["data"]
    # No-footgun: the proposal is the base, so the diff is empty and accepting
    # it is a safe no-op rather than replacing the canvas with a stub.
    assert data["graph_diff"]["empty"] is True
    assert "No LLM" in data["summary"]
    assert data["valid"] is True
    assert set(data["proposed_manifest"]["nodes"]) == {"start", "agent", "final"}


def test_copilot_edit_reports_grounding_from_registry(client: TestClient) -> None:
    register_demo_tools(client)
    _wid, vid = _seed_version(client)
    r = _edit(client, vid, instruction="use the lookup_policy tool")
    assert r.status_code == 200, r.text
    grounding = r.json()["data"]["grounding"]
    assert set(grounding) == {"tools", "skills", "eval_datasets"}
    # The registered demo tools are surfaced so the model references real refs.
    assert "lookup_policy" in grounding["tools"]
    assert "get_order" in grounding["tools"]


def test_copilot_edit_uses_payload_manifest_as_base(client: TestClient) -> None:
    """An in-progress (unsaved) canvas passed in the body is the base, not the
    stored version manifest."""
    wid, vid = _seed_version(client)
    edited = make_manifest(wid)
    edited["nodes"]["scratch"] = {"id": "scratch", "type": "note", "text": "wip"}
    r = _edit(client, vid, instruction="tidy up", manifest=edited)
    assert r.status_code == 200, r.text
    data = r.json()["data"]
    # Fake echoes the base back unchanged → the scratch note survives and the
    # diff is empty (proposed == provided base).
    assert "scratch" in data["proposed_manifest"]["nodes"]
    assert data["graph_diff"]["empty"] is True


# --------------------------------------------------------------------------
# Real edit path (monkeypatched provider): diff + validation surfaced
# --------------------------------------------------------------------------


def _provider_that(callable_):
    def _factory(_config: object) -> FakeLLMProvider:
        return FakeLLMProvider(edit_callable=callable_)

    return _factory


def test_copilot_edit_surfaces_an_applied_edit(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    def _adds_note(ctx: WorkflowEditContext) -> tuple[WorkflowEdit, LLMUsage]:
        manifest = dict(ctx.manifest)
        nodes = dict(manifest["nodes"])
        nodes["copilot_note"] = {"id": "copilot_note", "type": "note", "text": "added"}
        manifest["nodes"] = nodes
        return (
            WorkflowEdit(manifest=manifest, summary="Add an explanatory note", rationale="why"),
            LLMUsage(input_tokens=10, output_tokens=5, cost_usd=0.001),
        )

    monkeypatch.setattr(
        "caliber.routes.workflow_versions.build_provider", _provider_that(_adds_note)
    )
    _wid, vid = _seed_version(client)
    r = _edit(client, vid, instruction="add a note explaining the agent")
    assert r.status_code == 200, r.text
    data = r.json()["data"]
    assert data["summary"] == "Add an explanatory note"
    assert data["graph_diff"]["empty"] is False
    added = {n["id"] for n in data["graph_diff"]["added_nodes"]}
    assert "copilot_note" in added
    assert "copilot_note" in data["proposed_manifest"]["nodes"]
    assert data["valid"] is True
    assert data["usage"]["input_tokens"] == 10


def test_copilot_edit_reports_invalid_proposal_without_blocking(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A proposal that parses but fails semantic validation is returned with
    valid=False (the UI decides), not rejected outright."""

    def _adds_unresolved_tool(ctx: WorkflowEditContext) -> tuple[WorkflowEdit, LLMUsage]:
        manifest = dict(ctx.manifest)
        nodes = {k: dict(v) for k, v in manifest["nodes"].items()}
        # Structurally fine (a list of strings) but the ref doesn't resolve in
        # the registry → a semantic *error*, not a parse error.
        nodes["agent"]["tools"] = ["ghost_tool"]
        manifest["nodes"] = nodes
        return WorkflowEdit(manifest=manifest, summary="use a ghost tool", rationale=""), LLMUsage()

    monkeypatch.setattr(
        "caliber.routes.workflow_versions.build_provider", _provider_that(_adds_unresolved_tool)
    )
    _wid, vid = _seed_version(client)
    r = _edit(client, vid, instruction="make the agent use ghost_tool")
    # Parses (so it's diffable) but doesn't validate — surfaced, not 4xx'd.
    assert r.status_code == 200, r.text
    assert r.json()["data"]["valid"] is False


def test_copilot_edit_422_on_unparseable_proposal(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    def _garbage(_ctx: WorkflowEditContext) -> tuple[WorkflowEdit, LLMUsage]:
        return WorkflowEdit(manifest={"garbage": True}, summary="", rationale=""), LLMUsage()

    monkeypatch.setattr("caliber.routes.workflow_versions.build_provider", _provider_that(_garbage))
    _wid, vid = _seed_version(client)
    r = _edit(client, vid, instruction="destroy the manifest")
    assert r.status_code == 422


# --------------------------------------------------------------------------
# Request validation + auth
# --------------------------------------------------------------------------


def test_copilot_edit_400_on_empty_instruction(client: TestClient) -> None:
    _wid, vid = _seed_version(client)
    r = _edit(client, vid, instruction="")
    assert r.status_code == 400


def test_copilot_edit_404_on_unknown_version(client: TestClient) -> None:
    r = _edit(client, "wfv_does_not_exist", instruction="hi")
    assert r.status_code == 404


def test_copilot_edit_requires_operator_scope(client: TestClient) -> None:
    _wid, vid = _seed_version(client)
    r = client.post(
        f"{PREFIX}/workflow-versions/{vid}/copilot-edit",
        json={"instruction": "add a guardrail"},
        headers={"X-CALIBER-User": "@viewer"},
    )
    assert r.status_code == 403
