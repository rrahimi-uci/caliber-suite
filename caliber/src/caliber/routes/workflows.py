"""``/caliber/workflows`` endpoints (plan §15.1).

CRUD for the logical workflow container. Versions, deployments, and runs hang
off the workflow and live in their own route modules. Archiving a workflow is
blocked while a production deployment exists (plan §19.9).
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import yaml
from sqlalchemy import delete, select
from starlette.applications import Starlette
from starlette.exceptions import HTTPException
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from caliber.audit import record as audit_record
from caliber.auth import SCOPE_OPERATOR, require_scopes, require_user, resolve_identity
from caliber.db.models import (
    CaliberAgentConfig,
    CaliberApprovalRequest,
    CaliberRefinementJob,
    CaliberRegressionRun,
    CaliberRollbackCheckpoint,
    CaliberRuntimeApprovalRequest,
    CaliberVerificationItem,
    CaliberWorkflow,
    CaliberWorkflowBenchmarkReport,
    CaliberWorkflowDeployment,
    CaliberWorkflowPatch,
    CaliberWorkflowPromotion,
    CaliberWorkflowRun,
    CaliberWorkflowRunCheckpoint,
    CaliberWorkflowRunEvent,
    CaliberWorkflowSessionMemory,
    CaliberWorkflowVersion,
)
from caliber.db.scoping import apply_visibility_filter, get_visible
from caliber.ids import (
    new_workflow_benchmark_report_id,
    new_workflow_id,
    new_workflow_version_id,
)
from caliber.routes._deps import (
    envelope_response,
    envelope_response_dict,
    get_session_factory,
    parse_json_object,
    visibility_param,
)
from caliber.schemas import (
    WorkflowBakeoffWorksheetSchema,
    WorkflowBenchmarkReportCreateRequest,
    WorkflowBenchmarkReportSchema,
    WorkflowBenchmarkReportUpdateRequest,
    WorkflowComponentCatalogSchema,
    WorkflowCreateRequest,
    WorkflowImportRequest,
    WorkflowSchema,
    WorkflowSessionMemoryClearResultSchema,
    WorkflowSessionMemoryEntrySchema,
    WorkflowTemplateCatalogSchema,
    WorkflowUpdateRequest,
    WorkflowVersionSchema,
)
from caliber.workflows.component_catalog import build_workflow_component_catalog
from caliber.workflows.cron import next_fires, validate_cron
from caliber.workflows.manifest import (
    WorkflowManifestError,
    compute_manifest_hash,
    parse_manifest,
)
from caliber.workflows.promoter import LIVE_ALIASES
from caliber.workflows.template_catalog import build_workflow_template_catalog
from caliber.workflows.validation import find_inline_secrets

LIST_PATH = "/ajax-api/2.0/mlflow/caliber/workflows"
IMPORT_PATH = "/ajax-api/2.0/mlflow/caliber/workflows/import"
DETAIL_PATH = "/ajax-api/2.0/mlflow/caliber/workflows/{workflow_id}"
SESSION_MEMORY_PATH = DETAIL_PATH + "/session-memory"
COMPONENTS_PATH = "/ajax-api/2.0/mlflow/caliber/workflow-components"
CRON_PREVIEW_PATH = "/ajax-api/2.0/mlflow/caliber/workflow-cron-preview"
TEMPLATES_PATH = "/ajax-api/2.0/mlflow/caliber/workflow-templates"
BENCHMARK_REPORTS_PATH = "/ajax-api/2.0/mlflow/caliber/workflow-benchmark-reports"
BENCHMARK_REPORT_DETAIL_PATH = BENCHMARK_REPORTS_PATH + "/{report_id}"

_LIST_STATUS_VALUES = frozenset({"active", "paused", "archived", "all"})
_BENCHMARK_REPORT_STATUS_VALUES = frozenset({"draft", "completed", "archived", "all"})
_UPDATABLE_FIELDS = ("name", "description", "owner", "status", "default_experiment_id")
logger = logging.getLogger("caliber.routes.workflows")


def _publish_workflow_event(request: Request, payload: dict[str, Any]) -> None:
    publish = getattr(getattr(request.app.state, "event_bus", None), "publish", None)
    if callable(publish):
        try:
            publish(payload)
        except Exception:
            logger.warning(
                "failed to publish workflow event type=%r",
                payload.get("type"),
                exc_info=True,
            )


def _normalize_session_memory_history(value: Any) -> list[dict[str, str]]:
    history: list[dict[str, str]] = []
    if not isinstance(value, list):
        return history
    for item in value:
        if not isinstance(item, dict):
            continue
        role = item.get("role")
        content = item.get("content")
        if role not in {"user", "assistant"}:
            continue
        if not isinstance(content, str) or not content.strip():
            continue
        history.append({"role": str(role), "content": content})
    return history


def _serialize_session_memory_entry(
    row: CaliberWorkflowSessionMemory,
) -> WorkflowSessionMemoryEntrySchema:
    history = _normalize_session_memory_history(row.message_history)
    last_user_message = next(
        (item["content"] for item in reversed(history) if item["role"] == "user"),
        None,
    )
    last_assistant_message = next(
        (item["content"] for item in reversed(history) if item["role"] == "assistant"),
        None,
    )
    return WorkflowSessionMemoryEntrySchema.model_validate(
        {
            "workflow_id": row.workflow_id,
            "node_id": row.node_id,
            "session_id": row.session_id,
            "message_history": history,
            "message_count": len(history),
            "turn_count": row.turn_count,
            "created_at": row.created_at,
            "updated_at": row.updated_at,
            "last_user_message": last_user_message,
            "last_assistant_message": last_assistant_message,
        }
    )


def _require_session_id(request: Request) -> str:
    session_id = request.query_params.get("session_id", "").strip()
    if not session_id:
        raise HTTPException(
            status_code=400,
            detail="session_id query parameter is required for workflow session memory access",
        )
    return session_id


def _optional_node_id(request: Request) -> str | None:
    node_id = request.query_params.get("node_id", "").strip()
    return node_id or None


def _normalize_benchmark_worksheet(value: Any) -> WorkflowBakeoffWorksheetSchema:
    if isinstance(value, WorkflowBakeoffWorksheetSchema):
        return value
    if not isinstance(value, dict):
        value = {}
    return WorkflowBakeoffWorksheetSchema.model_validate(value)


def _benchmark_entry_has_evidence(entry: Any) -> bool:
    if entry is None:
        return False
    return bool(
        getattr(entry, "status", "not_started") != "not_started"
        or getattr(entry, "minutes_to_first_success", "").strip()
        or getattr(entry, "evidence_links", "").strip()
        or getattr(entry, "notes", "").strip()
    )


def _serialize_benchmark_report(
    row: CaliberWorkflowBenchmarkReport,
) -> WorkflowBenchmarkReportSchema:
    worksheet = _normalize_benchmark_worksheet(row.worksheet)
    scenario_entries = list(worksheet.scenarios.values())
    captured_count = sum(1 for entry in scenario_entries if _benchmark_entry_has_evidence(entry))
    passed_count = sum(1 for entry in scenario_entries if entry.status == "passed")
    blocked_count = sum(1 for entry in scenario_entries if entry.status == "blocked")
    return WorkflowBenchmarkReportSchema.model_validate(
        {
            "report_id": row.report_id,
            "name": row.name,
            "owner": row.owner,
            "status": row.status,
            "product_name": worksheet.product_name,
            "evaluator": worksheet.evaluator,
            "environment": worksheet.environment,
            "summary": worksheet.summary,
            "scenario_count": len(worksheet.scenarios),
            "captured_count": captured_count,
            "passed_count": passed_count,
            "blocked_count": blocked_count,
            "worksheet": worksheet,
            "created_at": row.created_at,
            "updated_at": row.updated_at,
        }
    )


async def list_workflows(request: Request) -> JSONResponse:
    require_user(request)
    identity = resolve_identity(request)
    factory = get_session_factory(request)
    requested_status = request.query_params.get("status", "all")
    if requested_status not in _LIST_STATUS_VALUES:
        raise HTTPException(
            status_code=400,
            detail=(
                f"invalid value for 'status': {requested_status!r}; "
                f"expected one of {sorted(_LIST_STATUS_VALUES)}"
            ),
        )
    with factory() as session:
        stmt = select(CaliberWorkflow).order_by(CaliberWorkflow.created_at.desc())
        if requested_status != "all":
            stmt = stmt.where(CaliberWorkflow.status == requested_status)
        stmt = apply_visibility_filter(
            stmt,
            CaliberWorkflow,
            identity,
            identity.active_project_id,
            only=visibility_param(request),
        )
        rows = session.execute(stmt).scalars().all()
        items = [WorkflowSchema.model_validate(row) for row in rows]
    return envelope_response(items)


async def get_workflow(request: Request) -> JSONResponse:
    require_user(request)
    identity = resolve_identity(request)
    workflow_id = request.path_params["workflow_id"]
    factory = get_session_factory(request)
    with factory() as session:
        row = get_visible(
            session, CaliberWorkflow, CaliberWorkflow.workflow_id, workflow_id, identity
        )
        if row is None:
            raise HTTPException(status_code=404, detail=f"workflow {workflow_id!r} not found")
        data = WorkflowSchema.model_validate(row)
    return envelope_response(data)


async def list_workflow_components(request: Request) -> JSONResponse:
    require_user(request)
    catalog = WorkflowComponentCatalogSchema.model_validate(build_workflow_component_catalog())
    return envelope_response(catalog)


async def preview_workflow_cron(request: Request) -> JSONResponse:
    """Preview the next fire times of a Start-trigger cron expression (read-only).

    Powers the Studio Trigger panel's "next runs" preview. The walk is tz-naive
    (cron.py is dependency-free + tz-naive), so we localize ``now`` into the
    requested timezone before delegating to :func:`next_fires`.
    """
    require_user(request)
    expr = request.query_params.get("expr", "")
    tz = request.query_params.get("tz", "UTC")
    try:
        count = min(max(int(request.query_params.get("count", "5")), 1), 20)
    except ValueError:
        count = 5
    try:
        validate_cron(expr)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=f"invalid cron expression: {exc}") from exc
    try:
        zone = ZoneInfo(tz)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise HTTPException(status_code=400, detail=f"unknown timezone: {tz!r}") from exc
    now_local = datetime.now(timezone.utc).astimezone(zone).replace(tzinfo=None)
    fires = next_fires(expr, now_local, count=count)
    return envelope_response_dict(
        {
            "timezone": tz,
            "expression": expr,
            "fire_times": [dt.isoformat() for dt in fires],
        }
    )


async def list_workflow_templates(request: Request) -> JSONResponse:
    require_user(request)
    catalog = WorkflowTemplateCatalogSchema.model_validate(build_workflow_template_catalog())
    return envelope_response(catalog)


async def list_workflow_benchmark_reports(request: Request) -> JSONResponse:
    require_user(request)
    identity = resolve_identity(request)
    factory = get_session_factory(request)
    requested_status = request.query_params.get("status", "all")
    if requested_status not in _BENCHMARK_REPORT_STATUS_VALUES:
        raise HTTPException(
            status_code=400,
            detail=(
                f"invalid value for 'status': {requested_status!r}; "
                f"expected one of {sorted(_BENCHMARK_REPORT_STATUS_VALUES)}"
            ),
        )
    with factory() as session:
        stmt = select(CaliberWorkflowBenchmarkReport).order_by(
            CaliberWorkflowBenchmarkReport.updated_at.desc(),
            CaliberWorkflowBenchmarkReport.created_at.desc(),
        )
        if requested_status != "all":
            stmt = stmt.where(CaliberWorkflowBenchmarkReport.status == requested_status)
        stmt = apply_visibility_filter(
            stmt,
            CaliberWorkflowBenchmarkReport,
            identity,
            identity.active_project_id,
            only=visibility_param(request),
        )
        rows = session.execute(stmt).scalars().all()
        items = [_serialize_benchmark_report(row) for row in rows]
    return envelope_response(items)


async def create_workflow_benchmark_report(request: Request) -> JSONResponse:
    body = await parse_json_object(request)
    payload = WorkflowBenchmarkReportCreateRequest.model_validate(body)
    actor = require_scopes(request, [SCOPE_OPERATOR])
    identity = resolve_identity(request)
    factory = get_session_factory(request)
    with factory() as session:
        report_id = payload.report_id or new_workflow_benchmark_report_id()
        if session.get(CaliberWorkflowBenchmarkReport, report_id) is not None:
            raise HTTPException(
                status_code=409,
                detail=f"workflow benchmark report id {report_id!r} already exists",
            )
        report = CaliberWorkflowBenchmarkReport(
            report_id=report_id,
            name=payload.name,
            owner=actor,
            project_id=identity.active_project_id,
            visibility="project" if identity.active_project_id else "user",
            status=payload.status,
            worksheet=payload.worksheet.model_dump(mode="json"),
        )
        session.add(report)
        session.flush()
        audit_record(
            session,
            actor=actor,
            action="create_workflow_benchmark_report",
            entity_type="workflow_benchmark_report",
            entity_id=report.report_id,
            details={
                "name": report.name,
                "status": report.status,
                "product_name": payload.worksheet.product_name,
            },
        )
        session.commit()
        data = _serialize_benchmark_report(report)
    return envelope_response(data, status_code=201)


async def update_workflow_benchmark_report(request: Request) -> JSONResponse:
    report_id = request.path_params["report_id"]
    body = await parse_json_object(request)
    payload = WorkflowBenchmarkReportUpdateRequest.model_validate(body)
    changes = payload.model_dump(exclude_unset=True)
    if not changes:
        raise HTTPException(
            status_code=400,
            detail="request body must include at least one of: name, status, worksheet",
        )
    actor = require_scopes(request, [SCOPE_OPERATOR])
    identity = resolve_identity(request)
    factory = get_session_factory(request)
    with factory() as session:
        report = get_visible(
            session,
            CaliberWorkflowBenchmarkReport,
            CaliberWorkflowBenchmarkReport.report_id,
            report_id,
            identity,
        )
        if report is None:
            raise HTTPException(
                status_code=404,
                detail=f"workflow benchmark report {report_id!r} not found",
            )
        diff: dict[str, dict[str, Any]] = {}
        if "name" in changes:
            diff["name"] = {"from": report.name, "to": payload.name}
            report.name = payload.name or report.name
        if "status" in changes:
            diff["status"] = {"from": report.status, "to": payload.status}
            report.status = payload.status or report.status
        if "worksheet" in changes and payload.worksheet is not None:
            previous = _normalize_benchmark_worksheet(report.worksheet)
            diff["worksheet"] = {
                "from_product_name": previous.product_name,
                "to_product_name": payload.worksheet.product_name,
            }
            report.worksheet = payload.worksheet.model_dump(mode="json")
        audit_record(
            session,
            actor=actor,
            action="update_workflow_benchmark_report",
            entity_type="workflow_benchmark_report",
            entity_id=report.report_id,
            details=diff,
        )
        session.commit()
        session.refresh(report)
        data = _serialize_benchmark_report(report)
    return envelope_response(data)


async def delete_workflow_benchmark_report(request: Request) -> JSONResponse:
    report_id = request.path_params["report_id"]
    actor = require_scopes(request, [SCOPE_OPERATOR])
    identity = resolve_identity(request)
    factory = get_session_factory(request)
    with factory() as session:
        report = get_visible(
            session,
            CaliberWorkflowBenchmarkReport,
            CaliberWorkflowBenchmarkReport.report_id,
            report_id,
            identity,
        )
        if report is None:
            raise HTTPException(
                status_code=404,
                detail=f"workflow benchmark report {report_id!r} not found",
            )
        audit_record(
            session,
            actor=actor,
            action="delete_workflow_benchmark_report",
            entity_type="workflow_benchmark_report",
            entity_id=report.report_id,
            details={"name": report.name, "status": report.status},
        )
        session.delete(report)
        session.commit()
    return JSONResponse({"status": "deleted"}, status_code=200)


async def list_workflow_session_memory(request: Request) -> JSONResponse:
    require_user(request)
    workflow_id = request.path_params["workflow_id"]
    session_id = _require_session_id(request)
    node_id = _optional_node_id(request)
    factory = get_session_factory(request)
    with factory() as session:
        workflow = session.get(CaliberWorkflow, workflow_id)
        if workflow is None:
            raise HTTPException(status_code=404, detail=f"workflow {workflow_id!r} not found")
        stmt = select(CaliberWorkflowSessionMemory).where(
            CaliberWorkflowSessionMemory.workflow_id == workflow_id,
            CaliberWorkflowSessionMemory.session_id == session_id,
        )
        if node_id is not None:
            stmt = stmt.where(CaliberWorkflowSessionMemory.node_id == node_id)
        stmt = stmt.order_by(
            CaliberWorkflowSessionMemory.updated_at.desc(),
            CaliberWorkflowSessionMemory.node_id.asc(),
        )
        rows = session.execute(stmt).scalars().all()
        items = [_serialize_session_memory_entry(row) for row in rows]
    return envelope_response(items)


async def clear_workflow_session_memory(request: Request) -> JSONResponse:
    workflow_id = request.path_params["workflow_id"]
    session_id = _require_session_id(request)
    node_id = _optional_node_id(request)
    actor = require_scopes(request, [SCOPE_OPERATOR])

    factory = get_session_factory(request)
    with factory() as session:
        workflow = session.get(CaliberWorkflow, workflow_id)
        if workflow is None:
            raise HTTPException(status_code=404, detail=f"workflow {workflow_id!r} not found")
        stmt = select(CaliberWorkflowSessionMemory).where(
            CaliberWorkflowSessionMemory.workflow_id == workflow_id,
            CaliberWorkflowSessionMemory.session_id == session_id,
        )
        if node_id is not None:
            stmt = stmt.where(CaliberWorkflowSessionMemory.node_id == node_id)
        rows = session.execute(stmt).scalars().all()
        deleted_entries = len(rows)
        deleted_messages = sum(
            len(_normalize_session_memory_history(row.message_history)) for row in rows
        )
        for row in rows:
            session.delete(row)
        audit_record(
            session,
            actor=actor,
            action="clear_workflow_session_memory",
            entity_type="workflow",
            entity_id=workflow.workflow_id,
            details={
                "session_id": session_id,
                "node_id": node_id,
                "deleted_entries": deleted_entries,
                "deleted_messages": deleted_messages,
            },
        )
        session.commit()
    return envelope_response(
        WorkflowSessionMemoryClearResultSchema.model_validate(
            {
                "workflow_id": workflow_id,
                "session_id": session_id,
                "node_id": node_id,
                "deleted_entries": deleted_entries,
                "deleted_messages": deleted_messages,
            }
        )
    )


async def create_workflow(request: Request) -> JSONResponse:
    body = await parse_json_object(request)
    payload = WorkflowCreateRequest.model_validate(body)
    actor = require_scopes(request, [SCOPE_OPERATOR])
    identity = resolve_identity(request)
    factory = get_session_factory(request)
    with factory() as session:
        existing = (
            session.execute(select(CaliberWorkflow).where(CaliberWorkflow.name == payload.name))
            .scalars()
            .first()
        )
        if existing is not None:
            raise HTTPException(
                status_code=409,
                detail=f"workflow name {payload.name!r} is already in use by {existing.workflow_id!r}",
            )
        workflow_id = payload.workflow_id or new_workflow_id()
        if session.get(CaliberWorkflow, workflow_id) is not None:
            raise HTTPException(
                status_code=409, detail=f"workflow id {workflow_id!r} already exists"
            )
        workflow = CaliberWorkflow(
            workflow_id=workflow_id,
            name=payload.name,
            description=payload.description,
            # Owner is the authenticated actor (payload.owner is ignored).
            owner=actor,
            project_id=identity.active_project_id,
            visibility="project" if identity.active_project_id else "user",
            status="active",
            default_experiment_id=payload.default_experiment_id,
        )
        session.add(workflow)
        session.flush()
        audit_record(
            session,
            actor=actor,
            action="create_workflow",
            entity_type="workflow",
            entity_id=workflow.workflow_id,
            details={"name": workflow.name, "owner": workflow.owner},
        )
        session.commit()
        data = WorkflowSchema.model_validate(workflow)
    _publish_workflow_event(
        request,
        {
            "type": "workflow.created",
            "workflow_id": data.workflow_id,
            "status": data.status,
            "name": data.name,
            "owner": data.owner,
        },
    )
    return envelope_response(data, status_code=201)


async def update_workflow(request: Request) -> JSONResponse:
    workflow_id = request.path_params["workflow_id"]
    body = await parse_json_object(request)
    payload = WorkflowUpdateRequest.model_validate(body)
    actor = require_scopes(request, [SCOPE_OPERATOR])
    identity = resolve_identity(request)
    changes = payload.model_dump(exclude_unset=True)
    if not changes:
        raise HTTPException(status_code=400, detail="request body must include at least one field")

    factory = get_session_factory(request)
    with factory() as session:
        # Scope by visibility so an operator can't mutate another project's
        # workflow by guessing its id (require_scopes only checks the scope).
        workflow = get_visible(
            session, CaliberWorkflow, CaliberWorkflow.workflow_id, workflow_id, identity
        )
        if workflow is None:
            raise HTTPException(status_code=404, detail=f"workflow {workflow_id!r} not found")

        if changes.get("status") == "archived":
            prod = (
                session.execute(
                    select(CaliberWorkflowDeployment).where(
                        CaliberWorkflowDeployment.workflow_id == workflow_id,
                        CaliberWorkflowDeployment.alias.in_(tuple(LIVE_ALIASES)),
                    )
                )
                .scalars()
                .first()
            )
            if prod is not None:
                raise HTTPException(
                    status_code=409,
                    detail="cannot archive workflow with active production deployments",
                )

        diff: dict[str, dict[str, object]] = {}
        for field in _UPDATABLE_FIELDS:
            if field not in changes:
                continue
            new_value = changes[field]
            old_value = getattr(workflow, field)
            if new_value != old_value:
                diff[field] = {"from": old_value, "to": new_value}
                setattr(workflow, field, new_value)
        if not diff:
            return envelope_response(WorkflowSchema.model_validate(workflow))

        audit_record(
            session,
            actor=actor,
            action="update_workflow",
            entity_type="workflow",
            entity_id=workflow.workflow_id,
            details={"changes": diff},
        )
        session.commit()
        data = WorkflowSchema.model_validate(workflow)
    if "status" in diff:
        _publish_workflow_status_event(request, workflow_id, str(diff["status"]["to"]))
    else:
        _publish_workflow_event(
            request,
            {
                "type": "workflow.updated",
                "workflow_id": data.workflow_id,
                "status": data.status,
                "changed_fields": sorted(diff),
            },
        )
    return envelope_response(data)


async def import_workflow(request: Request) -> JSONResponse:
    """Import a workflow from a manifest (plan §15.5).

    Accepts a parsed ``manifest`` object or a raw ``manifest_yaml`` string.
    Creates the workflow (when its ``workflow_id`` is new) and a fresh draft
    version holding the imported manifest. Importing a manifest whose
    ``workflow_id`` already exists appends a new draft version to it.
    """
    body = await parse_json_object(request)
    payload = WorkflowImportRequest.model_validate(body)
    actor = require_scopes(request, [SCOPE_OPERATOR])

    raw = _resolve_import_manifest(payload)
    try:
        manifest = parse_manifest(raw)
    except (WorkflowManifestError, ValueError) as exc:
        raise HTTPException(status_code=400, detail=f"invalid manifest: {exc}") from exc
    secrets = find_inline_secrets(raw)
    if secrets:
        raise HTTPException(
            status_code=400,
            detail=f"manifest contains inline secret value(s) at {secrets}; "
            "reference secrets by name via tool secret_refs (plan §18.2)",
        )

    normalized = manifest.to_dict()
    name = payload.name or manifest.name
    owner = payload.owner or manifest.owner or ""

    factory = get_session_factory(request)
    with factory() as session:
        workflow = session.get(CaliberWorkflow, manifest.workflow_id)
        if workflow is None:
            name_clash = (
                session.execute(select(CaliberWorkflow).where(CaliberWorkflow.name == name))
                .scalars()
                .first()
            )
            if name_clash is not None:
                raise HTTPException(
                    status_code=409,
                    detail=f"workflow name {name!r} is already in use by {name_clash.workflow_id!r}",
                )
            workflow = CaliberWorkflow(
                workflow_id=manifest.workflow_id,
                name=name,
                description=manifest.description,
                owner=owner,
                status="active",
            )
            session.add(workflow)
            session.flush()

        max_number = (
            session.execute(
                select(CaliberWorkflowVersion.version_number)
                .where(CaliberWorkflowVersion.workflow_id == workflow.workflow_id)
                .order_by(CaliberWorkflowVersion.version_number.desc())
            )
            .scalars()
            .first()
        )
        version = CaliberWorkflowVersion(
            version_id=new_workflow_version_id(),
            workflow_id=workflow.workflow_id,
            version_number=(max_number or 0) + 1,
            status="draft",
            manifest=normalized,
            manifest_hash=compute_manifest_hash(normalized),
            created_by=actor,
        )
        session.add(version)
        session.flush()
        audit_record(
            session,
            actor=actor,
            action="import_workflow",
            entity_type="workflow",
            entity_id=workflow.workflow_id,
            details={"version_id": version.version_id, "version_number": version.version_number},
        )
        session.commit()
        result = {
            "workflow": WorkflowSchema.model_validate(workflow).model_dump(mode="json"),
            "version": WorkflowVersionSchema.model_validate(version).model_dump(mode="json"),
        }
    return JSONResponse({"data": result}, status_code=201)


def _resolve_import_manifest(payload: WorkflowImportRequest) -> dict[str, Any]:
    if payload.manifest is not None and payload.manifest_yaml is not None:
        raise HTTPException(
            status_code=400, detail="provide exactly one of 'manifest' or 'manifest_yaml'"
        )
    if payload.manifest is not None:
        return dict(payload.manifest)
    if payload.manifest_yaml is not None:
        try:
            parsed = yaml.safe_load(payload.manifest_yaml)
        except yaml.YAMLError as exc:
            raise HTTPException(status_code=400, detail=f"invalid manifest_yaml: {exc}") from exc
        if not isinstance(parsed, dict):
            raise HTTPException(status_code=400, detail="manifest_yaml must decode to a mapping")
        return parsed
    raise HTTPException(status_code=400, detail="provide one of 'manifest' or 'manifest_yaml'")


def _publish_workflow_status_event(request: Request, workflow_id: str, status: str) -> None:
    if status == "paused":
        event_type = "workflow.paused"
    elif status == "active":
        event_type = "workflow.resumed"
    else:
        event_type = "workflow.updated"
    _publish_workflow_event(
        request,
        {"type": event_type, "workflow_id": workflow_id, "status": status},
    )


async def delete_workflow(request: Request) -> JSONResponse:
    """Delete a workflow and all related versions, deployments, promotions, and runs.

    Blocked when a production deployment exists.
    """
    workflow_id = request.path_params["workflow_id"]
    actor = require_scopes(request, [SCOPE_OPERATOR])
    identity = resolve_identity(request)

    factory = get_session_factory(request)
    with factory() as session:
        # Scope by visibility so an operator can't delete another project's
        # workflow (and cascade its versions/runs) by guessing its id.
        workflow = get_visible(
            session, CaliberWorkflow, CaliberWorkflow.workflow_id, workflow_id, identity
        )
        if workflow is None:
            raise HTTPException(status_code=404, detail=f"workflow {workflow_id!r} not found")

        prod = (
            session.execute(
                select(CaliberWorkflowDeployment).where(
                    CaliberWorkflowDeployment.workflow_id == workflow_id,
                    CaliberWorkflowDeployment.alias.in_(tuple(LIVE_ALIASES)),
                )
            )
            .scalars()
            .first()
        )
        if prod is not None:
            raise HTTPException(
                status_code=409,
                detail="cannot delete workflow with active production deployments",
            )

        run_ids_stmt = select(CaliberWorkflowRun.workflow_run_id).where(
            CaliberWorkflowRun.workflow_id == workflow_id
        )
        # Run-child rows reference run ids, not workflow ids, so delete them first.
        session.execute(
            delete(CaliberWorkflowRunEvent).where(
                CaliberWorkflowRunEvent.workflow_run_id.in_(run_ids_stmt)
            )
        )
        session.execute(
            delete(CaliberWorkflowRunCheckpoint).where(
                CaliberWorkflowRunCheckpoint.workflow_run_id.in_(run_ids_stmt)
            )
        )
        session.execute(
            delete(CaliberRuntimeApprovalRequest).where(
                CaliberRuntimeApprovalRequest.workflow_run_id.in_(run_ids_stmt)
            )
        )

        # Cascade-delete related workflow-scoped rows.
        for model in (
            CaliberWorkflowPatch,
            CaliberWorkflowPromotion,
            CaliberWorkflowRun,
            CaliberWorkflowDeployment,
            CaliberWorkflowSessionMemory,
            CaliberWorkflowVersion,
        ):
            session.execute(delete(model).where(model.workflow_id == workflow_id))

        # Remove Agent Fleet entries that ``sync_fleet_from_version`` auto-created
        # from this workflow's agent nodes at deploy time. These are separate
        # CaliberAgentConfig rows (stamped with ``source_workflow_id``), so the
        # workflow-scoped cascade above would otherwise leave them orphaned in the
        # fleet. Manually-registered agents carry no ``source_workflow_id`` and are
        # left untouched. (optimizer_config is JSON; filter in Python to stay
        # dialect-agnostic — the fleet table is small.)
        fleet_agent_ids = [
            agent.agent_id
            for agent in session.execute(select(CaliberAgentConfig)).scalars()
            if isinstance(agent.optimizer_config, dict)
            and agent.optimizer_config.get("source_workflow_id") == workflow_id
        ]
        if fleet_agent_ids:
            # Rollback checkpoints and regression runs reference an approval
            # row; clear them before the approval requests they hang off of.
            approval_ids_stmt = select(CaliberApprovalRequest.approval_id).where(
                CaliberApprovalRequest.agent_id.in_(fleet_agent_ids)
            )
            session.execute(
                delete(CaliberRollbackCheckpoint).where(
                    CaliberRollbackCheckpoint.approval_id.in_(approval_ids_stmt)
                )
            )
            session.execute(
                delete(CaliberRegressionRun).where(
                    CaliberRegressionRun.approval_id.in_(approval_ids_stmt)
                )
            )
            for agent_model in (
                CaliberApprovalRequest,
                CaliberVerificationItem,
                CaliberRefinementJob,
                CaliberRollbackCheckpoint,
                CaliberRegressionRun,
            ):
                session.execute(
                    delete(agent_model).where(agent_model.agent_id.in_(fleet_agent_ids))
                )
            session.execute(
                delete(CaliberAgentConfig).where(CaliberAgentConfig.agent_id.in_(fleet_agent_ids))
            )

        audit_record(
            session,
            actor=actor,
            action="delete_workflow",
            entity_type="workflow",
            entity_id=workflow.workflow_id,
            details={"name": workflow.name},
        )
        deleted_event = {
            "type": "workflow.deleted",
            "workflow_id": workflow.workflow_id,
            "status": workflow.status,
            "name": workflow.name,
        }
        session.delete(workflow)
        session.commit()

    _publish_workflow_event(request, deleted_event)
    return JSONResponse({"status": "deleted"}, status_code=200)


def register(app: Starlette) -> None:
    app.routes.append(Route(COMPONENTS_PATH, list_workflow_components, methods=["GET"]))
    app.routes.append(Route(CRON_PREVIEW_PATH, preview_workflow_cron, methods=["GET"]))
    app.routes.append(Route(TEMPLATES_PATH, list_workflow_templates, methods=["GET"]))
    app.routes.append(
        Route(BENCHMARK_REPORTS_PATH, list_workflow_benchmark_reports, methods=["GET"])
    )
    app.routes.append(
        Route(BENCHMARK_REPORTS_PATH, create_workflow_benchmark_report, methods=["POST"])
    )
    app.routes.append(
        Route(BENCHMARK_REPORT_DETAIL_PATH, update_workflow_benchmark_report, methods=["PATCH"])
    )
    app.routes.append(
        Route(BENCHMARK_REPORT_DETAIL_PATH, delete_workflow_benchmark_report, methods=["DELETE"])
    )
    app.routes.append(Route(LIST_PATH, list_workflows, methods=["GET"]))
    # Import must be registered before the {workflow_id} detail GET so the
    # literal ``/workflows/import`` path isn't captured as a workflow id.
    app.routes.append(Route(IMPORT_PATH, import_workflow, methods=["POST"]))
    app.routes.append(Route(LIST_PATH, create_workflow, methods=["POST"]))
    app.routes.append(Route(DETAIL_PATH, get_workflow, methods=["GET"]))
    app.routes.append(Route(SESSION_MEMORY_PATH, list_workflow_session_memory, methods=["GET"]))
    app.routes.append(Route(SESSION_MEMORY_PATH, clear_workflow_session_memory, methods=["DELETE"]))
    app.routes.append(Route(DETAIL_PATH, update_workflow, methods=["PATCH"]))
    app.routes.append(Route(DETAIL_PATH, delete_workflow, methods=["DELETE"]))
