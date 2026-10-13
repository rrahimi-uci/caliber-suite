"""``caliber-migrate`` brings an empty database to the Alembic head (alpha gap P0-5).

The command must work with no ``alembic.ini`` and from any working directory, which is
the situation of a pip-installed wheel. The subprocess tests run it from an empty temp
directory for that reason.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest
from alembic.script import ScriptDirectory
from sqlalchemy import create_engine, inspect, text

from caliber.db.migrate import SCRIPT_LOCATION, build_config, main


def _head() -> str:
    head = ScriptDirectory.from_config(build_config()).get_current_head()
    assert head is not None
    return head


def _version(db: Path) -> str | None:
    engine = create_engine(f"sqlite:///{db}")
    try:
        with engine.connect() as conn:
            if "caliber_alembic_version" not in inspect(conn).get_table_names():
                return None
            return conn.execute(text("select version_num from caliber_alembic_version")).scalar()
    finally:
        engine.dispose()


def test_config_locates_the_migrations_inside_the_package() -> None:
    config = build_config()
    assert config.get_main_option("script_location") == SCRIPT_LOCATION
    assert config.config_file_name is None  # no alembic.ini involved
    assert _head()


def test_main_migrates_an_empty_database_to_head(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    db = tmp_path / "fresh.db"
    monkeypatch.setenv("CALIBER_DATABASE_URL", f"sqlite:///{db}")

    assert main([]) == 0

    assert _version(db) == _head()
    engine = create_engine(f"sqlite:///{db}")
    try:
        assert "caliber_user_accounts" in inspect(engine).get_table_names()
    finally:
        engine.dispose()
    out = capsys.readouterr().out
    assert "database is at head" in out
    assert str(db) not in out  # the URL is never echoed


def test_running_it_twice_is_a_no_op(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    db = tmp_path / "twice.db"
    monkeypatch.setenv("CALIBER_DATABASE_URL", f"sqlite:///{db}")

    assert main([]) == 0
    assert main([]) == 0
    assert _version(db) == _head()


def test_current_reports_without_migrating(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    db = tmp_path / "current.db"
    monkeypatch.setenv("CALIBER_DATABASE_URL", f"sqlite:///{db}")

    assert main(["--current"]) == 0

    assert _version(db) is None  # nothing was created


def test_missing_database_url_is_refused_with_a_hint(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.delenv("CALIBER_DATABASE_URL", raising=False)

    assert main([]) == 2

    assert "CALIBER_DATABASE_URL is not set" in capsys.readouterr().err


def test_blank_database_url_is_refused(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("CALIBER_DATABASE_URL", "   ")

    assert main([]) == 2


def test_runs_from_an_unrelated_directory(tmp_path: Path) -> None:
    """As an installed ``caliber-migrate`` would: no repo checkout, no alembic.ini."""
    workdir = tmp_path / "elsewhere"
    workdir.mkdir()
    db = tmp_path / "cwd.db"
    env = {**os.environ, "CALIBER_DATABASE_URL": f"sqlite:///{db}"}
    proc = subprocess.run(
        [sys.executable, "-m", "caliber.db.migrate"],
        cwd=workdir,
        env=env,
        capture_output=True,
        text=True,
        timeout=180,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr[-2000:]
    assert _version(db) == _head()
