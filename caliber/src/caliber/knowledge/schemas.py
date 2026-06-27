"""Pydantic schemas for knowledge-base APIs."""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from caliber.knowledge.graph import GRAPH_ENTITY_TYPE_IDS, GraphExtractorBackend

KnowledgeBaseStatus = Literal["active", "archived"]
KnowledgeBuildStatus = Literal["queued", "processing", "completed", "failed"]
KnowledgeSourceKind = Literal["file", "folder"]
KnowledgeMessageRole = Literal["user", "assistant"]
KnowledgeRetrievalMode = Literal["dense", "hybrid", "graph_hybrid", "age_graph"]
KnowledgeGraphOutputTarget = Literal["object_store", "object_store_and_age"]
KnowledgeGraphRetrievalStrength = Literal["conservative", "balanced", "aggressive"]
KnowledgeAgeSeedMode = Literal[
    "entity_then_text",
    "query_entities_only",
    "query_text_only",
    "query_entities_and_text",
]
KnowledgeGraphExploreSource = Literal["local", "age"]
KnowledgeGraphBuildPresetId = Literal["portable", "balanced", "age_native", "age_strict"]
KnowledgeGraphQueryPresetId = Literal[
    "hybrid_precision",
    "hybrid_balanced",
    "age_balanced",
    "age_native",
    "age_strict",
]


class KnowledgeGraphConfigSchema(BaseModel):
    """User-configurable graph extraction, filtering, and sync settings."""

    model_config = ConfigDict(extra="forbid")

    extractor_backend: GraphExtractorBackend = "heuristic"
    spacy_model: str | None = Field(default=None, max_length=128)
    max_entities_per_chunk: int = Field(default=12, ge=1, le=32)
    entity_types: list[str] = Field(default_factory=list, max_length=16)
    minimum_entity_mentions: int = Field(default=1, ge=1, le=50)
    minimum_relationship_weight: float = Field(default=1.0, ge=0.0, le=10_000.0)
    default_retrieval_mode: KnowledgeRetrievalMode = "graph_hybrid"
    retrieval_strength: KnowledgeGraphRetrievalStrength = "balanced"
    output_target: KnowledgeGraphOutputTarget = "object_store"
    age_seed_mode: KnowledgeAgeSeedMode = "entity_then_text"
    age_traversal_hops: int = Field(default=1, ge=0, le=2)
    age_candidate_pool_size: int = Field(default=24, ge=4, le=200)
    age_dense_rerank_weight: float = Field(default=0.35, ge=0.0, le=3.0)
    strict_age_retrieval_default: bool = False

    @model_validator(mode="after")
    def _normalize_entity_types(self) -> KnowledgeGraphConfigSchema:
        ordered = list(
            dict.fromkeys(item.strip().lower() for item in self.entity_types if item.strip())
        )
        unknown = [item for item in ordered if item not in GRAPH_ENTITY_TYPE_IDS]
        if unknown:
            raise ValueError("unknown graph entity types: " + ", ".join(sorted(unknown)))
        self.entity_types = ordered
        if self.extractor_backend != "spacy":
            self.spacy_model = None
        elif self.spacy_model:
            self.spacy_model = self.spacy_model.strip() or None
        if (
            self.output_target != "object_store_and_age"
            and self.default_retrieval_mode == "age_graph"
        ):
            self.default_retrieval_mode = "graph_hybrid"
        if self.output_target != "object_store_and_age":
            self.strict_age_retrieval_default = False
        return self


class KnowledgeGraphBuildPresetSchema(BaseModel):
    """One named graph profile exposed to the KB build UX."""

    model_config = ConfigDict(extra="forbid")

    id: KnowledgeGraphBuildPresetId
    label: str = Field(min_length=1, max_length=128)
    eyebrow: str = Field(min_length=1, max_length=64)
    description: str = Field(min_length=1, max_length=512)
    badges: list[str] = Field(default_factory=list, max_length=8)
    patch: dict[str, object] = Field(default_factory=dict)
    recommended: bool = False
    age_required: bool = False


class KnowledgeGraphQueryPresetSchema(BaseModel):
    """One named graph query profile exposed to explorer / playground UX."""

    model_config = ConfigDict(extra="forbid")

    id: KnowledgeGraphQueryPresetId
    label: str = Field(min_length=1, max_length=128)
    eyebrow: str = Field(min_length=1, max_length=64)
    description: str = Field(min_length=1, max_length=512)
    badges: list[str] = Field(default_factory=list, max_length=8)
    retrieval_mode: KnowledgeRetrievalMode
    patch: dict[str, object] = Field(default_factory=dict)
    recommended: bool = False
    age_required: bool = False


class KnowledgeSourceSelectionRequest(BaseModel):
    """One object-store source selected for a knowledge-base build."""

    model_config = ConfigDict(extra="forbid")

    kind: KnowledgeSourceKind
    path: str = Field(min_length=1, max_length=2048)


class KnowledgeBaseCreateRequest(BaseModel):
    """Body of ``POST /knowledge-bases``."""

    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=256)
    description: str = Field(default="", max_length=4096)
    source_bucket: str = Field(min_length=3, max_length=256)
    sources: list[KnowledgeSourceSelectionRequest] = Field(min_length=1)
    chunking_strategy: str = Field(min_length=1, max_length=64)
    embedding_model: str = Field(min_length=1, max_length=256)
    chunking_config: dict[str, object] = Field(default_factory=dict)
    graph_config: KnowledgeGraphConfigSchema | None = None


class KnowledgeBaseVersionCreateRequest(BaseModel):
    """Body of ``POST /knowledge-bases/{knowledge_base_id}/versions``."""

    model_config = ConfigDict(extra="forbid")

    sources: list[KnowledgeSourceSelectionRequest] | None = None
    chunking_strategy: str = Field(min_length=1, max_length=64)
    embedding_model: str = Field(min_length=1, max_length=256)
    chunking_config: dict[str, object] = Field(default_factory=dict)
    graph_config: KnowledgeGraphConfigSchema | None = None


class KnowledgeBaseUpdateRequest(BaseModel):
    """Body of ``PATCH /knowledge-bases/{knowledge_base_id}``."""

    model_config = ConfigDict(extra="forbid")

    name: str | None = Field(default=None, min_length=1, max_length=256)
    description: str | None = Field(default=None, max_length=4096)
    status: KnowledgeBaseStatus | None = None


class KnowledgeMessageSchema(BaseModel):
    """One prior turn passed into the RAG playground."""

    model_config = ConfigDict(extra="forbid")

    role: KnowledgeMessageRole
    content: str = Field(min_length=1)


class KnowledgeQueryGraphOverridesSchema(BaseModel):
    """Optional query-time overrides for graph-aware retrieval."""

    model_config = ConfigDict(extra="forbid")

    retrieval_strength: KnowledgeGraphRetrievalStrength | None = None
    minimum_relationship_weight: float | None = Field(default=None, ge=0.0, le=10_000.0)
    age_seed_mode: KnowledgeAgeSeedMode | None = None
    age_traversal_hops: int | None = Field(default=None, ge=0, le=2)
    age_candidate_pool_size: int | None = Field(default=None, ge=4, le=200)
    age_dense_rerank_weight: float | None = Field(default=None, ge=0.0, le=3.0)
    strict_age_retrieval: bool = False

    def is_active(self) -> bool:
        return (
            any(
                value is not None
                for value in (
                    self.retrieval_strength,
                    self.minimum_relationship_weight,
                    self.age_seed_mode,
                    self.age_traversal_hops,
                    self.age_candidate_pool_size,
                    self.age_dense_rerank_weight,
                )
            )
            or self.strict_age_retrieval
        )


class KnowledgeQueryRequest(BaseModel):
    """Body of ``POST /knowledge/query``."""

    model_config = ConfigDict(extra="forbid")

    version_ids: list[str] = Field(min_length=1, max_length=3)
    question: str = Field(min_length=1)
    history: list[KnowledgeMessageSchema] = Field(default_factory=list)
    top_k: int = Field(default=6, ge=1, le=20)
    chat_model: str | None = Field(default=None, max_length=256)
    retrieval_modes: list[KnowledgeRetrievalMode] = Field(
        default_factory=list,
        max_length=2,
    )
    graph_overrides: KnowledgeQueryGraphOverridesSchema | None = None

    @model_validator(mode="after")
    def _dedupe_retrieval_modes(self) -> KnowledgeQueryRequest:
        ordered = list(dict.fromkeys(self.retrieval_modes))
        self.retrieval_modes = ordered
        return self


class KnowledgeGraphExploreRequest(BaseModel):
    """Query params for the dedicated knowledge-graph explorer view."""

    model_config = ConfigDict(extra="forbid")

    source: KnowledgeGraphExploreSource = "local"
    q: str = Field(default="", max_length=256)
    entity_type: str | None = Field(default=None, max_length=64)
    minimum_relationship_weight: float | None = Field(default=None, ge=0.0, le=10_000.0)
    traversal_hops: int | None = Field(default=None, ge=0, le=2)
    age_seed_mode: KnowledgeAgeSeedMode | None = None
    strict_age_retrieval: bool = False
    node_limit: int = Field(default=12, ge=4, le=48)

    @model_validator(mode="after")
    def _normalize(self) -> KnowledgeGraphExploreRequest:
        self.q = self.q.strip()
        self.entity_type = (self.entity_type or "").strip().lower() or None
        return self


class KnowledgeBaseSchema(BaseModel):
    """Serialized form of :class:`caliber.db.models.CaliberKnowledgeBase`."""

    model_config = ConfigDict(from_attributes=True)

    knowledge_base_id: str
    project_id: str | None
    visibility: str
    name: str
    description: str
    owner: str
    status: str
    source_bucket: str
    source_manifest: list[dict[str, object]] = Field(default_factory=list)
    source_fingerprint: str
    active_version_id: str | None
    baseline_run_id: str | None = None
    last_run_id: str | None
    last_run_status: str | None
    last_run_completed_at: datetime | None
    created_at: datetime
    updated_at: datetime
    active_version_summary: KnowledgeBaseActiveVersionSummarySchema | None = None


class KnowledgeBaseActiveVersionSummarySchema(BaseModel):
    """Compact graph/retrieval summary for a KB's active or latest version."""

    model_config = ConfigDict(extra="forbid")

    knowledge_base_version_id: str
    version_number: int
    status: str
    chunking_strategy: str
    embedding_model: str
    graph_extractor: GraphExtractorBackend
    graph_target: KnowledgeGraphOutputTarget
    default_retrieval_mode: KnowledgeRetrievalMode
    retrieval_strength: KnowledgeGraphRetrievalStrength
    graph_profile_id: str | None = None
    graph_profile_label: str | None = None
    age_sync_status: str | None = None
    age_ready: bool = False
    age_graph_name: str | None = None
    chunk_count: int = 0
    entity_count: int = 0
    relationship_count: int = 0
    created_at: datetime
    completed_at: datetime | None


class KnowledgeBaseVersionSchema(BaseModel):
    """Serialized form of :class:`caliber.db.models.CaliberKnowledgeBaseVersion`."""

    model_config = ConfigDict(from_attributes=True)

    knowledge_base_version_id: str
    knowledge_base_id: str
    version_number: int
    status: str
    chunking_strategy: str
    chunking_config: dict[str, object] = Field(default_factory=dict)
    graph_config: KnowledgeGraphConfigSchema = Field(default_factory=KnowledgeGraphConfigSchema)
    graph_profile_id: str | None = None
    graph_profile_label: str | None = None
    embedding_provider: str
    embedding_model: str
    embedding_dimension: int | None
    source_manifest: list[dict[str, object]] = Field(default_factory=list)
    source_fingerprint: str
    output_bucket: str
    output_prefix: str
    chunks_uri: str | None
    entities_uri: str | None
    relationships_uri: str | None
    graph_uri: str | None
    manifest_uri: str | None
    logs_uri: str | None
    stats_uri: str | None
    summary: dict[str, object] | None
    error_summary: str | None
    created_by: str
    created_at: datetime
    completed_at: datetime | None


class KnowledgeBaseSourceSchema(BaseModel):
    """Serialized form of :class:`caliber.db.models.CaliberKnowledgeBaseSource`."""

    model_config = ConfigDict(from_attributes=True)

    knowledge_base_source_id: str
    knowledge_base_version_id: str
    document_id: str
    selection_kind: str
    bucket: str
    object_key: str
    object_name: str
    object_store_path: str
    content_type: str | None
    size_bytes: int
    etag: str | None
    last_modified: datetime | None
    extracted_chars: int
    extracted_format: str | None
    ocr_used: bool
    status: str
    error_summary: str | None
    source_metadata: dict[str, object] | None = None
    created_at: datetime


class KnowledgeBaseChunkSchema(BaseModel):
    """Serialized form of :class:`caliber.db.models.CaliberKnowledgeBaseChunk`."""

    model_config = ConfigDict(from_attributes=True)

    knowledge_base_chunk_id: str
    knowledge_base_version_id: str
    document_id: str
    source_bucket: str
    source_key: str
    source_name: str
    chunk_index: int
    ordinal: int
    content: str
    content_hash: str
    token_count: int
    char_count: int
    start_index: int | None
    end_index: int | None
    embedding: list[float] = Field(default_factory=list)
    chunk_metadata: dict[str, object] | None = None
    created_at: datetime


class KnowledgeBaseRunSchema(BaseModel):
    """Serialized form of :class:`caliber.db.models.CaliberKnowledgeBaseRun`."""

    model_config = ConfigDict(from_attributes=True)

    knowledge_base_run_id: str
    knowledge_base_id: str
    knowledge_base_version_id: str
    status: str
    source_manifest: list[dict[str, object]] = Field(default_factory=list)
    metrics: dict[str, object] | None
    error_summary: str | None
    log_line_count: int
    created_by: str
    created_at: datetime
    queued_at: datetime | None
    claimed_by: str | None
    claimed_at: datetime | None
    lease_expires_at: datetime | None
    last_heartbeat_at: datetime | None
    started_at: datetime | None
    completed_at: datetime | None


class KnowledgeBaseRunEventSchema(BaseModel):
    """Serialized form of :class:`caliber.db.models.CaliberKnowledgeBaseRunEvent`."""

    model_config = ConfigDict(from_attributes=True)

    event_id: int
    knowledge_base_run_id: str
    sequence: int
    event_type: str
    payload: dict[str, object] | None
    created_at: datetime


class KnowledgeBaseEntitySchema(BaseModel):
    """Serialized form of :class:`caliber.db.models.CaliberKnowledgeBaseEntity`."""

    model_config = ConfigDict(from_attributes=True)

    knowledge_base_entity_id: str
    knowledge_base_version_id: str
    entity_key: str
    label: str
    entity_type: str
    aliases: list[str] = Field(default_factory=list)
    mention_count: int
    source_documents: list[str] = Field(default_factory=list)
    source_keys: list[str] = Field(default_factory=list)
    source_chunks: list[str] = Field(default_factory=list)
    entity_metadata: dict[str, object] | None = None
    created_at: datetime


class KnowledgeBaseRelationshipSchema(BaseModel):
    """Serialized relationship plus denormalized endpoint labels."""

    knowledge_base_relationship_id: str
    knowledge_base_version_id: str
    source_entity_id: str
    source_entity_key: str
    source_entity_label: str
    target_entity_id: str
    target_entity_key: str
    target_entity_label: str
    relationship_type: str
    weight: float
    evidence_chunk_ids: list[str] = Field(default_factory=list)
    source_documents: list[str] = Field(default_factory=list)
    relationship_metadata: dict[str, object] = Field(default_factory=dict)
    created_at: datetime


class KnowledgeGraphEntityViewSchema(KnowledgeBaseEntitySchema):
    """Graph-explorer projection for one entity node."""

    distance: int | None = None
    highlighted: bool = False
    graph_source: KnowledgeGraphExploreSource = "local"


class KnowledgeGraphRelationshipViewSchema(KnowledgeBaseRelationshipSchema):
    """Graph-explorer projection for one relationship edge."""

    hop_distance: int | None = None
    graph_source: KnowledgeGraphExploreSource = "local"


class KnowledgeBaseBuildResultSchema(BaseModel):
    """Response body for knowledge-base build endpoints."""

    knowledge_base: KnowledgeBaseSchema
    version: KnowledgeBaseVersionSchema
    run: KnowledgeBaseRunSchema


class KnowledgeOptionSchema(BaseModel):
    """One selectable option shown in build/playground controls."""

    id: str
    name: str
    description: str
    defaults: dict[str, object] = Field(default_factory=dict)
    tags: list[str] = Field(default_factory=list)
    available: bool = True
    unavailable_reason: str | None = None
    requires_override: bool = False


class KnowledgeOptionsSchema(BaseModel):
    """Catalog of chunkers, embeddings, and future extension points."""

    chunking_strategies: list[KnowledgeOptionSchema] = Field(default_factory=list)
    embedding_models: list[KnowledgeOptionSchema] = Field(default_factory=list)
    retrieval_modes: list[KnowledgeOptionSchema] = Field(default_factory=list)
    graph_extractors: list[KnowledgeOptionSchema] = Field(default_factory=list)
    graph_output_targets: list[KnowledgeOptionSchema] = Field(default_factory=list)
    graph_retrieval_strengths: list[KnowledgeOptionSchema] = Field(default_factory=list)
    graph_age_seed_modes: list[KnowledgeOptionSchema] = Field(default_factory=list)
    graph_entity_types: list[KnowledgeOptionSchema] = Field(default_factory=list)
    graph_build_presets: list[KnowledgeGraphBuildPresetSchema] = Field(default_factory=list)
    graph_query_presets: list[KnowledgeGraphQueryPresetSchema] = Field(default_factory=list)
    default_graph_config: KnowledgeGraphConfigSchema = Field(
        default_factory=KnowledgeGraphConfigSchema
    )
    age_enabled: bool = False
    age_graph_name: str | None = None
    age_viewer_url: str | None = None
    age_unavailable_reason: str | None = None
    reserved_output_prefix: str


class KnowledgeGraphExploreResultSchema(BaseModel):
    """Response body for the KB graph explorer."""

    knowledge_base_version_id: str
    requested_source: KnowledgeGraphExploreSource = "local"
    served_source: KnowledgeGraphExploreSource = "local"
    age_enabled: bool = False
    age_ready: bool = False
    age_graph_name: str | None = None
    age_status: str | None = None
    query: str = ""
    entity_type: str | None = None
    query_entity_labels: list[str] = Field(default_factory=list)
    minimum_relationship_weight: float = 0.0
    traversal_hops: int = 0
    node_limit: int = 12
    age_seed_mode: KnowledgeAgeSeedMode | None = None
    strict_age_retrieval: bool = False
    age_seed_strategy: Literal["query_entities", "query_text", "query_entities_and_text"] | None = (
        None
    )
    matched_entity_labels: list[str] = Field(default_factory=list)
    expanded_entity_labels: list[str] = Field(default_factory=list)
    fallback_reason: str | None = None
    entities: list[KnowledgeGraphEntityViewSchema] = Field(default_factory=list)
    relationships: list[KnowledgeGraphRelationshipViewSchema] = Field(default_factory=list)


class KnowledgeQueryCitationSchema(BaseModel):
    """A source citation returned with a RAG answer."""

    chunk_id: str
    label: str
    source_bucket: str
    source_key: str
    object_store_path: str
    score: float


class KnowledgeQueryChunkSchema(BaseModel):
    """One retrieved chunk surfaced in the playground."""

    chunk_id: str
    source_bucket: str
    source_key: str
    source_name: str
    score: float
    content: str
    chunk_index: int
    ordinal: int
    document_id: str
    metadata: dict[str, object] = Field(default_factory=dict)
    object_store_path: str
    score_breakdown: dict[str, float] = Field(default_factory=dict)
    matched_entity_labels: list[str] = Field(default_factory=list)


class KnowledgeQueryVersionResultSchema(BaseModel):
    """Retrieval + answer result for one version inside a compare request."""

    knowledge_base_version_id: str
    knowledge_base_id: str
    version_number: int
    knowledge_base_name: str
    chunking_strategy: str
    embedding_model: str
    retrieval_mode: KnowledgeRetrievalMode = "dense"
    answer: str | None
    answer_error: str | None = None
    citations: list[KnowledgeQueryCitationSchema] = Field(default_factory=list)
    retrieved_chunks: list[KnowledgeQueryChunkSchema] = Field(default_factory=list)
    graph_context: dict[str, object] = Field(default_factory=dict)
    timing_ms: dict[str, float] = Field(default_factory=dict)


class KnowledgeQueryResultSchema(BaseModel):
    """Compare-ready RAG playground response."""

    question: str
    versions: list[KnowledgeQueryVersionResultSchema] = Field(default_factory=list)

    @model_validator(mode="after")
    def _require_versions(self) -> KnowledgeQueryResultSchema:
        if not self.versions:
            raise ValueError("query result must contain at least one version")
        return self


# ---------------------------------------------------------------------------
# Knowledge-base calibration (Phase K1) — durable retrieval-quality test runs.
# ---------------------------------------------------------------------------


class KnowledgeCalibrationRequest(BaseModel):
    """Body of ``POST /knowledge-bases/{id}/calibrate``.

    Scores ``version_id`` against the test questions in ``eval_dataset_id``.
    ``eval_dataset_version`` pins the exact example set (reconstructed "as of
    version N"); ``None`` uses the dataset's current active set. ``retrieval_mode``
    and ``top_k`` shape retrieval (defaults mirror the query playground).
    """

    model_config = ConfigDict(extra="forbid")

    version_id: str = Field(min_length=1, max_length=64)
    eval_dataset_id: str = Field(min_length=1, max_length=64)
    eval_dataset_version: int | None = Field(default=None, ge=1, le=2**31 - 1)
    retrieval_mode: KnowledgeRetrievalMode = "dense"
    top_k: int = Field(default=6, ge=1, le=20)


class KnowledgeCalibrationQuestionResult(BaseModel):
    """Per-question scored result inside a calibration run's ``results`` array."""

    question: str
    recall_at_k: float | None = None
    ndcg_at_k: float | None = None
    faithfulness: float | None = None
    answer_correctness: float | None = None
    verdict: str
    score: float | None = None
    answer: str | None = None
    answer_error: str | None = None
    gold_sources: list[str] = Field(default_factory=list)
    retrieved_sources: list[str] = Field(default_factory=list)


class KnowledgeCalibrationRunSummary(BaseModel):
    """History-list row for a persisted calibration run (no per-question array)."""

    model_config = ConfigDict(from_attributes=True)

    test_run_id: str
    knowledge_base_id: str
    knowledge_base_version_id: str
    eval_dataset_id: str | None
    eval_dataset_version: int | None
    retrieval_mode: str
    top_k: int
    test_set_size: int
    metrics: dict[str, object] = Field(default_factory=dict)
    created_by: str
    status: str
    created_at: datetime
    completed_at: datetime | None


class KnowledgeCalibrationRunDetail(KnowledgeCalibrationRunSummary):
    """Full calibration run including the per-question ``results`` array."""

    results: list[KnowledgeCalibrationQuestionResult] = Field(default_factory=list)


class KnowledgeBaselineRequest(BaseModel):
    """Body of ``POST /knowledge-bases/{id}/baseline``."""

    model_config = ConfigDict(extra="forbid")

    test_run_id: str = Field(min_length=1, max_length=64)


class KnowledgeBaselineResponse(BaseModel):
    """Result of pinning a calibration run as a KB's comparison baseline."""

    knowledge_base_id: str
    baseline_run_id: str | None
