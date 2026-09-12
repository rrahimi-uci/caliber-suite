/**
 * AgentDetail page — configuration, preflight, revision history, and the
 * enable/disable/delete lifecycle controls.
 *
 * `src/pages/__tests__/agents.test.tsx` already covers the shared happy-path
 * flow (edit + save, disable, viewer read-only mode, the preflight verdict
 * matrix). This file focuses on what that one doesn't: the standalone
 * loading/not-found states, every mutation's *failure* branch, the full
 * edit-form field surface, and the delete confirm/cancel dance.
 */

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { http, HttpResponse } from "msw";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { afterAll, afterEach, beforeAll, describe, expect, it, vi } from "vitest";

import type { AgentConfig } from "@/api/types";
import { AgentDetail } from "@/pages/AgentDetail";
import { server } from "@/test/server";

vi.mock("@/lib/toast", () => ({
  showToast: {
    success: vi.fn(),
    error: vi.fn(),
    info: vi.fn(),
    warning: vi.fn(),
  },
}));

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

/** Registers the queries AgentDetail always fires, independent of the main
 * agent fetch's own state — a test rendering the loading/not-found state
 * still needs these satisfied or MSW's strict mode fails the request. */
function auxHandlers(agent: AgentConfig = agentFixture()): void {
  server.use(
    http.get(`${API_BASE}/agents/:id/skills`, () =>
      HttpResponse.json(envelope({ skills: [], missing: [] })),
    ),
    http.get(`${API_BASE}/agents/:agentId/experiment`, () =>
      HttpResponse.json(
        envelope({
          configured_experiment_id: agent.experiment_id,
          status: "reachable",
          detail: `resolved ${agent.experiment_id}`,
          experiment_id: agent.experiment_id,
          name: agent.agent_id,
          lifecycle_stage: "active",
        }),
      ),
    ),
    http.get(`${API_BASE}/audit-log`, () =>
      HttpResponse.json(envelope({ entries: [], total: 0, limit: 100, offset: 0 })),
    ),
  );
}

function baseHandlers(
  agent: AgentConfig = agentFixture(),
  options: { admin?: boolean } = {},
) {
  const isAdmin = options.admin ?? true;
  auxHandlers(agent);
  server.use(
    http.get(`${API_BASE}/me`, () =>
      HttpResponse.json(
        envelope({
          user_id: "@test",
          scopes: isAdmin
            ? ["caliber.viewer", "caliber.operator", "caliber.admin"]
            : ["caliber.viewer"],
          is_admin: isAdmin,
        }),
      ),
    ),
    http.get(`${API_BASE}/agents/:id`, () => HttpResponse.json(envelope(agent))),
  );
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
          <Route path="/agents" element={<p>Agents list</p>} />
          <Route path="/agents/:agentId" element={<AgentDetail />} />
        </Routes>
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

beforeAll(() => server.listen({ onUnhandledRequest: "error" }));
afterEach(() => {
  server.resetHandlers();
  vi.clearAllMocks();
});
afterAll(() => server.close());

describe("AgentDetail", () => {
  it("shows a loading state before the agent resolves", async () => {
    auxHandlers();
    const gate = new Promise<void>(() => undefined); // never resolves within the test
    server.use(
      http.get(`${API_BASE}/me`, () =>
        HttpResponse.json(envelope({ user_id: "@test", scopes: [], is_admin: true })),
      ),
      http.get(`${API_BASE}/agents/:id`, async () => {
        await gate;
        return HttpResponse.json(envelope(agentFixture()));
      }),
    );
    renderAt("/agents/support-agent");

    expect(await screen.findByText("Loading agent…")).toBeInTheDocument();
  });

  it("shows the API error message when the agent fails to load", async () => {
    auxHandlers();
    server.use(
      http.get(`${API_BASE}/me`, () =>
        HttpResponse.json(envelope({ user_id: "@test", scopes: [], is_admin: true })),
      ),
      http.get(`${API_BASE}/agents/:id`, () =>
        HttpResponse.json({ detail: "agent store unreachable" }, { status: 500 }),
      ),
    );
    renderAt("/agents/support-agent");

    expect(await screen.findByRole("alert")).toHaveTextContent("agent store unreachable");
  });

  it("navigates back to the agents list via the back button", async () => {
    baseHandlers();
    const user = userEvent.setup();
    renderAt("/agents/support-agent");

    await screen.findByRole("heading", { name: "Support Agent" });
    await user.click(screen.getByRole("button", { name: "← Agents" }));

    expect(await screen.findByText("Agents list")).toBeInTheDocument();
  });

  it("edits every field of the configuration form and saves the full payload", async () => {
    let patchBody: Record<string, unknown> | null = null;
    baseHandlers();
    server.use(
      http.patch(`${API_BASE}/agents/:id`, async ({ request }) => {
        patchBody = (await request.json()) as Record<string, unknown>;
        return HttpResponse.json(envelope({ ...agentFixture(), ...patchBody }));
      }),
    );
    const user = userEvent.setup();
    renderAt("/agents/support-agent");
    await screen.findByRole("heading", { name: "Support Agent" });

    await user.click(screen.getByRole("button", { name: "Edit" }));
    await screen.findByTestId("agent-edit-form");

    await user.clear(screen.getByLabelText("Artifact types"));
    await user.type(screen.getByLabelText("Artifact types"), "prompt, workflow");
    await user.clear(screen.getByLabelText("Skill names"));
    await user.type(screen.getByLabelText("Skill names"), "reasoning, tool-use");
    await user.clear(screen.getByLabelText("Optimize for"));
    await user.type(screen.getByLabelText("Optimize for"), "cost");
    await user.type(screen.getByLabelText("Collaboration mode"), "sequential");
    await user.clear(screen.getByLabelText("Required approvals"));
    await user.type(screen.getByLabelText("Required approvals"), "3");
    // `userEvent.type` parses `{`/`}` as special-key syntax, so JSON payloads go
    // through `fireEvent.change` instead.
    fireEvent.change(screen.getByLabelText("Evaluation thresholds JSON"), {
      target: { value: '{"minimum_score": 0.9}' },
    });
    fireEvent.change(screen.getByLabelText("Approval policy JSON"), {
      target: { value: "{}" },
    });

    await user.click(screen.getByRole("button", { name: "Save configuration" }));

    await waitFor(() => expect(patchBody).not.toBeNull());
    expect(patchBody).toMatchObject({
      artifact_types: ["prompt", "workflow"],
      optimize_for: "cost",
      collaboration_mode: "sequential",
      required_approvals: 3,
      eval_thresholds: { minimum_score: 0.9 },
      approval_policy: {},
    });
    expect((patchBody as unknown as Record<string, unknown>).optimizer_config).toMatchObject({
      skills: ["reasoning", "tool-use"],
    });
    expect(screen.queryByTestId("agent-edit-form")).not.toBeInTheDocument();
    const { showToast } = await import("@/lib/toast");
    expect(showToast.success).toHaveBeenCalledWith("Agent configuration saved");
  });

  it("cancels an edit without saving", async () => {
    baseHandlers();
    const user = userEvent.setup();
    renderAt("/agents/support-agent");
    await screen.findByRole("heading", { name: "Support Agent" });

    await user.click(screen.getByRole("button", { name: "Edit" }));
    await screen.findByTestId("agent-edit-form");
    await user.clear(screen.getByLabelText("Display name"));
    await user.type(screen.getByLabelText("Display name"), "Renamed");
    await user.click(screen.getByRole("button", { name: "Cancel" }));

    expect(screen.queryByTestId("agent-edit-form")).not.toBeInTheDocument();
    expect(screen.getByRole("heading", { name: "Support Agent" })).toBeInTheDocument();
  });

  it("shows a local form error for invalid JSON without calling the API", async () => {
    let patched = false;
    baseHandlers();
    server.use(
      http.patch(`${API_BASE}/agents/:id`, () => {
        patched = true;
        return HttpResponse.json(envelope(agentFixture()));
      }),
    );
    const user = userEvent.setup();
    renderAt("/agents/support-agent");
    await screen.findByRole("heading", { name: "Support Agent" });

    await user.click(screen.getByRole("button", { name: "Edit" }));
    const form = await screen.findByTestId("agent-edit-form");
    fireEvent.change(screen.getByLabelText("Optimizer configuration JSON"), {
      target: { value: "{not json" },
    });
    await user.click(screen.getByRole("button", { name: "Save configuration" }));

    expect(await screen.findByRole("alert")).toHaveTextContent(/Unexpected token|JSON/i);
    expect(patched).toBe(false);
    expect(form).toBeInTheDocument();
  });

  it("rejects a syntactically valid JSON array as a config value", async () => {
    // `parseObject` requires an object, not just parseable JSON — an array or a
    // bare primitive must be rejected with the same "must be a JSON object" error.
    baseHandlers();
    const user = userEvent.setup();
    renderAt("/agents/support-agent");
    await screen.findByRole("heading", { name: "Support Agent" });

    await user.click(screen.getByRole("button", { name: "Edit" }));
    await screen.findByTestId("agent-edit-form");
    fireEvent.change(screen.getByLabelText("Evaluation thresholds JSON"), {
      target: { value: "[1, 2, 3]" },
    });
    await user.click(screen.getByRole("button", { name: "Save configuration" }));

    expect(await screen.findByRole("alert")).toHaveTextContent(
      "Evaluation thresholds must be a JSON object",
    );
  });

  it("shows a save-failed toast when the update call is rejected", async () => {
    baseHandlers();
    server.use(
      http.patch(`${API_BASE}/agents/:id`, () =>
        HttpResponse.json({ detail: "name already taken" }, { status: 409 }),
      ),
    );
    const user = userEvent.setup();
    renderAt("/agents/support-agent");
    await screen.findByRole("heading", { name: "Support Agent" });

    await user.click(screen.getByRole("button", { name: "Edit" }));
    await screen.findByTestId("agent-edit-form");
    await user.click(screen.getByRole("button", { name: "Save configuration" }));

    const { showToast } = await import("@/lib/toast");
    await waitFor(() =>
      expect(showToast.error).toHaveBeenCalledWith(
        expect.stringContaining("Save failed"),
      ),
    );
    // The form stays open on failure so the operator can retry.
    expect(screen.getByTestId("agent-edit-form")).toBeInTheDocument();
  });

  it("shows an 'Agent enabled' toast when re-enabling a disabled agent", async () => {
    baseHandlers(agentFixture({ enabled: false }));
    server.use(
      http.patch(`${API_BASE}/agents/:id`, async ({ request }) => {
        const body = (await request.json()) as Record<string, unknown>;
        return HttpResponse.json(envelope(agentFixture({ ...body, enabled: true })));
      }),
    );
    const user = userEvent.setup();
    renderAt("/agents/support-agent");
    await screen.findByRole("heading", { name: "Support Agent" });

    expect(screen.getByText("Disabled")).toBeInTheDocument();
    await user.click(screen.getByTestId("agent-toggle-enabled"));

    const { showToast } = await import("@/lib/toast");
    await waitFor(() => expect(showToast.success).toHaveBeenCalledWith("Agent enabled"));
  });

  it("shows a status-update-failed toast when enable/disable is rejected", async () => {
    baseHandlers();
    server.use(
      http.patch(`${API_BASE}/agents/:id`, () =>
        HttpResponse.json({ detail: "agent is locked" }, { status: 409 }),
      ),
    );
    const user = userEvent.setup();
    renderAt("/agents/support-agent");
    await screen.findByRole("heading", { name: "Support Agent" });

    await user.click(screen.getByTestId("agent-toggle-enabled"));

    const { showToast } = await import("@/lib/toast");
    await waitFor(() =>
      expect(showToast.error).toHaveBeenCalledWith(
        expect.stringContaining("Status update failed"),
      ),
    );
  });

  it("confirms, cancels, and finally executes a permanent delete", async () => {
    let deleteCalls = 0;
    baseHandlers();
    server.use(
      http.delete(`${API_BASE}/agents/:id`, () => {
        deleteCalls += 1;
        return HttpResponse.json(envelope({ agent_id: "support-agent", deleted: true }));
      }),
    );
    const user = userEvent.setup();
    renderAt("/agents/support-agent");
    await screen.findByRole("heading", { name: "Support Agent" });

    await user.click(screen.getByRole("button", { name: "Delete permanently…" }));
    expect(screen.getByText("Delete agent and dependent records?")).toBeInTheDocument();

    // Cancelling the confirm step makes no request and returns to the initial button.
    await user.click(screen.getByRole("button", { name: "Cancel" }));
    expect(screen.getByRole("button", { name: "Delete permanently…" })).toBeInTheDocument();
    expect(deleteCalls).toBe(0);

    await user.click(screen.getByRole("button", { name: "Delete permanently…" }));
    await user.click(screen.getByTestId("confirm-delete-agent"));

    await waitFor(() => expect(deleteCalls).toBe(1));
    const { showToast } = await import("@/lib/toast");
    expect(showToast.success).toHaveBeenCalledWith(
      "Agent and dependent refinement records deleted",
    );
    // A successful delete navigates back to the agents list.
    expect(await screen.findByText("Agents list")).toBeInTheDocument();
  });

  it("shows a delete-failed toast and stays on the page when deletion is rejected", async () => {
    baseHandlers();
    server.use(
      http.delete(`${API_BASE}/agents/:id`, () =>
        HttpResponse.json({ detail: "agent has active refinement runs" }, { status: 409 }),
      ),
    );
    const user = userEvent.setup();
    renderAt("/agents/support-agent");
    await screen.findByRole("heading", { name: "Support Agent" });

    await user.click(screen.getByRole("button", { name: "Delete permanently…" }));
    await user.click(screen.getByTestId("confirm-delete-agent"));

    const { showToast } = await import("@/lib/toast");
    await waitFor(() =>
      expect(showToast.error).toHaveBeenCalledWith(expect.stringContaining("Delete failed")),
    );
    expect(screen.getByRole("heading", { name: "Support Agent" })).toBeInTheDocument();
  });

  it("shows a disabled agent's negative preflight verdict and admin-empty revision history", async () => {
    baseHandlers(agentFixture({ enabled: false }));
    server.use(
      http.get(`${API_BASE}/agents/:id/skills`, () =>
        HttpResponse.json(envelope({ skills: [], missing: ["ghost-skill"] })),
      ),
    );
    const user = userEvent.setup();
    renderAt("/agents/support-agent");
    await screen.findByRole("heading", { name: "Support Agent" });

    expect(screen.getByText("Disabled")).toBeInTheDocument();
    await user.click(screen.getByTestId("agent-preflight"));

    const results = screen.getByTestId("agent-preflight-results");
    expect(results).toHaveTextContent("Disabled agents are not claimed by workers");
    expect(results).toHaveTextContent("Missing: ghost-skill");
    expect(screen.getByText("No audit revisions recorded.")).toBeInTheDocument();
  });

  it("renders a revision-history entry as an expandable audit record", async () => {
    baseHandlers();
    server.use(
      http.get(`${API_BASE}/audit-log`, () =>
        HttpResponse.json(
          envelope({
            entries: [
              {
                log_id: 7,
                timestamp: "2025-03-01T00:00:00Z",
                actor: "@admin",
                action: "update_agent",
                entity_type: "agent",
                entity_id: "support-agent",
                details: { changes: { enabled: { from: true, to: false } } },
              },
            ],
            total: 1,
            limit: 100,
            offset: 0,
          }),
        ),
      ),
    );
    renderAt("/agents/support-agent");
    await screen.findByRole("heading", { name: "Support Agent" });

    expect(await screen.findByText(/update agent · @admin/i)).toBeInTheDocument();
    expect(screen.getByText(/"enabled"/)).toBeInTheDocument();
    expect(screen.queryByText("No audit revisions recorded.")).not.toBeInTheDocument();
  });

  it("shows the skills-check error message when the skills lookup fails", async () => {
    baseHandlers();
    server.use(
      http.get(`${API_BASE}/agents/:id/skills`, () =>
        HttpResponse.json({ detail: "skill registry unavailable" }, { status: 500 }),
      ),
    );
    const user = userEvent.setup();
    renderAt("/agents/support-agent");
    await screen.findByRole("heading", { name: "Support Agent" });

    await user.click(screen.getByTestId("agent-preflight"));
    expect(await screen.findByText("skill registry unavailable")).toBeInTheDocument();
  });
});
