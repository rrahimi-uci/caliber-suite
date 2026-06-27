import { render, screen, waitFor } from "@testing-library/react";
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
    expect(body.questions[0].key).toBe("polite");
  });
});
