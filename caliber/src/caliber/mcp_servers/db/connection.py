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


#: Statement timeout applied to read-only queries, in milliseconds. A tool
#: advertised as read cannot be allowed to pin a backend indefinitely, and the
#: caller has no way to cancel an in-flight MCP request. ``0`` disables it.
_READ_ONLY_TIMEOUT_ENV = "POSTGRES_READ_ONLY_STATEMENT_TIMEOUT_MS"
_DEFAULT_READ_ONLY_TIMEOUT_MS = 30_000


def _dsn() -> str:
    dsn = os.environ.get("POSTGRES_URL", "").strip()
    if not dsn:
        raise DbToolError("POSTGRES_URL is not set in the server environment")
    return dsn


def _read_only_dsn() -> str:
    """DSN for read-only tools.

    ``POSTGRES_READ_ONLY_URL`` lets an operator point read tools at a separate
    least-privilege role, so ``run_query`` is constrained by *grants* and not
    only by the transaction mode below. It is optional: without it read-only
    tools fall back to ``POSTGRES_URL`` and rely on the read-only transaction.
    """
    dsn = os.environ.get("POSTGRES_READ_ONLY_URL", "").strip()
    return dsn or _dsn()


def _read_only_timeout_ms() -> int:
    raw = os.environ.get(_READ_ONLY_TIMEOUT_ENV, "").strip()
    if not raw:
        return _DEFAULT_READ_ONLY_TIMEOUT_MS
    try:
        value = int(raw)
    except ValueError as exc:
        raise DbToolError(f"{_READ_ONLY_TIMEOUT_ENV} must be an integer number of ms") from exc
    return max(value, 0)


@contextmanager
def connect() -> Iterator[Any]:
    """Open an autocommit connection with dict rows; always close it."""
    psycopg, dict_row = _load_psycopg()
    conn = psycopg.connect(_dsn(), autocommit=True, row_factory=dict_row)
    try:
        yield conn
    finally:
        conn.close()


@contextmanager
def connect_read_only() -> Iterator[Any]:
    """Open a connection whose transaction the **engine** refuses writes in.

    A leading-keyword parser is not a security boundary: ``EXPLAIN ANALYZE
    DELETE ...`` and ``SELECT some_writing_function()`` both read as "select"
    while mutating. This is the durable fix — ``read_only=True`` makes psycopg
    begin the transaction with ``READ ONLY``, so PostgreSQL itself raises
    ``read-only transaction`` for any INSERT/UPDATE/DELETE/DDL *and* for a
    write attempted inside a called function.

    Autocommit is deliberately off: an autocommit connection has no surrounding
    transaction for the read-only attribute to apply to. The transaction is
    always rolled back, never committed, so a read tool cannot leave state
    behind even if the engine were misconfigured.
    """
    psycopg, dict_row = _load_psycopg()
    conn = psycopg.connect(_read_only_dsn(), autocommit=False, row_factory=dict_row)
    try:
        # Set before any statement runs: psycopg refuses to change this once a
        # transaction has begun, and autocommit=False begins one lazily.
        conn.read_only = True
        timeout_ms = _read_only_timeout_ms()
        if timeout_ms:
            # LOCAL: scoped to this transaction, so it cannot leak into a pooled
            # session and silently bound a later write tool.
            with conn.cursor() as cur:
                cur.execute(f"SET LOCAL statement_timeout = {timeout_ms}")
        yield conn
    finally:
        try:
            conn.rollback()
        finally:
            conn.close()


def query(sql: str, params: Sequence[Any] | None = None) -> list[dict[str, Any]]:
    """Run a statement and return all rows as dicts."""
    with connect() as conn, conn.cursor() as cur:
        cur.execute(sql, params or [])
        return list(cur.fetchall())


def read_only_query(sql: str, params: Sequence[Any] | None = None) -> list[dict[str, Any]]:
    """Run a statement inside a database-enforced read-only transaction.

    Used by every tool CALIBER classifies ``read`` / no-approval, so that
    classification is backed by the engine rather than by a keyword parser.
    """
    with connect_read_only() as conn, conn.cursor() as cur:
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
