"""An unreachable MLflow answers 503 quickly and says so (alpha gap P1-04).

With MLflow stopped, ``POST /prompts`` and Apply used to sit in MLflow's retry loop for
about eight minutes and then answer 502 with a client stack trace. These tests cover the
classifier, the bounded-retry defaults, and the user-visible outcome of registering a
prompt and applying a candidate against a tracking server that is not there.
"""

from __future__ import annotations

import json
import socket
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest
import requests
from mlflow.exceptions import MlflowException
from urllib3.exceptions import MaxRetryError, NewConnectionError

from caliber.mlflow_errors import (
    UNREACHABLE_DETAIL,
    is_mlflow_unreachable,
    unreachable_http_exception,
)


def _chained(outer: BaseException, cause: BaseException) -> BaseException:
    outer.__cause__ = cause
    return outer


@pytest.mark.parametrize(
    "exc",
    [
        ConnectionRefusedError("refused"),
        TimeoutError("slow"),
        requests.exceptions.ConnectionError("down"),
        requests.exceptions.ReadTimeout("slow"),
        MaxRetryError(None, "http://x", reason=None),  # type: ignore[arg-type]
        NewConnectionError(None, "boom"),  # type: ignore[arg-type]
        # MLflow wraps the typed error; only the cause carries it.
        _chained(MlflowException("API request failed"), requests.exceptions.ConnectionError()),
        # Text-only wrapping, as MLflow sometimes re-raises.
        MlflowException(
            "API request to http://h/api failed with exception HTTPConnectionPool: "
            "Max retries exceeded with url: /api (Caused by NewConnectionError(...))"
        ),
        RuntimeError("Connection refused by peer"),
        # Two levels deep through __context__.
        _chained(RuntimeError("outer"), _chained(ValueError("mid"), ConnectionResetError("x"))),
    ],
)
def test_connection_failures_are_recognised(exc: BaseException) -> None:
    assert is_mlflow_unreachable(exc) is True


@pytest.mark.parametrize(
    "exc",
    [
        MlflowException("RESOURCE_ALREADY_EXISTS: prompt exists"),
        ValueError("template is required"),
        PermissionError("denied"),
        KeyError("version"),
    ],
)
def test_other_failures_are_not_mistaken_for_an_outage(exc: BaseException) -> None:
    assert is_mlflow_unreachable(exc) is False


def test_a_cyclic_exception_chain_terminates() -> None:
    a = RuntimeError("a")
    b = RuntimeError("b")
    a.__cause__ = b
    b.__cause__ = a
    assert is_mlflow_unreachable(a) is False


def test_the_http_error_is_a_503_with_fixed_text_that_leaks_no_uri() -> None:
    error = unreachable_http_exception()
    assert error.status_code == 503
    assert error.detail == UNREACHABLE_DETAIL
    assert "MLflow" in UNREACHABLE_DETAIL
    assert "http" not in UNREACHABLE_DETAIL.lower()
    assert "@" not in UNREACHABLE_DETAIL


def test_create_app_bounds_mlflow_retries(monkeypatch: pytest.MonkeyPatch) -> None:
    from caliber.config import CaliberConfig
    from caliber.server import create_app

    monkeypatch.delenv("MLFLOW_HTTP_REQUEST_MAX_RETRIES", raising=False)
    monkeypatch.delenv("MLFLOW_HTTP_REQUEST_BACKOFF_FACTOR", raising=False)
    config = CaliberConfig.load(
        environ={
            "CALIBER_DATABASE_URL": "sqlite+pysqlite:///:memory:",
            "CALIBER_AUTH_MODE": "trusted_header",
            "CALIBER_BACKGROUND_TASKS_ENABLED": "false",
        }
    )
    app = create_app(config=config)
    try:
        import os

        assert os.environ["MLFLOW_HTTP_REQUEST_MAX_RETRIES"] == "1"
        assert os.environ["MLFLOW_HTTP_REQUEST_BACKOFF_FACTOR"] == "1"
    finally:
        app.state.engine.dispose()


def test_an_operator_override_of_the_retry_bound_is_kept(monkeypatch: pytest.MonkeyPatch) -> None:
    from caliber.config import CaliberConfig
    from caliber.server import create_app

    monkeypatch.setenv("MLFLOW_HTTP_REQUEST_MAX_RETRIES", "5")
    config = CaliberConfig.load(
        environ={
            "CALIBER_DATABASE_URL": "sqlite+pysqlite:///:memory:",
            "CALIBER_AUTH_MODE": "trusted_header",
            "CALIBER_BACKGROUND_TASKS_ENABLED": "false",
        }
    )
    app = create_app(config=config)
    try:
        import os

        assert os.environ["MLFLOW_HTTP_REQUEST_MAX_RETRIES"] == "5"
    finally:
        app.state.engine.dispose()


def _closed_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


_SCRIPT = textwrap.dedent(
    r"""
    import json, os, sys, time

    db, port = sys.argv[1], sys.argv[2]
    os.environ["MLFLOW_TRACKING_URI"] = f"http://127.0.0.1:{port}"
    # The test process may point MLflow's registry at a local store; this run must not.
    for name in ("MLFLOW_REGISTRY_URI", "MLFLOW_BACKEND_STORE_URI", "MLFLOW_HTTP_REQUEST_MAX_RETRIES"):
        os.environ.pop(name, None)

    from starlette.testclient import TestClient

    from caliber.config import CaliberConfig
    from caliber.db.base import Base
    from caliber.server import create_app

    config = CaliberConfig.load(
        environ={
            "CALIBER_DATABASE_URL": f"sqlite+pysqlite:///{db}",
            "CALIBER_AUTH_MODE": "trusted_header",
            "CALIBER_ADMIN_USERS": "@admin",
            "CALIBER_BACKGROUND_TASKS_ENABLED": "false",
        }
    )
    app = create_app(config=config)
    Base.metadata.create_all(app.state.engine)
    out = {}
    with TestClient(app, headers={"X-CALIBER-User": "@admin"}) as client:
        started = time.time()
        resp = client.post(
            "/ajax-api/2.0/mlflow/caliber/prompts",
            json={"name": "closed-port-prompt", "template": "hello {{a}}"},
        )
        out["seconds"] = round(time.time() - started, 1)
        out["status"] = resp.status_code
        out["detail"] = resp.json().get("detail")
    print("RESULT=" + json.dumps(out))
    """
)


def test_registering_a_prompt_with_mlflow_down_is_a_fast_503(tmp_path: Path) -> None:
    """The real MLflow client against a closed port, with the shipped retry defaults."""
    script = tmp_path / "closed_port.py"
    script.write_text(_SCRIPT, encoding="utf-8")
    proc = subprocess.run(
        [sys.executable, str(script), str(tmp_path / "c.db"), str(_closed_port())],
        capture_output=True,
        text=True,
        timeout=240,
        check=False,
    )
    lines = [line for line in proc.stdout.splitlines() if line.startswith("RESULT=")]
    assert lines, f"no result (exit {proc.returncode})\n{proc.stderr[-3000:]}"
    result = json.loads(lines[-1][len("RESULT=") :])

    assert result["status"] == 503, result
    assert result["detail"] == UNREACHABLE_DETAIL, result
    assert float(result["seconds"]) < 45, result
