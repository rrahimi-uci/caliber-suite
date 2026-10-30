"""Upgrading a ``caliber-v0.1.0a1`` database to the current head (B16-Upgrade, #594).

This file starts with the guard the rest of the chain stands on (B16-Upgrade.1, #702): the migrations the
alpha shipped are frozen. A database an operator created with the alpha sits at the alpha's head revision;
the beta's job is to run the migrations *after* that head. If any migration at or before the head could
change, a fresh install and an upgraded alpha install would end up with different schemas and nothing would
fail. ``tests/fixtures/alpha_0_1_0a1_migration_checksums.json`` records the byte checksum of every migration
file in the tag, and the alpha head derived from those files; ``tests/migration_checksums.py`` holds the
generator and the checks (``tests/test_migration_checksums.py`` proves the checks catch each kind of edit).

Later tests in this file seed a database at the alpha head and upgrade it; they read the head from
``tests.alpha_seed.alpha_head()``, never from a literal. ``tests/alpha_seed.py`` creates and seeds the database
(B16-Upgrade.2, #708).
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from dataclasses import replace
from datetime import datetime
from pathlib import Path

import pytest
from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.exc import OperationalError
from starlette.testclient import TestClient

from caliber.config import CaliberConfig
from caliber.db.migrate import VERSION_TABLE
from caliber.db.session import sessionmaker_from_engine
from caliber.server import create_app
from tests import alpha_seed
from tests.alpha_seed import (
    AlphaSeedError,
    alpha_database,
    alpha_head,
    assert_at_alpha_head,
    create_alpha_database,
    expected_rows,
    migrate_database,
    read_seeded_rows,
    row_counts,
    schema_shape,
    seed_alpha_database,
    seed_sha256,
)
from tests.alpha_seed import (
    alpha_seed as alpha_seed_tables,
)
from tests.dialect_helpers import DIALECTS, dialect_database_url
from tests.migration_checksums import (
    ALPHA_TAG,
    MIGRATIONS_PATH,
    RECOVERY_HINT,
    REPO_ROOT,
    REQUIRE_TAG_ENV,
    VERSIONS_DIR,
    TagUnavailableError,
    load_manifest,
    require_tag_from_env,
    validate_manifest,
    verify_manifest_against_tag,
    verify_tree,
)
from tests.workflow_helpers import PREFIX, create_draft, create_workflow, make_manifest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
MIGRATIONS_SCRIPT_LOCATION = PROJECT_ROOT / "src" / "caliber" / "db" / "migrations"


def _script_directory() -> ScriptDirectory:
    config = Config()
    config.set_main_option("script_location", str(MIGRATIONS_SCRIPT_LOCATION))
    return ScriptDirectory.from_config(config)


def test_shipped_migrations_unchanged() -> None:
    """Every migration the alpha shipped is still here, byte for byte, and nothing was slipped into its history."""
    problems = verify_tree(load_manifest(), VERSIONS_DIR)
    assert not problems, "\n".join([*problems, "", RECOVERY_HINT])


def test_the_manifest_is_a_well_formed_linear_history() -> None:
    manifest = load_manifest()
    assert validate_manifest(manifest) == []
    assert manifest["release_tag"] == ALPHA_TAG
    assert manifest["migrations_path"] == "caliber/src/caliber/db/migrations/versions"
    # The recorded head is the one the files themselves say; it is derived, so no literal is compared.
    assert manifest["head_revision"] == alpha_head()
    assert manifest["migration_count"] == len(manifest["migrations"])


def test_alembic_agrees_the_alpha_history_is_exactly_the_manifest() -> None:
    """Alembic's own view of the graph: the ancestors of the alpha head are the manifest's revisions, no more."""
    scripts = _script_directory()
    manifest = load_manifest()
    head = alpha_head()
    ancestry = {script.revision for script in scripts.walk_revisions(base="base", head=head)}
    assert ancestry == {entry["revision"] for entry in manifest["migrations"]}
    assert scripts.get_revision(head) is not None


def test_the_current_head_descends_from_the_alpha_head() -> None:
    """An alpha database can be upgraded: the one current head is reachable from the alpha head."""
    scripts = _script_directory()
    heads = scripts.get_heads()
    assert len(heads) == 1, f"the migration history has {len(heads)} heads: {heads}"
    ancestry = {script.revision for script in scripts.walk_revisions(base="base", head=heads[0])}
    assert alpha_head() in ancestry


def test_alpha_databases_are_versioned_in_the_table_the_code_still_reads() -> None:
    """If the version table were renamed, an alpha database would look unversioned and be migrated from zero."""
    assert load_manifest()["version_table"] == VERSION_TABLE


def test_line_endings_of_shipped_migrations_are_never_rewritten() -> None:
    """A Windows ``core.autocrlf`` checkout would otherwise change every byte the checksums cover."""
    result = subprocess.run(
        ["git", "check-attr", "text", "--", f"{MIGRATIONS_PATH}/0001_initial.py"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=True,
    )
    assert result.stdout.strip().endswith("text: unset"), result.stdout


def test_codeowners_routes_the_frozen_migrations_and_their_manifest_to_the_owner() -> None:
    """The checksum test cannot stop one change from editing a migration and its manifest entry; review can."""
    owned = {
        line.split()[0]: line.split()[1:]
        for line in (REPO_ROOT / ".github" / "CODEOWNERS").read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.startswith("#")
    }
    for pattern in (
        f"/{MIGRATIONS_PATH}/",
        "/caliber/tests/fixtures/alpha_0_1_0a1_migration_checksums.json",
    ):
        assert owned.get(pattern), f"CODEOWNERS has no owner for {pattern}"


def test_the_manifest_matches_the_release_tag() -> None:
    """The fixture is what the tag shipped, regenerated independently of the working tree.

    ``test_shipped_migrations_unchanged`` alone passes a change that edits a migration *and* its manifest
    entry; this is the check that does not, because it reads the tag's own objects. A checkout without the
    tag (the full-suite CI job, a shallow clone) skips it. The CI job that fetches the tag sets
    ``CALIBER_REQUIRE_ALPHA_TAG``, so there a missing tag fails instead of skipping.
    """
    manifest = load_manifest()
    try:
        problems = verify_manifest_against_tag(manifest, REPO_ROOT)
    except TagUnavailableError as exc:
        if require_tag_from_env():
            pytest.fail(f"{REQUIRE_TAG_ENV} is set but the tag is unavailable: {exc}")
        pytest.skip(str(exc))
    assert not problems, "\n".join([*problems, "", RECOVERY_HINT])


# --- B16-Upgrade.2 (#708): a seeded alpha database -------------------------------------------------------------
#
# ``tests/alpha_seed.py`` builds it. These tests prove the database really is an alpha database (the alpha head,
# the alpha table shapes, none of the schema later migrations add), that the data is deterministic, and that
# every way of being something else fails loudly instead of seeding a wrong database.

#: Pinned on purpose: changing the seed is allowed, but it must be a visible, reviewed edit (#715 asserts on it).
SEED_SHA256 = "5f56ae6bb206266d2c2b155b7c315bb9e1e773c76c498fa6b4e56c6c447a8502"


def _next_revision() -> str:
    """The migration that comes right after the alpha head (there must be one: the beta adds migrations)."""
    script = _script_directory().get_revision(alpha_head())
    assert script is not None
    following = script.nextrev
    assert len(following) == 1, (
        f"expected exactly one migration after the alpha head, got {following}"
    )
    return next(iter(following))


def _execute(url: str, statement: str) -> None:
    engine = create_engine(url)
    try:
        with engine.begin() as connection:
            connection.execute(text(statement))
    finally:
        engine.dispose()


def _count(url: str, table: str) -> int:
    engine = create_engine(url)
    try:
        with engine.connect() as connection:
            return int(connection.execute(text(f"SELECT COUNT(*) FROM {table}")).scalar_one())
    finally:
        engine.dispose()


def _current_revision(url: str) -> str:
    """Return the one revision recorded after an upgrade, failing closed on a split head."""
    engine = create_engine(url)
    try:
        with engine.connect() as connection:
            revisions = list(
                connection.execute(text(f"SELECT version_num FROM {VERSION_TABLE}")).scalars()
            )
    finally:
        engine.dispose()
    assert len(revisions) == 1, f"expected one recorded migration revision, got {revisions}"
    return str(revisions[0])


def _assert_new_governed_admission(url: str) -> None:
    """Exercise a new current run through the upgraded database, not just its DDL."""
    config = CaliberConfig.load(
        environ={
            "CALIBER_DATABASE_URL": url,
            "CALIBER_AUTH_MODE": "trusted_header",
            "CALIBER_ADMIN_USERS": "alice@example.test",
            "CALIBER_WORKFLOW_RUN_QUEUE_ENABLED": "true",
            "CALIBER_ADMISSION_ENABLED": "true",
            "CALIBER_BACKGROUND_TASKS_ENABLED": "false",
            "CALIBER_ASSISTANT_ENGINE": "fake",
            "CALIBER_ALLOW_FLAGGED_DSPY_OPTIMIZERS": "true",
            "CALIBER_ALLOW_FLAGGED_LOCAL_EMBEDDINGS": "true",
        }
    )
    app = create_app(config=config)
    app_engine = app.state.engine
    upgraded_engine = create_engine(url)
    app.state.engine = upgraded_engine
    app.state.session_factory = sessionmaker_from_engine(upgraded_engine)
    try:
        with TestClient(
            app,
            headers={
                "X-CALIBER-User": "alice@example.test",
                "X-CALIBER-Project": "PRJ-A0000001",
            },
        ) as client:
            workflow_id = create_workflow(client, "post-upgrade-governed-admission")
            version_id, _ = create_draft(client, workflow_id, make_manifest(workflow_id))
            published = client.post(f"{PREFIX}/workflow-versions/{version_id}/publish")
            assert published.status_code == 200, published.text

            response = client.post(
                f"{PREFIX}/workflow-runs",
                json={
                    "workflow_id": workflow_id,
                    "alias": "manual",
                    "input": "post-upgrade admission",
                    "idempotency_key": "post-upgrade-admission-1",
                },
            )
            assert response.status_code == 202, response.text
            run_id = response.json()["data"]["workflow_run_id"]

            with upgraded_engine.connect() as connection:
                run = connection.execute(
                    text(
                        "SELECT manifest_legacy, summary FROM caliber_workflow_runs "
                        "WHERE workflow_run_id = :run_id"
                    ),
                    {"run_id": run_id},
                ).one()
                assert not bool(run.manifest_legacy)
                summary = json.loads(run.summary) if isinstance(run.summary, str) else run.summary
                assert summary["admitted"] is True
                assert (
                    connection.execute(
                        text(
                            "SELECT COUNT(*) FROM caliber_run_manifests "
                            "WHERE workflow_run_id = :run_id"
                        ),
                        {"run_id": run_id},
                    ).scalar_one()
                    == 1
                )
                assert (
                    connection.execute(
                        text(
                            "SELECT COUNT(*) FROM caliber_run_grants "
                            "WHERE workflow_run_id = :run_id"
                        ),
                        {"run_id": run_id},
                    ).scalar_one()
                    == 1
                )
                assert (
                    connection.execute(
                        text(
                            "SELECT COUNT(*) FROM caliber_policy_decisions "
                            "WHERE workflow_run_id = :run_id"
                        ),
                        {"run_id": run_id},
                    ).scalar_one()
                    == 1
                )
    finally:
        app_engine.dispose()
        upgraded_engine.dispose()


@pytest.mark.parametrize("dialect", DIALECTS)
def test_alpha_database_upgrades_to_head(dialect: str, tmp_path: Path) -> None:
    """Upgrade representative alpha data and prove old and new governed paths coexist."""
    with (
        alpha_database(dialect, tmp_path, "upgrade_alpha") as database,
        dialect_database_url(dialect, tmp_path, "upgrade_head") as fresh_url,
    ):
        before = read_seeded_rows(database.url)
        migrate_database(database.url)
        migrate_database(fresh_url)

        assert _current_revision(database.url) == _script_directory().get_heads()[0]
        assert read_seeded_rows(database.url) == before == expected_rows()
        assert schema_shape(database.url) == schema_shape(fresh_url)

        legacy_runs = {row["workflow_run_id"] for row in before["caliber_workflow_runs"]}
        engine = create_engine(database.url)
        try:
            with engine.connect() as connection:
                migrated_runs = connection.execute(
                    text(
                        "SELECT workflow_run_id, manifest_legacy, runtime_lineage_id "
                        "FROM caliber_workflow_runs"
                    )
                ).all()
                assert {row.workflow_run_id for row in migrated_runs} == legacy_runs
                assert all(bool(row.manifest_legacy) for row in migrated_runs)
                assert all(row.runtime_lineage_id is None for row in migrated_runs)

                unresolved = connection.execute(
                    text(
                        "SELECT status, completed_at FROM caliber_effect_ledger "
                        "WHERE status = 'in_progress' ORDER BY effect_key"
                    )
                ).all()
                assert len(unresolved) == 2
                assert all(
                    row.status == "in_progress" and row.completed_at is None for row in unresolved
                )
        finally:
            engine.dispose()

        _assert_new_governed_admission(database.url)


@pytest.mark.parametrize("dialect", DIALECTS)
def test_seed_at_alpha_head(dialect: str, tmp_path: Path) -> None:
    """The seeded database is recorded at the alpha head, has the alpha table shapes and reads back exactly."""
    with alpha_database(dialect, tmp_path, "alpha_seed") as database:
        assert database.head == alpha_head()
        assert database.version_table == load_manifest()["version_table"] == VERSION_TABLE
        assert_at_alpha_head(
            database.url
        )  # exactly one revision recorded, and it is the alpha head

        engine = create_engine(database.url)
        try:
            with engine.connect() as connection:
                recorded = connection.execute(
                    text(f"SELECT version_num FROM {database.version_table}")
                ).all()
            assert [row[0] for row in recorded] == [alpha_head()]
        finally:
            engine.dispose()

        # Alpha shape: the seeded tables have exactly the columns the alpha schema gave them.
        shape = schema_shape(database.url)
        for table in alpha_seed_tables():
            assert shape[table.name] == tuple(sorted(table.columns)), table.name

        # Alpha data: every seeded row is there, byte for byte, and the counts are the declared counts.
        rows = read_seeded_rows(database.url)
        assert rows == expected_rows()
        assert {name: len(found) for name, found in rows.items()} == row_counts()
        for name, count in row_counts().items():
            assert _count(database.url, name) == count, name


@pytest.mark.parametrize("dialect", DIALECTS)
def test_the_alpha_database_has_none_of_the_schema_later_migrations_add(
    dialect: str, tmp_path: Path
) -> None:
    """Compare with a fresh install on the same dialect: every table and column added after the alpha is absent."""
    with (
        alpha_database(dialect, tmp_path, "alpha_shape", seed=False) as alpha,
        dialect_database_url(dialect, tmp_path, "head_shape") as head_url,
    ):
        migrate_database(head_url)
        alpha_shape, head_shape = schema_shape(alpha.url), schema_shape(head_url)

    new_tables = set(head_shape) - set(alpha_shape)
    new_columns = {
        (table, column)
        for table, columns in head_shape.items()
        if table in alpha_shape
        for column in set(columns) - set(alpha_shape[table])
    }
    # The beta really did add schema after the alpha, so the absence below is a real assertion.
    assert new_tables, "no table was added after the alpha head; the comparison would prove nothing"
    assert new_columns, "no column was added to an alpha table after the alpha head"
    assert not set(alpha_shape) - set(head_shape), "the beta must not drop an alpha table"
    # Spot checks by name, so a bug in the comparison itself cannot hide behind the derived sets.
    assert {"caliber_run_manifests", "caliber_run_grants", "caliber_effect_proposals"} <= new_tables
    assert not new_tables & set(alpha_shape)
    assert not any(column in alpha_shape[table] for table, column in new_columns)


def test_seeded_tables_have_exactly_the_alpha_columns_and_every_row_fills_all_of_them() -> None:
    """A row that left a column to a database default would make the seed non-deterministic (timestamps)."""
    for table in alpha_seed_tables():
        assert len(set(table.columns)) == len(table.columns), table.name
        expected = set(table.columns) - ({table.pk} if table.autoincrement else set())
        assert table.rows, f"{table.name} is seeded but empty"
        for row in table.rows:
            assert set(row) == expected, table.name
        if not table.autoincrement:
            keys = [row[table.pk] for row in table.rows]
            assert len(set(map(str, keys))) == len(keys), f"{table.name}: duplicate primary key"


def test_the_seed_is_deterministic() -> None:
    """Same bytes of seed data every run: rebuilt in-process and in a new interpreter."""
    first = seed_sha256()
    assert first == SEED_SHA256
    assert alpha_seed._build_seed() == alpha_seed_tables()

    env = {
        **os.environ,
        "PYTHONHASHSEED": "random",
        "PYTHONPATH": os.pathsep.join([str(PROJECT_ROOT), str(PROJECT_ROOT / "src")]),
    }
    fresh = subprocess.run(
        [sys.executable, "-c", "from tests.alpha_seed import seed_sha256; print(seed_sha256())"],
        capture_output=True,
        text=True,
        check=True,
        cwd=PROJECT_ROOT,
        env=env,
    )
    assert fresh.stdout.strip() == first


def test_two_seeded_databases_hold_identical_data(tmp_path: Path) -> None:
    with (
        alpha_database("sqlite", tmp_path, "twin_a") as one,
        alpha_database("sqlite", tmp_path, "twin_b") as two,
    ):
        assert read_seeded_rows(one.url) == read_seeded_rows(two.url) == expected_rows()


def test_the_seeded_sqlite_database_satisfies_its_foreign_keys(tmp_path: Path) -> None:
    """PostgreSQL enforces foreign keys while seeding; SQLite does not, so check the same property explicitly."""
    with alpha_database("sqlite", tmp_path, "alpha_fk") as database:
        engine = create_engine(database.url)
        try:
            with engine.connect() as connection:
                assert connection.execute(text("PRAGMA foreign_key_check")).all() == []
        finally:
            engine.dispose()


def test_the_seed_represents_what_an_alpha_install_accumulates() -> None:
    """The upgrade test needs these situations to exist in the data, not just rows in tables."""
    tables = {table.name: table for table in alpha_seed_tables()}
    runs = {row["workflow_run_id"]: row for row in tables["caliber_workflow_runs"].rows}
    statuses = {row["status"] for row in runs.values()}
    assert {"completed", "failed", "running", "waiting_approval", "queued", "cancelled"} <= statuses
    ledger = tables["caliber_effect_ledger"].rows
    assert {row["status"] for row in ledger} == {"in_progress", "completed", "failed"}
    unresolved = [row for row in ledger if row["status"] == "in_progress"]
    assert len(unresolved) >= 2 and all(row["completed_at"] is None for row in unresolved)
    assert {runs[row["workflow_run_id"]]["status"] for row in unresolved} == {
        "running",
        "cancelled",
    }
    # Old runs predate manifest snapshots and runtime lineage.
    assert any(row["manifest_snapshot"] is None for row in runs.values())
    assert all(row["runtime_lineage_id"] is None for row in runs.values())
    # Secrets are ciphertext only; the tools reference secrets that exist.
    secret_names = {row["name"] for row in tables["caliber_secrets"].rows}
    referenced = {
        name
        for row in tables["caliber_tool_registry"].rows
        for name in (row["secret_refs"] if isinstance(row["secret_refs"], list) else [])
    }
    assert referenced and referenced <= secret_names
    assert all(
        isinstance(row["ciphertext"], bytes) for row in tables["caliber_secret_versions"].rows
    )
    published = {row["status"] for row in tables["caliber_workflow_versions"].rows}
    assert {"published", "draft"} <= published
    assert {"admin", "editor", "viewer"} <= {
        row["role"] for row in tables["caliber_project_members"].rows
    }
    assert any(row["disabled"] for row in tables["caliber_user_accounts"].rows)
    assert any(row["status"] == "archived" for row in tables["caliber_projects"].rows)


def test_the_seeded_users_can_log_in_with_the_current_password_check() -> None:
    """The stored hash is in the alpha's own format, so an upgraded alpha user keeps their password."""
    from caliber.sessions import verify_password

    users = {t.name: t for t in alpha_seed_tables()}["caliber_user_accounts"].rows
    for row in users:
        assert verify_password(alpha_seed.ALPHA_SEED_PASSWORD, str(row["password_hash"]))
        assert not verify_password("another phrase", str(row["password_hash"]))


def test_no_seeded_value_looks_like_a_credential() -> None:
    """Every hash, nonce and ciphertext derives from a public constant; no key-shaped literal is stored."""
    serialised = json.dumps(
        [[t.name, t.rows] for t in alpha_seed_tables()], default=lambda value: value.hex()
    )
    for marker in ("sk-", "ghp_", "AKIA", "-----BEGIN", "xoxb-"):
        assert marker not in serialised


# --- refusals --------------------------------------------------------------------------------------------------


def test_seed_refuses_a_database_one_migration_past_the_alpha_head(tmp_path: Path) -> None:
    """An extra migration applied: the database is not an alpha database any more, and nothing is written."""
    with alpha_database("sqlite", tmp_path, "one_past", seed=False) as database:
        migrate_database(database.url, _next_revision())
        with pytest.raises(AlphaSeedError) as excinfo:
            seed_alpha_database(database.url)
        message = str(excinfo.value)
        assert "not a caliber-v0.1.0a1 database" in message
        assert _next_revision() in message
        assert f"the alpha head is {alpha_head()}" in message
        assert _count(database.url, "caliber_projects") == 0


def test_an_alpha_check_refuses_a_database_fully_upgraded_to_head(tmp_path: Path) -> None:
    with alpha_database("sqlite", tmp_path, "at_head", seed=False) as database:
        migrate_database(database.url)
        with pytest.raises(AlphaSeedError, match="not a caliber-v0.1.0a1 database"):
            assert_at_alpha_head(database.url)


def test_seed_refuses_a_database_behind_the_alpha_head(tmp_path: Path) -> None:
    script = _script_directory().get_revision(alpha_head())
    assert script is not None
    previous = script.down_revision
    assert isinstance(previous, str)
    with alpha_database("sqlite", tmp_path, "behind", seed=False) as database:
        _execute(database.url, f"UPDATE {database.version_table} SET version_num = '{previous}'")
        with pytest.raises(AlphaSeedError, match=previous):
            seed_alpha_database(database.url)


def test_seed_refuses_a_database_that_was_never_migrated(tmp_path: Path) -> None:
    with dialect_database_url("sqlite", tmp_path, "unversioned") as url:
        with pytest.raises(AlphaSeedError, match="no revision"):
            seed_alpha_database(url)
        with pytest.raises(AlphaSeedError, match="no revision"):
            assert_at_alpha_head(url)


def test_seed_refuses_a_version_table_with_two_revisions(tmp_path: Path) -> None:
    with alpha_database("sqlite", tmp_path, "two_heads", seed=False) as database:
        _execute(
            database.url, f"INSERT INTO {database.version_table} VALUES ('{_next_revision()}')"
        )
        with pytest.raises(AlphaSeedError, match="not a caliber-v0.1.0a1 database"):
            seed_alpha_database(database.url)


def test_seed_refuses_to_seed_twice_and_leaves_the_data_alone(tmp_path: Path) -> None:
    with alpha_database("sqlite", tmp_path, "twice") as database:
        before = read_seeded_rows(database.url)
        with pytest.raises(AlphaSeedError, match="already holds"):
            seed_alpha_database(database.url)
        assert read_seeded_rows(database.url) == before


def test_seed_refuses_a_database_that_already_holds_unrelated_rows(tmp_path: Path) -> None:
    with alpha_database("sqlite", tmp_path, "occupied", seed=False) as database:
        _execute(
            database.url,
            "INSERT INTO caliber_user_accounts (user_id, password_hash) VALUES ('operator', 'x')",
        )
        with pytest.raises(AlphaSeedError, match="caliber_user_accounts already holds 1 row"):
            seed_alpha_database(database.url)


def test_a_failure_part_way_through_seeding_rolls_everything_back(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    tables = alpha_seed_tables()
    broken = (*tables[:-1], replace(tables[-1], rows=({"no_such_column": 1},)))
    with alpha_database("sqlite", tmp_path, "rollback", seed=False) as database:
        monkeypatch.setattr(alpha_seed, "alpha_seed", lambda: broken)
        with pytest.raises(OperationalError):
            seed_alpha_database(database.url)
        monkeypatch.undo()
        assert _count(database.url, "caliber_projects") == 0
        seed_alpha_database(
            database.url
        )  # the database was left clean, so a correct seed still works
        assert read_seeded_rows(database.url) == expected_rows()


def test_creating_the_alpha_database_refuses_edited_migrations(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """If a shipped migration's bytes changed, the schema built from it is not the alpha's: refuse before migrating."""
    copy = tmp_path / "versions"
    shutil.copytree(VERSIONS_DIR, copy)
    victim = sorted(copy.glob("0001_*.py"))[0]
    victim.write_bytes(victim.read_bytes() + b"\n# edited\n")
    monkeypatch.setattr(alpha_seed, "_script_versions_dir", lambda: copy)
    with dialect_database_url("sqlite", tmp_path, "edited") as url:
        with pytest.raises(
            AlphaSeedError, match=r"not the ones caliber-v0\.1\.0a1 shipped"
        ) as excinfo:
            create_alpha_database(url)
        assert victim.name in str(excinfo.value)
        engine = create_engine(url)
        try:
            assert inspect(engine).get_table_names() == []  # refused before touching the database
        finally:
            engine.dispose()


def test_the_migrations_alembic_reads_are_the_ones_the_tree_verifies() -> None:
    """``create_alpha_database`` checks the directory alembic really uses, not just the one the tests assume."""
    assert alpha_seed._script_versions_dir().resolve() == VERSIONS_DIR.resolve()


def test_a_seed_table_definition_with_columns_the_alpha_does_not_have_is_rejected() -> None:
    build = alpha_seed._table
    with pytest.raises(AlphaSeedError, match="seed rows use columns"):
        build("t", "a b", [{"c": 1}], pk="a")
    with pytest.raises(AlphaSeedError, match="defaults use columns"):
        build("t", "a b", [], defaults={"c": 1}, pk="a")
    with pytest.raises(AlphaSeedError, match="duplicates"):
        build("t", "a a", [], pk="a")
    with pytest.raises(AlphaSeedError, match="primary key"):
        build("t", "a b", [], pk="z")


def test_values_read_back_from_either_dialect_have_one_spelling() -> None:
    """psycopg returns parsed JSON and datetimes (other drivers memoryviews); the seed stores text."""
    normalise = alpha_seed._normalise
    assert normalise({"b": 1, "a": [True]}) == '{"a": [true], "b": 1}'
    assert normalise(datetime(2026, 10, 12, 9, 0, 0)) == "2026-10-12 09:00:00.000000"
    assert normalise(memoryview(b"ab")) == b"ab"
    assert normalise("text") == "text"
