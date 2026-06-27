import { describe, expect, it } from "vitest";

import type { AgentConfig } from "@/api/types";
import {
  agentLabel,
  agentReferencesSkill,
  detectedSkillVariables,
  normalizeSkillTestCases,
  parseSkillVariables,
} from "@/pages/Skills";

describe("Skills helpers", () => {
  it("parses variable json values as strings", () => {
    expect(parseSkillVariables('{"name":"Ada","attempts":2}')).toEqual({
      name: "Ada",
      attempts: "2",
    });
    expect(() => parseSkillVariables("[]")).toThrow("Variables must be a JSON object.");
  });

  it("detects template variable names uniquely", () => {
    expect(
      detectedSkillVariables("Hello {{ user_name }} and {{orderId}} and {{ user_name }}"),
    ).toEqual(["orderId", "user_name"]);
  });

  it("normalizes calibration test cases and validates required fields", () => {
    const parsed = normalizeSkillTestCases([
      {
        input: "How do refunds work?",
        expectedBehavior: "References refund policy",
        tags: ["policy", "refund"],
      },
    ]);
    expect(parsed).toHaveLength(1);
    expect(parsed[0]?.tags).toEqual(["policy", "refund"]);

    expect(() => normalizeSkillTestCases({})).toThrow("Test cases must be a JSON array.");
    expect(() => normalizeSkillTestCases([{}])).toThrow("needs input and expectedBehavior");
  });

  it("finds whether an agent references a skill and builds labels", () => {
    const agent = {
      agent_id: "support-agent",
      name: "Support Agent",
      optimizer_config: { skills: ["reasoning-v1"] },
    } as AgentConfig;
    expect(agentReferencesSkill(agent, "reasoning-v1")).toBe(true);
    expect(agentReferencesSkill(agent, "other-skill")).toBe(false);
    expect(agentLabel(agent)).toBe("Support Agent (support-agent)");
    expect(agentLabel({ ...agent, name: "support-agent" })).toBe("support-agent");
  });
});

