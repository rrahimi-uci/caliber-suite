export type KnowledgeRetrievalMode = "dense" | "hybrid" | "graph_hybrid" | "age_graph";
export type KnowledgeGraphExtractorBackend = "heuristic" | "spacy";
export type KnowledgeGraphOutputTarget =
  | "object_store"
  | "object_store_and_age";
export type KnowledgeGraphRetrievalStrength =
  | "conservative"
  | "balanced"
  | "aggressive";
export type KnowledgeAgeSeedMode =
  | "entity_then_text"
  | "query_entities_only"
  | "query_text_only"
  | "query_entities_and_text";
export type KnowledgeGraphExploreSource = "local" | "age";
export type KnowledgeGraphBuildPresetId =
  | "portable"
  | "balanced"
  | "age_native"
  | "age_strict";
export type KnowledgeGraphQueryPresetId =
  | "hybrid_precision"
  | "hybrid_balanced"
  | "age_balanced"
  | "age_native"
  | "age_strict";

export interface KnowledgeGraphConfig {
  extractor_backend: KnowledgeGraphExtractorBackend;
  spacy_model: string | null;
  max_entities_per_chunk: number;
  entity_types: string[];
  minimum_entity_mentions: number;
  minimum_relationship_weight: number;
  default_retrieval_mode: KnowledgeRetrievalMode;
  retrieval_strength: KnowledgeGraphRetrievalStrength;
  output_target: KnowledgeGraphOutputTarget;
  age_seed_mode: KnowledgeAgeSeedMode;
  age_traversal_hops: number;
  age_candidate_pool_size: number;
  age_dense_rerank_weight: number;
  strict_age_retrieval_default?: boolean;
}

export interface KnowledgeGraphBuildPreset {
  id: KnowledgeGraphBuildPresetId;
  label: string;
  eyebrow: string;
  description: string;
  badges: string[];
  patch: Partial<KnowledgeGraphConfig>;
  recommended: boolean;
  age_required: boolean;
}

export interface KnowledgeGraphQueryPreset {
  id: KnowledgeGraphQueryPresetId;
  label: string;
  eyebrow: string;
  description: string;
  badges: string[];
  retrieval_mode: KnowledgeRetrievalMode;
  patch: KnowledgeQueryGraphOverrides;
  recommended: boolean;
  age_required: boolean;
}

export interface KnowledgeQueryGraphOverrides {
  retrieval_strength?: KnowledgeGraphRetrievalStrength;
  minimum_relationship_weight?: number;
  age_seed_mode?: KnowledgeAgeSeedMode;
  age_traversal_hops?: number;
  age_candidate_pool_size?: number;
  age_dense_rerank_weight?: number;
  strict_age_retrieval?: boolean;
}

export interface KnowledgeSourceSelection {
  kind: "file" | "folder";
  path: string;
}

export interface KnowledgeBase {
  knowledge_base_id: string;
  project_id: string | null;
  visibility: string;
  name: string;
  description: string;
  owner: string;
  status: string;
  source_bucket: string;
  source_manifest: Array<Record<string, unknown>>;
  source_fingerprint: string;
  active_version_id: string | null;
  baseline_run_id?: string | null;
  last_run_id: string | null;
  last_run_status: string | null;
  last_run_completed_at: string | null;
  created_at: string;
  updated_at: string;
  active_version_summary?: KnowledgeBaseActiveVersionSummary | null;
}

export interface KnowledgeBaseActiveVersionSummary {
  knowledge_base_version_id: string;
  version_number: number;
  status: string;
  chunking_strategy: string;
  embedding_model: string;
  graph_extractor: KnowledgeGraphExtractorBackend;
  graph_target: KnowledgeGraphOutputTarget;
  default_retrieval_mode: KnowledgeRetrievalMode;
  retrieval_strength: KnowledgeGraphRetrievalStrength;
  graph_profile_id?: string | null;
  graph_profile_label?: string | null;
  age_sync_status: string | null;
  age_ready: boolean;
  age_graph_name: string | null;
  chunk_count: number;
  entity_count: number;
  relationship_count: number;
  created_at: string;
  completed_at: string | null;
}

export interface KnowledgeBaseVersion {
  knowledge_base_version_id: string;
  knowledge_base_id: string;
  version_number: number;
  status: string;
  chunking_strategy: string;
  chunking_config: Record<string, unknown>;
  graph_config: KnowledgeGraphConfig;
  graph_profile_id?: string | null;
  graph_profile_label?: string | null;
  embedding_provider: string;
  embedding_model: string;
  embedding_dimension: number | null;
  source_manifest: Array<Record<string, unknown>>;
  source_fingerprint: string;
  output_bucket: string;
  output_prefix: string;
  chunks_uri: string | null;
  entities_uri: string | null;
  relationships_uri: string | null;
  graph_uri: string | null;
  manifest_uri: string | null;
  logs_uri: string | null;
  stats_uri: string | null;
  summary: Record<string, unknown> | null;
  error_summary: string | null;
  created_by: string;
  created_at: string;
  completed_at: string | null;
}

export interface KnowledgeBaseSource {
  knowledge_base_source_id: string;
  knowledge_base_version_id: string;
  document_id: string;
  selection_kind: string;
  bucket: string;
  object_key: string;
  object_name: string;
  object_store_path: string;
  content_type: string | null;
  size_bytes: number;
  etag: string | null;
  last_modified: string | null;
  extracted_chars: number;
  extracted_format: string | null;
  ocr_used: boolean;
  status: string;
  error_summary: string | null;
  source_metadata: Record<string, unknown> | null;
  created_at: string;
}

export interface KnowledgeBaseChunk {
  knowledge_base_chunk_id: string;
  knowledge_base_version_id: string;
  document_id: string;
  source_bucket: string;
  source_key: string;
  source_name: string;
  chunk_index: number;
  ordinal: number;
  content: string;
  content_hash: string;
  token_count: number;
  char_count: number;
  start_index: number | null;
  end_index: number | null;
  embedding: number[];
  chunk_metadata: Record<string, unknown> | null;
  created_at: string;
}

export interface KnowledgeBaseRun {
  knowledge_base_run_id: string;
  knowledge_base_id: string;
  knowledge_base_version_id: string;
  status: string;
  source_manifest: Array<Record<string, unknown>>;
  metrics: Record<string, unknown> | null;
  error_summary: string | null;
  log_line_count: number;
  created_by: string;
  created_at: string;
  queued_at: string | null;
  claimed_by: string | null;
  claimed_at: string | null;
  lease_expires_at: string | null;
  last_heartbeat_at: string | null;
  started_at: string | null;
  completed_at: string | null;
}

export interface KnowledgeBaseRunEvent {
  event_id: number;
  knowledge_base_run_id: string;
  sequence: number;
  event_type: string;
  payload: Record<string, unknown> | null;
  created_at: string;
}

export interface KnowledgeBaseEntity {
  knowledge_base_entity_id: string;
  knowledge_base_version_id: string;
  entity_key: string;
  label: string;
  entity_type: string;
  aliases: string[];
  mention_count: number;
  source_documents: string[];
  source_keys: string[];
  source_chunks: string[];
  entity_metadata: Record<string, unknown> | null;
  created_at: string;
}

export interface KnowledgeBaseRelationship {
  knowledge_base_relationship_id: string;
  knowledge_base_version_id: string;
  source_entity_id: string;
  source_entity_key: string;
  source_entity_label: string;
  target_entity_id: string;
  target_entity_key: string;
  target_entity_label: string;
  relationship_type: string;
  weight: number;
  evidence_chunk_ids: string[];
  source_documents: string[];
  relationship_metadata: Record<string, unknown>;
  created_at: string;
}

export interface KnowledgeGraphEntityView extends KnowledgeBaseEntity {
  distance: number | null;
  highlighted: boolean;
  graph_source: KnowledgeGraphExploreSource;
}

export interface KnowledgeGraphRelationshipView extends KnowledgeBaseRelationship {
  hop_distance: number | null;
  graph_source: KnowledgeGraphExploreSource;
}

export interface KnowledgeBaseBuildResult {
  knowledge_base: KnowledgeBase;
  version: KnowledgeBaseVersion;
  run: KnowledgeBaseRun;
}

export interface KnowledgeOption {
  id: string;
  name: string;
  description: string;
  defaults: Record<string, unknown>;
  tags: string[];
  available?: boolean;
  unavailable_reason?: string | null;
  requires_override?: boolean;
}

export interface KnowledgeOptions {
  chunking_strategies: KnowledgeOption[];
  embedding_models: KnowledgeOption[];
  retrieval_modes: KnowledgeOption[];
  graph_extractors: KnowledgeOption[];
  graph_output_targets: KnowledgeOption[];
  graph_retrieval_strengths: KnowledgeOption[];
  graph_age_seed_modes?: KnowledgeOption[];
  graph_entity_types: KnowledgeOption[];
  graph_build_presets?: KnowledgeGraphBuildPreset[];
  graph_query_presets?: KnowledgeGraphQueryPreset[];
  default_graph_config: KnowledgeGraphConfig;
  age_enabled: boolean;
  age_graph_name: string | null;
  age_viewer_url: string | null;
  age_unavailable_reason: string | null;
  reserved_output_prefix: string;
}

export interface KnowledgeGraphExploreResult {
  knowledge_base_version_id: string;
  requested_source: KnowledgeGraphExploreSource;
  served_source: KnowledgeGraphExploreSource;
  age_enabled: boolean;
  age_ready: boolean;
  age_graph_name: string | null;
  age_status: string | null;
  query: string;
  entity_type: string | null;
  query_entity_labels: string[];
  minimum_relationship_weight: number;
  traversal_hops: number;
  node_limit: number;
  age_seed_mode?: KnowledgeAgeSeedMode | null;
  strict_age_retrieval: boolean;
  age_seed_strategy?:
    | "query_entities"
    | "query_text"
    | "query_entities_and_text"
    | null;
  matched_entity_labels: string[];
  expanded_entity_labels: string[];
  fallback_reason: string | null;
  entities: KnowledgeGraphEntityView[];
  relationships: KnowledgeGraphRelationshipView[];
}


export interface KnowledgeQueryCitation {
  chunk_id: string;
  label: string;
  source_bucket: string;
  source_key: string;
  object_store_path: string;
  score: number;
}

export interface KnowledgeQueryChunk {
  chunk_id: string;
  source_bucket: string;
  source_key: string;
  source_name: string;
  score: number;
  content: string;
  chunk_index: number;
  ordinal: number;
  document_id: string;
  metadata: Record<string, unknown>;
  object_store_path: string;
  score_breakdown: Record<string, number>;
  matched_entity_labels: string[];
}

export interface KnowledgeQueryGraphContext {
  requested_backend?: string;
  applied_backend?: string;
  spacy_model?: string | null;
  fallback_reason?: string | null;
  query_entities?: string[];
  matched_entities?: string[];
  expanded_entities?: string[];
  boosted_chunk_count?: number;
  relationship_count?: number;
  age_graph_name?: string | null;
  age_status?: "ok" | "fallback";
  age_fallback_reason?: string | null;
  age_traversal_hops?: number;
  age_matched_chunk_count?: number;
  age_configured_seed_mode?: KnowledgeAgeSeedMode;
  age_configured_hops?: number;
  age_candidate_pool_size?: number;
  age_dense_rerank_weight?: number;
  age_seed_strategy?:
    | "query_entities"
    | "query_text"
    | "query_entities_and_text";
  minimum_relationship_weight?: number;
  age_ready?: boolean;
  age_requested?: boolean;
  fallback_retrieval_mode?: KnowledgeRetrievalMode;
  retrieval_strength?: KnowledgeGraphRetrievalStrength;
  query_override_active?: boolean;
  query_overrides?: KnowledgeQueryGraphOverrides;
  strict_age_retrieval?: boolean;
}

export interface KnowledgeQueryVersionResult {
  knowledge_base_version_id: string;
  knowledge_base_id: string;
  version_number: number;
  knowledge_base_name: string;
  chunking_strategy: string;
  embedding_model: string;
  retrieval_mode: KnowledgeRetrievalMode;
  answer: string | null;
  answer_error: string | null;
  citations: KnowledgeQueryCitation[];
  retrieved_chunks: KnowledgeQueryChunk[];
  graph_context: KnowledgeQueryGraphContext;
  timing_ms: Record<string, number>;
}

export interface KnowledgeQueryResult {
  question: string;
  versions: KnowledgeQueryVersionResult[];
}

// ---------------------------------------------------------------------------
// Knowledge-base calibration (Phase K2) — retrieval-quality test runs. Mirrors
// caliber/src/caliber/knowledge/schemas.py (KnowledgeCalibration* schemas).
// ---------------------------------------------------------------------------

/** Body of POST /knowledge-bases/{id}/calibrate. */
export interface KnowledgeCalibrationRequest {
  version_id: string;
  eval_dataset_id: string;
  eval_dataset_version?: number | null;
  retrieval_mode?: KnowledgeRetrievalMode;
  top_k?: number;
}

/** Per-question scored result inside a calibration run's `results` array. */
export interface KnowledgeCalibrationQuestionResult {
  question: string;
  recall_at_k?: number | null;
  ndcg_at_k?: number | null;
  faithfulness?: number | null;
  answer_correctness?: number | null;
  verdict: string;
  score?: number | null;
  answer?: string | null;
  answer_error?: string | null;
  gold_sources: string[];
  retrieved_sources: string[];
}

/** Aggregate metrics for a calibration run (null/absent when undefined). */
export interface KnowledgeCalibrationMetrics {
  recall_at_k?: number | null;
  ndcg_at_k?: number | null;
  faithfulness?: number | null;
  answer_correctness?: number | null;
  passed_count?: number;
  partial_count?: number;
  failed_count?: number;
  [key: string]: unknown;
}

/** History-list row for a persisted calibration run (no per-question array). */
export interface KnowledgeCalibrationRunSummary {
  test_run_id: string;
  knowledge_base_id: string;
  knowledge_base_version_id: string;
  eval_dataset_id: string | null;
  eval_dataset_version: number | null;
  retrieval_mode: string;
  top_k: number;
  test_set_size: number;
  metrics: KnowledgeCalibrationMetrics;
  created_by: string;
  status: string;
  created_at: string;
  completed_at: string | null;
}

/** Full calibration run including the per-question `results` array. */
export interface KnowledgeCalibrationRunDetail
  extends KnowledgeCalibrationRunSummary {
  results: KnowledgeCalibrationQuestionResult[];
}

/** Result of pinning a calibration run as a KB's comparison baseline. */
export interface KnowledgeBaselineResponse {
  knowledge_base_id: string;
  baseline_run_id: string | null;
}
