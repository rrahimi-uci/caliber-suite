import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { http, HttpResponse } from "msw";
import { afterAll, afterEach, beforeAll, describe, expect, it } from "vitest";
import { MemoryRouter, Route, Routes } from "react-router-dom";

import type { Skill } from "@/api/types";
import { Skills } from "@/pages/Skills";
import { server } from "@/test/server";

const API_BASE = "/ajax-api/2.0/mlflow/caliber";
const NOW = "2026-06-08T12:00:00Z";

function envelope<T>(data: T): { data: T } {
  return { data };
}

function makeSkill(overrides: Partial<Skill> = {}): Skill {
  return {
    skill_id: "SK-policy",
    name: "policy-answering",
    description: "Answer support policy questions.",
    summary: "Policy support helper",
    content: "Use {{policy_id}} and cite the policy source.",
    owner: "@support",
    category: "customer_support",
    tags: ["policy", "support"],
    skill_metadata: {},
    allowed_tools: null,
    depends_on: ["source-citation"],
    status: "active",
    version: 2,
    created_at: NOW,
    updated_at: NOW,
    ...overrides,
  };
}

function renderSkills(): void {
  render(
    <MemoryRouter future={{ v7_startTransition: true, v7_relativeSplatPath: true }} initialEntries={["/skills"]}>
      <Routes>
        <Route path="/skills" element={<Skills />} />
        <Route path="/skills/:skillId" element={<div>DETAIL ROUTE</div>} />
      </Routes>
    </MemoryRouter>,
  );
}

beforeAll(() => server.listen({ onUnhandledRequest: "error" }));
afterEach(() => server.resetHandlers());
afterAll(() => server.close());

describe("Skills page", () => {
  it("filters skills, archives/restores, and opens detail routes", async () => {
    const rows: Skill[] = [
      makeSkill(),
      makeSkill({
        skill_id: "SK-archived",
        name: "archived-legacy",
        summary: "",
        description: "Legacy archived guidance",
        tags: [],
        depends_on: [],
        status: "archived",
        category: "custom",
      }),
    ];
    const updates: Array<{ id: string; body: Record<string, unknown> }> = [];
    server.use(
      http.get(`${API_BASE}/skills`, ({ request }) => {
        const status = new URL(request.url).searchParams.get("status") ?? "active";
        const filtered =
          status === "all" ? rows : rows.filter((skill) => skill.status === status);
        return HttpResponse.json(envelope(filtered));
      }),
      http.patch(`${API_BASE}/skills/:skillId`, async ({ params, request }) => {
        const body = (await request.json()) as Record<string, unknown>;
        const id = String(params.skillId);
        updates.push({ id, body });
        const index = rows.findIndex((skill) => skill.skill_id === id);
        if (index >= 0) {
          rows[index] = { ...rows[index]!, ...(body as Partial<Skill>) };
        }
        return HttpResponse.json(envelope(rows[index] ?? makeSkill({ skill_id: id, ...body })));
      }),
      // Workspace endpoints for the opened skill so the header stays accurate
      // and the run-history fetch doesn't hit an unhandled route.
      http.get(`${API_BASE}/skills/SK-policy`, () =>
        HttpResponse.json(envelope(makeSkill())),
      ),
      http.get(`${API_BASE}/skills/SK-policy/workspace`, () =>
        HttpResponse.json(
          envelope({
            version: 2,
            category: "customer_support",
            status: "active",
            lifecycle: "Tested",
            last_run: null,
            baseline_run_id: null,
            baseline_run: null,
            bound_to: null,
          }),
        ),
      ),
      http.get(`${API_BASE}/skills/test-runs`, () => HttpResponse.json(envelope([]))),
    );

    const user = userEvent.setup();
    renderSkills();

    expect(await screen.findByText("policy-answering")).toBeInTheDocument();
    expect(screen.getByText("archived-legacy")).toBeInTheDocument();
    expect(screen.getByText("depends on: source-citation")).toBeInTheDocument();

    await user.type(screen.getByLabelText("Search skills"), "missing");
    expect(await screen.findByText("No skills match “missing”.")).toBeInTheDocument();
    await user.clear(screen.getByLabelText("Search skills"));

    await user.click(within(screen.getByTestId("skill-card-SK-policy")).getByRole("button", { name: "Archive" }));
    await waitFor(() =>
      expect(updates.at(-1)).toEqual({ id: "SK-policy", body: { status: "archived" } }),
    );

    await user.click(within(screen.getByTestId("skill-card-SK-policy")).getByRole("button", { name: "Restore" }));
    await waitFor(() =>
      expect(updates.at(-1)).toEqual({ id: "SK-policy", body: { status: "active" } }),
    );

    // Opening a skill now enters its in-page Workspace (not a detail route).
    fireEvent.doubleClick(
      screen.getByText("policy-answering").closest("[data-testid^='skill-card-']") as HTMLElement,
    );
    expect(await screen.findByTestId("skill-workspace-header")).toHaveTextContent("policy-answering");
  });

  it("renders summary stat tiles derived from real skill data", async () => {
    const rows: Skill[] = [
      makeSkill(),
      makeSkill({ skill_id: "SK-2", name: "json-formatter", category: "workflow_automation" }),
      makeSkill({
        skill_id: "SK-arch",
        name: "old-skill",
        status: "archived",
        category: "research",
      }),
    ];
    server.use(
      http.get(`${API_BASE}/skills`, ({ request }) => {
        const status = new URL(request.url).searchParams.get("status") ?? "active";
        const filtered = status === "all" ? rows : rows.filter((s) => s.status === status);
        return HttpResponse.json(envelope(filtered));
      }),
    );

    renderSkills();
    await screen.findByText("policy-answering");

    const registry = await screen.findByTestId("skill-tile-registry");
    expect(registry).toHaveTextContent("3");
    expect(registry).toHaveTextContent("Skills in registry");
    expect(screen.getByTestId("skill-tile-active")).toHaveTextContent("2");
    expect(screen.getByTestId("skill-tile-archived")).toHaveTextContent("1");
    expect(screen.getByTestId("skill-tile-categories")).toHaveTextContent("3");
  });

  it("renders a card grid and filters it with search", async () => {
    const rows: Skill[] = [
      makeSkill(),
      makeSkill({ skill_id: "SK-json", name: "json-formatter", category: "workflow_automation" }),
    ];
    server.use(
      http.get(`${API_BASE}/skills`, () => HttpResponse.json(envelope(rows))),
    );

    const user = userEvent.setup();
    renderSkills();

    // Cards render (not table rows).
    expect(await screen.findByTestId("skill-card-SK-policy")).toBeInTheDocument();
    expect(screen.getByTestId("skill-card-SK-json")).toBeInTheDocument();

    // Searching by category narrows the card grid.
    await user.type(screen.getByLabelText("Search skills"), "workflow_automation");
    expect(screen.getByTestId("skill-card-SK-json")).toBeInTheDocument();
    expect(screen.queryByTestId("skill-card-SK-policy")).not.toBeInTheDocument();

    // Clearing search restores both cards.
    await user.clear(screen.getByLabelText("Search skills"));
    expect(screen.getByTestId("skill-card-SK-policy")).toBeInTheDocument();
    expect(screen.getByTestId("skill-card-SK-json")).toBeInTheDocument();
  });

  it("narrows the card grid with the Category and Status filter dropdowns", async () => {
    const rows: Skill[] = [
      makeSkill(), // customer_support / active
      makeSkill({
        skill_id: "SK-json",
        name: "json-formatter",
        category: "workflow_automation",
      }),
      makeSkill({
        skill_id: "SK-old",
        name: "legacy-helper",
        category: "workflow_automation",
        status: "archived",
      }),
    ];
    server.use(
      http.get(`${API_BASE}/skills`, () => HttpResponse.json(envelope(rows))),
    );

    const user = userEvent.setup();
    renderSkills();
    expect(await screen.findByTestId("skill-card-SK-policy")).toBeInTheDocument();

    // Category options are derived + humanized (snake_case → Title Case).
    const categorySelect = screen.getByRole("combobox", { name: "Filter by category" });
    await user.selectOptions(categorySelect, "workflow_automation");
    expect(screen.queryByTestId("skill-card-SK-policy")).not.toBeInTheDocument();
    expect(screen.getByTestId("skill-card-SK-json")).toBeInTheDocument();
    expect(screen.getByTestId("skill-card-SK-old")).toBeInTheDocument();
    // The humanized label is offered as an option.
    expect(
      within(categorySelect).getByRole("option", { name: "Workflow Automation" }),
    ).toBeInTheDocument();

    // Status filter is additive with the category filter.
    await user.selectOptions(
      screen.getByRole("combobox", { name: "Filter by status" }),
      "archived",
    );
    expect(screen.queryByTestId("skill-card-SK-json")).not.toBeInTheDocument();
    expect(screen.getByTestId("skill-card-SK-old")).toBeInTheDocument();

    // Resetting both filters restores every card.
    await user.selectOptions(categorySelect, "");
    await user.selectOptions(
      screen.getByRole("combobox", { name: "Filter by status" }),
      "",
    );
    expect(screen.getByTestId("skill-card-SK-policy")).toBeInTheDocument();
    expect(screen.getByTestId("skill-card-SK-json")).toBeInTheDocument();
    expect(screen.getByTestId("skill-card-SK-old")).toBeInTheDocument();
  });

  it("opens detail when the New skill button reuses the build view", async () => {
    server.use(http.get(`${API_BASE}/skills`, () => HttpResponse.json(envelope([makeSkill()]))));

    const user = userEvent.setup();
    renderSkills();
    await screen.findByText("policy-answering");

    await user.click(screen.getByTestId("new-skill"));
    expect(await screen.findByTestId("skill-wizard")).toBeInTheDocument();
  });

  it("shows load and action errors", async () => {
    server.use(
      http.get(`${API_BASE}/skills`, () => HttpResponse.json({ detail: "skills down" }, { status: 500 })),
    );

    renderSkills();
    expect(await screen.findByText("Failed to load skills")).toBeInTheDocument();
    expect(screen.getByText("skills down")).toBeInTheDocument();
  });

  it("surfaces archive failures without leaving the list", async () => {
    server.use(
      http.get(`${API_BASE}/skills`, () => HttpResponse.json(envelope([makeSkill()]))),
      http.patch(`${API_BASE}/skills/SK-policy`, () =>
        HttpResponse.json({ detail: "archive denied" }, { status: 500 }),
      ),
    );

    const user = userEvent.setup();
    renderSkills();
    await user.click(await screen.findByRole("button", { name: "Archive" }));

    expect(await screen.findByText("archive denied")).toBeInTheDocument();
    expect(screen.getByText("policy-answering")).toBeInTheDocument();
  });

  it("opens a skill into its Workspace with six stage tabs and back returns", async () => {
    server.use(
      http.get(`${API_BASE}/skills`, () => HttpResponse.json(envelope([makeSkill()]))),
      http.get(`${API_BASE}/skills/SK-policy`, () => HttpResponse.json(envelope(makeSkill()))),
      http.get(`${API_BASE}/skills/SK-policy/workspace`, () =>
        HttpResponse.json(
          envelope({
            version: 2,
            category: "customer_support",
            status: "active",
            lifecycle: "Tested",
            last_run: null,
            baseline_run_id: null,
            baseline_run: null,
            bound_to: null,
          }),
        ),
      ),
      http.get(`${API_BASE}/skills/test-runs`, () => HttpResponse.json(envelope([]))),
    );

    const user = userEvent.setup();
    renderSkills();
    await screen.findByText("policy-answering");

    // Open the skill → Workspace header + lifecycle pill + six stage tabs.
    await user.click(await screen.findByTestId("skill-open-policy-answering"));
    const header = await screen.findByTestId("skill-workspace-header");
    expect(header).toHaveTextContent("policy-answering");
    expect(screen.getByTestId("skill-workspace-status-badge")).toHaveTextContent("Tested");
    for (const label of [
      "Author",
      "Render Preview",
      "Trigger Tests",
      "Scenario Sets",
      "Runs",
      "Bind",
    ]) {
      expect(screen.getByRole("button", { name: label })).toBeInTheDocument();
    }
    // No agent picker anywhere in skill testing.
    expect(screen.queryByLabelText("Select calibration agent")).not.toBeInTheDocument();

    // Back returns to the landing list.
    await user.click(screen.getByRole("button", { name: "Back to skills" }));
    expect(await screen.findByTestId("skill-card-SK-policy")).toBeInTheDocument();
    expect(screen.queryByTestId("skill-workspace-header")).not.toBeInTheDocument();

    // Build Skill enters the create-mode Workspace whose Author stage hosts the wizard.
    await user.click(screen.getByTestId("new-skill"));
    expect(await screen.findByTestId("skill-wizard")).toBeInTheDocument();
  });

  it("Author stage edits a skill, saves, and surfaces save errors", async () => {
    let current = makeSkill();
    server.use(
      http.get(`${API_BASE}/skills`, () => HttpResponse.json(envelope([current]))),
      http.get(`${API_BASE}/skills/SK-policy`, () => HttpResponse.json(envelope(current))),
      http.get(`${API_BASE}/skills/SK-policy/workspace`, () =>
        HttpResponse.json(
          envelope({
            version: current.version,
            category: current.category,
            status: current.status,
            lifecycle: "Tested",
            last_run: null,
            baseline_run_id: null,
            baseline_run: null,
            bound_to: null,
          }),
        ),
      ),
      http.get(`${API_BASE}/skills/test-runs`, () => HttpResponse.json(envelope([]))),
    );

    const user = userEvent.setup();
    renderSkills();
    await user.click(await screen.findByTestId("skill-open-policy-answering"));
    expect(await screen.findByTestId("skill-author-stage")).toBeInTheDocument();

    const saveButton = screen.getByTestId("skill-author-save");
    // Unmodified form starts clean — Save is disabled.
    expect(saveButton).toBeDisabled();

    await user.clear(screen.getByLabelText("Skill summary"));
    await user.type(screen.getByLabelText("Skill summary"), "Updated summary");
    expect(saveButton).toBeEnabled();

    // First save attempt fails — the error surfaces and the form stays dirty.
    server.use(
      http.patch(`${API_BASE}/skills/SK-policy`, () =>
        HttpResponse.json({ detail: "save denied" }, { status: 500 }),
      ),
    );
    await user.click(saveButton);
    expect(await screen.findByText("save denied")).toBeInTheDocument();
    expect(saveButton).toBeEnabled();

    // Retrying against a working endpoint saves and re-seeds the form.
    server.use(
      http.patch(`${API_BASE}/skills/SK-policy`, async ({ request }) => {
        const body = (await request.json()) as Partial<Skill>;
        current = { ...current, ...body };
        return HttpResponse.json(envelope(current));
      }),
    );
    await user.click(saveButton);
    await waitFor(() => expect(screen.getByTestId("skill-author-save")).toBeDisabled());
    expect(screen.getByText("Saved")).toBeInTheDocument();
    expect(screen.getByLabelText("Skill summary")).toHaveValue("Updated summary");
  });

  it("Scenario Sets stage adds/removes cases and Trigger Tests runs ad-hoc + batch checks", async () => {
    const savedRuns: Array<Record<string, unknown>> = [];
    server.use(
      http.get(`${API_BASE}/skills`, () => HttpResponse.json(envelope([makeSkill()]))),
      http.get(`${API_BASE}/skills/SK-policy`, () => HttpResponse.json(envelope(makeSkill()))),
      http.get(`${API_BASE}/skills/SK-policy/workspace`, () =>
        HttpResponse.json(
          envelope({
            version: 2,
            category: "customer_support",
            status: "active",
            lifecycle: "Tested",
            last_run: null,
            baseline_run_id: null,
            baseline_run: null,
            bound_to: null,
          }),
        ),
      ),
      http.get(`${API_BASE}/skills/test-runs`, () => HttpResponse.json(envelope([]))),
      http.post(`${API_BASE}/skills/:skillId/test-selection`, async ({ request }) => {
        const body = (await request.json()) as { user_message: string };
        // A message containing "no" is designed to mismatch the "expect
        // selected" scenario below, so the batch run exercises the
        // "regression" (mismatch) badge as well as the "matches" one.
        const selected = !body.user_message.toLowerCase().includes("no");
        return HttpResponse.json(
          envelope({
            skill_id: "SK-policy",
            skill_name: "policy-answering",
            is_selected: selected,
            selection_score: selected ? 0.9 : 0.2,
            selection_reason: `reason for: ${body.user_message}`,
          }),
        );
      }),
      http.post(`${API_BASE}/skills/test-runs`, async ({ request }) => {
        const body = (await request.json()) as Record<string, unknown>;
        savedRuns.push(body);
        return HttpResponse.json(
          envelope({
            test_run_id: "STR-saved-1",
            skill_id: "SK-policy",
            skill_version: 2,
            kind: "selection",
            test_set_size: Array.isArray(body.results) ? body.results.length : 0,
            passed_count: 0,
            failed_count: 0,
            partial_count: 0,
            overall_score: null,
            host_agent_id: null,
            trace_id: null,
            mlflow_run_id: null,
            created_by: "@test",
            status: "completed",
            created_at: "2025-01-01T00:00:00Z",
            completed_at: "2025-01-01T00:00:00Z",
          }),
          { status: 201 },
        );
      }),
    );

    const user = userEvent.setup();
    renderSkills();
    await user.click(await screen.findByTestId("skill-open-policy-answering"));
    await screen.findByTestId("skill-author-stage");

    // ── Scenario Sets: empty state, then add two cases.
    await user.click(screen.getByRole("button", { name: "Scenario Sets" }));
    expect(await screen.findByTestId("skill-scenario-sets")).toBeInTheDocument();
    expect(screen.getByText(/No scenarios yet/)).toBeInTheDocument();
    expect(screen.getByTestId("skill-scenario-add")).toBeDisabled();

    await user.type(
      screen.getByLabelText("Scenario user message"),
      "Yes please summarize this",
    );
    await user.type(screen.getByLabelText("Scenario tags"), "policy, refund");
    expect(screen.getByTestId("skill-scenario-add")).toBeEnabled();
    await user.click(screen.getByTestId("skill-scenario-add"));
    expect(screen.getByText("1 scenario")).toBeInTheDocument();
    expect(screen.getByText("expect selected")).toBeInTheDocument();

    // Second scenario: expected NOT selected, to produce a mismatch later.
    await user.type(screen.getByLabelText("Scenario user message"), "no thanks");
    await user.click(screen.getByLabelText("Expected: skill should be selected"));
    await user.click(screen.getByTestId("skill-scenario-add"));
    expect(screen.getByText("2 scenarios")).toBeInTheDocument();
    expect(screen.getByText("expect not selected")).toBeInTheDocument();

    // Remove the first case.
    await user.click(screen.getByRole("button", { name: "Remove scenario 1" }));
    expect(screen.getByText("1 scenario")).toBeInTheDocument();

    // Re-add a "should select" case so the batch run below has both outcomes.
    await user.type(screen.getByLabelText("Scenario user message"), "Yes again");
    await user.click(screen.getByLabelText("Expected: skill should be selected"));
    await user.click(screen.getByTestId("skill-scenario-add"));
    expect(screen.getByText("2 scenarios")).toBeInTheDocument();

    // Jump to Trigger Tests via the tab (the panel also has an inline link
    // with the same accessible name, so scope to the tablist).
    await user.click(
      within(screen.getByRole("tablist")).getByRole("button", { name: "Trigger Tests" }),
    );
    expect(await screen.findByTestId("skill-trigger-tests")).toBeInTheDocument();

    // Ad-hoc check.
    await user.type(
      screen.getByTestId("skill-trigger-message"),
      "Yes, an ad-hoc message",
    );
    await user.click(screen.getByTestId("skill-trigger-run"));
    expect(await screen.findByTestId("skill-trigger-result")).toBeInTheDocument();
    expect(screen.getByTestId("skill-trigger-selected")).toHaveTextContent("selected");

    // Batch-run the scenario set: one mismatch (regression), one match.
    await user.click(screen.getByRole("button", { name: /Run 2 scenarios/ }));
    await waitFor(() => expect(screen.getAllByTestId("skill-trigger-result")).toHaveLength(2));
    expect(screen.getByText("matches expectation")).toBeInTheDocument();
    expect(screen.getByText("regression")).toBeInTheDocument();

    // Save the batch as a durable run.
    await user.click(screen.getByTestId("skill-trigger-save"));
    expect(await screen.findByTestId("skill-trigger-saved")).toHaveTextContent(
      "STR-saved-1",
    );
    expect(savedRuns).toHaveLength(1);
    expect(savedRuns[0]).toMatchObject({ skill_id: "SK-policy", kind: "selection" });

    // Ad-hoc failure surfaces an error.
    server.use(
      http.post(`${API_BASE}/skills/:skillId/test-selection`, () =>
        HttpResponse.json({ detail: "selection failed" }, { status: 500 }),
      ),
    );
    await user.clear(screen.getByTestId("skill-trigger-message"));
    await user.type(screen.getByTestId("skill-trigger-message"), "trigger failure");
    await user.click(screen.getByTestId("skill-trigger-run"));
    expect(await screen.findByText("selection failed")).toBeInTheDocument();
  });

  it("Runs stage shows history, pins a baseline, computes regressions, and calibrates", async () => {
    const runA = {
      test_run_id: "STR-1",
      skill_id: "SK-policy",
      skill_version: 2,
      kind: "selection",
      test_set_size: 1,
      passed_count: 1,
      failed_count: 0,
      partial_count: 0,
      overall_score: 1,
      host_agent_id: null,
      trace_id: null,
      mlflow_run_id: null,
      created_by: "@test",
      status: "completed",
      created_at: "2025-01-02T00:00:00Z",
      completed_at: "2025-01-02T00:00:00Z",
    };
    const runB = {
      ...runA,
      test_run_id: "STR-2",
      overall_score: 0,
      passed_count: 0,
      failed_count: 1,
      created_at: "2025-01-03T00:00:00Z",
    };
    const runDetails: Record<string, unknown> = {
      "STR-1": {
        ...runA,
        results: [
          { name: "case-1", input: { user_message: "hi" }, output: { ok: true }, verdict: "pass", score: 1 },
        ],
      },
      "STR-2": {
        ...runB,
        results: [
          { name: "case-1", input: { user_message: "hi" }, output: { ok: false }, error: "boom", verdict: "fail", score: 0 },
        ],
      },
    };
    let baselineRunId: string | null = null;

    server.use(
      http.get(`${API_BASE}/skills`, () => HttpResponse.json(envelope([makeSkill()]))),
      http.get(`${API_BASE}/skills/SK-policy`, () => HttpResponse.json(envelope(makeSkill()))),
      http.get(`${API_BASE}/skills/SK-policy/workspace`, () =>
        HttpResponse.json(
          envelope({
            version: 2,
            category: "customer_support",
            status: "active",
            lifecycle: "Tested",
            last_run: null,
            baseline_run_id: baselineRunId,
            baseline_run: null,
            bound_to: null,
          }),
        ),
      ),
      http.get(`${API_BASE}/skills/test-runs`, () => HttpResponse.json(envelope([runB, runA]))),
      http.get(`${API_BASE}/skills/test-runs/:testRunId`, ({ params }) =>
        HttpResponse.json(envelope(runDetails[String(params.testRunId)])),
      ),
      http.post(`${API_BASE}/skills/:skillId/baseline`, async ({ request }) => {
        const body = (await request.json()) as { test_run_id: string };
        baselineRunId = body.test_run_id;
        return HttpResponse.json(envelope({ baseline_run_id: baselineRunId }));
      }),
      http.post(`${API_BASE}/skills/:skillId/calibrate`, () =>
        HttpResponse.json(
          envelope({ item: { item_id: "VI-1" }, job: { job_id: "JOB-1" } }),
          { status: 201 },
        ),
      ),
    );

    const user = userEvent.setup();
    renderSkills();
    await user.click(await screen.findByTestId("skill-open-policy-answering"));
    await user.click(screen.getByRole("button", { name: "Runs" }));

    // Latest run (STR-2, most recent) auto-selected; not yet a baseline.
    expect(await screen.findByTestId("skill-workspace-run-results")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Set as baseline" })).toBeInTheDocument();

    // Pin STR-2 as baseline.
    await user.click(screen.getByRole("button", { name: "Set as baseline" }));
    await waitFor(() =>
      expect(screen.getByTestId("skill-run-baseline-marker")).toBeInTheDocument(),
    );

    // Select the other run — comparison view appears (STR-1 passed where the
    // baseline STR-2 failed, but a "pass after fail" is not itself flagged as
    // a regression; assert the comparison surfaces the score delta and rows).
    await user.click(screen.getByLabelText("View run STR-1"));
    expect(await screen.findByTestId("skill-workspace-run-comparison")).toBeInTheDocument();
    expect(screen.getByTestId("skill-run-score-delta")).toHaveTextContent("+100%");
    expect(screen.getByTestId("skill-run-regressions")).toHaveTextContent("No regressions");

    // Calibrate (agent-free).
    await user.click(screen.getByTestId("skill-calibrate-btn"));
    expect(await screen.findByTestId("skill-calibrate-result")).toHaveTextContent(
      "JOB-1",
    );

    // Kind filter narrows the history fetch and resets the viewed run.
    await user.selectOptions(screen.getByLabelText("Filter runs by kind"), "render");
  });

  it("Runs stage shows the empty history state and surfaces calibrate/baseline errors", async () => {
    server.use(
      http.get(`${API_BASE}/skills`, () => HttpResponse.json(envelope([makeSkill()]))),
      http.get(`${API_BASE}/skills/SK-policy`, () => HttpResponse.json(envelope(makeSkill()))),
      http.get(`${API_BASE}/skills/SK-policy/workspace`, () =>
        HttpResponse.json(
          envelope({
            version: 2,
            category: "customer_support",
            status: "active",
            lifecycle: "Tested",
            last_run: null,
            baseline_run_id: null,
            baseline_run: null,
            bound_to: null,
          }),
        ),
      ),
      http.get(`${API_BASE}/skills/test-runs`, () => HttpResponse.json(envelope([]))),
      http.post(`${API_BASE}/skills/:skillId/calibrate`, () =>
        HttpResponse.json({ detail: "calibrate failed" }, { status: 500 }),
      ),
    );

    const user = userEvent.setup();
    renderSkills();
    await user.click(await screen.findByTestId("skill-open-policy-answering"));
    await user.click(screen.getByRole("button", { name: "Runs" }));

    expect(await screen.findByText(/No saved runs yet/)).toBeInTheDocument();
    expect(screen.queryByTestId("skill-workspace-run-results")).not.toBeInTheDocument();

    await user.click(screen.getByTestId("skill-calibrate-btn"));
    expect(await screen.findByText("calibrate failed")).toBeInTheDocument();
  });

  it("Bind stage binds to a workflow node and to standalone, showing the current binding", async () => {
    let boundTo: Record<string, unknown> | null = null;
    const bindCalls: Array<Record<string, unknown>> = [];

    server.use(
      http.get(`${API_BASE}/skills`, () => HttpResponse.json(envelope([makeSkill()]))),
      http.get(`${API_BASE}/skills/SK-policy`, () => HttpResponse.json(envelope(makeSkill()))),
      http.get(`${API_BASE}/skills/SK-policy/workspace`, () =>
        HttpResponse.json(
          envelope({
            version: 2,
            category: "customer_support",
            status: "active",
            lifecycle: "Tested",
            last_run: null,
            baseline_run_id: null,
            baseline_run: null,
            bound_to: boundTo,
          }),
        ),
      ),
      http.get(`${API_BASE}/skills/test-runs`, () => HttpResponse.json(envelope([]))),
      http.post(`${API_BASE}/skills/:skillId/bind`, async ({ request }) => {
        const body = (await request.json()) as Record<string, unknown>;
        bindCalls.push(body);
        boundTo = body;
        return HttpResponse.json(envelope({ bound_to: body, status: "Bound" }));
      }),
    );

    const user = userEvent.setup();
    renderSkills();
    await user.click(await screen.findByTestId("skill-open-policy-answering"));
    await user.click(screen.getByRole("button", { name: "Bind" }));
    expect(await screen.findByText(/Not bound yet/)).toBeInTheDocument();

    // Switch to workflow node and fill it in (default workflow WF-001 is
    // preselected once the agents/workflows loaders resolve). The option
    // buttons also render a hint sentence as part of their accessible name,
    // so anchor the match to the start of the label.
    await user.click(screen.getByRole("button", { name: /^Workflow node/ }));
    await screen.findByLabelText("Select workflow to bind");
    await user.type(screen.getByLabelText("Workflow node id"), "classifier");
    await user.click(screen.getByLabelText("Bind skill"));

    await waitFor(() =>
      expect(bindCalls.at(-1)).toMatchObject({
        kind: "workflow_node",
        workflow_id: "WF-001",
        node_id: "classifier",
      }),
    );
    expect(await screen.findByTestId("skill-workspace-bound-to")).toHaveTextContent(
      "Workflow node · WF-001 / classifier",
    );

    // Re-bind as standalone.
    await user.click(screen.getByRole("button", { name: /^Standalone/ }));
    await user.click(screen.getByLabelText("Bind skill"));
    await waitFor(() => expect(bindCalls.at(-1)).toEqual({ kind: "standalone" }));
    expect(await screen.findByTestId("skill-workspace-bound-to")).toHaveTextContent(
      "Standalone",
    );
  });
});
