"""The Workspace release evaluator: it claims queued evaluations, oldest first, as the execution owner (B10.1, #701).

Every database case runs on SQLite and, where ``CALIBER_TEST_POSTGRES_URL`` is set (the "Migration parity
(PostgreSQL)" job), on PostgreSQL migrated by Alembic. Lease expiry is moved by the database's own clock, never by
sleeping past a ttl.
"""

from __future__ import annotations

import asyncio
import logging
import threading
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, select, text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from caliber.config import CaliberConfig
from caliber.db import Base
from caliber.db.models import (
    CaliberProject,
    CaliberWorkerHeartbeat,
    CaliberWorkspaceEnvironment,
    CaliberWorkspaceReleaseEvaluation,
    CaliberWorkspaceRevision,
)
from caliber.db.session import sessionmaker_from_engine
from caliber.observability.worker_registry import (
    KIND_WORKSPACE_RELEASE_EVALUATOR,
    reap_stale_workers,
)
from caliber.orchestrator import workspace_release_evaluator as evaluator_module
from caliber.orchestrator.workspace_release_evaluator import (
    ClaimedEvaluation,
    WorkspaceReleaseEvaluator,
)
from caliber.ownership import DEFAULT_LEASE, Lease, LeaseLost, acquire
from caliber.workspace_release_service import (
    EVALUATION_FAILED,
    EVALUATION_QUEUED,
    EVALUATION_RUNNING,
    EVALUATION_SUCCEEDED,
    claim_workspace_release_evaluation,
    create_workspace_release,
    create_workspace_release_evaluation,
    start_workspace_release_evaluation,
)
from tests.dialect_helpers import DIALECTS, dialect_database_url

PROJECT_ROOT = Path(__file__).resolve().parent.parent
ALEMBIC_INI = PROJECT_ROOT / "alembic.ini"
PROJECT_ID = "PRJ-b10-1"
ENVIRONMENT_ID = "WSE-b10-1"
REVISION_ID = "WSR-b10-1"
HEX = "a" * 64
LOGGER = "caliber.orchestrator.workspace_release_evaluator"
BASE = datetime(2026, 10, 1, 12, 0, 0)

Factory = sessionmaker[Session]


class _Records(logging.Handler):
    def __init__(self) -> None:
        super().__init__(logging.DEBUG)
        self.messages: list[str] = []

    @property
    def text(self) -> str:
        return "\n".join(self.messages)

    def emit(self, record: logging.LogRecord) -> None:
        self.messages.append(record.getMessage())


@contextmanager
def _logged() -> Iterator[_Records]:
    """Capture the evaluator's own log (Alembic's logging setup detaches pytest's root handler)."""
    handler = _Records()
    logger = logging.getLogger(LOGGER)
    previous = logger.level
    logger.addHandler(handler)
    logger.setLevel(logging.DEBUG)
    try:
        yield handler
    finally:
        logger.removeHandler(handler)
        logger.setLevel(previous)


@contextmanager
def _database(
    dialect: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Iterator[tuple[Engine, Factory]]:
    with dialect_database_url(dialect, tmp_path, "release_evaluator") as url:
        if dialect == "sqlite":
            engine = create_engine(url, connect_args={"check_same_thread": False})
            Base.metadata.create_all(engine)
        else:
            monkeypatch.setenv("CALIBER_DATABASE_URL", url)
            monkeypatch.chdir(PROJECT_ROOT)
            command.upgrade(Config(str(ALEMBIC_INI)), "head")
            engine = create_engine(url)
        try:
            yield engine, sessionmaker_from_engine(engine)
        finally:
            engine.dispose()


def _config(**overrides: Any) -> SimpleNamespace:
    values: dict[str, Any] = {
        "workspace_release_evaluator_lease_seconds": 300.0,
        "execution_lease_seconds": 30.0,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def _evaluator(
    factory: Factory,
    *,
    provider: Callable[[], Lease | None] | None = None,
    interval: float = 0.01,
    **config: Any,
) -> WorkspaceReleaseEvaluator:
    return WorkspaceReleaseEvaluator(
        factory, _config(**config), poll_interval_seconds=interval, lease_provider=provider
    )


def _seed_workspace(session: Session) -> None:
    session.add(CaliberProject(project_id=PROJECT_ID, name="B10.1", owner="dev"))
    session.add(
        CaliberWorkspaceEnvironment(
            environment_id=ENVIRONMENT_ID,
            project_id=PROJECT_ID,
            name="dev",
            environment_class="development",
            promotion_order=10,
            status="active",
            created_by="dev",
        )
    )
    session.add(
        CaliberWorkspaceRevision(
            revision_id=REVISION_ID,
            project_id=PROJECT_ID,
            revision_number=1,
            source_kind="git",
            manifest={"apiVersion": "caliber/v1alpha1"},
            manifest_sha256=HEX,
            source_bundle_sha256=HEX,
            source_attestation="caller_attested",
            revision_sha256=HEX,
            status="ready",
            created_by="dev",
        )
    )
    session.flush()


def _queue(session: Session, name: str, *, at: datetime | None = None) -> str:
    """A release in ``evaluating`` with one queued evaluation requested at ``at``; returns the evaluation id."""
    release = create_workspace_release(
        session,
        project_id=PROJECT_ID,
        revision_id=REVISION_ID,
        environment_id=ENVIRONMENT_ID,
        environment_config_sha256=HEX,
        runtime_dependencies_sha256=HEX,
        policy_sha256=HEX,
        request_idempotency_key=f"release-{name}",
        requested_by="dev",
    )
    start_workspace_release_evaluation(session, release, actor="dev", expected_lock_version=1)
    evaluation = create_workspace_release_evaluation(
        session,
        project_id=PROJECT_ID,
        workspace_release_id=release.release_id,
        idempotency_key=f"eval-{name}",
        evaluation_plan_sha256=HEX,
        input_sha256=HEX,
        requested_by="dev",
    )
    if at is not None:
        evaluation.requested_at = at
    session.flush()
    return evaluation.evaluation_id


def _seed(
    factory: Factory, *names: str, times: dict[str, datetime] | None = None
) -> dict[str, str]:
    ids: dict[str, str] = {}
    with factory() as session:
        _seed_workspace(session)
        for name in names:
            ids[name] = _queue(session, name, at=(times or {}).get(name))
        session.commit()
    return ids


def _row(factory: Factory, evaluation_id: str) -> CaliberWorkspaceReleaseEvaluation:
    with factory() as session:
        row = session.get(CaliberWorkspaceReleaseEvaluation, evaluation_id)
        assert row is not None
        session.expunge(row)
        return row


def _acquire(factory: Factory, owner: str, ttl: float = 30.0) -> Lease:
    with factory() as session, session.begin():
        lease = acquire(session, owner, ttl_seconds=ttl)
    assert lease is not None
    return lease


def _expire_lease(engine: Engine) -> None:
    past = (
        "timezone('utc', clock_timestamp()) - interval '1 second'"
        if engine.dialect.name == "postgresql"
        else "strftime('%Y-%m-%d %H:%M:%f', 'now', '-1 seconds')"
    )
    with engine.begin() as connection:
        connection.execute(
            text(f"UPDATE caliber_execution_owner SET expires_at = {past} WHERE lease_name = :n"),
            {"n": DEFAULT_LEASE},
        )


def _heartbeats(factory: Factory) -> list[CaliberWorkerHeartbeat]:
    with factory() as session:
        rows = list(session.execute(select(CaliberWorkerHeartbeat)).scalars())
        session.expunge_all()
        return rows


# ---------------------------------------------------------------- the named oracle


@pytest.mark.parametrize("dialect", DIALECTS)
def test_claims_queued_evaluation(
    dialect: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with _database(dialect, tmp_path, monkeypatch) as (_, factory):
        ids = _seed(factory, "one")
        worker = _evaluator(factory)

        claimed = worker.tick()

        row = _row(factory, ids["one"])  # read back through a new session: it was committed
        assert claimed == ClaimedEvaluation(
            evaluation_id=ids["one"],
            project_id=PROJECT_ID,
            workspace_release_id=row.workspace_release_id,
            attempt_number=1,
            worker_id=worker.worker_id,
            fencing_token=None,
        )
        assert row.status == EVALUATION_RUNNING
        assert row.claimed_by == worker.worker_id == row.started_by
        assert row.attempt_number == 1
        assert row.started_at is not None and row.heartbeat_at is not None
        assert row.lease_expires_at is not None
        assert row.lease_expires_at - row.heartbeat_at == timedelta(seconds=300)
        assert row.completed_at is None and row.gate_verdict_id is None
        assert worker.tick() is None, "the same row is not claimed twice"
        assert _row(factory, ids["one"]).attempt_number == 1


def test_claims_the_oldest_queued_evaluation_first_one_per_tick(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with _database("sqlite", tmp_path, monkeypatch) as (_, factory):
        ids = _seed(
            factory,
            "newest",
            "oldest",
            "middle",
            times={
                "newest": BASE + timedelta(minutes=9),
                "oldest": BASE,
                "middle": BASE + timedelta(minutes=5),
            },
        )
        worker = _evaluator(factory)
        order = [worker.tick(), worker.tick(), worker.tick()]
        assert [c.evaluation_id for c in order if c] == [
            ids["oldest"],
            ids["middle"],
            ids["newest"],
        ]
        assert worker.tick() is None


def test_equal_request_times_break_by_evaluation_id(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with _database("sqlite", tmp_path, monkeypatch) as (_, factory):
        ids = _seed(factory, "a", "b", times={"a": BASE, "b": BASE})
        worker = _evaluator(factory)
        first, second = worker.tick(), worker.tick()
        assert first and second
        assert [first.evaluation_id, second.evaluation_id] == sorted(ids.values())


def test_only_queued_rows_are_claimed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    with _database("sqlite", tmp_path, monkeypatch) as (_, factory):
        ids = _seed(factory, "running", "done", "failed", "queued")
        with factory() as session:
            for name, status in (
                ("running", EVALUATION_RUNNING),
                ("done", EVALUATION_SUCCEEDED),
                ("failed", EVALUATION_FAILED),
            ):
                row = session.get(CaliberWorkspaceReleaseEvaluation, ids[name])
                assert row is not None
                row.status = status
                row.requested_at = BASE  # older than the queued row: only its status keeps it out
                if status == EVALUATION_RUNNING:
                    row.claimed_by = "someone-else"
                    row.lease_expires_at = BASE + timedelta(days=3650)
            queued = session.get(CaliberWorkspaceReleaseEvaluation, ids["queued"])
            assert queued is not None
            queued.requested_at = BASE + timedelta(days=1)
            session.commit()
        worker = _evaluator(factory)

        claimed = worker.tick()

        assert claimed is not None and claimed.evaluation_id == ids["queued"]
        assert worker.tick() is None
        assert _row(factory, ids["running"]).claimed_by == "someone-else"
        assert _row(factory, ids["done"]).status == EVALUATION_SUCCEEDED
        assert _row(factory, ids["failed"]).claimed_by is None


# ---------------------------------------------------------------- ownership and fencing


def test_a_process_without_the_lease_claims_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with _database("sqlite", tmp_path, monkeypatch) as (_, factory):
        ids = _seed(factory, "one")
        worker = _evaluator(factory, provider=lambda: None)

        assert worker.tick() is None

        assert _row(factory, ids["one"]).status == EVALUATION_QUEUED
        assert _row(factory, ids["one"]).claimed_by is None
        assert len(_heartbeats(factory)) == 1, "it is alive and says so, though it claims nothing"


@pytest.mark.parametrize("dialect", DIALECTS)
def test_a_claim_is_fenced_by_the_lease_it_was_made_under(
    dialect: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with _database(dialect, tmp_path, monkeypatch) as (_, factory):
        ids = _seed(factory, "one")
        lease = _acquire(factory, "node-a")
        worker = _evaluator(factory, provider=lambda: lease)

        claimed = worker.tick()

        assert claimed is not None and claimed.fencing_token == lease.fencing_token == 1
        assert _row(factory, ids["one"]).claimed_by == worker.worker_id


@pytest.mark.parametrize("dialect", DIALECTS)
def test_a_lease_taken_over_claims_nothing(
    dialect: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with _database(dialect, tmp_path, monkeypatch) as (engine, factory):
        ids = _seed(factory, "one")
        stale = _acquire(factory, "node-a")
        _expire_lease(engine)
        taken = _acquire(factory, "node-b")
        assert taken.fencing_token == 2
        worker = _evaluator(factory, provider=lambda: stale)

        with _logged() as caplog:
            assert worker.tick() is None

        row = _row(factory, ids["one"])
        assert row.status == EVALUATION_QUEUED and row.claimed_by is None, "nothing recorded"
        assert any("no longer the execution owner" in m for m in caplog.messages)
        # the new owner's evaluator, holding the exact lease, claims it
        newer = _evaluator(factory, provider=lambda: taken)
        claimed = newer.tick()
        assert claimed is not None and claimed.fencing_token == 2


@pytest.mark.parametrize("dialect", DIALECTS)
def test_an_expired_lease_claims_nothing(
    dialect: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with _database(dialect, tmp_path, monkeypatch) as (engine, factory):
        ids = _seed(factory, "one")
        lease = _acquire(factory, "node-a")
        _expire_lease(engine)  # nobody took over yet: the old owner still may not act
        worker = _evaluator(factory, provider=lambda: lease)

        assert worker.tick() is None

        assert _row(factory, ids["one"]).status == EVALUATION_QUEUED


def test_a_lease_store_failure_claims_nothing_and_is_logged_without_row_content(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with _database("sqlite", tmp_path, monkeypatch) as (_, factory):
        ids = _seed(factory, "one")
        lease = _acquire(factory, "node-a")

        def unavailable(session: Session, held: Lease) -> None:
            raise LeaseLost("the lease store is unavailable") from RuntimeError("driver detail")

        monkeypatch.setattr(evaluator_module, "assert_fenced", unavailable)
        worker = _evaluator(factory, provider=lambda: lease)
        with _logged() as caplog:
            assert worker.tick() is None

        assert _row(factory, ids["one"]).status == EVALUATION_QUEUED
        assert any("lease store is unavailable" in m for m in caplog.messages)
        assert HEX not in caplog.text, "no row content in the log"


def test_the_fence_is_checked_before_the_row_is_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A lost lease must not even look at the queue: the fence runs first, in the claiming transaction."""
    with _database("sqlite", tmp_path, monkeypatch) as (_, factory):
        _seed(factory, "one")
        lease = _acquire(factory, "node-a")
        calls: list[str] = []
        real_claim = claim_workspace_release_evaluation

        def fence(session: Session, held: Lease) -> None:
            calls.append("fence")

        def claim(*args: Any, **kwargs: Any) -> Any:
            calls.append("claim")
            return real_claim(*args, **kwargs)

        monkeypatch.setattr(evaluator_module, "assert_fenced", fence)
        monkeypatch.setattr(evaluator_module, "claim_workspace_release_evaluation", claim)
        _evaluator(factory, provider=lambda: lease).tick()
        assert calls == ["fence", "claim"]


def test_bind_lease_provider_turns_the_fence_on_and_off(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with _database("sqlite", tmp_path, monkeypatch) as (_, factory):
        ids = _seed(factory, "a", "b", times={"a": BASE, "b": BASE + timedelta(minutes=1)})
        worker = _evaluator(factory)
        worker.bind_lease_provider(lambda: None)
        assert worker.tick() is None
        worker.bind_lease_provider(None)  # no lease: the loop is the owner by construction
        claimed = worker.tick()
        assert claimed is not None and claimed.evaluation_id == ids["a"]
        assert claimed.fencing_token is None


# ---------------------------------------------------------------- races (PostgreSQL)


def test_postgresql_concurrent_evaluators_claim_each_row_exactly_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from tests.dialect_helpers import POSTGRES_TEST_URL

    if not POSTGRES_TEST_URL:
        pytest.skip("CALIBER_TEST_POSTGRES_URL is not set")
    with _database("postgresql", tmp_path, monkeypatch) as (_, factory):
        ids = _seed(factory, *[f"r{i}" for i in range(6)])
        lease = _acquire(factory, "node-a")
        workers = [_evaluator(factory, provider=lambda: lease) for _ in range(3)]
        claimed: list[ClaimedEvaluation] = []
        errors: list[BaseException] = []
        start = threading.Barrier(len(workers))

        def run(worker: WorkspaceReleaseEvaluator) -> None:
            try:
                start.wait(timeout=10)
                for _ in range(2):
                    got = worker.tick()
                    if got is not None:
                        claimed.append(got)
            except BaseException as exc:
                errors.append(exc)

        threads = [threading.Thread(target=run, args=(w,)) for w in workers]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=60)
        assert errors == []
        # whatever interleaving happened, no row went to two claimers and none was claimed twice
        won = [c.evaluation_id for c in claimed]
        assert len(won) == len(set(won)) == 6
        assert set(won) == set(ids.values())
        for evaluation_id in ids.values():
            row = _row(factory, evaluation_id)
            assert row.status == EVALUATION_RUNNING and row.attempt_number == 1
        assert {c.worker_id for c in claimed} <= {w.worker_id for w in workers}


def test_postgresql_a_held_lease_row_bounds_the_claim_wait(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from tests.dialect_helpers import POSTGRES_TEST_URL

    if not POSTGRES_TEST_URL:
        pytest.skip("CALIBER_TEST_POSTGRES_URL is not set")
    with _database("postgresql", tmp_path, monkeypatch) as (engine, factory):
        ids = _seed(factory, "one")
        lease = _acquire(factory, "node-a")
        # a third of 0.3 s is below the 100 ms floor: the claim waits at most 100 ms for the lease row
        worker = _evaluator(factory, provider=lambda: lease, execution_lease_seconds=0.3)
        with engine.connect() as holder:
            holder.execute(text("SELECT 1 FROM caliber_execution_owner FOR UPDATE"))
            with _logged() as caplog:
                assert worker.tick() is None
            holder.rollback()
        assert _row(factory, ids["one"]).status == EVALUATION_QUEUED
        assert any("lease store is unavailable" in m for m in caplog.messages)
        assert worker.tick() is not None, "once the row is free the claim goes through"


def test_postgresql_a_takeover_waits_for_the_claim_in_flight(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The claim holds the lease row until it commits, so a takeover cannot overtake it."""
    from tests.dialect_helpers import POSTGRES_TEST_URL

    if not POSTGRES_TEST_URL:
        pytest.skip("CALIBER_TEST_POSTGRES_URL is not set")
    with _database("postgresql", tmp_path, monkeypatch) as (engine, factory):
        _seed(factory, "one")
        lease = _acquire(factory, "node-a")
        inside = threading.Event()
        proceed = threading.Event()
        real_claim = claim_workspace_release_evaluation

        def slow_claim(*args: Any, **kwargs: Any) -> Any:
            inside.set()
            assert proceed.wait(timeout=10)
            return real_claim(*args, **kwargs)

        monkeypatch.setattr(evaluator_module, "claim_workspace_release_evaluation", slow_claim)
        worker = _evaluator(factory, provider=lambda: lease)
        result: list[ClaimedEvaluation | None] = []
        thread = threading.Thread(target=lambda: result.append(worker.tick()))
        thread.start()
        assert inside.wait(timeout=10)
        # the claim is mid-transaction and holds the lease row: a competing update of it must wait
        with engine.connect() as other:
            other.execute(text("SET lock_timeout = 200"))
            with pytest.raises(Exception, match="lock timeout|lock_timeout|could not obtain"):
                other.execute(text("UPDATE caliber_execution_owner SET owner_id = owner_id"))
            other.rollback()
        proceed.set()
        thread.join(timeout=30)
        assert result and result[0] is not None and result[0].fencing_token == lease.fencing_token


# ---------------------------------------------------------------- races (a lost claim)


def test_a_claim_that_loses_the_race_claims_nothing_this_tick(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with _database("sqlite", tmp_path, monkeypatch) as (_, factory):
        ids = _seed(
            factory, "first", "second", times={"first": BASE, "second": BASE + timedelta(hours=1)}
        )
        real_claim = claim_workspace_release_evaluation
        rival = _evaluator(factory)
        stolen: list[str] = []

        def claim(session: Session, evaluation: Any, **kwargs: Any) -> Any:
            if not stolen:  # another claimer wins the row between this one's read and its update
                stolen.append(evaluation.evaluation_id)
                with factory() as other:
                    real_claim(other, evaluation, worker_id=rival.worker_id, lease_seconds=300)
                    other.commit()
            return real_claim(session, evaluation, **kwargs)

        monkeypatch.setattr(evaluator_module, "claim_workspace_release_evaluation", claim)
        worker = _evaluator(factory)
        with _logged() as caplog:
            assert worker.tick() is None
        assert stolen == [ids["first"]]
        assert any("lost a claim race" in m for m in caplog.messages)
        assert _row(factory, ids["first"]).claimed_by == rival.worker_id
        assert _row(factory, ids["second"]).status == EVALUATION_QUEUED
        # the next tick takes the next row
        claimed = worker.tick()
        assert claimed is not None and claimed.evaluation_id == ids["second"]


def test_a_service_refusal_that_is_not_a_lost_race_is_not_swallowed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with _database("sqlite", tmp_path, monkeypatch) as (_, factory):
        ids = _seed(factory, "one")

        def broken(*args: Any, **kwargs: Any) -> Any:
            raise RuntimeError("the database went away")

        monkeypatch.setattr(evaluator_module, "claim_workspace_release_evaluation", broken)
        with pytest.raises(RuntimeError, match="went away"):
            _evaluator(factory).tick()
        assert _row(factory, ids["one"]).status == EVALUATION_QUEUED, "nothing half-claimed"


# ---------------------------------------------------------------- liveness


def test_every_tick_records_a_heartbeat_idle_ticks_included(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with _database("sqlite", tmp_path, monkeypatch) as (_, factory):
        worker = _evaluator(factory)
        assert worker.tick() is None  # nothing queued
        (row,) = _heartbeats(factory)
        assert row.worker_id == worker.worker_id
        assert row.kind == KIND_WORKSPACE_RELEASE_EVALUATOR == "workspace_release_evaluator"
        assert row.ticks == 1
        worker.tick()
        (row,) = _heartbeats(factory)
        assert row.ticks == 2


def test_a_heartbeat_failure_never_breaks_a_tick(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with _database("sqlite", tmp_path, monkeypatch) as (engine, factory):
        ids = _seed(factory, "one")
        with engine.begin() as connection:
            connection.execute(text("DROP TABLE caliber_worker_heartbeats"))  # the write must fail

        claimed = _evaluator(factory).tick()

        assert claimed is not None and claimed.evaluation_id == ids["one"]
        assert _row(factory, ids["one"]).status == EVALUATION_RUNNING


def test_a_stale_row_of_a_replaced_evaluator_is_reaped_but_other_kinds_are_not(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with _database("sqlite", tmp_path, monkeypatch) as (_, factory):
        long_ago = datetime.now(timezone.utc) - timedelta(days=2)
        with factory() as session:
            for worker_id, kind in (
                ("gone-evaluator", KIND_WORKSPACE_RELEASE_EVALUATOR),
                ("gone-run-worker", "workflow_run"),
            ):
                session.add(
                    CaliberWorkerHeartbeat(
                        worker_id=worker_id,
                        kind=kind,
                        started_at=long_ago,
                        last_heartbeat_at=long_ago,
                        ticks=1,
                    )
                )
            session.commit()
        worker = _evaluator(factory)
        worker.tick()
        ids = {row.worker_id for row in _heartbeats(factory)}
        assert ids == {worker.worker_id, "gone-run-worker"}


def test_reaping_runs_at_most_once_a_minute(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with _database("sqlite", tmp_path, monkeypatch) as (_, factory):
        calls: list[str] = []
        real = reap_stale_workers

        def counting(*args: Any, **kwargs: Any) -> int:
            calls.append("reap")
            return real(*args, **kwargs)

        monkeypatch.setattr(evaluator_module, "reap_stale_workers", counting)
        worker = _evaluator(factory)
        worker.tick()
        worker.tick()
        assert calls == ["reap"]


# ---------------------------------------------------------------- the loop


def test_the_loop_claims_a_queued_row_and_stop_removes_the_heartbeat(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with _database("sqlite", tmp_path, monkeypatch) as (_, factory):
        ids = _seed(factory, "one")
        worker = _evaluator(factory)

        async def scenario() -> None:
            await worker.start()
            with pytest.raises(RuntimeError, match="already running"):
                await worker.start()
            for _ in range(200):
                if _row(factory, ids["one"]).status == EVALUATION_RUNNING:
                    break
                await asyncio.sleep(0.02)
            await worker.stop()
            await worker.stop()  # a second stop is a no-op

        asyncio.run(scenario())
        assert _row(factory, ids["one"]).claimed_by == worker.worker_id
        assert _heartbeats(factory) == [], "a clean stop is not reported as a dead worker"


def test_a_failing_tick_is_logged_and_the_loop_keeps_going(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with _database("sqlite", tmp_path, monkeypatch) as (_, factory):
        worker = _evaluator(factory)
        calls: list[int] = []

        def tick() -> None:
            calls.append(1)
            if len(calls) == 1:
                raise RuntimeError("boom")

        monkeypatch.setattr(worker, "tick", tick)

        async def scenario() -> None:
            await worker.start()
            for _ in range(200):
                if len(calls) >= 3:
                    break
                await asyncio.sleep(0.01)
            await worker.stop()

        with _logged() as caplog:
            asyncio.run(scenario())
        assert len(calls) >= 3
        assert any("tick raised; continuing" in m for m in caplog.messages)


def test_stop_cancels_a_tick_that_outlives_the_grace(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with _database("sqlite", tmp_path, monkeypatch) as (_, factory):
        worker = _evaluator(factory)
        inside, release = threading.Event(), threading.Event()

        def tick() -> None:
            inside.set()
            release.wait(timeout=10)

        monkeypatch.setattr(worker, "tick", tick)

        async def scenario() -> None:
            await worker.start()
            assert await asyncio.to_thread(inside.wait, 10)
            await worker.stop(grace_seconds=0.05)
            release.set()

        with _logged() as caplog:
            asyncio.run(scenario())
        assert any("did not stop within" in m for m in caplog.messages)


def test_stop_without_start_does_nothing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    with _database("sqlite", tmp_path, monkeypatch) as (_, factory):
        asyncio.run(_evaluator(factory).stop())
        assert _heartbeats(factory) == []


# ---------------------------------------------------------------- construction and configuration


def test_construction_refuses_a_non_positive_interval_and_a_lease_under_a_second(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with _database("sqlite", tmp_path, monkeypatch) as (_, factory):
        for interval in (0, -1.0, float("nan")):
            with pytest.raises(ValueError, match="poll_interval_seconds"):
                WorkspaceReleaseEvaluator(factory, _config(), poll_interval_seconds=interval)
        with pytest.raises(ValueError, match="at least 1"):
            _evaluator(factory, workspace_release_evaluator_lease_seconds=0.5)


def test_the_documented_defaults_and_the_env_vars() -> None:
    defaults = CaliberConfig.load(environ={})
    assert defaults.workspace_release_evaluator_enabled is False, "opt in until B10.2 executes"
    assert defaults.workspace_release_evaluator_interval_seconds == 5.0
    assert defaults.workspace_release_evaluator_lease_seconds == 300.0
    tuned = CaliberConfig.load(
        environ={
            "CALIBER_WORKSPACE_RELEASE_EVALUATOR_ENABLED": "true",
            "CALIBER_WORKSPACE_RELEASE_EVALUATOR_INTERVAL_SECONDS": "1.5",
            "CALIBER_WORKSPACE_RELEASE_EVALUATOR_LEASE_SECONDS": "60",
        }
    )
    assert tuned.workspace_release_evaluator_enabled is True
    assert tuned.workspace_release_evaluator_interval_seconds == 1.5
    assert tuned.workspace_release_evaluator_lease_seconds == 60.0


def test_the_constructor_takes_the_documented_positional_arguments(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with _database("sqlite", tmp_path, monkeypatch) as (_, factory):
        worker = WorkspaceReleaseEvaluator(factory, _config())
        assert worker._interval_seconds == 5.0
        assert worker.worker_id.startswith("workspace-release-evaluator-")
        assert len(worker.worker_id) <= 128, "fits caliber_worker_heartbeats.worker_id"
        assert len(KIND_WORKSPACE_RELEASE_EVALUATOR) <= 32, "fits caliber_worker_heartbeats.kind"


# ---------------------------------------------------------------- composition root


def _built_evaluator(
    monkeypatch: pytest.MonkeyPatch, config: CaliberConfig
) -> WorkspaceReleaseEvaluator | None:
    """Whatever ``create_app`` hands the lifespan as its release evaluator."""
    from caliber import server

    seen: dict[str, Any] = {}
    real = server._build_lifespan

    def spy(*args: Any, **kwargs: Any) -> Any:
        seen.update(kwargs)
        return real(*args, **kwargs)

    monkeypatch.setattr(server, "_build_lifespan", spy)
    server.create_app(config)
    built: WorkspaceReleaseEvaluator | None = seen["workspace_release_evaluator"]
    return built


def test_the_server_builds_the_evaluator_only_when_the_release_feature_and_its_opt_in_are_on(
    app_config: CaliberConfig, monkeypatch: pytest.MonkeyPatch
) -> None:
    on = app_config.model_copy(
        update={
            "feature_release_candidates_enabled": True,
            "workspace_release_evaluator_enabled": True,
            "workspace_release_evaluator_interval_seconds": 7.0,
        }
    )
    built = _built_evaluator(monkeypatch, on)
    assert isinstance(built, WorkspaceReleaseEvaluator)
    assert built._interval_seconds == 7.0

    feature_off = on.model_copy(update={"feature_release_candidates_enabled": False})
    assert _built_evaluator(monkeypatch, feature_off) is None, "a disabled feature stays dormant"

    flag_off = on.model_copy(update={"workspace_release_evaluator_enabled": False})
    assert _built_evaluator(monkeypatch, flag_off) is None
    default = app_config.model_copy(update={"feature_release_candidates_enabled": True})
    assert _built_evaluator(monkeypatch, default) is None, "opt-in: the default leaves it off"
