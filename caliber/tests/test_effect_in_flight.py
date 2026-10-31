"""B05.5d (#997): a dispatch whose settle failed under the lease that is still current never stays ``dispatched`` for good.

``abandon_dispatch`` only helps when the dispatching lease was taken over. The gap this file closes is the same-token one: the
dispatcher kept its lease, sent the effect, and then could not record the outcome. Two rules close it and neither ever sends
anything again (there is no edge from ``unknown`` back to ``dispatched``):

* the dispatcher records ``unknown`` at once, under its own captured lease, for any settle failure that is not "someone else owns
  the row now";
* the reconciler's bounded in-flight rule (:func:`caliber.effects.records.expire_in_flight`) moves a row that stayed
  ``dispatched`` under the current token for longer than the bound, which covers a dispatcher that could not even do that.

The dispatcher's own settle and the reconciler racing for one row resolve at the row: exactly one wins, the effect is sent once
and the loser records nothing.
"""

from __future__ import annotations

import logging
import threading
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session, sessionmaker

import caliber.effects.dispatcher as dispatcher_module
from caliber.db.models import (
    CaliberAuditLog,
    CaliberEffectProposal,
    CaliberEffectReceipt,
    CaliberRuntimeApprovalRequest,
)
from caliber.effects import reconcile
from caliber.effects.connectors import Classified, TransportOutcome
from caliber.effects.dispatcher import (
    BACKENDS,
    BackendRegistration,
    EffectRefused,
    TransportResult,
    dispatch,
    submit_proposal,
)
from caliber.effects.receipts import ReceiptFacts
from caliber.effects.reconcile import (
    CONNECTORS,
    IN_FLIGHT,
    MOVED,
    RECONCILED_COMMITTED,
    STILL_UNKNOWN,
    Answer,
    PassResult,
    candidates,
    reconcile_one,
    reconcile_pass,
    reconcile_pass_from,
)
from caliber.effects.records import (
    BAD_FIELD,
    COMMITTED,
    DISPATCHED,
    ILLEGAL_TRANSITION,
    IN_FLIGHT_EXPIRED_REASON,
    LEASE_LOST,
    NOT_EXPIRED,
    UNKNOWN,
    ProposalError,
    StaleVersion,
    expire_in_flight,
    proposal_state,
    proposal_view,
)
from caliber.ownership import Lease, acquire, release
from tests.dialect_helpers import DIALECTS, POSTGRES_TEST_URL
from tests.effect_helpers import TTL_SECONDS, leased
from tests.test_effect_dispatcher import AT as DISPATCHED_AT
from tests.test_effect_dispatcher import EVIDENCE, POLICY, _validator
from tests.test_effect_proposals import _seed_run
from tests.test_effect_receipts import _proposal
from tests.test_effect_records import AT, ATTEMPT, OWNER
from tests.test_event_outbox import _database

PROJECT = "PRJ-ep"
TARGETS = {("document-request", PROJECT): "http://downstream.invalid"}
BOUND = 900.0
NOW = datetime(2026, 3, 4, 5, 6, 7, tzinfo=timezone.utc) + timedelta(seconds=BOUND + 1)


@pytest.fixture(autouse=True)
def _trusted_test_validator(monkeypatch: pytest.MonkeyPatch) -> None:
    """The canonical-validator seam (see ``test_effect_dispatcher``): these fixtures do not seed the run-authority graph."""
    monkeypatch.setattr(dispatcher_module, "approval_valid_at_commit", _validator)


@pytest.fixture(params=DIALECTS)
def database(
    request: pytest.FixtureRequest, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Any:
    with _database(request.param, tmp_path, monkeypatch) as (engine, factory):
        _seed_run(factory)
        yield engine, factory


def _state(factory: sessionmaker[Session], proposal_id: str) -> Any:
    with factory() as session:
        return proposal_state(session, proposal_id)


def _receipts(factory: sessionmaker[Session]) -> int:
    with factory() as session:
        return len(session.execute(select(CaliberEffectReceipt.receipt_id)).all())


def _audits(factory: sessionmaker[Session], action: str) -> list[dict[str, Any]]:
    with factory() as session:
        rows = session.execute(
            select(CaliberAuditLog).where(CaliberAuditLog.action == action)
        ).scalars()
        return [dict(row.details or {}) for row in rows]


# ---------------------------------------------------------------- expire_in_flight (the record-level rule)


def _held(factory: sessionmaker[Session], proposal_id: str) -> tuple[Lease, int]:
    with factory() as session:
        lease = leased(session, OWNER)
        session.commit()
        return lease, proposal_state(session, proposal_id).version


def test_an_attempt_in_flight_too_long_under_the_current_lease_becomes_unknown(
    database: Any,
) -> None:
    _, factory = database
    with factory() as session:
        proposal_id = _proposal(session)
        session.commit()
    lease, version = _held(factory, proposal_id)
    before = _state(factory, proposal_id)
    with factory() as session:
        moved = expire_in_flight(
            session,
            proposal_id,
            expected_version=version,
            lease=lease,
            older_than=NOW - timedelta(seconds=BOUND),
            now=NOW,
        )
        session.commit()
    after = _state(factory, proposal_id)
    assert (moved.applied, moved.state, moved.version) == (True, UNKNOWN, version + 1)
    assert (after.state, after.reason, after.owner_id, after.attempt) == (
        UNKNOWN,
        IN_FLIGHT_EXPIRED_REASON,
        OWNER,
        ATTEMPT,
    )
    assert after.dispatch_token == before.dispatch_token == lease.fencing_token
    assert after.unknown_at is not None and after.dispatched_at == before.dispatched_at


def test_expiry_refuses_what_is_not_in_flight_under_this_lease_and_changes_nothing(
    database: Any,
) -> None:
    _, factory = database
    with factory() as session:
        proposal_id = _proposal(session)
        session.commit()
    lease, version = _held(factory, proposal_id)
    cutoff = NOW - timedelta(seconds=BOUND)

    def attempt(**changes: Any) -> ProposalError:
        arguments: dict[str, Any] = {
            "expected_version": version,
            "lease": lease,
            "older_than": cutoff,
            "now": NOW,
        } | changes
        with factory() as session, pytest.raises(ProposalError) as refused:
            expire_in_flight(session, proposal_id, **arguments)
        return refused.value

    # dispatched after the cutoff: still within the bound
    young = attempt(older_than=DISPATCHED_AT - timedelta(seconds=1))
    assert young.code == NOT_EXPIRED
    # exactly at the cutoff is old enough (the bound is inclusive), so a naive-UTC cutoff must also work
    with factory() as session:
        moved = expire_in_flight(
            session,
            proposal_id,
            expected_version=version,
            lease=lease,
            older_than=DISPATCHED_AT.replace(tzinfo=None),
            now=NOW,
        )
        assert moved.state == UNKNOWN
        session.rollback()
    assert _state(factory, proposal_id).state == DISPATCHED

    assert attempt(expected_version=0).code == BAD_FIELD
    stale = attempt(expected_version=version + 5)
    assert isinstance(stale, StaleVersion) and stale.current_version == version
    # a lease that is not the current one (the token moved on) is the abandon rule's, never this one's
    with factory() as session:
        newer = _taken_over(session, lease)
        session.commit()
    assert attempt(lease=newer).code == NOT_EXPIRED
    assert attempt(lease=lease).code == LEASE_LOST, "the old lease is no longer held at all"
    assert attempt(lease=replace(newer, owner_id="someone-else")).code == LEASE_LOST
    assert _state(factory, proposal_id).state == DISPATCHED


def _taken_over(session: Session, stale: Lease, by: str = "reconciler-1") -> Lease:
    assert release(session, stale)
    taken = acquire(session, by, ttl_seconds=TTL_SECONDS)
    assert taken is not None and taken.fencing_token == stale.fencing_token + 1
    return taken


def test_expiry_needs_a_recorded_token_a_dispatched_attempt_and_a_known_proposal(
    database: Any,
) -> None:
    engine, factory = database
    with factory() as session:
        dispatched = _proposal(session)
        settled = _proposal(session, occurrence="occ-2")
        session.commit()
    lease, version = _held(factory, dispatched)
    with factory() as session:
        with pytest.raises(KeyError):
            expire_in_flight(
                session,
                "prop-missing",
                expected_version=1,
                lease=lease,
                older_than=NOW,
                now=NOW,
            )
        session.rollback()
    with factory() as session:  # a proposal that is not dispatched has nothing to expire
        from tests.test_effect_records import _move

        _move(session, settled, UNKNOWN, owner_id=OWNER, attempt=ATTEMPT, reason="timeout")
        session.commit()
        with pytest.raises(ProposalError) as wrong_state:
            expire_in_flight(
                session,
                settled,
                expected_version=proposal_state(session, settled).version,
                lease=lease,
                older_than=NOW,
                now=NOW,
            )
        assert wrong_state.value.code == ILLEGAL_TRANSITION
        session.rollback()
    with engine.begin() as connection:  # a row from before the token was recorded proves nothing
        if engine.dialect.name == "postgresql":
            connection.execute(text("ALTER TABLE caliber_effect_proposals DISABLE TRIGGER USER"))
        else:
            connection.execute(text("DROP TRIGGER trg_caliber_effect_proposals_together"))
        connection.execute(
            text(
                "UPDATE caliber_effect_proposals SET dispatch_token = NULL WHERE proposal_id = :p"
            ),
            {"p": dispatched},
        )
    with factory() as session, pytest.raises(ProposalError) as unproven:
        expire_in_flight(
            session, dispatched, expected_version=version, lease=lease, older_than=NOW, now=NOW
        )
    assert unproven.value.code == NOT_EXPIRED


def test_an_expiry_that_loses_the_update_race_is_stale_and_changes_nothing(
    database: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Another actor's move lands between the read and the UPDATE (inside the same transaction, so it is deterministic on
    both dialects): the UPDATE matches no row and the loser records nothing."""
    from sqlalchemy import update

    _, factory = database
    with factory() as session:
        proposal_id = _proposal(session)
        session.commit()
    lease, version = _held(factory, proposal_id)
    with factory() as session:
        real_execute = session.execute
        fired = {"done": False}

        def racing(statement: Any, *args: Any, **kwargs: Any) -> Any:
            if (
                not fired["done"]
                and "UPDATE" in str(statement).upper()
                and "caliber_effect_proposals" in str(statement)
            ):
                fired["done"] = True
                table = CaliberEffectProposal.__table__
                real_execute(
                    update(table)
                    .where(table.c.proposal_id == proposal_id)
                    .values(state=UNKNOWN, version=version + 1, reason="timeout", unknown_at=AT)
                )
            return real_execute(statement, *args, **kwargs)

        monkeypatch.setattr(session, "execute", racing)
        with pytest.raises(StaleVersion) as stale:
            expire_in_flight(
                session,
                proposal_id,
                expected_version=version,
                lease=lease,
                older_than=NOW,
                now=NOW,
            )
        assert (stale.value.current_version, stale.value.current_state) == (version + 1, UNKNOWN)
        assert fired["done"]


# ---------------------------------------------------------------- reconcile_pass with the in-flight bound


class _Scripted:
    """A lookup that answers what it is told and remembers what it was asked."""

    def __init__(self) -> None:
        self.asked: list[str] = []
        self.answers: dict[str, Answer] = {}

    def __call__(self, connector: Any, base_url: str, key: str) -> Answer:
        self.asked.append(key)
        return self.answers.get(key, Answer(404, {"detail": "not found"}))


def _facts_for(view: Any, **_: Any) -> ReceiptFacts:
    return ReceiptFacts(
        attempt=view.attempt or 1,
        idempotency_key=view.logical_key,
        connector=view.connector,
        connector_version=view.connector_version,
        resource=view.resource,
        intent_hash=view.intent_hash,
        downstream_request_id=f"DR-{view.proposal_id}",
        response_digest="sha256:" + "a" * 64,
    )


@pytest.fixture
def _fact_lookup(monkeypatch: pytest.MonkeyPatch) -> None:
    """The document-request connector, but one that confirms any 200 as this proposal's effect (the pass is under test)."""
    monkeypatch.setitem(
        CONNECTORS,
        "document-request",
        replace(
            CONNECTORS["document-request"],
            facts=lambda view, *, lookup_status, lookup_body: (
                _facts_for(view) if lookup_status == 200 else None
            ),
        ),
    )


def test_the_pass_expires_an_old_same_token_dispatch_and_then_asks_about_it(
    database: Any, _fact_lookup: None
) -> None:
    _, factory = database
    with factory() as session:
        proposal_id = _proposal(session)
        session.commit()
    lease, _ = _held(factory, proposal_id)
    key = proposal_view_key(factory, proposal_id)
    ask = _Scripted()

    without = reconcile_pass(factory, lease=lease, targets=TARGETS, lookup=ask, now=NOW)
    assert without == [], "no bound given: only the dispatch of a lost lease is touched"
    assert _state(factory, proposal_id).state == DISPATCHED

    results = reconcile_pass(
        factory,
        lease=lease,
        targets=TARGETS,
        lookup=ask,
        in_flight_seconds=BOUND,
        now=NOW,
    )
    assert [(r.proposal_id, r.outcome, r.state) for r in results] == [
        (proposal_id, STILL_UNKNOWN, UNKNOWN)
    ], "expired to unknown, asked by key, and a 404 proves nothing: it stays unknown"
    assert ask.asked == [key]
    state = _state(factory, proposal_id)
    assert (state.state, state.reason, state.owner_id) == (UNKNOWN, IN_FLIGHT_EXPIRED_REASON, OWNER)
    expiry = _audits(factory, "effect_in_flight_expired")
    assert len(expiry) == 1
    assert expiry[0]["reason"] == IN_FLIGHT_EXPIRED_REASON
    assert expiry[0]["dispatch_token"] == lease.fencing_token

    # the downstream shows it later (delayed visibility): the next pass commits it, with one receipt and no re-send
    ask.answers[key] = Answer(200, {"found": True})
    results = reconcile_pass(
        factory, lease=lease, targets=TARGETS, lookup=ask, in_flight_seconds=BOUND, now=NOW
    )
    assert [(r.outcome, r.state) for r in results] == [(RECONCILED_COMMITTED, COMMITTED)]
    assert _receipts(factory) == 1


def proposal_view_key(factory: sessionmaker[Session], proposal_id: str) -> str:
    with factory() as session:
        return proposal_view(session, proposal_id).logical_key


def test_a_young_same_token_dispatch_is_left_alone_and_not_asked_about(
    database: Any, _fact_lookup: None
) -> None:
    _, factory = database
    with factory() as session:
        proposal_id = _proposal(session)
        session.commit()
    lease, _ = _held(factory, proposal_id)
    ask = _Scripted()
    # "now" is 10 s after the dispatch and the bound is 900 s
    results = reconcile_pass(
        factory,
        lease=lease,
        targets=TARGETS,
        lookup=ask,
        in_flight_seconds=BOUND,
        now=DISPATCHED_AT + timedelta(seconds=10),
    )
    assert [(r.outcome, r.state) for r in results] == [(IN_FLIGHT, DISPATCHED)]
    assert ask.asked == []
    assert _state(factory, proposal_id).state == DISPATCHED
    assert _audits(factory, "effect_in_flight_expired") == []


def test_the_bound_never_touches_a_dispatch_without_a_token_of_this_lease(
    database: Any, _fact_lookup: None
) -> None:
    """A dispatch of a *lower* token is abandoned (the existing rule, no bound needed) and is not reported as expired."""
    _, factory = database
    with factory() as session:
        proposal_id = _proposal(session)
        old = leased(session, OWNER)
        new = _taken_over(session, old)
        session.commit()
    results = reconcile_pass(
        factory,
        lease=new,
        targets=TARGETS,
        lookup=_Scripted(),
        in_flight_seconds=BOUND,
        now=NOW,
    )
    assert [(r.proposal_id, r.state) for r in results] == [(proposal_id, UNKNOWN)]
    assert _state(factory, proposal_id).reason == "abandoned_dispatch"
    assert _audits(factory, "effect_in_flight_expired") == []


def test_candidates_list_the_current_token_only_when_asked(database: Any) -> None:
    _, factory = database
    with factory() as session:
        proposal_id = _proposal(session)
        lease = leased(session, OWNER)
        assert candidates(session, lease) == []
        assert candidates(session, lease, in_flight=True) == [proposal_id]
        session.commit()


@pytest.mark.parametrize("bad", [0, -1, float("nan"), float("inf"), True, "900"])
def test_the_bound_must_be_a_positive_finite_number(database: Any, bad: Any) -> None:
    _, factory = database
    with factory() as session:
        proposal_id = _proposal(session)
        session.commit()
    lease, _ = _held(factory, proposal_id)
    with pytest.raises(ValueError, match="in_flight_seconds"):
        reconcile_pass(
            factory, lease=lease, targets=TARGETS, lookup=_Scripted(), in_flight_seconds=bad
        )
    with pytest.raises(ValueError, match="in_flight_seconds"):
        reconcile_one(
            factory,
            proposal_id,
            lease=lease,
            targets=TARGETS,
            lookup=_Scripted(),
            in_flight_seconds=bad,
        )
    assert _state(factory, proposal_id).state == DISPATCHED


def test_the_pass_resumes_after_the_last_proposal_it_looked_at(
    database: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A budget that ends a pass early must not make the next pass start over: a backlog of effects that stay unknown would
    otherwise be all it ever looked at."""
    _, factory = database
    with factory() as session:
        ids = sorted(
            _unknown_one(session, f"occ-{n}") for n in range(4)
        )  # proposal ids are time-ordered random; the pass walks them in id order
        session.commit()
    lease = _unknown_lease(factory)
    clock = [0.0]
    monkeypatch.setattr(reconcile, "time", SimpleNamespace(monotonic=lambda: clock[0]))

    def slow(connector: Any, base_url: str, key: str) -> Answer:
        clock[0] += 1.0  # every lookup costs one second
        return Answer(404, {})

    first = reconcile_pass_from(
        factory, lease=lease, targets=TARGETS, lookup=slow, budget_seconds=1.5, now=NOW
    )
    assert isinstance(first, PassResult)
    assert [r.proposal_id for r in first.results] == ids[:2]
    assert first.resume_after == ids[1]
    second = reconcile_pass_from(
        factory,
        lease=lease,
        targets=TARGETS,
        lookup=slow,
        budget_seconds=1.5,
        after=first.resume_after,
        now=NOW,
    )
    assert [r.proposal_id for r in second.results] == ids[2:]
    assert second.resume_after is None, "nothing is left: the next pass starts from the top"
    # a budget that is already spent before the first proposal looks at nothing and keeps the cursor it was given
    spent = reconcile_pass_from(
        factory,
        lease=lease,
        targets=TARGETS,
        lookup=slow,
        budget_seconds=0,
        after=ids[0],
        now=NOW,
    )
    assert spent.results == [] and spent.resume_after == ids[0]


def _unknown_one(session: Session, occurrence: str) -> str:
    from tests.test_effect_records import _move

    proposal_id = _proposal(session, occurrence=occurrence)
    _move(session, proposal_id, UNKNOWN, owner_id=OWNER, attempt=ATTEMPT, reason="timeout")
    return proposal_id


def _unknown_lease(factory: sessionmaker[Session]) -> Lease:
    with factory() as session:
        lease = leased(session, OWNER)
        session.commit()
    return lease


def test_a_proposal_that_raises_is_reported_and_the_pass_goes_on_to_the_next(
    database: Any, _fact_lookup: None
) -> None:
    _, factory = database
    with factory() as session:
        first = _unknown_one(session, "occ-bad")
        second = _unknown_one(session, "occ-good")
        session.commit()
    bad, good = sorted((first, second))
    lease = _unknown_lease(factory)
    key_bad = proposal_view_key(factory, bad)
    ask = _Scripted()

    def lookup(connector: Any, base_url: str, key: str) -> Answer:
        if key == key_bad:
            raise RuntimeError("PAYLOAD-MARKER-FIVE")
        return ask(connector, base_url, key)

    ask.answers[proposal_view_key(factory, good)] = Answer(200, {"found": True})
    results = reconcile_pass(factory, lease=lease, targets=TARGETS, lookup=lookup, now=NOW)
    assert [(r.proposal_id, r.outcome, r.detail) for r in results][0] == (
        bad,
        STILL_UNKNOWN,
        "RuntimeError",
    )
    assert (results[1].proposal_id, results[1].outcome) == (good, RECONCILED_COMMITTED)
    # reconcile_one itself still raises: only the pass isolates one proposal from the next
    with pytest.raises(RuntimeError, match="PAYLOAD-MARKER-FIVE"):
        reconcile_one(factory, bad, lease=lease, targets=TARGETS, lookup=lookup, now=NOW)


def test_a_pass_that_is_stopped_by_its_progress_hook_looks_at_nothing_more(
    database: Any,
) -> None:
    _, factory = database
    with factory() as session:
        ids = sorted(_unknown_one(session, f"occ-{n}") for n in range(3))
        session.commit()
    lease = _unknown_lease(factory)
    ask = _Scripted()
    calls = {"n": 0}

    class _StopError(Exception):
        pass

    def progress() -> None:
        calls["n"] += 1
        if calls["n"] == 2:
            raise _StopError

    with pytest.raises(_StopError):
        reconcile_pass(
            factory, lease=lease, targets=TARGETS, lookup=ask, on_progress=progress, now=NOW
        )
    assert len(ask.asked) == 1, "only the first proposal was asked about"
    assert {_state(factory, proposal_id).state for proposal_id in ids} == {UNKNOWN}


# ---------------------------------------------------------------- the dispatcher records unknown for a failed settle


def _submit_one(session: Session, occurrence: str) -> tuple[str, str]:
    recorded = submit_proposal(
        session,
        workflow_run_id="RUN-ep",
        node_id="agent",
        occurrence_id=occurrence,
        action={
            "connector": "document-request",
            "connector_version": "1",
            "effect_class": "read",
            "resource": "test/resource",
            "args": {"value": occurrence},
        },
        tool_name="write_workdir_file",
        caliber_defined=True,
    )
    proposal = session.get(CaliberEffectProposal, recorded.proposal_id)
    assert proposal is not None
    approval_id = f"rap-{occurrence}"
    session.add(
        CaliberRuntimeApprovalRequest(
            runtime_approval_id=approval_id,
            workflow_run_id=proposal.workflow_run_id,
            project_id=PROJECT,
            node_id=proposal.node_id,
            status="approved",
            requested_at=DISPATCHED_AT.replace(tzinfo=None),
            decided_at=DISPATCHED_AT.replace(tzinfo=None),
            decided_by="approver",
            policy_snapshot={"required_role": "caliber.approver", "approval_count": 1},
            proposal_id=recorded.proposal_id,
            intent_hash=proposal.intent_hash,
            evidence_hash=EVIDENCE,
            policy_digest=POLICY,
            expected_resource_version=proposal.expected_resource_version,
            expires_at=(DISPATCHED_AT + timedelta(hours=1)).replace(tzinfo=None),
            requested_by="editor",
        )
    )
    session.commit()
    return recorded.proposal_id, approval_id


class _Sender:
    """A backend that "sends" (counts) and hands back a committed claim with a verifier that does what the test says."""

    def __init__(self, verify: Any = None) -> None:
        self.sends = 0
        self.verify = verify

    def perform(self, request: Any, _config: Any) -> TransportResult:
        self.sends += 1
        view = request.view
        facts = ReceiptFacts(
            attempt=view.attempt or 1,
            idempotency_key=view.logical_key,
            connector=view.connector,
            connector_version=view.connector_version,
            resource=view.resource,
            intent_hash=view.intent_hash,
            downstream_request_id=f"DR-{view.proposal_id}",
            response_digest="sha256:" + "a" * 64,
        )

        def verify() -> ReceiptFacts | None:
            if self.verify is not None:
                self.verify()
            return facts

        return TransportResult(
            Classified(TransportOutcome.COMMITTED_RECEIPT, "http_201", 201, 1), verify
        )


def _dispatch(
    factory: Any,
    proposal_id: str,
    approval_id: str,
    lease: Lease,
    sender: Any,
    monkeypatch: pytest.MonkeyPatch,
    kind: str = "in-flight",
    now: datetime | None = DISPATCHED_AT,
) -> Any:
    monkeypatch.setitem(BACKENDS, kind, BackendRegistration(sender, "document-request", "1"))
    return dispatch(
        factory,
        proposal_id,
        approval_id=approval_id,
        approver_scopes={"caliber.approver"},
        lease=lease,
        backend_kind=kind,
        approval_validator=_validator,
        now=now,
    )


def test_a_verifier_that_raises_leaves_unknown_not_dispatched_and_the_reconciler_finishes_it(
    database: Any, monkeypatch: pytest.MonkeyPatch, _fact_lookup: None
) -> None:
    _, factory = database
    with factory() as session:
        proposal_id, approval_id = _submit_one(session, "occ-v")
        lease = leased(session, "dispatcher")
        session.commit()

    def broken() -> None:
        raise RuntimeError("the downstream query failed: PAYLOAD-MARKER-ONE")

    sender = _Sender(verify=broken)
    with pytest.raises(RuntimeError, match="downstream query failed"):
        _dispatch(factory, proposal_id, approval_id, lease, sender, monkeypatch)

    state = _state(factory, proposal_id)
    assert (state.state, state.reason, state.owner_id, state.attempt) == (
        UNKNOWN,
        "settle_failed:RuntimeError",
        "dispatcher",
        1,
    ), "the exception class is recorded, never its message"
    assert "PAYLOAD-MARKER-ONE" not in (state.reason or "")
    assert sender.sends == 1 and _receipts(factory) == 0

    # the same owner's reconciler asks by key and settles it: the effect was sent once and is never sent again
    ask = _Scripted()
    ask.answers[proposal_view_key(factory, proposal_id)] = Answer(200, {"found": True})
    results = reconcile_pass(
        factory, lease=lease, targets=TARGETS, lookup=ask, in_flight_seconds=BOUND, now=NOW
    )
    assert [(r.outcome, r.state) for r in results] == [(RECONCILED_COMMITTED, COMMITTED)]
    assert sender.sends == 1 and _receipts(factory) == 1


@pytest.mark.parametrize("code", ["store_failed", "receipt_conflict", "settlement_corrupt"])
def test_a_refused_settle_that_is_not_someone_elses_records_unknown(
    database: Any, monkeypatch: pytest.MonkeyPatch, code: str
) -> None:
    _, factory = database
    with factory() as session:
        proposal_id, approval_id = _submit_one(session, "occ-c")
        lease = leased(session, "dispatcher")
        session.commit()

    def refuse(*_args: Any, **_kwargs: Any) -> Any:
        raise ProposalError(code, "scripted")

    monkeypatch.setattr(dispatcher_module, "settle_dispatch", refuse)
    with pytest.raises(EffectRefused) as refused:
        _dispatch(factory, proposal_id, approval_id, lease, _Sender(), monkeypatch)
    assert refused.value.code == code
    state = _state(factory, proposal_id)
    assert (state.state, state.reason) == (UNKNOWN, f"settle_failed:{code}")


@pytest.mark.parametrize(
    "code", ["lease_lost", "stale_owner", "stale_version", "illegal_transition"]
)
def test_a_settle_refused_because_someone_else_owns_the_row_writes_nothing(
    database: Any, monkeypatch: pytest.MonkeyPatch, code: str
) -> None:
    _, factory = database
    with factory() as session:
        proposal_id, approval_id = _submit_one(session, "occ-n")
        lease = leased(session, "dispatcher")
        session.commit()

    def refuse(*_args: Any, **_kwargs: Any) -> Any:
        raise ProposalError(code, "scripted")

    monkeypatch.setattr(dispatcher_module, "settle_dispatch", refuse)
    with pytest.raises(EffectRefused) as refused:
        _dispatch(factory, proposal_id, approval_id, lease, _Sender(), monkeypatch)
    assert refused.value.code == code
    assert _state(factory, proposal_id).state == DISPATCHED, (
        "the new owner's reconciler decides, not this dispatcher"
    )


def test_when_unknown_cannot_be_recorded_either_the_original_failure_surfaces_and_the_bound_takes_over(
    database: Any, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    _, factory = database
    with factory() as session:
        proposal_id, approval_id = _submit_one(session, "occ-d")
        lease = leased(session, "dispatcher")
        session.commit()
    real_transition = dispatcher_module.transition

    def store_down(session: Any, pid: str, to_state: str, **kwargs: Any) -> Any:
        if to_state == UNKNOWN:
            raise OperationalError("UPDATE", {}, Exception("the store is down: PAYLOAD-MARKER-TWO"))
        return real_transition(session, pid, to_state, **kwargs)

    def broken() -> None:
        raise ConnectionError("verifier unreachable")

    monkeypatch.setattr(dispatcher_module, "transition", store_down)
    with caplog.at_level(logging.WARNING, logger="caliber.effects.dispatcher"):
        with pytest.raises(ConnectionError, match="verifier unreachable"):
            _dispatch(factory, proposal_id, approval_id, lease, _Sender(verify=broken), monkeypatch)
    assert _state(factory, proposal_id).state == DISPATCHED
    text_logged = " ".join(record.getMessage() for record in caplog.records)
    assert proposal_id in text_logged and "OperationalError" in text_logged
    assert "PAYLOAD-MARKER-TWO" not in text_logged, "no payload or driver detail reaches the log"

    # nobody settled it and nobody ever will: the owner's loop moves it once it is old enough
    results = reconcile_pass(
        factory,
        lease=lease,
        targets=TARGETS,
        lookup=_Scripted(),
        in_flight_seconds=BOUND,
        now=NOW,
    )
    assert [(r.state, r.outcome) for r in results] == [(UNKNOWN, STILL_UNKNOWN)]
    assert _state(factory, proposal_id).reason == IN_FLIGHT_EXPIRED_REASON


def test_a_dispatcher_that_lost_its_lease_while_settling_records_nothing_and_the_new_owner_abandons_it(
    database: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, factory = database
    with factory() as session:
        proposal_id, approval_id = _submit_one(session, "occ-l")
        lease = leased(session, "dispatcher")
        session.commit()
    holder: dict[str, Lease] = {}

    def takeover_then_fail() -> None:
        with factory() as session:  # the lease lapses and another process acquires it mid-settle
            assert release(session, lease)
            taken = acquire(session, "reconciler-1", ttl_seconds=TTL_SECONDS)
            assert taken is not None
            holder["new"] = taken
            session.commit()
        raise RuntimeError("verifier failed after the takeover")

    with pytest.raises(RuntimeError, match="after the takeover"):
        _dispatch(
            factory,
            proposal_id,
            approval_id,
            lease,
            _Sender(verify=takeover_then_fail),
            monkeypatch,
        )
    assert _state(factory, proposal_id).state == DISPATCHED, (
        "a stale dispatcher cannot even mark unknown"
    )
    results = reconcile_pass(
        factory, lease=holder["new"], targets=TARGETS, lookup=_Scripted(), now=NOW
    )
    assert [r.proposal_id for r in results] == [proposal_id]
    assert _state(factory, proposal_id).reason == "abandoned_dispatch"


def test_a_settle_failure_is_recorded_when_the_caller_passed_a_session_not_a_factory(
    database: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, factory = database
    with factory() as session:
        proposal_id, approval_id = _submit_one(session, "occ-s")
        lease = leased(session, "dispatcher")
        session.commit()

    def broken() -> None:
        raise RuntimeError("verifier failed")

    monkeypatch.setitem(
        BACKENDS,
        "session-source",
        BackendRegistration(_Sender(verify=broken), "document-request", "1"),
    )
    with factory() as session, pytest.raises(RuntimeError, match="verifier failed"):
        dispatch(
            session,
            proposal_id,
            approval_id=approval_id,
            approver_scopes={"caliber.approver"},
            lease=lease,
            backend_kind="session-source",
            approval_validator=_validator,
            now=DISPATCHED_AT,
        )
    assert _state(factory, proposal_id).reason == "settle_failed:RuntimeError"


# ---------------------------------------------------------------- the live dispatcher and the loop racing for one row


def test_the_loop_expiring_a_row_while_its_dispatcher_verifies_wins_once_and_records_no_receipt(
    database: Any, monkeypatch: pytest.MonkeyPatch, _fact_lookup: None
) -> None:
    """Deterministic: the dispatcher has sent and is verifying (no lock held) when the loop's bound moves the row. The
    dispatcher's commit then finds the row moved and records nothing, not even a receipt; the effect was sent once."""
    _, factory = database
    with factory() as session:
        proposal_id, approval_id = _submit_one(session, "occ-r")
        lease = leased(session, "dispatcher")
        session.commit()
    moved: list[Any] = []

    def loop_runs_now() -> None:
        moved.extend(
            reconcile_pass(
                factory,
                lease=lease,
                targets=TARGETS,
                lookup=_Scripted(),
                in_flight_seconds=BOUND,
                now=NOW,
            )
        )

    sender = _Sender(verify=loop_runs_now)
    with pytest.raises(EffectRefused) as refused:
        _dispatch(factory, proposal_id, approval_id, lease, sender, monkeypatch)
    assert refused.value.code == "stale_version"
    assert [(r.proposal_id, r.state) for r in moved] == [(proposal_id, UNKNOWN)]
    state = _state(factory, proposal_id)
    assert (state.state, state.reason) == (UNKNOWN, IN_FLIGHT_EXPIRED_REASON)
    assert sender.sends == 1 and _receipts(factory) == 0

    # the outcome is not lost: the row is unknown, so a lookup by key settles it later, still without a second send
    ask = _Scripted()
    ask.answers[proposal_view_key(factory, proposal_id)] = Answer(200, {"found": True})
    results = reconcile_pass(
        factory, lease=lease, targets=TARGETS, lookup=ask, in_flight_seconds=BOUND, now=NOW
    )
    assert [(r.outcome, r.state) for r in results] == [(RECONCILED_COMMITTED, COMMITTED)]
    assert sender.sends == 1 and _receipts(factory) == 1


def test_the_dispatcher_settling_first_leaves_the_loop_nothing_to_move(
    database: Any, monkeypatch: pytest.MonkeyPatch, _fact_lookup: None
) -> None:
    _, factory = database
    with factory() as session:
        proposal_id, approval_id = _submit_one(session, "occ-w")
        lease = leased(session, "dispatcher")
        session.commit()
    sender = _Sender()
    outcome = _dispatch(factory, proposal_id, approval_id, lease, sender, monkeypatch)
    assert outcome.state == COMMITTED
    asked = _Scripted()
    results = reconcile_pass(
        factory,
        lease=lease,
        targets=TARGETS,
        lookup=asked,
        in_flight_seconds=BOUND,
        now=NOW,
    )
    assert results == [] and asked.asked == []
    assert sender.sends == 1 and _receipts(factory) == 1
    # asking about it explicitly is "moved", never a second look at the downstream
    again = reconcile_one(
        factory,
        proposal_id,
        lease=lease,
        targets=TARGETS,
        lookup=asked,
        in_flight_seconds=BOUND,
        now=NOW,
    )
    assert (again.outcome, again.state) == (MOVED, COMMITTED) and asked.asked == []


def _race_round(
    factory: Any, lease: Lease, n: int, monkeypatch: pytest.MonkeyPatch
) -> tuple[str, _Sender, dict[str, Any]]:
    """One proposal: its dispatcher (blocked in the verifier) and the loop's expiry are released together."""
    with factory() as session:
        proposal_id, approval_id = _submit_one(session, f"occ-race-{n}")
    gate = threading.Barrier(2)
    errors: list[BaseException] = []
    result: dict[str, Any] = {}
    sender = _Sender(verify=lambda: gate.wait(20))

    def run_dispatcher() -> None:
        try:
            result["dispatched"] = _dispatch(
                factory, proposal_id, approval_id, lease, sender, monkeypatch
            )
        except EffectRefused as exc:
            result["refused"] = exc.code
        except BaseException as exc:  # pragma: no cover - reported below
            errors.append(exc)

    def run_loop() -> None:
        try:
            gate.wait(20)
            result["loop"] = reconcile_one(
                factory,
                proposal_id,
                lease=lease,
                targets=TARGETS,
                lookup=_Scripted(),
                in_flight_seconds=BOUND,
                now=NOW,
            )
        except BaseException as exc:  # pragma: no cover - reported below
            errors.append(exc)

    threads = [
        threading.Thread(target=run_dispatcher, daemon=True),
        threading.Thread(target=run_loop, daemon=True),
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(60)
    assert not errors, errors
    return proposal_id, sender, result


def test_dispatcher_and_loop_racing_on_postgresql_resolve_to_exactly_one_outcome(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Both are released together, many times over: whoever reaches the row first wins, the other records nothing, the effect
    is sent once per proposal and every proposal ends committed with one receipt, or unknown with none."""
    if not POSTGRES_TEST_URL:
        pytest.skip("the race needs PostgreSQL")
    rounds = 12
    with _database("postgresql", tmp_path, monkeypatch) as (_, factory):
        _seed_run(factory)
        with factory() as session:
            lease = leased(session, "dispatcher")
            session.commit()
        committed = unknown = 0
        for n in range(rounds):
            proposal_id, sender, result = _race_round(factory, lease, n, monkeypatch)
            assert sender.sends == 1, "never a double send"
            state = _state(factory, proposal_id)
            if "dispatched" in result:
                committed += 1
                assert result["dispatched"].state == COMMITTED and state.state == COMMITTED
                assert result["loop"].outcome == MOVED, "the loop found nothing to move"
            else:
                unknown += 1
                assert result["refused"] == "stale_version"
                assert (state.state, state.reason) == (UNKNOWN, IN_FLIGHT_EXPIRED_REASON)
            with factory() as session:
                receipts = session.execute(
                    select(CaliberEffectReceipt.receipt_id).where(
                        CaliberEffectReceipt.proposal_id == proposal_id
                    )
                ).all()
            assert len(receipts) == (1 if state.state == COMMITTED else 0), (
                "no lost, no half outcome"
            )
        assert committed + unknown == rounds


def test_a_settle_whose_commit_landed_before_the_failure_is_never_overwritten_with_unknown(
    database: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A commit that raised after the database applied it (a dropped connection) is indistinguishable from one that failed:
    the dispatcher looks at the row first and records nothing over a settled one."""
    _, factory = database
    with factory() as session:
        proposal_id, approval_id = _submit_one(session, "occ-landed")
        lease = leased(session, "dispatcher")
        session.commit()
    real = dispatcher_module.settle_dispatch

    def settle_commit_then_fail(session: Any, *args: Any, **kwargs: Any) -> Any:
        settled = real(session, *args, **kwargs)
        session.commit()  # the outcome is durable ...
        raise RuntimeError(
            "the connection dropped while acknowledging"
        )  # ... and the caller never learns it

    monkeypatch.setattr(dispatcher_module, "settle_dispatch", settle_commit_then_fail)
    with pytest.raises(RuntimeError, match="connection dropped"):
        _dispatch(factory, proposal_id, approval_id, lease, _Sender(), monkeypatch)
    assert _state(factory, proposal_id).state == COMMITTED
    assert _receipts(factory) == 1


def test_the_in_flight_cutoff_accepts_a_naive_clock_as_utc(database: Any) -> None:
    _, factory = database
    with factory() as session:
        proposal_id = _proposal(session)
        session.commit()
    lease, _ = _held(factory, proposal_id)
    results = reconcile_pass(
        factory,
        lease=lease,
        targets=TARGETS,
        lookup=_Scripted(),
        in_flight_seconds=BOUND,
        now=NOW.replace(tzinfo=None),
    )
    assert [(r.proposal_id, r.state) for r in results] == [(proposal_id, UNKNOWN)]
