"""Engine and session lifecycle for the CALIBER plugin.

The MLflow server hosts CALIBER in the same process as MLflow itself; we keep
one engine per ``create_app`` invocation, stored on ``app.state.engine``.
Route handlers obtain a session via :func:`get_session_factory` which is also
parked on ``app.state``. Tests get their own engine via fixtures in
``tests/conftest.py``.

Sync SQLAlchemy is used throughout — Starlette tolerates both sync and async
route handlers, and MLflow's own ORM code is sync, so staying consistent keeps
the codebase smaller.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager

from sqlalchemy import Engine, create_engine
from sqlalchemy.orm import Session, sessionmaker

from caliber.config import CaliberConfig
from caliber.db_url import normalize_database_url


def create_engine_from_config(config: CaliberConfig) -> Engine:
    """Build a SQLAlchemy engine from the CALIBER configuration.

    A few engine kwargs are set explicitly because they pay for themselves at
    production scale:

    * ``future=True`` (default in 2.0) — opt into 2.0-style queries.
    * ``pool_pre_ping=True`` — survives stale connections after DB restarts.
    * ``connect_args={"check_same_thread": False}`` for SQLite — required when
      the engine is used from multiple worker threads (Starlette runs sync
      handlers in a threadpool). No-op on non-SQLite backends.
    """
    connect_args: dict[str, object] = {}
    if config.database_url.startswith("sqlite"):
        connect_args["check_same_thread"] = False
        # SQLite serializes writes; under contention (worker + poller +
        # janitor + a request handler all racing to write) the default
        # 5s busy-timeout still triggers spurious ``database table is
        # locked`` errors in CI. 30s costs nothing on a quiet system
        # and gives the background tasks plenty of room to back off.
        #
        # Concurrent reader/writer fan-out is handled by setting
        # ``journal_mode=WAL`` at bootstrap time in
        # ``scripts/run-dev.sh`` rather than via a per-connection
        # pragma here — once a SQLite file is in WAL mode the setting
        # is persistent across restarts, and ``PRAGMA journal_mode=WAL``
        # issued after MLflow already holds the file open silently
        # no-ops. Tests run in delete-mode (the SQLite default) which
        # matches their assumption that writes from one connection are
        # immediately visible to a SELECT from another.
        connect_args["timeout"] = 30

    return create_engine(
        normalize_database_url(config.database_url),
        pool_pre_ping=True,
        connect_args=connect_args,
    )


def sessionmaker_from_engine(engine: Engine) -> sessionmaker[Session]:
    """Return a configured sessionmaker bound to the engine.

    ``expire_on_commit=False`` lets route handlers return ORM objects after a
    commit without the attributes being lazily reloaded — which would fail
    once the session is closed.
    """
    return sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)


@contextmanager
def session_scope(factory: sessionmaker[Session]) -> Iterator[Session]:
    """Open a session, commit on success, rollback on exception, always close.

    The standard "unit of work" pattern. Use this in background tasks (like the
    feedback poller) where the request-scoped session dependency isn't available.
    """
    session = factory()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()
