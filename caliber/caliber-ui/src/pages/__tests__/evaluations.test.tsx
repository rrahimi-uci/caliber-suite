import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { http, HttpResponse } from "msw";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { afterAll, afterEach, beforeAll, describe, expect, it } from "vitest";

import { EvaluationDetail } from "@/pages/EvaluationDetail";
import { Evaluations } from "@/pages/Evaluations";
import { server } from "@/test/server";

const API_BASE = "/ajax-api/2.0/mlflow/caliber";

function envelope<T>(data: T): { data: T } {
  return { data };
}

const RUN_A = {
  run_id: "EVR-a",
  dataset_id: "ED-1",
  dataset_version: 2,
  label: "gpt baseline",
  predict_target: "llm",
  subject_ref: null,
  model: "gpt-4o-mini",
  scorers: ["exact_match"],
  pass_threshold: 0.5,
  n_examples: 2,
  passed_count: 1,
  failed_count: 1,
  overall_score: 0.5,
  pass_rate: 0.5,
  aggregate: { exact_match: 0.5 },
  status: "completed",
  error_message: null,
  created_by: "@me",
  created_at: "2026-01-01T00:00:00Z",
  completed_at: "2026-01-01T00:00:01Z",
};

const RUN_B = {
  ...RUN_A,
  run_id: "EVR-b",
  label: "older run",
  overall_score: 0.25,
  pass_rate: 0.5,
  aggregate: { exact_match: 0.25 },
};

const RUN_A_DETAIL = {
  ...RUN_A,
  results: [
    {
      example_id: "EX-1",
      input: { input: "capital of France" },
      expected: { expected: "Paris" },
      prediction: "Paris",
      scores: { exact_match: 1.0 },
      score: 1.0,
      passed: true,
      error: null,
    },
    {
      example_id: "EX-2",
      input: { input: "2+2" },
      expected: { expected: "4" },
      prediction: "I don't know",
      scores: { exact_match: 0.0 },
      score: 0.0,
      passed: false,
      error: null,
    },
  ],
};

const DATASETS = [
  {
    dataset_id: "ED-1",
    name: "factual-checks",
    description: "",
    owner: "@me",
    tags: [],
    status: "active",
    version: 2,
    created_at: "2026-01-01T00:00:00Z",
    updated_at: "2026-01-01T00:00:00Z",
  },
];

const JUDGES = [
  {
    judge_id: "JDG-tone",
    name: "tone-judge",
    description: "Rates politeness",
    instructions: "Is {{ outputs }} polite?",
    model: "openai:/gpt-4o-mini",
    feedback_value_type: "bool",
    owner: "@me",
    tags: [],
    status: "active",
    created_at: "2026-01-01T00:00:00Z",
    updated_at: "2026-01-01T00:00:00Z",
  },
];

let createBody: Record<string, unknown> | null = null;

function useHandlers(): void {
  createBody = null;
  server.use(
    http.get(`${API_BASE}/evaluations`, () => HttpResponse.json(envelope([RUN_A, RUN_B]))),
    http.get(`${API_BASE}/evaluations/:runId`, ({ params }) => {
      const detail = params.runId === "EVR-b" ? { ...RUN_B, results: [] } : RUN_A_DETAIL;
      return HttpResponse.json(envelope(detail));
    }),
    http.post(`${API_BASE}/evaluations`, async ({ request }) => {
      createBody = (await request.json()) as Record<string, unknown>;
      return HttpResponse.json(envelope(RUN_A_DETAIL), { status: 201 });
    }),
    http.get(`${API_BASE}/eval-datasets`, () => HttpResponse.json(envelope(DATASETS))),
    http.get(`${API_BASE}/judges`, () => HttpResponse.json(envelope(JUDGES))),
  );
}

function renderList(): void {
  render(
    <MemoryRouter
      future={{ v7_startTransition: true, v7_relativeSplatPath: true }}
      initialEntries={["/evaluations"]}
    >
      <Routes>
        <Route path="/evaluations" element={<Evaluations />} />
      </Routes>
    </MemoryRouter>,
  );
}

function renderDetail(runId = "EVR-a"): void {
  render(
    <MemoryRouter
      future={{ v7_startTransition: true, v7_relativeSplatPath: true }}
      initialEntries={[`/evaluations/${runId}`]}
    >
      <Routes>
        <Route path="/evaluations/:runId" element={<EvaluationDetail />} />
      </Routes>
    </MemoryRouter>,
  );
}

beforeAll(() => server.listen({ onUnhandledRequest: "error" }));
afterEach(() => server.resetHandlers());
afterAll(() => server.close());

describe("Evaluations page", () => {
  it("lists evaluation runs with scores", async () => {
    useHandlers();
    renderList();

    const rows = await screen.findAllByTestId("eval-run-row");
    expect(rows).toHaveLength(2);
    expect(rows[0]?.textContent).toContain("gpt baseline");
    expect(rows[0]?.textContent).toContain("50%");
  });

  it("runs an evaluation from the modal", async () => {
    useHandlers();
    renderList();

    await screen.findAllByTestId("eval-run-row");
    await userEvent.click(screen.getByRole("button", { name: /Run evaluation/ }));
    await userEvent.selectOptions(await screen.findByLabelText("Test set"), "ED-1");
    await userEvent.type(screen.getByLabelText("Label"), "smoke run");
    await userEvent.click(screen.getByRole("button", { name: /^Run$/ }));

    await waitFor(() => {
      expect(createBody).toMatchObject({ dataset_id: "ED-1", label: "smoke run" });
    });
    expect(createBody?.scorers).toEqual(["exact_match", "token_f1", "contains_expected"]);
  });

  it("offers custom LLM judges as graders and includes the selected one", async () => {
    useHandlers();
    renderList();

    await screen.findAllByTestId("eval-run-row");
    await userEvent.click(screen.getByRole("button", { name: /Run evaluation/ }));
    await userEvent.selectOptions(await screen.findByLabelText("Test set"), "ED-1");

    // The authored judge appears as a selectable grader.
    const judgeChip = await screen.findByTestId("judge-scorer-JDG-tone");
    expect(judgeChip).toHaveTextContent("tone-judge");
    await userEvent.click(judgeChip);
    await userEvent.click(screen.getByRole("button", { name: /^Run$/ }));

    await waitFor(() => expect(createBody).not.toBeNull());
    // The judge rides through as a ``Judge.<id>`` scorer token.
    expect(createBody?.scorers).toContain("Judge.JDG-tone");
  });

  it("scores a real artifact (prompt version) via predict_target + subject_ref", async () => {
    useHandlers();
    renderList();

    await screen.findAllByTestId("eval-run-row");
    await userEvent.click(screen.getByRole("button", { name: /Run evaluation/ }));
    await userEvent.selectOptions(await screen.findByLabelText("Test set"), "ED-1");
    await userEvent.selectOptions(screen.getByTestId("eval-predict-target"), "prompt");
    await userEvent.type(screen.getByTestId("eval-subject-ref"), "support-greeting@3");
    await userEvent.click(screen.getByRole("button", { name: /^Run$/ }));

    await waitFor(() => expect(createBody).not.toBeNull());
    expect(createBody).toMatchObject({
      dataset_id: "ED-1",
      predict_target: "prompt",
      subject_ref: "support-greeting@3",
    });
  });
});

describe("EvaluationDetail page", () => {
  it("renders the scorecard aggregate and per-example rows", async () => {
    useHandlers();
    renderDetail();

    const aggregate = await screen.findByTestId("eval-aggregate");
    expect(within(aggregate).getByText("Overall")).toBeInTheDocument();
    // Overall 50% appears in the aggregate.
    expect(within(aggregate).getAllByText("50%").length).toBeGreaterThan(0);

    const rows = await screen.findAllByTestId("eval-result-row");
    expect(rows).toHaveLength(2);
    expect(rows[0]?.textContent).toContain("Paris");
    expect(rows[0]?.textContent).toContain("Pass");
    expect(rows[1]?.textContent).toContain("Fail");
  });

  it("shows deltas when compared against a baseline run", async () => {
    useHandlers();
    renderDetail();

    await screen.findByTestId("eval-aggregate");
    await userEvent.selectOptions(
      screen.getByLabelText("Compare to baseline run"),
      "EVR-b",
    );
    // Overall 0.5 vs baseline 0.25 → +25pp.
    await waitFor(() => {
      expect(screen.getAllByText("+25pp").length).toBeGreaterThan(0);
    });
  });

  it("explains weighted coverage and preserves prediction evidence when a scorer fails", async () => {
    useHandlers();
    const evidenceRun = {
      ...RUN_A,
      scorers: ["exact_match", "Judge.JDG-tone"],
      aggregate: { exact_match: 2 / 3, "Judge.JDG-tone": 1 },
      overall_score: 5 / 6,
      pass_rate: 2 / 3,
      results: [
        {
          example_id: "EX-weighted",
          input: { input: "capital of France" },
          expected: { expected: "Paris" },
          prediction: "Paris",
          scores: { exact_match: 1, "Judge.JDG-tone": 1 },
          score: 1,
          passed: true,
          error: null,
          weight: 2,
          tags: ["golden", "geography"],
        },
        {
          example_id: "EX-incomplete",
          input: { input: "spell four" },
          expected: { expected: "four" },
          prediction: "Four",
          scores: { exact_match: 0 },
          score: 0,
          passed: false,
          error: "Judge.JDG-tone: provider unavailable",
          weight: 1,
          tags: ["edge"],
        },
      ],
    };
    server.use(
      http.get(`${API_BASE}/evaluations/:runId`, () =>
        HttpResponse.json(envelope(evidenceRun)),
      ),
    );
    renderDetail();

    const aggregate = await screen.findByTestId("eval-aggregate");
    expect(within(aggregate).getByText("Weighted pass rate")).toBeInTheDocument();
    expect(
      within(aggregate).getByText("1/2 raw rows · 2.00/3.00 passing weight"),
    ).toBeInTheDocument();
    expect(
      within(aggregate).getAllByText("valid 1/2 rows · 2.00/3.00 weight"),
    ).toHaveLength(2);

    // The custom judge is readable in both the aggregate card and table header.
    expect(screen.getAllByText(/tone-judge/).length).toBeGreaterThanOrEqual(2);
    expect(screen.getAllByTestId("eval-result-tag").map((tag) => tag.textContent)).toEqual([
      "golden",
      "geography",
      "edge",
    ]);

    const rows = await screen.findAllByTestId("eval-result-row");
    expect(rows[1]).toHaveTextContent("Four");
    expect(rows[1]).toHaveTextContent("Judge.JDG-tone: provider unavailable");
    expect(within(rows[1]).getByText("Incomplete")).toBeInTheDocument();
    expect(screen.getByTestId("eval-incomplete-warning")).toHaveTextContent(
      "Surviving raw scorer values remain visible",
    );
  });

  it("labels the all-zero equal-row fallback instead of implying row exclusion", async () => {
    useHandlers();
    const zeroWeightRun = {
      ...RUN_A_DETAIL,
      results: RUN_A_DETAIL.results.map((row) => ({ ...row, weight: 0, tags: [] })),
    };
    server.use(
      http.get(`${API_BASE}/evaluations/:runId`, () =>
        HttpResponse.json(envelope(zeroWeightRun)),
      ),
    );
    renderDetail();

    expect(await screen.findByTestId("eval-zero-weight-warning")).toHaveTextContent(
      "legacy run predates zero-total validation",
    );
    const aggregate = screen.getByTestId("eval-aggregate");
    expect(
      within(aggregate).getByText("Equal-row fallback · stored total weight 0.00"),
    ).toBeInTheDocument();
    expect(
      within(aggregate).getByText("1/2 rows passed · equal-row fallback"),
    ).toBeInTheDocument();
    expect(screen.getByRole("columnheader", { name: "Weight" })).toBeInTheDocument();
  });

  it("offers only compatible completed baselines and discloses changed variables", async () => {
    useHandlers();
    const changedCandidate = {
      ...RUN_B,
      run_id: "EVR-changed",
      label: "prompt candidate",
      predict_target: "prompt",
      subject_ref: "support-greeting@3",
      model: "gpt-4.1-mini",
    };
    const wrongVersion = {
      ...RUN_B,
      run_id: "EVR-old-version",
      label: "old snapshot",
      dataset_version: 1,
    };
    const wrongScorers = {
      ...RUN_B,
      run_id: "EVR-wrong-scorers",
      label: "wrong graders",
      scorers: ["token_f1"],
    };
    const failedRun = {
      ...RUN_B,
      run_id: "EVR-failed",
      label: "failed baseline",
      status: "failed",
    };
    server.use(
      http.get(`${API_BASE}/evaluations`, () =>
        HttpResponse.json(
          envelope([RUN_A, changedCandidate, wrongVersion, wrongScorers, failedRun]),
        ),
      ),
      http.get(`${API_BASE}/evaluations/:runId`, ({ params }) =>
        HttpResponse.json(
          envelope(
            params.runId === "EVR-changed"
              ? { ...changedCandidate, results: [] }
              : RUN_A_DETAIL,
          ),
        ),
      ),
    );
    renderDetail();

    const select = await screen.findByLabelText("Compare to baseline run");
    expect(
      within(select).getByRole("option", { name: /prompt candidate/i }),
    ).toBeInTheDocument();
    expect(within(select).queryByRole("option", { name: /old snapshot/i })).toBeNull();
    expect(within(select).queryByRole("option", { name: /wrong graders/i })).toBeNull();
    expect(within(select).queryByRole("option", { name: /failed baseline/i })).toBeNull();

    await userEvent.selectOptions(select, "EVR-changed");
    const context = await screen.findByTestId("baseline-comparison-context");
    expect(context).toHaveTextContent("dataset v2 and grader suite match");
    expect(context).toHaveTextContent(
      "Current: target llm, subject (none), model gpt-4o-mini",
    );
    expect(context).toHaveTextContent(
      "Baseline: target prompt, subject support-greeting@3, model gpt-4.1-mini",
    );
  });
});
