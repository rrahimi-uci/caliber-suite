import { describe, expect, it } from "vitest";

import type { PromptInfo } from "@/api/types";
import {
  getAssistantOptimizationSessionStorageKey,
  resolvePromptName,
  resolvePromptRef,
  toPromptIdentitySnapshot,
} from "@/pages/Prompts";

const basePrompt: PromptInfo = {
  agent_id: "support-agent",
  agent_name: "Support Agent",
  agent_enabled: true,
  prompt_name: "support-agent-prompt",
  version: 3,
  alias: "prod",
  template_preview: "",
  template_length: 0,
  approval_id: null,
  artifact_ref: null,
  has_prompt: true,
  needs_prompt: false,
  source: "mlflow",
};

describe("Prompts identity helpers", () => {
  it("resolves prompt names and artifact refs with fallback behavior", () => {
    expect(resolvePromptName(basePrompt)).toBe("support-agent-prompt");
    expect(resolvePromptName({ ...basePrompt, prompt_name: null })).toBe("support-agent");

    expect(resolvePromptRef({ ...basePrompt, artifact_ref: "prompts:/x@prod" })).toBe("prompts:/x@prod");
    expect(resolvePromptRef(basePrompt)).toBe("prompts:/support-agent-prompt/3");
    expect(resolvePromptRef({ ...basePrompt, version: null, alias: null })).toBe("prompts:/support-agent-prompt@prod");
  });

  it("builds prompt snapshots and optimization storage keys", () => {
    expect(toPromptIdentitySnapshot(basePrompt)).toEqual({
      agent_id: "support-agent",
      agent_name: "Support Agent",
      prompt_name: "support-agent-prompt",
      alias: "prod",
      version: 3,
      artifact_ref: "prompts:/support-agent-prompt/3",
    });
    expect(getAssistantOptimizationSessionStorageKey("support-agent")).toBe(
      "caliber.prompts.optimization.assistantSession.support-agent",
    );
  });
});

