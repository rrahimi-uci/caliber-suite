import {
  useCallback,
  useEffect,
  useMemo,
  useRef,
  useState,
  type FormEvent,
  type ReactNode,
} from "react";
import { Link, useSearchParams } from "react-router-dom";
import {
  ArrowRightLeft,
  Bot,
  Boxes,
  Check,
  ChevronDown,
  Database,
  Eye,
  FileCode2,
  Folder,
  FolderOpen,
  HardDrive,
  Layers3,
  Loader2,
  MessageSquareText,
  Play,
  RefreshCw,
  Search,
  Sparkles,
  Trash2,
  UploadCloud,
  X,
} from "lucide-react";

import { ApiError, caliberApi } from "@/api/caliberApi";
import type {
  KnowledgeAgeSeedMode,
  KnowledgeBase,
  KnowledgeBaseActiveVersionSummary,
  KnowledgeBaseBuildResult,
  KnowledgeBaseChunk,
  KnowledgeBaseEntity,
  KnowledgeGraphBuildPreset,
  KnowledgeGraphBuildPresetId as KnowledgePresetId,
  KnowledgeGraphEntityView,
  KnowledgeGraphExploreResult,
  KnowledgeGraphExploreSource,
  KnowledgeGraphQueryPreset,
  KnowledgeGraphRelationshipView,
  KnowledgeGraphConfig,
  KnowledgeBaseRelationship,
  KnowledgeBaseRun,
  KnowledgeBaseRunEvent,
  KnowledgeBaseSource,
  KnowledgeBaseVersion,
  KnowledgeOptions,
  KnowledgeQueryGraphOverrides,
  KnowledgeQueryResult,
  KnowledgeQueryVersionResult,
  KnowledgeRetrievalMode,
  KnowledgeSourceSelection,
} from "@/api/knowledgeTypes";
import type { ObjectStoreBucket } from "@/api/workflowTypes";
import type { CurrentUserInfo } from "@/api/types";
import { ClearFiltersButton } from "@/components/ClearFiltersButton";
import { FilterBar } from "@/components/FilterBar";
import { BucketTree } from "@/components/knowledge/BucketTree";
import { ListRow, ListRows } from "@/components/ListRow";
import { PageHeader } from "@/components/PageHeader";
import { PageTabs, type PageTab } from "@/components/PageTabs";
import { SearchInput } from "@/components/SearchInput";
import { FilterSelect } from "@/components/FilterSelect";
import { ViewToggle } from "@/components/ViewToggle";
import { useApiQuery, useInvalidate } from "@/hooks/useApiQuery";
import { useViewMode } from "@/hooks/useViewMode";
import { appendThemeHintToUrl } from "@/lib/externalLinks";
import {
  buildGraphQueryPresetState,
  fallbackGraphQueryPresets,
  graphQueryOverridesFromState,
  matchGraphQueryPreset,
  resolveGraphQueryPresetState,
} from "@/lib/knowledgeGraphProfiles";
import {
  parseKnowledgeBuildLaunchParams,
  stripKnowledgeBuildLaunchParams,
  type KnowledgeBuildLaunchPayload,
  type KnowledgeBuildLaunchPreset,
} from "@/lib/knowledgeBuildLaunch";
import { KnowledgeCalibrateTab } from "@/pages/knowledge/KnowledgeCalibrateTab";
import { VersionPanel } from "@/components/versioning/VersionPanel";
import { makeKnowledgeBaseVersionAdapter } from "@/components/versioning/adapters";

type KnowledgeTab =
  | "library"
  | "build"
  | "versions"
  | "graph"
  | "playground"
  | "calibrate"
  | "runs";
// ── Asset-workspace stages (Phase R1) ───────────────────────────────────────
// The four per-KB stages and Explore's sub-views. Legacy ``KnowledgeTab`` is
// kept only as the input alphabet for ``goToTab`` (the compatibility shim that
// existing handlers call), which maps an old tab name onto a stage + sub-view.
type WorkspaceStage = "build" | "explore" | "calibrate" | "use";
type ExploreView = "ask" | "chunks" | "graph";

const KB_WORKSPACE_STAGES: PageTab[] = [
  {
    key: "build",
    label: "Build",
    icon: (
      <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.75">
        <path d="M12 5v14M5 12h14" />
      </svg>
    ),
  },
  {
    key: "explore",
    label: "Explore",
    icon: (
      <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.75">
        <circle cx="11" cy="11" r="7" />
        <path d="m21 21-4.3-4.3" />
      </svg>
    ),
  },
  {
    key: "calibrate",
    label: "Calibrate",
    icon: (
      <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.75">
        <path d="M3 20h18" />
        <path d="M6 16V9M12 16V5M18 16v-4" />
      </svg>
    ),
  },
  {
    key: "use",
    label: "Use",
    icon: (
      <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.75">
        <path d="M5 12h14M13 5l7 7-7 7" />
      </svg>
    ),
  },
];

const KB_EXPLORE_VIEWS: Array<{ key: ExploreView; label: string }> = [
  // "Query" (not "Ask") avoids colliding with the playground's "Ask" submit
  // button when tests/users look up controls by accessible name.
  { key: "ask", label: "Query" },
  { key: "chunks", label: "Chunks" },
  { key: "graph", label: "Graph" },
];

type BuildMode = "new" | "existing";
type GraphBuildPresetId = KnowledgePresetId | "custom";

interface ChatTurn {
  question: string;
  result: KnowledgeQueryResult;
}

interface PendingPlaygroundSeed {
  retrievalModes: KnowledgeRetrievalMode[];
  graphOverrides: KnowledgeQueryGraphOverrides;
  graphTuningEnabled: boolean;
}

const FALLBACK_GRAPH_CONFIG: KnowledgeGraphConfig = {
  extractor_backend: "heuristic",
  spacy_model: null,
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
};
const EMPTY_KNOWLEDGE_BASES: KnowledgeBase[] = [];
const EMPTY_VERSIONS: KnowledgeBaseVersion[] = [];
const EMPTY_RUNS: KnowledgeBaseRun[] = [];
const EMPTY_GRAPH_ENTITIES: KnowledgeGraphEntityView[] = [];
const EMPTY_GRAPH_RELATIONSHIPS: KnowledgeGraphRelationshipView[] = [];

function fallbackGraphBuildPresets(
  ageEnabled: boolean,
): KnowledgeGraphBuildPreset[] {
  return [
    {
      id: "portable",
      label: "Portable graph artifacts",
      eyebrow: "Portable",
      description:
        "Keep entities and relationships in the object store only. Best when you want easy export, no shared graph dependency, and local GraphRAG fallback.",
      badges: ["Object store", "Hybrid-ready", "No AGE sync"],
      patch: {
        default_retrieval_mode:
          "graph_hybrid" as KnowledgeGraphConfig["default_retrieval_mode"],
        output_target: "object_store" as KnowledgeGraphConfig["output_target"],
        retrieval_strength:
          "balanced" as KnowledgeGraphConfig["retrieval_strength"],
        age_seed_mode:
          "entity_then_text" as KnowledgeGraphConfig["age_seed_mode"],
        age_traversal_hops: 0,
        age_candidate_pool_size: 16,
        age_dense_rerank_weight: 0.35,
        strict_age_retrieval_default: false,
      },
      recommended: false,
      age_required: false,
    },
    {
      id: "balanced",
      label: ageEnabled ? "Balanced GraphRAG + AGE" : "Balanced GraphRAG",
      eyebrow: "Recommended",
      description: ageEnabled
        ? "Use Apache AGE as the primary retrieval path with balanced traversal depth, moderate dense reranking, and graph evidence that stays easy to inspect."
        : "Use the default graph settings for day-to-day retrieval: grounded hybrid recall, moderate traversal, and dense reranking that stays easy to inspect.",
      badges: ageEnabled
        ? ["AGE primary", "1-hop traversal", "Balanced rerank"]
        : ["Object store", "1-hop local graph", "Balanced rerank"],
      patch: {
        default_retrieval_mode: (ageEnabled
          ? "age_graph"
          : "graph_hybrid") as KnowledgeGraphConfig["default_retrieval_mode"],
        output_target: (ageEnabled
          ? "object_store_and_age"
          : "object_store") as KnowledgeGraphConfig["output_target"],
        retrieval_strength:
          "balanced" as KnowledgeGraphConfig["retrieval_strength"],
        age_seed_mode:
          "entity_then_text" as KnowledgeGraphConfig["age_seed_mode"],
        age_traversal_hops: 1,
        age_candidate_pool_size: 24,
        age_dense_rerank_weight: 0.35,
        strict_age_retrieval_default: false,
      },
      recommended: true,
      age_required: false,
    },
    ...(ageEnabled
      ? [
          {
            id: "age_native" as const,
            label: "AGE-native retrieval",
            eyebrow: "Graph-first",
            description:
              "Prioritize Apache AGE as the primary retrieval path, walk deeper relationship trails, and use light dense reranking to keep the answer graph-native.",
            badges: ["AGE sync", "2-hop traversal", "Graph-first rerank"],
            patch: {
              default_retrieval_mode:
                "age_graph" as KnowledgeGraphConfig["default_retrieval_mode"],
              output_target:
                "object_store_and_age" as KnowledgeGraphConfig["output_target"],
              retrieval_strength:
                "aggressive" as KnowledgeGraphConfig["retrieval_strength"],
              age_seed_mode:
                "query_entities_and_text" as KnowledgeGraphConfig["age_seed_mode"],
              age_traversal_hops: 2,
              age_candidate_pool_size: 40,
              age_dense_rerank_weight: 0.2,
              strict_age_retrieval_default: false,
            },
            recommended: false,
            age_required: true,
          },
          {
            id: "age_strict" as const,
            label: "Strict AGE default",
            eyebrow: "Locked",
            description:
              "Save the version with AGE-backed retrieval locked in as the default path so graph-native runs do not silently fall back unless you override them.",
            badges: ["AGE primary", "Strict default", "No fallback"],
            patch: {
              default_retrieval_mode:
                "age_graph" as KnowledgeGraphConfig["default_retrieval_mode"],
              output_target:
                "object_store_and_age" as KnowledgeGraphConfig["output_target"],
              retrieval_strength:
                "aggressive" as KnowledgeGraphConfig["retrieval_strength"],
              age_seed_mode:
                "query_entities_and_text" as KnowledgeGraphConfig["age_seed_mode"],
              age_traversal_hops: 2,
              age_candidate_pool_size: 40,
              age_dense_rerank_weight: 0.2,
              strict_age_retrieval_default: true,
            },
            recommended: false,
            age_required: true,
          },
        ]
      : []),
  ];
}

function graphBuildPresetMatch(
  config: KnowledgeGraphConfig,
  presets: KnowledgeGraphBuildPreset[],
): GraphBuildPresetId {
  const match = presets.find((preset) =>
    Object.entries(preset.patch).every(([key, value]) => {
      const current = config[key as keyof KnowledgeGraphConfig];
      if (Array.isArray(value) && Array.isArray(current)) {
        return (
          value.length === current.length &&
          value.every((item, index) => item === current[index])
        );
      }
      return current === value;
    }),
  );
  return match?.id ?? "custom";
}

function humanSize(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`;
  const units = ["KB", "MB", "GB", "TB"];
  let value = bytes / 1024;
  let index = 0;
  while (value >= 1024 && index < units.length - 1) {
    value /= 1024;
    index += 1;
  }
  return `${value.toFixed(value < 10 ? 1 : 0)} ${units[index]}`;
}

function formatDate(value: string | null | undefined): string {
  if (!value) return "—";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return "—";
  return date.toLocaleString();
}

function chunkCount(version: KnowledgeBaseVersion | null | undefined): number {
  return Number(version?.summary?.chunk_count ?? 0);
}

function processedSourceCount(
  version: KnowledgeBaseVersion | null | undefined,
): number {
  return Number(version?.summary?.processed_sources ?? 0);
}

function entityCount(version: KnowledgeBaseVersion | null | undefined): number {
  return Number(version?.summary?.entity_count ?? 0);
}

function relationshipCount(
  version: KnowledgeBaseVersion | null | undefined,
): number {
  return Number(version?.summary?.relationship_count ?? 0);
}

function isVersionLive(
  version: Pick<KnowledgeBaseVersion, "status"> | null | undefined,
): boolean {
  return Boolean(
    version && (version.status === "queued" || version.status === "processing"),
  );
}

function isRunLive(
  run: Pick<KnowledgeBaseRun, "status"> | null | undefined,
): boolean {
  return Boolean(run && (run.status === "queued" || run.status === "running"));
}

function retrievalModeLabel(mode: string, fallback?: string): string {
  if (mode === "dense") return "Dense chunks";
  if (mode === "hybrid") return "Hybrid (keyword + vector)";
  if (mode === "graph_hybrid") return "GraphRAG hybrid";
  if (mode === "age_graph") return "Apache AGE graph";
  return fallback ?? mode;
}

// Compact one-word label for the Explore ask segmented control (Dense · Hybrid ·
// GraphRAG · AGE). Falls back to the full label for unknown modes.
function retrievalModeShortLabel(mode: string, fallback?: string): string {
  if (mode === "dense") return "Dense";
  if (mode === "hybrid") return "Hybrid";
  if (mode === "graph_hybrid") return "GraphRAG";
  if (mode === "age_graph") return "AGE";
  return fallback ?? retrievalModeLabel(mode);
}

function ageSeedModeLabel(
  mode: string | null | undefined,
  fallback?: string,
): string {
  if (mode === "entity_then_text") return "Entity first, then question text";
  if (mode === "query_entities_only") return "Extracted entities only";
  if (mode === "query_text_only") return "Question text only";
  if (mode === "query_entities_and_text") return "Entities plus question text";
  return fallback ?? "Entity first, then question text";
}

function ageSeedStrategyLabel(
  strategy: string | null | undefined,
): string | null {
  if (strategy === "query_text") return "Seeded from question text";
  if (strategy === "query_entities") return "Seeded from extracted entities";
  if (strategy === "query_entities_and_text")
    return "Seeded from entities + question text";
  return null;
}

function cloneGraphConfig(
  config: KnowledgeGraphConfig | null | undefined,
): KnowledgeGraphConfig {
  return {
    extractor_backend:
      config?.extractor_backend ?? FALLBACK_GRAPH_CONFIG.extractor_backend,
    spacy_model: config?.spacy_model ?? FALLBACK_GRAPH_CONFIG.spacy_model,
    max_entities_per_chunk: Number(
      config?.max_entities_per_chunk ??
        FALLBACK_GRAPH_CONFIG.max_entities_per_chunk,
    ),
    entity_types: [
      ...(config?.entity_types ?? FALLBACK_GRAPH_CONFIG.entity_types),
    ],
    minimum_entity_mentions: Number(
      config?.minimum_entity_mentions ??
        FALLBACK_GRAPH_CONFIG.minimum_entity_mentions,
    ),
    minimum_relationship_weight: Number(
      config?.minimum_relationship_weight ??
        FALLBACK_GRAPH_CONFIG.minimum_relationship_weight,
    ),
    default_retrieval_mode:
      config?.default_retrieval_mode ??
      FALLBACK_GRAPH_CONFIG.default_retrieval_mode,
    retrieval_strength:
      config?.retrieval_strength ?? FALLBACK_GRAPH_CONFIG.retrieval_strength,
    output_target: config?.output_target ?? FALLBACK_GRAPH_CONFIG.output_target,
    age_seed_mode: config?.age_seed_mode ?? FALLBACK_GRAPH_CONFIG.age_seed_mode,
    age_traversal_hops: Number(
      config?.age_traversal_hops ?? FALLBACK_GRAPH_CONFIG.age_traversal_hops,
    ),
    age_candidate_pool_size: Number(
      config?.age_candidate_pool_size ??
        FALLBACK_GRAPH_CONFIG.age_candidate_pool_size,
    ),
    age_dense_rerank_weight: Number(
      config?.age_dense_rerank_weight ??
        FALLBACK_GRAPH_CONFIG.age_dense_rerank_weight,
    ),
    strict_age_retrieval_default:
      typeof config?.strict_age_retrieval_default === "boolean"
        ? config.strict_age_retrieval_default
        : FALLBACK_GRAPH_CONFIG.strict_age_retrieval_default,
  };
}

function preferAgeGraphTarget(
  config: KnowledgeGraphConfig | null | undefined,
  ageEnabled: boolean,
): KnowledgeGraphConfig {
  const next = cloneGraphConfig(config);
  if (ageEnabled && next.output_target !== "object_store_and_age") {
    next.output_target = "object_store_and_age";
    next.default_retrieval_mode = "age_graph";
  }
  return next;
}

function graphOverridesFromConfig(
  config: KnowledgeGraphConfig | null | undefined,
): KnowledgeQueryGraphOverrides {
  const resolved = cloneGraphConfig(config);
  return {
    retrieval_strength: resolved.retrieval_strength,
    minimum_relationship_weight: resolved.minimum_relationship_weight,
    age_seed_mode: resolved.age_seed_mode,
    age_traversal_hops: resolved.age_traversal_hops,
    age_candidate_pool_size: resolved.age_candidate_pool_size,
    age_dense_rerank_weight: resolved.age_dense_rerank_weight,
    strict_age_retrieval: Boolean(resolved.strict_age_retrieval_default),
  };
}

function strictAgeGraphOverridesFromConfig(
  config: KnowledgeGraphConfig | null | undefined,
): KnowledgeQueryGraphOverrides {
  return {
    ...graphOverridesFromConfig(config),
    strict_age_retrieval: true,
  };
}

function buildGraphOverridesPayload(
  enabled: boolean,
  overrides: KnowledgeQueryGraphOverrides,
  includeAgeControls: boolean,
): KnowledgeQueryGraphOverrides | undefined {
  if (!enabled) return undefined;
  const payload: KnowledgeQueryGraphOverrides = {};
  if (overrides.retrieval_strength)
    payload.retrieval_strength = overrides.retrieval_strength;
  if (typeof overrides.minimum_relationship_weight === "number") {
    payload.minimum_relationship_weight = overrides.minimum_relationship_weight;
  }
  if (includeAgeControls && overrides.age_seed_mode) {
    payload.age_seed_mode = overrides.age_seed_mode;
  }
  if (includeAgeControls && typeof overrides.age_traversal_hops === "number") {
    payload.age_traversal_hops = overrides.age_traversal_hops;
  }
  if (
    includeAgeControls &&
    typeof overrides.age_candidate_pool_size === "number"
  ) {
    payload.age_candidate_pool_size = overrides.age_candidate_pool_size;
  }
  if (
    includeAgeControls &&
    typeof overrides.age_dense_rerank_weight === "number"
  ) {
    payload.age_dense_rerank_weight = overrides.age_dense_rerank_weight;
  }
  if (includeAgeControls && overrides.strict_age_retrieval) {
    payload.strict_age_retrieval = true;
  }
  return Object.keys(payload).length > 0 ? payload : undefined;
}

function configuredDefaultRetrievalMode(
  config:
    | Pick<KnowledgeGraphConfig, "default_retrieval_mode" | "output_target">
    | null
    | undefined,
  ageEnabled: boolean,
): KnowledgeRetrievalMode {
  const mode = config?.default_retrieval_mode ?? "graph_hybrid";
  if (
    mode === "age_graph" &&
    (!ageEnabled || config?.output_target !== "object_store_and_age")
  ) {
    return "graph_hybrid";
  }
  return mode;
}

function graphTargetLabel(target: string): string {
  if (target === "object_store_and_age") return "Object store + Apache AGE";
  return "Object store only";
}

function ageSyncStatus(
  version: Pick<KnowledgeBaseVersion, "summary"> | null | undefined,
): string {
  return String(version?.summary?.age_sync_status ?? "skipped").toLowerCase();
}

function isAgeConfiguredVersion(
  version:
    | Pick<KnowledgeBaseVersion, "graph_config" | "summary">
    | null
    | undefined,
  ageEnabled: boolean,
): boolean {
  return Boolean(
    ageEnabled &&
    version?.graph_config.output_target === "object_store_and_age",
  );
}

function isAgeReadyVersion(
  version:
    | Pick<KnowledgeBaseVersion, "graph_config" | "summary">
    | null
    | undefined,
  ageEnabled: boolean,
): boolean {
  return Boolean(
    isAgeConfiguredVersion(version, ageEnabled) &&
    ageSyncStatus(version) === "synced",
  );
}

function ageTraversalLabel(hops: number): string {
  if (hops <= 0) return "Direct entities only";
  if (hops === 1) return "One-hop expansion";
  return "Two-hop expansion";
}

function graphConfigSummary(
  version: KnowledgeBaseVersion,
  ageEnabled: boolean,
): string {
  const modes = ["Dense", "GraphRAG hybrid"];
  if (isAgeConfiguredVersion(version, ageEnabled)) {
    modes.push(
      isAgeReadyVersion(version, ageEnabled)
        ? "Apache AGE graph"
        : "Apache AGE graph (pending)",
    );
  }
  return modes.join(" + ");
}

function defaultPlaygroundRetrievalModesForVersion(
  version:
    | Pick<KnowledgeBaseVersion, "graph_config" | "summary">
    | null
    | undefined,
  ageEnabled: boolean,
): KnowledgeRetrievalMode[] {
  const defaultMode = configuredDefaultRetrievalMode(
    version?.graph_config,
    ageEnabled,
  );
  if (defaultMode === "age_graph" && !isAgeReadyVersion(version, ageEnabled)) {
    return ["graph_hybrid"];
  }
  return [defaultMode];
}

function graphModeTone(mode: KnowledgeRetrievalMode): string {
  if (mode === "graph_hybrid") return "bg-blue-50 text-blue-700";
  if (mode === "age_graph") return "bg-emerald-50 text-emerald-700";
  return "bg-slate-100 text-slate-600";
}

function graphStatusTone(status: string): string {
  if (status === "synced")
    return "border-emerald-200 bg-emerald-50 text-emerald-700";
  if (status === "failed") return "border-amber-200 bg-amber-50 text-amber-700";
  if (status === "skipped")
    return "border-slate-200 bg-slate-100 text-slate-600";
  return "border-blue-200 bg-blue-50 text-blue-700";
}

function graphStatusLabel(status: string): string {
  if (status === "synced") return "AGE synced";
  if (status === "failed") return "AGE sync failed";
  if (status === "skipped") return "Object-store graph";
  if (status === "pending") return "Sync queued";
  return "Graph status";
}

function libraryVersionSummary(
  knowledgeBase: Pick<KnowledgeBase, "active_version_summary">,
): KnowledgeBaseActiveVersionSummary | null {
  return knowledgeBase.active_version_summary ?? null;
}

// True when the active version's build is still in flight — drives the calm
// library card's single health signal ("building…" vs the chunk/entity line).
function kbSummaryBuilding(
  summary: KnowledgeBaseActiveVersionSummary,
): boolean {
  return ["queued", "processing", "running", "pending"].includes(
    String(summary.status ?? ""),
  );
}

function ageSyncActionLabel(
  version: Pick<KnowledgeBaseVersion, "graph_config" | "summary">,
  ageEnabled: boolean,
): string {
  if (isAgeReadyVersion(version, ageEnabled)) return "Resync to AGE";
  const status = ageSyncStatus(version);
  if (status === "failed") return "Retry AGE sync";
  if (version.graph_config.output_target !== "object_store_and_age") {
    return "Enable AGE graph retrieval";
  }
  return "Sync to AGE";
}

function objectStoreObjectPath(bucket: string, key: string): string {
  return `/object-store?bucket=${encodeURIComponent(bucket)}&key=${encodeURIComponent(key)}`;
}

function objectStorePrefixPath(bucket: string, prefix: string): string {
  return `/object-store?bucket=${encodeURIComponent(bucket)}&prefix=${encodeURIComponent(prefix)}`;
}

function versionArtifactPath(
  version: KnowledgeBaseVersion,
  fileName: string,
): string {
  return objectStoreObjectPath(
    version.output_bucket,
    `${version.output_prefix}/${fileName}`,
  );
}

function suiteServiceHref(port: number, path = "/"): string {
  if (typeof window === "undefined") return path;
  const url = new URL(window.location.href);
  url.port = String(port);
  url.pathname = path;
  url.search = "";
  url.hash = "";
  return appendThemeHintToUrl(url.toString());
}

function graphConfigsEqual(
  left: KnowledgeGraphConfig,
  right: KnowledgeGraphConfig,
): boolean {
  return (
    left.extractor_backend === right.extractor_backend &&
    left.spacy_model === right.spacy_model &&
    left.max_entities_per_chunk === right.max_entities_per_chunk &&
    left.minimum_entity_mentions === right.minimum_entity_mentions &&
    left.minimum_relationship_weight === right.minimum_relationship_weight &&
    left.default_retrieval_mode === right.default_retrieval_mode &&
    left.retrieval_strength === right.retrieval_strength &&
    left.output_target === right.output_target &&
    left.age_seed_mode === right.age_seed_mode &&
    left.age_traversal_hops === right.age_traversal_hops &&
    left.age_candidate_pool_size === right.age_candidate_pool_size &&
    left.age_dense_rerank_weight === right.age_dense_rerank_weight &&
    Boolean(left.strict_age_retrieval_default) ===
      Boolean(right.strict_age_retrieval_default) &&
    left.entity_types.length === right.entity_types.length &&
    left.entity_types.every((item, index) => item === right.entity_types[index])
  );
}


export function KnowledgeBases(): JSX.Element {
  const [searchParams, setSearchParams] = useSearchParams();
  const invalidate = useInvalidate();
  // ── Asset-workspace IA (Phase R1) ──────────────────────────────────────────
  // Library is the landing list (openKnowledgeBaseId === null). Opening a KB
  // enters its Workspace: a header (name · status · version switcher · set
  // active · back) over four stage tabs (Build · Explore · Calibrate · Use).
  // The header version switcher drives ``inspectedVersionId`` (the version the
  // stages operate on); Explore hosts an Ask/Chunks/Graph sub-nav.
  const [openKnowledgeBaseId, setOpenKnowledgeBaseId] = useState<string | null>(
    null,
  );
  // Create-mode Workspace: the Build stage hosts a brand-new build/corpus with
  // no KB resolved yet (header shows a "New knowledge base" placeholder).
  const [creatingKnowledgeBase, setCreatingKnowledgeBase] = useState(false);
  const [workspaceStage, setWorkspaceStage] = useState<WorkspaceStage>("build");
  const [exploreView, setExploreView] = useState<ExploreView>("ask");
  const [search, setSearch] = useState("");
  // Library status filter; defaults to the empty "All" sentinel so the default
  // library view is unchanged. Additive with the text search.
  const [libraryStatusFilter, setLibraryStatusFilter] = useState("");
  const [libraryOwnerFilter, setLibraryOwnerFilter] = useState("");
  const [libraryViewMode, setLibraryViewMode] = useViewMode("knowledge-bases");
  const [selectedKnowledgeBaseId, setSelectedKnowledgeBaseId] = useState<
    string | null
  >(null);
  const [inspectedVersionId, setInspectedVersionId] = useState<string | null>(
    null,
  );
  const [selectedRunId, setSelectedRunId] = useState<string | null>(null);
  const [playgroundVersionIds, setPlaygroundVersionIds] = useState<string[]>(
    [],
  );
  const [buildMode, setBuildMode] = useState<BuildMode>("new");
  const [newName, setNewName] = useState("");
  const [newDescription, setNewDescription] = useState("");
  const [sourceBucket, setSourceBucket] = useState("");
  const [sourceSearch, setSourceSearch] = useState("");
  const [selectedSources, setSelectedSources] = useState<
    KnowledgeSourceSelection[]
  >([]);
  const [chunkingStrategy, setChunkingStrategy] = useState("recursive");
  const [embeddingModel, setEmbeddingModel] = useState("BAAI/bge-m3");
  const [chunkSize, setChunkSize] = useState(1200);
  const [chunkOverlap, setChunkOverlap] = useState(180);
  const [semanticThreshold, setSemanticThreshold] = useState(0.78);
  const [graphConfig, setGraphConfig] = useState<KnowledgeGraphConfig>(
    FALLBACK_GRAPH_CONFIG,
  );
  const [appliedGraphConfigSeed, setAppliedGraphConfigSeed] = useState("");
  const [buildError, setBuildError] = useState<string | null>(null);
  const [buildBusy, setBuildBusy] = useState(false);
  const [ageSyncBusy, setAgeSyncBusy] = useState(false);
  const [ageSyncError, setAgeSyncError] = useState<string | null>(null);
  const [compareQuestion, setCompareQuestion] = useState("");
  const [playgroundTopK, setPlaygroundTopK] = useState(6);
  const [playgroundRetrievalModes, setPlaygroundRetrievalModes] = useState<
    KnowledgeRetrievalMode[]
  >([]);
  const [playgroundGraphTuningEnabled, setPlaygroundGraphTuningEnabled] =
    useState(false);
  const [playgroundGraphOverrides, setPlaygroundGraphOverrides] =
    useState<KnowledgeQueryGraphOverrides>(
      graphOverridesFromConfig(FALLBACK_GRAPH_CONFIG),
    );
  const [queryBusy, setQueryBusy] = useState(false);
  const [queryError, setQueryError] = useState<string | null>(null);
  // R3 declutter: the Explore Query view defaults to a clean ask box against the
  // header-selected version. Multi-version selection + the compare panel live
  // behind this disclosure; while it is closed the query tracks the header
  // version (``inspectedVersionId``) so collapsing never silently changes scope.
  const [playgroundCompareOpen, setPlaygroundCompareOpen] = useState(false);
  // Confirm-gated, admin-only KB deletion (mirrors the MCP/agent delete UX). The
  // id being confirmed doubles as the "confirm armed" flag for that one card.
  const [pendingDeleteKnowledgeBaseId, setPendingDeleteKnowledgeBaseId] =
    useState<string | null>(null);
  const [deleteBusy, setDeleteBusy] = useState(false);
  const [deleteError, setDeleteError] = useState<string | null>(null);
  const [selectedKbIds, setSelectedKbIds] = useState<Set<string>>(new Set());
  const [bulkDeleteOpen, setBulkDeleteOpen] = useState(false);
  const [bulkDeleteBusy, setBulkDeleteBusy] = useState(false);
  const [bulkDeleteError, setBulkDeleteError] = useState<string | null>(null);
  const toggleKbSelected = (knowledgeBaseId: string): void => {
    setSelectedKbIds((current) => {
      const next = new Set(current);
      if (next.has(knowledgeBaseId)) {
        next.delete(knowledgeBaseId);
      } else {
        next.add(knowledgeBaseId);
      }
      return next;
    });
  };
  const [chatTurns, setChatTurns] = useState<ChatTurn[]>([]);
  const [chunkSearch, setChunkSearch] = useState("");
  const [chunkSourceFilter, setChunkSourceFilter] = useState("");
  const [graphSearch, setGraphSearch] = useState("");
  const [graphRequestedSource, setGraphRequestedSource] =
    useState<KnowledgeGraphExploreSource>("local");
  const [graphRetrievalStrength, setGraphRetrievalStrength] =
    useState<KnowledgeGraphConfig["retrieval_strength"]>("balanced");
  const [graphTraversalHops, setGraphTraversalHops] = useState(1);
  const [graphAgeSeedMode, setGraphAgeSeedMode] =
    useState<KnowledgeAgeSeedMode>("entity_then_text");
  const [graphMinRelationshipWeight, setGraphMinRelationshipWeight] =
    useState(1);
  const [graphAgeCandidatePool, setGraphAgeCandidatePool] = useState(24);
  const [graphAgeDenseRerankWeight, setGraphAgeDenseRerankWeight] =
    useState(0.35);
  const [graphNodeLimit, setGraphNodeLimit] = useState(12);
  const [graphEntityTypeFilter, setGraphEntityTypeFilter] = useState("");
  const [selectedGraphEntityId, setSelectedGraphEntityId] = useState<
    string | null
  >(null);
  const [graphProbeQuestion, setGraphProbeQuestion] = useState("");
  const [graphProbeStrictAge, setGraphProbeStrictAge] = useState(false);
  const [graphProbeBusy, setGraphProbeBusy] = useState(false);
  const [graphProbeError, setGraphProbeError] = useState<string | null>(null);
  const [graphProbeResult, setGraphProbeResult] =
    useState<KnowledgeQueryVersionResult | null>(null);
  const [buildLaunchSelection, setBuildLaunchSelection] =
    useState<KnowledgeBuildLaunchPayload | null>(null);
  const [pendingBuildLaunchPreset, setPendingBuildLaunchPreset] =
    useState<KnowledgeBuildLaunchPreset | null>(null);
  const playgroundSeedOverrideRef = useRef<PendingPlaygroundSeed | null>(null);
  const appliedPlaygroundSeedSignatureRef = useRef("");
  const consumedBuildLaunchRef = useRef<string | null>(null);

  // Open a KB into its Workspace, optionally landing on a specific stage.
  const openKnowledgeBaseWorkspace = useCallback(
    (knowledgeBaseId: string, stage: WorkspaceStage = "build"): void => {
      setOpenKnowledgeBaseId(knowledgeBaseId);
      setSelectedKnowledgeBaseId(knowledgeBaseId);
      setWorkspaceStage(stage);
    },
    [],
  );

  // ── Compatibility shim ─────────────────────────────────────────────────────
  // The existing section handlers (``openPlaygroundForVersion``, the version
  // table, the library cards, the guidance panels, …) all navigate by calling
  // ``setActiveTab("playground" | "graph" | "versions" | "build" | "runs" |
  // "library")``. Rather than rewrite every call site, we re-point that name at
  // a mapper onto the new stage + Explore sub-view, opening the Workspace when a
  // KB is in context. Render gating below reads the stage/view, not this value.
  const setActiveTab = useCallback(
    (tab: KnowledgeTab): void => {
      if (tab === "library") {
        setOpenKnowledgeBaseId(null);
        setCreatingKnowledgeBase(false);
        return;
      }
      // Ensure a Workspace is open so the stage has something to render against.
      // When no KB is in context (e.g. a brand-new build launch), fall into
      // create mode rather than silently no-op'ing.
      setOpenKnowledgeBaseId((current) => current ?? selectedKnowledgeBaseId);
      if (!openKnowledgeBaseId && !selectedKnowledgeBaseId) {
        setCreatingKnowledgeBase(true);
      }
      switch (tab) {
        case "build":
        case "runs":
          setWorkspaceStage("build");
          break;
        case "versions":
          setWorkspaceStage("use");
          break;
        case "calibrate":
          setWorkspaceStage("calibrate");
          break;
        case "playground":
          setWorkspaceStage("explore");
          setExploreView("ask");
          break;
        case "graph":
          setWorkspaceStage("explore");
          setExploreView("graph");
          break;
      }
    },
    [openKnowledgeBaseId, selectedKnowledgeBaseId],
  );

  const knowledgeOptionsQuery = useApiQuery<KnowledgeOptions>(
    ["knowledge-bases", "options"],
    (signal) => caliberApi.getKnowledgeOptions(signal),
  );
  // Deleting a KB is an admin-only operation on the backend; gate the destructive
  // affordances so non-admins never see them (mirrors the MCP/agent pages).
  const meQuery = useApiQuery<CurrentUserInfo>(["me"], (signal) =>
    caliberApi.getMe(signal),
  );
  const isAdmin = meQuery.data?.is_admin ?? false;
  const knowledgeBasesQuery = useApiQuery<KnowledgeBase[]>(
    ["knowledge-bases", "list"],
    (signal) => caliberApi.listKnowledgeBases({ status: "all" }, signal),
    {
      refetchInterval: (query) =>
        (query.state.data ?? []).some(
          (item) =>
            item.last_run_status === "queued" ||
            item.last_run_status === "processing",
        )
          ? 2000
          : false,
    },
  );
  const bucketsQuery = useApiQuery<ObjectStoreBucket[]>(
    ["knowledge-bases", "object-store", "buckets"],
    (signal) => caliberApi.listObjectStoreBuckets(signal),
  );
  const versionsQuery = useApiQuery<KnowledgeBaseVersion[]>(
    ["knowledge-bases", selectedKnowledgeBaseId ?? "", "versions"],
    (signal) =>
      caliberApi.listKnowledgeBaseVersions(selectedKnowledgeBaseId!, signal),
    {
      enabled: Boolean(selectedKnowledgeBaseId),
      refetchInterval: (query) =>
        (query.state.data ?? []).some(
          (item) => item.status === "queued" || item.status === "processing",
        )
          ? 2000
          : false,
    },
  );
  const runsQuery = useApiQuery<KnowledgeBaseRun[]>(
    ["knowledge-bases", selectedKnowledgeBaseId ?? "", "runs"],
    (signal) =>
      caliberApi.listKnowledgeBaseRuns(selectedKnowledgeBaseId!, signal),
    {
      enabled: Boolean(selectedKnowledgeBaseId),
      refetchInterval: (query) =>
        (query.state.data ?? []).some(
          (item) => item.status === "queued" || item.status === "running",
        )
          ? 2000
          : false,
    },
  );
  const sourcesQuery = useApiQuery<KnowledgeBaseSource[]>(
    ["knowledge-bases", inspectedVersionId ?? "", "sources"],
    (signal) =>
      caliberApi.listKnowledgeBaseSources(inspectedVersionId!, signal),
    { enabled: Boolean(inspectedVersionId) },
  );
  const chunksQuery = useApiQuery<KnowledgeBaseChunk[]>(
    [
      "knowledge-bases",
      inspectedVersionId ?? "",
      "chunks",
      chunkSearch,
      chunkSourceFilter,
    ],
    (signal) =>
      caliberApi.listKnowledgeBaseChunks(
        inspectedVersionId!,
        {
          q: chunkSearch || undefined,
          sourceKey: chunkSourceFilter || undefined,
          limit: 150,
        },
        signal,
      ),
    {
      enabled: Boolean(inspectedVersionId),
      refetchInterval: () =>
        (versionsQuery.data ?? []).some(
          (item) =>
            item.knowledge_base_version_id === inspectedVersionId &&
            isVersionLive(item),
        )
          ? 2000
          : false,
    },
  );
  const entitiesQuery = useApiQuery<KnowledgeBaseEntity[]>(
    ["knowledge-bases", inspectedVersionId ?? "", "entities"],
    (signal) =>
      caliberApi.listKnowledgeBaseEntities(inspectedVersionId!, signal),
    {
      enabled: Boolean(inspectedVersionId),
      refetchInterval: () =>
        (versionsQuery.data ?? []).some(
          (item) =>
            item.knowledge_base_version_id === inspectedVersionId &&
            isVersionLive(item),
        )
          ? 2000
          : false,
    },
  );
  const relationshipsQuery = useApiQuery<KnowledgeBaseRelationship[]>(
    ["knowledge-bases", inspectedVersionId ?? "", "relationships"],
    (signal) =>
      caliberApi.listKnowledgeBaseRelationships(inspectedVersionId!, signal),
    {
      enabled: Boolean(inspectedVersionId),
      refetchInterval: () =>
        (versionsQuery.data ?? []).some(
          (item) =>
            item.knowledge_base_version_id === inspectedVersionId &&
            isVersionLive(item),
        )
          ? 2000
          : false,
    },
  );
  const runEventsQuery = useApiQuery<KnowledgeBaseRunEvent[]>(
    ["knowledge-bases", selectedRunId ?? "", "events"],
    (signal) => caliberApi.listKnowledgeBaseRunEvents(selectedRunId!, signal),
    {
      enabled: Boolean(selectedRunId),
      refetchInterval: () =>
        (runsQuery.data ?? []).some(
          (item) =>
            item.knowledge_base_run_id === selectedRunId && isRunLive(item),
        )
          ? 2000
          : false,
    },
  );

  const knowledgeBases = knowledgeBasesQuery.data ?? EMPTY_KNOWLEDGE_BASES;
  const selectedKnowledgeBase = useMemo(
    () =>
      knowledgeBases.find(
        (item) => item.knowledge_base_id === selectedKnowledgeBaseId,
      ) ?? null,
    [knowledgeBases, selectedKnowledgeBaseId],
  );
  const versions = versionsQuery.data ?? EMPTY_VERSIONS;
  // Memoize the adapter so the shared <VersionPanel> only reloads when the
  // active KB id changes (its internal effect depends on adapter identity).
  const knowledgeBaseVersionAdapter = useMemo(
    () => makeKnowledgeBaseVersionAdapter(selectedKnowledgeBaseId ?? ""),
    [selectedKnowledgeBaseId],
  );
  const runs = runsQuery.data ?? EMPTY_RUNS;
  const selectedRun = useMemo(
    () =>
      runs.find((item) => item.knowledge_base_run_id === selectedRunId) ?? null,
    [runs, selectedRunId],
  );
  const selectedVersion = useMemo(
    () =>
      versions.find(
        (item) => item.knowledge_base_version_id === inspectedVersionId,
      ) ?? null,
    [versions, inspectedVersionId],
  );
  const strategyMap = useMemo(
    () =>
      new Map(
        (knowledgeOptionsQuery.data?.chunking_strategies ?? []).map((item) => [
          item.id,
          item,
        ]),
      ),
    [knowledgeOptionsQuery.data],
  );
  const embeddingMap = useMemo(
    () =>
      new Map(
        (knowledgeOptionsQuery.data?.embedding_models ?? []).map((item) => [
          item.id,
          item,
        ]),
      ),
    [knowledgeOptionsQuery.data],
  );
  const embeddingOptions = useMemo(
    () => knowledgeOptionsQuery.data?.embedding_models ?? [],
    [knowledgeOptionsQuery.data?.embedding_models],
  );
  const selectedEmbeddingOption = embeddingMap.get(embeddingModel) ?? null;
  const embeddingBlockedReason = useMemo(
    () =>
      embeddingOptions.find((item) => item.available === false)
        ?.unavailable_reason ?? null,
    [embeddingOptions],
  );
  const embeddingSelectionBlockedReason =
    selectedEmbeddingOption?.available === false
      ? selectedEmbeddingOption.unavailable_reason ??
        "This embedding model is unavailable in the current runtime."
      : null;
  const hasAvailableEmbeddingOptions = embeddingOptions.some(
    (item) => item.available !== false,
  );
  const retrievalModeMap = useMemo(
    () =>
      new Map(
        (knowledgeOptionsQuery.data?.retrieval_modes ?? []).map((item) => [
          item.id,
          item,
        ]),
      ),
    [knowledgeOptionsQuery.data],
  );
  const graphExtractorMap = useMemo(
    () =>
      new Map(
        (knowledgeOptionsQuery.data?.graph_extractors ?? []).map((item) => [
          item.id,
          item,
        ]),
      ),
    [knowledgeOptionsQuery.data],
  );
  const graphOutputTargetMap = useMemo(
    () =>
      new Map(
        (knowledgeOptionsQuery.data?.graph_output_targets ?? []).map((item) => [
          item.id,
          item,
        ]),
      ),
    [knowledgeOptionsQuery.data],
  );
  const graphRetrievalStrengthMap = useMemo(
    () =>
      new Map(
        (knowledgeOptionsQuery.data?.graph_retrieval_strengths ?? []).map(
          (item) => [item.id, item],
        ),
      ),
    [knowledgeOptionsQuery.data],
  );
  const graphAgeSeedModeMap = useMemo(
    () =>
      new Map(
        (knowledgeOptionsQuery.data?.graph_age_seed_modes ?? []).map((item) => [
          item.id,
          item,
        ]),
      ),
    [knowledgeOptionsQuery.data],
  );
  const graphHybridModeOption = retrievalModeMap.get("graph_hybrid");
  const ageGraphModeOption = retrievalModeMap.get("age_graph");
  const ageEnabled = Boolean(knowledgeOptionsQuery.data?.age_enabled);
  const ageUnavailableReason =
    knowledgeOptionsQuery.data?.age_unavailable_reason ??
    "Apache AGE is unavailable in this deployment.";
  const ageViewerHref = useMemo(() => {
    const configured = knowledgeOptionsQuery.data?.age_viewer_url;
    return configured
      ? appendThemeHintToUrl(configured)
      : suiteServiceHref(8082);
  }, [knowledgeOptionsQuery.data?.age_viewer_url]);
  const buildGraphPresets = useMemo(
    () =>
      knowledgeOptionsQuery.data?.graph_build_presets?.length
        ? knowledgeOptionsQuery.data.graph_build_presets
        : fallbackGraphBuildPresets(ageEnabled),
    [ageEnabled, knowledgeOptionsQuery.data?.graph_build_presets],
  );
  const graphQueryPresets = useMemo(
    () =>
      knowledgeOptionsQuery.data?.graph_query_presets?.length
        ? knowledgeOptionsQuery.data.graph_query_presets
        : fallbackGraphQueryPresets(ageEnabled),
    [ageEnabled, knowledgeOptionsQuery.data?.graph_query_presets],
  );
  const activeGraphBuildPreset = graphBuildPresetMatch(
    graphConfig,
    buildGraphPresets,
  );
  const activeBuildGraphPresetDefinition =
    buildGraphPresets.find((preset) => preset.id === activeGraphBuildPreset) ??
    null;
  const buildUsesAgePrimary = Boolean(
    ageEnabled &&
    graphConfig.output_target === "object_store_and_age" &&
    graphConfig.default_retrieval_mode === "age_graph",
  );
  const buildUsesStrictAgeDefault = Boolean(
    buildUsesAgePrimary && graphConfig.strict_age_retrieval_default,
  );
  const buildGraphProfileLabel =
    activeGraphBuildPreset === "custom"
      ? "Custom graph profile"
      : (activeBuildGraphPresetDefinition?.label ?? "GraphRAG profile");
  const buildGraphProfileDescription =
    activeGraphBuildPreset === "custom"
      ? "Hand-tuned graph extraction and retrieval settings for this build."
      : (activeBuildGraphPresetDefinition?.description ??
        "Configure graph extraction, lineage, and retrieval for this build.");
  const latestVersionGraphConfig =
    versionsQuery.data?.[0]?.graph_config ?? null;
  const buildGraphTargetAutoUpgraded = Boolean(
    buildMode === "existing" &&
    knowledgeOptionsQuery.data?.age_enabled &&
    latestVersionGraphConfig &&
    latestVersionGraphConfig.output_target !== "object_store_and_age" &&
    graphConfig.output_target === "object_store_and_age",
  );
  const graphConfigSeedSignature = useMemo(
    () =>
      JSON.stringify({
        buildMode,
        selectedKnowledgeBaseId:
          buildMode === "existing"
            ? (selectedKnowledgeBase?.knowledge_base_id ?? "")
            : "",
        ageEnabled,
        seed:
          buildMode === "existing" && latestVersionGraphConfig
            ? latestVersionGraphConfig
            : (knowledgeOptionsQuery.data?.default_graph_config ??
              FALLBACK_GRAPH_CONFIG),
      }),
    [
      ageEnabled,
      buildMode,
      knowledgeOptionsQuery.data?.default_graph_config,
      latestVersionGraphConfig,
      selectedKnowledgeBase?.knowledge_base_id,
    ],
  );

  useEffect(() => {
    const signature = searchParams.toString();
    if (!signature || consumedBuildLaunchRef.current === signature) return;
    const launchSelection = parseKnowledgeBuildLaunchParams(searchParams);
    if (!launchSelection) return;
    consumedBuildLaunchRef.current = signature;
    // Object-store launch deep-links open a create-mode Workspace on the Build
    // stage (a brand-new build seeded from the chosen bucket/sources).
    setCreatingKnowledgeBase(true);
    setWorkspaceStage("build");
    setBuildMode(launchSelection.buildMode);
    setSourceBucket(launchSelection.bucket);
    setSelectedSources(launchSelection.sources);
    setBuildLaunchSelection(launchSelection);
    setPendingBuildLaunchPreset(launchSelection.graphPreset ?? "balanced");
    setBuildError(null);
    setAgeSyncError(null);
    setQueryError(null);
    setSearchParams(stripKnowledgeBuildLaunchParams(searchParams), {
      replace: true,
    });
  }, [searchParams, setSearchParams]);

  useEffect(() => {
    // Skip while starting a new knowledge base: the "New knowledge base"
    // button deliberately clears `selectedKnowledgeBaseId` so it cannot leak
    // in as an implicit "New version" target (see that handler). Re-seeding
    // it here on the very next render would immediately undo that.
    if (creatingKnowledgeBase) return;
    const firstKnowledgeBase = knowledgeBases[0];
    if (!selectedKnowledgeBaseId && firstKnowledgeBase) {
      setSelectedKnowledgeBaseId(firstKnowledgeBase.knowledge_base_id);
    }
  }, [creatingKnowledgeBase, knowledgeBases, selectedKnowledgeBaseId]);

  useEffect(() => {
    if (!selectedKnowledgeBase) return;
    if (buildMode === "existing") {
      setSourceBucket(selectedKnowledgeBase.source_bucket);
      const manifest = (selectedKnowledgeBase.source_manifest ?? []) as Array<
        Record<string, unknown>
      >;
      setSelectedSources(
        manifest
          .map((item) => ({
            kind: String(item.kind) as "file" | "folder",
            path: String(item.path ?? ""),
          }))
          .filter((item) => item.path),
      );
    }
  }, [buildMode, selectedKnowledgeBase]);

  useEffect(() => {
    if (!knowledgeOptionsQuery.data) return;
    if (graphConfigSeedSignature === appliedGraphConfigSeed) return;
    let nextGraphConfig: KnowledgeGraphConfig | null = null;
    if (buildMode === "existing" && latestVersionGraphConfig) {
      nextGraphConfig = preferAgeGraphTarget(
        latestVersionGraphConfig,
        Boolean(knowledgeOptionsQuery.data.age_enabled),
      );
    } else if (buildMode === "new") {
      nextGraphConfig = cloneGraphConfig(
        knowledgeOptionsQuery.data.default_graph_config,
      );
    }
    if (!nextGraphConfig) return;
    setAppliedGraphConfigSeed(graphConfigSeedSignature);
    setGraphConfig((current) =>
      graphConfigsEqual(current, nextGraphConfig as KnowledgeGraphConfig)
        ? current
        : (nextGraphConfig as KnowledgeGraphConfig),
    );
  }, [
    appliedGraphConfigSeed,
    buildMode,
    graphConfigSeedSignature,
    knowledgeOptionsQuery.data,
    latestVersionGraphConfig,
  ]);

  useEffect(() => {
    if (!knowledgeOptionsQuery.data) return;
    const current = strategyMap.get(chunkingStrategy);
    if (!current) return;
    setChunkSize((currentSize) => {
      const nextChunkSize = Number(current.defaults.chunk_size ?? currentSize);
      return Number.isFinite(nextChunkSize) ? nextChunkSize : 1200;
    });
    setChunkOverlap((currentOverlap) => {
      const nextChunkOverlap = Number(
        current.defaults.chunk_overlap ?? currentOverlap,
      );
      return Number.isFinite(nextChunkOverlap) ? nextChunkOverlap : 180;
    });
    setSemanticThreshold((currentThreshold) => {
      const nextThreshold = Number(
        current.defaults.semantic_similarity_threshold ?? currentThreshold,
      );
      return Number.isFinite(nextThreshold) ? nextThreshold : 0.78;
    });
  }, [chunkingStrategy, knowledgeOptionsQuery.data, strategyMap]);

  useEffect(() => {
    const available = (knowledgeOptionsQuery.data?.retrieval_modes ?? []).map(
      (item) => item.id,
    ) as KnowledgeRetrievalMode[];
    if (available.length === 0) return;
    setPlaygroundRetrievalModes((current) => {
      if (current.length === 0) return current;
      return current.filter((item) => available.includes(item));
    });
  }, [knowledgeOptionsQuery.data]);

  useEffect(() => {
    if (versions.length === 0) {
      if (inspectedVersionId !== null) {
        setInspectedVersionId(null);
      }
      if (playgroundVersionIds.length > 0) {
        setPlaygroundVersionIds([]);
      }
      return;
    }
    const firstVersion = versions[0];
    if (!firstVersion) return;
    const active = selectedKnowledgeBase?.active_version_id;
    if (
      !inspectedVersionId ||
      !versions.some(
        (version) => version.knowledge_base_version_id === inspectedVersionId,
      )
    ) {
      setInspectedVersionId(
        active &&
          versions.some(
            (version) => version.knowledge_base_version_id === active,
          )
          ? active
          : firstVersion.knowledge_base_version_id,
      );
    }
    if (playgroundVersionIds.length === 0) {
      const defaults =
        active &&
        versions.some((version) => version.knowledge_base_version_id === active)
          ? [active]
          : [firstVersion.knowledge_base_version_id];
      setPlaygroundVersionIds(defaults);
    }
  }, [
    versions,
    inspectedVersionId,
    playgroundVersionIds.length,
    selectedKnowledgeBase,
  ]);

  // While "Compare versions" is collapsed, the simplified Query view operates on
  // the header-selected version: keep the (otherwise multi-select) playground
  // version set pinned to ``inspectedVersionId`` so the same ``askKnowledge``
  // path runs against the version the header switcher shows. Opening Compare
  // releases this pin so the operator can pick a second version.
  useEffect(() => {
    if (playgroundCompareOpen) return;
    if (!inspectedVersionId) return;
    setPlaygroundVersionIds((current) =>
      current.length === 1 && current[0] === inspectedVersionId
        ? current
        : [inspectedVersionId],
    );
  }, [playgroundCompareOpen, inspectedVersionId]);

  useEffect(() => {
    if (runs.length === 0) {
      setSelectedRunId(null);
      return;
    }
    const firstRun = runs[0];
    if (
      (!selectedRunId ||
        !runs.some((run) => run.knowledge_base_run_id === selectedRunId)) &&
      firstRun
    ) {
      setSelectedRunId(firstRun.knowledge_base_run_id);
    }
  }, [runs, selectedRunId]);

  useEffect(() => {
    if (!sourceBucket) {
      const first = bucketsQuery.data?.[0]?.name;
      if (first) setSourceBucket(first);
    }
  }, [bucketsQuery.data, sourceBucket]);

  const q = search.trim().toLowerCase();
  // Status options derived from the distinct statuses actually present, so we
  // never offer a bucket with zero rows.
  const knowledgeStatusOptions = Array.from(
    new Set(knowledgeBases.map((item) => item.status)),
  )
    .filter(Boolean)
    .sort()
    .map((status) => ({
      value: status,
      label: status.charAt(0).toUpperCase() + status.slice(1),
    }));
  const libraryOwnerOptions = Array.from(
    new Set(knowledgeBases.map((item) => item.owner)),
  )
    .filter(Boolean)
    .sort()
    .map((owner) => ({ value: owner, label: owner }));
  const filteredKnowledgeBases = knowledgeBases.filter((item) => {
    if (libraryStatusFilter && item.status !== libraryStatusFilter) return false;
    if (libraryOwnerFilter && item.owner !== libraryOwnerFilter) return false;
    if (!q) return true;
    return [item.name, item.description, item.owner, item.source_bucket]
      .filter(Boolean)
      .some((field) => String(field).toLowerCase().includes(q));
  });
  const hasLibraryFilters = Boolean(
    search || libraryStatusFilter || libraryOwnerFilter,
  );

  const sourceFilter = sourceSearch.trim().toLowerCase();

  const activeKnowledgeBaseCount = knowledgeBases.filter(
    (item) => item.status === "active",
  ).length;
  const ageReadyKnowledgeBaseCount = knowledgeBases.filter(
    (item) => item.active_version_summary?.age_ready,
  ).length;

  const selectedPrimaryVersion = useMemo(
    () =>
      versions.find(
        (version) =>
          version.knowledge_base_version_id === playgroundVersionIds[0],
      ) ?? null,
    [playgroundVersionIds, versions],
  );
  const playgroundDefaultGraphConfig =
    selectedPrimaryVersion?.graph_config ??
    knowledgeOptionsQuery.data?.default_graph_config ??
    FALLBACK_GRAPH_CONFIG;
  const playgroundDefaultGraphConfigSignature = JSON.stringify(
    playgroundDefaultGraphConfig,
  );
  const playgroundSeedSignature = `${selectedPrimaryVersion?.knowledge_base_version_id ?? "none"}:${playgroundDefaultGraphConfigSignature}`;
  useEffect(() => {
    if (
      appliedPlaygroundSeedSignatureRef.current === playgroundSeedSignature &&
      playgroundSeedOverrideRef.current === null
    ) {
      return;
    }
    const pendingSeed = playgroundSeedOverrideRef.current;
    setPlaygroundGraphOverrides(
      pendingSeed?.graphOverrides ??
        graphOverridesFromConfig(playgroundDefaultGraphConfig),
    );
    setPlaygroundRetrievalModes(pendingSeed?.retrievalModes ?? []);
    setPlaygroundGraphTuningEnabled(pendingSeed?.graphTuningEnabled ?? false);
    playgroundSeedOverrideRef.current = null;
    appliedPlaygroundSeedSignatureRef.current = playgroundSeedSignature;
  }, [playgroundDefaultGraphConfig, playgroundSeedSignature]);
  const selectedPlaygroundVersions = useMemo(
    () =>
      versions.filter((version) =>
        playgroundVersionIds.includes(version.knowledge_base_version_id),
      ),
    [playgroundVersionIds, versions],
  );
  const playgroundUsesBuildDefault = playgroundRetrievalModes.length === 0;
  const selectedPrimaryConfiguredDefaultMode = selectedPrimaryVersion
    ? configuredDefaultRetrievalMode(
        selectedPrimaryVersion.graph_config,
        ageEnabled,
      )
    : null;
  const selectedPrimaryResolvedDefaultModes = useMemo(
    () =>
      defaultPlaygroundRetrievalModesForVersion(
        selectedPrimaryVersion,
        ageEnabled,
      ),
    [ageEnabled, selectedPrimaryVersion],
  );
  const effectivePlaygroundRetrievalModes = useMemo(() => {
    if (playgroundRetrievalModes.length > 0) {
      return playgroundRetrievalModes;
    }
    const defaults = selectedPlaygroundVersions.flatMap((version) =>
      defaultPlaygroundRetrievalModesForVersion(version, ageEnabled),
    );
    const unique = Array.from(new Set(defaults));
    if (unique.length > 0) {
      return unique;
    }
    return defaultPlaygroundRetrievalModesForVersion(
      selectedPrimaryVersion,
      ageEnabled,
    );
  }, [
    ageEnabled,
    playgroundRetrievalModes,
    selectedPlaygroundVersions,
    selectedPrimaryVersion,
  ]);
  const selectedPrimaryDefaultModeFallback = Boolean(
    selectedPrimaryVersion &&
    selectedPrimaryConfiguredDefaultMode === "age_graph" &&
    selectedPrimaryResolvedDefaultModes[0] !== "age_graph",
  );
  const playgroundAgeReadyCount = selectedPlaygroundVersions.filter((version) =>
    isAgeReadyVersion(version, ageEnabled),
  ).length;
  const playgroundUsesGraphModes = effectivePlaygroundRetrievalModes.some(
    (mode) => mode !== "dense",
  );
  const playgroundUsesAgeGraph =
    effectivePlaygroundRetrievalModes.includes("age_graph");
  const selectedPrimaryStrictAgeDefault = Boolean(
    selectedPrimaryVersion?.graph_config.strict_age_retrieval_default &&
      selectedPrimaryConfiguredDefaultMode === "age_graph",
  );
  const playgroundQueryPresetFallback = {
    retrieval_strength:
      selectedPrimaryVersion?.graph_config.retrieval_strength ??
      playgroundDefaultGraphConfig.retrieval_strength,
    minimum_relationship_weight:
      selectedPrimaryVersion?.graph_config.minimum_relationship_weight ??
      playgroundDefaultGraphConfig.minimum_relationship_weight,
    age_seed_mode:
      selectedPrimaryVersion?.graph_config.age_seed_mode ??
      playgroundDefaultGraphConfig.age_seed_mode,
    age_traversal_hops:
      selectedPrimaryVersion?.graph_config.age_traversal_hops ??
      playgroundDefaultGraphConfig.age_traversal_hops,
    age_candidate_pool_size:
      selectedPrimaryVersion?.graph_config.age_candidate_pool_size ??
      playgroundDefaultGraphConfig.age_candidate_pool_size,
    age_dense_rerank_weight:
      selectedPrimaryVersion?.graph_config.age_dense_rerank_weight ??
      playgroundDefaultGraphConfig.age_dense_rerank_weight,
    strict_age_retrieval: Boolean(
      selectedPrimaryVersion?.graph_config.strict_age_retrieval_default ??
        playgroundDefaultGraphConfig.strict_age_retrieval_default,
    ),
  };
  const activePlaygroundGraphQueryPreset =
    playgroundGraphTuningEnabled &&
    effectivePlaygroundRetrievalModes.length === 1 &&
    effectivePlaygroundRetrievalModes[0] !== "dense"
      ? matchGraphQueryPreset(
          buildGraphQueryPresetState({
            retrievalMode:
              effectivePlaygroundRetrievalModes[0] as KnowledgeRetrievalMode,
            overrides: playgroundGraphOverrides,
            fallback: playgroundQueryPresetFallback,
          }),
          graphQueryPresets,
        )
      : null;
  const activePlaygroundGraphQueryPresetDefinition =
    graphQueryPresets.find(
      (preset) => preset.id === activePlaygroundGraphQueryPreset,
    ) ?? null;
  const playgroundGraphOverridesPayload = buildGraphOverridesPayload(
    playgroundGraphTuningEnabled && playgroundUsesGraphModes,
    playgroundGraphOverrides,
    playgroundUsesAgeGraph,
  );
  const selectedVersionAgeStatus = ageSyncStatus(selectedVersion);
  const selectedVersionAgeError =
    typeof selectedVersion?.summary?.age_sync_error === "string"
      ? selectedVersion.summary.age_sync_error
      : null;
  const selectedVersionAgeConfigured = isAgeConfiguredVersion(
    selectedVersion,
    ageEnabled,
  );
  const selectedVersionAgeReady = isAgeReadyVersion(
    selectedVersion,
    ageEnabled,
  );
  const selectedVersionAgeActionLabel = selectedVersion
    ? ageSyncActionLabel(selectedVersion, ageEnabled)
    : "Sync to AGE";
  const selectedVersionStrictAgeDefault = Boolean(
    selectedVersion?.graph_config.strict_age_retrieval_default,
  );
  // The old library-sidebar derivations were removed with the always-on
  // profile sidebar (Phase R1); the workspace header now surfaces KB facts.
  const graphSourceOptions = useMemo(
    () =>
      ageEnabled
        ? [
            { id: "age" as const, label: "Apache AGE neighborhood" },
            { id: "local" as const, label: "Version graph artifacts" },
          ]
        : [{ id: "local" as const, label: "Version graph artifacts" }],
    [ageEnabled],
  );
  useEffect(() => {
    if (!selectedVersion) return;
    setAgeSyncError(null);
    setGraphRequestedSource(
      selectedVersionAgeConfigured && ageEnabled ? "age" : "local",
    );
    setGraphRetrievalStrength(selectedVersion.graph_config.retrieval_strength);
    setGraphTraversalHops(
      selectedVersionAgeConfigured
        ? selectedVersion.graph_config.age_traversal_hops
        : 1,
    );
    setGraphAgeSeedMode(
      selectedVersionAgeConfigured
        ? selectedVersion.graph_config.age_seed_mode
        : "entity_then_text",
    );
    setGraphMinRelationshipWeight(
      selectedVersion.graph_config.minimum_relationship_weight,
    );
    setGraphAgeCandidatePool(
      selectedVersion.graph_config.age_candidate_pool_size,
    );
    setGraphAgeDenseRerankWeight(
      selectedVersion.graph_config.age_dense_rerank_weight,
    );
    setGraphProbeStrictAge(
      Boolean(
        selectedVersionAgeConfigured &&
          selectedVersion.graph_config.strict_age_retrieval_default,
      ),
    );
    setGraphNodeLimit(12);
    setGraphEntityTypeFilter("");
    setGraphSearch("");
    setSelectedGraphEntityId(null);
  }, [
    selectedVersion,
    selectedVersion?.knowledge_base_version_id,
    selectedVersion?.graph_config.retrieval_strength,
    selectedVersion?.graph_config.age_traversal_hops,
    selectedVersion?.graph_config.age_seed_mode,
    selectedVersion?.graph_config.minimum_relationship_weight,
    selectedVersion?.graph_config.age_candidate_pool_size,
    selectedVersion?.graph_config.age_dense_rerank_weight,
    selectedVersion?.graph_config.strict_age_retrieval_default,
    selectedVersion?.graph_config.output_target,
    ageEnabled,
    selectedVersionAgeConfigured,
    selectedVersionAgeReady,
  ]);
  useEffect(() => {
    setGraphProbeError(null);
    setGraphProbeResult(null);
  }, [
    selectedVersion?.knowledge_base_version_id,
    selectedVersionAgeStatus,
    graphRequestedSource,
    graphRetrievalStrength,
    graphTraversalHops,
    graphAgeSeedMode,
    graphMinRelationshipWeight,
    graphAgeCandidatePool,
    graphAgeDenseRerankWeight,
  ]);
  useEffect(() => {
    setGraphProbeQuestion("");
  }, [selectedVersion?.knowledge_base_version_id]);
  useEffect(() => {
    if (graphRequestedSource !== "age" && graphProbeStrictAge) {
      setGraphProbeStrictAge(false);
    }
  }, [graphProbeStrictAge, graphRequestedSource]);
  const graphExplorerQuery = useApiQuery<KnowledgeGraphExploreResult>(
    [
      "knowledge-bases",
      inspectedVersionId ?? "",
      "graph",
      selectedVersionAgeStatus,
      graphRequestedSource,
      graphProbeStrictAge,
      graphSearch,
      graphEntityTypeFilter,
      graphTraversalHops,
      graphAgeSeedMode,
      graphMinRelationshipWeight,
      graphNodeLimit,
    ],
    (signal) =>
      caliberApi.getKnowledgeBaseGraph(
        inspectedVersionId!,
        {
          source: graphRequestedSource,
          q: graphSearch || undefined,
          entityType: graphEntityTypeFilter || undefined,
          minimumRelationshipWeight: graphMinRelationshipWeight,
          traversalHops: graphTraversalHops,
          ageSeedMode:
            graphRequestedSource === "age" ? graphAgeSeedMode : undefined,
          strictAgeRetrieval:
            graphRequestedSource === "age" ? graphProbeStrictAge : undefined,
          nodeLimit: graphNodeLimit,
        },
        signal,
      ),
    {
      enabled: Boolean(inspectedVersionId),
      refetchInterval: () =>
        (versionsQuery.data ?? []).some(
          (item) =>
            item.knowledge_base_version_id === inspectedVersionId &&
            isVersionLive(item),
        )
          ? 2000
          : false,
    },
  );
  const graphView = graphExplorerQuery.data ?? null;
  const graphVisibleEntities =
    (graphView?.entities as KnowledgeGraphEntityView[] | undefined) ??
    EMPTY_GRAPH_ENTITIES;
  const graphVisibleRelationships =
    (graphView?.relationships as KnowledgeGraphRelationshipView[] | undefined) ??
    EMPTY_GRAPH_RELATIONSHIPS;
  const graphRequestedAge = graphRequestedSource === "age";
  const graphStrictAgeActive = graphRequestedAge && graphProbeStrictAge;
  const graphFallbackExpected = Boolean(
    graphRequestedAge && !selectedVersionAgeReady && !graphStrictAgeActive,
  );
  const graphServedSource =
    graphView?.served_source ??
    (graphRequestedAge ? (graphFallbackExpected ? "local" : "age") : "local");
  const graphUsedAge = graphServedSource === "age";
  const graphFallbackReason = graphView?.fallback_reason ?? null;
  const graphStrictAgeBlocked = Boolean(
    graphStrictAgeActive &&
      graphFallbackReason &&
      graphVisibleEntities.length === 0 &&
      graphVisibleRelationships.length === 0,
  );
  const graphEffectiveAgeSeedMode = (graphView?.age_seed_mode ??
    graphAgeSeedMode ??
    selectedVersion?.graph_config.age_seed_mode ??
    "entity_then_text") as KnowledgeAgeSeedMode;
  const graphAgeSeedStrategy = graphView?.age_seed_strategy ?? null;
  const graphExplorerStatusLabel = graphRequestedAge
    ? graphStrictAgeBlocked
      ? "Strict AGE only"
      : graphUsedAge
      ? "Served from Apache AGE"
      : "AGE requested, served locally"
    : "Served from version graph";
  const graphExplorerStatusTone = graphRequestedAge
    ? graphStrictAgeBlocked
      ? "bg-amber-50 text-amber-700"
      : graphUsedAge
      ? "bg-emerald-50 text-emerald-700"
      : "bg-amber-50 text-amber-700"
    : "bg-slate-100 text-slate-600";
  const graphExplorerSourceSummary = graphUsedAge
    ? "Apache AGE neighborhood"
    : graphStrictAgeActive
      ? "AGE-only request"
    : graphRequestedAge
      ? "Version graph fallback"
      : "Version graph artifacts";
  const graphQueryPresetFallback = {
    retrieval_strength:
      selectedVersion?.graph_config.retrieval_strength ??
      FALLBACK_GRAPH_CONFIG.retrieval_strength,
    minimum_relationship_weight:
      selectedVersion?.graph_config.minimum_relationship_weight ??
      FALLBACK_GRAPH_CONFIG.minimum_relationship_weight,
    age_seed_mode:
      selectedVersion?.graph_config.age_seed_mode ??
      FALLBACK_GRAPH_CONFIG.age_seed_mode,
    age_traversal_hops:
      selectedVersion?.graph_config.age_traversal_hops ??
      FALLBACK_GRAPH_CONFIG.age_traversal_hops,
    age_candidate_pool_size:
      selectedVersion?.graph_config.age_candidate_pool_size ??
      FALLBACK_GRAPH_CONFIG.age_candidate_pool_size,
    age_dense_rerank_weight:
      selectedVersion?.graph_config.age_dense_rerank_weight ??
      FALLBACK_GRAPH_CONFIG.age_dense_rerank_weight,
    strict_age_retrieval: Boolean(
      selectedVersion?.graph_config.strict_age_retrieval_default ??
        FALLBACK_GRAPH_CONFIG.strict_age_retrieval_default,
    ),
  };
  const activeGraphQueryPreset = matchGraphQueryPreset(
    buildGraphQueryPresetState({
      retrievalMode: graphRequestedAge ? "age_graph" : "graph_hybrid",
      overrides: {
        retrieval_strength: graphRetrievalStrength,
        minimum_relationship_weight: graphMinRelationshipWeight,
        age_seed_mode: graphAgeSeedMode,
        age_traversal_hops: graphTraversalHops,
        age_candidate_pool_size: graphAgeCandidatePool,
        age_dense_rerank_weight: graphAgeDenseRerankWeight,
        strict_age_retrieval: graphProbeStrictAge,
      },
      fallback: graphQueryPresetFallback,
    }),
    graphQueryPresets,
  );
  const activeGraphQueryPresetDefinition =
    graphQueryPresets.find((preset) => preset.id === activeGraphQueryPreset) ??
    null;
  const graphEntityMap = useMemo(
    () =>
      new Map(
        graphVisibleEntities.map((entity) => [
          entity.knowledge_base_entity_id,
          entity,
        ]),
      ),
    [graphVisibleEntities],
  );
  const graphSourceMap = useMemo(
    () =>
      new Map(
        (sourcesQuery.data ?? []).map((source) => [source.object_key, source]),
      ),
    [sourcesQuery.data],
  );

  useEffect(() => {
    if (graphVisibleEntities.length === 0) {
      if (
        selectedGraphEntityId !== null &&
        !graphExplorerQuery.isFetching &&
        !graphExplorerQuery.isLoading
      ) {
        setSelectedGraphEntityId(null);
      }
      return;
    }
    if (selectedGraphEntityId && graphEntityMap.has(selectedGraphEntityId)) {
      return;
    }
    const preferred =
      graphVisibleEntities.find((entity) => entity.highlighted) ??
      graphVisibleEntities.find((entity) => entity.distance === 0) ??
      graphVisibleEntities[0] ??
      null;
    setSelectedGraphEntityId(preferred?.knowledge_base_entity_id ?? null);
  }, [
    graphEntityMap,
    graphExplorerQuery.isFetching,
    graphExplorerQuery.isLoading,
    graphVisibleEntities,
    selectedGraphEntityId,
  ]);

  const selectedGraphEntity = selectedGraphEntityId
    ? (graphEntityMap.get(selectedGraphEntityId) ?? null)
    : null;
  const selectedGraphRelationships = useMemo(
    () =>
      selectedGraphEntity
        ? graphVisibleRelationships
            .filter(
              (relationship) =>
                relationship.source_entity_id ===
                  selectedGraphEntity.knowledge_base_entity_id ||
                relationship.target_entity_id ===
                  selectedGraphEntity.knowledge_base_entity_id,
            )
            .sort((left, right) => right.weight - left.weight)
        : [],
    [graphVisibleRelationships, selectedGraphEntity],
  );
  const selectedGraphNeighbors = useMemo(
    () =>
      selectedGraphEntity
        ? selectedGraphRelationships
            .map((relationship) => {
              const neighborId =
                relationship.source_entity_id ===
                selectedGraphEntity.knowledge_base_entity_id
                  ? relationship.target_entity_id
                  : relationship.source_entity_id;
              const neighbor = graphEntityMap.get(neighborId);
              if (!neighbor) return null;
              return { relationship, neighbor };
            })
            .filter(
              (
                item,
              ): item is {
                relationship: KnowledgeGraphRelationshipView;
                neighbor: KnowledgeGraphEntityView;
              } => item !== null,
            )
        : [],
    [graphEntityMap, selectedGraphEntity, selectedGraphRelationships],
  );
  const selectedGraphSourceDetails = useMemo(
    () =>
      selectedGraphEntity
        ? selectedGraphEntity.source_keys
            .map((key) => graphSourceMap.get(key))
            .filter((item): item is KnowledgeBaseSource => Boolean(item))
        : [],
    [graphSourceMap, selectedGraphEntity],
  );

  const historyPayload = useMemo(
    () =>
      chatTurns.flatMap((turn) => {
        const primaryAnswer = turn.result.versions[0]?.answer;
        return primaryAnswer
          ? [
              { role: "user" as const, content: turn.question },
              { role: "assistant" as const, content: primaryAnswer },
            ]
          : [{ role: "user" as const, content: turn.question }];
      }),
    [chatTurns],
  );

  const createOrRun = async (
    event: FormEvent<HTMLFormElement>,
  ): Promise<void> => {
    event.preventDefault();
    if (embeddingSelectionBlockedReason) {
      setBuildError(embeddingSelectionBlockedReason);
      return;
    }
    setBuildBusy(true);
    setBuildError(null);
    try {
      const body = {
        sources: selectedSources,
        chunking_strategy: chunkingStrategy,
        embedding_model: embeddingModel,
        chunking_config: {
          chunk_size: chunkSize,
          chunk_overlap: chunkOverlap,
          semantic_similarity_threshold: semanticThreshold,
        },
        graph_config: {
          extractor_backend: graphConfig.extractor_backend,
          spacy_model:
            graphConfig.extractor_backend === "spacy"
              ? graphConfig.spacy_model?.trim() || null
              : null,
          max_entities_per_chunk: graphConfig.max_entities_per_chunk,
          entity_types: graphConfig.entity_types,
          minimum_entity_mentions: graphConfig.minimum_entity_mentions,
          minimum_relationship_weight: graphConfig.minimum_relationship_weight,
          default_retrieval_mode: graphConfig.default_retrieval_mode,
          retrieval_strength: graphConfig.retrieval_strength,
          output_target: graphConfig.output_target,
          age_seed_mode: graphConfig.age_seed_mode,
          age_traversal_hops: graphConfig.age_traversal_hops,
          age_candidate_pool_size: graphConfig.age_candidate_pool_size,
          age_dense_rerank_weight: graphConfig.age_dense_rerank_weight,
          strict_age_retrieval_default: Boolean(
            graphConfig.strict_age_retrieval_default,
          ),
        },
      };
      let result: KnowledgeBaseBuildResult;
      if (buildMode === "existing" && selectedKnowledgeBase) {
        result = await caliberApi.createKnowledgeBaseVersion(
          selectedKnowledgeBase.knowledge_base_id,
          body,
        );
      } else {
        result = await caliberApi.createKnowledgeBase({
          name: newName.trim(),
          description: newDescription.trim(),
          source_bucket: sourceBucket,
          ...body,
        });
      }
      await Promise.all([
        invalidate(["knowledge-bases", "list"]),
        invalidate([
          "knowledge-bases",
          result.knowledge_base.knowledge_base_id,
          "versions",
        ]),
        invalidate([
          "knowledge-bases",
          result.knowledge_base.knowledge_base_id,
          "runs",
        ]),
      ]);
      setSelectedKnowledgeBaseId(result.knowledge_base.knowledge_base_id);
      setInspectedVersionId(result.version.knowledge_base_version_id);
      setSelectedRunId(result.run.knowledge_base_run_id);
      setPlaygroundVersionIds([result.version.knowledge_base_version_id]);
      if (buildMode === "new") {
        setNewName("");
        setNewDescription("");
        setSelectedSources([]);
      }
      setBuildLaunchSelection(null);
      // Land the newly built/updated corpus in its Workspace. A live build keeps
      // the Build stage (its run history sits there); a finished build jumps to
      // the Use stage's version table.
      setOpenKnowledgeBaseId(result.knowledge_base.knowledge_base_id);
      setCreatingKnowledgeBase(false);
      setWorkspaceStage(
        isVersionLive(result.version) || isRunLive(result.run)
          ? "build"
          : "use",
      );
    } catch (error) {
      setBuildError(
        error instanceof ApiError ? error.message : "Knowledge build failed",
      );
    } finally {
      setBuildBusy(false);
    }
  };

  const activateVersion = async (
    version: KnowledgeBaseVersion,
  ): Promise<void> => {
    if (!selectedKnowledgeBase) return;
    try {
      await caliberApi.activateKnowledgeBaseVersion(
        selectedKnowledgeBase.knowledge_base_id,
        version.knowledge_base_version_id,
      );
      await Promise.all([
        invalidate(["knowledge-bases", "list"]),
        invalidate([
          "knowledge-bases",
          selectedKnowledgeBase.knowledge_base_id,
          "versions",
        ]),
      ]);
    } catch (error) {
      setBuildError(
        error instanceof ApiError
          ? error.message
          : "Failed to activate version",
      );
    }
  };

  // Permanently delete a KB + its data. Confirm-gated at the call site (the card
  // / header arms ``pendingDeleteKnowledgeBaseId`` first). On success we leave
  // any open workspace for the deleted KB and refresh the library list; failures
  // surface inline and non-fatally so the page stays usable.
  const deleteKnowledgeBase = async (knowledgeBaseId: string): Promise<void> => {
    setDeleteBusy(true);
    setDeleteError(null);
    try {
      await caliberApi.deleteKnowledgeBase(knowledgeBaseId);
      setPendingDeleteKnowledgeBaseId(null);
      if (openKnowledgeBaseId === knowledgeBaseId) {
        setOpenKnowledgeBaseId(null);
        setCreatingKnowledgeBase(false);
      }
      if (selectedKnowledgeBaseId === knowledgeBaseId) {
        setSelectedKnowledgeBaseId(null);
        setInspectedVersionId(null);
      }
      await invalidate(["knowledge-bases", "list"]);
    } catch (error) {
      setDeleteError(
        error instanceof ApiError
          ? error.message
          : "Failed to delete knowledge base",
      );
    } finally {
      setDeleteBusy(false);
    }
  };

  // Bulk cleanup: permanently delete the SELECTED knowledge bases (best-effort,
  // one at a time via the per-KB cascade endpoint so each is properly authorized
  // + its versions/chunks/graph/artifacts are removed). Collects failures rather
  // than aborting on the first.
  const deleteSelectedKnowledgeBases = async (): Promise<void> => {
    setBulkDeleteBusy(true);
    setBulkDeleteError(null);
    const targets = knowledgeBases.filter((item) =>
      selectedKbIds.has(item.knowledge_base_id),
    );
    let failed = 0;
    for (const item of targets) {
      try {
        await caliberApi.deleteKnowledgeBase(item.knowledge_base_id);
      } catch {
        failed += 1;
      }
    }
    if (openKnowledgeBaseId && selectedKbIds.has(openKnowledgeBaseId)) {
      setOpenKnowledgeBaseId(null);
      setCreatingKnowledgeBase(false);
    }
    setSelectedKnowledgeBaseId(null);
    setInspectedVersionId(null);
    setPendingDeleteKnowledgeBaseId(null);
    setSelectedKbIds(new Set());
    await invalidate(["knowledge-bases", "list"]);
    setBulkDeleteBusy(false);
    setBulkDeleteOpen(false);
    if (failed > 0) {
      setBulkDeleteError(
        `${failed} of ${targets.length} knowledge base${targets.length === 1 ? "" : "s"} could not be deleted.`,
      );
    }
  };

  const syncVersionToAge = async (
    version: KnowledgeBaseVersion,
  ): Promise<void> => {
    setAgeSyncBusy(true);
    setAgeSyncError(null);
    try {
      await caliberApi.syncKnowledgeBaseVersionToAge(
        version.knowledge_base_version_id,
      );
      await Promise.all([
        invalidate(["knowledge-bases", "list"]),
        invalidate([
          "knowledge-bases",
          selectedKnowledgeBase?.knowledge_base_id ?? "",
          "versions",
        ]),
        invalidate([
          "knowledge-bases",
          version.knowledge_base_version_id,
          "graph",
        ]),
      ]);
    } catch (error) {
      setAgeSyncError(
        error instanceof ApiError
          ? error.message
          : "Failed to sync the version into Apache AGE",
      );
    } finally {
      setAgeSyncBusy(false);
    }
  };

  const askKnowledge = async (
    event: FormEvent<HTMLFormElement>,
  ): Promise<void> => {
    event.preventDefault();
    if (!compareQuestion.trim() || playgroundVersionIds.length === 0) return;
    setQueryBusy(true);
    setQueryError(null);
    try {
      const result = await caliberApi.queryKnowledge({
        version_ids: playgroundVersionIds,
        question: compareQuestion.trim(),
        history: historyPayload,
        top_k: playgroundTopK,
        retrieval_modes: playgroundRetrievalModes,
        graph_overrides: playgroundGraphOverridesPayload,
      });
      setChatTurns((turns) => [
        ...turns,
        { question: compareQuestion.trim(), result },
      ]);
      setCompareQuestion("");
    } catch (error) {
      setQueryError(error instanceof ApiError ? error.message : "Query failed");
    } finally {
      setQueryBusy(false);
    }
  };

  const togglePlaygroundVersion = (versionId: string): void => {
    setPlaygroundVersionIds((current) => {
      if (current.includes(versionId)) {
        return current.filter((item) => item !== versionId);
      }
      if (current.length >= 2) {
        const [, second] = current;
        return second ? [second, versionId] : [versionId];
      }
      return [...current, versionId];
    });
  };

  const togglePlaygroundRetrievalMode = (
    mode: KnowledgeRetrievalMode,
  ): void => {
    setPlaygroundRetrievalModes((current) => {
      if (current.includes(mode)) {
        return current.filter((item) => item !== mode);
      }
      if (current.length >= 2) {
        const [, second] = current;
        return second ? [second, mode] : [mode];
      }
      return [...current, mode];
    });
  };

  const openPlaygroundForVersion = (
    version: KnowledgeBaseVersion,
    options?: {
      retrievalModes?: KnowledgeRetrievalMode[];
      resetGraphTuning?: boolean;
      graphOverrides?: KnowledgeQueryGraphOverrides;
      graphTuningEnabled?: boolean;
    },
  ): void => {
    setPlaygroundVersionIds([version.knowledge_base_version_id]);
    const nextModes = options?.retrievalModes?.length
      ? options.retrievalModes
      : [];
    const nextGraphOverrides =
      options?.graphOverrides ?? graphOverridesFromConfig(version.graph_config);
    const nextGraphTuningEnabled = options?.graphTuningEnabled ?? false;
    playgroundSeedOverrideRef.current = {
      retrievalModes: nextModes,
      graphOverrides: nextGraphOverrides,
      graphTuningEnabled: nextGraphTuningEnabled,
    };
    setPlaygroundRetrievalModes(nextModes);
    if (
      options?.resetGraphTuning ||
      options?.graphOverrides ||
      typeof options?.graphTuningEnabled === "boolean"
    ) {
      setPlaygroundGraphOverrides(nextGraphOverrides);
      setPlaygroundGraphTuningEnabled(nextGraphTuningEnabled);
    }
    setQueryError(null);
    setActiveTab("playground");
  };

  const buildGraphViewQuerySetup = (options?: {
    strictAgeRetrieval?: boolean;
  }): {
    useAgeGraph: boolean;
    retrievalModes: KnowledgeRetrievalMode[];
    graphOverrides: KnowledgeQueryGraphOverrides;
  } => {
    const useAgeGraph = graphRequestedAge && ageEnabled;
    const retrievalModes: KnowledgeRetrievalMode[] = [
      useAgeGraph ? "age_graph" : "graph_hybrid",
    ];
    const nextOverrides: KnowledgeQueryGraphOverrides = {
      retrieval_strength: graphRetrievalStrength,
      minimum_relationship_weight: graphMinRelationshipWeight,
      strict_age_retrieval: Boolean(useAgeGraph && options?.strictAgeRetrieval),
    };
    if (useAgeGraph) {
      nextOverrides.age_seed_mode = graphAgeSeedMode;
      nextOverrides.age_traversal_hops = graphTraversalHops;
      nextOverrides.age_candidate_pool_size = graphAgeCandidatePool;
      nextOverrides.age_dense_rerank_weight = graphAgeDenseRerankWeight;
    }
    return {
      useAgeGraph,
      retrievalModes,
      graphOverrides: nextOverrides,
    };
  };

  const applyGraphQueryPreset = (preset: KnowledgeGraphQueryPreset): void => {
    const nextState = resolveGraphQueryPresetState(
      preset,
      graphQueryPresetFallback,
    );
    setGraphRequestedSource(
      preset.retrieval_mode === "age_graph" ? "age" : "local",
    );
    setGraphRetrievalStrength(nextState.retrieval_strength);
    setGraphMinRelationshipWeight(nextState.minimum_relationship_weight);
    setGraphAgeSeedMode(nextState.age_seed_mode);
    setGraphTraversalHops(nextState.age_traversal_hops);
    setGraphAgeCandidatePool(nextState.age_candidate_pool_size);
    setGraphAgeDenseRerankWeight(nextState.age_dense_rerank_weight);
    setGraphProbeStrictAge(nextState.strict_age_retrieval);
  };

  const applyPlaygroundGraphQueryPreset = (
    preset: KnowledgeGraphQueryPreset,
  ): void => {
    const nextState = resolveGraphQueryPresetState(
      preset,
      playgroundQueryPresetFallback,
    );
    setPlaygroundRetrievalModes([preset.retrieval_mode]);
    setPlaygroundGraphOverrides(graphQueryOverridesFromState(nextState));
    setPlaygroundGraphTuningEnabled(true);
    setQueryError(null);
  };

  const openAgePlaygroundForVersion = (version: KnowledgeBaseVersion): void => {
    openPlaygroundForVersion(version, {
      retrievalModes: ["age_graph"],
      graphOverrides: strictAgeGraphOverridesFromConfig(version.graph_config),
      graphTuningEnabled: true,
      resetGraphTuning: true,
    });
  };

  const openPlaygroundFromGraphView = (
    version: KnowledgeBaseVersion,
    preferredQuestion?: string,
  ): void => {
    const { retrievalModes, graphOverrides } = buildGraphViewQuerySetup({
      strictAgeRetrieval: graphProbeStrictAge,
    });
    playgroundSeedOverrideRef.current = {
      retrievalModes,
      graphOverrides,
      graphTuningEnabled: true,
    };
    setPlaygroundVersionIds([version.knowledge_base_version_id]);
    setPlaygroundRetrievalModes(retrievalModes);
    setPlaygroundGraphOverrides(graphOverrides);
    setPlaygroundGraphTuningEnabled(true);
    setCompareQuestion((current) => {
      if (current.trim()) return current;
      if (preferredQuestion?.trim()) return preferredQuestion.trim();
      if (graphSearch.trim())
        return `What should I know about ${graphSearch.trim()}?`;
      return current;
    });
    setQueryError(null);
    setActiveTab("playground");
  };

  const runGraphProbe = async (
    event: FormEvent<HTMLFormElement>,
  ): Promise<void> => {
    event.preventDefault();
    if (!selectedVersion || !graphProbeQuestion.trim()) return;
    setGraphProbeBusy(true);
    setGraphProbeError(null);
    try {
      const { useAgeGraph, retrievalModes, graphOverrides } =
        buildGraphViewQuerySetup({ strictAgeRetrieval: graphProbeStrictAge });
      const result = await caliberApi.queryKnowledge({
        version_ids: [selectedVersion.knowledge_base_version_id],
        question: graphProbeQuestion.trim(),
        top_k: 4,
        retrieval_modes: retrievalModes,
        graph_overrides: buildGraphOverridesPayload(
          true,
          graphOverrides,
          useAgeGraph,
        ),
      });
      const first = result.versions[0] ?? null;
      setGraphProbeResult(first);
      if (!first) {
        setGraphProbeError("The graph probe did not return a version result.");
      }
    } catch (error) {
      setGraphProbeError(
        error instanceof ApiError ? error.message : "Graph retrieval failed",
      );
    } finally {
      setGraphProbeBusy(false);
    }
  };

  const patchGraphConfig = useCallback(
    (patch: Partial<KnowledgeGraphConfig>): void => {
      setGraphConfig((current) => {
        const next = cloneGraphConfig({ ...current, ...patch });
        if (next.extractor_backend !== "spacy") {
          next.spacy_model = null;
        }
        if (
          !knowledgeOptionsQuery.data?.age_enabled &&
          next.output_target === "object_store_and_age"
        ) {
          next.output_target = "object_store";
        }
        if (
          next.output_target !== "object_store_and_age" &&
          next.default_retrieval_mode === "age_graph"
        ) {
          next.default_retrieval_mode = "graph_hybrid";
        }
        if (next.output_target !== "object_store_and_age") {
          next.strict_age_retrieval_default = false;
        }
        return next;
      });
    },
    [knowledgeOptionsQuery.data?.age_enabled],
  );

  const applyGraphBuildPreset = useCallback(
    (presetId: Exclude<GraphBuildPresetId, "custom">): void => {
      const preset = buildGraphPresets.find((item) => item.id === presetId);
      if (!preset) return;
      patchGraphConfig(preset.patch);
    },
    [buildGraphPresets, patchGraphConfig],
  );

  useEffect(() => {
    if (!pendingBuildLaunchPreset || !knowledgeOptionsQuery.data) return;
    const nextPreset =
      (pendingBuildLaunchPreset === "age_native" ||
        pendingBuildLaunchPreset === "age_strict") &&
      !ageEnabled
        ? "balanced"
        : pendingBuildLaunchPreset;
    applyGraphBuildPreset(nextPreset);
    setPendingBuildLaunchPreset(null);
  }, [
    ageEnabled,
    applyGraphBuildPreset,
    knowledgeOptionsQuery.data,
    pendingBuildLaunchPreset,
  ]);

  const patchPlaygroundGraphOverrides = (
    patch: Partial<KnowledgeQueryGraphOverrides>,
  ): void => {
    setPlaygroundGraphOverrides((current) => ({
      ...current,
      ...patch,
    }));
  };

  const toggleGraphEntityType = (entityType: string): void => {
    setGraphConfig((current) => {
      const exists = current.entity_types.includes(entityType);
      return cloneGraphConfig({
        ...current,
        entity_types: exists
          ? current.entity_types.filter((item) => item !== entityType)
          : [...current.entity_types, entityType],
      });
    });
  };

  const focusGraphEntity = (entityId: string, nextSearch?: string): void => {
    setSelectedGraphEntityId(entityId);
    if (typeof nextSearch === "string") {
      setGraphSearch(nextSearch);
    }
  };

  const sourceKey = (sel: KnowledgeSourceSelection): string =>
    `${sel.kind}:${sel.path}`;
  const isSourceSelected = (sel: KnowledgeSourceSelection): boolean =>
    selectedSources.some((item) => sourceKey(item) === sourceKey(sel));
  // Checkbox toggle used by the bucket tree: add when absent, remove when present.
  const toggleSource = (sel: KnowledgeSourceSelection): void => {
    setSelectedSources((items) =>
      items.some((item) => sourceKey(item) === sourceKey(sel))
        ? items.filter((item) => sourceKey(item) !== sourceKey(sel))
        : [...items, sel],
    );
  };
  // "Select all in view" toggle: add or drop a batch of sources at once.
  const bulkToggleSources = (
    sources: KnowledgeSourceSelection[],
    select: boolean,
  ): void => {
    const keys = new Set(sources.map(sourceKey));
    setSelectedSources((items) => {
      const remaining = items.filter((item) => !keys.has(sourceKey(item)));
      return select ? [...remaining, ...sources] : remaining;
    });
  };

  // ── Asset-workspace render flags (Phase R1) ────────────────────────────────
  // Library is the landing when no KB is open and we are not creating one.
  const inWorkspace = Boolean(openKnowledgeBaseId) || creatingKnowledgeBase;
  // The KB resolved into the Workspace (null in create mode).
  const openKnowledgeBase = useMemo(
    () =>
      knowledgeBases.find(
        (item) => item.knowledge_base_id === openKnowledgeBaseId,
      ) ?? null,
    [knowledgeBases, openKnowledgeBaseId],
  );
  // Which section(s) each stage surfaces. Build groups build + its run history;
  // Explore fans out into Ask / Chunks / Graph; Use is the version table.
  const showBuildStage = inWorkspace && workspaceStage === "build";
  const showExploreStage = inWorkspace && workspaceStage === "explore";
  const showCalibrateStage = inWorkspace && workspaceStage === "calibrate";
  const showUseStage = inWorkspace && workspaceStage === "use";
  const showExploreAsk = showExploreStage && exploreView === "ask";
  const showExploreChunks = showExploreStage && exploreView === "chunks";
  const showExploreGraph = showExploreStage && exploreView === "graph";

  const pageError =
    buildError ??
    knowledgeBasesQuery.error?.message ??
    knowledgeOptionsQuery.error?.message ??
    null;

  // Selection helpers for the library's selective bulk delete (admin only).
  const allFilteredSelected =
    filteredKnowledgeBases.length > 0 &&
    filteredKnowledgeBases.every((item) =>
      selectedKbIds.has(item.knowledge_base_id),
    );
  const toggleSelectAllFiltered = (): void => {
    setSelectedKbIds((current) => {
      const next = new Set(current);
      if (allFilteredSelected) {
        for (const item of filteredKnowledgeBases) {
          next.delete(item.knowledge_base_id);
        }
      } else {
        for (const item of filteredKnowledgeBases) {
          next.add(item.knowledge_base_id);
        }
      }
      return next;
    });
  };

  // ── Library landing ────────────────────────────────────────────────────────
  // The KB list/cards + search/filters + "New knowledge base". The always-on
  // right profile sidebar is gone in R1 — its facts live in the Workspace.
  if (!inWorkspace) {
    return (
      <div className="space-y-6 animate-fade-in">
        <PageHeader
          title="Knowledge Bases"
          subtitle="Build, inspect, and query versioned corpora. Open a knowledge base to enter its workspace — build, explore, calibrate, and use it from one place."
          actions={
            <button
              type="button"
              data-testid="kb-new-knowledge-base"
              className="btn-primary flex items-center gap-2"
              onClick={() => {
                setBuildMode("new");
                setCreatingKnowledgeBase(true);
                setWorkspaceStage("build");
                // Clear any KB left selected from browsing the library or a
                // prior workspace -- otherwise it survives as the implicit
                // "New version" target: the "Create new"/"New version" toggle
                // inside the Build panel is enabled by `selectedKnowledgeBase`
                // alone, so a stale id let the toggle silently repoint this
                // "new knowledge base" flow at submitting a version of an
                // unrelated, previously-open KB. See the regression test
                // "starting a new knowledge base cannot target a stale
                // selection" in knowledge-bases.test.tsx.
                setSelectedKnowledgeBaseId(null);
              }}
            >
              <Sparkles className="h-4 w-4" />
              New knowledge base
            </button>
          }
        />

        {pageError && (
          <div className="rounded-2xl border border-red-200/70 bg-red-50 px-5 py-4 text-sm text-red-700 shadow-card">
            {pageError}
          </div>
        )}

        {bulkDeleteOpen && (
          <div
            data-testid="kb-bulk-delete-confirm"
            className="rounded-2xl border border-red-200 bg-red-50 px-5 py-4 shadow-card"
          >
            <p className="text-sm font-semibold text-red-800">
              Permanently delete {selectedKbIds.size} selected knowledge base
              {selectedKbIds.size === 1 ? "" : "s"}?
            </p>
            <p className="mt-1 text-xs text-red-700">
              This removes each selected knowledge base and all of its data —
              versions, chunks, graph, runs, and stored artifacts. This cannot be
              undone.
            </p>
            <div className="mt-3 flex items-center gap-2">
              <button
                type="button"
                data-testid="kb-bulk-delete-confirm-btn"
                className="inline-flex items-center gap-2 rounded-lg bg-red-600 px-3 py-2 text-sm font-medium text-white transition-colors hover:bg-red-700 disabled:opacity-50"
                disabled={bulkDeleteBusy}
                onClick={() => void deleteSelectedKnowledgeBases()}
              >
                {bulkDeleteBusy
                  ? "Deleting…"
                  : `Delete ${selectedKbIds.size} selected`}
              </button>
              <button
                type="button"
                data-testid="kb-bulk-delete-cancel"
                className="rounded-lg border border-slate-300 bg-white px-3 py-2 text-sm font-medium text-slate-700 transition-colors hover:bg-slate-50 disabled:opacity-50"
                disabled={bulkDeleteBusy}
                onClick={() => setBulkDeleteOpen(false)}
              >
                Cancel
              </button>
            </div>
          </div>
        )}

        {bulkDeleteError && (
          <div
            data-testid="kb-bulk-delete-error"
            className="rounded-2xl border border-red-200/70 bg-red-50 px-5 py-4 text-sm text-red-700 shadow-card"
          >
            {bulkDeleteError}
          </div>
        )}

        {/* Count badges sit on their own row above the shared toolbar. */}
        <div className="flex flex-wrap items-center gap-2 text-[11px] font-semibold text-slate-500">
          <span className="rounded-full border border-slate-200 bg-slate-50 px-3 py-1">
            {knowledgeBases.length} base
            {knowledgeBases.length === 1 ? "" : "s"}
          </span>
          <span className="rounded-full border border-slate-200 bg-slate-50 px-3 py-1">
            {activeKnowledgeBaseCount} active
          </span>
          <span className="rounded-full border border-slate-200 bg-slate-50 px-3 py-1">
            {ageReadyKnowledgeBaseCount} AGE ready
          </span>
        </div>

        <FilterBar
          search={
            <SearchInput
              value={search}
              onChange={setSearch}
              ariaLabel="Search knowledge bases"
              placeholder="Search by name, bucket, owner, or description…"
              className="w-full"
            />
          }
          filters={
            <>
              {knowledgeStatusOptions.length > 0 && (
                <FilterSelect
                  label="Status"
                  allLabel="All statuses"
                  value={libraryStatusFilter}
                  onChange={setLibraryStatusFilter}
                  options={knowledgeStatusOptions}
                  className="w-full sm:w-44"
                />
              )}
              {libraryOwnerOptions.length > 0 && (
                <FilterSelect
                  label="Owner"
                  allLabel="All owners"
                  value={libraryOwnerFilter}
                  onChange={setLibraryOwnerFilter}
                  options={libraryOwnerOptions}
                  className="w-full sm:w-44"
                />
              )}
            </>
          }
          actions={
            <>
              {isAdmin && filteredKnowledgeBases.length > 0 && (
                <label
                  className="inline-flex cursor-pointer items-center gap-1.5 rounded-lg border border-slate-200 bg-white px-3 py-2 text-xs font-medium text-slate-600"
                  data-testid="kb-select-all-label"
                >
                  <input
                    type="checkbox"
                    data-testid="kb-select-all"
                    className="h-4 w-4 rounded border-slate-300 text-caliber-600 focus:ring-caliber-500/30"
                    checked={allFilteredSelected}
                    onChange={toggleSelectAllFiltered}
                  />
                  Select all
                </label>
              )}
              {isAdmin && selectedKbIds.size > 0 && (
                <button
                  type="button"
                  data-testid="kb-delete-selected"
                  className="inline-flex items-center gap-2 rounded-lg border border-red-200 bg-white px-3 py-2 text-sm font-medium text-red-600 transition-colors hover:bg-red-50"
                  onClick={() => {
                    setBulkDeleteError(null);
                    setBulkDeleteOpen(true);
                  }}
                >
                  <Trash2 className="h-4 w-4" />
                  Delete {selectedKbIds.size} selected
                </button>
              )}
              <ClearFiltersButton
                visible={Boolean(search) || hasLibraryFilters}
                onClear={() => {
                  setSearch("");
                  setLibraryStatusFilter("");
                  setLibraryOwnerFilter("");
                }}
              />
              <ViewToggle value={libraryViewMode} onChange={setLibraryViewMode} />
            </>
          }
        />

        {libraryViewMode === "list" && filteredKnowledgeBases.length > 0 && (
          <ListRows testId="knowledge-bases-list">
            {filteredKnowledgeBases.map((item) => {
              const active = item.knowledge_base_id === selectedKnowledgeBaseId;
              const summary = libraryVersionSummary(item);
              const confirmingDelete =
                pendingDeleteKnowledgeBaseId === item.knowledge_base_id;
              return (
                <ListRow
                  key={item.knowledge_base_id}
                  testId={`kb-row-${item.knowledge_base_id}`}
                  title_attr="Open this knowledge base's workspace"
                  className={active ? "bg-caliber-50/60 dark:bg-caliber-500/10" : ""}
                  onClick={() => {
                    setBuildMode("existing");
                    openKnowledgeBaseWorkspace(item.knowledge_base_id);
                  }}
                  icon={
                    <span className="flex items-center gap-2">
                      {isAdmin && (
                        <span
                          onClick={(e) => e.stopPropagation()}
                          className="cursor-pointer"
                        >
                          <input
                            type="checkbox"
                            data-testid={`kb-row-select-${item.knowledge_base_id}`}
                            aria-label={`Select ${item.name}`}
                            className="h-4 w-4 rounded border-slate-300 text-caliber-600 focus:ring-caliber-500/30"
                            checked={selectedKbIds.has(item.knowledge_base_id)}
                            onChange={() =>
                              toggleKbSelected(item.knowledge_base_id)
                            }
                          />
                        </span>
                      )}
                      <span className="grid h-9 w-9 place-items-center rounded-xl bg-violet-50 text-caliber-purple">
                        <Database className="h-4 w-4" />
                      </span>
                    </span>
                  }
                  title={item.name}
                  subtitle={
                    <span className="uppercase tracking-[0.14em]">{item.source_bucket}</span>
                  }
                  columns={
                    <>
                      <span
                        className={`rounded-full px-2.5 py-0.5 text-[11px] font-semibold ${item.status === "active" ? "bg-emerald-50 text-emerald-700" : "bg-slate-100 text-slate-500"}`}
                      >
                        {item.status}
                      </span>
                      <span className="w-28" data-testid={`kb-row-facts-${item.knowledge_base_id}`}>
                        {item.source_manifest.length} source
                        {item.source_manifest.length === 1 ? "" : "s"}
                      </span>
                      {summary && kbSummaryBuilding(summary) ? (
                        <span
                          className="inline-flex w-32 items-center gap-1.5 text-blue-700"
                          data-testid={`kb-row-health-${item.knowledge_base_id}`}
                        >
                          <Loader2 className="h-3 w-3 animate-spin" />
                          building…
                        </span>
                      ) : summary ? (
                        <span
                          className="w-32 truncate"
                          data-testid={`kb-row-health-${item.knowledge_base_id}`}
                        >
                          {summary.chunk_count.toLocaleString()} chunks
                        </span>
                      ) : (
                        <span
                          className="w-32 truncate"
                          data-testid={`kb-row-health-${item.knowledge_base_id}`}
                        >
                          Awaiting build
                        </span>
                      )}
                      <span className="w-32 shrink-0" title={item.updated_at}>
                        Updated {formatDate(item.updated_at)}
                      </span>
                    </>
                  }
                  actions={
                    isAdmin ? (
                      confirmingDelete ? (
                        <div
                          className="flex items-center gap-1"
                          onClick={(e) => e.stopPropagation()}
                        >
                          <span className="px-1 text-[11px] font-semibold text-slate-600">
                            Delete?
                          </span>
                          <button
                            type="button"
                            data-testid={`kb-row-confirm-delete-${item.knowledge_base_id}`}
                            onClick={(e) => {
                              e.stopPropagation();
                              void deleteKnowledgeBase(item.knowledge_base_id);
                            }}
                            disabled={deleteBusy}
                            className="rounded-md bg-red-600 px-2 py-0.5 text-[11px] font-semibold text-white hover:bg-red-700 disabled:opacity-50"
                          >
                            {deleteBusy ? "Deleting…" : "Confirm"}
                          </button>
                          <button
                            type="button"
                            onClick={(e) => {
                              e.stopPropagation();
                              setPendingDeleteKnowledgeBaseId(null);
                            }}
                            className="rounded-md border border-slate-200 px-2 py-0.5 text-[11px] font-semibold text-slate-600 hover:bg-slate-50"
                          >
                            Cancel
                          </button>
                        </div>
                      ) : (
                        <button
                          type="button"
                          data-testid={`kb-row-delete-${item.knowledge_base_id}`}
                          title="Delete this knowledge base"
                          aria-label={`Delete ${item.name}`}
                          onClick={(e) => {
                            e.stopPropagation();
                            setDeleteError(null);
                            setPendingDeleteKnowledgeBaseId(item.knowledge_base_id);
                          }}
                          className="grid h-7 w-7 place-items-center rounded-lg border border-slate-200 bg-white text-slate-400 transition hover:border-red-200 hover:text-red-600"
                        >
                          <Trash2 className="h-3.5 w-3.5" />
                        </button>
                      )
                    ) : undefined
                  }
                />
              );
            })}
          </ListRows>
        )}

        {libraryViewMode === "grid" && (
          <div className="grid grid-cols-1 gap-4 md:grid-cols-2 xl:grid-cols-3">
            {filteredKnowledgeBases.map((item) => {
              const active = item.knowledge_base_id === selectedKnowledgeBaseId;
              const summary = libraryVersionSummary(item);
              const confirmingDelete =
                pendingDeleteKnowledgeBaseId === item.knowledge_base_id;
              return (
                <div
                  key={item.knowledge_base_id}
                  className={`group/card relative h-full rounded-2xl ${selectedKbIds.has(item.knowledge_base_id) ? "ring-2 ring-caliber-500/60" : ""}`}
                >
                {/* Selection checkbox for bulk delete — sibling overlay (no
                    nested buttons), admin only. */}
                {isAdmin && (
                  <label
                    className="absolute left-3 top-3 z-20 cursor-pointer rounded bg-white/90 p-0.5 shadow-sm"
                    onClick={(e) => e.stopPropagation()}
                    data-testid={`kb-card-select-label-${item.knowledge_base_id}`}
                  >
                    <input
                      type="checkbox"
                      data-testid={`kb-card-select-${item.knowledge_base_id}`}
                      aria-label={`Select ${item.name}`}
                      className="h-4 w-4 rounded border-slate-300 text-caliber-600 focus:ring-caliber-500/30"
                      checked={selectedKbIds.has(item.knowledge_base_id)}
                      onChange={() => toggleKbSelected(item.knowledge_base_id)}
                    />
                  </label>
                )}
                {/* Delete affordance sits OUTSIDE the card button (no nested
                    buttons): a small icon that arms a two-step confirm, admin
                    only — mirrors the MCP/agent delete UX. */}
                {isAdmin && (
                  <div className="absolute right-3 top-3 z-10">
                    {confirmingDelete ? (
                      <div className="flex items-center gap-1 rounded-lg border border-red-200 bg-white/95 px-1.5 py-1 shadow-sm">
                        <span className="px-1 text-[11px] font-semibold text-slate-600">
                          Delete?
                        </span>
                        <button
                          type="button"
                          data-testid={`kb-card-confirm-delete-${item.knowledge_base_id}`}
                          onClick={(e) => {
                            e.stopPropagation();
                            void deleteKnowledgeBase(item.knowledge_base_id);
                          }}
                          disabled={deleteBusy}
                          className="rounded-md bg-red-600 px-2 py-0.5 text-[11px] font-semibold text-white hover:bg-red-700 disabled:opacity-50"
                        >
                          {deleteBusy ? "Deleting…" : "Confirm"}
                        </button>
                        <button
                          type="button"
                          onClick={(e) => {
                            e.stopPropagation();
                            setPendingDeleteKnowledgeBaseId(null);
                          }}
                          className="rounded-md border border-slate-200 px-2 py-0.5 text-[11px] font-semibold text-slate-600 hover:bg-slate-50"
                        >
                          Cancel
                        </button>
                      </div>
                    ) : (
                      <button
                        type="button"
                        data-testid={`kb-card-delete-${item.knowledge_base_id}`}
                        title="Delete this knowledge base"
                        aria-label={`Delete ${item.name}`}
                        onClick={(e) => {
                          e.stopPropagation();
                          setDeleteError(null);
                          setPendingDeleteKnowledgeBaseId(item.knowledge_base_id);
                        }}
                        className="grid h-7 w-7 place-items-center rounded-lg border border-slate-200 bg-white/90 text-slate-400 opacity-0 shadow-sm transition hover:border-red-200 hover:text-red-600 focus:opacity-100 group-hover/card:opacity-100"
                      >
                        <Trash2 className="h-3.5 w-3.5" />
                      </button>
                    )}
                  </div>
                )}
                {confirmingDelete && deleteError && (
                  <div
                    data-testid={`kb-card-delete-error-${item.knowledge_base_id}`}
                    className="absolute inset-x-3 top-12 z-10 rounded-lg border border-red-200 bg-white px-3 py-2 text-[11px] text-red-700 shadow-sm"
                  >
                    {deleteError}
                  </div>
                )}
                <button
                  type="button"
                  data-testid={`kb-card-${item.knowledge_base_id}`}
                  title="Open this knowledge base's workspace"
                  onClick={() => {
                    setBuildMode("existing");
                    openKnowledgeBaseWorkspace(item.knowledge_base_id);
                  }}
                  className={`group card flex h-full w-full flex-col p-5 text-left transition-all duration-300 hover:-translate-y-0.5 hover:shadow-card-hover ${active ? "border-caliber-200 ring-1 ring-caliber-200" : ""}`}
                >
                  <div className="flex items-start justify-between gap-3">
                    <div className="flex min-w-0 items-center gap-3">
                      <span className="grid h-10 w-10 shrink-0 place-items-center rounded-xl bg-violet-50 text-caliber-purple">
                        <Database className="h-5 w-5" />
                      </span>
                      <div className="min-w-0">
                        <div className="truncate text-sm font-semibold text-slate-900 transition-colors group-hover:text-caliber-purple">
                          {item.name}
                        </div>
                        <div className="mt-0.5 flex items-center gap-1.5">
                          <span className="rounded-full bg-slate-100 px-2 py-0.5 text-[10px] font-semibold uppercase tracking-[0.14em] text-slate-500">
                            {item.source_bucket}
                          </span>
                          <span className="font-mono text-[10px] text-slate-400">
                            {item.knowledge_base_id}
                          </span>
                        </div>
                      </div>
                    </div>
                    <span
                      className={`rounded-full px-2.5 py-1 text-[11px] font-semibold ${item.status === "active" ? "bg-emerald-50 text-emerald-700" : "bg-slate-100 text-slate-500"}`}
                    >
                      {item.status}
                    </span>
                  </div>
                  <p className="mt-3 line-clamp-3 text-sm leading-relaxed text-slate-500">
                    {item.description ||
                      "Versioned document corpus with chunked evidence, graph lineage, and side-by-side retrieval comparison."}
                  </p>
                  <div
                    className="mt-3 text-xs text-slate-500"
                    data-testid={`kb-card-facts-${item.knowledge_base_id}`}
                  >
                    {item.source_manifest.length} source
                    {item.source_manifest.length === 1 ? "" : "s"}
                    {" · "}
                    {item.last_run_completed_at ?? summary?.completed_at
                      ? `built ${formatDate(
                          item.last_run_completed_at ?? summary?.completed_at,
                        )}`
                      : "not built yet"}
                  </div>
                  <div className="mt-2">
                    {summary && kbSummaryBuilding(summary) ? (
                      <span
                        className="inline-flex items-center gap-1.5 rounded-full border border-blue-200 bg-blue-50 px-2.5 py-1 text-[11px] font-semibold text-blue-700"
                        data-testid={`kb-card-health-${item.knowledge_base_id}`}
                      >
                        <Loader2 className="h-3 w-3 animate-spin" />
                        building…
                      </span>
                    ) : summary ? (
                      <span
                        className="inline-flex items-center rounded-full border border-slate-200 bg-slate-50 px-2.5 py-1 text-[11px] font-semibold text-slate-600"
                        data-testid={`kb-card-health-${item.knowledge_base_id}`}
                      >
                        {summary.chunk_count.toLocaleString()} chunks ·{" "}
                        {summary.entity_count.toLocaleString()} entities
                      </span>
                    ) : (
                      <span
                        className="inline-flex items-center rounded-full border border-slate-200 bg-slate-50 px-2.5 py-1 text-[11px] font-semibold text-slate-500"
                        data-testid={`kb-card-health-${item.knowledge_base_id}`}
                      >
                        Awaiting first build
                      </span>
                    )}
                  </div>
                  <div className="mt-auto flex items-center justify-between border-t border-slate-100 pt-3 text-[11px] text-slate-400">
                    <span>Updated {formatDate(item.updated_at)}</span>
                    <span className="inline-flex items-center gap-1 font-semibold text-caliber-purple">
                      Open workspace
                      <ArrowRightLeft className="h-3.5 w-3.5" />
                    </span>
                  </div>
                </button>
              </div>
              );
            })}
          </div>
        )}

        {filteredKnowledgeBases.length === 0 && (
          <div className="rounded-2xl border-2 border-dashed border-slate-200 bg-gradient-hero px-8 py-12 text-center">
            <div className="mx-auto mb-4 grid h-14 w-14 place-items-center rounded-2xl bg-white text-caliber-purple shadow-card">
              <Database className="h-6 w-6" />
            </div>
            <div className="text-sm font-semibold text-slate-600">
              No knowledge bases match the current search.
            </div>
            <div className="mt-1 text-xs text-slate-400">
              Use “New knowledge base” to create the first versioned corpus.
            </div>
          </div>
        )}
      </div>
    );
  }

  // ── Per-KB Workspace ───────────────────────────────────────────────────────
  // A header (name · status · version switcher · source bucket · set active ·
  // back) over four stage tabs. Each stage re-parents the existing section JSX,
  // scoped to the open KB and the version chosen in the header switcher
  // (``inspectedVersionId``).
  const workspaceName = creatingKnowledgeBase
    ? "New knowledge base"
    : (openKnowledgeBase?.name ?? selectedKnowledgeBase?.name ?? "Knowledge base");
  const workspaceStatus = creatingKnowledgeBase
    ? "draft"
    : (openKnowledgeBase?.status ?? selectedKnowledgeBase?.status ?? "—");
  const workspaceBucket =
    openKnowledgeBase?.source_bucket ?? selectedKnowledgeBase?.source_bucket ?? null;
  const workspaceActiveVersionId =
    openKnowledgeBase?.active_version_id ??
    selectedKnowledgeBase?.active_version_id ??
    null;
  // The version the header switcher operates on. ``inspectedVersionId`` is the
  // single id every stage already reads; the switcher just sets it.
  const switcherVersionId = inspectedVersionId ?? workspaceActiveVersionId ?? null;
  const switcherVersion =
    versions.find((v) => v.knowledge_base_version_id === switcherVersionId) ?? null;
  const statusTone =
    workspaceStatus === "active"
      ? "bg-emerald-50 text-emerald-700 ring-emerald-200/60"
      : workspaceStatus === "draft"
        ? "bg-slate-100 text-slate-600 ring-slate-200/60"
        : "bg-amber-50 text-amber-700 ring-amber-200/60";

  return (
    <div className="space-y-6 animate-fade-in">
      {pageError && (
        <div className="rounded-2xl border border-red-200/70 bg-red-50 px-5 py-4 text-sm text-red-700 shadow-card">
          {pageError}
        </div>
      )}

      <div>
        <button
          type="button"
          data-testid="kb-workspace-back"
          onClick={() => {
            setOpenKnowledgeBaseId(null);
            setCreatingKnowledgeBase(false);
          }}
          className="mb-3 inline-flex items-center gap-1.5 text-sm font-medium text-slate-500 hover:text-slate-700"
        >
          <svg className="h-4 w-4" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" aria-hidden="true">
            <path d="M19 12H5M12 19l-7-7 7-7" />
          </svg>
          Back to knowledge bases
        </button>

        <div
          data-testid="kb-workspace-header"
          className="flex flex-wrap items-center justify-between gap-4 rounded-2xl border border-slate-200/70 bg-white p-5 shadow-card"
        >
          <div className="flex min-w-0 items-center gap-3">
            <span className="grid h-11 w-11 shrink-0 place-items-center rounded-xl bg-violet-50 text-caliber-purple">
              <Database className="h-5 w-5" />
            </span>
            <div className="min-w-0">
              <h1 className="truncate text-xl font-bold tracking-tight text-slate-900">
                {workspaceName}
              </h1>
              <div className="mt-0.5 flex flex-wrap items-center gap-x-3 gap-y-0.5 text-xs text-slate-500">
                {workspaceBucket && (
                  <>
                    <span>
                      Source bucket:{" "}
                      <span className="font-mono text-slate-700">
                        {workspaceBucket}
                      </span>
                    </span>
                    <span className="text-slate-300">·</span>
                  </>
                )}
                <span>
                  Active version:{" "}
                  <span className="font-mono text-slate-700">
                    {workspaceActiveVersionId ?? "none"}
                  </span>
                </span>
              </div>
            </div>
          </div>

          <div className="flex flex-wrap items-center gap-2">
            <span
              data-testid="kb-workspace-status"
              className={`inline-flex items-center rounded-full px-2.5 py-0.5 text-xs font-medium ring-1 ${statusTone}`}
            >
              {workspaceStatus}
            </span>
            {!creatingKnowledgeBase && versions.length > 0 && (
              <label className="inline-flex items-center gap-1.5 text-xs text-slate-500">
                <span className="sr-only">Version</span>
                <select
                  data-testid="kb-workspace-version-switcher"
                  aria-label="Workspace version"
                  value={switcherVersionId ?? ""}
                  onChange={(event) =>
                    setInspectedVersionId(event.target.value || null)
                  }
                  className="form-input !py-1.5 text-xs"
                >
                  {versions.map((v) => (
                    <option
                      key={v.knowledge_base_version_id}
                      value={v.knowledge_base_version_id}
                    >
                      v{v.version_number}
                      {v.knowledge_base_version_id === workspaceActiveVersionId
                        ? " (active)"
                        : ""}
                    </option>
                  ))}
                </select>
              </label>
            )}
            {!creatingKnowledgeBase &&
              switcherVersion &&
              switcherVersion.knowledge_base_version_id !==
                workspaceActiveVersionId && (
                <button
                  type="button"
                  data-testid="kb-workspace-set-active"
                  onClick={() => void activateVersion(switcherVersion)}
                  className="btn-ghost !px-2.5 !py-1.5 text-xs"
                >
                  Set active
                </button>
              )}
            {/* Admin-only, confirm-gated KB deletion. On confirm the workspace
                closes and the library list refetches (the KB is gone). */}
            {isAdmin && !creatingKnowledgeBase && openKnowledgeBase && (
              pendingDeleteKnowledgeBaseId ===
              openKnowledgeBase.knowledge_base_id ? (
                <span className="inline-flex items-center gap-1.5 rounded-lg border border-red-200 bg-red-50/60 px-2 py-1">
                  <span className="text-xs font-semibold text-red-700">
                    Permanently delete + its data?
                  </span>
                  <button
                    type="button"
                    data-testid="kb-workspace-confirm-delete"
                    onClick={() =>
                      void deleteKnowledgeBase(
                        openKnowledgeBase.knowledge_base_id,
                      )
                    }
                    disabled={deleteBusy}
                    className="rounded-md bg-red-600 px-2.5 py-1 text-xs font-semibold text-white hover:bg-red-700 disabled:opacity-50"
                  >
                    {deleteBusy ? "Deleting…" : "Confirm"}
                  </button>
                  <button
                    type="button"
                    onClick={() => setPendingDeleteKnowledgeBaseId(null)}
                    className="rounded-md border border-slate-200 bg-white px-2.5 py-1 text-xs font-semibold text-slate-600 hover:bg-slate-50"
                  >
                    Cancel
                  </button>
                </span>
              ) : (
                <button
                  type="button"
                  data-testid="kb-workspace-delete"
                  onClick={() => {
                    setDeleteError(null);
                    setPendingDeleteKnowledgeBaseId(
                      openKnowledgeBase.knowledge_base_id,
                    );
                  }}
                  className="btn-ghost !px-2.5 !py-1.5 text-xs text-red-600 hover:!text-red-700"
                >
                  <Trash2 className="mr-1 inline h-3.5 w-3.5" />
                  Delete knowledge base
                </button>
              )
            )}
          </div>
        </div>
        {isAdmin &&
          openKnowledgeBase &&
          pendingDeleteKnowledgeBaseId ===
            openKnowledgeBase.knowledge_base_id &&
          deleteError && (
            <div
              data-testid="kb-workspace-delete-error"
              className="mt-3 rounded-xl border border-red-200 bg-red-50 px-3 py-2 text-sm text-red-700"
            >
              {deleteError}
            </div>
          )}
      </div>

      <PageTabs
        tabs={KB_WORKSPACE_STAGES}
        active={workspaceStage}
        onChange={(key) => setWorkspaceStage(key as WorkspaceStage)}
      />

      {showExploreStage && (
        <div
          data-testid="kb-explore-subnav"
          className="inline-flex flex-wrap gap-1 rounded-xl border border-slate-200/70 bg-slate-50 p-1"
        >
          {KB_EXPLORE_VIEWS.map((view) => (
            <button
              key={view.key}
              type="button"
              data-testid={`kb-explore-view-${view.key}`}
              onClick={() => setExploreView(view.key)}
              className={`rounded-lg px-3 py-1.5 text-xs font-semibold transition ${
                exploreView === view.key
                  ? "bg-white text-caliber-700 shadow-sm"
                  : "text-slate-500 hover:text-slate-700"
              }`}
            >
              {view.label}
            </button>
          ))}
        </div>
      )}


      {showBuildStage && (
        <div className="grid gap-5 xl:grid-cols-[minmax(0,1.05fr)_minmax(340px,0.95fr)]">
          <div className="card overflow-hidden">
            <div className="border-b border-slate-200/70 px-5 py-4">
              <div className="flex items-center justify-between gap-3">
                <div>
                  <div className="text-[11px] font-bold uppercase tracking-[0.16em] text-slate-500">
                    Object Store Source Picker
                  </div>
                  <h2 className="mt-2 text-lg font-semibold text-slate-900">
                    Select files or folders
                  </h2>
                </div>
                <span className="rounded-full bg-slate-100 px-3 py-1 text-[11px] font-semibold text-slate-500">
                  {selectedSources.length} selected
                </span>
              </div>
            </div>
            <div className="space-y-4 p-5">
              <div className="grid gap-4 md:grid-cols-[minmax(0,1fr)_minmax(0,1fr)]">
                <label className="space-y-2 text-sm">
                  <span className="font-medium text-slate-700">Bucket</span>
                  <select
                    value={sourceBucket}
                    onChange={(event) => setSourceBucket(event.target.value)}
                    className="form-input w-full"
                    disabled={
                      buildMode === "existing" && Boolean(selectedKnowledgeBase)
                    }
                  >
                    <option value="">Select bucket</option>
                    {(bucketsQuery.data ?? []).map((bucket) => (
                      <option key={bucket.name} value={bucket.name}>
                        {bucket.name}
                      </option>
                    ))}
                  </select>
                </label>
                <label className="space-y-2 text-sm">
                  <span className="font-medium text-slate-700">
                    Filter current folder
                  </span>
                  <SearchInput
                    value={sourceSearch}
                    onChange={setSourceSearch}
                    placeholder="Filter visible files…"
                    className="w-full"
                    ariaLabel="Filter source browser"
                  />
                </label>
              </div>

              <BucketTree
                bucket={sourceBucket}
                filter={sourceFilter}
                isSelected={isSourceSelected}
                onToggle={toggleSource}
                onBulk={bulkToggleSources}
              />

              <div className="rounded-2xl border border-slate-200/70 bg-slate-50/70 p-4">
                <div className="flex items-center justify-between gap-3">
                  <div>
                    <div className="text-sm font-semibold text-slate-800">
                      Selected sources
                    </div>
                    <div className="mt-1 text-xs text-slate-400">
                      Each build stores its own manifest and creates a fresh
                      immutable version.
                    </div>
                  </div>
                  <button
                    type="button"
                    onClick={() => setSelectedSources([])}
                    className="text-xs font-semibold text-slate-500 hover:text-slate-700"
                  >
                    Clear
                  </button>
                </div>
                <div className="mt-3 flex flex-wrap gap-2">
                  {selectedSources.map((source) => (
                    <span
                      key={`${source.kind}:${source.path}`}
                      className="inline-flex items-center gap-2 rounded-full border border-slate-200 bg-white px-3 py-1.5 text-xs text-slate-600 shadow-sm"
                    >
                      {source.kind === "folder" ? (
                        <Folder className="h-3.5 w-3.5 text-caliber-purple" />
                      ) : (
                        <FileCode2 className="h-3.5 w-3.5 text-slate-400" />
                      )}
                      <span className="max-w-[22rem] truncate">
                        {source.path}
                      </span>
                      <button
                        type="button"
                        onClick={() =>
                          setSelectedSources((items) =>
                            items.filter(
                              (item) =>
                                !(
                                  item.kind === source.kind &&
                                  item.path === source.path
                                ),
                            ),
                          )
                        }
                      >
                        ×
                      </button>
                    </span>
                  ))}
                  {selectedSources.length === 0 && (
                    <span className="text-sm text-slate-400">
                      Nothing selected yet.
                    </span>
                  )}
                </div>
              </div>
            </div>
          </div>

          <form
            className="card overflow-hidden"
            onSubmit={(event) => void createOrRun(event)}
          >
            <div className="border-b border-slate-200/70 px-5 py-4">
              <div className="flex flex-wrap items-start justify-between gap-3">
                <div>
                  <div className="text-[11px] font-bold uppercase tracking-[0.16em] text-slate-500">
                    Build Configuration
                  </div>
                  <h2 className="mt-2 text-lg font-semibold text-slate-900">
                    Chunk, embed, and prepare GraphRAG + Apache AGE retrieval
                  </h2>
                </div>
                <div className="flex flex-wrap gap-2 text-[11px]">
                  <span className="rounded-full border border-slate-200/70 bg-white/90 px-3 py-1 font-semibold text-slate-600 shadow-sm dark:border-slate-700/70 dark:bg-slate-950/80 dark:text-slate-200">
                    {buildGraphProfileLabel}
                  </span>
                  <span
                    className={`rounded-full border px-3 py-1 font-semibold shadow-sm ${
                      ageEnabled
                        ? "border-emerald-200 bg-emerald-50 text-emerald-700 dark:border-emerald-500/30 dark:bg-emerald-500/10 dark:text-emerald-200"
                        : "border-slate-200/70 bg-white/90 text-slate-600 dark:border-slate-700/70 dark:bg-slate-950/80 dark:text-slate-200"
                    }`}
                  >
                    {ageEnabled ? "Apache AGE available" : "AGE unavailable"}
                  </span>
                </div>
              </div>
            </div>
            <div className="space-y-5 p-5">
              <div className="grid grid-cols-2 gap-3">
                <button
                  type="button"
                  onClick={() => setBuildMode("new")}
                  className={`rounded-2xl border px-4 py-3 text-left transition ${buildMode === "new" ? "border-caliber-200 bg-caliber-50 text-caliber-700" : "border-slate-200 bg-white text-slate-600"}`}
                >
                  <div className="font-semibold">Create new</div>
                  <div className="mt-1 text-xs text-slate-400">
                    Fresh logical knowledge base and version history.
                  </div>
                </button>
                <button
                  type="button"
                  onClick={() => setBuildMode("existing")}
                  className={`rounded-2xl border px-4 py-3 text-left transition ${buildMode === "existing" ? "border-caliber-200 bg-caliber-50 text-caliber-700" : "border-slate-200 bg-white text-slate-600"}`}
                  disabled={!selectedKnowledgeBase}
                >
                  <div className="font-semibold">New version</div>
                  <div className="mt-1 text-xs text-slate-400">
                    Re-run an existing corpus with new chunking or embeddings.
                  </div>
                </button>
              </div>

              <div
                data-testid="kb-build-blueprint"
                className="rounded-2xl border border-slate-200/70 bg-slate-50/70 p-4 dark:border-slate-700/70 dark:bg-slate-900/70"
              >
                <div className="flex flex-wrap items-start justify-between gap-4">
                  <div className="max-w-3xl">
                    <div className="text-[11px] font-bold uppercase tracking-[0.16em] text-slate-500 dark:text-slate-400">
                      Pipeline blueprint
                    </div>
                    <div className="mt-2 text-sm font-semibold text-slate-900 dark:text-slate-100">
                      Make the graph-native path explicit before the first build
                    </div>
                    <div className="mt-1 text-xs leading-relaxed text-slate-500 dark:text-slate-400">
                      Every build saves chunking, embeddings, and graph settings
                      into an immutable version. You can keep the output
                      portable, or stage Apache AGE-backed retrieval from day
                      one.
                    </div>
                  </div>
                  <div className="flex flex-wrap gap-2 text-[11px]">
                    <span className="rounded-full border border-slate-200/70 bg-white/90 px-3 py-1 font-semibold text-slate-600 shadow-sm dark:border-slate-700/70 dark:bg-slate-950/80 dark:text-slate-200">
                      Immutable version
                    </span>
                    <span className="rounded-full border border-slate-200/70 bg-white/90 px-3 py-1 font-semibold text-slate-600 shadow-sm dark:border-slate-700/70 dark:bg-slate-950/80 dark:text-slate-200">
                      Default retrieval ·{" "}
                      {retrievalModeLabel(graphConfig.default_retrieval_mode)}
                    </span>
                  </div>
                </div>
                <div className="mt-4 grid gap-3 md:grid-cols-3">
                  <div className="rounded-2xl border border-slate-200/70 bg-white/90 p-4 shadow-card dark:border-slate-700/70 dark:bg-slate-950/90">
                    <div className="flex items-start gap-3">
                      <span className="grid h-10 w-10 shrink-0 place-items-center rounded-xl bg-blue-50 text-blue-600 dark:bg-blue-500/10 dark:text-blue-200">
                        <Boxes className="h-5 w-5" />
                      </span>
                      <div>
                        <div className="text-xs font-semibold uppercase tracking-[0.14em] text-slate-500 dark:text-slate-400">
                          Chunking
                        </div>
                        <div className="mt-1 text-sm font-semibold text-slate-900 dark:text-slate-100">
                          {strategyMap.get(chunkingStrategy)?.name ??
                            chunkingStrategy}
                        </div>
                        <div className="mt-1 text-xs leading-relaxed text-slate-500 dark:text-slate-400">
                          {strategyMap.get(chunkingStrategy)?.description ??
                            "Versioned chunk boundaries with configurable size, overlap, and semantic thresholds."}
                        </div>
                        <div className="mt-3 flex flex-wrap gap-2 text-[11px]">
                          <span className="rounded-full border border-slate-200 bg-slate-50 px-2.5 py-1 font-semibold text-slate-600 dark:border-slate-700/70 dark:bg-slate-900/80 dark:text-slate-300">
                            {chunkSize} chars
                          </span>
                          <span className="rounded-full border border-slate-200 bg-slate-50 px-2.5 py-1 font-semibold text-slate-600 dark:border-slate-700/70 dark:bg-slate-900/80 dark:text-slate-300">
                            {chunkOverlap} overlap
                          </span>
                        </div>
                      </div>
                    </div>
                  </div>

                  <div className="rounded-2xl border border-slate-200/70 bg-white/90 p-4 shadow-card dark:border-slate-700/70 dark:bg-slate-950/90">
                    <div className="flex items-start gap-3">
                      <span className="grid h-10 w-10 shrink-0 place-items-center rounded-xl bg-violet-50 text-caliber-purple dark:bg-violet-500/10 dark:text-violet-200">
                        <Sparkles className="h-5 w-5" />
                      </span>
                      <div>
                        <div className="text-xs font-semibold uppercase tracking-[0.14em] text-slate-500 dark:text-slate-400">
                          Embeddings
                        </div>
                        <div className="mt-1 text-sm font-semibold text-slate-900 dark:text-slate-100">
                          {embeddingMap.get(embeddingModel)?.name ??
                            embeddingModel}
                        </div>
                        <div className="mt-1 text-xs leading-relaxed text-slate-500 dark:text-slate-400">
                          {embeddingMap.get(embeddingModel)?.description ??
                            "Configurable Hugging Face embeddings preserved with the version for later compare and rollback."}
                        </div>
                        <div className="mt-3 flex flex-wrap gap-2 text-[11px]">
                          <span className="rounded-full border border-slate-200 bg-slate-50 px-2.5 py-1 font-semibold text-slate-600 dark:border-slate-700/70 dark:bg-slate-900/80 dark:text-slate-300">
                            Hugging Face
                          </span>
                        </div>
                      </div>
                    </div>
                  </div>

                  <div className="rounded-2xl border border-slate-200/70 bg-white/90 p-4 shadow-card dark:border-slate-700/70 dark:bg-slate-950/90">
                    <div className="flex items-start gap-3">
                      <span
                        className={`grid h-10 w-10 shrink-0 place-items-center rounded-xl ${
                          buildUsesAgePrimary
                            ? "bg-emerald-50 text-emerald-600 dark:bg-emerald-500/10 dark:text-emerald-200"
                            : "bg-amber-50 text-amber-700 dark:bg-amber-500/10 dark:text-amber-200"
                        }`}
                      >
                        <ArrowRightLeft className="h-5 w-5" />
                      </span>
                      <div>
                        <div className="text-xs font-semibold uppercase tracking-[0.14em] text-slate-500 dark:text-slate-400">
                          GraphRAG / AGE
                        </div>
                        <div className="mt-1 text-sm font-semibold text-slate-900 dark:text-slate-100">
                          {buildGraphProfileLabel}
                        </div>
                        <div className="mt-1 text-xs leading-relaxed text-slate-500 dark:text-slate-400">
                          {buildGraphProfileDescription}
                        </div>
                        <div className="mt-3 flex flex-wrap gap-2 text-[11px]">
                          <span className="rounded-full border border-slate-200 bg-slate-50 px-2.5 py-1 font-semibold text-slate-600 dark:border-slate-700/70 dark:bg-slate-900/80 dark:text-slate-300">
                            {graphTargetLabel(graphConfig.output_target)}
                          </span>
                          <span className="rounded-full border border-slate-200 bg-slate-50 px-2.5 py-1 font-semibold text-slate-600 dark:border-slate-700/70 dark:bg-slate-900/80 dark:text-slate-300">
                            {retrievalModeLabel(
                              graphConfig.default_retrieval_mode,
                            )}
                          </span>
                          {ageEnabled &&
                            graphConfig.output_target ===
                              "object_store_and_age" && (
                              <>
                                <span className="rounded-full border border-slate-200 bg-slate-50 px-2.5 py-1 font-semibold text-slate-600 dark:border-slate-700/70 dark:bg-slate-900/80 dark:text-slate-300">
                                  {ageTraversalLabel(
                                    graphConfig.age_traversal_hops,
                                  )}
                                </span>
                                <span className="rounded-full border border-slate-200 bg-slate-50 px-2.5 py-1 font-semibold text-slate-600 dark:border-slate-700/70 dark:bg-slate-900/80 dark:text-slate-300">
                                  Seed{" "}
                                  {ageSeedModeLabel(graphConfig.age_seed_mode)}
                                </span>
                              </>
                            )}
                        </div>
                      </div>
                    </div>
                  </div>
                </div>
                <div className="mt-4 flex flex-wrap items-center gap-2">
                  <span className="text-[11px] font-semibold uppercase tracking-[0.14em] text-slate-500 dark:text-slate-400">
                    Quick graph preset
                  </span>
                  {buildGraphPresets.map((preset) => {
                    const active = activeGraphBuildPreset === preset.id;
                    return (
                      <button
                        key={`quick-${preset.id}`}
                        type="button"
                        onClick={() => applyGraphBuildPreset(preset.id)}
                        aria-pressed={active}
                        className={`rounded-full border px-3 py-1 text-[11px] font-semibold transition ${
                          active
                            ? "border-caliber-200 bg-caliber-50 text-caliber-700 dark:border-violet-500/40 dark:bg-violet-500/15 dark:text-violet-100"
                            : "border-slate-200/70 bg-white/90 text-slate-600 hover:border-caliber-200 hover:text-caliber-purple dark:border-slate-700/70 dark:bg-slate-950/80 dark:text-slate-200 dark:hover:border-violet-500/40 dark:hover:text-violet-100"
                        }`}
                      >
                        {preset.label}
                      </button>
                    );
                  })}
                </div>
              </div>

              {buildLaunchSelection && (
                <div
                  className="rounded-2xl border border-blue-200 bg-blue-50 px-4 py-3 text-sm text-blue-800"
                  data-testid="kb-build-import-banner"
                >
                  Imported {selectedSources.length} object-store source
                  {selectedSources.length === 1 ? "" : "s"} from{" "}
                  <span className="font-mono">
                    {sourceBucket || buildLaunchSelection.bucket}
                  </span>
                  .{" "}
                  {buildLaunchSelection.graphPreset === "age_native"
                    ? ageEnabled
                      ? "AGE-native retrieval is staged for this build."
                      : "AGE-native retrieval was requested, but this deployment is using the balanced GraphRAG preset because Apache AGE is unavailable."
                    : buildLaunchSelection.graphPreset === "age_strict"
                      ? ageEnabled
                        ? "Strict AGE retrieval is staged as the saved default for this build."
                        : "Strict AGE retrieval was requested, but this deployment is using the balanced GraphRAG preset because Apache AGE is unavailable."
                    : buildLaunchSelection.graphPreset === "portable"
                      ? "Portable graph artifacts are staged for this build."
                      : "Balanced GraphRAG settings are staged for this build."}
                </div>
              )}

              {buildMode === "new" ? (
                <div className="space-y-4">
                  <label className="block space-y-2 text-sm">
                    <span className="font-medium text-slate-700">
                      Knowledge-base name
                    </span>
                    <input
                      value={newName}
                      onChange={(event) => setNewName(event.target.value)}
                      className="form-input w-full"
                      placeholder="e.g. Product docs corpus"
                    />
                  </label>
                  <label className="block space-y-2 text-sm">
                    <span className="font-medium text-slate-700">
                      Description
                    </span>
                    <textarea
                      value={newDescription}
                      onChange={(event) =>
                        setNewDescription(event.target.value)
                      }
                      className="form-input min-h-[96px] w-full"
                      placeholder="What this corpus contains, who it supports, and how it should be used."
                    />
                  </label>
                </div>
              ) : (
                <div className="rounded-2xl border border-slate-200/70 bg-slate-50/70 p-4 text-sm text-slate-600">
                  <div className="font-semibold text-slate-800">
                    {selectedKnowledgeBase?.name ??
                      "Select a knowledge base from the library"}
                  </div>
                  <div className="mt-1 text-xs text-slate-400">
                    The next run becomes a new immutable version. Previous
                    builds remain intact for compare and rollback.
                  </div>
                </div>
              )}

              <Disclosure
                summary="Advanced configuration"
                hint="Chunking, embedding, and the full GraphRAG / Apache AGE setup. Collapsed by default — the selected preset's values still apply to the build."
                testId="kb-build-advanced"
              >
                <div className="space-y-5">
              <div className="grid gap-4 md:grid-cols-2">
                <label className="block space-y-2 text-sm">
                  <span className="font-medium text-slate-700">
                    Chunking strategy
                  </span>
                  <select
                    value={chunkingStrategy}
                    onChange={(event) =>
                      setChunkingStrategy(event.target.value)
                    }
                    className="form-input w-full"
                  >
                    {(
                      knowledgeOptionsQuery.data?.chunking_strategies ?? []
                    ).map((item) => (
                      <option key={item.id} value={item.id}>
                        {item.name}
                      </option>
                    ))}
                  </select>
                  <span className="text-xs text-slate-400">
                    {strategyMap.get(chunkingStrategy)?.description}
                  </span>
                </label>
                <label className="block space-y-2 text-sm">
                  <span className="font-medium text-slate-700">
                    Embedding model
                  </span>
                  <select
                    value={embeddingModel}
                    onChange={(event) => setEmbeddingModel(event.target.value)}
                    className="form-input w-full"
                    disabled={!hasAvailableEmbeddingOptions}
                  >
                    {embeddingOptions.map((item) => (
                      <option
                        key={item.id}
                        value={item.id}
                        disabled={item.available === false}
                      >
                        {item.name}
                        {item.available === false ? " (Blocked)" : ""}
                      </option>
                    ))}
                  </select>
                  <span className="text-xs text-slate-400">
                    {embeddingMap.get(embeddingModel)?.description}
                  </span>
                  {embeddingBlockedReason && (
                    <span className="rounded-2xl border border-amber-200 bg-amber-50 px-3 py-2 text-xs leading-relaxed text-amber-800">
                      {embeddingBlockedReason}
                    </span>
                  )}
                </label>
              </div>

              <div className="grid gap-4 md:grid-cols-3">
                <label className="block space-y-2 text-sm">
                  <span className="font-medium text-slate-700">Chunk size</span>
                  <input
                    type="number"
                    min={100}
                    value={chunkSize}
                    onChange={(event) =>
                      setChunkSize(Number(event.target.value) || 0)
                    }
                    className="form-input w-full"
                  />
                </label>
                <label className="block space-y-2 text-sm">
                  <span className="font-medium text-slate-700">Overlap</span>
                  <input
                    type="number"
                    min={0}
                    value={chunkOverlap}
                    onChange={(event) =>
                      setChunkOverlap(Number(event.target.value) || 0)
                    }
                    className="form-input w-full"
                  />
                </label>
                <label className="block space-y-2 text-sm">
                  <span className="font-medium text-slate-700">
                    Semantic threshold
                  </span>
                  <input
                    type="number"
                    min={0.1}
                    max={0.99}
                    step={0.01}
                    value={semanticThreshold}
                    onChange={(event) =>
                      setSemanticThreshold(Number(event.target.value) || 0)
                    }
                    className="form-input w-full"
                  />
                </label>
              </div>

              <div className="rounded-2xl border border-slate-200/70 bg-slate-50/70 p-4">
                <div className="text-[11px] font-bold uppercase tracking-[0.16em] text-slate-500">
                  GraphRAG Outputs
                </div>
                <div className="mt-2 flex flex-wrap items-start justify-between gap-4">
                  <div className="max-w-3xl">
                    <div className="text-sm font-semibold text-slate-900">
                      Configure graph extraction, sync, and retrieval
                    </div>
                    <div className="mt-1 text-xs leading-relaxed text-slate-500">
                      Every build can extract entities and relationships,
                      persist graph artifacts, and optionally sync the
                      version-scoped graph into Apache AGE for
                      {` `}
                      {retrievalModeLabel(
                        ageGraphModeOption?.id ?? "age_graph",
                        ageGraphModeOption?.name,
                      )}
                      .
                    </div>
                  </div>
                  <div className="flex flex-wrap items-center gap-2">
                    <span className="rounded-full border border-slate-200/70 bg-white/85 px-3 py-1 text-[11px] font-semibold text-slate-600 shadow-sm dark:border-slate-700/70 dark:bg-slate-950/80 dark:text-slate-200">
                      {knowledgeOptionsQuery.data?.age_enabled &&
                      graphConfig.output_target === "object_store_and_age"
                        ? `AGE → ${knowledgeOptionsQuery.data?.age_graph_name ?? "knowledge_graph"}`
                        : "Object-store graph artifacts"}
                    </span>
                    <span className="rounded-full border border-slate-200/70 bg-white/85 px-3 py-1 text-[11px] font-semibold text-slate-600 shadow-sm dark:border-slate-700/70 dark:bg-slate-950/80 dark:text-slate-200">
                      Profile · {buildGraphProfileLabel}
                    </span>
                    <span className="rounded-full border border-slate-200/70 bg-white/85 px-3 py-1 text-[11px] font-semibold text-slate-600 shadow-sm dark:border-slate-700/70 dark:bg-slate-950/80 dark:text-slate-200">
                      {knowledgeOptionsQuery.data?.age_enabled &&
                      graphConfig.output_target === "object_store_and_age"
                        ? "Auto-sync on build"
                        : "Portable build"}
                    </span>
                    <span className="rounded-full border border-slate-200/70 bg-white/85 px-3 py-1 text-[11px] font-semibold text-slate-600 shadow-sm dark:border-slate-700/70 dark:bg-slate-950/80 dark:text-slate-200">
                      Primary retrieval ·{" "}
                      {retrievalModeLabel(graphConfig.default_retrieval_mode)}
                    </span>
                    {buildUsesStrictAgeDefault && (
                      <span className="rounded-full border border-emerald-200 bg-emerald-50 px-3 py-1 text-[11px] font-semibold text-emerald-700 shadow-sm dark:border-emerald-500/30 dark:bg-emerald-500/10 dark:text-emerald-100">
                        Strict AGE default
                      </span>
                    )}
                    {knowledgeOptionsQuery.data?.age_enabled &&
                      !buildUsesAgePrimary && (
                        <button
                          type="button"
                          onClick={() => applyGraphBuildPreset("age_native")}
                          className="rounded-full border border-emerald-200 bg-emerald-50 px-3 py-1 text-[11px] font-semibold text-emerald-700 shadow-sm transition hover:border-emerald-300 hover:bg-emerald-100 dark:border-emerald-500/30 dark:bg-emerald-500/10 dark:text-emerald-100 dark:hover:border-emerald-500/40 dark:hover:bg-emerald-500/15"
                        >
                          Use AGE graph retrieval
                        </button>
                      )}
                    {knowledgeOptionsQuery.data?.age_enabled &&
                      buildUsesAgePrimary && (
                        <span className="rounded-full border border-emerald-200 bg-emerald-50 px-3 py-1 text-[11px] font-semibold text-emerald-700 shadow-sm dark:border-emerald-500/30 dark:bg-emerald-500/10 dark:text-emerald-100">
                          AGE graph retrieval active
                        </span>
                      )}
                    {knowledgeOptionsQuery.data?.age_enabled &&
                      buildUsesAgePrimary &&
                      !buildUsesStrictAgeDefault && (
                        <button
                          type="button"
                          onClick={() =>
                            patchGraphConfig({
                              strict_age_retrieval_default: true,
                            })
                          }
                          className="rounded-full border border-slate-200/70 bg-white/85 px-3 py-1 text-[11px] font-semibold text-slate-600 shadow-sm transition hover:border-emerald-300 hover:text-emerald-700 dark:border-slate-700/70 dark:bg-slate-950/80 dark:text-slate-200 dark:hover:border-emerald-500/40 dark:hover:text-emerald-100"
                        >
                          Require strict AGE default
                        </button>
                      )}
                    {knowledgeOptionsQuery.data?.age_enabled && (
                      <a
                        href={ageViewerHref}
                        target="_blank"
                        rel="noreferrer"
                        className="rounded-full border border-slate-200/70 bg-white/85 px-3 py-1 text-[11px] font-semibold text-slate-600 shadow-sm transition hover:border-caliber-200 hover:text-caliber-purple dark:border-slate-700/70 dark:bg-slate-950/80 dark:text-slate-200 dark:hover:border-violet-500/40 dark:hover:text-violet-100"
                      >
                        Open AGE Viewer
                      </a>
                    )}
                  </div>
                </div>
                {!ageEnabled && (
                  <div className="mt-4 rounded-xl border border-amber-200 bg-amber-50 px-4 py-3 text-xs leading-relaxed text-amber-800 dark:border-amber-500/30 dark:bg-amber-500/10 dark:text-amber-100">
                    {ageUnavailableReason} CALIBER will keep builds on
                    version-scoped graph artifacts and GraphRAG hybrid retrieval
                    until AGE becomes available again.
                  </div>
                )}
                <div className="mt-4 grid gap-3 lg:grid-cols-3">
                  {buildGraphPresets.map((preset) => {
                    const active = activeGraphBuildPreset === preset.id;
                    return (
                      <button
                        key={preset.id}
                        type="button"
                        onClick={() => applyGraphBuildPreset(preset.id)}
                        aria-pressed={active}
                        data-testid={`kb-graph-preset-${preset.id}`}
                        aria-label={`Graph preset ${preset.label}`}
                        className={`rounded-2xl border px-4 py-4 text-left shadow-card transition-all duration-300 hover:-translate-y-0.5 ${
                          active
                            ? "border-caliber-200 bg-caliber-50 text-caliber-700 dark:border-violet-500/40 dark:bg-violet-500/15 dark:text-violet-100"
                            : "border-slate-200/70 bg-white/90 text-slate-600 hover:border-caliber-200 hover:text-caliber-purple dark:border-slate-700/70 dark:bg-slate-950/90 dark:text-slate-200 dark:hover:border-violet-500/40 dark:hover:text-violet-100"
                        }`}
                      >
                        <div className="flex flex-wrap items-center justify-between gap-2">
                          <div className="text-[11px] font-bold uppercase tracking-[0.16em] text-slate-500 dark:text-slate-400">
                            {preset.eyebrow}
                          </div>
                          {preset.recommended && (
                            <span className="rounded-full border border-emerald-200 bg-emerald-50 px-2.5 py-1 text-[10px] font-semibold uppercase tracking-[0.12em] text-emerald-700 dark:border-emerald-500/30 dark:bg-emerald-500/10 dark:text-emerald-200">
                              Recommended
                            </span>
                          )}
                        </div>
                        <div className="mt-2 text-sm font-semibold text-slate-900 dark:text-slate-100">
                          {preset.label}
                        </div>
                        <div className="mt-1 text-xs leading-relaxed text-slate-500 dark:text-slate-400">
                          {preset.description}
                        </div>
                        <div className="mt-3 flex flex-wrap gap-2">
                          {preset.badges.map((badge) => (
                            <span
                              key={`${preset.id}-${badge}`}
                              className="rounded-full border border-slate-200 bg-slate-50 px-2.5 py-1 text-[11px] font-semibold text-slate-500 dark:border-slate-700/70 dark:bg-slate-900/80 dark:text-slate-300"
                            >
                              {badge}
                            </span>
                          ))}
                        </div>
                      </button>
                    );
                  })}
                </div>
                {activeGraphBuildPreset === "custom" && (
                  <div className="mt-3 rounded-xl border border-blue-200 bg-blue-50 px-4 py-3 text-xs leading-relaxed text-blue-800 dark:border-blue-500/30 dark:bg-blue-500/10 dark:text-blue-100">
                    Custom graph profile active. The settings below no longer
                    match one of the canned GraphRAG presets exactly, which is
                    useful when you want to tune AGE traversal, rerank weight,
                    or sync target by hand.
                  </div>
                )}
                <div className="mt-4 grid gap-4 xl:grid-cols-[minmax(0,1.05fr)_minmax(0,0.95fr)]">
                  <div className="grid gap-4 md:grid-cols-2">
                    <label className="block space-y-2 text-sm">
                      <span className="font-medium text-slate-700">
                        Graph extractor
                      </span>
                      <select
                        value={graphConfig.extractor_backend}
                        onChange={(event) =>
                          patchGraphConfig({
                            extractor_backend: event.target
                              .value as KnowledgeGraphConfig["extractor_backend"],
                          })
                        }
                        className="form-input w-full"
                      >
                        {(
                          knowledgeOptionsQuery.data?.graph_extractors ?? []
                        ).map((item) => (
                          <option key={item.id} value={item.id}>
                            {item.name}
                          </option>
                        ))}
                      </select>
                      <span className="text-xs text-slate-400">
                        {
                          graphExtractorMap.get(graphConfig.extractor_backend)
                            ?.description
                        }
                      </span>
                    </label>
                    <label className="block space-y-2 text-sm">
                      <span className="font-medium text-slate-700">
                        Graph sync target
                      </span>
                      <select
                        value={graphConfig.output_target}
                        onChange={(event) =>
                          patchGraphConfig({
                            output_target: event.target
                              .value as KnowledgeGraphConfig["output_target"],
                          })
                        }
                        className="form-input w-full"
                      >
                        {(
                          knowledgeOptionsQuery.data?.graph_output_targets ?? []
                        ).map((item) => (
                          <option key={item.id} value={item.id}>
                            {item.name}
                          </option>
                        ))}
                      </select>
                      <span className="text-xs text-slate-400">
                        {
                          graphOutputTargetMap.get(graphConfig.output_target)
                            ?.description
                        }
                      </span>
                    </label>
                    <label className="block space-y-2 text-sm">
                      <span className="font-medium text-slate-700">
                        Default retrieval path
                      </span>
                      <select
                        value={graphConfig.default_retrieval_mode}
                        onChange={(event) =>
                          patchGraphConfig({
                            default_retrieval_mode: event.target
                              .value as KnowledgeGraphConfig["default_retrieval_mode"],
                          })
                        }
                        className="form-input w-full"
                      >
                        {(
                          knowledgeOptionsQuery.data?.retrieval_modes ?? []
                        ).map((item) => {
                          const disabled =
                            item.id === "age_graph" &&
                            graphConfig.output_target !==
                              "object_store_and_age";
                          return (
                            <option
                              key={item.id}
                              value={item.id}
                              disabled={disabled}
                            >
                              {item.name}
                            </option>
                          );
                        })}
                      </select>
                      <span className="text-xs text-slate-400">
                        {graphConfig.default_retrieval_mode === "age_graph"
                          ? "Open the Playground and linked graph actions in AGE-native mode for this version by default."
                          : graphConfig.default_retrieval_mode ===
                              "graph_hybrid"
                            ? "Start from GraphRAG hybrid so dense recall and graph-aware expansion are combined automatically."
                            : "Keep the version default focused on pure embedding similarity over chunks."}
                      </span>
                    </label>
                    {graphConfig.extractor_backend === "spacy" && (
                      <label className="block space-y-2 text-sm md:col-span-2">
                        <span className="font-medium text-slate-700">
                          spaCy model
                        </span>
                        <input
                          value={graphConfig.spacy_model ?? ""}
                          onChange={(event) =>
                            patchGraphConfig({
                              spacy_model: event.target.value,
                            })
                          }
                          className="form-input w-full"
                          placeholder="e.g. en_core_web_sm"
                        />
                        <span className="text-xs text-slate-400">
                          Use any installed spaCy model. CALIBER records a
                          fallback if the requested model is unavailable.
                        </span>
                      </label>
                    )}
                    <label className="block space-y-2 text-sm">
                      <span className="font-medium text-slate-700">
                        Retrieval strength
                      </span>
                      <select
                        value={graphConfig.retrieval_strength}
                        onChange={(event) =>
                          patchGraphConfig({
                            retrieval_strength: event.target
                              .value as KnowledgeGraphConfig["retrieval_strength"],
                          })
                        }
                        className="form-input w-full"
                      >
                        {(
                          knowledgeOptionsQuery.data
                            ?.graph_retrieval_strengths ?? []
                        ).map((item) => (
                          <option key={item.id} value={item.id}>
                            {item.name}
                          </option>
                        ))}
                      </select>
                      <span className="text-xs text-slate-400">
                        {
                          graphRetrievalStrengthMap.get(
                            graphConfig.retrieval_strength,
                          )?.description
                        }
                      </span>
                    </label>
                    {knowledgeOptionsQuery.data?.age_enabled && (
                      <label className="block space-y-2 text-sm">
                        <span className="font-medium text-slate-700">
                          AGE seed mode
                        </span>
                        <select
                          value={graphConfig.age_seed_mode}
                          onChange={(event) =>
                            patchGraphConfig({
                              age_seed_mode: event.target
                                .value as KnowledgeAgeSeedMode,
                            })
                          }
                          className="form-input w-full"
                          disabled={
                            graphConfig.output_target !== "object_store_and_age"
                          }
                        >
                          {(
                            knowledgeOptionsQuery.data?.graph_age_seed_modes ??
                            []
                          ).map((item) => (
                            <option key={item.id} value={item.id}>
                              {item.name}
                            </option>
                          ))}
                        </select>
                        <span className="text-xs text-slate-400">
                          {graphConfig.output_target === "object_store_and_age"
                            ? graphAgeSeedModeMap.get(graphConfig.age_seed_mode)
                                ?.description
                            : "This seed policy applies once AGE graph retrieval is enabled for the build target."}
                        </span>
                      </label>
                    )}
                    <label className="block space-y-2 text-sm">
                      <span className="font-medium text-slate-700">
                        Max entities / chunk
                      </span>
                      <input
                        type="number"
                        min={1}
                        max={32}
                        value={graphConfig.max_entities_per_chunk}
                        onChange={(event) =>
                          patchGraphConfig({
                            max_entities_per_chunk:
                              Number(event.target.value) || 1,
                          })
                        }
                        className="form-input w-full"
                      />
                    </label>
                    <label className="block space-y-2 text-sm">
                      <span className="font-medium text-slate-700">
                        Min entity mentions
                      </span>
                      <input
                        type="number"
                        min={1}
                        max={50}
                        value={graphConfig.minimum_entity_mentions}
                        onChange={(event) =>
                          patchGraphConfig({
                            minimum_entity_mentions:
                              Number(event.target.value) || 1,
                          })
                        }
                        className="form-input w-full"
                      />
                    </label>
                    <label className="block space-y-2 text-sm">
                      <span className="font-medium text-slate-700">
                        Min relationship weight
                      </span>
                      <input
                        type="number"
                        min={0}
                        step={0.5}
                        value={graphConfig.minimum_relationship_weight}
                        onChange={(event) =>
                          patchGraphConfig({
                            minimum_relationship_weight:
                              Number(event.target.value) || 0,
                          })
                        }
                        className="form-input w-full"
                      />
                    </label>
                    {knowledgeOptionsQuery.data?.age_enabled && (
                      <>
                        <label className="block space-y-2 text-sm">
                          <span className="font-medium text-slate-700">
                            AGE traversal hops
                          </span>
                          <select
                            aria-label="AGE traversal hops"
                            value={String(graphConfig.age_traversal_hops)}
                            onChange={(event) =>
                              patchGraphConfig({
                                age_traversal_hops:
                                  Number(event.target.value) || 0,
                              })
                            }
                            className="form-input w-full"
                            disabled={
                              graphConfig.output_target !==
                              "object_store_and_age"
                            }
                          >
                            <option value="0">0 · Direct entities only</option>
                            <option value="1">1 · One-hop expansion</option>
                            <option value="2">2 · Two-hop expansion</option>
                          </select>
                          <span className="text-xs text-slate-400">
                            {graphConfig.output_target ===
                            "object_store_and_age"
                              ? "Controls how far Apache AGE can walk from the matched entities before dense reranking."
                              : "Select Object store + Apache AGE to enable graph-native traversal."}
                          </span>
                        </label>
                        <label className="block space-y-2 text-sm">
                          <span className="font-medium text-slate-700">
                            AGE candidate pool
                          </span>
                          <input
                            aria-label="AGE candidate pool"
                            type="number"
                            min={4}
                            max={200}
                            value={graphConfig.age_candidate_pool_size}
                            onChange={(event) =>
                              patchGraphConfig({
                                age_candidate_pool_size:
                                  Number(event.target.value) || 4,
                              })
                            }
                            className="form-input w-full"
                            disabled={
                              graphConfig.output_target !==
                              "object_store_and_age"
                            }
                          />
                          <span className="text-xs text-slate-400">
                            {graphConfig.output_target ===
                            "object_store_and_age"
                              ? "Number of graph-matched chunks to collect before dense reranking picks the final answer set."
                              : "This pool is used only when AGE sync is enabled for the build target."}
                          </span>
                        </label>
                        <label className="block space-y-2 text-sm">
                          <span className="font-medium text-slate-700">
                            Dense rerank weight
                          </span>
                          <input
                            aria-label="AGE dense rerank weight"
                            type="number"
                            min={0}
                            max={3}
                            step={0.05}
                            value={graphConfig.age_dense_rerank_weight}
                            onChange={(event) =>
                              patchGraphConfig({
                                age_dense_rerank_weight:
                                  Number(event.target.value) || 0,
                              })
                            }
                            className="form-input w-full"
                            disabled={
                              graphConfig.output_target !==
                              "object_store_and_age"
                            }
                          />
                          <span className="text-xs text-slate-400">
                            {graphConfig.output_target ===
                            "object_store_and_age"
                              ? "Controls how strongly dense similarity reranks AGE chunk candidates. Lower values keep retrieval more graph-first."
                              : "This weight applies when AGE graph retrieval is enabled for the build target."}
                          </span>
                        </label>
                        <label className="flex items-start gap-3 rounded-xl border border-slate-200/70 bg-white/90 px-3 py-3 text-sm md:col-span-2">
                          <input
                            type="checkbox"
                            aria-label="Require strict AGE retrieval by default"
                            checked={Boolean(
                              graphConfig.strict_age_retrieval_default,
                            )}
                            onChange={(event) =>
                              patchGraphConfig({
                                strict_age_retrieval_default:
                                  event.target.checked,
                              })
                            }
                            className="mt-0.5 h-4 w-4 rounded border-slate-300 text-caliber-purple focus:ring-caliber-200"
                            disabled={
                              graphConfig.output_target !==
                              "object_store_and_age"
                            }
                          />
                          <span>
                            <span className="font-medium text-slate-700">
                              Require strict AGE retrieval by default
                            </span>
                            <span className="mt-1 block text-xs leading-relaxed text-slate-400">
                              Save this version so AGE-backed runs stay on the
                              graph-native path unless you explicitly override
                              the fallback behavior in Playground or workflows.
                            </span>
                          </span>
                        </label>
                      </>
                    )}
                  </div>

                  <div className="space-y-4">
                    {buildGraphTargetAutoUpgraded && (
                      <div className="rounded-xl border border-emerald-200 bg-emerald-50 px-4 py-3 text-xs leading-relaxed text-emerald-800">
                        This deployment supports Apache AGE, so the next build
                        is preconfigured to sync its graph there and unlock
                        {` `}
                        {retrievalModeLabel(
                          ageGraphModeOption?.id ?? "age_graph",
                          ageGraphModeOption?.name,
                        )}
                        {` `}
                        right away. Switch the sync target back to
                        object-store-only below if you want a portable build
                        without AGE sync.
                      </div>
                    )}
                    <div className="rounded-xl border border-slate-200/70 bg-white/80 px-4 py-4">
                      <div className="text-xs font-semibold uppercase tracking-[0.14em] text-slate-500">
                        Entity types
                      </div>
                      <div className="mt-3 flex flex-wrap gap-2">
                        {(
                          knowledgeOptionsQuery.data?.graph_entity_types ?? []
                        ).map((item) => {
                          const selected = graphConfig.entity_types.includes(
                            item.id,
                          );
                          return (
                            <button
                              key={item.id}
                              type="button"
                              onClick={() => toggleGraphEntityType(item.id)}
                              className={`rounded-full border px-3 py-1.5 text-[11px] font-semibold transition ${selected ? "border-caliber-200 bg-caliber-50 text-caliber-700" : "border-slate-200 bg-white text-slate-500 hover:border-caliber-200 hover:text-caliber-purple"}`}
                            >
                              {item.name}
                            </button>
                          );
                        })}
                      </div>
                      <div className="mt-3 text-xs leading-relaxed text-slate-500">
                        {graphConfig.entity_types.length > 0
                          ? `${graphConfig.entity_types.length} graph entity types selected for this build.`
                          : "No explicit filter applied. CALIBER keeps all supported graph entity types."}
                      </div>
                    </div>

                    <div className="grid gap-3 sm:grid-cols-3">
                      <div className="rounded-xl border border-slate-200/70 bg-white/80 px-3 py-3">
                        <div className="text-xs font-semibold uppercase tracking-[0.14em] text-slate-500">
                          Artifacts
                        </div>
                        <div className="mt-2 text-sm font-semibold text-slate-900">
                          Entities + relationships
                        </div>
                        <div className="mt-1 text-xs text-slate-500">
                          Writes{" "}
                          <span className="font-mono">entities.jsonl</span>,{" "}
                          <span className="font-mono">relationships.jsonl</span>
                          , and <span className="font-mono">graph.json</span>.
                        </div>
                      </div>
                      <div className="rounded-xl border border-slate-200/70 bg-white/80 px-3 py-3">
                        <div className="text-xs font-semibold uppercase tracking-[0.14em] text-slate-500">
                          Hybrid
                        </div>
                        <div className="mt-2 text-sm font-semibold text-slate-900">
                          {retrievalModeLabel(
                            graphHybridModeOption?.id ?? "graph_hybrid",
                            graphHybridModeOption?.name,
                          )}
                        </div>
                        <div className="mt-1 text-xs text-slate-500">
                          {graphHybridModeOption?.description ??
                            "Graph-aware retrieval boosts chunks through matched entities and relationship neighbors."}
                        </div>
                      </div>
                      <div className="rounded-xl border border-slate-200/70 bg-white/80 px-3 py-3">
                        <div className="text-xs font-semibold uppercase tracking-[0.14em] text-slate-500">
                          AGE
                        </div>
                        <div className="mt-2 text-sm font-semibold text-slate-900">
                          {retrievalModeLabel(
                            ageGraphModeOption?.id ?? "age_graph",
                            ageGraphModeOption?.name,
                          )}
                        </div>
                        <div className="mt-1 text-xs text-slate-500">
                          {ageGraphModeOption?.description ??
                            "Cypher-first graph retrieval over Apache AGE, with configurable dense reranking on returned chunks."}
                        </div>
                        {knowledgeOptionsQuery.data?.age_enabled &&
                          graphConfig.output_target ===
                            "object_store_and_age" && (
                        <div className="mt-2 text-[11px] text-slate-400">
                          {ageSeedModeLabel(graphConfig.age_seed_mode)} ·{" "}
                          {ageTraversalLabel(
                            graphConfig.age_traversal_hops,
                          )}{" "}
                          · pool {graphConfig.age_candidate_pool_size} ·
                          dense x
                          {graphConfig.age_dense_rerank_weight.toFixed(2)}
                          {graphConfig.strict_age_retrieval_default
                            ? " · strict default"
                            : ""}
                        </div>
                      )}
                      </div>
                    </div>

                    {knowledgeOptionsQuery.data?.age_enabled &&
                      graphConfig.output_target === "object_store_and_age" && (
                        <div className="rounded-xl border border-emerald-200 bg-emerald-50 px-4 py-3 text-xs leading-relaxed text-emerald-800">
                          Every completed build syncs into the shared Apache AGE
                          graph{" "}
                          <span className="font-mono">
                            {knowledgeOptionsQuery.data.age_graph_name ??
                              "knowledge_graph"}
                          </span>
                          . The Playground can then use AGE-backed graph
                          retrieval for this version
                          {graphConfig.strict_age_retrieval_default
                            ? " with strict AGE as the saved default."
                            : "."}
                        </div>
                      )}
                  </div>
                </div>
              </div>

              <div className="rounded-2xl border border-slate-200/70 bg-gradient-hero p-4">
                <div className="flex items-start gap-3">
                  <span className="grid h-10 w-10 place-items-center rounded-xl bg-white text-caliber-purple shadow-card">
                    <UploadCloud className="h-5 w-5" />
                  </span>
                  <div>
                    <div className="text-sm font-semibold text-slate-900">
                      Reserved output prefix
                    </div>
                    <div className="mt-1 text-xs leading-relaxed text-slate-500">
                      Build artifacts land under{" "}
                      <span className="font-mono">
                        {knowledgeOptionsQuery.data?.reserved_output_prefix ??
                          ".caliber/knowledge-bases"}
                      </span>{" "}
                      inside the selected bucket. That keeps chunks, manifests,
                      logs, and stats browsable from Object Store without
                      polluting your source folders.
                    </div>
                  </div>
                </div>
              </div>
                </div>
              </Disclosure>

              <button
                type="submit"
                disabled={
                  buildBusy ||
                  !sourceBucket ||
                  selectedSources.length === 0 ||
                  (buildMode === "new" && !newName.trim()) ||
                  !hasAvailableEmbeddingOptions ||
                  Boolean(embeddingSelectionBlockedReason)
                }
                className="btn-primary flex w-full items-center justify-center gap-2"
              >
                <Sparkles className="h-4 w-4" />
                {buildBusy
                  ? "Processing…"
                  : buildMode === "existing"
                    ? "Create version"
                    : "Create knowledge base"}
              </button>
            </div>
          </form>
        </div>
      )}

      {showUseStage &&
        (selectedKnowledgeBase ? (
          <div className="space-y-5">
            <div className="card overflow-hidden">
              <div className="flex flex-wrap items-center justify-between gap-4 border-b border-slate-200/70 px-5 py-4">
                <div>
                  <div className="text-[11px] font-bold uppercase tracking-[0.16em] text-slate-500">
                    Version History
                  </div>
                  <h2 className="mt-2 text-lg font-semibold text-slate-900">
                    {selectedKnowledgeBase.name}
                  </h2>
                  <div className="mt-1 text-sm text-slate-500">
                    {selectedKnowledgeBase.description ||
                      "Inspect prior builds, compare retrieval quality, and activate earlier versions if a new run regresses."}
                  </div>
                </div>
                <div className="flex flex-wrap gap-2 text-xs">
                  <span className="rounded-full border border-slate-200 bg-slate-50 px-3 py-1.5 font-semibold text-slate-600">
                    Bucket {selectedKnowledgeBase.source_bucket}
                  </span>
                  <span className="rounded-full border border-slate-200 bg-slate-50 px-3 py-1.5 font-semibold text-slate-600">
                    Active {selectedKnowledgeBase.active_version_id ?? "—"}
                  </span>
                </div>
              </div>
              <div className="overflow-x-auto">
                <table className="min-w-full text-sm">
                  <thead className="bg-slate-50 text-xs uppercase tracking-[0.14em] text-slate-500">
                    <tr>
                      <th className="px-4 py-3 text-left">Version</th>
                      <th className="px-4 py-3 text-left">Strategy</th>
                      <th className="px-4 py-3 text-left">Embedding</th>
                      <th className="px-4 py-3 text-left">Chunks</th>
                      <th className="px-4 py-3 text-left">Graph</th>
                      <th className="px-4 py-3 text-left">Status</th>
                      <th className="px-4 py-3 text-left">Created</th>
                      <th className="px-4 py-3 text-right">Actions</th>
                    </tr>
                  </thead>
                  <tbody>
                    {versions.map((version) => {
                      const active =
                        selectedKnowledgeBase.active_version_id ===
                        version.knowledge_base_version_id;
                      const inspected =
                        inspectedVersionId ===
                        version.knowledge_base_version_id;
                      return (
                        <tr
                          key={version.knowledge_base_version_id}
                          className={
                            inspected
                              ? "bg-caliber-50/50"
                              : "border-t border-slate-100"
                          }
                        >
                          <td className="px-4 py-3">
                            <button
                              type="button"
                              onClick={() =>
                                setInspectedVersionId(
                                  version.knowledge_base_version_id,
                                )
                              }
                              className="font-semibold text-slate-800 hover:text-caliber-purple"
                            >
                              v{version.version_number}
                            </button>
                            <div className="mt-1 text-xs text-slate-400">
                              {version.knowledge_base_version_id}
                            </div>
                          </td>
                          <td className="px-4 py-3 text-slate-600">
                            {version.chunking_strategy}
                          </td>
                          <td className="px-4 py-3 text-slate-600">
                            {version.embedding_model}
                          </td>
                          <td className="px-4 py-3 text-slate-600">
                            {chunkCount(version)}
                          </td>
                          <td className="px-4 py-3 text-slate-600">
                            <div>{entityCount(version)} entities</div>
                            <div className="mt-1 text-xs text-slate-400">
                              {relationshipCount(version)} relationships ·{" "}
                              {graphTargetLabel(
                                version.graph_config.output_target,
                              )}
                            </div>
                            <div className="mt-1 text-xs text-slate-400">
                              AGE{" "}
                              {String(
                                version.summary?.age_sync_status ?? "n/a",
                              )}
                            </div>
                          </td>
                          <td className="px-4 py-3">
                            <span
                              className={`rounded-full px-2.5 py-1 text-[11px] font-semibold ${version.status === "completed" ? "bg-emerald-50 text-emerald-700" : version.status === "failed" ? "bg-red-50 text-red-700" : "bg-amber-50 text-amber-700"}`}
                            >
                              {version.status}
                            </span>
                          </td>
                          <td className="px-4 py-3 text-slate-500">
                            {formatDate(version.created_at)}
                          </td>
                          <td className="px-4 py-3 text-right">
                            <div className="flex justify-end gap-2">
                              <button
                                type="button"
                                onClick={() => {
                                  setPlaygroundVersionIds([
                                    version.knowledge_base_version_id,
                                  ]);
                                  setActiveTab("playground");
                                }}
                                className="rounded-lg border border-slate-200 bg-white px-2.5 py-1.5 text-xs font-semibold text-slate-600 hover:border-caliber-200 hover:text-caliber-purple"
                              >
                                Open playground
                              </button>
                              {!active && version.status === "completed" && (
                                <button
                                  type="button"
                                  onClick={() => void activateVersion(version)}
                                  className="rounded-lg border border-slate-200 bg-white px-2.5 py-1.5 text-xs font-semibold text-slate-600 hover:border-caliber-200 hover:text-caliber-purple"
                                >
                                  Activate
                                </button>
                              )}
                              {active && (
                                <span className="rounded-lg bg-caliber-50 px-2.5 py-1.5 text-xs font-semibold text-caliber-700">
                                  Active
                                </span>
                              )}
                            </div>
                          </td>
                        </tr>
                      );
                    })}
                  </tbody>
                </table>
              </div>
            </div>

            {/* Shared version-management panel: Promote (activate) + Roll back
                (re-activate the prior version) with the advisory gate. */}
            <div className="card p-5" data-testid="kb-version-management">
              <div className="text-[11px] font-bold uppercase tracking-[0.16em] text-slate-500">
                Promote &amp; Roll back
              </div>
              <div className="mt-3">
                <VersionPanel adapter={knowledgeBaseVersionAdapter} />
              </div>
            </div>

            {/* Where used / query via API — a simple usage panel for the Use stage. */}
            <div className="card p-5" data-testid="kb-use-usage">
              <div className="text-[11px] font-bold uppercase tracking-[0.16em] text-slate-500">
                Where used · Query via API
              </div>
              <div className="mt-2 text-sm text-slate-600">
                Point an agent or retrieval call at this knowledge base by id. The
                active version{" "}
                <span className="font-mono text-slate-800">
                  {selectedKnowledgeBase.active_version_id ?? "none"}
                </span>{" "}
                is served when no version is pinned.
              </div>
              <pre className="mt-3 overflow-x-auto rounded-xl bg-slate-900 px-3 py-3 text-xs leading-5 text-slate-100">
                {`POST /knowledge/query\n{\n  "knowledge_base_id": "${selectedKnowledgeBase.knowledge_base_id}",\n  "question": "…"\n}`}
              </pre>
            </div>
          </div>
        ) : (
          <EmptyPanel
            title="Select a knowledge base"
            body="Choose a library entry to inspect its version history."
          />
        ))}

      {showExploreChunks &&
        (selectedKnowledgeBase ? (
          selectedVersion ? (
            <div className="grid gap-5 xl:grid-cols-[minmax(0,1.05fr)_minmax(340px,0.95fr)]">
                <div className="card overflow-hidden">
                  <div className="border-b border-slate-200/70 px-5 py-4">
                    <div className="flex items-center justify-between gap-3">
                      <div>
                        <div className="text-[11px] font-bold uppercase tracking-[0.16em] text-slate-500">
                          Chunk Browser
                        </div>
                        <h3 className="mt-2 text-lg font-semibold text-slate-900">
                          Inspect retrieved units for v
                          {selectedVersion.version_number}
                        </h3>
                      </div>
                      <button
                        type="button"
                        onClick={() =>
                          void invalidate([
                            "knowledge-bases",
                            inspectedVersionId ?? "",
                            "chunks",
                            chunkSearch,
                            chunkSourceFilter,
                          ])
                        }
                        className="btn-ghost !px-2.5 !py-1.5"
                      >
                        <RefreshCw className="h-4 w-4" /> Refresh
                      </button>
                    </div>
                  </div>
                  <div className="space-y-4 p-5">
                    <div className="grid gap-4 md:grid-cols-2">
                      <SearchInput
                        value={chunkSearch}
                        onChange={setChunkSearch}
                        placeholder="Search chunk content…"
                        ariaLabel="Search chunks"
                        className="w-full"
                      />
                      <select
                        value={chunkSourceFilter}
                        onChange={(event) =>
                          setChunkSourceFilter(event.target.value)
                        }
                        className="form-input w-full"
                      >
                        <option value="">All source documents</option>
                        {(sourcesQuery.data ?? []).map((source) => (
                          <option
                            key={source.knowledge_base_source_id}
                            value={source.object_key}
                          >
                            {source.object_name}
                          </option>
                        ))}
                      </select>
                    </div>
                    <div className="space-y-3">
                      {(chunksQuery.data ?? []).map((chunk) => (
                        <div
                          key={chunk.knowledge_base_chunk_id}
                          className="rounded-2xl border border-slate-200/70 bg-slate-50/60 p-4"
                        >
                          <div className="flex flex-wrap items-center justify-between gap-3 text-xs text-slate-400">
                            <span>
                              {chunk.source_name} · chunk{" "}
                              {chunk.chunk_index + 1}
                            </span>
                            <span>
                              {chunk.token_count} tokens · {chunk.char_count}{" "}
                              chars
                            </span>
                          </div>
                          <pre className="mt-3 whitespace-pre-wrap text-sm leading-6 text-slate-700">
                            {chunk.content}
                          </pre>
                        </div>
                      ))}
                      {(chunksQuery.data ?? []).length === 0 && (
                        <div className="rounded-2xl border border-dashed border-slate-200 bg-slate-50/60 px-4 py-8 text-center text-sm text-slate-400">
                          No chunks matched the current filters.
                        </div>
                      )}
                    </div>
                  </div>
                </div>

                <div className="space-y-5">
                  <div className="card p-5">
                    <div className="text-[11px] font-bold uppercase tracking-[0.16em] text-slate-500">
                      Version Summary
                    </div>
                    <div className="mt-4 grid gap-3">
                      <SummaryRow
                        label="Status"
                        value={selectedVersion.status}
                      />
                      <SummaryRow
                        label="Chunk count"
                        value={String(chunkCount(selectedVersion))}
                      />
                      <SummaryRow
                        label="Entities"
                        value={String(entityCount(selectedVersion))}
                      />
                      <SummaryRow
                        label="Embedding model"
                        value={selectedVersion.embedding_model}
                      />
                      <SummaryRow
                        label="Default retrieval"
                        value={retrievalModeLabel(
                          configuredDefaultRetrievalMode(
                            selectedVersion.graph_config,
                            ageEnabled,
                          ),
                          retrievalModeMap.get(
                            configuredDefaultRetrievalMode(
                              selectedVersion.graph_config,
                              ageEnabled,
                            ),
                          )?.name,
                        )}
                      />
                    </div>
                    <div className="mt-3">
                      <Disclosure
                        summary="More details"
                        testId="kb-version-summary-more"
                      >
                        <div className="grid gap-3">
                          <SummaryRow
                            label="Chunking strategy"
                            value={selectedVersion.chunking_strategy}
                          />
                          <SummaryRow
                            label="Embedding dim"
                            value={
                              selectedVersion.embedding_dimension
                                ? String(selectedVersion.embedding_dimension)
                                : "—"
                            }
                          />
                          <SummaryRow
                            label="Processed sources"
                            value={String(
                              processedSourceCount(selectedVersion),
                            )}
                          />
                          <SummaryRow
                            label="Relationships"
                            value={String(relationshipCount(selectedVersion))}
                          />
                          <SummaryRow
                            label="Graph extractor"
                            value={
                              selectedVersion.graph_config.extractor_backend
                            }
                          />
                          <SummaryRow
                            label="Graph target"
                            value={graphTargetLabel(
                              selectedVersion.graph_config.output_target,
                            )}
                          />
                          <SummaryRow
                            label="Retrieval strength"
                            value={
                              selectedVersion.graph_config.retrieval_strength
                            }
                          />
                          <SummaryRow
                            label="AGE seed mode"
                            value={ageSeedModeLabel(
                              selectedVersion.graph_config.age_seed_mode,
                              graphAgeSeedModeMap.get(
                                selectedVersion.graph_config.age_seed_mode,
                              )?.name,
                            )}
                          />
                          <SummaryRow
                            label="AGE traversal"
                            value={ageTraversalLabel(
                              selectedVersion.graph_config.age_traversal_hops,
                            )}
                          />
                          <SummaryRow
                            label="AGE candidate pool"
                            value={String(
                              selectedVersion.graph_config
                                .age_candidate_pool_size,
                            )}
                          />
                          <SummaryRow
                            label="Dense rerank weight"
                            value={selectedVersion.graph_config.age_dense_rerank_weight.toFixed(
                              2,
                            )}
                          />
                          <SummaryRow
                            label="Strict AGE default"
                            value={
                              selectedVersionStrictAgeDefault
                                ? "Enabled"
                                : "Fallback allowed"
                            }
                          />
                          <SummaryRow
                            label="Retrieval modes"
                            value={graphConfigSummary(
                              selectedVersion,
                              Boolean(knowledgeOptionsQuery.data?.age_enabled),
                            )}
                          />
                          <SummaryRow
                            label="AGE sync"
                            value={String(
                              selectedVersion.summary?.age_sync_status ?? "n/a",
                            )}
                          />
                          <SummaryRow
                            label="Graph artifact"
                            value={
                              selectedVersion.graph_uri
                                ? "graph.json ready"
                                : "Pending"
                            }
                          />
                          <SummaryRow
                            label="Artifacts"
                            value={selectedVersion.output_prefix}
                            mono
                          />
                        </div>
                      </Disclosure>
                    </div>
                    {selectedVersion.error_summary && (
                      <div className="mt-4 rounded-xl border border-red-200 bg-red-50 px-3 py-2 text-sm text-red-700">
                        {selectedVersion.error_summary}
                      </div>
                    )}
                    <div className="mt-4 flex flex-wrap gap-2">
                      <Link
                        to={`/object-store?bucket=${encodeURIComponent(selectedVersion.output_bucket)}&prefix=${encodeURIComponent(selectedVersion.output_prefix)}`}
                        className="btn-ghost !px-2.5 !py-1.5"
                      >
                        <FolderOpen className="h-4 w-4" /> Open artifacts
                      </Link>
                      <button
                        type="button"
                        onClick={() => {
                          openPlaygroundForVersion(selectedVersion);
                        }}
                        className="btn-ghost !px-2.5 !py-1.5"
                      >
                        <Play className="h-4 w-4" /> Compare in playground
                      </button>
                      {selectedVersionAgeReady && (
                        <button
                          type="button"
                          onClick={() =>
                            openAgePlaygroundForVersion(selectedVersion)
                          }
                          className="btn-ghost !px-2.5 !py-1.5"
                        >
                          <Play className="h-4 w-4" /> Query with AGE
                        </button>
                      )}
                      {ageEnabled && selectedVersion.status === "completed" && (
                        <button
                          type="button"
                          onClick={() => void syncVersionToAge(selectedVersion)}
                          disabled={ageSyncBusy}
                          className="btn-ghost !px-2.5 !py-1.5"
                        >
                          <RefreshCw
                            className={`h-4 w-4 ${ageSyncBusy ? "animate-spin" : ""}`}
                          />
                          {ageSyncBusy
                            ? "Syncing…"
                            : selectedVersionAgeActionLabel}
                        </button>
                      )}
                    </div>
                    {ageSyncError && (
                      <div className="mt-4 rounded-xl border border-amber-200 bg-amber-50 px-3 py-2 text-sm text-amber-700">
                        {ageSyncError}
                      </div>
                    )}
                    {isVersionLive(selectedVersion) && (
                      <div className="mt-4 rounded-xl border border-amber-200 bg-amber-50 px-3 py-2 text-sm text-amber-700">
                        This version is still building. Chunk, entity, and graph
                        views refresh automatically while the run is active.
                      </div>
                    )}
                  </div>

                  <div className="card p-5">
                    <div className="flex items-center justify-between gap-3">
                      <div>
                        <div className="text-[11px] font-bold uppercase tracking-[0.16em] text-slate-500">
                          GraphRAG Lineage
                        </div>
                        <div className="mt-2 text-sm font-semibold text-slate-900">
                          Extracted entities, relationships, and graph context
                        </div>
                      </div>
                      <span className="rounded-full bg-slate-100 px-2.5 py-1 text-[11px] font-semibold text-slate-500">
                        {(entitiesQuery.data ?? []).length} nodes
                      </span>
                    </div>
                    <div className="mt-4 grid gap-3 md:grid-cols-2">
                      <div className="rounded-xl border border-slate-200/70 bg-slate-50/70 px-3 py-3">
                        <div className="text-xs font-semibold uppercase tracking-[0.14em] text-slate-500">
                          Top entities
                        </div>
                        <div className="mt-1 text-[11px] text-slate-400">
                          {selectedVersion.graph_config.entity_types.length > 0
                            ? `Filtered to ${selectedVersion.graph_config.entity_types.length} entity types`
                            : "All entity types included"}
                          {` · `}
                          {
                            selectedVersion.graph_config.max_entities_per_chunk
                          }{" "}
                          max / chunk
                        </div>
                        <div className="mt-3 flex flex-wrap gap-2">
                          {(entitiesQuery.data ?? [])
                            .slice(0, 10)
                            .map((entity) => (
                              <span
                                key={entity.knowledge_base_entity_id}
                                className="inline-flex items-center gap-2 rounded-full border border-slate-200 bg-white px-3 py-1.5 text-xs font-medium text-slate-700 shadow-sm"
                              >
                                {entity.label}
                                <span className="rounded-full bg-slate-100 px-2 py-0.5 text-[10px] font-semibold text-slate-500">
                                  {entity.mention_count}
                                </span>
                              </span>
                            ))}
                          {(entitiesQuery.data ?? []).length === 0 && (
                            <span className="text-sm text-slate-400">
                              {isVersionLive(selectedVersion)
                                ? "Entities appear once the build completes."
                                : "No entities were extracted for this version."}
                            </span>
                          )}
                        </div>
                      </div>
                      <div className="rounded-xl border border-slate-200/70 bg-slate-50/70 px-3 py-3">
                        <div className="text-xs font-semibold uppercase tracking-[0.14em] text-slate-500">
                          Top relationships
                        </div>
                        <div className="mt-1 text-[11px] text-slate-400">
                          Min weight{" "}
                          {
                            selectedVersion.graph_config
                              .minimum_relationship_weight
                          }{" "}
                          · AGE{" "}
                          {String(
                            selectedVersion.summary?.age_sync_status ?? "n/a",
                          )}
                        </div>
                        <div className="mt-3 space-y-2">
                          {(relationshipsQuery.data ?? [])
                            .slice(0, 8)
                            .map((relationship) => (
                              <div
                                key={
                                  relationship.knowledge_base_relationship_id
                                }
                                className="rounded-lg border border-slate-200 bg-white px-3 py-2 text-xs text-slate-600 shadow-sm"
                              >
                                <div className="font-semibold text-slate-800">
                                  {relationship.source_entity_label} ↔{" "}
                                  {relationship.target_entity_label}
                                </div>
                                <div className="mt-1 text-slate-400">
                                  {relationship.relationship_type} ·{" "}
                                  {relationship.weight.toFixed(0)} co-occurrence
                                </div>
                              </div>
                            ))}
                          {(relationshipsQuery.data ?? []).length === 0 && (
                            <div className="text-sm text-slate-400">
                              {isVersionLive(selectedVersion)
                                ? "Relationships appear once the build completes."
                                : "No relationships were extracted for this version."}
                            </div>
                          )}
                        </div>
                      </div>
                    </div>
                  </div>

                  <div className="card p-5">
                    <div className="flex items-center justify-between gap-3">
                      <div>
                        <div className="text-[11px] font-bold uppercase tracking-[0.16em] text-slate-500">
                          Source Documents
                        </div>
                        <div className="mt-2 text-sm font-semibold text-slate-900">
                          Files expanded for this version
                        </div>
                      </div>
                      <span className="rounded-full bg-slate-100 px-2.5 py-1 text-[11px] font-semibold text-slate-500">
                        {sourcesQuery.data?.length ?? 0}
                      </span>
                    </div>
                    <div className="mt-4 space-y-3">
                      {(sourcesQuery.data ?? []).map((source) => (
                        <Link
                          key={source.knowledge_base_source_id}
                          to={source.object_store_path}
                          className="flex items-start justify-between gap-3 rounded-xl border border-slate-200/70 bg-slate-50/60 px-3 py-3 text-sm text-slate-600 hover:border-caliber-200 hover:text-caliber-purple"
                        >
                          <div className="min-w-0">
                            <div className="truncate font-medium text-slate-800">
                              {source.object_name}
                            </div>
                            <div className="mt-1 truncate text-xs text-slate-400">
                              {source.object_key}
                            </div>
                          </div>
                          <div className="text-right text-xs text-slate-400">
                            <div>{source.status}</div>
                            <div className="mt-1">
                              {humanSize(source.size_bytes)}
                            </div>
                          </div>
                        </Link>
                      ))}
                    </div>
                  </div>
                </div>
              </div>
          ) : (
            <EmptyPanel
              title="Select a version"
              body="Pick a version from the header switcher to browse its chunks."
            />
          )
        ) : (
          <EmptyPanel
            title="Select a knowledge base"
            body="Choose a knowledge base before browsing chunks."
          />
        ))}

      {showExploreGraph &&
        (selectedKnowledgeBase ? (
          selectedVersion ? (
            <div className="space-y-5">
              <div className="card p-5">
                <div className="flex flex-wrap items-start justify-between gap-4">
                  <div>
                    <div className="text-[11px] font-bold uppercase tracking-[0.16em] text-slate-500">
                      Graph Explorer
                    </div>
                    <h2 className="mt-2 text-lg font-semibold text-slate-900">
                      Inspect the version-scoped knowledge graph
                    </h2>
                    <div className="mt-1 max-w-3xl text-sm text-slate-500">
                      Visualize extracted entities and relationships, confirm
                      whether the version synced into Apache AGE, and jump
                      straight into graph artifacts or the shared AGE viewer
                      before comparing retrieval in the Playground.
                    </div>
                  </div>
                  <div className="flex flex-wrap gap-2 text-xs">
                    <span
                      className={`rounded-full border px-3 py-1.5 font-semibold ${graphStatusTone(selectedVersionAgeStatus)}`}
                    >
                      {graphStatusLabel(selectedVersionAgeStatus)}
                    </span>
                    <span className="rounded-full border border-slate-200 bg-slate-50 px-3 py-1.5 font-semibold text-slate-600">
                      v{selectedVersion.version_number} ·{" "}
                      {entityCount(selectedVersion)} entities ·{" "}
                      {relationshipCount(selectedVersion)} relationships
                    </span>
                  </div>
                </div>

                <div className="mt-5 grid gap-5 xl:grid-cols-[minmax(0,1.08fr)_minmax(320px,0.92fr)]">
                  <div className="rounded-2xl border border-slate-200/70 bg-slate-50/60 p-4">
                    <div className="flex flex-wrap items-start justify-between gap-3">
                      <div>
                        <div className="text-xs font-semibold uppercase tracking-[0.14em] text-slate-500">
                          Visual map
                        </div>
                        <div className="mt-1 text-sm font-semibold text-slate-900">
                          {graphUsedAge
                            ? "AGE neighborhood and relationship trails"
                            : "Version graph entities and relationship trails"}
                        </div>
                        <div className="mt-1 text-xs text-slate-400">
                          Switch between the version-local graph artifacts and
                          the Apache AGE neighborhood view, then tune the
                          traversal before comparing the result in the
                          Playground.
                        </div>
                      </div>
                      <div className="flex flex-wrap gap-2">
                        <button
                          type="button"
                          onClick={() =>
                            openPlaygroundFromGraphView(
                              selectedVersion,
                              selectedGraphEntity
                                ? `What should I know about ${selectedGraphEntity.label}?`
                                : undefined,
                            )
                          }
                          className="rounded-lg border border-emerald-200 bg-emerald-50 px-3 py-1.5 text-xs font-semibold text-emerald-700 transition hover:border-emerald-300 hover:bg-emerald-100 dark:border-emerald-500/30 dark:bg-emerald-500/10 dark:text-emerald-200 dark:hover:border-emerald-500/40 dark:hover:bg-emerald-500/15"
                        >
                          {graphRequestedAge
                            ? selectedVersionAgeReady
                              ? "Use AGE in Playground"
                              : "Test AGE path in Playground"
                            : "Use graph settings in Playground"}
                        </button>
                      </div>
                    </div>

                    <div className="mt-4 grid gap-3 xl:grid-cols-[minmax(0,0.96fr)_minmax(0,1.04fr)]">
                      <div className="xl:col-span-2 rounded-2xl border border-slate-200/70 bg-white/90 p-4 shadow-card">
                        <div className="flex flex-wrap items-start justify-between gap-3">
                          <div>
                            <div className="text-xs font-semibold uppercase tracking-[0.14em] text-slate-500">
                              Graph query profiles
                            </div>
                            <div className="mt-1 text-sm font-semibold text-slate-900">
                              Choose the retrieval posture before fine-tuning
                            </div>
                            <div className="mt-1 text-xs leading-relaxed text-slate-400">
                              Pick a portable GraphRAG profile or an AGE-backed
                              path for this version, then use the controls below
                              only when you want to override the canned shape.
                            </div>
                          </div>
                          <span className="rounded-full border border-slate-200 bg-slate-50 px-3 py-1.5 text-[11px] font-semibold text-slate-600">
                            {activeGraphQueryPresetDefinition?.label ??
                              "Custom graph tuning"}
                          </span>
                        </div>
                        <div className="mt-4 grid gap-2 xl:grid-cols-2">
                          {graphQueryPresets.map((preset) => {
                            const active = activeGraphQueryPreset === preset.id;
                            const disabled = Boolean(
                              preset.age_required && !ageEnabled,
                            );
                            return (
                              <button
                                key={preset.id}
                                type="button"
                                data-testid={`kb-graph-query-preset-${preset.id}`}
                                onClick={() => applyGraphQueryPreset(preset)}
                                disabled={disabled}
                                className={`rounded-2xl border px-4 py-3 text-left transition ${
                                  active
                                    ? "border-caliber-200 bg-caliber-50 text-caliber-700"
                                    : "border-slate-200 bg-white text-slate-600 hover:border-caliber-200 hover:text-caliber-purple"
                                } disabled:cursor-not-allowed disabled:border-slate-200 disabled:bg-slate-100 disabled:text-slate-400`}
                              >
                                <div className="flex flex-wrap items-center justify-between gap-2">
                                  <div className="text-[11px] font-bold uppercase tracking-[0.14em] text-slate-500">
                                    {preset.eyebrow}
                                  </div>
                                  {preset.recommended && (
                                    <span className="rounded-full border border-emerald-200 bg-emerald-50 px-2 py-0.5 text-[10px] font-semibold uppercase tracking-[0.12em] text-emerald-700">
                                      Recommended
                                    </span>
                                  )}
                                </div>
                                <div className="mt-2 text-sm font-semibold text-slate-900">
                                  {preset.label}
                                </div>
                                <div className="mt-1 text-xs leading-relaxed text-slate-400">
                                  {preset.description}
                                </div>
                                <div className="mt-3 flex flex-wrap gap-2">
                                  {preset.badges.map((badge) => (
                                    <span
                                      key={`${preset.id}-${badge}`}
                                      className="rounded-full border border-slate-200 bg-slate-50 px-2.5 py-1 text-[11px] font-semibold text-slate-500"
                                    >
                                      {badge}
                                    </span>
                                  ))}
                                </div>
                              </button>
                            );
                          })}
                        </div>
                        {activeGraphQueryPreset === "custom" && (
                          <div className="mt-3 rounded-xl border border-blue-200 bg-blue-50 px-3 py-2 text-xs leading-relaxed text-blue-800">
                            Custom graph tuning is active for this version. The
                            explorer, probe, and Playground handoff will use the
                            manual settings currently selected below.
                          </div>
                        )}
                      </div>
                      <Disclosure
                        summary="Advanced retrieval"
                        hint="Graph source, entity-type filter, traversal depth, and AGE seed / pool / rerank knobs. Collapsed by default — the selected profile drives the canvas."
                        testId="kb-graph-advanced"
                      >
                      <div className="grid gap-3 sm:grid-cols-2">
                        <label className="block space-y-2 text-sm">
                          <span className="font-medium text-slate-700">
                            Graph source
                          </span>
                          <select
                            value={graphRequestedSource}
                            onChange={(event) =>
                              setGraphRequestedSource(
                                event.target
                                  .value as KnowledgeGraphExploreSource,
                              )
                            }
                            className="form-input w-full"
                            aria-label="Graph source"
                          >
                            {graphSourceOptions.map((option) => (
                              <option key={option.id} value={option.id}>
                                {option.label}
                              </option>
                            ))}
                          </select>
                        </label>
                        <label className="block space-y-2 text-sm">
                          <span className="font-medium text-slate-700">
                            Entity type
                          </span>
                          <select
                            value={graphEntityTypeFilter}
                            onChange={(event) =>
                              setGraphEntityTypeFilter(event.target.value)
                            }
                            className="form-input w-full"
                            aria-label="Graph entity type"
                          >
                            <option value="">All graph entity types</option>
                            {(
                              knowledgeOptionsQuery.data?.graph_entity_types ??
                              []
                            ).map((item) => (
                              <option key={item.id} value={item.id}>
                                {item.name}
                              </option>
                            ))}
                          </select>
                        </label>
                        <label className="block space-y-2 text-sm">
                          <span className="font-medium text-slate-700">
                            Retrieval strength
                          </span>
                          <select
                            value={graphRetrievalStrength}
                            onChange={(event) =>
                              setGraphRetrievalStrength(
                                event.target
                                  .value as KnowledgeGraphConfig["retrieval_strength"],
                              )
                            }
                            className="form-input w-full"
                            aria-label="Graph retrieval strength"
                          >
                            {(
                              knowledgeOptionsQuery.data
                                ?.graph_retrieval_strengths ?? []
                            ).map((item) => (
                              <option key={item.id} value={item.id}>
                                {item.name}
                              </option>
                            ))}
                          </select>
                        </label>
                        {ageEnabled && (
                          <label className="block space-y-2 text-sm">
                            <span className="font-medium text-slate-700">
                              AGE seed mode
                            </span>
                            <select
                              value={graphAgeSeedMode}
                              onChange={(event) =>
                                setGraphAgeSeedMode(
                                  event.target.value as KnowledgeAgeSeedMode,
                                )
                              }
                              className="form-input w-full"
                              aria-label="Graph AGE seed mode"
                              disabled={!graphRequestedAge}
                            >
                              {(
                                knowledgeOptionsQuery.data
                                  ?.graph_age_seed_modes ?? []
                              ).map((item) => (
                                <option key={item.id} value={item.id}>
                                  {item.name}
                                </option>
                              ))}
                            </select>
                          </label>
                        )}
                        {ageEnabled && (
                          <label className="flex items-start gap-3 rounded-xl border border-slate-200/70 bg-white/90 px-3 py-3 text-sm sm:col-span-2">
                            <input
                              type="checkbox"
                              aria-label="Require strict AGE source in graph explorer"
                              checked={graphProbeStrictAge}
                              onChange={(event) =>
                                setGraphProbeStrictAge(event.target.checked)
                              }
                              className="mt-0.5 h-4 w-4 rounded border-slate-300 text-caliber-purple focus:ring-caliber-200"
                              disabled={!graphRequestedAge}
                            />
                            <span>
                              <span className="font-medium text-slate-700">
                                Require strict AGE source
                              </span>
                              <span className="mt-1 block text-xs leading-relaxed text-slate-400">
                                Keep this explorer on Apache AGE only. When
                                enabled, CALIBER will not silently fall back to
                                version-local graph artifacts for this view.
                              </span>
                            </span>
                          </label>
                        )}
                        <label className="block space-y-2 text-sm">
                          <span className="font-medium text-slate-700">
                            Traversal depth
                          </span>
                          <select
                            value={String(graphTraversalHops)}
                            onChange={(event) =>
                              setGraphTraversalHops(
                                Number(event.target.value) || 0,
                              )
                            }
                            className="form-input w-full"
                            aria-label="Graph traversal depth"
                          >
                            <option value="0">0 · Direct seeds only</option>
                            <option value="1">1 · One-hop neighborhood</option>
                            <option value="2">2 · Two-hop neighborhood</option>
                          </select>
                        </label>
                        <label className="block space-y-2 text-sm">
                          <span className="font-medium text-slate-700">
                            Min relationship weight
                          </span>
                          <input
                            type="number"
                            min={0}
                            step={0.5}
                            value={graphMinRelationshipWeight}
                            onChange={(event) =>
                              setGraphMinRelationshipWeight(
                                Number(event.target.value) || 0,
                              )
                            }
                            className="form-input w-full"
                            aria-label="Graph minimum relationship weight"
                          />
                        </label>
                        {ageEnabled && (
                          <>
                            <label className="block space-y-2 text-sm">
                              <span className="font-medium text-slate-700">
                                AGE candidate pool
                              </span>
                              <input
                                type="number"
                                min={4}
                                max={200}
                                value={graphAgeCandidatePool}
                                onChange={(event) =>
                                  setGraphAgeCandidatePool(
                                    Number(event.target.value) || 4,
                                  )
                                }
                                className="form-input w-full"
                                aria-label="Graph AGE candidate pool"
                                disabled={!graphRequestedAge}
                              />
                            </label>
                            <label className="block space-y-2 text-sm">
                              <span className="font-medium text-slate-700">
                                AGE dense rerank weight
                              </span>
                              <input
                                type="number"
                                min={0}
                                max={3}
                                step={0.05}
                                value={graphAgeDenseRerankWeight}
                                onChange={(event) =>
                                  setGraphAgeDenseRerankWeight(
                                    Number(event.target.value) || 0,
                                  )
                                }
                                className="form-input w-full"
                                aria-label="Graph AGE dense rerank weight"
                                disabled={!graphRequestedAge}
                              />
                            </label>
                          </>
                        )}
                        <label className="block space-y-2 text-sm sm:col-span-2">
                          <span className="font-medium text-slate-700">
                            Node limit
                          </span>
                          <input
                            type="number"
                            min={4}
                            max={48}
                            value={graphNodeLimit}
                            onChange={(event) =>
                              setGraphNodeLimit(Number(event.target.value) || 4)
                            }
                            className="form-input w-full"
                            aria-label="Graph node limit"
                          />
                        </label>
                      </div>
                      </Disclosure>

                      <div className="space-y-3">
                        <SearchInput
                          value={graphSearch}
                          onChange={setGraphSearch}
                          ariaLabel="Search graph"
                          placeholder={
                            graphRequestedAge
                              ? "Search AGE entities or concepts…"
                              : "Search entities, relationships, or sources…"
                          }
                          className="w-full"
                        />
                        <div className="rounded-2xl border border-slate-200/70 bg-white/90 px-4 py-3 text-xs text-slate-500">
                          <div className="flex flex-wrap items-center gap-2">
                            <span
                              className={`rounded-full px-2.5 py-1 font-semibold ${graphExplorerStatusTone}`}
                            >
                              {graphExplorerStatusLabel}
                            </span>
                            {graphExplorerQuery.isFetching && (
                              <span className="rounded-full bg-blue-50 px-2.5 py-1 font-semibold text-blue-700">
                                Refreshing…
                              </span>
                            )}
                          </div>
                          <div className="mt-2 leading-relaxed">
                            {graphStrictAgeBlocked
                              ? "Apache AGE was required for this view, so CALIBER kept the explorer on the graph-native path and skipped the normal local fallback."
                              : graphUsedAge
                              ? "This view is executing an AGE-backed neighborhood query over the synced graph for the current version."
                              : graphRequestedAge
                                ? "Apache AGE was requested for this view, but CALIBER is currently serving the version-local graph instead."
                                : "This view is rendering the extracted graph artifacts stored inside CALIBER for the current version."}
                          </div>
                          {ageEnabled && (
                            <div className="mt-2 leading-relaxed">
                              {graphRequestedAge
                                ? `${graphStrictAgeBlocked ? "Requested seed mode" : graphUsedAge ? "Seed mode" : "Requested seed mode"}: ${ageSeedModeLabel(graphEffectiveAgeSeedMode, graphAgeSeedModeMap.get(graphEffectiveAgeSeedMode)?.name)}${graphStrictAgeActive ? " · strict AGE only" : ""}`
                                : "Switch the graph source to Apache AGE to use seed-mode controls for graph-native neighborhood expansion."}
                            </div>
                          )}
                        </div>
                      </div>
                    </div>

                    {graphFallbackReason && (
                      <div className="mt-4 rounded-xl border border-amber-200 bg-amber-50 px-3 py-2 text-sm text-amber-700">
                        {graphFallbackReason}
                      </div>
                    )}

                    {graphView?.matched_entity_labels.length ||
                    graphView?.expanded_entity_labels.length ? (
                      <div className="mt-4 grid gap-3 md:grid-cols-2">
                        <div className="rounded-xl border border-slate-200/70 bg-white/85 px-4 py-3">
                          <div className="text-[11px] font-bold uppercase tracking-[0.14em] text-slate-500">
                            Seed entities
                          </div>
                          <div className="mt-2 flex flex-wrap gap-2">
                            {(graphView?.matched_entity_labels ?? []).map(
                              (label) => (
                                <span
                                  key={`seed-${label}`}
                                  className="rounded-full border border-slate-200 bg-slate-50 px-3 py-1 text-[11px] font-semibold text-slate-600"
                                >
                                  {label}
                                </span>
                              ),
                            )}
                            {(graphView?.matched_entity_labels ?? []).length ===
                              0 && (
                              <span className="text-xs text-slate-400">
                                No direct seed entities matched this filter.
                              </span>
                            )}
                          </div>
                        </div>
                        <div className="rounded-xl border border-slate-200/70 bg-white/85 px-4 py-3">
                          <div className="text-[11px] font-bold uppercase tracking-[0.14em] text-slate-500">
                            Expanded neighborhood
                          </div>
                          <div className="mt-2 flex flex-wrap gap-2">
                            {(graphView?.expanded_entity_labels ?? []).map(
                              (label) => (
                                <span
                                  key={`expanded-${label}`}
                                  className="rounded-full border border-slate-200 bg-slate-50 px-3 py-1 text-[11px] font-semibold text-slate-600"
                                >
                                  {label}
                                </span>
                              ),
                            )}
                            {(graphView?.expanded_entity_labels ?? [])
                              .length === 0 && (
                              <span className="text-xs text-slate-400">
                                No neighboring entities were added at the
                                current traversal depth.
                              </span>
                            )}
                          </div>
                        </div>
                      </div>
                    ) : null}

                    <div className="mt-4">
                      <KnowledgeGraphCanvas
                        entities={graphVisibleEntities}
                        relationships={graphVisibleRelationships}
                        isLoading={
                          graphExplorerQuery.isLoading ||
                          (graphExplorerQuery.isFetching && !graphView)
                        }
                        searchQuery={graphSearch}
                        sourceMode={graphUsedAge ? "age" : "local"}
                        sourceSummary={graphExplorerSourceSummary}
                        graphName={
                          graphUsedAge
                            ? (graphView?.age_graph_name ??
                              knowledgeOptionsQuery.data?.age_graph_name ??
                              null)
                            : null
                        }
                        ageSeedStrategy={
                          graphUsedAge ? graphAgeSeedStrategy : null
                        }
                        selectedEntityId={selectedGraphEntityId}
                        onSelectEntity={setSelectedGraphEntityId}
                      />
                    </div>

                    <div className="mt-4 flex flex-wrap gap-2 text-[11px]">
                      <span className="rounded-full border border-slate-200/70 bg-white/85 px-3 py-1 font-semibold text-slate-600 shadow-sm dark:border-slate-700/70 dark:bg-slate-950/80 dark:text-slate-200">
                        {graphVisibleEntities.length} visible nodes
                      </span>
                      <span className="rounded-full border border-slate-200/70 bg-white/85 px-3 py-1 font-semibold text-slate-600 shadow-sm dark:border-slate-700/70 dark:bg-slate-950/80 dark:text-slate-200">
                        {graphVisibleRelationships.length} visible edges
                      </span>
                      <span
                        className={`rounded-full border px-3 py-1 font-semibold shadow-sm ${
                          graphStrictAgeBlocked
                            ? "border-amber-200 bg-amber-50 text-amber-700 dark:border-amber-500/30 dark:bg-amber-500/10 dark:text-amber-200"
                            : graphUsedAge
                            ? "border-emerald-200 bg-emerald-50 text-emerald-700 dark:border-emerald-500/30 dark:bg-emerald-500/10 dark:text-emerald-200"
                            : graphRequestedAge
                              ? "border-amber-200 bg-amber-50 text-amber-700 dark:border-amber-500/30 dark:bg-amber-500/10 dark:text-amber-200"
                              : "border-slate-200/70 bg-white/85 text-slate-600 dark:border-slate-700/70 dark:bg-slate-950/80 dark:text-slate-200"
                        }`}
                      >
                        {graphExplorerSourceSummary}
                      </span>
                      <span className="rounded-full border border-slate-200/70 bg-white/85 px-3 py-1 font-semibold text-slate-600 shadow-sm dark:border-slate-700/70 dark:bg-slate-950/80 dark:text-slate-200">
                        Extractor{" "}
                        {selectedVersion.graph_config.extractor_backend}
                      </span>
                      <span className="rounded-full border border-slate-200/70 bg-white/85 px-3 py-1 font-semibold text-slate-600 shadow-sm dark:border-slate-700/70 dark:bg-slate-950/80 dark:text-slate-200">
                        Strength {graphRetrievalStrength}
                      </span>
                      {graphStrictAgeActive && (
                        <span className="rounded-full border border-amber-200 bg-amber-50 px-3 py-1 font-semibold text-amber-700 shadow-sm dark:border-amber-500/30 dark:bg-amber-500/10 dark:text-amber-200">
                          Strict AGE only
                        </span>
                      )}
                      {graphRequestedAge && ageEnabled && (
                        <span className="rounded-full border border-slate-200/70 bg-white/85 px-3 py-1 font-semibold text-slate-600 shadow-sm dark:border-slate-700/70 dark:bg-slate-950/80 dark:text-slate-200">
                          Seed{" "}
                          {ageSeedModeLabel(
                            graphEffectiveAgeSeedMode,
                            graphAgeSeedModeMap.get(graphEffectiveAgeSeedMode)
                              ?.name,
                          )}
                        </span>
                      )}
                      {graphRequestedAge && ageEnabled && (
                        <span className="rounded-full border border-slate-200/70 bg-white/85 px-3 py-1 font-semibold text-slate-600 shadow-sm dark:border-slate-700/70 dark:bg-slate-950/80 dark:text-slate-200">
                          Pool {graphAgeCandidatePool}
                        </span>
                      )}
                      {graphRequestedAge && ageEnabled && (
                        <span className="rounded-full border border-slate-200/70 bg-white/85 px-3 py-1 font-semibold text-slate-600 shadow-sm dark:border-slate-700/70 dark:bg-slate-950/80 dark:text-slate-200">
                          Dense x{graphAgeDenseRerankWeight.toFixed(2)}
                        </span>
                      )}
                      {graphAgeSeedStrategy && (
                        <span className="rounded-full border border-emerald-200 bg-emerald-50 px-3 py-1 font-semibold text-emerald-700 shadow-sm dark:border-emerald-500/30 dark:bg-emerald-500/10 dark:text-emerald-200">
                          {ageSeedStrategyLabel(graphAgeSeedStrategy) ??
                            "AGE seed strategy"}
                        </span>
                      )}
                      <span className="rounded-full border border-slate-200/70 bg-white/85 px-3 py-1 font-semibold text-slate-600 shadow-sm dark:border-slate-700/70 dark:bg-slate-950/80 dark:text-slate-200">
                        {ageTraversalLabel(graphTraversalHops)}
                      </span>
                      <span className="rounded-full border border-slate-200/70 bg-white/85 px-3 py-1 font-semibold text-slate-600 shadow-sm dark:border-slate-700/70 dark:bg-slate-950/80 dark:text-slate-200">
                        Min weight {graphMinRelationshipWeight}
                      </span>
                      <span className="rounded-full border border-slate-200/70 bg-white/85 px-3 py-1 font-semibold text-slate-600 shadow-sm dark:border-slate-700/70 dark:bg-slate-950/80 dark:text-slate-200">
                        Limit {graphNodeLimit}
                      </span>
                    </div>
                  </div>

                  <div className="space-y-4">
                    <div
                      data-testid="graph-focus-card"
                      className="rounded-2xl border border-slate-200/70 bg-white p-4 shadow-card dark:border-slate-700/70 dark:bg-slate-950/90"
                    >
                      <div className="flex flex-wrap items-start justify-between gap-3">
                        <div>
                          <div className="text-xs font-semibold uppercase tracking-[0.14em] text-slate-500 dark:text-slate-400">
                            Focused entity
                          </div>
                          <div className="mt-1 text-sm font-semibold text-slate-900 dark:text-slate-100">
                            {selectedGraphEntity
                              ? selectedGraphEntity.label
                              : "Select a node from the explorer"}
                          </div>
                          <div className="mt-1 text-xs text-slate-400 dark:text-slate-500">
                            {selectedGraphEntity
                              ? `${selectedGraphEntity.entity_type} · ${selectedGraphEntity.mention_count} mentions · ${selectedGraphEntity.source_documents.length} documents`
                              : "Use the graph canvas or the entity list below to inspect one node's sources, neighbors, and retrieval path."}
                          </div>
                        </div>
                        {selectedGraphEntity && (
                          <div className="flex flex-wrap gap-2">
                            <button
                              type="button"
                              onClick={() =>
                                setGraphSearch(selectedGraphEntity.label)
                              }
                              className="rounded-lg border border-slate-200 bg-white px-3 py-1.5 text-xs font-semibold text-slate-600 transition hover:border-caliber-200 hover:text-caliber-purple dark:border-slate-700/70 dark:bg-slate-950 dark:text-slate-200 dark:hover:border-violet-500/40 dark:hover:text-violet-100"
                            >
                              Search this node
                            </button>
                            <button
                              type="button"
                              onClick={() => {
                                openPlaygroundFromGraphView(
                                  selectedVersion,
                                  `What should I know about ${selectedGraphEntity.label}?`,
                                );
                              }}
                              className="rounded-lg border border-emerald-200 bg-emerald-50 px-3 py-1.5 text-xs font-semibold text-emerald-700 transition hover:border-emerald-300 hover:bg-emerald-100 dark:border-emerald-500/30 dark:bg-emerald-500/10 dark:text-emerald-200 dark:hover:border-emerald-500/40 dark:hover:bg-emerald-500/15"
                            >
                              {graphRequestedAge
                                ? selectedVersionAgeReady
                                  ? "Ask with AGE"
                                  : "Test AGE retrieval"
                                : "Ask in Playground"}
                            </button>
                          </div>
                        )}
                      </div>
                      {selectedGraphEntity ? (
                        <>
                          <div className="mt-4 grid gap-2">
                            <SummaryRow
                              label="Explorer source"
                              value={graphExplorerSourceSummary}
                            />
                            <SummaryRow
                              label="Hop distance"
                              value={
                                typeof selectedGraphEntity.distance === "number"
                                  ? String(selectedGraphEntity.distance)
                                  : "—"
                              }
                            />
                            <SummaryRow
                              label="Highlighted"
                              value={
                                selectedGraphEntity.highlighted
                                  ? "Seed match"
                                  : "Neighbor"
                              }
                            />
                            <SummaryRow
                              label="Source chunks"
                              value={String(
                                selectedGraphEntity.source_chunks.length,
                              )}
                            />
                            <SummaryRow
                              label="Connected trails"
                              value={String(selectedGraphRelationships.length)}
                            />
                          </div>

                          {selectedGraphEntity.aliases.length > 1 && (
                            <div className="mt-4">
                              <div className="text-[11px] font-bold uppercase tracking-[0.14em] text-slate-500 dark:text-slate-400">
                                Aliases
                              </div>
                              <div className="mt-2 flex flex-wrap gap-2">
                                {selectedGraphEntity.aliases
                                  .slice(0, 6)
                                  .map((alias) => (
                                    <span
                                      key={`${selectedGraphEntity.knowledge_base_entity_id}-${alias}`}
                                      className="rounded-full border border-slate-200 bg-slate-50 px-3 py-1 text-[11px] font-semibold text-slate-600 dark:border-slate-700/70 dark:bg-slate-900/80 dark:text-slate-200"
                                    >
                                      {alias}
                                    </span>
                                  ))}
                              </div>
                            </div>
                          )}

                          <div className="mt-4">
                            <div className="text-[11px] font-bold uppercase tracking-[0.14em] text-slate-500 dark:text-slate-400">
                              Sources
                            </div>
                            <div className="mt-2 space-y-2">
                              {(selectedGraphSourceDetails.length > 0
                                ? selectedGraphSourceDetails.map((source) => ({
                                    key: source.object_key,
                                    label: source.object_name,
                                    path: source.object_store_path,
                                  }))
                                : selectedGraphEntity.source_keys
                                    .slice(0, 4)
                                    .map((sourceKey) => ({
                                      key: sourceKey,
                                      label: sourceKey,
                                      path: objectStoreObjectPath(
                                        selectedKnowledgeBase.source_bucket,
                                        sourceKey,
                                      ),
                                    }))
                              ).map((source) => (
                                <Link
                                  key={`${selectedGraphEntity.knowledge_base_entity_id}-${source.key}`}
                                  to={source.path}
                                  className="flex items-center justify-between gap-3 rounded-xl border border-slate-200/70 bg-slate-50/70 px-3 py-2.5 text-sm text-slate-600 transition hover:border-caliber-200 hover:text-caliber-purple dark:border-slate-700/70 dark:bg-slate-900/70 dark:text-slate-200 dark:hover:border-violet-500/40 dark:hover:text-violet-100"
                                >
                                  <span className="truncate">
                                    {source.label}
                                  </span>
                                  <Eye className="h-4 w-4 shrink-0" />
                                </Link>
                              ))}
                            </div>
                          </div>

                          <div className="mt-4">
                            <div className="text-[11px] font-bold uppercase tracking-[0.14em] text-slate-500 dark:text-slate-400">
                              Connected neighbors
                            </div>
                            <div className="mt-2 space-y-2">
                              {selectedGraphNeighbors.length > 0 ? (
                                selectedGraphNeighbors
                                  .slice(0, 6)
                                  .map(({ neighbor, relationship }) => (
                                    <button
                                      key={`${selectedGraphEntity.knowledge_base_entity_id}-${relationship.knowledge_base_relationship_id}`}
                                      type="button"
                                      aria-label={`Focus related entity ${neighbor.label}`}
                                      onClick={() =>
                                        focusGraphEntity(
                                          neighbor.knowledge_base_entity_id,
                                          neighbor.label,
                                        )
                                      }
                                      className="flex w-full items-start justify-between gap-3 rounded-xl border border-slate-200/70 bg-slate-50/70 px-3 py-2.5 text-left transition hover:border-caliber-200 hover:bg-caliber-50/50 dark:border-slate-700/70 dark:bg-slate-900/70 dark:hover:border-violet-500/40 dark:hover:bg-violet-500/10"
                                    >
                                      <div>
                                        <div className="text-sm font-semibold text-slate-800 dark:text-slate-100">
                                          {neighbor.label}
                                        </div>
                                        <div className="mt-1 text-xs text-slate-400 dark:text-slate-500">
                                          {relationship.relationship_type} ·
                                          weight{" "}
                                          {relationship.weight.toFixed(2)}
                                        </div>
                                      </div>
                                      <span className="rounded-full bg-white px-2.5 py-1 text-[11px] font-semibold text-slate-500 shadow-sm dark:bg-slate-950 dark:text-slate-300">
                                        {typeof neighbor.distance === "number"
                                          ? `${neighbor.distance} hop`
                                          : "node"}
                                      </span>
                                    </button>
                                  ))
                              ) : (
                                <div className="rounded-xl border border-dashed border-slate-200 bg-slate-50/60 px-3 py-4 text-sm text-slate-400 dark:border-slate-700/70 dark:bg-slate-900/70 dark:text-slate-500">
                                  No visible relationship trails are connected
                                  to this node at the current filter depth.
                                </div>
                              )}
                            </div>
                          </div>
                        </>
                      ) : (
                        <div className="mt-4 rounded-xl border border-dashed border-slate-200 bg-slate-50/60 px-3 py-4 text-sm text-slate-400 dark:border-slate-700/70 dark:bg-slate-900/70 dark:text-slate-500">
                          The explorer will preselect the highest-signal graph
                          seed when results are available.
                        </div>
                      )}
                    </div>

                    <div className="rounded-2xl border border-slate-200/70 bg-white p-4 shadow-card dark:border-slate-700/70 dark:bg-slate-950/90">
                      <div className="flex flex-wrap items-start justify-between gap-3">
                        <div>
                          <div className="text-xs font-semibold uppercase tracking-[0.14em] text-slate-500 dark:text-slate-400">
                            Apache AGE Retrieval
                          </div>
                          <div className="mt-1 text-sm font-semibold text-slate-900 dark:text-slate-100">
                            {selectedVersionAgeReady
                              ? "Cypher-first retrieval is configured for this version."
                              : selectedVersionAgeConfigured &&
                                  selectedVersionAgeStatus === "failed"
                                ? "AGE sync failed for this version."
                                : selectedVersionAgeConfigured &&
                                    [
                                      "pending",
                                      "queued",
                                      "processing",
                                    ].includes(selectedVersionAgeStatus)
                                  ? "AGE sync is still running for this version."
                                  : "This version is currently using object-store graph artifacts only."}
                          </div>
                          <div className="mt-1 text-xs text-slate-400 dark:text-slate-500">
                            {selectedVersionAgeReady
                              ? "The Playground can run Apache AGE graph retrieval directly over the synced graph and rerank the returned chunks."
                              : selectedVersionAgeConfigured &&
                                  selectedVersionAgeStatus === "failed"
                                ? "This build targeted Apache AGE, but the sync did not complete. Rebuild the version or inspect the run logs before using the AGE retrieval path."
                                : selectedVersionAgeConfigured &&
                                    [
                                      "pending",
                                      "queued",
                                      "processing",
                                    ].includes(selectedVersionAgeStatus)
                                  ? "The version is waiting for its AGE sync to complete. Until then, the explorer and Playground stay on the local graph artifacts."
                                  : ageEnabled
                                    ? "Use Enable AGE graph retrieval to promote this version now, or switch the next build target to Object store + Apache AGE to keep AGE-native retrieval on future versions."
                                    : ageUnavailableReason}
                          </div>
                        </div>
                        {knowledgeOptionsQuery.data?.age_enabled && (
                          <div className="flex flex-wrap gap-2">
                            {selectedVersion.status === "completed" && (
                              <button
                                type="button"
                                onClick={() =>
                                  void syncVersionToAge(selectedVersion)
                                }
                                disabled={ageSyncBusy}
                                className="rounded-lg border border-slate-200 bg-white px-3 py-1.5 text-xs font-semibold text-slate-600 transition hover:border-caliber-200 hover:text-caliber-purple disabled:cursor-not-allowed disabled:opacity-60 dark:border-slate-700/70 dark:bg-slate-950 dark:text-slate-200 dark:hover:border-violet-500/40 dark:hover:text-violet-100"
                              >
                                {ageSyncBusy
                                  ? "Syncing…"
                                  : selectedVersionAgeActionLabel}
                              </button>
                            )}
                            {selectedVersionAgeReady && (
                              <button
                                type="button"
                                onClick={() =>
                                  openAgePlaygroundForVersion(selectedVersion)
                                }
                                className="rounded-lg border border-emerald-200 bg-emerald-50 px-3 py-1.5 text-xs font-semibold text-emerald-700 transition hover:border-emerald-300 hover:bg-emerald-100 dark:border-emerald-500/30 dark:bg-emerald-500/10 dark:text-emerald-200 dark:hover:border-emerald-500/40 dark:hover:bg-emerald-500/15"
                              >
                                Open AGE in Playground
                              </button>
                            )}
                            <a
                              href={ageViewerHref}
                              target="_blank"
                              rel="noreferrer"
                              className="rounded-lg border border-slate-200 bg-white px-3 py-1.5 text-xs font-semibold text-slate-600 transition hover:border-caliber-200 hover:text-caliber-purple dark:border-slate-700/70 dark:bg-slate-950 dark:text-slate-200 dark:hover:border-violet-500/40 dark:hover:text-violet-100"
                            >
                              Open AGE Viewer
                            </a>
                          </div>
                        )}
                      </div>
                      <div className="mt-4 grid gap-2">
                        <SummaryRow
                          label="Requested path"
                          value={
                            graphRequestedAge
                              ? "Apache AGE neighborhood"
                              : "Version graph artifacts"
                          }
                        />
                        <SummaryRow
                          label="Served path"
                          value={graphExplorerSourceSummary}
                        />
                        <SummaryRow
                          label="Sync target"
                          value={graphTargetLabel(
                            selectedVersion.graph_config.output_target,
                          )}
                        />
                        <SummaryRow
                          label="Sync status"
                          value={selectedVersionAgeStatus}
                        />
                        <SummaryRow
                          label="Graph name"
                          value={String(
                            selectedVersion.summary?.age_graph_name ??
                              knowledgeOptionsQuery.data?.age_graph_name ??
                              "knowledge_graph",
                          )}
                          mono
                        />
                        <SummaryRow
                          label="Traversal strength"
                          value={graphRetrievalStrength}
                        />
                        <SummaryRow
                          label="AGE seed mode"
                          value={ageSeedModeLabel(
                            graphRequestedAge
                              ? graphEffectiveAgeSeedMode
                              : selectedVersion.graph_config.age_seed_mode,
                            graphAgeSeedModeMap.get(
                              graphRequestedAge
                                ? graphEffectiveAgeSeedMode
                                : selectedVersion.graph_config.age_seed_mode,
                            )?.name,
                          )}
                        />
                        <SummaryRow
                          label="Seed strategy"
                          value={
                            ageSeedStrategyLabel(graphAgeSeedStrategy) ?? "—"
                          }
                        />
                        <SummaryRow
                          label="Query entities"
                          value={
                            graphView?.query_entity_labels?.length
                              ? graphView.query_entity_labels.join(", ")
                              : "—"
                          }
                        />
                        <SummaryRow
                          label="Explorer depth"
                          value={ageTraversalLabel(graphTraversalHops)}
                        />
                        <SummaryRow
                          label="Min relationship weight"
                          value={String(graphMinRelationshipWeight)}
                        />
                        <SummaryRow
                          label="Node limit"
                          value={String(graphNodeLimit)}
                        />
                        <SummaryRow
                          label="Candidate pool"
                          value={String(graphAgeCandidatePool)}
                        />
                        <SummaryRow
                          label="Dense rerank weight"
                          value={graphAgeDenseRerankWeight.toFixed(2)}
                        />
                        <SummaryRow
                          label="Synced nodes"
                          value={String(
                            Number(
                              selectedVersion.summary?.age_synced_nodes ?? 0,
                            ),
                          )}
                        />
                        <SummaryRow
                          label="Synced edges"
                          value={String(
                            Number(
                              selectedVersion.summary?.age_synced_edges ?? 0,
                            ),
                          )}
                        />
                      </div>
                      {selectedVersionAgeError && (
                        <div className="mt-4 rounded-xl border border-amber-200 bg-amber-50 px-3 py-2 text-sm text-amber-700">
                          {selectedVersionAgeError}
                        </div>
                      )}
                      {ageSyncError && (
                        <div className="mt-4 rounded-xl border border-amber-200 bg-amber-50 px-3 py-2 text-sm text-amber-700">
                          {ageSyncError}
                        </div>
                      )}
                    </div>

                    <div
                      data-testid="graph-retrieval-probe"
                      className="rounded-2xl border border-slate-200/70 bg-white p-4 shadow-card dark:border-slate-700/70 dark:bg-slate-950/90"
                    >
                      <div className="flex flex-wrap items-start justify-between gap-3">
                        <div>
                          <div className="text-xs font-semibold uppercase tracking-[0.14em] text-slate-500 dark:text-slate-400">
                            Graph Retrieval Probe
                          </div>
                          <div className="mt-1 text-sm font-semibold text-slate-900 dark:text-slate-100">
                            {graphRequestedAge
                              ? "Run the current Apache AGE retrieval settings"
                              : "Run the current GraphRAG hybrid settings"}
                          </div>
                          <div className="mt-1 text-xs leading-relaxed text-slate-400 dark:text-slate-500">
                            This probe uses the same version, graph source,
                            traversal depth, and relationship filter shown in
                            the explorer so you can verify grounded evidence
                            before opening the full compare flow.
                          </div>
                        </div>
                        <div className="flex flex-wrap gap-2 text-[11px]">
                          <span
                            className={`rounded-full px-2.5 py-1 font-semibold ${graphModeTone(graphRequestedAge ? "age_graph" : "graph_hybrid")}`}
                          >
                            {graphRequestedAge
                              ? "Apache AGE graph"
                              : "GraphRAG hybrid"}
                          </span>
                          <span className="rounded-full border border-slate-200 bg-slate-50 px-2.5 py-1 font-semibold text-slate-600 dark:border-slate-700/70 dark:bg-slate-900/80 dark:text-slate-200">
                            {ageTraversalLabel(graphTraversalHops)}
                          </span>
                          <span className="rounded-full border border-slate-200 bg-slate-50 px-2.5 py-1 font-semibold text-slate-600 dark:border-slate-700/70 dark:bg-slate-900/80 dark:text-slate-200">
                            {graphRetrievalStrength}
                          </span>
                          {graphRequestedAge && (
                            <>
                              <span className="rounded-full border border-slate-200 bg-slate-50 px-2.5 py-1 font-semibold text-slate-600 dark:border-slate-700/70 dark:bg-slate-900/80 dark:text-slate-200">
                                Seed{" "}
                                {ageSeedModeLabel(
                                  graphEffectiveAgeSeedMode,
                                  graphAgeSeedModeMap.get(
                                    graphEffectiveAgeSeedMode,
                                  )?.name,
                                )}
                              </span>
                              <span className="rounded-full border border-slate-200 bg-slate-50 px-2.5 py-1 font-semibold text-slate-600 dark:border-slate-700/70 dark:bg-slate-900/80 dark:text-slate-200">
                                Pool {graphAgeCandidatePool}
                              </span>
                              <span className="rounded-full border border-slate-200 bg-slate-50 px-2.5 py-1 font-semibold text-slate-600 dark:border-slate-700/70 dark:bg-slate-900/80 dark:text-slate-200">
                                Dense x{graphAgeDenseRerankWeight.toFixed(2)}
                              </span>
                            </>
                          )}
                        </div>
                      </div>

                      {(selectedGraphEntity || graphSearch.trim()) && (
                        <div className="mt-4 flex flex-wrap gap-2">
                          {selectedGraphEntity && (
                            <button
                              type="button"
                              onClick={() =>
                                setGraphProbeQuestion(
                                  `What should I know about ${selectedGraphEntity.label}?`,
                                )
                              }
                              className="rounded-full border border-slate-200 bg-slate-50 px-3 py-1.5 text-[11px] font-semibold text-slate-600 transition hover:border-caliber-200 hover:text-caliber-purple dark:border-slate-700/70 dark:bg-slate-900/80 dark:text-slate-200 dark:hover:border-violet-500/40 dark:hover:text-violet-100"
                            >
                              Ask about {selectedGraphEntity.label}
                            </button>
                          )}
                          {graphSearch.trim() && (
                            <button
                              type="button"
                              onClick={() =>
                                setGraphProbeQuestion(
                                  `What does ${graphSearch.trim()} connect to in this corpus?`,
                                )
                              }
                              className="rounded-full border border-slate-200 bg-slate-50 px-3 py-1.5 text-[11px] font-semibold text-slate-600 transition hover:border-caliber-200 hover:text-caliber-purple dark:border-slate-700/70 dark:bg-slate-900/80 dark:text-slate-200 dark:hover:border-violet-500/40 dark:hover:text-violet-100"
                            >
                              Use current graph search
                            </button>
                          )}
                        </div>
                      )}

                      <form
                        onSubmit={(event) => void runGraphProbe(event)}
                        className="mt-4 space-y-4"
                      >
                        <textarea
                          value={graphProbeQuestion}
                          onChange={(event) =>
                            setGraphProbeQuestion(event.target.value)
                          }
                          aria-label="Graph retrieval question"
                          className="form-input min-h-[104px] w-full"
                          placeholder={
                            selectedGraphEntity
                              ? `Ask a graph-grounded question about ${selectedGraphEntity.label}…`
                              : "Ask a graph-grounded question for this version…"
                          }
                        />
                        {graphRequestedAge && (
                          <label className="flex items-start gap-3 rounded-xl border border-slate-200/70 bg-slate-50/70 px-3 py-3 text-sm dark:border-slate-700/70 dark:bg-slate-900/70">
                            <input
                              type="checkbox"
                              aria-label="Require strict AGE retrieval in graph probe"
                              checked={graphProbeStrictAge}
                              onChange={(event) =>
                                setGraphProbeStrictAge(event.target.checked)
                              }
                              className="mt-0.5 h-4 w-4 rounded border-slate-300 text-caliber-purple focus:ring-caliber-200"
                            />
                            <span>
                              <span className="font-medium text-slate-700 dark:text-slate-100">
                                Strict AGE retrieval
                              </span>
                              <span className="mt-1 block text-xs leading-relaxed text-slate-400 dark:text-slate-500">
                                Keep this probe on the graph-native path only.
                                When enabled, CALIBER will not silently fall
                                back to hybrid or dense retrieval.
                              </span>
                            </span>
                          </label>
                        )}
                        <div className="flex flex-wrap items-center justify-between gap-3">
                          <div className="text-xs leading-relaxed text-slate-400 dark:text-slate-500">
                            {graphRequestedAge
                              ? `Uses ${graphRetrievalStrength} retrieval strength, ${ageSeedModeLabel(graphEffectiveAgeSeedMode, graphAgeSeedModeMap.get(graphEffectiveAgeSeedMode)?.name)} seeding, ${ageTraversalLabel(graphTraversalHops).toLowerCase()}, pool ${graphAgeCandidatePool}, dense x${graphAgeDenseRerankWeight.toFixed(2)}, and min relationship weight ${graphMinRelationshipWeight}.`
                              : `Uses ${retrievalModeLabel("graph_hybrid")} with retrieval strength ${graphRetrievalStrength} and min relationship weight ${graphMinRelationshipWeight}.`}
                          </div>
                          <div className="flex flex-wrap gap-2">
                            <button
                              type="button"
                              onClick={() =>
                                openPlaygroundFromGraphView(
                                  selectedVersion,
                                  graphProbeQuestion.trim() || undefined,
                                )
                              }
                              className="btn-ghost !px-2.5 !py-1.5"
                            >
                              <MessageSquareText className="h-4 w-4" /> Open in
                              Playground
                            </button>
                            <button
                              type="submit"
                              disabled={
                                graphProbeBusy || !graphProbeQuestion.trim()
                              }
                              className="btn-primary flex items-center gap-2"
                            >
                              <Bot className="h-4 w-4" />
                              {graphProbeBusy
                                ? "Running…"
                                : graphRequestedAge
                                  ? "Run AGE retrieval"
                                  : "Run graph retrieval"}
                            </button>
                          </div>
                        </div>
                      </form>

                      {graphProbeError && (
                        <div className="mt-4 rounded-xl border border-red-200 bg-red-50 px-3 py-2 text-sm text-red-700 dark:border-red-500/30 dark:bg-red-500/10 dark:text-red-200">
                          {graphProbeError}
                        </div>
                      )}

                      {graphProbeResult ? (
                        <div className="mt-4">
                          <KnowledgeQueryCompactResultCard
                            result={graphProbeResult}
                          />
                        </div>
                      ) : (
                        <div className="mt-4 rounded-xl border border-dashed border-slate-200 bg-slate-50/60 px-3 py-4 text-sm text-slate-400 dark:border-slate-700/70 dark:bg-slate-900/70 dark:text-slate-500">
                          Run a question here to inspect the exact chunks,
                          citations, and graph context this version returns with
                          the current graph settings.
                        </div>
                      )}
                    </div>

                    <div className="rounded-2xl border border-slate-200/70 bg-white p-4 shadow-card dark:border-slate-700/70 dark:bg-slate-950/90">
                      <div className="text-xs font-semibold uppercase tracking-[0.14em] text-slate-500 dark:text-slate-400">
                        Artifacts
                      </div>
                      <div className="mt-1 text-sm font-semibold text-slate-900 dark:text-slate-100">
                        Inspect the persisted graph outputs
                      </div>
                      <div className="mt-3 flex flex-wrap gap-2">
                        <Link
                          to={versionArtifactPath(
                            selectedVersion,
                            "graph.json",
                          )}
                          className="rounded-full border border-slate-200 bg-slate-50 px-3 py-1.5 text-xs font-semibold text-slate-600 transition hover:border-caliber-200 hover:text-caliber-purple dark:border-slate-700/70 dark:bg-slate-900/80 dark:text-slate-200 dark:hover:border-violet-500/40 dark:hover:text-violet-100"
                        >
                          graph.json
                        </Link>
                        <Link
                          to={versionArtifactPath(
                            selectedVersion,
                            "entities.jsonl",
                          )}
                          className="rounded-full border border-slate-200 bg-slate-50 px-3 py-1.5 text-xs font-semibold text-slate-600 transition hover:border-caliber-200 hover:text-caliber-purple dark:border-slate-700/70 dark:bg-slate-900/80 dark:text-slate-200 dark:hover:border-violet-500/40 dark:hover:text-violet-100"
                        >
                          entities.jsonl
                        </Link>
                        <Link
                          to={versionArtifactPath(
                            selectedVersion,
                            "relationships.jsonl",
                          )}
                          className="rounded-full border border-slate-200 bg-slate-50 px-3 py-1.5 text-xs font-semibold text-slate-600 transition hover:border-caliber-200 hover:text-caliber-purple dark:border-slate-700/70 dark:bg-slate-900/80 dark:text-slate-200 dark:hover:border-violet-500/40 dark:hover:text-violet-100"
                        >
                          relationships.jsonl
                        </Link>
                        <Link
                          to={objectStorePrefixPath(
                            selectedVersion.output_bucket,
                            selectedVersion.output_prefix,
                          )}
                          className="rounded-full border border-slate-200 bg-slate-50 px-3 py-1.5 text-xs font-semibold text-slate-600 transition hover:border-caliber-200 hover:text-caliber-purple dark:border-slate-700/70 dark:bg-slate-900/80 dark:text-slate-200 dark:hover:border-violet-500/40 dark:hover:text-violet-100"
                        >
                          Open artifact folder
                        </Link>
                      </div>
                    </div>
                  </div>
                </div>
              </div>

              <div className="grid gap-5 xl:grid-cols-[minmax(0,0.94fr)_minmax(0,1.06fr)]">
                <div className="card p-5">
                  <div className="flex items-center justify-between gap-3">
                    <div>
                      <div className="text-[11px] font-bold uppercase tracking-[0.16em] text-slate-500">
                        Entities
                      </div>
                      <div className="mt-2 text-sm font-semibold text-slate-900">
                        Filtered graph nodes
                      </div>
                    </div>
                    <span className="rounded-full bg-slate-100 px-2.5 py-1 text-[11px] font-semibold text-slate-500">
                      {graphVisibleEntities.length}
                    </span>
                  </div>
                  <div className="mt-4 space-y-3">
                    {graphVisibleEntities.map((entity) => (
                      <div
                        key={entity.knowledge_base_entity_id}
                        className={`rounded-xl border px-4 py-3 ${
                          selectedGraphEntityId ===
                          entity.knowledge_base_entity_id
                            ? "border-caliber-200 bg-caliber-50/70"
                            : "border-slate-200/70 bg-slate-50/70"
                        }`}
                      >
                        <div className="flex items-start justify-between gap-3">
                          <div>
                            <div className="flex flex-wrap items-center gap-2">
                              <div className="text-sm font-semibold text-slate-900">
                                {entity.label}
                              </div>
                              {typeof entity.distance === "number" && (
                                <span
                                  className={`rounded-full px-2 py-0.5 text-[10px] font-semibold ${entity.distance === 0 ? "bg-blue-50 text-blue-700" : "bg-slate-100 text-slate-600"}`}
                                >
                                  {entity.distance === 0
                                    ? "seed"
                                    : `${entity.distance} hop`}
                                </span>
                              )}
                            </div>
                            <div className="mt-1 text-xs text-slate-400">
                              {entity.entity_type} ·{" "}
                              {entity.source_documents.length} documents ·{" "}
                              {entity.source_chunks.length} chunks
                            </div>
                          </div>
                          <div className="flex flex-wrap items-center gap-2">
                            <span className="rounded-full bg-white px-2.5 py-1 text-[11px] font-semibold text-slate-600 shadow-sm">
                              {entity.mention_count} mentions
                            </span>
                            <button
                              type="button"
                              aria-label={`Focus entity ${entity.label}`}
                              onClick={() =>
                                focusGraphEntity(
                                  entity.knowledge_base_entity_id,
                                  entity.label,
                                )
                              }
                              className="rounded-full border border-slate-200 bg-white px-2.5 py-1 text-[11px] font-semibold text-slate-500 transition hover:border-caliber-200 hover:text-caliber-purple"
                            >
                              Focus
                            </button>
                          </div>
                        </div>
                        {entity.aliases.length > 1 && (
                          <div className="mt-3 flex flex-wrap gap-2">
                            {entity.aliases.slice(0, 4).map((alias) => (
                              <span
                                key={`${entity.knowledge_base_entity_id}-${alias}`}
                                className="rounded-full border border-slate-200 bg-white px-2.5 py-1 text-[11px] font-semibold text-slate-500"
                              >
                                {alias}
                              </span>
                            ))}
                          </div>
                        )}
                        {entity.source_keys.length > 0 && (
                          <div className="mt-3 text-xs text-slate-500">
                            Sources: {entity.source_keys.slice(0, 3).join(", ")}
                          </div>
                        )}
                      </div>
                    ))}
                    {graphVisibleEntities.length === 0 && (
                      <div className="rounded-2xl border border-dashed border-slate-200 bg-slate-50/60 px-4 py-8 text-center text-sm text-slate-400">
                        {isVersionLive(selectedVersion)
                          ? "Entities appear once the build completes."
                          : "No graph entities matched the current filter."}
                      </div>
                    )}
                  </div>
                </div>

                <div className="card p-5">
                  <div className="flex items-center justify-between gap-3">
                    <div>
                      <div className="text-[11px] font-bold uppercase tracking-[0.16em] text-slate-500">
                        Relationship Trails
                      </div>
                      <div className="mt-2 text-sm font-semibold text-slate-900">
                        Edges available to hybrid and AGE retrieval
                      </div>
                    </div>
                    <span className="rounded-full bg-slate-100 px-2.5 py-1 text-[11px] font-semibold text-slate-500">
                      {graphVisibleRelationships.length}
                    </span>
                  </div>
                  <div className="mt-4 space-y-3">
                    {graphVisibleRelationships.map((relationship) => (
                      <div
                        key={relationship.knowledge_base_relationship_id}
                        className="rounded-xl border border-slate-200/70 bg-slate-50/70 px-4 py-3"
                      >
                        <div className="flex flex-wrap items-start justify-between gap-3">
                          <div>
                            <div className="text-sm font-semibold text-slate-900">
                              {relationship.source_entity_label} →{" "}
                              {relationship.target_entity_label}
                            </div>
                            <div className="mt-1 text-xs text-slate-400">
                              {relationship.relationship_type} ·{" "}
                              {relationship.source_documents.length} documents ·{" "}
                              {relationship.evidence_chunk_ids.length} evidence
                              chunks
                            </div>
                          </div>
                          <div className="flex flex-wrap items-center gap-2">
                            <span className="rounded-full bg-white px-2.5 py-1 text-[11px] font-semibold text-slate-600 shadow-sm">
                              weight {relationship.weight.toFixed(2)}
                            </span>
                            {typeof relationship.hop_distance === "number" && (
                              <span className="rounded-full bg-slate-100 px-2.5 py-1 text-[11px] font-semibold text-slate-500">
                                {relationship.hop_distance === 0
                                  ? "seed edge"
                                  : `${relationship.hop_distance} hop`}
                              </span>
                            )}
                          </div>
                        </div>
                        {relationship.evidence_chunk_ids.length > 0 && (
                          <div className="mt-3 text-xs text-slate-500">
                            Evidence chunks:{" "}
                            {relationship.evidence_chunk_ids
                              .slice(0, 4)
                              .join(", ")}
                          </div>
                        )}
                      </div>
                    ))}
                    {graphVisibleRelationships.length === 0 && (
                      <div className="rounded-2xl border border-dashed border-slate-200 bg-slate-50/60 px-4 py-8 text-center text-sm text-slate-400">
                        {isVersionLive(selectedVersion)
                          ? "Relationships appear once the build completes."
                          : "No graph relationships matched the current filter."}
                      </div>
                    )}
                  </div>
                </div>
              </div>
            </div>
          ) : (
            <KnowledgeGuidancePanel
              title="No version ready yet"
              body="Run the first build for this corpus to unlock the graph explorer, AGE sync status, and graph-native retrieval controls."
              focus="graph"
              ageEnabled={ageEnabled}
              ageUnavailableReason={ageUnavailableReason}
              onOpenBuild={() => setActiveTab("build")}
            />
          )
        ) : (
          <KnowledgeGuidancePanel
            title="No knowledge base yet"
            body="Create the first corpus to inspect version-scoped graph artifacts, Apache AGE sync, and relationship trails here."
            focus="graph"
            ageEnabled={ageEnabled}
            ageUnavailableReason={ageUnavailableReason}
            onOpenBuild={() => setActiveTab("build")}
          />
        ))}

      {showExploreAsk &&
        (selectedKnowledgeBase ? (
          <div className="space-y-5">
            {/* Simplified default Query view: one clean ask box against the
                header-selected version, with a compact retrieval-mode segmented
                control + top-k on one row. Power features (multi-version
                compare, graph tuning) live in the disclosures below. */}
            <form
              onSubmit={(event) => void askKnowledge(event)}
              className="card p-5"
              data-testid="kb-explore-ask"
            >
              <div className="flex items-center gap-2 text-sm font-semibold text-slate-800">
                <MessageSquareText className="h-4 w-4 text-caliber-purple" />
                Ask the corpus
                {switcherVersion && (
                  <span className="rounded-full border border-slate-200 bg-slate-50 px-2 py-0.5 text-[11px] font-semibold text-slate-500">
                    v{switcherVersion.version_number}
                  </span>
                )}
              </div>
              <div className="mt-4 flex flex-wrap items-end gap-3">
                <div className="min-w-[220px]">
                  <div className="text-[11px] font-semibold uppercase tracking-[0.14em] text-slate-500">
                    Retrieval mode
                  </div>
                  <div
                    role="group"
                    aria-label="Retrieval mode"
                    data-testid="kb-explore-mode-segmented"
                    className="mt-1.5 inline-flex flex-wrap gap-1 rounded-xl border border-slate-200 bg-slate-50 p-1"
                  >
                    <button
                      type="button"
                      onClick={() => setPlaygroundRetrievalModes([])}
                      aria-pressed={playgroundUsesBuildDefault ? "true" : "false"}
                      title="Use this version's saved retrieval policy"
                      className={`rounded-lg px-3 py-1 text-xs font-semibold transition ${
                        playgroundUsesBuildDefault
                          ? "bg-white text-caliber-purple shadow-sm"
                          : "text-slate-500 hover:text-slate-700"
                      }`}
                    >
                      Auto
                    </button>
                    {(knowledgeOptionsQuery.data?.retrieval_modes ?? []).map(
                      (mode) => {
                        const active =
                          playgroundRetrievalModes.length === 1 &&
                          playgroundRetrievalModes[0] === mode.id;
                        return (
                          <button
                            key={mode.id}
                            type="button"
                            onClick={() =>
                              setPlaygroundRetrievalModes([
                                mode.id as KnowledgeRetrievalMode,
                              ])
                            }
                            aria-pressed={active ? "true" : "false"}
                            data-testid={`kb-explore-mode-${mode.id}`}
                            title={retrievalModeLabel(mode.id, mode.name)}
                            className={`rounded-lg px-3 py-1 text-xs font-semibold transition ${
                              active
                                ? "bg-white text-caliber-purple shadow-sm"
                                : "text-slate-500 hover:text-slate-700"
                            }`}
                          >
                            {retrievalModeShortLabel(mode.id, mode.name)}
                          </button>
                        );
                      },
                    )}
                  </div>
                </div>
                <label className="space-y-1.5 text-sm">
                  <span className="block text-[11px] font-semibold uppercase tracking-[0.14em] text-slate-500">
                    Top K
                  </span>
                  <input
                    type="number"
                    min={1}
                    max={20}
                    aria-label="Top K chunks"
                    value={playgroundTopK}
                    onChange={(event) =>
                      setPlaygroundTopK(Number(event.target.value) || 1)
                    }
                    className="form-input w-20"
                  />
                </label>
              </div>
              <div className="mt-4 grid gap-4 xl:grid-cols-[minmax(0,1fr)_auto]">
                <textarea
                  value={compareQuestion}
                  onChange={(event) => setCompareQuestion(event.target.value)}
                  className="form-input min-h-[100px] w-full"
                  placeholder="Ask a question about the selected documents…"
                />
                <button
                  type="submit"
                  disabled={
                    queryBusy ||
                    playgroundVersionIds.length === 0 ||
                    !compareQuestion.trim()
                  }
                  className="btn-primary flex items-center justify-center gap-2 xl:h-full xl:min-w-[11rem]"
                >
                  <Bot className="h-4 w-4" />
                  {queryBusy ? "Thinking…" : "Ask"}
                </button>
              </div>
              {queryError && (
                <div className="mt-4 rounded-xl border border-red-200 bg-red-50 px-3 py-2 text-sm text-red-700">
                  {queryError}
                </div>
              )}
            </form>

            <div
              className="rounded-2xl border border-slate-200/70 bg-slate-50/40 dark:border-slate-700/70 dark:bg-slate-900/40"
              data-testid="kb-explore-compare"
            >
              <button
                type="button"
                onClick={() => setPlaygroundCompareOpen((value) => !value)}
                aria-expanded={playgroundCompareOpen ? "true" : "false"}
                className="flex w-full items-center justify-between gap-3 px-4 py-3 text-left"
              >
                <span>
                  <span className="text-sm font-semibold text-slate-800 dark:text-slate-100">
                    Compare versions
                  </span>
                  <span className="mt-0.5 block text-xs text-slate-400 dark:text-slate-400">
                    Run the same question across two versions or retrieval modes
                    side by side.
                  </span>
                </span>
                <ChevronDown
                  className={`h-4 w-4 shrink-0 text-slate-400 transition-transform ${playgroundCompareOpen ? "rotate-180" : ""}`}
                />
              </button>
              {playgroundCompareOpen && (
              <div className="border-t border-slate-200/70 px-4 py-4 dark:border-slate-700/70">
              <div className="flex flex-wrap items-start justify-between gap-4">
                <div>
                  <div className="text-[11px] font-bold uppercase tracking-[0.16em] text-slate-500">
                    GraphRAG Playground
                  </div>
                  <h2 className="mt-2 text-lg font-semibold text-slate-900">
                    Compare answers across versions
                  </h2>
                  <div className="mt-1 text-sm text-slate-500">
                    Select one or two versions, choose one or two retrieval
                    modes, and inspect the exact chunks, matched entities, and
                    graph hints that grounded each response.
                  </div>
                </div>
                <div className="rounded-2xl border border-slate-200/70 bg-slate-50/70 px-4 py-3 text-sm text-slate-600">
                  <div className="font-semibold text-slate-800">
                    Primary version
                  </div>
                  <div className="mt-1">
                    {selectedPrimaryVersion
                      ? `v${selectedPrimaryVersion.version_number}`
                      : "Choose a version"}
                  </div>
                </div>
              </div>
              <div className="mt-5 grid gap-5 xl:grid-cols-[minmax(0,1fr)_minmax(240px,0.9fr)]">
                <div>
                  <div className="text-xs font-semibold uppercase tracking-[0.14em] text-slate-500">
                    Versions
                  </div>
                  <div className="mt-3 grid gap-3 md:grid-cols-2">
                    {versions.map((version) => {
                      const selected = playgroundVersionIds.includes(
                        version.knowledge_base_version_id,
                      );
                      return (
                        <button
                          key={version.knowledge_base_version_id}
                          type="button"
                          onClick={() =>
                            togglePlaygroundVersion(
                              version.knowledge_base_version_id,
                            )
                          }
                          className={`rounded-2xl border px-4 py-3 text-left transition ${selected ? "border-caliber-200 bg-caliber-50 text-caliber-700" : "border-slate-200 bg-white text-slate-600"}`}
                        >
                          <div className="flex items-center justify-between gap-2">
                            <div className="font-semibold">
                              v{version.version_number}
                            </div>
                            <span
                              className={`rounded-full px-2 py-0.5 text-[11px] font-semibold ${version.status === "completed" ? "bg-emerald-50 text-emerald-700" : "bg-amber-50 text-amber-700"}`}
                            >
                              {version.status}
                            </span>
                          </div>
                          <div className="mt-2 text-xs text-slate-400">
                            {version.chunking_strategy} ·{" "}
                            {version.embedding_model}
                          </div>
                          <div className="mt-2 flex flex-wrap gap-1.5 text-[11px]">
                            <span className="rounded-full border border-slate-200 bg-slate-50 px-2 py-0.5 font-semibold text-slate-600">
                              Default{" "}
                              {retrievalModeLabel(
                                configuredDefaultRetrievalMode(
                                  version.graph_config,
                                  ageEnabled,
                                ),
                                retrievalModeMap.get(
                                  configuredDefaultRetrievalMode(
                                    version.graph_config,
                                    ageEnabled,
                                  ),
                                )?.name,
                              )}
                            </span>
                            {isAgeReadyVersion(version, ageEnabled) && (
                              <span className="rounded-full border border-emerald-200 bg-emerald-50 px-2 py-0.5 font-semibold text-emerald-700">
                                AGE ready
                              </span>
                            )}
                            {version.graph_config.strict_age_retrieval_default &&
                              configuredDefaultRetrievalMode(
                                version.graph_config,
                                ageEnabled,
                              ) === "age_graph" && (
                                <span className="rounded-full border border-emerald-200 bg-emerald-50 px-2 py-0.5 font-semibold text-emerald-700">
                                  Strict AGE default
                                </span>
                              )}
                          </div>
                        </button>
                      );
                    })}
                  </div>
                </div>
                <div className="rounded-2xl border border-slate-200/70 bg-slate-50/70 p-4">
                  <div className="text-xs font-semibold uppercase tracking-[0.14em] text-slate-500">
                    Retrieval
                  </div>
                  <label className="mt-3 block space-y-2 text-sm">
                    <span className="font-medium text-slate-700">
                      Top K chunks
                    </span>
                    <input
                      type="number"
                      min={1}
                      max={20}
                      value={playgroundTopK}
                      onChange={(event) =>
                        setPlaygroundTopK(Number(event.target.value) || 1)
                      }
                      className="form-input w-full"
                    />
                  </label>
                  <div className="mt-4 text-xs font-semibold uppercase tracking-[0.14em] text-slate-500">
                    Mode
                  </div>
                  {selectedPrimaryVersion && (
                    <div className="mt-3 flex flex-wrap items-center gap-2">
                      <span
                        className={`rounded-full border px-3 py-1 text-[11px] font-semibold ${
                          playgroundUsesBuildDefault
                            ? "border-caliber-200 bg-caliber-50 text-caliber-700"
                            : "border-slate-200 bg-white text-slate-600"
                        }`}
                      >
                        {playgroundUsesBuildDefault
                          ? "Following build default"
                          : "Custom retrieval override"}
                      </span>
                      <span className="rounded-full border border-slate-200 bg-slate-50 px-3 py-1 text-[11px] font-semibold text-slate-600">
                        Default{" "}
                        {retrievalModeLabel(
                          selectedPrimaryResolvedDefaultModes[0] ?? "dense",
                          retrievalModeMap.get(
                            selectedPrimaryResolvedDefaultModes[0] ?? "dense",
                          )?.name,
                        )}
                      </span>
                      {selectedPrimaryStrictAgeDefault && (
                        <span className="rounded-full border border-emerald-200 bg-emerald-50 px-3 py-1 text-[11px] font-semibold text-emerald-700">
                          Strict AGE default
                        </span>
                      )}
                      {selectedPrimaryDefaultModeFallback && (
                        <span className="rounded-full border border-amber-200 bg-amber-50 px-3 py-1 text-[11px] font-semibold text-amber-700">
                          Configured for AGE once sync completes
                        </span>
                      )}
                      <button
                        type="button"
                        onClick={() => {
                          setPlaygroundRetrievalModes([]);
                          setPlaygroundGraphOverrides(
                            graphOverridesFromConfig(
                              selectedPrimaryVersion.graph_config,
                            ),
                          );
                          setPlaygroundGraphTuningEnabled(false);
                        }}
                        className={`rounded-full border px-3 py-1 text-[11px] font-semibold transition ${
                          playgroundUsesBuildDefault
                            ? "border-caliber-200 bg-caliber-50 text-caliber-700"
                            : "border-slate-200 bg-white text-slate-600 hover:border-caliber-200 hover:text-caliber-purple"
                        }`}
                      >
                        Follow build default
                      </button>
                    </div>
                  )}
                  <div className="mt-2 text-xs leading-relaxed text-slate-400">
                    Leave every mode card below unselected to let each version
                    follow its saved retrieval policy, including AGE-native
                    retrieval when the synced graph is ready.
                  </div>
                  <div className="mt-3 grid gap-2">
                    {(knowledgeOptionsQuery.data?.retrieval_modes ?? []).map(
                      (mode) => {
                        const selected = playgroundRetrievalModes.includes(
                          mode.id as KnowledgeRetrievalMode,
                        );
                        return (
                          <button
                            key={mode.id}
                            type="button"
                            onClick={() =>
                              togglePlaygroundRetrievalMode(
                                mode.id as KnowledgeRetrievalMode,
                              )
                            }
                            data-testid={`kb-playground-mode-${mode.id}`}
                            aria-label={`Playground retrieval mode ${retrievalModeLabel(mode.id, mode.name)}`}
                            className={`rounded-2xl border px-3 py-3 text-left transition ${selected ? "border-caliber-200 bg-caliber-50 text-caliber-700" : "border-slate-200 bg-white text-slate-600"}`}
                          >
                            <div className="font-semibold">
                              {retrievalModeLabel(mode.id, mode.name)}
                            </div>
                            <div className="mt-1 text-xs leading-relaxed text-slate-400">
                              {mode.description}
                            </div>
                          </button>
                        );
                      },
                    )}
                  </div>
                  <div className="mt-4">
                  <Disclosure
                    summary="Advanced retrieval"
                    hint="Query-time graph tuning and shared graph query profiles."
                    testId="kb-explore-advanced"
                  >
                  <div className="rounded-2xl border border-slate-200/70 bg-white/80 p-4">
                    <label className="flex items-start justify-between gap-3">
                      <div>
                        <div className="text-xs font-semibold uppercase tracking-[0.14em] text-slate-500">
                          Query-time graph tuning
                        </div>
                        <div className="mt-1 text-sm font-semibold text-slate-900">
                          Override graph retrieval for this question only
                        </div>
                        <div className="mt-1 text-xs leading-relaxed text-slate-400">
                          Leave this off to use each version's saved build-time
                          graph configuration. Turn it on to force the same
                          GraphRAG or AGE traversal settings across the selected
                          versions.
                        </div>
                      </div>
                      <input
                        type="checkbox"
                        aria-label="Enable query-time graph tuning"
                        checked={playgroundGraphTuningEnabled}
                        onChange={(event) =>
                          setPlaygroundGraphTuningEnabled(event.target.checked)
                        }
                        disabled={!playgroundUsesGraphModes}
                        className="mt-1 h-4 w-4 rounded border-slate-300 text-caliber-purple focus:ring-caliber-200 disabled:opacity-50"
                      />
                    </label>
                    <div className="mt-3 flex flex-wrap gap-2 text-[11px]">
                      <span className="rounded-full border border-slate-200 bg-slate-50 px-3 py-1 font-semibold text-slate-600">
                        {playgroundAgeReadyCount}/
                        {selectedPlaygroundVersions.length} selected versions
                        AGE-ready
                      </span>
                      <span className="rounded-full border border-slate-200 bg-slate-50 px-3 py-1 font-semibold text-slate-600">
                        {playgroundUsesBuildDefault
                          ? `Build default → ${effectivePlaygroundRetrievalModes.map((mode) => retrievalModeLabel(mode)).join(" + ")}`
                          : playgroundUsesAgeGraph
                            ? "AGE graph mode selected"
                            : "Hybrid or dense only"}
                      </span>
                      {playgroundUsesBuildDefault &&
                        selectedPrimaryStrictAgeDefault && (
                          <span className="rounded-full border border-emerald-200 bg-emerald-50 px-3 py-1 font-semibold text-emerald-700 dark:border-emerald-500/30 dark:bg-emerald-500/10 dark:text-emerald-200">
                            Saved strict AGE default
                          </span>
                        )}
                      {playgroundUsesAgeGraph &&
                        playgroundGraphOverrides.strict_age_retrieval && (
                          <span className="rounded-full border border-emerald-200 bg-emerald-50 px-3 py-1 font-semibold text-emerald-700 dark:border-emerald-500/30 dark:bg-emerald-500/10 dark:text-emerald-200">
                            Strict AGE path
                          </span>
                        )}
                      {playgroundUsesAgeGraph && (
                        <span className="rounded-full border border-slate-200 bg-slate-50 px-3 py-1 font-semibold text-slate-600">
                          Seed{" "}
                          {ageSeedModeLabel(
                            playgroundGraphOverrides.age_seed_mode ??
                              selectedPrimaryVersion?.graph_config
                                .age_seed_mode,
                          )}
                        </span>
                      )}
                    </div>
                    {!playgroundUsesGraphModes && (
                      <div className="mt-3 text-xs text-slate-400">
                        Select GraphRAG hybrid or Apache AGE graph above, or
                        follow a graph-aware build default, to enable
                        graph-specific query controls.
                      </div>
                    )}
                    {playgroundUsesAgeGraph &&
                      playgroundGraphOverrides.strict_age_retrieval && (
                        <div className="mt-3 rounded-xl border border-emerald-200 bg-emerald-50 px-3 py-2 text-xs leading-relaxed text-emerald-800 dark:border-emerald-500/30 dark:bg-emerald-500/10 dark:text-emerald-100">
                          Strict AGE path is active. This compare run stays on
                          the synced Apache AGE graph and will not silently fall
                          back to hybrid or dense retrieval.
                        </div>
                      )}
                    {playgroundUsesAgeGraph &&
                      playgroundAgeReadyCount <
                        selectedPlaygroundVersions.length && (
                        <div className="mt-3 rounded-xl border border-amber-200 bg-amber-50 px-3 py-2 text-xs leading-relaxed text-amber-800">
                          Only versions with a successful Apache AGE sync can
                          use the graph-native retrieval path. Versions that are
                          still pending, failed sync, or stayed
                          object-store-only will fall back unless you enable
                          strict AGE mode.
                        </div>
                      )}
                    {playgroundUsesGraphModes && (
                      <div className="mt-4 rounded-2xl border border-slate-200/70 bg-white/90 p-4">
                        <div className="flex flex-wrap items-start justify-between gap-3">
                          <div>
                            <div className="text-xs font-semibold uppercase tracking-[0.14em] text-slate-500">
                              Graph query profiles
                            </div>
                            <div className="mt-1 text-sm font-semibold text-slate-900">
                              Apply a shared retrieval posture to this compare
                              run
                            </div>
                            <div className="mt-1 text-xs leading-relaxed text-slate-400">
                              Choosing a profile turns on query-time graph
                              tuning automatically so every selected version is
                              evaluated against the same retrieval shape.
                            </div>
                          </div>
                          <span className="rounded-full border border-slate-200 bg-slate-50 px-3 py-1.5 text-[11px] font-semibold text-slate-600">
                            {activePlaygroundGraphQueryPresetDefinition?.label ??
                              (playgroundGraphTuningEnabled
                                ? "Custom graph tuning"
                                : "Tuning off")}
                          </span>
                        </div>
                        <div className="mt-4 grid gap-2 xl:grid-cols-2">
                          {graphQueryPresets.map((preset) => {
                            const active =
                              activePlaygroundGraphQueryPreset === preset.id;
                            const disabled = Boolean(
                              preset.age_required && !ageEnabled,
                            );
                            return (
                              <button
                                key={`playground-${preset.id}`}
                                type="button"
                                data-testid={`kb-playground-graph-preset-${preset.id}`}
                                onClick={() =>
                                  applyPlaygroundGraphQueryPreset(preset)
                                }
                                disabled={disabled}
                                className={`rounded-2xl border px-4 py-3 text-left transition ${
                                  active
                                    ? "border-caliber-200 bg-caliber-50 text-caliber-700"
                                    : "border-slate-200 bg-white text-slate-600 hover:border-caliber-200 hover:text-caliber-purple"
                                } disabled:cursor-not-allowed disabled:border-slate-200 disabled:bg-slate-100 disabled:text-slate-400`}
                              >
                                <div className="flex flex-wrap items-center justify-between gap-2">
                                  <div className="text-[11px] font-bold uppercase tracking-[0.14em] text-slate-500">
                                    {preset.eyebrow}
                                  </div>
                                  {preset.recommended && (
                                    <span className="rounded-full border border-emerald-200 bg-emerald-50 px-2 py-0.5 text-[10px] font-semibold uppercase tracking-[0.12em] text-emerald-700">
                                      Recommended
                                    </span>
                                  )}
                                </div>
                                <div className="mt-2 text-sm font-semibold text-slate-900">
                                  {preset.label}
                                </div>
                                <div className="mt-1 text-xs leading-relaxed text-slate-400">
                                  {preset.description}
                                </div>
                              </button>
                            );
                          })}
                        </div>
                        {playgroundGraphTuningEnabled &&
                          activePlaygroundGraphQueryPreset === "custom" && (
                            <div className="mt-3 rounded-xl border border-blue-200 bg-blue-50 px-3 py-2 text-xs leading-relaxed text-blue-800">
                              Custom graph tuning is active for this compare
                              run. The knobs below will be sent as explicit
                              query-time overrides.
                            </div>
                          )}
                      </div>
                    )}
                    {playgroundUsesGraphModes &&
                      playgroundGraphTuningEnabled && (
                        <div className="mt-4 grid gap-3">
                          <label className="block space-y-2 text-sm">
                            <span className="font-medium text-slate-700">
                              Retrieval strength
                            </span>
                            <select
                              value={
                                playgroundGraphOverrides.retrieval_strength ??
                                "balanced"
                              }
                              onChange={(event) =>
                                patchPlaygroundGraphOverrides({
                                  retrieval_strength: event.target
                                    .value as KnowledgeQueryGraphOverrides["retrieval_strength"],
                                })
                              }
                              className="form-input w-full"
                            >
                              {(
                                knowledgeOptionsQuery.data
                                  ?.graph_retrieval_strengths ?? []
                              ).map((item) => (
                                <option key={item.id} value={item.id}>
                                  {item.name}
                                </option>
                              ))}
                            </select>
                          </label>
                          <label className="block space-y-2 text-sm">
                            <span className="font-medium text-slate-700">
                              Min relationship weight
                            </span>
                            <input
                              type="number"
                              min={0}
                              step={0.5}
                              value={
                                playgroundGraphOverrides.minimum_relationship_weight ??
                                1
                              }
                              onChange={(event) =>
                                patchPlaygroundGraphOverrides({
                                  minimum_relationship_weight:
                                    Number(event.target.value) || 0,
                                })
                              }
                              className="form-input w-full"
                            />
                          </label>
                          <label className="block space-y-2 text-sm">
                            <span className="font-medium text-slate-700">
                              AGE seed mode
                            </span>
                            <select
                              aria-label="Playground AGE seed mode"
                              value={
                                playgroundGraphOverrides.age_seed_mode ??
                                "entity_then_text"
                              }
                              onChange={(event) =>
                                patchPlaygroundGraphOverrides({
                                  age_seed_mode: event.target
                                    .value as KnowledgeAgeSeedMode,
                                })
                              }
                              className="form-input w-full"
                              disabled={!playgroundUsesAgeGraph}
                            >
                              {(
                                knowledgeOptionsQuery.data
                                  ?.graph_age_seed_modes ?? []
                              ).map((item) => (
                                <option key={item.id} value={item.id}>
                                  {item.name}
                                </option>
                              ))}
                            </select>
                          </label>
                          <div className="grid gap-3 sm:grid-cols-2">
                            <label className="block space-y-2 text-sm">
                              <span className="font-medium text-slate-700">
                                AGE traversal hops
                              </span>
                              <select
                                aria-label="Playground AGE traversal hops"
                                value={String(
                                  playgroundGraphOverrides.age_traversal_hops ??
                                    1,
                                )}
                                onChange={(event) =>
                                  patchPlaygroundGraphOverrides({
                                    age_traversal_hops:
                                      Number(event.target.value) || 0,
                                  })
                                }
                                className="form-input w-full"
                                disabled={!playgroundUsesAgeGraph}
                              >
                                <option value="0">
                                  0 · Direct entities only
                                </option>
                                <option value="1">1 · One-hop expansion</option>
                                <option value="2">2 · Two-hop expansion</option>
                              </select>
                            </label>
                            <label className="block space-y-2 text-sm">
                              <span className="font-medium text-slate-700">
                                AGE candidate pool
                              </span>
                              <input
                                aria-label="Playground AGE candidate pool"
                                type="number"
                                min={4}
                                max={200}
                                value={
                                  playgroundGraphOverrides.age_candidate_pool_size ??
                                  24
                                }
                                onChange={(event) =>
                                  patchPlaygroundGraphOverrides({
                                    age_candidate_pool_size:
                                      Number(event.target.value) || 4,
                                  })
                                }
                                className="form-input w-full"
                                disabled={!playgroundUsesAgeGraph}
                              />
                            </label>
                          </div>
                          <label className="block space-y-2 text-sm">
                            <span className="font-medium text-slate-700">
                              Dense rerank weight
                            </span>
                            <input
                              aria-label="Playground AGE dense rerank weight"
                              type="number"
                              min={0}
                              max={3}
                              step={0.05}
                              value={
                                playgroundGraphOverrides.age_dense_rerank_weight ??
                                0.35
                              }
                              onChange={(event) =>
                                patchPlaygroundGraphOverrides({
                                  age_dense_rerank_weight:
                                    Number(event.target.value) || 0,
                                })
                              }
                              className="form-input w-full"
                              disabled={!playgroundUsesAgeGraph}
                            />
                          </label>
                          <label className="flex items-start gap-3 rounded-xl border border-slate-200/70 bg-slate-50/70 px-3 py-3 text-sm">
                            <input
                              type="checkbox"
                              aria-label="Require strict AGE retrieval"
                              checked={Boolean(
                                playgroundGraphOverrides.strict_age_retrieval,
                              )}
                              onChange={(event) =>
                                patchPlaygroundGraphOverrides({
                                  strict_age_retrieval: event.target.checked,
                                })
                              }
                              className="mt-0.5 h-4 w-4 rounded border-slate-300 text-caliber-purple focus:ring-caliber-200"
                              disabled={!playgroundUsesAgeGraph}
                            />
                            <span>
                              <span className="font-medium text-slate-700">
                                Strict AGE retrieval
                              </span>
                              <span className="mt-1 block text-xs leading-relaxed text-slate-400">
                                When enabled, AGE mode will not silently fall
                                back to hybrid or dense retrieval. This is
                                useful when you want to verify the graph-native
                                path itself.
                              </span>
                            </span>
                          </label>
                        </div>
                      )}
                  </div>
                  </Disclosure>
                  </div>
                  <div className="mt-3 text-xs text-slate-400">
                    Compare mode keeps the question identical and reruns
                    retrieval per version and retrieval mode, so chunking,
                    embeddings, and graph enrichment are the only variables.
                  </div>
                </div>
              </div>
              </div>
              )}
            </div>

            <div className="space-y-5">
              {chatTurns.map((turn, index) => (
                <div key={`${turn.question}-${index}`} className="space-y-4">
                  <div className="rounded-2xl border border-slate-200/70 bg-white px-5 py-4 shadow-card">
                    <div className="text-[11px] font-bold uppercase tracking-[0.16em] text-slate-500">
                      Question
                    </div>
                    <div className="mt-2 text-base font-medium text-slate-900">
                      {turn.question}
                    </div>
                  </div>
                  <div className="grid gap-4 xl:grid-cols-2 2xl:grid-cols-4">
                    {turn.result.versions.map((result) => (
                      <div
                        key={`${result.knowledge_base_version_id}-${result.retrieval_mode}`}
                        className="card overflow-hidden"
                      >
                        <div className="border-b border-slate-200/70 px-5 py-4">
                          <div className="flex items-start justify-between gap-3">
                            <div>
                              <div className="text-sm font-semibold text-slate-900">
                                v{result.version_number} ·{" "}
                                {result.chunking_strategy}
                              </div>
                              <div className="mt-1 flex flex-wrap items-center gap-2 text-xs text-slate-400">
                                <span>{result.embedding_model}</span>
                                <span
                                  className={`rounded-full px-2 py-0.5 font-semibold ${graphModeTone(result.retrieval_mode)}`}
                                >
                                  {retrievalModeLabel(
                                    result.retrieval_mode,
                                    retrievalModeMap.get(result.retrieval_mode)
                                      ?.name,
                                  )}
                                </span>
                                {result.retrieval_mode === "age_graph" &&
                                  !result.graph_context
                                    .fallback_retrieval_mode && (
                                    <span className="rounded-full border border-emerald-200 bg-emerald-50 px-2 py-0.5 font-semibold text-emerald-700">
                                      AGE-backed
                                    </span>
                                  )}
                                {result.retrieval_mode === "age_graph" &&
                                  result.graph_context
                                    .fallback_retrieval_mode && (
                                    <span className="rounded-full border border-amber-200 bg-amber-50 px-2 py-0.5 font-semibold text-amber-700">
                                      Fell back to{" "}
                                      {retrievalModeLabel(
                                        String(
                                          result.graph_context
                                            .fallback_retrieval_mode,
                                        ),
                                      )}
                                    </span>
                                  )}
                              </div>
                            </div>
                            <div className="rounded-full bg-slate-100 px-3 py-1 text-[11px] font-semibold text-slate-500">
                              {result.timing_ms.total ?? 0} ms
                            </div>
                          </div>
                        </div>
                        <div className="space-y-4 p-5">
                          <div className="rounded-2xl border border-slate-200/70 bg-slate-50/60 p-4 text-sm leading-6 text-slate-700">
                            {result.answer ??
                              result.answer_error ??
                              "No answer returned."}
                          </div>
                          {result.retrieval_mode !== "dense" && (
                            <div
                              className={`rounded-2xl p-4 ${result.retrieval_mode === "age_graph" ? "border border-emerald-100 bg-emerald-50/70" : "border border-blue-100 bg-blue-50/70"}`}
                            >
                              <div className="flex items-center justify-between gap-3">
                                <div
                                  className={`text-xs font-semibold uppercase tracking-[0.14em] ${result.retrieval_mode === "age_graph" ? "text-emerald-700" : "text-blue-700"}`}
                                >
                                  {result.retrieval_mode === "age_graph"
                                    ? "Apache AGE context"
                                    : "Graph context"}
                                </div>
                                <span
                                  className={`rounded-full bg-white px-2.5 py-1 text-[11px] font-semibold ${result.retrieval_mode === "age_graph" ? "text-emerald-700" : "text-blue-700"}`}
                                >
                                  {result.graph_context.boosted_chunk_count ??
                                    0}{" "}
                                  boosted
                                </span>
                              </div>
                              <div className="mt-3 flex flex-wrap gap-2">
                                {(
                                  result.graph_context.matched_entities ?? []
                                ).map((label) => (
                                  <span
                                    key={`${result.knowledge_base_version_id}-${result.retrieval_mode}-${label}`}
                                    className={`rounded-full border bg-white px-3 py-1 text-[11px] font-semibold ${result.retrieval_mode === "age_graph" ? "border-emerald-200 text-emerald-700" : "border-blue-200 text-blue-700"}`}
                                  >
                                    {label}
                                  </span>
                                ))}
                                {(result.graph_context.matched_entities ?? [])
                                  .length === 0 && (
                                  <span
                                    className={`text-xs ${result.retrieval_mode === "age_graph" ? "text-emerald-800/80" : "text-blue-700/80"}`}
                                  >
                                    No direct entity matches for this question.
                                  </span>
                                )}
                              </div>
                              {(result.graph_context.expanded_entities ?? [])
                                .length > 0 && (
                                <div
                                  className={`mt-3 text-xs ${result.retrieval_mode === "age_graph" ? "text-emerald-800" : "text-blue-800"}`}
                                >
                                  Expanded via graph:{" "}
                                  {(
                                    result.graph_context.expanded_entities ?? []
                                  )
                                    .slice(0, 6)
                                    .join(", ")}
                                </div>
                              )}
                              {result.retrieval_mode === "age_graph" &&
                                (result.graph_context.age_seed_strategy ||
                                  typeof result.graph_context
                                    .age_matched_chunk_count === "number") && (
                                  <div className="mt-3 text-xs text-emerald-800">
                                    {ageSeedStrategyLabel(
                                      result.graph_context.age_seed_strategy,
                                    ) ?? "AGE retrieval seeded"}
                                    {typeof result.graph_context
                                      .age_matched_chunk_count === "number"
                                      ? ` · matched ${result.graph_context.age_matched_chunk_count} chunks before rerank`
                                      : ""}
                                  </div>
                                )}
                              {result.graph_context.fallback_reason && (
                                <div
                                  className={`mt-3 text-xs ${result.retrieval_mode === "age_graph" ? "text-emerald-800" : "text-blue-800"}`}
                                >
                                  Extractor fallback:{" "}
                                  {result.graph_context.fallback_reason}
                                </div>
                              )}
                              {result.graph_context.age_graph_name && (
                                <div
                                  className={`mt-3 text-xs ${result.retrieval_mode === "age_graph" ? "text-emerald-800" : "text-blue-800"}`}
                                >
                                  Graph {result.graph_context.age_graph_name}
                                  {result.graph_context.age_configured_seed_mode
                                    ? ` · seed ${ageSeedModeLabel(result.graph_context.age_configured_seed_mode)}`
                                    : ""}
                                  {typeof result.graph_context
                                    .age_traversal_hops === "number"
                                    ? ` · ${result.graph_context.age_traversal_hops} hop traversal`
                                    : ""}
                                  {typeof result.graph_context
                                    .age_candidate_pool_size === "number"
                                    ? ` · pool ${result.graph_context.age_candidate_pool_size}`
                                    : ""}
                                  {typeof result.graph_context
                                    .age_dense_rerank_weight === "number"
                                    ? ` · dense x${Number(result.graph_context.age_dense_rerank_weight).toFixed(2)}`
                                    : ""}
                                  {result.graph_context.age_fallback_reason
                                    ? ` · fallback ${result.graph_context.age_fallback_reason}`
                                    : ""}
                                </div>
                              )}
                              {result.graph_context.query_override_active && (
                                <div
                                  className={`mt-3 text-xs ${result.retrieval_mode === "age_graph" ? "text-emerald-800" : "text-blue-800"}`}
                                >
                                  Query tuning
                                  {result.graph_context.retrieval_strength
                                    ? ` · ${result.graph_context.retrieval_strength}`
                                    : ""}
                                  {typeof result.graph_context
                                    .minimum_relationship_weight === "number"
                                    ? ` · min weight ${result.graph_context.minimum_relationship_weight}`
                                    : ""}
                                  {result.graph_context.age_configured_seed_mode
                                    ? ` · seed ${ageSeedModeLabel(result.graph_context.age_configured_seed_mode)}`
                                    : ""}
                                  {typeof result.graph_context
                                    .age_configured_hops === "number"
                                    ? ` · configured ${result.graph_context.age_configured_hops} hops`
                                    : ""}
                                  {typeof result.graph_context
                                    .age_candidate_pool_size === "number"
                                    ? ` · pool ${result.graph_context.age_candidate_pool_size}`
                                    : ""}
                                  {typeof result.graph_context
                                    .age_dense_rerank_weight === "number"
                                    ? ` · dense x${Number(result.graph_context.age_dense_rerank_weight).toFixed(2)}`
                                    : ""}
                                  {result.graph_context.strict_age_retrieval
                                    ? " · strict AGE"
                                    : ""}
                                </div>
                              )}
                              {result.graph_context.fallback_retrieval_mode && (
                                <div
                                  className={`mt-3 text-xs ${result.retrieval_mode === "age_graph" ? "text-emerald-800" : "text-blue-800"}`}
                                >
                                  Served via{" "}
                                  {retrievalModeLabel(
                                    result.graph_context
                                      .fallback_retrieval_mode,
                                  )}{" "}
                                  fallback.
                                </div>
                              )}
                              {result.graph_context.strict_age_retrieval &&
                                result.graph_context.age_fallback_reason &&
                                !result.graph_context
                                  .fallback_retrieval_mode && (
                                  <div
                                    className={`mt-3 text-xs font-medium ${result.retrieval_mode === "age_graph" ? "text-emerald-800" : "text-blue-800"}`}
                                  >
                                    Strict AGE mode prevented fallback to hybrid
                                    or dense retrieval.
                                  </div>
                                )}
                            </div>
                          )}
                          <div>
                            <div className="text-xs font-semibold uppercase tracking-[0.14em] text-slate-500">
                              Citations
                            </div>
                            <div className="mt-3 flex flex-wrap gap-2">
                              {result.citations.map((citation) => (
                                <Link
                                  key={citation.chunk_id}
                                  to={citation.object_store_path}
                                  className="inline-flex items-center gap-2 rounded-full border border-slate-200 bg-white px-3 py-1.5 text-xs font-semibold text-slate-600 hover:border-caliber-200 hover:text-caliber-purple"
                                >
                                  <Eye className="h-3.5 w-3.5" />
                                  {citation.label}
                                </Link>
                              ))}
                            </div>
                          </div>
                          <div className="space-y-3">
                            <div className="text-xs font-semibold uppercase tracking-[0.14em] text-slate-500">
                              Retrieved chunks
                            </div>
                            {result.retrieved_chunks.map((chunk) => (
                              <div
                                key={chunk.chunk_id}
                                className="rounded-2xl border border-slate-200/70 bg-slate-50/70 p-4"
                              >
                                <div className="flex items-center justify-between gap-3 text-xs text-slate-400">
                                  <Link
                                    to={chunk.object_store_path}
                                    className="font-semibold text-slate-600 hover:text-caliber-purple"
                                  >
                                    {chunk.source_name}
                                  </Link>
                                  <span>score {chunk.score.toFixed(3)}</span>
                                </div>
                                <div className="mt-2 flex flex-wrap items-center gap-2 text-[11px] text-slate-400">
                                  <span>
                                    dense{" "}
                                    {Number(
                                      chunk.score_breakdown.dense ?? 0,
                                    ).toFixed(3)}
                                  </span>
                                  {Number(
                                    chunk.score_breakdown.dense_rerank ?? 0,
                                  ) > 0 && (
                                    <span>
                                      dense-rerank{" "}
                                      {Number(
                                        chunk.score_breakdown.dense_rerank ?? 0,
                                      ).toFixed(3)}
                                    </span>
                                  )}
                                  {Number(
                                    chunk.score_breakdown.graph_boost ?? 0,
                                  ) > 0 && (
                                    <span>
                                      graph +
                                      {Number(
                                        chunk.score_breakdown.graph_boost ?? 0,
                                      ).toFixed(3)}
                                    </span>
                                  )}
                                  {Number(
                                    chunk.score_breakdown.age_graph ?? 0,
                                  ) > 0 && (
                                    <span>
                                      age{" "}
                                      {Number(
                                        chunk.score_breakdown.age_graph ?? 0,
                                      ).toFixed(2)}
                                    </span>
                                  )}
                                  {Number(
                                    chunk.score_breakdown.age_direct ?? 0,
                                  ) > 0 && (
                                    <span>
                                      direct{" "}
                                      {Number(
                                        chunk.score_breakdown.age_direct ?? 0,
                                      ).toFixed(2)}
                                    </span>
                                  )}
                                  {Number(
                                    chunk.score_breakdown.age_one_hop ?? 0,
                                  ) > 0 && (
                                    <span>
                                      hop1{" "}
                                      {Number(
                                        chunk.score_breakdown.age_one_hop ?? 0,
                                      ).toFixed(2)}
                                    </span>
                                  )}
                                  {Number(
                                    chunk.score_breakdown.age_two_hop ?? 0,
                                  ) > 0 && (
                                    <span>
                                      hop2{" "}
                                      {Number(
                                        chunk.score_breakdown.age_two_hop ?? 0,
                                      ).toFixed(2)}
                                    </span>
                                  )}
                                  {chunk.matched_entity_labels.map((label) => (
                                    <span
                                      key={`${chunk.chunk_id}-${label}`}
                                      className="rounded-full border border-slate-200 bg-white px-2 py-0.5 font-semibold text-slate-500"
                                    >
                                      {label}
                                    </span>
                                  ))}
                                </div>
                                <pre className="mt-3 whitespace-pre-wrap text-sm leading-6 text-slate-700">
                                  {chunk.content}
                                </pre>
                              </div>
                            ))}
                          </div>
                        </div>
                      </div>
                    ))}
                  </div>
                </div>
              ))}
              {chatTurns.length === 0 && (
                <EmptyPanel
                  title="No questions yet"
                  body="Ask the selected versions a question to inspect side-by-side retrieval and grounded answers."
                />
              )}
            </div>
          </div>
        ) : (
          <KnowledgeGuidancePanel
            title="No knowledge base yet"
            body="Create a versioned corpus first, then compare dense, GraphRAG hybrid, and Apache AGE answers in the Playground."
            focus="playground"
            ageEnabled={ageEnabled}
            ageUnavailableReason={ageUnavailableReason}
            onOpenBuild={() => setActiveTab("build")}
          />
        ))}

      {showCalibrateStage &&
        (selectedKnowledgeBase ? (
          <KnowledgeCalibrateTab
            knowledgeBaseId={selectedKnowledgeBase.knowledge_base_id}
            versions={versions}
            activeVersionId={selectedKnowledgeBase.active_version_id}
          />
        ) : (
          <EmptyPanel
            title="Select a knowledge base"
            body="Choose a knowledge base from the library to calibrate its retrieval quality against a test set."
          />
        ))}

      {showBuildStage &&
        (selectedKnowledgeBase ? (
          <div className="grid gap-5 xl:grid-cols-[minmax(0,0.92fr)_minmax(340px,1.08fr)]">
            <div className="card overflow-hidden">
              <div className="border-b border-slate-200/70 px-5 py-4">
                <div className="text-[11px] font-bold uppercase tracking-[0.16em] text-slate-500">
                  Build Runs
                </div>
                <h2 className="mt-2 text-lg font-semibold text-slate-900">
                  Pipeline executions
                </h2>
              </div>
              <div className="divide-y divide-slate-100">
                {runs.map((run) => (
                  <button
                    key={run.knowledge_base_run_id}
                    type="button"
                    onClick={() => setSelectedRunId(run.knowledge_base_run_id)}
                    className={`flex w-full items-start justify-between gap-3 px-5 py-4 text-left transition ${selectedRunId === run.knowledge_base_run_id ? "bg-caliber-50/50" : "hover:bg-slate-50/60"}`}
                  >
                    <div>
                      <div className="font-semibold text-slate-800">
                        {run.knowledge_base_run_id}
                      </div>
                      <div className="mt-1 text-xs text-slate-400">
                        {run.status === "queued"
                          ? `Queued ${formatDate(run.queued_at ?? run.created_at)}`
                          : run.status === "running"
                            ? `Started ${formatDate(run.started_at ?? run.created_at)}`
                            : `Completed ${formatDate(run.completed_at)}`}
                      </div>
                    </div>
                    <div className="text-right">
                      <div
                        className={`rounded-full px-2.5 py-1 text-[11px] font-semibold ${run.status === "completed" ? "bg-emerald-50 text-emerald-700" : run.status === "failed" ? "bg-red-50 text-red-700" : "bg-amber-50 text-amber-700"}`}
                      >
                        {run.status}
                      </div>
                      <div className="mt-2 text-xs text-slate-400">
                        {run.log_line_count} events
                      </div>
                    </div>
                  </button>
                ))}
                {runs.length === 0 && (
                  <div className="px-5 py-8 text-sm text-slate-400">
                    No runs yet.
                  </div>
                )}
              </div>
            </div>

            <div className="card overflow-hidden">
              <div className="border-b border-slate-200/70 px-5 py-4">
                <div className="flex items-center justify-between gap-3">
                  <div>
                    <div className="text-[11px] font-bold uppercase tracking-[0.16em] text-slate-500">
                      Pipeline status
                    </div>
                    <h2 className="mt-2 text-lg font-semibold text-slate-900">
                      Build progress
                    </h2>
                  </div>
                  {selectedRunId && (
                    <button
                      type="button"
                      onClick={() =>
                        void invalidate([
                          "knowledge-bases",
                          selectedRunId,
                          "events",
                        ])
                      }
                      className="btn-ghost !px-2.5 !py-1.5"
                    >
                      <RefreshCw className="h-4 w-4" /> Refresh
                    </button>
                  )}
                </div>
              </div>
              <div className="max-h-[56rem] overflow-y-auto p-5">
                {selectedRun ? (
                  <>
                    <KbBuildStepper
                      run={selectedRun}
                      events={runEventsQuery.data ?? []}
                    />
                    {isRunLive(selectedRun) && (
                      <div className="mt-4 rounded-2xl border border-amber-200 bg-amber-50 px-4 py-3 text-sm text-amber-700">
                        This run is still active. The pipeline above advances
                        automatically while the worker processes the build.
                      </div>
                    )}
                    {selectedRun.error_summary && (
                      <div className="mt-4 rounded-2xl border border-red-200 bg-red-50 px-4 py-3 text-sm text-red-700">
                        {selectedRun.error_summary}
                      </div>
                    )}
                    <div className="mt-4">
                      <Disclosure
                        summary="View log"
                        hint={`${(runEventsQuery.data ?? []).length} pipeline events`}
                        testId="kb-run-log"
                      >
                        <div className="space-y-3">
                          {(runEventsQuery.data ?? []).map((event) => (
                            <div
                              key={`${event.event_id}-${event.sequence}`}
                              className="rounded-2xl border border-slate-200/70 bg-slate-50/60 p-4"
                            >
                              <div className="flex items-center justify-between gap-3 text-xs text-slate-400">
                                <span className="font-semibold text-slate-700">
                                  #{event.sequence} · {event.event_type}
                                </span>
                                <span>{formatDate(event.created_at)}</span>
                              </div>
                              {event.payload && (
                                <pre className="mt-3 overflow-x-auto rounded-xl bg-slate-900 px-3 py-3 text-xs leading-5 text-slate-100">
                                  {JSON.stringify(event.payload, null, 2)}
                                </pre>
                              )}
                            </div>
                          ))}
                          {(runEventsQuery.data ?? []).length === 0 && (
                            <div className="rounded-2xl border border-dashed border-slate-200 bg-slate-50/60 px-4 py-8 text-center text-sm text-slate-400">
                              No pipeline events recorded for this run yet.
                            </div>
                          )}
                        </div>
                      </Disclosure>
                    </div>
                  </>
                ) : (
                  <div className="rounded-2xl border border-dashed border-slate-200 bg-slate-50/60 px-4 py-8 text-center text-sm text-slate-400">
                    Choose a run to inspect its pipeline progress.
                  </div>
                )}
              </div>
            </div>
          </div>
        ) : (
          <EmptyPanel
            title="Select a knowledge base"
            body="Choose a knowledge base from the library before inspecting pipeline runs."
          />
        ))}
    </div>
  );
}

function KnowledgeGraphCanvas({
  entities,
  relationships,
  isLoading,
  searchQuery,
  sourceMode,
  sourceSummary,
  graphName,
  ageSeedStrategy,
  selectedEntityId,
  onSelectEntity,
}: {
  entities: KnowledgeGraphEntityView[];
  relationships: KnowledgeGraphRelationshipView[];
  isLoading?: boolean;
  searchQuery: string;
  sourceMode?: KnowledgeGraphExploreSource;
  sourceSummary?: string;
  graphName?: string | null;
  ageSeedStrategy?: string | null;
  selectedEntityId?: string | null;
  onSelectEntity?: (entityId: string) => void;
}): JSX.Element {
  const width = 880;
  const height = 430;
  const centerX = width / 2;
  const centerY = height / 2 + 8;
  const hasNodes = entities.length > 0;
  const palette = [
    "#4f46e5",
    "#0f766e",
    "#0369a1",
    "#b45309",
    "#9333ea",
    "#be185d",
    "#1d4ed8",
    "#059669",
    "#dc2626",
    "#475569",
  ];
  const query = searchQuery.trim().toLowerCase();
  const sourceLabel =
    sourceSummary ??
    (sourceMode === "age"
      ? "Apache AGE neighborhood"
      : "Version graph artifacts");
  const connectionCountByEntityId = new Map<string, number>();
  let ranked: KnowledgeGraphEntityView[] = [];
  let visibleRelationships: KnowledgeGraphRelationshipView[] = [];
  for (const relationship of relationships) {
    connectionCountByEntityId.set(
      relationship.source_entity_id,
      (connectionCountByEntityId.get(relationship.source_entity_id) ?? 0) + 1,
    );
    connectionCountByEntityId.set(
      relationship.target_entity_id,
      (connectionCountByEntityId.get(relationship.target_entity_id) ?? 0) + 1,
    );
  }
  if (hasNodes) {
    const sortedEntities = [...entities].sort((left, right) => {
      const leftDistance =
        typeof left.distance === "number"
          ? left.distance
          : Number.POSITIVE_INFINITY;
      const rightDistance =
        typeof right.distance === "number"
          ? right.distance
          : Number.POSITIVE_INFINITY;
      const leftSeed = left.highlighted || leftDistance === 0 ? 1 : 0;
      const rightSeed = right.highlighted || rightDistance === 0 ? 1 : 0;
      return (
        rightSeed - leftSeed ||
        leftDistance - rightDistance ||
        (connectionCountByEntityId.get(right.knowledge_base_entity_id) ?? 0) -
          (connectionCountByEntityId.get(left.knowledge_base_entity_id) ?? 0) ||
        right.mention_count - left.mention_count ||
        left.label.localeCompare(right.label)
      );
    });
    ranked = (() => {
      const top = sortedEntities.slice(0, 14);
      if (
        !selectedEntityId ||
        top.some(
          (entity) => entity.knowledge_base_entity_id === selectedEntityId,
        )
      ) {
        return top;
      }
      const selectedEntity = sortedEntities.find(
        (entity) => entity.knowledge_base_entity_id === selectedEntityId,
      );
      if (!selectedEntity) return top;
      return [...top.slice(0, 13), selectedEntity];
    })();
    const rankedIdSet = new Set(
      ranked.map((entity) => entity.knowledge_base_entity_id),
    );
    visibleRelationships = relationships
      .filter(
        (relationship) =>
          rankedIdSet.has(relationship.source_entity_id) &&
          rankedIdSet.has(relationship.target_entity_id),
      )
      .sort((left, right) => right.weight - left.weight);
  }
  const maxMentions = Math.max(
    ...ranked.map((entity) => entity.mention_count),
    1,
  );
  const maxConnections = Math.max(
    ...ranked.map(
      (entity) =>
        connectionCountByEntityId.get(entity.knowledge_base_entity_id) ?? 0,
    ),
    1,
  );
  const positionMap = new Map<
    string,
    {
      entity: KnowledgeGraphEntityView;
      x: number;
      y: number;
      radius: number;
      color: string;
      highlighted: boolean;
      ring: "seed" | "inner" | "middle" | "outer";
      connectionCount: number;
    }
  >();
  const decorateNode = (
    entity: KnowledgeGraphEntityView,
    index: number,
    ring: "seed" | "inner" | "middle" | "outer",
    x: number,
    y: number,
  ): void => {
    const highlighted =
      entity.highlighted ||
      (query.length > 0 &&
        [
          entity.label,
          entity.entity_type,
          ...entity.aliases,
          ...entity.source_keys,
        ].some((field) => field.toLowerCase().includes(query)));
    const connectionCount =
      connectionCountByEntityId.get(entity.knowledge_base_entity_id) ?? 0;
    const radius = Math.min(
      38,
      18 +
        Math.round((entity.mention_count / maxMentions) * 12) +
        Math.round((connectionCount / maxConnections) * 4),
    );
    positionMap.set(entity.knowledge_base_entity_id, {
      entity,
      x,
      y,
      radius,
      color: palette[index % palette.length]!,
      highlighted,
      ring,
      connectionCount,
    });
  };
  if (hasNodes) {
    const seedEntities = ranked.filter(
      (entity) =>
        entity.highlighted ||
        (typeof entity.distance === "number" && entity.distance === 0),
    );
    const anchor = seedEntities[0] ?? ranked[0]!;
    decorateNode(anchor, 0, "seed", centerX, centerY);

    const remaining = ranked.filter(
      (entity) =>
        entity.knowledge_base_entity_id !== anchor.knowledge_base_entity_id,
    );
    const innerRing = [
      ...seedEntities.filter(
        (entity) =>
          entity.knowledge_base_entity_id !== anchor.knowledge_base_entity_id,
      ),
      ...remaining.filter(
        (entity) => typeof entity.distance === "number" && entity.distance === 1,
      ),
    ];
    const middleRing = remaining.filter(
      (entity) =>
        !innerRing.some(
          (item) =>
            item.knowledge_base_entity_id === entity.knowledge_base_entity_id,
        ) &&
        typeof entity.distance === "number" &&
        entity.distance === 2,
    );
    const outerRing = remaining.filter(
      (entity) =>
        !innerRing.some(
          (item) =>
            item.knowledge_base_entity_id === entity.knowledge_base_entity_id,
        ) &&
        !middleRing.some(
          (item) =>
            item.knowledge_base_entity_id === entity.knowledge_base_entity_id,
        ),
    );
    const placeRing = (
      items: KnowledgeGraphEntityView[],
      radiusX: number,
      radiusY: number,
      ring: "seed" | "inner" | "middle" | "outer",
      startAngle: number,
      paletteOffset: number,
    ): void => {
      if (items.length === 0) return;
      items.forEach((entity, index) => {
        const angle =
          startAngle + (index / Math.max(items.length, 1)) * (Math.PI * 2);
        decorateNode(
          entity,
          paletteOffset + index,
          ring,
          centerX + Math.cos(angle) * radiusX,
          centerY + Math.sin(angle) * radiusY,
        );
      });
    };
    placeRing(innerRing, 188, 92, "inner", -Math.PI / 2, 1);
    placeRing(middleRing, 292, 138, "middle", -Math.PI / 2 + 0.16, 4);
    placeRing(outerRing, 362, 186, "outer", -Math.PI / 2 + 0.32, 7);
  }

  const maxWeight = Math.max(
    ...visibleRelationships.map((item) => item.weight),
    1,
  );
  const selectedEntity =
    ranked.find(
      (entity) => entity.knowledge_base_entity_id === selectedEntityId,
    ) ?? null;
  const trailSummaries = (
    selectedEntity
      ? visibleRelationships.filter(
          (relationship) =>
            relationship.source_entity_id ===
              selectedEntity.knowledge_base_entity_id ||
            relationship.target_entity_id ===
              selectedEntity.knowledge_base_entity_id,
        )
      : visibleRelationships
  )
    .slice(0, 5)
    .map((relationship) => {
      const isFocusedSource =
        selectedEntity?.knowledge_base_entity_id ===
        relationship.source_entity_id;
      const neighborId = selectedEntity
        ? isFocusedSource
          ? relationship.target_entity_id
          : relationship.source_entity_id
        : relationship.target_entity_id;
      const neighbor = positionMap.get(neighborId)?.entity ?? null;
      return { relationship, neighbor };
    });

  return (
    <div
      data-testid="knowledge-graph-canvas"
      className="rounded-2xl border border-slate-200/70 bg-white px-4 py-4 shadow-card dark:border-slate-700/70 dark:bg-slate-950/90"
    >
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div>
          <div className="text-xs font-semibold uppercase tracking-[0.14em] text-slate-500 dark:text-slate-400">
            {sourceMode === "age"
              ? "AGE graph neighborhood"
              : "Version graph explorer"}
          </div>
          <div className="mt-1 text-sm font-semibold text-slate-900 dark:text-slate-100">
            {sourceLabel}
          </div>
          <div className="mt-1 text-xs leading-relaxed text-slate-400 dark:text-slate-500">
            {sourceMode === "age"
              ? `Inspect the live ${graphName ?? "knowledge graph"} neighborhood that AGE is returning for this version.`
              : "Inspect the version-scoped graph artifacts CALIBER stored alongside the chunks and embeddings for this build."}
          </div>
        </div>
        <div
          data-testid="knowledge-graph-canvas-summary"
          className="flex flex-wrap gap-2 text-[11px]"
        >
          <span
            className={`rounded-full border px-3 py-1 font-semibold ${
              sourceMode === "age"
                ? "border-emerald-200 bg-emerald-50 text-emerald-700 dark:border-emerald-500/30 dark:bg-emerald-500/10 dark:text-emerald-200"
                : "border-slate-200 bg-slate-50 text-slate-600 dark:border-slate-700/70 dark:bg-slate-900/80 dark:text-slate-200"
            }`}
          >
            {sourceMode === "age" ? "AGE-backed" : "Version-local"}
          </span>
          {graphName && sourceMode === "age" && (
            <span className="rounded-full border border-slate-200 bg-white px-3 py-1 font-mono font-semibold text-slate-600 dark:border-slate-700/70 dark:bg-slate-950 dark:text-slate-200">
              {graphName}
            </span>
          )}
          <span className="rounded-full border border-slate-200 bg-white px-3 py-1 font-semibold text-slate-600 dark:border-slate-700/70 dark:bg-slate-950 dark:text-slate-200">
            {ranked.length} visible nodes
          </span>
          <span className="rounded-full border border-slate-200 bg-white px-3 py-1 font-semibold text-slate-600 dark:border-slate-700/70 dark:bg-slate-950 dark:text-slate-200">
            {visibleRelationships.length} visible edges
          </span>
          {ageSeedStrategy && sourceMode === "age" && (
            <span className="rounded-full border border-emerald-200 bg-white px-3 py-1 font-semibold text-emerald-700 dark:border-emerald-500/30 dark:bg-slate-950 dark:text-emerald-200">
              {ageSeedStrategyLabel(ageSeedStrategy) ?? "AGE seeded"}
            </span>
          )}
        </div>
      </div>

      <div
        data-testid="knowledge-graph-canvas-legend"
        className="mt-4 flex flex-wrap gap-2 text-[11px]"
      >
        <span className="inline-flex items-center gap-2 rounded-full border border-slate-200 bg-slate-50 px-3 py-1 font-semibold text-slate-600 dark:border-slate-700/70 dark:bg-slate-900/80 dark:text-slate-200">
          <span className="h-2.5 w-2.5 rounded-full bg-indigo-600" />
          Seed match
        </span>
        <span className="inline-flex items-center gap-2 rounded-full border border-slate-200 bg-slate-50 px-3 py-1 font-semibold text-slate-600 dark:border-slate-700/70 dark:bg-slate-900/80 dark:text-slate-200">
          <span className="h-2.5 w-2.5 rounded-full bg-sky-600" />
          Expanded node
        </span>
        <span className="inline-flex items-center gap-2 rounded-full border border-slate-200 bg-slate-50 px-3 py-1 font-semibold text-slate-600 dark:border-slate-700/70 dark:bg-slate-900/80 dark:text-slate-200">
          <span className="h-2.5 w-2.5 rounded-full bg-emerald-600" />
          Selected trail
        </span>
      </div>

      <div className="mt-4 overflow-hidden rounded-[28px] border border-slate-200/70 bg-slate-50/60 shadow-inner dark:border-slate-700/70 dark:bg-slate-900/70">
        {hasNodes ? (
          <svg viewBox={`0 0 ${width} ${height}`} className="h-[400px] w-full">
            <rect
              x="0"
              y="0"
              width={width}
              height={height}
              rx="28"
              fill="url(#graph-canvas-bg)"
            />
            <defs>
              <linearGradient
                id="graph-canvas-bg"
                x1="0%"
                y1="0%"
                x2="100%"
                y2="100%"
              >
                <stop
                  offset="0%"
                  stopColor="currentColor"
                  className="text-slate-50 dark:text-slate-900"
                />
                <stop
                  offset="100%"
                  stopColor="currentColor"
                  className="text-indigo-50 dark:text-slate-800"
                />
              </linearGradient>
            </defs>

            <ellipse
              cx={centerX}
              cy={centerY}
              rx="112"
              ry="58"
              fill="none"
              stroke="#cbd5e1"
              strokeDasharray="6 10"
              strokeOpacity="0.45"
            />
            <ellipse
              cx={centerX}
              cy={centerY}
              rx="222"
              ry="108"
              fill="none"
              stroke="#cbd5e1"
              strokeDasharray="6 12"
              strokeOpacity="0.35"
            />
            <ellipse
              cx={centerX}
              cy={centerY}
              rx="322"
              ry="154"
              fill="none"
              stroke="#cbd5e1"
              strokeDasharray="6 14"
              strokeOpacity="0.28"
            />
            <text
              x="24"
              y="34"
              fontSize="11"
              fontWeight="700"
              className="fill-slate-500 dark:fill-slate-400"
            >
              Seed ring
            </text>
            <text
              x="24"
              y="54"
              fontSize="11"
              fontWeight="700"
              className="fill-slate-400 dark:fill-slate-500"
            >
              One-hop and two-hop expansion
            </text>

            {visibleRelationships.map((relationship, index) => {
              const source = positionMap.get(relationship.source_entity_id);
              const target = positionMap.get(relationship.target_entity_id);
              if (!source || !target) return null;
              const lineWidth = 1.4 + (relationship.weight / maxWeight) * 3.2;
              const connected = Boolean(
                selectedEntityId &&
                (relationship.source_entity_id === selectedEntityId ||
                  relationship.target_entity_id === selectedEntityId),
              );
              const midX = (source.x + target.x) / 2;
              const midY = (source.y + target.y) / 2;
              const dx = target.x - source.x;
              const dy = target.y - source.y;
              const length = Math.hypot(dx, dy) || 1;
              const labelOffset = connected ? 16 : 0;
              const labelX = midX + (-dy / length) * labelOffset;
              const labelY =
                midY +
                (dx / length) * labelOffset +
                (connected ? 0 : index % 2 === 0 ? -10 : 10);
              return (
                <g key={relationship.knowledge_base_relationship_id}>
                  <line
                    x1={source.x}
                    y1={source.y}
                    x2={target.x}
                    y2={target.y}
                    stroke={connected ? "#059669" : "#94a3b8"}
                    strokeOpacity={connected ? "0.86" : "0.42"}
                    strokeWidth={connected ? lineWidth + 1.2 : lineWidth}
                  />
                  {connected && (
                    <g>
                      <rect
                        x={labelX - 34}
                        y={labelY - 12}
                        width="68"
                        height="20"
                        rx="10"
                        fill="#ffffff"
                        fillOpacity="0.92"
                        stroke="#86efac"
                        strokeOpacity="0.85"
                      />
                      <text
                        x={labelX}
                        y={labelY + 3}
                        textAnchor="middle"
                        fontSize="10.5"
                        fontWeight="700"
                        fill="#047857"
                      >
                        {relationship.relationship_type}
                      </text>
                    </g>
                  )}
                  <title>
                    {relationship.source_entity_label} to{" "}
                    {relationship.target_entity_label} (
                    {relationship.relationship_type})
                  </title>
                </g>
              );
            })}

            {ranked.map((entity) => {
              const node = positionMap.get(entity.knowledge_base_entity_id);
              if (!node) return null;
              const shortLabel =
                entity.label.length > 18
                  ? `${entity.label.slice(0, 18)}…`
                  : entity.label;
              const selected =
                selectedEntityId === entity.knowledge_base_entity_id;
              return (
                <g
                  key={entity.knowledge_base_entity_id}
                  role="button"
                  tabIndex={0}
                  aria-label={`Select graph entity ${entity.label}`}
                  data-testid={`graph-node-${entity.knowledge_base_entity_id}`}
                  onClick={() =>
                    onSelectEntity?.(entity.knowledge_base_entity_id)
                  }
                  onKeyDown={(event) => {
                    if (event.key === "Enter" || event.key === " ") {
                      event.preventDefault();
                      onSelectEntity?.(entity.knowledge_base_entity_id);
                    }
                  }}
                  className="cursor-pointer outline-none"
                >
                  {node.highlighted && (
                    <circle
                      cx={node.x}
                      cy={node.y}
                      r={node.radius + 8}
                      fill={node.color}
                      fillOpacity="0.12"
                    />
                  )}
                  <circle
                    cx={node.x}
                    cy={node.y}
                    r={node.radius}
                    fill={node.color}
                    fillOpacity={
                      selected ? "1" : node.highlighted ? "0.95" : "0.82"
                    }
                    stroke={
                      selected
                        ? "#0f172a"
                        : node.highlighted
                          ? "#111827"
                          : "#ffffff"
                    }
                    strokeWidth={selected ? "4" : node.highlighted ? "3" : "2"}
                  />
                  <text
                    x={node.x}
                    y={node.y + 4}
                    textAnchor="middle"
                    fontSize={node.radius > 24 ? "12" : "11"}
                    fontWeight="700"
                    fill="#ffffff"
                  >
                    {entity.mention_count}
                  </text>
                  <text
                    x={node.x}
                    y={node.y + node.radius + 18}
                    textAnchor="middle"
                    fontSize="12"
                    fontWeight="600"
                    className="fill-slate-700 dark:fill-slate-200"
                  >
                    {shortLabel}
                  </text>
                  <title>
                    {entity.label} • {entity.entity_type} • {entity.mention_count}{" "}
                    mentions
                  </title>
                </g>
              );
            })}
          </svg>
        ) : (
          <div className="grid h-[400px] place-items-center px-6 text-center">
            <div>
              <div className="text-sm font-semibold text-slate-700 dark:text-slate-200">
                {isLoading
                  ? "Loading the graph neighborhood for this version…"
                  : "No graph nodes are available for this version yet."}
              </div>
              <div className="mt-2 text-xs leading-relaxed text-slate-400 dark:text-slate-500">
                {sourceMode === "age"
                  ? "CALIBER is preparing the AGE-backed neighborhood view and will populate the canvas as soon as the current graph query completes."
                  : "Once entities and relationships are available for this version, they will appear here as a navigable graph map."}
              </div>
            </div>
          </div>
        )}
      </div>

      <div
        data-testid="knowledge-graph-canvas-trails"
        className="mt-4 rounded-2xl border border-slate-200/70 bg-slate-50/70 px-4 py-3 dark:border-slate-700/70 dark:bg-slate-900/70"
      >
        <div className="text-[11px] font-bold uppercase tracking-[0.14em] text-slate-500 dark:text-slate-400">
          {selectedEntity
            ? `${selectedEntity.label} relationship trails`
            : "Relationship trails in view"}
        </div>
        <div className="mt-2 flex flex-wrap gap-2">
          {trailSummaries.length > 0 ? (
            trailSummaries.map(({ relationship, neighbor }) => (
              <button
                key={`trail-${relationship.knowledge_base_relationship_id}`}
                type="button"
                onClick={() =>
                  neighbor
                    ? onSelectEntity?.(neighbor.knowledge_base_entity_id)
                    : undefined
                }
                className="rounded-full border border-slate-200 bg-white px-3 py-1.5 text-left text-[11px] font-semibold text-slate-600 transition hover:border-caliber-200 hover:text-caliber-purple dark:border-slate-700/70 dark:bg-slate-950 dark:text-slate-200 dark:hover:border-violet-500/40 dark:hover:text-violet-100"
              >
                {selectedEntity && neighbor ? (
                  <>
                    <span>{neighbor.label}</span>
                    <span className="ml-2 text-slate-400 dark:text-slate-500">
                      {relationship.relationship_type} · w
                      {relationship.weight.toFixed(1)}
                    </span>
                  </>
                ) : (
                  <>
                    <span>
                      {relationship.source_entity_label} →{" "}
                      {relationship.target_entity_label}
                    </span>
                    <span className="ml-2 text-slate-400 dark:text-slate-500">
                      {relationship.relationship_type} · w
                      {relationship.weight.toFixed(1)}
                    </span>
                  </>
                )}
              </button>
            ))
          ) : (
            <span className="text-xs text-slate-400 dark:text-slate-500">
              No visible trails match the current node and graph filters.
            </span>
          )}
        </div>
      </div>

      <div className="mt-3 flex flex-wrap items-center gap-2 text-[11px]">
        {selectedEntityId && (
          <span className="rounded-full border border-caliber-200 bg-caliber-50 px-3 py-1 font-semibold text-caliber-700 dark:border-violet-500/40 dark:bg-violet-500/15 dark:text-violet-100">
            One node selected
          </span>
        )}
        <span className="rounded-full border border-slate-200 bg-slate-50 px-3 py-1 font-semibold text-slate-500 dark:border-slate-700/70 dark:bg-slate-900/80 dark:text-slate-300">
          Click a node to inspect its sources and relationship trails
        </span>
      </div>
      <div className="mt-3 text-xs text-slate-400 dark:text-slate-500">
        The explorer surfaces the highest-signal graph neighborhood for the
        current filter. Use the search bar to focus a concept, owner, source
        key, or relationship trail before jumping into AGE-backed retrieval or
        the raw graph artifacts.
      </div>
    </div>
  );
}

function KnowledgeQueryCompactResultCard({
  result,
}: {
  result: KnowledgeQueryVersionResult;
}): JSX.Element {
  return (
    <div
      data-testid="graph-probe-result"
      className="rounded-2xl border border-slate-200/70 bg-slate-50/70 p-4 dark:border-slate-700/70 dark:bg-slate-900/70"
    >
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div>
          <div className="text-sm font-semibold text-slate-900 dark:text-slate-100">
            {result.knowledge_base_name} · v{result.version_number}
          </div>
          <div className="mt-1 flex flex-wrap items-center gap-2 text-xs text-slate-400 dark:text-slate-500">
            <span>{result.chunking_strategy}</span>
            <span
              className={`rounded-full px-2 py-0.5 font-semibold ${graphModeTone(result.retrieval_mode)}`}
            >
              {retrievalModeLabel(result.retrieval_mode)}
            </span>
            {result.retrieval_mode === "age_graph" &&
              !result.graph_context.fallback_retrieval_mode && (
                <span className="rounded-full border border-emerald-200 bg-emerald-50 px-2 py-0.5 font-semibold text-emerald-700 dark:border-emerald-500/30 dark:bg-emerald-500/10 dark:text-emerald-200">
                  AGE-backed
                </span>
              )}
            {result.retrieval_mode === "age_graph" &&
              result.graph_context.fallback_retrieval_mode && (
                <span className="rounded-full border border-amber-200 bg-amber-50 px-2 py-0.5 font-semibold text-amber-700 dark:border-amber-500/30 dark:bg-amber-500/10 dark:text-amber-200">
                  Fell back to{" "}
                  {retrievalModeLabel(
                    result.graph_context.fallback_retrieval_mode,
                  )}
                </span>
              )}
          </div>
        </div>
        <div className="rounded-full bg-white px-3 py-1 text-[11px] font-semibold text-slate-500 shadow-sm dark:bg-slate-950 dark:text-slate-300">
          {result.timing_ms.total ?? 0} ms
        </div>
      </div>

      <div className="mt-4 rounded-2xl border border-slate-200/70 bg-white/90 px-4 py-3 text-sm leading-6 text-slate-700 dark:border-slate-700/70 dark:bg-slate-950/80 dark:text-slate-100">
        {result.answer ?? result.answer_error ?? "No answer returned."}
      </div>

      {result.retrieval_mode !== "dense" && (
        <div
          className={`mt-4 rounded-2xl p-4 ${
            result.retrieval_mode === "age_graph"
              ? "border border-emerald-200 bg-emerald-50/70 dark:border-emerald-500/30 dark:bg-emerald-500/10"
              : "border border-blue-200 bg-blue-50/70 dark:border-blue-500/30 dark:bg-blue-500/10"
          }`}
        >
          <div className="flex flex-wrap items-center justify-between gap-3">
            <div
              className={`text-xs font-semibold uppercase tracking-[0.14em] ${
                result.retrieval_mode === "age_graph"
                  ? "text-emerald-700 dark:text-emerald-200"
                  : "text-blue-700 dark:text-blue-200"
              }`}
            >
              {result.retrieval_mode === "age_graph"
                ? "Apache AGE context"
                : "Graph context"}
            </div>
            <span
              className={`rounded-full bg-white px-2.5 py-1 text-[11px] font-semibold shadow-sm dark:bg-slate-950 ${
                result.retrieval_mode === "age_graph"
                  ? "text-emerald-700 dark:text-emerald-200"
                  : "text-blue-700 dark:text-blue-200"
              }`}
            >
              {result.graph_context.boosted_chunk_count ?? 0} boosted
            </span>
          </div>
          <div className="mt-3 flex flex-wrap gap-2">
            {(result.graph_context.matched_entities ?? []).map((label) => (
              <span
                key={`${result.knowledge_base_version_id}-${result.retrieval_mode}-${label}`}
                className={`rounded-full border bg-white px-3 py-1 text-[11px] font-semibold shadow-sm dark:bg-slate-950 ${
                  result.retrieval_mode === "age_graph"
                    ? "border-emerald-200 text-emerald-700 dark:border-emerald-500/30 dark:text-emerald-200"
                    : "border-blue-200 text-blue-700 dark:border-blue-500/30 dark:text-blue-200"
                }`}
              >
                {label}
              </span>
            ))}
            {(result.graph_context.matched_entities ?? []).length === 0 && (
              <span
                className={`text-xs ${
                  result.retrieval_mode === "age_graph"
                    ? "text-emerald-800 dark:text-emerald-200"
                    : "text-blue-700 dark:text-blue-200"
                }`}
              >
                No direct entity matches for this question.
              </span>
            )}
          </div>
          {(result.graph_context.expanded_entities ?? []).length > 0 && (
            <div
              className={`mt-3 text-xs ${
                result.retrieval_mode === "age_graph"
                  ? "text-emerald-800 dark:text-emerald-200"
                  : "text-blue-800 dark:text-blue-200"
              }`}
            >
              Expanded via graph:{" "}
              {(result.graph_context.expanded_entities ?? [])
                .slice(0, 6)
                .join(", ")}
            </div>
          )}
          {result.retrieval_mode === "age_graph" &&
            (result.graph_context.age_seed_strategy ||
              typeof result.graph_context.age_matched_chunk_count ===
                "number") && (
              <div className="mt-3 text-xs text-emerald-800 dark:text-emerald-200">
                {ageSeedStrategyLabel(result.graph_context.age_seed_strategy) ??
                  "AGE retrieval seeded"}
                {typeof result.graph_context.age_matched_chunk_count ===
                "number"
                  ? ` · matched ${result.graph_context.age_matched_chunk_count} chunks before rerank`
                  : ""}
              </div>
            )}
          {(result.graph_context.fallback_reason ||
            result.graph_context.age_graph_name) && (
            <div
              className={`mt-3 text-xs ${
                result.retrieval_mode === "age_graph"
                  ? "text-emerald-800 dark:text-emerald-200"
                  : "text-blue-800 dark:text-blue-200"
              }`}
            >
              {result.graph_context.age_graph_name
                ? `Graph ${result.graph_context.age_graph_name}`
                : "Graph metadata available"}
              {result.graph_context.age_configured_seed_mode
                ? ` · seed ${ageSeedModeLabel(result.graph_context.age_configured_seed_mode)}`
                : ""}
              {typeof result.graph_context.age_traversal_hops === "number"
                ? ` · ${result.graph_context.age_traversal_hops} hop traversal`
                : ""}
              {typeof result.graph_context.minimum_relationship_weight ===
              "number"
                ? ` · min weight ${result.graph_context.minimum_relationship_weight}`
                : ""}
              {result.graph_context.age_fallback_reason
                ? ` · fallback ${result.graph_context.age_fallback_reason}`
                : ""}
            </div>
          )}
          {result.graph_context.strict_age_retrieval &&
            result.graph_context.age_fallback_reason &&
            !result.graph_context.fallback_retrieval_mode && (
              <div className="mt-3 text-xs font-medium text-emerald-800 dark:text-emerald-200">
                Strict AGE mode prevented fallback to hybrid or dense retrieval.
              </div>
            )}
        </div>
      )}

      {result.citations.length > 0 && (
        <div className="mt-4">
          <div className="text-xs font-semibold uppercase tracking-[0.14em] text-slate-500 dark:text-slate-400">
            Citations
          </div>
          <div className="mt-3 flex flex-wrap gap-2">
            {result.citations.map((citation) => (
              <Link
                key={citation.chunk_id}
                to={citation.object_store_path}
                className="inline-flex items-center gap-2 rounded-full border border-slate-200 bg-white px-3 py-1.5 text-xs font-semibold text-slate-600 transition hover:border-caliber-200 hover:text-caliber-purple dark:border-slate-700/70 dark:bg-slate-950 dark:text-slate-200 dark:hover:border-violet-500/40 dark:hover:text-violet-100"
              >
                <Eye className="h-3.5 w-3.5" />
                {citation.label}
              </Link>
            ))}
          </div>
        </div>
      )}

      {result.retrieved_chunks.length > 0 && (
        <div className="mt-4 space-y-3">
          <div className="text-xs font-semibold uppercase tracking-[0.14em] text-slate-500 dark:text-slate-400">
            Retrieved chunks
          </div>
          {result.retrieved_chunks.slice(0, 3).map((chunk) => (
            <div
              key={chunk.chunk_id}
              className="rounded-2xl border border-slate-200/70 bg-white/90 p-4 dark:border-slate-700/70 dark:bg-slate-950/80"
            >
              <div className="flex flex-wrap items-center justify-between gap-3 text-xs text-slate-400 dark:text-slate-500">
                <Link
                  to={chunk.object_store_path}
                  className="font-semibold text-slate-600 hover:text-caliber-purple dark:text-slate-200 dark:hover:text-violet-100"
                >
                  {chunk.source_name}
                </Link>
                <span>score {chunk.score.toFixed(3)}</span>
              </div>
              <div className="mt-2 flex flex-wrap items-center gap-2 text-[11px] text-slate-400 dark:text-slate-500">
                <span>
                  dense {Number(chunk.score_breakdown.dense ?? 0).toFixed(3)}
                </span>
                {Number(chunk.score_breakdown.dense_rerank ?? 0) > 0 && (
                  <span>
                    dense-rerank{" "}
                    {Number(chunk.score_breakdown.dense_rerank ?? 0).toFixed(3)}
                  </span>
                )}
                {Number(chunk.score_breakdown.graph_boost ?? 0) > 0 && (
                  <span>
                    graph +
                    {Number(chunk.score_breakdown.graph_boost ?? 0).toFixed(3)}
                  </span>
                )}
                {Number(chunk.score_breakdown.age_graph ?? 0) > 0 && (
                  <span>
                    age{" "}
                    {Number(chunk.score_breakdown.age_graph ?? 0).toFixed(2)}
                  </span>
                )}
                {chunk.matched_entity_labels.map((label) => (
                  <span
                    key={`${chunk.chunk_id}-${label}`}
                    className="rounded-full border border-slate-200 bg-slate-50 px-2 py-0.5 font-semibold text-slate-500 dark:border-slate-700/70 dark:bg-slate-900/80 dark:text-slate-300"
                  >
                    {label}
                  </span>
                ))}
              </div>
              <pre className="mt-3 whitespace-pre-wrap text-sm leading-6 text-slate-700 dark:text-slate-100">
                {chunk.content}
              </pre>
            </div>
          ))}
        </div>
      )}
    </div>
  );
}

// ── R2 declutter helpers ─────────────────────────────────────────────────────
// A lightweight progressive-disclosure section. Children are only mounted while
// the section is open, so collapsed "Advanced" controls do not appear in the
// DOM — but because every value they edit lives in parent component state (not
// in the controls themselves), collapsing never changes the build payload.
function Disclosure({
  summary,
  hint,
  defaultOpen = false,
  testId,
  children,
}: {
  summary: string;
  hint?: string;
  defaultOpen?: boolean;
  testId?: string;
  children: ReactNode;
}): JSX.Element {
  const [open, setOpen] = useState(defaultOpen);
  return (
    <div
      className="rounded-2xl border border-slate-200/70 bg-slate-50/40 dark:border-slate-700/70 dark:bg-slate-900/40"
      data-testid={testId}
    >
      <button
        type="button"
        onClick={() => setOpen((value) => !value)}
        aria-expanded={open ? "true" : "false"}
        className="flex w-full items-center justify-between gap-3 px-4 py-3 text-left"
      >
        <span>
          <span className="text-sm font-semibold text-slate-800 dark:text-slate-100">
            {summary}
          </span>
          {hint && (
            <span className="mt-0.5 block text-xs text-slate-400 dark:text-slate-400">
              {hint}
            </span>
          )}
        </span>
        <ChevronDown
          className={`h-4 w-4 shrink-0 text-slate-400 transition-transform ${open ? "rotate-180" : ""}`}
        />
      </button>
      {open && (
        <div className="border-t border-slate-200/70 px-4 py-4 dark:border-slate-700/70">
          {children}
        </div>
      )}
    </div>
  );
}

// The compact build pipeline shown in place of the raw event list. Step states
// are derived from the run status + the real builder event vocabulary
// (build_started → source_* → graph_* → chunks_persisted → build_completed).
type KbStepState = "pending" | "active" | "done" | "failed";
const KB_BUILD_STEPS = [
  "Queued",
  "Extract",
  "Chunk",
  "Embed",
  "Graph",
  "Index",
  "Ready",
] as const;

function kbBuildStepStates(
  run: KnowledgeBaseRun | null,
  events: KnowledgeBaseRunEvent[],
): KbStepState[] {
  const seen = new Set(events.map((event) => event.event_type));
  const status = run?.status ?? null;
  const failed =
    status === "failed" || seen.has("build_failed") || seen.has("source_failed");
  // ``reached`` is the index of the currently-active step: every step before it
  // is done, every step after it is pending. It is advanced by the completion
  // events emitted by caliber.knowledge.service.execute_run.
  let reached = 0; // Queued (a run row exists → queued at minimum)
  if (seen.has("build_started") || seen.has("sources_expanded")) {
    reached = 1; // Queued done → Extract/Chunk/Embed in flight
  }
  if (seen.has("source_completed") || seen.has("source_skipped")) {
    reached = 4; // sources read+chunked+embedded → Graph
  }
  if (seen.has("graph_built") || seen.has("graph_skipped")) {
    reached = 5; // graph done → Index
  }
  if (seen.has("chunks_persisted")) {
    reached = 6; // persisted → Ready
  }
  const ready = status === "completed" || seen.has("build_completed");

  return KB_BUILD_STEPS.map((_, index): KbStepState => {
    if (ready) return "done";
    if (index < reached) return "done";
    if (index === reached) return failed ? "failed" : "active";
    return "pending";
  });
}

function KbBuildStepper({
  run,
  events,
}: {
  run: KnowledgeBaseRun | null;
  events: KnowledgeBaseRunEvent[];
}): JSX.Element {
  const states = kbBuildStepStates(run, events);
  return (
    <ol
      className="flex flex-wrap items-center gap-1.5"
      data-testid="kb-build-stepper"
    >
      {KB_BUILD_STEPS.map((label, index) => {
        const state = states[index];
        const tone =
          state === "done"
            ? "border-emerald-200 bg-emerald-50 text-emerald-700 dark:border-emerald-500/30 dark:bg-emerald-500/10 dark:text-emerald-200"
            : state === "active"
              ? "border-blue-200 bg-blue-50 text-blue-700 dark:border-blue-500/30 dark:bg-blue-500/10 dark:text-blue-200"
              : state === "failed"
                ? "border-red-200 bg-red-50 text-red-700 dark:border-red-500/30 dark:bg-red-500/10 dark:text-red-200"
                : "border-slate-200 bg-white text-slate-400 dark:border-slate-700/70 dark:bg-slate-950/60 dark:text-slate-500";
        return (
          <li key={label} className="flex items-center gap-1.5">
            <span
              className={`inline-flex items-center gap-1.5 rounded-full border px-2.5 py-1 text-[11px] font-semibold ${tone}`}
              data-testid={`kb-build-step-${label.toLowerCase()}`}
              data-state={state}
            >
              {state === "done" ? (
                <Check className="h-3 w-3" />
              ) : state === "active" ? (
                <Loader2 className="h-3 w-3 animate-spin" />
              ) : state === "failed" ? (
                <X className="h-3 w-3" />
              ) : null}
              {label}
            </span>
            {index < KB_BUILD_STEPS.length - 1 && (
              <span className="text-slate-300 dark:text-slate-600">→</span>
            )}
          </li>
        );
      })}
    </ol>
  );
}

function SummaryRow({
  label,
  value,
  mono = false,
}: {
  label: string;
  value: string;
  mono?: boolean;
}): JSX.Element {
  return (
    <div className="flex items-start justify-between gap-3 rounded-xl border border-slate-200/70 bg-slate-50/70 px-3 py-2.5 text-sm dark:border-slate-700/70 dark:bg-slate-900/70">
      <span className="text-slate-500 dark:text-slate-400">{label}</span>
      <span
        className={`text-right font-medium text-slate-800 dark:text-slate-100 ${mono ? "font-mono text-xs" : ""}`}
      >
        {value}
      </span>
    </div>
  );
}

function KnowledgeGuidancePanel({
  title,
  body,
  focus,
  ageEnabled,
  ageUnavailableReason,
  onOpenBuild,
}: {
  title: string;
  body: string;
  focus: "graph" | "playground";
  ageEnabled: boolean;
  ageUnavailableReason: string;
  onOpenBuild: () => void;
}): JSX.Element {
  const focusTitle =
    focus === "graph" ? "Graph explorer" : "Playground compare";
  const focusDescription =
    focus === "graph"
      ? "Inspect entities, relationships, local graph artifacts, and the AGE neighborhood from one version-aware workspace."
      : "Ask one question against one or two versions and compare dense, GraphRAG hybrid, and AGE-backed answers side by side.";
  const FocusIcon = focus === "graph" ? ArrowRightLeft : MessageSquareText;

  return (
    <div className="space-y-5" data-testid={`kb-onboarding-${focus}`}>
      <EmptyPanel title={title} body={body} />
      <div className="card p-5">
        <div className="flex flex-wrap items-start justify-between gap-4">
          <div className="max-w-3xl">
            <div className="text-[11px] font-bold uppercase tracking-[0.16em] text-slate-500 dark:text-slate-400">
              GraphRAG journey
            </div>
            <h2 className="mt-2 text-lg font-semibold text-slate-900 dark:text-slate-100">
              {focus === "graph"
                ? "Build once, then inspect local and AGE graph views"
                : "Build once, then compare dense, hybrid, and AGE retrieval"}
            </h2>
            <div className="mt-1 text-sm leading-relaxed text-slate-500 dark:text-slate-400">
              CALIBER stores chunks, embeddings, entities, relationships, and
              graph artifacts together so every version can be traced, compared,
              and promoted into Apache AGE when needed.
            </div>
          </div>
          <button
            type="button"
            onClick={onOpenBuild}
            className="btn-primary flex items-center gap-2"
          >
            <Sparkles className="h-4 w-4" />
            Open Build tab
          </button>
        </div>
        <div className="mt-5 grid gap-3 md:grid-cols-3">
          <div className="rounded-2xl border border-slate-200/70 bg-slate-50/70 p-4 dark:border-slate-700/70 dark:bg-slate-900/70">
            <div className="flex items-start gap-3">
              <span className="grid h-10 w-10 shrink-0 place-items-center rounded-xl bg-violet-50 text-caliber-purple dark:bg-violet-500/10 dark:text-violet-200">
                <Layers3 className="h-5 w-5" />
              </span>
              <div>
                <div className="text-xs font-semibold uppercase tracking-[0.14em] text-slate-500 dark:text-slate-400">
                  Versioned corpus
                </div>
                <div className="mt-1 text-sm font-semibold text-slate-900 dark:text-slate-100">
                  Chunking + embeddings + lineage
                </div>
                <div className="mt-1 text-xs leading-relaxed text-slate-500 dark:text-slate-400">
                  Select files or folders, choose the chunker and embedding
                  model, then save the full configuration as an immutable build
                  version.
                </div>
              </div>
            </div>
          </div>
          <div className="rounded-2xl border border-slate-200/70 bg-slate-50/70 p-4 dark:border-slate-700/70 dark:bg-slate-900/70">
            <div className="flex items-start gap-3">
              <span className="grid h-10 w-10 shrink-0 place-items-center rounded-xl bg-blue-50 text-blue-600 dark:bg-blue-500/10 dark:text-blue-200">
                <FocusIcon className="h-5 w-5" />
              </span>
              <div>
                <div className="text-xs font-semibold uppercase tracking-[0.14em] text-slate-500 dark:text-slate-400">
                  {focusTitle}
                </div>
                <div className="mt-1 text-sm font-semibold text-slate-900 dark:text-slate-100">
                  {focus === "graph"
                    ? "Inspect graph artifacts and neighborhood trails"
                    : "Compare grounded RAG answers"}
                </div>
                <div className="mt-1 text-xs leading-relaxed text-slate-500 dark:text-slate-400">
                  {focusDescription}
                </div>
              </div>
            </div>
          </div>
          <div className="rounded-2xl border border-slate-200/70 bg-slate-50/70 p-4 dark:border-slate-700/70 dark:bg-slate-900/70">
            <div className="flex items-start gap-3">
              <span
                className={`grid h-10 w-10 shrink-0 place-items-center rounded-xl ${
                  ageEnabled
                    ? "bg-emerald-50 text-emerald-600 dark:bg-emerald-500/10 dark:text-emerald-200"
                    : "bg-amber-50 text-amber-700 dark:bg-amber-500/10 dark:text-amber-200"
                }`}
              >
                <HardDrive className="h-5 w-5" />
              </span>
              <div>
                <div className="text-xs font-semibold uppercase tracking-[0.14em] text-slate-500 dark:text-slate-400">
                  Apache AGE
                </div>
                <div className="mt-1 text-sm font-semibold text-slate-900 dark:text-slate-100">
                  {ageEnabled
                    ? "Cypher-first retrieval is available in this stack"
                    : "AGE is not enabled in this stack"}
                </div>
                <div className="mt-1 text-xs leading-relaxed text-slate-500 dark:text-slate-400">
                  {ageEnabled
                    ? "Choose Object store + Apache AGE to sync graph artifacts automatically and unlock graph-native retrieval in the explorer and Playground."
                    : ageUnavailableReason}
                </div>
              </div>
            </div>
          </div>
        </div>
      </div>
    </div>
  );
}

function EmptyPanel({
  title,
  body,
}: {
  title: string;
  body: string;
}): JSX.Element {
  return (
    <div className="rounded-2xl border-2 border-dashed border-slate-200 bg-gradient-hero px-8 py-12 text-center dark:border-slate-700/70 dark:bg-slate-950">
      <div className="mx-auto mb-4 grid h-14 w-14 place-items-center rounded-2xl bg-white text-caliber-purple shadow-card dark:bg-slate-900">
        <Search className="h-6 w-6" />
      </div>
      <div className="text-sm font-semibold text-slate-600 dark:text-slate-100">
        {title}
      </div>
      <div className="mt-1 text-xs text-slate-400 dark:text-slate-400">
        {body}
      </div>
    </div>
  );
}

export default KnowledgeBases;
