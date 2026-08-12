"""Governed OpenAPI integration drafts.

This is the control-plane import surface for external OpenAPI contracts. It
creates durable integration shells, pins imported contract snapshots, detects
operation dependencies deterministically, generates curated tool drafts, and
can publish approved drafts into CALIBER's governed tool registry via a
declarative HTTP execution backend.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session
from starlette.applications import Starlette
from starlette.concurrency import run_in_threadpool
from starlette.exceptions import HTTPException
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from caliber.audit import record as audit_record
from caliber.auth import SCOPE_ADMIN, SCOPE_OPERATOR, require_scopes, require_user, resolve_identity
from caliber.db.models import (
    CaliberOpenApiIntegration,
    CaliberOpenApiIntegrationVersion,
    CaliberOpenApiOperation,
    CaliberOpenApiOperationDependency,
    CaliberOpenApiToolDraft,
    CaliberToolRegistry,
)
from caliber.db.scoping import apply_visibility_filter, get_visible
from caliber.egress import EgressPolicy
from caliber.ids import (
    new_openapi_dependency_id,
    new_openapi_integration_id,
    new_openapi_integration_version_id,
    new_openapi_operation_id,
    new_openapi_tool_draft_id,
    new_tool_id,
)
from caliber.integrations.openapi.dependencies import CONFIDENCE_HIGH, CONFIDENCE_MEDIUM, detect_dependencies
from caliber.integrations.openapi.diff import diff_operations
from caliber.integrations.openapi.executor import execute_openapi_http_tool
from caliber.integrations.openapi.graph import build_graph_snapshot
from caliber.integrations.openapi.loader import OpenApiLoadError, load_spec, probe_spec_source
from caliber.integrations.openapi.normalize import (
    OpenApiImportError,
    normalize_openapi_document,
    parse_openapi_text,
    spec_sha256,
)
from caliber.integrations.openapi.tool_drafts import (
    auth_binding_secret_refs,
    build_execution_config,
    build_pack_execution_config,
    build_tool_draft_description,
    build_tool_draft_name,
    build_tool_input_schema,
    build_tool_output_schema,
    build_tool_pack_description,
    build_tool_pack_input_schema,
    build_tool_pack_name,
    pack_side_effect_level,
)
from caliber.routes._deps import (
    envelope_response,
    envelope_response_dict,
    get_session_factory,
    parse_json_object,
    visibility_param,
)
from caliber.schemas import (
    OpenApiDependencyReviewRequest,
    OpenApiGenerateToolDraftsRequest,
    OpenApiImportRequest,
    OpenApiIntegrationCreateRequest,
    OpenApiIntegrationSchema,
    OpenApiIntegrationUpdateRequest,
    OpenApiIntegrationVersionSchema,
    OpenApiOperationDependencySchema,
    OpenApiOperationSchema,
    OpenApiPublishToolDraftRequest,
    OpenApiToolDraftPreviewRequest,
    OpenApiToolDraftSchema,
    OpenApiToolDraftUpdateRequest,
    OpenApiValidateCredentialBindingRequest,
    OpenApiValidateSpecSourceRequest,
    ToolSchema,
)
from caliber.secrets import resolve_secret

PREFIX = "/ajax-api/2.0/mlflow/caliber"
LIST_PATH = PREFIX + "/openapi-integrations"
DETAIL_PATH = LIST_PATH + "/{integration_id}"
ARCHIVE_PATH = DETAIL_PATH + "/archive"
IMPORT_PATH = DETAIL_PATH + "/import"
REIMPORT_PATH = DETAIL_PATH + "/reimport"
VALIDATE_SPEC_SOURCE_PATH = DETAIL_PATH + "/validate-spec-source"
VERSIONS_PATH = DETAIL_PATH + "/versions"
VERSION_DETAIL_PATH = VERSIONS_PATH + "/{version_id}"
VERSION_DIFF_PATH = VERSION_DETAIL_PATH + "/diff"
OPERATIONS_PATH = DETAIL_PATH + "/operations"
OPERATION_DETAIL_PATH = OPERATIONS_PATH + "/{operation_id}"
DEPENDENCIES_PATH = DETAIL_PATH + "/dependencies"
DEPENDENCY_DETAIL_PATH = DEPENDENCIES_PATH + "/{dependency_id}"
GRAPH_PATH = DETAIL_PATH + "/graph"
TOOL_DRAFTS_PATH = DETAIL_PATH + "/tool-drafts"
TOOL_DRAFT_GENERATE_PATH = TOOL_DRAFTS_PATH + "/generate"
TOOL_DRAFT_DETAIL_PATH = TOOL_DRAFTS_PATH + "/{draft_id}"
TOOL_DRAFT_PREVIEW_PATH = TOOL_DRAFT_DETAIL_PATH + "/preview"
TOOL_DRAFT_PUBLISH_PATH = TOOL_DRAFT_DETAIL_PATH + "/publish"
VALIDATE_CREDENTIAL_BINDING_PATH = DETAIL_PATH + "/validate-credential-binding"

_LIST_STATUS_VALUES = frozenset({"draft", "review", "ready", "published", "archived", "all"})

#: §5.4's deterministic-vs-agent-assisted policy, expressed as the initial row
#: status a freshly detected dependency gets. ``low`` confidence rows land
#: ``advisory`` and are never auto-wired.
_INITIAL_DEPENDENCY_STATUS = {
    CONFIDENCE_HIGH: "auto_wired",
    CONFIDENCE_MEDIUM: "suggested",
}


def _visible_integration_or_404(
    session: Session,
    request: Request,
    integration_id: str,
) -> CaliberOpenApiIntegration:
    row: CaliberOpenApiIntegration | None = get_visible(
        session,
        CaliberOpenApiIntegration,
        CaliberOpenApiIntegration.integration_id,
        integration_id,
        resolve_identity(request),
    )
    if row is None:
        raise HTTPException(
            status_code=404,
            detail=f"OpenAPI integration {integration_id!r} not found",
        )
    return row


def _version_for_integration_or_404(
    session: Session,
    integration_id: str,
    version_id: str,
) -> CaliberOpenApiIntegrationVersion:
    row = session.get(CaliberOpenApiIntegrationVersion, version_id)
    if row is None or row.integration_id != integration_id:
        raise HTTPException(
            status_code=404,
            detail=f"OpenAPI integration version {version_id!r} not found",
        )
    return row


def _draft_for_integration_or_404(
    session: Session,
    integration_id: str,
    draft_id: str,
) -> CaliberOpenApiToolDraft:
    row = session.get(CaliberOpenApiToolDraft, draft_id)
    if row is None or row.integration_id != integration_id:
        raise HTTPException(
            status_code=404,
            detail=f"OpenAPI tool draft {draft_id!r} not found",
        )
    return row


def _operation_for_integration_or_404(
    session: Session,
    integration_id: str,
    operation_id: str,
) -> tuple[CaliberOpenApiOperation, CaliberOpenApiIntegrationVersion]:
    operation = session.get(CaliberOpenApiOperation, operation_id)
    if operation is None:
        raise HTTPException(
            status_code=404,
            detail=f"OpenAPI operation {operation_id!r} not found",
        )
    version = session.get(CaliberOpenApiIntegrationVersion, operation.integration_version_id)
    if version is None or version.integration_id != integration_id:
        raise HTTPException(
            status_code=404,
            detail=f"OpenAPI operation {operation_id!r} not found",
        )
    return operation, version


def _dependency_for_integration_or_404(
    session: Session,
    integration_id: str,
    dependency_id: str,
) -> tuple[CaliberOpenApiOperationDependency, CaliberOpenApiIntegrationVersion]:
    """Look up a dependency row by id, then confirm it belongs to this integration.

    Deliberately does not assume "the latest imported version" — a dependency
    from an older, superseded version is still a valid target for review, and
    conflating the two would 404 on exactly the rows an auditor is most likely
    to be looking at.
    """

    row = session.get(CaliberOpenApiOperationDependency, dependency_id)
    version = (
        session.get(CaliberOpenApiIntegrationVersion, row.integration_version_id)
        if row is not None
        else None
    )
    if row is None or version is None or version.integration_id != integration_id:
        raise HTTPException(
            status_code=404,
            detail=f"OpenAPI dependency {dependency_id!r} not found",
        )
    return row, version


def _draft_ready_for_publication(draft: CaliberOpenApiToolDraft, operations: list[CaliberOpenApiOperation]) -> bool:
    if not draft.server_url.strip():
        return False
    requires_auth = any(operation.auth_schemes for operation in operations)
    if requires_auth and not isinstance(draft.auth_binding, dict):
        return False
    return True


def _draft_operations(
    session: Session, integration_id: str, draft: CaliberOpenApiToolDraft
) -> list[CaliberOpenApiOperation]:
    ids = [draft.operation_id, *(draft.additional_operation_ids or [])]
    return [_operation_for_integration_or_404(session, integration_id, op_id)[0] for op_id in ids]


def _persist_dependencies(
    session: Session,
    version: CaliberOpenApiIntegrationVersion,
    operations: list[CaliberOpenApiOperation],
) -> list[CaliberOpenApiOperationDependency]:
    """Run deterministic detection and persist the canonical dependency rows.

    Operates on already-persisted operations (so real ``operation_id`` values
    exist to reference), which is why this runs after the operations insert and
    a ``session.flush()`` rather than alongside :func:`normalize_openapi_document`.
    """

    detector_input = [
        {
            "operation_id": operation.operation_id,
            "operation_key": operation.operation_key,
            "spec_operation_id": operation.spec_operation_id,
            "path": operation.path,
            "method": operation.method,
            "tags": list(operation.tags or []),
            "normalized_operation": dict(operation.normalized_operation or {}),
        }
        for operation in operations
    ]
    detected = detect_dependencies(detector_input)
    rows: list[CaliberOpenApiOperationDependency] = []
    for item in detected:
        row = CaliberOpenApiOperationDependency(
            dependency_id=new_openapi_dependency_id(),
            integration_version_id=version.version_id,
            from_operation_id=item["from_operation_id"],
            to_operation_id=item["to_operation_id"],
            dependency_type=item["dependency_type"],
            confidence=item["confidence"],
            source=item["source"],
            required=bool(item["required"]),
            binding_field_map=dict(item["binding_field_map"]),
            notes=str(item["notes"]),
            status=_INITIAL_DEPENDENCY_STATUS.get(item["confidence"], "advisory"),
        )
        session.add(row)
        rows.append(row)
    version.dependency_detected_at = datetime.now(timezone.utc)
    return rows


def _rebuild_graph_snapshot(
    session: Session,
    integration: CaliberOpenApiIntegration,
    version: CaliberOpenApiIntegrationVersion,
) -> dict[str, Any]:
    operations = (
        session.execute(
            select(CaliberOpenApiOperation).where(
                CaliberOpenApiOperation.integration_version_id == version.version_id
            )
        )
        .scalars()
        .all()
    )
    dependencies = (
        session.execute(
            select(CaliberOpenApiOperationDependency).where(
                CaliberOpenApiOperationDependency.integration_version_id == version.version_id
            )
        )
        .scalars()
        .all()
    )
    tool_drafts = (
        session.execute(
            select(CaliberOpenApiToolDraft).where(
                CaliberOpenApiToolDraft.integration_version_id == version.version_id
            )
        )
        .scalars()
        .all()
    )
    snapshot = build_graph_snapshot(
        integration=integration,
        version=version,
        operations=operations,
        dependencies=dependencies,
        tool_drafts=tool_drafts,
    )
    version.graph_snapshot = snapshot
    return snapshot


async def list_openapi_integrations(request: Request) -> JSONResponse:
    require_user(request)
    identity = resolve_identity(request)
    requested_status = request.query_params.get("status", "all")
    if requested_status not in _LIST_STATUS_VALUES:
        raise HTTPException(
            status_code=400,
            detail=(
                f"invalid value for 'status': {requested_status!r}; "
                f"expected one of {sorted(_LIST_STATUS_VALUES)}"
            ),
        )
    factory = get_session_factory(request)
    with factory() as session:
        stmt = select(CaliberOpenApiIntegration).order_by(CaliberOpenApiIntegration.name)
        if requested_status != "all":
            stmt = stmt.where(CaliberOpenApiIntegration.status == requested_status)
        stmt = apply_visibility_filter(
            stmt,
            CaliberOpenApiIntegration,
            identity,
            identity.active_project_id,
            only=visibility_param(request),
        )
        rows = session.execute(stmt).scalars().all()
        data = [OpenApiIntegrationSchema.model_validate(row) for row in rows]
    return envelope_response(data)


async def create_openapi_integration(request: Request) -> JSONResponse:
    body = await parse_json_object(request)
    payload = OpenApiIntegrationCreateRequest.model_validate(body)
    actor = require_scopes(request, [SCOPE_ADMIN, SCOPE_OPERATOR])
    identity = resolve_identity(request)
    factory = get_session_factory(request)
    with factory() as session:
        row = CaliberOpenApiIntegration(
            integration_id=new_openapi_integration_id(),
            name=payload.name,
            description=payload.description,
            owner=actor,
            status="draft",
            project_id=identity.active_project_id,
            visibility="project" if identity.active_project_id else "user",
        )
        session.add(row)
        session.flush()
        audit_record(
            session,
            actor=actor,
            action="create_openapi_integration",
            entity_type="openapi_integration",
            entity_id=row.integration_id,
            details={"name": row.name, "status": row.status},
        )
        session.commit()
        data = OpenApiIntegrationSchema.model_validate(row)
    return envelope_response(data, status_code=201)


async def get_openapi_integration(request: Request) -> JSONResponse:
    require_user(request)
    integration_id = request.path_params["integration_id"]
    factory = get_session_factory(request)
    with factory() as session:
        row = _visible_integration_or_404(session, request, integration_id)
        data = OpenApiIntegrationSchema.model_validate(row)
    return envelope_response(data)


async def update_openapi_integration(request: Request) -> JSONResponse:
    body = await parse_json_object(request)
    payload = OpenApiIntegrationUpdateRequest.model_validate(body)
    actor = require_scopes(request, [SCOPE_ADMIN, SCOPE_OPERATOR])
    integration_id = request.path_params["integration_id"]
    factory = get_session_factory(request)
    changes: dict[str, Any] = {}
    with factory() as session:
        row = _visible_integration_or_404(session, request, integration_id)
        if payload.name is not None and payload.name != row.name:
            changes["name"] = {"from": row.name, "to": payload.name}
            row.name = payload.name
        if payload.description is not None and payload.description != row.description:
            changes["description"] = {"from": row.description, "to": payload.description}
            row.description = payload.description
        if payload.status is not None and payload.status != row.status:
            if row.status == "archived":
                raise HTTPException(
                    status_code=409,
                    detail="archived OpenAPI integrations cannot change status",
                )
            changes["status"] = {"from": row.status, "to": payload.status}
            row.status = payload.status
        if changes:
            audit_record(
                session,
                actor=actor,
                action="update_openapi_integration",
                entity_type="openapi_integration",
                entity_id=row.integration_id,
                details={"changes": changes},
            )
            session.commit()
        data = OpenApiIntegrationSchema.model_validate(row)
    return envelope_response(data)


async def archive_openapi_integration(request: Request) -> JSONResponse:
    actor = require_scopes(request, [SCOPE_ADMIN, SCOPE_OPERATOR])
    integration_id = request.path_params["integration_id"]
    factory = get_session_factory(request)
    with factory() as session:
        row = _visible_integration_or_404(session, request, integration_id)
        if row.status != "archived":
            row.status = "archived"
            audit_record(
                session,
                actor=actor,
                action="archive_openapi_integration",
                entity_type="openapi_integration",
                entity_id=row.integration_id,
                details={"name": row.name},
            )
            session.commit()
        data = OpenApiIntegrationSchema.model_validate(row)
    return envelope_response(data)


def _do_import(
    session: Session,
    *,
    integration: CaliberOpenApiIntegration,
    actor: str,
    source_kind: str,
    source_ref: str,
    document: dict[str, Any],
) -> CaliberOpenApiIntegrationVersion:
    """Normalize, persist, and detect dependencies for one parsed spec document.

    Shared by ``import`` and ``reimport`` so the two routes cannot drift.
    """

    try:
        summary, operations, warnings = normalize_openapi_document(document)
    except OpenApiImportError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    digest = spec_sha256(document)
    existing = (
        session.execute(
            select(CaliberOpenApiIntegrationVersion).where(
                CaliberOpenApiIntegrationVersion.integration_id == integration.integration_id,
                CaliberOpenApiIntegrationVersion.spec_sha256 == digest,
            )
        )
        .scalars()
        .first()
    )
    if existing is not None:
        raise HTTPException(
            status_code=409,
            detail=(
                "this OpenAPI document is already imported as "
                f"{existing.version_id!r} for integration {integration.integration_id!r}"
            ),
        )

    version = CaliberOpenApiIntegrationVersion(
        version_id=new_openapi_integration_version_id(),
        integration_id=integration.integration_id,
        source_kind=source_kind,
        source_ref=source_ref,
        spec_sha256=digest,
        openapi_version=str(summary["openapi"]),
        title=str(summary["title"]),
        spec_version=str(summary["version"]),
        spec_description=str(summary["description"]),
        server_urls=list(summary["server_urls"]),
        auth_schemes=list(summary["auth_schemes"]),
        import_warnings=list(warnings),
        operation_count=int(summary["operation_count"]),
        raw_document=document,
        normalized_summary=summary,
        created_by=actor,
    )
    session.add(version)
    session.flush()
    persisted_operations: list[CaliberOpenApiOperation] = []
    for operation in operations:
        row = CaliberOpenApiOperation(
            operation_id=new_openapi_operation_id(),
            integration_version_id=version.version_id,
            operation_key=str(operation["operation_key"]),
            method=str(operation["method"]),
            path=str(operation["path"]),
            spec_operation_id=operation.get("spec_operation_id"),
            summary=str(operation["summary"]),
            description=str(operation["description"]),
            tags=list(operation["tags"]),
            deprecated=bool(operation["deprecated"]),
            side_effect_level=str(operation["side_effect_level"]),
            auth_schemes=list(operation["auth_schemes"]),
            request_body_required=bool(operation["request_body_required"]),
            request_content_types=list(operation["request_content_types"]),
            response_statuses=list(operation["response_statuses"]),
            normalized_operation=dict(operation["normalized_operation"]),
        )
        session.add(row)
        persisted_operations.append(row)
    session.flush()

    _persist_dependencies(session, version, persisted_operations)
    session.flush()
    _rebuild_graph_snapshot(session, integration, version)

    integration.last_imported_version_id = version.version_id
    integration.status = "review"
    return version


async def import_openapi_version(request: Request) -> JSONResponse:
    body = await parse_json_object(request)
    payload = OpenApiImportRequest.model_validate(body)
    actor = require_scopes(request, [SCOPE_ADMIN, SCOPE_OPERATOR])
    integration_id = request.path_params["integration_id"]
    policy = EgressPolicy.from_config(getattr(request.app.state, "config", None))
    factory = get_session_factory(request)
    with factory() as session:
        integration = _visible_integration_or_404(session, request, integration_id)
        if integration.status == "archived":
            raise HTTPException(
                status_code=409,
                detail="archived OpenAPI integrations cannot import new versions",
            )
        try:
            loaded = load_spec(
                source_kind=payload.source_kind,
                spec_text=payload.spec_text,
                spec_base64=payload.spec_base64,
                spec_url=payload.spec_url,
                source_ref=payload.source_ref,
                egress_policy=policy,
            )
            document = parse_openapi_text(loaded.spec_text)
        except (OpenApiLoadError, OpenApiImportError) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

        version = _do_import(
            session,
            integration=integration,
            actor=actor,
            source_kind=loaded.source_kind,
            source_ref=loaded.source_ref,
            document=document,
        )
        audit_record(
            session,
            actor=actor,
            action="import_openapi_integration_version",
            entity_type="openapi_integration",
            entity_id=integration.integration_id,
            details={
                "version_id": version.version_id,
                "operation_count": version.operation_count,
                "warning_count": len(version.import_warnings),
                "openapi_version": version.openapi_version,
                "source_kind": version.source_kind,
            },
        )
        session.commit()
        data = OpenApiIntegrationVersionSchema.model_validate(version)
    return envelope_response(data, status_code=201)


def _sync_reimport_openapi_version(
    factory: Any,
    request: Request,
    integration_id: str,
    actor: str,
    policy: EgressPolicy,
) -> JSONResponse:
    """The synchronous body of :func:`reimport_openapi_version`.

    Deliberately a module-level function, not a closure nested inside its async
    caller: this does a real outbound fetch (``load_spec``) plus a synchronous DB
    session, and either alone would stall every other in-flight request for its
    duration if left on the event loop — see ``routes/audit.py::export_audit_log``
    for the convention this follows.
    """

    with factory() as session:
        integration = _visible_integration_or_404(session, request, integration_id)
        if integration.status == "archived":
            raise HTTPException(
                status_code=409,
                detail="archived OpenAPI integrations cannot reimport",
            )
        if not integration.last_imported_version_id:
            raise HTTPException(
                status_code=404,
                detail=f"OpenAPI integration {integration.integration_id!r} has no imported version to refresh",
            )
        previous = _version_for_integration_or_404(
            session, integration.integration_id, integration.last_imported_version_id
        )
        if previous.source_kind != "url" or not previous.source_ref:
            raise HTTPException(
                status_code=409,
                detail=(
                    f"the last imported version used source_kind={previous.source_kind!r}; "
                    "reimport only re-fetches a 'url' source"
                ),
            )
        try:
            loaded = load_spec(
                source_kind="url",
                spec_url=previous.source_ref,
                egress_policy=policy,
            )
            document = parse_openapi_text(loaded.spec_text)
        except (OpenApiLoadError, OpenApiImportError) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

        version = _do_import(
            session,
            integration=integration,
            actor=actor,
            source_kind=loaded.source_kind,
            source_ref=loaded.source_ref,
            document=document,
        )
        session.flush()
        previous_operations = (
            session.execute(
                select(CaliberOpenApiOperation).where(
                    CaliberOpenApiOperation.integration_version_id == previous.version_id
                )
            )
            .scalars()
            .all()
        )
        new_operations = (
            session.execute(
                select(CaliberOpenApiOperation).where(
                    CaliberOpenApiOperation.integration_version_id == version.version_id
                )
            )
            .scalars()
            .all()
        )
        version_diff = diff_operations(previous_operations, new_operations)
        audit_record(
            session,
            actor=actor,
            action="reimport_openapi_integration_version",
            entity_type="openapi_integration",
            entity_id=integration.integration_id,
            details={
                "previous_version_id": previous.version_id,
                "version_id": version.version_id,
                "diff_summary": version_diff["summary"],
            },
        )
        session.commit()
        return envelope_response_dict(
            {
                "version": OpenApiIntegrationVersionSchema.model_validate(version).model_dump(
                    mode="json"
                ),
                "previous_version_id": previous.version_id,
                "diff": version_diff,
            },
            status_code=201,
        )


async def reimport_openapi_version(request: Request) -> JSONResponse:
    """Re-fetch and re-import from the most recent version's recorded source.

    Only meaningful for a ``url`` source — an inline or uploaded spec has no
    live location to re-fetch, so those report 409 rather than silently no-op.
    Always produces a *new* pinned version; it never mutates the one it was
    triggered from, keeping every past version's audit trail intact.
    """

    actor = require_scopes(request, [SCOPE_ADMIN, SCOPE_OPERATOR])
    integration_id = request.path_params["integration_id"]
    policy = EgressPolicy.from_config(getattr(request.app.state, "config", None))
    factory = get_session_factory(request)
    return await run_in_threadpool(
        _sync_reimport_openapi_version, factory, request, integration_id, actor, policy
    )


def _sync_validate_openapi_spec_source(
    factory: Any,
    request: Request,
    integration_id: str,
    payload: OpenApiValidateSpecSourceRequest,
    policy: EgressPolicy,
) -> JSONResponse:
    """The synchronous body of :func:`validate_openapi_spec_source`.

    Module-level for the same reason as ``_sync_reimport_openapi_version``:
    ``probe_spec_source`` performs a real HEAD/GET request.
    """

    with factory() as session:
        _visible_integration_or_404(session, request, integration_id)
    result = probe_spec_source(
        source_kind=payload.source_kind,
        spec_url=payload.spec_url,
        egress_policy=policy,
    )
    return envelope_response_dict(result)


async def validate_openapi_spec_source(request: Request) -> JSONResponse:
    """Probe an import source without importing it.

    Backs the Import page's "check before you import" step: a blocked or
    unreachable ``spec_url`` is reported here instead of surfacing as a 400 on
    the import call itself.
    """

    integration_id = request.path_params["integration_id"]
    body = await parse_json_object(request)
    payload = OpenApiValidateSpecSourceRequest.model_validate(body)
    require_scopes(request, [SCOPE_ADMIN, SCOPE_OPERATOR])
    policy = EgressPolicy.from_config(getattr(request.app.state, "config", None))
    factory = get_session_factory(request)
    return await run_in_threadpool(
        _sync_validate_openapi_spec_source, factory, request, integration_id, payload, policy
    )


async def list_openapi_versions(request: Request) -> JSONResponse:
    require_user(request)
    integration_id = request.path_params["integration_id"]
    factory = get_session_factory(request)
    with factory() as session:
        integration = _visible_integration_or_404(session, request, integration_id)
        rows = (
            session.execute(
                select(CaliberOpenApiIntegrationVersion)
                .where(CaliberOpenApiIntegrationVersion.integration_id == integration.integration_id)
                .order_by(CaliberOpenApiIntegrationVersion.created_at.desc())
            )
            .scalars()
            .all()
        )
        data = [OpenApiIntegrationVersionSchema.model_validate(row) for row in rows]
    return envelope_response(data)


async def get_openapi_version(request: Request) -> JSONResponse:
    require_user(request)
    integration_id = request.path_params["integration_id"]
    version_id = request.path_params["version_id"]
    factory = get_session_factory(request)
    with factory() as session:
        integration = _visible_integration_or_404(session, request, integration_id)
        row = _version_for_integration_or_404(session, integration.integration_id, version_id)
        data = OpenApiIntegrationVersionSchema.model_validate(row)
    return envelope_response(data)


def _sync_diff_openapi_version(
    factory: Any,
    request: Request,
    integration_id: str,
    version_id: str,
    compare_to_version_id: str,
) -> JSONResponse:
    """The synchronous body of :func:`diff_openapi_version`."""

    with factory() as session:
        integration = _visible_integration_or_404(session, request, integration_id)
        to_version = _version_for_integration_or_404(session, integration.integration_id, version_id)
        if compare_to_version_id:
            from_version = _version_for_integration_or_404(
                session, integration.integration_id, compare_to_version_id
            )
        else:
            from_version = (
                session.execute(
                    select(CaliberOpenApiIntegrationVersion)
                    .where(
                        CaliberOpenApiIntegrationVersion.integration_id == integration.integration_id,
                        CaliberOpenApiIntegrationVersion.version_id != to_version.version_id,
                        # ``<=``, not ``<``: SQLite's DATETIME column drops the
                        # fractional-second suffix a bound Python datetime carries
                        # (stored "...:04", compared against "...:04.000000"), so a
                        # strict "<" against ``to_version``'s own timestamp can
                        # otherwise text-compare as *less than itself* and match the
                        # row being diffed. Excluding by id is what actually prevents
                        # self-match; ``<=`` just keeps same-second predecessors.
                        CaliberOpenApiIntegrationVersion.created_at <= to_version.created_at,
                    )
                    .order_by(CaliberOpenApiIntegrationVersion.created_at.desc())
                )
                .scalars()
                .first()
            )
            if from_version is None:
                return envelope_response_dict(
                    {
                        "from_version_id": None,
                        "to_version_id": to_version.version_id,
                        "added": [],
                        "removed": [],
                        "changed": [],
                        "unchanged": [],
                        "breaking": [],
                        "summary": {
                            "added_count": 0,
                            "removed_count": 0,
                            "changed_count": 0,
                            "unchanged_count": 0,
                            "breaking_count": 0,
                        },
                        "detail": (
                            f"{to_version.version_id!r} is the first imported version; "
                            "nothing precedes it"
                        ),
                    }
                )
        from_operations = (
            session.execute(
                select(CaliberOpenApiOperation).where(
                    CaliberOpenApiOperation.integration_version_id == from_version.version_id
                )
            )
            .scalars()
            .all()
        )
        to_operations = (
            session.execute(
                select(CaliberOpenApiOperation).where(
                    CaliberOpenApiOperation.integration_version_id == to_version.version_id
                )
            )
            .scalars()
            .all()
        )
        result = diff_operations(from_operations, to_operations)
    return envelope_response_dict(
        {
            "from_version_id": from_version.version_id,
            "to_version_id": to_version.version_id,
            **result,
        }
    )


async def diff_openapi_version(request: Request) -> JSONResponse:
    """Diff one pinned version against another, defaulting to its predecessor.

    Backs §6's spec-drift mitigation: "pin imported versions; diff old vs new;
    require re-review and re-publish." ``compare_to_version_id`` in the body
    picks the ``from`` side explicitly; omitted, it is the version imported
    immediately before ``version_id`` for the same integration.
    """

    integration_id = request.path_params["integration_id"]
    version_id = request.path_params["version_id"]
    body = await parse_json_object(request, allow_empty=True)
    compare_to_version_id = str(body.get("compare_to_version_id") or "").strip()
    require_user(request)
    factory = get_session_factory(request)
    return await run_in_threadpool(
        _sync_diff_openapi_version, factory, request, integration_id, version_id, compare_to_version_id
    )


async def list_openapi_operations(request: Request) -> JSONResponse:
    require_user(request)
    integration_id = request.path_params["integration_id"]
    requested_version_id = request.query_params.get("version_id", "").strip()
    factory = get_session_factory(request)
    with factory() as session:
        integration = _visible_integration_or_404(session, request, integration_id)
        version_id = requested_version_id or (integration.last_imported_version_id or "")
        if not version_id:
            raise HTTPException(
                status_code=404,
                detail=f"OpenAPI integration {integration.integration_id!r} has no imported version",
            )
        _version_for_integration_or_404(session, integration.integration_id, version_id)
        rows = (
            session.execute(
                select(CaliberOpenApiOperation)
                .where(CaliberOpenApiOperation.integration_version_id == version_id)
                .order_by(CaliberOpenApiOperation.path, CaliberOpenApiOperation.method)
            )
            .scalars()
            .all()
        )
        data = [OpenApiOperationSchema.model_validate(row) for row in rows]
    return envelope_response(data)


async def get_openapi_operation(request: Request) -> JSONResponse:
    require_user(request)
    integration_id = request.path_params["integration_id"]
    operation_id = request.path_params["operation_id"]
    factory = get_session_factory(request)
    with factory() as session:
        integration = _visible_integration_or_404(session, request, integration_id)
        row = session.get(CaliberOpenApiOperation, operation_id)
        if row is None:
            raise HTTPException(status_code=404, detail=f"OpenAPI operation {operation_id!r} not found")
        version = session.get(CaliberOpenApiIntegrationVersion, row.integration_version_id)
        if version is None or version.integration_id != integration.integration_id:
            raise HTTPException(status_code=404, detail=f"OpenAPI operation {operation_id!r} not found")
        data = OpenApiOperationSchema.model_validate(row)
    return envelope_response(data)


def _sync_list_openapi_dependencies(
    factory: Any,
    request: Request,
    integration_id: str,
    requested_version_id: str,
    status_filter: str,
) -> JSONResponse:
    with factory() as session:
        integration = _visible_integration_or_404(session, request, integration_id)
        version_id = requested_version_id or (integration.last_imported_version_id or "")
        if not version_id:
            raise HTTPException(
                status_code=404,
                detail=f"OpenAPI integration {integration.integration_id!r} has no imported version",
            )
        _version_for_integration_or_404(session, integration.integration_id, version_id)
        stmt = select(CaliberOpenApiOperationDependency).where(
            CaliberOpenApiOperationDependency.integration_version_id == version_id
        )
        if status_filter:
            stmt = stmt.where(CaliberOpenApiOperationDependency.status == status_filter)
        rows = (
            session.execute(stmt.order_by(CaliberOpenApiOperationDependency.created_at))
            .scalars()
            .all()
        )
        data = [OpenApiOperationDependencySchema.model_validate(row) for row in rows]
    return envelope_response(data)


async def list_openapi_dependencies(request: Request) -> JSONResponse:
    """List canonical dependency rows for one version (default: latest imported)."""

    require_user(request)
    integration_id = request.path_params["integration_id"]
    requested_version_id = request.query_params.get("version_id", "").strip()
    status_filter = request.query_params.get("status", "").strip()
    factory = get_session_factory(request)
    return await run_in_threadpool(
        _sync_list_openapi_dependencies, factory, request, integration_id, requested_version_id, status_filter
    )


def _sync_review_openapi_dependency(
    factory: Any,
    request: Request,
    integration_id: str,
    dependency_id: str,
    payload: OpenApiDependencyReviewRequest,
    actor: str,
) -> JSONResponse:
    with factory() as session:
        integration = _visible_integration_or_404(session, request, integration_id)
        row, version = _dependency_for_integration_or_404(
            session, integration.integration_id, dependency_id
        )
        if row.status == "auto_wired":
            raise HTTPException(
                status_code=409,
                detail="high-confidence dependencies are already auto-wired and do not need review",
            )
        row.status = payload.status
        row.confirmed_by = actor
        row.confirmed_at = datetime.now(timezone.utc)
        if payload.notes:
            row.notes = f"{row.notes}\n{payload.notes}" if row.notes else payload.notes
        _rebuild_graph_snapshot(session, integration, version)
        audit_record(
            session,
            actor=actor,
            action="review_openapi_dependency",
            entity_type="openapi_operation_dependency",
            entity_id=row.dependency_id,
            details={"status": row.status, "dependency_type": row.dependency_type},
        )
        session.commit()
        data = OpenApiOperationDependencySchema.model_validate(row)
    return envelope_response(data)


async def review_openapi_dependency(request: Request) -> JSONResponse:
    """Confirm or reject one suggested/advisory dependency row.

    Implements §5.4's "publish step: operator confirms ambiguous dependencies."
    A rejected row keeps its history (status flips to ``rejected``) rather than
    being deleted, so the audit trail shows a reviewed-and-dismissed row instead
    of no row at all.
    """

    integration_id = request.path_params["integration_id"]
    dependency_id = request.path_params["dependency_id"]
    body = await parse_json_object(request)
    payload = OpenApiDependencyReviewRequest.model_validate(body)
    actor = require_scopes(request, [SCOPE_ADMIN, SCOPE_OPERATOR])
    factory = get_session_factory(request)
    return await run_in_threadpool(
        _sync_review_openapi_dependency, factory, request, integration_id, dependency_id, payload, actor
    )


def _sync_get_openapi_graph(
    factory: Any,
    request: Request,
    integration_id: str,
    requested_version_id: str,
) -> JSONResponse:
    with factory() as session:
        integration = _visible_integration_or_404(session, request, integration_id)
        version_id = requested_version_id or (integration.last_imported_version_id or "")
        if not version_id:
            raise HTTPException(
                status_code=404,
                detail=f"OpenAPI integration {integration.integration_id!r} has no imported version",
            )
        version = _version_for_integration_or_404(session, integration.integration_id, version_id)
        snapshot = version.graph_snapshot
        if snapshot is None:
            snapshot = _rebuild_graph_snapshot(session, integration, version)
            session.commit()
    return envelope_response_dict(snapshot)


async def get_openapi_graph(request: Request) -> JSONResponse:
    """Serve the derived API dependency graph for the latest (or requested) version.

    Served from the cached ``graph_snapshot`` per §5.2's v1 storage
    recommendation; rebuilt here only if a version predates that cache (e.g. an
    older import) rather than on every request.
    """

    require_user(request)
    integration_id = request.path_params["integration_id"]
    requested_version_id = request.query_params.get("version_id", "").strip()
    factory = get_session_factory(request)
    return await run_in_threadpool(
        _sync_get_openapi_graph, factory, request, integration_id, requested_version_id
    )


async def list_openapi_tool_drafts(request: Request) -> JSONResponse:
    require_user(request)
    integration_id = request.path_params["integration_id"]
    factory = get_session_factory(request)
    with factory() as session:
        integration = _visible_integration_or_404(session, request, integration_id)
        rows = (
            session.execute(
                select(CaliberOpenApiToolDraft)
                .where(CaliberOpenApiToolDraft.integration_id == integration.integration_id)
                .order_by(CaliberOpenApiToolDraft.created_at.desc())
            )
            .scalars()
            .all()
        )
        data = [OpenApiToolDraftSchema.model_validate(row) for row in rows]
    return envelope_response(data)


async def get_openapi_tool_draft(request: Request) -> JSONResponse:
    require_user(request)
    integration_id = request.path_params["integration_id"]
    draft_id = request.path_params["draft_id"]
    factory = get_session_factory(request)
    with factory() as session:
        integration = _visible_integration_or_404(session, request, integration_id)
        row = _draft_for_integration_or_404(session, integration.integration_id, draft_id)
        data = OpenApiToolDraftSchema.model_validate(row)
    return envelope_response(data)


def _select_operations(
    session: Session,
    version: CaliberOpenApiIntegrationVersion,
    payload: OpenApiGenerateToolDraftsRequest,
) -> list[CaliberOpenApiOperation]:
    """Resolve the operations a generate request names, by id and/or filter.

    Backs §6's "support selection by tag/path/method" tool-explosion mitigation:
    an operator can request a slice of a large spec (``tags=["tickets"]``,
    ``methods=["GET"]``) instead of enumerating operation ids by hand.
    """

    stmt = select(CaliberOpenApiOperation).where(
        CaliberOpenApiOperation.integration_version_id == version.version_id
    )
    if payload.methods:
        methods = {method.upper() for method in payload.methods}
        stmt = stmt.where(CaliberOpenApiOperation.method.in_(methods))
    rows = session.execute(stmt).scalars().all()

    if payload.tags:
        wanted_tags = set(payload.tags)
        rows = [row for row in rows if wanted_tags & set(row.tags or [])]
    if payload.path_prefix:
        rows = [row for row in rows if row.path.startswith(payload.path_prefix)]

    by_id = {row.operation_id: row for row in rows}
    selected: list[CaliberOpenApiOperation] = []
    seen: set[str] = set()
    for operation_id in payload.operation_ids:
        operation, operation_version = _operation_for_integration_or_404(
            session, version.integration_id, operation_id
        )
        if operation_version.version_id != version.version_id:
            raise HTTPException(
                status_code=400,
                detail=(
                    f"operation {operation.operation_id!r} belongs to version "
                    f"{operation_version.version_id!r}, not requested version {version.version_id!r}"
                ),
            )
        if operation.operation_id not in seen:
            selected.append(operation)
            seen.add(operation.operation_id)
    if payload.tags or payload.methods or payload.path_prefix:
        # Filter-selected operations already passed the id ownership check above
        # (they were queried scoped to this version), so no re-validation needed.
        for operation_id, operation in by_id.items():
            if operation_id not in seen:
                selected.append(operation)
                seen.add(operation_id)
    return selected


async def generate_openapi_tool_drafts(request: Request) -> JSONResponse:
    body = await parse_json_object(request)
    payload = OpenApiGenerateToolDraftsRequest.model_validate(body)
    actor = require_scopes(request, [SCOPE_ADMIN, SCOPE_OPERATOR])
    integration_id = request.path_params["integration_id"]
    factory = get_session_factory(request)
    with factory() as session:
        integration = _visible_integration_or_404(session, request, integration_id)
        version_id = payload.version_id or (integration.last_imported_version_id or "")
        if not version_id:
            raise HTTPException(
                status_code=404,
                detail=f"OpenAPI integration {integration.integration_id!r} has no imported version",
            )
        version = _version_for_integration_or_404(session, integration.integration_id, version_id)
        auth_binding = (
            payload.auth_binding.model_dump(mode="python", exclude_none=True)
            if payload.auth_binding is not None
            else None
        )
        server_url = (payload.server_url or (version.server_urls[0] if version.server_urls else "")).strip()
        operations = _select_operations(session, version, payload)
        if not operations:
            raise HTTPException(
                status_code=400,
                detail="the selection (operation_ids/tags/methods/path_prefix) matched no operations",
            )

        drafts: list[CaliberOpenApiToolDraft] = []
        if payload.group_as_pack and len(operations) > 1:
            drafts.append(
                _build_draft(
                    integration=integration,
                    version=version,
                    operations=operations,
                    server_url=server_url,
                    auth_binding=auth_binding,
                    payload=payload,
                    actor=actor,
                )
            )
        else:
            for operation in operations:
                drafts.append(
                    _build_draft(
                        integration=integration,
                        version=version,
                        operations=[operation],
                        server_url=server_url,
                        auth_binding=auth_binding,
                        payload=payload,
                        actor=actor,
                    )
                )
        for draft in drafts:
            session.add(draft)
        if drafts and all(
            _draft_ready_for_publication(draft, _draft_operations(session, integration.integration_id, draft))
            for draft in drafts
        ):
            integration.status = "ready"
        audit_record(
            session,
            actor=actor,
            action="generate_openapi_tool_drafts",
            entity_type="openapi_integration",
            entity_id=integration.integration_id,
            details={
                "draft_count": len(drafts),
                "version_id": version.version_id,
                "operation_count": len(operations),
                "group_as_pack": payload.group_as_pack,
            },
        )
        session.commit()
        data = [OpenApiToolDraftSchema.model_validate(row) for row in drafts]
    return envelope_response(data, status_code=201)


def _build_draft(
    *,
    integration: CaliberOpenApiIntegration,
    version: CaliberOpenApiIntegrationVersion,
    operations: list[CaliberOpenApiOperation],
    server_url: str,
    auth_binding: dict[str, Any] | None,
    payload: OpenApiGenerateToolDraftsRequest,
    actor: str,
) -> CaliberOpenApiToolDraft:
    requires_auth = any(operation.auth_schemes for operation in operations)
    is_ready = bool(server_url) and (not requires_auth or auth_binding is not None)
    if len(operations) > 1:
        side_effect_level = pack_side_effect_level(operations)
        return CaliberOpenApiToolDraft(
            draft_id=new_openapi_tool_draft_id(),
            integration_id=integration.integration_id,
            integration_version_id=version.version_id,
            operation_id=operations[0].operation_id,
            additional_operation_ids=[op.operation_id for op in operations[1:]],
            name=build_tool_pack_name(operations),
            description=build_tool_pack_description(integration, operations),
            owner=actor,
            status="ready" if is_ready else "draft",
            server_url=server_url,
            auth_binding=auth_binding,
            input_schema=build_tool_pack_input_schema(operations),
            output_schema=build_tool_output_schema(),
            execution_config=build_pack_execution_config(
                integration=integration,
                version=version,
                operations=operations,
                server_url=server_url,
                auth_binding=auth_binding,
            ),
            side_effect_level=side_effect_level,
            requires_approval=payload.requires_approval or side_effect_level != "read",
            allow_in_preview=payload.allow_in_preview,
            secret_refs=auth_binding_secret_refs(auth_binding),
        )
    operation = operations[0]
    return CaliberOpenApiToolDraft(
        draft_id=new_openapi_tool_draft_id(),
        integration_id=integration.integration_id,
        integration_version_id=version.version_id,
        operation_id=operation.operation_id,
        name=build_tool_draft_name(operation),
        description=build_tool_draft_description(integration, operation),
        owner=actor,
        status="ready" if is_ready else "draft",
        server_url=server_url,
        auth_binding=auth_binding,
        input_schema=build_tool_input_schema(operation),
        output_schema=build_tool_output_schema(),
        execution_config=build_execution_config(
            integration=integration,
            version=version,
            operation=operation,
            server_url=server_url,
            auth_binding=auth_binding,
        ),
        side_effect_level=operation.side_effect_level,
        requires_approval=payload.requires_approval or operation.side_effect_level != "read",
        allow_in_preview=payload.allow_in_preview,
        secret_refs=auth_binding_secret_refs(auth_binding),
    )


async def update_openapi_tool_draft(request: Request) -> JSONResponse:
    draft_id = request.path_params["draft_id"]
    integration_id = request.path_params["integration_id"]
    body = await parse_json_object(request)
    payload = OpenApiToolDraftUpdateRequest.model_validate(body)
    actor = require_scopes(request, [SCOPE_ADMIN, SCOPE_OPERATOR])
    changes = payload.model_dump(exclude_unset=True)
    if not changes:
        raise HTTPException(status_code=400, detail="request body must include at least one field")
    factory = get_session_factory(request)
    with factory() as session:
        integration = _visible_integration_or_404(session, request, integration_id)
        draft = _draft_for_integration_or_404(session, integration.integration_id, draft_id)
        operations = _draft_operations(session, integration.integration_id, draft)
        diff: dict[str, Any] = {}
        if "name" in changes and changes["name"] != draft.name:
            diff["name"] = {"from": draft.name, "to": changes["name"]}
            draft.name = str(changes["name"])
        if "description" in changes and changes["description"] != draft.description:
            diff["description"] = {"from": draft.description, "to": changes["description"]}
            draft.description = str(changes["description"])
        if "server_url" in changes and changes["server_url"] != draft.server_url:
            diff["server_url"] = {"from": draft.server_url, "to": changes["server_url"]}
            draft.server_url = str(changes["server_url"] or "")
        if "auth_binding" in changes:
            new_auth_binding = (
                payload.auth_binding.model_dump(mode="python", exclude_none=True)
                if payload.auth_binding is not None
                else None
            )
            diff["auth_binding"] = {"from": bool(draft.auth_binding), "to": bool(new_auth_binding)}
            draft.auth_binding = new_auth_binding
            draft.secret_refs = auth_binding_secret_refs(new_auth_binding)
        if "requires_approval" in changes and changes["requires_approval"] != draft.requires_approval:
            diff["requires_approval"] = {
                "from": draft.requires_approval,
                "to": changes["requires_approval"],
            }
            draft.requires_approval = bool(changes["requires_approval"])
        if "allow_in_preview" in changes and changes["allow_in_preview"] != draft.allow_in_preview:
            diff["allow_in_preview"] = {
                "from": draft.allow_in_preview,
                "to": changes["allow_in_preview"],
            }
            draft.allow_in_preview = bool(changes["allow_in_preview"])
        if "status" in changes:
            requested_status = str(changes["status"])
            if requested_status == "published" and draft.published_tool_id is None:
                raise HTTPException(
                    status_code=409,
                    detail="tool drafts become 'published' only through the publish route",
                )
            if requested_status != draft.status:
                diff["status"] = {"from": draft.status, "to": requested_status}
                draft.status = requested_status
        version = session.get(CaliberOpenApiIntegrationVersion, draft.integration_version_id)
        assert version is not None
        new_auth_binding = dict(draft.auth_binding) if isinstance(draft.auth_binding, dict) else None
        if len(operations) > 1:
            draft.execution_config = build_pack_execution_config(
                integration=integration,
                version=version,
                operations=operations,
                server_url=draft.server_url,
                auth_binding=new_auth_binding,
            )
        else:
            draft.execution_config = build_execution_config(
                integration=integration,
                version=version,
                operation=operations[0],
                server_url=draft.server_url,
                auth_binding=new_auth_binding,
            )
        if draft.status != "published" and _draft_ready_for_publication(draft, operations):
            draft.status = "ready"
        audit_record(
            session,
            actor=actor,
            action="update_openapi_tool_draft",
            entity_type="openapi_tool_draft",
            entity_id=draft.draft_id,
            details={"changes": diff},
        )
        session.commit()
        data = OpenApiToolDraftSchema.model_validate(draft)
    return envelope_response(data)


async def preview_openapi_tool_draft(request: Request) -> JSONResponse:
    """Execute one real upstream call for an unpublished draft.

    This is a live effect, not a simulation, so it is gated the same way a preview
    run of a registered tool is: a draft the operator has not marked
    ``allow_in_preview`` cannot be fired here regardless of scope. Without that
    gate, ``preview`` was a way to perform an approval-gated write before anyone
    approved it.
    """

    integration_id = request.path_params["integration_id"]
    draft_id = request.path_params["draft_id"]
    body = await parse_json_object(request)
    payload = OpenApiToolDraftPreviewRequest.model_validate(body)
    actor = require_scopes(request, [SCOPE_ADMIN, SCOPE_OPERATOR])
    policy = EgressPolicy.from_config(getattr(request.app.state, "config", None))
    factory = get_session_factory(request)
    with factory() as session:
        integration = _visible_integration_or_404(session, request, integration_id)
        draft = _draft_for_integration_or_404(session, integration.integration_id, draft_id)
        if not draft.allow_in_preview:
            raise HTTPException(
                status_code=409,
                detail=(
                    f"OpenAPI tool draft {draft.draft_id!r} is not previewable: set "
                    "'allow_in_preview' before running a live upstream call"
                ),
            )
        error: str | None = None
        result: dict[str, Any] | None = None
        try:
            result = execute_openapi_http_tool(
                execution_config=dict(draft.execution_config or {}),
                input_schema=(
                    dict(draft.input_schema or {}) if isinstance(draft.input_schema, dict) else None
                ),
                input_data=dict(payload.input),
                egress_policy=policy,
            )
        except Exception as exc:
            error = str(exc)
        # Audited whether or not it succeeded: a refused or failed outbound call is
        # exactly as interesting to a reviewer as one that worked.
        audit_record(
            session,
            actor=actor,
            action="preview_openapi_tool_draft",
            entity_type="openapi_tool_draft",
            entity_id=draft.draft_id,
            details=_execution_audit_details(draft, result=result, error=error),
        )
        session.commit()
    if error is not None:
        raise HTTPException(status_code=400, detail=error)
    return envelope_response_dict({"draft_id": draft_id, "result": result})


def _execution_audit_details(
    draft: CaliberOpenApiToolDraft,
    *,
    result: dict[str, Any] | None,
    error: str | None,
) -> dict[str, Any]:
    """Audit context for one outbound call.

    Deliberately records the *shape* of the exchange — operation, status, attempts
    — and never the request body, response body, or resolved credential. Those are
    the two things a governed integration must not leak into the audit log.
    """

    config = dict(draft.execution_config or {})
    details: dict[str, Any] = {
        "integration_id": draft.integration_id,
        "integration_version_id": draft.integration_version_id,
        "operation_id": draft.operation_id,
        "operation_key": config.get("operation_key"),
        "method": config.get("method"),
        "server_url": config.get("server_url"),
        "side_effect_level": draft.side_effect_level,
        "secret_refs": list(draft.secret_refs or []),
    }
    if result is not None:
        details["status_code"] = result.get("status_code")
        details["attempts"] = result.get("attempts")
    if error is not None:
        details["error"] = error
    return details


async def publish_openapi_tool_draft(request: Request) -> JSONResponse:
    integration_id = request.path_params["integration_id"]
    draft_id = request.path_params["draft_id"]
    body = await parse_json_object(request)
    payload = OpenApiPublishToolDraftRequest.model_validate(body)
    actor = require_scopes(request, [SCOPE_ADMIN])
    factory = get_session_factory(request)
    with factory() as session:
        integration = _visible_integration_or_404(session, request, integration_id)
        draft = _draft_for_integration_or_404(session, integration.integration_id, draft_id)
        operations = _draft_operations(session, integration.integration_id, draft)
        if draft.published_tool_id is not None:
            raise HTTPException(
                status_code=409,
                detail=(
                    f"OpenAPI tool draft {draft.draft_id!r} is already published as "
                    f"{draft.published_tool_id!r}"
                ),
            )
        if not _draft_ready_for_publication(draft, operations):
            raise HTTPException(
                status_code=409,
                detail="tool draft is not ready: configure a server URL and required auth binding first",
            )
        unresolved = [ref for ref in (draft.secret_refs or []) if not resolve_secret(ref)]
        if unresolved:
            raise HTTPException(
                status_code=409,
                detail=(
                    "tool draft has unresolved secret references: "
                    + ", ".join(repr(ref) for ref in unresolved)
                ),
            )
        tool_name = payload.name or draft.name
        existing = (
            session.execute(
                select(CaliberToolRegistry).where(
                    CaliberToolRegistry.name == tool_name,
                    CaliberToolRegistry.version == payload.version,
                )
            )
            .scalars()
            .first()
        )
        if existing is not None:
            raise HTTPException(
                status_code=409,
                detail=f"tool {tool_name!r} version {payload.version!r} already registered",
            )
        tool = CaliberToolRegistry(
            tool_id=new_tool_id(),
            name=tool_name,
            version=payload.version,
            description=payload.description or draft.description,
            module_path="<openapi_http>",
            callable_name="invoke",
            execution_backend="openapi_http",
            backend_config=dict(draft.execution_config or {}),
            input_schema=dict(draft.input_schema or {}) if isinstance(draft.input_schema, dict) else None,
            output_schema=dict(draft.output_schema or {}) if isinstance(draft.output_schema, dict) else None,
            side_effect_level=draft.side_effect_level,
            requires_approval=draft.requires_approval,
            allow_in_preview=draft.allow_in_preview,
            secret_refs=list(draft.secret_refs or []),
            owner=actor,
            project_id=integration.project_id,
            visibility=integration.visibility,
            status="active",
        )
        session.add(tool)
        session.flush()
        draft.published_tool_id = tool.tool_id
        draft.status = "published"
        draft.name = tool_name
        draft.description = payload.description or draft.description
        integration.status = "published"
        version = session.get(CaliberOpenApiIntegrationVersion, draft.integration_version_id)
        if version is not None:
            _rebuild_graph_snapshot(session, integration, version)
        audit_record(
            session,
            actor=actor,
            action="publish_openapi_tool_draft",
            entity_type="openapi_tool_draft",
            entity_id=draft.draft_id,
            details={"tool_id": tool.tool_id, "tool_name": tool.name, "tool_version": tool.version},
        )
        session.commit()
        return envelope_response_dict(
            {
                "draft": OpenApiToolDraftSchema.model_validate(draft).model_dump(mode="json"),
                "tool": ToolSchema.model_validate(tool).model_dump(mode="json"),
            },
            status_code=201,
        )


async def validate_openapi_credential_binding(request: Request) -> JSONResponse:
    integration_id = request.path_params["integration_id"]
    body = await parse_json_object(request)
    payload = OpenApiValidateCredentialBindingRequest.model_validate(body)
    require_scopes(request, [SCOPE_ADMIN, SCOPE_OPERATOR])
    factory = get_session_factory(request)
    with factory() as session:
        _visible_integration_or_404(session, request, integration_id)
    auth_binding = payload.auth_binding.model_dump(mode="python", exclude_none=True)
    refs = auth_binding_secret_refs(auth_binding)
    resolutions = [
        {"secret_ref": ref, "resolved": bool(resolve_secret(ref))}
        for ref in refs
    ]
    return envelope_response_dict(
        {
            "valid": all(item["resolved"] for item in resolutions),
            "kind": auth_binding.get("kind", "none"),
            "secret_refs": refs,
            "resolutions": resolutions,
        }
    )


def register(app: Starlette) -> None:
    app.routes.append(Route(LIST_PATH, list_openapi_integrations, methods=["GET"]))
    app.routes.append(Route(LIST_PATH, create_openapi_integration, methods=["POST"]))
    app.routes.append(Route(DETAIL_PATH, get_openapi_integration, methods=["GET"]))
    app.routes.append(Route(DETAIL_PATH, update_openapi_integration, methods=["PATCH"]))
    app.routes.append(Route(ARCHIVE_PATH, archive_openapi_integration, methods=["POST"]))
    app.routes.append(Route(IMPORT_PATH, import_openapi_version, methods=["POST"]))
    app.routes.append(Route(REIMPORT_PATH, reimport_openapi_version, methods=["POST"]))
    app.routes.append(
        Route(VALIDATE_SPEC_SOURCE_PATH, validate_openapi_spec_source, methods=["POST"])
    )
    app.routes.append(Route(VERSIONS_PATH, list_openapi_versions, methods=["GET"]))
    app.routes.append(Route(VERSION_DETAIL_PATH, get_openapi_version, methods=["GET"]))
    app.routes.append(Route(VERSION_DIFF_PATH, diff_openapi_version, methods=["POST"]))
    app.routes.append(Route(OPERATIONS_PATH, list_openapi_operations, methods=["GET"]))
    app.routes.append(Route(OPERATION_DETAIL_PATH, get_openapi_operation, methods=["GET"]))
    app.routes.append(Route(DEPENDENCIES_PATH, list_openapi_dependencies, methods=["GET"]))
    app.routes.append(
        Route(DEPENDENCY_DETAIL_PATH, review_openapi_dependency, methods=["PATCH"])
    )
    app.routes.append(Route(GRAPH_PATH, get_openapi_graph, methods=["GET"]))
    app.routes.append(Route(TOOL_DRAFTS_PATH, list_openapi_tool_drafts, methods=["GET"]))
    app.routes.append(Route(TOOL_DRAFT_GENERATE_PATH, generate_openapi_tool_drafts, methods=["POST"]))
    app.routes.append(Route(TOOL_DRAFT_DETAIL_PATH, get_openapi_tool_draft, methods=["GET"]))
    app.routes.append(Route(TOOL_DRAFT_DETAIL_PATH, update_openapi_tool_draft, methods=["PATCH"]))
    app.routes.append(Route(TOOL_DRAFT_PREVIEW_PATH, preview_openapi_tool_draft, methods=["POST"]))
    app.routes.append(Route(TOOL_DRAFT_PUBLISH_PATH, publish_openapi_tool_draft, methods=["POST"]))
    app.routes.append(
        Route(
            VALIDATE_CREDENTIAL_BINDING_PATH,
            validate_openapi_credential_binding,
            methods=["POST"],
        )
    )
