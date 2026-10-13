"""Stale-job janitor.

A background task that finds refinement jobs stuck in ``status='running'``
with a stale ``last_heartbeat_at`` and marks them as ``failed`` with a
diagnostic message. The motivating scenario is a SIGKILL'd worker mid-
stage — without the janitor, the job sits in ``running`` forever and
nothing else picks it up (the atomic-claim path only sees
``status='queued'``).

Defaults:

* :data:`DEFAULT_INTERVAL_SECONDS` — 60s between sweeps. Long enough
  that the DB load stays negligible, short enough that a crashed worker
  is reaped within a few minutes.
* :data:`DEFAULT_STALE_THRESHOLD_SECONDS` — 300s (5 min). Must be
  comfortably longer than the longest expected single stage so an LLM
  call that runs for 90 seconds doesn't get reaped prematurely.

The reap is idempotent: each affected row gets exactly one audit entry
(``action="reap_stale_job"``), the same way the worker's own
``fail_job`` path writes a single entry per failure.

A second sweep releases jobs stranded in ``status='applying'``. Apply commits
its claim (``candidate_ready`` to ``applying``) before it calls the promoter, so
a process that dies in that window leaves a job no request or reconciler will
touch: the release reconciler scans release operations, and none exists yet.
A job that has sat in ``applying`` past :data:`DEFAULT_APPLYING_STALE_SECONDS`
with **no** release operation referencing any of its approvals is returned to
``candidate_ready`` (``action="revert_stale_applying"``). A job whose operation
exists is never touched here; the reconciler owns it.
"""

from __future__ import annotations

import asyncio
import logging
from contextlib import suppress
from datetime import datetime, timedelta, timezone

from sqlalchemy import exists, or_, select, update
from sqlalchemy.orm import Session, sessionmaker

from caliber.audit import record as audit_record
from caliber.db.models import (
    CaliberApprovalRequest,
    CaliberRefinementJob,
    CaliberReleaseOperation,
)
from caliber.observability import metrics
from caliber.observability.trace import bind_trace_id

logger = logging.getLogger("caliber.orchestrator.janitor")

DEFAULT_INTERVAL_SECONDS = 60.0
DEFAULT_STALE_THRESHOLD_SECONDS = 300.0
#: How long a job may sit in ``applying`` with no release operation before it is
#: returned to ``candidate_ready``. Deliberately much longer than the running-job
#: threshold: a live Apply spends this time inside a provider call, and an
#: unreachable MLflow can legitimately take several minutes to give up.
DEFAULT_APPLYING_STALE_SECONDS = 1800.0


class JanitorTask:
    """Periodic sweeper for stuck-in-running refinement jobs.

    Parameters
    ----------
    session_factory:
        Bound to the same engine as the rest of the app.
    interval_seconds:
        Time between sweeps. Defaults to
        :data:`DEFAULT_INTERVAL_SECONDS`.
    stale_threshold_seconds:
        A job is considered crashed if its ``last_heartbeat_at`` is
        older than ``now - stale_threshold_seconds``.
    """

    def __init__(
        self,
        session_factory: sessionmaker[Session],
        interval_seconds: float = DEFAULT_INTERVAL_SECONDS,
        stale_threshold_seconds: float = DEFAULT_STALE_THRESHOLD_SECONDS,
        workflow_run_retention_days: float = 0.0,
        applying_stale_seconds: float = DEFAULT_APPLYING_STALE_SECONDS,
    ) -> None:
        self._session_factory = session_factory
        self._applying_stale = timedelta(seconds=applying_stale_seconds)
        self._interval_seconds = interval_seconds
        self._stale_threshold = timedelta(seconds=stale_threshold_seconds)
        # Retention for the workflow-run index (ext D2); 0 disables pruning.
        self._workflow_run_retention_days = workflow_run_retention_days
        self._task: asyncio.Task[None] | None = None
        self._stopped = asyncio.Event()

    async def start(self) -> None:
        if self._task is not None:
            raise RuntimeError("JanitorTask.start() called while already running")
        self._stopped.clear()
        self._task = asyncio.create_task(self._run(), name="caliber.janitor")
        logger.info(
            "janitor started (interval=%.1fs, stale_threshold=%ds)",
            self._interval_seconds,
            int(self._stale_threshold.total_seconds()),
        )

    async def stop(self) -> None:
        """Stop the janitor and wait for the current tick to finish.

        The janitor is cheap and short-lived (single sweep, no LLM
        calls), so we don't need a graceful drain — a hard cancel after
        signalling ``_stopped`` is fine.
        """
        if self._task is None:
            return
        self._stopped.set()
        self._task.cancel()
        with suppress(asyncio.CancelledError):
            await self._task
        self._task = None
        logger.info("janitor stopped")

    async def _run(self) -> None:
        try:
            while not self._stopped.is_set():
                try:
                    await asyncio.to_thread(self._tick)
                except Exception:
                    logger.exception("janitor tick raised; continuing")
                with suppress(TimeoutError):
                    await asyncio.wait_for(self._stopped.wait(), timeout=self._interval_seconds)
        except asyncio.CancelledError:
            raise

    # ------------------------------------------------------------------
    # Sync work
    # ------------------------------------------------------------------

    def _tick(self) -> None:
        """One sweep — find every stale ``running`` job and reap it."""
        with bind_trace_id():
            self._tick_inner()

    def _tick_inner(self) -> None:
        cutoff = datetime.now(timezone.utc) - self._stale_threshold
        with self._session_factory() as session:
            candidates = self._find_stale(session, cutoff)
            for job in candidates:
                self._reap(session, job, cutoff)
            reverted = self._revert_stale_applying(session)
            # Prune the workflow-run index past its retention window (ext D2).
            if self._workflow_run_retention_days > 0:
                from caliber.workflows.promoter import prune_workflow_runs  # noqa: PLC0415

                pruned = prune_workflow_runs(
                    session, retention_days=self._workflow_run_retention_days
                )
                if pruned:
                    logger.info("janitor pruned %d expired workflow-run rows", pruned)
            if candidates or reverted or self._workflow_run_retention_days > 0:
                session.commit()

    def _revert_stale_applying(self, session: Session) -> int:
        """Return jobs stranded in ``applying`` (no release operation) to ``candidate_ready``.

        Each revert is a conditional UPDATE that re-checks the status and the
        staleness atomically, so an Apply that finished or moved on between the
        SELECT and the UPDATE is left alone. Returns the number reverted.
        """
        cutoff = datetime.now(timezone.utc) - self._applying_stale
        has_operation = exists(
            select(CaliberReleaseOperation.operation_id)
            .join(
                CaliberApprovalRequest,
                CaliberApprovalRequest.approval_id == CaliberReleaseOperation.approval_id,
            )
            .where(CaliberApprovalRequest.job_id == CaliberRefinementJob.job_id)
            .correlate(CaliberRefinementJob)
        )
        stranded = session.execute(
            select(
                CaliberRefinementJob.job_id,
                CaliberRefinementJob.agent_id,
                CaliberRefinementJob.updated_at,
            )
            .where(CaliberRefinementJob.status == "applying")
            .where(CaliberRefinementJob.updated_at < cutoff)
            .where(~has_operation)
        ).all()
        reverted = 0
        for job_id, agent_id, updated_at in stranded:
            result = session.execute(
                update(CaliberRefinementJob)
                .where(CaliberRefinementJob.job_id == job_id)
                .where(CaliberRefinementJob.status == "applying")
                .where(CaliberRefinementJob.updated_at < cutoff)
                .where(~has_operation)
                .values(status="candidate_ready")
            )
            if int(getattr(result, "rowcount", 0) or 0) != 1:
                continue
            reverted += 1
            audit_record(
                session,
                actor="caliber.janitor",
                action="revert_stale_applying",
                entity_type="refinement_job",
                entity_id=job_id,
                details={
                    "agent_id": agent_id,
                    "last_update": updated_at.isoformat() if updated_at else None,
                    "applying_stale_seconds": int(self._applying_stale.total_seconds()),
                },
            )
            logger.warning(
                "reverted job=%s from applying to candidate_ready: no release operation "
                "after %ds (process presumably died mid-Apply)",
                job_id,
                int(self._applying_stale.total_seconds()),
            )
        return reverted

    def _find_stale(self, session: Session, cutoff: datetime) -> list[CaliberRefinementJob]:
        """Return jobs whose heartbeat *appears* stale.

        Returns are *candidates* — the per-row UPDATE in :meth:`_reap`
        re-checks the heartbeat predicate atomically (so a worker that
        beat between this SELECT and the UPDATE has its row left
        untouched). The double-check eliminates the TOCTOU window
        where a live worker would otherwise have a healthy job
        spuriously failed.

        We treat a ``NULL`` heartbeat as "never beat" only when the
        job's ``updated_at`` is also older than the cutoff — that way
        an in-flight job whose first heartbeat hasn't landed yet
        doesn't get incorrectly reaped.
        """
        stmt = (
            select(CaliberRefinementJob)
            .where(CaliberRefinementJob.status == "running")
            .where(
                (
                    CaliberRefinementJob.last_heartbeat_at.is_not(None)
                    & (CaliberRefinementJob.last_heartbeat_at < cutoff)
                )
                | (
                    CaliberRefinementJob.last_heartbeat_at.is_(None)
                    & (CaliberRefinementJob.updated_at < cutoff)
                )
            )
        )
        return list(session.execute(stmt).scalars().all())

    def _reap(self, session: Session, job: CaliberRefinementJob, cutoff: datetime) -> None:
        """Atomically mark one stale job as failed if its heartbeat is still stale.

        The candidate was loaded by :meth:`_find_stale`, but a live
        worker could have written a fresh ``last_heartbeat_at``
        between that SELECT and now. We issue an
        ``UPDATE ... WHERE``-with-heartbeat-predicate so the failure
        only lands when the row is *still* stale at the time of the
        write. ``returning(job_id)`` lets us cheaply detect the
        no-op case (the row raced with a heartbeat and recovered).

        Side effects (audit row, metric, log) are skipped when the
        UPDATE matched zero rows — without this, a recovered job
        would still get a false-positive ``reap_stale_job`` audit
        entry. The job's ``current_stage`` is preserved on the row so
        an operator can see where the crash happened.
        """
        heartbeat = (
            job.last_heartbeat_at.isoformat() if job.last_heartbeat_at is not None else "never"
        )
        previous_stage = job.current_stage
        error_message = (
            f"heartbeat stale (last seen {heartbeat}); worker presumed crashed "
            f"during stage {previous_stage!r}"
        )

        # Detach the ORM-loaded row so an issued UPDATE doesn't fight
        # SQLAlchemy's auto-flush for the same primary key.
        session.expunge(job)

        stmt = (
            update(CaliberRefinementJob)
            .where(CaliberRefinementJob.job_id == job.job_id)
            .where(CaliberRefinementJob.status == "running")
            .where(
                or_(
                    (
                        CaliberRefinementJob.last_heartbeat_at.is_not(None)
                        & (CaliberRefinementJob.last_heartbeat_at < cutoff)
                    ),
                    (
                        CaliberRefinementJob.last_heartbeat_at.is_(None)
                        & (CaliberRefinementJob.updated_at < cutoff)
                    ),
                )
            )
            .values(status="failed", error_message=error_message)
            .returning(CaliberRefinementJob.job_id)
        )
        reaped = session.execute(stmt).scalar_one_or_none()
        if reaped is None:
            # Raced with a heartbeat update — leave the row alone, no
            # audit, no metric. The next janitor tick will pick it up
            # again if it's still stale.
            logger.info(
                "janitor skipped job=%s: heartbeat refreshed during sweep",
                job.job_id,
            )
            return

        audit_record(
            session,
            actor="caliber.janitor",
            action="reap_stale_job",
            entity_type="refinement_job",
            entity_id=job.job_id,
            details={
                "from_stage": previous_stage,
                "last_heartbeat_at": heartbeat,
                "stale_threshold_seconds": int(self._stale_threshold.total_seconds()),
            },
        )

        metrics.record_job_terminal(
            agent_id=job.agent_id,
            artifact_type=job.artifact_type,
            status="failed",
        )

        logger.warning(
            "reaped stale job=%s agent=%s stage=%s heartbeat=%s",
            job.job_id,
            job.agent_id,
            previous_stage,
            heartbeat,
        )
