"""Integration tests for the first-party DB MCP server over stdio.

Exercises the real gateway → stdio subprocess → Postgres path for all three
modes. Skipped unless ``psycopg`` is installed AND ``POSTGRES_URL`` points at a
reachable Postgres with the ``vector`` + ``age`` extensions (i.e. the
``deploy/mcp`` container). Mirrors ``test_mcp_gateway.py``.

    docker compose -f deploy/mcp/docker-compose.yml up -d --build
    POSTGRES_URL=postgresql://caliber:caliber@localhost:5432/caliber \
        pytest tests/test_mcp_db_server.py
"""

from __future__ import annotations

import contextlib
import os
import sys

import pytest

from caliber.mcp_gateway import McpServerConfig, discover_tools_sync, invoke_tool_sync

psycopg = pytest.importorskip("psycopg")

POSTGRES_URL = os.environ.get("POSTGRES_URL", "").strip()

pytestmark = [
    pytest.mark.integration,
    pytest.mark.slow,
    pytest.mark.skipif(not POSTGRES_URL, reason="POSTGRES_URL not set"),
]


def _postgres_reachable() -> bool:
    try:
        with psycopg.connect(POSTGRES_URL, connect_timeout=3) as conn:  # type: ignore[union-attr]
            conn.execute("SELECT 1")
        return True
    except Exception:
        return False


if POSTGRES_URL and not _postgres_reachable():
    pytest.skip("POSTGRES_URL set but Postgres unreachable", allow_module_level=True)


def _cfg(mode: str) -> McpServerConfig:
    return McpServerConfig(
        server_id=f"test-db-{mode}",
        name=f"db-{mode}",
        transport="stdio",
        uri="",
        command=sys.executable,
        args=("-m", "caliber.mcp_servers.db", "--mode", mode),
        env={"POSTGRES_URL": POSTGRES_URL},
        headers={},
        auth_type="none",
        auth_config={},
    )


def _invoke(mode: str, tool: str, **arguments: object) -> object:
    return invoke_tool_sync(_cfg(mode), tool_name=tool, arguments=arguments, timeout_seconds=30.0)


def test_relational_discovers_and_round_trips() -> None:
    names = {t["name"] for t in discover_tools_sync(_cfg("relational"))}
    assert {"create_table", "insert_rows", "update_rows", "run_query"} <= names

    table = "caliber_it_rel"
    _invoke("relational", "execute_sql", sql=f"DROP TABLE IF EXISTS {table}")
    try:
        _invoke(
            "relational",
            "create_table",
            table=table,
            columns=[
                {"name": "id", "type": "serial", "primary_key": True},
                {"name": "label", "type": "text"},
            ],
        )
        assert _invoke(
            "relational", "insert_rows", table=table, rows=[{"label": "a"}, {"label": "b"}]
        ) == {
            "ok": True,
            "inserted": 2,
        }
        assert _invoke(
            "relational",
            "update_rows",
            table=table,
            set_values={"label": "c"},
            where={"label": "a"},
        ) == {"ok": True, "updated": 1}
        result = _invoke("relational", "run_query", sql=f"SELECT label FROM {table} ORDER BY id")
        assert result == {"rows": [{"label": "c"}, {"label": "b"}], "row_count": 2}
    finally:
        _invoke("relational", "execute_sql", sql=f"DROP TABLE IF EXISTS {table}")


def test_vector_upsert_and_search() -> None:
    names = {t["name"] for t in discover_tools_sync(_cfg("vector"))}
    assert {"create_vector_table", "upsert_vectors", "similarity_search"} <= names

    table = "caliber_it_vec"
    _invoke("vector", "run_query", sql="SELECT 1")  # warm/validate connection
    try:
        _invoke("vector", "create_vector_table", table=table, dim=3, metric="cosine")
        _invoke(
            "vector",
            "upsert_vectors",
            table=table,
            rows=[
                {"id": "x", "embedding": [1, 2, 3], "metadata": {"t": 1}},
                {"id": "y", "embedding": [9, 9, 9]},
            ],
        )
        result = _invoke("vector", "similarity_search", table=table, embedding=[1, 2, 4], k=2)
        assert isinstance(result, dict)
        matches = result["matches"]  # type: ignore[index]
        assert [m["id"] for m in matches] == ["x", "y"]
        assert matches[0]["distance"] < matches[1]["distance"]
    finally:
        _invoke("vector", "run_query", sql="SELECT 1")
        # cleanup via relational mode (execute_sql isn't exposed in vector mode)
        invoke_tool_sync(
            _cfg("relational"),
            tool_name="execute_sql",
            arguments={"sql": f"DROP TABLE IF EXISTS {table}"},
            timeout_seconds=30.0,
        )


def test_graph_vertices_edge_and_cypher() -> None:
    names = {t["name"] for t in discover_tools_sync(_cfg("graph"))}
    assert {"create_graph", "create_vertex", "create_edge", "cypher_query"} <= names

    graph = "caliber_it_g"
    with contextlib.suppress(Exception):
        _invoke("graph", "drop_graph", graph=graph)
    try:
        _invoke("graph", "create_graph", graph=graph)
        _invoke("graph", "create_vertex", graph=graph, label="Person", props={"name": "Ada"})
        _invoke("graph", "create_vertex", graph=graph, label="Person", props={"name": "Bob"})
        edge = _invoke(
            "graph",
            "create_edge",
            graph=graph,
            from_match={"name": "Ada"},
            to_match={"name": "Bob"},
            label="KNOWS",
            props={"since": 2020},
        )
        assert edge["edge"]["label"] == "KNOWS"  # type: ignore[index]
        result = _invoke(
            "graph",
            "cypher_query",
            graph=graph,
            cypher="MATCH (a)-[:KNOWS]->(b) RETURN a.name, b.name",
            columns=["a_name", "b_name"],
        )
        assert result == {"rows": [{"a_name": "Ada", "b_name": "Bob"}], "row_count": 1}
    finally:
        _invoke("graph", "drop_graph", graph=graph)
