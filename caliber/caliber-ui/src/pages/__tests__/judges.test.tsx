import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { http, HttpResponse } from "msw";
import { afterAll, afterEach, beforeAll, describe, expect, it } from "vitest";
import { MemoryRouter, Route, Routes } from "react-router-dom";

import { Judges } from "@/pages/Judges";
import { server } from "@/test/server";

const API_BASE = "/ajax-api/2.0/mlflow/caliber";
const NOW = "2026-06-21T18:00:00Z";

function envelope<T>(data: T): { data: T } {
  return { data };
}

function makeJudge(overrides: Partial<Record<string, unknown>> = {}) {
  return {
    judge_id: "JDG-1",
    name: "answer-faithfulness",
    description: "Judges faithfulness to the expected answer.",
    instructions: "Is {{ outputs }} faithful to {{ expectations }}?",
    model: "openai:/gpt-4o-mini",
    feedback_value_type: "bool" as const,
    owner: "@sarah",
    tags: ["faithfulness"],
    status: "active" as const,
    created_at: NOW,
    updated_at: NOW,
    ...overrides,
  };
}

function makeReviewQueue() {
  return {
    queue_id: "RVQ-1",
    name: "Answer reviews",
    description: "",
    questions: [
      {
        key: "correct",
        title: "Is the answer correct?",
        type: "pass_fail",
        options: [],
        required: true,
        target: "feedback",
      },
    ],
    reviewers: [],
    owner: "@sarah",
    status: "active",
    created_at: NOW,
    updated_at: NOW,
    item_count: 1,
    pending_count: 0,
  };
}

function renderPage(): void {
  render(
    <MemoryRouter
      future={{ v7_startTransition: true, v7_relativeSplatPath: true }}
      initialEntries={["/judges"]}
    >
      <Routes>
        <Route path="/judges" element={<Judges />} />
      </Routes>
    </MemoryRouter>,
  );
}

beforeAll(() => server.listen({ onUnhandledRequest: "error" }));
afterEach(() => server.resetHandlers());
afterAll(() => server.close());

describe("Judges", () => {
  it("lists judges with their model and return type", async () => {
    server.use(
      http.get(`${API_BASE}/judges`, () =>
        HttpResponse.json(envelope([makeJudge()])),
      ),
    );
    renderPage();
    expect(await screen.findByText("answer-faithfulness")).toBeInTheDocument();
    // Model appears in the row cell (and in the filter dropdown option).
    expect(screen.getAllByText("openai:/gpt-4o-mini").length).toBeGreaterThan(0);
    expect(screen.getByText("bool")).toBeInTheDocument();
  });

  it("offers in-use models as datalist suggestions on the create form", async () => {
    server.use(
      http.get(`${API_BASE}/judges`, () =>
        HttpResponse.json(
          envelope([
            makeJudge({ judge_id: "JDG-9", model: "anthropic:/claude-3-5-sonnet" }),
          ]),
        ),
      ),
    );
    const user = userEvent.setup();
    renderPage();
    await screen.findByText("answer-faithfulness");

    await user.click(screen.getByRole("button", { name: "+ New Judge" }));

    const datalist = document.getElementById("judge-model-options");
    expect(datalist).not.toBeNull();
    const values = Array.from(datalist?.querySelectorAll("option") ?? []).map(
      (o) => o.value,
    );
    // The model already used by an existing judge is offered as a suggestion,
    // alongside the app's default — and the field stays a free-text input.
    expect(values).toContain("anthropic:/claude-3-5-sonnet");
    expect(values).toContain("openai:/gpt-4o-mini");
  });

  it("blocks the create submit until instructions reference a template var", async () => {
    server.use(
      http.get(`${API_BASE}/judges`, () => HttpResponse.json(envelope([]))),
    );
    const user = userEvent.setup();
    renderPage();
    await screen.findByText("No judges yet.");

    await user.click(screen.getByRole("button", { name: "+ New Judge" }));
    await user.type(screen.getByPlaceholderText("answer-faithfulness"), "tone");

    const submit = screen.getByRole("button", { name: "Create judge" });
    // Instructions empty → submit disabled.
    expect(submit).toBeDisabled();

    // Instructions without a variable → still disabled + a warning shows.
    // (Type plain text; braces are inserted via the chips below to avoid
    // userEvent's `{{` special-key parsing.)
    const instructions = screen.getByPlaceholderText(/faithfully answer/);
    await user.type(instructions, "just say yes");
    expect(submit).toBeDisabled();
    expect(
      screen.getByText(/must reference at least one variable/),
    ).toBeInTheDocument();

    // Inserting the {{ outputs }} variable via its chip enables submit.
    await user.click(screen.getByRole("button", { name: "{{ outputs }}" }));
    expect(submit).toBeEnabled();
  });

  it("runs a judge in the 'Try it' playground and shows the score + rationale", async () => {
    let testRunBody: Record<string, unknown> | null = null;
    server.use(
      http.get(`${API_BASE}/judges`, () => HttpResponse.json(envelope([makeJudge()]))),
      http.post(`${API_BASE}/judges/:id/test-run`, async ({ request }) => {
        testRunBody = (await request.json()) as Record<string, unknown>;
        return HttpResponse.json(
          envelope({ score: 0.8, value: true, rationale: "well grounded" }),
        );
      }),
    );
    const user = userEvent.setup();
    renderPage();
    await screen.findByText("answer-faithfulness");

    await user.click(screen.getByTestId("judge-try-JDG-1"));
    const panel = await screen.findByTestId("judge-playground");
    await user.type(screen.getByTestId("judge-try-outputs"), "Paris is the capital.");
    await user.click(screen.getByTestId("judge-try-run"));

    const result = await screen.findByTestId("judge-try-result");
    expect(result).toHaveTextContent("80%");
    expect(result).toHaveTextContent("well grounded");
    expect(testRunBody).toMatchObject({ outputs: "Paris is the capital." });
    expect(panel).toBeInTheDocument();
  });

  it("checks human alignment (agreement + kappa) from the playground", async () => {
    let alignBody: Record<string, unknown> | null = null;
    server.use(
      http.get(`${API_BASE}/judges`, () => HttpResponse.json(envelope([makeJudge()]))),
      http.get(`${API_BASE}/review-queues`, () => HttpResponse.json(envelope([]))),
      http.post(`${API_BASE}/judges/:id/alignment`, async ({ request }) => {
        alignBody = (await request.json()) as Record<string, unknown>;
        return HttpResponse.json(
          envelope({
            n: 2,
            scored: 2,
            agreement_rate: 0.5,
            cohen_kappa: 0.0,
            threshold: 0.5,
            confusion: { true_pos: 1, false_pos: 1, true_neg: 0, false_neg: 0 },
            per_example: [],
          }),
        );
      }),
    );
    const user = userEvent.setup();
    renderPage();
    await screen.findByText("answer-faithfulness");

    await user.click(screen.getByTestId("judge-try-JDG-1"));
    await user.click(screen.getByTestId("judge-mode-align"));
    await user.type(screen.getByTestId("align-output-0"), "good answer");
    await user.type(screen.getByTestId("align-output-1"), "bad answer");
    await user.click(screen.getByTestId("align-run"));

    const result = await screen.findByTestId("align-result");
    expect(result).toHaveTextContent("50%");
    expect(result).toHaveTextContent("0.00");
    // Two labeled examples were submitted.
    expect((alignBody?.examples as unknown[]).length).toBe(2);
  });

  it("imports completed Review Queue labels with trace provenance", async () => {
    let alignBody: Record<string, unknown> | null = null;
    server.use(
      http.get(`${API_BASE}/judges`, () => HttpResponse.json(envelope([makeJudge()]))),
      http.get(`${API_BASE}/review-queues`, () =>
        HttpResponse.json(envelope([makeReviewQueue()])),
      ),
      http.get(`${API_BASE}/review-queues/RVQ-1/alignment-examples`, () =>
        HttpResponse.json(
          envelope({
            queue_id: "RVQ-1",
            question_key: "correct",
            examples: [
              {
                inputs: { trace_id: "tr-1", review_item_id: "RI-1" },
                outputs: "grounded answer",
                expectations: { gold: "grounded answer" },
                label: true,
                provenance: {
                  queue_id: "RVQ-1",
                  item_id: "RI-1",
                  trace_id: "tr-1",
                  question_key: "correct",
                  completed_by: "@reviewer",
                  assessment_ids: ["A-1"],
                },
              },
            ],
            skipped: [],
          }),
        ),
      ),
      http.post(`${API_BASE}/judges/:id/alignment`, async ({ request }) => {
        alignBody = (await request.json()) as Record<string, unknown>;
        return HttpResponse.json(
          envelope({
            n: 1,
            scored: 1,
            agreement_rate: 1,
            cohen_kappa: 1,
            threshold: 0.5,
            confusion: { true_pos: 1, false_pos: 0, true_neg: 0, false_neg: 0 },
            per_example: [],
          }),
        );
      }),
    );
    const user = userEvent.setup();
    renderPage();
    await screen.findByText("answer-faithfulness");
    await user.click(screen.getByTestId("judge-try-JDG-1"));
    await user.click(screen.getByTestId("judge-mode-align"));

    await user.selectOptions(screen.getByTestId("align-review-queue"), "RVQ-1");
    await user.selectOptions(screen.getByTestId("align-review-question"), "correct");
    await user.click(screen.getByTestId("align-import-review-labels"));
    expect(await screen.findByText(/Imported 1 completed label/)).toBeInTheDocument();
    expect(screen.getByTestId("align-output-0")).toHaveValue("grounded answer");

    await user.click(screen.getByTestId("align-run"));
    await waitFor(() => expect(alignBody).not.toBeNull());
    expect((alignBody?.examples as unknown[])[0]).toEqual({
      outputs: "grounded answer",
      label: true,
      inputs: { trace_id: "tr-1", review_item_id: "RI-1" },
      expectations: { gold: "grounded answer" },
    });
  });

  it("creates a judge and refreshes the list", async () => {
    let created = false;
    server.use(
      http.get(`${API_BASE}/judges`, () =>
        HttpResponse.json(envelope(created ? [makeJudge()] : [])),
      ),
      http.post(`${API_BASE}/judges`, async () => {
        created = true;
        return HttpResponse.json(envelope(makeJudge()), { status: 201 });
      }),
    );
    const user = userEvent.setup();
    renderPage();
    await screen.findByText("No judges yet.");

    await user.click(screen.getByRole("button", { name: "+ New Judge" }));
    await user.type(screen.getByPlaceholderText("answer-faithfulness"), "faith");
    await user.type(screen.getByPlaceholderText(/faithfully answer/), "Rate ");
    await user.click(screen.getByRole("button", { name: "{{ outputs }}" }));
    await user.click(screen.getByRole("button", { name: "Create judge" }));

    await waitFor(() =>
      expect(screen.getByText("answer-faithfulness")).toBeInTheDocument(),
    );
  });
});
