"""CALIBER's database and an MLflow store sharing one SQLite file (alpha gap P0-2).

That layout is what ``scripts/run-dev.sh`` and the browser-test harness use. It works
since Apply commits before its MLflow call, but SQLite is single-writer, so startup
says so instead of staying silent.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from caliber import server
from caliber.config import CaliberConfig
from caliber.db_url import shared_sqlite_store, sqlite_file_path
from caliber.server import _warn_if_sqlite_store_is_shared

_TMP = Path("/tmp")


def _capture_warnings(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Collect what ``server.logger.warning`` is called with.

    Patching the call, like ``tests/test_server.py`` does, rather than using
    ``caplog``: another test earlier in a serial run can leave the ``caliber`` logger
    or the root handlers in a state where ``caplog`` sees nothing, which is exactly how
    these tests passed alone and failed in CI's full run.
    """
    messages: list[str] = []

    def _warning(message: str, *args: object, **_kwargs: object) -> None:
        messages.append(message % args if args else message)

    monkeypatch.setattr(server.logger, "warning", _warning)
    return messages


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("sqlite:////tmp/a.db", (_TMP / "a.db").resolve()),
        ("sqlite+pysqlite:////tmp/a.db", (_TMP / "a.db").resolve()),
        ("sqlite:////tmp/a.db?timeout=30", (_TMP / "a.db").resolve()),
        ("sqlite:///rel/x.db", Path("rel/x.db").resolve()),
        ("sqlite:///:memory:", None),
        ("sqlite://", None),
        ("sqlite://host/path.db", None),
        ("postgresql+psycopg://u@h/db", None),
        ("http://127.0.0.1:5000", None),
        ("", None),
        (None, None),
    ],
)
def test_sqlite_file_path(url: str | None, expected: Path | None) -> None:
    assert sqlite_file_path(url) == expected


def test_shared_store_detects_the_same_file_however_it_is_spelled() -> None:
    shared = shared_sqlite_store("sqlite+pysqlite:////tmp/x.db", "sqlite:////tmp/x.db?timeout=30")
    assert shared == (_TMP / "x.db").resolve()
    assert shared_sqlite_store("sqlite:////tmp/x.db", None, "http://mlflow:5000") is None
    assert shared_sqlite_store("sqlite:////tmp/x.db", "sqlite:////tmp/y.db") is None
    assert shared_sqlite_store("postgresql+psycopg://u@h/db", "sqlite:////tmp/x.db") is None


def test_warns_when_an_mlflow_env_var_points_at_the_same_file(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    messages = _capture_warnings(monkeypatch)
    monkeypatch.setenv("MLFLOW_TRACKING_URI", "sqlite:////tmp/shared.db")

    _warn_if_sqlite_store_is_shared("sqlite+pysqlite:////tmp/shared.db")

    assert len(messages) == 1
    assert "share one SQLite file" in messages[0]
    assert str((_TMP / "shared.db").resolve()) in messages[0]


@pytest.mark.parametrize("env", ["MLFLOW_REGISTRY_URI", "MLFLOW_BACKEND_STORE_URI"])
def test_every_mlflow_store_variable_is_checked(monkeypatch: pytest.MonkeyPatch, env: str) -> None:
    messages = _capture_warnings(monkeypatch)
    monkeypatch.delenv("MLFLOW_TRACKING_URI", raising=False)
    monkeypatch.setenv(env, "sqlite:////tmp/shared.db")

    _warn_if_sqlite_store_is_shared("sqlite:////tmp/shared.db")

    assert any("share one SQLite file" in m for m in messages)


def test_no_warning_for_separate_files_or_a_remote_mlflow(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    messages = _capture_warnings(monkeypatch)
    for name in ("MLFLOW_TRACKING_URI", "MLFLOW_REGISTRY_URI", "MLFLOW_BACKEND_STORE_URI"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("MLFLOW_TRACKING_URI", "http://127.0.0.1:5000")
    _warn_if_sqlite_store_is_shared("sqlite:////tmp/caliber.db")
    monkeypatch.setenv("MLFLOW_TRACKING_URI", "sqlite:////tmp/mlflow.db")
    _warn_if_sqlite_store_is_shared("sqlite:////tmp/caliber.db")

    assert not any("share one SQLite file" in m for m in messages)


def test_create_app_emits_the_warning(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    from caliber.server import create_app

    messages = _capture_warnings(monkeypatch)
    db = tmp_path / "both.db"
    monkeypatch.setenv("MLFLOW_TRACKING_URI", f"sqlite:///{db}")
    config = CaliberConfig.load(
        environ={
            "CALIBER_DATABASE_URL": f"sqlite+pysqlite:///{db}",
            "CALIBER_AUTH_MODE": "trusted_header",
            "CALIBER_BACKGROUND_TASKS_ENABLED": "false",
        }
    )

    app = create_app(config=config)
    app.state.engine.dispose()

    assert any("share one SQLite file" in m for m in messages)
