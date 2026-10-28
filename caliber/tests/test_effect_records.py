"""The seven-state transition table with compare-and-set versioning (B05.2, #705).

Every case runs on SQLite and, where ``CALIBER_TEST_POSTGRES_URL`` is set, on PostgreSQL, against a database migrated by
Alembic. The concurrency cases are PostgreSQL's: SQLite serialises writers, so a race cannot be arranged there.
"""

from __future__ import annotations

import importlib.util
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import inspect, select, text, update
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session, sessionmaker

from caliber.db.immutable import ImmutableRecordError
from caliber.db.models import CaliberEffectProposal, CaliberEffectReceipt, CaliberPolicyDecision
from caliber.effects import records
from caliber.effects.connectors import Classified, TransportOutcome
from caliber.effects.receipts import ReceiptFacts, record_receipt, settle_dispatch
from caliber.effects.records import (
    APPROVAL_REFUSED,
    APPROVED,
    BAD_FIELD,
    COMMITTED,
    DISPATCHED,
    EFFECT_ACTION,
    EVIDENCE_REQUIRED,
    FAILED,
    ILLEGAL_TRANSITION,
    LEASE_LOST,
    POLICY_DECISION,
    PROPOSED,
    RECONCILED,
    STALE_OWNER,
    STALE_VERSION,
    STATES,
    TRANSITIONS,
    UNKNOWN,
    Approval,
    ProposalError,
    StaleVersion,
    effect_resource,
    proposal_state,
    proposal_view,
    read_proposal,
    transition,
)
from caliber.ownership import Lease, acquire, release
from tests.dialect_helpers import DIALECTS, POSTGRES_TEST_URL
from tests.effect_helpers import TTL_SECONDS, leased
from tests.test_effect_proposals import _record, _seed_run
from tests.test_event_outbox import ALEMBIC_INI, _database

AT = datetime(2026, 3, 4, 5, 6, 7, tzinfo=timezone.utc)
NAIVE_AT = AT.replace(tzinfo=None)
OWNER, ATTEMPT = "worker-1", 1


def _decision(
    session: Session,
    proposal_id: str,
    *,
    decision_id: str = "pd-ok",
    result: str = "allow",
    action: str = EFFECT_ACTION,
    run: str = "RUN-ep",
    resource: str | None = None,
) -> str:
    session.add(
        CaliberPolicyDecision(
            decision_id=decision_id,
            workflow_run_id=run,
            grant_id="gr-1",
            subject_user_id="alice",
            workload_id="wl_" + "0" * 64,
            action=action,
            resource=effect_resource(proposal_id) if resource is None else resource,
            purpose="answer a ticket",
            result=result,
            reason_codes=[],
            policy_version="policy-v1",
            input_hash="0" * 64,
            evaluated_at=NAIVE_AT,
        )
    )
    session.flush()
    return decision_id


def _new(session: Session, *, effect_class: str = "read", occurrence: str = "occ-1") -> str:
    return str(_record(session, occurrence, effect_class=effect_class).proposal_id)


def _move(session: Session, proposal_id: str, to: str, **evidence: Any) -> Any:
    state = proposal_state(session, proposal_id)
    if evidence.get("owner_id") and "lease" not in evidence and DISPATCHED in (to, state.state):
        evidence["lease"] = leased(session, evidence["owner_id"])  # the lease the owner captured
    return transition(session, proposal_id, to, expected_version=state.version, now=AT, **evidence)


def _receipt(session: Session, proposal_id: str) -> str:
    """The verified receipt of this proposal's dispatch (recorded once; asking again returns it)."""
    view = proposal_view(session, proposal_id)
    facts = ReceiptFacts(
        attempt=view.attempt or 1,
        idempotency_key=view.logical_key,
        connector=view.connector,
        connector_version=view.connector_version,
        resource=view.resource,
        intent_hash=view.intent_hash,
        downstream_request_id=f"DR-{proposal_id}",
        response_digest="sha256:" + "a" * 64,
        resource_version="2",
    )
    return record_receipt(session, proposal_id, facts, now=AT).receipt_id


def _evidence_for(session: Session, proposal_id: str, from_state: str, to: str) -> dict[str, Any]:
    """The evidence that makes ``from_state -> to`` a well-formed move (not necessarily a legal one)."""
    if to == APPROVED:
        decision = f"pd-{proposal_id}-{from_state}"
        _decision(session, proposal_id, decision_id=decision)
        return {"approval": Approval(POLICY_DECISION, decision)}
    if to == DISPATCHED:
        return {"owner_id": OWNER, "attempt": ATTEMPT}
    evidence: dict[str, Any] = {}
    if to == COMMITTED:
        open_now = proposal_state(session, proposal_id).state in (DISPATCHED, UNKNOWN, RECONCILED)
        evidence["receipt_id"] = _receipt(session, proposal_id) if open_now else "rcpt-none"
    if from_state == DISPATCHED:
        evidence.update(owner_id=OWNER, attempt=ATTEMPT)
    if to in (FAILED, UNKNOWN, RECONCILED):
        evidence["reason"] = "because"
    return evidence


_PATHS: dict[str, list[str]] = {
    PROPOSED: [],
    APPROVED: [APPROVED],
    DISPATCHED: [APPROVED, DISPATCHED],
    UNKNOWN: [APPROVED, DISPATCHED, UNKNOWN],
    RECONCILED: [APPROVED, DISPATCHED, UNKNOWN, RECONCILED],
    COMMITTED: [APPROVED, DISPATCHED, COMMITTED],
    FAILED: [FAILED],
}


def _at(session: Session, state: str, **kwargs: Any) -> str:
    """A fresh read proposal walked, by legal moves, to ``state``."""
    proposal_id = _new(session, **kwargs)
    here = PROPOSED
    for step in _PATHS[state]:
        _move(session, proposal_id, step, **_evidence_for(session, proposal_id, here, step))
        here = step
    assert proposal_state(session, proposal_id).state == state
    return proposal_id


def _refused(
    code: str, session: Session, proposal_id: str, to: str, **kwargs: Any
) -> ProposalError:
    kwargs.setdefault("expected_version", proposal_state(session, proposal_id).version)
    with pytest.raises(ProposalError) as refused:
        transition(session, proposal_id, to, **kwargs)
    assert refused.value.code == code, refused.value
    return refused.value


# ---------------------------------------------------------------- the table


def test_the_table_is_the_adr_0003_seven_states_and_only_these_ten_moves() -> None:
    assert STATES == (
        "proposed",
        "approved",
        "dispatched",
        "committed",
        "failed",
        "unknown",
        "reconciled",
    )
    assert set(TRANSITIONS) == set(STATES)
    legal = {(a, b) for a, targets in TRANSITIONS.items() for b in targets}
    assert legal == {
        ("proposed", "approved"),
        ("proposed", "failed"),
        ("approved", "dispatched"),
        ("approved", "failed"),
        ("dispatched", "committed"),
        ("dispatched", "failed"),
        ("dispatched", "unknown"),
        ("unknown", "reconciled"),
        ("reconciled", "committed"),
        ("reconciled", "failed"),
    }
    assert TRANSITIONS["committed"] == TRANSITIONS["failed"] == frozenset()
    assert ("unknown", "dispatched") not in legal, "recovery is never a blind second dispatch"
    assert ("proposed", "dispatched") not in legal, "even a policy-approved read is approved first"
    assert ("unknown", "committed") not in legal and ("unknown", "failed") not in legal


def test_the_database_trigger_lists_the_same_moves_as_the_table() -> None:
    path = (
        Path(__file__).resolve().parent.parent
        / "src/caliber/db/migrations/versions/0124_effect_proposal_transitions.py"
    )
    spec = importlib.util.spec_from_file_location("migration_0124", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assert {a: frozenset(b) for a, b in module._TRANSITIONS.items()} == {
        a: b for a, b in TRANSITIONS.items() if b
    }
    assert set(module._SETTLED) == {COMMITTED, FAILED}


@pytest.mark.parametrize("dialect", DIALECTS)
@pytest.mark.parametrize("from_state", STATES)
@pytest.mark.parametrize("to_state", STATES)
def test_transition_table(
    dialect: str, from_state: str, to_state: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """All 49 pairs: each legal one is recorded with its evidence and one version step, each other one is refused."""
    with _database(dialect, tmp_path, monkeypatch) as (_, factory):
        _seed_run(factory)
        with factory() as session:
            proposal_id = _at(session, from_state)
            before = proposal_state(session, proposal_id)
            evidence = _evidence_for(session, proposal_id, from_state, to_state)
            if "owner_id" in evidence:
                evidence["lease"] = leased(session, OWNER)
            if to_state in TRANSITIONS[from_state]:
                moved = transition(
                    session,
                    proposal_id,
                    to_state,
                    expected_version=before.version,
                    now=AT,
                    **evidence,
                )
                after = proposal_state(session, proposal_id)
                assert (moved.applied, moved.state, moved.version) == (
                    True,
                    to_state,
                    before.version + 1,
                )
                assert (after.state, after.version) == (to_state, before.version + 1)
            else:
                _refused(ILLEGAL_TRANSITION, session, proposal_id, to_state, **evidence)
                after = proposal_state(session, proposal_id)
                assert (after.state, after.version) == (from_state, before.version), (
                    "nothing was recorded"
                )
            session.commit()


@pytest.mark.parametrize("dialect", DIALECTS)
def test_a_settled_proposal_never_moves_again_in_the_application_or_the_database(
    dialect: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with _database(dialect, tmp_path, monkeypatch) as (engine, factory):
        _seed_run(factory)
        with factory() as session:
            ids = [
                _at(session, COMMITTED, occurrence="o-c"),
                _at(session, FAILED, occurrence="o-f"),
            ]
            session.commit()
        for proposal_id in ids:
            with factory() as session:
                for target in STATES:
                    _refused(
                        ILLEGAL_TRANSITION,
                        session,
                        proposal_id,
                        target,
                        **_evidence_for(session, proposal_id, "committed", target),
                    )
                session.rollback()
            for assignment in (
                "reason = 'edited'",
                "owner_id = 'x'",
                "version = 99",
                "state = 'approved'",
            ):
                with pytest.raises(DBAPIError, match="final"), engine.begin() as connection:
                    connection.execute(
                        text(
                            f"UPDATE caliber_effect_proposals SET {assignment} WHERE proposal_id = :p"
                        ),
                        {"p": proposal_id},
                    )


# ---------------------------------------------------------------- compare-and-set


@pytest.mark.parametrize("dialect", DIALECTS)
def test_a_stale_read_cannot_overwrite_a_newer_state(
    dialect: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with _database(dialect, tmp_path, monkeypatch) as (_, factory):
        _seed_run(factory)
        with factory() as session:
            proposal_id = _at(session, APPROVED)
            session.commit()
        with factory() as first, factory() as second:
            read_first = proposal_state(first, proposal_id).version
            read_second = proposal_state(second, proposal_id).version
            assert read_first == read_second == 2
            transition(
                first,
                proposal_id,
                DISPATCHED,
                expected_version=read_first,
                owner_id="w-1",
                attempt=1,
                lease=leased(first, "w-1"),
                now=AT,
            )
            first.commit()
            with pytest.raises(StaleVersion) as stale:
                transition(
                    second,
                    proposal_id,
                    DISPATCHED,
                    expected_version=read_second,
                    owner_id="w-2",
                    attempt=1,
                )
            assert (stale.value.code, stale.value.expected_version) == (STALE_VERSION, 2)
            assert (stale.value.current_version, stale.value.current_state) == (3, DISPATCHED)
            with pytest.raises(StaleVersion):
                transition(second, proposal_id, FAILED, expected_version=read_second, reason="late")
            second.rollback()
        with factory() as session:
            state = proposal_state(session, proposal_id)
            assert (state.state, state.owner_id, state.version) == (DISPATCHED, "w-1", 3)


@pytest.mark.parametrize("dialect", DIALECTS)
def test_the_same_move_repeated_by_the_same_actor_is_recorded_once(
    dialect: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with _database(dialect, tmp_path, monkeypatch) as (_, factory):
        _seed_run(factory)
        with factory() as session:
            proposal_id = _at(session, APPROVED)
            version = proposal_state(session, proposal_id).version
            first = transition(
                session,
                proposal_id,
                DISPATCHED,
                expected_version=version,
                owner_id=OWNER,
                attempt=1,
                lease=leased(session, OWNER),
                now=AT,
            )
            again = transition(
                session,
                proposal_id,
                DISPATCHED,
                expected_version=version,
                owner_id=OWNER,
                attempt=1,
                lease=leased(session, OWNER),
            )
            assert (first.applied, again.applied) == (True, False)
            assert again.version == first.version == version + 1
            state = proposal_state(session, proposal_id)
            assert state.dispatched_at == NAIVE_AT, "the repeat did not rewrite the first record"
            # The same state reached with different evidence is another actor's move, not a repeat.
            with pytest.raises(StaleVersion):
                transition(
                    session,
                    proposal_id,
                    DISPATCHED,
                    expected_version=version,
                    owner_id="other",
                    attempt=1,
                )
            with pytest.raises(StaleVersion):
                transition(
                    session,
                    proposal_id,
                    DISPATCHED,
                    expected_version=version,
                    owner_id=OWNER,
                    attempt=2,
                )
            session.commit()


@pytest.mark.parametrize("dialect", DIALECTS)
def test_every_state_change_advances_the_version_by_one_and_records_its_evidence(
    dialect: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with _database(dialect, tmp_path, monkeypatch) as (_, factory):
        _seed_run(factory)
        with factory() as session:
            proposal_id = _new(session)
            assert proposal_state(session, proposal_id).version == 1
            _move(
                session,
                proposal_id,
                APPROVED,
                approval=Approval(POLICY_DECISION, _decision(session, proposal_id)),
            )
            approved = proposal_state(session, proposal_id)
            assert (approved.version, approved.approval_kind, approved.approval_ref) == (
                2,
                POLICY_DECISION,
                "pd-ok",
            )
            _move(session, proposal_id, DISPATCHED, owner_id="w-9", attempt=3)
            dispatched = proposal_state(session, proposal_id)
            assert (dispatched.version, dispatched.owner_id, dispatched.attempt) == (3, "w-9", 3)
            assert dispatched.dispatched_at == NAIVE_AT and dispatched.unknown_at is None
            _move(session, proposal_id, UNKNOWN, owner_id="w-9", attempt=3, reason="timeout")
            unknown = proposal_state(session, proposal_id)
            assert (unknown.version, unknown.reason, unknown.unknown_at) == (4, "timeout", NAIVE_AT)
            assert (unknown.owner_id, unknown.attempt) == ("w-9", 3), "attempt and owner are kept"
            _move(session, proposal_id, RECONCILED, reason="found by key")
            _move(session, proposal_id, COMMITTED, receipt_id=_receipt(session, proposal_id))
            settled = proposal_state(session, proposal_id)
            assert (settled.state, settled.version, settled.settled_at) == (COMMITTED, 6, NAIVE_AT)
            assert (settled.reason, settled.owner_id, settled.attempt) == ("found by key", "w-9", 3)
            record = read_proposal(session, proposal_id)
            assert record["state"] == COMMITTED
            session.commit()


@pytest.mark.parametrize("dialect", DIALECTS)
def test_the_timestamps_default_to_now_and_a_naive_time_is_taken_as_utc(
    dialect: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with _database(dialect, tmp_path, monkeypatch) as (_, factory):
        _seed_run(factory)
        with factory() as session:
            proposal_id = _at(session, APPROVED)
            before = datetime.now(timezone.utc).replace(tzinfo=None)
            transition(
                session,
                proposal_id,
                DISPATCHED,
                expected_version=2,
                owner_id=OWNER,
                attempt=1,
                lease=leased(session, OWNER),
            )
            stamped = proposal_state(session, proposal_id).dispatched_at
            assert stamped is not None and before <= stamped <= datetime.now(timezone.utc).replace(
                tzinfo=None
            )
            transition(
                session,
                proposal_id,
                COMMITTED,
                expected_version=3,
                owner_id=OWNER,
                attempt=1,
                receipt_id=_receipt(session, proposal_id),
                lease=leased(session, OWNER),
                now=NAIVE_AT,
            )
            assert proposal_state(session, proposal_id).settled_at == NAIVE_AT
            session.commit()


# ---------------------------------------------------------------- stale owners


@pytest.mark.parametrize("dialect", DIALECTS)
@pytest.mark.parametrize("to_state", [COMMITTED, FAILED, UNKNOWN])
def test_a_stale_owner_cannot_record_a_move_out_of_dispatched(
    dialect: str, to_state: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with _database(dialect, tmp_path, monkeypatch) as (_, factory):
        _seed_run(factory)
        with factory() as session:
            proposal_id = _at(session, DISPATCHED)
            reason = (
                {"reason": "why"}
                if to_state != COMMITTED
                else {"receipt_id": _receipt(session, proposal_id)}
            )
            for owner, attempt in (("worker-2", ATTEMPT), (OWNER, 2), ("worker-2", 2)):
                _refused(
                    STALE_OWNER,
                    session,
                    proposal_id,
                    to_state,
                    owner_id=owner,
                    attempt=attempt,
                    **reason,
                )
            _refused(EVIDENCE_REQUIRED, session, proposal_id, to_state, **reason)
            _refused(EVIDENCE_REQUIRED, session, proposal_id, to_state, owner_id=OWNER, **reason)
            state = proposal_state(session, proposal_id)
            assert (state.state, state.version, state.owner_id) == (DISPATCHED, 3, OWNER)
            assert _move(
                session, proposal_id, to_state, owner_id=OWNER, attempt=ATTEMPT, **reason
            ).applied
            session.commit()


# ---------------------------------------------------------------- evidence


@pytest.mark.parametrize("dialect", DIALECTS)
def test_a_policy_approved_read_is_recorded_approved_before_it_can_be_dispatched(
    dialect: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with _database(dialect, tmp_path, monkeypatch) as (_, factory):
        _seed_run(factory)
        with factory() as session:
            proposal_id = _new(session)
            _decision(session, proposal_id)
            _refused(
                ILLEGAL_TRANSITION, session, proposal_id, DISPATCHED, owner_id=OWNER, attempt=1
            )
            moved = _move(
                session, proposal_id, APPROVED, approval=Approval(POLICY_DECISION, "pd-ok")
            )
            assert moved.state == APPROVED
            assert (
                _move(session, proposal_id, DISPATCHED, owner_id=OWNER, attempt=1).state
                == DISPATCHED
            )
            session.commit()


@pytest.mark.parametrize("dialect", DIALECTS)
def test_an_approval_that_is_not_an_allow_for_this_proposal_is_refused(
    dialect: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with _database(dialect, tmp_path, monkeypatch) as (_, factory):
        _seed_run(factory)
        with factory() as session:
            proposal_id = _new(session)
            other_id = _new(session, occurrence="occ-2")
            _decision(session, proposal_id, decision_id="pd-deny", result="deny")
            _decision(session, proposal_id, decision_id="pd-action", action="context.read")
            _decision(session, proposal_id, decision_id="pd-other-run", run="RUN-other")
            _decision(
                session, proposal_id, decision_id="pd-resource", resource="effect:somebody-else"
            )
            _decision(session, other_id, decision_id="pd-for-other")
            for ref in (
                "pd-deny",
                "pd-action",
                "pd-other-run",
                "pd-resource",
                "pd-for-other",
                "pd-missing",
            ):
                _refused(
                    APPROVAL_REFUSED,
                    session,
                    proposal_id,
                    APPROVED,
                    approval=Approval(POLICY_DECISION, ref),
                )
            _refused(
                APPROVAL_REFUSED,
                session,
                proposal_id,
                APPROVED,
                approval=Approval("approval", "apr-1"),
            )
            _refused(APPROVAL_REFUSED, session, proposal_id, APPROVED, approval=Approval("", "x"))
            _refused(
                BAD_FIELD, session, proposal_id, APPROVED, approval=Approval(POLICY_DECISION, "")
            )
            _refused(EVIDENCE_REQUIRED, session, proposal_id, APPROVED)
            assert proposal_state(session, proposal_id).state == PROPOSED


@pytest.mark.parametrize("dialect", DIALECTS)
@pytest.mark.parametrize("effect_class", ["reversible_write", "irreversible_write", "export"])
def test_only_a_read_can_be_approved_by_a_policy_decision_until_exact_intent_approvals_exist(
    dialect: str, effect_class: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with _database(dialect, tmp_path, monkeypatch) as (_, factory):
        _seed_run(factory)
        with factory() as session:
            proposal_id = _new(session, effect_class=effect_class)
            _decision(session, proposal_id)
            refused = _refused(
                APPROVAL_REFUSED,
                session,
                proposal_id,
                APPROVED,
                approval=Approval(POLICY_DECISION, "pd-ok"),
            )
            assert effect_class in str(refused) and "B06" in str(refused)
            assert proposal_state(session, proposal_id).state == PROPOSED


@pytest.mark.parametrize("dialect", DIALECTS)
def test_each_move_needs_its_own_evidence_and_refuses_evidence_that_is_not_its_own(
    dialect: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with _database(dialect, tmp_path, monkeypatch) as (_, factory):
        _seed_run(factory)
        with factory() as session:
            proposed = _at(session, PROPOSED)
            approved = _at(session, APPROVED, occurrence="o-a")
            dispatched = _at(session, DISPATCHED, occurrence="o-d")
            unknown = _at(session, UNKNOWN, occurrence="o-u")
            reconciled = _at(session, RECONCILED, occurrence="o-r")
            # a reason is required to fail, become unknown or be reconciled
            _refused(EVIDENCE_REQUIRED, session, proposed, FAILED)
            _refused(EVIDENCE_REQUIRED, session, approved, FAILED)
            _refused(EVIDENCE_REQUIRED, session, dispatched, FAILED, owner_id=OWNER, attempt=1)
            _refused(EVIDENCE_REQUIRED, session, dispatched, UNKNOWN, owner_id=OWNER, attempt=1)
            _refused(EVIDENCE_REQUIRED, session, unknown, RECONCILED)
            _refused(EVIDENCE_REQUIRED, session, reconciled, FAILED)
            # dispatching needs an owner and an attempt
            _refused(EVIDENCE_REQUIRED, session, approved, DISPATCHED)
            _refused(EVIDENCE_REQUIRED, session, approved, DISPATCHED, owner_id=OWNER)
            _refused(EVIDENCE_REQUIRED, session, approved, DISPATCHED, attempt=1)
            # evidence that belongs to no such move
            decision = Approval(POLICY_DECISION, "pd-x")
            _refused(
                BAD_FIELD,
                session,
                approved,
                DISPATCHED,
                owner_id=OWNER,
                attempt=1,
                approval=decision,
            )
            _refused(
                BAD_FIELD,
                session,
                approved,
                DISPATCHED,
                owner_id=OWNER,
                attempt=1,
                reason="because",
            )
            _refused(BAD_FIELD, session, proposed, FAILED, reason="r", owner_id=OWNER)
            _refused(BAD_FIELD, session, proposed, FAILED, reason="r", attempt=1)
            _refused(BAD_FIELD, session, reconciled, COMMITTED, approval=decision)
            _refused(
                BAD_FIELD,
                session,
                dispatched,
                COMMITTED,
                owner_id=OWNER,
                attempt=1,
                reason=None,
                approval=decision,
            )
            # a reason on a commit is allowed (and kept)
            assert _move(
                session,
                reconciled,
                COMMITTED,
                reason="verified",
                receipt_id=_receipt(session, reconciled),
            ).applied
            assert proposal_state(session, reconciled).reason == "verified"
            session.rollback()


@pytest.mark.parametrize("dialect", DIALECTS)
@pytest.mark.parametrize(
    "bad",
    [
        {"expected_version": 0},
        {"expected_version": -1},
        {"expected_version": True},
        {"expected_version": "1"},
        {"expected_version": 1.0},
        {"attempt": 0},
        {"attempt": True},
        {"attempt": "1"},
        {"owner_id": ""},
        {"owner_id": "w" * 129},
        {"owner_id": "a\x00b"},
        {"reason": "r" * 129},
        {"reason": "a\nb"},
        {"to_state": "done"},
        {"to_state": ""},
        {"to_state": None},
    ],
)
def test_malformed_arguments_are_refused_before_anything_is_read_or_written(
    dialect: str, bad: dict[str, Any], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with _database(dialect, tmp_path, monkeypatch) as (_, factory):
        _seed_run(factory)
        with factory() as session:
            proposal_id = _at(session, APPROVED)
            arguments: dict[str, Any] = {
                "to_state": DISPATCHED,
                "expected_version": 2,
                "owner_id": OWNER,
                "attempt": 1,
            }
            arguments.update(bad)
            to_state = arguments.pop("to_state")
            with pytest.raises(ProposalError) as refused:
                transition(session, proposal_id, to_state, **arguments)
            assert refused.value.code == BAD_FIELD
            state = proposal_state(session, proposal_id)
            assert (state.state, state.version) == (APPROVED, 2)


@pytest.mark.parametrize("dialect", DIALECTS)
def test_an_unknown_proposal_is_a_key_error(
    dialect: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with _database(dialect, tmp_path, monkeypatch) as (_, factory), factory() as session:
        with pytest.raises(KeyError):
            proposal_state(session, "prop-missing")
        with pytest.raises(KeyError):
            transition(session, "prop-missing", APPROVED, expected_version=1)


@pytest.mark.parametrize("dialect", DIALECTS)
def test_unknown_can_only_be_resolved_by_asking_never_by_dispatching_again(
    dialect: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with _database(dialect, tmp_path, monkeypatch) as (_, factory):
        _seed_run(factory)
        with factory() as session:
            proposal_id = _at(session, UNKNOWN)
            for target, extra in (
                (DISPATCHED, {"owner_id": "w-2", "attempt": 2}),
                (COMMITTED, {}),
                (FAILED, {"reason": "404"}),
                (APPROVED, {}),
                (UNKNOWN, {"reason": "again"}),
            ):
                _refused(ILLEGAL_TRANSITION, session, proposal_id, target, **extra)
            assert _move(session, proposal_id, RECONCILED, reason="found by key").applied
            assert _move(
                session, proposal_id, FAILED, reason="the downstream proved it never committed"
            ).applied
            session.commit()


# ---------------------------------------------------------------- the database enforces the table too


@pytest.mark.parametrize("dialect", DIALECTS)
def test_raw_sql_cannot_make_a_move_the_application_would_refuse(
    dialect: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with _database(dialect, tmp_path, monkeypatch) as (engine, factory):
        _seed_run(factory)
        with factory() as session:
            proposal_id = _at(session, DISPATCHED)
            session.commit()

        def sql(statement: str) -> None:
            with engine.begin() as connection:
                connection.execute(
                    text(f"UPDATE caliber_effect_proposals SET {statement} WHERE proposal_id = :p"),
                    {"p": proposal_id},
                )

        for bad, match in (
            ("state = 'proposed', version = 4", "illegal"),  # backwards
            ("state = 'reconciled', version = 4, reason = 'r'", "illegal"),  # a skip
            (
                "state = 'approved', version = 4, approval_kind = 'policy_decision', approval_ref = 'x'",
                "illegal",
            ),
            (
                "state = 'unknown', unknown_at = CURRENT_TIMESTAMP, reason = 'r'",
                "version",
            ),  # does not advance the version
            (
                "state = 'unknown', version = 5, unknown_at = CURRENT_TIMESTAMP, reason = 'r'",
                "version",
            ),  # advances by two
            (
                "state = 'unknown', version = 3, unknown_at = CURRENT_TIMESTAMP, reason = 'r'",
                "version",
            ),
            (
                "state = 'committed', version = 4",
                "evidence|needs a receipt",
            ),  # a commit that records no time
            (
                "state = 'failed', version = 4, settled_at = CURRENT_TIMESTAMP",
                "evidence",
            ),  # a failure with no reason
            (
                "state = 'unknown', version = 4, reason = 'timeout'",
                "evidence",
            ),  # unknown with no time
            ("owner_id = 'hijacker'", "evidence"),  # evidence moved without a state change
            ("attempt = 7", "evidence"),
            ("reason = 'edited'", "evidence"),
            ("version = 4", "evidence"),
            ("dispatched_at = CURRENT_TIMESTAMP", "evidence"),
            ("unknown_at = CURRENT_TIMESTAMP", "evidence"),
            ("settled_at = CURRENT_TIMESTAMP", "evidence"),
            ("approval_kind = 'approval'", "evidence"),
            ("approval_ref = 'pd-forged'", "evidence"),
        ):
            with pytest.raises(DBAPIError, match=match):
                sql(bad)
        sql("state = 'unknown', version = 4, unknown_at = CURRENT_TIMESTAMP, reason = 'timeout'")
        with factory() as session:
            state = proposal_state(session, proposal_id)
            assert (state.state, state.version, state.owner_id) == (UNKNOWN, 4, OWNER)
        with pytest.raises(DBAPIError, match="illegal"):
            sql("state = 'dispatched', version = 5")


@pytest.mark.parametrize("dialect", DIALECTS)
def test_a_no_op_update_of_the_mutable_columns_is_allowed_and_the_intent_stays_frozen(
    dialect: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with _database(dialect, tmp_path, monkeypatch) as (engine, factory):
        _seed_run(factory)
        with factory() as session:
            proposal_id = _at(session, APPROVED)
            session.commit()
        with engine.begin() as connection:
            connection.execute(
                text(
                    "UPDATE caliber_effect_proposals SET state = state, version = version WHERE proposal_id = :p"
                ),
                {"p": proposal_id},
            )
        with pytest.raises(DBAPIError, match="immutable"), engine.begin() as connection:
            connection.execute(
                text(
                    "UPDATE caliber_effect_proposals SET connector = 'x', state = 'dispatched', version = 3, "
                    "owner_id = 'w', attempt = 1, dispatched_at = CURRENT_TIMESTAMP WHERE proposal_id = :p"
                ),
                {"p": proposal_id},
            )


@pytest.mark.parametrize("dialect", DIALECTS)
def test_the_orm_still_refuses_to_edit_the_intent_and_bulk_updates(
    dialect: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with _database(dialect, tmp_path, monkeypatch) as (_, factory):
        _seed_run(factory)
        with factory() as session:
            proposal_id = _at(session, APPROVED)
            session.commit()
            row = session.get(CaliberEffectProposal, proposal_id)
            assert row is not None
            row.connector = "other"
            with pytest.raises(ImmutableRecordError):
                session.flush()
            session.rollback()
            # a bulk ORM update is refused whatever it sets; the compare-and-set uses a Core update instead
            with pytest.raises(ImmutableRecordError):
                session.execute(
                    update(CaliberEffectProposal)
                    .where(CaliberEffectProposal.proposal_id == proposal_id)
                    .values(state="dispatched")
                )
            session.rollback()
            assert proposal_state(session, proposal_id).state == APPROVED


@pytest.mark.parametrize("dialect", DIALECTS)
def test_the_migration_rolls_back_to_0123_and_forward_again(
    dialect: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with _database(dialect, tmp_path, monkeypatch) as (engine, factory):
        _seed_run(factory)
        with factory() as session:
            _new(session)
            session.commit()
        columns = {c["name"] for c in inspect(engine).get_columns("caliber_effect_proposals")}
        assert {"version", "attempt", "owner_id", "reason", "approval_ref"} <= columns
        command.downgrade(Config(str(ALEMBIC_INI)), "0123")
        columns = {c["name"] for c in inspect(engine).get_columns("caliber_effect_proposals")}
        assert (
            not {"version", "attempt", "owner_id", "dispatched_at", "reason", "approval_kind"}
            & columns
        )
        with pytest.raises(DBAPIError, match="DELETE refused"), engine.begin() as connection:
            connection.execute(
                text("DELETE FROM caliber_effect_proposals")
            )  # 0123's triggers survive
        command.upgrade(Config(str(ALEMBIC_INI)), "head")
        columns = {c["name"] for c in inspect(engine).get_columns("caliber_effect_proposals")}
        assert {"version", "attempt", "owner_id", "reason", "approval_ref"} <= columns


@pytest.mark.parametrize("dialect", DIALECTS)
def test_raw_sql_cannot_dispatch_without_an_owner_or_approve_without_an_approval(
    dialect: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with _database(dialect, tmp_path, monkeypatch) as (engine, factory):
        _seed_run(factory)
        with factory() as session:
            proposed = _at(session, PROPOSED)
            approved = _at(session, APPROVED, occurrence="o-a")
            reconciled = _at(session, RECONCILED, occurrence="o-r")
            session.commit()

        def sql(proposal_id: str, statement: str) -> None:
            with engine.begin() as connection:
                connection.execute(
                    text(f"UPDATE caliber_effect_proposals SET {statement} WHERE proposal_id = :p"),
                    {"p": proposal_id},
                )

        with pytest.raises(DBAPIError, match="evidence"):
            sql(proposed, "state = 'approved', version = 2")
        with pytest.raises(DBAPIError, match="evidence"):
            sql(
                approved, "state = 'dispatched', version = 3, dispatched_at = CURRENT_TIMESTAMP"
            )  # no owner
        with pytest.raises(DBAPIError, match="evidence"):
            sql(
                approved, "state = 'dispatched', version = 3, owner_id = 'w', attempt = 1"
            )  # no time
        with pytest.raises(DBAPIError, match="evidence"):
            sql(
                reconciled,
                "state = 'failed', version = 6, settled_at = CURRENT_TIMESTAMP, reason = NULL",
            )
        sql(
            approved,
            "state = 'dispatched', version = 3, owner_id = 'w', attempt = 1, dispatched_at = CURRENT_TIMESTAMP",
        )


@pytest.mark.parametrize("dialect", DIALECTS)
def test_the_orm_cannot_move_a_proposal_at_all(
    dialect: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with _database(dialect, tmp_path, monkeypatch) as (_, factory):
        _seed_run(factory)
        with factory() as session:
            proposal_id = _at(session, APPROVED)
            session.commit()
            row = session.get(CaliberEffectProposal, proposal_id)
            assert row is not None
            row.state = COMMITTED
            row.version += 1
            with pytest.raises(ImmutableRecordError):
                session.flush()
            session.rollback()
            assert proposal_state(session, proposal_id).state == APPROVED


def test_the_triggers_and_the_model_agree_on_which_columns_move_with_the_state() -> None:
    from caliber.db.models import CaliberEffectProposal as Model

    def migration(name: str) -> Any:
        path = (
            Path(__file__).resolve().parent.parent / f"src/caliber/db/migrations/versions/{name}.py"
        )
        spec = importlib.util.spec_from_file_location(name, path)
        assert spec is not None and spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module

    frozen = set(migration("0123_effect_proposals")._FROZEN)
    moving = set(migration("0125_effect_receipts")._EVIDENCE_0125) | {"state"}
    assert frozen.isdisjoint(moving)
    assert {c.name for c in Model.__table__.columns} == frozen | moving, (
        "every column is frozen or moves with state"
    )
    required = migration("0125_effect_receipts")._REQUIRED_0125
    assert set(required) <= set(STATES) and all(set(cols) <= moving for cols in required.values())


@pytest.mark.parametrize("dialect", DIALECTS)
@pytest.mark.parametrize(
    ("to_state", "prepare", "offered"),
    [
        (COMMITTED, DISPATCHED, {}),  # no owner or attempt
        (COMMITTED, DISPATCHED, {"owner_id": OWNER}),
        (FAILED, DISPATCHED, {"reason": "because"}),  # a dispatched failure needs the owner too
        (UNKNOWN, DISPATCHED, {"reason": "because"}),
        (FAILED, PROPOSED, {}),  # no reason
        (RECONCILED, UNKNOWN, {}),
        (APPROVED, PROPOSED, {}),  # no approval
        (DISPATCHED, APPROVED, {"owner_id": OWNER}),
    ],
)
def test_an_actor_that_cannot_present_the_evidence_is_never_told_the_move_was_already_recorded(
    dialect: str,
    to_state: str,
    prepare: str,
    offered: dict[str, Any],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Omitted evidence is not a wildcard: only a caller that can say who it is recognises its own repeat."""
    with _database(dialect, tmp_path, monkeypatch) as (_, factory):
        _seed_run(factory)
        with factory() as session:
            proposal_id = _at(session, prepare)
            before = proposal_state(session, proposal_id).version
            full = _evidence_for(session, proposal_id, prepare, to_state)
            _move(session, proposal_id, to_state, **full)
            with pytest.raises(ProposalError) as refused:
                transition(session, proposal_id, to_state, expected_version=before, **offered)
            assert refused.value.code in (
                EVIDENCE_REQUIRED,
                STALE_VERSION,
                APPROVAL_REFUSED,
                BAD_FIELD,
            )
            if "owner_id" in full:  # the owner's repeat is fenced too (B05.5a)
                full["lease"] = leased(session, OWNER)
            assert (
                transition(session, proposal_id, to_state, expected_version=before, **full).applied
                is False
            )


@pytest.mark.parametrize("dialect", DIALECTS)
@pytest.mark.parametrize("attempt", [2**31, 2**40, 2**70])
def test_an_attempt_number_beyond_the_column_is_a_refusal_not_a_database_error(
    dialect: str, attempt: int, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with _database(dialect, tmp_path, monkeypatch) as (_, factory):
        _seed_run(factory)
        with factory() as session:
            proposal_id = _at(session, APPROVED)
            refused = _refused(
                BAD_FIELD, session, proposal_id, DISPATCHED, owner_id=OWNER, attempt=attempt
            )
            assert str(records.MAX_ATTEMPT) in str(refused)
            assert _move(
                session, proposal_id, DISPATCHED, owner_id=OWNER, attempt=records.MAX_ATTEMPT
            ).applied
            session.commit()


@pytest.mark.parametrize("dialect", DIALECTS)
@pytest.mark.parametrize("same_move", [True, False])
def test_a_move_that_loses_the_update_race_is_classified_from_what_won(
    dialect: str, same_move: bool, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The other transaction's move lands between this one's read and its UPDATE (arranged inside the same transaction
    so it is deterministic on both dialects): the UPDATE matches no row, and the outcome is a repeat or a stale version."""
    with _database(dialect, tmp_path, monkeypatch) as (_, factory):
        _seed_run(factory)
        with factory() as session:
            proposal_id = _at(session, APPROVED)
            session.commit()
            real_execute = session.execute
            state = {"fired": False}

            def racing(statement: Any, *args: Any, **kwargs: Any) -> Any:
                if (
                    not state["fired"]
                    and "UPDATE" in str(statement).upper()
                    and "caliber_effect_proposals" in str(statement)
                ):
                    state["fired"] = True
                    table = CaliberEffectProposal.__table__
                    real_execute(
                        update(table)
                        .where(table.c.proposal_id == proposal_id)
                        .values(
                            state=DISPATCHED,
                            version=3,
                            owner_id=OWNER if same_move else "winner",
                            attempt=1,
                            dispatched_at=NAIVE_AT,
                        )
                    )
                return real_execute(statement, *args, **kwargs)

            monkeypatch.setattr(session, "execute", racing)
            if same_move:
                lost = transition(
                    session,
                    proposal_id,
                    DISPATCHED,
                    expected_version=2,
                    owner_id=OWNER,
                    attempt=1,
                    lease=leased(session, OWNER),
                )
                assert (lost.applied, lost.version) == (False, 3)
            else:
                with pytest.raises(StaleVersion) as stale:
                    transition(
                        session,
                        proposal_id,
                        DISPATCHED,
                        expected_version=2,
                        owner_id=OWNER,
                        attempt=1,
                        lease=leased(session, OWNER),
                    )
                assert (stale.value.current_version, stale.value.current_state) == (3, DISPATCHED)
            assert state["fired"]


# ---------------------------------------------------------------- fencing by the captured lease (B05.5a, #734)


def _count_receipt_rows(session: Session) -> int:
    return len(session.execute(select(CaliberEffectReceipt.receipt_id)).all())


def _taken_over(session: Session, stale: Lease, by: str = "worker-new") -> Lease:
    """The lease lapses and ``by`` acquires it: a new token, so everything ``stale`` captured is no longer the row's."""
    assert release(session, stale)
    taken = acquire(session, by, ttl_seconds=TTL_SECONDS)
    assert taken is not None and taken.fencing_token == stale.fencing_token + 1
    return taken


def _settle_with(session: Session, proposal_id: str, lease: Lease, outcome: Any) -> Any:
    queried: list[bool] = []

    def verify() -> ReceiptFacts | None:
        queried.append(True)
        view = proposal_view(session, proposal_id)
        return ReceiptFacts(
            attempt=view.attempt or 1,
            idempotency_key=view.logical_key,
            connector=view.connector,
            connector_version=view.connector_version,
            resource=view.resource,
            intent_hash=view.intent_hash,
            downstream_request_id=f"DR-{proposal_id}",
            response_digest="sha256:" + "a" * 64,
            resource_version="2",
        )

    try:
        return settle_dispatch(
            session,
            proposal_id,
            expected_version=proposal_state(session, proposal_id).version,
            owner_id=OWNER,
            attempt=ATTEMPT,
            lease=lease,
            outcome=outcome,
            verify=verify,
            now=AT,
        )
    finally:
        session.info["queried"] = len(queried)


_CLAIM = Classified(TransportOutcome.COMMITTED_RECEIPT, "http_201", 201, 1)
_AMBIGUOUS = Classified(TransportOutcome.AMBIGUOUS, "timeout", None, 1)
_REJECTED = Classified(TransportOutcome.DEFINITIVE_REJECTION, "http_422", 422, 1)
_NOT_SENT = Classified(TransportOutcome.PROVEN_NOT_SENT, "refused", None, 1)


@pytest.mark.parametrize("dialect", DIALECTS)
@pytest.mark.parametrize(
    ("outcome", "what"),
    [
        (_CLAIM, "commit"),
        (_AMBIGUOUS, "mark unknown"),
        (_REJECTED, "fail"),
        (_NOT_SENT, "fail (not sent)"),
    ],
)
def test_stale_fencing_token_leaves_unknown(
    dialect: str, outcome: Classified, what: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A worker whose lease was taken over cannot ``what``: nothing is recorded, not even ``unknown``, and its receipt is
    rolled back with the refused move. The attempt stays ``dispatched`` and what the downstream did is left unknown to
    the platform until the reconciler (B05.5b) asks it by key under the new lease."""
    with _database(dialect, tmp_path, monkeypatch) as (_, factory):
        _seed_run(factory)
        with factory() as session:
            proposal_id = _at(session, DISPATCHED)
            captured = leased(session, OWNER)
            before = proposal_state(session, proposal_id)
            _taken_over(session, captured)
            with pytest.raises(ProposalError) as refused:
                _settle_with(session, proposal_id, captured, outcome)
            assert refused.value.code == LEASE_LOST, what
            after = proposal_state(session, proposal_id)
            assert (after.state, after.version, after.owner_id, after.attempt) == (
                DISPATCHED,
                before.version,
                OWNER,
                ATTEMPT,
            )
            assert after.reason is None and after.receipt_id is None and after.unknown_at is None
            assert _count_receipt_rows(session) == 0, "the receipt went with the refused move"
            session.commit()
        with factory() as session:
            assert proposal_state(session, proposal_id).state == DISPATCHED


@pytest.mark.parametrize("dialect", DIALECTS)
def test_a_lease_that_is_not_the_rows_is_lease_lost_whatever_the_reason(
    dialect: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Expired (by the database's clock), taken over, a token nobody was issued yet, another lease name: each is refused,
    for the dispatch and for the way out of it, and each leaves the proposal exactly where it was."""
    with _database(dialect, tmp_path, monkeypatch) as (_, factory):
        _seed_run(factory)
        with factory() as session:
            approved = _at(session, APPROVED, occurrence="o-approved")
            dispatched = _at(session, DISPATCHED, occurrence="o-dispatched")
            held = leased(session, OWNER)
            expired = Lease(held.name, OWNER, held.fencing_token, held.expires_at)
            assert release(session, held), "released: expired by the database's own clock"
            lapsed = _refused(
                LEASE_LOST,
                session,
                approved,
                DISPATCHED,
                owner_id=OWNER,
                attempt=1,
                lease=expired,
            )
            assert "nothing recorded" in str(lapsed)
            fresh = leased(session, OWNER)
            assert fresh.fencing_token == held.fencing_token + 1
            other_name = acquire(session, OWNER, ttl_seconds=TTL_SECONDS, name="some-other-lease")
            assert other_name is not None, "a real, held lease, but not the execution-owner lease"
            for label, lease in {
                "an old token": expired,
                "a future token": Lease(
                    fresh.name, OWNER, fresh.fencing_token + 1, fresh.expires_at
                ),
                "another lease name": other_name,
                "another owner's": Lease(
                    fresh.name, "worker-other", fresh.fencing_token, fresh.expires_at
                ),
            }.items():
                for target, evidence in (
                    (approved, {"to": DISPATCHED}),
                    (dispatched, {"to": UNKNOWN, "reason": "timeout"}),
                    (dispatched, {"to": FAILED, "reason": "rejected"}),
                ):
                    kwargs = dict(evidence)
                    to = kwargs.pop("to")
                    # An owner mismatch is refused as a stale owner; every other lease is not the row's.
                    code = STALE_OWNER if label == "another owner's" else LEASE_LOST
                    _refused(
                        code,
                        session,
                        target,
                        to,
                        owner_id=OWNER,
                        attempt=ATTEMPT if target == dispatched else 1,
                        lease=lease,
                        **kwargs,
                    )
            for target, state in ((approved, APPROVED), (dispatched, DISPATCHED)):
                assert proposal_state(session, target).state == state
            moved = transition(
                session,
                approved,
                DISPATCHED,
                expected_version=proposal_state(session, approved).version,
                owner_id=OWNER,
                attempt=1,
                lease=fresh,
                now=AT,
            )
            assert moved.applied, "and the exact, held lease does move it"
            session.commit()


@pytest.mark.parametrize("dialect", DIALECTS)
def test_a_move_into_or_out_of_dispatched_needs_the_lease(
    dialect: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with _database(dialect, tmp_path, monkeypatch) as (_, factory):
        _seed_run(factory)
        with factory() as session:
            approved = _at(session, APPROVED, occurrence="o-a")
            dispatched = _at(session, DISPATCHED, occurrence="o-d")
            _refused(EVIDENCE_REQUIRED, session, approved, DISPATCHED, owner_id=OWNER, attempt=1)
            for to in (FAILED, UNKNOWN):
                _refused(
                    EVIDENCE_REQUIRED,
                    session,
                    dispatched,
                    to,
                    owner_id=OWNER,
                    attempt=ATTEMPT,
                    reason="r",
                )
            _refused(
                EVIDENCE_REQUIRED,
                session,
                dispatched,
                COMMITTED,
                owner_id=OWNER,
                attempt=ATTEMPT,
                receipt_id=_receipt(session, dispatched),
            )
            assert proposal_state(session, approved).state == APPROVED
            assert proposal_state(session, dispatched).state == DISPATCHED
            # Moves that are not the owner's keep needing no lease.
            _move(session, dispatched, UNKNOWN, owner_id=OWNER, attempt=ATTEMPT, reason="timeout")
            assert _move(session, dispatched, RECONCILED, reason="found by key").applied
            session.commit()


@pytest.mark.parametrize("dialect", DIALECTS)
def test_the_new_owner_cannot_settle_what_the_previous_one_dispatched(
    dialect: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A valid lease is not a licence to settle another owner's attempt: that is the reconciler's question to the
    downstream. The old owner, with the lease it captured, is refused as well."""
    with _database(dialect, tmp_path, monkeypatch) as (_, factory):
        _seed_run(factory)
        with factory() as session:
            proposal_id = _at(session, DISPATCHED)
            old = leased(session, OWNER)
            new = _taken_over(session, old)
            version = proposal_state(session, proposal_id).version
            with pytest.raises(ProposalError) as other_owner:
                settle_dispatch(
                    session,
                    proposal_id,
                    expected_version=version,
                    owner_id=new.owner_id,
                    attempt=ATTEMPT,
                    lease=new,
                    outcome=_CLAIM,
                    verify=lambda: pytest.fail("a settle by another owner must not query"),
                )
            assert other_owner.value.code == STALE_OWNER
            with pytest.raises(
                ProposalError
            ) as borrowed:  # the dispatcher's name on someone else's lease
                settle_dispatch(
                    session,
                    proposal_id,
                    expected_version=version,
                    owner_id=OWNER,
                    attempt=ATTEMPT,
                    lease=new,
                    outcome=_CLAIM,
                    verify=lambda: pytest.fail(
                        "a settle under another owner's lease must not query"
                    ),
                )
            assert borrowed.value.code == STALE_OWNER
            with pytest.raises(ProposalError) as old_owner:
                _settle_with(session, proposal_id, old, _CLAIM)
            assert old_owner.value.code == LEASE_LOST
            assert proposal_state(session, proposal_id).state == DISPATCHED
            assert session.info["queried"] == 1, (
                "the stale owner's query is read-only and records nothing"
            )
            assert _count_receipt_rows(session) == 0
            session.commit()


@pytest.mark.parametrize("dialect", DIALECTS)
def test_a_repeat_of_the_owners_move_is_fenced_too(
    dialect: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``applied=False`` tells a worker "recorded, this attempt is yours": a worker that lost its lease must not hear it."""
    with _database(dialect, tmp_path, monkeypatch) as (_, factory):
        _seed_run(factory)
        with factory() as session:
            proposal_id = _at(session, APPROVED)
            held = leased(session, OWNER)
            version = proposal_state(session, proposal_id).version
            kwargs: dict[str, Any] = {"expected_version": version, "owner_id": OWNER, "attempt": 1}
            assert transition(
                session, proposal_id, DISPATCHED, lease=held, now=AT, **kwargs
            ).applied
            assert not transition(session, proposal_id, DISPATCHED, lease=held, **kwargs).applied
            with pytest.raises(ProposalError) as no_lease:
                transition(session, proposal_id, DISPATCHED, **kwargs)
            assert no_lease.value.code == EVIDENCE_REQUIRED
            _taken_over(session, held)
            with pytest.raises(ProposalError) as stale:
                transition(session, proposal_id, DISPATCHED, lease=held, **kwargs)
            assert stale.value.code == LEASE_LOST, "the repeat of the dispatch under a lapsed lease"
            # and the repeat of a settle: the owner settled, lost the lease, and asks again
            current = leased(session, "worker-new")
            assert current.owner_id == "worker-new"
            settled = _at(session, DISPATCHED, occurrence="o-settled")
            owner_lease = leased(session, OWNER)
            first = _move(
                session, settled, UNKNOWN, owner_id=OWNER, attempt=ATTEMPT, reason="timeout"
            )
            assert first.applied
            _taken_over(session, owner_lease)
            with pytest.raises(ProposalError) as repeat:
                transition(
                    session,
                    settled,
                    UNKNOWN,
                    expected_version=first.version - 1,
                    owner_id=OWNER,
                    attempt=ATTEMPT,
                    reason="timeout",
                    lease=owner_lease,
                )
            assert repeat.value.code == LEASE_LOST
            # a move that is not the owner's keeps needing no lease, repeated or not
            assert _move(session, settled, RECONCILED, reason="found by key").applied
            session.commit()


@pytest.mark.parametrize("dialect", DIALECTS)
def test_a_fence_that_cannot_be_asked_is_not_lease_lost_and_a_lease_is_checked_before_the_receipt(
    dialect: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from sqlalchemy.exc import OperationalError

    from caliber.effects import receipts as receipts_module
    from caliber.ownership import LeaseLost

    with _database(dialect, tmp_path, monkeypatch) as (_, factory):
        _seed_run(factory)
        with factory() as session:
            proposal_id = _at(session, DISPATCHED)
            held = leased(session, OWNER)
            version = proposal_state(session, proposal_id).version

            def broken(*args: Any, **kwargs: Any) -> None:
                raise LeaseLost("the lease store is unavailable") from OperationalError(
                    "s", {}, Exception("down")
                )

            monkeypatch.setattr(records, "assert_fenced", broken)
            refused = _refused(
                records.STORE_FAILED,
                session,
                proposal_id,
                UNKNOWN,
                owner_id=OWNER,
                attempt=ATTEMPT,
                reason="r",
                lease=held,
            )
            assert "retry" in str(refused)
            monkeypatch.undo()
            with pytest.raises(ProposalError) as not_a_lease:
                transition(
                    session,
                    proposal_id,
                    UNKNOWN,
                    expected_version=version,
                    owner_id=OWNER,
                    attempt=ATTEMPT,
                    reason="r",
                    lease="a lease",  # type: ignore[arg-type]
                )
            assert not_a_lease.value.code == BAD_FIELD
            _taken_over(session, held)

            def no_receipt(*args: Any, **kwargs: Any) -> None:
                pytest.fail("a stale worker must be refused before it records a receipt")

            monkeypatch.setattr(receipts_module, "record_receipt", no_receipt)
            with pytest.raises(ProposalError) as stale:
                _settle_with(session, proposal_id, held, _CLAIM)
            assert stale.value.code == LEASE_LOST


@pytest.mark.parametrize("dialect", DIALECTS)
def test_a_settle_under_the_exact_held_lease_commits_with_its_receipt(
    dialect: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with _database(dialect, tmp_path, monkeypatch) as (_, factory):
        _seed_run(factory)
        with factory() as session:
            proposal_id = _at(session, DISPATCHED)
            held = leased(session, OWNER)
            settled = _settle_with(session, proposal_id, held, _CLAIM)
            assert settled.state == COMMITTED and settled.receipt_id and settled.applied
            assert _count_receipt_rows(session) == 1
            session.commit()


# ---------------------------------------------------------------- concurrency (PostgreSQL)


def _need_postgresql() -> None:
    if not POSTGRES_TEST_URL:
        pytest.skip("the race needs PostgreSQL")


def _race(
    factory: sessionmaker[Session],
    proposal_id: str,
    workers: int,
    owner: Any,
    lease: Any,
) -> list[str]:
    barrier = threading.Barrier(workers)
    outcomes: list[str] = []
    errors: list[BaseException] = []
    lock = threading.Lock()

    def run(index: int) -> None:
        try:
            with factory() as session:
                version = proposal_state(session, proposal_id).version
                barrier.wait(10)
                try:
                    moved = transition(
                        session,
                        proposal_id,
                        DISPATCHED,
                        expected_version=version,
                        owner_id=owner(index),
                        attempt=1,
                        lease=lease(index),
                    )
                    session.commit()
                    outcome = "applied" if moved.applied else "duplicate"
                except StaleVersion:
                    session.rollback()
                    outcome = "stale"
                except ProposalError as exc:
                    session.rollback()
                    outcome = exc.code
            with lock:
                outcomes.append(outcome)
        except BaseException as exc:
            with lock:
                errors.append(exc)

    threads = [threading.Thread(target=run, args=(i,), daemon=True) for i in range(workers)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(60)
    assert not errors, errors
    return outcomes


def test_concurrent_dispatchers_leave_exactly_one_holder_of_the_lease_the_winner_on_postgresql(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _need_postgresql()
    with _database("postgresql", tmp_path, monkeypatch) as (_, factory):
        _seed_run(factory)
        with factory() as session:
            proposal_id = _at(session, APPROVED)
            session.commit()
        with factory() as session:
            held = leased(session, "worker-0")
            session.commit()

        def lease(
            index: int,
        ) -> Lease:  # worker-0 holds it; the others captured a lease that is not the row's
            return (
                held
                if index == 0
                else Lease(
                    held.name, f"worker-{index}", held.fencing_token + index, held.expires_at
                )
            )

        outcomes = _race(factory, proposal_id, 8, lambda index: f"worker-{index}", lease)
        assert sorted(outcomes) == ["applied"] + [LEASE_LOST] * 7
        with factory() as session:
            state = proposal_state(session, proposal_id)
            assert (state.state, state.version, state.attempt) == (DISPATCHED, 3, 1)
            assert state.owner_id == "worker-0"


def test_concurrent_repeats_of_one_move_apply_it_once_on_postgresql(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _need_postgresql()
    with _database("postgresql", tmp_path, monkeypatch) as (_, factory):
        _seed_run(factory)
        with factory() as session:
            proposal_id = _at(session, APPROVED)
            session.commit()
        with factory() as session:
            held = leased(session, "same-worker")
            session.commit()
        outcomes = _race(factory, proposal_id, 8, lambda index: "same-worker", lambda index: held)
        assert sorted(outcomes) == ["applied"] + ["duplicate"] * 7
        with factory() as session:
            assert proposal_state(session, proposal_id).version == 3


def test_a_takeover_waits_for_a_settle_in_flight_and_then_the_old_lease_is_refused_on_postgresql(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The settle holds the lease row until its transaction ends: a takeover cannot overtake it, so the work is done under
    a lease that was valid or not done at all. (``lock_timeout`` stands in for waiting, so nothing sleeps.)"""
    _need_postgresql()
    from caliber.ownership import LeaseLost

    with _database("postgresql", tmp_path, monkeypatch) as (_, factory):
        _seed_run(factory)
        with factory() as session:
            proposal_id = _at(session, DISPATCHED)
            held = leased(session, OWNER)
            session.commit()
        with factory() as worker, factory() as successor:
            settled = _settle_with(
                worker, proposal_id, held, _CLAIM
            )  # not committed yet: the lease row is locked
            assert settled.state == COMMITTED
            successor.execute(text("SET LOCAL lock_timeout = '200ms'"))
            with pytest.raises(LeaseLost):
                release(successor, held)  # the takeover has to wait for the settle's transaction
            successor.rollback()
            worker.commit()
        with factory() as successor:
            assert release(successor, held), "and gets the lease once the settle is committed"
            taken = acquire(successor, "worker-new", ttl_seconds=TTL_SECONDS)
            assert taken is not None and taken.fencing_token == held.fencing_token + 1
            successor.commit()
        with factory() as session:
            assert proposal_state(session, proposal_id).state == COMMITTED


def test_records_module_exports_the_transition_api() -> None:
    for name in (
        "transition",
        "proposal_state",
        "TRANSITIONS",
        "StaleVersion",
        "Approval",
        "effect_resource",
    ):
        assert name in records.__all__


# ---------------------------------------------------------------- the connector sends a stable key (B05.3, #711)


@pytest.fixture(scope="module")
def _downstream_process(tmp_path_factory: pytest.TempPathFactory) -> Any:
    from tests.egi_scenarios.conftest import Downstream

    handle = Downstream(tmp_path_factory.mktemp("downstream"))
    try:
        handle.start()
        yield handle
    finally:
        handle.close()


@pytest.fixture
def downstream(_downstream_process: Any) -> Any:
    from caliber.egress import EgressPolicy
    from caliber.integrations.openapi.executor import bind_egress_policy

    _downstream_process.reset()
    bind_egress_policy(EgressPolicy(allowed_hosts=frozenset({"127.0.0.1"})))
    yield _downstream_process
    bind_egress_policy(None)


def _connector_call(downstream: Any, key: str, version: int | None) -> dict[str, Any]:
    from caliber.effects.connectors.document_request import submit_config, submit_input
    from caliber.integrations.openapi.executor import execute_openapi_http_tool

    return execute_openapi_http_tool(
        execution_config=submit_config(downstream.base_url),
        input_schema=None,
        input_data=submit_input(
            case_id="MORT-2026-000184",
            idempotency_key=key,
            expected_resource_version=version,
            body={"document_type": "pay_statement"},
        ),
    )


def test_connector_sends_stable_key(downstream: Any) -> None:
    """The connector puts the proposal's key on the request, so sending it twice commits once and both answers are the
    same committed receipt claim."""
    from caliber.effects.connectors import TransportOutcome, classify_result
    from caliber.effects.connectors.document_request import CAPABILITY

    first = _connector_call(downstream, "stable-key-1", 1)
    again = _connector_call(downstream, "stable-key-1", 1)
    assert (first["status_code"], again["status_code"]) == (201, 200)
    assert first["json"] == again["json"]
    assert downstream.mutations() == 1
    assert {
        classify_result(first, capability=CAPABILITY).outcome,
        classify_result(again, capability=CAPABILITY).outcome,
    } == {TransportOutcome.COMMITTED_RECEIPT}


def test_stale_version_fails_without_mutation(downstream: Any) -> None:
    """The connector sends the proposal's expected version as ``If-Match``; a stale one is a definitive rejection and the
    downstream commits nothing."""
    from caliber.effects.connectors import TransportOutcome, classify_result
    from caliber.effects.connectors.document_request import CAPABILITY

    refused = _connector_call(downstream, "stale-key-1", 999)
    assert refused["status_code"] == 412
    assert (
        classify_result(refused, capability=CAPABILITY).outcome
        is TransportOutcome.DEFINITIVE_REJECTION
    )
    assert downstream.mutations() == 0
    assert _connector_call(downstream, "stale-key-2", 1)["status_code"] == 201, (
        "the current version is accepted"
    )


# ---------------------------------------------------------------- the named receipt tests (B05.4a, #717)


def test_committed_requires_verified_receipt() -> None:
    """A proposal moves to ``committed`` only with a verified receipt of its own dispatch (the fuller cases, the
    downstream fixture and the database rules are in ``test_effect_receipts.py``)."""
    import tempfile

    from caliber.effects.records import RECEIPT_REFUSED

    with tempfile.TemporaryDirectory() as tmp, pytest.MonkeyPatch.context() as monkeypatch:
        with _database("sqlite", Path(tmp), monkeypatch) as (_, factory):
            _seed_run(factory)
            with factory() as session:
                proposal_id = _at(session, DISPATCHED)
                other = _new(session, occurrence="occ-other")
                _move(session, other, APPROVED, **_evidence_for(session, other, PROPOSED, APPROVED))
                _move(session, other, DISPATCHED, owner_id=OWNER, attempt=ATTEMPT)
                foreign = _receipt(session, other)
                _refused(
                    EVIDENCE_REQUIRED,
                    session,
                    proposal_id,
                    COMMITTED,
                    owner_id=OWNER,
                    attempt=ATTEMPT,
                )
                for bad in ("rcpt-missing", foreign):
                    _refused(
                        RECEIPT_REFUSED,
                        session,
                        proposal_id,
                        COMMITTED,
                        owner_id=OWNER,
                        attempt=ATTEMPT,
                        receipt_id=bad,
                    )
                own = _receipt(session, proposal_id)
                assert _move(
                    session, proposal_id, COMMITTED, owner_id=OWNER, attempt=ATTEMPT, receipt_id=own
                ).applied
                assert proposal_state(session, proposal_id).receipt_id == own


def test_200_without_query_match_is_unknown() -> None:
    """A 2xx claim whose authoritative query does not confirm it leaves the proposal ``unknown``: not committed, and not
    failed either."""
    import tempfile

    from caliber.effects.connectors import Classified, TransportOutcome
    from caliber.effects.receipts import settle_dispatch

    with tempfile.TemporaryDirectory() as tmp, pytest.MonkeyPatch.context() as monkeypatch:
        with _database("sqlite", Path(tmp), monkeypatch) as (_, factory):
            _seed_run(factory)
            with factory() as session:
                proposal_id = _at(session, DISPATCHED)
                settled = settle_dispatch(
                    session,
                    proposal_id,
                    expected_version=proposal_state(session, proposal_id).version,
                    owner_id=OWNER,
                    attempt=ATTEMPT,
                    lease=leased(session, OWNER),
                    outcome=Classified(TransportOutcome.COMMITTED_RECEIPT, "http_200", 200, 1),
                    verify=lambda: None,
                    now=AT,
                )
                assert (settled.state, settled.reason, settled.receipt_id) == (
                    UNKNOWN,
                    "receipt_unverified",
                    None,
                )
                assert proposal_state(session, proposal_id).state == UNKNOWN
