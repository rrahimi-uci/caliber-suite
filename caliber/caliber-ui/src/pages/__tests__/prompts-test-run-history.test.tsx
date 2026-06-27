import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { MemoryRouter } from "react-router-dom";

import type {
  PromptInfo,
  PromptTestRunDetail,
  PromptTestRunSummary,
} from "@/api/types";

// Mock the API client so these tests target the durability wiring (auto-save,
// history, replay) rather than re-driving the LLM generate/run flow.
vi.mock("@/api/caliberApi", () => ({
  caliberApi: {
    getAssistantConfig: vi.fn(),
    updateAssistantConfig: vi.fn(),
    getPrompt: vi.fn(),
    createAssistantSession: vi.fn(),
    sendAssistantMessage: vi.fn(),
    savePromptTestRun: vi.fn(),
    listPromptTestRuns: vi.fn(),
    getPromptTestRun: vi.fn(),
    createEvalDataset: vi.fn(),
    appendEvalExample: vi.fn(),
  },
}));

import { caliberApi } from "@/api/caliberApi";
import { PromptTestCases } from "@/pages/Prompts";

const mockApi = vi.mocked(caliberApi);

const supportPrompt: PromptInfo = {
  agent_id: "support-agent",
  agent_name: "Support Agent",
  agent_enabled: true,
  prompt_name: "support-agent",
  version: 3,
  alias: "prod",
  available_aliases: ["prod"],
  template_preview: "You are a helpful support assistant.",
  template_length: 36,
  approval_id: null,
  artifact_ref: "prompts:/support-agent@prod",
  has_prompt: true,
  needs_prompt: false,
  source: "both",
};

function summary(overrides: Partial<PromptTestRunSummary> = {}): PromptTestRunSummary {
  return {
    test_run_id: "PTR-hist1",
    agent_id: "support-agent",
    prompt_name: "support-agent",
    prompt_alias: "prod",
    prompt_version: 3,
    model: "gpt-4o-mini",
    eval_dataset_id: null,
    test_set_size: 2,
    passed_count: 1,
    failed_count: 1,
    partial_count: 0,
    overall_score: 0.5,
    trace_id: null,
    mlflow_run_id: null,
    created_by: "@test",
    status: "completed",
    created_at: "2025-01-02T10:00:00Z",
    completed_at: "2025-01-02T10:01:00Z",
    ...overrides,
  };
}

function detail(overrides: Partial<PromptTestRunDetail> = {}): PromptTestRunDetail {
  return {
    ...summary(),
    results: [
      {
        testCaseId: "tc-1",
        input: "How do I reset my password?",
        expectedBehavior: "Explain the reset flow",
        actualResponse: "Click 'Forgot password'.",
        verdict: "pass",
        score: 1,
        reasoning: "Correct guidance",
      },
      {
        testCaseId: "tc-2",
        input: "boom",
        expectedBehavior: "Stay safe",
        actualResponse: "",
        verdict: "fail",
        score: 0,
        reasoning: "Crashed",
      },
    ],
    ...overrides,
  };
}

function renderRunner(): void {
  render(
    <MemoryRouter future={{ v7_startTransition: true, v7_relativeSplatPath: true }}>
      <PromptTestCases prompts={[supportPrompt]} loading={false} />
    </MemoryRouter>,
  );
}

beforeEach(() => {
  mockApi.getAssistantConfig.mockResolvedValue({
    engine: "fake",
    model: "gpt-4o-mini",
    provider: "openai",
    reasoning: "medium",
    enabled: true,
    disabled_intents: [],
    disabled_domains: [],
    available_models: [
      { id: "gpt-4o-mini", name: "GPT-4o Mini", provider: "openai" },
    ],
  } as never);
  mockApi.getPrompt.mockResolvedValue({
    name: "support-agent",
    version: 3,
    alias: "prod",
    template: "You are a helpful support assistant.",
    template_length: 36,
    artifact_ref: "prompts:/support-agent@prod",
  } as never);
  mockApi.listPromptTestRuns.mockResolvedValue([]);
  mockApi.getPromptTestRun.mockResolvedValue(detail());
  mockApi.savePromptTestRun.mockResolvedValue(summary());
});

afterEach(() => {
  vi.clearAllMocks();
});

describe("prompt test run durability", () => {
  it("auto-saves a completed run with the right payload", async () => {
    const user = userEvent.setup();
    // One agent session + one judge session per case; both return judge JSON
    // (the agent response is just text the judge then scores).
    mockApi.createAssistantSession.mockResolvedValue({
      session_id: "ASST-x",
    } as never);
    mockApi.sendAssistantMessage.mockResolvedValue({
      assistant_message: {
        content: '{"verdict":"pass","score":0.9,"reasoning":"Looks good"}',
      },
    } as never);
    // Seed one stored run so we can replay it into the runner (gives test cases
    // without driving the generate flow).
    mockApi.listPromptTestRuns.mockResolvedValue([summary()]);

    renderRunner();
    await screen.findByLabelText("Select model");

    // Replay loads the stored cases into the runner.
    await user.click(await screen.findByRole("button", { name: "Replay" }));
    await waitFor(() => expect(mockApi.getPromptTestRun).toHaveBeenCalled());

    // Now run the (replayed) tests; completion triggers the auto-save.
    await user.click(await screen.findByRole("button", { name: /Run Tests & Judge/i }));

    await waitFor(() => expect(mockApi.savePromptTestRun).toHaveBeenCalled());
    const payload = mockApi.savePromptTestRun.mock.calls[0]![0];
    expect(payload.agent_id).toBe("support-agent");
    expect(payload.prompt_name).toBe("support-agent");
    expect(payload.prompt_alias).toBe("prod");
    expect(payload.prompt_version).toBe(3);
    expect(payload.model).toBe("gpt-4o-mini");
    expect(payload.results).toHaveLength(2);
    expect(payload.results[0]!.verdict).toBe("pass");

    expect(await screen.findByTestId("run-saved-indicator")).toBeInTheDocument();
  });

  it("renders run history and expands per-case detail", async () => {
    const user = userEvent.setup();
    mockApi.listPromptTestRuns.mockResolvedValue([summary()]);

    renderRunner();
    await screen.findByLabelText("Select model");

    const historySection = await screen.findByTestId("run-history");
    // Summary row shows the overall score and pass/fail counts.
    expect(within(historySection).getByText("50%")).toBeInTheDocument();
    expect(within(historySection).getByText(/1 pass/)).toBeInTheDocument();

    // Expanding the row fetches + renders per-case detail.
    await user.click(
      within(historySection).getByRole("button", {
        name: /Toggle details for run PTR-hist1/i,
      }),
    );
    await waitFor(() =>
      expect(mockApi.getPromptTestRun).toHaveBeenCalledWith("PTR-hist1"),
    );
    expect(
      await screen.findByText("How do I reset my password?"),
    ).toBeInTheDocument();
    expect(screen.getByText("Correct guidance")).toBeInTheDocument();
    expect(screen.getByText("Crashed")).toBeInTheDocument();
  });

  it("replays a stored run by loading its cases into the runner", async () => {
    const user = userEvent.setup();
    mockApi.listPromptTestRuns.mockResolvedValue([summary()]);

    renderRunner();
    await screen.findByLabelText("Select model");

    await user.click(await screen.findByRole("button", { name: "Replay" }));

    await waitFor(() =>
      expect(mockApi.getPromptTestRun).toHaveBeenCalledWith("PTR-hist1"),
    );
    // The stored case inputs are now in the runner's test-set table.
    expect(
      await screen.findByText("How do I reset my password?"),
    ).toBeInTheDocument();
    // And the runner exposes a Run button now that cases are loaded.
    expect(
      screen.getByRole("button", { name: /Run Tests & Judge/i }),
    ).toBeInTheDocument();
  });

  it("surfaces a non-fatal note when the auto-save fails", async () => {
    const user = userEvent.setup();
    mockApi.listPromptTestRuns.mockResolvedValue([summary()]);
    mockApi.createAssistantSession.mockResolvedValue({
      session_id: "ASST-x",
    } as never);
    mockApi.sendAssistantMessage.mockResolvedValue({
      assistant_message: {
        content: '{"verdict":"pass","score":0.9,"reasoning":"ok"}',
      },
    } as never);
    mockApi.savePromptTestRun.mockRejectedValue(new Error("save boom"));

    renderRunner();
    await screen.findByLabelText("Select model");
    await user.click(await screen.findByRole("button", { name: "Replay" }));
    await waitFor(() => expect(mockApi.getPromptTestRun).toHaveBeenCalled());
    await user.click(await screen.findByRole("button", { name: /Run Tests & Judge/i }));

    // Results still render; the save failure is a non-fatal inline note.
    expect(await screen.findByText("save boom")).toBeInTheDocument();
    expect(screen.getByText("Overall Score")).toBeInTheDocument();
  });
});
