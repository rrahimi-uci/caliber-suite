import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor } from "@testing-library/react";
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
  });
});
