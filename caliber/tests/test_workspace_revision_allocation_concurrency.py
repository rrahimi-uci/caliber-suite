"""`P4-A`'s cross-dialect concurrent-allocation gap.

``workspace_revisions.py::allocate_revision_number`` is the single counter
primitive both the `push`-import materializer
(``workspace_import_materializer.py``) and the managed-snapshot route
(``routes/workspace.py::_create_snapshot_sync``, `P4-B`/`P4-C`) use to hand
out project revision numbers. Its own docstring explains *why* it is a
read-then-conditional-update compare-and-set loop rather than
``SELECT ... FOR UPDATE``/``UPDATE ... RETURNING`` (portability across
SQLite and PostgreSQL), but until this file, nothing actually raced real
concurrent callers against it -- every existing test
(``test_workspace_revision_foundation.py``) either calls it sequentially
from a single session or replaces the session with a hand-written fake that
always reports ``rowcount=0`` to exercise the *bounded-retries* failure
path, never a fake or real *contended* success path.

This module closes that gap the same way `P1-A` closed its own
migration-parity one: a real multi-threaded, multi-session race is run
unconditionally against SQLite, and the identical scenario is parametrized
(``dialect_helpers.DIALECTS``) to also run against a real, ephemeral
PostgreSQL server in the "Migration parity (PostgreSQL)" CI job
(``.github/workflows/ci.yml``) when ``CALIBER_TEST_POSTGRES_URL`` is set --
skipped everywhere else, exactly like that job's other two dialect-parametrized
cases in ``test_migrations.py``.
"""

from __future__ import annotations

from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from pathlib import Path

import pytest
from sqlalchemy import create_engine
from sqlalchemy.engine import Engine
from sqlalchemy.orm import sessionmaker

from caliber.db import Base
from caliber.db.models import CaliberProject
from caliber.workspace_revisions import RevisionAllocationError, allocate_revision_number
from tests.dialect_helpers import DIALECTS, dialect_database_url

PROJECT_ID = "PRJ-revision-concurrency"

# High enough to reliably provoke compare-and-set contention (empirically,
# collisions start appearing well under this on both dialects), low enough
# to keep the case fast and to stay comfortably within
# ``allocate_revision_number``'s default ``max_attempts=8`` per contending
# caller.
WORKER_COUNT = 16


@contextmanager
def _dialect_engine(dialect: str, tmp_path: Path) -> Iterator[Engine]:
    """A schema-initialized engine for ``dialect``, torn down on exit.

    Mirrors ``db/session.py::create_engine_from_config``'s SQLite
    ``connect_args`` exactly (``check_same_thread=False`` -- required for
    cross-thread use -- and a generous busy timeout) since that is what
    production and every other test actually run against; a bare
    ``create_engine(url)`` would raise ``ProgrammingError`` the moment a
    second thread touched the same SQLite connection.
    """
    with dialect_database_url(dialect, tmp_path, "revision_concurrency") as db_url:
        connect_args: dict[str, object] = (
            {"check_same_thread": False, "timeout": 30} if dialect == "sqlite" else {}
        )
        engine = create_engine(db_url, connect_args=connect_args)
        try:
            Base.metadata.create_all(engine)
            yield engine
        finally:
            Base.metadata.drop_all(engine)
            engine.dispose()


@pytest.mark.parametrize("dialect", DIALECTS)
def test_allocate_revision_number_is_race_free_under_concurrent_sessions(
    dialect: str, tmp_path: Path
) -> None:
    """``WORKER_COUNT`` threads, each its own session, race one project's counter.

    No coordination beyond the allocator's own compare-and-set retry loop:
    every thread opens a fresh session from a shared ``sessionmaker`` (exactly
    how ``run_in_threadpool`` hands each request its own session in
    production) and calls ``allocate_revision_number`` for the *same*
    ``project_id`` at (as close to) the same time as the GIL and the
    threadpool scheduler allow. The allocator must still hand out
    ``1..WORKER_COUNT`` with no duplicate and no gap, and the project's
    counter must land exactly on ``WORKER_COUNT + 1`` -- proving the CAS loop
    is race-free under real contention, not just under the single-session
    sequential calls the rest of the suite exercises.
    """
    with _dialect_engine(dialect, tmp_path) as engine:
        factory = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)

        with factory() as setup_session:
            setup_session.add(CaliberProject(project_id=PROJECT_ID, name="Revision concurrency"))
            setup_session.commit()

        def _allocate_and_commit(_: int) -> int:
            with factory() as session:
                number = allocate_revision_number(session, PROJECT_ID)
                session.commit()
                return number

        with ThreadPoolExecutor(max_workers=WORKER_COUNT) as pool:
            results = list(pool.map(_allocate_and_commit, range(WORKER_COUNT)))

        assert sorted(results) == list(range(1, WORKER_COUNT + 1)), (
            f"expected exactly {list(range(1, WORKER_COUNT + 1))} with no duplicate/skip, "
            f"got {sorted(results)}"
        )

        with factory() as session:
            project = session.get(CaliberProject, PROJECT_ID)
            assert project is not None
            assert project.next_revision_number == WORKER_COUNT + 1


@pytest.mark.parametrize("dialect", DIALECTS)
def test_allocate_revision_number_keeps_failing_closed_under_concurrent_sessions(
    dialect: str, tmp_path: Path
) -> None:
    """The bounded-retry failure path also fails closed with real contention.

    ``test_workspace_revision_foundation.py::
    test_allocator_reports_contention_after_bounded_compare_and_set_retries``
    already proves this with a hand-written always-contended fake session;
    this is the same guarantee against a real database and real concurrent
    writers, at a low enough ``max_attempts`` that ``WORKER_COUNT`` genuine
    contenders can exhaust it. Every allocation must either succeed with a
    number in range or raise ``RevisionAllocationError`` -- never return a
    wrong or duplicate number, and never leave the counter attempted-but-
    unmoved.
    """
    with _dialect_engine(dialect, tmp_path) as engine:
        factory = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)

        with factory() as setup_session:
            setup_session.add(CaliberProject(project_id=PROJECT_ID, name="Revision concurrency"))
            setup_session.commit()

        def _allocate(_: int) -> int | None:
            with factory() as session:
                try:
                    number = allocate_revision_number(session, PROJECT_ID, max_attempts=1)
                except RevisionAllocationError:
                    session.rollback()
                    return None
                session.commit()
                return number

        with ThreadPoolExecutor(max_workers=WORKER_COUNT) as pool:
            results = list(pool.map(_allocate, range(WORKER_COUNT)))

        succeeded = [number for number in results if number is not None]
        assert len(succeeded) == len(set(succeeded)), f"duplicate revision numbers: {succeeded}"
        assert all(1 <= number <= WORKER_COUNT for number in succeeded)

        with factory() as session:
            project = session.get(CaliberProject, PROJECT_ID)
            assert project is not None
            # Every successful allocation advanced the counter by exactly one and
            # nothing else did -- a failed attempt (rowcount == 0) never commits a
            # partial/half-applied increment.
            assert project.next_revision_number == len(succeeded) + 1
