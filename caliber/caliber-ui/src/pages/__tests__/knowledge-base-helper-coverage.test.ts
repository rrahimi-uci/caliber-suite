import { describe, expect, it } from "vitest";

import type { KnowledgeGraphConfig } from "@/api/knowledgeTypes";
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

const config = (): KnowledgeGraphConfig => ({
  extractor_backend: "heuristic",
  spacy_model: "en_core_web_sm",
  max_entities_per_chunk: 12,
  entity_types: ["person", "document"],
  minimum_entity_mentions: 1,
  minimum_relationship_weight: 0.2,
  default_retrieval_mode: "graph_hybrid",
  retrieval_strength: "balanced",
  output_target: "object_store",
  age_seed_mode: "entity_then_text",
  age_traversal_hops: 1,
  age_candidate_pool_size: 24,
  age_dense_rerank_weight: 0.35,
  strict_age_retrieval_default: false,
});

const version = (overrides: Record<string, unknown> = {}) => ({
  graph_config: config(),
  summary: { chunk_count: 4, processed_sources: 2, entity_count: 3, relationship_count: 5 },
  output_bucket: "kb-output",
  output_prefix: "versions/v1",
  ...overrides,
}) as never;

describe("KnowledgeBases pure helper decision tables", () => {
  it("covers preset construction, matching, sizes, dates, and counters", () => {
    expect(fallbackGraphBuildPresets(true)).toHaveLength(4);
    expect(fallbackGraphBuildPresets(false)).toHaveLength(2);
    expect(fallbackGraphBuildPresets(false)[1]).toMatchObject({ label: "Balanced GraphRAG", recommended: true });

    const presets = fallbackGraphBuildPresets(true);
    expect(graphBuildPresetMatch(config(), presets)).toBe("custom");
    expect(graphBuildPresetMatch({ ...config(), ...presets[1]?.patch }, presets)).toBe("balanced");
    expect(graphBuildPresetMatch({ ...config(), entity_types: ["person"] }, [
      { ...presets[0], id: "array", patch: { entity_types: ["person"] } },
    ] as never)).toBe("array");

    expect(humanSize(12)).toBe("12 B");
    expect(humanSize(1536)).toBe("1.5 KB");
    expect(humanSize(11 * 1024)).toBe("11 KB");
    expect(humanSize(1024 ** 2)).toBe("1.0 MB");
    expect(humanSize(1024 ** 3)).toBe("1.0 GB");
    expect(humanSize(1024 ** 4)).toBe("1.0 TB");
    expect(formatDate(null)).toBe("—");
    expect(formatDate("not-a-date")).toBe("—");
    expect(formatDate("2026-01-01T00:00:00Z")).not.toBe("—");
    expect(chunkCount(null)).toBe(0);
    expect(processedSourceCount(version())).toBe(2);
    expect(entityCount(version())).toBe(3);
    expect(relationshipCount(version())).toBe(5);
    expect(isVersionLive(null)).toBe(false);
    expect(isVersionLive({ status: "queued" })).toBe(true);
    expect(isVersionLive({ status: "processing" })).toBe(true);
    expect(isVersionLive({ status: "ready" })).toBe(false);
    expect(isRunLive({ status: "queued" })).toBe(true);
    expect(isRunLive({ status: "running" })).toBe(true);
    expect(isRunLive({ status: "completed" })).toBe(false);
  });

  it("covers retrieval labels and graph configuration fallback branches", () => {
    for (const mode of ["dense", "hybrid", "graph_hybrid", "age_graph"]) {
      expect(retrievalModeLabel(mode)).not.toBe(mode);
      expect(retrievalModeShortLabel(mode)).not.toBe(mode);
    }
    expect(retrievalModeLabel("other", "Fallback")).toBe("Fallback");
    expect(retrievalModeLabel("other")).toBe("other");
    expect(retrievalModeShortLabel("other", "Short fallback")).toBe("Short fallback");
    expect(retrievalModeShortLabel("other")).toBe("other");
    for (const mode of ["entity_then_text", "query_entities_only", "query_text_only", "query_entities_and_text"]) {
      expect(ageSeedModeLabel(mode)).not.toBe(mode);
    }
    expect(ageSeedModeLabel(null, "Fallback seed")).toBe("Fallback seed");
    expect(ageSeedModeLabel("other")).toContain("Entity first");
    expect(ageSeedStrategyLabel("query_text")).toContain("question");
    expect(ageSeedStrategyLabel("query_entities")).toContain("entities");
    expect(ageSeedStrategyLabel("query_entities_and_text")).toContain("entities");
    expect(ageSeedStrategyLabel(null)).toBeNull();

    const full = config();
    expect(cloneGraphConfig(null)).toMatchObject({
      extractor_backend: "heuristic",
      default_retrieval_mode: "graph_hybrid",
      output_target: "object_store",
      entity_types: [],
    });
    expect(cloneGraphConfig(full)).toEqual(full);
    expect(preferAgeGraphTarget(full, false).output_target).toBe("object_store");
    expect(preferAgeGraphTarget(full, true)).toMatchObject({
      output_target: "object_store_and_age",
      default_retrieval_mode: "age_graph",
    });
    expect(graphOverridesFromConfig(full)).toMatchObject({ retrieval_strength: "balanced", strict_age_retrieval: false });
    expect(strictAgeGraphOverridesFromConfig(full).strict_age_retrieval).toBe(true);

    expect(buildGraphOverridesPayload(false, {}, true)).toBeUndefined();
    expect(buildGraphOverridesPayload(true, {}, false)).toBeUndefined();
    expect(buildGraphOverridesPayload(true, {
      retrieval_strength: "aggressive",
      minimum_relationship_weight: 0.4,
      age_seed_mode: "query_text",
      age_traversal_hops: 2,
      age_candidate_pool_size: 40,
      age_dense_rerank_weight: 0.2,
      strict_age_retrieval: true,
    }, true)).toMatchObject({
      retrieval_strength: "aggressive",
      minimum_relationship_weight: 0.4,
      age_seed_mode: "query_text",
      age_traversal_hops: 2,
      age_candidate_pool_size: 40,
      age_dense_rerank_weight: 0.2,
      strict_age_retrieval: true,
    });
    expect(buildGraphOverridesPayload(true, { age_seed_mode: "query_text" }, false)).toBeUndefined();
    expect(configuredDefaultRetrievalMode({ default_retrieval_mode: "age_graph", output_target: "object_store_and_age" }, true)).toBe("age_graph");
    expect(configuredDefaultRetrievalMode({ default_retrieval_mode: "age_graph", output_target: "object_store" }, true)).toBe("graph_hybrid");
    expect(configuredDefaultRetrievalMode({ default_retrieval_mode: "age_graph", output_target: "object_store_and_age" }, false)).toBe("graph_hybrid");
    expect(configuredDefaultRetrievalMode(null, true)).toBe("graph_hybrid");
  });

  it("covers AGE status, graph presentation, actions, and URLs", () => {
    expect(graphTargetLabel("object_store_and_age")).toContain("AGE");
    expect(graphTargetLabel("object_store")).toBe("Object store only");
    expect(ageSyncStatus(null)).toBe("skipped");
    expect(ageSyncStatus({ summary: { age_sync_status: "SYNCED" } })).toBe("synced");
    expect(isAgeConfiguredVersion(version({ graph_config: { ...config(), output_target: "object_store_and_age" } }), true)).toBe(true);
    expect(isAgeConfiguredVersion(version({ graph_config: { ...config(), output_target: "object_store_and_age" } }), false)).toBe(false);
    expect(isAgeReadyVersion(version({ graph_config: { ...config(), output_target: "object_store_and_age" }, summary: { age_sync_status: "synced" } }), true)).toBe(true);
    expect(isAgeReadyVersion(version({ graph_config: { ...config(), output_target: "object_store_and_age" }, summary: { age_sync_status: "failed" } }), true)).toBe(false);
    expect(ageTraversalLabel(0)).toBe("Direct entities only");
    expect(ageTraversalLabel(1)).toBe("One-hop expansion");
    expect(ageTraversalLabel(2)).toBe("Two-hop expansion");
    expect(graphConfigSummary(version({ graph_config: { ...config(), output_target: "object_store" } }), true)).not.toContain("AGE graph");
    expect(graphConfigSummary(version({ graph_config: { ...config(), output_target: "object_store_and_age" }, summary: { age_sync_status: "synced" } }), true)).toContain("Apache AGE graph");
    expect(graphConfigSummary(version({ graph_config: { ...config(), output_target: "object_store_and_age" }, summary: { age_sync_status: "pending" } }), true)).toContain("pending");
    expect(defaultPlaygroundRetrievalModesForVersion(version({ graph_config: { ...config(), default_retrieval_mode: "age_graph", output_target: "object_store_and_age" }, summary: { age_sync_status: "pending" } }), true)).toEqual(["graph_hybrid"]);
    expect(defaultPlaygroundRetrievalModesForVersion(version(), true)).toEqual(["graph_hybrid"]);
    expect(graphModeTone("graph_hybrid")).toContain("blue");
    expect(graphModeTone("age_graph")).toContain("emerald");
    expect(graphModeTone("dense")).toContain("slate");
    for (const status of ["synced", "failed", "skipped", "pending", "other"]) {
      expect(graphStatusTone(status)).toContain("-");
      expect(graphStatusLabel(status)).toBeTruthy();
    }
    expect(libraryVersionSummary({ active_version_summary: null })).toBeNull();
    expect(libraryVersionSummary({ active_version_summary: { status: "ready" } as never })).toEqual({ status: "ready" });
    for (const status of ["queued", "processing", "running", "pending"]) expect(kbSummaryBuilding({ status })).toBe(true);
    expect(kbSummaryBuilding({ status: "ready" })).toBe(false);
    expect(ageSyncActionLabel(version({ graph_config: { ...config(), output_target: "object_store_and_age" }, summary: { age_sync_status: "synced" } }), true)).toBe("Resync to AGE");
    expect(ageSyncActionLabel(version({ graph_config: { ...config(), output_target: "object_store_and_age" }, summary: { age_sync_status: "failed" } }), true)).toBe("Retry AGE sync");
    expect(ageSyncActionLabel(version(), true)).toBe("Enable AGE graph retrieval");
    expect(ageSyncActionLabel(version({ graph_config: { ...config(), output_target: "object_store_and_age" }, summary: { age_sync_status: "pending" } }), true)).toBe("Sync to AGE");
    expect(objectStoreObjectPath("bucket name", "folder/file.json")).toContain("bucket%20name");
    expect(objectStorePrefixPath("bucket", "folder/")).toContain("prefix=folder%2F");
    expect(versionArtifactPath(version() as never, "answer.json")).toContain("answer.json");
    expect(suiteServiceHref(8082, "/health")).toContain(":8082/health");
  });

  it("compares every graph-config field and its list boundary", () => {
    const fields: Array<keyof KnowledgeGraphConfig> = [
      "extractor_backend", "spacy_model", "max_entities_per_chunk", "minimum_entity_mentions",
      "minimum_relationship_weight", "default_retrieval_mode", "retrieval_strength", "output_target",
      "age_seed_mode", "age_traversal_hops", "age_candidate_pool_size", "age_dense_rerank_weight",
      "strict_age_retrieval_default", "entity_types",
    ];
    for (const field of fields) {
      const changed = config();
      if (field === "entity_types") changed.entity_types = ["different"];
      else if (field === "strict_age_retrieval_default") changed[field] = true;
      else if (typeof changed[field] === "number") changed[field] = (changed[field] as number) + 1;
      else changed[field] = "different" as never;
      expect(graphConfigsEqual(config(), changed)).toBe(false);
    }
    expect(graphConfigsEqual(config(), config())).toBe(true);
    expect(graphConfigsEqual(config(), { ...config(), entity_types: ["person"] })).toBe(false);
  });
});
