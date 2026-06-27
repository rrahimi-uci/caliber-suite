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
    await user.click(screen.getByTestId("skill-open-policy-answering"));
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
});
