"""Binding an approval request to the exact effect proposal it approves (B06.1, #706).

``create_approval_request`` ties a request to one proposal's full intent hash, expected resource version, the evidence the
approver is shown and the policy in force, with an expiry; ``assert_distinct_actor`` is the separation-of-duties rule
extracted from ``record_approval``. Every case that touches the database runs on SQLite and, where
``CALIBER_TEST_POSTGRES_URL`` is set, on PostgreSQL, against a database migrated by Alembic. Time is a fake clock.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import func, inspect, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from caliber.contracts import validate_record
from caliber.db.models import CaliberEffectProposal, CaliberRuntimeApprovalRequest
from caliber.effects.records import (
    APPROVED,
    COMMITTED,
    DISPATCHED,
    FAILED,
    POLICY_DECISION,
    RECONCILED,
    UNKNOWN,
    Approval,
)
from caliber.workflows.approval_policy import (
    ALLOWED_RESPONSES,
    MAX_TTL_SECONDS,
    ApprovalPolicy,
    ApprovalPolicyError,
    ApprovalRequestError,
    assert_distinct_actor,
    create_approval_request,
    record_approval,
)
from tests.dialect_helpers import DIALECTS, dialect_database_url
from tests.test_effect_proposals import _record, _seed_run
from tests.test_effect_records import AT, ATTEMPT, NAIVE_AT, OWNER, _decision, _move, _receipt
from tests.test_event_outbox import ALEMBIC_INI, _count, _database
from tests.test_migrations import PROJECT_ROOT

EVIDENCE = "sha256:" + "e" * 64
POLICY = "sha256:" + "f" * 64
SELF_APPROVAL_MESSAGE = (
    "the account that triggered this run cannot approve it; a second person "
    "must decide (set CALIBER_APPROVAL_ALLOW_SELF_APPROVAL=true for a "
    "single-operator install)"
)


# ---------------------------------------------------------------- the distinct-actor rule


def test_assert_distinct_actor() -> None:
    with pytest.raises(ApprovalPolicyError) as refused:
        assert_distinct_actor("u", "u", allow_self=False)
    assert str(refused.value) == SELF_APPROVAL_MESSAGE
    # A different actor, an unknown initiator (a legacy run) and a single-operator install pass.
    assert_distinct_actor("u", "other", allow_self=False)  # passes: nothing is raised
    assert_distinct_actor("u", None, allow_self=False)  # passes: nothing is raised
    assert_distinct_actor("u", "", allow_self=False)  # passes: nothing is raised
    assert_distinct_actor("u", "u", allow_self=True)  # passes: nothing is raised


def test_the_comparison_is_exact_not_case_folded_or_trimmed() -> None:
    for actor, initiator in (("U", "u"), ("u ", "u"), ("alice", "alice@example.test")):
        assert_distinct_actor(actor, initiator, allow_self=False)  # passes: nothing is raised


def test_record_approval_applies_the_extracted_rule_with_the_policys_setting() -> None:
    approver = frozenset({"caliber.approver"})
    with pytest.raises(ApprovalPolicyError) as refused:
        record_approval(
            policy=ApprovalPolicy(),
            existing_approvals=None,
            actor="u",
            actor_scopes=approver,
            initiated_by="u",
        )
    assert str(refused.value) == SELF_APPROVAL_MESSAGE
    allowed = record_approval(
        policy=ApprovalPolicy(allow_self_approval=True),
        existing_approvals=None,
        actor="u",
        actor_scopes=approver,
        initiated_by="u",
    )
    assert allowed.approvals == ("u",)


# ---------------------------------------------------------------- creating a bound request


def _proposal(session: Session, **intent: Any) -> CaliberEffectProposal:
    recorded = _record(session, **intent)
    proposal = session.get(CaliberEffectProposal, recorded.proposal_id)
    assert proposal is not None
    return proposal


def _create(session: Session, proposal: CaliberEffectProposal, **overrides: Any) -> Any:
    fields: dict[str, Any] = {
        "evidence_hash": EVIDENCE,
        "policy_digest": POLICY,
        "requested_by": "editor_a",
        "ttl_seconds": 3600,
        "now": AT,
    }
    fields.update(overrides)
    return create_approval_request(session, proposal=proposal, **fields)


@pytest.mark.parametrize("dialect", DIALECTS)
def test_a_request_is_bound_to_the_proposals_exact_intent_and_is_a_valid_v1_record(
    dialect: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with _database(dialect, tmp_path, monkeypatch) as (_, factory):
        _seed_run(factory)
        with factory() as session:
            proposal = _proposal(session)
            request = _create(session, proposal)
            session.commit()
            request_id, proposal_id = request.runtime_approval_id, proposal.proposal_id
            intent_hash = proposal.intent_hash
        with factory() as session:
            stored = session.get(CaliberRuntimeApprovalRequest, request_id)
            assert stored is not None
            assert stored.proposal_id == proposal_id
            assert stored.intent_hash == intent_hash and stored.intent_hash.startswith("sha256:")
            assert (stored.evidence_hash, stored.policy_digest) == (EVIDENCE, POLICY)
            assert stored.expected_resource_version == "7"
            assert stored.expires_at == NAIVE_AT + timedelta(hours=1)
            assert stored.requested_at == NAIVE_AT
            assert stored.requested_by == "editor_a"
            # Stored pending against the proposal's own run, project and node, and not yet decided or consumed.
            assert (stored.workflow_run_id, stored.project_id, stored.node_id) == (
                "RUN-ep",
                "PRJ-ep",
                "ask",
            )
            assert stored.status == "pending"
            assert (
                stored.decided_at,
                stored.decided_by,
                stored.consumed_at,
                stored.superseded_by,
            ) == (
                None,
                None,
                None,
                None,
            )
            assert stored.policy_snapshot == ApprovalPolicy().to_snapshot()
            record = {
                "schema_version": "v1",
                "approval_request_id": stored.runtime_approval_id,
                "proposal_id": stored.proposal_id,
                "intent_hash": stored.intent_hash,
                "evidence_hash": stored.evidence_hash,
                "policy_digest": stored.policy_digest,
                "expected_resource_version": stored.expected_resource_version,
                "risk_class": "irreversible_write",
                "requested_by": stored.requested_by,
                "expires_at": "2026-03-04T06:06:07.000000Z",
                "allowed_responses": list(ALLOWED_RESPONSES),
            }
        validate_record("approval_request", record)


@pytest.mark.parametrize("dialect", DIALECTS)
def test_request_requires_proposed_state(
    dialect: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A proposal in any state but ``proposed`` raises ``ValueError("proposal_not_proposed")`` and stores nothing."""
    with _database(dialect, tmp_path, monkeypatch) as (_, factory):
        _seed_run(factory)
        with factory() as session:
            by_state: dict[str, CaliberEffectProposal] = {}
            for state in (APPROVED, DISPATCHED, COMMITTED, FAILED, UNKNOWN, RECONCILED):
                by_state[state] = _proposal(session, node=state, effect_class="read")
            session.commit()
        with factory() as session:
            for state in (APPROVED, DISPATCHED, COMMITTED, UNKNOWN, RECONCILED):
                pid = by_state[state].proposal_id
                _move(
                    session,
                    pid,
                    APPROVED,
                    approval=Approval(
                        POLICY_DECISION, _decision(session, pid, decision_id=f"pd-{state}")
                    ),
                )
            for state in (DISPATCHED, COMMITTED, UNKNOWN, RECONCILED):
                _move(
                    session,
                    by_state[state].proposal_id,
                    DISPATCHED,
                    owner_id=OWNER,
                    attempt=ATTEMPT,
                )
            committed_id = by_state[COMMITTED].proposal_id
            _move(
                session,
                committed_id,
                COMMITTED,
                owner_id=OWNER,
                attempt=ATTEMPT,
                receipt_id=_receipt(session, committed_id),
            )
            for state in (UNKNOWN, RECONCILED):
                _move(
                    session,
                    by_state[state].proposal_id,
                    UNKNOWN,
                    owner_id=OWNER,
                    attempt=ATTEMPT,
                    reason="timeout",
                )
            _move(session, by_state[RECONCILED].proposal_id, RECONCILED, reason="found by key")
            _move(session, by_state[FAILED].proposal_id, FAILED, reason="refused")
            session.commit()
        with factory() as session:
            for state, stale in by_state.items():
                fresh = session.get(CaliberEffectProposal, stale.proposal_id)
                assert fresh is not None and fresh.state == state
                with pytest.raises(ValueError, match="^proposal_not_proposed$") as refused:
                    _create(session, fresh)
                assert isinstance(refused.value, ApprovalRequestError)
                assert refused.value.code == "proposal_not_proposed"
            session.rollback()
        assert _count(factory, CaliberRuntimeApprovalRequest) == 0


@pytest.mark.parametrize("dialect", DIALECTS)
def test_the_stored_state_decides_not_the_object_the_caller_holds(
    dialect: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with _database(dialect, tmp_path, monkeypatch) as (_, factory):
        _seed_run(factory)
        with factory() as session:
            proposal = _proposal(session, effect_class="read")
            pid = proposal.proposal_id
            # An object that still says ``proposed`` after the proposal moved on (a stale read) is refused...
            _move(
                session, pid, APPROVED, approval=Approval(POLICY_DECISION, _decision(session, pid))
            )
            with pytest.raises(ApprovalRequestError) as stale:
                _create(session, proposal)
            assert stale.value.code == "proposal_not_proposed"
            # ... so is a stand-in that claims to be approved while the stored proposal is still proposed...
            other = _proposal(session, node="other")
            standin = CaliberEffectProposal(proposal_id=other.proposal_id, state=APPROVED)
            with pytest.raises(ApprovalRequestError) as claimed:
                _create(session, standin)
            assert claimed.value.code == "proposal_not_proposed"
            # ... and a proposal that does not exist is its own refusal.
            ghost = CaliberEffectProposal(proposal_id="prop-missing", state="proposed")
            with pytest.raises(ApprovalRequestError) as missing:
                _create(session, ghost)
            assert missing.value.code == "proposal_not_found"
            # The request binds what is stored, not what the stand-in says.
            standin.state = "proposed"
            request = _create(session, standin)
            assert request.intent_hash == other.intent_hash
            session.rollback()


@pytest.mark.parametrize("dialect", DIALECTS)
def test_a_proposal_without_an_expected_version_binds_null_and_the_writer_never_commits(
    dialect: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with _database(dialect, tmp_path, monkeypatch) as (_, factory):
        _seed_run(factory)
        with factory() as session:
            proposal = _proposal(session, expected_resource_version=None, effect_class="export")
            request = _create(session, proposal)
            assert request.expected_resource_version is None
            assert (
                session.execute(
                    select(func.count()).select_from(CaliberRuntimeApprovalRequest)
                ).scalar_one()
                == 1
            )  # flushed, visible in the caller's transaction
            session.rollback()  # the caller decides: nothing was committed behind its back
        assert _count(factory, CaliberRuntimeApprovalRequest) == 0


def test_a_naive_clock_is_utc_an_offset_is_converted_and_none_is_now(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with _database("sqlite", tmp_path, monkeypatch) as (_, factory):
        _seed_run(factory)
        with factory() as session:
            proposal = _proposal(session)
            naive = _create(session, proposal, now=NAIVE_AT)
            assert naive.requested_at == NAIVE_AT and naive.expires_at == NAIVE_AT + timedelta(
                hours=1
            )
            plus_two = timezone(timedelta(hours=2))
            offset = _create(session, proposal, now=AT.astimezone(plus_two))
            assert offset.requested_at == NAIVE_AT  # converted to UTC, not stored as local time
            before = datetime.now(timezone.utc).replace(tzinfo=None)
            live = _create(session, proposal, now=None)
            assert before <= live.requested_at <= datetime.now(timezone.utc).replace(tzinfo=None)
            assert live.expires_at == live.requested_at + timedelta(hours=1)
            session.rollback()


def test_the_ttl_boundaries(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    with _database("sqlite", tmp_path, monkeypatch) as (_, factory):
        _seed_run(factory)
        with factory() as session:
            proposal = _proposal(session)
            assert _create(session, proposal, ttl_seconds=1).expires_at == NAIVE_AT + timedelta(
                seconds=1
            )
            longest = _create(session, proposal, ttl_seconds=MAX_TTL_SECONDS)
            assert longest.expires_at == NAIVE_AT + timedelta(days=365)
            session.rollback()


# ---------------------------------------------------------------- malformed input


def _lone_surrogate() -> str:
    return "a" + chr(
        0xD800
    )  # built here: a lone surrogate must not be a pytest parameter (the Allure listener aborts)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("evidence_hash", ""),
        ("evidence_hash", "e" * 64),
        ("evidence_hash", "sha256:" + "E" * 64),
        ("evidence_hash", "sha256:" + "e" * 63),
        ("evidence_hash", "sha256:" + "e" * 64 + "\n"),
        ("evidence_hash", None),
        ("evidence_hash", 7),
        ("policy_digest", ""),
        ("policy_digest", "sha512:" + "f" * 64),
        ("policy_digest", "sha256:" + "f" * 65),
        ("policy_digest", None),
        ("requested_by", ""),
        ("requested_by", "   "),
        ("requested_by", "x" * 257),
        ("requested_by", "ed\x00itor"),
        ("requested_by", "ed\nitor"),
        ("requested_by", None),
        ("requested_by", 5),
        ("ttl_seconds", 0),
        ("ttl_seconds", -1),
        ("ttl_seconds", MAX_TTL_SECONDS + 1),
        ("ttl_seconds", True),
        ("ttl_seconds", 1.5),
        ("ttl_seconds", "3600"),
        ("ttl_seconds", None),
    ],
)
def test_malformed_fields_are_refused_by_name_before_anything_is_read_or_stored(
    field: str, value: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with _database("sqlite", tmp_path, monkeypatch) as (_, factory):
        _seed_run(factory)
        with factory() as session:
            proposal = _proposal(session)
            with pytest.raises(ApprovalRequestError) as refused:
                _create(session, proposal, **{field: value})
            assert refused.value.code == "bad_field" and field in str(refused.value)
            assert (
                session.execute(
                    select(func.count()).select_from(CaliberRuntimeApprovalRequest)
                ).scalar_one()
                == 0
            )


def test_a_lone_surrogate_requester_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with _database("sqlite", tmp_path, monkeypatch) as (_, factory):
        _seed_run(factory)
        with factory() as session:
            with pytest.raises(ApprovalRequestError, match="requested_by"):
                _create(session, _proposal(session), requested_by=_lone_surrogate())


def test_a_request_the_contract_would_refuse_is_not_stored(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The record is validated against ``approval_request`` v1 *before* the row is added."""
    import caliber.workflows.approval_policy as module
    from caliber.contracts import ContractValidationError

    def refuse(kind: str, payload: Any) -> None:
        raise ContractValidationError(kind, ["refused for the test"])

    monkeypatch.setattr(module, "validate_record", refuse)
    with _database("sqlite", tmp_path, monkeypatch) as (_, factory):
        _seed_run(factory)
        with factory() as session:
            with pytest.raises(ContractValidationError):
                _create(session, _proposal(session))
            assert (
                session.execute(
                    select(func.count()).select_from(CaliberRuntimeApprovalRequest)
                ).scalar_one()
                == 0
            )


# ---------------------------------------------------------------- the schema


def _legacy_row(**overrides: Any) -> CaliberRuntimeApprovalRequest:
    fields: dict[str, Any] = {
        "runtime_approval_id": "RAP-legacy",
        "workflow_run_id": "RUN-ep",
        "project_id": "PRJ-ep",
        "node_id": "gate",
        "status": "pending",
        "policy_snapshot": {"timeout_behavior": "block"},
    }
    fields.update(overrides)
    return CaliberRuntimeApprovalRequest(**fields)


@pytest.mark.parametrize("dialect", DIALECTS)
def test_a_legacy_request_without_a_proposal_is_still_valid_and_a_half_bound_one_is_not(
    dialect: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with _database(dialect, tmp_path, monkeypatch) as (_, factory):
        _seed_run(factory)
        with factory() as session:
            proposal = _proposal(session)
            session.add(
                _legacy_row()
            )  # no proposal: the way every node-level approval is created today
            session.commit()
            pid = proposal.proposal_id
        full: dict[str, Any] = {
            "proposal_id": pid,
            "intent_hash": "sha256:" + "a" * 64,
            "evidence_hash": EVIDENCE,
            "policy_digest": POLICY,
            "expires_at": NAIVE_AT,
            "requested_by": "editor_a",
        }
        for missing in (
            "intent_hash",
            "evidence_hash",
            "policy_digest",
            "expires_at",
            "requested_by",
        ):
            partial = {key: value for key, value in full.items() if key != missing}
            with factory() as session:
                session.add(_legacy_row(runtime_approval_id=f"RAP-no-{missing}", **partial))
                with pytest.raises(IntegrityError):
                    session.commit()
        with factory() as session:  # a complete binding is accepted
            session.add(_legacy_row(runtime_approval_id="RAP-full", **full))
            session.commit()
        if dialect == "postgresql":  # SQLite does not enforce foreign keys in this repository
            with factory() as session:  # the proposal must exist
                session.add(
                    _legacy_row(
                        runtime_approval_id="RAP-orphan", **{**full, "proposal_id": "prop-none"}
                    )
                )
                with pytest.raises(IntegrityError):
                    session.commit()
        assert _count(factory, CaliberRuntimeApprovalRequest) == 2


def _columns(factory: sessionmaker[Session]) -> set[str]:
    with factory() as session:
        return {
            c["name"]
            for c in inspect(session.get_bind()).get_columns("caliber_runtime_approval_requests")
        }


@pytest.mark.parametrize("dialect", DIALECTS)
def test_migration_0127_keeps_legacy_rows_and_downgrades_cleanly(
    dialect: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    binding = {
        "proposal_id",
        "intent_hash",
        "evidence_hash",
        "policy_digest",
        "expected_resource_version",
        "expires_at",
        "superseded_by",
        "consumed_at",
        "requested_by",
    }
    with dialect_database_url(dialect, tmp_path, "approval_binding") as url:
        monkeypatch.setenv("CALIBER_DATABASE_URL", url)
        monkeypatch.chdir(PROJECT_ROOT)
        config = Config(str(ALEMBIC_INI))
        command.upgrade(config, "0126")
        from sqlalchemy import create_engine

        engine = create_engine(url)
        factory = sessionmaker(engine, expire_on_commit=False)
        try:
            assert not binding & _columns(factory)
            _seed_run(factory)
            with factory() as session:
                session.execute(
                    text(
                        "INSERT INTO caliber_runtime_approval_requests "
                        "(runtime_approval_id, workflow_run_id, project_id, node_id, status, requested_at) "
                        "VALUES ('RAP-old', 'RUN-ep', 'PRJ-ep', 'gate', 'pending', :at)"
                    ),
                    {"at": NAIVE_AT},
                )
                session.commit()
            command.upgrade(config, "head")
            assert binding <= _columns(factory)
            with factory() as session:
                old = session.get(CaliberRuntimeApprovalRequest, "RAP-old")
                assert old is not None and old.status == "pending"
                assert all(getattr(old, column) is None for column in binding)
                indexes = {
                    i["name"]
                    for i in inspect(session.get_bind()).get_indexes(
                        "caliber_runtime_approval_requests"
                    )
                }
                assert "ix_runtime_approvals_proposal" in indexes
            command.downgrade(config, "0126")
            assert not binding & _columns(factory)
            with factory() as session:
                assert (
                    session.execute(
                        text(
                            "SELECT status FROM caliber_runtime_approval_requests WHERE runtime_approval_id = 'RAP-old'"
                        )
                    ).scalar_one()
                    == "pending"
                )
            command.upgrade(config, "head")
            assert binding <= _columns(factory)
        finally:
            engine.dispose()
