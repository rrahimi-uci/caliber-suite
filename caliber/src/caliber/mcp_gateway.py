"""MCP gateway for discovery and tool invocation.

This module centralizes all MCP transport logic used by:

* ``/caliber/mcp-servers`` routes (operator test/discovery/playground calls)
* workflow runtime MCP tool bindings

The gateway uses the official ``mcp`` Python SDK for stdio/SSE/streamable-HTTP
transports and exposes small sync wrappers for call sites that run outside an
event loop (notably the workflow runtime's synchronous tool callables).
"""

from __future__ import annotations

import asyncio
import base64
import os
import re
import sys
import threading
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import timedelta
from functools import lru_cache
from typing import Any

import anyio
import httpx
from mcp import ClientSession
from mcp.client.sse import sse_client
from mcp.client.stdio import StdioServerParameters, stdio_client
from mcp.client.streamable_http import streamable_http_client
from sqlalchemy.orm import Session, sessionmaker

from caliber.config import CaliberConfig
from caliber.db.models import CaliberMcpServer
from caliber.db.session import create_engine_from_config, sessionmaker_from_engine
from caliber.observability.mlflow_tracing import get_tracer

_ENV_PLACEHOLDER_RE = re.compile(r"^\$\{([A-Za-z_][A-Za-z0-9_]*)\}$")
_SESSION_FACTORY_LOCK = threading.Lock()

# A stdio ``command`` of ``${PYTHON}`` resolves to the interpreter running the
# CALIBER server itself. First-party Python MCP servers (e.g.
# ``caliber.mcp_servers.db``) use this so they always launch under the same
# venv as CALIBER — which is where their dependencies are installed — without
# hardcoding an absolute, machine-specific path into a catalog or seed.
_PYTHON_COMMAND_SENTINEL = "${PYTHON}"


def _resolve_command(command: str) -> str:
    """Resolve the stdio ``command``, expanding the ``${PYTHON}`` sentinel."""
    if command.strip() == _PYTHON_COMMAND_SENTINEL:
        return sys.executable
    return command.strip()


class McpGatewayError(RuntimeError):
    """Base gateway failure."""


class McpGatewayConfigError(McpGatewayError):
    """Invalid or missing MCP server configuration."""


class McpGatewayTransportError(McpGatewayError):
    """Transport-level failure while speaking MCP."""


@dataclass(frozen=True)
class McpServerConfig:
    """Transport configuration for one MCP server."""

    server_id: str
    name: str
    transport: str
    uri: str
    command: str
    args: tuple[str, ...]
    env: dict[str, object]
    headers: dict[str, object]
    auth_type: str
    auth_config: dict[str, object]

    @classmethod
    def from_row(cls, row: CaliberMcpServer) -> McpServerConfig:
        return cls(
            server_id=row.server_id,
            name=row.name,
            transport=row.transport,
            uri=row.uri,
            command=row.command,
            args=tuple(str(arg) for arg in (row.args or [])),
            env=dict(row.env or {}),
            headers=dict(row.headers or {}),
            auth_type=row.auth_type,
            auth_config=dict(row.auth_config or {}),
        )


def load_server_config(server_id: str) -> McpServerConfig:
    """Load one MCP server configuration from the configured CALIBER database."""
    factory = _runtime_session_factory()
    with factory() as session:
        row = session.get(CaliberMcpServer, server_id)
        if row is None:
            raise McpGatewayConfigError(f"MCP server {server_id!r} not found")
        return McpServerConfig.from_row(row)


async def discover_tools(
    server: McpServerConfig,
    *,
    timeout_seconds: float = 20.0,
) -> list[dict[str, Any]]:
    """Return normalized MCP tools for a server."""
    try:
        async with _session_for(server, timeout_seconds=timeout_seconds) as session:
            try:
                listed = await session.list_tools()
            except Exception as exc:
                raise McpGatewayTransportError(
                    f"MCP tools/list failed: {_exception_message(exc)}"
                ) from exc
    except McpGatewayError:
        raise
    except Exception as exc:
        raise McpGatewayTransportError(f"MCP tools/list failed: {_exception_message(exc)}") from exc
    return [_normalize_tool(tool) for tool in listed.tools]


async def invoke_tool(
    server: McpServerConfig,
    *,
    tool_name: str,
    arguments: dict[str, Any],
    timeout_seconds: float = 45.0,
) -> Any:
    """Invoke one MCP tool and return a JSON-serializable payload.

    Wrapped in a single MLflow ``TOOL`` span so every MCP invocation — from the
    Playground, the operator routes, and the workflow runtime (all of which funnel
    through this choke-point) — surfaces in Observability with the same
    name/input/output/latency/status shape as native workflow tool calls. The
    tracer redacts + byte-caps every attribute, so raw args/result are safe to pass.
    """
    tracer = get_tracer()
    started = time.perf_counter()
    with tracer.span(
        f"mcp.{server.name}.{tool_name}",
        span_type="TOOL",
        attributes={
            "caliber.tool": tool_name,
            "caliber.mcp.server": server.name,
            "caliber.mcp.server_id": server.server_id,
            "caliber.mcp.transport": server.transport,
            "caliber.tool.input": arguments,
        },
    ) as span:
        try:
            try:
                async with _session_for(server, timeout_seconds=timeout_seconds) as session:
                    try:
                        result = await session.call_tool(tool_name, arguments)
                    except Exception as exc:
                        raise McpGatewayTransportError(
                            f"MCP tools/call failed: {_exception_message(exc)}"
                        ) from exc
            except McpGatewayError:
                raise
            except Exception as exc:
                raise McpGatewayTransportError(
                    f"MCP tools/call failed: {_exception_message(exc)}"
                ) from exc
            if bool(result.isError):
                span.set_attribute("caliber.mcp.is_error", True)
                raise McpGatewayTransportError(_tool_error_message(tool_name, result.content))
            normalized = _normalize_call_result(result)
            span.set_attribute("caliber.tool.output", normalized)
            return normalized
        finally:
            span.set_attribute(
                "caliber.tool.latency_ms", round((time.perf_counter() - started) * 1000, 3)
            )


def discover_tools_sync(
    server: McpServerConfig,
    *,
    timeout_seconds: float = 20.0,
) -> list[dict[str, Any]]:
    """Synchronous wrapper around :func:`discover_tools`."""
    _assert_no_running_event_loop("discover_tools_sync")
    return anyio.run(_discover_tools_async, server, timeout_seconds)


def invoke_tool_sync(
    server: McpServerConfig,
    *,
    tool_name: str,
    arguments: dict[str, Any],
    timeout_seconds: float = 45.0,
) -> Any:
    """Synchronous wrapper around :func:`invoke_tool`."""
    _assert_no_running_event_loop("invoke_tool_sync")
    return anyio.run(
        _invoke_tool_async,
        server,
        tool_name,
        arguments,
        timeout_seconds,
    )


def invoke_tool_by_server_id_sync(
    *,
    server_id: str,
    tool_name: str,
    arguments: dict[str, Any],
    timeout_seconds: float = 45.0,
) -> Any:
    """Resolve a server from DB and invoke a tool (sync)."""
    server = load_server_config(server_id)
    return invoke_tool_sync(
        server,
        tool_name=tool_name,
        arguments=arguments,
        timeout_seconds=timeout_seconds,
    )


async def _discover_tools_async(
    server: McpServerConfig,
    timeout_seconds: float,
) -> list[dict[str, Any]]:
    return await discover_tools(server, timeout_seconds=timeout_seconds)


async def _invoke_tool_async(
    server: McpServerConfig,
    tool_name: str,
    arguments: dict[str, Any],
    timeout_seconds: float,
) -> Any:
    return await invoke_tool(
        server,
        tool_name=tool_name,
        arguments=arguments,
        timeout_seconds=timeout_seconds,
    )


@asynccontextmanager
async def _session_for(
    server: McpServerConfig,
    *,
    timeout_seconds: float,
) -> AsyncIterator[ClientSession]:
    read_timeout = timedelta(seconds=max(timeout_seconds, 1.0))
    async with _transport_streams(server, timeout_seconds=timeout_seconds) as (read, write):
        try:
            async with ClientSession(
                read,
                write,
                read_timeout_seconds=read_timeout,
            ) as session:
                await session.initialize()
                yield session
        except McpGatewayError:
            raise
        except Exception as exc:
            raise McpGatewayTransportError(
                f"MCP session failed: {_exception_message(exc)}"
            ) from exc


@asynccontextmanager
async def _transport_streams(
    server: McpServerConfig,
    *,
    timeout_seconds: float,
) -> AsyncIterator[tuple[Any, Any]]:
    transport = server.transport.strip().lower()
    if transport == "stdio":
        if not server.command.strip():
            raise McpGatewayConfigError("stdio transport requires a non-empty command")
        params = StdioServerParameters(
            command=_resolve_command(server.command),
            args=[arg for arg in server.args if arg != ""],
            env=_resolved_stdio_env(server),
        )
        async with stdio_client(params) as (read, write):
            yield read, write
        return

    if transport == "sse":
        uri = server.uri.strip()
        if not uri:
            raise McpGatewayConfigError("sse transport requires a non-empty uri")
        headers = _resolved_http_headers(server)
        sse_read_timeout = max(timeout_seconds * 2.0, timeout_seconds + 5.0)
        async with sse_client(
            uri,
            headers=headers,
            timeout=timeout_seconds,
            sse_read_timeout=sse_read_timeout,
        ) as (read, write):
            yield read, write
        return

    if transport == "streamable-http":
        uri = server.uri.strip()
        if not uri:
            raise McpGatewayConfigError("streamable-http transport requires a non-empty uri")
        timeout = httpx.Timeout(
            timeout_seconds,
            read=max(timeout_seconds * 2.0, timeout_seconds + 5.0),
        )
        async with (
            httpx.AsyncClient(
                headers=_resolved_http_headers(server),
                timeout=timeout,
            ) as http_client,
            streamable_http_client(
                uri,
                http_client=http_client,
                terminate_on_close=True,
            ) as (read, write, _get_session_id),
        ):
            yield read, write
        return

    raise McpGatewayConfigError(
        f"unsupported MCP transport {server.transport!r}; "
        "expected one of {'stdio', 'sse', 'streamable-http'}"
    )


def _normalize_tool(tool: Any) -> dict[str, Any]:
    return {
        "name": str(getattr(tool, "name", "")),
        "description": str(getattr(tool, "description", "") or ""),
        "input_schema": dict(getattr(tool, "inputSchema", {}) or {}),
        "output_schema": _dict_or_none(getattr(tool, "outputSchema", None)),
    }


def _normalize_call_result(result: Any) -> Any:
    structured = getattr(result, "structuredContent", None)
    if structured is not None:
        return structured
    content = getattr(result, "content", None) or []
    payload: list[dict[str, Any]] = []
    for item in content:
        dumped = _model_dump_dict(item)
        if dumped is not None:
            payload.append(dumped)
    return payload


def _tool_error_message(tool_name: str, content: list[Any]) -> str:
    text_parts: list[str] = []
    for item in content:
        dumped = _model_dump_dict(item)
        if dumped is None:
            continue
        text = dumped.get("text")
        if isinstance(text, str) and text.strip():
            text_parts.append(text.strip())
    if text_parts:
        return "; ".join(text_parts)
    return f"MCP tool {tool_name!r} returned an error"


def _exception_message(exc: BaseException) -> str:
    messages = _collect_exception_messages(exc)
    meaningful = [
        message for message in messages if "unhandled errors in a TaskGroup" not in message
    ]
    parts = meaningful or messages
    if not parts:
        return type(exc).__name__
    return "; ".join(dict.fromkeys(parts))


def _collect_exception_messages(
    exc: BaseException,
    *,
    _seen: set[int] | None = None,
) -> list[str]:
    seen = _seen if _seen is not None else set()
    marker = id(exc)
    if marker in seen:
        return []
    seen.add(marker)

    messages: list[str] = []
    group_children = getattr(exc, "exceptions", None)
    if isinstance(group_children, tuple):
        for child in group_children:
            if not isinstance(child, BaseException):
                continue
            messages.extend(_collect_exception_messages(child, _seen=seen))

    text = str(exc).strip()
    messages.append(text if text else type(exc).__name__)

    cause = getattr(exc, "__cause__", None)
    if isinstance(cause, BaseException):
        messages.extend(_collect_exception_messages(cause, _seen=seen))

    context = getattr(exc, "__context__", None)
    if isinstance(context, BaseException) and context is not cause:
        messages.extend(_collect_exception_messages(context, _seen=seen))
    return messages


def _model_dump_dict(item: Any) -> dict[str, Any] | None:
    dump = getattr(item, "model_dump", None)
    if not callable(dump):
        return None
    value = dump(mode="json", by_alias=True, exclude_none=True)
    if isinstance(value, dict):
        return value
    return None


def _dict_or_none(value: Any) -> dict[str, Any] | None:
    if value is None:
        return None
    if isinstance(value, dict):
        return dict(value)
    return None


def _resolved_stdio_env(server: McpServerConfig) -> dict[str, str]:
    resolved: dict[str, str] = {}
    for key, raw in server.env.items():
        key_name = str(key).strip()
        if not key_name:
            continue
        resolved[key_name] = _resolve_env_value(raw)
    return resolved


def _resolve_env_value(raw: object) -> str:
    if isinstance(raw, str):
        match = _ENV_PLACEHOLDER_RE.match(raw.strip())
        if match is not None:
            return os.environ.get(match.group(1), "")
        return raw
    return str(raw)


def _resolved_http_headers(server: McpServerConfig) -> dict[str, str]:
    headers: dict[str, str] = {}
    for key, raw in server.headers.items():
        key_name = str(key).strip()
        if not key_name:
            continue
        headers[key_name] = str(raw)

    auth_type = server.auth_type.strip().lower()
    auth_config = server.auth_config

    if auth_type == "token":
        token = _resolve_token(auth_config)
        if token:
            headers.setdefault("Authorization", f"Bearer {token}")
    elif auth_type == "basic":
        user = str(auth_config.get("username", "")).strip()
        password = str(auth_config.get("password", ""))
        if user:
            value = f"{user}:{password}".encode()
            headers.setdefault("Authorization", f"Basic {base64.b64encode(value).decode('ascii')}")
    elif auth_type == "custom":
        raw_headers = auth_config.get("headers")
        if isinstance(raw_headers, dict):
            for key, raw in raw_headers.items():
                key_name = str(key).strip()
                if not key_name:
                    continue
                headers[key_name] = str(raw)

    return headers


def _resolve_token(auth_config: dict[str, object]) -> str:
    env_key = str(auth_config.get("token_env_var", "")).strip()
    if env_key:
        return os.environ.get(env_key, "")
    token = auth_config.get("token")
    if isinstance(token, str):
        return token
    return ""


@lru_cache(maxsize=1)
def _runtime_session_factory() -> sessionmaker[Session]:
    with _SESSION_FACTORY_LOCK:
        config = CaliberConfig.load()
        engine = create_engine_from_config(config)
        return sessionmaker_from_engine(engine)


def _assert_no_running_event_loop(caller: str) -> None:
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return
    raise McpGatewayError(
        f"{caller} cannot be called from an active event loop; use async APIs instead"
    )


__all__ = [
    "McpGatewayConfigError",
    "McpGatewayError",
    "McpGatewayTransportError",
    "McpServerConfig",
    "discover_tools",
    "discover_tools_sync",
    "invoke_tool",
    "invoke_tool_by_server_id_sync",
    "invoke_tool_sync",
    "load_server_config",
]
