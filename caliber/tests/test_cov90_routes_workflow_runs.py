"""Coverage-focused tests for ``caliber.routes.workflow_runs``.

These target specific error branches, defensive/edge-case predicate helpers,
and pure-function fallbacks that ``tests/test_routes_workflow_runs_async.py``
(the primary route-behavior suite) does not exercise. Where a branch is only
reachable by calling a private module-level helper directly (because every
HTTP call site already guards the condition before invoking it — confirmed by
reading the route bodies), this file imports and calls that helper directly
with real model instances. That is still a real assertion about the module's
behavior; it is simply not routed through the ASGI layer because no HTTP path
can reach the branch.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from sqlalchemy.orm import Session
from starlette.exceptions import HTTPException
from starlette.testclient import TestClient

import caliber.routes.workflow_runs as wr_routes
from caliber.db.models import (
    CaliberProject,
    CaliberRuntimeApprovalRequest,
    CaliberWorkflow,
    CaliberWorkflowDeployment,
    CaliberWorkflowRun,
    CaliberWorkflowRunCheckpoint,
    CaliberWorkflowVersion,
)
from caliber.ids import (
    new_project_id,
    new_runtime_approval_id,
    new_workflow_run_checkpoint_id,
    new_workflow_run_id,
)
from tests.workflow_helpers import (
    PREFIX,
    create_and_publish,
    create_draft,
    create_workflow,
    make_manifest,
)

# ---------------------------------------------------------------------------
# Local helpers (mirrors tests/test_routes_workflow_runs_async.py + workflow_helpers.py)
# ---------------------------------------------------------------------------


def _enable_queue(client: TestClient) -> None:
    client.app.state.config = client.app.state.config.model_copy(
        update={"workflow_run_queue_enabled": True}
    )


def _enable_queue_and_checkpointing(client: TestClient) -> None:
    client.app.state.config = client.app.state.config.model_copy(
        update={
            "workflow_run_queue_enabled": True,
            "workflow_run_checkpointing_enabled": True,
        }
    )


def _enable_queue_approvals_and_checkpointing(client: TestClient) -> None:
    client.app.state.config = client.app.state.config.model_copy(
        update={
            "workflow_run_queue_enabled": True,
            "workflow_run_runtime_approvals_enabled": True,
            "workflow_run_checkpointing_enabled": True,
        }
    )


def _wait_event_manifest(workflow_id: str, node_id: str = "wait_gate") -> dict:
    manifest = make_manifest(workflow_id)
    manifest["edges"] = [
        {"id": "e1", "from": "start", "to": node_id, "map": {"msg": "input"}},
        {"id": "e2", "from": node_id, "to": "final", "map": {"output": "response"}},
    ]
    manifest["nodes"][node_id] = {
        "id": node_id,
        "type": "wait_for_event",
        "event_name": "ticket.approved",
        "inputs": {"input": {"type": "string"}},
        "outputs": {
            "output": {"type": "string"},
            "event_payload": {"type": "structured"},
            "event_name": {"type": "string"},
        },
    }
    return manifest


def _seed_deployed_workflow(
    db_session: Session,
    *,
    workflow_id: str,
    trigger: dict | None = None,
    alias: str = "prod",
    workflow_status: str = "active",
    deployment_status: str = "active",
    version_id: str | None = None,
    skip_version: bool = False,
) -> str:
    """Directly seed workflow + published version (+ optional Start trigger) + deployment."""
    manifest = make_manifest(workflow_id)
    if trigger is not None:
        manifest["nodes"]["start"]["trigger"] = trigger
    resolved_version_id = version_id or f"{workflow_id}-v1"
    db_session.add(
        CaliberWorkflow(workflow_id=workflow_id, name="WF", owner="@test", status=workflow_status)
    )
    if not skip_version:
        db_session.add(
            CaliberWorkflowVersion(
                version_id=resolved_version_id,
                workflow_id=workflow_id,
                version_number=1,
                status="published",
                manifest=manifest,
                manifest_hash="h",
            )
        )
    db_session.add(
        CaliberWorkflowDeployment(
            deployment_id=f"{workflow_id}-{alias}-dep",
            workflow_id=workflow_id,
            alias=alias,
            version_id=resolved_version_id,
            status=deployment_status,
            deployed_at=datetime.now(timezone.utc),
        )
    )
    db_session.commit()
    return resolved_version_id


_QUEUED_RUN_COUNTER = {"n": 0}


def _create_queued_run(client: TestClient, *, manifest: dict | None = None) -> tuple[str, str, str]:
    """Enable queue, create+publish a workflow, submit a run. Returns (workflow_id, version_id, run_id)."""
    _enable_queue(client)
    _QUEUED_RUN_COUNTER["n"] += 1
    workflow_name = f"Queued Run WF {_QUEUED_RUN_COUNTER['n']}"
    wid, vid = create_and_publish(client, workflow_name=workflow_name, manifest=manifest)
    created = client.post(
        f"{PREFIX}/workflow-runs",
        json={"workflow_version_id": vid, "input": "hello"},
    )
    assert created.status_code == 202, created.text
    return wid, vid, created.json()["data"]["workflow_run_id"]


def _seed_waiting_approval_run(
    db_session: Session,
    run_id: str,
    *,
    node_id: str = "human_gate",
    with_checkpoint: bool = True,
    approval_status: str | None = None,
) -> None:
    run = db_session.get(CaliberWorkflowRun, run_id)
    assert run is not None
    run.status = "waiting_approval"
    run.current_node_id = node_id
    if with_checkpoint:
        checkpoint_id = new_workflow_run_checkpoint_id()
        db_session.add(
            CaliberWorkflowRunCheckpoint(
                checkpoint_id=checkpoint_id,
                workflow_run_id=run_id,
                sequence=1,
                node_id=node_id,
                state_blob={
                    "kind": "human_approval",
                    "node_id": node_id,
                    "output": "approved output",
                    "output_by_port": {"request": "approved output"},
                    "input_by_port": {"request": "hello"},
                },
            )
        )
        summary = dict(run.summary or {})
        summary["resume_checkpoint_id"] = checkpoint_id
        run.summary = summary
    if approval_status is not None:
        db_session.add(
            CaliberRuntimeApprovalRequest(
                runtime_approval_id=new_runtime_approval_id(),
                workflow_run_id=run_id,
                node_id=node_id,
                status=approval_status,
            )
        )
    db_session.commit()


def _seed_waiting_event_run(
    db_session: Session,
    run_id: str,
    *,
    node_id: str = "wait_gate",
    expected_event_name: str = "ticket.approved",
) -> None:
    run = db_session.get(CaliberWorkflowRun, run_id)
    assert run is not None
    run.status = "waiting_event"
    run.current_node_id = node_id
    checkpoint_id = new_workflow_run_checkpoint_id()
    db_session.add(
        CaliberWorkflowRunCheckpoint(
            checkpoint_id=checkpoint_id,
            workflow_run_id=run_id,
            sequence=1,
            node_id=node_id,
            state_blob={
                "kind": "wait_for_event",
                "node_id": node_id,
                "output": "resume output",
                "input_by_port": {"input": "hello"},
                "expected_event_name": expected_event_name,
            },
        )
    )
    summary = dict(run.summary or {})
    summary["resume_checkpoint_id"] = checkpoint_id
    run.summary = summary
    db_session.commit()


# ---------------------------------------------------------------------------
# _workflow_and_version_for_run — workflow_version_id branch (97-99, 103-106, 108-111)
# ---------------------------------------------------------------------------


def test_create_run_unknown_workflow_version_id_404(client: TestClient) -> None:
    _enable_queue(client)
    r = client.post(f"{PREFIX}/workflow-runs", json={"workflow_version_id": "WFV-ghost"})
    assert r.status_code == 404
    assert "not found" in r.json()["detail"]


def test_create_run_version_with_missing_workflow_404(
    client: TestClient, db_session: Session
) -> None:
    _enable_queue(client)
    db_session.add(
        CaliberWorkflowVersion(
            version_id="V-orphan",
            workflow_id="WF-ghost-owner",
            version_number=1,
            status="published",
            manifest=make_manifest("WF-ghost-owner"),
            manifest_hash="h",
        )
    )
    db_session.commit()
    r = client.post(f"{PREFIX}/workflow-runs", json={"workflow_version_id": "V-orphan"})
    assert r.status_code == 404
    assert "WF-ghost-owner" in r.json()["detail"]


def test_create_run_workflow_id_mismatch_with_version_400(client: TestClient) -> None:
    _enable_queue(client)
    _wid, vid = create_and_publish(client)
    r = client.post(
        f"{PREFIX}/workflow-runs",
        json={"workflow_version_id": vid, "workflow_id": "some-other-workflow"},
    )
    assert r.status_code == 400
    assert "does not match" in r.json()["detail"]


# ---------------------------------------------------------------------------
# _workflow_and_version_for_run — workflow_id + alias branch (118, 121, 130-131, 135, 151, 157)
# ---------------------------------------------------------------------------


def test_create_run_unknown_workflow_id_404(client: TestClient) -> None:
    _enable_queue(client)
    r = client.post(
        f"{PREFIX}/workflow-runs", json={"workflow_id": "WF-missing", "alias": "manual"}
    )
    assert r.status_code == 404
    assert "WF-missing" in r.json()["detail"]


def test_create_run_manual_alias_no_versions_404(client: TestClient) -> None:
    _enable_queue(client)
    wid = create_workflow(client, "No Versions Yet")
    r = client.post(f"{PREFIX}/workflow-runs", json={"workflow_id": wid, "alias": "manual"})
    assert r.status_code == 404
    assert "no workflow versions found" in r.json()["detail"]


def test_create_run_manual_alias_uses_latest_version(client: TestClient) -> None:
    # Exercises the (uncommon) workflow_id + alias="manual" success path, which
    # every other test in the suite bypasses by always addressing a run by its
    # explicit workflow_version_id.
    _enable_queue(client)
    wid = create_workflow(client, "Manual Alias WF")
    vid, _ = create_draft(client, wid, make_manifest(wid))
    r = client.post(f"{PREFIX}/workflow-runs", json={"workflow_id": wid, "alias": "manual"})
    assert r.status_code == 202, r.text
    assert r.json()["data"]["workflow_version_id"] == vid
    assert r.json()["data"]["deployment_alias"] == "manual"


def test_create_run_alias_no_active_deployment_409(client: TestClient) -> None:
    _enable_queue(client)
    wid = create_workflow(client, "No Deployment WF")
    create_draft(client, wid, make_manifest(wid))
    r = client.post(f"{PREFIX}/workflow-runs", json={"workflow_id": wid, "alias": "prod"})
    assert r.status_code == 404, r.text
    assert "no active deployment" in r.json()["detail"]


def test_create_run_deployment_points_to_missing_version_409(
    client: TestClient, db_session: Session
) -> None:
    _enable_queue(client)
    wid = create_workflow(client, "Dangling Deployment WF")
    create_draft(client, wid, make_manifest(wid))
    db_session.add(
        CaliberWorkflowDeployment(
            deployment_id="dep-dangling",
            workflow_id=wid,
            alias="prod",
            version_id="V-does-not-exist",
            status="active",
            deployed_at=datetime.now(timezone.utc),
        )
    )
    db_session.commit()
    r = client.post(f"{PREFIX}/workflow-runs", json={"workflow_id": wid, "alias": "prod"})
    assert r.status_code == 409
    assert "points to missing version" in r.json()["detail"]


# ---------------------------------------------------------------------------
# create_workflow_run — submitted-manifest validation (403-404, 407)
# ---------------------------------------------------------------------------


def test_create_run_rejects_invalid_submitted_manifest(client: TestClient) -> None:
    _enable_queue(client)
    _wid, vid = create_and_publish(client)
    r = client.post(
        f"{PREFIX}/workflow-runs",
        json={"workflow_version_id": vid, "manifest": {"not": "a valid manifest"}},
    )
    assert r.status_code == 400
    assert "invalid manifest" in r.json()["detail"]


def test_create_run_rejects_manifest_with_inline_secret(client: TestClient) -> None:
    _enable_queue(client)
    _wid, vid = create_and_publish(client)
    manifest = make_manifest(_wid)
    manifest["nodes"]["guard"] = {
        "id": "guard",
        "type": "guardrail",
        "mode": "post_agent",
        "inputs": {"response": {"type": "string"}},
        "outputs": {"passthrough": {"type": "string"}},
        "checks": [{"custom_check": {"api_key": "sk-inline-secret-value"}}],
    }
    manifest["edges"].append(
        {"id": "e_guard", "from": "agent", "to": "guard", "map": {"final_output": "response"}}
    )
    r = client.post(
        f"{PREFIX}/workflow-runs",
        json={"workflow_version_id": vid, "manifest": manifest},
    )
    assert r.status_code == 400
    assert "inline secret" in r.json()["detail"]


# ---------------------------------------------------------------------------
# create_workflow_run — workflow status guard (567, 571)
# ---------------------------------------------------------------------------


def test_create_run_rejects_paused_workflow(client: TestClient, db_session: Session) -> None:
    _enable_queue(client)
    wid, vid = create_and_publish(client)
    workflow = db_session.get(CaliberWorkflow, wid)
    assert workflow is not None
    workflow.status = "paused"
    db_session.commit()
    r = client.post(f"{PREFIX}/workflow-runs", json={"workflow_version_id": vid})
    assert r.status_code == 409
    assert "paused" in r.json()["detail"]


def test_create_run_rejects_archived_workflow(client: TestClient, db_session: Session) -> None:
    _enable_queue(client)
    wid, vid = create_and_publish(client)
    workflow = db_session.get(CaliberWorkflow, wid)
    assert workflow is not None
    workflow.status = "archived"
    db_session.commit()
    r = client.post(f"{PREFIX}/workflow-runs", json={"workflow_version_id": vid})
    assert r.status_code == 409
    assert "archived" in r.json()["detail"]


# ---------------------------------------------------------------------------
# create_workflow_run — tenant resolution via project (584-586)
# ---------------------------------------------------------------------------


def test_create_run_resolves_tenant_from_project(client: TestClient, db_session: Session) -> None:
    _enable_queue(client)
    wid, vid = create_and_publish(client)
    project_id = new_project_id()
    db_session.add(
        CaliberProject(project_id=project_id, tenant_id="acme-corp", name="Acme", owner="@test")
    )
    workflow = db_session.get(CaliberWorkflow, wid)
    assert workflow is not None
    workflow.project_id = project_id
    db_session.commit()

    r = client.post(f"{PREFIX}/workflow-runs", json={"workflow_version_id": vid})
    assert r.status_code == 202, r.text
    assert r.json()["data"]["tenant_id"] == "acme-corp"


# ---------------------------------------------------------------------------
# create_workflow_run — idempotency race handling (618-620, 626-627, 630)
# ---------------------------------------------------------------------------


def test_create_run_idempotency_race_recovers_existing_duplicate(
    client: TestClient, db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    _enable_queue(client)
    wid, vid = create_and_publish(client)
    db_session.add(
        CaliberWorkflowRun(
            workflow_run_id=new_workflow_run_id(),
            workflow_id=wid,
            workflow_version_id=vid,
            status="completed",
            source="manual",
            idempotency_key="race-key-1",
            queued_at=datetime.now(timezone.utc),
        )
    )
    db_session.commit()

    real_find = wr_routes._find_idempotent
    calls = {"n": 0}

    def _flaky_find(*args: object, **kwargs: object) -> object:
        calls["n"] += 1
        if calls["n"] == 1:
            return None  # simulate the pre-insert dedup check missing a concurrent writer
        return real_find(*args, **kwargs)

    monkeypatch.setattr(wr_routes, "_find_idempotent", _flaky_find)

    r = client.post(
        f"{PREFIX}/workflow-runs",
        json={"workflow_version_id": vid, "idempotency_key": "race-key-1"},
    )
    assert r.status_code == 202, r.text
    assert calls["n"] >= 2


def test_create_run_idempotency_conflict_without_recoverable_duplicate(
    client: TestClient, db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    _enable_queue(client)
    wid, vid = create_and_publish(client)
    db_session.add(
        CaliberWorkflowRun(
            workflow_run_id=new_workflow_run_id(),
            workflow_id=wid,
            workflow_version_id=vid,
            status="completed",
            source="manual",
            idempotency_key="race-key-2",
            queued_at=datetime.now(timezone.utc),
        )
    )
    db_session.commit()

    # Simulate the dedup lookup never finding a duplicate (neither before the
    # insert nor when recovering from the resulting IntegrityError), forcing
    # the terminal 409 branch.
    monkeypatch.setattr(wr_routes, "_find_idempotent", lambda *a, **k: None)

    r = client.post(
        f"{PREFIX}/workflow-runs",
        json={"workflow_version_id": vid, "idempotency_key": "race-key-2"},
    )
    assert r.status_code == 409
    assert "idempotency conflict" in r.json()["detail"]


# ---------------------------------------------------------------------------
# create_workflow_run — non-string input coercion (823, via _input_to_text)
# ---------------------------------------------------------------------------


def test_create_run_coerces_non_string_input_to_text(client: TestClient) -> None:
    _enable_queue(client)
    _wid, vid = create_and_publish(client)
    r = client.post(
        f"{PREFIX}/workflow-runs",
        json={"workflow_version_id": vid, "input": {"key": "value"}},
    )
    assert r.status_code == 202, r.text
    assert r.json()["data"]["summary"]["input"] == str({"key": "value"})


# ---------------------------------------------------------------------------
# _run_lineage_sort_key — all timestamps missing (278)
# ---------------------------------------------------------------------------


def test_lineage_sort_handles_run_with_no_timestamps() -> None:
    # ``queued_at`` is a NOT NULL column (server-default only), so a
    # persisted row can never actually reach this fallback — exercise the
    # pure sort-key function directly with an in-memory (never-flushed) row.
    run = CaliberWorkflowRun(
        workflow_run_id="WR-no-ts",
        workflow_id="wf",
        status="completed",
        queued_at=None,
        started_at=None,
        completed_at=None,
    )
    key = wr_routes._run_lineage_sort_key(run)
    assert key[1] == datetime.min.replace(tzinfo=timezone.utc)


# ---------------------------------------------------------------------------
# _build_workflow_run_lineage — cycle detection + missing parent (306-307, 314-315)
# ---------------------------------------------------------------------------


def test_lineage_detects_self_referential_cycle(client: TestClient, db_session: Session) -> None:
    _wid, _vid, run_id = _create_queued_run(client)
    run = db_session.get(CaliberWorkflowRun, run_id)
    assert run is not None
    run.parent_run_id = run.workflow_run_id
    db_session.commit()

    r = client.get(f"{PREFIX}/workflow-runs/{run_id}/lineage")
    assert r.status_code == 200, r.text
    assert r.json()["data"]["truncated"] is True


def test_lineage_reports_missing_parent(client: TestClient, db_session: Session) -> None:
    _wid, _vid, run_id = _create_queued_run(client)
    run = db_session.get(CaliberWorkflowRun, run_id)
    assert run is not None
    run.parent_run_id = "WR-ghost-parent"
    db_session.commit()

    r = client.get(f"{PREFIX}/workflow-runs/{run_id}/lineage")
    assert r.status_code == 200, r.text
    data = r.json()["data"]
    assert data["missing_parent_id"] == "WR-ghost-parent"
    assert data["root_run_id"] == run_id


# ---------------------------------------------------------------------------
# _build_workflow_run_lineage — max_parent_hops truncation (309-310)
# ---------------------------------------------------------------------------


def test_lineage_truncates_long_parent_chain(client: TestClient, db_session: Session) -> None:
    wid, vid, leaf_id = _create_queued_run(client)
    previous_id = leaf_id
    # 70 ancestors chained beyond the default max_parent_hops (64).
    for i in range(70):
        ancestor_id = f"WR-ancestor-{i}"
        db_session.add(
            CaliberWorkflowRun(
                workflow_run_id=ancestor_id,
                workflow_id=wid,
                workflow_version_id=vid,
                status="completed",
                queued_at=datetime.now(timezone.utc) - timedelta(minutes=i + 1),
                parent_run_id=None,
            )
        )
        db_session.flush()
        parent_row = db_session.get(CaliberWorkflowRun, previous_id)
        assert parent_row is not None
        parent_row.parent_run_id = ancestor_id
        previous_id = ancestor_id
    db_session.commit()

    r = client.get(f"{PREFIX}/workflow-runs/{leaf_id}/lineage")
    assert r.status_code == 200, r.text
    assert r.json()["data"]["truncated"] is True


# ---------------------------------------------------------------------------
# _build_workflow_run_lineage — max_runs truncation during children BFS (350-351, 355-356)
# ---------------------------------------------------------------------------


def test_lineage_children_bfs_respects_small_max_runs(
    client: TestClient, db_session: Session
) -> None:
    wid, vid, root_id = _create_queued_run(client)
    root = db_session.get(CaliberWorkflowRun, root_id)
    assert root is not None
    now = datetime.now(timezone.utc)
    for i in range(3):
        db_session.add(
            CaliberWorkflowRun(
                workflow_run_id=f"WR-child-{i}",
                workflow_id=wid,
                workflow_version_id=vid,
                status="completed",
                parent_run_id=root_id,
                queued_at=now + timedelta(seconds=i),
            )
        )
    db_session.commit()
    db_session.refresh(root)

    # Direct call with a tiny max_runs: the HTTP route hard-codes 250, which
    # would require hundreds of rows to exercise this truncation branch.
    result = wr_routes._build_workflow_run_lineage(db_session, root, max_runs=2)
    assert result.truncated is True
    assert len(result.runs) <= 2


# ---------------------------------------------------------------------------
# _summary_input — pure-function branches (383-390)
# ---------------------------------------------------------------------------


def test_summary_input_direct_branches() -> None:
    assert wr_routes._summary_input(None) == ""
    assert wr_routes._summary_input(["not", "a", "dict"]) == ""  # type: ignore[arg-type]
    assert wr_routes._summary_input({"input": "hello"}) == "hello"
    assert wr_routes._summary_input({"input": None}) == ""
    assert wr_routes._summary_input({"input": 42}) == "42"
    assert wr_routes._summary_input({}) == ""


# ---------------------------------------------------------------------------
# _transition_or_409 — illegal transition (396-397)
# ---------------------------------------------------------------------------


def test_transition_or_409_raises_for_illegal_transition() -> None:
    with pytest.raises(HTTPException) as exc_info:
        wr_routes._transition_or_409("completed", "queued")
    assert exc_info.value.status_code == 409


# ---------------------------------------------------------------------------
# _copy_manifest_summary_metadata — non-dict summary (531)
# ---------------------------------------------------------------------------


def test_copy_manifest_summary_metadata_handles_non_dict_summary() -> None:
    assert wr_routes._copy_manifest_summary_metadata(None) == {}
    assert wr_routes._copy_manifest_summary_metadata("not a dict") == {}  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# retry_workflow_run — status/version guards (1129, 1134, 1151)
# ---------------------------------------------------------------------------


def test_retry_rejects_non_retryable_status(client: TestClient) -> None:
    _wid, _vid, run_id = _create_queued_run(client)
    r = client.post(f"{PREFIX}/workflow-runs/{run_id}/retry", json={})
    assert r.status_code == 409
    assert "is not retryable" in r.json()["detail"]


def test_retry_rejects_missing_workflow_version_id(client: TestClient, db_session: Session) -> None:
    _wid, _vid, run_id = _create_queued_run(client)
    run = db_session.get(CaliberWorkflowRun, run_id)
    assert run is not None
    run.status = "failed"
    run.workflow_version_id = None
    db_session.commit()

    r = client.post(f"{PREFIX}/workflow-runs/{run_id}/retry", json={})
    assert r.status_code == 409
    assert "without workflow_version_id" in r.json()["detail"]


def test_retry_rejects_unknown_checkpoint_id(client: TestClient, db_session: Session) -> None:
    _wid, _vid, run_id = _create_queued_run(client)
    run = db_session.get(CaliberWorkflowRun, run_id)
    assert run is not None
    run.status = "failed"
    db_session.commit()

    r = client.post(f"{PREFIX}/workflow-runs/{run_id}/retry", json={"checkpoint_id": "CKPT-ghost"})
    assert r.status_code == 404
    assert "CKPT-ghost" in r.json()["detail"]


# ---------------------------------------------------------------------------
# retry_workflow_run — manifest_mode backfill fallback (1221, plus 531 end-to-end)
# ---------------------------------------------------------------------------


def test_retry_falls_back_when_summary_and_input_payload_absent(
    client: TestClient, db_session: Session
) -> None:
    wid, _vid, run_id = _create_queued_run(client)
    run = db_session.get(CaliberWorkflowRun, run_id)
    assert run is not None
    run.status = "failed"
    run.input_payload = ""
    run.summary = None
    run.manifest_snapshot = make_manifest(wid)
    db_session.commit()

    r = client.post(f"{PREFIX}/workflow-runs/{run_id}/retry", json={"reason": "fallback"})
    assert r.status_code == 202, r.text
    assert r.json()["data"]["status"] == "queued"


# ---------------------------------------------------------------------------
# _retry_checkpoint_manifest_error — wait_for_event branches (442, 460-461, 467, 472-473, 482)
# ---------------------------------------------------------------------------


def _seed_failed_run_with_wait_gate(client: TestClient, db_session: Session) -> tuple[str, str]:
    _enable_queue(client)
    workflow_id = "retry-wait-gate-wf"
    _wid, vid = create_and_publish(client, manifest=_wait_event_manifest(workflow_id))
    created = client.post(
        f"{PREFIX}/workflow-runs",
        json={"workflow_version_id": vid, "input": "checkpoint replay"},
    )
    assert created.status_code == 202, created.text
    run_id = created.json()["data"]["workflow_run_id"]
    run = db_session.get(CaliberWorkflowRun, run_id)
    assert run is not None
    run.status = "failed"
    run.completed_at = datetime.now(timezone.utc)
    db_session.commit()
    return vid, run_id


def test_retry_rejects_wait_for_event_checkpoint_missing_resume_inputs_key(
    client: TestClient, db_session: Session
) -> None:
    _vid, run_id = _seed_failed_run_with_wait_gate(client, db_session)
    checkpoint_id = new_workflow_run_checkpoint_id()
    db_session.add(
        CaliberWorkflowRunCheckpoint(
            checkpoint_id=checkpoint_id,
            workflow_run_id=run_id,
            sequence=1,
            node_id="wait_gate",
            state_blob={
                "kind": "wait_for_event",
                "node_id": "wait_gate",
                "output": "resume output",
                "input_by_port": {"input": "checkpoint replay"},
                "expected_event_name": "ticket.approved",
                # No "resume_event_inputs" key at all (distinct from an empty dict).
            },
        )
    )
    db_session.commit()

    r = client.post(
        f"{PREFIX}/workflow-runs/{run_id}/retry",
        json={"reason": "recover", "checkpoint_id": checkpoint_id},
    )
    assert r.status_code == 409
    assert "missing its stored resume event payload" in r.json()["detail"]


def test_retry_succeeds_wait_for_event_checkpoint_with_resume_event_payload(
    client: TestClient, db_session: Session
) -> None:
    _vid, run_id = _seed_failed_run_with_wait_gate(client, db_session)
    checkpoint_id = new_workflow_run_checkpoint_id()
    db_session.add(
        CaliberWorkflowRunCheckpoint(
            checkpoint_id=checkpoint_id,
            workflow_run_id=run_id,
            sequence=1,
            node_id="wait_gate",
            state_blob={
                "kind": "wait_for_event",
                "node_id": "wait_gate",
                "output": "resume output",
                "input_by_port": {"input": "checkpoint replay"},
                "expected_event_name": "ticket.approved",
                "resume_event_inputs": {
                    "event_name": "ticket.approved",
                    "resume_event": {"ticket_id": "T-1", "approved": True},
                },
            },
        )
    )
    db_session.commit()

    r = client.post(
        f"{PREFIX}/workflow-runs/{run_id}/retry",
        json={"reason": "recover", "checkpoint_id": checkpoint_id},
    )
    assert r.status_code == 202, r.text


def test_retry_succeeds_wait_for_event_checkpoint_payload_under_event_name_key(
    client: TestClient, db_session: Session
) -> None:
    _vid, run_id = _seed_failed_run_with_wait_gate(client, db_session)
    checkpoint_id = new_workflow_run_checkpoint_id()
    db_session.add(
        CaliberWorkflowRunCheckpoint(
            checkpoint_id=checkpoint_id,
            workflow_run_id=run_id,
            sequence=1,
            node_id="wait_gate",
            state_blob={
                "kind": "wait_for_event",
                "node_id": "wait_gate",
                "output": "resume output",
                "input_by_port": {"input": "checkpoint replay"},
                "expected_event_name": "ticket.approved",
                # Payload is only present under the expected-event-name key,
                # not under "resume_event"/"event"/"event_payload".
                "resume_event_inputs": {
                    "event_name": "ticket.approved",
                    "ticket.approved": {"ticket_id": "T-9"},
                },
            },
        )
    )
    db_session.commit()

    r = client.post(
        f"{PREFIX}/workflow-runs/{run_id}/retry",
        json={"reason": "recover", "checkpoint_id": checkpoint_id},
    )
    assert r.status_code == 202, r.text


def test_retry_rejects_checkpoint_when_manifest_snapshot_is_invalid(
    client: TestClient, db_session: Session
) -> None:
    wid, _vid, run_id = _create_queued_run(client)
    run = db_session.get(CaliberWorkflowRun, run_id)
    assert run is not None
    run.status = "failed"
    run.manifest_snapshot = {"not": "a valid manifest"}
    checkpoint_id = new_workflow_run_checkpoint_id()
    db_session.add(
        CaliberWorkflowRunCheckpoint(
            checkpoint_id=checkpoint_id,
            workflow_run_id=run_id,
            sequence=1,
            node_id="agent",
            state_blob={
                "kind": "human_approval",
                "node_id": "agent",
                "input_by_port": {"request": "hello"},
            },
        )
    )
    db_session.commit()

    r = client.post(
        f"{PREFIX}/workflow-runs/{run_id}/retry",
        json={"reason": "recover", "checkpoint_id": checkpoint_id},
    )
    assert r.status_code == 409
    assert "manifest is invalid" in r.json()["detail"]


def test_retry_rejects_wait_for_event_checkpoint_targeting_wrong_node_type(
    client: TestClient, db_session: Session
) -> None:
    wid, _vid, run_id = _create_queued_run(client, manifest=make_manifest("wf-node-type-check"))
    run = db_session.get(CaliberWorkflowRun, run_id)
    assert run is not None
    run.status = "failed"
    checkpoint_id = new_workflow_run_checkpoint_id()
    db_session.add(
        CaliberWorkflowRunCheckpoint(
            checkpoint_id=checkpoint_id,
            workflow_run_id=run_id,
            sequence=1,
            node_id="agent",
            state_blob={
                "kind": "wait_for_event",
                "node_id": "agent",
                "input_by_port": {"input": "hello"},
                "expected_event_name": "ticket.approved",
                "resume_event_inputs": {
                    "event_name": "ticket.approved",
                    "resume_event": {"ok": True},
                },
            },
        )
    )
    db_session.commit()

    r = client.post(
        f"{PREFIX}/workflow-runs/{run_id}/retry",
        json={"reason": "recover", "checkpoint_id": checkpoint_id},
    )
    assert r.status_code == 409
    assert "does not match current manifest" in r.json()["detail"]


def test_retry_rejects_wait_until_checkpoint_targeting_wrong_node_type(
    client: TestClient, db_session: Session
) -> None:
    wid, _vid, run_id = _create_queued_run(client, manifest=make_manifest("wf-node-type-check"))
    run = db_session.get(CaliberWorkflowRun, run_id)
    assert run is not None
    run.status = "failed"
    checkpoint_id = new_workflow_run_checkpoint_id()
    db_session.add(
        CaliberWorkflowRunCheckpoint(
            checkpoint_id=checkpoint_id,
            workflow_run_id=run_id,
            sequence=1,
            node_id="agent",
            state_blob={
                "kind": "wait_until",
                "node_id": "agent",
                "input_by_port": {"input": "hello"},
            },
        )
    )
    db_session.commit()

    r = client.post(
        f"{PREFIX}/workflow-runs/{run_id}/retry",
        json={"reason": "recover", "checkpoint_id": checkpoint_id},
    )
    assert r.status_code == 409
    assert "does not match current manifest" in r.json()["detail"]


def test_retry_rejects_runtime_approval_checkpoint_targeting_non_tool_node(
    client: TestClient, db_session: Session
) -> None:
    wid, _vid, run_id = _create_queued_run(client, manifest=make_manifest("wf-node-type-check"))
    run = db_session.get(CaliberWorkflowRun, run_id)
    assert run is not None
    run.status = "failed"
    checkpoint_id = new_workflow_run_checkpoint_id()
    db_session.add(
        CaliberWorkflowRunCheckpoint(
            checkpoint_id=checkpoint_id,
            workflow_run_id=run_id,
            sequence=1,
            node_id="agent",
            state_blob={
                "kind": "runtime_approval",
                "node_id": "agent",
                "input_by_port": {"input": "hello"},
            },
        )
    )
    db_session.commit()

    r = client.post(
        f"{PREFIX}/workflow-runs/{run_id}/retry",
        json={"reason": "recover", "checkpoint_id": checkpoint_id},
    )
    assert r.status_code == 409
    assert "does not match current manifest" in r.json()["detail"]


# ---------------------------------------------------------------------------
# cancel_workflow_run — terminal + already-cancelled branches (1027, 1032-1033)
# ---------------------------------------------------------------------------


def test_cancel_rejects_terminal_completed_run(client: TestClient, db_session: Session) -> None:
    _wid, _vid, run_id = _create_queued_run(client)
    run = db_session.get(CaliberWorkflowRun, run_id)
    assert run is not None
    run.status = "completed"
    db_session.commit()

    r = client.post(f"{PREFIX}/workflow-runs/{run_id}/cancel", json={})
    assert r.status_code == 409
    assert "terminal status" in r.json()["detail"]


def test_cancel_already_cancelled_run_returns_current_state(
    client: TestClient, db_session: Session
) -> None:
    _wid, _vid, run_id = _create_queued_run(client)
    run = db_session.get(CaliberWorkflowRun, run_id)
    assert run is not None
    run.status = "cancelled"
    db_session.commit()

    r = client.post(f"{PREFIX}/workflow-runs/{run_id}/cancel", json={"reason": "again"})
    assert r.status_code == 200
    assert r.json()["data"]["status"] == "cancelled"


# ---------------------------------------------------------------------------
# get_workflow_run_manifest — snapshot-absent fallback branches (862-863, 870-872, 879-881)
# ---------------------------------------------------------------------------


def test_manifest_404_without_snapshot_or_version_id(
    client: TestClient, db_session: Session
) -> None:
    wid, _vid, run_id = _create_queued_run(client)
    run = db_session.get(CaliberWorkflowRun, run_id)
    assert run is not None
    run.manifest_snapshot = None
    run.workflow_version_id = None
    db_session.commit()

    r = client.get(f"{PREFIX}/workflow-runs/{run_id}/manifest")
    assert r.status_code == 404
    assert "does not have a manifest snapshot" in r.json()["detail"]


def test_manifest_404_when_version_missing(client: TestClient, db_session: Session) -> None:
    wid, _vid, run_id = _create_queued_run(client)
    run = db_session.get(CaliberWorkflowRun, run_id)
    assert run is not None
    run.manifest_snapshot = None
    run.workflow_version_id = "V-ghost"
    db_session.commit()

    r = client.get(f"{PREFIX}/workflow-runs/{run_id}/manifest")
    assert r.status_code == 404
    assert "workflow version" in r.json()["detail"]


def test_manifest_saved_version_mode_when_snapshot_absent(
    client: TestClient, db_session: Session
) -> None:
    wid, vid, run_id = _create_queued_run(client)
    run = db_session.get(CaliberWorkflowRun, run_id)
    assert run is not None
    run.manifest_snapshot = None
    db_session.commit()
    version = db_session.get(CaliberWorkflowVersion, vid)
    assert version is not None

    r = client.get(f"{PREFIX}/workflow-runs/{run_id}/manifest")
    assert r.status_code == 200, r.text
    data = r.json()["data"]
    assert data["manifest_mode"] == "saved_version"
    assert data["manifest"] == version.manifest


# ---------------------------------------------------------------------------
# _parse_positive_int — bad query params (905-906, 908)
# ---------------------------------------------------------------------------


def test_list_events_rejects_non_integer_after_param(client: TestClient) -> None:
    _wid, _vid, run_id = _create_queued_run(client)
    r = client.get(f"{PREFIX}/workflow-runs/{run_id}/events?after=notanumber")
    assert r.status_code == 400
    assert "must be an integer" in r.json()["detail"]


def test_list_checkpoints_rejects_out_of_range_limit(client: TestClient) -> None:
    _wid, _vid, run_id = _create_queued_run(client)
    r = client.get(f"{PREFIX}/workflow-runs/{run_id}/checkpoints?limit=5000")
    assert r.status_code == 400
    assert "must be between" in r.json()["detail"]


# ---------------------------------------------------------------------------
# _start_trigger — malformed manifest / no start node (685-686, 690)
# ---------------------------------------------------------------------------


def test_start_trigger_returns_none_for_malformed_manifest() -> None:
    version = SimpleNamespace(manifest={"schema_version": 1})  # missing required fields
    assert wr_routes._start_trigger(version) is None  # type: ignore[arg-type]


def test_start_trigger_returns_none_when_no_start_node(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_manifest = SimpleNamespace(nodes={})
    monkeypatch.setattr(wr_routes, "parse_manifest", lambda data: fake_manifest)
    version = SimpleNamespace(manifest={})
    assert wr_routes._start_trigger(version) is None  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# trigger_workflow_event — workflow lookup + status guard (723, 790)
# ---------------------------------------------------------------------------


def test_trigger_unknown_workflow_404(client: TestClient) -> None:
    _enable_queue(client)
    r = client.post(f"{PREFIX}/workflows/WF-missing/trigger", json={})
    assert r.status_code == 404
    assert "WF-missing" in r.json()["detail"]


def test_trigger_rejects_paused_workflow(client: TestClient, db_session: Session) -> None:
    _enable_queue(client)
    _seed_deployed_workflow(
        db_session,
        workflow_id="trigger-paused-wf",
        trigger={"mode": "event", "event_name": "e", "alias": "prod"},
    )
    workflow = db_session.get(CaliberWorkflow, "trigger-paused-wf")
    assert workflow is not None
    workflow.status = "paused"
    db_session.commit()

    r = client.post(f"{PREFIX}/workflows/trigger-paused-wf/trigger", json={"event_name": "e"})
    assert r.status_code == 409
    assert "workflow is paused" in r.json()["detail"]


# ---------------------------------------------------------------------------
# _resolve_event_trigger_target — explicit alias branch (706, 711, 719)
# ---------------------------------------------------------------------------


def test_trigger_explicit_alias_non_event_trigger_409(
    client: TestClient, db_session: Session
) -> None:
    _enable_queue(client)
    _seed_deployed_workflow(db_session, workflow_id="trigger-manual-alias-wf", trigger=None)

    r = client.post(
        f"{PREFIX}/workflows/trigger-manual-alias-wf/trigger",
        json={"alias": "prod", "event_name": "e"},
    )
    assert r.status_code == 409
    assert "not configured for event triggers" in r.json()["detail"]


def test_trigger_explicit_alias_disabled_trigger_409(
    client: TestClient, db_session: Session
) -> None:
    _enable_queue(client)
    _seed_deployed_workflow(
        db_session,
        workflow_id="trigger-disabled-wf",
        trigger={"mode": "event", "event_name": "e", "alias": "prod", "enabled": False},
    )

    r = client.post(
        f"{PREFIX}/workflows/trigger-disabled-wf/trigger",
        json={"alias": "prod", "event_name": "e"},
    )
    assert r.status_code == 409
    assert "event trigger is disabled" in r.json()["detail"]


def test_trigger_explicit_alias_matching_succeeds(client: TestClient, db_session: Session) -> None:
    _enable_queue(client)
    _seed_deployed_workflow(
        db_session,
        workflow_id="trigger-explicit-alias-wf",
        trigger={"mode": "event", "event_name": "e", "alias": "prod", "enabled": True},
    )

    r = client.post(
        f"{PREFIX}/workflows/trigger-explicit-alias-wf/trigger",
        json={"alias": "prod", "event_name": "e"},
    )
    assert r.status_code == 202, r.text
    assert r.json()["data"]["deployment_alias"] == "prod"


# ---------------------------------------------------------------------------
# _resolve_event_trigger_target — auto-discover loop (745, 750)
# ---------------------------------------------------------------------------


def test_trigger_auto_discover_skips_deployment_with_missing_version(
    client: TestClient, db_session: Session
) -> None:
    _enable_queue(client)
    _seed_deployed_workflow(
        db_session,
        workflow_id="trigger-ghost-version-wf",
        trigger={"mode": "event", "event_name": "e", "alias": "prod"},
        version_id="V-does-not-exist",
        skip_version=True,
    )

    r = client.post(f"{PREFIX}/workflows/trigger-ghost-version-wf/trigger", json={})
    assert r.status_code == 409
    assert "not configured for event triggers" in r.json()["detail"]


def test_trigger_auto_discover_skips_alias_mismatch(
    client: TestClient, db_session: Session
) -> None:
    _enable_queue(client)
    # Deployed at "staging", but the manifest's Start trigger declares "prod".
    _seed_deployed_workflow(
        db_session,
        workflow_id="trigger-alias-drift-wf",
        trigger={"mode": "event", "event_name": "e", "alias": "prod"},
        alias="staging",
    )

    r = client.post(f"{PREFIX}/workflows/trigger-alias-drift-wf/trigger", json={})
    assert r.status_code == 409
    assert "not configured for event triggers" in r.json()["detail"]


# ---------------------------------------------------------------------------
# approve/reject runtime approval — feature-flag + lookup guards
# (247, 1305, 1313, 1328, 1367, 1464)
# ---------------------------------------------------------------------------


def test_approve_requires_runtime_approvals_enabled(client: TestClient) -> None:
    _enable_queue(client)  # runtime approvals left disabled
    r = client.post(f"{PREFIX}/workflow-runs/WR-anything/approval/approve", json={})
    assert r.status_code == 409
    assert "runtime workflow approvals are disabled" in r.json()["detail"]


def test_approve_rejects_unknown_runtime_approval_id(
    client: TestClient, db_session: Session
) -> None:
    _enable_queue_approvals_and_checkpointing(client)
    _wid, _vid, run_id = _create_queued_run(client)
    _seed_waiting_approval_run(db_session, run_id, approval_status="pending")

    r = client.post(
        f"{PREFIX}/workflow-runs/{run_id}/approval/approve",
        json={"runtime_approval_id": "RA-ghost"},
    )
    assert r.status_code == 404
    assert "RA-ghost" in r.json()["detail"]


def test_approve_rejects_already_decided_runtime_approval_id(
    client: TestClient, db_session: Session
) -> None:
    _enable_queue_approvals_and_checkpointing(client)
    _wid, _vid, run_id = _create_queued_run(client)
    _seed_waiting_approval_run(db_session, run_id, approval_status="approved")
    approval = (
        db_session.query(CaliberRuntimeApprovalRequest)
        .filter(CaliberRuntimeApprovalRequest.workflow_run_id == run_id)
        .one()
    )

    r = client.post(
        f"{PREFIX}/workflow-runs/{run_id}/approval/approve",
        json={"runtime_approval_id": approval.runtime_approval_id},
    )
    assert r.status_code == 409
    assert "is not pending" in r.json()["detail"]


def test_approve_rejects_when_no_pending_approvals_exist(
    client: TestClient, db_session: Session
) -> None:
    _enable_queue_approvals_and_checkpointing(client)
    _wid, _vid, run_id = _create_queued_run(client)
    _seed_waiting_approval_run(db_session, run_id, approval_status=None)

    r = client.post(f"{PREFIX}/workflow-runs/{run_id}/approval/approve", json={})
    assert r.status_code == 409
    assert "no pending runtime approvals" in r.json()["detail"]


def test_approve_rejects_when_run_not_waiting_for_approval(client: TestClient) -> None:
    _enable_queue_approvals_and_checkpointing(client)
    _wid, _vid, run_id = _create_queued_run(client)  # status stays "queued"

    r = client.post(f"{PREFIX}/workflow-runs/{run_id}/approval/approve", json={})
    assert r.status_code == 409
    assert "is not waiting for approval" in r.json()["detail"]


def test_reject_rejects_when_run_not_waiting_for_approval(client: TestClient) -> None:
    _enable_queue_approvals_and_checkpointing(client)
    _wid, _vid, run_id = _create_queued_run(client)  # status stays "queued"

    r = client.post(f"{PREFIX}/workflow-runs/{run_id}/approval/reject", json={})
    assert r.status_code == 409
    assert "is not waiting for approval" in r.json()["detail"]


# ---------------------------------------------------------------------------
# _checkpoint_* / _approval_checkpoint_* / _waiting_event_checkpoint_* predicate
# helpers — every HTTP call site already guards ``state_blob`` being a dict
# before calling these, so their internal "state_blob is None" branches
# (1602, 1611, 1623, 1633, 1640, 1651, 1656, 1658, 1660, 1673, 1683, 1692,
# 1704, 1719) are unreachable via the route layer. Exercised directly here.
# ---------------------------------------------------------------------------


def _bare_checkpoint(**overrides: object) -> CaliberWorkflowRunCheckpoint:
    defaults: dict[str, object] = {
        "checkpoint_id": "CKPT-bare",
        "workflow_run_id": "WR-bare",
        "sequence": 1,
        "node_id": "n",
        "state_blob": None,
    }
    defaults.update(overrides)
    return CaliberWorkflowRunCheckpoint(**defaults)  # type: ignore[arg-type]


def _bare_run(**overrides: object) -> CaliberWorkflowRun:
    defaults: dict[str, object] = {
        "workflow_run_id": "WR-bare",
        "workflow_id": "WF-bare",
        "current_node_id": "n",
    }
    defaults.update(overrides)
    return CaliberWorkflowRun(**defaults)  # type: ignore[arg-type]


def test_store_resume_event_inputs_noop_when_state_blob_not_dict() -> None:
    checkpoint = _bare_checkpoint(state_blob=None)
    wr_routes._store_resume_event_inputs(checkpoint, event_name="e", event_payload=None)
    assert checkpoint.state_blob is None  # untouched


def test_store_resume_event_inputs_treats_non_string_expected_event_name_as_none() -> None:
    checkpoint = _bare_checkpoint(
        state_blob={"kind": "wait_for_event", "expected_event_name": 12345}
    )
    wr_routes._store_resume_event_inputs(checkpoint, event_name="e", event_payload={"x": 1})
    assert checkpoint.state_blob["resume_event_inputs"]["event_name"] == "e"


def test_approval_checkpoint_predicates_false_when_state_blob_missing() -> None:
    checkpoint = _bare_checkpoint(state_blob=None)
    assert wr_routes._approval_checkpoint_input_snapshot_missing(checkpoint) is False
    assert wr_routes._approval_checkpoint_kind_invalid(checkpoint) is False
    assert wr_routes._checkpoint_node_id_missing(checkpoint) is False
    run = _bare_run()
    assert wr_routes._checkpoint_node_id_mismatch(run, checkpoint) is False


def test_checkpoint_node_id_mismatch_true_when_ids_missing() -> None:
    checkpoint_missing_own_node_id = _bare_checkpoint(node_id=None, state_blob={"node_id": "n"})
    run_with_node = _bare_run(current_node_id="n")
    assert (
        wr_routes._checkpoint_node_id_mismatch(run_with_node, checkpoint_missing_own_node_id)
        is True
    )

    checkpoint_missing_state_node_id = _bare_checkpoint(node_id="n", state_blob={"node_id": None})
    assert (
        wr_routes._checkpoint_node_id_mismatch(run_with_node, checkpoint_missing_state_node_id)
        is True
    )

    checkpoint_ok = _bare_checkpoint(node_id="n", state_blob={"node_id": "n"})
    run_missing_current = _bare_run(current_node_id=None)
    assert wr_routes._checkpoint_node_id_mismatch(run_missing_current, checkpoint_ok) is True


def test_waiting_event_checkpoint_predicates_false_when_state_blob_missing() -> None:
    checkpoint = _bare_checkpoint(state_blob=None)
    assert wr_routes._waiting_event_checkpoint_input_snapshot_missing(checkpoint) is False
    assert wr_routes._waiting_event_checkpoint_kind_invalid(checkpoint) is False
    assert wr_routes._waiting_event_checkpoint_expected_event_missing(checkpoint) is False
    assert (
        wr_routes._waiting_event_checkpoint_correlation_value_missing_for_event_match(checkpoint)
        is False
    )
    assert wr_routes._waiting_event_checkpoint_uses_legacy_event_match_shape(checkpoint) is False


# ---------------------------------------------------------------------------
# _match_waiting_event_run_for_external_resume — direct unit branches
# (1790, 1793, 1795, 1802) — the resume-by-event loop already guards
# workflow_id / state_blob-dict-ness before calling this helper.
# ---------------------------------------------------------------------------


def test_match_waiting_event_run_workflow_id_mismatch_returns_none() -> None:
    run = _bare_run(workflow_id="WF-a")
    checkpoint = _bare_checkpoint(state_blob={"kind": "wait_for_event"})
    result = wr_routes._match_waiting_event_run_for_external_resume(
        run=run,
        checkpoint=checkpoint,
        event_name="e",
        event_payload=None,
        workflow_id="WF-b",
    )
    assert result is None


def test_match_waiting_event_run_state_blob_not_dict_returns_none() -> None:
    run = _bare_run(workflow_id="WF-a")
    checkpoint = _bare_checkpoint(state_blob=None)
    result = wr_routes._match_waiting_event_run_for_external_resume(
        run=run,
        checkpoint=checkpoint,
        event_name="e",
        event_payload=None,
        workflow_id=None,
    )
    assert result is None


def test_match_waiting_event_run_wrong_kind_returns_none() -> None:
    run = _bare_run(workflow_id="WF-a")
    checkpoint = _bare_checkpoint(state_blob={"kind": "wait_until"})
    result = wr_routes._match_waiting_event_run_for_external_resume(
        run=run,
        checkpoint=checkpoint,
        event_name="e",
        event_payload=None,
        workflow_id=None,
    )
    assert result is None


def test_match_waiting_event_run_expected_event_name_mismatch_returns_none() -> None:
    run = _bare_run(workflow_id="WF-a")
    checkpoint = _bare_checkpoint(
        state_blob={"kind": "wait_for_event", "expected_event_name": "ticket.approved"}
    )
    result = wr_routes._match_waiting_event_run_for_external_resume(
        run=run,
        checkpoint=checkpoint,
        event_name="ticket.other",
        event_payload=None,
        workflow_id=None,
    )
    assert result is None


# ---------------------------------------------------------------------------
# resume_workflow_run — non-resumable / rejected / no-approved-decision (1826, 1869, 1884)
# ---------------------------------------------------------------------------


def test_resume_rejects_non_resumable_status(client: TestClient) -> None:
    _enable_queue_and_checkpointing(client)
    _wid, _vid, run_id = _create_queued_run(client)  # status "queued"

    r = client.post(f"{PREFIX}/workflow-runs/{run_id}/resume", json={})
    assert r.status_code == 409
    assert "not resumable" in r.json()["detail"]


def test_resume_rejects_after_rejected_decision(client: TestClient, db_session: Session) -> None:
    _enable_queue_and_checkpointing(client)
    _wid, _vid, run_id = _create_queued_run(client)
    _seed_waiting_approval_run(db_session, run_id, approval_status="rejected")

    r = client.post(f"{PREFIX}/workflow-runs/{run_id}/resume", json={})
    assert r.status_code == 409
    assert "rejection" in r.json()["detail"]


def test_resume_requires_an_approved_decision_when_none_exists(
    client: TestClient, db_session: Session
) -> None:
    _enable_queue_and_checkpointing(client)
    _wid, _vid, run_id = _create_queued_run(client)
    _seed_waiting_approval_run(db_session, run_id, approval_status=None)

    r = client.post(f"{PREFIX}/workflow-runs/{run_id}/resume", json={})
    assert r.status_code == 409
    assert "no approved runtime approval decision" in r.json()["detail"]


def test_resume_rejects_mismatched_event_name(client: TestClient, db_session: Session) -> None:
    _enable_queue_and_checkpointing(client)
    _wid, _vid, run_id = _create_queued_run(client)
    _seed_waiting_event_run(db_session, run_id, expected_event_name="ticket.approved")

    r = client.post(f"{PREFIX}/workflow-runs/{run_id}/resume", json={"event_name": "ticket.other"})
    assert r.status_code == 409
    assert "does not match" in r.json()["detail"]


# ---------------------------------------------------------------------------
# resume_workflow_run_by_event — loop-skip + no-match branches (1795, 1802, 2014, 2016, 2080)
# ---------------------------------------------------------------------------


def test_resume_by_event_no_match_when_checkpoint_kind_is_not_wait_for_event(
    client: TestClient, db_session: Session
) -> None:
    _enable_queue_and_checkpointing(client)
    _wid, _vid, run_id = _create_queued_run(client)
    _seed_waiting_event_run(db_session, run_id)
    run = db_session.get(CaliberWorkflowRun, run_id)
    assert run is not None
    checkpoint_id = dict(run.summary or {})["resume_checkpoint_id"]
    checkpoint = db_session.get(CaliberWorkflowRunCheckpoint, checkpoint_id)
    assert checkpoint is not None
    state_blob = dict(checkpoint.state_blob)
    state_blob["kind"] = "wait_until"
    checkpoint.state_blob = state_blob
    db_session.commit()

    r = client.post(
        f"{PREFIX}/workflow-runs/resume-by-event", json={"event_name": "ticket.approved"}
    )
    assert r.status_code == 404
    assert "no waiting workflow run matched" in r.json()["detail"]


def test_resume_by_event_no_match_on_expected_event_name_mismatch(
    client: TestClient, db_session: Session
) -> None:
    _enable_queue_and_checkpointing(client)
    _wid, _vid, run_id = _create_queued_run(client)
    _seed_waiting_event_run(db_session, run_id, expected_event_name="ticket.approved")

    r = client.post(f"{PREFIX}/workflow-runs/resume-by-event", json={"event_name": "ticket.other"})
    assert r.status_code == 404
    assert "no waiting workflow run matched" in r.json()["detail"]


def test_resume_by_event_skips_run_without_resume_checkpoint(
    client: TestClient, db_session: Session
) -> None:
    _enable_queue_and_checkpointing(client)
    _wid, _vid, run_id = _create_queued_run(client)
    run = db_session.get(CaliberWorkflowRun, run_id)
    assert run is not None
    run.status = "waiting_event"  # no resume_checkpoint_id in summary
    db_session.commit()

    r = client.post(
        f"{PREFIX}/workflow-runs/resume-by-event", json={"event_name": "ticket.approved"}
    )
    assert r.status_code == 404
    assert "no waiting workflow run matched" in r.json()["detail"]


def test_resume_by_event_filters_candidates_by_workflow_id(
    client: TestClient, db_session: Session
) -> None:
    _enable_queue_and_checkpointing(client)
    _wid_a, _vid_a, run_a = _create_queued_run(client)
    _seed_waiting_event_run(db_session, run_a, expected_event_name="ticket.approved")
    _wid_b, _vid_b, run_b = _create_queued_run(client)
    _seed_waiting_event_run(db_session, run_b, expected_event_name="ticket.approved")

    r = client.post(
        f"{PREFIX}/workflow-runs/resume-by-event",
        json={"event_name": "ticket.approved", "workflow_id": _wid_a},
    )
    assert r.status_code == 202, r.text
    assert r.json()["data"]["workflow_run_id"] == run_a
