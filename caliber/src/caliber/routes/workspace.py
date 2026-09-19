"""Provider-neutral Workspace source, import, and revision routes (`P4-C`).

This slice makes the persistence primitives from `workspace_source`,
`workspace_imports`, and `workspace_revisions` recoverable over HTTP. It does
not call a Git provider and does not claim to implement mutable-resource
adapters or Change Requests. A source provider is supplied through the
application state's :class:`WorkspaceSourceProviderRegistry`; without one,
source configuration remains safely disabled.
"""

from __future__ import annotations

import base64
import hashlib
import io
import json
import zipfile
from collections.abc import Mapping
from datetime import datetime, timedelta, timezone
from typing import Any, Literal, cast

import yaml
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker
from starlette.applications import Starlette
from starlette.concurrency import run_in_threadpool
from starlette.datastructures import UploadFile
from starlette.exceptions import HTTPException
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from caliber.audit import record as audit_record
from caliber.auth import (
    SCOPE_OPERATOR,
    CaliberIdentity,
    require_scopes,
    require_user,
    resolve_identity,
)
from caliber.db.models import (
    CaliberProject,
    CaliberWorkflowFile,
    CaliberWorkspaceImportJob,
    CaliberWorkspaceRevision,
    CaliberWorkspaceRevisionResource,
    CaliberWorkspaceSource,
)
from caliber.ids import (
    new_workspace_import_id,
    new_workspace_revision_id,
    new_workspace_revision_resource_id,
    new_workspace_source_id,
)
from caliber.resource_access import require_project_access
from caliber.routes._deps import (
    envelope_response,
    get_session_factory,
    get_working_dir_service,
    parse_json_object,
)
from caliber.schemas import (
    WorkspaceImportJobListSchema,
    WorkspaceImportJobSchema,
    WorkspaceImportReconcileSchema,
    WorkspaceRevisionDiffSchema,
    WorkspaceRevisionListSchema,
    WorkspaceRevisionResourceSchema,
    WorkspaceRevisionSchema,
    WorkspaceRevisionSnapshotRequest,
    WorkspaceRevisionSnapshotResourceRequest,
    WorkspaceSourceCapabilitiesSchema,
    WorkspaceSourceConfigureRequest,
    WorkspaceSourceResponse,
    WorkspaceSourceSchema,
)
from caliber.storage import (
    StorageError,
    StorageValidationError,
    WorkingDirectoryService,
    safe_relative_path,
)
from caliber.workflows.manifest import canonical_json
from caliber.workspace_manifest import manifest_digest, parse_workspace_manifest
from caliber.workspace_release_adapters import (
    SnapshotPin,
    WorkspaceReleaseAdapterError,
    WorkspaceReleaseAdapterUnavailableError,
    WorkspaceResourceAdapterRegistry,
)
from caliber.workspace_revisions import allocate_revision_number, finalize_revision
from caliber.workspace_source import materialize_workspace_source
from caliber.workspace_sources import (
    WorkspaceSourceProviderError,
    WorkspaceSourceProviderRegistry,
    WorkspaceSourceProviderUnavailableError,
    WorkspaceSourceVerificationError,
    source_etag,
)

PREFIX = "/ajax-api/2.0/mlflow/caliber"
SOURCE_PATH = PREFIX + "/projects/{project_id}/source"
SOURCE_ENABLE_PATH = PREFIX + "/projects/{project_id}/source:enable"
SOURCE_DISABLE_PATH = PREFIX + "/projects/{project_id}/source:disable"
SOURCE_CAPABILITIES_PATH = PREFIX + "/projects/{project_id}/source/capabilities"
SOURCE_RECONCILE_PATH = PREFIX + "/projects/{project_id}/source:reconcile"
IMPORTS_PATH = PREFIX + "/projects/{project_id}/revision-imports"
IMPORT_DETAIL_PATH = IMPORTS_PATH + "/{job_id}"
IMPORT_RECONCILE_PATH = IMPORT_DETAIL_PATH + ":reconcile"
REVISIONS_PATH = PREFIX + "/projects/{project_id}/revisions"
REVISION_DETAIL_PATH = REVISIONS_PATH + "/{revision_id}"
REVISION_DIFF_PATH = REVISION_DETAIL_PATH + "/diff"
REVISIONS_SNAPSHOT_PATH = REVISIONS_PATH + ":snapshot"

_ETAG_RESPONSE = {
    "description": "The source changed or is not configured for the supplied validator.",
    "content": {
        "application/json": {
            "schema": {
                "type": "object",
                "properties": {
                    "detail": {"type": "string"},
                    "status_code": {"type": "integer"},
                },
                "required": ["detail", "status_code"],
            }
        }
    },
}

_Factory = sessionmaker[Session]
_IMPORT_PAGE_DEFAULT = 50
_IMPORT_PAGE_MAX = 100
_REVISION_PAGE_DEFAULT = 50
_REVISION_PAGE_MAX = 100
_CURSOR_VERSION = "workspace-v1"
_IDEMPOTENCY_KEY_MAX = 256
_COMMIT_SHA_MAX = 128


def _utc_now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value is not None else None


def _quoted_etag(value: str) -> str:
    return f'"{value}"'


def _etag_value(header: str | None) -> str | None:
    if header is None:
        return None
    value = header.strip()
    if value == "*":
        return value
    if value.startswith('"') and value.endswith('"') and value != '"':
        value = value[1:-1]
    return value or None


def _require_workspace_header(request: Request, project_id: str) -> None:
    header_project_id = request.headers.get("X-CALIBER-Project")
    if header_project_id is not None and header_project_id != project_id:
        raise HTTPException(status_code=400, detail="workspace_context_mismatch")


def _require_project_bound_pat_for_import(identity: CaliberIdentity, project_id: str) -> None:
    """Refuse a PAT-authenticated import unless the token is bound to `project_id`.

    `P1-E` (docs/workspace-plan.md section 9.1) delivered the plumbing --
    ``CaliberIdentity.credential_kind``/``credential_project_id`` and
    ``auth.py::resolve_identity``'s central refusal of a project-bound PAT
    naming a *different* project via the URL/header -- but explicitly left
    "require project-bound PATs for CI import" unenforced (no CI-import route
    existed yet). This is that route now (`P4-C`'s ``create_import``, plus
    ``reconcile_import`` since it performs the same durable, project-scoped
    import mutation on the same resource family).

    Section 9.1's design: "New CI import tokens must be project-bound. A CI
    actor is a dedicated automation user authenticated by a project-bound
    PAT..." -- read together with section 4's workflow table, which allows
    *either* "Developer" (an interactive session) *or* "CI automation user
    with project-bound PAT" to package/pin. So the requirement is scoped to
    the PAT credential kind specifically, not to every caller:

    * A session (or trusted-header) caller is never touched here --
      ``credential_kind != "pat"`` is a no-op, matching "Developer" access.
    * A PAT bound to a *different* project is already refused (403) by
      ``resolve_identity`` before this ever runs, since ``project_id`` is
      always a path segment on the import routes. Re-checking here is a
      harmless belt-and-suspenders match on the same condition.
    * A PAT with **no** project binding is newly refused here: "New CI
      import tokens must be project-bound" only makes sense as a real gate
      if an unbound PAT cannot substitute for one. An unbound PAT remains
      valid for every other route `P1-E` already covers (reads, non-import
      writes); only this durable import-mutation surface now requires the
      binding.
    """
    if identity.credential_kind != "pat":
        return
    if identity.credential_project_id != project_id:
        if identity.credential_project_id is None:
            detail = (
                "workspace import requires a personal access token bound to "
                f"project {project_id!r}; this token has no project binding"
            )
        else:
            detail = (
                f"this personal access token is bound to project "
                f"{identity.credential_project_id!r} and cannot import into "
                f"project {project_id!r}"
            )
        raise HTTPException(status_code=403, detail=detail)


def _require_if_match(request_value: str | None, current: str) -> None:
    supplied = _etag_value(request_value)
    if supplied in {"*", current}:
        return
    raise HTTPException(
        status_code=412,
        detail="source_etag_mismatch",
        headers={"ETag": _quoted_etag(current)},
    )


def _repository_path(value: str, *, allow_empty: bool = False) -> str:
    normalized = value.strip()
    if not normalized and allow_empty:
        return ""
    try:
        return safe_relative_path(normalized)
    except StorageValidationError as exc:
        raise HTTPException(status_code=400, detail=f"invalid_repository_path: {exc}") from exc


def _source_schema(source: CaliberWorkspaceSource) -> WorkspaceSourceSchema:
    return WorkspaceSourceSchema(
        source_id=source.source_id,
        project_id=source.project_id,
        provider=cast(Literal["github", "gitlab", "bitbucket"], source.provider),
        provider_host=source.provider_host,
        canonical_repository_id=source.canonical_repository_id,
        display_path=source.display_path,
        default_branch=source.default_branch,
        root_path=source.root_path,
        manifest_path=source.manifest_path,
        import_mode=cast(Literal["push", "provider_pull"], source.import_mode),
        status=cast(Literal["active", "disabled", "error"], source.status),
        has_connection=bool(source.connection_ref),
        external_review_policy_version=source.external_review_policy_version,
        provider_ruleset_sha256=source.provider_ruleset_sha256,
        last_verified_at=_iso(source.last_verified_at),
        last_reconciled_at=_iso(source.last_reconciled_at),
        updated_at=_iso(source.updated_at),
        etag=source_etag(source),
    )


def _source_response(
    project: CaliberProject, source: CaliberWorkspaceSource | None
) -> WorkspaceSourceResponse:
    return WorkspaceSourceResponse(
        source_mode=cast(Literal["caliber_managed", "git_managed"], project.source_mode),
        source=_source_schema(source) if source is not None else None,
    )


def _source_for_project(session: Session, project_id: str) -> CaliberWorkspaceSource | None:
    return session.execute(
        select(CaliberWorkspaceSource).where(CaliberWorkspaceSource.project_id == project_id)
    ).scalar_one_or_none()


def _source_has_inflight_import(session: Session, source_id: str) -> bool:
    return (
        session.execute(
            select(CaliberWorkspaceImportJob.import_job_id)
            .where(
                CaliberWorkspaceImportJob.source_id == source_id,
                CaliberWorkspaceImportJob.status.in_(("queued", "running", "reconcile_required")),
            )
            .limit(1)
        ).scalar_one_or_none()
        is not None
    )


def _get_source_sync(
    factory: _Factory,
    *,
    project_id: str,
    identity: CaliberIdentity,
    project_action: str,
) -> WorkspaceSourceResponse:
    with factory() as session:
        project, _decision = require_project_access(session, identity, project_id, project_action)
        return _source_response(project, _source_for_project(session, project_id))


def _put_source_sync(
    factory: _Factory,
    *,
    project_id: str,
    actor: str,
    identity: CaliberIdentity,
    project_action: str,
    payload: WorkspaceSourceConfigureRequest,
    if_match: str | None,
) -> WorkspaceSourceResponse:
    with factory() as session:
        project, _decision = require_project_access(session, identity, project_id, project_action)
        if project.status != "active":
            raise HTTPException(status_code=409, detail="archived_workspace")
        source = _source_for_project(session, project_id)
        if source is not None:
            current = source_etag(source)
            _require_if_match(if_match, current)
            if _source_has_inflight_import(session, source.source_id):
                raise HTTPException(status_code=409, detail="source_has_in_flight_import")
            if source.status == "active":
                raise HTTPException(
                    status_code=409,
                    detail="source_binding_active_disable_before_replacement",
                )
        else:
            if _etag_value(if_match) is not None:
                raise HTTPException(status_code=412, detail="source_not_configured")
            source = CaliberWorkspaceSource(
                source_id=new_workspace_source_id(),
                project_id=project_id,
                provider=payload.provider,
                provider_host=payload.provider_host.strip().lower(),
                canonical_repository_id=payload.canonical_repository_id.strip(),
                display_path=payload.display_path.strip(),
                default_branch=payload.default_branch.strip(),
                root_path=_repository_path(payload.root_path, allow_empty=True),
                manifest_path=_repository_path(payload.manifest_path),
                import_mode=payload.import_mode,
                connection_ref=payload.connection_ref.strip() if payload.connection_ref else None,
                created_by=actor,
            )
            session.add(source)

        source.provider = payload.provider
        source.provider_host = payload.provider_host.strip().lower()
        source.canonical_repository_id = payload.canonical_repository_id.strip()
        source.display_path = payload.display_path.strip()
        source.default_branch = payload.default_branch.strip()
        source.root_path = _repository_path(payload.root_path, allow_empty=True)
        source.manifest_path = _repository_path(payload.manifest_path)
        source.import_mode = payload.import_mode
        source.connection_ref = payload.connection_ref.strip() if payload.connection_ref else None
        source.status = "disabled"
        source.provider_capabilities = {}
        source.last_verified_at = None
        source.last_reconciled_at = None
        source.updated_by = actor
        project.source_mode = "git_managed"
        session.flush()
        audit_record(
            session,
            actor=actor,
            action="configure_workspace_source",
            entity_type="workspace_source",
            entity_id=source.source_id,
            details={"project_id": project_id, "provider": source.provider},
        )
        session.commit()
        return _source_response(project, source)


def _provider_registry(request: Request) -> WorkspaceSourceProviderRegistry:
    registry = getattr(request.app.state, "workspace_source_registry", None)
    if registry is None:
        return WorkspaceSourceProviderRegistry()
    if not isinstance(registry, WorkspaceSourceProviderRegistry):
        raise RuntimeError("app.state.workspace_source_registry has an invalid type")
    return registry


def _resource_adapter_registry(request: Request) -> WorkspaceResourceAdapterRegistry:
    registry = getattr(request.app.state, "workspace_resource_adapter_registry", None)
    if registry is None:
        return WorkspaceResourceAdapterRegistry()
    if not isinstance(registry, WorkspaceResourceAdapterRegistry):
        raise RuntimeError("app.state.workspace_resource_adapter_registry has an invalid type")
    return registry


def _transition_source_sync(  # noqa: PLR0912, PLR0915 - explicit lifecycle state machine
    factory: _Factory,
    *,
    project_id: str,
    actor: str,
    identity: CaliberIdentity,
    project_action: str,
    transition: str,
    if_match: str | None,
    registry: WorkspaceSourceProviderRegistry,
) -> WorkspaceSourceResponse:
    with factory() as session:
        project, _decision = require_project_access(session, identity, project_id, project_action)
        source = _source_for_project(session, project_id)
        if source is None:
            raise HTTPException(status_code=404, detail="workspace source not found")
        current = source_etag(source)
        _require_if_match(if_match, current)
        if transition == "enable":
            if source.status != "disabled":
                raise HTTPException(status_code=409, detail="source_not_disabled")
            try:
                verification = registry.verify(source)
            except WorkspaceSourceProviderUnavailableError as exc:
                raise HTTPException(status_code=409, detail=str(exc)) from exc
            except WorkspaceSourceVerificationError as exc:
                source.status = "error"
                source.updated_by = actor
                session.commit()
                raise HTTPException(status_code=409, detail=str(exc)) from exc
            except WorkspaceSourceProviderError as exc:
                raise HTTPException(status_code=409, detail=str(exc)) from exc
            source.provider_capabilities = dict(verification.capabilities)
            source.last_verified_at = _utc_now()
            source.status = "active"
            audit_action = "enable_workspace_source"
        elif transition == "disable":
            if source.status not in {"active", "error"}:
                raise HTTPException(status_code=409, detail="source_not_active")
            if _source_has_inflight_import(session, source.source_id):
                raise HTTPException(status_code=409, detail="source_has_in_flight_import")
            source.status = "disabled"
            audit_action = "disable_workspace_source"
        elif transition == "reconcile":
            if source.status not in {"active", "error"}:
                raise HTTPException(status_code=409, detail="source_not_active")
            try:
                verification = registry.verify(source)
            except WorkspaceSourceProviderError as exc:
                raise HTTPException(status_code=409, detail=str(exc)) from exc
            source.provider_capabilities = dict(verification.capabilities)
            source.last_verified_at = _utc_now()
            source.last_reconciled_at = source.last_verified_at
            source.status = "active"
            audit_action = "reconcile_workspace_source"
        else:  # pragma: no cover - private helper has a closed call vocabulary
            raise ValueError(f"unknown source transition {transition!r}")

        source.updated_by = actor
        session.flush()
        audit_record(
            session,
            actor=actor,
            action=audit_action,
            entity_type="workspace_source",
            entity_id=source.source_id,
            details={"project_id": project_id, "status": source.status},
        )
        session.commit()
        return _source_response(project, source)


def _source_capabilities_sync(
    factory: _Factory,
    *,
    project_id: str,
    identity: CaliberIdentity,
    project_action: str,
    registry: WorkspaceSourceProviderRegistry,
) -> WorkspaceSourceCapabilitiesSchema:
    with factory() as session:
        require_project_access(session, identity, project_id, project_action)
        source = _source_for_project(session, project_id)
        if source is None:
            raise HTTPException(status_code=404, detail="workspace source not found")
        capabilities = registry.capabilities(source.provider)
        return WorkspaceSourceCapabilitiesSchema(
            source_id=source.source_id,
            provider=source.provider,
            provider_host=source.provider_host,
            available=capabilities is not None,
            capabilities=dict(capabilities or {}),
            reason=None if capabilities is not None else "source_provider_unavailable",
            last_verified_at=_iso(source.last_verified_at),
        )


def _job_schema(job: CaliberWorkspaceImportJob) -> WorkspaceImportJobSchema:
    return WorkspaceImportJobSchema(
        import_job_id=job.import_job_id,
        project_id=job.project_id,
        source_id=job.source_id,
        repository=job.repository,
        commit_sha=job.commit_sha,
        upload_sha256=job.upload_sha256,
        source_bundle_sha256=job.source_bundle_sha256,
        source_snapshot_file_id=job.source_snapshot_file_id,
        manifest_sha256=job.manifest_sha256,
        status=cast(
            Literal["queued", "running", "succeeded", "failed", "reconcile_required"], job.status
        ),
        revision_id=job.revision_id,
        idempotency_key=job.idempotency_key,
        attempt_count=job.attempt_count,
        max_attempts=job.max_attempts,
        claimed_by=job.claimed_by,
        claimed_at=_iso(job.claimed_at),
        lease_expires_at=_iso(job.lease_expires_at),
        last_heartbeat_at=_iso(job.last_heartbeat_at),
        error_code=job.error_code,
        error_summary=job.error_summary,
        created_by=job.created_by,
        updated_by=job.updated_by,
        created_at=_iso(job.created_at),
        updated_at=_iso(job.updated_at),
        completed_at=_iso(job.completed_at),
    )


def _manifest_from_bundle(data: bytes, path: str) -> tuple[dict[str, object], str]:
    try:
        normalized_path = safe_relative_path(path)
    except StorageValidationError as exc:
        raise HTTPException(status_code=400, detail=f"invalid_manifest_path: {exc}") from exc
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            manifest_bytes: bytes | None = None
            for info in archive.infolist():
                if info.is_dir():
                    continue
                if safe_relative_path(info.filename) == normalized_path:
                    manifest_bytes = archive.read(info)
                    break
    except (OSError, RuntimeError, ValueError, StorageError, zipfile.BadZipFile) as exc:
        raise HTTPException(status_code=400, detail=f"manifest_read_failed: {exc}") from exc
    if manifest_bytes is None:
        raise HTTPException(status_code=400, detail=f"manifest_not_found: {normalized_path}")
    try:
        document = yaml.safe_load(manifest_bytes)
    except yaml.YAMLError as exc:
        raise HTTPException(status_code=400, detail=f"invalid_manifest_yaml: {exc}") from exc
    if not isinstance(document, dict):
        raise HTTPException(status_code=400, detail="manifest_must_decode_to_mapping")
    manifest = parse_workspace_manifest(cast(dict[str, object], document))
    return manifest.model_dump(mode="json"), manifest_digest(manifest)


def _form_text(form: Mapping[str, Any], name: str, *, required: bool = True) -> str | None:
    value = form.get(name)
    if value is None:
        if required:
            raise HTTPException(status_code=400, detail=f"{name}_required")
        return None
    if not isinstance(value, str):
        raise HTTPException(status_code=400, detail=f"{name}_must_be_text")
    normalized = value.strip()
    if required and not normalized:
        raise HTTPException(status_code=400, detail=f"{name}_required")
    return normalized or None


def _same_import_input(
    job: CaliberWorkspaceImportJob,
    *,
    source_id: str,
    repository: str,
    commit_sha: str,
    upload_sha256: str,
    source_bundle_sha256: str,
    manifest_sha256: str,
) -> bool:
    return (
        job.source_id == source_id
        and job.repository == repository
        and job.commit_sha == commit_sha
        and job.upload_sha256 == upload_sha256
        and job.source_bundle_sha256 == source_bundle_sha256
        and job.manifest_sha256 == manifest_sha256
    )


def _create_import_sync(
    factory: _Factory,
    *,
    project_id: str,
    actor: str,
    identity: CaliberIdentity,
    project_action: str,
    repository: str,
    commit_sha: str,
    idempotency_key: str,
    bundle: bytes,
    storage: WorkingDirectoryService,
) -> WorkspaceImportJobSchema:
    with factory() as session:
        project, _decision = require_project_access(session, identity, project_id, project_action)
        if project.status != "active":
            raise HTTPException(status_code=409, detail="archived_workspace")
        source = _source_for_project(session, project_id)
        if source is None:
            raise HTTPException(status_code=409, detail="workspace_source_not_configured")
        if source.status != "active":
            raise HTTPException(status_code=409, detail="workspace_source_not_active")
        if source.import_mode != "push":
            raise HTTPException(status_code=409, detail="source_import_mode_does_not_accept_push")
        if repository != source.display_path:
            raise HTTPException(status_code=409, detail="repository_does_not_match_source")

        try:
            materialized = materialize_workspace_source(bundle)
        except StorageError as exc:
            raise HTTPException(status_code=400, detail=f"invalid_source_bundle: {exc}") from exc
        _manifest, manifest_sha256 = _manifest_from_bundle(bundle, source.manifest_path)

        existing = session.execute(
            select(CaliberWorkspaceImportJob).where(
                CaliberWorkspaceImportJob.project_id == project_id,
                CaliberWorkspaceImportJob.idempotency_key == idempotency_key,
            )
        ).scalar_one_or_none()
        if existing is not None:
            if _same_import_input(
                existing,
                source_id=source.source_id,
                repository=repository,
                commit_sha=commit_sha,
                upload_sha256=materialized.upload_sha256,
                source_bundle_sha256=materialized.source_bundle_sha256,
                manifest_sha256=manifest_sha256,
            ):
                return _job_schema(existing)
            raise HTTPException(status_code=409, detail="import_idempotency_conflict")

        conflicting_job = session.execute(
            select(CaliberWorkspaceImportJob)
            .where(
                CaliberWorkspaceImportJob.source_id == source.source_id,
                CaliberWorkspaceImportJob.commit_sha == commit_sha,
                CaliberWorkspaceImportJob.source_bundle_sha256.is_not(None),
                CaliberWorkspaceImportJob.source_bundle_sha256 != materialized.source_bundle_sha256,
            )
            .limit(1)
        ).scalar_one_or_none()
        if conflicting_job is not None:
            raise HTTPException(status_code=409, detail="source_commit_digest_conflict")

        snapshot = storage.for_backend(project.storage_backend).register_project_file(
            session,
            project_id=project_id,
            kind="artifact",
            filename=f".caliber/workspace-snapshots/{materialized.snapshot_sha256}.tar",
            data=materialized.snapshot_bytes,
            media_type="application/x-tar",
            actor=actor,
            tenant_id=project.tenant_id,
            metadata={
                "workspace_snapshot": True,
                "source_id": source.source_id,
                "commit_sha": commit_sha,
                "source_bundle_sha256": materialized.source_bundle_sha256,
                "snapshot_sha256": materialized.snapshot_sha256,
            },
        )
        job = CaliberWorkspaceImportJob(
            import_job_id=new_workspace_import_id(),
            project_id=project_id,
            source_id=source.source_id,
            repository=repository,
            commit_sha=commit_sha,
            upload_sha256=materialized.upload_sha256,
            source_bundle_sha256=materialized.source_bundle_sha256,
            source_snapshot_file_id=snapshot.file_id,
            manifest_sha256=manifest_sha256,
            idempotency_key=idempotency_key,
            created_by=actor,
        )
        session.add(job)
        session.flush()
        audit_record(
            session,
            actor=actor,
            action="queue_workspace_revision_import",
            entity_type="workspace_import_job",
            entity_id=job.import_job_id,
            details={
                "project_id": project_id,
                "source_id": source.source_id,
                "commit_sha": commit_sha,
                "source_bundle_sha256": materialized.source_bundle_sha256,
                "manifest_sha256": manifest_sha256,
            },
        )
        session.commit()
        return _job_schema(job)


def _job_for_project(session: Session, project_id: str, job_id: str) -> CaliberWorkspaceImportJob:
    job = session.execute(
        select(CaliberWorkspaceImportJob).where(
            CaliberWorkspaceImportJob.project_id == project_id,
            CaliberWorkspaceImportJob.import_job_id == job_id,
        )
    ).scalar_one_or_none()
    if job is None:
        raise HTTPException(status_code=404, detail=f"import job {job_id!r} not found")
    return job


def _get_import_sync(
    factory: _Factory,
    *,
    project_id: str,
    job_id: str,
    identity: CaliberIdentity,
    project_action: str,
) -> WorkspaceImportJobSchema:
    with factory() as session:
        require_project_access(session, identity, project_id, project_action)
        return _job_schema(_job_for_project(session, project_id, job_id))


def _parse_cursor(value: str | None) -> tuple[datetime, str] | None:
    if not value:
        return None
    try:
        padded = value + "=" * (-len(value) % 4)
        payload = json.loads(base64.urlsafe_b64decode(padded.encode("ascii")))
        if not isinstance(payload, dict) or payload.get("v") != _CURSOR_VERSION:
            raise ValueError
        stamp = datetime.fromisoformat(str(payload["created_at"])).replace(tzinfo=None)
        row_id = str(payload["id"])
        if not row_id:
            raise ValueError
        return stamp, row_id
    except (ValueError, TypeError, KeyError, json.JSONDecodeError):
        raise HTTPException(status_code=400, detail="invalid_cursor") from None


def _cursor(stamp: datetime, row_id: str) -> str:
    payload = {
        "v": _CURSOR_VERSION,
        "created_at": stamp.replace(tzinfo=None).isoformat(),
        "id": row_id,
    }
    raw = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _page_params(
    request: Request, *, default: int, maximum: int
) -> tuple[int, tuple[datetime, str] | None]:
    raw_limit = request.query_params.get("limit")
    try:
        limit = default if raw_limit is None else int(raw_limit)
    except ValueError:
        raise HTTPException(status_code=400, detail="invalid_limit") from None
    if limit < 1 or limit > maximum:
        raise HTTPException(status_code=400, detail=f"limit_must_be_between_1_and_{maximum}")
    return limit, _parse_cursor(request.query_params.get("cursor"))


def _list_imports_sync(
    factory: _Factory,
    *,
    project_id: str,
    identity: CaliberIdentity,
    project_action: str,
    limit: int,
    cursor: tuple[datetime, str] | None,
    status: str | None,
) -> tuple[list[WorkspaceImportJobSchema], str | None]:
    with factory() as session:
        require_project_access(session, identity, project_id, project_action)
        stmt = select(CaliberWorkspaceImportJob).where(
            CaliberWorkspaceImportJob.project_id == project_id
        )
        if status is not None:
            stmt = stmt.where(CaliberWorkspaceImportJob.status == status)
        if cursor is not None:
            stamp, row_id = cursor
            lower_bound = stamp - timedelta(microseconds=1)
            upper_bound = stamp + timedelta(microseconds=1)
            stmt = stmt.where(
                (CaliberWorkspaceImportJob.created_at < lower_bound)
                | (
                    (CaliberWorkspaceImportJob.created_at >= lower_bound)
                    & (CaliberWorkspaceImportJob.created_at < upper_bound)
                    & (CaliberWorkspaceImportJob.import_job_id < row_id)
                )
            )
        rows = (
            session.execute(
                stmt.order_by(
                    CaliberWorkspaceImportJob.created_at.desc(),
                    CaliberWorkspaceImportJob.import_job_id.desc(),
                ).limit(limit + 1)
            )
            .scalars()
            .all()
        )
        has_more = len(rows) > limit
        rows = rows[:limit]
        next_cursor = _cursor(rows[-1].created_at, rows[-1].import_job_id) if has_more else None
        return [_job_schema(row) for row in rows], next_cursor


def _reconcile_import_sync(
    factory: _Factory,
    *,
    project_id: str,
    job_id: str,
    actor: str,
    identity: CaliberIdentity,
    project_action: str,
    storage: WorkingDirectoryService,
) -> WorkspaceImportReconcileSchema:
    with factory() as session:
        require_project_access(session, identity, project_id, project_action)
        job = _job_for_project(session, project_id, job_id)
        if job.status != "reconcile_required":
            raise HTTPException(status_code=409, detail="import_not_reconcile_required")
        if not job.source_snapshot_file_id:
            raise HTTPException(status_code=409, detail="import_snapshot_unavailable")
        snapshot = session.get(CaliberWorkflowFile, job.source_snapshot_file_id)
        if snapshot is None or snapshot.project_id != project_id or snapshot.deleted_at is not None:
            raise HTTPException(status_code=409, detail="import_snapshot_unavailable")
        try:
            data = storage.read_bytes(snapshot)
        except StorageError as exc:
            raise HTTPException(
                status_code=409, detail=f"import_snapshot_unavailable: {exc}"
            ) from exc
        observed_sha256 = hashlib.sha256(data).hexdigest()
        if snapshot.sha256 != observed_sha256:
            raise HTTPException(status_code=409, detail="import_snapshot_digest_mismatch")
        metadata = snapshot.file_metadata if isinstance(snapshot.file_metadata, dict) else {}
        if metadata.get("snapshot_sha256") != observed_sha256:
            raise HTTPException(status_code=409, detail="import_snapshot_metadata_mismatch")
        audit_record(
            session,
            actor=actor,
            action="observe_workspace_import",
            entity_type="workspace_import_job",
            entity_id=job.import_job_id,
            details={"project_id": project_id, "snapshot_sha256": observed_sha256},
        )
        session.commit()
        return WorkspaceImportReconcileSchema(
            job=_job_schema(job),
            observed=True,
            observation="local_snapshot_intact",
        )


def _resource_schema(resource: CaliberWorkspaceRevisionResource) -> WorkspaceRevisionResourceSchema:
    return WorkspaceRevisionResourceSchema(
        resource_pin_id=resource.resource_pin_id,
        revision_id=resource.revision_id,
        resource_type=resource.resource_type,
        logical_name=resource.logical_name,
        resource_id=resource.resource_id,
        version_ref=resource.version_ref,
        content_sha256=resource.content_sha256,
        source_path=resource.source_path,
        source_sha256=resource.source_sha256,
        provider_ref=resource.provider_ref,
        snapshot_file_id=resource.snapshot_file_id,
        snapshot_sha256=resource.snapshot_sha256,
        purpose=resource.purpose,
        resolution=dict(resource.resolution or {}),
    )


def _revision_schema(
    revision: CaliberWorkspaceRevision,
    resources: list[CaliberWorkspaceRevisionResource],
) -> WorkspaceRevisionSchema:
    return WorkspaceRevisionSchema(
        revision_id=revision.revision_id,
        project_id=revision.project_id,
        revision_number=revision.revision_number,
        source_id=revision.source_id,
        source_commit_sha=revision.source_commit_sha,
        source_kind=cast(Literal["git", "managed"], revision.source_kind),
        manifest=dict(revision.manifest or {}),
        manifest_sha256=revision.manifest_sha256,
        source_bundle_sha256=revision.source_bundle_sha256,
        source_snapshot_file_id=revision.source_snapshot_file_id,
        source_attestation=revision.source_attestation,
        revision_sha256=revision.revision_sha256,
        status=cast(Literal["validating", "ready", "invalid"], revision.status),
        validation_report=dict(revision.validation_report or {})
        if revision.validation_report is not None
        else None,
        created_by=revision.created_by,
        validated_by=revision.validated_by,
        validated_at=_iso(revision.validated_at),
        created_at=_iso(revision.created_at),
        resources=[_resource_schema(resource) for resource in resources],
    )


def _revision_for_project(
    session: Session, project_id: str, revision_id: str
) -> CaliberWorkspaceRevision:
    revision = session.execute(
        select(CaliberWorkspaceRevision).where(
            CaliberWorkspaceRevision.project_id == project_id,
            CaliberWorkspaceRevision.revision_id == revision_id,
        )
    ).scalar_one_or_none()
    if revision is None:
        raise HTTPException(status_code=404, detail=f"revision {revision_id!r} not found")
    return revision


def _revision_resources(
    session: Session, revision_id: str
) -> list[CaliberWorkspaceRevisionResource]:
    return list(
        session.execute(
            select(CaliberWorkspaceRevisionResource)
            .where(CaliberWorkspaceRevisionResource.revision_id == revision_id)
            .order_by(
                CaliberWorkspaceRevisionResource.resource_type,
                CaliberWorkspaceRevisionResource.logical_name,
            )
        )
        .scalars()
        .all()
    )


def _list_revisions_sync(
    factory: _Factory,
    *,
    project_id: str,
    identity: CaliberIdentity,
    project_action: str,
    limit: int,
    cursor: tuple[datetime, str] | None,
    status: str | None,
) -> tuple[list[WorkspaceRevisionSchema], str | None]:
    with factory() as session:
        require_project_access(session, identity, project_id, project_action)
        stmt = select(CaliberWorkspaceRevision).where(
            CaliberWorkspaceRevision.project_id == project_id
        )
        if status is not None:
            stmt = stmt.where(CaliberWorkspaceRevision.status == status)
        if cursor is not None:
            stamp, row_id = cursor
            lower_bound = stamp - timedelta(microseconds=1)
            upper_bound = stamp + timedelta(microseconds=1)
            stmt = stmt.where(
                (CaliberWorkspaceRevision.created_at < lower_bound)
                | (
                    (CaliberWorkspaceRevision.created_at >= lower_bound)
                    & (CaliberWorkspaceRevision.created_at < upper_bound)
                    & (CaliberWorkspaceRevision.revision_id < row_id)
                )
            )
        rows = (
            session.execute(
                stmt.order_by(
                    CaliberWorkspaceRevision.created_at.desc(),
                    CaliberWorkspaceRevision.revision_id.desc(),
                ).limit(limit + 1)
            )
            .scalars()
            .all()
        )
        has_more = len(rows) > limit
        rows = rows[:limit]
        next_cursor = _cursor(rows[-1].created_at, rows[-1].revision_id) if has_more else None
        return [
            _revision_schema(row, _revision_resources(session, row.revision_id)) for row in rows
        ], next_cursor


def _get_revision_sync(
    factory: _Factory,
    *,
    project_id: str,
    revision_id: str,
    identity: CaliberIdentity,
    project_action: str,
) -> WorkspaceRevisionSchema:
    with factory() as session:
        require_project_access(session, identity, project_id, project_action)
        revision = _revision_for_project(session, project_id, revision_id)
        return _revision_schema(revision, _revision_resources(session, revision.revision_id))


def _resource_key(resource: CaliberWorkspaceRevisionResource) -> tuple[str, str]:
    return resource.resource_type, resource.logical_name


def _resource_changed(
    base: CaliberWorkspaceRevisionResource,
    candidate: CaliberWorkspaceRevisionResource,
) -> bool:
    return (
        base.resource_id,
        base.version_ref,
        base.content_sha256,
        base.source_path,
        base.source_sha256,
        base.provider_ref,
        base.snapshot_file_id,
        base.snapshot_sha256,
        base.purpose,
        base.resolution or {},
    ) != (
        candidate.resource_id,
        candidate.version_ref,
        candidate.content_sha256,
        candidate.source_path,
        candidate.source_sha256,
        candidate.provider_ref,
        candidate.snapshot_file_id,
        candidate.snapshot_sha256,
        candidate.purpose,
        candidate.resolution or {},
    )


def _diff_revisions_sync(
    factory: _Factory,
    *,
    project_id: str,
    revision_id: str,
    base_revision_id: str,
    identity: CaliberIdentity,
    project_action: str,
) -> WorkspaceRevisionDiffSchema:
    with factory() as session:
        require_project_access(session, identity, project_id, project_action)
        candidate = _revision_for_project(session, project_id, revision_id)
        base = _revision_for_project(session, project_id, base_revision_id)
        base_resources = {
            _resource_key(row): row for row in _revision_resources(session, base.revision_id)
        }
        candidate_resources = {
            _resource_key(row): row for row in _revision_resources(session, candidate.revision_id)
        }
        added = [
            candidate_resources[key]
            for key in sorted(candidate_resources.keys() - base_resources.keys())
        ]
        removed = [
            base_resources[key]
            for key in sorted(base_resources.keys() - candidate_resources.keys())
        ]
        changed = [
            candidate_resources[key]
            for key in sorted(candidate_resources.keys() & base_resources.keys())
            if _resource_changed(base_resources[key], candidate_resources[key])
        ]
        return WorkspaceRevisionDiffSchema(
            base_revision_id=base.revision_id,
            revision_id=candidate.revision_id,
            manifest_changed=base.manifest_sha256 != candidate.manifest_sha256,
            source_bundle_changed=base.source_bundle_sha256 != candidate.source_bundle_sha256,
            source_commit_changed=base.source_commit_sha != candidate.source_commit_sha,
            added=[_resource_schema(row) for row in added],
            removed=[_resource_schema(row) for row in removed],
            changed=[_resource_schema(row) for row in changed],
        )


def _create_snapshot_sync(
    factory: _Factory,
    *,
    project_id: str,
    actor: str,
    identity: CaliberIdentity,
    project_action: str,
    payload: WorkspaceRevisionSnapshotRequest,
    adapter_registry: WorkspaceResourceAdapterRegistry,
) -> WorkspaceRevisionSchema:
    """Pin an explicit list of live CALIBER resource versions into a new,
    immutable, source-less ("managed") revision (`P4-B`/`P4-C`).

    Unlike ``_create_import_sync`` (which pins resources by reading an
    already-validated Git source snapshot), this resolves and snapshots each
    requested pin *live*, through the registered
    :class:`~caliber.workspace_release_adapters.WorkspaceResourceAdapter` for
    its ``resource_type`` -- failing closed (409) for any resource type with
    no registered adapter, or whose adapter's ``snapshot()`` does not yet
    return a :class:`~caliber.workspace_release_adapters.SnapshotPin` (see
    that type's docstring: only ``prompt`` does today).

    Idempotency is content-derived rather than header-based: the resource
    list's own digest becomes ``revision_sha256``, so a byte-identical retry
    naturally converges on the already-created revision via
    ``uq_workspace_revision_digest`` (project_id, revision_sha256) instead of
    creating a duplicate -- the same "second attempt is idempotent" property
    ``workspace_source.assert_source_commit_digest`` gives the import path,
    without needing a separate ``Idempotency-Key`` header here (this
    request's JSON body *is* the content whose digest is being deduplicated,
    unlike the multipart import route's arbitrary uploaded bytes). A
    concurrent request racing to create the same content is caught by
    ``begin_nested()``/``IntegrityError`` (mirrors
    ``routes/workflows.py``'s benchmark-report create) and replayed from the
    winner's committed row rather than erroring.
    """
    with factory() as session:
        project, _decision = require_project_access(session, identity, project_id, project_action)
        if project.status != "active":
            raise HTTPException(status_code=409, detail="archived_workspace")

        seen_keys: set[tuple[str, str]] = set()
        prepared: list[tuple[WorkspaceRevisionSnapshotResourceRequest, SnapshotPin]] = []
        for entry in payload.resources:
            logical_name = entry.logical_name or entry.resource_id
            key = (entry.resource_type, logical_name)
            if key in seen_keys:
                raise HTTPException(
                    status_code=400,
                    detail=(
                        f"duplicate resource pin for resource_type={entry.resource_type!r} "
                        f"logical_name={logical_name!r}"
                    ),
                )
            seen_keys.add(key)
            try:
                adapter = adapter_registry.require(entry.resource_type)
            except WorkspaceReleaseAdapterUnavailableError as exc:
                raise HTTPException(
                    status_code=409, detail=f"resource_type_adapter_unavailable: {exc}"
                ) from exc
            declaration = {"resource_id": entry.resource_id, "version_ref": entry.version_ref}
            try:
                resolved = adapter.resolve(session, project, declaration, identity)
            except WorkspaceReleaseAdapterError as exc:
                raise HTTPException(
                    status_code=409, detail=f"resource_resolve_failed: {exc}"
                ) from exc
            try:
                pin = adapter.snapshot(session, resolved)
            except WorkspaceReleaseAdapterError as exc:
                raise HTTPException(
                    status_code=409, detail=f"resource_snapshot_failed: {exc}"
                ) from exc
            if not isinstance(pin, SnapshotPin):
                raise HTTPException(
                    status_code=409,
                    detail=(
                        f"resource_type {entry.resource_type!r} does not support "
                        "managed snapshotting yet"
                    ),
                )
            prepared.append((entry, pin))

        prepared.sort(
            key=lambda item: (item[0].resource_type, item[0].logical_name or item[0].resource_id)
        )

        descriptor = {
            "schema_version": "v1alpha1-managed",
            "project_id": project_id,
            "source_kind": "managed",
            "resources": [
                {
                    "resource_type": entry.resource_type,
                    "logical_name": entry.logical_name or entry.resource_id,
                    "resource_id": pin.resource_id,
                    "version_ref": pin.version_ref,
                    "content_sha256": pin.content_sha256,
                }
                for entry, pin in prepared
            ],
        }
        revision_sha256 = hashlib.sha256(canonical_json(descriptor).encode("utf-8")).hexdigest()

        existing = session.execute(
            select(CaliberWorkspaceRevision).where(
                CaliberWorkspaceRevision.project_id == project_id,
                CaliberWorkspaceRevision.revision_sha256 == revision_sha256,
            )
        ).scalar_one_or_none()
        if existing is not None:
            return _revision_schema(existing, _revision_resources(session, existing.revision_id))

        try:
            with session.begin_nested():
                revision_number = allocate_revision_number(session, project_id)
                revision = CaliberWorkspaceRevision(
                    revision_id=new_workspace_revision_id(),
                    project_id=project_id,
                    revision_number=revision_number,
                    source_id=None,
                    source_commit_sha=None,
                    source_kind="managed",
                    manifest={
                        "schema_version": "v1alpha1-managed",
                        "resource_count": len(prepared),
                    },
                    manifest_sha256=None,
                    source_bundle_sha256=None,
                    source_snapshot_file_id=None,
                    source_attestation="caller_attested",
                    revision_sha256=revision_sha256,
                    created_by=actor,
                )
                session.add(revision)
                session.flush()
                for entry, pin in prepared:
                    session.add(
                        CaliberWorkspaceRevisionResource(
                            resource_pin_id=new_workspace_revision_resource_id(),
                            revision_id=revision.revision_id,
                            resource_type=entry.resource_type,
                            logical_name=entry.logical_name or entry.resource_id,
                            resource_id=pin.resource_id,
                            version_ref=pin.version_ref,
                            content_sha256=pin.content_sha256,
                            source_path=None,
                            source_sha256=None,
                            provider_ref=pin.provider_ref,
                            snapshot_file_id=None,
                            snapshot_sha256=None,
                            purpose=entry.purpose,
                            resolution=dict(pin.resolution),
                        )
                    )
                finalize_revision(
                    session,
                    revision,
                    "ready",
                    validation_report={
                        "strategy": "managed_snapshot",
                        "resource_count": len(prepared),
                    },
                    validated_by=actor,
                )
                session.flush()
        except IntegrityError:
            replay = session.execute(
                select(CaliberWorkspaceRevision).where(
                    CaliberWorkspaceRevision.project_id == project_id,
                    CaliberWorkspaceRevision.revision_sha256 == revision_sha256,
                )
            ).scalar_one_or_none()
            if replay is None:
                raise HTTPException(
                    status_code=409, detail="workspace_revision_snapshot_conflict"
                ) from None
            return _revision_schema(replay, _revision_resources(session, replay.revision_id))

        audit_record(
            session,
            actor=actor,
            action="snapshot_workspace_revision",
            entity_type="workspace_revision",
            entity_id=revision.revision_id,
            details={
                "project_id": project_id,
                "revision_sha256": revision_sha256,
                "resource_count": len(prepared),
            },
        )
        session.commit()
        return _revision_schema(revision, _revision_resources(session, revision.revision_id))


async def get_source(request: Request) -> JSONResponse:
    project_id = request.path_params["project_id"]
    _require_workspace_header(request, project_id)
    require_user(request)
    identity = resolve_identity(request)
    response = await run_in_threadpool(
        _get_source_sync,
        get_session_factory(request),
        project_id=project_id,
        identity=identity,
        project_action="read",
    )
    etag = response.source.etag if response.source is not None else None
    return JSONResponse(
        {"data": response.model_dump(mode="json")},
        headers={"ETag": _quoted_etag(etag)} if etag else None,
    )


async def put_source(request: Request) -> JSONResponse:
    project_id = request.path_params["project_id"]
    _require_workspace_header(request, project_id)
    body = await parse_json_object(request)
    payload = WorkspaceSourceConfigureRequest.model_validate(body)
    actor = require_scopes(request, [SCOPE_OPERATOR])
    identity = resolve_identity(request)
    response = await run_in_threadpool(
        _put_source_sync,
        get_session_factory(request),
        project_id=project_id,
        actor=actor,
        identity=identity,
        project_action="source.manage",
        payload=payload,
        if_match=request.headers.get("If-Match"),
    )
    return JSONResponse(
        {"data": response.model_dump(mode="json")},
        headers={"ETag": _quoted_etag(response.source.etag)} if response.source else None,
    )


async def enable_source(request: Request) -> JSONResponse:
    project_id = request.path_params["project_id"]
    _require_workspace_header(request, project_id)
    actor = require_scopes(request, [SCOPE_OPERATOR])
    identity = resolve_identity(request)
    response = await run_in_threadpool(
        _transition_source_sync,
        get_session_factory(request),
        project_id=project_id,
        actor=actor,
        identity=identity,
        project_action="source.manage",
        transition="enable",
        if_match=request.headers.get("If-Match"),
        registry=_provider_registry(request),
    )
    return JSONResponse(
        {"data": response.model_dump(mode="json")},
        headers={"ETag": _quoted_etag(response.source.etag)} if response.source else None,
    )


async def disable_source(request: Request) -> JSONResponse:
    project_id = request.path_params["project_id"]
    _require_workspace_header(request, project_id)
    actor = require_scopes(request, [SCOPE_OPERATOR])
    identity = resolve_identity(request)
    response = await run_in_threadpool(
        _transition_source_sync,
        get_session_factory(request),
        project_id=project_id,
        actor=actor,
        identity=identity,
        project_action="source.manage",
        transition="disable",
        if_match=request.headers.get("If-Match"),
        registry=_provider_registry(request),
    )
    return JSONResponse(
        {"data": response.model_dump(mode="json")},
        headers={"ETag": _quoted_etag(response.source.etag)} if response.source else None,
    )


async def reconcile_source(request: Request) -> JSONResponse:
    project_id = request.path_params["project_id"]
    _require_workspace_header(request, project_id)
    actor = require_scopes(request, [SCOPE_OPERATOR])
    identity = resolve_identity(request)
    response = await run_in_threadpool(
        _transition_source_sync,
        get_session_factory(request),
        project_id=project_id,
        actor=actor,
        identity=identity,
        project_action="source.manage",
        transition="reconcile",
        if_match=request.headers.get("If-Match"),
        registry=_provider_registry(request),
    )
    return JSONResponse(
        {"data": response.model_dump(mode="json")},
        headers={"ETag": _quoted_etag(response.source.etag)} if response.source else None,
    )


async def source_capabilities(request: Request) -> JSONResponse:
    project_id = request.path_params["project_id"]
    _require_workspace_header(request, project_id)
    require_user(request)
    identity = resolve_identity(request)
    response = await run_in_threadpool(
        _source_capabilities_sync,
        get_session_factory(request),
        project_id=project_id,
        identity=identity,
        project_action="read",
        registry=_provider_registry(request),
    )
    return envelope_response(response)


async def create_import(request: Request) -> JSONResponse:
    project_id = request.path_params["project_id"]
    _require_workspace_header(request, project_id)
    actor = require_scopes(request, [SCOPE_OPERATOR])
    identity = resolve_identity(request)
    _require_project_bound_pat_for_import(identity, project_id)
    idempotency_key = request.headers.get("Idempotency-Key", "").strip()
    if not idempotency_key:
        raise HTTPException(status_code=400, detail="idempotency_key_required")
    if len(idempotency_key) > _IDEMPOTENCY_KEY_MAX:
        raise HTTPException(status_code=400, detail="idempotency_key_too_long")
    try:
        form = await request.form()
        upload = form.get("bundle")
        if not isinstance(upload, UploadFile):
            raise HTTPException(status_code=400, detail="multipart_field_bundle_required")
        try:
            bundle = await upload.read()
        finally:
            await upload.close()
        repository = _form_text(form, "repository")
        commit_sha = _form_text(form, "commit_sha")
        assert repository is not None and commit_sha is not None
    except HTTPException:
        raise
    except (OSError, ValueError) as exc:
        raise HTTPException(status_code=400, detail=f"invalid_import_multipart: {exc}") from exc
    if not isinstance(bundle, bytes) or not bundle:
        raise HTTPException(status_code=400, detail="bundle_required")
    if len(commit_sha) > _COMMIT_SHA_MAX:
        raise HTTPException(status_code=400, detail="commit_sha_too_long")
    response = await run_in_threadpool(
        _create_import_sync,
        get_session_factory(request),
        project_id=project_id,
        actor=actor,
        identity=identity,
        project_action="revision.import",
        repository=repository,
        commit_sha=commit_sha,
        idempotency_key=idempotency_key,
        bundle=bundle,
        storage=get_working_dir_service(request),
    )
    return envelope_response(response, status_code=202)


async def list_imports(request: Request) -> JSONResponse:
    project_id = request.path_params["project_id"]
    _require_workspace_header(request, project_id)
    require_user(request)
    identity = resolve_identity(request)
    limit, cursor = _page_params(request, default=_IMPORT_PAGE_DEFAULT, maximum=_IMPORT_PAGE_MAX)
    status = request.query_params.get("status")
    if status is not None and status not in {
        "queued",
        "running",
        "succeeded",
        "failed",
        "reconcile_required",
    }:
        raise HTTPException(status_code=400, detail="invalid_import_status")
    items, next_cursor = await run_in_threadpool(
        _list_imports_sync,
        get_session_factory(request),
        project_id=project_id,
        identity=identity,
        project_action="read",
        limit=limit,
        cursor=cursor,
        status=status,
    )
    return JSONResponse(
        {
            "data": WorkspaceImportJobListSchema(items=items).model_dump(mode="json"),
            "next_cursor": next_cursor,
        }
    )


async def get_import(request: Request) -> JSONResponse:
    project_id = request.path_params["project_id"]
    _require_workspace_header(request, project_id)
    require_user(request)
    identity = resolve_identity(request)
    response = await run_in_threadpool(
        _get_import_sync,
        get_session_factory(request),
        project_id=project_id,
        job_id=request.path_params["job_id"],
        identity=identity,
        project_action="read",
    )
    return envelope_response(response)


async def reconcile_import(request: Request) -> JSONResponse:
    project_id = request.path_params["project_id"]
    _require_workspace_header(request, project_id)
    actor = require_scopes(request, [SCOPE_OPERATOR])
    identity = resolve_identity(request)
    _require_project_bound_pat_for_import(identity, project_id)
    response = await run_in_threadpool(
        _reconcile_import_sync,
        get_session_factory(request),
        project_id=project_id,
        job_id=request.path_params["job_id"],
        actor=actor,
        identity=identity,
        project_action="revision.import",
        storage=get_working_dir_service(request),
    )
    return envelope_response(response)


async def list_revisions(request: Request) -> JSONResponse:
    project_id = request.path_params["project_id"]
    _require_workspace_header(request, project_id)
    require_user(request)
    identity = resolve_identity(request)
    limit, cursor = _page_params(
        request, default=_REVISION_PAGE_DEFAULT, maximum=_REVISION_PAGE_MAX
    )
    status = request.query_params.get("status")
    if status is not None and status not in {"validating", "ready", "invalid"}:
        raise HTTPException(status_code=400, detail="invalid_revision_status")
    items, next_cursor = await run_in_threadpool(
        _list_revisions_sync,
        get_session_factory(request),
        project_id=project_id,
        identity=identity,
        project_action="read",
        limit=limit,
        cursor=cursor,
        status=status,
    )
    return JSONResponse(
        {
            "data": WorkspaceRevisionListSchema(items=items).model_dump(mode="json"),
            "next_cursor": next_cursor,
        }
    )


async def get_revision(request: Request) -> JSONResponse:
    project_id = request.path_params["project_id"]
    _require_workspace_header(request, project_id)
    require_user(request)
    identity = resolve_identity(request)
    response = await run_in_threadpool(
        _get_revision_sync,
        get_session_factory(request),
        project_id=project_id,
        revision_id=request.path_params["revision_id"],
        identity=identity,
        project_action="read",
    )
    return envelope_response(response)


async def diff_revisions(request: Request) -> JSONResponse:
    project_id = request.path_params["project_id"]
    _require_workspace_header(request, project_id)
    require_user(request)
    identity = resolve_identity(request)
    base_revision_id = request.query_params.get("base")
    if not base_revision_id:
        raise HTTPException(status_code=400, detail="base_revision_required")
    response = await run_in_threadpool(
        _diff_revisions_sync,
        get_session_factory(request),
        project_id=project_id,
        revision_id=request.path_params["revision_id"],
        base_revision_id=base_revision_id,
        identity=identity,
        project_action="read",
    )
    return envelope_response(response)


async def snapshot_revision(request: Request) -> JSONResponse:
    project_id = request.path_params["project_id"]
    _require_workspace_header(request, project_id)
    actor = require_scopes(request, [SCOPE_OPERATOR])
    identity = resolve_identity(request)
    body = await parse_json_object(request)
    payload = WorkspaceRevisionSnapshotRequest.model_validate(body)
    response = await run_in_threadpool(
        _create_snapshot_sync,
        get_session_factory(request),
        project_id=project_id,
        actor=actor,
        identity=identity,
        project_action="revision.create",
        payload=payload,
        adapter_registry=_resource_adapter_registry(request),
    )
    return envelope_response(response, status_code=201)


def register(app: Starlette) -> None:
    for endpoint in (put_source, enable_source, disable_source, reconcile_source):
        endpoint.__caliber_openapi_responses__ = {"412": _ETAG_RESPONSE}  # type: ignore[attr-defined]
    app.routes.append(Route(SOURCE_PATH, get_source, methods=["GET"]))
    app.routes.append(Route(SOURCE_PATH, put_source, methods=["PUT"]))
    app.routes.append(Route(SOURCE_ENABLE_PATH, enable_source, methods=["POST"]))
    app.routes.append(Route(SOURCE_DISABLE_PATH, disable_source, methods=["POST"]))
    app.routes.append(Route(SOURCE_CAPABILITIES_PATH, source_capabilities, methods=["GET"]))
    app.routes.append(Route(SOURCE_RECONCILE_PATH, reconcile_source, methods=["POST"]))
    app.routes.append(Route(IMPORTS_PATH, create_import, methods=["POST"]))
    app.routes.append(Route(IMPORTS_PATH, list_imports, methods=["GET"]))
    app.routes.append(Route(IMPORT_DETAIL_PATH, get_import, methods=["GET"]))
    app.routes.append(Route(IMPORT_RECONCILE_PATH, reconcile_import, methods=["POST"]))
    app.routes.append(Route(REVISIONS_PATH, list_revisions, methods=["GET"]))
    app.routes.append(Route(REVISION_DETAIL_PATH, get_revision, methods=["GET"]))
    app.routes.append(Route(REVISION_DIFF_PATH, diff_revisions, methods=["GET"]))
    app.routes.append(Route(REVISIONS_SNAPSHOT_PATH, snapshot_revision, methods=["POST"]))
