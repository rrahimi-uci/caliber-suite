"""``/caliber/projects`` — workspaces that group uploaded files.

A project is a lightweight workspace (storage doc §2.1: ``project/{project_id}``).
It exposes CRUD plus a files sub-resource so users can create a project and
upload/browse/download files in it from the UI, independent of any workflow run.
File operations reuse the storage service + the multipart/error helpers from
:mod:`caliber.routes.files`.
"""

from __future__ import annotations

import importlib.util
from datetime import datetime, timezone
from pathlib import PurePosixPath
from typing import Any

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session
from starlette.applications import Starlette
from starlette.exceptions import HTTPException
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

from caliber.audit import record as audit_record
from caliber.auth import (
    SCOPE_ADMIN,
    SCOPE_OPERATOR,
    require_scopes,
    require_user,
    resolve_identity,
)
from caliber.db.models import CaliberProject, CaliberWorkflowFile
from caliber.ids import new_project_id
from caliber.routes._deps import (
    envelope_response_dict,
    get_session_factory,
    get_working_dir_service,
    parse_json_object,
)
from caliber.routes.files import _read_upload, _storage_http
from caliber.storage import (
    VISIBLE_STATUSES,
    CaliberFileRecord,
    StorageError,
    StorageValidationError,
    build_ref,
    safe_relative_path,
)

PREFIX = "/ajax-api/2.0/mlflow/caliber"
LIST_PATH = PREFIX + "/projects"
STORAGE_PATH = PREFIX + "/projects/storage"
DETAIL_PATH = PREFIX + "/projects/{project_id}"
FILES_PATH = PREFIX + "/projects/{project_id}/files"
FOLDERS_PATH = PREFIX + "/projects/{project_id}/folders"
FILE_PATH = PREFIX + "/projects/{project_id}/files/{file_id}"
FILE_CONTENT_PATH = PREFIX + "/projects/{project_id}/files/{file_id}/content"

_VALID_STATUSES = frozenset({"active", "archived"})


def _active_storage_backend(request: Request) -> str:
    config = getattr(request.app.state, "config", None)
    workflow_storage = getattr(config, "workflow_storage", None)
    return str(getattr(workflow_storage, "backend", "local"))


def _backend_label(backend: str) -> str:
    if backend == "s3":
        return "MinIO / S3-compatible object storage"
    return "Local file system"


def _workflow_storage_config(request: Request) -> Any:
    config = getattr(request.app.state, "config", None)
    return getattr(config, "workflow_storage", None)


def _backend_configuration_status(request: Request, backend: str) -> tuple[bool, str | None]:
    if backend == "local":
        return True, None
    if backend == "s3":
        workflow_storage = _workflow_storage_config(request)
        if not getattr(workflow_storage, "bucket", None):
            return False, "Set CALIBER_WORKFLOW_STORAGE_BUCKET."
        if importlib.util.find_spec("boto3") is None:
            return False, "Install the s3 extra so boto3 is available."
        return True, None
    return False, f"Unknown storage backend {backend!r}."


def _is_backend_configured(request: Request, backend: str) -> bool:
    configured, _reason = _backend_configuration_status(request, backend)
    return configured


def _require_configured_backend(request: Request, backend: str) -> None:
    if _is_backend_configured(request, backend):
        return
    if backend == "s3":
        _configured, reason = _backend_configuration_status(request, backend)
        raise HTTPException(
            status_code=400,
            detail=f"storage backend 's3' is not configured. {reason}",
        )
    raise HTTPException(status_code=400, detail=f"storage backend {backend!r} is not configured")


def _available_backend_options(request: Request) -> list[dict[str, Any]]:
    active = _active_storage_backend(request)
    local_configured, local_reason = _backend_configuration_status(request, "local")
    s3_configured, s3_reason = _backend_configuration_status(request, "s3")
    return [
        {
            "id": "local",
            "label": _backend_label("local"),
            "active": active == "local",
            "configured": local_configured,
            "reason": local_reason,
        },
        {
            "id": "s3",
            "label": _backend_label("s3"),
            "active": active == "s3",
            "configured": s3_configured,
            "reason": s3_reason,
        },
    ]


def _project_storage_service(request: Request, project: CaliberProject) -> Any:
    _require_configured_backend(request, project.storage_backend)
    return get_working_dir_service(request).for_backend(project.storage_backend)


def _project_to_dict(
    row: CaliberProject,
    *,
    file_count: int | None = None,
    storage_backend: str | None = None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "project_id": row.project_id,
        "name": row.name,
        "description": row.description,
        "owner": row.owner,
        "status": row.status,
        "storage_backend": storage_backend or row.storage_backend,
        "created_at": row.created_at.isoformat() if row.created_at else None,
        "updated_at": row.updated_at.isoformat() if row.updated_at else None,
    }
    if file_count is not None:
        payload["file_count"] = file_count
    return payload


def _normalize_requested_backend(value: object) -> str | None:
    if not isinstance(value, str) or not value.strip():
        return None
    raw = value.strip().lower()
    if raw in {"minio", "s3"}:
        return "s3"
    if raw in {"local", "filesystem", "file-system", "file_system"}:
        return "local"
    raise HTTPException(status_code=400, detail=f"unsupported storage backend {value!r}")


def _is_directory_marker(row: CaliberWorkflowFile) -> bool:
    meta = row.file_metadata if isinstance(row.file_metadata, dict) else {}
    return bool(meta.get("directory_marker"))


def _folder_path_from_marker(row: CaliberWorkflowFile) -> str | None:
    meta = row.file_metadata if isinstance(row.file_metadata, dict) else {}
    raw = meta.get("directory_path")
    if isinstance(raw, str) and raw.strip():
        try:
            return safe_relative_path(raw)
        except StorageValidationError:
            return None
    if row.relative_path.endswith("/.caliber-folder"):
        return row.relative_path.removesuffix("/.caliber-folder")
    return None


def _folder_payload(
    project_id: str, path: str, *, row: CaliberWorkflowFile | None = None
) -> dict[str, Any]:
    safe_path = safe_relative_path(path)
    payload: dict[str, Any] = {
        "path": safe_path,
        "name": PurePosixPath(safe_path).name,
        "file_ref": build_ref("projects", project_id, "metadata", f"{safe_path}/.caliber-folder"),
        "storage_backend": row.storage_backend if row is not None else None,
        "created_at": row.created_at.isoformat() if row is not None and row.created_at else None,
    }
    return payload


def _visible_project_rows(session: Session, project_id: str) -> list[CaliberWorkflowFile]:
    return list(
        session.execute(
            select(CaliberWorkflowFile)
            .where(
                CaliberWorkflowFile.project_id == project_id,
                CaliberWorkflowFile.deleted_at.is_(None),
            )
            .order_by(CaliberWorkflowFile.created_at.asc())
        )
        .scalars()
        .all()
    )


def _project_file_count(session: Session, project_id: str) -> int:
    return sum(
        1
        for row in _visible_project_rows(session, project_id)
        if row.status in VISIBLE_STATUSES and not _is_directory_marker(row)
    )


def _project_file_counts(session: Session, project_ids: list[str]) -> dict[str, int]:
    """Visible (non-directory-marker) file counts for many projects in one query.

    Replaces the per-project N+1 that calling :func:`_project_file_count` in a
    loop would incur when listing projects. The directory-marker flag lives in
    the ``file_metadata`` JSON (not portably queryable across SQLite/Postgres),
    so that final exclusion still happens in Python — but over a single result
    set rather than one round-trip per project.
    """
    counts: dict[str, int] = dict.fromkeys(project_ids, 0)
    if not project_ids:
        return counts
    stmt = select(
        CaliberWorkflowFile.project_id,
        CaliberWorkflowFile.file_metadata,
    ).where(
        CaliberWorkflowFile.project_id.in_(project_ids),
        CaliberWorkflowFile.deleted_at.is_(None),
        CaliberWorkflowFile.status.in_(tuple(VISIBLE_STATUSES)),
    )
    for project_id, metadata in session.execute(stmt):
        meta = metadata if isinstance(metadata, dict) else {}
        if meta.get("directory_marker"):
            continue
        counts[project_id] = counts.get(project_id, 0) + 1
    return counts


def _folders_from_rows(project_id: str, rows: list[CaliberWorkflowFile]) -> list[dict[str, Any]]:
    folders: dict[str, dict[str, Any]] = {}

    def add_path(path: str, row: CaliberWorkflowFile | None = None) -> None:
        parts = PurePosixPath(path).parts
        for idx in range(1, len(parts) + 1):
            folder_path = "/".join(parts[:idx])
            folders.setdefault(folder_path, _folder_payload(project_id, folder_path, row=row))

    for row in rows:
        if row.status not in VISIBLE_STATUSES:
            continue
        marker_path = _folder_path_from_marker(row) if _is_directory_marker(row) else None
        if marker_path:
            add_path(marker_path, row=row)
        if not _is_directory_marker(row):
            file_parent = "/".join(PurePosixPath(row.relative_path).parts[:-1])
            if file_parent:
                add_path(file_parent)
    return [folders[key] for key in sorted(folders)]


def _require_project(session: Session, project_id: str) -> CaliberProject:
    project = session.get(CaliberProject, project_id)
    if project is None:
        raise HTTPException(status_code=404, detail=f"project {project_id!r} not found")
    return project


def _file_for_project_or_404(
    session: Session, project_id: str, file_id: str
) -> CaliberWorkflowFile:
    row = session.get(CaliberWorkflowFile, file_id)
    if row is None or row.project_id != project_id or row.deleted_at is not None:
        raise HTTPException(
            status_code=404, detail=f"file {file_id!r} not found in project {project_id!r}"
        )
    return row


async def list_projects(request: Request) -> JSONResponse:
    require_user(request)
    identity = resolve_identity(request)
    status = request.query_params.get("status")
    if status is not None and status not in _VALID_STATUSES and status != "all":
        raise HTTPException(status_code=400, detail=f"invalid status {status!r}")
    factory = get_session_factory(request)
    with factory() as session:
        stmt = select(CaliberProject)
        if status and status != "all":
            stmt = stmt.where(CaliberProject.status == status)
        elif not status:
            stmt = stmt.where(CaliberProject.status == "active")
        # Projects are owner-scoped (no visibility tiers); admins see all.
        if not identity.has_scope(SCOPE_ADMIN):
            stmt = stmt.where(CaliberProject.owner == identity.user_id)
        rows = session.execute(stmt.order_by(CaliberProject.created_at.desc())).scalars().all()
        # file counts per project (visible files only) — a single grouped query
        # rather than one per project (avoids an N+1 as the project list grows).
        counts = _project_file_counts(session, [row.project_id for row in rows])
        items = [_project_to_dict(row, file_count=counts.get(row.project_id, 0)) for row in rows]
    return envelope_response_dict(items)


async def get_project_storage(request: Request) -> JSONResponse:
    require_user(request)
    workflow_storage = _workflow_storage_config(request)
    backend = str(getattr(workflow_storage, "backend", "local"))
    payload: dict[str, Any] = {
        "backend": backend,
        "backend_label": _backend_label(backend),
        "available_backends": _available_backend_options(request),
    }
    payload["base_uri"] = getattr(workflow_storage, "base_uri", "file://./caliber-workspaces")
    if backend == "s3" or getattr(workflow_storage, "bucket", None):
        payload["bucket"] = getattr(workflow_storage, "bucket", None)
        payload["prefix"] = getattr(workflow_storage, "prefix", "")
        payload["public_endpoint_url"] = getattr(workflow_storage, "public_endpoint_url", None)
    return envelope_response_dict(payload)


async def create_project(request: Request) -> JSONResponse:
    body = await parse_json_object(request)
    actor = require_scopes(request, [SCOPE_OPERATOR])
    name = body.get("name")
    if not isinstance(name, str) or not name.strip():
        raise HTTPException(status_code=400, detail="'name' is required")
    description = body.get("description")
    requested_backend = _normalize_requested_backend(body.get("storage_backend"))
    active_backend = _active_storage_backend(request)
    storage_backend = requested_backend or active_backend
    _require_configured_backend(request, storage_backend)
    factory = get_session_factory(request)
    with factory() as session:
        project = CaliberProject(
            project_id=new_project_id(),
            name=name.strip(),
            description=description.strip() if isinstance(description, str) else "",
            owner=actor,
            storage_backend=storage_backend,
        )
        session.add(project)
        try:
            session.flush()
        except IntegrityError as exc:
            raise HTTPException(
                status_code=409, detail=f"a project named {name.strip()!r} already exists"
            ) from exc
        audit_record(
            session,
            actor=actor,
            action="create_project",
            entity_type="project",
            entity_id=project.project_id,
            details={"name": project.name, "storage_backend": storage_backend},
        )
        session.commit()
        payload = _project_to_dict(project, file_count=0)
    return envelope_response_dict(payload, status_code=201)


async def get_project(request: Request) -> JSONResponse:
    require_user(request)
    project_id = request.path_params["project_id"]
    factory = get_session_factory(request)
    with factory() as session:
        project = _require_project(session, project_id)
        count = _project_file_count(session, project_id)
        payload = _project_to_dict(
            project,
            file_count=count,
        )
    return envelope_response_dict(payload)


async def update_project(request: Request) -> JSONResponse:
    project_id = request.path_params["project_id"]
    body = await parse_json_object(request)
    actor = require_scopes(request, [SCOPE_OPERATOR])
    factory = get_session_factory(request)
    with factory() as session:
        project = _require_project(session, project_id)
        if isinstance(body.get("name"), str) and body["name"].strip():
            project.name = body["name"].strip()
        if isinstance(body.get("description"), str):
            project.description = body["description"].strip()
        if isinstance(body.get("status"), str):
            if body["status"] not in _VALID_STATUSES:
                raise HTTPException(status_code=400, detail=f"invalid status {body['status']!r}")
            project.status = body["status"]
        try:
            session.flush()
        except IntegrityError as exc:
            raise HTTPException(status_code=409, detail="project name already in use") from exc
        audit_record(
            session,
            actor=actor,
            action="update_project",
            entity_type="project",
            entity_id=project_id,
            details={"status": project.status},
        )
        session.commit()
        payload = _project_to_dict(project)
    return envelope_response_dict(payload)


async def list_project_files(request: Request) -> JSONResponse:
    require_user(request)
    project_id = request.path_params["project_id"]
    factory = get_session_factory(request)
    with factory() as session:
        _require_project(session, project_id)
        rows = _visible_project_rows(session, project_id)
        items = [
            CaliberFileRecord.from_row(r).to_api()
            for r in rows
            if r.status in VISIBLE_STATUSES and not _is_directory_marker(r)
        ]
        directories = _folders_from_rows(project_id, rows)
    return envelope_response_dict({"items": items, "directories": directories, "next_cursor": None})


async def create_project_folder(request: Request) -> JSONResponse:
    project_id = request.path_params["project_id"]
    body = await parse_json_object(request)
    actor = require_scopes(request, [SCOPE_OPERATOR])
    raw_path = body.get("path")
    if not isinstance(raw_path, str) or not raw_path.strip():
        raise HTTPException(status_code=400, detail="'path' is required")
    factory = get_session_factory(request)
    try:
        with factory() as session:
            project = _require_project(session, project_id)
            service = _project_storage_service(request, project)
            rec = service.create_project_folder(
                session,
                project_id=project_id,
                path=raw_path,
                actor=actor,
            )
            row = session.get(CaliberWorkflowFile, rec.file_id)
            folder_path = _folder_path_from_marker(row) if row is not None else raw_path
            audit_record(
                session,
                actor=actor,
                action="create_folder",
                entity_type="workflow_file",
                entity_id=rec.file_id,
                details={"project_id": project_id, "path": folder_path},
            )
            session.commit()
            payload = _folder_payload(project_id, folder_path or raw_path, row=row)
    except StorageError as exc:
        raise _storage_http(exc) from exc
    return envelope_response_dict(payload, status_code=201)


async def upload_project_file(request: Request) -> JSONResponse:
    project_id = request.path_params["project_id"]
    actor = require_scopes(request, [SCOPE_OPERATOR])
    data, filename, kind, media_type, _metadata = await _read_upload(request)
    factory = get_session_factory(request)
    try:
        with factory() as session:
            project = _require_project(session, project_id)
            service = _project_storage_service(request, project)
            rec = service.register_project_file(
                session,
                project_id=project_id,
                kind=kind,
                filename=filename,
                data=data,
                media_type=media_type,
                actor=actor,
                metadata=_metadata,
            )
            audit_record(
                session,
                actor=actor,
                action="upload_file",
                entity_type="workflow_file",
                entity_id=rec.file_id,
                details={"project_id": project_id, "name": rec.name, "kind": kind},
            )
            session.commit()
            payload = rec.to_api()
            payload["project_id"] = project_id
    except StorageError as exc:
        raise _storage_http(exc) from exc
    return envelope_response_dict(payload, status_code=201)


async def download_project_file(request: Request) -> Response:
    project_id = request.path_params["project_id"]
    file_id = request.path_params["file_id"]
    actor = require_user(request)
    service = get_working_dir_service(request)
    factory = get_session_factory(request)
    try:
        with factory() as session:
            row = _file_for_project_or_404(session, project_id, file_id)
            content = service.read_bytes(row)
            service.record_event(
                session,
                action="download_file",
                actor=actor,
                file_id=row.file_id,
                relative_path=row.relative_path,
                status="ok",
            )
            session.commit()
            name = row.name
    except StorageError as exc:
        raise _storage_http(exc) from exc
    return Response(
        content=content,
        media_type="application/octet-stream",
        headers={
            "Content-Disposition": f'attachment; filename="{name}"',
            "X-Content-Type-Options": "nosniff",
        },
    )


async def delete_project_file(request: Request) -> JSONResponse:
    project_id = request.path_params["project_id"]
    file_id = request.path_params["file_id"]
    actor = require_scopes(request, [SCOPE_OPERATOR])
    service = get_working_dir_service(request)
    factory = get_session_factory(request)
    with factory() as session:
        row = _file_for_project_or_404(session, project_id, file_id)
        # Soft-delete in metadata (storage doc §2.6); the retention janitor
        # reclaims the physical object later.
        row.status = "deleted"
        row.deleted_at = datetime.now(timezone.utc)
        service.record_event(
            session,
            action="delete_file",
            actor=actor,
            file_id=file_id,
            relative_path=row.relative_path,
            status="ok",
        )
        session.commit()
    return envelope_response_dict({"file_id": file_id, "status": "deleted"})


def register(app: Starlette) -> None:
    app.routes.append(Route(LIST_PATH, list_projects, methods=["GET"]))
    app.routes.append(Route(LIST_PATH, create_project, methods=["POST"]))
    app.routes.append(Route(STORAGE_PATH, get_project_storage, methods=["GET"]))
    app.routes.append(Route(FOLDERS_PATH, create_project_folder, methods=["POST"]))
    app.routes.append(Route(FILES_PATH, upload_project_file, methods=["POST"]))
    app.routes.append(Route(FILES_PATH, list_project_files, methods=["GET"]))
    app.routes.append(Route(FILE_CONTENT_PATH, download_project_file, methods=["GET"]))
    app.routes.append(Route(FILE_PATH, delete_project_file, methods=["DELETE"]))
    app.routes.append(Route(DETAIL_PATH, get_project, methods=["GET"]))
    app.routes.append(Route(DETAIL_PATH, update_project, methods=["PATCH"]))
