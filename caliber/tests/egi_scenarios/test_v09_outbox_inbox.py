"""V09 public path: business events survive crashes and a saturated notification bus (B13.4, #685), and an inbound
event is applied once (B13.5a, #689; consumers wired in B13.5b, #693).

The reference-profile app on PostgreSQL (the same logic on SQLite in the development profile with the outbox switched
on). A run is admitted through the public API and every run event it produced has a durable outbox row written in the
same transaction. A publisher that dies after handing an event over is followed by one that redelivers it, and the
consumer's inbox holds it exactly once. When the in-process event bus's bounded subscriber queue is saturated and
drops notifications, no business event is lost: all of them are in the outbox and reach the consumer.

The one consumer of the inbound inbox is the signed connector receipt callback (B05.4b, #725); the two tests at the end
drive it through the public route. The GitHub webhook keeps its own delivery inbox
(``caliber_workspace_source_events``, migrations 0098/0101) and is covered by ``tests/test_github_webhooks_route.py``.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Iterator
from datetime import datetime, timedelta, timezone
from typing import Any

import pytest
from sqlalchemy import func, select
from starlette.testclient import TestClient

from caliber.db.models import (
    CaliberAuditLog,
    CaliberEffectReceipt,
    CaliberEventInbox,
    CaliberEventOutbox,
    CaliberInboxEvent,
    CaliberWorkflowRunEvent,
)
from caliber.effects.records import (
    COMMITTED,
    proposal_state,
)
from caliber.egress import EgressPolicy
from caliber.events.bus import EventBus
from caliber.events.inbox import DUPLICATE, PROCESSED, InboxPayloadMismatch, process_once
from caliber.events.outbox import (
    DELIVERED,
    PENDING,
    OutboxPublisher,
    SqlInboxConsumer,
    bind_outbox,
    payload_hash,
)
from caliber.integrations.openapi.executor import bind_egress_policy
from caliber.server import create_app
from caliber.workflows.run_events import append_run_event
from tests.egi_scenarios.conftest import (
    EDITOR_A,
    PROJECT_A,
    Downstream,
    ReferenceApp,
    Workspaces,
    seed_release,
)
from tests.egi_scenarios.test_b02_admission import _call, _published_workflow
from tests.test_effect_proposals import _seed_run
from tests.test_routes_system_effects import (
    CONNECTOR,
    PROJECT,
    SECRET,
    SECRET_ENV,
    _unknown_proposal,
)
from tests.test_routes_system_effects import _call as _signed_callback

SATURATING_EVENTS = 400  # far over the bus's per-subscriber queue bound


def _admit_a_run(app: ReferenceApp, key: str) -> str:
    workflow_id = _published_workflow(app)
    response = _call(
        app,
        "POST",
        EDITOR_A,
        PROJECT_A,
        "/workflow-runs",
        {
            "workflow_id": workflow_id,
            "alias": "manual",
            "input": "refund?",
            "idempotency_key": key,
            **seed_release(app, PROJECT_A),
        },
    )
    assert response.status_code == 202, response.text
    return str(response.json()["data"]["workflow_run_id"])


def _count(app: ReferenceApp, model: Any, *where: Any) -> int:
    with app.session_factory() as session:
        return int(
            session.execute(select(func.count()).select_from(model).where(*where)).scalar_one()
        )


def test_every_run_event_has_an_outbox_row_and_a_crashed_publisher_is_followed_by_one_that_redelivers(
    reference_app: ReferenceApp, two_workspaces: Workspaces
) -> None:
    bind_outbox(True)
    run_id = _admit_a_run(reference_app, "v09-1")
    with reference_app.session_factory() as session:
        sequences = list(
            session.execute(
                select(CaliberWorkflowRunEvent.sequence).where(
                    CaliberWorkflowRunEvent.workflow_run_id == run_id
                )
            ).scalars()
        )
        rows = {
            row.event_id: row
            for row in session.execute(
                select(CaliberEventOutbox).where(CaliberEventOutbox.aggregate_id == run_id)
            ).scalars()
        }
    assert sequences and set(rows) == {f"run-event:{run_id}:{n}" for n in sequences}
    assert all(row.status == PENDING and row.project_id == PROJECT_A for row in rows.values())

    inbox = SqlInboxConsumer("v09-consumer", reference_app.session_factory)
    now = datetime.now(timezone.utc) + timedelta(seconds=1)
    clock = [now]
    publisher = OutboxPublisher(
        reference_app.session_factory,
        consumers=[inbox],
        clock=lambda: clock[0],
        lease_seconds=5,
        batch_size=len(sequences),
    )

    def die_before_mark(event_id: str) -> None:
        raise SystemExit("the publisher dies after the handoff, before it records it")

    real_settle = publisher._settle_delivered
    publisher._settle_delivered = die_before_mark  # type: ignore[method-assign]
    with pytest.raises(SystemExit):
        publisher.publish_batch()
    assert _count(reference_app, CaliberEventInbox) == 1, "the first handoff was durable"
    assert _count(reference_app, CaliberEventOutbox, CaliberEventOutbox.status == DELIVERED) == 0

    publisher._settle_delivered = real_settle  # type: ignore[method-assign]
    clock[0] = now + timedelta(seconds=10)  # the dead publisher's lease has expired
    while publisher.publish_batch().claimed:
        pass
    assert _count(reference_app, CaliberEventInbox) == len(sequences), "once each, never twice"
    assert _count(reference_app, CaliberEventOutbox, CaliberEventOutbox.status == PENDING) == 0


def test_a_saturated_notification_bus_loses_no_business_event(
    reference_app: ReferenceApp, two_workspaces: Workspaces
) -> None:
    bind_outbox(True)
    run_id = _admit_a_run(reference_app, "v09-2")
    before = _count(reference_app, CaliberEventOutbox)

    async def saturate() -> int:
        bus = EventBus()
        dropped: list[dict[str, Any]] = []
        stream = bus.subscribe(on_drop=dropped.append)
        first = asyncio.ensure_future(
            stream.__anext__()
        )  # a subscriber that reads one event and stalls
        await asyncio.sleep(0)
        bus.publish({"type": "tick", "n": -1})
        await first
        for number in range(SATURATING_EVENTS):
            bus.publish({"type": "tick", "n": number})
        for _ in range(SATURATING_EVENTS * 2):
            await asyncio.sleep(0)
        await stream.aclose()
        return len(dropped)

    dropped = asyncio.run(saturate())
    assert dropped > 0, "the notification queue really overflowed"

    # The same burst as business events: none is dropped, because the outbox row is the delivery, not the queue.
    with reference_app.session_factory() as session:
        for _ in range(SATURATING_EVENTS):
            append_run_event(
                session,
                workflow_run_id=run_id,
                project_id=PROJECT_A,
                event_type="workflow.run.progress",
            )
        session.commit()
    assert _count(reference_app, CaliberEventOutbox) == before + SATURATING_EVENTS

    inbox = SqlInboxConsumer("v09-bus", reference_app.session_factory)
    publisher = OutboxPublisher(
        reference_app.session_factory,
        consumers=[inbox],
        clock=lambda: datetime.now(timezone.utc) + timedelta(seconds=5),
        batch_size=200,
    )
    while publisher.publish_batch().claimed:
        pass
    assert _count(reference_app, CaliberEventInbox) == before + SATURATING_EVENTS
    assert _count(reference_app, CaliberEventOutbox, CaliberEventOutbox.status == PENDING) == 0


def test_the_reference_profile_binds_the_outbox_at_startup(reference_app: ReferenceApp) -> None:
    """Nothing here switches it on: the app's own startup does, in the reference profile (and only there)."""
    from caliber.events.outbox import outbox_enabled

    assert outbox_enabled() is (reference_app.profile == "reference")


def test_inbox_dedupes_by_id_and_hash(
    reference_app: ReferenceApp, two_workspaces: Workspaces
) -> None:
    """An inbound callback for an admitted run, delivered twice, changes the run once; a different message that reuses
    its id is refused; a delivery whose transition fails leaves no trace, so the sender's retry is applied."""
    bind_outbox(True)
    run_id = _admit_a_run(reference_app, "v09-3")
    body = {"status": "approved", "document": "DOC-1"}
    digest = payload_hash(body)

    def callback(event_id: str, digest: str, fail: bool = False) -> Any:
        with reference_app.session_factory() as session:

            def transition() -> None:
                append_run_event(
                    session,
                    workflow_run_id=run_id,
                    project_id=PROJECT_A,
                    event_type="workflow.run.callback",
                    payload={"event_id": event_id},
                )
                if fail:
                    raise RuntimeError("the transition failed after it wrote")

            try:
                result = process_once(
                    session,
                    consumer="v09-connector",
                    source="document-request",
                    event_id=event_id,
                    payload_hash=digest,
                    apply=transition,
                )
            except BaseException:
                session.rollback()
                raise
            session.commit()
            return result

    def run_events() -> int:
        return _count(
            reference_app,
            CaliberWorkflowRunEvent,
            CaliberWorkflowRunEvent.workflow_run_id == run_id,
            CaliberWorkflowRunEvent.event_type == "workflow.run.callback",
        )

    assert callback("cb-1", digest).outcome == PROCESSED
    assert callback("cb-1", digest).outcome == DUPLICATE
    assert run_events() == 1, "the same callback twice is one transition"

    with pytest.raises(InboxPayloadMismatch):
        callback("cb-1", payload_hash({"status": "rejected"}))
    assert run_events() == 1

    with pytest.raises(RuntimeError, match="after it wrote"):
        callback("cb-2", digest, fail=True)
    assert run_events() == 1, "the failed transition's run event was rolled back"
    assert _count(reference_app, CaliberInboxEvent, CaliberInboxEvent.event_id == "cb-2") == 0
    assert callback("cb-2", digest).outcome == PROCESSED, (
        "the retry is processed, not treated as done"
    )
    assert run_events() == 2
    assert _count(reference_app, CaliberInboxEvent) == 2


# ----------------------------------------------------------------- the connector receipt callback (B13.5b, #693)


@pytest.fixture
def callback_client(
    reference_app: ReferenceApp, downstream: Downstream, monkeypatch: pytest.MonkeyPatch
) -> Iterator[TestClient]:
    """The reference app's own database behind an app that accepts the document-request connector's signed callback."""
    monkeypatch.setenv(SECRET_ENV, SECRET)
    bind_egress_policy(EgressPolicy(allowed_hosts=frozenset({"127.0.0.1"})))
    binding = {
        "connector": CONNECTOR,
        "project_id": PROJECT,
        "secret_source": f"env://{SECRET_ENV}",
        "base_url": downstream.base_url,
    }
    config = reference_app.config.model_copy(
        update={
            "effect_callback_connectors": json.dumps([binding]),
            "egress_allowed_hosts": "127.0.0.1",
        }
    )
    app = create_app(config=config)
    app.state.engine = reference_app.client.app.state.engine  # type: ignore[attr-defined]
    app.state.session_factory = reference_app.session_factory
    try:
        with TestClient(app) as client:  # no CALIBER identity: the signature is the only credential
            yield client
    finally:
        bind_egress_policy(None)


def _effect_facts(app: ReferenceApp, proposal_id: str) -> dict[str, Any]:
    """What is persisted about one proposal and its settlement: the state, its compare-and-set version, and how many
    receipts, consumed deliveries and audited callbacks exist."""
    with app.session_factory() as session:
        state = proposal_state(session, proposal_id)
    return {
        "state": state.state,
        "version": state.version,
        "receipts": _count(app, CaliberEffectReceipt),
        "inbox": _count(app, CaliberInboxEvent),
        "audited": _count(
            app, CaliberAuditLog, CaliberAuditLog.action == "effect_receipt_callback"
        ),
    }


def test_duplicate_callback_one_transition(
    reference_app: ReferenceApp, callback_client: TestClient, downstream: Downstream
) -> None:
    """The same signed callback delivered twice moves the proposal once (``unknown`` -> ``reconciled`` -> ``committed``,
    one receipt, one audit row, one consumed delivery): the redelivery is answered ``duplicate`` from the inbox without
    asking the downstream (it is stopped for those answers), a message that reuses the delivery id is a 409, and none of it sends anything downstream."""
    _seed_run(reference_app.session_factory)
    proposal_id, key = _unknown_proposal(reference_app.session_factory, downstream)
    assert _effect_facts(reference_app, proposal_id) == {
        "state": "unknown",
        "version": 4,
        "receipts": 0,
        "inbox": 0,
        "audited": 0,
    }

    first = _signed_callback(callback_client, key, delivery="delivery-A")
    assert (first.status_code, first.json()["data"]["status"]) == (200, "committed")
    after_first = _effect_facts(reference_app, proposal_id)
    assert after_first == {
        "state": COMMITTED,
        "version": 6,
        "receipts": 1,
        "inbox": 1,
        "audited": 1,
    }

    downstream.stop()  # from here the downstream cannot be asked: the answers below come from the inbox alone
    again = _signed_callback(callback_client, key, delivery="delivery-A")
    assert (again.status_code, again.json()) == (200, {"data": {"status": "duplicate"}})
    assert _effect_facts(reference_app, proposal_id) == after_first, (
        "the redelivery changed nothing"
    )

    clash = _signed_callback(callback_client, "another-key", delivery="delivery-A")
    assert (clash.status_code, clash.json()) == (409, {"reason": "inbox_payload_mismatch"})
    assert _effect_facts(reference_app, proposal_id) == after_first
    downstream.restart()  # same port and database: the state it holds is kept
    assert downstream.mutations() == 1, "the callback only ever asked: the effect happened once"


def test_out_of_order_ignored(
    reference_app: ReferenceApp,
    callback_client: TestClient,
    downstream: Downstream,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A callback that arrives after the proposal has moved past what it reports does not move it again or backwards.
    The callback carries no version of its own (it names a key and the platform asks the downstream), so the ordering
    is the proposal's compare-and-set ``version``: a late delivery is acknowledged with the settled outcome, the
    proposal stays where it is, and nothing new is recorded ("3 then 2 stays at 3", here version 6)."""
    from caliber.routes import system_effects

    _seed_run(reference_app.session_factory)
    proposal_id, key = _unknown_proposal(reference_app.session_factory, downstream)
    binding = system_effects.parse_bindings(
        callback_client.app.state.config.effect_callback_connectors  # type: ignore[attr-defined]
    )[0]
    with reference_app.session_factory() as session:
        stale_view = system_effects._resolve_callback_proposal(session, binding, key)
    assert stale_view is not None and stale_view.state == "unknown"
    assert _effect_facts(reference_app, proposal_id)["version"] == 4

    first = _signed_callback(callback_client, key, delivery="delivery-1")
    assert (first.status_code, first.json()["data"]["status"]) == (200, "committed")
    settled = _effect_facts(reference_app, proposal_id)
    assert (settled["state"], settled["version"]) == (COMMITTED, 6)

    # A later delivery of the same news (another delivery id): answered from the settled state, before any query.
    downstream.stop()  # the settled state answers; the downstream is not asked
    late = _signed_callback(callback_client, key, delivery="delivery-2")
    assert (late.status_code, late.json()["data"]["status"]) == (200, "already_committed")
    assert _effect_facts(reference_app, proposal_id) == settled, (
        "the late delivery consumed nothing"
    )

    # A delivery that read the proposal while it was still ``unknown`` (version 4) and reaches the commit after the
    # first one won: the older view loses, the proposal is not moved again, and no second receipt or audit row appears.
    downstream.restart()
    monkeypatch.setattr(system_effects, "_resolve_callback_proposal", lambda *a, **k: stale_view)
    stale = _signed_callback(callback_client, key, delivery="delivery-3")
    assert (stale.status_code, stale.json()["data"]["status"]) == (200, "committed")
    assert stale.json()["data"]["receipt_id"] == first.json()["data"]["receipt_id"]
    after = _effect_facts(reference_app, proposal_id)
    assert (after["state"], after["version"], after["receipts"], after["audited"]) == (
        COMMITTED,
        6,
        1,
        1,
    )
    assert downstream.mutations() == 1
