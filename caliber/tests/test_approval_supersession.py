"""Expiry and supersession of a bound approval request (B06.2, #712).

``approval_refusal`` is the one pure rule that says why a request can no longer be decided or consumed (superseded first, then
expired at or after ``expires_at``); ``create_approval_request`` supersedes the live bound requests of the same
``(workflow_run_id, node_id)`` in the transaction that inserts the replacement, under a lock on the run row and then the
proposal row; ``approval_ttl_seconds`` is the deployment's default window. Database cases run on SQLite and, where
``CALIBER_TEST_POSTGRES_URL`` is set, on PostgreSQL migrated by Alembic. Time is a fake clock; the race tests poll
``pg_stat_activity`` or use ``lock_timeout`` and never sleep to make a race happen. The route (``409 expired`` /
``superseded``) is exercised in ``tests/egi_scenarios/test_v04_stale_approval.py``.
"""

from __future__ import annotations

import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session, sessionmaker

from caliber.config import CaliberConfig, ConfigError
from caliber.db.models import (
    CaliberEffectProposal,
    CaliberRuntimeApprovalRequest,
    CaliberWorkflowRun,
)
from caliber.effects.records import APPROVED, POLICY_DECISION, Approval, record_proposal
from caliber.workflows.approval_policy import (
    REFUSAL_EXPIRED,
    REFUSAL_SUPERSEDED,
    STATUS_SUPERSEDED,
    ApprovalPolicy,
    approval_refusal,
)
from caliber.workflows.approval_requests import (
    MAX_TTL_SECONDS,
    ApprovalRequestError,
    configured_ttl_seconds,
    create_approval_request,
)
from tests.dialect_helpers import DIALECTS
from tests.test_approval_binding import EVIDENCE
from tests.test_effect_proposals import _intent, _seed_run
from tests.test_effect_records import AT, NAIVE_AT, _decision, _move, _need_postgresql
from tests.test_event_outbox import _count, _database

# ---------------------------------------------------------------- the pure rule

EXPIRES = NAIVE_AT + timedelta(hours=1)


def _refusal(**overrides: Any) -> str | None:
    fields: dict[str, Any] = {
        "status": "pending",
        "superseded_by": None,
        "expires_at": EXPIRES,
        "now": AT,
    }
    fields.update(overrides)
    return approval_refusal(**fields)


def test_the_codes_are_the_stable_strings_the_route_and_the_commit_recheck_return() -> None:
    assert (REFUSAL_EXPIRED, REFUSAL_SUPERSEDED, STATUS_SUPERSEDED) == (
        "expired",
        "superseded",
        "superseded",
    )


def test_a_live_request_has_no_refusal_up_to_the_last_instant_before_expiry() -> None:
    assert _refusal() is None
    assert _refusal(now=EXPIRES.replace(tzinfo=timezone.utc) - timedelta(microseconds=1)) is None


def test_the_window_is_half_open_so_the_expiry_instant_itself_is_refused() -> None:
    at_expiry = EXPIRES.replace(tzinfo=timezone.utc)
    assert _refusal(now=at_expiry) == REFUSAL_EXPIRED
    assert _refusal(now=at_expiry + timedelta(microseconds=1)) == REFUSAL_EXPIRED
    assert _refusal(now=at_expiry + timedelta(days=400)) == REFUSAL_EXPIRED


def test_the_clock_may_be_naive_utc_or_any_offset_and_the_instant_is_what_counts() -> None:
    plus_two = timezone(timedelta(hours=2))
    expiry = EXPIRES.replace(tzinfo=timezone.utc)
    # The same instant written three ways is refused three ways; one microsecond earlier is not.
    for clock in (expiry, expiry.astimezone(plus_two), EXPIRES):
        assert _refusal(now=clock) == REFUSAL_EXPIRED
    for clock in (
        expiry - timedelta(microseconds=1),
        (expiry - timedelta(microseconds=1)).astimezone(plus_two),
        EXPIRES - timedelta(microseconds=1),
    ):
        assert _refusal(now=clock) is None
    # An aware expiry (never stored, but a caller may hold one) compares as the same instant.
    assert _refusal(expires_at=expiry.astimezone(plus_two), now=expiry) == REFUSAL_EXPIRED


def test_a_request_with_no_expiry_never_expires() -> None:
    assert _refusal(expires_at=None, now=AT + timedelta(days=36500)) is None


@pytest.mark.parametrize(
    ("status", "superseded_by"),
    [
        ("superseded", None),
        ("superseded", "RAP-new"),
        ("pending", "RAP-new"),
        ("approved", "RAP-n"),
    ],
)
def test_a_replaced_request_is_superseded_whatever_its_status_says(
    status: str, superseded_by: str | None
) -> None:
    assert _refusal(status=status, superseded_by=superseded_by) == REFUSAL_SUPERSEDED


def test_superseded_is_reported_before_expired_and_a_decided_request_is_not_a_refusal_by_itself() -> (
    None
):
    late = EXPIRES.replace(tzinfo=timezone.utc) + timedelta(seconds=1)
    assert _refusal(status="superseded", now=late) == REFUSAL_SUPERSEDED
    assert _refusal(superseded_by="RAP-new", now=late) == REFUSAL_SUPERSEDED
    # Status alone is not this rule's business (an approved or rejected request is refused for being decided elsewhere); only
    # supersession and the clock are.
    for status in ("approved", "rejected", "pending", None):
        assert _refusal(status=status) is None
        assert _refusal(status=status, now=late) == REFUSAL_EXPIRED


def test_a_blank_superseded_by_is_not_a_supersession() -> None:
    assert _refusal(superseded_by="") is None


# ---------------------------------------------------------------- the configured window


def test_the_default_window_is_a_day_and_the_environment_sets_it() -> None:
    assert CaliberConfig.load(environ={}).approval_ttl_seconds == 86400
    assert (
        CaliberConfig.load(environ={"CALIBER_APPROVAL_TTL_SECONDS": "3600"}).approval_ttl_seconds
        == 3600
    )


@pytest.mark.parametrize("value", ["1", "31536000"])
def test_the_configured_window_accepts_its_bounds(value: str) -> None:
    config = CaliberConfig.load(environ={"CALIBER_APPROVAL_TTL_SECONDS": value})
    assert config.approval_ttl_seconds == int(value)
    assert configured_ttl_seconds(config) == int(value)


@pytest.mark.parametrize("value", ["0", "-5", "31536001"])
def test_a_window_outside_the_bounds_stops_startup_naming_the_setting(value: str) -> None:
    with pytest.raises(ConfigError, match="approval_ttl_seconds"):
        CaliberConfig.load(environ={"CALIBER_APPROVAL_TTL_SECONDS": value})


@pytest.mark.parametrize("value", ["soon", "1.5", ""])
def test_a_window_that_is_not_an_integer_stops_startup(value: str) -> None:
    # The ValueError every integer setting raises for text that is not an integer.
    with pytest.raises(ValueError, match="int"):
        CaliberConfig.load(environ={"CALIBER_APPROVAL_TTL_SECONDS": value})


def test_the_configuration_ceiling_is_the_writers_ceiling() -> None:
    field = CaliberConfig.model_fields["approval_ttl_seconds"]
    ceilings = [m.le for m in field.metadata if hasattr(m, "le")]
    assert ceilings == [MAX_TTL_SECONDS]


@pytest.mark.parametrize("value", [0, -1, MAX_TTL_SECONDS + 1, True, "60", 60.0, None])
def test_a_hand_built_config_cannot_hand_the_writer_a_window_it_would_refuse(value: Any) -> None:
    with pytest.raises(ApprovalRequestError) as refused:
        configured_ttl_seconds(SimpleNamespace(approval_ttl_seconds=value))
    assert refused.value.code == "bad_field"
    assert "approval_ttl_seconds" in str(refused.value)


def test_a_config_without_the_setting_is_refused_not_defaulted() -> None:
    with pytest.raises(ApprovalRequestError, match="approval_ttl_seconds"):
        configured_ttl_seconds(SimpleNamespace())


@pytest.mark.parametrize("dialect", DIALECTS)
def test_a_request_expires_the_configured_window_after_it_was_requested(
    dialect: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = CaliberConfig.load(environ={"CALIBER_APPROVAL_TTL_SECONDS": "5400"})
    with _database(dialect, tmp_path, monkeypatch) as (_, factory):
        _seed_run(factory)
        with factory() as session:
            request = _request(session, ttl_seconds=configured_ttl_seconds(config))
            session.commit()
            assert request.expires_at == NAIVE_AT + timedelta(seconds=5400)
            assert (
                approval_refusal(
                    status=request.status,
                    superseded_by=request.superseded_by,
                    expires_at=request.expires_at,
                    now=AT + timedelta(seconds=5399),
                )
                is None
            )
            assert (
                approval_refusal(
                    status=request.status,
                    superseded_by=request.superseded_by,
                    expires_at=request.expires_at,
                    now=AT + timedelta(seconds=5400),
                )
                == REFUSAL_EXPIRED
            )


# ---------------------------------------------------------------- supersession


def _proposal(
    session: Session,
    *,
    run: str = "RUN-ep",
    node: str = "ask",
    occurrence: str = "occ-1",
    **intent: Any,
) -> CaliberEffectProposal:
    recorded = record_proposal(
        session,
        workflow_run_id=run,
        node_id=node,
        occurrence_id=occurrence,
        intent=_intent(**intent),
    )
    proposal = session.get(CaliberEffectProposal, recorded.proposal_id)
    assert proposal is not None
    return proposal


def _request(
    session: Session,
    proposal: CaliberEffectProposal | None = None,
    **overrides: Any,
) -> CaliberRuntimeApprovalRequest:
    fields: dict[str, Any] = {
        "policy": ApprovalPolicy(),
        "evidence_hash": EVIDENCE,
        "requested_by": "editor_a",
        "ttl_seconds": 3600,
        "now": AT,
    }
    fields.update(overrides)
    return create_approval_request(session, proposal=proposal or _proposal(session), **fields)


def _state(
    factory: sessionmaker[Session], *ids: str
) -> dict[str, tuple[str, str | None, datetime | None]]:
    with factory() as session:
        rows = session.execute(
            select(CaliberRuntimeApprovalRequest).where(
                CaliberRuntimeApprovalRequest.runtime_approval_id.in_(ids)
            )
        ).scalars()
        return {r.runtime_approval_id: (r.status, r.superseded_by, r.consumed_at) for r in rows}


def _set(factory: sessionmaker[Session], request_id: str, **values: Any) -> None:
    with factory() as session:
        row = session.get(CaliberRuntimeApprovalRequest, request_id)
        assert row is not None
        for name, value in values.items():
            setattr(row, name, value)
        session.commit()


def _add_run(factory: sessionmaker[Session], run_id: str) -> None:
    with factory() as session:
        session.add(
            CaliberWorkflowRun(
                workflow_run_id=run_id, workflow_id="wf-ep", project_id="PRJ-ep", status="running"
            )
        )
        session.commit()


@pytest.mark.parametrize("dialect", DIALECTS)
def test_a_new_request_for_the_same_proposal_supersedes_the_older_one(
    dialect: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with _database(dialect, tmp_path, monkeypatch) as (_, factory):
        _seed_run(factory)
        with factory() as session:
            proposal = _proposal(session)
            first = _request(session, proposal)
            session.commit()
            first_id = first.runtime_approval_id
            before = {
                "intent_hash": first.intent_hash,
                "evidence_hash": first.evidence_hash,
                "policy_digest": first.policy_digest,
                "expires_at": first.expires_at,
                "requested_by": first.requested_by,
                "requested_at": first.requested_at,
                "proposal_id": first.proposal_id,
            }
        with factory() as session:
            again = session.get(CaliberEffectProposal, proposal.proposal_id)
            assert again is not None
            second = _request(session, again, now=AT + timedelta(minutes=5))
            session.commit()
            second_id = second.runtime_approval_id
            assert (second.status, second.superseded_by) == ("pending", None)
        assert second_id != first_id
        assert _state(factory, first_id, second_id) == {
            first_id: ("superseded", second_id, None),
            second_id: ("pending", None, None),
        }
        with factory() as session:
            older = session.get(CaliberRuntimeApprovalRequest, first_id)
            assert older is not None
            # Only the lifecycle markers moved: the binding the approver saw is intact, and no decision was recorded.
            assert {name: getattr(older, name) for name in before} == before
            assert (older.decided_at, older.decided_by, older.decision_reason) == (None, None, None)
            assert (older.policy_snapshot or {}).get("approvals") is None


@pytest.mark.parametrize("dialect", DIALECTS)
def test_a_request_for_a_changed_intent_of_the_same_node_supersedes_the_older_one(
    dialect: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with _database(dialect, tmp_path, monkeypatch) as (_, factory):
        _seed_run(factory)
        with factory() as session:
            first = _request(session, _proposal(session, occurrence="occ-1"))
            changed = _proposal(session, occurrence="occ-2", expected_resource_version="8")
            assert changed.intent_hash != first.intent_hash
            second = _request(session, changed)
            session.commit()
            first_id, second_id = first.runtime_approval_id, second.runtime_approval_id
        assert _state(factory, first_id, second_id) == {
            first_id: ("superseded", second_id, None),
            second_id: ("pending", None, None),
        }


@pytest.mark.parametrize("dialect", DIALECTS)
def test_a_request_for_another_node_or_another_run_never_supersedes_this_one(
    dialect: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with _database(dialect, tmp_path, monkeypatch) as (_, factory):
        _seed_run(factory)
        _add_run(factory, "RUN-other")
        with factory() as session:
            mine = _request(session, _proposal(session, node="ask"))
            other_node = _request(session, _proposal(session, node="review"))
            other_run = _request(session, _proposal(session, run="RUN-other", node="ask"))
            # Even an identical intent in another run is another request.
            same_intent_other_run = _request(
                session, _proposal(session, run="RUN-other", node="ask", occurrence="occ-2")
            )
            session.commit()
            ids = [
                r.runtime_approval_id for r in (mine, other_node, other_run, same_intent_other_run)
            ]
        states = _state(factory, *ids)
        assert states[ids[0]] == ("pending", None, None)
        assert states[ids[1]] == ("pending", None, None)
        assert states[ids[2]] == (
            "superseded",
            ids[3],
            None,
        )  # same run and node as ids[3]: replaced
        assert states[ids[3]] == ("pending", None, None)


@pytest.mark.parametrize("dialect", DIALECTS)
def test_an_approved_but_unconsumed_request_is_superseded_and_a_chain_points_at_each_successor(
    dialect: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with _database(dialect, tmp_path, monkeypatch) as (_, factory):
        _seed_run(factory)
        with factory() as session:
            first = _request(session, _proposal(session, occurrence="occ-1"))
            session.commit()
            first_id = first.runtime_approval_id
        _set(factory, first_id, status="approved", decided_at=NAIVE_AT, decided_by="approver_a")
        with factory() as session:
            second = _request(session, _proposal(session, occurrence="occ-2"))
            session.commit()
            second_id = second.runtime_approval_id
        with factory() as session:
            third = _request(session, _proposal(session, occurrence="occ-3"))
            session.commit()
            third_id = third.runtime_approval_id
        assert _state(factory, first_id, second_id, third_id) == {
            first_id: ("superseded", second_id, None),  # not rewritten when the next one came
            second_id: ("superseded", third_id, None),
            third_id: ("pending", None, None),
        }
        with factory() as session:
            first_row = session.get(CaliberRuntimeApprovalRequest, first_id)
            assert first_row is not None
            # The decision that was recorded stays on the record; it is simply no longer usable.
            assert (first_row.decided_by, first_row.decided_at) == ("approver_a", NAIVE_AT)
            assert (
                approval_refusal(
                    status=first_row.status,
                    superseded_by=first_row.superseded_by,
                    expires_at=first_row.expires_at,
                    now=AT,
                )
                == REFUSAL_SUPERSEDED
            )


@pytest.mark.parametrize("dialect", DIALECTS)
def test_requests_that_are_finished_or_not_bound_are_left_alone(
    dialect: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with _database(dialect, tmp_path, monkeypatch) as (_, factory):
        _seed_run(factory)

        def create(occurrence: str) -> str:
            with factory() as session:
                request = _request(session, _proposal(session, occurrence=occurrence))
                session.commit()
                return request.runtime_approval_id

        # Each request is finished before the next is created (creating one supersedes the previous live request).
        rejected_id = create("occ-r")
        _set(factory, rejected_id, status="rejected", decided_at=NAIVE_AT, decided_by="approver_a")
        consumed_id = create("occ-c")
        _set(factory, consumed_id, status="approved", consumed_at=NAIVE_AT)
        # A row that says it was superseded keeps its successor even if its status was set back by hand: never rewritten.
        inconsistent_id = create("occ-i")
        _set(factory, inconsistent_id, status="approved", superseded_by="RAP-earlier")
        replaced_id = create("occ-s")
        # A legacy, node-level request guards no intent a change could void: pending and approved ones both stay.
        with factory() as session:
            for legacy_id, status in (("RAP-legacy-p", "pending"), ("RAP-legacy-a", "approved")):
                session.add(
                    CaliberRuntimeApprovalRequest(
                        runtime_approval_id=legacy_id,
                        workflow_run_id="RUN-ep",
                        project_id="PRJ-ep",
                        node_id="ask",
                        status=status,
                        requested_at=NAIVE_AT,
                        policy_snapshot={"timeout_behavior": "block"},
                    )
                )
            session.commit()
        newest_id = create("occ-new")
        states = _state(
            factory, rejected_id, consumed_id, replaced_id, "RAP-legacy-p", "RAP-legacy-a"
        )
        assert _state(factory, inconsistent_id)[inconsistent_id][1] == "RAP-earlier"
        assert states[rejected_id] == ("rejected", None, None)
        assert states[consumed_id] == ("approved", None, NAIVE_AT)
        assert states["RAP-legacy-p"] == ("pending", None, None)
        assert states["RAP-legacy-a"] == ("approved", None, None)
        # ... while the one live bound request was replaced.
        assert states[replaced_id] == ("superseded", newest_id, None)


@pytest.mark.parametrize("dialect", DIALECTS)
def test_an_expired_request_is_superseded_like_any_other_live_one(
    dialect: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with _database(dialect, tmp_path, monkeypatch) as (_, factory):
        _seed_run(factory)
        with factory() as session:
            old = _request(session, _proposal(session, occurrence="occ-1"), ttl_seconds=60)
            session.commit()
            old_id = old.runtime_approval_id
        with factory() as session:
            new = _request(
                session, _proposal(session, occurrence="occ-2"), now=AT + timedelta(days=2)
            )
            session.commit()
            new_id = new.runtime_approval_id
        assert _state(factory, old_id)[old_id] == ("superseded", new_id, None)


@pytest.mark.parametrize("dialect", DIALECTS)
def test_supersession_is_atomic_with_the_new_request(
    dialect: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with _database(dialect, tmp_path, monkeypatch) as (_, factory):
        _seed_run(factory)
        with factory() as session:
            old = _request(session, _proposal(session, occurrence="occ-1"))
            session.commit()
            old_id = old.runtime_approval_id
        with factory() as session:
            new = _request(session, _proposal(session, occurrence="occ-2"))
            new_id = new.runtime_approval_id
            session.rollback()  # the caller's transaction fails after the writer returned
        assert _state(factory, old_id) == {old_id: ("pending", None, None)}
        assert _count(factory, CaliberRuntimeApprovalRequest) == 1
        with factory() as session:
            assert session.get(CaliberRuntimeApprovalRequest, new_id) is None


@pytest.mark.parametrize("dialect", DIALECTS)
def test_a_refused_request_supersedes_nothing(
    dialect: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with _database(dialect, tmp_path, monkeypatch) as (_, factory):
        _seed_run(factory)
        with factory() as session:
            old = _request(session, _proposal(session, occurrence="occ-1"))
            session.commit()
            old_id = old.runtime_approval_id
        with factory() as session:
            later = _proposal(session, occurrence="occ-2")
            with pytest.raises(ApprovalRequestError, match="requested_by"):
                _request(session, later, requested_by="")
            with pytest.raises(ApprovalRequestError, match="policy"):
                _request(session, later, policy=None)
            with pytest.raises(ApprovalRequestError, match="ttl_seconds"):
                _request(session, later, ttl_seconds=0)
            session.commit()
        with (
            factory() as session
        ):  # a proposal that has already moved on cannot be asked about either
            read = _proposal(session, occurrence="occ-read", effect_class="read")
            _move(
                session,
                read.proposal_id,
                APPROVED,
                approval=Approval(POLICY_DECISION, _decision(session, read.proposal_id)),
            )
            with pytest.raises(ApprovalRequestError) as refused:
                _request(session, read)
            assert refused.value.code == "proposal_not_proposed"
            session.rollback()
        assert _state(factory, old_id) == {old_id: ("pending", None, None)}
        assert _count(factory, CaliberRuntimeApprovalRequest) == 1


# ---------------------------------------------------------------- concurrency (PostgreSQL)


def _blocked_on_a_lock(factory: sessionmaker[Session]) -> bool:
    """Whether a session of this database is waiting for a lock (matched by wait event, not by statement text: PostgreSQL
    truncates a long statement in ``pg_stat_activity``, and the ``FOR UPDATE`` of a wide select is cut off)."""
    with factory() as observer:
        return bool(
            observer.execute(
                text(
                    "SELECT count(*) FROM pg_stat_activity WHERE datname = current_database() "
                    "AND wait_event_type = 'Lock'"
                )
            ).scalar_one()
        )


def _node_proposals(factory: sessionmaker[Session], count: int) -> list[str]:
    _seed_run(factory)
    with factory() as session:
        ids = [
            _proposal(session, occurrence=f"occ-{i}", expected_resource_version=str(i)).proposal_id
            for i in range(count)
        ]
        session.commit()
    return ids


def test_creators_for_different_proposals_of_one_node_serialize_on_the_run_on_postgresql(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Different proposals lock different proposal rows: only the run lock stops each creator reading the other's request as
    absent and leaving two live ones. The second creator waits for the first one's transaction, then supersedes its request."""
    _need_postgresql()
    with _database("postgresql", tmp_path, monkeypatch) as (_, factory):
        first_id, second_id = _node_proposals(factory, 2)
        with factory() as first, factory() as second:
            proposal_one = first.get(CaliberEffectProposal, first_id)
            proposal_two = second.get(CaliberEffectProposal, second_id)
            assert proposal_one is not None and proposal_two is not None
            held = _request(first, proposal_one)  # flushed, not committed: the run row is locked
            second.execute(text("SET LOCAL lock_timeout = '200ms'"))
            with pytest.raises(DBAPIError, match="lock timeout"):
                _request(second, proposal_two)
            second.rollback()
            first.commit()
            held_id = held.runtime_approval_id
        with factory() as session:
            proposal_two = session.get(CaliberEffectProposal, second_id)
            assert proposal_two is not None
            newer = _request(session, proposal_two, now=AT + timedelta(minutes=1))
            session.commit()
            newer_id = newer.runtime_approval_id
        assert _state(factory, held_id, newer_id) == {
            held_id: ("superseded", newer_id, None),
            newer_id: ("pending", None, None),
        }


def test_racing_creators_for_one_node_leave_exactly_one_live_request_on_postgresql(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workers = 6
    with _database("postgresql", tmp_path, monkeypatch) as (_, factory):
        proposal_ids = _node_proposals(factory, workers)
        barrier = threading.Barrier(workers)
        created: list[str] = []
        errors: list[BaseException] = []
        guard = threading.Lock()

        def run(proposal_id: str) -> None:
            try:
                with factory() as session:
                    proposal = session.get(CaliberEffectProposal, proposal_id)
                    assert proposal is not None
                    barrier.wait(10)
                    request = _request(session, proposal)
                    session.commit()
                with guard:
                    created.append(request.runtime_approval_id)
            except BaseException as exc:
                with guard:
                    errors.append(exc)

        threads = [threading.Thread(target=run, args=(pid,), daemon=True) for pid in proposal_ids]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(60)
        assert not errors, errors
        assert len(set(created)) == workers
        with factory() as session:
            rows = list(session.execute(select(CaliberRuntimeApprovalRequest)).scalars())
        live = [r for r in rows if r.status == "pending"]
        assert len(live) == 1 and live[0].superseded_by is None
        replaced = [r for r in rows if r is not live[0]]
        assert all(r.status == "superseded" for r in replaced)
        successors = [r.superseded_by for r in replaced]
        assert all(s in {r.runtime_approval_id for r in rows} for s in successors)
        assert all(r.superseded_by != r.runtime_approval_id for r in replaced)
        # A chain: each request was replaced by exactly one successor, no two by the same one.
        assert len(set(successors)) == len(replaced) == workers - 1


def test_a_request_consumed_while_a_creator_waits_is_not_overwritten_on_postgresql(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The older request is read ``FOR UPDATE`` with the predicate in the read: a consumer that holds it finishes first, and the
    supersession then does not see it (a consumed request stays approved)."""
    _need_postgresql()
    with _database("postgresql", tmp_path, monkeypatch) as (_, factory):
        first_id, second_id = _node_proposals(factory, 2)
        with factory() as session:
            proposal = session.get(CaliberEffectProposal, first_id)
            assert proposal is not None
            old = _request(session, proposal)
            session.commit()
            old_id = old.runtime_approval_id
        _set(factory, old_id, status="approved")
        result: dict[str, Any] = {}

        def create() -> None:
            try:
                with factory() as session:
                    proposal = session.get(CaliberEffectProposal, second_id)
                    assert proposal is not None
                    result["new_id"] = _request(session, proposal).runtime_approval_id
                    session.commit()
            except BaseException as exc:
                result["error"] = exc

        with factory() as consumer:
            row = consumer.execute(
                select(CaliberRuntimeApprovalRequest)
                .where(CaliberRuntimeApprovalRequest.runtime_approval_id == old_id)
                .with_for_update()
            ).scalar_one()
            row.consumed_at = NAIVE_AT  # the dispatcher's claim, not yet committed
            consumer.flush()
            creator = threading.Thread(target=create, daemon=True)
            creator.start()
            deadline = time.monotonic() + 15
            while not _blocked_on_a_lock(factory) and time.monotonic() < deadline:
                time.sleep(0.02)  # polling for the lock wait, not waiting for a race to happen
            assert _blocked_on_a_lock(factory), result
            assert creator.is_alive() and "new_id" not in result
            consumer.commit()
        creator.join(15)
        assert not creator.is_alive()
        assert "error" not in result, result
        assert _state(factory, old_id, result["new_id"]) == {
            old_id: ("approved", None, NAIVE_AT),
            result["new_id"]: ("pending", None, None),
        }


def test_a_decision_in_flight_finishes_first_and_is_then_superseded_on_postgresql(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A decider that holds the older request (``FOR UPDATE``) and approves it is committed before the supersession: the
    approved-but-unconsumed request ends ``superseded``, never ``approved`` over a newer pending one."""
    _need_postgresql()
    with _database("postgresql", tmp_path, monkeypatch) as (_, factory):
        first_id, second_id = _node_proposals(factory, 2)
        with factory() as session:
            proposal = session.get(CaliberEffectProposal, first_id)
            assert proposal is not None
            old_id = _request(session, proposal).runtime_approval_id
            session.commit()
        result: dict[str, Any] = {}

        def create() -> None:
            try:
                with factory() as session:
                    proposal = session.get(CaliberEffectProposal, second_id)
                    assert proposal is not None
                    result["new_id"] = _request(session, proposal).runtime_approval_id
                    session.commit()
            except BaseException as exc:
                result["error"] = exc

        with factory() as decider:
            row = decider.execute(
                select(CaliberRuntimeApprovalRequest)
                .where(CaliberRuntimeApprovalRequest.runtime_approval_id == old_id)
                .with_for_update()
            ).scalar_one()
            assert (
                approval_refusal(  # what the decider sees under its lock
                    status=row.status,
                    superseded_by=row.superseded_by,
                    expires_at=row.expires_at,
                    now=AT,
                )
                is None
            )
            row.status = "approved"
            row.decided_by = "approver_a"
            decider.flush()
            creator = threading.Thread(target=create, daemon=True)
            creator.start()
            deadline = time.monotonic() + 15
            while not _blocked_on_a_lock(factory) and time.monotonic() < deadline:
                time.sleep(0.02)
            assert _blocked_on_a_lock(factory), result
            decider.commit()
        creator.join(15)
        assert not creator.is_alive()
        assert "error" not in result, result
        assert _state(factory, old_id, result["new_id"]) == {
            old_id: ("superseded", result["new_id"], None),
            result["new_id"]: ("pending", None, None),
        }
        # A decider arriving afterwards reads the committed outcome under its lock and is told so.
        with factory() as late:
            row = late.execute(
                select(CaliberRuntimeApprovalRequest)
                .where(CaliberRuntimeApprovalRequest.runtime_approval_id == old_id)
                .with_for_update()
            ).scalar_one()
            assert (
                approval_refusal(
                    status=row.status,
                    superseded_by=row.superseded_by,
                    expires_at=row.expires_at,
                    now=AT,
                )
                == REFUSAL_SUPERSEDED
            )
