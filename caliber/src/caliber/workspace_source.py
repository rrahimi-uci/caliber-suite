"""Deterministic Workspace source-bundle materialization (P4-B).

This module is deliberately provider-free. It validates an uploaded ZIP,
normalizes it into a repository-relative source tree, computes the canonical
tree digest described by ``docs/workspace-plan.md`` §9.3, and emits a fixed
metadata tar snapshot suitable for content-addressed storage.

It does not write storage objects, create revision rows, call a provider, or
claim an import job. Those side effects belong to the later import service and
worker slices. The database helper only checks the P4-A revision observation
guard before a caller attempts to create a new revision.
"""

from __future__ import annotations

import hashlib
import io
import json
import stat
import tarfile
import zipfile
from dataclasses import dataclass
from typing import Final

from sqlalchemy import select
from sqlalchemy.orm import Session

from caliber.db.models import CaliberWorkspaceRevision
from caliber.storage.archive import safe_zip_members
from caliber.storage.base import StorageValidationError, safe_relative_path, sniff_media_type

# P4-B item 6's ratified source-bundle limits. The raw upload ceiling is about
# transport bytes; the other limits apply after ZIP metadata has been checked.
MAX_SOURCE_BUNDLE_BYTES: Final = 100 * 1024 * 1024
MAX_SOURCE_FILE_COUNT: Final = 10_000
MAX_SOURCE_FILE_BYTES: Final = 25 * 1024 * 1024
MAX_SOURCE_TOTAL_BYTES: Final = 1024 * 1024 * 1024
MAX_SOURCE_DECOMPRESSION_RATIO: Final = 50

# ``mimetypes`` varies with the host's MIME database. A canonical digest needs
# a stable answer, so only use deterministic extension mappings after magic
# bytes have failed to identify a binary format.
_EXTENSION_MEDIA_TYPES: Final[dict[str, str]] = {
    ".cjs": "text/javascript",
    ".css": "text/css",
    ".csv": "text/csv",
    ".html": "text/html",
    ".htm": "text/html",
    ".js": "text/javascript",
    ".json": "application/json",
    ".jsonl": "application/jsonl",
    ".md": "text/markdown",
    ".py": "text/x-python",
    ".toml": "application/toml",
    ".ts": "text/typescript",
    ".txt": "text/plain",
    ".xml": "application/xml",
    ".yaml": "application/yaml",
    ".yml": "application/yaml",
}


class WorkspaceSourceDigestConflictError(RuntimeError):
    """A source/commit was previously observed with different content."""

    code = "source_commit_digest_conflict"


@dataclass(frozen=True)
class WorkspaceSourceEntry:
    """One canonical source-tree file."""

    path: str
    media_type: str
    executable: bool
    size_bytes: int
    sha256: str


@dataclass(frozen=True)
class WorkspaceSourceMaterialization:
    """Validated source metadata plus the deterministic retained snapshot."""

    upload_sha256: str
    source_bundle_sha256: str
    snapshot_sha256: str
    entries: tuple[WorkspaceSourceEntry, ...]
    snapshot_bytes: bytes


def _media_type(data: bytes, path: str) -> str:
    """Return a stable media type for the source-tree digest."""
    detected = sniff_media_type(data)
    if detected is not None:
        return detected
    for suffix, media_type in _EXTENSION_MEDIA_TYPES.items():
        if path.lower().endswith(suffix):
            return media_type
    return "application/octet-stream"


def _validate_limit(name: str, value: int) -> None:
    if value < 1:
        raise ValueError(f"{name} must be positive")


def _canonical_descriptor(entries: tuple[WorkspaceSourceEntry, ...]) -> bytes:
    return json.dumps(
        [
            {
                "executable": entry.executable,
                "media_type": entry.media_type,
                "path": entry.path,
                "sha256": entry.sha256,
                "size_bytes": entry.size_bytes,
            }
            for entry in entries
        ],
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _snapshot_bytes(entries: tuple[WorkspaceSourceEntry, ...], contents: dict[str, bytes]) -> bytes:
    """Build a fixed tar stream without ZIP/container metadata."""
    output = io.BytesIO()
    with tarfile.open(fileobj=output, mode="w", format=tarfile.PAX_FORMAT) as archive:
        for entry in entries:
            content = contents[entry.path]
            info = tarfile.TarInfo(entry.path)
            info.mode = 0o755 if entry.executable else 0o644
            info.mtime = 0
            info.uid = 0
            info.gid = 0
            info.uname = ""
            info.gname = ""
            info.size = len(content)
            archive.addfile(info, io.BytesIO(content))
    return output.getvalue()


def materialize_workspace_source(
    data: bytes,
    *,
    max_bundle_bytes: int = MAX_SOURCE_BUNDLE_BYTES,
    max_entries: int = MAX_SOURCE_FILE_COUNT,
    max_total_bytes: int = MAX_SOURCE_TOTAL_BYTES,
    max_file_bytes: int = MAX_SOURCE_FILE_BYTES,
    max_ratio: int = MAX_SOURCE_DECOMPRESSION_RATIO,
) -> WorkspaceSourceMaterialization:
    """Validate and canonicalize a Workspace ZIP source bundle.

    ``source_bundle_sha256`` is calculated from sorted normalized paths, stable
    media types, executable bits, sizes, and file digests. It is intentionally
    independent of ZIP member order, compression level, timestamps, and other
    container metadata. ``snapshot_bytes`` is a deterministic tar stream of
    the same files and can be persisted under that canonical digest by a later
    storage-aware import service.
    """
    for name, value in (
        ("max_bundle_bytes", max_bundle_bytes),
        ("max_entries", max_entries),
        ("max_total_bytes", max_total_bytes),
        ("max_file_bytes", max_file_bytes),
        ("max_ratio", max_ratio),
    ):
        _validate_limit(name, value)
    if len(data) > max_bundle_bytes:
        raise StorageValidationError(f"source bundle is {len(data)} bytes (max {max_bundle_bytes})")

    safe_zip_members(
        data,
        max_entries=max_entries,
        max_total_bytes=max_total_bytes,
        max_entry_bytes=max_file_bytes,
        max_ratio=max_ratio,
    )

    contents: dict[str, bytes] = {}
    modes: dict[str, int] = {}
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            for info in archive.infolist():
                if info.is_dir():
                    continue
                path = safe_relative_path(info.filename)
                if path in contents:
                    raise StorageValidationError(
                        f"archive contains duplicate normalized path: {path!r}"
                    )
                try:
                    content = archive.read(info)
                except (OSError, RuntimeError, ValueError, zipfile.BadZipFile) as exc:
                    raise StorageValidationError(
                        f"archive entry {info.filename!r} failed integrity validation: {exc}"
                    ) from exc
                if len(content) != info.file_size:
                    raise StorageValidationError(
                        f"archive entry {info.filename!r} size changed while reading"
                    )
                contents[path] = content
                modes[path] = (info.external_attr >> 16) & 0xFFFF
    except zipfile.BadZipFile as exc:
        raise StorageValidationError(f"not a valid zip archive: {exc}") from exc

    entries = tuple(
        WorkspaceSourceEntry(
            path=path,
            media_type=_media_type(contents[path], path),
            executable=bool(modes[path] & (stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)),
            size_bytes=len(contents[path]),
            sha256=hashlib.sha256(contents[path]).hexdigest(),
        )
        for path in sorted(contents)
    )
    descriptor = _canonical_descriptor(entries)
    snapshot = _snapshot_bytes(entries, contents)
    return WorkspaceSourceMaterialization(
        upload_sha256=hashlib.sha256(data).hexdigest(),
        source_bundle_sha256=hashlib.sha256(descriptor).hexdigest(),
        snapshot_sha256=hashlib.sha256(snapshot).hexdigest(),
        entries=entries,
        snapshot_bytes=snapshot,
    )


def assert_source_commit_digest(
    session: Session,
    source_id: str,
    source_commit_sha: str,
    source_bundle_sha256: str,
) -> str | None:
    """Check the P4-A one-observed-digest rule before creating a revision.

    Returns the existing revision ID for an idempotent same-digest observation;
    returns ``None`` when no observation exists. A conflicting digest fails
    closed with the stable ``source_commit_digest_conflict`` code.
    """
    if not source_id or not source_commit_sha or not source_bundle_sha256:
        raise ValueError("source_id, source_commit_sha, and source_bundle_sha256 are required")
    row = session.execute(
        select(
            CaliberWorkspaceRevision.revision_id,
            CaliberWorkspaceRevision.source_bundle_sha256,
        )
        .where(
            CaliberWorkspaceRevision.source_id == source_id,
            CaliberWorkspaceRevision.source_commit_sha == source_commit_sha,
        )
        .limit(1)
    ).first()
    if row is None:
        return None
    revision_id, existing_digest = row
    if existing_digest != source_bundle_sha256:
        raise WorkspaceSourceDigestConflictError(
            f"{WorkspaceSourceDigestConflictError.code}: source {source_id!r} commit "
            f"{source_commit_sha!r} is bound to {existing_digest!r}, not "
            f"{source_bundle_sha256!r}"
        )
    return str(revision_id)


__all__ = [
    "MAX_SOURCE_BUNDLE_BYTES",
    "MAX_SOURCE_DECOMPRESSION_RATIO",
    "MAX_SOURCE_FILE_BYTES",
    "MAX_SOURCE_FILE_COUNT",
    "MAX_SOURCE_TOTAL_BYTES",
    "WorkspaceSourceDigestConflictError",
    "WorkspaceSourceEntry",
    "WorkspaceSourceMaterialization",
    "assert_source_commit_digest",
    "materialize_workspace_source",
]
