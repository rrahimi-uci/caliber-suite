"""The single-owner execution lease and its fencing token (B13.2, #668).

Every case runs on SQLite and, where ``CALIBER_TEST_POSTGRES_URL`` is set (the "Migration parity (PostgreSQL)"
job), on PostgreSQL, against a database migrated by Alembic. Expiry is the database's: tests move a lease into the
past by writing ``expires_at`` relative to the database clock, never by sleeping past a ttl, except where a test is
about a lease expiring *during* a transaction.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.engine import Engine
from sqlalchemy.exc import IntegrityError, OperationalError
from sqlalchemy.orm import Session

from caliber.ownership import (
    DEFAULT_LEASE,
    MAX_TTL_SECONDS,
    Lease,
    LeaseError,
    LeaseLost,
    acquire,
    assert_fenced,
    current_lease,
    database_now,
    release,
    renew,
)
from tests.dialect_helpers import DIALECTS, POSTGRES_TEST_URL, dialect_database_url

PROJECT_ROOT = Path(__file__).resolve().parent.parent
ALEMBIC_INI = PROJECT_ROOT / "alembic.ini"
pytestmark = pytest.mark.slow

TTL = 30.0


@contextmanager
def _migrated(
    dialect: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Iterator[tuple[Engine, Config]]:
    with dialect_database_url(dialect, tmp_path, "execution_owner") as db_url:
        monkeypatch.setenv("CALIBER_DATABASE_URL", db_url)
        monkeypatch.chdir(PROJECT_ROOT)
        config = Config(str(ALEMBIC_INI))
        command.upgrade(config, "head")
        engine = create_engine(db_url)
        try:
            yield engine, config
        finally:
            engine.dispose()


def _acquire(
    engine: Engine, owner: str, ttl: float = TTL, name: str = DEFAULT_LEASE
) -> Lease | None:
    with Session(engine) as session, session.begin():
        return acquire(session, owner, ttl_seconds=ttl, name=name)


def _renew(engine: Engine, lease: Lease, ttl: float = TTL) -> Lease:
    with Session(engine) as session, session.begin():
        return renew(session, lease, ttl_seconds=ttl)


def _fenced(engine: Engine, lease: Lease) -> None:
    with Session(engine) as session, session.begin():
        assert_fenced(session, lease)


def _release(engine: Engine, lease: Lease) -> bool:
    with Session(engine) as session, session.begin():
        return release(session, lease)


def _stored(engine: Engine, name: str = DEFAULT_LEASE) -> Lease | None:
    with Session(engine) as session:
        return current_lease(session, name)


def _expire(engine: Engine, name: str = DEFAULT_LEASE) -> None:
    """Move the lease one second into the past by the database's own clock."""
    past = (
        "timezone('utc', clock_timestamp()) - interval '1 second'"
        if engine.dialect.name == "postgresql"
        else "strftime('%Y-%m-%d %H:%M:%f', 'now', '-1 seconds')"
    )
    with engine.begin() as connection:
        connection.execute(
            text(f"UPDATE caliber_execution_owner SET expires_at = {past} WHERE lease_name = :n"),
            {"n": name},
        )


def _lost(action: Callable[[], Any]) -> None:
    with pytest.raises(LeaseLost):
        action()


# ---------------------------------------------------------------- the named oracle


@pytest.mark.parametrize("dialect", DIALECTS)
def test_single_owner_and_fencing(
    dialect: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with _migrated(dialect, tmp_path, monkeypatch) as (engine, _):
        a = _acquire(engine, "node-a")
        assert a is not None and a.fencing_token == 1 and a.owner_id == "node-a"
        assert _acquire(engine, "node-b") is None, "one owner at a time"
        _fenced(engine, a)

        renewed = _acquire(
            engine, "node-a"
        )  # the owner asking again is a renewal: same token, later expiry
        assert (
            renewed is not None
            and renewed.fencing_token == 1
            and renewed.expires_at >= a.expires_at
        )
        same = _renew(engine, a)
        assert same.fencing_token == 1 and same.expires_at >= renewed.expires_at

        _expire(engine)
        _lost(
            lambda: _fenced(engine, a)
        )  # expired: the old owner may not act even though nobody took over yet
        b = _acquire(engine, "node-b")
        assert b is not None and b.fencing_token == 2, "a new acquisition raises the token"
        _lost(lambda: _fenced(engine, a))
        _lost(lambda: _renew(engine, a))
        assert _release(engine, a) is False, "a stale owner cannot release the new owner's lease"
        assert _acquire(engine, "node-a") is None
        _fenced(engine, b)
        assert _renew(engine, b).fencing_token == 2

        assert _release(engine, b) is True
        _lost(lambda: _fenced(engine, b))
        again = _acquire(engine, "node-a")
        assert again is not None and again.fencing_token == 3, (
            "release keeps the token; the next acquirer raises it"
        )
        assert _stored(engine) == Lease(DEFAULT_LEASE, "node-a", 3, again.expires_at)


# ---------------------------------------------------------------- tokens


@pytest.mark.parametrize("dialect", DIALECTS)
def test_the_first_acquisition_is_token_one_and_reacquiring_after_expiry_raises_it_even_for_the_same_owner(
    dialect: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with _migrated(dialect, tmp_path, monkeypatch) as (engine, _):
        assert _stored(engine) is None
        first = _acquire(engine, "node-a")
        assert first is not None and first.fencing_token == 1
        _expire(engine)
        second = _acquire(engine, "node-a")
        assert second is not None and second.fencing_token == 2, (
            "its own lapsed lease is a new acquisition"
        )
        _lost(lambda: _fenced(engine, first))
        _fenced(engine, second)


@pytest.mark.parametrize("dialect", DIALECTS)
def test_tokens_grow_by_exactly_one_per_new_acquisition_and_never_on_renewal(
    dialect: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with _migrated(dialect, tmp_path, monkeypatch) as (engine, _):
        seen: list[int] = []
        for turn in range(12):
            lease = _acquire(engine, f"node-{turn % 3}")
            assert lease is not None
            seen.append(lease.fencing_token)
            for _ in range(3):
                assert _renew(engine, lease).fencing_token == lease.fencing_token
            _expire(engine)
        assert seen == list(range(1, 13))


@pytest.mark.parametrize("dialect", DIALECTS)
def test_stale_future_and_foreign_tokens_never_pass_the_fence(
    dialect: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Equality on owner *and* token, not ``token < latest`` and not a freshly read current token."""
    with _migrated(dialect, tmp_path, monkeypatch) as (engine, _):
        _expire_first = _acquire(engine, "node-a")
        assert _expire_first is not None
        _expire(engine)
        held = _acquire(engine, "node-b")
        assert held is not None and held.fencing_token == 2
        for forged in (
            Lease(DEFAULT_LEASE, "node-b", 1, held.expires_at),  # stale token, right owner
            Lease(DEFAULT_LEASE, "node-b", 3, held.expires_at),  # a token from the future
            Lease(DEFAULT_LEASE, "node-b", 2**40, held.expires_at),
            Lease(DEFAULT_LEASE, "node-a", 2, held.expires_at),  # right token, wrong owner
            Lease(DEFAULT_LEASE, "node-b", 0, held.expires_at),
            Lease("another-lease", "node-b", 2, held.expires_at),  # a different lease name
        ):
            _lost(lambda forged=forged: _fenced(engine, forged))
            _lost(lambda forged=forged: _renew(engine, forged))
            assert _release(engine, forged) is False
        _fenced(engine, held)
        assert _stored(engine) is not None and _stored(engine).fencing_token == 2  # type: ignore[union-attr]


@pytest.mark.parametrize("dialect", DIALECTS)
def test_a_delayed_heartbeat_extends_nothing_and_never_resurrects_a_lapsed_lease(
    dialect: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with _migrated(dialect, tmp_path, monkeypatch) as (engine, _):
        a = _acquire(engine, "node-a")
        assert a is not None
        _expire(engine)
        before = _stored(engine)
        _lost(lambda: _renew(engine, a))  # late, and nobody has taken over yet
        assert _stored(engine) == before, "a refused heartbeat changes nothing"
        b = _acquire(engine, "node-b")
        assert b is not None and b.fencing_token == 2
        stored = _stored(engine)
        _lost(lambda: _renew(engine, a))  # late, and another owner holds it
        assert _stored(engine) == stored


@pytest.mark.parametrize("dialect", DIALECTS)
def test_independent_lease_names_do_not_interfere(
    dialect: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with _migrated(dialect, tmp_path, monkeypatch) as (engine, _):
        one, two = (
            _acquire(engine, "node-a", name="loops"),
            _acquire(engine, "node-b", name="outbox"),
        )
        assert one is not None and two is not None
        assert (one.fencing_token, two.fencing_token) == (1, 1)
        assert _acquire(engine, "node-b", name="loops") is None
        _expire(engine, "loops")
        _fenced(engine, two)
        _lost(lambda: _fenced(engine, one))


# ---------------------------------------------------------------- database time


@pytest.mark.parametrize("dialect", DIALECTS)
def test_expiry_is_the_database_clock_plus_the_ttl_not_the_callers(
    dialect: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with _migrated(dialect, tmp_path, monkeypatch) as (engine, _):
        with Session(engine) as session:
            before = database_now(session)
        lease = _acquire(engine, "node-a", ttl=120.0)
        with Session(engine) as session:
            after = database_now(session)
        assert lease is not None
        assert before + timedelta(seconds=119) <= lease.expires_at <= after + timedelta(seconds=121)
        # A process whose own clock is wildly wrong changes nothing: the statement uses the database's.
        monkeypatch.setattr("time.time", lambda: 4_000_000_000.0)
        monkeypatch.setattr(
            "caliber.ownership.datetime",
            type("Frozen", (datetime,), {"now": staticmethod(lambda *a: datetime(1999, 1, 1))}),
        )
        _fenced(engine, lease)
        assert _renew(engine, lease, ttl=120.0).fencing_token == 1


def test_a_lease_that_has_not_expired_by_database_time_is_not_taken(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with _migrated("sqlite", tmp_path, monkeypatch) as (engine, _):
        held = _acquire(engine, "node-a", ttl=3600.0)
        assert held is not None
        for _ in range(5):
            assert _acquire(engine, "node-b") is None
        _fenced(engine, held)


# ---------------------------------------------------------------- validation and failure


@pytest.mark.parametrize(
    ("owner", "ttl", "name"),
    [
        ("", TTL, DEFAULT_LEASE),
        ("  ", TTL, DEFAULT_LEASE),
        (" node", TTL, DEFAULT_LEASE),
        ("node ", TTL, DEFAULT_LEASE),
        (None, TTL, DEFAULT_LEASE),
        ("node", 0, DEFAULT_LEASE),
        ("node", -1.0, DEFAULT_LEASE),
        ("node", MAX_TTL_SECONDS + 1, DEFAULT_LEASE),
        ("node", True, DEFAULT_LEASE),
        ("node", TTL, ""),
        ("node", TTL, "   "),
    ],
)
def test_a_malformed_request_is_refused_and_touches_nothing(
    owner: Any, ttl: Any, name: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with _migrated("sqlite", tmp_path, monkeypatch) as (engine, _):
        with pytest.raises(LeaseError):
            _acquire(engine, owner, ttl, name)
        with pytest.raises(LeaseError):
            _renew(engine, Lease(name, owner, 1, datetime(2030, 1, 1)), ttl)
        assert _stored(engine) is None


def test_the_largest_allowed_ttl_is_accepted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with _migrated("sqlite", tmp_path, monkeypatch) as (engine, _):
        assert _acquire(engine, "node-a", MAX_TTL_SECONDS) is not None


def test_an_unavailable_lease_store_is_a_loss_never_a_grant(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with _migrated("sqlite", tmp_path, monkeypatch) as (engine, _):
        lease = _acquire(engine, "node-a")
        assert lease is not None

        def broken(*_a: Any, **_k: Any) -> Any:
            raise OperationalError("UPDATE", {}, Exception("connection lost"))

        with Session(engine) as session:
            monkeypatch.setattr(session, "execute", broken)
            for action in (
                lambda: acquire(session, "node-b", ttl_seconds=TTL),
                lambda: renew(session, lease, ttl_seconds=TTL),
                lambda: release(session, lease),
                lambda: assert_fenced(session, lease),
            ):
                with pytest.raises(LeaseLost, match="unavailable"):
                    action()


@pytest.mark.parametrize("dialect", DIALECTS)
def test_the_table_refuses_a_token_below_one(
    dialect: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with _migrated(dialect, tmp_path, monkeypatch) as (engine, _):
        with pytest.raises(IntegrityError):
            with engine.begin() as connection:
                connection.execute(
                    text(
                        "INSERT INTO caliber_execution_owner (lease_name, owner_id, fencing_token, expires_at, "
                        "acquired_at, renewed_at) VALUES ('x', 'o', 0, '2030-01-01', '2030-01-01', '2030-01-01')"
                    )
                )


# ---------------------------------------------------------------- PostgreSQL: real concurrency


def _race(engine: Engine, workers: list[Callable[[Session], Any]]) -> list[Any]:
    """Run ``workers`` at the same instant, each in its own session and transaction; return their results."""
    barrier = threading.Barrier(len(workers))
    results: list[Any] = [None] * len(workers)
    errors: list[BaseException] = []

    def run(index: int, work: Callable[[Session], Any]) -> None:
        try:
            barrier.wait(10)
            with Session(engine) as session, session.begin():
                results[index] = work(session)
        except BaseException as exc:  # pragma: no cover - surfaced below
            errors.append(exc)

    threads = [
        threading.Thread(target=run, args=(i, w), daemon=True) for i, w in enumerate(workers)
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(30)
    assert not errors, errors
    return results


@pytest.mark.skipif(not POSTGRES_TEST_URL, reason="real concurrency is PostgreSQL-only")
def test_postgresql_exactly_one_of_many_concurrent_first_acquirers_wins(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with _migrated("postgresql", tmp_path, monkeypatch) as (engine, _):
        workers = [lambda s, n=n: acquire(s, f"node-{n}", ttl_seconds=TTL) for n in range(8)]
        results = _race(engine, workers)
        winners = [lease for lease in results if lease is not None]
        assert len(winners) == 1 and winners[0].fencing_token == 1
        assert _stored(engine) == Lease(
            DEFAULT_LEASE, winners[0].owner_id, 1, winners[0].expires_at
        )


@pytest.mark.skipif(not POSTGRES_TEST_URL, reason="real concurrency is PostgreSQL-only")
def test_postgresql_exactly_one_of_many_concurrent_takeovers_of_an_expired_lease_wins(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with _migrated("postgresql", tmp_path, monkeypatch) as (engine, _):
        assert _acquire(engine, "incumbent") is not None
        for round_number in range(1, 4):
            _expire(engine)
            results = _race(
                engine, [lambda s, n=n: acquire(s, f"node-{n}", ttl_seconds=TTL) for n in range(8)]
            )
            winners = [lease for lease in results if lease is not None]
            assert len(winners) == 1, results
            assert winners[0].fencing_token == round_number + 1, (
                "exactly one increment per takeover"
            )


@pytest.mark.skipif(not POSTGRES_TEST_URL, reason="row locks are PostgreSQL-only")
def test_postgresql_a_takeover_waits_for_the_fenced_transition_it_would_overtake(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The lease expires *during* the owner's transaction, after ``assert_fenced`` passed. The takeover must wait for
    that transaction, so the transition completes under the lease it checked, and only then does the token change."""
    with _migrated("postgresql", tmp_path, monkeypatch) as (engine, _):
        owner = _acquire(engine, "node-a", ttl=1.5)
        assert owner is not None
        events: list[str] = []
        taken: list[Lease | None] = []
        done = threading.Event()

        def taker() -> None:
            with Session(engine) as session, session.begin():
                session.execute(text("SET LOCAL lock_timeout = '20s'"))
                taken.append(acquire(session, "node-b", ttl_seconds=TTL))
                events.append("takeover")
            done.set()

        session = Session(engine)
        session.begin()
        assert_fenced(session, owner)  # passes and locks the row
        time.sleep(2.0)  # the lease is now expired by the database clock
        thread = threading.Thread(target=taker, daemon=True)
        thread.start()
        time.sleep(1.0)
        assert not done.is_set(), "the takeover must wait for the fenced transaction"
        events.append("transition")
        session.commit()
        session.close()
        assert done.wait(20)
        thread.join(5)
        assert events == ["transition", "takeover"]
        assert taken[0] is not None and taken[0].fencing_token == 2
        _lost(lambda: _fenced(engine, owner))


@pytest.mark.skipif(not POSTGRES_TEST_URL, reason="row locks are PostgreSQL-only")
def test_postgresql_concurrent_renewals_of_the_same_lease_all_keep_the_token(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with _migrated("postgresql", tmp_path, monkeypatch) as (engine, _):
        lease = _acquire(engine, "node-a")
        assert lease is not None
        results = _race(engine, [lambda s: renew(s, lease, ttl_seconds=TTL) for _ in range(6)])
        assert {r.fencing_token for r in results} == {1}


# ---------------------------------------------------------------- migration


@pytest.mark.parametrize("dialect", DIALECTS)
def test_the_migration_creates_the_table_and_rolls_back_cleanly(
    dialect: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with _migrated(dialect, tmp_path, monkeypatch) as (engine, config):
        columns = {c["name"] for c in inspect(engine).get_columns("caliber_execution_owner")}
        assert columns == {
            "lease_name",
            "owner_id",
            "fencing_token",
            "expires_at",
            "acquired_at",
            "renewed_at",
        }
        assert _acquire(engine, "node-a") is not None
        command.downgrade(config, "0115")
        assert "caliber_execution_owner" not in inspect(engine).get_table_names()
        command.upgrade(config, "head")
        assert _stored(engine) is None, "a re-applied migration starts with no lease held"
