"""Storage abstraction core: types, exceptions, path safety, and ref grammar.

This is the backend-agnostic layer.

Three responsibilities live here:

1. The value types every backend speaks (:class:`StorageObjectRef`,
   :class:`FileObjectMetadata`, :class:`SignedUrl`) plus the canonical
   :data:`FileKind` vocabulary and its :data:`KIND_TO_SEGMENT` mapping
   (doc §0.1 rule 1 — ``kind`` is singular, the URI path segment may be plural).
2. The :class:`StorageBackend` protocol the local and S3 backends implement.
3. The path-safety primitives (:func:`safe_relative_path`, :func:`safe_join`)
   and the ``caliber://`` reference grammar (:func:`build_ref`, :func:`parse_ref`).
   These are shared so every route/tool validates the same way rather than
   re-implementing traversal checks (doc §8.2).
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import PurePosixPath
from typing import BinaryIO, Literal, Protocol, runtime_checkable

StorageBackendName = Literal["local", "s3"]

# Canonical kind vocabulary (doc §0.1 rule 1). Singular everywhere; the URI
# path segment is derived via KIND_TO_SEGMENT, never by concatenating the value.
FileKind = Literal["input", "work", "tmp", "artifact", "log", "metadata"]

FILE_KINDS: frozenset[str] = frozenset({"input", "work", "tmp", "artifact", "log", "metadata"})

KIND_TO_SEGMENT: dict[str, str] = {
    "input": "input",
    "work": "work",
    "tmp": "tmp",
    "artifact": "artifacts",  # plural in the path
    "log": "logs",  # plural in the path
    "metadata": "metadata",
}

SEGMENT_TO_KIND: dict[str, str] = {seg: kind for kind, seg in KIND_TO_SEGMENT.items()}

# Dataset-scoped files use a distinct vocabulary (doc §0.1 rule 2). ``expected``
# matches the URI segment and the caliber_eval_dataset_files.kind column.
DATASET_KINDS: frozenset[str] = frozenset({"input", "expected", "reference", "rubric", "fixture"})

# File lifecycle states (doc §4.6 status domain / §5.5).
FileStatus = Literal[
    "pending_upload",
    "uploaded",
    "scanning",
    "rejected",
    "attached",
    "processing",
    "artifact",
    "deleted",
]

# Scheme + the run-kinds the ref grammar understands (doc §13).
CALIBER_SCHEME = "caliber://"

# Smallest printable byte; anything below is a control character.
_CONTROL_CHAR_MAX = 0x20
# Minimum ``/``-split part counts for each ref shape (doc §13 grammar).
_MIN_REF_PARTS = 2  # scheme body must have at least resource-type + id
_MIN_RUN_REF_PARTS = 4  # {type}/{id}/{segment}/{path}
_MIN_WORKSPACE_REF_PARTS = 3  # workflow-workspaces/{id}/{path}
_MIN_DATASET_REF_PARTS = 6  # datasets/{id}/examples/{ex}/{segment}/{path}
_DATASET_EXAMPLES_IDX = 2  # parts[2] must be the literal "examples"


# --------------------------------------------------------------------------- #
# Exceptions (doc §3.6). Each maps to a stable HTTP status in the routes layer.
# --------------------------------------------------------------------------- #
class StorageError(Exception):
    """Base class for all storage-layer failures."""


class StorageNotFoundError(StorageError):
    """The requested object/key does not exist (HTTP 404)."""


class StorageConflictError(StorageError):
    """Write would overwrite an existing object with ``overwrite=False`` (409)."""


class StoragePermissionError(StorageError):
    """Caller is not permitted to access this object/scope (403)."""


class StorageValidationError(StorageError):
    """Input failed validation: bad path, size, type, or unsupported op (400)."""


class StorageUnavailableError(StorageError):
    """The backend is temporarily unavailable (503)."""


class StorageChecksumMismatchError(StorageError):
    """A post-upload checksum did not match the expected value (422)."""


# --------------------------------------------------------------------------- #
# Value types (doc §3.2).
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class StorageObjectRef:
    """Backend-specific locator for a stored object."""

    backend: StorageBackendName
    bucket: str | None
    key: str
    uri: str


@dataclass(frozen=True)
class FileObjectMetadata:
    """Storage-layer metadata for one object.

    Intentionally has no ``file_id``/``relative_path``/``file_ref`` — those live
    on the DB-backed ``CaliberFileRecord`` (doc §0.1 rule 4). ``object_version_id``
    is the provider version (doc §0.1 rule 5).
    """

    ref: StorageObjectRef
    name: str
    kind: FileKind
    size_bytes: int
    media_type: str | None
    sha256: str | None
    etag: str | None
    object_version_id: str | None
    created_at: datetime
    updated_at: datetime
    custom: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class SignedUrl:
    """A time-boxed URL for direct browser upload/download."""

    url: str
    method: Literal["GET", "PUT"]
    expires_at: datetime
    headers: dict[str, str] = field(default_factory=dict)


# --------------------------------------------------------------------------- #
# Path safety (doc §8.2). Cloned from skill_packages._clean_import_path so every
# user-supplied path validates identically, plus a realpath/symlink guard for
# the local backend.
# --------------------------------------------------------------------------- #
def safe_relative_path(raw: str) -> str:
    """Normalize a user-supplied relative path, rejecting traversal.

    Rejects absolute paths, ``..`` segments, control characters, and empty /
    ``.`` / ``/`` inputs. Returns a clean forward-slash path with no leading
    prefix. Raises :class:`StorageValidationError` on anything unsafe.
    """
    cleaned = raw.replace("\\", "/").strip()
    if not cleaned or cleaned in {".", "/"}:
        raise StorageValidationError(f"unsafe path: {raw!r}")
    if any(ord(ch) < _CONTROL_CHAR_MAX for ch in cleaned):
        raise StorageValidationError(f"path contains control characters: {raw!r}")
    path = PurePosixPath(cleaned)
    if path.is_absolute() or ".." in path.parts:
        raise StorageValidationError(f"unsafe path: {raw!r}")
    parts = [part for part in path.parts if part not in {"", "."}]
    if not parts:
        raise StorageValidationError(f"unsafe path: {raw!r}")
    return "/".join(parts)


_MAGIC_SIGNATURES: tuple[tuple[bytes, str], ...] = (
    (b"%PDF-", "application/pdf"),
    (b"\x89PNG\r\n\x1a\n", "image/png"),
    (b"\xff\xd8\xff", "image/jpeg"),
    (b"GIF87a", "image/gif"),
    (b"GIF89a", "image/gif"),
    (b"PK\x03\x04", "application/zip"),
    (b"PK\x05\x06", "application/zip"),
    (b"\x1f\x8b", "application/gzip"),
    (b"%!PS", "application/postscript"),
    (b"<?xml", "application/xml"),
)


def sniff_media_type(data: bytes, filename: str | None = None) -> str | None:
    """Best-effort server-side content sniff (storage doc §8.4).

    Checks leading magic bytes for common types, then falls back to the
    extension via :mod:`mimetypes`. Returns ``None`` when undetermined. This is
    deliberately conservative — it exists to catch obvious extension/declared
    spoofing, not to be a full file-type oracle.
    """
    head = data[:16]
    for signature, media_type in _MAGIC_SIGNATURES:
        if head.startswith(signature):
            return media_type
    if filename:
        import mimetypes  # noqa: PLC0415 — stdlib, only needed on the fallback path

        guessed, _ = mimetypes.guess_type(filename)
        if guessed:
            return guessed
    return None


def segment_for_kind(kind: str) -> str:
    """Map a :data:`FileKind` to its URI/directory path segment (doc §0.1 rule 1)."""
    try:
        return KIND_TO_SEGMENT[kind]
    except KeyError:
        raise StorageValidationError(f"unknown file kind: {kind!r}") from None


def build_key(root_prefix: str, kind: str, relative_path: str) -> str:
    """Compose a storage key as ``{root}/{segment}/{relative_path}``.

    ``relative_path`` is prefix-excluded (doc §0.1 rule 3); the kind's path
    segment is prepended here. The result is normalized and validated.
    """
    rel = safe_relative_path(relative_path)
    segment = segment_for_kind(kind)
    root = root_prefix.strip("/")
    return f"{root}/{segment}/{rel}" if root else f"{segment}/{rel}"


def local_realpath_guard(root_dir: str, target_path: str) -> str:
    """Resolve ``target_path`` and assert it stays under ``root_dir`` on disk.

    Defeats symlink escape: a ``..``-free key can still leave the namespace if a
    path component is a symlink. Returns the real (resolved) absolute path.
    """
    real_root = os.path.realpath(root_dir)
    real_target = os.path.realpath(target_path)
    if real_target != real_root and not real_target.startswith(real_root + os.sep):
        raise StoragePermissionError(f"resolved path {real_target!r} escapes root {real_root!r}")
    return real_target


# --------------------------------------------------------------------------- #
# caliber:// reference grammar (doc §13).
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class ParsedRef:
    """A decomposed ``caliber://`` reference."""

    resource_type: str  # "workflow-runs" | "playground-runs" | "datasets" | "workflow-workspaces"
    resource_id: str  # run id / workflow id / dataset id
    segment: str | None  # path segment (input/work/artifacts/...) or None for workspaces
    relative_path: str  # prefix-excluded path under the segment
    example_id: str | None = None  # only for datasets


def build_ref(
    resource_type: str,
    resource_id: str,
    kind: str,
    relative_path: str,
    *,
    example_id: str | None = None,
) -> str:
    """Build a grammar-conformant ``caliber://`` ref (doc §13)."""
    rel = safe_relative_path(relative_path)
    segment = segment_for_kind(kind) if kind in KIND_TO_SEGMENT else kind
    if resource_type == "datasets":
        if not example_id:
            raise StorageValidationError("dataset refs require example_id")
        return f"{CALIBER_SCHEME}datasets/{resource_id}/examples/{example_id}/{segment}/{rel}"
    return f"{CALIBER_SCHEME}{resource_type}/{resource_id}/{segment}/{rel}"


def parse_ref(ref: str) -> ParsedRef:
    """Parse and validate a ``caliber://`` reference.

    Validates the segment against the resource type's allowed kinds and rejects
    any traversal in the trailing path (doc §13 validation rules). Raises
    :class:`StorageValidationError` on a malformed ref.
    """
    if not ref.startswith(CALIBER_SCHEME):
        raise StorageValidationError(f"not a caliber ref: {ref!r}")
    body = ref[len(CALIBER_SCHEME) :]
    parts = body.split("/")
    if len(parts) < _MIN_REF_PARTS:
        raise StorageValidationError(f"malformed caliber ref: {ref!r}")
    resource_type = parts[0]

    if resource_type in {"workflow-runs", "playground-runs", "projects"}:
        if len(parts) < _MIN_RUN_REF_PARTS:
            raise StorageValidationError(f"malformed {resource_type} ref: {ref!r}")
        resource_id, segment = parts[1], parts[2]
        rel = "/".join(parts[3:])
        _validate_run_segment(segment, ref)
        return ParsedRef(resource_type, resource_id, segment, safe_relative_path(rel))

    if resource_type == "datasets":
        # caliber://datasets/{dataset_id}/examples/{example_id}/{segment}/{path}
        if len(parts) < _MIN_DATASET_REF_PARTS or parts[_DATASET_EXAMPLES_IDX] != "examples":
            raise StorageValidationError(f"malformed dataset ref: {ref!r}")
        dataset_id, example_id, segment = parts[1], parts[3], parts[4]
        rel = "/".join(parts[5:])
        if segment not in DATASET_KINDS:
            raise StorageValidationError(f"segment {segment!r} not allowed for datasets in {ref!r}")
        return ParsedRef(
            resource_type,
            dataset_id,
            segment,
            safe_relative_path(rel),
            example_id=example_id,
        )

    if resource_type == "workflow-workspaces":
        if len(parts) < _MIN_WORKSPACE_REF_PARTS:
            raise StorageValidationError(f"malformed workspace ref: {ref!r}")
        resource_id = parts[1]
        rel = "/".join(parts[2:])
        return ParsedRef(resource_type, resource_id, None, safe_relative_path(rel))

    raise StorageValidationError(f"unknown resource type {resource_type!r} in {ref!r}")


def _validate_run_segment(segment: str, ref: str) -> None:
    if segment not in SEGMENT_TO_KIND:
        raise StorageValidationError(f"segment {segment!r} not allowed for runs in {ref!r}")


# --------------------------------------------------------------------------- #
# Backend protocol (doc §3.2).
# --------------------------------------------------------------------------- #
@runtime_checkable
class StorageBackend(Protocol):
    """Low-level object store. Local and S3 backends implement this."""

    name: StorageBackendName

    def write_bytes(
        self,
        key: str,
        data: bytes,
        *,
        media_type: str | None = None,
        metadata: dict[str, str] | None = None,
        overwrite: bool = False,
    ) -> FileObjectMetadata: ...

    def write_stream(
        self,
        key: str,
        stream: BinaryIO,
        *,
        media_type: str | None = None,
        metadata: dict[str, str] | None = None,
        overwrite: bool = False,
    ) -> FileObjectMetadata: ...

    def read_bytes(self, key: str, *, byte_range: tuple[int, int] | None = None) -> bytes: ...

    def open_read(self, key: str) -> BinaryIO: ...

    def exists(self, key: str) -> bool: ...

    def stat(self, key: str) -> FileObjectMetadata: ...

    def list(
        self,
        prefix: str,
        *,
        recursive: bool = True,
        limit: int = 1000,
        cursor: str | None = None,
    ) -> tuple[list[FileObjectMetadata], str | None]: ...

    def delete(self, key: str, *, hard: bool = False) -> None: ...

    def copy(
        self, src_key: str, dst_key: str, *, overwrite: bool = False
    ) -> FileObjectMetadata: ...

    def move(
        self, src_key: str, dst_key: str, *, overwrite: bool = False
    ) -> FileObjectMetadata: ...

    def signed_url(
        self,
        key: str,
        *,
        method: Literal["GET", "PUT"] = "GET",
        expires_seconds: int = 900,
        media_type: str | None = None,
    ) -> SignedUrl: ...
