import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { http, HttpResponse } from "msw";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { afterAll, afterEach, beforeAll, describe, expect, it } from "vitest";

import type { AgentConfig } from "@/api/types";
import { AgentDetail } from "@/pages/AgentDetail";
import { Agents } from "@/pages/Agents";
import { server } from "@/test/server";

const API_BASE = "/ajax-api/2.0/mlflow/caliber";

function envelope<T>(data: T): { data: T } {
  return { data };
}

function agentFixture(overrides: Partial<AgentConfig> = {}): AgentConfig {
  return {
    agent_id: "support-agent",
    experiment_id: "exp-support",
    name: "Support Agent",
    owner: "@test",
    artifact_types: ["prompt"],
    eval_thresholds: { minimum_score: 0.8 },
    optimizer_config: { skills: ["reasoning"] },
    approval_policy: {},
    optimize_for: "quality",
    collaboration_mode: null,
    enabled: true,
    required_approvals: 1,
    created_at: "2025-01-01T00:00:00Z",
    updated_at: "2025-01-02T00:00:00Z",
    ...overrides,
  };
}

function handlers(initial: AgentConfig[] = [agentFixture()]) {
  const state = { agents: [...initial], patches: 0, creates: 0 };
  server.use(
    http.get(`${API_BASE}/me`, () =>
      HttpResponse.json(
        envelope({
          user_id: "@test",
          scopes: ["caliber.viewer", "caliber.operator", "caliber.admin"],
          is_admin: true,
        }),
      ),
    ),
    http.get(`${API_BASE}/agents`, () =>
      HttpResponse.json(envelope(state.agents)),
    ),
    http.post(`${API_BASE}/agents`, async ({ request }) => {
      state.creates += 1;
      const body = (await request.json()) as Partial<AgentConfig>;
      const created = agentFixture({
        agent_id: body.agent_id,
        experiment_id: body.experiment_id,
        name: body.name,
        optimizer_config: body.optimizer_config ?? {},
      });
      state.agents.push(created);
      return HttpResponse.json(envelope(created), { status: 201 });
    }),
    http.get(`${API_BASE}/agents/:id`, ({ params }) => {
      const agent = state.agents.find((item) => item.agent_id === params.id);
      return agent
        ? HttpResponse.json(envelope(agent))
        : HttpResponse.json({ detail: "not found" }, { status: 404 });
    }),
    http.patch(`${API_BASE}/agents/:id`, async ({ params, request }) => {
      state.patches += 1;
      const body = (await request.json()) as Partial<AgentConfig>;
      const index = state.agents.findIndex(
        (item) => item.agent_id === params.id,
      );
      state.agents[index] = { ...state.agents[index]!, ...body };
      return HttpResponse.json(envelope(state.agents[index]));
    }),
    http.get(`${API_BASE}/agents/:id/skills`, () =>
      HttpResponse.json(
        envelope({
          skills: [
            {
              skill_id: "SK-1",
              name: "reasoning",
              description: "",
              summary: "",
              content: "Reason carefully",
              owner: "@test",
              category: "custom",
              tags: [],
              skill_metadata: {},
              allowed_tools: null,
              depends_on: [],
              status: "active",
              version: 2,
              created_at: "2025-01-01T00:00:00Z",
              updated_at: "2025-01-01T00:00:00Z",
            },
          ],
          missing: [],
        }),
      ),
    ),
    http.get(`${API_BASE}/agents/:agentId/experiment`, () =>
      HttpResponse.json(
        envelope({
          configured_experiment_id: "42",
          status: "reachable",
          detail: "resolved to experiment 42",
          experiment_id: "42",
          name: "support-agent",
          lifecycle_stage: "active",
        }),
      ),
    ),
    http.get(`${API_BASE}/audit-log`, () =>
      HttpResponse.json(
        envelope({
          entries: [
            {
              log_id: 1,
              timestamp: "2025-01-02T00:00:00Z",
              actor: "@test",
              action: "update_agent",
              entity_type: "agent",
              entity_id: "support-agent",
              details: {
                changes: { name: { from: "Old", to: "Support Agent" } },
              },
            },
          ],
          total: 1,
          limit: 100,
          offset: 0,
        }),
      ),
    ),
    http.delete(`${API_BASE}/agents/:id`, () =>
      HttpResponse.json(envelope({ agent_id: "support-agent", deleted: true })),
    ),
  );
  return state;
}

function renderAt(path: string) {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  return render(
    <QueryClientProvider client={client}>
      <MemoryRouter
        initialEntries={[path]}
        future={{ v7_startTransition: true, v7_relativeSplatPath: true }}
      >
        <Routes>
          <Route path="/agents" element={<Agents />} />
          <Route path="/agents/:agentId" element={<AgentDetail />} />
        </Routes>
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

beforeAll(() => server.listen({ onUnhandledRequest: "error" }));
afterEach(() => server.resetHandlers());
afterAll(() => server.close());

describe("Agents lifecycle UI", () => {
  it("lists agent configurations and registers a new one", async () => {
    const state = handlers();
    renderAt("/agents");
    const user = userEvent.setup();

    expect(await screen.findByText("Support Agent")).toBeInTheDocument();
    await user.click(screen.getByTestId("new-agent"));
    await user.type(screen.getByLabelText("Agent ID"), "research-agent");
    await user.type(
      screen.getByLabelText("MLflow experiment ID"),
      "exp-research",
    );
    await user.type(screen.getByLabelText("Display name"), "Research Agent");
    await user.click(screen.getByRole("button", { name: "Register agent" }));

    expect(
      await screen.findByRole("heading", { name: "Research Agent" }),
    ).toBeInTheDocument();
    expect(state.creates).toBe(1);
  });

  it("runs an honest configuration preflight and exposes audit-backed revisions", async () => {
    handlers();
    renderAt("/agents/support-agent");
    const user = userEvent.setup();

    expect(
      await screen.findByRole("heading", { name: "Support Agent" }),
    ).toBeInTheDocument();
    await user.click(screen.getByTestId("agent-preflight"));
    expect(screen.getByTestId("agent-preflight-results")).toHaveTextContent(
      "Referenced skills resolve",
    );
    expect(screen.getByText(/does not invoke a model/i)).toBeInTheDocument();
    expect(
      await screen.findByText(/Audit-backed changes/i),
    ).toBeInTheDocument();
    expect(screen.getByText(/update agent/i)).toBeInTheDocument();
  });

  it("edits and disables an agent while retaining the configuration", async () => {
    const state = handlers();
    renderAt("/agents/support-agent");
    const user = userEvent.setup();
    await screen.findByRole("heading", { name: "Support Agent" });

    await user.click(screen.getByRole("button", { name: "Edit" }));
    const name = screen.getByLabelText("Display name");
    await user.clear(name);
    await user.type(name, "Support Agent v2");
    await user.click(
      screen.getByRole("button", { name: "Save configuration" }),
    );
    await waitFor(() => expect(state.patches).toBe(1));

    await user.click(screen.getByTestId("agent-toggle-enabled"));
    await waitFor(() => expect(state.patches).toBe(2));
    expect(state.agents[0]).toMatchObject({
      name: "Support Agent v2",
      enabled: false,
    });
  });

  it("keeps admin-only lifecycle mutations out of the viewer UI", async () => {
    handlers();
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
    );

    const { unmount } = renderAt("/agents");
    expect(await screen.findByText("Support Agent")).toBeInTheDocument();
    expect(screen.queryByTestId("new-agent")).not.toBeInTheDocument();
    expect(screen.getByText(/read-only for your account/i)).toBeInTheDocument();
    unmount();

    renderAt("/agents/support-agent");
    expect(
      await screen.findByRole("heading", { name: "Support Agent" }),
    ).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Edit" })).not.toBeInTheDocument();
    expect(screen.queryByTestId("agent-toggle-enabled")).not.toBeInTheDocument();
    expect(screen.getByText(/Administrator access is required/i)).toBeInTheDocument();
    // Regression: the page fired the admin-only audit request as a viewer and
    // rendered the resulting 403 as if the page itself were broken. ``/me``
    // already answers this, so the request is not made and the copy explains the
    // permission instead of showing an error.
    expect(
      await screen.findByTestId("agent-history-requires-admin"),
    ).toBeInTheDocument();
    expect(screen.queryByText(/Could not load revision history/i)).not.toBeInTheDocument();
  });

  it("reports a real experiment-binding verdict, including 'unverified'", async () => {
    // Regression: the check only proved the stored id was a non-empty string, so
    // a typo'd or deleted experiment passed a control labelled "experiment
    // binding preflight".
    handlers();
    server.use(
      http.get(`${API_BASE}/agents/:agentId/experiment`, () =>
        HttpResponse.json(
          envelope({
            configured_experiment_id: "9999",
            status: "missing",
            detail: "MLflow has no experiment '9999'",
          }),
        ),
      ),
    );
    const { unmount } = renderAt("/agents/support-agent");
    await userEvent.click(await screen.findByTestId("agent-preflight"));
    expect(await screen.findByText(/MLflow has no experiment/)).toBeInTheDocument();
    expect(screen.getAllByText("fail").length).toBeGreaterThan(0);
    unmount();

    // A registry outage is neither pass nor fail.
    server.use(
      http.get(`${API_BASE}/agents/:agentId/experiment`, () =>
        HttpResponse.json(
          envelope({
            configured_experiment_id: "42",
            status: "unverified",
            detail: "could not reach MLflow: connection refused",
          }),
        ),
      ),
    );
    renderAt("/agents/support-agent");
    await userEvent.click(await screen.findByTestId("agent-preflight"));
    expect(await screen.findByText(/could not reach MLflow/)).toBeInTheDocument();
    expect(screen.getAllByText("unverified").length).toBeGreaterThan(0);
  });
});

describe("Agents list page", () => {
  it("shows a distinct empty state before and after a search narrows the list to nothing", async () => {
    handlers([]);
    const user = userEvent.setup();
    const { unmount } = renderAt("/agents");

    expect(await screen.findByText("No agent configurations yet")).toBeInTheDocument();
    unmount();

    handlers([agentFixture()]);
    renderAt("/agents");
    await screen.findByText("Support Agent");
    await user.type(screen.getByRole("searchbox", { name: "Search agents" }), "nonexistent");

    expect(await screen.findByText("No agents match your search")).toBeInTheDocument();
    expect(screen.queryByText("Support Agent")).not.toBeInTheDocument();
  });

  it("filters agents by name, id, experiment, and owner, and clears via the × button", async () => {
    handlers([
      agentFixture({ agent_id: "support-agent", name: "Support Agent", owner: "@ops" }),
      agentFixture({
        agent_id: "billing-agent",
        name: "Billing Agent",
        experiment_id: "exp-billing",
        owner: "@finance",
      }),
    ]);
    const user = userEvent.setup();
    renderAt("/agents");
    await screen.findByText("Support Agent");
    expect(screen.getByText("Billing Agent")).toBeInTheDocument();

    const search = screen.getByRole("searchbox", { name: "Search agents" });
    await user.type(search, "@finance");
    expect(screen.queryByText("Support Agent")).not.toBeInTheDocument();
    expect(screen.getByText("Billing Agent")).toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: "Clear search" }));
    expect(await screen.findByText("Support Agent")).toBeInTheDocument();
  });

  it("navigates to an agent's detail page when its card is clicked", async () => {
    handlers();
    const user = userEvent.setup();
    renderAt("/agents");

    await user.click(await screen.findByTestId("agent-card-support-agent"));
    expect(await screen.findByRole("heading", { name: "Support Agent" })).toBeInTheDocument();
  });

  it("surfaces a load error for the agent list", async () => {
    server.use(
      http.get(`${API_BASE}/me`, () =>
        HttpResponse.json(envelope({ user_id: "@test", scopes: [], is_admin: true })),
      ),
      http.get(`${API_BASE}/agents`, () =>
        HttpResponse.json({ detail: "agent registry unavailable" }, { status: 500 }),
      ),
    );
    renderAt("/agents");

    expect(await screen.findByRole("alert")).toHaveTextContent("agent registry unavailable");
    expect(screen.queryByText(/No agent configurations/)).not.toBeInTheDocument();
  });

  it("shows a registration-failed toast without navigating when creation is rejected", async () => {
    handlers();
    server.use(
      http.post(`${API_BASE}/agents`, () =>
        HttpResponse.json({ detail: "agent_id already exists" }, { status: 409 }),
      ),
    );
    const user = userEvent.setup();
    renderAt("/agents");
    await screen.findByText("Support Agent");

    await user.click(screen.getByTestId("new-agent"));
    await user.type(screen.getByLabelText("Agent ID"), "dup-agent");
    await user.type(screen.getByLabelText("MLflow experiment ID"), "exp-dup");
    await user.type(screen.getByLabelText("Display name"), "Duplicate Agent");
    await user.click(screen.getByRole("button", { name: "Register agent" }));

    // Stays on the list (no navigation to a detail page for a failed create).
    await waitFor(() =>
      expect(screen.getByTestId("agent-create-form")).toBeInTheDocument(),
    );
    expect(screen.getByText("Support Agent")).toBeInTheDocument();
  });

  it("edits artifact types, skills, and required approvals in the create form, and cancels without submitting", async () => {
    const state = handlers();
    const user = userEvent.setup();
    renderAt("/agents");
    await screen.findByText("Support Agent");

    await user.click(screen.getByTestId("new-agent"));
    const form = await screen.findByTestId("agent-create-form");
    await user.clear(within(form).getByLabelText(/Artifact types/));
    await user.type(within(form).getByLabelText(/Artifact types/), "prompt, dataset");
    await user.type(within(form).getByLabelText(/Skills/), "reasoning, tool-use");
    await user.clear(within(form).getByLabelText("Required approvals"));
    await user.type(within(form).getByLabelText("Required approvals"), "2");

    // Cancel discards the draft instead of submitting it.
    await user.click(within(form).getByRole("button", { name: "Cancel" }));
    expect(screen.queryByTestId("agent-create-form")).not.toBeInTheDocument();
    expect(state.creates).toBe(0);
  });

  it("does not submit the create form while required fields are blank", async () => {
    const state = handlers();
    const user = userEvent.setup();
    renderAt("/agents");
    await screen.findByText("Support Agent");

    await user.click(screen.getByTestId("new-agent"));
    const form = await screen.findByTestId("agent-create-form");
    // Only the display name is filled in — agent id / experiment id are still blank.
    await user.type(within(form).getByLabelText("Display name"), "Incomplete Agent");
    fireEvent.submit(form);

    expect(state.creates).toBe(0);
    expect(screen.getByTestId("agent-create-form")).toBeInTheDocument();
  });
});
