"""Relational (plain SQL) tools for the DB MCP server.

Each function is a plain, signature-introspectable callable that FastMCP
registers as a tool. Identifiers are validated + quoted by
:mod:`caliber.mcp_servers.db.identifiers`; values are always bound as
parameters. Tools raise :class:`DbToolError` on bad input — FastMCP turns that
into the tool's error message.
"""

from __future__ import annotations

from typing import Any

from caliber.mcp_servers.db import connection as conn
from caliber.mcp_servers.db import identifiers as ids


def list_tables() -> dict[str, Any]:
    """List the tables in the public schema."""
    rows = conn.query(
        "SELECT table_name FROM information_schema.tables "
        "WHERE table_schema = 'public' ORDER BY table_name"
    )
    return {"tables": [row["table_name"] for row in rows]}


def describe_table(table: str) -> dict[str, Any]:
    """Return the columns (name, type, nullability, default) of a table."""
    ids.validate_identifier(table, kind="table")
    rows = conn.query(
        "SELECT column_name, data_type, is_nullable, column_default "
        "FROM information_schema.columns "
        "WHERE table_schema = 'public' AND table_name = %s "
        "ORDER BY ordinal_position",
        [table],
    )
    if not rows:
        raise ids.DbToolError(f"table {table!r} not found in the public schema")
    return {
        "table": table,
        "columns": [
            {
                "name": row["column_name"],
                "type": row["data_type"],
                "nullable": row["is_nullable"] == "YES",
                "default": row["column_default"],
            }
            for row in rows
        ],
    }


def create_table(
    table: str, columns: list[dict[str, Any]], if_not_exists: bool = True
) -> dict[str, Any]:
    """Create a table. ``columns`` items take name, type, primary_key?, not_null?."""
    conn.execute(ids.compose_create_table(table, columns, if_not_exists=if_not_exists))
    return {"ok": True, "table": table}


def insert_rows(table: str, rows: list[dict[str, Any]]) -> dict[str, Any]:
    """Insert one or more rows. Every row must share the same columns."""
    if not rows:
        raise ids.DbToolError("insert_rows requires at least one row")
    columns = list(rows[0].keys())
    if not columns:
        raise ids.DbToolError("rows must have at least one column")
    for row in rows:
        if list(row.keys()) != columns:
            raise ids.DbToolError("all rows must have the same columns")
    sql = ids.compose_insert(table, columns)
    inserted = conn.execute_many(sql, [[row[col] for col in columns] for row in rows])
    return {"ok": True, "inserted": inserted}


def update_rows(
    table: str,
    set_values: dict[str, Any],
    where: dict[str, Any] | None = None,
    allow_full: bool = False,
) -> dict[str, Any]:
    """Update rows matching ``where`` (equality AND). Set ``allow_full`` for no WHERE."""
    if not set_values:
        raise ids.DbToolError("update_rows requires at least one column in set_values")
    ids.require_where(where, allow_full)
    set_columns = list(set_values.keys())
    where_columns = list(where.keys()) if where else []
    sql = ids.compose_update(table, set_columns, where_columns)
    params = [set_values[col] for col in set_columns]
    params += [where[col] for col in where_columns] if where else []
    return {"ok": True, "updated": conn.execute(sql, params)}


def delete_rows(
    table: str, where: dict[str, Any] | None = None, allow_full: bool = False
) -> dict[str, Any]:
    """Delete rows matching ``where`` (equality AND). Set ``allow_full`` for no WHERE."""
    ids.require_where(where, allow_full)
    where_columns = list(where.keys()) if where else []
    sql = ids.compose_delete(table, where_columns)
    params = [where[col] for col in where_columns] if where else []
    return {"ok": True, "deleted": conn.execute(sql, params)}


def run_query(sql: str, params: list[Any] | None = None) -> dict[str, Any]:
    """Run a read-only query (SELECT/WITH/...) with optional bound parameters."""
    ids.assert_read_only(sql)
    rows = conn.query(sql, params or [])
    return {"rows": rows, "row_count": len(rows)}


def execute_sql(sql: str) -> dict[str, Any]:
    """Escape hatch: run arbitrary DDL/DML. Caller owns the SQL — no parameters."""
    return {"ok": True, "rowcount": conn.execute(sql)}
