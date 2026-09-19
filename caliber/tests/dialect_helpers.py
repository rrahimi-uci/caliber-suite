"""Shared SQLite/PostgreSQL dialect-parametrization scaffolding for tests.

Not collected by pytest (no ``test_`` prefix), mirroring ``workflow_helpers.py``.

Originally lived only in ``test_migrations.py`` (`P1-A`'s
``migration-parity-postgres`` CI job, ``.github/workflows/ci.yml``). Extracted
here so a second test module -- currently
``test_workspace_revision_allocation_concurrency.py`` (`P4-A`'s
cross-dialect concurrent-allocation gap) -- can run the same
``DIALECTS``-parametrized pattern against a real PostgreSQL server without
duplicating the throwaway-database bookkeeping. Any test module using this
must also be added to the ``migration-parity-postgres`` job's ``pytest``
invocation (both ``.github/workflows/ci.yml`` and
``scripts/ci-local.sh::job_migration_parity_postgres``) or its
``postgresql``-parametrized cases will simply never run.
"""

from __future__ import annotations

import os
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url

from caliber.db_url import normalize_database_url

# Unset (empty string) everywhere except the dedicated CI job, so every
# PostgreSQL-parametrized case below is skipped by default rather than
# failing on a socket nobody offered.
POSTGRES_TEST_URL = os.environ.get("CALIBER_TEST_POSTGRES_URL", "").strip()

DIALECTS = [
    pytest.param("sqlite", id="sqlite"),
    pytest.param(
        "postgresql",
        id="postgresql",
        marks=pytest.mark.skipif(
            not POSTGRES_TEST_URL,
            reason="CALIBER_TEST_POSTGRES_URL is not set (see the "
            "'Migration parity (PostgreSQL)' CI job)",
        ),
    ),
]


@contextmanager
def dialect_database_url(dialect: str, tmp_path: Path, name: str) -> Iterator[str]:
    """Yield an isolated, empty database URL for ``dialect``.

    SQLite: a fresh file under ``tmp_path`` -- this file's established
    pattern, unchanged. PostgreSQL: a throwaway database created on the
    shared CI service (``CALIBER_TEST_POSTGRES_URL`` is an *admin* connection
    string -- any reachable database on that server, used only to run
    ``CREATE DATABASE``/``DROP DATABASE``) and dropped again once the test
    finishes, so multiple PostgreSQL-parametrized tests sharing one live
    server in the same CI job never collide.
    """
    if dialect == "sqlite":
        yield f"sqlite:///{tmp_path / f'{name}.db'}"
        return

    assert dialect == "postgresql"
    admin_url = normalize_database_url(POSTGRES_TEST_URL)
    db_name = f"caliber_test_{name}_{uuid.uuid4().hex[:12]}"
    admin_engine = create_engine(admin_url, isolation_level="AUTOCOMMIT")
    try:
        with admin_engine.connect() as connection:
            connection.execute(text(f'CREATE DATABASE "{db_name}"'))
        try:
            # str(URL) masks the password (renders "***"); this URL is used to
            # open a real connection, so the password must round-trip intact.
            yield make_url(admin_url).set(database=db_name).render_as_string(hide_password=False)
        finally:
            with admin_engine.connect() as connection:
                connection.execute(
                    text(
                        "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                        "WHERE datname = :name AND pid <> pg_backend_pid()"
                    ),
                    {"name": db_name},
                )
                connection.execute(text(f'DROP DATABASE IF EXISTS "{db_name}"'))
    finally:
        admin_engine.dispose()
