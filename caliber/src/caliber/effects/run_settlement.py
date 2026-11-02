"""Settle what a run that ended left unsent (#1074; ADR 0003 amendment, "never-dispatched proposals").

A governed step records its effect as a ``proposed`` proposal and, for a protected effect, asks a person to approve it
(``caliber.workflows.approval_requests``). If the run then ends without that proposal being dispatched (the approval was
rejected, the run failed, was cancelled or expired), nothing used to close the proposal or its request: the proposal stayed
``proposed`` and its request ``pending`` or ``approved`` for good, and an approved decision nobody consumed stayed usable
for as long as its expiry allowed. This module closes them, with **no new proposal state and no migration**:

* A ``proposed`` proposal moves to ``failed`` (the existing ``proposed`` to ``failed`` edge of ADR 0003, a compare-and-set on
  ``version`` like every move) with a short ``reason`` naming why: ``run_failed``, ``run_cancelled``, ``run_expired``,
  ``run_completed`` or ``approval_rejected``. It never carried ``dispatched_at``, ``owner_id`` or a receipt, which is how a
  reader tells "never sent" from a failed dispatch; ``settled_at`` records when. ``failed`` is final (migration 0124).
* Every *live* bound request of the run (``pending``, or ``approved`` and not consumed, not superseded) becomes
  ``withdrawn`` (status; nothing else on the row moves, so the decision and its approvers stay as recorded). A legacy
  node-level request (no ``proposal_id``) guards no effect and is left alone, as supersession leaves it.

**What is never touched.** Only ``proposed`` is settled. ``dispatched`` (a claim committed: the effect may have been sent),
``unknown``, ``reconciled``, ``committed`` and ``failed`` are not read, let alone changed: they belong to the dispatcher and the
reconciler, which ask the downstream by key. A request that dispatch already consumed (``consumed_at``) is not withdrawn. A
repeated call finds nothing live and changes nothing (no audit row either).

**Who can call it.** Only code of the run's own lifecycle: the end paths (:func:`settle_run_effects`, in the transaction that
writes the run's terminal status) and the owner-only sweep (:func:`settle_ended_runs`, under the execution-owner lease, for runs
that ended before this existed or by a path that missed it). The reason is one of a fixed vocabulary, never a value from a
model, a manifest or a request; the function refuses a run that has not ended, so it can never settle a live run's proposals.

**Locking and the race with a dispatch.** The order is the dispatcher's and the request writers': run, then the proposal rows,
then the request rows, all ``FOR UPDATE`` (a no-op on SQLite). The dispatcher's claim takes the same locks in the same order
and re-reads the run's status under the run lock (``run_ended``), so exactly one of two things happens: the claim commits first
(the proposal is ``dispatched``, the request consumed, and this module finds nothing to settle) or the settlement commits first
(the proposal is ``failed`` and the request ``withdrawn``, and the claim is refused with no effect sent and nothing consumed).
There is no interleaving in which a proposal is both dispatched and settled, or settled after a send.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Final

from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from caliber.audit import record as audit_record
from caliber.db.models import (
    CaliberEffectProposal,
    CaliberRuntimeApprovalRequest,
    CaliberWorkflowRun,
)
from caliber.effects.records import (
    BAD_FIELD,
    FAILED,
    PROPOSED,
    RUN_NOT_FOUND,
    ProposalError,
    transition,
)
from caliber.ownership import Lease, assert_fenced
from caliber.workflows.approval_policy import STATUS_WITHDRAWN
from caliber.workflows.run_state import (
    RUN_STATUS_CANCELLED,
    RUN_STATUS_COMPLETED,
    RUN_STATUS_EXPIRED,
    RUN_STATUS_FAILED,
    TERMINAL_RUN_STATUSES,
)

logger = logging.getLogger("caliber.effects.run_settlement")

RUN_NOT_ENDED: Final = "run_not_ended"
RUN_FAILED_REASON: Final = "run_failed"
RUN_CANCELLED_REASON: Final = "run_cancelled"
RUN_EXPIRED_REASON: Final = "run_expired"
RUN_COMPLETED_REASON: Final = "run_completed"
APPROVAL_REJECTED_REASON: Final = "approval_rejected"
#: Why a never-dispatched proposal was settled; the ``reason`` recorded on it and in the audit row. A fixed vocabulary.
SETTLE_REASONS: Final = frozenset(
    {
        RUN_FAILED_REASON,
        RUN_CANCELLED_REASON,
        RUN_EXPIRED_REASON,
        RUN_COMPLETED_REASON,
        APPROVAL_REJECTED_REASON,
    }
)
#: The reason for a run that is found already ended (the sweep); the end paths name theirs.
REASON_FOR_RUN_STATUS: Final = {
    RUN_STATUS_FAILED: RUN_FAILED_REASON,
    RUN_STATUS_CANCELLED: RUN_CANCELLED_REASON,
    RUN_STATUS_EXPIRED: RUN_EXPIRED_REASON,
    RUN_STATUS_COMPLETED: RUN_COMPLETED_REASON,
}
#: ``status`` values of a request that is still live (answerable, or answered and not yet consumed).
LIVE_REQUEST_STATUSES: Final = ("pending", "approved")
#: Most runs one sweep settles (the rest wait for the next tick), and the most ids an audit row lists (the counts are exact).
DEFAULT_SWEEP_LIMIT: Final = 50
MAX_SWEEP_LIMIT: Final = 1000
_MAX_AUDITED_IDS: Final = 50
_AUDIT_ACTOR: Final = "workflow_run_lifecycle"

SessionFactory = Callable[[], Session]


@dataclass(frozen=True)
class RunSettlement:
    """What one call settled: the proposals moved to ``failed`` and the requests moved to ``withdrawn`` (ids only)."""

    workflow_run_id: str
    proposals: tuple[str, ...]
    requests: tuple[str, ...]

    @property
    def changed(self) -> bool:
        return bool(self.proposals or self.requests)


def settle_run_effects(
    session: Session,
    workflow_run_id: str,
    *,
    reason: str,
    actor: str = _AUDIT_ACTOR,
    now: datetime | None = None,
) -> RunSettlement:
    """Settle the never-dispatched proposals and live bound requests of an ended run, in the caller's transaction (never commits).

    The caller has written the run's terminal status in this session (it is flushed here) or is looking at a run that ended
    earlier; a run that has not ended is refused with ``run_not_ended`` and nothing is read or written, and an unknown run is
    ``run_not_found``. ``reason`` is one of :data:`SETTLE_REASONS` (anything else is ``bad_field``). The run row is locked
    first, then the ``proposed`` proposals and then the live bound requests of the run, each ``FOR UPDATE`` and in id order.

    Each ``proposed`` proposal moves to ``failed`` by :func:`caliber.effects.records.transition` at the version just read
    under its lock, and each live bound request's status becomes ``withdrawn``. One audit row (action ``settle_run_effects``,
    entity ``workflow_run``) lists the ids (the first 50) and the exact counts, only when something changed, so calling this
    again for the same run writes nothing. Idempotent: the second call finds no ``proposed`` proposal and no live request.
    """
    if reason not in SETTLE_REASONS:
        raise ProposalError(BAD_FIELD, "reason must be one of the settlement reasons")
    session.flush()  # the session factory has autoflush off: the terminal status the caller just set must be visible
    status = session.execute(
        select(CaliberWorkflowRun.status)
        .where(CaliberWorkflowRun.workflow_run_id == workflow_run_id)
        .with_for_update()
    ).scalar_one_or_none()
    if status is None:
        raise ProposalError(RUN_NOT_FOUND, f"run {workflow_run_id} does not exist")
    if status not in TERMINAL_RUN_STATUSES:
        raise ProposalError(RUN_NOT_ENDED, f"run {workflow_run_id} has not ended")

    proposals = session.execute(
        select(CaliberEffectProposal.proposal_id, CaliberEffectProposal.version)
        .where(
            CaliberEffectProposal.workflow_run_id == workflow_run_id,
            CaliberEffectProposal.state == PROPOSED,
        )
        .order_by(CaliberEffectProposal.proposal_id)
        .with_for_update()
    ).all()
    settled_proposals: list[str] = []
    for proposal_id, version in proposals:
        transition(session, proposal_id, FAILED, expected_version=version, reason=reason, now=now)
        settled_proposals.append(proposal_id)

    live = (
        session.execute(
            select(CaliberRuntimeApprovalRequest)
            .where(
                CaliberRuntimeApprovalRequest.workflow_run_id == workflow_run_id,
                CaliberRuntimeApprovalRequest.proposal_id.is_not(None),
                CaliberRuntimeApprovalRequest.status.in_(LIVE_REQUEST_STATUSES),
                CaliberRuntimeApprovalRequest.superseded_by.is_(None),
                CaliberRuntimeApprovalRequest.consumed_at.is_(None),
            )
            .order_by(CaliberRuntimeApprovalRequest.runtime_approval_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        .scalars()
        .all()
    )
    withdrawn: list[str] = []
    for request in live:
        request.status = STATUS_WITHDRAWN
        withdrawn.append(request.runtime_approval_id)
    session.flush()

    result = RunSettlement(workflow_run_id, tuple(settled_proposals), tuple(withdrawn))
    if result.changed:
        audit_record(
            session,
            actor=actor,
            action="settle_run_effects",
            entity_type="workflow_run",
            entity_id=workflow_run_id,
            details={
                "reason": reason,
                "proposal_count": len(settled_proposals),
                "proposals": settled_proposals[:_MAX_AUDITED_IDS],
                "request_count": len(withdrawn),
                "requests": withdrawn[:_MAX_AUDITED_IDS],
            },
        )
    return result


def _ended_runs_with_work(
    session: Session, limit: int, after: str | None = None
) -> list[tuple[str, str]]:
    """``(run id, status)`` of ended runs after ``after`` that still hold a ``proposed`` proposal or a live bound request, at most ``limit``."""
    with_proposal = select(CaliberEffectProposal.workflow_run_id).where(
        CaliberEffectProposal.state == PROPOSED
    )
    with_request = select(CaliberRuntimeApprovalRequest.workflow_run_id).where(
        CaliberRuntimeApprovalRequest.proposal_id.is_not(None),
        CaliberRuntimeApprovalRequest.status.in_(LIVE_REQUEST_STATUSES),
        CaliberRuntimeApprovalRequest.superseded_by.is_(None),
        CaliberRuntimeApprovalRequest.consumed_at.is_(None),
    )
    query = select(CaliberWorkflowRun.workflow_run_id, CaliberWorkflowRun.status).where(
        CaliberWorkflowRun.status.in_(sorted(TERMINAL_RUN_STATUSES)),
        CaliberWorkflowRun.workflow_run_id.in_(with_proposal)
        | CaliberWorkflowRun.workflow_run_id.in_(with_request),
    )
    if after is not None:
        query = query.where(CaliberWorkflowRun.workflow_run_id > after)
    rows = session.execute(query.order_by(CaliberWorkflowRun.workflow_run_id).limit(limit)).all()
    return [(run_id, status) for run_id, status in rows]


@dataclass(frozen=True)
class Sweep:
    """One sweep: how many runs it settled, how many candidates it looked at and the last one (the next sweep's ``after``)."""

    changed: int
    examined: int
    last: str | None


def sweep_ended_runs(
    session_factory: SessionFactory,
    lease: Lease,
    *,
    limit: int = DEFAULT_SWEEP_LIMIT,
    after: str | None = None,
    now: datetime | None = None,
) -> Sweep:
    """Settle ended runs' leftover proposals and live requests, resuming after the run id ``after`` (the owner-only backstop).

    For a run that ended before the end paths settled in their own transaction, or by a path that missed it. ``lease`` is the
    execution-owner lease, compared (owner, fencing token, the database's clock) in the transaction of every run it settles,
    so a process that does not hold it changes nothing (:class:`~caliber.ownership.LeaseLost` propagates). Each run is its own
    transaction (committed on success; a refusal or a database error rolls that run back, is logged by code or class only, and
    the sweep goes on), at most ``limit`` runs per call, in id order after ``after``. A run that was settled in the meantime, or
    that is not over any more, is skipped. The caller keeps the cursor: when ``examined`` equals ``limit`` it passes ``last`` as
    the next ``after``, otherwise it starts over from the beginning, so a run that keeps failing delays nothing behind it.
    Idempotent and restart-safe (a cursor is only a place to start).
    """
    if type(limit) is not int or not 1 <= limit <= MAX_SWEEP_LIMIT:
        raise ValueError("limit is out of bounds")
    with session_factory() as session:
        assert_fenced(session, lease)
        candidates = _ended_runs_with_work(session, limit, after)
        session.rollback()
    changed = 0
    for run_id, status in candidates:
        with session_factory() as session:
            assert_fenced(session, lease)
            try:
                result = settle_run_effects(
                    session, run_id, reason=REASON_FOR_RUN_STATUS[status], now=now
                )
                session.commit()
            except ProposalError as exc:
                session.rollback()
                logger.warning(
                    "run %s was not settled (%s); a later sweep retries", run_id, exc.code
                )
                continue
            except SQLAlchemyError as exc:
                session.rollback()
                logger.warning(
                    "run %s was not settled (%s); a later sweep retries",
                    run_id,
                    type(exc).__name__,
                )
                continue
        if result.changed:
            changed += 1
            logger.info(
                "settled the unsent effects of ended run %s: %d proposal(s) failed, %d request(s) withdrawn",
                run_id,
                len(result.proposals),
                len(result.requests),
            )
    return Sweep(changed, len(candidates), candidates[-1][0] if candidates else None)


def settle_ended_runs(
    session_factory: SessionFactory,
    lease: Lease,
    *,
    limit: int = DEFAULT_SWEEP_LIMIT,
    now: datetime | None = None,
) -> int:
    """One sweep from the first run (:func:`sweep_ended_runs` with no cursor); returns how many runs changed."""
    return sweep_ended_runs(session_factory, lease, limit=limit, now=now).changed


__all__ = [
    "APPROVAL_REJECTED_REASON",
    "DEFAULT_SWEEP_LIMIT",
    "LIVE_REQUEST_STATUSES",
    "REASON_FOR_RUN_STATUS",
    "RUN_CANCELLED_REASON",
    "RUN_COMPLETED_REASON",
    "RUN_EXPIRED_REASON",
    "RUN_FAILED_REASON",
    "RUN_NOT_ENDED",
    "SETTLE_REASONS",
    "RunSettlement",
    "Sweep",
    "settle_ended_runs",
    "settle_run_effects",
    "sweep_ended_runs",
]
