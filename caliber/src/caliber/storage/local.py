"""Local filesystem :class:`StorageBackend` (doc §3.7, Phase 1 MVP).

Design points enforced here:

* **Atomic writes** — write to a temp file in the same directory, then
  ``os.replace`` onto the final key, so a reader never sees a half-written file.
* **Path containment** — every key is realpath-resolved and asserted to stay
  under the backend root (defeats ``..`` *and* symlink escape, doc §8.2). The
  final component is opened ``O_NOFOLLOW`` so a symlinked file can't be followed.
* **Metadata lives in the DB** — the backend returns size/mtime and the sha256
  it computes on write. ``media_type``/custom metadata are owned by the
  ``caliber_workflow_files`` row, not inferred from disk (doc §3.1).

Signed URLs are an S3/MinIO feature; on the local backend they raise
:class:`StorageValidationError` and callers fall back to the download proxy
(doc §4.7, §8.3).
"""

from __future__ import annotations

import contextlib
import hashlib
import os
import re
from datetime import datetime, timezone
from typing import BinaryIO, Literal
from urllib.parse import urlparse

from caliber.storage.base import (
    FileObjectMetadata,
    SignedUrl,
    StorageBackendName,
    StorageConflictError,
    StorageNotFoundError,
    StorageObjectRef,
    StorageValidationError,
    local_realpath_guard,
)

_CHUNK = 1024 * 1024

#: Atomic writes stage to ``<name>.tmp-<pid>`` then ``os.replace`` onto the final
#: key, so list() must hide those temp stragglers — but ONLY those. Matching a
#: bare ``".tmp-" in fname`` substring also hid legitimate files (e.g.
#: ``report.tmp-data.json``); a trailing ``.tmp-<digits>`` is precise.
_TMP_SUFFIX_RE = re.compile(r"\.tmp-\d+$")


def _root_from_base_uri(base_uri: str) -> str:
    """Resolve a ``file://`` (or bare path) base URI to an absolute directory."""
    if base_uri.startswith("file://"):
        parsed = urlparse(base_uri)
        # file:///abs/path -> path is "/abs/path"; file://./rel -> netloc "."
        path = (
            (parsed.netloc + parsed.path) if parsed.netloc not in ("", "localhost") else parsed.path
        )
        path = path or base_uri[len("file://") :]
    else:
        path = base_uri
    return os.path.abspath(path)


class LocalStorageBackend:
    """Filesystem-backed object store rooted at a single directory."""

    name: StorageBackendName = "local"

    def __init__(self, base_uri: str = "file://./caliber-workspaces") -> None:
        self._root = _root_from_base_uri(base_uri)
        # The root is created LAZILY on first write — every write path goes
        # through ``_abs``, which ``makedirs`` the key's parent (so the root is
        # created when something is actually stored). Reads / list / exists /
        # delete all tolerate a missing root. We deliberately do NOT mkdir here:
        # the backend is constructed during app wiring, health checks, and tests
        # that may only read, and an eager mkdir on a *relative* default
        # ``base_uri`` left stray empty ``caliber-workspaces`` dirs in whatever
        # cwd the process happened to run from.

    # ----- path helpers ---------------------------------------------------- #
    def _abs(self, key: str) -> str:
        if key.startswith("/") or ".." in key.split("/"):
            raise StorageValidationError(f"unsafe storage key: {key!r}")
        candidate = os.path.join(self._root, key)
        # Lexical containment first; realpath guard on the parent dir (the file
        # itself may not exist yet for writes).
        parent = os.path.dirname(candidate)
        os.makedirs(parent, exist_ok=True)
        local_realpath_guard(self._root, parent)
        return candidate

    def _meta_from_disk(self, key: str, abs_path: str, *, sha256: str | None) -> FileObjectMetadata:
        st = os.stat(abs_path)
        ts = datetime.fromtimestamp(st.st_mtime, tz=timezone.utc)
        return FileObjectMetadata(
            ref=StorageObjectRef(backend="local", bucket=None, key=key, uri=f"file://{abs_path}"),
            name=os.path.basename(key),
            kind="work",  # backend is kind-agnostic; the DB row carries the real kind
            size_bytes=st.st_size,
            media_type=None,
            sha256=sha256,
            etag=sha256,
            object_version_id=None,
            created_at=ts,
            updated_at=ts,
        )

    # ----- writes ---------------------------------------------------------- #
    def write_bytes(
        self,
        key: str,
        data: bytes,
        *,
        media_type: str | None = None,
        metadata: dict[str, str] | None = None,
        overwrite: bool = False,
    ) -> FileObjectMetadata:
        abs_path = self._abs(key)
        if not overwrite and os.path.exists(abs_path):
            raise StorageConflictError(f"object already exists: {key!r}")
        digest = hashlib.sha256(data).hexdigest()
        tmp = f"{abs_path}.tmp-{os.getpid()}"
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
        try:
            with os.fdopen(fd, "wb") as fh:
                fh.write(data)
                fh.flush()
                os.fsync(fh.fileno())
        except BaseException:
            _silent_unlink(tmp)
            raise
        os.replace(tmp, abs_path)
        return self._meta_from_disk(key, abs_path, sha256=digest)

    def write_stream(
        self,
        key: str,
        stream: BinaryIO,
        *,
        media_type: str | None = None,
        metadata: dict[str, str] | None = None,
        overwrite: bool = False,
    ) -> FileObjectMetadata:
        abs_path = self._abs(key)
        if not overwrite and os.path.exists(abs_path):
            raise StorageConflictError(f"object already exists: {key!r}")
        hasher = hashlib.sha256()
        tmp = f"{abs_path}.tmp-{os.getpid()}"
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
        try:
            with os.fdopen(fd, "wb") as fh:
                while True:
                    chunk = stream.read(_CHUNK)
                    if not chunk:
                        break
                    if isinstance(chunk, str):  # text stream guard
                        chunk = chunk.encode("utf-8")
                    hasher.update(chunk)
                    fh.write(chunk)
                fh.flush()
                os.fsync(fh.fileno())
        except BaseException:
            _silent_unlink(tmp)
            raise
        os.replace(tmp, abs_path)
        return self._meta_from_disk(key, abs_path, sha256=hasher.hexdigest())

    # ----- reads ----------------------------------------------------------- #
    def read_bytes(self, key: str, *, byte_range: tuple[int, int] | None = None) -> bytes:
        abs_path = self._require(key)
        with open(abs_path, "rb") as fh:
            if byte_range is None:
                return fh.read()
            start, end = byte_range  # inclusive end, HTTP Range semantics
            if start < 0 or end < start:
                raise StorageValidationError(f"invalid byte range: {byte_range!r}")
            fh.seek(start)
            return fh.read(end - start + 1)

    def open_read(self, key: str) -> BinaryIO:
        abs_path = self._require(key)
        return open(abs_path, "rb")

    def exists(self, key: str) -> bool:
        try:
            abs_path = os.path.join(self._root, key)
        except (ValueError, TypeError):
            return False
        return os.path.isfile(abs_path)

    def stat(self, key: str) -> FileObjectMetadata:
        abs_path = self._require(key)
        return self._meta_from_disk(key, abs_path, sha256=None)

    def list(
        self,
        prefix: str,
        *,
        recursive: bool = True,
        limit: int = 1000,
        cursor: str | None = None,
    ) -> tuple[list[FileObjectMetadata], str | None]:
        base = os.path.join(self._root, prefix)
        results: list[FileObjectMetadata] = []
        if not os.path.isdir(base):
            return results, None
        collected: list[str] = []
        if recursive:
            for dirpath, _dirs, files in os.walk(base):
                for fname in files:
                    if _TMP_SUFFIX_RE.search(fname):
                        continue
                    full = os.path.join(dirpath, fname)
                    collected.append(os.path.relpath(full, self._root))
        else:
            for fname in os.listdir(base):
                if _TMP_SUFFIX_RE.search(fname):
                    continue
                full = os.path.join(base, fname)
                if os.path.isfile(full):
                    collected.append(os.path.relpath(full, self._root))
        collected.sort()
        start = 0
        if cursor is not None:
            try:
                start = int(cursor)
            except ValueError:
                start = 0
        window = collected[start : start + limit]
        for rel_key in window:
            results.append(self.stat(rel_key.replace(os.sep, "/")))
        next_cursor = str(start + limit) if start + limit < len(collected) else None
        return results, next_cursor

    # ----- mutations ------------------------------------------------------- #
    def delete(self, key: str, *, hard: bool = False) -> None:
        abs_path = os.path.join(self._root, key)
        if os.path.isfile(abs_path):
            os.remove(abs_path)
        # Soft vs hard delete is a metadata concern; the file is removed either way.

    def copy(self, src_key: str, dst_key: str, *, overwrite: bool = False) -> FileObjectMetadata:
        src = self._require(src_key)
        dst = self._abs(dst_key)
        if not overwrite and os.path.exists(dst):
            raise StorageConflictError(f"object already exists: {dst_key!r}")
        with open(src, "rb") as fh:
            return self.write_stream(dst_key, fh, overwrite=overwrite)

    def move(self, src_key: str, dst_key: str, *, overwrite: bool = False) -> FileObjectMetadata:
        if self._abs(src_key) == self._abs(dst_key):
            # Moving an object onto itself is a no-op — NOT a copy-then-delete,
            # which would delete the only copy and lose the data.
            return self.stat(src_key)
        meta = self.copy(src_key, dst_key, overwrite=overwrite)
        self.delete(src_key)
        return meta

    def signed_url(
        self,
        key: str,
        *,
        method: Literal["GET", "PUT"] = "GET",
        expires_seconds: int = 900,
        media_type: str | None = None,
    ) -> SignedUrl:
        raise StorageValidationError(
            "local backend does not support signed URLs; use the download proxy "
            "(GET .../files/{file_id}/content) or configure the s3 backend"
        )

    # ----- internal -------------------------------------------------------- #
    def _require(self, key: str) -> str:
        if key.startswith("/") or ".." in key.split("/"):
            raise StorageValidationError(f"unsafe storage key: {key!r}")
        abs_path = os.path.join(self._root, key)
        if not os.path.isfile(abs_path):
            raise StorageNotFoundError(f"object not found: {key!r}")
        # Guard against a symlinked path component pointing outside the root.
        local_realpath_guard(self._root, abs_path)
        return abs_path


def _silent_unlink(path: str) -> None:
    with contextlib.suppress(OSError):
        os.remove(path)
