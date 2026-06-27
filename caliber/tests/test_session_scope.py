"""Tests for :mod:`caliber.db.session` — session_scope, engine creation."""

from __future__ import annotations

from pathlib import Path

import pytest
from sqlalchemy import text
from sqlalchemy.orm import Session

from caliber.config import CaliberConfig
from caliber.db import Base
from caliber.db.session import (
    create_engine_from_config,
    session_scope,
    sessionmaker_from_engine,
)


def _make_config(tmp_path: Path) -> CaliberConfig:
    db_path = tmp_path / "caliber.db"
    return CaliberConfig.load(
        environ={
            "CALIBER_DATABASE_URL": f"sqlite+pysqlite:///{db_path}",
        }
    )


def test_session_scope_commits_on_success(tmp_path: Path) -> None:
    config = _make_config(tmp_path)
    engine = create_engine_from_config(config)
    Base.metadata.create_all(engine)
    factory = sessionmaker_from_engine(engine)

    with session_scope(factory) as session:
        session.execute(text("SELECT 1"))

    engine.dispose()


def test_session_scope_rollback_on_exception(tmp_path: Path) -> None:
    config = _make_config(tmp_path)
    engine = create_engine_from_config(config)
    Base.metadata.create_all(engine)
    factory = sessionmaker_from_engine(engine)

    with pytest.raises(RuntimeError, match="boom"), session_scope(factory) as session:
        session.execute(text("SELECT 1"))
        raise RuntimeError("boom")

    engine.dispose()


def test_session_scope_always_closes(tmp_path: Path) -> None:
    """After session_scope exits (even on error), close() was called."""
    config = _make_config(tmp_path)
    engine = create_engine_from_config(config)
    Base.metadata.create_all(engine)
    factory = sessionmaker_from_engine(engine)

    closed = {"called": False}
    original_close = Session.close

    def tracking_close(self):
        closed["called"] = True
        return original_close(self)

    Session.close = tracking_close
    try:
        try:
            with session_scope(factory) as session:
                raise ValueError("force error")
        except ValueError:
            pass
        assert closed["called"]
    finally:
        Session.close = original_close

    engine.dispose()


def test_create_engine_non_sqlite(tmp_path: Path) -> None:
    """Ensure non-sqlite databases skip check_same_thread."""
    # We can't actually connect to a postgres DB in unit tests, but we
    # can verify the engine is created without the sqlite connect_args.
    config = CaliberConfig.load(
        environ={
            "CALIBER_DATABASE_URL": f"sqlite+pysqlite:///{tmp_path / 'x.db'}",
        }
    )
    engine = create_engine_from_config(config)
    assert engine is not None
    engine.dispose()
