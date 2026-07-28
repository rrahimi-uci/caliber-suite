"""Unit tests for the MCP gateway's pure helpers and config/error paths.

Complements ``test_mcp_gateway.py`` (which drives a real stdio server) by
covering env/header/auth resolution, result normalization, transport-config
errors, and ``load_server_config`` without spawning anything.
"""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from types import SimpleNamespace
from typing import Any

import pytest

from caliber import mcp_gateway as gw
from caliber.mcp_gateway import (
    McpGatewayConfigError,
    McpGatewayError,
    McpServerConfig,
)

try:
    from builtins import ExceptionGroup as CompatExceptionGroupError
except ImportError:  # pragma: no cover - Python < 3.11 fallback for static analysis

    class CompatExceptionGroupError(Exception):
        def __init__(self, message: str, exceptions: list[BaseException]) -> None:
            super().__init__(message)
            self.exceptions = exceptions


def _server(**overrides: Any) -> McpServerConfig:
    base: dict[str, Any] = {
        "server_id": "MCP-x",
        "name": "x",
        "transport": "stdio",
        "uri": "",
        "command": "${PYTHON}",
        "args": ("-m", "caliber.mcp_servers.db", "--mode", "relational"),
        "env": {},
        "headers": {},
        "auth_type": "none",
        "auth_config": {},
        "discovered_tools": ({"name": "t"}, {"name": "search"}),
        "tool_policies": {
            "t": {
                "allowed": True,
                "side_effect_level": "read",
                "requires_approval": False,
            },
            "search": {
                "allowed": True,
                "side_effect_level": "read",
                "requires_approval": False,
            },
        },
    }
    base.update(overrides)
    return McpServerConfig(**base)


@pytest.fixture(autouse=True)
def _allow_test_remote_host(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CALIBER_MCP_REMOTE_HOST_ALLOWLIST", "h")
    monkeypatch.setenv("CALIBER_MCP_ALLOW_INSECURE_HTTP", "true")


# ---------------------------------------------------------------------------
# from_row
# ---------------------------------------------------------------------------


def test_from_row_maps_and_defaults_nones() -> None:
    row = SimpleNamespace(
        server_id="MCP-1",
        name="srv",
        transport="sse",
        uri="http://h/mcp",
        command="",
        args=None,
        env=None,
        headers=None,
        auth_type="token",
        auth_config=None,
    )
    cfg = McpServerConfig.from_row(row)
    assert cfg.server_id == "MCP-1"
    assert cfg.args == ()
    assert cfg.env == {} and cfg.headers == {} and cfg.auth_config == {}


# ---------------------------------------------------------------------------
# env resolution
# ---------------------------------------------------------------------------


def test_resolve_env_value(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MY_VAR", "secret")
    assert gw._resolve_env_value("${MY_VAR}") == "secret"
    monkeypatch.delenv("MISSING", raising=False)
    assert gw._resolve_env_value("${MISSING}") == ""
    assert gw._resolve_env_value("literal") == "literal"
    assert gw._resolve_env_value(123) == "123"


def test_resolved_stdio_env_skips_blank_keys(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PG", "postgres://x")
    monkeypatch.setenv("PATH", "/poisoned")
    monkeypatch.setenv("SHELL", "/tmp/evil-shell")
    server = _server(env={"POSTGRES_URL": "${PG}", "": "ignored", "RAW": "v"})
    resolved = gw._resolved_stdio_env(
        server,
        working_directory="/private/mcp",
        safe_path="/trusted/bin",
    )
    assert resolved["POSTGRES_URL"] == "postgres://x"
    assert resolved["RAW"] == "v"
    assert resolved["PATH"] == "/trusted/bin"
    assert resolved["SHELL"] == ""
    assert resolved["HOME"] == "/private/mcp"
    assert resolved["TMPDIR"] == "/private/mcp"


# ---------------------------------------------------------------------------
# command resolution
# ---------------------------------------------------------------------------


def test_resolve_command() -> None:
    import sys

    assert gw._resolve_command("${PYTHON}") == sys.executable
    assert gw._resolve_command("  npx  ") == "npx"


# ---------------------------------------------------------------------------
# http headers / auth
# ---------------------------------------------------------------------------


def test_http_headers_token_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TOK", "abc")
    server = _server(
        headers={"X-Extra": "1", "": "skip"},
        auth_type="token",
        auth_config={"token_env_var": "TOK"},
    )
    headers = gw._resolved_http_headers(server)
    assert headers["X-Extra"] == "1"
    assert headers["Authorization"] == "Bearer abc"


def test_http_headers_token_literal() -> None:
    server = _server(auth_type="token", auth_config={"token": "lit"})
    assert gw._resolved_http_headers(server)["Authorization"] == "Bearer lit"


def test_http_headers_basic() -> None:
    server = _server(auth_type="basic", auth_config={"username": "u", "password": "p"})
    # base64("u:p") == "dTpw"
    assert gw._resolved_http_headers(server)["Authorization"] == "Basic dTpw"


def test_http_headers_basic_without_user_is_noop() -> None:
    server = _server(auth_type="basic", auth_config={"password": "p"})
    assert "Authorization" not in gw._resolved_http_headers(server)


def test_http_headers_custom() -> None:
    server = _server(auth_type="custom", auth_config={"headers": {"X-Api": "k", "": "skip"}})
    assert gw._resolved_http_headers(server) == {"X-Api": "k"}


def test_resolve_token_variants(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("T", "fromenv")
    assert gw._resolve_token({"token_env_var": "T"}) == "fromenv"
    assert gw._resolve_token({"token": "fromliteral"}) == "fromliteral"
    assert gw._resolve_token({}) == ""


# ---------------------------------------------------------------------------
# tool / result normalization
# ---------------------------------------------------------------------------


def test_normalize_tool() -> None:
    tool = SimpleNamespace(name="t", description="d", inputSchema={"a": 1}, outputSchema={"b": 2})
    assert gw._normalize_tool(tool) == {
        "name": "t",
        "description": "d",
        "input_schema": {"a": 1},
        "output_schema": {"b": 2},
    }
    bare = SimpleNamespace(name="t", description=None, inputSchema=None, outputSchema=None)
    out = gw._normalize_tool(bare)
    assert out["description"] == "" and out["input_schema"] == {} and out["output_schema"] is None


class _Dumpable:
    def __init__(self, value: Any) -> None:
        self._value = value

    def model_dump(self, **_: Any) -> Any:
        return self._value


def test_model_dump_dict() -> None:
    assert gw._model_dump_dict(_Dumpable({"x": 1})) == {"x": 1}
    assert gw._model_dump_dict(_Dumpable(["not", "a", "dict"])) is None
    assert gw._model_dump_dict(object()) is None


def test_normalize_call_result_structured_and_content() -> None:
    assert gw._normalize_call_result(SimpleNamespace(structuredContent={"ok": True})) == {
        "ok": True
    }
    res = SimpleNamespace(structuredContent=None, content=[_Dumpable({"text": "hi"}), object()])
    assert gw._normalize_call_result(res) == [{"text": "hi"}]


def test_tool_error_message() -> None:
    content = [_Dumpable({"text": "boom"}), _Dumpable({"text": "  "}), object()]
    assert gw._tool_error_message("t", content) == "boom"
    assert gw._tool_error_message("t", []) == "MCP tool 't' returned an error"


def test_dict_or_none() -> None:
    assert gw._dict_or_none(None) is None
    assert gw._dict_or_none({"a": 1}) == {"a": 1}
    assert gw._dict_or_none([1, 2]) is None


# ---------------------------------------------------------------------------
# transport config errors (raised before any connection)
# ---------------------------------------------------------------------------


def _enter_transport(server: McpServerConfig) -> None:
    async def _run() -> None:
        async with gw._transport_streams(server, timeout_seconds=1.0):
            pass  # pragma: no cover - config errors raise before yield

    asyncio.run(_run())


@pytest.mark.parametrize(
    "server",
    [
        _server(transport="stdio", command="   "),
        _server(transport="sse", uri=""),
        _server(transport="streamable-http", uri=""),
        _server(transport="bogus"),
    ],
)
def test_transport_config_errors(server: McpServerConfig) -> None:
    with pytest.raises(McpGatewayConfigError):
        _enter_transport(server)


# ---------------------------------------------------------------------------
# load_server_config
# ---------------------------------------------------------------------------


class _FakeSession:
    def __init__(self, row: Any) -> None:
        self._row = row

    def __enter__(self) -> _FakeSession:
        return self

    def __exit__(self, *_: object) -> None:
        return None

    def get(self, _model: Any, _sid: str) -> Any:
        return self._row


def test_load_server_config_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(gw, "_runtime_session_factory", lambda: lambda: _FakeSession(None))
    with pytest.raises(McpGatewayConfigError):
        gw.load_server_config("nope")


def test_load_server_config_found(monkeypatch: pytest.MonkeyPatch) -> None:
    row = SimpleNamespace(
        server_id="MCP-9",
        name="n",
        transport="stdio",
        uri="",
        command="python",
        args=["-m", "x"],
        env={},
        headers={},
        auth_type="none",
        auth_config={},
    )
    monkeypatch.setattr(gw, "_runtime_session_factory", lambda: lambda: _FakeSession(row))
    cfg = gw.load_server_config("MCP-9")
    assert cfg.server_id == "MCP-9" and cfg.args == ("-m", "x")


# ---------------------------------------------------------------------------
# event-loop guard
# ---------------------------------------------------------------------------


def test_event_loop_guard_outside_loop() -> None:
    gw._assert_no_running_event_loop("caller")  # no raise outside a loop


def test_event_loop_guard_inside_loop() -> None:
    async def _run() -> None:
        gw._assert_no_running_event_loop("caller")

    with pytest.raises(McpGatewayError):
        asyncio.run(_run())


# ---------------------------------------------------------------------------
# discover / invoke async paths (session faked — no transport)
# ---------------------------------------------------------------------------


class _FakeClientSession:
    def __init__(
        self,
        *,
        tools: list[Any] | None = None,
        call_result: Any = None,
        list_error: Exception | None = None,
        call_error: Exception | None = None,
    ) -> None:
        self._tools = tools or []
        self._call_result = call_result
        self._list_error = list_error
        self._call_error = call_error

    async def list_tools(self) -> Any:
        if self._list_error:
            raise self._list_error
        return SimpleNamespace(tools=self._tools)

    async def call_tool(self, _name: str, _args: dict[str, Any]) -> Any:
        if self._call_error:
            raise self._call_error
        return self._call_result


def _patch_session(monkeypatch: pytest.MonkeyPatch, session: _FakeClientSession) -> None:
    @asynccontextmanager
    async def _fake(_server: McpServerConfig, *, timeout_seconds: float) -> Any:
        yield session

    monkeypatch.setattr(gw, "_session_for", _fake)


def test_discover_tools_async_success(monkeypatch: pytest.MonkeyPatch) -> None:
    tool = SimpleNamespace(name="t", description="d", inputSchema={}, outputSchema=None)
    _patch_session(monkeypatch, _FakeClientSession(tools=[tool]))
    out = asyncio.run(gw.discover_tools(_server(), timeout_seconds=1.0))
    assert out[0]["name"] == "t"


def test_discover_tools_async_error(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_session(monkeypatch, _FakeClientSession(list_error=RuntimeError("x")))
    with pytest.raises(gw.McpGatewayTransportError):
        asyncio.run(gw.discover_tools(_server(), timeout_seconds=1.0))


def test_invoke_tool_success(monkeypatch: pytest.MonkeyPatch) -> None:
    res = SimpleNamespace(isError=False, structuredContent={"ok": True}, content=[])
    _patch_session(monkeypatch, _FakeClientSession(call_result=res))
    out = asyncio.run(gw.invoke_tool(_server(), tool_name="t", arguments={}))
    assert out == {"ok": True}


def test_invoke_tool_is_error(monkeypatch: pytest.MonkeyPatch) -> None:
    res = SimpleNamespace(
        isError=True, structuredContent=None, content=[_Dumpable({"text": "bad"})]
    )
    _patch_session(monkeypatch, _FakeClientSession(call_result=res))
    with pytest.raises(gw.McpGatewayTransportError, match="bad"):
        asyncio.run(gw.invoke_tool(_server(), tool_name="t", arguments={}))


def test_invoke_tool_call_error(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_session(monkeypatch, _FakeClientSession(call_error=RuntimeError("nope")))
    with pytest.raises(gw.McpGatewayTransportError):
        asyncio.run(gw.invoke_tool(_server(), tool_name="t", arguments={}))


def test_invoke_tool_wraps_exception_group_from_session_teardown(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    @asynccontextmanager
    async def _broken_session(_server: gw.McpServerConfig, *, timeout_seconds: float) -> Any:
        del timeout_seconds
        yield _FakeClientSession(
            call_result=SimpleNamespace(
                isError=False,
                structuredContent={"ok": True},
                content=[],
            )
        )
        raise CompatExceptionGroupError(
            "unhandled errors in a TaskGroup",
            [RuntimeError("Invalid input: query is required")],
        )

    monkeypatch.setattr(gw, "_session_for", _broken_session)

    with pytest.raises(gw.McpGatewayTransportError, match="Invalid input: query is required"):
        asyncio.run(gw.invoke_tool(_server(), tool_name="t", arguments={}))


def test_exception_message_walks_wrapped_taskgroup_causes() -> None:
    with pytest.raises(RuntimeError) as excinfo:
        try:
            raise CompatExceptionGroupError(
                "unhandled errors in a TaskGroup",
                [RuntimeError("Invalid input: query is required")],
            )
        except BaseException as inner:
            raise RuntimeError(
                "MCP session failed: unhandled errors in a TaskGroup (1 sub-exception)"
            ) from inner

    assert gw._exception_message(excinfo.value) == "Invalid input: query is required"


def test_invoke_tool_by_server_id_sync(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(gw, "load_server_config", lambda sid: _server(server_id=sid))
    monkeypatch.setattr(
        gw,
        "invoke_tool_sync",
        lambda server, *, tool_name, arguments, timeout_seconds: {"called": tool_name},
    )
    assert gw.invoke_tool_by_server_id_sync(server_id="S", tool_name="t", arguments={}) == {
        "called": "t"
    }


# ---------------------------------------------------------------------------
# sse / streamable-http transports (clients faked — no network)
# ---------------------------------------------------------------------------


class _FakeClientSessionCtx:
    """Stand-in for mcp.ClientSession (async context manager)."""

    def __init__(self, *_a: object, **_k: object) -> None:
        pass

    async def __aenter__(self) -> _FakeClientSessionCtx:
        return self

    async def __aexit__(self, *_: object) -> None:
        return None

    async def initialize(self) -> None:
        return None

    async def list_tools(self) -> Any:
        return SimpleNamespace(
            tools=[
                SimpleNamespace(name="remote", description="", inputSchema={}, outputSchema=None)
            ]
        )


def test_session_for_sse_transport(monkeypatch: pytest.MonkeyPatch) -> None:
    @asynccontextmanager
    async def _fake_sse(_uri: str, **_k: object) -> Any:
        yield ("read", "write")

    monkeypatch.setattr(gw, "sse_client", _fake_sse)
    monkeypatch.setattr(gw, "ClientSession", _FakeClientSessionCtx)
    out = asyncio.run(
        gw.discover_tools(_server(transport="sse", uri="http://h/mcp"), timeout_seconds=1.0)
    )
    assert out[0]["name"] == "remote"


def test_session_for_streamable_transport(monkeypatch: pytest.MonkeyPatch) -> None:
    class _FakeAsyncClient:
        def __init__(self, **_k: object) -> None:
            pass

        async def __aenter__(self) -> _FakeAsyncClient:
            return self

        async def __aexit__(self, *_: object) -> None:
            return None

    @asynccontextmanager
    async def _fake_streamable(_uri: str, **_k: object) -> Any:
        yield ("read", "write", lambda: "session-id")

    monkeypatch.setattr(gw.httpx, "AsyncClient", _FakeAsyncClient)
    monkeypatch.setattr(gw, "streamable_http_client", _fake_streamable)
    monkeypatch.setattr(gw, "ClientSession", _FakeClientSessionCtx)
    out = asyncio.run(
        gw.discover_tools(
            _server(transport="streamable-http", uri="http://h/mcp"), timeout_seconds=1.0
        )
    )
    assert out[0]["name"] == "remote"


def test_session_for_flattens_wrapped_taskgroup_errors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    @asynccontextmanager
    async def _fake_sse(_uri: str, **_k: object) -> Any:
        yield ("read", "write")

    class _BrokenClientSessionCtx:
        def __init__(self, *_a: object, **_k: object) -> None:
            pass

        async def __aenter__(self) -> _BrokenClientSessionCtx:
            return self

        async def __aexit__(self, *_: object) -> None:
            try:
                raise CompatExceptionGroupError(
                    "unhandled errors in a TaskGroup",
                    [RuntimeError("Invalid input: query is required")],
                )
            except BaseException as inner:
                raise RuntimeError(
                    "MCP session failed: unhandled errors in a TaskGroup (1 sub-exception)"
                ) from inner

        async def initialize(self) -> None:
            return None

        async def list_tools(self) -> Any:
            return SimpleNamespace(tools=[])

    monkeypatch.setattr(gw, "sse_client", _fake_sse)
    monkeypatch.setattr(gw, "ClientSession", _BrokenClientSessionCtx)

    with pytest.raises(gw.McpGatewayTransportError, match="Invalid input: query is required"):
        asyncio.run(
            gw.discover_tools(
                _server(transport="sse", uri="http://h/mcp"),
                timeout_seconds=1.0,
            )
        )


# ---------------------------------------------------------------------------
# MLflow tracing: every MCP invoke emits a TOOL span (Observability parity)
# ---------------------------------------------------------------------------


def test_invoke_tool_emits_tool_span(monkeypatch: pytest.MonkeyPatch) -> None:
    from caliber.observability.mlflow_tracing import Tracer, set_tracer

    from .test_mlflow_tracing import FakeMlflow

    res = SimpleNamespace(isError=False, structuredContent={"ok": True}, content=[])
    _patch_session(monkeypatch, _FakeClientSession(call_result=res))
    fake = FakeMlflow()
    set_tracer(Tracer(mlflow_module=fake))
    try:
        out = asyncio.run(
            gw.invoke_tool(_server(name="github"), tool_name="search", arguments={"q": "x"})
        )
    finally:
        set_tracer(None)

    assert out == {"ok": True}
    assert len(fake.spans) == 1
    span = fake.spans[0]
    assert span.name == "mcp.github.search"
    assert span.span_type == "TOOL"
    assert span.attributes["caliber.tool"] == "search"
    assert span.attributes["caliber.mcp.server"] == "github"
    assert span.attributes["caliber.mcp.transport"] == "stdio"
    assert "caliber.tool.input" in span.attributes
    # Attributes are JSON-serialized + byte-capped by the tracer, so the dict
    # output surfaces as a string.
    assert "ok" in span.attributes["caliber.tool.output"]
    assert isinstance(span.attributes["caliber.tool.latency_ms"], float)
    assert span.attributes["caliber.tool.latency_ms"] >= 0.0
    assert span.attributes["caliber.status"] == "completed"


def test_invoke_tool_error_records_failed_span(monkeypatch: pytest.MonkeyPatch) -> None:
    from caliber.observability.mlflow_tracing import Tracer, set_tracer

    from .test_mlflow_tracing import FakeMlflow

    _patch_session(monkeypatch, _FakeClientSession(call_error=RuntimeError("nope")))
    fake = FakeMlflow()
    set_tracer(Tracer(mlflow_module=fake))
    try:
        with pytest.raises(gw.McpGatewayTransportError):
            asyncio.run(gw.invoke_tool(_server(), tool_name="t", arguments={}))
    finally:
        set_tracer(None)

    assert len(fake.spans) == 1
    span = fake.spans[0]
    assert span.span_type == "TOOL"
    assert span.attributes["caliber.status"] == "failed"
    assert span.attributes["caliber.error_type"] == "McpGatewayTransportError"
    assert "caliber.tool.latency_ms" in span.attributes
