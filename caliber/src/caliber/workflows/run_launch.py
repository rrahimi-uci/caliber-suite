"""Reusable workflow-run enqueue service.

The HTTP create-run handler, the event-trigger endpoint, and the cron scheduler
all need to put a ``queued`` :class:`CaliberWorkflowRun` on the queue the same
way. This module is that single insert path (idempotency check → row → timeline
event → audit), independent of Starlette so the background scheduler can call it.
"""

from __future__ import annotations

from collections.abc import Callable
from copy import deepcopy
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from caliber.audit import record as audit_record
from caliber.db.models import (
    CaliberProject,
    CaliberWorkflow,
    CaliberWorkflowRun,
    CaliberWorkflowRunEvent,
    CaliberWorkflowVersion,
)
from caliber.ids import new_workflow_run_id
from caliber.workflows.run_state import RUN_STATUS_QUEUED


def _find_idempotent(
    session: Session, *, workflow_id: str, source: str, idempotency_key: str | None
) -> CaliberWorkflowRun | None:
    if not idempotency_key:
        return None
    return (
        session.execute(
            select(CaliberWorkflowRun)
            .where(
                CaliberWorkflowRun.workflow_id == workflow_id,
                CaliberWorkflowRun.source == source,
                CaliberWorkflowRun.idempotency_key == idempotency_key,
            )
            .order_by(CaliberWorkflowRun.queued_at.desc())
        )
        .scalars()
        .first()
    )


def _append_run_event(
    session: Session,
    *,
    workflow_run_id: str,
    project_id: str | None,
    event_type: str,
    payload: dict[str, Any] | None = None,
) -> None:
    sequence = (
        session.execute(
            select(func.max(CaliberWorkflowRunEvent.sequence)).where(
                CaliberWorkflowRunEvent.workflow_run_id == workflow_run_id
            )
        )
        .scalars()
        .first()
    )
    session.add(
        CaliberWorkflowRunEvent(
            workflow_run_id=workflow_run_id,
            project_id=project_id,
            sequence=(sequence or 0) + 1,
            event_type=event_type,
            payload=payload,
        )
    )
    session.flush()


def _clone_manifest(manifest: dict[str, Any]) -> dict[str, Any]:
    return deepcopy(manifest)


def _saved_version_manifest_metadata(version: CaliberWorkflowVersion) -> dict[str, Any]:
    return {
        "manifest_mode": "saved_version",
        "manifest_hash": version.manifest_hash,
        "workflow_version_number": version.version_number,
    }


def enqueue_workflow_run(
    session: Session,
    *,
    workflow: CaliberWorkflow,
    version: CaliberWorkflowVersion,
    alias: str,
    source: str,
    actor: str,
    input_text: str = "",
    idempotency_key: str | None = None,
    priority: int = 0,
    session_id: str | None = None,
    publish: Callable[[dict[str, Any]], None] | None = None,
) -> tuple[CaliberWorkflowRun, bool]:
    """Insert a ``queued`` run for the worker to pick up.

    Returns ``(run, created)``. When an idempotent run already exists for
    ``(workflow_id, source, idempotency_key)`` it is returned with
    ``created=False`` and no new row/events are written. The caller owns the
    transaction boundary (this flushes but does not commit).
    """
    existing = _find_idempotent(
        session,
        workflow_id=workflow.workflow_id,
        source=source,
        idempotency_key=idempotency_key,
    )
    if existing is not None:
        return existing, False

    tenant_id = "local"
    if workflow.project_id:
        project = session.get(CaliberProject, workflow.project_id)
        if project is not None:
            tenant_id = project.tenant_id

    now = datetime.now(timezone.utc)
    manifest_metadata = _saved_version_manifest_metadata(version)
    run = CaliberWorkflowRun(
        workflow_run_id=new_workflow_run_id(),
        workflow_id=workflow.workflow_id,
        project_id=workflow.project_id,
        tenant_id=tenant_id,
        workflow_version_id=version.version_id,
        deployment_alias=alias,
        session_id=session_id,
        status=RUN_STATUS_QUEUED,
        source=source,
        created_by=actor,
        priority=priority,
        queued_at=now,
        started_at=None,
        attempt_number=1,
        idempotency_key=idempotency_key,
        input_payload=input_text or "",
        manifest_snapshot=_clone_manifest(version.manifest),
        summary={
            "preview": False,
            "status": RUN_STATUS_QUEUED,
            "input": (input_text or "")[:1000],
            **manifest_metadata,
        },
    )
    try:
        # SAVEPOINT so a duplicate-key conflict rolls back ONLY this insert. A
        # bare session.rollback() would unwind the caller's ENTIRE transaction
        # (the contract is "the caller owns the transaction boundary"), silently
        # discarding work already staged in the same session — e.g. sibling cron
        # runs enqueued earlier in the same scheduler tick.
        with session.begin_nested():
            session.add(run)
            session.flush()
    except IntegrityError:
        duplicate = _find_idempotent(
            session,
            workflow_id=workflow.workflow_id,
            source=source,
            idempotency_key=idempotency_key,
        )
        if duplicate is not None:
            return duplicate, False
        raise

    _append_run_event(
        session,
        workflow_run_id=run.workflow_run_id,
        project_id=run.project_id,
        event_type="workflow.run.queued",
        payload={
            "workflow_id": workflow.workflow_id,
            "workflow_version_id": version.version_id,
            "alias": alias,
            "source": source,
            "actor": actor,
            "manifest_mode": manifest_metadata["manifest_mode"],
        },
    )
    audit_record(
        session,
        actor=actor,
        action="enqueue_workflow_run",
        entity_type="workflow_run",
        entity_id=run.workflow_run_id,
        details={
            "workflow_id": workflow.workflow_id,
            "workflow_version_id": version.version_id,
            "alias": alias,
            "source": source,
            "manifest_mode": manifest_metadata["manifest_mode"],
            "manifest_hash": manifest_metadata["manifest_hash"],
        },
    )

    if publish is not None:
        publish(
            {
                "type": "workflow.run.queued",
                "workflow_id": run.workflow_id,
                "workflow_version_id": run.workflow_version_id,
                "workflow_run_id": run.workflow_run_id,
                "status": run.status,
                "alias": run.deployment_alias,
            }
        )
    return run, True
