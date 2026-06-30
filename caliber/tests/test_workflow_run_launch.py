"""Tests for the shared workflow-run enqueue path."""

from __future__ import annotations

import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from caliber.db.models import (
    CaliberProject,
    CaliberWorkflow,
    CaliberWorkflowRun,
    CaliberWorkflowRunEvent,
    CaliberWorkflowVersion,
)
from caliber.workflows import run_launch
from caliber.workflows.run_launch import _append_run_event, enqueue_workflow_run
from caliber.workflows.run_state import RUN_STATUS_QUEUED


def _seed_workflow(
    session: Session,
    *,
    workflow_id: str = "WF-RUN-LAUNCH",
    version_id: str = "WFV-RUN-LAUNCH",
    project_id: str | None = "P-RUN-LAUNCH",
) -> tuple[CaliberWorkflow, CaliberWorkflowVersion]:
    if project_id is not None:
        session.add(
            CaliberProject(
                project_id=project_id,
                tenant_id="tenant-run-launch",
                name=f"Project {project_id}",
                owner="@test",
            )
        )
    workflow = CaliberWorkflow(
        workflow_id=workflow_id,
        project_id=project_id,
        name="Run Launch Workflow",
        owner="@test",
        status="active",
    )
    version = CaliberWorkflowVersion(
        version_id=version_id,
        workflow_id=workflow.workflow_id,
        version_number=1,
        status="published",
        manifest={"nodes": [], "edges": []},
        manifest_hash="hash-run-launch",
        created_by="@test",
    )
    session.add_all([workflow, version])
    session.flush()
    return workflow, version


def test_enqueue_workflow_run_creates_queued_run_event_and_publish_payload(
    db_session: Session,
) -> None:
    workflow, version = _seed_workflow(db_session)
    published: list[dict[str, object]] = []

    run, created = enqueue_workflow_run(
        db_session,
        workflow=workflow,
        version=version,
        alias="prod",
        source="webhook",
        actor="@webhook",
        input_text="x" * 1205,
        idempotency_key="event-123",
        priority=7,
        session_id="ASSISTANT-1",
        publish=published.append,
    )

    assert created is True
    assert run.workflow_run_id.startswith("WR-")
    assert run.workflow_id == workflow.workflow_id
    assert run.workflow_version_id == version.version_id
    assert run.project_id == workflow.project_id
    assert run.tenant_id == "tenant-run-launch"
    assert run.deployment_alias == "prod"
    assert run.session_id == "ASSISTANT-1"
    assert run.status == RUN_STATUS_QUEUED
    assert run.source == "webhook"
    assert run.priority == 7
    assert run.attempt_number == 1
    assert run.idempotency_key == "event-123"
    assert run.started_at is None
    assert run.input_payload == "x" * 1205
    assert run.manifest_snapshot == version.manifest
    assert run.summary == {
        "preview": False,
        "status": RUN_STATUS_QUEUED,
        "input": "x" * 1000,
        "manifest_mode": "saved_version",
        "manifest_hash": version.manifest_hash,
        "workflow_version_number": version.version_number,
    }
    assert published == [
        {
            "type": "workflow.run.queued",
            "workflow_id": workflow.workflow_id,
            "workflow_version_id": version.version_id,
            "workflow_run_id": run.workflow_run_id,
            "status": RUN_STATUS_QUEUED,
            "alias": "prod",
        }
    ]

    event = db_session.execute(select(CaliberWorkflowRunEvent)).scalar_one()
    assert event.workflow_run_id == run.workflow_run_id
    assert event.project_id == workflow.project_id
    assert event.sequence == 1
    assert event.event_type == "workflow.run.queued"
    assert event.payload == {
        "workflow_id": workflow.workflow_id,
        "workflow_version_id": version.version_id,
        "alias": "prod",
        "source": "webhook",
        "actor": "@webhook",
        "manifest_mode": "saved_version",
    }

    same_run, duplicate_created = enqueue_workflow_run(
        db_session,
        workflow=workflow,
        version=version,
        alias="prod",
        source="webhook",
        actor="@webhook",
        input_text="new payload should not overwrite",
        idempotency_key="event-123",
        publish=published.append,
    )

    assert duplicate_created is False
    assert same_run.workflow_run_id == run.workflow_run_id
    assert published == [
        {
            "type": "workflow.run.queued",
            "workflow_id": workflow.workflow_id,
            "workflow_version_id": version.version_id,
            "workflow_run_id": run.workflow_run_id,
            "status": RUN_STATUS_QUEUED,
            "alias": "prod",
        }
    ]
    assert db_session.execute(select(CaliberWorkflowRun)).scalars().all() == [run]
    assert db_session.execute(select(CaliberWorkflowRunEvent)).scalars().all() == [event]


def test_enqueue_workflow_run_without_idempotency_creates_distinct_local_runs(
    db_session: Session,
) -> None:
    workflow, version = _seed_workflow(
        db_session,
        workflow_id="WF-RUN-LAUNCH-LOCAL",
        version_id="WFV-RUN-LAUNCH-LOCAL",
        project_id=None,
    )

    first, first_created = enqueue_workflow_run(
        db_session,
        workflow=workflow,
        version=version,
        alias="dev",
        source="manual",
        actor="@test",
    )
    second, second_created = enqueue_workflow_run(
        db_session,
        workflow=workflow,
        version=version,
        alias="dev",
        source="manual",
        actor="@test",
    )

    assert first_created is True
    assert second_created is True
    assert first.workflow_run_id != second.workflow_run_id
    assert first.tenant_id == "local"
    assert second.tenant_id == "local"
    assert first.input_payload == ""
    assert second.input_payload == ""
    assert first.summary["input"] == ""
    assert second.summary["input"] == ""
    assert first.summary["manifest_mode"] == "saved_version"
    assert second.summary["manifest_mode"] == "saved_version"


def test_append_run_event_continues_sequence_for_existing_timeline(
    db_session: Session,
) -> None:
    workflow, version = _seed_workflow(
        db_session,
        workflow_id="WF-RUN-LAUNCH-EVENTS",
        version_id="WFV-RUN-LAUNCH-EVENTS",
    )
    run, _ = enqueue_workflow_run(
        db_session,
        workflow=workflow,
        version=version,
        alias="prod",
        source="cron",
        actor="@scheduler",
    )

    _append_run_event(
        db_session,
        workflow_run_id=run.workflow_run_id,
        project_id=run.project_id,
        event_type="workflow.run.started",
        payload={"worker_id": "worker-1"},
    )

    events = (
        db_session.execute(
            select(CaliberWorkflowRunEvent)
            .where(CaliberWorkflowRunEvent.workflow_run_id == run.workflow_run_id)
            .order_by(CaliberWorkflowRunEvent.sequence)
        )
        .scalars()
        .all()
    )

    assert [event.sequence for event in events] == [1, 2]
    assert [event.event_type for event in events] == [
        "workflow.run.queued",
        "workflow.run.started",
    ]
    assert events[1].payload == {"worker_id": "worker-1"}


def test_enqueue_idempotency_conflict_uses_savepoint_preserving_prior_runs(
    db_session: Session,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Regression (#12): a duplicate-key conflict on flush must roll back ONLY
    the failed insert (via SAVEPOINT), not the caller's whole transaction. A
    bare session.rollback() previously discarded sibling runs already staged in
    the same session — e.g. other cron deployments fired in the same scheduler
    tick — silently dropping their scheduled runs."""
    workflow, version = _seed_workflow(db_session)

    # Two runs staged earlier in the same transaction (the "sibling" work).
    run1, c1 = enqueue_workflow_run(
        db_session,
        workflow=workflow,
        version=version,
        alias="prod",
        source="cron",
        actor="@sched",
        input_text="one",
        idempotency_key="cron:K1",
    )
    run2, c2 = enqueue_workflow_run(
        db_session,
        workflow=workflow,
        version=version,
        alias="prod",
        source="cron",
        actor="@sched",
        input_text="two",
        idempotency_key="cron:K2",
    )
    assert c1 is True and c2 is True

    # Force the INSERT path (bypass the SELECT short-circuit) so re-enqueuing K2
    # collides with run2 on the unique (workflow_id, source, idempotency_key)
    # constraint at flush — the same shape as a cross-process idempotency race.
    monkeypatch.setattr(run_launch, "_find_idempotent", lambda *a, **k: None)
    with pytest.raises(IntegrityError):
        enqueue_workflow_run(
            db_session,
            workflow=workflow,
            version=version,
            alias="prod",
            source="cron",
            actor="@sched",
            input_text="dup",
            idempotency_key="cron:K2",
        )

    # The SAVEPOINT rolled back ONLY the failed insert — both prior runs survive.
    # (With the old bare session.rollback(), the whole transaction was discarded
    # and these would be gone.)
    db_session.flush()
    assert db_session.get(CaliberWorkflowRun, run1.workflow_run_id) is not None
    assert db_session.get(CaliberWorkflowRun, run2.workflow_run_id) is not None
