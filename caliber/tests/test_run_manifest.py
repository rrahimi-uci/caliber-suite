"""Persisted run manifests: written once, one per run, and never invented for a legacy run (B02.1a, #656).

Every case runs on SQLite and, where ``CALIBER_TEST_POSTGRES_URL`` is set (the "Migration parity (PostgreSQL)"
job), on PostgreSQL, against a database migrated by Alembic so the migration's triggers and constraints are the
thing under test rather than the ORM metadata alone.
"""

from __future__ import annotations

import ast
import dataclasses
import hashlib
import json
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, delete, inspect, select, text, update
from sqlalchemy.engine import Engine
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.orm import Session

from caliber import run_manifest as run_manifest_module
from caliber.contracts import ContractValidationError, validate_record
from caliber.db.immutable import ImmutableRecordError, guard_immutable
from caliber.db.models import CaliberRunManifest, CaliberWorkflowRun
from caliber.policy.principal import Subject, Workload
from caliber.run_manifest import (
    CODES,
    BuiltManifest,
    DeploymentScope,
    KnowledgeBasePin,
    ManifestError,
    PinnedRuleBundle,
    ResolvedPins,
    build_run_manifest,
    canonical_bytes,
    digest_of,
    new_manifest_id,
    pin_rule_bundle,
    read_manifest,
)
from tests.dialect_helpers import DIALECTS, POSTGRES_TEST_URL, dialect_database_url

PROJECT_ROOT = Path(__file__).resolve().parent.parent
ALEMBIC_INI = PROJECT_ROOT / "alembic.ini"

pytestmark = pytest.mark.slow


def _manifest_bytes(run_id: str, **extra: Any) -> bytes:
    record = {"schema_version": "v1", "workflow_run_id": run_id, **extra}
    return json.dumps(record, sort_keys=True, separators=(",", ":")).encode()


def _manifest(run_id: str, manifest_id: str | None = None, **extra: Any) -> CaliberRunManifest:
    raw = _manifest_bytes(run_id, **extra)
    return CaliberRunManifest(
        manifest_id=manifest_id or f"m-{uuid.uuid4().hex[:12]}",
        workflow_run_id=run_id,
        schema_version="v1",
        manifest_bytes=raw,
        manifest_digest=hashlib.sha256(raw).hexdigest(),
    )


def _alembic(db_url: str, monkeypatch: pytest.MonkeyPatch) -> Config:
    monkeypatch.setenv("CALIBER_DATABASE_URL", db_url)
    monkeypatch.chdir(PROJECT_ROOT)
    return Config(str(ALEMBIC_INI))


@contextmanager
def _migrated(
    dialect: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, revision: str = "head"
) -> Iterator[tuple[Engine, Config]]:
    with dialect_database_url(dialect, tmp_path, "run_manifest") as db_url:
        config = _alembic(db_url, monkeypatch)
        command.upgrade(config, revision)
        engine = create_engine(db_url)
        try:
            yield engine, config
        finally:
            engine.dispose()


def _add_run(engine: Engine, run_id: str | None = None, **columns: Any) -> str:
    run_id = run_id or f"run-{uuid.uuid4().hex[:12]}"
    columns.setdefault("manifest_legacy", False)  # an admitted run; legacy is the default otherwise
    with Session(engine) as session, session.begin():
        session.add(CaliberWorkflowRun(workflow_run_id=run_id, workflow_id="wf-1", **columns))
    return run_id


def _store(engine: Engine, run_id: str, **extra: Any) -> str:
    """Write a manifest for ``run_id`` and return its id."""
    manifest = _manifest(run_id, **extra)
    manifest_id = manifest.manifest_id
    with Session(engine) as session, session.begin():
        session.add(manifest)
    return manifest_id


def _stored(engine: Engine, run_id: str) -> CaliberRunManifest:
    with Session(engine) as session:
        return session.execute(
            select(CaliberRunManifest).where(CaliberRunManifest.workflow_run_id == run_id)
        ).scalar_one()


# ---------------------------------------------------------------- one row per run, bytes and digest kept


@pytest.mark.parametrize("dialect", DIALECTS)
def test_a_manifest_round_trips_its_canonical_bytes_and_digest(
    dialect: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with _migrated(dialect, tmp_path, monkeypatch) as (engine, _):
        run_id = _add_run(engine)
        stored = _store(engine, run_id, purpose="triage")
        read = _stored(engine, run_id)
        assert bytes(read.manifest_bytes) == _manifest_bytes(run_id, purpose="triage")
        assert read.manifest_digest == hashlib.sha256(bytes(read.manifest_bytes)).hexdigest()
        assert read.manifest_id == stored
        assert read.schema_version == "v1"
        assert read.created_at is not None


@pytest.mark.parametrize("dialect", DIALECTS)
def test_a_second_manifest_for_the_same_run_is_refused(
    dialect: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with _migrated(dialect, tmp_path, monkeypatch) as (engine, _):
        run_id = _add_run(engine)
        first = _store(engine, run_id)
        with pytest.raises(IntegrityError), Session(engine) as session, session.begin():
            session.add(_manifest(run_id, purpose="a different pin set"))
        assert _stored(engine, run_id).manifest_id == first, "the original is kept"
        other = _add_run(engine)
        _store(engine, other)  # another run is unaffected


@pytest.mark.parametrize("dialect", DIALECTS)
def test_the_digest_must_be_the_sha256_of_the_bytes(
    dialect: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with _migrated(dialect, tmp_path, monkeypatch) as (engine, _):
        run_id = _add_run(engine)
        bad = _manifest(run_id)
        bad.manifest_digest = "0" * 64
        with pytest.raises(ValueError, match="SHA-256 of manifest_bytes"):
            with Session(engine) as session, session.begin():
                session.add(bad)
        assert session_count(engine) == 0


def session_count(engine: Engine) -> int:
    with engine.connect() as connection:
        return connection.execute(text("SELECT COUNT(*) FROM caliber_run_manifests")).scalar_one()


@pytest.mark.parametrize("dialect", DIALECTS)
def test_the_database_refuses_a_malformed_digest_or_empty_bytes_written_around_the_orm(
    dialect: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with _migrated(dialect, tmp_path, monkeypatch) as (engine, _):
        run_id = _add_run(engine)
        row = {
            "manifest_id": "m-raw",
            "workflow_run_id": run_id,
            "schema_version": "v1",
            "manifest_bytes": b"{}",
            "manifest_digest": "short",
        }
        insert = text(
            "INSERT INTO caliber_run_manifests (manifest_id, workflow_run_id, schema_version, "
            "manifest_bytes, manifest_digest) VALUES (:manifest_id, :workflow_run_id, "
            ":schema_version, :manifest_bytes, :manifest_digest)"
        )
        with pytest.raises(IntegrityError), engine.begin() as connection:
            connection.execute(insert, row)
        with pytest.raises(IntegrityError), engine.begin() as connection:
            connection.execute(insert, {**row, "manifest_bytes": b"", "manifest_digest": "0" * 64})
        assert session_count(engine) == 0


@pytest.mark.skipif(not POSTGRES_TEST_URL, reason="the digest CHECK is PostgreSQL-only")
def test_postgresql_also_pins_the_digest_to_the_bytes_when_the_orm_is_bypassed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with _migrated("postgresql", tmp_path, monkeypatch) as (engine, _):
        run_id = _add_run(engine)
        with pytest.raises(IntegrityError, match="ck_run_manifest_digest_is_sha256"):
            with engine.begin() as connection:
                connection.execute(
                    text(
                        "INSERT INTO caliber_run_manifests (manifest_id, workflow_run_id, schema_version, "
                        "manifest_bytes, manifest_digest) VALUES ('m-raw', :run, 'v1', :bytes, :digest)"
                    ),
                    {"run": run_id, "bytes": b"{}", "digest": "0" * 64},
                )


# ---------------------------------------------------------------- immutability: ORM, bulk SQL, delete


@pytest.mark.parametrize("dialect", DIALECTS)
def test_update_raises_immutable_record_error(
    dialect: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with _migrated(dialect, tmp_path, monkeypatch) as (engine, _):
        run_id = _add_run(engine)
        _store(engine, run_id, purpose="original")
        before = bytes(_stored(engine, run_id).manifest_bytes)
        with pytest.raises(
            ImmutableRecordError, match="caliber_run_manifests is immutable: UPDATE"
        ):
            with Session(engine) as session, session.begin():
                row = session.execute(select(CaliberRunManifest)).scalar_one()
                row.manifest_bytes = b'{"pins": "changed after the fact"}'
        assert bytes(_stored(engine, run_id).manifest_bytes) == before


@pytest.mark.parametrize("dialect", DIALECTS)
def test_an_orm_delete_raises_immutable_record_error(
    dialect: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with _migrated(dialect, tmp_path, monkeypatch) as (engine, _):
        run_id = _add_run(engine)
        _store(engine, run_id)
        with pytest.raises(ImmutableRecordError, match="DELETE refused"):
            with Session(engine) as session, session.begin():
                session.delete(session.execute(select(CaliberRunManifest)).scalar_one())
        assert session_count(engine) == 1


@pytest.mark.parametrize("dialect", DIALECTS)
def test_an_orm_bulk_update_or_delete_raises_immutable_record_error(
    dialect: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with _migrated(dialect, tmp_path, monkeypatch) as (engine, _):
        run_id = _add_run(engine)
        _store(engine, run_id)
        statements = (
            update(CaliberRunManifest).values(schema_version="v2"),
            delete(CaliberRunManifest),
        )
        for statement in statements:
            with pytest.raises(ImmutableRecordError, match="refused"):
                with Session(engine) as session, session.begin():
                    session.execute(statement)
        with pytest.raises(ImmutableRecordError, match="DELETE refused"):
            with Session(engine) as session, session.begin():
                session.query(CaliberRunManifest).delete()
        assert _stored(engine, run_id).schema_version == "v1"


@pytest.mark.parametrize("dialect", DIALECTS)
@pytest.mark.parametrize(
    "sql",
    [
        "UPDATE caliber_run_manifests SET schema_version = 'v2'",
        "UPDATE caliber_run_manifests SET manifest_digest = manifest_digest WHERE 1 = 1",
        "DELETE FROM caliber_run_manifests",
        "DELETE FROM caliber_run_manifests WHERE manifest_id = 'does-not-matter'",
    ],
)
def test_bulk_sql_is_refused_by_the_database_itself(
    dialect: str, sql: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Core and raw SQL skip the ORM guard entirely; only the trigger is left, and it holds. Row triggers fire
    only for a matching row, so a manifest is seeded first."""
    with _migrated(dialect, tmp_path, monkeypatch) as (engine, _):
        run_id = _add_run(engine)
        _store(engine, run_id)
        if "does-not-matter" in sql:
            sql = sql.replace("does-not-matter", _stored(engine, run_id).manifest_id)
        with pytest.raises(IntegrityError, match="caliber_run_manifests is immutable"):
            with engine.begin() as connection:
                connection.execute(text(sql))
        assert session_count(engine) == 1
        assert _stored(engine, run_id).schema_version == "v1"


@pytest.mark.parametrize("dialect", DIALECTS)
def test_a_core_update_and_delete_are_refused_by_the_database(
    dialect: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with _migrated(dialect, tmp_path, monkeypatch) as (engine, _):
        run_id = _add_run(engine)
        _store(engine, run_id)
        table = CaliberRunManifest.__table__
        for statement in (update(table).values(schema_version="v2"), delete(table)):
            with pytest.raises(IntegrityError, match="is immutable"):
                with engine.begin() as connection:
                    connection.execute(statement)
        assert session_count(engine) == 1


@pytest.mark.skipif(not POSTGRES_TEST_URL, reason="TRUNCATE triggers are PostgreSQL-only")
def test_postgresql_refuses_truncate(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    with _migrated("postgresql", tmp_path, monkeypatch) as (engine, _):
        _store(engine, _add_run(engine))
        with pytest.raises(IntegrityError, match="is immutable: TRUNCATE refused"):
            with engine.begin() as connection:
                connection.execute(text("TRUNCATE caliber_run_manifests"))
        assert session_count(engine) == 1


def test_the_guard_leaves_other_models_alone_and_registering_twice_is_harmless(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert guard_immutable(CaliberRunManifest) is CaliberRunManifest
    with _migrated("sqlite", tmp_path, monkeypatch) as (engine, _):
        run_id = _add_run(engine)
        with Session(engine) as session, session.begin():
            session.get(CaliberWorkflowRun, run_id).status = "running"  # type: ignore[union-attr]
            session.execute(update(CaliberWorkflowRun).values(status="completed"))
        with Session(engine) as session:
            assert session.get(CaliberWorkflowRun, run_id).status == "completed"  # type: ignore[union-attr]


# ---------------------------------------------------------------- the runtime role has no way to change a row


@pytest.mark.skipif(not POSTGRES_TEST_URL, reason="role privileges are PostgreSQL-only")
def test_postgresql_runtime_role_with_select_and_insert_cannot_update_delete_or_truncate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with _migrated("postgresql", tmp_path, monkeypatch) as (engine, _):
        run_id = _add_run(engine)
        role = f"caliber_runtime_{uuid.uuid4().hex[:8]}"
        with engine.begin() as connection:
            connection.execute(text(f'CREATE ROLE "{role}"'))
            connection.execute(text(f'GRANT SELECT ON caliber_workflow_runs TO "{role}"'))
            connection.execute(text(f'GRANT SELECT, INSERT ON caliber_run_manifests TO "{role}"'))
        try:
            raw = _manifest_bytes(run_id)
            with engine.begin() as connection:
                connection.execute(
                    text(f'SET LOCAL ROLE "{role}"')
                )  # reset at the end of the transaction
                connection.execute(
                    text(
                        "INSERT INTO caliber_run_manifests (manifest_id, workflow_run_id, schema_version, "
                        "manifest_bytes, manifest_digest) VALUES ('m-role', :run, 'v1', :bytes, :digest)"
                    ),
                    {"run": run_id, "bytes": raw, "digest": hashlib.sha256(raw).hexdigest()},
                )
            for sql in (
                "UPDATE caliber_run_manifests SET schema_version = 'v2'",
                "DELETE FROM caliber_run_manifests",
                "TRUNCATE caliber_run_manifests",
            ):
                with pytest.raises(DBAPIError, match="permission denied"):
                    with engine.begin() as connection:
                        connection.execute(text(f'SET LOCAL ROLE "{role}"'))
                        connection.execute(text(sql))
            assert session_count(engine) == 1
        finally:
            with engine.begin() as connection:
                connection.execute(
                    text(
                        f'REVOKE ALL ON caliber_workflow_runs, caliber_run_manifests FROM "{role}"'
                    )
                )
                connection.execute(text(f'DROP ROLE "{role}"'))


@pytest.mark.skipif(not POSTGRES_TEST_URL, reason="privileges are PostgreSQL-only")
def test_postgresql_public_holds_no_update_delete_or_truncate_on_the_manifests(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with _migrated("postgresql", tmp_path, monkeypatch) as (engine, _):
        with engine.connect() as connection:
            granted = {
                row[0]
                for row in connection.execute(
                    text(
                        "SELECT privilege_type FROM information_schema.role_table_grants "
                        "WHERE table_name = 'caliber_run_manifests' AND grantee = 'PUBLIC'"
                    )
                )
            }
        assert not granted & {"UPDATE", "DELETE", "TRUNCATE"}


# ---------------------------------------------------------------- legacy runs: marked, never given a manifest


@pytest.mark.parametrize("dialect", DIALECTS)
def test_runs_that_predate_the_migration_are_marked_legacy_and_get_no_manifest(
    dialect: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with dialect_database_url(dialect, tmp_path, "run_manifest_legacy") as db_url:
        config = _alembic(db_url, monkeypatch)
        command.upgrade(config, "0113")
        engine = create_engine(db_url)
        try:
            with engine.begin() as connection:
                connection.execute(
                    text(
                        "INSERT INTO caliber_workflow_runs (workflow_run_id, workflow_id, tenant_id, "
                        "status, source, priority, attempt_number) "
                        "VALUES ('old-run', 'wf-1', 'local', 'completed', 'manual', 0, 1)"
                    )
                )
            command.upgrade(config, "head")

            with Session(engine) as session:
                assert session.get(CaliberWorkflowRun, "old-run").manifest_legacy is True  # type: ignore[union-attr]
            assert session_count(engine) == 0, "no manifest is manufactured for a historical run"

            new_run = _add_run(engine)
            with Session(engine) as session:
                assert session.get(CaliberWorkflowRun, new_run).manifest_legacy is False  # type: ignore[union-attr]
            _store(engine, new_run)  # a new run can have its manifest

            with pytest.raises(
                IntegrityError, match="a manifest needs an existing, non-legacy run"
            ):
                with Session(engine) as session, session.begin():
                    session.add(_manifest("old-run"))
            assert session_count(engine) == 1
        finally:
            engine.dispose()


@pytest.mark.parametrize("dialect", DIALECTS)
def test_the_legacy_flag_is_write_once(
    dialect: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with _migrated(dialect, tmp_path, monkeypatch) as (engine, _):
        fresh = _add_run(engine)
        legacy = _add_run(engine, manifest_legacy=True)
        for run_id, value in ((fresh, True), (legacy, False)):
            with pytest.raises(IntegrityError, match="manifest_legacy is write-once"):
                with Session(engine) as session, session.begin():
                    session.get(CaliberWorkflowRun, run_id).manifest_legacy = value  # type: ignore[union-attr]
        with engine.begin() as connection:  # an unrelated column, and the same value, are fine
            connection.execute(
                text(
                    "UPDATE caliber_workflow_runs SET status = 'running', manifest_legacy = manifest_legacy"
                )
            )
        with Session(engine) as session:
            assert session.get(CaliberWorkflowRun, fresh).manifest_legacy is False  # type: ignore[union-attr]
            assert session.get(CaliberWorkflowRun, legacy).manifest_legacy is True  # type: ignore[union-attr]


@pytest.mark.parametrize("dialect", DIALECTS)
def test_a_run_inserted_as_legacy_cannot_get_a_manifest_either(
    dialect: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with _migrated(dialect, tmp_path, monkeypatch) as (engine, _):
        run_id = _add_run(engine, manifest_legacy=True)
        with pytest.raises(IntegrityError, match="legacy run"):
            _store(engine, run_id)


# ---------------------------------------------------------------- migration: shape, rollback, re-apply


@pytest.mark.parametrize("dialect", DIALECTS)
def test_the_migration_creates_the_table_and_column_and_rolls_back_cleanly(
    dialect: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with _migrated(dialect, tmp_path, monkeypatch) as (engine, config):
        inspector = inspect(engine)
        assert "caliber_run_manifests" in inspector.get_table_names()
        assert "manifest_legacy" in {
            c["name"] for c in inspector.get_columns("caliber_workflow_runs")
        }
        unique = {
            tuple(u["column_names"])
            for u in inspector.get_unique_constraints("caliber_run_manifests")
        }
        assert ("workflow_run_id",) in unique

        run_id = _add_run(engine)
        _store(engine, run_id)

        command.downgrade(config, "0113")
        inspector = inspect(engine)
        assert "caliber_run_manifests" not in inspector.get_table_names()
        assert "manifest_legacy" not in {
            c["name"] for c in inspector.get_columns("caliber_workflow_runs")
        }
        with engine.connect() as connection:
            assert (
                connection.execute(
                    text("SELECT COUNT(*) FROM caliber_workflow_runs WHERE workflow_run_id = :r"),
                    {"r": run_id},
                ).scalar_one()
                == 1
            ), "a rollback loses manifests, never runs"
            if dialect == "postgresql":
                leftovers = connection.execute(
                    text(
                        "SELECT proname FROM pg_proc WHERE proname LIKE 'caliber_run%' "
                        "OR proname LIKE 'caliber_runs_legacy%'"
                    )
                ).fetchall()
                assert leftovers == [], "the trigger functions are dropped with the table"
            else:
                names = {
                    row[0]
                    for row in connection.execute(
                        text("SELECT name FROM sqlite_master WHERE type = 'trigger'")
                    )
                }
                assert not {n for n in names if "caliber_run_manifests" in n or "legacy_flag" in n}

        command.upgrade(config, "head")
        assert "caliber_run_manifests" in inspect(engine).get_table_names()
        with Session(engine) as session:
            assert session.get(CaliberWorkflowRun, run_id).manifest_legacy is True  # type: ignore[union-attr]
        assert session_count(engine) == 0, (
            "re-applying starts with no manifests, and the run is legacy"
        )


# ---------------------------------------------------------------- a run created outside admission is legacy


@pytest.mark.parametrize("dialect", DIALECTS)
def test_a_run_created_without_saying_otherwise_is_legacy_and_cannot_gain_a_manifest(
    dialect: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with _migrated(dialect, tmp_path, monkeypatch) as (engine, _):
        via_orm = f"run-{uuid.uuid4().hex[:8]}"
        with Session(engine) as session, session.begin():
            session.add(CaliberWorkflowRun(workflow_run_id=via_orm, workflow_id="wf-1"))
        via_sql = f"run-{uuid.uuid4().hex[:8]}"
        with engine.begin() as connection:
            connection.execute(
                text(
                    "INSERT INTO caliber_workflow_runs (workflow_run_id, workflow_id, tenant_id, status, "
                    "source, priority, attempt_number) VALUES (:r, 'wf-1', 'local', 'completed', 'manual', 0, 1)"
                ),
                {"r": via_sql},
            )
        with Session(engine) as session:
            assert session.get(CaliberWorkflowRun, via_orm).manifest_legacy is True  # type: ignore[union-attr]
            assert session.get(CaliberWorkflowRun, via_sql).manifest_legacy is True  # type: ignore[union-attr]
        for run_id in (via_orm, via_sql):
            with pytest.raises(IntegrityError, match="non-legacy run"):
                _store(engine, run_id)


@pytest.mark.parametrize("dialect", DIALECTS)
def test_a_manifest_for_a_run_that_does_not_exist_is_refused_on_every_dialect(
    dialect: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with _migrated(dialect, tmp_path, monkeypatch) as (engine, _):
        with pytest.raises(IntegrityError, match="non-legacy run"):
            _store(engine, "no-such-run")
        assert session_count(engine) == 0


@pytest.mark.skipif(not POSTGRES_TEST_URL, reason="search_path shadowing is PostgreSQL-only")
def test_postgresql_a_temp_table_cannot_shadow_the_runs_table_to_fabricate_a_manifest(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Regression: the trigger function once resolved ``caliber_workflow_runs`` through ``search_path``, which
    searches ``pg_temp`` first, so a role holding only INSERT on the manifests could attach one to a legacy run
    by creating a temporary table of that name."""
    with _migrated("postgresql", tmp_path, monkeypatch) as (engine, _):
        legacy = _add_run(engine, manifest_legacy=True)
        raw = _manifest_bytes(legacy)
        with pytest.raises(IntegrityError, match="non-legacy run"):
            with engine.begin() as connection:
                connection.execute(
                    text(
                        "CREATE TEMP TABLE caliber_workflow_runs "
                        "(workflow_run_id varchar(64), manifest_legacy boolean) ON COMMIT DROP"
                    )
                )
                connection.execute(
                    text("INSERT INTO pg_temp.caliber_workflow_runs VALUES (:r, false)"),
                    {"r": legacy},
                )
                connection.execute(
                    text(
                        "INSERT INTO caliber_run_manifests (manifest_id, workflow_run_id, schema_version, "
                        "manifest_bytes, manifest_digest) VALUES ('m-fab', :run, 'v1', :bytes, :digest)"
                    ),
                    {"run": legacy, "bytes": raw, "digest": hashlib.sha256(raw).hexdigest()},
                )
        assert session_count(engine) == 0


# ---------------------------------------------------------------- runs that carry a manifest are retained


@pytest.mark.parametrize("dialect", DIALECTS)
def test_retention_pruning_skips_runs_that_have_a_manifest(
    dialect: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from datetime import datetime, timedelta, timezone

    from caliber.workflows.promoter import prune_workflow_runs

    with _migrated(dialect, tmp_path, monkeypatch) as (engine, _):
        pinned = _add_run(engine)
        _store(engine, pinned)
        plain = _add_run(engine)
        legacy = _add_run(engine, manifest_legacy=True)
        far_future = datetime.now(timezone.utc) + timedelta(days=30)
        with Session(engine) as session, session.begin():
            assert prune_workflow_runs(session, retention_days=1, now=far_future) == 2
        with Session(engine) as session:
            assert session.get(CaliberWorkflowRun, pinned) is not None, "a manifest's run is kept"
            assert session.get(CaliberWorkflowRun, plain) is None
            assert session.get(CaliberWorkflowRun, legacy) is None, "legacy runs are still prunable"
        assert session_count(engine) == 1


def test_deleting_a_workflow_whose_runs_have_manifests_is_a_409(
    client: Any, db_session: Session
) -> None:
    from tests.workflow_helpers import PREFIX, create_workflow

    kept = create_workflow(client, "Has Manifest")
    plain = create_workflow(client, "No Manifest")
    pinned_run = "run-pinned"
    for workflow_id, run_id, legacy in ((kept, pinned_run, False), (plain, "run-legacy", True)):
        db_session.add(
            CaliberWorkflowRun(
                workflow_run_id=run_id, workflow_id=workflow_id, manifest_legacy=legacy
            )
        )
    db_session.commit()
    db_session.add(_manifest(pinned_run))
    db_session.commit()

    refused = client.delete(f"{PREFIX}/workflows/{kept}")
    assert refused.status_code == 409, refused.text
    assert "immutable run manifests" in refused.text
    assert db_session.get(
        CaliberRunManifest, db_session.execute(select(CaliberRunManifest.manifest_id)).scalar_one()
    )
    assert client.get(f"{PREFIX}/workflows/{kept}").status_code == 200

    allowed = client.delete(f"{PREFIX}/workflows/{plain}")
    assert allowed.status_code in (200, 204), allowed.text
    assert client.get(f"{PREFIX}/workflows/{plain}").status_code == 404


# ---------------------------------------------------------------- the admission builder (B02.1b, #676)

FIXTURES = PROJECT_ROOT / "tests" / "fixtures"
RULE_BUNDLE = FIXTURES / "rule_bundles" / "support_rules.v1.yaml"
RULE_BUNDLE_SHA = hashlib.sha256(RULE_BUNDLE.read_bytes()).hexdigest()
WORKFLOW_DIGEST = hashlib.sha256(b"workflow-version-3").hexdigest()
CREATED = datetime(2026, 10, 26, 9, 0, 0, tzinfo=timezone.utc)


def _pins(**overrides: Any) -> ResolvedPins:
    fields: dict[str, Any] = {
        "workflow_version": "wfv-3",
        "bundle_digest": hashlib.sha256(b"compiled-bundle").hexdigest(),
        "prompt_versions": ["support@4", "greeting@2"],
        "tool_versions": ["lookup_case@1"],
        "skill_versions": [],
        "knowledge_base_versions": [KnowledgeBasePin("kb-1", "v7")],
        "model_routes": ["scripted"],
        "policy_version": "pol-1",
        **overrides,
    }
    return ResolvedPins(**fields)


def _build(**overrides: Any) -> BuiltManifest:
    arguments: dict[str, Any] = {
        "manifest_id": "mf-1",
        "workflow_run_id": "run-1",
        "pins": _pins(),
        "subject": Subject("alice", "session", "proj-1"),
        "workload": Workload.for_workflow("document-request", WORKFLOW_DIGEST),
        "purpose": "request a missing document",
        "scope": DeploymentScope.fixed("proj-1"),
        "created_at": CREATED,
        **overrides,
    }
    return build_run_manifest(**arguments)


def _refused(code: str, **overrides: Any) -> ManifestError:
    with pytest.raises(ManifestError) as caught:
        _build(**overrides)
    assert caught.value.code == code, caught.value
    return caught.value


def test_build_validates_against_schema_and_carries_exactly_what_was_resolved() -> None:
    built = _build()
    record = built.record
    validate_record("run_manifest", record)
    assert record["schema_version"] == "v1" and record["legacy"] is False
    assert (record["tenant_id"], record["region"], record["residency"], record["project_id"]) == (
        "local",
        "local",
        "local",
        "proj-1",
    )
    assert record["subject"] == {"user_id": "alice", "auth_method": "session"}, (
        "no project in the subject"
    )
    assert record["workload"] == {
        "workload_id": "wl_" + WORKFLOW_DIGEST,
        "name": "document-request",
        "workflow_version_digest": WORKFLOW_DIGEST,
    }
    assert record["pins"]["prompt_versions"] == ["greeting@2", "support@4"], "sorted"
    assert record["pins"]["knowledge_base_versions"] == [
        {"knowledge_base_id": "kb-1", "version_id": "v7"}
    ]
    assert (record["rule_bundle_path"], record["rule_bundle_sha256"]) == (None, None)
    assert record["created_at"] == "2026-10-26T09:00:00.000000Z"
    assert (built.manifest_id, built.workflow_run_id) == ("mf-1", "run-1")


def test_the_digest_is_the_sha256_of_the_canonical_bytes_repeatable_and_excludes_itself() -> None:
    first, second = _build(), _build()
    assert first == second and first.manifest_bytes == second.manifest_bytes
    assert first.manifest_digest == hashlib.sha256(first.manifest_bytes).hexdigest()
    assert first.manifest_bytes == canonical_bytes(first.record)
    assert b"digest" not in first.manifest_bytes.replace(b"bundle_digest", b"").replace(
        b"workflow_version_digest", b""
    ), "the manifest does not carry its own digest"
    assert b" " not in first.manifest_bytes.replace(b"request a missing document", b"")
    assert (
        first.manifest_bytes
        == json.dumps(
            first.record, sort_keys=True, separators=(",", ":"), ensure_ascii=False
        ).encode()
    )
    first.record["purpose"] = (
        "edited"  # a copy: the built bytes are not changed by editing what was read
    )
    assert first.record["purpose"] == "request a missing document"


def test_the_order_pins_were_resolved_in_does_not_change_the_digest() -> None:
    other = _pins(
        prompt_versions=["support@4", "greeting@2"][::-1],
        knowledge_base_versions=[KnowledgeBasePin("kb-1", "v7")],
    )
    assert _build(pins=other).manifest_digest == _build().manifest_digest


@pytest.mark.parametrize(
    "overrides",
    [
        {"manifest_id": "mf-2"},
        {"workflow_run_id": "run-2"},
        {"purpose": "another purpose"},
        {"created_at": CREATED + timedelta(microseconds=1)},
        {
            "scope": DeploymentScope.fixed("proj-2"),
            "subject": Subject("alice", "session", "proj-2"),
        },
        {"pins": _pins(policy_version="pol-2")},
        {"pins": _pins(model_routes=["scripted", "other"])},
        {"pins": _pins(knowledge_base_versions=[KnowledgeBasePin("kb-1", "v8")])},
        {"subject": Subject("bob", "session", "proj-1")},
        {
            "workload": Workload.for_workflow(
                "document-request", hashlib.sha256(b"other").hexdigest()
            )
        },
    ],
)
def test_every_input_changes_the_digest(overrides: dict[str, Any]) -> None:
    assert _build(**overrides).manifest_digest != _build().manifest_digest


def test_timestamps_are_utc_and_must_carry_an_offset() -> None:
    plus_two = datetime(2026, 10, 26, 11, 0, 0, tzinfo=timezone(timedelta(hours=2)))
    assert _build(created_at=plus_two).manifest_digest == _build().manifest_digest
    _refused("bad_field", created_at=datetime(2026, 10, 26, 9, 0, 0))
    _refused("bad_field", created_at="2026-10-26T09:00:00Z")


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("workflow_version", ""),
        ("workflow_version", None),
        ("workflow_version", "latest"),
        ("workflow_version", "LATEST"),
        ("workflow_version", "wfv-*"),
        ("policy_version", "current"),
        ("policy_version", ""),
        ("prompt_versions", ["support"]),
        ("prompt_versions", ["support@latest"]),
        ("prompt_versions", ["@4"]),
        ("prompt_versions", ["support@"]),
        ("prompt_versions", [""]),
        ("tool_versions", ["lookup_case@HEAD"]),
        ("skill_versions", ["triage@current"]),
        ("model_routes", []),
        ("model_routes", ["default"]),
        ("model_routes", [None]),
        ("bundle_digest", ""),
        ("bundle_digest", None),
    ],
)
def test_unresolved_reference_refused(field: str, value: Any) -> None:
    with pytest.raises(ManifestError) as caught:
        _pins(**{field: value})
    assert caught.value.code == "unresolved_pin", caught.value


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("bundle_digest", "sha256:" + "a" * 64),
        ("bundle_digest", "A" * 64),
        ("bundle_digest", "a" * 63),
        ("bundle_digest", 5),
        ("workflow_version", " wfv-3"),
        ("workflow_version", "wfv\u200b3"),
        ("workflow_version", "x" * 513),
        ("workflow_version", 3),
        ("prompt_versions", "support@4"),
        ("prompt_versions", 4),
        ("tool_versions", "lookup_case@1"),
    ],
)
def test_malformed_references_are_refused_not_coerced(field: str, value: Any) -> None:
    with pytest.raises(ManifestError) as caught:
        _pins(**{field: value})
    assert caught.value.code == "bad_pin", caught.value


def test_a_knowledge_base_pin_must_be_resolved_and_unique() -> None:
    for kb_id, version in (("kb-1", ""), ("kb-1", "latest"), ("", "v1"), ("kb-1", None)):
        with pytest.raises(ManifestError):
            KnowledgeBasePin(kb_id, version)  # type: ignore[arg-type]
    with pytest.raises(ManifestError) as caught:
        _pins(
            knowledge_base_versions=[KnowledgeBasePin("kb-1", "v7"), KnowledgeBasePin("kb-1", "v7")]
        )
    assert caught.value.code == "duplicate_pin"
    several = _pins(
        knowledge_base_versions=[KnowledgeBasePin("kb-1", "v8"), KnowledgeBasePin("kb-1", "v7")]
    )
    assert [kb.version_id for kb in several.knowledge_base_versions] == ["v7", "v8"], (
        "one KB, two versions"
    )
    for bad in ("kb-1", [("kb-1", "v7")], 5):
        with pytest.raises(ManifestError) as typed:
            _pins(knowledge_base_versions=bad)
        assert typed.value.code == "bad_pin"
    assert _pins(knowledge_base_versions=[]).knowledge_base_versions == ()


def test_pinning_the_same_reference_twice_is_refused() -> None:
    for field in ("prompt_versions", "tool_versions", "skill_versions", "model_routes"):
        value = ["a@1", "a@1"] if field != "model_routes" else ["r", "r"]
        with pytest.raises(ManifestError) as caught:
            _pins(**{field: value})
        assert caught.value.code == "duplicate_pin"


def test_pins_are_immutable_and_normalised_to_tuples() -> None:
    pins = _pins()
    assert isinstance(pins.prompt_versions, tuple) and isinstance(
        pins.knowledge_base_versions, tuple
    )
    with pytest.raises(dataclasses.FrozenInstanceError):
        pins.policy_version = "pol-2"  # type: ignore[misc]


def test_strict_subject_shape_only_typed_principals_are_accepted() -> None:
    _refused("bad_field", subject={"user_id": "alice", "auth_method": "session"})
    _refused(
        "bad_field", workload={"workload_id": "wl_x", "name": "n", "workflow_version_digest": "d"}
    )
    _refused("bad_pin", pins={"workflow_version": "wfv-3"})
    _refused("bad_scope", scope={"tenant_id": "local"})
    _refused("bad_field", manifest_id="")
    _refused("bad_field", workflow_run_id=" run-1")
    _refused("bad_field", purpose="")
    _refused("bad_field", purpose="why\nbecause")


def test_the_scope_is_the_servers_and_a_caller_cannot_switch_organization_or_project() -> None:
    assert _build(requested_tenant_id="local").manifest_digest == _build().manifest_digest
    _refused("organization_switch", requested_tenant_id="other-org")
    _refused("organization_switch", requested_tenant_id="")
    _refused("project_mismatch", subject=Subject("alice", "session", "proj-2"))
    _refused("project_mismatch", subject=Subject("alice", "session", None))
    _refused("project_mismatch", scope=DeploymentScope.fixed(None))
    outside = _build(
        subject=Subject("alice", "session", None), scope=DeploymentScope.fixed(None)
    ).record
    assert "project_id" not in outside, "a run outside any project names none"
    validate_record("run_manifest", outside)
    for fields in (
        ("", "proj-1", "local", "local"),
        ("local", "", "local", "local"),
        ("local", "proj-1", "", "local"),
        ("local", "proj-1", "local", " local"),
    ):
        with pytest.raises(ManifestError) as caught:
            DeploymentScope(*fields)
        assert caught.value.code == "bad_scope"
    assert DeploymentScope.fixed("p") == DeploymentScope("local", "p", "local", "local")


def test_bundle_bytes_pinned_before_insert(tmp_path: Path) -> None:
    path = tmp_path / "rules.yaml"
    path.write_bytes(RULE_BUNDLE.read_bytes())
    pinned = pin_rule_bundle(path, RULE_BUNDLE_SHA)
    built = _build(rule_bundle=pinned, rule_bundles_enabled=True)
    record = built.record
    assert (record["rule_bundle_path"], record["rule_bundle_sha256"]) == (
        str(path),
        RULE_BUNDLE_SHA,
    )
    validate_record("run_manifest", record)
    # The digest is of the bytes verified now: changing the file afterwards does not alter the manifest ...
    path.write_bytes(b"changed: true\n")
    assert (
        _build(rule_bundle=pinned, rule_bundles_enabled=True).manifest_digest
        == built.manifest_digest
    )
    # ... and the changed bytes can no longer be pinned at all.
    with pytest.raises(ManifestError) as caught:
        pin_rule_bundle(path, RULE_BUNDLE_SHA)
    assert caught.value.code == "rule_bundle_refused" and "digest_mismatch" in str(caught.value)


@pytest.mark.parametrize(
    ("case", "expected"),
    [
        ("missing", "missing_bytes"),
        ("wrong_digest", "digest_mismatch"),
        ("bad_digest", "bad_expected_digest"),
        ("directory", "missing_bytes"),
    ],
)
def test_a_missing_or_changed_bundle_prevents_admission(
    tmp_path: Path, case: str, expected: str
) -> None:
    path = tmp_path / "rules.yaml"
    path.write_bytes(RULE_BUNDLE.read_bytes())
    digest = RULE_BUNDLE_SHA
    if case == "missing":
        path = tmp_path / "absent.yaml"
    elif case == "wrong_digest":
        digest = "0" * 64
    elif case == "bad_digest":
        digest = "ABC"
    elif case == "directory":
        path = tmp_path
    with pytest.raises(ManifestError) as caught:
        pin_rule_bundle(path, digest)
    assert caught.value.code == "rule_bundle_refused"
    assert expected in str(caught.value)


def test_an_enabled_bundle_is_required_and_a_disabled_one_is_not_silently_ignored(
    tmp_path: Path,
) -> None:
    pinned = pin_rule_bundle(RULE_BUNDLE, RULE_BUNDLE_SHA)
    _refused("rule_bundle_required", rule_bundles_enabled=True)
    _refused("rule_bundle_unexpected", rule_bundle=pinned)
    _refused("rule_bundle_required", rule_bundle="rules.yaml", rule_bundles_enabled=True)
    with pytest.raises(ManifestError):
        PinnedRuleBundle("", pinned.bundle)
    with pytest.raises(ManifestError):
        PinnedRuleBundle("rules.yaml", "bundle")  # type: ignore[arg-type]


def test_old_fixture_readable_and_a_stricter_writer_does_not_orphan_older_records() -> None:
    for name in ("valid_1.json", "valid_2.json"):
        raw = (FIXTURES / "contracts" / "run_manifest" / name).read_bytes()
        record = read_manifest(raw, hashlib.sha256(raw).hexdigest())
        assert record == json.loads(raw)
    legacy_shaped = json.loads(
        (FIXTURES / "contracts" / "run_manifest" / "valid_1.json").read_bytes()
    )
    assert legacy_shaped["pins"]["bundle_digest"] == "sha256:aaaa", (
        "not a bare digest: the builder would refuse it"
    )
    built = _build()
    assert read_manifest(built.manifest_bytes, built.manifest_digest) == built.record


@pytest.mark.parametrize(
    "raw",
    [
        b"",
        b"[]",
        b"not json",
        b'{"schema_version": "v1", "schema_version": "v1"}',
        b'{"schema_version": "v1", "x": NaN}',
        b'{"schema_version": "v1"}',
        b'{"schema_version": "v9"}',
        b"\xff\xfe",
    ],
)
def test_reading_a_malformed_manifest_is_a_typed_refusal(raw: bytes) -> None:
    with pytest.raises(ManifestError) as caught:
        read_manifest(raw)
    assert caught.value.code == "invalid_manifest"


def test_reading_checks_the_digest_when_one_is_given() -> None:
    built = _build()
    with pytest.raises(ManifestError, match="do not match their digest"):
        read_manifest(built.manifest_bytes, "0" * 64)
    with pytest.raises(ManifestError):
        read_manifest("text", None)  # type: ignore[arg-type]


def test_the_v1_schema_accepts_a_project_id_and_refuses_an_empty_one() -> None:
    record = _build().record
    validate_record("run_manifest", record)
    without = {k: v for k, v in record.items() if k != "project_id"}
    validate_record("run_manifest", without)
    with pytest.raises(ContractValidationError):
        validate_record("run_manifest", {**record, "project_id": ""})
    with pytest.raises(ContractValidationError):
        validate_record("run_manifest", {**record, "tenant": "x"})


def test_refusal_codes_are_a_closed_set() -> None:
    with pytest.raises(ValueError, match="unknown manifest refusal code"):
        ManifestError("typo_code", "x")
    assert {"unresolved_pin", "organization_switch", "rule_bundle_refused"} <= CODES


def test_the_builder_module_is_pure() -> None:
    """No database, session, network, clock or randomness in the module's builder path (the id helper is separate)."""
    source = Path(run_manifest_module.__file__).read_text()
    tree = ast.parse(source)
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported |= {alias.name.split(".")[0] for alias in node.names}
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module)
    forbidden = {
        "sqlalchemy",
        "httpx",
        "requests",
        "socket",
        "subprocess",
        "caliber.db",
        "caliber.db.models",
        "time",
        "random",
    }
    assert not (imported & forbidden), imported & forbidden
    builder = next(
        n
        for n in ast.walk(tree)
        if isinstance(n, ast.FunctionDef) and n.name == "build_run_manifest"
    )
    called = {
        n.func.attr if isinstance(n.func, ast.Attribute) else getattr(n.func, "id", "")
        for n in ast.walk(builder)
        if isinstance(n, ast.Call)
    }
    assert not (
        {"now", "utcnow", "uuid4", "open", "execute", "load_rule_bundle", "new_manifest_id"}
        & called
    )
    assert new_manifest_id() != new_manifest_id()


@pytest.mark.parametrize("dialect", DIALECTS)
def test_a_built_manifest_is_stored_and_read_back_byte_for_byte(
    dialect: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with _migrated(dialect, tmp_path, monkeypatch) as (engine, _):
        run_id = _add_run(engine)
        built = _build(workflow_run_id=run_id, manifest_id=new_manifest_id())
        with Session(engine) as session, session.begin():
            session.add(CaliberRunManifest(**built.row_values()))
        stored = _stored(engine, run_id)
        assert bytes(stored.manifest_bytes) == built.manifest_bytes
        assert (
            stored.manifest_digest
            == built.manifest_digest
            == digest_of(bytes(stored.manifest_bytes))
        )
        assert read_manifest(bytes(stored.manifest_bytes), stored.manifest_digest) == built.record


def test_an_unexpected_read_error_is_a_refusal_not_a_crash(monkeypatch: pytest.MonkeyPatch) -> None:
    def unreadable(path: object, expected: str) -> None:
        raise PermissionError("denied")

    monkeypatch.setattr(run_manifest_module, "load_rule_bundle", unreadable)
    with pytest.raises(ManifestError) as caught:
        pin_rule_bundle("rules.yaml", RULE_BUNDLE_SHA)
    assert caught.value.code == "rule_bundle_refused" and "PermissionError" in str(caught.value)
    assert "denied" not in str(caught.value), "the operating system's message is not echoed"


# ---------------------------------------------------------------- findings of the independent review (#955)


def test_a_generator_of_knowledge_base_pins_is_not_silently_lost() -> None:
    pins = _pins(knowledge_base_versions=(KnowledgeBasePin(f"kb-{i}", "v1") for i in (3, 1, 2)))
    assert [kb.knowledge_base_id for kb in pins.knowledge_base_versions] == ["kb-1", "kb-2", "kb-3"]
    assert len(_build(pins=pins).record["pins"]["knowledge_base_versions"]) == 3


@pytest.mark.parametrize(
    "value",
    [
        "latest.",
        "v-latest",
        "wfv:latest",
        "wfv/latest",
        "wfv@latest!",
        "wfv-?",
        "1.x",
        "^1.0",
        ">=1",
        "~1.2",
        "main",
        "nightly",
        "HEAD~1",
        "${LATEST}",
        "alias:current",
        "l\u0430test",  # not caught by folding (a Cyrillic a): the segment is not a floating word, see below
        "\uff2c\uff21\uff34\uff25\uff33\uff34",  # fullwidth LATEST folds to latest
        "\uff4c\uff41\uff54\uff45\uff53\uff54",
    ],
)
def test_floating_selectors_are_recognised_after_unicode_folding(value: str) -> None:
    if value == "l\u0430test":
        _pins(
            workflow_version=value
        )  # a homoglyph is a different word: resolution is the caller's duty
        return
    with pytest.raises(ManifestError) as caught:
        _pins(workflow_version=value)
    assert caught.value.code == "unresolved_pin", value


@pytest.mark.parametrize(
    "version",
    [
        "v-latest",
        "wfv:latest",
        "latest.",
        "1.x",
        ">=1",
        "\uff4c\uff41\uff54\uff45\uff53\uff54",
        "main",
    ],
)
def test_a_floating_version_of_a_named_prompt_is_refused_but_a_floating_looking_name_is_not(
    version: str,
) -> None:
    with pytest.raises(ManifestError) as caught:
        _pins(prompt_versions=[f"support@{version}"])
    assert caught.value.code == "unresolved_pin"
    assert _pins(prompt_versions=["production-support@4", "latest-news@2"]).prompt_versions == (
        "latest-news@2",
        "production-support@4",
    )


def test_a_pin_has_exactly_one_at_sign_and_model_routes_are_checked_too() -> None:
    for bad in ("a@b@1", "a@@1", "@1", "a@"):
        with pytest.raises(ManifestError):
            _pins(tool_versions=[bad])
    for route in ("gpt-4o-latest", "openai/latest", "default"):
        with pytest.raises(ManifestError):
            _pins(model_routes=[route])
    assert _pins(model_routes=["openai/gpt-4o-2024-08-06"]).model_routes


def test_text_must_be_nfc_and_free_of_line_separators_and_str_subclasses() -> None:
    _refused("bad_field", purpose="cafe\u0301")  # NFD
    _refused("bad_field", purpose="line\u2028separator")
    _refused("bad_field", purpose="para\u2029graph")

    class Loud(str):
        pass

    _refused("bad_field", purpose=Loud("why"))
    assert (
        _build(purpose="caf\u00e9 \u2713").manifest_bytes.decode("utf-8").count("caf\u00e9") == 1
    ), "non-ASCII is written as UTF-8, not as an escape"


def test_lists_and_ids_are_bounded() -> None:
    with pytest.raises(ManifestError):
        _pins(prompt_versions=[f"p{i}@1" for i in range(257)])
    with pytest.raises(ManifestError):
        _pins(model_routes=[f"r{i}" for i in range(257)])
    with pytest.raises(ManifestError):
        _pins(knowledge_base_versions=[KnowledgeBasePin(f"kb-{i}", "v1") for i in range(257)])
    _refused("bad_field", manifest_id="m" * 65)
    _refused("bad_field", workflow_run_id="r" * 65)
    assert _build(manifest_id="m" * 64, workflow_run_id="r" * 64)


def test_digests_must_match_in_full() -> None:
    for bad in ("a" * 65, "a" * 64 + "\n", "a" * 63 + "G"):
        with pytest.raises(ManifestError):
            _pins(bundle_digest=bad)


def test_the_scope_is_enforced_not_just_conventional() -> None:
    for scope in (
        DeploymentScope("evil-org", "proj-1", "local", "local"),
        DeploymentScope("local", "proj-1", "mars", "local"),
        DeploymentScope("local", "proj-1", "local", "mars"),
    ):
        _refused("bad_scope", scope=scope)


def test_a_rule_bundle_snapshot_cannot_be_forged_or_relative(tmp_path: Path) -> None:
    real = pin_rule_bundle(RULE_BUNDLE, RULE_BUNDLE_SHA)
    assert Path(real.path).is_absolute()
    with pytest.raises(ManifestError):
        PinnedRuleBundle("rules.yaml", real.bundle)
    forged = dataclasses.replace(real.bundle, sha256="zzzz")
    with pytest.raises(ManifestError):
        PinnedRuleBundle(real.path, forged)
    link = tmp_path / "link.yaml"
    link.symlink_to(RULE_BUNDLE)
    assert pin_rule_bundle(link, RULE_BUNDLE_SHA).path == str(RULE_BUNDLE.resolve()), (
        "symlinks are resolved"
    )


@pytest.mark.parametrize("path", ["a\x00b", 5, None, b"/x", ""])
def test_a_rule_bundle_path_that_is_not_a_path_is_a_refusal(path: Any) -> None:
    with pytest.raises(ManifestError) as caught:
        pin_rule_bundle(path, RULE_BUNDLE_SHA)
    assert caught.value.code == "rule_bundle_refused"


def test_a_digest_that_is_not_a_string_is_a_refusal() -> None:
    with pytest.raises(ManifestError) as caught:
        pin_rule_bundle(RULE_BUNDLE, None)  # type: ignore[arg-type]
    assert caught.value.code == "rule_bundle_refused"


def test_reading_refuses_what_it_cannot_store_or_digest() -> None:
    built = _build()
    record = built.record
    cases = {
        "deep": b"[" * 100000 + b"]" * 100000,
        "deep_object": b'{"a":' * 100000 + b"1" + b"}" * 100000,
        "bom": b"\xef\xbb\xbf" + built.manifest_bytes,
        "utf16": built.manifest_bytes.decode().encode("utf-16"),
        "huge": b" " * ((1 << 20) + 1) + built.manifest_bytes,
        "surrogate": json.dumps({**record, "purpose": "\ud800"}).encode(),
        "duplicate": built.manifest_bytes.replace(
            b'"legacy":false', b'"legacy":false,"legacy":false'
        ),
        "nan": built.manifest_bytes.replace(b'"legacy":false', b'"legacy":NaN'),
    }
    for name, raw in cases.items():
        with pytest.raises(ManifestError) as caught:
            read_manifest(raw)
        assert caught.value.code == "invalid_manifest", name


def test_a_manifest_that_disagrees_with_itself_cannot_be_constructed() -> None:
    built = _build()
    for args in (
        (built.manifest_bytes, "0" * 64),
        (built.manifest_bytes, 5),
        ("text", built.manifest_digest),
        (b"garbage", digest_of(b"garbage")),
        (b"{}", digest_of(b"{}")),
    ):
        with pytest.raises(ManifestError):
            BuiltManifest(*args)  # type: ignore[arg-type]
    assert BuiltManifest(built.manifest_bytes, built.manifest_digest) == built


def test_the_row_carries_the_records_own_creation_time() -> None:
    built = _build(
        created_at=datetime(2026, 10, 26, 11, 0, 0, 5, tzinfo=timezone(timedelta(hours=2)))
    )
    assert built.row_values()["created_at"] == datetime(2026, 10, 26, 9, 0, 0, 5)


def test_an_unrepresentable_timestamp_is_a_refusal_not_an_overflow() -> None:
    _refused("bad_field", created_at=datetime(1, 1, 1, tzinfo=timezone(timedelta(hours=2))))
    _refused(
        "bad_field", created_at=datetime(9999, 12, 31, 23, tzinfo=timezone(timedelta(hours=-2)))
    )


def test_a_route_list_that_is_not_a_list_and_a_bundle_that_is_not_a_bundle_are_refused() -> None:
    for bad in ("scripted", 5, None):
        with pytest.raises(ManifestError) as caught:
            _pins(model_routes=bad)
        assert caught.value.code in {"bad_pin", "unresolved_pin"}
    with pytest.raises(ManifestError, match="verified RuleBundle"):
        PinnedRuleBundle("/etc/caliber/rules.yaml", "bundle")  # type: ignore[arg-type]


def test_a_request_digest_is_recorded_when_given_and_absent_otherwise() -> None:
    digest = hashlib.sha256(b"the canonical admission intent").hexdigest()
    assert "request_digest" not in read_manifest(_build().manifest_bytes), (
        "optional, so older readers agree"
    )
    built = _build(request_digest=digest)
    assert read_manifest(built.manifest_bytes, built.manifest_digest)["request_digest"] == digest
    assert built.manifest_digest != _build().manifest_digest, "the digest is part of what is sealed"


@pytest.mark.parametrize("bad", ["", "abc", "A" * 64, "g" * 64, "sha256:" + "a" * 64])
def test_a_malformed_request_digest_is_refused(bad: str) -> None:
    with pytest.raises(ManifestError) as caught:
        _build(request_digest=bad)
    assert caught.value.code in {"bad_pin", "unresolved_pin"}


def test_the_schema_does_not_accept_a_request_digest_with_a_trailing_newline() -> None:
    from caliber.contracts import ContractValidationError, validate_record

    record = read_manifest(_build().manifest_bytes)
    validate_record("run_manifest", {**record, "request_digest": "a" * 64})
    with pytest.raises(ContractValidationError):
        validate_record("run_manifest", {**record, "request_digest": "a" * 64 + "\n"})


def test_the_grant_id_is_recorded_when_given_and_validated() -> None:
    assert "grant_id" not in read_manifest(_build().manifest_bytes)
    built = _build(grant_id="gr-abc")
    assert read_manifest(built.manifest_bytes, built.manifest_digest)["grant_id"] == "gr-abc"
    for bad in ("", " ", "x" * 65, "gr\x00"):
        with pytest.raises(ManifestError) as caught:
            _build(grant_id=bad)
        assert caught.value.code == "bad_field"
