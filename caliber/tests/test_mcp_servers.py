"""Integration tests for /caliber/mcp-servers endpoints."""

from __future__ import annotations

from typing import Any

import pytest
from sqlalchemy.orm import Session
from starlette.testclient import TestClient

from caliber.db.models import CaliberMcpServer
from caliber.routes import mcp_servers as mcp_routes

BASE = "/ajax-api/2.0/mlflow/caliber/mcp-servers"
VIEWER_HEADERS = {"X-CALIBER-User": "@viewer"}
DISCOVER_SUFFIX = "/discover-tools"
TOOLS_SUFFIX = "/tools"


@pytest.fixture(autouse=True)
def _mock_mcp_gateway(monkeypatch: pytest.MonkeyPatch) -> None:
    tools_by_server: dict[str, list[dict[str, Any]]] = {
        "github": [{"name": "search_repositories", "description": "Search repos"}],
        "jira": [{"name": "search_issues", "description": "Search issues"}],
        "linear": [{"name": "search_issues", "description": "Search issues"}],
        "mongodb": [{"name": "find", "description": "Find documents"}],
        "pgvector": [{"name": "vector_search", "description": "Run semantic vector search"}],
        "apache age": [
            {"name": "cypher_query", "description": "Run graph queries with openCypher"}
        ],
        "ollama": [{"name": "run", "description": "Run a local Ollama model"}],
        "brave search": [{"name": "web_search", "description": "Web search"}],
        "minio": [{"name": "list_buckets", "description": "List buckets"}],
        "hugging face": [{"name": "search_models", "description": "Search models"}],
        "azure": [{"name": "list_resource_groups", "description": "List resource groups"}],
    }

    async def _discover(
        server: mcp_routes.McpServerConfig, *, timeout_seconds: float = 20.0
    ) -> list[dict[str, Any]]:
        return list(tools_by_server.get(server.name.lower(), []))

    async def _invoke(
        server: mcp_routes.McpServerConfig,
        *,
        tool_name: str,
        arguments: dict[str, Any],
        timeout_seconds: float = 45.0,
    ) -> Any:
        return {
            "server_id": server.server_id,
            "tool_name": tool_name,
            "arguments": arguments,
        }

    monkeypatch.setattr(mcp_routes, "discover_tools_via_gateway", _discover)
    monkeypatch.setattr(mcp_routes, "invoke_tool_via_gateway", _invoke)


def _seed_server(
    db: Session,
    *,
    server_id: str = "MCP-test1",
    name: str = "TestServer",
    transport: str = "stdio",
    command: str = "npx test-server",
    status: str = "active",
    **kwargs: object,
) -> CaliberMcpServer:
    server = CaliberMcpServer(
        server_id=server_id,
        name=name,
        transport=transport,
        command=command,
        status=status,
        **kwargs,
    )
    db.add(server)
    db.commit()
    return server


# ── LIST ────────────────────────────────────────────────────────────────


class TestListMcpServers:
    def test_empty(self, client: TestClient) -> None:
        r = client.get(BASE)
        assert r.status_code == 200
        assert r.json()["data"] == []

    def test_returns_all(self, client: TestClient, db_session: Session) -> None:
        _seed_server(db_session, server_id="MCP-a", name="Alpha")
        _seed_server(db_session, server_id="MCP-b", name="Beta")
        r = client.get(BASE)
        assert r.status_code == 200
        names = [s["name"] for s in r.json()["data"]]
        assert names == ["Alpha", "Beta"]  # ordered by name

    def test_filter_by_status(self, client: TestClient, db_session: Session) -> None:
        _seed_server(db_session, server_id="MCP-a", name="Active", status="active")
        _seed_server(db_session, server_id="MCP-b", name="Errored", status="error")
        r = client.get(f"{BASE}?status=active")
        assert r.status_code == 200
        names = [s["name"] for s in r.json()["data"]]
        assert names == ["Active"]

    def test_filter_all_returns_everything(self, client: TestClient, db_session: Session) -> None:
        _seed_server(db_session, server_id="MCP-a", name="A", status="active")
        _seed_server(db_session, server_id="MCP-b", name="B", status="disabled")
        r = client.get(f"{BASE}?status=all")
        assert r.status_code == 200
        assert len(r.json()["data"]) == 2

    def test_invalid_status_filter(self, client: TestClient) -> None:
        r = client.get(f"{BASE}?status=bogus")
        assert r.status_code == 400

    def test_requires_auth(self, client: TestClient) -> None:
        r = client.get(BASE, headers={"X-CALIBER-User": ""})
        assert r.status_code == 401


# ── GET ─────────────────────────────────────────────────────────────────


class TestGetMcpServer:
    def test_found(self, client: TestClient, db_session: Session) -> None:
        _seed_server(db_session)
        r = client.get(f"{BASE}/MCP-test1")
        assert r.status_code == 200
        assert r.json()["data"]["name"] == "TestServer"

    def test_not_found(self, client: TestClient) -> None:
        r = client.get(f"{BASE}/MCP-nonexistent")
        assert r.status_code == 404


# ── CREATE ──────────────────────────────────────────────────────────────


class TestCreateMcpServer:
    def test_stdio(self, client: TestClient) -> None:
        payload = {
            "name": "MyServer",
            "description": "A test server",
            "transport": "stdio",
            "command": "npx my-server",
            "args": ["-y"],
        }
        r = client.post(BASE, json=payload)
        assert r.status_code == 201
        data = r.json()["data"]
        assert data["name"] == "MyServer"
        assert data["server_id"].startswith("MCP-")
        assert data["status"] == "active"
        assert data["transport"] == "stdio"

    def test_sse(self, client: TestClient) -> None:
        payload = {
            "name": "SseServer",
            "transport": "sse",
            "uri": "http://localhost:8080/sse",
        }
        r = client.post(BASE, json=payload)
        assert r.status_code == 201
        assert r.json()["data"]["transport"] == "sse"

    def test_streamable_http(self, client: TestClient) -> None:
        payload = {
            "name": "StreamServer",
            "transport": "streamable-http",
            "uri": "http://localhost:9090/mcp",
        }
        r = client.post(BASE, json=payload)
        assert r.status_code == 201

    def test_duplicate_name_409(self, client: TestClient, db_session: Session) -> None:
        _seed_server(db_session, name="Taken")
        r = client.post(BASE, json={"name": "Taken", "transport": "stdio", "command": "x"})
        assert r.status_code == 409

    def test_missing_name(self, client: TestClient) -> None:
        r = client.post(BASE, json={"transport": "stdio", "command": "x"})
        assert r.status_code == 400

    def test_invalid_transport(self, client: TestClient) -> None:
        r = client.post(BASE, json={"name": "x", "transport": "websocket"})
        assert r.status_code == 400

    def test_extra_field_rejected(self, client: TestClient) -> None:
        r = client.post(
            BASE,
            json={"name": "x", "transport": "stdio", "command": "y", "bogus": True},
        )
        assert r.status_code == 400

    def test_create_seeds_discovered_tools(self, client: TestClient) -> None:
        # Catalog templates pass a known toolset so tools show before a live
        # discovery (which would otherwise overwrite them on test-connection).
        r = client.post(
            BASE,
            json={
                "name": "Graph DB",
                "transport": "stdio",
                "command": "npx",
                "args": ["-y", "@modelcontextprotocol/server-postgres", "postgresql://localhost/x"],
                "discovered_tools": [
                    {"name": "query", "description": "Run openCypher via cypher(...)"},
                    {"name": "list_tables", "description": "List graphs"},
                ],
            },
        )
        assert r.status_code == 201, r.text
        tools = r.json()["data"]["discovered_tools"]
        assert [t["name"] for t in tools] == ["query", "list_tables"]

    def test_forbidden_for_non_admin(self, client: TestClient) -> None:
        r = client.post(
            BASE,
            json={"name": "x", "transport": "stdio", "command": "y"},
            headers=VIEWER_HEADERS,
        )
        assert r.status_code == 403

    def test_auth_fields_persisted(self, client: TestClient) -> None:
        payload = {
            "name": "AuthServer",
            "transport": "stdio",
            "command": "npx srv",
            "auth_type": "token",
            "auth_config": {"token_env_var": "MY_TOKEN"},
            "env": {"MY_TOKEN": "secret"},
            "icon": "github",
        }
        r = client.post(BASE, json=payload)
        assert r.status_code == 201
        data = r.json()["data"]
        assert data["auth_type"] == "token"
        assert data["auth_config"]["token_env_var"] == "MY_TOKEN"
        assert data["env"]["MY_TOKEN"] == "secret"
        assert data["icon"] == "github"


# ── UPDATE ──────────────────────────────────────────────────────────────


class TestUpdateMcpServer:
    def test_partial_update(self, client: TestClient, db_session: Session) -> None:
        _seed_server(db_session)
        r = client.patch(f"{BASE}/MCP-test1", json={"description": "Updated"})
        assert r.status_code == 200
        assert r.json()["data"]["description"] == "Updated"

    def test_update_status(self, client: TestClient, db_session: Session) -> None:
        _seed_server(db_session)
        r = client.patch(f"{BASE}/MCP-test1", json={"status": "disabled"})
        assert r.status_code == 200
        assert r.json()["data"]["status"] == "disabled"

    def test_no_fields_400(self, client: TestClient, db_session: Session) -> None:
        _seed_server(db_session)
        r = client.patch(f"{BASE}/MCP-test1", json={})
        assert r.status_code == 400

    def test_not_found(self, client: TestClient) -> None:
        r = client.patch(f"{BASE}/MCP-gone", json={"description": "x"})
        assert r.status_code == 404

    def test_noop_returns_200(self, client: TestClient, db_session: Session) -> None:
        _seed_server(db_session, description="same")
        r = client.patch(f"{BASE}/MCP-test1", json={"description": "same"})
        assert r.status_code == 200

    def test_forbidden_for_non_admin(self, client: TestClient, db_session: Session) -> None:
        _seed_server(db_session)
        r = client.patch(
            f"{BASE}/MCP-test1",
            json={"description": "x"},
            headers=VIEWER_HEADERS,
        )
        assert r.status_code == 403


# ── DELETE ──────────────────────────────────────────────────────────────


class TestDeleteMcpServer:
    def test_success(self, client: TestClient, db_session: Session) -> None:
        _seed_server(db_session)
        r = client.delete(f"{BASE}/MCP-test1")
        assert r.status_code == 204
        # Verify it's gone
        r2 = client.get(f"{BASE}/MCP-test1")
        assert r2.status_code == 404

    def test_not_found(self, client: TestClient) -> None:
        r = client.delete(f"{BASE}/MCP-gone")
        assert r.status_code == 404

    def test_forbidden_for_non_admin(self, client: TestClient, db_session: Session) -> None:
        _seed_server(db_session)
        r = client.delete(f"{BASE}/MCP-test1", headers=VIEWER_HEADERS)
        assert r.status_code == 403


# ── TEST CONNECTION ─────────────────────────────────────────────────────


class TestConnection:
    def test_success_with_demo_tools(self, client: TestClient, db_session: Session) -> None:
        _seed_server(db_session, name="GitHub", command="npx server-github")
        r = client.post(f"{BASE}/MCP-test1/test-connection")
        assert r.status_code == 200
        data = r.json()["data"]
        assert data["success"] is True
        assert len(data["tools"]) > 0
        assert data["error"] is None

    def test_success_updates_db(self, client: TestClient, db_session: Session) -> None:
        _seed_server(db_session, name="GitHub", command="npx x")
        client.post(f"{BASE}/MCP-test1/test-connection")
        r = client.get(f"{BASE}/MCP-test1")
        data = r.json()["data"]
        assert data["status"] == "active"
        assert data["last_connected_at"] is not None
        assert len(data["discovered_tools"]) > 0

    def test_stdio_missing_command_fails(self, client: TestClient, db_session: Session) -> None:
        _seed_server(db_session, command="")
        r = client.post(f"{BASE}/MCP-test1/test-connection")
        data = r.json()["data"]
        assert data["success"] is False
        assert "command" in data["error"]

    def test_sse_missing_uri_fails(self, client: TestClient, db_session: Session) -> None:
        _seed_server(db_session, transport="sse", command="", uri="")
        r = client.post(f"{BASE}/MCP-test1/test-connection")
        data = r.json()["data"]
        assert data["success"] is False
        assert "uri" in data["error"]

    def test_error_status_persisted(self, client: TestClient, db_session: Session) -> None:
        _seed_server(db_session, transport="sse", command="", uri="")
        client.post(f"{BASE}/MCP-test1/test-connection")
        r = client.get(f"{BASE}/MCP-test1")
        assert r.json()["data"]["status"] == "error"

    def test_not_found(self, client: TestClient) -> None:
        r = client.post(f"{BASE}/MCP-gone/test-connection")
        assert r.status_code == 404

    def test_forbidden_for_non_admin(self, client: TestClient, db_session: Session) -> None:
        _seed_server(db_session)
        r = client.post(f"{BASE}/MCP-test1/test-connection", headers=VIEWER_HEADERS)
        assert r.status_code == 403

    def test_unknown_server_returns_empty_tools(
        self, client: TestClient, db_session: Session
    ) -> None:
        _seed_server(db_session, name="CustomThing", command="my-custom-server")
        r = client.post(f"{BASE}/MCP-test1/test-connection")
        data = r.json()["data"]
        assert data["success"] is True
        assert data["tools"] == []

    @pytest.mark.parametrize(
        "server_name",
        [
            "Jira",
            "Linear",
            "MongoDB",
            "pgvector",
            "Apache AGE",
            "Ollama",
            "Brave Search",
            "MinIO",
            "Hugging Face",
            "Azure",
        ],
    )
    def test_catalog_servers_have_demo_tools(
        self, client: TestClient, db_session: Session, server_name: str
    ) -> None:
        _seed_server(db_session, name=server_name, command="npx placeholder")
        r = client.post(f"{BASE}/MCP-test1/test-connection")
        data = r.json()["data"]
        assert data["success"] is True
        assert len(data["tools"]) > 0, f"{server_name} should have demo tools"


# ── DISCOVER + TOOLS + POLICY ──────────────────────────────────────────


class TestDiscoverTools:
    def test_discover_tools_refreshes_server_tools(
        self, client: TestClient, db_session: Session
    ) -> None:
        _seed_server(db_session, name="GitHub", command="npx server-github")
        r = client.post(f"{BASE}/MCP-test1{DISCOVER_SUFFIX}")
        assert r.status_code == 200
        data = r.json()["data"]
        assert data["server_id"] == "MCP-test1"
        assert data["tool_count"] > 0
        assert len(data["tools"]) > 0

    def test_discover_tools_forbidden_for_non_admin(
        self, client: TestClient, db_session: Session
    ) -> None:
        _seed_server(db_session)
        r = client.post(f"{BASE}/MCP-test1{DISCOVER_SUFFIX}", headers=VIEWER_HEADERS)
        assert r.status_code == 403


class TestToolListAndPolicy:
    def test_list_tools_includes_effective_policy(
        self, client: TestClient, db_session: Session
    ) -> None:
        _seed_server(
            db_session,
            name="GitHub",
            command="npx server-github",
            discovered_tools=[
                {"name": "search_repositories", "description": "Search repos"},
            ],
            tool_policies={"search_repositories": {"allowed": False, "side_effect_level": "read"}},
        )
        r = client.get(f"{BASE}/MCP-test1{TOOLS_SUFFIX}")
        assert r.status_code == 200
        payload = r.json()["data"]
        assert payload["server_id"] == "MCP-test1"
        assert len(payload["tools"]) == 1
        assert payload["tools"][0]["name"] == "search_repositories"
        assert payload["tools"][0]["policy"]["allowed"] is False

    def test_update_tool_policy(self, client: TestClient, db_session: Session) -> None:
        _seed_server(
            db_session,
            discovered_tools=[{"name": "read_file", "description": "Read"}],
        )
        r = client.patch(
            f"{BASE}/MCP-test1/tools/read_file/policy",
            json={"allowed": False, "requires_approval": True},
        )
        assert r.status_code == 200
        body = r.json()["data"]
        assert body["tool_name"] == "read_file"
        assert body["policy"]["allowed"] is False
        assert body["policy"]["requires_approval"] is True

        server = db_session.get(CaliberMcpServer, "MCP-test1")
        assert server is not None
        assert server.tool_policies["read_file"]["allowed"] is False

    def test_update_tool_policy_rejects_unknown_tool(
        self, client: TestClient, db_session: Session
    ) -> None:
        _seed_server(db_session, discovered_tools=[{"name": "read_file", "description": "Read"}])
        r = client.patch(
            f"{BASE}/MCP-test1/tools/write_file/policy",
            json={"allowed": False},
        )
        assert r.status_code == 404

    def test_policy_blocks_invoke(self, client: TestClient, db_session: Session) -> None:
        _seed_server(
            db_session,
            discovered_tools=[{"name": "read_file", "description": "Read"}],
            tool_policies={"read_file": {"allowed": False}},
        )
        r = client.post(
            f"{BASE}/MCP-test1/invoke-tool",
            json={"tool_name": "read_file", "arguments": {"path": "/tmp/a.txt"}},
        )
        assert r.status_code == 200
        data = r.json()["data"]
        assert data["success"] is False
        assert "blocked by policy" in data["error"]


# ── AUDIT ───────────────────────────────────────────────────────────────


class TestAudit:
    def test_create_produces_audit(self, client: TestClient, db_session: Session) -> None:
        from caliber.db.models import CaliberAuditLog

        client.post(BASE, json={"name": "AuditMe", "transport": "stdio", "command": "x"})
        logs = db_session.query(CaliberAuditLog).filter_by(action="create_mcp_server").all()
        assert len(logs) == 1
        assert logs[0].entity_type == "mcp_server"

    def test_delete_produces_audit(self, client: TestClient, db_session: Session) -> None:
        from caliber.db.models import CaliberAuditLog

        _seed_server(db_session)
        client.delete(f"{BASE}/MCP-test1")
        logs = db_session.query(CaliberAuditLog).filter_by(action="delete_mcp_server").all()
        assert len(logs) == 1

    def test_update_produces_audit(self, client: TestClient, db_session: Session) -> None:
        from caliber.db.models import CaliberAuditLog

        _seed_server(db_session)
        client.patch(f"{BASE}/MCP-test1", json={"description": "Changed"})
        logs = db_session.query(CaliberAuditLog).filter_by(action="update_mcp_server").all()
        assert len(logs) == 1
        assert "changes" in logs[0].details
