import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { http, HttpResponse } from "msw";
import { MemoryRouter, useLocation } from "react-router-dom";
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
const SIDEBAR_COLLAPSE_KEY = "caliber.sidebar.collapsed";
const ASSISTANT_WIDTH_KEY = "caliber.assistant.panel.width";

function envelope<T>(data: T): { data: T } {
  return { data };
}

class MockEventSource {
  static instances: MockEventSource[] = [];

  static reset(): void {
    MockEventSource.instances = [];
  }

  readonly listeners = new Map<string, Set<(event: MessageEvent) => void>>();
  onerror: ((event: Event) => void) | null = null;

  constructor(readonly url: string) {
    MockEventSource.instances.push(this);
  }

  addEventListener(event: string, handler: (event: MessageEvent) => void): void {
    const handlers = this.listeners.get(event) ?? new Set<(event: MessageEvent) => void>();
    handlers.add(handler);
    this.listeners.set(event, handlers);
  }

  removeEventListener(event: string, handler: (event: MessageEvent) => void): void {
    this.listeners.get(event)?.delete(handler);
  }

  close(): void { }

  emit(event: string, payload: Record<string, unknown>): void {
    const frame = { data: JSON.stringify(payload) } as MessageEvent;
    for (const handler of this.listeners.get(event) ?? []) {
      handler(frame);
    }
  }
}

function CurrentPath(): JSX.Element {
  const location = useLocation();
  return <div data-testid="current-path">{location.pathname}</div>;
}

function renderApp(
  initialPath: string,
  { authenticated = true }: { authenticated?: boolean } = {},
): ReturnType<typeof render> {
  if (authenticated) {
    saveLocalAuthSession(createLocalAuthSession("admin"));
  }
  const queryClient = new QueryClient({
    defaultOptions: {
      queries: { retry: false, gcTime: 0 },
      mutations: { retry: false },
    },
  });

  return render(
    <QueryClientProvider client={queryClient}>
      <MemoryRouter future={{ v7_startTransition: true, v7_relativeSplatPath: true }} initialEntries={[initialPath]}>
        <CurrentPath />
        <App />
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

beforeAll(() => {
  class ResizeObserverMock {
    observe(): void {}
    unobserve(): void {}
    disconnect(): void {}
  }
  Object.defineProperty(globalThis, "ResizeObserver", {
    value: ResizeObserverMock,
    writable: true,
  });
  server.listen({ onUnhandledRequest: "error" });
  vi.stubGlobal("EventSource", MockEventSource);
});

afterEach(() => {
  cleanup();
  server.resetHandlers();
  MockEventSource.reset();
  clearLocalAuthSession();
  window.localStorage.removeItem(SIDEBAR_COLLAPSE_KEY);
  window.localStorage.removeItem(ASSISTANT_WIDTH_KEY);
});

afterAll(() => {
  server.close();
  vi.unstubAllGlobals();
});

describe("App shell end-to-end journeys", () => {
  it("shows the login page and signs in against the server", async () => {
    server.use(
      http.get(`${API_BASE}/health`, () =>
        HttpResponse.json(envelope({ status: "ok", version: "test" })),
      ),
      // Credentials are verified server-side (C1). There is no default credential and
      // no browser-synthesised identity, so this journey needs the real endpoint.
      http.post(`${API_BASE}/auth/login`, () =>
        HttpResponse.json(
          envelope({ user_id: "@local-admin", expires_at: "2026-12-31T00:00:00Z" }),
        ),
      ),
      http.get(`${API_BASE}/dashboard/summary`, () =>
        HttpResponse.json(
          envelope({
            agents_total: 0,
            agents_enabled: 0,
            verification_pending: 0,
            verification_pending_critical: 0,
            jobs_queued: 0,
            jobs_running: 0,
            jobs_awaiting_approval: 0,
            jobs_completed: 0,
            jobs_failed: 0,
            jobs_rejected: 0,
            approvals_pending: 0,
            generated_at: "2026-06-02T00:00:00Z",
          }),
        ),
      ),
    );

    renderApp("/tools", { authenticated: false });
    await waitFor(() => expect(screen.getByTestId("current-path")).toHaveTextContent("/login"));
    expect(
      screen.getByRole("heading", { name: "Build Trusted Agentic Workflows with Verification and Calibration" }),
    ).toBeInTheDocument();
    expect(
      screen.getByText(
        /Design, evaluate, and refine prompts, tools, skills, and multi-agent workflows/i,
      ),
    ).toBeInTheDocument();
    // Typed, not prefilled: the form used to arrive with admin/admin already in it.
    await userEvent.type(screen.getByLabelText(/username/i), "@local-admin");
    await userEvent.type(screen.getByLabelText(/^password$/i), "correct-horse-battery");
    await userEvent.click(screen.getByRole("button", { name: /^Sign in$/ }));
    expect(await screen.findByLabelText("CALIBER navigation")).toBeInTheDocument();
    await waitFor(() => expect(screen.getByTestId("current-path")).toHaveTextContent("/"));
    // Local storage holds display state only. The session is an HttpOnly cookie the
    // browser cannot read, which is the point — so no token may appear here.
    expect(window.localStorage.getItem("caliber.auth.session")).toContain("@local-admin");
    expect(window.localStorage.getItem("caliber.auth.session")).not.toContain("token");
  });

  it("logs out and clears the local session", async () => {
    server.use(
      http.get(`${API_BASE}/health`, () =>
        HttpResponse.json(envelope({ status: "ok", version: "test" })),
      ),
      http.get(`${API_BASE}/dashboard/summary`, () =>
        HttpResponse.json(
          envelope({
            agents_total: 0,
            agents_enabled: 0,
            verification_pending: 0,
            verification_pending_critical: 0,
            jobs_queued: 0,
            jobs_running: 0,
            jobs_awaiting_approval: 0,
            jobs_completed: 0,
            jobs_failed: 0,
            jobs_rejected: 0,
            approvals_pending: 0,
            generated_at: "2026-06-02T00:00:00Z",
          }),
        ),
      ),
    );

    // Logging out must revoke the session on the SERVER. Clearing local state alone
    // left the cookie valid, so "log out" did not end the session.
    let revoked = false;
    server.use(
      http.post(`${API_BASE}/auth/logout`, () => {
        revoked = true;
        return HttpResponse.json(envelope({ revoked: true }));
      }),
    );

    renderApp("/");

    expect(await screen.findByLabelText("CALIBER navigation")).toBeInTheDocument();
    // "@admin", not "@local-admin": identityForUsername no longer special-cases the
    // old default username, because there is no default credential to special-case.
    expect(window.localStorage.getItem("caliber.auth.session")).toContain("@admin");

    await userEvent.click(screen.getByRole("button", { name: /Log out/i }));

    await waitFor(() => expect(revoked).toBe(true));
    await waitFor(() => expect(screen.getByTestId("current-path")).toHaveTextContent("/login"));
    expect(window.localStorage.getItem("caliber.auth.session")).toBeNull();
  });

  it("renders workflow detail deep links through the real app router", async () => {
    server.use(
      http.get(`${API_BASE}/health`, () =>
        HttpResponse.json(envelope({ status: "ok", version: "test" })),
      ),
      http.get(`${API_BASE}/dashboard/summary`, () =>
        HttpResponse.json(
          envelope({
            agents_total: 1,
            agents_enabled: 1,
            verification_pending: 0,
            verification_pending_critical: 0,
            jobs_queued: 0,
            jobs_running: 0,
            jobs_awaiting_approval: 0,
            jobs_completed: 0,
            jobs_failed: 0,
            jobs_rejected: 0,
            approvals_pending: 0,
            generated_at: "2026-06-02T00:00:00Z",
          }),
        ),
      ),
      http.get(`${API_BASE}/workflows/WF-170c47e2`, () =>
        HttpResponse.json(
          envelope({
            workflow_id: "WF-170c47e2",
            name: "Support Workflow",
            description: "",
            owner: "@test",
            status: "active",
            default_experiment_id: null,
            created_at: "2026-06-02T00:00:00Z",
            updated_at: "2026-06-02T00:00:00Z",
          }),
        ),
      ),
      http.get(`${API_BASE}/workflows/WF-170c47e2/versions`, () =>
        HttpResponse.json(envelope([])),
      ),
      http.get(`${API_BASE}/workflows/WF-170c47e2/deployments`, () =>
        HttpResponse.json(envelope([])),
      ),
      http.get(`${API_BASE}/workflows/WF-170c47e2/promotions`, () =>
        HttpResponse.json(envelope([])),
      ),
      http.get(`${API_BASE}/workflows/WF-170c47e2/patches`, () =>
        HttpResponse.json(envelope([])),
      ),
      http.get(`${API_BASE}/workflows/WF-170c47e2/runs`, () =>
        HttpResponse.json(envelope([])),
      ),
      http.get(`${API_BASE}/workflows/WF-170c47e2/runs/stats`, () =>
        HttpResponse.json(
          envelope({
            workflow_id: "WF-170c47e2",
            total_runs: 0,
            matching_runs: 0,
            waiting_event_runs: 0,
            artifact_persistence: {
              failed: 0,
              persisted: 0,
            },
          }),
        ),
      ),
      http.get(`${API_BASE}/agents`, () =>
        HttpResponse.json(
          envelope([
            {
              agent_id: "support-agent",
              experiment_id: "exp-support",
              name: "Support Agent",
              owner: "@test",
              artifact_types: ["prompt"],
              eval_thresholds: {},
              optimizer_config: {},
              approval_policy: {},
              optimize_for: "quality",
              collaboration_mode: null,
              enabled: true,
              required_approvals: 1,
              created_at: "2026-06-02T00:00:00Z",
              updated_at: "2026-06-02T00:00:00Z",
            },
          ]),
        ),
      ),
      http.get(`${API_BASE}/workflows/WF-170c47e2/calibration/options`, () =>
        HttpResponse.json(
          envelope({
            supported_objectives: ["quality", "tool_adherence"],
            supported_move_set: ["add_grounding_guardrail"],
            scorer_options: ["quality_match", "tool_adherence"],
            default_budget: { max_candidates: 3, max_eval_examples: 20, min_examples: 2 },
            data: {
              workflow_version_id: null,
              deploy_gate_dataset: {
                available: false,
                reason: "No active deploy-gate eval dataset with non-superseded examples.",
              },
            },
          }),
        ),
      ),
    );

    renderApp("/workflows/WF-170c47e2");

    expect(await screen.findByTestId("workflow-detail", {}, { timeout: 5000 })).toBeInTheDocument();
    expect(await screen.findByRole("heading", { name: "Support Workflow" })).toBeInTheDocument();
    expect(screen.queryByRole("heading", { name: "Not found" })).not.toBeInTheDocument();
  });

  it("resolves workflow run routes through the real app router", async () => {
    const run = {
      workflow_run_id: "WR-170c47e2",
      workflow_id: "WF-170c47e2",
      project_id: null,
      tenant_id: null,
      workflow_version_id: "WFV-170c47e2",
      deployment_alias: null,
      mlflow_run_id: null,
      trace_id: "trace-run-170c47e2",
      session_id: null,
      status: "completed",
      source: "manual",
      priority: 0,
      queued_at: "2026-06-02T00:00:00Z",
      started_at: "2026-06-02T00:00:10Z",
      completed_at: "2026-06-02T00:01:00Z",
      current_node_id: "final",
      summary: {
        node_path: ["start", "support_agent", "final"],
        steps: [
          {
            node_id: "start",
            node_type: "start",
            status: "ok",
            output: "hello",
            tool_calls: [],
            handoff_target: null,
            detail: "",
            duration_ms: 0,
          },
        ],
      },
    };
    const version = {
      version_id: "WFV-170c47e2",
      workflow_id: "WF-170c47e2",
      version_number: 1,
      status: "published",
      manifest: {
        schema_version: 1,
        workflow_id: "WF-170c47e2",
        name: "Support Workflow",
        nodes: {
          start: {
            id: "start",
            type: "start",
            outputs: { user_message: { type: "string" } },
          },
          support_agent: {
            id: "support_agent",
            type: "agent",
            name: "support-agent",
            model: "inherit",
            instructions: { type: "inline", text: "hi" },
            tools: [],
            inputs: { input: { type: "string" } },
            outputs: { final_output: { type: "string" } },
          },
          final: {
            id: "final",
            type: "output",
            inputs: { response: { type: "string" } },
          },
        },
        edges: [
          {
            id: "e1",
            from: "start",
            to: "support_agent",
            map: { user_message: "input" },
          },
          {
            id: "e2",
            from: "support_agent",
            to: "final",
            map: { final_output: "response" },
          },
        ],
      },
      manifest_hash: "hash-run-170c47e2",
      compiler_version: null,
      compiled_artifact_uri: null,
      compiled_bundle: null,
      validation_report: null,
      created_by: "@test",
      created_at: "2026-06-02T00:00:00Z",
      published_by: "@test",
      published_at: "2026-06-02T00:00:00Z",
    };

    server.use(
      http.get(`${API_BASE}/health`, () =>
        HttpResponse.json(envelope({ status: "ok", version: "test" })),
      ),
      http.get(`${API_BASE}/dashboard/summary`, () =>
        HttpResponse.json(
          envelope({
            agents_total: 1,
            agents_enabled: 1,
            verification_pending: 0,
            verification_pending_critical: 0,
            jobs_queued: 0,
            jobs_running: 0,
            jobs_awaiting_approval: 0,
            jobs_completed: 0,
            jobs_failed: 0,
            jobs_rejected: 0,
            approvals_pending: 0,
            generated_at: "2026-06-02T00:00:00Z",
          }),
        ),
      ),
      http.get(`${API_BASE}/capabilities`, () =>
        HttpResponse.json(
          envelope({
            workflow_runs: {
              queue_enabled: true,
              supports_async_submit: true,
              supports_cancel: true,
              supports_retry: true,
              supports_resume: true,
              runtime_approvals_enabled: true,
              checkpointing_enabled: true,
              event_backend: "nats",
            },
            sync_workflow_version_run: true,
          }),
        ),
      ),
      http.get(`${API_BASE}/workflow-runs/WR-170c47e2`, () =>
        HttpResponse.json(envelope(run)),
      ),
      http.get(`${API_BASE}/workflow-runs/WR-170c47e2/lineage`, () =>
        HttpResponse.json(
          envelope({
            workflow_run_id: "WR-170c47e2",
            workflow_id: "WF-170c47e2",
            root_run_id: "WR-170c47e2",
            total_attempts: 1,
            parent_run_id: null,
            parent_count: 0,
            child_count: 0,
            missing_parent_id: null,
            truncated: false,
            runs: [run],
          }),
        ),
      ),
      http.get(`${API_BASE}/workflow-runs/WR-170c47e2/manifest`, () =>
        HttpResponse.json(
          envelope({
            workflow_run_id: "WR-170c47e2",
            workflow_id: "WF-170c47e2",
            workflow_version_id: "WFV-170c47e2",
            manifest_mode: "saved_version",
            manifest_hash: "hash-run-170c47e2",
            manifest: version.manifest,
          }),
        ),
      ),
      http.get(`${API_BASE}/workflow-runs/WR-170c47e2/approvals`, () =>
        HttpResponse.json(envelope([])),
      ),
      http.get(`${API_BASE}/workflow-runs/WR-170c47e2/events`, () =>
        HttpResponse.json(envelope([])),
      ),
      http.get(`${API_BASE}/workflow-runs/WR-170c47e2/checkpoints`, () =>
        HttpResponse.json(envelope([])),
      ),
      http.get(`${API_BASE}/workflow-runs/WR-170c47e2/files`, () =>
        HttpResponse.json(envelope({ items: [], next_cursor: null })),
      ),
      http.get(`${API_BASE}/workflows/WF-170c47e2`, () =>
        HttpResponse.json(
          envelope({
            workflow_id: "WF-170c47e2",
            name: "Support Workflow",
            description: "",
            owner: "@test",
            status: "active",
            default_experiment_id: null,
            created_at: "2026-06-02T00:00:00Z",
            updated_at: "2026-06-02T00:00:00Z",
          }),
        ),
      ),
      http.get(`${API_BASE}/workflows/WF-170c47e2/versions`, () =>
        HttpResponse.json(envelope([version])),
      ),
      http.get(`${API_BASE}/workflows/WF-170c47e2/deployments`, () =>
        HttpResponse.json(envelope([])),
      ),
      http.get(`${API_BASE}/workflows/WF-170c47e2/promotions`, () =>
        HttpResponse.json(envelope([])),
      ),
      http.get(`${API_BASE}/workflows/WF-170c47e2/patches`, () =>
        HttpResponse.json(envelope([])),
      ),
      http.get(`${API_BASE}/workflows/WF-170c47e2/runs`, ({ request }) => {
        const url = new URL(request.url);
        const hasCursor = url.searchParams.has("cursor");
        if (hasCursor || url.searchParams.has("limit") || url.searchParams.has("search")) {
          return HttpResponse.json({ data: [run], next_cursor: null });
        }
        return HttpResponse.json(envelope([run]));
      }),
      http.get(`${API_BASE}/workflows/WF-170c47e2/runs/stats`, () =>
        HttpResponse.json(
          envelope({
            workflow_id: "WF-170c47e2",
            total_runs: 1,
            matching_runs: 1,
            waiting_event_runs: 0,
            artifact_persistence: {
              failed: 0,
              persisted: 0,
            },
          }),
        ),
      ),
      http.get(`${API_BASE}/agents`, () =>
        HttpResponse.json(
          envelope([
            {
              agent_id: "support-agent",
              experiment_id: "exp-support",
              name: "Support Agent",
              owner: "@test",
              artifact_types: ["prompt"],
              eval_thresholds: {},
              optimizer_config: {},
              approval_policy: {},
              optimize_for: "quality",
              collaboration_mode: null,
              enabled: true,
              required_approvals: 1,
              created_at: "2026-06-02T00:00:00Z",
              updated_at: "2026-06-02T00:00:00Z",
            },
          ]),
        ),
      ),
      http.get(`${API_BASE}/workflows/WF-170c47e2/calibration/options`, () =>
        HttpResponse.json(
          envelope({
            supported_objectives: ["quality", "tool_adherence"],
            supported_move_set: ["add_grounding_guardrail"],
            scorer_options: ["quality_match", "tool_adherence"],
            default_budget: { max_candidates: 3, max_eval_examples: 20, min_examples: 2 },
            data: {
              workflow_version_id: "WFV-170c47e2",
              deploy_gate_dataset: {
                available: false,
                reason: "No active deploy-gate eval dataset with non-superseded examples.",
              },
            },
          }),
        ),
      ),
    );

    renderApp("/workflow-runs/WR-170c47e2");

    await waitFor(
      () => {
        expect(screen.getByTestId("current-path")).toHaveTextContent("/workflows/WF-170c47e2");
      },
      { timeout: 12000 },
    );
    expect(screen.queryByTestId("workflow-run-redirect-error")).not.toBeInTheDocument();
  }, 15000);

  it("navigates across routes and refreshes sidebar badges from SSE-triggered summary reload", async () => {
    // We intentionally return different summary payloads on subsequent calls so
    // we can prove a live event triggers a re-fetch and updates the shell UI.
    let summaryCallCount = 0;
    server.use(
      http.get(`${API_BASE}/health`, () =>
        HttpResponse.json(envelope({ status: "ok", version: "test" })),
      ),
      http.get(`${API_BASE}/dashboard/summary`, () => {
        summaryCallCount += 1;
        if (summaryCallCount === 1) {
          return HttpResponse.json(
            envelope({
              agents_total: 3,
              agents_enabled: 3,
              verification_pending: 5,
              verification_pending_critical: 1,
              jobs_queued: 2,
              jobs_running: 1,
              jobs_awaiting_approval: 1,
              jobs_completed: 8,
              jobs_failed: 0,
              jobs_rejected: 0,
              approvals_pending: 3,
              generated_at: "2026-06-02T00:00:00Z",
            }),
          );
        }
        return HttpResponse.json(
          envelope({
            agents_total: 3,
            agents_enabled: 3,
            verification_pending: 2,
            verification_pending_critical: 0,
            jobs_queued: 0,
            jobs_running: 1,
            jobs_awaiting_approval: 0,
            jobs_completed: 10,
            jobs_failed: 0,
            jobs_rejected: 0,
            approvals_pending: 1,
            generated_at: "2026-06-02T00:01:00Z",
          }),
        );
      }),
    );

    renderApp("/prompts");

    // First we verify the app boot path: route content renders and the dashboard
    // summary is fetched once at app scope.
    expect(await screen.findByRole("heading", { name: "Prompts" })).toBeInTheDocument();
    await waitFor(() => expect(summaryCallCount).toBe(1));

    // Then we navigate through the *real* app router using the sidebar link,
    // validating that shell + route composition works end-to-end.
    await userEvent.click(screen.getByRole("link", { name: "Skills" }));
    expect(await screen.findByRole("heading", { name: "Skills" })).toBeInTheDocument();
    expect(screen.getByText("reasoning-v1")).toBeInTheDocument();

    // Finally, emulate a backend SSE frame that should trigger
    // useDashboardSummary() to refresh from the server.
    const stream = MockEventSource.instances[0];
    expect(stream).toBeDefined();
    act(() => {
      stream?.emit("job.candidate_ready", {
        type: "job.candidate_ready",
        job_id: "RFN-123",
        agent_id: "support-agent",
      });
    });

    // The SSE frame re-fetches the dashboard summary at app scope.
    await waitFor(() => {
      expect(summaryCallCount).toBeGreaterThanOrEqual(2);
    });
  });

  it("persists sidebar collapse preference across remounts", async () => {
    server.use(
      http.get(`${API_BASE}/health`, () =>
        HttpResponse.json(envelope({ status: "ok", version: "test" })),
      ),
      http.get(`${API_BASE}/dashboard/summary`, () =>
        HttpResponse.json(
          envelope({
            agents_total: 1,
            agents_enabled: 1,
            verification_pending: 0,
            verification_pending_critical: 0,
            jobs_queued: 0,
            jobs_running: 0,
            jobs_awaiting_approval: 0,
            jobs_completed: 0,
            jobs_failed: 0,
            jobs_rejected: 0,
            approvals_pending: 0,
            generated_at: "2026-06-02T00:00:00Z",
          }),
        ),
      ),
    );

    // Mount #1: collapse the sidebar and assert the preference is written.
    const firstMount = renderApp("/prompts");
    expect(await screen.findByRole("heading", { name: "Prompts" })).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "Collapse sidebar" }));
    await waitFor(() => {
      expect(
        window.localStorage.getItem(SIDEBAR_COLLAPSE_KEY)
      ).toBe("true");
    });
    firstMount.unmount();

    // Mount #2: the app should boot in collapsed mode by reading localStorage.
    renderApp("/prompts");
    expect(await screen.findByRole("heading", { name: "Prompts" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Expand sidebar" })).toBeInTheDocument();
  });

  it("reserves page space when assistant drawer is open", async () => {
    server.use(
      http.get(`${API_BASE}/health`, () =>
        HttpResponse.json(envelope({ status: "ok", version: "test" })),
      ),
      http.get(`${API_BASE}/dashboard/summary`, () =>
        HttpResponse.json(
          envelope({
            agents_total: 1,
            agents_enabled: 1,
            verification_pending: 0,
            verification_pending_critical: 0,
            jobs_queued: 0,
            jobs_running: 0,
            jobs_awaiting_approval: 0,
            jobs_completed: 0,
            jobs_failed: 0,
            jobs_rejected: 0,
            approvals_pending: 0,
            generated_at: "2026-06-02T00:00:00Z",
          }),
        ),
      ),
    );

    renderApp("/prompts");
    expect(await screen.findByRole("heading", { name: "Prompts" })).toBeInTheDocument();

    const main = screen.getByRole("main");
    expect(main).toHaveAttribute("data-assistant-open", "false");

    await userEvent.click(screen.getByRole("button", { name: "Ask Aria" }));
    expect(await screen.findByTestId("assistant-panel")).toBeInTheDocument();
    expect(main).toHaveAttribute("data-assistant-open", "true");

    await userEvent.click(screen.getByLabelText("Close"));
    await waitFor(() => {
      expect(main).toHaveAttribute("data-assistant-open", "false");
    });
  });

  it("supports resizing and collapse/expand for the assistant drawer", async () => {
    server.use(
      http.get(`${API_BASE}/health`, () =>
        HttpResponse.json(envelope({ status: "ok", version: "test" })),
      ),
      http.get(`${API_BASE}/dashboard/summary`, () =>
        HttpResponse.json(
          envelope({
            agents_total: 1,
            agents_enabled: 1,
            verification_pending: 0,
            verification_pending_critical: 0,
            jobs_queued: 0,
            jobs_running: 0,
            jobs_awaiting_approval: 0,
            jobs_completed: 0,
            jobs_failed: 0,
            jobs_rejected: 0,
            approvals_pending: 0,
            generated_at: "2026-06-02T00:00:00Z",
          }),
        ),
      ),
    );

    renderApp("/prompts");
    expect(await screen.findByRole("heading", { name: "Prompts" })).toBeInTheDocument();

    const main = screen.getByRole("main");
    await userEvent.click(screen.getByRole("button", { name: "Ask Aria" }));
    expect(await screen.findByTestId("assistant-panel")).toBeInTheDocument();
    expect(main).toHaveAttribute("data-assistant-width", "380");

    fireEvent.mouseDown(screen.getByLabelText("Resize assistant panel"), { clientX: 1000 });
    fireEvent.mouseMove(window, { clientX: 900 });
    fireEvent.mouseUp(window);
    await waitFor(() => {
      expect(main).toHaveAttribute("data-assistant-width", "480");
    });

    await userEvent.click(screen.getByRole("button", { name: "Collapse assistant" }));
    await waitFor(() => {
      expect(main).toHaveAttribute("data-assistant-width", "64");
    });

    await userEvent.click(screen.getByRole("button", { name: "Expand assistant" }));
    await waitFor(() => {
      expect(main).toHaveAttribute("data-assistant-width", "480");
    });
  });

  it("renders an unrecognised URL as a 404, not an unbuilt-page stub", async () => {
    // Regression for ui-complete-report.md §10: the wildcard route rendered
    // "This page lands in a follow-up milestone", so a mistyped link read as a
    // missing CALIBER feature rather than a wrong address.
    renderApp("/definitely-not-a-caliber-page");

    const notFound = await screen.findByTestId("route-not-found");
    expect(notFound).toHaveTextContent("Page not found");
    expect(notFound).toHaveTextContent(/doesn.t match any CALIBER page/);
    expect(notFound).not.toHaveTextContent(/follow-up milestone/);
    expect(screen.getByRole("link", { name: "Go to the dashboard" })).toHaveAttribute(
      "href",
      "/",
    );
  });
});
