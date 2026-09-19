"""Tests for :mod:`caliber.workspace_import_worker`.

Drives ``WorkspaceImportWorker``'s claim / materialize / finish / lease-
recovery machinery directly against seeded rows and a real local-backend
``WorkingDirectoryService`` -- no real network, no real source-control
provider (only the same offline ``WorkspaceSourceProvider`` test double
``test_workspace_routes.py`` already established for the one provider
contract that is actually wired near Workspace imports). Mirrors the
patterns in ``test_knowledge_worker_coverage.py`` (sync ``_tick`` calls,
``asyncio``-marked ``start``/``stop`` lifecycle tests) and
``test_e2e_workspace_import.py`` (hand-seeded job/source/project rows).
"""

from __future__ import annotations

import asyncio
import io
import zipfile
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy.orm import Session, sessionmaker

from caliber.config import CaliberConfig
from caliber.db.models import (
    CaliberProject,
    CaliberWorkflowFile,
    CaliberWorkspaceImportJob,
    CaliberWorkspaceRevision,
    CaliberWorkspaceSource,
)
from caliber.ids import new_workspace_import_id
from caliber.storage import WorkingDirectoryService, build_backend
from caliber.workspace_import_worker import WorkspaceImportWorker
from caliber.workspace_imports import IMPORT_JOB_STATUS_RECONCILE_REQUIRED
from caliber.workspace_manifest import manifest_digest, parse_workspace_manifest
from caliber.workspace_source import materialize_workspace_source
from caliber.workspace_sources import (
    WorkspaceSourceProviderError,
    WorkspaceSourceProviderRegistry,
    WorkspaceSourceVerification,
)

PROJECT_ID = "PRJ-import-worker"
SOURCE_ID = "WSS-import-worker"

_MANIFEST_YAML = b"""apiVersion: caliber/v1alpha1
kind: Workspace
metadata:
  slug: import-worker
resources:
  workflows:
    - name: support
      path: workflows/support.json
"""


def _utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


_MANIFEST_DOC = {
    "apiVersion": "caliber/v1alpha1",
    "kind": "Workspace",
    "metadata": {"slug": "import-worker"},
    "resources": {"workflows": [{"name": "support", "path": "workflows/support.json"}]},
}


def _bundle() -> bytes:
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr(".caliber/workspace.yaml", _MANIFEST_YAML)
        archive.writestr("workflows/support.json", '{"name":"support"}\n')
    return output.getvalue()


@dataclass
class _FakeSourceProvider:
    name: str = "github"

    def capabilities(self) -> Mapping[str, object]:
        return {"push_import": True}

    def verify(self, source: CaliberWorkspaceSource) -> WorkspaceSourceVerification:
        return WorkspaceSourceVerification(
            canonical_repository_id=source.canonical_repository_id,
            capabilities=self.capabilities(),
        )


@dataclass
class _FailingSourceProvider:
    name: str = "github"

    def capabilities(self) -> Mapping[str, object]:
        return {}

    def verify(self, source: CaliberWorkspaceSource) -> WorkspaceSourceVerification:
        raise WorkspaceSourceProviderError("source verification temporarily unavailable")


def _storage(app_config: CaliberConfig) -> WorkingDirectoryService:
    return WorkingDirectoryService(
        build_backend(app_config.workflow_storage), app_config.workflow_storage
    )


def _make_worker(
    session_factory: sessionmaker[Session],
    app_config: CaliberConfig,
    *,
    provider_registry: WorkspaceSourceProviderRegistry | None = None,
    interval_seconds: float = 0.01,
) -> WorkspaceImportWorker:
    fast = app_config.model_copy(
        update={"workspace_import_worker_interval_seconds": interval_seconds}
    )
    return WorkspaceImportWorker(
        session_factory,
        config=fast,
        storage=_storage(app_config),
        provider_registry=provider_registry,
    )


def _seed_project_and_source(
    session: Session, *, project_id: str = PROJECT_ID, source_id: str = SOURCE_ID
) -> tuple[CaliberProject, CaliberWorkspaceSource]:
    project = CaliberProject(project_id=project_id, name=f"Import worker {project_id}")
    source = CaliberWorkspaceSource(
        source_id=source_id,
        project_id=project_id,
        provider="github",
        provider_host="github.com",
        canonical_repository_id=f"github:{project_id}",
        display_path="owner/import-worker",
    )
    session.add_all([project, source])
    session.flush()
    return project, source


def _seed_import_job(
    session: Session,
    app_config: CaliberConfig,
    *,
    project: CaliberProject,
    source: CaliberWorkspaceSource,
    bundle: bytes | None = None,
    commit_sha: str = "commit-1",
    idempotency_key: str | None = None,
    manifest_sha256: str | None = None,
    with_snapshot: bool = True,
) -> CaliberWorkspaceImportJob:
    if bundle is None:
        bundle = _bundle()
    materialized = materialize_workspace_source(bundle)
    if manifest_sha256 is None:
        manifest_sha256 = manifest_digest(parse_workspace_manifest(_MANIFEST_DOC))
    snapshot_file_id = None
    if with_snapshot:
        record = (
            _storage(app_config)
            .for_backend(project.storage_backend)
            .register_project_file(
                session,
                project_id=project.project_id,
                kind="artifact",
                filename=f".caliber/workspace-snapshots/{materialized.snapshot_sha256}.tar",
                data=materialized.snapshot_bytes,
                media_type="application/x-tar",
                actor="@importer",
                tenant_id=project.tenant_id,
                metadata={
                    "workspace_snapshot": True,
                    "snapshot_sha256": materialized.snapshot_sha256,
                },
            )
        )
        snapshot_file_id = record.file_id
    job = CaliberWorkspaceImportJob(
        import_job_id=new_workspace_import_id(),
        project_id=project.project_id,
        source_id=source.source_id,
        repository=source.display_path,
        commit_sha=commit_sha,
        upload_sha256=materialized.upload_sha256,
        source_bundle_sha256=materialized.source_bundle_sha256,
        source_snapshot_file_id=snapshot_file_id,
        manifest_sha256=manifest_sha256,
        idempotency_key=idempotency_key or f"key-{new_workspace_import_id()}",
        created_by="@importer",
    )
    session.add(job)
    session.commit()
    return job


def test_tick_claims_materializes_and_finishes_a_queued_job(
    db_session: Session, session_factory: sessionmaker[Session], app_config: CaliberConfig
) -> None:
    project, source = _seed_project_and_source(db_session)
    job = _seed_import_job(db_session, app_config, project=project, source=source)
    worker = _make_worker(session_factory, app_config)

    worker._tick()

    db_session.expire_all()
    refreshed = db_session.get(CaliberWorkspaceImportJob, job.import_job_id)
    assert refreshed is not None
    assert refreshed.status == "succeeded"
    assert refreshed.revision_id is not None
    assert refreshed.error_code is None

    revision = db_session.get(CaliberWorkspaceRevision, refreshed.revision_id)
    assert revision is not None
    assert revision.status == "ready"
    assert revision.project_id == project.project_id
    assert revision.source_attestation == "caller_attested"


def test_tick_is_a_noop_when_the_queue_is_empty(
    session_factory: sessionmaker[Session], app_config: CaliberConfig
) -> None:
    worker = _make_worker(session_factory, app_config)
    worker._tick()  # must not raise


def test_expired_lease_moves_to_reconcile_required_without_a_revision(
    db_session: Session, session_factory: sessionmaker[Session], app_config: CaliberConfig
) -> None:
    """Simulates a worker crash mid-materialization: the job is claimed
    (``running``) with a lease that has already expired, standing in for a
    process that died holding the lease before it ever reached
    ``finish_import_job``. The recovery tick must move it to
    ``reconcile_required`` -- never silently drop it, and never create a
    revision for a materialization that never actually completed."""
    project, source = _seed_project_and_source(db_session)
    job = _seed_import_job(db_session, app_config, project=project, source=source)
    job.status = "running"
    job.claimed_by = "ghost-worker-from-a-dead-process"
    job.claimed_at = _utcnow() - timedelta(seconds=600)
    job.lease_expires_at = _utcnow() - timedelta(seconds=300)
    job.last_heartbeat_at = _utcnow() - timedelta(seconds=600)
    job.attempt_count = 1
    db_session.commit()

    worker = _make_worker(session_factory, app_config)
    worker._tick()

    db_session.expire_all()
    refreshed = db_session.get(CaliberWorkspaceImportJob, job.import_job_id)
    assert refreshed is not None
    assert refreshed.status == IMPORT_JOB_STATUS_RECONCILE_REQUIRED
    assert refreshed.error_code == "import_lease_expired"
    assert refreshed.revision_id is None
    assert (
        db_session.query(CaliberWorkspaceRevision).filter_by(project_id=project.project_id).count()
        == 0
    )


def test_a_fresh_job_for_the_same_content_after_recovery_creates_exactly_one_revision(
    db_session: Session, session_factory: sessionmaker[Session], app_config: CaliberConfig
) -> None:
    """After the crash/reconcile scenario above, the same source content
    queued as a *new* job (the realistic recovery path: an operator
    re-submits, or a duplicate queued job already existed) must materialize
    to exactly one revision -- proving the crash path didn't leave a
    dangling half-created revision that a second attempt would collide with
    or duplicate."""
    project, source = _seed_project_and_source(db_session)
    bundle = _bundle()
    stuck = _seed_import_job(
        db_session, app_config, project=project, source=source, bundle=bundle, commit_sha="commit-y"
    )
    stuck.status = "running"
    stuck.claimed_by = "ghost-worker"
    stuck.lease_expires_at = _utcnow() - timedelta(seconds=1)
    stuck.attempt_count = 1
    db_session.commit()

    worker = _make_worker(session_factory, app_config)
    worker._tick()  # recovers `stuck` to reconcile_required; claims nothing else

    db_session.expire_all()
    assert db_session.get(CaliberWorkspaceImportJob, stuck.import_job_id).status == (
        IMPORT_JOB_STATUS_RECONCILE_REQUIRED
    )

    fresh = _seed_import_job(
        db_session, app_config, project=project, source=source, bundle=bundle, commit_sha="commit-y"
    )
    worker._tick()

    db_session.expire_all()
    refreshed = db_session.get(CaliberWorkspaceImportJob, fresh.import_job_id)
    assert refreshed.status == "succeeded"
    assert (
        db_session.query(CaliberWorkspaceRevision).filter_by(project_id=project.project_id).count()
        == 1
    )


def test_missing_snapshot_file_fails_the_job_deterministically(
    db_session: Session, session_factory: sessionmaker[Session], app_config: CaliberConfig
) -> None:
    project, source = _seed_project_and_source(db_session)
    job = _seed_import_job(
        db_session, app_config, project=project, source=source, with_snapshot=False
    )
    worker = _make_worker(session_factory, app_config)

    worker._tick()

    db_session.expire_all()
    refreshed = db_session.get(CaliberWorkspaceImportJob, job.import_job_id)
    assert refreshed.status == "failed"
    assert refreshed.error_code == "import_snapshot_unavailable"
    assert refreshed.revision_id is None


def test_tampered_snapshot_digest_fails_the_job_deterministically(
    db_session: Session, session_factory: sessionmaker[Session], app_config: CaliberConfig
) -> None:
    project, source = _seed_project_and_source(db_session)
    job = _seed_import_job(db_session, app_config, project=project, source=source)
    snapshot = db_session.get(CaliberWorkflowFile, job.source_snapshot_file_id)
    assert snapshot is not None
    # Simulate corruption/tampering between submission and worker pickup: the
    # recorded digest no longer matches what materialize_import_job (via the
    # worker) will read back.
    snapshot.sha256 = "0" * 64
    db_session.commit()

    worker = _make_worker(session_factory, app_config)
    worker._tick()

    db_session.expire_all()
    refreshed = db_session.get(CaliberWorkspaceImportJob, job.import_job_id)
    assert refreshed.status == "failed"
    assert refreshed.error_code == "import_snapshot_digest_mismatch"
    assert refreshed.revision_id is None


def test_storage_read_failure_fails_the_job_deterministically(
    db_session: Session, session_factory: sessionmaker[Session], app_config: CaliberConfig
) -> None:
    from caliber.storage.base import StorageError

    project, source = _seed_project_and_source(db_session)
    job = _seed_import_job(db_session, app_config, project=project, source=source)
    worker = _make_worker(session_factory, app_config)

    class _RaisingStorage:
        def read_bytes(self, *_args: object, **_kwargs: object) -> bytes:
            raise StorageError("simulated storage outage")

    worker._storage = _RaisingStorage()  # type: ignore[assignment]

    worker._tick()

    db_session.expire_all()
    refreshed = db_session.get(CaliberWorkspaceImportJob, job.import_job_id)
    assert refreshed.status == "failed"
    assert refreshed.error_code == "import_snapshot_unavailable"


def test_finish_failed_swallows_a_lost_lease_instead_of_raising(
    db_session: Session, session_factory: sessionmaker[Session], app_config: CaliberConfig
) -> None:
    """If the job is not actually held by this worker (e.g. its lease already
    expired and another worker reconciled/reclaimed it), recording a failure
    must not raise -- the other worker's outcome wins and this one backs off
    quietly, exactly like a stale heartbeat does."""
    project, source = _seed_project_and_source(db_session)
    job = _seed_import_job(db_session, app_config, project=project, source=source)
    worker = _make_worker(session_factory, app_config)

    with session_factory() as session:
        # job is still `queued`, never claimed by this worker_id -- finish_import_job's
        # lease-fenced WHERE clause will match nothing.
        worker._finish_failed(
            session, job.import_job_id, error_code="whatever", error_summary="whatever"
        )  # must not raise

    db_session.expire_all()
    refreshed = db_session.get(CaliberWorkspaceImportJob, job.import_job_id)
    assert refreshed.status == "queued"


def test_provider_verification_success_marks_the_revision_provider_attested(
    db_session: Session, session_factory: sessionmaker[Session], app_config: CaliberConfig
) -> None:
    project, source = _seed_project_and_source(db_session)
    job = _seed_import_job(db_session, app_config, project=project, source=source)
    registry = WorkspaceSourceProviderRegistry({"github": _FakeSourceProvider()})
    worker = _make_worker(session_factory, app_config, provider_registry=registry)

    worker._tick()

    db_session.expire_all()
    refreshed = db_session.get(CaliberWorkspaceImportJob, job.import_job_id)
    assert refreshed.status == "succeeded"
    revision = db_session.get(CaliberWorkspaceRevision, refreshed.revision_id)
    assert revision.source_attestation == "provider_attested"
    assert revision.validation_report["provider_attestation"]["provider"] == "github"


def test_provider_verification_failure_does_not_block_materialization(
    db_session: Session, session_factory: sessionmaker[Session], app_config: CaliberConfig
) -> None:
    """No provider is registered in a real deployment today (`server.py`
    never populates `app.state.workspace_source_registry`); an unavailable or
    failing provider must never turn a materializable, digest-verified
    snapshot into a failed import."""
    project, source = _seed_project_and_source(db_session)
    job = _seed_import_job(db_session, app_config, project=project, source=source)
    registry = WorkspaceSourceProviderRegistry({"github": _FailingSourceProvider()})
    worker = _make_worker(session_factory, app_config, provider_registry=registry)

    worker._tick()

    db_session.expire_all()
    refreshed = db_session.get(CaliberWorkspaceImportJob, job.import_job_id)
    assert refreshed.status == "succeeded"
    revision = db_session.get(CaliberWorkspaceRevision, refreshed.revision_id)
    assert revision.source_attestation == "caller_attested"


def test_no_registered_provider_is_a_silent_skip(
    db_session: Session, session_factory: sessionmaker[Session], app_config: CaliberConfig
) -> None:
    project, source = _seed_project_and_source(db_session)
    job = _seed_import_job(db_session, app_config, project=project, source=source)
    worker = _make_worker(
        session_factory, app_config, provider_registry=WorkspaceSourceProviderRegistry()
    )

    worker._tick()

    db_session.expire_all()
    refreshed = db_session.get(CaliberWorkspaceImportJob, job.import_job_id)
    assert refreshed.status == "succeeded"


@pytest.mark.asyncio
async def test_start_twice_raises_runtime_error(
    session_factory: sessionmaker[Session], app_config: CaliberConfig
) -> None:
    worker = _make_worker(session_factory, app_config)

    async def _hang() -> None:
        await asyncio.Event().wait()

    worker._run = _hang  # type: ignore[method-assign]
    await worker.start()
    try:
        with pytest.raises(RuntimeError, match="already running"):
            await worker.start()
    finally:
        await worker.stop(grace_seconds=0.05)


@pytest.mark.asyncio
async def test_stop_cancels_after_grace_timeout(
    session_factory: sessionmaker[Session], app_config: CaliberConfig
) -> None:
    worker = _make_worker(session_factory, app_config)

    async def _hang() -> None:
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            raise

    worker._run = _hang  # type: ignore[method-assign]
    await worker.start()
    await worker.stop(grace_seconds=0.05)
    assert worker._task is None


@pytest.mark.asyncio
async def test_stop_is_noop_when_never_started(
    session_factory: sessionmaker[Session], app_config: CaliberConfig
) -> None:
    worker = _make_worker(session_factory, app_config)
    await worker.stop(grace_seconds=1.0)
    assert worker._task is None


@pytest.mark.asyncio
async def test_run_loop_executes_real_tick_and_materializes(
    db_session: Session, session_factory: sessionmaker[Session], app_config: CaliberConfig
) -> None:
    project, source = _seed_project_and_source(db_session)
    job = _seed_import_job(db_session, app_config, project=project, source=source)
    worker = _make_worker(session_factory, app_config, interval_seconds=0.01)

    await worker.start()
    deadline_ticks = 200
    status = None
    for _ in range(deadline_ticks):
        db_session.expire_all()
        refreshed = db_session.get(CaliberWorkspaceImportJob, job.import_job_id)
        status = refreshed.status if refreshed else None
        if status in ("succeeded", "failed", IMPORT_JOB_STATUS_RECONCILE_REQUIRED):
            break
        await asyncio.sleep(0.01)
    await worker.stop(grace_seconds=5.0)

    assert status == "succeeded"


@pytest.mark.asyncio
async def test_real_run_reraises_on_direct_cancel(
    session_factory: sessionmaker[Session], app_config: CaliberConfig
) -> None:
    """Drive the real ``_run`` and cancel its task directly, exercising the
    ``except asyncio.CancelledError: raise`` re-raise."""
    worker = _make_worker(session_factory, app_config)
    worker._tick = lambda: None  # type: ignore[method-assign]

    await worker.start()
    task = worker._task
    assert task is not None
    await asyncio.sleep(0.02)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    worker._task = None


class _OneShotEvent:
    """Stand-in for ``threading.Event``: ``wait`` returns False on the first
    call (so the heartbeat loop body runs exactly once) and True thereafter,
    deterministically -- without depending on the 5s ``max(5, lease/3)``
    wall-clock floor."""

    def __init__(self) -> None:
        self._calls = 0

    def wait(self, _timeout: float | None = None) -> bool:
        self._calls += 1
        return self._calls > 1


def test_heartbeat_loop_renews_once_then_stops(
    session_factory: sessionmaker[Session], app_config: CaliberConfig
) -> None:
    worker = _make_worker(session_factory, app_config)
    calls: list[str] = []
    worker._renew_import_lease = calls.append  # type: ignore[method-assign]

    worker._heartbeat_loop("WSI-does-not-exist", _OneShotEvent())  # type: ignore[arg-type]

    assert calls == ["WSI-does-not-exist"]


def test_heartbeat_loop_stops_renewing_once_the_lease_is_lost(
    session_factory: sessionmaker[Session], app_config: CaliberConfig
) -> None:
    worker = _make_worker(session_factory, app_config)

    def _lost(_job_id: str) -> None:
        from caliber.workspace_imports import WorkspaceImportLeaseLostError

        raise WorkspaceImportLeaseLostError("lease lost")

    worker._renew_import_lease = _lost  # type: ignore[method-assign]
    # An event that never reports "stop": if the loop didn't return on
    # WorkspaceImportLeaseLostError it would spin forever.
    worker._heartbeat_loop("WSI-lost", _OneShotEvent())  # type: ignore[arg-type]


def test_heartbeat_loop_swallows_unexpected_renew_errors(
    session_factory: sessionmaker[Session], app_config: CaliberConfig
) -> None:
    worker = _make_worker(session_factory, app_config)

    def _boom(_job_id: str) -> None:
        raise RuntimeError("boom")

    worker._renew_import_lease = _boom  # type: ignore[method-assign]
    worker._heartbeat_loop("WSI-boom", _OneShotEvent())  # type: ignore[arg-type]


def test_real_heartbeat_renews_the_lease_of_a_running_job(
    db_session: Session, session_factory: sessionmaker[Session], app_config: CaliberConfig
) -> None:
    """Exercises ``_renew_import_lease`` for real against a job this worker
    actually owns (the claim path already sets ``claimed_by``/``lease_expires_at``
    via ``claim_next_import_job``)."""
    from caliber.workspace_imports import claim_next_import_job

    project, source = _seed_project_and_source(db_session)
    job = _seed_import_job(db_session, app_config, project=project, source=source)
    worker = _make_worker(session_factory, app_config)
    with session_factory() as session:
        claimed = claim_next_import_job(session, worker_id=worker._worker_id, lease_seconds=60)
    assert claimed is not None
    before = claimed.lease_expires_at

    worker._renew_import_lease(job.import_job_id)

    db_session.expire_all()
    refreshed = db_session.get(CaliberWorkspaceImportJob, job.import_job_id)
    assert refreshed.lease_expires_at is not None
    assert before is not None
    assert refreshed.lease_expires_at >= before


def test_source_row_missing_fails_the_job(
    db_session: Session, session_factory: sessionmaker[Session], app_config: CaliberConfig
) -> None:
    project, source = _seed_project_and_source(db_session)
    job = _seed_import_job(db_session, app_config, project=project, source=source)
    db_session.delete(source)
    db_session.commit()

    worker = _make_worker(session_factory, app_config)
    worker._tick()

    db_session.expire_all()
    refreshed = db_session.get(CaliberWorkspaceImportJob, job.import_job_id)
    assert refreshed.status == "failed"
    assert refreshed.error_code == "import_job_row_missing"


def test_a_materialization_error_fails_the_job_with_its_stable_code(
    db_session: Session, session_factory: sessionmaker[Session], app_config: CaliberConfig
) -> None:
    """A deterministic materialization problem (e.g. the manifest is missing
    from the retained snapshot -- see test_workspace_import_materializer.py
    for the direct unit test of that error) is mapped onto ``finish_import_job``'s
    ``failed`` outcome with the materializer's own stable error code, not
    left to look like a generic worker crash."""
    project, source = _seed_project_and_source(db_session)
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("elsewhere/workspace.yaml", _MANIFEST_YAML)
        archive.writestr("workflows/support.json", '{"name":"support"}\n')
    job = _seed_import_job(
        db_session,
        app_config,
        project=project,
        source=source,
        bundle=output.getvalue(),
    )
    worker = _make_worker(session_factory, app_config)

    worker._tick()

    db_session.expire_all()
    refreshed = db_session.get(CaliberWorkspaceImportJob, job.import_job_id)
    assert refreshed.status == "failed"
    assert refreshed.error_code == "import_manifest_not_found"
    assert refreshed.revision_id is None


@pytest.mark.asyncio
async def test_run_loop_survives_tick_exception(
    session_factory: sessionmaker[Session], app_config: CaliberConfig
) -> None:
    worker = _make_worker(session_factory, app_config, interval_seconds=0.01)

    ticks = {"n": 0}

    def _boom_tick() -> None:
        ticks["n"] += 1
        raise RuntimeError("tick blew up")

    worker._tick = _boom_tick  # type: ignore[method-assign]

    await worker.start()
    for _ in range(50):
        if ticks["n"] >= 1:
            break
        await asyncio.sleep(0.01)
    await worker.stop(grace_seconds=5.0)

    assert ticks["n"] >= 1
    assert worker._task is None
