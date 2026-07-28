"""Built-in run-scoped file tools for agents (storage doc §4.4).

These are the callables an agent invokes to read/write files inside its run
working directory. They are deliberately thin wrappers over
:class:`caliber.storage.WorkingDirectoryService`, taking an explicit
``(service, session, ctx)`` so they are pure and unit-testable without the full
executor. Writes are classified ``side_effect_level="write"`` so the preview
sandbox mocks them (storage doc §4.4); reads/list/metadata are read-only.

All file references are validated through the service's ref grammar and the
ref↔context run binding (storage doc §8.1), so a tool can never reach another
run's files. Paths are prefix-excluded relative paths (storage doc §0.1 rule 3).
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import tempfile
from collections.abc import Callable, Iterable
from pathlib import Path, PurePosixPath
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from caliber.config import WorkflowStorageConfig
from caliber.db.models import CaliberProject, CaliberWorkflowFile
from caliber.storage import VISIBLE_STATUSES, FileKind, StorageError
from caliber.storage.base import parse_ref
from caliber.storage.service import (
    CaliberFileRecord,
    WorkingDirectoryContext,
    WorkingDirectoryService,
    build_backend,
)
from caliber.workflows.ir import IRAgent, IRTool, IRWorkflow

# Side-effect levels for the sandbox/preview gate (storage doc §4.4).
FILE_TOOL_SIDE_EFFECTS: dict[str, str] = {
    "list_workdir_files": "read",
    "read_workdir_file": "read",
    "get_file_metadata": "read",
    "write_workdir_file": "write",
    "create_artifact": "write",
    "copy_workdir_file": "write",
    "delete_workdir_file": "write",
}

# Default per-call read cap, mirroring IRFileInput.max_bytes (storage doc §11.3a).
DEFAULT_READ_CAP = 200_000


class ScopedManagedFileResolver:
    """Resolve only content-pinned file refs declared by the compiled workflow.

    The resolver is bound to one run/project and one immutable manifest snapshot.
    A caller cannot substitute an arbitrary ``caliber://`` URI: the logical ref,
    DB row id, stored digest, byte length, and actual byte digest must all match.
    """

    def __init__(
        self,
        service: WorkingDirectoryService,
        session: Session,
        ctx: WorkingDirectoryContext,
        snapshots: list[Any],
    ) -> None:
        self._service = service
        self._session = session
        self._ctx = ctx
        self._snapshots: dict[str, Any] = {}
        for item in snapshots:
            file_ref = str(item.file_ref)
            existing = self._snapshots.get(file_ref)
            if existing is not None and any(
                getattr(existing, field_name, None) != getattr(item, field_name, None)
                for field_name in ("file_id", "sha256", "size_bytes", "object_version_id")
            ):
                raise ValueError(
                    "workflow declares conflicting immutable snapshots for one file ref"
                )
            self._snapshots[file_ref] = item
        self._verified_cache: dict[str, tuple[bytes, CaliberWorkflowFile, Any]] = {}

    def _snapshot_for(self, value: Any) -> Any:
        if isinstance(value, str):
            file_ref = value
        elif isinstance(value, dict):
            file_ref = str(value.get("file_ref") or "")
        else:
            file_ref = str(getattr(value, "file_ref", "") or "")
        snapshot = self._snapshots.get(file_ref)
        if snapshot is None:
            raise ValueError("managed file ref is not declared by this workflow version")
        if not isinstance(value, str):
            for field_name in ("file_id", "sha256", "size_bytes", "object_version_id"):
                supplied = (
                    value.get(field_name)
                    if isinstance(value, dict)
                    else getattr(value, field_name, None)
                )
                if supplied is not None and supplied != getattr(snapshot, field_name):
                    raise ValueError(f"managed file {field_name} does not match the pinned ref")
        return snapshot

    def _verified_bytes(  # noqa: PLR0912 - fail-closed pin and scope checks
        self, value: Any
    ) -> tuple[bytes, CaliberWorkflowFile, Any]:
        snapshot = self._snapshot_for(value)
        cached = self._verified_cache.get(str(snapshot.file_ref))
        if cached is not None:
            return cached
        parsed = parse_ref(snapshot.file_ref)
        try:
            row = self._service.resolve_scoped_file(
                self._session,
                self._ctx,
                file_ref=snapshot.file_ref,
            )
        except StorageError as exc:
            raise ValueError(f"managed file scope validation failed: {exc}") from exc
        if row.file_id != snapshot.file_id:
            raise ValueError("managed file record no longer matches the pinned reference")
        if parsed.resource_type == "projects":
            if parsed.resource_id != self._ctx.project_id or row.project_id != self._ctx.project_id:
                raise ValueError("managed project file is outside this workflow's project")
        elif parsed.resource_type in {"workflow-runs", "playground-runs"}:
            bound_run_id = (
                row.playground_run_id
                if parsed.resource_type == "playground-runs"
                else row.workflow_run_id
            )
            if parsed.resource_id != self._ctx.run_id or bound_run_id != self._ctx.run_id:
                raise ValueError("managed run file is outside this workflow run")
        else:
            raise ValueError(
                f"managed file resource type {parsed.resource_type!r} is not supported here"
            )
        if row.sha256 is None or not hmac.compare_digest(row.sha256, snapshot.sha256):
            raise ValueError("managed file metadata digest does not match the pinned reference")
        if row.size_bytes != snapshot.size_bytes:
            raise ValueError("managed file size does not match the pinned reference")
        if (
            snapshot.object_version_id is not None
            and row.object_version_id != snapshot.object_version_id
        ):
            raise ValueError("managed file object version does not match the pinned reference")
        data = self._service.read_bytes(row)
        actual_sha256 = hashlib.sha256(data).hexdigest()
        if not hmac.compare_digest(actual_sha256, snapshot.sha256):
            raise ValueError("managed file bytes failed the pinned sha256 check")
        if len(data) != snapshot.size_bytes:
            raise ValueError("managed file bytes failed the pinned size check")
        verified = (data, row, snapshot)
        self._verified_cache[str(snapshot.file_ref)] = verified
        return verified

    def verify_all(self) -> None:
        """Eagerly verify every declared snapshot before a run starts."""
        for snapshot in self._snapshots.values():
            self._verified_bytes(snapshot)

    def read_text(self, snapshot: Any, encoding: str, max_bytes: int) -> tuple[str, dict[str, Any]]:
        data, row, pinned = self._verified_bytes(snapshot)
        truncated = len(data) > max_bytes
        raw = data[:max_bytes]
        chosen_encoding = encoding or "utf-8"
        return raw.decode(chosen_encoding, errors="replace"), {
            "file_id": row.file_id,
            "file_ref": row.file_ref,
            "name": row.name,
            "media_type": row.media_type,
            "bytes": len(raw),
            "size_bytes": len(data),
            "sha256": pinned.sha256,
            "truncated": truncated,
            "encoding": chosen_encoding,
            "immutable": True,
        }

    def extract_document(
        self, ref: Any, *, max_chars: int = 200_000, ocr: str = "auto"
    ) -> dict[str, Any]:
        """Run the shipped parser over verified bytes in a private temp file."""
        from caliber.workflows.ingestion_tools import extract_document  # noqa: PLC0415

        data, row, pinned = self._verified_bytes(ref)
        suffix = PurePosixPath(row.name).suffix
        with tempfile.TemporaryDirectory(prefix="caliber-managed-document-") as temp_dir:
            materialized = Path(temp_dir) / f"source{suffix}"
            materialized.write_bytes(data)
            result = extract_document(str(materialized), max_chars=max_chars, ocr=ocr)
        # Do not leak the temporary host path. Preserve source lineage using the
        # canonical ref and its pinned digest instead.
        result["source"] = row.name
        result["file_ref"] = row.file_ref
        result["sha256"] = pinned.sha256
        return result


def bind_managed_file_runtime(
    service: WorkingDirectoryService,
    session: Session,
    ctx: WorkingDirectoryContext,
    snapshots: list[Any],
    *,
    extract_document_aliases: Iterable[str] = (),
) -> tuple[ScopedManagedFileResolver | None, dict[str, Callable[..., Any]]]:
    """Build the managed file-node resolver and secure extractor override."""
    if not snapshots:
        return None, {}
    resolver = ScopedManagedFileResolver(service, session, ctx, snapshots)
    aliases = {"extract_document", *extract_document_aliases}
    return resolver, dict.fromkeys(aliases, resolver.extract_document)


def managed_file_tool_aliases(ir: IRWorkflow) -> set[str]:
    """Find local aliases bound to CALIBER's shipped document extractor."""
    aliases: set[str] = set()
    for node in ir.nodes.values():
        bindings = node.tools if isinstance(node, IRAgent) else []
        if isinstance(node, IRTool) and node.binding is not None:
            bindings = [*bindings, node.binding]
        for binding in bindings:
            if (
                binding.module_path == "caliber.workflows.ingestion_tools"
                and binding.callable_name == "extract_document"
            ):
                aliases.add(binding.local_name)
    return aliases


def bind_project_managed_file_runtime(
    session: Session,
    *,
    storage_config: WorkflowStorageConfig,
    project_id: str | None,
    workflow_id: str,
    runtime_id: str,
    snapshots: list[Any],
    extract_document_aliases: Iterable[str] = (),
) -> tuple[ScopedManagedFileResolver | None, dict[str, Callable[..., Any]]]:
    """Bind and preflight managed project files for a DB-backed execution path.

    Preview and evaluation paths do not create a persisted run workspace, but
    they still have a workflow/project boundary. This helper constructs only a
    scoped context (no files are written), selects the project's configured
    backend, and verifies every pinned object before returning the resolver.
    """
    if not snapshots:
        return None, {}
    if not project_id:
        raise ValueError("managed file inputs require the workflow to belong to a project")
    project = session.get(CaliberProject, project_id)
    if project is None:
        raise ValueError(f"workflow project {project_id!r} no longer exists")
    service = WorkingDirectoryService(build_backend(storage_config), storage_config)
    service = service.for_backend(project.storage_backend)
    ctx = service.create_run_workspace(
        tenant_id=project.tenant_id or "local",
        project_id=project.project_id,
        workflow_id=workflow_id,
        workflow_run_id=runtime_id,
    )
    resolver, tools = bind_managed_file_runtime(
        service,
        session,
        ctx,
        snapshots,
        extract_document_aliases=extract_document_aliases,
    )
    assert resolver is not None  # snapshots is non-empty
    resolver.verify_all()
    return resolver, tools


def list_workdir_files(
    session: Session,
    ctx: WorkingDirectoryContext,
    *,
    kind: str | None = None,
) -> dict[str, Any]:
    """List visible files in the run, optionally filtered by ``kind``."""
    stmt = select(CaliberWorkflowFile).where(
        CaliberWorkflowFile.workflow_run_id == ctx.run_id,
        CaliberWorkflowFile.tenant_id == ctx.tenant_id,
        CaliberWorkflowFile.project_id == ctx.project_id,
        CaliberWorkflowFile.deleted_at.is_(None),
    )
    if kind:
        stmt = stmt.where(CaliberWorkflowFile.kind == kind)
    rows = session.execute(stmt.order_by(CaliberWorkflowFile.created_at.asc())).scalars().all()
    items = [CaliberFileRecord.from_row(r).to_api() for r in rows if r.status in VISIBLE_STATUSES]
    return {"files": items}


def read_workdir_file(
    service: WorkingDirectoryService,
    session: Session,
    ctx: WorkingDirectoryContext,
    *,
    file_ref: str,
    encoding: str | None = "utf-8",
    max_bytes: int = DEFAULT_READ_CAP,
) -> dict[str, Any]:
    """Read a run-scoped file or project directory file (bounded).

    Project refs (``caliber://projects/...``) are intentionally readable so a
    workflow can consume files staged by users in the File Directory page.
    """
    fh = service.open_for_tool(session, file_ref, ctx=ctx, actor="@runtime")
    try:
        data = fh.read(max_bytes + 1)
    finally:
        fh.close()
    truncated = len(data) > max_bytes
    data = data[:max_bytes]
    if encoding:
        try:
            return {
                "file_ref": file_ref,
                "encoding": encoding,
                "content": data.decode(encoding),
                "truncated": truncated,
            }
        except UnicodeDecodeError:
            pass
    return {
        "file_ref": file_ref,
        "encoding": "base64",
        "content_base64": base64.b64encode(data).decode("ascii"),
        "truncated": truncated,
    }


def write_workdir_file(
    service: WorkingDirectoryService,
    session: Session,
    ctx: WorkingDirectoryContext,
    *,
    path: str,
    content: str | None = None,
    content_base64: str | None = None,
    media_type: str | None = None,
    kind: FileKind = "work",
    node_id: str | None = None,
    metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Write a file to ``work/``/``artifacts/``/``logs/`` (prefix-excluded path)."""
    if content is not None:
        data = content.encode("utf-8")
    elif content_base64 is not None:
        data = base64.b64decode(content_base64)
    else:
        raise ValueError("write_workdir_file requires 'content' or 'content_base64'")
    rec = service.write_artifact(
        session,
        ctx,
        path=path,
        data=data,
        media_type=media_type,
        actor="@runtime",
        node_id=node_id,
        tool_name="write_workdir_file",
        kind=kind,
        metadata=metadata,
    )
    return rec.to_api()


def create_artifact(
    service: WorkingDirectoryService,
    session: Session,
    ctx: WorkingDirectoryContext,
    *,
    path: str,
    content: str | None = None,
    content_base64: str | None = None,
    media_type: str | None = None,
    node_id: str | None = None,
    metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Write and register a final artifact in one call (kind=artifact)."""
    return write_workdir_file(
        service,
        session,
        ctx,
        path=path,
        content=content,
        content_base64=content_base64,
        media_type=media_type,
        kind="artifact",
        node_id=node_id,
        metadata=metadata,
    )


def get_file_metadata(
    service: WorkingDirectoryService,
    session: Session,
    ctx: WorkingDirectoryContext,
    *,
    file_ref: str,
) -> dict[str, Any]:
    """Return metadata for a run-scoped file or project directory file."""
    try:
        row = service.resolve_scoped_file(session, ctx, file_ref=file_ref)
    except StorageError as exc:
        raise ValueError(f"file not found for this run or File Directory: {file_ref!r}") from exc
    return CaliberFileRecord.from_row(row).to_api()


def bind_run_read_tools(
    service: WorkingDirectoryService,
    session: Session,
    ctx: WorkingDirectoryContext,
    *,
    read_cap: int = DEFAULT_READ_CAP,
) -> dict[str, Callable[..., Any]]:
    """Return run-scoped READ file tools with the executor's grounding signature.

    The runtime invokes a tool callable as ``fn(input_text)`` (or ``fn()``). These
    wrappers accept that positional arg and operate on the bound run context, so an
    agent that binds ``list_workdir_files`` / ``read_workdir_file`` /
    ``get_file_metadata`` is grounded on the run's files. They are injected into
    ``execute(..., extra_tools=...)`` (storage doc §4.4). Only read tools are
    injected; structured write-tool calls await function-calling support.
    """

    def _list(_arg: object = "") -> dict[str, Any]:
        return list_workdir_files(session, ctx)

    def _read(arg: object = "") -> dict[str, Any]:
        ref = arg if isinstance(arg, str) and arg.startswith("caliber://") else None
        if ref is not None:
            return read_workdir_file(service, session, ctx, file_ref=ref, max_bytes=read_cap)
        # No explicit ref: ground on the run's input files (bounded, concatenated).
        rows = (
            session.execute(
                select(CaliberWorkflowFile).where(
                    CaliberWorkflowFile.workflow_run_id == ctx.run_id,
                    CaliberWorkflowFile.tenant_id == ctx.tenant_id,
                    CaliberWorkflowFile.project_id == ctx.project_id,
                    CaliberWorkflowFile.kind == "input",
                    CaliberWorkflowFile.deleted_at.is_(None),
                )
            )
            .scalars()
            .all()
        )
        chunks: list[dict[str, Any]] = []
        remaining = read_cap
        for row in rows:
            if remaining <= 0:
                break
            data = service.read_bytes(row, byte_range=(0, remaining - 1))
            remaining -= len(data)
            try:
                text = data.decode("utf-8")
            except UnicodeDecodeError:
                text = base64.b64encode(data).decode("ascii")
            chunks.append({"file_ref": row.file_ref, "name": row.name, "content": text})
        return {"input_files": chunks, "count": len(chunks)}

    def _meta(arg: object = "") -> dict[str, Any]:
        if isinstance(arg, str) and arg.startswith("caliber://"):
            return get_file_metadata(service, session, ctx, file_ref=arg)
        return list_workdir_files(session, ctx)

    return {
        "list_workdir_files": _list,
        "read_workdir_file": _read,
        "get_file_metadata": _meta,
    }
