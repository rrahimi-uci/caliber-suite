"""Verify an exported evidence archive without server or storage access.

The verifier is deliberately offline and fail-closed.  It reads ZIP members
through :mod:`zipfile` rather than extracting them, bounds declared and actual
uncompressed sizes, validates the evidence-package contract, and recomputes
the manifest, metadata, and payload digests.  Beta archives are unsigned:
successful verification proves package integrity and linkage, not origin,
authorization, approval, or authenticity.
"""

from __future__ import annotations

import hashlib
import io
import json
import stat
import zipfile
from dataclasses import dataclass

from caliber.contracts import SUPPORTED_VERSIONS, validate_record
from caliber.evidence.package import _canonical_bytes, archive_member_name

_CONTROL_CHAR_LIMIT = 0x20


@dataclass(frozen=True)
class VerificationLimits:
    """Resource limits applied before archive members are materialized."""

    max_archive_bytes: int = 64 * 1024 * 1024
    max_manifest_bytes: int = 1 * 1024 * 1024
    max_member_bytes: int = 16 * 1024 * 1024
    max_total_uncompressed_bytes: int = 64 * 1024 * 1024
    max_entries: int = 512
    max_compression_ratio: int = 1_000


DEFAULT_LIMITS = VerificationLimits()


class EvidenceVerificationError(ValueError):
    """The archive is malformed, unsafe, or fails an integrity check."""

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(message)


@dataclass(frozen=True)
class EvidenceVerification:
    """The bounded result of verifying one archive."""

    archive_sha256: str
    package_sha256: str
    workflow_run_id: str
    member_ids: tuple[str, ...]
    redaction: str
    origin_verified: bool = False

    @property
    def valid(self) -> bool:
        """Always true for a returned result; invalid input raises instead."""

        return True


def _fail(code: str, message: str) -> EvidenceVerificationError:
    return EvidenceVerificationError(code, message)


def _check_limits(limits: VerificationLimits) -> None:
    if (
        limits.max_archive_bytes <= 0
        or limits.max_manifest_bytes <= 0
        or limits.max_member_bytes <= 0
        or limits.max_total_uncompressed_bytes <= 0
        or limits.max_entries <= 0
        or limits.max_compression_ratio <= 0
    ):
        raise _fail("invalid_limits", "verification limits must be positive")
    if limits.max_manifest_bytes > limits.max_member_bytes:
        raise _fail("invalid_limits", "manifest limit exceeds member limit")


def _check_entry(info: zipfile.ZipInfo, limits: VerificationLimits) -> None:
    name = info.filename
    if not name or name.startswith(("/", "\\")) or "\\" in name:
        raise _fail("unsafe_member_path", "archive contains an unsafe member path")
    parts = name.split("/")
    if any(part in {"", ".", ".."} for part in parts) or any(
        ord(char) < _CONTROL_CHAR_LIMIT for char in name
    ):
        raise _fail("unsafe_member_path", "archive contains an unsafe member path")
    if info.is_dir():
        raise _fail("unexpected_directory", "archive contains an unexpected directory")
    mode = (info.external_attr >> 16) & 0xFFFF
    if mode and stat.S_ISLNK(mode):
        raise _fail("symlink_member", "archive contains a symlink member")
    if info.flag_bits & 0x1:
        raise _fail("encrypted_member", "encrypted archive members are not supported")
    if info.compress_type not in {zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED}:
        raise _fail("unsupported_compression", "archive uses unsupported compression")
    if info.file_size < 0 or info.compress_size < 0:
        raise _fail("invalid_member_size", "archive contains an invalid member size")
    member_limit = limits.max_manifest_bytes if name == "manifest.json" else limits.max_member_bytes
    if info.file_size > member_limit:
        raise _fail("member_size_limit", "archive member exceeds its size limit")
    if info.file_size and (
        info.compress_size == 0
        or info.file_size > info.compress_size * limits.max_compression_ratio
    ):
        raise _fail("compression_ratio_limit", "archive member exceeds its compression ratio limit")


def _read_member(archive: zipfile.ZipFile, info: zipfile.ZipInfo) -> bytes:
    try:
        # Reading through the ZipInfo object preserves duplicate-name checks and
        # makes zipfile validate the member CRC before returning success.
        with archive.open(info, mode="r") as stream:
            return stream.read()
    except (OSError, RuntimeError, ValueError, zipfile.BadZipFile) as exc:
        raise _fail("member_read_failed", "archive member could not be read safely") from exc


def verify_archive(  # noqa: PLR0912, PLR0915 - each refusal is an explicit security gate
    archive_bytes: bytes, *, limits: VerificationLimits = DEFAULT_LIMITS
) -> EvidenceVerification:
    """Verify an evidence ZIP and return integrity-only metadata.

    No server, database, object store, or extraction directory is consulted.
    The function never returns a partial success result: every refusal is an
    :class:`EvidenceVerificationError` with a stable, non-sensitive code.
    """

    _check_limits(limits)
    if not isinstance(archive_bytes, bytes):
        raise _fail("invalid_archive_type", "archive must be bytes")
    if len(archive_bytes) > limits.max_archive_bytes:
        raise _fail("archive_size_limit", "archive exceeds its size limit")
    archive_digest = hashlib.sha256(archive_bytes).hexdigest()
    try:
        archive = zipfile.ZipFile(io.BytesIO(archive_bytes), mode="r")
    except (OSError, zipfile.BadZipFile) as exc:
        raise _fail("malformed_zip", "archive is not a valid ZIP") from exc

    with archive:
        infos = archive.infolist()
        if len(infos) > limits.max_entries:
            raise _fail("entry_count_limit", "archive contains too many members")
        names = [info.filename for info in infos]
        if len(names) != len(set(names)):
            raise _fail("duplicate_member", "archive contains duplicate member names")
        for info in infos:
            _check_entry(info, limits)
        total_size = sum(info.file_size for info in infos)
        if total_size > limits.max_total_uncompressed_bytes:
            raise _fail("total_size_limit", "archive exceeds its total uncompressed size limit")
        manifest_info = next((info for info in infos if info.filename == "manifest.json"), None)
        if manifest_info is None:
            raise _fail("missing_manifest", "archive has no manifest")
        manifest_bytes = _read_member(archive, manifest_info)
        try:
            manifest = json.loads(manifest_bytes.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise _fail("invalid_manifest_json", "manifest is not valid JSON") from exc
        if not isinstance(manifest, dict):
            raise _fail("invalid_manifest_shape", "manifest is not an object")
        if manifest_bytes != _canonical_bytes(manifest):
            raise _fail("noncanonical_manifest", "manifest is not canonical JSON")
        try:
            validate_record("evidence_package", manifest)
        except Exception as exc:
            raise _fail("invalid_manifest", "manifest fails the evidence-package contract") from exc

        members = manifest["members"]
        expected_names = {
            "manifest.json",
            *(archive_member_name(member["member_id"]) for member in members),
        }
        if set(names) != expected_names:
            raise _fail("membership_mismatch", "archive members do not match the manifest")
        info_by_name = {info.filename: info for info in infos}
        member_ids: list[str] = []
        for member in members:
            member_id = str(member["member_id"])
            member_ids.append(member_id)
            metadata_digest = hashlib.sha256(_canonical_bytes(member["metadata"])).hexdigest()
            if metadata_digest != member["metadata_sha256"]:
                raise _fail("metadata_digest_mismatch", "member metadata failed its digest check")
            kind = member["kind"]
            declared_version = member["metadata"].get("schema_version")
            if kind not in SUPPORTED_VERSIONS or declared_version not in SUPPORTED_VERSIONS[kind]:
                raise _fail("unsupported_member_schema", "member declares an unsupported schema")
            payload = _read_member(archive, info_by_name[archive_member_name(member_id)])
            if hashlib.sha256(payload).hexdigest() != member["payload_sha256"]:
                raise _fail("payload_digest_mismatch", "member payload failed its digest check")

    return EvidenceVerification(
        archive_sha256=archive_digest,
        package_sha256=str(manifest["package_sha256"]),
        workflow_run_id=str(manifest["workflow_run_id"]),
        member_ids=tuple(member_ids),
        redaction=str(manifest["view"]["redaction"]),
        origin_verified=False,
    )


verify_evidence_archive = verify_archive


__all__ = [
    "DEFAULT_LIMITS",
    "EvidenceVerification",
    "EvidenceVerificationError",
    "VerificationLimits",
    "verify_archive",
    "verify_evidence_archive",
]
