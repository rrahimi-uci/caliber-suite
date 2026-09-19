"""Background worker that claims queued Workspace import jobs and
materializes them into immutable revisions.

Architecture
------------
Mirrors :class:`caliber.knowledge.worker.KnowledgeBaseWorker`: an asyncio
polling loop started/stopped with the app lifespan
(``server.py::_build_lifespan``), ticking on a plain interval, doing its
actual claim/materialize/finish work synchronously inside
``asyncio.to_thread`` so it never blocks the event loop. This worker does
*not* invent a new process-management convention; it is wired into
``server.py`` exactly like the workflow-run, Aria-plan, and knowledge-build
workers already are, gated by its own ``*_worker_enabled`` config flag and
disabled by the same ``background_tasks_enabled`` test switch.

Unlike ``KnowledgeBaseWorker``, this worker does not hand-roll its claim/
lease SQL: :mod:`caliber.workspace_imports` already provides the durable,
tested, lease-fenced primitives (`claim_next_import_job`,
`heartbeat_import_job`, `finish_import_job`,
`reconcile_expired_import_jobs`). This worker is the first real caller of
that queue; the actual materialization logic lives in
:mod:`caliber.workspace_import_materializer` so it can be unit-tested
without any asyncio or worker-loop machinery.

Crash-safety / idempotency
---------------------------
A claimed job is processed in one dedicated SQLAlchemy session. The
materializer (``workspace_import_materializer.materialize_import_job``)
creates the ``WorkspaceRevision``/``WorkspaceRevisionResource`` rows and
calls ``workspace_revisions.finalize_revision`` to flip the revision to
``ready`` -- but it only *flushes*, never commits. This worker then calls
``workspace_imports.finish_import_job`` on the *same session*, which issues
its own lease-fenced UPDATE and commits. That commit is therefore the single
atomic commit point for both the new revision (and its resource pins) and
the job's terminal ``succeeded`` transition: either both land together, or
neither does.

If the process crashes (or is killed) at any point before that commit,
nothing was persisted -- there is no half-created revision sitting in
``validating`` for anything to later mark ``ready``. The job simply stays
``running`` until its lease expires; this worker's own periodic
``reconcile_expired_import_jobs`` call (or another worker's) then moves it
to ``reconcile_required``, the documented "an operator or a later reconcile
command must observe external state before deciding" boundary -- consistent
with how ``workspace_imports.py`` already treats every other lease-expiry
case. A *second* attempt at the same content (whether via a fresh claim of
the same job after an operator's deliberate ``retry_failed_import_job``, or
a different job queued for the same source/commit/bundle content) is safe
by construction: ``workspace_source.assert_source_commit_digest`` is the
materializer's first step, and finds nothing if the earlier attempt never
committed, or replays the existing revision id if it did.

This worker deliberately never calls ``retry_failed_import_job`` itself.
That primitive's own docstring frames a requeue as a *deliberate* action by
an actor with the budget to spend a retry -- not something the worker that
just spent the current attempt should decide on its own.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import threading
from contextlib import suppress
from typing import Any

from sqlalchemy.orm import Session, sessionmaker

from caliber.db.models import (
    CaliberWorkflowFile,
    CaliberWorkspaceImportJob,
    CaliberWorkspaceSource,
)
from caliber.observability.trace import bind_trace_id
from caliber.storage.base import StorageError
from caliber.storage.service import WorkingDirectoryService
from caliber.workspace_import_materializer import (
    WorkspaceImportMaterializationError,
    materialize_import_job,
)
from caliber.workspace_imports import (
    WorkspaceImportLeaseLostError,
    claim_next_import_job,
    finish_import_job,
    heartbeat_import_job,
    reconcile_expired_import_jobs,
)
from caliber.workspace_sources import (
    WorkspaceSourceProviderError,
    WorkspaceSourceProviderRegistry,
)

logger = logging.getLogger("caliber.workspace_import_worker")

# A snapshot read/parse failure or storage error is this worker's analogue of
# a provider "source fetch" failure: the retained bytes could not be turned
# into a usable materialization. See the module docstring on
# ``workspace_import_materializer`` for why there is no real network fetch
# in the currently-delivered ``push`` import mode.
_SOURCE_FETCH_ERROR_CODE = "import_snapshot_unavailable"
_SOURCE_DIGEST_ERROR_CODE = "import_snapshot_digest_mismatch"
_JOB_ROW_MISSING_ERROR_CODE = "import_job_row_missing"


class WorkspaceImportWorker:
    """Claims queued ``CaliberWorkspaceImportJob`` rows and materializes them."""

    def __init__(
        self,
        session_factory: sessionmaker[Session],
        *,
        config: Any,
        storage: WorkingDirectoryService,
        provider_registry: WorkspaceSourceProviderRegistry | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._config = config
        self._storage = storage
        self._provider_registry = provider_registry or WorkspaceSourceProviderRegistry()
        self._interval_seconds = float(config.workspace_import_worker_interval_seconds)
        self._lease_seconds = float(config.workspace_import_lease_seconds)
        from caliber.observability.worker_registry import new_worker_id  # noqa: PLC0415

        self._worker_id = new_worker_id("workspace-import-worker", self)
        self._task: asyncio.Task[None] | None = None
        self._stopped = asyncio.Event()

    async def start(self) -> None:
        if self._task is not None:
            raise RuntimeError("WorkspaceImportWorker.start() called while already running")
        self._stopped.clear()
        self._task = asyncio.create_task(self._run(), name="caliber.workspace_import_worker")
        logger.info(
            "workspace-import worker started (interval=%.1fs lease=%.1fs worker=%s)",
            self._interval_seconds,
            self._lease_seconds,
            self._worker_id,
        )

    async def stop(self, *, grace_seconds: float = 30.0) -> None:
        if self._task is None:
            return
        self._stopped.set()
        try:
            await asyncio.wait_for(asyncio.shield(self._task), timeout=grace_seconds)
        except (TimeoutError, asyncio.TimeoutError):
            logger.warning(
                "workspace-import worker did not stop within %.1fs; cancelling",
                grace_seconds,
            )
            self._task.cancel()
            with suppress(asyncio.CancelledError):
                await self._task
        self._task = None
        logger.info("workspace-import worker stopped")

    async def _run(self) -> None:
        try:
            while not self._stopped.is_set():
                try:
                    await asyncio.to_thread(self._tick)
                except Exception:
                    logger.exception("workspace-import worker tick raised; continuing")
                with suppress(TimeoutError):
                    await asyncio.wait_for(self._stopped.wait(), timeout=self._interval_seconds)
        except asyncio.CancelledError:
            raise

    def _tick(self) -> None:
        with bind_trace_id():
            self._recover_expired_leases()
            with self._session_factory() as session:
                job = claim_next_import_job(
                    session, worker_id=self._worker_id, lease_seconds=self._lease_seconds
                )
            if job is None:
                return
            self._process_job(job.import_job_id)

    def _recover_expired_leases(self) -> None:
        with self._session_factory() as session:
            reconciled = reconcile_expired_import_jobs(session, actor=self._worker_id)
        if reconciled:
            logger.warning(
                "workspace-import worker moved %d expired import job(s) to reconcile_required: %s",
                len(reconciled),
                reconciled,
            )

    def _renew_import_lease(self, job_id: str) -> None:
        with self._session_factory() as session:
            heartbeat_import_job(
                session,
                job_id,
                worker_id=self._worker_id,
                lease_seconds=self._lease_seconds,
            )

    def _heartbeat_loop(self, job_id: str, stop_event: threading.Event) -> None:
        interval = max(5.0, self._lease_seconds / 3.0)
        while not stop_event.wait(interval):
            try:
                self._renew_import_lease(job_id)
            except WorkspaceImportLeaseLostError:
                logger.warning(
                    "workspace-import heartbeat lost the lease for %s; stopping renewal", job_id
                )
                return
            except Exception:
                logger.debug("workspace-import heartbeat failed for %s", job_id, exc_info=True)

    def _process_job(self, job_id: str) -> None:
        heartbeat_stop = threading.Event()
        heartbeat = threading.Thread(
            target=self._heartbeat_loop,
            args=(job_id, heartbeat_stop),
            name=f"caliber-workspace-import-heartbeat-{job_id}",
            daemon=True,
        )
        heartbeat.start()
        try:
            self._materialize_and_finish(job_id)
        finally:
            heartbeat_stop.set()
            heartbeat.join(timeout=5.0)

    def _verify_source_best_effort(
        self, source: CaliberWorkspaceSource
    ) -> dict[str, object] | None:
        """Best-effort source-connection attestation; never blocks materialization.

        No provider is registered in a default deployment
        (``server.py`` never populates ``app.state.workspace_source_registry``
        today), and the content this worker materializes already comes from an
        independently digest-verified retained snapshot -- so an unavailable
        or failing provider is recorded, not fatal. See the module docstring
        on ``workspace_import_materializer`` for why this is the only
        source-provider contract that is actually wired near imports.
        """
        provider = self._provider_registry.get(source.provider)
        if provider is None:
            return None
        try:
            verification = self._provider_registry.verify(source)
        except WorkspaceSourceProviderError as exc:
            logger.info(
                "workspace-import worker: best-effort source verification for %s failed: %s",
                source.source_id,
                exc,
            )
            return None
        return {
            "provider": source.provider,
            "canonical_repository_id": verification.canonical_repository_id,
        }

    def _materialize_and_finish(self, job_id: str) -> None:
        with self._session_factory() as session:
            job = session.get(CaliberWorkspaceImportJob, job_id)
            if job is None:  # pragma: no cover - defensive; claim just proved existence
                logger.error("workspace-import worker: claimed job %s vanished", job_id)
                return
            source = session.get(CaliberWorkspaceSource, job.source_id)
            if source is None:
                self._finish_failed(
                    session,
                    job_id,
                    error_code=_JOB_ROW_MISSING_ERROR_CODE,
                    error_summary=f"workspace source {job.source_id!r} was not found",
                )
                return

            snapshot_id = job.source_snapshot_file_id
            snapshot = session.get(CaliberWorkflowFile, snapshot_id) if snapshot_id else None
            if (
                snapshot is None
                or snapshot.project_id != job.project_id
                or snapshot.deleted_at is not None
            ):
                self._finish_failed(
                    session,
                    job_id,
                    error_code=_SOURCE_FETCH_ERROR_CODE,
                    error_summary="the retained source snapshot is unavailable",
                )
                return

            try:
                data = self._storage.read_bytes(snapshot)
            except StorageError as exc:
                self._finish_failed(
                    session,
                    job_id,
                    error_code=_SOURCE_FETCH_ERROR_CODE,
                    error_summary=f"reading the retained source snapshot failed: {exc}",
                )
                return

            observed_sha256 = hashlib.sha256(data).hexdigest()
            if snapshot.sha256 != observed_sha256:
                self._finish_failed(
                    session,
                    job_id,
                    error_code=_SOURCE_DIGEST_ERROR_CODE,
                    error_summary="the retained source snapshot failed integrity verification",
                )
                return

            provider_attestation = self._verify_source_best_effort(source)

            try:
                result = materialize_import_job(
                    session,
                    job,
                    source,
                    snapshot,
                    data,
                    worker_id=self._worker_id,
                    provider_attestation=provider_attestation,
                )
            except WorkspaceImportMaterializationError as exc:
                session.rollback()
                self._finish_failed(
                    session,
                    job_id,
                    error_code=getattr(exc, "code", "workspace_import_materialization_error"),
                    error_summary=str(exc),
                )
                return

            finish_import_job(
                session,
                job_id,
                worker_id=self._worker_id,
                status="succeeded",
                revision_id=result.revision.revision_id,
            )
            logger.info(
                "workspace-import worker materialized %s -> revision %s (replayed=%s)",
                job_id,
                result.revision.revision_id,
                result.replayed,
            )

    def _finish_failed(
        self, session: Session, job_id: str, *, error_code: str, error_summary: str
    ) -> None:
        try:
            finish_import_job(
                session,
                job_id,
                worker_id=self._worker_id,
                status="failed",
                error_code=error_code,
                error_summary=error_summary[:2048],
            )
        except WorkspaceImportLeaseLostError:
            logger.warning(
                "workspace-import worker lost the lease for %s before it could record "
                "failure %r; leaving it for lease-expiry reconciliation",
                job_id,
                error_code,
            )


__all__ = ["WorkspaceImportWorker"]
