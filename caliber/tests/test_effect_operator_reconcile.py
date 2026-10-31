"""B05.5d (#997): the operator's one action on an effect proposal is "ask the downstream now", owner-fenced and audited.

An operator can never say what happened to a proposal. What these tests pin:

* it asks under the lease the serving process holds, and a process without it, or with a lease taken over, records nothing;
* every request is audited with who, why and what came of it, whether or not the proposal moved;
* only the downstream's own evidence settles a proposal: a 404, a 5xx, no binding or an unreadable answer leave it ``unknown``,
  and nothing in the request body can make it ``committed`` or ``failed``;
* the operator view offers ``reconcile`` exactly where it can do something.
"""

from __future__ import annotations

import threading
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker
from starlette.testclient import TestClient

import caliber.routes.system_effects as routes_module
from caliber.db.models import CaliberAuditLog, CaliberEffectReceipt
from caliber.effects.operator_action import (
    ACTION,
    AUDIT_ACTION,
    MAX_REASON_CHARS,
    NotReconcilable,
    reconcile_as_operator,
)
from caliber.effects.operator_view import describe_sources, list_proposals, proposal_states
from caliber.effects.reconcile import (
    CONNECTORS,
    IN_FLIGHT,
    MOVED,
    NOT_BOUND,
    RECONCILED_COMMITTED,
    STILL_UNKNOWN,
    Answer,
)
from caliber.effects.records import (
    COMMITTED,
    DISPATCHED,
    FAILED,
    LEASE_LOST,
    PROPOSED,
    UNKNOWN,
    Intent,
    ProposalError,
    proposal_state,
    proposal_view,
    record_proposal,
)
from caliber.orchestrator.effect_reconciler import EffectReconcilerTask
from caliber.ownership import acquire, release
from tests.conftest import DEFAULT_TEST_USER
from tests.dialect_helpers import DIALECTS, POSTGRES_TEST_URL
from tests.effect_helpers import TTL_SECONDS, leased
from tests.test_effect_in_flight import _facts_for, _Scripted, _unknown_one
from tests.test_effect_proposals import _seed_run
from tests.test_effect_receipts import _proposal
from tests.test_effect_records import OWNER, _move, _receipt
from tests.test_event_outbox import _database
from tests.workflow_helpers import PREFIX

PROJECT = "PRJ-ep"
BASE_URL = "http://downstream.invalid"
TARGETS = {("document-request", PROJECT): BASE_URL}
BINDINGS = (
    '[{"connector": "document-request", "project_id": "PRJ-ep", "secret_source": "env:X", '
    f'"base_url": "{BASE_URL}"}}]'
)
OPERATOR = "@operator-on-call"
REASON = "ticket INC-4471: the downstream's owner confirms the request is visible"
AUDIT_KEYS = {
    "reason",
    "state_before",
    "outcome",
    "state",
    "detail",
    "owner_id",
    "fencing_token",
}


@pytest.fixture(params=DIALECTS)
def database(
    request: pytest.FixtureRequest, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Any:
    with _database(request.param, tmp_path, monkeypatch) as (engine, factory):
        _seed_run(factory)
        yield engine, factory


@pytest.fixture
def _fact_lookup(monkeypatch: pytest.MonkeyPatch) -> None:
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


def _audits(factory: sessionmaker[Session], action: str = AUDIT_ACTION) -> list[CaliberAuditLog]:
    with factory() as session:
        return list(
            session.execute(
                select(CaliberAuditLog).where(CaliberAuditLog.action == action)
            ).scalars()
        )


def _state(factory: sessionmaker[Session], proposal_id: str) -> Any:
    with factory() as session:
        return proposal_state(session, proposal_id)


def _key(factory: sessionmaker[Session], proposal_id: str) -> str:
    with factory() as session:
        return proposal_view(session, proposal_id).logical_key


def _unknown_and_lease(factory: sessionmaker[Session]) -> tuple[str, Any]:
    with factory() as session:
        proposal_id = _unknown_one(session, "occ-op")
        lease = leased(session, OWNER)
        session.commit()
    return proposal_id, lease


def _ask(
    factory: sessionmaker[Session],
    proposal_id: str,
    lease: Any,
    lookup: Any,
    *,
    targets: Any = TARGETS,
    reason: str = REASON,
    in_flight_seconds: float | None = None,
) -> Any:
    return reconcile_as_operator(
        factory,
        proposal_id,
        actor=OPERATOR,
        reason=reason,
        lease=lease,
        targets=targets,
        in_flight_seconds=in_flight_seconds,
        lookup=lookup,
    )


# ---------------------------------------------------------------- the action


def test_the_downstream_proving_the_effect_settles_it_and_the_request_is_audited(
    database: Any, _fact_lookup: None
) -> None:
    _, factory = database
    proposal_id, lease = _unknown_and_lease(factory)
    ask = _Scripted()
    ask.answers[_key(factory, proposal_id)] = Answer(200, {"found": True})

    result = _ask(factory, proposal_id, lease, ask)

    assert (result.outcome, result.state, result.settled) == (RECONCILED_COMMITTED, COMMITTED, True)
    assert _state(factory, proposal_id).state == COMMITTED
    (row,) = _audits(factory)
    assert (row.actor, row.entity_type, row.entity_id) == (OPERATOR, "effect_proposal", proposal_id)
    assert set(row.details) == AUDIT_KEYS, (
        "identifiers, outcome and the operator's reason: no payload"
    )
    assert row.details["reason"] == REASON and row.details["state_before"] == UNKNOWN
    assert (row.details["outcome"], row.details["state"]) == (RECONCILED_COMMITTED, COMMITTED)
    assert row.details["owner_id"] == lease.owner_id
    assert row.details["fencing_token"] == lease.fencing_token
    (settled,) = _audits(factory, "effect_reconciled")
    assert settled.actor == f"reconciler:{lease.owner_id}", (
        "the move itself is the reconciler's own record"
    )


@pytest.mark.parametrize(
    ("answer", "targets", "outcome", "detail"),
    [
        (Answer(404, {"detail": "not found"}), TARGETS, STILL_UNKNOWN, "http_404"),
        (Answer(500, {}), TARGETS, STILL_UNKNOWN, "http_500"),
        (Answer(401, {}), TARGETS, STILL_UNKNOWN, "http_401"),
        (Answer(200, {"not": "a stored request"}), TARGETS, "unverified", None),
        (Answer(404, {}), {}, NOT_BOUND, "document-request"),
    ],
)
def test_insufficient_evidence_leaves_the_proposal_unknown_and_never_infers_a_failure(
    database: Any,
    monkeypatch: pytest.MonkeyPatch,
    answer: Answer,
    targets: Any,
    outcome: str,
    detail: str | None,
) -> None:
    _, factory = database
    monkeypatch.setitem(
        CONNECTORS,
        "document-request",
        replace(
            CONNECTORS["document-request"], facts=lambda view, *, lookup_status, lookup_body: None
        ),
    )
    proposal_id, lease = _unknown_and_lease(factory)
    before = _state(factory, proposal_id)
    ask = _Scripted()
    ask.answers[_key(factory, proposal_id)] = answer

    result = _ask(factory, proposal_id, lease, ask, targets=targets)

    assert (result.outcome, result.state, result.settled) == (outcome, UNKNOWN, False)
    assert result.detail == detail
    after = _state(factory, proposal_id)
    assert (after.state, after.version, after.reason) == (UNKNOWN, before.version, before.reason)
    (row,) = _audits(factory)
    assert (row.details["outcome"], row.details["state"]) == (outcome, UNKNOWN)
    with factory() as session:
        assert session.execute(select(CaliberEffectReceipt.receipt_id)).first() is None


def test_an_abandoned_dispatch_is_taken_over_and_asked_about(database: Any) -> None:
    _, factory = database
    with factory() as session:
        proposal_id = _proposal(session)
        old = leased(session, OWNER)
        assert release(session, old)
        new = acquire(session, "reconciler-1", ttl_seconds=TTL_SECONDS)
        assert new is not None
        session.commit()
    result = _ask(factory, proposal_id, new, _Scripted())
    assert (result.outcome, result.state) == (STILL_UNKNOWN, UNKNOWN)
    assert _state(factory, proposal_id).reason == "abandoned_dispatch"
    (row,) = _audits(factory)
    assert (
        row.details["state_before"] == DISPATCHED
        and row.details["fencing_token"] == new.fencing_token
    )


def test_a_dispatch_of_the_current_lease_is_left_to_the_dispatcher_until_the_bound(
    database: Any,
) -> None:
    _, factory = database
    with factory() as session:
        proposal_id = _proposal(session)
        lease = leased(session, OWNER)
        session.commit()
    ask = _Scripted()
    result = _ask(
        factory, proposal_id, lease, ask
    )  # no bound: only a lost lease's dispatch is touched
    assert (result.outcome, result.state, result.settled) == (IN_FLIGHT, DISPATCHED, False)
    assert ask.asked == []
    assert len(_audits(factory)) == 1, "the request is audited even though nothing moved"
    # with the loop's bound, a dispatch in flight since 2026-03 is expired to unknown and then asked about
    result = _ask(factory, proposal_id, lease, ask, in_flight_seconds=900.0)
    assert (result.outcome, result.state) == (STILL_UNKNOWN, UNKNOWN)
    assert _state(factory, proposal_id).reason == "in_flight_expired"


def _bare_proposal(factory: sessionmaker[Session]) -> str:
    with factory() as session:
        recorded = record_proposal(
            session,
            workflow_run_id="RUN-ep",
            node_id="n",
            occurrence_id="occ-fresh",
            intent=Intent(
                connector="document-request",
                connector_version="1",
                effect_class="read",
                resource="cases/C/document-requests",
                args={"document_type": "d"},
                expected_resource_version="1",
            ),
        )
        session.commit()
        return str(recorded.proposal_id)


def _settled_proposal(factory: sessionmaker[Session], state: str) -> str:
    with factory() as session:
        proposal_id = _proposal(session)
        if state == COMMITTED:
            _move(
                session,
                proposal_id,
                COMMITTED,
                owner_id=OWNER,
                attempt=1,
                receipt_id=_receipt(session, proposal_id),
            )
        else:
            _move(session, proposal_id, FAILED, owner_id=OWNER, attempt=1, reason="rejected:400")
        session.commit()
        return proposal_id


@pytest.mark.parametrize("state", [PROPOSED, COMMITTED, FAILED])
def test_a_proposal_that_is_not_open_has_nothing_to_reconcile(database: Any, state: str) -> None:
    _, factory = database
    proposal_id = (
        _bare_proposal(factory) if state == PROPOSED else _settled_proposal(factory, state)
    )
    _, lease = _unknown_and_lease(factory)
    ask = _Scripted()
    with pytest.raises(NotReconcilable) as refused:
        _ask(factory, proposal_id, lease, ask)
    assert refused.value.state == state and ask.asked == []
    assert _audits(factory) == [], "nothing was requested, so nothing is recorded as requested"


def test_an_unknown_proposal_id_is_a_key_error(database: Any) -> None:
    _, factory = database
    _, lease = _unknown_and_lease(factory)
    with pytest.raises(KeyError):
        _ask(factory, "prop-missing", lease, _Scripted())


@pytest.mark.parametrize("reason", ["", "   ", "x" * (MAX_REASON_CHARS + 1)])
def test_a_request_needs_a_reason_of_bounded_length(database: Any, reason: str) -> None:
    _, factory = database
    proposal_id, lease = _unknown_and_lease(factory)
    ask = _Scripted()
    with pytest.raises(ValueError, match="reason"):
        _ask(factory, proposal_id, lease, ask, reason=reason)
    assert ask.asked == [] and _audits(factory) == []
    # the longest allowed reason is kept as given (trimmed)
    _ask(factory, proposal_id, lease, ask, reason=" " + "y" * MAX_REASON_CHARS + " ")
    assert _audits(factory)[0].details["reason"] == "y" * MAX_REASON_CHARS


def test_a_lease_taken_over_stops_the_request_with_nothing_asked_or_moved_and_audits_it(
    database: Any,
) -> None:
    _, factory = database
    proposal_id, stale = _unknown_and_lease(factory)
    with factory() as session:
        assert release(session, stale)
        assert acquire(session, "someone-else", ttl_seconds=TTL_SECONDS) is not None
        session.commit()
    ask = _Scripted()
    with pytest.raises(ProposalError) as lost:
        _ask(factory, proposal_id, stale, ask)
    assert lost.value.code == LEASE_LOST
    assert ask.asked == []
    assert _state(factory, proposal_id).state == UNKNOWN
    (row,) = _audits(factory)
    assert row.details["outcome"] == LEASE_LOST and row.details["state"] is None


# ---------------------------------------------------------------- the operator view


def test_reconcile_is_offered_for_exactly_the_rows_where_it_does_something(database: Any) -> None:
    _, factory = database
    with factory() as session:
        abandoned = _proposal(session, occurrence="o-abandoned")
        unknown = _unknown_one(session, "o-unknown")
        release(
            session, leased(session, OWNER)
        )  # the lease lapses: the next dispatch is a new acquisition
        current = _proposal(session, occurrence="o-current")
        session.commit()
    with factory() as session:
        rows = {
            r["proposal_id"]: r
            for r in list_proposals(session, states=proposal_states("any"), limit=50)
        }
    assert (rows[abandoned]["abandoned"], rows[current]["abandoned"]) == (True, False)
    assert {pid: row["safe_actions"] for pid, row in rows.items()} == {
        abandoned: [ACTION],
        unknown: [ACTION],
        current: [],
    }, "a dispatch of the lease that is current now is the dispatcher's: nothing to ask yet"
    assert describe_sources()["effect_proposal"]["safe_actions"] == [ACTION] == ["reconcile"]


# ---------------------------------------------------------------- the route


class _Role:
    def __init__(self, lease: Any, role: str = "owner") -> None:
        self.lease, self.role = lease, role


@pytest.fixture
def owner_client(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    client.app.state.config = client.app.state.config.model_copy(  # type: ignore[attr-defined]
        update={"effect_callback_connectors": BINDINGS}
    )
    _seed_run(client.app.state.session_factory)  # type: ignore[attr-defined]
    return client


def _route(proposal_id: str) -> str:
    return f"{PREFIX}/system/effects/proposals/{proposal_id}/reconcile"


def _hold(client: TestClient, db_session: Session, role: str = "owner") -> Any:
    lease = leased(db_session, OWNER)
    db_session.commit()
    client.app.state.execution_role = _Role(lease, role)  # type: ignore[attr-defined]
    return lease


def test_the_route_asks_as_the_owner_settles_on_evidence_and_audits_the_caller(
    owner_client: TestClient,
    db_session: Session,
    monkeypatch: pytest.MonkeyPatch,
    _fact_lookup: None,
) -> None:
    proposal_id = _unknown_one(db_session, "occ-route")
    db_session.commit()
    _hold(owner_client, db_session)
    ask = _Scripted()
    key = proposal_view(db_session, proposal_id).logical_key
    ask.answers[key] = Answer(200, {"found": True})
    monkeypatch.setattr(routes_module, "ask_downstream", ask)

    response = owner_client.post(_route(proposal_id), json={"reason": REASON})

    assert response.status_code == 200, response.text
    assert response.json()["data"] == {
        "proposal_id": proposal_id,
        "outcome": RECONCILED_COMMITTED,
        "state": COMMITTED,
        "detail": response.json()["data"]["detail"],
        "settled": True,
    }
    assert ask.asked == [key]
    db_session.expire_all()
    (row,) = db_session.execute(
        select(CaliberAuditLog).where(CaliberAuditLog.action == AUDIT_ACTION)
    ).scalars()
    assert row.actor == DEFAULT_TEST_USER, "the authenticated caller is the actor"
    assert row.details["reason"] == REASON


def test_the_route_leaves_an_unproven_proposal_unknown_and_says_so(
    owner_client: TestClient, db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    proposal_id = _unknown_one(db_session, "occ-route-404")
    db_session.commit()
    _hold(owner_client, db_session)
    monkeypatch.setattr(routes_module, "ask_downstream", _Scripted())  # every lookup is a 404
    response = owner_client.post(_route(proposal_id), json={"reason": REASON})
    assert response.status_code == 200
    data = response.json()["data"]
    assert (data["outcome"], data["state"], data["settled"]) == (STILL_UNKNOWN, UNKNOWN, False)
    db_session.expire_all()
    assert proposal_state(db_session, proposal_id).state == UNKNOWN


@pytest.mark.parametrize("mode", ["no_controller", "secondary", "lease_dropped"])
def test_a_process_that_is_not_the_execution_owner_asks_and_records_nothing(
    owner_client: TestClient, db_session: Session, monkeypatch: pytest.MonkeyPatch, mode: str
) -> None:
    proposal_id = _unknown_one(db_session, "occ-secondary")
    db_session.commit()
    ask = _Scripted()
    monkeypatch.setattr(routes_module, "ask_downstream", ask)
    state = owner_client.app.state  # type: ignore[attr-defined]
    if hasattr(state, "execution_role"):
        del state.execution_role
    if mode == "secondary":
        _hold(owner_client, db_session, role="secondary")
    elif mode == "lease_dropped":
        state.execution_role = _Role(None, "owner")
    response = owner_client.post(_route(proposal_id), json={"reason": REASON})
    assert response.status_code == 409
    assert "not_execution_owner" in response.json()["detail"]
    assert ask.asked == []
    db_session.expire_all()
    assert proposal_state(db_session, proposal_id).state == UNKNOWN
    assert (
        db_session.execute(
            select(CaliberAuditLog).where(CaliberAuditLog.action == AUDIT_ACTION)
        ).first()
        is None
    )


def test_a_lease_lost_between_the_controller_and_the_database_is_a_409_with_nothing_recorded(
    owner_client: TestClient, db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    proposal_id = _unknown_one(db_session, "occ-lost")
    stale = _hold(owner_client, db_session)
    assert release(db_session, stale)
    assert acquire(db_session, "someone-else", ttl_seconds=TTL_SECONDS) is not None
    db_session.commit()
    ask = _Scripted()
    monkeypatch.setattr(routes_module, "ask_downstream", ask)
    response = owner_client.post(_route(proposal_id), json={"reason": REASON})
    assert response.status_code == 409 and response.json()["detail"] == "lease_lost"
    assert ask.asked == []
    db_session.expire_all()
    assert proposal_state(db_session, proposal_id).state == UNKNOWN


@pytest.mark.parametrize(
    "body",
    [
        {},
        {"reason": ""},
        {"reason": 7},
        {"reason": "x" * (MAX_REASON_CHARS + 1)},
        {"reason": REASON, "resolution": "failed"},
        {"reason": REASON, "state": "committed"},
        {"reason": REASON, "receipt_id": "rcpt-1"},
    ],
)
def test_the_body_is_a_reason_and_nothing_else_so_an_operator_cannot_say_what_happened(
    owner_client: TestClient,
    db_session: Session,
    monkeypatch: pytest.MonkeyPatch,
    body: dict[str, Any],
) -> None:
    proposal_id = _unknown_one(db_session, "occ-body")
    db_session.commit()
    _hold(owner_client, db_session)
    ask = _Scripted()
    monkeypatch.setattr(routes_module, "ask_downstream", ask)
    response = owner_client.post(_route(proposal_id), json=body)
    assert response.status_code == 400, response.text
    assert ask.asked == []
    db_session.expire_all()
    assert proposal_state(db_session, proposal_id).state == UNKNOWN


def test_an_unknown_proposal_is_404_and_a_settled_one_is_409(
    owner_client: TestClient, db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    proposal_id = _proposal(db_session, occurrence="occ-done")
    _move(
        db_session,
        proposal_id,
        COMMITTED,
        owner_id=OWNER,
        attempt=1,
        receipt_id=_receipt(db_session, proposal_id),
    )
    db_session.commit()
    _hold(owner_client, db_session)
    monkeypatch.setattr(routes_module, "ask_downstream", _Scripted())
    assert owner_client.post(_route("prop-nope"), json={"reason": REASON}).status_code == 404
    settled = owner_client.post(_route(proposal_id), json={"reason": REASON})
    assert settled.status_code == 409 and "not_reconcilable: committed" in settled.json()["detail"]


def test_the_route_needs_the_admin_scope(
    owner_client: TestClient, db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    proposal_id = _unknown_one(db_session, "occ-scope")
    db_session.commit()
    _hold(owner_client, db_session)
    ask = _Scripted()
    monkeypatch.setattr(routes_module, "ask_downstream", ask)
    response = owner_client.post(
        _route(proposal_id), json={"reason": REASON}, headers={"X-CALIBER-User": "@viewer"}
    )
    assert response.status_code == 403 and ask.asked == []


def test_any_other_refusal_from_the_store_is_surfaced_as_its_code(
    owner_client: TestClient, db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    proposal_id = _unknown_one(db_session, "occ-code")
    db_session.commit()
    _hold(owner_client, db_session)

    def refuse(*_a: Any, **_k: Any) -> Any:
        raise ProposalError("store_failed", "scripted")

    monkeypatch.setattr(routes_module, "reconcile_as_operator", refuse)
    response = owner_client.post(_route(proposal_id), json={"reason": REASON})
    assert response.status_code == 409 and response.json()["detail"] == "store_failed"


# ---------------------------------------------------------------- the operator racing the loop (PostgreSQL)


def test_the_operator_and_the_loop_asking_at_once_settle_it_exactly_once_on_postgresql(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    if not POSTGRES_TEST_URL:
        pytest.skip("the race needs PostgreSQL")
    with _database("postgresql", tmp_path, monkeypatch) as (_, factory):
        _seed_run(factory)
        monkeypatch.setitem(
            CONNECTORS,
            "document-request",
            replace(
                CONNECTORS["document-request"],
                facts=lambda view, *, lookup_status, lookup_body: _facts_for(view),
            ),
        )
        proposal_id, lease = _unknown_and_lease(factory)
        gate = threading.Barrier(2)
        errors: list[BaseException] = []
        results: dict[str, Any] = {}

        def lookup(connector: Any, base_url: str, key: str) -> Answer:
            gate.wait(20)  # both have read it unknown before either writes
            return Answer(200, {"found": True})

        task = EffectReconcilerTask(
            factory,
            SimpleNamespace(effect_callback_connectors=BINDINGS),
            lease_provider=lambda: lease,
            lookup=lookup,
        )

        def operator() -> None:
            try:
                results["operator"] = _ask(factory, proposal_id, lease, lookup)
            except BaseException as exc:  # pragma: no cover - reported below
                errors.append(exc)

        def loop() -> None:
            try:
                results["loop"] = task.tick()
            except BaseException as exc:  # pragma: no cover - reported below
                errors.append(exc)

        threads = [
            threading.Thread(target=operator, daemon=True),
            threading.Thread(target=loop, daemon=True),
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(60)
        assert not errors, errors
        assert _state(factory, proposal_id).state == COMMITTED
        with factory() as session:
            assert len(session.execute(select(CaliberEffectReceipt.receipt_id)).all()) == 1
        outcomes = [results["operator"].outcome] + [r.outcome for r in results["loop"].results]
        assert sorted(outcomes) == sorted([RECONCILED_COMMITTED, MOVED]), outcomes
        # whichever lost still left its audit row
        assert len(_audits(factory)) == 1
