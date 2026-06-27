"""Migration sanity test.

Runs ``alembic upgrade head`` against a fresh DB and asserts that the resulting
schema matches what ``Base.metadata`` would produce via ``create_all``. This
catches drift between the ORM models and the migration files — a class of
bug that's silent in normal development and painful in production.

This is the only test that exercises Alembic; route tests use ``create_all``
directly for speed.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, inspect

from caliber.db import Base

PROJECT_ROOT = Path(__file__).resolve().parent.parent
ALEMBIC_INI = PROJECT_ROOT / "alembic.ini"


@pytest.mark.slow
def test_alembic_upgrade_head_matches_metadata(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db_path = tmp_path / "alembic_test.db"
    db_url = f"sqlite:///{db_path}"
    monkeypatch.setenv("CALIBER_DATABASE_URL", db_url)

    cfg = Config(str(ALEMBIC_INI))
    # alembic.ini paths are relative to the ini file; make sure CWD matches so
    # script_location = src/caliber/db/migrations resolves.
    monkeypatch.chdir(PROJECT_ROOT)
    command.upgrade(cfg, "head")

    # Now compare the tables created by the migration to the model metadata.
    # Caliber's alembic version table is namespaced (``caliber_alembic_version``)
    # so it can coexist with MLflow's default ``alembic_version`` on the shared
    # production backend store — exclude both names here for symmetry with how
    # migrations are run in production.
    engine = create_engine(db_url)
    inspector = inspect(engine)
    db_tables = set(inspector.get_table_names()) - {"alembic_version", "caliber_alembic_version"}
    model_tables = set(Base.metadata.tables.keys())

    assert db_tables == model_tables, (
        f"migration ↔ model drift: only in DB: {db_tables - model_tables}, "
        f"only in models: {model_tables - db_tables}"
    )

    # Spot-check column presence for each table — full type comparison is
    # brittle across SQLAlchemy versions, but column names should match.
    for table_name in model_tables:
        db_columns = {col["name"] for col in inspector.get_columns(table_name)}
        model_columns = {col.name for col in Base.metadata.tables[table_name].columns}
        assert db_columns == model_columns, (
            f"column drift in {table_name}: "
            f"only in DB: {db_columns - model_columns}, "
            f"only in models: {model_columns - db_columns}"
        )

    engine.dispose()
    # Clean up the env var the monkeypatch set, just in case parallel tests share state.
    os.environ.pop("CALIBER_DATABASE_URL", None)
