import type { KnowledgeSourceSelection } from "@/api/knowledgeTypes";

export type KnowledgeBuildLaunchPreset =
  | "portable"
  | "balanced"
  | "age_native"
  | "age_strict";
export type KnowledgeBuildLaunchMode = "new" | "existing";

export interface KnowledgeBuildLaunchPayload {
  bucket: string;
  sources: KnowledgeSourceSelection[];
  buildMode: KnowledgeBuildLaunchMode;
  graphPreset: KnowledgeBuildLaunchPreset | null;
}

const BUILD_LAUNCH_PARAM_KEYS = ["tab", "build_mode", "bucket", "graph_preset", "source"] as const;

function isLaunchPreset(value: string | null): value is KnowledgeBuildLaunchPreset {
  return (
    value === "portable" ||
    value === "balanced" ||
    value === "age_native" ||
    value === "age_strict"
  );
}

function decodeSource(value: string): KnowledgeSourceSelection | null {
  const separatorIndex = value.indexOf(":");
  if (separatorIndex <= 0) return null;
  const kind = value.slice(0, separatorIndex);
  const path = value.slice(separatorIndex + 1);
  if ((kind !== "file" && kind !== "folder") || !path) return null;
  return { kind, path };
}

function encodeSource(source: KnowledgeSourceSelection): string {
  return `${source.kind}:${source.path}`;
}

export function buildKnowledgeBuildLaunchPath(payload: KnowledgeBuildLaunchPayload): string {
  const params = new URLSearchParams();
  params.set("tab", "build");
  params.set("build_mode", payload.buildMode);
  params.set("bucket", payload.bucket);
  if (payload.graphPreset) {
    params.set("graph_preset", payload.graphPreset);
  }
  for (const source of payload.sources) {
    params.append("source", encodeSource(source));
  }
  return `/knowledge-bases?${params.toString()}`;
}

export function parseKnowledgeBuildLaunchParams(
  params: URLSearchParams,
): KnowledgeBuildLaunchPayload | null {
  const tab = params.get("tab");
  const bucket = params.get("bucket")?.trim() ?? "";
  const rawSources = params.getAll("source");
  if (tab !== "build" || !bucket || rawSources.length === 0) return null;

  const sources = rawSources
    .map((value) => decodeSource(value))
    .filter((value): value is KnowledgeSourceSelection => value !== null);
  if (sources.length === 0) return null;

  const buildMode = params.get("build_mode") === "existing" ? "existing" : "new";
  const requestedPreset = params.get("graph_preset");
  const graphPreset = isLaunchPreset(requestedPreset)
    ? requestedPreset
    : null;

  return {
    bucket,
    sources,
    buildMode,
    graphPreset,
  };
}

export function stripKnowledgeBuildLaunchParams(params: URLSearchParams): URLSearchParams {
  const next = new URLSearchParams(params);
  for (const key of BUILD_LAUNCH_PARAM_KEYS) {
    next.delete(key);
  }
  return next;
}
