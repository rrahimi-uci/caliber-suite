"""A scripted MCP server for deterministic, offline tests (B04.5, #759).

The fake is installed **under** the gateway, at the two seams the SDK owns (the transport and the client session), so the
real gateway code runs: the tool-policy gate, the remote-host allowlist and address check, the span, the result
normalization and the ``never_sent`` bookkeeping. Nothing is spawned and nothing touches the network: the server's host
resolves to a fixed public address.

A test scripts what the *server* does (return a result, raise, claim a failure, run a side effect on the call) and reads
what the *platform* sent (``calls``). The server's own claims about its tools (``annotations`` and the like) are test input:
the platform must not believe them.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from types import SimpleNamespace
from typing import Any

import pytest
from sqlalchemy.orm import Session, sessionmaker

import caliber.egress as egress_module
import caliber.mcp_gateway as gateway_module
from caliber.db.models import CaliberMcpServer

HOST = "mcp.example.test"
URI = f"https://{HOST}/mcp"
PUBLIC_ADDRESS = "93.184.216.34"
SERVER_ID = "MCP-fake-1"
#: A value that must never reach a record, an error or a span.
CREDENTIAL = "tok-secret-4f9c1d"


class FakeMcp:
    """What the platform can observe of one MCP server: its calls, and what it does when called."""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []
        self.sessions_opened = 0
        #: Raised by ``initialize`` (a session that never opens: nothing was sent).
        self.initialize_error: BaseException | None = None
        #: What ``call_tool`` returns, or a callable ``(name, arguments) -> result`` run on the call.
        self.result: Any = {"ok": True}
        #: Raised by ``call_tool`` after the call was recorded (the request had left).
        self.call_error: BaseException | None = None
        #: ``True`` makes the answer a tool-reported error (``is_error``): the server says it failed.
        self.reports_error = False
        #: Run when a call arrives, before the answer: the server's side effect.
        self.on_call: Callable[[str, dict[str, Any]], None] | None = None

    def tool_calls(self, name: str | None = None) -> list[dict[str, Any]]:
        return [c for c in self.calls if name is None or c["name"] == name]

    def install(self, monkeypatch: pytest.MonkeyPatch) -> FakeMcp:
        fake = self

        class _FakeSession:
            def __init__(self, _read: Any, _write: Any, **_kwargs: Any) -> None:
                pass

            async def __aenter__(self) -> _FakeSession:
                return self

            async def __aexit__(self, *_exc: object) -> None:
                return None

            async def initialize(self) -> None:
                fake.sessions_opened += 1
                if fake.initialize_error is not None:
                    raise fake.initialize_error

            async def call_tool(self, name: str, arguments: dict[str, Any]) -> Any:
                fake.calls.append({"name": name, "arguments": arguments})
                if fake.on_call is not None:
                    fake.on_call(name, arguments)
                if fake.call_error is not None:
                    raise fake.call_error
                result = fake.result(name, arguments) if callable(fake.result) else fake.result
                return SimpleNamespace(
                    is_error=fake.reports_error,
                    structured_content=None if fake.reports_error else result,
                    content=[
                        SimpleNamespace(model_dump=lambda **_k: {"type": "text", "text": "boom"})
                    ]
                    if fake.reports_error
                    else [],
                )

        @asynccontextmanager
        async def transport(*_args: Any, **_kwargs: Any) -> AsyncIterator[tuple[Any, Any]]:
            yield object(), object()

        monkeypatch.setattr(gateway_module, "ClientSession", _FakeSession)
        monkeypatch.setattr(gateway_module, "sse_client", transport)
        monkeypatch.setattr(egress_module, "_resolve_addresses", lambda _host: [PUBLIC_ADDRESS])
        monkeypatch.setenv("CALIBER_MCP_REMOTE_HOST_ALLOWLIST", HOST)
        return self


def server_policy(level: str = "write", *, allowed: bool = True, approval: bool = False) -> dict:
    return {"allowed": allowed, "side_effect_level": level, "requires_approval": approval}


def seed_server(
    factory: sessionmaker[Session],
    *,
    server_id: str = SERVER_ID,
    name: str = "fake",
    tools: dict[str, dict[str, Any] | None] | None = None,
    discovered: list[str] | None = None,
    **overrides: Any,
) -> None:
    """A registered MCP server row. ``tools`` maps a tool name to its administrator policy (``None``: discovered, unclassified)."""
    tools = {"issue_write": server_policy("write"), "lookup": server_policy("read")} | (tools or {})
    names = list(tools) if discovered is None else discovered
    fields: dict[str, Any] = {
        "server_id": server_id,
        "name": name,
        "transport": "sse",
        "uri": URI,
        "command": "",
        "args": [],
        "env": {},
        "headers": {"Authorization": f"Bearer {CREDENTIAL}"},
        "auth_type": "none",
        "auth_config": {},
        "status": "active",
        "discovered_tools": [
            # What the server reported about itself is untrusted data; a test can put a claim there.
            {"name": tool, "annotations": {"readOnlyHint": True}}
            for tool in names
        ],
        "tool_policies": {tool: policy for tool, policy in tools.items() if policy is not None},
    }
    fields.update(overrides)
    with factory() as session:
        session.add(CaliberMcpServer(**fields))
        session.commit()


def use_database(monkeypatch: pytest.MonkeyPatch, factory: sessionmaker[Session]) -> None:
    """The gateway reads MCP server rows from ``factory`` (the test database) instead of the process configuration."""
    monkeypatch.setattr(gateway_module, "_runtime_session_factory", lambda: factory)
