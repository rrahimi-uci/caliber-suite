"""Tests for the standalone tool-sandbox CLI entry point."""

from __future__ import annotations

import pytest

from caliber.tool_sandbox import __main__ as sandbox_main


def test_main_runs_uvicorn_with_cli_host_and_port(monkeypatch) -> None:
    calls: dict[str, object] = {}

    class FakeUvicorn:
        @staticmethod
        def run(app: str, **kwargs) -> None:
            calls["app"] = app
            calls.update(kwargs)

    monkeypatch.setattr(sandbox_main, "uvicorn_mod", FakeUvicorn)
    monkeypatch.setattr(
        "sys.argv",
        ["caliber-tool-sandbox", "--host", "0.0.0.0", "--port", "9999"],
    )

    sandbox_main.main()

    assert calls == {
        "app": "caliber.tool_sandbox.server:create_app",
        "factory": True,
        "host": "0.0.0.0",
        "port": 9999,
    }


def test_main_exits_when_uvicorn_extra_is_missing(monkeypatch) -> None:
    monkeypatch.setattr(sandbox_main, "uvicorn_mod", None)
    monkeypatch.setattr("sys.argv", ["caliber-tool-sandbox"])

    with pytest.raises(SystemExit) as exc:
        sandbox_main.main()

    assert "uvicorn is required" in str(exc.value)
