"""Safe archive (zip) inspection — zip-slip + zip-bomb defenses (storage doc §8.2).

Extraction is **not** a storage-layer concern and must never auto-run on upload.
When a tool or agent needs to extract an uploaded archive it routes the bytes
through :func:`safe_zip_members` first, which validates every entry against the
same path rules as the rest of the storage layer and enforces decompressed-size,
entry-count, and compression-ratio ceilings. Returns the validated member names;
raises :class:`StorageValidationError` on anything unsafe.
"""

from __future__ import annotations

import io
import zipfile
from dataclasses import dataclass

from caliber.storage.base import StorageValidationError, safe_relative_path

# Conservative defaults; callers override from WorkflowStorageConfig where relevant.
DEFAULT_MAX_ENTRIES = 1000
DEFAULT_MAX_TOTAL_BYTES = 1024 * 1024 * 1024  # 1 GiB decompressed
DEFAULT_MAX_RATIO = 100  # decompressed:compressed per entry
_S_IFLNK = 0xA000  # POSIX symlink mode bits (stat.S_IFLNK)


@dataclass(frozen=True)
class ArchiveMember:
    """A validated archive entry."""

    name: str  # normalized, prefix-safe relative path
    size: int  # decompressed size
    compress_size: int


def safe_zip_members(
    data: bytes,
    *,
    max_entries: int = DEFAULT_MAX_ENTRIES,
    max_total_bytes: int = DEFAULT_MAX_TOTAL_BYTES,
    max_ratio: int = DEFAULT_MAX_RATIO,
) -> list[ArchiveMember]:
    """Validate a zip's entries; return safe members or raise.

    Rejects: non-zip data, traversal/absolute entry names (zip-slip), symlink
    members, too many entries, excessive total decompressed size, and per-entry
    compression ratios above ``max_ratio`` (zip-bomb).
    """
    try:
        zf = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile as exc:
        raise StorageValidationError(f"not a valid zip archive: {exc}") from exc

    infos = zf.infolist()
    if len(infos) > max_entries:
        raise StorageValidationError(f"archive has {len(infos)} entries (max {max_entries})")

    members: list[ArchiveMember] = []
    total = 0
    for info in infos:
        if info.is_dir():
            continue
        # Symlink members carry the symlink bit in the high 16 bits of external_attr.
        mode = (info.external_attr >> 16) & 0xFFFF
        if mode & _S_IFLNK == _S_IFLNK:
            raise StorageValidationError(f"archive contains a symlink entry: {info.filename!r}")
        # Zip-slip: every entry name must be a safe relative path.
        try:
            name = safe_relative_path(info.filename)
        except StorageValidationError as exc:
            raise StorageValidationError(f"unsafe archive entry {info.filename!r}: {exc}") from exc
        total += info.file_size
        if total > max_total_bytes:
            raise StorageValidationError(
                f"archive decompresses to more than {max_total_bytes} bytes (zip bomb)"
            )
        if info.compress_size > 0 and info.file_size / info.compress_size > max_ratio:
            raise StorageValidationError(
                f"archive entry {info.filename!r} exceeds compression ratio {max_ratio} (zip bomb)"
            )
        members.append(
            ArchiveMember(name=name, size=info.file_size, compress_size=info.compress_size)
        )
    return members
