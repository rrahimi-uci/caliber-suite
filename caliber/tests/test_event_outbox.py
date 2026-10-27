"""The transactional outbox and the consumer inbox (B13.4, #685).

Every case runs on SQLite and, where ``CALIBER_TEST_POSTGRES_URL`` is set, on PostgreSQL, against a database migrated
by Alembic. Time is a fake clock; no case sleeps to make a retry due.
"""

from __future__ import annotations

import asyncio
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, func, select
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from caliber.contracts import ContractValidationError, validate_record
from caliber.db.models import (
    CaliberEventInbox,
    CaliberEventOutbox,
    CaliberProject,
    CaliberWorkflow,
    CaliberWorkflowRun,
    CaliberWorkflowRunEvent,
)
from caliber.events import outbox
from caliber.events.outbox import (
    DEAD,
    DELIVERED,
    PENDING,
    OutboxConflict,
    OutboxPublisher,
    SqlInboxConsumer,
    bind_outbox,
    build_envelope,
    claim_unprocessed,
    enqueue,
    mark_processed,
    outbox_active,
    outbox_depth,
    payload_hash,
    receive,
)
from caliber.workflows.run_events import append_run_event
from tests.dialect_helpers import DIALECTS, dialect_database_url

PROJECT_ROOT = Path(__file__).resolve().parent.parent
ALEMBIC_INI = PROJECT_ROOT / "alembic.ini"
T0 = datetime(
    2026, 1, 15, 12, 0, 0, tzinfo=timezone.utc
)  # in the past, so a real clock finds these events due


class Clock:
    def __init__(self) -> None:
        self.now = T0

    def __call__(self) -> datetime:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += timedelta(seconds=seconds)


@contextmanager
def _database(
    dialect: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Iterator[tuple[Engine, sessionmaker[Session]]]:
    with dialect_database_url(dialect, tmp_path, "event_outbox") as url:
        monkeypatch.setenv("CALIBER_DATABASE_URL", url)
        monkeypatch.chdir(PROJECT_ROOT)
        command.upgrade(Config(str(ALEMBIC_INI)), "head")
        engine = create_engine(url)
        try:
            yield engine, sessionmaker(engine, expire_on_commit=False)
        finally:
            engine.dispose()


def _seed_run(factory: sessionmaker[Session], run_id: str = "RUN-ob") -> str:
    with factory() as session:
        session.add_all(
            [
                CaliberProject(project_id="PRJ-ob", name="Ob", owner="@test"),
                CaliberWorkflow(workflow_id="wf-ob", project_id="PRJ-ob", name="Ob", owner="@test"),
                CaliberWorkflowRun(
                    workflow_run_id=run_id,
                    workflow_id="wf-ob",
                    project_id="PRJ-ob",
                    status="running",
                ),
            ]
        )
        session.commit()
    return run_id


def _count(factory: sessionmaker[Session], model: Any, *where: Any) -> int:
    with factory() as session:
        return int(
            session.execute(select(func.count()).select_from(model).where(*where)).scalar_one()
        )


class Recorder:
    """A consumer that records what it was handed and can be told to fail."""

    def __init__(self, name: str = "recorder") -> None:
        self.name = name
        self.received: list[str] = []
        self.fail = 0

    def receive(self, envelope: dict[str, Any], payload: dict[str, Any]) -> None:
        if self.fail:
            self.fail -= 1
            raise RuntimeError("consumer down")
        self.received.append(str(envelope["id"]))


def _event(
    session: Session, number: int, *, payload: dict[str, Any] | None = None
) -> CaliberEventOutbox:
    return enqueue(
        session,
        event_id=f"evt-{number}",
        event_type="caliber.test",
        aggregate_type="thing",
        aggregate_id="T-1",
        project_id="PRJ-ob",
        payload=payload or {"n": number, "secret": "SECRET-PAYLOAD"},
        now=T0,
    )


# ---------------------------------------------------------------- the envelope and enqueue


def test_the_envelope_is_an_adr_0003_event_envelope_that_references_its_payload() -> None:
    envelope = build_envelope(
        event_id="e-1", event_type="caliber.x", payload={"a": 1}, time=T0, correlation_id="c-1"
    )
    validate_record("event_envelope", envelope)
    assert envelope["datahash"] == payload_hash({"a": 1}) and envelope["datahash"].startswith(
        "sha256:"
    )
    assert envelope["dataref"] == "caliber://event_outbox/e-1" and "a" not in envelope, (
        "never inline"
    )
    assert envelope["time"] == "2026-01-15T12:00:00.000000Z" and envelope["tenantid"] == "local"
    assert payload_hash({"b": 1, "a": 2}) == payload_hash({"a": 2, "b": 1}), (
        "key order is not content"
    )
    with pytest.raises(ContractValidationError):
        build_envelope(event_id="", event_type="x", payload={}, time=T0, correlation_id="c")


@pytest.mark.parametrize("dialect", DIALECTS)
def test_enqueue_is_idempotent_on_the_event_id_and_refuses_different_content(
    dialect: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with _database(dialect, tmp_path, monkeypatch) as (_, factory):
        with factory() as session:
            first = _event(session, 1)
            session.commit()
            again = _event(session, 1)
            assert again.event_id == first.event_id and again.status == PENDING
            session.commit()
            with pytest.raises(OutboxConflict, match="different content"):
                _event(session, 1, payload={"n": "other"})
            session.rollback()
        assert _count(factory, CaliberEventOutbox) == 1


# ---------------------------------------------------------------- atomic with the business transition


@pytest.mark.parametrize("dialect", DIALECTS)
def test_event_and_outbox_atomic(
    dialect: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The run event and its outbox row commit together or not at all."""
    with _database(dialect, tmp_path, monkeypatch) as (_, factory):
        run_id = _seed_run(factory)
        bind_outbox(True)
        with factory() as session:
            append_run_event(
                session,
                workflow_run_id=run_id,
                project_id="PRJ-ob",
                event_type="workflow.run.queued",
            )
            session.rollback()  # the transition did not happen
        assert (
            _count(factory, CaliberWorkflowRunEvent) == 0
            and _count(factory, CaliberEventOutbox) == 0
        )

        with factory() as session:
            event = append_run_event(
                session,
                workflow_run_id=run_id,
                project_id="PRJ-ob",
                event_type="workflow.run.queued",
                payload={"alias": "prod"},
                node_id="n1",
            )
            session.commit()
            sequence = event.sequence
        with factory() as session:
            row = session.execute(select(CaliberEventOutbox)).scalar_one()
            assert row.event_id == f"run-event:{run_id}:{sequence}"
            assert (row.event_type, row.aggregate_type, row.aggregate_id) == (
                "caliber.workflow.run.queued",
                "workflow_run",
                run_id,
            )
            assert row.payload == {
                "workflow_run_id": run_id,
                "project_id": "PRJ-ob",
                "sequence": sequence,
                "event_type": "workflow.run.queued",
                "node_id": "n1",
                "payload": {"alias": "prod"},
            }
            validate_record("event_envelope", row.envelope)
            assert (row.status, row.attempts) == (PENDING, 0)


@pytest.mark.parametrize("dialect", DIALECTS)
def test_the_outbox_is_off_unless_bound_and_a_sequence_conflict_leaves_one_row_per_event(
    dialect: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from caliber.workflows import run_events

    with _database(dialect, tmp_path, monkeypatch) as (_, factory):
        run_id = _seed_run(factory)
        with factory() as session:
            append_run_event(session, workflow_run_id=run_id, project_id="PRJ-ob", event_type="a")
            session.commit()
        assert _count(factory, CaliberEventOutbox) == 0, "off by default: nothing is written"

        bind_outbox(True)
        real = run_events._current_max
        stale = {"left": 1}

        def stale_once(session: Session, workflow_run_id: str) -> int:
            if stale["left"]:
                stale["left"] -= 1
                return 0  # a stale read: the first allocation collides and retries
            return real(session, workflow_run_id)

        monkeypatch.setattr(run_events, "_current_max", stale_once)
        with factory() as session:
            append_run_event(session, workflow_run_id=run_id, project_id="PRJ-ob", event_type="b")
            session.commit()
        assert _count(factory, CaliberWorkflowRunEvent) == 2
        assert _count(factory, CaliberEventOutbox) == 1, (
            "the abandoned attempt left no outbox row behind"
        )


# ---------------------------------------------------------------- the publisher


@pytest.mark.parametrize("dialect", DIALECTS)
def test_the_publisher_hands_each_event_to_every_consumer_then_marks_it_delivered(
    dialect: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with _database(dialect, tmp_path, monkeypatch) as (_, factory):
        with factory() as session:
            for number in range(3):
                _event(session, number)
            session.commit()
        first, second = Recorder("one"), Recorder("two")
        clock = Clock()
        publisher = OutboxPublisher(factory, consumers=[first, second], clock=clock)
        result = publisher.publish_batch()
        assert (result.delivered, result.retried, result.dead) == (3, 0, 0)
        assert first.received == second.received and sorted(first.received) == [
            "evt-0",
            "evt-1",
            "evt-2",
        ]
        with factory() as session:
            depth = outbox_depth(session)
            assert (depth["pending"], depth["delivered"], depth["dead"]) == (0, 3, 0)
            assert depth["oldest_pending"] is None
            row = session.get(CaliberEventOutbox, "evt-0")
            assert row is not None and row.status == DELIVERED and row.delivered_at is not None
            assert row.claimed_by is None and row.last_error is None
        assert publisher.publish_batch().claimed == 0, "nothing left to do"


@pytest.mark.parametrize("dialect", DIALECTS)
def test_with_no_consumer_an_event_is_delivered_at_once_and_pruned_after_retention(
    dialect: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with _database(dialect, tmp_path, monkeypatch) as (_, factory):
        with factory() as session:
            _event(session, 1)
            session.commit()
        clock = Clock()
        publisher = OutboxPublisher(factory, clock=clock, retention_days=7)
        assert publisher.publish_batch().delivered == 1
        assert publisher.prune_delivered() == 0, "still inside the retention window"
        clock.advance(8 * 86400)
        assert publisher.prune_delivered() == 1
        assert _count(factory, CaliberEventOutbox) == 0


@pytest.mark.parametrize("dialect", DIALECTS)
def test_a_failing_consumer_is_retried_with_backoff_and_the_event_is_never_lost(
    dialect: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with _database(dialect, tmp_path, monkeypatch) as (_, factory):
        with factory() as session:
            _event(session, 1)
            session.commit()
        consumer, clock = Recorder(), Clock()
        consumer.fail = 2
        publisher = OutboxPublisher(
            factory, consumers=[consumer], clock=clock, backoff_base_seconds=10
        )
        assert publisher.publish_batch().retried == 1
        assert publisher.publish_batch().claimed == 0, "not due yet: the backoff holds"
        with factory() as session:
            row = session.get(CaliberEventOutbox, "evt-1")
            assert row is not None and (row.status, row.attempts) == (PENDING, 1)
            assert row.last_error is not None and "consumer down" in row.last_error
            assert "SECRET-PAYLOAD" not in row.last_error, "the payload is never in the error"
        clock.advance(11)
        assert publisher.publish_batch().retried == 1  # the second failure: 20 s now
        clock.advance(11)
        assert publisher.publish_batch().claimed == 0
        clock.advance(10)
        assert publisher.publish_batch().delivered == 1
        assert consumer.received == ["evt-1"]


@pytest.mark.parametrize("dialect", DIALECTS)
def test_an_event_that_exhausts_its_attempts_is_dead_and_kept_for_an_operator(
    dialect: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with _database(dialect, tmp_path, monkeypatch) as (_, factory):
        with factory() as session:
            _event(session, 1)
            session.commit()
        consumer, clock = Recorder(), Clock()
        consumer.fail = 99
        publisher = OutboxPublisher(
            factory,
            consumers=[consumer],
            clock=clock,
            max_attempts=3,
            backoff_base_seconds=1,
            backoff_cap_seconds=2,
        )
        outcomes = []
        for _ in range(3):
            clock.advance(5)
            outcomes.append(publisher.publish_batch())
        assert [o.dead for o in outcomes] == [0, 0, 1]
        with factory() as session:
            row = session.get(CaliberEventOutbox, "evt-1")
            assert row is not None and (row.status, row.attempts) == (DEAD, 3)
            assert outbox_depth(session)["dead"] == 1
        clock.advance(1000)
        assert publisher.publish_batch().claimed == 0, "a dead event is not retried"


@pytest.mark.parametrize("dialect", DIALECTS)
def test_a_claim_held_by_a_dead_publisher_is_taken_over_when_its_lease_expires(
    dialect: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Crash before the handoff: the row was claimed, nothing was handed over, the claimer is gone."""
    with _database(dialect, tmp_path, monkeypatch) as (_, factory):
        with factory() as session:
            _event(session, 1)
            session.commit()
        clock = Clock()
        dying = OutboxPublisher(
            factory, consumers=[Recorder("dying")], clock=clock, lease_seconds=30, owner_id="dying"
        )

        class Crash(Recorder):
            def receive(self, envelope: dict[str, Any], payload: dict[str, Any]) -> None:
                raise SystemExit("the process dies here")

        dying.consumers = [Crash("dying")]
        with pytest.raises(SystemExit):
            dying.publish_batch()  # claimed, then died in the handoff
        with factory() as session:
            row = session.get(CaliberEventOutbox, "evt-1")
            assert row is not None and row.claimed_by == "dying" and row.status == PENDING
        survivor_consumer = Recorder()
        survivor = OutboxPublisher(
            factory, consumers=[survivor_consumer], clock=clock, owner_id="survivor"
        )
        assert survivor.publish_batch().claimed == 0, "still leased to the dead publisher"
        clock.advance(31)
        assert survivor.publish_batch().delivered == 1
        assert survivor_consumer.received == ["evt-1"]


@pytest.mark.parametrize("dialect", DIALECTS)
def test_publish_after_crash(dialect: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The three crash points of the issue: before the handoff, after it but before the mark, and during consume."""
    with _database(dialect, tmp_path, monkeypatch) as (_, factory):
        run_id = _seed_run(factory)
        bind_outbox(True)
        with factory() as session:
            append_run_event(
                session,
                workflow_run_id=run_id,
                project_id="PRJ-ob",
                event_type="workflow.run.queued",
            )
            session.commit()
        # 1. The process died right after the commit: the event is in the outbox, nothing was published.
        assert _count(factory, CaliberEventOutbox, CaliberEventOutbox.status == PENDING) == 1
        assert _count(factory, CaliberEventInbox) == 0

        inbox = SqlInboxConsumer("billing", factory)
        clock = Clock()
        clock.now = datetime.now(timezone.utc)  # the event was enqueued by the real clock
        publisher = OutboxPublisher(factory, consumers=[inbox], clock=clock, lease_seconds=5)

        # 2. The handoff succeeds, then the mark fails (a crash between them).
        real_settle = publisher._settle_delivered
        calls = {"n": 0}

        def die_before_mark(event_id: str) -> None:
            calls["n"] += 1
            raise SystemExit("the process dies after the handoff, before the mark")

        publisher._settle_delivered = die_before_mark  # type: ignore[method-assign]
        with pytest.raises(SystemExit):
            publisher.publish_batch()
        assert _count(factory, CaliberEventInbox) == 1, "the handoff was durable"
        assert _count(factory, CaliberEventOutbox, CaliberEventOutbox.status == PENDING) == 1, (
            "but not marked"
        )

        # 3. A new publisher redelivers; the inbox ignores the duplicate; the event is marked delivered.
        publisher._settle_delivered = real_settle  # type: ignore[method-assign]
        clock.advance(6)
        assert publisher.publish_batch().delivered == 1
        assert _count(factory, CaliberEventInbox) == 1, (
            "exactly one inbox row however often it was handed over"
        )
        assert _count(factory, CaliberEventOutbox, CaliberEventOutbox.status == DELIVERED) == 1

        # 4. The consumer dies while consuming: the event is still there for it, and processing is idempotent.
        with factory() as session:
            pending = claim_unprocessed(session, "billing")
            assert [row.event_id for row in pending] == [f"run-event:{run_id}:1"]
            # (the consumer crashes here without marking: nothing changed)
        with factory() as session:
            assert len(claim_unprocessed(session, "billing")) == 1
            assert mark_processed(session, "billing", f"run-event:{run_id}:1", now=T0)
            session.commit()
            assert not mark_processed(session, "billing", f"run-event:{run_id}:1"), "processed once"
            assert claim_unprocessed(session, "billing") == []


@pytest.mark.parametrize("dialect", DIALECTS)
def test_the_inbox_is_the_consumers_deduplication(
    dialect: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with _database(dialect, tmp_path, monkeypatch) as (_, factory):
        envelope = build_envelope(
            event_id="e-9", event_type="caliber.x", payload={"a": 1}, time=T0, correlation_id="c"
        )
        with factory() as session:
            assert receive(session, "one", envelope, {"a": 1}) is True
            session.commit()
            assert receive(session, "one", envelope, {"a": 1}) is False, (
                "the consumer already has it"
            )
            assert receive(session, "two", envelope, {"a": 1}) is True, (
                "another consumer is another inbox"
            )
            session.commit()
        assert _count(factory, CaliberEventInbox) == 2


def test_concurrent_publishers_on_postgresql_hand_each_event_over_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from tests.dialect_helpers import POSTGRES_TEST_URL

    if not POSTGRES_TEST_URL:
        pytest.skip("SKIP LOCKED is PostgreSQL's")
    with _database("postgresql", tmp_path, monkeypatch) as (_, factory):
        with factory() as session:
            for number in range(40):
                _event(session, number)
            session.commit()
        handed: list[str] = []
        lock = threading.Lock()

        class Counting(Recorder):
            def receive(self, envelope: dict[str, Any], payload: dict[str, Any]) -> None:
                with lock:
                    handed.append(str(envelope["id"]))

        barrier = threading.Barrier(4)

        def work(index: int) -> None:
            publisher = OutboxPublisher(
                factory, consumers=[Counting()], owner_id=f"p{index}", batch_size=5
            )
            barrier.wait(10)
            while publisher.publish_batch().claimed:
                pass

        threads = [threading.Thread(target=work, args=(i,), daemon=True) for i in range(4)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(60)
        assert sorted(handed) == sorted(f"evt-{n}" for n in range(40)), (
            "every event exactly once, none twice"
        )
        assert _count(factory, CaliberEventOutbox, CaliberEventOutbox.status == DELIVERED) == 40


# ---------------------------------------------------------------- the loop and the switch


def test_the_publisher_loop_delivers_in_the_background_and_stops_cleanly(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def scenario(factory: sessionmaker[Session]) -> None:
        with factory() as session:
            _event(session, 1)
            session.commit()
        consumer = Recorder()
        publisher = OutboxPublisher(factory, consumers=[consumer], poll_seconds=0.05)
        await publisher.start()
        with pytest.raises(RuntimeError, match="already running"):
            await publisher.start()
        for _ in range(100):
            if consumer.received:
                break
            await asyncio.sleep(0.05)
        await publisher.stop()
        await publisher.stop()  # idempotent
        assert consumer.received == ["evt-1"]

    with _database("sqlite", tmp_path, monkeypatch) as (_, factory):
        asyncio.run(scenario(factory))


def test_a_failing_tick_does_not_end_the_loop(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def scenario(factory: sessionmaker[Session]) -> None:
        publisher = OutboxPublisher(factory, poll_seconds=0.05)
        calls = {"n": 0}
        real = publisher.publish_batch

        def flaky() -> Any:
            calls["n"] += 1
            if calls["n"] == 1:
                raise RuntimeError("database blip")
            return real()

        publisher.publish_batch = flaky  # type: ignore[method-assign]
        await publisher.start()
        for _ in range(100):
            if calls["n"] >= 3:
                break
            await asyncio.sleep(0.05)
        await publisher.stop()
        assert calls["n"] >= 3, "the loop kept ticking after a failure"

    with _database("sqlite", tmp_path, monkeypatch) as (_, factory):
        asyncio.run(scenario(factory))


def test_the_loop_prunes_delivered_events_every_sixty_ticks(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def scenario(factory: sessionmaker[Session]) -> None:
        publisher = OutboxPublisher(factory, poll_seconds=0.01)
        pruned = {"n": 0}
        publisher.prune_delivered = lambda: pruned.__setitem__("n", pruned["n"] + 1) or 0  # type: ignore[method-assign]
        publisher._ticks = 59  # the next tick is the sixtieth
        await publisher.start()
        for _ in range(100):
            if pruned["n"]:
                break
            await asyncio.sleep(0.02)
        await publisher.stop()
        assert pruned["n"] >= 1

    with _database("sqlite", tmp_path, monkeypatch) as (_, factory):
        asyncio.run(scenario(factory))


def test_the_outbox_is_active_in_the_reference_profile_or_with_the_flag() -> None:
    from types import SimpleNamespace

    assert outbox_active(SimpleNamespace(profile="reference", event_outbox_enabled=False)) is True
    assert outbox_active(SimpleNamespace(profile="development", event_outbox_enabled=True)) is True
    assert (
        outbox_active(SimpleNamespace(profile="development", event_outbox_enabled=False)) is False
    )
    assert outbox_active(None) is False
    bind_outbox(True)
    assert outbox.outbox_enabled() is True
    bind_outbox(False)
    assert outbox.outbox_enabled() is False


def _stale_first_read(session: Session) -> None:
    """Make the session's first ``get`` miss, as if another transaction committed the row just after the read."""
    real_get = session.get
    state = {"first": True}

    def get(*args: Any, **kwargs: Any) -> Any:
        if state["first"]:
            state["first"] = False
            return None
        return real_get(*args, **kwargs)

    session.get = get  # type: ignore[method-assign]


@pytest.mark.parametrize("dialect", DIALECTS)
def test_an_enqueue_that_loses_the_insert_race_returns_the_winner_or_refuses_different_content(
    dialect: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with _database(dialect, tmp_path, monkeypatch) as (_, factory):
        with factory() as winner:
            _event(winner, 1)
            winner.commit()
        with factory() as session:
            _stale_first_read(session)
            again = _event(session, 1)
            assert again.event_id == "evt-1" and again.status == PENDING
        with factory() as session:
            _stale_first_read(session)
            with pytest.raises(OutboxConflict, match="different content"):
                _event(session, 1, payload={"n": "other"})
        assert _count(factory, CaliberEventOutbox) == 1


@pytest.mark.parametrize("dialect", DIALECTS)
def test_a_receive_that_loses_the_insert_race_reports_a_duplicate(
    dialect: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with _database(dialect, tmp_path, monkeypatch) as (_, factory):
        envelope = build_envelope(
            event_id="evt-r",
            event_type="caliber.test",
            payload={"a": 1},
            time=T0,
            correlation_id="c",
        )
        with factory() as first:
            assert receive(first, "billing", envelope, {"a": 1})
            first.commit()
        with factory() as session:
            _stale_first_read(session)
            assert receive(session, "billing", envelope, {"a": 1}) is False
        assert _count(factory, CaliberEventInbox) == 1
