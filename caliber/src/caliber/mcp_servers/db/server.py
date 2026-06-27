"""Build the DB MCP server for a given mode.

One FastMCP server per mode (``relational`` | ``vector`` | ``graph``) so each
CALIBER catalog entry that spawns this module exposes only its class's tools.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from mcp.server.fastmcp import FastMCP

from caliber.mcp_servers.db import tools_graph as graph
from caliber.mcp_servers.db import tools_relational as rel
from caliber.mcp_servers.db import tools_vector as vec

MODES = ("relational", "vector", "graph")

#: Tool callables registered for each mode. Vector and graph modes include the
#: read-only relational basics so an operator can inspect the schema too.
_TOOLS_BY_MODE: dict[str, tuple[Callable[..., Any], ...]] = {
    "relational": (
        rel.list_tables,
        rel.describe_table,
        rel.create_table,
        rel.insert_rows,
        rel.update_rows,
        rel.delete_rows,
        rel.run_query,
        rel.execute_sql,
    ),
    "vector": (
        rel.list_tables,
        rel.describe_table,
        rel.run_query,
        vec.create_vector_table,
        vec.upsert_vectors,
        vec.similarity_search,
    ),
    "graph": (
        rel.run_query,
        graph.create_graph,
        graph.drop_graph,
        graph.create_vertex,
        graph.create_edge,
        graph.cypher_query,
    ),
}


def build_server(mode: str) -> FastMCP:
    """Construct a FastMCP server exposing the tools for ``mode``."""
    if mode not in _TOOLS_BY_MODE:
        raise ValueError(f"unknown mode {mode!r}; expected one of {MODES}")
    app: FastMCP = FastMCP(f"caliber-db-{mode}")
    for tool in _TOOLS_BY_MODE[mode]:
        app.tool()(tool)
    return app
