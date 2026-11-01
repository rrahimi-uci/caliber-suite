"""The execution role: acquire, renew and lose the lease, and start and stop the owner-only loops with it (B13.3, #673).

The controller is driven with fake loops against a real migrated database on SQLite and, where
``CALIBER_TEST_POSTGRES_URL`` is set, PostgreSQL. Ticks are called directly (``tick()``) so the tests do not depend on
timing; one test runs the real background task with a short lease.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
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
from tests.dialect_helpers import DIALECTS, POSTGRES_TEST_URL, dialect_database_url

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


# ---------------------------------------------------------------- failure modes found in review


def test_loops_that_fail_to_start_are_stopped_the_lease_is_given_back_and_the_next_tick_recovers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def scenario(factory: sessionmaker[Session]) -> None:
        events: list[str] = []
        failing = {"on": True}

        async def start() -> None:
            events.append("start")
            if failing["on"]:
                raise RuntimeError("already running")

        async def stop() -> None:
            events.append("stop")

        controller = ExecutionRoleController(
            factory, lease_seconds=30, start_loops=start, stop_loops=stop, owner_id="node-a"
        )
        rival_loops = Loops()
        rival = _controller(factory, rival_loops, "node-b")
        with _logged() as records:
            await controller.start()
        assert controller.role == SECONDARY and controller.lease is None
        assert events == ["start", "stop"], "the loops that did start are stopped"
        assert any("failed to start" in message for message in records.messages)
        # Not stuck holding the lease: another process can take it at once.
        assert await rival.tick() == OWNER
        await rival.stop()
        failing["on"] = False
        assert await controller.tick() == OWNER and controller._loops_running
        lease = _stored(factory)
        assert lease is not None and (lease.owner_id, lease.fencing_token) == ("node-a", 3)
        await controller.stop()

    with _database("sqlite", tmp_path, monkeypatch) as (_, factory):
        asyncio.run(scenario(factory))


def test_shutting_down_during_a_lease_loss_stop_waits_for_that_stop_and_never_cancels_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def scenario(engine: Engine, factory: sessionmaker[Session]) -> None:
        release_stop = asyncio.Event()
        stopping = asyncio.Event()
        finished: list[str] = []

        async def start() -> None:
            return None

        async def stop() -> None:
            stopping.set()
            await release_stop.wait()
            finished.append("stopped")

        controller = ExecutionRoleController(
            factory, lease_seconds=30, start_loops=start, stop_loops=stop, owner_id="node-a"
        )
        await controller.start()
        _expire(engine)
        _controller(
            factory, Loops(), "node-b"
        )  # a rival exists only to make the lease change hands
        rival = _controller(factory, Loops(), "node-c")
        assert await rival.tick() == OWNER
        tick = asyncio.create_task(controller.tick())
        await stopping.wait()
        assert controller.role == SECONDARY
        shutdown = asyncio.create_task(controller.stop())
        await asyncio.sleep(0.1)
        assert not shutdown.done(), "shutdown waits for the stop already in progress"
        tick.cancel()  # even a cancelled caller does not cut the loops' stop short
        await asyncio.sleep(0.05)
        assert finished == []
        release_stop.set()
        await shutdown
        assert finished == ["stopped"]
        await rival.stop()
        with contextlib.suppress(asyncio.CancelledError):
            await tick

    with _database("sqlite", tmp_path, monkeypatch) as (engine, factory):
        asyncio.run(scenario(engine, factory))


def test_a_renewal_that_hangs_costs_the_owner_its_loops_at_the_local_deadline(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def scenario(factory: sessionmaker[Session]) -> None:
        loops = Loops()
        controller = _controller(factory, loops, "node-a", lease=3.0)
        await controller.start()
        gate = threading.Event()
        monkeypatch.setattr(controller, "_renew", lambda lease: gate.wait(10) and lease)
        started = time.monotonic()
        with _logged() as records:
            assert await controller.tick() == SECONDARY
        took = time.monotonic() - started
        gate.set()
        assert took < 2.5, (
            f"the renewal was abandoned at the local deadline, not waited for ({took:.1f}s)"
        )
        assert loops.events == ["start", "stop"] and controller.lease is None
        assert any("no answer from the database" in message for message in records.messages)
        await controller.stop()

    with _database("sqlite", tmp_path, monkeypatch) as (_, factory):
        asyncio.run(scenario(factory))


def test_a_secondary_whose_acquire_hangs_stays_secondary_and_keeps_ticking(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def scenario(factory: sessionmaker[Session]) -> None:
        controller = _controller(factory, Loops(), "node-a", lease=3.0)
        gate = threading.Event()
        monkeypatch.setattr(controller, "_acquire", lambda: gate.wait(10) and None)
        started = time.monotonic()
        assert await controller.tick() == SECONDARY
        assert time.monotonic() - started < 2.0
        gate.set()
        await controller.stop()

    with _database("sqlite", tmp_path, monkeypatch) as (_, factory):
        asyncio.run(scenario(factory))


def test_the_lease_keeps_being_renewed_while_the_loops_drain_at_shutdown(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def scenario(factory: sessionmaker[Session]) -> None:
        renewals: list[int] = []
        draining = asyncio.Event()

        async def start() -> None:
            return None

        async def stop() -> None:
            draining.set()
            await asyncio.sleep(
                1.6
            )  # a drain longer than one tick (the lease is 1.5 s, a tick 0.5 s)

        controller = ExecutionRoleController(
            factory, lease_seconds=1.5, start_loops=start, stop_loops=stop, owner_id="node-a"
        )
        original = controller._renew

        def counting(lease: Lease) -> Lease:
            renewals.append(1)
            return original(lease)

        monkeypatch.setattr(controller, "_renew", counting)
        rival = _controller(factory, Loops(), "node-b", lease=1.5)
        await controller.start()
        await asyncio.sleep(0.1)
        shutdown = asyncio.create_task(controller.stop())
        await draining.wait()
        before = len(renewals)
        assert await rival.tick() == SECONDARY
        await shutdown
        assert len(renewals) > before, "still renewing while draining"
        assert await rival.tick() == OWNER, "released only after the drain, so no overlap"
        await rival.stop()

    with _database("sqlite", tmp_path, monkeypatch) as (_, factory):
        asyncio.run(scenario(factory))


def test_a_tick_during_shutdown_never_takes_a_lease(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def scenario(factory: sessionmaker[Session]) -> None:
        loops = Loops()
        controller = _controller(factory, loops, "node-a")
        controller._stopping = True
        assert (
            await controller.tick() == SECONDARY and loops.events == [] and controller.lease is None
        )

    with _database("sqlite", tmp_path, monkeypatch) as (_, factory):
        asyncio.run(scenario(factory))


def test_a_stop_that_raises_is_logged_and_the_role_stays_secondary(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def scenario(factory: sessionmaker[Session]) -> None:
        async def start() -> None:
            return None

        async def stop() -> None:
            raise RuntimeError("a loop would not stop")

        controller = ExecutionRoleController(
            factory, lease_seconds=30, start_loops=start, stop_loops=stop, owner_id="node-a"
        )
        await controller.start()
        with _logged() as records:
            await controller.stop()
        assert controller.role == SECONDARY and controller.lease is None
        assert any(
            "stopping the owner-only loops failed" in message for message in records.messages
        )

    with _database("sqlite", tmp_path, monkeypatch) as (_, factory):
        asyncio.run(scenario(factory))


@pytest.mark.skipif(not POSTGRES_TEST_URL, reason="lock_timeout is a PostgreSQL setting")
def test_postgresql_a_renewal_blocked_on_the_lease_row_gives_up_with_a_lock_timeout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def scenario(engine: Engine, factory: sessionmaker[Session]) -> None:
        loops = Loops()
        controller = _controller(factory, loops, "node-a", lease=3.0)
        await controller.start()
        with engine.connect() as holder:
            holder.execute(text("SELECT 1 FROM caliber_execution_owner FOR UPDATE"))
            with _logged() as records:
                assert await controller.tick() == SECONDARY
            await asyncio.sleep(0.2)
            holder.rollback()
        assert loops.events == ["start", "stop"]
        # The database gave up first (lock_timeout is a third of the lease, the local deadline two thirds), so
        # the failure is the store's, not the controller's abandoned wait.
        assert any("lease store is unavailable" in m for m in records.messages), records.messages
        await controller.stop()

    with _database("postgresql", tmp_path, monkeypatch) as (engine, factory):
        asyncio.run(scenario(engine, factory))


class _FakeLoop:
    """A background task whose stop takes ``delay`` seconds and records when it began and ended."""

    def __init__(self, name: str, log: list[tuple[str, str, float]], delay: float = 0.0) -> None:
        self.name, self.log, self.delay = name, log, delay

    def bind_lease_provider(self, provider: Any) -> None:
        """What a loop that dispatches under the lease takes (the workflow-run worker, #1051); this one ignores it."""

    async def start(self) -> None:
        self.log.append((self.name, "start", time.monotonic()))

    async def stop(self, **_: Any) -> None:
        self.log.append((self.name, "stop-begin", time.monotonic()))
        await asyncio.sleep(self.delay)
        if self.name == "janitor-fails":
            raise RuntimeError("did not stop")
        self.log.append((self.name, "stop-end", time.monotonic()))


class _FakeEvaluator(_FakeLoop):
    """The release evaluator's lifecycle surface: a loop that also takes the lease provider."""

    UNBOUND = object()

    def __init__(self, name: str, log: list[tuple[str, str, float]]) -> None:
        super().__init__(name, log)
        self.provider: Any = self.UNBOUND

    def bind_lease_provider(self, provider: Any) -> None:
        self.provider = provider


def _lifespan(
    log: list[tuple[str, str, float]],
    *,
    janitor: str = "janitor",
    outbox: bool = False,
    evaluator: _FakeEvaluator | None = None,
    reconciler: _FakeEvaluator | None = None,
    workflow_worker: _FakeEvaluator | None = None,
    session_factory: Any = None,
    reference_profile: bool = False,
) -> Any:
    from caliber.server import _build_lifespan

    names = ["worker", "workflow", "aria", "knowledge", "imports", "scheduler"]
    loops = [_FakeLoop(n, log, 0.3) for n in names]
    worker, workflow, aria, knowledge, imports, scheduler = loops
    if workflow_worker is not None:
        workflow = workflow_worker
    bus = _FakeLoop("bus", log)
    engine = SimpleNamespace(dispose=lambda: log.append(("engine", "dispose", time.monotonic())))
    return _build_lifespan(
        engine,  # type: ignore[arg-type]
        worker,  # type: ignore[arg-type]
        workflow,  # type: ignore[arg-type]
        aria,  # type: ignore[arg-type]
        knowledge,  # type: ignore[arg-type]
        imports,  # type: ignore[arg-type]
        scheduler,  # type: ignore[arg-type]
        _FakeLoop(janitor, log, 0.3),  # type: ignore[arg-type]
        _FakeLoop("reconciler", log, 0.3),  # type: ignore[arg-type]
        _FakeLoop("drain", log, 0.3),  # type: ignore[arg-type]
        _FakeLoop("webhooks", log, 0.1),  # type: ignore[arg-type]
        5.0,
        None,  # type: ignore[arg-type]
        None,  # type: ignore[arg-type]
        None,  # type: ignore[arg-type]
        None,  # type: ignore[arg-type]
        bus,  # type: ignore[arg-type]
        background_tasks_enabled=True,
        session_factory=session_factory,
        reference_profile=reference_profile,
        outbox_publisher=_FakeLoop("outbox", log, 0.3) if outbox else None,  # type: ignore[arg-type]
        workspace_release_evaluator=evaluator,  # type: ignore[arg-type]
        effect_reconciler=reconciler,  # type: ignore[arg-type]
    )


def test_the_outbox_publisher_runs_and_stops_with_the_owner_loops() -> None:
    log: list[tuple[str, str, float]] = []

    async def scenario() -> None:
        app = SimpleNamespace(state=SimpleNamespace())
        async with _lifespan(log, outbox=True)(app):
            assert ("outbox", "start") in [(n, what) for n, what, _ in log]
        assert ("outbox", "stop-end") in [(n, what) for n, what, _ in log]

    asyncio.run(scenario())


def test_the_release_evaluator_runs_and_stops_with_the_owner_loops_and_is_unfenced_without_a_lease() -> (
    None
):
    log: list[tuple[str, str, float]] = []
    evaluator = _FakeEvaluator("evaluator", log)

    async def scenario() -> None:
        app = SimpleNamespace(state=SimpleNamespace())
        async with _lifespan(log, evaluator=evaluator)(app):
            assert ("evaluator", "start") in [(n, what) for n, what, _ in log]
            assert app.state.execution_role.role == OWNER
        assert ("evaluator", "stop-end") in [(n, what) for n, what, _ in log]

    asyncio.run(scenario())
    # outside the reference profile there is no lease: nothing is bound and the evaluator is the owner by construction
    assert evaluator.provider is _FakeEvaluator.UNBOUND


def test_a_loop_set_without_the_release_evaluator_still_starts() -> None:
    log: list[tuple[str, str, float]] = []

    async def scenario() -> None:
        async with _lifespan(log)(SimpleNamespace(state=SimpleNamespace())):
            pass

    asyncio.run(scenario())
    assert "evaluator" not in {n for n, _, _ in log}


@pytest.mark.parametrize("dialect", DIALECTS)
def test_the_release_evaluator_is_fenced_by_the_lease_the_role_holds(
    dialect: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    log: list[tuple[str, str, float]] = []
    evaluator = _FakeEvaluator("evaluator", log)
    with _database(dialect, tmp_path, monkeypatch) as (_, factory):

        async def scenario() -> None:
            app = SimpleNamespace(state=SimpleNamespace())
            lifespan = _lifespan(
                log, evaluator=evaluator, session_factory=factory, reference_profile=True
            )
            async with lifespan(app):
                role = app.state.execution_role
                held = evaluator.provider()
                assert isinstance(held, Lease)
                assert held.owner_id == role.owner_id and held.fencing_token == 1
                assert held == role.lease, "the exact lease the owner captured"
            assert evaluator.provider() is None, "no lease once the loops are stopped: no claim"

        asyncio.run(scenario())


def test_the_effect_reconciler_runs_and_stops_with_the_owner_loops_and_is_never_bound_without_a_lease() -> (
    None
):
    log: list[tuple[str, str, float]] = []
    reconciler = _FakeEvaluator("effect-reconciler", log)

    async def scenario() -> None:
        app = SimpleNamespace(state=SimpleNamespace())
        async with _lifespan(log, reconciler=reconciler)(app):
            assert ("effect-reconciler", "start") in [(n, what) for n, what, _ in log]
        assert ("effect-reconciler", "stop-end") in [(n, what) for n, what, _ in log]

    asyncio.run(scenario())
    # outside the reference profile there is no lease: nothing is bound, and the loop itself does nothing without one
    assert reconciler.provider is _FakeEvaluator.UNBOUND


@pytest.mark.parametrize("dialect", DIALECTS)
def test_the_effect_reconciler_is_bound_to_the_lease_the_role_holds_and_loses_it_with_the_role(
    dialect: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    log: list[tuple[str, str, float]] = []
    reconciler = _FakeEvaluator("effect-reconciler", log)
    with _database(dialect, tmp_path, monkeypatch) as (_, factory):

        async def scenario() -> None:
            app = SimpleNamespace(state=SimpleNamespace())
            lifespan = _lifespan(
                log, reconciler=reconciler, session_factory=factory, reference_profile=True
            )
            async with lifespan(app):
                role = app.state.execution_role
                held = reconciler.provider()
                assert isinstance(held, Lease)
                assert held == role.lease, "the exact lease the owner captured"
            assert reconciler.provider() is None, "no lease once the loops are stopped: no pass"

        asyncio.run(scenario())


def test_the_workflow_run_worker_is_never_bound_to_a_lease_outside_the_reference_profile() -> None:
    log: list[tuple[str, str, float]] = []
    run_worker = _FakeEvaluator("workflow", log)

    async def scenario() -> None:
        app = SimpleNamespace(state=SimpleNamespace())
        async with _lifespan(log, workflow_worker=run_worker)(app):
            assert ("workflow", "start") in [(n, what) for n, what, _ in log]

    asyncio.run(scenario())
    assert run_worker.provider is _FakeEvaluator.UNBOUND


@pytest.mark.parametrize("dialect", DIALECTS)
def test_the_workflow_run_worker_dispatches_only_under_the_lease_the_role_holds(
    dialect: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#1051: a run's protected effects are sent under the exact lease the owner captured, and none once the role lost it."""
    log: list[tuple[str, str, float]] = []
    run_worker = _FakeEvaluator("workflow", log)
    with _database(dialect, tmp_path, monkeypatch) as (_, factory):

        async def scenario() -> None:
            app = SimpleNamespace(state=SimpleNamespace())
            lifespan = _lifespan(
                log, workflow_worker=run_worker, session_factory=factory, reference_profile=True
            )
            async with lifespan(app):
                role = app.state.execution_role
                held = run_worker.provider()
                assert isinstance(held, Lease)
                assert held == role.lease, "the exact lease the owner captured"
            assert run_worker.provider() is None, (
                "no lease once the loops are stopped: nothing is dispatched"
            )

        asyncio.run(scenario())


def test_the_owner_loops_stop_together_after_the_webhooks_and_before_the_bus_and_engine() -> None:
    log: list[tuple[str, str, float]] = []

    async def scenario() -> None:
        app = SimpleNamespace(state=SimpleNamespace())
        async with _lifespan(log)(app):
            assert app.state.execution_role.role == OWNER
        assert app.state.execution_role.role == SECONDARY, "leaving the lifespan stopped the loops"

    asyncio.run(scenario())
    begins = {n: t for n, what, t in log if what == "stop-begin"}
    ends = {n: t for n, what, t in log if what == "stop-end"}
    others = [n for n in begins if n not in {"webhooks", "bus"}]
    assert len(others) == 9
    assert ends["webhooks"] <= min(begins[n] for n in others), "webhooks stop first"
    assert max(begins[n] for n in others) - min(begins[n] for n in others) < 0.15, (
        "all signalled together"
    )
    assert max(ends[n] for n in others) - min(begins[n] for n in others) < 0.8, (
        "so the drains overlap"
    )
    assert begins["bus"] >= max(ends[n] for n in others)
    assert log[-1][:2] == ("engine", "dispose")


def test_a_loop_that_fails_to_stop_does_not_keep_the_others_from_stopping() -> None:
    log: list[tuple[str, str, float]] = []

    async def scenario() -> None:
        async with _lifespan(log, janitor="janitor-fails")(
            SimpleNamespace(state=SimpleNamespace())
        ):
            pass

    with _logged() as records:
        asyncio.run(scenario())
    ended = {n for n, what, _ in log if what == "stop-end"}
    assert {
        "worker",
        "workflow",
        "aria",
        "knowledge",
        "imports",
        "scheduler",
        "reconciler",
        "drain",
    } <= ended
    assert any("stopping the owner-only loops failed" in message for message in records.messages)
    assert log[-1][:2] == ("engine", "dispose"), "the engine is still disposed"


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
