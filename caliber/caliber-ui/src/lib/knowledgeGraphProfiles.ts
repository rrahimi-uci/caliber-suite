import type {
  KnowledgeAgeSeedMode,
  KnowledgeGraphQueryPreset,
  KnowledgeGraphQueryPresetId,
  KnowledgeGraphRetrievalStrength,
  KnowledgeQueryGraphOverrides,
  KnowledgeRetrievalMode,
} from "@/api/knowledgeTypes";

export interface KnowledgeGraphQueryPresetState {
  retrieval_mode: KnowledgeRetrievalMode;
  retrieval_strength: KnowledgeGraphRetrievalStrength;
  minimum_relationship_weight: number;
  age_seed_mode: KnowledgeAgeSeedMode;
  age_traversal_hops: number;
  age_candidate_pool_size: number;
  age_dense_rerank_weight: number;
  strict_age_retrieval: boolean;
}

const QUERY_PRESET_DEFAULTS: Omit<
  KnowledgeGraphQueryPresetState,
  "retrieval_mode"
> = {
  retrieval_strength: "balanced",
  minimum_relationship_weight: 1,
  age_seed_mode: "entity_then_text",
  age_traversal_hops: 1,
  age_candidate_pool_size: 24,
  age_dense_rerank_weight: 0.35,
  strict_age_retrieval: false,
};

export function fallbackGraphQueryPresets(
  ageEnabled: boolean,
): KnowledgeGraphQueryPreset[] {
  return [
    {
      id: "hybrid_precision",
      label: "Precise GraphRAG",
      eyebrow: "Precision",
      description:
        "Stay on the version-local graph, require stronger relationship evidence, and keep expansion tight around directly matched entities.",
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
      description:
        "Blend dense recall with graph-aware evidence expansion without depending on Apache AGE. This is the safest everyday fallback profile.",
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
            id: "age_balanced" as const,
            label: "Balanced AGE",
            eyebrow: "Recommended",
            description:
              "Run Apache AGE as the primary retrieval path with one-hop traversal and moderate dense reranking for grounded, inspectable answers.",
            badges: ["AGE primary", "1-hop", "Balanced rerank"],
            retrieval_mode: "age_graph" as const,
            patch: {
              retrieval_strength: "balanced" as const,
              minimum_relationship_weight: 1,
              age_seed_mode: "entity_then_text" as const,
              age_traversal_hops: 1,
              age_candidate_pool_size: 24,
              age_dense_rerank_weight: 0.35,
            },
            recommended: true,
            age_required: true,
          },
          {
            id: "age_native" as const,
            label: "AGE-native retrieval",
            eyebrow: "Graph-first",
            description:
              "Keep retrieval graph-first with broader seeding, two-hop traversal, and lighter dense reranking so the answer path stays AGE-backed.",
            badges: ["AGE primary", "2-hop", "Graph-first"],
            retrieval_mode: "age_graph" as const,
            patch: {
              retrieval_strength: "aggressive" as const,
              minimum_relationship_weight: 1,
              age_seed_mode: "query_entities_and_text" as const,
              age_traversal_hops: 2,
              age_candidate_pool_size: 40,
              age_dense_rerank_weight: 0.2,
            },
            recommended: false,
            age_required: true,
          },
          {
            id: "age_strict" as const,
            label: "Strict AGE only",
            eyebrow: "Locked",
            description:
              "Stay on Apache AGE only with graph-first traversal and no silent fallback to local GraphRAG or dense-only retrieval.",
            badges: ["AGE primary", "Strict", "No fallback"],
            retrieval_mode: "age_graph" as const,
            patch: {
              retrieval_strength: "aggressive" as const,
              minimum_relationship_weight: 1,
              age_seed_mode: "query_entities_and_text" as const,
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
  ];
}

export function buildGraphQueryPresetState({
  retrievalMode,
  overrides,
  fallback,
}: {
  retrievalMode: KnowledgeRetrievalMode;
  overrides?: KnowledgeQueryGraphOverrides | null;
  fallback?: Partial<Omit<KnowledgeGraphQueryPresetState, "retrieval_mode">>;
}): KnowledgeGraphQueryPresetState {
  return {
    retrieval_mode: retrievalMode,
    retrieval_strength:
      overrides?.retrieval_strength ??
      fallback?.retrieval_strength ??
      QUERY_PRESET_DEFAULTS.retrieval_strength,
    minimum_relationship_weight:
      typeof overrides?.minimum_relationship_weight === "number"
        ? overrides.minimum_relationship_weight
        : (fallback?.minimum_relationship_weight ??
          QUERY_PRESET_DEFAULTS.minimum_relationship_weight),
    age_seed_mode:
      overrides?.age_seed_mode ??
      fallback?.age_seed_mode ??
      QUERY_PRESET_DEFAULTS.age_seed_mode,
    age_traversal_hops:
      typeof overrides?.age_traversal_hops === "number"
        ? overrides.age_traversal_hops
        : (fallback?.age_traversal_hops ??
          QUERY_PRESET_DEFAULTS.age_traversal_hops),
    age_candidate_pool_size:
      typeof overrides?.age_candidate_pool_size === "number"
        ? overrides.age_candidate_pool_size
        : (fallback?.age_candidate_pool_size ??
          QUERY_PRESET_DEFAULTS.age_candidate_pool_size),
    age_dense_rerank_weight:
      typeof overrides?.age_dense_rerank_weight === "number"
        ? overrides.age_dense_rerank_weight
        : (fallback?.age_dense_rerank_weight ??
          QUERY_PRESET_DEFAULTS.age_dense_rerank_weight),
    strict_age_retrieval:
      typeof overrides?.strict_age_retrieval === "boolean"
        ? overrides.strict_age_retrieval
        : (fallback?.strict_age_retrieval ??
          QUERY_PRESET_DEFAULTS.strict_age_retrieval),
  };
}

export function resolveGraphQueryPresetState(
  preset: KnowledgeGraphQueryPreset,
  fallback?: Partial<Omit<KnowledgeGraphQueryPresetState, "retrieval_mode">>,
): KnowledgeGraphQueryPresetState {
  return buildGraphQueryPresetState({
    retrievalMode: preset.retrieval_mode,
    overrides: preset.patch,
    fallback,
  });
}

export function graphQueryOverridesFromState(
  state: KnowledgeGraphQueryPresetState,
): KnowledgeQueryGraphOverrides {
  const overrides: KnowledgeQueryGraphOverrides = {
    retrieval_strength: state.retrieval_strength,
    minimum_relationship_weight: state.minimum_relationship_weight,
  };
  if (state.retrieval_mode === "age_graph") {
    overrides.age_seed_mode = state.age_seed_mode;
    overrides.age_traversal_hops = state.age_traversal_hops;
    overrides.age_candidate_pool_size = state.age_candidate_pool_size;
    overrides.age_dense_rerank_weight = state.age_dense_rerank_weight;
    if (state.strict_age_retrieval) {
      overrides.strict_age_retrieval = true;
    }
  }
  return overrides;
}

export function matchGraphQueryPreset(
  state: KnowledgeGraphQueryPresetState,
  presets: KnowledgeGraphQueryPreset[],
): KnowledgeGraphQueryPresetId | "custom" {
  const stateRecord: Record<string, unknown> = {
    retrieval_mode: state.retrieval_mode,
    retrieval_strength: state.retrieval_strength,
    minimum_relationship_weight: state.minimum_relationship_weight,
    age_seed_mode: state.age_seed_mode,
    age_traversal_hops: state.age_traversal_hops,
    age_candidate_pool_size: state.age_candidate_pool_size,
    age_dense_rerank_weight: state.age_dense_rerank_weight,
    strict_age_retrieval: state.strict_age_retrieval,
  };
  const match = presets.find((preset) => {
    if (preset.retrieval_mode !== state.retrieval_mode) return false;
    return Object.entries(preset.patch).every(
      ([key, value]) => stateRecord[key] === value,
    );
  });
  return match?.id ?? "custom";
}
