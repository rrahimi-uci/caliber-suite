"""Thin psycopg connection layer for the DB MCP server.

``psycopg`` is imported lazily (mirroring how :mod:`caliber.llm.provider` lazy
imports the optional ``agents`` SDK) so that importing this package — and the
pure :mod:`caliber.mcp_servers.db.identifiers` builders it ships — never
requires the ``[postgres]`` extra. The driver is only needed when a tool
actually runs against Postgres.
"""

from __future__ import annotations

import os
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from typing import Any

from caliber.mcp_servers.db.identifiers import DbToolError


def _load_psycopg() -> tuple[Any, Any]:
    """Return ``(psycopg, dict_row)`` or raise a clean ``DbToolError``."""
    try:
        import psycopg  # noqa: PLC0415 (lazy import is deliberate)
        from psycopg.rows import dict_row  # noqa: PLC0415
    except ImportError as exc:  # pragma: no cover - exercised only without the extra
        raise DbToolError("psycopg is not installed; install caliber[postgres]") from exc
    return psycopg, dict_row


def _dsn() -> str:
    dsn = os.environ.get("POSTGRES_URL", "").strip()
    if not dsn:
        raise DbToolError("POSTGRES_URL is not set in the server environment")
    return dsn


@contextmanager
def connect() -> Iterator[Any]:
    """Open an autocommit connection with dict rows; always close it."""
    psycopg, dict_row = _load_psycopg()
    conn = psycopg.connect(_dsn(), autocommit=True, row_factory=dict_row)
    try:
        yield conn
    finally:
        conn.close()


def query(sql: str, params: Sequence[Any] | None = None) -> list[dict[str, Any]]:
    """Run a statement and return all rows as dicts."""
    with connect() as conn, conn.cursor() as cur:
        cur.execute(sql, params or [])
        return list(cur.fetchall())


def execute(sql: str, params: Sequence[Any] | None = None) -> int:
    """Run a single statement and return the affected row count."""
    with connect() as conn, conn.cursor() as cur:
        cur.execute(sql, params or [])
        return int(cur.rowcount)


def execute_many(sql: str, rows: Sequence[Sequence[Any]]) -> int:
    """Run a statement once per row, atomically; return the number submitted.

    Wrapped in a single transaction so a mid-batch failure rolls the WHOLE
    operation back — under plain autocommit the earlier rows would already be
    durably written even though the tool reports the batch as failed.
    """
    with connect() as conn, conn.transaction(), conn.cursor() as cur:
        cur.executemany(sql, rows)
        return len(rows)


def execute_script(statements: Sequence[str]) -> None:
    """Run several statements on one connection (e.g. table + index), atomically.

    A single transaction so a later statement's failure (e.g. a failed index
    build) rolls back the earlier ones rather than leaving a half-built schema.
    """
    with connect() as conn, conn.transaction(), conn.cursor() as cur:
        for sql in statements:
            cur.execute(sql)
