"""``/caliber/projects`` — workspaces that group uploaded files.

A project is a lightweight workspace (storage doc §2.1: ``project/{project_id}``).
It exposes CRUD plus a files sub-resource so users can create a project and
upload/browse/download files in it from the UI, independent of any workflow run.
File operations reuse the storage service + the multipart/error helpers from
:mod:`caliber.routes.files`.
"""

from __future__ import annotations

import importlib.util
import re
from datetime import datetime, timezone
from pathlib import PurePosixPath
from typing import Any

from sqlalchemy import and_, or_, select
from sqlalchemy import update as sa_update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session
from starlette.applications import Starlette
from starlette.exceptions import HTTPException
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

from caliber.audit import record as audit_record
from caliber.auth import (
    SCOPE_APPROVER,
    SCOPE_OPERATOR,
    CaliberIdentity,
    require_all_scopes,
    require_scopes,
    require_user,
    resolve_identity,
)
from caliber.db.models import (
    CaliberProject,
    CaliberProjectMember,
    CaliberWorkflowFile,
    CaliberWorkspaceEnvironment,
)
from caliber.deployment_environments import (
    WORKSPACE_ENVIRONMENT_DEFAULT_STATUS,
    WORKSPACE_ENVIRONMENT_NAMES,
    WORKSPACE_ENVIRONMENT_PROMOTION_ORDER,
    workspace_environment_class,
)
from caliber.ids import new_project_id, new_project_member_id, new_workspace_environment_id
from caliber.resource_access import (
    PROJECT_ROLES,
    ROLE_OWNER,
    is_eligible_for_owner_role,
    member_payload,
    permissions_for_role,
    require_project_access,
)
from caliber.routes._deps import (
    envelope_response,
    get_session_factory,
    get_working_dir_service,
    parse_json_object,
)
from caliber.routes.files import _read_upload, _storage_http
from caliber.schemas import (
    DeletedSchema,
    ProjectFileListSchema,
    ProjectFileSchema,
    ProjectFolderSchema,
    ProjectMemberCreateRequest,
    ProjectMemberListSchema,
    ProjectMemberSchema,
    ProjectMemberUpdateRequest,
    ProjectSchema,
    ProjectStorageSchema,
    ProjectTransferOwnershipRequest,
)
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


def _project_to_schema(
    row: CaliberProject,
    *,
    file_count: int | None = None,
    storage_backend: str | None = None,
    access_role: str | None = None,
    permissions: frozenset[str] | set[str] | None = None,
) -> ProjectSchema:
    """The declared shape of a project response.

    ``file_count`` stays optional rather than defaulting to 0: the list route
    computes it with one grouped query and the detail route does not, and a
    zero would be indistinguishable from "this project has no files".
    """
    return ProjectSchema(
        project_id=row.project_id,
        name=row.name,
        description=row.description,
        owner=row.owner,
        status=row.status,
        storage_backend=storage_backend or row.storage_backend,
        created_at=row.created_at.isoformat() if row.created_at else None,
        updated_at=row.updated_at.isoformat() if row.updated_at else None,
        file_count=file_count,
        access_role=access_role,
        permissions=sorted(permissions or ()),
        archived_at=row.archived_at.isoformat() if row.archived_at else None,
        archived_by=row.archived_by,
    )


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


def _require_project(
    session: Session, project_id: str, *, identity: CaliberIdentity
) -> CaliberProject:
    """Resolve a project, applying the **same owner rule its list sibling applies**.

    Closes the C3 instance the review named first: "project lists are owner-filtered,
    but ``_require_project()`` is a bare ``session.get``", which detail, update, list
    files, create folder, and upload all funnelled through -- so a known project id read
    and mutated another owner's project even though listing hid it.

    Projects are visible to their owner and active members only -- `P1-B`
    removed the platform-admin owner bypass (`caliber.admin` is no longer
    an implicit workspace role). The same helper
    is used by list, detail, file, and membership routes so a detail rule cannot drift
    from its list rule.

    A project owned by someone else 404s rather than 403s: "exists but forbidden"
    confirms the id is real.
    """
    project, _decision = require_project_access(session, identity, project_id, "read")
    return project


def _require_project_action(
    session: Session,
    project_id: str,
    *,
    identity: CaliberIdentity,
    action: str,
) -> CaliberProject:
    project, _decision = require_project_access(session, identity, project_id, action)
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
        # Owners and active members can see a project. `caliber.admin` is
        # deliberately *not* an implicit workspace role (`P1-B`, section
        # 5.4) -- this filter now applies unconditionally, admin included.
        # It must stay coupled to `resource_access.py::project_role()`'s own
        # admin-bypass removal: the per-row `require_project_access` call
        # below has no try/except, so every row this query returns must
        # already be one the caller has genuine access to, for every caller.
        stmt = (
            stmt.outerjoin(
                CaliberProjectMember,
                and_(
                    CaliberProjectMember.project_id == CaliberProject.project_id,
                    CaliberProjectMember.user_id == identity.user_id,
                    CaliberProjectMember.status == "active",
                ),
            )
            .where(
                or_(
                    CaliberProject.owner == identity.user_id,
                    CaliberProjectMember.member_id.is_not(None),
                )
            )
            .distinct()
        )
        rows = session.execute(stmt.order_by(CaliberProject.created_at.desc())).scalars().all()
        # file counts per project (visible files only) — a single grouped query
        # rather than one per project (avoids an N+1 as the project list grows).
        counts = _project_file_counts(session, [row.project_id for row in rows])
        items = []
        for row in rows:
            _project, decision = require_project_access(session, identity, row.project_id)
            items.append(
                _project_to_schema(
                    row,
                    file_count=counts.get(row.project_id, 0),
                    access_role=decision.role,
                    permissions=decision.permissions,
                )
            )
    return envelope_response(items)


async def get_project_storage(request: Request) -> JSONResponse:
    require_user(request)
    workflow_storage = _workflow_storage_config(request)
    backend = str(getattr(workflow_storage, "backend", "local"))
    storage = ProjectStorageSchema(
        backend=backend,
        backend_label=_backend_label(backend),
        available_backends=_available_backend_options(request),
        base_uri=getattr(workflow_storage, "base_uri", "file://./caliber-workspaces"),
    )
    # Object-store fields are attached only for a backend that has them, so a
    # local-backend response does not advertise an empty bucket.
    if backend == "s3" or getattr(workflow_storage, "bucket", None):
        storage.bucket = getattr(workflow_storage, "bucket", None)
        storage.prefix = getattr(workflow_storage, "prefix", "")
        storage.public_endpoint_url = getattr(workflow_storage, "public_endpoint_url", None)
    return envelope_response(storage)


#: `caliber_projects.tenant_id`'s only value anywhere in the system today
#: (no multi-tenant support exists) -- named here rather than repeating the
#: literal, since it's about to gain a second use (slug-uniqueness scoping).
_DEFAULT_TENANT_ID = "local"

_SLUG_COLLAPSE = re.compile(r"[^a-z0-9]+")


def _slugify(name: str) -> str:
    """Lowercase, non-alphanumeric runs collapsed to a single ``-``, trimmed.

    Mirrors migration ``0093``'s backfill exactly (same regex, same
    fallback) -- a project created live and a pre-existing project the
    migration backfilled must derive the same slug from the same name.
    """
    slug = _SLUG_COLLAPSE.sub("-", name.strip().lower()).strip("-")
    return slug or "workspace"


def _derive_unique_slug(session: Session, tenant_id: str, name: str) -> str:
    """A slugified, tenant-unique handle for a new project.

    Same collision strategy as migration ``0093``'s backfill: append ``-2``,
    ``-3``, ... until free. A true concurrent race (two creates deriving the
    same slug at once) still falls through to the ``uq_project_tenant_slug``
    database constraint at flush time -- this pre-check only avoids the
    common case of a 409 for what the caller would see as two differently
    named projects (``"Demo"`` and ``"demo"``).
    """
    base_slug = _slugify(name)
    slug = base_slug
    suffix = 2
    while (
        session.execute(
            select(CaliberProject.project_id).where(
                CaliberProject.tenant_id == tenant_id, CaliberProject.slug == slug
            )
        ).first()
        is not None
    ):
        slug = f"{base_slug}-{suffix}"
        suffix += 1
    return slug


async def create_project(request: Request) -> JSONResponse:
    body = await parse_json_object(request)
    # `project.create` is the one action authorized before any Workspace
    # membership exists (section 2.4's pre-membership bootstrap check) --
    # both scopes are required, not either, so an Operator alone or an
    # Approver alone cannot unilaterally create a workspace.
    actor = require_all_scopes(request, [SCOPE_OPERATOR, SCOPE_APPROVER])
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
        slug = _derive_unique_slug(session, _DEFAULT_TENANT_ID, name.strip())
        project = CaliberProject(
            project_id=new_project_id(),
            tenant_id=_DEFAULT_TENANT_ID,
            name=name.strip(),
            description=description.strip() if isinstance(description, str) else "",
            owner=actor,
            storage_backend=storage_backend,
            slug=slug,
            source_mode="caliber_managed",
        )
        session.add(project)
        session.add(
            CaliberProjectMember(
                member_id=new_project_member_id(),
                project_id=project.project_id,
                user_id=actor,
                role=ROLE_OWNER,
                status="active",
                created_by=actor,
            )
        )
        # Seed the four fixed environment identities in the same transaction
        # as the project + owner membership -- a failed seed must leave no
        # partial workspace (section 12.2).
        for env_name in WORKSPACE_ENVIRONMENT_NAMES:
            session.add(
                CaliberWorkspaceEnvironment(
                    environment_id=new_workspace_environment_id(),
                    project_id=project.project_id,
                    name=env_name,
                    environment_class=workspace_environment_class(env_name),
                    promotion_order=WORKSPACE_ENVIRONMENT_PROMOTION_ORDER[env_name],
                    status=WORKSPACE_ENVIRONMENT_DEFAULT_STATUS[env_name],
                    created_by=actor,
                )
            )
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
            details={"name": project.name, "storage_backend": storage_backend, "slug": slug},
        )
        session.commit()
        payload = _project_to_schema(
            project,
            file_count=0,
            access_role=ROLE_OWNER,
            permissions=permissions_for_role(ROLE_OWNER),
        )
    return envelope_response(payload, status_code=201)


async def get_project(request: Request) -> JSONResponse:
    require_user(request)
    project_id = request.path_params["project_id"]
    factory = get_session_factory(request)
    with factory() as session:
        project, decision = require_project_access(
            session, resolve_identity(request), project_id, "read"
        )
        count = _project_file_count(session, project_id)
        payload = _project_to_schema(
            project,
            file_count=count,
            access_role=decision.role,
            permissions=decision.permissions,
        )
    return envelope_response(payload)


async def list_project_members(request: Request) -> JSONResponse:
    """List active project members and their effective roles."""
    identity = resolve_identity(request)
    project_id = request.path_params["project_id"]
    factory = get_session_factory(request)
    with factory() as session:
        require_project_access(session, identity, project_id, "read")
        rows = (
            session.execute(
                select(CaliberProjectMember)
                .where(
                    CaliberProjectMember.project_id == project_id,
                    CaliberProjectMember.status == "active",
                )
                .order_by(CaliberProjectMember.created_at.asc(), CaliberProjectMember.user_id.asc())
            )
            .scalars()
            .all()
        )
        payload = ProjectMemberListSchema(
            members=[ProjectMemberSchema.model_validate(member_payload(row)) for row in rows]
        )
    return envelope_response(payload)


def _require_owner_role_eligible(request: Request, user_id: str) -> None:
    """Raise `409` unless `user_id`'s live platform scopes qualify them for
    the `owner` (Admin) role.

    Shared by every call site that grants, reactivates into, or transfers
    primary ownership to that role (`add_project_member`,
    `update_project_member`, `transfer_project_ownership`) -- a review of
    the first version of this PR found the check duplicated three times
    with slightly different guard conditions, and one of those three copies
    (`update_project_member`'s) had a real gap (see that function's own
    comment). One shared call site can no longer drift out of sync with the
    other two the way that duplication did.
    """
    if not is_eligible_for_owner_role(getattr(request.app.state, "config", None), user_id):
        raise HTTPException(
            status_code=409,
            detail=(
                f"{user_id!r} lacks the caliber.operator + caliber.approver scopes "
                "required for the owner role"
            ),
        )


async def add_project_member(request: Request) -> JSONResponse:
    body = await parse_json_object(request)
    payload = ProjectMemberCreateRequest.model_validate(body)
    # `project.manage_members` requires `caliber.operator` (section 2.4) --
    # a project owner with only the universal `caliber.viewer` scope holds
    # the project-role permission but not the global-scope ceiling. Found
    # missing here during P1-B's GitHub Copilot review; the project-role
    # check alone (below) does not enforce this.
    require_scopes(request, [SCOPE_OPERATOR])
    identity = resolve_identity(request)
    project_id = request.path_params["project_id"]
    user_id = payload.user_id.strip()
    if not user_id:
        raise HTTPException(status_code=400, detail="'user_id' is required")
    if payload.role not in PROJECT_ROLES:
        raise HTTPException(
            status_code=400, detail="role must be owner, editor, reviewer, or viewer"
        )
    factory = get_session_factory(request)
    with factory() as session:
        require_project_access(session, identity, project_id, "project.manage_members")
        project = session.get(CaliberProject, project_id)
        if project is None:  # defensive; the access helper already checked it
            raise HTTPException(status_code=404, detail=f"project {project_id!r} not found")
        # `P1-C`: granting the `owner` (Admin) role -- multiple active
        # `owner`-role memberships are allowed alongside `project.owner`'s
        # one primary-owner pointer -- requires the target to already carry
        # both platform scopes section 2.4 names (`caliber.operator` +
        # `caliber.approver`). A scope-ineligible target is rejected, not
        # silently downgraded, so the caller sees exactly why. Checked
        # whenever the request would create/reactivate a member *with* the
        # `owner` role -- `payload.role` is required on this request (unlike
        # `update_project_member`'s optional one), so this single check also
        # covers reactivating a previously-removed `owner`-role member.
        if payload.role == ROLE_OWNER:
            _require_owner_role_eligible(request, user_id)
        existing = session.execute(
            select(CaliberProjectMember).where(
                CaliberProjectMember.project_id == project_id,
                CaliberProjectMember.user_id == user_id,
            )
        ).scalar_one_or_none()
        if existing is not None:
            if existing.status != "active":
                existing.status = "active"
                existing.role = payload.role
                existing.created_by = identity.user_id
                # `P1-C`: reactivating a member clears its prior
                # deactivation provenance -- those columns describe the
                # *current* inactive state, not a permanent history (the
                # audit log already carries that).
                existing.deactivated_at = None
                existing.deactivated_by = None
                member = existing
            else:
                raise HTTPException(status_code=409, detail="user is already a project member")
        else:
            member = CaliberProjectMember(
                member_id=new_project_member_id(),
                project_id=project_id,
                user_id=user_id,
                role=payload.role,
                status="active",
                created_by=identity.user_id,
            )
            session.add(member)
        audit_record(
            session,
            actor=identity.user_id,
            action="add_project_member",
            entity_type="project",
            entity_id=project_id,
            details={"user_id": user_id, "role": payload.role},
        )
        session.commit()
        data = ProjectMemberSchema.model_validate(member_payload(member))
    return envelope_response(data, status_code=201)


async def update_project_member(request: Request) -> JSONResponse:
    body = await parse_json_object(request)
    payload = ProjectMemberUpdateRequest.model_validate(body)
    # See `add_project_member`: `project.manage_members` requires
    # `caliber.operator`, not just the project-role permission.
    require_scopes(request, [SCOPE_OPERATOR])
    identity = resolve_identity(request)
    project_id = request.path_params["project_id"]
    user_id = request.path_params["user_id"]
    if payload.role is not None and payload.role not in PROJECT_ROLES:
        raise HTTPException(
            status_code=400, detail="role must be owner, editor, reviewer, or viewer"
        )
    if payload.status is not None and payload.status not in {"active", "inactive"}:
        raise HTTPException(status_code=400, detail="status must be active or inactive")
    factory = get_session_factory(request)
    with factory() as session:
        project, _decision = require_project_access(
            session, identity, project_id, "project.manage_members"
        )
        member = session.execute(
            select(CaliberProjectMember).where(
                CaliberProjectMember.project_id == project_id,
                CaliberProjectMember.user_id == user_id,
            )
        ).scalar_one_or_none()
        if member is None:
            raise HTTPException(status_code=404, detail="project member not found")
        # `P1-C`: the primary-owner invariant now lives on `project.owner`,
        # not on holding the `owner` role -- multiple `owner`-role (Admin)
        # memberships are allowed, and every one of them *except* the
        # primary owner's own row can be freely demoted/deactivated here.
        # Changing the primary owner's row is only ever done through
        # `transfer-ownership`, which moves `project.owner` itself.
        if project.owner == user_id:
            raise HTTPException(
                status_code=409,
                detail="the primary owner cannot be changed here; use transfer-ownership",
            )
        # `P1-C` review fix: checking only `payload.role == ROLE_OWNER` missed
        # reactivating an *already* `owner`-role member through a status-only
        # update (`{"status": "active"}`, no `role` field) -- `payload.role`
        # is `None` there, so the check never ran and a member deactivated
        # while eligible could be reactivated with zero re-verification,
        # contradicting `is_eligible_for_owner_role`'s own "checked against
        # live grants, not a snapshot" contract. Re-derive the *effective*
        # role/reactivation instead of trusting `payload.role` alone: the
        # check now fires whenever this request explicitly grants the
        # `owner` role, or reactivates a member whose stored role already is
        # `owner`. A demotion away from `owner` (even one that also
        # reactivates) is never blocked by this -- `effective_role` would
        # not be `ROLE_OWNER` in that case.
        effective_role = payload.role if payload.role is not None else member.role
        reactivating = payload.status == "active" and member.status != "active"
        granting_owner_role = payload.role == ROLE_OWNER
        if effective_role == ROLE_OWNER and (granting_owner_role or reactivating):
            _require_owner_role_eligible(request, user_id)
        if payload.role is not None:
            member.role = payload.role
        if payload.status is not None:
            member.status = payload.status
            if payload.status == "inactive":
                member.deactivated_at = datetime.now(timezone.utc)
                member.deactivated_by = identity.user_id
            else:
                member.deactivated_at = None
                member.deactivated_by = None
        audit_record(
            session,
            actor=identity.user_id,
            action="update_project_member",
            entity_type="project",
            entity_id=project_id,
            details={"user_id": user_id, "role": member.role, "status": member.status},
        )
        session.commit()
        data = ProjectMemberSchema.model_validate(member_payload(member))
    return envelope_response(data)


async def remove_project_member(request: Request) -> JSONResponse:
    # See `add_project_member`: `project.manage_members` requires
    # `caliber.operator`, not just the project-role permission.
    require_scopes(request, [SCOPE_OPERATOR])
    identity = resolve_identity(request)
    project_id = request.path_params["project_id"]
    user_id = request.path_params["user_id"]
    factory = get_session_factory(request)
    with factory() as session:
        project, _decision = require_project_access(
            session, identity, project_id, "project.manage_members"
        )
        member = session.execute(
            select(CaliberProjectMember).where(
                CaliberProjectMember.project_id == project_id,
                CaliberProjectMember.user_id == user_id,
            )
        ).scalar_one_or_none()
        if member is None:
            raise HTTPException(status_code=404, detail="project member not found")
        # `P1-C`: only the primary owner's own row is protected -- see
        # `update_project_member`. A secondary Admin (`owner`-role, not the
        # primary owner) can be removed like any other member.
        if project.owner == user_id:
            raise HTTPException(status_code=409, detail="the primary owner cannot be removed")
        member.status = "inactive"
        member.deactivated_at = datetime.now(timezone.utc)
        member.deactivated_by = identity.user_id
        audit_record(
            session,
            actor=identity.user_id,
            action="remove_project_member",
            entity_type="project",
            entity_id=project_id,
            details={"user_id": user_id},
        )
        session.commit()
    return JSONResponse({"data": {"project_id": project_id, "user_id": user_id, "removed": True}})


async def update_project(request: Request) -> JSONResponse:
    project_id = request.path_params["project_id"]
    body = await parse_json_object(request)
    # `project.update` requires `caliber.operator` (section 2.4) -- same
    # gap class as `add_project_member`, found during the same review: an
    # editor with only `caliber.viewer` holds the project-role permission
    # but not the global-scope ceiling.
    require_scopes(request, [SCOPE_OPERATOR])
    identity = resolve_identity(request)
    # `P1-C`: section 12.2's target contract narrows `PATCH` to name/
    # description only -- lifecycle transitions move to the dedicated,
    # audited `:archive`/`:restore` routes below, which also set
    # `archived_at`/`archived_by` (this route never did). Rejecting `status`
    # explicitly here, rather than silently ignoring it, tells a caller
    # still targeting the old contract exactly where the transition moved.
    if "status" in body:
        raise HTTPException(
            status_code=400,
            detail="'status' is not settable via PATCH; use :archive or :restore",
        )
    factory = get_session_factory(request)
    with factory() as session:
        project = _require_project_action(
            session, project_id, identity=identity, action="project.update"
        )
        actor = identity.user_id
        if isinstance(body.get("name"), str) and body["name"].strip():
            project.name = body["name"].strip()
        if isinstance(body.get("description"), str):
            project.description = body["description"].strip()
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
            details={"name": project.name, "description": project.description},
        )
        session.commit()
        payload = _project_to_schema(project)
    return envelope_response(payload)


async def archive_project(request: Request) -> JSONResponse:
    """`P1-C`, section 12.2: explicit archive transition with provenance.

    Distinct from the old `PATCH .../projects/{id}` status flip: this route
    records who archived the project and when (`archived_at`/`archived_by`,
    schema-prepped by migration `0093`, unwired until now) and gives it its
    own audited action name rather than the generic ``update_project``.
    """
    project_id = request.path_params["project_id"]
    require_scopes(request, [SCOPE_OPERATOR])
    identity = resolve_identity(request)
    factory = get_session_factory(request)
    with factory() as session:
        project = _require_project_action(
            session, project_id, identity=identity, action="project.archive"
        )
        if project.status == "archived":
            raise HTTPException(status_code=409, detail="project is already archived")
        project.status = "archived"
        project.archived_at = datetime.now(timezone.utc)
        project.archived_by = identity.user_id
        audit_record(
            session,
            actor=identity.user_id,
            action="archive_project",
            entity_type="project",
            entity_id=project_id,
            details={"status": project.status},
        )
        session.commit()
        payload = _project_to_schema(project)
    return envelope_response(payload)


async def restore_project(request: Request) -> JSONResponse:
    """`P1-C`, section 12.2: the inverse of `archive_project`.

    Clears `archived_at`/`archived_by` rather than leaving them set --
    those columns describe the *current* archive transition, not a
    permanent history (the audit log already carries that; see
    `archive_project`/`restore_project`'s own audit records).
    """
    project_id = request.path_params["project_id"]
    require_scopes(request, [SCOPE_OPERATOR])
    identity = resolve_identity(request)
    factory = get_session_factory(request)
    with factory() as session:
        project = _require_project_action(
            session, project_id, identity=identity, action="project.restore"
        )
        if project.status != "archived":
            raise HTTPException(status_code=409, detail="project is not archived")
        project.status = "active"
        project.archived_at = None
        project.archived_by = None
        audit_record(
            session,
            actor=identity.user_id,
            action="restore_project",
            entity_type="project",
            entity_id=project_id,
            details={"status": project.status},
        )
        session.commit()
        payload = _project_to_schema(project)
    return envelope_response(payload)


async def transfer_project_ownership(request: Request) -> JSONResponse:
    """`P1-C`, section 12.2 + 19.1 item 3: atomic primary-owner transfer.

    Only the *current* primary owner (``project.owner``, not merely any
    ``owner``-role Admin membership) may initiate a transfer -- a secondary
    Admin holding the ``owner`` role does not change primary ownership by
    calling this route, matching the exit criterion "secondary Admin does
    not change primary owner". The target must already be an active
    ``owner``-role member (promoted through `add_project_member`/
    `update_project_member`, which already re-check scope eligibility at
    grant time) *and* still carry both required scopes right now -- a grant
    made while eligible does not stay valid forever if the target is later
    demoted at the platform-identity level.
    """
    body = await parse_json_object(request)
    payload = ProjectTransferOwnershipRequest.model_validate(body)
    new_owner_user_id = payload.new_owner_user_id.strip()
    if not new_owner_user_id:
        raise HTTPException(status_code=400, detail="'new_owner_user_id' is required")
    require_scopes(request, [SCOPE_OPERATOR])
    identity = resolve_identity(request)
    project_id = request.path_params["project_id"]
    factory = get_session_factory(request)
    with factory() as session:
        project = _require_project_action(
            session, project_id, identity=identity, action="project.transfer_owner"
        )
        if project.owner != identity.user_id:
            raise HTTPException(
                status_code=403,
                detail="only the current primary owner may transfer ownership",
            )
        if new_owner_user_id == project.owner:
            raise HTTPException(status_code=409, detail="user is already the primary owner")
        target = session.execute(
            select(CaliberProjectMember).where(
                CaliberProjectMember.project_id == project_id,
                CaliberProjectMember.user_id == new_owner_user_id,
                CaliberProjectMember.status == "active",
            )
        ).scalar_one_or_none()
        if target is None or target.role != ROLE_OWNER:
            raise HTTPException(
                status_code=409,
                detail="new_owner_user_id must be an active owner-role (Admin) member",
            )
        _require_owner_role_eligible(request, new_owner_user_id)
        previous_owner = project.owner
        # Copilot review of this PR's first version: the earlier `project.owner
        # != identity.user_id` check above reads a value that can go stale --
        # two concurrent transfer requests from the same primary owner both
        # pass it before either commits, and whichever commits second silently
        # overwrites the first (a lost update, plus a misleading audit record
        # for a transfer that never actually took effect). Making the write
        # itself conditional on `owner` still matching `identity.user_id` at
        # commit time -- not just at the earlier read -- closes that window:
        # only the request that still sees the expected current owner can
        # ever change the row, and a loser gets a `409` instead of silently
        # clobbering the winner.
        result = session.execute(
            sa_update(CaliberProject)
            .where(
                CaliberProject.project_id == project_id,
                CaliberProject.owner == previous_owner,
            )
            .values(owner=new_owner_user_id)
        )
        if int(getattr(result, "rowcount", 0) or 0) != 1:
            raise HTTPException(
                status_code=409,
                detail="primary owner changed since this request was read; retry",
            )
        # Keep the already-loaded ORM object in sync with the conditional
        # update just applied above (a plain Core `update()` bypasses the
        # ORM's own change tracking) -- `_project_to_schema` below reads it.
        project.owner = new_owner_user_id
        audit_record(
            session,
            actor=identity.user_id,
            action="transfer_project_owner",
            entity_type="project",
            entity_id=project_id,
            details={"previous_owner": previous_owner, "new_owner": new_owner_user_id},
        )
        session.commit()
        payload_out = _project_to_schema(project)
    return envelope_response(payload_out)


async def list_project_files(request: Request) -> JSONResponse:
    require_user(request)
    project_id = request.path_params["project_id"]
    factory = get_session_factory(request)
    with factory() as session:
        _require_project(session, project_id, identity=resolve_identity(request))
        rows = _visible_project_rows(session, project_id)
        items = [
            CaliberFileRecord.from_row(r).to_api()
            for r in rows
            if r.status in VISIBLE_STATUSES and not _is_directory_marker(r)
        ]
        directories = _folders_from_rows(project_id, rows)
    return envelope_response(
        ProjectFileListSchema(
            items=[ProjectFileSchema.model_validate(item) for item in items],
            directories=[ProjectFolderSchema.model_validate(d) for d in directories],
            next_cursor=None,
        )
    )


async def create_project_folder(request: Request) -> JSONResponse:
    project_id = request.path_params["project_id"]
    body = await parse_json_object(request)
    require_scopes(request, [SCOPE_OPERATOR])
    identity = resolve_identity(request)
    actor = identity.user_id
    raw_path = body.get("path")
    if not isinstance(raw_path, str) or not raw_path.strip():
        raise HTTPException(status_code=400, detail="'path' is required")
    factory = get_session_factory(request)
    try:
        with factory() as session:
            project = _require_project_action(
                session, project_id, identity=identity, action="resource.write"
            )
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
    return envelope_response(ProjectFolderSchema.model_validate(payload), status_code=201)


async def upload_project_file(request: Request) -> JSONResponse:
    project_id = request.path_params["project_id"]
    require_scopes(request, [SCOPE_OPERATOR])
    identity = resolve_identity(request)
    actor = identity.user_id
    data, filename, kind, media_type, _metadata = await _read_upload(request)
    factory = get_session_factory(request)
    try:
        with factory() as session:
            project = _require_project_action(
                session, project_id, identity=identity, action="resource.write"
            )
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
    return envelope_response(ProjectFileSchema.model_validate(payload), status_code=201)


async def download_project_file(request: Request) -> Response:
    project_id = request.path_params["project_id"]
    file_id = request.path_params["file_id"]
    identity = resolve_identity(request)
    actor = identity.user_id
    service = get_working_dir_service(request)
    factory = get_session_factory(request)
    try:
        with factory() as session:
            _require_project_action(session, project_id, identity=identity, action="read")
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
    require_scopes(request, [SCOPE_OPERATOR])
    identity = resolve_identity(request)
    actor = identity.user_id
    service = get_working_dir_service(request)
    factory = get_session_factory(request)
    with factory() as session:
        _require_project_action(session, project_id, identity=identity, action="resource.write")
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
    return envelope_response(DeletedSchema(file_id=file_id, status="deleted"))


def register(app: Starlette) -> None:
    app.routes.append(Route(LIST_PATH, list_projects, methods=["GET"]))
    app.routes.append(Route(LIST_PATH, create_project, methods=["POST"]))
    app.routes.append(Route(STORAGE_PATH, get_project_storage, methods=["GET"]))
    app.routes.append(Route(FOLDERS_PATH, create_project_folder, methods=["POST"]))
    app.routes.append(Route(FILES_PATH, upload_project_file, methods=["POST"]))
    app.routes.append(Route(FILES_PATH, list_project_files, methods=["GET"]))
    app.routes.append(Route(FILE_CONTENT_PATH, download_project_file, methods=["GET"]))
    app.routes.append(Route(FILE_PATH, delete_project_file, methods=["DELETE"]))
    app.routes.append(Route(DETAIL_PATH + "/members", list_project_members, methods=["GET"]))
    app.routes.append(Route(DETAIL_PATH + "/members", add_project_member, methods=["POST"]))
    app.routes.append(
        Route(DETAIL_PATH + "/members/{user_id}", update_project_member, methods=["PATCH"])
    )
    app.routes.append(
        Route(DETAIL_PATH + "/members/{user_id}", remove_project_member, methods=["DELETE"])
    )
    app.routes.append(Route(DETAIL_PATH, get_project, methods=["GET"]))
    app.routes.append(Route(DETAIL_PATH, update_project, methods=["PATCH"]))
    app.routes.append(Route(DETAIL_PATH + "/archive", archive_project, methods=["POST"]))
    app.routes.append(Route(DETAIL_PATH + "/restore", restore_project, methods=["POST"]))
    app.routes.append(
        Route(DETAIL_PATH + "/transfer-ownership", transfer_project_ownership, methods=["POST"])
    )
