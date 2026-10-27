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

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from caliber.audit import record as audit_record
from caliber.auth import CaliberIdentity
from caliber.db.models import (
    CaliberProject,
    CaliberWorkflow,
    CaliberWorkflowRun,
    CaliberWorkflowVersion,
    CaliberWorkspaceRelease,
)
from caliber.db.transactions import begin_sqlite_transaction
from caliber.ids import new_workflow_run_id
from caliber.workflows.admission import (
    SUBJECT_INVALID,
    AdmissionDenied,
    AdmissionRecord,
    AdmissionSettings,
    admitted_manifest_metadata,
    check_retry_intent,
    intent_of,
    persist_admission,
    prepare_admission,
)
from caliber.workflows.run_events import append_run_event
from caliber.workflows.run_state import RUN_STATUS_QUEUED
from caliber.workspace_runtime_lineage import create_runtime_lineage


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
    append_run_event(
        session,
        workflow_run_id=workflow_run_id,
        project_id=project_id,
        event_type=event_type,
        payload=payload,
    )


def _clone_manifest(manifest: dict[str, Any]) -> dict[str, Any]:
    return deepcopy(manifest)


def _saved_version_manifest_metadata(version: CaliberWorkflowVersion) -> dict[str, Any]:
    return {
        "manifest_mode": "saved_version",
        "manifest_hash": version.manifest_hash,
        "workflow_version_number": version.version_number,
    }


def enqueue_workflow_run(  # noqa: PLR0912, PLR0915 - one insert path: idempotency, admission, lineage, events
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
    workspace_release_id: str | None = None,
    environment_id: str | None = None,
    model_id: str | None = None,
    config_sha256: str | None = None,
    publish: Callable[[dict[str, Any]], None] | None = None,
    identity: CaliberIdentity | None = None,
    admission: AdmissionSettings | None = None,
) -> tuple[CaliberWorkflowRun, bool]:
    """Insert a ``queued`` run for the worker to pick up.

    Returns ``(run, created)``. When an idempotent run already exists for
    ``(workflow_id, source, idempotency_key)`` it is returned with
    ``created=False`` and no new row/events are written. The caller owns the
    transaction boundary (this flushes but does not commit).

    With ``admission`` enabled (B02.2a) the run is *admitted*: its pins are resolved and its manifest built before the
    row is written, the row is written with ``manifest_legacy = false``, and the grant, the policy decision and the
    immutable manifest are stored in this same transaction. ``identity`` is the authenticated caller (required). A
    refusal raises :class:`~caliber.workflows.admission.AdmissionDenied` and the caller must roll back: the run row
    has already been flushed. A retry that reuses an idempotency key returns the original run and manifest when it is
    the same request and raises ``AdmissionDenied`` (409) when it is not.
    """
    admitting = admission is not None and admission.enabled
    intent = intent_of(
        workflow_id=workflow.workflow_id,
        alias=alias,
        actor=actor,
        input_text=input_text or "",
        project_id=workflow.project_id,
        workspace_release_id=workspace_release_id,
        environment_id=environment_id,
        model_id=model_id,
        config_sha256=config_sha256,
        priority=priority,
        session_id=session_id,
    )
    existing = _find_idempotent(
        session,
        workflow_id=workflow.workflow_id,
        source=source,
        idempotency_key=idempotency_key,
    )
    if existing is not None:
        if admitting:
            check_retry_intent(existing, intent)
        return existing, False

    tenant_id = "local"
    if workflow.project_id:
        project = session.get(CaliberProject, workflow.project_id)
        if project is not None:
            tenant_id = project.tenant_id

    now = datetime.now(timezone.utc)
    prepared = None
    if admitting:
        assert admission is not None
        if identity is None:
            raise AdmissionDenied(
                SUBJECT_INVALID, "an admitted run needs the authenticated caller", status=401
            )
        prepared = prepare_admission(
            settings=admission,
            workflow=workflow,
            version=version,
            identity=identity,
            source=source,
            now=now,
            run_tenant_id=tenant_id,
        )
    manifest_snapshot = _clone_manifest(version.manifest)
    manifest_metadata = _saved_version_manifest_metadata(version)
    if prepared is not None:
        # An admitted run executes exactly what its pins describe: the version's sealed, resolved manifest.
        manifest_snapshot = _clone_manifest(prepared.execution_manifest)
        manifest_metadata = admitted_manifest_metadata(version, manifest_snapshot)
    run_summary = {
        "preview": False,
        "status": RUN_STATUS_QUEUED,
        "input": (input_text or "")[:1000],
        **manifest_metadata,
    }
    if workspace_release_id is not None:
        run_summary["strict_lineage"] = True
    if prepared is not None:
        run_summary["admission_intent"] = intent

    run = CaliberWorkflowRun(
        workflow_run_id=prepared.run_id if prepared is not None else new_workflow_run_id(),
        manifest_legacy=prepared is None,
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
        manifest_snapshot=manifest_snapshot,
        summary=run_summary,
    )
    try:
        # SAVEPOINT so a duplicate-key conflict rolls back ONLY this insert. A
        # bare session.rollback() would unwind the caller's ENTIRE transaction
        # (the contract is "the caller owns the transaction boundary"), silently
        # discarding work already staged in the same session — e.g. sibling cron
        # runs enqueued earlier in the same scheduler tick.
        begin_sqlite_transaction(
            session
        )  # a first-statement SAVEPOINT would autocommit on pysqlite
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
            if admitting:
                check_retry_intent(duplicate, intent)
            return duplicate, False
        raise

    if workspace_release_id is not None and environment_id is not None:
        release = session.get(CaliberWorkspaceRelease, workspace_release_id)
        lineage = create_runtime_lineage(
            session,
            project_id=workflow.project_id or "",
            workspace_release_id=workspace_release_id,
            revision_id=release.revision_id if release is not None else "",
            environment_id=environment_id,
            consumer_kind="run",
            consumer_id=run.workflow_run_id,
            model_id=model_id,
            config_sha256=config_sha256,
            strict_execution=True,
            created_by=actor,
        )
        run.runtime_lineage_id = lineage.lineage_id
        session.flush()

    record: AdmissionRecord | None = None
    if prepared is not None:
        assert admission is not None and identity is not None
        record = persist_admission(session, prepared, run, identity=identity, settings=admission)
        run.summary = {**(run.summary or {}), "admitted": True, "manifest_id": record.manifest_id}
        session.flush()

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
            **(
                {
                    "admitted": True,
                    "grant_id": record.grant_id,
                    "decision_id": record.decision_id,
                    "manifest_id": record.manifest_id,
                    "run_manifest_digest": record.manifest_digest,
                }
                if record is not None
                else {}
            ),
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
