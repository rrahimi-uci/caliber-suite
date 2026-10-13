"""Readiness reports an unmigrated or out-of-date database as not ready (alpha gap P1-03).

Before this check, ``/readiness`` probed the database with ``SELECT 1`` only. On a
database that had never been migrated it answered ``ready: true`` with no blockers while
every API route failed with ``no such table``. These tests use real SQLite files migrated
with the real packaged migrations, so the verdict is the one an operator would see.
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterator
from pathlib import Path

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker
from starlette.testclient import TestClient

from caliber.config import CaliberConfig
from caliber.db.migrate import VERSION_TABLE, head_revision, main, schema_status
from caliber.observability.readiness import collect_readiness
from caliber.routes.health import HEALTH_PATH, READINESS_PATH


@pytest.fixture
def db_url(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> str:
    url = f"sqlite:///{tmp_path / 'schema.db'}"
    monkeypatch.setenv("CALIBER_DATABASE_URL", url)
    return url


@pytest.fixture
def factory(db_url: str) -> Iterator[sessionmaker[Session]]:
    engine: Engine = create_engine(db_url)
    try:
        yield sessionmaker(engine)
    finally:
        engine.dispose()


def _report(factory: sessionmaker[Session] | None) -> object:
    return asyncio.run(
        collect_readiness(config=CaliberConfig(), session_factory=factory, environ={})
    )


def _schema_check(report: object) -> object:
    return next(check for check in report.checks if check.name == "schema")  # type: ignore[attr-defined]


def test_an_empty_database_is_uninitialised(factory: sessionmaker[Session]) -> None:
    with factory() as session:
        status = schema_status(session)
    assert status.state == "uninitialised"
    assert status.current is None
    assert status.head == head_revision()
    assert not status.ok
    assert "caliber-migrate" in status.describe()


def test_an_empty_version_table_is_uninitialised(factory: sessionmaker[Session]) -> None:
    with factory() as session:
        session.execute(text(f"CREATE TABLE {VERSION_TABLE} (version_num VARCHAR(32))"))
        session.commit()
        assert schema_status(session).state == "uninitialised"


def test_a_migrated_database_is_current(db_url: str, factory: sessionmaker[Session]) -> None:
    assert main([]) == 0
    with factory() as session:
        status = schema_status(session)
    assert status.state == "current"
    assert status.ok
    assert status.current == status.head == head_revision()
    assert status.describe() == f"schema at head {head_revision()}"


def test_an_older_revision_is_behind_with_a_pending_count(
    db_url: str, factory: sessionmaker[Session]
) -> None:
    assert main(["0100"]) == 0
    with factory() as session:
        status = schema_status(session)
    assert status.state == "behind"
    assert status.current == "0100"
    assert status.pending == int(head_revision() or "0") - 100
    assert f"{status.pending} migration(s) pending" in status.describe()


def test_one_migration_behind_reports_one_pending(
    db_url: str, factory: sessionmaker[Session]
) -> None:
    previous = f"{int(head_revision() or '0') - 1:04d}"
    assert main([previous]) == 0
    with factory() as session:
        status = schema_status(session)
    assert (status.state, status.pending) == ("behind", 1)


def test_a_revision_this_release_does_not_ship_is_unknown(
    db_url: str, factory: sessionmaker[Session]
) -> None:
    assert main([]) == 0
    with factory() as session:
        session.execute(text(f"UPDATE {VERSION_TABLE} SET version_num = '9999_from_the_future'"))
        session.commit()
        status = schema_status(session)
    assert status.state == "unknown"
    assert "newer version" in status.describe()


def test_readiness_blocks_on_an_unmigrated_database(factory: sessionmaker[Session]) -> None:
    report = _report(factory)
    check = _schema_check(report)

    # The database is reachable; the schema is not there.
    assert next(c for c in report.checks if c.name == "database").ready is True  # type: ignore[attr-defined]
    assert check.ready is False  # type: ignore[attr-defined]
    assert check.required is True  # type: ignore[attr-defined]
    assert report.ready is False  # type: ignore[attr-defined]
    assert any("schema" in b and "caliber-migrate" in b for b in report.blockers)  # type: ignore[attr-defined]


def test_readiness_blocks_when_the_database_is_behind(
    db_url: str, factory: sessionmaker[Session]
) -> None:
    assert main(["0100"]) == 0
    report = _report(factory)

    assert report.ready is False  # type: ignore[attr-defined]
    assert any("behind" in b for b in report.blockers)  # type: ignore[attr-defined]


def test_readiness_is_ready_at_head_and_names_the_revision(
    db_url: str, factory: sessionmaker[Session]
) -> None:
    assert main([]) == 0
    report = _report(factory)
    check = _schema_check(report)

    assert check.ready is True  # type: ignore[attr-defined]
    assert head_revision() in check.detail  # type: ignore[attr-defined]
    assert not any("schema" in b for b in report.blockers)  # type: ignore[attr-defined]


def test_the_schema_check_is_skipped_without_a_database() -> None:
    check = _schema_check(_report(None))
    assert check.ready is None  # type: ignore[attr-defined]
    assert check.required is False  # type: ignore[attr-defined]


def test_the_readiness_route_is_503_on_an_unmigrated_database_while_health_stays_up(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from caliber.server import create_app

    db = tmp_path / "route.db"
    monkeypatch.setenv("CALIBER_DATABASE_URL", f"sqlite+pysqlite:///{db}")
    config = CaliberConfig.load(
        environ={
            "CALIBER_DATABASE_URL": f"sqlite+pysqlite:///{db}",
            "CALIBER_AUTH_MODE": "trusted_header",
            "CALIBER_BACKGROUND_TASKS_ENABLED": "false",
        }
    )
    app = create_app(config=config)
    try:
        with TestClient(app) as client:
            readiness = client.get(READINESS_PATH)
            assert readiness.status_code == 503, readiness.text
            data = readiness.json()["data"]
            assert data["ready"] is False
            assert any("schema" in b for b in data["blockers"])

            # Liveness is a different question: the process is up and the database answers.
            assert client.get(HEALTH_PATH).status_code == 200

            assert main([]) == 0
            after = client.get(READINESS_PATH)
            assert after.status_code == 200, after.text
            assert after.json()["data"]["ready"] is True
    finally:
        app.state.engine.dispose()
