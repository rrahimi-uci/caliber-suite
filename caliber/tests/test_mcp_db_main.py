"""CLI contracts for the bundled stdio/HTTP database MCP server."""

from __future__ import annotations

import pytest

from caliber.mcp_servers.db import __main__ as db_main


def test_main_starts_streamable_http_on_requested_interface(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}

    class _Server:
        def run(self, *, transport: str) -> None:
            captured["transport"] = transport

    def _build(mode: str, *, host: str, port: int) -> _Server:
        captured.update(mode=mode, host=host, port=port)
        return _Server()

    monkeypatch.setattr(db_main, "build_server", _build)
    result = db_main.main(
        [
            "--mode",
            "vector",
            "--transport",
            "streamable-http",
            "--host",
            "0.0.0.0",
            "--port",
            "8102",
        ]
    )
    assert result == 0
    assert captured == {
        "mode": "vector",
        "host": "0.0.0.0",
        "port": 8102,
        "transport": "streamable-http",
    }


def test_parse_mode_remains_backward_compatible() -> None:
    assert db_main.parse_mode(["--mode", "graph"]) == "graph"


def test_main_rejects_invalid_port() -> None:
    with pytest.raises(SystemExit, match="--port must be between"):
        db_main.main(["--port", "70000"])
