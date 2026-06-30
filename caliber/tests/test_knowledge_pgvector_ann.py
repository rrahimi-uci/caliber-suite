"""Unit tests for pgvector SQL-side ANN (``caliber.knowledge.pgvector_ann``).

Tests run on SQLite, so the Postgres path is exercised with a fake session whose
``get_bind().dialect.name`` is ``"postgresql"`` and whose ``execute`` returns
canned rows — the same pattern ``test_knowledge_age`` uses for its Postgres-only
code. No real pgvector needed.
"""

from __future__ import annotations

from typing import Any

from caliber.knowledge import pgvector_ann


class _Dialect:
    def __init__(self, name: str) -> None:
        self.name = name


class _Bind:
    def __init__(self, name: str) -> None:
        self.dialect = _Dialect(name)


class _Result:
    def __init__(self, rows: list[tuple[Any, ...]]) -> None:
        self._rows = rows

    def all(self) -> list[tuple[Any, ...]]:
        return self._rows


class _FakeSession:
    def __init__(
        self, *, name: str = "postgresql", rows: list[tuple[Any, ...]] | None = None
    ) -> None:
        self._name = name
        self._rows = rows or []
        self.calls: list[tuple[str, Any]] = []

    def get_bind(self) -> _Bind:
        return _Bind(self._name)

    def execute(self, stmt: Any, params: Any = None) -> _Result:
        self.calls.append((str(stmt), params))
        return _Result(self._rows)


def test_is_postgres() -> None:
    assert pgvector_ann.is_postgres(_FakeSession(name="postgresql")) is True
    assert pgvector_ann.is_postgres(_FakeSession(name="sqlite")) is False


def test_to_vector_literal() -> None:
    assert pgvector_ann.to_vector_literal([0.1, 0.2, 0.3]) == "[0.1,0.2,0.3]"
    assert pgvector_ann.to_vector_literal([]) == "[]"


def test_dense_candidate_ids_builds_knn_query_and_parses_rows() -> None:
    session = _FakeSession(rows=[("chunk-a", 0.1), ("chunk-b", 0.35)])
    out = pgvector_ann.dense_candidate_ids(
        session, version_id="KBV-1", query_vector=[0.1, 0.2], pool_size=5
    )
    assert out == [("chunk-a", 0.1), ("chunk-b", 0.35)]
    sql, params = session.calls[0]
    # KNN distance operator + bounded top-k.
    assert "<=>" in sql
    assert "LIMIT :pool" in sql
    assert params == {"q": "[0.1,0.2]", "vid": "KBV-1", "pool": 5}


def test_populate_chunk_vectors_writes_matching_dim_and_skips_others() -> None:
    session = _FakeSession()
    pgvector_ann.populate_chunk_vectors(
        session,
        [("c1", [0.1, 0.2]), ("c2", []), ("c3", [0.3, 0.4]), ("c4", [0.1, 0.2, 0.3])],
        expected_dim=2,
    )
    assert len(session.calls) == 1
    _sql, payload = session.calls[0]
    # Empty (c2) and wrong-dimension (c4, dim 3 != 2) rows are dropped so a
    # vector(2) column never sees a mismatched insert.
    assert payload == [
        {"id": "c1", "v": "[0.1,0.2]"},
        {"id": "c3", "v": "[0.3,0.4]"},
    ]


def test_populate_chunk_vectors_noop_when_none_match() -> None:
    session = _FakeSession()
    pgvector_ann.populate_chunk_vectors(session, [("c1", []), ("c2", [0.1])], expected_dim=2)
    assert session.calls == []
