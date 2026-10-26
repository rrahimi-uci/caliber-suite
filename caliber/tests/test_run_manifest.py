"""Persisted run manifests: written once, one per run, and never invented for a legacy run (B02.1a, #656).

Every case runs on SQLite and, where ``CALIBER_TEST_POSTGRES_URL`` is set (the "Migration parity (PostgreSQL)"
job), on PostgreSQL, against a database migrated by Alembic so the migration's triggers and constraints are the
thing under test rather than the ORM metadata alone.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, delete, inspect, select, text, update
from sqlalchemy.engine import Engine
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.orm import Session

from caliber.db.immutable import ImmutableRecordError, guard_immutable
from caliber.db.models import CaliberRunManifest, CaliberWorkflowRun
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
    """Core and raw SQL skip the ORM guard entirely; only the trigger is left, and it holds. A statement that
    matches no row is refused too on PostgreSQL's row triggers only when a row matches, so seed one first."""
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
            with engine.connect() as connection:
                connection.execute(text(f'SET ROLE "{role}"'))
                connection.execute(
                    text(
                        "INSERT INTO caliber_run_manifests (manifest_id, workflow_run_id, schema_version, "
                        "manifest_bytes, manifest_digest) VALUES ('m-role', :run, 'v1', :bytes, :digest)"
                    ),
                    {"run": run_id, "bytes": raw, "digest": hashlib.sha256(raw).hexdigest()},
                )
                connection.commit()
                for sql in (
                    "UPDATE caliber_run_manifests SET schema_version = 'v2'",
                    "DELETE FROM caliber_run_manifests",
                    "TRUNCATE caliber_run_manifests",
                ):
                    with pytest.raises(DBAPIError, match="permission denied"):
                        connection.execute(text(sql))
                    connection.rollback()
                assert (
                    connection.execute(
                        text("SELECT COUNT(*) FROM caliber_run_manifests")
                    ).scalar_one()
                    == 1
                )
                connection.execute(
                    text("RESET ROLE")
                )  # the pooled connection must not stay the role
                connection.commit()
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

            with pytest.raises(IntegrityError, match="a legacy run cannot have a manifest"):
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
