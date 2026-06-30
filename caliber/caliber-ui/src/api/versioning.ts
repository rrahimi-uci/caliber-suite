/**
 * UI-facing normalized version model.
 *
 * The backend exposes several different versioning idioms (MLflow registry
 * versions + aliases for prompts, immutable row chains for workflows/KBs, a
 * mutable counter for skills...). The <VersionPanel> never branches on artifact
 * type — instead each artifact provides an adapter that maps its native shape
 * onto this normalized model.
 *
 * Deliberately distinct from the assistant `ArtifactType` union (which carries
 * `mcp_server` and lacks `knowledge_base`/`test_set`). `mcp_server` is excluded
 * here — MCP servers are not versioned and use the edit-history surface instead.
 */
import type { PromptVersionInfo } from "./types";
import type { KnowledgeBaseVersion } from "./knowledgeTypes";
import type { ToolDefinition, WorkflowDeployment, WorkflowVersion } from "./workflowTypes";

export type VersionedArtifactType =
  | "prompt"
  | "workflow"
  | "knowledge_base"
  | "test_set"
  | "tool"
  | "skill";

export type VersionStatus =
  | "draft"
  | "published"
  | "active"
  | "deprecated"
  | "archived";

export type GateState = "pass" | "fail" | "none" | "pending" | "stale";

/**
 * `state` is the AUTHORITATIVE verdict (computed upstream from BOTH the
 * aggregate floor AND the per-dimension regression rule). The numeric fields
 * are display detail — the UI must never infer pass/fail from score-vs-threshold
 * alone (a candidate clearing the aggregate can still FAIL on a regression).
 */
export interface GateVerdict {
  state: GateState;
  score?: number | null;
  baseline_score?: number | null;
  min_aggregate_score?: number | null;
  worst_regression?: number | null;
  max_regression_delta?: number | null;
  eval_run_id?: string | null;
  evaluated_at?: string | null;
}

export interface PromptRollbackResult {
  name: string;
  alias: string;
  version: number;
  rolled_back_from: number;
}

export interface SkillVersionInfo {
  skill_version_id: string;
  skill_id: string;
  version_number: number;
  content: string;
  summary: string;
  created_by: string | null;
  created_at: string | null;
}

export interface VersionCapabilities {
  hasHistory: boolean;
  canPromote: boolean;
  canRollback: boolean;
  canDiff: boolean;
  canEditDraft: boolean;
  canDelete: boolean;
  gating: "advisory" | "none";
}

export interface ArtifactVersion {
  artifactType: VersionedArtifactType;
  artifactId: string;
  artifactName: string;
  versionKey: string; // opaque to the panel; adapters coerce to the client's type
  versionLabel: string;
  ordinal: number | null;
  status: VersionStatus;
  isLive: boolean;
  liveAliases: string[];
  wasLiveUntil?: string | null;
  author?: string | null;
  createdAt?: string | null;
  publishedAt?: string | null;
  label?: string | null;
  gate?: GateVerdict;
  capabilities: VersionCapabilities;
  raw: unknown;
}

const PROMPT_CAPABILITIES: VersionCapabilities = {
  hasHistory: true,
  canPromote: true,
  canRollback: true,
  canDiff: true,
  canEditDraft: false,
  canDelete: false,
  gating: "advisory",
};

function toIsoString(value: number | string | null): string | null {
  if (value === null) return null;
  if (typeof value === "number") return new Date(value).toISOString();
  return value;
}

/**
 * Map the MLflow-backed prompt version list onto the normalized model.
 *
 * `isLive` is derived from the registry `current` flag (falling back to whether
 * the live alias points at the version). Prompt versions are immutable in the
 * registry, so every row is `published` except the live one, surfaced `active`.
 */
export function promptVersionsToArtifactVersions(
  name: string,
  infos: PromptVersionInfo[],
  liveAlias = "prod",
): ArtifactVersion[] {
  return infos.map((info) => {
    const isLive = info.current || info.aliases.includes(liveAlias);
    return {
      artifactType: "prompt",
      artifactId: name,
      artifactName: name,
      versionKey: String(info.version),
      versionLabel: `v${info.version}`,
      ordinal: info.version,
      status: isLive ? "active" : "published",
      isLive,
      liveAliases: info.aliases,
      author: null, // MLflow prompt versions carry no author
      createdAt: toIsoString(info.creation_timestamp),
      label: info.commit_message,
      capabilities: PROMPT_CAPABILITIES,
      raw: info,
    } satisfies ArtifactVersion;
  });
}

/** Convenience: the version currently serving the live alias, if any. */
export function liveVersion(versions: ArtifactVersion[]): ArtifactVersion | undefined {
  return versions.find((v) => v.isLive);
}

/**
 * Map a tool family's `(name, version)` rows onto the normalized model.
 *
 * Tools have no live alias — there's nothing to promote or roll back — so this
 * is read-only version history (status badges only). The version string is
 * free-form; `ordinal` parses a leading integer for ordering, else null.
 */
export function toolVersionsToArtifactVersions(
  toolName: string,
  tools: ToolDefinition[],
): ArtifactVersion[] {
  return tools.map((tool) => {
    const leading = Number.parseInt(tool.version, 10);
    return {
      artifactType: "tool",
      artifactId: tool.tool_id,
      artifactName: toolName,
      versionKey: tool.tool_id,
      versionLabel: `v${tool.version}`,
      ordinal: Number.isNaN(leading) ? null : leading,
      status: tool.status, // "active" | "deprecated" | "archived" — all valid VersionStatus
      isLive: false, // no live pointer for tools
      liveAliases: [],
      author: tool.owner,
      label: tool.description || null,
      capabilities: {
        hasHistory: true,
        canPromote: false,
        canRollback: false,
        canDiff: false,
        canEditDraft: false,
        canDelete: false,
        gating: "none",
      },
      raw: tool,
    } satisfies ArtifactVersion;
  });
}

/**
 * Map skill content-version snapshots onto the normalized model.
 *
 * The highest version_number is the live content. Skills aren't "promoted"
 * (there's no alias), so only rollback is offered — restore an earlier snapshot
 * as a new version. The live row is the rollback trigger when an earlier
 * version exists.
 */
export function skillVersionsToArtifactVersions(
  skillId: string,
  versions: SkillVersionInfo[],
): ArtifactVersion[] {
  const maxNumber = versions.reduce((m, v) => Math.max(m, v.version_number), 0);
  return versions.map((version) => {
    const isLive = version.version_number === maxNumber;
    return {
      artifactType: "skill",
      artifactId: skillId,
      artifactName: skillId,
      versionKey: String(version.version_number),
      versionLabel: `v${version.version_number}`,
      ordinal: version.version_number,
      status: isLive ? "active" : "published",
      isLive,
      liveAliases: isLive ? ["current"] : [],
      author: version.created_by,
      createdAt: version.created_at,
      label: version.summary || null,
      capabilities: {
        hasHistory: true,
        canPromote: false, // skills have no alias to promote to
        canRollback: isLive && versions.length > 1,
        canDiff: true,
        canEditDraft: false,
        canDelete: false,
        gating: "none",
      },
      raw: version,
    } satisfies ArtifactVersion;
  });
}

/**
 * Map immutable workflow version rows onto the normalized model.
 *
 * `isLive` is the version the live-alias deployment points at. Only a
 * *published* (immutable) version that isn't already live can be promoted; the
 * live version is the rollback target. Drafts are editable but not promotable.
 */
export function workflowVersionsToArtifactVersions(
  workflowId: string,
  versions: WorkflowVersion[],
  deployments: WorkflowDeployment[],
  liveAlias = "prod",
): ArtifactVersion[] {
  const liveVersionId = deployments.find((d) => d.alias === liveAlias)?.version_id ?? null;
  return versions.map((version) => {
    const isLive = version.version_id === liveVersionId;
    const status: VersionStatus = isLive ? "active" : version.status;
    return {
      artifactType: "workflow",
      artifactId: workflowId,
      artifactName: workflowId,
      versionKey: version.version_id,
      versionLabel: `v${version.version_number}`,
      ordinal: version.version_number,
      status,
      isLive,
      liveAliases: isLive ? [liveAlias] : [],
      author: version.created_by ?? null,
      createdAt: version.created_at ?? null,
      publishedAt: version.published_at ?? null,
      label: null,
      capabilities: {
        hasHistory: true,
        canPromote: version.status === "published" && !isLive,
        canRollback: isLive,
        canDiff: true,
        canEditDraft: version.status === "draft",
        canDelete: false,
        gating: "advisory",
      },
      raw: version,
    } satisfies ArtifactVersion;
  });
}

/**
 * Map knowledge-base version rows onto the normalized model.
 *
 * `isLive` is the KB's `active_version_id`. Only a `completed` build can be
 * activated (promoted); the active version is the rollback target.
 */
export function knowledgeBaseVersionsToArtifactVersions(
  knowledgeBaseId: string,
  versions: KnowledgeBaseVersion[],
  activeVersionId: string | null,
): ArtifactVersion[] {
  return versions.map((version) => {
    const isLive = version.knowledge_base_version_id === activeVersionId;
    const completed = version.status === "completed";
    const status: VersionStatus = isLive ? "active" : completed ? "published" : "draft";
    return {
      artifactType: "knowledge_base",
      artifactId: knowledgeBaseId,
      artifactName: knowledgeBaseId,
      versionKey: version.knowledge_base_version_id,
      versionLabel: `v${version.version_number}`,
      ordinal: version.version_number,
      status,
      isLive,
      liveAliases: isLive ? ["active"] : [],
      label: version.embedding_model,
      capabilities: {
        hasHistory: true,
        canPromote: completed && !isLive,
        canRollback: isLive,
        canDiff: false,
        canEditDraft: false,
        canDelete: false,
        gating: "none",
      },
      raw: version,
    } satisfies ArtifactVersion;
  });
}
