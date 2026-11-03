"""Server-free verification for CALIBER evidence archives.

This module deliberately depends only on the Python standard library so the
standalone SDK can verify an archive without installing the CALIBER server.
It implements the versioned ``evidence_package.v1`` wire contract, including
the same canonical digest and member naming rules as the server producer.
Successful verification proves integrity and linkage only; Beta archives are
unsigned and therefore do not prove origin or authenticity.
"""

from __future__ import annotations

import hashlib
import io
import json
import stat
import zipfile
from dataclasses import dataclass
from typing import Any

_SUPPORTED_KINDS = frozenset(
    {
        "run_manifest",
        "context_manifest",
        "proposal",
        "receipt",
        "policy_decision",
        "approval_request",
        "approval_decision",
        "event_envelope",
        "agent_step_request",
        "agent_step_result",
        "evidence_package",
    }
)
_HEX64 = set("0123456789abcdef")
_REQUIRED_MANIFEST_FIELDS = {
    "schema_version",
    "package_id",
    "workflow_run_id",
    "tenant_id",
    "region",
    "project_id",
    "view",
    "members",
    "missing_links",
    "evaluator_provenance",
    "package_sha256",
    "created_at",
}
_OPTIONAL_MANIFEST_FIELDS = {"signatures"}
_REQUIRED_MEMBER_FIELDS = {
    "member_id",
    "kind",
    "payload_sha256",
    "metadata_sha256",
    "parent_ids",
    "metadata",
}


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
        return True


def _fail(code: str, message: str) -> EvidenceVerificationError:
    return EvidenceVerificationError(code, message)


def _canonical(value: Any) -> bytes:
    try:
        return json.dumps(
            value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
        ).encode("utf-8")
    except (TypeError, ValueError, RecursionError) as exc:
        raise _fail("noncanonical_json", "value is not canonical JSON") from exc


def _package_digest(manifest: dict[str, Any]) -> str:
    without_digest = {
        key: value for key, value in manifest.items() if key not in {"package_sha256", "signatures"}
    }
    members = []
    for member in without_digest["members"]:
        members.append({**member, "parent_ids": sorted(member["parent_ids"])})
    without_digest["members"] = sorted(members, key=lambda item: item["member_id"])
    return hashlib.sha256(_canonical(without_digest)).hexdigest()


def _member_name(member_id: str) -> str:
    return f"members/{hashlib.sha256(member_id.encode('utf-8')).hexdigest()}.json"


def _is_hex_digest(value: object) -> bool:
    return isinstance(value, str) and len(value) == 64 and set(value) <= _HEX64


def _text(value: object, *, max_length: int) -> bool:
    return (
        isinstance(value, str)
        and 0 < len(value) <= max_length
        and value.strip() == value
        and not any(ord(char) < 0x20 or 0x7F <= ord(char) <= 0x9F for char in value)
        and not any(0xD800 <= ord(char) <= 0xDFFF for char in value)
    )


def _validate_manifest(manifest: object) -> dict[str, Any]:
    if not isinstance(manifest, dict):
        raise _fail("invalid_manifest_shape", "manifest is not an object")
    keys = set(manifest)
    if (
        not keys >= _REQUIRED_MANIFEST_FIELDS
        or keys - _REQUIRED_MANIFEST_FIELDS - _OPTIONAL_MANIFEST_FIELDS
    ):
        raise _fail("invalid_manifest", "manifest fields do not match evidence-package v1")
    if manifest.get("schema_version") != "v1":
        raise _fail("unsupported_schema", "manifest declares an unsupported schema")
    for field, limit in (
        ("package_id", 128),
        ("workflow_run_id", 64),
        ("tenant_id", 128),
        ("region", 128),
        ("project_id", 128),
        ("created_at", 128),
    ):
        if not _text(manifest.get(field), max_length=limit):
            raise _fail("invalid_manifest", "manifest contains an invalid required field")
    view = manifest.get("view")
    if (
        not isinstance(view, dict)
        or set(view) != {"authorization", "redaction"}
        or view.get("authorization") != "authorized"
    ):
        raise _fail("invalid_view", "manifest view is invalid")
    if view["redaction"] not in {"none", "redacted"}:
        raise _fail("invalid_view", "manifest view is invalid")
    members = manifest.get("members")
    if not isinstance(members, list) or not members:
        raise _fail("invalid_members", "manifest has no members")
    member_ids: list[str] = []
    for member in members:
        if not isinstance(member, dict) or set(member) != _REQUIRED_MEMBER_FIELDS:
            raise _fail("invalid_member", "manifest contains an invalid member")
        member_id = member.get("member_id")
        if not _text(member_id, max_length=128):
            raise _fail("invalid_member", "manifest contains an invalid member")
        assert isinstance(member_id, str)
        if member_id in member_ids:
            raise _fail("duplicate_member", "manifest contains duplicate member ids")
        member_ids.append(member_id)
        if not isinstance(member.get("kind"), str) or not member["kind"].islower():
            raise _fail("invalid_member", "manifest contains an invalid member kind")
        if member["kind"] not in _SUPPORTED_KINDS:
            raise _fail("unsupported_member_schema", "member declares an unsupported schema")
        if not _is_hex_digest(member.get("payload_sha256")) or not _is_hex_digest(
            member.get("metadata_sha256")
        ):
            raise _fail("invalid_member", "manifest contains an invalid member digest")
        parents = member.get("parent_ids")
        if not isinstance(parents, list) or not all(
            _text(parent, max_length=128) for parent in parents
        ):
            raise _fail("invalid_parent_links", "manifest contains invalid parent links")
        if len(parents) != len(set(parents)):
            raise _fail("invalid_parent_links", "manifest contains invalid parent links")
        metadata = member.get("metadata")
        if not isinstance(metadata, dict) or metadata.get("schema_version") != "v1":
            raise _fail("unsupported_member_schema", "member declares an unsupported schema")
    member_set = set(member_ids)
    missing = manifest.get("missing_links")
    if not isinstance(missing, list):
        raise _fail("invalid_missing_links", "manifest missing-link list is invalid")
    missing_ids = {
        link.get("expected_member_id")
        for link in missing
        if isinstance(link, dict) and isinstance(link.get("expected_member_id"), str)
    }
    for member in members:
        if any(
            parent not in member_set and parent not in missing_ids
            for parent in member["parent_ids"]
        ):
            raise _fail("broken_parent_link", "manifest contains a parent link outside the package")
    if not isinstance(manifest.get("evaluator_provenance"), list):
        raise _fail("invalid_provenance", "manifest evaluator provenance is invalid")
    if not _is_hex_digest(manifest.get("package_sha256")):
        raise _fail("invalid_manifest", "manifest package digest is invalid")
    if manifest["package_sha256"] != _package_digest(manifest):
        raise _fail("package_digest_mismatch", "manifest package digest failed verification")
    return manifest


def _check_limits(limits: VerificationLimits) -> None:
    if (
        any(
            value <= 0
            for value in (
                limits.max_archive_bytes,
                limits.max_manifest_bytes,
                limits.max_member_bytes,
                limits.max_total_uncompressed_bytes,
                limits.max_entries,
                limits.max_compression_ratio,
            )
        )
        or limits.max_manifest_bytes > limits.max_member_bytes
    ):
        raise _fail("invalid_limits", "verification limits are invalid")


def _check_entry(info: zipfile.ZipInfo, limits: VerificationLimits) -> None:
    name = info.filename
    if not name or name.startswith(("/", "\\")) or "\\" in name:
        raise _fail("unsafe_member_path", "archive contains an unsafe member path")
    if any(part in {"", ".", ".."} for part in name.split("/")) or any(
        ord(char) < 0x20 for char in name
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
    member_limit = limits.max_manifest_bytes if name == "manifest.json" else limits.max_member_bytes
    if info.file_size > member_limit:
        raise _fail("member_size_limit", "archive member exceeds its size limit")
    if info.file_size and (
        not info.compress_size or info.file_size > info.compress_size * limits.max_compression_ratio
    ):
        raise _fail("compression_ratio_limit", "archive member exceeds its compression ratio limit")


def _read_member(archive: zipfile.ZipFile, info: zipfile.ZipInfo) -> bytes:
    try:
        with archive.open(info, mode="r") as stream:
            return stream.read()
    except (OSError, RuntimeError, ValueError, zipfile.BadZipFile) as exc:
        raise _fail("member_read_failed", "archive member could not be read safely") from exc


def verify_archive(
    archive_bytes: bytes, *, limits: VerificationLimits = DEFAULT_LIMITS
) -> EvidenceVerification:
    """Verify an evidence ZIP without extracting it or contacting a server."""

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
        if sum(info.file_size for info in infos) > limits.max_total_uncompressed_bytes:
            raise _fail("total_size_limit", "archive exceeds its total uncompressed size limit")
        manifest_info = next((info for info in infos if info.filename == "manifest.json"), None)
        if manifest_info is None:
            raise _fail("missing_manifest", "archive has no manifest")
        manifest_bytes = _read_member(archive, manifest_info)
        try:
            manifest = json.loads(manifest_bytes.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise _fail("invalid_manifest_json", "manifest is not valid JSON") from exc
        if manifest_bytes != _canonical(manifest):
            raise _fail("noncanonical_manifest", "manifest is not canonical JSON")
        manifest = _validate_manifest(manifest)
        expected_names = {
            "manifest.json",
            *(_member_name(member["member_id"]) for member in manifest["members"]),
        }
        if set(names) != expected_names:
            raise _fail("membership_mismatch", "archive members do not match the manifest")
        info_by_name = {info.filename: info for info in infos}
        verified_ids: list[str] = []
        for member in manifest["members"]:
            member_id = member["member_id"]
            verified_ids.append(member_id)
            if (
                hashlib.sha256(_canonical(member["metadata"])).hexdigest()
                != member["metadata_sha256"]
            ):
                raise _fail("metadata_digest_mismatch", "member metadata failed its digest check")
            payload = _read_member(archive, info_by_name[_member_name(member_id)])
            if hashlib.sha256(payload).hexdigest() != member["payload_sha256"]:
                raise _fail("payload_digest_mismatch", "member payload failed its digest check")
    return EvidenceVerification(
        archive_sha256=archive_digest,
        package_sha256=manifest["package_sha256"],
        workflow_run_id=manifest["workflow_run_id"],
        member_ids=tuple(verified_ids),
        redaction=manifest["view"]["redaction"],
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
