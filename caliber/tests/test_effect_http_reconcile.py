# ruff: noqa: F811 - the fixtures are the ones imported from the reconciler's own tests
"""#1055: an ``http`` effect that ended unknown is settled by the lookup by key an operator listed, and by nothing else.

The reconciler used to bind only the document-request connector, so every governed webhook or API-request effect that ended
``unknown`` (a timeout after the send, a 2xx nobody could confirm, a failed settle, an abandoned dispatch, ``in_flight_expired``)
reported ``not_bound`` on every pass and parked its run for good. These tests drive the same reconciler (SQLite always, PostgreSQL
when ``CALIBER_TEST_POSTGRES_URL`` is set) with a scripted downstream, and check three things: an effect whose resource an operator
listed is settled by a stored request that is *this effect* and by nothing weaker; every other answer leaves it ``unknown``
(a 404 is never "it did not happen"); and no path sends the request again.
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from caliber.db.models import (
    CaliberAuditLog,
    CaliberEffectProposal,
    CaliberEffectReceipt,
    CaliberWorkflowRun,
)
from caliber.effects.connectors import http_lookup
from caliber.effects.operator_action import reconcile_as_operator
from caliber.effects.reconcile import (
    CONNECTORS,
    IN_FLIGHT,
    LOOKUP_FAILED,
    MOVED,
    NOT_BOUND,
    RECONCILED_COMMITTED,
    STILL_UNKNOWN,
    UNVERIFIED,
    Answer,
    reconcile_one,
    reconcile_pass,
    targets_from_config,
)
from caliber.effects.records import (
    APPROVED,
    COMMITTED,
    DISPATCHED,
    FAILED,
    RECONCILED,
    UNKNOWN,
    Approval,
    Intent,
    ProposalError,
    proposal_state,
    proposal_view,
    record_proposal,
)
from caliber.integrations.openapi.executor import OpenApiExecutionError
from caliber.orchestrator.effect_reconciler import EffectReconcilerTask
from caliber.ownership import Lease
from tests.dialect_helpers import DIALECTS  # noqa: F401 - the module's parametrization
from tests.effect_helpers import leased
from tests.test_effect_reconcile import (  # noqa: F401 - the fixture and helpers of the reconciler's tests
    PROJECT,
    RECONCILER,
    _owner,
    _park_run,
    _state,
    _taken_over,
    database,
)
from tests.test_effect_records import AT, ATTEMPT, OWNER, POLICY_DECISION, _decision, _move

URL = "https://hooks.example.test/notify"
LOOKUP = "https://hooks.example.test/lookups/by-key"
BODY = {"case": "MORT-1", "amount": 12}
ARGS = {"url": URL, "method": "POST", "headers": {}, "body": BODY}


def _config(*rows: dict[str, str]) -> Any:
    return SimpleNamespace(
        effect_callback_connectors="[]", effect_http_verifiers=json.dumps(list(rows))
    )


def _listed(project: str = PROJECT, resource: str = URL, lookup: str = LOOKUP) -> dict[str, str]:
    return {"project_id": project, "resource": resource, "lookup_url": lookup}


TARGETS = targets_from_config(_config(_listed()))


def _http_unknown(
    session: Session,
    occurrence: str,
    *,
    resource: str = URL,
    args: dict[str, Any] | None = None,
    to: str = UNKNOWN,
    reason: str = "ambiguous:may_have_been_sent",
) -> str:
    """A governed webhook effect, approved and dispatched (attempt 1), then moved to ``to`` (``unknown`` by default)."""
    recorded = record_proposal(
        session,
        workflow_run_id="RUN-ep",
        node_id="hook",
        occurrence_id=occurrence,
        intent=Intent(
            connector="http",
            connector_version="1",
            effect_class="read",
            resource=resource,
            args=ARGS if args is None else args,
            expected_resource_version=None,
        ),
    )
    proposal_id = recorded.proposal_id
    decision = _decision(session, proposal_id, decision_id=f"pd-{proposal_id}")
    _move(session, proposal_id, APPROVED, approval=Approval(POLICY_DECISION, decision))
    _move(session, proposal_id, DISPATCHED, owner_id=OWNER, attempt=ATTEMPT)
    if to != DISPATCHED:
        _move(session, proposal_id, UNKNOWN, owner_id=OWNER, attempt=ATTEMPT, reason=reason)
    return proposal_id


def _stored(view_key: str, **changes: Any) -> dict[str, Any]:
    """What the downstream's lookup answers when it holds this proposal's request."""
    body: dict[str, Any] = {
        "idempotency_key": view_key,
        "request_id": "REQ-1",
        "method": "POST",
        "url": URL,
        "body": BODY,
    }
    body.update(changes)
    return body


class Downstream:
    """A scripted downstream: what the lookup answers, where it was asked and how often."""

    def __init__(self) -> None:
        self.asked: list[tuple[str, str]] = []
        self.answer: Any = Answer(404, {"detail": "not found"})

    def __call__(self, connector: Any, base_url: str, key: str) -> Answer:
        assert connector is CONNECTORS["http"]
        self.asked.append((base_url, key))
        answer = self.answer(key) if callable(self.answer) else self.answer
        if isinstance(answer, Exception):
            raise answer
        return answer  # type: ignore[no-any-return]


def _reconcile(
    factory: sessionmaker[Session],
    proposal_id: str,
    downstream: Downstream,
    *,
    targets: Any = None,
    lease: Lease | None = None,
) -> Any:
    return reconcile_one(
        factory,
        proposal_id,
        lease=lease or _owner(factory),
        targets=TARGETS if targets is None else targets,
        lookup=downstream,
        now=AT,
    )


def _setup(
    factory: sessionmaker[Session], occurrence: str = "occ-1", **kwargs: Any
) -> tuple[str, str]:
    with factory() as session:
        proposal_id = _http_unknown(session, occurrence, **kwargs)
        key = proposal_view(session, proposal_id).logical_key
        session.commit()
    return proposal_id, key


# ----------------------------------------------------------------------------------------- the targets


def test_the_targets_are_the_callback_bindings_and_the_listed_endpoints_and_never_anything_else() -> (
    None
):
    binding = {
        "connector": "document-request",
        "project_id": PROJECT,
        "secret_source": "env://S",
        "base_url": "https://dr.example.test",
    }
    config = SimpleNamespace(
        effect_callback_connectors=json.dumps([binding]),
        effect_http_verifiers=json.dumps([_listed()]),
    )
    assert targets_from_config(config) == {
        ("document-request", PROJECT): "https://dr.example.test",
        (f"http:{URL}", PROJECT): LOOKUP,
    }
    assert targets_from_config(SimpleNamespace()) == {}, (
        "a configuration that predates the settings"
    )
    assert targets_from_config(_config()) == {}
    with pytest.raises(ValueError):
        targets_from_config(SimpleNamespace(effect_http_verifiers="not json"))
    with pytest.raises(ValueError):
        targets_from_config(SimpleNamespace(effect_callback_connectors="not json"))


def test_the_http_connector_is_reconcilable_and_its_target_is_its_own_resource() -> None:
    connector = CONNECTORS["http"]
    view = SimpleNamespace(connector="http", resource=URL)
    assert connector.target_key(view) == f"http:{URL}"  # type: ignore[arg-type]
    other = SimpleNamespace(connector="document-request", resource="cases/x/document-requests")
    assert CONNECTORS["document-request"].target_key(other) == "document-request"  # type: ignore[arg-type]
    assert connector.capability.lookup_by_key and not connector.capability.absence_proves_noncommit


# ------------------------------------------------------------------------------------- the settlement


def test_an_unknown_http_effect_is_committed_by_the_stored_request_and_nothing_is_sent(
    database: Any,
) -> None:
    _, factory = database
    proposal_id, key = _setup(factory)
    downstream = Downstream()
    downstream.answer = Answer(200, _stored(key))

    result = _reconcile(factory, proposal_id, downstream)

    assert (result.outcome, result.state) == (RECONCILED_COMMITTED, COMMITTED)
    assert downstream.asked == [(LOOKUP, key)], "asked once, at the listed URL, by the logical key"
    with factory() as session:
        state = proposal_state(session, proposal_id)
        assert state.state == COMMITTED and state.receipt_id == result.detail
        receipt = session.execute(select(CaliberEffectReceipt)).scalar_one()
        assert receipt.proposal_id == proposal_id and receipt.verified_by == "query"
        assert (receipt.connector, receipt.logical_key, receipt.downstream_request_id) == (
            "http",
            key,
            http_lookup.receipt_request_id(URL, "REQ-1"),
        )
        assert receipt.intent_hash == proposal_view(session, proposal_id).intent_hash
        audit = session.execute(
            select(CaliberAuditLog).where(CaliberAuditLog.action == "effect_reconciled")
        ).scalar_one()
        assert audit.entity_id == proposal_id
        assert audit.actor == f"reconciler:{RECONCILER}"
    # settled once: another pass finds nothing and the lookup is never repeated
    assert reconcile_pass(factory, lease=_owner(factory), targets=TARGETS, lookup=downstream) == []
    assert len(downstream.asked) == 1


def test_the_pass_settles_it_with_targets_built_from_the_configuration(database: Any) -> None:
    _, factory = database
    proposal_id, key = _setup(factory)
    downstream = Downstream()
    downstream.answer = lambda asked: Answer(200, _stored(asked))

    results = reconcile_pass(
        factory,
        lease=_owner(factory),
        targets=targets_from_config(_config(_listed())),
        lookup=downstream,
        now=AT,
    )

    assert [(r.proposal_id, r.outcome) for r in results] == [(proposal_id, RECONCILED_COMMITTED)]
    assert _state(factory, proposal_id).state == COMMITTED


def test_a_dispatch_whose_lease_was_taken_over_is_abandoned_then_settled_by_the_lookup(
    database: Any,
) -> None:
    """The dispatcher died after the send: the new owner's pass abandons the dispatch to unknown and reads the downstream."""
    _, factory = database
    with factory() as session:
        proposal_id = _http_unknown(session, "occ-dead", to=DISPATCHED)
        key = proposal_view(session, proposal_id).logical_key
        old = leased(session, OWNER)
        new = _taken_over(session, old, RECONCILER)
        session.commit()
    downstream = Downstream()
    downstream.answer = Answer(200, _stored(key))

    result = _reconcile(factory, proposal_id, downstream, lease=new)

    assert (result.outcome, result.state) == (RECONCILED_COMMITTED, COMMITTED)
    assert downstream.asked == [(LOOKUP, key)]


def test_a_reconciled_proposal_is_settled_by_the_next_lookup(database: Any) -> None:
    _, factory = database
    proposal_id, key = _setup(factory)
    with factory() as session:
        _move(session, proposal_id, RECONCILED, reason="reconciler")
        session.commit()
    downstream = Downstream()
    downstream.answer = Answer(200, _stored(key))
    result = _reconcile(factory, proposal_id, downstream)
    assert (result.outcome, result.state) == (RECONCILED_COMMITTED, COMMITTED)


def test_the_run_parked_behind_it_wakes_when_the_lookup_commits_it(database: Any) -> None:
    _, factory = database
    proposal_id, key = _setup(factory)
    _park_run(factory, proposal_id)
    downstream = Downstream()
    downstream.answer = Answer(200, _stored(key))

    _reconcile(factory, proposal_id, downstream)

    with factory() as session:
        run = session.get(CaliberWorkflowRun, "RUN-ep")
        assert run is not None and run.status == "queued"
        assert run.summary["parked_effects"]["wake_reason"] == "all_effect_dependencies_settled"


# ------------------------------------------------------------------- what leaves it unknown, always


@pytest.mark.parametrize(
    "answer",
    [
        Answer(404, {"detail": "not found"}),
        Answer(500, {"detail": "boom"}),
        Answer(503, None),
        Answer(401, {"detail": "no"}),
        Answer(408, None),
    ],
)
def test_an_answer_other_than_a_stored_request_leaves_it_unknown_never_failed(
    database: Any, answer: Answer
) -> None:
    """A 404 is delayed visibility as far as anyone can tell for this connector: it never certifies that nothing happened."""
    _, factory = database
    proposal_id, _ = _setup(factory)
    downstream = Downstream()
    downstream.answer = answer
    lease = _owner(factory)

    for _ in range(3):  # pass after pass, nothing changes and nothing is sent
        result = _reconcile(factory, proposal_id, downstream, lease=lease)
        assert (result.outcome, result.state) == (STILL_UNKNOWN, UNKNOWN)
        assert result.detail == f"http_{answer.status_code}"

    with factory() as session:
        assert proposal_state(session, proposal_id).state == UNKNOWN
        assert session.execute(select(CaliberEffectReceipt)).first() is None


@pytest.mark.parametrize(
    "changes",
    [
        {"idempotency_key": "someone-elses-key"},
        {"url": "https://hooks.example.test/elsewhere"},
        {"method": "PUT"},
        {"body": {"case": "MORT-2", "amount": 12}},
        {"body": None},
        {"request_id": ""},
    ],
)
def test_a_stored_request_that_is_not_this_effect_is_not_a_receipt(
    database: Any, changes: dict[str, Any]
) -> None:
    _, factory = database
    proposal_id, key = _setup(factory)
    downstream = Downstream()
    downstream.answer = Answer(200, _stored(key, **changes))

    result = _reconcile(factory, proposal_id, downstream)

    assert (result.outcome, result.state) == (UNVERIFIED, UNKNOWN)
    with factory() as session:
        assert session.execute(select(CaliberEffectReceipt)).first() is None


def test_a_downstream_that_cannot_be_reached_is_reported_and_changes_nothing(database: Any) -> None:
    _, factory = database
    proposal_id, _ = _setup(factory)
    downstream = Downstream()
    downstream.answer = OpenApiExecutionError("connection refused", never_sent=False, attempts=1)

    result = _reconcile(factory, proposal_id, downstream)

    assert (result.outcome, result.state, result.detail) == (
        LOOKUP_FAILED,
        UNKNOWN,
        "OpenApiExecutionError",
    )
    assert _state(factory, proposal_id).state == UNKNOWN


@pytest.mark.parametrize(
    "targets",
    [
        {},
        {(f"http:{URL}", "another-project"): LOOKUP},
        {(f"http:{URL}/other", PROJECT): LOOKUP},
        {
            ("http", PROJECT): LOOKUP
        },  # the connector's own name is not an endpoint: only a listed resource is
        {(f"{URL}", PROJECT): LOOKUP},
    ],
    ids=["nothing listed", "other project", "other resource", "connector name", "bare resource"],
)
def test_an_effect_whose_resource_nobody_listed_stays_not_bound_and_nothing_is_asked(
    database: Any, targets: dict[tuple[str, str], str]
) -> None:
    _, factory = database
    proposal_id, _ = _setup(factory)
    downstream = Downstream()
    downstream.answer = Answer(200, {"would": "settle"})

    result = _reconcile(factory, proposal_id, downstream, targets=targets)

    assert (result.outcome, result.state, result.detail) == (NOT_BOUND, UNKNOWN, "http")
    assert downstream.asked == []


def test_an_unlisted_resource_is_never_asked_even_when_another_is_listed(database: Any) -> None:
    _, factory = database
    other = "https://hooks.example.test/other"
    proposal_id, _ = _setup(factory, "occ-other", resource=other, args={**ARGS, "url": other})
    downstream = Downstream()
    result = _reconcile(factory, proposal_id, downstream)
    assert result.outcome == NOT_BOUND and downstream.asked == []


def test_a_proposal_that_moved_since_it_was_read_is_reported_moved_and_not_overwritten(
    database: Any,
) -> None:
    _, factory = database
    proposal_id, key = _setup(factory)
    downstream = Downstream()

    def settle_elsewhere(asked: str) -> Answer:
        # the downstream answers while a callback (or another reconciler) wins the race to commit
        with factory() as session:
            from tests.test_effect_records import _receipt

            _move(session, proposal_id, RECONCILED, reason="callback")
            _move(session, proposal_id, COMMITTED, receipt_id=_receipt(session, proposal_id))
            session.commit()
        return Answer(200, _stored(asked))

    downstream.answer = settle_elsewhere

    result = _reconcile(factory, proposal_id, downstream)

    assert (result.outcome, result.state) == (MOVED, COMMITTED)
    with factory() as session:
        assert len(list(session.execute(select(CaliberEffectReceipt)).scalars())) == 1


def test_an_in_flight_dispatch_of_the_current_lease_is_left_alone(database: Any) -> None:
    _, factory = database
    with factory() as session:
        proposal_id = _http_unknown(session, "occ-live", to=DISPATCHED)
        session.commit()
    downstream = Downstream()
    result = _reconcile(factory, proposal_id, downstream, lease=leased_by_owner(factory))
    assert (result.outcome, result.state) == (IN_FLIGHT, DISPATCHED)
    assert downstream.asked == []


def leased_by_owner(factory: sessionmaker[Session]) -> Lease:
    with factory() as session:
        lease = leased(session, OWNER)
        session.commit()
    return lease


# ------------------------------------------------------------------------------------- the operator


def test_the_operators_ask_now_settles_an_http_effect_the_same_way_and_is_audited(
    database: Any,
) -> None:
    """The operator action only asks; with a listed endpoint it can now settle, on the downstream's evidence."""
    _, factory = database
    proposal_id, key = _setup(factory)
    downstream = Downstream()
    downstream.answer = Answer(200, _stored(key))

    result = reconcile_as_operator(
        factory,
        proposal_id,
        actor="ops-admin",
        reason="the downstream's owner says it is visible",
        lease=_owner(factory),
        targets=TARGETS,
        lookup=downstream,
    )

    assert (result.outcome, result.state, result.settled) == (
        RECONCILED_COMMITTED,
        COMMITTED,
        True,
    )
    with factory() as session:
        audit = session.execute(
            select(CaliberAuditLog).where(CaliberAuditLog.action == "effect_reconcile_requested")
        ).scalar_one()
        assert audit.actor == "ops-admin" and audit.details["state"] == COMMITTED


def test_the_operators_ask_now_for_an_unlisted_http_effect_can_only_ask(database: Any) -> None:
    _, factory = database
    proposal_id, _ = _setup(factory)
    result = reconcile_as_operator(
        factory,
        proposal_id,
        actor="ops-admin",
        reason="try",
        lease=_owner(factory),
        targets={},
        lookup=Downstream(),
    )
    assert (result.outcome, result.state, result.settled) == (NOT_BOUND, UNKNOWN, False)


def test_a_failed_proposal_is_never_revived_by_a_lookup(database: Any) -> None:
    _, factory = database
    proposal_id, key = _setup(factory)
    with factory() as session:
        _move(session, proposal_id, RECONCILED, reason="because")
        _move(session, proposal_id, FAILED, reason="because")
        session.commit()
    downstream = Downstream()
    downstream.answer = Answer(200, _stored(key))
    result = _reconcile(factory, proposal_id, downstream)
    assert (result.outcome, result.state) == (MOVED, FAILED)
    assert downstream.asked == []
    with factory() as session:
        assert session.execute(select(CaliberEffectReceipt)).first() is None
        row = session.get(CaliberEffectProposal, proposal_id)
        assert row is not None and row.state == FAILED
    with factory() as session, pytest.raises(ProposalError):
        _move(session, proposal_id, COMMITTED, receipt_id="rcpt-none")


# ----------------------------------------------------------------------------- the loop and the route


def test_the_owner_loop_settles_an_http_effect_through_the_listed_endpoint(database: Any) -> None:
    _, factory = database
    proposal_id, key = _setup(factory)
    lease = _owner(factory)
    downstream = Downstream()
    downstream.answer = Answer(200, _stored(key))
    task = EffectReconcilerTask(
        factory,
        SimpleNamespace(
            effect_callback_connectors="[]", effect_http_verifiers=json.dumps([_listed()])
        ),
        lease_provider=lambda: lease,
        lookup=downstream,
    )
    assert task._targets == {(f"http:{URL}", PROJECT): LOOKUP}

    report = task.tick()

    assert report is not None and report.ended == "complete"
    assert [(r.proposal_id, r.outcome) for r in report.results] == [
        (proposal_id, RECONCILED_COMMITTED)
    ]
    assert _state(factory, proposal_id).state == COMMITTED
    assert downstream.asked == [(LOOKUP, key)]


def test_the_loop_with_no_listing_leaves_an_http_effect_not_bound(database: Any) -> None:
    _, factory = database
    proposal_id, _ = _setup(factory)
    lease = _owner(factory)
    downstream = Downstream()
    task = EffectReconcilerTask(
        factory,
        SimpleNamespace(effect_callback_connectors="[]"),
        lease_provider=lambda: lease,
        lookup=downstream,
    )
    report = task.tick()
    assert report is not None
    assert [(r.proposal_id, r.outcome) for r in report.results] == [(proposal_id, NOT_BOUND)]
    assert downstream.asked == []


class _Role:
    def __init__(self, lease: Any) -> None:
        self.lease, self.role = lease, "owner"


def test_the_operator_route_reads_the_listing_from_the_configuration_and_settles_it(
    client: Any, db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    from tests.test_effect_proposals import _seed_run
    from tests.workflow_helpers import PREFIX

    client.app.state.config = client.app.state.config.model_copy(
        update={"effect_http_verifiers": json.dumps([_listed()])}
    )
    _seed_run(client.app.state.session_factory)
    proposal_id = _http_unknown(db_session, "occ-route")
    key = proposal_view(db_session, proposal_id).logical_key
    lease = leased(db_session, OWNER)
    db_session.commit()
    client.app.state.execution_role = _Role(lease)
    downstream = Downstream()
    downstream.answer = Answer(200, _stored(key))
    monkeypatch.setattr("caliber.routes.system_effects.ask_downstream", downstream)

    response = client.post(
        f"{PREFIX}/system/effects/proposals/{proposal_id}/reconcile",
        json={"reason": "the downstream's owner says it is visible"},
    )

    assert response.status_code == 200, response.text
    data = response.json()["data"]
    assert (data["outcome"], data["state"], data["settled"]) == (
        RECONCILED_COMMITTED,
        COMMITTED,
        True,
    )
    assert downstream.asked == [(LOOKUP, key)]
    db_session.expire_all()
    audit = db_session.execute(
        select(CaliberAuditLog).where(CaliberAuditLog.action == "effect_reconcile_requested")
    ).scalar_one()
    assert audit.details["state"] == COMMITTED


# ------------------------------------------------------------ request ids are per endpoint (review of #1092)


def _endpoint_proposal(
    factory: sessionmaker[Session], occurrence: str, resource: str
) -> tuple[str, str]:
    return _setup(factory, occurrence, resource=resource, args={**ARGS, "url": resource})


def test_two_endpoints_that_issue_the_same_request_id_both_settle(database: Any) -> None:
    _, factory = database
    other = "https://hooks.example.test/other"
    first, first_key = _endpoint_proposal(factory, "occ-a", URL)
    second, second_key = _endpoint_proposal(factory, "occ-b", other)
    targets = targets_from_config(
        _config(
            _listed(), _listed(resource=other, lookup="https://hooks.example.test/other-lookups")
        )
    )
    downstream = Downstream()

    def answer(key: str) -> Answer:
        resource = URL if key == first_key else other
        return Answer(200, _stored(key, request_id="1", url=resource))

    downstream.answer = answer
    lease = _owner(factory)

    for proposal_id in (first, second):
        result = _reconcile(factory, proposal_id, downstream, targets=targets, lease=lease)
        assert (result.outcome, result.state) == (RECONCILED_COMMITTED, COMMITTED)
    with factory() as session:
        ids = sorted(
            r.downstream_request_id for r in session.execute(select(CaliberEffectReceipt)).scalars()
        )
    assert ids == sorted(
        [http_lookup.receipt_request_id(URL, "1"), http_lookup.receipt_request_id(other, "1")]
    )


def test_an_endpoint_that_reuses_an_id_for_a_second_effect_cannot_commit_it_and_starves_no_one_else(
    database: Any,
) -> None:
    _, factory = database
    other = "https://hooks.example.test/other"
    first, first_key = _endpoint_proposal(factory, "occ-a", URL)
    squatted, squatted_key = _endpoint_proposal(factory, "occ-b", URL)
    bystander, bystander_key = _endpoint_proposal(factory, "occ-c", other)
    targets = targets_from_config(
        _config(
            _listed(), _listed(resource=other, lookup="https://hooks.example.test/other-lookups")
        )
    )
    downstream = Downstream()
    downstream.answer = lambda key: Answer(
        200, _stored(key, request_id="1", url=other if key == bystander_key else URL)
    )
    lease = _owner(factory)

    assert _reconcile(factory, first, downstream, targets=targets, lease=lease).state == COMMITTED
    with pytest.raises(ProposalError) as refused:
        _reconcile(factory, squatted, downstream, targets=targets, lease=lease)
    assert refused.value.code == "receipt_conflict"
    assert _state(factory, squatted).state == UNKNOWN, (
        "the id is the first effect's: the second stays unknown"
    )
    # a different endpoint issuing the same id is unaffected
    assert (
        _reconcile(factory, bystander, downstream, targets=targets, lease=lease).state == COMMITTED
    )
