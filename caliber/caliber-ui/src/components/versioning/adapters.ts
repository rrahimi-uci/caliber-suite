/**
 * Per-artifact {@link VersionAdapter} factories — the glue between the
 * artifact-agnostic <VersionPanel> and the concrete `caliberApi` calls.
 */
import { caliberApi } from "@/api/caliberApi";
import {
  knowledgeBaseVersionsToArtifactVersions,
  promptVersionsToArtifactVersions,
  workflowVersionsToArtifactVersions,
} from "@/api/versioning";

import type { VersionAdapter } from "@/components/versioning/VersionPanel";

/**
 * Adapter for MLflow-backed prompt versions.
 *
 * - history: `listPromptVersions` → normalized model
 * - promote: the audited `promotePrompt` carrying the gate verdict + override
 *   (the panel only enables override after a typed reason on a FAIL)
 * - rollback: the audited `rollbackPrompt`, which restores the exact prior live
 *   version recorded server-side
 *
 * The opaque `versionKey` is coerced to the numeric MLflow version here (the
 * string↔number boundary lives in the adapter, not the shared client).
 */
export function makePromptVersionAdapter(name: string, liveAlias = "prod"): VersionAdapter {
  return {
    loadVersions: async () =>
      promptVersionsToArtifactVersions(name, await caliberApi.listPromptVersions(name), liveAlias),
    promote: async (version, opts) => {
      await caliberApi.promotePrompt(name, Number(version.versionKey), {
        alias: liveAlias,
        gate_state: version.gate?.state,
        gate_score: version.gate?.score ?? undefined,
        overridden: opts.overridden,
        override_reason: opts.reason || undefined,
      });
    },
    rollback: async () => {
      await caliberApi.rollbackPrompt(name, liveAlias);
    },
  };
}

/**
 * Adapter for workflow versions. History is the version list joined with the
 * deployments (to mark which version the live alias serves). Promote rotates
 * the live alias to a published version; rollback pops the server-side
 * deployment rollback stack. Workflow deploys use deploy-gates, not the
 * per-version advisory verdict, so the gate/override opts are unused here.
 */
export function makeWorkflowVersionAdapter(workflowId: string, liveAlias = "prod"): VersionAdapter {
  return {
    loadVersions: async () => {
      const [versions, deployments] = await Promise.all([
        caliberApi.listWorkflowVersions(workflowId),
        caliberApi.listWorkflowDeployments(workflowId),
      ]);
      return workflowVersionsToArtifactVersions(workflowId, versions, deployments, liveAlias);
    },
    promote: async (version) => {
      await caliberApi.promoteWorkflow(workflowId, liveAlias, version.versionKey);
    },
    rollback: async () => {
      await caliberApi.rollbackWorkflow(workflowId, liveAlias);
    },
  };
}

/**
 * Adapter for knowledge-base versions. History is the version list; the active
 * pointer (`active_version_id` on the KB record) marks which is live. Promote
 * activates a completed version; rollback re-activates the recorded prior
 * active version.
 */
export function makeKnowledgeBaseVersionAdapter(knowledgeBaseId: string): VersionAdapter {
  return {
    loadVersions: async () => {
      const [versions, kb] = await Promise.all([
        caliberApi.listKnowledgeBaseVersions(knowledgeBaseId),
        caliberApi.getKnowledgeBase(knowledgeBaseId),
      ]);
      return knowledgeBaseVersionsToArtifactVersions(
        knowledgeBaseId,
        versions,
        kb.active_version_id ?? null,
      );
    },
    promote: async (version) => {
      await caliberApi.activateKnowledgeBaseVersion(knowledgeBaseId, version.versionKey);
    },
    rollback: async () => {
      await caliberApi.rollbackKnowledgeBase(knowledgeBaseId);
    },
  };
}
