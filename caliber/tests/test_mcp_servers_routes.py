"""Route-level error/edge paths for /caliber/mcp-servers.

Complements ``test_mcp_servers.py`` (happy paths with a success-mocking gateway)
by covering the failure branches: gateway errors on test/discover/invoke,
unknown-tool and policy-blocked invokes, and tool-policy updates.
"""

from __future__ import annotations

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session
from starlette.testclient import TestClient

from caliber.db.models import (
    CaliberAuditLog,
    CaliberMcpServer,
    CaliberWorkflowDeployment,
    CaliberWorkflowVersion,
)
from caliber.mcp_gateway import McpGatewayTransportError
from caliber.mcp_secrets import MCP_WRITE_ONLY_SENTINEL
from caliber.routes import mcp_servers as mcp_routes

BASE = "/ajax-api/2.0/mlflow/caliber/mcp-servers"
AUDIT_BASE = "/ajax-api/2.0/mlflow/caliber/audit-log"


def _seed(db: Session, **kw: object) -> CaliberMcpServer:
    defaults: dict[str, object] = {
        "server_id": "MCP-r1",
        "name": "Relational",
        "transport": "stdio",
        "command": "${PYTHON}",
        "args": ["-m", "caliber.mcp_servers.db", "--mode", "relational"],
        "status": "active",
    }
    discovered = kw.get("discovered_tools")
    if isinstance(discovered, list) and "tool_policies" not in kw:
        kw["tool_policies"] = {
            str(tool["name"]): {
                "allowed": True,
                "side_effect_level": "read",
                "requires_approval": False,
            }
            for tool in discovered
            if isinstance(tool, dict) and tool.get("name")
        }
    defaults.update(kw)
    server = CaliberMcpServer(**defaults)  # type: ignore[arg-type]
    db.add(server)
    db.commit()
    return server


def _raise_gateway(*_a: object, **_k: object) -> object:
    raise McpGatewayTransportError("boom: cannot connect")


def test_mcp_server_history_survives_deletion_and_includes_snapshot(
    client: TestClient, db_session: Session
) -> None:
    """The edit-history (audit trail) is returned even after the server is
    deleted, while literal connection credentials remain write-only."""
    _seed(
        db_session,
        server_id="MCP-h",
        name="Relational",
        env={"PASSWORD": "history-env-secret", "SAFE_REF": "${SAFE_REF}"},
        headers={"Authorization": "Bearer history-header-secret"},
        auth_type="token",
        auth_config={
            "token_env_var": "PASSWORD",
            "token": "history-auth-secret",
        },
    )
    # An edit, then a delete.
    client.patch(f"{BASE}/MCP-h", json={"uri": "stdio://new"})
    assert client.delete(f"{BASE}/MCP-h").status_code == 204

    resp = client.get(f"{BASE}/MCP-h/history")
    assert resp.status_code == 200
    rows = resp.json()["data"]
    actions = [r["action"] for r in rows]
    assert "update_mcp_server" in actions
    assert "delete_mcp_server" in actions  # history outlives the row
    delete_row = next(r for r in rows if r["action"] == "delete_mcp_server")
    snapshot = delete_row["details"]["snapshot"]
    assert snapshot["name"] == "Relational"
    assert snapshot["env"] == {
        "PASSWORD": MCP_WRITE_ONLY_SENTINEL,
        "SAFE_REF": "${SAFE_REF}",
    }
    assert snapshot["headers"]["Authorization"] == MCP_WRITE_ONLY_SENTINEL
    assert snapshot["auth_config"] == {
        "token_env_var": "PASSWORD",
        "token": MCP_WRITE_ONLY_SENTINEL,
    }
    assert "history-env-secret" not in resp.text
    assert "history-header-secret" not in resp.text
    assert "history-auth-secret" not in resp.text


def test_delete_mcp_server_snapshots_definition_without_literal_secrets(
    client: TestClient, db_session: Session
) -> None:
    """A hard delete records structural configuration, never credentials."""
    _seed(
        db_session,
        server_id="MCP-del",
        name="Relational",
        env={"PASSWORD": "delete-env-secret"},
        headers={"Authorization": "Bearer delete-header-secret"},
        auth_config={"password": "delete-auth-secret"},
    )
    resp = client.delete(f"{BASE}/MCP-del")
    assert resp.status_code == 204

    rows = (
        db_session.execute(select(CaliberAuditLog).where(CaliberAuditLog.entity_id == "MCP-del"))
        .scalars()
        .all()
    )
    delete_rows = [r for r in rows if r.action == "delete_mcp_server"]
    assert len(delete_rows) == 1
    snapshot = (delete_rows[0].details or {}).get("snapshot")
    assert snapshot is not None
    assert snapshot["name"] == "Relational"
    assert snapshot["server_id"] == "MCP-del"
    assert snapshot["transport"] == "stdio"
    assert snapshot["env"]["PASSWORD"] == MCP_WRITE_ONLY_SENTINEL
    assert snapshot["headers"]["Authorization"] == MCP_WRITE_ONLY_SENTINEL
    assert snapshot["auth_config"]["password"] == MCP_WRITE_ONLY_SENTINEL
    details_text = str(delete_rows[0].details)
    assert "delete-env-secret" not in details_text
    assert "delete-header-secret" not in details_text
    assert "delete-auth-secret" not in details_text


def test_mcp_history_redacts_legacy_raw_audit_details_on_read(
    client: TestClient, db_session: Session
) -> None:
    """Rows written before the write-only contract cannot leak via history."""
    db_session.add(
        CaliberAuditLog(
            actor="@legacy",
            action="update_mcp_server",
            entity_type="mcp_server",
            entity_id="MCP-legacy",
            details={
                "changes": {
                    "env": {
                        "from": {"TOKEN": "legacy-old-secret"},
                        "to": {"TOKEN": "legacy-new-secret"},
                    },
                    "headers": {
                        "from": {"Authorization": "Bearer old-header"},
                        "to": {"Authorization": "Bearer new-header"},
                    },
                }
            },
        )
    )
    db_session.commit()

    resp = client.get(f"{BASE}/MCP-legacy/history")
    assert resp.status_code == 200
    changes = resp.json()["data"][0]["details"]["changes"]
    assert changes["env"]["from"]["TOKEN"] == MCP_WRITE_ONLY_SENTINEL
    assert changes["env"]["to"]["TOKEN"] == MCP_WRITE_ONLY_SENTINEL
    assert changes["headers"]["from"]["Authorization"] == MCP_WRITE_ONLY_SENTINEL
    assert changes["headers"]["to"]["Authorization"] == MCP_WRITE_ONLY_SENTINEL
    assert "legacy-old-secret" not in resp.text
    assert "legacy-new-secret" not in resp.text
    assert "Bearer old-header" not in resp.text
    assert "Bearer new-header" not in resp.text

    # The generic admin audit explorer and both exports apply the same legacy
    # containment; MCP history is not the only serialization surface.
    audit_page = client.get(AUDIT_BASE, params={"entity_type": "mcp_server"})
    audit_json = client.get(
        f"{AUDIT_BASE}/export",
        params={"entity_type": "mcp_server", "format": "json"},
    )
    audit_csv = client.get(
        f"{AUDIT_BASE}/export",
        params={"entity_type": "mcp_server", "format": "csv"},
    )
    for audit_response in (audit_page, audit_json, audit_csv):
        assert audit_response.status_code == 200
        assert MCP_WRITE_ONLY_SENTINEL in audit_response.text
        assert "legacy-old-secret" not in audit_response.text
        assert "legacy-new-secret" not in audit_response.text
        assert "Bearer old-header" not in audit_response.text
        assert "Bearer new-header" not in audit_response.text


def test_delete_mcp_server_blocked_when_referenced_by_active_deployment(
    client: TestClient, db_session: Session
) -> None:
    """Deleting a server bound by a live deployment is a 409, not a silent orphan."""
    _seed(db_session, server_id="MCP-ref")
    db_session.add(
        CaliberWorkflowVersion(
            version_id="WFV-ref",
            workflow_id="WF-ref",
            version_number=1,
            status="published",
            manifest={"tools": {"db": {"server_id": "MCP-ref", "tool_name": "query"}}},
        )
    )
    db_session.add(
        CaliberWorkflowDeployment(
            deployment_id="DEP-ref",
            workflow_id="WF-ref",
            alias="prod",
            version_id="WFV-ref",
            status="active",
        )
    )
    db_session.commit()

    resp = client.delete(f"{BASE}/MCP-ref")
    assert resp.status_code == 409
    assert "referenced" in resp.json()["detail"]
    # The server must still exist (delete was refused).
    assert db_session.get(CaliberMcpServer, "MCP-ref") is not None


def test_delete_mcp_server_detects_direct_resource_node_dependency(
    client: TestClient, db_session: Session
) -> None:
    _seed(db_session, server_id="MCP-node")
    db_session.add(
        CaliberWorkflowVersion(
            version_id="WFV-node-ref",
            workflow_id="WF-node-ref",
            version_number=1,
            status="published",
            manifest={
                "nodes": {
                    "lookup": {
                        "id": "lookup",
                        "type": "mcp_resource",
                        "server_id": "MCP-node",
                        "tool_name": "query",
                    }
                }
            },
        )
    )
    db_session.add(
        CaliberWorkflowDeployment(
            deployment_id="DEP-node-ref",
            workflow_id="WF-node-ref",
            alias="prod",
            version_id="WFV-node-ref",
            status="active",
        )
    )
    db_session.commit()

    response = client.delete(f"{BASE}/MCP-node")

    assert response.status_code == 409
    assert db_session.get(CaliberMcpServer, "MCP-node") is not None


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
        tool_policies={
            "execute_sql": {
                "allowed": False,
                "side_effect_level": "external_action",
                "requires_approval": True,
            }
        },
    )
    r = client.post(
        f"{BASE}/MCP-r1/invoke-tool", json={"tool_name": "execute_sql", "arguments": {}}
    )
    data = r.json()["data"]
    assert data["success"] is False and "blocked by policy" in data["error"]


def test_invoke_unclassified_tool_fails_closed(client: TestClient, db_session: Session) -> None:
    _seed(
        db_session,
        discovered_tools=[{"name": "execute_sql"}],
        tool_policies={},
    )
    r = client.post(
        f"{BASE}/MCP-r1/invoke-tool",
        json={"tool_name": "execute_sql", "arguments": {}},
    )
    data = r.json()["data"]
    assert data["success"] is False
    assert "no explicit allow" in data["error"]


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


def test_calibrate_mcp_tool_rejects_approval_required_policy(
    client: TestClient, db_session: Session
) -> None:
    _seed(
        db_session,
        discovered_tools=[{"name": "execute_sql"}],
        tool_policies={
            "execute_sql": {
                "allowed": True,
                "side_effect_level": "external_action",
                "requires_approval": True,
            }
        },
        tool_test_cases={"execute_sql": [{"name": "write", "input": {}}]},
    )
    r = client.post(f"{BASE}/MCP-r1/tools/execute_sql/calibrate")
    assert r.status_code == 409
    assert "no approval checkpoint" in r.json()["detail"]


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


def test_delete_mcp_server_blocked_by_a_rollback_checkpoint_reference(
    client: TestClient, db_session: Session
) -> None:
    """Regression: deletion checked only the alias's *current* target.

    A rollback checkpoint is a promise that the alias can go back to that
    version. Deleting a server that only an older checkpointed version depends on
    silently converted a one-click rollback into a broken deployment discovered
    at the next run.
    """
    _seed(db_session, server_id="MCP-ckpt")
    # Current target: no MCP dependency at all.
    db_session.add(
        CaliberWorkflowVersion(
            version_id="WFV-ckpt-new",
            workflow_id="WF-ckpt",
            version_number=2,
            status="published",
            manifest={"nodes": {}},
        )
    )
    # Checkpointed predecessor: this is the one that binds the server.
    db_session.add(
        CaliberWorkflowVersion(
            version_id="WFV-ckpt-old",
            workflow_id="WF-ckpt",
            version_number=1,
            status="published",
            manifest={"tools": {"db": {"server_id": "MCP-ckpt", "tool_name": "query"}}},
        )
    )
    db_session.add(
        CaliberWorkflowDeployment(
            deployment_id="DEP-ckpt",
            workflow_id="WF-ckpt",
            alias="prod",
            version_id="WFV-ckpt-new",
            status="active",
            rollback_checkpoint=[{"version_id": "WFV-ckpt-old", "deployed_by": "@ops"}],
        )
    )
    db_session.commit()

    resp = client.delete(f"{BASE}/MCP-ckpt")
    assert resp.status_code == 409
    detail = resp.json()["detail"]
    assert "rollback checkpoint" in detail
    assert "WFV-ckpt-old" in detail
    assert db_session.get(CaliberMcpServer, "MCP-ckpt") is not None


def test_delete_mcp_server_allowed_when_nothing_current_or_checkpointed_uses_it(
    client: TestClient, db_session: Session
) -> None:
    """The checkpoint check must not become a blanket refusal: an unreferenced
    server still deletes."""
    _seed(db_session, server_id="MCP-free")
    db_session.add(
        CaliberWorkflowVersion(
            version_id="WFV-free-new",
            workflow_id="WF-free",
            version_number=2,
            status="published",
            manifest={"nodes": {}},
        )
    )
    db_session.add(
        CaliberWorkflowVersion(
            version_id="WFV-free-old",
            workflow_id="WF-free",
            version_number=1,
            status="published",
            manifest={"tools": {"other": {"server_id": "MCP-somewhere-else", "tool_name": "q"}}},
        )
    )
    db_session.add(
        CaliberWorkflowDeployment(
            deployment_id="DEP-free",
            workflow_id="WF-free",
            alias="prod",
            version_id="WFV-free-new",
            status="active",
            rollback_checkpoint=[{"version_id": "WFV-free-old"}],
        )
    )
    db_session.commit()

    assert client.delete(f"{BASE}/MCP-free").status_code == 204
    assert db_session.get(CaliberMcpServer, "MCP-free") is None
