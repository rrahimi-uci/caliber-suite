"""Route-level error/edge paths for /caliber/mcp-servers.

Complements ``test_mcp_servers.py`` (happy paths with a success-mocking gateway)
by covering the failure branches: gateway errors on test/discover/invoke,
unknown-tool and policy-blocked invokes, and tool-policy updates.
"""

from __future__ import annotations

import pytest
from sqlalchemy.orm import Session
from starlette.testclient import TestClient

from caliber.db.models import CaliberMcpServer
from caliber.mcp_gateway import McpGatewayTransportError
from caliber.routes import mcp_servers as mcp_routes

BASE = "/ajax-api/2.0/mlflow/caliber/mcp-servers"


def _seed(db: Session, **kw: object) -> CaliberMcpServer:
    defaults: dict[str, object] = {
        "server_id": "MCP-r1",
        "name": "Relational",
        "transport": "stdio",
        "command": "${PYTHON}",
        "status": "active",
    }
    defaults.update(kw)
    server = CaliberMcpServer(**defaults)  # type: ignore[arg-type]
    db.add(server)
    db.commit()
    return server


def _raise_gateway(*_a: object, **_k: object) -> object:
    raise McpGatewayTransportError("boom: cannot connect")


# ── test-connection ───────────────────────────────────────────────────────


def test_test_connection_gateway_error(
    client: TestClient, db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    _seed(db_session)
    monkeypatch.setattr(mcp_routes, "discover_tools_via_gateway", _raise_gateway)
    r = client.post(f"{BASE}/MCP-r1/test-connection")
    assert r.status_code == 200
    data = r.json()["data"]
    assert data["success"] is False
    assert "boom" in data["error"]


def test_test_connection_missing_command_config(client: TestClient, db_session: Session) -> None:
    _seed(db_session, server_id="MCP-bad", command="")  # stdio needs a command
    r = client.post(f"{BASE}/MCP-bad/test-connection")
    assert r.json()["data"]["success"] is False
    assert "command" in r.json()["data"]["error"]


def test_test_connection_unknown_server(client: TestClient) -> None:
    assert client.post(f"{BASE}/MCP-nope/test-connection").status_code == 404


# ── discover-tools ──────────────────────────────────────────────────────────


def test_discover_tools_gateway_error(
    client: TestClient, db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    _seed(db_session)
    monkeypatch.setattr(mcp_routes, "discover_tools_via_gateway", _raise_gateway)
    r = client.post(f"{BASE}/MCP-r1/discover-tools")
    assert r.status_code == 502


# ── invoke-tool ───────────────────────────────────────────────────────────


def test_invoke_requires_tool_name(client: TestClient, db_session: Session) -> None:
    _seed(db_session)
    r = client.post(f"{BASE}/MCP-r1/invoke-tool", json={"arguments": {}})
    assert r.status_code == 400


def test_invoke_unknown_server(client: TestClient) -> None:
    r = client.post(f"{BASE}/MCP-nope/invoke-tool", json={"tool_name": "x"})
    assert r.status_code == 404


def test_invoke_unknown_tool(client: TestClient, db_session: Session) -> None:
    _seed(db_session, discovered_tools=[{"name": "create_table"}])
    r = client.post(f"{BASE}/MCP-r1/invoke-tool", json={"tool_name": "nope", "arguments": {}})
    data = r.json()["data"]
    assert data["success"] is False and "not found" in data["error"]


def test_invoke_blocked_by_policy(client: TestClient, db_session: Session) -> None:
    _seed(
        db_session,
        discovered_tools=[{"name": "execute_sql"}],
        tool_policies={"execute_sql": {"allowed": False}},
    )
    r = client.post(
        f"{BASE}/MCP-r1/invoke-tool", json={"tool_name": "execute_sql", "arguments": {}}
    )
    data = r.json()["data"]
    assert data["success"] is False and "blocked by policy" in data["error"]


def test_invoke_success(
    client: TestClient, db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    _seed(db_session, discovered_tools=[{"name": "create_table"}])

    async def _ok(*_a: object, **_k: object) -> dict[str, object]:
        return {"ok": True, "table": "t"}

    monkeypatch.setattr(mcp_routes, "invoke_tool_via_gateway", _ok)
    r = client.post(
        f"{BASE}/MCP-r1/invoke-tool", json={"tool_name": "create_table", "arguments": {}}
    )
    data = r.json()["data"]
    assert data["success"] is True and data["result"] == {"ok": True, "table": "t"}


def test_invoke_uses_playground_timeout(
    client: TestClient, db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    _seed(db_session, discovered_tools=[{"name": "create_table"}])
    captured: dict[str, object] = {}

    async def _ok(*_a: object, **kwargs: object) -> dict[str, object]:
        captured.update(kwargs)
        return {"ok": True}

    monkeypatch.setattr(mcp_routes, "invoke_tool_via_gateway", _ok)
    r = client.post(
        f"{BASE}/MCP-r1/invoke-tool",
        json={"tool_name": "create_table", "arguments": {}},
    )
    assert r.status_code == 200
    assert captured["timeout_seconds"] == mcp_routes._PLAYGROUND_INVOKE_TIMEOUT_SECONDS


def test_invoke_gateway_error(
    client: TestClient, db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    _seed(db_session, discovered_tools=[{"name": "create_table"}])

    async def _boom(*_a: object, **_k: object) -> object:
        raise McpGatewayTransportError("tools/call failed")

    monkeypatch.setattr(mcp_routes, "invoke_tool_via_gateway", _boom)
    r = client.post(
        f"{BASE}/MCP-r1/invoke-tool", json={"tool_name": "create_table", "arguments": {}}
    )
    data = r.json()["data"]
    assert data["success"] is False and "tools/call failed" in data["error"]


# ── tool policy ───────────────────────────────────────────────────────────


def test_update_tool_policy(client: TestClient, db_session: Session) -> None:
    _seed(db_session, discovered_tools=[{"name": "execute_sql"}])
    r = client.patch(
        f"{BASE}/MCP-r1/tools/execute_sql/policy",
        json={"side_effect_level": "write", "requires_approval": True},
    )
    assert r.status_code == 200
    policy = r.json()["data"]["policy"]
    assert policy["side_effect_level"] == "write" and policy["requires_approval"] is True


def test_update_tool_policy_empty_body(client: TestClient, db_session: Session) -> None:
    _seed(db_session, discovered_tools=[{"name": "execute_sql"}])
    assert client.patch(f"{BASE}/MCP-r1/tools/execute_sql/policy", json={}).status_code == 400


def test_update_tool_policy_unknown_tool(client: TestClient, db_session: Session) -> None:
    _seed(db_session, discovered_tools=[{"name": "execute_sql"}])
    r = client.patch(f"{BASE}/MCP-r1/tools/nope/policy", json={"allowed": False})
    assert r.status_code == 404


def test_update_tool_policy_unknown_server(client: TestClient) -> None:
    r = client.patch(f"{BASE}/MCP-nope/tools/x/policy", json={"allowed": False})
    assert r.status_code == 404


def test_list_tools_includes_policy(client: TestClient, db_session: Session) -> None:
    _seed(
        db_session,
        discovered_tools=[{"name": "execute_sql", "description": "raw"}],
        tool_policies={"execute_sql": {"allowed": True, "side_effect_level": "write"}},
    )
    r = client.get(f"{BASE}/MCP-r1/tools")
    assert r.status_code == 200
    tools = r.json()["data"]["tools"]
    assert tools[0]["policy"]["side_effect_level"] == "write"


# ── calibration ───────────────────────────────────────────────────────────


def test_save_mcp_tool_test_cases(client: TestClient, db_session: Session) -> None:
    _seed(db_session, discovered_tools=[{"name": "create_table"}])
    r = client.put(
        f"{BASE}/MCP-r1/tools/create_table/test-cases",
        json={"test_cases": [{"name": "basic", "input": {"table": "t"}}]},
    )
    assert r.status_code == 200
    assert r.json()["data"]["test_cases"][0]["name"] == "basic"
    db_session.expire_all()
    server = db_session.get(CaliberMcpServer, "MCP-r1")
    assert server is not None
    assert server.tool_test_cases["create_table"][0]["name"] == "basic"


def test_save_mcp_tool_test_cases_unknown_tool_404(client: TestClient, db_session: Session) -> None:
    _seed(db_session, discovered_tools=[{"name": "create_table"}])
    r = client.put(f"{BASE}/MCP-r1/tools/nope/test-cases", json={"test_cases": []})
    assert r.status_code == 404


def test_save_mcp_tool_test_cases_viewer_forbidden(client: TestClient, db_session: Session) -> None:
    _seed(db_session, discovered_tools=[{"name": "create_table"}])
    r = client.put(
        f"{BASE}/MCP-r1/tools/create_table/test-cases",
        json={"test_cases": []},
        headers={"X-CALIBER-User": "@viewer"},
    )
    assert r.status_code == 403


def test_calibrate_mcp_tool_scores_and_persists(
    client: TestClient, db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    _seed(db_session, discovered_tools=[{"name": "create_table"}])
    client.put(
        f"{BASE}/MCP-r1/tools/create_table/test-cases",
        json={
            "test_cases": [
                {"name": "no_error", "input": {"table": "t"}},
                {
                    "name": "contains_pass",
                    "input": {"table": "t"},
                    "assertion": {"type": "output_contains", "value": "created"},
                },
                {
                    "name": "contains_fail",
                    "input": {"table": "t"},
                    "assertion": {"type": "output_contains", "value": "missing"},
                },
            ]
        },
    )

    async def _ok(*_a: object, **_k: object) -> dict[str, object]:
        return {"status": "created", "table": "t"}

    monkeypatch.setattr(mcp_routes, "invoke_tool_via_gateway", _ok)
    r = client.post(f"{BASE}/MCP-r1/tools/create_table/calibrate")
    assert r.status_code == 200
    data = r.json()["data"]
    assert data["total"] == 3
    assert data["passed"] == 2
    assert data["pass_rate"] == round(2 / 3, 4)
    by_name = {c["name"]: c for c in data["cases"]}
    assert by_name["no_error"]["passed"] is True
    assert by_name["contains_fail"]["passed"] is False
    db_session.expire_all()
    server = db_session.get(CaliberMcpServer, "MCP-r1")
    assert server is not None
    assert server.tool_calibrations["create_table"]["passed"] == 2


def test_calibrate_mcp_tool_gateway_error_fails_case(
    client: TestClient, db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    _seed(db_session, discovered_tools=[{"name": "create_table"}])
    client.put(
        f"{BASE}/MCP-r1/tools/create_table/test-cases",
        json={"test_cases": [{"name": "boom", "input": {}}]},
    )

    async def _boom(*_a: object, **_k: object) -> object:
        raise McpGatewayTransportError("tools/call failed")

    monkeypatch.setattr(mcp_routes, "invoke_tool_via_gateway", _boom)
    r = client.post(f"{BASE}/MCP-r1/tools/create_table/calibrate")
    assert r.status_code == 200
    data = r.json()["data"]
    assert data["passed"] == 0
    assert "tools/call failed" in data["cases"][0]["error"]


def test_calibrate_mcp_tool_no_cases_400(client: TestClient, db_session: Session) -> None:
    _seed(db_session, discovered_tools=[{"name": "create_table"}])
    r = client.post(f"{BASE}/MCP-r1/tools/create_table/calibrate")
    assert r.status_code == 400
    assert "no saved test cases" in r.json()["detail"]


def test_calibrate_mcp_tool_unknown_tool_404(client: TestClient, db_session: Session) -> None:
    _seed(db_session, discovered_tools=[{"name": "create_table"}])
    r = client.post(f"{BASE}/MCP-r1/tools/nope/calibrate")
    assert r.status_code == 404


def test_calibrate_mcp_tool_viewer_forbidden(client: TestClient, db_session: Session) -> None:
    _seed(db_session, discovered_tools=[{"name": "create_table"}])
    r = client.post(
        f"{BASE}/MCP-r1/tools/create_table/calibrate",
        headers={"X-CALIBER-User": "@viewer"},
    )
    assert r.status_code == 403
