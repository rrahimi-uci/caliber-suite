import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { http, HttpResponse } from "msw";
import { MemoryRouter } from "react-router-dom";
import {
  afterAll,
  afterEach,
  beforeAll,
  beforeEach,
  describe,
  expect,
  it,
  vi,
} from "vitest";

import { Releases } from "@/pages/Releases";
import { server } from "@/test/server";

const API_BASE = "/ajax-api/2.0/mlflow/caliber";

function envelope<T>(data: T): { data: T } {
  return { data };
}

function renderPage(): void {
  const qc = new QueryClient({
    defaultOptions: { queries: { retry: false, gcTime: 0 } },
  });
  render(
    <QueryClientProvider client={qc}>
      <MemoryRouter>
        <Releases />
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

beforeAll(() => server.listen({ onUnhandledRequest: "error" }));
beforeEach(() => {
  server.use(
    http.get(`${API_BASE}/releases/candidates`, () =>
      HttpResponse.json(envelope([])),
    ),
    http.get(`${API_BASE}/releases/operations`, () =>
      HttpResponse.json(envelope([])),
    ),
    http.get(`${API_BASE}/system/effects`, () =>
      HttpResponse.json(
        envelope({
          effects: [],
          status: "in_progress",
          resolutions: ["retry", "skip"],
        }),
      ),
    ),
    http.get(`${API_BASE}/system/webhook-dead-letters`, () =>
      HttpResponse.json(
        envelope({ dead_letters: [], status: "open", open_count: 0 }),
      ),
    ),
  );
});
afterEach(() => server.resetHandlers());
afterAll(() => server.close());

describe("Releases", () => {
  it("creates, signs, and generates Allure evidence for a release candidate", async () => {
    let candidate: Record<string, unknown> | null = null;
    let signoffBody: Record<string, unknown> | null = null;
    server.use(
      http.get(`${API_BASE}/releases/live`, () => HttpResponse.json(envelope([]))),
      http.get(`${API_BASE}/releases/timeline`, () => HttpResponse.json(envelope([]))),
      http.get(`${API_BASE}/releases/candidates`, () =>
        HttpResponse.json(envelope(candidate ? [candidate] : [])),
      ),
      http.post(`${API_BASE}/releases/candidates`, async ({ request }) => {
        const body = (await request.json()) as Record<string, unknown>;
        candidate = {
          ...body,
          candidate_id: "RC-1",
          project_id: null,
          visibility: "user",
          waivers: [],
          weighted_score: 0.95,
          blockers: [],
          status: "ready",
          owner: "@test",
          created_at: "2026-08-04T00:00:00Z",
          updated_at: "2026-08-04T00:00:00Z",
        };
        return HttpResponse.json(envelope(candidate), { status: 201 });
      }),
      http.post(`${API_BASE}/releases/candidates/RC-1/signoffs`, async ({ request }) => {
        signoffBody = (await request.json()) as Record<string, unknown>;
        candidate = { ...candidate, status: "signed_go" };
        return HttpResponse.json(
          envelope({
            signoff_id: "RSO-1",
            candidate_id: "RC-1",
            decision: "go",
            rationale: signoffBody.rationale,
            decided_by: "@test",
            candidate_snapshot: candidate,
            created_at: "2026-08-04T00:00:00Z",
          }),
          { status: 201 },
        );
      }),
      http.post(`${API_BASE}/releases/candidates/RC-1/reports`, () =>
        HttpResponse.json(
          envelope({
            report_job_id: "RRJ-1",
            candidate_id: "RC-1",
            status: "completed",
            format: "allure-json",
            report: { results: [] },
            error_message: null,
            created_by: "@test",
            created_at: "2026-08-04T00:00:00Z",
            completed_at: "2026-08-04T00:00:01Z",
          }),
          { status: 201 },
        ),
      ),
    );
    renderPage();
    fireEvent.click(await screen.findByRole("button", { name: "New candidate" }));
    fireEvent.change(screen.getByTestId("release-candidate-name"), {
      target: { value: "Support v7" },
    });
    fireEvent.change(screen.getByTestId("release-candidate-artifact"), {
      target: { value: "WF-support" },
    });
    fireEvent.change(screen.getByTestId("release-candidate-version"), {
      target: { value: "WFV-7" },
    });
    fireEvent.change(screen.getByTestId("release-candidate-rollback"), {
      target: { value: "WFV-6" },
    });
    fireEvent.click(screen.getByTestId("release-candidate-create"));
    const card = await screen.findByTestId("release-candidate-RC-1");
    expect(card).toHaveTextContent("95%");
    expect(card).toHaveTextContent("rollback WFV-6");

    fireEvent.change(within(card).getByLabelText("Signoff rationale"), {
      target: { value: "All declared evidence is green." },
    });
    fireEvent.click(within(card).getByRole("button", { name: "Sign off GO" }));
    await waitFor(() => expect(signoffBody?.decision).toBe("go"));

    // Refresh swaps the card after signoff; generate the report from the current card.
    const signedCard = await screen.findByTestId("release-candidate-RC-1");
    fireEvent.click(within(signedCard).getByRole("button", { name: "Generate Allure" }));
    expect(await screen.findByText(/Allure report RRJ-1 generated/)).toBeInTheDocument();
  });

  it("keeps signoff and waiver state isolated per candidate", async () => {
    // Regression: `rationale`/`waiverKey`/`waiverReason` used to live once on
    // the parent list, shared by every rendered row. Typing a rationale for
    // one candidate silently overwrote what every other visible candidate's
    // "Sign off" button would submit, and a waiver typed for one candidate
    // could be recorded against another. Two candidates, two different
    // rationales/waivers, and the submitted body must always match the row
    // that was clicked -- never whichever text was typed last, anywhere on
    // the page.
    const makeCandidate = (id: string) => ({
      candidate_id: id,
      project_id: null,
      visibility: "user",
      name: `Candidate ${id}`,
      artifact_type: "workflow",
      artifact_ref: `WF-${id}`,
      version_ref: `WFV-${id}`,
      criteria: [],
      evidence: [],
      waivers: [],
      required_score: 0.8,
      weighted_score: 0.5,
      blockers: [{ criterion_key: "quality", reason: "below threshold" }],
      planned_action: {},
      rollback_target: {},
      status: "ready",
      owner: "@test",
      created_at: "2026-08-04T00:00:00Z",
      updated_at: "2026-08-04T00:00:00Z",
    });
    const signoffBodies: Record<string, Record<string, unknown>> = {};
    const waiverBodies: Record<string, Record<string, unknown>> = {};
    server.use(
      http.get(`${API_BASE}/releases/live`, () => HttpResponse.json(envelope([]))),
      http.get(`${API_BASE}/releases/timeline`, () => HttpResponse.json(envelope([]))),
      http.get(`${API_BASE}/releases/candidates`, () =>
        HttpResponse.json(envelope([makeCandidate("RC-1"), makeCandidate("RC-2")])),
      ),
      http.post(`${API_BASE}/releases/candidates/:id/signoffs`, async ({ request, params }) => {
        const body = (await request.json()) as Record<string, unknown>;
        signoffBodies[params.id as string] = body;
        return HttpResponse.json(
          envelope({
            signoff_id: `RSO-${params.id as string}`,
            candidate_id: params.id,
            decision: body.decision,
            rationale: body.rationale,
            decided_by: "@test",
            candidate_snapshot: makeCandidate(params.id as string),
            created_at: "2026-08-04T00:00:00Z",
          }),
          { status: 201 },
        );
      }),
      http.post(`${API_BASE}/releases/candidates/:id/waivers`, async ({ request, params }) => {
        const body = (await request.json()) as Record<string, unknown>;
        waiverBodies[params.id as string] = body;
        return HttpResponse.json(
          envelope({
            waiver_id: `RCW-${params.id as string}`,
            candidate_id: params.id,
            criterion_key: body.criterion_key,
            reason: body.reason,
            granted_by: "@test",
            created_at: "2026-08-04T00:00:00Z",
          }),
          { status: 201 },
        );
      }),
    );

    renderPage();
    const card1 = await screen.findByTestId("release-candidate-RC-1");
    const card2 = await screen.findByTestId("release-candidate-RC-2");

    // Two different rationales, typed into two different rows.
    fireEvent.change(within(card1).getByLabelText("Signoff rationale"), {
      target: { value: "Candidate one evidence review." },
    });
    fireEvent.change(within(card2).getByLabelText("Signoff rationale"), {
      target: { value: "Candidate two evidence review." },
    });
    // Row 1's box must still show row 1's text -- not silently overwritten by
    // typing into row 2 (the observable symptom of shared state).
    expect(within(card1).getByLabelText("Signoff rationale")).toHaveValue(
      "Candidate one evidence review.",
    );

    // Two different waivers, typed into two different rows.
    fireEvent.change(within(card1).getByLabelText("Waiver criterion key"), {
      target: { value: "quality" },
    });
    fireEvent.change(within(card1).getByLabelText("Waiver reason"), {
      target: { value: "Manual review compensates for row one." },
    });
    fireEvent.change(within(card2).getByLabelText("Waiver criterion key"), {
      target: { value: "quality" },
    });
    fireEvent.change(within(card2).getByLabelText("Waiver reason"), {
      target: { value: "Manual review compensates for row two." },
    });
    expect(within(card1).getByLabelText("Waiver reason")).toHaveValue(
      "Manual review compensates for row one.",
    );

    fireEvent.click(within(card1).getByRole("button", { name: "Sign off GO" }));
    await waitFor(() => expect(signoffBodies["RC-1"]).toBeDefined());
    expect(signoffBodies["RC-1"]).toMatchObject({
      decision: "go",
      rationale: "Candidate one evidence review.",
    });
    // The row container -- not the button, which the real app unmounts once
    // status leaves `ready` -- is where focus is explicitly returned.
    await waitFor(() => expect(card1).toHaveFocus());

    fireEvent.click(within(card2).getByRole("button", { name: "Sign off NO-GO" }));
    await waitFor(() => expect(signoffBodies["RC-2"]).toBeDefined());
    expect(signoffBodies["RC-2"]).toMatchObject({
      decision: "no_go",
      rationale: "Candidate two evidence review.",
    });

    fireEvent.click(within(card1).getByRole("button", { name: "Record waiver" }));
    await waitFor(() => expect(waiverBodies["RC-1"]).toBeDefined());
    expect(waiverBodies["RC-1"]).toMatchObject({
      criterion_key: "quality",
      reason: "Manual review compensates for row one.",
    });

    fireEvent.click(within(card2).getByRole("button", { name: "Record waiver" }));
    await waitFor(() => expect(waiverBodies["RC-2"]).toBeDefined());
    expect(waiverBodies["RC-2"]).toMatchObject({
      criterion_key: "quality",
      reason: "Manual review compensates for row two.",
    });
  });

  it("renders the live board and the promotion/rollback timeline", async () => {
    server.use(
      http.get(`${API_BASE}/releases/live`, () =>
        HttpResponse.json(
          envelope([
            {
              artifact_type: "workflow",
              artifact_id: "WF-1",
              alias: "prod",
              version_id: "WFV-7",
              since: "2026-06-29T00:00:00Z",
              by: "@reza",
            },
          ]),
        ),
      ),
      http.get(`${API_BASE}/releases/timeline`, () =>
        HttpResponse.json(
          envelope([
            {
              log_id: 2,
              timestamp: "2026-06-30T00:00:00Z",
              actor: "@reza",
              action: "rollback_prompt",
              entity_type: "prompt",
              entity_id: "support-agent",
              details: { from_version: 7, to_version: 6, overridden: true },
            },
            {
              log_id: 1,
              timestamp: "2026-06-29T00:00:00Z",
              actor: "@sam",
              action: "promote_workflow",
              entity_type: "workflow",
              entity_id: "WF-1",
              details: { to_version: "WFV-7" },
            },
          ]),
        ),
      ),
    );

    renderPage();

    // Live board.
    expect(await screen.findByTestId("releases-live-WF-1")).toHaveTextContent(
      "WFV-7",
    );
    // Timeline events.
    const rollback = await screen.findByTestId("releases-event-2");
    expect(rollback).toHaveTextContent("Roll back");
    expect(rollback).toHaveTextContent("support-agent");
    expect(rollback).toHaveTextContent("v7 → v6");
    // Overridden-gate badge surfaces.
    expect(screen.getByTestId("releases-overridden-2")).toBeInTheDocument();
    expect(screen.getByTestId("releases-event-1")).toHaveTextContent("Promote");
  });

  it("shows empty states when nothing is live or recorded", async () => {
    server.use(
      http.get(`${API_BASE}/releases/live`, () =>
        HttpResponse.json(envelope([])),
      ),
      http.get(`${API_BASE}/releases/timeline`, () =>
        HttpResponse.json(envelope([])),
      ),
    );
    renderPage();
    expect(
      await screen.findByText("Nothing deployed yet in your visible projects."),
    ).toBeInTheDocument();
    expect(
      screen.getByText(
        "No promotions or rollbacks yet in your visible projects.",
      ),
    ).toBeInTheDocument();
  });

  it("distinguishes a failed aggregate query from an empty release board", async () => {
    // Regression: both aggregates can fail independently, and the page had no
    // error state — a failed query rendered as "Nothing deployed yet.", so a
    // broken release board was indistinguishable from an empty one. On this page
    // that misreads as "nothing is in production".
    server.use(
      http.get(`${API_BASE}/releases/live`, () =>
        HttpResponse.json({ detail: "boom" }, { status: 500 }),
      ),
      http.get(`${API_BASE}/releases/timeline`, () =>
        HttpResponse.json({ detail: "boom" }, { status: 500 }),
      ),
    );
    renderPage();

    expect(
      await screen.findByTestId("releases-live-error"),
    ).toBeInTheDocument();
    expect(
      await screen.findByTestId("releases-timeline-error"),
    ).toBeInTheDocument();
    // The misleading empty-state copy must NOT appear alongside the error.
    expect(
      screen.queryByText("Nothing deployed yet in your visible projects."),
    ).not.toBeInTheDocument();
    expect(
      screen.queryByText(
        "No promotions or rollbacks yet in your visible projects.",
      ),
    ).not.toBeInTheDocument();
  });

  it("surfaces and operates all three recovery queues", async () => {
    const resolved: Array<Record<string, unknown>> = [];
    server.use(
      http.get(`${API_BASE}/releases/live`, () =>
        HttpResponse.json(envelope([])),
      ),
      http.get(`${API_BASE}/releases/timeline`, () =>
        HttpResponse.json(envelope([])),
      ),
      http.get(`${API_BASE}/releases/operations`, () =>
        HttpResponse.json(
          envelope([
            {
              operation_id: "REL-prepared",
              operation_type: "promote",
              resource_type: "prompt",
              resource_name: "support-agent",
              target_name: "prod",
              active_lock: "prompt:support-agent:prod",
              version_before: 4,
              version_after: 5,
              actor: "@reza",
              status: "prepared",
              last_error: null,
              created_at: "2026-08-04T00:00:00Z",
              updated_at: "2026-08-04T00:00:00Z",
            },
          ]),
        ),
      ),
      http.get(`${API_BASE}/system/effects`, () =>
        HttpResponse.json(
          envelope({
            effects: [
              {
                effect_key: "effect-1",
                workflow_run_id: "WR-1",
                node_id: "send",
                status: "in_progress",
                claimed_at: "2026-08-04T00:00:00Z",
              },
            ],
            status: "in_progress",
            resolutions: ["retry", "skip"],
          }),
        ),
      ),
      http.get(`${API_BASE}/system/webhook-dead-letters`, () =>
        HttpResponse.json(
          envelope({
            dead_letters: [
              {
                dead_letter_id: "DL-1",
                url: "https://example.invalid/hook",
                event_type: "release.applied",
                reason: "receiver unavailable",
                attempts: 3,
                kind: "exhausted",
                status: "open",
                failed_at: "2026-08-04T00:00:00Z",
                has_event: true,
              },
            ],
            status: "open",
            open_count: 1,
          }),
        ),
      ),
      http.post(
        `${API_BASE}/releases/operations/REL-prepared/resolve`,
        async ({ request }) => {
          resolved.push((await request.json()) as Record<string, unknown>);
          return HttpResponse.json(
            envelope({ operation_id: "REL-prepared", status: "applied" }),
          );
        },
      ),
    );
    vi.spyOn(window, "prompt").mockReturnValue("verified by operator");

    renderPage();

    expect(
      await screen.findByTestId("release-operation-REL-prepared"),
    ).toHaveTextContent("support-agent@prod");
    expect(screen.getByTestId("system-effect-effect-1")).toHaveTextContent(
      "WR-1",
    );
    expect(screen.getByTestId("dead-letter-DL-1")).toHaveTextContent(
      "release.applied",
    );
    fireEvent.click(screen.getByRole("button", { name: "Retry exact intent" }));
    await waitFor(() => expect(resolved).toEqual([{ action: "retry" }]));
  });

  it("does not request or render operator recovery queues for a viewer", async () => {
    let privilegedRequests = 0;
    server.use(
      http.get(`${API_BASE}/me`, () =>
        HttpResponse.json(
          envelope({
            user_id: "@viewer",
            scopes: ["caliber.viewer"],
            is_admin: false,
          }),
        ),
      ),
      http.get(`${API_BASE}/releases/live`, () =>
        HttpResponse.json(envelope([])),
      ),
      http.get(`${API_BASE}/releases/timeline`, () =>
        HttpResponse.json(envelope([])),
      ),
      http.get(`${API_BASE}/releases/operations`, () => {
        privilegedRequests += 1;
        return HttpResponse.json(envelope([]));
      }),
      http.get(`${API_BASE}/system/effects`, () => {
        privilegedRequests += 1;
        return HttpResponse.json(envelope({ effects: [] }));
      }),
      http.get(`${API_BASE}/system/webhook-dead-letters`, () => {
        privilegedRequests += 1;
        return HttpResponse.json(envelope({ dead_letters: [], open_count: 0 }));
      }),
    );

    renderPage();

    expect(
      await screen.findByText("Nothing deployed yet in your visible projects."),
    ).toBeInTheDocument();
    await waitFor(() => expect(privilegedRequests).toBe(0));
    expect(
      screen.queryByTestId("release-recovery-console"),
    ).not.toBeInTheDocument();
  });

  it("lets an operator inspect effects but reserves effect resolution for admins", async () => {
    server.use(
      http.get(`${API_BASE}/me`, () =>
        HttpResponse.json(
          envelope({
            user_id: "@operator",
            scopes: ["caliber.viewer", "caliber.operator"],
            is_admin: false,
          }),
        ),
      ),
      http.get(`${API_BASE}/releases/live`, () =>
        HttpResponse.json(envelope([])),
      ),
      http.get(`${API_BASE}/releases/timeline`, () =>
        HttpResponse.json(envelope([])),
      ),
      http.get(`${API_BASE}/system/effects`, () =>
        HttpResponse.json(
          envelope({
            effects: [
              {
                effect_key: "effect-admin",
                workflow_run_id: "WR-2",
                node_id: "send",
                status: "in_progress",
                claimed_at: "2026-08-04T00:00:00Z",
              },
            ],
            status: "in_progress",
            resolutions: ["retry", "skip"],
          }),
        ),
      ),
    );

    renderPage();

    const effect = await screen.findByTestId("system-effect-effect-admin");
    expect(effect).toHaveTextContent(
      "Admin scope is required to resolve effects.",
    );
    expect(within(effect).queryByRole("button")).not.toBeInTheDocument();
  });
});
