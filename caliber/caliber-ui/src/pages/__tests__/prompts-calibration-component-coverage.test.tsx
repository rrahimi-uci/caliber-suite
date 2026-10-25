import { http, HttpResponse } from "msw";
import { fireEvent } from "@testing-library/react";
import { afterAll, afterEach, beforeAll, describe, expect, it, vi } from "vitest";

import type { PromptInfo } from "@/api/types";
import {
  CalibrationApplyReviewDialog,
  PromptCalibrationTab,
  PromptOptimizationTab,
  PromptRunsStage,
} from "@/pages/Prompts";
import { capabilitiesBody } from "@/test/handlers";
import { render, screen, userEvent, waitFor, within } from "@/test/utils";
import { server } from "@/test/server";

const API_BASE = "/ajax-api/2.0/mlflow/caliber";

const supportPrompt: PromptInfo = {
  agent_id: "support-agent",
  agent_name: "Support Agent",
  agent_enabled: true,
  prompt_name: "support-agent",
  version: 3,
  alias: "prod",
  available_aliases: ["prod", "staging"],
  template_preview: "Answer support questions.",
  template_length: 28,
  approval_id: null,
  artifact_ref: "prompts:/support-agent@prod",
  has_prompt: true,
  needs_prompt: false,
  source: "both",
};

const draftPrompt: PromptInfo = {
  ...supportPrompt,
  agent_id: "draft-agent",
  agent_name: "Draft Agent",
  prompt_name: "draft-agent",
  version: null,
  alias: null,
  available_aliases: [],
  has_prompt: false,
  needs_prompt: true,
  source: "caliber",
};

beforeAll(() => server.listen({ onUnhandledRequest: "error" }));
afterEach(() => {
  server.resetHandlers();
  window.localStorage.clear();
});
afterAll(() => server.close());

describe("PromptCalibrationTab component states", () => {
  it("uses an uncontrolled shared prompt scope and switches prompt and alias", async () => {
    const user = userEvent.setup();
    server.use(
      http.get(`${API_BASE}/capabilities`, () =>
        HttpResponse.json(capabilitiesBody({ aria: true })),
      ),
    );

    render(
      <PromptCalibrationTab
        prompts={[draftPrompt, supportPrompt]}
        loading={false}
      />,
    );

    const promptPicker = await screen.findByRole("combobox", {
      name: "Select a prompt",
    });
    expect(promptPicker).toHaveValue("support-agent");
    await user.selectOptions(promptPicker, "draft-agent");
    expect(promptPicker).toHaveValue("draft-agent");

    await waitFor(() => {
      expect(screen.getByRole("combobox", { name: "Select calibration alias" })).toHaveValue(
        "prod",
      );
    });
  });

  it("honors controlled prompt and alias values and reports changes", async () => {
    const user = userEvent.setup();
    const onAgentChange = vi.fn();
    const onPromptAliasChange = vi.fn();
    server.use(
      http.get(`${API_BASE}/capabilities`, () =>
        HttpResponse.json(capabilitiesBody({ aria: true })),
      ),
    );

    render(
      <PromptCalibrationTab
        prompts={[supportPrompt]}
        loading={false}
        agentId="support-agent"
        onAgentChange={onAgentChange}
        promptAlias="prod"
        onPromptAliasChange={onPromptAliasChange}
      />,
    );

    const promptPicker = await screen.findByRole("combobox", {
      name: "Select a prompt",
    });
    await user.selectOptions(promptPicker, "support-agent");
    await user.selectOptions(
      screen.getByRole("combobox", { name: "Select calibration alias" }),
      "staging",
    );
    expect(onAgentChange).toHaveBeenCalledWith("support-agent");
    expect(onPromptAliasChange).toHaveBeenCalledWith("staging");
  });

  it("renders the empty shared scope while Aria is enabled", async () => {
    server.use(
      http.get(`${API_BASE}/capabilities`, () =>
        HttpResponse.json(capabilitiesBody({ aria: true })),
      ),
    );

    render(<PromptCalibrationTab prompts={[]} loading={false} />);
    expect(await screen.findByText("No prompts to test yet.")).toBeInTheDocument();
  });

  it("builds assistant intent context across enabled, unavailable, empty, and invalid scorer configs", async () => {
    let resolvePayload: Record<string, unknown> | null = null;
    server.use(
      http.get(`${API_BASE}/capabilities`, () =>
        HttpResponse.json(capabilitiesBody({ aria: true })),
      ),
      http.get(`${API_BASE}/prompts/calibration/options`, () =>
        HttpResponse.json(
          {
            data: {
              optimizers: ["MetaPrompt"],
              default_optimizer: "MetaPrompt",
              scorers: [
                {
                  name: "json-scorer",
                  label: "JSON scorer",
                  description: "Structured scorer",
                  requires_config: true,
                  provider: "mlflow",
                  category: "core",
                  available: true,
                  unavailable_reason: null,
                  install_command: null,
                  config_template: { guidelines: ["Be precise."] },
                },
                {
                  name: "array-scorer",
                  label: "Array scorer",
                  description: "Array config edge case",
                  requires_config: true,
                  provider: "mlflow",
                  category: "quality",
                  available: true,
                  unavailable_reason: null,
                  install_command: null,
                  config_template: null,
                },
                {
                  name: "invalid-scorer",
                  label: "Invalid scorer",
                  description: "Invalid config edge case",
                  requires_config: true,
                  provider: "mlflow",
                  category: "quality",
                  available: true,
                  unavailable_reason: null,
                  install_command: null,
                  config_template: null,
                },
                {
                  name: "unavailable-scorer",
                  label: "Unavailable scorer",
                  description: "Disabled dependency",
                  requires_config: false,
                  provider: "mlflow",
                  category: "core",
                  available: false,
                  unavailable_reason: "Install the provider first.",
                  install_command: "pip install provider",
                  config_template: null,
                },
                {
                  name: "disabled-scorer",
                  label: "Disabled scorer",
                  description: "Optional scorer",
                  requires_config: false,
                  provider: "mlflow",
                  category: "core",
                  available: true,
                  unavailable_reason: null,
                  install_command: null,
                  config_template: null,
                },
              ],
              default_scorers: ["json-scorer", "array-scorer", "invalid-scorer"],
              default_gate: {
                min_aggregate_score: 0.85,
                max_regression_delta: 0.02,
              },
            },
          },
        ),
      ),
      http.get(`${API_BASE}/eval-datasets`, () => HttpResponse.json({ data: [] })),
      http.get(`${API_BASE}/jobs`, () => HttpResponse.json({ data: [] })),
      http.post(`${API_BASE}/assistant/sessions`, () =>
        HttpResponse.json(
          {
            data: {
              session_id: "asst-config-coverage",
              title: "Prompt calibration workbench",
              goal: "Intent planning",
              status: "active",
              metadata_: {},
              created_at: "2026-01-01T00:00:00Z",
              updated_at: "2026-01-01T00:00:00Z",
            },
          },
          { status: 201 },
        ),
      ),
      http.get(`${API_BASE}/assistant/sessions/:sessionId/plans/latest`, () =>
        HttpResponse.json({ detail: "not found" }, { status: 404 }),
      ),
      http.post(
        `${API_BASE}/assistant/sessions/:sessionId/intent/resolve`,
        async ({ request }) => {
          resolvePayload = (await request.json()) as Record<string, unknown>;
          return HttpResponse.json({
            data: {
              mode: "intent_plan",
              intent: {
                name: "run_prompt_optimization",
                confidence: 0.9,
                rationale: "Coverage context",
              },
              alternatives: [],
              slots: [],
              assumptions: [],
              questions: [],
              evidence: [],
            },
          });
        },
      ),
    );

    const user = userEvent.setup();
    render(
      <PromptOptimizationTab prompts={[supportPrompt]} loading={false} />,
    );
    await screen.findByText("Run Configuration");

    const configTextareas = screen.getAllByPlaceholderText(
      '{"guidelines": ["Do not hallucinate."]}',
    );
    fireEvent.change(configTextareas[1]!, { target: { value: "[]" } });
    fireEvent.change(configTextareas[2]!, { target: { value: "{invalid" } });
    await user.type(
      screen.getByLabelText("Assistant intent request"),
      "Compare scorer configuration handling",
    );
    await user.click(screen.getByRole("button", { name: "Analyze Intent" }));

    expect(await screen.findByText("run_prompt_optimization")).toBeInTheDocument();
    const context = (resolvePayload?.context ?? {}) as {
      scorers?: Array<Record<string, unknown>>;
    };
    expect(context.scorers).toEqual([
      { name: "json-scorer", weight: 1, config: { guidelines: ["Be precise."] } },
      { name: "array-scorer", weight: 1 },
      { name: "invalid-scorer", weight: 1 },
    ]);
  });

  it("covers optimization loading, plan validation, and scorer validation states", async () => {
    const user = userEvent.setup();
    server.use(
      http.get(`${API_BASE}/capabilities`, () =>
        HttpResponse.json(capabilitiesBody({ aria: true })),
      ),
      http.get(`${API_BASE}/prompts/calibration/options`, () =>
        HttpResponse.json({
          data: {
            optimizers: ["MetaPrompt"],
            default_optimizer: "MetaPrompt",
            scorers: [
              {
                name: "helpfulness",
                label: "Helpfulness",
                description: "Checks answer quality.",
                requires_config: false,
                provider: null,
                category: null,
                available: true,
                unavailable_reason: null,
                install_command: null,
                config_template: null,
              },
            ],
            default_scorers: ["helpfulness"],
            default_gate: {
              min_aggregate_score: 0.85,
              max_regression_delta: 0.02,
            },
          },
        }),
      ),
      http.get(`${API_BASE}/eval-datasets`, () =>
        HttpResponse.json({
          data: [
            {
              dataset_id: "dataset-1",
              name: "Support examples",
              description: "Examples",
              owner: "@test",
              tags: [],
              status: "active",
              version: 1,
              created_at: "2026-01-01T00:00:00Z",
              updated_at: "2026-01-01T00:00:00Z",
            },
          ],
        }),
      ),
      http.get(`${API_BASE}/jobs`, () => HttpResponse.json({ data: [] })),
      http.post(`${API_BASE}/assistant/sessions`, () =>
        HttpResponse.json(
          {
            data: {
              session_id: "session-coverage",
              title: "Prompt calibration workbench",
              goal: "Coverage",
              status: "active",
              metadata_: {},
              created_at: "2026-01-01T00:00:00Z",
              updated_at: "2026-01-01T00:00:00Z",
            },
          },
          { status: 201 },
        ),
      ),
      http.get(`${API_BASE}/assistant/sessions/:sessionId/plans/latest`, () =>
        HttpResponse.json({ detail: "not found" }, { status: 404 }),
      ),
      http.post(`${API_BASE}/assistant/sessions/:sessionId/plans`, () =>
        HttpResponse.json({
          data: {
            mode: "intent_plan",
            plan_id: "plan-needs-input",
            intent: {
              name: "run_prompt_optimization",
              confidence: 0.5,
              rationale: "More information is required.",
            },
            actions: [],
            slots: [],
            missing_slots: ["eval_dataset_id"],
            assumptions: [],
            questions: ["Which dataset should be used?"],
            ready: false,
            requires_confirmation: false,
          },
        }),
      ),
    );

    render(
      <PromptOptimizationTab prompts={[supportPrompt]} loading={false} />,
    );
    await screen.findByText("Run Configuration");

    await user.click(screen.getByRole("button", { name: "Analyze Intent" }));
    expect(
      await screen.findByText("Describe what you want to accomplish first."),
    ).toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: "Build Plan" }));
    expect(await screen.findByText("plan-needs-input")).toBeInTheDocument();
    expect(screen.getByText("Status needs input")).toBeInTheDocument();
    expect(screen.getByText("Missing: eval_dataset_id")).toBeInTheDocument();

    const weight = screen.getByLabelText("helpfulness weight");
    await user.clear(weight);
    await user.type(weight, "0");
    await user.click(screen.getByRole("button", { name: "Start Calibration Run" }));
    expect(
      await screen.findByText("Scorer helpfulness must have a positive weight."),
    ).toBeInTheDocument();
  });

  it("covers optimization loading and empty prompt scopes", async () => {
    server.use(
      http.get(`${API_BASE}/capabilities`, () =>
        HttpResponse.json(capabilitiesBody({ aria: false })),
      ),
      http.get(`${API_BASE}/prompts/calibration/options`, () =>
        HttpResponse.json({ detail: "calibration service unavailable" }, { status: 503 }),
      ),
      http.get(`${API_BASE}/eval-datasets`, () =>
        HttpResponse.json({ detail: "datasets unavailable" }, { status: 503 }),
      ),
      http.get(`${API_BASE}/jobs`, () => HttpResponse.json({ data: [] })),
    );

    const { unmount } = render(
      <PromptOptimizationTab prompts={[]} loading={true} />,
    );
    expect(screen.getByText("Loading prompts…")).toBeInTheDocument();
    unmount();

    render(<PromptOptimizationTab prompts={[]} loading={false} />);
    expect(await screen.findByText("No prompts to calibrate yet.")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Analyze Intent" })).not.toBeInTheDocument();
  });

  it("renders persisted optimization jobs, scorer variants, and applies a candidate", async () => {
    const user = userEvent.setup();
    const candidateJob = {
      job_id: "job-persisted-candidate",
      agent_id: "support-agent",
      artifact_type: "prompt",
      status: "candidate_ready",
      current_stage: "candidate",
      optimizer_type: "MIPROv2",
      created_at: "2026-01-02T00:00:00Z",
      updated_at: "2026-01-02T00:00:00Z",
      error_message: "Previous warning retained for review",
      gepa_progress: { iterations: 4, latest_score: 0.91 },
      eval_results: {
        candidate: { overall: 0.91, dimensions: { quality: 0.92 } },
        baseline: { overall: 0.84, dimensions: { quality: 0.8 } },
        deltas: { overall: 0.07 },
      },
      candidate: {
        baseline_content: "Answer briefly.",
        content: "Answer clearly with evidence.",
        rationale: "Improves groundedness.",
        diff_summary: "Adds evidence requirements.",
      },
    };
    const applyCalls: string[] = [];
    server.use(
      http.get(`${API_BASE}/capabilities`, () =>
        HttpResponse.json(capabilitiesBody({ aria: false })),
      ),
      http.get(`${API_BASE}/prompts/calibration/options`, () =>
        HttpResponse.json({
          data: {
            optimizers: ["MetaPrompt", "MIPROv2"],
            default_optimizer: "MIPROv2",
            scorers: [
              {
                name: "faithfulness",
                label: "Faithfulness",
                description: "Checks grounded claims.",
                requires_config: true,
                provider: "deepeval",
                category: "quality",
                available: true,
                unavailable_reason: null,
                install_command: null,
                config_template: null,
              },
              {
                name: "optional",
                label: "Optional scorer",
                description: "Not enabled by default.",
                requires_config: false,
                provider: null,
                category: null,
                available: true,
                unavailable_reason: null,
                install_command: null,
                config_template: null,
              },
              {
                name: "missing-provider",
                label: "Missing provider",
                description: "Unavailable dependency.",
                requires_config: false,
                provider: "deepeval",
                category: "quality",
                available: false,
                unavailable_reason: null,
                install_command: null,
                config_template: null,
              },
            ],
            default_scorers: ["faithfulness"],
            default_gate: {
              min_aggregate_score: 0.8,
              max_regression_delta: 0.04,
            },
            runtime: {
              deepeval: {
                available: false,
                reason: null,
                install_command: "pip install deepeval",
              },
            },
          },
        }),
      ),
      http.get(`${API_BASE}/eval-datasets`, () =>
        HttpResponse.json({
          data: [
            {
              dataset_id: "dataset-persisted",
              name: "Persisted support set",
              description: "Support examples",
              owner: "@test",
              tags: [],
              status: "active",
              version: 7,
              created_at: "2026-01-01T00:00:00Z",
              updated_at: "2026-01-01T00:00:00Z",
            },
          ],
        }),
      ),
      http.get(`${API_BASE}/jobs`, () =>
        HttpResponse.json({ data: [candidateJob] }),
      ),
      http.post(`${API_BASE}/jobs/:jobId/apply`, ({ params }) => {
        applyCalls.push(String(params.jobId));
        return HttpResponse.json({
          data: {
            simulated: true,
            promotion: { artifact_ref: "prompts:/support-agent@prod" },
          },
        });
      }),
    );

    render(<PromptOptimizationTab prompts={[supportPrompt]} loading={false} />);
    expect(await screen.findByText("DeepEval runtime: Not installed")).toBeInTheDocument();
    expect(screen.getByText("pip install deepeval")).toBeInTheDocument();
    expect(screen.getAllByText("MIPROv2").length).toBeGreaterThanOrEqual(1);
    expect(screen.getAllByText("job-persisted-candidate").length).toBeGreaterThanOrEqual(1);
    expect(screen.getByTestId("job-gepa-progress")).toHaveTextContent(
      "GEPA optimizing",
    );
    expect(screen.getByText("Previous warning retained for review")).toBeInTheDocument();
    expect(screen.getByText("0.91")).toBeInTheDocument();

    const faithfulness = screen.getByRole("checkbox", { name: "Faithfulness" });
    expect(faithfulness).toBeChecked();
    expect(screen.getByRole("checkbox", { name: "Missing provider" })).toBeDisabled();
    expect(screen.getByText("Scorer dependency is unavailable.")).toBeInTheDocument();
    const config = screen.getByPlaceholderText('{"guidelines": ["Do not hallucinate."]}');
    await user.clear(config);
    fireEvent.change(config, { target: { value: '{"guidelines":["Use citations"]}' } });
    await user.click(screen.getByRole("checkbox", { name: "Optional scorer" }));
    await user.clear(screen.getByLabelText("Calibration run notes"));
    await user.type(screen.getByLabelText("Calibration run notes"), "Review persisted candidate");

    const applyButtons = screen.getAllByTestId("job-apply-btn");
    expect(applyButtons.length).toBeGreaterThanOrEqual(1);
    await user.click(applyButtons[0]!);
    expect(await screen.findByText("Gate: Not recorded")).toBeInTheDocument();
    expect(screen.getByText("Improves groundedness.")).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Apply candidate live" }));
    expect(await screen.findByRole("status")).toHaveTextContent("Simulated apply");
    expect(applyCalls).toEqual(["job-persisted-candidate"]);
  });

  it("applies assistant slot fallbacks and renders populated operation results", async () => {
    const user = userEvent.setup();
    const warnSpy = vi.spyOn(console, "warn").mockImplementation(() => {});
    server.use(
      http.get(`${API_BASE}/capabilities`, () =>
        HttpResponse.json(capabilitiesBody({ aria: true })),
      ),
      http.get(`${API_BASE}/prompts/calibration/options`, () =>
        HttpResponse.json({
          data: {
            optimizers: ["MetaPrompt", "MIPROv2"],
            default_optimizer: "MetaPrompt",
            scorers: [
              {
                name: "rubric",
                label: "Rubric",
                description: "Structured rubric scorer.",
                requires_config: true,
                provider: "mlflow",
                category: "quality",
                available: true,
                unavailable_reason: null,
                install_command: null,
                config_template: { guidelines: ["Be precise."] },
              },
              {
                name: "plain",
                label: "Plain",
                description: "No configuration required.",
                requires_config: false,
                provider: null,
                category: null,
                available: true,
                unavailable_reason: null,
                install_command: null,
                config_template: null,
              },
              {
                name: "disabled",
                label: "Disabled",
                description: "Unavailable dependency.",
                requires_config: false,
                provider: "optional",
                category: "quality",
                available: false,
                unavailable_reason: "Install the optional provider.",
                install_command: null,
                config_template: null,
              },
            ],
            default_scorers: ["rubric"],
            default_gate: {
              min_aggregate_score: 0.85,
              max_regression_delta: 0.02,
            },
          },
        }),
      ),
      http.get(`${API_BASE}/eval-datasets`, () =>
        HttpResponse.json({
          data: [
            {
              dataset_id: "dataset-slot",
              name: "Slot Dataset",
              description: "",
              owner: "@test",
              tags: [],
              status: "active",
              version: 2,
              created_at: "2026-01-01T00:00:00Z",
              updated_at: "2026-01-01T00:00:00Z",
            },
          ],
        }),
      ),
      http.get(`${API_BASE}/jobs`, () => HttpResponse.json({ data: [] })),
      http.post(`${API_BASE}/assistant/sessions`, () =>
        HttpResponse.json(
          {
            data: {
              session_id: "assistant-slot-matrix",
              title: "Prompt calibration workbench",
              goal: "Coverage",
              status: "active",
              metadata_: {},
              created_at: "2026-01-01T00:00:00Z",
              updated_at: "2026-01-01T00:00:00Z",
            },
          },
          { status: 201 },
        ),
      ),
      http.get(`${API_BASE}/assistant/sessions/:sessionId/plans/latest`, () =>
        HttpResponse.json({ detail: "not found" }, { status: 404 }),
      ),
      http.post(
        `${API_BASE}/assistant/sessions/:sessionId/intent/resolve`,
        () =>
          HttpResponse.json({
            data: {
              mode: "intent_plan",
              intent: {
                name: "run_prompt_optimization",
                confidence: 0.8,
                rationale: "Coverage matrix",
              },
              alternatives: [],
              slots: [],
              assumptions: [],
              questions: ["Which scorer should be used?"],
              evidence: [],
            },
          }),
      ),
      http.post(`${API_BASE}/assistant/sessions/:sessionId/plans`, () =>
        HttpResponse.json({
          data: {
            mode: "intent_plan",
            plan_id: "plan-slot-matrix",
            intent: {
              name: "run_prompt_optimization",
              confidence: 0.9,
              rationale: "Slots cover all optional form values.",
            },
            actions: [],
            slots: [
              { name: "prompt_name", value: "support-agent" },
              { name: "eval_dataset_id", value: "dataset-slot" },
              { name: "prompt_alias", value: "staging" },
              { name: "optimizer_type", value: "MIPROv2" },
              { name: "gate.min_aggregate_score", value: 0.91 },
              { name: "gate.max_regression_delta", value: 0.01 },
              { name: "notes", value: "assistant notes" },
              {
                name: "scorers",
                value: [
                  "rubric",
                  { name: "plain", weight: 0, config: [] },
                  null,
                  { name: "disabled", weight: 2, config: { mode: "strict" } },
                  { weight: 2 },
                ],
              },
            ],
            missing_slots: [],
            assumptions: [],
            questions: [],
            ready: true,
            requires_confirmation: true,
          },
        }),
      ),
      http.post(
        `${API_BASE}/assistant/sessions/:sessionId/plans/execute`,
        () =>
          HttpResponse.json({
            data: {
              operation_id: "operation-slot-matrix",
              plan_id: "plan-slot-matrix",
              intent_name: "run_prompt_optimization",
              status: "completed",
              executed_action: "run_prompt_optimization",
              result: {
                result_type: "optimization_run",
                status: "completed",
                summary: "Completed with review warnings.",
                warnings: ["Provider used a fallback evaluator."],
                trace_id: "trace-slot-matrix",
                correlation_id: "corr-slot-matrix",
                job: { job_id: "malformed-job" },
              },
              run: null,
            },
          }),
      ),
    );

    render(<PromptOptimizationTab prompts={[supportPrompt]} loading={false} />);
    await screen.findByText("Run Configuration");

    await user.click(screen.getByRole("button", { name: "Analyze Intent" }));
    expect(
      await screen.findByText("Describe what you want to accomplish first."),
    ).toBeInTheDocument();
    await user.type(
      screen.getByLabelText("Assistant intent request"),
      "Cover every prompt calibration slot.",
    );
    await user.click(screen.getByRole("button", { name: "Analyze Intent" }));
    expect(await screen.findByText(/Questions: Which scorer/)).toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: "Build Plan" }));
    expect(await screen.findByText("plan-slot-matrix")).toBeInTheDocument();
    expect(screen.getByLabelText("Calibration prompt alias")).toHaveValue("staging");
    expect(screen.getByLabelText("Calibration strategy")).toHaveValue("MIPROv2");
    expect(screen.getByLabelText("Minimum aggregate score")).toHaveValue("0.91");
    expect(screen.getByLabelText("Maximum regression delta")).toHaveValue("0.01");
    expect(screen.getByLabelText("Calibration run notes")).toHaveValue("assistant notes");

    await user.click(screen.getByRole("button", { name: "Execute Confirmed Plan" }));
    expect(await screen.findByText("operation-slot-matrix")).toBeInTheDocument();
    expect(await screen.findByText("Completed with review warnings.")).toBeInTheDocument();
    expect(screen.getByText("Provider used a fallback evaluator.")).toBeInTheDocument();
    expect(screen.getByText("trace-slot-matrix")).toBeInTheDocument();
    expect(screen.getByText("corr-slot-matrix")).toBeInTheDocument();
    expect(warnSpy).toHaveBeenCalledWith(
      "caliber: assistant-intent execution returned a malformed refinement job",
      { job_id: "malformed-job" },
    );
    warnSpy.mockRestore();
  });

  it("reports upload validation failures and calibration start failures", async () => {
    const user = userEvent.setup();
    server.use(
      http.get(`${API_BASE}/capabilities`, () =>
        HttpResponse.json(capabilitiesBody({ aria: false })),
      ),
      http.get(`${API_BASE}/prompts/calibration/options`, () =>
        HttpResponse.json({
          data: {
            optimizers: ["MetaPrompt"],
            default_optimizer: "MetaPrompt",
            scorers: [
              {
                name: "helpfulness",
                label: "Helpfulness",
                description: "Checks quality.",
                requires_config: false,
                provider: null,
                category: null,
                available: true,
                unavailable_reason: null,
                install_command: null,
                config_template: null,
              },
            ],
            default_scorers: ["helpfulness"],
            default_gate: {
              min_aggregate_score: 0.85,
              max_regression_delta: 0.02,
            },
          },
        }),
      ),
      http.get(`${API_BASE}/eval-datasets`, () => HttpResponse.json({ data: [] })),
      http.get(`${API_BASE}/jobs`, () => HttpResponse.json({ data: [] })),
      http.post(`${API_BASE}/prompts/calibration/runs`, () =>
        HttpResponse.json({ detail: "calibration queue unavailable" }, { status: 503 }),
      ),
    );

    render(<PromptOptimizationTab prompts={[supportPrompt]} loading={false} />);
    await screen.findByText("Run Configuration");

    await user.click(screen.getByRole("button", { name: "Upload Dataset" }));
    expect(await screen.findByText("Select a dataset file first.")).toBeInTheDocument();

    const invalidFile = new File(["\n\n"], "empty.jsonl", {
      type: "application/x-ndjson",
    });
    await user.upload(screen.getByLabelText("Upload calibration dataset"), invalidFile);
    const datasetName = screen.getByPlaceholderText("Dataset name");
    await user.clear(datasetName);
    await user.click(screen.getByRole("button", { name: "Upload Dataset" }));
    expect(await screen.findByText("Dataset name is required.")).toBeInTheDocument();

    await user.type(datasetName, "empty-upload");
    await user.click(screen.getByRole("button", { name: "Upload Dataset" }));
    expect(
      await screen.findByText("Uploaded file did not contain any valid examples."),
    ).toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: "Start Calibration Run" }));
    expect(await screen.findByText("Please select or upload an eval dataset.")).toBeInTheDocument();
  });

  it("renders the complete calibration apply review, including scores, diff, and actions", async () => {
    const user = userEvent.setup();
    const onCancel = vi.fn();
    const onApply = vi.fn();

    render(
      <CalibrationApplyReviewDialog
        run={
          {
            job_id: "job-rich-review",
            candidate: {
              baseline_content: "Answer briefly.\nUse citations.",
              content: "Answer clearly.\nUse citations.\nAsk one follow-up.",
              rationale: "The candidate is more precise.",
              diff_summary: "Improves clarity and follow-up behavior.",
            },
            eval_results: {
              candidate: { overall: 0.91, dimensions: { quality: 0.95, safety: 0.88 } },
              baseline: { overall: 0.82, dimensions: { quality: 0.8, safety: 0.88 } },
              deltas: { overall: 0.09 },
              gate: { passed: true, reasons: [] },
              n_examples: 12,
              eval_dataset_id: "support-v2",
              provider: "mlflow",
            },
          } as never
        }
        alias="staging"
        busy={false}
        error={null}
        onCancel={onCancel}
        onApply={onApply}
      />,
    );

    expect(screen.getByText("Gate: Passed")).toBeInTheDocument();
    expect(screen.getByText("12 evaluation examples · support-v2")).toBeInTheDocument();
    expect(screen.getByText("Improves clarity and follow-up behavior.")).toBeInTheDocument();
    expect(screen.getByText("Optimizer rationale:")).toBeInTheDocument();
    expect(screen.getByText("quality")).toBeInTheDocument();
    expect(screen.getByText("+9.0 pts")).toBeInTheDocument();
    expect(screen.getByTestId("calibration-prompt-diff")).toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: "Apply candidate live" }));
    await user.click(screen.getByRole("button", { name: "Cancel" }));
    expect(onApply).toHaveBeenCalledTimes(1);
    expect(onCancel).toHaveBeenCalledTimes(1);
  });

  it("renders unavailable and blocked review states without enabling apply", async () => {
    const user = userEvent.setup();
    const onCancel = vi.fn();
    const onApply = vi.fn();

    render(
      <CalibrationApplyReviewDialog
        run={
          {
            job_id: "job-blocked-review",
            candidate: { diff_summary: "Candidate artifact was not retained." },
            eval_results: {
              gate: { passed: false, reasons: ["Safety regressed", "Needs review"] },
              simulated: true,
              provider: "fake-evaluator",
            },
          } as never
        }
        alias="prod"
        busy={true}
        error="publish rejected"
        onCancel={onCancel}
        onApply={onApply}
      />,
    );

    expect(screen.getByText("Gate: Failed")).toBeInTheDocument();
    expect(screen.getByTestId("calibration-simulated-evaluation")).toHaveTextContent(
      "fake-evaluator",
    );
    expect(screen.getByText(/Candidate prompt content is unavailable/)).toBeInTheDocument();
    expect(screen.getByText(/No baseline snapshot was recorded/)).toBeInTheDocument();
    expect(screen.getByText("Safety regressed")).toBeInTheDocument();
    expect(screen.getByRole("alert")).toHaveTextContent("publish rejected");

    const apply = screen.getByRole("button", { name: "Applying…" });
    expect(apply).toBeDisabled();
    expect(screen.getByRole("button", { name: "Cancel" })).toBeDisabled();
    await user.click(screen.getByRole("button", { name: "Close apply review" }));
    expect(onCancel).not.toHaveBeenCalled();
    expect(onApply).not.toHaveBeenCalled();
  });

  it("covers an unrecorded gate, missing baseline, and cancel/close behavior", async () => {
    const user = userEvent.setup();
    const onCancel = vi.fn();

    render(
      <CalibrationApplyReviewDialog
        run={
          {
            job_id: "job-new-content",
            candidate: { content: "A brand new prompt." },
            eval_results: {
              candidate: { overall: 0.7, dimensions: { quality: 0.7 } },
              deltas: {},
            },
          } as never
        }
        alias="prod"
        busy={false}
        error={null}
        onCancel={onCancel}
        onApply={vi.fn()}
      />,
    );

    expect(screen.getByText("Gate: Not recorded")).toBeInTheDocument();
    expect(screen.getByText(/No baseline snapshot was recorded/)).toBeInTheDocument();
    expect(screen.getByText("Evaluation score")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Apply candidate live" })).toBeEnabled();
    await user.click(screen.getByRole("button", { name: "Close apply review" }));
    expect(onCancel).toHaveBeenCalledTimes(1);
  });
});

describe("PromptRunsStage comparison and failure states", () => {
  const summary = (id: string, score: number | null) => ({
    test_run_id: id,
    agent_id: "support-agent",
    prompt_name: "support-agent",
    prompt_alias: "prod",
    prompt_version: 3,
    model: "gpt-4o-mini",
    eval_dataset_id: null,
    test_set_size: 2,
    passed_count: score === 1 ? 2 : 1,
    failed_count: score === 0 ? 1 : 0,
    partial_count: score !== null && score !== 1 && score !== 0 ? 1 : 0,
    overall_score: score,
    trace_id: null,
    mlflow_run_id: null,
    created_by: "@test",
    status: "completed",
    created_at: id === "current" ? "2025-02-02T00:00:00Z" : "2025-02-01T00:00:00Z",
    completed_at: null,
  });

  const detail = (
    id: string,
    score: number,
    results: Array<Record<string, unknown>>,
  ) => ({ ...summary(id, score), results });

  function renderRuns(
    workspace: Record<string, unknown> | null,
    prompt: PromptInfo = supportPrompt,
  ) {
    render(
      <PromptRunsStage
        prompt={prompt}
        workspace={workspace as never}
        onAfterRun={vi.fn()}
      />,
    );
  }

  it("renders the disabled Aria notice and the empty history copy", async () => {
    server.use(
      http.get(`${API_BASE}/capabilities`, () =>
        HttpResponse.json(capabilitiesBody({ aria: false })),
      ),
      http.get(`${API_BASE}/prompts/test-runs`, () =>
        HttpResponse.json({ data: [] }),
      ),
    );

    renderRuns(null);

    expect(
      await screen.findByTestId("feature-disabled-notice"),
    ).toHaveTextContent("Running a test set against this prompt needs");
    expect(await screen.findByText("No saved runs yet.")).toBeInTheDocument();
  });

  it("aligns comparison cases by input, renders missing outputs, and reports a positive delta", async () => {
    server.use(
      http.get(`${API_BASE}/capabilities`, () =>
        HttpResponse.json(capabilitiesBody({ aria: true })),
      ),
      http.get(`${API_BASE}/prompts/support-agent`, () =>
        HttpResponse.json({ data: { template: "Answer: {{input}}" } }),
      ),
      http.get(`${API_BASE}/prompts/test-runs`, () =>
        HttpResponse.json({
          data: [summary("current", 1), summary("baseline", 0.5)],
        }),
      ),
      http.get(`${API_BASE}/prompts/test-runs/current`, () =>
        HttpResponse.json({
          data: detail("current", 1, [
            {
              testCaseId: "new-case",
              input: "new input",
              expectedBehavior: "new expected",
              actualResponse: "",
              verdict: "pass",
              score: 1,
              reasoning: "new",
            },
            {
              testCaseId: "current-id",
              input: "shared input",
              expectedBehavior: "shared expected",
              actualResponse: "current output",
              verdict: "pass",
              score: 1,
              reasoning: "shared",
            },
          ]),
        }),
      ),
      http.get(`${API_BASE}/prompts/test-runs/baseline`, () =>
        HttpResponse.json({
          data: detail("baseline", 0.5, [
            {
              testCaseId: "base-id",
              input: "shared input",
              expectedBehavior: "shared expected",
              actualResponse: "baseline output",
              verdict: "partial",
              score: 0.5,
              reasoning: "partial",
            },
            {
              testCaseId: "old-only",
              input: "old input",
              expectedBehavior: "old expected",
              actualResponse: "old output",
              verdict: "fail",
              score: 0,
              reasoning: "old",
            },
          ]),
        }),
      ),
    );

    renderRuns({ version: 3, baseline_run_id: "baseline" });

    const comparison = await screen.findByTestId("workspace-run-comparison");
    expect(within(comparison).getByTestId("run-score-delta")).toHaveTextContent(
      "+75%",
    );
    expect(within(comparison).getByTestId("run-regressions")).toHaveTextContent(
      "No regressions",
    );
    expect(comparison).toHaveTextContent("(no baseline output)");
    expect(comparison).toHaveTextContent("(no output)");
  });

  it("surfaces detail and baseline failures and pin errors without crashing the stage", async () => {
    const user = userEvent.setup();
    server.use(
      http.get(`${API_BASE}/capabilities`, () =>
        HttpResponse.json(capabilitiesBody({ aria: true })),
      ),
      http.get(`${API_BASE}/prompts/support-agent`, () =>
        HttpResponse.json({ data: { template: "Answer" } }),
      ),
      http.get(`${API_BASE}/prompts/test-runs`, () =>
        HttpResponse.json({ data: [summary("current", 1), summary("baseline", 1)] }),
      ),
      http.get(`${API_BASE}/prompts/test-runs/current`, () =>
        HttpResponse.json({ detail: "current detail unavailable" }, { status: 503 }),
      ),
      http.get(`${API_BASE}/prompts/test-runs/baseline`, () =>
        HttpResponse.json({ detail: "baseline detail unavailable" }, { status: 503 }),
      ),
      http.post(`${API_BASE}/prompts/support-agent/baseline`, () =>
        HttpResponse.json({ detail: "baseline is locked" }, { status: 409 }),
      ),
    );

    renderRuns({ version: 3, baseline_run_id: "baseline" });
    expect(await screen.findByText("Loading results…")).toBeInTheDocument();
    await waitFor(() =>
      expect(screen.queryByTestId("workspace-run-comparison")).not.toBeInTheDocument(),
    );

    await user.click(await screen.findByRole("button", { name: "Set as baseline" }));
    expect(await screen.findByText("baseline is locked")).toBeInTheDocument();
  });
});
