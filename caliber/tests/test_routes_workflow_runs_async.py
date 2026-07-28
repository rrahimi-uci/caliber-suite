"""Route tests for async workflow-run APIs."""

from __future__ import annotations

from datetime import datetime, timezone

from starlette.testclient import TestClient

import caliber.routes.workflow_runs as workflow_runs_routes
from caliber.db.models import (
    CaliberRuntimeApprovalRequest,
    CaliberWorkflowRun,
    CaliberWorkflowRunCheckpoint,
    CaliberWorkflowRunEvent,
    CaliberWorkflowVersion,
)
from caliber.ids import new_runtime_approval_id, new_workflow_run_checkpoint_id
from tests.workflow_helpers import PREFIX, create_and_publish, make_manifest


def _approval_manifest(workflow_id: str) -> dict[str, object]:
    manifest = make_manifest(workflow_id)
    manifest["edges"] = [
        {"id": "e1", "from": "start", "to": "agent", "map": {"msg": "input"}},
        {"id": "e2", "from": "agent", "to": "human_gate", "map": {"final_output": "request"}},
        {"id": "e3", "from": "human_gate", "to": "final", "map": {"request": "response"}},
    ]
    nodes = manifest["nodes"]
    assert isinstance(nodes, dict)
    nodes["human_gate"] = {
        "id": "human_gate",
        "type": "human_approval",
        "inputs": {"request": {"type": "string"}},
        "outputs": {"request": {"type": "string"}},
    }
    return manifest


def _wait_event_manifest(workflow_id: str) -> dict[str, object]:
    manifest = make_manifest(workflow_id)
    manifest["edges"] = [
        {"id": "e1", "from": "start", "to": "wait_gate", "map": {"msg": "input"}},
        {"id": "e2", "from": "wait_gate", "to": "final", "map": {"output": "response"}},
    ]
    nodes = manifest["nodes"]
    assert isinstance(nodes, dict)
    nodes["wait_gate"] = {
        "id": "wait_gate",
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


def _tool_approval_manifest(workflow_id: str) -> dict[str, object]:
    manifest = make_manifest(workflow_id)
    manifest["edges"] = [
        {"id": "e1", "from": "start", "to": "tool_gate", "map": {"msg": "input"}},
        {"id": "e2", "from": "tool_gate", "to": "final", "map": {"text": "response"}},
    ]
    nodes = manifest["nodes"]
    assert isinstance(nodes, dict)
    nodes["tool_gate"] = {
        "id": "tool_gate",
        "type": "tool",
        "tool_name": "escalate",
        "inputs": {
            "input": {"type": "string"},
            "arguments": {"type": "structured"},
        },
        "outputs": {
            "text": {"type": "string"},
            "result": {"type": "structured"},
            "metadata": {"type": "structured"},
        },
    }
    manifest["tools"] = {
        "escalate": {
            "registry_ref": "tool.escalate.v1",
            "version_constraint": ">=1.0",
            "requires_approval": True,
        }
    }
    return manifest


def _enable_queue(client: TestClient) -> None:
    client.app.state.config = client.app.state.config.model_copy(
        update={"workflow_run_queue_enabled": True}
    )


def _enable_runtime_approvals(client: TestClient) -> None:
    client.app.state.config = client.app.state.config.model_copy(
        update={
            "workflow_run_queue_enabled": True,
            "workflow_run_runtime_approvals_enabled": True,
            "workflow_run_checkpointing_enabled": True,
        }
    )


def _seed_waiting_approval(
    client: TestClient,
    run_id: str,
    *,
    approval_status: str = "pending",
    with_checkpoint: bool = True,
    node_id: str = "human_gate",
    checkpoint_kind: str = "human_approval",
    checkpoint_output: str = "approved output",
    checkpoint_output_by_port: dict[str, object] | None = None,
    checkpoint_input_by_port: dict[str, object] | None = None,
) -> str:
    now = datetime.now(timezone.utc)
    approval_id = new_runtime_approval_id()
    checkpoint_id = new_workflow_run_checkpoint_id()
    with client.app.state.session_factory() as session:
        run = session.get(CaliberWorkflowRun, run_id)
        assert run is not None
        run.status = "waiting_approval"
        run.current_node_id = node_id
        run.error_code = "waiting_approval"
        run.error_summary = "waiting for runtime approval decision"
        run.last_heartbeat_at = now
        run.lease_expires_at = None
        summary = dict(run.summary or {})
        if with_checkpoint:
            summary["resume_checkpoint_id"] = checkpoint_id
            state_blob: dict[str, object] = {
                "kind": checkpoint_kind,
                "node_id": node_id,
                "output": checkpoint_output,
            }
            if checkpoint_output_by_port is not None:
                state_blob["output_by_port"] = checkpoint_output_by_port
            if checkpoint_input_by_port is not None:
                state_blob["input_by_port"] = checkpoint_input_by_port
            session.add(
                CaliberWorkflowRunCheckpoint(
                    checkpoint_id=checkpoint_id,
                    workflow_run_id=run.workflow_run_id,
                    project_id=run.project_id,
                    sequence=1,
                    node_id=node_id,
                    state_blob=state_blob,
                )
            )
        run.summary = summary
        approval = CaliberRuntimeApprovalRequest(
            runtime_approval_id=approval_id,
            workflow_run_id=run.workflow_run_id,
            project_id=run.project_id,
            node_id=node_id,
            status=approval_status,
            policy_snapshot={"timeout_behavior": "block"},
        )
        if approval_status in {"approved", "rejected"}:
            approval.decided_at = now
            approval.decided_by = "@seed"
            approval.decision_reason = "seeded"
        session.add(approval)
        session.commit()
    return approval_id


def _seed_waiting_event(
    client: TestClient,
    run_id: str,
    *,
    with_checkpoint: bool = True,
    node_id: str = "wait_gate",
    checkpoint_kind: str = "wait_event",
    expected_event_name: str | None = None,
    input_by_port: dict[str, object] | None = None,
    correlation_key: str | None = None,
    correlation_value: object = None,
    timeout_seconds: float | None = None,
    resume_at: str | None = None,
    wait_until: str | None = None,
    timezone_name: str | None = None,
) -> None:
    now = datetime.now(timezone.utc)
    checkpoint_id = new_workflow_run_checkpoint_id()
    with client.app.state.session_factory() as session:
        run = session.get(CaliberWorkflowRun, run_id)
        assert run is not None
        run.status = "waiting_event"
        run.current_node_id = node_id
        run.error_code = "waiting_event"
        run.error_summary = "waiting for resume event"
        run.last_heartbeat_at = now
        run.lease_expires_at = None
        summary = dict(run.summary or {})
        if with_checkpoint:
            summary["resume_checkpoint_id"] = checkpoint_id
            state_blob: dict[str, object]
            if checkpoint_kind == "wait_for_event":
                state_blob = {
                    "kind": "wait_for_event",
                    "node_id": node_id,
                    "output": "resume output",
                    "input_by_port": input_by_port or {"input": "resume output"},
                }
                if expected_event_name:
                    state_blob["expected_event_name"] = expected_event_name
                if correlation_key:
                    state_blob["correlation_key"] = correlation_key
                if correlation_value not in (None, ""):
                    state_blob["correlation_value"] = correlation_value
                if timeout_seconds is not None:
                    state_blob["timeout_seconds"] = timeout_seconds
            elif checkpoint_kind == "wait_until":
                state_blob = {
                    "kind": "wait_until",
                    "node_id": node_id,
                    "output": "resume output",
                    "input_by_port": input_by_port or {"input": "resume output"},
                }
                if resume_at:
                    state_blob["resume_at"] = resume_at
                if wait_until:
                    state_blob["wait_until"] = wait_until
                if timezone_name:
                    state_blob["timezone"] = timezone_name
            else:
                state_blob = {
                    "kind": "wait_event",
                    "node_id": node_id,
                    "output": "resume output",
                    "output_by_port": {"output": "resume output"},
                }
            session.add(
                CaliberWorkflowRunCheckpoint(
                    checkpoint_id=checkpoint_id,
                    workflow_run_id=run.workflow_run_id,
                    project_id=run.project_id,
                    sequence=1,
                    node_id=node_id,
                    state_blob=state_blob,
                )
            )
        run.summary = summary
        session.commit()


def test_create_workflow_run_queue_flag_required(client: TestClient) -> None:
    _wid, vid = create_and_publish(client)
    response = client.post(
        f"{PREFIX}/workflow-runs",
        json={"workflow_version_id": vid, "input": "hello"},
    )
    assert response.status_code == 409
    assert "queue is disabled" in response.json()["detail"]


def test_create_and_get_workflow_run(client: TestClient) -> None:
    _enable_queue(client)
    wid, vid = create_and_publish(client)

    create = client.post(
        f"{PREFIX}/workflow-runs",
        json={"workflow_version_id": vid, "alias": "manual", "input": "hello"},
    )
    assert create.status_code == 202, create.text
    data = create.json()["data"]
    assert data["workflow_id"] == wid
    assert data["workflow_version_id"] == vid
    assert data["status"] == "queued"
    assert data["started_at"] is None
    assert data["queued_at"] is not None
    assert data["source"] == "manual"

    run_id = data["workflow_run_id"]
    get_run = client.get(f"{PREFIX}/workflow-runs/{run_id}")
    assert get_run.status_code == 200
    assert get_run.json()["data"]["workflow_run_id"] == run_id

    events = client.get(f"{PREFIX}/workflow-runs/{run_id}/events")
    assert events.status_code == 200
    event_rows = events.json()["data"]
    assert len(event_rows) == 1
    assert event_rows[0]["sequence"] == 1
    assert event_rows[0]["event_type"] == "workflow.run.queued"

    after = client.get(f"{PREFIX}/workflow-runs/{run_id}/events?after=1")
    assert after.status_code == 200
    assert after.json()["data"] == []


def test_create_workflow_run_succeeds_when_event_bus_publish_raises(
    client: TestClient, monkeypatch
) -> None:
    _enable_queue(client)
    wid, vid = create_and_publish(client)

    class _FailingEventBus:
        def publish(self, payload: dict[str, object]) -> None:
            raise RuntimeError(f"event bus offline: {payload.get('type')}")

    client.app.state.event_bus = _FailingEventBus()
    captured: dict[str, object] = {}

    def _warning(message: str, *args: object, **kwargs: object) -> None:
        captured["message"] = message % args if args else message
        captured["kwargs"] = dict(kwargs)

    monkeypatch.setattr(workflow_runs_routes.logger, "warning", _warning)

    create = client.post(
        f"{PREFIX}/workflow-runs",
        json={"workflow_version_id": vid, "alias": "manual", "input": "hello"},
    )
    assert create.status_code == 202, create.text
    data = create.json()["data"]
    assert data["workflow_id"] == wid
    assert data["workflow_version_id"] == vid
    assert data["status"] == "queued"

    run_id = data["workflow_run_id"]
    events = client.get(f"{PREFIX}/workflow-runs/{run_id}/events")
    assert events.status_code == 200
    event_rows = events.json()["data"]
    assert len(event_rows) == 1
    assert event_rows[0]["event_type"] == "workflow.run.queued"
    assert captured["message"] == "failed to publish workflow-run event type='workflow.run.queued'"
    assert captured["kwargs"] == {"exc_info": True}


def test_get_workflow_run_lineage_returns_retry_chain(client: TestClient) -> None:
    _enable_queue(client)
    _wid, vid = create_and_publish(client)

    created = client.post(
        f"{PREFIX}/workflow-runs",
        json={"workflow_version_id": vid, "alias": "manual", "input": "hello"},
    )
    assert created.status_code == 202, created.text
    root_run_id = created.json()["data"]["workflow_run_id"]

    with client.app.state.session_factory() as session:
        root_run = session.get(CaliberWorkflowRun, root_run_id)
        assert root_run is not None
        root_run.status = "failed"
        root_run.completed_at = datetime.now(timezone.utc)
        session.commit()

    retry_one = client.post(f"{PREFIX}/workflow-runs/{root_run_id}/retry")
    assert retry_one.status_code == 202, retry_one.text
    current_run_id = retry_one.json()["data"]["workflow_run_id"]

    with client.app.state.session_factory() as session:
        current_run = session.get(CaliberWorkflowRun, current_run_id)
        assert current_run is not None
        current_run.status = "failed"
        current_run.completed_at = datetime.now(timezone.utc)
        session.commit()

    retry_two = client.post(f"{PREFIX}/workflow-runs/{current_run_id}/retry")
    assert retry_two.status_code == 202, retry_two.text
    child_run_id = retry_two.json()["data"]["workflow_run_id"]

    lineage = client.get(f"{PREFIX}/workflow-runs/{current_run_id}/lineage")
    assert lineage.status_code == 200, lineage.text
    data = lineage.json()["data"]
    assert data["workflow_run_id"] == current_run_id
    assert data["root_run_id"] == root_run_id
    assert data["total_attempts"] == 3
    assert data["parent_count"] == 1
    assert data["child_count"] == 1
    assert data["missing_parent_id"] is None
    assert data["truncated"] is False
    assert [row["workflow_run_id"] for row in data["runs"]] == [
        root_run_id,
        current_run_id,
        child_run_id,
    ]
    assert [row["attempt_number"] for row in data["runs"]] == [1, 2, 3]


def test_create_workflow_run_persists_manifest_snapshot_and_serves_it_back(
    client: TestClient,
) -> None:
    _enable_queue(client)
    wid, vid = create_and_publish(client)
    manifest = make_manifest(wid)
    manifest["name"] = "Draft Snapshot"

    created = client.post(
        f"{PREFIX}/workflow-runs",
        json={
            "workflow_version_id": vid,
            "workflow_id": wid,
            "alias": "manual",
            "input": "hello",
            "manifest": manifest,
        },
    )
    assert created.status_code == 202, created.text
    run_id = created.json()["data"]["workflow_run_id"]

    with client.app.state.session_factory() as session:
        run = session.get(CaliberWorkflowRun, run_id)
        assert run is not None
        assert run.manifest_snapshot == manifest
        assert run.summary is not None
        assert run.summary["manifest_mode"] == "snapshot"
        assert isinstance(run.summary["manifest_hash"], str)

    manifest_response = client.get(f"{PREFIX}/workflow-runs/{run_id}/manifest")
    assert manifest_response.status_code == 200, manifest_response.text
    manifest_data = manifest_response.json()["data"]
    assert manifest_data["workflow_run_id"] == run_id
    assert manifest_data["workflow_version_id"] == vid
    assert manifest_data["manifest_mode"] == "snapshot"
    assert manifest_data["manifest"]["name"] == "Draft Snapshot"
    assert manifest_data["manifest_hash"]


def test_create_workflow_run_rejects_deployed_alias_manifest_override(
    client: TestClient,
) -> None:
    _enable_queue(client)
    wid, vid = create_and_publish(client)
    manifest = make_manifest(wid)
    manifest["name"] = "Unreviewed production snapshot"

    response = client.post(
        f"{PREFIX}/workflow-runs",
        json={
            "workflow_version_id": vid,
            "alias": "prod",
            "input": "hello",
            "manifest": manifest,
        },
    )

    assert response.status_code == 400
    assert "immutable saved version" in response.json()["detail"]


def test_saved_version_runs_persist_manifest_copy_for_replay_and_survive_version_deletion(
    client: TestClient,
) -> None:
    _enable_queue(client)
    wid, vid = create_and_publish(client)

    created = client.post(
        f"{PREFIX}/workflow-runs",
        json={
            "workflow_version_id": vid,
            "workflow_id": wid,
            "alias": "manual",
            "input": "hello",
        },
    )
    assert created.status_code == 202, created.text
    run_id = created.json()["data"]["workflow_run_id"]

    with client.app.state.session_factory() as session:
        run = session.get(CaliberWorkflowRun, run_id)
        version = session.get(CaliberWorkflowVersion, vid)
        queued_event = (
            session.query(CaliberWorkflowRunEvent)
            .filter(CaliberWorkflowRunEvent.workflow_run_id == run_id)
            .filter(CaliberWorkflowRunEvent.event_type == "workflow.run.queued")
            .one()
        )
        assert run is not None
        assert version is not None
        assert run.manifest_snapshot == version.manifest
        assert run.summary is not None
        assert run.summary["manifest_mode"] == "saved_version"
        assert run.summary["manifest_hash"] == version.manifest_hash
        assert run.summary["workflow_version_number"] == version.version_number
        assert dict(queued_event.payload or {})["manifest_mode"] == "saved_version"
        stored_manifest = run.manifest_snapshot
        session.delete(version)
        session.commit()

    manifest_response = client.get(f"{PREFIX}/workflow-runs/{run_id}/manifest")
    assert manifest_response.status_code == 200, manifest_response.text
    manifest_data = manifest_response.json()["data"]
    assert manifest_data["workflow_run_id"] == run_id
    assert manifest_data["workflow_version_id"] == vid
    assert manifest_data["manifest_mode"] == "saved_version"
    assert manifest_data["manifest"] == stored_manifest
    assert manifest_data["manifest_hash"]


def test_list_workflow_run_checkpoints(client: TestClient) -> None:
    _enable_queue(client)
    _wid, vid = create_and_publish(client)

    created = client.post(
        f"{PREFIX}/workflow-runs",
        json={"workflow_version_id": vid, "alias": "manual", "input": "hello"},
    )
    assert created.status_code == 202, created.text
    run_id = created.json()["data"]["workflow_run_id"]
    checkpoint_one_id = new_workflow_run_checkpoint_id()
    checkpoint_two_id = new_workflow_run_checkpoint_id()

    with client.app.state.session_factory() as session:
        run = session.get(CaliberWorkflowRun, run_id)
        assert run is not None
        summary = dict(run.summary or {})
        summary["resume_checkpoint_id"] = checkpoint_two_id
        run.summary = summary
        session.add_all(
            [
                CaliberWorkflowRunCheckpoint(
                    checkpoint_id=checkpoint_one_id,
                    workflow_run_id=run_id,
                    project_id=run.project_id,
                    sequence=1,
                    node_id="wait_gate",
                    state_blob={
                        "kind": "wait_for_event",
                        "node_id": "wait_gate",
                        "expected_event_name": "ticket.approved",
                    },
                ),
                CaliberWorkflowRunCheckpoint(
                    checkpoint_id=checkpoint_two_id,
                    workflow_run_id=run_id,
                    project_id=run.project_id,
                    sequence=2,
                    node_id="human_gate",
                    state_blob={
                        "kind": "human_approval",
                        "node_id": "human_gate",
                        "output": "approved output",
                    },
                ),
            ]
        )
        session.commit()

    response = client.get(f"{PREFIX}/workflow-runs/{run_id}/checkpoints")
    assert response.status_code == 200
    data = response.json()["data"]
    assert [row["sequence"] for row in data] == [1, 2]
    assert data[0]["checkpoint_id"] == checkpoint_one_id
    assert data[0]["state_blob"]["kind"] == "wait_for_event"
    assert data[1]["checkpoint_id"] == checkpoint_two_id
    assert data[1]["state_blob"]["kind"] == "human_approval"

    after = client.get(f"{PREFIX}/workflow-runs/{run_id}/checkpoints?after=1")
    assert after.status_code == 200
    after_data = after.json()["data"]
    assert len(after_data) == 1
    assert after_data[0]["checkpoint_id"] == checkpoint_two_id


def test_list_workflow_run_checkpoints_includes_runtime_tool_approval_state(
    client: TestClient,
) -> None:
    _enable_queue(client)
    _wid, vid = create_and_publish(client)

    created = client.post(
        f"{PREFIX}/workflow-runs",
        json={"workflow_version_id": vid, "alias": "manual", "input": "escalate ticket T-300"},
    )
    assert created.status_code == 202, created.text
    run_id = created.json()["data"]["workflow_run_id"]
    _seed_waiting_approval(
        client,
        run_id,
        node_id="tool_gate",
        checkpoint_kind="runtime_approval",
        checkpoint_output="escalate ticket T-300",
        checkpoint_input_by_port={"input": "escalate ticket T-300"},
    )

    response = client.get(f"{PREFIX}/workflow-runs/{run_id}/checkpoints")
    assert response.status_code == 200
    data = response.json()["data"]
    assert len(data) == 1
    assert data[0]["node_id"] == "tool_gate"
    assert data[0]["state_blob"] == {
        "kind": "runtime_approval",
        "node_id": "tool_gate",
        "output": "escalate ticket T-300",
        "input_by_port": {"input": "escalate ticket T-300"},
    }


def test_create_workflow_run_by_alias_pins_version(client: TestClient) -> None:
    _enable_queue(client)
    wid, vid = create_and_publish(client)
    promote = client.post(
        f"{PREFIX}/workflows/{wid}/deployments/dev/promote",
        json={"version_id": vid},
    )
    assert promote.status_code == 200, promote.text

    response = client.post(
        f"{PREFIX}/workflow-runs",
        json={"workflow_id": wid, "alias": "dev", "input": "hello"},
    )
    assert response.status_code == 202, response.text
    data = response.json()["data"]
    assert data["workflow_id"] == wid
    assert data["workflow_version_id"] == vid
    assert data["deployment_alias"] == "dev"


def test_workflow_run_idempotency_returns_existing_run(client: TestClient) -> None:
    _enable_queue(client)
    wid, vid = create_and_publish(client)
    payload = {
        "workflow_version_id": vid,
        "workflow_id": wid,
        "idempotency_key": "my-key",
        "source": "manual",
        "input": "hello",
    }
    first = client.post(f"{PREFIX}/workflow-runs", json=payload)
    assert first.status_code == 202
    second = client.post(f"{PREFIX}/workflow-runs", json=payload)
    assert second.status_code == 202
    first_id = first.json()["data"]["workflow_run_id"]
    second_id = second.json()["data"]["workflow_run_id"]
    assert first_id == second_id

    runs = client.get(f"{PREFIX}/workflows/{wid}/runs")
    assert runs.status_code == 200
    queued_rows = [row for row in runs.json()["data"] if row["workflow_run_id"] == first_id]
    assert len(queued_rows) == 1


def test_cancel_queued_run_marks_cancelled(client: TestClient) -> None:
    _enable_queue(client)
    _wid, vid = create_and_publish(client)
    created = client.post(
        f"{PREFIX}/workflow-runs",
        json={"workflow_version_id": vid, "input": "hello"},
    )
    run_id = created.json()["data"]["workflow_run_id"]
    cancelled = client.post(
        f"{PREFIX}/workflow-runs/{run_id}/cancel",
        json={"reason": "operator requested"},
    )
    assert cancelled.status_code == 200, cancelled.text
    data = cancelled.json()["data"]
    assert data["status"] == "cancelled"
    assert data["cancel_requested_by"] == "@test"
    assert data["cancel_reason"] == "operator requested"
    assert data["completed_at"] is not None
    events = client.get(f"{PREFIX}/workflow-runs/{run_id}/events").json()["data"]
    assert [event["event_type"] for event in events] == [
        "workflow.run.queued",
        "workflow.run.cancelled",
    ]


def test_cancel_running_run_sets_cancel_requested(client: TestClient) -> None:
    _enable_queue(client)
    _wid, vid = create_and_publish(client)
    created = client.post(
        f"{PREFIX}/workflow-runs",
        json={"workflow_version_id": vid, "input": "hello"},
    )
    run_id = created.json()["data"]["workflow_run_id"]
    with client.app.state.session_factory() as session:
        run = session.get(CaliberWorkflowRun, run_id)
        assert run is not None
        run.status = "running"
        run.started_at = datetime.now(timezone.utc)
        session.commit()

    response = client.post(
        f"{PREFIX}/workflow-runs/{run_id}/cancel",
        json={"reason": "cancel while running"},
    )
    assert response.status_code == 200, response.text
    data = response.json()["data"]
    assert data["status"] == "running"
    assert data["cancel_requested_at"] is not None
    assert data["cancel_requested_by"] == "@test"
    assert data["cancel_reason"] == "cancel while running"
    events = client.get(f"{PREFIX}/workflow-runs/{run_id}/events").json()["data"]
    assert [event["event_type"] for event in events] == [
        "workflow.run.queued",
        "workflow.run.cancel_requested",
    ]


def test_cancel_waiting_approval_run_marks_cancelled_and_preserves_recovery_evidence(
    client: TestClient,
) -> None:
    _enable_runtime_approvals(client)
    _wid, vid = create_and_publish(client)
    created = client.post(
        f"{PREFIX}/workflow-runs",
        json={"workflow_version_id": vid, "input": "needs approval"},
    )
    assert created.status_code == 202, created.text
    run_id = created.json()["data"]["workflow_run_id"]
    approval_id = _seed_waiting_approval(
        client,
        run_id,
        approval_status="pending",
        checkpoint_output="approval pending",
        checkpoint_output_by_port={"approved": False},
        checkpoint_input_by_port={"request": "needs approval"},
    )

    cancelled = client.post(
        f"{PREFIX}/workflow-runs/{run_id}/cancel",
        json={"reason": "operator aborted paused gate"},
    )
    assert cancelled.status_code == 200, cancelled.text
    data = cancelled.json()["data"]
    assert data["status"] == "cancelled"
    assert data["current_node_id"] is None
    assert data["cancel_requested_by"] == "@test"
    assert data["cancel_reason"] == "operator aborted paused gate"
    assert data["completed_at"] is not None
    assert data["error_code"] == "cancelled"
    assert data["error_summary"] == "operator aborted paused gate"
    assert data["summary"]["status"] == "cancelled"
    assert data["summary"]["cancel_reason"] == "operator aborted paused gate"

    checkpoints = client.get(f"{PREFIX}/workflow-runs/{run_id}/checkpoints")
    assert checkpoints.status_code == 200, checkpoints.text
    checkpoint_rows = checkpoints.json()["data"]
    assert len(checkpoint_rows) == 1
    assert checkpoint_rows[0]["node_id"] == "human_gate"
    assert checkpoint_rows[0]["state_blob"]["kind"] == "human_approval"

    approvals = client.get(f"{PREFIX}/workflow-runs/{run_id}/approvals")
    assert approvals.status_code == 200, approvals.text
    approval_rows = approvals.json()["data"]
    assert len(approval_rows) == 1
    assert approval_rows[0]["runtime_approval_id"] == approval_id
    assert approval_rows[0]["status"] == "pending"

    events = client.get(f"{PREFIX}/workflow-runs/{run_id}/events")
    assert events.status_code == 200, events.text
    assert [event["event_type"] for event in events.json()["data"]] == [
        "workflow.run.queued",
        "workflow.run.cancelled",
    ]


def test_cancel_waiting_event_run_marks_cancelled_and_preserves_checkpoint_metadata(
    client: TestClient,
    monkeypatch,
) -> None:
    _enable_runtime_approvals(client)
    _wid, vid = create_and_publish(client)
    created = client.post(
        f"{PREFIX}/workflow-runs",
        json={"workflow_version_id": vid, "input": "await external event"},
    )
    assert created.status_code == 202, created.text
    run_id = created.json()["data"]["workflow_run_id"]
    _seed_waiting_event(
        client,
        run_id,
        checkpoint_kind="wait_for_event",
        expected_event_name="ticket.approved",
        input_by_port={"ticket_id": "T-42"},
        correlation_key="ticket_id",
        correlation_value="T-42",
        timeout_seconds=300,
    )
    published: list[dict[str, object]] = []
    monkeypatch.setattr(
        client.app.state.event_bus,
        "publish",
        lambda payload: published.append(dict(payload)),
    )

    cancelled = client.post(
        f"{PREFIX}/workflow-runs/{run_id}/cancel",
        json={"reason": "operator aborted waiting event"},
    )
    assert cancelled.status_code == 200, cancelled.text
    data = cancelled.json()["data"]
    assert data["status"] == "cancelled"
    assert data["current_node_id"] is None
    assert data["cancel_requested_by"] == "@test"
    assert data["cancel_reason"] == "operator aborted waiting event"
    assert data["completed_at"] is not None
    assert data["error_code"] == "cancelled"
    assert data["error_summary"] == "operator aborted waiting event"
    assert data["summary"]["status"] == "cancelled"
    assert data["summary"]["cancel_reason"] == "operator aborted waiting event"

    checkpoints = client.get(f"{PREFIX}/workflow-runs/{run_id}/checkpoints")
    assert checkpoints.status_code == 200, checkpoints.text
    checkpoint_rows = checkpoints.json()["data"]
    assert len(checkpoint_rows) == 1
    assert checkpoint_rows[0]["node_id"] == "wait_gate"
    assert checkpoint_rows[0]["state_blob"]["kind"] == "wait_for_event"
    assert checkpoint_rows[0]["state_blob"]["expected_event_name"] == "ticket.approved"
    assert checkpoint_rows[0]["state_blob"]["correlation_key"] == "ticket_id"
    assert checkpoint_rows[0]["state_blob"]["correlation_value"] == "T-42"
    assert checkpoint_rows[0]["state_blob"]["timeout_seconds"] == 300

    events = client.get(f"{PREFIX}/workflow-runs/{run_id}/events")
    assert events.status_code == 200, events.text
    assert [event["event_type"] for event in events.json()["data"]] == [
        "workflow.run.queued",
        "workflow.run.cancelled",
    ]
    assert [event["type"] for event in published] == ["workflow.run.cancelled"]
    assert published[0]["workflow_run_id"] == run_id
    assert published[0]["reason"] == "operator aborted waiting event"


def test_cancel_wait_until_run_marks_cancelled_and_preserves_timing_metadata(
    client: TestClient,
    monkeypatch,
) -> None:
    _enable_runtime_approvals(client)
    _wid, vid = create_and_publish(client)
    created = client.post(
        f"{PREFIX}/workflow-runs",
        json={"workflow_version_id": vid, "input": "wait until tomorrow"},
    )
    assert created.status_code == 202, created.text
    run_id = created.json()["data"]["workflow_run_id"]
    _seed_waiting_event(
        client,
        run_id,
        checkpoint_kind="wait_until",
        input_by_port={"ticket_id": "T-77"},
        resume_at="2026-06-16T12:00:00Z",
        wait_until="2026-06-16 05:00:00",
        timezone_name="America/Los_Angeles",
    )
    published: list[dict[str, object]] = []
    monkeypatch.setattr(
        client.app.state.event_bus,
        "publish",
        lambda payload: published.append(dict(payload)),
    )

    cancelled = client.post(
        f"{PREFIX}/workflow-runs/{run_id}/cancel",
        json={"reason": "operator aborted timed wait"},
    )
    assert cancelled.status_code == 200, cancelled.text
    data = cancelled.json()["data"]
    assert data["status"] == "cancelled"
    assert data["current_node_id"] is None
    assert data["cancel_requested_by"] == "@test"
    assert data["cancel_reason"] == "operator aborted timed wait"
    assert data["completed_at"] is not None
    assert data["error_code"] == "cancelled"
    assert data["error_summary"] == "operator aborted timed wait"
    assert data["summary"]["status"] == "cancelled"
    assert data["summary"]["cancel_reason"] == "operator aborted timed wait"

    checkpoints = client.get(f"{PREFIX}/workflow-runs/{run_id}/checkpoints")
    assert checkpoints.status_code == 200, checkpoints.text
    checkpoint_rows = checkpoints.json()["data"]
    assert len(checkpoint_rows) == 1
    assert checkpoint_rows[0]["node_id"] == "wait_gate"
    assert checkpoint_rows[0]["state_blob"]["kind"] == "wait_until"
    assert checkpoint_rows[0]["state_blob"]["resume_at"] == "2026-06-16T12:00:00Z"
    assert checkpoint_rows[0]["state_blob"]["wait_until"] == "2026-06-16 05:00:00"
    assert checkpoint_rows[0]["state_blob"]["timezone"] == "America/Los_Angeles"

    events = client.get(f"{PREFIX}/workflow-runs/{run_id}/events")
    assert events.status_code == 200, events.text
    assert [event["event_type"] for event in events.json()["data"]] == [
        "workflow.run.queued",
        "workflow.run.cancelled",
    ]
    assert [event["type"] for event in published] == ["workflow.run.cancelled"]
    assert published[0]["workflow_run_id"] == run_id
    assert published[0]["reason"] == "operator aborted timed wait"


def test_retry_creates_queued_attempt_with_parent_lineage(client: TestClient, monkeypatch) -> None:
    _enable_queue(client)
    wid, vid = create_and_publish(client)
    created = client.post(
        f"{PREFIX}/workflow-runs",
        json={"workflow_version_id": vid, "input": "hello"},
    )
    run_id = created.json()["data"]["workflow_run_id"]
    with client.app.state.session_factory() as session:
        run = session.get(CaliberWorkflowRun, run_id)
        assert run is not None
        run.status = "failed"
        run.completed_at = datetime.now(timezone.utc)
        run.error_code = "runtime_error"
        run.error_summary = "seeded failure"
        session.commit()
    published: list[dict[str, object]] = []
    monkeypatch.setattr(
        client.app.state.event_bus,
        "publish",
        lambda payload: published.append(dict(payload)),
    )

    retried = client.post(
        f"{PREFIX}/workflow-runs/{run_id}/retry",
        json={"reason": "retry after fix"},
    )
    assert retried.status_code == 202, retried.text
    data = retried.json()["data"]
    assert data["workflow_run_id"] != run_id
    assert data["status"] == "queued"
    assert data["parent_run_id"] == run_id
    assert data["attempt_number"] == 2
    assert data["workflow_version_id"] == vid
    assert [event["type"] for event in published] == [
        "workflow.run.retried",
        "workflow.run.queued",
    ]
    assert published[0]["workflow_run_id"] == run_id
    assert published[0]["retried_run_id"] == data["workflow_run_id"]
    assert published[1]["workflow_run_id"] == data["workflow_run_id"]
    assert published[1]["retry_of"] == run_id

    runs = client.get(f"{PREFIX}/workflows/{wid}/runs")
    assert runs.status_code == 200
    run_ids = {row["workflow_run_id"] for row in runs.json()["data"]}
    assert run_id in run_ids
    assert data["workflow_run_id"] in run_ids


def test_retry_preserves_full_input_payload_for_worker_replay(client: TestClient) -> None:
    _enable_queue(client)
    _wid, vid = create_and_publish(client)
    big = "Y" * 5000
    created = client.post(
        f"{PREFIX}/workflow-runs",
        json={"workflow_version_id": vid, "input": big},
    )
    assert created.status_code == 202, created.text
    run_id = created.json()["data"]["workflow_run_id"]
    with client.app.state.session_factory() as session:
        run = session.get(CaliberWorkflowRun, run_id)
        assert run is not None
        run.status = "failed"
        run.completed_at = datetime.now(timezone.utc)
        session.commit()

    retried = client.post(f"{PREFIX}/workflow-runs/{run_id}/retry")
    assert retried.status_code == 202, retried.text
    retried_id = retried.json()["data"]["workflow_run_id"]

    with client.app.state.session_factory() as session:
        retried_run = session.get(CaliberWorkflowRun, retried_id)
        assert retried_run is not None
        assert retried_run.input_payload == big
        assert retried_run.summary is not None
        assert retried_run.summary["input"] == big[:1000]


def test_retry_preserves_manifest_snapshot_for_draft_runs(client: TestClient) -> None:
    _enable_queue(client)
    wid, vid = create_and_publish(client)
    manifest = make_manifest(wid)
    manifest["name"] = "Retry Draft Snapshot"
    created = client.post(
        f"{PREFIX}/workflow-runs",
        json={"workflow_version_id": vid, "input": "hello", "manifest": manifest},
    )
    assert created.status_code == 202, created.text
    run_id = created.json()["data"]["workflow_run_id"]
    with client.app.state.session_factory() as session:
        run = session.get(CaliberWorkflowRun, run_id)
        assert run is not None
        run.status = "failed"
        run.completed_at = datetime.now(timezone.utc)
        session.commit()

    retried = client.post(f"{PREFIX}/workflow-runs/{run_id}/retry")
    assert retried.status_code == 202, retried.text
    retried_id = retried.json()["data"]["workflow_run_id"]

    with client.app.state.session_factory() as session:
        retried_run = session.get(CaliberWorkflowRun, retried_id)
        assert retried_run is not None
        assert retried_run.manifest_snapshot == manifest
        assert retried_run.summary is not None
        assert retried_run.summary["manifest_mode"] == "snapshot"
        assert retried_run.summary["manifest_hash"]

    manifest_response = client.get(f"{PREFIX}/workflow-runs/{retried_id}/manifest")
    assert manifest_response.status_code == 200, manifest_response.text
    manifest_data = manifest_response.json()["data"]
    assert manifest_data["manifest_mode"] == "snapshot"
    assert manifest_data["manifest"]["name"] == "Retry Draft Snapshot"
    events = client.get(f"{PREFIX}/workflow-runs/{retried_id}/events")
    assert events.status_code == 200
    queued_event = events.json()["data"][0]
    assert queued_event["payload"]["manifest_mode"] == "snapshot"


def test_retry_backfills_missing_saved_version_snapshot_from_workflow_version(
    client: TestClient,
) -> None:
    _enable_queue(client)
    _wid, vid = create_and_publish(client)
    created = client.post(
        f"{PREFIX}/workflow-runs",
        json={"workflow_version_id": vid, "input": "legacy retry"},
    )
    assert created.status_code == 202, created.text
    run_id = created.json()["data"]["workflow_run_id"]

    with client.app.state.session_factory() as session:
        run = session.get(CaliberWorkflowRun, run_id)
        version = session.get(CaliberWorkflowVersion, vid)
        assert run is not None
        assert version is not None
        stored_manifest = dict(version.manifest)
        run.status = "failed"
        run.completed_at = datetime.now(timezone.utc)
        run.manifest_snapshot = None
        run.summary = {
            "preview": False,
            "status": "failed",
            "input": "legacy retry",
        }
        session.commit()

    retried = client.post(f"{PREFIX}/workflow-runs/{run_id}/retry")
    assert retried.status_code == 202, retried.text
    retried_id = retried.json()["data"]["workflow_run_id"]

    with client.app.state.session_factory() as session:
        retried_run = session.get(CaliberWorkflowRun, retried_id)
        version = session.get(CaliberWorkflowVersion, vid)
        assert retried_run is not None
        assert version is not None
        assert retried_run.manifest_snapshot == stored_manifest
        assert retried_run.summary is not None
        assert retried_run.summary["manifest_mode"] == "saved_version"
        assert retried_run.summary["manifest_hash"] == version.manifest_hash
        assert retried_run.summary["workflow_version_number"] == version.version_number
        queued_event = (
            session.query(CaliberWorkflowRunEvent)
            .filter(CaliberWorkflowRunEvent.workflow_run_id == retried_id)
            .one()
        )
        assert dict(queued_event.payload or {})["manifest_mode"] == "saved_version"
        session.delete(version)
        session.commit()

    manifest_response = client.get(f"{PREFIX}/workflow-runs/{retried_id}/manifest")
    assert manifest_response.status_code == 200, manifest_response.text
    manifest_data = manifest_response.json()["data"]
    assert manifest_data["manifest_mode"] == "saved_version"
    assert manifest_data["manifest"] == stored_manifest


def test_retry_rejects_when_legacy_run_lacks_snapshot_and_workflow_version(
    client: TestClient,
) -> None:
    _enable_queue(client)
    _wid, vid = create_and_publish(client)
    created = client.post(
        f"{PREFIX}/workflow-runs",
        json={"workflow_version_id": vid, "input": "legacy retry"},
    )
    assert created.status_code == 202, created.text
    run_id = created.json()["data"]["workflow_run_id"]

    with client.app.state.session_factory() as session:
        run = session.get(CaliberWorkflowRun, run_id)
        version = session.get(CaliberWorkflowVersion, vid)
        assert run is not None
        assert version is not None
        run.status = "failed"
        run.completed_at = datetime.now(timezone.utc)
        run.manifest_snapshot = None
        run.summary = {
            "preview": False,
            "status": "failed",
            "input": "legacy retry",
        }
        session.delete(version)
        session.commit()

    retried = client.post(f"{PREFIX}/workflow-runs/{run_id}/retry")
    assert retried.status_code == 409
    assert "persisted manifest snapshot or workflow version" in retried.json()["detail"]


def test_retry_can_seed_resume_checkpoint_from_parent_run(client: TestClient) -> None:
    _enable_queue(client)
    workflow_id = "route-retry-checkpoint-approval"
    _wid, vid = create_and_publish(client, manifest=_approval_manifest(workflow_id))
    created = client.post(
        f"{PREFIX}/workflow-runs",
        json={"workflow_version_id": vid, "input": "checkpoint replay"},
    )
    assert created.status_code == 202, created.text
    run_id = created.json()["data"]["workflow_run_id"]
    checkpoint_id = new_workflow_run_checkpoint_id()
    with client.app.state.session_factory() as session:
        run = session.get(CaliberWorkflowRun, run_id)
        assert run is not None
        run.status = "failed"
        run.completed_at = datetime.now(timezone.utc)
        session.add(
            CaliberWorkflowRunCheckpoint(
                checkpoint_id=checkpoint_id,
                workflow_run_id=run.workflow_run_id,
                project_id=run.project_id,
                sequence=1,
                node_id="human_gate",
                state_blob={
                    "kind": "human_approval",
                    "node_id": "human_gate",
                    "output": "approved output",
                    "output_by_port": {"request": "approved output"},
                    "input_by_port": {"request": "checkpoint replay"},
                },
            )
        )
        session.commit()

    retried = client.post(
        f"{PREFIX}/workflow-runs/{run_id}/retry",
        json={"reason": "recover from checkpoint", "checkpoint_id": checkpoint_id},
    )
    assert retried.status_code == 202, retried.text
    retried_id = retried.json()["data"]["workflow_run_id"]

    with client.app.state.session_factory() as session:
        retried_run = session.get(CaliberWorkflowRun, retried_id)
        assert retried_run is not None
        assert retried_run.summary is not None
        assert retried_run.summary["resume_checkpoint_id"] == checkpoint_id
        assert retried_run.summary["resume_checkpoint_run_id"] == run_id
        assert retried_run.summary["retry_mode"] == "checkpoint"

    events = client.get(f"{PREFIX}/workflow-runs/{retried_id}/events")
    assert events.status_code == 200
    queued_event = events.json()["data"][0]
    assert queued_event["payload"]["checkpoint_id"] == checkpoint_id


def test_retry_rejects_checkpoint_missing_from_current_manifest(client: TestClient) -> None:
    _enable_queue(client)
    workflow_id = "route-retry-checkpoint-missing-node"
    _wid, vid = create_and_publish(client, manifest=_approval_manifest(workflow_id))
    created = client.post(
        f"{PREFIX}/workflow-runs",
        json={"workflow_version_id": vid, "input": "checkpoint replay"},
    )
    assert created.status_code == 202, created.text
    run_id = created.json()["data"]["workflow_run_id"]
    checkpoint_id = new_workflow_run_checkpoint_id()
    with client.app.state.session_factory() as session:
        run = session.get(CaliberWorkflowRun, run_id)
        assert run is not None
        run.status = "failed"
        run.completed_at = datetime.now(timezone.utc)
        manifest_snapshot = dict(run.manifest_snapshot or {})
        nodes = dict(manifest_snapshot.get("nodes") or {})
        human_gate = dict(nodes.pop("human_gate"))
        human_gate["id"] = "human_gate_v2"
        nodes["human_gate_v2"] = human_gate
        manifest_snapshot["nodes"] = nodes
        manifest_snapshot["edges"] = [
            {
                **dict(edge),
                "to": "human_gate_v2" if edge.get("to") == "human_gate" else edge.get("to"),
                "from": "human_gate_v2" if edge.get("from") == "human_gate" else edge.get("from"),
            }
            for edge in list(manifest_snapshot.get("edges") or [])
        ]
        run.manifest_snapshot = manifest_snapshot
        session.add(
            CaliberWorkflowRunCheckpoint(
                checkpoint_id=checkpoint_id,
                workflow_run_id=run.workflow_run_id,
                project_id=run.project_id,
                sequence=1,
                node_id="human_gate",
                state_blob={
                    "kind": "human_approval",
                    "node_id": "human_gate",
                    "output": "approved output",
                    "output_by_port": {"request": "approved output"},
                    "input_by_port": {"request": "checkpoint replay"},
                },
            )
        )
        session.commit()

    retried = client.post(
        f"{PREFIX}/workflow-runs/{run_id}/retry",
        json={"reason": "recover from checkpoint", "checkpoint_id": checkpoint_id},
    )
    assert retried.status_code == 409, retried.text
    assert (
        retried.json()["detail"]
        == "workflow run retry checkpoint node 'human_gate' is not present in the current manifest"
    )

    with client.app.state.session_factory() as session:
        retried_runs = (
            session.query(CaliberWorkflowRun)
            .filter(CaliberWorkflowRun.parent_run_id == run_id)
            .all()
        )
        assert retried_runs == []


def test_retry_rejects_checkpoint_kind_drift_in_current_manifest(client: TestClient) -> None:
    _enable_queue(client)
    workflow_id = "route-retry-checkpoint-kind-drift"
    _wid, vid = create_and_publish(client, manifest=_approval_manifest(workflow_id))
    created = client.post(
        f"{PREFIX}/workflow-runs",
        json={"workflow_version_id": vid, "input": "checkpoint replay"},
    )
    assert created.status_code == 202, created.text
    run_id = created.json()["data"]["workflow_run_id"]
    checkpoint_id = new_workflow_run_checkpoint_id()
    with client.app.state.session_factory() as session:
        run = session.get(CaliberWorkflowRun, run_id)
        assert run is not None
        run.status = "failed"
        run.completed_at = datetime.now(timezone.utc)
        manifest_snapshot = dict(run.manifest_snapshot or {})
        nodes = dict(manifest_snapshot.get("nodes") or {})
        nodes["human_gate"] = {
            "id": "human_gate",
            "type": "python_code",
            "code": 'return {"output": payload["input"]}',
            "inputs": {"input": {"type": "string"}},
            "outputs": {"output": {"type": "string"}},
        }
        manifest_snapshot["nodes"] = nodes
        run.manifest_snapshot = manifest_snapshot
        session.add(
            CaliberWorkflowRunCheckpoint(
                checkpoint_id=checkpoint_id,
                workflow_run_id=run.workflow_run_id,
                project_id=run.project_id,
                sequence=1,
                node_id="human_gate",
                state_blob={
                    "kind": "human_approval",
                    "node_id": "human_gate",
                    "output": "approved output",
                    "output_by_port": {"request": "approved output"},
                    "input_by_port": {"request": "checkpoint replay"},
                },
            )
        )
        session.commit()

    retried = client.post(
        f"{PREFIX}/workflow-runs/{run_id}/retry",
        json={"reason": "recover from checkpoint", "checkpoint_id": checkpoint_id},
    )
    assert retried.status_code == 409, retried.text
    assert (
        retried.json()["detail"]
        == "workflow run retry checkpoint kind 'human_approval' does not match current manifest node 'human_gate' type 'python_code'"
    )

    with client.app.state.session_factory() as session:
        retried_runs = (
            session.query(CaliberWorkflowRun)
            .filter(CaliberWorkflowRun.parent_run_id == run_id)
            .all()
        )
        assert retried_runs == []


def test_retry_rejects_checkpoint_missing_input_snapshot(client: TestClient) -> None:
    _enable_queue(client)
    workflow_id = "route-retry-checkpoint-missing-input"
    _wid, vid = create_and_publish(client, manifest=_approval_manifest(workflow_id))
    created = client.post(
        f"{PREFIX}/workflow-runs",
        json={"workflow_version_id": vid, "input": "checkpoint replay"},
    )
    assert created.status_code == 202, created.text
    run_id = created.json()["data"]["workflow_run_id"]
    checkpoint_id = new_workflow_run_checkpoint_id()
    with client.app.state.session_factory() as session:
        run = session.get(CaliberWorkflowRun, run_id)
        assert run is not None
        run.status = "failed"
        run.completed_at = datetime.now(timezone.utc)
        session.add(
            CaliberWorkflowRunCheckpoint(
                checkpoint_id=checkpoint_id,
                workflow_run_id=run.workflow_run_id,
                project_id=run.project_id,
                sequence=1,
                node_id="human_gate",
                state_blob={
                    "kind": "human_approval",
                    "node_id": "human_gate",
                    "output": "approved output",
                    "output_by_port": {"request": "approved output"},
                },
            )
        )
        session.commit()

    retried = client.post(
        f"{PREFIX}/workflow-runs/{run_id}/retry",
        json={"reason": "recover from checkpoint", "checkpoint_id": checkpoint_id},
    )
    assert retried.status_code == 409, retried.text
    assert retried.json()["detail"] == "workflow run retry checkpoint is missing its input snapshot"


def test_retry_rejects_corrupt_checkpoint_payload(client: TestClient) -> None:
    _enable_queue(client)
    workflow_id = "route-retry-checkpoint-corrupt-payload"
    _wid, vid = create_and_publish(client, manifest=_approval_manifest(workflow_id))
    created = client.post(
        f"{PREFIX}/workflow-runs",
        json={"workflow_version_id": vid, "input": "checkpoint replay"},
    )
    assert created.status_code == 202, created.text
    run_id = created.json()["data"]["workflow_run_id"]
    checkpoint_id = new_workflow_run_checkpoint_id()
    with client.app.state.session_factory() as session:
        run = session.get(CaliberWorkflowRun, run_id)
        assert run is not None
        run.status = "failed"
        run.completed_at = datetime.now(timezone.utc)
        session.add(
            CaliberWorkflowRunCheckpoint(
                checkpoint_id=checkpoint_id,
                workflow_run_id=run.workflow_run_id,
                project_id=run.project_id,
                sequence=1,
                node_id="human_gate",
                state_blob=["corrupt-checkpoint-payload"],
            )
        )
        session.commit()

    retried = client.post(
        f"{PREFIX}/workflow-runs/{run_id}/retry",
        json={"reason": "recover from checkpoint", "checkpoint_id": checkpoint_id},
    )
    assert retried.status_code == 409, retried.text
    assert retried.json()["detail"] == "workflow run retry checkpoint is corrupt"

    with client.app.state.session_factory() as session:
        retried_runs = (
            session.query(CaliberWorkflowRun)
            .filter(CaliberWorkflowRun.parent_run_id == run_id)
            .all()
        )
        assert retried_runs == []


def test_retry_rejects_checkpoint_missing_node_id(client: TestClient) -> None:
    _enable_queue(client)
    workflow_id = "route-retry-checkpoint-missing-node-id"
    _wid, vid = create_and_publish(client, manifest=_approval_manifest(workflow_id))
    created = client.post(
        f"{PREFIX}/workflow-runs",
        json={"workflow_version_id": vid, "input": "checkpoint replay"},
    )
    assert created.status_code == 202, created.text
    run_id = created.json()["data"]["workflow_run_id"]
    checkpoint_id = new_workflow_run_checkpoint_id()
    with client.app.state.session_factory() as session:
        run = session.get(CaliberWorkflowRun, run_id)
        assert run is not None
        run.status = "failed"
        run.completed_at = datetime.now(timezone.utc)
        session.add(
            CaliberWorkflowRunCheckpoint(
                checkpoint_id=checkpoint_id,
                workflow_run_id=run.workflow_run_id,
                project_id=run.project_id,
                sequence=1,
                node_id="human_gate",
                state_blob={
                    "kind": "human_approval",
                    "output": "approved output",
                    "output_by_port": {"request": "approved output"},
                    "input_by_port": {"request": "checkpoint replay"},
                },
            )
        )
        session.commit()

    retried = client.post(
        f"{PREFIX}/workflow-runs/{run_id}/retry",
        json={"reason": "recover from checkpoint", "checkpoint_id": checkpoint_id},
    )
    assert retried.status_code == 409, retried.text
    assert retried.json()["detail"] == "workflow run retry checkpoint is missing node_id"

    with client.app.state.session_factory() as session:
        retried_runs = (
            session.query(CaliberWorkflowRun)
            .filter(CaliberWorkflowRun.parent_run_id == run_id)
            .all()
        )
        assert retried_runs == []


def test_retry_rejects_checkpoint_node_identity_mismatch(client: TestClient) -> None:
    _enable_queue(client)
    workflow_id = "route-retry-checkpoint-node-mismatch"
    _wid, vid = create_and_publish(client, manifest=_approval_manifest(workflow_id))
    created = client.post(
        f"{PREFIX}/workflow-runs",
        json={"workflow_version_id": vid, "input": "checkpoint replay"},
    )
    assert created.status_code == 202, created.text
    run_id = created.json()["data"]["workflow_run_id"]
    checkpoint_id = new_workflow_run_checkpoint_id()
    with client.app.state.session_factory() as session:
        run = session.get(CaliberWorkflowRun, run_id)
        assert run is not None
        run.status = "failed"
        run.completed_at = datetime.now(timezone.utc)
        session.add(
            CaliberWorkflowRunCheckpoint(
                checkpoint_id=checkpoint_id,
                workflow_run_id=run.workflow_run_id,
                project_id=run.project_id,
                sequence=1,
                node_id="human_gate_v2",
                state_blob={
                    "kind": "human_approval",
                    "node_id": "human_gate",
                    "output": "approved output",
                    "output_by_port": {"request": "approved output"},
                    "input_by_port": {"request": "checkpoint replay"},
                },
            )
        )
        session.commit()

    retried = client.post(
        f"{PREFIX}/workflow-runs/{run_id}/retry",
        json={"reason": "recover from checkpoint", "checkpoint_id": checkpoint_id},
    )
    assert retried.status_code == 409, retried.text
    assert (
        retried.json()["detail"]
        == "workflow run retry checkpoint does not match its stored node identity"
    )


def test_retry_rejects_runtime_approval_checkpoint_when_tool_no_longer_requires_approval(
    client: TestClient,
) -> None:
    _enable_queue(client)
    workflow_id = "route-retry-runtime-approval-no-longer-required"
    _wid, vid = create_and_publish(client, manifest=_tool_approval_manifest(workflow_id))
    created = client.post(
        f"{PREFIX}/workflow-runs",
        json={"workflow_version_id": vid, "input": "checkpoint replay"},
    )
    assert created.status_code == 202, created.text
    run_id = created.json()["data"]["workflow_run_id"]
    checkpoint_id = new_workflow_run_checkpoint_id()
    with client.app.state.session_factory() as session:
        run = session.get(CaliberWorkflowRun, run_id)
        assert run is not None
        run.status = "failed"
        run.completed_at = datetime.now(timezone.utc)
        manifest_snapshot = dict(run.manifest_snapshot or {})
        tools = dict(manifest_snapshot.get("tools") or {})
        escalate = dict(tools.get("escalate") or {})
        escalate["requires_approval"] = False
        tools["escalate"] = escalate
        manifest_snapshot["tools"] = tools
        run.manifest_snapshot = manifest_snapshot
        session.add(
            CaliberWorkflowRunCheckpoint(
                checkpoint_id=checkpoint_id,
                workflow_run_id=run.workflow_run_id,
                project_id=run.project_id,
                sequence=1,
                node_id="tool_gate",
                state_blob={
                    "kind": "runtime_approval",
                    "node_id": "tool_gate",
                    "output": "checkpoint replay",
                    "output_by_port": {"text": "checkpoint replay"},
                    "input_by_port": {"input": "checkpoint replay"},
                },
            )
        )
        session.commit()

    retried = client.post(
        f"{PREFIX}/workflow-runs/{run_id}/retry",
        json={"reason": "recover from checkpoint", "checkpoint_id": checkpoint_id},
    )
    assert retried.status_code == 409, retried.text
    assert (
        retried.json()["detail"]
        == "workflow run retry checkpoint kind 'runtime_approval' does not match current manifest node 'tool_gate' type 'tool'"
    )


def test_retry_rejects_wait_for_event_checkpoint_without_resume_payload(client: TestClient) -> None:
    _enable_queue(client)
    workflow_id = "route-retry-checkpoint-missing-event-payload"
    _wid, vid = create_and_publish(client, manifest=_wait_event_manifest(workflow_id))
    created = client.post(
        f"{PREFIX}/workflow-runs",
        json={"workflow_version_id": vid, "input": "checkpoint replay"},
    )
    assert created.status_code == 202, created.text
    run_id = created.json()["data"]["workflow_run_id"]
    checkpoint_id = new_workflow_run_checkpoint_id()
    with client.app.state.session_factory() as session:
        run = session.get(CaliberWorkflowRun, run_id)
        assert run is not None
        run.status = "failed"
        run.completed_at = datetime.now(timezone.utc)
        session.add(
            CaliberWorkflowRunCheckpoint(
                checkpoint_id=checkpoint_id,
                workflow_run_id=run.workflow_run_id,
                project_id=run.project_id,
                sequence=1,
                node_id="wait_gate",
                state_blob={
                    "kind": "wait_for_event",
                    "node_id": "wait_gate",
                    "output": "resume output",
                    "input_by_port": {"input": "checkpoint replay"},
                    "expected_event_name": "ticket.approved",
                    "resume_event_inputs": {"event_name": "ticket.approved"},
                },
            )
        )
        session.commit()

    retried = client.post(
        f"{PREFIX}/workflow-runs/{run_id}/retry",
        json={"reason": "recover from checkpoint", "checkpoint_id": checkpoint_id},
    )
    assert retried.status_code == 409, retried.text
    assert (
        retried.json()["detail"]
        == "workflow run retry checkpoint is missing its stored resume event payload"
    )


def test_retry_rejects_wait_for_event_checkpoint_missing_expected_event_name(
    client: TestClient,
) -> None:
    _enable_queue(client)
    workflow_id = "route-retry-checkpoint-missing-expected-event-name"
    _wid, vid = create_and_publish(client, manifest=_wait_event_manifest(workflow_id))
    created = client.post(
        f"{PREFIX}/workflow-runs",
        json={"workflow_version_id": vid, "input": "checkpoint replay"},
    )
    assert created.status_code == 202, created.text
    run_id = created.json()["data"]["workflow_run_id"]
    checkpoint_id = new_workflow_run_checkpoint_id()
    with client.app.state.session_factory() as session:
        run = session.get(CaliberWorkflowRun, run_id)
        assert run is not None
        run.status = "failed"
        run.completed_at = datetime.now(timezone.utc)
        session.add(
            CaliberWorkflowRunCheckpoint(
                checkpoint_id=checkpoint_id,
                workflow_run_id=run.workflow_run_id,
                project_id=run.project_id,
                sequence=1,
                node_id="wait_gate",
                state_blob={
                    "kind": "wait_for_event",
                    "node_id": "wait_gate",
                    "output": "resume output",
                    "input_by_port": {"input": "checkpoint replay"},
                    "resume_event_inputs": {
                        "event_name": "ticket.approved",
                        "resume_event": {"ticket_id": "T-42", "approved": True},
                    },
                },
            )
        )
        session.commit()

    retried = client.post(
        f"{PREFIX}/workflow-runs/{run_id}/retry",
        json={"reason": "recover from checkpoint", "checkpoint_id": checkpoint_id},
    )
    assert retried.status_code == 409, retried.text
    assert (
        retried.json()["detail"]
        == "workflow run retry checkpoint is missing its expected event name"
    )


def test_retry_rejects_wait_for_event_checkpoint_with_mismatched_event_name(
    client: TestClient,
) -> None:
    _enable_queue(client)
    workflow_id = "route-retry-checkpoint-mismatched-event-name"
    _wid, vid = create_and_publish(client, manifest=_wait_event_manifest(workflow_id))
    created = client.post(
        f"{PREFIX}/workflow-runs",
        json={"workflow_version_id": vid, "input": "checkpoint replay"},
    )
    assert created.status_code == 202, created.text
    run_id = created.json()["data"]["workflow_run_id"]
    checkpoint_id = new_workflow_run_checkpoint_id()
    with client.app.state.session_factory() as session:
        run = session.get(CaliberWorkflowRun, run_id)
        assert run is not None
        run.status = "failed"
        run.completed_at = datetime.now(timezone.utc)
        session.add(
            CaliberWorkflowRunCheckpoint(
                checkpoint_id=checkpoint_id,
                workflow_run_id=run.workflow_run_id,
                project_id=run.project_id,
                sequence=1,
                node_id="wait_gate",
                state_blob={
                    "kind": "wait_for_event",
                    "node_id": "wait_gate",
                    "output": "resume output",
                    "input_by_port": {"input": "checkpoint replay"},
                    "expected_event_name": "ticket.approved",
                    "resume_event_inputs": {
                        "event_name": "ticket.rejected",
                        "resume_event": {"ticket_id": "T-42", "approved": True},
                    },
                },
            )
        )
        session.commit()

    retried = client.post(
        f"{PREFIX}/workflow-runs/{run_id}/retry",
        json={"reason": "recover from checkpoint", "checkpoint_id": checkpoint_id},
    )
    assert retried.status_code == 409, retried.text
    assert (
        retried.json()["detail"]
        == "workflow run retry checkpoint event 'ticket.rejected' does not match expected event 'ticket.approved'"
    )


def test_retry_honors_max_attempt_budget(client: TestClient) -> None:
    _enable_queue(client)
    client.app.state.config = client.app.state.config.model_copy(
        update={"workflow_run_max_attempts": 2}
    )
    _wid, vid = create_and_publish(client)
    created = client.post(
        f"{PREFIX}/workflow-runs",
        json={"workflow_version_id": vid, "input": "hello"},
    )
    run_id = created.json()["data"]["workflow_run_id"]
    with client.app.state.session_factory() as session:
        run = session.get(CaliberWorkflowRun, run_id)
        assert run is not None
        run.status = "failed"
        run.attempt_number = 2
        run.completed_at = datetime.now(timezone.utc)
        session.commit()

    response = client.post(f"{PREFIX}/workflow-runs/{run_id}/retry")
    assert response.status_code == 409
    assert "retry limit reached" in response.json()["detail"]


def test_runtime_approval_routes_approve_and_resume(client: TestClient) -> None:
    _enable_runtime_approvals(client)
    _wid, vid = create_and_publish(client)
    created = client.post(
        f"{PREFIX}/workflow-runs",
        json={"workflow_version_id": vid, "input": "hello"},
    )
    run_id = created.json()["data"]["workflow_run_id"]
    original_queued_at = created.json()["data"]["queued_at"]
    approval_id = _seed_waiting_approval(
        client,
        run_id,
        approval_status="pending",
        checkpoint_input_by_port={"input": "hello"},
    )

    approvals = client.get(f"{PREFIX}/workflow-runs/{run_id}/approvals")
    assert approvals.status_code == 200
    approvals_data = approvals.json()["data"]
    assert len(approvals_data) == 1
    assert approvals_data[0]["runtime_approval_id"] == approval_id
    assert approvals_data[0]["status"] == "pending"

    approved = client.post(
        f"{PREFIX}/workflow-runs/{run_id}/approval/approve",
        json={"reason": "looks good"},
    )
    assert approved.status_code == 200, approved.text
    run_data = approved.json()["data"]
    assert run_data["status"] == "waiting_approval"

    approvals_after = client.get(f"{PREFIX}/workflow-runs/{run_id}/approvals").json()["data"]
    assert approvals_after[0]["status"] == "approved"
    assert approvals_after[0]["decided_by"] == "@test"
    assert approvals_after[0]["decision_reason"] == "looks good"

    resumed = client.post(f"{PREFIX}/workflow-runs/{run_id}/resume")
    assert resumed.status_code == 202, resumed.text
    resumed_data = resumed.json()["data"]
    assert resumed_data["status"] == "queued"
    assert resumed_data["queued_at"] == original_queued_at.removesuffix("Z")
    assert resumed_data["error_code"] is None


def test_runtime_tool_approval_routes_approve_and_resume(client: TestClient) -> None:
    _enable_runtime_approvals(client)
    _wid, vid = create_and_publish(client)
    created = client.post(
        f"{PREFIX}/workflow-runs",
        json={"workflow_version_id": vid, "input": "escalate ticket T-300"},
    )
    run_id = created.json()["data"]["workflow_run_id"]
    approval_id = _seed_waiting_approval(
        client,
        run_id,
        approval_status="pending",
        node_id="tool_gate",
        checkpoint_kind="runtime_approval",
        checkpoint_output="escalate ticket T-300",
        checkpoint_input_by_port={"input": "escalate ticket T-300"},
    )

    approvals = client.get(f"{PREFIX}/workflow-runs/{run_id}/approvals")
    assert approvals.status_code == 200
    approvals_data = approvals.json()["data"]
    assert len(approvals_data) == 1
    assert approvals_data[0]["runtime_approval_id"] == approval_id
    assert approvals_data[0]["node_id"] == "tool_gate"

    approved = client.post(
        f"{PREFIX}/workflow-runs/{run_id}/approval/approve",
        json={"reason": "tool execution approved"},
    )
    assert approved.status_code == 200, approved.text
    assert approved.json()["data"]["status"] == "waiting_approval"

    resumed = client.post(f"{PREFIX}/workflow-runs/{run_id}/resume")
    assert resumed.status_code == 202, resumed.text
    resumed_data = resumed.json()["data"]
    assert resumed_data["status"] == "queued"
    assert resumed_data["error_code"] is None
    assert resumed_data["current_node_id"] is None

    with client.app.state.session_factory() as session:
        run = session.get(CaliberWorkflowRun, run_id)
        assert run is not None
        checkpoint_id = dict(run.summary or {}).get("resume_checkpoint_id")
        checkpoint = session.get(CaliberWorkflowRunCheckpoint, checkpoint_id)
        assert checkpoint is not None
        assert checkpoint.state_blob["kind"] == "runtime_approval"
        assert checkpoint.state_blob["input_by_port"] == {"input": "escalate ticket T-300"}


def test_runtime_approval_approve_rejects_corrupt_checkpoint_payload(client: TestClient) -> None:
    _enable_runtime_approvals(client)
    _wid, vid = create_and_publish(client)
    created = client.post(
        f"{PREFIX}/workflow-runs",
        json={"workflow_version_id": vid, "input": "hello"},
    )
    run_id = created.json()["data"]["workflow_run_id"]
    approval_id = _seed_waiting_approval(client, run_id, approval_status="pending")

    with client.app.state.session_factory() as session:
        run = session.get(CaliberWorkflowRun, run_id)
        assert run is not None
        checkpoint_id = dict(run.summary or {}).get("resume_checkpoint_id")
        checkpoint = session.get(CaliberWorkflowRunCheckpoint, checkpoint_id)
        assert checkpoint is not None
        checkpoint.state_blob = ["corrupt-checkpoint-payload"]
        session.commit()

    approved = client.post(
        f"{PREFIX}/workflow-runs/{run_id}/approval/approve",
        json={"reason": "looks good"},
    )
    assert approved.status_code == 409, approved.text
    assert approved.json()["detail"] == "workflow run approval checkpoint is corrupt"

    approvals_after = client.get(f"{PREFIX}/workflow-runs/{run_id}/approvals").json()["data"]
    assert approvals_after[0]["runtime_approval_id"] == approval_id
    assert approvals_after[0]["status"] == "pending"


def test_runtime_approval_approve_rejects_missing_input_snapshot(client: TestClient) -> None:
    _enable_runtime_approvals(client)
    _wid, vid = create_and_publish(client)
    created = client.post(
        f"{PREFIX}/workflow-runs",
        json={"workflow_version_id": vid, "input": "hello"},
    )
    run_id = created.json()["data"]["workflow_run_id"]
    _seed_waiting_approval(client, run_id, approval_status="pending")

    with client.app.state.session_factory() as session:
        run = session.get(CaliberWorkflowRun, run_id)
        assert run is not None
        checkpoint_id = dict(run.summary or {}).get("resume_checkpoint_id")
        checkpoint = session.get(CaliberWorkflowRunCheckpoint, checkpoint_id)
        assert checkpoint is not None
        assert isinstance(checkpoint.state_blob, dict)
        checkpoint.state_blob = {
            **checkpoint.state_blob,
            "input_by_port": None,
        }
        session.commit()

    approved = client.post(
        f"{PREFIX}/workflow-runs/{run_id}/approval/approve",
        json={"reason": "looks good"},
    )
    assert approved.status_code == 409, approved.text
    assert (
        approved.json()["detail"]
        == "workflow run approval checkpoint is missing its input snapshot"
    )


def test_runtime_approval_approve_rejects_invalid_checkpoint_kind(client: TestClient) -> None:
    _enable_runtime_approvals(client)
    _wid, vid = create_and_publish(client)
    created = client.post(
        f"{PREFIX}/workflow-runs",
        json={"workflow_version_id": vid, "input": "hello"},
    )
    run_id = created.json()["data"]["workflow_run_id"]
    _seed_waiting_approval(client, run_id, approval_status="pending")

    with client.app.state.session_factory() as session:
        run = session.get(CaliberWorkflowRun, run_id)
        assert run is not None
        checkpoint_id = dict(run.summary or {}).get("resume_checkpoint_id")
        checkpoint = session.get(CaliberWorkflowRunCheckpoint, checkpoint_id)
        assert checkpoint is not None
        assert isinstance(checkpoint.state_blob, dict)
        checkpoint.state_blob = {
            **checkpoint.state_blob,
            "kind": "wait_for_event",
            "input_by_port": {"input": "hello"},
        }
        session.commit()

    approved = client.post(
        f"{PREFIX}/workflow-runs/{run_id}/approval/approve",
        json={"reason": "looks good"},
    )
    assert approved.status_code == 409, approved.text
    assert approved.json()["detail"] == "workflow run approval checkpoint is invalid"


def test_runtime_approval_approve_rejects_missing_node_id(client: TestClient) -> None:
    _enable_runtime_approvals(client)
    _wid, vid = create_and_publish(client)
    created = client.post(
        f"{PREFIX}/workflow-runs",
        json={"workflow_version_id": vid, "input": "hello"},
    )
    run_id = created.json()["data"]["workflow_run_id"]
    _seed_waiting_approval(client, run_id, approval_status="pending")

    with client.app.state.session_factory() as session:
        run = session.get(CaliberWorkflowRun, run_id)
        assert run is not None
        checkpoint_id = dict(run.summary or {}).get("resume_checkpoint_id")
        checkpoint = session.get(CaliberWorkflowRunCheckpoint, checkpoint_id)
        assert checkpoint is not None
        assert isinstance(checkpoint.state_blob, dict)
        state_blob = dict(checkpoint.state_blob)
        state_blob.pop("node_id", None)
        checkpoint.state_blob = state_blob
        session.commit()

    approved = client.post(
        f"{PREFIX}/workflow-runs/{run_id}/approval/approve",
        json={"reason": "looks good"},
    )
    assert approved.status_code == 409, approved.text
    assert approved.json()["detail"] == "workflow run approval checkpoint is missing node_id"


def test_runtime_approval_approve_rejects_checkpoint_node_mismatch(client: TestClient) -> None:
    _enable_runtime_approvals(client)
    _wid, vid = create_and_publish(client)
    created = client.post(
        f"{PREFIX}/workflow-runs",
        json={"workflow_version_id": vid, "input": "hello"},
    )
    run_id = created.json()["data"]["workflow_run_id"]
    _seed_waiting_approval(client, run_id, approval_status="pending")

    with client.app.state.session_factory() as session:
        run = session.get(CaliberWorkflowRun, run_id)
        assert run is not None
        checkpoint_id = dict(run.summary or {}).get("resume_checkpoint_id")
        checkpoint = session.get(CaliberWorkflowRunCheckpoint, checkpoint_id)
        assert checkpoint is not None
        checkpoint.node_id = "human_gate_v2"
        session.commit()

    approved = client.post(
        f"{PREFIX}/workflow-runs/{run_id}/approval/approve",
        json={"reason": "looks good"},
    )
    assert approved.status_code == 409, approved.text
    assert (
        approved.json()["detail"]
        == "workflow run approval checkpoint does not match current waiting node"
    )


def test_runtime_approval_approve_requires_checkpointing_flag(client: TestClient) -> None:
    _enable_runtime_approvals(client)
    client.app.state.config = client.app.state.config.model_copy(
        update={"workflow_run_checkpointing_enabled": False}
    )
    _wid, vid = create_and_publish(client)
    created = client.post(
        f"{PREFIX}/workflow-runs",
        json={"workflow_version_id": vid, "input": "hello"},
    )
    run_id = created.json()["data"]["workflow_run_id"]
    approval_id = _seed_waiting_approval(client, run_id, approval_status="pending")

    approved = client.post(
        f"{PREFIX}/workflow-runs/{run_id}/approval/approve",
        json={"reason": "looks good"},
    )
    assert approved.status_code == 409, approved.text
    assert "checkpointing is disabled" in approved.json()["detail"]

    approvals_after = client.get(f"{PREFIX}/workflow-runs/{run_id}/approvals").json()["data"]
    assert approvals_after[0]["runtime_approval_id"] == approval_id
    assert approvals_after[0]["status"] == "pending"


def test_runtime_approval_approve_requires_resume_checkpoint(client: TestClient) -> None:
    _enable_runtime_approvals(client)
    _wid, vid = create_and_publish(client)
    created = client.post(
        f"{PREFIX}/workflow-runs",
        json={"workflow_version_id": vid, "input": "hello"},
    )
    run_id = created.json()["data"]["workflow_run_id"]
    approval_id = _seed_waiting_approval(client, run_id, approval_status="pending")

    with client.app.state.session_factory() as session:
        run = session.get(CaliberWorkflowRun, run_id)
        assert run is not None
        run.summary = {
            **dict(run.summary or {}),
            "resume_checkpoint_id": "WRCK-missing-approval-checkpoint",
        }
        checkpoint = session.get(
            CaliberWorkflowRunCheckpoint, dict(run.summary)["resume_checkpoint_id"]
        )
        if checkpoint is not None:
            session.delete(checkpoint)
        session.commit()

    approved = client.post(
        f"{PREFIX}/workflow-runs/{run_id}/approval/approve",
        json={"reason": "looks good"},
    )
    assert approved.status_code == 409, approved.text
    assert approved.json()["detail"] == "workflow run has no resume checkpoint"

    approvals_after = client.get(f"{PREFIX}/workflow-runs/{run_id}/approvals").json()["data"]
    assert approvals_after[0]["runtime_approval_id"] == approval_id
    assert approvals_after[0]["status"] == "pending"


def test_runtime_approval_resume_rejects_corrupt_checkpoint_payload(client: TestClient) -> None:
    _enable_runtime_approvals(client)
    _wid, vid = create_and_publish(client)
    created = client.post(
        f"{PREFIX}/workflow-runs",
        json={"workflow_version_id": vid, "input": "hello"},
    )
    run_id = created.json()["data"]["workflow_run_id"]
    _seed_waiting_approval(client, run_id, approval_status="approved")

    with client.app.state.session_factory() as session:
        run = session.get(CaliberWorkflowRun, run_id)
        assert run is not None
        checkpoint_id = dict(run.summary or {}).get("resume_checkpoint_id")
        checkpoint = session.get(CaliberWorkflowRunCheckpoint, checkpoint_id)
        assert checkpoint is not None
        checkpoint.state_blob = ["corrupt-checkpoint-payload"]
        session.commit()

    resumed = client.post(f"{PREFIX}/workflow-runs/{run_id}/resume")
    assert resumed.status_code == 409, resumed.text
    assert resumed.json()["detail"] == "workflow run approval checkpoint is corrupt"

    with client.app.state.session_factory() as session:
        run = session.get(CaliberWorkflowRun, run_id)
        assert run is not None
        assert run.status == "waiting_approval"


def test_runtime_approval_resume_rejects_missing_input_snapshot(client: TestClient) -> None:
    _enable_runtime_approvals(client)
    _wid, vid = create_and_publish(client)
    created = client.post(
        f"{PREFIX}/workflow-runs",
        json={"workflow_version_id": vid, "input": "hello"},
    )
    run_id = created.json()["data"]["workflow_run_id"]
    _seed_waiting_approval(client, run_id, approval_status="approved")

    with client.app.state.session_factory() as session:
        run = session.get(CaliberWorkflowRun, run_id)
        assert run is not None
        checkpoint_id = dict(run.summary or {}).get("resume_checkpoint_id")
        checkpoint = session.get(CaliberWorkflowRunCheckpoint, checkpoint_id)
        assert checkpoint is not None
        assert isinstance(checkpoint.state_blob, dict)
        checkpoint.state_blob = {
            "kind": checkpoint.state_blob["kind"],
            "node_id": checkpoint.state_blob["node_id"],
            "output": checkpoint.state_blob["output"],
        }
        session.commit()

    resumed = client.post(f"{PREFIX}/workflow-runs/{run_id}/resume")
    assert resumed.status_code == 409, resumed.text
    assert (
        resumed.json()["detail"] == "workflow run approval checkpoint is missing its input snapshot"
    )

    with client.app.state.session_factory() as session:
        run = session.get(CaliberWorkflowRun, run_id)
        assert run is not None
        assert run.status == "waiting_approval"


def test_runtime_approval_resume_rejects_invalid_checkpoint_kind(client: TestClient) -> None:
    _enable_runtime_approvals(client)
    _wid, vid = create_and_publish(client)
    created = client.post(
        f"{PREFIX}/workflow-runs",
        json={"workflow_version_id": vid, "input": "hello"},
    )
    run_id = created.json()["data"]["workflow_run_id"]
    _seed_waiting_approval(
        client,
        run_id,
        approval_status="approved",
        checkpoint_kind="wait_event",
        checkpoint_input_by_port={"input": "hello"},
    )

    resumed = client.post(f"{PREFIX}/workflow-runs/{run_id}/resume")
    assert resumed.status_code == 409, resumed.text
    assert resumed.json()["detail"] == "workflow run approval checkpoint is invalid"

    with client.app.state.session_factory() as session:
        run = session.get(CaliberWorkflowRun, run_id)
        assert run is not None
        assert run.status == "waiting_approval"


def test_runtime_approval_resume_rejects_missing_node_id(client: TestClient) -> None:
    _enable_runtime_approvals(client)
    _wid, vid = create_and_publish(client)
    created = client.post(
        f"{PREFIX}/workflow-runs",
        json={"workflow_version_id": vid, "input": "hello"},
    )
    run_id = created.json()["data"]["workflow_run_id"]
    _seed_waiting_approval(client, run_id, approval_status="approved")

    with client.app.state.session_factory() as session:
        run = session.get(CaliberWorkflowRun, run_id)
        assert run is not None
        checkpoint_id = dict(run.summary or {}).get("resume_checkpoint_id")
        checkpoint = session.get(CaliberWorkflowRunCheckpoint, checkpoint_id)
        assert checkpoint is not None
        assert isinstance(checkpoint.state_blob, dict)
        checkpoint.state_blob = {
            "kind": checkpoint.state_blob["kind"],
            "node_id": "",
            "output": checkpoint.state_blob["output"],
            "input_by_port": {"input": "hello"},
        }
        session.commit()

    resumed = client.post(f"{PREFIX}/workflow-runs/{run_id}/resume")
    assert resumed.status_code == 409, resumed.text
    assert resumed.json()["detail"] == "workflow run approval checkpoint is missing node_id"

    with client.app.state.session_factory() as session:
        run = session.get(CaliberWorkflowRun, run_id)
        assert run is not None
        assert run.status == "waiting_approval"


def test_runtime_approval_resume_rejects_checkpoint_node_mismatch(client: TestClient) -> None:
    _enable_runtime_approvals(client)
    _wid, vid = create_and_publish(client)
    created = client.post(
        f"{PREFIX}/workflow-runs",
        json={"workflow_version_id": vid, "input": "hello"},
    )
    run_id = created.json()["data"]["workflow_run_id"]
    _seed_waiting_approval(client, run_id, approval_status="approved")

    with client.app.state.session_factory() as session:
        run = session.get(CaliberWorkflowRun, run_id)
        assert run is not None
        checkpoint_id = dict(run.summary or {}).get("resume_checkpoint_id")
        checkpoint = session.get(CaliberWorkflowRunCheckpoint, checkpoint_id)
        assert checkpoint is not None
        checkpoint.node_id = "tool_gate"
        assert isinstance(checkpoint.state_blob, dict)
        checkpoint.state_blob = {
            "kind": checkpoint.state_blob["kind"],
            "node_id": "tool_gate",
            "output": checkpoint.state_blob["output"],
            "input_by_port": {"input": "hello"},
        }
        session.commit()

    resumed = client.post(f"{PREFIX}/workflow-runs/{run_id}/resume")
    assert resumed.status_code == 409, resumed.text
    assert (
        resumed.json()["detail"]
        == "workflow run approval checkpoint does not match current waiting node"
    )

    with client.app.state.session_factory() as session:
        run = session.get(CaliberWorkflowRun, run_id)
        assert run is not None
        assert run.status == "waiting_approval"


def test_runtime_approval_reject_marks_run_failed(client: TestClient, monkeypatch) -> None:
    _enable_runtime_approvals(client)
    _wid, vid = create_and_publish(client)
    created = client.post(
        f"{PREFIX}/workflow-runs",
        json={"workflow_version_id": vid, "input": "hello"},
    )
    run_id = created.json()["data"]["workflow_run_id"]
    approval_id = _seed_waiting_approval(client, run_id, approval_status="pending")
    published: list[dict[str, object]] = []
    monkeypatch.setattr(
        client.app.state.event_bus,
        "publish",
        lambda payload: published.append(dict(payload)),
    )

    rejected = client.post(
        f"{PREFIX}/workflow-runs/{run_id}/approval/reject",
        json={"reason": "safety policy"},
    )
    assert rejected.status_code == 200, rejected.text
    data = rejected.json()["data"]
    assert data["status"] == "failed"
    assert data["error_code"] == "approval_rejected"
    assert data["error_summary"] == "safety policy"
    approvals = client.get(f"{PREFIX}/workflow-runs/{run_id}/approvals").json()["data"]
    assert approvals[0]["status"] == "rejected"
    assert approvals[0]["decided_by"] == "@test"
    assert [event["type"] for event in published] == [
        "workflow.run.approval.rejected",
        "workflow.run.failed",
    ]
    assert published[0]["workflow_run_id"] == run_id
    assert published[0]["runtime_approval_id"] == approval_id
    assert published[0]["reason"] == "safety policy"
    assert published[1]["workflow_run_id"] == run_id
    assert published[1]["error"] == "safety policy"


def test_resume_requires_approved_decision(client: TestClient) -> None:
    _enable_runtime_approvals(client)
    _wid, vid = create_and_publish(client)
    created = client.post(
        f"{PREFIX}/workflow-runs",
        json={"workflow_version_id": vid, "input": "hello"},
    )
    run_id = created.json()["data"]["workflow_run_id"]
    _seed_waiting_approval(client, run_id, approval_status="pending")

    response = client.post(f"{PREFIX}/workflow-runs/{run_id}/resume")
    assert response.status_code == 409
    assert "pending" in response.json()["detail"]


def test_resume_requires_checkpointing_flag(client: TestClient) -> None:
    _enable_queue(client)
    client.app.state.config = client.app.state.config.model_copy(
        update={
            "workflow_run_runtime_approvals_enabled": True,
            "workflow_run_checkpointing_enabled": False,
        }
    )
    _wid, vid = create_and_publish(client)
    created = client.post(
        f"{PREFIX}/workflow-runs",
        json={"workflow_version_id": vid, "input": "hello"},
    )
    run_id = created.json()["data"]["workflow_run_id"]
    _seed_waiting_approval(client, run_id, approval_status="approved")

    response = client.post(f"{PREFIX}/workflow-runs/{run_id}/resume")
    assert response.status_code == 409
    assert "checkpointing is disabled" in response.json()["detail"]


def test_waiting_event_run_can_resume_without_approval(client: TestClient) -> None:
    _enable_runtime_approvals(client)
    _wid, vid = create_and_publish(client)
    created = client.post(
        f"{PREFIX}/workflow-runs",
        json={"workflow_version_id": vid, "input": "hello"},
    )
    run_id = created.json()["data"]["workflow_run_id"]
    _seed_waiting_event(client, run_id)

    resumed = client.post(f"{PREFIX}/workflow-runs/{run_id}/resume")
    assert resumed.status_code == 202, resumed.text
    resumed_data = resumed.json()["data"]
    assert resumed_data["status"] == "queued"
    assert resumed_data["error_code"] is None
    assert resumed_data["error_summary"] is None


def test_waiting_event_manual_resume_stores_expected_event_name_and_synthetic_payload(
    client: TestClient,
) -> None:
    _enable_runtime_approvals(client)
    _wid, vid = create_and_publish(client)
    created = client.post(
        f"{PREFIX}/workflow-runs",
        json={"workflow_version_id": vid, "input": "hello"},
    )
    run_id = created.json()["data"]["workflow_run_id"]
    _seed_waiting_event(
        client,
        run_id,
        checkpoint_kind="wait_for_event",
        expected_event_name="ticket.approved",
        input_by_port={"input": "hello"},
    )

    resumed = client.post(f"{PREFIX}/workflow-runs/{run_id}/resume")
    assert resumed.status_code == 202, resumed.text
    resumed_data = resumed.json()["data"]
    assert resumed_data["status"] == "queued"
    assert resumed_data["error_code"] is None
    assert resumed_data["error_summary"] is None

    with client.app.state.session_factory() as session:
        run = session.get(CaliberWorkflowRun, run_id)
        assert run is not None
        checkpoint_id = dict(run.summary or {}).get("resume_checkpoint_id")
        checkpoint = session.get(CaliberWorkflowRunCheckpoint, checkpoint_id)
        assert checkpoint is not None
        assert isinstance(checkpoint.state_blob, dict)
        assert checkpoint.state_blob["expected_event_name"] == "ticket.approved"
        assert checkpoint.state_blob["resume_event_inputs"] == {
            "resume_event": {
                "manual_resume": True,
                "event_name": "ticket.approved",
            },
            "event": {
                "manual_resume": True,
                "event_name": "ticket.approved",
            },
            "event_payload": {
                "manual_resume": True,
                "event_name": "ticket.approved",
            },
            "event_name": "ticket.approved",
            "ticket.approved": {
                "manual_resume": True,
                "event_name": "ticket.approved",
            },
        }


def test_waiting_event_resume_stores_event_payload_for_checkpoint_replay(
    client: TestClient,
) -> None:
    _enable_runtime_approvals(client)
    _wid, vid = create_and_publish(client)
    created = client.post(
        f"{PREFIX}/workflow-runs",
        json={"workflow_version_id": vid, "input": "hello"},
    )
    run_id = created.json()["data"]["workflow_run_id"]
    _seed_waiting_event(
        client,
        run_id,
        checkpoint_kind="wait_for_event",
        expected_event_name="ticket.approved",
        input_by_port={"input": "hello"},
    )

    resumed = client.post(
        f"{PREFIX}/workflow-runs/{run_id}/resume",
        json={
            "event_name": "ticket.approved",
            "event_payload": {"ticket_id": "T-42", "approved": True},
        },
    )
    assert resumed.status_code == 202, resumed.text

    with client.app.state.session_factory() as session:
        run = session.get(CaliberWorkflowRun, run_id)
        assert run is not None
        checkpoint_id = dict(run.summary or {}).get("resume_checkpoint_id")
        checkpoint = session.get(CaliberWorkflowRunCheckpoint, checkpoint_id)
        assert checkpoint is not None
        assert isinstance(checkpoint.state_blob, dict)
        assert checkpoint.state_blob["resume_event_inputs"] == {
            "resume_event": {"ticket_id": "T-42", "approved": True},
            "event": {"ticket_id": "T-42", "approved": True},
            "event_payload": {"ticket_id": "T-42", "approved": True},
            "event_name": "ticket.approved",
            "ticket.approved": {"ticket_id": "T-42", "approved": True},
        }


def test_waiting_event_resume_by_correlated_event_matches_single_run(client: TestClient) -> None:
    _enable_runtime_approvals(client)
    _wid, vid = create_and_publish(client)
    created = client.post(
        f"{PREFIX}/workflow-runs",
        json={"workflow_version_id": vid, "input": "hello"},
    )
    run_id = created.json()["data"]["workflow_run_id"]
    original_queued_at = created.json()["data"]["queued_at"]
    _seed_waiting_event(
        client,
        run_id,
        checkpoint_kind="wait_for_event",
        expected_event_name="ticket.approved",
        input_by_port={"input": '{"ticket_id":"T-42","approved":false}'},
        correlation_key="ticket_id",
        correlation_value="T-42",
    )

    resumed = client.post(
        f"{PREFIX}/workflow-runs/resume-by-event",
        json={
            "event_name": "ticket.approved",
            "event_payload": {"ticket_id": "T-42", "approved": True},
        },
    )
    assert resumed.status_code == 202, resumed.text
    data = resumed.json()["data"]
    assert data["workflow_run_id"] == run_id
    assert data["status"] == "queued"
    assert data["queued_at"] == original_queued_at.removesuffix("Z")

    with client.app.state.session_factory() as session:
        run = session.get(CaliberWorkflowRun, run_id)
        assert run is not None
        checkpoint_id = dict(run.summary or {}).get("resume_checkpoint_id")
        checkpoint = session.get(CaliberWorkflowRunCheckpoint, checkpoint_id)
        assert checkpoint is not None
        assert isinstance(checkpoint.state_blob, dict)
        assert checkpoint.state_blob["resume_event_inputs"] == {
            "resume_event": {"ticket_id": "T-42", "approved": True},
            "event": {"ticket_id": "T-42", "approved": True},
            "event_payload": {"ticket_id": "T-42", "approved": True},
            "event_name": "ticket.approved",
            "ticket.approved": {"ticket_id": "T-42", "approved": True},
        }


def test_waiting_event_resume_by_event_rejects_ambiguous_matches(client: TestClient) -> None:
    _enable_runtime_approvals(client)
    _wid, vid = create_and_publish(client)
    created_one = client.post(
        f"{PREFIX}/workflow-runs",
        json={"workflow_version_id": vid, "input": "hello"},
    )
    created_two = client.post(
        f"{PREFIX}/workflow-runs",
        json={"workflow_version_id": vid, "input": "hello again"},
    )
    run_one = created_one.json()["data"]["workflow_run_id"]
    run_two = created_two.json()["data"]["workflow_run_id"]
    _seed_waiting_event(
        client,
        run_one,
        checkpoint_kind="wait_for_event",
        expected_event_name="ticket.approved",
    )
    _seed_waiting_event(
        client,
        run_two,
        checkpoint_kind="wait_for_event",
        expected_event_name="ticket.approved",
    )

    resumed = client.post(
        f"{PREFIX}/workflow-runs/resume-by-event",
        json={"event_name": "ticket.approved", "event_payload": {"approved": True}},
    )
    assert resumed.status_code == 409, resumed.text
    detail = resumed.json()["detail"]
    assert run_one in detail
    assert run_two in detail
    assert "correlation_key" in detail


def test_waiting_event_resume_by_event_rejects_corrupt_checkpoint_payload(
    client: TestClient,
) -> None:
    _enable_runtime_approvals(client)
    _wid, vid = create_and_publish(client)
    created = client.post(
        f"{PREFIX}/workflow-runs",
        json={"workflow_version_id": vid, "input": "hello"},
    )
    run_id = created.json()["data"]["workflow_run_id"]
    _seed_waiting_event(
        client,
        run_id,
        checkpoint_kind="wait_for_event",
        expected_event_name="ticket.approved",
    )

    with client.app.state.session_factory() as session:
        run = session.get(CaliberWorkflowRun, run_id)
        assert run is not None
        checkpoint_id = dict(run.summary or {}).get("resume_checkpoint_id")
        checkpoint = session.get(CaliberWorkflowRunCheckpoint, checkpoint_id)
        assert checkpoint is not None
        checkpoint.state_blob = ["corrupt-checkpoint-payload"]
        session.commit()

    resumed = client.post(
        f"{PREFIX}/workflow-runs/resume-by-event",
        json={"event_name": "ticket.approved", "event_payload": {"approved": True}},
    )
    assert resumed.status_code == 409, resumed.text
    detail = resumed.json()["detail"]
    assert "corrupt resume checkpoints" in detail
    assert run_id in detail


def test_waiting_event_resume_by_event_rejects_invalid_checkpoint_kind(client: TestClient) -> None:
    _enable_runtime_approvals(client)
    _wid, vid = create_and_publish(client)
    created = client.post(
        f"{PREFIX}/workflow-runs",
        json={"workflow_version_id": vid, "input": "hello"},
    )
    run_id = created.json()["data"]["workflow_run_id"]
    _seed_waiting_event(
        client,
        run_id,
        checkpoint_kind="wait_for_event",
        expected_event_name="ticket.approved",
    )

    with client.app.state.session_factory() as session:
        run = session.get(CaliberWorkflowRun, run_id)
        assert run is not None
        checkpoint_id = dict(run.summary or {}).get("resume_checkpoint_id")
        checkpoint = session.get(CaliberWorkflowRunCheckpoint, checkpoint_id)
        assert checkpoint is not None
        assert isinstance(checkpoint.state_blob, dict)
        checkpoint.state_blob = {
            "kind": "runtime_approval",
            "node_id": checkpoint.state_blob["node_id"],
            "output": checkpoint.state_blob["output"],
            "input_by_port": {"input": "hello"},
        }
        session.commit()

    resumed = client.post(
        f"{PREFIX}/workflow-runs/resume-by-event",
        json={"event_name": "ticket.approved", "event_payload": {"approved": True}},
    )
    assert resumed.status_code == 409, resumed.text
    detail = resumed.json()["detail"]
    assert "corrupt resume checkpoints" in detail
    assert run_id in detail


def test_waiting_event_resume_by_event_rejects_missing_node_id(client: TestClient) -> None:
    _enable_runtime_approvals(client)
    _wid, vid = create_and_publish(client)
    created = client.post(
        f"{PREFIX}/workflow-runs",
        json={"workflow_version_id": vid, "input": "hello"},
    )
    run_id = created.json()["data"]["workflow_run_id"]
    _seed_waiting_event(
        client,
        run_id,
        checkpoint_kind="wait_for_event",
        expected_event_name="ticket.approved",
    )

    with client.app.state.session_factory() as session:
        run = session.get(CaliberWorkflowRun, run_id)
        assert run is not None
        checkpoint_id = dict(run.summary or {}).get("resume_checkpoint_id")
        checkpoint = session.get(CaliberWorkflowRunCheckpoint, checkpoint_id)
        assert checkpoint is not None
        assert isinstance(checkpoint.state_blob, dict)
        checkpoint.state_blob = {
            "kind": "wait_for_event",
            "node_id": "",
            "output": checkpoint.state_blob["output"],
            "input_by_port": {"input": "hello"},
            "expected_event_name": "ticket.approved",
        }
        session.commit()

    resumed = client.post(
        f"{PREFIX}/workflow-runs/resume-by-event",
        json={"event_name": "ticket.approved", "event_payload": {"approved": True}},
    )
    assert resumed.status_code == 409, resumed.text
    detail = resumed.json()["detail"]
    assert "corrupt resume checkpoints" in detail
    assert run_id in detail


def test_waiting_event_resume_by_event_rejects_missing_input_snapshot(client: TestClient) -> None:
    _enable_runtime_approvals(client)
    _wid, vid = create_and_publish(client)
    created = client.post(
        f"{PREFIX}/workflow-runs",
        json={"workflow_version_id": vid, "input": "hello"},
    )
    run_id = created.json()["data"]["workflow_run_id"]
    _seed_waiting_event(
        client,
        run_id,
        checkpoint_kind="wait_for_event",
        expected_event_name="ticket.approved",
    )

    with client.app.state.session_factory() as session:
        run = session.get(CaliberWorkflowRun, run_id)
        assert run is not None
        checkpoint_id = dict(run.summary or {}).get("resume_checkpoint_id")
        checkpoint = session.get(CaliberWorkflowRunCheckpoint, checkpoint_id)
        assert checkpoint is not None
        assert isinstance(checkpoint.state_blob, dict)
        checkpoint.state_blob = {
            "kind": checkpoint.state_blob["kind"],
            "node_id": checkpoint.state_blob["node_id"],
            "output": checkpoint.state_blob["output"],
            "expected_event_name": checkpoint.state_blob["expected_event_name"],
        }
        session.commit()

    resumed = client.post(
        f"{PREFIX}/workflow-runs/resume-by-event",
        json={"event_name": "ticket.approved", "event_payload": {"approved": True}},
    )
    assert resumed.status_code == 409, resumed.text
    detail = resumed.json()["detail"]
    assert "corrupt resume checkpoints" in detail
    assert run_id in detail


def test_waiting_event_resume_by_event_rejects_missing_expected_event_name(
    client: TestClient,
) -> None:
    _enable_runtime_approvals(client)
    _wid, vid = create_and_publish(client)
    created = client.post(
        f"{PREFIX}/workflow-runs",
        json={"workflow_version_id": vid, "input": "hello"},
    )
    run_id = created.json()["data"]["workflow_run_id"]
    _seed_waiting_event(
        client,
        run_id,
        checkpoint_kind="wait_for_event",
        expected_event_name="ticket.approved",
    )

    with client.app.state.session_factory() as session:
        run = session.get(CaliberWorkflowRun, run_id)
        assert run is not None
        checkpoint_id = dict(run.summary or {}).get("resume_checkpoint_id")
        checkpoint = session.get(CaliberWorkflowRunCheckpoint, checkpoint_id)
        assert checkpoint is not None
        assert isinstance(checkpoint.state_blob, dict)
        checkpoint.state_blob = {
            "kind": checkpoint.state_blob["kind"],
            "node_id": checkpoint.state_blob["node_id"],
            "output": checkpoint.state_blob["output"],
            "input_by_port": {"input": "hello"},
        }
        session.commit()

    resumed = client.post(
        f"{PREFIX}/workflow-runs/resume-by-event",
        json={"event_name": "ticket.approved", "event_payload": {"approved": True}},
    )
    assert resumed.status_code == 409, resumed.text
    detail = resumed.json()["detail"]
    assert "corrupt resume checkpoints" in detail
    assert run_id in detail


def test_waiting_event_resume_by_event_rejects_missing_correlation_value(
    client: TestClient,
) -> None:
    _enable_runtime_approvals(client)
    _wid, vid = create_and_publish(client)
    created = client.post(
        f"{PREFIX}/workflow-runs",
        json={"workflow_version_id": vid, "input": "hello"},
    )
    run_id = created.json()["data"]["workflow_run_id"]
    _seed_waiting_event(
        client,
        run_id,
        checkpoint_kind="wait_for_event",
        expected_event_name="ticket.approved",
        correlation_key="ticket_id",
        correlation_value=None,
    )

    resumed = client.post(
        f"{PREFIX}/workflow-runs/resume-by-event",
        json={"event_name": "ticket.approved", "event_payload": {"ticket_id": "T-42"}},
    )
    assert resumed.status_code == 409, resumed.text
    detail = resumed.json()["detail"]
    assert "missing correlation_value" in detail
    assert "correlation_key" in detail
    assert run_id in detail


def test_waiting_event_resume_by_event_rejects_legacy_wait_event_checkpoint(
    client: TestClient,
) -> None:
    _enable_runtime_approvals(client)
    _wid, vid = create_and_publish(client)
    created = client.post(
        f"{PREFIX}/workflow-runs",
        json={"workflow_version_id": vid, "input": "hello"},
    )
    run_id = created.json()["data"]["workflow_run_id"]
    _seed_waiting_event(
        client,
        run_id,
        checkpoint_kind="wait_event",
        expected_event_name="ticket.approved",
    )

    resumed = client.post(
        f"{PREFIX}/workflow-runs/resume-by-event",
        json={"event_name": "ticket.approved", "event_payload": {"approved": True}},
    )
    assert resumed.status_code == 409, resumed.text
    detail = resumed.json()["detail"]
    assert "legacy wait_event checkpoints" in detail
    assert "workflow-wide event matching" in detail
    assert run_id in detail


def test_waiting_event_resume_by_event_rejects_checkpoint_state_node_mismatch(
    client: TestClient,
) -> None:
    _enable_runtime_approvals(client)
    _wid, vid = create_and_publish(client)
    created = client.post(
        f"{PREFIX}/workflow-runs",
        json={"workflow_version_id": vid, "input": "hello"},
    )
    run_id = created.json()["data"]["workflow_run_id"]
    _seed_waiting_event(
        client,
        run_id,
        checkpoint_kind="wait_for_event",
        expected_event_name="ticket.approved",
    )

    with client.app.state.session_factory() as session:
        run = session.get(CaliberWorkflowRun, run_id)
        assert run is not None
        checkpoint_id = dict(run.summary or {}).get("resume_checkpoint_id")
        checkpoint = session.get(CaliberWorkflowRunCheckpoint, checkpoint_id)
        assert checkpoint is not None
        assert isinstance(checkpoint.state_blob, dict)
        checkpoint.state_blob = {
            "kind": "wait_for_event",
            "node_id": "other_gate",
            "output": checkpoint.state_blob["output"],
            "input_by_port": {"input": "hello"},
            "expected_event_name": "ticket.approved",
        }
        session.commit()

    resumed = client.post(
        f"{PREFIX}/workflow-runs/resume-by-event",
        json={"event_name": "ticket.approved", "event_payload": {"approved": True}},
    )
    assert resumed.status_code == 409, resumed.text
    detail = resumed.json()["detail"]
    assert "corrupt resume checkpoints" in detail
    assert run_id in detail


def test_waiting_event_resume_requires_checkpoint(client: TestClient) -> None:
    _enable_runtime_approvals(client)
    _wid, vid = create_and_publish(client)
    created = client.post(
        f"{PREFIX}/workflow-runs",
        json={"workflow_version_id": vid, "input": "hello"},
    )
    run_id = created.json()["data"]["workflow_run_id"]
    _seed_waiting_event(client, run_id, with_checkpoint=False)

    response = client.post(f"{PREFIX}/workflow-runs/{run_id}/resume")
    assert response.status_code == 409
    assert "resume checkpoint" in response.json()["detail"]


def test_waiting_event_resume_rejects_corrupt_checkpoint_payload(client: TestClient) -> None:
    _enable_runtime_approvals(client)
    _wid, vid = create_and_publish(client)
    created = client.post(
        f"{PREFIX}/workflow-runs",
        json={"workflow_version_id": vid, "input": "hello"},
    )
    run_id = created.json()["data"]["workflow_run_id"]
    _seed_waiting_event(
        client,
        run_id,
        checkpoint_kind="wait_for_event",
        expected_event_name="ticket.approved",
    )

    with client.app.state.session_factory() as session:
        run = session.get(CaliberWorkflowRun, run_id)
        assert run is not None
        checkpoint_id = dict(run.summary or {}).get("resume_checkpoint_id")
        checkpoint = session.get(CaliberWorkflowRunCheckpoint, checkpoint_id)
        assert checkpoint is not None
        checkpoint.state_blob = ["corrupt-checkpoint-payload"]
        session.commit()

    response = client.post(
        f"{PREFIX}/workflow-runs/{run_id}/resume",
        json={"event_name": "ticket.approved"},
    )
    assert response.status_code == 409
    assert response.json()["detail"] == "workflow run resume checkpoint is corrupt"

    with client.app.state.session_factory() as session:
        run = session.get(CaliberWorkflowRun, run_id)
        assert run is not None
        assert run.status == "waiting_event"


def test_waiting_event_resume_rejects_invalid_checkpoint_kind(client: TestClient) -> None:
    _enable_runtime_approvals(client)
    _wid, vid = create_and_publish(client)
    created = client.post(
        f"{PREFIX}/workflow-runs",
        json={"workflow_version_id": vid, "input": "hello"},
    )
    run_id = created.json()["data"]["workflow_run_id"]
    _seed_waiting_event(
        client,
        run_id,
        checkpoint_kind="wait_for_event",
        expected_event_name="ticket.approved",
    )

    with client.app.state.session_factory() as session:
        run = session.get(CaliberWorkflowRun, run_id)
        assert run is not None
        checkpoint_id = dict(run.summary or {}).get("resume_checkpoint_id")
        checkpoint = session.get(CaliberWorkflowRunCheckpoint, checkpoint_id)
        assert checkpoint is not None
        assert isinstance(checkpoint.state_blob, dict)
        checkpoint.state_blob = {
            "kind": "runtime_approval",
            "node_id": checkpoint.state_blob["node_id"],
            "output": checkpoint.state_blob["output"],
            "input_by_port": {"input": "hello"},
        }
        session.commit()

    response = client.post(
        f"{PREFIX}/workflow-runs/{run_id}/resume",
        json={"event_name": "ticket.approved"},
    )
    assert response.status_code == 409
    assert response.json()["detail"] == "workflow run resume checkpoint is invalid"

    with client.app.state.session_factory() as session:
        run = session.get(CaliberWorkflowRun, run_id)
        assert run is not None
        assert run.status == "waiting_event"


def test_waiting_event_resume_rejects_missing_node_id(client: TestClient) -> None:
    _enable_runtime_approvals(client)
    _wid, vid = create_and_publish(client)
    created = client.post(
        f"{PREFIX}/workflow-runs",
        json={"workflow_version_id": vid, "input": "hello"},
    )
    run_id = created.json()["data"]["workflow_run_id"]
    _seed_waiting_event(
        client,
        run_id,
        checkpoint_kind="wait_for_event",
        expected_event_name="ticket.approved",
    )

    with client.app.state.session_factory() as session:
        run = session.get(CaliberWorkflowRun, run_id)
        assert run is not None
        checkpoint_id = dict(run.summary or {}).get("resume_checkpoint_id")
        checkpoint = session.get(CaliberWorkflowRunCheckpoint, checkpoint_id)
        assert checkpoint is not None
        assert isinstance(checkpoint.state_blob, dict)
        checkpoint.state_blob = {
            "kind": "wait_for_event",
            "node_id": "",
            "output": checkpoint.state_blob["output"],
            "input_by_port": {"input": "hello"},
            "expected_event_name": "ticket.approved",
        }
        session.commit()

    response = client.post(
        f"{PREFIX}/workflow-runs/{run_id}/resume",
        json={"event_name": "ticket.approved"},
    )
    assert response.status_code == 409
    assert response.json()["detail"] == "workflow run resume checkpoint is missing node_id"

    with client.app.state.session_factory() as session:
        run = session.get(CaliberWorkflowRun, run_id)
        assert run is not None
        assert run.status == "waiting_event"


def test_waiting_event_resume_rejects_missing_input_snapshot(client: TestClient) -> None:
    _enable_runtime_approvals(client)
    _wid, vid = create_and_publish(client)
    created = client.post(
        f"{PREFIX}/workflow-runs",
        json={"workflow_version_id": vid, "input": "hello"},
    )
    run_id = created.json()["data"]["workflow_run_id"]
    _seed_waiting_event(
        client,
        run_id,
        checkpoint_kind="wait_for_event",
        expected_event_name="ticket.approved",
    )

    with client.app.state.session_factory() as session:
        run = session.get(CaliberWorkflowRun, run_id)
        assert run is not None
        checkpoint_id = dict(run.summary or {}).get("resume_checkpoint_id")
        checkpoint = session.get(CaliberWorkflowRunCheckpoint, checkpoint_id)
        assert checkpoint is not None
        assert isinstance(checkpoint.state_blob, dict)
        checkpoint.state_blob = {
            "kind": checkpoint.state_blob["kind"],
            "node_id": checkpoint.state_blob["node_id"],
            "output": checkpoint.state_blob["output"],
            "expected_event_name": checkpoint.state_blob["expected_event_name"],
        }
        session.commit()

    response = client.post(
        f"{PREFIX}/workflow-runs/{run_id}/resume",
        json={"event_name": "ticket.approved"},
    )
    assert response.status_code == 409
    assert (
        response.json()["detail"] == "workflow run resume checkpoint is missing its input snapshot"
    )

    with client.app.state.session_factory() as session:
        run = session.get(CaliberWorkflowRun, run_id)
        assert run is not None
        assert run.status == "waiting_event"


def test_waiting_event_resume_rejects_checkpoint_node_mismatch(client: TestClient) -> None:
    _enable_runtime_approvals(client)
    _wid, vid = create_and_publish(client)
    created = client.post(
        f"{PREFIX}/workflow-runs",
        json={"workflow_version_id": vid, "input": "hello"},
    )
    run_id = created.json()["data"]["workflow_run_id"]
    _seed_waiting_event(
        client,
        run_id,
        checkpoint_kind="wait_for_event",
        expected_event_name="ticket.approved",
    )

    with client.app.state.session_factory() as session:
        run = session.get(CaliberWorkflowRun, run_id)
        assert run is not None
        checkpoint_id = dict(run.summary or {}).get("resume_checkpoint_id")
        checkpoint = session.get(CaliberWorkflowRunCheckpoint, checkpoint_id)
        assert checkpoint is not None
        checkpoint.node_id = "other_gate"
        assert isinstance(checkpoint.state_blob, dict)
        checkpoint.state_blob = {
            "kind": "wait_for_event",
            "node_id": "other_gate",
            "output": checkpoint.state_blob["output"],
            "input_by_port": {"input": "hello"},
            "expected_event_name": "ticket.approved",
        }
        session.commit()

    response = client.post(
        f"{PREFIX}/workflow-runs/{run_id}/resume",
        json={"event_name": "ticket.approved"},
    )
    assert response.status_code == 409
    assert (
        response.json()["detail"]
        == "workflow run resume checkpoint does not match current waiting node"
    )

    with client.app.state.session_factory() as session:
        run = session.get(CaliberWorkflowRun, run_id)
        assert run is not None
        assert run.status == "waiting_event"


def test_waiting_event_resume_rejects_missing_expected_event_name(
    client: TestClient,
) -> None:
    _enable_runtime_approvals(client)
    _wid, vid = create_and_publish(client)
    created = client.post(
        f"{PREFIX}/workflow-runs",
        json={"workflow_version_id": vid, "input": "hello"},
    )
    run_id = created.json()["data"]["workflow_run_id"]
    _seed_waiting_event(
        client,
        run_id,
        checkpoint_kind="wait_for_event",
        expected_event_name="ticket.approved",
    )

    with client.app.state.session_factory() as session:
        run = session.get(CaliberWorkflowRun, run_id)
        assert run is not None
        checkpoint_id = dict(run.summary or {}).get("resume_checkpoint_id")
        checkpoint = session.get(CaliberWorkflowRunCheckpoint, checkpoint_id)
        assert checkpoint is not None
        assert isinstance(checkpoint.state_blob, dict)
        checkpoint.state_blob = {
            "kind": "wait_for_event",
            "node_id": checkpoint.state_blob["node_id"],
            "output": checkpoint.state_blob["output"],
            "input_by_port": {"input": "hello"},
        }
        session.commit()

    response = client.post(
        f"{PREFIX}/workflow-runs/{run_id}/resume",
        json={"event_name": "ticket.approved"},
    )
    assert response.status_code == 409
    assert (
        response.json()["detail"]
        == "workflow run resume checkpoint is missing its expected event name"
    )

    with client.app.state.session_factory() as session:
        run = session.get(CaliberWorkflowRun, run_id)
        assert run is not None
        assert run.status == "waiting_event"


def test_wait_until_run_can_resume_and_stores_manual_resume_payload(client: TestClient) -> None:
    _enable_runtime_approvals(client)
    _wid, vid = create_and_publish(client)
    created = client.post(
        f"{PREFIX}/workflow-runs",
        json={"workflow_version_id": vid, "input": "hello"},
    )
    run_id = created.json()["data"]["workflow_run_id"]
    _seed_waiting_event(
        client,
        run_id,
        checkpoint_kind="wait_until",
        input_by_port={"input": "hello"},
        resume_at="2026-06-16T12:00:00Z",
        wait_until="2026-06-16 05:00:00",
        timezone_name="America/Los_Angeles",
    )

    resumed = client.post(f"{PREFIX}/workflow-runs/{run_id}/resume")
    assert resumed.status_code == 202, resumed.text
    resumed_data = resumed.json()["data"]
    assert resumed_data["status"] == "queued"
    assert resumed_data["error_code"] is None
    assert resumed_data["error_summary"] is None

    with client.app.state.session_factory() as session:
        run = session.get(CaliberWorkflowRun, run_id)
        assert run is not None
        checkpoint_id = dict(run.summary or {}).get("resume_checkpoint_id")
        checkpoint = session.get(CaliberWorkflowRunCheckpoint, checkpoint_id)
        assert checkpoint is not None
        assert isinstance(checkpoint.state_blob, dict)
        assert checkpoint.state_blob["resume_at"] == "2026-06-16T12:00:00Z"
        assert checkpoint.state_blob["wait_until"] == "2026-06-16 05:00:00"
        assert checkpoint.state_blob["timezone"] == "America/Los_Angeles"
        assert checkpoint.state_blob["resume_event_inputs"] == {
            "resume_event": {"manual_resume": True},
            "event": {"manual_resume": True},
            "event_payload": {"manual_resume": True},
        }
