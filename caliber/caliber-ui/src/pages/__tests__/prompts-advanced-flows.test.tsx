import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { http, HttpResponse } from "msw";
import {
  afterAll,
  afterEach,
  beforeAll,
  describe,
  expect,
  it,
  vi,
} from "vitest";
import { MemoryRouter } from "react-router-dom";

import type { PromptInfo } from "@/api/types";
import {
  PromptChatPlayground,
  PromptOptimizationTab,
  PromptTestCases,
} from "@/pages/Prompts";
import { server } from "@/test/server";

const API_BASE = "/ajax-api/2.0/mlflow/caliber";

function envelope<T>(data: T): { data: T } {
  return { data };
}

const supportPrompt: PromptInfo = {
  agent_id: "support-agent",
  agent_name: "Support Agent",
  agent_enabled: true,
  prompt_name: "support-agent",
  version: 3,
  alias: "prod",
  template_preview: "You are a helpful support assistant.",
  template_length: 36,
  approval_id: null,
  artifact_ref: "prompts:/support-agent@prod",
  has_prompt: true,
  needs_prompt: false,
  source: "both",
};

const billingPrompt: PromptInfo = {
  agent_id: "billing-agent",
  agent_name: "Billing Agent",
  agent_enabled: true,
  prompt_name: "billing-agent",
  version: 1,
  alias: "prod",
  template_preview: "You answer billing questions.",
  template_length: 29,
  approval_id: null,
  artifact_ref: "prompts:/billing-agent@prod",
  has_prompt: true,
  needs_prompt: false,
  source: "caliber",
};

function renderWithRouter(ui: JSX.Element): void {
  render(
    <MemoryRouter
      future={{ v7_startTransition: true, v7_relativeSplatPath: true }}
    >
      {ui}
    </MemoryRouter>,
  );
}

beforeAll(() => server.listen({ onUnhandledRequest: "error" }));
afterEach(() => {
  server.resetHandlers();
  window.localStorage.clear();
});
afterAll(() => server.close());

describe("Prompts advanced playground flows", () => {
  it("updates model, handles file states, sends chat messages, and resets sessions", async () => {
    const user = userEvent.setup({ applyAccept: false });
    const scrollSpy = vi.fn();
    Object.defineProperty(Element.prototype, "scrollIntoView", {
      configurable: true,
      writable: true,
      value: scrollSpy,
    });

    let updatedModel: string | null = null;
    const messagePayloads: Array<Record<string, unknown>> = [];

    server.use(
      http.get(`${API_BASE}/assistant/config`, () =>
        HttpResponse.json(
          envelope({
            engine: "fake",
            model: "gpt-4o-mini",
            provider: "openai",
            reasoning: "medium",
            enabled: true,
            disabled_intents: [],
            disabled_domains: [],
            available_models: [
              { id: "gpt-4o-mini", name: "GPT-4o Mini", provider: "openai" },
              { id: "gpt-4.1-mini", name: "GPT-4.1 Mini", provider: "openai" },
            ],
          }),
        ),
      ),
      http.patch(`${API_BASE}/assistant/config`, async ({ request }) => {
        const body = (await request.json()) as { model?: string };
        updatedModel = body.model ?? null;
        return HttpResponse.json(
          envelope({
            engine: "fake",
            model: body.model ?? "gpt-4o-mini",
            provider: "openai",
            reasoning: "medium",
            enabled: true,
            disabled_intents: [],
            disabled_domains: [],
            available_models: [
              { id: "gpt-4o-mini", name: "GPT-4o Mini", provider: "openai" },
              { id: "gpt-4.1-mini", name: "GPT-4.1 Mini", provider: "openai" },
            ],
          }),
        );
      }),
      http.post(`${API_BASE}/assistant/sessions`, () =>
        HttpResponse.json(
          envelope({
            session_id: "ASST-playground-x",
            title: "Prompt Playground",
            owner: "@test",
            status: "active",
            goal: "goal",
            metadata_: {},
            active_draft_id: null,
            created_at: "2025-01-01T00:00:00Z",
            updated_at: "2025-01-01T00:00:00Z",
          }),
          { status: 201 },
        ),
      ),
      http.post(
        `${API_BASE}/assistant/sessions/:sessionId/messages`,
        async ({ request, params }) => {
          const body = (await request.json()) as Record<string, unknown>;
          messagePayloads.push(body);
          if (messagePayloads.length === 1) {
            return HttpResponse.json(
              envelope({
                assistant_message: {
                  message_id: "AMSG-1",
                  session_id: String(params.sessionId),
                  role: "assistant",
                  content: "Assistant reply",
                  metadata_: {},
                  sequence_number: 1,
                  created_at: "2025-01-01T00:00:01Z",
                },
                questions: [],
                draft_updates: [],
                run: null,
              }),
              { status: 201 },
            );
          }
          return HttpResponse.json({ detail: "send failed" }, { status: 500 });
        },
      ),
    );

    renderWithRouter(
      <PromptChatPlayground
        prompts={[supportPrompt, billingPrompt]}
        loading={false}
      />,
    );

    await screen.findByLabelText("Select model");
    await user.selectOptions(
      screen.getByLabelText("Select model"),
      "gpt-4.1-mini",
    );
    await user.click(
      screen.getByRole("button", { name: "Start Chat Session" }),
    );

    expect(updatedModel).toBe("gpt-4.1-mini");
    await screen.findByText(/Locked prompt ref:/);

    const fileInput = screen.getByLabelText(
      "Attach a file",
    ) as HTMLInputElement;
    const oversized = new File([new Uint8Array(300 * 1024)], "huge.txt", {
      type: "text/plain",
    });
    await user.upload(fileInput, oversized);
    expect(await screen.findByText(/File too large/)).toBeInTheDocument();

    const unsupported = new File(["%PDF"], "report.pdf", {
      type: "application/pdf",
    });
    await user.upload(fileInput, unsupported);
    expect(
      await screen.findByText(/Only text-based files are supported/),
    ).toBeInTheDocument();

    await user.upload(
      fileInput,
      new File(["temp"], "notes.txt", { type: "text/plain" }),
    );
    expect(await screen.findByText("notes.txt")).toBeInTheDocument();
    await user.click(screen.getByTitle("Remove file"));
    expect(screen.queryByText("notes.txt")).not.toBeInTheDocument();

    await user.upload(
      fileInput,
      new File(["ticket details"], "context.md", { type: "text/markdown" }),
    );
    await user.type(screen.getByRole("textbox"), "Summarize this");
    await user.click(screen.getByTitle("Send message"));

    expect(await screen.findByText("Assistant reply")).toBeInTheDocument();
    expect(messagePayloads[0]?.content).toContain(
      'The user has uploaded a file named "context.md"',
    );
    expect(scrollSpy).toHaveBeenCalled();

    await user.type(screen.getByRole("textbox"), "Second turn");
    await user.click(screen.getByTitle("Send message"));
    expect(await screen.findByText("send failed")).toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: "New Session" }));
    expect(
      screen.getByRole("button", { name: "Start Chat Session" }),
    ).toBeInTheDocument();
  });

  it("surfaces assistant config and session-start failures", async () => {
    const user = userEvent.setup();
    let startAttempts = 0;

    server.use(
      http.get(`${API_BASE}/assistant/config`, () =>
        HttpResponse.json({ detail: "config unavailable" }, { status: 500 }),
      ),
      http.post(`${API_BASE}/assistant/sessions`, () => {
        startAttempts += 1;
        return HttpResponse.json(
          { detail: "session create failed" },
          { status: 500 },
        );
      }),
    );

    renderWithRouter(
      <PromptChatPlayground prompts={[supportPrompt]} loading={false} />,
    );

    await waitFor(() => {
      expect(screen.queryByText("Loading models…")).not.toBeInTheDocument();
    });
    await user.click(
      screen.getByRole("button", { name: "Start Chat Session" }),
    );
    await waitFor(() => {
      expect(startAttempts).toBe(1);
    });
  });
});

describe("Prompts advanced test-case generation flows", () => {
  it("generates, runs, scores, saves, and removes prompt test cases", async () => {
    const user = userEvent.setup();
    const requestedModels: string[] = [];
    let createdExamples = 0;
    let sessionCount = 0;
    const sessionTitles = new Map<string, string>();

    server.use(
      http.get(`${API_BASE}/assistant/config`, () =>
        HttpResponse.json(
          envelope({
            engine: "fake",
            model: "gpt-4o-mini",
            provider: "openai",
            reasoning: "medium",
            enabled: true,
            disabled_intents: [],
            disabled_domains: [],
            available_models: [
              { id: "gpt-4o-mini", name: "GPT-4o Mini", provider: "openai" },
              { id: "gpt-4.1-mini", name: "GPT-4.1 Mini", provider: "openai" },
            ],
          }),
        ),
      ),
      http.patch(`${API_BASE}/assistant/config`, async ({ request }) => {
        const body = (await request.json()) as { model?: string };
        requestedModels.push(body.model ?? "");
        return HttpResponse.json(
          envelope({
            engine: "fake",
            model: body.model ?? "gpt-4o-mini",
            provider: "openai",
            reasoning: "medium",
            enabled: true,
            disabled_intents: [],
            disabled_domains: [],
            available_models: [
              { id: "gpt-4o-mini", name: "GPT-4o Mini", provider: "openai" },
              { id: "gpt-4.1-mini", name: "GPT-4.1 Mini", provider: "openai" },
            ],
          }),
        );
      }),
      http.post(`${API_BASE}/assistant/sessions`, async ({ request }) => {
        const body = (await request.json()) as { title?: string };
        sessionCount += 1;
        const sid = `ASST-advanced-${sessionCount}`;
        sessionTitles.set(sid, body.title ?? "");
        return HttpResponse.json(
          envelope({
            session_id: sid,
            title: body.title ?? "session",
            owner: "@test",
            status: "active",
            goal: "",
            metadata_: {},
            active_draft_id: null,
            created_at: "2025-01-01T00:00:00Z",
            updated_at: "2025-01-01T00:00:00Z",
          }),
          { status: 201 },
        );
      }),
      http.post(
        `${API_BASE}/assistant/sessions/:sessionId/messages`,
        async ({ params, request }) => {
          const sid = String(params.sessionId);
          const title = sessionTitles.get(sid) ?? "";
          const body = (await request.json()) as { content?: string };

          if (title.startsWith("Test Gen:")) {
            return HttpResponse.json(
              envelope({
                assistant_message: {
                  message_id: "AMSG-gen",
                  session_id: sid,
                  role: "assistant",
                  content: JSON.stringify([
                    {
                      input: "Question 1",
                      expectedBehavior: "Answer clearly",
                      tags: ["basic"],
                    },
                    {
                      input: "Question 2",
                      expectedBehavior: "Decline unsafe request",
                      tags: ["policy"],
                    },
                    {
                      input: "Question 3",
                      expectedBehavior: "Ask clarifying questions",
                      tags: ["edge"],
                    },
                  ]),
                  metadata_: {},
                  sequence_number: 1,
                  created_at: "2025-01-01T00:00:01Z",
                },
                questions: [],
                draft_updates: [],
                run: null,
              }),
              { status: 201 },
            );
          }

          if (title.startsWith("Test Run:") && body.content === "Question 2") {
            return HttpResponse.json(
              { detail: "agent execution failed" },
              { status: 500 },
            );
          }

          if (title.startsWith("Test Run:")) {
            return HttpResponse.json(
              envelope({
                assistant_message: {
                  message_id: "AMSG-agent",
                  session_id: sid,
                  role: "assistant",
                  content: `Response for ${body.content ?? ""}`,
                  metadata_: {},
                  sequence_number: 1,
                  created_at: "2025-01-01T00:00:01Z",
                },
                questions: [],
                draft_updates: [],
                run: null,
              }),
              { status: 201 },
            );
          }

          if (title.endsWith("#3")) {
            return HttpResponse.json(
              envelope({
                assistant_message: {
                  message_id: "AMSG-judge-bad",
                  session_id: sid,
                  role: "assistant",
                  content: "{not-json}",
                  metadata_: {},
                  sequence_number: 1,
                  created_at: "2025-01-01T00:00:01Z",
                },
                questions: [],
                draft_updates: [],
                run: null,
              }),
              { status: 201 },
            );
          }

          return HttpResponse.json(
            envelope({
              assistant_message: {
                message_id: "AMSG-judge",
                session_id: sid,
                role: "assistant",
                content:
                  '{"verdict":"pass","score":0.92,"reasoning":"Looks good"}',
                metadata_: {},
                sequence_number: 1,
                created_at: "2025-01-01T00:00:01Z",
              },
              questions: [],
              draft_updates: [],
              run: null,
            }),
            { status: 201 },
          );
        },
      ),
      http.post(`${API_BASE}/eval-datasets`, () =>
        HttpResponse.json(
          envelope({
            dataset_id: "eds-advanced-1",
            name: "Prompt Test: Support Agent",
            description: "",
            owner: "@local-admin",
            tags: [],
            status: "active",
            version: 1,
            created_at: "2025-01-01T00:00:00Z",
            updated_at: "2025-01-01T00:00:00Z",
          }),
          { status: 201 },
        ),
      ),
      http.post(`${API_BASE}/eval-datasets/:datasetId/examples`, () => {
        createdExamples += 1;
        return HttpResponse.json(
          envelope({
            example_id: `ex-${createdExamples}`,
            dataset_id: "eds-advanced-1",
            version: 1,
            input: {},
            expected: {},
            tags: [],
            metadata_: {},
            superseded_by: null,
            created_at: "2025-01-01T00:00:00Z",
          }),
          { status: 201 },
        );
      }),
    );

    renderWithRouter(
      <PromptTestCases
        prompts={[supportPrompt, billingPrompt]}
        loading={false}
      />,
    );

    await screen.findByLabelText("Select model");

    await user.click(screen.getByLabelText("Decrease test case count"));
    await user.click(screen.getByLabelText("Increase test case count"));
    await user.click(screen.getByRole("button", { name: "10" }));

    await user.selectOptions(
      screen.getByLabelText("Select model"),
      "gpt-4.1-mini",
    );
    await user.click(
      screen.getByRole("button", { name: /Generate Test Cases/i }),
    );
    expect(await screen.findByText("Question 1")).toBeInTheDocument();

    await user.selectOptions(
      screen.getByLabelText("Select model"),
      "gpt-4o-mini",
    );
    await user.click(
      screen.getByRole("button", { name: /Run Tests & Judge/i }),
    );

    expect(await screen.findByText("Overall Score")).toBeInTheDocument();
    expect(screen.getByText("Pass")).toBeInTheDocument();
    expect(screen.getByText("Fail")).toBeInTheDocument();

    await user.click(screen.getByText("Question 3"));
    expect(
      await screen.findByText("Judge response was not valid JSON"),
    ).toBeInTheDocument();

    await user.click(
      screen.getByRole("button", { name: /Save to Test Sets/i }),
    );
    expect(await screen.findByText(/Saved to Test Sets/)).toBeInTheDocument();
    expect(
      screen.getByRole("link", { name: /View Test Sets/i }),
    ).toHaveAttribute("href", "/eval-datasets");
    expect(createdExamples).toBe(3);
    expect(requestedModels).toContain("gpt-4.1-mini");
    expect(requestedModels).toContain("gpt-4o-mini");

    await user.click(screen.getAllByTitle("Remove test case")[0]!);
    expect(screen.queryByText("Question 1")).not.toBeInTheDocument();
  });

  it("shows generation parsing failures when assistant output is not a JSON array", async () => {
    const user = userEvent.setup();
    let sessionId = "";

    server.use(
      http.get(`${API_BASE}/assistant/config`, () =>
        HttpResponse.json(
          envelope({
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
          }),
        ),
      ),
      http.post(`${API_BASE}/assistant/sessions`, () => {
        sessionId = "ASST-bad-json";
        return HttpResponse.json(
          envelope({
            session_id: sessionId,
            title: "Test Gen",
            owner: "@test",
            status: "active",
            goal: "",
            metadata_: {},
            active_draft_id: null,
            created_at: "2025-01-01T00:00:00Z",
            updated_at: "2025-01-01T00:00:00Z",
          }),
          { status: 201 },
        );
      }),
      http.post(
        `${API_BASE}/assistant/sessions/:sid/messages`,
        ({ params }) => {
          if (String(params.sid) === sessionId) {
            return HttpResponse.json(
              envelope({
                assistant_message: {
                  message_id: "AMSG-invalid",
                  session_id: sessionId,
                  role: "assistant",
                  content: "not-json",
                  metadata_: {},
                  sequence_number: 1,
                  created_at: "2025-01-01T00:00:01Z",
                },
                questions: [],
                draft_updates: [],
                run: null,
              }),
              { status: 201 },
            );
          }
          return HttpResponse.json({ detail: "unexpected" }, { status: 500 });
        },
      ),
    );

    renderWithRouter(
      <PromptTestCases prompts={[supportPrompt]} loading={false} />,
    );
    await screen.findByLabelText("Select model");
    await user.click(
      screen.getByRole("button", { name: /Generate Test Cases/i }),
    );
    expect(
      await screen.findByText(
        "LLM did not return a valid JSON array. Try again.",
      ),
    ).toBeInTheDocument();
  });
});

describe("Prompt optimization calibration edge flows", () => {
  function optionHandlers(extraHandlers: Parameters<typeof server.use>) {
    server.use(
      http.get(`${API_BASE}/prompts/calibration/options`, () =>
        HttpResponse.json(
          envelope({
            optimizers: ["MetaPrompt", "MIPROv2"],
            default_optimizer: "MetaPrompt",
            scorers: [
              {
                name: "rubric",
                label: "Rubric",
                description: "Scores against custom rubric criteria.",
                requires_config: true,
                provider: "mlflow",
                category: "core",
                available: true,
                unavailable_reason: null,
                install_command: null,
                config_template: { guidelines: ["Be accurate."] },
              },
            ],
            default_scorers: ["rubric"],
            default_gate: {
              min_aggregate_score: 0.85,
              max_regression_delta: 0.02,
            },
          }),
        ),
      ),
      http.get(`${API_BASE}/jobs`, () => HttpResponse.json(envelope([]))),
      ...extraHandlers,
    );
  }

  it("renders loading and no-deployed-prompt states", async () => {
    renderWithRouter(<PromptOptimizationTab prompts={[]} loading />);
    expect(await screen.findByText("Loading prompts…")).toBeInTheDocument();
  });

  it("renders the no-prompts-to-calibrate state for a pure placeholder", async () => {
    // A draft (has_prompt=false but a real prompt_name) is now calibratable, so
    // only a pure promptless-agent placeholder (null prompt_name) hits this empty
    // state.
    renderWithRouter(
      <PromptOptimizationTab
        prompts={[
          {
            ...supportPrompt,
            prompt_name: null,
            has_prompt: false,
            needs_prompt: true,
            artifact_ref: null,
          },
        ]}
        loading={false}
      />,
    );
    expect(
      await screen.findByText("No prompts to calibrate yet."),
    ).toBeInTheDocument();
  });

  it("treats a draft prompt (has_prompt=false) as calibratable", async () => {
    optionHandlers([
      http.get(`${API_BASE}/eval-datasets`, () =>
        HttpResponse.json(envelope([])),
      ),
    ]);

    renderWithRouter(
      <PromptOptimizationTab
        prompts={[{ ...supportPrompt, has_prompt: false, artifact_ref: null }]}
        loading={false}
      />,
    );
    // The draft does NOT fall into the empty state — the run configuration renders.
    expect(await screen.findByText("Run Configuration")).toBeInTheDocument();
    expect(
      screen.queryByText("No prompts to calibrate yet."),
    ).not.toBeInTheDocument();
  });

  it("surfaces calibration option load errors", async () => {
    server.use(
      http.get(`${API_BASE}/prompts/calibration/options`, () =>
        HttpResponse.json({ detail: "options offline" }, { status: 500 }),
      ),
      http.get(`${API_BASE}/eval-datasets`, () =>
        HttpResponse.json(envelope([])),
      ),
      http.get(`${API_BASE}/jobs`, () => HttpResponse.json(envelope([]))),
    );

    renderWithRouter(
      <PromptOptimizationTab prompts={[supportPrompt]} loading={false} />,
    );
    expect(await screen.findByText("options offline")).toBeInTheDocument();
  });

  it("restores a persisted candidate as the active run after a UI reload", async () => {
    optionHandlers([
      http.get(`${API_BASE}/eval-datasets`, () =>
        HttpResponse.json(envelope([])),
      ),
    ]);
    server.use(
      http.get(`${API_BASE}/jobs`, () =>
        HttpResponse.json(
          envelope([
            {
              job_id: "job-ready-after-reload",
              agent_id: "support-agent",
              workflow_id: null,
              primary_item_id: "item-ready",
              mlflow_run_id: null,
              artifact_type: "prompt",
              optimizer_type: "MetaPrompt",
              status: "candidate_ready",
              current_stage: "eval",
              attempt_count: 1,
              error_message: null,
              total_tokens: 10,
              cost_usd: 0.01,
              bundle_targets: [],
              bundle_expansion_count: 1,
              diagnosis: null,
              candidate: { content: "Improved prompt" },
              eval_results: { candidate: { overall: 0.92 } },
              calibration_spec: null,
              created_at: "2025-01-02T00:00:00Z",
              updated_at: "2025-01-02T00:01:00Z",
            },
          ]),
        ),
      ),
    );

    renderWithRouter(
      <PromptOptimizationTab prompts={[supportPrompt]} loading={false} />,
    );

    expect(
      (await screen.findAllByText("job-ready-after-reload")).length,
    ).toBeGreaterThan(0);
    expect(screen.getByText("92.0%")).toBeInTheDocument();
    expect(screen.getAllByTestId("job-apply-btn").length).toBeGreaterThan(0);
  });

  it("shows the backend calibration failure on the active run", async () => {
    optionHandlers([
      http.get(`${API_BASE}/eval-datasets`, () =>
        HttpResponse.json(envelope([])),
      ),
    ]);
    server.use(
      http.get(`${API_BASE}/jobs`, () =>
        HttpResponse.json(
          envelope([
            {
              job_id: "job-failed-scorer",
              agent_id: "support-agent",
              workflow_id: null,
              primary_item_id: "item-failed",
              mlflow_run_id: null,
              artifact_type: "prompt",
              optimizer_type: "MetaPrompt",
              status: "failed",
              current_stage: "eval",
              attempt_count: 1,
              error_message:
                "candidate evaluation produced no valid score for selected scorer(s): Correctness",
              total_tokens: 10,
              cost_usd: 0.01,
              bundle_targets: [],
              bundle_expansion_count: 1,
              diagnosis: null,
              candidate: null,
              eval_results: null,
              calibration_spec: null,
              created_at: "2025-01-02T00:00:00Z",
              updated_at: "2025-01-02T00:01:00Z",
            },
          ]),
        ),
      ),
    );

    renderWithRouter(
      <PromptOptimizationTab prompts={[supportPrompt]} loading={false} />,
    );

    expect(await screen.findByRole("alert")).toHaveTextContent(
      "no valid score for selected scorer(s): Correctness",
    );
  });

  it("validates scorer selections, weights, and scorer config before starting a run", async () => {
    optionHandlers([
      http.get(`${API_BASE}/eval-datasets`, () =>
        HttpResponse.json(
          envelope([
            {
              dataset_id: "eds-validations",
              name: "Validation Dataset",
              description: "",
              owner: "@test",
              tags: [],
              status: "active",
              version: 1,
              created_at: "2025-01-01T00:00:00Z",
              updated_at: "2025-01-01T00:00:00Z",
            },
          ]),
        ),
      ),
    ]);

    const user = userEvent.setup();
    renderWithRouter(
      <PromptOptimizationTab prompts={[supportPrompt]} loading={false} />,
    );

    await screen.findByText("Run Configuration");
    const rubric = screen.getByRole("checkbox", { name: "Rubric" });
    await user.click(rubric);
    await user.click(
      screen.getByRole("button", { name: "Start Calibration Run" }),
    );
    expect(
      await screen.findByText("Select at least one scorer."),
    ).toBeInTheDocument();

    await user.click(rubric);
    const weight = screen.getByLabelText("rubric weight");
    await user.clear(weight);
    await user.type(weight, "0");
    await user.click(
      screen.getByRole("button", { name: "Start Calibration Run" }),
    );
    expect(
      await screen.findByText("Scorer rubric must have a positive weight."),
    ).toBeInTheDocument();

    await user.clear(weight);
    await user.type(weight, "1");
    const config = screen.getByPlaceholderText(
      '{"guidelines": ["Do not hallucinate."]}',
    );
    await user.clear(config);
    fireEvent.change(config, { target: { value: "[]" } });
    await user.click(
      screen.getByRole("button", { name: "Start Calibration Run" }),
    );
    expect(
      await screen.findByText("Scorer rubric config must be a JSON object."),
    ).toBeInTheDocument();
  });

  it("uploads JSONL calibration datasets and starts a configured scorer run", async () => {
    const examples: Array<Record<string, unknown>> = [];
    let createdDataset: Record<string, unknown> | null = null;
    let runPayload: Record<string, unknown> | null = null;
    let datasets = [
      {
        dataset_id: "eds-existing",
        name: "Existing Dataset",
        description: "",
        owner: "@test",
        tags: [],
        status: "active",
        version: 1,
        created_at: "2025-01-01T00:00:00Z",
        updated_at: "2025-01-01T00:00:00Z",
      },
    ];

    optionHandlers([
      http.get(`${API_BASE}/eval-datasets`, () =>
        HttpResponse.json(envelope(datasets)),
      ),
      http.post(`${API_BASE}/eval-datasets`, async ({ request }) => {
        createdDataset = (await request.json()) as Record<string, unknown>;
        const uploaded = {
          dataset_id: "eds-uploaded",
          name: String(createdDataset.name),
          description: String(createdDataset.description ?? ""),
          owner: "@local-admin",
          tags: ["prompt-calibration", "prompt-optimization", "upload"],
          status: "active",
          version: 1,
          created_at: "2025-01-01T00:00:00Z",
          updated_at: "2025-01-01T00:00:00Z",
        };
        datasets = [uploaded, ...datasets];
        return HttpResponse.json(envelope(uploaded), { status: 201 });
      }),
      http.post(
        `${API_BASE}/eval-datasets/:datasetId/examples`,
        async ({ request, params }) => {
          examples.push({
            datasetId: String(params.datasetId),
            ...((await request.json()) as Record<string, unknown>),
          });
          return HttpResponse.json(
            envelope({
              example_id: `ex-${examples.length}`,
              dataset_id: String(params.datasetId),
              version: 1,
              input: {},
              expected: {},
              tags: [],
              metadata_: {},
              superseded_by: null,
              created_at: "2025-01-01T00:00:00Z",
            }),
            { status: 201 },
          );
        },
      ),
      http.post(`${API_BASE}/prompts/calibration/runs`, async ({ request }) => {
        runPayload = (await request.json()) as Record<string, unknown>;
        return HttpResponse.json(
          envelope({
            item: {
              item_id: "item-configured-run",
              agent_id: "support-agent",
              assessment_id: null,
              trace_id: null,
              experiment_id: null,
              session_id: null,
              workflow_id: null,
              category: "prompt_optimization",
              free_text: "Prompt calibration run",
              severity: "standard",
              artifact_type_hint: "prompt",
              artifact_ref: "prompts:/support-agent@prod",
              submitted_context: {},
              status: "verified",
              priority: 100,
              assigned_to: null,
              verified_by: "@test",
              verified_at: "2025-01-01T00:00:00Z",
              verification_notes: null,
              refinement_target: "prompt",
              duplicate_of_id: null,
              created_at: "2025-01-01T00:00:00Z",
            },
            job: {
              job_id: "job-configured-run",
              agent_id: "support-agent",
              workflow_id: null,
              primary_item_id: "item-configured-run",
              mlflow_run_id: null,
              artifact_type: "prompt",
              optimizer_type: "MetaPrompt",
              status: "queued",
              current_stage: "triage",
              attempt_count: 0,
              error_message: null,
              total_tokens: 0,
              cost_usd: 0,
              bundle_targets: [],
              bundle_expansion_count: 1,
              diagnosis: null,
              candidate: null,
              eval_results: null,
              created_at: "2025-01-01T00:00:00Z",
              updated_at: "2025-01-01T00:00:00Z",
            },
          }),
          { status: 201 },
        );
      }),
    ]);

    const user = userEvent.setup();
    renderWithRouter(
      <PromptOptimizationTab prompts={[supportPrompt]} loading={false} />,
    );
    await screen.findByText("Run Configuration");

    await user.click(screen.getByRole("button", { name: "Upload Dataset" }));
    expect(
      await screen.findByText("Select a dataset file first."),
    ).toBeInTheDocument();

    const file = new File(
      [
        '{"input":"Refund request","expected":"Route to refunds","tags":["refund"],"weight":2}\n',
        '{"user_message":"Need help","reference_answer":"Ask one clarifying question"}\n',
      ],
      "cases.jsonl",
      { type: "application/x-ndjson" },
    );
    await user.upload(
      screen.getByLabelText("Upload calibration dataset"),
      file,
    );
    expect(screen.getByDisplayValue("prompt-cal-cases")).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Upload Dataset" }));

    expect(
      await screen.findByText("Uploaded 2 examples to prompt-cal-cases."),
    ).toBeInTheDocument();
    expect(createdDataset).toMatchObject({
      name: "prompt-cal-cases",
      description: "Uploaded from Prompt Calibration",
      owner: "@local-admin",
    });
    expect(examples).toHaveLength(2);
    expect(examples[0]).toMatchObject({
      datasetId: "eds-uploaded",
      input: { user_message: "Refund request" },
      expected: { expected_response: "Route to refunds" },
      tags: ["refund"],
      weight: 2,
    });

    const config = screen.getByPlaceholderText(
      '{"guidelines": ["Do not hallucinate."]}',
    );
    await user.clear(config);
    fireEvent.change(config, {
      target: { value: '{"guidelines":["Strictly cite policy."]}' },
    });
    await user.clear(screen.getByLabelText("rubric weight"));
    await user.type(screen.getByLabelText("rubric weight"), "2");
    await user.type(
      screen.getByLabelText("Calibration run notes"),
      "Configured rubric run",
    );
    await user.click(
      screen.getByRole("button", { name: "Start Calibration Run" }),
    );

    expect(await screen.findByText("job-configured-run")).toBeInTheDocument();
    expect(runPayload).toMatchObject({
      agent_id: "support-agent",
      eval_dataset_id: "eds-uploaded",
      // Reproducibility: the run pins the selected dataset's current version.
      eval_dataset_version: 1,
      optimizer_type: "MetaPrompt",
      notes: "Configured rubric run",
      scorers: [
        {
          name: "rubric",
          weight: 2,
          config: { guidelines: ["Strictly cite policy."] },
        },
      ],
    });

    // The pinned version is surfaced in the run provenance panel.
    expect(
      await screen.findByText(/prompt-cal-cases @ v1/),
    ).toBeInTheDocument();
  });
});
