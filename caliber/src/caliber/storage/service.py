"""WorkingDirectoryService — the run/playground-scoped file API (storage doc §3.3).

Storage backends are low-level. Workflow code, routes, and tools go through this
service, which:

* computes run/playground namespaces (storage doc §2.1),
* validates uploads (extension deny-list, allow-list media types, size +
  per-run quotas — storage doc §8.4),
* writes through the backend and records the canonical ``CaliberFileRecord``
  (DB row in ``caliber_workflow_files``),
* enforces the ref↔context run binding so a tool can't reach another run
  (storage doc §8.1),
* resolves ``caliber://`` refs to physical files.

The service holds the backend + config; DB-touching methods take a ``Session`` so
they compose with the route handlers' ``with factory() as session`` blocks.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field, replace
from datetime import datetime
from pathlib import PurePosixPath
from typing import Any, BinaryIO
from uuid import uuid4

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from caliber.config import RetentionConfig, WorkflowStorageConfig
from caliber.db.models import (
    CaliberEvalDatasetFile,
    CaliberWorkflowFile,
    CaliberWorkflowFileEvent,
)
from caliber.ids import new_workflow_file_id
from caliber.storage.base import (
    DATASET_KINDS,
    KIND_TO_SEGMENT,
    FileKind,
    StorageBackend,
    StorageConflictError,
    StorageError,
    StorageNotFoundError,
    StoragePermissionError,
    StorageValidationError,
    build_key,
    build_ref,
    parse_ref,
    safe_relative_path,
    sniff_media_type,
)


@dataclass(frozen=True)
class WorkingDirectoryContext:
    """Per-run handle passed to the runtime/tools (storage doc §4.2)."""

    tenant_id: str
    project_id: str
    run_kind: str  # "workflow" | "playground"
    run_id: str
    workflow_id: str | None
    root_prefix: str
    root_ref: str
    input_prefix: str
    work_prefix: str
    artifact_prefix: str
    log_prefix: str
    metadata_prefix: str
    tmp_prefix: str


@dataclass(frozen=True)
class CaliberFileRecord:
    """Canonical file metadata object (storage doc §0.1 rule 4).

    The API responses and the §2.4 ref JSON are projections of this. Mirrors the
    ``caliber_workflow_files`` row.
    """

    file_id: str
    file_ref: str
    relative_path: str
    kind: str
    name: str
    media_type: str | None
    size_bytes: int
    sha256: str | None
    etag: str | None
    object_version_id: str | None
    version: int
    status: str
    storage_backend: str
    workflow_run_id: str | None
    playground_run_id: str | None
    producer_node_id: str | None
    created_at: datetime | None
    updated_at: datetime | None
    metadata: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_row(cls, row: CaliberWorkflowFile) -> CaliberFileRecord:
        return cls(
            file_id=row.file_id,
            file_ref=row.file_ref,
            relative_path=row.relative_path,
            kind=row.kind,
            name=row.name,
            media_type=row.media_type,
            size_bytes=row.size_bytes,
            sha256=row.sha256,
            etag=row.etag,
            object_version_id=row.object_version_id,
            version=row.version,
            status=row.status,
            storage_backend=row.storage_backend,
            workflow_run_id=row.workflow_run_id,
            playground_run_id=row.playground_run_id,
            producer_node_id=row.producer_node_id,
            created_at=row.created_at,
            updated_at=row.updated_at,
            metadata=dict(row.file_metadata or {}),
        )

    def to_api(self) -> dict[str, Any]:
        """Project to the §4.7 API response shape."""
        payload = {
            "file_id": self.file_id,
            "file_ref": self.file_ref,
            "name": self.name,
            "kind": self.kind,
            "relative_path": self.relative_path,
            "media_type": self.media_type,
            "size_bytes": self.size_bytes,
            "sha256": self.sha256,
            "etag": self.etag,
            "object_version_id": self.object_version_id,
            "version": self.version,
            "status": self.status,
            "storage_backend": self.storage_backend,
            "producer_node_id": self.producer_node_id,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "metadata": self.metadata,
        }
        if self.sha256:
            payload["immutable_ref"] = {
                "file_id": self.file_id,
                "file_ref": self.file_ref,
                "sha256": self.sha256,
                "name": self.name,
                "size_bytes": self.size_bytes,
                "media_type": self.media_type,
                "object_version_id": self.object_version_id,
            }
        return payload


# Visible in list/UI; abandoned/soft-deleted rows are hidden (storage doc §4.7).
VISIBLE_STATUSES: frozenset[str] = frozenset(
    {"uploaded", "scanning", "attached", "processing", "artifact"}
)


class WorkingDirectoryService:
    """Run/playground-scoped file operations over a :class:`StorageBackend`."""

    def __init__(
        self,
        backend: StorageBackend,
        config: WorkflowStorageConfig,
        *,
        backend_cache: dict[str, StorageBackend] | None = None,
    ) -> None:
        self._backend = backend
        self._config = config
        self._backends = backend_cache if backend_cache is not None else {}
        self._backends[backend.name] = backend

    def _config_for_backend(self, backend_name: str) -> WorkflowStorageConfig:
        if backend_name not in {"local", "s3"}:
            raise StorageValidationError(f"unknown storage backend: {backend_name!r}")
        return self._config.model_copy(update={"backend": backend_name})

    def _backend_for_name(self, backend_name: str) -> StorageBackend:
        if backend_name == self._backend.name:
            return self._backend
        cached = self._backends.get(backend_name)
        if cached is not None:
            return cached
        backend = build_backend(self._config_for_backend(backend_name))
        self._backends[backend.name] = backend
        return backend

    def for_backend(self, backend_name: str) -> WorkingDirectoryService:
        """Return a service whose writes target ``backend_name``.

        Read helpers still follow each row's recorded ``storage_backend`` so
        project directories can mix local and MinIO-backed rows safely.
        """
        backend = self._backend_for_name(backend_name)
        return WorkingDirectoryService(
            backend,
            self._config_for_backend(backend_name),
            backend_cache=self._backends,
        )

    def _backend_for_row(self, row: CaliberWorkflowFile) -> StorageBackend:
        return self._backend_for_name(row.storage_backend)

    # ----- namespaces ------------------------------------------------------ #
    def create_run_workspace(
        self,
        *,
        tenant_id: str = "local",
        project_id: str = "default",
        workflow_id: str,
        workflow_run_id: str,
    ) -> WorkingDirectoryContext:
        root = (
            f"tenant/{tenant_id}/project/{project_id}/workflow/{workflow_id}/runs/{workflow_run_id}"
        )
        return self._context(tenant_id, project_id, "workflow", workflow_run_id, workflow_id, root)

    def create_playground_workspace(
        self,
        *,
        tenant_id: str = "local",
        project_id: str = "default",
        playground_run_id: str,
    ) -> WorkingDirectoryContext:
        root = f"tenant/{tenant_id}/project/{project_id}/playground/{playground_run_id}"
        return self._context(tenant_id, project_id, "playground", playground_run_id, None, root)

    def _context(
        self,
        tenant_id: str,
        project_id: str,
        run_kind: str,
        run_id: str,
        workflow_id: str | None,
        root: str,
    ) -> WorkingDirectoryContext:
        resource_type = "workflow-runs" if run_kind == "workflow" else "playground-runs"
        root_ref = f"caliber://{resource_type}/{run_id}/"
        return WorkingDirectoryContext(
            tenant_id=tenant_id,
            project_id=project_id,
            run_kind=run_kind,
            run_id=run_id,
            workflow_id=workflow_id,
            root_prefix=root,
            root_ref=root_ref,
            input_prefix=f"{root}/input",
            work_prefix=f"{root}/work",
            artifact_prefix=f"{root}/artifacts",
            log_prefix=f"{root}/logs",
            metadata_prefix=f"{root}/metadata",
            tmp_prefix=f"{root}/tmp",  # noqa: S108 — namespace segment, not a real /tmp path
        )

    # ----- validation ------------------------------------------------------ #
    def validate_upload(self, filename: str, size_bytes: int, media_type: str | None) -> str:
        """Validate filename/size/type; return the safe relative path."""
        rel = safe_relative_path(filename)
        if size_bytes > self._config.max_upload_bytes:
            raise StorageValidationError(
                f"file exceeds max_upload_bytes ({self._config.max_upload_bytes})"
            )
        ext = PurePosixPath(rel).suffix.lower()
        if ext in {e.lower() for e in self._config.denied_extensions}:
            raise StorageValidationError(f"file extension {ext!r} is denied")
        allowed = self._config.allowed_media_types
        if allowed and media_type is not None and media_type not in allowed:
            raise StorageValidationError(f"media type {media_type!r} is not in allowed_media_types")
        return rel

    def _effective_media_type(self, filename: str, data: bytes, declared: str | None) -> str | None:
        """Server-side content sniff (storage doc §8.4): the sniffed type wins.

        Defeats extension/declared spoofing — a ``.txt`` carrying ``%PDF`` bytes
        is treated as ``application/pdf`` for the allow-list check and stored as
        such. Falls back to the declared type when undetermined.
        """
        if not self._config.sniff_content_type:
            return declared
        return sniff_media_type(data, filename) or declared

    def _check_run_quota(
        self, session: Session, ctx: WorkingDirectoryContext, incoming_bytes: int
    ) -> None:
        # Files are recorded under workflow_run_id OR playground_run_id depending
        # on the run kind (see _write_and_record). Scope the quota to the column
        # matching this run's kind — querying workflow_run_id unconditionally let
        # every playground run bypass both quotas (the count/total came back 0).
        run_col = (
            CaliberWorkflowFile.playground_run_id
            if ctx.run_kind == "playground"
            else CaliberWorkflowFile.workflow_run_id
        )
        total, count = session.execute(
            select(
                func.coalesce(func.sum(CaliberWorkflowFile.size_bytes), 0),
                func.count(CaliberWorkflowFile.file_id),
            ).where(
                run_col == ctx.run_id,
                CaliberWorkflowFile.deleted_at.is_(None),
            )
        ).one()
        if count + 1 > self._config.max_files_per_run:
            raise StorageValidationError(
                f"run {ctx.run_id} exceeds max_files_per_run ({self._config.max_files_per_run})"
            )
        if total + incoming_bytes > self._config.max_run_bytes:
            raise StorageValidationError(
                f"run {ctx.run_id} exceeds max_run_bytes ({self._config.max_run_bytes})"
            )

    # ----- writes ---------------------------------------------------------- #
    def register_upload(
        self,
        session: Session,
        ctx: WorkingDirectoryContext,
        *,
        kind: FileKind,
        filename: str,
        data: bytes,
        media_type: str | None,
        actor: str,
        status: str = "uploaded",
        metadata: dict[str, Any] | None = None,
    ) -> CaliberFileRecord:
        effective_type = self._effective_media_type(filename, data, media_type)
        rel = self.validate_upload(filename, len(data), effective_type)
        self._check_run_quota(session, ctx, len(data))
        return self._write_and_record(
            session,
            ctx,
            kind=kind,
            relative_path=rel,
            data=data,
            media_type=effective_type,
            actor=actor,
            status=status,
            metadata=metadata,
            action="upload_file",
        )

    def write_artifact(
        self,
        session: Session,
        ctx: WorkingDirectoryContext,
        *,
        path: str,
        data: bytes,
        media_type: str | None,
        actor: str,
        node_id: str | None = None,
        tool_name: str | None = None,
        kind: FileKind = "artifact",
        metadata: dict[str, Any] | None = None,
    ) -> CaliberFileRecord:
        rel = safe_relative_path(path)
        if len(data) > self._config.max_upload_bytes:
            raise StorageValidationError("artifact exceeds max_upload_bytes")
        self._check_run_quota(session, ctx, len(data))
        status = "artifact" if kind == "artifact" else "attached"
        return self._write_and_record(
            session,
            ctx,
            kind=kind,
            relative_path=rel,
            data=data,
            media_type=media_type,
            actor=actor,
            status=status,
            node_id=node_id,
            tool_name=tool_name,
            metadata=metadata,
            action="write_file_runtime" if kind != "artifact" else "register_artifact",
        )

    def _write_and_record(
        self,
        session: Session,
        ctx: WorkingDirectoryContext,
        *,
        kind: FileKind,
        relative_path: str,
        data: bytes,
        media_type: str | None,
        actor: str,
        status: str,
        node_id: str | None = None,
        tool_name: str | None = None,
        metadata: dict[str, Any] | None = None,
        action: str,
    ) -> CaliberFileRecord:
        key = build_key(ctx.root_prefix, kind, relative_path)
        meta = self._backend.write_bytes(key, data, media_type=media_type, overwrite=True)
        resource_type = "workflow-runs" if ctx.run_kind == "workflow" else "playground-runs"
        file_ref = build_ref(resource_type, ctx.run_id, kind, relative_path)
        row = CaliberWorkflowFile(
            file_id=new_workflow_file_id(),
            tenant_id=ctx.tenant_id,
            project_id=ctx.project_id,
            workflow_id=ctx.workflow_id,
            workflow_run_id=ctx.run_id if ctx.run_kind == "workflow" else None,
            playground_run_id=ctx.run_id if ctx.run_kind == "playground" else None,
            kind=kind,
            name=PurePosixPath(relative_path).name,
            relative_path=relative_path,
            file_ref=file_ref,
            storage_backend=self._backend.name,
            storage_uri=meta.ref.uri,
            bucket=meta.ref.bucket,
            object_key=key,
            object_version_id=meta.object_version_id,
            media_type=media_type,
            size_bytes=meta.size_bytes,
            sha256=meta.sha256,
            etag=meta.etag,
            status=status,
            producer_node_id=node_id,
            producer_tool_name=tool_name,
            created_by=actor,
            file_metadata=metadata or {},
        )
        session.add(row)
        session.flush()
        self.record_event(
            session,
            action=action,
            actor=actor,
            file_id=row.file_id,
            run_id=ctx.run_id,
            relative_path=relative_path,
            status="ok",
        )
        return CaliberFileRecord.from_row(row)

    # ----- reads / resolution --------------------------------------------- #
    def get_row(self, session: Session, file_id: str) -> CaliberWorkflowFile | None:
        return session.get(CaliberWorkflowFile, file_id)

    def resolve_file_ref(self, session: Session, file_ref: str) -> CaliberWorkflowFile | None:
        parse_ref(file_ref)  # validate grammar / reject traversal
        return session.execute(
            select(CaliberWorkflowFile).where(
                CaliberWorkflowFile.file_ref == file_ref,
                CaliberWorkflowFile.deleted_at.is_(None),
            )
        ).scalar_one_or_none()

    def resolve_scoped_file(
        self,
        session: Session,
        ctx: WorkingDirectoryContext,
        *,
        file_id: str | None = None,
        file_ref: str | None = None,
        allowed_kinds: frozenset[str] | None = None,
        allow_staged_or_dataset_input: bool = False,
    ) -> CaliberWorkflowFile:
        """Resolve a visible row inside one tenant/project/run capability scope."""
        if bool(file_id) == bool(file_ref):
            raise StorageValidationError("exactly one of file_id or file_ref is required")
        row = (
            self.get_row(session, str(file_id))
            if file_id
            else self.resolve_file_ref(session, str(file_ref))
        )
        if row is None or row.deleted_at is not None or row.status not in VISIBLE_STATUSES:
            raise StorageNotFoundError(f"file not found: {file_ref or file_id!r}")
        parsed = parse_ref(row.file_ref)
        if file_ref is not None and row.file_ref != file_ref:
            raise StoragePermissionError("file row does not match the requested reference")
        if row.tenant_id != ctx.tenant_id or row.project_id != ctx.project_id:
            raise StoragePermissionError("file is outside this run's tenant/project")
        if allowed_kinds is not None and row.kind not in allowed_kinds:
            raise StoragePermissionError(f"file kind {row.kind!r} is not allowed in this context")

        in_current_run = (
            parsed.resource_type == "workflow-runs"
            and parsed.resource_id == ctx.run_id
            and row.workflow_run_id == ctx.run_id
        ) or (
            parsed.resource_type == "playground-runs"
            and parsed.resource_id == ctx.run_id
            and row.playground_run_id == ctx.run_id
        )
        in_current_project = (
            parsed.resource_type == "projects"
            and parsed.resource_id == ctx.project_id
            and row.project_id == ctx.project_id
            and row.workflow_run_id is None
            and row.playground_run_id is None
        )
        staged_input = (
            allow_staged_or_dataset_input
            and parsed.resource_type == "workflow-runs"
            and row.workflow_id == "_staging"
            and row.kind == "input"
        )
        dataset_input = (
            allow_staged_or_dataset_input
            and parsed.resource_type == "datasets"
            and row.dataset_id == parsed.resource_id
            and row.example_id == parsed.example_id
            and row.kind == "input"
        )
        if not (in_current_run or in_current_project or staged_input or dataset_input):
            raise StoragePermissionError("file reference is outside this run capability scope")
        return row

    def open_for_tool(
        self,
        session: Session,
        file_ref: str,
        *,
        actor: str,
        ctx: WorkingDirectoryContext | None = None,
        run_id: str | None = None,
    ) -> BinaryIO:
        """Open a run or project file for an agent.

        Run-scoped refs still enforce the ref↔context run binding (§8.1).
        Project/File Directory refs are intentionally shared resources so agents
        can read files staged by users before a workflow run starts.
        """
        parsed = parse_ref(file_ref)
        if ctx is None:
            if run_id is None or parsed.resource_type == "projects":
                raise StoragePermissionError("a scoped run context is required for file access")
            candidate = self.resolve_file_ref(session, file_ref)
            if candidate is None:
                raise StorageNotFoundError(f"file not found for ref {file_ref!r}")
            ctx = self.create_run_workspace(
                tenant_id=candidate.tenant_id,
                project_id=candidate.project_id,
                workflow_id=candidate.workflow_id or "_runtime",
                workflow_run_id=run_id,
            )
        if parsed.resource_type in {"workflow-runs", "playground-runs"} and (
            parsed.resource_id != ctx.run_id
        ):
            self.record_event(
                session,
                action="file_access_denied",
                actor=actor,
                run_id=ctx.run_id,
                relative_path=parsed.relative_path,
                status="deny",
                error=f"ref run {parsed.resource_id} != context run {ctx.run_id}",
            )
            raise StoragePermissionError(f"file ref {file_ref!r} is outside run {ctx.run_id}")
        row = self.resolve_scoped_file(session, ctx, file_ref=file_ref)
        self.record_event(
            session,
            action="read_file_runtime",
            actor=actor,
            file_id=row.file_id,
            run_id=ctx.run_id,
            relative_path=row.relative_path,
            status="ok",
        )
        return self._backend_for_row(row).open_read(row.object_key)

    def read_bytes(
        self, row: CaliberWorkflowFile, *, byte_range: tuple[int, int] | None = None
    ) -> bytes:
        return self._backend_for_row(row).read_bytes(row.object_key, byte_range=byte_range)

    # ----- dataset files (storage doc §7.3) ------------------------------- #
    def register_dataset_file(
        self,
        session: Session,
        *,
        dataset_id: str,
        example_id: str,
        role: str,
        kind: str,
        filename: str,
        data: bytes,
        media_type: str | None,
        actor: str,
        match_spec: dict[str, Any] | None = None,
        tenant_id: str = "local",
        project_id: str = "default",
    ) -> CaliberFileRecord:
        """Store a dataset file's bytes + create its example join row (storage doc §7.3).

        Bytes live in a ``caliber_workflow_files`` row (dataset-scoped); the
        ``caliber_eval_dataset_files`` row records the example role + match spec.
        Resolvable as ``caliber://datasets/{dataset_id}/examples/{example_id}/{kind}/{path}``.
        """
        if kind not in DATASET_KINDS:
            raise StorageValidationError(
                f"dataset kind {kind!r} must be one of {sorted(DATASET_KINDS)}"
            )
        rel = safe_relative_path(filename)
        segment = KIND_TO_SEGMENT.get(kind, kind)
        root = (
            f"tenant/{tenant_id}/project/{project_id}/datasets/{dataset_id}/examples/{example_id}"
        )
        key = f"{root}/{segment}/{rel}"
        meta = self._backend.write_bytes(key, data, media_type=media_type, overwrite=True)
        file_ref = build_ref("datasets", dataset_id, kind, rel, example_id=example_id)
        row = CaliberWorkflowFile(
            file_id=new_workflow_file_id(),
            tenant_id=tenant_id,
            project_id=project_id,
            dataset_id=dataset_id,
            example_id=example_id,
            kind=kind,
            name=PurePosixPath(rel).name,
            relative_path=rel,
            file_ref=file_ref,
            storage_backend=self._backend.name,
            storage_uri=meta.ref.uri,
            bucket=meta.ref.bucket,
            object_key=key,
            object_version_id=meta.object_version_id,
            media_type=media_type,
            size_bytes=meta.size_bytes,
            sha256=meta.sha256,
            etag=meta.etag,
            status="attached",
            created_by=actor,
        )
        session.add(row)
        session.flush()
        session.add(
            CaliberEvalDatasetFile(
                dataset_file_id=f"DF-{uuid4().hex[:12]}",
                dataset_id=dataset_id,
                example_id=example_id,
                role=role,
                kind=kind,
                name=row.name,
                file_id=row.file_id,
                match_spec=match_spec,
            )
        )
        session.flush()
        return CaliberFileRecord.from_row(row)

    # ----- project files (workspaces) ------------------------------------- #
    def register_project_file(
        self,
        session: Session,
        *,
        project_id: str,
        kind: FileKind,
        filename: str,
        data: bytes,
        media_type: str | None,
        actor: str,
        tenant_id: str = "local",
        metadata: dict[str, Any] | None = None,
    ) -> CaliberFileRecord:
        """Store a file under a project workspace (storage doc §2.1).

        Namespace: ``tenant/{tenant_id}/project/{project_id}/files/{segment}/{path}``;
        resolvable as ``caliber://projects/{project_id}/{segment}/{path}``. The row
        carries ``project_id`` with no run binding.
        """
        effective_type = self._effective_media_type(filename, data, media_type)
        rel = self.validate_upload(filename, len(data), effective_type)
        digest = hashlib.sha256(data).hexdigest()
        segment = KIND_TO_SEGMENT.get(kind, kind)
        # A canonical ref is immutable once published into a workflow manifest.
        # Re-uploading identical bytes is idempotent; different bytes receive a
        # content-addressed sibling path instead of overwriting the old object.
        requested_ref = build_ref("projects", project_id, kind, rel)
        existing = session.execute(
            select(CaliberWorkflowFile).where(
                CaliberWorkflowFile.file_ref == requested_ref,
                CaliberWorkflowFile.deleted_at.is_(None),
            )
        ).scalar_one_or_none()
        if existing is not None:
            if existing.sha256 == digest and existing.size_bytes == len(data):
                return CaliberFileRecord.from_row(existing)
            path = PurePosixPath(rel)
            rel = str(path.with_name(f"{path.stem}.sha256-{digest[:12]}{path.suffix}"))
            versioned_ref = build_ref("projects", project_id, kind, rel)
            existing_version = session.execute(
                select(CaliberWorkflowFile).where(
                    CaliberWorkflowFile.file_ref == versioned_ref,
                    CaliberWorkflowFile.deleted_at.is_(None),
                )
            ).scalar_one_or_none()
            if existing_version is not None:
                if existing_version.sha256 == digest and existing_version.size_bytes == len(data):
                    return CaliberFileRecord.from_row(existing_version)
                raise StorageValidationError(
                    "content-addressed project file path has conflicting bytes"
                )
        # Physical objects are content-addressed even when this caller wins the
        # canonical logical ref. Concurrent uploads of different bytes can race
        # on the DB ref, but can never overwrite the object pinned by the winner.
        key = f"tenant/{tenant_id}/project/{project_id}/files/{segment}/.objects/{digest}/{rel}"
        try:
            meta = self._backend.write_bytes(
                key,
                data,
                media_type=effective_type,
                overwrite=False,
            )
        except StorageConflictError:
            stored = self._backend.read_bytes(key)
            if hashlib.sha256(stored).hexdigest() != digest:
                raise StorageValidationError(
                    "content-addressed project object has conflicting bytes"
                ) from None
            meta = replace(
                self._backend.stat(key),
                sha256=digest,
                media_type=effective_type,
            )
        file_ref = build_ref("projects", project_id, kind, rel)
        row = CaliberWorkflowFile(
            file_id=new_workflow_file_id(),
            tenant_id=tenant_id,
            project_id=project_id,
            kind=kind,
            name=PurePosixPath(rel).name,
            relative_path=rel,
            file_ref=file_ref,
            storage_backend=self._backend.name,
            storage_uri=meta.ref.uri,
            bucket=meta.ref.bucket,
            object_key=key,
            object_version_id=meta.object_version_id,
            media_type=effective_type,
            size_bytes=meta.size_bytes,
            sha256=meta.sha256,
            etag=meta.etag,
            status="attached",
            created_by=actor,
            file_metadata=metadata or {},
        )
        session.add(row)
        session.flush()
        self.record_event(
            session,
            action="upload_file",
            actor=actor,
            file_id=row.file_id,
            relative_path=rel,
            status="ok",
        )
        return CaliberFileRecord.from_row(row)

    def create_project_folder(
        self,
        session: Session,
        *,
        project_id: str,
        path: str,
        actor: str,
        tenant_id: str = "local",
    ) -> CaliberFileRecord:
        """Create an empty project folder using a metadata marker object.

        Object stores do not have real directories, and local filesystems should
        share the same semantics. A zero-byte ``metadata/{path}/.caliber-folder``
        marker makes empty folders visible in the DB-backed file directory while
        keeping normal project file refs under their content kind.
        """
        folder = safe_relative_path(path).rstrip("/")
        marker_rel = f"{folder}/.caliber-folder"
        file_ref = build_ref("projects", project_id, "metadata", marker_rel)
        existing = session.execute(
            select(CaliberWorkflowFile).where(
                CaliberWorkflowFile.file_ref == file_ref,
                CaliberWorkflowFile.deleted_at.is_(None),
            )
        ).scalar_one_or_none()
        if existing is not None:
            return CaliberFileRecord.from_row(existing)

        key = f"tenant/{tenant_id}/project/{project_id}/files/metadata/{marker_rel}"
        meta = self._backend.write_bytes(
            key,
            b"",
            media_type="application/x-caliber-directory",
            metadata={"directory_marker": "true", "directory_path": folder},
            overwrite=True,
        )
        row = CaliberWorkflowFile(
            file_id=new_workflow_file_id(),
            tenant_id=tenant_id,
            project_id=project_id,
            kind="metadata",
            name=PurePosixPath(folder).name,
            relative_path=marker_rel,
            file_ref=file_ref,
            storage_backend=self._backend.name,
            storage_uri=meta.ref.uri,
            bucket=meta.ref.bucket,
            object_key=key,
            object_version_id=meta.object_version_id,
            media_type="application/x-caliber-directory",
            size_bytes=0,
            sha256=meta.sha256,
            etag=meta.etag,
            status="attached",
            created_by=actor,
            file_metadata={"directory_marker": True, "directory_path": folder},
        )
        session.add(row)
        session.flush()
        self.record_event(
            session,
            action="create_folder",
            actor=actor,
            file_id=row.file_id,
            relative_path=folder,
            status="ok",
        )
        return CaliberFileRecord.from_row(row)

    # ----- input binding (staging -> run) --------------------------------- #
    def bind_input_file(
        self,
        session: Session,
        ctx: WorkingDirectoryContext,
        source: CaliberWorkflowFile,
        *,
        actor: str,
    ) -> CaliberFileRecord:
        """Copy a staged/dataset file into the run's ``input/`` namespace."""
        data = self._backend_for_row(source).read_bytes(source.object_key)
        return self._write_and_record(
            session,
            ctx,
            kind="input",
            relative_path=source.relative_path if source.kind == "input" else source.name,
            data=data,
            media_type=source.media_type,
            actor=actor,
            status="attached",
            metadata={"bound_from": source.file_id},
            action="upload_file",
        )

    def materialize_input_files(
        self,
        session: Session,
        ctx: WorkingDirectoryContext,
        input_files: list[dict[str, str]],
        *,
        actor: str,
    ) -> list[CaliberFileRecord]:
        """Bind run-request ``input_files`` into the run ``input/`` (storage doc §4.8).

        Each item references a staged upload (``file_id``) or a read-only dataset
        file (``file_ref``). Raises :class:`StorageValidationError` /
        :class:`StorageNotFoundError` for unresolvable references.
        """
        bound: list[CaliberFileRecord] = []
        for item in input_files:
            if item.get("file_id"):
                source = self.resolve_scoped_file(
                    session,
                    ctx,
                    file_id=item["file_id"],
                    allowed_kinds=frozenset({"input"}),
                    allow_staged_or_dataset_input=True,
                )
            elif item.get("file_ref"):
                source = self.resolve_scoped_file(
                    session,
                    ctx,
                    file_ref=item["file_ref"],
                    allowed_kinds=frozenset({"input"}),
                    allow_staged_or_dataset_input=True,
                )
            else:
                raise StorageValidationError("input_files entry needs 'file_id' or 'file_ref'")
            bound.append(self.bind_input_file(session, ctx, source, actor=actor))
        return bound

    def run_file_summary(self, session: Session, run_id: str) -> dict[str, Any]:
        """Build the per-run file summary block for ``CaliberWorkflowRun.summary``.

        Returns ``file_counts`` (by kind) and an ``artifacts`` list (registered
        artifact-kind files), so uploaded inputs and produced artifacts show up in
        run detail (storage doc §4.5).
        """
        rows = (
            session.execute(
                select(CaliberWorkflowFile).where(
                    CaliberWorkflowFile.workflow_run_id == run_id,
                    CaliberWorkflowFile.deleted_at.is_(None),
                )
            )
            .scalars()
            .all()
        )
        counts: dict[str, int] = {}
        artifacts: list[dict[str, Any]] = []
        for row in rows:
            if row.status not in VISIBLE_STATUSES:
                continue
            counts[row.kind] = counts.get(row.kind, 0) + 1
            if row.kind == "artifact":
                artifacts.append(
                    {
                        "file_id": row.file_id,
                        "file_ref": row.file_ref,
                        "name": row.name,
                        "media_type": row.media_type,
                        "size_bytes": row.size_bytes,
                        "sha256": row.sha256,
                        "producer_node_id": row.producer_node_id,
                    }
                )
        return {"file_counts": counts, "artifacts": artifacts}

    def cleanup_run_files(
        self,
        session: Session,
        run_id: str,
        *,
        actor: str = "@janitor",
        kinds: tuple[str, ...] | None = None,
    ) -> int:
        """Soft-delete + physically remove a run's files (retention, storage doc §2.6).

        ``kinds`` optionally restricts cleanup (e.g. ``("tmp",)`` for early tmp
        cleanup). Promoted/eval-golden files are protected by the caller's policy,
        not here. Returns the number of files removed. Cleanup failures are recorded
        but never raise (a janitor must not crash on one bad object).
        """
        stmt = select(CaliberWorkflowFile).where(
            CaliberWorkflowFile.workflow_run_id == run_id,
            CaliberWorkflowFile.deleted_at.is_(None),
        )
        if kinds:
            stmt = stmt.where(CaliberWorkflowFile.kind.in_(kinds))
        removed = 0
        for row in session.execute(stmt).scalars().all():
            try:
                self._backend_for_row(row).delete(row.object_key, hard=True)
            except StorageError as exc:
                self.record_event(
                    session,
                    action="cleanup_files",
                    actor=actor,
                    file_id=row.file_id,
                    run_id=run_id,
                    relative_path=row.relative_path,
                    status="error",
                    error=str(exc),
                )
                continue
            row.status = "deleted"
            row.deleted_at = func.now()
            self.record_event(
                session,
                action="delete_file",
                actor=actor,
                file_id=row.file_id,
                run_id=run_id,
                relative_path=row.relative_path,
                status="ok",
            )
            removed += 1
        return removed

    # ----- events ---------------------------------------------------------- #
    def record_event(
        self,
        session: Session,
        *,
        action: str,
        actor: str,
        status: str,
        file_id: str | None = None,
        run_id: str | None = None,
        relative_path: str | None = None,
        error: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        session.add(
            CaliberWorkflowFileEvent(
                event_id=f"FEV-{uuid4().hex[:12]}",
                file_id=file_id,
                workflow_run_id=run_id,
                actor=actor,
                action=action,
                relative_path=relative_path,
                status=status,
                error=error,
                event_metadata=metadata or {},
            )
        )


def retention_days_for(
    status: str,
    retention: RetentionConfig,
    *,
    is_preview: bool = False,
    is_eval: bool = False,
) -> int:
    """Days a finalized run's files are retained, by status/kind (storage doc §2.6)."""
    if is_eval:
        return retention.eval_run_days
    if is_preview:
        return retention.preview_run_days
    if status in {"error", "blocked", "failed"}:
        return retention.failed_run_days
    return retention.default_run_days


def build_backend(config: WorkflowStorageConfig) -> StorageBackend:
    """Construct the configured storage backend (storage doc §3.5)."""
    # Lazy backend import: the local backend is the Phase 1 MVP. The s3/MinIO
    # backend (Phase 2) plugs in here without changing any call site.
    if config.backend == "local":
        from caliber.storage.local import LocalStorageBackend  # noqa: PLC0415

        return LocalStorageBackend(config.base_uri)
    if config.backend == "s3":
        try:
            from caliber.storage.s3 import S3StorageBackend  # noqa: PLC0415
        except ImportError as exc:  # boto3 not installed
            raise StorageValidationError(
                "s3 backend requires boto3; install with `pip install caliber[s3]`"
            ) from exc
        return S3StorageBackend(config)
    raise StorageValidationError(f"unknown storage backend: {config.backend!r}")
