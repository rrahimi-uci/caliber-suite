"""Materialize and persist an authorized evidence-package archive.

The package assembler owns the selected run membership and authorization
boundary.  This module is the only beta consumer that materializes the
selected payload bytes: it verifies the assembler's digests, applies the
configured redactor before hashing the exported view, writes a deterministic
ZIP through ``StorageBackend``, and records the scope-bound object locator.
Verifier, SDK/CLI, and retention behavior are separate beta slices.
"""

from __future__ import annotations

import copy
import hashlib
import io
import json
import uuid
import zipfile
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from caliber.audit import get_redactor
from caliber.auth import CaliberIdentity
from caliber.contracts import evidence_package_digest, validate_record
from caliber.db.models import (
    CaliberEvidenceArchive,
    CaliberProject,
)
from caliber.evidence.package import (
    EvidencePackage,
    EvidencePackageError,
    _canonical_bytes,
    _digest,
    archive_member_name,
    assemble,
    payload_bytes_for_package,
)
from caliber.redaction import DEFAULT_PATTERNS, IDENTITY_REDACTOR, Redactor
from caliber.storage import (
    StorageBackend,
    StorageConflictError,
    StorageError,
    safe_relative_path,
)


class EvidenceExportError(ValueError):
    """A package could not be materialized as a valid archive."""


@dataclass(frozen=True)
class EvidenceArchive:
    """The persisted archive and its canonical manifest."""

    archive_id: str
    workflow_run_id: str
    object_key: str
    archive_bytes: bytes
    manifest: dict[str, Any]
    package_sha256: str
    authorization_scope_digest: str
    archive_sha256: str

    @property
    def digest(self) -> str:
        """Alias for the archive's content digest."""
        return self.archive_sha256


def authorization_scope_digest(identity: CaliberIdentity) -> str:
    """Return the stable cache scope for the resolved authorization context.

    The user and active/credential project are included in addition to platform
    scopes.  This prevents an archive created for one principal or project
    context from being reused merely because the evidence-package digest is
    identical.
    """

    scope = {
        "user_id": identity.user_id,
        "active_project_id": identity.active_project_id,
        "credential_kind": identity.credential_kind,
        "credential_project_id": identity.credential_project_id,
        "scopes": sorted(identity.scopes),
    }
    return hashlib.sha256(_canonical_bytes(scope)).hexdigest()


def _effective_redactor(force_redact: bool) -> Redactor:
    if not force_redact:
        # The route has already resolved whether this identity may read raw
        # payloads.  A process-wide audit redactor must not silently change
        # that authorization result or poison the unredacted cache key.
        return IDENTITY_REDACTOR
    configured = get_redactor()
    if configured.patterns:
        return configured
    return Redactor.from_patterns(DEFAULT_PATTERNS, replacement="[REDACTED]")


def _redact_payload(payload: bytes, redactor: Redactor) -> bytes:
    try:
        value = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise EvidenceExportError(
            "cannot export a non-JSON evidence payload while redaction is enabled"
        ) from exc
    return _canonical_bytes(redactor.redact_value(value))


def _prepare_manifest(
    package: EvidencePackage, payloads: dict[str, bytes], *, redact: bool
) -> tuple[dict[str, Any], dict[str, bytes]]:
    """Apply redaction and recompute every digest covered by the export view."""

    manifest = copy.deepcopy(package.record)
    redactor = _effective_redactor(redact)
    exported_payloads: dict[str, bytes] = {}
    for member in manifest["members"]:
        member_id = str(member["member_id"])
        payload = payloads[member_id]
        if redactor.patterns:
            payload = _redact_payload(payload, redactor)
            member["metadata"] = redactor.redact_value(member["metadata"])
            member["metadata_sha256"] = _digest(member["metadata"])
        member["payload_sha256"] = hashlib.sha256(payload).hexdigest()
        exported_payloads[member_id] = payload

    if redactor.patterns:
        manifest["evaluator_provenance"] = redactor.redact_value(manifest["evaluator_provenance"])
        manifest["view"] = {"authorization": "authorized", "redaction": "redacted"}
    else:
        manifest["view"] = {"authorization": "authorized", "redaction": "none"}
    manifest["package_sha256"] = "0" * 64
    manifest["package_sha256"] = evidence_package_digest(manifest)
    try:
        validate_record("evidence_package", manifest)
    except Exception as exc:  # normalize contract implementation details at this boundary
        raise EvidenceExportError(f"export manifest is invalid: {exc}") from exc
    return manifest, exported_payloads


def _zip_bytes(manifest: dict[str, Any], payloads: dict[str, bytes]) -> bytes:
    """Build a deterministic archive without extracting or trusting members."""

    output = io.BytesIO()
    with zipfile.ZipFile(output, mode="w", compression=zipfile.ZIP_DEFLATED) as archive:
        entries = [("manifest.json", _canonical_bytes(manifest))]
        entries.extend(
            (archive_member_name(member_id), payloads[member_id]) for member_id in sorted(payloads)
        )
        for name, data in entries:
            info = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            info.create_system = 3
            info.external_attr = 0o600 << 16
            archive.writestr(info, data)
    return output.getvalue()


def _object_key(
    *, tenant_id: str, project_id: str, run_id: str, scope_digest: str, view_digest: str
) -> str:
    segments = (tenant_id, project_id, run_id, scope_digest, view_digest)
    try:
        safe_segments = [safe_relative_path(segment) for segment in segments]
    except Exception as exc:
        raise EvidenceExportError("evidence archive scope contains an unsafe identifier") from exc
    return (
        f"tenant/{safe_segments[0]}/project/{safe_segments[1]}"
        f"/workflow-runs/{safe_segments[2]}/evidence/{safe_segments[3]}/{safe_segments[4]}.zip"
    )


def _result_from_row(
    row: CaliberEvidenceArchive, archive_bytes: bytes, manifest: dict[str, Any]
) -> EvidenceArchive:
    return EvidenceArchive(
        archive_id=row.archive_id,
        workflow_run_id=row.workflow_run_id,
        object_key=row.object_key,
        archive_bytes=archive_bytes,
        manifest=manifest,
        package_sha256=row.package_sha256,
        authorization_scope_digest=row.authorization_scope_digest,
        archive_sha256=row.archive_sha256,
    )


def _read_existing(backend: StorageBackend, row: CaliberEvidenceArchive) -> EvidenceArchive:
    try:
        archive_bytes = backend.read_bytes(row.object_key)
    except StorageError as exc:
        raise EvidenceExportError("persisted evidence archive is unavailable") from exc
    actual = hashlib.sha256(archive_bytes).hexdigest()
    if actual != row.archive_sha256:
        raise EvidenceExportError("persisted evidence archive failed its content digest check")
    try:
        with zipfile.ZipFile(io.BytesIO(archive_bytes)) as archive:
            manifest = json.loads(archive.read("manifest.json").decode("utf-8"))
    except (OSError, KeyError, UnicodeDecodeError, json.JSONDecodeError, zipfile.BadZipFile) as exc:
        raise EvidenceExportError("persisted evidence archive has no valid manifest") from exc
    if not isinstance(manifest, dict):
        raise EvidenceExportError("persisted evidence archive manifest is not an object")
    try:
        validate_record("evidence_package", manifest)
    except Exception as exc:
        raise EvidenceExportError("persisted evidence archive manifest is invalid") from exc
    if manifest.get("package_sha256") != row.package_sha256:
        raise EvidenceExportError(
            "persisted evidence archive manifest digest does not match its locator"
        )
    return _result_from_row(row, archive_bytes, manifest)


def export_archive(
    session: Session,
    *,
    run_id: str,
    identity: CaliberIdentity,
    backend: StorageBackend,
    actor: str | None = None,
    redact: bool = False,
) -> EvidenceArchive:
    """Export an authorized run package through the supplied storage backend."""

    if not isinstance(backend, StorageBackend):
        raise EvidenceExportError("backend must implement StorageBackend")
    package = assemble(session, run_id=run_id, identity=identity)
    project = session.get(CaliberProject, package.record["project_id"])
    if project is None:
        raise EvidencePackageError("evidence package project disappeared while exporting")
    if backend.name != project.storage_backend:
        raise EvidenceExportError(
            "storage backend does not match the selected project's configured backend"
        )

    raw_payloads = payload_bytes_for_package(session, package)
    manifest, payloads = _prepare_manifest(package, raw_payloads, redact=redact)
    scope_digest = authorization_scope_digest(identity)
    view_digest = str(manifest["package_sha256"])
    existing = session.execute(
        select(CaliberEvidenceArchive).where(
            CaliberEvidenceArchive.workflow_run_id == run_id,
            CaliberEvidenceArchive.package_sha256 == view_digest,
            CaliberEvidenceArchive.authorization_scope_digest == scope_digest,
            CaliberEvidenceArchive.deleted_at.is_(None),
        )
    ).scalar_one_or_none()
    if existing is not None:
        return _read_existing(backend, existing)

    archive_bytes = _zip_bytes(manifest, payloads)
    archive_digest = hashlib.sha256(archive_bytes).hexdigest()
    key = _object_key(
        tenant_id=str(manifest["tenant_id"]),
        project_id=str(manifest["project_id"]),
        run_id=run_id,
        scope_digest=scope_digest,
        view_digest=str(manifest["package_sha256"]),
    )
    try:
        meta = backend.write_bytes(
            key,
            archive_bytes,
            media_type="application/zip",
            metadata={
                "caliber-schema": "evidence_package.v1",
                "caliber-package-sha256": str(manifest["package_sha256"]),
                "caliber-authorization-scope": scope_digest,
            },
            overwrite=False,
        )
    except StorageConflictError:
        # A concurrent exporter may have won the conditional create.  Never
        # overwrite it: read and verify the existing object, then persist the
        # corresponding locator if this transaction has not observed it yet.
        try:
            existing_bytes = backend.read_bytes(key)
        except StorageError as exc:
            raise EvidenceExportError("concurrent evidence archive is unavailable") from exc
        if hashlib.sha256(existing_bytes).hexdigest() != archive_digest:
            raise EvidenceExportError(
                "evidence archive key already contains different bytes"
            ) from None
        meta = backend.stat(key)
    except StorageError as exc:
        raise EvidenceExportError(f"evidence archive storage failed: {exc}") from exc

    exported_at = datetime.now(timezone.utc)
    row = CaliberEvidenceArchive(
        archive_id=f"evidence-archive-{uuid.uuid4().hex}",
        workflow_run_id=run_id,
        tenant_id=str(manifest["tenant_id"]),
        project_id=str(manifest["project_id"]),
        package_id=str(manifest["package_id"]),
        package_sha256=str(manifest["package_sha256"]),
        authorization_scope_digest=scope_digest,
        member_ids_json=[str(member["member_id"]) for member in manifest["members"]],
        storage_backend=meta.ref.backend,
        storage_uri=meta.ref.uri,
        bucket=meta.ref.bucket,
        object_key=key,
        object_version_id=meta.object_version_id,
        archive_sha256=archive_digest,
        size_bytes=len(archive_bytes),
        exported_by=actor or identity.user_id,
        exported_at=exported_at,
    )
    session.add(row)
    try:
        session.flush()
    except IntegrityError:
        session.rollback()
        concurrent = session.execute(
            select(CaliberEvidenceArchive).where(
                CaliberEvidenceArchive.workflow_run_id == run_id,
                CaliberEvidenceArchive.package_sha256 == str(manifest["package_sha256"]),
                CaliberEvidenceArchive.authorization_scope_digest == scope_digest,
                CaliberEvidenceArchive.deleted_at.is_(None),
            )
        ).scalar_one_or_none()
        if concurrent is None:
            raise
        return _read_existing(backend, concurrent)
    return _result_from_row(row, archive_bytes, manifest)


export_evidence_archive = export_archive
export_evidence = export_archive


__all__ = [
    "EvidenceArchive",
    "EvidenceExportError",
    "authorization_scope_digest",
    "export_archive",
    "export_evidence",
    "export_evidence_archive",
]
