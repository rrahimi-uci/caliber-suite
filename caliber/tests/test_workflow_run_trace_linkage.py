"""Workflow runs must persist the MLflow *trace* id, not just the run id.

From the repository review (``ui-complete-report.md`` §3, "Trace-link
correctness defect"): :class:`WorkflowRunResult` carries both
``mlflow_run_id`` and ``mlflow_trace_id`` and the runtime populates both, but
neither the queued worker nor the synchronous run route ever assigned
``CaliberWorkflowRun.trace_id``. Two shipped surfaces read that column:

* ``GET /workflow-runs/{id}/trace`` — the in-app span viewer, which
  short-circuited to an empty tree for every run; and
* ``GET /workflow-runs/by-trace/{trace_id}`` — trace → run localization,
  which could never resolve a workflow run.

These tests pin the persistence for both execution paths and assert the two
dependent endpoints actually resolve.
"""

from __future__ import annotations

import pytest
from starlette.testclient import TestClient

from caliber.db.models import CaliberWorkflowRun
from caliber.orchestrator.workflow_run_worker import WorkflowRunWorker
from caliber.workflows.runtime import NodeStep, WorkflowRunResult
from tests.workflow_helpers import PREFIX, create_and_publish, make_manifest

TRACE_ID = "tr-abc123"
MLFLOW_RUN_ID = "MR-abc123"


def _traced_result() -> WorkflowRunResult:
    return WorkflowRunResult(
        status="completed",
        output="done",
        steps=[NodeStep("agent", "agent", "ok", output="done")],
        mlflow_run_id=MLFLOW_RUN_ID,
        mlflow_trace_id=TRACE_ID,
    )


def _enable_queue(client: TestClient) -> None:
    """The async run queue is opt-in; the queued-path tests need it on."""
    client.app.state.config = client.app.state.config.model_copy(
        update={"workflow_run_queue_enabled": True}
    )


def _build_worker(client: TestClient) -> WorkflowRunWorker:
    return WorkflowRunWorker(
        session_factory=client.app.state.session_factory,
        config=client.app.state.config,
        event_bus=getattr(client.app.state, "event_bus", None),
    )


def test_queued_worker_persists_the_trace_id(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    _enable_queue(client)
    workflow_id = "trace-linkage-wf"
    _wid, version_id = create_and_publish(
        client,
        workflow_name="Trace Linkage",
        manifest=make_manifest(workflow_id),
    )
    created = client.post(
        f"{PREFIX}/workflow-runs",
        json={"workflow_version_id": version_id, "input": "hello"},
    )
    assert created.status_code == 202, created.text
    run_id = created.json()["data"]["workflow_run_id"]

    monkeypatch.setattr(
        "caliber.orchestrator.workflow_run_worker.execute",
        lambda *args, **kwargs: _traced_result(),
    )
    _build_worker(client)._tick()

    with client.app.state.session_factory() as session:
        run = session.get(CaliberWorkflowRun, run_id)
        assert run is not None
        assert run.status == "completed"
        assert run.mlflow_run_id == MLFLOW_RUN_ID
        # The defect: this was None, so the trace panel rendered empty.
        assert run.trace_id == TRACE_ID


def test_trace_endpoint_resolves_after_a_queued_run(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``/workflow-runs/{id}/trace`` reports the id instead of a null tree."""
    _enable_queue(client)
    workflow_id = "trace-endpoint-wf"
    _wid, version_id = create_and_publish(
        client,
        workflow_name="Trace Endpoint",
        manifest=make_manifest(workflow_id),
    )
    run_id = client.post(
        f"{PREFIX}/workflow-runs",
        json={"workflow_version_id": version_id, "input": "hello"},
    ).json()["data"]["workflow_run_id"]

    monkeypatch.setattr(
        "caliber.orchestrator.workflow_run_worker.execute",
        lambda *args, **kwargs: _traced_result(),
    )
    _build_worker(client)._tick()

    resp = client.get(f"{PREFIX}/workflow-runs/{run_id}/trace")

    assert resp.status_code == 200, resp.text
    # MLflow is absent under test so the span list is empty, but the run is now
    # *linked* — previously trace_id was None and the route short-circuited
    # without ever attempting a lookup.
    assert resp.json()["data"]["trace_id"] == TRACE_ID


def test_by_trace_lookup_finds_the_queued_run(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``/workflow-runs/by-trace/{trace_id}`` was unresolvable for every run."""
    _enable_queue(client)
    workflow_id = "by-trace-wf"
    _wid, version_id = create_and_publish(
        client,
        workflow_name="By Trace",
        manifest=make_manifest(workflow_id),
    )
    run_id = client.post(
        f"{PREFIX}/workflow-runs",
        json={"workflow_version_id": version_id, "input": "hello"},
    ).json()["data"]["workflow_run_id"]

    monkeypatch.setattr(
        "caliber.orchestrator.workflow_run_worker.execute",
        lambda *args, **kwargs: _traced_result(),
    )
    _build_worker(client)._tick()

    resp = client.get(f"{PREFIX}/workflow-runs/by-trace/{TRACE_ID}")

    assert resp.status_code == 200, resp.text
    assert resp.json()["data"]["workflow_run_id"] == run_id


def test_synchronous_run_route_persists_the_trace_id(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The non-queued path had the same gap and must stay in step."""
    workflow_id = "sync-trace-wf"
    _wid, version_id = create_and_publish(
        client,
        workflow_name="Sync Trace",
        manifest=make_manifest(workflow_id),
    )

    monkeypatch.setattr(
        "caliber.routes.workflow_versions.execute",
        lambda *args, **kwargs: _traced_result(),
    )
    resp = client.post(f"{PREFIX}/workflow-versions/{version_id}/run", json={"input": "hello"})
    assert resp.status_code in (200, 201), resp.text

    with client.app.state.session_factory() as session:
        run = (
            session.query(CaliberWorkflowRun)
            .filter(CaliberWorkflowRun.workflow_version_id == version_id)
            .one()
        )
        assert run.mlflow_run_id == MLFLOW_RUN_ID
        assert run.trace_id == TRACE_ID
