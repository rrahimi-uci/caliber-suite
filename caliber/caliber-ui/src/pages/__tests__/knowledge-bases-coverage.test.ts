import { describe, expect, it } from "vitest";

import {
  ageSeedModeLabel,
  ageSeedStrategyLabel,
  ageSyncActionLabel,
  ageSyncStatus,
  ageTraversalLabel,
  buildGraphOverridesPayload,
  chunkCount,
  cloneGraphConfig,
  configuredDefaultRetrievalMode,
  defaultPlaygroundRetrievalModesForVersion,
  entityCount,
  fallbackGraphBuildPresets,
  formatDate,
  graphBuildPresetMatch,
  graphConfigSummary,
  graphConfigsEqual,
  graphModeTone,
  graphOverridesFromConfig,
  graphStatusLabel,
  graphStatusTone,
  graphTargetLabel,
  humanSize,
  isAgeConfiguredVersion,
  isAgeReadyVersion,
  isRunLive,
  isVersionLive,
  kbBuildStepStates,
  kbSummaryBuilding,
  libraryVersionSummary,
  objectStoreObjectPath,
  objectStorePrefixPath,
  preferAgeGraphTarget,
  processedSourceCount,
  relationshipCount,
  retrievalModeLabel,
  retrievalModeShortLabel,
  strictAgeGraphOverridesFromConfig,
  suiteServiceHref,
  versionArtifactPath,
} from "@/pages/KnowledgeBases";

const config = (overrides: Record<string, unknown> = {}) => ({
  extractor_backend: "heuristic",
  spacy_model: "en_core_web_sm",
  max_entities_per_chunk: 12,
  entity_types: [],
  minimum_entity_mentions: 1,
  minimum_relationship_weight: 1,
  default_retrieval_mode: "graph_hybrid",
  retrieval_strength: "balanced",
  output_target: "object_store",
  age_seed_mode: "entity_then_text",
  age_traversal_hops: 1,
  age_candidate_pool_size: 24,
  age_dense_rerank_weight: 0.35,
  strict_age_retrieval_default: false,
  ...overrides,
});

const version = (overrides: Record<string, unknown> = {}) => ({
  graph_config: config(),
  summary: { age_sync_status: "skipped", chunk_count: 3, processed_sources: 2, entity_count: 4, relationship_count: 5 },
  output_bucket: "kb-bucket",
  output_prefix: "kb/version-1",
  ...overrides,
});

describe("Knowledge Bases decision helpers", () => {
  it("covers graph presets, labels, and state counters", () => {
    expect(fallbackGraphBuildPresets(false)).toHaveLength(2);
    expect(fallbackGraphBuildPresets(true)).toHaveLength(4);
    const presets = fallbackGraphBuildPresets(false);
    expect(graphBuildPresetMatch(config(), presets)).toBe("balanced");
    expect(graphBuildPresetMatch(config({ age_traversal_hops: 99 }), presets)).toBe("custom");
    expect(humanSize(12)).toBe("12 B");
    expect(humanSize(1024)).toBe("1.0 KB");
    expect(humanSize(1024 * 1024 * 12)).toBe("12 MB");
    expect(humanSize(1024 ** 4 * 2)).toBe("2.0 TB");
    expect(formatDate(null)).toBe("—");
    expect(formatDate("invalid")).toBe("—");
    expect(formatDate("2026-01-01T00:00:00Z")).not.toBe("—");
    expect(chunkCount(null)).toBe(0);
    expect(processedSourceCount(version() as never)).toBe(2);
    expect(entityCount(version() as never)).toBe(4);
    expect(relationshipCount(version() as never)).toBe(5);
    expect(isVersionLive({ status: "queued" })).toBe(true);
    expect(isVersionLive({ status: "ready" })).toBe(false);
    expect(isRunLive({ status: "running" })).toBe(true);
    expect(isRunLive({ status: "failed" })).toBe(false);
    expect(retrievalModeLabel("dense")).toBe("Dense chunks");
    expect(retrievalModeLabel("hybrid")).toContain("Hybrid");
    expect(retrievalModeLabel("graph_hybrid")).toBe("GraphRAG hybrid");
    expect(retrievalModeLabel("age_graph")).toBe("Apache AGE graph");
    expect(retrievalModeLabel("other", "Fallback")).toBe("Fallback");
    expect(retrievalModeShortLabel("dense")).toBe("Dense");
    expect(retrievalModeShortLabel("hybrid")).toBe("Hybrid");
    expect(retrievalModeShortLabel("graph_hybrid")).toBe("GraphRAG");
    expect(retrievalModeShortLabel("age_graph")).toBe("AGE");
    expect(retrievalModeShortLabel("other", "Other")).toBe("Other");
    expect(ageSeedModeLabel("entity_then_text")).toContain("Entity first");
    expect(ageSeedModeLabel("query_entities_only")).toContain("Extracted");
    expect(ageSeedModeLabel("query_text_only")).toContain("Question");
    expect(ageSeedModeLabel("query_entities_and_text")).toContain("Entities");
    expect(ageSeedModeLabel(null, "fallback")).toBe("fallback");
    expect(ageSeedStrategyLabel("query_text")).toContain("question");
    expect(ageSeedStrategyLabel("query_entities")).toContain("entities");
    expect(ageSeedStrategyLabel("query_entities_and_text")).toContain("entities +");
    expect(ageSeedStrategyLabel(null)).toBeNull();
  });

  it("resolves AGE readiness, defaults, and graph status presentation", () => {
    const ageReady = version({
      graph_config: config({ output_target: "object_store_and_age", default_retrieval_mode: "age_graph" }),
      summary: { age_sync_status: "SYNCED" },
    });
    const ageFailed = version({ graph_config: config({ output_target: "object_store_and_age" }), summary: { age_sync_status: "failed" } });
    expect(cloneGraphConfig(null)).toMatchObject({ extractor_backend: "heuristic", spacy_model: null, entity_types: [] });
    expect(preferAgeGraphTarget(config(), true)).toMatchObject({ output_target: "object_store_and_age", default_retrieval_mode: "age_graph" });
    expect(preferAgeGraphTarget(config({ output_target: "object_store_and_age" }), false).output_target).toBe("object_store_and_age");
    expect(ageSyncStatus(null)).toBe("skipped");
    expect(isAgeConfiguredVersion(ageReady as never, true)).toBe(true);
    expect(isAgeConfiguredVersion(ageReady as never, false)).toBe(false);
    expect(isAgeReadyVersion(ageReady as never, true)).toBe(true);
    expect(isAgeReadyVersion(ageFailed as never, true)).toBe(false);
    expect(ageTraversalLabel(0)).toBe("Direct entities only");
    expect(ageTraversalLabel(1)).toBe("One-hop expansion");
    expect(ageTraversalLabel(2)).toBe("Two-hop expansion");
    expect(graphConfigSummary(version() as never, true)).toBe("Dense + GraphRAG hybrid");
    expect(graphConfigSummary(ageReady as never, true)).toContain("Apache AGE graph");
    expect(graphConfigSummary(ageFailed as never, true)).toContain("pending");
    expect(defaultPlaygroundRetrievalModesForVersion(ageReady as never, true)).toEqual(["age_graph"]);
    expect(defaultPlaygroundRetrievalModesForVersion(ageFailed as never, true)).toEqual(["graph_hybrid"]);
    expect(configuredDefaultRetrievalMode({ default_retrieval_mode: "age_graph", output_target: "object_store" }, true)).toBe("graph_hybrid");
    expect(configuredDefaultRetrievalMode({ default_retrieval_mode: "age_graph", output_target: "object_store_and_age" }, true)).toBe("age_graph");
    expect(graphTargetLabel("object_store_and_age")).toContain("AGE");
    expect(graphTargetLabel("object_store")).toContain("only");
    expect(graphModeTone("graph_hybrid")).toContain("blue");
    expect(graphModeTone("age_graph")).toContain("emerald");
    expect(graphModeTone("dense")).toContain("slate");
    for (const status of ["synced", "failed", "skipped", "pending", "other"]) {
      expect(graphStatusTone(status)).toBeTruthy();
      expect(graphStatusLabel(status)).toBeTruthy();
    }
    expect(ageSyncActionLabel(ageReady as never, true)).toBe("Resync to AGE");
    expect(ageSyncActionLabel(ageFailed as never, true)).toBe("Retry AGE sync");
    expect(ageSyncActionLabel(version() as never, true)).toBe("Enable AGE graph retrieval");
    expect(ageSyncActionLabel(version({ graph_config: config({ output_target: "object_store_and_age" }) }) as never, true)).toBe("Sync to AGE");
  });

  it("builds graph query payloads without leaking disabled controls", () => {
    const overrides = {
      retrieval_strength: "aggressive",
      minimum_relationship_weight: 2,
      age_seed_mode: "query_entities_only",
      age_traversal_hops: 2,
      age_candidate_pool_size: 40,
      age_dense_rerank_weight: 0.2,
      strict_age_retrieval: true,
    } as never;
    expect(graphOverridesFromConfig(config({ strict_age_retrieval_default: true }))).toMatchObject({ strict_age_retrieval: true });
    expect(strictAgeGraphOverridesFromConfig(null)).toMatchObject({ strict_age_retrieval: true });
    expect(buildGraphOverridesPayload(false, overrides, true)).toBeUndefined();
    expect(buildGraphOverridesPayload(true, {}, true)).toBeUndefined();
    expect(buildGraphOverridesPayload(true, overrides, false)).toEqual({ retrieval_strength: "aggressive", minimum_relationship_weight: 2 });
    expect(buildGraphOverridesPayload(true, overrides, true)).toEqual(overrides);
  });

  it("covers paths and equality checks used by artifact links and library cards", () => {
    expect(libraryVersionSummary({ active_version_summary: null })).toBeNull();
    expect(libraryVersionSummary({ active_version_summary: { status: "ready" } } as never)).toEqual({ status: "ready" });
    for (const status of ["queued", "processing", "running", "pending", "ready"]) {
      expect(kbSummaryBuilding({ status } as never)).toBe(status !== "ready");
    }
    expect(objectStoreObjectPath("b u", "a/b")).toContain("bucket=b%20u");
    expect(objectStorePrefixPath("b", "a/")).toContain("prefix=a%2F");
    expect(versionArtifactPath(version() as never, "graph.json")).toContain("graph.json");
    expect(suiteServiceHref(8080, "/health")).toContain(":8080/health");
    const left = config();
    expect(graphConfigsEqual(left as never, { ...left } as never)).toBe(true);
    expect(graphConfigsEqual(left as never, config({ entity_types: ["person"] }) as never)).toBe(false);
  });

  it("covers the build step state machine for queued, failed, and completed runs", () => {
    expect(kbBuildStepStates(null, [])).toEqual(["active", "pending", "pending", "pending", "pending", "pending", "pending"]);
    expect(kbBuildStepStates({ status: "failed" } as never, [{ event_type: "build_started" }, { event_type: "source_failed" }] as never)).toEqual(["done", "failed", "pending", "pending", "pending", "pending", "pending"]);
    expect(kbBuildStepStates({ status: "running" } as never, [
      { event_type: "build_started" }, { event_type: "source_completed" },
      { event_type: "graph_built" }, { event_type: "chunks_persisted" },
    ] as never)).toEqual(["done", "done", "done", "done", "done", "done", "active"]);
    expect(kbBuildStepStates({ status: "completed" } as never, [])).toEqual(["done", "done", "done", "done", "done", "done", "done"]);
  });

  it("covers sparse config fallbacks, optional counters, and graph payload branches", () => {
    const sparse = cloneGraphConfig({} as never);
    expect(sparse).toMatchObject({
      extractor_backend: "heuristic",
      spacy_model: null,
      max_entities_per_chunk: 12,
      minimum_entity_mentions: 1,
      minimum_relationship_weight: 1,
      default_retrieval_mode: "graph_hybrid",
      output_target: "object_store",
      age_traversal_hops: 1,
      strict_age_retrieval_default: false,
    });
    expect(cloneGraphConfig({
      extractor_backend: "custom",
      spacy_model: "model",
      max_entities_per_chunk: 7,
      entity_types: ["person"],
      minimum_entity_mentions: 2,
      minimum_relationship_weight: 3,
      default_retrieval_mode: "dense",
      retrieval_strength: "aggressive",
      output_target: "object_store_and_age",
      age_seed_mode: "query_text_only",
      age_traversal_hops: 3,
      age_candidate_pool_size: 50,
      age_dense_rerank_weight: 0.1,
      strict_age_retrieval_default: "true",
    } as never)).toMatchObject({
      extractor_backend: "custom",
      entity_types: ["person"],
      strict_age_retrieval_default: false,
    });

    expect(chunkCount({ summary: { chunk_count: 9 } } as never)).toBe(9);
    expect(processedSourceCount({ summary: { processed_sources: 4 } } as never)).toBe(4);
    expect(entityCount({ summary: { entity_count: 6 } } as never)).toBe(6);
    expect(relationshipCount({ summary: { relationship_count: 8 } } as never)).toBe(8);
    expect(isVersionLive(null)).toBe(false);
    expect(isVersionLive({ status: "processing" })).toBe(true);
    expect(isRunLive(null)).toBe(false);
    expect(isRunLive({ status: "queued" })).toBe(true);
    expect(ageSyncStatus({ summary: null } as never)).toBe("skipped");
    expect(isAgeConfiguredVersion(null, true)).toBe(false);
    expect(isAgeReadyVersion(null, true)).toBe(false);
    expect(defaultPlaygroundRetrievalModesForVersion(null, false)).toEqual(["graph_hybrid"]);
    expect(ageSyncActionLabel({ graph_config: config({ output_target: "object_store_and_age" }), summary: { age_sync_status: "pending" } } as never, true)).toBe("Sync to AGE");
    expect(suiteServiceHref(8081)).toContain(":8081/");
    expect(suiteServiceHref(8082, "/health?ignored=1")).toContain(":8082/health");

    expect(buildGraphOverridesPayload(true, {}, true)).toBeUndefined();
    expect(buildGraphOverridesPayload(true, {
      age_seed_mode: "query_text_only",
      age_traversal_hops: 2,
      age_candidate_pool_size: 20,
      age_dense_rerank_weight: 0.25,
      strict_age_retrieval: false,
    }, true)).toEqual({
      age_seed_mode: "query_text_only",
      age_traversal_hops: 2,
      age_candidate_pool_size: 20,
      age_dense_rerank_weight: 0.25,
    });
    expect(buildGraphOverridesPayload(true, {
      retrieval_strength: "balanced",
      minimum_relationship_weight: 0,
      age_seed_mode: "query_text_only",
      age_traversal_hops: 2,
      age_candidate_pool_size: 20,
      age_dense_rerank_weight: 0.25,
      strict_age_retrieval: true,
    }, false)).toEqual({
      retrieval_strength: "balanced",
      minimum_relationship_weight: 0,
    });
  });

  it("checks every graph-config equality field independently", () => {
    const base = config();
    const changes = [
      { extractor_backend: "spacy" },
      { spacy_model: "other-model" },
      { max_entities_per_chunk: 99 },
      { minimum_entity_mentions: 2 },
      { minimum_relationship_weight: 2 },
      { default_retrieval_mode: "dense" },
      { retrieval_strength: "aggressive" },
      { output_target: "object_store_and_age" },
      { age_seed_mode: "query_text_only" },
      { age_traversal_hops: 2 },
      { age_candidate_pool_size: 99 },
      { age_dense_rerank_weight: 0.1 },
      { strict_age_retrieval_default: true },
      { entity_types: ["person"] },
    ];
    for (const change of changes) {
      expect(graphConfigsEqual(base as never, { ...base, ...change } as never)).toBe(false);
    }
    expect(graphBuildPresetMatch(
      { ...base, entity_types: ["person"] } as never,
      [{ id: "array-match", patch: { entity_types: ["person"] } } as never],
    )).toBe("array-match");
    expect(graphBuildPresetMatch(
      { ...base, entity_types: ["person"] } as never,
      [{ id: "array-mismatch", patch: { entity_types: ["person", "service"] } } as never],
    )).toBe("custom");
    expect(cloneGraphConfig({
      extractor_backend: null,
      spacy_model: null,
      max_entities_per_chunk: null,
      entity_types: null,
      minimum_entity_mentions: null,
      minimum_relationship_weight: null,
      default_retrieval_mode: null,
      retrieval_strength: null,
      output_target: null,
      age_seed_mode: null,
      age_traversal_hops: null,
      age_candidate_pool_size: null,
      age_dense_rerank_weight: null,
      strict_age_retrieval_default: true,
    } as never)).toMatchObject({
      extractor_backend: "heuristic",
      strict_age_retrieval_default: true,
    });
  });
});
