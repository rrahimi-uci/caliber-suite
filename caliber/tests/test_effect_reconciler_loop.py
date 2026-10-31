"""B05.5d (#997): the owner-only loop that calls ``reconcile_pass`` under the execution-owner lease.

What is pinned here, and what each pin is for:

* the loop acts only under the exact lease its provider returns *now*, never as a secondary and never without one;
* a lease that is gone, replaced or taken over, or a stop in progress, ends the pass *between proposals*: no further proposal
  is read, asked about or written, so nothing new starts after the lease is lost;
* a budget ends a pass early and the next pass continues after the last proposal looked at, so a backlog of effects that stay
  ``unknown`` cannot starve the rest;
* every transaction of the pass is bounded on PostgreSQL; the loop keeps its own heartbeat and removes it on a clean stop;
* a stop that outlasts its grace does not orphan a pass: the pass ends at its next proposal.
"""

from __future__ import annotations

import asyncio
import logging
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from sqlalchemy import select, text
from sqlalchemy.orm import Session, sessionmaker

import caliber.orchestrator.effect_reconciler as loop_module
from caliber.db.models import CaliberStepTokenReservation, CaliberWorkerHeartbeat
from caliber.effects import reconcile
from caliber.effects.reconcile import (
    COMMITTED,
    CONNECTORS,
    RECONCILED_COMMITTED,
    STILL_UNKNOWN,
    Answer,
    PassResult,
)
from caliber.effects.records import (
    DISPATCHED,
    IN_FLIGHT_EXPIRED_REASON,
    LEASE_LOST,
    STORE_FAILED,
    UNKNOWN,
    ProposalError,
    proposal_state,
    proposal_view,
)
from caliber.observability.worker_registry import KIND_EFFECT_RECONCILER, stale_after_seconds
from caliber.orchestrator.effect_reconciler import (
    BUDGET,
    COMPLETE,
    LEASE_CHANGED,
    LEASE_REFUSED,
    REFUSED,
    STOPPED,
    EffectReconcilerTask,
)
from caliber.ownership import Lease, acquire, release
from tests import test_effect_records as records_tests
from tests.dialect_helpers import DIALECTS, POSTGRES_TEST_URL
from tests.effect_helpers import TTL_SECONDS, leased
from tests.test_effect_in_flight import _facts_for, _Scripted, _unknown_one
from tests.test_effect_proposals import _seed_run
from tests.test_effect_receipts import _proposal
from tests.test_effect_records import OWNER
from tests.test_event_outbox import _database

BASE_URL = "http://downstream.invalid"
BINDINGS = (
    '[{"connector": "document-request", "project_id": "PRJ-ep", "secret_source": "env:X", '
    f'"base_url": "{BASE_URL}"}}]'
)


@pytest.fixture(params=DIALECTS)
def database(
    request: pytest.FixtureRequest, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Any:
    with _database(request.param, tmp_path, monkeypatch) as (engine, factory):
        _seed_run(factory)
        yield engine, factory


@pytest.fixture
def _fact_lookup(monkeypatch: pytest.MonkeyPatch) -> None:
    """The document-request connector, but one that confirms any 200 as this proposal's effect (the loop is under test)."""
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


class _Records(logging.Handler):
    def __init__(self) -> None:
        super().__init__(level=logging.DEBUG)
        self.messages: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.messages.append(record.getMessage())


@contextmanager
def _logged(name: str = "caliber.orchestrator.effect_reconciler") -> Iterator[_Records]:
    """Capture a log (Alembic's logging setup detaches pytest's root handler)."""
    handler = _Records()
    logger = logging.getLogger(name)
    previous = logger.level
    logger.addHandler(handler)
    logger.setLevel(logging.DEBUG)
    try:
        yield handler
    finally:
        logger.removeHandler(handler)
        logger.setLevel(previous)


def _config(**overrides: Any) -> SimpleNamespace:
    values: dict[str, Any] = {
        "effect_reconciler_interval_seconds": 15.0,
        "effect_reconciler_budget_seconds": 20.0,
        "effect_in_flight_max_seconds": 900.0,
        "execution_lease_seconds": 30.0,
        "effect_callback_connectors": BINDINGS,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def _task(
    factory: sessionmaker[Session],
    provider: Any = None,
    lookup: Any = None,
    **config: Any,
) -> EffectReconcilerTask:
    return EffectReconcilerTask(
        factory,
        _config(**config),
        lease_provider=provider,
        lookup=lookup or _Scripted(),
    )


def _lease(factory: sessionmaker[Session], owner: str = OWNER) -> Lease:
    with factory() as session:
        lease = leased(session, owner)
        session.commit()
    return lease


def _state(factory: sessionmaker[Session], proposal_id: str) -> Any:
    with factory() as session:
        return proposal_state(session, proposal_id)


def _heartbeats(factory: sessionmaker[Session]) -> list[CaliberWorkerHeartbeat]:
    with factory() as session:
        return list(session.execute(select(CaliberWorkerHeartbeat)).scalars())


def _unknowns(factory: sessionmaker[Session], count: int) -> list[str]:
    with factory() as session:
        ids = sorted(_unknown_one(session, f"occ-{n}") for n in range(count))
        session.commit()
    return ids


# ---------------------------------------------------------------- construction


def test_the_defaults_are_the_documented_cadence_and_bounds() -> None:
    task = EffectReconcilerTask(None, SimpleNamespace())
    assert task._interval_seconds == loop_module.DEFAULT_INTERVAL_SECONDS == 15.0
    assert task._budget_seconds == loop_module.DEFAULT_BUDGET_SECONDS == 20.0
    assert task.in_flight_seconds == loop_module.DEFAULT_IN_FLIGHT_MAX_SECONDS == 900.0
    assert task._targets == {}


@pytest.mark.parametrize(
    ("name", "bad"),
    [
        ("effect_reconciler_interval_seconds", 0.5),
        ("effect_reconciler_interval_seconds", 3601.0),
        ("effect_reconciler_interval_seconds", float("nan")),
        ("effect_reconciler_budget_seconds", 0.5),
        ("effect_reconciler_budget_seconds", 601.0),
        ("effect_reconciler_budget_seconds", float("inf")),
        ("effect_in_flight_max_seconds", 299.0),
        ("effect_in_flight_max_seconds", 86401.0),
        ("effect_in_flight_max_seconds", True),
        ("effect_in_flight_max_seconds", "900"),
        ("execution_lease_seconds", 0.5),
    ],
)
def test_a_setting_outside_its_bounds_is_refused_by_name(name: str, bad: Any) -> None:
    with pytest.raises(ValueError, match=name):
        EffectReconcilerTask(None, _config(**{name: bad}))


def test_targets_are_the_callback_connector_bindings_and_a_bad_binding_is_refused() -> None:
    task = EffectReconcilerTask(None, _config())
    assert task._targets == {("document-request", "PRJ-ep"): BASE_URL}
    with pytest.raises(ValueError, match="JSON array"):
        EffectReconcilerTask(None, _config(effect_callback_connectors="not json"))


# ---------------------------------------------------------------- never a secondary


def test_without_a_lease_provider_the_loop_does_nothing_at_all() -> None:
    def no_database() -> Any:
        raise AssertionError("a loop with no lease must not touch the database")

    task = EffectReconcilerTask(no_database, _config())
    assert task.tick() is None


def test_a_secondary_records_its_own_liveness_and_reconciles_nothing(database: Any) -> None:
    _, factory = database
    ids = _unknowns(factory, 2)
    ask = _Scripted()
    task = _task(factory, provider=lambda: None, lookup=ask)
    assert task.tick() is None
    assert ask.asked == [], "no lease, no question to the downstream"
    assert {_state(factory, i).state for i in ids} == {UNKNOWN}
    (beat,) = _heartbeats(factory)
    assert (beat.worker_id, beat.kind) == (task.worker_id, KIND_EFFECT_RECONCILER)


# ---------------------------------------------------------------- one pass under the lease


def test_the_owner_settles_what_the_downstream_proves_and_expires_a_stuck_dispatch(
    database: Any, _fact_lookup: None
) -> None:
    _, factory = database
    with factory() as session:
        found = _unknown_one(session, "occ-found")
        silent = _unknown_one(session, "occ-silent")
        stuck = _proposal(session, occurrence="occ-stuck")
        keys = {i: proposal_view(session, i).logical_key for i in (found, silent, stuck)}
        session.commit()
    lease = _lease(factory)
    ask = _Scripted()
    ask.answers[keys[found]] = Answer(200, {"found": True})
    task = _task(factory, provider=lambda: lease, lookup=ask)

    report = task.tick()

    assert report is not None
    assert (report.ended, report.fencing_token, report.code) == (
        COMPLETE,
        lease.fencing_token,
        None,
    )
    outcomes = {r.proposal_id: (r.outcome, r.state) for r in report.results}
    assert outcomes == {
        found: (RECONCILED_COMMITTED, COMMITTED),
        silent: (STILL_UNKNOWN, UNKNOWN),
        stuck: (STILL_UNKNOWN, UNKNOWN),
    }
    assert sorted(ask.asked) == sorted(keys.values()), "every one was asked about by its own key"
    assert _state(factory, stuck).reason == IN_FLIGHT_EXPIRED_REASON, (
        "dispatched under this very lease and in flight since 2026-03: the bound moved it"
    )
    assert _state(factory, silent).state == UNKNOWN, "a 404 is not proof of anything"


def test_a_young_same_token_dispatch_is_the_dispatchers_and_left_alone(
    database: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, factory = database
    monkeypatch.setattr(  # dispatched just now, not in 2026-03
        records_tests, "AT", datetime.now(timezone.utc)
    )
    with factory() as session:
        proposal_id = _proposal(session)
        session.commit()
    lease = _lease(factory)
    ask = _Scripted()
    report = _task(factory, provider=lambda: lease, lookup=ask).tick()
    assert report is not None and report.ended == COMPLETE
    assert [(r.outcome, r.state) for r in report.results] == [("in_flight", DISPATCHED)]
    assert ask.asked == []
    assert _state(factory, proposal_id).state == DISPATCHED


def test_a_pass_with_nothing_to_do_reports_complete_and_asks_nothing(database: Any) -> None:
    _, factory = database
    ask = _Scripted()
    lease = _lease(factory)
    report = _task(factory, provider=lambda: lease, lookup=ask).tick()
    assert report is not None and (report.ended, report.results) == (COMPLETE, ())
    assert ask.asked == []


# ---------------------------------------------------------------- lease loss and stop end the pass between proposals


def test_a_lease_that_goes_away_mid_pass_stops_before_the_next_proposal(database: Any) -> None:
    _, factory = database
    ids = _unknowns(factory, 3)
    lease = _lease(factory)
    held: list[Lease | None] = [lease]
    ask = _Scripted()

    def provider() -> Lease | None:
        return held[0]

    def lookup(connector: Any, base_url: str, key: str) -> Answer:
        held[0] = None  # the controller lost the lease while the first lookup was in progress
        return ask(connector, base_url, key)

    task = _task(factory, provider=provider, lookup=lookup)
    report = task.tick()
    assert report is not None and report.ended == LEASE_CHANGED and report.results == ()
    assert len(ask.asked) == 1, "the proposal in progress finished; nothing after it was looked at"
    assert {_state(factory, i).state for i in ids} == {UNKNOWN}
    # and the next tick, still without a lease, does not start at all
    assert task.tick() is None and len(ask.asked) == 1


def test_a_lease_replaced_by_a_higher_token_ends_the_pass_and_the_next_one_starts_fresh(
    database: Any,
) -> None:
    _, factory = database
    _unknowns(factory, 2)
    lease = _lease(factory)
    held = [lease]
    ask = _Scripted()

    def lookup(connector: Any, base_url: str, key: str) -> Answer:
        held[0] = replace_token(lease)
        return ask(connector, base_url, key)

    task = _task(factory, provider=lambda: held[0], lookup=lookup)
    report = task.tick()
    assert report is not None and report.ended == LEASE_CHANGED
    assert len(ask.asked) == 1
    assert task._cursor is None


def replace_token(lease: Lease) -> Lease:
    return Lease(lease.name, lease.owner_id, lease.fencing_token + 1, lease.expires_at)


def test_a_lease_taken_over_in_the_database_makes_the_pass_record_and_ask_nothing(
    database: Any,
) -> None:
    """The provider still returns the stale lease (the controller has not noticed yet): the database says no first."""
    _, factory = database
    ids = _unknowns(factory, 2)
    stale = _lease(factory)
    with factory() as session:
        assert release(session, stale)
        assert acquire(session, "someone-else", ttl_seconds=TTL_SECONDS) is not None
        session.commit()
    ask = _Scripted()
    task = _task(factory, provider=lambda: stale, lookup=ask)
    report = task.tick()
    assert report is not None
    assert (report.ended, report.code, report.results) == (LEASE_REFUSED, LEASE_LOST, ())
    assert ask.asked == []
    assert {_state(factory, i).state for i in ids} == {UNKNOWN}


def test_a_refused_pass_is_reported_and_retried_next_tick(
    database: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, factory = database
    lease = _lease(factory)
    task = _task(factory, provider=lambda: lease)
    task._cursor = (lease.fencing_token, "prop-x")

    def refuse(*_a: Any, **_k: Any) -> Any:
        raise ProposalError(STORE_FAILED, "the lease could not be checked")

    monkeypatch.setattr(loop_module, "reconcile_pass_from", refuse)
    report = task.tick()
    assert report is not None and (report.ended, report.code) == (REFUSED, STORE_FAILED)
    assert task._cursor is None


def test_a_stop_requested_mid_pass_ends_it_before_the_next_proposal(database: Any) -> None:
    _, factory = database
    ids = _unknowns(factory, 3)
    lease = _lease(factory)
    ask = _Scripted()
    task = _task(factory, provider=lambda: lease)

    def lookup(connector: Any, base_url: str, key: str) -> Answer:
        task._stop_requested.set()
        return ask(connector, base_url, key)

    task._lookup = lookup
    report = task.tick()
    assert report is not None and report.ended == STOPPED
    assert len(ask.asked) == 1
    assert {_state(factory, i).state for i in ids} == {UNKNOWN}


# ---------------------------------------------------------------- the budget and the cursor


def test_a_budget_cut_pass_continues_after_the_last_proposal_it_looked_at(
    database: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, factory = database
    lease = _lease(factory)
    task = _task(factory, provider=lambda: lease)
    seen: list[Any] = []
    script = [
        PassResult([], "prop-b"),
        PassResult([], "prop-c"),
        PassResult([]),
        PassResult([]),
    ]

    def fake(*_a: Any, **kwargs: Any) -> PassResult:
        seen.append(kwargs["after"])
        assert kwargs["budget_seconds"] == 20.0 and kwargs["in_flight_seconds"] == 900.0
        assert callable(kwargs["on_progress"])
        return script[len(seen) - 1]

    monkeypatch.setattr(loop_module, "reconcile_pass_from", fake)
    ended = [task.tick().ended for _ in range(4)]
    assert ended == [BUDGET, BUDGET, COMPLETE, COMPLETE]
    assert seen == [None, "prop-b", "prop-c", None], "a finished pass starts over from the top"


def test_a_new_lease_never_inherits_the_cursor_of_an_old_one(
    database: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, factory = database
    lease = _lease(factory)
    held = [lease]
    task = _task(factory, provider=lambda: held[0])
    seen: list[Any] = []

    def fake(*_a: Any, **kwargs: Any) -> PassResult:
        seen.append(kwargs["after"])
        return PassResult([], "prop-b")

    monkeypatch.setattr(loop_module, "reconcile_pass_from", fake)
    task.tick()
    held[0] = replace_token(lease)
    task.tick()
    assert seen == [None, None]


def test_a_real_budget_spreads_a_backlog_over_passes_without_starving_the_tail(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Through the real ``reconcile_pass_from`` (only its clock is scripted): every lookup costs 0.6 s of a 1 s budget."""
    with _database("sqlite", tmp_path, monkeypatch) as (_, factory):
        _seed_run(factory)
        ids = _unknowns(factory, 3)
        lease = _lease(factory)
        clock = [0.0]
        monkeypatch.setattr(reconcile, "time", SimpleNamespace(monotonic=lambda: clock[0]))
        asked: list[str] = []

        def slow(connector: Any, base_url: str, key: str) -> Answer:
            asked.append(key)
            clock[0] += 0.6
            return Answer(404, {})

        task = _task(
            factory,
            provider=lambda: lease,
            lookup=slow,
            effect_reconciler_budget_seconds=1.0,
        )
        first = task.tick()
        assert first is not None and first.ended == BUDGET
        assert [r.proposal_id for r in first.results] == ids[:2]
        second = task.tick()
        assert second is not None and second.ended == COMPLETE
        assert [r.proposal_id for r in second.results] == [ids[2]], "the tail was reached"
        assert len(asked) == 3


# ---------------------------------------------------------------- bounded transactions (PostgreSQL)


def test_every_transaction_of_the_pass_is_bounded_on_postgresql(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    if not POSTGRES_TEST_URL:
        pytest.skip("the bounds are a PostgreSQL setting")
    with _database("postgresql", tmp_path, monkeypatch) as (_, factory):
        task = _task(factory, execution_lease_seconds=30.0)
        with task._open() as session:
            assert session.execute(text("SHOW lock_timeout")).scalar_one() == "10s"
            assert session.execute(text("SHOW statement_timeout")).scalar_one() == "10s"
            session.commit()
            # SET LOCAL ends with its transaction: the next one is bounded again, not unbounded
            assert session.execute(text("SHOW lock_timeout")).scalar_one() == "10s"
        short = _task(factory, execution_lease_seconds=3.0)
        with short._open() as session:
            assert session.execute(text("SHOW statement_timeout")).scalar_one() == "1s"


def test_sqlite_sessions_are_plain(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    with _database("sqlite", tmp_path, monkeypatch) as (_, factory):
        with _task(factory)._open() as session:
            assert session.execute(text("SELECT 1")).scalar_one() == 1


# ---------------------------------------------------------------- liveness


def test_liveness_is_written_every_tick_and_inside_a_pass_at_most_once_per_interval(
    database: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, factory = database
    _unknowns(factory, 3)
    lease = _lease(factory)
    calls: list[str] = []
    real = loop_module.record_heartbeat

    def counting(session: Any, **kwargs: Any) -> None:
        calls.append(kwargs["kind"])
        real(session, **kwargs)

    monkeypatch.setattr(loop_module, "record_heartbeat", counting)
    task = _task(factory, provider=lambda: lease)
    task.tick()
    assert calls == [KIND_EFFECT_RECONCILER], "the interval is 15 s: the pass did not beat again"
    (beat,) = _heartbeats(factory)
    assert beat.ticks == 1
    task._interval_seconds = 0.0  # a long pass relative to the interval beats before each proposal
    calls.clear()
    task.tick()
    assert len(calls) == 1 + 3


def test_replaced_reconcilers_are_reaped_at_most_once_a_minute_with_a_window_that_covers_the_budget(
    database: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, factory = database
    calls: list[float] = []

    def counting(session: Any, **kwargs: Any) -> int:
        calls.append(kwargs["stale_after"])
        return 0

    monkeypatch.setattr(loop_module, "reap_stale_workers", counting)
    lease = _lease(factory)
    task = _task(factory, provider=lambda: lease)
    task.tick()
    task.tick()
    assert calls == [stale_after_seconds(15.0, 20.0 + 30.0)]


# ---------------------------------------------------------------- the lifecycle


def test_start_runs_the_pass_in_the_background_and_a_clean_stop_removes_the_heartbeat(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with _database("sqlite", tmp_path, monkeypatch) as (_, factory):
        _seed_run(factory)
        ids = _unknowns(factory, 1)
        lease = _lease(factory)
        ask = _Scripted()
        task = _task(factory, provider=lambda: lease, lookup=ask)

        async def scenario() -> None:
            await task.start()
            with pytest.raises(RuntimeError, match="already running"):
                await task.start()
            for _ in range(300):
                if ask.asked:
                    break
                await asyncio.sleep(0.02)
            await task.stop()
            await task.stop()  # a second stop is a no-op

        asyncio.run(scenario())
        assert len(ask.asked) == 1, "the loop ran a pass without anyone calling tick"
        assert _heartbeats(factory) == [], "a clean stop is not reported as a dead worker"
        assert _state(factory, ids[0]).state == UNKNOWN


def test_stop_without_start_does_nothing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    with _database("sqlite", tmp_path, monkeypatch) as (_, factory):
        asyncio.run(_task(factory).stop())
        assert _heartbeats(factory) == []


def test_a_tick_that_raises_does_not_end_the_loop(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with _database("sqlite", tmp_path, monkeypatch) as (_, factory):
        task = _task(factory)
        calls: list[int] = []

        def tick() -> None:
            calls.append(1)
            raise RuntimeError("boom")

        monkeypatch.setattr(task, "tick", tick)
        task._interval_seconds = 0.01

        async def scenario() -> None:
            await task.start()
            for _ in range(300):
                if len(calls) >= 3:
                    break
                await asyncio.sleep(0.02)
            await task.stop()

        with _logged() as records:
            asyncio.run(scenario())
        assert len(calls) >= 3
        assert any("tick raised; continuing" in m for m in records.messages)


def test_a_stop_that_outlasts_its_grace_leaves_no_orphan_pass(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The lookup in progress cannot be interrupted, but the pass ends at its next proposal: no proposal is looked at after
    the stop, and the thread is gone once that one lookup returns."""
    with _database("sqlite", tmp_path, monkeypatch) as (_, factory):
        _seed_run(factory)
        ids = _unknowns(factory, 3)
        lease = _lease(factory)
        inside, release_lookup, finished = threading.Event(), threading.Event(), threading.Event()
        asked: list[str] = []

        def lookup(connector: Any, base_url: str, key: str) -> Answer:
            asked.append(key)
            inside.set()
            release_lookup.wait(timeout=20)
            return Answer(404, {})

        task = _task(factory, provider=lambda: lease, lookup=lookup)
        real_tick = task.tick

        def tick() -> Any:
            try:
                return real_tick()
            finally:
                finished.set()

        monkeypatch.setattr(task, "tick", tick)

        async def scenario() -> None:
            await task.start()
            assert await asyncio.to_thread(inside.wait, 10)
            await task.stop(grace_seconds=0.05)
            assert not finished.is_set(), "the stop returned while the lookup was still running"
            release_lookup.set()
            assert await asyncio.to_thread(finished.wait, 10), "the orphaned pass ended on its own"

        with _logged() as records:
            asyncio.run(scenario())
        assert any("did not stop within" in m for m in records.messages)
        assert len(asked) == 1, "nothing after the stop was looked at"
        assert {_state(factory, i).state for i in ids} == {UNKNOWN}
        assert _heartbeats(factory) == []


# ---------------------------------------------------------------- composition root


def _built_reconciler(monkeypatch: pytest.MonkeyPatch, config: Any) -> EffectReconcilerTask | None:
    """Whatever ``create_app`` hands the lifespan as its effect reconciler."""
    from caliber import server

    seen: dict[str, Any] = {}
    real = server._build_lifespan

    def spy(*args: Any, **kwargs: Any) -> Any:
        seen.update(kwargs)
        return real(*args, **kwargs)

    monkeypatch.setattr(server, "_build_lifespan", spy)
    monkeypatch.setattr(server, "reference_profile_violations", lambda _config: [])
    server.create_app(config)
    built: EffectReconcilerTask | None = seen["effect_reconciler"]
    return built


def test_the_server_builds_the_loop_in_the_reference_profile_unless_it_is_turned_off(
    app_config: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    reference = app_config.model_copy(
        update={
            "profile": "reference",
            "effect_reconciler_interval_seconds": 7.0,
            "effect_reconciler_budget_seconds": 9.0,
            "effect_in_flight_max_seconds": 600.0,
            "effect_callback_connectors": BINDINGS,
        }
    )
    built = _built_reconciler(monkeypatch, reference)
    assert isinstance(built, EffectReconcilerTask)
    assert (built._interval_seconds, built._budget_seconds, built.in_flight_seconds) == (
        7.0,
        9.0,
        600.0,
    )
    assert built._targets == {("document-request", "PRJ-ep"): BASE_URL}
    off = reference.model_copy(update={"effect_reconciler_enabled": False})
    assert _built_reconciler(monkeypatch, off) is None
    development = reference.model_copy(update={"profile": "development"})
    assert _built_reconciler(monkeypatch, development) is None, (
        "no lease outside the reference profile"
    )
    assert app_config.effect_reconciler_enabled is True, (
        "on by default: a dispatcher needs its recovery"
    )


def test_the_in_flight_bound_has_a_floor_above_the_longest_default_dispatch() -> None:
    """5 attempts of the executor's 30 s timeout, its backoff and the 30 s receipt lookup come to about 3 minutes: a bound
    below the floor would expire dispatches that are merely slow."""
    from pydantic import ValidationError

    from caliber.config import CaliberConfig

    assert loop_module.MIN_IN_FLIGHT_MAX_SECONDS == 300.0
    with pytest.raises(ValidationError):
        CaliberConfig(effect_in_flight_max_seconds=299.0)
    assert CaliberConfig(effect_in_flight_max_seconds=300.0).effect_in_flight_max_seconds == 300.0
    assert CaliberConfig().effect_in_flight_max_seconds == 900.0


def test_a_proposal_whose_lookup_raises_does_not_block_the_ones_behind_it(
    database: Any, _fact_lookup: None
) -> None:
    """One row's failure (here a connector bug whose message carries a payload) is reported by class name only; the pass goes
    on, so the rows behind it are still reached and the cursor is not pinned in front of the bad row."""
    _, factory = database
    first, second = _unknowns(factory, 2)
    with factory() as session:
        keys = {i: proposal_view(session, i).logical_key for i in (first, second)}
    ask = _Scripted()
    ask.answers[keys[second]] = Answer(200, {"found": True})

    def lookup(connector: Any, base_url: str, key: str) -> Answer:
        if key == keys[first]:
            raise RuntimeError("PAYLOAD-MARKER-THREE")
        return ask(connector, base_url, key)

    lease = _lease(factory)
    with _logged() as records:
        report = _task(factory, provider=lambda: lease, lookup=lookup).tick()
    assert report is not None and report.ended == COMPLETE
    assert [(r.proposal_id, r.outcome, r.state, r.detail) for r in report.results] == [
        (first, STILL_UNKNOWN, UNKNOWN, "RuntimeError"),
        (second, RECONCILED_COMMITTED, COMMITTED, report.results[1].detail),
    ]
    assert _state(factory, first).state == UNKNOWN and _state(factory, second).state == COMMITTED
    assert "PAYLOAD-MARKER-THREE" not in " ".join(records.messages)


# ---------------------------------------------------------------- step token reservations (B08.4)


def _open_reservation(
    factory: sessionmaker[Session], *, occurrence: str, deadline: datetime
) -> str:
    with factory() as session:
        row = CaliberStepTokenReservation(
            reservation_id=f"tres-{occurrence}",
            workflow_run_id="RUN-ep",
            node_id="agent",
            occurrence_id=occurrence,
            attempt=1,
            step_request_id=f"step-{occurrence}",
            reserved_tokens=500,
            budget_tokens=10_000,
            deadline_at=deadline,
        )
        session.add(row)
        session.commit()
        return row.reservation_id


def _reservation_state(factory: sessionmaker[Session], reservation_id: str) -> tuple[Any, ...]:
    with factory() as session:
        row = session.get(CaliberStepTokenReservation, reservation_id)
        assert row is not None
        return (row.state, row.charged_tokens, row.charge_basis)


def test_the_grace_setting_defaults_and_is_bounded() -> None:
    assert EffectReconcilerTask(None, SimpleNamespace())._reservation_grace_seconds == 300.0
    assert (
        EffectReconcilerTask(
            None, _config(agent_reservation_grace_seconds=42)
        )._reservation_grace_seconds
        == 42.0
    )
    for bad in (0.5, 86401.0, float("nan"), True, "300"):
        with pytest.raises(ValueError, match="agent_reservation_grace_seconds"):
            EffectReconcilerTask(None, _config(agent_reservation_grace_seconds=bad))


def test_the_owner_finalizes_the_reservations_a_crashed_worker_left_open(database: Any) -> None:
    _, factory = database
    stale = _open_reservation(factory, occurrence="stale", deadline=datetime(2026, 1, 1))
    live = _open_reservation(factory, occurrence="live", deadline=datetime(2099, 1, 1))
    lease = _lease(factory)

    report = _task(factory, provider=lambda: lease).tick()

    assert report is not None and report.ended == COMPLETE
    assert _reservation_state(factory, stale) == ("abandoned", 500, "conservative")
    assert _reservation_state(factory, live) == ("reserved", None, None)


def test_a_secondary_finalizes_no_reservation(database: Any) -> None:
    _, factory = database
    stale = _open_reservation(factory, occurrence="stale", deadline=datetime(2026, 1, 1))
    assert _task(factory, provider=lambda: None).tick() is None
    assert _reservation_state(factory, stale)[0] == "reserved"


def test_the_sweep_runs_under_the_exact_lease_and_a_taken_over_lease_finalizes_nothing(
    database: Any,
) -> None:
    _, factory = database
    stale = _open_reservation(factory, occurrence="stale", deadline=datetime(2026, 1, 1))
    old = _lease(factory, "owner-old")
    _lease(factory, "owner-new")  # takeover: ``old`` is stale now
    with _logged() as records:
        report = _task(factory, provider=lambda: old).tick()
    assert report is not None
    assert _reservation_state(factory, stale)[0] == "reserved"
    assert any("sweep skipped: the execution-owner lease is gone" in m for m in records.messages)


def test_a_stop_in_progress_skips_the_sweep(database: Any) -> None:
    _, factory = database
    stale = _open_reservation(factory, occurrence="stale", deadline=datetime(2026, 1, 1))
    lease = _lease(factory)
    task = _task(factory, provider=lambda: lease)
    task._stop_requested.set()
    task.tick()
    assert _reservation_state(factory, stale)[0] == "reserved"


def test_a_sweep_that_fails_is_logged_by_class_and_never_stops_the_pass(
    database: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, factory = database

    def broken(*_args: Any, **_kwargs: Any) -> int:
        raise RuntimeError("PAYLOAD-MARKER-SWEEP")

    monkeypatch.setattr(loop_module, "abandon_stale", broken)
    lease = _lease(factory)
    with _logged() as records:
        report = _task(factory, provider=lambda: lease).tick()
    assert report is not None and report.ended == COMPLETE
    joined = " ".join(records.messages)
    assert "step token reservation sweep failed (RuntimeError)" in joined
    assert "PAYLOAD-MARKER-SWEEP" not in joined
