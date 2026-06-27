import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { MemoryRouter, Route, Routes } from "react-router-dom";

import type {
  Skill,
  SkillSelectionResult,
  SkillTestRunDetail,
  SkillTestRunSummary,
  SkillWorkspaceResponse,
} from "@/api/types";

// Mock the API client so these tests target the Workspace wiring (open/back,
// first-class Trigger Tests, durable run persistence, set-baseline, diff, bind,
// agent-free calibrate) without driving real network.
vi.mock("@/api/caliberApi", () => ({
  ApiError: class ApiError extends Error {},
  caliberApi: {
    listSkills: vi.fn(),
    getSkill: vi.fn(),
    getMe: vi.fn(),
    getSkillWorkspace: vi.fn(),
    testSkillSelection: vi.fn(),
    saveSkillTestRun: vi.fn(),
    listSkillTestRuns: vi.fn(),
    getSkillTestRun: vi.fn(),
    setSkillBaseline: vi.fn(),
    bindSkill: vi.fn(),
    calibrateSkill: vi.fn(),
    testRenderSkill: vi.fn(),
    listAgents: vi.fn(),
    listWorkflows: vi.fn(),
    updateSkill: vi.fn(),
  },
}));

import { caliberApi } from "@/api/caliberApi";
import { Skills } from "@/pages/Skills";

const mockApi = vi.mocked(caliberApi);
const NOW = "2026-01-01T00:00:00Z";

function makeSkill(overrides: Partial<Skill> = {}): Skill {
  return {
    skill_id: "SK-1",
    name: "doc-summarizer",
    description: "Summarize long documents",
    summary: "Summarize a long document into bullets",
    content: "Summarize {{document}} into bullet points.",
    owner: "@team",
    category: "summarization",
    tags: ["summary"],
    skill_metadata: {},
    allowed_tools: null,
    depends_on: [],
    status: "active",
    version: 3,
    created_at: NOW,
    updated_at: NOW,
    ...overrides,
  };
}

function workspace(overrides: Partial<SkillWorkspaceResponse> = {}): SkillWorkspaceResponse {
  return {
    version: 3,
    category: "summarization",
    status: "active",
    lifecycle: "Tested",
    last_run: null,
    baseline_run_id: null,
    baseline_run: null,
    bound_to: null,
    ...overrides,
  };
}

function selection(overrides: Partial<SkillSelectionResult> = {}): SkillSelectionResult {
  return {
    skill_id: "SK-1",
    skill_name: "doc-summarizer",
    is_selected: true,
    selection_score: 0.82,
    selection_reason: "Matched summarize / document signals.",
    ...overrides,
  };
}

function summary(overrides: Partial<SkillTestRunSummary> = {}): SkillTestRunSummary {
  return {
    test_run_id: "STR-1",
    skill_id: "SK-1",
    skill_version: 3,
    kind: "selection",
    test_set_size: 1,
    passed_count: 1,
    failed_count: 0,
    partial_count: 0,
    overall_score: 1,
    host_agent_id: null,
    trace_id: null,
    mlflow_run_id: null,
    created_by: "@test",
    status: "completed",
    created_at: "2025-01-02T10:00:00Z",
    completed_at: "2025-01-02T10:01:00Z",
    ...overrides,
  };
}

function detail(overrides: Partial<SkillTestRunDetail> = {}): SkillTestRunDetail {
  return {
    ...summary(),
    results: [
      {
        name: "summarize this contract",
        input: { user_message: "summarize this contract" },
        output: { is_selected: true, selection_score: 0.82 },
        error: null,
        verdict: "pass",
        score: 1,
        duration_ms: 3,
        reasoning: "Selected as expected.",
      },
    ],
    ...overrides,
  };
}

function renderSkills(): void {
  render(
    <MemoryRouter
      future={{ v7_startTransition: true, v7_relativeSplatPath: true }}
      initialEntries={["/skills"]}
    >
      <Routes>
        <Route path="/skills" element={<Skills />} />
      </Routes>
    </MemoryRouter>,
  );
}

async function openWorkspace(): Promise<void> {
  await userEvent.click(await screen.findByTestId("skill-open-doc-summarizer"));
  await screen.findByTestId("skill-workspace-header");
}

beforeEach(() => {
  mockApi.listSkills.mockResolvedValue([makeSkill()]);
  mockApi.getSkill.mockResolvedValue(makeSkill());
  mockApi.getMe.mockResolvedValue({ is_admin: true } as never);
  mockApi.getSkillWorkspace.mockResolvedValue(workspace());
  mockApi.testSkillSelection.mockResolvedValue(selection());
  mockApi.saveSkillTestRun.mockResolvedValue(summary());
  mockApi.listSkillTestRuns.mockResolvedValue([]);
  mockApi.getSkillTestRun.mockResolvedValue(detail());
  mockApi.setSkillBaseline.mockResolvedValue({ baseline_run_id: "STR-1" });
  mockApi.bindSkill.mockResolvedValue({ bound_to: { kind: "agent", agent_id: "AG-1" }, status: "Bound" });
  mockApi.calibrateSkill.mockResolvedValue({ item: { item_id: "VI-1" }, job: { job_id: "JOB-1" } });
  mockApi.listAgents.mockResolvedValue([
    {
      agent_id: "AG-1",
      experiment_id: "exp-1",
      name: "Summary Agent",
      owner: "@team",
      artifact_types: ["skill"],
      eval_thresholds: {},
      optimizer_config: {},
      approval_policy: {},
      optimize_for: "quality",
      collaboration_mode: null,
      enabled: true,
      required_approvals: 1,
      created_at: NOW,
      updated_at: NOW,
    },
  ] as never);
  mockApi.listWorkflows.mockResolvedValue([]);
});

afterEach(() => {
  vi.clearAllMocks();
});

describe("SkillWorkspace", () => {
  it("opens a skill into the Workspace with a header + six stage tabs, and back returns", async () => {
    renderSkills();
    await openWorkspace();

    expect(screen.getByTestId("skill-workspace-header")).toHaveTextContent("doc-summarizer");
    expect(screen.getByTestId("skill-workspace-status-badge")).toHaveTextContent("Tested");
    for (const label of [
      "Author",
      "Render Preview",
      "Trigger Tests",
      "Scenario Sets",
      "Runs",
      "Bind",
    ]) {
      expect(screen.getByRole("button", { name: label })).toBeInTheDocument();
    }

    await userEvent.click(screen.getByRole("button", { name: "Back to skills" }));
    expect(await screen.findByTestId("skill-card-SK-1")).toBeInTheDocument();
    expect(screen.queryByTestId("skill-workspace-header")).not.toBeInTheDocument();
  });

  it("never shows an agent picker anywhere in skill testing", async () => {
    renderSkills();
    await openWorkspace();

    // The legacy mandatory agent picker is gone from every skill-testing stage.
    expect(screen.queryByLabelText("Select calibration agent")).not.toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "Trigger Tests" }));
    expect(await screen.findByTestId("skill-trigger-tests")).toBeInTheDocument();
    expect(screen.queryByLabelText("Select calibration agent")).not.toBeInTheDocument();
    expect(screen.queryByText(/select an agent/i)).not.toBeInTheDocument();
  });

  it("Trigger Tests calls testSkillSelection and renders is_selected/score/reason", async () => {
    renderSkills();
    await openWorkspace();
    await userEvent.click(screen.getByRole("button", { name: "Trigger Tests" }));

    await userEvent.type(
      await screen.findByTestId("skill-trigger-message"),
      "Summarize this 40-page report",
    );
    await userEvent.click(screen.getByTestId("skill-trigger-run"));

    await waitFor(() => expect(mockApi.testSkillSelection).toHaveBeenCalled());
    const [skillIdArg, body] = mockApi.testSkillSelection.mock.calls[0]!;
    expect(skillIdArg).toBe("SK-1");
    expect(body.user_message).toBe("Summarize this 40-page report");

    const result = await screen.findByTestId("skill-trigger-result");
    expect(within(result).getByTestId("skill-trigger-selected")).toHaveTextContent("selected");
    expect(result).toHaveTextContent("82%");
    expect(result).toHaveTextContent("Matched summarize / document signals.");
  });

  it("Trigger Tests can save a completed batch as a durable kind:'selection' run", async () => {
    renderSkills();
    await openWorkspace();
    await userEvent.click(screen.getByRole("button", { name: "Trigger Tests" }));

    await userEvent.type(
      await screen.findByTestId("skill-trigger-message"),
      "Summarize this report",
    );
    await userEvent.click(screen.getByTestId("skill-trigger-run"));
    await screen.findByTestId("skill-trigger-result");

    await userEvent.click(screen.getByTestId("skill-trigger-save"));
    await waitFor(() => expect(mockApi.saveSkillTestRun).toHaveBeenCalled());
    const payload = mockApi.saveSkillTestRun.mock.calls[0]![0];
    expect(payload.skill_id).toBe("SK-1");
    expect(payload.kind).toBe("selection");
    expect(payload.results).toHaveLength(1);
    expect(payload.results[0]!.input).toMatchObject({ user_message: "Summarize this report" });
  });

  it("Runs: set-baseline calls setSkillBaseline", async () => {
    mockApi.listSkillTestRuns.mockResolvedValue([summary()]);
    renderSkills();
    await openWorkspace();
    await userEvent.click(screen.getByRole("button", { name: "Runs" }));

    // The latest run auto-views; pin it as baseline.
    await userEvent.click(await screen.findByRole("button", { name: /Set as baseline/i }));
    await waitFor(() =>
      expect(mockApi.setSkillBaseline).toHaveBeenCalledWith("SK-1", "STR-1"),
    );
  });

  it("Runs: a second run renders a baseline diff + regression", async () => {
    // History has two runs; the baseline is the (passing) older one.
    mockApi.listSkillTestRuns.mockResolvedValue([
      summary({ test_run_id: "STR-2", overall_score: 0, passed_count: 0, failed_count: 1 }),
      summary({ test_run_id: "STR-1" }),
    ]);
    mockApi.getSkillWorkspace.mockResolvedValue(workspace({ baseline_run_id: "STR-1" }));
    // The viewed (newest) run now fails the case that passed in the baseline.
    mockApi.getSkillTestRun.mockImplementation(async (id: string) => {
      if (id === "STR-1") return detail({ test_run_id: "STR-1" });
      return detail({
        test_run_id: "STR-2",
        overall_score: 0,
        passed_count: 0,
        failed_count: 1,
        results: [
          {
            name: "summarize this contract",
            input: { user_message: "summarize this contract" },
            output: { is_selected: false, selection_score: 0 },
            error: null,
            verdict: "fail",
            score: 0,
            duration_ms: 2,
            reasoning: "Did not select.",
          },
        ],
      });
    });

    renderSkills();
    await openWorkspace();
    await userEvent.click(screen.getByRole("button", { name: "Runs" }));

    const comparison = await screen.findByTestId("skill-workspace-run-comparison");
    expect(comparison).toBeInTheDocument();
    expect(within(comparison).getByTestId("skill-run-score-delta")).toHaveTextContent("-100%");
    expect(within(comparison).getByText(/1 regression/)).toBeInTheDocument();
  });

  it("Runs: calibrate calls calibrateSkill with NO agent_id", async () => {
    renderSkills();
    await openWorkspace();
    await userEvent.click(screen.getByRole("button", { name: "Runs" }));

    await userEvent.click(await screen.findByTestId("skill-calibrate-btn"));
    await waitFor(() => expect(mockApi.calibrateSkill).toHaveBeenCalled());
    const [skillIdArg, body] = mockApi.calibrateSkill.mock.calls[0]!;
    expect(skillIdArg).toBe("SK-1");
    // The agent-free contract: nothing agent-shaped is sent.
    expect(body ?? {}).not.toHaveProperty("agent_id");
    expect(await screen.findByTestId("skill-calibrate-result")).toHaveTextContent("JOB-1");
  });

  it("Bind: selecting Agent + binding calls bindSkill and header reflects Bound", async () => {
    // After bind, the refetched workspace reports the new binding + lifecycle.
    mockApi.getSkillWorkspace
      .mockResolvedValueOnce(workspace())
      .mockResolvedValue(workspace({ lifecycle: "Bound", bound_to: { kind: "agent", agent_id: "AG-1" } }));

    renderSkills();
    await openWorkspace();
    await userEvent.click(screen.getByRole("button", { name: "Bind" }));

    // Agent is the default kind; pick the loaded agent and bind.
    await screen.findByLabelText("Select agent to bind");
    await userEvent.click(screen.getByRole("button", { name: "Bind skill" }));

    await waitFor(() => expect(mockApi.bindSkill).toHaveBeenCalled());
    const [skillIdArg, payload] = mockApi.bindSkill.mock.calls[0]!;
    expect(skillIdArg).toBe("SK-1");
    expect(payload).toMatchObject({ kind: "agent", agent_id: "AG-1" });

    // Header lifecycle flips to Bound and the current-binding panel shows it.
    await waitFor(() =>
      expect(screen.getByTestId("skill-workspace-status-badge")).toHaveTextContent("Bound"),
    );
    expect(screen.getByTestId("skill-workspace-bound-to")).toHaveTextContent("AG-1");
  });

  it("Scenario Sets builds cases that Trigger Tests runs in a batch", async () => {
    renderSkills();
    await openWorkspace();

    // Build one scenario.
    await userEvent.click(screen.getByRole("button", { name: "Scenario Sets" }));
    await userEvent.type(
      await screen.findByLabelText("Scenario user message"),
      "Summarize this filing",
    );
    await userEvent.click(screen.getByTestId("skill-scenario-add"));
    expect(await screen.findByTestId("skill-scenario-case")).toHaveTextContent("Summarize this filing");

    // Trigger Tests can run the whole scenario batch via testSkillSelection.
    // Scope to the tablist — the Scenario Sets banner also renders a "Trigger
    // Tests" link to this stage.
    const tablist = screen.getByRole("tablist");
    await userEvent.click(within(tablist).getByRole("button", { name: "Trigger Tests" }));
    await userEvent.click(await screen.findByRole("button", { name: /Run 1 scenario/i }));
    await waitFor(() => expect(mockApi.testSkillSelection).toHaveBeenCalled());
    expect(await screen.findByTestId("skill-trigger-result")).toHaveTextContent("selected");
  });
});
