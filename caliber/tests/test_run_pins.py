"""The worker executes what a run was pinned to, or fails it by name (B02.2b, #683)."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

import pytest
from sqlalchemy.orm import Session
from starlette.testclient import TestClient

from caliber.db.models import (
    CaliberProject,
    CaliberProjectMember,
    CaliberRunManifest,
    CaliberWorkflowRun,
    CaliberWorkflowVersion,
)
from caliber.orchestrator.workflow_run_worker import WorkflowRunWorker
from caliber.workflows import run_pins
from caliber.workflows.run_pins import PinsError, load_run_manifest, verify_run_pins
from tests.workflow_helpers import (
    PREFIX,
    create_draft,
    create_workflow,
    make_support_manifest,
    register_demo_tools,
)

PROJECT = "P-pins"


def _manifest(workflow_id: str, text: str = "Help the customer.") -> dict[str, Any]:
    manifest = make_support_manifest(workflow_id)
    manifest["artifacts"] = {}
    manifest["nodes"]["support_agent"]["instructions"] = {"type": "inline", "text": text}
    return manifest


def _setup(
    client: TestClient, db: Session, *, admission: bool = True, mcp: bool = False
) -> tuple[str, str]:
    client.app.state.config = client.app.state.config.model_copy(
        update={
            "workflow_run_queue_enabled": True,
            "admission_enabled": admission,
        }
    )
    db.add(CaliberProject(project_id=PROJECT, name="pins", owner="@test"))
    db.flush()
    db.add(
        CaliberProjectMember(
            member_id="M-pins",
            project_id=PROJECT,
            user_id="@test",
            role="owner",
            created_by="@test",
        )
    )
    db.commit()
    client.headers.update({"X-CALIBER-Project": PROJECT})
    register_demo_tools(client)
    workflow_id = create_workflow(client, "Pins WF")
    manifest = _manifest(workflow_id)
    if mcp:
        manifest["tools"]["fetch"] = {
            "type": "mcp_tool",
            "server_id": "MCP-pins",
            "tool_name": "fetch",
        }
        manifest["nodes"]["support_agent"]["tools"] = ["fetch"]
    version_id, _ = create_draft(client, workflow_id, manifest)
    published = client.post(f"{PREFIX}/workflow-versions/{version_id}/publish")
    assert published.status_code == 200, published.text
    return workflow_id, version_id


def _worker(client: TestClient) -> WorkflowRunWorker:
    return WorkflowRunWorker(
        session_factory=client.app.state.session_factory,
        config=client.app.state.config,
        event_bus=getattr(client.app.state, "event_bus", None),
    )


def _submit(client: TestClient, workflow_id: str, key: str = "k") -> str:
    created = client.post(
        f"{PREFIX}/workflow-runs",
        json={"workflow_id": workflow_id, "alias": "manual", "input": "hi", "idempotency_key": key},
    )
    assert created.status_code == 202, created.text
    return str(created.json()["data"]["workflow_run_id"])


def _run(db: Session, run_id: str) -> CaliberWorkflowRun:
    db.expire_all()
    run = db.get(CaliberWorkflowRun, run_id)
    assert run is not None
    return run


def test_an_admitted_run_executes_and_keeps_what_admission_recorded(
    client: TestClient, db_session: Session
) -> None:
    workflow_id, _ = _setup(client, db_session)
    run_id = _submit(client, workflow_id)
    _worker(client)._tick()
    run = _run(db_session, run_id)
    assert run.status == "completed", (run.error_code, run.error_summary)
    summary = run.summary or {}
    assert summary["admitted"] is True and summary["manifest_id"]
    assert summary["admission_intent"]["workflow_id"] == workflow_id, (
        "kept through the worker's summary rebuild"
    )
    assert summary["authoring_manifest_hash"]


def test_a_legacy_run_keeps_todays_path(client: TestClient, db_session: Session) -> None:
    workflow_id, _ = _setup(client, db_session, admission=False)
    run_id = _submit(client, workflow_id)
    _worker(client)._tick()
    run = _run(db_session, run_id)
    assert run.manifest_legacy is True and run.status == "completed"


def test_a_version_recompiled_after_admission_fails_the_run_by_name(
    client: TestClient, db_session: Session
) -> None:
    workflow_id, version_id = _setup(client, db_session)
    run_id = _submit(client, workflow_id)
    version = db_session.get(CaliberWorkflowVersion, version_id)
    assert version is not None
    from caliber.workflows.deployment_bundle import seal_bundle

    bundle = dict(version.compiled_bundle["deployment_bundle"])
    bundle["portability"] = {"changed": True}  # any change re-seals to another digest
    version.compiled_bundle = {**version.compiled_bundle, "deployment_bundle": seal_bundle(bundle)}
    db_session.commit()
    _worker(client)._tick()
    run = _run(db_session, run_id)
    assert (run.status, run.error_code) == ("failed", "pins_changed"), run.error_summary
    assert "recompiled" in (run.error_summary or "")


def test_a_version_that_no_longer_exists_fails_the_run(
    client: TestClient, db_session: Session
) -> None:
    workflow_id, version_id = _setup(client, db_session)
    run_id = _submit(client, workflow_id)
    version = db_session.get(CaliberWorkflowVersion, version_id)
    assert version is not None
    version.compiled_bundle = None
    db_session.commit()
    _worker(client)._tick()
    assert _run(db_session, run_id).error_code == "pins_changed"


def test_a_snapshot_that_is_not_the_pinned_bundles_fails_the_run(
    client: TestClient, db_session: Session
) -> None:
    workflow_id, _ = _setup(client, db_session)
    run_id = _submit(client, workflow_id)
    run = _run(db_session, run_id)
    snapshot = dict(run.manifest_snapshot)
    snapshot["name"] = "swapped after admission"
    run.manifest_snapshot = snapshot
    db_session.commit()
    _worker(client)._tick()
    assert _run(db_session, run_id).error_code == "pins_mismatch"


def test_another_provider_is_not_a_pinned_model_route(
    client: TestClient, db_session: Session
) -> None:
    workflow_id, _ = _setup(client, db_session)
    run_id = _submit(client, workflow_id)
    client.app.state.config = client.app.state.config.model_copy(update={"llm_provider": "openai"})
    _worker(client)._tick()
    run = _run(db_session, run_id)
    assert (run.status, run.error_code) == ("failed", "model_route_changed"), run.error_summary


def test_a_different_workflow_version_than_the_pinned_one_fails(
    client: TestClient, db_session: Session
) -> None:
    workflow_id, _ = _setup(client, db_session)
    run_id = _submit(client, workflow_id)
    second, _ = create_draft(client, workflow_id, _manifest(workflow_id, "another"))
    assert client.post(f"{PREFIX}/workflow-versions/{second}/publish").status_code == 200
    run = _run(db_session, run_id)
    run.workflow_version_id = second
    db_session.commit()
    _worker(client)._tick()
    assert _run(db_session, run_id).error_code == "pins_mismatch"


def test_an_admitted_run_without_a_manifest_is_never_dispatched(
    client: TestClient, db_session: Session
) -> None:
    workflow_id, version_id = _setup(client, db_session)
    db_session.add(
        CaliberWorkflowRun(
            workflow_run_id="WR-partial",
            workflow_id=workflow_id,
            project_id=PROJECT,
            workflow_version_id=version_id,
            status="queued",
            manifest_legacy=False,
            input_payload="x",
        )
    )
    db_session.commit()
    _worker(client)._tick()
    run = _run(db_session, "WR-partial")
    assert (run.status, run.error_code) == ("failed", "manifest_missing")


def test_a_manifest_that_does_not_read_back_or_names_another_run_fails(
    client: TestClient, db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    workflow_id, _ = _setup(client, db_session)
    run_id = _submit(client, workflow_id)
    assert load_run_manifest(db_session, run_id) is not None
    assert load_run_manifest(db_session, "WR-none") is None

    from caliber.run_manifest import ManifestError

    def unreadable(*_a: Any, **_k: Any) -> Any:
        raise ManifestError("invalid_manifest", "bytes changed")

    with monkeypatch.context() as patch:
        patch.setattr(run_pins, "read_manifest", unreadable)
        with pytest.raises(PinsError) as caught:
            load_run_manifest(db_session, run_id)
    assert caught.value.code == "manifest_corrupt"

    real = run_pins.read_manifest
    with monkeypatch.context() as patch:
        patch.setattr(
            run_pins,
            "read_manifest",
            lambda raw, digest=None: {**real(raw, digest), "workflow_run_id": "WR-other"},
        )
        with pytest.raises(PinsError, match="another run"):
            load_run_manifest(db_session, run_id)


def test_verify_returns_the_pins_for_an_intact_run_and_none_for_a_legacy_one(
    client: TestClient, db_session: Session
) -> None:
    workflow_id, version_id = _setup(client, db_session)
    run_id = _submit(client, workflow_id)
    run = _run(db_session, run_id)
    version = db_session.get(CaliberWorkflowVersion, version_id)
    pins = verify_run_pins(
        db_session,
        run,
        version,
        run.manifest_snapshot,
        llm_provider="fake",
        default_model=client.app.state.config.llm_diagnosis_model,
    )
    assert pins is not None and pins.pins["workflow_version"] == version_id
    assert pins.manifest_id == (run.summary or {})["manifest_id"]
    legacy = CaliberWorkflowRun(
        workflow_run_id="WR-leg", workflow_id=workflow_id, manifest_legacy=True
    )
    assert verify_run_pins(db_session, legacy, version, None, llm_provider="fake") is None
    assert db_session.query(CaliberRunManifest).count() == 1


# ---------------------------------------------------------------- what the bundle names but does not contain (#961 review)


def _tool_row(db: Session, name: str = "escalate") -> Any:
    from caliber.db.models import CaliberToolRegistry

    row = db.query(CaliberToolRegistry).filter_by(name=name).first()
    assert row is not None
    return row


def test_a_registered_tool_edited_after_admission_fails_the_run_by_name(
    client: TestClient, db_session: Session
) -> None:
    """The registry row can be PATCHed in place (side-effect level, approval, module): the pin must notice."""
    workflow_id, _ = _setup(client, db_session)
    run_id = _submit(client, workflow_id)
    tool = _tool_row(db_session)
    assert tool.requires_approval is True
    tool.requires_approval = False
    tool.side_effect_level = "read"
    db_session.commit()
    _worker(client)._tick()
    run = _run(db_session, run_id)
    assert (run.status, run.error_code) == ("failed", "pins_changed"), run.error_summary
    assert "escalate" in (run.error_summary or "") and "changed after admission" in (
        run.error_summary or ""
    )


@pytest.mark.parametrize(
    ("column", "value"),
    [("module_path", "evil.module"), ("callable_name", "other"), ("secret_refs", ["NEW_SECRET"])],
)
def test_any_pinned_tool_field_changing_is_a_pin_change(
    client: TestClient, db_session: Session, column: str, value: Any
) -> None:
    workflow_id, _ = _setup(client, db_session)
    run_id = _submit(client, workflow_id)
    setattr(_tool_row(db_session, "lookup_policy"), column, value)
    db_session.commit()
    _worker(client)._tick()
    assert _run(db_session, run_id).error_code == "pins_changed"


def test_a_tool_that_is_gone_after_admission_fails_the_run(
    client: TestClient, db_session: Session
) -> None:
    workflow_id, _ = _setup(client, db_session)
    run_id = _submit(client, workflow_id)
    _tool_row(db_session, "get_order").status = "archived"
    db_session.commit()
    _worker(client)._tick()
    run = _run(db_session, run_id)
    assert run.error_code == "pins_changed" and "get_order" in (run.error_summary or "")


def test_the_default_model_an_inheriting_agent_gets_is_pinned(
    client: TestClient, db_session: Session
) -> None:
    workflow_id, _ = _setup(client, db_session)
    run_id = _submit(client, workflow_id)
    client.app.state.config = client.app.state.config.model_copy(
        update={"llm_diagnosis_model": "gpt-other"}
    )
    _worker(client)._tick()
    run = _run(db_session, run_id)
    assert (run.status, run.error_code) == ("failed", "model_route_changed"), run.error_summary
    assert "default model" in (run.error_summary or "")


def _mcp_workflow(client: TestClient, db: Session) -> tuple[str, str]:
    from caliber.db.models import CaliberMcpServer

    db.add(
        CaliberMcpServer(
            server_id="MCP-pins",
            name="pins",
            owner="@test",
            project_id=PROJECT,
            visibility="project",
            transport="streamable-http",
            last_connected_at=datetime.now(timezone.utc),
            uri="https://mcp.example.invalid/mcp",
            status="active",
            discovered_tools=[
                {"name": "fetch", "description": "d", "input_schema": {"type": "object"}}
            ],
            tool_policies={
                "fetch": {"allowed": True, "side_effect_level": "read", "requires_approval": False}
            },
        )
    )
    db.commit()
    return _setup(client, db, mcp=True)


def test_an_mcp_tool_whose_schema_changed_after_admission_fails_the_run(
    client: TestClient, db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CALIBER_MCP_REMOTE_HOST_ALLOWLIST", "mcp.example.invalid")
    from caliber.db.models import CaliberMcpServer

    workflow_id, _ = _mcp_workflow(client, db_session)
    run_id = _submit(client, workflow_id)
    server = db_session.get(CaliberMcpServer, "MCP-pins")
    assert server is not None
    server.discovered_tools = [
        {"name": "fetch", "description": "changed", "input_schema": {"type": "object"}}
    ]
    db_session.commit()
    _worker(client)._tick()
    run = _run(db_session, run_id)
    assert (run.status, run.error_code) == ("failed", "pins_changed"), run.error_summary
    assert "MCP tool" in (run.error_summary or "")


def test_an_mcp_tool_policy_or_server_removed_after_admission_fails_the_run(
    client: TestClient, db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CALIBER_MCP_REMOTE_HOST_ALLOWLIST", "mcp.example.invalid")
    from caliber.db.models import CaliberMcpServer

    workflow_id, _ = _mcp_workflow(client, db_session)
    run_id = _submit(client, workflow_id)
    server = db_session.get(CaliberMcpServer, "MCP-pins")
    assert server is not None
    server.tool_policies = {
        "fetch": {"allowed": True, "side_effect_level": "read", "requires_approval": True}
    }
    db_session.commit()
    _worker(client)._tick()
    assert _run(db_session, run_id).error_code == "pins_changed"


def _child_parent(client: TestClient, db: Session) -> tuple[str, str, str, str]:
    """A published child (an agent with a skill) and a published parent that calls it."""
    from caliber.db.models import CaliberSkill

    db.add(
        CaliberSkill(
            skill_id="SK-child",
            name="child-skill",
            description="d",
            content="CHILD-SKILL-ONE",
            owner="@test",
            project_id=PROJECT,
            visibility="project",
            status="active",
            version=1,
        )
    )
    db.commit()
    client.app.state.config = client.app.state.config.model_copy(
        update={"workflow_run_queue_enabled": True, "admission_enabled": True}
    )
    db.add(CaliberProject(project_id=PROJECT, name="pins", owner="@test"))
    db.flush()
    db.add(
        CaliberProjectMember(
            member_id="M-pins",
            project_id=PROJECT,
            user_id="@test",
            role="owner",
            created_by="@test",
        )
    )
    db.commit()
    client.headers.update({"X-CALIBER-Project": PROJECT})
    from tests.workflow_helpers import make_manifest

    child_id = create_workflow(client, "Child WF")
    child_manifest = make_manifest(child_id)
    child_manifest["nodes"]["agent"]["skills"] = ["child-skill"]
    child_version, _ = create_draft(client, child_id, child_manifest)
    assert client.post(f"{PREFIX}/workflow-versions/{child_version}/publish").status_code == 200

    parent_id = create_workflow(client, "Parent WF")
    parent = make_manifest(parent_id)
    parent["nodes"]["agent"] = {
        "id": "agent",
        "type": "subworkflow",
        "workflow_id": child_id,
        "alias": "manual",
        "version_id": child_version,
        "inputs": {"input": {"type": "string"}},
        "outputs": {"final_output": {"type": "string"}},
    }
    parent_version, _ = create_draft(client, parent_id, parent)
    published = client.post(f"{PREFIX}/workflow-versions/{parent_version}/publish")
    assert published.status_code == 200, published.text
    return parent_id, parent_version, child_id, child_version


def test_a_child_workflow_recompiled_after_admission_fails_the_parent_run(
    client: TestClient, db_session: Session
) -> None:
    from caliber.db.models import CaliberSkill

    parent_id, _, _, child_version = _child_parent(client, db_session)
    run_id = _submit(client, parent_id)
    skill = db_session.get(CaliberSkill, "SK-child")
    assert skill is not None
    skill.content, skill.version = "CHILD-SKILL-TWO", 2
    db_session.commit()
    assert client.post(f"{PREFIX}/workflow-versions/{child_version}/compile").status_code == 200
    _worker(client)._tick()
    run = _run(db_session, run_id)
    assert (run.status, run.error_code) == ("failed", "pins_changed"), run.error_summary
    assert "child workflow version" in (run.error_summary or "")


def test_a_child_workflow_that_is_not_published_cannot_be_what_a_run_was_pinned_to(
    client: TestClient, db_session: Session
) -> None:
    parent_id, _, _, child_version = _child_parent(client, db_session)
    run_id = _submit(client, parent_id)
    child = db_session.get(CaliberWorkflowVersion, child_version)
    assert child is not None
    child.status = "draft"
    db_session.commit()
    _worker(client)._tick()
    assert _run(db_session, run_id).error_code == "pins_changed"


def test_a_child_with_no_sealed_bundle_is_refused_at_admission(
    client: TestClient, db_session: Session
) -> None:
    parent_id, parent_version, _, _ = _child_parent(client, db_session)
    version = db_session.get(CaliberWorkflowVersion, parent_version)
    assert version is not None
    bundle = version.compiled_bundle["deployment_bundle"]
    for item in bundle["dependencies"]:
        if item["kind"] == "subworkflow":
            item["snapshot"].pop("bundle_digest", None)
    from caliber.workflows.deployment_bundle import seal_bundle

    version.compiled_bundle = {**version.compiled_bundle, "deployment_bundle": seal_bundle(bundle)}
    db_session.commit()
    refused = client.post(
        f"{PREFIX}/workflow-runs",
        json={"workflow_id": parent_id, "alias": "manual", "idempotency_key": "x"},
    )
    assert refused.status_code == 409 and "child workflow has no sealed bundle" in refused.text


def test_an_intact_child_workflow_runs(client: TestClient, db_session: Session) -> None:
    parent_id, _, _, _ = _child_parent(client, db_session)
    run_id = _submit(client, parent_id)
    _worker(client)._tick()
    run = _run(db_session, run_id)
    assert run.error_code is None, run.error_summary


def test_the_external_checks_skip_what_has_no_snapshot_and_bound_the_child_tree(
    client: TestClient, db_session: Session
) -> None:
    workflow_id, _ = _setup(client, db_session)
    # Dependencies without a snapshot (an unresolved or snapshot-less lock entry) are not this check's business.
    run_pins._verify_external(
        db_session,
        workflow_id,
        {"dependencies": ["x", {"kind": "tool"}, {"kind": "eval_dataset"}]},
        depth=0,
        seen=set(),
    )
    item = {"snapshot": {"version_id": "WFV-x", "bundle_digest": "d" * 64}}
    with pytest.raises(PinsError, match="nested too deeply"):
        run_pins._verify_child(db_session, item, depth=run_pins._MAX_CHILD_DEPTH, seen=set())
    run_pins._verify_child(
        db_session, item, depth=0, seen={"WFV-x"}
    )  # already verified on this walk: a cycle ends
