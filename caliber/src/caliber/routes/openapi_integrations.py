"""Governed OpenAPI integration drafts.

This is the control-plane import surface for external OpenAPI contracts. It
creates durable integration shells, pins imported contract snapshots, generates
curated tool drafts, and can publish approved drafts into CALIBER's governed
tool registry via a declarative HTTP execution backend.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session
from starlette.applications import Starlette
from starlette.exceptions import HTTPException
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from caliber.audit import record as audit_record
from caliber.auth import SCOPE_ADMIN, SCOPE_OPERATOR, require_scopes, require_user, resolve_identity
from caliber.db.models import (
    CaliberOpenApiIntegration,
    CaliberOpenApiToolDraft,
    CaliberOpenApiIntegrationVersion,
    CaliberOpenApiOperation,
    CaliberToolRegistry,
)
from caliber.db.scoping import apply_visibility_filter, get_visible
from caliber.ids import (
    new_openapi_integration_id,
    new_openapi_integration_version_id,
    new_openapi_operation_id,
    new_openapi_tool_draft_id,
    new_tool_id,
)
from caliber.integrations.openapi.executor import execute_openapi_http_tool
from caliber.integrations.openapi.normalize import (
    OpenApiImportError,
    normalize_openapi_document,
    parse_openapi_text,
    spec_sha256,
)
from caliber.integrations.openapi.tool_drafts import (
    auth_binding_secret_refs,
    build_execution_config,
    build_tool_draft_description,
    build_tool_draft_name,
    build_tool_input_schema,
    build_tool_output_schema,
)
from caliber.routes._deps import (
    envelope_response,
    envelope_response_dict,
    get_session_factory,
    parse_json_object,
    visibility_param,
)
from caliber.schemas import (
    OpenApiGenerateToolDraftsRequest,
    OpenApiImportRequest,
    OpenApiPublishToolDraftRequest,
    OpenApiToolDraftPreviewRequest,
    OpenApiToolDraftSchema,
    OpenApiToolDraftUpdateRequest,
    OpenApiIntegrationCreateRequest,
    OpenApiIntegrationSchema,
    OpenApiIntegrationUpdateRequest,
    OpenApiIntegrationVersionSchema,
    OpenApiOperationSchema,
    OpenApiValidateCredentialBindingRequest,
    ToolSchema,
)
from caliber.secrets import resolve_secret

PREFIX = "/ajax-api/2.0/mlflow/caliber"
LIST_PATH = PREFIX + "/openapi-integrations"
DETAIL_PATH = LIST_PATH + "/{integration_id}"
ARCHIVE_PATH = DETAIL_PATH + "/archive"
IMPORT_PATH = DETAIL_PATH + "/import"
VERSIONS_PATH = DETAIL_PATH + "/versions"
VERSION_DETAIL_PATH = VERSIONS_PATH + "/{version_id}"
OPERATIONS_PATH = DETAIL_PATH + "/operations"
OPERATION_DETAIL_PATH = OPERATIONS_PATH + "/{operation_id}"
TOOL_DRAFTS_PATH = DETAIL_PATH + "/tool-drafts"
TOOL_DRAFT_GENERATE_PATH = TOOL_DRAFTS_PATH + "/generate"
TOOL_DRAFT_DETAIL_PATH = TOOL_DRAFTS_PATH + "/{draft_id}"
TOOL_DRAFT_PREVIEW_PATH = TOOL_DRAFT_DETAIL_PATH + "/preview"
TOOL_DRAFT_PUBLISH_PATH = TOOL_DRAFT_DETAIL_PATH + "/publish"
VALIDATE_CREDENTIAL_BINDING_PATH = DETAIL_PATH + "/validate-credential-binding"

_LIST_STATUS_VALUES = frozenset({"draft", "review", "ready", "published", "archived", "all"})


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


def _draft_ready_for_publication(draft: CaliberOpenApiToolDraft, operation: CaliberOpenApiOperation) -> bool:
    if not draft.server_url.strip():
        return False
    if operation.auth_schemes and not isinstance(draft.auth_binding, dict):
        return False
    return True


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


async def import_openapi_version(request: Request) -> JSONResponse:
    body = await parse_json_object(request)
    payload = OpenApiImportRequest.model_validate(body)
    actor = require_scopes(request, [SCOPE_ADMIN, SCOPE_OPERATOR])
    integration_id = request.path_params["integration_id"]
    factory = get_session_factory(request)
    with factory() as session:
        integration = _visible_integration_or_404(session, request, integration_id)
        if integration.status == "archived":
            raise HTTPException(
                status_code=409,
                detail="archived OpenAPI integrations cannot import new versions",
            )
        try:
            document = parse_openapi_text(payload.spec_text)
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
            source_kind=payload.source_kind,
            source_ref=payload.source_ref or "",
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
        for operation in operations:
            session.add(
                CaliberOpenApiOperation(
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
            )

        integration.last_imported_version_id = version.version_id
        integration.status = "review"
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
            },
        )
        session.commit()
        data = OpenApiIntegrationVersionSchema.model_validate(version)
    return envelope_response(data, status_code=201)


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
        drafts: list[CaliberOpenApiToolDraft] = []
        for operation_id in payload.operation_ids:
            operation, operation_version = _operation_for_integration_or_404(
                session, integration.integration_id, operation_id
            )
            if operation_version.version_id != version.version_id:
                raise HTTPException(
                    status_code=400,
                    detail=(
                        f"operation {operation.operation_id!r} belongs to version "
                        f"{operation_version.version_id!r}, not requested version {version.version_id!r}"
                    ),
                )
            draft = CaliberOpenApiToolDraft(
                draft_id=new_openapi_tool_draft_id(),
                integration_id=integration.integration_id,
                integration_version_id=version.version_id,
                operation_id=operation.operation_id,
                name=build_tool_draft_name(operation),
                description=build_tool_draft_description(integration, operation),
                owner=actor,
                status="ready"
                if server_url and (not operation.auth_schemes or auth_binding is not None)
                else "draft",
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
            session.add(draft)
            drafts.append(draft)
        if drafts and all(
            _draft_ready_for_publication(draft, _operation_for_integration_or_404(session, integration.integration_id, draft.operation_id)[0])
            for draft in drafts
        ):
            integration.status = "ready"
        audit_record(
            session,
            actor=actor,
            action="generate_openapi_tool_drafts",
            entity_type="openapi_integration",
            entity_id=integration.integration_id,
            details={"draft_count": len(drafts), "version_id": version.version_id},
        )
        session.commit()
        data = [OpenApiToolDraftSchema.model_validate(row) for row in drafts]
    return envelope_response(data, status_code=201)


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
        operation, version = _operation_for_integration_or_404(
            session, integration.integration_id, draft.operation_id
        )
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
        draft.execution_config = build_execution_config(
            integration=integration,
            version=version,
            operation=operation,
            server_url=draft.server_url,
            auth_binding=dict(draft.auth_binding) if isinstance(draft.auth_binding, dict) else None,
        )
        if draft.status != "published" and _draft_ready_for_publication(draft, operation):
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
    integration_id = request.path_params["integration_id"]
    draft_id = request.path_params["draft_id"]
    body = await parse_json_object(request)
    payload = OpenApiToolDraftPreviewRequest.model_validate(body)
    require_scopes(request, [SCOPE_ADMIN, SCOPE_OPERATOR])
    factory = get_session_factory(request)
    with factory() as session:
        integration = _visible_integration_or_404(session, request, integration_id)
        draft = _draft_for_integration_or_404(session, integration.integration_id, draft_id)
        try:
            result = execute_openapi_http_tool(
                execution_config=dict(draft.execution_config or {}),
                input_schema=dict(draft.input_schema or {}) if isinstance(draft.input_schema, dict) else None,
                input_data=dict(payload.input),
            )
        except Exception as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
    return envelope_response_dict({"draft_id": draft_id, "result": result})


async def publish_openapi_tool_draft(request: Request) -> JSONResponse:
    integration_id = request.path_params["integration_id"]
    draft_id = request.path_params["draft_id"]
    body = await parse_json_object(request)
    payload = OpenApiPublishToolDraftRequest.model_validate(body)
    actor = require_scopes(request, [SCOPE_ADMIN])
    identity = resolve_identity(request)
    factory = get_session_factory(request)
    with factory() as session:
        integration = _visible_integration_or_404(session, request, integration_id)
        draft = _draft_for_integration_or_404(session, integration.integration_id, draft_id)
        operation, _version = _operation_for_integration_or_404(
            session, integration.integration_id, draft.operation_id
        )
        if draft.published_tool_id is not None:
            raise HTTPException(
                status_code=409,
                detail=(
                    f"OpenAPI tool draft {draft.draft_id!r} is already published as "
                    f"{draft.published_tool_id!r}"
                ),
            )
        if not _draft_ready_for_publication(draft, operation):
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
    app.routes.append(Route(VERSIONS_PATH, list_openapi_versions, methods=["GET"]))
    app.routes.append(Route(VERSION_DETAIL_PATH, get_openapi_version, methods=["GET"]))
    app.routes.append(Route(OPERATIONS_PATH, list_openapi_operations, methods=["GET"]))
    app.routes.append(Route(OPERATION_DETAIL_PATH, get_openapi_operation, methods=["GET"]))
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
