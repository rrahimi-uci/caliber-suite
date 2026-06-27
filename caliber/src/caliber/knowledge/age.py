"""Apache AGE sync and retrieval helpers for knowledge bases."""

from __future__ import annotations

import re
from collections import defaultdict
from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Any, Literal

from sqlalchemy import text
from sqlalchemy.engine import Connection, Engine
from sqlalchemy.orm import Session, sessionmaker

from caliber.mcp_servers.db.identifiers import (
    assert_cypher_body_safe,
    compose_cypher_columns,
    cypher_map,
    cypher_value,
    parse_agtype,
    validate_identifier,
)

AgeRetrievalStrength = Literal["conservative", "balanced", "aggressive"]
AgeSeedMode = Literal[
    "entity_then_text",
    "query_entities_only",
    "query_text_only",
    "query_entities_and_text",
]
AgeSeedStrategy = Literal["query_entities", "query_text", "query_entities_and_text"]

_NODE_BATCH_SIZE = 64
_EDGE_BATCH_SIZE = 96
_CHUNK_PREVIEW_LIMIT = 220
_RETRIEVAL_QUERY_CANDIDATE_LIMIT = 12
_AGE_TWO_HOPS = 2
_MIN_RELATIONSHIP_ENTITY_COUNT = 2
_MIN_RETRIEVAL_QUERY_TOKEN_LENGTH = 3
_RETRIEVAL_QUERY_STOPWORDS = frozenset(
    {
        "a",
        "an",
        "and",
        "about",
        "are",
        "can",
        "does",
        "for",
        "from",
        "how",
        "into",
        "its",
        "the",
        "their",
        "them",
        "this",
        "what",
        "when",
        "where",
        "which",
        "who",
        "why",
        "with",
        "your",
    }
)
_ONE_HOP_FACTORS: dict[AgeRetrievalStrength, float] = {
    "conservative": 0.18,
    "balanced": 0.35,
    "aggressive": 0.55,
}
_TWO_HOP_FACTORS: dict[AgeRetrievalStrength, float] = {
    "conservative": 0.08,
    "balanced": 0.14,
    "aggressive": 0.18,
}


def _merge_entity_match_rows(*batches: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    merged: dict[str, dict[str, Any]] = {}
    for batch in batches:
        for row in batch:
            entity_id = str(row.get("entity_id") or row.get("entity_key") or "").strip()
            if not entity_id or entity_id in merged:
                continue
            merged[entity_id] = row
    return list(merged.values())


def postgres_age_required_reason() -> str:
    """Return a user-facing explanation for AGE-unavailable deployments."""

    return (
        "Apache AGE requires the PostgreSQL+AGE stack. In local dev, start the suite "
        "with ./start.sh or point CALIBER_DATABASE_URL at a PostgreSQL database with "
        "AGE enabled."
    )


@dataclass(frozen=True)
class AgeSyncResult:
    status: Literal["synced", "failed"]
    graph_name: str
    node_count: int = 0
    edge_count: int = 0
    error: str | None = None


@dataclass(frozen=True)
class AgeChunkCandidate:
    chunk_id: str
    graph_score: float
    direct_score: float = 0.0
    one_hop_score: float = 0.0
    two_hop_score: float = 0.0
    matched_entities: tuple[str, ...] = ()
    expanded_entities: tuple[str, ...] = ()


@dataclass(frozen=True)
class AgeRetrievalResult:
    status: Literal["ok", "fallback"]
    graph_name: str
    chunk_candidates: list[AgeChunkCandidate] = field(default_factory=list)
    matched_entities: tuple[str, ...] = ()
    expanded_entities: tuple[str, ...] = ()
    fallback_reason: str | None = None
    traversal_hops: int = 0
    matched_chunk_count: int = 0
    seed_strategy: AgeSeedStrategy | None = None


@dataclass(frozen=True)
class AgeGraphEntity:
    entity_id: str
    entity_key: str
    label: str
    entity_type: str
    mention_count: int
    aliases: tuple[str, ...] = ()
    source_documents: tuple[str, ...] = ()
    source_keys: tuple[str, ...] = ()
    distance: int | None = None
    highlighted: bool = False


@dataclass(frozen=True)
class AgeGraphRelationship:
    relationship_id: str
    source_entity_id: str
    source_entity_key: str
    source_entity_label: str
    target_entity_id: str
    target_entity_key: str
    target_entity_label: str
    relationship_type: str
    weight: float
    evidence_chunk_ids: tuple[str, ...] = ()
    source_documents: tuple[str, ...] = ()
    hop_distance: int | None = None


@dataclass(frozen=True)
class AgeGraphExploreResult:
    status: Literal["ok", "fallback"]
    graph_name: str
    entities: list[AgeGraphEntity] = field(default_factory=list)
    relationships: list[AgeGraphRelationship] = field(default_factory=list)
    matched_entities: tuple[str, ...] = ()
    expanded_entities: tuple[str, ...] = ()
    fallback_reason: str | None = None
    seed_strategy: AgeSeedStrategy | None = None


@dataclass(frozen=True)
class AgeDocumentNode:
    document_id: str
    object_key: str
    object_name: str
    object_store_path: str


@dataclass
class _AgeChunkScoreState:
    chunk_scores: defaultdict[str, float] = field(default_factory=lambda: defaultdict(float))
    chunk_labels: defaultdict[str, set[str]] = field(default_factory=lambda: defaultdict(set))
    chunk_expanded_labels: defaultdict[str, set[str]] = field(
        default_factory=lambda: defaultdict(set)
    )
    direct_scores: defaultdict[str, float] = field(default_factory=lambda: defaultdict(float))
    one_hop_scores: defaultdict[str, float] = field(default_factory=lambda: defaultdict(float))
    two_hop_scores: defaultdict[str, float] = field(default_factory=lambda: defaultdict(float))


def _select_age_seed_rows(
    seed_mode: AgeSeedMode,
    entity_matches: list[dict[str, Any]],
    text_matches: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], AgeSeedStrategy | None]:
    matched: list[dict[str, Any]]
    seed_strategy: AgeSeedStrategy | None = None
    if seed_mode == "query_entities_only":
        matched = entity_matches
        if entity_matches:
            seed_strategy = "query_entities"
    elif seed_mode == "query_text_only":
        matched = text_matches
        if text_matches:
            seed_strategy = "query_text"
    elif seed_mode == "query_entities_and_text":
        matched = _merge_entity_match_rows(entity_matches, text_matches)
        if entity_matches and text_matches:
            seed_strategy = "query_entities_and_text"
        elif entity_matches:
            seed_strategy = "query_entities"
        elif text_matches:
            seed_strategy = "query_text"
    elif entity_matches:
        matched = entity_matches
        seed_strategy = "query_entities"
    else:
        matched = text_matches
        if text_matches:
            seed_strategy = "query_text"
    return matched, seed_strategy


def _retrieval_seed_miss_reason(
    seed_mode: AgeSeedMode,
    *,
    has_keys: bool,
    has_query: bool,
) -> str:
    if seed_mode == "query_entities_only":
        return "No Apache AGE entities matched the question entities for this version."
    if seed_mode == "query_text_only":
        return "No Apache AGE entities matched the question text for this version."
    if has_keys and has_query:
        return "No Apache AGE entities matched the question entities or text for this version."
    if has_keys:
        return "No Apache AGE entities matched the question entities for this version."
    return "No Apache AGE entities matched the question text for this version."


class ApacheAgeKnowledgeStore:
    """Writes KB graph lineage into Apache AGE and queries it back with Cypher."""

    def __init__(
        self,
        *,
        session_factory: sessionmaker[Session],
        enabled: bool,
        graph_name: str,
    ) -> None:
        self._session_factory = session_factory
        self._enabled = bool(enabled)
        self._graph_name = validate_identifier(graph_name, kind="graph")
        bind = session_factory.kw.get("bind")
        self._engine = bind if isinstance(bind, Engine) else None

    @property
    def graph_name(self) -> str:
        return self._graph_name

    @property
    def available(self) -> bool:
        return bool(
            self._enabled and self._engine is not None and self._engine.dialect.name == "postgresql"
        )

    @property
    def unavailable_reason(self) -> str | None:
        if self.available:
            return None
        if not self._enabled:
            return "Apache AGE is disabled by configuration."
        return postgres_age_required_reason()

    def sync_version(
        self,
        *,
        knowledge_base_id: str,
        knowledge_base_name: str,
        version_id: str,
        version_number: int,
        source_bucket: str,
        documents: list[AgeDocumentNode],
        chunks: list[dict[str, Any]],
        entities: list[dict[str, Any]],
        relationships: list[dict[str, Any]],
    ) -> AgeSyncResult:
        if not self._enabled:
            return AgeSyncResult(
                status="failed",
                graph_name=self._graph_name,
                error="Apache AGE sync is disabled by configuration.",
            )
        if self._engine is None or self._engine.dialect.name != "postgresql":
            return AgeSyncResult(
                status="failed",
                graph_name=self._graph_name,
                error=postgres_age_required_reason(),
            )

        try:
            with self._engine.begin() as conn:
                self._prepare(conn)
                self._ensure_graph(conn)
                self._delete_version_subgraph(conn, version_id=version_id)
                self._create_version_node(
                    conn,
                    knowledge_base_id=knowledge_base_id,
                    knowledge_base_name=knowledge_base_name,
                    version_id=version_id,
                    version_number=version_number,
                    source_bucket=source_bucket,
                )
                self._create_document_nodes(
                    conn,
                    version_id=version_id,
                    knowledge_base_id=knowledge_base_id,
                    documents=documents,
                )
                self._create_chunk_nodes(
                    conn, version_id=version_id, knowledge_base_id=knowledge_base_id, chunks=chunks
                )
                self._create_entity_nodes(
                    conn,
                    version_id=version_id,
                    knowledge_base_id=knowledge_base_id,
                    entities=entities,
                )
                self._create_version_edges(
                    conn, version_id=version_id, documents=documents, entities=entities
                )
                self._create_chunk_edges(conn, version_id=version_id, chunks=chunks)
                self._create_entity_chunk_edges(conn, version_id=version_id, entities=entities)
                self._create_relationship_edges(
                    conn, version_id=version_id, relationships=relationships
                )
        except Exception as exc:
            return AgeSyncResult(
                status="failed",
                graph_name=self._graph_name,
                error=f"{type(exc).__name__}: {exc}",
            )

        mention_edges = sum(len(_as_list(item.get("source_chunks"))) for item in entities)
        node_count = 1 + len(documents) + len(chunks) + len(entities)
        edge_count = (
            len(documents) + len(chunks) + len(entities) + mention_edges + len(relationships)
        )
        return AgeSyncResult(
            status="synced",
            graph_name=self._graph_name,
            node_count=node_count,
            edge_count=edge_count,
        )

    def drop_version(self, *, version_id: str) -> bool:
        """Best-effort removal of one version's nodes/edges from the AGE graph.

        ``DETACH DELETE`` every node tagged with ``version_id`` (the same scope
        :meth:`sync_version` clears before re-syncing). Returns ``True`` when a
        drop ran, ``False`` when AGE is unavailable (disabled or non-PostgreSQL).
        Raises only on an actual database failure, so the caller can wrap this in
        its own try/except and keep the relational delete authoritative.
        """
        if not self.available or self._engine is None:
            return False
        with self._engine.begin() as conn:
            self._prepare(conn)
            self._ensure_graph(conn)
            self._delete_version_subgraph(conn, version_id=version_id)
        return True

    def retrieve(
        self,
        *,
        version_id: str,
        query_entity_keys: list[str],
        retrieval_strength: AgeRetrievalStrength,
        minimum_relationship_weight: float,
        top_k: int,
        traversal_hops: int,
        candidate_pool_size: int,
        seed_mode: AgeSeedMode = "entity_then_text",
        query_text: str | None = None,
    ) -> AgeRetrievalResult:
        if not self._enabled:
            return AgeRetrievalResult(
                status="fallback",
                graph_name=self._graph_name,
                fallback_reason="Apache AGE retrieval is disabled by configuration.",
            )
        if self._engine is None or self._engine.dialect.name != "postgresql":
            return AgeRetrievalResult(
                status="fallback",
                graph_name=self._graph_name,
                fallback_reason=postgres_age_required_reason(),
            )
        keys = [item for item in query_entity_keys if item]
        normalized_query = _normalize_retrieval_query(query_text)
        if not keys and not normalized_query:
            return AgeRetrievalResult(
                status="fallback",
                graph_name=self._graph_name,
                fallback_reason="The question did not produce any graph entities to match.",
            )
        max_hops = max(0, min(int(traversal_hops), 2))
        candidate_limit = max(top_k, int(candidate_pool_size))

        try:
            with self._engine.connect() as conn:
                self._prepare(conn)
                return self._retrieve_from_connection(
                    conn,
                    version_id=version_id,
                    keys=keys,
                    normalized_query=normalized_query,
                    seed_mode=seed_mode,
                    retrieval_strength=retrieval_strength,
                    minimum_relationship_weight=minimum_relationship_weight,
                    candidate_limit=candidate_limit,
                    max_hops=max_hops,
                )
        except Exception as exc:
            return AgeRetrievalResult(
                status="fallback",
                graph_name=self._graph_name,
                fallback_reason=f"Apache AGE query failed: {type(exc).__name__}: {exc}",
            )

    def _retrieve_from_connection(
        self,
        conn: Connection,
        *,
        version_id: str,
        keys: list[str],
        normalized_query: str,
        seed_mode: AgeSeedMode,
        retrieval_strength: AgeRetrievalStrength,
        minimum_relationship_weight: float,
        candidate_limit: int,
        max_hops: int,
    ) -> AgeRetrievalResult:
        matched, seed_strategy, fallback_reason = self._resolve_retrieval_seed_rows(
            conn,
            version_id=version_id,
            keys=keys,
            normalized_query=normalized_query,
            seed_mode=seed_mode,
        )
        if fallback_reason is not None:
            return AgeRetrievalResult(
                status="fallback",
                graph_name=self._graph_name,
                fallback_reason=fallback_reason,
                seed_strategy=seed_strategy,
            )

        matched_entity_ids = [str(row["entity_id"]) for row in matched if row.get("entity_id")]
        score_state, hops = self._collect_chunk_scores(
            conn,
            version_id=version_id,
            matched_entity_ids=matched_entity_ids,
            retrieval_strength=retrieval_strength,
            minimum_relationship_weight=minimum_relationship_weight,
            max_hops=max_hops,
        )
        matched_labels = tuple(str(row.get("label") or "") for row in matched if row.get("label"))
        if not score_state.chunk_scores:
            return AgeRetrievalResult(
                status="fallback",
                graph_name=self._graph_name,
                fallback_reason=(
                    "Apache AGE did not return any chunk lineage for the matched entities."
                ),
                matched_entities=matched_labels,
            )

        candidates, expanded_labels = self._build_chunk_candidates(
            score_state,
            candidate_limit=candidate_limit,
        )
        return AgeRetrievalResult(
            status="ok",
            graph_name=self._graph_name,
            chunk_candidates=candidates,
            matched_entities=matched_labels,
            expanded_entities=expanded_labels,
            traversal_hops=hops,
            matched_chunk_count=len(score_state.chunk_scores),
            seed_strategy=seed_strategy,
        )

    def _resolve_retrieval_seed_rows(
        self,
        conn: Connection,
        *,
        version_id: str,
        keys: list[str],
        normalized_query: str,
        seed_mode: AgeSeedMode,
    ) -> tuple[list[dict[str, Any]], AgeSeedStrategy | None, str | None]:
        entity_matches = (
            self._match_entities(
                conn,
                version_id=version_id,
                query_entity_keys=keys,
            )
            if seed_mode != "query_text_only" and keys
            else []
        )
        text_matches = (
            self._match_entities_from_query_text(
                conn,
                version_id=version_id,
                query_text=normalized_query,
                limit=8,
            )
            if seed_mode != "query_entities_only" and normalized_query
            else []
        )
        matched, seed_strategy = _select_age_seed_rows(
            seed_mode,
            entity_matches,
            text_matches,
        )
        if matched:
            return matched, seed_strategy, None
        return (
            [],
            seed_strategy,
            _retrieval_seed_miss_reason(
                seed_mode,
                has_keys=bool(keys),
                has_query=bool(normalized_query),
            ),
        )

    def _collect_chunk_scores(
        self,
        conn: Connection,
        *,
        version_id: str,
        matched_entity_ids: list[str],
        retrieval_strength: AgeRetrievalStrength,
        minimum_relationship_weight: float,
        max_hops: int,
    ) -> tuple[_AgeChunkScoreState, int]:
        state = _AgeChunkScoreState()
        direct_rows = self._direct_chunk_matches(
            conn,
            version_id=version_id,
            matched_entity_ids=matched_entity_ids,
        )
        self._accumulate_chunk_rows(
            direct_rows,
            factor=1.0,
            chunk_scores=state.chunk_scores,
            chunk_labels=state.chunk_labels,
            chunk_expanded_labels=state.chunk_expanded_labels,
            hop_scores=state.direct_scores,
        )
        hops = 0
        if max_hops >= 1:
            hops = 1
            one_hop_rows = self._neighbor_chunk_matches(
                conn,
                version_id=version_id,
                matched_entity_ids=matched_entity_ids,
                minimum_relationship_weight=minimum_relationship_weight,
            )
            self._accumulate_chunk_rows(
                one_hop_rows,
                factor=_ONE_HOP_FACTORS[retrieval_strength],
                chunk_scores=state.chunk_scores,
                chunk_labels=state.chunk_labels,
                chunk_expanded_labels=state.chunk_expanded_labels,
                hop_scores=state.one_hop_scores,
            )
        if max_hops >= _AGE_TWO_HOPS:
            hops = _AGE_TWO_HOPS
            two_hop_rows = self._two_hop_chunk_matches(
                conn,
                version_id=version_id,
                matched_entity_ids=matched_entity_ids,
                minimum_relationship_weight=minimum_relationship_weight,
            )
            self._accumulate_chunk_rows(
                two_hop_rows,
                factor=_TWO_HOP_FACTORS[retrieval_strength],
                chunk_scores=state.chunk_scores,
                chunk_labels=state.chunk_labels,
                chunk_expanded_labels=state.chunk_expanded_labels,
                hop_scores=state.two_hop_scores,
            )
        return state, hops

    def _build_chunk_candidates(
        self,
        state: _AgeChunkScoreState,
        *,
        candidate_limit: int,
    ) -> tuple[list[AgeChunkCandidate], tuple[str, ...]]:
        ranked = sorted(
            state.chunk_scores.items(),
            key=lambda item: item[1],
            reverse=True,
        )[:candidate_limit]
        expanded_labels = tuple(
            sorted({label for labels in state.chunk_expanded_labels.values() for label in labels})
        )
        candidates = [
            AgeChunkCandidate(
                chunk_id=chunk_id,
                graph_score=score,
                direct_score=state.direct_scores.get(chunk_id, 0.0),
                one_hop_score=state.one_hop_scores.get(chunk_id, 0.0),
                two_hop_score=state.two_hop_scores.get(chunk_id, 0.0),
                matched_entities=tuple(sorted(state.chunk_labels.get(chunk_id, set()))),
                expanded_entities=tuple(sorted(state.chunk_expanded_labels.get(chunk_id, set()))),
            )
            for chunk_id, score in ranked
        ]
        return candidates, expanded_labels

    def explore(
        self,
        *,
        version_id: str,
        query: str = "",
        query_entity_keys: list[str] | None = None,
        entity_type: str | None = None,
        minimum_relationship_weight: float = 0.0,
        traversal_hops: int = 1,
        seed_mode: AgeSeedMode = "entity_then_text",
        node_limit: int = 12,
    ) -> AgeGraphExploreResult:
        if not self._enabled:
            return AgeGraphExploreResult(
                status="fallback",
                graph_name=self._graph_name,
                fallback_reason="Apache AGE graph exploration is disabled by configuration.",
            )
        if self._engine is None or self._engine.dialect.name != "postgresql":
            return AgeGraphExploreResult(
                status="fallback",
                graph_name=self._graph_name,
                fallback_reason=postgres_age_required_reason(),
            )

        raw_query = query.strip()
        normalized_query = _normalize_retrieval_query(raw_query)
        keys = [item for item in (query_entity_keys or []) if item]
        normalized_type = (entity_type or "").strip().lower() or None
        max_hops = max(0, min(int(traversal_hops), 2))
        max_nodes = max(4, min(int(node_limit), 48))
        seed_limit = max(1, min(max_nodes, 8 if normalized_query else max_nodes))

        try:
            with self._engine.connect() as conn:
                self._prepare(conn)
                return self._explore_from_connection(
                    conn,
                    version_id=version_id,
                    raw_query=raw_query,
                    normalized_query=normalized_query,
                    keys=keys,
                    normalized_type=normalized_type,
                    minimum_relationship_weight=minimum_relationship_weight,
                    max_hops=max_hops,
                    seed_mode=seed_mode,
                    max_nodes=max_nodes,
                    seed_limit=seed_limit,
                )
        except Exception as exc:
            return AgeGraphExploreResult(
                status="fallback",
                graph_name=self._graph_name,
                fallback_reason=f"Apache AGE graph exploration failed: {type(exc).__name__}: {exc}",
            )

    def _explore_from_connection(
        self,
        conn: Connection,
        *,
        version_id: str,
        raw_query: str,
        normalized_query: str,
        keys: list[str],
        normalized_type: str | None,
        minimum_relationship_weight: float,
        max_hops: int,
        seed_mode: AgeSeedMode,
        max_nodes: int,
        seed_limit: int,
    ) -> AgeGraphExploreResult:
        seed_rows, seed_strategy, use_query_matchers = self._resolve_explore_seed_rows(
            conn,
            version_id=version_id,
            raw_query=raw_query,
            normalized_query=normalized_query,
            keys=keys,
            normalized_type=normalized_type,
            seed_mode=seed_mode,
            seed_limit=seed_limit,
        )
        if not seed_rows and normalized_query:
            seed_rows = self._explore_seed_entities(
                conn,
                version_id=version_id,
                query=normalized_query,
                entity_type=normalized_type,
                limit=seed_limit,
            )
            if seed_rows and seed_strategy is None:
                seed_strategy = "query_text"
        if not seed_rows:
            return AgeGraphExploreResult(
                status="ok",
                graph_name=self._graph_name,
                fallback_reason=(
                    "No Apache AGE entities matched the current graph filters."
                    if use_query_matchers or normalized_query or keys
                    else None
                ),
                seed_strategy=seed_strategy,
            )

        entities_by_id, distances = self._expand_explore_entities(
            conn,
            version_id=version_id,
            seed_rows=seed_rows,
            normalized_type=normalized_type,
            minimum_relationship_weight=minimum_relationship_weight,
            max_hops=max_hops,
            max_nodes=max_nodes,
        )
        graph_relationships = self._graph_relationships_for_entities(
            conn,
            version_id=version_id,
            entity_ids=list(entities_by_id),
            minimum_relationship_weight=minimum_relationship_weight,
            distances=distances,
        )
        ordered_entities = self._ordered_graph_entities(entities_by_id)
        matched, expanded = self._graph_entity_labels(ordered_entities)
        return AgeGraphExploreResult(
            status="ok",
            graph_name=self._graph_name,
            entities=ordered_entities,
            relationships=graph_relationships,
            matched_entities=matched,
            expanded_entities=expanded,
            seed_strategy=seed_strategy,
        )

    def _resolve_explore_seed_rows(
        self,
        conn: Connection,
        *,
        version_id: str,
        raw_query: str,
        normalized_query: str,
        keys: list[str],
        normalized_type: str | None,
        seed_mode: AgeSeedMode,
        seed_limit: int,
    ) -> tuple[list[dict[str, Any]], AgeSeedStrategy | None, bool]:
        use_query_matchers = bool(keys) or seed_mode != "entity_then_text"
        if not use_query_matchers:
            return (
                self._explore_seed_entities(
                    conn,
                    version_id=version_id,
                    query=raw_query.lower(),
                    entity_type=normalized_type,
                    limit=seed_limit,
                ),
                None,
                False,
            )

        entity_matches = (
            self._match_entities(
                conn,
                version_id=version_id,
                query_entity_keys=keys,
                entity_type=normalized_type,
            )
            if seed_mode != "query_text_only" and keys
            else []
        )
        text_matches = (
            self._match_entities_from_query_text(
                conn,
                version_id=version_id,
                query_text=normalized_query,
                entity_type=normalized_type,
                limit=seed_limit,
            )
            if seed_mode != "query_entities_only" and normalized_query
            else []
        )
        seed_rows, seed_strategy = _select_age_seed_rows(
            seed_mode,
            entity_matches,
            text_matches,
        )
        return seed_rows, seed_strategy, True

    def _expand_explore_entities(
        self,
        conn: Connection,
        *,
        version_id: str,
        seed_rows: list[dict[str, Any]],
        normalized_type: str | None,
        minimum_relationship_weight: float,
        max_hops: int,
        max_nodes: int,
    ) -> tuple[dict[str, AgeGraphEntity], dict[str, int]]:
        entities_by_id: dict[str, AgeGraphEntity] = {}
        distances: dict[str, int] = {}
        frontier: list[str] = []
        for row in seed_rows:
            entity = self._graph_entity_from_row(row, distance=0, highlighted=True)
            entities_by_id[entity.entity_id] = entity
            distances[entity.entity_id] = 0
            frontier.append(entity.entity_id)

        for hop in range(1, max_hops + 1):
            if not frontier or len(entities_by_id) >= max_nodes:
                break
            frontier = self._next_explore_frontier(
                conn,
                version_id=version_id,
                frontier=frontier,
                hop=hop,
                normalized_type=normalized_type,
                minimum_relationship_weight=minimum_relationship_weight,
                max_nodes=max_nodes,
                entities_by_id=entities_by_id,
                distances=distances,
            )
        return entities_by_id, distances

    def _next_explore_frontier(
        self,
        conn: Connection,
        *,
        version_id: str,
        frontier: list[str],
        hop: int,
        normalized_type: str | None,
        minimum_relationship_weight: float,
        max_nodes: int,
        entities_by_id: dict[str, AgeGraphEntity],
        distances: dict[str, int],
    ) -> list[str]:
        neighbor_rows = self._explore_neighbor_entities(
            conn,
            version_id=version_id,
            seed_entity_ids=frontier,
            entity_type=normalized_type,
            minimum_relationship_weight=minimum_relationship_weight,
            limit=max(max_nodes * 3, 18),
        )
        next_frontier: list[str] = []
        for row in neighbor_rows:
            entity_id = str(row.get("entity_id") or "")
            if not entity_id or entity_id in entities_by_id:
                continue
            entity = self._graph_entity_from_row(row, distance=hop, highlighted=False)
            entities_by_id[entity.entity_id] = entity
            distances[entity.entity_id] = hop
            next_frontier.append(entity.entity_id)
            if len(entities_by_id) >= max_nodes:
                break
        return next_frontier

    def _graph_relationships_for_entities(
        self,
        conn: Connection,
        *,
        version_id: str,
        entity_ids: list[str],
        minimum_relationship_weight: float,
        distances: dict[str, int],
    ) -> list[AgeGraphRelationship]:
        relationships = self._explore_relationships(
            conn,
            version_id=version_id,
            entity_ids=entity_ids,
            minimum_relationship_weight=minimum_relationship_weight,
        )
        return [
            AgeGraphRelationship(
                relationship_id=str(row.get("relationship_id") or ""),
                source_entity_id=str(row.get("source_entity_id") or ""),
                source_entity_key=str(row.get("source_entity_key") or ""),
                source_entity_label=str(row.get("source_entity_label") or ""),
                target_entity_id=str(row.get("target_entity_id") or ""),
                target_entity_key=str(row.get("target_entity_key") or ""),
                target_entity_label=str(row.get("target_entity_label") or ""),
                relationship_type=str(row.get("relationship_type") or "related"),
                weight=float(row.get("weight") or 0.0),
                evidence_chunk_ids=tuple(
                    str(item) for item in _as_list(row.get("evidence_chunk_ids")) if item
                ),
                source_documents=tuple(
                    str(item) for item in _as_list(row.get("source_documents")) if item
                ),
                hop_distance=self._relationship_hop_distance(
                    row,
                    distances=distances,
                ),
            )
            for row in relationships
        ]

    def _ordered_graph_entities(
        self,
        entities_by_id: dict[str, AgeGraphEntity],
    ) -> list[AgeGraphEntity]:
        return sorted(
            entities_by_id.values(),
            key=lambda item: (
                item.distance if item.distance is not None else 99,
                -item.mention_count,
                item.label.lower(),
                item.entity_key,
            ),
        )

    def _graph_entity_labels(
        self,
        ordered_entities: list[AgeGraphEntity],
    ) -> tuple[tuple[str, ...], tuple[str, ...]]:
        matched = tuple(entity.label for entity in ordered_entities if entity.distance == 0)
        expanded = tuple(
            entity.label
            for entity in ordered_entities
            if entity.distance is not None and entity.distance > 0
        )
        return matched, expanded

    def _prepare(self, conn: Connection) -> None:
        conn.exec_driver_sql('SET search_path = ag_catalog, "$user", public')

    def _ensure_graph(self, conn: Connection) -> None:
        rows = list(
            conn.execute(
                text("SELECT name FROM ag_graph WHERE name = :name"), {"name": self._graph_name}
            ).mappings()
        )
        if rows:
            return
        conn.exec_driver_sql(f"SELECT create_graph('{self._graph_name}')")

    def _delete_version_subgraph(self, conn: Connection, *, version_id: str) -> None:
        body = f"MATCH (n {{version_id: {cypher_value(version_id)}}}) DETACH DELETE n"
        self._run_cypher(conn, body)

    def _create_version_node(
        self,
        conn: Connection,
        *,
        knowledge_base_id: str,
        knowledge_base_name: str,
        version_id: str,
        version_number: int,
        source_bucket: str,
    ) -> None:
        props = {
            "knowledge_base_id": knowledge_base_id,
            "knowledge_base_name": knowledge_base_name,
            "version_id": version_id,
            "version_number": version_number,
            "source_bucket": source_bucket,
        }
        self._run_cypher(conn, f"CREATE (:KBVersion {cypher_map(props)})")

    def _create_document_nodes(
        self,
        conn: Connection,
        *,
        version_id: str,
        knowledge_base_id: str,
        documents: list[AgeDocumentNode],
    ) -> None:
        props = [
            {
                "version_id": version_id,
                "knowledge_base_id": knowledge_base_id,
                "document_id": item.document_id,
                "object_key": item.object_key,
                "object_name": item.object_name,
                "object_store_path": item.object_store_path,
            }
            for item in documents
        ]
        self._batch_create_nodes(conn, "KBDocument", props)

    def _create_chunk_nodes(
        self,
        conn: Connection,
        *,
        version_id: str,
        knowledge_base_id: str,
        chunks: list[dict[str, Any]],
    ) -> None:
        props = [
            {
                "version_id": version_id,
                "knowledge_base_id": knowledge_base_id,
                "chunk_id": str(item.get("chunk_id") or ""),
                "document_id": str(item.get("document_id") or ""),
                "source_key": str(item.get("source_key") or ""),
                "source_name": str(item.get("source_name") or ""),
                "ordinal": int(item.get("ordinal") or 0),
                "chunk_index": int(item.get("chunk_index") or 0),
                "content_preview": _preview_text(str(item.get("content") or "")),
            }
            for item in chunks
            if item.get("chunk_id")
        ]
        self._batch_create_nodes(conn, "KBChunk", props)

    def _create_entity_nodes(
        self,
        conn: Connection,
        *,
        version_id: str,
        knowledge_base_id: str,
        entities: list[dict[str, Any]],
    ) -> None:
        props = [
            {
                "version_id": version_id,
                "knowledge_base_id": knowledge_base_id,
                "entity_id": str(item.get("entity_id") or ""),
                "entity_key": str(item.get("entity_key") or ""),
                "label": str(item.get("label") or ""),
                "entity_type": str(item.get("entity_type") or ""),
                "mention_count": int(item.get("mention_count") or 0),
                "aliases": [str(alias) for alias in _as_list(item.get("aliases")) if alias],
                "source_documents": [
                    str(doc) for doc in _as_list(item.get("source_documents")) if doc
                ],
                "source_keys": [str(key) for key in _as_list(item.get("source_keys")) if key],
            }
            for item in entities
            if item.get("entity_id")
        ]
        self._batch_create_nodes(conn, "KBEntity", props)

    def _create_version_edges(
        self,
        conn: Connection,
        *,
        version_id: str,
        documents: list[AgeDocumentNode],
        entities: list[dict[str, Any]],
    ) -> None:
        document_bodies = [
            (
                f"MATCH (v:KBVersion {{version_id: {cypher_value(version_id)}}}) "
                "WITH v "
                f"MATCH (d:KBDocument {{version_id: {cypher_value(version_id)}, document_id: {cypher_value(item.document_id)}}}) "
                "CREATE (v)-[:HAS_DOCUMENT]->(d)"
            )
            for item in documents
        ]
        self._batch_run_cypher(conn, document_bodies, batch_size=_EDGE_BATCH_SIZE)
        entity_bodies = [
            (
                f"MATCH (v:KBVersion {{version_id: {cypher_value(version_id)}}}) "
                "WITH v "
                f"MATCH (e:KBEntity {{version_id: {cypher_value(version_id)}, entity_id: {cypher_value(str(item.get('entity_id') or ''))}}}) "
                "CREATE (v)-[:HAS_ENTITY]->(e)"
            )
            for item in entities
            if item.get("entity_id")
        ]
        self._batch_run_cypher(conn, entity_bodies, batch_size=_EDGE_BATCH_SIZE)

    def _create_chunk_edges(
        self,
        conn: Connection,
        *,
        version_id: str,
        chunks: list[dict[str, Any]],
    ) -> None:
        bodies = [
            (
                f"MATCH (d:KBDocument {{version_id: {cypher_value(version_id)}, document_id: {cypher_value(str(item.get('document_id') or ''))}}}) "
                "WITH d "
                f"MATCH (c:KBChunk {{version_id: {cypher_value(version_id)}, chunk_id: {cypher_value(str(item.get('chunk_id') or ''))}}}) "
                "CREATE (d)-[:HAS_CHUNK]->(c)"
            )
            for item in chunks
            if item.get("chunk_id") and item.get("document_id")
        ]
        self._batch_run_cypher(conn, bodies, batch_size=_EDGE_BATCH_SIZE)

    def _create_entity_chunk_edges(
        self,
        conn: Connection,
        *,
        version_id: str,
        entities: list[dict[str, Any]],
    ) -> None:
        bodies: list[str] = []
        for item in entities:
            entity_id = str(item.get("entity_id") or "")
            if not entity_id:
                continue
            for chunk_id in _as_list(item.get("source_chunks")):
                if not chunk_id:
                    continue
                bodies.append(
                    f"MATCH (e:KBEntity {{version_id: {cypher_value(version_id)}, entity_id: {cypher_value(entity_id)}}}) "
                    "WITH e "
                    f"MATCH (c:KBChunk {{version_id: {cypher_value(version_id)}, chunk_id: {cypher_value(str(chunk_id))}}}) "
                    "CREATE (e)-[:MENTIONED_IN]->(c)"
                )
        self._batch_run_cypher(conn, bodies, batch_size=_EDGE_BATCH_SIZE)

    def _create_relationship_edges(
        self,
        conn: Connection,
        *,
        version_id: str,
        relationships: list[dict[str, Any]],
    ) -> None:
        bodies = [
            (
                f"MATCH (s:KBEntity {{version_id: {cypher_value(version_id)}, entity_id: {cypher_value(str(item.get('source_entity_id') or ''))}}}) "
                "WITH s "
                f"MATCH (t:KBEntity {{version_id: {cypher_value(version_id)}, entity_id: {cypher_value(str(item.get('target_entity_id') or ''))}}}) "
                f"CREATE (s)-[:RELATED {cypher_map({'relationship_id': str(item.get('relationship_id') or ''), 'relationship_type': str(item.get('relationship_type') or 'related'), 'weight': float(item.get('weight') or 0.0), 'evidence_chunk_ids': [str(chunk_id) for chunk_id in _as_list(item.get('evidence_chunk_ids')) if chunk_id], 'source_documents': [str(document_id) for document_id in _as_list(item.get('source_documents')) if document_id]})}]->(t)"
            )
            for item in relationships
            if item.get("source_entity_id") and item.get("target_entity_id")
        ]
        self._batch_run_cypher(conn, bodies, batch_size=_EDGE_BATCH_SIZE)

    def _match_entities(
        self,
        conn: Connection,
        *,
        version_id: str,
        query_entity_keys: list[str],
        entity_type: str | None = None,
    ) -> list[dict[str, Any]]:
        query_keys = _cypher_list(query_entity_keys)
        type_clause = (
            f" AND toLower(e.entity_type) = {cypher_value(entity_type)}" if entity_type else ""
        )
        body = (
            f"MATCH (e:KBEntity {{version_id: {cypher_value(version_id)}}}) "
            f"WHERE e.entity_key IN {query_keys}{type_clause} "
            "RETURN e.entity_id AS entity_id, e.label AS label, e.entity_key AS entity_key, e.mention_count AS mention_count "
            "ORDER BY e.mention_count DESC LIMIT 8"
        )
        return self._query_cypher(
            conn,
            body,
            columns=["entity_id", "label", "entity_key", "mention_count"],
        )

    def _match_entities_from_query_text(
        self,
        conn: Connection,
        *,
        version_id: str,
        query_text: str,
        entity_type: str | None = None,
        limit: int,
    ) -> list[dict[str, Any]]:
        candidates = _retrieval_query_candidates(query_text)
        if not candidates:
            return []
        property_clauses: list[str] = []
        alias_clauses: list[str] = []
        for candidate in candidates:
            literal = cypher_value(candidate)
            property_clauses.extend(
                [
                    f"toLower(e.label) CONTAINS {literal}",
                    f"toLower(e.entity_key) CONTAINS {literal}",
                ]
            )
            alias_clauses.append(f"toLower(alias) CONTAINS {literal}")
        type_clause = (
            f" AND toLower(e.entity_type) = {cypher_value(entity_type)}" if entity_type else ""
        )
        row_limit = max(limit * 4, 16)
        rows: list[dict[str, Any]] = []
        if property_clauses:
            property_body = (
                f"MATCH (e:KBEntity {{version_id: {cypher_value(version_id)}}}) "
                f"WHERE ({' OR '.join(property_clauses)}){type_clause} "
                "RETURN e.entity_id AS entity_id, e.label AS label, e.entity_key AS entity_key, "
                "e.mention_count AS mention_count, coalesce(e.aliases, []) AS aliases "
                f"ORDER BY e.mention_count DESC, e.label ASC LIMIT {row_limit}"
            )
            rows.extend(
                self._query_cypher(
                    conn,
                    property_body,
                    columns=["entity_id", "label", "entity_key", "mention_count", "aliases"],
                )
            )
        if alias_clauses:
            # Apache AGE does not support Neo4j-style list predicates such as
            # `any(alias IN aliases WHERE ...)`, so alias matching is expanded
            # through UNWIND + WITH + WHERE instead.
            alias_body = (
                f"MATCH (e:KBEntity {{version_id: {cypher_value(version_id)}}}) "
                "UNWIND coalesce(e.aliases, []) AS alias "
                "WITH e, alias "
                f"WHERE ({' OR '.join(alias_clauses)}){type_clause} "
                "RETURN DISTINCT e.entity_id AS entity_id, e.label AS label, e.entity_key AS entity_key, "
                "e.mention_count AS mention_count, coalesce(e.aliases, []) AS aliases "
                f"ORDER BY e.mention_count DESC, e.label ASC LIMIT {row_limit}"
            )
            rows = _merge_entity_match_rows(
                rows,
                self._query_cypher(
                    conn,
                    alias_body,
                    columns=["entity_id", "label", "entity_key", "mention_count", "aliases"],
                ),
            )
        scored: list[tuple[int, int, int, int, str, dict[str, Any]]] = []
        for row in rows:
            matches = _matched_query_candidates(row, candidates)
            if not matches:
                continue
            scored.append(
                (
                    len(matches),
                    max(len(item.split()) for item in matches),
                    max(len(item) for item in matches),
                    int(row.get("mention_count") or 0),
                    str(row.get("label") or "").lower(),
                    row,
                )
            )
        scored.sort(key=lambda item: (-item[0], -item[1], -item[2], -item[3], item[4]))
        return [row for *_meta, row in scored[:limit]]

    def _direct_chunk_matches(
        self,
        conn: Connection,
        *,
        version_id: str,
        matched_entity_ids: list[str],
    ) -> list[dict[str, Any]]:
        entity_ids = _cypher_list(matched_entity_ids)
        body = (
            f"MATCH (e:KBEntity {{version_id: {cypher_value(version_id)}}})-[:MENTIONED_IN]->(c:KBChunk {{version_id: {cypher_value(version_id)}}}) "
            f"WHERE e.entity_id IN {entity_ids} "
            "RETURN c.chunk_id AS chunk_id, collect(DISTINCT e.label) AS matched_labels, [] AS expanded_labels, sum(coalesce(e.mention_count, 1)) AS graph_score"
        )
        return self._query_cypher(
            conn,
            body,
            columns=["chunk_id", "matched_labels", "expanded_labels", "graph_score"],
        )

    def _explore_seed_entities(
        self,
        conn: Connection,
        *,
        version_id: str,
        query: str,
        entity_type: str | None,
        limit: int,
    ) -> list[dict[str, Any]]:
        type_clause = f" AND e.entity_type = {cypher_value(entity_type)}" if entity_type else ""
        if query:
            body = (
                f"MATCH (e:KBEntity {{version_id: {cypher_value(version_id)}}}) "
                f"WHERE (toLower(e.label) CONTAINS {cypher_value(query)} "
                f"OR toLower(e.entity_key) CONTAINS {cypher_value(query)} "
                f"OR toLower(e.entity_type) CONTAINS {cypher_value(query)})"
                f"{type_clause} "
                "RETURN e.entity_id AS entity_id, e.entity_key AS entity_key, e.label AS label, "
                "e.entity_type AS entity_type, e.mention_count AS mention_count, "
                "coalesce(e.aliases, []) AS aliases, coalesce(e.source_documents, []) AS source_documents, "
                "coalesce(e.source_keys, []) AS source_keys "
                f"ORDER BY e.mention_count DESC, e.label ASC LIMIT {int(limit)}"
            )
        else:
            body = (
                f"MATCH (e:KBEntity {{version_id: {cypher_value(version_id)}}}) "
                f"WHERE true{type_clause} "
                "RETURN e.entity_id AS entity_id, e.entity_key AS entity_key, e.label AS label, "
                "e.entity_type AS entity_type, e.mention_count AS mention_count, "
                "coalesce(e.aliases, []) AS aliases, coalesce(e.source_documents, []) AS source_documents, "
                "coalesce(e.source_keys, []) AS source_keys "
                f"ORDER BY e.mention_count DESC, e.label ASC LIMIT {int(limit)}"
            )
        return self._query_cypher(
            conn,
            body,
            columns=[
                "entity_id",
                "entity_key",
                "label",
                "entity_type",
                "mention_count",
                "aliases",
                "source_documents",
                "source_keys",
            ],
        )

    def _explore_neighbor_entities(
        self,
        conn: Connection,
        *,
        version_id: str,
        seed_entity_ids: list[str],
        entity_type: str | None,
        minimum_relationship_weight: float,
        limit: int,
    ) -> list[dict[str, Any]]:
        if not seed_entity_ids:
            return []
        type_clause = f" AND n.entity_type = {cypher_value(entity_type)}" if entity_type else ""
        entity_ids = _cypher_list(seed_entity_ids)
        body = (
            f"MATCH (e:KBEntity {{version_id: {cypher_value(version_id)}}})-[rel:RELATED]-(n:KBEntity {{version_id: {cypher_value(version_id)}}}) "
            f"WHERE e.entity_id IN {entity_ids} "
            f"AND coalesce(rel.weight, 0) >= {float(minimum_relationship_weight)}"
            f"{type_clause} "
            "WITH n, max(coalesce(rel.weight, 0)) AS max_weight "
            "RETURN n.entity_id AS entity_id, n.entity_key AS entity_key, n.label AS label, "
            "n.entity_type AS entity_type, n.mention_count AS mention_count, "
            "coalesce(n.aliases, []) AS aliases, coalesce(n.source_documents, []) AS source_documents, "
            "coalesce(n.source_keys, []) AS source_keys, max_weight AS max_weight "
            f"ORDER BY max_weight DESC, n.mention_count DESC, n.label ASC LIMIT {int(limit)}"
        )
        return self._query_cypher(
            conn,
            body,
            columns=[
                "entity_id",
                "entity_key",
                "label",
                "entity_type",
                "mention_count",
                "aliases",
                "source_documents",
                "source_keys",
                "max_weight",
            ],
        )

    def _explore_relationships(
        self,
        conn: Connection,
        *,
        version_id: str,
        entity_ids: list[str],
        minimum_relationship_weight: float,
    ) -> list[dict[str, Any]]:
        if len(entity_ids) < _MIN_RELATIONSHIP_ENTITY_COUNT:
            return []
        ids = _cypher_list(entity_ids)
        body = (
            f"MATCH (s:KBEntity {{version_id: {cypher_value(version_id)}}})-[rel:RELATED]-(t:KBEntity {{version_id: {cypher_value(version_id)}}}) "
            f"WHERE s.entity_id IN {ids} AND t.entity_id IN {ids} "
            f"AND s.entity_id < t.entity_id "
            f"AND coalesce(rel.weight, 0) >= {float(minimum_relationship_weight)} "
            "RETURN coalesce(rel.relationship_id, '') AS relationship_id, "
            "s.entity_id AS source_entity_id, s.entity_key AS source_entity_key, s.label AS source_entity_label, "
            "t.entity_id AS target_entity_id, t.entity_key AS target_entity_key, t.label AS target_entity_label, "
            "coalesce(rel.relationship_type, 'related') AS relationship_type, "
            "coalesce(rel.weight, 0) AS weight, "
            "coalesce(rel.evidence_chunk_ids, []) AS evidence_chunk_ids, "
            "coalesce(rel.source_documents, []) AS source_documents "
            "ORDER BY weight DESC, source_entity_label ASC, target_entity_label ASC"
        )
        return self._query_cypher(
            conn,
            body,
            columns=[
                "relationship_id",
                "source_entity_id",
                "source_entity_key",
                "source_entity_label",
                "target_entity_id",
                "target_entity_key",
                "target_entity_label",
                "relationship_type",
                "weight",
                "evidence_chunk_ids",
                "source_documents",
            ],
        )

    def _neighbor_chunk_matches(
        self,
        conn: Connection,
        *,
        version_id: str,
        matched_entity_ids: list[str],
        minimum_relationship_weight: float,
    ) -> list[dict[str, Any]]:
        entity_ids = _cypher_list(matched_entity_ids)
        body = (
            f"MATCH (e:KBEntity {{version_id: {cypher_value(version_id)}}})-[rel:RELATED]-(n:KBEntity {{version_id: {cypher_value(version_id)}}})-[:MENTIONED_IN]->(c:KBChunk {{version_id: {cypher_value(version_id)}}}) "
            f"WHERE e.entity_id IN {entity_ids} AND coalesce(rel.weight, 0) >= {minimum_relationship_weight} "
            "RETURN c.chunk_id AS chunk_id, collect(DISTINCT e.label) AS matched_labels, collect(DISTINCT n.label) AS expanded_labels, sum(coalesce(rel.weight, 1.0)) AS graph_score"
        )
        return self._query_cypher(
            conn,
            body,
            columns=["chunk_id", "matched_labels", "expanded_labels", "graph_score"],
        )

    def _two_hop_chunk_matches(
        self,
        conn: Connection,
        *,
        version_id: str,
        matched_entity_ids: list[str],
        minimum_relationship_weight: float,
    ) -> list[dict[str, Any]]:
        entity_ids = _cypher_list(matched_entity_ids)
        body = (
            f"MATCH (e:KBEntity {{version_id: {cypher_value(version_id)}}})-[rel1:RELATED]-(n1:KBEntity {{version_id: {cypher_value(version_id)}}})"
            f"-[rel2:RELATED]-(n2:KBEntity {{version_id: {cypher_value(version_id)}}})-[:MENTIONED_IN]->(c:KBChunk {{version_id: {cypher_value(version_id)}}}) "
            f"WHERE e.entity_id IN {entity_ids} "
            f"AND coalesce(rel1.weight, 0) >= {minimum_relationship_weight} "
            f"AND coalesce(rel2.weight, 0) >= {minimum_relationship_weight} "
            "AND e.entity_id <> n2.entity_id "
            "RETURN c.chunk_id AS chunk_id, collect(DISTINCT e.label) AS matched_labels, collect(DISTINCT n1.label) + collect(DISTINCT n2.label) AS expanded_labels, sum(coalesce(rel1.weight, 1.0) + coalesce(rel2.weight, 1.0)) AS graph_score"
        )
        return self._query_cypher(
            conn,
            body,
            columns=["chunk_id", "matched_labels", "expanded_labels", "graph_score"],
        )

    def _accumulate_chunk_rows(
        self,
        rows: list[dict[str, Any]],
        *,
        factor: float,
        chunk_scores: dict[str, float],
        chunk_labels: dict[str, set[str]],
        chunk_expanded_labels: dict[str, set[str]],
        hop_scores: dict[str, float],
    ) -> None:
        for row in rows:
            chunk_id = str(row.get("chunk_id") or "")
            if not chunk_id:
                continue
            factored_score = max(0.0, float(row.get("graph_score") or 0.0) * factor)
            chunk_scores[chunk_id] += factored_score
            hop_scores[chunk_id] += factored_score
            for label in _as_list(row.get("matched_labels")):
                if label:
                    chunk_labels[chunk_id].add(str(label))
            for label in _as_list(row.get("expanded_labels")):
                if label:
                    chunk_expanded_labels[chunk_id].add(str(label))

    def _graph_entity_from_row(
        self,
        row: dict[str, Any],
        *,
        distance: int | None,
        highlighted: bool,
    ) -> AgeGraphEntity:
        return AgeGraphEntity(
            entity_id=str(row.get("entity_id") or ""),
            entity_key=str(row.get("entity_key") or ""),
            label=str(row.get("label") or ""),
            entity_type=str(row.get("entity_type") or ""),
            mention_count=int(row.get("mention_count") or 0),
            aliases=tuple(str(item) for item in _as_list(row.get("aliases")) if item),
            source_documents=tuple(
                str(item) for item in _as_list(row.get("source_documents")) if item
            ),
            source_keys=tuple(str(item) for item in _as_list(row.get("source_keys")) if item),
            distance=distance,
            highlighted=highlighted,
        )

    def _relationship_hop_distance(
        self,
        row: dict[str, Any],
        *,
        distances: dict[str, int],
    ) -> int | None:
        source_id = str(row.get("source_entity_id") or "")
        target_id = str(row.get("target_entity_id") or "")
        if source_id not in distances or target_id not in distances:
            return None
        return max(distances[source_id], distances[target_id])

    def _batch_create_nodes(
        self,
        conn: Connection,
        label: str,
        props: list[dict[str, Any]],
    ) -> None:
        validate_identifier(label, kind="label")
        bodies = [f"CREATE (:{label} {cypher_map(item)})" for item in props if item]
        self._batch_run_cypher(conn, bodies, batch_size=_NODE_BATCH_SIZE)

    def _batch_run_cypher(
        self,
        conn: Connection,
        statements: Iterable[str],
        *,
        batch_size: int,
    ) -> None:
        del batch_size
        for statement in statements:
            if not statement:
                continue
            self._run_cypher(conn, statement)

    def _run_cypher(self, conn: Connection, body: str) -> None:
        self._query_cypher(conn, body, columns=["result"])

    def _query_cypher(
        self,
        conn: Connection,
        body: str,
        *,
        columns: list[str] | None,
    ) -> list[dict[str, Any]]:
        safe_body = assert_cypher_body_safe(body)
        cols = compose_cypher_columns(columns)
        sql = f"SELECT * FROM cypher('{self._graph_name}', $$ {safe_body} $$) AS ({cols})"  # noqa: S608
        # psycopg still interprets '%' inside exec_driver_sql() strings using its
        # pyformat placeholder rules. AGE bodies embed raw object-store paths
        # (for example docs%2Fguide.md), so escape literal percents before the
        # driver sees the statement.
        sql = sql.replace("%", "%%")
        rows = conn.exec_driver_sql(sql).mappings().all()
        return [{key: parse_agtype(value) for key, value in row.items()} for row in rows]


def build_age_store(
    *, config: Any, session_factory: sessionmaker[Session]
) -> ApacheAgeKnowledgeStore:
    """Construct the AGE helper used by knowledge-base builds and retrieval."""

    return ApacheAgeKnowledgeStore(
        session_factory=session_factory,
        enabled=bool(getattr(config, "knowledge_age_enabled", False)),
        graph_name=str(getattr(config, "knowledge_age_graph_name", "knowledge_graph")),
    )


def _cypher_list(values: Iterable[str]) -> str:
    return "[" + ", ".join(cypher_value(str(item)) for item in values if item) + "]"


def _normalize_retrieval_query(value: str | None) -> str:
    return (value or "").strip().lower()


def _retrieval_query_candidates(query_text: str) -> list[str]:
    tokens = [
        token
        for token in re.findall(r"[a-z0-9]+", _normalize_retrieval_query(query_text))
        if len(token) >= _MIN_RETRIEVAL_QUERY_TOKEN_LENGTH
        and token not in _RETRIEVAL_QUERY_STOPWORDS
    ]
    ordered: list[str] = []
    for index in range(len(tokens) - 1):
        phrase = f"{tokens[index]} {tokens[index + 1]}"
        if phrase not in ordered:
            ordered.append(phrase)
    for token in tokens:
        if token not in ordered:
            ordered.append(token)
    return ordered[:_RETRIEVAL_QUERY_CANDIDATE_LIMIT]


def _matched_query_candidates(
    row: dict[str, Any],
    candidates: list[str],
) -> list[str]:
    haystacks = [
        str(row.get("label") or "").lower(),
        str(row.get("entity_key") or "").lower(),
        *(str(alias).lower() for alias in _as_list(row.get("aliases")) if str(alias).strip()),
    ]
    return [
        candidate
        for candidate in candidates
        if any(candidate in haystack for haystack in haystacks)
    ]


def _preview_text(content: str) -> str:
    compact = " ".join(content.split())
    if len(compact) <= _CHUNK_PREVIEW_LIMIT:
        return compact
    return compact[: _CHUNK_PREVIEW_LIMIT - 3].rstrip() + "..."


def _as_list(value: Any) -> list[Any]:
    if isinstance(value, list):
        return value
    if isinstance(value, tuple):
        return list(value)
    return []


__all__ = [
    "AgeChunkCandidate",
    "AgeDocumentNode",
    "AgeGraphEntity",
    "AgeGraphExploreResult",
    "AgeGraphRelationship",
    "AgeRetrievalResult",
    "AgeRetrievalStrength",
    "AgeSyncResult",
    "ApacheAgeKnowledgeStore",
    "build_age_store",
]
