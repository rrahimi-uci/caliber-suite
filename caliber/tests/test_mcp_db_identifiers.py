"""Unit tests for the pure SQL/Cypher builders and validators.

No database or psycopg required — this is where the package's safety logic
lives and is exhaustively exercised.
"""

from __future__ import annotations

import pytest

from caliber.mcp_servers.db import identifiers as ids

# ---------------------------------------------------------------------------
# Identifier validation
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("name", ["t", "_t", "Table1", "a_b_c", "col_123"])
def test_validate_identifier_accepts_safe_names(name: str) -> None:
    assert ids.validate_identifier(name) == name


@pytest.mark.parametrize(
    "name",
    ["1col", "a-b", "a b", "", "drop table", 'a"b', "a;b", "tbl); DROP TABLE x;--", 123, None],
)
def test_validate_identifier_rejects_unsafe(name: object) -> None:
    with pytest.raises(ids.DbToolError):
        ids.validate_identifier(name)  # type: ignore[arg-type]


def test_quote_ident_wraps_and_validates() -> None:
    assert ids.quote_ident("users") == '"users"'
    with pytest.raises(ids.DbToolError):
        ids.quote_ident("a-b")


# ---------------------------------------------------------------------------
# Column types
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("text", "text"),
        ("INT", "int"),
        ("Double Precision", "double precision"),
        ("varchar(255)", "varchar(255)"),
        ("numeric(10,2)", "numeric(10,2)"),
        ("vector(1536)", "vector(1536)"),
        (" jsonb ", "jsonb"),
    ],
)
def test_validate_column_type_accepts(raw: str, expected: str) -> None:
    assert ids.validate_column_type(raw) == expected


@pytest.mark.parametrize(
    "raw",
    ["text; drop table x", "varchar(10,2)", "money", "", 5, "int[]"],
)
def test_validate_column_type_rejects(raw: object) -> None:
    with pytest.raises(ids.DbToolError):
        ids.validate_column_type(raw)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Relational SQL builders
# ---------------------------------------------------------------------------


def test_compose_create_table() -> None:
    sql = ids.compose_create_table(
        "people",
        [
            {"name": "id", "type": "serial", "primary_key": True},
            {"name": "email", "type": "varchar(255)", "not_null": True},
        ],
    )
    assert sql == (
        'CREATE TABLE IF NOT EXISTS "people" '
        '("id" serial PRIMARY KEY, "email" varchar(255) NOT NULL)'
    )


def test_compose_create_table_without_if_not_exists() -> None:
    sql = ids.compose_create_table("t", [{"name": "a", "type": "int"}], if_not_exists=False)
    assert sql.startswith('CREATE TABLE "t"')


@pytest.mark.parametrize(
    "columns",
    [[], [{"name": "id"}], [{"type": "int"}], ["notadict"], [{"name": "a-b", "type": "int"}]],
)
def test_compose_create_table_rejects_bad_columns(columns: list[object]) -> None:
    with pytest.raises(ids.DbToolError):
        ids.compose_create_table("t", columns)  # type: ignore[arg-type]


def test_compose_insert() -> None:
    assert ids.compose_insert("t", ["a", "b"]) == 'INSERT INTO "t" ("a", "b") VALUES (%s, %s)'
    with pytest.raises(ids.DbToolError):
        ids.compose_insert("t", [])


def test_compose_update() -> None:
    assert ids.compose_update("t", ["a"], ["id"]) == 'UPDATE "t" SET "a" = %s WHERE "id" = %s'
    assert ids.compose_update("t", ["a"], []) == 'UPDATE "t" SET "a" = %s'
    with pytest.raises(ids.DbToolError):
        ids.compose_update("t", [], ["id"])


def test_compose_delete() -> None:
    assert ids.compose_delete("t", ["id"]) == 'DELETE FROM "t" WHERE "id" = %s'
    assert ids.compose_delete("t", []) == 'DELETE FROM "t"'


def test_require_where_guard() -> None:
    ids.require_where({"id": 1}, allow_full=False)
    ids.require_where(None, allow_full=True)
    with pytest.raises(ids.DbToolError):
        ids.require_where(None, allow_full=False)
    with pytest.raises(ids.DbToolError):
        ids.require_where({}, allow_full=False)


@pytest.mark.parametrize("sql", ["SELECT 1", "  with x as (select 1) select * from x", "TABLE t"])
def test_assert_read_only_accepts(sql: str) -> None:
    ids.assert_read_only(sql)


@pytest.mark.parametrize("sql", ["INSERT INTO t VALUES (1)", "delete from t", "", "  ", 1])
def test_assert_read_only_rejects(sql: object) -> None:
    with pytest.raises(ids.DbToolError):
        ids.assert_read_only(sql)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT 1; DELETE FROM users",  # stacked statements
        "select 1 ; delete from t",  # stacked, lowercase + spaces
        "WITH x AS (DELETE FROM users RETURNING *) SELECT * FROM x",  # data-modifying CTE
        "with x as ( update t set a=1 returning * ) select * from x",  # CTE update
        "WITH x AS (INSERT INTO t VALUES (1) RETURNING *) SELECT * FROM x",  # CTE insert
    ],
)
def test_assert_read_only_rejects_write_smuggling(sql: str) -> None:
    """Regression (#6): the guard must fail CLOSED for stacked statements and
    data-modifying CTEs, both of which previously passed the leading-keyword check."""
    with pytest.raises(ids.DbToolError):
        ids.assert_read_only(sql)


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT 1;",  # a single trailing semicolon is fine
        "SELECT * FROM t WHERE note = 'a; delete from x'",  # ';'/keyword inside a string literal
        'SELECT * FROM "delete_audit"',  # write word only inside a quoted identifier
        "WITH x AS (SELECT 1) SELECT * FROM x",  # benign CTE
    ],
)
def test_assert_read_only_accepts_safe_edge_cases(sql: str) -> None:
    ids.assert_read_only(sql)


# ---------------------------------------------------------------------------
# pgvector builders
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("metric", "operator", "opclass"),
    [
        ("cosine", "<=>", "vector_cosine_ops"),
        ("l2", "<->", "vector_l2_ops"),
        ("IP", "<#>", "vector_ip_ops"),
    ],
)
def test_metric_maps(metric: str, operator: str, opclass: str) -> None:
    assert ids.metric_to_operator(metric) == operator
    assert ids.metric_to_opclass(metric) == opclass


def test_metric_maps_reject_unknown() -> None:
    with pytest.raises(ids.DbToolError):
        ids.metric_to_operator("manhattan")
    with pytest.raises(ids.DbToolError):
        ids.metric_to_opclass("manhattan")


def test_embedding_to_literal() -> None:
    assert ids.embedding_to_literal([1, 2.5, 3]) == "[1.0,2.5,3.0]"


@pytest.mark.parametrize("bad", [[], [1, "x"], [True, 2], "notalist", [None]])
def test_embedding_to_literal_rejects(bad: object) -> None:
    with pytest.raises(ids.DbToolError):
        ids.embedding_to_literal(bad)


def test_compose_create_vector_table() -> None:
    ddl, index = ids.compose_create_vector_table("docs", 3, metric="cosine")
    assert 'CREATE TABLE IF NOT EXISTS "docs"' in ddl
    assert "embedding vector(3)" in ddl
    assert "vector_cosine_ops" in index
    assert '"docs_embedding_idx"' in index


@pytest.mark.parametrize(
    "kwargs",
    [
        {"dim": 0},
        {"dim": -1},
        {"dim": True},
        {"dim": 3, "id_type": "money"},
        {"dim": 3, "metric": "bogus"},
    ],
)
def test_compose_create_vector_table_rejects(kwargs: dict[str, object]) -> None:
    with pytest.raises(ids.DbToolError):
        ids.compose_create_vector_table("t", **kwargs)  # type: ignore[arg-type]


def test_compose_upsert_and_search_contain_casts_and_operator() -> None:
    assert "%s::vector" in ids.compose_upsert_vector("t")
    assert "%s::jsonb" in ids.compose_upsert_vector("t")
    assert "<=>" in ids.compose_similarity_search("t", "cosine")
    assert "ORDER BY distance LIMIT %s" in ids.compose_similarity_search("t", "l2")


# ---------------------------------------------------------------------------
# Cypher builders
# ---------------------------------------------------------------------------


def test_cypher_string_literal_escapes() -> None:
    assert ids.cypher_string_literal("O'Brien") == "'O\\'Brien'"
    assert ids.cypher_string_literal("a\\b") == "'a\\\\b'"


@pytest.mark.parametrize(
    ("value", "rendered"),
    [
        (True, "true"),
        (False, "false"),
        (None, "null"),
        (42, "42"),
        (1.5, "1.5"),
        (["Ada", "Bob"], "['Ada', 'Bob']"),
        ((1, 2, 3), "[1, 2, 3]"),
    ],
)
def test_cypher_value_scalars(value: object, rendered: str) -> None:
    assert ids.cypher_value(value) == rendered


def test_cypher_value_rejects_unsupported() -> None:
    with pytest.raises(ids.DbToolError):
        ids.cypher_value({"name": "Ada"})


def test_cypher_map() -> None:
    assert ids.cypher_map({"name": "Ada", "age": 36}) == "{name: 'Ada', age: 36}"
    assert ids.cypher_map({"aliases": ["Ada", "Lovelace"]}) == "{aliases: ['Ada', 'Lovelace']}"
    assert ids.cypher_map(None) == "{}"
    assert ids.cypher_map({}) == "{}"
    with pytest.raises(ids.DbToolError):
        ids.cypher_map({"bad-key": 1})
    with pytest.raises(ids.DbToolError):
        ids.cypher_map("notadict")  # type: ignore[arg-type]


def test_compose_cypher_columns() -> None:
    assert ids.compose_cypher_columns(None) == "result agtype"
    assert ids.compose_cypher_columns(["a", "b"]) == "a agtype, b agtype"
    with pytest.raises(ids.DbToolError):
        ids.compose_cypher_columns(["a-b"])


def test_assert_cypher_body_safe() -> None:
    assert ids.assert_cypher_body_safe("MATCH (n) RETURN n") == "MATCH (n) RETURN n"
    with pytest.raises(ids.DbToolError):
        ids.assert_cypher_body_safe("RETURN $$injected$$")
    with pytest.raises(ids.DbToolError):
        ids.assert_cypher_body_safe("")


def test_parse_agtype() -> None:
    assert ids.parse_agtype('{"id": 1, "label": "P"}::vertex') == {"id": 1, "label": "P"}
    assert ids.parse_agtype('{"id": 2}::edge') == {"id": 2}
    assert ids.parse_agtype('"Ada"') == "Ada"
    assert ids.parse_agtype("not json") == "not json"
    assert ids.parse_agtype(42) == 42
