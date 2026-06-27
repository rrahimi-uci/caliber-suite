import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { http, HttpResponse } from "msw";
import { fireEvent, render as rtlRender } from "@testing-library/react";
import { afterAll, afterEach, beforeAll, describe, expect, it } from "vitest";
import { MemoryRouter, Route, Routes } from "react-router-dom";

import { KnowledgeBases } from "@/pages/KnowledgeBases";
import { render, screen, userEvent, waitFor, within } from "@/test/utils";
import { server } from "@/test/server";

const API_BASE = "/ajax-api/2.0/mlflow/caliber";
const KB = `${API_BASE}/knowledge-bases`;
const QUERY = `${API_BASE}/knowledge/query`;
const OS = `${API_BASE}/object-store`;
const NOW = "2026-06-12T18:00:00Z";

function envelope<T>(data: T): { data: T } {
  return { data };
}

function knowledgeOptions(
  ageEnabled = true,
  ageViewerUrl: string | null = ageEnabled ? "http://127.0.0.1:8082" : null,
  embeddingUnavailableReason: string | null = null,
) {
  return {
    chunking_strategies: [
      {
        id: "recursive",
        name: "Recursive character",
        description: "Balanced default for mixed document types.",
        defaults: {
          chunk_size: 1200,
          chunk_overlap: 180,
          semantic_similarity_threshold: 0.78,
        },
        tags: ["default"],
      },
    ],
    embedding_models: [
      {
        id: "sentence-transformers/all-MiniLM-L6-v2",
        name: "MiniLM L6 v2",
        description: "Fast local embedding model for interactive builds.",
        defaults: {},
        tags: ["fast"],
        available: embeddingUnavailableReason == null,
        unavailable_reason: embeddingUnavailableReason,
        requires_override: embeddingUnavailableReason != null,
      },
    ],
    retrieval_modes: [
      {
        id: "dense",
        name: "Dense chunks",
        description: "Pure embedding similarity over stored chunks.",
        defaults: {},
        tags: ["default"],
      },
      {
        id: "graph_hybrid",
        name: "GraphRAG hybrid",
        description: "Dense retrieval plus local graph-aware expansion.",
        defaults: {},
        tags: ["graph"],
      },
      ...(ageEnabled
        ? [
            {
              id: "age_graph",
              name: "Apache AGE graph",
              description: "Cypher-first retrieval over the synced AGE graph.",
              defaults: {},
              tags: ["apache-age"],
            },
          ]
        : []),
    ],
    graph_extractors: [
      {
        id: "heuristic",
        name: "Heuristic",
        description: "Dependency-light entity extraction.",
        defaults: {},
        tags: ["default"],
      },
      {
        id: "spacy",
        name: "spaCy named entities",
        description: "Higher-recall NLP extraction.",
        defaults: {},
        tags: ["nlp"],
      },
    ],
    graph_output_targets: [
      {
        id: "object_store",
        name: "Object store artifacts only",
        description: "Persist graph artifacts without external graph sync.",
        defaults: {},
        tags: ["default"],
      },
      ...(ageEnabled
        ? [
            {
              id: "object_store_and_age",
              name: "Object store + Apache AGE",
              description: "Persist artifacts and sync the build into AGE.",
              defaults: {},
              tags: ["apache-age"],
            },
          ]
        : []),
    ],
    graph_retrieval_strengths: [
      {
        id: "conservative",
        name: "Conservative",
        description: "Tight graph traversal for precision-first retrieval.",
        defaults: {},
        tags: [],
      },
      {
        id: "balanced",
        name: "Balanced",
        description: "Recommended graph traversal balance.",
        defaults: {},
        tags: ["recommended"],
      },
      {
        id: "aggressive",
        name: "Aggressive",
        description: "Deeper graph expansion for graph-first retrieval.",
        defaults: {},
        tags: ["high-recall"],
      },
    ],
    graph_age_seed_modes: [
      {
        id: "entity_then_text",
        name: "Entity first, then question text",
        description:
          "Try extracted entities first, then fall back to the raw question text.",
        defaults: {},
        tags: ["default"],
      },
      {
        id: "query_entities_only",
        name: "Extracted entities only",
        description:
          "Require extracted entity matches before AGE traversal begins.",
        defaults: {},
        tags: ["precise"],
      },
      {
        id: "query_text_only",
        name: "Question text only",
        description: "Seed AGE directly from the question text.",
        defaults: {},
        tags: ["text-first"],
      },
      {
        id: "query_entities_and_text",
        name: "Entities plus question text",
        description:
          "Combine extracted entities with text matches for broader recall.",
        defaults: {},
        tags: ["high-recall"],
      },
    ],
    graph_entity_types: [
      {
        id: "person",
        name: "Person",
        description: "People and owners mentioned in the corpus.",
        defaults: {},
        tags: [],
      },
      {
        id: "document",
        name: "Document",
        description: "Named documents and runbooks.",
        defaults: {},
        tags: [],
      },
    ],
    graph_query_presets: [
      {
        id: "hybrid_precision",
        label: "Precise GraphRAG",
        eyebrow: "Precision",
        description:
          "Tight local graph expansion around direct entity matches.",
        badges: ["Local graph", "0-hop", "Conservative"],
        retrieval_mode: "graph_hybrid",
        patch: {
          retrieval_strength: "conservative",
          minimum_relationship_weight: 2,
          age_traversal_hops: 0,
        },
        recommended: false,
        age_required: false,
      },
      {
        id: "hybrid_balanced",
        label: "Balanced GraphRAG",
        eyebrow: ageEnabled ? "Portable" : "Recommended",
        description: "Balanced local GraphRAG profile.",
        badges: ["Local graph", "1-hop", "Balanced"],
        retrieval_mode: "graph_hybrid",
        patch: {
          retrieval_strength: "balanced",
          minimum_relationship_weight: 1,
          age_traversal_hops: 1,
        },
        recommended: !ageEnabled,
        age_required: false,
      },
      ...(ageEnabled
        ? [
            {
              id: "age_balanced",
              label: "Balanced AGE",
              eyebrow: "Recommended",
              description: "Balanced AGE retrieval profile.",
              badges: ["AGE primary", "1-hop", "Balanced rerank"],
              retrieval_mode: "age_graph",
              patch: {
                retrieval_strength: "balanced",
                minimum_relationship_weight: 1,
                age_seed_mode: "entity_then_text",
                age_traversal_hops: 1,
                age_candidate_pool_size: 24,
                age_dense_rerank_weight: 0.35,
              },
              recommended: true,
              age_required: true,
            },
            {
              id: "age_native",
              label: "AGE-native retrieval",
              eyebrow: "Graph-first",
              description: "Graph-first AGE retrieval profile.",
              badges: ["AGE primary", "2-hop", "Graph-first"],
              retrieval_mode: "age_graph",
              patch: {
                retrieval_strength: "aggressive",
                minimum_relationship_weight: 1,
                age_seed_mode: "query_entities_and_text",
                age_traversal_hops: 2,
                age_candidate_pool_size: 40,
                age_dense_rerank_weight: 0.2,
              },
              recommended: false,
              age_required: true,
            },
            {
              id: "age_strict",
              label: "Strict AGE only",
              eyebrow: "Locked",
              description: "AGE-only retrieval profile with no fallback.",
              badges: ["AGE primary", "Strict", "No fallback"],
              retrieval_mode: "age_graph",
              patch: {
                retrieval_strength: "aggressive",
                minimum_relationship_weight: 1,
                age_seed_mode: "query_entities_and_text",
                age_traversal_hops: 2,
                age_candidate_pool_size: 40,
                age_dense_rerank_weight: 0.2,
                strict_age_retrieval: true,
              },
              recommended: false,
              age_required: true,
            },
          ]
        : []),
    ],
    default_graph_config: {
      extractor_backend: "heuristic",
      spacy_model: null,
      max_entities_per_chunk: 12,
      entity_types: [],
      minimum_entity_mentions: 1,
      minimum_relationship_weight: 1,
      default_retrieval_mode: ageEnabled ? "age_graph" : "graph_hybrid",
      retrieval_strength: "balanced",
      output_target: ageEnabled ? "object_store_and_age" : "object_store",
      age_seed_mode: "entity_then_text",
      age_traversal_hops: 1,
      age_candidate_pool_size: 24,
      age_dense_rerank_weight: 0.35,
      strict_age_retrieval_default: false,
    },
    age_enabled: ageEnabled,
    age_graph_name: ageEnabled ? "knowledge_graph" : null,
    age_viewer_url: ageViewerUrl,
    age_unavailable_reason: ageEnabled
      ? null
      : "Apache AGE requires the PostgreSQL+AGE stack. In local dev, start the suite with ./start.sh or point CALIBER_DATABASE_URL at a PostgreSQL database with AGE enabled.",
    reserved_output_prefix: ".caliber/knowledge-bases",
  };
}

function knowledgeBase() {
  return {
    knowledge_base_id: "KB-1",
    project_id: null,
    visibility: "user",
    name: "Operations Corpus",
    description: "Runbooks and incident procedures",
    owner: "@ops",
    status: "active",
    source_bucket: "reports",
    source_manifest: [{ kind: "folder", path: "docs/" }],
    source_fingerprint: "fingerprint-1",
    active_version_id: "KBV-1",
    last_run_id: "KBR-1",
    last_run_status: "completed",
    last_run_completed_at: NOW,
    created_at: NOW,
    updated_at: NOW,
    active_version_summary: {
      knowledge_base_version_id: "KBV-1",
      version_number: 1,
      status: "completed",
      chunking_strategy: "recursive",
      embedding_model: "sentence-transformers/all-MiniLM-L6-v2",
      graph_extractor: "heuristic",
      graph_target: "object_store_and_age",
      default_retrieval_mode: "age_graph",
      retrieval_strength: "balanced",
      graph_profile_id: "balanced",
      graph_profile_label: "Balanced GraphRAG + AGE",
      age_sync_status: "synced",
      age_ready: true,
      age_graph_name: "knowledge_graph",
      chunk_count: 3,
      entity_count: 2,
      relationship_count: 1,
      created_at: NOW,
      completed_at: NOW,
    },
  };
}

function version() {
  return {
    knowledge_base_version_id: "KBV-1",
    knowledge_base_id: "KB-1",
    version_number: 1,
    status: "completed",
    chunking_strategy: "recursive",
    chunking_config: {
      chunk_size: 1200,
      chunk_overlap: 180,
      semantic_similarity_threshold: 0.78,
    },
    graph_config: {
      extractor_backend: "heuristic",
      spacy_model: null,
      max_entities_per_chunk: 12,
      entity_types: ["person"],
      minimum_entity_mentions: 1,
      minimum_relationship_weight: 1,
      default_retrieval_mode: "age_graph",
      retrieval_strength: "balanced",
      output_target: "object_store_and_age",
      age_seed_mode: "entity_then_text",
      age_traversal_hops: 1,
      age_candidate_pool_size: 24,
      age_dense_rerank_weight: 0.35,
      strict_age_retrieval_default: false,
    },
    graph_profile_id: "balanced",
    graph_profile_label: "Balanced GraphRAG + AGE",
    embedding_provider: "huggingface",
    embedding_model: "sentence-transformers/all-MiniLM-L6-v2",
    embedding_dimension: 384,
    source_manifest: [{ kind: "folder", path: "docs/" }],
    source_fingerprint: "fingerprint-1",
    output_bucket: "reports",
    output_prefix: ".caliber/knowledge-bases/KB-1/v1",
    chunks_uri: "s3://reports/.caliber/knowledge-bases/KB-1/v1/chunks.jsonl",
    entities_uri:
      "s3://reports/.caliber/knowledge-bases/KB-1/v1/entities.jsonl",
    relationships_uri:
      "s3://reports/.caliber/knowledge-bases/KB-1/v1/relationships.jsonl",
    graph_uri: "s3://reports/.caliber/knowledge-bases/KB-1/v1/graph.json",
    manifest_uri: "s3://reports/.caliber/knowledge-bases/KB-1/v1/manifest.json",
    logs_uri: "s3://reports/.caliber/knowledge-bases/KB-1/v1/logs.jsonl",
    stats_uri: "s3://reports/.caliber/knowledge-bases/KB-1/v1/stats.json",
    summary: {
      chunk_count: 3,
      processed_sources: 1,
      entity_count: 2,
      relationship_count: 1,
      graph_profile_id: "balanced",
      graph_profile_label: "Balanced GraphRAG + AGE",
      age_sync_status: "synced",
      age_graph_name: "knowledge_graph",
      age_synced_nodes: 6,
      age_synced_edges: 5,
    },
    error_summary: null,
    created_by: "@ops",
    created_at: NOW,
    completed_at: NOW,
  };
}

function run() {
  return {
    knowledge_base_run_id: "KBR-1",
    knowledge_base_id: "KB-1",
    knowledge_base_version_id: "KBV-1",
    status: "completed",
    source_manifest: [{ kind: "folder", path: "docs/" }],
    metrics: { chunk_count: 3, entity_count: 2 },
    error_summary: null,
    log_line_count: 8,
    created_by: "@ops",
    created_at: NOW,
    queued_at: NOW,
    claimed_by: null,
    claimed_at: NOW,
    lease_expires_at: null,
    last_heartbeat_at: NOW,
    started_at: NOW,
    completed_at: NOW,
  };
}

// ── Calibration (Phase K2) fixtures ──────────────────────────────────────
function evalDataset() {
  return {
    dataset_id: "DS-1",
    name: "Ops retrieval questions",
    description: "Gold questions for the operations corpus",
    owner: "@ops",
    tags: ["retrieval"],
    status: "active",
    version: 2,
    created_at: NOW,
    updated_at: NOW,
  };
}

function calibrationSummary(overrides: Record<string, unknown> = {}) {
  return {
    test_run_id: "KBT-1",
    knowledge_base_id: "KB-1",
    knowledge_base_version_id: "KBV-1",
    eval_dataset_id: "DS-1",
    eval_dataset_version: 2,
    retrieval_mode: "dense",
    top_k: 6,
    test_set_size: 2,
    metrics: {
      recall_at_k: 0.75,
      ndcg_at_k: 0.6,
      faithfulness: 0.9,
      answer_correctness: 0.8,
      passed_count: 1,
      partial_count: 1,
      failed_count: 0,
    },
    created_by: "@ops",
    status: "completed",
    created_at: NOW,
    completed_at: NOW,
    ...overrides,
  };
}

function calibrationDetail(overrides: Record<string, unknown> = {}) {
  return {
    ...calibrationSummary(),
    results: [
      {
        question: "How do we restart the ingestion worker?",
        recall_at_k: 1,
        ndcg_at_k: 0.9,
        faithfulness: 1,
        answer_correctness: 1,
        verdict: "pass",
        score: 1,
        answer: "Run scripts/restart_worker.sh.",
        answer_error: null,
        gold_sources: ["docs/runbook.md"],
        retrieved_sources: ["docs/runbook.md"],
      },
      {
        question: "What is the on-call escalation path?",
        recall_at_k: 0.5,
        ndcg_at_k: 0.3,
        faithfulness: 0.8,
        answer_correctness: 0.6,
        verdict: "partial",
        score: 0.6,
        answer: "Page the secondary on-call.",
        answer_error: null,
        gold_sources: ["docs/oncall.md"],
        retrieved_sources: ["docs/general.md"],
      },
    ],
    ...overrides,
  };
}

/** Baseline KBT-0 with a higher second-question score so KBT-1 regresses it. */
function baselineDetail() {
  return calibrationDetail({
    test_run_id: "KBT-0",
    metrics: {
      recall_at_k: 0.85,
      ndcg_at_k: 0.7,
      faithfulness: 0.95,
      answer_correctness: 0.9,
      passed_count: 2,
      partial_count: 0,
      failed_count: 0,
    },
    results: [
      {
        question: "How do we restart the ingestion worker?",
        recall_at_k: 1,
        ndcg_at_k: 0.9,
        faithfulness: 1,
        answer_correctness: 1,
        verdict: "pass",
        score: 1,
        answer: "Run scripts/restart_worker.sh.",
        answer_error: null,
        gold_sources: ["docs/runbook.md"],
        retrieved_sources: ["docs/runbook.md"],
      },
      {
        question: "What is the on-call escalation path?",
        recall_at_k: 1,
        ndcg_at_k: 0.9,
        faithfulness: 1,
        answer_correctness: 0.95,
        verdict: "pass",
        score: 0.95,
        answer: "Page the secondary on-call.",
        answer_error: null,
        gold_sources: ["docs/oncall.md"],
        retrieved_sources: ["docs/oncall.md"],
      },
    ],
  });
}

/** Shared handlers so a KB is selectable and its detail/version tabs load. */
function calibrateBaseHandlers(bases: unknown[], versions: unknown[]) {
  return [
    http.get(`${KB}/options`, () =>
      HttpResponse.json(envelope(knowledgeOptions(true))),
    ),
    http.get(`${KB}`, () => HttpResponse.json(envelope(bases))),
    http.get(`${KB}/KB-1`, () =>
      HttpResponse.json(envelope(bases[0])),
    ),
    http.get(`${OS}/buckets`, () =>
      HttpResponse.json(envelope([{ name: "reports", creation_date: NOW }])),
    ),
    http.get(`${OS}/buckets/reports/objects`, () =>
      HttpResponse.json(envelope(objectListing())),
    ),
    http.get(`${KB}/KB-1/versions`, () =>
      HttpResponse.json(envelope(versions)),
    ),
    http.get(`${KB}/KB-1/runs`, () => HttpResponse.json(envelope([]))),
    http.get(`${API_BASE}/eval-datasets`, () =>
      HttpResponse.json(envelope([evalDataset()])),
    ),
    http.get(`${API_BASE}/knowledge-base-versions/KBV-1/sources`, () =>
      HttpResponse.json(envelope([])),
    ),
    http.get(`${API_BASE}/knowledge-base-versions/KBV-1/chunks`, () =>
      HttpResponse.json(envelope([])),
    ),
    http.get(`${API_BASE}/knowledge-base-versions/KBV-1/entities`, () =>
      HttpResponse.json(envelope([])),
    ),
    http.get(`${API_BASE}/knowledge-base-versions/KBV-1/relationships`, () =>
      HttpResponse.json(envelope([])),
    ),
  ];
}

function graphExplorer(source: "local" | "age" = "local", query = "") {
  const graphSource = source;
  return {
    knowledge_base_version_id: "KBV-1",
    requested_source: graphSource,
    served_source: graphSource,
    age_enabled: true,
    age_ready: true,
    age_graph_name: "knowledge_graph",
    age_status: "synced",
    query,
    entity_type: null,
    query_entity_labels: query ? ["Bob"] : [],
    minimum_relationship_weight: 1,
    traversal_hops: source === "age" ? 2 : 1,
    node_limit: 12,
    age_seed_mode: source === "age" ? "entity_then_text" : null,
    strict_age_retrieval: false,
    age_seed_strategy: source === "age" ? "query_entities" : null,
    matched_entity_labels: ["Bob"],
    expanded_entity_labels: ["Platform reliability", "Alice"],
    fallback_reason: null,
    entities: [
      {
        knowledge_base_entity_id: "ENT-1",
        knowledge_base_version_id: "KBV-1",
        entity_key: "bob",
        label: "Bob",
        entity_type: "person",
        aliases: ["Bob"],
        mention_count: 4,
        source_documents: ["DOC-1"],
        source_keys: ["incident-playbook.md"],
        source_chunks: ["CH-1"],
        entity_metadata: {},
        created_at: NOW,
        distance: 0,
        highlighted: true,
        graph_source: graphSource,
      },
      {
        knowledge_base_entity_id: "ENT-2",
        knowledge_base_version_id: "KBV-1",
        entity_key: "platform-reliability",
        label: "Platform reliability",
        entity_type: "concept",
        aliases: ["Platform reliability"],
        mention_count: 3,
        source_documents: ["DOC-1"],
        source_keys: ["incident-playbook.md"],
        source_chunks: source === "local" ? ["CH-1"] : [],
        entity_metadata: {},
        created_at: NOW,
        distance: 1,
        highlighted: false,
        graph_source: graphSource,
      },
      {
        knowledge_base_entity_id: "ENT-3",
        knowledge_base_version_id: "KBV-1",
        entity_key: "alice",
        label: "Alice",
        entity_type: "person",
        aliases: ["Alice"],
        mention_count: 2,
        source_documents: ["DOC-1"],
        source_keys: ["incident-playbook.md"],
        source_chunks: source === "local" ? ["CH-1"] : [],
        entity_metadata: {},
        created_at: NOW,
        distance: 1,
        highlighted: false,
        graph_source: graphSource,
      },
    ],
    relationships: [
      {
        knowledge_base_relationship_id: "REL-1",
        knowledge_base_version_id: "KBV-1",
        source_entity_id: "ENT-1",
        source_entity_key: "bob",
        source_entity_label: "Bob",
        target_entity_id: "ENT-2",
        target_entity_key: "platform-reliability",
        target_entity_label: "Platform reliability",
        relationship_type: "co_occurs",
        weight: 4,
        evidence_chunk_ids: ["CH-1"],
        source_documents: ["DOC-1"],
        relationship_metadata: {},
        created_at: NOW,
        hop_distance: 1,
        graph_source: graphSource,
      },
      {
        knowledge_base_relationship_id: "REL-2",
        knowledge_base_version_id: "KBV-1",
        source_entity_id: "ENT-3",
        source_entity_key: "alice",
        source_entity_label: "Alice",
        target_entity_id: "ENT-1",
        target_entity_key: "bob",
        target_entity_label: "Bob",
        relationship_type: "co_occurs",
        weight: 2,
        evidence_chunk_ids: ["CH-1"],
        source_documents: ["DOC-1"],
        relationship_metadata: {},
        created_at: NOW,
        hop_distance: 1,
        graph_source: graphSource,
      },
    ],
  };
}

function objectListing() {
  return {
    bucket: "reports",
    prefix: "",
    prefixes: [],
    objects: [
      {
        key: "incident-playbook.md",
        size: 512,
        created_at: NOW,
        last_modified: NOW,
        etag: "etag-1",
      },
    ],
    next_token: null,
    is_truncated: false,
  };
}

/**
 * Asset-workspace IA navigation helpers (Phase R1). The landing is the KB list;
 * opening a card enters the Workspace (header + Build · Explore · Calibrate ·
 * Use stage tabs). Explore fans out into an Ask / Chunks / Graph sub-nav.
 */
type WsUser = ReturnType<typeof userEvent.setup>;

async function openKnowledgeBaseWorkspace(
  user: WsUser,
  knowledgeBaseId = "KB-1",
): Promise<void> {
  const card = await screen.findByTestId(`kb-card-${knowledgeBaseId}`);
  await user.click(card);
  await screen.findByTestId("kb-workspace-header");
}

async function gotoStage(
  user: WsUser,
  stage: "Build" | "Explore" | "Calibrate" | "Use",
): Promise<void> {
  await user.click(await screen.findByRole("button", { name: stage }));
}

async function gotoExplore(
  user: WsUser,
  view: "ask" | "chunks" | "graph",
): Promise<void> {
  await user.click(await screen.findByRole("button", { name: "Explore" }));
  await user.click(await screen.findByTestId(`kb-explore-view-${view}`));
}

/** Open KB-1's Workspace and land on the requested section in one call. */
async function openTo(
  user: WsUser,
  target:
    | "build"
    | "use"
    | "calibrate"
    | "explore-ask"
    | "explore-chunks"
    | "explore-graph",
  knowledgeBaseId = "KB-1",
): Promise<void> {
  await openKnowledgeBaseWorkspace(user, knowledgeBaseId);
  if (target === "build") return gotoStage(user, "Build");
  if (target === "use") return gotoStage(user, "Use");
  if (target === "calibrate") return gotoStage(user, "Calibrate");
  if (target === "explore-ask") return gotoExplore(user, "ask");
  if (target === "explore-chunks") return gotoExplore(user, "chunks");
  return gotoExplore(user, "graph");
}

/**
 * R2 declutter: the Build stage hides chunking/embedding/GraphRAG config behind
 * an "Advanced configuration" disclosure. Open it before asserting on or editing
 * any of those controls.
 */
async function openBuildAdvanced(user: WsUser): Promise<void> {
  const toggle = await within(
    await screen.findByTestId("kb-build-advanced"),
  ).findByRole("button", { name: /Advanced configuration/ });
  await user.click(toggle);
}

/**
 * R2 declutter: the Graph explore sub-view hides the graph tuning knobs behind
 * an "Advanced retrieval" disclosure. Open it before editing those knobs.
 */
async function openGraphAdvanced(user: WsUser): Promise<void> {
  const toggle = await within(
    await screen.findByTestId("kb-graph-advanced"),
  ).findByRole("button", { name: /Advanced retrieval/ });
  await user.click(toggle);
}

/**
 * R3 declutter: the Explore Query (ask) view defaults to a clean ask box on the
 * header version. The multi-version selector grid + multi-select mode cards +
 * compare panel live behind a "Compare versions" disclosure. Open it before
 * asserting on or interacting with any of those controls.
 */
async function openExploreCompare(user: WsUser): Promise<void> {
  const toggle = await within(
    await screen.findByTestId("kb-explore-compare"),
  ).findByRole("button", { name: /Compare versions/ });
  await user.click(toggle);
}

/**
 * R3 declutter: query-time graph tuning + graph query profiles live under an
 * "Advanced retrieval" disclosure inside the Explore Compare panel. Open the
 * compare panel first, then this, before editing those knobs.
 */
async function openExploreAdvanced(user: WsUser): Promise<void> {
  const toggle = await within(
    await screen.findByTestId("kb-explore-advanced"),
  ).findByRole("button", { name: /Advanced retrieval/ });
  await user.click(toggle);
}

function renderKnowledgeBasesAt(initialPath: string): void {
  const queryClient = new QueryClient({
    defaultOptions: {
      queries: { retry: false, gcTime: 0 },
      mutations: { retry: false },
    },
  });
  rtlRender(
    <QueryClientProvider client={queryClient}>
      <MemoryRouter future={{ v7_startTransition: true, v7_relativeSplatPath: true }} initialEntries={[initialPath]}>
        <Routes>
          <Route path="/knowledge-bases" element={<KnowledgeBases />} />
        </Routes>
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

beforeAll(() => server.listen({ onUnhandledRequest: "error" }));
afterEach(() => server.resetHandlers());
afterAll(() => server.close());

describe("Knowledge Bases page", () => {
  it("surfaces the graph-native blueprint before the first build exists", async () => {
    server.use(
      http.get(`${KB}/options`, () =>
        HttpResponse.json(envelope(knowledgeOptions(true))),
      ),
      http.get(`${KB}`, () => HttpResponse.json(envelope([]))),
      http.get(`${OS}/buckets`, () =>
        HttpResponse.json(envelope([{ name: "reports", creation_date: NOW }])),
      ),
      http.get(`${OS}/buckets/reports/objects`, () =>
        HttpResponse.json(envelope(objectListing())),
      ),
    );

    const user = userEvent.setup();
    render(<KnowledgeBases />);

    // No corpus yet → "New knowledge base" opens a create-mode workspace whose
    // Build stage hosts the blueprint.
    await user.click(await screen.findByTestId("kb-new-knowledge-base"));

    const blueprint = await screen.findByTestId("kb-build-blueprint");
    expect(blueprint).toHaveTextContent("Pipeline blueprint");
    expect(blueprint).toHaveTextContent("Quick graph preset");
    expect(blueprint).toHaveTextContent("AGE-native retrieval");
    expect(blueprint).toHaveTextContent("Apache AGE graph");
    expect(blueprint).toHaveTextContent("Object store + Apache AGE");
  });

  it("shows GraphRAG and AGE onboarding in the graph explore view before any corpus exists", async () => {
    server.use(
      http.get(`${KB}/options`, () =>
        HttpResponse.json(envelope(knowledgeOptions(true))),
      ),
      http.get(`${KB}`, () => HttpResponse.json(envelope([]))),
      http.get(`${OS}/buckets`, () =>
        HttpResponse.json(envelope([{ name: "reports", creation_date: NOW }])),
      ),
      http.get(`${OS}/buckets/reports/objects`, () =>
        HttpResponse.json(envelope(objectListing())),
      ),
    );

    const user = userEvent.setup();
    render(<KnowledgeBases />);

    // Enter a create-mode workspace, then Explore → Graph shows the onboarding
    // (no corpus exists yet).
    await user.click(await screen.findByTestId("kb-new-knowledge-base"));
    await gotoExplore(user, "graph");

    const onboarding = await screen.findByTestId("kb-onboarding-graph");
    expect(onboarding).toHaveTextContent(
      "Build once, then inspect local and AGE graph views",
    );
    expect(onboarding).toHaveTextContent(
      "Cypher-first retrieval is available in this stack",
    );

    await user.click(screen.getByRole("button", { name: "Open Build tab" }));
    expect(await screen.findByTestId("kb-build-blueprint")).toBeInTheDocument();
  });

  it("surfaces the workspace header on open and reaches AGE retrieval from the graph explore view", async () => {
    const bases = [knowledgeBase()];
    const kbVersion = version();

    server.use(
      http.get(`${KB}/options`, () =>
        HttpResponse.json(envelope(knowledgeOptions(true))),
      ),
      http.get(`${KB}`, () => HttpResponse.json(envelope(bases))),
      http.get(`${OS}/buckets`, () =>
        HttpResponse.json(envelope([{ name: "reports", creation_date: NOW }])),
      ),
      http.get(`${OS}/buckets/reports/objects`, () =>
        HttpResponse.json(envelope(objectListing())),
      ),
      http.get(`${KB}/KB-1/versions`, () =>
        HttpResponse.json(envelope([kbVersion])),
      ),
      http.get(`${KB}/KB-1/runs`, () => HttpResponse.json(envelope([]))),
      http.get(`${API_BASE}/knowledge-base-versions/KBV-1/sources`, () =>
        HttpResponse.json(envelope([])),
      ),
      http.get(`${API_BASE}/knowledge-base-versions/KBV-1/chunks`, () =>
        HttpResponse.json(envelope([])),
      ),
      http.get(`${API_BASE}/knowledge-base-versions/KBV-1/entities`, () =>
        HttpResponse.json(envelope([])),
      ),
      http.get(`${API_BASE}/knowledge-base-versions/KBV-1/relationships`, () =>
        HttpResponse.json(envelope([])),
      ),
      http.get(
        `${API_BASE}/knowledge-base-versions/KBV-1/graph`,
        ({ request }) => {
          const url = new URL(request.url);
          const source = (url.searchParams.get("source") ?? "local") as
            | "local"
            | "age";
          return HttpResponse.json(
            envelope(graphExplorer(source, url.searchParams.get("q") ?? "")),
          );
        },
      ),
    );

    const user = userEvent.setup();
    render(<KnowledgeBases />);

    // Landing shows the card + filters; the always-on profile sidebar is gone.
    await screen.findByTestId("kb-card-KB-1");
    expect(screen.getByLabelText("Search knowledge bases")).toBeInTheDocument();
    // R2 calm card: a single health line, not the old badge/stat-box wall.
    expect(screen.getByTestId("kb-card-health-KB-1")).toHaveTextContent(
      "3 chunks · 2 entities",
    );
    expect(screen.getByTestId("kb-card-facts-KB-1")).toHaveTextContent(
      "1 source",
    );
    expect(screen.queryByText("Active graph pulse")).not.toBeInTheDocument();
    expect(
      screen.queryByTestId("knowledge-library-profile"),
    ).not.toBeInTheDocument();

    // Open the KB → its workspace header surfaces the KB facts.
    await openKnowledgeBaseWorkspace(user);
    const header = screen.getByTestId("kb-workspace-header");
    expect(header).toHaveTextContent("Operations Corpus");
    expect(header).toHaveTextContent("reports");
    expect(header).toHaveTextContent("KBV-1");
    expect(screen.getByTestId("kb-workspace-status")).toHaveTextContent("active");
    expect(
      screen.getByTestId("kb-workspace-version-switcher"),
    ).toBeInTheDocument();
    // The landing search box is no longer mounted inside the workspace.
    expect(
      screen.queryByLabelText("Search knowledge bases"),
    ).not.toBeInTheDocument();

    // Explore → Graph exposes the AGE retrieval entry point into the playground.
    await gotoExplore(user, "graph");
    await user.click(
      await screen.findByRole("button", { name: "Open AGE in Playground" }),
    );
    // The compare panel (multi-version + mode state) is behind a disclosure now,
    // and the graph-tuning summary chips behind Advanced retrieval.
    await openExploreCompare(user);
    await openExploreAdvanced(user);
    expect(
      await screen.findByText("Compare answers across versions"),
    ).toBeInTheDocument();
    expect(screen.getByText("AGE graph mode selected")).toBeInTheDocument();
  });

  it("explains why AGE retrieval is unavailable when the deployment cannot serve it", async () => {
    const bases = [
      {
        ...knowledgeBase(),
        active_version_summary: {
          ...knowledgeBase().active_version_summary,
          graph_target: "object_store",
          age_sync_status: "skipped",
          age_ready: false,
          age_graph_name: null,
        },
      },
    ];
    const portableVersion = {
      ...version(),
      graph_config: {
        ...version().graph_config,
        output_target: "object_store" as const,
      },
      summary: {
        ...version().summary,
        age_sync_status: "skipped",
        age_synced_nodes: 0,
        age_synced_edges: 0,
      },
    };

    server.use(
      http.get(`${KB}/options`, () =>
        HttpResponse.json(envelope(knowledgeOptions(false))),
      ),
      http.get(`${KB}`, () => HttpResponse.json(envelope(bases))),
      http.get(`${OS}/buckets`, () =>
        HttpResponse.json(envelope([{ name: "reports", creation_date: NOW }])),
      ),
      http.get(`${OS}/buckets/reports/objects`, () =>
        HttpResponse.json(envelope(objectListing())),
      ),
      http.get(`${KB}/KB-1/versions`, () =>
        HttpResponse.json(envelope([portableVersion])),
      ),
      http.get(`${KB}/KB-1/runs`, () => HttpResponse.json(envelope([]))),
      http.get(`${API_BASE}/knowledge-base-versions/KBV-1/sources`, () =>
        HttpResponse.json(envelope([])),
      ),
      http.get(`${API_BASE}/knowledge-base-versions/KBV-1/chunks`, () =>
        HttpResponse.json(envelope([])),
      ),
      http.get(`${API_BASE}/knowledge-base-versions/KBV-1/entities`, () =>
        HttpResponse.json(envelope([])),
      ),
      http.get(`${API_BASE}/knowledge-base-versions/KBV-1/relationships`, () =>
        HttpResponse.json(envelope([])),
      ),
      http.get(
        `${API_BASE}/knowledge-base-versions/KBV-1/graph`,
        ({ request }) => {
          const url = new URL(request.url);
          return HttpResponse.json(
            envelope(graphExplorer("local", url.searchParams.get("q") ?? "")),
          );
        },
      ),
    );

    const user = userEvent.setup();
    render(<KnowledgeBases />);

    // Open the KB and visit its Build stage — the build form explains why AGE
    // retrieval is unavailable in this deployment.
    await openTo(user, "build");
    expect(
      screen.queryByText(/unlocks after the next build or a manual sync/i),
    ).not.toBeInTheDocument();
    await openBuildAdvanced(user);
    expect(
      await screen.findByText((content) =>
        content.includes(
          "CALIBER will keep builds on version-scoped graph artifacts",
        ),
      ),
    ).toBeInTheDocument();
  });

  it("submits AGE-backed graph configuration during a new build", async () => {
    const postedBodies: Array<Record<string, unknown>> = [];
    let bases: Array<ReturnType<typeof knowledgeBase>> = [];
    const kbVersion = version();
    const kbRun = run();

    server.use(
      http.get(`${KB}/options`, () =>
        HttpResponse.json(envelope(knowledgeOptions(true))),
      ),
      http.get(`${KB}`, () => HttpResponse.json(envelope(bases))),
      http.get(`${OS}/buckets`, () =>
        HttpResponse.json(envelope([{ name: "reports", creation_date: NOW }])),
      ),
      http.get(`${OS}/buckets/reports/objects`, () =>
        HttpResponse.json(envelope(objectListing())),
      ),
      http.post(`${KB}`, async ({ request }) => {
        const body = (await request.json()) as Record<string, unknown>;
        postedBodies.push(body);
        bases = [knowledgeBase()];
        return HttpResponse.json(
          envelope({
            knowledge_base: bases[0],
            version: kbVersion,
            run: kbRun,
          }),
          { status: 201 },
        );
      }),
      http.get(`${KB}/KB-1/versions`, () =>
        HttpResponse.json(envelope([kbVersion])),
      ),
      http.get(`${KB}/KB-1/runs`, () => HttpResponse.json(envelope([kbRun]))),
      http.get(`${API_BASE}/knowledge-base-versions/KBV-1/sources`, () =>
        HttpResponse.json(envelope([])),
      ),
      http.get(`${API_BASE}/knowledge-base-versions/KBV-1/chunks`, () =>
        HttpResponse.json(envelope([])),
      ),
      http.get(`${API_BASE}/knowledge-base-versions/KBV-1/entities`, () =>
        HttpResponse.json(envelope([])),
      ),
      http.get(`${API_BASE}/knowledge-base-versions/KBV-1/relationships`, () =>
        HttpResponse.json(envelope([])),
      ),
      http.get(
        `${API_BASE}/knowledge-base-versions/KBV-1/graph`,
        ({ request }) => {
          const url = new URL(request.url);
          const source = (url.searchParams.get("source") ?? "local") as
            | "local"
            | "age";
          return HttpResponse.json(
            envelope(graphExplorer(source, url.searchParams.get("q") ?? "")),
          );
        },
      ),
      http.get(`${API_BASE}/knowledge-runs/KBR-1/events`, () =>
        HttpResponse.json(envelope([])),
      ),
    );

    const user = userEvent.setup();
    render(<KnowledgeBases />);

    await user.click(await screen.findByTestId("kb-new-knowledge-base"));
    await openBuildAdvanced(user);
    expect(await screen.findByText("GraphRAG Outputs")).toBeInTheDocument();
    expect(screen.getByText("AGE → knowledge_graph")).toBeInTheDocument();
    expect(screen.getByText("Auto-sync on build")).toBeInTheDocument();
    expect(
      screen.getByText("Primary retrieval · Apache AGE graph"),
    ).toBeInTheDocument();
    await waitFor(() => {
      expect(
        screen.getByText("AGE graph retrieval active"),
      ).toBeInTheDocument();
      expect(
        screen.getByText("Primary retrieval · Apache AGE graph"),
      ).toBeInTheDocument();
      expect(
        screen.getByRole("combobox", { name: /Graph sync target/ }),
      ).toHaveValue("object_store_and_age");
      expect(
        screen.getByRole("combobox", { name: /Default retrieval path/ }),
      ).toHaveValue("age_graph");
      expect(
        screen.getByRole("combobox", { name: /Retrieval strength/ }),
      ).toHaveValue("balanced");
      expect(screen.getByLabelText("AGE traversal hops")).toHaveValue("1");
      expect(screen.getByLabelText("AGE candidate pool")).toHaveValue(24);
      expect(screen.getByLabelText("AGE dense rerank weight")).toHaveValue(
        0.35,
      );
    });

    await user.selectOptions(screen.getByLabelText("Bucket"), "reports");
    await screen.findByText("incident-playbook.md");
    await user.click(
      screen.getByRole("checkbox", { name: "Select file incident-playbook.md" }),
    );
    expect(await screen.findByText("1 selected")).toBeInTheDocument();

    await user.type(
      screen.getByLabelText("Knowledge-base name"),
      "Operations Corpus",
    );
    await user.type(
      screen.getByLabelText("Description"),
      "AGE-synced operational knowledge.",
    );
    const ageCandidatePool = await screen.findByLabelText("AGE candidate pool");
    fireEvent.change(ageCandidatePool, { target: { value: "44" } });
    await user.click(
      screen.getByLabelText("Require strict AGE retrieval by default"),
    );
    expect(
      screen.getByText(/Custom graph profile active/i),
    ).toBeInTheDocument();
    expect(screen.getAllByText("Strict AGE default").length).toBeGreaterThan(0);
    const submitButton = screen.getByRole("button", {
      name: "Create knowledge base",
    });
    await waitFor(() => expect(submitButton).toBeEnabled());
    const buildForm = submitButton.closest("form");
    expect(buildForm).not.toBeNull();
    fireEvent.submit(buildForm!);

    await waitFor(() => expect(postedBodies).toHaveLength(1));
    expect(postedBodies[0]).toMatchObject({
      source_bucket: "reports",
      sources: [{ kind: "file", path: "incident-playbook.md" }],
      graph_config: expect.objectContaining({
        output_target: "object_store_and_age",
        default_retrieval_mode: "age_graph",
        retrieval_strength: "balanced",
        age_traversal_hops: 1,
        age_candidate_pool_size: 44,
        age_dense_rerank_weight: 0.35,
        strict_age_retrieval_default: true,
      }),
    });

    expect(await screen.findByText("Version History")).toBeInTheDocument();
    expect(
      screen.getAllByText(/Object store \+ Apache AGE/).length,
    ).toBeGreaterThan(0);
  });

  it("blocks knowledge-base creation when local embeddings are flagged in the runtime", async () => {
    const embeddingBlockedReason =
      "Local Hugging Face embedding builds are blocked because the current runtime includes flagged dependencies: torch 2.12.0 (CVE-2025-3000). Set CALIBER_ALLOW_FLAGGED_LOCAL_EMBEDDINGS=true only if you explicitly accept the risk for this deployment.";

    server.use(
      http.get(`${KB}/options`, () =>
        HttpResponse.json(
          envelope(
            knowledgeOptions(true, "http://127.0.0.1:8082", embeddingBlockedReason),
          ),
        ),
      ),
      http.get(`${KB}`, () => HttpResponse.json(envelope([]))),
      http.get(`${OS}/buckets`, () =>
        HttpResponse.json(envelope([{ name: "reports", creation_date: NOW }])),
      ),
      http.get(`${OS}/buckets/reports/objects`, () =>
        HttpResponse.json(envelope(objectListing())),
      ),
    );

    const user = userEvent.setup();
    render(<KnowledgeBases />);

    await user.click(await screen.findByTestId("kb-new-knowledge-base"));
    await user.selectOptions(screen.getByLabelText("Bucket"), "reports");
    await screen.findByText("incident-playbook.md");
    await user.click(
      screen.getByRole("checkbox", { name: "Select file incident-playbook.md" }),
    );
    await user.type(
      screen.getByLabelText("Knowledge-base name"),
      "Blocked Corpus",
    );

    await openBuildAdvanced(user);
    const embeddingSelect = screen
      .getByText("Embedding model")
      .closest("label")
      ?.querySelector("select");
    expect(embeddingSelect).not.toBeNull();
    expect(embeddingSelect).toBeDisabled();
    expect(screen.getByText(embeddingBlockedReason)).toBeInTheDocument();
    expect(
      screen.getByRole("button", { name: "Create knowledge base" }),
    ).toBeDisabled();
  });

  it("defaults an existing knowledge base back to Apache AGE sync for the next build", async () => {
    const postedBodies: Array<Record<string, unknown>> = [];
    const bases = [knowledgeBase()];
    const legacyVersion = {
      ...version(),
      graph_config: {
        ...version().graph_config,
        output_target: "object_store",
      },
      summary: {
        ...version().summary,
        age_sync_status: "skipped",
        age_synced_nodes: 0,
        age_synced_edges: 0,
      },
    };
    const nextVersion = version();
    const nextRun = run();

    server.use(
      http.get(`${KB}/options`, () =>
        HttpResponse.json(envelope(knowledgeOptions(true))),
      ),
      http.get(`${KB}`, () => HttpResponse.json(envelope(bases))),
      http.get(`${OS}/buckets`, () =>
        HttpResponse.json(envelope([{ name: "reports", creation_date: NOW }])),
      ),
      http.get(`${OS}/buckets/reports/objects`, () =>
        HttpResponse.json(envelope(objectListing())),
      ),
      http.get(`${KB}/KB-1/versions`, () =>
        HttpResponse.json(envelope([legacyVersion])),
      ),
      http.get(`${KB}/KB-1/runs`, () => HttpResponse.json(envelope([]))),
      http.post(`${KB}/KB-1/versions`, async ({ request }) => {
        postedBodies.push((await request.json()) as Record<string, unknown>);
        return HttpResponse.json(
          envelope({
            knowledge_base: bases[0],
            version: nextVersion,
            run: nextRun,
          }),
          { status: 201 },
        );
      }),
      http.get(`${API_BASE}/knowledge-base-versions/KBV-1/sources`, () =>
        HttpResponse.json(envelope([])),
      ),
      http.get(`${API_BASE}/knowledge-base-versions/KBV-1/chunks`, () =>
        HttpResponse.json(envelope([])),
      ),
      http.get(`${API_BASE}/knowledge-base-versions/KBV-1/entities`, () =>
        HttpResponse.json(envelope([])),
      ),
      http.get(`${API_BASE}/knowledge-base-versions/KBV-1/relationships`, () =>
        HttpResponse.json(envelope([])),
      ),
      http.get(
        `${API_BASE}/knowledge-base-versions/KBV-1/graph`,
        ({ request }) => {
          const url = new URL(request.url);
          const source = (url.searchParams.get("source") ?? "local") as
            | "local"
            | "age";
          return HttpResponse.json(
            envelope(graphExplorer(source, url.searchParams.get("q") ?? "")),
          );
        },
      ),
      http.get(`${API_BASE}/knowledge-runs/KBR-1/events`, () =>
        HttpResponse.json(envelope([])),
      ),
    );

    const user = userEvent.setup();
    render(<KnowledgeBases />);

    // Open the existing KB → its Build stage re-builds the corpus (existing mode).
    await openTo(user, "build");
    await openBuildAdvanced(user);
    expect(await screen.findByText("GraphRAG Outputs")).toBeInTheDocument();
    expect(
      screen.getByText(
        /the next build is preconfigured to sync its graph there/i,
      ),
    ).toBeInTheDocument();

    const graphTarget = screen.getByRole("combobox", {
      name: /Graph sync target/,
    }) as HTMLSelectElement;
    expect(graphTarget.value).toBe("object_store_and_age");

    const submitButton = screen.getByRole("button", { name: "Create version" });
    await waitFor(() => expect(submitButton).toBeEnabled());
    const buildForm = submitButton.closest("form");
    expect(buildForm).not.toBeNull();
    fireEvent.submit(buildForm!);

    await waitFor(() => expect(postedBodies).toHaveLength(1));
    expect(postedBodies[0]).toMatchObject({
      graph_config: expect.objectContaining({
        default_retrieval_mode: "age_graph",
        output_target: "object_store_and_age",
      }),
    });
  });

  it("hydrates the build tab from object-store launch params and stages AGE-native retrieval", async () => {
    server.use(
      http.get(`${KB}/options`, () =>
        HttpResponse.json(envelope(knowledgeOptions(true))),
      ),
      http.get(`${KB}`, () => HttpResponse.json(envelope([knowledgeBase()]))),
      http.get(`${OS}/buckets`, () =>
        HttpResponse.json(envelope([{ name: "reports", creation_date: NOW }])),
      ),
      http.get(`${OS}/buckets/reports/objects`, () =>
        HttpResponse.json(envelope(objectListing())),
      ),
      http.get(`${KB}/KB-1/versions`, () =>
        HttpResponse.json(envelope([version()])),
      ),
      http.get(`${KB}/KB-1/runs`, () => HttpResponse.json(envelope([]))),
      http.get(`${API_BASE}/knowledge-base-versions/KBV-1/sources`, () =>
        HttpResponse.json(envelope([])),
      ),
      http.get(`${API_BASE}/knowledge-base-versions/KBV-1/chunks`, () =>
        HttpResponse.json(envelope([])),
      ),
      http.get(`${API_BASE}/knowledge-base-versions/KBV-1/entities`, () =>
        HttpResponse.json(envelope([])),
      ),
      http.get(`${API_BASE}/knowledge-base-versions/KBV-1/relationships`, () =>
        HttpResponse.json(envelope([])),
      ),
      http.get(
        `${API_BASE}/knowledge-base-versions/KBV-1/graph`,
        ({ request }) => {
          const url = new URL(request.url);
          const source = (url.searchParams.get("source") ?? "local") as
            | "local"
            | "age";
          return HttpResponse.json(
            envelope(graphExplorer(source, url.searchParams.get("q") ?? "")),
          );
        },
      ),
    );

    const user = userEvent.setup();
    renderKnowledgeBasesAt(
      "/knowledge-bases?tab=build&build_mode=new&bucket=reports&graph_preset=age_native&source=file:incident-playbook.md&source=folder:docs/",
    );

    // The import banner + source picker stay on the main flow (not collapsed).
    // Wait for options to hydrate so the AGE-native preset is applied to the banner.
    await screen.findByTestId("kb-build-import-banner");
    await waitFor(() =>
      expect(screen.getByTestId("kb-build-import-banner")).toHaveTextContent(
        "Imported 2 object-store sources from reports. AGE-native retrieval is staged for this build.",
      ),
    );
    expect(screen.getAllByText("incident-playbook.md").length).toBeGreaterThan(
      0,
    );
    expect(screen.getAllByText("docs/").length).toBeGreaterThan(0);
    expect(screen.getByRole("combobox", { name: "Bucket" })).toHaveValue(
      "reports",
    );

    // The GraphRAG config moved behind the Advanced configuration disclosure.
    await openBuildAdvanced(user);
    expect(await screen.findByText("GraphRAG Outputs")).toBeInTheDocument();
    await waitFor(() => {
      expect(
        screen.getByText("AGE graph retrieval active"),
      ).toBeInTheDocument();
      expect(
        screen.getByRole("combobox", { name: /Graph sync target/ }),
      ).toHaveValue("object_store_and_age");
      expect(
        screen.getByRole("combobox", { name: /Default retrieval path/ }),
      ).toHaveValue("age_graph");
    });
  });

  it("can sync an existing version into Apache AGE after the build completes", async () => {
    let syncCalls = 0;
    const bases = [knowledgeBase()];
    let currentVersion = {
      ...version(),
      graph_config: {
        ...version().graph_config,
        output_target: "object_store",
      },
      summary: {
        ...version().summary,
        age_sync_status: "skipped",
        age_synced_nodes: 0,
        age_synced_edges: 0,
      },
    };

    server.use(
      http.get(`${KB}/options`, () =>
        HttpResponse.json(envelope(knowledgeOptions(true))),
      ),
      http.get(`${KB}`, () => HttpResponse.json(envelope(bases))),
      http.get(`${OS}/buckets`, () =>
        HttpResponse.json(envelope([{ name: "reports", creation_date: NOW }])),
      ),
      http.get(`${OS}/buckets/reports/objects`, () =>
        HttpResponse.json(envelope(objectListing())),
      ),
      http.get(`${KB}/KB-1/versions`, () =>
        HttpResponse.json(envelope([currentVersion])),
      ),
      http.get(`${KB}/KB-1/runs`, () => HttpResponse.json(envelope([]))),
      http.post(`${API_BASE}/knowledge-base-versions/KBV-1/age-sync`, () => {
        syncCalls += 1;
        currentVersion = {
          ...version(),
          graph_config: {
            ...version().graph_config,
            default_retrieval_mode: "age_graph",
          },
          summary: {
            ...version().summary,
            age_sync_attempted_at: NOW,
          },
        };
        return HttpResponse.json(envelope(currentVersion));
      }),
      http.get(`${API_BASE}/knowledge-base-versions/KBV-1/sources`, () =>
        HttpResponse.json(envelope([])),
      ),
      http.get(`${API_BASE}/knowledge-base-versions/KBV-1/chunks`, () =>
        HttpResponse.json(envelope([])),
      ),
      http.get(`${API_BASE}/knowledge-base-versions/KBV-1/entities`, () =>
        HttpResponse.json(envelope([])),
      ),
      http.get(`${API_BASE}/knowledge-base-versions/KBV-1/relationships`, () =>
        HttpResponse.json(envelope([])),
      ),
      http.get(
        `${API_BASE}/knowledge-base-versions/KBV-1/graph`,
        ({ request }) => {
          const url = new URL(request.url);
          const source = (url.searchParams.get("source") ?? "local") as
            | "local"
            | "age";
          return HttpResponse.json(
            envelope(graphExplorer(source, url.searchParams.get("q") ?? "")),
          );
        },
      ),
    );

    const user = userEvent.setup();
    render(<KnowledgeBases />);

    // The version-detail (and its AGE sync action) lives under Explore → Chunks.
    await openTo(user, "explore-chunks");
    expect(
      await screen.findByRole("button", { name: "Enable AGE graph retrieval" }),
    ).toBeInTheDocument();

    await user.click(
      screen.getByRole("button", { name: "Enable AGE graph retrieval" }),
    );
    await waitFor(() => expect(syncCalls).toBe(1));
    expect(screen.getByText("Apache AGE graph")).toBeInTheDocument();

    await user.click(
      screen.getByRole("button", { name: "Compare in playground" }),
    );
    await openExploreCompare(user);
    await openExploreAdvanced(user);
    expect(
      await screen.findByText("Compare answers across versions"),
    ).toBeInTheDocument();
    expect(screen.getByText("Following build default")).toBeInTheDocument();
    expect(
      screen.getByText("Build default → Apache AGE graph"),
    ).toBeInTheDocument();

    await gotoExplore(user, "chunks");
    expect(
      await screen.findByRole("button", { name: "Query with AGE" }),
    ).toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: "Query with AGE" }));
    // The compare disclosure (component state) stays open across sub-view
    // switches; the inner Advanced retrieval disclosure remounts collapsed.
    expect(
      await screen.findByText("Compare answers across versions"),
    ).toBeInTheDocument();
    await openExploreAdvanced(user);
    expect(screen.getByText("AGE graph mode selected")).toBeInTheDocument();
  });

  it("refreshes the graph explorer when an AGE sync finishes on the graph tab", async () => {
    let syncCalls = 0;
    const bases = [knowledgeBase()];
    let currentVersion = {
      ...version(),
      summary: {
        ...version().summary,
        age_sync_status: "failed",
        age_sync_error: "age sync unavailable",
        age_synced_nodes: 0,
        age_synced_edges: 0,
      },
    };

    server.use(
      http.get(`${KB}/options`, () =>
        HttpResponse.json(envelope(knowledgeOptions(true))),
      ),
      http.get(`${KB}`, () => HttpResponse.json(envelope(bases))),
      http.get(`${OS}/buckets`, () =>
        HttpResponse.json(envelope([{ name: "reports", creation_date: NOW }])),
      ),
      http.get(`${OS}/buckets/reports/objects`, () =>
        HttpResponse.json(envelope(objectListing())),
      ),
      http.get(`${KB}/KB-1/versions`, () =>
        HttpResponse.json(envelope([currentVersion])),
      ),
      http.get(`${KB}/KB-1/runs`, () => HttpResponse.json(envelope([]))),
      http.post(`${API_BASE}/knowledge-base-versions/KBV-1/age-sync`, () => {
        syncCalls += 1;
        currentVersion = version();
        return HttpResponse.json(envelope(currentVersion));
      }),
      http.get(`${API_BASE}/knowledge-base-versions/KBV-1/sources`, () =>
        HttpResponse.json(envelope([])),
      ),
      http.get(`${API_BASE}/knowledge-base-versions/KBV-1/chunks`, () =>
        HttpResponse.json(envelope([])),
      ),
      http.get(`${API_BASE}/knowledge-base-versions/KBV-1/entities`, () =>
        HttpResponse.json(envelope([])),
      ),
      http.get(`${API_BASE}/knowledge-base-versions/KBV-1/relationships`, () =>
        HttpResponse.json(envelope([])),
      ),
      http.get(
        `${API_BASE}/knowledge-base-versions/KBV-1/graph`,
        ({ request }) => {
          const url = new URL(request.url);
          const source = (url.searchParams.get("source") ?? "local") as
            | "local"
            | "age";
          if (
            source === "age" &&
            currentVersion.summary.age_sync_status === "synced"
          ) {
            return HttpResponse.json(
              envelope(graphExplorer("age", url.searchParams.get("q") ?? "")),
            );
          }
          if (source === "age") {
            return HttpResponse.json(
              envelope({
                ...graphExplorer("local", url.searchParams.get("q") ?? ""),
                requested_source: "age",
                served_source: "local",
                age_ready: false,
                age_status: "failed",
                fallback_reason:
                  "This knowledge-base version did not finish syncing to Apache AGE.",
              }),
            );
          }
          return HttpResponse.json(
            envelope({
              ...graphExplorer("local", url.searchParams.get("q") ?? ""),
              age_ready: currentVersion.summary.age_sync_status === "synced",
              age_status: currentVersion.summary.age_sync_status,
            }),
          );
        },
      ),
    );

    const user = userEvent.setup();
    render(<KnowledgeBases />);

    await openTo(user, "explore-graph");
    expect(
      await screen.findByText(
        /currently serving the version-local graph instead/i,
      ),
    ).toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: "Retry AGE sync" }));
    await waitFor(() => expect(syncCalls).toBe(1));

    expect(
      await screen.findByText(
        /executing an AGE-backed neighborhood query over the synced graph/i,
      ),
    ).toBeInTheDocument();
    expect(
      screen.queryByText(/currently serving the version-local graph instead/i),
    ).not.toBeInTheDocument();
  });

  it("uses the saved default retrieval path when opening the playground", async () => {
    let queryBody: Record<string, unknown> | null = null;
    const bases = [knowledgeBase()];
    const kbVersion = {
      ...version(),
      graph_config: {
        ...version().graph_config,
        default_retrieval_mode: "age_graph",
      },
    };

    server.use(
      http.get(`${KB}/options`, () =>
        HttpResponse.json(envelope(knowledgeOptions(true))),
      ),
      http.get(`${KB}`, () => HttpResponse.json(envelope(bases))),
      http.get(`${OS}/buckets`, () =>
        HttpResponse.json(envelope([{ name: "reports", creation_date: NOW }])),
      ),
      http.get(`${OS}/buckets/reports/objects`, () =>
        HttpResponse.json(envelope(objectListing())),
      ),
      http.get(`${KB}/KB-1/versions`, () =>
        HttpResponse.json(envelope([kbVersion])),
      ),
      http.get(`${KB}/KB-1/runs`, () => HttpResponse.json(envelope([]))),
      http.get(`${API_BASE}/knowledge-base-versions/KBV-1/sources`, () =>
        HttpResponse.json(envelope([])),
      ),
      http.get(`${API_BASE}/knowledge-base-versions/KBV-1/chunks`, () =>
        HttpResponse.json(envelope([])),
      ),
      http.get(`${API_BASE}/knowledge-base-versions/KBV-1/entities`, () =>
        HttpResponse.json(envelope([])),
      ),
      http.get(`${API_BASE}/knowledge-base-versions/KBV-1/relationships`, () =>
        HttpResponse.json(envelope([])),
      ),
      http.get(
        `${API_BASE}/knowledge-base-versions/KBV-1/graph`,
        ({ request }) => {
          const url = new URL(request.url);
          const source = (url.searchParams.get("source") ?? "local") as
            | "local"
            | "age";
          return HttpResponse.json(
            envelope(graphExplorer(source, url.searchParams.get("q") ?? "")),
          );
        },
      ),
      http.post(QUERY, async ({ request }) => {
        queryBody = (await request.json()) as Record<string, unknown>;
        return HttpResponse.json(
          envelope({
            question: "Who owns platform reliability?",
            versions: [
              {
                knowledge_base_version_id: "KBV-1",
                knowledge_base_id: "KB-1",
                version_number: 1,
                knowledge_base_name: "Operations Corpus",
                chunking_strategy: "recursive",
                embedding_model: "sentence-transformers/all-MiniLM-L6-v2",
                retrieval_mode: "age_graph",
                answer: "Bob owns Platform reliability.",
                answer_error: null,
                citations: [],
                retrieved_chunks: [],
                graph_context: {
                  age_graph_name: "knowledge_graph",
                  age_status: "ok",
                },
                timing_ms: { total: 42 },
              },
            ],
          }),
        );
      }),
    );

    const user = userEvent.setup();
    render(<KnowledgeBases />);

    await openTo(user, "explore-ask");
    // The default Query view runs against the header version with no compare
    // panel mounted: the ask box is visible immediately.
    expect(
      screen.getByPlaceholderText("Ask a question about the selected documents…"),
    ).toBeInTheDocument();
    expect(
      screen.queryByText("Compare answers across versions"),
    ).not.toBeInTheDocument();

    // The build-default chips live behind the Compare versions disclosure, with
    // the graph-tuning summary under Advanced retrieval.
    await openExploreCompare(user);
    await openExploreAdvanced(user);
    expect(
      await screen.findByText("Compare answers across versions"),
    ).toBeInTheDocument();
    expect(screen.getByText("Following build default")).toBeInTheDocument();
    expect(
      screen.getAllByText("Default Apache AGE graph").length,
    ).toBeGreaterThan(0);
    expect(
      screen.getByText("Build default → Apache AGE graph"),
    ).toBeInTheDocument();

    await user.type(
      screen.getByPlaceholderText(
        "Ask a question about the selected documents…",
      ),
      "Who owns platform reliability?",
    );
    await user.click(screen.getByRole("button", { name: "Ask" }));

    await waitFor(() =>
      expect(queryBody).toMatchObject({
        version_ids: ["KBV-1"],
        retrieval_modes: [],
      }),
    );
    expect(await screen.findByText("Apache AGE context")).toBeInTheDocument();
  });

  it("surfaces Apache AGE retrieval in the playground and sends the age_graph mode", async () => {
    let queryBody: Record<string, unknown> | null = null;
    const bases = [knowledgeBase()];
    const kbVersion = {
      ...version(),
      graph_config: {
        ...version().graph_config,
        default_retrieval_mode: "dense",
      },
    };

    server.use(
      http.get(`${KB}/options`, () =>
        HttpResponse.json(envelope(knowledgeOptions(true))),
      ),
      http.get(`${KB}`, () => HttpResponse.json(envelope(bases))),
      http.get(`${OS}/buckets`, () =>
        HttpResponse.json(envelope([{ name: "reports", creation_date: NOW }])),
      ),
      http.get(`${OS}/buckets/reports/objects`, () =>
        HttpResponse.json(envelope(objectListing())),
      ),
      http.get(`${KB}/KB-1/versions`, () =>
        HttpResponse.json(envelope([kbVersion])),
      ),
      http.get(`${KB}/KB-1/runs`, () => HttpResponse.json(envelope([]))),
      http.get(`${API_BASE}/knowledge-base-versions/KBV-1/sources`, () =>
        HttpResponse.json(envelope([])),
      ),
      http.get(`${API_BASE}/knowledge-base-versions/KBV-1/chunks`, () =>
        HttpResponse.json(envelope([])),
      ),
      http.get(`${API_BASE}/knowledge-base-versions/KBV-1/entities`, () =>
        HttpResponse.json(envelope([])),
      ),
      http.get(`${API_BASE}/knowledge-base-versions/KBV-1/relationships`, () =>
        HttpResponse.json(envelope([])),
      ),
      http.get(
        `${API_BASE}/knowledge-base-versions/KBV-1/graph`,
        ({ request }) => {
          const url = new URL(request.url);
          const source = (url.searchParams.get("source") ?? "local") as
            | "local"
            | "age";
          return HttpResponse.json(
            envelope(graphExplorer(source, url.searchParams.get("q") ?? "")),
          );
        },
      ),
      http.post(QUERY, async ({ request }) => {
        queryBody = (await request.json()) as Record<string, unknown>;
        return HttpResponse.json(
          envelope({
            question: "Who owns platform reliability?",
            versions: [
              {
                knowledge_base_version_id: "KBV-1",
                knowledge_base_id: "KB-1",
                version_number: 1,
                knowledge_base_name: "Operations Corpus",
                chunking_strategy: "recursive",
                embedding_model: "sentence-transformers/all-MiniLM-L6-v2",
                retrieval_mode: "age_graph",
                answer: "Bob owns Platform reliability.",
                answer_error: null,
                citations: [
                  {
                    chunk_id: "CH-1",
                    label: "incident-playbook.md",
                    source_bucket: "reports",
                    source_key: "incident-playbook.md",
                    object_store_path:
                      "/object-store?bucket=reports&key=incident-playbook.md",
                    score: 0.99,
                  },
                ],
                retrieved_chunks: [
                  {
                    chunk_id: "CH-1",
                    source_bucket: "reports",
                    source_key: "incident-playbook.md",
                    source_name: "incident-playbook.md",
                    score: 1.21,
                    content:
                      "Alice leads Support. Bob owns Platform reliability.",
                    chunk_index: 0,
                    ordinal: 0,
                    document_id: "DOC-1",
                    metadata: {},
                    object_store_path:
                      "/object-store?bucket=reports&key=incident-playbook.md",
                    score_breakdown: {
                      dense: 0.82,
                      dense_rerank: 0.123,
                      graph_boost: 0.39,
                      age_graph: 4.5,
                      age_direct: 3.0,
                      age_one_hop: 1.5,
                      age_two_hop: 0,
                    },
                    matched_entity_labels: ["Bob", "Platform reliability"],
                  },
                ],
                graph_context: {
                  requested_backend: "heuristic",
                  applied_backend: "heuristic",
                  matched_entities: ["Bob"],
                  expanded_entities: ["Platform reliability"],
                  boosted_chunk_count: 1,
                  relationship_count: 1,
                  age_graph_name: "knowledge_graph",
                  age_status: "ok",
                  age_seed_strategy: "query_text",
                  age_traversal_hops: 1,
                  age_matched_chunk_count: 7,
                  age_candidate_pool_size: 24,
                  age_dense_rerank_weight: 0.15,
                  minimum_relationship_weight: 2.5,
                  retrieval_strength: "balanced",
                  query_override_active: true,
                  strict_age_retrieval: true,
                },
                timing_ms: {
                  total: 42,
                },
              },
            ],
          }),
        );
      }),
    );

    const user = userEvent.setup();
    render(<KnowledgeBases />);

    await openTo(user, "explore-ask");
    await openExploreCompare(user);
    expect(
      await screen.findByText("Compare answers across versions"),
    ).toBeInTheDocument();
    expect(
      screen.getByRole("button", { name: /Apache AGE graph/ }),
    ).toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: /Apache AGE graph/ }));
    await openExploreAdvanced(user);
    expect(
      screen.getByText("1/1 selected versions AGE-ready"),
    ).toBeInTheDocument();
    await user.click(screen.getByLabelText("Enable query-time graph tuning"));
    await user.selectOptions(
      screen.getByLabelText("Retrieval strength"),
      "balanced",
    );
    fireEvent.change(screen.getByLabelText("Min relationship weight"), {
      target: { value: "2.5" },
    });
    await user.selectOptions(
      screen.getByLabelText("Playground AGE seed mode"),
      "query_text_only",
    );
    await user.selectOptions(
      screen.getByLabelText("Playground AGE traversal hops"),
      "2",
    );
    fireEvent.change(screen.getByLabelText("Playground AGE candidate pool"), {
      target: { value: "40" },
    });
    fireEvent.change(
      screen.getByLabelText("Playground AGE dense rerank weight"),
      {
        target: { value: "0.15" },
      },
    );
    await user.click(screen.getByLabelText("Require strict AGE retrieval"));
    await user.type(
      screen.getByPlaceholderText(
        "Ask a question about the selected documents…",
      ),
      "Who owns platform reliability?",
    );
    await user.click(screen.getByRole("button", { name: "Ask" }));

    await waitFor(() =>
      expect(queryBody).toMatchObject({
        version_ids: ["KBV-1"],
        retrieval_modes: ["age_graph"],
        graph_overrides: {
          retrieval_strength: "balanced",
          minimum_relationship_weight: 2.5,
          age_seed_mode: "query_text_only",
          age_traversal_hops: 2,
          age_candidate_pool_size: 40,
          age_dense_rerank_weight: 0.15,
          strict_age_retrieval: true,
        },
      }),
    );

    expect(await screen.findByText("Apache AGE context")).toBeInTheDocument();
    expect(screen.getByText(/knowledge_graph/)).toBeInTheDocument();
    expect(
      screen.getByText("Bob owns Platform reliability."),
    ).toBeInTheDocument();
    expect(screen.getByText("Platform reliability")).toBeInTheDocument();
    expect(screen.getByText(/Seeded from question text/i)).toBeInTheDocument();
    expect(
      screen.getByText(/matched 7 chunks before rerank/i),
    ).toBeInTheDocument();
    expect(screen.getAllByText(/pool 24/).length).toBeGreaterThan(0);
    expect(screen.getAllByText(/dense x0.15/i).length).toBeGreaterThan(0);
    expect(screen.getAllByText(/strict AGE/i).length).toBeGreaterThan(0);
  });

  it("opens an AGE-ready version directly into AGE retrieval from the version detail", async () => {
    let queryBody: Record<string, unknown> | null = null;
    const bases = [knowledgeBase()];
    const kbVersion = version();

    server.use(
      http.get(`${KB}/options`, () =>
        HttpResponse.json(envelope(knowledgeOptions(true))),
      ),
      http.get(`${KB}`, () => HttpResponse.json(envelope(bases))),
      http.get(`${OS}/buckets`, () =>
        HttpResponse.json(envelope([{ name: "reports", creation_date: NOW }])),
      ),
      http.get(`${OS}/buckets/reports/objects`, () =>
        HttpResponse.json(envelope(objectListing())),
      ),
      http.get(`${KB}/KB-1/versions`, () =>
        HttpResponse.json(envelope([kbVersion])),
      ),
      http.get(`${KB}/KB-1/runs`, () => HttpResponse.json(envelope([]))),
      http.get(`${API_BASE}/knowledge-base-versions/KBV-1/sources`, () =>
        HttpResponse.json(envelope([])),
      ),
      http.get(`${API_BASE}/knowledge-base-versions/KBV-1/chunks`, () =>
        HttpResponse.json(envelope([])),
      ),
      http.get(`${API_BASE}/knowledge-base-versions/KBV-1/entities`, () =>
        HttpResponse.json(envelope([])),
      ),
      http.get(`${API_BASE}/knowledge-base-versions/KBV-1/relationships`, () =>
        HttpResponse.json(envelope([])),
      ),
      http.get(
        `${API_BASE}/knowledge-base-versions/KBV-1/graph`,
        ({ request }) => {
          const url = new URL(request.url);
          const source = (url.searchParams.get("source") ?? "local") as
            | "local"
            | "age";
          return HttpResponse.json(
            envelope(graphExplorer(source, url.searchParams.get("q") ?? "")),
          );
        },
      ),
      http.post(QUERY, async ({ request }) => {
        queryBody = (await request.json()) as Record<string, unknown>;
        return HttpResponse.json(
          envelope({
            question: "Who owns platform reliability?",
            versions: [
              {
                knowledge_base_version_id: "KBV-1",
                knowledge_base_id: "KB-1",
                version_number: 1,
                knowledge_base_name: "Operations Corpus",
                chunking_strategy: "recursive",
                embedding_model: "sentence-transformers/all-MiniLM-L6-v2",
                retrieval_mode: "age_graph",
                answer: "Bob owns Platform reliability.",
                answer_error: null,
                citations: [],
                retrieved_chunks: [],
                graph_context: {
                  age_graph_name: "knowledge_graph",
                  age_status: "ok",
                },
                timing_ms: { total: 42 },
              },
            ],
          }),
        );
      }),
    );

    const user = userEvent.setup();
    render(<KnowledgeBases />);

    await openTo(user, "explore-chunks");
    await user.click(
      await screen.findByRole("button", { name: "Query with AGE" }),
    );

    await openExploreCompare(user);
    await openExploreAdvanced(user);
    expect(
      await screen.findByText("Compare answers across versions"),
    ).toBeInTheDocument();
    expect(screen.getByText("AGE graph mode selected")).toBeInTheDocument();
    expect(screen.getByText("Strict AGE path")).toBeInTheDocument();

    await user.type(
      screen.getByPlaceholderText(
        "Ask a question about the selected documents…",
      ),
      "Who owns platform reliability?",
    );
    await user.click(screen.getByRole("button", { name: "Ask" }));

    await waitFor(() =>
      expect(queryBody).toMatchObject({
        version_ids: ["KBV-1"],
        retrieval_modes: ["age_graph"],
        graph_overrides: {
          strict_age_retrieval: true,
        },
      }),
    );
    expect(await screen.findByText("Apache AGE context")).toBeInTheDocument();
    expect(
      screen.getByText("Bob owns Platform reliability."),
    ).toBeInTheDocument();
  });

  it("renders the graph explorer with AGE sync details and artifact entry points", async () => {
    const ageViewerUrl = "https://graphs.example.com/workbench";
    const bases = [knowledgeBase()];
    const kbVersion = version();

    server.use(
      http.get(`${KB}/options`, () =>
        HttpResponse.json(envelope(knowledgeOptions(true, ageViewerUrl))),
      ),
      http.get(`${KB}`, () => HttpResponse.json(envelope(bases))),
      http.get(`${OS}/buckets`, () =>
        HttpResponse.json(envelope([{ name: "reports", creation_date: NOW }])),
      ),
      http.get(`${OS}/buckets/reports/objects`, () =>
        HttpResponse.json(envelope(objectListing())),
      ),
      http.get(`${KB}/KB-1/versions`, () =>
        HttpResponse.json(envelope([kbVersion])),
      ),
      http.get(`${KB}/KB-1/runs`, () => HttpResponse.json(envelope([]))),
      http.get(`${API_BASE}/knowledge-base-versions/KBV-1/sources`, () =>
        HttpResponse.json(
          envelope([
            {
              knowledge_base_source_id: "SRC-1",
              knowledge_base_version_id: "KBV-1",
              document_id: "DOC-1",
              selection_kind: "file",
              bucket: "reports",
              object_key: "incident-playbook.md",
              object_name: "incident-playbook.md",
              object_store_path:
                "/object-store?bucket=reports&key=incident-playbook.md",
              content_type: "text/markdown",
              size_bytes: 512,
              etag: "etag-1",
              last_modified: NOW,
              extracted_chars: 210,
              extracted_format: "markdown",
              ocr_used: false,
              status: "processed",
              error_summary: null,
              source_metadata: null,
              created_at: NOW,
            },
          ]),
        ),
      ),
      http.get(`${API_BASE}/knowledge-base-versions/KBV-1/chunks`, () =>
        HttpResponse.json(envelope([])),
      ),
      http.get(`${API_BASE}/knowledge-base-versions/KBV-1/entities`, () =>
        HttpResponse.json(
          envelope([
            {
              knowledge_base_entity_id: "ENT-1",
              knowledge_base_version_id: "KBV-1",
              entity_key: "bob",
              label: "Bob",
              entity_type: "person",
              aliases: ["Bob"],
              mention_count: 4,
              source_documents: ["DOC-1"],
              source_keys: ["incident-playbook.md"],
              source_chunks: ["CH-1"],
              entity_metadata: {},
              created_at: NOW,
            },
            {
              knowledge_base_entity_id: "ENT-2",
              knowledge_base_version_id: "KBV-1",
              entity_key: "platform-reliability",
              label: "Platform reliability",
              entity_type: "concept",
              aliases: ["Platform reliability"],
              mention_count: 3,
              source_documents: ["DOC-1"],
              source_keys: ["incident-playbook.md"],
              source_chunks: ["CH-1"],
              entity_metadata: {},
              created_at: NOW,
            },
            {
              knowledge_base_entity_id: "ENT-3",
              knowledge_base_version_id: "KBV-1",
              entity_key: "alice",
              label: "Alice",
              entity_type: "person",
              aliases: ["Alice"],
              mention_count: 2,
              source_documents: ["DOC-1"],
              source_keys: ["incident-playbook.md"],
              source_chunks: ["CH-1"],
              entity_metadata: {},
              created_at: NOW,
            },
          ]),
        ),
      ),
      http.get(`${API_BASE}/knowledge-base-versions/KBV-1/relationships`, () =>
        HttpResponse.json(
          envelope([
            {
              knowledge_base_relationship_id: "REL-1",
              knowledge_base_version_id: "KBV-1",
              source_entity_id: "ENT-1",
              source_entity_key: "bob",
              source_entity_label: "Bob",
              target_entity_id: "ENT-2",
              target_entity_key: "platform-reliability",
              target_entity_label: "Platform reliability",
              relationship_type: "co_occurs",
              weight: 4,
              evidence_chunk_ids: ["CH-1"],
              source_documents: ["DOC-1"],
              relationship_metadata: {},
              created_at: NOW,
            },
            {
              knowledge_base_relationship_id: "REL-2",
              knowledge_base_version_id: "KBV-1",
              source_entity_id: "ENT-3",
              source_entity_key: "alice",
              source_entity_label: "Alice",
              target_entity_id: "ENT-1",
              target_entity_key: "bob",
              target_entity_label: "Bob",
              relationship_type: "co_occurs",
              weight: 2,
              evidence_chunk_ids: ["CH-1"],
              source_documents: ["DOC-1"],
              relationship_metadata: {},
              created_at: NOW,
            },
          ]),
        ),
      ),
      http.get(
        `${API_BASE}/knowledge-base-versions/KBV-1/graph`,
        ({ request }) => {
          const url = new URL(request.url);
          const source = (url.searchParams.get("source") ?? "local") as
            | "local"
            | "age";
          return HttpResponse.json(
            envelope(graphExplorer(source, url.searchParams.get("q") ?? "")),
          );
        },
      ),
    );

    const user = userEvent.setup();
    render(<KnowledgeBases />);

    await openTo(user, "explore-graph");

    expect(
      await screen.findByText("Inspect the version-scoped knowledge graph"),
    ).toBeInTheDocument();
    expect(
      screen.getByText(
        "Cypher-first retrieval is configured for this version.",
      ),
    ).toBeInTheDocument();
    expect(screen.getAllByText("One-hop expansion").length).toBeGreaterThan(0);
    expect(
      screen.getByRole("link", { name: "Open AGE Viewer" }),
    ).toHaveAttribute("href", ageViewerUrl);
    expect(
      screen.getByRole("link", { name: "graph.json" }),
    ).toBeInTheDocument();
    expect(
      screen.getByRole("link", { name: "entities.jsonl" }),
    ).toBeInTheDocument();
    expect(screen.getByTestId("knowledge-graph-canvas")).toHaveTextContent(
      "AGE graph neighborhood",
    );
    expect(
      screen.getByTestId("knowledge-graph-canvas-summary"),
    ).toHaveTextContent("knowledge_graph");
    expect(
      screen.getByTestId("knowledge-graph-canvas-legend"),
    ).toHaveTextContent("Seed match");
    await waitFor(() => {
      expect(screen.getByTestId("graph-focus-card")).toHaveTextContent("Bob");
      expect(screen.getByTestId("graph-focus-card")).toHaveTextContent(
        "Connected trails",
      );
    });
    expect(
      screen.getByTestId("knowledge-graph-canvas-trails"),
    ).toHaveTextContent("Platform reliability");

    await user.type(
      screen.getByRole("searchbox", { name: "Search graph" }),
      "Bob",
    );
    expect(screen.getAllByText("Bob").length).toBeGreaterThan(0);
    expect(screen.getAllByText("Platform reliability").length).toBeGreaterThan(
      0,
    );
    await user.click(
      screen.getByRole("button", {
        name: "Focus related entity Platform reliability",
      }),
    );
    expect(screen.getByTestId("graph-focus-card")).toHaveTextContent(
      "Platform reliability",
    );
    expect(
      screen.getByRole("link", { name: "incident-playbook.md" }),
    ).toBeInTheDocument();
  });

  it("keeps the AGE graph path visible and explains fallback when the version failed to sync", async () => {
    const bases = [knowledgeBase()];
    const failedVersion = {
      ...version(),
      summary: {
        ...version().summary,
        age_sync_status: "failed",
        age_sync_error: "age sync unavailable",
      },
    };

    server.use(
      http.get(`${KB}/options`, () =>
        HttpResponse.json(envelope(knowledgeOptions(true))),
      ),
      http.get(`${KB}`, () => HttpResponse.json(envelope(bases))),
      http.get(`${OS}/buckets`, () =>
        HttpResponse.json(envelope([{ name: "reports", creation_date: NOW }])),
      ),
      http.get(`${OS}/buckets/reports/objects`, () =>
        HttpResponse.json(envelope(objectListing())),
      ),
      http.get(`${KB}/KB-1/versions`, () =>
        HttpResponse.json(envelope([failedVersion])),
      ),
      http.get(`${KB}/KB-1/runs`, () => HttpResponse.json(envelope([]))),
      http.get(`${API_BASE}/knowledge-base-versions/KBV-1/sources`, () =>
        HttpResponse.json(envelope([])),
      ),
      http.get(`${API_BASE}/knowledge-base-versions/KBV-1/chunks`, () =>
        HttpResponse.json(envelope([])),
      ),
      http.get(`${API_BASE}/knowledge-base-versions/KBV-1/entities`, () =>
        HttpResponse.json(envelope([])),
      ),
      http.get(`${API_BASE}/knowledge-base-versions/KBV-1/relationships`, () =>
        HttpResponse.json(envelope([])),
      ),
      http.get(
        `${API_BASE}/knowledge-base-versions/KBV-1/graph`,
        ({ request }) => {
          const url = new URL(request.url);
          const source = (url.searchParams.get("source") ?? "local") as
            | "local"
            | "age";
          if (source === "age") {
            return HttpResponse.json(
              envelope({
                ...graphExplorer("local", url.searchParams.get("q") ?? ""),
                requested_source: "age",
                served_source: "local",
                age_ready: false,
                age_status: "failed",
                fallback_reason:
                  "This knowledge-base version did not finish syncing to Apache AGE.",
              }),
            );
          }
          return HttpResponse.json(
            envelope({
              ...graphExplorer("local", url.searchParams.get("q") ?? ""),
              age_ready: false,
              age_status: "failed",
              fallback_reason:
                "This knowledge-base version did not finish syncing to Apache AGE.",
            }),
          );
        },
      ),
    );

    const user = userEvent.setup();
    render(<KnowledgeBases />);

    await openTo(user, "explore-graph");
    expect(
      await screen.findByText("AGE sync failed for this version."),
    ).toBeInTheDocument();
    expect(screen.getByText(/the sync did not complete/i)).toBeInTheDocument();

    await openGraphAdvanced(user);
    const graphSource = screen.getByLabelText(
      "Graph source",
    ) as HTMLSelectElement;
    expect(graphSource.value).toBe("age");
    expect(
      Array.from(graphSource.options).map((option) => option.value),
    ).toEqual(["age", "local"]);
    expect(
      screen.getByText("AGE requested, served locally"),
    ).toBeInTheDocument();
    expect(
      screen.getByText(/currently serving the version-local graph instead/i),
    ).toBeInTheDocument();

    await user.click(
      screen.getByRole("button", { name: "Test AGE path in Playground" }),
    );
    await openExploreCompare(user);
    await openExploreAdvanced(user);
    expect(
      await screen.findByText("AGE graph mode selected"),
    ).toBeInTheDocument();
    expect(
      screen.getByText("0/1 selected versions AGE-ready"),
    ).toBeInTheDocument();
    expect(
      screen.getByText(
        /only versions with a successful apache age sync can use the graph-native retrieval path/i,
      ),
    ).toBeInTheDocument();
  });

  it("carries AGE graph explorer settings into the playground query", async () => {
    let lastGraphSeedMode = "";
    let queryBody: Record<string, unknown> | null = null;
    const bases = [knowledgeBase()];
    const kbVersion = version();

    server.use(
      http.get(`${KB}/options`, () =>
        HttpResponse.json(envelope(knowledgeOptions(true))),
      ),
      http.get(`${KB}`, () => HttpResponse.json(envelope(bases))),
      http.get(`${OS}/buckets`, () =>
        HttpResponse.json(envelope([{ name: "reports", creation_date: NOW }])),
      ),
      http.get(`${OS}/buckets/reports/objects`, () =>
        HttpResponse.json(envelope(objectListing())),
      ),
      http.get(`${KB}/KB-1/versions`, () =>
        HttpResponse.json(envelope([kbVersion])),
      ),
      http.get(`${KB}/KB-1/runs`, () => HttpResponse.json(envelope([]))),
      http.get(`${API_BASE}/knowledge-base-versions/KBV-1/sources`, () =>
        HttpResponse.json(envelope([])),
      ),
      http.get(`${API_BASE}/knowledge-base-versions/KBV-1/chunks`, () =>
        HttpResponse.json(envelope([])),
      ),
      http.get(`${API_BASE}/knowledge-base-versions/KBV-1/entities`, () =>
        HttpResponse.json(envelope([])),
      ),
      http.get(`${API_BASE}/knowledge-base-versions/KBV-1/relationships`, () =>
        HttpResponse.json(envelope([])),
      ),
      http.get(
        `${API_BASE}/knowledge-base-versions/KBV-1/graph`,
        ({ request }) => {
          const url = new URL(request.url);
          const source = (url.searchParams.get("source") ?? "local") as
            | "local"
            | "age";
          lastGraphSeedMode = url.searchParams.get("age_seed_mode") ?? "";
          return HttpResponse.json(
            envelope({
              ...graphExplorer(source, url.searchParams.get("q") ?? ""),
              age_seed_mode: lastGraphSeedMode || "entity_then_text",
              age_seed_strategy:
                lastGraphSeedMode === "query_text_only"
                  ? "query_text"
                  : "query_entities",
            }),
          );
        },
      ),
      http.post(QUERY, async ({ request }) => {
        queryBody = (await request.json()) as Record<string, unknown>;
        return HttpResponse.json(
          envelope({
            question: "What does Bob own?",
            versions: [
              {
                knowledge_base_version_id: "KBV-1",
                knowledge_base_id: "KB-1",
                version_number: 1,
                knowledge_base_name: "Operations Corpus",
                chunking_strategy: "recursive",
                embedding_model: "sentence-transformers/all-MiniLM-L6-v2",
                retrieval_mode: "age_graph",
                answer: "Bob owns platform reliability.",
                answer_error: null,
                citations: [],
                retrieved_chunks: [],
                graph_context: {
                  age_graph_name: "knowledge_graph",
                  age_status: "ok",
                  age_seed_strategy: "query_text",
                },
                timing_ms: { total: 42 },
              },
            ],
          }),
        );
      }),
    );

    const user = userEvent.setup();
    render(<KnowledgeBases />);

    await openTo(user, "explore-graph");
    expect(await screen.findByText("Graph Explorer")).toBeInTheDocument();

    await openGraphAdvanced(user);
    await user.selectOptions(
      screen.getByLabelText("Graph AGE seed mode"),
      "query_text_only",
    );
    await user.selectOptions(
      screen.getByLabelText("Graph traversal depth"),
      "2",
    );
    await user.selectOptions(
      screen.getByLabelText("Graph retrieval strength"),
      "aggressive",
    );
    fireEvent.change(
      screen.getByLabelText("Graph minimum relationship weight"),
      {
        target: { value: "2.5" },
      },
    );
    fireEvent.change(screen.getByLabelText("Graph AGE candidate pool"), {
      target: { value: "32" },
    });
    fireEvent.change(screen.getByLabelText("Graph AGE dense rerank weight"), {
      target: { value: "0.15" },
    });

    await waitFor(() => expect(lastGraphSeedMode).toBe("query_text_only"));
    await user.click(
      screen.getByLabelText("Require strict AGE retrieval in graph probe"),
    );

    await user.click(
      screen.getByRole("button", { name: "Use AGE in Playground" }),
    );
    await openExploreCompare(user);
    await openExploreAdvanced(user);
    expect(
      await screen.findByText("Compare answers across versions"),
    ).toBeInTheDocument();
    expect(screen.getByText("Custom retrieval override")).toBeInTheDocument();
    expect(screen.getByText("Strict AGE path")).toBeInTheDocument();

    await user.type(
      screen.getByPlaceholderText(
        "Ask a question about the selected documents…",
      ),
      "What does Bob own?",
    );
    await user.click(screen.getByRole("button", { name: "Ask" }));

    await waitFor(() =>
      expect(queryBody).toMatchObject({
        version_ids: ["KBV-1"],
        retrieval_modes: ["age_graph"],
        graph_overrides: {
          retrieval_strength: "aggressive",
          minimum_relationship_weight: 2.5,
          age_seed_mode: "query_text_only",
          age_traversal_hops: 2,
          age_candidate_pool_size: 32,
          age_dense_rerank_weight: 0.15,
        },
      }),
    );
    expect(
      await screen.findByText("Bob owns platform reliability."),
    ).toBeInTheDocument();
  });

  it("runs AGE-backed retrieval directly from the graph tab", async () => {
    let lastGraphSeedMode = "";
    let lastGraphStrict = false;
    let queryBody: Record<string, unknown> | null = null;
    const bases = [knowledgeBase()];
    const kbVersion = version();

    server.use(
      http.get(`${KB}/options`, () =>
        HttpResponse.json(envelope(knowledgeOptions(true))),
      ),
      http.get(`${KB}`, () => HttpResponse.json(envelope(bases))),
      http.get(`${OS}/buckets`, () =>
        HttpResponse.json(envelope([{ name: "reports", creation_date: NOW }])),
      ),
      http.get(`${OS}/buckets/reports/objects`, () =>
        HttpResponse.json(envelope(objectListing())),
      ),
      http.get(`${KB}/KB-1/versions`, () =>
        HttpResponse.json(envelope([kbVersion])),
      ),
      http.get(`${KB}/KB-1/runs`, () => HttpResponse.json(envelope([]))),
      http.get(`${API_BASE}/knowledge-base-versions/KBV-1/sources`, () =>
        HttpResponse.json(envelope([])),
      ),
      http.get(`${API_BASE}/knowledge-base-versions/KBV-1/chunks`, () =>
        HttpResponse.json(envelope([])),
      ),
      http.get(`${API_BASE}/knowledge-base-versions/KBV-1/entities`, () =>
        HttpResponse.json(envelope([])),
      ),
      http.get(`${API_BASE}/knowledge-base-versions/KBV-1/relationships`, () =>
        HttpResponse.json(envelope([])),
      ),
      http.get(
        `${API_BASE}/knowledge-base-versions/KBV-1/graph`,
        ({ request }) => {
          const url = new URL(request.url);
          const source = (url.searchParams.get("source") ?? "local") as
            | "local"
            | "age";
          lastGraphSeedMode = url.searchParams.get("age_seed_mode") ?? "";
          lastGraphStrict =
            url.searchParams.get("strict_age_retrieval") === "true";
          return HttpResponse.json(
            envelope({
              ...graphExplorer(source, url.searchParams.get("q") ?? ""),
              age_seed_mode: lastGraphSeedMode || "entity_then_text",
              strict_age_retrieval: lastGraphStrict,
              age_seed_strategy:
                lastGraphSeedMode === "query_text_only"
                  ? "query_text"
                  : "query_entities",
            }),
          );
        },
      ),
      http.post(QUERY, async ({ request }) => {
        queryBody = (await request.json()) as Record<string, unknown>;
        return HttpResponse.json(
          envelope({
            question: "What does Bob own?",
            versions: [
              {
                knowledge_base_version_id: "KBV-1",
                knowledge_base_id: "KB-1",
                version_number: 1,
                knowledge_base_name: "Operations Corpus",
                chunking_strategy: "recursive",
                embedding_model: "sentence-transformers/all-MiniLM-L6-v2",
                retrieval_mode: "age_graph",
                answer: "Bob owns platform reliability.",
                answer_error: null,
                citations: [
                  {
                    chunk_id: "CH-1",
                    label: "[1] incident-playbook.md",
                    source_bucket: "reports",
                    source_key: "incident-playbook.md",
                    object_store_path:
                      "/object-store?bucket=reports&key=incident-playbook.md",
                    score: 0.91,
                  },
                ],
                retrieved_chunks: [
                  {
                    chunk_id: "CH-1",
                    source_bucket: "reports",
                    source_key: "incident-playbook.md",
                    source_name: "incident-playbook.md",
                    score: 0.91,
                    content: "Bob owns Platform reliability.",
                    chunk_index: 0,
                    ordinal: 0,
                    document_id: "DOC-1",
                    metadata: {},
                    object_store_path:
                      "/object-store?bucket=reports&key=incident-playbook.md",
                    score_breakdown: {
                      dense: 0.45,
                      dense_rerank: 0.16,
                      graph_boost: 0.46,
                      age_graph: 4.1,
                    },
                    matched_entity_labels: ["Bob", "Platform reliability"],
                  },
                ],
                graph_context: {
                  age_graph_name: "knowledge_graph",
                  age_status: "ok",
                  age_seed_strategy: "query_text",
                  age_traversal_hops: 2,
                  age_matched_chunk_count: 5,
                  age_candidate_pool_size: 24,
                  age_dense_rerank_weight: 0.35,
                  matched_entities: ["Bob"],
                  expanded_entities: ["Platform reliability"],
                  boosted_chunk_count: 1,
                  strict_age_retrieval: true,
                },
                timing_ms: { total: 48 },
              },
            ],
          }),
        );
      }),
    );

    const user = userEvent.setup();
    render(<KnowledgeBases />);

    await openTo(user, "explore-graph");
    expect(
      await screen.findByText("Graph Retrieval Probe"),
    ).toBeInTheDocument();
    expect(screen.getByText("Served from Apache AGE")).toBeInTheDocument();

    await openGraphAdvanced(user);
    await user.selectOptions(
      screen.getByLabelText("Graph AGE seed mode"),
      "query_text_only",
    );
    await user.click(
      screen.getByLabelText("Require strict AGE source in graph explorer"),
    );
    await user.selectOptions(
      screen.getByLabelText("Graph traversal depth"),
      "2",
    );
    await user.selectOptions(
      screen.getByLabelText("Graph retrieval strength"),
      "aggressive",
    );
    fireEvent.change(
      screen.getByLabelText("Graph minimum relationship weight"),
      {
        target: { value: "2.5" },
      },
    );
    fireEvent.change(screen.getByLabelText("Graph AGE candidate pool"), {
      target: { value: "32" },
    });
    fireEvent.change(screen.getByLabelText("Graph AGE dense rerank weight"), {
      target: { value: "0.15" },
    });

    await waitFor(() => expect(lastGraphSeedMode).toBe("query_text_only"));
    await waitFor(() => expect(lastGraphStrict).toBe(true));

    await user.type(
      screen.getByLabelText("Graph retrieval question"),
      "What does Bob own?",
    );
    expect(
      screen.getByLabelText("Require strict AGE retrieval in graph probe"),
    ).toBeChecked();
    await user.click(screen.getByRole("button", { name: "Run AGE retrieval" }));

    await waitFor(() =>
      expect(queryBody).toMatchObject({
        version_ids: ["KBV-1"],
        question: "What does Bob own?",
        top_k: 4,
        retrieval_modes: ["age_graph"],
        graph_overrides: {
          retrieval_strength: "aggressive",
          minimum_relationship_weight: 2.5,
          age_seed_mode: "query_text_only",
          age_traversal_hops: 2,
          age_candidate_pool_size: 32,
          age_dense_rerank_weight: 0.15,
          strict_age_retrieval: true,
        },
      }),
    );
    expect(await screen.findByTestId("graph-probe-result")).toHaveTextContent(
      "Bob owns platform reliability.",
    );
    expect(screen.getByText("Apache AGE context")).toBeInTheDocument();
    expect(
      screen.getByRole("link", { name: "[1] incident-playbook.md" }),
    ).toBeInTheDocument();
  });

  it("seeds strict AGE defaults into the graph explorer and playground", async () => {
    let lastGraphStrict = false;
    const bases = [knowledgeBase()];
    const strictVersion = {
      ...version(),
      graph_config: {
        ...version().graph_config,
        retrieval_strength: "aggressive" as const,
        age_seed_mode: "query_entities_and_text" as const,
        age_traversal_hops: 2,
        age_candidate_pool_size: 40,
        age_dense_rerank_weight: 0.2,
        strict_age_retrieval_default: true,
      },
      graph_profile_id: "age_strict",
      graph_profile_label: "Strict AGE default",
      summary: {
        ...version().summary,
        graph_profile_id: "age_strict",
        graph_profile_label: "Strict AGE default",
      },
    };

    server.use(
      http.get(`${KB}/options`, () =>
        HttpResponse.json(envelope(knowledgeOptions(true))),
      ),
      http.get(`${KB}`, () => HttpResponse.json(envelope(bases))),
      http.get(`${OS}/buckets`, () =>
        HttpResponse.json(envelope([{ name: "reports", creation_date: NOW }])),
      ),
      http.get(`${OS}/buckets/reports/objects`, () =>
        HttpResponse.json(envelope(objectListing())),
      ),
      http.get(`${KB}/KB-1/versions`, () =>
        HttpResponse.json(envelope([strictVersion])),
      ),
      http.get(`${KB}/KB-1/runs`, () => HttpResponse.json(envelope([]))),
      http.get(`${API_BASE}/knowledge-base-versions/KBV-1/sources`, () =>
        HttpResponse.json(envelope([])),
      ),
      http.get(`${API_BASE}/knowledge-base-versions/KBV-1/chunks`, () =>
        HttpResponse.json(envelope([])),
      ),
      http.get(`${API_BASE}/knowledge-base-versions/KBV-1/entities`, () =>
        HttpResponse.json(envelope([])),
      ),
      http.get(`${API_BASE}/knowledge-base-versions/KBV-1/relationships`, () =>
        HttpResponse.json(envelope([])),
      ),
      http.get(
        `${API_BASE}/knowledge-base-versions/KBV-1/graph`,
        ({ request }) => {
          const url = new URL(request.url);
          const source = (url.searchParams.get("source") ?? "local") as
            | "local"
            | "age";
          lastGraphStrict =
            url.searchParams.get("strict_age_retrieval") === "true";
          return HttpResponse.json(
            envelope({
              ...graphExplorer(source, url.searchParams.get("q") ?? ""),
              strict_age_retrieval: lastGraphStrict,
            }),
          );
        },
      ),
    );

    const user = userEvent.setup();
    render(<KnowledgeBases />);

    await openTo(user, "explore-graph");
    expect(await screen.findByText("Graph Explorer")).toBeInTheDocument();
    await waitFor(() => expect(lastGraphStrict).toBe(true));
    await openGraphAdvanced(user);
    expect(
      screen.getByLabelText("Require strict AGE source in graph explorer"),
    ).toBeChecked();
    expect(
      screen.getByLabelText("Require strict AGE retrieval in graph probe"),
    ).toBeChecked();

    await gotoExplore(user, "ask");
    await openExploreCompare(user);
    await openExploreAdvanced(user);
    expect(
      await screen.findByText("Compare answers across versions"),
    ).toBeInTheDocument();
    expect(screen.getByText("Following build default")).toBeInTheDocument();
    expect(screen.getByText("Saved strict AGE default")).toBeInTheDocument();
  });

  it("applies graph query presets before running AGE-backed retrieval", async () => {
    let queryBody: Record<string, unknown> | null = null;
    server.use(
      http.get(`${KB}/options`, () =>
        HttpResponse.json(envelope(knowledgeOptions(true))),
      ),
      http.get(`${KB}`, () => HttpResponse.json(envelope([knowledgeBase()]))),
      http.get(`${OS}/buckets`, () =>
        HttpResponse.json(envelope([{ name: "reports", creation_date: NOW }])),
      ),
      http.get(`${OS}/buckets/reports/objects`, () =>
        HttpResponse.json(envelope(objectListing())),
      ),
      http.get(`${KB}/KB-1/versions`, () =>
        HttpResponse.json(envelope([version()])),
      ),
      http.get(`${KB}/KB-1/runs`, () => HttpResponse.json(envelope([]))),
      http.get(`${API_BASE}/knowledge-base-versions/KBV-1/sources`, () =>
        HttpResponse.json(envelope([])),
      ),
      http.get(`${API_BASE}/knowledge-base-versions/KBV-1/chunks`, () =>
        HttpResponse.json(envelope([])),
      ),
      http.get(`${API_BASE}/knowledge-base-versions/KBV-1/entities`, () =>
        HttpResponse.json(envelope([])),
      ),
      http.get(`${API_BASE}/knowledge-base-versions/KBV-1/relationships`, () =>
        HttpResponse.json(envelope([])),
      ),
      http.get(
        `${API_BASE}/knowledge-base-versions/KBV-1/graph`,
        ({ request }) => {
          const url = new URL(request.url);
          const source = (url.searchParams.get("source") ?? "local") as
            | "local"
            | "age";
          return HttpResponse.json(
            envelope(graphExplorer(source, url.searchParams.get("q") ?? "")),
          );
        },
      ),
      http.post(QUERY, async ({ request }) => {
        queryBody = (await request.json()) as Record<string, unknown>;
        return HttpResponse.json(
          envelope({
            question: "What does Bob own?",
            versions: [
              {
                knowledge_base_version_id: "KBV-1",
                knowledge_base_id: "KB-1",
                version_number: 1,
                knowledge_base_name: "Operations Corpus",
                chunking_strategy: "recursive",
                embedding_model: "sentence-transformers/all-MiniLM-L6-v2",
                retrieval_mode: "age_graph",
                answer: "Bob owns platform reliability.",
                answer_error: null,
                citations: [],
                retrieved_chunks: [],
                graph_context: {
                  age_graph_name: "knowledge_graph",
                  age_status: "ok",
                },
                timing_ms: { total: 41 },
              },
            ],
          }),
        );
      }),
    );

    const user = userEvent.setup();
    render(<KnowledgeBases />);

    await openTo(user, "explore-graph");
    expect(await screen.findByText("Graph query profiles")).toBeInTheDocument();

    // Presets stay on the main flow; the knobs they drive are under Advanced.
    await user.click(
      screen.getByTestId("kb-graph-query-preset-hybrid_precision"),
    );
    await openGraphAdvanced(user);
    expect(
      (screen.getByLabelText("Graph source") as HTMLSelectElement).value,
    ).toBe("local");
    expect(
      (screen.getByLabelText("Graph retrieval strength") as HTMLSelectElement)
        .value,
    ).toBe("conservative");
    expect(
      (screen.getByLabelText("Graph traversal depth") as HTMLSelectElement)
        .value,
    ).toBe("0");
    expect(
      (
        screen.getByLabelText(
          "Graph minimum relationship weight",
        ) as HTMLInputElement
      ).value,
    ).toBe("2");

    await user.click(screen.getByTestId("kb-graph-query-preset-age_strict"));
    expect(
      (screen.getByLabelText("Graph source") as HTMLSelectElement).value,
    ).toBe("age");
    expect(
      (screen.getByLabelText("Graph retrieval strength") as HTMLSelectElement)
        .value,
    ).toBe("aggressive");
    expect(
      (screen.getByLabelText("Graph AGE seed mode") as HTMLSelectElement).value,
    ).toBe("query_entities_and_text");
    expect(
      (screen.getByLabelText("Graph traversal depth") as HTMLSelectElement)
        .value,
    ).toBe("2");
    expect(
      (screen.getByLabelText("Graph AGE candidate pool") as HTMLInputElement)
        .value,
    ).toBe("40");
    expect(
      (
        screen.getByLabelText(
          "Graph AGE dense rerank weight",
        ) as HTMLInputElement
      ).value,
    ).toBe("0.2");
    expect(
      screen.getByLabelText("Require strict AGE source in graph explorer"),
    ).toBeChecked();

    await user.type(
      screen.getByLabelText("Graph retrieval question"),
      "What does Bob own?",
    );
    await user.click(screen.getByRole("button", { name: "Run AGE retrieval" }));

    await waitFor(() =>
      expect(queryBody).toMatchObject({
        retrieval_modes: ["age_graph"],
        graph_overrides: {
          retrieval_strength: "aggressive",
          minimum_relationship_weight: 1,
          age_seed_mode: "query_entities_and_text",
          age_traversal_hops: 2,
          age_candidate_pool_size: 40,
          age_dense_rerank_weight: 0.2,
          strict_age_retrieval: true,
        },
      }),
    );
  });

  // ── Calibrate tab (Phase K2): retrieval-quality test runs ───────────────
  it("runs a calibration and renders the aggregate metric cards + per-question rows", async () => {
    const bases = [knowledgeBase()];
    let calibrateBody: Record<string, unknown> = {};

    server.use(
      ...calibrateBaseHandlers(bases, [version()]),
      http.get(`${KB}/KB-1/test-runs`, () => HttpResponse.json(envelope([]))),
      http.post(`${KB}/KB-1/calibrate`, async ({ request }) => {
        calibrateBody = (await request.json().catch(() => ({}))) as Record<
          string,
          unknown
        >;
        return HttpResponse.json(envelope(calibrationSummary()), {
          status: 201,
        });
      }),
      http.get(`${API_BASE}/knowledge/test-runs/KBT-1`, () =>
        HttpResponse.json(envelope(calibrationDetail())),
      ),
    );

    const user = userEvent.setup();
    render(<KnowledgeBases />);

    await openTo(user, "calibrate");
    expect(await screen.findByTestId("kb-calibrate-config")).toBeInTheDocument();

    // Choose mode + top-k, then run.
    fireEvent.change(screen.getByTestId("kb-calibrate-mode"), {
      target: { value: "hybrid" },
    });
    fireEvent.change(screen.getByTestId("kb-calibrate-topk"), {
      target: { value: "8" },
    });
    await user.click(screen.getByTestId("kb-calibrate-run"));

    // Calibrate called with the chosen version/dataset/mode/top_k.
    await waitFor(() =>
      expect(calibrateBody).toMatchObject({
        version_id: "KBV-1",
        eval_dataset_id: "DS-1",
        retrieval_mode: "hybrid",
        top_k: 8,
      }),
    );

    // Aggregate metric cards render the returned values.
    const cards = await screen.findByTestId("kb-calibrate-metric-cards");
    expect(cards).toBeInTheDocument();
    expect(screen.getByTestId("kb-calibrate-metric-recall_at_k")).toHaveTextContent(
      "75%",
    );
    expect(
      screen.getByTestId("kb-calibrate-metric-answer_correctness"),
    ).toHaveTextContent("80%");

    // Per-question rows render from the run detail.
    const rows = await screen.findAllByTestId("kb-calibrate-question-row");
    expect(rows).toHaveLength(2);
    expect(
      screen.getByText("How do we restart the ingestion worker?"),
    ).toBeInTheDocument();
  });

  it("sets a run as baseline and shows metric deltas + a regression vs. the baseline", async () => {
    // KB already has KBT-0 pinned as baseline; the new run KBT-1 regresses Q2.
    const bases = [{ ...knowledgeBase(), baseline_run_id: "KBT-0" }];

    server.use(
      ...calibrateBaseHandlers(bases, [version()]),
      http.get(`${KB}/KB-1/test-runs`, () =>
        HttpResponse.json(
          envelope([
            calibrationSummary(),
            calibrationSummary({
              test_run_id: "KBT-0",
              metrics: { ...baselineDetail().metrics },
            }),
          ]),
        ),
      ),
      http.post(`${KB}/KB-1/calibrate`, () =>
        HttpResponse.json(envelope(calibrationSummary()), { status: 201 }),
      ),
      http.post(`${KB}/KB-1/baseline`, async ({ request }) => {
        const body = (await request.json().catch(() => ({}))) as Record<
          string,
          unknown
        >;
        return HttpResponse.json(
          envelope({
            knowledge_base_id: "KB-1",
            baseline_run_id: String(body.test_run_id ?? "KBT-1"),
          }),
        );
      }),
      http.get(`${API_BASE}/knowledge/test-runs/KBT-1`, () =>
        HttpResponse.json(envelope(calibrationDetail())),
      ),
      http.get(`${API_BASE}/knowledge/test-runs/KBT-0`, () =>
        HttpResponse.json(envelope(baselineDetail())),
      ),
    );

    const user = userEvent.setup();
    render(<KnowledgeBases />);

    await openTo(user, "calibrate");

    // KBT-1 is the latest viewed run; baseline KBT-0 is pinned → compare shows.
    const comparison = await screen.findByTestId("kb-calibrate-comparison");
    expect(comparison).toBeInTheDocument();

    // Metric delta for answer-correctness: 0.8 (candidate) − 0.9 (baseline).
    expect(
      screen.getByTestId("kb-calibrate-delta-answer_correctness"),
    ).toHaveTextContent("-10%");

    // Q2 dropped from pass→partial: shows up as a regression row.
    const regressions = await screen.findAllByTestId(
      "kb-calibrate-regression-row",
    );
    expect(regressions).toHaveLength(1);
    expect(regressions[0]).toHaveTextContent(
      "What is the on-call escalation path?",
    );

    // Now pin KBT-1 as the new baseline.
    let baselineCalled = false;
    server.use(
      http.post(`${KB}/KB-1/baseline`, async ({ request }) => {
        baselineCalled = true;
        const body = (await request.json().catch(() => ({}))) as Record<
          string,
          unknown
        >;
        expect(body).toMatchObject({ test_run_id: "KBT-1" });
        return HttpResponse.json(
          envelope({ knowledge_base_id: "KB-1", baseline_run_id: "KBT-1" }),
        );
      }),
    );
    await user.click(screen.getByTestId("kb-calibrate-set-baseline"));
    await waitFor(() => expect(baselineCalled).toBe(true));
    // KBT-1 is now the baseline → its own card shows the baseline marker.
    expect(
      await screen.findByTestId("kb-calibrate-baseline-marker"),
    ).toBeInTheDocument();
  });

  it("lists past calibration runs and loads detail when one is selected", async () => {
    const bases = [knowledgeBase()];

    server.use(
      ...calibrateBaseHandlers(bases, [version()]),
      http.get(`${KB}/KB-1/test-runs`, () =>
        HttpResponse.json(
          envelope([
            calibrationSummary({ test_run_id: "KBT-2", retrieval_mode: "hybrid" }),
            calibrationSummary({ test_run_id: "KBT-1" }),
          ]),
        ),
      ),
      http.get(`${API_BASE}/knowledge/test-runs/KBT-2`, () =>
        HttpResponse.json(
          envelope(calibrationDetail({ test_run_id: "KBT-2" })),
        ),
      ),
      http.get(`${API_BASE}/knowledge/test-runs/KBT-1`, () =>
        HttpResponse.json(envelope(calibrationDetail())),
      ),
    );

    const user = userEvent.setup();
    render(<KnowledgeBases />);

    await openTo(user, "calibrate");

    // History lists both runs.
    const historyRows = await screen.findAllByTestId(
      "kb-calibrate-history-row",
    );
    expect(historyRows).toHaveLength(2);

    // Selecting the second run loads its detail (metric cards render).
    await user.click(historyRows[1]!);
    await waitFor(() =>
      expect(
        screen.getByTestId("kb-calibrate-metric-faithfulness"),
      ).toHaveTextContent("90%"),
    );
  });
});

/**
 * Asset-workspace IA (Phase R1): Library landing ↔ per-KB Workspace shell.
 * The handlers below back a fully-loaded KB-1 so the Workspace can resolve its
 * header facts, versions, graph, chunks, and runs.
 */
function workspaceHandlers(bases: unknown[], kbVersions: unknown[]) {
  return [
    http.get(`${KB}/options`, () =>
      HttpResponse.json(envelope(knowledgeOptions(true))),
    ),
    http.get(`${KB}`, () => HttpResponse.json(envelope(bases))),
    http.get(`${KB}/KB-1`, () => HttpResponse.json(envelope(bases[0]))),
    http.get(`${OS}/buckets`, () =>
      HttpResponse.json(envelope([{ name: "reports", creation_date: NOW }])),
    ),
    http.get(`${OS}/buckets/reports/objects`, () =>
      HttpResponse.json(envelope(objectListing())),
    ),
    http.get(`${KB}/KB-1/versions`, () =>
      HttpResponse.json(envelope(kbVersions)),
    ),
    http.get(`${KB}/KB-1/runs`, () => HttpResponse.json(envelope([run()]))),
    http.get(`${API_BASE}/knowledge-runs/KBR-1/events`, () =>
      HttpResponse.json(envelope([])),
    ),
    http.get(`${API_BASE}/knowledge-base-versions/KBV-1/sources`, () =>
      HttpResponse.json(envelope([])),
    ),
    http.get(`${API_BASE}/knowledge-base-versions/KBV-1/chunks`, () =>
      HttpResponse.json(envelope([])),
    ),
    http.get(`${API_BASE}/knowledge-base-versions/KBV-1/entities`, () =>
      HttpResponse.json(envelope([])),
    ),
    http.get(`${API_BASE}/knowledge-base-versions/KBV-1/relationships`, () =>
      HttpResponse.json(envelope([])),
    ),
    http.get(`${API_BASE}/knowledge-base-versions/KBV-1/graph`, ({ request }) => {
      const url = new URL(request.url);
      const source = (url.searchParams.get("source") ?? "local") as
        | "local"
        | "age";
      return HttpResponse.json(
        envelope(graphExplorer(source, url.searchParams.get("q") ?? "")),
      );
    }),
  ];
}

describe("Knowledge Bases asset workspace (R1 IA)", () => {
  it("opens a KB into a workspace with a header and four stage tabs, then returns via Back", async () => {
    server.use(...workspaceHandlers([knowledgeBase()], [version()]));

    const user = userEvent.setup();
    render(<KnowledgeBases />);

    // Landing: the flat tab bar is gone; only the KB cards + filters remain.
    await screen.findByTestId("kb-card-KB-1");
    expect(
      screen.queryByTestId("kb-workspace-header"),
    ).not.toBeInTheDocument();

    // Open the KB → workspace header surfaces name + status + version switcher.
    await openKnowledgeBaseWorkspace(user);
    const header = screen.getByTestId("kb-workspace-header");
    expect(header).toHaveTextContent("Operations Corpus");
    expect(screen.getByTestId("kb-workspace-status")).toHaveTextContent(
      "active",
    );
    expect(
      screen.getByTestId("kb-workspace-version-switcher"),
    ).toBeInTheDocument();

    // The four stages are present.
    for (const stage of ["Build", "Explore", "Calibrate", "Use"]) {
      expect(screen.getByRole("button", { name: stage })).toBeInTheDocument();
    }

    // Back returns to the Library landing.
    await user.click(screen.getByTestId("kb-workspace-back"));
    await screen.findByTestId("kb-card-KB-1");
    expect(
      screen.queryByTestId("kb-workspace-header"),
    ).not.toBeInTheDocument();
  });

  it("renders each stage's content: Build form+runs, Explore query/chunks/graph, Calibrate, Use", async () => {
    server.use(
      ...workspaceHandlers([knowledgeBase()], [version()]),
      http.get(`${API_BASE}/eval-datasets`, () =>
        HttpResponse.json(envelope([evalDataset()])),
      ),
    );

    const user = userEvent.setup();
    render(<KnowledgeBases />);

    // Build stage shows the build config form AND the build run history together.
    await openTo(user, "build");
    expect(await screen.findByText("Select files or folders")).toBeInTheDocument();
    // R2: the GraphRAG/embedding config starts collapsed; opening Advanced reveals it.
    expect(screen.queryByText("GraphRAG Outputs")).not.toBeInTheDocument();
    await openBuildAdvanced(user);
    expect(await screen.findByText("GraphRAG Outputs")).toBeInTheDocument();
    expect(screen.getByText("Build Runs")).toBeInTheDocument();

    // Explore → Query: the simplified default ask view (compare panel is behind
    // its disclosure now).
    await gotoExplore(user, "ask");
    expect(await screen.findByTestId("kb-explore-ask")).toBeInTheDocument();
    expect(
      screen.getByPlaceholderText("Ask a question about the selected documents…"),
    ).toBeInTheDocument();

    // Explore → Chunks (chunk browser).
    await gotoExplore(user, "chunks");
    expect(await screen.findByText("Chunk Browser")).toBeInTheDocument();

    // Explore → Graph (entity viz).
    await gotoExplore(user, "graph");
    expect(await screen.findByText("Graph Explorer")).toBeInTheDocument();

    // Calibrate stage.
    await gotoStage(user, "Calibrate");
    expect(await screen.findByTestId("kb-calibrate-config")).toBeInTheDocument();

    // Use stage: the version table + the usage panel.
    await gotoStage(user, "Use");
    expect(await screen.findByText("Version History")).toBeInTheDocument();
    expect(screen.getByTestId("kb-use-usage")).toHaveTextContent(
      "Query via API",
    );
  });

  it("activates an earlier version from the Use stage table", async () => {
    let activated: string | null = null;
    const v1 = version();
    const v2 = {
      ...version(),
      knowledge_base_version_id: "KBV-2",
      version_number: 2,
    };
    // KB whose active version is v2 → v1 is the inactive one we can activate.
    const base = { ...knowledgeBase(), active_version_id: "KBV-2" };

    server.use(
      ...workspaceHandlers([base], [v2, v1]),
      http.post(`${KB}/KB-1/versions/KBV-1/activate`, () => {
        activated = "KBV-1";
        return HttpResponse.json(envelope({ ...base, active_version_id: "KBV-1" }));
      }),
    );

    const user = userEvent.setup();
    render(<KnowledgeBases />);

    await openTo(user, "use");
    const activateButton = await screen.findByRole("button", {
      name: "Activate",
    });
    await user.click(activateButton);
    await waitFor(() => expect(activated).toBe("KBV-1"));
  });

  it("switches the working version from the header and exposes Set active", async () => {
    let activated: string | null = null;
    const v1 = version();
    const v2 = {
      ...version(),
      knowledge_base_version_id: "KBV-2",
      version_number: 2,
    };
    const base = { ...knowledgeBase(), active_version_id: "KBV-1" };

    server.use(
      ...workspaceHandlers([base], [v1, v2]),
      http.get(`${API_BASE}/knowledge-base-versions/KBV-2/sources`, () =>
        HttpResponse.json(envelope([])),
      ),
      http.get(`${API_BASE}/knowledge-base-versions/KBV-2/chunks`, () =>
        HttpResponse.json(envelope([])),
      ),
      http.post(`${KB}/KB-1/versions/KBV-2/activate`, () => {
        activated = "KBV-2";
        return HttpResponse.json(envelope({ ...base, active_version_id: "KBV-2" }));
      }),
    );

    const user = userEvent.setup();
    render(<KnowledgeBases />);

    await openKnowledgeBaseWorkspace(user);
    const switcher = (await screen.findByTestId(
      "kb-workspace-version-switcher",
    )) as HTMLSelectElement;
    // Defaults to the active version (v1); no Set active needed yet.
    await waitFor(() => expect(switcher.value).toBe("KBV-1"));
    expect(
      screen.queryByTestId("kb-workspace-set-active"),
    ).not.toBeInTheDocument();

    // Switch to v2 → Set active appears; clicking it activates v2.
    await user.selectOptions(switcher, "KBV-2");
    const setActive = await screen.findByTestId("kb-workspace-set-active");
    await user.click(setActive);
    await waitFor(() => expect(activated).toBe("KBV-2"));
  });
});

describe("Knowledge Bases R2 declutter", () => {
  it("keeps the Build advanced configuration collapsed by default and expands it on demand", async () => {
    server.use(...workspaceHandlers([knowledgeBase()], [version()]));

    const user = userEvent.setup();
    render(<KnowledgeBases />);

    await openTo(user, "build");

    // Simple controls stay on the main flow.
    expect(
      await screen.findByText("Select files or folders"),
    ).toBeInTheDocument();
    expect(screen.getByText("Quick graph preset")).toBeInTheDocument();

    // The advanced disclosure is present but collapsed: its controls are absent.
    const advanced = screen.getByTestId("kb-build-advanced");
    const toggle = within(advanced).getByRole("button", {
      name: /Advanced configuration/,
    });
    expect(toggle).toHaveAttribute("aria-expanded", "false");
    expect(screen.queryByText("GraphRAG Outputs")).not.toBeInTheDocument();
    expect(screen.queryByLabelText("Chunk size")).not.toBeInTheDocument();
    expect(
      screen.queryByRole("combobox", { name: /Graph sync target/ }),
    ).not.toBeInTheDocument();

    // Expanding reveals the full chunking + GraphRAG / AGE configuration.
    await user.click(toggle);
    expect(toggle).toHaveAttribute("aria-expanded", "true");
    expect(await screen.findByText("GraphRAG Outputs")).toBeInTheDocument();
    expect(screen.getByLabelText("Chunk size")).toBeInTheDocument();
    expect(
      screen.getByRole("combobox", { name: /Graph sync target/ }),
    ).toBeInTheDocument();
  });

  it("shows the build pipeline stepper for an in-progress run", async () => {
    const runningRun = {
      ...run(),
      status: "running",
      completed_at: null,
    };
    server.use(
      // Overrides first: within one server.use() call the earliest match wins.
      http.get(`${KB}/KB-1/runs`, () =>
        HttpResponse.json(envelope([runningRun])),
      ),
      // Real builder events: sources processed, graph built — Index/Ready pending.
      http.get(`${API_BASE}/knowledge-runs/KBR-1/events`, () =>
        HttpResponse.json(
          envelope([
            {
              event_id: 1,
              knowledge_base_run_id: "KBR-1",
              sequence: 1,
              event_type: "build_started",
              payload: null,
              created_at: NOW,
            },
            {
              event_id: 2,
              knowledge_base_run_id: "KBR-1",
              sequence: 2,
              event_type: "source_completed",
              payload: null,
              created_at: NOW,
            },
            {
              event_id: 3,
              knowledge_base_run_id: "KBR-1",
              sequence: 3,
              event_type: "graph_built",
              payload: null,
              created_at: NOW,
            },
          ]),
        ),
      ),
      ...workspaceHandlers([knowledgeBase()], [version()]),
    );

    const user = userEvent.setup();
    render(<KnowledgeBases />);

    await openTo(user, "build");

    // The compact stepper replaces the raw event list as the primary feedback.
    const stepper = await screen.findByTestId("kb-build-stepper");
    expect(stepper).toBeInTheDocument();
    // Queued/Extract/Chunk/Embed are done once sources finish + the graph built;
    // Index is the active step; Ready is still pending.
    await waitFor(() =>
      expect(screen.getByTestId("kb-build-step-graph")).toHaveAttribute(
        "data-state",
        "done",
      ),
    );
    expect(screen.getByTestId("kb-build-step-queued")).toHaveAttribute(
      "data-state",
      "done",
    );
    expect(screen.getByTestId("kb-build-step-index")).toHaveAttribute(
      "data-state",
      "active",
    );
    expect(screen.getByTestId("kb-build-step-ready")).toHaveAttribute(
      "data-state",
      "pending",
    );

    // The raw event log is still reachable, now behind a "View log" disclosure.
    expect(screen.getByTestId("kb-run-log")).toBeInTheDocument();
    expect(screen.queryByText("#1 · build_started")).not.toBeInTheDocument();
    await user.click(
      within(screen.getByTestId("kb-run-log")).getByRole("button", {
        name: /View log/,
      }),
    );
    expect(
      await screen.findByText("#1 · build_started"),
    ).toBeInTheDocument();
  });

  it("renders a calm library card with a single health line and no badge wall", async () => {
    server.use(...workspaceHandlers([knowledgeBase()], [version()]));

    render(<KnowledgeBases />);

    await screen.findByTestId("kb-card-KB-1");
    // The one health signal + facts line replace the old multi-badge wall.
    expect(screen.getByTestId("kb-card-health-KB-1")).toHaveTextContent(
      "3 chunks · 2 entities",
    );
    expect(screen.getByTestId("kb-card-facts-KB-1")).toHaveTextContent(
      "1 source",
    );

    // The removed badges / stat-box grid no longer render on the card.
    const card = screen.getByTestId("kb-card-KB-1");
    expect(within(card).queryByText("Active graph pulse")).not.toBeInTheDocument();
    expect(within(card).queryByText(/^Last run/)).not.toBeInTheDocument();
    expect(within(card).queryByText("Graph profile")).not.toBeInTheDocument();
    expect(within(card).queryByText("Owner")).not.toBeInTheDocument();
  });

  it("shows a building… health signal on the card while the active version builds", async () => {
    const buildingBase = {
      ...knowledgeBase(),
      active_version_summary: {
        ...knowledgeBase().active_version_summary,
        status: "processing",
      },
    };
    server.use(...workspaceHandlers([buildingBase], [version()]));

    render(<KnowledgeBases />);

    await screen.findByTestId("kb-card-KB-1");
    expect(screen.getByTestId("kb-card-health-KB-1")).toHaveTextContent(
      "building…",
    );
    expect(screen.getByTestId("kb-card-health-KB-1")).not.toHaveTextContent(
      "chunks",
    );
  });
});

describe("Knowledge Bases R3 declutter (Explore Query)", () => {
  it("defaults the Query view to a clean ask box and hides the version grid behind Compare versions", async () => {
    server.use(...workspaceHandlers([knowledgeBase()], [version()]));

    const user = userEvent.setup();
    render(<KnowledgeBases />);

    await openTo(user, "explore-ask");

    // The simplified default: ask box + single mode segmented control + top-k.
    expect(await screen.findByTestId("kb-explore-ask")).toBeInTheDocument();
    expect(
      screen.getByTestId("kb-explore-mode-segmented"),
    ).toBeInTheDocument();
    expect(screen.getByLabelText("Top K chunks")).toBeInTheDocument();
    expect(
      screen.getByPlaceholderText("Ask a question about the selected documents…"),
    ).toBeInTheDocument();

    // The verbose multi-version compare panel is collapsed by default.
    const compare = screen.getByTestId("kb-explore-compare");
    expect(
      within(compare).getByRole("button", { name: /Compare versions/ }),
    ).toHaveAttribute("aria-expanded", "false");
    expect(
      screen.queryByText("Compare answers across versions"),
    ).not.toBeInTheDocument();

    // Expanding it reveals the version selector grid + compare panel.
    await openExploreCompare(user);
    expect(
      await screen.findByText("Compare answers across versions"),
    ).toBeInTheDocument();
    expect(screen.getByText("Versions")).toBeInTheDocument();
  });

  it("runs a query against the header version with the segmented mode while compare stays collapsed", async () => {
    let queryBody: Record<string, unknown> | null = null;
    server.use(
      http.post(QUERY, async ({ request }) => {
        queryBody = (await request.json()) as Record<string, unknown>;
        return HttpResponse.json(
          envelope({
            question: "Who owns reliability?",
            versions: [
              {
                knowledge_base_version_id: "KBV-1",
                knowledge_base_id: "KB-1",
                version_number: 1,
                knowledge_base_name: "Operations Corpus",
                chunking_strategy: "recursive",
                embedding_model: "sentence-transformers/all-MiniLM-L6-v2",
                retrieval_mode: "graph_hybrid",
                answer: "Bob owns reliability.",
                answer_error: null,
                citations: [],
                retrieved_chunks: [],
                graph_context: {},
                timing_ms: { total: 12 },
              },
            ],
          }),
        );
      }),
      ...workspaceHandlers([knowledgeBase()], [version()]),
    );

    const user = userEvent.setup();
    render(<KnowledgeBases />);

    await openTo(user, "explore-ask");

    // Pick GraphRAG from the compact segmented control (single-select).
    await user.click(await screen.findByTestId("kb-explore-mode-graph_hybrid"));
    await user.type(
      screen.getByPlaceholderText(
        "Ask a question about the selected documents…",
      ),
      "Who owns reliability?",
    );
    await user.click(screen.getByRole("button", { name: "Ask" }));

    // Compare never opened; the query still runs against the header version
    // (KBV-1) with the single chosen mode.
    await waitFor(() =>
      expect(queryBody).toMatchObject({
        version_ids: ["KBV-1"],
        retrieval_modes: ["graph_hybrid"],
      }),
    );
    expect(
      screen.queryByText("Compare answers across versions"),
    ).not.toBeInTheDocument();
    expect(await screen.findByText("Bob owns reliability.")).toBeInTheDocument();
  });
});

describe("Knowledge Bases delete", () => {
  it("deletes a knowledge base from the library card after confirming", async () => {
    let deletedId: string | null = null;
    server.use(
      http.delete(`${KB}/:id`, ({ params }) => {
        deletedId = params.id as string;
        return HttpResponse.json(
          envelope({ knowledge_base_id: params.id, deleted: true }),
        );
      }),
      ...workspaceHandlers([knowledgeBase()], [version()]),
    );

    const user = userEvent.setup();
    render(<KnowledgeBases />);

    // The delete affordance + its two-step confirm live on the library card.
    await user.click(await screen.findByTestId("kb-card-delete-KB-1"));
    await user.click(
      await screen.findByTestId("kb-card-confirm-delete-KB-1"),
    );

    await waitFor(() => expect(deletedId).toBe("KB-1"));
  });

  const secondKnowledgeBase = () => ({
    ...knowledgeBase(),
    knowledge_base_id: "KB-2",
    name: "Second Corpus",
    active_version_id: null,
    last_run_id: null,
  });

  it("selects all then deletes the selected knowledge bases", async () => {
    const deleted: string[] = [];
    server.use(
      http.delete(`${KB}/:id`, ({ params }) => {
        deleted.push(params.id as string);
        return HttpResponse.json(
          envelope({ knowledge_base_id: params.id, deleted: true }),
        );
      }),
      ...workspaceHandlers([knowledgeBase(), secondKnowledgeBase()], [version()]),
    );

    const user = userEvent.setup();
    render(<KnowledgeBases />);

    // Check all, then the "Delete selected" action appears.
    await user.click(await screen.findByTestId("kb-select-all"));
    await user.click(await screen.findByTestId("kb-delete-selected"));
    // Two-step confirm shows the exact selected count before anything is deleted.
    expect(
      await screen.findByTestId("kb-bulk-delete-confirm"),
    ).toHaveTextContent("2 selected knowledge bases");
    await user.click(screen.getByTestId("kb-bulk-delete-confirm-btn"));

    await waitFor(() =>
      expect(deleted).toEqual(expect.arrayContaining(["KB-1", "KB-2"])),
    );
  });

  it("deletes only the individually selected knowledge base", async () => {
    const deleted: string[] = [];
    server.use(
      http.delete(`${KB}/:id`, ({ params }) => {
        deleted.push(params.id as string);
        return HttpResponse.json(
          envelope({ knowledge_base_id: params.id, deleted: true }),
        );
      }),
      ...workspaceHandlers([knowledgeBase(), secondKnowledgeBase()], [version()]),
    );

    const user = userEvent.setup();
    render(<KnowledgeBases />);

    // Tick only KB-2's card checkbox → Delete selected targets just that one.
    await user.click(await screen.findByTestId("kb-card-select-KB-2"));
    await user.click(await screen.findByTestId("kb-delete-selected"));
    await user.click(await screen.findByTestId("kb-bulk-delete-confirm-btn"));

    await waitFor(() => expect(deleted).toEqual(["KB-2"]));
  });

  it("deletes from the workspace header, returning to the library", async () => {
    let deletedId: string | null = null;
    const remaining: unknown[] = [knowledgeBase()];
    server.use(
      http.delete(`${KB}/:id`, ({ params }) => {
        deletedId = params.id as string;
        // After deletion the list refetches empty (the KB is gone).
        remaining.length = 0;
        return new HttpResponse(null, { status: 204 });
      }),
      http.get(`${KB}`, () => HttpResponse.json(envelope(remaining))),
      ...workspaceHandlers([knowledgeBase()], [version()]),
    );

    const user = userEvent.setup();
    render(<KnowledgeBases />);

    await openKnowledgeBaseWorkspace(user, "KB-1");
    await user.click(await screen.findByTestId("kb-workspace-delete"));
    await user.click(await screen.findByTestId("kb-workspace-confirm-delete"));

    await waitFor(() => expect(deletedId).toBe("KB-1"));
    // The workspace closes back to the library landing (no header) and the KB
    // is gone from the list.
    await waitFor(() =>
      expect(screen.queryByTestId("kb-workspace-header")).not.toBeInTheDocument(),
    );
    expect(screen.queryByTestId("kb-card-KB-1")).not.toBeInTheDocument();
  });

  it("surfaces a non-fatal error when deletion fails", async () => {
    server.use(
      http.delete(`${KB}/:id`, () =>
        HttpResponse.json(
          { detail: "knowledge base is in use" },
          { status: 409 },
        ),
      ),
      ...workspaceHandlers([knowledgeBase()], [version()]),
    );

    const user = userEvent.setup();
    render(<KnowledgeBases />);

    await openKnowledgeBaseWorkspace(user, "KB-1");
    await user.click(await screen.findByTestId("kb-workspace-delete"));
    await user.click(await screen.findByTestId("kb-workspace-confirm-delete"));

    // The workspace stays open and the error shows inline.
    expect(
      await screen.findByTestId("kb-workspace-delete-error"),
    ).toHaveTextContent("knowledge base is in use");
    expect(screen.getByTestId("kb-workspace-header")).toBeInTheDocument();
  });
});
