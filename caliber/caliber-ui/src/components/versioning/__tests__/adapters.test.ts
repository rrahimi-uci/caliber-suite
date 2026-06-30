import { describe, expect, it, vi } from "vitest";

import { caliberApi } from "@/api/caliberApi";
import type { ArtifactVersion } from "@/api/versioning";
import {
  makeKnowledgeBaseVersionAdapter,
  makePromptVersionAdapter,
  makeWorkflowVersionAdapter,
} from "@/components/versioning/adapters";

vi.mock("@/api/caliberApi", () => ({
  caliberApi: {
    listPromptVersions: vi.fn(),
    promotePrompt: vi.fn(),
    rollbackPrompt: vi.fn(),
    listWorkflowVersions: vi.fn(),
    listWorkflowDeployments: vi.fn(),
    promoteWorkflow: vi.fn(),
    rollbackWorkflow: vi.fn(),
    listKnowledgeBaseVersions: vi.fn(),
    getKnowledgeBase: vi.fn(),
    activateKnowledgeBaseVersion: vi.fn(),
    rollbackKnowledgeBase: vi.fn(),
  },
}));

const api = caliberApi as unknown as Record<string, ReturnType<typeof vi.fn>>;

function artifactVersion(overrides: Partial<ArtifactVersion>): ArtifactVersion {
  return {
    artifactType: "prompt",
    artifactId: "support-agent",
    artifactName: "support-agent",
    versionKey: "5",
    versionLabel: "v5",
    ordinal: 5,
    status: "published",
    isLive: false,
    liveAliases: [],
    capabilities: {
      hasHistory: true,
      canPromote: true,
      canRollback: true,
      canDiff: true,
      canEditDraft: false,
      canDelete: false,
      gating: "advisory",
    },
    raw: null,
    ...overrides,
  };
}

describe("makePromptVersionAdapter", () => {
  it("loads + normalizes the prompt version list", async () => {
    api.listPromptVersions.mockResolvedValue([
      {
        name: "support-agent",
        version: 5,
        aliases: ["prod"],
        creation_timestamp: null,
        updated_timestamp: null,
        run_id: null,
        source: null,
        commit_message: "v5",
        current: true,
      },
    ]);
    const adapter = makePromptVersionAdapter("support-agent");
    const versions = await adapter.loadVersions();
    expect(api.listPromptVersions).toHaveBeenCalledWith("support-agent");
    expect(versions[0]).toMatchObject({ versionKey: "5", isLive: true });
  });

  it("coerces versionKey to a number and forwards the gate/override on promote", async () => {
    const adapter = makePromptVersionAdapter("support-agent");
    await adapter.promote(
      artifactVersion({ versionKey: "5", gate: { state: "fail", score: 0.7 } }),
      { overridden: true, reason: "urgent hotfix" },
    );
    expect(api.promotePrompt).toHaveBeenCalledWith("support-agent", 5, {
      alias: "prod",
      gate_state: "fail",
      gate_score: 0.7,
      overridden: true,
      override_reason: "urgent hotfix",
    });
  });

  it("omits the override reason when not provided", async () => {
    const adapter = makePromptVersionAdapter("support-agent");
    await adapter.promote(artifactVersion({ versionKey: "6" }), {
      overridden: false,
      reason: "",
    });
    expect(api.promotePrompt).toHaveBeenCalledWith("support-agent", 6, {
      alias: "prod",
      gate_state: undefined,
      gate_score: undefined,
      overridden: false,
      override_reason: undefined,
    });
  });

  it("rolls back via the audited endpoint", async () => {
    const adapter = makePromptVersionAdapter("support-agent");
    await adapter.rollback();
    expect(api.rollbackPrompt).toHaveBeenCalledWith("support-agent", "prod");
  });
});

describe("makeWorkflowVersionAdapter", () => {
  it("joins versions with the live deployment to mark isLive + promotability", async () => {
    api.listWorkflowVersions.mockResolvedValue([
      { version_id: "WFV-2", workflow_id: "WF-1", version_number: 2, status: "published" },
      { version_id: "WFV-1", workflow_id: "WF-1", version_number: 1, status: "published" },
      { version_id: "WFV-d", workflow_id: "WF-1", version_number: 3, status: "draft" },
    ]);
    api.listWorkflowDeployments.mockResolvedValue([
      { deployment_id: "DEP-1", workflow_id: "WF-1", alias: "prod", version_id: "WFV-2", status: "active" },
    ]);
    const versions = await makeWorkflowVersionAdapter("WF-1").loadVersions();
    const live = versions.find((v) => v.versionKey === "WFV-2");
    const prior = versions.find((v) => v.versionKey === "WFV-1");
    const draft = versions.find((v) => v.versionKey === "WFV-d");
    expect(live).toMatchObject({ isLive: true, status: "active" });
    expect(live?.capabilities.canRollback).toBe(true);
    expect(prior).toMatchObject({ isLive: false, status: "published" });
    expect(prior?.capabilities.canPromote).toBe(true);
    // A draft can be edited but not promoted.
    expect(draft?.capabilities.canPromote).toBe(false);
    expect(draft?.capabilities.canEditDraft).toBe(true);
  });

  it("promotes a version_id to the live alias and rolls back via the stack", async () => {
    const adapter = makeWorkflowVersionAdapter("WF-1");
    await adapter.promote(
      { versionKey: "WFV-9" } as ArtifactVersion,
      { overridden: false, reason: "" },
    );
    expect(api.promoteWorkflow).toHaveBeenCalledWith("WF-1", "prod", "WFV-9");
    await adapter.rollback();
    expect(api.rollbackWorkflow).toHaveBeenCalledWith("WF-1", "prod");
  });
});

describe("makeKnowledgeBaseVersionAdapter", () => {
  it("marks the active version live and only completed versions promotable", async () => {
    api.listKnowledgeBaseVersions.mockResolvedValue([
      { knowledge_base_version_id: "KBV-2", knowledge_base_id: "KB-1", version_number: 2, status: "completed", embedding_model: "m" },
      { knowledge_base_version_id: "KBV-1", knowledge_base_id: "KB-1", version_number: 1, status: "completed", embedding_model: "m" },
      { knowledge_base_version_id: "KBV-f", knowledge_base_id: "KB-1", version_number: 3, status: "failed", embedding_model: "m" },
    ]);
    api.getKnowledgeBase.mockResolvedValue({ knowledge_base_id: "KB-1", active_version_id: "KBV-2" });
    const versions = await makeKnowledgeBaseVersionAdapter("KB-1").loadVersions();
    expect(versions.find((v) => v.versionKey === "KBV-2")).toMatchObject({ isLive: true, status: "active" });
    expect(versions.find((v) => v.versionKey === "KBV-1")?.capabilities.canPromote).toBe(true);
    // A failed build cannot be activated.
    expect(versions.find((v) => v.versionKey === "KBV-f")?.capabilities.canPromote).toBe(false);
  });

  it("activates a version on promote and rolls back to the prior active", async () => {
    const adapter = makeKnowledgeBaseVersionAdapter("KB-1");
    await adapter.promote({ versionKey: "KBV-9" } as ArtifactVersion, { overridden: false, reason: "" });
    expect(api.activateKnowledgeBaseVersion).toHaveBeenCalledWith("KB-1", "KBV-9");
    await adapter.rollback();
    expect(api.rollbackKnowledgeBase).toHaveBeenCalledWith("KB-1");
  });
});
