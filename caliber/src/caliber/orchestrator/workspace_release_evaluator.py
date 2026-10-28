"""Owner-only worker that claims queued Workspace release evaluations (B10.1, #701).

``POST .../releases/{id}/evaluate`` queues one :class:`CaliberWorkspaceReleaseEvaluation` row per attempt
(``caliber.workspace_release_service.create_workspace_release_evaluation``). This worker is the first consumer of that
queue. It does **not** add a second queue or a second lease protocol: it calls the service's
``claim_workspace_release_evaluation`` and leaves heartbeat and completion to that service's
``heartbeat_workspace_release_evaluation`` / ``complete_workspace_release_evaluation``.

**Do not enable it in a deployment until B10.2 (#707) lands.** Today it only *claims*: nothing runs the evaluation,
nothing finishes it, and nothing acts when its lease lapses (reclaiming an expired attempt is B10.4, #721). A claimed
row therefore sits ``running`` and its release ``evaluating``, and every later ``evaluate`` is refused with 409,
until the row is requeued. A clean :meth:`stop` requeues this process's own claims; a crash does not, and the operator
recovery is in ``docs/operate/reference-profile.md``. The feature is off by default
(``CALIBER_WORKSPACE_RELEASE_EVALUATOR_ENABLED``) and never built while ``CALIBER_FEATURE_RELEASE_CANDIDATES`` is off.

What one :meth:`WorkspaceReleaseEvaluator.tick` does, in order:

1. Writes this worker's own ``caliber_worker_heartbeats`` row (``worker_registry.record_heartbeat``, kind
   ``workspace_release_evaluator``), every tick, idle ones included. Nothing reads that kind yet (queue health reads
   workflow runs only), so for now it is a row an operator can query, not a probe.
2. Decides whether this process may claim. The loop only runs in the process that holds the execution-owner lease
   (ADR 0002, ``caliber.execution_role``), and in the reference profile the claim itself is **fenced**:
   ``caliber.ownership.assert_fenced`` runs in the claiming transaction with the exact lease the owner captured, so a
   process whose lease was taken over claims nothing and records nothing. Outside the reference profile there is no
   lease and the loop is the owner by construction (``lease_provider`` is ``None``).
3. Claims the oldest ``queued`` evaluation (``requested_at``, then id) and commits. The row becomes ``running`` with a
   lease of ``workspace_release_evaluator_lease_seconds`` and ``claimed_by`` set to the *claim identity*: this worker's
   id, plus ``:t<fencing token>`` of the lease it claimed under when there is one. #721 must pass that identity (the
   ``worker_id`` of :class:`ClaimedEvaluation`) to the service's heartbeat and complete, and can read the token back
   from the row to tell attempts of different owners apart without a migration. Two claimers racing for one row cannot
   both win: ``FOR UPDATE SKIP LOCKED`` on PostgreSQL and the service's conditional ``UPDATE ... WHERE status =
   'queued'`` everywhere; the loser claims nothing this tick.

What it deliberately does not do yet: resolve the evaluation plan and run it (B10.2, #707), record gate evidence
(B10.3, #714), heartbeat the evaluation's own lease and reclaim an expired attempt (B10.4, #721).

Known limits: an idle tick still writes a heartbeat row and runs one indexed SELECT; a tick still running when the stop
grace ends is abandoned (a thread cannot be cancelled) and may claim a row after the requeue; a queued row whose claim
keeps failing with an unexpected error is retried first on every tick, so it holds up the rows behind it.

Errors carry identifiers and exception classes, never row content.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Callable
from contextlib import suppress
from dataclasses import dataclass
from typing import Any

from sqlalchemy import or_, select, text, update
from sqlalchemy.orm import Session, sessionmaker

from caliber.db.models import CaliberWorkspaceReleaseEvaluation
from caliber.observability.trace import bind_trace_id
from caliber.observability.worker_registry import (
    KIND_WORKSPACE_RELEASE_EVALUATOR,
    deregister,
    new_worker_id,
    reap_stale_workers,
    record_heartbeat,
    stale_after_seconds,
)
from caliber.ownership import Lease, LeaseLost, assert_fenced
from caliber.workspace_release_service import (
    EVALUATION_QUEUED,
    EVALUATION_RUNNING,
    WorkspaceReleaseLeaseError,
    claim_workspace_release_evaluation,
)

logger = logging.getLogger("caliber.orchestrator.workspace_release_evaluator")

DEFAULT_POLL_INTERVAL_SECONDS = 5.0
DEFAULT_LEASE_SECONDS = 300.0
#: The most a claim waits for the lease row on PostgreSQL, as a fraction of the execution lease (the lease guidance in
#: ``docs/operate/reference-profile.md``: a fenced transaction must not block failover indefinitely).
_LOCK_WAIT_FRACTION = 1.0 / 3.0
_MIN_LOCK_WAIT_MILLIS = 100
#: Appended to the worker id with the fencing token: ``<worker id>:t<token>``.
_FENCE_MARK = ":t"
_DEFAULT_EXECUTION_LEASE_SECONDS = 30.0
_REAP_EVERY_SECONDS = 60.0

#: Who may claim right now: returns the exact lease the owner captured, ``None`` when this process does not hold it.
LeaseProvider = Callable[[], Lease | None]


@dataclass(frozen=True)
class ClaimedEvaluation:
    """What one tick claimed: identifiers only, never row content."""

    evaluation_id: str
    project_id: str
    workspace_release_id: str
    attempt_number: int
    #: The claim identity written to ``claimed_by``: the worker id, plus ``:t<token>`` when the claim was fenced. It is
    #: what the service's heartbeat and complete must be given as ``worker_id``.
    worker_id: str
    #: The fencing token of the execution-owner lease the claim was made under; ``None`` outside the reference profile.
    fencing_token: int | None


class WorkspaceReleaseEvaluator:
    """Claims queued Workspace release evaluations, oldest first, while this process is the execution owner."""

    def __init__(
        self,
        session_factory: sessionmaker[Session],
        config: Any,
        poll_interval_seconds: float = DEFAULT_POLL_INTERVAL_SECONDS,
        *,
        lease_provider: LeaseProvider | None = None,
    ) -> None:
        if not poll_interval_seconds > 0:
            raise ValueError("poll_interval_seconds must be positive")
        self._session_factory = session_factory
        self._config = config
        self._interval_seconds = float(poll_interval_seconds)
        self._lease_seconds = float(
            getattr(config, "workspace_release_evaluator_lease_seconds", DEFAULT_LEASE_SECONDS)
        )
        if self._lease_seconds < 1:
            raise ValueError("workspace_release_evaluator_lease_seconds must be at least 1")
        self._execution_lease_seconds = float(
            getattr(config, "execution_lease_seconds", _DEFAULT_EXECUTION_LEASE_SECONDS)
        )
        self._lease_provider = lease_provider
        self._worker_id = new_worker_id("workspace-release-evaluator", self)
        self._task: asyncio.Task[None] | None = None
        self._stopped = asyncio.Event()
        self._last_reap = float("-inf")

    @property
    def worker_id(self) -> str:
        """The id recorded as ``claimed_by`` on every row this evaluator claims."""
        return self._worker_id

    def bind_lease_provider(self, provider: LeaseProvider | None) -> None:
        """Fence every claim with the execution-owner lease ``provider`` returns (``None``: no lease, no fence).

        The lifespan binds it once its :class:`~caliber.execution_role.ExecutionRoleController` exists, because the
        evaluator is built before that controller is.
        """
        self._lease_provider = provider

    # -- lifecycle -----------------------------------------------------------------------------

    async def start(self) -> None:
        if self._task is not None:
            raise RuntimeError("WorkspaceReleaseEvaluator.start() called while already running")
        self._stopped.clear()
        self._task = asyncio.create_task(self._run(), name="caliber.workspace_release_evaluator")
        logger.warning(
            "workspace release evaluator is enabled: it CLAIMS queued release evaluations but does not execute them "
            "until B10.2 (#707), and nothing reclaims an expired claim until B10.4 (#721); a claimed attempt stays "
            "running (a clean stop requeues it) and later evaluate calls answer 409. See docs/operate/reference-profile.md"
        )
        logger.info(
            "workspace release evaluator started (interval=%.1fs lease=%.1fs worker=%s)",
            self._interval_seconds,
            self._lease_seconds,
            self._worker_id,
        )

    async def stop(self, *, grace_seconds: float = 30.0) -> None:
        """Stop claiming: a tick in flight finishes (bounded by ``grace_seconds``), the claims this process still
        holds go back to ``queued``, then the liveness row goes."""
        if self._task is None:
            return
        self._stopped.set()
        try:
            await asyncio.wait_for(asyncio.shield(self._task), timeout=grace_seconds)
        except (TimeoutError, asyncio.TimeoutError):
            logger.warning(
                "workspace release evaluator did not stop within %.1fs; cancelling", grace_seconds
            )
            self._task.cancel()
            with suppress(asyncio.CancelledError):
                await self._task
        self._task = None
        try:
            await asyncio.to_thread(self.release_claims)
        except Exception:
            logger.exception(
                "workspace release evaluator could not requeue its claims; see the recovery SQL in the docs"
            )
        await asyncio.to_thread(self._deregister_liveness)
        logger.info("workspace release evaluator stopped")

    async def _run(self) -> None:
        while not self._stopped.is_set():
            try:
                await asyncio.to_thread(self.tick)
            except Exception:
                logger.exception("workspace release evaluator tick raised; continuing")
            with suppress(TimeoutError):
                await asyncio.wait_for(self._stopped.wait(), timeout=self._interval_seconds)

    # -- one pass ------------------------------------------------------------------------------

    def tick(self) -> ClaimedEvaluation | None:
        """Claim the oldest queued evaluation if this process may, and say what was claimed (``None``: nothing)."""
        with bind_trace_id():
            # First, and outside the work below: an idle evaluator must prove it is alive too.
            self._record_liveness()
            lease: Lease | None = None
            if self._lease_provider is not None:
                lease = self._lease_provider()
                if lease is None:
                    logger.debug("workspace release evaluator is not the execution owner; no claim")
                    return None
            claimed = self._claim_oldest(lease)
            if claimed is not None:
                logger.warning(
                    "workspace release evaluator claimed %s (release %s, attempt %d) as %s: nothing executes it "
                    "until B10.2 (#707); it is requeued on a clean stop, otherwise see the recovery SQL in the docs",
                    claimed.evaluation_id,
                    claimed.workspace_release_id,
                    claimed.attempt_number,
                    claimed.worker_id,
                )
            return claimed

    def _claim_oldest(self, lease: Lease | None) -> ClaimedEvaluation | None:
        with self._session_factory() as session:
            try:
                if lease is not None:
                    self._bound_lock_wait(session)
                    assert_fenced(session, lease)
                candidate = session.execute(
                    select(CaliberWorkspaceReleaseEvaluation)
                    .where(CaliberWorkspaceReleaseEvaluation.status == EVALUATION_QUEUED)
                    .order_by(
                        CaliberWorkspaceReleaseEvaluation.requested_at,
                        CaliberWorkspaceReleaseEvaluation.evaluation_id,
                    )
                    .limit(1)
                    .with_for_update(skip_locked=True)
                ).scalar_one_or_none()
                if candidate is None:
                    session.rollback()
                    return None
                identity = self._claim_identity(lease)
                claimed = claim_workspace_release_evaluation(
                    session,
                    candidate,
                    worker_id=identity,
                    lease_seconds=int(self._lease_seconds),
                )
                result = ClaimedEvaluation(
                    evaluation_id=claimed.evaluation_id,
                    project_id=claimed.project_id,
                    workspace_release_id=claimed.workspace_release_id,
                    attempt_number=claimed.attempt_number,
                    worker_id=identity,
                    fencing_token=lease.fencing_token if lease is not None else None,
                )
                session.commit()
                return result
            except LeaseLost as exc:
                # Another process holds the lease now, or the store could not be asked: claim nothing.
                logger.warning(
                    "workspace release evaluator is no longer the execution owner (%s); nothing claimed",
                    exc.reason,
                    exc_info=exc.__cause__,
                )
                session.rollback()
                return None
            except WorkspaceReleaseLeaseError as exc:
                # Someone else claimed the row between the read and the update: the next tick takes the next one.
                logger.info("workspace release evaluator lost a claim race (%s)", exc)
                session.rollback()
                return None

    def _claim_identity(self, lease: Lease | None) -> str:
        """What ``claimed_by`` holds: the worker id, with the fencing token it claimed under when there is one."""
        if lease is None:
            return self._worker_id
        return f"{self._worker_id}{_FENCE_MARK}{lease.fencing_token}"

    def _bound_lock_wait(self, session: Session) -> None:
        """Bound how long the fenced claim may wait, on PostgreSQL (SQLite has no such timeouts).

        Same bound as ``ExecutionRoleController._bounded``: a third of the execution lease (at least 100 ms), for a
        lock on the lease row and for any one statement, so a stuck session cannot hold failover up.
        """
        if session.get_bind().dialect.name == "postgresql":
            millis = max(
                int(self._execution_lease_seconds * _LOCK_WAIT_FRACTION * 1000),
                _MIN_LOCK_WAIT_MILLIS,
            )
            session.execute(text(f"SET LOCAL lock_timeout = {millis}"))
            session.execute(text(f"SET LOCAL statement_timeout = {millis}"))

    def release_claims(self) -> int:
        """Put every evaluation this worker still holds as ``running`` back to ``queued``; returns how many.

        Deliberately a compare-and-set on this worker's own claim identity (``claimed_by`` is the worker id or
        ``<worker id>:t<token>``) and on ``running``, not a fenced transition: it must work after the lease is gone,
        it never touches a row another worker (or another incarnation of this process) owns, and a requeued row is
        only more work for whoever owns the loops next. The attempt number is kept. Never raises on a lost claim.
        """
        own = CaliberWorkspaceReleaseEvaluation.claimed_by
        with self._session_factory() as session:
            result = session.execute(
                update(CaliberWorkspaceReleaseEvaluation)
                .where(
                    CaliberWorkspaceReleaseEvaluation.status == EVALUATION_RUNNING,
                    or_(
                        own == self._worker_id,
                        own.startswith(f"{self._worker_id}{_FENCE_MARK}", autoescape=True),
                    ),
                )
                .values(
                    status=EVALUATION_QUEUED,
                    claimed_by=None,
                    started_by=None,
                    started_at=None,
                    heartbeat_at=None,
                    lease_expires_at=None,
                ),
                execution_options={"synchronize_session": False},
            )
            session.commit()
        requeued = int(getattr(result, "rowcount", 0) or 0)
        if requeued:
            logger.warning(
                "workspace release evaluator requeued %d claimed evaluation(s) it was not going to finish",
                requeued,
            )
        return requeued

    # -- liveness ------------------------------------------------------------------------------

    def _record_liveness(self) -> None:
        """Write this worker's own heartbeat row; never raises (see ``worker_registry``)."""
        with self._session_factory() as session:
            record_heartbeat(
                session, worker_id=self._worker_id, kind=KIND_WORKSPACE_RELEASE_EVALUATOR
            )
            self._reap_replaced_workers(session)

    def _reap_replaced_workers(self, session: Session) -> None:
        """Drop heartbeat rows of evaluators this one has outlived, at most once a minute.

        A restarted process registers under a new id, so each unclean restart leaves one stale row behind.
        """
        now = time.monotonic()
        if now - self._last_reap < _REAP_EVERY_SECONDS:
            return
        self._last_reap = now
        reap_stale_workers(
            session,
            worker_id=self._worker_id,
            kind=KIND_WORKSPACE_RELEASE_EVALUATOR,
            stale_after=stale_after_seconds(self._interval_seconds),
        )

    def _deregister_liveness(self) -> None:
        """Drop the heartbeat row on a clean stop; an unclean exit leaves it to go stale, which is the signal."""
        with self._session_factory() as session:
            deregister(session, worker_id=self._worker_id)


__all__ = [
    "DEFAULT_LEASE_SECONDS",
    "DEFAULT_POLL_INTERVAL_SECONDS",
    "ClaimedEvaluation",
    "LeaseProvider",
    "WorkspaceReleaseEvaluator",
]
