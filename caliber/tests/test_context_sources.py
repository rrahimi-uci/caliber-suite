"""The approved context source inventory (B07.1a, #692).

Every case runs on SQLite and, where ``CALIBER_TEST_POSTGRES_URL`` is set, on PostgreSQL, against a database migrated by
Alembic.
"""

from __future__ import annotations

import os
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, func, inspect, select
from sqlalchemy.engine import Engine
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from caliber.context import sources
from caliber.context.sources import (
    INVALID_ARGUMENT,
    NOT_APPROVED,
    SOURCE_NOT_FOUND,
    SourceError,
    approve_source,
    approved_sources,
    is_approved,
    revoke_source,
)
from caliber.db.models import (
    CaliberApprovedContextSource,
    CaliberAuditLog,
    CaliberKnowledgeBase,
    CaliberKnowledgeBaseVersion,
    CaliberProject,
)
from tests.dialect_helpers import DIALECTS, dialect_database_url

PROJECT_ROOT = Path(__file__).resolve().parent.parent
ALEMBIC_INI = PROJECT_ROOT / "alembic.ini"


@contextmanager
def _database(
    dialect: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Iterator[tuple[Engine, sessionmaker[Session]]]:
    with dialect_database_url(dialect, tmp_path, "context_sources") as url:
        monkeypatch.setenv("CALIBER_DATABASE_URL", url)
        monkeypatch.chdir(PROJECT_ROOT)
        command.upgrade(Config(str(ALEMBIC_INI)), "head")
        engine = create_engine(url)
        try:
            yield engine, sessionmaker(engine, expire_on_commit=False)
        finally:
            engine.dispose()


def _seed(factory: sessionmaker[Session]) -> None:
    """Project A owns KB-A (versions 1 and 2); project B owns KB-B (version 1)."""
    with factory() as session:
        session.add_all(
            [
                CaliberProject(project_id="P-A", name="A", owner="@a"),
                CaliberProject(project_id="P-B", name="B", owner="@b"),
            ]
        )
        session.flush()
        for kb, project in (("KB-A", "P-A"), ("KB-B", "P-B")):
            session.add(
                CaliberKnowledgeBase(
                    knowledge_base_id=kb,
                    name=kb,
                    owner="@x",
                    project_id=project,
                    visibility="project",
                    status="active",
                    source_bucket="b",
                )
            )
        session.flush()
        for kb, numbers in (("KB-A", (1, 2)), ("KB-B", (1,))):
            for number in numbers:
                session.add(
                    CaliberKnowledgeBaseVersion(
                        knowledge_base_version_id=f"{kb}-V{number}",
                        knowledge_base_id=kb,
                        version_number=number,
                        status="ready",
                        chunking_strategy="fixed",
                        embedding_model="m",
                        output_bucket="b",
                        output_prefix="p",
                    )
                )
        session.commit()


def _count(factory: sessionmaker[Session]) -> int:
    with factory() as session:
        return int(
            session.execute(
                select(func.count()).select_from(CaliberApprovedContextSource)
            ).scalar_one()
        )


@pytest.mark.parametrize("dialect", DIALECTS)
def test_approve_and_revoke(dialect: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    with _database(dialect, tmp_path, monkeypatch) as (_, factory):
        _seed(factory)
        with factory() as session:
            assert not is_approved(session, "P-A", "KB-A", "KB-A-V1")
            row = approve_source(session, "P-A", "KB-A", "KB-A-V1", "@editor")
            session.commit()
            assert row.id.startswith("CAS-") and row.approved_by == "@editor"
            assert row.revoked_at is None and row.approved_at is not None
            assert is_approved(session, "P-A", "KB-A", "KB-A-V1")
            assert not is_approved(session, "P-A", "KB-A", "KB-A-V2"), (
                "a version, not the knowledge base"
            )
            assert not is_approved(session, "P-B", "KB-A", "KB-A-V1"), "a project, not everyone"

            revoked = revoke_source(session, "P-A", "KB-A", "KB-A-V1", "@owner")
            session.commit()
            assert revoked.revoked_at is not None and revoked.revoked_by == "@owner"
            assert not is_approved(session, "P-A", "KB-A", "KB-A-V1")
            assert _count(factory) == 1, "the row stays as evidence"
            with pytest.raises(SourceError) as again:
                revoke_source(session, "P-A", "KB-A", "KB-A-V1", "@owner")
            assert again.value.code == NOT_APPROVED
            session.rollback()


@pytest.mark.parametrize("dialect", DIALECTS)
def test_approval_is_idempotent_and_a_revoked_source_can_be_approved_again(
    dialect: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with _database(dialect, tmp_path, monkeypatch) as (_, factory):
        _seed(factory)
        with factory() as session:
            first = approve_source(session, "P-A", "KB-A", "KB-A-V1", "@one")
            again = approve_source(session, "P-A", "KB-A", "KB-A-V1", "@two")
            session.commit()
            assert again.id == first.id and again.approved_by == "@one", "the first approval stands"
            revoke_source(session, "P-A", "KB-A", "KB-A-V1", "@owner")
            back = approve_source(session, "P-A", "KB-A", "KB-A-V1", "@three")
            session.commit()
            assert back.id == first.id and back.approved_by == "@three"
            assert back.revoked_at is None and back.revoked_by is None
            assert is_approved(session, "P-A", "KB-A", "KB-A-V1")
            assert [r.version_id for r in approved_sources(session, "P-A")] == ["KB-A-V1"]
            approve_source(session, "P-A", "KB-A", "KB-A-V2", "@one")
            session.commit()
            assert [r.version_id for r in approved_sources(session, "P-A")] == [
                "KB-A-V1",
                "KB-A-V2",
            ]
            assert approved_sources(session, "P-B") == []
        assert _count(factory) == 2


@pytest.mark.parametrize("dialect", DIALECTS)
def test_only_a_projects_own_real_sources_can_be_approved_with_one_uniform_refusal(
    dialect: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with _database(dialect, tmp_path, monkeypatch) as (_, factory):
        _seed(factory)
        messages = set()
        with factory() as session:
            for project, kb, version in (
                ("P-A", "KB-B", "KB-B-V1"),  # another project's knowledge base
                ("P-B", "KB-A", "KB-A-V1"),
                ("P-A", "KB-A", "KB-B-V1"),  # a version of another knowledge base
                ("P-A", "KB-A", "KB-A-V9"),  # unknown version
                ("P-A", "KB-nope", "KB-A-V1"),  # unknown knowledge base
                ("P-nope", "KB-A", "KB-A-V1"),  # unknown project
            ):
                with pytest.raises(SourceError) as refused:
                    approve_source(session, project, kb, version, "@editor")
                assert refused.value.code == SOURCE_NOT_FOUND
                messages.add(str(refused.value))
        assert len(messages) == 1, "one message for every kind of miss"
        assert not any(x in next(iter(messages)) for x in ("KB-", "P-A", "P-B")), "no identifier"
        assert _count(factory) == 0


@pytest.mark.parametrize("dialect", DIALECTS)
def test_arguments_are_validated(
    dialect: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with _database(dialect, tmp_path, monkeypatch) as (_, factory):
        _seed(factory)
        with factory() as session:
            bad: list[dict[str, Any]] = [
                {"project_id": ""},
                {"project_id": "x" * 65},
                {"knowledge_base_id": "  "},
                {"version_id": "v\x00"},
                {"actor": ""},
                {"actor": "a" * 257},
                {"actor": None},
            ]
            for override in bad:
                arguments: dict[str, Any] = {
                    "project_id": "P-A",
                    "knowledge_base_id": "KB-A",
                    "version_id": "KB-A-V1",
                    "actor": "@editor",
                    **override,
                }
                for function in (approve_source, revoke_source):
                    with pytest.raises(SourceError) as caught:
                        function(session, **arguments)
                    assert caught.value.code == INVALID_ARGUMENT, override
        assert _count(factory) == 0


@pytest.mark.parametrize("dialect", DIALECTS)
def test_approve_and_revoke_are_audited_without_secrets(
    dialect: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with _database(dialect, tmp_path, monkeypatch) as (_, factory):
        _seed(factory)
        with factory() as session:
            row = approve_source(session, "P-A", "KB-A", "KB-A-V1", "@editor")
            approve_source(session, "P-A", "KB-A", "KB-A-V1", "@other")  # a no-op: no second row
            revoke_source(session, "P-A", "KB-A", "KB-A-V1", "@owner")
            approve_source(session, "P-A", "KB-A", "KB-A-V1", "@editor")
            session.commit()
            rows = list(
                session.execute(
                    select(CaliberAuditLog)
                    .where(CaliberAuditLog.entity_type == "context_source")
                    .order_by(CaliberAuditLog.log_id)
                ).scalars()
            )
        assert [(r.action, r.actor) for r in rows] == [
            ("approve_context_source", "@editor"),
            ("revoke_context_source", "@owner"),
            ("approve_context_source", "@editor"),
        ]
        assert all(r.entity_id == row.id for r in rows)
        assert rows[0].details == {
            "project_id": "P-A",
            "knowledge_base_id": "KB-A",
            "version_id": "KB-A-V1",
        }


@pytest.mark.parametrize("dialect", DIALECTS)
def test_the_unique_key_is_enforced_by_the_database(
    dialect: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with _database(dialect, tmp_path, monkeypatch) as (_, factory):
        _seed(factory)
        with factory() as session:
            approve_source(session, "P-A", "KB-A", "KB-A-V1", "@editor")
            session.commit()
            session.add(
                CaliberApprovedContextSource(
                    id="CAS-dup",
                    project_id="P-A",
                    knowledge_base_id="KB-A",
                    version_id="KB-A-V1",
                    approved_by="@x",
                )
            )
            with pytest.raises(IntegrityError):
                session.flush()
            session.rollback()
            if dialect == "postgresql":  # SQLite does not enforce foreign keys unless asked to
                session.add(
                    CaliberApprovedContextSource(
                        id="CAS-orphan",
                        project_id="P-nope",
                        knowledge_base_id="KB-A",
                        version_id="KB-A-V1",
                        approved_by="@x",
                    )
                )
                with pytest.raises(IntegrityError):  # the project foreign key
                    session.flush()
                session.rollback()


@pytest.mark.parametrize("dialect", DIALECTS)
def test_losing_the_insert_race_returns_the_winner(
    dialect: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with _database(dialect, tmp_path, monkeypatch) as (_, factory):
        _seed(factory)
        with factory() as winner:
            won = approve_source(winner, "P-A", "KB-A", "KB-A-V1", "@winner")
            winner.commit()
        with factory() as session:
            real = sources._find
            state = {"first": True}

            def stale(*args: Any) -> Any:
                if state["first"]:
                    state["first"] = False
                    return None  # the read happened before the other transaction committed
                return real(*args)

            monkeypatch.setattr(sources, "_find", stale)
            lost = approve_source(session, "P-A", "KB-A", "KB-A-V1", "@loser")
            session.commit()
            assert lost.id == won.id and lost.approved_by == "@winner"
        assert _count(factory) == 1


def test_concurrent_approvals_on_postgresql_create_one_row(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    if not os.environ.get("CALIBER_TEST_POSTGRES_URL"):
        pytest.skip("CALIBER_TEST_POSTGRES_URL is not set")
    with _database("postgresql", tmp_path, monkeypatch) as (_, factory):
        _seed(factory)
        barrier = threading.Barrier(4)
        results: list[str] = []
        errors: list[BaseException] = []

        def approve() -> None:
            try:
                with factory() as session:
                    barrier.wait(10)
                    row = approve_source(session, "P-A", "KB-A", "KB-A-V1", "@racer")
                    session.commit()
                    results.append(row.id)
            except BaseException as exc:  # pragma: no cover - reported below
                errors.append(exc)

        threads = [threading.Thread(target=approve, daemon=True) for _ in range(4)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(30)
        assert errors == [] and len(results) == 4 and len(set(results)) == 1
        assert _count(factory) == 1


@pytest.mark.parametrize("dialect", DIALECTS)
def test_the_migration_rolls_back_cleanly(
    dialect: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with _database(dialect, tmp_path, monkeypatch) as (engine, _):
        assert "caliber_approved_context_sources" in inspect(engine).get_table_names()
        command.downgrade(Config(str(ALEMBIC_INI)), "0119")
        assert "caliber_approved_context_sources" not in inspect(engine).get_table_names()
        command.upgrade(Config(str(ALEMBIC_INI)), "head")
        assert "caliber_approved_context_sources" in inspect(engine).get_table_names()
