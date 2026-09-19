"""Migration sanity test.

Runs ``alembic upgrade head`` against a fresh DB and asserts that the resulting
schema matches what ``Base.metadata`` would produce via ``create_all``. This
catches drift between the ORM models and the migration files — a class of
bug that's silent in normal development and painful in production.

This is the only test that exercises Alembic; route tests use ``create_all``
directly for speed.

Dialect coverage (``docs/workspace-plan.md`` section 15.2): every test here
runs against SQLite unconditionally. Two tests additionally run in
``DIALECTS``-parametrized form -- ``test_alembic_upgrade_head_matches_metadata``
(fresh-install + full upgrade chain + ORM-metadata parity) and
``test_0093_backfills_slug_source_mode_and_four_environments_per_project``
(upgrade-from-preceding-revision with real data, `P1-A`'s own migration) --
also against a real PostgreSQL server when ``CALIBER_TEST_POSTGRES_URL`` is
set. That env var is set only by the "Migration parity (PostgreSQL)" CI job
(``.github/workflows/ci.yml``), which is the one deliberate, narrowly-scoped
exception to this repo's otherwise-offline test policy: every other test in
this file, and every test outside this file, stays SQLite/offline.

The ``DIALECTS``/``dialect_database_url`` scaffolding itself now lives in
``dialect_helpers.py`` (shared with
``test_workspace_revision_allocation_concurrency.py``, `P4-A`'s
cross-dialect concurrent-allocation coverage) rather than being private to
this file.
"""

from __future__ import annotations

import os
import warnings
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.exc import IntegrityError, SAWarning

from caliber.db import Base
from tests.dialect_helpers import DIALECTS, dialect_database_url

PROJECT_ROOT = Path(__file__).resolve().parent.parent
ALEMBIC_INI = PROJECT_ROOT / "alembic.ini"


# Columns a migration adds via raw, dialect-gated DDL (``op.execute``, not
# ``op.add_column`` with a typed ``sa.Column``) rather than through the ORM,
# so they deliberately never appear in ``Base.metadata`` and would otherwise
# look like drift. Currently just ``0060``'s Postgres-only pgvector column
# (its own docstring: "Postgres-only ... On SQLite this whole migration is a
# no-op"; ``caliber.knowledge.pgvector_ann`` reads/writes it via raw SQL by
# design). Reflecting it also emits an ``SAWarning`` ("did not recognize type
# 'vector'") since SQLAlchemy has no built-in mapping for the pgvector type;
# that warning is expected and suppressed alongside the exclusion below.
_UNMANAGED_COLUMNS: dict[str, set[str]] = {
    "caliber_knowledge_base_chunks": {"embedding_vec"},
}


@pytest.mark.slow
@pytest.mark.parametrize("dialect", DIALECTS)
def test_alembic_upgrade_head_matches_metadata(
    dialect: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with dialect_database_url(dialect, tmp_path, "alembic_test") as db_url:
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
        db_tables = set(inspector.get_table_names()) - {
            "alembic_version",
            "caliber_alembic_version",
        }
        model_tables = set(Base.metadata.tables.keys())

        assert db_tables == model_tables, (
            f"migration ↔ model drift ({dialect}): only in DB: {db_tables - model_tables}, "
            f"only in models: {model_tables - db_tables}"
        )

        # Spot-check column presence for each table — full type comparison is
        # brittle across SQLAlchemy versions, but column names should match.
        for table_name in model_tables:
            with warnings.catch_warnings():
                warnings.simplefilter("ignore", category=SAWarning)
                db_columns = {col["name"] for col in inspector.get_columns(table_name)}
            db_columns -= _UNMANAGED_COLUMNS.get(table_name, set())
            model_columns = {col.name for col in Base.metadata.tables[table_name].columns}
            assert db_columns == model_columns, (
                f"column drift in {table_name} ({dialect}): "
                f"only in DB: {db_columns - model_columns}, "
                f"only in models: {model_columns - db_columns}"
            )

        incident_indexes = {
            index["name"]: index for index in inspector.get_indexes("caliber_incidents")
        }
        open_index = incident_indexes["uq_caliber_incidents_open_objective"]
        assert bool(open_index["unique"])
        # Each dialect reflects its own partial-index predicate key
        # (models.py declares both `sqlite_where` and `postgresql_where`
        # identically on this index).
        where_key = "sqlite_where" if dialect == "sqlite" else "postgresql_where"
        where_clause = str(open_index["dialect_options"][where_key])
        if dialect == "sqlite":
            assert "status = 'open'" in where_clause
        else:
            # PostgreSQL reflects the partial-index predicate back with
            # explicit type casts (e.g. ``(status)::text = 'open'::text``)
            # rather than the literal source text SQLite preserves.
            assert "status" in where_clause and "'open'" in where_clause

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


@pytest.mark.slow
@pytest.mark.parametrize("dialect", DIALECTS)
def test_0093_backfills_slug_source_mode_and_four_environments_per_project(
    dialect: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`P1-A`'s backfill: every pre-existing project gets a derived,
    tenant-unique slug, a default source mode, and its four fixed
    environment rows -- additively, with no data loss. Two projects whose
    names slugify identically ("Demo" / "demo!") must not collide."""
    with dialect_database_url(dialect, tmp_path, "workspace_environments") as db_url:
        monkeypatch.setenv("CALIBER_DATABASE_URL", db_url)
        monkeypatch.chdir(PROJECT_ROOT)
        cfg = Config(str(ALEMBIC_INI))
        command.upgrade(cfg, "0092")

        engine = create_engine(db_url)
        try:
            with engine.begin() as connection:
                connection.execute(
                    text(
                        "INSERT INTO caliber_projects (project_id, tenant_id, name, owner) VALUES "
                        "(:id1, 'local', 'Demo', 'alice'), "
                        "(:id2, 'local', 'demo!', 'bob')"
                    ),
                    {"id1": "PRJ-demo-1", "id2": "PRJ-demo-2"},
                )

            command.upgrade(cfg, "0093")

            with engine.connect() as connection:
                projects = {
                    row.project_id: (row.slug, row.source_mode)
                    for row in connection.execute(
                        text("SELECT project_id, slug, source_mode FROM caliber_projects")
                    )
                }
                assert projects == {
                    "PRJ-demo-1": ("demo", "caliber_managed"),
                    "PRJ-demo-2": ("demo-2", "caliber_managed"),
                }

                for project_id in ("PRJ-demo-1", "PRJ-demo-2"):
                    environments = connection.execute(
                        text(
                            "SELECT name, environment_class, promotion_order, status "
                            "FROM caliber_workspace_environments "
                            "WHERE project_id = :project_id ORDER BY promotion_order"
                        ),
                        {"project_id": project_id},
                    ).fetchall()
                    assert [tuple(row) for row in environments] == [
                        ("dev", "development", 10, "active"),
                        ("qa", "qa", 20, "disabled"),
                        ("staging", "staging", 30, "disabled"),
                        ("prod", "production", 40, "disabled"),
                    ]
        finally:
            engine.dispose()
            os.environ.pop("CALIBER_DATABASE_URL", None)


#: `0095` (Phase 2 item 3): every index that migration adds, keyed by the
#: table it lands on -- the same pairs the migration module itself declares,
#: duplicated here (not imported) so this test still catches a mismatch if
#: the migration's own lists ever drift from what actually got created.
_0095_VISIBILITY_INDEXES = {
    "caliber_agent_config": "ix_agent_config_project_visibility",
    "caliber_aria_plans": "ix_aria_plans_project_visibility",
    "caliber_eval_datasets": "ix_eval_datasets_project_visibility",
    "caliber_eval_runs": "ix_eval_runs_project_visibility",
    "caliber_judges": "ix_judges_project_visibility",
    "caliber_llm_model_pricing": "ix_llm_model_pricing_project_visibility",
    "caliber_openapi_integrations": "ix_openapi_integrations_project_visibility",
    "caliber_review_queues": "ix_review_queues_project_visibility",
    "caliber_skills": "ix_skills_project_visibility",
    "caliber_tool_registry": "ix_tool_registry_project_visibility",
    "caliber_workflows": "ix_workflows_project_visibility",
}
_0095_PROJECT_ONLY_INDEXES = {
    "caliber_workflow_runs": "ix_workflow_runs_project",
    "caliber_workflow_run_events": "ix_workflow_run_events_project",
    "caliber_workflow_run_checkpoints": "ix_workflow_run_checkpoints_project",
    "caliber_workflow_files": "ix_workflow_files_project",
}


@pytest.mark.slow
def test_0095_adds_project_scoping_indexes_on_every_target_table(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A real `alembic upgrade head` run, not just `Base.metadata` --
    proves the migration itself creates every declared index, on every
    declared table, with a `project_id`-leading column list (so the
    project-scoped lookup it exists for can actually use it)."""
    db_path = tmp_path / "alembic_0095_test.db"
    db_url = f"sqlite:///{db_path}"
    monkeypatch.setenv("CALIBER_DATABASE_URL", db_url)

    cfg = Config(str(ALEMBIC_INI))
    monkeypatch.chdir(PROJECT_ROOT)
    command.upgrade(cfg, "head")

    engine = create_engine(db_url)
    try:
        inspector = inspect(engine)
        for table, index_name in _0095_VISIBILITY_INDEXES.items():
            indexes = {ix["name"]: ix for ix in inspector.get_indexes(table)}
            assert index_name in indexes, f"{table} is missing {index_name}"
            assert indexes[index_name]["column_names"] == ["project_id", "visibility"]
        for table, index_name in _0095_PROJECT_ONLY_INDEXES.items():
            indexes = {ix["name"]: ix for ix in inspector.get_indexes(table)}
            assert index_name in indexes, f"{table} is missing {index_name}"
            assert indexes[index_name]["column_names"] == ["project_id"]
    finally:
        engine.dispose()
        os.environ.pop("CALIBER_DATABASE_URL", None)


@pytest.mark.slow
def test_0096_migrates_legacy_mcp_servers_to_private_visibility(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Existing MCP rows gain the safe private default without a guessed project."""
    db_path = tmp_path / "alembic_0096_test.db"
    db_url = f"sqlite:///{db_path}"
    monkeypatch.setenv("CALIBER_DATABASE_URL", db_url)

    cfg = Config(str(ALEMBIC_INI))
    monkeypatch.chdir(PROJECT_ROOT)
    command.upgrade(cfg, "0095")

    engine = create_engine(db_url)
    try:
        with engine.begin() as connection:
            connection.execute(
                text(
                    "INSERT INTO caliber_mcp_servers (server_id, name, owner) "
                    "VALUES ('MCP-legacy', 'Legacy', '@legacy')"
                )
            )

        command.upgrade(cfg, "head")

        inspector = inspect(engine)
        indexes = {ix["name"]: ix for ix in inspector.get_indexes("caliber_mcp_servers")}
        assert indexes["ix_mcp_servers_project_visibility"]["column_names"] == [
            "project_id",
            "visibility",
        ]
        with engine.connect() as connection:
            row = connection.execute(
                text(
                    "SELECT project_id, visibility, owner "
                    "FROM caliber_mcp_servers WHERE server_id = 'MCP-legacy'"
                )
            ).one()
            assert tuple(row) == (None, "user", "@legacy")
    finally:
        engine.dispose()
        os.environ.pop("CALIBER_DATABASE_URL", None)


@pytest.mark.slow
def test_0099_backfills_workspace_import_attempt_budget(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Existing import jobs receive the safe total-attempt defaults."""
    db_path = tmp_path / "workspace_import_attempt_budget.db"
    db_url = f"sqlite:///{db_path}"
    monkeypatch.setenv("CALIBER_DATABASE_URL", db_url)

    cfg = Config(str(ALEMBIC_INI))
    monkeypatch.chdir(PROJECT_ROOT)
    command.upgrade(cfg, "0098")

    engine = create_engine(db_url)
    try:
        with engine.begin() as connection:
            connection.execute(
                text(
                    "INSERT INTO caliber_projects (project_id, tenant_id, name, owner) "
                    "VALUES ('PRJ-0099', 'local', 'Import budget', '@owner')"
                )
            )
            connection.execute(
                text(
                    "INSERT INTO caliber_workspace_sources "
                    "(source_id, project_id, provider, provider_host, "
                    "canonical_repository_id, display_path) VALUES "
                    "('WSS-0099', 'PRJ-0099', 'github', 'github.com', 'repo-0099', "
                    "'owner/repo')"
                )
            )
            connection.execute(
                text(
                    "INSERT INTO caliber_workspace_import_jobs "
                    "(import_job_id, project_id, source_id, repository, commit_sha, "
                    "idempotency_key) VALUES "
                    "('WSI-0099', 'PRJ-0099', 'WSS-0099', 'owner/repo', :commit_sha, "
                    "'import-0099')"
                ),
                {"commit_sha": "a" * 40},
            )

        command.upgrade(cfg, "head")

        with engine.connect() as connection:
            row = connection.execute(
                text(
                    "SELECT attempt_count, max_attempts "
                    "FROM caliber_workspace_import_jobs WHERE import_job_id = 'WSI-0099'"
                )
            ).one()
            assert tuple(row) == (0, 3)
    finally:
        engine.dispose()
        os.environ.pop("CALIBER_DATABASE_URL", None)


@pytest.mark.slow
def test_0111_backfills_git_source_kind_and_enforces_the_digest_check(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Upgrade-from-0110: an existing (necessarily Git-sourced) revision row
    survives the new ``source_kind`` column unchanged, backfilled to
    ``'git'`` with both digests intact (no behavior change for the only kind
    that existed before this migration). After upgrading to head,
    ``ck_workspace_revision_source_kind_digest`` rejects a ``'git'`` row
    missing either digest, a ``'managed'`` row carrying either digest, and
    ``ck_workspace_revision_source_kind`` rejects an unknown ``source_kind``
    -- all enforced by the database itself, not only by application code. A
    genuine ``'managed'`` row (both digests NULL) is representable."""
    db_path = tmp_path / "workspace_revision_source_kind.db"
    db_url = f"sqlite:///{db_path}"
    monkeypatch.setenv("CALIBER_DATABASE_URL", db_url)

    cfg = Config(str(ALEMBIC_INI))
    monkeypatch.chdir(PROJECT_ROOT)
    command.upgrade(cfg, "0110")

    engine = create_engine(db_url)
    try:
        with engine.begin() as connection:
            connection.execute(
                text(
                    "INSERT INTO caliber_projects (project_id, tenant_id, name, owner) "
                    "VALUES ('PRJ-0111', 'local', 'Revision source kind', '@owner')"
                )
            )
            connection.execute(
                text(
                    "INSERT INTO caliber_workspace_revisions "
                    "(revision_id, project_id, revision_number, manifest, "
                    "manifest_sha256, source_bundle_sha256, revision_sha256, "
                    "status, created_by) VALUES "
                    "('WSR-0111-git', 'PRJ-0111', 1, '{}', :manifest_sha256, "
                    ":source_bundle_sha256, :revision_sha256, 'ready', '@owner')"
                ),
                {
                    "manifest_sha256": "a" * 64,
                    "source_bundle_sha256": "b" * 64,
                    "revision_sha256": "c" * 64,
                },
            )

        command.upgrade(cfg, "head")

        with engine.connect() as connection:
            row = connection.execute(
                text(
                    "SELECT source_kind, manifest_sha256, source_bundle_sha256 "
                    "FROM caliber_workspace_revisions WHERE revision_id = 'WSR-0111-git'"
                )
            ).one()
            assert tuple(row) == ("git", "a" * 64, "b" * 64)

        # A genuine managed revision (no Git commit, no source bundle) is
        # representable: both digests NULL, source_kind = 'managed'.
        with engine.begin() as connection:
            connection.execute(
                text(
                    "INSERT INTO caliber_workspace_revisions "
                    "(revision_id, project_id, revision_number, source_kind, manifest, "
                    "manifest_sha256, source_bundle_sha256, revision_sha256, "
                    "status, created_by) VALUES "
                    "('WSR-0111-managed', 'PRJ-0111', 2, 'managed', '{}', "
                    "NULL, NULL, :revision_sha256, 'ready', '@owner')"
                ),
                {"revision_sha256": "d" * 64},
            )
        with engine.connect() as connection:
            row = connection.execute(
                text(
                    "SELECT source_kind, manifest_sha256, source_bundle_sha256 "
                    "FROM caliber_workspace_revisions WHERE revision_id = 'WSR-0111-managed'"
                )
            ).one()
            assert tuple(row) == ("managed", None, None)

        # A 'git' row missing a digest violates the digest CHECK.
        with engine.begin() as connection, pytest.raises(IntegrityError):
            connection.execute(
                text(
                    "INSERT INTO caliber_workspace_revisions "
                    "(revision_id, project_id, revision_number, source_kind, manifest, "
                    "manifest_sha256, source_bundle_sha256, revision_sha256, "
                    "status, created_by) VALUES "
                    "('WSR-0111-bad-git', 'PRJ-0111', 3, 'git', '{}', "
                    "NULL, :source_bundle_sha256, :revision_sha256, 'ready', '@owner')"
                ),
                {"source_bundle_sha256": "b" * 64, "revision_sha256": "e" * 64},
            )

        # A 'managed' row carrying a digest also violates the digest CHECK.
        with engine.begin() as connection, pytest.raises(IntegrityError):
            connection.execute(
                text(
                    "INSERT INTO caliber_workspace_revisions "
                    "(revision_id, project_id, revision_number, source_kind, manifest, "
                    "manifest_sha256, source_bundle_sha256, revision_sha256, "
                    "status, created_by) VALUES "
                    "('WSR-0111-bad-managed', 'PRJ-0111', 4, 'managed', '{}', "
                    ":manifest_sha256, NULL, :revision_sha256, 'ready', '@owner')"
                ),
                {"manifest_sha256": "a" * 64, "revision_sha256": "f" * 64},
            )

        # An unknown source_kind violates the kind CHECK.
        with engine.begin() as connection, pytest.raises(IntegrityError):
            connection.execute(
                text(
                    "INSERT INTO caliber_workspace_revisions "
                    "(revision_id, project_id, revision_number, source_kind, manifest, "
                    "manifest_sha256, source_bundle_sha256, revision_sha256, "
                    "status, created_by) VALUES "
                    "('WSR-0111-bad-kind', 'PRJ-0111', 5, 'unknown', '{}', "
                    ":manifest_sha256, :source_bundle_sha256, :revision_sha256, "
                    "'ready', '@owner')"
                ),
                {
                    "manifest_sha256": "a" * 64,
                    "source_bundle_sha256": "b" * 64,
                    "revision_sha256": "g" * 64,
                },
            )
    finally:
        engine.dispose()
        os.environ.pop("CALIBER_DATABASE_URL", None)


@pytest.mark.slow
def test_0106_preserves_job_sourced_tasks_and_enforces_exactly_one_source(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Upgrade-from-0105: an existing job-sourced ``caliber_rework_tasks`` row
    survives the ``job_id``/``agent_id`` nullability change unchanged, the new
    ``workspace_release_id``/``project_id`` columns land NULL on it (no
    backfill -- see ``db/models.py::CaliberReworkTask``'s docstring), a
    release-sourced row is now representable, and
    ``ck_rework_task_exactly_one_source`` rejects a row with both sources or
    neither, enforced by SQLite itself rather than only by the ORM."""
    db_path = tmp_path / "rework_task_release_fk.db"
    db_url = f"sqlite:///{db_path}"
    monkeypatch.setenv("CALIBER_DATABASE_URL", db_url)

    cfg = Config(str(ALEMBIC_INI))
    monkeypatch.chdir(PROJECT_ROOT)
    command.upgrade(cfg, "0105")

    engine = create_engine(db_url)
    try:
        with engine.begin() as connection:
            connection.execute(
                text(
                    "INSERT INTO caliber_agent_config "
                    "(agent_id, experiment_id, name, owner, artifact_types, "
                    "eval_thresholds, optimizer_config, approval_policy) VALUES "
                    "('agent-0106', 'exp-0106', 'Agent 0106', '@owner', '[]', '{}', '{}', '{}')"
                )
            )
            connection.execute(
                text(
                    "INSERT INTO caliber_verification_queue "
                    "(item_id, agent_id, category, free_text, severity) VALUES "
                    "('FB-0106', 'agent-0106', 'hallucination', 'looks off', 'critical')"
                )
            )
            connection.execute(
                text(
                    "INSERT INTO caliber_refinement_jobs "
                    "(job_id, agent_id, primary_item_id, artifact_type, bundle_targets) "
                    "VALUES ('RFN-0106', 'agent-0106', 'FB-0106', 'prompt', '[]')"
                )
            )
            connection.execute(
                text(
                    "INSERT INTO caliber_rework_tasks "
                    "(task_id, job_id, agent_id, failure_kind, reason, status, created_by) "
                    "VALUES ('RWT-0106', 'RFN-0106', 'agent-0106', 'machine_gate', "
                    "'regression gate failed', 'open', '@system')"
                )
            )

        command.upgrade(cfg, "head")

        with engine.connect() as connection:
            row = connection.execute(
                text(
                    "SELECT job_id, agent_id, workspace_release_id, project_id, failure_kind "
                    "FROM caliber_rework_tasks WHERE task_id = 'RWT-0106'"
                )
            ).one()
            assert tuple(row) == ("RFN-0106", "agent-0106", None, None, "machine_gate")

        with engine.begin() as connection, pytest.raises(IntegrityError):
            connection.execute(
                text(
                    "INSERT INTO caliber_rework_tasks "
                    "(task_id, job_id, workspace_release_id, agent_id, failure_kind, "
                    "reason, status, created_by) VALUES "
                    "('RWT-0106-neither', NULL, NULL, NULL, 'release_no_go', 'x', 'open', "
                    "'@system')"
                )
            )
        with engine.begin() as connection, pytest.raises(IntegrityError):
            connection.execute(
                text(
                    "INSERT INTO caliber_rework_tasks "
                    "(task_id, job_id, workspace_release_id, agent_id, failure_kind, "
                    "reason, status, created_by) VALUES "
                    "('RWT-0106-both', 'RFN-0106', 'WSREL-fake', 'agent-0106', "
                    "'release_no_go', 'x', 'open', '@system')"
                )
            )

        # A release-sourced row -- job_id/agent_id NULL, workspace_release_id/
        # project_id set -- is now representable and satisfies the check.
        with engine.begin() as connection:
            connection.execute(
                text(
                    "INSERT INTO caliber_rework_tasks "
                    "(task_id, job_id, workspace_release_id, agent_id, project_id, "
                    "failure_kind, reason, status, created_by) VALUES "
                    "('RWT-0106-release', NULL, 'WSREL-0106', NULL, 'PRJ-0106', "
                    "'release_no_go', 'release rejected', 'open', '@system')"
                )
            )
        with engine.connect() as connection:
            row = connection.execute(
                text(
                    "SELECT job_id, agent_id, workspace_release_id, project_id "
                    "FROM caliber_rework_tasks WHERE task_id = 'RWT-0106-release'"
                )
            ).one()
            assert tuple(row) == (None, None, "WSREL-0106", "PRJ-0106")
    finally:
        engine.dispose()
        os.environ.pop("CALIBER_DATABASE_URL", None)
