"""Coverage tests for :mod:`caliber.knowledge.age` (Apache AGE graph layer).

Apache AGE targets PostgreSQL + the AGE extension, which is unavailable in the
unit-test env (SQLite, in-memory). These tests therefore exercise everything
reachable *without* a live AGE server:

* module-level pure helpers (seed selection, miss-reason strings, query-token
  candidate extraction, preview truncation, list coercion);
* config / enablement guard clauses (``available`` / ``unavailable_reason`` and
  the disabled / non-PostgreSQL early-returns in ``sync_version`` / ``retrieve``
  / ``explore`` / ``drop_version``);
* the Cypher / query-string builders and result-row mapping, driven against a
  small fake ``Connection`` (records ``exec_driver_sql`` bodies, returns canned
  ``.mappings().all()`` rows) or by stubbing ``_query_cypher`` to capture the
  generated Cypher body and hand back canned rows;
* the retrieval / exploration control flow (seed misses, empty chunk lineage,
  frontier expansion breaks) via a fake engine + stubbed sub-queries.

Anything that genuinely needs a real AGE/Postgres round-trip (the *execution*
of Cypher, agtype decoding of real vertices) is out of scope and skipped.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy.orm import sessionmaker

from caliber.knowledge.age import (
    AgeDocumentNode,
    ApacheAgeKnowledgeStore,
    _as_list,
    _cypher_list,
    _merge_entity_match_rows,
    _preview_text,
    _retrieval_query_candidates,
    _retrieval_seed_miss_reason,
    _select_age_seed_rows,
    postgres_age_required_reason,
)

# ---------------------------------------------------------------------------
# Fake connection / engine plumbing (no live AGE)
# ---------------------------------------------------------------------------


class _Mappings:
    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self._rows = list(rows)

    def all(self) -> list[dict[str, Any]]:
        return list(self._rows)

    def __iter__(self):  # _ensure_graph does list(... .mappings())
        return iter(self._rows)


class _DriverResult:
    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self._rows = rows

    def mappings(self) -> _Mappings:
        return _Mappings(self._rows)


class _FakeConn:
    """Records ``exec_driver_sql`` bodies; ``execute`` feeds ``_ensure_graph``."""

    def __init__(
        self,
        *,
        graph_rows: list[dict[str, Any]] | None = None,
        driver_rows: list[dict[str, Any]] | None = None,
        raise_on: str | None = None,
    ) -> None:
        self.sql_calls: list[str] = []
        self.exec_calls: list[tuple[Any, Any]] = []
        self._graph_rows = graph_rows if graph_rows is not None else []
        self._driver_rows = driver_rows if driver_rows is not None else []
        self._raise_on = raise_on

    def exec_driver_sql(self, sql: str) -> _DriverResult:
        self.sql_calls.append(sql)
        if self._raise_on is not None and self._raise_on in sql:
            raise RuntimeError("boom")
        return _DriverResult(self._driver_rows)

    def execute(self, stmt: Any, params: Any = None) -> _DriverResult:
        self.exec_calls.append((stmt, params))
        return _DriverResult(self._graph_rows)


class _Ctx:
    def __init__(self, conn: object) -> None:
        self._conn = conn

    def __enter__(self) -> object:
        return self._conn

    def __exit__(self, *_exc: object) -> bool:
        return False


class _FakePgEngine:
    class _Dialect:
        name = "postgresql"

    def __init__(self, conn: object) -> None:
        self.dialect = self._Dialect()
        self._conn = conn

    def begin(self) -> _Ctx:
        return _Ctx(self._conn)

    def connect(self) -> _Ctx:
        return _Ctx(self._conn)


class _FakeSqliteEngine:
    class _Dialect:
        name = "sqlite"

    def __init__(self) -> None:
        self.dialect = self._Dialect()


def _store(*, enabled: bool = True, graph_name: str = "knowledge_graph") -> ApacheAgeKnowledgeStore:
    return ApacheAgeKnowledgeStore(
        session_factory=sessionmaker(),
        enabled=enabled,
        graph_name=graph_name,
    )


def _pg_store(conn: object, *, enabled: bool = True) -> ApacheAgeKnowledgeStore:
    store = _store(enabled=enabled)
    store._engine = _FakePgEngine(conn)  # type: ignore[assignment]
    store._prepare = lambda _conn: None  # type: ignore[method-assign]
    return store


# ---------------------------------------------------------------------------
# Module-level pure helpers
# ---------------------------------------------------------------------------


def test_postgres_age_required_reason_mentions_stack() -> None:
    reason = postgres_age_required_reason()
    assert "Apache AGE" in reason
    assert "./start.sh" in reason
    assert "CALIBER_DATABASE_URL" in reason


def test_select_seed_rows_query_entities_only() -> None:
    entity_rows = [{"entity_id": "E1"}]
    matched, strategy = _select_age_seed_rows(
        "query_entities_only", entity_rows, [{"entity_id": "T"}]
    )
    assert matched == entity_rows
    assert strategy == "query_entities"


def test_select_seed_rows_query_entities_only_without_matches() -> None:
    matched, strategy = _select_age_seed_rows("query_entities_only", [], [{"entity_id": "T"}])
    assert matched == []
    assert strategy is None


def test_select_seed_rows_merge_prefers_entities_when_text_missing() -> None:
    entity_rows = [{"entity_id": "E1"}]
    matched, strategy = _select_age_seed_rows("query_entities_and_text", entity_rows, [])
    assert matched == entity_rows
    assert strategy == "query_entities"


def test_select_seed_rows_merge_prefers_text_when_entities_missing() -> None:
    text_rows = [{"entity_id": "T1"}]
    matched, strategy = _select_age_seed_rows("query_entities_and_text", [], text_rows)
    assert matched == text_rows
    assert strategy == "query_text"


def test_retrieval_seed_miss_reason_covers_every_branch() -> None:
    assert "question entities" in _retrieval_seed_miss_reason(
        "query_entities_only", has_keys=True, has_query=True
    )
    assert "question text" in _retrieval_seed_miss_reason(
        "query_text_only", has_keys=True, has_query=True
    )
    assert "entities or text" in _retrieval_seed_miss_reason(
        "entity_then_text", has_keys=True, has_query=True
    )
    assert "question entities" in _retrieval_seed_miss_reason(
        "entity_then_text", has_keys=True, has_query=False
    )
    assert "question text" in _retrieval_seed_miss_reason(
        "entity_then_text", has_keys=False, has_query=True
    )


def test_merge_entity_match_rows_dedupes_by_id_and_key() -> None:
    merged = _merge_entity_match_rows(
        [{"entity_id": "E1", "label": "a"}, {"entity_id": "", "entity_key": "k1"}],
        [{"entity_id": "E1", "label": "dup"}, {"entity_key": "k1", "label": "same-key"}],
    )
    ids = [row.get("entity_id") or row.get("entity_key") for row in merged]
    assert ids == ["E1", "k1"]


def test_cypher_list_quotes_and_skips_empty() -> None:
    rendered = _cypher_list(["a", "", "b"])
    assert rendered == "['a', 'b']"


def test_retrieval_query_candidates_builds_phrases_then_tokens() -> None:
    candidates = _retrieval_query_candidates("incident response playbook")
    assert "incident response" in candidates
    assert "response playbook" in candidates
    assert "incident" in candidates
    assert "playbook" in candidates
    # stopwords and short tokens are dropped
    assert _retrieval_query_candidates("a the of it") == []


def test_preview_text_truncates_long_content() -> None:
    short = _preview_text("  hello   world  ")
    assert short == "hello world"
    long_content = "word " * 100
    preview = _preview_text(long_content)
    assert preview.endswith("...")
    assert len(preview) <= 220


def test_as_list_handles_tuple_and_other() -> None:
    assert _as_list(["a"]) == ["a"]
    assert _as_list(("a", "b")) == ["a", "b"]
    assert _as_list("scalar") == []
    assert _as_list(None) == []


# ---------------------------------------------------------------------------
# Properties + availability guards
# ---------------------------------------------------------------------------


def test_graph_name_property_returns_validated_name() -> None:
    assert _store(graph_name="my_graph").graph_name == "my_graph"


def test_available_and_unavailable_reason_when_postgres() -> None:
    store = _pg_store(_FakeConn())
    assert store.available is True
    assert store.unavailable_reason is None


def test_unavailable_reason_when_engine_is_not_postgres() -> None:
    store = _store(enabled=True)
    store._engine = _FakeSqliteEngine()  # type: ignore[assignment]
    assert store.available is False
    assert store.unavailable_reason == postgres_age_required_reason()


def test_unavailable_reason_when_disabled() -> None:
    store = _store(enabled=False)
    assert store.available is False
    assert store.unavailable_reason == "Apache AGE is disabled by configuration."


# ---------------------------------------------------------------------------
# sync_version guards + happy path + failure path
# ---------------------------------------------------------------------------


def _sync_args() -> dict[str, Any]:
    return {
        "knowledge_base_id": "KB-1",
        "knowledge_base_name": "Docs",
        "version_id": "KBV-1",
        "version_number": 1,
        "source_bucket": "docs-bucket",
        "documents": [
            AgeDocumentNode(
                document_id="DOC-1",
                object_key="docs/guide.md",
                object_name="guide.md",
                object_store_path="/object-store?bucket=demo&key=docs%2Fguide.md",
            )
        ],
        "chunks": [
            {
                "chunk_id": "CH-1",
                "document_id": "DOC-1",
                "source_key": "docs/guide.md",
                "source_name": "guide.md",
                "ordinal": 0,
                "chunk_index": 0,
                "content": "word " * 100,
            }
        ],
        "entities": [
            {
                "entity_id": "ENT-1",
                "entity_key": "bob",
                "label": "Bob",
                "entity_type": "person",
                "mention_count": 3,
                "aliases": ["Bobby"],
                "source_documents": ["DOC-1"],
                "source_keys": ["docs/guide.md"],
                "source_chunks": ["CH-1"],
            }
        ],
        "relationships": [
            {
                "relationship_id": "REL-1",
                "relationship_type": "related",
                "weight": 1.5,
                "evidence_chunk_ids": ["CH-1"],
                "source_documents": ["DOC-1"],
                "source_entity_id": "ENT-1",
                "target_entity_id": "ENT-2",
            }
        ],
    }


def test_sync_version_disabled_returns_failed() -> None:
    result = _store(enabled=False).sync_version(**_sync_args())
    assert result.status == "failed"
    assert "disabled" in (result.error or "")


def test_sync_version_non_postgres_returns_failed() -> None:
    store = _store(enabled=True)
    store._engine = _FakeSqliteEngine()  # type: ignore[assignment]
    result = store.sync_version(**_sync_args())
    assert result.status == "failed"
    assert result.error == postgres_age_required_reason()


def test_sync_version_writes_all_node_and_edge_types() -> None:
    conn = _FakeConn()  # graph_rows empty -> create_graph runs
    store = _store(enabled=True)
    store._engine = _FakePgEngine(conn)  # type: ignore[assignment]

    result = store.sync_version(**_sync_args())

    assert result.status == "synced"
    assert result.node_count == 4  # version + 1 doc + 1 chunk + 1 entity
    assert result.edge_count == 5  # doc + chunk + entity + 1 mention + 1 relationship
    joined = "\n".join(conn.sql_calls)
    assert "create_graph('knowledge_graph')" in joined
    assert "DETACH DELETE" in joined
    assert "CREATE (:KBVersion" in joined
    assert "CREATE (:KBDocument" in joined
    assert "CREATE (:KBChunk" in joined
    assert "CREATE (:KBEntity" in joined
    assert "HAS_DOCUMENT" in joined
    assert "HAS_ENTITY" in joined
    assert "HAS_CHUNK" in joined
    assert "MENTIONED_IN" in joined
    assert ":RELATED" in joined
    # object-store percent literals are escaped for the psycopg driver.
    assert "docs%%2Fguide.md" in joined


def test_sync_version_failure_is_wrapped_as_failed_result() -> None:
    conn = _FakeConn(raise_on="search_path")  # _prepare raises inside the txn
    store = _store(enabled=True)
    store._engine = _FakePgEngine(conn)  # type: ignore[assignment]

    result = store.sync_version(**_sync_args())

    assert result.status == "failed"
    assert result.error is not None
    assert result.error.startswith("RuntimeError:")


# ---------------------------------------------------------------------------
# drop_version
# ---------------------------------------------------------------------------


def test_drop_version_returns_false_when_unavailable() -> None:
    assert _store(enabled=False).drop_version(version_id="KBV-1") is False


def test_drop_version_runs_detach_delete_when_available() -> None:
    conn = _FakeConn()
    store = _store(enabled=True)
    store._engine = _FakePgEngine(conn)  # type: ignore[assignment]

    assert store.drop_version(version_id="KBV-9") is True
    joined = "\n".join(conn.sql_calls)
    assert "DETACH DELETE" in joined
    assert "KBV-9" in joined


# ---------------------------------------------------------------------------
# retrieve guard + control-flow branches
# ---------------------------------------------------------------------------


def test_retrieve_disabled_falls_back() -> None:
    result = _store(enabled=False).retrieve(
        version_id="KBV-1",
        query_entity_keys=["bob"],
        retrieval_strength="balanced",
        minimum_relationship_weight=1.0,
        top_k=3,
        traversal_hops=1,
        candidate_pool_size=6,
    )
    assert result.status == "fallback"
    assert "disabled" in (result.fallback_reason or "")


def test_retrieve_non_postgres_falls_back() -> None:
    store = _store(enabled=True)
    store._engine = _FakeSqliteEngine()  # type: ignore[assignment]
    result = store.retrieve(
        version_id="KBV-1",
        query_entity_keys=["bob"],
        retrieval_strength="balanced",
        minimum_relationship_weight=1.0,
        top_k=3,
        traversal_hops=1,
        candidate_pool_size=6,
    )
    assert result.status == "fallback"
    assert result.fallback_reason == postgres_age_required_reason()


def test_retrieve_without_keys_or_query_falls_back() -> None:
    store = _pg_store(_FakeConn())
    result = store.retrieve(
        version_id="KBV-1",
        query_entity_keys=[],
        retrieval_strength="balanced",
        minimum_relationship_weight=1.0,
        top_k=3,
        traversal_hops=1,
        candidate_pool_size=6,
        query_text="   ",
    )
    assert result.status == "fallback"
    assert "did not produce any graph entities" in (result.fallback_reason or "")


def test_retrieve_wraps_query_exceptions_as_fallback() -> None:
    store = _store(enabled=True)
    store._engine = _FakePgEngine(_FakeConn())  # type: ignore[assignment]

    def _boom(_conn: object) -> None:
        raise RuntimeError("cypher exploded")

    store._prepare = _boom  # type: ignore[method-assign]
    result = store.retrieve(
        version_id="KBV-1",
        query_entity_keys=["bob"],
        retrieval_strength="balanced",
        minimum_relationship_weight=1.0,
        top_k=3,
        traversal_hops=1,
        candidate_pool_size=6,
    )
    assert result.status == "fallback"
    assert "Apache AGE query failed" in (result.fallback_reason or "")


def test_retrieve_falls_back_when_seed_matches_nothing() -> None:
    store = _pg_store(_FakeConn())
    store._match_entities = lambda _c, *, version_id, query_entity_keys: []  # type: ignore[method-assign]
    store._match_entities_from_query_text = (  # type: ignore[method-assign]
        lambda _c, *, version_id, query_text, limit: []
    )
    result = store.retrieve(
        version_id="KBV-1",
        query_entity_keys=["nope"],
        retrieval_strength="balanced",
        minimum_relationship_weight=1.0,
        top_k=3,
        traversal_hops=0,
        candidate_pool_size=6,
        query_text="unmatched question",
    )
    assert result.status == "fallback"
    assert "No Apache AGE entities matched" in (result.fallback_reason or "")


def test_retrieve_falls_back_when_no_chunk_lineage() -> None:
    store = _pg_store(_FakeConn())
    store._match_entities = (  # type: ignore[method-assign]
        lambda _c, *, version_id, query_entity_keys: [
            {"entity_id": "ENT-1", "label": "Bob", "entity_key": "bob", "mention_count": 3}
        ]
    )
    store._direct_chunk_matches = (  # type: ignore[method-assign]
        lambda _c, *, version_id, matched_entity_ids: []
    )
    result = store.retrieve(
        version_id="KBV-1",
        query_entity_keys=["bob"],
        retrieval_strength="balanced",
        minimum_relationship_weight=1.0,
        top_k=3,
        traversal_hops=0,
        candidate_pool_size=6,
    )
    assert result.status == "fallback"
    assert "did not return any chunk lineage" in (result.fallback_reason or "")
    assert result.matched_entities == ("Bob",)


# ---------------------------------------------------------------------------
# explore guard + control-flow branches
# ---------------------------------------------------------------------------


def test_explore_disabled_falls_back() -> None:
    result = _store(enabled=False).explore(version_id="KBV-1", query="bob")
    assert result.status == "fallback"
    assert "disabled" in (result.fallback_reason or "")


def test_explore_non_postgres_falls_back() -> None:
    store = _store(enabled=True)
    store._engine = _FakeSqliteEngine()  # type: ignore[assignment]
    result = store.explore(version_id="KBV-1", query="bob")
    assert result.status == "fallback"
    assert result.fallback_reason == postgres_age_required_reason()


def test_explore_wraps_exceptions_as_fallback() -> None:
    store = _store(enabled=True)
    store._engine = _FakePgEngine(_FakeConn())  # type: ignore[assignment]

    def _boom(_conn: object) -> None:
        raise RuntimeError("explore exploded")

    store._prepare = _boom  # type: ignore[method-assign]
    result = store.explore(version_id="KBV-1", query="bob")
    assert result.status == "fallback"
    assert "graph exploration failed" in (result.fallback_reason or "")


def test_explore_seeds_from_query_text_when_matchers_miss() -> None:
    store = _pg_store(_FakeConn())
    store._match_entities = (  # type: ignore[method-assign]
        lambda _c, *, version_id, query_entity_keys, entity_type=None: []
    )
    store._explore_seed_entities = (  # type: ignore[method-assign]
        lambda _c, *, version_id, query, entity_type, limit: [
            {
                "entity_id": "ENT-9",
                "entity_key": "theme-updates",
                "label": "Theme updates",
                "entity_type": "concept",
                "mention_count": 3,
                "aliases": [],
                "source_documents": [],
                "source_keys": [],
            }
        ]
    )
    store._explore_neighbor_entities = (  # type: ignore[method-assign]
        lambda _c, *, version_id, seed_entity_ids, entity_type, minimum_relationship_weight, limit: []
    )
    store._explore_relationships = (  # type: ignore[method-assign]
        lambda _c, *, version_id, entity_ids, minimum_relationship_weight: []
    )

    result = store.explore(
        version_id="KBV-1",
        query="platform reliability",
        query_entity_keys=["nomatch"],
        seed_mode="query_entities_only",
        traversal_hops=1,
    )
    assert result.status == "ok"
    assert result.seed_strategy == "query_text"
    assert [entity.label for entity in result.entities] == ["Theme updates"]


def test_explore_returns_empty_with_reason_when_no_seed() -> None:
    store = _pg_store(_FakeConn())
    store._match_entities = (  # type: ignore[method-assign]
        lambda _c, *, version_id, query_entity_keys, entity_type=None: []
    )
    result = store.explore(
        version_id="KBV-1",
        query="",
        query_entity_keys=["nomatch"],
        seed_mode="query_entities_only",
        traversal_hops=1,
    )
    assert result.status == "ok"
    assert result.entities == []
    assert "No Apache AGE entities matched the current graph filters" in (
        result.fallback_reason or ""
    )


def _seed_row(entity_id: str, label: str) -> dict[str, Any]:
    return {
        "entity_id": entity_id,
        "entity_key": label.lower(),
        "label": label,
        "entity_type": "concept",
        "mention_count": 1,
        "aliases": [],
        "source_documents": [],
        "source_keys": [],
    }


def test_explore_stops_expansion_when_frontier_empties() -> None:
    store = _pg_store(_FakeConn())
    store._explore_seed_entities = (  # type: ignore[method-assign]
        lambda _c, *, version_id, query, entity_type, limit: [_seed_row("ENT-1", "Bob")]
    )
    store._explore_neighbor_entities = (  # type: ignore[method-assign]
        lambda _c, *, version_id, seed_entity_ids, entity_type, minimum_relationship_weight, limit: []
    )
    store._explore_relationships = (  # type: ignore[method-assign]
        lambda _c, *, version_id, entity_ids, minimum_relationship_weight: []
    )
    result = store.explore(version_id="KBV-1", query="Bob", traversal_hops=2, node_limit=12)
    assert result.status == "ok"
    assert [entity.label for entity in result.entities] == ["Bob"]


def test_explore_frontier_skips_known_and_stops_at_node_limit() -> None:
    store = _pg_store(_FakeConn())
    store._explore_seed_entities = (  # type: ignore[method-assign]
        lambda _c, *, version_id, query, entity_type, limit: [_seed_row("ENT-1", "Bob")]
    )
    # First row repeats the seed (skipped), then four new neighbors — the node
    # cap of 4 stops ingestion after three neighbors are added.
    neighbors = [
        _seed_row("ENT-1", "Bob"),  # already known -> continue
        _seed_row("ENT-2", "Alpha"),
        _seed_row("ENT-3", "Beta"),
        _seed_row("ENT-4", "Gamma"),
        _seed_row("ENT-5", "Delta"),  # never reached -> break at node limit
    ]
    store._explore_neighbor_entities = (  # type: ignore[method-assign]
        lambda _c, *, version_id, seed_entity_ids, entity_type, minimum_relationship_weight, limit: (
            list(neighbors)
        )
    )
    store._explore_relationships = (  # type: ignore[method-assign]
        lambda _c, *, version_id, entity_ids, minimum_relationship_weight: []
    )
    result = store.explore(version_id="KBV-1", query="Bob", traversal_hops=1, node_limit=4)
    assert result.status == "ok"
    labels = {entity.label for entity in result.entities}
    assert labels == {"Bob", "Alpha", "Beta", "Gamma"}
    assert "Delta" not in labels


# ---------------------------------------------------------------------------
# Query-string builders + row helpers (stub _query_cypher to capture the body)
# ---------------------------------------------------------------------------


def _capture(store: ApacheAgeKnowledgeStore) -> list[str]:
    bodies: list[str] = []

    def _fake(_conn: object, body: str, *, columns: list[str]) -> list[dict[str, Any]]:
        bodies.append(body)
        return []

    store._query_cypher = _fake  # type: ignore[method-assign]
    return bodies


def test_match_entities_builds_key_and_type_filters() -> None:
    store = _store()
    bodies = _capture(store)
    store._match_entities(
        object(),
        version_id="KBV-1",
        query_entity_keys=["bob", ""],
        entity_type="Person",
    )
    body = bodies[0]
    assert "e.entity_key IN ['bob']" in body
    # The value is inlined verbatim; the lower-casing is applied DB-side.
    assert "toLower(e.entity_type) = 'Person'" in body
    assert "ORDER BY e.mention_count DESC LIMIT 8" in body


def test_match_entities_from_query_text_empty_when_no_candidates() -> None:
    store = _store()
    bodies = _capture(store)
    rows = store._match_entities_from_query_text(
        object(), version_id="KBV-1", query_text="a the of", limit=4
    )
    assert rows == []
    assert bodies == []  # short-circuited before any query


def test_match_entities_from_query_text_filters_unmatched_rows() -> None:
    store = _store()

    def _fake(_conn: object, body: str, *, columns: list[str]) -> list[dict[str, Any]]:
        if "UNWIND" in body:
            return []
        return [
            {
                "entity_id": "ENT-hit",
                "label": "Incident Response",
                "entity_key": "incident-response",
                "mention_count": 4,
                "aliases": [],
            },
            {
                "entity_id": "ENT-miss",
                "label": "Totally Unrelated",
                "entity_key": "unrelated",
                "mention_count": 9,
                "aliases": [],
            },
        ]

    store._query_cypher = _fake  # type: ignore[method-assign]
    rows = store._match_entities_from_query_text(
        object(), version_id="KBV-1", query_text="incident response", limit=4
    )
    assert [row["entity_id"] for row in rows] == ["ENT-hit"]


def test_direct_chunk_matches_builds_mentioned_in_query() -> None:
    store = _store()
    bodies = _capture(store)
    store._direct_chunk_matches(object(), version_id="KBV-1", matched_entity_ids=["ENT-1"])
    body = bodies[0]
    assert ":MENTIONED_IN]->(c:KBChunk" in body
    assert "e.entity_id IN ['ENT-1']" in body


def test_explore_seed_entities_query_and_default_bodies() -> None:
    store = _store()
    bodies = _capture(store)
    store._explore_seed_entities(
        object(), version_id="KBV-1", query="bob", entity_type="person", limit=5
    )
    store._explore_seed_entities(object(), version_id="KBV-1", query="", entity_type=None, limit=5)
    assert "toLower(e.label) CONTAINS 'bob'" in bodies[0]
    assert "e.entity_type = 'person'" in bodies[0]
    assert "WHERE true" in bodies[1]


def test_explore_neighbor_entities_empty_seed_returns_early() -> None:
    store = _store()
    bodies = _capture(store)
    assert (
        store._explore_neighbor_entities(
            object(),
            version_id="KBV-1",
            seed_entity_ids=[],
            entity_type=None,
            minimum_relationship_weight=0.0,
            limit=10,
        )
        == []
    )
    assert bodies == []


def test_explore_neighbor_entities_builds_related_traversal() -> None:
    store = _store()
    bodies = _capture(store)
    store._explore_neighbor_entities(
        object(),
        version_id="KBV-1",
        seed_entity_ids=["ENT-1"],
        entity_type="concept",
        minimum_relationship_weight=0.5,
        limit=10,
    )
    body = bodies[0]
    assert "[rel:RELATED]-(n:KBEntity" in body
    assert "coalesce(rel.weight, 0) >= 0.5" in body
    assert "n.entity_type = 'concept'" in body


def test_explore_relationships_needs_two_entities() -> None:
    store = _store()
    bodies = _capture(store)
    assert (
        store._explore_relationships(
            object(), version_id="KBV-1", entity_ids=["ENT-1"], minimum_relationship_weight=0.0
        )
        == []
    )
    assert bodies == []


def test_explore_relationships_builds_pairwise_query() -> None:
    store = _store()
    bodies = _capture(store)
    store._explore_relationships(
        object(),
        version_id="KBV-1",
        entity_ids=["ENT-1", "ENT-2"],
        minimum_relationship_weight=1.0,
    )
    body = bodies[0]
    assert "s.entity_id < t.entity_id" in body
    assert "s.entity_id IN ['ENT-1', 'ENT-2']" in body


def test_neighbor_and_two_hop_chunk_matches_build_expected_paths() -> None:
    store = _store()
    bodies = _capture(store)
    store._neighbor_chunk_matches(
        object(), version_id="KBV-1", matched_entity_ids=["ENT-1"], minimum_relationship_weight=1.0
    )
    store._two_hop_chunk_matches(
        object(), version_id="KBV-1", matched_entity_ids=["ENT-1"], minimum_relationship_weight=1.0
    )
    one_hop, two_hop = bodies
    assert "[rel:RELATED]-(n:KBEntity" in one_hop
    assert "[rel1:RELATED]" in two_hop
    assert "[rel2:RELATED]" in two_hop
    assert "e.entity_id <> n2.entity_id" in two_hop


def test_accumulate_chunk_rows_skips_blank_chunk_ids() -> None:
    from collections import defaultdict

    store = _store()
    scores: defaultdict[str, float] = defaultdict(float)
    labels: defaultdict[str, set[str]] = defaultdict(set)
    expanded: defaultdict[str, set[str]] = defaultdict(set)
    hop: defaultdict[str, float] = defaultdict(float)
    store._accumulate_chunk_rows(
        [
            {"chunk_id": "", "graph_score": 5.0, "matched_labels": ["skip"]},
            {
                "chunk_id": "CH-1",
                "graph_score": 2.0,
                "matched_labels": ["Bob"],
                "expanded_labels": ["Reliability"],
            },
        ],
        factor=0.5,
        chunk_scores=scores,
        chunk_labels=labels,
        chunk_expanded_labels=expanded,
        hop_scores=hop,
    )
    assert dict(scores) == {"CH-1": 1.0}
    assert labels["CH-1"] == {"Bob"}
    assert expanded["CH-1"] == {"Reliability"}
    assert "" not in scores


def test_relationship_hop_distance_none_when_missing() -> None:
    store = _store()
    assert (
        store._relationship_hop_distance(
            {"source_entity_id": "ENT-1", "target_entity_id": "ENT-2"},
            distances={"ENT-1": 0},
        )
        is None
    )
    assert (
        store._relationship_hop_distance(
            {"source_entity_id": "ENT-1", "target_entity_id": "ENT-2"},
            distances={"ENT-1": 0, "ENT-2": 2},
        )
        == 2
    )


def test_batch_run_cypher_skips_empty_statements() -> None:
    store = _store()
    conn = _FakeConn()
    store._batch_run_cypher(conn, ["", "CREATE (:X {a: 'b'})", ""], batch_size=8)
    assert len(conn.sql_calls) == 1
    assert "CREATE (:X" in conn.sql_calls[0]


def test_batch_create_nodes_validates_label_and_emits_one_per_prop() -> None:
    store = _store()
    conn = _FakeConn()
    store._batch_create_nodes(
        conn,
        "KBEntity",
        [{"entity_id": "ENT-1"}, {}, {"entity_id": "ENT-2"}],
    )
    # Empty prop dict is filtered out; two node CREATEs remain.
    assert len(conn.sql_calls) == 2


def test_create_entity_chunk_edges_skips_blank_entities_and_chunks() -> None:
    store = _store()
    conn = _FakeConn()
    store._create_entity_chunk_edges(
        conn,
        version_id="KBV-1",
        entities=[
            {"entity_id": "", "source_chunks": ["CH-x"]},  # blank entity_id -> skip
            {"entity_id": "ENT-1", "source_chunks": ["", "CH-1"]},  # blank chunk -> skip
        ],
    )
    # Only ENT-1 -> CH-1 survives both guards.
    assert len(conn.sql_calls) == 1
    assert "MENTIONED_IN" in conn.sql_calls[0]
    assert "CH-1" in conn.sql_calls[0]


def test_build_age_store_reads_config_flags() -> None:
    from caliber.knowledge.age import build_age_store

    class _Cfg:
        knowledge_age_enabled = True
        knowledge_age_graph_name = "custom_graph"

    store = build_age_store(config=_Cfg(), session_factory=sessionmaker())
    assert store.graph_name == "custom_graph"


def test_ensure_graph_creates_when_absent_and_skips_when_present() -> None:
    store = _store()
    absent = _FakeConn(graph_rows=[])
    store._ensure_graph(absent)
    assert any("create_graph" in sql for sql in absent.sql_calls)

    present = _FakeConn(graph_rows=[{"name": "knowledge_graph"}])
    store._ensure_graph(present)
    assert present.sql_calls == []  # graph already exists -> no create
