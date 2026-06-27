"""Apache AGE (graph / openCypher) tools for the DB MCP server.

AGE cannot bind parameters into ``cypher(graph, $$...$$)`` — the graph name and
query body must be literals — so the graph name is regex-validated and inlined,
properties are serialized into escaped Cypher map literals
(:func:`identifiers.cypher_map`), and the result column list is validated. The
``cypher_query`` tool is the raw escape hatch (body checked only for ``$$``).
"""

from __future__ import annotations

from typing import Any

from caliber.mcp_servers.db import connection as conn
from caliber.mcp_servers.db import identifiers as ids


def create_graph(graph: str) -> dict[str, Any]:
    """Create a named graph (an AGE schema)."""
    name = ids.validate_identifier(graph, kind="graph")
    conn.execute(f"SELECT create_graph('{name}')")
    return {"ok": True, "graph": name}


def drop_graph(graph: str, cascade: bool = True) -> dict[str, Any]:
    """Drop a graph and (by default) everything in it."""
    name = ids.validate_identifier(graph, kind="graph")
    flag = "true" if cascade else "false"
    conn.execute(f"SELECT drop_graph('{name}', {flag})")
    return {"ok": True, "graph": name}


def create_vertex(graph: str, label: str, props: dict[str, Any] | None = None) -> dict[str, Any]:
    """Create a vertex ``(:label {props})`` and return it."""
    name = ids.validate_identifier(graph, kind="graph")
    ids.validate_identifier(label, kind="label")
    body = f"CREATE (n:{label} {ids.cypher_map(props)}) RETURN n"
    rows = conn.query(
        f"SELECT * FROM cypher('{name}', $$ {body} $$) AS (n agtype)"  # noqa: S608
    )
    return {"ok": True, "vertex": ids.parse_agtype(rows[0]["n"]) if rows else None}


def create_edge(
    graph: str,
    from_match: dict[str, Any],
    to_match: dict[str, Any],
    label: str,
    props: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Match two vertices by properties and connect them with a ``label`` edge."""
    name = ids.validate_identifier(graph, kind="graph")
    ids.validate_identifier(label, kind="label")
    if not from_match or not to_match:
        raise ids.DbToolError("from_match and to_match must each be a non-empty object")
    body = (
        f"MATCH (a {ids.cypher_map(from_match)}), (b {ids.cypher_map(to_match)}) "
        f"CREATE (a)-[r:{label} {ids.cypher_map(props)}]->(b) RETURN r"
    )
    rows = conn.query(
        f"SELECT * FROM cypher('{name}', $$ {body} $$) AS (r agtype)"  # noqa: S608
    )
    return {"ok": True, "edge": ids.parse_agtype(rows[0]["r"]) if rows else None}


def cypher_query(graph: str, cypher: str, columns: list[str] | None = None) -> dict[str, Any]:
    """Escape hatch: run raw openCypher against ``graph``.

    ``columns`` names the returned columns (each becomes ``<name> agtype``);
    default is a single ``result`` column. The body may not contain ``$$``.
    """
    name = ids.validate_identifier(graph, kind="graph")
    body = ids.assert_cypher_body_safe(cypher)
    cols = ids.compose_cypher_columns(columns)
    rows = conn.query(
        f"SELECT * FROM cypher('{name}', $$ {body} $$) AS ({cols})"  # noqa: S608
    )
    parsed = [{key: ids.parse_agtype(value) for key, value in row.items()} for row in rows]
    return {"rows": parsed, "row_count": len(parsed)}
