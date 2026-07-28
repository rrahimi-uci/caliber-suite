"""In-process tests for the DB MCP tool wrappers, server builder, and connection.

These don't touch Postgres: the connection layer is monkeypatched so the tool
logic (SQL/params it builds, result shaping, guards) is exercised and measured
without a subprocess. The real DB path is covered by the skip-gated integration
tests in ``test_mcp_db_server.py``.
"""

from __future__ import annotations

import asyncio
import contextlib
from typing import Any

import pytest

from caliber.mcp_servers.db import connection as conn
from caliber.mcp_servers.db import identifiers as ids
from caliber.mcp_servers.db import server as server_mod
from caliber.mcp_servers.db import tools_graph as graph
from caliber.mcp_servers.db import tools_relational as rel
from caliber.mcp_servers.db import tools_vector as vec


class _Recorder:
    """Stand-in for the connection module's execute/query helpers."""

    def __init__(self) -> None:
        self.executed: list[tuple[str, Any]] = []
        self.many: list[tuple[str, list[Any]]] = []
        self.scripts: list[list[str]] = []
        self.rows: list[dict[str, Any]] = []
        self.rowcount = 1
        #: SQL routed through the *read-only* helper, kept separate so a test can
        #: assert that a read-classified tool did not fall back to the
        #: read-write ``query`` path.
        self.read_only: list[tuple[str, Any]] = []

    def query(self, sql: str, params: Any = None) -> list[dict[str, Any]]:
        self.executed.append((sql, params))
        return self.rows

    def read_only_query(self, sql: str, params: Any = None) -> list[dict[str, Any]]:
        self.executed.append((sql, params))
        self.read_only.append((sql, params))
        return self.rows

    def execute(self, sql: str, params: Any = None) -> int:
        self.executed.append((sql, params))
        return self.rowcount

    def execute_many(self, sql: str, rows: list[Any]) -> int:
        self.many.append((sql, list(rows)))
        return len(rows)

    def execute_script(self, statements: list[str]) -> None:
        self.scripts.append(list(statements))


@pytest.fixture
def rec(monkeypatch: pytest.MonkeyPatch) -> _Recorder:
    recorder = _Recorder()
    for name in ("query", "read_only_query", "execute", "execute_many", "execute_script"):
        monkeypatch.setattr(conn, name, getattr(recorder, name))
    return recorder


# ---------------------------------------------------------------------------
# Relational tools
# ---------------------------------------------------------------------------


def test_list_and_describe(rec: _Recorder) -> None:
    rec.rows = [{"table_name": "a"}, {"table_name": "b"}]
    assert rel.list_tables() == {"tables": ["a", "b"]}

    rec.rows = [
        {"column_name": "id", "data_type": "integer", "is_nullable": "NO", "column_default": None}
    ]
    out = rel.describe_table("t")
    assert out["columns"][0] == {
        "name": "id",
        "type": "integer",
        "nullable": False,
        "default": None,
    }

    rec.rows = []
    with pytest.raises(ids.DbToolError):
        rel.describe_table("missing")


def test_create_table_emits_ddl(rec: _Recorder) -> None:
    assert rel.create_table("t", [{"name": "id", "type": "serial"}]) == {"ok": True, "table": "t"}
    sql, _ = rec.executed[-1]
    assert sql.startswith('CREATE TABLE IF NOT EXISTS "t"')


def test_insert_rows(rec: _Recorder) -> None:
    out = rel.insert_rows("t", [{"a": 1, "b": 2}, {"a": 3, "b": 4}])
    assert out == {"ok": True, "inserted": 2}
    sql, rows = rec.many[-1]
    assert sql == 'INSERT INTO "t" ("a", "b") VALUES (%s, %s)'
    assert rows == [[1, 2], [3, 4]]


def test_insert_rows_rejects_empty_and_ragged(rec: _Recorder) -> None:
    with pytest.raises(ids.DbToolError):
        rel.insert_rows("t", [])
    with pytest.raises(ids.DbToolError):
        rel.insert_rows("t", [{}])
    with pytest.raises(ids.DbToolError):
        rel.insert_rows("t", [{"a": 1}, {"b": 2}])


def test_update_rows_param_order(rec: _Recorder) -> None:
    out = rel.update_rows("t", {"a": "new"}, {"id": 7})
    assert out == {"ok": True, "updated": 1}
    sql, params = rec.executed[-1]
    assert sql == 'UPDATE "t" SET "a" = %s WHERE "id" = %s'
    assert params == ["new", 7]  # set values first, then where


def test_update_and_delete_full_table_guard(rec: _Recorder) -> None:
    with pytest.raises(ids.DbToolError):
        rel.update_rows("t", {"a": 1})  # no where, no allow_full
    with pytest.raises(ids.DbToolError):
        rel.delete_rows("t")
    assert rel.delete_rows("t", allow_full=True)["ok"] is True
    assert rel.update_rows("t", {"a": 1}, allow_full=True)["ok"] is True
    with pytest.raises(ids.DbToolError):
        rel.update_rows("t", {})  # nothing to set


def test_run_query_and_execute_sql(rec: _Recorder) -> None:
    rec.rows = [{"x": 1}]
    assert rel.run_query("SELECT 1") == {"rows": [{"x": 1}], "row_count": 1}
    with pytest.raises(ids.DbToolError):
        rel.run_query("DELETE FROM t")
    assert rel.execute_sql("DROP TABLE t")["ok"] is True


# ---------------------------------------------------------------------------
# Vector tools
# ---------------------------------------------------------------------------


def test_create_vector_table_runs_two_statements(rec: _Recorder) -> None:
    out = vec.create_vector_table("docs", 3, metric="cosine")
    assert out == {"ok": True, "table": "docs", "dim": 3, "metric": "cosine"}
    assert len(rec.scripts[-1]) == 2  # table + index


def test_upsert_vectors(rec: _Recorder) -> None:
    out = vec.upsert_vectors("docs", [{"id": "x", "embedding": [1, 2, 3], "metadata": {"k": 1}}])
    assert out == {"ok": True, "upserted": 1}
    _, rows = rec.many[-1]
    assert rows[0][0] == "x"
    assert rows[0][1] == "[1.0,2.0,3.0]"
    with pytest.raises(ids.DbToolError):
        vec.upsert_vectors("docs", [])
    with pytest.raises(ids.DbToolError):
        vec.upsert_vectors("docs", [{"id": "x"}])  # missing embedding


def test_similarity_search(rec: _Recorder) -> None:
    rec.rows = [{"id": "x", "distance": 0.1, "metadata": {"k": 1}}]
    out = vec.similarity_search("docs", [1, 2, 3], k=1)
    assert out == {"matches": [{"id": "x", "distance": 0.1, "metadata": {"k": 1}}]}
    with pytest.raises(ids.DbToolError):
        vec.similarity_search("docs", [1, 2, 3], k=0)


# ---------------------------------------------------------------------------
# Graph tools
# ---------------------------------------------------------------------------


def test_graph_create_and_drop(rec: _Recorder) -> None:
    assert graph.create_graph("g")["graph"] == "g"
    assert "create_graph('g')" in rec.executed[-1][0]
    assert graph.drop_graph("g", cascade=False)["graph"] == "g"
    assert "drop_graph('g', false)" in rec.executed[-1][0]


def test_graph_vertex_edge_and_cypher(rec: _Recorder) -> None:
    rec.rows = [{"n": '{"id": 1, "label": "Person"}::vertex'}]
    out = graph.create_vertex("g", "Person", {"name": "Ada"})
    assert out["vertex"] == {"id": 1, "label": "Person"}
    body = rec.executed[-1][0]
    assert "CREATE (n:Person {name: 'Ada'})" in body

    rec.rows = [{"r": '{"id": 2, "label": "KNOWS"}::edge'}]
    edge = graph.create_edge("g", {"name": "Ada"}, {"name": "Bob"}, "KNOWS", {"since": 2020})
    assert edge["edge"] == {"id": 2, "label": "KNOWS"}
    with pytest.raises(ids.DbToolError):
        graph.create_edge("g", {}, {"name": "Bob"}, "KNOWS")

    rec.rows = [{"a": '"Ada"', "b": '"Bob"'}]
    res = graph.cypher_query(
        "g", "MATCH (a)-[:KNOWS]->(b) RETURN a.name, b.name", columns=["a", "b"]
    )
    assert res == {"rows": [{"a": "Ada", "b": "Bob"}], "row_count": 1}
    with pytest.raises(ids.DbToolError):
        graph.cypher_query("g", "RETURN $$x$$")  # body contains $$


# ---------------------------------------------------------------------------
# Server builder
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("mode", "expected"),
    [
        ("relational", "create_table"),
        ("vector", "create_vector_table"),
        ("graph", "create_graph"),
    ],
)
def test_build_server_registers_mode_tools(mode: str, expected: str) -> None:
    app = server_mod.build_server(mode)
    names = {tool.name for tool in asyncio.run(app.list_tools())}
    assert expected in names


def test_build_server_rejects_unknown_mode() -> None:
    with pytest.raises(ValueError, match="unknown mode"):
        server_mod.build_server("nosuch")


# ---------------------------------------------------------------------------
# Connection layer (fake psycopg — no real Postgres)
# ---------------------------------------------------------------------------


class _FakeCursor:
    def __init__(self, rows: list[dict[str, Any]], rowcount: int) -> None:
        self._rows = rows
        self.rowcount = rowcount
        self.executed: list[tuple[str, Any]] = []
        self.many: list[tuple[str, list[Any]]] = []

    def __enter__(self) -> _FakeCursor:
        return self

    def __exit__(self, *_: object) -> None:
        return None

    def execute(self, sql: str, params: Any = None) -> None:
        self.executed.append((sql, params))

    def executemany(self, sql: str, seq: Any) -> None:
        self.many.append((sql, list(seq)))

    def fetchall(self) -> list[dict[str, Any]]:
        return self._rows


class _FakeConn:
    def __init__(self, cur: _FakeCursor) -> None:
        self._cur = cur
        self.closed = False
        #: Mirrors psycopg's connection attribute. ``None`` means "never set",
        #: which is the failure the read-only tests guard against.
        self.read_only: bool | None = None
        self.rolled_back = 0
        self.committed = 0

    def cursor(self) -> _FakeCursor:
        return self._cur

    def transaction(self) -> Any:
        # execute_many / execute_script now wrap their work in conn.transaction();
        # a no-op context manager is enough for the cursor-driving assertions.
        return contextlib.nullcontext()

    def rollback(self) -> None:
        self.rolled_back += 1

    def commit(self) -> None:
        self.committed += 1

    def close(self) -> None:
        self.closed = True


def _patch_psycopg(monkeypatch: pytest.MonkeyPatch, cur: _FakeCursor) -> _FakeConn:
    fake_conn = _FakeConn(cur)
    calls: list[tuple[tuple[object, ...], dict[str, object]]] = []

    class _FakePsycopg:
        @staticmethod
        def connect(*args: object, **kwargs: object) -> _FakeConn:
            calls.append((args, dict(kwargs)))
            return fake_conn

    monkeypatch.setattr(conn, "_load_psycopg", lambda: (_FakePsycopg, object()))
    monkeypatch.setenv("POSTGRES_URL", "postgresql://x:y@localhost/z")
    fake_conn.connect_calls = calls  # type: ignore[attr-defined]
    return fake_conn


def test_dsn_requires_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("POSTGRES_URL", raising=False)
    with pytest.raises(ids.DbToolError):
        conn._dsn()
    monkeypatch.setenv("POSTGRES_URL", "  postgresql://x  ")
    assert conn._dsn() == "postgresql://x"


def test_connection_helpers_drive_cursor(monkeypatch: pytest.MonkeyPatch) -> None:
    cur = _FakeCursor(rows=[{"a": 1}], rowcount=3)
    fake_conn = _patch_psycopg(monkeypatch, cur)

    assert conn.query("SELECT 1", [1]) == [{"a": 1}]
    assert conn.execute("UPDATE t SET a=1") == 3
    assert conn.execute_many("INSERT INTO t VALUES (%s)", [[1], [2]]) == 2
    conn.execute_script(["CREATE TABLE t (a int)", "CREATE INDEX i ON t (a)"])
    assert ("CREATE INDEX i ON t (a)", None) in cur.executed
    assert fake_conn.closed is True


def test_execute_many_is_atomic_and_rolls_back_on_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Regression (#14): execute_many must run inside a single transaction so a
    mid-batch failure rolls the whole op back, instead of leaving a committed
    prefix under plain autocommit."""
    events: list[str] = []

    class _Txn:
        def __enter__(self) -> _Txn:
            events.append("begin")
            return self

        def __exit__(self, exc_type: Any, *_: Any) -> bool:
            events.append("rollback" if exc_type else "commit")
            return False

    class _Cur:
        def __enter__(self) -> _Cur:
            return self

        def __exit__(self, *_: Any) -> bool:
            return False

        def executemany(self, _sql: str, _rows: Any) -> None:
            raise RuntimeError("boom on second row")

    class _Conn:
        def transaction(self) -> _Txn:
            return _Txn()

        def cursor(self) -> _Cur:
            return _Cur()

    class _ConnectCM:
        def __enter__(self) -> _Conn:
            return _Conn()

        def __exit__(self, *_: Any) -> bool:
            return False

    monkeypatch.setattr(conn, "connect", lambda: _ConnectCM())
    with pytest.raises(RuntimeError):
        conn.execute_many("INSERT INTO t (x) VALUES (%s)", [[1], [2]])
    assert events == ["begin", "rollback"]  # whole op rolled back, not committed


# ---------------------------------------------------------------------------
# Read-only enforcement (C11)
# ---------------------------------------------------------------------------


def test_read_only_query_uses_a_database_enforced_read_only_transaction(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Regression (C11): ``run_query`` is classified read / no-approval, so the
    guarantee must come from the engine, not from parsing the SQL.

    The connection must (a) not be autocommit — a read-only attribute needs a
    surrounding transaction to apply to, (b) be marked ``read_only`` *before* the
    statement runs, (c) carry a statement timeout, and (d) end in a rollback so
    nothing a read tool touched can be committed.
    """
    cur = _FakeCursor(rows=[{"a": 1}], rowcount=0)
    fake_conn = _patch_psycopg(monkeypatch, cur)
    monkeypatch.delenv("POSTGRES_READ_ONLY_URL", raising=False)
    monkeypatch.delenv("POSTGRES_READ_ONLY_STATEMENT_TIMEOUT_MS", raising=False)

    assert conn.read_only_query("SELECT 1", [1]) == [{"a": 1}]

    args, kwargs = fake_conn.connect_calls[-1]  # type: ignore[attr-defined]
    assert kwargs["autocommit"] is False
    assert fake_conn.read_only is True
    assert cur.executed[0] == (f"SET LOCAL statement_timeout = {30_000}", None)
    assert cur.executed[-1] == ("SELECT 1", [1])
    assert fake_conn.rolled_back == 1
    assert fake_conn.committed == 0
    assert fake_conn.closed is True
    # Falls back to POSTGRES_URL when no dedicated read-only role is configured.
    assert args[0] == "postgresql://x:y@localhost/z"


def test_read_only_query_prefers_a_least_privilege_role_when_configured(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``POSTGRES_READ_ONLY_URL`` lets an operator add a GRANT-level boundary on
    top of the read-only transaction. Write helpers must keep the primary DSN."""
    cur = _FakeCursor(rows=[], rowcount=0)
    fake_conn = _patch_psycopg(monkeypatch, cur)
    monkeypatch.setenv("POSTGRES_READ_ONLY_URL", "postgresql://ro:ro@localhost/z")

    conn.read_only_query("SELECT 1")
    assert fake_conn.connect_calls[-1][0][0] == "postgresql://ro:ro@localhost/z"  # type: ignore[attr-defined]

    conn.execute("UPDATE t SET a = 1")
    assert fake_conn.connect_calls[-1][0][0] == "postgresql://x:y@localhost/z"  # type: ignore[attr-defined]


def test_read_only_statement_timeout_is_configurable_and_can_be_disabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cur = _FakeCursor(rows=[], rowcount=0)
    _patch_psycopg(monkeypatch, cur)

    monkeypatch.setenv("POSTGRES_READ_ONLY_STATEMENT_TIMEOUT_MS", "1500")
    conn.read_only_query("SELECT 1")
    assert cur.executed[0] == ("SET LOCAL statement_timeout = 1500", None)

    cur.executed.clear()
    monkeypatch.setenv("POSTGRES_READ_ONLY_STATEMENT_TIMEOUT_MS", "0")
    conn.read_only_query("SELECT 1")
    assert cur.executed == [("SELECT 1", [])]

    monkeypatch.setenv("POSTGRES_READ_ONLY_STATEMENT_TIMEOUT_MS", "not-a-number")
    with pytest.raises(ids.DbToolError):
        conn.read_only_query("SELECT 1")


def test_read_only_transaction_rolls_back_even_when_the_query_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A driver error (e.g. Postgres refusing a write in a read-only
    transaction) must still leave the connection rolled back and closed."""

    class _Boom(_FakeCursor):
        def execute(self, sql: str, params: Any = None) -> None:
            super().execute(sql, params)
            if not sql.startswith("SET LOCAL"):
                raise RuntimeError("cannot execute DELETE in a read-only transaction")

    cur = _Boom(rows=[], rowcount=0)
    fake_conn = _patch_psycopg(monkeypatch, cur)
    with pytest.raises(RuntimeError, match="read-only transaction"):
        conn.read_only_query("SELECT drop_graph('g', true)")
    assert fake_conn.rolled_back == 1
    assert fake_conn.closed is True


@pytest.mark.parametrize(
    "tool",
    [
        lambda: rel.run_query("SELECT 1"),
        lambda: rel.list_tables(),
        lambda: rel.describe_table("t"),
    ],
)
def test_read_classified_relational_tools_never_use_the_read_write_path(
    rec: _Recorder, tool: Any
) -> None:
    """Regression (C11): every tool the UI presets mark read / no-approval must
    route through ``read_only_query``. Using ``query`` would put it back on the
    privileged autocommit connection that accepted mutation."""
    rec.rows = [
        {
            "table_name": "t",
            "column_name": "id",
            "data_type": "integer",
            "is_nullable": "NO",
            "column_default": None,
        }
    ]
    tool()
    assert len(rec.read_only) == 1
    assert rec.read_only[0][0] == rec.executed[-1][0]


def test_similarity_search_is_read_only(rec: _Recorder) -> None:
    rec.rows = [{"id": "a", "distance": 0.5, "metadata": {}}]
    vec.similarity_search("t", [0.1, 0.2], k=1)
    assert len(rec.read_only) == 1


def test_write_tools_keep_the_read_write_connection(rec: _Recorder) -> None:
    """The read-only path must not be applied to tools that legitimately write —
    that would break them instead of containing them."""
    rel.delete_rows("t", {"id": 1})
    rel.update_rows("t", {"a": 2}, {"id": 1})
    graph.drop_graph("g")
    assert rec.read_only == []
