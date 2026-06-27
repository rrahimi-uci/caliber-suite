import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { http, HttpResponse } from "msw";
import { afterAll, afterEach, beforeAll, describe, expect, it } from "vitest";
import { MemoryRouter, Route, Routes } from "react-router-dom";

import { AriaPlans } from "@/pages/AriaPlans";
import { server } from "@/test/server";

const API_BASE = "/ajax-api/2.0/mlflow/caliber";
const NOW = "2026-06-21T18:00:00Z";

function envelope<T>(data: T): { data: T } {
  return { data };
}

function makePlan(overrides: Record<string, unknown> = {}) {
  return {
    plan_id: "PLAN-1",
    session_id: null,
    project_id: null,
    goal: "Create a faithfulness judge",
    status: "draft" as const,
    autonomy: "approve_plan" as const,
    owner: "@reza",
    constraints: {},
    done_when: [],
    context_refs: [],
    created_at: NOW,
    updated_at: NOW,
    step_count: 1,
    ...overrides,
  };
}

function makeStep(overrides: Record<string, unknown> = {}) {
  return {
    step_id: "PSTEP-1",
    plan_id: "PLAN-1",
    seq: 0,
    capability_key: "judge.create",
    title: "Create judge",
    inputs: {},
    depends_on: [],
    status: "pending" as const,
    result: {},
    evidence: {},
    error: null,
    draft_id: null,
    job_id: null,
    approval_id: null,
    checkpoint_id: null,
    created_at: NOW,
    updated_at: NOW,
    ...overrides,
  };
}

function renderPage(): void {
  render(
    <MemoryRouter
      future={{ v7_startTransition: true, v7_relativeSplatPath: true }}
      initialEntries={["/aria/plans"]}
    >
      <Routes>
        <Route path="/aria/plans" element={<AriaPlans />} />
      </Routes>
    </MemoryRouter>,
  );
}

beforeAll(() => server.listen({ onUnhandledRequest: "error" }));
afterEach(() => server.resetHandlers());
afterAll(() => server.close());

describe("AriaPlans", () => {
  it("lists plans with status and step count", async () => {
    server.use(
      http.get(`${API_BASE}/aria/plans`, () =>
        HttpResponse.json(envelope([makePlan()])),
      ),
    );
    renderPage();
    expect(
      await screen.findByText("Create a faithfulness judge"),
    ).toBeInTheDocument();
    expect(screen.getByText("draft")).toBeInTheDocument();
    // The autonomy column renders the friendly label, not the raw enum.
    expect(screen.queryByText("approve_plan")).not.toBeInTheDocument();
    expect(
      screen.getAllByText("Review the plan, then run").length,
    ).toBeGreaterThan(0);
  });

  it("decomposes a goal and opens the new plan's step DAG", async () => {
    server.use(
      http.get(`${API_BASE}/aria/plans`, () => HttpResponse.json(envelope([]))),
      http.post(`${API_BASE}/aria/plans`, async ({ request }) => {
        const body = (await request.json()) as { goal: string };
        expect(body.goal).toContain("judge");
        return HttpResponse.json(
          envelope({ plan: makePlan(), steps: [makeStep()] }),
          { status: 201 },
        );
      }),
      http.get(`${API_BASE}/aria/plans/PLAN-1`, () =>
        HttpResponse.json(envelope({ plan: makePlan(), steps: [makeStep()] })),
      ),
      http.get(`${API_BASE}/aria/plans/PLAN-1/interactions`, () =>
        HttpResponse.json(envelope([])),
      ),
    );
    const user = userEvent.setup();
    renderPage();
    await screen.findByText("No plans yet — decompose a goal above.");

    await user.type(screen.getByLabelText("Goal"), "create a judge");
    await user.click(screen.getByRole("button", { name: "Decompose goal" }));

    // Lands on the detail view showing the decomposed step + its capability.
    await waitFor(() =>
      expect(screen.getByText("Plan steps")).toBeInTheDocument(),
    );
    expect(screen.getByText("judge.create")).toBeInTheDocument();
    expect(
      screen.getByRole("button", { name: "Approve plan" }),
    ).toBeInTheDocument();
  });

  it("approves a draft plan and reflects the approved status", async () => {
    let approved = false;
    server.use(
      http.get(`${API_BASE}/aria/plans`, () =>
        HttpResponse.json(envelope([makePlan()])),
      ),
      http.get(`${API_BASE}/aria/plans/PLAN-1`, () =>
        HttpResponse.json(
          envelope({
            plan: makePlan({ status: approved ? "approved" : "draft" }),
            steps: [makeStep()],
          }),
        ),
      ),
      http.get(`${API_BASE}/aria/plans/PLAN-1/interactions`, () =>
        HttpResponse.json(envelope([])),
      ),
      http.post(`${API_BASE}/aria/plans/PLAN-1/approve`, () => {
        approved = true;
        return HttpResponse.json(
          envelope({
            plan: makePlan({ status: "approved" }),
            steps: [makeStep()],
          }),
        );
      }),
    );
    const user = userEvent.setup();
    renderPage();

    await user.click(await screen.findByText("Create a faithfulness judge"));
    await user.click(
      await screen.findByRole("button", { name: "Approve plan" }),
    );

    // After approve + refetch, the draft-only actions disappear, Execute appears.
    await waitFor(() =>
      expect(
        screen.queryByRole("button", { name: "Approve plan" }),
      ).not.toBeInTheDocument(),
    );
    expect(screen.getByRole("button", { name: "Execute" })).toBeInTheDocument();
  });

  it("executes a plan, surfaces the interaction, and approves it to resume", async () => {
    let phase: "approved" | "paused" | "completed" = "approved";
    const interaction = {
      interaction_id: "ASK-1",
      plan_id: "PLAN-1",
      step_id: "PSTEP-1",
      kind: "permission" as const,
      prompt: "Approve step 1: Create judge (judge.create)?",
      options: [],
      evidence: {},
      required_scope: null,
      status: "pending" as const,
      response: {},
      responded_by: null,
      responded_at: null,
      created_at: NOW,
    };
    const planFor = () =>
      envelope({
        plan: makePlan({ status: phase }),
        steps: [
          makeStep({
            status: phase === "completed" ? "done" : "waiting_input",
          }),
        ],
      });
    server.use(
      http.get(`${API_BASE}/aria/plans`, () =>
        HttpResponse.json(envelope([makePlan({ status: "approved" })])),
      ),
      http.get(`${API_BASE}/aria/plans/PLAN-1`, () =>
        HttpResponse.json(planFor()),
      ),
      http.get(`${API_BASE}/aria/plans/PLAN-1/interactions`, () =>
        HttpResponse.json(envelope(phase === "paused" ? [interaction] : [])),
      ),
      http.post(`${API_BASE}/aria/plans/PLAN-1/execute`, () => {
        phase = "paused";
        return HttpResponse.json(planFor());
      }),
      http.post(`${API_BASE}/aria/interactions/ASK-1/answer`, () => {
        phase = "completed";
        return HttpResponse.json(planFor());
      }),
    );
    const user = userEvent.setup();
    renderPage();

    await user.click(await screen.findByText("Create a faithfulness judge"));
    await user.click(await screen.findByRole("button", { name: "Execute" }));

    // Paused → the approval card appears.
    await waitFor(() =>
      expect(screen.getByText("Aria needs your approval")).toBeInTheDocument(),
    );
    await user.click(screen.getByRole("button", { name: "Approve" }));

    // Answered → plan completes, the approval card clears.
    await waitFor(() =>
      expect(
        screen.queryByText("Aria needs your approval"),
      ).not.toBeInTheDocument(),
    );
    expect(screen.getByText("completed")).toBeInTheDocument();
  });
});
