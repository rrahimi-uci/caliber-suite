"""Async workflow-run routes (queue-based submit + lifecycle reads)."""

from __future__ import annotations

import json
import logging
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from copy import deepcopy
from datetime import datetime, timezone
from typing import Any, Literal, cast

from sqlalchemy import func, select, text
from sqlalchemy.exc import IntegrityError, OperationalError
from sqlalchemy.orm import Session
from starlette.applications import Starlette
from starlette.exceptions import HTTPException
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from caliber.audit import record as audit_record
from caliber.auth import SCOPE_OPERATOR, current_scopes, require_scopes, require_user
from caliber.db.models import (
    CaliberProject,
    CaliberRuntimeApprovalRequest,
    CaliberWorkflow,
    CaliberWorkflowDeployment,
    CaliberWorkflowRun,
    CaliberWorkflowRunCheckpoint,
    CaliberWorkflowRunEvent,
    CaliberWorkflowVersion,
    CaliberWorkspaceRelease,
    CaliberWorkspaceRuntimeLineage,
)
from caliber.ids import new_workflow_run_id
from caliber.mcp_policy import deployment_blockers
from caliber.observability import causal_links
from caliber.observability.trace_access import can_read_run_payloads, redact_trace_detail
from caliber.profile import payload_governance_active
from caliber.resource_access import ACTION_APPROVE, require_project_access_if_scoped
from caliber.routes._deps import (
    envelope_response,
    get_session_factory,
    parse_json_object,
    scoped_child_or_404,
)
from caliber.routes._errors import CaliberHTTPException
from caliber.routes._job_access import LOCK_TIMEOUT_MS, RETRY_AFTER_SECONDS, is_lock_contention
from caliber.run_manifest import canonical_bytes, digest_of
from caliber.schemas import (
    AdmissionManifestSchema,
    CausalLinksSchema,
    RuntimeApprovalAckSchema,
    WorkflowApprovalViewSchema,
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
from caliber.workflows.admission import (
    OVERRIDE_NOT_ADMITTED,
    SOURCE_NOT_APPROVED,
    AdmissionDenied,
    AdmissionSettings,
    admitted_manifest_metadata,
    check_retry_intent,
    intent_of,
    persist_admission,
    prepare_admission,
    record_denial,
)
from caliber.workflows.approval_policy import (
    REFUSAL_ALREADY_CONSUMED,
    REFUSAL_EXPIRED,
    REFUSAL_SUPERSEDED,
    ApprovalPolicy,
    ApprovalPolicyError,
    approval_refusal,
    assert_distinct_actor,
    record_approval,
)
from caliber.workflows.approval_view import build_approval_view
from caliber.workflows.manifest import (
    StartNode,
    StartTrigger,
    WorkflowManifestError,
    compute_manifest_hash,
    parse_manifest,
)
from caliber.workflows.run_events import append_run_event
from caliber.workflows.run_launch import (
    LINEAGE_REQUIRED_MESSAGE,
    LineageRequiredError,
    enqueue_workflow_run,
    require_lineage,
)
from caliber.workflows.run_pins import PinsError, load_run_manifest
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
from caliber.workspace_runtime_lineage import create_runtime_lineage

PREFIX = "/ajax-api/2.0/mlflow/caliber"
CREATE_PATH = PREFIX + "/workflow-runs"
DETAIL_PATH = PREFIX + "/workflow-runs/{run_id}"
LINEAGE_PATH = DETAIL_PATH + "/lineage"
MANIFEST_PATH = DETAIL_PATH + "/manifest"
ADMISSION_MANIFEST_PATH = DETAIL_PATH + "/admission-manifest"
CAUSAL_LINKS_PATH = DETAIL_PATH + "/causal-links"
EVENTS_PATH = DETAIL_PATH + "/events"
TRACE_PATH = DETAIL_PATH + "/trace"
CHECKPOINTS_PATH = DETAIL_PATH + "/checkpoints"
CANCEL_PATH = DETAIL_PATH + "/cancel"
RETRY_PATH = DETAIL_PATH + "/retry"
APPROVALS_PATH = DETAIL_PATH + "/approvals"
APPROVAL_VIEW_PATH = APPROVALS_PATH + "/{approval_id}"
APPROVE_PATH = DETAIL_PATH + "/approval/approve"
REJECT_PATH = DETAIL_PATH + "/approval/reject"
RESUME_PATH = DETAIL_PATH + "/resume"
RESUME_BY_EVENT_PATH = PREFIX + "/workflow-runs/resume-by-event"
TRIGGER_PATH = PREFIX + "/workflows/{workflow_id}/trigger"
logger = logging.getLogger("caliber.routes.workflow_runs")


def _visible_workflow_for_run(session: Session, request: Request, workflow_id: str) -> Any:
    """The caller's view of a workflow, or ``None``.

    Every workflow read reached from a run-creation or trigger path goes through here. They
    were bare primary-key lookups, so a caller could queue a run against another project's
    workflow — an independent probe observed **202 Accepted**, which is worse than a read:
    it executes someone else's graph and bills their providers.

    A forbidden parent 404s exactly like a missing one, so "exists but you may not see it"
    is never disclosed.
    """
    from caliber.auth import resolve_identity  # noqa: PLC0415 - avoids an import cycle
    from caliber.db.scoping import get_visible  # noqa: PLC0415

    if not workflow_id:
        return None
    return get_visible(
        session,
        CaliberWorkflow,
        CaliberWorkflow.workflow_id,
        workflow_id,
        resolve_identity(request),
    )


def _workflow_and_version_for_run(
    session: Session, payload: WorkflowRunCreateRequest, *, request: Request
) -> tuple[CaliberWorkflow, CaliberWorkflowVersion, str]:
    if payload.workflow_version_id:
        version = session.get(CaliberWorkflowVersion, payload.workflow_version_id)
        if version is None:
            raise HTTPException(
                status_code=404,
                detail=f"workflow version {payload.workflow_version_id!r} not found",
            )
        workflow = _visible_workflow_for_run(session, request, version.workflow_id)
        if workflow is None:
            # Do not reveal the foreign version's parent workflow. A caller supplied the
            # version id, but not its parent; naming ``version.workflow_id`` here both
            # confirms that the version exists and hands out another protected id. Keep a
            # forbidden version indistinguishable from a missing one.
            raise HTTPException(
                status_code=404,
                detail=f"workflow version {payload.workflow_version_id!r} not found",
            )
        if payload.workflow_id and payload.workflow_id != workflow.workflow_id:
            raise HTTPException(
                status_code=400,
                detail="workflow_id does not match workflow_version_id",
            )
        return workflow, version, payload.alias or "manual"

    workflow_id = payload.workflow_id or ""
    alias = payload.alias or "manual"
    workflow_row = _visible_workflow_for_run(session, request, workflow_id)
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
    return append_run_event(
        session,
        workflow_run_id=workflow_run_id,
        project_id=project_id,
        event_type=event_type,
        payload=payload,
        node_id=node_id,
    )


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


def _admission_settings(request: Request) -> AdmissionSettings:
    """The deployment's admission settings, read from the running config (``enabled`` is false unless the reference
    profile or ``CALIBER_ADMISSION_ENABLED`` says so)."""
    return AdmissionSettings.from_config(request.app.state.config)


def _refuse_admission(session: Session, exc: AdmissionDenied) -> HTTPException:
    """Roll the whole transaction back (the run, its grant and its decision are one unit) and answer the refusal."""
    session.rollback()
    record_denial(
        session, exc
    )  # the decision's own rows went with the rollback; its audit row is written now
    if exc.code == SOURCE_NOT_APPROVED:
        # machine-readable (``reason_code`` in the standard error body), not only in the message
        return CaliberHTTPException(exc.status, str(exc), reason_code=SOURCE_NOT_APPROVED)
    return HTTPException(status_code=exc.status, detail=str(exc))


def _lineage_required_response(exc: LineageRequiredError) -> JSONResponse:
    """``422 {"error": "lineage_required"}``: the reference profile starts no run without runtime lineage."""
    return JSONResponse(
        {"error": "lineage_required", "detail": str(exc), "status_code": 422}, status_code=422
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


def _get_run_or_404(session: Session, run_id: str, *, request: Request) -> CaliberWorkflowRun:
    """Fetch a run, but only if its parent workflow is visible to the caller (C3).

    This was a bare ``session.get``, which is the C3 defect: a run carries no visibility
    columns of its own — its access rules live on the parent workflow — so a guessed id
    read another project's run detail, trace, events, checkpoints, and approvals, and the
    write routes let a caller cancel, retry, resume, approve, or reject it. The list
    sibling enforced a predicate this lookup skipped entirely.

    ``request`` is keyword-only and required rather than defaulted to ``None``: an optional
    scope argument is one a caller forgets, and the forgotten case would fail open. Every
    caller is a route handler, so ``request`` is always genuinely available.

    A forbidden parent 404s identically to a missing one, so the message is unchanged and
    "exists but you may not see it" is not disclosed.
    """
    row = scoped_child_or_404(
        session,
        request,
        child=CaliberWorkflowRun,
        child_id=run_id,
        child_label="workflow run",
        parent_model=CaliberWorkflow,
        parent_pk=CaliberWorkflow.workflow_id,
    )
    return cast("CaliberWorkflowRun", row)


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


def _canonical_input(value: object) -> str | None:
    """The canonical JSON of an input that is not text, or ``None`` (text, or no input): what makes two spellings of
    one object the same request and an object different from the string that happens to print like it."""
    if value is None or isinstance(value, str):
        return None
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str)


def _replay_idempotent(
    session: Session,
    request: Request,
    *,
    workflow_id: str,
    source: str,
    idempotency_key: str | None,
    admission: AdmissionSettings,
    intent_for: Callable[[CaliberWorkflow], dict[str, Any]],
) -> CaliberWorkflowRun | None:
    """The run an idempotency key already names, answered *before* the target is resolved.

    A retry of the same request returns the original run even when what it would resolve to has since changed (an
    alias moved, was deactivated, now fails a preflight, or the workflow was paused): the original is what it asked
    for. The caller is authorized first (the workflow must be visible to them and they must be allowed to run it), and
    a different request under the key is a ``409``. ``None`` when nothing is named or the workflow is not visible, so
    the normal path decides.
    """
    from caliber.auth import resolve_identity  # noqa: PLC0415 - avoids an import cycle

    if not idempotency_key or not admission.enabled:
        return None
    workflow = _visible_workflow_for_run(session, request, workflow_id)
    if workflow is None:
        return None
    require_project_access_if_scoped(
        session, resolve_identity(request), workflow.project_id, "resource.execute"
    )
    existing = _find_idempotent(
        session, workflow_id=workflow.workflow_id, source=source, idempotency_key=idempotency_key
    )
    if existing is None:
        return None
    try:
        check_retry_intent(session, existing, intent_for(workflow))
    except AdmissionDenied as exc:
        raise _refuse_admission(session, exc) from exc
    return existing


def _create_intent(
    payload: WorkflowRunCreateRequest, workflow: CaliberWorkflow, actor: str
) -> dict[str, Any]:
    """The canonical intent of a create-run request: the selectors the caller named, never what they resolved to."""
    return intent_of(
        workflow_id=workflow.workflow_id,
        alias=payload.alias or "manual",
        actor=actor,
        input_text=_input_to_text(payload.input),
        project_id=workflow.project_id,
        input_files=[dict(item) for item in payload.input_files] if payload.input_files else None,
        version_selector=payload.workflow_version_id,
        workspace_release_id=payload.workspace_release_id,
        environment_id=payload.environment_id,
        model_id=payload.model_id,
        config_sha256=payload.config_sha256,
        priority=payload.priority,
        session_id=payload.session_id,
        source="api",  # the route's own word, never the client-chosen ``source`` field
        input_json=_canonical_input(payload.input),
    )


async def create_workflow_run(request: Request) -> JSONResponse:  # noqa: PLR0912, PLR0915
    from caliber.auth import resolve_identity  # noqa: PLC0415 - avoids an import cycle

    actor = require_scopes(request, [SCOPE_OPERATOR])
    _ensure_queue_enabled(request)
    identity = resolve_identity(request)
    body = await parse_json_object(request)
    payload = WorkflowRunCreateRequest.model_validate(body)
    if _admission_settings(request).lineage_required:
        try:
            require_lineage(payload.workspace_release_id, payload.environment_id)
        except LineageRequiredError as exc:
            return _lineage_required_response(exc)
    factory = get_session_factory(request)
    with factory() as session:
        replayed_version = (
            session.get(CaliberWorkflowVersion, payload.workflow_version_id)
            if payload.workflow_version_id
            else None
        )
        replayed = _replay_idempotent(
            session,
            request,
            workflow_id=replayed_version.workflow_id
            if replayed_version is not None
            else payload.workflow_id or "",
            source=payload.source,
            idempotency_key=payload.idempotency_key,
            admission=_admission_settings(request),
            intent_for=lambda workflow: _create_intent(payload, workflow, actor),
        )
        if replayed is not None:
            return envelope_response(WorkflowRunSchema.model_validate(replayed), status_code=202)
        workflow, version, alias = _workflow_and_version_for_run(session, payload, request=request)
        # `P1-D`: `resource.execute` -- a no-op when the workflow is unscoped
        # (`project_id is None`, a genuine personal/global workflow), a real
        # project-role check otherwise. `_visible_workflow_for_run` above
        # already applies the *visibility* filter; this is the separate
        # project-membership-role axis section 2.4 names.
        require_project_access_if_scoped(session, identity, workflow.project_id, "resource.execute")
        admission = _admission_settings(request)
        submitted_manifest = payload.manifest
        if submitted_manifest is not None and admission.enabled:
            raise HTTPException(
                status_code=409,
                detail=(
                    f"{OVERRIDE_NOT_ADMITTED}: a manifest override cannot be pinned, so it is not admitted; "
                    "save it as a version, publish it and run that"
                ),
            )
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
        mcp_blockers = deployment_blockers(
            session,
            manifest_snapshot,
            alias=alias,
            identity=identity,
        )
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

        intent = _create_intent(payload, workflow, actor)
        existing = _find_idempotent(
            session,
            workflow_id=workflow.workflow_id,
            source=payload.source,
            idempotency_key=payload.idempotency_key,
        )
        if existing is not None:
            if admission.enabled:
                try:
                    check_retry_intent(session, existing, intent)
                except AdmissionDenied as exc:
                    raise _refuse_admission(session, exc) from exc
            return envelope_response(WorkflowRunSchema.model_validate(existing), status_code=202)

        tenant_id = "local"
        if workflow.project_id:
            project = session.get(CaliberProject, workflow.project_id)
            if project is not None:
                tenant_id = project.tenant_id

        now = datetime.now(timezone.utc)
        prepared = None
        if admission.enabled:
            try:
                prepared = prepare_admission(
                    session=session,
                    settings=admission,
                    workflow=workflow,
                    version=version,
                    identity=identity,
                    source="api",  # the route's own word, never the client-chosen ``source`` field
                    now=now,
                    run_tenant_id=tenant_id,
                    intent=intent,
                )
            except AdmissionDenied as exc:
                raise _refuse_admission(session, exc) from exc
            # An admitted run executes exactly what its pins describe: the version's sealed, resolved manifest.
            manifest_snapshot = _clone_manifest(prepared.execution_manifest)
            manifest_metadata = admitted_manifest_metadata(version, manifest_snapshot)
        run = CaliberWorkflowRun(
            workflow_run_id=prepared.run_id if prepared is not None else new_workflow_run_id(),
            manifest_legacy=prepared is None,
            workflow_id=workflow.workflow_id,
            project_id=workflow.project_id,
            tenant_id=tenant_id,
            workflow_version_id=version.version_id,
            deployment_alias=alias,
            session_id=payload.session_id,
            status=RUN_STATUS_QUEUED,
            source=payload.source,
            created_by=actor,
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
                "strict_lineage": payload.workspace_release_id is not None,
                **({"admission_intent": intent} if prepared is not None else {}),
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
                if admission.enabled:
                    try:
                        check_retry_intent(session, duplicate, intent)
                    except AdmissionDenied as denied:
                        raise _refuse_admission(session, denied) from denied
                return envelope_response(
                    WorkflowRunSchema.model_validate(duplicate), status_code=202
                )
            raise HTTPException(
                status_code=409, detail="workflow run idempotency conflict"
            ) from exc

        if payload.workspace_release_id is not None and payload.environment_id is not None:
            try:
                release = session.get(CaliberWorkspaceRelease, payload.workspace_release_id)
                lineage = create_runtime_lineage(
                    session,
                    project_id=workflow.project_id or "",
                    workspace_release_id=payload.workspace_release_id,
                    revision_id=release.revision_id if release is not None else "",
                    environment_id=payload.environment_id,
                    consumer_kind="run",
                    consumer_id=run.workflow_run_id,
                    model_id=payload.model_id,
                    config_sha256=payload.config_sha256,
                    strict_execution=True,
                    created_by=actor,
                )
            except RuntimeError as exc:
                raise HTTPException(status_code=409, detail=str(exc)) from exc
            except ValueError as exc:
                raise HTTPException(status_code=400, detail=str(exc)) from exc
            run.runtime_lineage_id = lineage.lineage_id
            session.flush()

        admitted = None
        if prepared is not None:
            try:
                admitted = persist_admission(
                    session, prepared, run, identity=identity, settings=admission
                )
            except AdmissionDenied as exc:
                raise _refuse_admission(session, exc) from exc
            run.summary = {
                **(run.summary or {}),
                "admitted": True,
                "manifest_id": admitted.manifest_id,
            }
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
                **(
                    {
                        "admitted": True,
                        "grant_id": admitted.grant_id,
                        "decision_id": admitted.decision_id,
                        "manifest_id": admitted.manifest_id,
                        "run_manifest_digest": admitted.manifest_digest,
                    }
                    if admitted is not None
                    else {}
                ),
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
    request: Request,
    workflow_id: str,
    requested_alias: str | None,
) -> tuple[CaliberWorkflow, CaliberWorkflowVersion, str, StartTrigger]:
    """Resolve the deployment an event trigger fires, scoped to the caller.

    The trigger paths resolved workflows and deployments with bare reads, so an event
    posted with a foreign workflow ID started that workflow's run.
    """
    if requested_alias:
        workflow, version, alias = _workflow_and_version_for_run(
            session,
            WorkflowRunCreateRequest(workflow_id=workflow_id, alias=requested_alias),
            request=request,
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

    workflow_row = _visible_workflow_for_run(session, request, workflow_id)
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
    from caliber.auth import resolve_identity  # noqa: PLC0415 - avoids an import cycle

    workflow_id = request.path_params["workflow_id"]
    actor = require_scopes(request, [SCOPE_OPERATOR])
    _ensure_queue_enabled(request)
    body = await parse_json_object(request, allow_empty=True)
    payload = WorkflowTriggerRequest.model_validate(body)
    if _admission_settings(request).lineage_required:
        try:
            require_lineage(payload.workspace_release_id, payload.environment_id)
        except LineageRequiredError as exc:
            return _lineage_required_response(exc)

    factory = get_session_factory(request)
    with factory() as session:
        replayed = _replay_idempotent(
            session,
            request,
            workflow_id=workflow_id,
            source="event",
            idempotency_key=payload.idempotency_key,
            admission=_admission_settings(request),
            intent_for=lambda wf: intent_of(
                workflow_id=wf.workflow_id,
                alias=payload.alias or "",
                actor=actor,
                input_text=_input_to_text(payload.input),
                project_id=wf.project_id,
                workspace_release_id=payload.workspace_release_id,
                environment_id=payload.environment_id,
                model_id=payload.model_id,
                config_sha256=payload.config_sha256,
                source="event",
                input_json=_canonical_input(payload.input),
            ),
        )
        if replayed is not None:
            return envelope_response(WorkflowRunSchema.model_validate(replayed), status_code=200)
        workflow, version, alias, trigger = _resolve_event_trigger_target(
            session,
            request=request,
            workflow_id=workflow_id,
            requested_alias=payload.alias,
        )
        require_project_access_if_scoped(
            session, resolve_identity(request), workflow.project_id, "resource.execute"
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

        try:
            run, created = enqueue_workflow_run(
                session,
                workflow=workflow,
                version=version,
                alias=alias,
                requested_alias=payload.alias or "",
                source="event",
                actor=actor,
                input_text=_input_to_text(payload.input),
                input_json=_canonical_input(payload.input),
                idempotency_key=payload.idempotency_key,
                workspace_release_id=payload.workspace_release_id,
                environment_id=payload.environment_id,
                model_id=payload.model_id,
                config_sha256=payload.config_sha256,
                publish=lambda evt: _emit_queue_event(request, evt),
                identity=resolve_identity(request),
                admission=_admission_settings(request),
            )
        except AdmissionDenied as exc:
            raise _refuse_admission(session, exc) from exc
        except (
            RuntimeError
        ) as exc:  # the lineage is not valid for this project (as on the create-run route)
            session.rollback()
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except ValueError as exc:
            session.rollback()
            raise HTTPException(status_code=400, detail=str(exc)) from exc
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
        row = _get_run_or_404(session, run_id, request=request)
        data = WorkflowRunSchema.model_validate(row)
    return envelope_response(data)


async def get_workflow_run_lineage(request: Request) -> JSONResponse:
    require_user(request)
    run_id = request.path_params["run_id"]
    factory = get_session_factory(request)
    with factory() as session:
        run = _get_run_or_404(session, run_id, request=request)
        data = _build_workflow_run_lineage(session, run)
    return envelope_response(data)


async def get_workflow_run_admission_manifest(request: Request) -> JSONResponse:
    """The run's immutable admission manifest (B02.5, #695); ``legacy`` runs answer with nulls. Distinct from
    ``/manifest``, which is the workflow manifest the run executed and is unchanged."""
    from caliber.auth import resolve_identity  # noqa: PLC0415 - avoids an import cycle

    require_user(request)
    run_id = request.path_params["run_id"]
    factory = get_session_factory(request)
    with factory() as session:
        run = _get_run_or_404(session, run_id, request=request)
        try:
            require_project_access_if_scoped(
                session, resolve_identity(request), run.project_id, "read"
            )
        except HTTPException as exc:
            if exc.status_code != 404:  # noqa: PLR2004 - HTTP not found
                raise
            # A run you can see but whose project you have no role in is, for this evidence, a run that is not there.
            raise HTTPException(
                status_code=404, detail=f"workflow run {run_id!r} not found"
            ) from exc
        if run.manifest_legacy:
            data = AdmissionManifestSchema(workflow_run_id=run.workflow_run_id, legacy=True)
        else:
            try:
                loaded = load_run_manifest(session, run.workflow_run_id)
            except PinsError as exc:
                raise HTTPException(status_code=409, detail=f"{exc.code}: {exc}") from exc
            if loaded is None:
                raise HTTPException(
                    status_code=409,
                    detail="manifest_missing: this admitted run has no stored manifest",
                )
            if digest_of(canonical_bytes(loaded.record)) != loaded.digest:
                raise HTTPException(
                    status_code=409,
                    detail="manifest_corrupt: the stored manifest is not in canonical form, so its digest "
                    "cannot be checked against the record",
                )
            data = AdmissionManifestSchema(
                workflow_run_id=run.workflow_run_id,
                schema_version=str(loaded.record["schema_version"]),
                legacy=False,
                manifest_id=loaded.manifest_id,
                run_manifest=loaded.record,
                manifest_digest=loaded.digest,
            )
    return envelope_response(data)


async def get_workflow_run_causal_links(request: Request) -> JSONResponse:
    """The persisted causal links of a run: manifest, context manifests, effects, approvals, decisions, receipts (B11.3b, #745).

    Read-only, identifiers and digests only (see :mod:`caliber.observability.causal_links`). Access is the run's workflow
    visibility **plus** a role in the run's project (``read``), evaluated on every request from the caller's current
    membership; a run you cannot read, a run in another project and an unknown id are the same ``404``.
    ``section`` (``effects`` by default, ``context_manifests`` or ``policy_decisions``) picks the paginated list and
    ``limit`` and ``cursor`` page it.
    """
    from caliber.auth import resolve_identity  # noqa: PLC0415 - avoids an import cycle

    require_user(request)
    run_id = request.path_params["run_id"]
    factory = get_session_factory(request)
    with factory() as session:
        run = _get_run_or_404(session, run_id, request=request)
        try:
            require_project_access_if_scoped(
                session, resolve_identity(request), run.project_id, "read"
            )
        except HTTPException as exc:
            # A run you can see but whose project you have no role in is, for this evidence, a run that is not there.
            raise HTTPException(
                status_code=404, detail=f"workflow run {run_id!r} not found"
            ) from exc
        section = request.query_params.get("section") or "effects"
        if section not in causal_links.SECTIONS:
            raise HTTPException(
                status_code=400,
                detail=f"section must be one of {', '.join(causal_links.SECTIONS)}",
            )
        limit = _parse_positive_int(
            request.query_params.get("limit"),
            default=causal_links.DEFAULT_LIMIT,
            min_value=1,
            max_value=causal_links.MAX_LIMIT,
            label="limit",
        )
        try:
            payload = causal_links.read_causal_links(
                session,
                run,
                section=section,
                limit=limit,
                cursor=request.query_params.get("cursor"),
            )
        except causal_links.CausalLinksCursorError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        data = CausalLinksSchema.model_validate(payload)
    return envelope_response(data)


async def get_workflow_run_manifest(request: Request) -> JSONResponse:
    require_user(request)
    run_id = request.path_params["run_id"]
    factory = get_session_factory(request)
    with factory() as session:
        run = _get_run_or_404(session, run_id, request=request)
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
        _get_run_or_404(session, run_id, request=request)
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
    from caliber.auth import resolve_identity  # noqa: PLC0415 - avoids an import cycle

    require_user(request)
    run_id = request.path_params["run_id"]
    factory = get_session_factory(request)
    with factory() as session:
        run = _get_run_or_404(session, run_id, request=request)
        trace_id = run.trace_id or None
        # B11.4b: in the reference profile the span payloads are for those who may read them (the run's project
        # owner or editor); everyone else who can see the run gets the same tree with the placeholders.
        payloads_allowed = not payload_governance_active(
            getattr(request.app.state, "config", None)
        ) or can_read_run_payloads(
            session,
            resolve_identity(request),
            project_id=run.project_id,
            created_by=run.created_by,
        )
    if not trace_id:
        return envelope_response(WorkflowRunTraceSchema(trace_id=None, spans=[]))
    tree = fetch_trace_spans(trace_id)
    spans = redact_trace_detail({"spans": tree.spans}, payloads_allowed)["spans"]
    data = WorkflowRunTraceSchema.model_validate(
        {"trace_id": tree.trace_id, "spans": spans, "mlflow_url": tree.mlflow_url}
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
        _get_run_or_404(session, run_id, request=request)
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


async def cancel_workflow_run(request: Request) -> JSONResponse:  # noqa: PLR0915
    from caliber.auth import resolve_identity  # noqa: PLC0415 - avoids an import cycle

    actor = require_scopes(request, [SCOPE_OPERATOR])
    _ensure_queue_enabled(request)
    run_id = request.path_params["run_id"]
    body = await parse_json_object(request, allow_empty=True)
    payload = WorkflowRunCancelRequest.model_validate(body)
    now = datetime.now(timezone.utc)
    publish_payload: dict[str, Any] | None = None
    factory = get_session_factory(request)
    with factory() as session:
        run = _get_run_or_404(session, run_id, request=request)
        require_project_access_if_scoped(
            session, resolve_identity(request), run.project_id, "resource.execute"
        )
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


async def retry_workflow_run(request: Request) -> JSONResponse:  # noqa: PLR0912, PLR0915
    from caliber.auth import resolve_identity  # noqa: PLC0415 - avoids an import cycle

    actor = require_scopes(request, [SCOPE_OPERATOR])
    _ensure_queue_enabled(request)
    run_id = request.path_params["run_id"]
    body = await parse_json_object(request, allow_empty=True)
    payload = WorkflowRunRetryRequest.model_validate(body)
    now = datetime.now(timezone.utc)
    max_attempts = int(request.app.state.config.workflow_run_max_attempts)
    factory = get_session_factory(request)
    with factory() as session:
        run = _get_run_or_404(session, run_id, request=request)
        require_project_access_if_scoped(
            session, resolve_identity(request), run.project_id, "resource.execute"
        )
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
        retry_admission = _admission_settings(request)
        # A retried run keeps the lineage of the run it retries (a fresh record for the new run): the reference profile
        # starts no run without one, so a run that has none cannot be retried there.
        original_lineage = (
            session.get(CaliberWorkspaceRuntimeLineage, run.runtime_lineage_id)
            if run.runtime_lineage_id
            else None
        )
        if retry_admission.lineage_required and original_lineage is None:
            return _lineage_required_response(LineageRequiredError(LINEAGE_REQUIRED_MESSAGE))
        retry_prepared = None
        if retry_admission.enabled:
            # A retry is a new run, so it is admitted like one: pinned afresh, granted, decided and recorded.
            retry_workflow = session.get(CaliberWorkflow, run.workflow_id)
            retry_version = session.get(CaliberWorkflowVersion, run.workflow_version_id)
            if retry_workflow is None or retry_version is None:
                raise HTTPException(
                    status_code=409,
                    detail="cannot retry run: its workflow or version no longer exists",
                )
            retry_intent = intent_of(
                workflow_id=run.workflow_id,
                alias=run.deployment_alias or "",
                actor=actor,
                input_text=retry_input_text,
                project_id=run.project_id,
                input_files=[
                    dict(item)
                    for item in (run.summary or {}).get("input_files") or []
                    if isinstance(item, dict)
                ]
                or None,
                version_selector=run.workflow_version_id,
                priority=int(run.priority or 0),
                session_id=run.session_id,
                source="retry",
                workspace_release_id=original_lineage.workspace_release_id
                if original_lineage
                else None,
                environment_id=original_lineage.environment_id if original_lineage else None,
                model_id=original_lineage.model_id if original_lineage else None,
                config_sha256=original_lineage.config_sha256 if original_lineage else None,
            )
            try:
                retry_prepared = prepare_admission(
                    session=session,
                    settings=retry_admission,
                    workflow=retry_workflow,
                    version=retry_version,
                    identity=resolve_identity(request),
                    source="retry",
                    now=now,
                    run_tenant_id=run.tenant_id or "local",
                    intent=retry_intent,
                )
            except AdmissionDenied as exc:
                raise _refuse_admission(session, exc) from exc
            retry_manifest_snapshot = _clone_manifest(retry_prepared.execution_manifest)
            retry_manifest_metadata = admitted_manifest_metadata(
                retry_version, retry_manifest_snapshot
            )
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
            **({"admission_intent": retry_intent} if retry_prepared is not None else {}),
        }
        original_input_files = (run.summary or {}).get("input_files")
        if isinstance(original_input_files, list):
            retry_summary["input_files"] = deepcopy(original_input_files)
        if retry_checkpoint is not None:
            retry_summary["resume_checkpoint_id"] = retry_checkpoint.checkpoint_id
            retry_summary["resume_checkpoint_run_id"] = retry_checkpoint.workflow_run_id
            retry_summary["retry_mode"] = "checkpoint"
        retried = CaliberWorkflowRun(
            workflow_run_id=retry_prepared.run_id
            if retry_prepared is not None
            else new_workflow_run_id(),
            manifest_legacy=retry_prepared is None,
            workflow_id=run.workflow_id,
            project_id=run.project_id,
            tenant_id=run.tenant_id or "local",
            workflow_version_id=run.workflow_version_id,
            deployment_alias=run.deployment_alias,
            session_id=run.session_id,
            status=RUN_STATUS_QUEUED,
            source=run.source or "manual",
            # The retrying actor, not the original: they are the one who caused this
            # attempt, so they are the one who must not also approve it.
            created_by=actor,
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
        if original_lineage is not None:
            try:
                retry_lineage = create_runtime_lineage(
                    session,
                    project_id=original_lineage.project_id,
                    workspace_release_id=original_lineage.workspace_release_id,
                    revision_id=original_lineage.revision_id,
                    environment_id=original_lineage.environment_id,
                    consumer_kind="run",
                    consumer_id=retried.workflow_run_id,
                    model_id=original_lineage.model_id,
                    config_sha256=original_lineage.config_sha256,
                    strict_execution=True,
                    created_by=actor,
                )
            except RuntimeError as exc:  # the release or environment is no longer eligible
                raise HTTPException(status_code=409, detail=str(exc)) from exc
            retried.runtime_lineage_id = retry_lineage.lineage_id
            retried.summary = {**(retried.summary or {}), "strict_lineage": True}
            session.flush()
        if retry_prepared is not None:
            try:
                retry_record = persist_admission(
                    session,
                    retry_prepared,
                    retried,
                    identity=resolve_identity(request),
                    settings=retry_admission,
                )
            except AdmissionDenied as exc:
                raise _refuse_admission(session, exc) from exc
            retried.summary = {
                **(retried.summary or {}),
                "admitted": True,
                "manifest_id": retry_record.manifest_id,
            }
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


#: ``reason_code`` of the 503 when a decision meets lock contention (a PostgreSQL lock timeout or deadlock victim, a SQLite busy timeout): nothing was decided.
APPROVAL_BUSY = "approval_busy"
_APPROVAL_BUSY_DETAIL = (
    "this approval is being decided or dispatched right now; nothing was decided, retry shortly"
)


def _approval_busy(session: Session) -> CaliberHTTPException:
    """Release what the aborted transaction held and answer the retryable, payload-free ``503``."""
    session.rollback()
    return CaliberHTTPException(
        status_code=503,
        detail=_APPROVAL_BUSY_DETAIL,
        reason_code=APPROVAL_BUSY,
        headers={"Retry-After": RETRY_AFTER_SECONDS},
    )


@contextmanager
def _decision_session(factory: Any) -> Iterator[Session]:
    """A session for one approval decision, in which lock contention is the retryable ``503`` ``approval_busy`` (#1066).

    Contention is a PostgreSQL lock wait that outlasts the lock timeout (``55P03``), a deadlock in which the server aborted
    this transaction as the victim (``40P01``, a writer that takes the run and request rows in another order) and, on SQLite,
    the busy timeout (``database is locked``) that surfaces at the first write or the commit because SQLite has no row locks
    to wait on at the ``FOR UPDATE`` statements. It can strike any statement of the decision, so the whole session is
    covered rather than the two locking statements: the transaction is rolled back, nothing was decided, and a retry is safe.
    Any other database error propagates unchanged.
    """
    with factory() as session:
        try:
            yield session
        except OperationalError as exc:
            if not is_lock_contention(exc):
                raise
            raise _approval_busy(session) from None


def _lock_workflow_run(session: Session, run: CaliberWorkflowRun) -> CaliberWorkflowRun:
    """Refresh and lock a visible run before writing an approval decision (the first lock of the order run, request).

    On PostgreSQL the waits of this transaction are bounded by ``SET LOCAL lock_timeout`` (the Apply claim's bound,
    :data:`~caliber.routes._job_access.LOCK_TIMEOUT_MS`): a lock is held by a transaction about to commit (a dispatcher's
    claim, a request writer), so a wait is short, and a stuck one must not pin a handler that runs on the event loop. A
    timeout, like a deadlock or a SQLite busy timeout (:func:`_decision_session`), is a ``503`` ``approval_busy`` with
    nothing decided.
    """
    if session.get_bind().dialect.name == "postgresql":
        session.execute(text(f"SET LOCAL lock_timeout = {int(LOCK_TIMEOUT_MS)}"))
    return session.execute(
        select(CaliberWorkflowRun)
        .where(CaliberWorkflowRun.workflow_run_id == run.workflow_run_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    ).scalar_one()


#: ``reason_code`` of the 409 for a bound request that already holds a decision (approved but not yet consumed by dispatch).
APPROVAL_ALREADY_DECIDED = "already_decided"
#: ``reason_code`` of the 409 for a bound request that belongs to another node than the one the run waits at.
APPROVAL_NOT_CURRENT = "approval_not_current"
#: The 409 message for each refusal of a bound request. Static on purpose: no request id, proposal id, hash, requester or approver.
_BOUND_REFUSAL_DETAIL = {
    REFUSAL_SUPERSEDED: (
        "this approval request was replaced by a newer request and can no longer be decided; "
        "decide the current request for this step"
    ),
    REFUSAL_EXPIRED: (
        "this approval request has expired and can no longer be decided; "
        "ask for a new approval of the step"
    ),
    REFUSAL_ALREADY_CONSUMED: "this approval decision was already consumed by dispatch",
    APPROVAL_ALREADY_DECIDED: (
        "this approval request already holds a decision and cannot be decided again; "
        "an approval is consumed when its effect is dispatched"
    ),
    APPROVAL_NOT_CURRENT: "this approval request is not for the step the run is waiting at",
}


def _utc_now() -> datetime:
    """The clock the decision routes judge an approval's expiry by (a seam so a test can fix it)."""
    return datetime.now(timezone.utc)


def _bound_refusal(code: str) -> CaliberHTTPException:
    """The payload-free 409 for a bound request that cannot be decided: one stable ``reason_code`` and a static message."""
    return CaliberHTTPException(
        status_code=409, detail=_BOUND_REFUSAL_DETAIL[code], reason_code=code
    )


def _require_bound_authority(
    row: CaliberRuntimeApprovalRequest,
    *,
    actor: str,
    scopes: frozenset[str] | set[str],
    enforce_requester: bool,
) -> None:
    """``403`` unless the caller may decide ``row`` at all: the node's ``required_role`` scope and, for an approval, not the requester.

    Runs before any state refusal, so nobody who may not decide a request learns its state from the codes.
    """
    policy = ApprovalPolicy.from_snapshot(row.policy_snapshot)
    if policy.required_role not in scopes:
        raise HTTPException(
            status_code=403,
            detail=f"this approval requires the {policy.required_role!r} scope; you hold {sorted(scopes)}",
        )
    if enforce_requester:
        try:
            assert_distinct_actor(actor, row.requested_by, allow_self=policy.allow_self_approval)
        except ApprovalPolicyError as exc:
            raise HTTPException(status_code=403, detail=str(exc)) from exc


def _refuse_undecidable_bound(
    row: CaliberRuntimeApprovalRequest, *, run: CaliberWorkflowRun, now: datetime
) -> None:
    """The stable ``409`` for a bound request that is final or not for this run's step; see the selector for the order."""
    if row.consumed_at is not None:
        raise _bound_refusal(REFUSAL_ALREADY_CONSUMED)
    refusal = approval_refusal(
        status=row.status,
        superseded_by=row.superseded_by,
        expires_at=row.expires_at,
        now=now,
    )
    if refusal is not None:
        raise _bound_refusal(refusal)
    if row.node_id != run.current_node_id:
        raise _bound_refusal(APPROVAL_NOT_CURRENT)
    if row.status != "pending":
        raise _bound_refusal(APPROVAL_ALREADY_DECIDED)


def _select_pending_runtime_approval(
    session: Session,
    *,
    run: CaliberWorkflowRun,
    runtime_approval_id: str | None,
    now: datetime,
    actor: str,
    scopes: frozenset[str] | set[str],
    enforce_requester: bool,
) -> CaliberRuntimeApprovalRequest:
    """Select and lock (``FOR UPDATE``) the approval this decision is about; the caller holds the run lock.

    A request bound to an effect proposal (``proposal_id`` set, B06.1) is decidable by its explicit id: this route
    records the human decision and nothing else. It neither rechecks the effect nor consumes the request; the dispatcher
    rechecks the exact intent, evidence, policy and the approver's *current* authority, and sets ``consumed_at`` in the
    transaction that claims dispatch (B06.4, #735). The row is read under the lock, so a decision cannot interleave with a
    creator superseding it or a dispatcher consuming it.

    For a bound request the caller's authority is checked **first** (the node's ``required_role`` scope, and for an
    approval the requester rule), so a project member who may not decide it learns nothing about its state from the
    refusal codes below. (That holds for these *decision routes*. The read-only view, ``GET .../approvals/{id}``, is a separate
    surface of the project ``read`` tier and deliberately shows any reader a request's state, as the list always showed its status;
    the effect and the requester stay with those who may decide.) The caller is the authenticated principal and ``scopes`` are the scopes of the presented token.

    A bound request that cannot be decided is a ``409`` with a stable ``reason_code`` and a static, payload-free message,
    and nothing is written. Checked in this order, because each earlier state is final and hides the later ones:
    ``already_consumed`` (a consumed request stays terminal after its expiry passes), ``superseded`` (reported before
    ``expired``), ``expired`` (``now`` at or past ``expires_at``, B06.2), ``approval_not_current`` (another node's request),
    ``already_decided`` (approved, and waiting for dispatch). A legacy node-level request keeps its alpha behaviour. The
    default selection (no id) ignores bound rows, so a legacy request is never shadowed by a newer bound one and a bound
    request is only ever decided by naming it.
    """
    if runtime_approval_id:
        row = session.execute(
            select(CaliberRuntimeApprovalRequest)
            .where(CaliberRuntimeApprovalRequest.runtime_approval_id == runtime_approval_id)
            .where(CaliberRuntimeApprovalRequest.workflow_run_id == run.workflow_run_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        ).scalar_one_or_none()
        if row is None:
            raise HTTPException(
                status_code=404,
                detail=(
                    "runtime approval "
                    f"{runtime_approval_id!r} not found for workflow run {run.workflow_run_id!r}"
                ),
            )
        if row.proposal_id is not None:
            _require_bound_authority(
                row, actor=actor, scopes=scopes, enforce_requester=enforce_requester
            )
            _refuse_undecidable_bound(row, run=run, now=now)
        elif row.consumed_at is not None:
            raise _bound_refusal(REFUSAL_ALREADY_CONSUMED)
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
        .where(CaliberRuntimeApprovalRequest.proposal_id.is_(None))
        .order_by(CaliberRuntimeApprovalRequest.requested_at.desc())
        .with_for_update()
        .execution_options(populate_existing=True)
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
    """The run's approval requests. Visible to whoever can see the run, as it always was.

    The exact-intent fields added for the approval panel (``proposal_id``, ``expires_at``, ``superseded_by``,
    ``consumed_at``, B06.5, #743) follow the view route's rule instead: they are filled only for a caller who also holds
    the project ``read`` role (a run with no project has none to hold), and are ``None`` for anyone else, so a list reader
    learns nothing about a bound request that ``GET .../approvals/{id}`` would refuse them.
    """
    from caliber.auth import resolve_identity  # noqa: PLC0415 - avoids an import cycle

    require_user(request)
    run_id = request.path_params["run_id"]
    factory = get_session_factory(request)
    with factory() as session:
        run = _get_run_or_404(session, run_id, request=request)
        try:
            require_project_access_if_scoped(
                session, resolve_identity(request), run.project_id, "read"
            )
            may_read_binding = True
        except HTTPException:
            may_read_binding = False
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
        if not may_read_binding:
            for item in data:
                item.proposal_id = None
                item.expires_at = None
                item.superseded_by = None
                item.consumed_at = None
    return envelope_response(data)


async def approve_workflow_run_approval(request: Request) -> JSONResponse:  # noqa: PLR0915 - legacy route plus B06.4 lock/decision semantics
    from caliber.auth import resolve_identity  # noqa: PLC0415 - avoids an import cycle

    # ``require_user``, not ``require_scopes([SCOPE_OPERATOR])``. The node's own
    # ``required_role`` is the authorization rule, and ``record_approval`` enforces it
    # below with a message naming the missing scope. Pre-requiring operator broke the
    # documented case: ``caliber.approver`` does not imply ``caliber.operator``, so a
    # deployment that granted someone exactly the approver scope — the role the field
    # is named after — could not approve anything.
    actor = require_user(request)
    _ensure_queue_enabled(request)
    _ensure_runtime_approvals_enabled(request)
    _ensure_checkpointing_enabled(request)
    run_id = request.path_params["run_id"]
    body = await parse_json_object(request, allow_empty=True)
    payload = WorkflowRunApprovalDecisionRequest.model_validate(body)
    factory = get_session_factory(request)
    with _decision_session(factory) as session:
        run = _get_run_or_404(session, run_id, request=request)
        # The node's required_role (below) is a platform scope; this is the separate
        # project-role axis. A viewer or editor who happens to hold the approver scope
        # still cannot decide a run in a project where they have no approval role (alpha
        # gap P1-24). It is checked before the run lock, so a caller who is refused never
        # queues behind (or holds up) a dispatcher.
        require_project_access_if_scoped(
            session, resolve_identity(request), run.project_id, ACTION_APPROVE
        )
        # Decision writers, request creators and the dispatcher share the run ->
        # proposal -> approval lock order. Holding the run lock before selecting the
        # request serializes a decision with supersession and dispatch, and the clock
        # is read after the lock is won: a decision that waited behind a dispatcher
        # or creator is judged (expiry) at the moment it can actually write.
        run = _lock_workflow_run(session, run)
        now = _utc_now()
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
            now=now,
            actor=actor,
            scopes=current_scopes(request),
            enforce_requester=True,
        )
        approval_id = approval.runtime_approval_id
        # Enforce the node's OWN policy (C6): this endpoint previously required only
        # the global operator scope, so a node configured for ``caliber.admin`` with a
        # quorum of two was approvable by any operator, once. ``record_approval``
        # applies the required role, distinct-approver quorum, and the
        # initiator-cannot-approve rule.
        policy = ApprovalPolicy.from_snapshot(approval.policy_snapshot)
        recorded = list((approval.policy_snapshot or {}).get("approvals") or [])
        try:
            quorum = record_approval(
                policy=policy,
                existing_approvals=recorded,
                actor=actor,
                actor_scopes=current_scopes(request),
                initiated_by=run.created_by,
            )
        except ApprovalPolicyError as exc:
            raise HTTPException(status_code=403, detail=str(exc)) from exc

        # Persist the accumulated approvers alongside the snapshot so a quorum
        # survives a restart and cannot be satisfied by one person twice.
        approval.policy_snapshot = {
            **(approval.policy_snapshot or {}),
            **policy.to_snapshot(),
            "approvals": list(quorum.approvals),
        }
        if not quorum.satisfied:
            # Still short of the quorum: record the partial decision and leave the run
            # waiting. Returning 200 with the remaining count is what lets the UI show
            # "1 of 2 approvals".
            audit_record(
                session,
                actor=actor,
                action="record_workflow_run_partial_approval",
                entity_type="runtime_approval",
                entity_id=approval.runtime_approval_id,
                details={
                    "workflow_run_id": run.workflow_run_id,
                    "approvals": len(quorum.approvals),
                    "required": quorum.required,
                },
            )
            _append_run_event(
                session,
                workflow_run_id=run.workflow_run_id,
                project_id=run.project_id,
                event_type="workflow.run.approval.partial",
                payload={
                    "runtime_approval_id": approval.runtime_approval_id,
                    "node_id": approval.node_id,
                    "decided_by": actor,
                    "approvals": len(quorum.approvals),
                    "required": quorum.required,
                },
                node_id=approval.node_id,
            )
            session.commit()
            return envelope_response(
                RuntimeApprovalAckSchema(
                    workflow_run_id=run.workflow_run_id,
                    runtime_approval_id=approval.runtime_approval_id,
                    status="pending",
                    approvals=len(quorum.approvals),
                    required_approvals=quorum.required,
                    remaining_approvals=quorum.remaining,
                )
            )

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
                "approvals": list(quorum.approvals),
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
    from caliber.auth import resolve_identity  # noqa: PLC0415 - avoids an import cycle

    # Same rule as approve: the node decides who may decide. This route previously
    # checked only the global operator scope and never read the node policy at all, so
    # a gate configured for ``caliber.admin`` was rejectable by any operator. Refusing
    # a release is a governance decision as much as permitting one.
    actor = require_user(request)
    _ensure_queue_enabled(request)
    _ensure_runtime_approvals_enabled(request)
    run_id = request.path_params["run_id"]
    body = await parse_json_object(request, allow_empty=True)
    payload = WorkflowRunApprovalDecisionRequest.model_validate(body)
    approval_id: str | None = None
    approval_node_id: str | None = None
    factory = get_session_factory(request)
    with _decision_session(factory) as session:
        run = _get_run_or_404(session, run_id, request=request)
        # The node's required_role (below) is a platform scope; this is the separate
        # project-role axis. A viewer or editor who happens to hold the approver scope
        # still cannot decide a run in a project where they have no approval role (alpha
        # gap P1-24). Checked before the run lock, as in approve.
        require_project_access_if_scoped(
            session, resolve_identity(request), run.project_id, ACTION_APPROVE
        )
        # Keep the decision writer in the same run-first lock order as the
        # dispatcher and approval-request supersession writer; read the clock
        # only once that lock is held.
        run = _lock_workflow_run(session, run)
        now = _utc_now()
        if run.status != RUN_STATUS_WAITING_APPROVAL:
            raise HTTPException(
                status_code=409,
                detail=f"workflow run {run.workflow_run_id!r} is not waiting for approval",
            )
        approval = _select_pending_runtime_approval(
            session,
            run=run,
            runtime_approval_id=payload.runtime_approval_id,
            now=now,
            actor=actor,
            scopes=current_scopes(request),
            enforce_requester=False,
        )
        approval_id = approval.runtime_approval_id
        approval_node_id = approval.node_id
        # The node's required_role, enforced before the decision is written. Quorum and
        # the self-decision rule deliberately do NOT apply to a rejection: one
        # authorized reviewer refusing is a complete answer, and requiring a second
        # person to agree before a release can be *stopped* would be a safety
        # regression dressed up as consistency.
        reject_policy = ApprovalPolicy.from_snapshot(approval.policy_snapshot)
        if reject_policy.required_role not in current_scopes(request):
            raise HTTPException(
                status_code=403,
                detail=(
                    f"rejecting this approval requires the {reject_policy.required_role!r} "
                    f"scope; you hold {sorted(current_scopes(request))}"
                ),
            )
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
    from caliber.auth import resolve_identity  # noqa: PLC0415 - avoids an import cycle

    actor = require_scopes(request, [SCOPE_OPERATOR])
    _ensure_queue_enabled(request)
    _ensure_checkpointing_enabled(request)
    run_id = request.path_params["run_id"]
    body = await parse_json_object(request, allow_empty=True)
    payload = WorkflowRunResumeRequest.model_validate(body)
    factory = get_session_factory(request)
    with factory() as session:
        run = _get_run_or_404(session, run_id, request=request)
        require_project_access_if_scoped(
            session, resolve_identity(request), run.project_id, "resource.execute"
        )
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
        # Restricted to runs whose parent workflow the caller can see. This scanned every
        # waiting run in the database, so an external event could resume another project's
        # run — and the 409 diagnostics below echo run IDs, so even a non-matching event
        # enumerated foreign runs. A run carries no visibility of its own, hence the
        # subquery against the workflows the caller may actually see.
        from caliber.auth import resolve_identity  # noqa: PLC0415 - avoids an import cycle
        from caliber.db.scoping import apply_visibility_filter  # noqa: PLC0415

        identity = resolve_identity(request)
        visible_workflow_ids = apply_visibility_filter(
            select(CaliberWorkflow.workflow_id),
            CaliberWorkflow,
            identity,
            identity.active_project_id,
        )
        rows = (
            session.execute(
                select(CaliberWorkflowRun)
                .where(
                    CaliberWorkflowRun.status == RUN_STATUS_WAITING_EVENT,
                    CaliberWorkflowRun.workflow_id.in_(visible_workflow_ids),
                )
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
        require_project_access_if_scoped(session, identity, run.project_id, "resource.execute")
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


async def get_workflow_run_approval_view(request: Request) -> JSONResponse:
    """The approver's view of one approval request of a run: what is being decided and what the caller may do (B06.5, #743).

    Read-only; the decision routes stay the authority and re-derive everything under their locks. Access is the run's
    workflow visibility **plus** a ``read`` role in the run's project, evaluated now; a run you cannot read, an approval of
    another run and an unknown id are the same ``404``. The caller who also holds the project ``approve`` action sees the
    effect (connector, class, resource and the shape of its arguments, never a value); anyone else sees the pins, the state and
    the quorum. ``caller`` says, from the decision routes' own rules, whether the authenticated principal may approve or reject
    now and why not, so a client disables a button from this and decides nothing itself. See :mod:`caliber.workflows.approval_view`.
    """
    from caliber.auth import resolve_identity  # noqa: PLC0415 - avoids an import cycle

    actor = require_user(request)
    run_id = request.path_params["run_id"]
    approval_id = request.path_params["approval_id"]
    config = request.app.state.config
    factory = get_session_factory(request)
    with factory() as session:
        run = _get_run_or_404(session, run_id, request=request)
        identity = resolve_identity(request)
        try:
            require_project_access_if_scoped(session, identity, run.project_id, "read")
        except HTTPException as exc:
            # A run you can see but whose project you have no role in is, for this record, a run that is not there.
            raise HTTPException(
                status_code=404, detail=f"workflow run {run_id!r} not found"
            ) from exc
        approval = session.execute(
            select(CaliberRuntimeApprovalRequest)
            .where(CaliberRuntimeApprovalRequest.runtime_approval_id == approval_id)
            .where(CaliberRuntimeApprovalRequest.workflow_run_id == run.workflow_run_id)
        ).scalar_one_or_none()
        if approval is None:
            raise HTTPException(
                status_code=404,
                detail=f"runtime approval {approval_id!r} not found for workflow run {run_id!r}",
            )
        try:
            require_project_access_if_scoped(session, identity, run.project_id, ACTION_APPROVE)
            project_allows_approve = True
        except HTTPException:
            project_allows_approve = False
        data = WorkflowApprovalViewSchema.model_validate(
            build_approval_view(
                session,
                run=run,
                approval=approval,
                actor=actor,
                scopes=current_scopes(request),
                project_allows_approve=project_allows_approve,
                now=_utc_now(),
                queue_enabled=bool(config.workflow_run_queue_enabled),
                approvals_enabled=bool(config.workflow_run_runtime_approvals_enabled),
                checkpointing_enabled=bool(config.workflow_run_checkpointing_enabled),
            )
        )
    return envelope_response(data)


def register(app: Starlette) -> None:
    app.routes.append(Route(CREATE_PATH, create_workflow_run, methods=["POST"]))
    app.routes.append(Route(DETAIL_PATH, get_workflow_run, methods=["GET"]))
    app.routes.append(Route(LINEAGE_PATH, get_workflow_run_lineage, methods=["GET"]))
    app.routes.append(Route(MANIFEST_PATH, get_workflow_run_manifest, methods=["GET"]))
    app.routes.append(
        Route(ADMISSION_MANIFEST_PATH, get_workflow_run_admission_manifest, methods=["GET"])
    )
    app.routes.append(Route(CAUSAL_LINKS_PATH, get_workflow_run_causal_links, methods=["GET"]))
    app.routes.append(Route(EVENTS_PATH, list_workflow_run_events, methods=["GET"]))
    app.routes.append(Route(TRACE_PATH, get_workflow_run_trace, methods=["GET"]))
    app.routes.append(Route(CHECKPOINTS_PATH, list_workflow_run_checkpoints, methods=["GET"]))
    app.routes.append(Route(CANCEL_PATH, cancel_workflow_run, methods=["POST"]))
    app.routes.append(Route(RETRY_PATH, retry_workflow_run, methods=["POST"]))
    app.routes.append(Route(APPROVALS_PATH, list_workflow_run_approvals, methods=["GET"]))
    app.routes.append(Route(APPROVAL_VIEW_PATH, get_workflow_run_approval_view, methods=["GET"]))
    app.routes.append(Route(APPROVE_PATH, approve_workflow_run_approval, methods=["POST"]))
    app.routes.append(Route(REJECT_PATH, reject_workflow_run_approval, methods=["POST"]))
    app.routes.append(Route(RESUME_PATH, resume_workflow_run, methods=["POST"]))
    app.routes.append(Route(RESUME_BY_EVENT_PATH, resume_workflow_run_by_event, methods=["POST"]))
    app.routes.append(Route(TRIGGER_PATH, trigger_workflow_event, methods=["POST"]))
