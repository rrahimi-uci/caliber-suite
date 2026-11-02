"""#1074: the proposals and approval requests a run that ended left unsent are settled, never dispatched.

A ``proposed`` proposal of a run that failed, was cancelled, expired, completed or had an approval rejected moves to ``failed``
(the existing ADR 0003 edge; no new state, no migration) with a ``reason`` naming why, and the run's live bound requests become
``withdrawn``. These tests pin what is settled and, as important, what is *never* touched (every state past ``proposed``, a
consumed request, another run's rows), the idempotence of a repeat and of the owner-only sweep, the vocabulary and the refusals,
and the pure withdrawal rule. Database cases run on SQLite and, where ``CALIBER_TEST_POSTGRES_URL`` is set, on PostgreSQL migrated
by Alembic. The races against a dispatch and the HTTP route are in
``tests/egi_scenarios/test_settle_never_dispatched.py``.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

import caliber.effects.run_settlement as settlement_module
import caliber.orchestrator.effect_reconciler as loop_module
from caliber.config import CaliberConfig
from caliber.db.models import (
    CaliberAuditLog,
    CaliberEffectProposal,
    CaliberRuntimeApprovalRequest,
    CaliberWorkflowRun,
    CaliberWorkflowRunCheckpoint,
)
from caliber.effects.records import (
    COMMITTED,
    DISPATCHED,
    FAILED,
    PROPOSED,
    RECONCILED,
    UNKNOWN,
    ProposalError,
    proposal_state,
)
from caliber.effects.run_settlement import (
    APPROVAL_REJECTED_REASON,
    REASON_FOR_RUN_STATUS,
    SETTLE_REASONS,
    RunSettlement,
    Sweep,
    settle_ended_runs,
    settle_run_effects,
    sweep_ended_runs,
)
from caliber.orchestrator.workflow_run_worker import WorkflowRunWorker
from caliber.ownership import LeaseLost
from caliber.workflows.approval_policy import (
    REFUSAL_ALREADY_CONSUMED,
    REFUSAL_EXPIRED,
    REFUSAL_NOT_APPROVED,
    REFUSAL_SUPERSEDED,
    REFUSAL_WITHDRAWN,
    STATUS_WITHDRAWN,
    approval_refusal,
)
from caliber.workflows.approval_requests import (
    NOT_BOUND,
    ApprovalWithdrawRefused,
    withdraw_approval,
)
from caliber.workflows.run_state import TERMINAL_RUN_STATUSES
from caliber.workflows.runtime import WorkflowRunResult
from tests.dialect_helpers import DIALECTS
from tests.test_approval_supersession import _proposal, _request
from tests.test_effect_proposals import _seed_run
from tests.test_effect_reconciler_loop import _lease, _logged, _task
from tests.test_effect_records import AT, NAIVE_AT, _at
from tests.test_event_outbox import _database

RUN = "RUN-ep"


@pytest.fixture(params=DIALECTS)
def database(
    request: pytest.FixtureRequest, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Any:
    with _database(request.param, tmp_path, monkeypatch) as (engine, factory):
        _seed_run(factory)
        yield engine, factory


def _end(factory: sessionmaker[Session], status: str, run_id: str = RUN) -> None:
    with factory() as session:
        run = session.get(CaliberWorkflowRun, run_id)
        assert run is not None
        run.status = status
        session.commit()


def _second_run(factory: sessionmaker[Session], run_id: str = "RUN-other") -> str:
    with factory() as session:
        session.add(
            CaliberWorkflowRun(
                workflow_run_id=run_id, workflow_id="wf-ep", project_id="PRJ-ep", status="failed"
            )
        )
        session.commit()
    return run_id


def _proposed_with_request(
    factory: sessionmaker[Session],
    occurrence: str,
    *,
    run: str = RUN,
    status: str = "approved",
    node: str = "ask",
) -> tuple[str, str]:
    """A ``proposed`` proposal and its bound request in ``status``; returns ``(proposal_id, request_id)``."""
    with factory() as session:
        proposal = _proposal(session, run=run, node=node, occurrence=occurrence)
        request = _request(session, proposal)
        request.status = status
        if status == "approved":
            request.decided_by = "approver_a"
            request.decided_at = NAIVE_AT
        session.commit()
        return proposal.proposal_id, request.runtime_approval_id


def _settle(
    factory: sessionmaker[Session], reason: str = "run_failed", run: str = RUN
) -> RunSettlement:
    with factory() as session:
        result = settle_run_effects(session, run, reason=reason, now=AT)
        session.commit()
    return result


def _audit(factory: sessionmaker[Session]) -> list[CaliberAuditLog]:
    with factory() as session:
        return list(
            session.execute(
                select(CaliberAuditLog).where(CaliberAuditLog.action == "settle_run_effects")
            ).scalars()
        )


def _request_row(factory: sessionmaker[Session], request_id: str) -> CaliberRuntimeApprovalRequest:
    with factory() as session:
        row = session.get(CaliberRuntimeApprovalRequest, request_id)
        assert row is not None
        return row


# ---------------------------------------------------------------- the vocabulary


def test_every_terminal_run_status_has_one_reason_and_the_reasons_are_the_documented_set() -> None:
    assert set(REASON_FOR_RUN_STATUS) == set(TERMINAL_RUN_STATUSES)
    assert {
        "run_failed",
        "run_cancelled",
        "run_expired",
        "run_completed",
        "approval_rejected",
    } == SETTLE_REASONS
    assert set(REASON_FOR_RUN_STATUS.values()) <= SETTLE_REASONS
    assert APPROVAL_REJECTED_REASON in SETTLE_REASONS
    assert STATUS_WITHDRAWN == REFUSAL_WITHDRAWN == "withdrawn"


# ---------------------------------------------------------------- settling an ended run


@pytest.mark.parametrize(
    ("status", "reason"),
    [
        ("failed", "run_failed"),
        ("cancelled", "run_cancelled"),
        ("expired", "run_expired"),
        ("completed", "run_completed"),
        ("failed", "approval_rejected"),
    ],
)
def test_a_proposed_proposal_of_an_ended_run_fails_with_the_reason_and_its_requests_are_withdrawn(
    database: Any, status: str, reason: str
) -> None:
    _, factory = database
    pending_p, pending_r = _proposed_with_request(factory, "a", status="pending")
    approved_p, approved_r = _proposed_with_request(factory, "b", status="approved", node="other")
    _end(factory, status)

    result = _settle(factory, reason)

    assert result.proposals == tuple(sorted((pending_p, approved_p)))
    assert result.requests == tuple(sorted((pending_r, approved_r)))
    assert result.changed
    with factory() as session:
        for proposal_id in (pending_p, approved_p):
            state = proposal_state(session, proposal_id)
            assert (state.state, state.reason, state.version) == (FAILED, reason, 2)
            assert state.settled_at == NAIVE_AT
            # it was never sent: no dispatch evidence, no receipt
            assert state.dispatched_at is None and state.owner_id is None and state.attempt is None
            assert state.receipt_id is None and state.approval_ref is None
    for request_id in (pending_r, approved_r):
        row = _request_row(factory, request_id)
        assert (row.status, row.consumed_at, row.superseded_by) == ("withdrawn", None, None)
    # the recorded decision is not rewritten: who approved and when stay as they were
    assert _request_row(factory, approved_r).decided_by == "approver_a"
    # the settled request refuses every consumer with the stable code
    row = _request_row(factory, approved_r)
    assert (
        approval_refusal(
            status=row.status,
            superseded_by=row.superseded_by,
            expires_at=row.expires_at,
            now=AT,
        )
        == REFUSAL_WITHDRAWN
    )
    [audit] = _audit(factory)
    assert audit.entity_id == RUN and audit.action == "settle_run_effects"
    assert audit.details["reason"] == reason
    assert (audit.details["proposal_count"], audit.details["request_count"]) == (2, 2)
    assert audit.details["proposals"] == list(result.proposals)
    assert audit.details["requests"] == list(result.requests)


def test_only_a_proposed_proposal_is_settled_every_later_state_is_left_exactly_as_it_is(
    database: Any,
) -> None:
    """``dispatched`` and ``unknown`` may have been sent; ``approved``, ``reconciled`` and settled rows are final or owned elsewhere."""
    _, factory = database
    kept: dict[str, str] = {}
    with factory() as session:
        for state, occurrence in (
            (DISPATCHED, "d"),
            (UNKNOWN, "u"),
            (RECONCILED, "r"),
            (COMMITTED, "c"),
            (FAILED, "f"),
            ("approved", "ap"),
        ):
            kept[state] = _at(session, state, occurrence=occurrence)
        session.commit()
    proposed, _ = _proposed_with_request(factory, "p", status="approved")
    with factory() as session:
        before = {s: proposal_state(session, p) for s, p in kept.items()}
    _end(factory, "failed")

    result = _settle(factory)

    assert result.proposals == (proposed,)
    with factory() as session:
        for state, proposal_id in kept.items():
            assert proposal_state(session, proposal_id) == before[state], state


def test_a_consumed_superseded_rejected_or_legacy_request_is_not_withdrawn(database: Any) -> None:
    _, factory = database
    _, consumed = _proposed_with_request(factory, "c")
    _, superseded = _proposed_with_request(factory, "s", node="n2")
    _, rejected = _proposed_with_request(factory, "x", status="rejected", node="n3")
    with factory() as session:
        row = session.get(CaliberRuntimeApprovalRequest, consumed)
        assert row is not None
        row.consumed_at = NAIVE_AT
        row.commit_decision_id = "pd-1"
        old = session.get(CaliberRuntimeApprovalRequest, superseded)
        assert old is not None
        old.status = "superseded"
        old.superseded_by = "RAP-newer"
        # a legacy node-level request guards no effect proposal
        session.add(
            CaliberRuntimeApprovalRequest(
                runtime_approval_id="RAP-legacy",
                workflow_run_id=RUN,
                project_id="PRJ-ep",
                node_id="gate",
                status="pending",
            )
        )
        session.commit()
    _end(factory, "cancelled")

    result = _settle(factory, "run_cancelled")

    assert consumed not in result.requests
    assert set(result.requests).isdisjoint({consumed, superseded, rejected, "RAP-legacy"})
    assert _request_row(factory, consumed).status == "approved"
    assert _request_row(factory, superseded).status == "superseded"
    assert _request_row(factory, rejected).status == "rejected"
    assert _request_row(factory, "RAP-legacy").status == "pending"


def test_another_runs_rows_are_untouched(database: Any) -> None:
    _, factory = database
    other = _second_run(factory)
    other_p, other_r = _proposed_with_request(factory, "o", run=other)
    mine_p, mine_r = _proposed_with_request(factory, "m")
    _end(factory, "failed")

    result = _settle(factory)

    assert result.proposals == (mine_p,) and result.requests == (mine_r,)
    with factory() as session:
        assert proposal_state(session, other_p).state == PROPOSED
    assert _request_row(factory, other_r).status == "approved"


def test_a_repeat_changes_nothing_and_writes_no_audit_row(database: Any) -> None:
    _, factory = database
    proposal_id, request_id = _proposed_with_request(factory, "a")
    _end(factory, "failed")
    first = _settle(factory)
    with factory() as session:
        after_first = proposal_state(session, proposal_id)

    second = _settle(factory)

    assert first.changed and not second.changed
    assert (second.proposals, second.requests) == ((), ())
    with factory() as session:
        assert proposal_state(session, proposal_id) == after_first
    assert _request_row(factory, request_id).status == "withdrawn"
    assert len(_audit(factory)) == 1


def test_an_ended_run_with_nothing_to_settle_is_a_quiet_no_op(database: Any) -> None:
    _, factory = database
    _end(factory, "completed")
    assert not _settle(factory, "run_completed").changed
    assert _audit(factory) == []


def test_the_terminal_status_the_caller_just_wrote_is_seen_without_a_commit(
    database: Any,
) -> None:
    """The session factory has autoflush off; the settlement flushes the status the end path set in the same transaction."""
    _, factory = database
    proposal_id, _ = _proposed_with_request(factory, "a")
    with factory(autoflush=False) as session:
        run = session.get(CaliberWorkflowRun, RUN)
        assert run is not None
        run.status = "failed"  # not flushed, not committed
        result = settle_run_effects(session, RUN, reason="run_failed", now=AT)
        assert result.proposals == (proposal_id,)
        session.rollback()  # the whole end path rolls back together: nothing is half-settled
    with factory() as session:
        assert proposal_state(session, proposal_id).state == PROPOSED
        run = session.get(CaliberWorkflowRun, RUN)
        assert run is not None and run.status == "running"
    assert _audit(factory) == []


def test_a_run_that_has_not_ended_is_refused_and_nothing_is_touched(database: Any) -> None:
    _, factory = database
    proposal_id, request_id = _proposed_with_request(factory, "a")
    for live in ("queued", "running", "waiting_approval", "waiting_effect", "waiting_event"):
        _end(factory, live)
        with factory() as session, pytest.raises(ProposalError) as refused:
            settle_run_effects(session, RUN, reason="run_failed")
        assert refused.value.code == "run_not_ended", live
    with factory() as session:
        assert proposal_state(session, proposal_id).state == PROPOSED
    assert _request_row(factory, request_id).status == "approved"


def test_an_unknown_run_and_a_reason_outside_the_vocabulary_are_refused(database: Any) -> None:
    _, factory = database
    _proposed_with_request(factory, "a")
    _end(factory, "failed")
    with factory() as session, pytest.raises(ProposalError) as missing:
        settle_run_effects(session, "RUN-nope", reason="run_failed")
    assert missing.value.code == "run_not_found"
    for bad in ("", "because", "run_succeeded", None, 7):
        with factory() as session, pytest.raises(ProposalError) as refused:
            settle_run_effects(session, RUN, reason=bad)  # type: ignore[arg-type]
        assert refused.value.code == "bad_field"
    with factory() as session:
        assert (
            session.execute(
                select(func.count())
                .select_from(CaliberEffectProposal)
                .where(CaliberEffectProposal.state == PROPOSED)
            ).scalar_one()
            == 1
        )


def test_the_audit_row_lists_at_most_fifty_ids_and_counts_them_all(database: Any) -> None:
    _, factory = database
    with factory() as session:
        for number in range(53):
            _proposal(session, occurrence=f"many-{number:02d}")
        session.commit()
    _end(factory, "failed")

    result = _settle(factory)

    assert len(result.proposals) == 53
    [audit] = _audit(factory)
    assert audit.details["proposal_count"] == 53
    assert len(audit.details["proposals"]) == 50
    assert audit.details["proposals"] == list(result.proposals)[:50]


# ---------------------------------------------------------------- the owner-only sweep


def test_the_sweep_settles_ended_runs_only_and_a_second_sweep_finds_nothing(
    database: Any,
) -> None:
    _, factory = database
    ended = _second_run(factory, "RUN-ended")
    ended_p, ended_r = _proposed_with_request(factory, "e", run=ended)
    live_p, live_r = _proposed_with_request(factory, "l")  # RUN-ep is still running
    dispatched = None
    with factory() as session:
        dispatched = _at(session, DISPATCHED, occurrence="sent")
        session.commit()
    lease = _lease(factory)

    assert settle_ended_runs(factory, lease) == 1

    with factory() as session:
        assert proposal_state(session, ended_p).state == FAILED
        assert proposal_state(session, ended_p).reason == "run_failed"
        assert proposal_state(session, live_p).state == PROPOSED
        assert proposal_state(session, dispatched).state == DISPATCHED
    assert _request_row(factory, ended_r).status == "withdrawn"
    assert _request_row(factory, live_r).status == "approved"
    assert settle_ended_runs(factory, lease) == 0
    assert len(_audit(factory)) == 1


def test_the_sweep_names_each_runs_own_reason(database: Any) -> None:
    _, factory = database
    expected = {}
    for number, status in enumerate(sorted(TERMINAL_RUN_STATUSES)):
        run = _second_run(factory, f"RUN-{status}")
        _end(factory, status, run)
        expected[status] = _proposed_with_request(factory, f"o{number}", run=run)[0]
    lease = _lease(factory)

    assert settle_ended_runs(factory, lease) == len(TERMINAL_RUN_STATUSES)

    with factory() as session:
        for status, proposal_id in expected.items():
            assert proposal_state(session, proposal_id).reason == REASON_FOR_RUN_STATUS[status]


def test_the_sweep_finds_a_run_whose_only_leftover_is_a_live_request(database: Any) -> None:
    _, factory = database
    ended = _second_run(factory, "RUN-req-only")
    _, request_id = _proposed_with_request(factory, "e", run=ended)
    lease = _lease(factory)
    settle_ended_runs(factory, lease)  # settles both
    with factory() as session:
        request = session.get(CaliberRuntimeApprovalRequest, request_id)
        assert request is not None
        request.status = "pending"  # re-open it by hand to model a request a path left live
        session.commit()

    assert settle_ended_runs(factory, lease) == 1
    assert _request_row(factory, request_id).status == "withdrawn"


def test_the_sweep_is_bounded_and_continues_where_it_left_off(database: Any) -> None:
    _, factory = database
    ids = []
    for number in range(3):
        run = _second_run(factory, f"RUN-b{number}")
        ids.append(_proposed_with_request(factory, f"b{number}", run=run)[0])
    lease = _lease(factory)

    assert settle_ended_runs(factory, lease, limit=2) == 2
    with factory() as session:
        assert sorted(proposal_state(session, p).state for p in ids) == [FAILED, FAILED, PROPOSED]
    assert settle_ended_runs(factory, lease, limit=2) == 1
    assert settle_ended_runs(factory, lease, limit=2) == 0


@pytest.mark.parametrize("limit", [0, -1, 1001, True, 1.5, "5", None])
def test_the_sweep_limit_is_bounded(database: Any, limit: Any) -> None:
    _, factory = database
    with pytest.raises(ValueError, match="limit"):
        settle_ended_runs(factory, _lease(factory), limit=limit)


def test_a_process_that_does_not_hold_the_lease_settles_nothing(database: Any) -> None:
    _, factory = database
    ended = _second_run(factory, "RUN-ended")
    proposal_id, request_id = _proposed_with_request(factory, "e", run=ended)
    stale = _lease(factory, "owner-old")
    _lease(factory, "owner-new")  # a takeover: ``stale`` is not the held lease any more

    with pytest.raises(LeaseLost):
        settle_ended_runs(factory, stale)

    with factory() as session:
        assert proposal_state(session, proposal_id).state == PROPOSED
    assert _request_row(factory, request_id).status == "approved"


def test_a_lease_lost_between_two_runs_stops_the_sweep_at_the_next_run(
    database: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, factory = database
    first = _second_run(factory, "RUN-1")
    second = _second_run(factory, "RUN-2")
    first_p, _ = _proposed_with_request(factory, "one", run=first)
    second_p, _ = _proposed_with_request(factory, "two", run=second)
    lease = _lease(factory)
    real = settlement_module.assert_fenced
    calls: list[str] = []

    def take_over_before_the_second_run(session: Session, held: Any) -> None:
        calls.append("fenced")
        if (
            len(calls) == 3
        ):  # the candidate read, the first run, then the second run's own transaction
            _lease(factory, "owner-new")
        real(session, held)

    monkeypatch.setattr(settlement_module, "assert_fenced", take_over_before_the_second_run)

    with pytest.raises(LeaseLost):
        settle_ended_runs(factory, lease)

    assert len(calls) == 3
    with factory() as session:
        assert proposal_state(session, first_p).state == FAILED, (
            "the first run was settled under the lease"
        )
        assert proposal_state(session, second_p).state == PROPOSED, "nothing after the takeover"


def test_a_run_that_cannot_be_settled_is_skipped_and_the_sweep_goes_on(
    database: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, factory = database
    broken = _second_run(factory, "RUN-1")
    fine = _second_run(factory, "RUN-2")
    broken_p, _ = _proposed_with_request(factory, "one", run=broken)
    fine_p, _ = _proposed_with_request(factory, "two", run=fine)
    real = settlement_module.settle_run_effects

    def refuse_first(session: Session, run_id: str, **kwargs: Any) -> RunSettlement:
        if run_id == broken:
            raise ProposalError("stale_version", "moved under us")
        return real(session, run_id, **kwargs)

    monkeypatch.setattr(settlement_module, "settle_run_effects", refuse_first)
    records: list[str] = []

    class _Capture(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            records.append(record.getMessage())

    logger = logging.getLogger("caliber.effects.run_settlement")
    handler = _Capture()
    logger.addHandler(handler)
    try:
        assert settle_ended_runs(factory, _lease(factory)) == 1
    finally:
        logger.removeHandler(handler)

    with factory() as session:
        assert proposal_state(session, broken_p).state == PROPOSED
        assert proposal_state(session, fine_p).state == FAILED
    assert any("RUN-1" in m and "stale_version" in m for m in records)


def test_a_database_error_on_one_run_is_skipped_by_class_and_the_sweep_goes_on(
    database: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A trigger or constraint error is not a ``ProposalError``: it must not abort the sweep or leak its text."""
    _, factory = database
    broken = _second_run(factory, "RUN-1")
    fine = _second_run(factory, "RUN-2")
    broken_p, _ = _proposed_with_request(factory, "one", run=broken)
    fine_p, _ = _proposed_with_request(factory, "two", run=fine)
    real = settlement_module.settle_run_effects

    def explode_first(session: Session, run_id: str, **kwargs: Any) -> RunSettlement:
        if run_id == broken:
            raise IntegrityError("DB-TEXT-MARKER", {}, Exception("raw"))
        return real(session, run_id, **kwargs)

    monkeypatch.setattr(settlement_module, "settle_run_effects", explode_first)
    records: list[str] = []

    class _Capture(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            records.append(record.getMessage())

    logger = logging.getLogger("caliber.effects.run_settlement")
    handler = _Capture()
    logger.addHandler(handler)
    try:
        assert settle_ended_runs(factory, _lease(factory)) == 1
    finally:
        logger.removeHandler(handler)

    with factory() as session:
        assert proposal_state(session, broken_p).state == PROPOSED
        assert proposal_state(session, fine_p).state == FAILED
    joined = " ".join(records)
    assert "IntegrityError" in joined and "DB-TEXT-MARKER" not in joined


def test_a_run_that_keeps_failing_cannot_starve_the_runs_behind_it(
    database: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Candidates are the first ``limit`` by id; the cursor moves past failing ones so a good run behind them is reached."""
    _, factory = database
    bad = [_second_run(factory, f"RUN-a{n}") for n in range(3)]
    good = _second_run(factory, "RUN-z")
    bad_p = [_proposed_with_request(factory, f"bad{n}", run=run)[0] for n, run in enumerate(bad)]
    good_p, _ = _proposed_with_request(factory, "good", run=good)
    real = settlement_module.settle_run_effects

    def refuse_bad(session: Session, run_id: str, **kwargs: Any) -> RunSettlement:
        if run_id in bad:
            raise ProposalError("stale_version", "moved under us")
        return real(session, run_id, **kwargs)

    monkeypatch.setattr(settlement_module, "settle_run_effects", refuse_bad)
    lease = _lease(factory)

    first = sweep_ended_runs(factory, lease, limit=2)
    assert (first.changed, first.examined, first.last) == (0, 2, "RUN-a1")
    second = sweep_ended_runs(factory, lease, limit=2, after=first.last)
    assert (second.changed, second.examined, second.last) == (1, 2, "RUN-z")
    third = sweep_ended_runs(factory, lease, limit=2, after=second.last)
    assert (third.changed, third.examined, third.last) == (0, 0, None), "past the end: start over"

    with factory() as session:
        assert proposal_state(session, good_p).state == FAILED
        assert {proposal_state(session, p).state for p in bad_p} == {PROPOSED}


def test_the_reconciler_continues_after_a_full_batch_and_starts_over_after_a_short_one(
    database: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, factory = database
    seen: list[str | None] = []
    answers = iter(
        [
            Sweep(0, loop_module.DEFAULT_SWEEP_LIMIT, "RUN-last"),
            Sweep(0, 3, "RUN-end"),
            Sweep(0, 0, None),
        ]
    )

    def fake(_factory: Any, _lease: Any, *, after: str | None = None, **_kw: Any) -> Sweep:
        seen.append(after)
        return next(answers)

    monkeypatch.setattr(loop_module, "sweep_ended_runs", fake)
    lease = _lease(factory)
    task = _task(factory, provider=lambda: lease)
    for _ in range(3):
        task._last_settle = float("-inf")  # the once-a-minute throttle is not under test
        task.tick()

    assert seen == [None, "RUN-last", None]


def test_a_run_that_is_no_longer_over_when_the_sweep_reaches_it_is_skipped(
    database: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The candidate list was read, then the run came back to life: the run-lock re-check refuses and nothing is settled."""
    _, factory = database
    proposal_id, request_id = _proposed_with_request(factory, "a")  # RUN-ep is ``running``
    monkeypatch.setattr(settlement_module, "_ended_runs_with_work", lambda *_a: [(RUN, "failed")])

    assert settle_ended_runs(factory, _lease(factory)) == 0

    with factory() as session:
        assert proposal_state(session, proposal_id).state == PROPOSED
    assert _request_row(factory, request_id).status == "approved"


# ---------------------------------------------------------------- the reconciler loop runs the sweep


def test_the_owner_settles_ended_runs_on_its_tick_once_a_minute(database: Any) -> None:
    _, factory = database
    ended = _second_run(factory, "RUN-ended")
    first_p, first_r = _proposed_with_request(factory, "one", run=ended)
    lease = _lease(factory)
    task = _task(factory, provider=lambda: lease)

    report = task.tick()

    assert report is not None
    with factory() as session:
        assert proposal_state(session, first_p).state == FAILED
    assert _request_row(factory, first_r).status == "withdrawn"
    # within the minute the next tick does not sweep again (nothing new is picked up) ...
    later = _second_run(factory, "RUN-later")
    later_p, _ = _proposed_with_request(factory, "two", run=later)
    task.tick()
    with factory() as session:
        assert proposal_state(session, later_p).state == PROPOSED
    # ... and after it does
    task._last_settle -= loop_module._SETTLE_EVERY_SECONDS
    task.tick()
    with factory() as session:
        assert proposal_state(session, later_p).state == FAILED


def test_a_secondary_a_stop_or_a_lost_lease_settles_nothing_and_a_failure_never_stops_the_loop(
    database: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, factory = database
    ended = _second_run(factory, "RUN-ended")
    proposal_id, _ = _proposed_with_request(factory, "one", run=ended)

    assert _task(factory, provider=lambda: None).tick() is None  # a secondary
    stopping = _task(factory, provider=lambda: _lease(factory))
    stopping._stop_requested.set()
    stopping.tick()
    old = _lease(factory, "owner-old")
    _lease(factory, "owner-new")
    with _logged() as records:
        report = _task(factory, provider=lambda: old).tick()
    assert report is not None
    assert any(
        "settlement skipped: the execution-owner lease is gone" in m for m in records.messages
    )
    with factory() as session:
        assert proposal_state(session, proposal_id).state == PROPOSED

    def broken(*_args: Any, **_kwargs: Any) -> int:
        raise RuntimeError("PAYLOAD-MARKER-SETTLE")

    monkeypatch.setattr(loop_module, "sweep_ended_runs", broken)
    current = _lease(factory)
    with _logged() as records:
        report = _task(factory, provider=lambda: current).tick()
    assert report is not None
    joined = " ".join(records.messages)
    assert "ended-run settlement failed (RuntimeError)" in joined
    assert "PAYLOAD-MARKER-SETTLE" not in joined


# ---------------------------------------------------------------- the pure withdrawal rule


def _row(**fields: Any) -> CaliberRuntimeApprovalRequest:
    values: dict[str, Any] = {
        "runtime_approval_id": "RAP-x",
        "workflow_run_id": RUN,
        "node_id": "ask",
        "status": "approved",
        "proposal_id": "EP-x",
        "expires_at": NAIVE_AT + timedelta(hours=1),
        "consumed_at": None,
        "superseded_by": None,
    }
    values.update(fields)
    return CaliberRuntimeApprovalRequest(**values)


def test_an_approved_unconsumed_request_is_withdrawn_and_a_repeat_is_the_same_answer() -> None:
    row = _row()
    assert withdraw_approval(row, now=AT) is True
    assert row.status == "withdrawn"
    assert withdraw_approval(row, now=AT) is False
    assert row.status == "withdrawn"


@pytest.mark.parametrize(
    ("fields", "code"),
    [
        ({"proposal_id": None}, NOT_BOUND),
        ({"consumed_at": NAIVE_AT}, REFUSAL_ALREADY_CONSUMED),
        # consumed stays terminal even after its expiry and whatever status it holds
        (
            {"consumed_at": NAIVE_AT, "expires_at": NAIVE_AT - timedelta(days=1)},
            REFUSAL_ALREADY_CONSUMED,
        ),
        ({"status": "superseded"}, REFUSAL_SUPERSEDED),
        ({"superseded_by": "RAP-new"}, REFUSAL_SUPERSEDED),
        ({"expires_at": NAIVE_AT}, REFUSAL_EXPIRED),
        ({"status": "pending"}, REFUSAL_NOT_APPROVED),
        ({"status": "rejected"}, REFUSAL_NOT_APPROVED),
    ],
)
def test_a_request_that_cannot_be_withdrawn_is_refused_with_a_stable_code_and_not_changed(
    fields: dict[str, Any], code: str
) -> None:
    row = _row(**fields)
    before = row.status

    with pytest.raises(ApprovalWithdrawRefused) as refused:
        withdraw_approval(row, now=AT)

    assert refused.value.code == code and str(refused.value) == code
    assert row.status == before


def test_a_withdrawn_request_is_refused_before_the_clock_and_after_a_supersession() -> None:
    late = AT + timedelta(days=2)
    kwargs: dict[str, Any] = {"superseded_by": None, "expires_at": NAIVE_AT + timedelta(hours=1)}
    assert approval_refusal(status="withdrawn", now=AT, **kwargs) == REFUSAL_WITHDRAWN
    assert approval_refusal(status="withdrawn", now=late, **kwargs) == REFUSAL_WITHDRAWN
    assert (
        approval_refusal(status="withdrawn", now=AT, superseded_by="RAP-n", expires_at=None)
        == REFUSAL_SUPERSEDED
    )
    assert approval_refusal(status="withdrawn", now=AT, superseded_by=None, expires_at=None) == (
        REFUSAL_WITHDRAWN
    )


# ---------------------------------------------------------------- the worker's end paths settle in their own transaction


def _worker(factory: sessionmaker[Session]) -> WorkflowRunWorker:
    return WorkflowRunWorker(factory, config=CaliberConfig.load(environ={}))


def _end_with(factory: sessionmaker[Session], how: str) -> None:
    """End ``RUN-ep`` through the worker's own method for ``how`` (the run is ``running``, as when a worker ends it)."""
    worker = _worker(factory)
    with factory() as session:
        run = session.get(CaliberWorkflowRun, RUN)
        assert run is not None
        result = WorkflowRunResult(status="completed", output="done")
        if how == "failed":
            worker._mark_failed(session, run, error_code="boom", error_summary="it broke")
        elif how == "cancelled":
            worker._mark_cancelled(session, run, result)
        elif how == "completed":
            worker._mark_completed(session, run, result)
        else:  # the maintenance path that fails a run waiting on an event whose checkpoint is unusable
            run.status = "waiting_event"
            session.flush()
            worker._fail_waiting_event_run_from_maintenance(
                session,
                run,
                checkpoint_id="CP-x",
                checkpoint_node_id="gate",
                published=[],
                error_code="resume_checkpoint_unavailable",
                error_summary="unusable",
                reason="invalid_wait_for_event_checkpoint",
            )
            session.commit()


@pytest.mark.parametrize(
    ("how", "status", "reason"),
    [
        ("failed", "failed", "run_failed"),
        ("cancelled", "cancelled", "run_cancelled"),
        ("completed", "completed", "run_completed"),
        ("maintenance", "failed", "run_failed"),
    ],
)
def test_the_workers_end_of_run_paths_settle_unsent_effects_in_the_same_transaction(
    database: Any, how: str, status: str, reason: str
) -> None:
    _, factory = database
    proposal_id, request_id = _proposed_with_request(factory, "w")
    with factory() as session:
        sent = _at(session, DISPATCHED, occurrence="sent")
        session.commit()

    _end_with(factory, how)

    with factory() as session:
        run = session.get(CaliberWorkflowRun, RUN)
        assert run is not None and run.status == status
        state = proposal_state(session, proposal_id)
        assert (state.state, state.reason) == (FAILED, reason)
        assert proposal_state(session, sent).state == DISPATCHED, (
            "a sent effect is the reconciler's"
        )
    assert _request_row(factory, request_id).status == "withdrawn"
    assert len(_audit(factory)) == 1


def test_the_workers_expiry_path_settles_the_runs_unsent_effects(database: Any) -> None:
    """A run waiting on an event past its deadline is ``expired`` by the worker's maintenance pass."""
    _, factory = database
    proposal_id, request_id = _proposed_with_request(factory, "x")
    with factory() as session:
        run = session.get(CaliberWorkflowRun, RUN)
        assert run is not None
        run.status = "waiting_event"
        run.current_node_id = "gate"
        session.add(
            CaliberWorkflowRunCheckpoint(
                checkpoint_id="CP-wait",
                workflow_run_id=RUN,
                project_id="PRJ-ep",
                sequence=1,
                node_id="gate",
                state_blob={
                    "kind": "wait_for_event",
                    "node_id": "gate",
                    "timeout_seconds": 30,
                    "expected_event_name": "ticket.approved",
                },
            )
        )
        run.summary = {"resume_checkpoint_id": "CP-wait"}
        session.commit()
        checkpoint = session.get(CaliberWorkflowRunCheckpoint, "CP-wait")
        assert checkpoint is not None
        checkpoint.created_at = datetime.now(timezone.utc) - timedelta(seconds=45)
        session.commit()

    _worker(factory)._tick()

    with factory() as session:
        run = session.get(CaliberWorkflowRun, RUN)
        assert run is not None and run.status == "expired"
        state = proposal_state(session, proposal_id)
        assert (state.state, state.reason) == (FAILED, "run_expired")
    assert _request_row(factory, request_id).status == "withdrawn"
