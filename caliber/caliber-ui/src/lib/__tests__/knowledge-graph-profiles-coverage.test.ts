import { describe, expect, it } from "vitest";

import {
  buildGraphQueryPresetState,
  fallbackGraphQueryPresets,
  graphQueryOverridesFromState,
  matchGraphQueryPreset,
  resolveGraphQueryPresetState,
} from "@/lib/knowledgeGraphProfiles";

describe("knowledge graph query profiles", () => {
  it("builds deterministic fallback catalogs for AGE-enabled and portable deployments", () => {
    const portable = fallbackGraphQueryPresets(false);
    const age = fallbackGraphQueryPresets(true);
    expect(portable).toHaveLength(2);
    expect(age).toHaveLength(5);
    expect(portable.find((preset) => preset.id === "hybrid_balanced")).toMatchObject({
      eyebrow: "Recommended",
      recommended: true,
      age_required: false,
    });
    expect(age.find((preset) => preset.id === "hybrid_balanced")).toMatchObject({
      eyebrow: "Portable",
      recommended: false,
    });
    expect(age.find((preset) => preset.id === "age_strict")).toMatchObject({
      recommended: false,
      age_required: true,
      patch: { strict_age_retrieval: true },
    });
  });

  it("uses overrides, partial fallbacks, and defaults for each scalar field", () => {
    expect(buildGraphQueryPresetState({ retrievalMode: "dense" })).toEqual({
      retrieval_mode: "dense",
      retrieval_strength: "balanced",
      minimum_relationship_weight: 1,
      age_seed_mode: "entity_then_text",
      age_traversal_hops: 1,
      age_candidate_pool_size: 24,
      age_dense_rerank_weight: 0.35,
      strict_age_retrieval: false,
    });
    const fallback = {
      retrieval_strength: "aggressive",
      minimum_relationship_weight: 4,
      age_seed_mode: "query_text_only",
      age_traversal_hops: 3,
      age_candidate_pool_size: 80,
      age_dense_rerank_weight: 0.1,
      strict_age_retrieval: true,
    } as never;
    expect(buildGraphQueryPresetState({ retrievalMode: "age_graph", fallback })).toMatchObject(fallback);
    expect(buildGraphQueryPresetState({
      retrievalMode: "age_graph",
      overrides: {
        retrieval_strength: "conservative",
        minimum_relationship_weight: 2,
        age_seed_mode: "query_entities_only",
        age_traversal_hops: 0,
        age_candidate_pool_size: 12,
        age_dense_rerank_weight: 0,
        strict_age_retrieval: true,
      },
      fallback,
    })).toMatchObject({
      retrieval_strength: "conservative",
      minimum_relationship_weight: 2,
      age_seed_mode: "query_entities_only",
      age_traversal_hops: 0,
      age_candidate_pool_size: 12,
      age_dense_rerank_weight: 0,
      strict_age_retrieval: true,
    });
    expect(buildGraphQueryPresetState({
      retrievalMode: "graph_hybrid",
      overrides: { minimum_relationship_weight: "bad", age_traversal_hops: null } as never,
      fallback: { minimum_relationship_weight: 2, age_traversal_hops: 4 },
    })).toMatchObject({ minimum_relationship_weight: 2, age_traversal_hops: 4 });
  });

  it("emits only AGE overrides for AGE profiles and matches custom state", () => {
    const base = {
      retrieval_mode: "graph_hybrid" as const,
      retrieval_strength: "balanced" as const,
      minimum_relationship_weight: 1,
      age_seed_mode: "entity_then_text" as const,
      age_traversal_hops: 1,
      age_candidate_pool_size: 24,
      age_dense_rerank_weight: 0.35,
      strict_age_retrieval: true,
    };
    expect(graphQueryOverridesFromState(base)).toEqual({
      retrieval_strength: "balanced",
      minimum_relationship_weight: 1,
    });
    expect(graphQueryOverridesFromState({ ...base, retrieval_mode: "age_graph" })).toMatchObject({
      age_seed_mode: "entity_then_text",
      age_traversal_hops: 1,
      age_candidate_pool_size: 24,
      age_dense_rerank_weight: 0.35,
      strict_age_retrieval: true,
    });
    const presets = fallbackGraphQueryPresets(true);
    for (const preset of presets) {
      expect(matchGraphQueryPreset(resolveGraphQueryPresetState(preset), presets)).toBeTruthy();
    }
    expect(matchGraphQueryPreset({ ...base, retrieval_mode: "age_graph", minimum_relationship_weight: 99 }, presets)).toBe("custom");
    expect(resolveGraphQueryPresetState(presets[0]!, { retrieval_strength: "aggressive" })).toMatchObject({
      retrieval_mode: "graph_hybrid",
      retrieval_strength: "conservative",
    });
  });
});
