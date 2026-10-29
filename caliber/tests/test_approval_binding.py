"""Binding an approval request to the exact effect proposal it approves (B06.1, #706).

``create_approval_request`` ties a request to one proposal's full intent hash, expected resource version, the evidence the
approver is shown and a digest computed from the node's own policy, with an expiry; ``assert_distinct_actor`` is the
separation-of-duties rule extracted from ``record_approval``. Migration 0127 freezes the binding in the database. Every case
that touches the database runs on SQLite and, where ``CALIBER_TEST_POSTGRES_URL`` is set, on PostgreSQL, against a database
migrated by Alembic. Time is a fake clock; the lock tests use ``lock_timeout`` or poll ``pg_stat_activity`` and never sleep to
make a race happen.
"""

from __future__ import annotations

import hashlib
import json
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, func, inspect, select, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session, sessionmaker

from caliber.contracts import ContractValidationError, validate_record
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
from caliber.workflows import approval_requests
from caliber.workflows.approval_policy import (
    ApprovalPolicy,
    ApprovalPolicyError,
    assert_distinct_actor,
    record_approval,
)
from caliber.workflows.approval_requests import (
    ALLOWED_RESPONSES,
    MAX_TTL_SECONDS,
    ApprovalRequestError,
    create_approval_request,
    policy_digest,
)
from tests.dialect_helpers import DIALECTS, dialect_database_url
from tests.test_effect_proposals import _record, _seed_run
from tests.test_effect_records import (
    AT,
    ATTEMPT,
    NAIVE_AT,
    OWNER,
    _decision,
    _move,
    _need_postgresql,
    _receipt,
)
from tests.test_event_outbox import ALEMBIC_INI, _count, _database
from tests.test_migrations import PROJECT_ROOT

EVIDENCE = "sha256:" + "e" * 64
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


def test_the_policy_module_does_not_import_or_export_the_database_writer() -> None:
    """``approval_policy`` is imported lazily by manifest validation: it stays free of the request writer."""
    import subprocess
    import sys

    probe = (
        "import sys, caliber.workflows.approval_policy as m; "
        "assert not hasattr(m, 'create_approval_request'); "
        "assert 'caliber.workflows.approval_requests' not in sys.modules; "
        "assert not hasattr(m, 'ApprovalRequestError')"
    )
    done = subprocess.run(
        [sys.executable, "-c", probe], capture_output=True, text=True, check=False
    )
    assert done.returncode == 0, done.stderr


# ---------------------------------------------------------------- creating a bound request


def _proposal(session: Session, **intent: Any) -> CaliberEffectProposal:
    recorded = _record(session, **intent)
    proposal = session.get(CaliberEffectProposal, recorded.proposal_id)
    assert proposal is not None
    return proposal


def _create(session: Session, proposal: CaliberEffectProposal, **overrides: Any) -> Any:
    fields: dict[str, Any] = {
        "policy": ApprovalPolicy(),
        "evidence_hash": EVIDENCE,
        "requested_by": "editor_a",
        "ttl_seconds": 3600,
        "now": AT,
    }
    fields.update(overrides)
    return create_approval_request(session, proposal=proposal, **fields)


def _snapshot_digest(snapshot: dict[str, Any]) -> str:
    """The expected digest, computed independently of the production helper."""
    data = json.dumps(snapshot, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return "sha256:" + hashlib.sha256(data.encode("utf-8")).hexdigest()


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
            assert stored.evidence_hash == EVIDENCE
            assert stored.policy_digest == _snapshot_digest(ApprovalPolicy().to_snapshot())
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


# ---------------------------------------------------------------- the node's own policy


@pytest.mark.parametrize("dialect", DIALECTS)
def test_the_stored_snapshot_is_exactly_the_nodes_policy_and_the_digest_is_computed_from_it(
    dialect: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    admin_quorum = ApprovalPolicy(required_role="caliber.admin", approval_count=2)
    single_operator = ApprovalPolicy(allow_self_approval=True)
    with _database(dialect, tmp_path, monkeypatch) as (_, factory):
        _seed_run(factory)
        with factory() as session:
            ids = {}
            for name, policy in (
                ("admin", admin_quorum),
                ("self", single_operator),
                ("default", ApprovalPolicy()),
            ):
                ids[name] = _create(
                    session, _proposal(session, node=name), policy=policy
                ).runtime_approval_id
            session.commit()
        with factory() as session:
            rows = {
                name: session.get(CaliberRuntimeApprovalRequest, rid) for name, rid in ids.items()
            }
            admin, own, default = rows["admin"], rows["self"], rows["default"]
            assert admin is not None and own is not None and default is not None
            assert admin.policy_snapshot == {
                "required_role": "caliber.admin",
                "approval_count": 2,
                "timeout_behavior": "block",
                "allow_self_approval": False,
            }
            assert own.policy_snapshot == {
                "required_role": "caliber.approver",
                "approval_count": 1,
                "timeout_behavior": "block",
                "allow_self_approval": True,
            }
            # The decision route reads the snapshot back as the same policy, so what is pinned is what is enforced.
            assert ApprovalPolicy.from_snapshot(admin.policy_snapshot) == admin_quorum
            assert ApprovalPolicy.from_snapshot(own.policy_snapshot) == single_operator
            for row in (admin, own, default):
                assert row.policy_digest == _snapshot_digest(row.policy_snapshot or {})
            assert len({admin.policy_digest, own.policy_digest, default.policy_digest}) == 3


def test_the_digest_covers_the_policy_and_is_stable() -> None:
    base = ApprovalPolicy()
    assert policy_digest(base) == policy_digest(ApprovalPolicy())
    assert policy_digest(base) == _snapshot_digest(base.to_snapshot())
    assert policy_digest(base).startswith("sha256:") and len(policy_digest(base)) == 71
    for other in (
        ApprovalPolicy(required_role="caliber.admin"),
        ApprovalPolicy(approval_count=2),
        ApprovalPolicy(allow_self_approval=True),
    ):
        assert policy_digest(other) != policy_digest(base)


def test_the_caller_cannot_supply_a_digest_and_must_name_the_policy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with _database("sqlite", tmp_path, monkeypatch) as (_, factory):
        _seed_run(factory)
        with factory() as session:
            proposal = _proposal(session)
            with pytest.raises(TypeError):
                create_approval_request(  # type: ignore[call-arg]
                    session,
                    proposal=proposal,
                    policy=ApprovalPolicy(),
                    evidence_hash=EVIDENCE,
                    policy_digest="sha256:" + "f" * 64,
                    requested_by="editor_a",
                    ttl_seconds=60,
                )
            with pytest.raises(TypeError):
                create_approval_request(  # type: ignore[call-arg]
                    session,
                    proposal=proposal,
                    evidence_hash=EVIDENCE,
                    requested_by="editor_a",
                    ttl_seconds=60,
                )


def _bad_policies() -> dict[str, Any]:
    return {
        "a dict": {"required_role": "caliber.admin"},
        "none": None,
        "an unknown role": ApprovalPolicy(required_role="admin"),
        "a count of zero": ApprovalPolicy(approval_count=0),
        "a negative count": ApprovalPolicy(approval_count=-1),
        "a boolean count": ApprovalPolicy(approval_count=True),
        "a text count": ApprovalPolicy(approval_count="2"),  # type: ignore[arg-type]
        "escalate": ApprovalPolicy(timeout_behavior="escalate"),
        "auto_reject": ApprovalPolicy(timeout_behavior="auto_reject"),
        "a text self-approval flag": ApprovalPolicy(allow_self_approval="yes"),  # type: ignore[arg-type]
    }


@pytest.mark.parametrize("name", sorted(_bad_policies()))
def test_a_policy_the_runtime_cannot_honour_is_refused_and_nothing_is_stored(
    name: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with _database("sqlite", tmp_path, monkeypatch) as (_, factory):
        _seed_run(factory)
        with factory() as session:
            with pytest.raises(ApprovalRequestError, match="policy") as refused:
                _create(session, _proposal(session), policy=_bad_policies()[name])
            assert refused.value.code == "bad_field"
            assert (
                session.execute(
                    select(func.count()).select_from(CaliberRuntimeApprovalRequest)
                ).scalar_one()
                == 0
            )


# ---------------------------------------------------------------- only a proposed proposal


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
            # flushed, visible in the caller's transaction
            assert (
                session.execute(
                    select(func.count()).select_from(CaliberRuntimeApprovalRequest)
                ).scalar_one()
                == 1
            )
            session.rollback()  # the caller decides: nothing was committed behind its back
        assert _count(factory, CaliberRuntimeApprovalRequest) == 0


# ---------------------------------------------------------------- the clock


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


def _unrepresentable_clocks() -> dict[str, Any]:
    utc = timezone.utc
    return {
        "the last instant of year 9999": datetime(9999, 12, 31, 23, 59, 59, tzinfo=utc),
        "the largest naive datetime": datetime.max,
        "the largest aware datetime": datetime.max.replace(tzinfo=utc),
        "the smallest datetime east of UTC": datetime(1, 1, 1, tzinfo=timezone(timedelta(hours=5))),
        "not a datetime": "2026-03-04T05:06:07Z",
        "a number": 1772600767,
    }


@pytest.mark.parametrize("name", sorted(_unrepresentable_clocks()))
def test_a_clock_the_expiry_cannot_be_computed_from_is_a_typed_refusal_not_an_overflow(
    name: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with _database("sqlite", tmp_path, monkeypatch) as (_, factory):
        _seed_run(factory)
        with factory() as session:
            with pytest.raises(ApprovalRequestError, match="now") as refused:
                _create(
                    session,
                    _proposal(session),
                    now=_unrepresentable_clocks()[name],
                    ttl_seconds=3600,
                )
            assert refused.value.code == "bad_field"
            assert (
                session.execute(
                    select(func.count()).select_from(CaliberRuntimeApprovalRequest)
                ).scalar_one()
                == 0
            )


# ---------------------------------------------------------------- malformed input


def _lone_surrogate() -> str:
    # built here: a lone surrogate must not be a pytest parameter (the Allure listener aborts)
    return "a" + chr(0xD800)


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


#: Requesters that look like a name but are not safe to store or compare: surrounding whitespace, and characters that are
#: invisible or reorder text (so two different ids can look identical).
def _with(code: int, *, at: str = "middle") -> str:
    """``editor_a`` with the character ``code`` at the start, in the middle or at the end (built with ``chr``, not typed)."""
    mark = chr(code)
    return {"start": mark + "editor_a", "middle": "edi" + mark + "tor_a", "end": "editor_a" + mark}[
        at
    ]


_DISGUISED_REQUESTERS = {
    "a leading space": " editor_a",
    "a trailing space": "editor_a ",
    "a trailing tab": "editor_a" + chr(9),
    "a leading no-break space": _with(0xA0, at="start"),
    "a zero-width space": _with(0x200B),
    "a zero-width joiner": _with(0x200D),
    "a zero-width non-joiner": _with(0x200C),
    "a byte-order mark": _with(0xFEFF, at="start"),
    "a right-to-left override": _with(0x202E, at="start"),
    "a left-to-right embedding": _with(0x202A),
    "a bidirectional isolate": _with(0x2066),
    "a left-to-right mark": _with(0x200E, at="end"),
    "a line separator": _with(0x2028),
    "a paragraph separator": _with(0x2029),
    "a soft hyphen": _with(0xAD),
    "a C1 control": _with(0x85),
}


@pytest.mark.parametrize("name", sorted(_DISGUISED_REQUESTERS))
def test_a_requester_with_surrounding_whitespace_or_invisible_characters_is_refused(
    name: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with _database("sqlite", tmp_path, monkeypatch) as (_, factory):
        _seed_run(factory)
        with factory() as session:
            with pytest.raises(ApprovalRequestError, match="requested_by") as refused:
                _create(session, _proposal(session), requested_by=_DISGUISED_REQUESTERS[name])
            assert refused.value.code == "bad_field"
            assert (
                session.execute(
                    select(func.count()).select_from(CaliberRuntimeApprovalRequest)
                ).scalar_one()
                == 0
            )


@pytest.mark.parametrize(
    "requester",
    [
        "editor_a",
        "editor a",
        "Jos" + chr(0xE9) + " O'Neil",
        "reza.rahimi@example.test",
        chr(0x7DE8) + chr(0x96C6),
        "x" * 256,
    ],
)
def test_ordinary_requesters_are_accepted(
    requester: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with _database("sqlite", tmp_path, monkeypatch) as (_, factory):
        _seed_run(factory)
        with factory() as session:
            assert (
                _create(session, _proposal(session), requested_by=requester).requested_by
                == requester
            )
            session.rollback()


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

    def refuse(kind: str, payload: Any) -> None:
        raise ContractValidationError(kind, ["refused for the test"])

    monkeypatch.setattr(approval_requests, "validate_record", refuse)
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


# ---------------------------------------------------------------- locking the proposal row (PostgreSQL)


def _bind_two_sessions(factory: sessionmaker[Session]) -> str:
    _seed_run(factory)
    with factory() as session:
        proposal_id = _proposal(session, effect_class="read").proposal_id
        session.commit()
    return proposal_id


def test_a_creator_holds_the_proposal_row_until_it_commits_on_postgresql(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Creators serialize: the second one waits for the first one's transaction, then also succeeds (and supersedes it, B06.2)."""
    _need_postgresql()
    with _database("postgresql", tmp_path, monkeypatch) as (_, factory):
        proposal_id = _bind_two_sessions(factory)
        with factory() as first, factory() as second:
            proposal = first.get(CaliberEffectProposal, proposal_id)
            assert proposal is not None
            _create(first, proposal)  # flushed, not committed: the proposal row is locked
            second.execute(text("SET LOCAL lock_timeout = '200ms'"))
            with pytest.raises(DBAPIError, match="lock timeout"):
                _create(
                    second, proposal
                )  # the second creator has to wait for the first one's transaction
            second.rollback()
            first.commit()
        with factory() as session:
            again = session.get(CaliberEffectProposal, proposal_id)
            assert again is not None
            second_request = _create(session, again, now=AT + timedelta(minutes=1))
            session.commit()
        assert second_request.proposal_id == proposal_id
        # No unique index: both rows exist; the newer one superseded the older (B06.2, tests/test_approval_supersession.py).
        assert (
            _count(
                factory,
                CaliberRuntimeApprovalRequest,
                CaliberRuntimeApprovalRequest.proposal_id == proposal_id,
            )
            == 2
        )


def test_concurrent_creators_for_one_proposal_all_succeed_serially_on_postgresql(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _need_postgresql()
    workers = 4
    with _database("postgresql", tmp_path, monkeypatch) as (_, factory):
        proposal_id = _bind_two_sessions(factory)
        barrier = threading.Barrier(workers)
        outcomes: list[str] = []
        errors: list[BaseException] = []
        guard = threading.Lock()

        def run(index: int) -> None:
            try:
                with factory() as session:
                    proposal = session.get(CaliberEffectProposal, proposal_id)
                    assert proposal is not None
                    barrier.wait(10)
                    request = _create(session, proposal, requested_by=f"editor_{index}")
                    session.commit()
                with guard:
                    outcomes.append(request.runtime_approval_id)
            except BaseException as exc:
                with guard:
                    errors.append(exc)

        threads = [threading.Thread(target=run, args=(i,), daemon=True) for i in range(workers)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(60)
        assert not errors, errors
        assert len(set(outcomes)) == workers
        assert (
            _count(
                factory,
                CaliberRuntimeApprovalRequest,
                CaliberRuntimeApprovalRequest.proposal_id == proposal_id,
            )
            == workers
        )


def test_a_creator_blocks_a_move_of_the_proposal_until_it_commits_on_postgresql(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``transition``'s UPDATE of the proposal waits for the creator, so a request is never created for a proposal that
    approved itself in between."""
    _need_postgresql()
    with _database("postgresql", tmp_path, monkeypatch) as (_, factory):
        proposal_id = _bind_two_sessions(factory)
        with factory() as creator, factory() as mover:
            proposal = creator.get(CaliberEffectProposal, proposal_id)
            assert proposal is not None
            _create(creator, proposal)  # the proposal row is locked
            decision = _decision(mover, proposal_id)
            mover.execute(text("SET LOCAL lock_timeout = '200ms'"))
            with pytest.raises(DBAPIError, match="lock timeout"):
                _move(mover, proposal_id, APPROVED, approval=Approval(POLICY_DECISION, decision))
            mover.rollback()
            creator.commit()
        with factory() as mover:  # once the creator has committed the move goes through
            decision = _decision(mover, proposal_id, decision_id="pd-after")
            assert _move(
                mover, proposal_id, APPROVED, approval=Approval(POLICY_DECISION, decision)
            ).applied
            mover.commit()


def _blocked_on_a_lock(factory: sessionmaker[Session]) -> bool:
    with factory() as observer:
        return bool(
            observer.execute(
                text(
                    "SELECT count(*) FROM pg_stat_activity WHERE datname = current_database() "
                    "AND wait_event_type = 'Lock' AND query ILIKE '%FOR UPDATE%'"
                )
            ).scalar_one()
        )


def test_a_proposal_that_moves_to_approved_before_the_creator_reads_it_is_refused_on_postgresql(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The mover has updated the row but not committed. The creator blocks on it, and when the move commits it sees
    ``approved`` and refuses; it does not bind a request to a proposal that is no longer waiting for one."""
    _need_postgresql()
    with _database("postgresql", tmp_path, monkeypatch) as (_, factory):
        proposal_id = _bind_two_sessions(factory)
        result: dict[str, Any] = {}

        def create_when_unlocked() -> None:
            try:
                with factory() as session:
                    proposal = session.get(
                        CaliberEffectProposal, proposal_id
                    )  # still ``proposed``: the move is uncommitted
                    assert proposal is not None and proposal.state == "proposed"
                    result["entered"] = True
                    try:
                        _create(session, proposal)
                        result["outcome"] = "created"
                    except ApprovalRequestError as exc:
                        result["outcome"] = exc.code
                    session.commit()
            except BaseException as exc:
                result["error"] = exc

        with factory() as mover:
            decision = _decision(mover, proposal_id)
            _move(
                mover, proposal_id, APPROVED, approval=Approval(POLICY_DECISION, decision)
            )  # row locked by the UPDATE
            creator = threading.Thread(target=create_when_unlocked, daemon=True)
            creator.start()
            deadline = time.monotonic() + 15
            while not _blocked_on_a_lock(factory) and time.monotonic() < deadline:
                time.sleep(0.02)  # polling for the lock wait, not waiting for a race to happen
            assert _blocked_on_a_lock(factory), result
            assert (
                creator.is_alive() and "outcome" not in result
            )  # it is waiting, not deciding on a stale read
            mover.commit()
        creator.join(15)
        assert not creator.is_alive()
        assert "error" not in result, result
        assert result["outcome"] == "proposal_not_proposed"
        assert _count(factory, CaliberRuntimeApprovalRequest) == 0


# ---------------------------------------------------------------- the schema: the binding is a database fact


def _row(**overrides: Any) -> dict[str, Any]:
    values: dict[str, Any] = {
        "runtime_approval_id": "RAP-raw",
        "workflow_run_id": "RUN-ep",
        "project_id": "PRJ-ep",
        "node_id": "gate",
        "status": "pending",
        "requested_at": NAIVE_AT,
    }
    values.update(overrides)
    return values


def _insert(session: Session, **overrides: Any) -> None:
    values = _row(**overrides)
    columns = ", ".join(values)
    marks = ", ".join(f":{name}" for name in values)
    session.execute(
        text(f"INSERT INTO caliber_runtime_approval_requests ({columns}) VALUES ({marks})"), values
    )
    session.commit()


def _bound_columns(proposal_id: str, **overrides: Any) -> dict[str, Any]:
    values: dict[str, Any] = {
        "proposal_id": proposal_id,
        "intent_hash": "sha256:" + "a" * 64,
        "evidence_hash": EVIDENCE,
        "policy_digest": "sha256:" + "b" * 64,
        "expected_resource_version": "7",
        "expires_at": NAIVE_AT + timedelta(hours=1),
        "requested_by": "editor_a",
    }
    values.update(overrides)
    return values


_BINDING = (
    "proposal_id",
    "intent_hash",
    "evidence_hash",
    "policy_digest",
    "expected_resource_version",
    "expires_at",
    "requested_by",
)


@pytest.mark.parametrize("dialect", DIALECTS)
def test_a_legacy_request_without_a_proposal_is_still_valid(
    dialect: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with _database(dialect, tmp_path, monkeypatch) as (_, factory):
        _seed_run(factory)
        with (
            factory() as session
        ):  # no proposal: the way every node-level approval is created today
            session.add(
                CaliberRuntimeApprovalRequest(
                    runtime_approval_id="RAP-legacy",
                    workflow_run_id="RUN-ep",
                    project_id="PRJ-ep",
                    node_id="gate",
                    status="pending",
                    policy_snapshot={"timeout_behavior": "block"},
                )
            )
            session.commit()
        with factory() as session:  # and its lifecycle columns can move
            session.execute(
                text(
                    "UPDATE caliber_runtime_approval_requests SET status = 'superseded', superseded_by = 'RAP-next', "
                    "consumed_at = :at WHERE runtime_approval_id = 'RAP-legacy'"
                ),
                {"at": NAIVE_AT},
            )
            session.commit()
        assert _count(factory, CaliberRuntimeApprovalRequest) == 1


def _insert_refusals(proposal_id: str) -> dict[str, tuple[dict[str, Any], str]]:
    """Inserts the database must refuse, with the constraint that names why."""
    full = _bound_columns(proposal_id)
    cases: dict[str, tuple[dict[str, Any], str]] = {}
    for missing in ("intent_hash", "evidence_hash", "policy_digest", "expires_at", "requested_by"):
        cases[f"a proposal without its {missing}"] = (
            {k: v for k, v in full.items() if k != missing},
            "ck_runtime_approval_bound",
        )
    for present in _BINDING[1:]:  # the other direction: binding values with no proposal
        cases[f"{present} without a proposal"] = (
            {present: full[present]},
            "ck_runtime_approval_bound",
        )
    cases["every value but the proposal"] = (
        {k: v for k, v in full.items() if k != "proposal_id"},
        "ck_runtime_approval_bound",
    )
    for column, length in (
        ("intent_hash", 64),
        ("intent_hash", 72),
        ("evidence_hash", 70),
        ("policy_digest", 72),
        ("policy_digest", 0),
    ):
        cases[f"a {length}-character {column}"] = (
            {**full, column: "x" * length},
            "ck_runtime_approval_digest_length|too long",  # PostgreSQL's own varchar(71) refuses a longer value first
        )
    return cases


@pytest.mark.parametrize("dialect", DIALECTS)
def test_raw_sql_cannot_store_a_half_bound_or_mis_sized_request(
    dialect: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with _database(dialect, tmp_path, monkeypatch) as (_, factory):
        _seed_run(factory)
        with factory() as session:
            proposal_id = _proposal(session).proposal_id
            session.commit()
        for number, (values, constraint) in enumerate(_insert_refusals(proposal_id).values()):
            with factory() as session, pytest.raises(DBAPIError, match=constraint):
                _insert(session, runtime_approval_id=f"RAP-refused-{number}", **values)
        with factory() as session:  # a complete, correctly sized binding is accepted
            _insert(session, **_bound_columns(proposal_id))
        assert _count(factory, CaliberRuntimeApprovalRequest) == 1
        if dialect == "postgresql":  # SQLite does not enforce foreign keys in this repository
            with factory() as session, pytest.raises(DBAPIError, match="foreign key"):
                _insert(session, runtime_approval_id="RAP-orphan", **_bound_columns("prop-none"))


@pytest.mark.parametrize("dialect", DIALECTS)
def test_raw_sql_cannot_rebind_or_bind_after_the_fact(
    dialect: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with _database(dialect, tmp_path, monkeypatch) as (_, factory):
        _seed_run(factory)
        with factory() as session:
            first = _proposal(session, node="first").proposal_id
            second = _proposal(session, node="second").proposal_id
            first_proposal = session.get(CaliberEffectProposal, first)
            assert first_proposal is not None
            request_id = _create(session, first_proposal).runtime_approval_id
            _insert(session, runtime_approval_id="RAP-legacy")  # commits both
        with factory() as session:
            stored = session.get(CaliberRuntimeApprovalRequest, request_id)
            assert stored is not None
            before = {column: getattr(stored, column) for column in _BINDING}
        changes: dict[str, Any] = {
            "proposal_id": second,
            "intent_hash": "sha256:" + "c" * 64,
            "evidence_hash": "sha256:" + "d" * 64,
            "policy_digest": "sha256:" + "d" * 64,
            "expected_resource_version": "8",
            "expires_at": NAIVE_AT + timedelta(days=30),
            "requested_by": "someone_else",
        }
        for column, value in changes.items():  # every binding column is frozen, one at a time
            with factory() as session, pytest.raises(DBAPIError, match="frozen"):
                session.execute(
                    text(
                        f"UPDATE caliber_runtime_approval_requests SET {column} = :v WHERE runtime_approval_id = :i"
                    ),
                    {"v": value, "i": request_id},
                )
        with factory() as session, pytest.raises(DBAPIError):  # a NULL is a change too
            session.execute(
                text(
                    "UPDATE caliber_runtime_approval_requests SET expected_resource_version = NULL WHERE runtime_approval_id = :i"
                ),
                {"i": request_id},
            )
        with (
            factory() as session,
            pytest.raises(DBAPIError, match="frozen"),
        ):  # a legacy row cannot be bound afterwards
            session.execute(
                text(
                    "UPDATE caliber_runtime_approval_requests SET proposal_id = :p, intent_hash = :h, evidence_hash = :e, "
                    "policy_digest = :d, expires_at = :x, requested_by = 'editor_a' WHERE runtime_approval_id = 'RAP-legacy'"
                ),
                {
                    "p": second,
                    "h": "sha256:" + "a" * 64,
                    "e": EVIDENCE,
                    "d": "sha256:" + "b" * 64,
                    "x": NAIVE_AT,
                },
            )
        with factory() as session:  # nothing moved
            stored = session.get(CaliberRuntimeApprovalRequest, request_id)
            assert stored is not None
            assert {column: getattr(stored, column) for column in _BINDING} == before


@pytest.mark.parametrize("dialect", DIALECTS)
def test_the_lifecycle_columns_still_move_and_a_no_op_write_of_the_binding_is_allowed(
    dialect: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with _database(dialect, tmp_path, monkeypatch) as (_, factory):
        _seed_run(factory)
        with factory() as session:
            request_id = _create(session, _proposal(session)).runtime_approval_id
            session.commit()
        with factory() as session:
            request = session.get(CaliberRuntimeApprovalRequest, request_id)
            assert request is not None
            # What the decision route and the later steps write: status, decision columns, the quorum in the snapshot,
            # supersession and consumption.
            request.status = "approved"
            request.decided_at = NAIVE_AT
            request.decided_by = "approver_b"
            request.decision_reason = "looks right"
            request.policy_snapshot = {
                **(request.policy_snapshot or {}),
                "approvals": ["approver_b"],
            }
            request.superseded_by = "RAP-next"
            request.consumed_at = NAIVE_AT
            session.commit()
        with factory() as session:
            session.execute(
                text(
                    "UPDATE caliber_runtime_approval_requests SET intent_hash = intent_hash, requested_by = requested_by, "
                    "expires_at = expires_at, proposal_id = proposal_id WHERE runtime_approval_id = :i"
                ),
                {"i": request_id},
            )
            session.commit()
        with factory() as session:
            stored = session.get(CaliberRuntimeApprovalRequest, request_id)
            assert stored is not None
            assert (stored.status, stored.decided_by, stored.superseded_by, stored.consumed_at) == (
                "approved",
                "approver_b",
                "RAP-next",
                NAIVE_AT,
            )
            # the digest still pins the policy as requested, not the approvers that accumulated in the snapshot
            assert stored.policy_digest == policy_digest(ApprovalPolicy())


def _columns(factory: sessionmaker[Session]) -> set[str]:
    with factory() as session:
        return {
            c["name"]
            for c in inspect(session.get_bind()).get_columns("caliber_runtime_approval_requests")
        }


def _triggers(factory: sessionmaker[Session], dialect: str) -> set[str]:
    with factory() as session:
        if dialect == "sqlite":
            query = "SELECT name FROM sqlite_master WHERE type = 'trigger' AND tbl_name = 'caliber_runtime_approval_requests'"
        else:
            query = (
                "SELECT tgname FROM pg_trigger WHERE tgrelid = 'caliber_runtime_approval_requests'::regclass "
                "AND NOT tgisinternal"
            )
        return {row[0] for row in session.execute(text(query))}


@pytest.mark.parametrize("dialect", DIALECTS)
def test_migration_0127_keeps_legacy_rows_and_downgrades_cleanly(
    dialect: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    binding = {*_BINDING, "superseded_by", "consumed_at"}
    trigger = "trg_caliber_runtime_approval_requests_frozen"
    with dialect_database_url(dialect, tmp_path, "approval_binding") as url:
        monkeypatch.setenv("CALIBER_DATABASE_URL", url)
        monkeypatch.chdir(PROJECT_ROOT)
        config = Config(str(ALEMBIC_INI))
        command.upgrade(config, "0126")
        engine = create_engine(url)
        factory = sessionmaker(engine, expire_on_commit=False)
        try:
            assert not binding & _columns(factory)
            assert not _triggers(factory, dialect)
            _seed_run(factory)
            with factory() as session:
                _insert(session, runtime_approval_id="RAP-old")
            command.upgrade(config, "head")
            assert binding <= _columns(factory)
            assert _triggers(factory, dialect) == {trigger}
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
            assert not _triggers(
                factory, dialect
            )  # the trigger (and on PostgreSQL its function) are gone
            if dialect == "postgresql":
                with factory() as session:
                    assert (
                        session.execute(
                            text(
                                "SELECT count(*) FROM pg_proc WHERE proname = 'caliber_runtime_approvals_frozen'"
                            )
                        ).scalar_one()
                        == 0
                    )
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
            assert _triggers(factory, dialect) == {trigger}
        finally:
            engine.dispose()
