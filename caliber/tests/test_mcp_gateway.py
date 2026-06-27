"""Tests for the shared MCP gateway transport module."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from caliber.mcp_gateway import (
    McpGatewayTransportError,
    McpServerConfig,
    _resolve_command,
    discover_tools_sync,
    invoke_tool_sync,
)


def _fake_stdio_server(command: str = sys.executable) -> McpServerConfig:
    fixture = Path(__file__).with_name("fixtures") / "fake_mcp_server.py"
    return McpServerConfig(
        server_id="MCP-test",
        name="Fake",
        transport="stdio",
        uri="",
        command=command,
        args=(str(fixture),),
        env={},
        headers={},
        auth_type="none",
        auth_config={},
    )


def test_resolve_command_python_sentinel() -> None:
    assert _resolve_command("${PYTHON}") == sys.executable
    assert _resolve_command(" ${PYTHON} ") == sys.executable
    assert _resolve_command("npx") == "npx"
    assert _resolve_command("  /usr/bin/python3  ") == "/usr/bin/python3"


def test_discover_tools_over_stdio_with_python_sentinel() -> None:
    # ``${PYTHON}`` resolves to the current interpreter, so a first-party
    # Python MCP server launches under the same venv as CALIBER.
    tools = discover_tools_sync(_fake_stdio_server(command="${PYTHON}"), timeout_seconds=20.0)
    assert "web_search" in {tool["name"] for tool in tools}


def test_discover_tools_over_stdio() -> None:
    tools = discover_tools_sync(_fake_stdio_server(), timeout_seconds=20.0)
    names = {tool["name"] for tool in tools}
    assert "web_search" in names
    assert "read_file" in names


def test_invoke_tool_over_stdio() -> None:
    result = invoke_tool_sync(
        _fake_stdio_server(),
        tool_name="web_search",
        arguments={"query": "refund", "limit": 2},
        timeout_seconds=20.0,
    )
    assert isinstance(result, dict)
    assert result["query"] == "refund"
    assert result["hits"] == ["refund-1", "refund-2"]


def test_invoke_tool_error_surfaces() -> None:
    with pytest.raises(McpGatewayTransportError):
        invoke_tool_sync(
            _fake_stdio_server(),
            tool_name="fail_tool",
            arguments={"message": "boom"},
            timeout_seconds=20.0,
        )
