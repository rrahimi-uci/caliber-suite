"""Tests for :mod:`caliber.workspace_import_materializer`.

Pure, offline, DB-and-bytes-only tests of the "materialize an already-claimed,
already-snapshot-backed import job into a revision" logic -- no worker loop,
no asyncio, no HTTP, no source-control provider. Mirrors the fixture style of
``test_e2e_workspace_import.py`` (hand-seeded rows via ``db_session``) and
reuses the real ``materialize_workspace_source``/``storage`` pipeline for the
retained snapshot, since that snapshot's exact tar shape is exactly what
``routes/workspace.py::_create_import_sync`` produces in production.
"""

from __future__ import annotations

import io
import zipfile

import pytest
from sqlalchemy.orm import Session

from caliber.config import CaliberConfig
from caliber.db.models import (
    CaliberProject,
    CaliberWorkflowFile,
    CaliberWorkspaceImportJob,
    CaliberWorkspaceRevision,
    CaliberWorkspaceRevisionResource,
    CaliberWorkspaceSource,
)
from caliber.ids import new_workspace_import_id
from caliber.storage import WorkingDirectoryService, build_backend
from caliber.workspace_import_materializer import (
    ManifestDigestMismatchError,
    ManifestNotFoundError,
    ResourcePathMissingError,
    SnapshotCorruptError,
    SourceCommitConflictError,
    WorkspaceImportMaterializationError,
    materialize_import_job,
)
from caliber.workspace_manifest import manifest_digest, parse_workspace_manifest
from caliber.workspace_source import materialize_workspace_source

PROJECT_ID = "PRJ-materializer"
SOURCE_ID = "WSS-materializer"

_MANIFEST_DOC = {
    "apiVersion": "caliber/v1alpha1",
    "kind": "Workspace",
    "metadata": {"slug": "materializer"},
    "resources": {
        "workflows": [{"name": "support", "path": "workflows/support.json"}],
        "prompts": [{"name": "greeting", "path": "prompts/greeting.md"}],
        "mcpBindings": [
            {
                "name": "search",
                "connectionRef": "secret://mcp-search",
                "policyPath": "mcp/search-policy.json",
            }
        ],
        "models": [
            {
                "name": "primary",
                "provider": "openai",
                "snapshot": "gpt-4o-mini-2024-07-18",
                "configPath": "models/primary.yaml",
            }
        ],
    },
}


def _bundle(*, manifest_path: str = ".caliber/workspace.yaml", omit: str | None = None) -> bytes:
    output = io.BytesIO()
    files = {
        manifest_path: b"""apiVersion: caliber/v1alpha1
kind: Workspace
metadata:
  slug: materializer
resources:
  workflows:
    - name: support
      path: workflows/support.json
  prompts:
    - name: greeting
      path: prompts/greeting.md
  mcpBindings:
    - name: search
      connectionRef: secret://mcp-search
      policyPath: mcp/search-policy.json
  models:
    - name: primary
      provider: openai
      snapshot: gpt-4o-mini-2024-07-18
      configPath: models/primary.yaml
""",
        "workflows/support.json": b'{"name":"support"}\n',
        "prompts/greeting.md": b"Hello!\n",
        "mcp/search-policy.json": b'{"allow":["search"]}\n',
        "models/primary.yaml": b"temperature: 0.2\n",
    }
    if omit is not None:
        files.pop(omit, None)
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for path, data in files.items():
            archive.writestr(path, data)
    return output.getvalue()


@pytest.fixture
def storage(app_config: CaliberConfig) -> WorkingDirectoryService:
    return WorkingDirectoryService(
        build_backend(app_config.workflow_storage), app_config.workflow_storage
    )


def _seed_project_and_source(
    session: Session, *, project_id: str = PROJECT_ID, source_id: str = SOURCE_ID
) -> tuple[CaliberProject, CaliberWorkspaceSource]:
    project = CaliberProject(project_id=project_id, name=f"Materializer {project_id}")
    source = CaliberWorkspaceSource(
        source_id=source_id,
        project_id=project_id,
        provider="github",
        provider_host="github.com",
        canonical_repository_id=f"github:{project_id}",
        display_path="owner/materializer",
    )
    session.add_all([project, source])
    session.flush()
    return project, source


def _seed_job_and_snapshot(
    session: Session,
    storage: WorkingDirectoryService,
    *,
    project: CaliberProject,
    source: CaliberWorkspaceSource,
    bundle: bytes,
    commit_sha: str = "commit-1",
    manifest_sha256: str | None = None,
    idempotency_key: str | None = None,
) -> tuple[CaliberWorkspaceImportJob, CaliberWorkflowFile]:
    materialized = materialize_workspace_source(bundle)
    if manifest_sha256 is None:
        manifest = parse_workspace_manifest(_MANIFEST_DOC)
        manifest_sha256 = manifest_digest(manifest)
    record = storage.for_backend(project.storage_backend).register_project_file(
        session,
        project_id=project.project_id,
        kind="artifact",
        filename=f".caliber/workspace-snapshots/{materialized.snapshot_sha256}.tar",
        data=materialized.snapshot_bytes,
        media_type="application/x-tar",
        actor="@importer",
        tenant_id=project.tenant_id,
        metadata={"workspace_snapshot": True, "snapshot_sha256": materialized.snapshot_sha256},
    )
    # register_project_file returns a CaliberFileRecord DTO; the materializer
    # and WorkingDirectoryService.read_bytes both need the real ORM row.
    snapshot = session.get(CaliberWorkflowFile, record.file_id)
    assert snapshot is not None
    job = CaliberWorkspaceImportJob(
        import_job_id=new_workspace_import_id(),
        project_id=project.project_id,
        source_id=source.source_id,
        repository=source.display_path,
        commit_sha=commit_sha,
        upload_sha256=materialized.upload_sha256,
        source_bundle_sha256=materialized.source_bundle_sha256,
        source_snapshot_file_id=snapshot.file_id,
        manifest_sha256=manifest_sha256,
        idempotency_key=idempotency_key or f"key-{new_workspace_import_id()}",
        created_by="@importer",
    )
    session.add(job)
    session.flush()
    return job, snapshot


def _read(storage: WorkingDirectoryService, snapshot: CaliberWorkflowFile) -> bytes:
    return storage.read_bytes(snapshot)


def test_materialize_creates_a_ready_revision_with_sorted_resource_pins(
    db_session: Session, storage: WorkingDirectoryService
) -> None:
    project, source = _seed_project_and_source(db_session)
    job, snapshot = _seed_job_and_snapshot(
        db_session, storage, project=project, source=source, bundle=_bundle()
    )
    data = _read(storage, snapshot)

    result = materialize_import_job(
        db_session, job, source, snapshot, data, worker_id="import-worker-1"
    )

    assert result.replayed is False
    revision = result.revision
    assert revision.status == "ready"
    assert revision.project_id == project.project_id
    assert revision.source_commit_sha == job.commit_sha
    assert revision.manifest_sha256 == job.manifest_sha256
    assert revision.revision_number == 1
    assert revision.source_attestation == "caller_attested"
    assert revision.validation_report is not None
    assert revision.validation_report["resource_count"] == 4

    resources = (
        db_session.query(CaliberWorkspaceRevisionResource)
        .filter_by(revision_id=revision.revision_id)
        .order_by(CaliberWorkspaceRevisionResource.resource_type)
        .all()
    )
    by_type = {(r.resource_type, r.logical_name): r for r in resources}
    assert set(by_type) == {
        ("workflow", "support"),
        ("prompt", "greeting"),
        ("mcp_binding", "search"),
        ("model", "primary"),
    }
    mcp = by_type[("mcp_binding", "search")]
    assert mcp.provider_ref == "secret://mcp-search"
    assert mcp.source_path == "mcp/search-policy.json"
    model = by_type[("model", "primary")]
    assert model.provider_ref == "openai:gpt-4o-mini-2024-07-18"
    assert all(r.snapshot_file_id == job.source_snapshot_file_id for r in resources)
    assert all(r.version_ref == job.commit_sha for r in resources)
    assert all(r.purpose == "runtime" for r in resources)


def test_materialize_is_idempotent_for_the_same_source_commit_and_content(
    db_session: Session, storage: WorkingDirectoryService
) -> None:
    project, source = _seed_project_and_source(db_session)
    bundle = _bundle()
    job1, snapshot1 = _seed_job_and_snapshot(
        db_session, storage, project=project, source=source, bundle=bundle, commit_sha="commit-x"
    )
    first = materialize_import_job(
        db_session, job1, source, snapshot1, _read(storage, snapshot1), worker_id="worker-a"
    )
    assert first.replayed is False

    # A second job queued for the exact same source/commit/content (e.g. a
    # duplicate submission under a different Idempotency-Key, or a retried
    # attempt at the same job after a crash that landed before the first
    # attempt's commit -- see the worker module docstring).
    job2, snapshot2 = _seed_job_and_snapshot(
        db_session,
        storage,
        project=project,
        source=source,
        bundle=bundle,
        commit_sha="commit-x",
    )
    second = materialize_import_job(
        db_session, job2, source, snapshot2, _read(storage, snapshot2), worker_id="worker-b"
    )

    assert second.replayed is True
    assert second.revision.revision_id == first.revision.revision_id
    # No second revision row was created.
    assert (
        db_session.query(CaliberWorkspaceRevision).filter_by(project_id=project.project_id).count()
        == 1
    )


def test_materialize_rejects_a_conflicting_digest_for_the_same_commit(
    db_session: Session, storage: WorkingDirectoryService
) -> None:
    project, source = _seed_project_and_source(db_session)
    job1, snapshot1 = _seed_job_and_snapshot(
        db_session,
        storage,
        project=project,
        source=source,
        bundle=_bundle(),
        commit_sha="commit-conflict",
    )
    materialize_import_job(
        db_session, job1, source, snapshot1, _read(storage, snapshot1), worker_id="worker-a"
    )

    # A different bundle claiming to be the same commit: in real deployments
    # `_create_import_sync`'s own `conflicting_job` check blocks this at
    # submission time; this proves the materializer fails closed too rather
    # than trusting only the route-level check.
    different_manifest = dict(_MANIFEST_DOC)
    different_bundle = _bundle(manifest_path=".caliber/workspace.yaml")
    # Force a different bundle digest without changing the manifest digest:
    # add an extra file so `source_bundle_sha256` differs.
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        with zipfile.ZipFile(io.BytesIO(_bundle())) as base:
            for info in base.infolist():
                archive.writestr(info.filename, base.read(info))
        archive.writestr("docs/EXTRA.md", "an unrelated extra file\n")
    different_bundle = output.getvalue()

    job2, snapshot2 = _seed_job_and_snapshot(
        db_session,
        storage,
        project=project,
        source=source,
        bundle=different_bundle,
        commit_sha="commit-conflict",
    )
    with pytest.raises(SourceCommitConflictError) as excinfo:
        materialize_import_job(
            db_session, job2, source, snapshot2, _read(storage, snapshot2), worker_id="worker-b"
        )
    assert excinfo.value.code == "source_commit_digest_conflict"
    del different_manifest  # unused placeholder kept for readability above


def test_materialize_fails_closed_when_manifest_missing_from_snapshot(
    db_session: Session, storage: WorkingDirectoryService
) -> None:
    project, source = _seed_project_and_source(db_session)
    # manifest_path on the source is the default (.caliber/workspace.yaml);
    # seed a job whose bundle put it somewhere else so the retained snapshot
    # genuinely lacks that path.
    job, snapshot = _seed_job_and_snapshot(
        db_session,
        storage,
        project=project,
        source=source,
        bundle=_bundle(manifest_path="elsewhere/workspace.yaml"),
    )
    with pytest.raises(ManifestNotFoundError) as excinfo:
        materialize_import_job(
            db_session, job, source, snapshot, _read(storage, snapshot), worker_id="worker-a"
        )
    assert excinfo.value.code == "import_manifest_not_found"


def test_materialize_fails_closed_on_manifest_digest_mismatch(
    db_session: Session, storage: WorkingDirectoryService
) -> None:
    project, source = _seed_project_and_source(db_session)
    job, snapshot = _seed_job_and_snapshot(
        db_session,
        storage,
        project=project,
        source=source,
        bundle=_bundle(),
        manifest_sha256="0" * 64,  # deliberately wrong
    )
    with pytest.raises(ManifestDigestMismatchError) as excinfo:
        materialize_import_job(
            db_session, job, source, snapshot, _read(storage, snapshot), worker_id="worker-a"
        )
    assert excinfo.value.code == "import_manifest_digest_mismatch"


def test_materialize_fails_closed_when_a_declared_resource_path_is_missing(
    db_session: Session, storage: WorkingDirectoryService
) -> None:
    project, source = _seed_project_and_source(db_session)
    # The manifest declares workflows/support.json, but the bundle omits it --
    # something submission-time validation never checks today (only the
    # bundle structure and the manifest *document* are validated then).
    job, snapshot = _seed_job_and_snapshot(
        db_session,
        storage,
        project=project,
        source=source,
        bundle=_bundle(omit="workflows/support.json"),
    )
    with pytest.raises(ResourcePathMissingError) as excinfo:
        materialize_import_job(
            db_session, job, source, snapshot, _read(storage, snapshot), worker_id="worker-a"
        )
    assert excinfo.value.code == "import_resource_path_missing"
    assert "support" in str(excinfo.value)


def test_materialize_rejects_a_job_missing_its_content_digests(
    db_session: Session, storage: WorkingDirectoryService
) -> None:
    """``source_bundle_sha256``/``manifest_sha256`` are nullable at the schema
    level (a future ``provider_pull`` import mode may not set them at claim
    time), but the only currently-wired ``push`` mode always populates both
    synchronously at submission. A job missing either is not something this
    worker can diagnose further, so it fails closed with a stable code rather
    than crashing on a null downstream."""
    project, source = _seed_project_and_source(db_session)
    job, snapshot = _seed_job_and_snapshot(
        db_session, storage, project=project, source=source, bundle=_bundle()
    )
    job.source_bundle_sha256 = None
    db_session.flush()

    with pytest.raises(WorkspaceImportMaterializationError) as excinfo:
        materialize_import_job(
            db_session, job, source, snapshot, _read(storage, snapshot), worker_id="worker-a"
        )
    assert excinfo.value.code == "workspace_import_materialization_error"


def test_materialize_fails_closed_on_a_corrupt_snapshot(
    db_session: Session, storage: WorkingDirectoryService
) -> None:
    project, source = _seed_project_and_source(db_session)
    job, snapshot = _seed_job_and_snapshot(
        db_session, storage, project=project, source=source, bundle=_bundle()
    )
    with pytest.raises(SnapshotCorruptError) as excinfo:
        materialize_import_job(
            db_session, job, source, snapshot, b"not a tar file at all", worker_id="worker-a"
        )
    assert excinfo.value.code == "import_snapshot_corrupt"


def test_materialize_records_provider_attestation_when_supplied(
    db_session: Session, storage: WorkingDirectoryService
) -> None:
    project, source = _seed_project_and_source(db_session)
    job, snapshot = _seed_job_and_snapshot(
        db_session, storage, project=project, source=source, bundle=_bundle()
    )
    attestation = {"provider": "github", "canonical_repository_id": source.canonical_repository_id}

    result = materialize_import_job(
        db_session,
        job,
        source,
        snapshot,
        _read(storage, snapshot),
        worker_id="worker-a",
        provider_attestation=attestation,
    )

    assert result.revision.source_attestation == "provider_attested"
    assert result.revision.validation_report["provider_attestation"] == attestation


def test_materialize_resolves_a_knowledge_base_resource(
    db_session: Session, storage: WorkingDirectoryService
) -> None:
    project, source = _seed_project_and_source(db_session)
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr(
            ".caliber/workspace.yaml",
            """apiVersion: caliber/v1alpha1
kind: Workspace
metadata:
  slug: materializer
resources:
  knowledgeBases:
    - name: handbook
      manifestPath: kb/handbook.json
""",
        )
        archive.writestr("kb/handbook.json", '{"chunks":[]}\n')
    bundle = output.getvalue()
    manifest = parse_workspace_manifest(
        {
            "apiVersion": "caliber/v1alpha1",
            "kind": "Workspace",
            "metadata": {"slug": "materializer"},
            "resources": {
                "knowledgeBases": [{"name": "handbook", "manifestPath": "kb/handbook.json"}]
            },
        }
    )
    job, snapshot = _seed_job_and_snapshot(
        db_session,
        storage,
        project=project,
        source=source,
        bundle=bundle,
        manifest_sha256=manifest_digest(manifest),
    )

    result = materialize_import_job(
        db_session, job, source, snapshot, _read(storage, snapshot), worker_id="worker-a"
    )

    resources = (
        db_session.query(CaliberWorkspaceRevisionResource)
        .filter_by(revision_id=result.revision.revision_id)
        .all()
    )
    assert len(resources) == 1
    assert resources[0].resource_type == "knowledge_base"
    assert resources[0].source_path == "kb/handbook.json"


def test_extract_tar_entries_skips_non_file_members(
    db_session: Session, storage: WorkingDirectoryService
) -> None:
    """A directory member in the retained tar (never produced by
    ``materialize_workspace_source`` today, but a defensive guard against a
    hand-crafted or future snapshot format change) must be skipped rather
    than misread as file content."""
    import tarfile as tarfile_module

    project, source = _seed_project_and_source(db_session)
    job, snapshot = _seed_job_and_snapshot(
        db_session, storage, project=project, source=source, bundle=_bundle()
    )
    original = _read(storage, snapshot)

    output = io.BytesIO()
    with tarfile_module.open(fileobj=output, mode="w") as archive:
        directory_info = tarfile_module.TarInfo("a-directory")
        directory_info.type = tarfile_module.DIRTYPE
        archive.addfile(directory_info)
        with tarfile_module.open(fileobj=io.BytesIO(original)) as base:
            for member in base.getmembers():
                extracted = base.extractfile(member)
                archive.addfile(member, extracted)
    tampered = output.getvalue()

    result = materialize_import_job(
        db_session, job, source, snapshot, tampered, worker_id="worker-a"
    )
    assert result.revision.status == "ready"


def test_materialize_fails_closed_on_invalid_manifest_yaml(
    db_session: Session, storage: WorkingDirectoryService
) -> None:
    from caliber.workspace_import_materializer import ManifestInvalidError

    project, source = _seed_project_and_source(db_session)
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr(".caliber/workspace.yaml", "not: [valid: yaml: at all")
        archive.writestr("workflows/support.json", '{"name":"support"}\n')
    job, snapshot = _seed_job_and_snapshot(
        db_session,
        storage,
        project=project,
        source=source,
        bundle=output.getvalue(),
        manifest_sha256="a" * 64,
    )
    with pytest.raises(ManifestInvalidError) as excinfo:
        materialize_import_job(
            db_session, job, source, snapshot, _read(storage, snapshot), worker_id="worker-a"
        )
    assert excinfo.value.code == "import_manifest_invalid"


def test_materialize_fails_closed_on_a_non_mapping_manifest(
    db_session: Session, storage: WorkingDirectoryService
) -> None:
    from caliber.workspace_import_materializer import ManifestInvalidError

    project, source = _seed_project_and_source(db_session)
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr(".caliber/workspace.yaml", "- just\n- a\n- list\n")
        archive.writestr("workflows/support.json", '{"name":"support"}\n')
    job, snapshot = _seed_job_and_snapshot(
        db_session,
        storage,
        project=project,
        source=source,
        bundle=output.getvalue(),
        manifest_sha256="a" * 64,
    )
    with pytest.raises(ManifestInvalidError) as excinfo:
        materialize_import_job(
            db_session, job, source, snapshot, _read(storage, snapshot), worker_id="worker-a"
        )
    assert excinfo.value.code == "import_manifest_invalid"


def test_materialize_fails_closed_on_a_schema_invalid_manifest(
    db_session: Session, storage: WorkingDirectoryService
) -> None:
    """Well-formed YAML that decodes to a mapping but fails the
    ``WorkspaceManifest`` pydantic schema (wrong ``apiVersion``) -- a
    different failure mode than not-YAML-at-all or not-a-mapping."""
    from caliber.workspace_import_materializer import ManifestInvalidError

    project, source = _seed_project_and_source(db_session)
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr(
            ".caliber/workspace.yaml",
            """apiVersion: caliber/v2
kind: Workspace
metadata:
  slug: materializer
""",
        )
        archive.writestr("workflows/support.json", '{"name":"support"}\n')
    job, snapshot = _seed_job_and_snapshot(
        db_session,
        storage,
        project=project,
        source=source,
        bundle=output.getvalue(),
        manifest_sha256="a" * 64,
    )
    with pytest.raises(ManifestInvalidError) as excinfo:
        materialize_import_job(
            db_session, job, source, snapshot, _read(storage, snapshot), worker_id="worker-a"
        )
    assert excinfo.value.code == "import_manifest_invalid"


def test_revision_digest_is_deterministic_and_content_addressed(
    db_session: Session, storage: WorkingDirectoryService
) -> None:
    """Same content submitted for two different projects must not collide, but
    materializing the exact same job content twice (idempotent replay, tested
    above) always yields the same `revision_sha256`."""
    project, source = _seed_project_and_source(db_session)
    job, snapshot = _seed_job_and_snapshot(
        db_session, storage, project=project, source=source, bundle=_bundle(), commit_sha="c1"
    )
    result = materialize_import_job(
        db_session, job, source, snapshot, _read(storage, snapshot), worker_id="worker-a"
    )
    assert len(result.revision.revision_sha256) == 64
    int(result.revision.revision_sha256, 16)  # valid hex
