"""Service layer for versioned knowledge-base builds and RAG queries."""

from __future__ import annotations

import hashlib
import importlib
import json
import logging
import math
import re
import tempfile
from collections import defaultdict
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from pathlib import Path
from time import perf_counter
from typing import Any, Literal, cast
from urllib.parse import quote

from sqlalchemy import delete, or_, select
from sqlalchemy.orm import Session, sessionmaker
from starlette.exceptions import HTTPException

from caliber.audit import record as audit_record
from caliber.auth import CaliberIdentity
from caliber.config import CaliberConfig
from caliber.db.models import (
    CaliberAuditLog,
    CaliberEvalDataset,
    CaliberEvalDatasetExample,
    CaliberKnowledgeBase,
    CaliberKnowledgeBaseChunk,
    CaliberKnowledgeBaseEntity,
    CaliberKnowledgeBaseRelationship,
    CaliberKnowledgeBaseRun,
    CaliberKnowledgeBaseRunEvent,
    CaliberKnowledgeBaseSource,
    CaliberKnowledgeBaseTestRun,
    CaliberKnowledgeBaseVersion,
)
from caliber.db.scoping import VisibilityTier, apply_visibility_filter
from caliber.ids import (
    new_knowledge_base_chunk_id,
    new_knowledge_base_entity_id,
    new_knowledge_base_id,
    new_knowledge_base_relationship_id,
    new_knowledge_base_run_id,
    new_knowledge_base_source_id,
    new_knowledge_base_test_run_id,
    new_knowledge_base_version_id,
)
from caliber.knowledge import calibration as kb_calibration
from caliber.knowledge import pgvector_ann
from caliber.knowledge.age import (
    AgeDocumentNode,
    AgeRetrievalResult,
    AgeSyncResult,
    build_age_store,
    postgres_age_required_reason,
)
from caliber.knowledge.chunking import chunk_text, list_chunking_strategies
from caliber.knowledge.embeddings import (
    KnowledgeDependencyError,
    build_embedding_backend,
    build_reranker_backend,
    ensure_embedding_backend_runtime_available,
    list_embedding_model_specs,
)
from caliber.knowledge.graph import (
    GRAPH_ENTITY_TYPE_SPECS,
    build_graph_bundle,
    entity_key_for_label,
)
from caliber.knowledge.schemas import (
    KnowledgeAgeSeedMode,
    KnowledgeBaseActiveVersionSummarySchema,
    KnowledgeBaseBuildResultSchema,
    KnowledgeBaseChunkSchema,
    KnowledgeBaseCreateRequest,
    KnowledgeBaseEntitySchema,
    KnowledgeBaseRelationshipSchema,
    KnowledgeBaseRunEventSchema,
    KnowledgeBaseRunSchema,
    KnowledgeBaseSchema,
    KnowledgeBaseSourceSchema,
    KnowledgeBaseUpdateRequest,
    KnowledgeBaseVersionCreateRequest,
    KnowledgeBaseVersionSchema,
    KnowledgeCalibrationRunSummary,
    KnowledgeGraphBuildPresetSchema,
    KnowledgeGraphConfigSchema,
    KnowledgeGraphEntityViewSchema,
    KnowledgeGraphExploreResultSchema,
    KnowledgeGraphExploreSource,
    KnowledgeGraphQueryPresetSchema,
    KnowledgeGraphRelationshipViewSchema,
    KnowledgeOptionSchema,
    KnowledgeOptionsSchema,
    KnowledgeQueryChunkSchema,
    KnowledgeQueryCitationSchema,
    KnowledgeQueryGraphOverridesSchema,
    KnowledgeQueryRequest,
    KnowledgeQueryResultSchema,
    KnowledgeQueryVersionResultSchema,
    KnowledgeRetrievalMode,
)
from caliber.observability.mlflow_tracing import get_tracer
from caliber.runtime_advisories import local_embedding_block_reason
from caliber.secrets import resolve_secret
from caliber.workflows.ingestion_tools import IngestionError, extract_document

logger = logging.getLogger(__name__)

_RESERVED_OUTPUT_PREFIX = ".caliber/knowledge-bases"
_MAX_EXTRACT_CHARS = 500_000
_SUPPORTED_EXTENSIONS = frozenset(
    {
        ".pdf",
        ".docx",
        ".pptx",
        ".xlsx",
        ".xlsm",
        ".md",
        ".markdown",
        ".txt",
        ".text",
        ".csv",
        ".tsv",
        ".json",
        ".jsonl",
        ".ndjson",
        ".yaml",
        ".yml",
        ".xml",
        ".html",
        ".htm",
        ".css",
        ".js",
        ".jsx",
        ".ts",
        ".tsx",
        ".py",
        ".sql",
        ".log",
    }
)
_TEXT_MEDIA_HINTS = (
    "text/",
    "application/json",
    "application/ld+json",
    "application/x-ndjson",
    "application/xml",
    "application/yaml",
    "application/x-yaml",
    "application/javascript",
)
_FALLBACK_PREVIEW_LIMIT = 280
_GRAPH_ENTITY_MATCH_THRESHOLD = 0.55
_ENTITY_PARTIAL_OVERLAP_MIN = 2

# Lexical (BM25) + reciprocal-rank-fusion tuning for the ``hybrid`` retrieval mode.
_BM25_K1 = 1.5
_BM25_B = 0.75
_RRF_K = 60
# Additive lexical (BM25) boost for the tri-hybrid ``graph_hybrid`` mode. A strong
# keyword match boosts meaningfully but cannot dominate the graph_boost cap of 1.25.
_GRAPH_LEXICAL_BOOST_WEIGHT = 0.6
_GRAPH_LEXICAL_BOOST_CAP = 0.6
_LEXICAL_STOPWORDS = frozenset(
    {
        "a",
        "an",
        "and",
        "are",
        "as",
        "at",
        "be",
        "by",
        "for",
        "from",
        "how",
        "in",
        "is",
        "it",
        "of",
        "on",
        "or",
        "that",
        "the",
        "to",
        "was",
        "what",
        "when",
        "where",
        "which",
        "who",
        "why",
        "will",
        "with",
    }
)


@dataclass(frozen=True)
class _ExpandedSource:
    document_id: str
    selection_kind: str
    bucket: str
    object_key: str
    object_name: str
    object_store_path: str
    size_bytes: int
    etag: str | None
    last_modified: datetime | None
    content_type: str | None


@dataclass(frozen=True)
class _PersistedArtifacts:
    logs_uri: str | None = None
    manifest_uri: str | None = None
    stats_uri: str | None = None
    chunks_uri: str | None = None
    entities_uri: str | None = None
    relationships_uri: str | None = None
    graph_uri: str | None = None


@dataclass(frozen=True)
class _RetrievedChunk:
    chunk: CaliberKnowledgeBaseChunk
    score: float
    dense_score: float
    graph_boost: float = 0.0
    matched_entities: tuple[str, ...] = ()
    score_breakdown: dict[str, float] = field(default_factory=dict)


@dataclass(frozen=True)
class _EntityMatch:
    entity: CaliberKnowledgeBaseEntity
    score: float


@dataclass(frozen=True)
class _GraphExplorerResult:
    entities: list[KnowledgeGraphEntityViewSchema]
    relationships: list[KnowledgeGraphRelationshipViewSchema]
    matched_entity_labels: list[str] = field(default_factory=list)
    expanded_entity_labels: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class _AgeSyncInputs:
    graph_config: KnowledgeGraphConfigSchema
    knowledge_base_id: str
    knowledge_base_name: str
    source_bucket: str
    version_number: int
    source_manifest: list[dict[str, Any]]
    chunking_strategy: str
    embedding_model: str
    chunking_config: dict[str, Any]
    output_bucket: str
    output_prefix: str
    version_status: str
    version_error_summary: str | None
    age_documents: list[AgeDocumentNode]
    chunk_exports: list[dict[str, Any]]
    entity_exports: list[dict[str, Any]]
    relationship_exports: list[dict[str, Any]]


@dataclass(frozen=True)
class _AgeSyncPersistResult:
    version: KnowledgeBaseVersionSchema
    summary: dict[str, Any]


@dataclass(frozen=True)
class _LocalGraphContext:
    eligible_entities: list[KnowledgeBaseEntitySchema]
    entity_lookup: dict[str, KnowledgeBaseEntitySchema]
    eligible_relationships: list[KnowledgeBaseRelationshipSchema]
    adjacency: dict[str, list[KnowledgeBaseRelationshipSchema]]


class KnowledgeBaseService:
    """Owns knowledge-base CRUD, build execution, and RAG querying."""

    def __init__(
        self,
        *,
        config: CaliberConfig,
        session_factory: sessionmaker[Session],
        object_store_client: Any | None = None,
    ) -> None:
        self._config = config
        self._session_factory = session_factory
        self._object_store_client = object_store_client
        self._embedder_cache: dict[str, Any] = {}
        self._reranker_cache: dict[str, Any] = {}
        self._age_store = build_age_store(config=config, session_factory=session_factory)

    # ------------------------------------------------------------------
    # Catalog / list / detail
    # ------------------------------------------------------------------
    def options(self) -> KnowledgeOptionsSchema:
        default_graph = self._default_graph_config()
        age_available = self._age_available()
        embedding_block_reason = local_embedding_block_reason(
            allow_flagged=self._config.allow_flagged_local_embeddings
        )
        retrieval_modes = [
            KnowledgeOptionSchema(
                id="dense",
                name="Dense chunks",
                description="Pure embedding similarity over stored chunks.",
                tags=["default", "fast"],
            ),
            KnowledgeOptionSchema(
                id="hybrid",
                name="Hybrid (keyword + vector)",
                description=(
                    "Reciprocal-rank fusion of BM25 keyword search and dense embedding "
                    "similarity — catches exact terms a pure vector search can miss."
                ),
                tags=["hybrid", "keyword", "vector"],
            ),
            KnowledgeOptionSchema(
                id="graph_hybrid",
                name="GraphRAG hybrid",
                description=(
                    "Embedding similarity, BM25 keyword matching, and knowledge-graph "
                    "entity/relationship expansion fused together — relationship-aware "
                    "chunk expansion with graph-grounded evidence hints."
                ),
                tags=["graph", "keyword", "traceable"],
            ),
        ]
        if age_available:
            retrieval_modes.append(
                KnowledgeOptionSchema(
                    id="age_graph",
                    name="Apache AGE graph",
                    description=(
                        "Primary graph retrieval through Apache AGE / Cypher over the synced "
                        "knowledge graph, with configurable dense reranking on returned chunks."
                    ),
                    tags=["graph", "cypher", "apache-age"],
                )
            )
        return KnowledgeOptionsSchema(
            chunking_strategies=[
                KnowledgeOptionSchema(
                    id=item.strategy_id,
                    name=item.name,
                    description=item.description,
                    defaults=item.defaults,
                    tags=list(item.tags),
                )
                for item in list_chunking_strategies()
            ],
            embedding_models=[
                KnowledgeOptionSchema(
                    id=item.model_id,
                    name=item.name,
                    description=item.description,
                    tags=list(item.tags),
                    available=embedding_block_reason is None,
                    unavailable_reason=embedding_block_reason,
                    requires_override=embedding_block_reason is not None,
                )
                for item in list_embedding_model_specs()
            ],
            retrieval_modes=retrieval_modes,
            graph_extractors=[
                KnowledgeOptionSchema(
                    id="heuristic",
                    name="Heuristic",
                    description="Dependency-light extractor using headings, acronyms, and title-case phrases.",
                    tags=["fast", "default"],
                ),
                KnowledgeOptionSchema(
                    id="spacy",
                    name="spaCy named entities",
                    description="Named-entity extraction with spaCy plus heuristic heading and phrase capture.",
                    tags=["nlp", "higher-recall"],
                ),
            ],
            graph_output_targets=[
                KnowledgeOptionSchema(
                    id="object_store",
                    name="Object store artifacts only",
                    description="Write graph artifacts back to the object store but keep retrieval inside CALIBER tables.",
                    tags=["portable", "default"],
                ),
                *(
                    [
                        KnowledgeOptionSchema(
                            id="object_store_and_age",
                            name="Object store + Apache AGE",
                            description=(
                                "Persist graph artifacts and sync the version-scoped graph into the shared "
                                f"Apache AGE graph {self._config.knowledge_age_graph_name!r}."
                            ),
                            tags=["apache-age", "cypher"],
                        )
                    ]
                    if age_available
                    else []
                ),
            ],
            graph_retrieval_strengths=[
                KnowledgeOptionSchema(
                    id="conservative",
                    name="Conservative",
                    description="Prefer direct entity-to-chunk links with minimal relationship expansion.",
                    tags=["precise"],
                ),
                KnowledgeOptionSchema(
                    id="balanced",
                    name="Balanced",
                    description="Blend direct chunk evidence with one-hop relationship expansion for reliable GraphRAG.",
                    tags=["recommended"],
                ),
                KnowledgeOptionSchema(
                    id="aggressive",
                    name="Aggressive",
                    description="Push deeper graph expansion and broader chunk recall for exploratory questions.",
                    tags=["high-recall"],
                ),
            ],
            graph_age_seed_modes=[
                KnowledgeOptionSchema(
                    id="entity_then_text",
                    name="Entity first, then question text",
                    description="Try extracted query entities first, then fall back to raw question text if needed.",
                    tags=["default", "balanced"],
                ),
                KnowledgeOptionSchema(
                    id="query_entities_only",
                    name="Extracted entities only",
                    description="Require explicit entity matches from the question before AGE traversal starts.",
                    tags=["precise"],
                ),
                KnowledgeOptionSchema(
                    id="query_text_only",
                    name="Question text only",
                    description="Seed AGE directly from the raw question text and ignore extracted query entities.",
                    tags=["text-first"],
                ),
                KnowledgeOptionSchema(
                    id="query_entities_and_text",
                    name="Entities plus question text",
                    description="Union extracted entities with text-matched entities for the broadest AGE candidate set.",
                    tags=["high-recall"],
                ),
            ],
            graph_entity_types=[
                KnowledgeOptionSchema(
                    id=item_id,
                    name=name,
                    description=description,
                )
                for item_id, name, description in GRAPH_ENTITY_TYPE_SPECS
            ],
            graph_build_presets=self._graph_build_presets(age_available),
            graph_query_presets=self._graph_query_presets(age_available),
            default_graph_config=default_graph,
            age_enabled=age_available,
            age_graph_name=self._config.knowledge_age_graph_name if age_available else None,
            age_viewer_url=(
                str(self._config.knowledge_age_viewer_url).strip()
                if age_available and self._config.knowledge_age_viewer_url
                else None
            ),
            age_unavailable_reason=(
                None if age_available else self._age_unavailable_deployment_reason()
            ),
            reserved_output_prefix=_RESERVED_OUTPUT_PREFIX,
        )

    def list_knowledge_bases(
        self,
        *,
        identity: CaliberIdentity,
        status: str = "active",
        visibility: VisibilityTier | None = None,
    ) -> list[KnowledgeBaseSchema]:
        with self._session_factory() as session:
            stmt = select(CaliberKnowledgeBase).order_by(CaliberKnowledgeBase.updated_at.desc())
            if status != "all":
                stmt = stmt.where(CaliberKnowledgeBase.status == status)
            stmt = apply_visibility_filter(
                stmt,
                CaliberKnowledgeBase,
                identity,
                identity.active_project_id,
                only=visibility,
            )
            rows = session.execute(stmt).scalars().all()
            library_versions = self._library_version_map(session, rows)
            return [
                self._serialize_knowledge_base(
                    row,
                    version=library_versions.get(row.knowledge_base_id),
                )
                for row in rows
            ]

    def get_knowledge_base(
        self, knowledge_base_id: str, *, identity: CaliberIdentity
    ) -> KnowledgeBaseSchema:
        with self._session_factory() as session:
            row = self._require_visible_knowledge_base(session, knowledge_base_id, identity)
            library_versions = self._library_version_map(session, [row])
            return self._serialize_knowledge_base(
                row,
                version=library_versions.get(row.knowledge_base_id),
            )

    def update_knowledge_base(
        self,
        knowledge_base_id: str,
        payload: KnowledgeBaseUpdateRequest,
        *,
        identity: CaliberIdentity,
        actor: str,
    ) -> KnowledgeBaseSchema:
        changes = payload.model_dump(exclude_unset=True)
        if not changes:
            raise HTTPException(
                status_code=400, detail="request body must include at least one field"
            )
        with self._session_factory() as session:
            row = self._require_visible_knowledge_base(session, knowledge_base_id, identity)
            diff: dict[str, dict[str, object | None]] = {}
            for field, value in changes.items():
                old = getattr(row, field)
                if old != value:
                    setattr(row, field, value)
                    diff[field] = {"from": old, "to": value}
            if not diff:
                library_versions = self._library_version_map(session, [row])
                return self._serialize_knowledge_base(
                    row,
                    version=library_versions.get(row.knowledge_base_id),
                )
            audit_record(
                session,
                actor=actor,
                action="update_knowledge_base",
                entity_type="knowledge_base",
                entity_id=row.knowledge_base_id,
                details={"changes": diff},
            )
            session.commit()
            session.refresh(row)
            library_versions = self._library_version_map(session, [row])
            return self._serialize_knowledge_base(
                row,
                version=library_versions.get(row.knowledge_base_id),
            )

    def list_versions(
        self, knowledge_base_id: str, *, identity: CaliberIdentity
    ) -> list[KnowledgeBaseVersionSchema]:
        with self._session_factory() as session:
            self._require_visible_knowledge_base(session, knowledge_base_id, identity)
            stmt = (
                select(CaliberKnowledgeBaseVersion)
                .where(CaliberKnowledgeBaseVersion.knowledge_base_id == knowledge_base_id)
                .order_by(CaliberKnowledgeBaseVersion.version_number.desc())
            )
            rows = session.execute(stmt).scalars().all()
            return [self._serialize_version(row) for row in rows]

    def get_version(
        self, version_id: str, *, identity: CaliberIdentity
    ) -> KnowledgeBaseVersionSchema:
        with self._session_factory() as session:
            version, _base = self._require_visible_version(session, version_id, identity)
            return self._serialize_version(version)

    def list_sources(
        self, version_id: str, *, identity: CaliberIdentity
    ) -> list[KnowledgeBaseSourceSchema]:
        with self._session_factory() as session:
            version, _base = self._require_visible_version(session, version_id, identity)
            stmt = (
                select(CaliberKnowledgeBaseSource)
                .where(
                    CaliberKnowledgeBaseSource.knowledge_base_version_id
                    == version.knowledge_base_version_id
                )
                .order_by(CaliberKnowledgeBaseSource.object_key.asc())
            )
            rows = session.execute(stmt).scalars().all()
            return [KnowledgeBaseSourceSchema.model_validate(row) for row in rows]

    def list_chunks(
        self,
        version_id: str,
        *,
        identity: CaliberIdentity,
        query: str = "",
        source_key: str | None = None,
        limit: int = 200,
    ) -> list[KnowledgeBaseChunkSchema]:
        with self._session_factory() as session:
            version, _base = self._require_visible_version(session, version_id, identity)
            stmt = select(CaliberKnowledgeBaseChunk).where(
                CaliberKnowledgeBaseChunk.knowledge_base_version_id
                == version.knowledge_base_version_id
            )
            if source_key:
                stmt = stmt.where(CaliberKnowledgeBaseChunk.source_key == source_key)
            if query.strip():
                like = f"%{query.strip()}%"
                stmt = stmt.where(CaliberKnowledgeBaseChunk.content.ilike(like))
            stmt = stmt.order_by(CaliberKnowledgeBaseChunk.ordinal.asc()).limit(
                max(1, min(limit, 1000))
            )
            rows = session.execute(stmt).scalars().all()
            return [KnowledgeBaseChunkSchema.model_validate(row) for row in rows]

    def list_entities(
        self,
        version_id: str,
        *,
        identity: CaliberIdentity,
    ) -> list[KnowledgeBaseEntitySchema]:
        with self._session_factory() as session:
            version, _base = self._require_visible_version(session, version_id, identity)
            return self._list_entities_for_version(
                session,
                version_id=version.knowledge_base_version_id,
            )

    def list_relationships(
        self,
        version_id: str,
        *,
        identity: CaliberIdentity,
    ) -> list[KnowledgeBaseRelationshipSchema]:
        with self._session_factory() as session:
            version, _base = self._require_visible_version(session, version_id, identity)
            return self._list_relationships_for_version(
                session,
                version_id=version.knowledge_base_version_id,
            )

    def explore_graph(
        self,
        version_id: str,
        *,
        identity: CaliberIdentity,
        query: str = "",
        source: KnowledgeGraphExploreSource = "local",
        entity_type: str | None = None,
        minimum_relationship_weight: float | None = None,
        traversal_hops: int | None = None,
        age_seed_mode: KnowledgeAgeSeedMode | None = None,
        strict_age_retrieval: bool = False,
        node_limit: int = 12,
    ) -> KnowledgeGraphExploreResultSchema:
        with self._session_factory() as session:
            version, _base = self._require_visible_version(session, version_id, identity)
            graph_config = self._resolve_graph_config(version.graph_config)
            requested_source: KnowledgeGraphExploreSource = "age" if source == "age" else "local"
            min_weight = max(
                0.0,
                float(
                    graph_config.minimum_relationship_weight
                    if minimum_relationship_weight is None
                    else minimum_relationship_weight
                ),
            )
            hops = max(
                0,
                min(
                    int(
                        graph_config.age_traversal_hops
                        if traversal_hops is None
                        else traversal_hops
                    ),
                    2,
                ),
            )
            limit = max(4, min(int(node_limit), 48))
            normalized_query = query.strip()
            normalized_type = (entity_type or "").strip().lower() or None
            question_bundle = (
                self._question_graph_bundle(
                    question=normalized_query,
                    graph_config=graph_config,
                )
                if normalized_query
                else None
            )
            query_entity_labels = (
                [item.label for item in question_bundle.entities]
                if question_bundle is not None
                else []
            )
            query_entity_keys = (
                [item.entity_key for item in question_bundle.entities]
                if question_bundle is not None
                else []
            )
            resolved_age_seed_mode = age_seed_mode or graph_config.age_seed_mode
            age_ready = self._version_age_ready(version, graph_config=graph_config)
            summary = version.summary if isinstance(version.summary, dict) else {}
            resolved_age_graph_name = (
                str(summary.get("age_graph_name"))
                if summary.get("age_graph_name") is not None
                else (self._age_store.graph_name if self._age_available() else None)
            )
            fallback_reason: str | None = None

            def _strict_age_response(
                reason: str,
                *,
                age_seed_strategy: Literal[
                    "query_entities", "query_text", "query_entities_and_text"
                ]
                | None = None,
                matched_entity_labels: list[str] | None = None,
                expanded_entity_labels: list[str] | None = None,
            ) -> KnowledgeGraphExploreResultSchema:
                return KnowledgeGraphExploreResultSchema(
                    knowledge_base_version_id=version.knowledge_base_version_id,
                    requested_source=requested_source,
                    served_source="age",
                    age_enabled=self._age_available(),
                    age_ready=age_ready,
                    age_graph_name=resolved_age_graph_name,
                    age_status=(
                        str(summary.get("age_sync_status"))
                        if summary.get("age_sync_status") is not None
                        else None
                    ),
                    query=normalized_query,
                    entity_type=normalized_type,
                    query_entity_labels=query_entity_labels,
                    minimum_relationship_weight=min_weight,
                    traversal_hops=hops,
                    node_limit=limit,
                    age_seed_mode=resolved_age_seed_mode,
                    strict_age_retrieval=True,
                    age_seed_strategy=age_seed_strategy,
                    matched_entity_labels=matched_entity_labels or [],
                    expanded_entity_labels=expanded_entity_labels or [],
                    fallback_reason=reason,
                    entities=[],
                    relationships=[],
                )

            if requested_source == "age" and age_ready:
                age_result = self._age_store.explore(
                    version_id=version.knowledge_base_version_id,
                    query=normalized_query,
                    query_entity_keys=query_entity_keys,
                    entity_type=normalized_type,
                    minimum_relationship_weight=min_weight,
                    traversal_hops=hops,
                    seed_mode=resolved_age_seed_mode,
                    node_limit=limit,
                )
                if age_result.status == "ok":
                    return KnowledgeGraphExploreResultSchema(
                        knowledge_base_version_id=version.knowledge_base_version_id,
                        requested_source=requested_source,
                        served_source="age",
                        age_enabled=self._age_available(),
                        age_ready=True,
                        age_graph_name=age_result.graph_name,
                        age_status=str(summary.get("age_sync_status") or "synced"),
                        query=normalized_query,
                        entity_type=normalized_type,
                        query_entity_labels=query_entity_labels,
                        minimum_relationship_weight=min_weight,
                        traversal_hops=hops,
                        node_limit=limit,
                        age_seed_mode=resolved_age_seed_mode,
                        strict_age_retrieval=strict_age_retrieval,
                        age_seed_strategy=age_result.seed_strategy,
                        matched_entity_labels=list(age_result.matched_entities),
                        expanded_entity_labels=list(age_result.expanded_entities),
                        fallback_reason=age_result.fallback_reason,
                        entities=[
                            KnowledgeGraphEntityViewSchema.model_validate(
                                {
                                    "knowledge_base_entity_id": entity.entity_id,
                                    "knowledge_base_version_id": version.knowledge_base_version_id,
                                    "entity_key": entity.entity_key,
                                    "label": entity.label,
                                    "entity_type": entity.entity_type,
                                    "aliases": list(entity.aliases),
                                    "mention_count": entity.mention_count,
                                    "source_documents": list(entity.source_documents),
                                    "source_keys": list(entity.source_keys),
                                    "source_chunks": [],
                                    "entity_metadata": {},
                                    "created_at": version.created_at,
                                    "distance": entity.distance,
                                    "highlighted": entity.highlighted,
                                    "graph_source": "age",
                                }
                            )
                            for entity in age_result.entities
                        ],
                        relationships=[
                            KnowledgeGraphRelationshipViewSchema.model_validate(
                                {
                                    "knowledge_base_relationship_id": relationship.relationship_id
                                    or f"age:{relationship.source_entity_id}:{relationship.target_entity_id}",
                                    "knowledge_base_version_id": version.knowledge_base_version_id,
                                    "source_entity_id": relationship.source_entity_id,
                                    "source_entity_key": relationship.source_entity_key,
                                    "source_entity_label": relationship.source_entity_label,
                                    "target_entity_id": relationship.target_entity_id,
                                    "target_entity_key": relationship.target_entity_key,
                                    "target_entity_label": relationship.target_entity_label,
                                    "relationship_type": relationship.relationship_type,
                                    "weight": float(relationship.weight),
                                    "evidence_chunk_ids": list(relationship.evidence_chunk_ids),
                                    "source_documents": list(relationship.source_documents),
                                    "relationship_metadata": {},
                                    "created_at": version.created_at,
                                    "hop_distance": relationship.hop_distance,
                                    "graph_source": "age",
                                }
                            )
                            for relationship in age_result.relationships
                        ],
                    )
                if strict_age_retrieval:
                    return _strict_age_response(
                        age_result.fallback_reason
                        or "Apache AGE graph exploration did not return any matching neighborhood.",
                        age_seed_strategy=age_result.seed_strategy,
                        matched_entity_labels=list(age_result.matched_entities),
                        expanded_entity_labels=list(age_result.expanded_entities),
                    )
                fallback_reason = age_result.fallback_reason
            elif requested_source == "age":
                fallback_reason = self._age_unavailable_reason(
                    version,
                    graph_config=graph_config,
                )
                if strict_age_retrieval:
                    return _strict_age_response(fallback_reason)

            local_graph = self._explore_local_graph(
                entities=self._list_entities_for_version(
                    session,
                    version_id=version.knowledge_base_version_id,
                ),
                relationships=self._list_relationships_for_version(
                    session,
                    version_id=version.knowledge_base_version_id,
                ),
                query=normalized_query,
                entity_type=normalized_type,
                minimum_relationship_weight=min_weight,
                traversal_hops=hops,
                node_limit=limit,
            )
            return KnowledgeGraphExploreResultSchema(
                knowledge_base_version_id=version.knowledge_base_version_id,
                requested_source=requested_source,
                served_source="local",
                age_enabled=self._age_available(),
                age_ready=age_ready,
                age_graph_name=resolved_age_graph_name,
                age_status=str(summary.get("age_sync_status"))
                if summary.get("age_sync_status") is not None
                else None,
                query=normalized_query,
                entity_type=normalized_type,
                query_entity_labels=query_entity_labels,
                minimum_relationship_weight=min_weight,
                traversal_hops=hops,
                node_limit=limit,
                age_seed_mode=resolved_age_seed_mode if requested_source == "age" else None,
                strict_age_retrieval=strict_age_retrieval,
                matched_entity_labels=local_graph.matched_entity_labels,
                expanded_entity_labels=local_graph.expanded_entity_labels,
                fallback_reason=fallback_reason,
                entities=local_graph.entities,
                relationships=local_graph.relationships,
            )

    def list_runs(
        self, knowledge_base_id: str, *, identity: CaliberIdentity
    ) -> list[KnowledgeBaseRunSchema]:
        with self._session_factory() as session:
            self._require_visible_knowledge_base(session, knowledge_base_id, identity)
            stmt = (
                select(CaliberKnowledgeBaseRun)
                .where(CaliberKnowledgeBaseRun.knowledge_base_id == knowledge_base_id)
                .order_by(CaliberKnowledgeBaseRun.created_at.desc())
            )
            rows = session.execute(stmt).scalars().all()
            return [KnowledgeBaseRunSchema.model_validate(row) for row in rows]

    def list_run_events(
        self, run_id: str, *, identity: CaliberIdentity
    ) -> list[KnowledgeBaseRunEventSchema]:
        with self._session_factory() as session:
            run = session.get(CaliberKnowledgeBaseRun, run_id)
            if run is None:
                raise HTTPException(
                    status_code=404, detail=f"knowledge-base run {run_id!r} not found"
                )
            self._require_visible_knowledge_base(session, run.knowledge_base_id, identity)
            stmt = (
                select(CaliberKnowledgeBaseRunEvent)
                .where(CaliberKnowledgeBaseRunEvent.knowledge_base_run_id == run_id)
                .order_by(CaliberKnowledgeBaseRunEvent.sequence.asc())
            )
            rows = session.execute(stmt).scalars().all()
            return [KnowledgeBaseRunEventSchema.model_validate(row) for row in rows]

    # ------------------------------------------------------------------
    # Builds / version activation
    # ------------------------------------------------------------------
    def create_knowledge_base(
        self,
        payload: KnowledgeBaseCreateRequest,
        *,
        identity: CaliberIdentity,
        actor: str,
    ) -> KnowledgeBaseBuildResultSchema:
        queue_build = self._should_enqueue_builds()
        self._ensure_embedding_runtime_available()
        source_manifest = [_selection_dict(item.kind, item.path) for item in payload.sources]
        fingerprint = _fingerprint_sources(payload.source_bucket, source_manifest)
        knowledge_base_id = new_knowledge_base_id()
        version_id = new_knowledge_base_version_id()
        run_id = new_knowledge_base_run_id()
        now = _utcnow()
        graph_config = self._resolve_graph_config(payload.graph_config)
        self._assert_graph_target_supported(graph_config)

        with self._session_factory() as session:
            self._assert_unique_name(session, payload.name, identity, exclude_id=None)
            output_prefix = self._output_prefix(knowledge_base_id, 1, version_id)
            knowledge_base = CaliberKnowledgeBase(
                knowledge_base_id=knowledge_base_id,
                name=payload.name,
                description=payload.description,
                owner=identity.user_id,
                project_id=identity.active_project_id,
                visibility="project" if identity.active_project_id else "user",
                status="active",
                source_bucket=payload.source_bucket,
                source_manifest=source_manifest,
                source_fingerprint=fingerprint,
                last_run_id=run_id,
                last_run_status="queued" if queue_build else "processing",
            )
            version = CaliberKnowledgeBaseVersion(
                knowledge_base_version_id=version_id,
                knowledge_base_id=knowledge_base_id,
                version_number=1,
                status="queued" if queue_build else "processing",
                chunking_strategy=payload.chunking_strategy,
                chunking_config=dict(payload.chunking_config),
                graph_config=graph_config.model_dump(),
                embedding_model=payload.embedding_model,
                source_manifest=source_manifest,
                source_fingerprint=fingerprint,
                output_bucket=payload.source_bucket,
                output_prefix=output_prefix,
                created_by=actor,
            )
            run = CaliberKnowledgeBaseRun(
                knowledge_base_run_id=run_id,
                knowledge_base_id=knowledge_base_id,
                knowledge_base_version_id=version_id,
                status="queued" if queue_build else "running",
                source_manifest=source_manifest,
                created_by=actor,
                queued_at=now if queue_build else None,
                started_at=None if queue_build else now,
            )
            session.add(knowledge_base)
            session.add(version)
            # Persist parent rows first so Postgres sees the referenced
            # knowledge-base version before the run row arrives.
            session.flush()
            session.add(run)
            session.flush()
            if queue_build:
                session.add(
                    CaliberKnowledgeBaseRunEvent(
                        knowledge_base_run_id=run_id,
                        sequence=1,
                        event_type="build_queued",
                        payload={
                            "at": now.isoformat(),
                            "knowledge_base_id": knowledge_base_id,
                            "version_id": version_id,
                            "run_id": run_id,
                            "source_bucket": payload.source_bucket,
                            "chunking_strategy": payload.chunking_strategy,
                            "embedding_model": payload.embedding_model,
                        },
                        created_at=now,
                    )
                )
                run.log_line_count = 1
            audit_record(
                session,
                actor=actor,
                action="create_knowledge_base",
                entity_type="knowledge_base",
                entity_id=knowledge_base_id,
                details={
                    "name": payload.name,
                    "source_bucket": payload.source_bucket,
                    "version_id": version_id,
                    "chunking_strategy": payload.chunking_strategy,
                    "embedding_model": payload.embedding_model,
                    "graph_config": graph_config.model_dump(),
                },
            )
            session.commit()

        if queue_build:
            return self._build_result(knowledge_base_id, version_id, run_id, identity)
        self.execute_run(run_id)
        return self._build_result(knowledge_base_id, version_id, run_id, identity)

    def create_version(
        self,
        knowledge_base_id: str,
        payload: KnowledgeBaseVersionCreateRequest,
        *,
        identity: CaliberIdentity,
        actor: str,
    ) -> KnowledgeBaseBuildResultSchema:
        queue_build = self._should_enqueue_builds()
        self._ensure_embedding_runtime_available()
        now = _utcnow()
        with self._session_factory() as session:
            knowledge_base = self._require_visible_knowledge_base(
                session, knowledge_base_id, identity
            )
            versions = (
                session.execute(
                    select(CaliberKnowledgeBaseVersion)
                    .where(CaliberKnowledgeBaseVersion.knowledge_base_id == knowledge_base_id)
                    .order_by(CaliberKnowledgeBaseVersion.version_number.desc())
                    .limit(1)
                )
                .scalars()
                .all()
            )
            next_number = (versions[0].version_number if versions else 0) + 1
            source_manifest = (
                [_selection_dict(item.kind, item.path) for item in payload.sources]
                if payload.sources is not None
                else list(knowledge_base.source_manifest or [])
            )
            if not source_manifest:
                raise HTTPException(
                    status_code=400, detail="knowledge base has no sources to process"
                )
            fingerprint = _fingerprint_sources(knowledge_base.source_bucket, source_manifest)
            version_id = new_knowledge_base_version_id()
            run_id = new_knowledge_base_run_id()
            output_prefix = self._output_prefix(knowledge_base_id, next_number, version_id)
            prior_graph_config = versions[0].graph_config if versions else None
            graph_config = self._resolve_graph_config(
                payload.graph_config, fallback=prior_graph_config
            )
            graph_config = self._prefer_age_graph_target(
                graph_config,
                explicit=payload.graph_config is not None,
                promote_default_retrieval_when_upgrading=True,
            )
            self._assert_graph_target_supported(graph_config)
            version = CaliberKnowledgeBaseVersion(
                knowledge_base_version_id=version_id,
                knowledge_base_id=knowledge_base_id,
                version_number=next_number,
                status="queued" if queue_build else "processing",
                chunking_strategy=payload.chunking_strategy,
                chunking_config=dict(payload.chunking_config),
                graph_config=graph_config.model_dump(),
                embedding_model=payload.embedding_model,
                source_manifest=source_manifest,
                source_fingerprint=fingerprint,
                output_bucket=knowledge_base.source_bucket,
                output_prefix=output_prefix,
                created_by=actor,
            )
            run = CaliberKnowledgeBaseRun(
                knowledge_base_run_id=run_id,
                knowledge_base_id=knowledge_base_id,
                knowledge_base_version_id=version_id,
                status="queued" if queue_build else "running",
                source_manifest=source_manifest,
                created_by=actor,
                queued_at=now if queue_build else None,
                started_at=None if queue_build else now,
            )
            knowledge_base.last_run_id = run_id
            knowledge_base.last_run_status = "queued" if queue_build else "processing"
            session.add(version)
            # Insert the new version before the run so strict FK databases do
            # not observe an out-of-order flush.
            session.flush()
            session.add(run)
            session.flush()
            if queue_build:
                session.add(
                    CaliberKnowledgeBaseRunEvent(
                        knowledge_base_run_id=run_id,
                        sequence=1,
                        event_type="build_queued",
                        payload={
                            "at": now.isoformat(),
                            "knowledge_base_id": knowledge_base_id,
                            "version_id": version_id,
                            "run_id": run_id,
                            "source_bucket": knowledge_base.source_bucket,
                            "chunking_strategy": payload.chunking_strategy,
                            "embedding_model": payload.embedding_model,
                        },
                        created_at=now,
                    )
                )
                run.log_line_count = 1
            audit_record(
                session,
                actor=actor,
                action="create_knowledge_base_version",
                entity_type="knowledge_base_version",
                entity_id=version_id,
                details={
                    "knowledge_base_id": knowledge_base_id,
                    "version_number": next_number,
                    "chunking_strategy": payload.chunking_strategy,
                    "embedding_model": payload.embedding_model,
                    "graph_config": graph_config.model_dump(),
                },
            )
            session.commit()
        if queue_build:
            return self._build_result(knowledge_base_id, version_id, run_id, identity)
        self.execute_run(run_id)
        return self._build_result(knowledge_base_id, version_id, run_id, identity)

    def activate_version(
        self,
        knowledge_base_id: str,
        version_id: str,
        *,
        identity: CaliberIdentity,
        actor: str,
    ) -> KnowledgeBaseSchema:
        with self._session_factory() as session:
            knowledge_base = self._require_visible_knowledge_base(
                session, knowledge_base_id, identity
            )
            version = session.get(CaliberKnowledgeBaseVersion, version_id)
            if version is None or version.knowledge_base_id != knowledge_base_id:
                raise HTTPException(
                    status_code=404, detail=f"knowledge-base version {version_id!r} not found"
                )
            if version.status != "completed":
                raise HTTPException(
                    status_code=409, detail="only completed versions can be activated"
                )
            # Capture the outgoing active version BEFORE overwriting so a future
            # rollback can restore the EXACT prior active (not an ordinal guess).
            previous_active_version_id = knowledge_base.active_version_id
            knowledge_base.active_version_id = version_id
            knowledge_base.source_manifest = list(version.source_manifest or [])
            knowledge_base.source_fingerprint = version.source_fingerprint
            audit_record(
                session,
                actor=actor,
                action="activate_knowledge_base_version",
                entity_type="knowledge_base",
                entity_id=knowledge_base_id,
                details={
                    "version_id": version_id,
                    "version_number": version.version_number,
                    "previous_active_version_id": previous_active_version_id,
                },
            )
            session.commit()
            session.refresh(knowledge_base)
            return self._serialize_knowledge_base(knowledge_base, version=version)

    def rollback_version(
        self,
        knowledge_base_id: str,
        *,
        identity: CaliberIdentity,
        actor: str,
    ) -> KnowledgeBaseSchema:
        """Re-activate the version that was active immediately before the current one.

        The prior active version id is read from the activation audit trail (the
        ``previous_active_version_id`` recorded by :meth:`activate_version`), so
        the restore is exact. Returns 409 when there is no recorded prior active
        version, or when that version is no longer completed/available.
        """
        with self._session_factory() as session:
            knowledge_base = self._require_visible_knowledge_base(
                session, knowledge_base_id, identity
            )
            current_active = knowledge_base.active_version_id
            if current_active is None:
                raise HTTPException(
                    status_code=409,
                    detail=f"knowledge base {knowledge_base_id!r} has no active version to roll back",
                )
            target_version_id = self._previous_active_version_id(
                session, knowledge_base_id, current_active
            )
            if target_version_id is None:
                raise HTTPException(
                    status_code=409,
                    detail=(
                        f"no recorded prior active version for {knowledge_base_id!r}; "
                        "rollback needs an audited activation to restore"
                    ),
                )
            version = session.get(CaliberKnowledgeBaseVersion, target_version_id)
            if version is None or version.knowledge_base_id != knowledge_base_id:
                raise HTTPException(
                    status_code=409,
                    detail=f"prior active version {target_version_id!r} no longer exists",
                )
            if version.status != "completed":
                raise HTTPException(
                    status_code=409,
                    detail=f"prior active version {target_version_id!r} is not in a completed state",
                )
            knowledge_base.active_version_id = target_version_id
            knowledge_base.source_manifest = list(version.source_manifest or [])
            knowledge_base.source_fingerprint = version.source_fingerprint
            audit_record(
                session,
                actor=actor,
                action="rollback_knowledge_base_version",
                entity_type="knowledge_base",
                entity_id=knowledge_base_id,
                details={
                    "version_id": target_version_id,
                    "version_number": version.version_number,
                    "previous_active_version_id": current_active,
                },
            )
            session.commit()
            session.refresh(knowledge_base)
            return self._serialize_knowledge_base(knowledge_base, version=version)

    @staticmethod
    def _previous_active_version_id(
        session: Session, knowledge_base_id: str, current_active: str
    ) -> str | None:
        """The version active immediately before ``current_active``.

        Walks the activation/rollback audit trail newest-first and returns the
        ``previous_active_version_id`` of the most recent row whose
        ``version_id`` is the version currently active.
        """
        rows = (
            session.execute(
                select(CaliberAuditLog)
                .where(CaliberAuditLog.entity_type == "knowledge_base")
                .where(CaliberAuditLog.entity_id == knowledge_base_id)
                .where(
                    CaliberAuditLog.action.in_(
                        ("activate_knowledge_base_version", "rollback_knowledge_base_version")
                    )
                )
                .order_by(CaliberAuditLog.timestamp.desc(), CaliberAuditLog.log_id.desc())
                .limit(100)
            )
            .scalars()
            .all()
        )
        for row in rows:
            details = row.details or {}
            if details.get("version_id") == current_active:
                prior = details.get("previous_active_version_id")
                return prior if isinstance(prior, str) else None
        return None

    def sync_version_to_age(
        self,
        version_id: str,
        *,
        identity: CaliberIdentity,
        actor: str,
    ) -> KnowledgeBaseVersionSchema:
        if not self._age_available():
            raise HTTPException(
                status_code=400,
                detail=self._age_unavailable_deployment_reason(),
            )

        with self._session_factory() as session:
            inputs = self._load_age_sync_inputs(session, version_id=version_id, identity=identity)
        age_sync = self._age_store.sync_version(
            knowledge_base_id=inputs.knowledge_base_id,
            knowledge_base_name=inputs.knowledge_base_name,
            version_id=version_id,
            version_number=inputs.version_number,
            source_bucket=inputs.source_bucket,
            documents=inputs.age_documents,
            chunks=inputs.chunk_exports,
            entities=inputs.entity_exports,
            relationships=inputs.relationship_exports,
        )
        sync_at = _utcnow()
        age_sync_summary = self._age_sync_summary(age_sync)
        persisted = self._persist_age_sync_result(
            version_id=version_id,
            inputs=inputs,
            age_sync=age_sync,
            age_sync_summary=age_sync_summary,
            sync_at=sync_at,
            actor=actor,
        )

        try:
            self._refresh_version_age_sync_artifacts(
                knowledge_base_id=inputs.knowledge_base_id,
                version_id=version_id,
                version_status=inputs.version_status,
                version_error_summary=inputs.version_error_summary,
                source_bucket=inputs.source_bucket,
                source_manifest=inputs.source_manifest,
                chunking_strategy=inputs.chunking_strategy,
                embedding_model=inputs.embedding_model,
                chunking_config=inputs.chunking_config,
                output_bucket=inputs.output_bucket,
                output_prefix=inputs.output_prefix,
                graph_config=inputs.graph_config,
                summary=persisted.summary,
                age_sync_summary=age_sync_summary,
            )
        except Exception:
            logger.warning(
                "failed to refresh AGE sync artifacts for knowledge-base version %s",
                version_id,
                exc_info=True,
            )

        return persisted.version

    def _load_age_sync_inputs(
        self,
        session: Session,
        *,
        version_id: str,
        identity: CaliberIdentity,
    ) -> _AgeSyncInputs:
        version, knowledge_base = self._require_visible_version(session, version_id, identity)
        if version.status != "completed":
            raise HTTPException(
                status_code=409,
                detail="only completed knowledge-base versions can sync to Apache AGE",
            )

        graph_config = self._prefer_age_graph_target(
            self._resolve_graph_config(version.graph_config),
            explicit=False,
            promote_default_retrieval_when_upgrading=True,
        )
        self._assert_graph_target_supported(graph_config)

        source_rows = (
            session.execute(
                select(CaliberKnowledgeBaseSource)
                .where(CaliberKnowledgeBaseSource.knowledge_base_version_id == version_id)
                .order_by(CaliberKnowledgeBaseSource.object_key.asc())
            )
            .scalars()
            .all()
        )
        chunk_rows = (
            session.execute(
                select(CaliberKnowledgeBaseChunk)
                .where(CaliberKnowledgeBaseChunk.knowledge_base_version_id == version_id)
                .order_by(CaliberKnowledgeBaseChunk.ordinal.asc())
            )
            .scalars()
            .all()
        )
        if not chunk_rows:
            raise HTTPException(
                status_code=409,
                detail="knowledge-base version has no chunks available for Apache AGE sync",
            )

        entity_rows = self._list_entities_for_version(
            session,
            version_id=version.knowledge_base_version_id,
        )
        relationship_rows = self._list_relationships_for_version(
            session,
            version_id=version.knowledge_base_version_id,
        )
        document_ids_with_chunks = {row.document_id for row in chunk_rows}
        return _AgeSyncInputs(
            graph_config=graph_config,
            knowledge_base_id=knowledge_base.knowledge_base_id,
            knowledge_base_name=knowledge_base.name,
            source_bucket=knowledge_base.source_bucket,
            version_number=version.version_number,
            source_manifest=list(version.source_manifest or []),
            chunking_strategy=version.chunking_strategy,
            embedding_model=version.embedding_model,
            chunking_config=dict(version.chunking_config or {}),
            output_bucket=version.output_bucket,
            output_prefix=version.output_prefix,
            version_status=version.status,
            version_error_summary=version.error_summary,
            age_documents=[
                AgeDocumentNode(
                    document_id=row.document_id,
                    object_key=row.object_key,
                    object_name=row.object_name,
                    object_store_path=row.object_store_path,
                )
                for row in source_rows
                if row.status == "processed" and row.document_id in document_ids_with_chunks
            ],
            chunk_exports=[
                {
                    "chunk_id": row.knowledge_base_chunk_id,
                    "document_id": row.document_id,
                    "source_bucket": row.source_bucket,
                    "source_key": row.source_key,
                    "source_name": row.source_name,
                    "object_store_path": _object_store_path(row.source_bucket, row.source_key),
                    "chunk_index": row.chunk_index,
                    "ordinal": row.ordinal,
                    "content": row.content,
                    "metadata": dict(row.chunk_metadata or {}),
                    "start_index": row.start_index,
                    "end_index": row.end_index,
                    "token_count": row.token_count,
                    "char_count": row.char_count,
                    "embedding": [float(value) for value in list(row.embedding or [])],
                }
                for row in chunk_rows
            ],
            entity_exports=[row.model_dump() for row in entity_rows],
            relationship_exports=[row.model_dump() for row in relationship_rows],
        )

    def _age_sync_summary(self, age_sync: AgeSyncResult) -> dict[str, Any]:
        return {
            "status": age_sync.status,
            "graph_name": age_sync.graph_name,
            "node_count": age_sync.node_count,
            "edge_count": age_sync.edge_count,
            "error": age_sync.error,
        }

    def _persist_age_sync_result(
        self,
        *,
        version_id: str,
        inputs: _AgeSyncInputs,
        age_sync: AgeSyncResult,
        age_sync_summary: dict[str, Any],
        sync_at: datetime,
        actor: str,
    ) -> _AgeSyncPersistResult:
        with self._session_factory() as session:
            version_row = session.get(CaliberKnowledgeBaseVersion, version_id)
            if version_row is None:
                raise HTTPException(
                    status_code=404, detail=f"knowledge-base version {version_id!r} not found"
                )
            current_summary = dict(version_row.summary or {})
            graph_profile = self._graph_build_preset_match(
                inputs.graph_config,
                age_available=self._age_available(),
            )
            current_summary.update(
                {
                    "graph_output_target": inputs.graph_config.output_target,
                    "graph_default_retrieval_mode": inputs.graph_config.default_retrieval_mode,
                    "graph_retrieval_strength": inputs.graph_config.retrieval_strength,
                    "graph_age_seed_mode": inputs.graph_config.age_seed_mode,
                    "graph_entity_types": list(inputs.graph_config.entity_types),
                    "graph_max_entities_per_chunk": inputs.graph_config.max_entities_per_chunk,
                    "graph_minimum_entity_mentions": inputs.graph_config.minimum_entity_mentions,
                    "graph_minimum_relationship_weight": inputs.graph_config.minimum_relationship_weight,
                    "graph_age_traversal_hops": inputs.graph_config.age_traversal_hops,
                    "graph_age_candidate_pool_size": inputs.graph_config.age_candidate_pool_size,
                    "graph_age_dense_rerank_weight": inputs.graph_config.age_dense_rerank_weight,
                    "graph_strict_age_retrieval_default": inputs.graph_config.strict_age_retrieval_default,
                    "graph_profile_id": graph_profile.id if graph_profile is not None else "custom",
                    "graph_profile_label": (
                        graph_profile.label if graph_profile is not None else "Custom graph profile"
                    ),
                    "age_sync_status": age_sync.status,
                    "age_graph_name": age_sync.graph_name,
                    "age_synced_nodes": age_sync.node_count,
                    "age_synced_edges": age_sync.edge_count,
                    "age_sync_attempted_at": sync_at.isoformat(),
                }
            )
            if age_sync.status == "synced":
                current_summary["age_sync_completed_at"] = sync_at.isoformat()
            if age_sync.error:
                current_summary["age_sync_error"] = age_sync.error
            else:
                current_summary.pop("age_sync_error", None)
            version_row.graph_config = inputs.graph_config.model_dump()
            version_row.summary = current_summary
            audit_record(
                session,
                actor=actor,
                action="sync_knowledge_base_version_to_age",
                entity_type="knowledge_base_version",
                entity_id=version_id,
                details={
                    "knowledge_base_id": inputs.knowledge_base_id,
                    "version_number": inputs.version_number,
                    "graph_config": inputs.graph_config.model_dump(),
                    "age_sync": age_sync_summary,
                },
            )
            session.commit()
            session.refresh(version_row)
            return _AgeSyncPersistResult(
                version=self._serialize_version(version_row),
                summary=current_summary,
            )

    # ------------------------------------------------------------------
    # Query / compare
    # ------------------------------------------------------------------
    def query(
        self, payload: KnowledgeQueryRequest, *, identity: CaliberIdentity
    ) -> KnowledgeQueryResultSchema:
        version_results: list[KnowledgeQueryVersionResultSchema] = []
        tracer = get_tracer()
        requested_modes: list[KnowledgeRetrievalMode] = list(dict.fromkeys(payload.retrieval_modes))
        query_graph_overrides = (
            payload.graph_overrides
            if payload.graph_overrides is not None and payload.graph_overrides.is_active()
            else None
        )
        with self._session_factory() as session:
            requested = list(dict.fromkeys(payload.version_ids))
            pairs = [
                self._require_visible_version(session, version_id, identity)
                for version_id in requested
            ]
            for version, knowledge_base in pairs:
                version_graph_config = self._resolve_graph_config(version.graph_config)
                version_requested_modes = self._resolve_query_retrieval_modes(
                    version=version,
                    requested_modes=requested_modes,
                    graph_config=version_graph_config,
                )
                age_ready = self._version_age_ready(version, graph_config=version_graph_config)
                if version.status != "completed":
                    for mode in version_requested_modes:
                        started = perf_counter()
                        version_results.append(
                            KnowledgeQueryVersionResultSchema(
                                knowledge_base_version_id=version.knowledge_base_version_id,
                                knowledge_base_id=knowledge_base.knowledge_base_id,
                                version_number=version.version_number,
                                knowledge_base_name=knowledge_base.name,
                                chunking_strategy=version.chunking_strategy,
                                embedding_model=version.embedding_model,
                                retrieval_mode=mode,
                                answer=None,
                                answer_error="Version is not ready for retrieval.",
                                retrieved_chunks=[],
                                citations=[],
                                graph_context=self._finalize_query_graph_context(
                                    {"matched_entities": [], "expanded_entities": []},
                                    graph_config=self._apply_query_graph_overrides(
                                        version_graph_config,
                                        query_graph_overrides,
                                    ),
                                    graph_overrides=query_graph_overrides,
                                    retrieval_mode=mode,
                                    age_ready=age_ready,
                                ),
                                timing_ms={"total": round((perf_counter() - started) * 1000, 2)},
                            )
                        )
                    continue
                chunks, dense_scores = self._load_dense_candidates(
                    session, version, payload.question
                )
                graph_config = self._apply_query_graph_overrides(
                    version_graph_config,
                    query_graph_overrides,
                )
                entities, relationships = self._load_graph_context(
                    session, version, version_requested_modes
                )
                # Two-stage retrieval: when reranking is on, the first stage
                # gathers a larger candidate pool that the cross-encoder reorders
                # down to ``payload.top_k``.
                rerank_enabled = self._config.knowledge_rerank_enabled
                effective_k = (
                    max(payload.top_k, self._config.knowledge_ann_candidate_pool_size)
                    if rerank_enabled
                    else payload.top_k
                )
                for mode in version_requested_modes:
                    with tracer.span(
                        f"rag.query.{mode}",
                        span_type="RETRIEVER",
                        attributes={
                            "caliber.rag.mode": mode,
                            "caliber.rag.top_k": payload.top_k,
                            "caliber.rag.kb_id": knowledge_base.knowledge_base_id,
                            "caliber.rag.version": version.version_number,
                            "caliber.rag.embedding_model": version.embedding_model,
                            "caliber.rag.question": payload.question,
                        },
                    ) as mode_span:
                        started = perf_counter()
                        retrieval_started = perf_counter()
                        if mode == "graph_hybrid":
                            top, graph_context = self._retrieve_graph_hybrid(
                                question=payload.question,
                                chunks=chunks,
                                dense_scores=dense_scores,
                                entities=entities,
                                relationships=relationships,
                                graph_config=graph_config,
                                top_k=effective_k,
                            )
                        elif mode == "age_graph":
                            top, graph_context = self._retrieve_age_graph(
                                question=payload.question,
                                version=version,
                                chunks=chunks,
                                dense_scores=dense_scores,
                                entities=entities,
                                relationships=relationships,
                                graph_config=graph_config,
                                top_k=effective_k,
                                strict_age_retrieval=bool(
                                    query_graph_overrides.strict_age_retrieval
                                    if query_graph_overrides is not None
                                    else graph_config.strict_age_retrieval_default
                                ),
                            )
                        elif mode == "hybrid":
                            lexical_scores = self._lexical_scores(chunks, payload.question)
                            top, graph_context = self._retrieve_hybrid(
                                chunks=chunks,
                                dense_scores=dense_scores,
                                lexical_scores=lexical_scores,
                                top_k=effective_k,
                            )
                        else:
                            top, graph_context = self._retrieve_dense(
                                chunks=chunks,
                                dense_scores=dense_scores,
                                top_k=effective_k,
                            )
                        if rerank_enabled:
                            top = self._rerank(payload.question, top, payload.top_k)
                        retrieval_ms = (perf_counter() - retrieval_started) * 1000
                        retrieved_chunks = [
                            KnowledgeQueryChunkSchema(
                                chunk_id=item.chunk.knowledge_base_chunk_id,
                                source_bucket=item.chunk.source_bucket,
                                source_key=item.chunk.source_key,
                                source_name=item.chunk.source_name,
                                score=round(item.score, 6),
                                content=item.chunk.content,
                                chunk_index=item.chunk.chunk_index,
                                ordinal=item.chunk.ordinal,
                                document_id=item.chunk.document_id,
                                metadata=dict(item.chunk.chunk_metadata or {}),
                                object_store_path=_object_store_path(
                                    item.chunk.source_bucket, item.chunk.source_key
                                ),
                                score_breakdown={
                                    key: round(float(value), 6)
                                    for key, value in item.score_breakdown.items()
                                },
                                matched_entity_labels=list(item.matched_entities),
                            )
                            for item in top
                        ]
                        answer_started = perf_counter()
                        answer, answer_error = self._generate_answer(
                            question=payload.question,
                            history=[message.model_dump() for message in payload.history],
                            knowledge_base=knowledge_base,
                            version=version,
                            retrieved_chunks=retrieved_chunks,
                            chat_model=payload.chat_model,
                        )
                        answer_ms = (perf_counter() - answer_started) * 1000
                        citations = [
                            KnowledgeQueryCitationSchema(
                                chunk_id=item.chunk_id,
                                label=f"[{index + 1}] {item.source_name}",
                                source_bucket=item.source_bucket,
                                source_key=item.source_key,
                                object_store_path=item.object_store_path,
                                score=item.score,
                            )
                            for index, item in enumerate(
                                retrieved_chunks[: min(4, len(retrieved_chunks))]
                            )
                        ]
                        mode_span.set_attribute(
                            "caliber.rag.retrieved_count", len(retrieved_chunks)
                        )
                        mode_span.set_attribute(
                            "caliber.rag.scores", [c.score for c in retrieved_chunks]
                        )
                        mode_span.set_attribute("caliber.rag.retrieval_ms", round(retrieval_ms, 3))
                        mode_span.set_attribute("caliber.rag.generation_ms", round(answer_ms, 3))
                        if answer_error:
                            mode_span.set_attribute("caliber.rag.answer_error", answer_error)
                        mode_span.set_attribute(
                            "caliber.rag.citations", [c.label for c in citations]
                        )
                        version_results.append(
                            KnowledgeQueryVersionResultSchema(
                                knowledge_base_version_id=version.knowledge_base_version_id,
                                knowledge_base_id=knowledge_base.knowledge_base_id,
                                version_number=version.version_number,
                                knowledge_base_name=knowledge_base.name,
                                chunking_strategy=version.chunking_strategy,
                                embedding_model=version.embedding_model,
                                retrieval_mode=mode,
                                answer=answer,
                                answer_error=answer_error,
                                citations=citations,
                                retrieved_chunks=retrieved_chunks,
                                graph_context=self._finalize_query_graph_context(
                                    graph_context,
                                    graph_config=graph_config,
                                    graph_overrides=query_graph_overrides,
                                    retrieval_mode=mode,
                                    age_ready=age_ready,
                                ),
                                timing_ms={
                                    "retrieval": round(retrieval_ms, 2),
                                    "generation": round(answer_ms, 2),
                                    "total": round((perf_counter() - started) * 1000, 2),
                                },
                            )
                        )
        return KnowledgeQueryResultSchema(question=payload.question, versions=version_results)

    def _resolve_query_retrieval_modes(
        self,
        *,
        version: CaliberKnowledgeBaseVersion,
        requested_modes: list[KnowledgeRetrievalMode],
        graph_config: KnowledgeGraphConfigSchema | None = None,
    ) -> list[KnowledgeRetrievalMode]:
        if requested_modes:
            return requested_modes
        resolved_graph_config = graph_config or self._resolve_graph_config(version.graph_config)
        default_mode = resolved_graph_config.default_retrieval_mode
        if default_mode == "age_graph" and not self._version_age_ready(
            version,
            graph_config=resolved_graph_config,
        ):
            return ["graph_hybrid"]
        return [default_mode]

    def _load_dense_candidates(
        self,
        session: Session,
        version: CaliberKnowledgeBaseVersion,
        question: str,
    ) -> tuple[list[CaliberKnowledgeBaseChunk], dict[str, float]]:
        """Load the chunk working-set + dense scores for a query.

        Two paths, same return shape:

        * **pgvector (Postgres + flag on)** — a SQL-side ANN top-k narrows to a
          candidate pool; only those chunks are materialized and their dense
          scores come from the DB-computed cosine distance. Scales to millions.
        * **fallback (SQLite / flag off)** — load every chunk for the version and
          score in Python (the original behaviour the tests pin).
        """
        embedder = self._embedding_backend(version.embedding_model)
        query_vector = embedder.embed_query(question)
        version_id = version.knowledge_base_version_id

        if self._config.knowledge_pgvector_enabled and pgvector_ann.is_postgres(session):
            pool = max(1, self._config.knowledge_ann_candidate_pool_size)
            candidates = pgvector_ann.dense_candidate_ids(
                session, version_id=version_id, query_vector=query_vector, pool_size=pool
            )
            if candidates:
                by_id = dict(candidates)
                chunks = (
                    session.execute(
                        select(CaliberKnowledgeBaseChunk).where(
                            CaliberKnowledgeBaseChunk.knowledge_base_chunk_id.in_(list(by_id))
                        )
                    )
                    .scalars()
                    .all()
                )
                # Cosine distance (``<=>``) → similarity, matching ``_cosine_similarity``.
                dense_scores = {
                    chunk.knowledge_base_chunk_id: 1.0 - by_id[chunk.knowledge_base_chunk_id]
                    for chunk in chunks
                }
                return list(chunks), dense_scores
            # No vectors populated yet (e.g. pre-backfill) → fall through to the scan.

        chunks = (
            session.execute(
                select(CaliberKnowledgeBaseChunk).where(
                    CaliberKnowledgeBaseChunk.knowledge_base_version_id == version_id
                )
            )
            .scalars()
            .all()
        )
        return list(chunks), self._dense_scores(chunks, query_vector)

    def _load_graph_context(
        self,
        session: Session,
        version: CaliberKnowledgeBaseVersion,
        requested_modes: Sequence[KnowledgeRetrievalMode],
    ) -> tuple[
        Sequence[CaliberKnowledgeBaseEntity],
        Sequence[CaliberKnowledgeBaseRelationship],
    ]:
        """Load entities + relationships for a version, but only for graph modes."""
        if not ({"graph_hybrid", "age_graph"} & set(requested_modes)):
            return [], []
        version_id = version.knowledge_base_version_id
        entities = (
            session.execute(
                select(CaliberKnowledgeBaseEntity).where(
                    CaliberKnowledgeBaseEntity.knowledge_base_version_id == version_id
                )
            )
            .scalars()
            .all()
        )
        relationships = (
            session.execute(
                select(CaliberKnowledgeBaseRelationship).where(
                    CaliberKnowledgeBaseRelationship.knowledge_base_version_id == version_id
                )
            )
            .scalars()
            .all()
        )
        return entities, relationships

    def _dense_scores(
        self,
        chunks: Sequence[CaliberKnowledgeBaseChunk],
        query_vector: list[float],
    ) -> dict[str, float]:
        scores: dict[str, float] = {}
        for chunk in chunks:
            if not chunk.embedding:
                continue
            scores[chunk.knowledge_base_chunk_id] = _cosine_similarity(
                query_vector,
                [float(value) for value in list(chunk.embedding or [])],
            )
        return scores

    def _lexical_scores(
        self,
        chunks: Sequence[CaliberKnowledgeBaseChunk],
        query: str,
    ) -> dict[str, float]:
        """Pure-Python BM25 over chunk content for the in-memory chunk set.

        Builds document frequencies in a single pass, then scores each chunk in a
        second pass. Returns ``{chunk_id: bm25_score}``; chunks with no query-term
        overlap are absent (treated as ``0``). Deterministic for a fixed input set.
        """

        query_terms = list(dict.fromkeys(_lexical_tokens(query)))
        if not query_terms:
            return {}

        tokenized: list[tuple[str, list[str]]] = [
            (chunk.knowledge_base_chunk_id, _lexical_tokens(chunk.content or ""))
            for chunk in chunks
        ]
        non_empty = [(chunk_id, tokens) for chunk_id, tokens in tokenized if tokens]
        document_count = len(non_empty)
        if document_count == 0:
            return {}

        document_frequency: dict[str, int] = defaultdict(int)
        for _chunk_id, tokens in non_empty:
            for term in set(tokens):
                if term in query_terms:
                    document_frequency[term] += 1

        # BM25 inverse document frequency (Robertson/Sparck-Jones, non-negative form).
        idf: dict[str, float] = {}
        for term in query_terms:
            freq = document_frequency.get(term, 0)
            idf[term] = math.log(1.0 + (document_count - freq + 0.5) / (freq + 0.5))

        average_length = sum(len(tokens) for _chunk_id, tokens in non_empty) / document_count

        scores: dict[str, float] = {}
        for chunk_id, tokens in non_empty:
            score = self._bm25_document_score(
                tokens=tokens,
                idf=idf,
                average_length=average_length,
            )
            if score > 0:
                scores[chunk_id] = score
        return scores

    @staticmethod
    def _bm25_document_score(
        *,
        tokens: list[str],
        idf: dict[str, float],
        average_length: float,
    ) -> float:
        """BM25 score for one document given precomputed query-term IDFs."""

        term_counts: dict[str, int] = defaultdict(int)
        for term in tokens:
            if term in idf:
                term_counts[term] += 1
        if not term_counts:
            return 0.0
        length_ratio = (len(tokens) / average_length) if average_length else 0.0
        normalizer = _BM25_K1 * (1.0 - _BM25_B + _BM25_B * length_ratio)
        score = 0.0
        for term, count in term_counts.items():
            denominator = count + normalizer
            if denominator > 0:
                score += idf[term] * (count * (_BM25_K1 + 1.0)) / denominator
        return score

    @staticmethod
    def _reciprocal_rank_fusion(
        *ranked: list[tuple[str, float]],
        k: int = _RRF_K,
    ) -> dict[str, float]:
        """Fuse score-ranked lists into RRF scores: ``sum(1 / (k + rank))``.

        Each argument is a list of ``(chunk_id, score)`` already ordered by score
        descending; only the rank position (1-based) contributes, so the fusion is
        agnostic to the differing scales of cosine similarity and BM25.
        """

        fused: dict[str, float] = defaultdict(float)
        for ranking in ranked:
            for rank, (chunk_id, _score) in enumerate(ranking, start=1):
                fused[chunk_id] += 1.0 / (k + rank)
        return dict(fused)

    def _retrieve_hybrid(
        self,
        *,
        chunks: Sequence[CaliberKnowledgeBaseChunk],
        dense_scores: dict[str, float],
        lexical_scores: dict[str, float],
        top_k: int,
    ) -> tuple[list[_RetrievedChunk], dict[str, Any]]:
        dense_ranking = sorted(
            dense_scores.items(),
            key=lambda item: (item[1], item[0]),
            reverse=True,
        )
        lexical_ranking = sorted(
            lexical_scores.items(),
            key=lambda item: (item[1], item[0]),
            reverse=True,
        )
        fused = self._reciprocal_rank_fusion(dense_ranking, lexical_ranking)
        chunk_by_id = {chunk.knowledge_base_chunk_id: chunk for chunk in chunks}
        scored: list[_RetrievedChunk] = []
        for chunk_id, rrf_score in fused.items():
            chunk = chunk_by_id.get(chunk_id)
            if chunk is None:
                continue
            dense_score = dense_scores.get(chunk_id, 0.0)
            lexical_score = lexical_scores.get(chunk_id, 0.0)
            scored.append(
                _RetrievedChunk(
                    chunk=chunk,
                    score=rrf_score,
                    dense_score=dense_score,
                    score_breakdown={
                        "dense": dense_score,
                        "lexical": lexical_score,
                        "rrf": rrf_score,
                    },
                )
            )
        scored.sort(
            key=lambda item: (item.score, item.dense_score, -item.chunk.ordinal),
            reverse=True,
        )
        return scored[:top_k], {
            "matched_entities": [],
            "expanded_entities": [],
            "lexical_matched_chunk_count": len(lexical_scores),
            "dense_matched_chunk_count": len(dense_scores),
            "fused_chunk_count": len(fused),
        }

    def _retrieve_dense(
        self,
        *,
        chunks: Sequence[CaliberKnowledgeBaseChunk],
        dense_scores: dict[str, float],
        top_k: int,
    ) -> tuple[list[_RetrievedChunk], dict[str, Any]]:
        scored = [
            _RetrievedChunk(
                chunk=chunk,
                score=dense_scores[chunk.knowledge_base_chunk_id],
                dense_score=dense_scores[chunk.knowledge_base_chunk_id],
                score_breakdown={
                    "dense": dense_scores[chunk.knowledge_base_chunk_id],
                },
            )
            for chunk in chunks
            if chunk.knowledge_base_chunk_id in dense_scores
        ]
        scored.sort(
            key=lambda item: (item.score, -item.chunk.ordinal),
            reverse=True,
        )
        return scored[:top_k], {"matched_entities": [], "expanded_entities": []}

    def _retrieve_graph_hybrid(
        self,
        *,
        question: str,
        chunks: Sequence[CaliberKnowledgeBaseChunk],
        dense_scores: dict[str, float],
        entities: Sequence[CaliberKnowledgeBaseEntity],
        relationships: Sequence[CaliberKnowledgeBaseRelationship],
        graph_config: KnowledgeGraphConfigSchema,
        top_k: int,
    ) -> tuple[list[_RetrievedChunk], dict[str, Any]]:
        lexical_scores = self._lexical_scores(chunks, question)
        max_lexical = max(lexical_scores.values(), default=0.0)
        question_bundle = self._question_graph_bundle(question=question, graph_config=graph_config)
        matched = self._match_query_entities(
            question=question,
            entities=entities,
            query_entity_keys={item.entity_key for item in question_bundle.entities},
        )
        if not matched:
            # No graph anchor: degrade to the dense+lexical fusion so keyword matches
            # still surface, but keep the graph-context response shape unchanged.
            fused_scored, _ = self._retrieve_hybrid(
                chunks=chunks,
                dense_scores=dense_scores,
                lexical_scores=lexical_scores,
                top_k=top_k,
            )
            return fused_scored, {
                "requested_backend": question_bundle.metadata.get("requested_backend"),
                "applied_backend": question_bundle.metadata.get("applied_backend"),
                "spacy_model": question_bundle.metadata.get("spacy_model"),
                "fallback_reason": question_bundle.metadata.get("fallback_reason"),
                "query_entities": [item.label for item in question_bundle.entities],
                "matched_entities": [],
                "expanded_entities": [],
                "boosted_chunk_count": 0,
                "relationship_count": len(relationships),
                "lexical_matched_chunk_count": len(lexical_scores),
            }

        entity_by_id = {entity.knowledge_base_entity_id: entity for entity in entities}
        adjacency: dict[str, list[CaliberKnowledgeBaseRelationship]] = defaultdict(list)
        max_relationship_weight = max((float(item.weight) for item in relationships), default=1.0)
        for relationship in relationships:
            adjacency[relationship.source_entity_id].append(relationship)
            adjacency[relationship.target_entity_id].append(relationship)

        chunk_boosts: dict[str, float] = defaultdict(float)
        chunk_labels: dict[str, set[str]] = defaultdict(set)
        expanded_labels: set[str] = set()
        matched_labels = {item.entity.label for item in matched}
        for item in matched:
            direct_boost = 0.22 + min(0.38, item.score * 0.16)
            for chunk_id in item.entity.source_chunks or []:
                chunk_boosts[chunk_id] += direct_boost
                chunk_labels[chunk_id].add(item.entity.label)
            for relationship in adjacency.get(item.entity.knowledge_base_entity_id, []):
                neighbor_id = (
                    relationship.target_entity_id
                    if relationship.source_entity_id == item.entity.knowledge_base_entity_id
                    else relationship.source_entity_id
                )
                neighbor = entity_by_id.get(neighbor_id)
                if neighbor is None:
                    continue
                relationship_factor = (
                    min(1.0, float(relationship.weight) / max_relationship_weight)
                    if max_relationship_weight > 0
                    else 0.0
                )
                neighbor_boost = max(0.08, item.score * 0.14 * relationship_factor)
                for chunk_id in neighbor.source_chunks or []:
                    chunk_boosts[chunk_id] += neighbor_boost
                    chunk_labels[chunk_id].add(item.entity.label)
                    chunk_labels[chunk_id].add(neighbor.label)
                for chunk_id in relationship.evidence_chunk_ids or []:
                    chunk_boosts[chunk_id] += neighbor_boost * 0.5
                    chunk_labels[chunk_id].add(item.entity.label)
                    chunk_labels[chunk_id].add(neighbor.label)
                if neighbor.label not in matched_labels:
                    expanded_labels.add(neighbor.label)

        scored: list[_RetrievedChunk] = []
        for chunk in chunks:
            chunk_id = chunk.knowledge_base_chunk_id
            dense_score = dense_scores.get(chunk_id, 0.0)
            graph_boost = min(1.25, chunk_boosts.get(chunk_id, 0.0))
            lexical_boost = (
                0.0
                if max_lexical <= 0
                else min(
                    _GRAPH_LEXICAL_BOOST_CAP,
                    (lexical_scores.get(chunk_id, 0.0) / max_lexical) * _GRAPH_LEXICAL_BOOST_WEIGHT,
                )
            )
            if dense_score <= 0 and graph_boost <= 0 and lexical_boost <= 0:
                continue
            scored.append(
                _RetrievedChunk(
                    chunk=chunk,
                    score=dense_score + graph_boost + lexical_boost,
                    dense_score=dense_score,
                    graph_boost=graph_boost,
                    matched_entities=tuple(sorted(chunk_labels.get(chunk_id, set()))),
                    score_breakdown={
                        "dense": dense_score,
                        "graph_boost": graph_boost,
                        "lexical": lexical_scores.get(chunk_id, 0.0),
                    },
                )
            )
        scored.sort(
            key=lambda item: (item.score, item.graph_boost, item.dense_score, -item.chunk.ordinal),
            reverse=True,
        )
        return scored[:top_k], {
            "requested_backend": question_bundle.metadata.get("requested_backend"),
            "applied_backend": question_bundle.metadata.get("applied_backend"),
            "spacy_model": question_bundle.metadata.get("spacy_model"),
            "fallback_reason": question_bundle.metadata.get("fallback_reason"),
            "query_entities": [item.label for item in question_bundle.entities],
            "matched_entities": [item.entity.label for item in matched],
            "expanded_entities": sorted(expanded_labels),
            "boosted_chunk_count": sum(1 for value in chunk_boosts.values() if value > 0),
            "relationship_count": len(relationships),
            "lexical_matched_chunk_count": len(lexical_scores),
        }

    def _retrieve_age_graph(
        self,
        *,
        question: str,
        version: CaliberKnowledgeBaseVersion,
        chunks: Sequence[CaliberKnowledgeBaseChunk],
        dense_scores: dict[str, float],
        entities: Sequence[CaliberKnowledgeBaseEntity],
        relationships: Sequence[CaliberKnowledgeBaseRelationship],
        graph_config: KnowledgeGraphConfigSchema,
        top_k: int,
        strict_age_retrieval: bool = False,
    ) -> tuple[list[_RetrievedChunk], dict[str, Any]]:
        question_bundle = self._question_graph_bundle(question=question, graph_config=graph_config)
        query_entity_keys = [item.entity_key for item in question_bundle.entities]
        local_matches = self._match_query_entities(
            question=question,
            entities=entities,
            query_entity_keys=set(query_entity_keys),
        )
        matched_entity_labels = [item.entity.label for item in local_matches]
        if local_matches:
            query_entity_keys = [item.entity.entity_key for item in local_matches]
        base_context: dict[str, Any] = {
            "requested_backend": question_bundle.metadata.get("requested_backend"),
            "applied_backend": question_bundle.metadata.get("applied_backend"),
            "spacy_model": question_bundle.metadata.get("spacy_model"),
            "fallback_reason": question_bundle.metadata.get("fallback_reason"),
            "query_entities": [item.label for item in question_bundle.entities],
            "matched_entities": matched_entity_labels,
            "expanded_entities": [],
            "boosted_chunk_count": 0,
            "relationship_count": len(relationships),
            "age_graph_name": self._age_store.graph_name,
            "age_status": "fallback",
            "age_fallback_reason": None,
            "age_traversal_hops": 0,
            "age_matched_chunk_count": 0,
            "retrieval_strength": graph_config.retrieval_strength,
            "age_configured_seed_mode": graph_config.age_seed_mode,
            "age_configured_hops": graph_config.age_traversal_hops,
            "age_candidate_pool_size": graph_config.age_candidate_pool_size,
            "age_dense_rerank_weight": graph_config.age_dense_rerank_weight,
            "strict_age_retrieval": strict_age_retrieval,
            "age_seed_strategy": None,
        }
        if not self._version_age_ready(version, graph_config=graph_config):
            reason = self._age_unavailable_reason(version, graph_config=graph_config)
            return self._age_graph_failure(
                strict_age_retrieval=strict_age_retrieval,
                base_context=base_context,
                reason=reason,
                question=question,
                chunks=chunks,
                dense_scores=dense_scores,
                entities=entities,
                relationships=relationships,
                graph_config=graph_config,
                top_k=top_k,
            )

        age_result = self._age_store.retrieve(
            version_id=version.knowledge_base_version_id,
            query_entity_keys=query_entity_keys,
            retrieval_strength=graph_config.retrieval_strength,
            minimum_relationship_weight=graph_config.minimum_relationship_weight,
            top_k=top_k,
            traversal_hops=graph_config.age_traversal_hops,
            candidate_pool_size=graph_config.age_candidate_pool_size,
            seed_mode=graph_config.age_seed_mode,
            query_text=question,
        )
        self._apply_age_retrieval_context(base_context, age_result)

        if age_result.status != "ok":
            return self._age_graph_failure(
                strict_age_retrieval=strict_age_retrieval,
                base_context=base_context,
                reason=age_result.fallback_reason
                or "Apache AGE retrieval did not return chunk candidates.",
                question=question,
                chunks=chunks,
                dense_scores=dense_scores,
                entities=entities,
                relationships=relationships,
                graph_config=graph_config,
                top_k=top_k,
            )

        scored = self._score_age_chunk_candidates(
            chunks=chunks,
            dense_scores=dense_scores,
            age_result=age_result,
            graph_config=graph_config,
        )
        if not scored:
            return self._age_graph_failure(
                strict_age_retrieval=strict_age_retrieval,
                base_context=base_context,
                reason="Apache AGE returned chunk references that were not present in this version snapshot.",
                question=question,
                chunks=chunks,
                dense_scores=dense_scores,
                entities=entities,
                relationships=relationships,
                graph_config=graph_config,
                top_k=top_k,
            )
        scored.sort(
            key=lambda item: (item.score, item.graph_boost, item.dense_score, -item.chunk.ordinal),
            reverse=True,
        )
        base_context["boosted_chunk_count"] = len(scored)
        return scored[:top_k], base_context

    def _age_graph_failure(
        self,
        *,
        strict_age_retrieval: bool,
        base_context: dict[str, Any],
        reason: str,
        question: str,
        chunks: Sequence[CaliberKnowledgeBaseChunk],
        dense_scores: dict[str, float],
        entities: Sequence[CaliberKnowledgeBaseEntity],
        relationships: Sequence[CaliberKnowledgeBaseRelationship],
        graph_config: KnowledgeGraphConfigSchema,
        top_k: int,
    ) -> tuple[list[_RetrievedChunk], dict[str, Any]]:
        if strict_age_retrieval:
            return [], {
                **base_context,
                "age_fallback_reason": reason,
            }
        return self._age_graph_fallback(
            question=question,
            chunks=chunks,
            dense_scores=dense_scores,
            entities=entities,
            relationships=relationships,
            graph_config=graph_config,
            top_k=top_k,
            graph_context=base_context,
            reason=reason,
        )

    def _apply_age_retrieval_context(
        self,
        base_context: dict[str, Any],
        age_result: AgeRetrievalResult,
    ) -> None:
        base_context["age_status"] = age_result.status
        base_context["age_graph_name"] = age_result.graph_name
        base_context["age_fallback_reason"] = age_result.fallback_reason
        base_context["age_traversal_hops"] = age_result.traversal_hops
        base_context["age_matched_chunk_count"] = age_result.matched_chunk_count
        base_context["age_seed_strategy"] = age_result.seed_strategy
        if age_result.matched_entities:
            base_context["matched_entities"] = list(age_result.matched_entities)
        if age_result.expanded_entities:
            base_context["expanded_entities"] = list(age_result.expanded_entities)

    def _score_age_chunk_candidates(
        self,
        *,
        chunks: Sequence[CaliberKnowledgeBaseChunk],
        dense_scores: dict[str, float],
        age_result: AgeRetrievalResult,
        graph_config: KnowledgeGraphConfigSchema,
    ) -> list[_RetrievedChunk]:
        chunk_by_id = {chunk.knowledge_base_chunk_id: chunk for chunk in chunks}
        max_graph_score = max(
            (item.graph_score for item in age_result.chunk_candidates), default=0.0
        )
        scored: list[_RetrievedChunk] = []
        for candidate in age_result.chunk_candidates:
            chunk = chunk_by_id.get(candidate.chunk_id)
            if chunk is None:
                continue
            dense_score = dense_scores.get(candidate.chunk_id, 0.0)
            normalized_graph = (
                candidate.graph_score / max_graph_score if max_graph_score > 0 else 0.0
            )
            dense_rerank = dense_score * graph_config.age_dense_rerank_weight
            labels = tuple(
                sorted(set(candidate.matched_entities) | set(candidate.expanded_entities))
            )
            scored.append(
                _RetrievedChunk(
                    chunk=chunk,
                    score=dense_rerank + normalized_graph,
                    dense_score=dense_score,
                    graph_boost=normalized_graph,
                    matched_entities=labels,
                    score_breakdown={
                        "dense": dense_score,
                        "dense_rerank": dense_rerank,
                        "graph_boost": normalized_graph,
                        "age_graph": candidate.graph_score,
                        "age_direct": candidate.direct_score,
                        "age_one_hop": candidate.one_hop_score,
                        "age_two_hop": candidate.two_hop_score,
                    },
                )
            )
        return scored

    def _age_graph_fallback(
        self,
        *,
        question: str,
        chunks: Sequence[CaliberKnowledgeBaseChunk],
        dense_scores: dict[str, float],
        entities: Sequence[CaliberKnowledgeBaseEntity],
        relationships: Sequence[CaliberKnowledgeBaseRelationship],
        graph_config: KnowledgeGraphConfigSchema,
        top_k: int,
        graph_context: dict[str, Any],
        reason: str,
    ) -> tuple[list[_RetrievedChunk], dict[str, Any]]:
        if entities:
            top, fallback_context = self._retrieve_graph_hybrid(
                question=question,
                chunks=chunks,
                dense_scores=dense_scores,
                entities=entities,
                relationships=relationships,
                graph_config=graph_config,
                top_k=top_k,
            )
            fallback_mode = "graph_hybrid"
        else:
            top, fallback_context = self._retrieve_dense(
                chunks=chunks,
                dense_scores=dense_scores,
                top_k=top_k,
            )
            fallback_mode = "dense"
        merged_context = {
            **fallback_context,
            **graph_context,
            "matched_entities": fallback_context.get(
                "matched_entities", graph_context.get("matched_entities", [])
            ),
            "expanded_entities": fallback_context.get(
                "expanded_entities", graph_context.get("expanded_entities", [])
            ),
            "boosted_chunk_count": fallback_context.get(
                "boosted_chunk_count", graph_context.get("boosted_chunk_count", 0)
            ),
            "fallback_retrieval_mode": fallback_mode,
            "age_fallback_reason": reason,
        }
        if not merged_context.get("fallback_reason"):
            merged_context["fallback_reason"] = graph_context.get("fallback_reason")
        return top, merged_context

    def _match_query_entities(
        self,
        *,
        question: str,
        entities: Sequence[CaliberKnowledgeBaseEntity],
        query_entity_keys: set[str],
    ) -> list[_EntityMatch]:
        if not entities:
            return []
        normalized_question = _normalize_query_text(question)
        padded_question = f" {normalized_question} "
        question_terms = set(normalized_question.split())
        matches: list[_EntityMatch] = []
        for entity in entities:
            score = self._entity_match_score(
                entity=entity,
                padded_question=padded_question,
                question_terms=question_terms,
                query_entity_keys=query_entity_keys,
            )
            if score >= _GRAPH_ENTITY_MATCH_THRESHOLD:
                matches.append(_EntityMatch(entity=entity, score=score))
        matches.sort(
            key=lambda item: (
                item.score,
                item.entity.mention_count,
                len(item.entity.source_chunks or []),
            ),
            reverse=True,
        )
        return matches[:6]

    def _entity_match_score(
        self,
        *,
        entity: CaliberKnowledgeBaseEntity,
        padded_question: str,
        question_terms: set[str],
        query_entity_keys: set[str],
    ) -> float:
        score = 1.0 if entity.entity_key in query_entity_keys else 0.0
        aliases = [entity.label, *list(entity.aliases or [])]
        alias_score = 0.0
        for alias in aliases:
            alias_key = entity_key_for_label(alias)
            if not alias_key:
                continue
            alias_terms = set(alias_key.split("-"))
            alias_phrase = " ".join(alias_key.split("-"))
            if alias_phrase and f" {alias_phrase} " in padded_question:
                alias_score = max(alias_score, 1.2 if len(alias_terms) > 1 else 0.8)
                continue
            overlap = alias_terms & question_terms
            if not overlap:
                continue
            coverage = len(overlap) / max(1, len(alias_terms))
            if coverage == 1:
                alias_score = max(alias_score, 0.85)
            elif len(overlap) >= _ENTITY_PARTIAL_OVERLAP_MIN:
                alias_score = max(alias_score, 0.5 + (coverage * 0.2))
            elif len(alias_terms) == 1:
                alias_score = max(alias_score, 0.35)
        if alias_score == 0.0 and score == 0.0:
            return 0.0
        return score + alias_score + min(0.2, float(entity.mention_count) / 50.0)

    # ------------------------------------------------------------------
    # Internal build execution
    # ------------------------------------------------------------------
    def _build_result(
        self,
        knowledge_base_id: str,
        version_id: str,
        run_id: str,
        identity: CaliberIdentity,
    ) -> KnowledgeBaseBuildResultSchema:
        with self._session_factory() as session:
            knowledge_base = self._require_visible_knowledge_base(
                session, knowledge_base_id, identity
            )
            version = session.get(CaliberKnowledgeBaseVersion, version_id)
            run = session.get(CaliberKnowledgeBaseRun, run_id)
            if version is None or run is None:
                raise HTTPException(
                    status_code=500, detail="knowledge-base build rows were not persisted"
                )
            return KnowledgeBaseBuildResultSchema(
                knowledge_base=self._serialize_knowledge_base(knowledge_base, version=version),
                version=self._serialize_version(version),
                run=KnowledgeBaseRunSchema.model_validate(run),
            )

    def execute_run(
        self,
        run_id: str,
        *,
        worker_id: str | None = None,
        heartbeat: Callable[[], object] | None = None,
    ) -> None:
        with self._session_factory() as session:
            run = session.get(CaliberKnowledgeBaseRun, run_id)
            if run is None or run.status not in {"queued", "running"}:
                return
            version = session.get(CaliberKnowledgeBaseVersion, run.knowledge_base_version_id)
            knowledge_base = session.get(CaliberKnowledgeBase, run.knowledge_base_id)
            if version is None or knowledge_base is None:
                run.status = "failed"
                run.error_summary = "knowledge base or version not found"
                run.completed_at = _utcnow()
                session.commit()
                return
            initial_events = [
                (event.event_type, dict(event.payload or {}))
                for event in session.execute(
                    select(CaliberKnowledgeBaseRunEvent)
                    .where(CaliberKnowledgeBaseRunEvent.knowledge_base_run_id == run_id)
                    .order_by(CaliberKnowledgeBaseRunEvent.sequence.asc())
                )
                .scalars()
                .all()
            ]
            now = _utcnow()
            run.status = "running"
            run.started_at = run.started_at or now
            run.completed_at = None
            run.error_summary = None
            version.status = "processing"
            version.error_summary = None
            version.completed_at = None
            knowledge_base.last_run_status = "processing"
            session.commit()
            source_bucket = knowledge_base.source_bucket
            source_manifest = list(run.source_manifest or version.source_manifest or [])
            chunking_strategy = version.chunking_strategy
            embedding_model = version.embedding_model
            chunking_config = dict(version.chunking_config or {})
            graph_config = self._resolve_graph_config(version.graph_config)
            knowledge_base_id = knowledge_base.knowledge_base_id
            knowledge_base_name = knowledge_base.name
            version_id = version.knowledge_base_version_id
            version_number = version.version_number

        if heartbeat is not None:
            heartbeat()
        self._process_build(
            knowledge_base_id=knowledge_base_id,
            knowledge_base_name=knowledge_base_name,
            version_id=version_id,
            version_number=version_number,
            run_id=run_id,
            source_bucket=source_bucket,
            source_manifest=source_manifest,
            chunking_strategy=chunking_strategy,
            embedding_model=embedding_model,
            chunking_config=chunking_config,
            graph_config=graph_config,
            initial_events=initial_events,
            worker_id=worker_id,
            heartbeat=heartbeat,
        )

    def _process_build(  # noqa: PLR0912, PLR0915
        self,
        *,
        knowledge_base_id: str,
        knowledge_base_name: str,
        version_id: str,
        version_number: int,
        run_id: str,
        source_bucket: str,
        source_manifest: list[dict[str, str]],
        chunking_strategy: str,
        embedding_model: str,
        chunking_config: dict[str, object],
        graph_config: KnowledgeGraphConfigSchema,
        initial_events: list[tuple[str, dict[str, Any]]] | None = None,
        worker_id: str | None = None,
        heartbeat: Callable[[], object] | None = None,
    ) -> None:
        events: list[tuple[str, dict[str, Any]]] = list(initial_events or [])
        source_rows: list[CaliberKnowledgeBaseSource] = []
        chunk_rows: list[CaliberKnowledgeBaseChunk] = []
        entity_rows: list[CaliberKnowledgeBaseEntity] = []
        relationship_rows: list[CaliberKnowledgeBaseRelationship] = []
        chunk_exports: list[dict[str, Any]] = []
        entity_exports: list[dict[str, Any]] = []
        relationship_exports: list[dict[str, Any]] = []
        graph_export: dict[str, Any] | None = None
        graph_metadata: dict[str, Any] = {
            "requested_backend": graph_config.extractor_backend,
            "applied_backend": graph_config.extractor_backend,
            "spacy_model": graph_config.spacy_model,
            "fallback_reason": None,
            "output_target": graph_config.output_target,
            "retrieval_strength": graph_config.retrieval_strength,
            "entity_types": list(graph_config.entity_types),
            "minimum_entity_mentions": graph_config.minimum_entity_mentions,
            "minimum_relationship_weight": graph_config.minimum_relationship_weight,
            "age_seed_mode": graph_config.age_seed_mode,
            "max_entities_per_chunk": graph_config.max_entities_per_chunk,
            "age_traversal_hops": graph_config.age_traversal_hops,
            "age_candidate_pool_size": graph_config.age_candidate_pool_size,
            "age_dense_rerank_weight": graph_config.age_dense_rerank_weight,
        }
        age_sync_summary: dict[str, Any] = {
            "status": "skipped" if graph_config.output_target == "object_store" else "pending",
            "graph_name": self._age_store.graph_name
            if graph_config.output_target == "object_store_and_age"
            else None,
            "node_count": 0,
            "edge_count": 0,
            "error": None,
        }
        processed_sources = 0
        skipped_sources = 0
        failed_sources = 0
        total_chars = 0
        total_tokens = 0
        embedding_dimension: int | None = None
        started_perf = perf_counter()
        output_prefix = self._output_prefix(knowledge_base_id, 0, version_id)

        def touch_heartbeat() -> None:
            if heartbeat is not None:
                heartbeat()

        def log_event(event_type: str, payload: dict[str, Any]) -> None:
            events.append((event_type, {**payload, "at": _utcnow().isoformat()}))

        def build_summary(*, duration_seconds: float | None = None) -> dict[str, Any]:
            graph_profile = self._graph_build_preset_match(
                graph_config,
                age_available=self._age_available(),
            )
            avg_chunk_chars = (
                round(sum(chunk.char_count for chunk in chunk_rows) / len(chunk_rows), 2)
                if chunk_rows
                else 0.0
            )
            avg_chunk_tokens = (
                round(sum(chunk.token_count for chunk in chunk_rows) / len(chunk_rows), 2)
                if chunk_rows
                else 0.0
            )
            summary: dict[str, Any] = {
                "processed_sources": processed_sources,
                "skipped_sources": skipped_sources,
                "failed_sources": failed_sources,
                "chunk_count": len(chunk_rows),
                "entity_count": len(entity_rows),
                "relationship_count": len(relationship_rows),
                "graph_node_count": len(entity_rows),
                "graph_edge_count": len(relationship_rows),
                "total_chars": total_chars,
                "total_tokens": total_tokens,
                "avg_chunk_chars": avg_chunk_chars,
                "avg_chunk_tokens": avg_chunk_tokens,
                "embedding_dimension": embedding_dimension,
                "graph_backend": graph_metadata.get("applied_backend"),
                "graph_backend_requested": graph_metadata.get("requested_backend"),
                "graph_output_target": graph_config.output_target,
                "graph_default_retrieval_mode": graph_config.default_retrieval_mode,
                "graph_retrieval_strength": graph_config.retrieval_strength,
                "graph_age_seed_mode": graph_config.age_seed_mode,
                "graph_entity_types": list(graph_config.entity_types),
                "graph_max_entities_per_chunk": graph_config.max_entities_per_chunk,
                "graph_minimum_entity_mentions": graph_config.minimum_entity_mentions,
                "graph_minimum_relationship_weight": graph_config.minimum_relationship_weight,
                "graph_age_traversal_hops": graph_config.age_traversal_hops,
                "graph_age_candidate_pool_size": graph_config.age_candidate_pool_size,
                "graph_age_dense_rerank_weight": graph_config.age_dense_rerank_weight,
                "graph_strict_age_retrieval_default": graph_config.strict_age_retrieval_default,
                "graph_profile_id": graph_profile.id if graph_profile is not None else "custom",
                "graph_profile_label": (
                    graph_profile.label if graph_profile is not None else "Custom graph profile"
                ),
                "age_sync_status": age_sync_summary["status"],
                "age_graph_name": age_sync_summary["graph_name"],
                "age_synced_nodes": age_sync_summary["node_count"],
                "age_synced_edges": age_sync_summary["edge_count"],
                "source_fingerprint": _fingerprint_sources(source_bucket, source_manifest),
            }
            if graph_metadata.get("fallback_reason"):
                summary["graph_backend_fallback_reason"] = graph_metadata["fallback_reason"]
            if age_sync_summary.get("error"):
                summary["age_sync_error"] = age_sync_summary["error"]
            if duration_seconds is not None:
                summary["duration_seconds"] = round(duration_seconds, 3)
            return summary

        def persist_failure(message: str) -> None:
            log_event("build_failed", {"message": message})
            logger.warning("knowledge-base build failed (version=%s): %s", version_id, message)
            summary = build_summary(duration_seconds=perf_counter() - started_perf)
            artifacts = _PersistedArtifacts(
                chunks_uri=_artifact_uri(source_bucket, output_prefix, "chunks.jsonl")
                if chunk_exports
                else None,
                entities_uri=_artifact_uri(source_bucket, output_prefix, "entities.jsonl")
                if entity_exports
                else None,
                relationships_uri=_artifact_uri(
                    source_bucket,
                    output_prefix,
                    "relationships.jsonl",
                )
                if relationship_exports
                else None,
                graph_uri=_artifact_uri(source_bucket, output_prefix, "graph.json")
                if graph_export is not None
                else None,
            )
            try:
                artifacts = self._persist_artifacts(
                    source_bucket=source_bucket,
                    output_prefix=output_prefix,
                    source_rows=source_rows,
                    chunk_exports=chunk_exports,
                    entity_exports=entity_exports,
                    relationship_exports=relationship_exports,
                    graph_export=graph_export,
                    events=events,
                    manifest={
                        "knowledge_base_id": knowledge_base_id,
                        "knowledge_base_version_id": version_id,
                        "status": "failed",
                        "error": message,
                        "source_bucket": source_bucket,
                        "source_manifest": source_manifest,
                        "chunking_strategy": chunking_strategy,
                        "embedding_model": embedding_model,
                        "chunking_config": chunking_config,
                        "graph_config": graph_config.model_dump(),
                        "graph": graph_metadata,
                        "summary": summary,
                    },
                    stats={"status": "failed", **summary},
                )
            except Exception:
                logger.warning(
                    "failed to persist knowledge-base failure artifacts (version=%s)",
                    version_id,
                    exc_info=True,
                )
            completed_at = _utcnow()
            with self._session_factory() as session:
                version = session.get(CaliberKnowledgeBaseVersion, version_id)
                run = session.get(CaliberKnowledgeBaseRun, run_id)
                knowledge_base = session.get(CaliberKnowledgeBase, knowledge_base_id)
                if version is None or run is None or knowledge_base is None:
                    return
                if source_rows:
                    session.add_all(source_rows)
                if chunk_rows:
                    session.add_all(chunk_rows)
                if entity_rows:
                    session.add_all(entity_rows)
                if relationship_rows:
                    session.add_all(relationship_rows)
                self._replace_run_events(session, run_id, events)
                version.status = "failed"
                version.output_prefix = output_prefix
                version.output_bucket = source_bucket
                version.logs_uri = artifacts.logs_uri
                version.manifest_uri = artifacts.manifest_uri
                version.stats_uri = artifacts.stats_uri
                version.chunks_uri = artifacts.chunks_uri
                version.entities_uri = artifacts.entities_uri
                version.relationships_uri = artifacts.relationships_uri
                version.graph_uri = artifacts.graph_uri
                version.summary = summary
                version.error_summary = message
                version.completed_at = completed_at
                run.status = "failed"
                run.error_summary = message
                run.metrics = dict(summary)
                run.log_line_count = len(events)
                run.completed_at = completed_at
                run.lease_expires_at = None
                run.last_heartbeat_at = completed_at
                knowledge_base.last_run_status = "failed"
                knowledge_base.last_run_completed_at = run.completed_at
                session.commit()

        with self._session_factory() as session:
            version = session.get(CaliberKnowledgeBaseVersion, version_id)
            if version is not None:
                output_prefix = version.output_prefix

        touch_heartbeat()
        log_event(
            "build_started",
            {
                "knowledge_base_id": knowledge_base_id,
                "version_id": version_id,
                "run_id": run_id,
                "source_bucket": source_bucket,
                "chunking_strategy": chunking_strategy,
                "embedding_model": embedding_model,
                "graph_config": graph_config.model_dump(),
                **({"worker_id": worker_id} if worker_id else {}),
            },
        )

        try:
            expanded_sources = self._expand_sources(source_bucket, source_manifest)
            if not expanded_sources:
                raise ValueError("No source objects were found in the selected files or folders.")
            log_event("sources_expanded", {"count": len(expanded_sources)})
            touch_heartbeat()
            embedder = self._embedding_backend(embedding_model)
            for source in expanded_sources:
                touch_heartbeat()
                log_event(
                    "source_started",
                    {"object_key": source.object_key, "document_id": source.document_id},
                )
                if not _looks_supported(source.object_key, source.content_type):
                    skipped_sources += 1
                    source_rows.append(
                        CaliberKnowledgeBaseSource(
                            knowledge_base_source_id=new_knowledge_base_source_id(),
                            knowledge_base_version_id=version_id,
                            document_id=source.document_id,
                            selection_kind=source.selection_kind,
                            bucket=source.bucket,
                            object_key=source.object_key,
                            object_name=source.object_name,
                            object_store_path=source.object_store_path,
                            content_type=source.content_type,
                            size_bytes=source.size_bytes,
                            etag=source.etag,
                            last_modified=source.last_modified,
                            status="skipped",
                            error_summary="unsupported or binary file type",
                        )
                    )
                    log_event(
                        "source_skipped",
                        {"object_key": source.object_key, "reason": "unsupported_file_type"},
                    )
                    continue
                try:
                    extracted = self._extract_source_text(source)
                except IngestionError as exc:
                    failed_sources += 1
                    source_rows.append(
                        CaliberKnowledgeBaseSource(
                            knowledge_base_source_id=new_knowledge_base_source_id(),
                            knowledge_base_version_id=version_id,
                            document_id=source.document_id,
                            selection_kind=source.selection_kind,
                            bucket=source.bucket,
                            object_key=source.object_key,
                            object_name=source.object_name,
                            object_store_path=source.object_store_path,
                            content_type=source.content_type,
                            size_bytes=source.size_bytes,
                            etag=source.etag,
                            last_modified=source.last_modified,
                            status="failed",
                            error_summary=str(exc),
                        )
                    )
                    log_event("source_failed", {"object_key": source.object_key, "error": str(exc)})
                    continue

                touch_heartbeat()
                extracted_text = str(extracted.get("text", "")).strip()
                if not extracted_text:
                    skipped_sources += 1
                    source_rows.append(
                        CaliberKnowledgeBaseSource(
                            knowledge_base_source_id=new_knowledge_base_source_id(),
                            knowledge_base_version_id=version_id,
                            document_id=source.document_id,
                            selection_kind=source.selection_kind,
                            bucket=source.bucket,
                            object_key=source.object_key,
                            object_name=source.object_name,
                            object_store_path=source.object_store_path,
                            content_type=source.content_type,
                            size_bytes=source.size_bytes,
                            etag=source.etag,
                            last_modified=source.last_modified,
                            extracted_chars=0,
                            extracted_format=str(extracted.get("format") or ""),
                            ocr_used=bool(extracted.get("ocr_used", False)),
                            status="skipped",
                            error_summary="document extracted to empty text",
                        )
                    )
                    log_event(
                        "source_skipped", {"object_key": source.object_key, "reason": "empty_text"}
                    )
                    continue

                fragments = chunk_text(
                    extracted_text,
                    chunking_strategy,
                    chunking_config,
                    base_metadata={
                        "source_bucket": source.bucket,
                        "source_key": source.object_key,
                        "document_id": source.document_id,
                        "object_store_path": source.object_store_path,
                        "format": extracted.get("format"),
                    },
                    embedder=embedder if chunking_strategy == "semantic" else None,
                )
                if not fragments:
                    skipped_sources += 1
                    source_rows.append(
                        CaliberKnowledgeBaseSource(
                            knowledge_base_source_id=new_knowledge_base_source_id(),
                            knowledge_base_version_id=version_id,
                            document_id=source.document_id,
                            selection_kind=source.selection_kind,
                            bucket=source.bucket,
                            object_key=source.object_key,
                            object_name=source.object_name,
                            object_store_path=source.object_store_path,
                            content_type=source.content_type,
                            size_bytes=source.size_bytes,
                            etag=source.etag,
                            last_modified=source.last_modified,
                            extracted_chars=int(extracted.get("chars") or 0),
                            extracted_format=str(extracted.get("format") or ""),
                            ocr_used=bool(extracted.get("ocr_used", False)),
                            status="skipped",
                            error_summary="chunker produced zero chunks",
                        )
                    )
                    log_event(
                        "source_skipped", {"object_key": source.object_key, "reason": "zero_chunks"}
                    )
                    continue

                embeddings = embedder.embed_texts([fragment.content for fragment in fragments])
                touch_heartbeat()
                if embeddings:
                    embedding_dimension = len(embeddings[0])
                processed_sources += 1
                total_chars += len(extracted_text)
                source_rows.append(
                    CaliberKnowledgeBaseSource(
                        knowledge_base_source_id=new_knowledge_base_source_id(),
                        knowledge_base_version_id=version_id,
                        document_id=source.document_id,
                        selection_kind=source.selection_kind,
                        bucket=source.bucket,
                        object_key=source.object_key,
                        object_name=source.object_name,
                        object_store_path=source.object_store_path,
                        content_type=source.content_type,
                        size_bytes=source.size_bytes,
                        etag=source.etag,
                        last_modified=source.last_modified,
                        extracted_chars=int(extracted.get("chars") or 0),
                        extracted_format=str(extracted.get("format") or ""),
                        ocr_used=bool(extracted.get("ocr_used", False)),
                        status="processed",
                        source_metadata={
                            "parser_format": extracted.get("format"),
                            "truncated": bool(extracted.get("truncated", False)),
                            "chars": int(extracted.get("chars") or 0),
                        },
                    )
                )
                for index, (fragment, vector) in enumerate(
                    zip(fragments, embeddings, strict=False)
                ):
                    token_count = _estimate_token_count(fragment.content)
                    total_tokens += token_count
                    ordinal = len(chunk_rows) + 1
                    chunk_id = new_knowledge_base_chunk_id()
                    content_hash = hashlib.sha256(fragment.content.encode("utf-8")).hexdigest()
                    chunk_rows.append(
                        CaliberKnowledgeBaseChunk(
                            knowledge_base_chunk_id=chunk_id,
                            knowledge_base_version_id=version_id,
                            document_id=source.document_id,
                            source_bucket=source.bucket,
                            source_key=source.object_key,
                            source_name=source.object_name,
                            chunk_index=index,
                            ordinal=ordinal,
                            content=fragment.content,
                            content_hash=content_hash,
                            token_count=token_count,
                            char_count=len(fragment.content),
                            start_index=fragment.start_index,
                            end_index=fragment.end_index,
                            embedding=[float(value) for value in vector],
                            chunk_metadata=dict(fragment.metadata),
                        )
                    )
                    chunk_exports.append(
                        {
                            "chunk_id": chunk_id,
                            "document_id": source.document_id,
                            "source_bucket": source.bucket,
                            "source_key": source.object_key,
                            "source_name": source.object_name,
                            "object_store_path": source.object_store_path,
                            "chunk_index": index,
                            "ordinal": ordinal,
                            "content": fragment.content,
                            "metadata": dict(fragment.metadata),
                            "start_index": fragment.start_index,
                            "end_index": fragment.end_index,
                            "token_count": token_count,
                            "char_count": len(fragment.content),
                            "embedding": [float(value) for value in vector],
                        }
                    )
                log_event(
                    "source_completed",
                    {
                        "object_key": source.object_key,
                        "chunks": len(fragments),
                        "chars": int(extracted.get("chars") or 0),
                    },
                )

            if not chunk_rows:
                raise ValueError(
                    "The selected files did not produce any chunks. Check the source types or extraction settings."
                )

            try:
                graph_bundle = build_graph_bundle(
                    chunk_exports,
                    backend=graph_config.extractor_backend,
                    spacy_model=graph_config.spacy_model
                    or self._config.knowledge_graph_spacy_model,
                    max_entities_per_chunk=graph_config.max_entities_per_chunk,
                    entity_types=set(graph_config.entity_types),
                    minimum_entity_mentions=graph_config.minimum_entity_mentions,
                    minimum_relationship_weight=graph_config.minimum_relationship_weight,
                )
                graph_metadata = dict(graph_bundle.metadata)
                entity_id_by_key: dict[str, str] = {}
                for entity in graph_bundle.entities:
                    entity_id = new_knowledge_base_entity_id()
                    entity_id_by_key[entity.entity_key] = entity_id
                    entity_rows.append(
                        CaliberKnowledgeBaseEntity(
                            knowledge_base_entity_id=entity_id,
                            knowledge_base_version_id=version_id,
                            entity_key=entity.entity_key,
                            label=entity.label,
                            entity_type=entity.entity_type,
                            aliases=list(entity.aliases),
                            mention_count=entity.mention_count,
                            source_documents=list(entity.source_documents),
                            source_keys=list(entity.source_keys),
                            source_chunks=list(entity.source_chunks),
                            entity_metadata=dict(entity.metadata),
                        )
                    )
                    entity_exports.append(
                        {
                            "entity_id": entity_id,
                            "entity_key": entity.entity_key,
                            "label": entity.label,
                            "entity_type": entity.entity_type,
                            "aliases": list(entity.aliases),
                            "mention_count": entity.mention_count,
                            "source_documents": list(entity.source_documents),
                            "source_keys": list(entity.source_keys),
                            "source_chunks": list(entity.source_chunks),
                            "metadata": dict(entity.metadata),
                        }
                    )
                for relationship in graph_bundle.relationships:
                    source_entity_id = entity_id_by_key.get(relationship.source_entity_key)
                    target_entity_id = entity_id_by_key.get(relationship.target_entity_key)
                    if not source_entity_id or not target_entity_id:
                        continue
                    relationship_id = new_knowledge_base_relationship_id()
                    relationship_rows.append(
                        CaliberKnowledgeBaseRelationship(
                            knowledge_base_relationship_id=relationship_id,
                            knowledge_base_version_id=version_id,
                            source_entity_id=source_entity_id,
                            target_entity_id=target_entity_id,
                            relationship_type=relationship.relationship_type,
                            weight=float(relationship.weight),
                            evidence_chunk_ids=list(relationship.evidence_chunk_ids),
                            source_documents=list(relationship.source_documents),
                            relationship_metadata=dict(relationship.metadata),
                        )
                    )
                    relationship_exports.append(
                        {
                            "relationship_id": relationship_id,
                            "source_entity_id": source_entity_id,
                            "source_entity_key": relationship.source_entity_key,
                            "target_entity_id": target_entity_id,
                            "target_entity_key": relationship.target_entity_key,
                            "relationship_type": relationship.relationship_type,
                            "weight": float(relationship.weight),
                            "evidence_chunk_ids": list(relationship.evidence_chunk_ids),
                            "source_documents": list(relationship.source_documents),
                            "metadata": dict(relationship.metadata),
                        }
                    )
                graph_export = graph_bundle.graph
                log_event(
                    "graph_built",
                    {
                        "entity_count": len(entity_rows),
                        "relationship_count": len(relationship_rows),
                        "backend": graph_metadata.get("applied_backend"),
                        "fallback_reason": graph_metadata.get("fallback_reason"),
                    },
                )
            except Exception as exc:  # pragma: no cover - best-effort enrichment
                logger.warning(
                    "knowledge graph extraction failed (version=%s)",
                    version_id,
                    exc_info=True,
                )
                log_event("graph_skipped", {"error": str(exc)})
                graph_metadata = {
                    **graph_metadata,
                    "status": "skipped",
                    "error": str(exc),
                }
                graph_export = {
                    "format": "caliber-knowledge-graph/v1",
                    "entity_count": 0,
                    "relationship_count": 0,
                    "metadata": graph_metadata,
                    "nodes": [],
                    "edges": [],
                }

            if graph_config.output_target == "object_store_and_age":
                log_event(
                    "age_sync_started",
                    {
                        "graph_name": self._age_store.graph_name,
                    },
                )
                document_ids_with_chunks = {chunk.document_id for chunk in chunk_rows}
                age_documents = [
                    AgeDocumentNode(
                        document_id=row.document_id,
                        object_key=row.object_key,
                        object_name=row.object_name,
                        object_store_path=row.object_store_path,
                    )
                    for row in source_rows
                    if row.document_id in document_ids_with_chunks and row.status == "processed"
                ]
                age_sync = self._age_store.sync_version(
                    knowledge_base_id=knowledge_base_id,
                    knowledge_base_name=knowledge_base_name,
                    version_id=version_id,
                    version_number=version_number,
                    source_bucket=source_bucket,
                    documents=age_documents,
                    chunks=chunk_exports,
                    entities=entity_exports,
                    relationships=relationship_exports,
                )
                age_sync_summary = {
                    "status": age_sync.status,
                    "graph_name": age_sync.graph_name,
                    "node_count": age_sync.node_count,
                    "edge_count": age_sync.edge_count,
                    "error": age_sync.error,
                }
                graph_metadata["age"] = dict(age_sync_summary)
                if graph_export is not None:
                    graph_export["metadata"] = {
                        **dict(graph_export.get("metadata") or {}),
                        "age": dict(age_sync_summary),
                    }
                if age_sync.status == "synced":
                    log_event(
                        "age_sync_completed",
                        {
                            "graph_name": age_sync.graph_name,
                            "node_count": age_sync.node_count,
                            "edge_count": age_sync.edge_count,
                        },
                    )
                else:
                    log_event(
                        "age_sync_failed",
                        {
                            "graph_name": age_sync.graph_name,
                            "error": age_sync.error or "unknown_error",
                        },
                    )
            else:
                log_event(
                    "age_sync_skipped",
                    {
                        "reason": "output_target_object_store",
                    },
                )

            duration_seconds = perf_counter() - started_perf
            summary = build_summary(duration_seconds=duration_seconds)
            log_event(
                "chunks_persisted",
                {"chunk_count": len(chunk_rows), "embedding_dimension": embedding_dimension},
            )
            log_event(
                "build_completed",
                {
                    "chunk_count": len(chunk_rows),
                    "entity_count": len(entity_rows),
                    "relationship_count": len(relationship_rows),
                },
            )
            touch_heartbeat()
            artifacts = self._persist_artifacts(
                source_bucket=source_bucket,
                output_prefix=output_prefix,
                source_rows=source_rows,
                chunk_exports=chunk_exports,
                entity_exports=entity_exports,
                relationship_exports=relationship_exports,
                graph_export=graph_export,
                events=events,
                manifest={
                    "knowledge_base_id": knowledge_base_id,
                    "knowledge_base_version_id": version_id,
                    "status": "completed",
                    "source_bucket": source_bucket,
                    "source_manifest": source_manifest,
                    "chunking_strategy": chunking_strategy,
                    "embedding_model": embedding_model,
                    "chunking_config": chunking_config,
                    "graph_config": graph_config.model_dump(),
                    "graph": graph_metadata,
                    "summary": summary,
                },
                stats={"status": "completed", **summary},
            )
            with self._session_factory() as session:
                version = session.get(CaliberKnowledgeBaseVersion, version_id)
                run = session.get(CaliberKnowledgeBaseRun, run_id)
                knowledge_base = session.get(CaliberKnowledgeBase, knowledge_base_id)
                if version is None or run is None or knowledge_base is None:
                    raise RuntimeError("knowledge-base build rows disappeared before completion")
                session.add_all(source_rows)
                session.add_all(chunk_rows)
                if entity_rows:
                    session.add_all(entity_rows)
                if relationship_rows:
                    session.add_all(relationship_rows)
                # Populate the pgvector column for SQL-side ANN (Postgres-only).
                if self._config.knowledge_pgvector_enabled and pgvector_ann.is_postgres(session):
                    session.flush()
                    pgvector_ann.populate_chunk_vectors(
                        session,
                        [(c.knowledge_base_chunk_id, c.embedding) for c in chunk_rows],
                        expected_dim=self._config.knowledge_embedding_dimension,
                    )
                self._replace_run_events(session, run_id, events)
                completed_at = _utcnow()
                version.status = "completed"
                version.embedding_dimension = embedding_dimension
                version.output_prefix = output_prefix
                version.output_bucket = source_bucket
                version.chunks_uri = artifacts.chunks_uri
                version.entities_uri = artifacts.entities_uri
                version.relationships_uri = artifacts.relationships_uri
                version.graph_uri = artifacts.graph_uri
                version.logs_uri = artifacts.logs_uri
                version.manifest_uri = artifacts.manifest_uri
                version.stats_uri = artifacts.stats_uri
                version.summary = summary
                version.error_summary = None
                version.completed_at = completed_at
                run.status = "completed"
                run.metrics = summary
                run.error_summary = None
                run.log_line_count = len(events)
                run.completed_at = completed_at
                run.lease_expires_at = None
                run.last_heartbeat_at = completed_at
                knowledge_base.active_version_id = version_id
                knowledge_base.source_manifest = list(source_manifest)
                knowledge_base.source_fingerprint = _fingerprint_sources(
                    source_bucket, source_manifest
                )
                knowledge_base.last_run_status = "completed"
                knowledge_base.last_run_completed_at = run.completed_at
                session.commit()
        except (KnowledgeDependencyError, ValueError, IngestionError, RuntimeError) as exc:
            persist_failure(str(exc))
        except Exception as exc:  # pragma: no cover - defensive unexpected failure path
            persist_failure(f"Unexpected build failure: {type(exc).__name__}: {exc}")

    # ------------------------------------------------------------------
    # Support helpers
    # ------------------------------------------------------------------
    def _expand_sources(
        self,
        source_bucket: str,
        source_manifest: list[dict[str, str]],
    ) -> list[_ExpandedSource]:
        client = self._object_store()
        expanded: list[_ExpandedSource] = []
        seen: set[str] = set()
        for selection in source_manifest:
            kind = str(selection.get("kind") or "file")
            path = str(selection.get("path") or "").strip()
            if not path:
                continue
            if kind == "file":
                head = client.head_object(Bucket=source_bucket, Key=path)
                if path.startswith(f"{_RESERVED_OUTPUT_PREFIX}/"):
                    continue
                seen.add(path)
                expanded.append(
                    _ExpandedSource(
                        document_id=_document_id(source_bucket, path),
                        selection_kind="file",
                        bucket=source_bucket,
                        object_key=path,
                        object_name=_basename(path),
                        object_store_path=_object_store_path(source_bucket, path),
                        size_bytes=int(head.get("ContentLength") or 0),
                        etag=_strip_etag(head.get("ETag")),
                        last_modified=head.get("LastModified"),
                        content_type=str(head.get("ContentType") or "application/octet-stream"),
                    )
                )
                continue
            prefix = path if path.endswith("/") else f"{path}/"
            token: str | None = None
            while True:
                kwargs: dict[str, Any] = {"Bucket": source_bucket, "Prefix": prefix, "MaxKeys": 500}
                if token:
                    kwargs["ContinuationToken"] = token
                response = client.list_objects_v2(**kwargs)
                for item in response.get("Contents", []) or []:
                    key = str(item.get("Key") or "")
                    if not key or key.endswith("/"):
                        continue
                    if key.startswith(f"{_RESERVED_OUTPUT_PREFIX}/") or key in seen:
                        continue
                    seen.add(key)
                    head = client.head_object(Bucket=source_bucket, Key=key)
                    expanded.append(
                        _ExpandedSource(
                            document_id=_document_id(source_bucket, key),
                            selection_kind="folder",
                            bucket=source_bucket,
                            object_key=key,
                            object_name=_basename(key),
                            object_store_path=_object_store_path(source_bucket, key),
                            size_bytes=int(item.get("Size") or 0),
                            etag=_strip_etag(item.get("ETag")),
                            last_modified=item.get("LastModified"),
                            content_type=str(head.get("ContentType") or "application/octet-stream"),
                        )
                    )
                token = response.get("NextContinuationToken")
                if not response.get("IsTruncated"):
                    break
        return sorted(expanded, key=lambda item: item.object_key)

    def _extract_source_text(self, source: _ExpandedSource) -> dict[str, Any]:
        client = self._object_store()
        response = client.get_object(Bucket=source.bucket, Key=source.object_key)
        payload = response["Body"].read()
        suffix = Path(source.object_key).suffix
        temp_path = Path(tempfile.mkstemp(prefix="caliber-kb-", suffix=suffix)[1])
        try:
            temp_path.write_bytes(payload)
            return extract_document(str(temp_path), max_chars=_MAX_EXTRACT_CHARS)
        finally:
            temp_path.unlink(missing_ok=True)

    def _persist_artifacts(
        self,
        *,
        source_bucket: str,
        output_prefix: str,
        source_rows: list[CaliberKnowledgeBaseSource],
        chunk_exports: list[dict[str, Any]],
        entity_exports: list[dict[str, Any]],
        relationship_exports: list[dict[str, Any]],
        graph_export: dict[str, Any] | None,
        events: list[tuple[str, dict[str, Any]]],
        manifest: dict[str, Any],
        stats: dict[str, Any],
    ) -> _PersistedArtifacts:
        client = self._object_store()
        if client is None:
            return _PersistedArtifacts()
        logs_uri = self._put_jsonl(
            source_bucket,
            f"{output_prefix}/logs.jsonl",
            [
                {"sequence": index + 1, "event_type": event_type, "payload": payload}
                for index, (event_type, payload) in enumerate(events)
            ],
        )
        self._put_jsonl(
            source_bucket,
            f"{output_prefix}/sources.jsonl",
            [
                {
                    "document_id": row.document_id,
                    "selection_kind": row.selection_kind,
                    "bucket": row.bucket,
                    "object_key": row.object_key,
                    "object_name": row.object_name,
                    "object_store_path": row.object_store_path,
                    "content_type": row.content_type,
                    "size_bytes": row.size_bytes,
                    "etag": row.etag,
                    "status": row.status,
                    "error_summary": row.error_summary,
                    "extracted_chars": row.extracted_chars,
                    "extracted_format": row.extracted_format,
                    "ocr_used": row.ocr_used,
                    "metadata": row.source_metadata or {},
                }
                for row in source_rows
            ],
        )
        chunks_uri = self._put_jsonl(source_bucket, f"{output_prefix}/chunks.jsonl", chunk_exports)
        entities_uri = (
            self._put_jsonl(source_bucket, f"{output_prefix}/entities.jsonl", entity_exports)
            if entity_exports or graph_export is not None
            else None
        )
        relationships_uri = (
            self._put_jsonl(
                source_bucket,
                f"{output_prefix}/relationships.jsonl",
                relationship_exports,
            )
            if relationship_exports or graph_export is not None
            else None
        )
        graph_uri = (
            self._put_json(source_bucket, f"{output_prefix}/graph.json", graph_export)
            if graph_export is not None
            else None
        )
        manifest_uri = self._put_json(source_bucket, f"{output_prefix}/manifest.json", manifest)
        stats_uri = self._put_json(source_bucket, f"{output_prefix}/stats.json", stats)
        return _PersistedArtifacts(
            logs_uri=logs_uri,
            manifest_uri=manifest_uri,
            stats_uri=stats_uri,
            chunks_uri=chunks_uri,
            entities_uri=entities_uri,
            relationships_uri=relationships_uri,
            graph_uri=graph_uri,
        )

    def _put_json(self, bucket: str, key: str, payload: dict[str, Any]) -> str | None:
        client = self._object_store()
        if client is None:
            return None
        client.put_object(
            Bucket=bucket,
            Key=key,
            Body=json.dumps(payload, ensure_ascii=True, indent=2).encode("utf-8"),
            ContentType="application/json",
        )
        return _artifact_uri(bucket, key.rsplit("/", 1)[0], key.rsplit("/", 1)[-1])

    def _put_jsonl(self, bucket: str, key: str, rows: list[dict[str, Any]]) -> str | None:
        client = self._object_store()
        if client is None:
            return None
        body = "\n".join(json.dumps(row, ensure_ascii=True) for row in rows).encode("utf-8")
        client.put_object(Bucket=bucket, Key=key, Body=body, ContentType="application/x-ndjson")
        return _artifact_uri(bucket, key.rsplit("/", 1)[0], key.rsplit("/", 1)[-1])

    def _refresh_version_age_sync_artifacts(
        self,
        *,
        knowledge_base_id: str,
        version_id: str,
        version_status: str,
        version_error_summary: str | None,
        source_bucket: str,
        source_manifest: list[dict[str, Any]],
        chunking_strategy: str,
        embedding_model: str,
        chunking_config: dict[str, Any],
        output_bucket: str,
        output_prefix: str,
        graph_config: KnowledgeGraphConfigSchema,
        summary: dict[str, Any],
        age_sync_summary: dict[str, Any],
    ) -> None:
        client = self._object_store()
        if client is None:
            return

        graph_key = f"{output_prefix}/graph.json"
        graph_payload: dict[str, Any] | None = None
        try:
            response = client.get_object(Bucket=output_bucket, Key=graph_key)
            loaded = json.loads(response["Body"].read())
            if isinstance(loaded, dict):
                graph_payload = loaded
        except Exception:
            graph_payload = None

        graph_metadata = (
            dict(graph_payload.get("metadata") or {}) if isinstance(graph_payload, dict) else {}
        )
        graph_metadata.update(
            {
                "output_target": graph_config.output_target,
                "retrieval_strength": graph_config.retrieval_strength,
                "entity_types": list(graph_config.entity_types),
                "minimum_entity_mentions": graph_config.minimum_entity_mentions,
                "minimum_relationship_weight": graph_config.minimum_relationship_weight,
                "max_entities_per_chunk": graph_config.max_entities_per_chunk,
                "age_traversal_hops": graph_config.age_traversal_hops,
                "age_candidate_pool_size": graph_config.age_candidate_pool_size,
                "age_dense_rerank_weight": graph_config.age_dense_rerank_weight,
                "age": dict(age_sync_summary),
            }
        )
        if graph_payload is None:
            graph_payload = {
                "format": "caliber-knowledge-graph/v1",
                "entity_count": int(summary.get("entity_count") or 0),
                "relationship_count": int(summary.get("relationship_count") or 0),
                "metadata": graph_metadata,
                "nodes": [],
                "edges": [],
            }
        else:
            graph_payload["metadata"] = graph_metadata

        self._put_json(output_bucket, graph_key, graph_payload)
        self._put_json(
            output_bucket,
            f"{output_prefix}/manifest.json",
            {
                "knowledge_base_id": knowledge_base_id,
                "knowledge_base_version_id": version_id,
                "status": version_status,
                "error": version_error_summary,
                "source_bucket": source_bucket,
                "source_manifest": source_manifest,
                "chunking_strategy": chunking_strategy,
                "embedding_model": embedding_model,
                "chunking_config": chunking_config,
                "graph_config": graph_config.model_dump(),
                "graph": graph_metadata,
                "summary": summary,
            },
        )
        self._put_json(
            output_bucket,
            f"{output_prefix}/stats.json",
            {"status": version_status, **summary},
        )

    def _replace_run_events(
        self, session: Session, run_id: str, events: list[tuple[str, dict[str, Any]]]
    ) -> None:
        session.query(CaliberKnowledgeBaseRunEvent).filter(
            CaliberKnowledgeBaseRunEvent.knowledge_base_run_id == run_id
        ).delete(synchronize_session=False)
        session.add_all(
            [
                CaliberKnowledgeBaseRunEvent(
                    knowledge_base_run_id=run_id,
                    sequence=index + 1,
                    event_type=event_type,
                    payload=payload,
                    created_at=_event_created_at(payload),
                )
                for index, (event_type, payload) in enumerate(events)
            ]
        )

    def _list_entities_for_version(
        self,
        session: Session,
        *,
        version_id: str,
    ) -> list[KnowledgeBaseEntitySchema]:
        stmt = (
            select(CaliberKnowledgeBaseEntity)
            .where(CaliberKnowledgeBaseEntity.knowledge_base_version_id == version_id)
            .order_by(
                CaliberKnowledgeBaseEntity.mention_count.desc(),
                CaliberKnowledgeBaseEntity.label.asc(),
            )
        )
        rows = session.execute(stmt).scalars().all()
        return [KnowledgeBaseEntitySchema.model_validate(row) for row in rows]

    def _list_relationships_for_version(
        self,
        session: Session,
        *,
        version_id: str,
    ) -> list[KnowledgeBaseRelationshipSchema]:
        relationships = (
            session.execute(
                select(CaliberKnowledgeBaseRelationship)
                .where(CaliberKnowledgeBaseRelationship.knowledge_base_version_id == version_id)
                .order_by(
                    CaliberKnowledgeBaseRelationship.weight.desc(),
                    CaliberKnowledgeBaseRelationship.relationship_type.asc(),
                )
            )
            .scalars()
            .all()
        )
        if not relationships:
            return []
        entity_ids = {rel.source_entity_id for rel in relationships} | {
            rel.target_entity_id for rel in relationships
        }
        entities = {
            row.knowledge_base_entity_id: row
            for row in session.execute(
                select(CaliberKnowledgeBaseEntity).where(
                    CaliberKnowledgeBaseEntity.knowledge_base_entity_id.in_(entity_ids)
                )
            )
            .scalars()
            .all()
        }
        return [
            KnowledgeBaseRelationshipSchema(
                knowledge_base_relationship_id=row.knowledge_base_relationship_id,
                knowledge_base_version_id=row.knowledge_base_version_id,
                source_entity_id=row.source_entity_id,
                source_entity_key=entities[row.source_entity_id].entity_key,
                source_entity_label=entities[row.source_entity_id].label,
                target_entity_id=row.target_entity_id,
                target_entity_key=entities[row.target_entity_id].entity_key,
                target_entity_label=entities[row.target_entity_id].label,
                relationship_type=row.relationship_type,
                weight=float(row.weight),
                evidence_chunk_ids=list(row.evidence_chunk_ids or []),
                source_documents=list(row.source_documents or []),
                relationship_metadata=dict(row.relationship_metadata or {}),
                created_at=row.created_at,
            )
            for row in relationships
            if row.source_entity_id in entities and row.target_entity_id in entities
        ]

    def _explore_local_graph(
        self,
        *,
        entities: list[KnowledgeBaseEntitySchema],
        relationships: list[KnowledgeBaseRelationshipSchema],
        query: str,
        entity_type: str | None,
        minimum_relationship_weight: float,
        traversal_hops: int,
        node_limit: int,
    ) -> _GraphExplorerResult:
        normalized_query = query.strip().lower()
        normalized_type = (entity_type or "").strip().lower() or None
        context = self._local_graph_context(
            entities=entities,
            relationships=relationships,
            normalized_type=normalized_type,
            minimum_relationship_weight=minimum_relationship_weight,
        )
        if not context.eligible_entities:
            return _GraphExplorerResult(entities=[], relationships=[])
        seed_entities = self._resolve_local_graph_seed_entities(
            context=context,
            normalized_query=normalized_query,
        )
        if not seed_entities:
            return _GraphExplorerResult(entities=[], relationships=[])
        visible_entities, distances = self._expand_local_graph_visibility(
            context=context,
            seed_entities=seed_entities,
            normalized_query=normalized_query,
            traversal_hops=traversal_hops,
            node_limit=node_limit,
        )
        return self._build_local_graph_result(
            context=context,
            visible_entities=visible_entities,
            distances=distances,
            node_limit=node_limit,
        )

    def _local_graph_context(
        self,
        *,
        entities: list[KnowledgeBaseEntitySchema],
        relationships: list[KnowledgeBaseRelationshipSchema],
        normalized_type: str | None,
        minimum_relationship_weight: float,
    ) -> _LocalGraphContext:
        eligible_entities = [
            entity
            for entity in entities
            if not normalized_type or entity.entity_type.lower() == normalized_type
        ]
        entity_lookup = {entity.knowledge_base_entity_id: entity for entity in eligible_entities}
        eligible_relationships = [
            relationship
            for relationship in relationships
            if (
                relationship.weight >= minimum_relationship_weight
                and relationship.source_entity_id in entity_lookup
                and relationship.target_entity_id in entity_lookup
            )
        ]
        adjacency: dict[str, list[KnowledgeBaseRelationshipSchema]] = defaultdict(list)
        for relationship in eligible_relationships:
            adjacency[relationship.source_entity_id].append(relationship)
            adjacency[relationship.target_entity_id].append(relationship)
        return _LocalGraphContext(
            eligible_entities=eligible_entities,
            entity_lookup=entity_lookup,
            eligible_relationships=eligible_relationships,
            adjacency=adjacency,
        )

    def _resolve_local_graph_seed_entities(
        self,
        *,
        context: _LocalGraphContext,
        normalized_query: str,
    ) -> list[KnowledgeBaseEntitySchema]:
        seed_entities = [
            entity
            for entity in context.eligible_entities
            if self._local_graph_entity_matches_query(
                entity=entity, normalized_query=normalized_query
            )
        ]
        if normalized_query and not seed_entities:
            related_seed_ids: set[str] = set()
            for relationship in context.eligible_relationships:
                if any(
                    normalized_query in field.lower()
                    for field in (
                        relationship.source_entity_label,
                        relationship.target_entity_label,
                        relationship.relationship_type,
                    )
                ):
                    related_seed_ids.add(relationship.source_entity_id)
                    related_seed_ids.add(relationship.target_entity_id)
            seed_entities = [
                context.entity_lookup[entity_id]
                for entity_id in related_seed_ids
                if entity_id in context.entity_lookup
            ]
        if not seed_entities and normalized_query:
            return []
        if not seed_entities:
            seed_entities = list(context.eligible_entities)
        seed_entities.sort(
            key=lambda entity: (-entity.mention_count, entity.label.lower(), entity.entity_key)
        )
        return seed_entities

    def _local_graph_entity_matches_query(
        self,
        *,
        entity: KnowledgeBaseEntitySchema,
        normalized_query: str,
    ) -> bool:
        if not normalized_query:
            return True
        haystacks = [
            entity.label,
            entity.entity_type,
            *entity.aliases,
            *entity.source_keys,
        ]
        return any(normalized_query in field.lower() for field in haystacks)

    def _expand_local_graph_visibility(
        self,
        *,
        context: _LocalGraphContext,
        seed_entities: list[KnowledgeBaseEntitySchema],
        normalized_query: str,
        traversal_hops: int,
        node_limit: int,
    ) -> tuple[dict[str, KnowledgeGraphEntityViewSchema], dict[str, int]]:
        seed_limit = max(1, min(node_limit, 8 if normalized_query else node_limit))
        visible_entities: dict[str, KnowledgeGraphEntityViewSchema] = {}
        distances: dict[str, int] = {}
        frontier: list[str] = []

        for entity in seed_entities[:seed_limit]:
            visible_entities[entity.knowledge_base_entity_id] = (
                KnowledgeGraphEntityViewSchema.model_validate(
                    {
                        **entity.model_dump(),
                        "distance": 0,
                        "highlighted": True,
                        "graph_source": "local",
                    }
                )
            )
            distances[entity.knowledge_base_entity_id] = 0
            frontier.append(entity.knowledge_base_entity_id)

        max_hops = max(0, min(int(traversal_hops), 2))
        for hop in range(1, max_hops + 1):
            if not frontier or len(visible_entities) >= node_limit:
                break
            candidate_weights = self._local_graph_candidate_weights(
                context=context,
                frontier=frontier,
                visible_entities=visible_entities,
            )
            if not candidate_weights:
                frontier = []
                continue
            ordered_neighbor_ids = sorted(
                candidate_weights,
                key=lambda entity_id: (
                    -candidate_weights[entity_id],
                    -context.entity_lookup[entity_id].mention_count,
                    context.entity_lookup[entity_id].label.lower(),
                    context.entity_lookup[entity_id].entity_key,
                ),
            )
            next_frontier: list[str] = []
            for neighbor_id in ordered_neighbor_ids:
                if len(visible_entities) >= node_limit:
                    break
                neighbor = context.entity_lookup[neighbor_id]
                visible_entities[neighbor_id] = KnowledgeGraphEntityViewSchema.model_validate(
                    {
                        **neighbor.model_dump(),
                        "distance": hop,
                        "highlighted": False,
                        "graph_source": "local",
                    }
                )
                distances[neighbor_id] = hop
                next_frontier.append(neighbor_id)
            frontier = next_frontier
        return visible_entities, distances

    def _local_graph_candidate_weights(
        self,
        *,
        context: _LocalGraphContext,
        frontier: list[str],
        visible_entities: dict[str, KnowledgeGraphEntityViewSchema],
    ) -> dict[str, float]:
        candidate_weights: dict[str, float] = {}
        for entity_id in frontier:
            for relationship in context.adjacency.get(entity_id, []):
                neighbor_id = (
                    relationship.target_entity_id
                    if relationship.source_entity_id == entity_id
                    else relationship.source_entity_id
                )
                if neighbor_id in visible_entities or neighbor_id not in context.entity_lookup:
                    continue
                candidate_weights[neighbor_id] = max(
                    candidate_weights.get(neighbor_id, 0.0),
                    float(relationship.weight),
                )
        return candidate_weights

    def _build_local_graph_result(
        self,
        *,
        context: _LocalGraphContext,
        visible_entities: dict[str, KnowledgeGraphEntityViewSchema],
        distances: dict[str, int],
        node_limit: int,
    ) -> _GraphExplorerResult:
        ordered_entities = sorted(
            visible_entities.values(),
            key=lambda entity: (
                entity.distance if entity.distance is not None else 99,
                -entity.mention_count,
                entity.label.lower(),
                entity.entity_key,
            ),
        )
        visible_ids = set(visible_entities)
        visible_relationships = [
            KnowledgeGraphRelationshipViewSchema.model_validate(
                {
                    **relationship.model_dump(),
                    "hop_distance": max(
                        distances.get(relationship.source_entity_id, 0),
                        distances.get(relationship.target_entity_id, 0),
                    ),
                    "graph_source": "local",
                }
            )
            for relationship in context.eligible_relationships
            if (
                relationship.source_entity_id in visible_ids
                and relationship.target_entity_id in visible_ids
            )
        ]
        visible_relationships.sort(
            key=lambda relationship: (
                -relationship.weight,
                relationship.source_entity_label.lower(),
                relationship.target_entity_label.lower(),
                relationship.relationship_type.lower(),
            )
        )
        return _GraphExplorerResult(
            entities=ordered_entities,
            relationships=visible_relationships[: max(node_limit * 3, 24)],
            matched_entity_labels=[
                entity.label for entity in ordered_entities if entity.distance == 0
            ],
            expanded_entity_labels=[
                entity.label
                for entity in ordered_entities
                if entity.distance is not None and entity.distance > 0
            ],
        )

    def _require_visible_knowledge_base(
        self,
        session: Session,
        knowledge_base_id: str,
        identity: CaliberIdentity,
    ) -> CaliberKnowledgeBase:
        stmt = select(CaliberKnowledgeBase).where(
            CaliberKnowledgeBase.knowledge_base_id == knowledge_base_id
        )
        stmt = apply_visibility_filter(
            stmt,
            CaliberKnowledgeBase,
            identity,
            identity.active_project_id,
        )
        row = session.execute(stmt).scalars().first()
        if row is None:
            raise HTTPException(
                status_code=404, detail=f"knowledge base {knowledge_base_id!r} not found"
            )
        return row

    def _serialize_knowledge_base(
        self,
        knowledge_base: CaliberKnowledgeBase,
        *,
        version: CaliberKnowledgeBaseVersion | None = None,
    ) -> KnowledgeBaseSchema:
        payload = KnowledgeBaseSchema.model_validate(knowledge_base).model_dump()
        payload["active_version_summary"] = (
            self._serialize_knowledge_base_active_version(version).model_dump()
            if version is not None
            else None
        )
        return KnowledgeBaseSchema.model_validate(payload)

    def _serialize_version(
        self,
        version: CaliberKnowledgeBaseVersion,
    ) -> KnowledgeBaseVersionSchema:
        payload = KnowledgeBaseVersionSchema.model_validate(version).model_dump()
        graph_config = self._resolve_graph_config(version.graph_config)
        summary = version.summary if isinstance(version.summary, dict) else {}
        graph_profile = self._graph_build_preset_match(
            graph_config,
            age_available=self._age_available(),
        )
        payload["graph_profile_id"] = str(
            summary.get("graph_profile_id")
            or (graph_profile.id if graph_profile is not None else "custom")
        )
        payload["graph_profile_label"] = str(
            summary.get("graph_profile_label")
            or (graph_profile.label if graph_profile is not None else "Custom graph profile")
        )
        return KnowledgeBaseVersionSchema.model_validate(payload)

    def _serialize_knowledge_base_active_version(
        self,
        version: CaliberKnowledgeBaseVersion,
    ) -> KnowledgeBaseActiveVersionSummarySchema:
        graph_config = self._resolve_graph_config(version.graph_config)
        summary = version.summary if isinstance(version.summary, dict) else {}
        graph_profile = self._graph_build_preset_match(
            graph_config,
            age_available=self._age_available(),
        )
        return KnowledgeBaseActiveVersionSummarySchema(
            knowledge_base_version_id=version.knowledge_base_version_id,
            version_number=version.version_number,
            status=version.status,
            chunking_strategy=version.chunking_strategy,
            embedding_model=version.embedding_model,
            graph_extractor=graph_config.extractor_backend,
            graph_target=graph_config.output_target,
            default_retrieval_mode=graph_config.default_retrieval_mode,
            retrieval_strength=graph_config.retrieval_strength,
            graph_profile_id=str(
                summary.get("graph_profile_id")
                or (graph_profile.id if graph_profile is not None else "custom")
            ),
            graph_profile_label=str(
                summary.get("graph_profile_label")
                or (graph_profile.label if graph_profile is not None else "Custom graph profile")
            ),
            age_sync_status=self._version_age_sync_status(version),
            age_ready=self._version_age_ready(version, graph_config=graph_config),
            age_graph_name=(
                str(summary.get("age_graph_name"))
                if summary.get("age_graph_name") is not None
                else (
                    self._age_store.graph_name
                    if graph_config.output_target == "object_store_and_age"
                    else None
                )
            ),
            chunk_count=int(summary.get("chunk_count") or 0),
            entity_count=int(summary.get("entity_count") or 0),
            relationship_count=int(summary.get("relationship_count") or 0),
            created_at=version.created_at,
            completed_at=version.completed_at,
        )

    def _library_version_map(
        self,
        session: Session,
        knowledge_bases: Sequence[CaliberKnowledgeBase],
    ) -> dict[str, CaliberKnowledgeBaseVersion]:
        if not knowledge_bases:
            return {}
        knowledge_base_ids = [row.knowledge_base_id for row in knowledge_bases]
        rows = (
            session.execute(
                select(CaliberKnowledgeBaseVersion)
                .where(CaliberKnowledgeBaseVersion.knowledge_base_id.in_(knowledge_base_ids))
                .order_by(
                    CaliberKnowledgeBaseVersion.knowledge_base_id.asc(),
                    CaliberKnowledgeBaseVersion.version_number.desc(),
                )
            )
            .scalars()
            .all()
        )
        versions_by_base: dict[str, list[CaliberKnowledgeBaseVersion]] = defaultdict(list)
        for row in rows:
            versions_by_base[row.knowledge_base_id].append(row)

        selected: dict[str, CaliberKnowledgeBaseVersion] = {}
        for knowledge_base in knowledge_bases:
            candidates = versions_by_base.get(knowledge_base.knowledge_base_id, [])
            if not candidates:
                continue
            active = next(
                (
                    version
                    for version in candidates
                    if version.knowledge_base_version_id == knowledge_base.active_version_id
                ),
                None,
            )
            selected[knowledge_base.knowledge_base_id] = active or candidates[0]
        return selected

    def _require_visible_version(
        self,
        session: Session,
        version_id: str,
        identity: CaliberIdentity,
    ) -> tuple[CaliberKnowledgeBaseVersion, CaliberKnowledgeBase]:
        version = session.get(CaliberKnowledgeBaseVersion, version_id)
        if version is None:
            raise HTTPException(
                status_code=404, detail=f"knowledge-base version {version_id!r} not found"
            )
        knowledge_base = self._require_visible_knowledge_base(
            session, version.knowledge_base_id, identity
        )
        return version, knowledge_base

    def _assert_unique_name(
        self,
        session: Session,
        name: str,
        identity: CaliberIdentity,
        *,
        exclude_id: str | None,
    ) -> None:
        stmt = select(CaliberKnowledgeBase).where(CaliberKnowledgeBase.name == name)
        if identity.active_project_id:
            stmt = stmt.where(CaliberKnowledgeBase.project_id == identity.active_project_id)
        else:
            stmt = stmt.where(CaliberKnowledgeBase.project_id.is_(None))
        stmt = stmt.where(CaliberKnowledgeBase.owner == identity.user_id)
        existing = session.execute(stmt).scalars().first()
        if existing is not None and existing.knowledge_base_id != exclude_id:
            raise HTTPException(
                status_code=409,
                detail=f"knowledge-base name {name!r} is already in use by {existing.knowledge_base_id!r}",
            )

    def _should_enqueue_builds(self) -> bool:
        return bool(
            self._config.knowledge_build_queue_enabled and self._config.background_tasks_enabled
        )

    def _embedding_backend(self, model_id: str) -> Any:
        self._ensure_embedding_runtime_available()
        cached = self._embedder_cache.get(model_id)
        if cached is not None:
            return cached
        backend = build_embedding_backend(model_id)
        self._embedder_cache[model_id] = backend
        return backend

    def _ensure_embedding_runtime_available(self) -> None:
        ensure_embedding_backend_runtime_available(
            allow_flagged_local_embeddings=self._config.allow_flagged_local_embeddings
        )

    def _reranker_backend(self) -> Any:
        """The cached cross-encoder reranker, built lazily, or ``None`` if disabled."""
        if not self._config.knowledge_rerank_enabled:
            return None
        model_id = self._config.knowledge_rerank_model
        cached = self._reranker_cache.get(model_id)
        if cached is not None:
            return cached
        self._ensure_embedding_runtime_available()  # same torch/advisory guard
        backend = build_reranker_backend(model_id)
        self._reranker_cache[model_id] = backend
        return backend

    def _rerank(
        self, question: str, items: list[_RetrievedChunk], top_k: int
    ) -> list[_RetrievedChunk]:
        """Re-score a candidate pool with the cross-encoder and return the top_k.

        Second stage of two-stage retrieval: the first stage (ANN / cosine /
        hybrid) gathers a larger candidate pool, then this re-orders it by joint
        ``(query, passage)`` relevance. The raw cross-encoder logits are min-max
        normalized to ``[0, 1]`` for a stable ``score`` and recorded under
        ``score_breakdown['rerank']`` (the original retrieval score is preserved
        under ``['retrieval']``). A no-op (just a top_k slice) when reranking is
        disabled or the pool is trivial.
        """
        reranker = self._reranker_backend()
        if reranker is None or len(items) <= 1:
            return items[:top_k]
        scores = reranker.rerank_scores(question, [item.chunk.content for item in items])
        if len(scores) != len(items):
            return items[:top_k]
        lo, hi = min(scores), max(scores)
        span = hi - lo
        reranked: list[_RetrievedChunk] = []
        for item, raw in zip(items, scores, strict=True):
            normalized = (raw - lo) / span if span > 0 else 1.0
            breakdown = {**item.score_breakdown, "retrieval": item.score, "rerank": float(raw)}
            reranked.append(replace(item, score=normalized, score_breakdown=breakdown))
        reranked.sort(key=lambda item: item.score_breakdown["rerank"], reverse=True)
        return reranked[:top_k]

    def _default_graph_config(self) -> KnowledgeGraphConfigSchema:
        return KnowledgeGraphConfigSchema(
            extractor_backend=self._config.knowledge_graph_extractor_backend,
            spacy_model=(
                self._config.knowledge_graph_spacy_model
                if self._config.knowledge_graph_extractor_backend == "spacy"
                else None
            ),
            default_retrieval_mode="age_graph" if self._age_available() else "graph_hybrid",
            output_target="object_store_and_age" if self._age_available() else "object_store",
            strict_age_retrieval_default=False,
        )

    def _graph_build_presets(
        self,
        age_available: bool,
    ) -> list[KnowledgeGraphBuildPresetSchema]:
        presets = [
            KnowledgeGraphBuildPresetSchema(
                id="portable",
                label="Portable graph artifacts",
                eyebrow="Portable",
                description=(
                    "Keep entities and relationships in the object store only. Best when "
                    "you want easy export, no shared graph dependency, and local GraphRAG fallback."
                ),
                badges=["Object store", "Hybrid-ready", "No AGE sync"],
                patch={
                    "default_retrieval_mode": "graph_hybrid",
                    "output_target": "object_store",
                    "retrieval_strength": "balanced",
                    "age_seed_mode": "entity_then_text",
                    "age_traversal_hops": 0,
                    "age_candidate_pool_size": 16,
                    "age_dense_rerank_weight": 0.35,
                    "strict_age_retrieval_default": False,
                },
            ),
            KnowledgeGraphBuildPresetSchema(
                id="balanced",
                label="Balanced GraphRAG + AGE" if age_available else "Balanced GraphRAG",
                eyebrow="Recommended",
                description=(
                    "Use Apache AGE as the primary retrieval path with balanced traversal depth, "
                    "moderate dense reranking, and graph evidence that stays easy to inspect."
                    if age_available
                    else "Use the default graph settings for day-to-day retrieval: grounded hybrid "
                    "recall, moderate traversal, and dense reranking that stays easy to inspect."
                ),
                badges=(
                    ["AGE primary", "1-hop traversal", "Balanced rerank"]
                    if age_available
                    else ["Object store", "1-hop local graph", "Balanced rerank"]
                ),
                patch={
                    "default_retrieval_mode": "age_graph" if age_available else "graph_hybrid",
                    "output_target": "object_store_and_age" if age_available else "object_store",
                    "retrieval_strength": "balanced",
                    "age_seed_mode": "entity_then_text",
                    "age_traversal_hops": 1,
                    "age_candidate_pool_size": 24,
                    "age_dense_rerank_weight": 0.35,
                    "strict_age_retrieval_default": False,
                },
                recommended=True,
            ),
        ]
        if age_available:
            presets.append(
                KnowledgeGraphBuildPresetSchema(
                    id="age_native",
                    label="AGE-native retrieval",
                    eyebrow="Graph-first",
                    description=(
                        "Prioritize Apache AGE as the primary retrieval path, walk deeper relationship "
                        "trails, and use light dense reranking to keep the answer graph-native."
                    ),
                    badges=["AGE sync", "2-hop traversal", "Graph-first rerank"],
                    patch={
                        "default_retrieval_mode": "age_graph",
                        "output_target": "object_store_and_age",
                        "retrieval_strength": "aggressive",
                        "age_seed_mode": "query_entities_and_text",
                        "age_traversal_hops": 2,
                        "age_candidate_pool_size": 40,
                        "age_dense_rerank_weight": 0.2,
                        "strict_age_retrieval_default": False,
                    },
                    age_required=True,
                )
            )
            presets.append(
                KnowledgeGraphBuildPresetSchema(
                    id="age_strict",
                    label="Strict AGE default",
                    eyebrow="Locked",
                    description=(
                        "Sync into Apache AGE and save this version so graph-native retrieval stays on "
                        "the AGE path by default across Playground, workflows, and graph inspection."
                    ),
                    badges=["AGE primary", "Strict default", "No fallback"],
                    patch={
                        "default_retrieval_mode": "age_graph",
                        "output_target": "object_store_and_age",
                        "retrieval_strength": "aggressive",
                        "age_seed_mode": "query_entities_and_text",
                        "age_traversal_hops": 2,
                        "age_candidate_pool_size": 40,
                        "age_dense_rerank_weight": 0.2,
                        "strict_age_retrieval_default": True,
                    },
                    age_required=True,
                )
            )
        return presets

    def _graph_build_preset_match(
        self,
        graph_config: KnowledgeGraphConfigSchema,
        *,
        age_available: bool,
    ) -> KnowledgeGraphBuildPresetSchema | None:
        current = graph_config.model_dump()
        for preset in self._graph_build_presets(age_available):
            if all(current.get(key) == value for key, value in preset.patch.items()):
                return preset
        return None

    def _graph_query_presets(
        self,
        age_available: bool,
    ) -> list[KnowledgeGraphQueryPresetSchema]:
        presets = [
            KnowledgeGraphQueryPresetSchema(
                id="hybrid_precision",
                label="Precise GraphRAG",
                eyebrow="Precision",
                description=(
                    "Stay on the version-local graph, require stronger relationship evidence, "
                    "and keep expansion tight around the directly matched entities."
                ),
                badges=["Local graph", "0-hop", "Conservative"],
                retrieval_mode="graph_hybrid",
                patch={
                    "retrieval_strength": "conservative",
                    "minimum_relationship_weight": 2.0,
                    "age_traversal_hops": 0,
                },
            ),
            KnowledgeGraphQueryPresetSchema(
                id="hybrid_balanced",
                label="Balanced GraphRAG",
                eyebrow="Recommended" if not age_available else "Portable",
                description=(
                    "Blend dense recall with graph-aware evidence expansion without depending "
                    "on Apache AGE. This is the safest everyday fallback profile."
                ),
                badges=["Local graph", "1-hop", "Balanced"],
                retrieval_mode="graph_hybrid",
                patch={
                    "retrieval_strength": "balanced",
                    "minimum_relationship_weight": 1.0,
                    "age_traversal_hops": 1,
                },
                recommended=not age_available,
            ),
        ]
        if age_available:
            presets.extend(
                [
                    KnowledgeGraphQueryPresetSchema(
                        id="age_balanced",
                        label="Balanced AGE",
                        eyebrow="Recommended",
                        description=(
                            "Run Apache AGE as the primary retrieval path with one-hop traversal "
                            "and moderate dense reranking for grounded, inspectable answers."
                        ),
                        badges=["AGE primary", "1-hop", "Balanced rerank"],
                        retrieval_mode="age_graph",
                        patch={
                            "retrieval_strength": "balanced",
                            "minimum_relationship_weight": 1.0,
                            "age_seed_mode": "entity_then_text",
                            "age_traversal_hops": 1,
                            "age_candidate_pool_size": 24,
                            "age_dense_rerank_weight": 0.35,
                        },
                        recommended=True,
                        age_required=True,
                    ),
                    KnowledgeGraphQueryPresetSchema(
                        id="age_native",
                        label="AGE-native retrieval",
                        eyebrow="Graph-first",
                        description=(
                            "Keep retrieval graph-first with broader seeding, two-hop traversal, "
                            "and lighter dense reranking so the answer path stays AGE-backed."
                        ),
                        badges=["AGE primary", "2-hop", "Graph-first"],
                        retrieval_mode="age_graph",
                        patch={
                            "retrieval_strength": "aggressive",
                            "minimum_relationship_weight": 1.0,
                            "age_seed_mode": "query_entities_and_text",
                            "age_traversal_hops": 2,
                            "age_candidate_pool_size": 40,
                            "age_dense_rerank_weight": 0.2,
                        },
                        age_required=True,
                    ),
                    KnowledgeGraphQueryPresetSchema(
                        id="age_strict",
                        label="Strict AGE only",
                        eyebrow="Locked",
                        description=(
                            "Keep retrieval on Apache AGE only, preserve the graph-first path, "
                            "and refuse fallback to local GraphRAG or dense chunk search."
                        ),
                        badges=["AGE primary", "Strict", "No fallback"],
                        retrieval_mode="age_graph",
                        patch={
                            "retrieval_strength": "aggressive",
                            "minimum_relationship_weight": 1.0,
                            "age_seed_mode": "query_entities_and_text",
                            "age_traversal_hops": 2,
                            "age_candidate_pool_size": 40,
                            "age_dense_rerank_weight": 0.2,
                            "strict_age_retrieval": True,
                        },
                        age_required=True,
                    ),
                ]
            )
        return presets

    def _age_available(self) -> bool:
        available = getattr(self._age_store, "available", None)
        if isinstance(available, bool):
            return available
        return bool(self._config.knowledge_age_enabled)

    def _age_unavailable_deployment_reason(self) -> str:
        reason = getattr(self._age_store, "unavailable_reason", None)
        if isinstance(reason, str) and reason.strip():
            return reason.strip()
        if not self._config.knowledge_age_enabled:
            return "Apache AGE is disabled by configuration."
        return postgres_age_required_reason()

    def _version_age_sync_status(
        self,
        version: CaliberKnowledgeBaseVersion,
    ) -> str | None:
        summary = version.summary if isinstance(version.summary, dict) else {}
        raw = summary.get("age_sync_status")
        if raw is None:
            return None
        normalized = str(raw).strip().lower()
        return normalized or None

    def _version_age_ready(
        self,
        version: CaliberKnowledgeBaseVersion,
        *,
        graph_config: KnowledgeGraphConfigSchema | None = None,
    ) -> bool:
        resolved = graph_config or self._resolve_graph_config(version.graph_config)
        return bool(
            self._age_available()
            and resolved.output_target == "object_store_and_age"
            and self._version_age_sync_status(version) == "synced"
        )

    def _age_unavailable_reason(
        self,
        version: CaliberKnowledgeBaseVersion,
        *,
        graph_config: KnowledgeGraphConfigSchema | None = None,
    ) -> str:
        if not self._age_available():
            return self._age_unavailable_deployment_reason()
        resolved = graph_config or self._resolve_graph_config(version.graph_config)
        if resolved.output_target != "object_store_and_age":
            return "This knowledge-base version was not synced to Apache AGE."
        age_status = self._version_age_sync_status(version)
        if age_status in {"queued", "pending", "processing"}:
            return "This knowledge-base version is still syncing to Apache AGE."
        if age_status == "failed":
            return "This knowledge-base version did not finish syncing to Apache AGE."
        return "Apache AGE graph retrieval is not available for this version."

    def _resolve_graph_config(
        self,
        raw: KnowledgeGraphConfigSchema | dict[str, Any] | None,
        fallback: KnowledgeGraphConfigSchema | dict[str, Any] | None = None,
    ) -> KnowledgeGraphConfigSchema:
        payload = self._default_graph_config().model_dump()
        if fallback:
            payload.update(self._coerce_graph_config_dict(fallback))
        if raw is not None:
            payload.update(self._coerce_graph_config_dict(raw, exclude_unset=True))
        return KnowledgeGraphConfigSchema.model_validate(payload)

    def _coerce_graph_config_dict(
        self,
        value: KnowledgeGraphConfigSchema | dict[str, Any],
        *,
        exclude_unset: bool = False,
    ) -> dict[str, Any]:
        if isinstance(value, KnowledgeGraphConfigSchema):
            return value.model_dump(exclude_unset=exclude_unset)
        return dict(value)

    def _assert_graph_target_supported(self, graph_config: KnowledgeGraphConfigSchema) -> None:
        if graph_config.output_target == "object_store_and_age" and not self._age_available():
            raise HTTPException(
                status_code=400,
                detail=self._age_unavailable_deployment_reason(),
            )

    def _prefer_age_graph_target(
        self,
        graph_config: KnowledgeGraphConfigSchema,
        *,
        explicit: bool,
        promote_default_retrieval_when_upgrading: bool = False,
    ) -> KnowledgeGraphConfigSchema:
        was_age_target = graph_config.output_target == "object_store_and_age"
        if explicit or not self._age_available() or was_age_target:
            return graph_config
        payload = graph_config.model_dump()
        payload["output_target"] = "object_store_and_age"
        if promote_default_retrieval_when_upgrading:
            payload["default_retrieval_mode"] = "age_graph"
        return KnowledgeGraphConfigSchema.model_validate(payload)

    def _apply_query_graph_overrides(
        self,
        graph_config: KnowledgeGraphConfigSchema,
        graph_overrides: KnowledgeQueryGraphOverridesSchema | None,
    ) -> KnowledgeGraphConfigSchema:
        if graph_overrides is None or not graph_overrides.is_active():
            return graph_config
        payload = graph_config.model_dump()
        if graph_overrides.retrieval_strength is not None:
            payload["retrieval_strength"] = graph_overrides.retrieval_strength
        if graph_overrides.minimum_relationship_weight is not None:
            payload["minimum_relationship_weight"] = graph_overrides.minimum_relationship_weight
        if graph_overrides.age_seed_mode is not None:
            payload["age_seed_mode"] = graph_overrides.age_seed_mode
        if graph_overrides.age_traversal_hops is not None:
            payload["age_traversal_hops"] = graph_overrides.age_traversal_hops
        if graph_overrides.age_candidate_pool_size is not None:
            payload["age_candidate_pool_size"] = graph_overrides.age_candidate_pool_size
        if graph_overrides.age_dense_rerank_weight is not None:
            payload["age_dense_rerank_weight"] = graph_overrides.age_dense_rerank_weight
        return KnowledgeGraphConfigSchema.model_validate(payload)

    def _finalize_query_graph_context(
        self,
        graph_context: dict[str, Any],
        *,
        graph_config: KnowledgeGraphConfigSchema,
        graph_overrides: KnowledgeQueryGraphOverridesSchema | None,
        retrieval_mode: KnowledgeRetrievalMode,
        age_ready: bool,
    ) -> dict[str, Any]:
        resolved = dict(graph_context)
        resolved.setdefault("retrieval_strength", graph_config.retrieval_strength)
        resolved.setdefault(
            "minimum_relationship_weight",
            graph_config.minimum_relationship_weight,
        )
        resolved.setdefault("age_configured_seed_mode", graph_config.age_seed_mode)
        resolved.setdefault("age_configured_hops", graph_config.age_traversal_hops)
        resolved.setdefault("age_candidate_pool_size", graph_config.age_candidate_pool_size)
        resolved.setdefault("age_dense_rerank_weight", graph_config.age_dense_rerank_weight)
        resolved.setdefault("age_ready", age_ready)
        resolved.setdefault("age_requested", retrieval_mode == "age_graph")
        if graph_overrides is not None and graph_overrides.is_active():
            override_payload = graph_overrides.model_dump(exclude_none=True)
            if not graph_overrides.strict_age_retrieval:
                override_payload.pop("strict_age_retrieval", None)
            resolved["query_override_active"] = bool(override_payload)
            if override_payload:
                resolved["query_overrides"] = override_payload
            resolved.setdefault(
                "strict_age_retrieval",
                graph_overrides.strict_age_retrieval,
            )
        else:
            resolved.setdefault("query_override_active", False)
            resolved.setdefault(
                "strict_age_retrieval",
                graph_config.strict_age_retrieval_default
                if retrieval_mode == "age_graph"
                else False,
            )
        return resolved

    def _question_graph_bundle(
        self,
        *,
        question: str,
        graph_config: KnowledgeGraphConfigSchema,
    ) -> Any:
        return build_graph_bundle(
            [
                {
                    "chunk_id": "query",
                    "document_id": "query",
                    "source_key": "query",
                    "content": question,
                    "metadata": {},
                }
            ],
            backend=graph_config.extractor_backend,
            spacy_model=graph_config.spacy_model or self._config.knowledge_graph_spacy_model,
            max_entities_per_chunk=min(max(8, graph_config.max_entities_per_chunk), 24),
            entity_types=set(graph_config.entity_types),
            minimum_entity_mentions=1,
            minimum_relationship_weight=0.0,
        )

    def _object_store(self) -> Any:
        if self._object_store_client is not None:
            return self._object_store_client
        try:
            boto3 = importlib.import_module("boto3")
            boto_config_cls = importlib.import_module("botocore.config").Config
        except ImportError as exc:  # pragma: no cover - only without optional deps
            raise KnowledgeDependencyError(
                "Knowledge-base object-store access needs boto3. Install caliber[s3]."
            ) from exc
        access_key = resolve_secret(self._config.object_store_access_key_source)
        secret_key = resolve_secret(self._config.object_store_secret_key_source)
        self._object_store_client = boto3.client(
            "s3",
            endpoint_url=self._config.object_store_endpoint_url or None,
            region_name=self._config.object_store_region,
            aws_access_key_id=access_key,
            aws_secret_access_key=secret_key,
            config=boto_config_cls(
                s3={
                    "addressing_style": "path"
                    if self._config.object_store_force_path_style
                    else "auto"
                },
                retries={"max_attempts": 2, "mode": "standard"},
                connect_timeout=5,
                read_timeout=120,
            ),
        )
        return self._object_store_client

    def _generate_answer(
        self,
        *,
        question: str,
        history: list[dict[str, str]],
        knowledge_base: CaliberKnowledgeBase,
        version: CaliberKnowledgeBaseVersion,
        retrieved_chunks: list[KnowledgeQueryChunkSchema],
        chat_model: str | None,
    ) -> tuple[str | None, str | None]:
        if not retrieved_chunks:
            return None, "No relevant chunks were retrieved for this question."
        engine = (self._config.assistant_engine or "fake").lower()
        model = chat_model or self._config.assistant_model
        if engine == "openai":
            try:
                return self._generate_openai_answer(
                    model=model,
                    question=question,
                    history=history,
                    knowledge_base=knowledge_base,
                    version=version,
                    retrieved_chunks=retrieved_chunks,
                ), None
            except Exception as exc:  # pragma: no cover - network/provider dependent
                return _fallback_answer(question, retrieved_chunks), str(exc)
        if engine == "anthropic":
            try:
                return self._generate_anthropic_answer(
                    model=model,
                    question=question,
                    history=history,
                    knowledge_base=knowledge_base,
                    version=version,
                    retrieved_chunks=retrieved_chunks,
                ), None
            except Exception as exc:  # pragma: no cover - network/provider dependent
                return _fallback_answer(question, retrieved_chunks), str(exc)
        return _fallback_answer(question, retrieved_chunks), None

    def _generate_openai_answer(
        self,
        *,
        model: str,
        question: str,
        history: list[dict[str, str]],
        knowledge_base: CaliberKnowledgeBase,
        version: CaliberKnowledgeBaseVersion,
        retrieved_chunks: list[KnowledgeQueryChunkSchema],
    ) -> str:
        from openai import OpenAI  # noqa: PLC0415

        api_key = resolve_secret(self._config.llm_api_key_env)
        if not api_key:
            raise RuntimeError("OpenAI API key is not configured")
        client = OpenAI(api_key=api_key)
        messages: list[dict[str, str]] = [
            {
                "role": "system",
                "content": _rag_system_prompt(knowledge_base.name, version.version_number),
            }
        ]
        for message in history[-6:]:
            role = message.get("role", "user")
            if role not in {"user", "assistant"}:
                role = "user"
            messages.append({"role": role, "content": message.get("content", "")})
        messages.append({"role": "user", "content": _rag_user_prompt(question, retrieved_chunks)})
        response = client.chat.completions.create(model=model, messages=cast(Any, messages))
        return str(response.choices[0].message.content or "").strip()

    def _generate_anthropic_answer(
        self,
        *,
        model: str,
        question: str,
        history: list[dict[str, str]],
        knowledge_base: CaliberKnowledgeBase,
        version: CaliberKnowledgeBaseVersion,
        retrieved_chunks: list[KnowledgeQueryChunkSchema],
    ) -> str:
        from anthropic import Anthropic  # type: ignore[import-not-found]  # noqa: PLC0415

        api_key = resolve_secret("ANTHROPIC_API_KEY")
        if not api_key:
            raise RuntimeError("Anthropic API key is not configured")
        client = Anthropic(api_key=api_key)
        messages: list[dict[str, str]] = []
        for message in history[-6:]:
            role = message.get("role", "user")
            if role not in {"user", "assistant"}:
                role = "user"
            messages.append({"role": role, "content": message.get("content", "")})
        messages.append({"role": "user", "content": _rag_user_prompt(question, retrieved_chunks)})
        response = client.messages.create(
            model=model,
            max_tokens=1200,
            system=_rag_system_prompt(knowledge_base.name, version.version_number),
            messages=messages,
        )
        parts: list[str] = []
        for block in response.content:
            text = getattr(block, "text", "")
            if text:
                parts.append(str(text))
        return "".join(parts).strip()

    # ------------------------------------------------------------------
    # Calibration — durable retrieval-quality test runs (Phase K1)
    # ------------------------------------------------------------------
    def calibrate(
        self,
        knowledge_base_id: str,
        *,
        version_id: str,
        eval_dataset_id: str,
        eval_dataset_version: int | None = None,
        retrieval_mode: KnowledgeRetrievalMode = "dense",
        top_k: int = 6,
        identity: CaliberIdentity,
        actor: str,
        judge: kb_calibration.KbJudge | None = None,
        runner: kb_calibration.QuestionRunner | None = None,
    ) -> KnowledgeCalibrationRunSummary:
        """Score one KB version against an eval dataset and persist a durable run.

        Synchronous for now — for a tiny question set the retrieve+judge loop is
        quick. (It can move to the build worker later if datasets grow.) The
        ``judge`` (LLM-judged metrics) and ``runner`` (per-question retrieval +
        answer) are both **injectable** so tests stub them deterministically; in
        production ``judge`` defaults to one built from config and ``runner``
        defaults to one backed by :meth:`query`.

        Raises 404 if the KB, version, or dataset is missing (or the version
        doesn't belong to the KB); 400 if the dataset has no examples at the
        requested version.
        """
        with self._session_factory() as session:
            knowledge_base = self._require_visible_knowledge_base(
                session, knowledge_base_id, identity
            )
            _version, version_base = self._require_visible_version(session, version_id, identity)
            if version_base.knowledge_base_id != knowledge_base.knowledge_base_id:
                raise HTTPException(
                    status_code=404,
                    detail=(
                        f"version {version_id!r} does not belong to knowledge base "
                        f"{knowledge_base_id!r}"
                    ),
                )
            dataset = session.get(CaliberEvalDataset, eval_dataset_id)
            if dataset is None:
                raise HTTPException(
                    status_code=404, detail=f"eval dataset {eval_dataset_id!r} not found"
                )
            questions = self._load_calibration_questions(
                session, eval_dataset_id, eval_dataset_version
            )
            if not questions:
                suffix = (
                    "" if eval_dataset_version is None else f" at version {eval_dataset_version}"
                )
                raise HTTPException(
                    status_code=400,
                    detail=f"eval dataset {eval_dataset_id!r} has no examples{suffix}",
                )

        resolved_judge = judge if judge is not None else self._build_calibration_judge()
        resolved_runner = (
            runner
            if runner is not None
            else self._build_query_runner(version_id=version_id, identity=identity)
        )

        outcome = kb_calibration.run_calibration(
            questions=questions,
            runner=resolved_runner,
            top_k=top_k,
            retrieval_mode=retrieval_mode,
            judge=resolved_judge,
        )

        now = _utcnow()
        with self._session_factory() as session:
            run = CaliberKnowledgeBaseTestRun(
                test_run_id=new_knowledge_base_test_run_id(),
                knowledge_base_id=knowledge_base_id,
                knowledge_base_version_id=version_id,
                eval_dataset_id=eval_dataset_id,
                eval_dataset_version=eval_dataset_version,
                retrieval_mode=retrieval_mode,
                top_k=top_k,
                test_set_size=len(questions),
                metrics=outcome.metrics,
                results=outcome.results,
                created_by=actor,
                status="completed",
                completed_at=now,
            )
            session.add(run)
            session.flush()
            audit_record(
                session,
                actor=actor,
                action="calibrate_knowledge_base",
                entity_type="knowledge_base_test_run",
                entity_id=run.test_run_id,
                details={
                    "knowledge_base_id": knowledge_base_id,
                    "knowledge_base_version_id": version_id,
                    "eval_dataset_id": eval_dataset_id,
                    "eval_dataset_version": eval_dataset_version,
                    "retrieval_mode": retrieval_mode,
                    "top_k": top_k,
                    "test_set_size": len(questions),
                },
            )
            session.commit()
            return KnowledgeCalibrationRunSummary.model_validate(run)

    def list_calibration_runs(
        self,
        knowledge_base_id: str,
        *,
        identity: CaliberIdentity,
        limit: int = 20,
    ) -> list[KnowledgeCalibrationRunSummary]:
        """Newest-first calibration-run summaries for a KB (no heavy ``results``)."""
        capped = max(1, min(limit, 100))
        with self._session_factory() as session:
            self._require_visible_knowledge_base(session, knowledge_base_id, identity)
            stmt = (
                select(CaliberKnowledgeBaseTestRun)
                .where(CaliberKnowledgeBaseTestRun.knowledge_base_id == knowledge_base_id)
                .order_by(CaliberKnowledgeBaseTestRun.created_at.desc())
                .limit(capped)
            )
            rows = session.execute(stmt).scalars().all()
            return [KnowledgeCalibrationRunSummary.model_validate(row) for row in rows]

    def get_calibration_run(
        self,
        test_run_id: str,
        *,
        identity: CaliberIdentity,
    ) -> CaliberKnowledgeBaseTestRun:
        """Return the full calibration-run row (incl. ``results``), or 404.

        The owning KB is re-checked for visibility so the detail route can't be
        used to read a run on a KB the caller can't see.
        """
        with self._session_factory() as session:
            run = session.get(CaliberKnowledgeBaseTestRun, test_run_id)
            if run is None:
                raise HTTPException(
                    status_code=404, detail=f"calibration run {test_run_id!r} not found"
                )
            self._require_visible_knowledge_base(session, run.knowledge_base_id, identity)
            session.expunge(run)
            return run

    def set_baseline(
        self,
        knowledge_base_id: str,
        *,
        test_run_id: str,
        identity: CaliberIdentity,
        actor: str,
    ) -> KnowledgeBaseSchema:
        """Pin a calibration run as the KB's comparison baseline.

        Raises 404 if the KB is missing; 400 if the run is unknown or belongs to
        a different KB.
        """
        with self._session_factory() as session:
            knowledge_base = self._require_visible_knowledge_base(
                session, knowledge_base_id, identity
            )
            run = session.get(CaliberKnowledgeBaseTestRun, test_run_id)
            if run is None or run.knowledge_base_id != knowledge_base_id:
                raise HTTPException(
                    status_code=400,
                    detail=(
                        f"calibration run {test_run_id!r} does not belong to knowledge base "
                        f"{knowledge_base_id!r}"
                    ),
                )
            knowledge_base.baseline_run_id = test_run_id
            audit_record(
                session,
                actor=actor,
                action="set_knowledge_base_baseline",
                entity_type="knowledge_base",
                entity_id=knowledge_base_id,
                details={"baseline_run_id": test_run_id},
            )
            session.commit()
            session.refresh(knowledge_base)
            return self._serialize_knowledge_base(knowledge_base)

    def delete(
        self,
        knowledge_base_id: str,
        *,
        identity: CaliberIdentity,
        actor: str,
    ) -> None:
        """Hard-delete a knowledge base and every row that hangs off it.

        Unlike the ``PATCH status=archived`` soft delete, this fully removes the
        KB and all of its versions, sources, chunks, entities, relationships,
        runs, run events, and calibration test runs in one transaction, in
        foreign-key-safe order (run-children before runs; relationships before
        the entities they reference; all version-scoped children before the
        versions). The KB's self-references (``active_version_id`` /
        ``last_run_id`` / ``baseline_run_id``) are nulled and flushed *before*
        the versions and runs they point at are removed, so deleting those rows
        can't violate the FK back-reference.

        External artifacts (object-store outputs and any Apache AGE subgraph) are
        cleaned up best-effort *before* the commit, each wrapped so a failure
        there never aborts the relational delete — the DB cascade is the
        guaranteed part. Raises 404 if the KB does not exist or is not visible.
        """
        with self._session_factory() as session:
            knowledge_base = self._require_visible_knowledge_base(
                session, knowledge_base_id, identity
            )
            knowledge_base_name = knowledge_base.name

            version_ids = list(
                session.execute(
                    select(CaliberKnowledgeBaseVersion.knowledge_base_version_id).where(
                        CaliberKnowledgeBaseVersion.knowledge_base_id == knowledge_base_id
                    )
                )
                .scalars()
                .all()
            )
            run_ids = list(
                session.execute(
                    select(CaliberKnowledgeBaseRun.knowledge_base_run_id).where(
                        CaliberKnowledgeBaseRun.knowledge_base_id == knowledge_base_id
                    )
                )
                .scalars()
                .all()
            )
            # Capture each version's object-store output location up front, while
            # the version rows still exist, so the best-effort prefix delete below
            # has somewhere to point after the rows are gone.
            version_outputs = list(
                session.execute(
                    select(
                        CaliberKnowledgeBaseVersion.output_bucket,
                        CaliberKnowledgeBaseVersion.output_prefix,
                    ).where(CaliberKnowledgeBaseVersion.knowledge_base_id == knowledge_base_id)
                ).all()
            )

            # Best-effort external cleanup — never let it abort the DB delete.
            for version_id in version_ids:
                self._best_effort_drop_age_version(version_id)
            for bucket, prefix in version_outputs:
                self._best_effort_delete_output_prefix(bucket, prefix)

            # Null the KB's self-references and flush so the FK back-pointers are
            # gone before the versions / runs they target are deleted.
            knowledge_base.active_version_id = None
            knowledge_base.last_run_id = None
            knowledge_base.baseline_run_id = None
            session.flush()

            if run_ids:
                session.execute(
                    delete(CaliberKnowledgeBaseRunEvent).where(
                        CaliberKnowledgeBaseRunEvent.knowledge_base_run_id.in_(run_ids)
                    )
                )
            session.execute(
                delete(CaliberKnowledgeBaseRun).where(
                    CaliberKnowledgeBaseRun.knowledge_base_id == knowledge_base_id
                )
            )
            if version_ids:
                # Relationships reference entities (source/target FKs), so they
                # must go before the entities they point at.
                session.execute(
                    delete(CaliberKnowledgeBaseRelationship).where(
                        CaliberKnowledgeBaseRelationship.knowledge_base_version_id.in_(version_ids)
                    )
                )
                session.execute(
                    delete(CaliberKnowledgeBaseEntity).where(
                        CaliberKnowledgeBaseEntity.knowledge_base_version_id.in_(version_ids)
                    )
                )
                session.execute(
                    delete(CaliberKnowledgeBaseChunk).where(
                        CaliberKnowledgeBaseChunk.knowledge_base_version_id.in_(version_ids)
                    )
                )
                session.execute(
                    delete(CaliberKnowledgeBaseSource).where(
                        CaliberKnowledgeBaseSource.knowledge_base_version_id.in_(version_ids)
                    )
                )
            session.execute(
                delete(CaliberKnowledgeBaseTestRun).where(
                    CaliberKnowledgeBaseTestRun.knowledge_base_id == knowledge_base_id
                )
            )
            session.execute(
                delete(CaliberKnowledgeBaseVersion).where(
                    CaliberKnowledgeBaseVersion.knowledge_base_id == knowledge_base_id
                )
            )
            session.delete(knowledge_base)

            audit_record(
                session,
                actor=actor,
                action="delete_knowledge_base",
                entity_type="knowledge_base",
                entity_id=knowledge_base_id,
                details={"name": knowledge_base_name},
            )
            session.commit()

    def _best_effort_drop_age_version(self, version_id: str) -> None:
        """Drop one version's AGE subgraph, swallowing any failure.

        AGE may be disabled or unreachable; either way a failure here must not
        block the relational delete (the synced graph is a derived projection).
        """
        try:
            if not self._age_available():
                return
            self._age_store.drop_version(version_id=version_id)
        except Exception:
            logger.warning(
                "best-effort AGE subgraph drop failed for knowledge-base version %s",
                version_id,
                exc_info=True,
            )

    def _best_effort_delete_output_prefix(self, bucket: str, prefix: str) -> None:
        """Delete every object under ``bucket/prefix`` best-effort.

        Mirrors the object-store routes' folder delete: list keys under the
        version's output prefix (paginated) and ``delete_objects`` them in
        batches of 1000. Any object-store failure (missing bucket, no client,
        network) is logged and swallowed so the DB delete stays authoritative.
        """
        if not bucket or not prefix:
            return
        normalized = prefix if prefix.endswith("/") else f"{prefix}/"
        try:
            client = self._object_store()
            if client is None:
                return
            keys: list[str] = []
            token: str | None = None
            while True:
                kwargs: dict[str, Any] = {
                    "Bucket": bucket,
                    "Prefix": normalized,
                    "MaxKeys": 1000,
                }
                if token:
                    kwargs["ContinuationToken"] = token
                response = client.list_objects_v2(**kwargs)
                keys.extend(item["Key"] for item in response.get("Contents", []) or [])
                token = response.get("NextContinuationToken")
                if not response.get("IsTruncated"):
                    break
            for start in range(0, len(keys), 1000):  # S3 caps delete_objects at 1000 keys
                chunk = keys[start : start + 1000]
                client.delete_objects(
                    Bucket=bucket,
                    Delete={"Objects": [{"Key": key} for key in chunk], "Quiet": True},
                )
        except Exception:
            logger.warning(
                "best-effort object-store cleanup failed for knowledge-base output %s/%s",
                bucket,
                prefix,
                exc_info=True,
            )

    def _load_calibration_questions(
        self,
        session: Session,
        eval_dataset_id: str,
        version: int | None,
    ) -> list[tuple[str, dict[str, Any] | None]]:
        """Resolve ``(question, expected)`` pairs from a dataset, version-pinned.

        Mirrors :func:`caliber.eval.predict.build_db_load_dataset`'s versioned
        reconstruction: the current active set when ``version`` is ``None``, else
        the set "as of version N" (``dataset_version <= N`` minus rows retired at
        or before N). Each example's ``input`` must carry a non-blank
        ``question``; examples without one are skipped.
        """
        stmt = (
            select(CaliberEvalDatasetExample)
            .where(CaliberEvalDatasetExample.dataset_id == eval_dataset_id)
            .order_by(CaliberEvalDatasetExample.created_at)
        )
        if version is None:
            stmt = stmt.where(CaliberEvalDatasetExample.superseded_at.is_(None))
        else:
            stmt = stmt.where(CaliberEvalDatasetExample.dataset_version <= version).where(
                or_(
                    CaliberEvalDatasetExample.superseded_version.is_(None),
                    CaliberEvalDatasetExample.superseded_version > version,
                )
            )
        rows = session.execute(stmt).scalars().all()
        questions: list[tuple[str, dict[str, Any] | None]] = []
        for row in rows:
            payload = dict(row.input or {})
            question = payload.get("question")
            if not isinstance(question, str) or not question.strip():
                continue
            expected = dict(row.expected) if row.expected else None
            questions.append((question.strip(), expected))
        return questions

    def _build_calibration_judge(self) -> kb_calibration.KbJudge | None:
        """Build the faithfulness + correctness judges, or ``None`` if unavailable.

        Goes through the unified judge path
        (:func:`caliber.knowledge.calibration.build_kb_judge` → ``make_judge``),
        the same machinery the Evaluations scorecard and optimization gate use, so
        every judged metric in the platform is built one way. Returns ``None`` when
        the judges can't be built so calibration degrades to its deterministic
        retrieval metrics rather than failing.
        """
        model = getattr(self._config, "llm_diagnosis_model", None)
        return kb_calibration.build_kb_judge(model)

    def _build_query_runner(
        self,
        *,
        version_id: str,
        identity: CaliberIdentity,
    ) -> kb_calibration.QuestionRunner:
        """A ``QuestionRunner`` that drives :meth:`query` for one KB version.

        Each call runs a single-version, single-mode retrieval and packages the
        ranked chunks into a :class:`~caliber.knowledge.calibration.RetrievalOutcome`.
        """

        def run(question: str, top_k: int, retrieval_mode: str) -> kb_calibration.RetrievalOutcome:
            payload = KnowledgeQueryRequest(
                version_ids=[version_id],
                question=question,
                top_k=top_k,
                retrieval_modes=[cast(KnowledgeRetrievalMode, retrieval_mode)],
            )
            result = self.query(payload, identity=identity)
            version_result = result.versions[0] if result.versions else None
            if version_result is None:
                return kb_calibration.RetrievalOutcome(
                    answer=None,
                    retrieved_sources=[],
                    retrieved_chunk_texts=[],
                    retrieved_source_keys=[],
                    answer_error="no version result returned",
                )
            retrieved_sources = [
                kb_calibration.chunk_source_identifiers(chunk)
                for chunk in version_result.retrieved_chunks
            ]
            return kb_calibration.RetrievalOutcome(
                answer=version_result.answer,
                retrieved_sources=retrieved_sources,
                retrieved_chunk_texts=[chunk.content for chunk in version_result.retrieved_chunks],
                retrieved_source_keys=[
                    chunk.source_key or chunk.source_name
                    for chunk in version_result.retrieved_chunks
                ],
                answer_error=version_result.answer_error,
            )

        return run

    def _output_prefix(self, knowledge_base_id: str, version_number: int, version_id: str) -> str:
        return f"{_RESERVED_OUTPUT_PREFIX}/{knowledge_base_id}/versions/{version_number:04d}-{version_id}"


# ----------------------------------------------------------------------
# Small pure helpers
# ----------------------------------------------------------------------


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _basename(path: str) -> str:
    return path.rsplit("/", 1)[-1] or path.rstrip("/")


def _selection_dict(kind: str, path: str) -> dict[str, str]:
    return {"kind": kind, "path": path}


def _fingerprint_sources(bucket: str, source_manifest: list[dict[str, str]]) -> str:
    payload = json.dumps(
        {
            "bucket": bucket,
            "sources": sorted(source_manifest, key=lambda item: (item["kind"], item["path"])),
        },
        sort_keys=True,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _object_store_path(bucket: str, key: str) -> str:
    return f"/object-store?bucket={quote(bucket, safe='')}&key={quote(key, safe='')}"


def _artifact_uri(bucket: str, prefix: str, filename: str) -> str:
    return f"s3://{bucket}/{prefix}/{filename}"


def _estimate_token_count(text: str) -> int:
    return max(1, len(text.split()))


def _normalize_query_text(text: str) -> str:
    return " ".join("".join(char.lower() if char.isalnum() else " " for char in text).split())


def _lexical_tokens(text: str) -> list[str]:
    """Lowercase, split on non-alphanumeric runs, drop empties and stopwords."""

    return [
        token
        for token in re.split(r"[^a-z0-9]+", text.lower())
        if token and token not in _LEXICAL_STOPWORDS
    ]


def _strip_etag(value: Any) -> str | None:
    if value is None:
        return None
    return str(value).strip('"')


def _looks_supported(key: str, content_type: str | None) -> bool:
    suffix = Path(key).suffix.lower()
    if suffix in _SUPPORTED_EXTENSIONS:
        return True
    media = (content_type or "").lower()
    return any(hint in media for hint in _TEXT_MEDIA_HINTS)


def _cosine_similarity(left: list[float], right: list[float]) -> float:
    if not left or not right or len(left) != len(right):
        return 0.0
    dot = sum(a * b for a, b in zip(left, right, strict=False))
    left_norm = sum(value * value for value in left) ** 0.5
    right_norm = sum(value * value for value in right) ** 0.5
    if left_norm == 0 or right_norm == 0:
        return 0.0
    return float(dot / (left_norm * right_norm))


def _rag_system_prompt(knowledge_base_name: str, version_number: int) -> str:
    return (
        "You are CALIBER's document-grounded retrieval assistant. "
        f"Answer only from the provided chunks for knowledge base '{knowledge_base_name}' version {version_number}. "
        "If the evidence is incomplete, say so clearly. Cite claims inline using the provided [n] labels."
    )


def _rag_user_prompt(question: str, retrieved_chunks: list[KnowledgeQueryChunkSchema]) -> str:
    context_blocks = []
    for index, chunk in enumerate(retrieved_chunks, start=1):
        context_blocks.append(
            "\n".join(
                [
                    f"[{index}] source={chunk.source_key}",
                    chunk.content,
                ]
            )
        )
    joined = "\n\n".join(context_blocks)
    return f"Question: {question}\n\nRetrieved context:\n\n{joined}"


def _fallback_answer(question: str, retrieved_chunks: list[KnowledgeQueryChunkSchema]) -> str:
    lines = [f"Question: {question}", "", "Most relevant retrieved evidence:"]
    for index, chunk in enumerate(retrieved_chunks[:4], start=1):
        preview = chunk.content.strip().replace("\n", " ")
        if len(preview) > _FALLBACK_PREVIEW_LIMIT:
            preview = f"{preview[: _FALLBACK_PREVIEW_LIMIT - 3].rstrip()}..."
        lines.append(f"[{index}] {chunk.source_name}: {preview}")
    lines.append("")
    lines.append(
        "Answer generated from retrieved passages only; inspect the cited chunks below for full context."
    )
    return "\n".join(lines)


def _document_id(bucket: str, key: str) -> str:
    return hashlib.sha256(f"{bucket}:{key}".encode()).hexdigest()[:16]


def _event_created_at(payload: dict[str, Any] | None) -> datetime:
    raw = payload.get("at") if isinstance(payload, dict) else None
    if isinstance(raw, str):
        try:
            return datetime.fromisoformat(raw.replace("Z", "+00:00"))
        except ValueError:
            return _utcnow()
    return _utcnow()
