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
from sqlalchemy import create_engine, inspect, text

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

    incident_indexes = {
        index["name"]: index for index in inspector.get_indexes("caliber_incidents")
    }
    open_index = incident_indexes["uq_caliber_incidents_open_objective"]
    assert bool(open_index["unique"])
    assert "status = 'open'" in str(open_index["dialect_options"]["sqlite_where"])

    engine.dispose()
    # Clean up the env var the monkeypatch set, just in case parallel tests share state.
    os.environ.pop("CALIBER_DATABASE_URL", None)


@pytest.mark.slow
def test_open_incident_index_migration_refuses_ambiguous_history(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """0079 must not silently choose which duplicate open incident is authoritative."""
    db_path = tmp_path / "duplicate_incidents.db"
    db_url = f"sqlite:///{db_path}"
    monkeypatch.setenv("CALIBER_DATABASE_URL", db_url)
    monkeypatch.chdir(PROJECT_ROOT)
    cfg = Config(str(ALEMBIC_INI))
    command.upgrade(cfg, "0078")

    engine = create_engine(db_url)
    try:
        with engine.begin() as connection:
            connection.execute(
                text(
                    """
                    INSERT INTO caliber_incidents
                        (incident_id, objective, signal, severity, status, detail, opened_at)
                    VALUES
                        (:first_id, :objective, 'latency', 'warning', 'open', '', CURRENT_TIMESTAMP),
                        (:second_id, :objective, 'latency', 'critical', 'open', '', CURRENT_TIMESTAMP)
                    """
                ),
                {
                    "first_id": "INC-duplicate-1",
                    "second_id": "INC-duplicate-2",
                    "objective": "latency<=1",
                },
            )

        with pytest.raises(RuntimeError, match="resolve duplicate open incidents first"):
            command.upgrade(cfg, "head")

        inspector = inspect(engine)
        assert "uq_caliber_incidents_open_objective" not in {
            index["name"] for index in inspector.get_indexes("caliber_incidents")
        }
        with engine.connect() as connection:
            assert (
                connection.execute(
                    text("SELECT version_num FROM caliber_alembic_version")
                ).scalar_one()
                == "0078"
            )
    finally:
        engine.dispose()


@pytest.mark.slow
def test_resolution_notification_migration_marks_history_handled_without_replay(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """0080 must reserve NULL for new pending all-clears, not legacy history."""
    db_path = tmp_path / "legacy_incident_notifications.db"
    db_url = f"sqlite:///{db_path}"
    monkeypatch.setenv("CALIBER_DATABASE_URL", db_url)
    monkeypatch.chdir(PROJECT_ROOT)
    cfg = Config(str(ALEMBIC_INI))
    command.upgrade(cfg, "0079")

    engine = create_engine(db_url)
    try:
        with engine.begin() as connection:
            connection.execute(
                text(
                    """
                    INSERT INTO caliber_incidents
                        (incident_id, objective, signal, severity, status, detail,
                         opened_at, resolved_at)
                    VALUES
                        ('INC-resolved-timestamp', 'latency<=1', 'latency', 'warning',
                         'resolved', '', '2026-01-01 00:00:00', '2026-01-01 00:05:00'),
                        ('INC-resolved-fallback', 'errors<=1', 'errors', 'warning',
                         'resolved', '', '2026-01-02 00:00:00', NULL),
                        ('INC-still-open', 'queue<=1', 'queue', 'warning',
                         'open', '', '2026-01-03 00:00:00', NULL)
                    """
                )
            )

        command.upgrade(cfg, "0080")

        with engine.connect() as connection:
            rows = {
                row.incident_id: row
                for row in connection.execute(
                    text(
                        """
                        SELECT incident_id, opened_at, resolved_at, resolved_notified_at
                        FROM caliber_incidents
                        ORDER BY incident_id
                        """
                    )
                )
            }
            version = connection.execute(
                text("SELECT version_num FROM caliber_alembic_version")
            ).scalar_one()

        assert version == "0080"
        assert (
            rows["INC-resolved-timestamp"].resolved_notified_at
            == rows["INC-resolved-timestamp"].resolved_at
        )
        assert (
            rows["INC-resolved-fallback"].resolved_notified_at
            == rows["INC-resolved-fallback"].opened_at
        )
        assert rows["INC-still-open"].resolved_notified_at is None
    finally:
        engine.dispose()


@pytest.mark.slow
def test_calibration_revision_migration_invalidates_unversioned_current_evidence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A pre-0078 result cannot be declared current merely by assigning revision one."""
    db_path = tmp_path / "legacy_calibration.db"
    db_url = f"sqlite:///{db_path}"
    monkeypatch.setenv("CALIBER_DATABASE_URL", db_url)
    monkeypatch.chdir(PROJECT_ROOT)
    cfg = Config(str(ALEMBIC_INI))
    command.upgrade(cfg, "0077")

    engine = create_engine(db_url)
    try:
        with engine.begin() as connection:
            connection.execute(
                text(
                    """
                    INSERT INTO caliber_tool_registry
                        (tool_id, name, version, module_path, callable_name, last_calibration)
                    VALUES
                        ('TL-legacy', 'legacy_calibration', '1.0', 'pkg.mod', 'run',
                         '{"pass_rate": 1.0}')
                    """
                )
            )

        command.upgrade(cfg, "0078")

        with engine.connect() as connection:
            row = connection.execute(
                text(
                    """
                    SELECT calibration_revision, last_calibration
                    FROM caliber_tool_registry
                    WHERE tool_id = 'TL-legacy'
                    """
                )
            ).one()
        assert row.calibration_revision == 1
        assert row.last_calibration is None
    finally:
        engine.dispose()


@pytest.mark.slow
def test_skill_snapshot_migration_backfills_only_missing_current_versions(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """0081 anchors live skill state without duplicating existing history."""
    db_path = tmp_path / "legacy_skill_versions.db"
    db_url = f"sqlite:///{db_path}"
    monkeypatch.setenv("CALIBER_DATABASE_URL", db_url)
    monkeypatch.chdir(PROJECT_ROOT)
    cfg = Config(str(ALEMBIC_INI))
    command.upgrade(cfg, "0080")

    engine = create_engine(db_url)
    try:
        with engine.begin() as connection:
            connection.execute(
                text(
                    """
                    INSERT INTO caliber_skills
                        (skill_id, name, summary, content, owner, tags, version)
                    VALUES
                        ('SK-missing', 'missing-history', 'Live summary', 'Live content',
                         '@owner', '[]', 4),
                        ('SK-present', 'present-history', 'Known summary', 'Known content',
                         '@owner', '[]', 2),
                        ('SK-mismatch', 'reused-version', 'Live D', 'Content D',
                         '@owner', '[]', 2),
                        ('SK-stale', 'stale-live-version', 'Summary A', 'Content A',
                         '@owner', '[]', 1)
                    """
                )
            )
            connection.execute(
                text(
                    """
                    INSERT INTO caliber_skill_versions
                        (skill_version_id, skill_id, version_number, content, summary, created_by)
                    VALUES
                        ('SKV-present', 'SK-present', 2, 'Known content', 'Known summary',
                         '@owner'),
                        ('SKV-mismatch-v2', 'SK-mismatch', 2, 'Content C', 'Summary C',
                         '@owner'),
                        ('SKV-stale-v1', 'SK-stale', 1, 'Content A', 'Summary A', '@owner'),
                        ('SKV-stale-v2', 'SK-stale', 2, 'Content B', 'Summary B', '@owner')
                    """
                )
            )

        command.upgrade(cfg, "0081")

        with engine.connect() as connection:
            rows = (
                connection.execute(
                    text(
                        """
                    SELECT skill_id, version_number, content, summary, created_by
                    FROM caliber_skill_versions
                    WHERE skill_id IN ('SK-missing', 'SK-present', 'SK-mismatch', 'SK-stale')
                    ORDER BY skill_id, version_number
                    """
                    )
                )
                .mappings()
                .all()
            )
            version = connection.execute(
                text("SELECT version_num FROM caliber_alembic_version")
            ).scalar_one()
            live_versions = dict(
                connection.execute(
                    text(
                        """
                        SELECT skill_id, version
                        FROM caliber_skills
                        WHERE skill_id IN ('SK-missing', 'SK-present', 'SK-mismatch', 'SK-stale')
                        """
                    )
                ).all()
            )

        assert version == "0081"
        assert [dict(row) for row in rows] == [
            {
                "skill_id": "SK-mismatch",
                "version_number": 2,
                "content": "Content C",
                "summary": "Summary C",
                "created_by": "@owner",
            },
            {
                "skill_id": "SK-mismatch",
                "version_number": 3,
                "content": "Content D",
                "summary": "Live D",
                "created_by": "migration:0081",
            },
            {
                "skill_id": "SK-missing",
                "version_number": 4,
                "content": "Live content",
                "summary": "Live summary",
                "created_by": "migration:0081",
            },
            {
                "skill_id": "SK-present",
                "version_number": 2,
                "content": "Known content",
                "summary": "Known summary",
                "created_by": "@owner",
            },
            {
                "skill_id": "SK-stale",
                "version_number": 1,
                "content": "Content A",
                "summary": "Summary A",
                "created_by": "@owner",
            },
            {
                "skill_id": "SK-stale",
                "version_number": 2,
                "content": "Content B",
                "summary": "Summary B",
                "created_by": "@owner",
            },
            {
                "skill_id": "SK-stale",
                "version_number": 3,
                "content": "Content A",
                "summary": "Summary A",
                "created_by": "migration:0081",
            },
        ]
        assert live_versions == {
            "SK-mismatch": 3,
            "SK-missing": 4,
            "SK-present": 2,
            "SK-stale": 3,
        }
    finally:
        engine.dispose()
