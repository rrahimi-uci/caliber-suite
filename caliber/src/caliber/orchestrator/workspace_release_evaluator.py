"""Owner-only worker that executes queued Workspace release evaluations (B10.2, #707).

``POST .../releases/{id}/evaluate`` queues one :class:`CaliberWorkspaceReleaseEvaluation` row per attempt
(``caliber.workspace_release_service.create_workspace_release_evaluation``). This worker is the first consumer of that
queue. It does **not** add a second queue or a second lease protocol: it calls the service's
``claim_workspace_release_evaluation`` and leaves heartbeat and completion to that service's
``heartbeat_workspace_release_evaluation`` / ``complete_workspace_release_evaluation``.

The worker resolves the release's own immutable revision pins, calls the configured evaluation provider, and
completes the same leased attempt. Expired claims are recovered by the service into a new, fenced attempt while
preserving the prior attempt and any provider run identifiers. A clean :meth:`stop` requeues this process's own
claims; a crash is recovered by the next owner after the lease expires. The feature is off by default
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

It records a digest of the provider result and its simulated/real provenance. Independent gate-evidence rows and
verdict records arrive in B10.3; durable attempt history, provider-run preservation, and stale-worker fencing arrive
in B10.4.

Known limits: an idle tick still writes a heartbeat row and runs one indexed SELECT; a tick still running when the stop
grace ends is abandoned (a thread cannot be cancelled) and may claim a row after the requeue; a queued row whose claim
keeps failing with an unexpected error is retried first on every tick, so it holds up the rows behind it.

Errors carry identifiers and exception classes, never row content.
"""

from __future__ import annotations

import asyncio
import logging
import math
import time
from collections.abc import Callable
from contextlib import suppress
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import or_, select, text, update
from sqlalchemy.orm import Session, sessionmaker

from caliber.auth import CaliberIdentity
from caliber.db.models import (
    CaliberWorkspaceRelease,
    CaliberWorkspaceReleaseEvaluation,
    CaliberWorkspaceReleaseEvaluationAttempt,
)
from caliber.eval.evidence import build_release_gate_evidence
from caliber.eval.provider import EvalComparison, EvalProvider
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
from caliber.workspace_release_adapters import WorkspaceResourceAdapterRegistry
from caliber.workspace_release_evaluation_plan import (
    WorkspaceReleaseEvaluationPlan,
    WorkspaceReleaseEvaluationPlanError,
    resolve_workspace_release_evaluation_plan,
)
from caliber.workspace_release_service import (
    EVALUATION_QUEUED,
    EVALUATION_RUNNING,
    WorkspaceReleaseLeaseError,
    claim_workspace_release_evaluation,
    complete_workspace_release_evaluation,
    heartbeat_workspace_release_evaluation,
    record_workspace_release_evaluation_attempt_runs,
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
        eval_provider: EvalProvider | None = None,
        adapter_registry: WorkspaceResourceAdapterRegistry | None = None,
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
        self._eval_provider = eval_provider
        self._adapter_registry = adapter_registry
        self._worker_id = new_worker_id("workspace-release-evaluator", self)
        self._task: asyncio.Task[None] | None = None
        self._stopped = asyncio.Event()
        self._last_reap = float("-inf")

    @property
    def worker_id(self) -> str:
        """The id recorded as ``claimed_by`` on every row this evaluator claims."""
        return self._worker_id

    @property
    def execution_enabled(self) -> bool:
        """Whether this instance has the dependencies required to execute plans."""
        return self._eval_provider is not None and self._adapter_registry is not None

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
        if self.execution_enabled:
            logger.info(
                "workspace release evaluator executes pinned plans with expired-attempt recovery (B10.4, #721)"
            )
        else:
            logger.warning(
                "workspace release evaluator is enabled: it CLAIMS queued release evaluations but does not "
                "execute them without an EvalProvider and adapter registry (test/dry-run mode); "
                "execution is B10.2 (#707); attempt recovery/fencing is B10.4 (#721)"
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
            if claimed is not None and self.execution_enabled:
                self._execute_claim(claimed)
            elif claimed is not None:
                logger.warning(
                    "workspace release evaluator claimed %s (release %s, attempt %d) as %s: "
                    "execution dependencies are not configured; nothing executes it until B10.2 (#707); "
                    "it is requeued on a clean stop",
                    claimed.evaluation_id,
                    claimed.workspace_release_id,
                    claimed.attempt_number,
                    claimed.worker_id,
                )
            return claimed

    def _assert_execution_owner(self, session: Session) -> None:
        """Fence execution and completion with the current owner lease."""
        if self._lease_provider is None:
            return
        lease = self._lease_provider()
        if lease is None:
            raise LeaseLost("execution-owner lease is no longer held")
        assert_fenced(session, lease)

    def _evaluation_identity(
        self, evaluation: CaliberWorkspaceReleaseEvaluation, project_id: str
    ) -> CaliberIdentity:
        # Background work has no request object. Reuse the persisted requester
        # as the subject and keep the release project as the active scope.
        from caliber.db.scoping import synthetic_identity  # noqa: PLC0415

        return synthetic_identity(evaluation.requested_by, project_id)

    def _prepare_claim(
        self, claim: ClaimedEvaluation
    ) -> tuple[WorkspaceReleaseEvaluationPlan | None, int | None, Exception | None]:
        """Heartbeat and resolve a claim before making the provider call."""
        with self._session_factory() as session:
            evaluation = session.get(CaliberWorkspaceReleaseEvaluation, claim.evaluation_id)
            release = session.get(CaliberWorkspaceRelease, claim.workspace_release_id)
            if evaluation is None or release is None:
                return (
                    None,
                    None,
                    WorkspaceReleaseEvaluationPlanError(
                        "evaluation_coordinates_unavailable",
                        "claimed release evaluation disappeared",
                    ),
                )
            try:
                self._assert_execution_owner(session)
                heartbeat_workspace_release_evaluation(
                    session,
                    evaluation,
                    worker_id=claim.worker_id,
                    attempt_number=claim.attempt_number,
                    lease_seconds=int(self._lease_seconds),
                )
                assert self._adapter_registry is not None
                assert self._eval_provider is not None
                plan = resolve_workspace_release_evaluation_plan(
                    session,
                    release=release,
                    evaluation=evaluation,
                    identity=self._evaluation_identity(evaluation, release.project_id),
                    adapter_registry=self._adapter_registry,
                    provider=self._eval_provider,
                    config=self._config,
                )
                expected_lock_version = release.lock_version
                session.commit()
                return plan, expected_lock_version, None
            except Exception as exc:
                # Roll back the resolution transaction. Completion below is a
                # separate fenced CAS and can therefore safely block the
                # release without leaving an aborted session open.
                session.rollback()
                return None, release.lock_version, exc

    def _execute_claim(self, claim: ClaimedEvaluation) -> None:
        plan, expected_lock_version, preparation_error = self._prepare_claim(claim)
        if preparation_error is not None or plan is None or expected_lock_version is None:
            self._complete_blocked(
                claim,
                expected_lock_version=expected_lock_version,
                error=preparation_error or RuntimeError("evaluation plan was not resolved"),
            )
            return
        assert self._eval_provider is not None
        comparison: EvalComparison | None = None
        try:
            comparison = self._eval_provider.evaluate(plan.request)
            if comparison.evaluation_run_ids:
                self._record_external_runs(claim, comparison.evaluation_run_ids)
            result, evidence_sha256, gate_evidence = self._gate_result(plan, comparison, claim)
        except Exception as exc:
            self._complete_blocked(
                claim,
                expected_lock_version=expected_lock_version,
                error=exc,
                evaluation_run_ids=(
                    comparison.evaluation_run_ids if comparison is not None else ()
                ),
            )
            return
        self._complete(
            claim,
            expected_lock_version=expected_lock_version,
            result=result,
            evidence_sha256=evidence_sha256,
            gate_evidence=gate_evidence,
            evaluation_run_ids=comparison.evaluation_run_ids,
        )

    def _record_external_runs(
        self, claim: ClaimedEvaluation, evaluation_run_ids: tuple[str, ...]
    ) -> None:
        """Persist provider run references before attempting release settlement."""

        with self._session_factory() as session:
            evaluation = session.get(CaliberWorkspaceReleaseEvaluation, claim.evaluation_id)
            if evaluation is None:
                return
            record_workspace_release_evaluation_attempt_runs(
                session,
                evaluation,
                attempt_number=claim.attempt_number,
                worker_id=claim.worker_id,
                evaluation_run_ids=evaluation_run_ids,
            )
            session.commit()

    def _gate_result(
        self,
        plan: WorkspaceReleaseEvaluationPlan,
        comparison: EvalComparison,
        claim: ClaimedEvaluation,
    ) -> tuple[str, str, dict[str, Any]]:
        if comparison.eval_dataset_id != plan.request.eval_dataset_id or comparison.n_examples < 1:
            raise ValueError("provider returned an invalid pinned dataset result")
        overall = float(comparison.candidate.overall)
        if not math.isfinite(overall) or not 0.0 <= overall <= 1.0:
            raise ValueError("provider returned an invalid candidate score")
        delta: float | None = None
        if comparison.baseline is not None:
            baseline_overall = float(comparison.baseline.overall)
            if not math.isfinite(baseline_overall) or not 0.0 <= baseline_overall <= 1.0:
                raise ValueError("provider returned an invalid baseline score")
            delta = overall - baseline_overall
        elif plan.min_aggregate_delta is not None:
            raise ValueError("a delta gate requires a pinned baseline")
        passes = overall >= plan.min_aggregate_score
        if plan.min_aggregate_delta is not None:
            passes = passes and delta is not None and delta >= plan.min_aggregate_delta
        gate_evidence, evidence_sha256 = build_release_gate_evidence(
            scored_rows=comparison.evidence_rows,
            dataset_id=plan.request.eval_dataset_id,
            dataset_version=plan.request.eval_dataset_version,
            plan_sha256=plan.plan_digest,
            input_sha256=plan.input_digest,
            evaluation_id=claim.evaluation_id,
            provider=plan.provider_info,
            candidate={
                "overall": overall,
                "dimensions": dict(comparison.candidate.dimensions),
            },
            baseline=(
                {
                    "overall": comparison.baseline.overall,
                    "dimensions": dict(comparison.baseline.dimensions),
                }
                if comparison.baseline is not None
                else None
            ),
            deltas=comparison.deltas,
            n_examples=comparison.n_examples,
            min_aggregate_score=plan.min_aggregate_score,
            min_aggregate_delta=plan.min_aggregate_delta,
            result="pass" if passes else "no_go",
        )
        return "pass" if passes else "no_go", evidence_sha256, gate_evidence

    def _complete_blocked(
        self,
        claim: ClaimedEvaluation,
        *,
        expected_lock_version: int | None,
        error: Exception,
        evaluation_run_ids: tuple[str, ...] = (),
    ) -> None:
        if expected_lock_version is None:
            logger.error(
                "workspace release evaluation %s could not be resolved: %s",
                claim.evaluation_id,
                type(error).__name__,
            )
            return
        self._complete(
            claim,
            expected_lock_version=expected_lock_version,
            result="blocked",
            error_code=getattr(error, "code", "evaluation_execution_failed"),
            error_summary="workspace release evaluation could not run",
            evaluation_run_ids=evaluation_run_ids,
        )

    def _complete(
        self,
        claim: ClaimedEvaluation,
        *,
        expected_lock_version: int,
        result: str,
        evidence_sha256: str | None = None,
        gate_evidence: dict[str, Any] | None = None,
        evaluation_run_ids: tuple[str, ...] = (),
        error_code: str | None = None,
        error_summary: str | None = None,
    ) -> None:
        with self._session_factory() as session:
            evaluation = session.get(CaliberWorkspaceReleaseEvaluation, claim.evaluation_id)
            if evaluation is None:
                logger.error(
                    "workspace release evaluation %s disappeared before completion",
                    claim.evaluation_id,
                )
                return
            try:
                self._assert_execution_owner(session)
                complete_workspace_release_evaluation(
                    session,
                    evaluation,
                    worker_id=claim.worker_id,
                    attempt_number=claim.attempt_number,
                    result=result,  # type: ignore[arg-type]
                    expected_release_lock_version=expected_lock_version,
                    evidence_sha256=evidence_sha256,
                    gate_evidence=gate_evidence,
                    evaluation_run_ids=list(evaluation_run_ids),
                    error_code=error_code,
                    error_summary=error_summary,
                )
                session.commit()
                logger.info(
                    "workspace release evaluation %s completed as %s (provider=%s simulated=%s)",
                    claim.evaluation_id,
                    result,
                    getattr(
                        self._eval_provider, "provider_name", type(self._eval_provider).__name__
                    ),
                    bool(getattr(self._eval_provider, "is_simulated", False)),
                )
            except (LeaseLost, WorkspaceReleaseLeaseError) as exc:
                session.rollback()
                if evaluation_run_ids:
                    try:
                        evaluation = session.get(
                            CaliberWorkspaceReleaseEvaluation, claim.evaluation_id
                        )
                        if evaluation is not None:
                            record_workspace_release_evaluation_attempt_runs(
                                session,
                                evaluation,
                                attempt_number=claim.attempt_number,
                                worker_id=claim.worker_id,
                                evaluation_run_ids=evaluation_run_ids,
                            )
                            session.commit()
                    except Exception:
                        session.rollback()
                        logger.exception(
                            "workspace release evaluation %s could not persist orphan provider runs",
                            claim.evaluation_id,
                        )
                logger.warning(
                    "workspace release evaluation %s was not completed because its lease/fence was lost (%s)",
                    claim.evaluation_id,
                    type(exc).__name__,
                )
            except Exception:
                session.rollback()
                logger.exception(
                    "workspace release evaluation %s failed while completing; lease recovery will handle it",
                    claim.evaluation_id,
                )

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
            owned = (
                session.execute(
                    select(CaliberWorkspaceReleaseEvaluation).where(
                        CaliberWorkspaceReleaseEvaluation.status == EVALUATION_RUNNING,
                        or_(
                            own == self._worker_id,
                            own.startswith(f"{self._worker_id}{_FENCE_MARK}", autoescape=True),
                        ),
                    )
                )
                .scalars()
                .all()
            )
            for evaluation in owned:
                attempt = session.execute(
                    select(CaliberWorkspaceReleaseEvaluationAttempt).where(
                        CaliberWorkspaceReleaseEvaluationAttempt.evaluation_id
                        == evaluation.evaluation_id,
                        CaliberWorkspaceReleaseEvaluationAttempt.attempt_number
                        == evaluation.attempt_number,
                    )
                ).scalar_one_or_none()
                if attempt is not None:
                    attempt.status = "requeued"
                    attempt.completed_at = datetime.now(timezone.utc).replace(tzinfo=None)
                    attempt.lease_expires_at = None
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
