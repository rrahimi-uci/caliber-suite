import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { http, HttpResponse } from "msw";
import { afterAll, afterEach, beforeAll, describe, expect, it } from "vitest";
import { MemoryRouter, Route, Routes } from "react-router-dom";

import { ReviewQueues } from "@/pages/ReviewQueues";
import { server } from "@/test/server";

const API_BASE = "/ajax-api/2.0/mlflow/caliber";
const NOW = "2026-06-21T18:00:00Z";

function envelope<T>(data: T): { data: T } {
  return { data };
}

const QUESTIONS = [
  {
    key: "correct",
    title: "Is the answer correct?",
    type: "pass_fail",
    options: [],
    required: true,
    target: "feedback",
  },
];

function makeQueue(overrides: Record<string, unknown> = {}) {
  return {
    queue_id: "RVQ-1",
    name: "answer-quality",
    description: "Human review of answers.",
    questions: QUESTIONS,
    reviewers: ["@sarah"],
    owner: "@sarah",
    status: "active" as const,
    created_at: NOW,
    updated_at: NOW,
    item_count: 1,
    pending_count: 1,
    ...overrides,
  };
}

function makeItem(overrides: Record<string, unknown> = {}) {
  return {
    item_id: "RVI-1",
    queue_id: "RVQ-1",
    trace_id: "tr-abc",
    experiment_id: null,
    status: "pending" as const,
    assigned_to: null,
    answers: {},
    assessment_ids: [],
    created_at: NOW,
    completed_at: null,
    completed_by: null,
    ...overrides,
  };
}

function renderPage(): void {
  render(
    <MemoryRouter
      future={{ v7_startTransition: true, v7_relativeSplatPath: true }}
      initialEntries={["/review-queues"]}
    >
      <Routes>
        <Route path="/review-queues" element={<ReviewQueues />} />
      </Routes>
    </MemoryRouter>,
  );
}

beforeAll(() => server.listen({ onUnhandledRequest: "error" }));
afterEach(() => server.resetHandlers());
afterAll(() => server.close());

describe("ReviewQueues", () => {
  it("lists queues with review progress", async () => {
    server.use(
      http.get(`${API_BASE}/review-queues`, () =>
        HttpResponse.json(envelope([makeQueue()])),
      ),
    );
    renderPage();
    expect(await screen.findByText("answer-quality")).toBeInTheDocument();
    expect(screen.getByText("0/1 reviewed")).toBeInTheDocument();
  });

  it("opens a queue and submits a review that writes back to the trace", async () => {
    let submitted = false;
    server.use(
      http.get(`${API_BASE}/review-queues`, () =>
        HttpResponse.json(envelope([makeQueue()])),
      ),
      http.get(`${API_BASE}/review-queues/RVQ-1`, () =>
        HttpResponse.json(
          envelope({
            queue: makeQueue(),
            items: [
              submitted
                ? makeItem({
                    status: "completed",
                    completed_by: "@test",
                    assessment_ids: ["asmt-1"],
                  })
                : makeItem(),
            ],
          }),
        ),
      ),
      http.post(
        `${API_BASE}/review-queues/RVQ-1/items/RVI-1/submit`,
        async ({ request }) => {
          const body = (await request.json()) as { answers: Record<string, unknown> };
          expect(body.answers.correct).toBe(true);
          submitted = true;
          return HttpResponse.json(
            envelope(makeItem({ status: "completed", assessment_ids: ["asmt-1"] })),
          );
        },
      ),
    );

    const user = userEvent.setup();
    renderPage();

    // Open the queue from the list.
    await user.click(await screen.findByRole("button", { name: "Open" }));

    // The review form auto-selects the pending item; answer Pass and submit.
    await user.click(await screen.findByRole("button", { name: "Pass" }));
    await user.click(screen.getByRole("button", { name: "Submit review" }));

    // After submit + refetch, the item flips to completed.
    await waitFor(() =>
      expect(screen.getByText("All items reviewed. 🎉")).toBeInTheDocument(),
    );
  });

  it("builds a queue with a custom question and creates it", async () => {
    let createdBody: Record<string, unknown> | null = null;
    server.use(
      http.get(`${API_BASE}/review-queues`, () => HttpResponse.json(envelope([]))),
      http.post(`${API_BASE}/review-queues`, async ({ request }) => {
        createdBody = (await request.json()) as Record<string, unknown>;
        return HttpResponse.json(envelope(makeQueue()), { status: 201 });
      }),
    );
    const user = userEvent.setup();
    renderPage();
    await screen.findByText("No review queues yet.");

    await user.click(screen.getByRole("button", { name: "+ New Queue" }));
    await user.type(screen.getByPlaceholderText("answer-quality"), "tone-review");
    await user.type(screen.getByLabelText("Question 1 key"), "polite");
    await user.type(
      screen.getByLabelText("Question 1 title"),
      "Was the reply polite?",
    );
    await user.click(screen.getByRole("button", { name: "Create queue" }));

    await waitFor(() => expect(createdBody).not.toBeNull());
    const body = createdBody as unknown as {
      name: string;
      questions: Array<{ key: string }>;
    };
    expect(body.name).toBe("tone-review");
    expect(body.questions[0]!.key).toBe("polite");
  });

  it("shows '—' fallbacks and 'no items' for a queue with no description/reviewers/items", async () => {
    server.use(
      http.get(`${API_BASE}/review-queues`, () =>
        HttpResponse.json(
          envelope([
            makeQueue({
              description: "",
              reviewers: [],
              item_count: 0,
              pending_count: 0,
            }),
          ]),
        ),
      ),
    );
    renderPage();
    await screen.findByText("answer-quality");
    expect(screen.getAllByText("—")).toHaveLength(2); // description + reviewers
    expect(screen.getByText("no items")).toBeInTheDocument();
  });

  it("shows a fully-reviewed queue's progress pill distinctly from an in-progress one", async () => {
    server.use(
      http.get(`${API_BASE}/review-queues`, () =>
        HttpResponse.json(
          envelope([
            makeQueue({ queue_id: "RVQ-1", item_count: 2, pending_count: 1 }),
            makeQueue({
              queue_id: "RVQ-2",
              name: "fully-done",
              item_count: 2,
              pending_count: 0,
            }),
          ]),
        ),
      ),
    );
    renderPage();
    expect(await screen.findByText("1/2 reviewed")).toBeInTheDocument();
    expect(screen.getByText("2/2 reviewed")).toBeInTheDocument();
  });

  it("opens a queue by clicking its name (not just the Open link)", async () => {
    server.use(
      http.get(`${API_BASE}/review-queues`, () =>
        HttpResponse.json(envelope([makeQueue()])),
      ),
      http.get(`${API_BASE}/review-queues/RVQ-1`, () =>
        HttpResponse.json(envelope({ queue: makeQueue(), items: [] })),
      ),
    );
    const user = userEvent.setup();
    renderPage();
    await user.click(await screen.findByRole("button", { name: "answer-quality" }));
    expect(await screen.findByText("Enqueue traces, then review them here.")).toBeInTheDocument();
  });

  it("navigates back to the list from a queue's detail view", async () => {
    server.use(
      http.get(`${API_BASE}/review-queues`, () =>
        HttpResponse.json(envelope([makeQueue()])),
      ),
      http.get(`${API_BASE}/review-queues/RVQ-1`, () =>
        HttpResponse.json(envelope({ queue: makeQueue(), items: [] })),
      ),
    );
    const user = userEvent.setup();
    renderPage();
    await user.click(await screen.findByRole("button", { name: "Open" }));
    await screen.findByText("Enqueue traces, then review them here.");

    await user.click(screen.getByRole("button", { name: "Review Queues" }));
    expect(await screen.findByRole("button", { name: "+ New Queue" })).toBeInTheDocument();
    expect(
      screen.queryByText("Enqueue traces, then review them here."),
    ).not.toBeInTheDocument();
  });

  it("cancels the create-queue panel, adds/removes a question, and edits its type/target/options", async () => {
    server.use(
      http.get(`${API_BASE}/review-queues`, () => HttpResponse.json(envelope([]))),
    );
    const user = userEvent.setup();
    renderPage();
    await screen.findByText("No review queues yet.");

    await user.click(screen.getByRole("button", { name: "+ New Queue" }));
    // Two "Cancel"-labeled buttons exist while the panel is open: the header
    // toggle (which flips to "Cancel") and the panel's own Cancel button.
    const cancelButtons = screen.getAllByRole("button", { name: "Cancel" });
    await user.click(cancelButtons[cancelButtons.length - 1]!);
    expect(screen.queryByRole("button", { name: "Create queue" })).not.toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: "+ New Queue" }));
    await user.click(screen.getByRole("button", { name: "+ Add question" }));
    expect(screen.getByLabelText("Question 2 key")).toBeInTheDocument();

    await user.selectOptions(screen.getByLabelText("Question 1 type"), "categorical");
    await user.selectOptions(screen.getByLabelText("Question 1 target"), "expectation");
    expect(screen.getByLabelText("Question 1 target")).toHaveValue("expectation");
    fireEvent.change(screen.getByLabelText("Question 1 options"), {
      target: { value: "none, hallucination, refusal" },
    });

    await user.click(screen.getByRole("button", { name: "Remove question 2" }));
    expect(screen.queryByLabelText("Question 2 key")).not.toBeInTheDocument();

    let createdBody: Record<string, unknown> | null = null;
    server.use(
      http.post(`${API_BASE}/review-queues`, async ({ request }) => {
        createdBody = (await request.json()) as Record<string, unknown>;
        return HttpResponse.json(envelope(makeQueue()), { status: 201 });
      }),
    );
    await user.type(screen.getByPlaceholderText("answer-quality"), "harm-check");
    await user.type(screen.getByLabelText("Question 1 key"), "category");
    await user.type(screen.getByLabelText("Question 1 title"), "Which category?");
    await user.click(screen.getByRole("button", { name: "Create queue" }));

    await waitFor(() => expect(createdBody).not.toBeNull());
    const body = createdBody as unknown as {
      questions: Array<{ options: string[]; target: string }>;
    };
    expect(body.questions[0]!.target).toBe("expectation");
    expect(body.questions[0]!.options).toEqual(["none", "hallucination", "refusal"]);
  });

  it("shows the server error message when creating a queue fails", async () => {
    server.use(
      http.get(`${API_BASE}/review-queues`, () => HttpResponse.json(envelope([]))),
      http.post(`${API_BASE}/review-queues`, () =>
        HttpResponse.json({ detail: "duplicate queue name" }, { status: 409 }),
      ),
    );
    const user = userEvent.setup();
    renderPage();
    await screen.findByText("No review queues yet.");
    await user.click(screen.getByRole("button", { name: "+ New Queue" }));
    await user.type(screen.getByPlaceholderText("answer-quality"), "dup");
    await user.type(screen.getByLabelText("Question 1 key"), "k");
    await user.type(screen.getByLabelText("Question 1 title"), "t");
    await user.click(screen.getByRole("button", { name: "Create queue" }));
    expect(await screen.findByText("duplicate queue name")).toBeInTheDocument();
  });

  it("enqueues traces, clears the input on success, and surfaces failures", async () => {
    let addedBody: Record<string, unknown> | null = null;
    let shouldFail = false;
    server.use(
      http.get(`${API_BASE}/review-queues`, () =>
        HttpResponse.json(envelope([makeQueue()])),
      ),
      http.get(`${API_BASE}/review-queues/RVQ-1`, () =>
        HttpResponse.json(envelope({ queue: makeQueue({ description: "" }), items: [] })),
      ),
      http.post(`${API_BASE}/review-queues/RVQ-1/items`, async ({ request }) => {
        if (shouldFail) {
          return HttpResponse.json({ detail: "trace not found" }, { status: 404 });
        }
        addedBody = (await request.json()) as Record<string, unknown>;
        return HttpResponse.json(envelope([]), { status: 201 });
      }),
    );
    const user = userEvent.setup();
    renderPage();
    await user.click(await screen.findByRole("button", { name: "Open" }));
    await screen.findByText("Enqueue traces, then review them here.");
    // Blank description falls back to the em-dash.
    expect(screen.getByText("—")).toBeInTheDocument();

    // Enqueue is disabled until there's something to submit.
    expect(screen.getByRole("button", { name: "Enqueue" })).toBeDisabled();
    await user.type(
      screen.getByLabelText("Trace ids"),
      "tr-abc123, tr-def456 tr-ghi789",
    );
    await user.click(screen.getByRole("button", { name: "Enqueue" }));
    await waitFor(() => expect(addedBody).not.toBeNull());
    expect(addedBody).toEqual({
      trace_ids: ["tr-abc123", "tr-def456", "tr-ghi789"],
    });
    // The input is cleared after a successful enqueue.
    expect(screen.getByLabelText("Trace ids")).toHaveValue("");

    shouldFail = true;
    await user.type(screen.getByLabelText("Trace ids"), "tr-missing");
    await user.click(screen.getByRole("button", { name: "Enqueue" }));
    expect(await screen.findByText("trace not found")).toBeInTheDocument();
  });

  it("switches the selected item and answers categorical/numeric/text questions", async () => {
    const multiQuestions = [
      ...QUESTIONS,
      {
        key: "category",
        title: "Pick a category",
        type: "categorical",
        options: ["none", "hallucination", "refusal"],
        required: false,
        target: "feedback",
      },
      {
        key: "score",
        title: "Score 1-10",
        type: "numeric",
        options: [],
        required: false,
        target: "feedback",
      },
      {
        key: "notes",
        title: "Notes",
        type: "text",
        options: [],
        required: false,
        target: "expectation",
      },
    ];
    const multiQueue = makeQueue({ questions: multiQuestions });
    let submittedAnswers: Record<string, unknown> | null = null;
    server.use(
      http.get(`${API_BASE}/review-queues`, () =>
        HttpResponse.json(envelope([multiQueue])),
      ),
      http.get(`${API_BASE}/review-queues/RVQ-1`, () =>
        HttpResponse.json(
          envelope({
            queue: multiQueue,
            items: [
              makeItem({ item_id: "RVI-1", trace_id: "tr-1" }),
              makeItem({ item_id: "RVI-2", trace_id: "tr-2" }),
            ],
          }),
        ),
      ),
      http.post(
        `${API_BASE}/review-queues/RVQ-1/items/RVI-2/submit`,
        async ({ request }) => {
          const body = (await request.json()) as { answers: Record<string, unknown> };
          submittedAnswers = body.answers;
          return HttpResponse.json(
            envelope(makeItem({ item_id: "RVI-2", status: "completed" })),
          );
        },
      ),
    );
    const user = userEvent.setup();
    renderPage();
    await user.click(await screen.findByRole("button", { name: "Open" }));

    // Two pending items -> the form auto-selects the first; switch to the second.
    // (The switch is verified implicitly below: submitting posts to
    // RVI-2/submit, the only mocked submit endpoint, which only succeeds if
    // clicking the "tr-2" row actually changed the selected item.)
    // "tr-1" (the auto-selected first item) appears twice once items load --
    // once in the item list, once in its own detail panel -- so wait for
    // the list to render via `findAllByText` rather than the ambiguous
    // singular `findByText`.
    await screen.findAllByText("tr-1");
    await user.click(screen.getByText("tr-2"));

    await user.click(screen.getByRole("button", { name: "Pass" }));
    await user.selectOptions(
      screen.getByLabelText("Pick a category"),
      "hallucination",
    );
    const numberInput = screen.getByLabelText("Score 1-10");
    fireEvent.change(numberInput, { target: { value: "7" } });
    // Clearing the numeric field back to blank stores null, not NaN/0 — verify
    // before restoring the value, so the rest of the flow still submits 7.
    fireEvent.change(numberInput, { target: { value: "" } });
    expect(numberInput).toHaveValue(null);
    fireEvent.change(numberInput, { target: { value: "7" } });
    await user.type(screen.getByLabelText("Notes"), "looks fine");

    await user.click(screen.getByRole("button", { name: "Submit review" }));
    await waitFor(() => expect(submittedAnswers).not.toBeNull());
    expect(submittedAnswers).toMatchObject({
      correct: true,
      category: "hallucination",
      score: 7,
      notes: "looks fine",
    });
  });

  it("shows the completed item's read-only summary and a submit failure message", async () => {
    server.use(
      http.get(`${API_BASE}/review-queues`, () =>
        HttpResponse.json(envelope([makeQueue({ pending_count: 0 })])),
      ),
      http.get(`${API_BASE}/review-queues/RVQ-1`, () =>
        HttpResponse.json(
          envelope({
            queue: makeQueue(),
            items: [
              makeItem({
                status: "completed",
                completed_by: "@sarah",
                assessment_ids: ["asmt-1", "asmt-2"],
                answers: { correct: true },
              }),
            ],
          }),
        ),
      ),
    );
    const user = userEvent.setup();
    renderPage();
    await user.click(await screen.findByRole("button", { name: "Open" }));
    // No item is pending, so nothing auto-selects; click the completed item.
    await user.click(await screen.findByText("tr-abc"));
    expect(
      await screen.findByText(/Reviewed by @sarah/),
    ).toHaveTextContent("wrote 2 assessment(s) back to the trace.");
    expect(screen.queryByRole("button", { name: "Submit review" })).not.toBeInTheDocument();
  });

  it("surfaces a server error when submitting a review fails", async () => {
    server.use(
      http.get(`${API_BASE}/review-queues`, () =>
        HttpResponse.json(envelope([makeQueue()])),
      ),
      http.get(`${API_BASE}/review-queues/RVQ-1`, () =>
        HttpResponse.json(envelope({ queue: makeQueue(), items: [makeItem()] })),
      ),
      http.post(`${API_BASE}/review-queues/RVQ-1/items/RVI-1/submit`, () =>
        HttpResponse.json({ detail: "assessment write failed" }, { status: 500 }),
      ),
    );
    const user = userEvent.setup();
    renderPage();
    await user.click(await screen.findByRole("button", { name: "Open" }));
    await user.click(await screen.findByRole("button", { name: "Pass" }));
    await user.click(screen.getByRole("button", { name: "Submit review" }));
    expect(await screen.findByText("assessment write failed")).toBeInTheDocument();
  });
});
