"""CALIBER file/workspace storage subsystem.

The workflow file-workspace storage subsystem. This package
is unrelated to ``caliber.artifact_store`` (the prompt-registry read store);
the ``artifact_store_provider`` config knob is not reused here (storage doc §3.1).

Public surface:

* :class:`StorageBackend` protocol + :class:`LocalStorageBackend` implementation.
* Value types: :class:`StorageObjectRef`, :class:`FileObjectMetadata`,
  :class:`SignedUrl`.
* :class:`WorkingDirectoryService` + :class:`WorkingDirectoryContext` +
  :class:`CaliberFileRecord` — the run/playground-scoped file API.
* Path-safety + ``caliber://`` ref grammar helpers.
* Stable storage exceptions.
"""

from __future__ import annotations

from caliber.storage.archive import ArchiveMember, safe_zip_members
from caliber.storage.base import (
    CALIBER_SCHEME,
    DATASET_KINDS,
    FILE_KINDS,
    KIND_TO_SEGMENT,
    FileKind,
    FileObjectMetadata,
    FileStatus,
    ParsedRef,
    SignedUrl,
    StorageBackend,
    StorageBackendName,
    StorageChecksumMismatchError,
    StorageConflictError,
    StorageError,
    StorageNotFoundError,
    StorageObjectRef,
    StoragePermissionError,
    StorageUnavailableError,
    StorageValidationError,
    build_key,
    build_ref,
    parse_ref,
    safe_relative_path,
    segment_for_kind,
    sniff_media_type,
)
from caliber.storage.compare import SUPPORTED_MATCH_TYPES, compare_artifact
from caliber.storage.local import LocalStorageBackend
from caliber.storage.service import (
    VISIBLE_STATUSES,
    CaliberFileRecord,
    WorkingDirectoryContext,
    WorkingDirectoryService,
    build_backend,
    retention_days_for,
)

__all__ = [
    "CALIBER_SCHEME",
    "DATASET_KINDS",
    "FILE_KINDS",
    "KIND_TO_SEGMENT",
    "SUPPORTED_MATCH_TYPES",
    "VISIBLE_STATUSES",
    "ArchiveMember",
    "CaliberFileRecord",
    "FileKind",
    "FileObjectMetadata",
    "FileStatus",
    "LocalStorageBackend",
    "ParsedRef",
    "SignedUrl",
    "StorageBackend",
    "StorageBackendName",
    "StorageChecksumMismatchError",
    "StorageConflictError",
    "StorageError",
    "StorageNotFoundError",
    "StorageObjectRef",
    "StoragePermissionError",
    "StorageUnavailableError",
    "StorageValidationError",
    "WorkingDirectoryContext",
    "WorkingDirectoryService",
    "build_backend",
    "build_key",
    "build_ref",
    "compare_artifact",
    "parse_ref",
    "retention_days_for",
    "safe_relative_path",
    "safe_zip_members",
    "segment_for_kind",
    "sniff_media_type",
]
