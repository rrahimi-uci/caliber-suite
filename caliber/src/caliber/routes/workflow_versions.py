"""``/caliber/workflows/{id}/versions`` and ``/caliber/workflow-versions`` (plan §15.2).

Draft lifecycle: create → validate → compile → publish, with preview-run and
export at any point. Draft updates use optimistic locking on ``manifest_hash``
(``If-Match`` semantics) so two concurrent editors can't silently clobber each
other (plan §16.7.7). Published versions are immutable.
"""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timezone
from typing import Any, Literal, cast

import yaml
from pydantic import ValidationError
from sqlalchemy import func, select
from sqlalchemy.orm import Session
from starlette.applications import Starlette
from starlette.exceptions import HTTPException
from starlette.requests import Request
from starlette.responses import JSONResponse, PlainTextResponse
from starlette.routing import Route

from caliber.audit import record as audit_record
from caliber.auth import SCOPE_OPERATOR, require_scopes, require_user
from caliber.db.models import (
    CaliberEvalDataset,
    CaliberSkill,
    CaliberToolRegistry,
    CaliberWorkflow,
    CaliberWorkflowDeployment,
    CaliberWorkflowPatch,
    CaliberWorkflowRun,
    CaliberWorkflowVersion,
)
from caliber.ids import new_workflow_patch_id, new_workflow_run_id, new_workflow_version_id
from caliber.llm.provider import (
    LLMProviderError,
    WorkflowEditContext,
    WorkflowGenerationContext,
    build_provider,
)
from caliber.observability import metrics
from caliber.routes._deps import envelope_response, get_session_factory, parse_json_object
from caliber.schemas import (
    CopilotEditRequest,
    PlanBuildRequest,
    PreviewRunRequest,
    ProposePatchRequest,
    WorkflowPatchSchema,
    WorkflowRunHistoryArtifactStatsSchema,
    WorkflowRunHistoryStatsSchema,
    WorkflowRunRequest,
    WorkflowRunSchema,
    WorkflowVersionCreateRequest,
    WorkflowVersionSchema,
    WorkflowVersionUpdateRequest,
)
from caliber.storage import StorageError, WorkingDirectoryService, build_backend
from caliber.workflows.compiler import CompileError, compile_workflow
from caliber.workflows.diff import compute_graph_diff
from caliber.workflows.file_tools import bind_run_read_tools
from caliber.workflows.manifest import (
    SubworkflowNode,
    WorkflowManifestError,
    compute_manifest_hash,
    parse_manifest,
)
from caliber.workflows.memory_tools import bind_run_memory_tools
from caliber.workflows.promoter import (
    PublishError,
    build_executor,
    build_plan,
    compile_version,
    publish_version,
    resolver_from_session,
    run_preview,
    workflow_run_summary,
)
from caliber.workflows.refinement import (
    generate_workflow_patch,
    localize_failure,
)
from caliber.workflows.runtime import NodeStep, WorkflowRunResult, execute
from caliber.workflows.validation import (
    SEVERITY_WARNING,
    ValidationReport,
    find_inline_secrets,
    validate_manifest,
)

logger = logging.getLogger("caliber.workflows.run")
_MANIFEST_ERROR_PATH_MIN_PARTS = 3

PREFIX = "/ajax-api/2.0/mlflow/caliber"
LIST_PATH = PREFIX + "/workflows/{workflow_id}/versions"
DETAIL_PATH = PREFIX + "/workflow-versions/{version_id}"
VALIDATE_PATH = DETAIL_PATH + "/validate"
COMPILE_PATH = DETAIL_PATH + "/compile"
PUBLISH_PATH = DETAIL_PATH + "/publish"
PREVIEW_PATH = DETAIL_PATH + "/preview-run"
RUN_PATH = DETAIL_PATH + "/run"
PROPOSE_PATCH_PATH = DETAIL_PATH + "/propose-patch"
COPILOT_EDIT_PATH = DETAIL_PATH + "/copilot-edit"
PLAN_BUILD_PATH = DETAIL_PATH + "/plan-build"

_RUN_LIST_DEFAULT_LIMIT = 200
_RUN_LIST_MAX_LIMIT = 200
_RUN_LIST_FILTER_SCAN_BATCH_SIZE = 250


def _clone_manifest(manifest: dict[str, Any]) -> dict[str, Any]:
    """Deep-copy a manifest so persisted run snapshots never alias editor state."""
    return cast(dict[str, Any], json.loads(json.dumps(manifest)))


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


EXPORT_MANIFEST_PATH = DETAIL_PATH + "/export/manifest"
EXPORT_PYTHON_PATH = DETAIL_PATH + "/export/python"
PATCHES_PATH = PREFIX + "/workflows/{workflow_id}/patches"
RUNS_PATH = PREFIX + "/workflows/{workflow_id}/runs"
RUN_STATS_PATH = PREFIX + "/workflows/{workflow_id}/runs/stats"
RUN_BY_TRACE_PATH = PREFIX + "/workflow-runs/by-trace/{trace_id}"


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


def _stringify_manifest_error_path(parts: list[Any]) -> str:
    path = ""
    for part in parts:
        if isinstance(part, int):
            path += f"[{part}]"
        else:
            text = str(part)
            path = f"{path}.{text}" if path else text
    return path


def _latest_workflow_version(
    session: Session,
    workflow_id: str,
) -> CaliberWorkflowVersion | None:
    return (
        session.execute(
            select(CaliberWorkflowVersion)
            .where(CaliberWorkflowVersion.workflow_id == workflow_id)
            .order_by(CaliberWorkflowVersion.version_number.desc())
        )
        .scalars()
        .first()
    )


def _active_workflow_deployment(
    session: Session,
    *,
    workflow_id: str,
    alias: str,
) -> CaliberWorkflowDeployment | None:
    return (
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


def _enrich_subworkflow_validation(
    session: Session,
    manifest: Any,
    report: ValidationReport,
) -> None:
    parent_workflow_id = manifest.workflow_id.strip()
    for node_id, node in manifest.nodes.items():
        if not isinstance(node, SubworkflowNode):
            continue
        workflow_id = node.workflow_id.strip()
        if not workflow_id or workflow_id == parent_workflow_id:
            continue

        path = f"nodes.{node_id}.workflow_id"
        target_workflow = session.get(CaliberWorkflow, workflow_id)
        if target_workflow is None:
            report.add(
                "subworkflow_unknown_workflow",
                path,
                f"Subworkflow node {node_id!r} references workflow {workflow_id!r}, "
                "but that workflow does not exist in this workspace.",
            )
            continue

        alias = node.alias.strip() or "prod"
        if alias == "manual":
            latest_version = _latest_workflow_version(session, workflow_id)
            if latest_version is None:
                report.add(
                    "subworkflow_manual_missing_version",
                    f"nodes.{node_id}.alias",
                    f"Subworkflow node {node_id!r} uses alias 'manual', but child workflow "
                    f"{workflow_id!r} has no saved versions to execute yet.",
                )
                continue
            if latest_version.status != "published":
                report.add(
                    "subworkflow_manual_uses_unpublished_version",
                    f"nodes.{node_id}.alias",
                    f"Subworkflow node {node_id!r} resolves alias 'manual' to child version "
                    f"v{latest_version.version_number} ({latest_version.version_id}), which is "
                    f"currently {latest_version.status!r}. Saving or publishing a newer child "
                    "version will change this node's runtime target immediately.",
                    severity=SEVERITY_WARNING,
                )
            continue

        deployment = _active_workflow_deployment(
            session,
            workflow_id=workflow_id,
            alias=alias,
        )
        if deployment is None:
            report.add(
                "subworkflow_missing_active_alias",
                f"nodes.{node_id}.alias",
                f"Subworkflow node {node_id!r} targets alias {alias!r}, but child workflow "
                f"{workflow_id!r} has no active deployment for that alias.",
            )
            continue

        target_version = session.get(CaliberWorkflowVersion, deployment.version_id)
        if target_version is None:
            report.add(
                "subworkflow_missing_target_version",
                f"nodes.{node_id}.alias",
                f"Deployment {deployment.deployment_id!r} for child workflow {workflow_id!r} "
                f"references missing version {deployment.version_id!r}.",
            )


def _normalize_manifest_error_path(
    raw_manifest: dict[str, Any],
    loc: tuple[Any, ...],
) -> str:
    parts = list(loc)
    raw_nodes = raw_manifest.get("nodes")
    if (
        len(parts) >= _MANIFEST_ERROR_PATH_MIN_PARTS
        and parts[0] == "nodes"
        and isinstance(raw_nodes, dict)
        and isinstance(parts[1], str)
    ):
        raw_node = raw_nodes.get(parts[1])
        raw_type = raw_node.get("type") if isinstance(raw_node, dict) else None
        if isinstance(raw_type, str) and parts[2] == raw_type:
            parts.pop(2)
    raw_tools = raw_manifest.get("tools")
    if (
        len(parts) >= _MANIFEST_ERROR_PATH_MIN_PARTS
        and parts[0] == "tools"
        and isinstance(raw_tools, dict)
        and isinstance(parts[1], str)
    ):
        raw_tool = raw_tools.get(parts[1])
        raw_type = (
            raw_tool.get("type", "registered_function") if isinstance(raw_tool, dict) else None
        )
        if isinstance(raw_type, str) and parts[2] == raw_type:
            parts.pop(2)
    return _stringify_manifest_error_path(parts)


def _manifest_parse_error_report(
    raw_manifest: dict[str, Any],
    exc: Exception,
) -> dict[str, Any]:
    errors: list[dict[str, str]] = []
    if isinstance(exc, ValidationError):
        for item in exc.errors(include_url=False):
            loc = item.get("loc")
            message = str(item.get("msg") or str(exc))
            path = (
                _normalize_manifest_error_path(raw_manifest, tuple(loc))
                if isinstance(loc, tuple)
                else ""
            )
            errors.append(
                {
                    "code": "parse_error",
                    "path": path,
                    "message": message,
                    "severity": "error",
                }
            )
    if not errors:
        errors.append(
            {
                "code": "parse_error",
                "path": "",
                "message": str(exc),
                "severity": "error",
            }
        )
    return {"valid": False, "errors": errors, "warnings": []}


def _get_version_or_404(session: Session, version_id: str) -> CaliberWorkflowVersion:
    version = session.get(CaliberWorkflowVersion, version_id)
    if version is None:
        raise HTTPException(status_code=404, detail=f"workflow version {version_id!r} not found")
    return version


async def list_versions(request: Request) -> JSONResponse:
    require_user(request)
    workflow_id = request.path_params["workflow_id"]
    factory = get_session_factory(request)
    with factory() as session:
        if session.get(CaliberWorkflow, workflow_id) is None:
            raise HTTPException(status_code=404, detail=f"workflow {workflow_id!r} not found")
        rows = (
            session.execute(
                select(CaliberWorkflowVersion)
                .where(CaliberWorkflowVersion.workflow_id == workflow_id)
                .order_by(CaliberWorkflowVersion.version_number.desc())
            )
            .scalars()
            .all()
        )
        items = [WorkflowVersionSchema.model_validate(row) for row in rows]
    return envelope_response(items)


async def create_version(request: Request) -> JSONResponse:
    workflow_id = request.path_params["workflow_id"]
    body = await parse_json_object(request)
    payload = WorkflowVersionCreateRequest.model_validate(body)
    actor = require_scopes(request, [SCOPE_OPERATOR])
    _parse_manifest_or_400(payload.manifest)

    factory = get_session_factory(request)
    with factory() as session:
        if session.get(CaliberWorkflow, workflow_id) is None:
            raise HTTPException(status_code=404, detail=f"workflow {workflow_id!r} not found")
        max_number = (
            session.execute(
                select(CaliberWorkflowVersion.version_number)
                .where(CaliberWorkflowVersion.workflow_id == workflow_id)
                .order_by(CaliberWorkflowVersion.version_number.desc())
            )
            .scalars()
            .first()
        )
        version = CaliberWorkflowVersion(
            version_id=new_workflow_version_id(),
            workflow_id=workflow_id,
            version_number=(max_number or 0) + 1,
            status="draft",
            manifest=payload.manifest,
            manifest_hash=compute_manifest_hash(payload.manifest),
            created_by=actor,
        )
        session.add(version)
        session.flush()
        audit_record(
            session,
            actor=actor,
            action="create_workflow_version",
            entity_type="workflow_version",
            entity_id=version.version_id,
            details={"workflow_id": workflow_id, "version_number": version.version_number},
        )
        session.commit()
        data = WorkflowVersionSchema.model_validate(version)
    return envelope_response(data, status_code=201)


async def get_version(request: Request) -> JSONResponse:
    require_user(request)
    version_id = request.path_params["version_id"]
    factory = get_session_factory(request)
    with factory() as session:
        version = _get_version_or_404(session, version_id)
        data = WorkflowVersionSchema.model_validate(version)
    return envelope_response(data)


async def update_version(request: Request) -> JSONResponse:
    version_id = request.path_params["version_id"]
    body = await parse_json_object(request)
    payload = WorkflowVersionUpdateRequest.model_validate(body)
    actor = require_scopes(request, [SCOPE_OPERATOR])
    _parse_manifest_or_400(payload.manifest)

    factory = get_session_factory(request)
    with factory() as session:
        version = _get_version_or_404(session, version_id)
        if version.status != "draft":
            raise HTTPException(
                status_code=409, detail="only draft versions can be edited (published is immutable)"
            )
        if payload.manifest_hash != version.manifest_hash:
            raise HTTPException(
                status_code=409,
                detail=(
                    "manifest_hash mismatch (stale draft): reload before editing. "
                    f"expected {version.manifest_hash!r}"
                ),
            )
        version.manifest = payload.manifest
        version.manifest_hash = compute_manifest_hash(payload.manifest)
        # Editing invalidates a prior compile.
        version.compiled_artifact_uri = None
        version.validation_report = None
        audit_record(
            session,
            actor=actor,
            action="update_workflow_version",
            entity_type="workflow_version",
            entity_id=version.version_id,
            details={"manifest_hash": version.manifest_hash},
        )
        session.commit()
        data = WorkflowVersionSchema.model_validate(version)
    return envelope_response(data)


async def validate_version(request: Request) -> JSONResponse:
    require_user(request)
    version_id = request.path_params["version_id"]
    factory = get_session_factory(request)
    with factory() as session:
        version = _get_version_or_404(session, version_id)
        resolver = resolver_from_session(session)
        try:
            manifest = parse_manifest(version.manifest)
        except (WorkflowManifestError, ValueError) as exc:
            return envelope_response_dict(_manifest_parse_error_report(version.manifest, exc))
        skill_names = {str(name) for name in session.execute(select(CaliberSkill.name)).scalars()}
        report = validate_manifest(manifest, resolver=resolver, skill_names=skill_names)
        _enrich_subworkflow_validation(session, manifest, report)
    return envelope_response_dict(report.to_dict())


async def compile_version_route(request: Request) -> JSONResponse:
    version_id = request.path_params["version_id"]
    require_scopes(request, [SCOPE_OPERATOR])
    factory = get_session_factory(request)
    with factory() as session:
        version = _get_version_or_404(session, version_id)
        try:
            result = compile_version(session, version, persist=True)
        except CompileError as exc:
            metrics.record_workflow_compile(ok=False)
            report = exc.report or {
                "valid": False,
                "errors": [
                    {"code": "compile_error", "path": "", "message": str(exc), "severity": "error"}
                ],
                "warnings": [],
            }
            return JSONResponse(
                {"detail": str(exc), "status_code": 400, "report": report}, status_code=400
            )
        metrics.record_workflow_compile(ok=True, duration_ms=result.compile_ms)
        session.commit()
        payload = {
            "version_id": version.version_id,
            "compiled_artifact_uri": version.compiled_artifact_uri,
            "compiler_version": version.compiler_version,
            "manifest_hash": version.manifest_hash,
            "report": result.report,
            "generated_python": result.generated_python,
            "requirements": result.requirements,
            "compile_ms": result.compile_ms,
            "cached": result.cached,
        }
    return envelope_response_dict(payload)


async def publish_version_route(request: Request) -> JSONResponse:
    version_id = request.path_params["version_id"]
    actor = require_scopes(request, [SCOPE_OPERATOR])
    factory = get_session_factory(request)
    with factory() as session:
        version = _get_version_or_404(session, version_id)
        already = version.status == "published"
        try:
            publish_version(session, version, actor=actor)
        except PublishError as exc:
            message = str(exc)
            status = 409 if "deprecated" in message else 400
            raise HTTPException(status_code=status, detail=message) from exc
        if not already:
            audit_record(
                session,
                actor=actor,
                action="publish_workflow_version",
                entity_type="workflow_version",
                entity_id=version.version_id,
                details={"version_number": version.version_number},
            )
        session.commit()
        data = WorkflowVersionSchema.model_validate(version)
    return envelope_response(data)


async def preview_run_route(request: Request) -> JSONResponse:
    version_id = request.path_params["version_id"]
    body = await parse_json_object(request)
    payload = PreviewRunRequest.model_validate(body)
    require_scopes(request, [SCOPE_OPERATOR])
    config = request.app.state.config
    factory = get_session_factory(request)
    with factory() as session:
        version = _get_version_or_404(session, version_id)
        try:
            result = run_preview(
                session,
                version,
                _input_to_text(payload.input),
                session_id=payload.session_id,
                config=config,
                manifest_override=payload.manifest,
            )
        except (CompileError, WorkflowManifestError, ValueError, RuntimeError) as exc:
            raise HTTPException(status_code=400, detail=f"cannot preview: {exc}") from exc
    metrics.record_workflow_preview(str(result.get("status", "error")))
    return envelope_response_dict(result)


async def run_version_route(request: Request) -> JSONResponse:
    """Execute a workflow version as a real, persisted manual run.

    Unlike preview-run this path uses real tool bindings and the configured
    workflow executor. With ``CALIBER_LLM_PROVIDER=openai`` it performs actual
    LLM calls; with the default ``fake`` provider it remains deterministic.
    """
    version_id = request.path_params["version_id"]
    body = await parse_json_object(request)
    payload = WorkflowRunRequest.model_validate(body)
    actor = require_scopes(request, [SCOPE_OPERATOR])
    if payload.manifest is not None:
        _parse_manifest_or_400(payload.manifest)
    config = request.app.state.config
    factory = get_session_factory(request)
    event_bus = getattr(request.app.state, "event_bus", None)
    try:
        result = await asyncio.to_thread(
            _run_workflow_version_sync,
            factory,
            version_id,
            payload,
            config,
            event_bus,
            actor,
        )
    except (CompileError, WorkflowManifestError, ValueError, RuntimeError) as exc:
        raise HTTPException(status_code=400, detail=f"cannot run workflow: {exc}") from exc
    return envelope_response_dict(result)


def _run_workflow_version_sync(  # noqa: PLR0915 - run orchestration + file workspace
    factory: Any,
    version_id: str,
    payload: WorkflowRunRequest,
    config: Any,
    event_bus: Any,
    actor: str,
) -> dict[str, Any]:
    input_text = _input_to_text(payload.input)
    alias = payload.alias or "manual"
    started = datetime.now(timezone.utc)
    steps: list[dict[str, Any]] = []
    with factory() as session:
        version = _get_version_or_404(session, version_id)
        workflow = session.get(CaliberWorkflow, version.workflow_id)
        if workflow is None:
            raise HTTPException(
                status_code=404, detail=f"workflow {version.workflow_id!r} not found"
            )
        if workflow.status == "paused":
            raise HTTPException(
                status_code=409, detail="workflow is paused; resume it before running"
            )
        if workflow.status == "archived":
            raise HTTPException(status_code=409, detail="archived workflows cannot be run")

        plan = build_plan(
            session,
            version,
            alias=alias,
            manifest_override=payload.manifest,
            config=config,
            session_factory=factory,
        )
        executor = build_executor(config, ir=plan.ir)
        manifest_metadata = _manifest_summary_metadata(version, payload.manifest)
        manifest_snapshot = (
            _clone_manifest(payload.manifest)
            if payload.manifest is not None
            else _clone_manifest(version.manifest)
        )
        run = CaliberWorkflowRun(
            workflow_run_id=new_workflow_run_id(),
            workflow_id=workflow.workflow_id,
            workflow_version_id=version.version_id,
            deployment_alias=alias,
            trace_id=f"workflow-{version.workflow_id}-{started.timestamp():.0f}",
            session_id=payload.session_id,
            status="running",
            started_at=started,
            manifest_snapshot=manifest_snapshot,
            summary={
                "preview": False,
                **manifest_metadata,
                "node_path": [],
                "steps": [],
                "logs": [
                    {
                        "level": "info",
                        "message": "manual workflow run started",
                        "actor": actor,
                    }
                ],
            },
        )
        session.add(run)
        session.flush()
        run_id = run.workflow_run_id
        session.commit()

        # Create the run's working directory and wire run-scoped file tools
        # (storage doc §4.2, §4.8). Best-effort: a storage failure must not abort
        # the run, but a bad input_files reference is a client error (400).
        wd_service = WorkingDirectoryService(
            build_backend(config.workflow_storage), config.workflow_storage
        )
        wd_ctx = wd_service.create_run_workspace(
            workflow_id=workflow.workflow_id, workflow_run_id=run_id
        )
        if payload.input_files:
            try:
                wd_service.materialize_input_files(
                    session, wd_ctx, payload.input_files, actor=actor
                )
            except StorageError as exc:
                raise ValueError(f"cannot bind input files: {exc}") from exc
            session.commit()
        # READ file tools, bound to this run, available to any agent that binds
        # them by name (storage doc §4.4). Injected via execute(extra_tools=...).
        extra_tools = bind_run_read_tools(wd_service, session, wd_ctx)
        # Agent long-term memory (mem0), scoped to the workflow so it persists
        # across runs. Empty dict when memory is disabled, so this is a no-op.
        extra_tools = {
            **extra_tools,
            **bind_run_memory_tools(config, agent_id=wd_ctx.workflow_id),
        }

        _publish_workflow_event(
            event_bus,
            {
                "type": "workflow.run.started",
                "workflow_id": workflow.workflow_id,
                "workflow_version_id": version.version_id,
                "workflow_run_id": run_id,
                "status": "running",
                "alias": alias,
            },
        )

        def _on_node_start(node_id: str, node: Any, _inputs: dict[str, Any]) -> None:
            payload = _node_started_to_dict(node_id, node.node_type)
            row = session.get(CaliberWorkflowRun, run_id)
            if row is not None:
                row.status = "running"
                row.current_node_id = node_id
                session.commit()
            _publish_workflow_event(
                event_bus,
                {
                    "type": "workflow.run.node_started",
                    "workflow_id": workflow.workflow_id,
                    "workflow_version_id": version.version_id,
                    "workflow_run_id": run_id,
                    "status": "running",
                    **payload,
                },
            )

        def _on_step(step: NodeStep) -> None:
            step_payload = _step_to_dict(step)
            steps.append(step_payload)
            row = session.get(CaliberWorkflowRun, run_id)
            if row is not None:
                prior_summary = dict(row.summary or {})
                row.status = "running"
                row.current_node_id = step.node_id
                row.summary = {
                    "preview": False,
                    **_copy_manifest_summary_metadata(prior_summary),
                    "node_path": [s["node_id"] for s in steps],
                    "steps": steps,
                    "logs": [
                        {
                            "level": "info",
                            "message": f"{step.node_type} {step.node_id} -> {step.status}",
                        }
                    ],
                }
                session.commit()
            _publish_workflow_event(
                event_bus,
                {
                    "type": "workflow.run.step",
                    "workflow_id": workflow.workflow_id,
                    "workflow_version_id": version.version_id,
                    "workflow_run_id": run_id,
                    "step": step_payload,
                },
            )

        result = execute(
            plan,
            input_text,
            executor=executor,
            session_id=payload.session_id,
            preview=False,
            on_step=_on_step,
            on_node_start=_on_node_start,
            extra_tools=extra_tools,
        )
        # Persist any named file artifacts the run produced (e.g. kg.json +
        # report.html from a python_code node's ``result.artifacts``) into the run
        # workspace, so a single run can emit multiple files despite the output
        # node flattening to one string. Best-effort: a storage failure must not
        # fail an otherwise-successful run.
        for art_path, art_content in (result.artifacts or {}).items():
            try:
                lower = art_path.lower()
                media = (
                    "application/json"
                    if lower.endswith(".json")
                    else "text/html"
                    if lower.endswith((".html", ".htm"))
                    else "text/plain"
                )
                wd_service.write_artifact(
                    session,
                    wd_ctx,
                    path=art_path,
                    data=str(art_content).encode("utf-8"),
                    media_type=media,
                    actor=actor,
                    kind="artifact",
                )
            except StorageError:
                logger.warning("could not persist run artifact %r", art_path, exc_info=True)
        if result.artifacts:
            session.commit()
        row = session.get(CaliberWorkflowRun, run_id)
        if row is not None:
            row.status = result.status
            row.completed_at = datetime.now(timezone.utc)
            if result.mlflow_run_id:
                row.mlflow_run_id = result.mlflow_run_id
            prior_summary = dict(row.summary or {})
            summary = workflow_run_summary(result, preview=False)
            summary.update(_copy_manifest_summary_metadata(prior_summary))
            # Surface uploaded inputs + produced artifacts in run detail (§4.5).
            summary.update(wd_service.run_file_summary(session, run_id))
            row.summary = summary
            session.commit()
            session.refresh(row)
        event_type = (
            "workflow.run.completed" if result.status == "completed" else "workflow.run.failed"
        )
        _publish_workflow_event(
            event_bus,
            {
                "type": event_type,
                "workflow_id": workflow.workflow_id,
                "workflow_version_id": version.version_id,
                "workflow_run_id": run_id,
                "status": result.status,
                "error": result.error,
            },
        )
        return _run_result_payload(run_id, result, preview=False)


def _input_to_text(value: object) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    return json.dumps(value, sort_keys=True, default=str)


def _step_to_dict(step: NodeStep) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "node_id": step.node_id,
        "node_type": step.node_type,
        "status": step.status,
        "output": step.output,
        "tool_calls": step.tool_calls,
        "handoff_target": step.handoff_target,
        "detail": step.detail,
        "duration_ms": step.duration_ms,
        "input_by_port": dict(step.input_by_port or {}),
        "output_by_port": dict(step.output_by_port or {}),
    }
    if step.tokens > 0:
        payload["tokens"] = step.tokens
    if step.prompt_tokens > 0:
        payload["prompt_tokens"] = step.prompt_tokens
    if step.completion_tokens > 0:
        payload["completion_tokens"] = step.completion_tokens
    if step.cached_prompt_tokens > 0:
        payload["cached_prompt_tokens"] = step.cached_prompt_tokens
    if step.cost_usd > 0:
        payload["cost_usd"] = step.cost_usd
    if isinstance(step.model, str) and step.model:
        payload["model"] = step.model
    if isinstance(step.prompt_version, str) and step.prompt_version:
        payload["prompt_version"] = step.prompt_version
    return payload


def _node_started_to_dict(node_id: str, node_type: Any) -> dict[str, Any]:
    normalized_node_type = getattr(node_type, "value", node_type)
    return {
        "node_id": node_id,
        "node_type": str(normalized_node_type),
    }


def _run_result_payload(
    workflow_run_id: str,
    result: WorkflowRunResult,
    *,
    preview: bool,
) -> dict[str, Any]:
    return {
        "workflow_run_id": workflow_run_id,
        "status": result.status,
        "output": result.output,
        "error": result.error,
        "tokens": result.tokens,
        "tags": result.tags,
        "steps": [_step_to_dict(step) for step in result.steps],
        "guardrail_results": result.guardrail_results,
        "artifacts": result.artifacts,
        "preview": preview,
    }


def _publish_workflow_event(event_bus: Any, payload: dict[str, Any]) -> None:
    publish = getattr(event_bus, "publish", None)
    if callable(publish):
        try:
            publish(payload)
        except Exception:
            logger.warning(
                "failed to publish workflow event type=%r",
                payload.get("type"),
                exc_info=True,
            )


async def propose_patch_route(request: Request) -> JSONResponse:
    """Generate a CALIBER workflow patch candidate from failure evidence (plan §17.3).

    Localizes the failure, generates id-keyed semantic patch ops, materializes +
    compiles the candidate manifest, and persists a ``CaliberWorkflowPatch`` for
    the approval UI. Returns the diagnosis, patch, graph diff, and the
    candidate's validation report.
    """
    version_id = request.path_params["version_id"]
    body = await parse_json_object(request)
    payload = ProposePatchRequest.model_validate(body)
    actor = require_scopes(request, [SCOPE_OPERATOR])
    factory = get_session_factory(request)
    with factory() as session:
        version = _get_version_or_404(session, version_id)
        resolver = resolver_from_session(session)
        try:
            base = parse_manifest(version.manifest)
        except (WorkflowManifestError, ValueError) as exc:
            raise HTTPException(status_code=400, detail=f"invalid base manifest: {exc}") from exc

        evidence = dict(payload.evidence)
        evidence.setdefault("workflow_id", version.workflow_id)
        evidence.setdefault("workflow_version_id", version.version_id)
        diagnosis = localize_failure(evidence)
        try:
            candidate = generate_workflow_patch(base, diagnosis, resolver=resolver)
        except (CompileError, WorkflowManifestError, ValueError) as exc:
            raise HTTPException(status_code=400, detail=f"cannot generate patch: {exc}") from exc

        # Validate the candidate manifest compiles (graph + tools resolve).
        candidate_valid = True
        candidate_report: dict[str, Any] = {"valid": True, "errors": [], "warnings": []}
        if candidate.patch_kind == "workflow_manifest":
            cand_manifest = parse_manifest(candidate.candidate_manifest)
            report = validate_manifest(cand_manifest, resolver=resolver)
            candidate_valid = report.valid
            candidate_report = report.to_dict()

        patch = CaliberWorkflowPatch(
            patch_id=new_workflow_patch_id(),
            job_id=payload.job_id,
            workflow_id=version.workflow_id,
            base_version_id=version.version_id,
            candidate_manifest=candidate.candidate_manifest,
            semantic_ops=candidate.semantic_ops,
            patch_summary=candidate.summary,
            graph_diff=candidate.graph_diff,
            risk_summary=f"patch_kind={candidate.patch_kind}; "
            f"components={','.join(diagnosis.affected_components)}",
        )
        session.add(patch)
        session.flush()
        metrics.record_workflow_patch(candidate.patch_kind)
        audit_record(
            session,
            actor=actor,
            action="generate_workflow_patch",
            entity_type="workflow",
            entity_id=version.workflow_id,
            details={
                "patch_id": patch.patch_id,
                "base_version_id": version.version_id,
                "patch_kind": candidate.patch_kind,
                "components": diagnosis.affected_components,
            },
        )
        session.commit()
        result = {
            "patch_id": patch.patch_id,
            "diagnosis": diagnosis.to_dict(),
            "patch_kind": candidate.patch_kind,
            "semantic_ops": candidate.semantic_ops,
            "summary": candidate.summary,
            "prompt_suggestion": candidate.prompt_suggestion,
            "graph_diff": candidate.graph_diff,
            "candidate_manifest": candidate.candidate_manifest,
            "candidate_valid": candidate_valid,
            "candidate_validation": candidate_report,
        }
    return envelope_response_dict(result)


def _copilot_grounding(session: Session) -> dict[str, list[str]]:
    """Best-effort registry context for the copilot.

    Lists the names of registered tools, skills, and eval datasets so the
    model proposes *resolvable* references rather than inventing them
    (Lakeflow "knows your data" → Caliber "knows your artifacts"). Kept
    intentionally cheap — names only, capped — since it is advisory context,
    not a correctness boundary; the route re-validates whatever the model
    returns against the resolver.
    """
    cap = 250
    tools = list(
        session.execute(
            select(CaliberToolRegistry.name)
            .where(CaliberToolRegistry.status != "archived")
            .distinct()
            .limit(cap)
        ).scalars()
    )
    skills = list(session.execute(select(CaliberSkill.name).distinct().limit(cap)).scalars())
    datasets = list(
        session.execute(select(CaliberEvalDataset.name).distinct().limit(cap)).scalars()
    )
    return {
        "tools": sorted({str(t) for t in tools if t}),
        "skills": sorted({str(s) for s in skills if s}),
        "eval_datasets": sorted({str(d) for d in datasets if d}),
    }


def _manifest_proposal_envelope(
    *,
    base: Any,
    edit: Any,
    usage: Any,
    resolver: Any,
    grounding: dict[str, list[str]],
    stage: str,
) -> JSONResponse:
    """Validate + diff a provider-proposed manifest into the accept/reject envelope.

    Shared by the copilot-edit and plan-build routes: both ask the provider for
    a *full* manifest and surface it identically — parse (must be structurally
    diffable, else 422), semantic-validate (reported, not enforced), and
    graph-diff against ``base``. ``stage`` only flavours the 422 message.
    """
    try:
        proposed = parse_manifest(edit.manifest)
    except (WorkflowManifestError, ValueError) as exc:
        raise HTTPException(
            status_code=422, detail=f"{stage} proposed an unparseable manifest: {exc}"
        ) from exc
    report = validate_manifest(proposed, resolver=resolver)
    graph_diff = compute_graph_diff(base, proposed)
    return envelope_response_dict(
        {
            "proposed_manifest": proposed.to_dict(),
            "summary": edit.summary,
            "rationale": edit.rationale,
            "graph_diff": graph_diff,
            "valid": report.valid,
            "report": report.to_dict(),
            "grounding": grounding,
            "usage": {
                "input_tokens": usage.input_tokens,
                "output_tokens": usage.output_tokens,
                "cost_usd": usage.cost_usd,
            },
        }
    )


async def copilot_edit_route(request: Request) -> JSONResponse:
    """Propose a natural-language edit to a workflow manifest (in-canvas copilot).

    **Modify-in-place:** takes the open manifest plus an NL instruction, grounds
    the edit in the registry, asks the configured LLM provider for the *full*
    edited manifest, validates it parses + compiles, and returns the proposal +
    graph diff for an accept/reject UI. Nothing is persisted — the client
    applies an accepted manifest through the normal version-save path.

    With the default ``fake`` provider the manifest comes back unchanged (a
    safe no-op), so the dock works end-to-end without an LLM configured.
    """
    version_id = request.path_params["version_id"]
    body = await parse_json_object(request)
    payload = CopilotEditRequest.model_validate(body)
    require_scopes(request, [SCOPE_OPERATOR])
    factory = get_session_factory(request)
    config = request.app.state.config
    with factory() as session:
        version = _get_version_or_404(session, version_id)
        resolver = resolver_from_session(session)
        base_raw = payload.manifest if payload.manifest is not None else version.manifest
        try:
            base = parse_manifest(base_raw)
        except (WorkflowManifestError, ValueError) as exc:
            raise HTTPException(status_code=400, detail=f"invalid base manifest: {exc}") from exc

        grounding = _copilot_grounding(session)
        provider = build_provider(config)
        ctx = WorkflowEditContext(
            instruction=payload.instruction,
            manifest=base.to_dict(),
            grounding=grounding,
        )
        try:
            edit, usage = provider.propose_workflow_edit(ctx)
        except LLMProviderError as exc:
            raise HTTPException(status_code=502, detail=f"copilot edit failed: {exc}") from exc

        return _manifest_proposal_envelope(
            base=base,
            edit=edit,
            usage=usage,
            resolver=resolver,
            grounding=grounding,
            stage="copilot",
        )


async def plan_build_route(request: Request) -> JSONResponse:
    """Author a workflow manifest from a plain-language goal (plan-to-build).

    Blank-slate sibling of :func:`copilot_edit_route`: takes a goal plus the
    open canvas (used only as the diff base + identity source), grounds the
    generation in the registry, asks the provider to author the *full* graph
    toward the goal, then validates + diffs it exactly like the copilot so the
    same accept/reject overlay renders the proposal. Nothing is persisted — the
    client applies an accepted manifest through the normal version-save path.

    With the default ``fake`` provider the manifest comes back unchanged (an
    empty diff), so the Plan tab works end-to-end without an LLM configured.
    """
    version_id = request.path_params["version_id"]
    body = await parse_json_object(request)
    payload = PlanBuildRequest.model_validate(body)
    require_scopes(request, [SCOPE_OPERATOR])
    factory = get_session_factory(request)
    config = request.app.state.config
    with factory() as session:
        version = _get_version_or_404(session, version_id)
        resolver = resolver_from_session(session)
        base_raw = payload.manifest if payload.manifest is not None else version.manifest
        try:
            base = parse_manifest(base_raw)
        except (WorkflowManifestError, ValueError) as exc:
            raise HTTPException(status_code=400, detail=f"invalid base manifest: {exc}") from exc

        grounding = _copilot_grounding(session)
        provider = build_provider(config)
        ctx = WorkflowGenerationContext(
            goal=payload.goal,
            manifest=base.to_dict(),
            grounding=grounding,
        )
        try:
            edit, usage = provider.generate_workflow_from_goal(ctx)
        except LLMProviderError as exc:
            raise HTTPException(status_code=502, detail=f"plan-build failed: {exc}") from exc

        return _manifest_proposal_envelope(
            base=base,
            edit=edit,
            usage=usage,
            resolver=resolver,
            grounding=grounding,
            stage="plan-build",
        )


async def list_patches_route(request: Request) -> JSONResponse:
    require_user(request)
    workflow_id = request.path_params["workflow_id"]
    factory = get_session_factory(request)
    with factory() as session:
        if session.get(CaliberWorkflow, workflow_id) is None:
            raise HTTPException(status_code=404, detail=f"workflow {workflow_id!r} not found")
        rows = (
            session.execute(
                select(CaliberWorkflowPatch)
                .where(CaliberWorkflowPatch.workflow_id == workflow_id)
                .order_by(CaliberWorkflowPatch.created_at.desc())
            )
            .scalars()
            .all()
        )
        items = [WorkflowPatchSchema.model_validate(r) for r in rows]
    return envelope_response(items)


def _parse_run_list_int_param(
    request: Request,
    *,
    name: str,
    default: int,
    minimum: int = 0,
    maximum: int | None = None,
) -> int:
    raw = request.query_params.get(name)
    if raw is None or not raw.strip():
        return default
    try:
        value = int(raw)
    except ValueError as exc:
        raise HTTPException(
            status_code=400, detail=f"query param {name!r} must be an integer"
        ) from exc
    if value < minimum:
        raise HTTPException(
            status_code=400,
            detail=f"query param {name!r} must be greater than or equal to {minimum}",
        )
    if maximum is not None and value > maximum:
        raise HTTPException(
            status_code=400,
            detail=f"query param {name!r} must be less than or equal to {maximum}",
        )
    return value


def _run_sort_expression() -> Any:
    return func.coalesce(CaliberWorkflowRun.started_at, CaliberWorkflowRun.queued_at)


def _run_artifact_persistence_summary(
    run: CaliberWorkflowRun,
) -> dict[str, Any] | None:
    summary = run.summary if isinstance(run.summary, dict) else None
    raw = summary.get("artifact_persistence") if isinstance(summary, dict) else None
    return raw if isinstance(raw, dict) else None


def _run_artifact_persistence_status(
    run: CaliberWorkflowRun,
) -> Literal["failed", "persisted"] | None:
    artifact_persistence = _run_artifact_persistence_summary(run)
    status = artifact_persistence.get("status") if isinstance(artifact_persistence, dict) else None
    if status == "failed":
        return "failed"
    if status == "persisted":
        return "persisted"
    return None


def _run_matches_artifact_filter(
    run: CaliberWorkflowRun,
    artifact_filter: Literal["failed", "persisted"] | None,
) -> bool:
    if artifact_filter is None:
        return True
    return _run_artifact_persistence_status(run) == artifact_filter


def _run_matches_search(run: CaliberWorkflowRun, query: str | None) -> bool:
    if not query:
        return True
    artifact_persistence = _run_artifact_persistence_summary(run)
    artifact_names_raw = (
        artifact_persistence.get("artifact_names")
        if isinstance(artifact_persistence, dict)
        else None
    )
    artifact_names = (
        [item.strip() for item in artifact_names_raw if isinstance(item, str) and item.strip()]
        if isinstance(artifact_names_raw, list)
        else []
    )
    recent_persisted_keys_raw = (
        artifact_persistence.get("recent_persisted_keys")
        if isinstance(artifact_persistence, dict)
        else None
    )
    recent_persisted_keys = (
        [
            item.strip()
            for item in recent_persisted_keys_raw
            if isinstance(item, str) and item.strip()
        ]
        if isinstance(recent_persisted_keys_raw, list)
        else []
    )
    searchable: list[str | None] = [
        run.workflow_run_id,
        run.trace_id,
        run.workflow_version_id,
        run.deployment_alias,
        run.status,
        run.error_summary,
        run.current_node_id,
        artifact_persistence.get("bucket")
        if isinstance(artifact_persistence, dict)
        and isinstance(artifact_persistence.get("bucket"), str)
        else None,
        "artifact upload failed" if _run_artifact_persistence_status(run) == "failed" else None,
        "artifacts stored" if _run_artifact_persistence_status(run) == "persisted" else None,
        artifact_persistence.get("error")
        if isinstance(artifact_persistence, dict)
        and isinstance(artifact_persistence.get("error"), str)
        else None,
        artifact_persistence.get("failed_object_key")
        if isinstance(artifact_persistence, dict)
        and isinstance(artifact_persistence.get("failed_object_key"), str)
        else None,
    ]
    searchable.extend(artifact_names)
    searchable.extend(recent_persisted_keys)
    return any(
        query in value.lower() for value in searchable if isinstance(value, str) and value.strip()
    )


def _parse_run_history_filters(
    request: Request,
) -> tuple[str | None, Literal["failed", "persisted"] | None]:
    search = request.query_params.get("search")
    normalized_search = (
        search.strip().lower() if isinstance(search, str) and search.strip() else None
    )
    raw_artifact_filter = request.query_params.get("artifact_persistence")
    artifact_filter: Literal["failed", "persisted"] | None = None
    if raw_artifact_filter is not None and raw_artifact_filter.strip():
        if raw_artifact_filter not in {"failed", "persisted"}:
            raise HTTPException(
                status_code=400,
                detail="query param 'artifact_persistence' must be one of: failed, persisted",
            )
        artifact_filter = cast(Literal["failed", "persisted"], raw_artifact_filter)
    return normalized_search, artifact_filter


def _parse_run_list_filters(
    request: Request,
) -> tuple[str | None, Literal["failed", "persisted"] | None, int, int]:
    normalized_search, artifact_filter = _parse_run_history_filters(request)
    limit = _parse_run_list_int_param(
        request,
        name="limit",
        default=_RUN_LIST_DEFAULT_LIMIT,
        minimum=1,
        maximum=_RUN_LIST_MAX_LIMIT,
    )
    cursor = _parse_run_list_int_param(
        request,
        name="cursor",
        default=0,
        minimum=0,
    )
    return normalized_search, artifact_filter, limit, cursor


def _workflow_run_list_query(workflow_id: str) -> Any:
    return (
        select(CaliberWorkflowRun)
        .where(CaliberWorkflowRun.workflow_id == workflow_id)
        .order_by(_run_sort_expression().desc(), CaliberWorkflowRun.workflow_run_id.desc())
    )


def _list_unfiltered_runs_page(
    session: Session,
    *,
    workflow_id: str,
    limit: int,
    cursor: int,
) -> tuple[list[CaliberWorkflowRun], str | None]:
    page_rows = cast(
        list[CaliberWorkflowRun],
        session.execute(_workflow_run_list_query(workflow_id).offset(cursor).limit(limit + 1))
        .scalars()
        .all(),
    )
    has_more = len(page_rows) > limit
    rows = page_rows[:limit]
    next_cursor = str(cursor + len(rows)) if has_more else None
    return rows, next_cursor


def _list_filtered_runs_page(
    session: Session,
    *,
    workflow_id: str,
    search: str | None,
    artifact_filter: Literal["failed", "persisted"] | None,
    limit: int,
    cursor: int,
) -> tuple[list[CaliberWorkflowRun], str | None]:
    rows: list[CaliberWorkflowRun] = []
    matched_count = 0
    raw_offset = 0
    batch_size = max(_RUN_LIST_FILTER_SCAN_BATCH_SIZE, limit * 2)
    has_more = False
    while True:
        batch = (
            session.execute(
                _workflow_run_list_query(workflow_id).offset(raw_offset).limit(batch_size)
            )
            .scalars()
            .all()
        )
        if not batch:
            break
        raw_offset += len(batch)
        for row in batch:
            if not _run_matches_artifact_filter(row, artifact_filter):
                continue
            if not _run_matches_search(row, search):
                continue
            if matched_count < cursor:
                matched_count += 1
                continue
            if len(rows) >= limit:
                has_more = True
                break
            rows.append(row)
            matched_count += 1
        if has_more or len(batch) < batch_size:
            break
    next_cursor = str(cursor + len(rows)) if has_more else None
    return rows, next_cursor


def _run_history_stats(
    rows: list[CaliberWorkflowRun],
    *,
    workflow_id: str,
    search: str | None,
    artifact_filter: Literal["failed", "persisted"] | None,
) -> WorkflowRunHistoryStatsSchema:
    waiting_event_runs = 0
    failed_artifact_runs = 0
    persisted_artifact_runs = 0
    matching_runs = 0
    for row in rows:
        if row.status == "waiting_event":
            waiting_event_runs += 1
        artifact_status = _run_artifact_persistence_status(row)
        if artifact_status == "failed":
            failed_artifact_runs += 1
        elif artifact_status == "persisted":
            persisted_artifact_runs += 1
        if _run_matches_artifact_filter(row, artifact_filter) and _run_matches_search(row, search):
            matching_runs += 1
    return WorkflowRunHistoryStatsSchema(
        workflow_id=workflow_id,
        total_runs=len(rows),
        matching_runs=matching_runs,
        waiting_event_runs=waiting_event_runs,
        artifact_persistence=WorkflowRunHistoryArtifactStatsSchema(
            failed=failed_artifact_runs,
            persisted=persisted_artifact_runs,
        ),
    )


async def list_runs_route(request: Request) -> JSONResponse:
    """List indexed workflow runs (the §16.7.2 Runs tab + localization index)."""
    require_user(request)
    workflow_id = request.path_params["workflow_id"]
    normalized_search, artifact_filter, limit, cursor = _parse_run_list_filters(request)
    factory = get_session_factory(request)
    with factory() as session:
        if session.get(CaliberWorkflow, workflow_id) is None:
            raise HTTPException(status_code=404, detail=f"workflow {workflow_id!r} not found")
        if normalized_search is None and artifact_filter is None:
            rows, next_cursor = _list_unfiltered_runs_page(
                session,
                workflow_id=workflow_id,
                limit=limit,
                cursor=cursor,
            )
        else:
            rows, next_cursor = _list_filtered_runs_page(
                session,
                workflow_id=workflow_id,
                search=normalized_search,
                artifact_filter=artifact_filter,
                limit=limit,
                cursor=cursor,
            )
        items = [WorkflowRunSchema.model_validate(r).model_dump(mode="json") for r in rows]
    return JSONResponse({"data": items, "next_cursor": next_cursor})


async def run_history_stats_route(request: Request) -> JSONResponse:
    """Return exact workflow run-history totals for runs-tab triage."""
    require_user(request)
    workflow_id = request.path_params["workflow_id"]
    normalized_search, artifact_filter = _parse_run_history_filters(request)
    factory = get_session_factory(request)
    with factory() as session:
        if session.get(CaliberWorkflow, workflow_id) is None:
            raise HTTPException(status_code=404, detail=f"workflow {workflow_id!r} not found")
        rows = cast(
            list[CaliberWorkflowRun],
            session.execute(_workflow_run_list_query(workflow_id)).scalars().all(),
        )
        stats = _run_history_stats(
            rows,
            workflow_id=workflow_id,
            search=normalized_search,
            artifact_filter=artifact_filter,
        )
    return envelope_response(stats)


async def run_by_trace_route(request: Request) -> JSONResponse:
    """Resolve a trace id to its workflow run (feedback localization, §11.2)."""
    require_user(request)
    trace_id = request.path_params["trace_id"]
    factory = get_session_factory(request)
    with factory() as session:
        row = (
            session.execute(
                select(CaliberWorkflowRun)
                .where(CaliberWorkflowRun.trace_id == trace_id)
                .order_by(
                    func.coalesce(
                        CaliberWorkflowRun.started_at, CaliberWorkflowRun.queued_at
                    ).desc()
                )
            )
            .scalars()
            .first()
        )
        if row is None:
            raise HTTPException(
                status_code=404, detail=f"no workflow run indexed for trace {trace_id!r}"
            )
        data = WorkflowRunSchema.model_validate(row)
    return envelope_response(data)


async def export_manifest_route(request: Request) -> PlainTextResponse:
    require_user(request)
    version_id = request.path_params["version_id"]
    factory = get_session_factory(request)
    with factory() as session:
        version = _get_version_or_404(session, version_id)
        manifest = version.manifest
    text = yaml.safe_dump(manifest, sort_keys=False, default_flow_style=False)
    return PlainTextResponse(text, media_type="application/x-yaml")


async def export_python_route(request: Request) -> PlainTextResponse:
    require_user(request)
    version_id = request.path_params["version_id"]
    factory = get_session_factory(request)
    with factory() as session:
        version = _get_version_or_404(session, version_id)
        # Prefer the immutable stored bundle (plan §13.3) so an exported
        # published version is byte-identical to what was compiled/approved.
        bundle = version.compiled_bundle
        if isinstance(bundle, dict) and isinstance(bundle.get("generated_python"), str):
            return PlainTextResponse(bundle["generated_python"], media_type="text/x-python")
        resolver = resolver_from_session(session)
        try:
            result = compile_workflow(
                parse_manifest(version.manifest),
                resolver=resolver,
                version=str(version.version_number),
            )
        except (CompileError, WorkflowManifestError, ValueError) as exc:
            raise HTTPException(status_code=400, detail=f"cannot export: {exc}") from exc
        code = result.generated_python
    return PlainTextResponse(code, media_type="text/x-python")


def envelope_response_dict(payload: dict[str, Any]) -> JSONResponse:
    """Like ``envelope_response`` but for a plain dict (not a Pydantic model)."""
    return JSONResponse({"data": payload})


def register(app: Starlette) -> None:
    app.routes.append(Route(LIST_PATH, list_versions, methods=["GET"]))
    app.routes.append(Route(LIST_PATH, create_version, methods=["POST"]))
    app.routes.append(Route(DETAIL_PATH, get_version, methods=["GET"]))
    app.routes.append(Route(DETAIL_PATH, update_version, methods=["PATCH"]))
    app.routes.append(Route(VALIDATE_PATH, validate_version, methods=["POST"]))
    app.routes.append(Route(COMPILE_PATH, compile_version_route, methods=["POST"]))
    app.routes.append(Route(PUBLISH_PATH, publish_version_route, methods=["POST"]))
    app.routes.append(Route(PREVIEW_PATH, preview_run_route, methods=["POST"]))
    app.routes.append(Route(RUN_PATH, run_version_route, methods=["POST"]))
    app.routes.append(Route(PROPOSE_PATCH_PATH, propose_patch_route, methods=["POST"]))
    app.routes.append(Route(COPILOT_EDIT_PATH, copilot_edit_route, methods=["POST"]))
    app.routes.append(Route(PLAN_BUILD_PATH, plan_build_route, methods=["POST"]))
    app.routes.append(Route(PATCHES_PATH, list_patches_route, methods=["GET"]))
    app.routes.append(Route(RUNS_PATH, list_runs_route, methods=["GET"]))
    app.routes.append(Route(RUN_STATS_PATH, run_history_stats_route, methods=["GET"]))
    app.routes.append(Route(RUN_BY_TRACE_PATH, run_by_trace_route, methods=["GET"]))
    app.routes.append(Route(EXPORT_MANIFEST_PATH, export_manifest_route, methods=["GET"]))
    app.routes.append(Route(EXPORT_PYTHON_PATH, export_python_route, methods=["GET"]))
