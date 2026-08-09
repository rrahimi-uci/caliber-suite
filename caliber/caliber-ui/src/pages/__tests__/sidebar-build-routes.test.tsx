import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { http, HttpResponse } from "msw";
import { MemoryRouter } from "react-router-dom";
import {
  afterAll,
  afterEach,
  beforeAll,
  describe,
  expect,
  it,
  vi,
} from "vitest";

import { App } from "@/App";
import {
  clearLocalAuthSession,
  createLocalAuthSession,
  saveLocalAuthSession,
} from "@/auth/localAuth";
import { server } from "@/test/server";

const API_BASE = "/ajax-api/2.0/mlflow/caliber";

function envelope<T>(data: T): { data: T } {
  return { data };
}

class MockEventSource {
  readonly listeners = new Map<string, Set<(event: MessageEvent) => void>>();
  onerror: ((event: Event) => void) | null = null;

  addEventListener(
    event: string,
    handler: (event: MessageEvent) => void,
  ): void {
    const handlers =
      this.listeners.get(event) ?? new Set<(event: MessageEvent) => void>();
    handlers.add(handler);
    this.listeners.set(event, handlers);
  }

  removeEventListener(
    event: string,
    handler: (event: MessageEvent) => void,
  ): void {
    this.listeners.get(event)?.delete(handler);
  }

  close(): void {}
}

function renderApp(initialPath: string): ReturnType<typeof render> {
  saveLocalAuthSession(createLocalAuthSession("admin"));
  const queryClient = new QueryClient({
    defaultOptions: {
      queries: { retry: false, gcTime: 0 },
      mutations: { retry: false },
    },
  });

  return render(
    <QueryClientProvider client={queryClient}>
      <MemoryRouter
        future={{ v7_startTransition: true, v7_relativeSplatPath: true }}
        initialEntries={[initialPath]}
      >
        <App />
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

/**
 * Click a sidebar destination, expanding its group first if needed.
 *
 * Only the group owning the current route starts open, so a chain of
 * cross-group navigations has to expand as it goes. Expanding on demand — via
 * whatever the sidebar renders — keeps this test from restating the
 * label-to-group mapping, which would then need editing every time the
 * information architecture moved.
 */
async function navigateVia(
  user: ReturnType<typeof userEvent.setup>,
  name: string,
): Promise<void> {
  if (!screen.queryByRole("link", { name })) {
    for (const toggle of screen.getAllByTestId(/^nav-group-toggle-/)) {
      if (toggle.getAttribute("aria-expanded") === "false") {
        await user.click(toggle);
      }
    }
  }
  await user.click(screen.getByRole("link", { name }));
}

beforeAll(() => {
  server.listen({ onUnhandledRequest: "error" });
  vi.stubGlobal("EventSource", MockEventSource);
});

afterEach(() => {
  cleanup();
  server.resetHandlers();
  clearLocalAuthSession();
  // The sidebar persists open groups; leaking that state would make the
  // expand-on-demand helper above a no-op in whichever test ran second.
  window.localStorage.clear();
});

afterAll(() => {
  server.close();
  vi.unstubAllGlobals();
});

describe("Sidebar Build routes", () => {
  it("opens the wired sidebar routes without falling through to Not found", async () => {
    server.use(
      http.get(`${API_BASE}/auth/session`, () =>
        HttpResponse.json(
          envelope({
            user_id: "admin",
            scopes: ["admin"],
            is_admin: true,
            auth_mode: "session",
            authenticated_by: "session",
            login_required: false,
          }),
        ),
      ),
      http.get(`${API_BASE}/health`, () =>
        HttpResponse.json(envelope({ status: "ok", version: "test" })),
      ),
      http.get(`${API_BASE}/dashboard/summary`, () =>
        HttpResponse.json(
          envelope({
            agents_total: 3,
            agents_enabled: 3,
            verification_pending: 0,
            verification_pending_critical: 0,
            jobs_queued: 0,
            jobs_running: 0,
            jobs_awaiting_approval: 0,
            jobs_completed: 0,
            jobs_failed: 0,
            jobs_rejected: 0,
            approvals_pending: 0,
            generated_at: "2026-06-03T00:00:00Z",
          }),
        ),
      ),
      http.get(`${API_BASE}/prompts`, () =>
        HttpResponse.json(
          envelope([
            {
              agent_id: "support-agent",
              agent_name: "Support Agent",
              agent_enabled: true,
              prompt_name: "support-agent",
              version: 3,
              alias: "prod",
              template_preview: "Be concise and helpful.",
              template_length: 22,
              approval_id: null,
              artifact_ref: "prompts:/support-agent/3",
              has_prompt: true,
              source: "mlflow",
            },
          ]),
        ),
      ),
      http.get(`${API_BASE}/agents`, () =>
        HttpResponse.json(
          envelope([
            {
              agent_id: "support-agent",
              experiment_id: "exp-support",
              name: "Support Agent",
              owner: "@sarah",
              artifact_types: ["prompt"],
              required_approvals: 1,
              optimize_for: "quality",
              eval_thresholds: {},
              optimizer_config: {},
              enabled: true,
              created_at: "2026-06-03T00:00:00Z",
              updated_at: "2026-06-03T00:00:00Z",
            },
          ]),
        ),
      ),
      http.get(`${API_BASE}/tools`, () =>
        HttpResponse.json(
          envelope([
            {
              tool_id: "tool-001",
              name: "search_docs",
              version: "1.0.0",
              description: "Search internal docs",
              module_path: "tools.docs",
              callable_name: "search_docs",
              input_schema: { type: "object", properties: {} },
              output_schema: { type: "object", properties: {} },
              side_effect_level: "read",
              requires_approval: false,
              allow_in_preview: true,
              secret_refs: [],
              owner: "@sarah",
              status: "active",
              created_at: "2026-06-03T00:00:00Z",
              updated_at: "2026-06-03T00:00:00Z",
            },
          ]),
        ),
      ),
      http.get(`${API_BASE}/mcp-servers`, () =>
        HttpResponse.json(
          envelope([
            {
              server_id: "mcp-001",
              name: "GitHub",
              description: "GitHub MCP server",
              transport: "streamable-http",
              uri: "https://api.githubcopilot.com/mcp/",
              command: "",
              args: [],
              env: {},
              headers: {},
              auth_type: "none",
              auth_config: {},
              tool_policies: {},
              icon: "github",
              owner: "@sarah",
              status: "active",
              connection_error: null,
              discovered_tools: [],
              last_connected_at: null,
              created_at: "2026-06-03T00:00:00Z",
              updated_at: "2026-06-03T00:00:00Z",
            },
          ]),
        ),
      ),
      http.get(`${API_BASE}/workflows`, () =>
        HttpResponse.json(
          envelope([
            {
              workflow_id: "wf-001",
              name: "Support Workflow",
              description: "Default support workflow",
              owner: "@sarah",
              status: "active",
              created_at: "2026-06-03T00:00:00Z",
              updated_at: "2026-06-03T00:00:00Z",
            },
          ]),
        ),
      ),
      http.get(`${API_BASE}/knowledge-bases/KB-1/versions`, () =>
        HttpResponse.json(envelope([])),
      ),
      http.get(`${API_BASE}/knowledge-bases/KB-1/runs`, () =>
        HttpResponse.json(envelope([])),
      ),
    );

    const user = userEvent.setup();
    // Routes are React.lazy-loaded, so each navigation resolves a dynamic
    // import() before the heading renders. On slower CI runners the default
    // 1000ms findByRole timeout can lapse mid-chain (the Settings leg is the
    // heaviest), so give the lazy-route waits a generous timeout.
    renderApp("/prompts");
    expect(
      await screen.findByRole(
        "heading",
        { name: "Prompts" },
        { timeout: 5000 },
      ),
    ).toBeInTheDocument();
    expect(
      screen.queryByRole("heading", { name: "Not found" }),
    ).not.toBeInTheDocument();

    await navigateVia(user, "Tools");
    expect(
      await screen.findByRole("heading", { name: "Tools" }, { timeout: 5000 }),
    ).toBeInTheDocument();
    expect(
      screen.queryByRole("heading", { name: "Not found" }),
    ).not.toBeInTheDocument();

    await navigateVia(user, "MCP Servers");
    expect(
      await screen.findByRole(
        "heading",
        { name: "MCP Servers" },
        { timeout: 5000 },
      ),
    ).toBeInTheDocument();
    expect(
      screen.queryByRole("heading", { name: "Not found" }),
    ).not.toBeInTheDocument();

    await navigateVia(user, "Workflows");
    expect(
      await screen.findByRole(
        "heading",
        { name: "Workflows" },
        { timeout: 5000 },
      ),
    ).toBeInTheDocument();
    expect(
      screen.queryByRole("heading", { name: "Not found" }),
    ).not.toBeInTheDocument();

    await navigateVia(user, "Agents");
    expect(
      await screen.findByRole("heading", { name: "Agents" }, { timeout: 5000 }),
    ).toBeInTheDocument();
    expect(
      screen.queryByRole("heading", { name: "Not found" }),
    ).not.toBeInTheDocument();

    await navigateVia(user, "Knowledge Bases");
    expect(
      await screen.findByRole(
        "heading",
        { name: "Knowledge Bases" },
        { timeout: 5000 },
      ),
    ).toBeInTheDocument();
    expect(
      screen.queryByRole("heading", { name: "Not found" }),
    ).not.toBeInTheDocument();

    await navigateVia(user, "Object Store");
    expect(
      await screen.findByRole(
        "heading",
        { name: "Object Store" },
        { timeout: 5000 },
      ),
    ).toBeInTheDocument();
    expect(
      screen.queryByRole("heading", { name: "Not found" }),
    ).not.toBeInTheDocument();

    await navigateVia(user, "Settings");
    expect(
      await screen.findByRole(
        "heading",
        { name: "Settings" },
        { timeout: 5000 },
      ),
    ).toBeInTheDocument();
    expect(
      screen.queryByRole("heading", { name: "Not found" }),
    ).not.toBeInTheDocument();
  });
});
