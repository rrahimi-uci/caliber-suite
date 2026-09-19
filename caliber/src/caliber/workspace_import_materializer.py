"""Materialize a durable, already-validated Workspace import job into an
immutable ``WorkspaceRevision`` (the still-open piece of `P4-A`/`P4-B`/`P4-C`).

Architecture note -- read this before changing the source of truth
--------------------------------------------------------------------
The only currently-wired import path is the ``push`` (multipart ZIP upload)
mode implemented by ``routes/workspace.py::_create_import_sync``. That route
already calls :func:`caliber.workspace_source.materialize_workspace_source`
*synchronously during the HTTP request*, so by the time a
``CaliberWorkspaceImportJob`` reaches ``queued``, its canonical, content-
addressed source-tree snapshot is already validated and persisted as a
``CaliberWorkflowFile`` (``job.source_snapshot_file_id``), and its manifest
digest is already recorded (``job.manifest_sha256``). This module's job is
the remaining, still-open step: read that retained snapshot back, recompute
and cross-check the manifest and each declared resource's content digest
against it, and emit the immutable ``WorkspaceRevision``/
``WorkspaceRevisionResource`` rows.

This module deliberately does **not** call any source-control provider to
fetch file content. Two provider-shaped contracts exist in this codebase and
neither is a content-fetch API:

* ``workspace_sources.WorkspaceSourceProvider`` (`P4-C`) only verifies a
  source *connection's* identity/capabilities (``verify(source)``, no commit
  argument); it is optionally consulted by the worker
  (``workspace_import_worker.py``) as best-effort attestation, not as a
  content source, and no adapter is registered in the real server today
  (``server.py`` never populates ``app.state.workspace_source_registry``).
* ``workspace_source_control.SourceControlProvider`` (`P4-E`) fetches
  normalized *commit metadata* (SHA, parents, changed paths, an opaque tree
  digest) for Change-Request review-evidence verification. It has no method
  that returns file bytes/tree entries, and it is not wired to the import
  path at all -- it is a different concern (proving humans reviewed code),
  not a way to materialize source content.

So there is no real "fetch the commit's tree" step to perform yet for a
`push`-mode import: the bytes already live in the retained snapshot. A
future `provider_pull` import mode (rejected today by
``_create_import_sync`` with ``source_import_mode_does_not_accept_push``)
would need real content-fetch capability that does not exist on either
adapter contract; adding it is explicitly out of scope here (see the task
brief's scope-discipline note) and is left as a named gap for that later
slice.
"""

from __future__ import annotations

import hashlib
import io
import tarfile
from dataclasses import dataclass, field
from typing import Any, Final

import yaml
from pydantic import ValidationError
from sqlalchemy.orm import Session

from caliber.db.models import (
    CaliberWorkflowFile,
    CaliberWorkspaceImportJob,
    CaliberWorkspaceRevision,
    CaliberWorkspaceRevisionResource,
    CaliberWorkspaceSource,
)
from caliber.ids import new_workspace_revision_id, new_workspace_revision_resource_id
from caliber.storage.base import StorageValidationError, safe_relative_path
from caliber.workflows.manifest import canonical_json
from caliber.workspace_manifest import (
    WorkspaceManifest,
    manifest_digest,
    parse_workspace_manifest,
)
from caliber.workspace_revisions import allocate_revision_number, finalize_revision
from caliber.workspace_source import (
    WorkspaceSourceDigestConflictError,
    assert_source_commit_digest,
)

# The single resolution strategy this worker implements today: resources are
# pinned directly off the retained source snapshot, with no live CALIBER
# resource-registry lookup. A mutable-resource/model/dependency adapter
# (`P4-B`/`P4-C`'s own "still open" item) would give ``resource_id`` real
# identity; until then it is a deterministic, source-only identifier.
MATERIALIZER_ADAPTER_VERSION: Final = "workspace-import-worker/1"

# Manifest resource-list field name -> WorkspaceRevisionResource.resource_type.
_RESOURCE_TYPE_MAP: Final[dict[str, str]] = {
    "agents": "agent",
    "workflows": "workflow",
    "prompts": "prompt",
    "skills": "skill",
    "tools": "tool",
    "testSets": "test_set",
    "knowledgeBases": "knowledge_base",
    "judges": "judge",
    "integrations": "integration",
    "mcpBindings": "mcp_binding",
    "models": "model",
}


class WorkspaceImportMaterializationError(RuntimeError):
    """Base class for a deterministic, fully-diagnosed materialization failure.

    Every subclass carries a stable ``code`` the worker maps 1:1 onto
    ``finish_import_job``'s ``error_code`` for a ``failed`` outcome -- these
    are reserved for failures the worker can fully explain without any
    ambiguity about external state. A truly unexpected exception (a bug, a
    transient storage error not covered below, a concurrent unique-constraint
    race) is deliberately left unwrapped: it propagates to the worker's tick
    loop, the job's lease is left to expire, and
    :func:`caliber.workspace_imports.reconcile_expired_import_jobs` moves it
    to ``reconcile_required`` for explicit operator observation -- the same
    recovery boundary every other ambiguous outcome in this system already
    uses, rather than a bespoke retry heuristic invented for this worker.
    """

    code = "workspace_import_materialization_error"


class SnapshotCorruptError(WorkspaceImportMaterializationError):
    code = "import_snapshot_corrupt"


class ManifestNotFoundError(WorkspaceImportMaterializationError):
    code = "import_manifest_not_found"


class ManifestInvalidError(WorkspaceImportMaterializationError):
    code = "import_manifest_invalid"


class ManifestDigestMismatchError(WorkspaceImportMaterializationError):
    code = "import_manifest_digest_mismatch"


class ResourcePathMissingError(WorkspaceImportMaterializationError):
    code = "import_resource_path_missing"


class SourceCommitConflictError(WorkspaceImportMaterializationError):
    code = "source_commit_digest_conflict"


@dataclass(frozen=True)
class _ResourcePin:
    resource_type: str
    logical_name: str
    resource_id: str
    version_ref: str
    content_sha256: str
    source_path: str
    source_sha256: str
    provider_ref: str | None
    snapshot_file_id: str | None
    snapshot_sha256: str | None
    purpose: str = "runtime"
    resolution: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class MaterializationResult:
    """Outcome of :func:`materialize_import_job`."""

    revision: CaliberWorkspaceRevision
    replayed: bool
    """``True`` when an existing revision for this exact source/commit/bundle
    content was reused rather than created -- an idempotent re-materialization
    of a job that already succeeded once (e.g. a duplicate queued job sharing
    the same content, or a worker retry after a crash that landed *before*
    the previous attempt's commit -- see the module docstring on
    ``workspace_import_worker`` for why a crash can never land *after* it)."""


def _extract_tar_entries(data: bytes) -> dict[str, bytes]:
    """Read the deterministic PAX tar snapshot back into ``{path: bytes}``.

    Mirrors the shape :func:`caliber.workspace_source.materialize_workspace_source`
    wrote it in. Any structural problem here means the retained snapshot is
    corrupt -- not a source-content problem the caller can fix by retrying
    the same bytes -- so it fails closed with a stable, diagnosable code.
    """
    entries: dict[str, bytes] = {}
    try:
        with tarfile.open(fileobj=io.BytesIO(data), mode="r:") as archive:
            for member in archive.getmembers():
                if not member.isfile():
                    continue
                path = safe_relative_path(member.name)
                extracted = archive.extractfile(member)
                if extracted is None:  # pragma: no cover - tarfile returns a stream for every
                    continue  # regular file; this guards an interface contract, not a real case
                entries[path] = extracted.read()
    except (tarfile.TarError, StorageValidationError, OSError) as exc:
        raise SnapshotCorruptError(
            f"{SnapshotCorruptError.code}: retained source snapshot could not be read: {exc}"
        ) from exc
    return entries


def _load_and_verify_manifest(
    entries: dict[str, bytes],
    *,
    manifest_path: str,
    expected_manifest_sha256: str,
) -> WorkspaceManifest:
    manifest_bytes = entries.get(manifest_path)
    if manifest_bytes is None:
        raise ManifestNotFoundError(
            f"{ManifestNotFoundError.code}: manifest path {manifest_path!r} was not found in "
            "the retained source snapshot"
        )
    try:
        document = yaml.safe_load(manifest_bytes)
    except yaml.YAMLError as exc:
        raise ManifestInvalidError(f"{ManifestInvalidError.code}: {exc}") from exc
    if not isinstance(document, dict):
        raise ManifestInvalidError(
            f"{ManifestInvalidError.code}: manifest must decode to a mapping"
        )
    try:
        manifest = parse_workspace_manifest(document)
    except ValidationError as exc:
        raise ManifestInvalidError(f"{ManifestInvalidError.code}: {exc}") from exc
    digest = manifest_digest(manifest)
    if digest != expected_manifest_sha256:
        raise ManifestDigestMismatchError(
            f"{ManifestDigestMismatchError.code}: recomputed manifest digest {digest!r} does "
            f"not match the import job's recorded digest {expected_manifest_sha256!r}"
        )
    return manifest


def _primary_path_and_provider_ref(resource_type: str, entry: Any) -> tuple[str, str | None]:
    if resource_type == "knowledge_base":
        return entry.manifestPath, None
    if resource_type == "mcp_binding":
        return entry.policyPath, entry.connectionRef
    if resource_type == "model":
        return entry.configPath, f"{entry.provider}:{entry.snapshot}"
    return entry.path, None


def _resource_pins(
    manifest: WorkspaceManifest,
    entries: dict[str, bytes],
    *,
    commit_sha: str,
    snapshot_file_id: str | None,
    snapshot_sha256: str | None,
) -> list[_ResourcePin]:
    pins: list[_ResourcePin] = []
    for field_name, resource_type in _RESOURCE_TYPE_MAP.items():
        for entry in getattr(manifest.resources, field_name):
            primary_path, provider_ref = _primary_path_and_provider_ref(resource_type, entry)
            content = entries.get(primary_path)
            if content is None:
                raise ResourcePathMissingError(
                    f"{ResourcePathMissingError.code}: {resource_type} {entry.name!r} declares "
                    f"path {primary_path!r}, which was not found in the retained source snapshot"
                )
            content_sha256 = hashlib.sha256(content).hexdigest()
            pins.append(
                _ResourcePin(
                    resource_type=resource_type,
                    logical_name=entry.name,
                    # No live resource-adapter exists yet (P4-B/P4-C's own
                    # "still open" item); this is a deterministic, source-only
                    # identity, not a resolved CALIBER resource row.
                    resource_id=f"{resource_type}:{entry.name}",
                    version_ref=commit_sha,
                    content_sha256=content_sha256,
                    source_path=primary_path,
                    source_sha256=content_sha256,
                    provider_ref=provider_ref,
                    snapshot_file_id=snapshot_file_id,
                    snapshot_sha256=snapshot_sha256,
                    resolution={
                        "strategy": "source_snapshot",
                        "adapter_version": MATERIALIZER_ADAPTER_VERSION,
                    },
                )
            )
    pins.sort(key=lambda pin: (pin.resource_type, pin.logical_name))
    return pins


def _revision_descriptor(
    *,
    project_id: str,
    source_id: str | None,
    import_mode: str,
    commit_sha: str,
    source_bundle_sha256: str,
    snapshot_sha256: str | None,
    manifest_sha256: str,
    resources: list[_ResourcePin],
) -> dict[str, Any]:
    """The canonical package descriptor named by ``docs/workspace-plan.md``
    section 9.3: ``revision_sha256 = SHA-256(canonical_json(package_descriptor))``.

    The plan names the ingredients (schema version, project, source mode/
    commit/tree digest/snapshot reference, resource entries sorted by
    ``(resource_type, logical_name)`` with version reference, content digest,
    snapshot reference and adapter version) without pinning an exact JSON
    shape; this is this worker's concrete, deterministic implementation of
    that formula.
    """
    return {
        "schema_version": "v1alpha1",
        "project_id": project_id,
        "source_id": source_id,
        "source_mode": import_mode,
        "source_commit_sha": commit_sha,
        "source_bundle_sha256": source_bundle_sha256,
        "source_snapshot_sha256": snapshot_sha256,
        "manifest_sha256": manifest_sha256,
        "resources": [
            {
                "resource_type": pin.resource_type,
                "logical_name": pin.logical_name,
                "version_ref": pin.version_ref,
                "content_sha256": pin.content_sha256,
                "snapshot_sha256": pin.snapshot_sha256,
                "adapter_version": MATERIALIZER_ADAPTER_VERSION,
            }
            for pin in resources
        ],
    }


def materialize_import_job(
    session: Session,
    job: CaliberWorkspaceImportJob,
    source: CaliberWorkspaceSource,
    snapshot: CaliberWorkflowFile,
    snapshot_data: bytes,
    *,
    worker_id: str,
    provider_attestation: dict[str, Any] | None = None,
) -> MaterializationResult:
    """Turn a claimed, snapshot-backed import job into a ``ready`` revision.

    Does not commit. The caller (``workspace_import_worker.py``) shares one
    session across this call and its subsequent
    ``workspace_imports.finish_import_job`` call so both land in the single
    commit ``finish_import_job`` performs -- see that module's docstring for
    why this is what makes a mid-materialization crash safe.
    """
    # source_bundle_sha256/manifest_sha256 are nullable at the schema level
    # (a future provider_pull import mode may not have them at claim time),
    # but the only currently-wired `push` mode always sets both synchronously
    # at submission (`routes/workspace.py::_create_import_sync`) before the
    # job is ever queued -- see the module docstring. A job missing either
    # here is not a materialization problem this worker can diagnose further.
    if not job.source_bundle_sha256 or not job.manifest_sha256:
        raise WorkspaceImportMaterializationError(
            f"{WorkspaceImportMaterializationError.code}: import job {job.import_job_id!r} is "
            "missing its source_bundle_sha256/manifest_sha256; only push-mode imports "
            "(already validated at submission) are supported"
        )

    try:
        existing_revision_id = assert_source_commit_digest(
            session, job.source_id, job.commit_sha, job.source_bundle_sha256
        )
    except WorkspaceSourceDigestConflictError as exc:
        raise materialization_error_from_digest_conflict(exc) from exc
    if existing_revision_id is not None:
        revision = session.get(CaliberWorkspaceRevision, existing_revision_id)
        if revision is None:  # pragma: no cover - defensive; FK proves existence
            raise WorkspaceImportMaterializationError(
                f"workspace_import_materialization_error: revision "
                f"{existing_revision_id!r} observed but not found"
            )
        return MaterializationResult(revision=revision, replayed=True)

    entries = _extract_tar_entries(snapshot_data)
    manifest = _load_and_verify_manifest(
        entries,
        manifest_path=source.manifest_path,
        expected_manifest_sha256=job.manifest_sha256,
    )
    resources = _resource_pins(
        manifest,
        entries,
        commit_sha=job.commit_sha,
        snapshot_file_id=job.source_snapshot_file_id,
        snapshot_sha256=snapshot.sha256,
    )
    descriptor = _revision_descriptor(
        project_id=job.project_id,
        source_id=job.source_id,
        import_mode=source.import_mode,
        commit_sha=job.commit_sha,
        source_bundle_sha256=job.source_bundle_sha256,
        snapshot_sha256=snapshot.sha256,
        manifest_sha256=job.manifest_sha256,
        resources=resources,
    )
    revision_sha256 = hashlib.sha256(canonical_json(descriptor).encode("utf-8")).hexdigest()

    revision_number = allocate_revision_number(session, job.project_id)
    revision = CaliberWorkspaceRevision(
        revision_id=new_workspace_revision_id(),
        project_id=job.project_id,
        revision_number=revision_number,
        source_id=job.source_id,
        source_commit_sha=job.commit_sha,
        manifest=manifest.model_dump(mode="json"),
        manifest_sha256=job.manifest_sha256,
        source_bundle_sha256=job.source_bundle_sha256,
        source_snapshot_file_id=job.source_snapshot_file_id,
        source_attestation="provider_attested" if provider_attestation else "caller_attested",
        revision_sha256=revision_sha256,
        created_by=worker_id,
    )
    session.add(revision)
    session.flush()
    for pin in resources:
        session.add(
            CaliberWorkspaceRevisionResource(
                resource_pin_id=new_workspace_revision_resource_id(),
                revision_id=revision.revision_id,
                resource_type=pin.resource_type,
                logical_name=pin.logical_name,
                resource_id=pin.resource_id,
                version_ref=pin.version_ref,
                content_sha256=pin.content_sha256,
                source_path=pin.source_path,
                source_sha256=pin.source_sha256,
                provider_ref=pin.provider_ref,
                snapshot_file_id=pin.snapshot_file_id,
                snapshot_sha256=pin.snapshot_sha256,
                purpose=pin.purpose,
                resolution=pin.resolution,
            )
        )
    finalize_revision(
        session,
        revision,
        "ready",
        validation_report={
            "materializer": MATERIALIZER_ADAPTER_VERSION,
            "resource_count": len(resources),
            "provider_attestation": provider_attestation,
        },
        validated_by=worker_id,
    )
    session.flush()
    return MaterializationResult(revision=revision, replayed=False)


def materialization_error_from_digest_conflict(
    exc: WorkspaceSourceDigestConflictError,
) -> SourceCommitConflictError:
    """Re-type the source-module's conflict error under this module's taxonomy."""
    return SourceCommitConflictError(str(exc))


__all__ = [
    "MATERIALIZER_ADAPTER_VERSION",
    "ManifestDigestMismatchError",
    "ManifestInvalidError",
    "ManifestNotFoundError",
    "MaterializationResult",
    "ResourcePathMissingError",
    "SnapshotCorruptError",
    "SourceCommitConflictError",
    "WorkspaceImportMaterializationError",
    "materialization_error_from_digest_conflict",
    "materialize_import_job",
]
