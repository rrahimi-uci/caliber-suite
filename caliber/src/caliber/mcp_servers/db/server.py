"""Build the DB MCP server for a given mode.

One FastMCP server per mode (``relational`` | ``vector`` | ``graph``) so each
CALIBER catalog entry that spawns this module exposes only its class's tools.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any, Literal

# ``FastMCP`` was renamed to ``MCPServer`` in mcp 2.x (mcp.server.fastmcp raises
# ModuleNotFoundError with a migration pointer now); ``host``/``port``/
# ``stateless_http``/``json_response`` moved off the constructor and onto
# ``run_streamable_http_async``/``run_sse_async`` at the same time. The alias
# keeps every ``FastMCP``-typed annotation below accurate to what the object
# actually is post-rename, and ``_HttpBoundServer`` re-threads the moved
# constructor kwargs back through ``.run(transport=...)`` so ``build_server``'s
# own signature -- and every caller of it -- is unaffected by the rename.
from mcp.server.mcpserver import MCPServer as FastMCP

from caliber.mcp_servers.db import tools_graph as graph
from caliber.mcp_servers.db import tools_relational as rel
from caliber.mcp_servers.db import tools_vector as vec

MODES = ("relational", "vector", "graph")


class _HttpBoundServer(FastMCP):
    """A server whose HTTP-transport options are fixed at construction time.

    mcp 2.x moved ``host``/``port``/``stateless_http``/``json_response`` from
    the constructor to ``run_sse_async``/``run_streamable_http_async``, which
    would otherwise force every ``build_server`` caller to also learn the
    transport-specific kwarg shape. This class hides that: it stores the four
    values once and injects them into ``.run(transport=...)`` for the two
    transports that use them, leaving ``stdio`` (which takes none of them)
    untouched.
    """

    def __init__(
        self,
        name: str,
        *,
        host: str,
        port: int,
        stateless_http: bool,
        json_response: bool,
    ) -> None:
        super().__init__(name)
        self._host = host
        self._port = port
        self._stateless_http = stateless_http
        self._json_response = json_response

    def run(
        self,
        transport: Literal["stdio", "sse", "streamable-http"] = "stdio",
        **kwargs: Any,
    ) -> None:
        if transport in ("sse", "streamable-http"):
            kwargs.setdefault("host", self._host)
            kwargs.setdefault("port", self._port)
            kwargs.setdefault("stateless_http", self._stateless_http)
            kwargs.setdefault("json_response", self._json_response)
        super().run(transport, **kwargs)


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


def build_server(
    mode: str,
    *,
    host: str = "127.0.0.1",
    port: int = 8000,
) -> FastMCP:
    """Construct a FastMCP server exposing the tools for ``mode``."""
    if mode not in _TOOLS_BY_MODE:
        raise ValueError(f"unknown mode {mode!r}; expected one of {MODES}")
    app: FastMCP = _HttpBoundServer(
        f"caliber-db-{mode}",
        host=host,
        port=port,
        stateless_http=True,
        json_response=True,
    )
    for tool in _TOOLS_BY_MODE[mode]:
        app.tool()(tool)
    return app
