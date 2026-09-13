import { fireEvent, render, screen, waitFor } from "@testing-library/react";
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
    expect(values).toContain("openai:/gpt-5.6-luna");
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
    const body = alignBody as unknown as Record<string, unknown>;
    expect((body.examples as unknown[]).length).toBe(2);
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
    const body = alignBody as unknown as Record<string, unknown>;
    expect((body.examples as unknown[])[0]).toEqual({
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

  it("searches by name/description/owner/instructions and shows a no-match empty state", async () => {
    server.use(
      http.get(`${API_BASE}/judges`, () =>
        HttpResponse.json(
          envelope([
            makeJudge({
              judge_id: "JDG-1",
              name: "answer-faithfulness",
              owner: "@sarah",
            }),
            makeJudge({
              judge_id: "JDG-2",
              name: "tone-checker",
              description: "Checks the reply is polite.",
              owner: "@alex",
              model: "anthropic:/claude-3-5-sonnet",
            }),
          ]),
        ),
      ),
    );
    const user = userEvent.setup();
    renderPage();
    await screen.findByText("answer-faithfulness");
    expect(screen.getByText("tone-checker")).toBeInTheDocument();

    await user.type(
      screen.getByPlaceholderText("Search by name, model, instructions…"),
      "@alex",
    );
    expect(screen.getByText("tone-checker")).toBeInTheDocument();
    expect(screen.queryByText("answer-faithfulness")).not.toBeInTheDocument();

    await user.clear(
      screen.getByPlaceholderText("Search by name, model, instructions…"),
    );
    await user.type(
      screen.getByPlaceholderText("Search by name, model, instructions…"),
      "no such judge anywhere",
    );
    expect(
      screen.getByText("No judges match “no such judge anywhere”."),
    ).toBeInTheDocument();
  });

  it("filters by model, and Clear filters resets search/model/status together", async () => {
    server.use(
      http.get(`${API_BASE}/judges`, () =>
        HttpResponse.json(
          envelope([
            makeJudge({ judge_id: "JDG-1", model: "openai:/gpt-4o-mini" }),
            makeJudge({
              judge_id: "JDG-2",
              name: "tone-checker",
              model: "anthropic:/claude-3-5-sonnet",
            }),
          ]),
        ),
      ),
    );
    const user = userEvent.setup();
    renderPage();
    await screen.findByText("answer-faithfulness");

    // No filters active yet -> no "Clear filters" affordance.
    expect(screen.queryByRole("button", { name: /clear filters/i })).not.toBeInTheDocument();

    await user.selectOptions(
      screen.getByLabelText("Filter by model"),
      "anthropic:/claude-3-5-sonnet",
    );
    expect(screen.getByText("tone-checker")).toBeInTheDocument();
    expect(screen.queryByText("answer-faithfulness")).not.toBeInTheDocument();

    const clear = screen.getByRole("button", { name: /clear filters/i });
    await user.click(clear);
    expect(screen.getByText("answer-faithfulness")).toBeInTheDocument();
    expect(screen.getByText("tone-checker")).toBeInTheDocument();
  });

  it("refetches with the selected status filter when switching tabs", async () => {
    const seenStatuses: string[] = [];
    server.use(
      http.get(`${API_BASE}/judges`, ({ request }) => {
        const status = new URL(request.url).searchParams.get("status") ?? "";
        seenStatuses.push(status);
        return HttpResponse.json(
          envelope(
            status === "archived"
              ? [makeJudge({ judge_id: "JDG-2", name: "retired-judge", status: "archived" })]
              : status === "all"
                ? [makeJudge(), makeJudge({ judge_id: "JDG-2", name: "retired-judge", status: "archived" })]
                : [makeJudge()],
          ),
        );
      }),
    );
    const user = userEvent.setup();
    renderPage();
    await screen.findByText("answer-faithfulness");

    await user.click(screen.getByRole("button", { name: "Archived" }));
    expect(await screen.findByText("retired-judge")).toBeInTheDocument();
    expect(screen.queryByText("answer-faithfulness")).not.toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: "All" }));
    expect(await screen.findByText("answer-faithfulness")).toBeInTheDocument();
    expect(screen.getByText("retired-judge")).toBeInTheDocument();

    expect(seenStatuses).toEqual(["active", "archived", "all"]);
  });

  it("shows the fallback description, model, and return-type badge when a judge omits them", async () => {
    server.use(
      http.get(`${API_BASE}/judges`, () =>
        HttpResponse.json(
          envelope([
            makeJudge({
              description: "",
              instructions: "Is {{ outputs }} on-brand?",
              model: null,
              feedback_value_type: null,
            }),
          ]),
        ),
      ),
    );
    renderPage();
    await screen.findByText("answer-faithfulness");
    expect(screen.getByText("Is {{ outputs }} on-brand?")).toBeInTheDocument();
    expect(screen.getByText("default")).toBeInTheDocument();
    expect(screen.getByText("auto")).toBeInTheDocument();
  });

  it("archives an active judge and restores an archived one", async () => {
    let status: "active" | "archived" = "active";
    const patchBodies: Array<Record<string, unknown>> = [];
    server.use(
      http.get(`${API_BASE}/judges`, () =>
        HttpResponse.json(envelope([makeJudge({ status })])),
      ),
      http.patch(`${API_BASE}/judges/JDG-1`, async ({ request }) => {
        const body = (await request.json()) as Record<string, unknown>;
        patchBodies.push(body);
        status = body.status as "active" | "archived";
        return HttpResponse.json(envelope(makeJudge({ status })));
      }),
    );
    const user = userEvent.setup();
    renderPage();
    await screen.findByText("answer-faithfulness");

    await user.click(screen.getByRole("button", { name: "Archive" }));
    await waitFor(() =>
      expect(screen.getByRole("button", { name: "Restore" })).toBeInTheDocument(),
    );
    expect(patchBodies[0]).toEqual({ status: "archived" });

    await user.click(screen.getByRole("button", { name: "Restore" }));
    await waitFor(() =>
      expect(screen.getByRole("button", { name: "Archive" })).toBeInTheDocument(),
    );
    expect(patchBodies[1]).toEqual({ status: "active" });
  });

  it("shows an error banner when archiving fails", async () => {
    server.use(
      http.get(`${API_BASE}/judges`, () =>
        HttpResponse.json(envelope([makeJudge()])),
      ),
      http.patch(`${API_BASE}/judges/JDG-1`, () =>
        HttpResponse.json({ detail: "judge is locked by another editor" }, { status: 409 }),
      ),
    );
    const user = userEvent.setup();
    renderPage();
    await screen.findByText("answer-faithfulness");

    await user.click(screen.getByRole("button", { name: "Archive" }));
    expect(
      await screen.findByText("judge is locked by another editor"),
    ).toBeInTheDocument();
    // The action didn't go through, so the button still reads "Archive".
    expect(screen.getByRole("button", { name: "Archive" })).toBeInTheDocument();
  });

  it("parses valid JSON inputs/expectations in the playground and rejects invalid or non-object JSON", async () => {
    let testRunBody: Record<string, unknown> | null = null;
    server.use(
      http.get(`${API_BASE}/judges`, () => HttpResponse.json(envelope([makeJudge()]))),
      http.post(`${API_BASE}/judges/:id/test-run`, async ({ request }) => {
        testRunBody = (await request.json()) as Record<string, unknown>;
        return HttpResponse.json(envelope({ score: 1, value: true, rationale: "ok" }));
      }),
    );
    const user = userEvent.setup();
    renderPage();
    await screen.findByText("answer-faithfulness");
    await user.click(screen.getByTestId("judge-try-JDG-1"));

    await user.type(screen.getByTestId("judge-try-outputs"), "Paris.");
    fireEvent.change(screen.getByTestId("judge-try-inputs"), {
      target: { value: '{"question": "capital of France"}' },
    });
    fireEvent.change(screen.getByTestId("judge-try-expectations"), {
      target: { value: "[not valid json" },
    });
    await user.click(screen.getByTestId("judge-try-run"));
    expect(
      await screen.findByText("Expectations is not valid JSON."),
    ).toBeInTheDocument();
    expect(testRunBody).toBeNull();

    fireEvent.change(screen.getByTestId("judge-try-expectations"), {
      target: { value: "[1,2,3]" },
    });
    await user.click(screen.getByTestId("judge-try-run"));
    expect(
      await screen.findByText("Expectations must be a JSON object."),
    ).toBeInTheDocument();
    expect(testRunBody).toBeNull();

    fireEvent.change(screen.getByTestId("judge-try-expectations"), {
      target: { value: '{"expected": "Paris"}' },
    });
    await user.click(screen.getByTestId("judge-try-run"));
    await waitFor(() => expect(testRunBody).not.toBeNull());
    expect(testRunBody).toMatchObject({
      outputs: "Paris.",
      inputs: { question: "capital of France" },
      expectations: { expected: "Paris" },
    });
  });

  it("surfaces the server error message when a judge test-run fails", async () => {
    server.use(
      http.get(`${API_BASE}/judges`, () => HttpResponse.json(envelope([makeJudge()]))),
      http.post(`${API_BASE}/judges/:id/test-run`, () =>
        HttpResponse.json({ detail: "model unavailable" }, { status: 502 }),
      ),
    );
    const user = userEvent.setup();
    renderPage();
    await screen.findByText("answer-faithfulness");
    await user.click(screen.getByTestId("judge-try-JDG-1"));
    await user.type(screen.getByTestId("judge-try-outputs"), "Paris.");
    await user.click(screen.getByTestId("judge-try-run"));
    expect(await screen.findByText("model unavailable")).toBeInTheDocument();
  });

  it("blocks the alignment check until at least one example has an output", async () => {
    server.use(
      http.get(`${API_BASE}/judges`, () => HttpResponse.json(envelope([makeJudge()]))),
      http.get(`${API_BASE}/review-queues`, () => HttpResponse.json(envelope([]))),
    );
    const user = userEvent.setup();
    renderPage();
    await screen.findByText("answer-faithfulness");
    await user.click(screen.getByTestId("judge-try-JDG-1"));
    await user.click(screen.getByTestId("judge-mode-align"));

    await user.click(screen.getByTestId("align-run"));
    expect(
      await screen.findByText("Add at least one labeled example."),
    ).toBeInTheDocument();
  });

  it("removes an alignment example row and surfaces alignment-run failures", async () => {
    server.use(
      http.get(`${API_BASE}/judges`, () => HttpResponse.json(envelope([makeJudge()]))),
      http.get(`${API_BASE}/review-queues`, () => HttpResponse.json(envelope([]))),
      http.post(`${API_BASE}/judges/:id/alignment`, () =>
        HttpResponse.json({ detail: "alignment service unavailable" }, { status: 503 }),
      ),
    );
    const user = userEvent.setup();
    renderPage();
    await screen.findByText("answer-faithfulness");
    await user.click(screen.getByTestId("judge-try-JDG-1"));
    await user.click(screen.getByTestId("judge-mode-align"));

    expect(screen.getByTestId("align-output-1")).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Remove example 2" }));
    expect(screen.queryByTestId("align-output-1")).not.toBeInTheDocument();

    await user.type(screen.getByTestId("align-output-0"), "some output");
    await user.click(screen.getByTestId("align-run"));
    expect(
      await screen.findByText("alignment service unavailable"),
    ).toBeInTheDocument();
  });

  it("surfaces an error when importing review labels fails", async () => {
    server.use(
      http.get(`${API_BASE}/judges`, () => HttpResponse.json(envelope([makeJudge()]))),
      http.get(`${API_BASE}/review-queues`, () =>
        HttpResponse.json(envelope([makeReviewQueue()])),
      ),
      http.get(`${API_BASE}/review-queues/RVQ-1/alignment-examples`, () =>
        HttpResponse.json({ detail: "queue has no completed labels" }, { status: 422 }),
      ),
    );
    const user = userEvent.setup();
    renderPage();
    await screen.findByText("answer-faithfulness");
    await user.click(screen.getByTestId("judge-try-JDG-1"));
    await user.click(screen.getByTestId("judge-mode-align"));
    await user.selectOptions(screen.getByTestId("align-review-queue"), "RVQ-1");
    await user.selectOptions(screen.getByTestId("align-review-question"), "correct");
    await user.click(screen.getByTestId("align-import-review-labels"));
    expect(
      await screen.findByText("queue has no completed labels"),
    ).toBeInTheDocument();
  });

  it("surfaces the server error message when creating a judge fails", async () => {
    server.use(
      http.get(`${API_BASE}/judges`, () => HttpResponse.json(envelope([]))),
      http.post(`${API_BASE}/judges`, () =>
        HttpResponse.json({ detail: "duplicate judge name" }, { status: 409 }),
      ),
    );
    const user = userEvent.setup();
    renderPage();
    await screen.findByText("No judges yet.");
    await user.click(screen.getByRole("button", { name: "+ New Judge" }));
    await user.type(screen.getByPlaceholderText("answer-faithfulness"), "dup");
    await user.type(screen.getByPlaceholderText(/faithfully answer/), "Rate ");
    await user.click(screen.getByRole("button", { name: "{{ outputs }}" }));
    await user.click(screen.getByRole("button", { name: "Create judge" }));
    expect(await screen.findByText("duplicate judge name")).toBeInTheDocument();
  });

  it("shows a load-failure banner when the judges list request fails", async () => {
    server.use(
      http.get(`${API_BASE}/judges`, () =>
        HttpResponse.json({ detail: "database unavailable" }, { status: 500 }),
      ),
    );
    renderPage();
    expect(await screen.findByText("Failed to load judges")).toBeInTheDocument();
    expect(screen.getByText("database unavailable")).toBeInTheDocument();
  });

  it("flags invalid JSON typed into the Inputs field specifically", async () => {
    let testRunBody: unknown = null;
    server.use(
      http.get(`${API_BASE}/judges`, () => HttpResponse.json(envelope([makeJudge()]))),
      http.post(`${API_BASE}/judges/:id/test-run`, async ({ request }) => {
        testRunBody = await request.json();
        return HttpResponse.json(envelope({ score: 1, value: true, rationale: "ok" }));
      }),
    );
    const user = userEvent.setup();
    renderPage();
    await screen.findByText("answer-faithfulness");
    await user.click(screen.getByTestId("judge-try-JDG-1"));
    await user.type(screen.getByTestId("judge-try-outputs"), "Paris.");
    fireEvent.change(screen.getByTestId("judge-try-inputs"), {
      target: { value: "{broken" },
    });
    await user.click(screen.getByTestId("judge-try-run"));
    expect(await screen.findByText("Inputs is not valid JSON.")).toBeInTheDocument();
    expect(testRunBody).toBeNull();
  });

  it("cancels the create form, closes the playground, and drives the remaining playground/alignment controls", async () => {
    server.use(
      http.get(`${API_BASE}/judges`, () => HttpResponse.json(envelope([makeJudge()]))),
      http.get(`${API_BASE}/review-queues`, () => HttpResponse.json(envelope([]))),
    );
    const user = userEvent.setup();
    renderPage();
    await screen.findByText("answer-faithfulness");

    // Cancel dismisses the create-judge panel.
    await user.click(screen.getByRole("button", { name: "+ New Judge" }));
    expect(screen.getByRole("button", { name: "Create judge" })).toBeInTheDocument();
    // Two "Cancel" buttons exist while the panel is open: the toggle button
    // itself (which reads "Cancel" instead of "+ New Judge") and the panel's
    // own Cancel button — the panel's is the last one in the DOM.
    const cancelButtons = screen.getAllByRole("button", { name: "Cancel" });
    await user.click(cancelButtons[cancelButtons.length - 1]!);
    expect(screen.queryByRole("button", { name: "Create judge" })).not.toBeInTheDocument();

    // Fill in the Model, Description, and Return type fields directly.
    await user.click(screen.getByRole("button", { name: "+ New Judge" }));
    await user.clear(screen.getByPlaceholderText("openai:/gpt-5.6-luna"));
    await user.type(screen.getByPlaceholderText("openai:/gpt-5.6-luna"), "openai:/gpt-5");
    await user.type(
      screen.getByPlaceholderText(/Judges whether the answer is faithful/),
      "Custom description",
    );
    await user.selectOptions(screen.getByLabelText("Return type"), "int");
    expect(screen.getByPlaceholderText("openai:/gpt-5.6-luna")).toHaveValue(
      "openai:/gpt-5",
    );
    expect(screen.getByLabelText("Return type")).toHaveValue("int");

    // Open the playground, switch to alignment mode and back to "Try once".
    await user.click(screen.getByTestId("judge-try-JDG-1"));
    await user.click(screen.getByTestId("judge-mode-align"));
    expect(screen.getByTestId("align-run")).toBeInTheDocument();
    await user.click(screen.getByTestId("judge-mode-try"));
    expect(screen.getByTestId("judge-try-run")).toBeInTheDocument();

    // Re-enter alignment mode: add a row and flip a row's human label.
    await user.click(screen.getByTestId("judge-mode-align"));
    await user.click(screen.getByTestId("align-add-row"));
    expect(screen.getByTestId("align-output-2")).toBeInTheDocument();
    await user.selectOptions(
      screen.getByTestId("align-label-0"),
      "fail",
    );
    expect(screen.getByTestId("align-label-0")).toHaveValue("fail");

    // Close the playground.
    await user.click(screen.getByRole("button", { name: "Close" }));
    expect(screen.queryByTestId("judge-playground")).not.toBeInTheDocument();
  });
});
