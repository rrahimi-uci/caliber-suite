"""File/workspace storage routes (storage doc §4.7).

Endpoints (all under ``/ajax-api/2.0/mlflow/caliber/``):

* ``POST /workflow-files`` — staging upload before a run exists (operator).
* ``POST /workflow-runs/{run_id}/files`` — run-scoped multipart upload (operator).
* ``GET  /workflow-runs/{run_id}/files`` — list run files (viewer).
* ``GET  /workflow-runs/{run_id}/files/{file_id}`` — file metadata (viewer).
* ``GET  /workflow-runs/{run_id}/files/{file_id}/content`` — download proxy (viewer).
* ``POST /workflow-runs/{run_id}/artifacts`` — register an existing file as an
  artifact (operator).

Every handler authenticates and scopes (storage doc §4.7 preamble / §8.1). Reads
require an authenticated user; mutations require ``SCOPE_OPERATOR``. The download
proxy resolves the object only from the DB row's ``object_key`` and serves it as an
attachment with ``X-Content-Type-Options: nosniff`` (SSRF / content-confusion
defense, storage doc §4.7). A file id from another run cannot be fetched through a
different run's path (no IDOR).
"""

from __future__ import annotations

import json
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session
from starlette.applications import Starlette
from starlette.exceptions import HTTPException
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

from caliber.audit import record as audit_record
from caliber.auth import SCOPE_OPERATOR, require_scopes, require_user
from caliber.db.models import CaliberWorkflowFile, CaliberWorkflowRun
from caliber.routes._deps import (
    envelope_response_dict,
    get_session_factory,
    get_working_dir_service,
    parse_json_object,
)
from caliber.storage import (
    FILE_KINDS,
    VISIBLE_STATUSES,
    CaliberFileRecord,
    StorageError,
    StorageNotFoundError,
    StoragePermissionError,
    StorageValidationError,
)

PREFIX = "/ajax-api/2.0/mlflow/caliber"
STAGING_PATH = PREFIX + "/workflow-files"
RUN_FILES_PATH = PREFIX + "/workflow-runs/{run_id}/files"
RUN_FILE_PATH = PREFIX + "/workflow-runs/{run_id}/files/{file_id}"
RUN_FILE_CONTENT_PATH = PREFIX + "/workflow-runs/{run_id}/files/{file_id}/content"
RUN_ARTIFACTS_PATH = PREFIX + "/workflow-runs/{run_id}/artifacts"
# Playground runs (storage doc §6). A playground run id is lightweight — any
# authenticated operator may open one; the namespace is created on first upload.
PG_FILES_PATH = PREFIX + "/playground-runs/{run_id}/files"
PG_FILE_PATH = PREFIX + "/playground-runs/{run_id}/files/{file_id}"
PG_FILE_CONTENT_PATH = PREFIX + "/playground-runs/{run_id}/files/{file_id}/content"

# Kinds a client may upload before a run starts / via the run upload route.
_UPLOADABLE_KINDS = frozenset({"input", "work", "artifact", "log"})

_HTTP_FOR_STORAGE: dict[type[StorageError], int] = {
    StorageNotFoundError: 404,
    StoragePermissionError: 403,
    StorageValidationError: 400,
}


def _storage_http(exc: StorageError) -> HTTPException:
    status = 500
    for cls, code in _HTTP_FOR_STORAGE.items():
        if isinstance(exc, cls):
            status = code
            break
    return HTTPException(status_code=status, detail=str(exc))


def _require_run(session: Session, run_id: str) -> CaliberWorkflowRun:
    run = session.get(CaliberWorkflowRun, run_id)
    if run is None:
        raise HTTPException(status_code=404, detail=f"workflow run {run_id!r} not found")
    return run


def _file_for_run_or_404(session: Session, run_id: str, file_id: str) -> CaliberWorkflowFile:
    row = session.get(CaliberWorkflowFile, file_id)
    # IDOR guard: the row must belong to this run (storage doc §8.1).
    if row is None or row.workflow_run_id != run_id or row.deleted_at is not None:
        raise HTTPException(status_code=404, detail=f"file {file_id!r} not found in run {run_id!r}")
    return row


async def _read_upload(request: Request) -> tuple[bytes, str, str, str | None, dict[str, Any]]:
    """Parse a multipart upload form. Returns (data, filename, kind, media_type, metadata).

    Always closes the uploaded file's temp handle (Starlette backs uploads with a
    ``SpooledTemporaryFile``); leaving it open trips a ResourceWarning at GC.
    """
    form = await request.form()
    upload = form.get("file")
    try:
        if upload is None or not hasattr(upload, "read"):
            raise HTTPException(status_code=400, detail="multipart field 'file' is required")
        data = await upload.read()
        filename = getattr(upload, "filename", None) or form.get("path") or "upload.bin"
        kind = str(form.get("kind") or "input")
        if kind not in _UPLOADABLE_KINDS:
            raise HTTPException(
                status_code=400,
                detail=f"invalid kind {kind!r}; expected one of {sorted(_UPLOADABLE_KINDS)}",
            )
        media_type = getattr(upload, "content_type", None) or form.get("media_type")
        raw_meta = form.get("metadata")
        metadata: dict[str, Any] = {}
        if isinstance(raw_meta, str) and raw_meta.strip():
            try:
                parsed = json.loads(raw_meta)
            except json.JSONDecodeError as exc:
                raise HTTPException(
                    status_code=400, detail=f"invalid metadata JSON: {exc}"
                ) from exc
            if isinstance(parsed, dict):
                metadata = parsed
        path_override = form.get("path")
        if isinstance(path_override, str) and path_override.strip():
            filename = path_override
        media_str = media_type if isinstance(media_type, str) else None
        return data, str(filename), kind, media_str, metadata
    finally:
        close = getattr(upload, "close", None)
        if close is not None:
            await close()


async def staging_upload(request: Request) -> JSONResponse:
    """Upload a file before a run exists (workflow_run_id NULL, storage doc §4.7)."""
    actor = require_scopes(request, [SCOPE_OPERATOR])
    data, filename, kind, media_type, metadata = await _read_upload(request)
    session_id = request.query_params.get("session_id")
    service = get_working_dir_service(request)
    factory = get_session_factory(request)
    try:
        with factory() as session:
            # Staging namespace keyed by session (or a generated one).
            staging_run = f"staging-{session_id}" if session_id else "staging"
            ctx = service.create_run_workspace(workflow_id="_staging", workflow_run_id=staging_run)
            rec = service.register_upload(
                session,
                ctx,
                kind=kind,  # type: ignore[arg-type]
                filename=filename,
                data=data,
                media_type=media_type,
                actor=actor,
                metadata={**metadata, "session_id": session_id} if session_id else metadata,
            )
            # Staging rows are not bound to a run yet.
            row = session.get(CaliberWorkflowFile, rec.file_id)
            if row is not None:
                row.workflow_run_id = None
                row.session_id = session_id
            audit_record(
                session,
                actor=actor,
                action="upload_file",
                entity_type="workflow_file",
                entity_id=rec.file_id,
                details={"staging": True, "name": rec.name, "kind": kind},
            )
            session.commit()
            payload = rec.to_api()
            payload["workflow_run_id"] = None
    except StorageError as exc:
        raise _storage_http(exc) from exc
    return envelope_response_dict(payload, status_code=201)


async def run_upload(request: Request) -> JSONResponse:
    """Upload a file into a workflow run namespace (storage doc §4.7)."""
    run_id = request.path_params["run_id"]
    actor = require_scopes(request, [SCOPE_OPERATOR])
    data, filename, kind, media_type, metadata = await _read_upload(request)
    service = get_working_dir_service(request)
    factory = get_session_factory(request)
    try:
        with factory() as session:
            run = _require_run(session, run_id)
            ctx = service.create_run_workspace(workflow_id=run.workflow_id, workflow_run_id=run_id)
            rec = service.register_upload(
                session,
                ctx,
                kind=kind,  # type: ignore[arg-type]
                filename=filename,
                data=data,
                media_type=media_type,
                actor=actor,
                status="attached" if kind == "input" else "uploaded",
                metadata=metadata,
            )
            audit_record(
                session,
                actor=actor,
                action="upload_file",
                entity_type="workflow_file",
                entity_id=rec.file_id,
                details={"workflow_run_id": run_id, "name": rec.name, "kind": kind},
            )
            session.commit()
            payload = rec.to_api()
            payload["workflow_run_id"] = run_id
    except StorageError as exc:
        raise _storage_http(exc) from exc
    return envelope_response_dict(payload, status_code=201)


async def list_files(request: Request) -> JSONResponse:
    """List files for a run, hiding pending/rejected/deleted rows (storage doc §4.7)."""
    run_id = request.path_params["run_id"]
    require_user(request)
    kind = request.query_params.get("kind")
    if kind and kind not in FILE_KINDS:
        raise HTTPException(
            status_code=400,
            detail=f"invalid kind {kind!r}; expected one of {sorted(FILE_KINDS)}",
        )
    factory = get_session_factory(request)
    with factory() as session:
        _require_run(session, run_id)
        stmt = select(CaliberWorkflowFile).where(
            CaliberWorkflowFile.workflow_run_id == run_id,
            CaliberWorkflowFile.deleted_at.is_(None),
        )
        if kind:
            stmt = stmt.where(CaliberWorkflowFile.kind == kind)
        stmt = stmt.order_by(CaliberWorkflowFile.created_at.asc())
        rows = session.execute(stmt).scalars().all()

        items = [
            CaliberFileRecord.from_row(r).to_api() for r in rows if r.status in VISIBLE_STATUSES
        ]
    return envelope_response_dict({"items": items, "next_cursor": None})


async def get_file(request: Request) -> JSONResponse:
    run_id = request.path_params["run_id"]
    file_id = request.path_params["file_id"]
    require_user(request)
    factory = get_session_factory(request)
    with factory() as session:
        _require_run(session, run_id)
        row = _file_for_run_or_404(session, run_id, file_id)

        payload = CaliberFileRecord.from_row(row).to_api()
        payload["workflow_run_id"] = run_id
    return envelope_response_dict(payload)


async def get_file_content(request: Request) -> Response:
    """Download proxy: resolve object from the DB row's key only (storage doc §4.7).

    Serves as an attachment with ``nosniff`` so untrusted HTML/SVG never renders
    inline. The request never carries a storage URI (SSRF defense).
    """
    run_id = request.path_params["run_id"]
    file_id = request.path_params["file_id"]
    actor = require_user(request)
    service = get_working_dir_service(request)
    factory = get_session_factory(request)
    try:
        with factory() as session:
            _require_run(session, run_id)
            row = _file_for_run_or_404(session, run_id, file_id)
            data = service.read_bytes(row)
            service.record_event(
                session,
                action="download_file",
                actor=actor,
                file_id=row.file_id,
                run_id=run_id,
                relative_path=row.relative_path,
                status="ok",
            )
            session.commit()
            name = row.name
    except StorageError as exc:
        raise _storage_http(exc) from exc
    return Response(
        content=data,
        media_type="application/octet-stream",
        headers={
            "Content-Disposition": f'attachment; filename="{name}"',
            "X-Content-Type-Options": "nosniff",
        },
    )


async def register_artifact(request: Request) -> JSONResponse:
    """Mark an existing run file as a registered artifact (storage doc §4.7)."""
    run_id = request.path_params["run_id"]
    actor = require_scopes(request, [SCOPE_OPERATOR])
    body = await parse_json_object(request)
    file_id = body.get("file_id")
    if not isinstance(file_id, str):
        raise HTTPException(status_code=400, detail="'file_id' is required")
    factory = get_session_factory(request)
    service = get_working_dir_service(request)
    with factory() as session:
        _require_run(session, run_id)
        row = _file_for_run_or_404(session, run_id, file_id)
        row.status = "artifact"
        row.kind = "artifact"
        meta = dict(row.file_metadata or {})
        for key in ("artifact_type", "display_name", "summary"):
            if key in body:
                meta[key] = body[key]
        if isinstance(body.get("producer_node_id"), str):
            row.producer_node_id = body["producer_node_id"]
        row.file_metadata = meta
        service.record_event(
            session,
            action="register_artifact",
            actor=actor,
            file_id=row.file_id,
            run_id=run_id,
            relative_path=row.relative_path,
            status="ok",
        )
        audit_record(
            session,
            actor=actor,
            action="register_artifact",
            entity_type="workflow_file",
            entity_id=row.file_id,
            details={"workflow_run_id": run_id, "name": row.name},
        )
        session.commit()

        payload = CaliberFileRecord.from_row(row).to_api()
        payload["workflow_run_id"] = run_id
    return envelope_response_dict(payload, status_code=201)


def _pg_file_or_404(session: Session, run_id: str, file_id: str) -> CaliberWorkflowFile:
    row = session.get(CaliberWorkflowFile, file_id)
    if row is None or row.playground_run_id != run_id or row.deleted_at is not None:
        raise HTTPException(
            status_code=404, detail=f"file {file_id!r} not found in playground run {run_id!r}"
        )
    return row


async def pg_upload(request: Request) -> JSONResponse:
    """Upload a file into a playground run namespace (storage doc §6.2)."""
    run_id = request.path_params["run_id"]
    actor = require_scopes(request, [SCOPE_OPERATOR])
    data, filename, kind, media_type, metadata = await _read_upload(request)
    service = get_working_dir_service(request)
    factory = get_session_factory(request)
    try:
        with factory() as session:
            ctx = service.create_playground_workspace(playground_run_id=run_id)
            rec = service.register_upload(
                session,
                ctx,
                kind=kind,  # type: ignore[arg-type]
                filename=filename,
                data=data,
                media_type=media_type,
                actor=actor,
                status="attached" if kind == "input" else "uploaded",
                metadata=metadata,
            )
            audit_record(
                session,
                actor=actor,
                action="upload_file",
                entity_type="workflow_file",
                entity_id=rec.file_id,
                details={"playground_run_id": run_id, "name": rec.name, "kind": kind},
            )
            session.commit()
            payload = rec.to_api()
            payload["playground_run_id"] = run_id
    except StorageError as exc:
        raise _storage_http(exc) from exc
    return envelope_response_dict(payload, status_code=201)


async def pg_list_files(request: Request) -> JSONResponse:
    run_id = request.path_params["run_id"]
    require_user(request)
    kind = request.query_params.get("kind")
    if kind and kind not in FILE_KINDS:
        raise HTTPException(status_code=400, detail=f"invalid kind {kind!r}")
    factory = get_session_factory(request)
    with factory() as session:
        stmt = select(CaliberWorkflowFile).where(
            CaliberWorkflowFile.playground_run_id == run_id,
            CaliberWorkflowFile.deleted_at.is_(None),
        )
        if kind:
            stmt = stmt.where(CaliberWorkflowFile.kind == kind)
        rows = session.execute(stmt.order_by(CaliberWorkflowFile.created_at.asc())).scalars().all()
        items = [
            CaliberFileRecord.from_row(r).to_api() for r in rows if r.status in VISIBLE_STATUSES
        ]
    return envelope_response_dict({"items": items, "next_cursor": None})


async def pg_get_file_content(request: Request) -> Response:
    run_id = request.path_params["run_id"]
    file_id = request.path_params["file_id"]
    actor = require_user(request)
    service = get_working_dir_service(request)
    factory = get_session_factory(request)
    try:
        with factory() as session:
            row = _pg_file_or_404(session, run_id, file_id)
            data = service.read_bytes(row)
            service.record_event(
                session,
                action="download_file",
                actor=actor,
                file_id=row.file_id,
                run_id=run_id,
                relative_path=row.relative_path,
                status="ok",
            )
            session.commit()
            name = row.name
    except StorageError as exc:
        raise _storage_http(exc) from exc
    return Response(
        content=data,
        media_type="application/octet-stream",
        headers={
            "Content-Disposition": f'attachment; filename="{name}"',
            "X-Content-Type-Options": "nosniff",
        },
    )


def register(app: Starlette) -> None:
    # Literal paths before path-param paths (storage doc §4.7 / route convention).
    app.routes.append(Route(STAGING_PATH, staging_upload, methods=["POST"]))
    app.routes.append(Route(RUN_FILES_PATH, run_upload, methods=["POST"]))
    app.routes.append(Route(RUN_FILES_PATH, list_files, methods=["GET"]))
    app.routes.append(Route(RUN_ARTIFACTS_PATH, register_artifact, methods=["POST"]))
    app.routes.append(Route(RUN_FILE_CONTENT_PATH, get_file_content, methods=["GET"]))
    app.routes.append(Route(RUN_FILE_PATH, get_file, methods=["GET"]))
    # Playground run file routes (storage doc §6).
    app.routes.append(Route(PG_FILES_PATH, pg_upload, methods=["POST"]))
    app.routes.append(Route(PG_FILES_PATH, pg_list_files, methods=["GET"]))
    app.routes.append(Route(PG_FILE_CONTENT_PATH, pg_get_file_content, methods=["GET"]))
