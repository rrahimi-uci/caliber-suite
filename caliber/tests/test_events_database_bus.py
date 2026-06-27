"""Unit tests for the database-backed event bus adapter."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker

from caliber.config import CaliberConfig
from caliber.db import Base
from caliber.db.models import CaliberLiveEvent
from caliber.db.session import create_engine_from_config, sessionmaker_from_engine
from caliber.events.database_bus import DatabaseEventBus
from caliber.events.nats_bus import build_event_bus


@pytest.fixture
def live_event_session_factory(tmp_path: Path) -> sessionmaker[Session]:
    config = CaliberConfig(database_url=f"sqlite+pysqlite:///{tmp_path / 'events.db'}")
    engine = create_engine_from_config(config)
    Base.metadata.create_all(engine)
    factory = sessionmaker_from_engine(engine)
    try:
        yield factory
    finally:
        Base.metadata.drop_all(engine)
        engine.dispose()


@pytest.mark.asyncio
async def test_database_event_bus_publishes_local_and_persists(
    live_event_session_factory: sessionmaker[Session],
) -> None:
    bus = DatabaseEventBus(
        session_factory=live_event_session_factory,
        poll_interval_seconds=0.01,
    )
    await bus.start()

    subscription = bus.subscribe()
    next_task = asyncio.create_task(subscription.__anext__())
    await asyncio.sleep(0)

    bus.publish({"type": "workflow.run.queued", "workflow_run_id": "WR-1"})

    event = await asyncio.wait_for(next_task, timeout=1.0)
    assert event == {"type": "workflow.run.queued", "workflow_run_id": "WR-1"}

    persisted: list[CaliberLiveEvent] = []
    for _ in range(20):
        with live_event_session_factory() as session:
            persisted = session.execute(select(CaliberLiveEvent)).scalars().all()
        if persisted:
            break
        await asyncio.sleep(0.01)

    assert len(persisted) == 1
    assert persisted[0].event_type == "workflow.run.queued"
    assert persisted[0].payload == {"type": "workflow.run.queued", "workflow_run_id": "WR-1"}

    await subscription.aclose()
    await bus.stop()


@pytest.mark.asyncio
async def test_database_event_bus_forwards_remote_events(
    live_event_session_factory: sessionmaker[Session],
) -> None:
    bus_a = DatabaseEventBus(
        session_factory=live_event_session_factory,
        poll_interval_seconds=0.01,
    )
    bus_b = DatabaseEventBus(
        session_factory=live_event_session_factory,
        poll_interval_seconds=0.01,
    )
    await bus_a.start()
    await bus_b.start()

    subscription = bus_b.subscribe()
    next_task = asyncio.create_task(subscription.__anext__())
    await asyncio.sleep(0)

    bus_a.publish({"type": "workflow.run.started", "workflow_run_id": "WR-2"})

    event = await asyncio.wait_for(next_task, timeout=1.0)
    assert event == {
        "type": "workflow.run.started",
        "workflow_run_id": "WR-2",
        "_caliber_remote": True,
    }

    await subscription.aclose()
    await bus_b.stop()
    await bus_a.stop()


@pytest.mark.asyncio
async def test_database_event_bus_ignores_historical_rows_on_start(
    live_event_session_factory: sessionmaker[Session],
) -> None:
    with live_event_session_factory() as session:
        session.add(
            CaliberLiveEvent(
                origin="seed",
                event_type="workflow.run.completed",
                payload={"type": "workflow.run.completed", "workflow_run_id": "WR-old"},
            )
        )
        session.commit()

    bus = DatabaseEventBus(
        session_factory=live_event_session_factory,
        poll_interval_seconds=0.01,
    )
    await bus.start()

    subscription = bus.subscribe()
    next_task = asyncio.create_task(subscription.__anext__())
    await asyncio.sleep(0.05)
    assert next_task.done() is False

    next_task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await next_task
    await subscription.aclose()
    await bus.stop()


@pytest.mark.asyncio
async def test_database_event_bus_start_requires_live_event_table(
    tmp_path: Path,
) -> None:
    engine = create_engine(
        f"sqlite+pysqlite:///{tmp_path / 'missing-table.db'}",
        connect_args={"check_same_thread": False, "timeout": 30},
        pool_pre_ping=True,
    )
    factory = sessionmaker_from_engine(engine)
    bus = DatabaseEventBus(session_factory=factory)
    try:
        with pytest.raises(RuntimeError, match="caliber_live_events table"):
            await bus.start()
    finally:
        engine.dispose()


def test_build_event_bus_selects_database_backend(
    live_event_session_factory: sessionmaker[Session],
) -> None:
    database_bus = build_event_bus(
        CaliberConfig(workflow_run_event_backend="database"),
        session_factory=live_event_session_factory,
    )
    assert isinstance(database_bus, DatabaseEventBus)


def test_build_event_bus_requires_session_factory_for_database() -> None:
    with pytest.raises(ValueError, match="session_factory"):
        build_event_bus(CaliberConfig(workflow_run_event_backend="database"))
