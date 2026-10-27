"""V09 public path: business events survive crashes and a saturated notification bus (B13.4, #685).

The reference-profile app on PostgreSQL (the same logic on SQLite in the development profile with the outbox switched
on). A run is admitted through the public API and every run event it produced has a durable outbox row written in the
same transaction. A publisher that dies after handing an event over is followed by one that redelivers it, and the
consumer's inbox holds it exactly once. When the in-process event bus's bounded subscriber queue is saturated and
drops notifications, no business event is lost: all of them are in the outbox and reach the consumer.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from typing import Any

import pytest
from sqlalchemy import func, select

from caliber.db.models import CaliberEventInbox, CaliberEventOutbox, CaliberWorkflowRunEvent
from caliber.events.bus import EventBus
from caliber.events.outbox import DELIVERED, PENDING, OutboxPublisher, SqlInboxConsumer, bind_outbox
from caliber.workflows.run_events import append_run_event
from tests.egi_scenarios.conftest import EDITOR_A, PROJECT_A, ReferenceApp, Workspaces
from tests.egi_scenarios.test_b02_admission import _call, _published_workflow

SATURATING_EVENTS = 400  # far over the bus's per-subscriber queue bound


def _admit_a_run(app: ReferenceApp, key: str) -> str:
    workflow_id = _published_workflow(app)
    response = _call(
        app,
        "POST",
        EDITOR_A,
        PROJECT_A,
        "/workflow-runs",
        {"workflow_id": workflow_id, "alias": "manual", "input": "refund?", "idempotency_key": key},
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
