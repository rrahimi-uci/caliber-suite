"""The owner-only reconciler (B05.5b, #742): unknown and abandoned effects are settled by a lookup by key, never re-sent."""

from __future__ import annotations

import itertools
import threading
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import select, text, update
from sqlalchemy.orm import Session, sessionmaker

from caliber.db.models import (
    CaliberAuditLog,
    CaliberEffectProposal,
    CaliberEffectReceipt,
    CaliberWorkflowRun,
    CaliberWorkflowRunCheckpoint,
)
from caliber.effects import reconcile
from caliber.effects.callbacks import CallbackBinding
from caliber.effects.connectors import ConnectorCapability
from caliber.effects.reconcile import (
    ABSENCE_PROVEN_REASON,
    CONNECTORS,
    IN_FLIGHT,
    LOOKUP_FAILED,
    MOVED,
    NOT_BOUND,
    RECONCILED_COMMITTED,
    RECONCILED_FAILED,
    STILL_UNKNOWN,
    UNVERIFIED,
    Answer,
    UnknownEffectError,
    candidates,
    commit_found,
    effect_gate,
    reconcile_one,
    reconcile_pass,
    targets_from_bindings,
    unresolved_effects,
    wake_parked_runs,
)
from caliber.effects.records import (
    ABANDONED_REASON,
    APPROVED,
    COMMITTED,
    DISPATCHED,
    EVIDENCE_REQUIRED,
    FAILED,
    ILLEGAL_TRANSITION,
    LEASE_LOST,
    NOT_ABANDONED,
    RECONCILED,
    UNKNOWN,
    ProposalError,
    StaleVersion,
    abandon_dispatch,
    proposal_state,
    transition,
)
from caliber.integrations.openapi.executor import OpenApiExecutionError
from caliber.ownership import Lease, acquire, release
from tests.dialect_helpers import DIALECTS, POSTGRES_TEST_URL
from tests.effect_helpers import TTL_SECONDS, leased
from tests.test_effect_proposals import _seed_run
from tests.test_effect_receipts import CASE, DOC, _proposal
from tests.test_effect_records import AT, ATTEMPT, OWNER, _move
from tests.test_event_outbox import _count, _database

RECONCILER = "reconciler-1"
PROJECT = "PRJ-ep"
BASE_URL = "http://downstream.invalid"
TARGETS = {("document-request", PROJECT): BASE_URL}


def _stored(session: Session, proposal_id: str, **overrides: Any) -> dict[str, Any]:
    """What the downstream's lookup answers when it holds this proposal's request."""
    from caliber.effects.records import proposal_view

    view = proposal_view(session, proposal_id)
    body: dict[str, Any] = {
        "request_id": f"DR-{proposal_id}",
        "idempotency_key": view.logical_key,
        "case_id": CASE,
        "document_type": DOC,
        "case_version": 2,
    }
    body.update(overrides)
    return body


class _Downstream:
    """A scripted downstream: what a lookup by key answers, and how often it was asked."""

    def __init__(self, session_factory: sessionmaker[Session]) -> None:
        self._factory = session_factory
        self.asked: list[str] = []
        self.script: dict[str, Any] = {}
        self.default: Any = Answer(404, {"detail": "not found"})

    def __call__(self, connector: Any, base_url: str, key: str) -> Answer:
        assert base_url == BASE_URL
        self.asked.append(key)
        answer = self.script.get(key, self.default)
        if callable(answer):
            answer = answer()
        if isinstance(answer, Exception):
            raise answer
        return answer  # type: ignore[no-any-return]


@pytest.fixture(params=DIALECTS)
def database(
    request: pytest.FixtureRequest, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Any:
    with _database(request.param, tmp_path, monkeypatch) as (engine, factory):
        _seed_run(factory)
        yield engine, factory


def _unknown(session: Session, occurrence: str) -> str:
    proposal_id = _proposal(session, occurrence=occurrence)
    _move(session, proposal_id, UNKNOWN, owner_id=OWNER, attempt=ATTEMPT, reason="timeout")
    return proposal_id


def _owner(factory: sessionmaker[Session], owner: str = RECONCILER) -> Lease:
    """The lease the reconciler holds: it took the lease over, so everything dispatched before is abandoned."""
    with factory() as session:
        lease = leased(session, owner)
        session.commit()
    return lease


def _state(factory: sessionmaker[Session], proposal_id: str) -> Any:
    with factory() as session:
        return proposal_state(session, proposal_id)


def _park_run(factory: sessionmaker[Session], proposal_id: str, *, node_id: str = "final") -> None:
    """Persist the same checkpoint shape the queue worker writes for a parked run."""
    from caliber.effects.records import proposal_view

    with factory() as session:
        run = session.get(CaliberWorkflowRun, "RUN-ep")
        assert run is not None
        view = proposal_view(session, proposal_id)
        checkpoint_id = "checkpoint-effect-1"
        run.status = "waiting_effect"
        run.current_node_id = node_id
        run.error_code = "effect_reconciliation"
        run.error_summary = "waiting for authoritative effect reconciliation"
        run.summary = {
            "status": "waiting_effect",
            "resume_checkpoint_id": checkpoint_id,
            "resume_checkpoint_run_id": run.workflow_run_id,
            "parked_effects": {
                "state": "waiting_effect",
                "node_id": node_id,
                "reason": "effect_dependency_unresolved",
                "resume_policy": "same_run_same_occurrence",
                "effects": [
                    {
                        "proposal_id": view.proposal_id,
                        "node_id": "ask",
                        "state": view.state,
                        "logical_key": view.logical_key,
                        "intent_hash": view.intent_hash,
                    }
                ],
            },
        }
        session.add(
            CaliberWorkflowRunCheckpoint(
                checkpoint_id=checkpoint_id,
                workflow_run_id=run.workflow_run_id,
                project_id=run.project_id,
                sequence=1,
                node_id=node_id,
                state_blob={
                    "kind": "effect_reconciliation",
                    "node_id": node_id,
                    "input_by_port": {"input": "hello"},
                    "effects": [
                        {
                            "proposal_id": view.proposal_id,
                            "node_id": "ask",
                            "state": view.state,
                            "logical_key": view.logical_key,
                            "intent_hash": view.intent_hash,
                        }
                    ],
                    "reason": "effect_dependency_unresolved",
                    "resume_policy": "same_run_same_occurrence",
                },
            )
        )
        session.commit()


def test_reconciliation_wakes_a_parked_run_after_authoritative_commit(database: Any) -> None:
    _, factory = database
    with factory() as session:
        proposal_id = _unknown(session, "o-parked")
        view = reconcile.proposal_view(session, proposal_id)
        session.commit()
    _park_run(factory, proposal_id)
    downstream = _Downstream(factory)
    with factory() as session:
        downstream.script[view.logical_key] = Answer(200, _stored(session, proposal_id))
        session.commit()

    result = reconcile_one(
        factory,
        proposal_id,
        lease=_owner(factory),
        targets=TARGETS,
        lookup=downstream,
        now=AT,
    )
    assert (result.outcome, result.state) == (RECONCILED_COMMITTED, COMMITTED)
    with factory() as session:
        run = session.get(CaliberWorkflowRun, "RUN-ep")
        assert run is not None
        assert run.status == "queued"
        assert run.summary["parked_effects"]["state"] == "queued"
        assert run.summary["parked_effects"]["wake_reason"] == "all_effect_dependencies_settled"
        checkpoint_id = run.summary["resume_checkpoint_id"]
        checkpoint = session.get(CaliberWorkflowRunCheckpoint, checkpoint_id)
        assert checkpoint is not None and checkpoint.state_blob["kind"] == "effect_reconciliation"


def test_parked_run_wakes_after_failed_settlement_and_duplicate_wake_is_noop(database: Any) -> None:
    _, factory = database
    with factory() as session:
        proposal_id = _unknown(session, "o-parked-failed")
        _move(session, proposal_id, RECONCILED, reason="absence proven")
        _move(session, proposal_id, FAILED, reason=ABSENCE_PROVEN_REASON)
        session.commit()
    _park_run(factory, proposal_id)
    assert wake_parked_runs(factory, proposal_id, settled_state=FAILED) == ["RUN-ep"]
    assert wake_parked_runs(factory, proposal_id, settled_state=FAILED) == []
    with factory() as session:
        run = session.get(CaliberWorkflowRun, "RUN-ep")
        assert run is not None and run.status == "queued"


# ---------------------------------------------------------------- abandoned dispatches


def test_a_dispatch_records_the_token_it_was_made_under(database: Any) -> None:
    _, factory = database
    with factory() as session:
        proposal_id = _proposal(session)
        held = leased(session, OWNER)
        assert proposal_state(session, proposal_id).dispatch_token == held.fencing_token
        session.commit()


def test_a_dispatch_under_another_token_is_not_a_repeat(database: Any) -> None:
    """A restarted owner re-dispatching what its predecessor dispatched would be a blind second send: it is refused."""
    _, factory = database
    with factory() as session:
        proposal_id = _proposal(session)
        version = proposal_state(session, proposal_id).version
        new = _taken_over(
            session, leased(session, OWNER), OWNER
        )  # the same stable owner id, a new acquisition
        with pytest.raises(StaleVersion):
            transition(
                session,
                proposal_id,
                DISPATCHED,
                expected_version=version - 1,
                owner_id=OWNER,
                attempt=ATTEMPT,
                lease=new,
            )
        assert proposal_state(session, proposal_id).version == version
        session.commit()


def _taken_over(session: Session, stale: Lease, by: str) -> Lease:
    assert release(session, stale)
    taken = acquire(session, by, ttl_seconds=TTL_SECONDS)
    assert taken is not None and taken.fencing_token == stale.fencing_token + 1
    return taken


def test_a_dispatch_whose_lease_was_taken_over_is_abandoned_to_unknown(database: Any) -> None:
    _, factory = database
    with factory() as session:
        proposal_id = _proposal(session)
        old = leased(session, OWNER)
        new = _taken_over(session, old, RECONCILER)
        before = proposal_state(session, proposal_id)
        moved = abandon_dispatch(
            session, proposal_id, expected_version=before.version, lease=new, now=AT
        )
        after = proposal_state(session, proposal_id)
        assert (moved.applied, moved.state, moved.version) == (True, UNKNOWN, before.version + 1)
        assert (after.state, after.reason, after.owner_id, after.attempt) == (
            UNKNOWN,
            ABANDONED_REASON,
            OWNER,
            ATTEMPT,
        )
        assert after.dispatch_token == old.fencing_token, "the evidence of the dispatch is kept"
        assert after.unknown_at is not None
        with pytest.raises(StaleVersion):  # a second reconciler reading the old version
            abandon_dispatch(session, proposal_id, expected_version=before.version, lease=new)
        session.commit()


def test_an_abandon_that_loses_the_update_race_is_stale_and_changes_nothing(
    database: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Another actor's move lands between this one's read and its UPDATE (arranged inside the same transaction so it is
    deterministic on both dialects): the UPDATE matches no row."""
    from caliber.db.models import CaliberEffectProposal as Model

    _, factory = database
    with factory() as session:
        proposal_id = _proposal(session)
        new = _taken_over(session, leased(session, OWNER), RECONCILER)
        version = proposal_state(session, proposal_id).version
        session.commit()
        real_execute = session.execute
        fired = {"done": False}

        def racing(statement: Any, *args: Any, **kwargs: Any) -> Any:
            if (
                not fired["done"]
                and "UPDATE" in str(statement).upper()
                and "caliber_effect_proposals" in str(statement)
            ):
                fired["done"] = True
                table = Model.__table__
                real_execute(
                    update(table)
                    .where(table.c.proposal_id == proposal_id)
                    .values(state=UNKNOWN, version=version + 1, reason="timeout", unknown_at=AT)
                )
            return real_execute(statement, *args, **kwargs)

        monkeypatch.setattr(session, "execute", racing)
        with pytest.raises(StaleVersion) as stale:
            abandon_dispatch(session, proposal_id, expected_version=version, lease=new)
        assert (stale.value.current_version, stale.value.current_state) == (version + 1, UNKNOWN)
        assert fired["done"]


def test_an_attempt_dispatched_under_the_current_lease_is_not_abandoned(database: Any) -> None:
    _, factory = database
    with factory() as session:
        proposal_id = _proposal(session)
        held = leased(session, OWNER)
        version = proposal_state(session, proposal_id).version
        with pytest.raises(ProposalError) as in_flight:
            abandon_dispatch(session, proposal_id, expected_version=version, lease=held)
        assert in_flight.value.code == NOT_ABANDONED
        assert proposal_state(session, proposal_id).state == DISPATCHED
        session.commit()


def test_an_attempt_with_no_recorded_token_proves_nothing_and_is_never_abandoned(
    database: Any,
) -> None:
    engine, factory = database
    with factory() as session:
        proposal_id = _proposal(session)
        new = _taken_over(session, leased(session, OWNER), RECONCILER)
        session.commit()
    with (
        engine.begin() as connection
    ):  # a row from before the token was recorded (the guard is the new rule, not theirs)
        if engine.dialect.name == "postgresql":
            connection.execute(text("ALTER TABLE caliber_effect_proposals DISABLE TRIGGER USER"))
        else:
            connection.execute(text("DROP TRIGGER trg_caliber_effect_proposals_together"))
        connection.execute(
            text(
                "UPDATE caliber_effect_proposals SET dispatch_token = NULL WHERE proposal_id = :p"
            ),
            {"p": proposal_id},
        )
    with factory() as session:
        version = proposal_state(session, proposal_id).version
        with pytest.raises(ProposalError) as unproven:
            abandon_dispatch(session, proposal_id, expected_version=version, lease=new)
        assert unproven.value.code == NOT_ABANDONED
        assert proposal_id not in candidates(session, new)


def test_only_the_holder_of_the_execution_owner_lease_abandons_anything(database: Any) -> None:
    _, factory = database
    with factory() as session:
        proposal_id = _proposal(session)
        unknown = _unknown(session, "o-2")
        old = leased(session, OWNER)
        new = _taken_over(session, old, RECONCILER)
        version = proposal_state(session, proposal_id).version
        for label, lease, code in (
            ("the dead owner's", old, LEASE_LOST),
            ("a future token", replace(new, fencing_token=new.fencing_token + 1), LEASE_LOST),
            ("another owner's", replace(new, owner_id="someone-else"), LEASE_LOST),
            ("another lease name", replace(new, name="not-the-owner-lease"), LEASE_LOST),
        ):
            with pytest.raises(ProposalError) as refused:
                abandon_dispatch(session, proposal_id, expected_version=version, lease=lease)
            assert refused.value.code == code, label
        assert proposal_state(session, proposal_id).state == DISPATCHED
        with pytest.raises(ProposalError) as bad:
            abandon_dispatch(session, proposal_id, expected_version=0, lease=new)
        assert bad.value.code == "bad_field"
        with pytest.raises(KeyError):
            abandon_dispatch(session, "prop-missing", expected_version=1, lease=new)
        with pytest.raises(ProposalError) as wrong_state:
            abandon_dispatch(
                session,
                unknown,
                expected_version=proposal_state(session, unknown).version,
                lease=new,
            )
        assert wrong_state.value.code == ILLEGAL_TRANSITION
        session.commit()


# ---------------------------------------------------------------- candidates


def test_candidates_are_unknown_reconciled_and_abandoned_dispatches_oldest_first(
    database: Any,
) -> None:
    _, factory = database
    with factory() as session:
        abandoned = _proposal(session, occurrence="o-abandoned")
        unknown = _unknown(session, "o-unknown")
        reconciled = _unknown(session, "o-reconciled")
        _move(session, reconciled, RECONCILED, reason="found by key")
        committed = _proposal(session, occurrence="o-committed")
        from caliber.effects.receipts import record_receipt
        from tests.test_effect_receipts import _facts

        record_receipt(session, committed, _facts(session, committed), now=AT)
        _move(
            session,
            committed,
            COMMITTED,
            owner_id=OWNER,
            attempt=ATTEMPT,
            receipt_id=_receipt_of(session, committed),
        )
        failed = _proposal(session, occurrence="o-failed")
        _move(session, failed, FAILED, owner_id=OWNER, attempt=ATTEMPT, reason="rejected")
        session.commit()
    lease = _owner(factory)
    with factory() as session:
        assert set(candidates(session, lease)) == {abandoned, unknown, reconciled}
        assert len(candidates(session, lease, limit=2)) == 2
        for bad in (0, -1, True, 1.5):
            with pytest.raises(ValueError):
                candidates(session, lease, limit=bad)  # type: ignore[arg-type]


def _receipt_of(session: Session, proposal_id: str) -> str:
    return str(
        session.execute(
            select(CaliberEffectReceipt.receipt_id).where(
                CaliberEffectReceipt.proposal_id == proposal_id
            )
        ).scalar_one()
    )


def test_a_dispatch_under_the_current_lease_is_not_a_candidate(database: Any) -> None:
    _, factory = database
    with factory() as session:
        proposal_id = _proposal(session)
        lease = leased(session, OWNER)
        session.commit()
        assert proposal_id not in candidates(session, lease)


# ---------------------------------------------------------------- reconciling one proposal


def test_an_unknown_effect_the_downstream_holds_is_committed_with_its_receipt(
    database: Any,
) -> None:
    _, factory = database
    with factory() as session:
        proposal_id = _unknown(session, "o-1")
        body = _stored(session, proposal_id)
        session.commit()
    lease = _owner(factory)
    downstream = _Downstream(factory)
    with factory() as session:
        from caliber.effects.records import proposal_view

        downstream.script[proposal_view(session, proposal_id).logical_key] = Answer(200, body)
    result = reconcile_one(
        factory, proposal_id, lease=lease, targets=TARGETS, lookup=downstream, now=AT
    )
    assert (result.outcome, result.state) == (RECONCILED_COMMITTED, COMMITTED)
    with factory() as session:
        state = proposal_state(session, proposal_id)
        assert (state.state, state.receipt_id) == (COMMITTED, result.detail)
        assert _count(factory, CaliberEffectReceipt) == 1
        audited = (
            session.execute(
                select(CaliberAuditLog).where(CaliberAuditLog.action == "effect_reconciled")
            )
            .scalars()
            .all()
        )
        assert [a.actor for a in audited] == [f"reconciler:{RECONCILER}"]
    assert len(downstream.asked) == 1
    # a second look at the same proposal finds nothing to do and does not ask again
    again = reconcile_one(
        factory, proposal_id, lease=lease, targets=TARGETS, lookup=downstream, now=AT
    )
    assert (again.outcome, again.state) == (MOVED, COMMITTED)
    assert len(downstream.asked) == 1


def test_a_lost_owners_dispatch_is_abandoned_then_settled_in_one_pass(database: Any) -> None:
    """Lost-owner: the dispatcher died holding the attempt. The new owner makes it ``unknown`` and asks by key."""
    _, factory = database
    with factory() as session:
        proposal_id = _proposal(session)
        body = _stored(session, proposal_id)
        from caliber.effects.records import proposal_view

        key = proposal_view(session, proposal_id).logical_key
        session.commit()
    lease = _owner(factory)
    downstream = _Downstream(factory)
    downstream.script[key] = Answer(200, body)
    results = reconcile_pass(factory, lease=lease, targets=TARGETS, lookup=downstream, now=AT)
    assert [(r.proposal_id, r.outcome) for r in results] == [(proposal_id, RECONCILED_COMMITTED)]
    state = _state(factory, proposal_id)
    assert state.state == COMMITTED and state.owner_id == OWNER


def test_a_404_is_delayed_visibility_not_proof_that_nothing_committed(database: Any) -> None:
    """Crash before the send looks exactly like a request still in flight at the downstream: it stays unknown."""
    _, factory = database
    with factory() as session:
        proposal_id = _proposal(session)  # dispatched, never sent
        session.commit()
    lease = _owner(factory)
    downstream = _Downstream(factory)  # answers 404 to everything
    result = reconcile_pass(factory, lease=lease, targets=TARGETS, lookup=downstream, now=AT)[0]
    assert (result.proposal_id, result.outcome, result.state) == (
        proposal_id,
        STILL_UNKNOWN,
        UNKNOWN,
    )
    state = _state(factory, proposal_id)
    assert (state.state, state.reason) == (UNKNOWN, ABANDONED_REASON)
    assert _count_receipts(factory) == 0
    # and unknown can only be left through reconciled: there is no way back to a second dispatch
    with factory() as session, pytest.raises(ProposalError) as blind:
        transition(
            session,
            proposal_id,
            DISPATCHED,
            expected_version=state.version,
            owner_id=OWNER,
            attempt=ATTEMPT + 1,
            lease=lease,
        )
    assert blind.value.code == ILLEGAL_TRANSITION


def _count_receipts(factory: sessionmaker[Session]) -> int:
    return _count(factory, CaliberEffectReceipt)


def test_absence_is_final_only_for_a_connector_that_proves_it(
    database: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, factory = database
    with factory() as session:
        proposal_id = _unknown(session, "o-1")
        session.commit()
    lease = _owner(factory)
    base = CONNECTORS["document-request"]
    proving = ConnectorCapability(
        **{
            **base.capability.__dict__,
            "absence_proves_noncommit": True,
            "lookup_consistency": "read_your_writes",
        }
    )
    monkeypatch.setitem(reconcile.CONNECTORS, "document-request", replace(base, capability=proving))
    downstream = _Downstream(factory)
    result = reconcile_one(
        factory, proposal_id, lease=lease, targets=TARGETS, lookup=downstream, now=AT
    )
    assert (result.outcome, result.state, result.detail) == (
        RECONCILED_FAILED,
        FAILED,
        ABSENCE_PROVEN_REASON,
    )
    state = _state(factory, proposal_id)
    assert (state.state, state.reason) == (FAILED, ABSENCE_PROVEN_REASON)
    assert _count_receipts(factory) == 0


@pytest.mark.parametrize(
    ("answer", "outcome"),
    [
        (Answer(500, {"error": "down"}), STILL_UNKNOWN),
        (Answer(401, {}), STILL_UNKNOWN),
        (Answer(404, {}), STILL_UNKNOWN),
        (OpenApiExecutionError("unreachable"), LOOKUP_FAILED),
        (Answer(200, {"case_id": CASE}), UNVERIFIED),  # no request id: not a receipt
        (Answer(200, ["not", "a", "request"]), UNVERIFIED),
        (Answer(200, {"error": "weird"}), UNVERIFIED),
    ],
)
def test_nothing_but_a_matching_stored_request_moves_an_unknown_effect(
    answer: Any, outcome: str, database: Any
) -> None:
    _, factory = database
    with factory() as session:
        proposal_id = _unknown(session, "o-1")
        session.commit()
    lease = _owner(factory)
    downstream = _Downstream(factory)
    downstream.default = answer
    result = reconcile_one(
        factory, proposal_id, lease=lease, targets=TARGETS, lookup=downstream, now=AT
    )
    assert result.outcome == outcome and result.state == UNKNOWN
    assert _state(factory, proposal_id).state == UNKNOWN and _count_receipts(factory) == 0


def test_another_request_under_the_key_is_not_this_effects_receipt(database: Any) -> None:
    _, factory = database
    with factory() as session:
        proposal_id = _unknown(session, "o-1")
        other = _stored(session, proposal_id, document_type="bank_statement")
        session.commit()
    downstream = _Downstream(factory)
    downstream.default = Answer(200, other)
    result = reconcile_one(
        factory, proposal_id, lease=_owner(factory), targets=TARGETS, lookup=downstream, now=AT
    )
    assert (result.outcome, result.state) == (UNVERIFIED, UNKNOWN)


def test_a_connector_with_no_binding_is_left_unknown(database: Any) -> None:
    _, factory = database
    with factory() as session:
        proposal_id = _unknown(session, "o-1")
        session.commit()
    downstream = _Downstream(factory)
    lease = _owner(factory)
    unbound = reconcile_one(
        factory, proposal_id, lease=lease, targets={}, lookup=downstream, now=AT
    )
    other_project = reconcile_one(
        factory,
        proposal_id,
        lease=lease,
        targets={("document-request", "PRJ-other"): BASE_URL},
        lookup=downstream,
        now=AT,
    )
    assert (unbound.outcome, other_project.outcome) == (NOT_BOUND, NOT_BOUND)
    assert downstream.asked == [] and _state(factory, proposal_id).state == UNKNOWN


def test_a_connector_the_platform_cannot_reconcile_is_left_unknown(
    database: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, factory = database
    with factory() as session:
        proposal_id = _unknown(session, "o-1")
        session.commit()
    monkeypatch.delitem(reconcile.CONNECTORS, "document-request")
    result = reconcile_one(
        factory,
        proposal_id,
        lease=_owner(factory),
        targets=TARGETS,
        lookup=_Downstream(factory),
        now=AT,
    )
    assert (result.outcome, result.state) == (NOT_BOUND, UNKNOWN)


def test_a_dispatch_in_flight_under_the_current_lease_is_left_alone(database: Any) -> None:
    _, factory = database
    with factory() as session:
        proposal_id = _proposal(session)
        lease = leased(session, OWNER)  # the dispatcher is this very owner
        session.commit()
    downstream = _Downstream(factory)
    result = reconcile_one(
        factory, proposal_id, lease=lease, targets=TARGETS, lookup=downstream, now=AT
    )
    assert (result.outcome, result.state) == (IN_FLIGHT, DISPATCHED)
    assert downstream.asked == []
    assert _state(factory, proposal_id).state == DISPATCHED


def test_the_default_lookup_asks_through_the_connectors_own_query(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: dict[str, Any] = {}

    def executed(**kwargs: Any) -> dict[str, Any]:
        seen.update(kwargs)
        return {"status_code": 200, "json": {"request_id": "DR-1"}}

    monkeypatch.setattr(reconcile, "execute_openapi_http_tool", executed)
    answer = reconcile.ask_downstream(CONNECTORS["document-request"], BASE_URL, "key-1")
    assert answer == Answer(200, {"request_id": "DR-1"})
    assert seen["execution_config"]["server_url"] == BASE_URL
    assert seen["input_data"] == {"query_params": {"idempotency_key": "key-1"}}


def test_an_abandon_that_another_reconciler_got_to_first_is_moved_not_an_error(
    database: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, factory = database
    with factory() as session:
        proposal_id = _proposal(session)
        session.commit()
    lease = _owner(factory)

    def lost(*args: Any, **kwargs: Any) -> Any:
        raise StaleVersion(proposal_id, 3, 4, UNKNOWN)

    monkeypatch.setattr(reconcile, "abandon_dispatch", lost)
    result = reconcile_one(
        factory,
        proposal_id,
        lease=lease,
        targets=TARGETS,
        lookup=_Downstream(factory),
        now=AT,
    )
    assert (result.outcome, result.state) == (MOVED, DISPATCHED)


def test_an_abandon_refused_for_any_other_reason_is_raised(
    database: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, factory = database
    with factory() as session:
        proposal_id = _proposal(session)
        session.commit()
    lease = _owner(factory)

    def refused(*args: Any, **kwargs: Any) -> Any:
        raise ProposalError(EVIDENCE_REQUIRED, "scripted")

    monkeypatch.setattr(reconcile, "abandon_dispatch", refused)
    with pytest.raises(ProposalError) as raised:
        reconcile_one(
            factory,
            proposal_id,
            lease=lease,
            targets=TARGETS,
            lookup=_Downstream(factory),
            now=AT,
        )
    assert raised.value.code == EVIDENCE_REQUIRED


def test_a_proposal_settled_between_the_abandon_and_the_lookup_is_not_asked_about(
    database: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, factory = database
    with factory() as session:
        proposal_id = _unknown(session, "o-1")
        session.commit()
    lease = _owner(factory)

    def settled_meanwhile(*args: Any, **kwargs: Any) -> None:
        with factory() as other:
            _move(other, proposal_id, RECONCILED, reason="found by key")
            _move(other, proposal_id, FAILED, reason="rejected")
            other.commit()

    monkeypatch.setattr(reconcile, "_take_over", settled_meanwhile)
    downstream = _Downstream(factory)
    result = reconcile_one(
        factory, proposal_id, lease=lease, targets=TARGETS, lookup=downstream, now=AT
    )
    assert (result.outcome, result.state) == (MOVED, FAILED) and downstream.asked == []


def test_a_settled_proposal_is_not_asked_about(database: Any) -> None:
    _, factory = database
    with factory() as session:
        proposal_id = _proposal(session)
        _move(session, proposal_id, FAILED, owner_id=OWNER, attempt=ATTEMPT, reason="rejected")
        session.commit()
    downstream = _Downstream(factory)
    result = reconcile_one(
        factory, proposal_id, lease=_owner(factory), targets=TARGETS, lookup=downstream, now=AT
    )
    assert (result.outcome, result.state) == (MOVED, FAILED) and downstream.asked == []


def test_a_proposal_that_moved_while_the_downstream_was_asked_is_not_overwritten(
    database: Any,
) -> None:
    _, factory = database
    with factory() as session:
        proposal_id = _unknown(session, "o-1")
        body = _stored(session, proposal_id)
        session.commit()
    lease = _owner(factory)

    def meanwhile() -> Answer:
        with factory() as other:  # another actor settles it between the lookup and the write
            _move(other, proposal_id, RECONCILED, reason="found by key")
            other.commit()
        return Answer(200, body)

    downstream = _Downstream(factory)
    from caliber.effects.records import proposal_view

    with factory() as session:
        downstream.script[proposal_view(session, proposal_id).logical_key] = meanwhile
    result = reconcile_one(
        factory, proposal_id, lease=lease, targets=TARGETS, lookup=downstream, now=AT
    )
    assert (result.outcome, result.state) == (MOVED, RECONCILED)
    assert _count_receipts(factory) == 0


def test_a_reconciler_that_lost_its_lease_while_asking_records_nothing(database: Any) -> None:
    _, factory = database
    with factory() as session:
        proposal_id = _unknown(session, "o-1")
        body = _stored(session, proposal_id)
        from caliber.effects.records import proposal_view

        key = proposal_view(session, proposal_id).logical_key
        session.commit()
    lease = _owner(factory)

    def taken_over() -> Answer:
        with factory() as other:  # a new owner acquires after this one's lease lapsed
            _taken_over(other, lease, "reconciler-2")
            other.commit()
        return Answer(200, body)

    downstream = _Downstream(factory)
    downstream.script[key] = taken_over
    with pytest.raises(ProposalError) as lost:
        reconcile_one(factory, proposal_id, lease=lease, targets=TARGETS, lookup=downstream, now=AT)
    assert lost.value.code == LEASE_LOST
    assert _state(factory, proposal_id).state == UNKNOWN and _count_receipts(factory) == 0
    # the new owner settles it
    with factory() as session:
        new = leased(session, "reconciler-2")
    downstream.script[key] = Answer(200, body)
    result = reconcile_one(
        factory, proposal_id, lease=new, targets=TARGETS, lookup=downstream, now=AT
    )
    assert result.outcome == RECONCILED_COMMITTED


def test_a_pass_without_the_lease_asks_nothing(database: Any) -> None:
    _, factory = database
    with factory() as session:
        _unknown(session, "o-1")
        old = leased(session, OWNER)
        session.commit()
    _owner(factory)  # someone else holds it now
    downstream = _Downstream(factory)
    with pytest.raises(ProposalError) as lost:
        reconcile_pass(factory, lease=old, targets=TARGETS, lookup=downstream)
    assert lost.value.code == LEASE_LOST and downstream.asked == []


def test_no_question_is_asked_on_a_lease_that_is_gone(database: Any) -> None:
    _, factory = database
    with factory() as session:
        proposal_id = _unknown(session, "o-1")
        old = leased(session, OWNER)
        session.commit()
    _owner(factory)  # someone else holds it now

    def never(*args: Any, **kwargs: Any) -> Answer:
        pytest.fail("the downstream must not be asked under a lease that was taken over")

    with pytest.raises(ProposalError) as lost:
        reconcile_one(factory, proposal_id, lease=old, targets=TARGETS, lookup=never, now=AT)
    assert lost.value.code == LEASE_LOST and _state(factory, proposal_id).state == UNKNOWN


def test_a_pass_stops_when_the_lease_goes_and_keeps_what_it_already_settled(
    database: Any,
) -> None:
    _, factory = database
    with factory() as session:
        first = _unknown(session, "o-1")
        second = _unknown(session, "o-2")
        bodies = {p: _stored(session, p) for p in (first, second)}
        from caliber.effects.records import proposal_view

        keys = {p: proposal_view(session, p).logical_key for p in (first, second)}
        session.commit()
    lease = _owner(factory)
    downstream = _Downstream(factory)
    for pid in (first, second):
        downstream.script[keys[pid]] = Answer(200, bodies[pid])
    order = sorted((first, second), key=lambda p: candidates_order(factory, lease).index(p))

    def lose_the_lease() -> Answer:
        with factory() as other:
            _taken_over(other, lease, "reconciler-2")
            other.commit()
        return Answer(200, bodies[order[1]])

    downstream.script[keys[order[1]]] = lose_the_lease
    with pytest.raises(ProposalError) as lost:
        reconcile_pass(factory, lease=lease, targets=TARGETS, lookup=downstream, now=AT)
    assert lost.value.code == LEASE_LOST
    assert _state(factory, order[0]).state == COMMITTED, "settled before the lease went"
    assert _state(factory, order[1]).state == UNKNOWN


def candidates_order(factory: sessionmaker[Session], lease: Lease) -> list[str]:
    with factory() as session:
        return candidates(session, lease)


def test_a_pass_reports_a_proposal_it_could_not_settle_and_goes_on(
    database: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, factory = database
    with factory() as session:
        broken = _unknown(session, "o-1")
        fine = _unknown(session, "o-2")
        fine_body = _stored(session, fine)
        from caliber.effects.records import proposal_view

        fine_key = proposal_view(session, fine).logical_key
        session.commit()
    lease = _owner(factory)
    real = reconcile.reconcile_one

    def flaky(factory_: Any, proposal_id: str, **kwargs: Any) -> Any:
        if proposal_id == broken:
            raise ProposalError(EVIDENCE_REQUIRED, "scripted")
        return real(factory_, proposal_id, **kwargs)

    monkeypatch.setattr(reconcile, "reconcile_one", flaky)
    downstream = _Downstream(factory)
    downstream.script[fine_key] = Answer(200, fine_body)
    results = {
        r.proposal_id: r
        for r in reconcile_pass(factory, lease=lease, targets=TARGETS, lookup=downstream, now=AT)
    }
    assert (results[broken].outcome, results[broken].detail) == (STILL_UNKNOWN, EVIDENCE_REQUIRED)
    assert results[fine].outcome == RECONCILED_COMMITTED


def test_targets_come_from_the_callback_bindings() -> None:
    bindings = (
        CallbackBinding("document-request", "PRJ-a", "env://A", "https://a.example"),
        CallbackBinding("document-request", "PRJ-b", "env://B", "https://b.example"),
    )
    assert targets_from_bindings(bindings) == {
        ("document-request", "PRJ-a"): "https://a.example",
        ("document-request", "PRJ-b"): "https://b.example",
    }


def test_commit_found_is_one_commit_however_often_it_is_called(database: Any) -> None:
    _, factory = database
    with factory() as session:
        proposal_id = _unknown(session, "o-1")
        from caliber.effects.connectors.document_request import facts_from_lookup
        from caliber.effects.records import proposal_view

        facts = facts_from_lookup(
            proposal_view(session, proposal_id),
            lookup_status=200,
            lookup_body=_stored(session, proposal_id),
        )
        assert facts is not None
        first = commit_found(session, proposal_id, facts, reason="t", now=AT)
        second = commit_found(session, proposal_id, facts, reason="t", now=AT)
        assert first[1] is True and second == (first[0], False)
        assert proposal_state(session, proposal_id).state == COMMITTED
        session.commit()


# ---------------------------------------------------------------- unknown blocks what depends on it


def test_an_open_effect_blocks_its_node_and_everything_downstream_until_it_is_settled(
    database: Any,
) -> None:
    _, factory = database
    with factory() as session:
        proposal_id = _unknown(session, "o-1")  # a proposal of node "ask" in RUN-ep
        node = session.execute(
            select(CaliberEffectProposal.node_id).where(
                CaliberEffectProposal.proposal_id == proposal_id
            )
        ).scalar_one()
        session.commit()
    gate = effect_gate(factory, "RUN-ep")
    for nodes in ({node}, {node, "later"}, {"a", node}):
        with pytest.raises(UnknownEffectError) as blocked:
            gate(nodes)
        assert blocked.value.open_effects == ((proposal_id, node, UNKNOWN),)
        assert "reconcile" in str(blocked.value) and proposal_id in str(blocked.value)
    gate({"unrelated"})  # nothing upstream of it asked for an effect
    gate(set())
    assert effect_gate(factory, "RUN-other") is not None
    effect_gate(factory, "RUN-other")({node})  # another run's effects are not this run's business
    with factory() as session:
        assert unresolved_effects(session, "RUN-ep", {node}) == [(proposal_id, node, UNKNOWN)]
        assert unresolved_effects(session, "RUN-ep", []) == []
    # settled by the reconciler, the gate opens
    with factory() as session:
        body = _stored(session, proposal_id)
        from caliber.effects.records import proposal_view

        key = proposal_view(session, proposal_id).logical_key
    downstream = _Downstream(factory)
    downstream.script[key] = Answer(200, body)
    reconcile_one(
        factory, proposal_id, lease=_owner(factory), targets=TARGETS, lookup=downstream, now=AT
    )
    gate({node})


@pytest.mark.parametrize("state", [APPROVED])
def test_an_effect_that_was_never_dispatched_does_not_block(state: str, database: Any) -> None:
    _, factory = database
    with factory() as session:
        from tests.test_effect_records import _at

        proposal_id = _at(session, state)
        node = session.execute(
            select(CaliberEffectProposal.node_id).where(
                CaliberEffectProposal.proposal_id == proposal_id
            )
        ).scalar_one()
        session.commit()
    effect_gate(factory, "RUN-ep")({node})


# ---------------------------------------------------------------- two reconcilers, one proposal (PostgreSQL)


def test_two_reconcilers_of_one_proposal_commit_it_once_on_postgresql(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    if not POSTGRES_TEST_URL:
        pytest.skip("the race needs PostgreSQL")
    with _database("postgresql", tmp_path, monkeypatch) as (_, factory):
        _seed_run(factory)
        with factory() as session:
            proposal_id = _unknown(session, "o-1")
            body = _stored(session, proposal_id)
            from caliber.effects.records import proposal_view

            key = proposal_view(session, proposal_id).logical_key
            session.commit()
        lease = _owner(factory)
        barrier = threading.Barrier(4)
        outcomes: list[str] = []
        errors: list[BaseException] = []
        lock = threading.Lock()

        def asked(connector: Any, base_url: str, k: str) -> Answer:
            barrier.wait(10)  # every reconciler has read it unknown before any writes
            return Answer(200, body)

        def run() -> None:
            try:
                result = reconcile_one(
                    factory, proposal_id, lease=lease, targets=TARGETS, lookup=asked, now=AT
                )
                with lock:
                    outcomes.append(result.outcome)
            except BaseException as exc:  # pragma: no cover - reported below
                with lock:
                    errors.append(exc)

        threads = [threading.Thread(target=run, daemon=True) for _ in range(4)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(60)
        assert not errors, errors
        assert sorted(outcomes) == [RECONCILED_COMMITTED] + [MOVED] * 3
        assert _count_receipts(factory) == 1
        assert _state(factory, proposal_id).state == COMMITTED
        assert key


# ---------------------------------------------------------------- the runtime asks before it runs a node


def test_the_runtime_gate_is_asked_about_a_node_and_everything_upstream_of_it() -> None:
    from caliber.workflows.compiler import build_ir
    from caliber.workflows.manifest import parse_manifest
    from caliber.workflows.runtime import FakeWorkflowExecutor, RuntimePlan, execute
    from tests.workflow_helpers import fake_resolver, make_manifest

    ir = build_ir(parse_manifest(make_manifest()), fake_resolver(), version="gate")
    asked: list[set[str]] = []
    plan = RuntimePlan(
        ir=ir, resolver=fake_resolver(), effect_gate=lambda ids: asked.append(set(ids))
    )
    assert execute(plan, "hi", executor=FakeWorkflowExecutor()).status == "completed"
    assert asked == [{"start"}, {"start", "agent"}, {"start", "agent", "final"}]


def test_a_refusing_gate_stops_the_node_before_it_runs_and_the_run_reports_why() -> None:
    from caliber.workflows.compiler import build_ir
    from caliber.workflows.manifest import parse_manifest
    from caliber.workflows.runtime import FakeWorkflowExecutor, RuntimePlan, execute
    from tests.workflow_helpers import fake_resolver, make_manifest

    ir = build_ir(parse_manifest(make_manifest()), fake_resolver(), version="gate")
    calls: list[set[str]] = []

    def gate(ids: Any) -> None:
        calls.append(set(ids))
        if "agent" in ids:
            raise UnknownEffectError([("prop-1", "start", UNKNOWN)])

    result = execute(
        RuntimePlan(ir=ir, resolver=fake_resolver(), effect_gate=gate),
        "hi",
        executor=FakeWorkflowExecutor(),
    )
    assert result.status == "blocked"
    assert (result.error or "") == "waiting_effect:agent"
    checkpoint = result.steps[-1].checkpoint_state
    assert checkpoint and checkpoint["kind"] == "effect_reconciliation"
    assert checkpoint["effects"][0]["proposal_id"] == "prop-1"
    assert [step.node_id for step in result.steps] == ["start", "agent"]
    assert calls == [{"start"}, {"start", "agent"}], (
        "asked once per node, never for the node after the refusal"
    )


def test_a_plan_without_a_gate_runs_as_it_always_did() -> None:
    from caliber.workflows.compiler import build_ir
    from caliber.workflows.manifest import parse_manifest
    from caliber.workflows.runtime import FakeWorkflowExecutor, RuntimePlan, execute
    from tests.workflow_helpers import fake_resolver, make_manifest

    ir = build_ir(parse_manifest(make_manifest()), fake_resolver(), version="gate")
    plan = RuntimePlan(ir=ir, resolver=fake_resolver())
    assert plan.effect_gate is None
    assert execute(plan, "hi", executor=FakeWorkflowExecutor()).status == "completed"


def test_upstream_nodes_follow_every_path_and_never_include_what_comes_after() -> None:
    from types import SimpleNamespace

    from caliber.workflows.runtime import _upstream_node_ids

    def graph(edges: list[tuple[str, str]]) -> Any:
        nodes = {n: SimpleNamespace() for pair in edges for n in pair}
        return SimpleNamespace(
            nodes=nodes, edges=[SimpleNamespace(from_node=f, to_node=t) for f, t in edges]
        )

    ir = graph(
        [("s", "a"), ("s", "b"), ("a", "j"), ("b", "j"), ("j", "o"), ("x", "j"), ("j", "later")]
    )
    assert _upstream_node_ids(ir, "j") == {"s", "a", "b", "x", "j"}
    assert _upstream_node_ids(ir, "s") == {"s"}
    assert _upstream_node_ids(ir, "o") == {"s", "a", "b", "x", "j", "o"}
    assert _upstream_node_ids(graph([("a", "b"), ("b", "a")]), "a") == {"a", "b"}


def test_a_node_depends_on_the_inline_targets_of_everything_upstream_of_it() -> None:
    """A fan-out, loop or boundary runs its target itself (no edge to it): the target's effect is the node's effect."""
    from caliber.workflows.ir import (
        IRAgent,
        IREdge,
        IRErrorBoundary,
        IRForEach,
        IRHandoff,
        IRLoop,
        IRWorkflow,
    )
    from caliber.workflows.runtime import _upstream_node_ids

    nodes: dict[str, Any] = {
        "start": IRAgent(node_id="start", node_type="agent", handoffs=[IRHandoff("helper")]),
        "each": IRForEach(node_id="each", node_type="for_each", target_node_id="t-each"),
        "again": IRLoop(node_id="again", node_type="loop", target_node_id="t-loop"),
        "guard": IRErrorBoundary(
            node_id="guard",
            node_type="error_boundary",
            target_node_id="t-guard",
            compensate_with="undo",
        ),
        "plain": IRAgent(node_id="plain", node_type="agent"),
        "final": IRAgent(node_id="final", node_type="agent"),
        "unrelated": IRAgent(node_id="unrelated", node_type="agent"),
    }
    for name in ("helper", "t-each", "t-loop", "t-guard", "undo"):
        nodes[name] = IRAgent(node_id=name, node_type="agent")
    chain = ["start", "each", "again", "guard", "plain", "final"]
    ir = IRWorkflow(
        workflow_id="w",
        version="1",
        nodes=nodes,
        edges=[
            IREdge(f"e{i}", a, "out", b, "in", None)  # type: ignore[arg-type]
            for i, (a, b) in enumerate(itertools.pairwise(chain))
        ],
        entry_node_id="start",
        output_node_id="final",
    )
    assert _upstream_node_ids(ir, "final") == {
        *chain,
        "helper",
        "t-each",
        "t-loop",
        "t-guard",
        "undo",
    }
    assert _upstream_node_ids(ir, "each") == {"start", "each", "helper", "t-each"}
    assert "unrelated" not in _upstream_node_ids(ir, "final")


def _wrapped_manifest(wrapper: str) -> dict[str, Any]:
    from tests.workflow_helpers import make_manifest

    data = make_manifest()
    wrap: dict[str, Any] = {"id": "wrap", "type": wrapper, "target_node_id": "agent"}
    if wrapper == "for_each":
        wrap |= {
            "inputs": {"items": {"type": "structured"}},
            "outputs": {"text": {"type": "string"}, "results": {"type": "structured"}},
        }
        port, out = "items", "text"
    elif wrapper == "loop":
        wrap |= {
            "max_iterations": 2,
            "stop_condition": "False",
            "inputs": {"input": {"type": "string"}},
            "outputs": {"output": {"type": "string"}},
        }
        port, out = "input", "output"
    else:
        wrap |= {
            "fallback_text": "fallback",
            "inputs": {"input": {"type": "string"}},
            "outputs": {"output": {"type": "string"}, "error": {"type": "structured"}},
        }
        port, out = "input", "output"
    data["nodes"]["wrap"] = wrap
    data["edges"] = [
        {"id": "e1", "from": "start", "to": "wrap", "map": {"msg": port}},
        {"id": "e2", "from": "wrap", "to": "final", "map": {out: "response"}},
    ]
    return data


def _gate_refusing(node: str, asked: list[set[str]]) -> Any:
    def gate(ids: Any) -> None:
        asked.append(set(ids))
        if node in ids:
            raise UnknownEffectError([("prop-1", node, UNKNOWN)])

    return gate


@pytest.mark.parametrize("wrapper", ["for_each", "loop", "error_boundary"])
def test_an_open_effect_of_an_inline_target_is_not_absorbed_and_blocks_everything_after(
    wrapper: str,
) -> None:
    """A fan-out, loop or boundary may not isolate the refusal as the item's (or the target's) own error, and the nodes
    after it are gated on the target as well."""
    from caliber.workflows.compiler import build_ir
    from caliber.workflows.manifest import parse_manifest
    from caliber.workflows.runtime import FakeWorkflowExecutor, RuntimePlan, execute
    from tests.workflow_helpers import fake_resolver

    asked: list[set[str]] = []
    plan = RuntimePlan(
        ir=build_ir(parse_manifest(_wrapped_manifest(wrapper)), fake_resolver(), version="g"),
        resolver=fake_resolver(),
        effect_gate=_gate_refusing("agent", asked),
    )
    executor = FakeWorkflowExecutor()
    result = execute(plan, '["a","b"]', executor=executor)
    assert result.status == "blocked", [s.node_id for s in result.steps]
    assert (result.error or "") == "waiting_effect:wrap", result.error
    assert executor.calls == [], "the provider was never called"
    assert "final" not in [step.node_id for step in result.steps]
    assert any("wrap" in ids and "agent" in ids for ids in asked), (
        "the wrapper is gated on its own target"
    )


def test_a_compensation_does_not_run_against_an_effect_nobody_can_vouch_for() -> None:
    from caliber.workflows.compiler import build_ir
    from caliber.workflows.manifest import parse_manifest
    from caliber.workflows.runtime import FakeWorkflowExecutor, RuntimePlan, execute
    from tests.workflow_helpers import fake_resolver, make_manifest

    data = make_manifest()
    data["nodes"]["boom"] = {
        "id": "boom",
        "type": "python_code",
        "code": "raise RuntimeError('boom')",
        "inputs": {"input": {"type": "string"}},
        "outputs": {"text": {"type": "string"}},
    }
    data["nodes"]["wrap"] = {
        "id": "wrap",
        "type": "error_boundary",
        "target_node_id": "boom",
        "compensate_with": "agent",
        "fallback_text": "fallback",
        "inputs": {"input": {"type": "string"}},
        "outputs": {"output": {"type": "string"}, "error": {"type": "structured"}},
    }
    data["edges"] = [
        {"id": "e1", "from": "start", "to": "wrap", "map": {"msg": "input"}},
        {"id": "e2", "from": "wrap", "to": "final", "map": {"output": "response"}},
    ]
    asked: list[set[str]] = []
    executor = FakeWorkflowExecutor()
    result = execute(
        RuntimePlan(
            ir=build_ir(parse_manifest(data), fake_resolver(), version="g"),
            resolver=fake_resolver(),
            effect_gate=_gate_refusing("agent", asked),
        ),
        "hi",
        executor=executor,
    )
    assert result.status == "blocked" and (result.error or "") == "waiting_effect:wrap"
    assert executor.calls == []


def test_a_node_retry_is_gated_again_before_each_attempt() -> None:
    from caliber.workflows.compiler import build_ir
    from caliber.workflows.manifest import parse_manifest
    from caliber.workflows.runtime import FakeWorkflowExecutor, RuntimePlan, execute
    from tests.workflow_helpers import fake_resolver, make_manifest

    data = make_manifest()
    data["nodes"]["boom"] = {
        "id": "boom",
        "type": "python_code",
        "code": "raise RuntimeError('boom')",
        "execution_policy": {"max_retries": 2},
        "inputs": {"input": {"type": "string"}},
        "outputs": {"text": {"type": "string"}},
    }
    data["edges"] = [
        {"id": "e1", "from": "start", "to": "boom", "map": {"msg": "input"}},
        {"id": "e2", "from": "boom", "to": "final", "map": {"text": "response"}},
    ]
    calls: list[set[str]] = []

    def gate(ids: Any) -> None:
        calls.append(set(ids))
        if (
            "boom" in ids and sum("boom" in c for c in calls) == 2
        ):  # the second attempt: its effect went unknown
            raise UnknownEffectError([("prop-1", "boom", UNKNOWN)])

    result = execute(
        RuntimePlan(
            ir=build_ir(parse_manifest(data), fake_resolver(), version="g"),
            resolver=fake_resolver(),
            effect_gate=gate,
        ),
        "hi",
        executor=FakeWorkflowExecutor(),
    )
    assert result.status == "blocked" and (result.error or "") == "waiting_effect:boom"
    assert sum("boom" in c for c in calls) == 2, "asked on the retry, and a refusal is not retried"


def test_a_guardrail_retry_is_gated_before_it_runs_the_agent_again(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from caliber.workflows import runtime as runtime_module
    from caliber.workflows.compiler import build_ir
    from caliber.workflows.manifest import parse_manifest
    from caliber.workflows.runtime import RuntimePlan, execute
    from tests.test_workflow_hardening import _GroundAfterExecutor
    from tests.workflow_helpers import fake_resolver, make_support_manifest

    data = make_support_manifest()
    data["nodes"]["policy_guardrail"]["on_failure"] = "block_retry"
    data["nodes"]["policy_guardrail"]["max_retries"] = 2
    retrying = {"started": False}
    real = runtime_module._retry_blocked_guardrail

    def marked(*args: Any, **kwargs: Any) -> Any:
        retrying["started"] = True
        return real(*args, **kwargs)

    monkeypatch.setattr(runtime_module, "_retry_blocked_guardrail", marked)

    def gate(ids: Any) -> None:
        if retrying["started"] and "policy_guardrail" not in ids:  # the agent's own turn
            raise UnknownEffectError([("prop-1", "support_agent", UNKNOWN)])

    result = execute(
        RuntimePlan(
            ir=build_ir(parse_manifest(data), fake_resolver(), version="g"),
            resolver=fake_resolver(),
            effect_gate=gate,
        ),
        "what is your refund policy?",
        executor=_GroundAfterExecutor(),
    )
    assert result.status == "blocked" and (result.error or "") == "waiting_effect:support_agent"
    assert retrying["started"]


@pytest.mark.parametrize("wrapper", ["for_each", "loop", "error_boundary"])
def test_an_effect_that_goes_unknown_while_the_wrapper_runs_is_still_not_absorbed(
    wrapper: str,
) -> None:
    """The wrapper passed its own gate; the open effect appears when the *target* is asked (a concurrent branch left it unknown)."""
    from caliber.workflows.compiler import build_ir
    from caliber.workflows.manifest import parse_manifest
    from caliber.workflows.runtime import FakeWorkflowExecutor, RuntimePlan, execute
    from tests.workflow_helpers import fake_resolver

    def gate(ids: Any) -> None:
        if set(ids) == {"agent"}:  # only the target's own run
            raise UnknownEffectError([("prop-1", "agent", UNKNOWN)])

    executor = FakeWorkflowExecutor()
    result = execute(
        RuntimePlan(
            ir=build_ir(parse_manifest(_wrapped_manifest(wrapper)), fake_resolver(), version="g"),
            resolver=fake_resolver(),
            effect_gate=gate,
        ),
        '["a","b"]',
        executor=executor,
    )
    assert result.status == "blocked", [(s.node_id, s.status, s.detail) for s in result.steps]
    assert (result.error or "") == "waiting_effect:agent" and executor.calls == []


# ---------------------------------------------------------------- a backlog cannot hide a newer proposal


def test_a_pass_reaches_a_newer_proposal_behind_a_backlog_that_stays_unknown(database: Any) -> None:
    _, factory = database
    with factory() as session:
        stuck = [_unknown(session, f"o-stuck-{n}") for n in range(3)]
        fresh = _unknown(session, "o-fresh")
        body = _stored(session, fresh)
        from caliber.effects.records import proposal_view

        key = proposal_view(session, fresh).logical_key
        session.commit()
    lease = _owner(factory)
    downstream = _Downstream(factory)  # 404 for the backlog
    downstream.script[key] = Answer(200, body)
    results = reconcile_pass(
        factory, lease=lease, targets=TARGETS, lookup=downstream, limit=2, now=AT
    )
    outcomes = {r.proposal_id: r.outcome for r in results}
    assert outcomes[fresh] == RECONCILED_COMMITTED
    assert {outcomes[p] for p in stuck} == {STILL_UNKNOWN}
    assert len(results) == 4 and len(downstream.asked) == 4, "every proposal asked once, none twice"


def test_a_pass_stops_at_its_budget_and_reports_progress_before_each_proposal(
    database: Any,
) -> None:
    _, factory = database
    with factory() as session:
        for n in range(3):
            _unknown(session, f"o-{n}")
        session.commit()
    lease = _owner(factory)
    downstream = _Downstream(factory)
    progress: list[int] = []
    results = reconcile_pass(
        factory,
        lease=lease,
        targets=TARGETS,
        lookup=downstream,
        budget_seconds=0.0,
        on_progress=lambda: progress.append(1),
    )
    assert results == [] and progress == [] and downstream.asked == []
    results = reconcile_pass(
        factory,
        lease=lease,
        targets=TARGETS,
        lookup=downstream,
        on_progress=lambda: progress.append(1),
    )
    assert len(results) == 3 and len(progress) == 3

    def stop() -> None:
        raise RuntimeError("the owner loop is stopping")

    with pytest.raises(RuntimeError, match="stopping"):
        reconcile_pass(factory, lease=lease, targets=TARGETS, lookup=downstream, on_progress=stop)


@pytest.mark.parametrize("raised", ["stale", "receipt_state"])
def test_a_commit_that_loses_to_another_actor_is_moved_not_an_error(
    raised: str, database: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    from caliber.effects.receipts import RECEIPT_STATE

    _, factory = database
    with factory() as session:
        proposal_id = _unknown(session, "o-1")
        body = _stored(session, proposal_id)
        from caliber.effects.records import proposal_view

        key = proposal_view(session, proposal_id).logical_key
        session.commit()
    lease = _owner(factory)

    def lost(*args: Any, **kwargs: Any) -> Any:
        if raised == "stale":
            raise StaleVersion(proposal_id, 3, 4, COMMITTED)
        raise ProposalError(RECEIPT_STATE, "committed meanwhile")

    monkeypatch.setattr(reconcile, "commit_found", lost)
    downstream = _Downstream(factory)
    downstream.script[key] = Answer(200, body)
    result = reconcile_one(
        factory, proposal_id, lease=lease, targets=TARGETS, lookup=downstream, now=AT
    )
    assert result.outcome == MOVED and result.state == UNKNOWN


def test_any_other_refusal_while_committing_is_raised(
    database: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, factory = database
    with factory() as session:
        proposal_id = _unknown(session, "o-1")
        body = _stored(session, proposal_id)
        from caliber.effects.records import proposal_view

        key = proposal_view(session, proposal_id).logical_key
        session.commit()

    def refused(*args: Any, **kwargs: Any) -> Any:
        raise ProposalError(EVIDENCE_REQUIRED, "scripted")

    monkeypatch.setattr(reconcile, "commit_found", refused)
    downstream = _Downstream(factory)
    downstream.script[key] = Answer(200, body)
    with pytest.raises(ProposalError) as raised:
        reconcile_one(
            factory,
            proposal_id,
            lease=_owner(factory),
            targets=TARGETS,
            lookup=downstream,
            now=AT,
        )
    assert raised.value.code == EVIDENCE_REQUIRED
