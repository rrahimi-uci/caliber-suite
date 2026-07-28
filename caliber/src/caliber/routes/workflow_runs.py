"""Async workflow-run routes (queue-based submit + lifecycle reads)."""

from __future__ import annotations

import logging
from copy import deepcopy
from datetime import datetime, timezone
from typing import Any, Literal

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session
from starlette.applications import Starlette
from starlette.exceptions import HTTPException
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from caliber.audit import record as audit_record
from caliber.auth import SCOPE_OPERATOR, require_scopes, require_user
from caliber.db.models import (
    CaliberProject,
    CaliberRuntimeApprovalRequest,
    CaliberWorkflow,
    CaliberWorkflowDeployment,
    CaliberWorkflowRun,
    CaliberWorkflowRunCheckpoint,
    CaliberWorkflowRunEvent,
    CaliberWorkflowVersion,
)
from caliber.ids import new_workflow_run_id
from caliber.mcp_policy import deployment_blockers
from caliber.routes._deps import envelope_response, get_session_factory, parse_json_object
from caliber.schemas import (
    WorkflowRunApprovalDecisionRequest,
    WorkflowRunCancelRequest,
    WorkflowRunCheckpointSchema,
    WorkflowRunCreateRequest,
    WorkflowRunEventSchema,
    WorkflowRunLineageSchema,
    WorkflowRunManifestSchema,
    WorkflowRunResumeByEventRequest,
    WorkflowRunResumeRequest,
    WorkflowRunRetryRequest,
    WorkflowRunSchema,
    WorkflowRuntimeApprovalSchema,
    WorkflowRunTraceSchema,
    WorkflowTriggerRequest,
)
from caliber.trace_client import fetch_trace_spans
from caliber.workflows.manifest import (
    StartNode,
    StartTrigger,
    WorkflowManifestError,
    compute_manifest_hash,
    parse_manifest,
)
from caliber.workflows.run_launch import enqueue_workflow_run
from caliber.workflows.run_state import (
    RUN_STATUS_CANCELLED,
    RUN_STATUS_COMPLETED,
    RUN_STATUS_EXPIRED,
    RUN_STATUS_FAILED,
    RUN_STATUS_QUEUED,
    RUN_STATUS_RUNNING,
    RUN_STATUS_WAITING_APPROVAL,
    RUN_STATUS_WAITING_EVENT,
    InvalidRunStateTransition,
    assert_run_transition,
)
from caliber.workflows.validation import find_inline_secrets

PREFIX = "/ajax-api/2.0/mlflow/caliber"
CREATE_PATH = PREFIX + "/workflow-runs"
DETAIL_PATH = PREFIX + "/workflow-runs/{run_id}"
LINEAGE_PATH = DETAIL_PATH + "/lineage"
MANIFEST_PATH = DETAIL_PATH + "/manifest"
EVENTS_PATH = DETAIL_PATH + "/events"
TRACE_PATH = DETAIL_PATH + "/trace"
CHECKPOINTS_PATH = DETAIL_PATH + "/checkpoints"
CANCEL_PATH = DETAIL_PATH + "/cancel"
RETRY_PATH = DETAIL_PATH + "/retry"
APPROVALS_PATH = DETAIL_PATH + "/approvals"
APPROVE_PATH = DETAIL_PATH + "/approval/approve"
REJECT_PATH = DETAIL_PATH + "/approval/reject"
RESUME_PATH = DETAIL_PATH + "/resume"
RESUME_BY_EVENT_PATH = PREFIX + "/workflow-runs/resume-by-event"
TRIGGER_PATH = PREFIX + "/workflows/{workflow_id}/trigger"
logger = logging.getLogger("caliber.routes.workflow_runs")


def _workflow_and_version_for_run(
    session: Session, payload: WorkflowRunCreateRequest
) -> tuple[CaliberWorkflow, CaliberWorkflowVersion, str]:
    if payload.workflow_version_id:
        version = session.get(CaliberWorkflowVersion, payload.workflow_version_id)
        if version is None:
            raise HTTPException(
                status_code=404,
                detail=f"workflow version {payload.workflow_version_id!r} not found",
            )
        workflow = session.get(CaliberWorkflow, version.workflow_id)
        if workflow is None:
            raise HTTPException(
                status_code=404,
                detail=f"workflow {version.workflow_id!r} not found",
            )
        if payload.workflow_id and payload.workflow_id != workflow.workflow_id:
            raise HTTPException(
                status_code=400,
                detail="workflow_id does not match workflow_version_id",
            )
        return workflow, version, payload.alias or "manual"

    workflow_id = payload.workflow_id or ""
    alias = payload.alias or "manual"
    workflow_row = session.get(CaliberWorkflow, workflow_id)
    if workflow_row is None:
        raise HTTPException(status_code=404, detail=f"workflow {workflow_id!r} not found")

    if alias == "manual":
        version = (
            session.execute(
                select(CaliberWorkflowVersion)
                .where(CaliberWorkflowVersion.workflow_id == workflow_id)
                .order_by(CaliberWorkflowVersion.version_number.desc())
            )
            .scalars()
            .first()
        )
        if version is None:
            raise HTTPException(
                status_code=404,
                detail=f"no workflow versions found for workflow {workflow_id!r}",
            )
        return workflow_row, version, "manual"

    deployment = (
        session.execute(
            select(CaliberWorkflowDeployment)
            .where(
                CaliberWorkflowDeployment.workflow_id == workflow_id,
                CaliberWorkflowDeployment.alias == alias,
                CaliberWorkflowDeployment.status == "active",
            )
            .order_by(CaliberWorkflowDeployment.deployed_at.desc())
        )
        .scalars()
        .first()
    )
    if deployment is None:
        raise HTTPException(
            status_code=404,
            detail=f"no active deployment for workflow {workflow_id!r} alias {alias!r}",
        )
    version = session.get(CaliberWorkflowVersion, deployment.version_id)
    if version is None:
        raise HTTPException(
            status_code=409,
            detail=(
                f"deployment {deployment.deployment_id!r} points to missing version "
                f"{deployment.version_id!r}"
            ),
        )
    return workflow_row, version, deployment.alias


def _append_run_event(
    session: Session,
    *,
    workflow_run_id: str,
    project_id: str | None,
    event_type: str,
    payload: dict[str, Any] | None = None,
    node_id: str | None = None,
) -> CaliberWorkflowRunEvent:
    sequence = (
        session.execute(
            select(func.max(CaliberWorkflowRunEvent.sequence)).where(
                CaliberWorkflowRunEvent.workflow_run_id == workflow_run_id
            )
        )
        .scalars()
        .first()
    )
    event = CaliberWorkflowRunEvent(
        workflow_run_id=workflow_run_id,
        project_id=project_id,
        sequence=(sequence or 0) + 1,
        event_type=event_type,
        node_id=node_id,
        payload=payload,
    )
    session.add(event)
    session.flush()
    return event


def _find_idempotent(
    session: Session,
    *,
    workflow_id: str,
    source: str,
    idempotency_key: str | None,
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


def _emit_queue_event(request: Request, payload: dict[str, Any]) -> None:
    publish = getattr(getattr(request.app.state, "event_bus", None), "publish", None)
    if callable(publish):
        try:
            publish(payload)
        except Exception:
            logger.warning(
                "failed to publish workflow-run event type=%r",
                payload.get("type"),
                exc_info=True,
            )


def _ensure_queue_enabled(request: Request) -> None:
    if bool(request.app.state.config.workflow_run_queue_enabled):
        return
    raise HTTPException(
        status_code=409,
        detail="workflow run queue is disabled (set CALIBER_WORKFLOW_RUN_QUEUE_ENABLED=true)",
    )


def _ensure_runtime_approvals_enabled(request: Request) -> None:
    if bool(request.app.state.config.workflow_run_runtime_approvals_enabled):
        return
    raise HTTPException(
        status_code=409,
        detail=(
            "runtime workflow approvals are disabled "
            "(set CALIBER_WORKFLOW_RUN_RUNTIME_APPROVALS_ENABLED=true)"
        ),
    )


def _ensure_checkpointing_enabled(request: Request) -> None:
    if bool(request.app.state.config.workflow_run_checkpointing_enabled):
        return
    raise HTTPException(
        status_code=409,
        detail=(
            "workflow run checkpointing is disabled "
            "(set CALIBER_WORKFLOW_RUN_CHECKPOINTING_ENABLED=true)"
        ),
    )


def _get_run_or_404(session: Session, run_id: str) -> CaliberWorkflowRun:
    row = session.get(CaliberWorkflowRun, run_id)
    if row is None:
        raise HTTPException(status_code=404, detail=f"workflow run {run_id!r} not found")
    return row


def _run_lineage_sort_key(run: CaliberWorkflowRun) -> tuple[int, datetime, str]:
    timestamp = run.queued_at or run.started_at or run.completed_at
    if timestamp is None:
        timestamp = datetime.min.replace(tzinfo=timezone.utc)
    elif timestamp.tzinfo is None:
        timestamp = timestamp.replace(tzinfo=timezone.utc)
    return (
        max(1, int(run.attempt_number or 1)),
        timestamp,
        run.workflow_run_id,
    )


def _build_workflow_run_lineage(
    session: Session,
    run: CaliberWorkflowRun,
    *,
    max_runs: int = 250,
    max_parent_hops: int = 64,
) -> WorkflowRunLineageSchema:
    lineage_runs: dict[str, CaliberWorkflowRun] = {run.workflow_run_id: run}
    parent_count = 0
    missing_parent_id: str | None = None
    truncated = False
    seen_parent_ids: set[str] = {run.workflow_run_id}
    cursor = run
    parent_hops = 0

    while cursor.parent_run_id:
        parent_id = cursor.parent_run_id
        if parent_id in seen_parent_ids:
            truncated = True
            break
        if parent_hops >= max_parent_hops or len(lineage_runs) >= max_runs:
            truncated = True
            break
        seen_parent_ids.add(parent_id)
        parent = session.get(CaliberWorkflowRun, parent_id)
        if parent is None:
            missing_parent_id = parent_id
            break
        lineage_runs[parent.workflow_run_id] = parent
        parent_count += 1
        cursor = parent
        parent_hops += 1

    root_run = cursor
    workflow_scope_id = root_run.workflow_id or run.workflow_id
    frontier: list[str] = list(lineage_runs)
    visited_frontier: set[str] = set()

    while frontier and len(lineage_runs) < max_runs:
        children = (
            session.execute(
                select(CaliberWorkflowRun)
                .where(
                    CaliberWorkflowRun.workflow_id == workflow_scope_id,
                    CaliberWorkflowRun.parent_run_id.in_(frontier),
                )
                .order_by(
                    CaliberWorkflowRun.queued_at.asc(),
                    CaliberWorkflowRun.workflow_run_id.asc(),
                )
            )
            .scalars()
            .all()
        )
        next_frontier: list[str] = []
        for child in children:
            child_id = child.workflow_run_id
            if child_id in lineage_runs or child_id in visited_frontier:
                continue
            lineage_runs[child_id] = child
            next_frontier.append(child_id)
            if len(lineage_runs) >= max_runs:
                truncated = True
                break
        visited_frontier.update(frontier)
        frontier = [child_id for child_id in next_frontier if child_id not in visited_frontier]
        if frontier and len(lineage_runs) >= max_runs:
            truncated = True
            break

    child_count = int(
        session.execute(
            select(func.count())
            .select_from(CaliberWorkflowRun)
            .where(
                CaliberWorkflowRun.workflow_id == run.workflow_id,
                CaliberWorkflowRun.parent_run_id == run.workflow_run_id,
            )
        ).scalar_one()
    )

    ordered_runs = sorted(lineage_runs.values(), key=_run_lineage_sort_key)
    return WorkflowRunLineageSchema(
        workflow_run_id=run.workflow_run_id,
        root_run_id=root_run.workflow_run_id,
        total_attempts=len(ordered_runs),
        parent_count=parent_count,
        child_count=child_count,
        missing_parent_id=missing_parent_id,
        truncated=truncated,
        runs=[WorkflowRunSchema.model_validate(item) for item in ordered_runs],
    )


def _summary_input(summary: dict[str, Any] | None) -> str:
    if not isinstance(summary, dict):
        return ""
    value = summary.get("input")
    if isinstance(value, str):
        return value
    if value is None:
        return ""
    return str(value)


def _transition_or_409(from_status: str, to_status: str) -> None:
    try:
        assert_run_transition(from_status, to_status)
    except InvalidRunStateTransition as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


def _parse_manifest_or_400(manifest: dict[str, Any]) -> None:
    try:
        parse_manifest(manifest)
    except (WorkflowManifestError, ValueError) as exc:
        raise HTTPException(status_code=400, detail=f"invalid manifest: {exc}") from exc
    secrets = find_inline_secrets(manifest)
    if secrets:
        raise HTTPException(
            status_code=400,
            detail=f"manifest contains inline secret value(s) at {secrets}; "
            "reference secrets by name via tool secret_refs (plan §18.2)",
        )


def _retry_checkpoint_manifest_error(  # noqa: PLR0911, PLR0912
    checkpoint: CaliberWorkflowRunCheckpoint,
    manifest: dict[str, Any],
) -> str | None:
    state_blob = checkpoint.state_blob if isinstance(checkpoint.state_blob, dict) else None
    if state_blob is None:
        return "workflow run retry checkpoint is corrupt"
    checkpoint_node_id = state_blob.get("node_id")
    row_node_id = checkpoint.node_id
    if not isinstance(checkpoint_node_id, str) or not checkpoint_node_id:
        return "workflow run retry checkpoint is missing node_id"
    if not isinstance(row_node_id, str) or not row_node_id or row_node_id != checkpoint_node_id:
        return "workflow run retry checkpoint does not match its stored node identity"
    kind = state_blob.get("kind")
    input_by_port = state_blob.get("input_by_port")
    if kind in {
        "wait_for_event",
        "wait_until",
        "runtime_approval",
        "human_approval",
    } and not isinstance(input_by_port, dict):
        return "workflow run retry checkpoint is missing its input snapshot"
    if kind == "wait_for_event":
        expected_event_name = state_blob.get("expected_event_name")
        if not isinstance(expected_event_name, str) or not expected_event_name.strip():
            return "workflow run retry checkpoint is missing its expected event name"
        injected_inputs = state_blob.get("resume_event_inputs")
        if not isinstance(injected_inputs, dict):
            return "workflow run retry checkpoint is missing its stored resume event payload"
        injected_event_name = (
            injected_inputs.get("event_name")
            if isinstance(injected_inputs.get("event_name"), str)
            else None
        )
        if (
            isinstance(injected_event_name, str)
            and injected_event_name.strip()
            and injected_event_name != expected_event_name
        ):
            return (
                f"workflow run retry checkpoint event {injected_event_name!r} does not match "
                f"expected event {expected_event_name!r}"
            )
        has_resume_payload = False
        for key in ("resume_event", "event_payload", "event"):
            if injected_inputs.get(key) not in (None, "", {}):
                has_resume_payload = True
                break
        if (
            not has_resume_payload
            and expected_event_name
            and injected_inputs.get(expected_event_name) not in (None, "", {})
        ):
            has_resume_payload = True
        if not has_resume_payload:
            return "workflow run retry checkpoint is missing its stored resume event payload"
    try:
        parsed_manifest = parse_manifest(manifest)
    except (WorkflowManifestError, ValueError) as exc:
        return f"workflow run retry manifest is invalid: {exc}"
    node = parsed_manifest.nodes.get(checkpoint_node_id)
    if node is None:
        return (
            f"workflow run retry checkpoint node {checkpoint_node_id!r} "
            "is not present in the current manifest"
        )
    node_type = getattr(node, "type", None)
    if kind == "wait_for_event" and node_type != "wait_for_event":
        return (
            f"workflow run retry checkpoint kind {kind!r} does not match current manifest "
            f"node {checkpoint_node_id!r} type {node_type!r}"
        )
    if kind == "wait_until" and node_type != "wait_until":
        return (
            f"workflow run retry checkpoint kind {kind!r} does not match current manifest "
            f"node {checkpoint_node_id!r} type {node_type!r}"
        )
    if kind == "human_approval" and node_type != "human_approval":
        return (
            f"workflow run retry checkpoint kind {kind!r} does not match current manifest "
            f"node {checkpoint_node_id!r} type {node_type!r}"
        )
    if kind == "runtime_approval":
        if node_type != "tool":
            return (
                f"workflow run retry checkpoint kind {kind!r} does not match current manifest "
                f"node {checkpoint_node_id!r} type {node_type!r}"
            )
        tool_name = getattr(node, "tool_name", None)
        binding = parsed_manifest.tools.get(tool_name) if isinstance(tool_name, str) else None
        if binding is None or not getattr(binding, "requires_approval", False):
            return (
                f"workflow run retry checkpoint kind {kind!r} does not match current manifest "
                f"node {checkpoint_node_id!r} type {node_type!r}"
            )
    return None


def _manifest_summary_metadata(
    version: CaliberWorkflowVersion,
    manifest: dict[str, Any] | None,
) -> dict[str, Any]:
    if manifest is None:
        return {
            "manifest_mode": "saved_version",
            "manifest_hash": version.manifest_hash,
            "workflow_version_number": version.version_number,
        }
    return {
        "manifest_mode": "snapshot",
        "manifest_hash": compute_manifest_hash(manifest),
        "workflow_version_number": version.version_number,
    }


def _copy_manifest_summary_metadata(summary: dict[str, Any] | None) -> dict[str, Any]:
    if not isinstance(summary, dict):
        return {}
    copied: dict[str, Any] = {}
    manifest_mode = summary.get("manifest_mode")
    if manifest_mode in {"saved_version", "snapshot"}:
        copied["manifest_mode"] = manifest_mode
    manifest_hash = summary.get("manifest_hash")
    if isinstance(manifest_hash, str) and manifest_hash:
        copied["manifest_hash"] = manifest_hash
    version_number = summary.get("workflow_version_number")
    if isinstance(version_number, int):
        copied["workflow_version_number"] = version_number
    return copied


def _clone_manifest(manifest: dict[str, Any]) -> dict[str, Any]:
    return deepcopy(manifest)


async def create_workflow_run(request: Request) -> JSONResponse:
    actor = require_scopes(request, [SCOPE_OPERATOR])
    _ensure_queue_enabled(request)
    body = await parse_json_object(request)
    payload = WorkflowRunCreateRequest.model_validate(body)
    factory = get_session_factory(request)
    with factory() as session:
        workflow, version, alias = _workflow_and_version_for_run(session, payload)
        submitted_manifest = payload.manifest
        if submitted_manifest is not None and alias != "manual":
            raise HTTPException(
                status_code=400,
                detail=(
                    "manifest overrides are preview/manual-run only; deployed aliases execute "
                    "their immutable saved version"
                ),
            )
        manifest_metadata = _manifest_summary_metadata(version, submitted_manifest)
        manifest_snapshot = (
            _clone_manifest(submitted_manifest)
            if submitted_manifest is not None
            else _clone_manifest(version.manifest)
        )
        if submitted_manifest is not None:
            _parse_manifest_or_400(manifest_snapshot)
        mcp_blockers = deployment_blockers(session, manifest_snapshot, alias=alias)
        if mcp_blockers:
            raise HTTPException(
                status_code=400,
                detail="MCP runtime preflight failed: " + "; ".join(mcp_blockers),
            )
        if workflow.status == "paused":
            raise HTTPException(
                status_code=409, detail="workflow is paused; resume it before running"
            )
        if workflow.status == "archived":
            raise HTTPException(status_code=409, detail="archived workflows cannot be run")

        existing = _find_idempotent(
            session,
            workflow_id=workflow.workflow_id,
            source=payload.source,
            idempotency_key=payload.idempotency_key,
        )
        if existing is not None:
            return envelope_response(WorkflowRunSchema.model_validate(existing), status_code=202)

        tenant_id = "local"
        if workflow.project_id:
            project = session.get(CaliberProject, workflow.project_id)
            if project is not None:
                tenant_id = project.tenant_id

        now = datetime.now(timezone.utc)
        run = CaliberWorkflowRun(
            workflow_run_id=new_workflow_run_id(),
            workflow_id=workflow.workflow_id,
            project_id=workflow.project_id,
            tenant_id=tenant_id,
            workflow_version_id=version.version_id,
            deployment_alias=alias,
            session_id=payload.session_id,
            status=RUN_STATUS_QUEUED,
            source=payload.source,
            priority=payload.priority,
            queued_at=now,
            started_at=None,
            attempt_number=1,
            idempotency_key=payload.idempotency_key,
            # Full input lives in input_payload (worker replays from it); the
            # summary keeps a bounded preview so polling responses stay small.
            input_payload=_input_to_text(payload.input),
            manifest_snapshot=manifest_snapshot,
            summary={
                "preview": False,
                "status": RUN_STATUS_QUEUED,
                "input": _input_to_text(payload.input)[:1000],
                **(
                    {"input_files": [dict(item) for item in payload.input_files]}
                    if payload.input_files
                    else {}
                ),
                **manifest_metadata,
            },
        )
        session.add(run)
        try:
            session.flush()
        except IntegrityError as exc:
            session.rollback()
            duplicate = _find_idempotent(
                session,
                workflow_id=workflow.workflow_id,
                source=payload.source,
                idempotency_key=payload.idempotency_key,
            )
            if duplicate is not None:
                return envelope_response(
                    WorkflowRunSchema.model_validate(duplicate), status_code=202
                )
            raise HTTPException(
                status_code=409, detail="workflow run idempotency conflict"
            ) from exc

        _append_run_event(
            session,
            workflow_run_id=run.workflow_run_id,
            project_id=run.project_id,
            event_type="workflow.run.queued",
            payload={
                "workflow_id": workflow.workflow_id,
                "workflow_version_id": version.version_id,
                "alias": alias,
                "source": payload.source,
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
                "source": payload.source,
                "priority": payload.priority,
                "manifest_mode": manifest_metadata["manifest_mode"],
                "manifest_hash": manifest_metadata["manifest_hash"],
            },
        )
        session.commit()
        data = WorkflowRunSchema.model_validate(run)

    _emit_queue_event(
        request,
        {
            "type": "workflow.run.queued",
            "workflow_id": data.workflow_id,
            "workflow_version_id": data.workflow_version_id,
            "workflow_run_id": data.workflow_run_id,
            "status": data.status,
            "alias": data.deployment_alias,
        },
    )
    return envelope_response(data, status_code=202)


def _start_trigger(version: CaliberWorkflowVersion) -> StartTrigger | None:
    """Extract the Start node's trigger config from a version's manifest."""
    try:
        manifest = parse_manifest(version.manifest)
    except Exception:  # malformed manifest — treat as no trigger
        return None
    for node in manifest.nodes.values():
        if isinstance(node, StartNode):
            return node.trigger
    return None


def _resolve_event_trigger_target(
    session: Session,
    *,
    workflow_id: str,
    requested_alias: str | None,
) -> tuple[CaliberWorkflow, CaliberWorkflowVersion, str, StartTrigger]:
    if requested_alias:
        workflow, version, alias = _workflow_and_version_for_run(
            session,
            WorkflowRunCreateRequest(workflow_id=workflow_id, alias=requested_alias),
        )
        trigger = _start_trigger(version)
        if trigger is None or trigger.mode != "event":
            raise HTTPException(
                status_code=409,
                detail="workflow Start is not configured for event triggers",
            )
        if not trigger.enabled:
            raise HTTPException(status_code=409, detail="event trigger is disabled")
        if trigger.alias != alias:
            raise HTTPException(
                status_code=409,
                detail=(
                    f"workflow Start event trigger targets alias {trigger.alias!r}, not {alias!r}"
                ),
            )
        return workflow, version, alias, trigger

    workflow_row = session.get(CaliberWorkflow, workflow_id)
    if workflow_row is None:
        raise HTTPException(status_code=404, detail=f"workflow {workflow_id!r} not found")

    deployments = (
        session.execute(
            select(CaliberWorkflowDeployment)
            .where(
                CaliberWorkflowDeployment.workflow_id == workflow_id,
                CaliberWorkflowDeployment.status == "active",
            )
            .order_by(CaliberWorkflowDeployment.deployed_at.desc())
        )
        .scalars()
        .all()
    )
    candidates: list[tuple[CaliberWorkflowVersion, str, StartTrigger]] = []
    seen_aliases: set[str] = set()
    for deployment in deployments:
        if deployment.alias in seen_aliases:
            continue
        seen_aliases.add(deployment.alias)
        version_row = session.get(CaliberWorkflowVersion, deployment.version_id)
        if version_row is None:
            continue
        trigger = _start_trigger(version_row)
        if trigger is None or trigger.mode != "event" or not trigger.enabled:
            continue
        if trigger.alias != deployment.alias:
            continue
        candidates.append((version_row, deployment.alias, trigger))

    if not candidates:
        raise HTTPException(
            status_code=409,
            detail="workflow Start is not configured for event triggers",
        )
    if len(candidates) > 1:
        raise HTTPException(
            status_code=409,
            detail="workflow exposes multiple active event trigger aliases; specify alias explicitly",
        )
    version, alias, trigger = candidates[0]
    return workflow_row, version, alias, trigger


async def trigger_workflow_event(request: Request) -> JSONResponse:
    """Start a run from an external event (Start trigger ``mode == "event"``).

    Resolves the deployed version targeted by the Start trigger. Callers can
    pass ``alias`` explicitly; otherwise the route uses the configured trigger
    alias when there is exactly one active event target. When the Start trigger
    names an ``event_name``, a mismatching ``event_name`` in the body is
    rejected.
    """
    workflow_id = request.path_params["workflow_id"]
    actor = require_scopes(request, [SCOPE_OPERATOR])
    _ensure_queue_enabled(request)
    body = await parse_json_object(request, allow_empty=True)
    payload = WorkflowTriggerRequest.model_validate(body)

    factory = get_session_factory(request)
    with factory() as session:
        workflow, version, alias, trigger = _resolve_event_trigger_target(
            session,
            workflow_id=workflow_id,
            requested_alias=payload.alias,
        )
        if workflow.status in {"paused", "archived"}:
            raise HTTPException(
                status_code=409, detail=f"workflow is {workflow.status}; cannot trigger"
            )
        if trigger.event_name and payload.event_name and trigger.event_name != payload.event_name:
            raise HTTPException(
                status_code=409,
                detail=(
                    f"event_name {payload.event_name!r} does not match the workflow's "
                    f"configured event {trigger.event_name!r}"
                ),
            )

        run, created = enqueue_workflow_run(
            session,
            workflow=workflow,
            version=version,
            alias=alias,
            source="event",
            actor=actor,
            input_text=_input_to_text(payload.input),
            idempotency_key=payload.idempotency_key,
            publish=lambda evt: _emit_queue_event(request, evt),
        )
        session.commit()
        data = WorkflowRunSchema.model_validate(run)
    return envelope_response(data, status_code=202 if created else 200)


def _input_to_text(value: object) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    return str(value)


async def get_workflow_run(request: Request) -> JSONResponse:
    require_user(request)
    run_id = request.path_params["run_id"]
    factory = get_session_factory(request)
    with factory() as session:
        row = _get_run_or_404(session, run_id)
        data = WorkflowRunSchema.model_validate(row)
    return envelope_response(data)


async def get_workflow_run_lineage(request: Request) -> JSONResponse:
    require_user(request)
    run_id = request.path_params["run_id"]
    factory = get_session_factory(request)
    with factory() as session:
        run = _get_run_or_404(session, run_id)
        data = _build_workflow_run_lineage(session, run)
    return envelope_response(data)


async def get_workflow_run_manifest(request: Request) -> JSONResponse:
    require_user(request)
    run_id = request.path_params["run_id"]
    factory = get_session_factory(request)
    with factory() as session:
        run = _get_run_or_404(session, run_id)
        summary = dict(run.summary or {})
        manifest_mode: Literal["saved_version", "snapshot"]
        if isinstance(run.manifest_snapshot, dict):
            manifest = _clone_manifest(run.manifest_snapshot)
            manifest_hash = str(summary.get("manifest_hash") or compute_manifest_hash(manifest))
            summary_mode = summary.get("manifest_mode")
            manifest_mode = (
                summary_mode if summary_mode in {"saved_version", "snapshot"} else "snapshot"
            )
        else:
            if not run.workflow_version_id:
                raise HTTPException(
                    status_code=404,
                    detail=(
                        f"workflow run {run.workflow_run_id!r} does not have a manifest snapshot "
                        "or workflow_version_id"
                    ),
                )
            version = session.get(CaliberWorkflowVersion, run.workflow_version_id)
            if version is None:
                raise HTTPException(
                    status_code=404,
                    detail=(
                        f"workflow version {run.workflow_version_id!r} not found for workflow run "
                        f"{run.workflow_run_id!r}"
                    ),
                )
            manifest = _clone_manifest(version.manifest or {})
            manifest_hash = version.manifest_hash
            manifest_mode = "saved_version"
        data = WorkflowRunManifestSchema(
            workflow_run_id=run.workflow_run_id,
            workflow_id=run.workflow_id,
            workflow_version_id=run.workflow_version_id,
            manifest_mode=manifest_mode,
            manifest_hash=manifest_hash,
            manifest=manifest,
        )
    return envelope_response(data)


def _parse_positive_int(
    raw: str | None,
    *,
    default: int,
    min_value: int,
    max_value: int,
    label: str,
) -> int:
    if raw is None:
        return default
    try:
        value = int(raw)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=f"{label} must be an integer") from exc
    if value < min_value or value > max_value:
        raise HTTPException(
            status_code=400, detail=f"{label} must be between {min_value} and {max_value}"
        )
    return value


async def list_workflow_run_events(request: Request) -> JSONResponse:
    require_user(request)
    run_id = request.path_params["run_id"]
    after = _parse_positive_int(
        request.query_params.get("after"),
        default=0,
        min_value=0,
        max_value=10_000_000,
        label="after",
    )
    limit = _parse_positive_int(
        request.query_params.get("limit"),
        default=200,
        min_value=1,
        max_value=1_000,
        label="limit",
    )
    factory = get_session_factory(request)
    with factory() as session:
        _get_run_or_404(session, run_id)
        events = (
            session.execute(
                select(CaliberWorkflowRunEvent)
                .where(
                    CaliberWorkflowRunEvent.workflow_run_id == run_id,
                    CaliberWorkflowRunEvent.sequence > after,
                )
                .order_by(CaliberWorkflowRunEvent.sequence.asc())
                .limit(limit)
            )
            .scalars()
            .all()
        )
        data = [WorkflowRunEventSchema.model_validate(event) for event in events]
    return envelope_response(data)


async def get_workflow_run_trace(request: Request) -> JSONResponse:
    """Return the run's MLflow trace as a JSON-able span tree (in-app viewer).

    Resolves the run → its ``trace_id`` and fetches the span tree via
    :func:`fetch_trace_spans` (guarded — MLflow absent / no trace → empty). When
    the run has no ``trace_id`` we short-circuit to ``{trace_id: null, spans: []}``
    with a 200, so the fake provider / tracing-off paths render a friendly empty
    state rather than a 404.
    """
    require_user(request)
    run_id = request.path_params["run_id"]
    factory = get_session_factory(request)
    with factory() as session:
        run = _get_run_or_404(session, run_id)
        trace_id = run.trace_id or None
    if not trace_id:
        return envelope_response(WorkflowRunTraceSchema(trace_id=None, spans=[]))
    tree = fetch_trace_spans(trace_id)
    data = WorkflowRunTraceSchema.model_validate(
        {"trace_id": tree.trace_id, "spans": tree.spans, "mlflow_url": tree.mlflow_url}
    )
    return envelope_response(data)


async def list_workflow_run_checkpoints(request: Request) -> JSONResponse:
    require_user(request)
    run_id = request.path_params["run_id"]
    after = _parse_positive_int(
        request.query_params.get("after"),
        default=0,
        min_value=0,
        max_value=10_000_000,
        label="after",
    )
    limit = _parse_positive_int(
        request.query_params.get("limit"),
        default=200,
        min_value=1,
        max_value=1_000,
        label="limit",
    )
    factory = get_session_factory(request)
    with factory() as session:
        _get_run_or_404(session, run_id)
        checkpoints = (
            session.execute(
                select(CaliberWorkflowRunCheckpoint)
                .where(
                    CaliberWorkflowRunCheckpoint.workflow_run_id == run_id,
                    CaliberWorkflowRunCheckpoint.sequence > after,
                )
                .order_by(CaliberWorkflowRunCheckpoint.sequence.asc())
                .limit(limit)
            )
            .scalars()
            .all()
        )
        data = [
            WorkflowRunCheckpointSchema.model_validate(checkpoint) for checkpoint in checkpoints
        ]
    return envelope_response(data)


async def cancel_workflow_run(request: Request) -> JSONResponse:
    actor = require_scopes(request, [SCOPE_OPERATOR])
    _ensure_queue_enabled(request)
    run_id = request.path_params["run_id"]
    body = await parse_json_object(request, allow_empty=True)
    payload = WorkflowRunCancelRequest.model_validate(body)
    now = datetime.now(timezone.utc)
    publish_payload: dict[str, Any] | None = None
    factory = get_session_factory(request)
    with factory() as session:
        run = _get_run_or_404(session, run_id)
        terminal_non_cancelled = {RUN_STATUS_COMPLETED, RUN_STATUS_FAILED, RUN_STATUS_EXPIRED}
        if run.status in terminal_non_cancelled:
            raise HTTPException(
                status_code=409,
                detail=f"cannot cancel workflow run in terminal status {run.status!r}",
            )
        if run.status == RUN_STATUS_CANCELLED:
            data = WorkflowRunSchema.model_validate(run)
            return envelope_response(data)

        if run.status == RUN_STATUS_RUNNING:
            run.cancel_requested_at = now
            run.cancel_requested_by = actor
            run.cancel_reason = payload.reason
            _append_run_event(
                session,
                workflow_run_id=run.workflow_run_id,
                project_id=run.project_id,
                event_type="workflow.run.cancel_requested",
                payload={"requested_by": actor, "reason": payload.reason},
                node_id=run.current_node_id,
            )
            audit_record(
                session,
                actor=actor,
                action="request_cancel_workflow_run",
                entity_type="workflow_run",
                entity_id=run.workflow_run_id,
                details={"reason": payload.reason},
            )
            session.commit()
            data = WorkflowRunSchema.model_validate(run)
            publish_payload = {
                "type": "workflow.run.cancel_requested",
                "workflow_id": run.workflow_id,
                "workflow_version_id": run.workflow_version_id,
                "workflow_run_id": run.workflow_run_id,
                "status": run.status,
                "reason": payload.reason,
            }
            _emit_queue_event(request, publish_payload)
            return envelope_response(data)

        _transition_or_409(run.status, RUN_STATUS_CANCELLED)
        run.status = RUN_STATUS_CANCELLED
        run.completed_at = now
        run.cancel_requested_at = now
        run.cancel_requested_by = actor
        run.cancel_reason = payload.reason
        run.current_node_id = None
        run.claimed_by = None
        run.claimed_at = None
        run.lease_expires_at = None
        run.last_heartbeat_at = now
        run.error_code = "cancelled"
        run.error_summary = payload.reason or "cancelled by operator"
        summary = dict(run.summary or {})
        summary["status"] = RUN_STATUS_CANCELLED
        if payload.reason:
            summary["cancel_reason"] = payload.reason
        run.summary = summary

        _append_run_event(
            session,
            workflow_run_id=run.workflow_run_id,
            project_id=run.project_id,
            event_type="workflow.run.cancelled",
            payload={"cancelled_by": actor, "reason": payload.reason},
        )
        audit_record(
            session,
            actor=actor,
            action="cancel_workflow_run",
            entity_type="workflow_run",
            entity_id=run.workflow_run_id,
            details={"reason": payload.reason},
        )
        session.commit()
        data = WorkflowRunSchema.model_validate(run)
        publish_payload = {
            "type": "workflow.run.cancelled",
            "workflow_id": run.workflow_id,
            "workflow_version_id": run.workflow_version_id,
            "workflow_run_id": run.workflow_run_id,
            "status": run.status,
            "reason": payload.reason,
        }
    if publish_payload is not None:
        _emit_queue_event(request, publish_payload)
    return envelope_response(data)


async def retry_workflow_run(request: Request) -> JSONResponse:  # noqa: PLR0915
    actor = require_scopes(request, [SCOPE_OPERATOR])
    _ensure_queue_enabled(request)
    run_id = request.path_params["run_id"]
    body = await parse_json_object(request, allow_empty=True)
    payload = WorkflowRunRetryRequest.model_validate(body)
    now = datetime.now(timezone.utc)
    max_attempts = int(request.app.state.config.workflow_run_max_attempts)
    factory = get_session_factory(request)
    with factory() as session:
        run = _get_run_or_404(session, run_id)
        if run.status not in {RUN_STATUS_FAILED, RUN_STATUS_CANCELLED, RUN_STATUS_EXPIRED}:
            raise HTTPException(
                status_code=409,
                detail=f"workflow run {run.workflow_run_id} is not retryable from status {run.status!r}",
            )
        if not run.workflow_version_id:
            raise HTTPException(
                status_code=409,
                detail="cannot retry run without workflow_version_id",
            )
        attempt_number = max(1, int(run.attempt_number or 1))
        if attempt_number >= max_attempts:
            raise HTTPException(
                status_code=409,
                detail=(
                    "retry limit reached for workflow run "
                    f"(attempt {attempt_number}/{max_attempts})"
                ),
            )
        retry_checkpoint: CaliberWorkflowRunCheckpoint | None = None
        if payload.checkpoint_id:
            retry_checkpoint = session.get(CaliberWorkflowRunCheckpoint, payload.checkpoint_id)
            if retry_checkpoint is None or retry_checkpoint.workflow_run_id != run.workflow_run_id:
                raise HTTPException(
                    status_code=404,
                    detail=(
                        f"checkpoint {payload.checkpoint_id!r} not found for workflow run "
                        f"{run.workflow_run_id!r}"
                    ),
                )
        retry_input_text = run.input_payload or _summary_input(run.summary)
        retry_manifest_metadata = _copy_manifest_summary_metadata(run.summary)
        retry_manifest_snapshot = (
            _clone_manifest(run.manifest_snapshot)
            if isinstance(run.manifest_snapshot, dict)
            else None
        )
        if retry_manifest_snapshot is None:
            version = session.get(CaliberWorkflowVersion, run.workflow_version_id)
            if version is None:
                raise HTTPException(
                    status_code=409,
                    detail=(
                        "cannot retry run without a persisted manifest snapshot or workflow version"
                    ),
                )
            retry_manifest_snapshot = _clone_manifest(version.manifest)
            retry_manifest_metadata = {
                **_manifest_summary_metadata(version, None),
                **retry_manifest_metadata,
            }
        if retry_checkpoint is not None:
            checkpoint_error = _retry_checkpoint_manifest_error(
                retry_checkpoint,
                retry_manifest_snapshot,
            )
            if checkpoint_error is not None:
                raise HTTPException(status_code=409, detail=checkpoint_error)
        retry_summary = {
            "preview": False,
            "status": RUN_STATUS_QUEUED,
            "input": retry_input_text[:1000],
            "retry_of": run.workflow_run_id,
            **retry_manifest_metadata,
        }
        original_input_files = (run.summary or {}).get("input_files")
        if isinstance(original_input_files, list):
            retry_summary["input_files"] = deepcopy(original_input_files)
        if retry_checkpoint is not None:
            retry_summary["resume_checkpoint_id"] = retry_checkpoint.checkpoint_id
            retry_summary["resume_checkpoint_run_id"] = retry_checkpoint.workflow_run_id
            retry_summary["retry_mode"] = "checkpoint"
        retried = CaliberWorkflowRun(
            workflow_run_id=new_workflow_run_id(),
            workflow_id=run.workflow_id,
            project_id=run.project_id,
            tenant_id=run.tenant_id or "local",
            workflow_version_id=run.workflow_version_id,
            deployment_alias=run.deployment_alias,
            session_id=run.session_id,
            status=RUN_STATUS_QUEUED,
            source=run.source or "manual",
            priority=int(run.priority or 0),
            queued_at=now,
            started_at=None,
            attempt_number=attempt_number + 1,
            parent_run_id=run.workflow_run_id,
            input_payload=retry_input_text,
            input_file_ref=run.input_file_ref,
            manifest_snapshot=retry_manifest_snapshot,
            summary=retry_summary,
        )
        session.add(retried)
        session.flush()
        queued_manifest_mode = retry_manifest_metadata.get("manifest_mode")
        if queued_manifest_mode not in {"saved_version", "snapshot"}:
            queued_manifest_mode = None
        _append_run_event(
            session,
            workflow_run_id=retried.workflow_run_id,
            project_id=retried.project_id,
            event_type="workflow.run.queued",
            payload={
                "workflow_id": retried.workflow_id,
                "workflow_version_id": retried.workflow_version_id,
                "alias": retried.deployment_alias,
                "source": retried.source,
                "actor": actor,
                "retry_of": run.workflow_run_id,
                "reason": payload.reason,
                "checkpoint_id": retry_checkpoint.checkpoint_id if retry_checkpoint else None,
                "manifest_mode": queued_manifest_mode,
            },
        )
        _append_run_event(
            session,
            workflow_run_id=run.workflow_run_id,
            project_id=run.project_id,
            event_type="workflow.run.retried",
            payload={
                "retried_run_id": retried.workflow_run_id,
                "actor": actor,
                "checkpoint_id": retry_checkpoint.checkpoint_id if retry_checkpoint else None,
            },
        )
        audit_record(
            session,
            actor=actor,
            action="retry_workflow_run",
            entity_type="workflow_run",
            entity_id=retried.workflow_run_id,
            details={
                "retry_of": run.workflow_run_id,
                "attempt_number": retried.attempt_number,
                "reason": payload.reason,
                "checkpoint_id": retry_checkpoint.checkpoint_id if retry_checkpoint else None,
                "manifest_mode": queued_manifest_mode,
                "manifest_hash": retry_manifest_metadata.get("manifest_hash"),
            },
        )
        session.commit()
        data = WorkflowRunSchema.model_validate(retried)

    _emit_queue_event(
        request,
        {
            "type": "workflow.run.retried",
            "workflow_id": run.workflow_id,
            "workflow_version_id": run.workflow_version_id,
            "workflow_run_id": run.workflow_run_id,
            "status": run.status,
            "retried_run_id": data.workflow_run_id,
            "checkpoint_id": retry_checkpoint.checkpoint_id if retry_checkpoint else None,
        },
    )
    _emit_queue_event(
        request,
        {
            "type": "workflow.run.queued",
            "workflow_id": data.workflow_id,
            "workflow_version_id": data.workflow_version_id,
            "workflow_run_id": data.workflow_run_id,
            "status": data.status,
            "alias": data.deployment_alias,
            "retry_of": run_id,
            "checkpoint_id": retry_checkpoint.checkpoint_id if retry_checkpoint else None,
        },
    )
    return envelope_response(data, status_code=202)


def _select_pending_runtime_approval(
    session: Session,
    *,
    run: CaliberWorkflowRun,
    runtime_approval_id: str | None,
) -> CaliberRuntimeApprovalRequest:
    if runtime_approval_id:
        row = session.get(CaliberRuntimeApprovalRequest, runtime_approval_id)
        if row is None or row.workflow_run_id != run.workflow_run_id:
            raise HTTPException(
                status_code=404,
                detail=(
                    "runtime approval "
                    f"{runtime_approval_id!r} not found for workflow run {run.workflow_run_id!r}"
                ),
            )
        if row.status != "pending":
            raise HTTPException(
                status_code=409,
                detail=f"runtime approval {runtime_approval_id!r} is not pending",
            )
        return row
    stmt = (
        select(CaliberRuntimeApprovalRequest)
        .where(CaliberRuntimeApprovalRequest.workflow_run_id == run.workflow_run_id)
        .where(CaliberRuntimeApprovalRequest.status == "pending")
        .order_by(CaliberRuntimeApprovalRequest.requested_at.desc())
    )
    if run.current_node_id:
        stmt = stmt.where(CaliberRuntimeApprovalRequest.node_id == run.current_node_id)
    row = session.execute(stmt).scalars().first()
    if row is None:
        raise HTTPException(
            status_code=409,
            detail=f"workflow run {run.workflow_run_id!r} has no pending runtime approvals",
        )
    return row


async def list_workflow_run_approvals(request: Request) -> JSONResponse:
    require_user(request)
    run_id = request.path_params["run_id"]
    factory = get_session_factory(request)
    with factory() as session:
        _get_run_or_404(session, run_id)
        rows = (
            session.execute(
                select(CaliberRuntimeApprovalRequest)
                .where(CaliberRuntimeApprovalRequest.workflow_run_id == run_id)
                .order_by(CaliberRuntimeApprovalRequest.requested_at.asc())
            )
            .scalars()
            .all()
        )
        data = [WorkflowRuntimeApprovalSchema.model_validate(row) for row in rows]
    return envelope_response(data)


async def approve_workflow_run_approval(request: Request) -> JSONResponse:
    actor = require_scopes(request, [SCOPE_OPERATOR])
    _ensure_queue_enabled(request)
    _ensure_runtime_approvals_enabled(request)
    _ensure_checkpointing_enabled(request)
    run_id = request.path_params["run_id"]
    body = await parse_json_object(request, allow_empty=True)
    payload = WorkflowRunApprovalDecisionRequest.model_validate(body)
    now = datetime.now(timezone.utc)
    factory = get_session_factory(request)
    with factory() as session:
        run = _get_run_or_404(session, run_id)
        if run.status != RUN_STATUS_WAITING_APPROVAL:
            raise HTTPException(
                status_code=409,
                detail=f"workflow run {run.workflow_run_id!r} is not waiting for approval",
            )
        checkpoint = _resume_checkpoint_row(session, run)
        if checkpoint is None:
            raise HTTPException(
                status_code=409,
                detail="workflow run has no resume checkpoint",
            )
        if not isinstance(checkpoint.state_blob, dict):
            raise HTTPException(
                status_code=409,
                detail="workflow run approval checkpoint is corrupt",
            )
        if _checkpoint_node_id_missing(checkpoint):
            raise HTTPException(
                status_code=409,
                detail="workflow run approval checkpoint is missing node_id",
            )
        if _checkpoint_node_id_mismatch(run, checkpoint):
            raise HTTPException(
                status_code=409,
                detail="workflow run approval checkpoint does not match current waiting node",
            )
        if _approval_checkpoint_kind_invalid(checkpoint):
            raise HTTPException(
                status_code=409,
                detail="workflow run approval checkpoint is invalid",
            )
        if _approval_checkpoint_input_snapshot_missing(checkpoint):
            raise HTTPException(
                status_code=409,
                detail="workflow run approval checkpoint is missing its input snapshot",
            )
        approval = _select_pending_runtime_approval(
            session,
            run=run,
            runtime_approval_id=payload.runtime_approval_id,
        )
        approval_id = approval.runtime_approval_id
        approval.status = "approved"
        approval.decided_at = now
        approval.decided_by = actor
        approval.decision_reason = payload.reason
        _append_run_event(
            session,
            workflow_run_id=run.workflow_run_id,
            project_id=run.project_id,
            event_type="workflow.run.approval.approved",
            payload={
                "runtime_approval_id": approval.runtime_approval_id,
                "node_id": approval.node_id,
                "decided_by": actor,
                "reason": payload.reason,
            },
            node_id=approval.node_id,
        )
        audit_record(
            session,
            actor=actor,
            action="approve_workflow_run_runtime_approval",
            entity_type="runtime_approval",
            entity_id=approval.runtime_approval_id,
            details={"workflow_run_id": run.workflow_run_id, "reason": payload.reason},
        )
        session.commit()
        data = WorkflowRunSchema.model_validate(run)

    _emit_queue_event(
        request,
        {
            "type": "workflow.run.approval.approved",
            "workflow_id": data.workflow_id,
            "workflow_version_id": data.workflow_version_id,
            "workflow_run_id": data.workflow_run_id,
            "status": data.status,
            "runtime_approval_id": approval_id,
        },
    )
    return envelope_response(data)


async def reject_workflow_run_approval(request: Request) -> JSONResponse:
    actor = require_scopes(request, [SCOPE_OPERATOR])
    _ensure_queue_enabled(request)
    _ensure_runtime_approvals_enabled(request)
    run_id = request.path_params["run_id"]
    body = await parse_json_object(request, allow_empty=True)
    payload = WorkflowRunApprovalDecisionRequest.model_validate(body)
    now = datetime.now(timezone.utc)
    approval_id: str | None = None
    approval_node_id: str | None = None
    factory = get_session_factory(request)
    with factory() as session:
        run = _get_run_or_404(session, run_id)
        if run.status != RUN_STATUS_WAITING_APPROVAL:
            raise HTTPException(
                status_code=409,
                detail=f"workflow run {run.workflow_run_id!r} is not waiting for approval",
            )
        approval = _select_pending_runtime_approval(
            session,
            run=run,
            runtime_approval_id=payload.runtime_approval_id,
        )
        approval_id = approval.runtime_approval_id
        approval_node_id = approval.node_id
        approval.status = "rejected"
        approval.decided_at = now
        approval.decided_by = actor
        approval.decision_reason = payload.reason

        _transition_or_409(run.status, RUN_STATUS_FAILED)
        run.status = RUN_STATUS_FAILED
        run.completed_at = now
        run.current_node_id = None
        run.claimed_by = None
        run.claimed_at = None
        run.lease_expires_at = None
        run.last_heartbeat_at = now
        run.error_code = "approval_rejected"
        run.error_summary = payload.reason or "runtime approval rejected"
        summary = dict(run.summary or {})
        summary["status"] = RUN_STATUS_FAILED
        summary["error"] = run.error_summary
        run.summary = summary

        _append_run_event(
            session,
            workflow_run_id=run.workflow_run_id,
            project_id=run.project_id,
            event_type="workflow.run.approval.rejected",
            payload={
                "runtime_approval_id": approval.runtime_approval_id,
                "node_id": approval.node_id,
                "decided_by": actor,
                "reason": payload.reason,
            },
            node_id=approval.node_id,
        )
        _append_run_event(
            session,
            workflow_run_id=run.workflow_run_id,
            project_id=run.project_id,
            event_type="workflow.run.failed",
            payload={
                "status": run.status,
                "error": run.error_summary,
                "runtime_approval_id": approval.runtime_approval_id,
            },
        )
        audit_record(
            session,
            actor=actor,
            action="reject_workflow_run_runtime_approval",
            entity_type="runtime_approval",
            entity_id=approval.runtime_approval_id,
            details={"workflow_run_id": run.workflow_run_id, "reason": payload.reason},
        )
        session.commit()
        data = WorkflowRunSchema.model_validate(run)

    _emit_queue_event(
        request,
        {
            "type": "workflow.run.approval.rejected",
            "workflow_id": data.workflow_id,
            "workflow_version_id": data.workflow_version_id,
            "workflow_run_id": data.workflow_run_id,
            "status": data.status,
            "runtime_approval_id": approval_id,
            "node_id": approval_node_id,
            "reason": payload.reason,
        },
    )
    _emit_queue_event(
        request,
        {
            "type": "workflow.run.failed",
            "workflow_id": data.workflow_id,
            "workflow_version_id": data.workflow_version_id,
            "workflow_run_id": data.workflow_run_id,
            "status": data.status,
            "error": data.error_summary,
        },
    )
    return envelope_response(data)


def _resume_checkpoint_row(
    session: Session,
    run: CaliberWorkflowRun,
) -> CaliberWorkflowRunCheckpoint | None:
    summary = run.summary if isinstance(run.summary, dict) else {}
    checkpoint_id = summary.get("resume_checkpoint_id")
    if not isinstance(checkpoint_id, str) or not checkpoint_id:
        return None
    checkpoint = session.get(CaliberWorkflowRunCheckpoint, checkpoint_id)
    if checkpoint is None or checkpoint.workflow_run_id != run.workflow_run_id:
        return None
    return checkpoint


def _resume_event_inputs(
    *,
    expected_event_name: str | None,
    event_name: str | None,
    event_payload: object,
) -> dict[str, Any]:
    event_name = event_name or expected_event_name
    payload: object = event_payload
    if payload is None:
        payload_dict: dict[str, Any] = {"manual_resume": True}
        if event_name:
            payload_dict["event_name"] = event_name
        payload = payload_dict
    injected = {
        "resume_event": payload,
        "event": payload,
        "event_payload": payload,
    }
    if event_name:
        injected["event_name"] = event_name
        injected[event_name] = payload
    return injected


def _store_resume_event_inputs(
    checkpoint: CaliberWorkflowRunCheckpoint,
    *,
    event_name: str | None,
    event_payload: object,
) -> None:
    if not isinstance(checkpoint.state_blob, dict):
        return
    state_blob = dict(checkpoint.state_blob)
    kind = state_blob.get("kind")
    if kind not in {"wait_for_event", "wait_until"}:
        return
    expected_event_name = None
    if kind == "wait_for_event":
        expected_event_name = state_blob.get("expected_event_name")
        if not isinstance(expected_event_name, str):
            expected_event_name = None
    state_blob["resume_event_inputs"] = _resume_event_inputs(
        expected_event_name=expected_event_name,
        event_name=event_name,
        event_payload=event_payload,
    )
    checkpoint.state_blob = state_blob


def _approval_checkpoint_input_snapshot_missing(checkpoint: CaliberWorkflowRunCheckpoint) -> bool:
    state_blob = checkpoint.state_blob if isinstance(checkpoint.state_blob, dict) else None
    if state_blob is None:
        return False
    kind = state_blob.get("kind")
    return kind in {"human_approval", "runtime_approval"} and not isinstance(
        state_blob.get("input_by_port"), dict
    )


def _approval_checkpoint_kind_invalid(checkpoint: CaliberWorkflowRunCheckpoint) -> bool:
    state_blob = checkpoint.state_blob if isinstance(checkpoint.state_blob, dict) else None
    if state_blob is None:
        return False
    return state_blob.get("kind") not in {"human_approval", "runtime_approval"}


def _checkpoint_node_id_missing(checkpoint: CaliberWorkflowRunCheckpoint) -> bool:
    state_blob = checkpoint.state_blob if isinstance(checkpoint.state_blob, dict) else None
    if state_blob is None:
        return False
    node_id = state_blob.get("node_id")
    return not isinstance(node_id, str) or not node_id


def _checkpoint_node_id_mismatch(
    run: CaliberWorkflowRun,
    checkpoint: CaliberWorkflowRunCheckpoint,
) -> bool:
    state_blob = checkpoint.state_blob if isinstance(checkpoint.state_blob, dict) else None
    if state_blob is None:
        return False
    current_node_id = run.current_node_id
    checkpoint_node_id = checkpoint.node_id
    state_node_id = state_blob.get("node_id")
    if not isinstance(current_node_id, str) or not current_node_id:
        return True
    if not isinstance(checkpoint_node_id, str) or not checkpoint_node_id:
        return True
    if not isinstance(state_node_id, str) or not state_node_id:
        return True
    return (
        checkpoint_node_id != current_node_id
        or state_node_id != current_node_id
        or checkpoint_node_id != state_node_id
    )


def _waiting_event_checkpoint_input_snapshot_missing(
    checkpoint: CaliberWorkflowRunCheckpoint,
) -> bool:
    state_blob = checkpoint.state_blob if isinstance(checkpoint.state_blob, dict) else None
    if state_blob is None:
        return False
    kind = state_blob.get("kind")
    return kind in {"wait_for_event", "wait_until"} and not isinstance(
        state_blob.get("input_by_port"), dict
    )


def _waiting_event_checkpoint_kind_invalid(checkpoint: CaliberWorkflowRunCheckpoint) -> bool:
    state_blob = checkpoint.state_blob if isinstance(checkpoint.state_blob, dict) else None
    if state_blob is None:
        return False
    return state_blob.get("kind") not in {"wait_for_event", "wait_until", "wait_event"}


def _waiting_event_checkpoint_expected_event_missing(
    checkpoint: CaliberWorkflowRunCheckpoint,
) -> bool:
    state_blob = checkpoint.state_blob if isinstance(checkpoint.state_blob, dict) else None
    if state_blob is None:
        return False
    if state_blob.get("kind") != "wait_for_event":
        return False
    expected_event_name = state_blob.get("expected_event_name")
    return not isinstance(expected_event_name, str) or not expected_event_name.strip()


def _waiting_event_checkpoint_correlation_value_missing_for_event_match(
    checkpoint: CaliberWorkflowRunCheckpoint,
) -> bool:
    state_blob = checkpoint.state_blob if isinstance(checkpoint.state_blob, dict) else None
    if state_blob is None:
        return False
    if state_blob.get("kind") != "wait_for_event":
        return False
    correlation_key = state_blob.get("correlation_key")
    if not (isinstance(correlation_key, str) and correlation_key.strip()):
        return False
    correlation_value = state_blob.get("correlation_value")
    return correlation_value in (None, "")


def _waiting_event_checkpoint_uses_legacy_event_match_shape(
    checkpoint: CaliberWorkflowRunCheckpoint,
) -> bool:
    state_blob = checkpoint.state_blob if isinstance(checkpoint.state_blob, dict) else None
    if state_blob is None:
        return False
    return state_blob.get("kind") == "wait_event"


def _requeue_resumed_run(
    session: Session,
    *,
    run: CaliberWorkflowRun,
    actor: str,
    event_name: str | None,
    event_payload_supplied: bool,
    extra_details: dict[str, Any] | None = None,
    node_id: str | None = None,
) -> WorkflowRunSchema:
    now = datetime.now(timezone.utc)
    prior_queued_at = run.queued_at
    from_status = run.status
    _transition_or_409(from_status, RUN_STATUS_QUEUED)
    run.status = RUN_STATUS_QUEUED
    # Preserve the run's original queue position so a resumed gate can continue
    # ahead of newer sibling runs instead of being pushed to the back.
    run.queued_at = prior_queued_at or now
    run.claimed_by = None
    run.claimed_at = None
    run.lease_expires_at = None
    run.last_heartbeat_at = now
    run.current_node_id = None
    run.error_code = None
    run.error_summary = None
    summary = dict(run.summary or {})
    summary["status"] = RUN_STATUS_QUEUED
    run.summary = summary
    event_payload: dict[str, Any] = {
        "actor": actor,
        "event_name": event_name,
        "event_payload_supplied": event_payload_supplied,
    }
    if extra_details:
        event_payload.update(extra_details)
    _append_run_event(
        session,
        workflow_run_id=run.workflow_run_id,
        project_id=run.project_id,
        event_type="workflow.run.resumed",
        payload=event_payload,
        node_id=node_id,
    )
    audit_details = {"from_status": from_status}
    if extra_details:
        audit_details.update(extra_details)
    audit_record(
        session,
        actor=actor,
        action="resume_workflow_run",
        entity_type="workflow_run",
        entity_id=run.workflow_run_id,
        details=audit_details,
    )
    session.commit()
    return WorkflowRunSchema.model_validate(run)


def _match_waiting_event_run_for_external_resume(
    *,
    run: CaliberWorkflowRun,
    checkpoint: CaliberWorkflowRunCheckpoint,
    event_name: str,
    event_payload: object,
    workflow_id: str | None,
) -> str | None:
    if workflow_id and run.workflow_id != workflow_id:
        return None
    state_blob = checkpoint.state_blob if isinstance(checkpoint.state_blob, dict) else None
    if state_blob is None:
        return None
    if state_blob.get("kind") != "wait_for_event":
        return None
    expected_event_name = state_blob.get("expected_event_name")
    if (
        isinstance(expected_event_name, str)
        and expected_event_name.strip()
        and expected_event_name != event_name
    ):
        return None
    correlation_key = state_blob.get("correlation_key")
    if not (isinstance(correlation_key, str) and correlation_key.strip()):
        return "event_name"
    correlation_value = state_blob.get("correlation_value")
    payload_matches = (
        isinstance(event_payload, dict)
        and correlation_value not in (None, "")
        and event_payload.get(correlation_key) == correlation_value
    )
    return "correlation_key" if payload_matches else None


async def resume_workflow_run(request: Request) -> JSONResponse:  # noqa: PLR0912, PLR0915
    actor = require_scopes(request, [SCOPE_OPERATOR])
    _ensure_queue_enabled(request)
    _ensure_checkpointing_enabled(request)
    run_id = request.path_params["run_id"]
    body = await parse_json_object(request, allow_empty=True)
    payload = WorkflowRunResumeRequest.model_validate(body)
    factory = get_session_factory(request)
    with factory() as session:
        run = _get_run_or_404(session, run_id)
        if run.status not in {RUN_STATUS_WAITING_APPROVAL, RUN_STATUS_WAITING_EVENT}:
            raise HTTPException(
                status_code=409,
                detail=f"workflow run {run.workflow_run_id!r} is not resumable from {run.status!r}",
            )
        checkpoint = _resume_checkpoint_row(session, run)
        if checkpoint is None:
            raise HTTPException(
                status_code=409,
                detail="workflow run has no resume checkpoint",
            )
        if run.status == RUN_STATUS_WAITING_APPROVAL:
            node_id = run.current_node_id
            filters = [CaliberRuntimeApprovalRequest.workflow_run_id == run.workflow_run_id]
            if node_id:
                filters.append(CaliberRuntimeApprovalRequest.node_id == node_id)
            pending = (
                session.execute(
                    select(CaliberRuntimeApprovalRequest.runtime_approval_id)
                    .where(*filters)
                    .where(CaliberRuntimeApprovalRequest.status == "pending")
                    .limit(1)
                )
                .scalars()
                .first()
            )
            if pending is not None:
                raise HTTPException(
                    status_code=409,
                    detail=(
                        "workflow run cannot resume while runtime approval decision is pending"
                    ),
                )
            rejected = (
                session.execute(
                    select(CaliberRuntimeApprovalRequest.runtime_approval_id)
                    .where(*filters)
                    .where(CaliberRuntimeApprovalRequest.status == "rejected")
                    .limit(1)
                )
                .scalars()
                .first()
            )
            if rejected is not None:
                raise HTTPException(
                    status_code=409,
                    detail="workflow run cannot resume after runtime approval rejection",
                )
            approved = (
                session.execute(
                    select(CaliberRuntimeApprovalRequest.runtime_approval_id)
                    .where(*filters)
                    .where(CaliberRuntimeApprovalRequest.status == "approved")
                    .limit(1)
                )
                .scalars()
                .first()
            )
            if approved is None:
                raise HTTPException(
                    status_code=409,
                    detail="workflow run has no approved runtime approval decision to resume from",
                )
            if not isinstance(checkpoint.state_blob, dict):
                raise HTTPException(
                    status_code=409,
                    detail="workflow run approval checkpoint is corrupt",
                )
            if _checkpoint_node_id_missing(checkpoint):
                raise HTTPException(
                    status_code=409,
                    detail="workflow run approval checkpoint is missing node_id",
                )
            if _checkpoint_node_id_mismatch(run, checkpoint):
                raise HTTPException(
                    status_code=409,
                    detail="workflow run approval checkpoint does not match current waiting node",
                )
            if _approval_checkpoint_kind_invalid(checkpoint):
                raise HTTPException(
                    status_code=409,
                    detail="workflow run approval checkpoint is invalid",
                )
            if _approval_checkpoint_input_snapshot_missing(checkpoint):
                raise HTTPException(
                    status_code=409,
                    detail="workflow run approval checkpoint is missing its input snapshot",
                )
        if run.status == RUN_STATUS_WAITING_EVENT and isinstance(checkpoint.state_blob, dict):
            if _checkpoint_node_id_missing(checkpoint):
                raise HTTPException(
                    status_code=409,
                    detail="workflow run resume checkpoint is missing node_id",
                )
            if _checkpoint_node_id_mismatch(run, checkpoint):
                raise HTTPException(
                    status_code=409,
                    detail="workflow run resume checkpoint does not match current waiting node",
                )
            if _waiting_event_checkpoint_kind_invalid(checkpoint):
                raise HTTPException(
                    status_code=409,
                    detail="workflow run resume checkpoint is invalid",
                )
            if _waiting_event_checkpoint_input_snapshot_missing(checkpoint):
                raise HTTPException(
                    status_code=409,
                    detail="workflow run resume checkpoint is missing its input snapshot",
                )
            if _waiting_event_checkpoint_expected_event_missing(checkpoint):
                raise HTTPException(
                    status_code=409,
                    detail="workflow run resume checkpoint is missing its expected event name",
                )
            state_blob = dict(checkpoint.state_blob)
            expected_event_name = state_blob.get("expected_event_name")
            if not isinstance(expected_event_name, str):
                expected_event_name = None
            if (
                payload.event_name
                and expected_event_name
                and payload.event_name != expected_event_name
            ):
                raise HTTPException(
                    status_code=409,
                    detail=(
                        f"resume event_name {payload.event_name!r} does not match the workflow node's "
                        f"configured event {expected_event_name!r}"
                    ),
                )
            _store_resume_event_inputs(
                checkpoint,
                event_name=payload.event_name,
                event_payload=payload.event_payload,
            )
        elif run.status == RUN_STATUS_WAITING_EVENT:
            raise HTTPException(
                status_code=409,
                detail="workflow run resume checkpoint is corrupt",
            )

        data = _requeue_resumed_run(
            session,
            run=run,
            actor=actor,
            event_name=payload.event_name,
            event_payload_supplied=payload.event_payload is not None,
            node_id=checkpoint.node_id,
        )

    _emit_queue_event(
        request,
        {
            "type": "workflow.run.resumed",
            "workflow_id": data.workflow_id,
            "workflow_version_id": data.workflow_version_id,
            "workflow_run_id": data.workflow_run_id,
            "status": data.status,
        },
    )
    return envelope_response(data, status_code=202)


async def resume_workflow_run_by_event(  # noqa: PLR0912, PLR0915
    request: Request,
) -> JSONResponse:
    actor = require_scopes(request, [SCOPE_OPERATOR])
    _ensure_queue_enabled(request)
    _ensure_checkpointing_enabled(request)
    body = await parse_json_object(request)
    payload = WorkflowRunResumeByEventRequest.model_validate(body)
    factory = get_session_factory(request)
    with factory() as session:
        rows = (
            session.execute(
                select(CaliberWorkflowRun)
                .where(CaliberWorkflowRun.status == RUN_STATUS_WAITING_EVENT)
                .order_by(CaliberWorkflowRun.queued_at.asc())
            )
            .scalars()
            .all()
        )
        matches: list[tuple[CaliberWorkflowRun, CaliberWorkflowRunCheckpoint, str]] = []
        corrupt_candidates: list[str] = []
        missing_correlation_candidates: list[str] = []
        legacy_wait_event_candidates: list[str] = []
        for run in rows:
            checkpoint = _resume_checkpoint_row(session, run)
            if checkpoint is None:
                continue
            if payload.workflow_id and run.workflow_id != payload.workflow_id:
                continue
            if not isinstance(checkpoint.state_blob, dict):
                corrupt_candidates.append(run.workflow_run_id)
                continue
            if _checkpoint_node_id_missing(checkpoint):
                corrupt_candidates.append(run.workflow_run_id)
                continue
            if _checkpoint_node_id_mismatch(run, checkpoint):
                corrupt_candidates.append(run.workflow_run_id)
                continue
            if _waiting_event_checkpoint_kind_invalid(checkpoint):
                corrupt_candidates.append(run.workflow_run_id)
                continue
            if _waiting_event_checkpoint_input_snapshot_missing(checkpoint):
                corrupt_candidates.append(run.workflow_run_id)
                continue
            if _waiting_event_checkpoint_expected_event_missing(checkpoint):
                corrupt_candidates.append(run.workflow_run_id)
                continue
            if _waiting_event_checkpoint_correlation_value_missing_for_event_match(checkpoint):
                missing_correlation_candidates.append(run.workflow_run_id)
                continue
            if _waiting_event_checkpoint_uses_legacy_event_match_shape(checkpoint):
                legacy_wait_event_candidates.append(run.workflow_run_id)
                continue
            match_mode = _match_waiting_event_run_for_external_resume(
                run=run,
                checkpoint=checkpoint,
                event_name=payload.event_name,
                event_payload=payload.event_payload,
                workflow_id=payload.workflow_id,
            )
            if match_mode:
                matches.append((run, checkpoint, match_mode))
        if not matches:
            if corrupt_candidates:
                candidate_run_ids = corrupt_candidates[:5]
                raise HTTPException(
                    status_code=409,
                    detail=(
                        f"event {payload.event_name!r} reached waiting workflow runs with corrupt "
                        f"resume checkpoints: {', '.join(candidate_run_ids)}"
                    ),
                )
            if missing_correlation_candidates:
                candidate_run_ids = missing_correlation_candidates[:5]
                raise HTTPException(
                    status_code=409,
                    detail=(
                        f"event {payload.event_name!r} reached waiting workflow runs with "
                        "resume checkpoints missing correlation_value for their configured "
                        f"correlation_key: {', '.join(candidate_run_ids)}"
                    ),
                )
            if legacy_wait_event_candidates:
                candidate_run_ids = legacy_wait_event_candidates[:5]
                raise HTTPException(
                    status_code=409,
                    detail=(
                        f"event {payload.event_name!r} reached waiting workflow runs using "
                        "legacy wait_event checkpoints that cannot be targeted by workflow-wide "
                        f"event matching: {', '.join(candidate_run_ids)}"
                    ),
                )
            raise HTTPException(
                status_code=404,
                detail=(
                    f"no waiting workflow run matched event {payload.event_name!r}"
                    + (f" for workflow {payload.workflow_id!r}" if payload.workflow_id else "")
                ),
            )
        correlation_matches = [
            (run, checkpoint, mode)
            for run, checkpoint, mode in matches
            if mode == "correlation_key"
        ]
        if correlation_matches:
            matches = correlation_matches
        if len(matches) > 1:
            candidate_run_ids = [run.workflow_run_id for run, _checkpoint, _mode in matches[:5]]
            raise HTTPException(
                status_code=409,
                detail=(
                    f"event {payload.event_name!r} matched multiple waiting workflow runs: "
                    + ", ".join(candidate_run_ids)
                    + ". Provide a correlation_key on the wait_for_event node, pass workflow_id, "
                    "or resume the target run directly by run_id."
                ),
            )
        run, checkpoint, match_mode = matches[0]
        _store_resume_event_inputs(
            checkpoint,
            event_name=payload.event_name,
            event_payload=payload.event_payload,
        )
        data = _requeue_resumed_run(
            session,
            run=run,
            actor=actor,
            event_name=payload.event_name,
            event_payload_supplied=payload.event_payload is not None,
            extra_details={
                "resume_source": "external_event",
                "matched_via": match_mode,
            },
            node_id=checkpoint.node_id,
        )

    _emit_queue_event(
        request,
        {
            "type": "workflow.run.resumed",
            "workflow_id": data.workflow_id,
            "workflow_version_id": data.workflow_version_id,
            "workflow_run_id": data.workflow_run_id,
            "status": data.status,
        },
    )
    return envelope_response(data, status_code=202)


def register(app: Starlette) -> None:
    app.routes.append(Route(CREATE_PATH, create_workflow_run, methods=["POST"]))
    app.routes.append(Route(DETAIL_PATH, get_workflow_run, methods=["GET"]))
    app.routes.append(Route(LINEAGE_PATH, get_workflow_run_lineage, methods=["GET"]))
    app.routes.append(Route(MANIFEST_PATH, get_workflow_run_manifest, methods=["GET"]))
    app.routes.append(Route(EVENTS_PATH, list_workflow_run_events, methods=["GET"]))
    app.routes.append(Route(TRACE_PATH, get_workflow_run_trace, methods=["GET"]))
    app.routes.append(Route(CHECKPOINTS_PATH, list_workflow_run_checkpoints, methods=["GET"]))
    app.routes.append(Route(CANCEL_PATH, cancel_workflow_run, methods=["POST"]))
    app.routes.append(Route(RETRY_PATH, retry_workflow_run, methods=["POST"]))
    app.routes.append(Route(APPROVALS_PATH, list_workflow_run_approvals, methods=["GET"]))
    app.routes.append(Route(APPROVE_PATH, approve_workflow_run_approval, methods=["POST"]))
    app.routes.append(Route(REJECT_PATH, reject_workflow_run_approval, methods=["POST"]))
    app.routes.append(Route(RESUME_PATH, resume_workflow_run, methods=["POST"]))
    app.routes.append(Route(RESUME_BY_EVENT_PATH, resume_workflow_run_by_event, methods=["POST"]))
    app.routes.append(Route(TRIGGER_PATH, trigger_workflow_event, methods=["POST"]))
