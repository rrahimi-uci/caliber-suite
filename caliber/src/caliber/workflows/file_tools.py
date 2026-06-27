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
from collections.abc import Callable
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from caliber.db.models import CaliberWorkflowFile
from caliber.storage import VISIBLE_STATUSES, FileKind
from caliber.storage.base import parse_ref
from caliber.storage.service import (
    CaliberFileRecord,
    WorkingDirectoryContext,
    WorkingDirectoryService,
)

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


def list_workdir_files(
    session: Session,
    ctx: WorkingDirectoryContext,
    *,
    kind: str | None = None,
) -> dict[str, Any]:
    """List visible files in the run, optionally filtered by ``kind``."""
    stmt = select(CaliberWorkflowFile).where(
        CaliberWorkflowFile.workflow_run_id == ctx.run_id,
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
    fh = service.open_for_tool(session, file_ref, run_id=ctx.run_id, actor="@runtime")
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
    parsed = parse_ref(file_ref)
    row = service.resolve_file_ref(session, file_ref)
    if row is None or (parsed.resource_type != "projects" and row.workflow_run_id != ctx.run_id):
        raise ValueError(f"file not found for this run or File Directory: {file_ref!r}")
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
