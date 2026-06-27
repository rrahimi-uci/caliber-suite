from __future__ import annotations

from sqlalchemy.orm import sessionmaker

from caliber.knowledge.age import AgeDocumentNode, ApacheAgeKnowledgeStore


class _FakeMappingsResult:
    def all(self) -> list[dict[str, object]]:
        return []


class _FakeDriverResult:
    def mappings(self) -> _FakeMappingsResult:
        return _FakeMappingsResult()


class _RecordingConnection:
    def __init__(self) -> None:
        self.sql_calls: list[str] = []

    def exec_driver_sql(self, sql: str) -> _FakeDriverResult:
        self.sql_calls.append(sql)
        return _FakeDriverResult()


class _FakeConnectContext:
    def __init__(self, conn: object) -> None:
        self._conn = conn

    def __enter__(self) -> object:
        return self._conn

    def __exit__(self, exc_type, exc, tb) -> None:
        return None


class _FakeEngine:
    class _Dialect:
        name = "postgresql"

    def __init__(self, conn: object) -> None:
        self.dialect = self._Dialect()
        self._conn = conn

    def connect(self) -> _FakeConnectContext:
        return _FakeConnectContext(self._conn)


def test_query_cypher_escapes_percent_for_driver_sql() -> None:
    store = ApacheAgeKnowledgeStore(
        session_factory=sessionmaker(),
        enabled=True,
        graph_name="knowledge_graph",
    )
    conn = _RecordingConnection()

    rows = store._query_cypher(
        conn,
        "CREATE (:KBDocument {object_store_path: '/object-store?bucket=demo&key=docs%2Fguide.md'})",
        columns=["result"],
    )

    assert rows == []
    assert conn.sql_calls
    assert "docs%%2Fguide.md" in conn.sql_calls[0]
    assert "docs%2Fguide.md" not in conn.sql_calls[0]


def test_batch_run_cypher_executes_each_write_statement_individually() -> None:
    store = ApacheAgeKnowledgeStore(
        session_factory=sessionmaker(),
        enabled=True,
        graph_name="knowledge_graph",
    )
    conn = _RecordingConnection()

    store._batch_run_cypher(
        conn,
        [
            "CREATE (:KBVersion {version_id: 'KBV-1'})",
            "CREATE (:KBVersion {version_id: 'KBV-2'})",
        ],
        batch_size=8,
    )

    assert len(conn.sql_calls) == 2
    assert "KBV-1" in conn.sql_calls[0]
    assert "KBV-2" in conn.sql_calls[1]


def test_match_entities_from_query_text_uses_age_compatible_alias_query() -> None:
    store = ApacheAgeKnowledgeStore(
        session_factory=sessionmaker(),
        enabled=True,
        graph_name="knowledge_graph",
    )
    conn = object()
    query_bodies: list[str] = []

    def _fake_query(
        _conn,
        body: str,
        *,
        columns: list[str],
    ) -> list[dict[str, object]]:
        assert columns == ["entity_id", "label", "entity_key", "mention_count", "aliases"]
        query_bodies.append(body)
        if "UNWIND coalesce(e.aliases, []) AS alias" in body:
            return [
                {
                    "entity_id": "ENT-alias",
                    "label": "Caliber Incident Playbook",
                    "entity_key": "caliber-incident-playbook",
                    "mention_count": 5,
                    "aliases": ["incident playbook"],
                }
            ]
        return [
            {
                "entity_id": "ENT-label",
                "label": "Incident Response",
                "entity_key": "incident-response",
                "mention_count": 3,
                "aliases": ["response guide"],
            }
        ]

    store._query_cypher = _fake_query  # type: ignore[method-assign]

    rows = store._match_entities_from_query_text(
        conn,
        version_id="KBV-1",
        query_text="incident playbook",
        limit=4,
    )

    assert len(query_bodies) == 2
    assert all("any(alias IN" not in body for body in query_bodies)
    assert any(
        "UNWIND coalesce(e.aliases, []) AS alias WITH e, alias WHERE" in body
        for body in query_bodies
    )
    assert rows[0]["entity_id"] == "ENT-alias"
    assert {row["entity_id"] for row in rows} == {"ENT-alias", "ENT-label"}


def test_create_version_edges_avoids_joined_match_create_patterns() -> None:
    store = ApacheAgeKnowledgeStore(
        session_factory=sessionmaker(),
        enabled=True,
        graph_name="knowledge_graph",
    )
    conn = _RecordingConnection()

    store._create_version_edges(
        conn,
        version_id="KBV-1",
        documents=[
            AgeDocumentNode(
                document_id="DOC-1",
                object_key="docs/guide.md",
                object_name="guide.md",
                object_store_path="/object-store?bucket=demo&key=docs%2Fguide.md",
            )
        ],
        entities=[{"entity_id": "KBE-1"}],
    )

    assert len(conn.sql_calls) == 2
    assert "), (d:KBDocument" not in conn.sql_calls[0]
    assert "WITH v MATCH (d:KBDocument" in conn.sql_calls[0]
    assert "), (e:KBEntity" not in conn.sql_calls[1]
    assert "WITH v MATCH (e:KBEntity" in conn.sql_calls[1]


def test_retrieve_respects_configured_hops_and_candidate_pool() -> None:
    store = ApacheAgeKnowledgeStore(
        session_factory=sessionmaker(),
        enabled=True,
        graph_name="knowledge_graph",
    )
    conn = object()
    store._engine = _FakeEngine(conn)
    store._prepare = lambda _conn: None  # type: ignore[method-assign]
    store._match_entities = (  # type: ignore[method-assign]
        lambda _conn, *, version_id, query_entity_keys: [
            {
                "entity_id": "ENT-1",
                "label": "Bob",
                "entity_key": "bob",
                "mention_count": 3,
            }
        ]
    )
    store._direct_chunk_matches = (  # type: ignore[method-assign]
        lambda _conn, *, version_id, matched_entity_ids: [
            {
                "chunk_id": "CH-1",
                "matched_labels": ["Bob"],
                "expanded_labels": [],
                "graph_score": 3.0,
            },
            {
                "chunk_id": "CH-2",
                "matched_labels": ["Bob"],
                "expanded_labels": [],
                "graph_score": 1.0,
            },
        ]
    )
    hop_calls: list[str] = []
    store._neighbor_chunk_matches = (  # type: ignore[method-assign]
        lambda _conn, *, version_id, matched_entity_ids, minimum_relationship_weight: (
            hop_calls.append("one")
            or [
                {
                    "chunk_id": "CH-2",
                    "matched_labels": ["Bob"],
                    "expanded_labels": ["Platform reliability"],
                    "graph_score": 4.0,
                }
            ]
        )
    )
    store._two_hop_chunk_matches = (  # type: ignore[method-assign]
        lambda _conn, *, version_id, matched_entity_ids, minimum_relationship_weight: (
            hop_calls.append("two")
            or [
                {
                    "chunk_id": "CH-3",
                    "matched_labels": ["Bob"],
                    "expanded_labels": ["Platform reliability", "Support"],
                    "graph_score": 2.0,
                }
            ]
        )
    )

    direct_only = store.retrieve(
        version_id="KBV-1",
        query_entity_keys=["bob"],
        retrieval_strength="balanced",
        minimum_relationship_weight=1.0,
        top_k=1,
        traversal_hops=0,
        candidate_pool_size=2,
    )

    assert hop_calls == []
    assert direct_only.traversal_hops == 0
    assert [candidate.chunk_id for candidate in direct_only.chunk_candidates] == ["CH-1", "CH-2"]

    expanded = store.retrieve(
        version_id="KBV-1",
        query_entity_keys=["bob"],
        retrieval_strength="aggressive",
        minimum_relationship_weight=1.0,
        top_k=1,
        traversal_hops=2,
        candidate_pool_size=3,
    )

    assert hop_calls == ["one", "two"]
    assert expanded.traversal_hops == 2
    assert [candidate.chunk_id for candidate in expanded.chunk_candidates] == [
        "CH-2",
        "CH-1",
        "CH-3",
    ]
    assert "Platform reliability" in expanded.expanded_entities
    assert expanded.chunk_candidates[0].direct_score == 1.0
    assert expanded.chunk_candidates[0].one_hop_score > 0
    assert expanded.chunk_candidates[0].two_hop_score == 0
    assert expanded.chunk_candidates[0].expanded_entities == ("Platform reliability",)
    assert expanded.chunk_candidates[2].two_hop_score > 0


def test_retrieve_can_seed_from_query_text_when_entity_keys_are_missing() -> None:
    store = ApacheAgeKnowledgeStore(
        session_factory=sessionmaker(),
        enabled=True,
        graph_name="knowledge_graph",
    )
    conn = object()
    store._engine = _FakeEngine(conn)
    store._prepare = lambda _conn: None  # type: ignore[method-assign]
    store._match_entities = (  # type: ignore[method-assign]
        lambda _conn, *, version_id, query_entity_keys: []
    )
    store._match_entities_from_query_text = (  # type: ignore[method-assign]
        lambda _conn, *, version_id, query_text, limit: [
            {
                "entity_id": "ENT-9",
                "label": "Theme updates",
                "entity_key": "theme-updates",
                "mention_count": 3,
            }
        ]
    )
    store._direct_chunk_matches = (  # type: ignore[method-assign]
        lambda _conn, *, version_id, matched_entity_ids: [
            {
                "chunk_id": "CH-9",
                "matched_labels": ["Theme updates"],
                "expanded_labels": [],
                "graph_score": 2.5,
            }
        ]
    )
    store._neighbor_chunk_matches = (  # type: ignore[method-assign]
        lambda _conn, *, version_id, matched_entity_ids, minimum_relationship_weight: []
    )
    store._two_hop_chunk_matches = (  # type: ignore[method-assign]
        lambda _conn, *, version_id, matched_entity_ids, minimum_relationship_weight: []
    )

    result = store.retrieve(
        version_id="KBV-1",
        query_entity_keys=[],
        retrieval_strength="balanced",
        minimum_relationship_weight=1.0,
        top_k=3,
        traversal_hops=0,
        candidate_pool_size=6,
        query_text="who handles theme updates?",
    )

    assert result.status == "ok"
    assert result.seed_strategy == "query_text"
    assert result.matched_entities == ("Theme updates",)
    assert [candidate.chunk_id for candidate in result.chunk_candidates] == ["CH-9"]
    assert result.chunk_candidates[0].graph_score == 2.5


def test_retrieve_can_be_forced_to_use_query_text_only() -> None:
    store = ApacheAgeKnowledgeStore(
        session_factory=sessionmaker(),
        enabled=True,
        graph_name="knowledge_graph",
    )
    conn = object()
    store._engine = _FakeEngine(conn)
    store._prepare = lambda _conn: None  # type: ignore[method-assign]
    entity_match_calls: list[list[str]] = []
    text_match_calls: list[str] = []
    store._match_entities = (  # type: ignore[method-assign]
        lambda _conn, *, version_id, query_entity_keys: (
            entity_match_calls.append(query_entity_keys)
            or [
                {
                    "entity_id": "ENT-ignored",
                    "label": "Ignored entity",
                    "entity_key": "ignored-entity",
                    "mention_count": 5,
                }
            ]
        )
    )
    store._match_entities_from_query_text = (  # type: ignore[method-assign]
        lambda _conn, *, version_id, query_text, limit: (
            text_match_calls.append(query_text)
            or [
                {
                    "entity_id": "ENT-9",
                    "label": "Theme updates",
                    "entity_key": "theme-updates",
                    "mention_count": 3,
                }
            ]
        )
    )
    store._direct_chunk_matches = (  # type: ignore[method-assign]
        lambda _conn, *, version_id, matched_entity_ids: [
            {
                "chunk_id": "CH-9",
                "matched_labels": ["Theme updates"],
                "expanded_labels": [],
                "graph_score": 2.5,
            }
        ]
    )
    store._neighbor_chunk_matches = (  # type: ignore[method-assign]
        lambda _conn, *, version_id, matched_entity_ids, minimum_relationship_weight: []
    )
    store._two_hop_chunk_matches = (  # type: ignore[method-assign]
        lambda _conn, *, version_id, matched_entity_ids, minimum_relationship_weight: []
    )

    result = store.retrieve(
        version_id="KBV-1",
        query_entity_keys=["ignored-entity"],
        retrieval_strength="balanced",
        minimum_relationship_weight=1.0,
        top_k=3,
        traversal_hops=0,
        candidate_pool_size=6,
        seed_mode="query_text_only",
        query_text="who handles theme updates?",
    )

    assert entity_match_calls == []
    assert text_match_calls == ["who handles theme updates?"]
    assert result.status == "ok"
    assert result.seed_strategy == "query_text"
    assert result.matched_entities == ("Theme updates",)


def test_retrieve_can_merge_query_entities_and_text_matches() -> None:
    store = ApacheAgeKnowledgeStore(
        session_factory=sessionmaker(),
        enabled=True,
        graph_name="knowledge_graph",
    )
    conn = object()
    store._engine = _FakeEngine(conn)
    store._prepare = lambda _conn: None  # type: ignore[method-assign]
    store._match_entities = (  # type: ignore[method-assign]
        lambda _conn, *, version_id, query_entity_keys: [
            {
                "entity_id": "ENT-1",
                "label": "Bob",
                "entity_key": "bob",
                "mention_count": 5,
            }
        ]
    )
    store._match_entities_from_query_text = (  # type: ignore[method-assign]
        lambda _conn, *, version_id, query_text, limit: [
            {
                "entity_id": "ENT-1",
                "label": "Bob",
                "entity_key": "bob",
                "mention_count": 5,
            },
            {
                "entity_id": "ENT-2",
                "label": "Platform reliability",
                "entity_key": "platform-reliability",
                "mention_count": 3,
            },
        ]
    )
    direct_match_entity_ids: list[list[str]] = []
    store._direct_chunk_matches = (  # type: ignore[method-assign]
        lambda _conn, *, version_id, matched_entity_ids: (
            direct_match_entity_ids.append(matched_entity_ids)
            or [
                {
                    "chunk_id": "CH-1",
                    "matched_labels": ["Bob"],
                    "expanded_labels": [],
                    "graph_score": 2.0,
                },
                {
                    "chunk_id": "CH-2",
                    "matched_labels": ["Platform reliability"],
                    "expanded_labels": [],
                    "graph_score": 1.5,
                },
            ]
        )
    )
    store._neighbor_chunk_matches = (  # type: ignore[method-assign]
        lambda _conn, *, version_id, matched_entity_ids, minimum_relationship_weight: []
    )
    store._two_hop_chunk_matches = (  # type: ignore[method-assign]
        lambda _conn, *, version_id, matched_entity_ids, minimum_relationship_weight: []
    )

    result = store.retrieve(
        version_id="KBV-1",
        query_entity_keys=["bob"],
        retrieval_strength="balanced",
        minimum_relationship_weight=1.0,
        top_k=3,
        traversal_hops=0,
        candidate_pool_size=6,
        seed_mode="query_entities_and_text",
        query_text="who owns platform reliability?",
    )

    assert direct_match_entity_ids == [["ENT-1", "ENT-2"]]
    assert result.status == "ok"
    assert result.seed_strategy == "query_entities_and_text"
    assert result.matched_entities == ("Bob", "Platform reliability")
    assert [candidate.chunk_id for candidate in result.chunk_candidates] == ["CH-1", "CH-2"]


def test_explore_returns_seed_and_neighbor_subgraph() -> None:
    store = ApacheAgeKnowledgeStore(
        session_factory=sessionmaker(),
        enabled=True,
        graph_name="knowledge_graph",
    )
    conn = object()
    store._engine = _FakeEngine(conn)
    store._prepare = lambda _conn: None  # type: ignore[method-assign]
    store._explore_seed_entities = (  # type: ignore[method-assign]
        lambda _conn, *, version_id, query, entity_type, limit: [
            {
                "entity_id": "ENT-1",
                "entity_key": "bob",
                "label": "Bob",
                "entity_type": "person",
                "mention_count": 4,
                "aliases": ["Bob"],
                "source_documents": ["DOC-1"],
                "source_keys": ["docs/guide.md"],
            }
        ]
    )
    store._explore_neighbor_entities = (  # type: ignore[method-assign]
        lambda _conn, *, version_id, seed_entity_ids, entity_type, minimum_relationship_weight, limit: [
            {
                "entity_id": "ENT-2",
                "entity_key": "platform-reliability",
                "label": "Platform reliability",
                "entity_type": "concept",
                "mention_count": 2,
                "aliases": ["Platform reliability"],
                "source_documents": ["DOC-1"],
                "source_keys": ["docs/guide.md"],
                "max_weight": 3.0,
            }
        ]
    )
    store._explore_relationships = (  # type: ignore[method-assign]
        lambda _conn, *, version_id, entity_ids, minimum_relationship_weight: [
            {
                "relationship_id": "REL-1",
                "source_entity_id": "ENT-1",
                "source_entity_key": "bob",
                "source_entity_label": "Bob",
                "target_entity_id": "ENT-2",
                "target_entity_key": "platform-reliability",
                "target_entity_label": "Platform reliability",
                "relationship_type": "co_occurs",
                "weight": 3.0,
                "evidence_chunk_ids": ["CH-1"],
                "source_documents": ["DOC-1"],
            }
        ]
    )

    result = store.explore(
        version_id="KBV-1",
        query="Bob",
        entity_type=None,
        minimum_relationship_weight=1.0,
        traversal_hops=1,
        node_limit=6,
    )

    assert result.status == "ok"
    assert [entity.label for entity in result.entities] == ["Bob", "Platform reliability"]
    assert result.entities[0].distance == 0
    assert result.entities[0].highlighted is True
    assert result.entities[1].distance == 1
    assert result.relationships[0].relationship_id == "REL-1"
    assert result.relationships[0].hop_distance == 1
    assert result.matched_entities == ("Bob",)
    assert result.expanded_entities == ("Platform reliability",)


def test_explore_can_use_query_text_only_seed_mode() -> None:
    store = ApacheAgeKnowledgeStore(
        session_factory=sessionmaker(),
        enabled=True,
        graph_name="knowledge_graph",
    )
    conn = object()
    store._engine = _FakeEngine(conn)
    store._prepare = lambda _conn: None  # type: ignore[method-assign]
    store._match_entities = (  # type: ignore[method-assign]
        lambda _conn, *, version_id, query_entity_keys, entity_type=None: [
            {
                "entity_id": "ENT-ignore",
                "entity_key": "ignored",
                "label": "Ignored",
                "entity_type": "person",
                "mention_count": 9,
                "aliases": ["Ignored"],
                "source_documents": ["DOC-0"],
                "source_keys": ["docs/ignored.md"],
            }
        ]
    )
    store._match_entities_from_query_text = (  # type: ignore[method-assign]
        lambda _conn, *, version_id, query_text, entity_type=None, limit: [
            {
                "entity_id": "ENT-9",
                "entity_key": "theme-updates",
                "label": "Theme updates",
                "entity_type": "concept",
                "mention_count": 3,
                "aliases": ["Theme updates"],
                "source_documents": ["DOC-1"],
                "source_keys": ["docs/guide.md"],
            }
        ]
    )
    store._explore_neighbor_entities = (  # type: ignore[method-assign]
        lambda _conn, *, version_id, seed_entity_ids, entity_type, minimum_relationship_weight, limit: []
    )
    store._explore_relationships = (  # type: ignore[method-assign]
        lambda _conn, *, version_id, entity_ids, minimum_relationship_weight: []
    )

    result = store.explore(
        version_id="KBV-1",
        query="theme updates owner",
        query_entity_keys=["ignored"],
        entity_type=None,
        minimum_relationship_weight=1.0,
        traversal_hops=0,
        seed_mode="query_text_only",
        node_limit=6,
    )

    assert result.status == "ok"
    assert result.seed_strategy == "query_text"
    assert [entity.label for entity in result.entities] == ["Theme updates"]
    assert result.matched_entities == ("Theme updates",)
