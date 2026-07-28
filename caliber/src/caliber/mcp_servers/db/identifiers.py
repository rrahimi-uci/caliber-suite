"""Pure SQL / openCypher builders and input validation — no database access.

This module is deliberately free of any ``psycopg`` import so it can be unit
tested without a driver or a live Postgres, and so it carries the bulk of the
package's test coverage. Every table / column / label / graph name a caller
supplies is validated against :data:`IDENT_RE` and then quoted, so the SQL
strings these helpers build are injection-safe even though they use f-strings;
*values* are always bound as parameters by the tool layer, never interpolated.
"""

from __future__ import annotations

import json
import re
from typing import Any

#: Postgres/AGE identifiers we accept: a letter or underscore followed by
#: letters, digits, or underscores. The same pattern the workflow manifest uses
#: for node/edge ids. Anything outside this set is rejected before it can reach
#: a query, which is what makes the f-string composition below safe.
IDENT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")

#: Column types accepted verbatim in ``create_table``. Free-form types are
#: rejected so a caller can't smuggle SQL through the type position.
_SIMPLE_TYPES = frozenset(
    {
        "int",
        "integer",
        "bigint",
        "smallint",
        "serial",
        "bigserial",
        "text",
        "boolean",
        "bool",
        "real",
        "double precision",
        "timestamptz",
        "timestamp",
        "date",
        "jsonb",
        "json",
        "uuid",
    }
)

#: Parametric types: ``varchar(n)`` / ``char(n)`` / ``numeric(p[,s])`` /
#: ``vector(n)``. Validated by regex and rebuilt from the parsed integers.
_PARAM_TYPE_RE = re.compile(r"^(varchar|char|numeric|vector)\s*\(\s*(\d+)(?:\s*,\s*(\d+))?\s*\)$")

#: Allowed primary-key / id column types for vector tables.
_ID_TYPES = frozenset({"text", "uuid", "int", "integer", "bigint"})

#: Distance metric -> pgvector operator.
_METRIC_OPERATOR = {"l2": "<->", "cosine": "<=>", "ip": "<#>"}
#: Distance metric -> pgvector index opclass.
_METRIC_OPCLASS = {
    "l2": "vector_l2_ops",
    "cosine": "vector_cosine_ops",
    "ip": "vector_ip_ops",
}

#: Leading keywords ``run_query`` treats as read-only.
_READ_KEYWORDS = frozenset({"select", "with", "table", "values", "show", "explain"})


class DbToolError(ValueError):
    """Invalid tool input — a bad identifier, type, metric, or shape.

    The tool layer lets this propagate; FastMCP turns it into an MCP error
    result, which the CALIBER gateway surfaces as the tool's error message.
    """


# ---------------------------------------------------------------------------
# Identifier / type validation
# ---------------------------------------------------------------------------


def validate_identifier(name: Any, *, kind: str = "identifier") -> str:
    """Return ``name`` if it is a safe SQL/Cypher identifier, else raise."""
    if not isinstance(name, str) or not IDENT_RE.match(name):
        raise DbToolError(f"invalid {kind} {name!r}: must match {IDENT_RE.pattern}")
    return name


def quote_ident(name: Any, *, kind: str = "identifier") -> str:
    """Validate ``name`` and wrap it in double quotes for safe SQL embedding."""
    return '"' + validate_identifier(name, kind=kind) + '"'


def validate_column_type(raw: Any) -> str:
    """Return a canonical, whitelisted column type, or raise ``DbToolError``."""
    if not isinstance(raw, str) or not raw.strip():
        raise DbToolError("column type must be a non-empty string")
    text = raw.strip().lower()
    if text in _SIMPLE_TYPES:
        return text
    match = _PARAM_TYPE_RE.match(text)
    if match is not None:
        base, first, second = match.group(1), match.group(2), match.group(3)
        if second is not None:
            if base != "numeric":
                raise DbToolError(f"type {base!r} takes a single size argument")
            return f"numeric({first},{second})"
        return f"{base}({first})"
    raise DbToolError(
        f"unsupported column type {raw!r}; allowed: simple types "
        f"{sorted(_SIMPLE_TYPES)} or varchar(n)/char(n)/numeric(p,s)/vector(n)"
    )


# ---------------------------------------------------------------------------
# Relational SQL builders (return SQL text; values bound by the tool layer)
# ---------------------------------------------------------------------------


def compose_create_table(
    table: str, columns: list[dict[str, Any]], *, if_not_exists: bool = True
) -> str:
    """Build a ``CREATE TABLE`` statement from a validated column spec."""
    if not columns:
        raise DbToolError("create_table requires at least one column")
    rendered: list[str] = []
    for col in columns:
        if not isinstance(col, dict):
            raise DbToolError("each column must be an object with 'name' and 'type'")
        col_sql = (
            f"{quote_ident(col.get('name'), kind='column')} {validate_column_type(col.get('type'))}"
        )
        if col.get("primary_key"):
            col_sql += " PRIMARY KEY"
        if col.get("not_null"):
            col_sql += " NOT NULL"
        rendered.append(col_sql)
    ine = "IF NOT EXISTS " if if_not_exists else ""
    return f"CREATE TABLE {ine}{quote_ident(table, kind='table')} ({', '.join(rendered)})"


def compose_insert(table: str, columns: list[str]) -> str:
    """Build a parameterized ``INSERT`` for the given validated columns."""
    if not columns:
        raise DbToolError("insert requires at least one column")
    cols = ", ".join(quote_ident(c, kind="column") for c in columns)
    placeholders = ", ".join(["%s"] * len(columns))
    return (
        f"INSERT INTO {quote_ident(table, kind='table')} "  # noqa: S608 (idents validated)
        f"({cols}) VALUES ({placeholders})"
    )


def compose_update(table: str, set_columns: list[str], where_columns: list[str]) -> str:
    """Build a parameterized ``UPDATE``. Values bind set-first, then where."""
    if not set_columns:
        raise DbToolError("update requires at least one column to set")
    assignments = ", ".join(f"{quote_ident(c, kind='column')} = %s" for c in set_columns)
    sql = f"UPDATE {quote_ident(table, kind='table')} SET {assignments}"  # noqa: S608
    if where_columns:
        clause = " AND ".join(f"{quote_ident(c, kind='column')} = %s" for c in where_columns)
        sql += f" WHERE {clause}"
    return sql


def compose_delete(table: str, where_columns: list[str]) -> str:
    """Build a parameterized ``DELETE`` with an optional WHERE clause."""
    sql = f"DELETE FROM {quote_ident(table, kind='table')}"  # noqa: S608 (idents validated)
    if where_columns:
        clause = " AND ".join(f"{quote_ident(c, kind='column')} = %s" for c in where_columns)
        sql += f" WHERE {clause}"
    return sql


def require_where(where: dict[str, Any] | None, allow_full: bool) -> None:
    """Guard against accidental full-table updates/deletes."""
    if not where and not allow_full:
        raise DbToolError(
            "refusing to run without a WHERE filter; pass allow_full=true to override"
        )


#: Data-modifying statements Postgres permits inside a WITH (CTE) body. A
#: leading-keyword check misses these because the statement begins with WITH.
_CTE_WRITE_RE = re.compile(r"\(\s*(?:insert|update|delete|merge)\b", re.IGNORECASE)

#: ``ANALYZE`` inside an ``EXPLAIN`` option list, either spelling.
_ANALYZE_WORD_RE = re.compile(r"\banaly[sz]e\b", re.IGNORECASE)

#: Bare keywords Postgres accepts between ``EXPLAIN`` and the statement in the
#: legacy (non-parenthesised) form: ``EXPLAIN [ANALYZE] [VERBOSE] <stmt>``.
_EXPLAIN_BARE_OPTIONS = frozenset({"analyze", "analyse", "verbose"})


def _split_explain(skeleton: str) -> tuple[str, bool]:
    """Split an ``EXPLAIN`` skeleton into ``(explained statement, analyze?)``.

    ``EXPLAIN ANALYZE`` *executes* the plan it explains, so
    ``EXPLAIN ANALYZE DELETE FROM t`` deletes rows while reading as ``explain``
    to a leading-keyword check. Both accepted syntaxes are handled: the modern
    ``EXPLAIN (ANALYZE, VERBOSE) <stmt>`` option list and the legacy bare-keyword
    ``EXPLAIN ANALYZE VERBOSE <stmt>``.
    """
    rest = skeleton.lstrip()
    rest = rest[len("explain") :].lstrip()
    analyze = False
    if rest.startswith("("):
        close = rest.find(")")
        options = rest[1:] if close == -1 else rest[1:close]
        analyze = bool(_ANALYZE_WORD_RE.search(options))
        rest = "" if close == -1 else rest[close + 1 :].lstrip()
        return rest, analyze
    while rest:
        token = rest.split(None, 1)[0]
        if token.lower() not in _EXPLAIN_BARE_OPTIONS:
            break
        analyze = analyze or token.lower() in ("analyze", "analyse")
        rest = rest[len(token) :].lstrip()
    return rest, analyze


def _sql_skeleton(sql: str) -> str:
    """Blank out comments and string / quoted-identifier / dollar-quoted spans so
    statement-splitting and keyword scanning can't be fooled by a ``;`` or a
    write keyword that lives inside a literal. Removed spans become a space to
    preserve token boundaries."""
    out: list[str] = []
    i, n = 0, len(sql)
    while i < n:
        two = sql[i : i + 2]
        ch = sql[i]
        if two == "--":  # line comment
            j = sql.find("\n", i)
            i = n if j == -1 else j
            out.append(" ")
        elif two == "/*":  # block comment
            j = sql.find("*/", i + 2)
            i = n if j == -1 else j + 2
            out.append(" ")
        elif ch in ("'", '"'):  # string literal / quoted identifier (doubled = escape)
            quote = ch
            i += 1
            while i < n:
                if sql[i] == quote:
                    if i + 1 < n and sql[i + 1] == quote:
                        i += 2
                        continue
                    i += 1
                    break
                i += 1
            out.append(" ")
        elif ch == "$":  # dollar-quoted string: $tag$ ... $tag$
            m = re.match(r"\$[A-Za-z_0-9]*\$", sql[i:])
            if m:
                tag = m.group(0)
                j = sql.find(tag, i + len(tag))
                i = n if j == -1 else j + len(tag)
                out.append(" ")
            else:
                out.append(ch)
                i += 1
        else:
            out.append(ch)
            i += 1
    return "".join(out)


def assert_read_only(sql: Any) -> None:
    """Reject anything that isn't a single, genuinely read-only statement.

    This is **defense in depth, not the boundary**. The boundary is
    :func:`caliber.mcp_servers.db.connection.read_only_query`, which runs the
    statement in a ``READ ONLY`` transaction so the engine refuses a write even
    when it is reached through a function call this parser cannot see (e.g.
    ``SELECT drop_graph('g', true)``). This function exists to return a clean,
    actionable tool error for the cases it *can* recognise:

    1. **Stacked statements** — ``SELECT 1; DELETE FROM t`` has head ``select``
       but psycopg's simple-query protocol runs BOTH statements.
    2. **Data-modifying CTEs** — ``WITH x AS (DELETE FROM t RETURNING *) SELECT
       * FROM x`` has head ``with`` yet performs the DELETE.
    3. **``EXPLAIN ANALYZE``** — executes the plan it explains, so
       ``EXPLAIN ANALYZE DELETE FROM t`` deletes rows under an ``explain`` head.
    4. **``EXPLAIN <write>``** — plain EXPLAIN does not execute, but a
       read-classified tool has no reason to plan a mutation.
    """
    if not isinstance(sql, str) or not sql.strip():
        raise DbToolError("sql must be a non-empty string")
    skeleton = _sql_skeleton(sql)
    head = (skeleton.lstrip().split(None, 1) or [""])[0].lower()
    if head not in _READ_KEYWORDS:
        raise DbToolError(
            "run_query only allows read queries (SELECT/WITH/TABLE/VALUES/SHOW/EXPLAIN); "
            "use execute_sql for writes"
        )
    if len([s for s in skeleton.split(";") if s.strip()]) > 1:
        raise DbToolError(
            "run_query allows a single statement only; stacked statements (';') "
            "are not permitted — use execute_sql for writes"
        )
    if _CTE_WRITE_RE.search(skeleton):
        raise DbToolError(
            "run_query is read-only; a data-modifying statement "
            "(INSERT/UPDATE/DELETE/MERGE) inside a CTE is not permitted — "
            "use execute_sql for writes"
        )
    if head == "explain":
        inner, analyze = _split_explain(skeleton)
        if analyze:
            raise DbToolError(
                "run_query does not allow EXPLAIN ANALYZE: it executes the "
                "statement it explains — use execute_sql for writes"
            )
        inner_head = (inner.split(None, 1) or [""])[0].lower()
        if inner_head == "explain" or inner_head not in _READ_KEYWORDS:
            raise DbToolError(
                "run_query only explains read queries "
                "(SELECT/WITH/TABLE/VALUES/SHOW); use execute_sql for writes"
            )


# ---------------------------------------------------------------------------
# pgvector builders
# ---------------------------------------------------------------------------


def metric_to_operator(metric: Any) -> str:
    """Return the pgvector distance operator for a metric name."""
    key = metric.strip().lower() if isinstance(metric, str) else metric
    if key not in _METRIC_OPERATOR:
        raise DbToolError(f"unknown metric {metric!r}; one of {sorted(_METRIC_OPERATOR)}")
    return _METRIC_OPERATOR[key]


def metric_to_opclass(metric: Any) -> str:
    """Return the pgvector index opclass for a metric name."""
    key = metric.strip().lower() if isinstance(metric, str) else metric
    if key not in _METRIC_OPCLASS:
        raise DbToolError(f"unknown metric {metric!r}; one of {sorted(_METRIC_OPCLASS)}")
    return _METRIC_OPCLASS[key]


def embedding_to_literal(embedding: Any) -> str:
    """Render a numeric vector as a pgvector text literal ``[1.0,2.0,...]``."""
    if not isinstance(embedding, (list, tuple)) or not embedding:
        raise DbToolError("embedding must be a non-empty list of numbers")
    parts: list[str] = []
    for value in embedding:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise DbToolError("embedding values must be numbers")
        parts.append(repr(float(value)))
    return "[" + ",".join(parts) + "]"


def compose_create_vector_table(
    table: str, dim: Any, *, metric: str = "cosine", id_type: str = "text"
) -> tuple[str, str]:
    """Return ``(create_table_sql, create_index_sql)`` for a pgvector table."""
    if not isinstance(dim, int) or isinstance(dim, bool) or dim <= 0:
        raise DbToolError("dim must be a positive integer")
    if not isinstance(id_type, str) or id_type.strip().lower() not in _ID_TYPES:
        raise DbToolError(f"id_type must be one of {sorted(_ID_TYPES)}")
    opclass = metric_to_opclass(metric)
    table_sql = quote_ident(table, kind="table")
    index_sql = quote_ident(f"{table}_embedding_idx", kind="index")
    ddl = (
        f"CREATE TABLE IF NOT EXISTS {table_sql} "
        f"(id {id_type.strip().lower()} PRIMARY KEY, embedding vector({dim}), metadata jsonb)"
    )
    index = (
        f"CREATE INDEX IF NOT EXISTS {index_sql} ON {table_sql} USING hnsw (embedding {opclass})"
    )
    return ddl, index


def compose_upsert_vector(table: str) -> str:
    """Build the parameterized upsert for a pgvector table."""
    table_sql = quote_ident(table, kind="table")
    return (
        f"INSERT INTO {table_sql} (id, embedding, metadata) "  # noqa: S608 (idents validated)
        "VALUES (%s, %s::vector, %s::jsonb) "
        "ON CONFLICT (id) DO UPDATE SET "
        "embedding = EXCLUDED.embedding, metadata = EXCLUDED.metadata"
    )


def compose_similarity_search(table: str, metric: str) -> str:
    """Build the parameterized nearest-neighbour query for a pgvector table."""
    operator = metric_to_operator(metric)
    table_sql = quote_ident(table, kind="table")
    return (
        f"SELECT id, embedding {operator} %s::vector AS distance, metadata "  # noqa: S608
        f"FROM {table_sql} ORDER BY distance LIMIT %s"
    )


# ---------------------------------------------------------------------------
# Apache AGE / openCypher builders
# ---------------------------------------------------------------------------


def cypher_string_literal(value: str) -> str:
    """Escape a string for safe inlining inside a Cypher single-quoted literal."""
    return "'" + value.replace("\\", "\\\\").replace("'", "\\'") + "'"


def cypher_value(value: Any) -> str:
    """Render a property value as a Cypher literal."""
    if isinstance(value, bool):
        return "true" if value else "false"
    if value is None:
        return "null"
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, str):
        return cypher_string_literal(value)
    if isinstance(value, (list, tuple)):
        return "[" + ", ".join(cypher_value(item) for item in value) + "]"
    raise DbToolError(
        f"unsupported property value type {type(value).__name__}; "
        "use str/int/float/bool/null or lists of those values"
    )


def cypher_map(props: dict[str, Any] | None) -> str:
    """Render a property dict as a Cypher map literal, validating each key.

    AGE cannot bind parameters into ``cypher(...)`` (the query string must be a
    literal), so properties are serialized here with escaped values rather than
    passed as bind parameters. Keys are validated as identifiers.
    """
    if props is None:
        return "{}"
    if not isinstance(props, dict):
        raise DbToolError("properties must be an object")
    parts = [
        f"{validate_identifier(key, kind='property')}: {cypher_value(value)}"
        for key, value in props.items()
    ]
    return "{" + ", ".join(parts) + "}" if parts else "{}"


def compose_cypher_columns(columns: list[str] | None) -> str:
    """Render the ``AS (...)`` column list for a ``cypher(...)`` call."""
    if not columns:
        return "result agtype"
    return ", ".join(f"{validate_identifier(col, kind='column')} agtype" for col in columns)


def assert_cypher_body_safe(body: Any) -> str:
    """Ensure a raw Cypher body can be dollar-quoted without breaking out."""
    if not isinstance(body, str) or not body.strip():
        raise DbToolError("cypher must be a non-empty string")
    if "$$" in body:
        raise DbToolError("cypher body may not contain '$$'")
    return body


def parse_agtype(value: Any) -> Any:
    """Best-effort decode of an AGE ``agtype`` text result into JSON.

    AGE returns vertices/edges as ``{...}::vertex`` / ``{...}::edge`` text and
    scalars as JSON-ish text. Strip a trailing type tag and ``json.loads``; fall
    back to the raw value if it isn't JSON.
    """
    if not isinstance(value, str):
        return value
    candidate = value
    for suffix in ("::vertex", "::edge", "::path"):
        if candidate.endswith(suffix):
            candidate = candidate[: -len(suffix)]
            break
    try:
        return json.loads(candidate)
    except (ValueError, TypeError):
        return value
