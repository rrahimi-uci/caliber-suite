/**
 * Per-artifact {@link VersionAdapter} factories — the glue between the
 * artifact-agnostic <VersionPanel> and the concrete `caliberApi` calls.
 */
import { caliberApi } from "@/api/caliberApi";
import {
  knowledgeBaseVersionsToArtifactVersions,
  promptVersionsToArtifactVersions,
  skillVersionsToArtifactVersions,
  toolVersionsToArtifactVersions,
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
export function makePromptVersionAdapter(
  name: string,
  liveAlias = "prod",
): VersionAdapter {
  let expectedLiveVersion: number | null | undefined;
  return {
    loadVersions: async () => {
      const versions = promptVersionsToArtifactVersions(
        name,
        await caliberApi.listPromptVersions(name),
        liveAlias,
      );
      const live = versions.find((version) => version.isLive);
      expectedLiveVersion = live ? Number(live.versionKey) : null;
      return versions;
    },
    promote: async (version, opts) => {
      await caliberApi.promotePrompt(name, Number(version.versionKey), {
        alias: liveAlias,
        gate_state: version.gate?.state,
        gate_score: version.gate?.score ?? undefined,
        overridden: opts.overridden,
        override_reason: opts.reason || undefined,
        expected_version: expectedLiveVersion,
      });
      expectedLiveVersion = Number(version.versionKey);
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
export function makeWorkflowVersionAdapter(
  workflowId: string,
  liveAlias = "prod",
): VersionAdapter {
  return {
    loadVersions: async () => {
      const [versions, deployments] = await Promise.all([
        caliberApi.listWorkflowVersions(workflowId),
        caliberApi.listWorkflowDeployments(workflowId),
      ]);
      return workflowVersionsToArtifactVersions(
        workflowId,
        versions,
        deployments,
        liveAlias,
      );
    },
    promote: async (version) => {
      await caliberApi.promoteWorkflow(
        workflowId,
        liveAlias,
        version.versionKey,
      );
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
export function makeKnowledgeBaseVersionAdapter(
  knowledgeBaseId: string,
): VersionAdapter {
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
      await caliberApi.activateKnowledgeBaseVersion(
        knowledgeBaseId,
        version.versionKey,
      );
    },
    rollback: async () => {
      await caliberApi.rollbackKnowledgeBase(knowledgeBaseId);
    },
  };
}

/**
 * Adapter for skill content versions. History is the snapshot list; skills have
 * no alias, so there is no promote — only rollback (restore the prior snapshot
 * as a new version). `canPromote` is false on every row, so `promote` is never
 * invoked by the panel.
 */
export function makeSkillVersionAdapter(skillId: string): VersionAdapter {
  return {
    loadVersions: async () =>
      skillVersionsToArtifactVersions(
        skillId,
        await caliberApi.listSkillVersions(skillId),
      ),
    promote: async () => {
      throw new Error(
        "skills are not promoted; use rollback to restore a prior version",
      );
    },
    rollback: async () => {
      await caliberApi.rollbackSkill(skillId);
    },
  };
}

/**
 * Adapter for a tool family's versions. Read-only history — tools have no live
 * alias, so neither promote nor rollback apply (`canPromote`/`canRollback` are
 * false on every row, so the mutators are never invoked).
 */
export function makeToolVersionAdapter(
  toolId: string,
  toolName: string,
): VersionAdapter {
  return {
    loadVersions: async () =>
      toolVersionsToArtifactVersions(
        toolName,
        await caliberApi.listToolVersions(toolId),
      ),
    promote: async () => {
      throw new Error("tools are not promoted");
    },
    rollback: async () => {
      throw new Error("tools are not rolled back");
    },
  };
}
