import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { http, HttpResponse } from "msw";
import { afterAll, afterEach, beforeAll, describe, expect, it } from "vitest";
import { MemoryRouter } from "react-router-dom";

import type { AriaPlanDetail } from "@/api/types";
import { AriaPlanCard } from "@/components/assistant/AriaPlanCard";
import { server } from "@/test/server";

const API_BASE = "/ajax-api/2.0/mlflow/caliber";
const NOW = "2026-06-21T18:00:00Z";

function envelope<T>(data: T): { data: T } {
  return { data };
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

function makeDetail(
  planOverrides: Record<string, unknown> = {},
  steps = [makeStep()],
): AriaPlanDetail {
  return {
    plan: {
      plan_id: "PLAN-1",
      session_id: "SESS-1",
      project_id: null,
      goal: "Create a faithfulness judge",
      status: "draft",
      autonomy: "approve_plan",
      owner: "@reza",
      constraints: {},
      done_when: [],
      context_refs: [],
      created_at: NOW,
      updated_at: NOW,
      step_count: steps.length,
      ...planOverrides,
    },
    steps,
  } as AriaPlanDetail;
}

function makeInteraction(overrides: Record<string, unknown> = {}) {
  return {
    interaction_id: "PINT-1",
    plan_id: "PLAN-1",
    step_id: "PSTEP-1",
    kind: "permission" as const,
    prompt: "Approve creating the judge?",
    options: [],
    evidence: {},
    required_scope: null,
    status: "pending" as const,
    response: {},
    responded_by: null,
    responded_at: null,
    created_at: NOW,
    ...overrides,
  };
}

function renderCard(detail: AriaPlanDetail): void {
  render(
    <MemoryRouter
      future={{ v7_startTransition: true, v7_relativeSplatPath: true }}
    >
      <AriaPlanCard initialDetail={detail} />
    </MemoryRouter>,
  );
}

beforeAll(() => server.listen({ onUnhandledRequest: "error" }));
afterEach(() => server.resetHandlers());
afterAll(() => server.close());

describe("AriaPlanCard", () => {
  it("renders a draft plan's goal and steps with an approve & run action", async () => {
    server.use(
      http.get(`${API_BASE}/aria/plans/PLAN-1/interactions`, () =>
        HttpResponse.json(envelope([])),
      ),
    );
    renderCard(makeDetail());

    expect(
      await screen.findByText("Create a faithfulness judge"),
    ).toBeInTheDocument();
    expect(screen.getByText("Create judge")).toBeInTheDocument();
    expect(screen.getByText("judge.create")).toBeInTheDocument();
    expect(
      screen.getByRole("button", { name: /approve & run/i }),
    ).toBeInTheDocument();
  });

  it("approve & run drives the plan to completion in-thread", async () => {
    const doneSteps = [
      makeStep({ status: "done", result: { judge_id: "JDG-1" } }),
    ];
    let approved = false;
    let executed = false;
    server.use(
      http.get(`${API_BASE}/aria/plans/PLAN-1/interactions`, () =>
        HttpResponse.json(envelope([])),
      ),
      http.post(`${API_BASE}/aria/plans/PLAN-1/approve`, () => {
        approved = true;
        return HttpResponse.json(envelope(makeDetail({ status: "approved" })));
      }),
      http.post(`${API_BASE}/aria/plans/PLAN-1/execute`, () => {
        executed = true;
        return HttpResponse.json(
          envelope(makeDetail({ status: "completed" }, doneSteps)),
        );
      }),
    );
    renderCard(makeDetail());

    await userEvent.click(
      await screen.findByRole("button", { name: /approve & run/i }),
    );

    expect(await screen.findByText(/all steps complete/i)).toBeInTheDocument();
    expect(approved).toBe(true);
    expect(executed).toBe(true);
  });

  it("surfaces a mid-run interaction inline and resumes when answered", async () => {
    let answered = false;
    server.use(
      http.get(`${API_BASE}/aria/plans/PLAN-1/interactions`, () =>
        HttpResponse.json(envelope(answered ? [] : [makeInteraction()])),
      ),
      http.post(`${API_BASE}/aria/interactions/PINT-1/answer`, () => {
        answered = true;
        return HttpResponse.json(
          envelope(
            makeDetail({ status: "completed" }, [makeStep({ status: "done" })]),
          ),
        );
      }),
    );
    renderCard(
      makeDetail({ status: "paused" }, [makeStep({ status: "waiting_input" })]),
    );

    expect(
      await screen.findByText(/aria needs your approval/i),
    ).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: /^approve$/i }));

    await waitFor(() => expect(answered).toBe(true));
    expect(await screen.findByText(/all steps complete/i)).toBeInTheDocument();
  });

  it("explains a below-gate confirm with the evidence and accept/reject", async () => {
    server.use(
      http.get(`${API_BASE}/aria/plans/PLAN-1/interactions`, () =>
        HttpResponse.json(
          envelope([
            makeInteraction({
              kind: "confirm",
              prompt:
                "Step 1 scored 0.72 below the gate (faithfulness >= 0.9). Accept anyway?",
              evidence: { metric: "faithfulness", min: 0.9, value: 0.72 },
            }),
          ]),
        ),
      ),
    );
    renderCard(
      makeDetail({ status: "paused" }, [makeStep({ status: "waiting_input" })]),
    );

    expect(
      await screen.findByText(/aria needs you to confirm/i),
    ).toBeInTheDocument();
    expect(screen.getByText(/below quality gate/i)).toBeInTheDocument();
    expect(screen.getByText(/faithfulness: 0.72/)).toBeInTheDocument();
    expect(screen.getByText(/needs ≥ 0.9/)).toBeInTheDocument();
    expect(
      screen.getByRole("button", { name: /^accept$/i }),
    ).toBeInTheDocument();
    expect(
      screen.getByRole("button", { name: /^reject$/i }),
    ).toBeInTheDocument();
  });

  it("spells out separation of duties on a gated permission", async () => {
    server.use(
      http.get(`${API_BASE}/aria/plans/PLAN-1/interactions`, () =>
        HttpResponse.json(
          envelope([
            makeInteraction({ kind: "permission", required_scope: "approver" }),
          ]),
        ),
      ),
    );
    renderCard(
      makeDetail({ status: "paused" }, [makeStep({ status: "waiting_input" })]),
    );

    expect(
      await screen.findByText(/separation of duties/i),
    ).toBeInTheDocument();
    expect(screen.getByText(/requires approver/i)).toBeInTheDocument();
    expect(
      screen.getByRole("button", { name: /^approve$/i }),
    ).toBeInTheDocument();
  });
});
