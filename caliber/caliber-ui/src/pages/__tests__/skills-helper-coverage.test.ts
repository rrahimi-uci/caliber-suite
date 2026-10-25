import { describe, expect, it } from "vitest";

import {
  agentLabel,
  agentReferencesSkill,
  describeSkillBinding,
  detectedSkillVariables,
  normalizeSkillTestCases,
  parseSkillVariables,
  summarizeSkillResults,
} from "@/pages/Skills";

describe("Skills helper decisions", () => {
  it("summarizes runs and describes every binding kind", () => {
    expect(summarizeSkillResults([])).toEqual({ passCount: 0, failCount: 0, partialCount: 0, overallScore: null });
    expect(summarizeSkillResults([
      { verdict: "pass", score: 1 },
      { verdict: "partial", score: 0.5 },
      { verdict: "fail", score: 0 },
    ] as never)).toEqual({ passCount: 1, failCount: 1, partialCount: 1, overallScore: 0.5 });
    expect(describeSkillBinding(null)).toBeNull();
    expect(describeSkillBinding({})).toBeNull();
    expect(describeSkillBinding({ kind: "agent", agent_id: "support" })).toBe("Production agent · support");
    expect(describeSkillBinding({ kind: "agent" })).toBe("Production agent");
    expect(describeSkillBinding({ kind: "workflow_node", workflow_id: "wf", node_id: "node" })).toBe("Workflow node · wf / node");
    expect(describeSkillBinding({ kind: "workflow_node" })).toBe("Workflow node · ? / ?");
    expect(describeSkillBinding({ kind: "standalone" })).toBe("Standalone");
    expect(describeSkillBinding({ kind: "future" })).toBe("future");
  });

  it("parses variables, finds placeholders, and validates generated cases", () => {
    expect(parseSkillVariables('{"name":"Ada","count":2,"enabled":true}')).toEqual({ name: "Ada", count: "2", enabled: "true" });
    expect(() => parseSkillVariables("[]")).toThrow("JSON object");
    expect(() => parseSkillVariables("not-json")).toThrow();
    expect(detectedSkillVariables("Hello {{ name }} {{name}} {{_id}} {{bad-name}} {{2bad}}" )).toEqual(["_id", "name"]);
    expect(detectedSkillVariables("No placeholders here")).toEqual([]);
    expect(() => normalizeSkillTestCases("not-an-array")).toThrow("JSON array");
    expect(normalizeSkillTestCases([
      { input: "  hello ", expectedBehavior: "  answer  ", tags: [" smoke ", "", 2] },
      { input: "next", expectedBehavior: "next" },
    ])).toEqual([
      { id: expect.stringMatching(/^skill-tc-/), input: "hello", expectedBehavior: "answer", tags: [" smoke "] },
      { id: expect.stringMatching(/^skill-tc-/), input: "next", expectedBehavior: "next", tags: [] },
    ]);
    expect(() => normalizeSkillTestCases([null])).toThrow("must be an object");
    expect(() => normalizeSkillTestCases([{ input: "", expectedBehavior: "ok" }])).toThrow("needs input");
    expect(() => normalizeSkillTestCases([{ input: "ok", expectedBehavior: "" }])).toThrow("needs input");
  });

  it("handles agent skill references and display labels", () => {
    expect(agentReferencesSkill({ optimizer_config: { skills: ["support"] } } as never, "support")).toBe(true);
    expect(agentReferencesSkill({ optimizer_config: { skills: [] } } as never, "support")).toBe(false);
    expect(agentReferencesSkill({ optimizer_config: {} } as never, "support")).toBe(false);
    expect(agentLabel({ agent_id: "a1", name: "Support" } as never)).toBe("Support (a1)");
    expect(agentLabel({ agent_id: "a1", name: "a1" } as never)).toBe("a1");
    expect(agentLabel({ agent_id: "a1", name: "" } as never)).toBe("a1");
  });
});
