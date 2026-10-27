"""The execution role: acquire, renew and lose the lease, and start and stop the owner-only loops with it (B13.3, #673).

The controller is driven with fake loops against a real migrated database on SQLite and, where
``CALIBER_TEST_POSTGRES_URL`` is set, PostgreSQL. Ticks are called directly (``tick()``) so the tests do not depend on
timing; one test runs the real background task with a short lease.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session, sessionmaker

from caliber.execution_role import (
    OWNER,
    ROLES,
    SECONDARY,
    ExecutionRoleController,
    new_owner_id,
)
from caliber.ownership import DEFAULT_LEASE, Lease, current_lease
from tests.dialect_helpers import DIALECTS, dialect_database_url

PROJECT_ROOT = Path(__file__).resolve().parent.parent
ALEMBIC_INI = PROJECT_ROOT / "alembic.ini"
pytestmark = pytest.mark.slow


@contextmanager
def _database(
    dialect: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Iterator[tuple[Engine, sessionmaker[Session]]]:
    with dialect_database_url(dialect, tmp_path, "execution_role") as url:
        monkeypatch.setenv("CALIBER_DATABASE_URL", url)
        monkeypatch.chdir(PROJECT_ROOT)
        command.upgrade(Config(str(ALEMBIC_INI)), "head")
        engine = create_engine(url)
        try:
            yield engine, sessionmaker(engine)
        finally:
            engine.dispose()


class _Records(logging.Handler):
    """Collects this logger's records. ``caplog`` cannot be used: Alembic's ``fileConfig`` (run by the migrated
    database fixture) replaces the root handlers, caplog's included."""

    def __init__(self) -> None:
        super().__init__(level=logging.DEBUG)
        self.messages: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.messages.append(record.getMessage())


@contextmanager
def _logged() -> Iterator[_Records]:
    handler = _Records()
    logger = logging.getLogger("caliber.execution_role")
    previous = logger.level
    logger.addHandler(handler)
    logger.setLevel(logging.DEBUG)
    try:
        yield handler
    finally:
        logger.removeHandler(handler)
        logger.setLevel(previous)


class Loops:
    """Stands in for the ten owner-only loops: records what the controller did to them."""

    def __init__(self) -> None:
        self.events: list[str] = []
        self.running = False

    async def start(self) -> None:
        assert not self.running, "started twice without a stop"
        self.running = True
        self.events.append("start")

    async def stop(self) -> None:
        assert self.running, "stopped while not running"
        self.running = False
        self.events.append("stop")


def _controller(
    factory: sessionmaker[Session], loops: Loops, owner: str, *, lease: float = 30.0, **kwargs: Any
) -> ExecutionRoleController:
    return ExecutionRoleController(
        factory,
        lease_seconds=lease,
        start_loops=loops.start,
        stop_loops=loops.stop,
        owner_id=owner,
        **kwargs,
    )


def _expire(engine: Engine) -> None:
    past = (
        "timezone('utc', clock_timestamp()) - interval '1 second'"
        if engine.dialect.name == "postgresql"
        else "strftime('%Y-%m-%d %H:%M:%f', 'now', '-1 seconds')"
    )
    with engine.begin() as connection:
        connection.execute(text(f"UPDATE caliber_execution_owner SET expires_at = {past}"))


def _stored(factory: sessionmaker[Session]) -> Lease | None:
    with factory() as session:
        return current_lease(session, DEFAULT_LEASE)


# ---------------------------------------------------------------- one owner, the rest secondary


@pytest.mark.parametrize("dialect", DIALECTS)
def test_one_process_is_owner_and_the_other_is_secondary(
    dialect: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def scenario(factory: sessionmaker[Session]) -> None:
        a_loops, b_loops = Loops(), Loops()
        a, b = _controller(factory, a_loops, "node-a"), _controller(factory, b_loops, "node-b")
        await a.start()
        await b.start()
        try:
            assert (a.role, b.role) == (OWNER, SECONDARY)
            assert a_loops.events == ["start"] and b_loops.events == [], (
                "a secondary never starts a loop"
            )
            for _ in range(3):  # ticks renew for the owner and keep trying for the secondary
                assert await a.tick() == OWNER and await b.tick() == SECONDARY
            assert b_loops.events == []
            lease = _stored(factory)
            assert lease is not None and (lease.owner_id, lease.fencing_token) == ("node-a", 1)
        finally:
            await b.stop()
            await a.stop()

    with _database(dialect, tmp_path, monkeypatch) as (_, factory):
        asyncio.run(scenario(factory))


@pytest.mark.parametrize("dialect", DIALECTS)
def test_a_secondary_becomes_owner_when_the_owner_stops_and_releases_the_lease(
    dialect: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def scenario(factory: sessionmaker[Session]) -> None:
        a_loops, b_loops = Loops(), Loops()
        a, b = _controller(factory, a_loops, "node-a"), _controller(factory, b_loops, "node-b")
        await a.start()
        await b.start()
        await a.stop()  # a rolling restart: stop the loops and release the lease at once
        assert a_loops.events == ["start", "stop"] and a.role == SECONDARY and a.lease is None
        assert await b.tick() == OWNER and b_loops.events == ["start"]
        lease = _stored(factory)
        assert lease is not None and (lease.owner_id, lease.fencing_token) == ("node-b", 2)
        await b.stop()

    with _database(dialect, tmp_path, monkeypatch) as (_, factory):
        asyncio.run(scenario(factory))


@pytest.mark.parametrize("dialect", DIALECTS)
def test_a_killed_owner_is_replaced_once_its_lease_expires(
    dialect: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The owner never renews or releases (it died); the lease lapses by database time and the secondary takes over."""

    async def scenario(engine: Engine, factory: sessionmaker[Session]) -> None:
        a_loops, b_loops = Loops(), Loops()
        a, b = _controller(factory, a_loops, "node-a"), _controller(factory, b_loops, "node-b")
        await a.start()
        await b.start()
        assert await b.tick() == SECONDARY, "still held"
        _expire(engine)  # no renewal comes: the owner is gone
        assert await b.tick() == OWNER and b_loops.running
        lease = _stored(factory)
        assert lease is not None and (lease.owner_id, lease.fencing_token) == ("node-b", 2)
        await b.stop()

    with _database(dialect, tmp_path, monkeypatch) as (engine, factory):
        asyncio.run(scenario(engine, factory))


# ---------------------------------------------------------------- losing the lease


@pytest.mark.parametrize("dialect", DIALECTS)
def test_an_owner_that_loses_the_lease_stops_its_loops_before_it_reports_secondary_and_can_reacquire(
    dialect: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def scenario(engine: Engine, factory: sessionmaker[Session]) -> None:
        loops, rival_loops = Loops(), Loops()
        owner = _controller(factory, loops, "node-a")
        rival = _controller(factory, rival_loops, "node-b")
        await owner.start()
        _expire(engine)
        assert await rival.tick() == OWNER  # another process took the lapsed lease
        observed: list[str] = []
        original_stop = loops.stop

        async def stop_and_observe() -> None:
            observed.append(
                owner.role
            )  # the role is already secondary while the loops are being stopped
            await original_stop()

        owner._stop_loops = stop_and_observe  # type: ignore[method-assign]
        with _logged() as records:
            assert await owner.tick() == SECONDARY
        assert observed == [SECONDARY] and loops.events == ["start", "stop"] and owner.lease is None
        assert any("lease lost" in message for message in records.messages)
        # While the rival holds it the old owner stays secondary; once the rival is gone it reacquires, higher token.
        assert await owner.tick() == SECONDARY and loops.events == ["start", "stop"]
        await rival.stop()
        assert await owner.tick() == OWNER and loops.events == ["start", "stop", "start"]
        lease = _stored(factory)
        assert lease is not None and (lease.owner_id, lease.fencing_token) == ("node-a", 3)
        await owner.stop()

    with _database(dialect, tmp_path, monkeypatch) as (engine, factory):
        asyncio.run(scenario(engine, factory))


def test_an_unreachable_database_costs_the_owner_its_loops_and_a_secondary_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def scenario(factory: sessionmaker[Session]) -> None:
        loops, idle_loops = Loops(), Loops()
        owner, idle = (
            _controller(factory, loops, "node-a"),
            _controller(factory, idle_loops, "node-b"),
        )
        await owner.start()
        await idle.start()

        def broken(*_a: Any, **_k: Any) -> Any:
            raise OperationalError("UPDATE", {}, Exception("connection lost"))

        monkeypatch.setattr(owner, "_renew", broken)
        monkeypatch.setattr(idle, "_acquire", broken)
        assert await owner.tick() == SECONDARY and loops.events == ["start", "stop"], "fail closed"
        assert await idle.tick() == SECONDARY and idle_loops.events == []
        monkeypatch.undo()
        assert await idle.tick() == SECONDARY, "still held by the old row until it expires"
        await idle.stop()

    with _database("sqlite", tmp_path, monkeypatch) as (_, factory):
        asyncio.run(scenario(factory))


# ---------------------------------------------------------------- the lifecycle


@pytest.mark.parametrize("dialect", DIALECTS)
def test_stop_stops_the_loops_releases_the_lease_and_is_idempotent(
    dialect: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def scenario(factory: sessionmaker[Session]) -> None:
        loops = Loops()
        controller = _controller(factory, loops, "node-a")
        await controller.start()
        await controller.stop()
        await controller.stop()
        assert loops.events == ["start", "stop"] and controller.role == SECONDARY
        lease = _stored(factory)
        assert lease is not None and lease.expires_at <= __import__("datetime").datetime.now(
            __import__("datetime").timezone.utc
        ).replace(tzinfo=None) + __import__("datetime").timedelta(seconds=1)

    with _database(dialect, tmp_path, monkeypatch) as (_, factory):
        asyncio.run(scenario(factory))


def test_a_release_that_fails_at_shutdown_is_logged_not_raised(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def scenario(factory: sessionmaker[Session]) -> None:
        loops = Loops()
        controller = _controller(factory, loops, "node-a")
        await controller.start()
        monkeypatch.setattr(
            controller, "_release", lambda lease: (_ for _ in ()).throw(RuntimeError("down"))
        )
        with _logged() as records:
            await controller.stop()
        assert loops.events == ["start", "stop"]
        assert any("could not release" in message for message in records.messages)

    with _database("sqlite", tmp_path, monkeypatch) as (_, factory):
        asyncio.run(scenario(factory))


def test_starting_twice_is_refused(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    async def scenario(factory: sessionmaker[Session]) -> None:
        controller = _controller(factory, Loops(), "node-a")
        await controller.start()
        try:
            with pytest.raises(RuntimeError, match="already running"):
                await controller.start()
        finally:
            await controller.stop()

    with _database("sqlite", tmp_path, monkeypatch) as (_, factory):
        asyncio.run(scenario(factory))


def test_without_a_lease_the_process_is_the_owner_when_it_runs_loops_and_secondary_when_it_does_not(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def scenario(factory: sessionmaker[Session]) -> None:
        loops = Loops()
        development = _controller(factory, loops, "dev", leased=False)
        await development.start()
        assert development.role == OWNER and loops.events == ["start"] and _stored(factory) is None
        await development.stop()
        assert loops.events == ["start", "stop"] and _stored(factory) is None

        idle = Loops()
        http_only = _controller(factory, idle, "http", leased=True, run_loops=False)
        await http_only.start()
        assert http_only.role == SECONDARY and idle.events == [] and _stored(factory) is None
        await http_only.stop()

    with _database("sqlite", tmp_path, monkeypatch) as (_, factory):
        asyncio.run(scenario(factory))


def test_the_background_task_renews_and_takes_over_on_its_own(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Real timing, short lease: the owner keeps its token across several ticks; stopping it hands over."""

    async def scenario(factory: sessionmaker[Session]) -> None:
        a_loops, b_loops = Loops(), Loops()
        a = _controller(factory, a_loops, "node-a", lease=1.5)
        b = _controller(factory, b_loops, "node-b", lease=1.5)
        await a.start()
        await b.start()
        try:
            await asyncio.sleep(2.0)  # longer than the lease: only renewal keeps it
            assert (a.role, b.role) == (OWNER, SECONDARY)
            lease = _stored(factory)
            assert lease is not None and lease.owner_id == "node-a" and lease.fencing_token == 1
            await a.stop()
            for _ in range(40):
                if b.role == OWNER:
                    break
                await asyncio.sleep(0.1)
            assert b.role == OWNER
        finally:
            await b.stop()
            await a.stop()

    with _database("sqlite", tmp_path, monkeypatch) as (_, factory):
        asyncio.run(scenario(factory))


def test_a_failing_tick_does_not_end_the_protocol(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def scenario(factory: sessionmaker[Session]) -> None:
        controller = _controller(factory, Loops(), "node-a", lease=1.2)
        await controller.start()
        calls: list[int] = []
        original = controller.tick

        async def flaky() -> str:
            calls.append(1)
            if len(calls) == 1:
                raise RuntimeError("transient")
            return await original()

        monkeypatch.setattr(controller, "tick", flaky)
        with _logged() as records:
            await asyncio.sleep(1.3)
        await controller.stop()
        assert len(calls) >= 2, "the loop kept ticking after a failure"
        assert any("tick failed" in message for message in records.messages)

    with _database("sqlite", tmp_path, monkeypatch) as (_, factory):
        asyncio.run(scenario(factory))


def test_owner_ids_are_unique_per_incarnation_and_the_ticks_are_a_third_of_the_lease() -> None:
    assert new_owner_id() != new_owner_id()
    host, pid, suffix = new_owner_id().rsplit(":", 2)
    assert host and pid.isdigit() and len(suffix) == 12
    assert {"owner", "secondary"} == ROLES
    controller = ExecutionRoleController(
        None, lease_seconds=30.0, start_loops=Loops().start, stop_loops=Loops().stop
    )
    assert (
        controller.tick_seconds == 10.0
        and controller.role == SECONDARY
        and controller.lease is None
    )
