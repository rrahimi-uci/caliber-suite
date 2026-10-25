import { afterEach, describe, expect, it, vi } from "vitest";

import { caliberApi } from "@/api/caliberApi";
import type { PromptInfo } from "@/api/types";
import {
  calibrationReviewView,
  clampTestCaseCount,
  defaultEditTargetAlias,
  describeBinding,
  deltaTone,
  formatDelta,
  formatOverallScore,
  formatScore,
  getPromptAliasOptions,
  getAssistantOptimizationSessionStorageKey,
  isTestablePrompt,
  isAssistantRefinementJob,
  normalizeUploadedExample,
  parseUploadedDataset,
  readString,
  readStringArray,
  runPromptTestCases,
  resolvePromptName,
  resolvePromptRef,
  scoreSnapshot,
  scorerCategoryLabel,
  statusTone,
  summarizeResults,
  synthesizePromptInfo,
  toPromptIdentitySnapshot,
} from "@/pages/Prompts";

const prompt = (overrides: Partial<PromptInfo> = {}): PromptInfo => ({
  agent_id: "agent-1",
  agent_name: "Support",
  agent_enabled: true,
  prompt_name: "support",
  version: 3,
  alias: "prod",
  available_aliases: [],
  template_preview: "Hello",
  template_length: 5,
  approval_id: null,
  artifact_ref: null,
  has_prompt: true,
  needs_prompt: false,
  source: "mlflow",
  ...overrides,
});

describe("Prompt decision helpers", () => {
  it("resolves prompt identity and all reference fallbacks", () => {
    expect(resolvePromptName(prompt())).toBe("support");
    expect(resolvePromptName(prompt({ prompt_name: null }))).toBe("agent-1");
    expect(isTestablePrompt(prompt())).toBe(true);
    expect(isTestablePrompt(prompt({ prompt_name: "  " }))).toBe(false);
    expect(resolvePromptRef(prompt({ artifact_ref: "runs:/a/b" }))).toBe("runs:/a/b");
    expect(resolvePromptRef(prompt({ version: 4 }))).toBe("prompts:/support/4");
    expect(resolvePromptRef(prompt({ version: null, alias: "" }))).toBe("prompts:/support@prod");
    expect(toPromptIdentitySnapshot(prompt({ alias: "" }))).toMatchObject({
      prompt_name: "support", alias: "prod", artifact_ref: "prompts:/support/3",
    });
    expect(synthesizePromptInfo("new-prompt")).toMatchObject({
      agent_id: "new-prompt", prompt_name: "new-prompt", source: "caliber", has_prompt: true,
    });
  });

  it("orders aliases consistently and chooses the live edit target", () => {
    expect(getPromptAliasOptions(null)).toEqual([]);
    expect(getPromptAliasOptions(prompt({ available_aliases: ["dev", "custom", "prod"], alias: "staging" }))).toEqual([
      "prod", "staging", "dev", "custom",
    ]);
    expect(getPromptAliasOptions(prompt({ available_aliases: [], alias: "" }))).toEqual(["prod"]);
    expect(defaultEditTargetAlias(prompt({ alias: "staging" }))).toBe("prod");
    expect(defaultEditTargetAlias(prompt({ alias: "prod", available_aliases: ["staging"] }))).toBe("prod");
  });

  it("describes bindings and aggregates test verdicts", () => {
    expect(describeBinding(null)).toBeNull();
    expect(describeBinding({ kind: "agent", agent_id: "a1" })).toBe("Production agent · a1");
    expect(describeBinding({ kind: "agent" })).toBe("Production agent");
    expect(describeBinding({ kind: "workflow_node", workflow_id: "wf", node_id: "node" })).toBe("Workflow node · wf / node");
    expect(describeBinding({ kind: "workflow_node" })).toBe("Workflow node · ? / ?");
    expect(describeBinding({ kind: "standalone" })).toBe("Standalone");
    expect(describeBinding({ kind: "future" })).toBe("future");
    expect(summarizeResults([])).toEqual({ passCount: 0, failCount: 0, partialCount: 0, overallScore: null });
    expect(summarizeResults([
      { testCaseId: "1", input: "a", expectedBehavior: "b", actualResponse: "c", verdict: "pass", score: 1, reasoning: "ok" },
      { testCaseId: "2", input: "a", expectedBehavior: "b", actualResponse: "c", verdict: "partial", score: 0.5, reasoning: "partial" },
      { testCaseId: "3", input: "a", expectedBehavior: "b", actualResponse: "c", verdict: "fail", score: 0, reasoning: "bad" },
    ])).toEqual({ passCount: 1, failCount: 1, partialCount: 1, overallScore: 0.5 });
  });

  it("normalizes calibration review data with measured and missing fields", () => {
    const view = calibrationReviewView({
      candidate: {
        baseline_content: "old",
        content: "new",
        rationale: "because",
        diff_summary: "one change",
      },
      eval_results: {
        candidate: { overall: 0.8, dimensions: { quality: 0.9, ignored: "nope" } },
        baseline: { overall: 0.6, dimensions: { quality: 0.7 } },
        deltas: { overall: 0.2 },
        gate: { passed: true, reasons: ["better"] },
        n_examples: 4,
        eval_dataset_id: "ds-1",
        simulated: true,
        provider: "judge",
      },
    } as never);
    expect(view).toMatchObject({
      baselineContent: "old", candidateContent: "new", overallDelta: 0.2,
      gatePassed: true, gateReasons: ["better"], exampleCount: 4, simulated: true,
    });
    const fallback = calibrationReviewView({ candidate: null, eval_results: {} } as never);
    expect(fallback.candidateScore).toEqual({ overall: null, dimensions: {} });
    expect(fallback.overallDelta).toBeNull();
    expect(fallback.gatePassed).toBeNull();
  });

  it("validates assistant jobs and formats calibration metrics safely", () => {
    expect(getAssistantOptimizationSessionStorageKey("agent-1")).toContain("agent-1");
    expect(isAssistantRefinementJob(null)).toBe(false);
    expect(isAssistantRefinementJob({ job_id: "job", status: "running", current_stage: "candidate", created_at: "now", error_message: null })).toBe(true);
    expect(isAssistantRefinementJob({ job_id: "", status: "running", current_stage: "candidate", created_at: "now", error_message: null })).toBe(false);
    expect(isAssistantRefinementJob({ job_id: "job", status: "unknown", current_stage: "candidate", created_at: "now", error_message: null })).toBe(false);
    expect(isAssistantRefinementJob({ job_id: "job", status: "running", current_stage: "unknown", created_at: "now", error_message: null })).toBe(false);
    expect(isAssistantRefinementJob({ job_id: "job", status: "running", current_stage: "candidate", created_at: "now", error_message: 3 })).toBe(false);
    expect(scoreSnapshot(null)).toEqual({ overall: null, dimensions: {} });
    expect(scoreSnapshot({ overall: 0.8, dimensions: { quality: 0.9, ignored: "x" } })).toEqual({ overall: 0.8, dimensions: { quality: 0.9 } });
    expect(formatScore(null)).toBe("—");
    expect(formatScore(0.875)).toBe("87.5%");
    expect(formatDelta(null)).toBe("—");
    expect(formatDelta(0.1)).toBe("+10.0 pts");
    expect(formatDelta(-0.1)).toBe("-10.0 pts");
    expect(deltaTone(null)).toContain("zinc");
    expect(deltaTone(0)).toContain("zinc");
    expect(deltaTone(-0.1)).toContain("red");
    expect(deltaTone(0.1)).toContain("emerald");
    expect(clampTestCaseCount(Number.NaN)).toBe(1);
    expect(clampTestCaseCount(-2)).toBe(1);
    expect(clampTestCaseCount(4.6)).toBe(5);
    expect(clampTestCaseCount(999)).toBe(50);
  });

  it("normalizes uploaded examples across supported JSON shapes", () => {
    expect(readString(" value ")).toBe(" value ");
    expect(readString("  ")).toBeNull();
    expect(readString(3)).toBeNull();
    expect(readStringArray(["a", " ", 3, "b"])).toEqual(["a", "b"]);
    expect(readStringArray(null)).toEqual([]);
    expect(scorerCategoryLabel("deepeval_beta")).toBe("DeepEval");
    expect(scorerCategoryLabel("core")).toBe("Core MLflow");
    expect(scorerCategoryLabel("custom")).toBe("custom");
    expect(formatOverallScore({})).toBe("—");
    expect(formatOverallScore({ candidate: { overall: 0.8125 } })).toBe("81.3%");
    expect(formatOverallScore({ candidate: { overall: "0.8" } })).toBe("—");

    expect(normalizeUploadedExample({
      input: { user_message: "structured" },
      expected: { expected_response: "answer" },
      tags: ["a", 2, "b"],
      weight: 2,
    }, 0)).toEqual({
      input: { user_message: "structured" },
      expected: { expected_response: "answer" },
      tags: ["a", "b"],
      weight: 2,
    });
    expect(normalizeUploadedExample({
      input: "string input",
      expected: "string expected",
      reference_answer: "ignored when expected is present",
    }, 1)).toMatchObject({
      input: { user_message: "string input" },
      expected: { expected_response: "string expected" },
      weight: undefined,
    });
    expect(normalizeUploadedExample({ user_message: "legacy", reference_answer: "reference" }, 2)).toEqual({
      input: { user_message: "legacy" },
      expected: { expected_response: "reference" },
      tags: [],
      weight: undefined,
    });
    expect(normalizeUploadedExample({}, 3)).toEqual({
      input: { user_message: "Example 4" },
      expected: { expected_response: "" },
      tags: [],
      weight: undefined,
    });
    expect(() => normalizeUploadedExample(null, 0)).toThrow("row 1");
    expect(parseUploadedDataset(
      '{"input":"one"}\n\n{"user_message":"two"}\n',
      "cases.jsonl",
    )).toHaveLength(2);
    expect(parseUploadedDataset(
      JSON.stringify({ examples: [{ input: "nested" }] }),
      "cases.json",
    )[0]).toMatchObject({ input: { user_message: "nested" } });
    expect(parseUploadedDataset("[{\"input\":\"array\"}]", "cases.json")).toHaveLength(1);
    expect(() => parseUploadedDataset("{}", "cases.json")).toThrow(
      "array or an object with an examples array",
    );
  });

  it("covers refinement status and tone fallbacks", () => {
    for (const status of ["completed", "applied"]) {
      expect(statusTone(status)).toContain("emerald");
    }
    for (const status of ["running", "queued"]) {
      expect(statusTone(status)).toContain("blue");
    }
    for (const status of ["blocked", "candidate_ready"]) {
      expect(statusTone(status)).toContain("amber");
    }
    for (const status of ["failed", "rejected", "cancelled"]) {
      expect(statusTone(status)).toContain("red");
    }
    expect(statusTone("unknown")).toContain("zinc");
  });

  it("covers prompt identity, scoring, and upload boundary combinations", () => {
    expect(resolvePromptName(prompt({ prompt_name: "  " }))).toBe("  ");
    expect(isTestablePrompt(prompt({ prompt_name: null }))).toBe(false);
    expect(isTestablePrompt(prompt({ prompt_name: "" }))).toBe(false);
    expect(resolvePromptRef(prompt({ artifact_ref: "" }))).toBe("prompts:/support/3");
    expect(resolvePromptRef(prompt({ artifact_ref: null, version: 0, alias: "dev" }))).toBe("prompts:/support/0");
    expect(resolvePromptRef(prompt({ artifact_ref: null, version: null, alias: null }))).toBe("prompts:/support@prod");
    expect(toPromptIdentitySnapshot(prompt({ alias: null, prompt_name: null, version: null }))).toMatchObject({
      prompt_name: "agent-1", alias: "prod", artifact_ref: "prompts:/agent-1@prod",
    });
    expect(getPromptAliasOptions(prompt({ available_aliases: null, alias: null }))).toEqual(["prod"]);
    expect(getPromptAliasOptions(prompt({ available_aliases: ["zeta", "alpha"], alias: "beta" }))).toEqual([
      "alpha", "beta", "zeta",
    ]);
    expect(defaultEditTargetAlias(prompt({ alias: "dev", available_aliases: [] }))).toBe("prod");

    expect(describeBinding({ kind: "agent", agent_id: 3 })).toBe("Production agent");
    expect(describeBinding({ kind: "workflow_node", workflow_id: "wf" })).toBe("Workflow node · wf / ?");
    expect(describeBinding({ kind: "workflow_node", node_id: "node" })).toBe("Workflow node · ? / node");
    expect(describeBinding({ kind: "" })).toBe("");
    expect(describeBinding({ kind: 3 })).toBeNull();

    expect(summarizeResults([
      { testCaseId: "1", input: "", expectedBehavior: "", actualResponse: "", verdict: "unknown", score: null, reasoning: "" },
    ] as never)).toEqual({ passCount: 0, failCount: 0, partialCount: 0, overallScore: 0 });
    expect(scoreSnapshot({ overall: "0.8", dimensions: null })).toEqual({ overall: null, dimensions: {} });
    expect(scoreSnapshot({ overall: 0, dimensions: ["bad"] })).toEqual({ overall: 0, dimensions: {} });
    expect(calibrationReviewView({
      candidate: { content: "new", baseline_content: "old" },
      eval_results: {
        candidate: { overall: 0.5, dimensions: { quality: 0.5 } },
        baseline: { overall: 0.5, dimensions: { quality: 0.4 } },
        gate: { passed: false, reasons: ["regressed", 3] },
      },
    } as never)).toMatchObject({ overallDelta: 0, gatePassed: false, gateReasons: ["regressed"] });
    expect(formatScore(Number.NaN)).toBe("NaN%");
    expect(formatDelta(Number.NaN)).toBe("NaN pts");

    for (const status of ["applied", "candidate_ready", "rejected", "cancelled", "other"]) {
      expect(isAssistantRefinementJob({
        job_id: "job", status, current_stage: "done", created_at: "now", error_message: null,
      })).toBe(["applied", "candidate_ready", "rejected", "cancelled"].includes(status));
    }
    for (const stage of ["triage", "evidence", "diagnosis", "candidate", "eval", "done", "other"]) {
      expect(isAssistantRefinementJob({
        job_id: "job", status: "running", current_stage: stage, created_at: "now", error_message: null,
      })).toBe(stage !== "other");
    }
    expect(isAssistantRefinementJob({
      job_id: "job", status: "running", current_stage: "done", created_at: "now", error_message: "failed",
    })).toBe(true);

    expect(readStringArray(["ok", " ", 4, "also"])).toEqual(["ok", "also"]);
    expect(formatOverallScore({ candidate: null })).toBe("—");
    expect(formatOverallScore({ candidate: { overall: Number.NaN } })).toBe("NaN%");
    expect(parseUploadedDataset("{\"input\":\"broken\"}", "data.jsonl")).toHaveLength(1);
    expect(() => parseUploadedDataset("{bad}", "data.jsonl")).toThrow();
    expect(normalizeUploadedExample({ input: [], expected: [], tags: "not-array", weight: "2" }, 0)).toEqual({
      input: { user_message: "Example 1" }, expected: { expected_response: "" }, tags: [], weight: undefined,
    });
    expect(normalizeUploadedExample({ input: { value: 1 }, expected: { answer: 2 } }, 0)).toMatchObject({
      input: { value: 1 }, expected: { answer: 2 },
    });
  });
});

describe("runPromptTestCases", () => {
  afterEach(() => vi.restoreAllMocks());

  it("records judged, malformed, empty, and API-error cases with progress callbacks", async () => {
    const create = vi.spyOn(caliberApi, "createAssistantSession").mockImplementation(
      async ({ title }) => {
        if (String(title).includes("#4")) throw new Error("assistant unavailable");
        return { session_id: `session-${String(title).slice(0, 8)}` } as never;
      },
    );
    const judgeResponses = [
      '{"verdict":"pass","score":1.4,"reasoning":"good"}',
      "{not-json}",
      "No JSON here",
    ];
    let judgeIndex = 0;
    vi.spyOn(caliberApi, "sendAssistantMessage").mockImplementation(async (_session, body) => {
      if (body.content === "Judge the response now.") {
        return { assistant_message: { content: judgeResponses[judgeIndex++] ?? "" } } as never;
      }
      return { assistant_message: { content: "actual response" } } as never;
    });
    const progress: Array<[number, number]> = [];
    const partial: number[] = [];
    const results = await runPromptTestCases({
      promptName: "support",
      version: 2,
      alias: "prod",
      template: "You help users.",
      cases: [
        { id: "1", input: "one", expectedBehavior: "first" },
        { id: "2", input: "two", expectedBehavior: "second" },
        { id: "3", input: "three", expectedBehavior: "third" },
        { id: "4", input: "four", expectedBehavior: "fourth" },
      ] as never,
      onProgress: (current, total) => progress.push([current, total]),
      onPartial: (items) => partial.push(items.length),
    });
    expect(create).toHaveBeenCalled();
    expect(progress).toEqual([[1, 4], [2, 4], [3, 4], [4, 4]]);
    expect(partial).toEqual([1, 2, 3, 4]);
    expect(results).toMatchObject([
      { verdict: "pass", score: 1, reasoning: "good", actualResponse: "actual response" },
      { verdict: "fail", score: 0, reasoning: "Judge response was not valid JSON" },
      { verdict: "fail", score: 0, reasoning: "Could not parse judge response" },
      { verdict: "fail", score: 0, reasoning: "assistant unavailable" },
    ]);
  });
});
