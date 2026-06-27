"""pgvector (vector similarity) tools for the DB MCP server."""

from __future__ import annotations

import json
from typing import Any

from caliber.mcp_servers.db import connection as conn
from caliber.mcp_servers.db import identifiers as ids


def create_vector_table(
    table: str, dim: int, metric: str = "cosine", id_type: str = "text"
) -> dict[str, Any]:
    """Create a vector table ``(id, embedding vector(dim), metadata jsonb)`` + HNSW index.

    ``metric`` is one of cosine | l2 | ip (drives the index opclass and the
    operator used by ``similarity_search``).
    """
    ddl, index = ids.compose_create_vector_table(table, dim, metric=metric, id_type=id_type)
    conn.execute_script([ddl, index])
    return {"ok": True, "table": table, "dim": dim, "metric": metric}


def upsert_vectors(table: str, rows: list[dict[str, Any]]) -> dict[str, Any]:
    """Insert/update vectors. Each row: ``{id, embedding: [float], metadata?: {}}``."""
    if not rows:
        raise ids.DbToolError("upsert_vectors requires at least one row")
    sql = ids.compose_upsert_vector(table)
    payload: list[list[Any]] = []
    for row in rows:
        if "id" not in row or "embedding" not in row:
            raise ids.DbToolError("each row needs 'id' and 'embedding'")
        payload.append(
            [
                row["id"],
                ids.embedding_to_literal(row["embedding"]),
                json.dumps(row.get("metadata") or {}),
            ]
        )
    conn.execute_many(sql, payload)
    return {"ok": True, "upserted": len(rows)}


def similarity_search(
    table: str, embedding: list[float], k: int = 5, metric: str = "cosine"
) -> dict[str, Any]:
    """Return the ``k`` nearest rows to ``embedding`` by ``metric`` distance."""
    if not isinstance(k, int) or isinstance(k, bool) or k <= 0:
        raise ids.DbToolError("k must be a positive integer")
    sql = ids.compose_similarity_search(table, metric)
    rows = conn.query(sql, [ids.embedding_to_literal(embedding), k])
    return {
        "matches": [
            {
                "id": row["id"],
                "distance": float(row["distance"]),
                "metadata": row["metadata"],
            }
            for row in rows
        ]
    }
