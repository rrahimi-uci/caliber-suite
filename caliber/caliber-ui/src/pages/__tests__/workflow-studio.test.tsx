import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { http, HttpResponse } from "msw";
import { afterAll, afterEach, beforeAll, describe, expect, it, vi } from "vitest";
import { MemoryRouter, Route, Routes } from "react-router-dom";

import type { ReactElement } from "react";

import { showToast } from "@/lib/toast";
import { McpServers } from "@/pages/McpServers";
import { ToolRegistry } from "@/pages/ToolRegistry";
import { WorkflowDetail } from "@/pages/WorkflowDetail";
import { WorkflowEditor } from "@/pages/WorkflowEditor";
import { Workflows } from "@/pages/Workflows";
import { server } from "@/test/server";

const streamState = vi.hoisted(() => ({
  event: null as Record<string, unknown> | null,
}));

vi.mock("@/hooks/useEventStream", () => ({
  useEventStream: () => {
    const event = streamState.event;
    streamState.event = null;
    return event;
  },
}));

// These suites exercise the dormant multi-stage (dev/staging/prod) workflow UI,
// including the Deployments and Promotions tabs. The shipping single-environment
// default (those tabs hidden) is covered in environment-single-env.test.tsx.
vi.mock("@/lib/environment", () => ({
  SINGLE_ENVIRONMENT: false,
  LIVE_ALIAS: "prod",
  DEPLOYMENT_ALIASES: ["dev", "staging", "prod"],
}));

vi.mock("@/lib/toast", () => ({
  showToast: { success: vi.fn(), error: vi.fn(), info: vi.fn(), warning: vi.fn() },
}));

const API_BASE = "/ajax-api/2.0/mlflow/caliber";
const NOW = "2026-05-30T00:00:00Z";

function envelope<T>(data: T): { data: T } {
  return { data };
}

function renderAt(ui: ReactElement, initialPath: string, routePath: string) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false, gcTime: 0 } } });
  const renderTree = () => (
    <QueryClientProvider client={qc}>
      <MemoryRouter future={{ v7_startTransition: true, v7_relativeSplatPath: true }} initialEntries={[initialPath]}>
        <Routes>
          <Route path={routePath} element={ui} />
          <Route path="/workflows" element={<div>WORKFLOWS ROUTE</div>} />
          <Route path="/workflows/:workflowId/editor/:versionId" element={<div>EDITOR ROUTE</div>} />
          <Route path="/workflows/:workflowId" element={<div>DETAIL ROUTE</div>} />
        </Routes>
      </MemoryRouter>
    </QueryClientProvider>
  );
  const rendered = render(renderTree());
  return {
    ...rendered,
    rerenderAt: () => rendered.rerender(renderTree()),
  };
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
});
afterEach(() => {
  cleanup();
  streamState.event = null;
  vi.clearAllMocks();
  server.resetHandlers();
});
afterAll(() => server.close());

function makeVersion(overrides: Record<string, unknown> = {}) {
  return {
    version_id: "WFV-1",
    workflow_id: "WF-1",
    version_number: 1,
    status: "draft",
    manifest: {
      schema_version: 1,
      workflow_id: "WF-1",
      name: "Support",
      nodes: {
        start: { id: "start", type: "start", outputs: { user_message: { type: "string" } } },
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
        final: { id: "final", type: "output", inputs: { response: { type: "string" } } },
      },
      edges: [
        { id: "e1", from: "start", to: "support_agent", map: { user_message: "input" } },
        { id: "e2", from: "support_agent", to: "final", map: { final_output: "response" } },
      ],
    },
    manifest_hash: "hash1",
    compiler_version: null,
    compiled_artifact_uri: null,
    compiled_bundle: null,
    validation_report: null,
    created_by: "@test",
    created_at: NOW,
    published_by: null,
    published_at: null,
    ...overrides,
  };
}

function makeWorkflow(overrides: Record<string, unknown> = {}) {
  return {
    workflow_id: "WF-1",
    name: "Support",
    description: "",
    owner: "@test",
    status: "active",
    default_experiment_id: null,
    created_at: NOW,
    updated_at: NOW,
    ...overrides,
  };
}

describe("Workflows page", () => {
  it("renders workflow cards", async () => {
    server.use(
      http.get(`${API_BASE}/workflows`, () =>
        HttpResponse.json(
          envelope([
            {
              workflow_id: "WF-1",
              name: "Support Refund",
              description: "",
              owner: "@sarah",
              status: "active",
              default_experiment_id: null,
              created_at: NOW,
              updated_at: NOW,
            },
          ]),
        ),
      ),
    );
    renderAt(<Workflows />, "/workflows", "/workflows");
    expect(await screen.findByText("Support Refund")).toBeInTheDocument();
  });

  it("shows empty state when no workflows", async () => {
    server.use(http.get(`${API_BASE}/workflows`, () => HttpResponse.json(envelope([]))));
    renderAt(<Workflows />, "/workflows", "/workflows");
    expect(await screen.findByTestId("workflows-empty")).toBeInTheDocument();
  });

  it("creates a workflow from a template and navigates to the editor", async () => {
    server.use(
      http.get(`${API_BASE}/workflows`, () => HttpResponse.json(envelope([]))),
      http.post(`${API_BASE}/workflows`, () =>
        HttpResponse.json(
          envelope({
            workflow_id: "WF-9",
            name: "New WF",
            description: "",
            owner: "",
            status: "active",
            default_experiment_id: null,
            created_at: NOW,
            updated_at: NOW,
          }),
          { status: 201 },
        ),
      ),
      http.post(`${API_BASE}/workflows/WF-9/versions`, () =>
        HttpResponse.json(envelope(makeVersion({ version_id: "WFV-9", workflow_id: "WF-9" })), {
          status: 201,
        }),
      ),
    );
    renderAt(<Workflows />, "/workflows", "/workflows");
    await userEvent.click(await screen.findByTestId("new-workflow"));
    await userEvent.type(screen.getByTestId("new-workflow-name"), "New WF");
    await userEvent.click(screen.getByTestId("template-single_agent"));
    expect(await screen.findByText("EDITOR ROUTE")).toBeInTheDocument();
  });

  it("offers the HITL governance template and posts its manifest", async () => {
    let versionBody: Record<string, unknown> | null = null;
    server.use(
      http.get(`${API_BASE}/workflows`, () => HttpResponse.json(envelope([]))),
      http.post(`${API_BASE}/workflows`, () =>
        HttpResponse.json(
          envelope({
            workflow_id: "WF-9",
            name: "Reviewed WF",
            description: "",
            owner: "",
            status: "active",
            default_experiment_id: null,
            created_at: NOW,
            updated_at: NOW,
          }),
          { status: 201 },
        ),
      ),
      http.post(`${API_BASE}/workflows/WF-9/versions`, async ({ request }) => {
        versionBody = (await request.json()) as Record<string, unknown>;
        return HttpResponse.json(envelope(makeVersion({ version_id: "WFV-9", workflow_id: "WF-9" })), {
          status: 201,
        });
      }),
    );
    renderAt(<Workflows />, "/workflows", "/workflows");
    await userEvent.click(await screen.findByTestId("new-workflow"));
    await userEvent.type(screen.getByTestId("new-workflow-name"), "Reviewed WF");
    await userEvent.click(screen.getByTestId("template-hitl_review"));

    expect(await screen.findByText("EDITOR ROUTE")).toBeInTheDocument();
    const nodes = (versionBody!.manifest as { nodes: Record<string, unknown> }).nodes;
    expect(nodes.pii_guard).toBeDefined();
    expect(nodes.review).toBeDefined();
  });

  it("renames a workflow inline from the card", async () => {
    let patchBody: Record<string, unknown> | null = null;
    server.use(
      http.get(`${API_BASE}/workflows`, () =>
        HttpResponse.json(
          envelope([
            {
              workflow_id: "WF-1",
              name: "Support Refund",
              description: "",
              owner: "@sarah",
              status: "active",
              default_experiment_id: null,
              created_at: NOW,
              updated_at: NOW,
            },
          ]),
        ),
      ),
      http.patch(`${API_BASE}/workflows/WF-1`, async ({ request }) => {
        patchBody = (await request.json()) as Record<string, unknown>;
        return HttpResponse.json(
          envelope({
            workflow_id: "WF-1",
            name: String(patchBody.name),
            description: "",
            owner: "@sarah",
            status: "active",
            default_experiment_id: null,
            created_at: NOW,
            updated_at: NOW,
          }),
        );
      }),
    );

    const user = userEvent.setup();
    renderAt(<Workflows />, "/workflows", "/workflows");
    await screen.findByText("Support Refund");
    await user.click(screen.getByTestId("edit-workflow-WF-1"));
    const nameInput = screen.getByPlaceholderText("Workflow name");
    await user.clear(nameInput);
    await user.type(nameInput, "Support Concierge");
    fireEvent.blur(nameInput);

    await waitFor(() => expect(patchBody).toMatchObject({ name: "Support Concierge" }));
  });

  it("opens delete confirmation and deletes a workflow", async () => {
    let deleteCalls = 0;
    server.use(
      http.get(`${API_BASE}/workflows`, () =>
        HttpResponse.json(
          envelope([
            {
              workflow_id: "WF-DEL",
              name: "Delete Me",
              description: "",
              owner: "@sarah",
              status: "active",
              default_experiment_id: null,
              created_at: NOW,
              updated_at: NOW,
            },
          ]),
        ),
      ),
      http.delete(`${API_BASE}/workflows/WF-DEL`, () => {
        deleteCalls += 1;
        return HttpResponse.json(envelope({ ok: true }));
      }),
    );

    const user = userEvent.setup();
    renderAt(<Workflows />, "/workflows", "/workflows");
    await screen.findByText("Delete Me");
    await user.click(screen.getByTestId("delete-workflow-WF-DEL"));
    expect(await screen.findByText("Delete Workflow")).toBeInTheDocument();
    await user.click(screen.getByTestId("confirm-delete"));
    await waitFor(() => expect(deleteCalls).toBe(1));
  });

  it("cancels delete confirmation without calling the backend", async () => {
    let deleteCalls = 0;
    server.use(
      http.get(`${API_BASE}/workflows`, () =>
        HttpResponse.json(
          envelope([
            {
              workflow_id: "WF-CANCEL",
              name: "Keep Me",
              description: "",
              owner: "@sarah",
              status: "active",
              default_experiment_id: null,
              created_at: NOW,
              updated_at: NOW,
            },
          ]),
        ),
      ),
      http.delete(`${API_BASE}/workflows/WF-CANCEL`, () => {
        deleteCalls += 1;
        return HttpResponse.json(envelope({ ok: true }));
      }),
    );

    const user = userEvent.setup();
    renderAt(<Workflows />, "/workflows", "/workflows");
    await user.click(await screen.findByTestId("delete-workflow-WF-CANCEL"));
    await user.click(screen.getByRole("button", { name: "Cancel" }));

    expect(screen.queryByText("Delete Workflow")).not.toBeInTheDocument();
    expect(deleteCalls).toBe(0);
  });

  it("surfaces create, rename, and delete mutation failures", async () => {
    let createCalls = 0;
    let renameCalls = 0;
    let deleteCalls = 0;
    server.use(
      http.get(`${API_BASE}/workflows`, () =>
        HttpResponse.json(
          envelope([
            {
              workflow_id: "WF-ERR",
              name: "Error Flow",
              description: "",
              owner: "@sarah",
              status: "active",
              default_experiment_id: null,
              created_at: NOW,
              updated_at: NOW,
            },
          ]),
        ),
      ),
      http.post(`${API_BASE}/workflows`, () => {
        createCalls += 1;
        return HttpResponse.json({ detail: "create failed" }, { status: 500 });
      }),
      http.patch(`${API_BASE}/workflows/WF-ERR`, () => {
        renameCalls += 1;
        return HttpResponse.json({ detail: "rename failed" }, { status: 500 });
      }),
      http.delete(`${API_BASE}/workflows/WF-ERR`, () => {
        deleteCalls += 1;
        return HttpResponse.json({ detail: "delete failed" }, { status: 500 });
      }),
    );

    const user = userEvent.setup();
    renderAt(<Workflows />, "/workflows", "/workflows");
    await screen.findByText("Error Flow");

    await user.click(screen.getByTestId("new-workflow"));
    await user.type(screen.getByTestId("new-workflow-name"), "Broken Flow");
    await user.click(screen.getByTestId("template-single_agent"));
    await waitFor(() => expect(createCalls).toBe(1));

    await user.click(screen.getByTestId("edit-workflow-WF-ERR"));
    const nameInput = screen.getByPlaceholderText("Workflow name");
    await user.clear(nameInput);
    await user.type(nameInput, "Still Broken");
    fireEvent.blur(nameInput);
    await waitFor(() => expect(renameCalls).toBe(1));

    await user.click(screen.getByTestId("delete-workflow-WF-ERR"));
    await user.click(screen.getByTestId("confirm-delete"));
    await waitFor(() => expect(deleteCalls).toBe(1));
    expect(screen.queryByText("Delete Workflow")).not.toBeInTheDocument();
  });

  it.each([
    ["view icon", "View Support Refund"],
    ["name button", "Support Refund"],
    ["open affordance", "Open Support Refund"],
  ])("navigates to workflow detail from the card %s", async (_label, accessibleName) => {
    server.use(
      http.get(`${API_BASE}/workflows`, () =>
        HttpResponse.json(
          envelope([
            {
              workflow_id: "WF-NAV",
              name: "Support Refund",
              description: "",
              owner: "@sarah",
              status: "active",
              default_experiment_id: null,
              created_at: NOW,
              updated_at: NOW,
            },
          ]),
        ),
      ),
    );

    const user = userEvent.setup();
    renderAt(<Workflows />, "/workflows", "/workflows");
    await user.click(await screen.findByRole("button", { name: accessibleName }));

    expect(await screen.findByText("DETAIL ROUTE")).toBeInTheDocument();
  });

  it("filters workflow cards by search query", async () => {
    server.use(
      http.get(`${API_BASE}/workflows`, () =>
        HttpResponse.json(
          envelope([
            {
              workflow_id: "WF-1",
              name: "Support Refund",
              description: "",
              owner: "@sarah",
              status: "active",
              default_experiment_id: null,
              created_at: NOW,
              updated_at: NOW,
            },
            {
              workflow_id: "WF-2",
              name: "Travel Concierge",
              description: "",
              owner: "@ops",
              status: "paused",
              default_experiment_id: null,
              created_at: NOW,
              updated_at: NOW,
            },
          ]),
        ),
      ),
    );
    const user = userEvent.setup();
    renderAt(<Workflows />, "/workflows", "/workflows");
    await screen.findByText("Support Refund");
    await user.type(screen.getByPlaceholderText("Search workflows…"), "travel");
    expect(screen.getByText("Travel Concierge")).toBeInTheDocument();
    expect(screen.queryByText("Support Refund")).not.toBeInTheDocument();
  });

  it("shows an error card when workflow listing fails", async () => {
    server.use(
      http.get(`${API_BASE}/workflows`, () =>
        HttpResponse.json({ detail: "boom" }, { status: 500 }),
      ),
    );
    renderAt(<Workflows />, "/workflows", "/workflows");
    expect(await screen.findByText("Error:")).toBeInTheDocument();
    expect(screen.getByText(/boom/)).toBeInTheDocument();
  });
});

describe("ToolRegistry page", () => {
  function toolFixture(overrides: Record<string, unknown> = {}) {
    return {
      tool_id: "TL-1",
      name: "lookup_policy",
      version: "1.0",
      description: "Find the policy that applies to a request.",
      module_path: "m",
      callable_name: "lookup_policy",
      input_schema: null,
      output_schema: null,
      side_effect_level: "read",
      requires_approval: false,
      allow_in_preview: true,
      secret_refs: [],
      test_cases: [],
      last_calibration: null,
      owner: "",
      status: "active",
      deprecated_at: null,
      successor_tool_id: null,
      created_at: NOW,
      updated_at: NOW,
      ...overrides,
    };
  }

  /** Register the list + per-tool detail/source/workspace handlers for one tool. */
  function useToolHandlers(tool: Record<string, unknown>): void {
    server.use(
      http.get(`${API_BASE}/tools`, () => HttpResponse.json(envelope([tool]))),
      http.get(`${API_BASE}/tools/:toolId`, () => HttpResponse.json(envelope(tool))),
      http.get(`${API_BASE}/tools/:toolId/source`, () =>
        HttpResponse.json(
          envelope({
            module_path: String(tool.module_path),
            callable_name: String(tool.callable_name),
            available: true,
            signature: "lookup_policy(query: str) -> dict",
            doc: "",
            source: "def lookup_policy(query):\n    return {}\n",
            error: null,
          }),
        ),
      ),
      http.get(`${API_BASE}/tools/:toolId/workspace`, () =>
        HttpResponse.json(
          envelope({
            version: String(tool.version),
            side_effect_level: String(tool.side_effect_level),
            status: String(tool.status),
            lifecycle: "Tested",
            last_run: null,
            baseline_run_id: null,
            baseline_run: null,
            has_fixtures: false,
            last_calibration_score: null,
          }),
        ),
      ),
    );
  }

  it("lists tools with side-effect badges", async () => {
    useToolHandlers(toolFixture({ description: "" }));
    renderAt(<ToolRegistry />, "/tools", "/tools");
    expect(await screen.findByTestId("tool-row-lookup_policy")).toBeInTheDocument();
    expect(screen.getByText(/🟢 read/)).toBeInTheDocument();
  });

  it("opens the wizard from the Register Tool action", async () => {
    server.use(
      http.get(`${API_BASE}/tools`, () => HttpResponse.json(envelope([]))),
    );
    renderAt(<ToolRegistry />, "/tools", "/tools");
    await userEvent.click(await screen.findByRole("button", { name: "Register Tool" }));
    expect(screen.getByTestId("tool-wizard")).toBeInTheDocument();
    expect(screen.getByTestId("step-identity")).toBeInTheDocument();
  });

  it("opens a tool into the Workspace and back returns to the registry", async () => {
    useToolHandlers(toolFixture());
    renderAt(<ToolRegistry />, "/tools", "/tools");
    await userEvent.click(await screen.findByTestId("tool-open-lookup_policy"));

    // Workspace header + lifecycle pill + the six stage tabs.
    expect(await screen.findByTestId("tool-workspace-header")).toBeInTheDocument();
    expect(screen.getByTestId("tool-workspace-status-badge")).toHaveTextContent("Tested");
    for (const label of ["Spec", "Sandbox", "Fixtures", "Test Runs", "Hardening", "Publish"]) {
      expect(screen.getByRole("button", { name: label })).toBeInTheDocument();
    }

    await userEvent.click(screen.getByRole("button", { name: "Back to tools" }));
    expect(await screen.findByTestId("tool-row-lookup_policy")).toBeInTheDocument();
  });

  it("shows input and output signatures on the Sandbox stage", async () => {
    useToolHandlers(
      toolFixture({
        input_schema: {
          type: "object",
          properties: {
            query: { type: "string", description: "Search text" },
            limit: { type: "integer", description: "Maximum matches" },
          },
          required: ["query"],
        },
        output_schema: {
          type: "object",
          properties: {
            policy_id: { type: "string", description: "Matched policy id" },
            confidence: { type: "number" },
          },
          required: ["policy_id"],
        },
      }),
    );
    renderAt(<ToolRegistry />, "/tools", "/tools");
    await userEvent.click(await screen.findByTestId("tool-open-lookup_policy"));
    await userEvent.click(await screen.findByRole("button", { name: "Sandbox" }));

    expect(await screen.findByTestId("tool-input-signature")).toHaveTextContent("Input Signature");
    expect(screen.getByTestId("tool-input-signature")).toHaveTextContent("query");
    expect(screen.getByTestId("tool-input-signature")).toHaveTextContent("string");
    expect(screen.getByTestId("tool-input-signature")).toHaveTextContent("required");
    expect(screen.getByTestId("tool-output-signature")).toHaveTextContent("Output Signature");
    expect(screen.getByTestId("tool-output-signature")).toHaveTextContent("policy_id");
    expect(screen.getByTestId("tool-output-signature")).toHaveTextContent("confidence");
  });

  it("generates and runs tool unit tests from the Hardening stage", async () => {
    let toolRunBody: Record<string, unknown> | null = null;
    useToolHandlers(
      toolFixture({
        input_schema: {
          type: "object",
          properties: { query: { type: "string" } },
          required: ["query"],
        },
        output_schema: {
          type: "object",
          properties: { policy_id: { type: "string" } },
        },
      }),
    );
    server.use(
      http.get(`${API_BASE}/assistant/config`, () =>
        HttpResponse.json(
          envelope({
            engine: "assistant",
            model: "gpt-test",
            provider: "openai",
            reasoning: "low",
            enabled: true,
            disabled_intents: [],
            disabled_domains: [],
            available_models: [{ id: "gpt-test", name: "GPT Test", provider: "openai" }],
          }),
        ),
      ),
      http.post(`${API_BASE}/assistant/sessions`, async ({ request }) => {
        const body = (await request.json()) as Record<string, unknown>;
        return HttpResponse.json(
          envelope({
            session_id: String(body.title).startsWith("Judge") ? "ASST-JUDGE" : "ASST-GEN",
            title: body.title ?? "Tool Unit Tests",
            owner: "@test",
            status: "active",
            goal: body.goal ?? "",
            metadata_: {},
            active_draft_id: null,
            created_at: NOW,
            updated_at: NOW,
          }),
          { status: 201 },
        );
      }),
      http.post(`${API_BASE}/assistant/sessions/:sessionId/messages`, ({ params }) => {
        const isJudge = params.sessionId === "ASST-JUDGE";
        return HttpResponse.json(
          envelope({
            assistant_message: {
              message_id: isJudge ? "AMSG-JUDGE" : "AMSG-GEN",
              session_id: params.sessionId,
              role: "assistant",
              content: isJudge
                ? '{"verdict":"pass","score":1,"reasoning":"Policy id matches."}'
                : '[{"input":{"query":"refund"},"expectedOutput":{"policy_id":"refund-30"},"expectedBehavior":"Returns refund policy id","tags":["happy-path"]}]',
              metadata_: {},
              sequence_number: 1,
              created_at: NOW,
            },
            questions: [],
            draft_updates: [],
            run: null,
          }),
          { status: 201 },
        );
      }),
      http.post(`${API_BASE}/tools/TL-1/test-run`, async ({ request }) => {
        toolRunBody = (await request.json()) as Record<string, unknown>;
        return HttpResponse.json(
          envelope({
            tool_id: "TL-1",
            output: { policy_id: "refund-30" },
            mocked: false,
            duration_ms: 4,
            error: null,
          }),
        );
      }),
    );

    renderAt(<ToolRegistry />, "/tools", "/tools");
    await userEvent.click(await screen.findByTestId("tool-open-lookup_policy"));
    await userEvent.click(await screen.findByRole("button", { name: "Hardening" }));
    await userEvent.click(await screen.findByTestId("tool-tests-generate"));
    expect(await screen.findByText(/Returns refund policy id/i)).toBeInTheDocument();
    await userEvent.click(screen.getByTestId("tool-tests-run"));

    expect(await screen.findByText(/Policy id matches/i)).toBeInTheDocument();
    expect(toolRunBody).toMatchObject({ input: { query: "refund" } });
  });
});

describe("McpServers playground", () => {
  it("shows input and output signatures for discovered tools", async () => {
    server.use(
      http.get(`${API_BASE}/mcp-servers`, () =>
        HttpResponse.json(
          envelope([
            {
              server_id: "MCP-1",
              name: "Docs",
              description: "Docs MCP server",
              transport: "stdio",
              uri: "",
              command: "npx",
              args: ["docs-server"],
              env: {},
              headers: {},
              auth_type: "none",
              auth_config: {},
              tool_policies: {},
              icon: "book",
              status: "active",
              last_connected_at: null,
              connection_error: null,
              owner: "@qa",
              discovered_tools: [
                {
                  name: "search_docs",
                  description: "Search documentation",
                  input_schema: {
                    type: "object",
                    properties: {
                      query: { type: "string", description: "Search query" },
                    },
                    required: ["query"],
                  },
                  output_schema: {
                    type: "object",
                    properties: {
                      results: { type: "array", description: "Matched documents" },
                    },
                  },
                },
              ],
              created_at: NOW,
              updated_at: NOW,
            },
          ]),
        ),
      ),
    );
    renderAt(<McpServers />, "/mcp-servers", "/mcp-servers");
    await userEvent.click(await screen.findByRole("button", { name: /Playground/i }));
    await userEvent.click(await screen.findByRole("button", { name: /search_docs/i }));

    expect(await screen.findByTestId("mcp-tool-input-signature")).toHaveTextContent("Input Signature");
    expect(screen.getByTestId("mcp-tool-input-signature")).toHaveTextContent("query");
    expect(screen.getByTestId("mcp-tool-input-signature")).toHaveTextContent("required");
    expect(screen.getByTestId("mcp-tool-output-signature")).toHaveTextContent("Output Signature");
    expect(screen.getByTestId("mcp-tool-output-signature")).toHaveTextContent("results");
    expect(screen.getByTestId("mcp-tool-output-signature")).toHaveTextContent("array");
  });

  it("generates and runs MCP tool tests in the playground", async () => {
    let invokeBody: Record<string, unknown> | null = null;
    server.use(
      http.get(`${API_BASE}/mcp-servers`, () =>
        HttpResponse.json(
          envelope([
            {
              server_id: "MCP-1",
              name: "Docs",
              description: "Docs MCP server",
              transport: "stdio",
              uri: "",
              command: "npx",
              args: ["docs-server"],
              env: {},
              headers: {},
              auth_type: "none",
              auth_config: {},
              tool_policies: {},
              icon: "book",
              status: "active",
              last_connected_at: null,
              connection_error: null,
              owner: "@qa",
              discovered_tools: [
                {
                  name: "search_docs",
                  description: "Search documentation",
                  input_schema: {
                    type: "object",
                    properties: { query: { type: "string" } },
                    required: ["query"],
                  },
                  output_schema: {
                    type: "object",
                    properties: { results: { type: "array" } },
                  },
                },
              ],
              created_at: NOW,
              updated_at: NOW,
            },
          ]),
        ),
      ),
      http.get(`${API_BASE}/assistant/config`, () =>
        HttpResponse.json(
          envelope({
            engine: "assistant",
            model: "gpt-test",
            provider: "openai",
            reasoning: "low",
            enabled: true,
            disabled_intents: [],
            disabled_domains: [],
            available_models: [{ id: "gpt-test", name: "GPT Test", provider: "openai" }],
          }),
        ),
      ),
      http.post(`${API_BASE}/assistant/sessions`, async ({ request }) => {
        const body = (await request.json()) as Record<string, unknown>;
        return HttpResponse.json(
          envelope({
            session_id: String(body.title).startsWith("Judge") ? "ASST-MCP-JUDGE" : "ASST-MCP-GEN",
            title: body.title ?? "MCP Tool Tests",
            owner: "@test",
            status: "active",
            goal: body.goal ?? "",
            metadata_: {},
            active_draft_id: null,
            created_at: NOW,
            updated_at: NOW,
          }),
          { status: 201 },
        );
      }),
      http.post(`${API_BASE}/assistant/sessions/:sessionId/messages`, ({ params }) => {
        const isJudge = params.sessionId === "ASST-MCP-JUDGE";
        return HttpResponse.json(
          envelope({
            assistant_message: {
              message_id: isJudge ? "AMSG-MCP-JUDGE" : "AMSG-MCP-GEN",
              session_id: params.sessionId,
              role: "assistant",
              content: isJudge
                ? '{"verdict":"pass","score":1,"reasoning":"Returned matching docs."}'
                : '[{"input":{"query":"calibration"},"expectedOutput":{"results":["calibration guide"]},"expectedBehavior":"Returns matching docs","tags":["search"]}]',
              metadata_: {},
              sequence_number: 1,
              created_at: NOW,
            },
            questions: [],
            draft_updates: [],
            run: null,
          }),
          { status: 201 },
        );
      }),
      http.post(`${API_BASE}/mcp-servers/MCP-1/invoke-tool`, async ({ request }) => {
        invokeBody = (await request.json()) as Record<string, unknown>;
        return HttpResponse.json(
          envelope({
            server_id: "MCP-1",
            tool_name: "search_docs",
            success: true,
            error: null,
            result: { results: ["calibration guide"] },
            duration_ms: 5,
          }),
        );
      }),
    );

    renderAt(<McpServers />, "/mcp-servers", "/mcp-servers");
    await userEvent.click(await screen.findByRole("button", { name: /Playground/i }));
    await userEvent.click(await screen.findByRole("button", { name: /search_docs/i }));
    await userEvent.click(await screen.findByTestId("mcp-tests-generate"));
    expect(await screen.findByText(/Returns matching docs/i)).toBeInTheDocument();
    await userEvent.click(screen.getByTestId("mcp-tests-run"));

    expect(await screen.findByText(/Returned matching docs/i)).toBeInTheDocument();
    expect(invokeBody).toMatchObject({
      tool_name: "search_docs",
      arguments: { query: "calibration" },
    });
  });
});

describe("WorkflowEditor page", () => {
  function editorHandlers(
    overrides: {
      workflow?: Record<string, unknown>;
      version?: Record<string, unknown>;
      capabilities?: Record<string, unknown>;
      runs?: Array<Record<string, unknown>>;
    } = {},
  ) {
    const workflow = makeWorkflow(overrides.workflow);
    const version = makeVersion(overrides.version);
    const capabilities = overrides.capabilities ?? {
      queue_enabled: false,
      supports_async_submit: false,
      supports_cancel: false,
      supports_retry: false,
      supports_resume: false,
      runtime_approvals_enabled: false,
      checkpointing_enabled: false,
      event_backend: "in_process",
    };
    const runs = overrides.runs ?? [];
    return [
      http.get(`${API_BASE}/workflows/WF-1`, () =>
        HttpResponse.json(envelope(workflow)),
      ),
      http.get(`${API_BASE}/workflow-versions/WFV-1`, () =>
        HttpResponse.json(envelope(version)),
      ),
      http.get(`${API_BASE}/capabilities`, () =>
        HttpResponse.json(
          envelope({
            workflow_runs: capabilities,
            sync_workflow_version_run: true,
          }),
        ),
      ),
      http.get(`${API_BASE}/workflows/WF-1/runs`, () =>
        HttpResponse.json(envelope(runs)),
      ),
      http.get(`${API_BASE}/tools`, () => HttpResponse.json(envelope([]))),
      http.get(`${API_BASE}/mcp-servers`, () => HttpResponse.json(envelope([]))),
      // Default save handler so autosave (debounced) + manual Save are always
      // handled; advances the hash like the real optimistic-locking endpoint.
      http.patch(`${API_BASE}/workflow-versions/WFV-1`, async ({ request }) => {
        const body = (await request.json()) as { manifest: Record<string, unknown> };
        return HttpResponse.json(
          envelope(makeVersion({ manifest: body.manifest, manifest_hash: "hash-autosave" })),
        );
      }),
    ];
  }

  function makeEditorRun(overrides: Record<string, unknown> = {}) {
    return {
      workflow_run_id: "WR-EDITOR-1",
      workflow_id: "WF-1",
      project_id: null,
      tenant_id: null,
      workflow_version_id: "WFV-1",
      deployment_alias: null,
      mlflow_run_id: null,
      trace_id: "trace-editor-1",
      session_id: null,
      status: "running",
      source: "editor",
      priority: 0,
      queued_at: NOW,
      started_at: NOW,
      completed_at: null,
      current_node_id: null,
      summary: {
        node_path: ["start"],
        steps: [],
      },
      ...overrides,
    };
  }

  function editorRunMonitorHandlers(
    run: Record<string, unknown>,
    {
      manifest,
      manifestMode = "saved_version",
      versionId = "WFV-1",
      versionNumber = 1,
      versionManifest,
      checkpoints = [],
      events = [],
      approvals = [],
      files = [],
      lineage = null,
    }: {
      manifest: Record<string, unknown>;
      manifestMode?: "saved_version" | "snapshot";
      versionId?: string;
      versionNumber?: number;
      versionManifest?: Record<string, unknown>;
      checkpoints?: Array<Record<string, unknown>>;
      events?: Array<Record<string, unknown>>;
      approvals?: Array<Record<string, unknown>>;
      files?: Array<Record<string, unknown>>;
      lineage?: Record<string, unknown> | null;
    },
  ) {
    const runId = String(run.workflow_run_id);
    const workflowVersionId = String(run.workflow_version_id ?? versionId);
    return [
      http.get(`${API_BASE}/workflow-runs/${runId}`, () =>
        HttpResponse.json(envelope(run)),
      ),
      http.get(`${API_BASE}/workflow-runs/${runId}/events`, () =>
        HttpResponse.json(envelope(events)),
      ),
      http.get(`${API_BASE}/workflow-runs/${runId}/checkpoints`, () =>
        HttpResponse.json(envelope(checkpoints)),
      ),
      http.get(`${API_BASE}/workflow-runs/${runId}/trace`, () =>
        HttpResponse.json(envelope({ trace_id: null, spans: [] })),
      ),
      http.get(`${API_BASE}/workflow-runs/${runId}/approvals`, () =>
        HttpResponse.json(envelope(approvals)),
      ),
      http.get(`${API_BASE}/workflow-runs/${runId}/files`, () =>
        HttpResponse.json(
          envelope({
            items: files,
            next_cursor: null,
          }),
        ),
      ),
      http.get(`${API_BASE}/workflow-runs/${runId}/lineage`, () =>
        HttpResponse.json(
          envelope(
            lineage ?? {
              workflow_run_id: runId,
              root_run_id: runId,
              total_attempts: 1,
              parent_count: 0,
              child_count: 0,
              missing_parent_id: null,
              truncated: false,
              runs: [run],
            },
          ),
        ),
      ),
      http.get(`${API_BASE}/workflow-runs/${runId}/manifest`, () =>
        HttpResponse.json(
          envelope({
            workflow_run_id: runId,
            workflow_id: "WF-1",
            workflow_version_id: workflowVersionId,
            manifest_mode: manifestMode,
            manifest_hash: `hash-${runId}`,
            manifest,
          }),
        ),
      ),
      http.get(`${API_BASE}/workflow-versions/${workflowVersionId}`, () =>
        HttpResponse.json(
          envelope(
            makeVersion({
              version_id: workflowVersionId,
              version_number: versionNumber,
              status: "published",
              manifest: versionManifest ?? manifest,
            }),
          ),
        ),
      ),
    ];
  }

  it("renders the three panels and node outline", async () => {
    server.use(...editorHandlers());
    renderAt(<WorkflowEditor />, "/workflows/WF-1/editor/WFV-1", "/workflows/:workflowId/editor/:versionId");
    expect(await screen.findByTestId("workflow-editor")).toBeInTheDocument();
    expect(screen.getByTestId("wf-node-palette")).toBeInTheDocument();
    expect(screen.getByTestId("outline-support_agent")).toBeInTheDocument();
    expect(screen.getByTestId("wf-problems")).toBeInTheDocument();
  });

  it("selecting a node from the outline shows its inspector", async () => {
    server.use(...editorHandlers());
    renderAt(<WorkflowEditor />, "/workflows/WF-1/editor/WFV-1", "/workflows/:workflowId/editor/:versionId");
    await userEvent.click(await screen.findByTestId("outline-support_agent"));
    const inspector = await screen.findByTestId("wf-inspector");
    expect(inspector).toHaveAttribute("data-node-type", "agent");
  });

  it("adds a node from the palette to the outline", async () => {
    server.use(...editorHandlers());
    renderAt(<WorkflowEditor />, "/workflows/WF-1/editor/WFV-1", "/workflows/:workflowId/editor/:versionId");
    await screen.findByTestId("workflow-editor");
    await userEvent.click(screen.getByTestId("palette-guardrail"));
    expect(await screen.findByTestId("outline-guardrail")).toBeInTheDocument();
    expect(screen.getByTestId("wf-inspector")).toHaveAttribute("data-node-type", "guardrail");
    await userEvent.click(screen.getByTestId("palette-folder_input"));
    expect(await screen.findByTestId("outline-folder_input")).toBeInTheDocument();
    expect(screen.getByTestId("wf-inspector")).toHaveAttribute("data-node-type", "folder_input");
  });

  it("undoes and redoes a palette add from the toolbar", async () => {
    server.use(...editorHandlers());
    renderAt(<WorkflowEditor />, "/workflows/WF-1/editor/WFV-1", "/workflows/:workflowId/editor/:versionId");
    await screen.findByTestId("workflow-editor");
    // Undo/redo are disabled until there's history.
    expect(screen.getByTestId("editor-undo")).toBeDisabled();
    expect(screen.getByTestId("editor-redo")).toBeDisabled();

    await userEvent.click(screen.getByTestId("palette-guardrail"));
    expect(await screen.findByTestId("outline-guardrail")).toBeInTheDocument();

    await userEvent.click(screen.getByTestId("editor-undo"));
    await waitFor(() =>
      expect(screen.queryByTestId("outline-guardrail")).not.toBeInTheDocument(),
    );

    await userEvent.click(screen.getByTestId("editor-redo"));
    expect(await screen.findByTestId("outline-guardrail")).toBeInTheDocument();
  });

  it("removes the selected node with the Delete key", async () => {
    server.use(...editorHandlers());
    renderAt(<WorkflowEditor />, "/workflows/WF-1/editor/WFV-1", "/workflows/:workflowId/editor/:versionId");
    await screen.findByTestId("workflow-editor");
    await userEvent.click(screen.getByTestId("palette-guardrail"));
    await screen.findByTestId("outline-guardrail");

    fireEvent.keyDown(document, { key: "Delete" });
    await waitFor(() =>
      expect(screen.queryByTestId("outline-guardrail")).not.toBeInTheDocument(),
    );
  });

  it("duplicates the selected node with Cmd+D", async () => {
    server.use(...editorHandlers());
    renderAt(<WorkflowEditor />, "/workflows/WF-1/editor/WFV-1", "/workflows/:workflowId/editor/:versionId");
    await userEvent.click(await screen.findByTestId("outline-support_agent"));

    fireEvent.keyDown(document, { key: "d", metaKey: true });
    // The duplicate is a fresh agent-type node; the original stays.
    expect(await screen.findByTestId("outline-agent_1")).toBeInTheDocument();
    expect(screen.getByTestId("outline-support_agent")).toBeInTheDocument();
  });

  it("select-all enters multi-select and bulk-deletes everything but start", async () => {
    server.use(...editorHandlers());
    renderAt(<WorkflowEditor />, "/workflows/WF-1/editor/WFV-1", "/workflows/:workflowId/editor/:versionId");
    await screen.findByTestId("workflow-editor");
    expect(screen.getByTestId("outline-support_agent")).toBeInTheDocument();

    fireEvent.keyDown(document, { key: "a", metaKey: true });
    // The right rail yields to a bulk-actions panel for the 3-node selection.
    expect(await screen.findByTestId("wf-bulk-panel")).toHaveTextContent("3 nodes selected");

    await userEvent.click(screen.getByTestId("wf-bulk-delete"));
    // support_agent + final removed; the unique start node is preserved.
    await waitFor(() =>
      expect(screen.queryByTestId("outline-support_agent")).not.toBeInTheDocument(),
    );
    expect(screen.queryByTestId("outline-final")).not.toBeInTheDocument();
    expect(screen.getByTestId("outline-start")).toBeInTheDocument();
  });

  it("bulk-duplicates the selection with Cmd+D (skipping start/output)", async () => {
    server.use(...editorHandlers());
    renderAt(<WorkflowEditor />, "/workflows/WF-1/editor/WFV-1", "/workflows/:workflowId/editor/:versionId");
    await screen.findByTestId("workflow-editor");
    fireEvent.keyDown(document, { key: "a", metaKey: true });
    await screen.findByTestId("wf-bulk-panel");

    fireEvent.keyDown(document, { key: "d", metaKey: true });
    // Only the agent node duplicates (start + output are skipped) → agent_1.
    expect(await screen.findByTestId("outline-agent_1")).toBeInTheDocument();
    expect(screen.queryByTestId("outline-start_1")).not.toBeInTheDocument();
  });

  it("copies and pastes a node with Cmd+C / Cmd+V", async () => {
    server.use(...editorHandlers());
    renderAt(<WorkflowEditor />, "/workflows/WF-1/editor/WFV-1", "/workflows/:workflowId/editor/:versionId");
    await userEvent.click(await screen.findByTestId("outline-support_agent"));

    fireEvent.keyDown(document, { key: "c", metaKey: true });
    fireEvent.keyDown(document, { key: "v", metaKey: true });
    expect(await screen.findByTestId("outline-agent_1")).toBeInTheDocument();
    expect(screen.getByTestId("outline-support_agent")).toBeInTheDocument();
  });

  it("Validate calls the API and renders problems", async () => {
    server.use(
      ...editorHandlers(),
      http.post(`${API_BASE}/workflow-versions/WFV-1/validate`, () =>
        HttpResponse.json(
          envelope({
            valid: false,
            errors: [
              { code: "missing_tool", path: "nodes.support_agent.tools", message: "Tool X not registered.", severity: "error" },
            ],
            warnings: [],
          }),
        ),
      ),
    );
    renderAt(<WorkflowEditor />, "/workflows/WF-1/editor/WFV-1", "/workflows/:workflowId/editor/:versionId");
    await userEvent.click(await screen.findByTestId("editor-validate"));
    expect(await screen.findByTestId("problem-missing_tool")).toBeInTheDocument();
  });

  it("saves agent tool selections with manifest registry bindings", async () => {
    let savedManifest: unknown = null;
    server.use(
      http.get(`${API_BASE}/tools`, () =>
        HttpResponse.json(envelope([makeTool({ tool_id: "TL-GREP", name: "grep_files", callable_name: "grep_files" })])),
      ),
      // Listed before editorHandlers() so this capturing handler wins the
      // first-match over editorHandlers' default save handler.
      http.patch(`${API_BASE}/workflow-versions/WFV-1`, async ({ request }) => {
        const body = (await request.json()) as { manifest: Record<string, unknown> };
        savedManifest = body.manifest;
        return HttpResponse.json(envelope(makeVersion({ manifest: body.manifest, manifest_hash: "hash2" })));
      }),
      ...editorHandlers(),
    );
    renderAt(<WorkflowEditor />, "/workflows/WF-1/editor/WFV-1", "/workflows/:workflowId/editor/:versionId");
    await userEvent.click(await screen.findByTestId("outline-support_agent"));
    await userEvent.click(await screen.findByTestId("tools-add"));
    await userEvent.click(await screen.findByTestId("tool-grep_files"));
    await userEvent.click(screen.getByTestId("editor-save"));

    await waitFor(() => expect(savedManifest).not.toBeNull());
    const manifest = savedManifest as {
      nodes: { support_agent: { tools: string[] } };
      tools: { grep_files: { registry_ref: string; version_constraint: string } };
    };
    expect(manifest.nodes.support_agent.tools).toContain("grep_files");
    expect(manifest.tools.grep_files).toMatchObject({
      registry_ref: "tool.grep_files.v1",
      version_constraint: ">=1.0,<2.0",
    });
  });

  it("runs preview from the editor toolbar and shows execution output", async () => {
    server.use(
      ...editorHandlers(),
      http.post(`${API_BASE}/workflow-versions/WFV-1/preview-run`, async ({ request }) => {
        const body = (await request.json()) as { input: string };
        return HttpResponse.json(
          envelope({
            status: "completed",
            output: `Preview output for: ${body.input}`,
            steps: [
              {
                node_id: "start",
                node_type: "start",
                status: "ok",
                output: body.input,
                tool_calls: [],
                handoff_target: null,
                detail: "",
                duration_ms: 0,
              },
              {
                node_id: "support_agent",
                node_type: "agent",
                status: "ok",
                output: "handled",
                tool_calls: [],
                handoff_target: null,
                detail: "",
                duration_ms: 0,
              },
            ],
            error: null,
          }),
        );
      }),
    );
    renderAt(<WorkflowEditor />, "/workflows/WF-1/editor/WFV-1", "/workflows/:workflowId/editor/:versionId");
    await userEvent.click(await screen.findByTestId("editor-preview"));
    await userEvent.clear(screen.getByTestId("preview-input"));
    await userEvent.type(screen.getByTestId("preview-input"), "Need a refund");
    await userEvent.click(screen.getByTestId("preview-run"));
    expect(await screen.findByTestId("preview-result")).toHaveTextContent("completed");
    expect(screen.getByTestId("preview-result")).toHaveTextContent("support_agent");
    expect(screen.getByTestId("preview-result")).toHaveTextContent("Preview output for: Need a refund");
  });

  it("opens quick-add from a node and inserts a connected guardrail", async () => {
    server.use(...editorHandlers());
    renderAt(<WorkflowEditor />, "/workflows/WF-1/editor/WFV-1", "/workflows/:workflowId/editor/:versionId");
    await screen.findByTestId("workflow-editor");
    fireEvent.click(await screen.findByTestId("quick-add-support_agent"));
    expect(await screen.findByText("Add & connect")).toBeInTheDocument();
    const guardrailButtons = screen.getAllByRole("button", { name: /Guardrail/i });
    fireEvent.click(guardrailButtons[guardrailButtons.length - 1]!);
    expect(await screen.findByTestId("outline-guardrail")).toBeInTheDocument();
  });

  it("publishes from the drawer after validating", async () => {
    let publishCalls = 0;
    server.use(
      ...editorHandlers(),
      http.post(`${API_BASE}/workflow-versions/WFV-1/validate`, () =>
        HttpResponse.json(envelope({ valid: true, errors: [], warnings: [] })),
      ),
      http.post(`${API_BASE}/workflow-versions/WFV-1/publish`, () => {
        publishCalls += 1;
        return HttpResponse.json(
          envelope(makeVersion({ status: "published", published_by: "@release", published_at: NOW })),
        );
      }),
    );
    renderAt(<WorkflowEditor />, "/workflows/WF-1/editor/WFV-1", "/workflows/:workflowId/editor/:versionId");
    await userEvent.click(await screen.findByTestId("editor-publish"));
    expect(await screen.findByTestId("publish-drawer")).toBeInTheDocument();
    await userEvent.click(screen.getByTestId("publish-run-validate"));
    await userEvent.click(await screen.findByTestId("publish-next-1"));
    await userEvent.click(screen.getByTestId("publish-next-2"));
    await userEvent.click(screen.getByTestId("publish-confirm"));
    await waitFor(() => expect(publishCalls).toBe(1));
    expect(screen.getByTestId("editor-message")).toHaveTextContent("Published.");
  });

  it("turns an empty editor run history into draft-first guidance", async () => {
    server.use(
      ...editorHandlers({
        capabilities: {
          queue_enabled: true,
          supports_async_submit: true,
          supports_cancel: false,
          supports_retry: false,
          supports_resume: false,
          runtime_approvals_enabled: false,
          checkpointing_enabled: false,
          event_backend: "in_process",
        },
        runs: [],
      }),
    );

    renderAt(<WorkflowEditor />, "/workflows/WF-1/editor/WFV-1", "/workflows/:workflowId/editor/:versionId");
    await userEvent.click(await screen.findByTestId("editor-run-monitor"));

    expect(await screen.findByTestId("run-history-list")).toHaveTextContent(
      "No recent workflow runs exist for this draft yet. Use Run to create the first editor execution",
    );
    expect(screen.getByTestId("run-history-list")).toHaveTextContent(
      "publish the version first if you need deployment-triggered history",
    );
  });

  it("turns an empty editor run history into queue-disabled guidance", async () => {
    server.use(
      ...editorHandlers({
        version: { status: "published" },
        capabilities: {
          queue_enabled: false,
          supports_async_submit: false,
          supports_cancel: false,
          supports_retry: false,
          supports_resume: false,
          runtime_approvals_enabled: false,
          checkpointing_enabled: false,
          event_backend: "in_process",
        },
        runs: [],
      }),
    );

    renderAt(<WorkflowEditor />, "/workflows/WF-1/editor/WFV-1", "/workflows/:workflowId/editor/:versionId");
    await userEvent.click(await screen.findByTestId("editor-run-monitor"));

    expect(await screen.findByTestId("run-history-list")).toHaveTextContent(
      "This deployment has workflow execution disabled. Enable the run queue",
    );
    expect(screen.getByTestId("run-history-list")).toHaveTextContent(
      "create the first editor execution history for this workflow",
    );
  });

  it("turns an empty editor run history into paused-workflow guidance", async () => {
    server.use(
      ...editorHandlers({
        workflow: { status: "paused" },
        version: { status: "published" },
        capabilities: {
          queue_enabled: true,
          supports_async_submit: true,
          supports_cancel: false,
          supports_retry: false,
          supports_resume: false,
          runtime_approvals_enabled: false,
          checkpointing_enabled: false,
          event_backend: "in_process",
        },
        runs: [],
      }),
    );

    renderAt(<WorkflowEditor />, "/workflows/WF-1/editor/WFV-1", "/workflows/:workflowId/editor/:versionId");
    await userEvent.click(await screen.findByTestId("editor-run-monitor"));

    expect(await screen.findByTestId("run-history-list")).toHaveTextContent(
      "This workflow is paused, so new editor runs cannot start yet.",
    );
    expect(screen.getByTestId("run-history-list")).toHaveTextContent(
      "Resume it first, then use Run to create the first execution history for this version.",
    );
  });

  it("turns snapshot-backed editor monitor notes into paused-run guidance", async () => {
    const run = {
      ...makeEditorRun({
        workflow_run_id: "WR-SNAPSHOT-WAIT",
        workflow_version_id: "WFV-1",
        status: "waiting_event",
        current_node_id: "wait_gate",
        summary: {
          node_path: ["start", "wait_gate"],
          steps: [],
          manifest_mode: "snapshot",
        },
      }),
    };
    const snapshotManifest = {
      ...makeVersion().manifest,
      nodes: {
        ...makeVersion().manifest.nodes,
        wait_gate: {
          id: "wait_gate",
          type: "wait_for_event",
          event_name: "ticket.approved",
        },
      },
      edges: [
        { id: "e1", from: "start", to: "wait_gate", map: { user_message: "ticket_id" } },
      ],
    };

    server.use(
      ...editorHandlers({
        capabilities: {
          queue_enabled: true,
          supports_async_submit: true,
          supports_cancel: false,
          supports_retry: false,
          supports_resume: false,
          runtime_approvals_enabled: false,
          checkpointing_enabled: true,
          event_backend: "in_process",
        },
        runs: [run],
      }),
      ...editorRunMonitorHandlers(run, {
        manifest: snapshotManifest,
        manifestMode: "snapshot",
        checkpoints: [
          {
            checkpoint_id: "CP-SNAPSHOT-WAIT",
            workflow_run_id: "WR-SNAPSHOT-WAIT",
            project_id: null,
            sequence: 1,
            node_id: "wait_gate",
            state_blob: {
              kind: "wait_for_event",
              node_id: "wait_gate",
              expected_event_name: "ticket.approved",
            },
            created_at: NOW,
          },
        ],
      }),
    );

    renderAt(<WorkflowEditor />, "/workflows/WF-1/editor/WFV-1", "/workflows/:workflowId/editor/:versionId");
    await userEvent.click(await screen.findByTestId("editor-run-monitor"));
    await userEvent.click(await screen.findByTestId("run-history-select-WR-SNAPSHOT-WAIT"));

    expect(await screen.findByTestId("run-monitor-manifest-note")).toHaveTextContent(
      "unsaved draft snapshot captured when you queued it",
    );
    expect(screen.getByTestId("run-monitor-manifest-note")).toHaveTextContent(
      "recovery and checkpoint panels in this run monitor for authoritative resume state while the snapshot-backed run is paused",
    );
  });

  it("turns historical-version editor monitor notes into completed-run comparison guidance", async () => {
    const legacyManifest = {
      schema_version: 1,
      workflow_id: "WF-1",
      name: "Support v1",
      nodes: {
        start: { id: "start", type: "start", outputs: { user_message: { type: "string" } } },
        legacy_agent: {
          id: "legacy_agent",
          type: "agent",
          name: "legacy-agent",
          model: "inherit",
          instructions: { type: "inline", text: "legacy" },
          tools: [],
          inputs: { input: { type: "string" } },
          outputs: { final_output: { type: "string" } },
        },
      },
      edges: [
        { id: "legacy-e1", from: "start", to: "legacy_agent", map: { user_message: "input" } },
      ],
    };
    const run = {
      ...makeEditorRun({
        workflow_run_id: "WR-HIST-DONE",
        workflow_version_id: "WFV-OLD",
        status: "completed",
        completed_at: NOW,
        summary: {
          node_path: ["start", "legacy_agent"],
          steps: [
            {
              node_id: "start",
              node_type: "start",
              status: "ok",
              output: "legacy customer message",
              tool_calls: [],
              handoff_target: null,
              detail: "captured trigger input",
              duration_ms: 5,
              output_by_port: { user_message: "legacy customer message" },
            },
          ],
        },
      }),
    };

    server.use(
      ...editorHandlers({
        version: {
          version_id: "WFV-1",
          version_number: 2,
          status: "draft",
        },
        capabilities: {
          queue_enabled: true,
          supports_async_submit: true,
          supports_cancel: false,
          supports_retry: false,
          supports_resume: false,
          runtime_approvals_enabled: false,
          checkpointing_enabled: true,
          event_backend: "in_process",
        },
        runs: [run],
      }),
      ...editorRunMonitorHandlers(run, {
        manifest: legacyManifest,
        manifestMode: "saved_version",
        versionId: "WFV-OLD",
        versionNumber: 1,
      }),
    );

    renderAt(<WorkflowEditor />, "/workflows/WF-1/editor/WFV-1", "/workflows/:workflowId/editor/:versionId");
    await userEvent.click(await screen.findByTestId("editor-run-monitor"));
    await userEvent.click(await screen.findByTestId("run-history-select-WR-HIST-DONE"));

    expect(await screen.findByTestId("run-monitor-manifest-note")).toHaveTextContent(
      "This monitor is replaying saved workflow version v1",
    );
    expect(screen.getByTestId("run-monitor-manifest-note")).toHaveTextContent(
      "Compare the debugger, final outputs, and generated artifacts in this run monitor before making follow-up edits to the current draft.",
    );
  });
});

describe("WorkflowDetail calibration", () => {
  function makeRun(overrides: Record<string, unknown> = {}) {
    return {
      workflow_run_id: "WR-1",
      workflow_id: "WF-1",
      project_id: null,
      tenant_id: null,
      workflow_version_id: "WFV-1",
      deployment_alias: null,
      mlflow_run_id: null,
      trace_id: "trace-1",
      session_id: null,
      status: "running",
      source: "manual",
      priority: 0,
      queued_at: NOW,
      started_at: NOW,
      completed_at: null,
      summary: {
        node_path: ["start", "support_agent"],
        steps: [
          {
            node_id: "start",
            node_type: "start",
            status: "ok",
            output: "input",
            tool_calls: [],
            handoff_target: null,
            detail: "",
            duration_ms: 0,
          },
        ],
      },
      ...overrides,
    };
  }

  function testRunArtifactPersistenceStatus(
    run: Record<string, unknown>,
  ): "failed" | "persisted" | null {
    const summary =
      run.summary && typeof run.summary === "object" && !Array.isArray(run.summary)
        ? (run.summary as Record<string, unknown>)
        : null;
    const artifactPersistence =
      summary?.artifact_persistence
      && typeof summary.artifact_persistence === "object"
      && !Array.isArray(summary.artifact_persistence)
        ? (summary.artifact_persistence as Record<string, unknown>)
        : null;
    const status = artifactPersistence?.status;
    return status === "failed" || status === "persisted" ? status : null;
  }

  function testRunMatchesArtifactFilter(
    run: Record<string, unknown>,
    artifactFilter: "failed" | "persisted" | null,
  ): boolean {
    if (!artifactFilter) return true;
    return testRunArtifactPersistenceStatus(run) === artifactFilter;
  }

  function testRunMatchesSearch(run: Record<string, unknown>, query: string | null): boolean {
    if (!query) return true;
    const normalized = query.trim().toLowerCase();
    if (!normalized) return true;
    const summary =
      run.summary && typeof run.summary === "object" && !Array.isArray(run.summary)
        ? (run.summary as Record<string, unknown>)
        : null;
    const artifactPersistence =
      summary?.artifact_persistence
      && typeof summary.artifact_persistence === "object"
      && !Array.isArray(summary.artifact_persistence)
        ? (summary.artifact_persistence as Record<string, unknown>)
        : null;
    const artifactNames = Array.isArray(artifactPersistence?.artifact_names)
      ? artifactPersistence.artifact_names.filter(
          (value): value is string => typeof value === "string" && value.trim().length > 0,
        )
      : [];
    const searchable = [
      run.workflow_run_id,
      run.trace_id,
      run.workflow_version_id,
      run.deployment_alias,
      run.status,
      run.error_summary,
      run.current_node_id,
      typeof summary?.error === "string" ? summary.error : null,
      typeof artifactPersistence?.bucket === "string" ? artifactPersistence.bucket : null,
      testRunArtifactPersistenceStatus(run) === "failed"
        ? "artifact upload failed"
        : testRunArtifactPersistenceStatus(run) === "persisted"
          ? "artifacts stored"
          : null,
      typeof artifactPersistence?.error === "string" ? artifactPersistence.error : null,
      ...artifactNames,
    ].filter((value): value is string => typeof value === "string" && value.trim().length > 0);
    return searchable.some((value) => value.toLowerCase().includes(normalized));
  }

  function buildWorkflowRunHistoryStats(
    runs: Array<Record<string, unknown>>,
    options: {
      search?: string | null;
      artifactFilter?: "failed" | "persisted" | null;
    } = {},
  ): Record<string, unknown> {
    const search = options.search ?? null;
    const artifactFilter = options.artifactFilter ?? null;
    let waitingEventRuns = 0;
    let failedArtifactRuns = 0;
    let persistedArtifactRuns = 0;
    let matchingRuns = 0;
    for (const run of runs) {
      if (run.status === "waiting_event") {
        waitingEventRuns += 1;
      }
      const artifactStatus = testRunArtifactPersistenceStatus(run);
      if (artifactStatus === "failed") {
        failedArtifactRuns += 1;
      } else if (artifactStatus === "persisted") {
        persistedArtifactRuns += 1;
      }
      if (
        testRunMatchesArtifactFilter(run, artifactFilter)
        && testRunMatchesSearch(run, search)
      ) {
        matchingRuns += 1;
      }
    }
    return {
      workflow_id: "WF-1",
      total_runs: runs.length,
      matching_runs: matchingRuns,
      waiting_event_runs: waitingEventRuns,
      artifact_persistence: {
        failed: failedArtifactRuns,
        persisted: persistedArtifactRuns,
      },
    };
  }

  function buildWorkflowRunLineage(
    runId: string,
    runs: Array<Record<string, unknown>>,
  ): Record<string, unknown> | null {
    const map = new Map<string, Record<string, unknown>>();
    for (const item of runs) {
      const itemRunId = String(item.workflow_run_id ?? "");
      if (itemRunId) map.set(itemRunId, item);
    }
    const run = map.get(runId);
    if (!run) return null;

    const childrenByParent = new Map<string, Array<Record<string, unknown>>>();
    for (const item of map.values()) {
      const parentId =
        typeof item.parent_run_id === "string" ? item.parent_run_id : null;
      if (!parentId) continue;
      const next = childrenByParent.get(parentId) ?? [];
      next.push(item);
      childrenByParent.set(parentId, next);
    }

    let missingParentId: string | null = null;
    let parentCount = 0;
    let cursor: Record<string, unknown> = run;
    const seen = new Set<string>([runId]);
    while (typeof cursor.parent_run_id === "string" && cursor.parent_run_id) {
      const parentId = cursor.parent_run_id;
      if (seen.has(parentId)) break;
      seen.add(parentId);
      const parent = map.get(parentId);
      if (!parent) {
        missingParentId = parentId;
        break;
      }
      parentCount += 1;
      cursor = parent;
    }

    const rootRunId = String(cursor.workflow_run_id ?? runId);
    const connected = new Map<string, Record<string, unknown>>();
    const queue = [rootRunId];
    while (queue.length > 0) {
      const currentId = queue.shift()!;
      if (connected.has(currentId)) continue;
      const current = map.get(currentId);
      if (!current) continue;
      connected.set(currentId, current);
      for (const child of childrenByParent.get(currentId) ?? []) {
        const childId = String(child.workflow_run_id ?? "");
        if (childId) queue.push(childId);
      }
    }

    const lineageRuns = [...connected.values()].sort((left, right) => {
      const leftAttempt = Math.max(1, Number(left.attempt_number ?? 1));
      const rightAttempt = Math.max(1, Number(right.attempt_number ?? 1));
      if (leftAttempt !== rightAttempt) return leftAttempt - rightAttempt;
      const leftTime = String(
        left.queued_at ?? left.started_at ?? left.completed_at ?? "",
      );
      const rightTime = String(
        right.queued_at ?? right.started_at ?? right.completed_at ?? "",
      );
      if (leftTime !== rightTime) return leftTime.localeCompare(rightTime);
      return String(left.workflow_run_id ?? "").localeCompare(
        String(right.workflow_run_id ?? ""),
      );
    });

    return {
      workflow_run_id: runId,
      root_run_id: rootRunId,
      total_attempts: lineageRuns.length,
      parent_count: parentCount,
      child_count: childrenByParent.get(runId)?.length ?? 0,
      missing_parent_id: missingParentId,
      truncated: false,
      runs: lineageRuns,
    };
  }

  function detailHandlers(
    overrides: {
      datasetAvailable?: boolean;
      judgeAvailable?: boolean;
      versions?: Array<Record<string, unknown>>;
      deployments?: Array<Record<string, unknown>>;
      runs?: Array<Record<string, unknown>>;
      promotions?: Array<Record<string, unknown>>;
      patches?: Array<Record<string, unknown>>;
      capabilities?: Record<string, unknown>;
      runApprovalsById?: Record<string, Array<Record<string, unknown>>>;
      runEventsById?: Record<string, Array<Record<string, unknown>>>;
      runCheckpointsById?: Record<string, Array<Record<string, unknown>>>;
      runManifestsById?: Record<string, Record<string, unknown> | null>;
      runFilesById?: Record<string, Array<Record<string, unknown>>>;
      sessionMemoryBySessionId?: Record<string, Array<Record<string, unknown>>>;
      workflow?: Record<string, unknown>;
      agents?: Array<Record<string, unknown>>;
      calibrationJobs?: Array<Record<string, unknown>>;
    } = {},
  ) {
    const datasetAvailable = overrides.datasetAvailable ?? true;
    const calibrationJobs = overrides.calibrationJobs ?? [];
    const judgeAvailable = overrides.judgeAvailable ?? false;
    const versions = overrides.versions ?? [makeVersion({ status: "published" })];
    const deployments = overrides.deployments ?? [];
    const runs = overrides.runs ?? [];
    const promotions = overrides.promotions ?? [];
    const patches = overrides.patches ?? [];
    const workflow = {
      workflow_id: "WF-1",
      name: "Support Workflow",
      description: "",
      owner: "@test",
      status: "active",
      default_experiment_id: null,
      created_at: NOW,
      updated_at: NOW,
      ...overrides.workflow,
    };
    const capabilities = overrides.capabilities ?? {
      queue_enabled: false,
      supports_async_submit: false,
      supports_cancel: false,
      supports_retry: false,
      supports_resume: false,
      runtime_approvals_enabled: false,
      checkpointing_enabled: false,
      event_backend: "in_process",
    };
    const runApprovalsById = overrides.runApprovalsById ?? {};
    const runEventsById = overrides.runEventsById ?? {};
    const runCheckpointsById = overrides.runCheckpointsById ?? {};
    const runManifestsById = overrides.runManifestsById ?? {};
    const runFilesById = overrides.runFilesById ?? {};
    const sessionMemoryBySessionId = Object.fromEntries(
      Object.entries(overrides.sessionMemoryBySessionId ?? {}).map(([sessionId, entries]) => [
        sessionId,
        [...entries],
      ]),
    );
    const agents = overrides.agents ?? [
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
        created_at: NOW,
        updated_at: NOW,
      },
    ];
    return [
      http.get(`${API_BASE}/capabilities`, () =>
        HttpResponse.json(
          envelope({
            workflow_runs: capabilities,
            sync_workflow_version_run: true,
          }),
        ),
      ),
      http.get(`${API_BASE}/jobs`, () => HttpResponse.json(envelope(calibrationJobs))),
      http.post(`${API_BASE}/jobs/:jobId/apply`, ({ params }) =>
        HttpResponse.json(
          envelope({ job_id: String(params.jobId), status: "applied", promotion: {} }),
        ),
      ),
      http.get(`${API_BASE}/workflows/WF-1`, () =>
        HttpResponse.json(envelope(workflow)),
      ),
      http.get(`${API_BASE}/workflows/WF-1/versions`, () =>
        HttpResponse.json(envelope(versions)),
      ),
      http.get(`${API_BASE}/workflow-versions/:versionId`, ({ params }) => {
        const versionId = String(params.versionId);
        const version =
          versions.find((item) => String(item.version_id) === versionId) ?? null;
        if (!version) {
          return HttpResponse.json(
            { detail: `workflow version ${versionId} not found` },
            { status: 404 },
          );
        }
        return HttpResponse.json(envelope(version));
      }),
      http.get(`${API_BASE}/workflows/WF-1/deployments`, () => HttpResponse.json(envelope(deployments))),
      http.get(`${API_BASE}/workflows/WF-1/promotions`, () => HttpResponse.json(envelope(promotions))),
      http.get(`${API_BASE}/workflows/WF-1/patches`, () => HttpResponse.json(envelope(patches))),
      http.get(`${API_BASE}/workflows/WF-1/runs`, () => HttpResponse.json(envelope(runs))),
      http.get(`${API_BASE}/workflows/WF-1/runs/stats`, ({ request }) => {
        const url = new URL(request.url);
        const search = url.searchParams.get("search");
        const artifactFilter = url.searchParams.get("artifact_persistence");
        return HttpResponse.json(
          envelope(
            buildWorkflowRunHistoryStats(runs, {
              search,
              artifactFilter:
                artifactFilter === "failed" || artifactFilter === "persisted"
                  ? artifactFilter
                  : null,
            }),
          ),
        );
      }),
      http.get(`${API_BASE}/workflow-runs/:runId`, ({ params }) => {
        const runId = String(params.runId);
        const run =
          runs.find((item) => String(item.workflow_run_id) === runId) ?? null;
        if (!run) {
          return HttpResponse.json(
            { detail: `workflow run ${runId} not found` },
            { status: 404 },
          );
        }
        return HttpResponse.json(envelope(run));
      }),
      http.get(`${API_BASE}/workflow-runs/:runId/lineage`, ({ params }) => {
        const runId = String(params.runId);
        const lineage = buildWorkflowRunLineage(runId, runs);
        if (!lineage) {
          return HttpResponse.json(
            { detail: `workflow run ${runId} not found` },
            { status: 404 },
          );
        }
        return HttpResponse.json(envelope(lineage));
      }),
      http.get(`${API_BASE}/workflow-runs/:runId/manifest`, ({ params }) => {
        const runId = String(params.runId);
        if (Object.prototype.hasOwnProperty.call(runManifestsById, runId)) {
          const manifestPayload = runManifestsById[runId];
          if (manifestPayload === null) {
            return HttpResponse.json(
              { detail: `workflow manifest for run ${runId} is not available` },
              { status: 404 },
            );
          }
          return HttpResponse.json(envelope(manifestPayload));
        }

        const run =
          runs.find((item) => String(item.workflow_run_id) === runId) ?? null;
        if (!run) {
          return HttpResponse.json(
            { detail: `workflow run ${runId} not found` },
            { status: 404 },
          );
        }

        const workflowVersionId =
          typeof run.workflow_version_id === "string" ? run.workflow_version_id : null;
        const version =
          workflowVersionId === null
            ? versions[0] ?? null
            : versions.find((item) => item.version_id === workflowVersionId) ?? null;
        if (!version) {
          return HttpResponse.json(
            {
              detail: `workflow version ${workflowVersionId ?? "for this run"} not found`,
            },
            { status: 404 },
          );
        }
        return HttpResponse.json(
          envelope({
            workflow_run_id: runId,
            workflow_id: "WF-1",
            workflow_version_id:
              typeof version.version_id === "string" ? version.version_id : workflowVersionId,
            manifest_mode: "saved_version",
            manifest_hash:
              typeof version.manifest_hash === "string"
                ? version.manifest_hash
                : `hash-${runId}`,
            manifest: version.manifest,
          }),
        );
      }),
      http.get(`${API_BASE}/workflow-runs/:runId/approvals`, ({ params }) =>
        HttpResponse.json(envelope(runApprovalsById[String(params.runId)] ?? [])),
      ),
      http.get(`${API_BASE}/workflow-runs/:runId/events`, ({ params }) =>
        HttpResponse.json(envelope(runEventsById[String(params.runId)] ?? [])),
      ),
      http.get(`${API_BASE}/workflow-runs/:runId/checkpoints`, ({ params }) =>
        HttpResponse.json(envelope(runCheckpointsById[String(params.runId)] ?? [])),
      ),
      http.get(`${API_BASE}/workflow-runs/:runId/files`, ({ params }) =>
        HttpResponse.json(
          envelope({
            items: runFilesById[String(params.runId)] ?? [],
            next_cursor: null,
          }),
        ),
      ),
      http.get(`${API_BASE}/workflows/WF-1/session-memory`, ({ request }) => {
        const url = new URL(request.url);
        const sessionId = url.searchParams.get("session_id") ?? "";
        const nodeId = url.searchParams.get("node_id");
        const entries = [...(sessionMemoryBySessionId[sessionId] ?? [])];
        const filtered = nodeId
          ? entries.filter((entry) => entry.node_id === nodeId)
          : entries;
        return HttpResponse.json(envelope(filtered));
      }),
      http.delete(`${API_BASE}/workflows/WF-1/session-memory`, ({ request }) => {
        const url = new URL(request.url);
        const sessionId = url.searchParams.get("session_id") ?? "";
        const nodeId = url.searchParams.get("node_id");
        const entries = [...(sessionMemoryBySessionId[sessionId] ?? [])];
        const removed = nodeId
          ? entries.filter((entry) => entry.node_id === nodeId)
          : entries;
        const remaining = nodeId
          ? entries.filter((entry) => entry.node_id !== nodeId)
          : [];
        sessionMemoryBySessionId[sessionId] = remaining;
        const deletedMessages = removed.reduce(
          (sum, entry) =>
            sum + (Array.isArray(entry.message_history) ? entry.message_history.length : 0),
          0,
        );
        return HttpResponse.json(
          envelope({
            workflow_id: "WF-1",
            session_id: sessionId,
            node_id: nodeId,
            deleted_entries: removed.length,
            deleted_messages: deletedMessages,
          }),
        );
      }),
      http.get(`${API_BASE}/agents`, () =>
        HttpResponse.json(envelope(agents)),
      ),
      http.get(`${API_BASE}/workflows/WF-1/calibration/options`, () =>
        HttpResponse.json(
          envelope({
            supported_objectives: ["quality", "tool_adherence"],
            supported_move_set: ["add_grounding_guardrail"],
            scorer_options: ["quality_match", "tool_adherence"],
            default_budget: { max_candidates: 3, max_eval_examples: 20, min_examples: 2 },
            data: {
              workflow_version_id: "WFV-1",
              judge: judgeAvailable
                ? {
                    available: true,
                    provider: "openai",
                    model: "gpt-4o-mini",
                  }
                : {
                    available: false,
                    reason: "Enable workflow_llm_judge_enabled to use LLM judge scoring.",
                  },
              deploy_gate_dataset: datasetAvailable
                ? {
                    available: true,
                    dataset_ref: "support_eval",
                    dataset_name: "support-eval-v3",
                    dataset_id: "ds-1",
                    active: true,
                    example_count: 4,
                  }
                : {
                    available: false,
                    reason: "No active deploy-gate eval dataset with non-superseded examples.",
                  },
            },
          }),
        ),
      ),
    ];
  }

  it("applies a candidate_ready calibration run inline from workflow detail", async () => {
    const candidateReadyJob = {
      job_id: "RFN-CAL-READY",
      agent_id: "support-agent",
      workflow_id: "WF-1",
      primary_item_id: "FB-READY",
      mlflow_run_id: null,
      artifact_type: "workflow_manifest",
      optimizer_type: null,
      status: "candidate_ready",
      current_stage: "eval",
      attempt_count: 1,
      error_message: null,
      total_tokens: 0,
      cost_usd: 0,
      bundle_targets: [],
      bundle_expansion_count: 0,
      diagnosis: null,
      candidate: null,
      eval_results: null,
      calibration_spec: { objective: { maximize: "quality", epsilon: 0.02 } },
      created_at: NOW,
      updated_at: NOW,
    };
    const appliedJob = { ...candidateReadyJob, status: "applied" };
    let applyCount = 0;
    server.use(
      // First /jobs read returns the candidate_ready run; after Apply the
      // invalidation re-reads and the run reports as applied.
      http.get(`${API_BASE}/jobs`, () =>
        HttpResponse.json(envelope(applyCount === 0 ? [candidateReadyJob] : [appliedJob])),
      ),
      http.post(`${API_BASE}/jobs/:jobId/apply`, ({ params }) => {
        applyCount += 1;
        return HttpResponse.json(
          envelope({ job_id: String(params.jobId), status: "applied", promotion: {} }),
        );
      }),
      ...detailHandlers(),
    );

    renderAt(<WorkflowDetail />, "/workflows/WF-1", "/workflows/:workflowId");

    await userEvent.click(await screen.findByTestId("workflow-calibrate"));
    const applyButton = await screen.findByTestId("job-apply-btn");
    expect(screen.getByTestId("workflow-calibration-runs")).toHaveTextContent("RFN-CAL-READY");

    await userEvent.click(applyButton);

    // The apply endpoint is called and the re-fetched run reflects "applied",
    // so the Apply button is gone.
    await waitFor(() => expect(applyCount).toBe(1));
    await waitFor(() =>
      expect(screen.queryByTestId("job-apply-btn")).not.toBeInTheDocument(),
    );
    expect(screen.getByTestId("workflow-calibration-runs")).toHaveTextContent("applied");
  });

  it("hydrates a deep-linked run from workflow detail query params", async () => {
    const deepLinkedRun = makeRun({
      workflow_run_id: "WR-DEEP-LINK",
      trace_id: "trace-deep-link",
      status: "waiting_event",
      current_node_id: "wait_gate",
    });
    server.use(
      ...detailHandlers({
        runs: [deepLinkedRun],
      }),
    );

    renderAt(
      <WorkflowDetail />,
      "/workflows/WF-1?tab=runs&run=WR-DEEP-LINK",
      "/workflows/:workflowId",
    );

    expect(await screen.findByTestId("workflow-run-panel")).toBeInTheDocument();
    expect((await screen.findAllByText("WR-DEEP-LINK")).length).toBeGreaterThan(0);
    expect(await screen.findByTestId("run-open-link")).toHaveAttribute(
      "href",
      "/workflow-runs/WR-DEEP-LINK",
    );
    expect(await screen.findByTestId("run-waiting-event-chip")).toHaveTextContent(
      "waiting event wait_gate",
    );
  });

  it("starts a workflow calibration run", async () => {
    let posted: Record<string, unknown> | null = null;
    server.use(
      ...detailHandlers(),
      http.post(`${API_BASE}/workflows/WF-1/calibration/runs`, async ({ request }) => {
        posted = (await request.json()) as Record<string, unknown>;
        return HttpResponse.json(
          envelope({
            item: {
              item_id: "FB-1",
              agent_id: "support-agent",
              category: "workflow_calibration",
              free_text: "",
              severity: "standard",
              status: "verified",
              source: "manual",
              workflow_id: "WF-1",
              submitted_context: {},
              created_at: NOW,
              updated_at: NOW,
            },
            job: {
              job_id: "RFN-CAL-1",
              agent_id: "support-agent",
              workflow_id: "WF-1",
              primary_item_id: "FB-1",
              mlflow_run_id: null,
              artifact_type: "workflow_manifest",
              optimizer_type: null,
              status: "queued",
              current_stage: "diagnosis",
              attempt_count: 1,
              error_message: null,
              total_tokens: 0,
              cost_usd: 0,
              bundle_targets: [],
              bundle_expansion_count: 0,
              diagnosis: null,
              candidate: null,
              eval_results: null,
              calibration_spec: { objective: { maximize: "tool_adherence", epsilon: 0.01 } },
              created_at: NOW,
              updated_at: NOW,
            },
          }),
          { status: 201 },
        );
      }),
    );

    renderAt(<WorkflowDetail />, "/workflows/WF-1", "/workflows/:workflowId");
    await userEvent.click(await screen.findByTestId("workflow-calibrate"));
    expect(await screen.findByTestId("workflow-calibration-dataset")).toHaveTextContent(
      "support-eval-v3",
    );
    await userEvent.selectOptions(screen.getByTestId("workflow-calibration-objective"), "tool_adherence");
    await userEvent.clear(screen.getByTestId("workflow-calibration-epsilon"));
    await userEvent.type(screen.getByTestId("workflow-calibration-epsilon"), "0");
    await userEvent.clear(screen.getByTestId("workflow-calibration-max-candidates"));
    await userEvent.type(screen.getByTestId("workflow-calibration-max-candidates"), "2");
    await userEvent.click(screen.getByTestId("workflow-calibration-start"));

    await waitFor(() => expect(posted).not.toBeNull());
    expect(posted).toMatchObject({
      agent_id: "support-agent",
      objective: { maximize: "tool_adherence", epsilon: 0 },
      budget: { max_candidates: 2, max_eval_examples: 20, min_examples: 2 },
      judge: { enabled: false },
    });
    // The latest run id + status now
    // render inline instead of linking to a job detail page.
    expect(await screen.findByTestId("workflow-calibration-last-run")).toHaveTextContent(
      "RFN-CAL-1",
    );
  });

  it("can enable LLM judge scoring for a calibration run when available", async () => {
    let posted: Record<string, unknown> | null = null;
    server.use(
      ...detailHandlers({ judgeAvailable: true }),
      http.post(`${API_BASE}/workflows/WF-1/calibration/runs`, async ({ request }) => {
        posted = (await request.json()) as Record<string, unknown>;
        return HttpResponse.json(
          envelope({
            item: {
              item_id: "FB-JUDGE-1",
              agent_id: "support-agent",
              category: "workflow_calibration",
              free_text: "",
              severity: "standard",
              status: "verified",
              source: "manual",
              workflow_id: "WF-1",
              submitted_context: {},
              created_at: NOW,
              updated_at: NOW,
            },
            job: {
              job_id: "RFN-CAL-JUDGE-1",
              agent_id: "support-agent",
              workflow_id: "WF-1",
              primary_item_id: "FB-JUDGE-1",
              mlflow_run_id: null,
              artifact_type: "workflow_manifest",
              optimizer_type: null,
              status: "queued",
              current_stage: "diagnosis",
              attempt_count: 1,
              error_message: null,
              total_tokens: 0,
              cost_usd: 0,
              bundle_targets: [],
              bundle_expansion_count: 0,
              diagnosis: null,
              candidate: null,
              eval_results: null,
              calibration_spec: { judge: { enabled: true } },
              created_at: NOW,
              updated_at: NOW,
            },
          }),
          { status: 201 },
        );
      }),
    );

    renderAt(<WorkflowDetail />, "/workflows/WF-1", "/workflows/:workflowId");
    await userEvent.click(await screen.findByTestId("workflow-calibrate"));
    expect(await screen.findByTestId("workflow-calibration-judge-status")).toHaveTextContent(
      "openai · gpt-4o-mini",
    );

    const judgeToggle = screen.getByTestId("workflow-calibration-judge-toggle");
    expect(judgeToggle).toBeEnabled();
    await userEvent.click(judgeToggle);
    expect(await screen.findByTestId("workflow-calibration-judge-enabled")).toHaveTextContent(
      "LLM judge enabled for this run",
    );

    await userEvent.click(screen.getByTestId("workflow-calibration-start"));

    await waitFor(() => expect(posted).not.toBeNull());
    expect(posted).toMatchObject({
      agent_id: "support-agent",
      judge: { enabled: true },
    });
  });

  it("disables start when no deploy-gate dataset is available", async () => {
    server.use(...detailHandlers({ datasetAvailable: false }));

    renderAt(<WorkflowDetail />, "/workflows/WF-1", "/workflows/:workflowId");
    await userEvent.click(await screen.findByTestId("workflow-calibrate"));

    expect(await screen.findByTestId("workflow-calibration-dataset")).toHaveTextContent(
      "No active deploy-gate eval dataset",
    );
    expect(screen.getByTestId("workflow-calibration-start")).toBeDisabled();
    expect(screen.getByTestId("workflow-calibration-judge-toggle")).toBeDisabled();
  });

  it("renders empty states when a workflow has no versions, deployments, runs, promotions, or patches", async () => {
    server.use(
      ...detailHandlers({
        datasetAvailable: false,
        versions: [],
        capabilities: {
          queue_enabled: true,
          supports_async_submit: true,
          supports_cancel: false,
          supports_retry: false,
          supports_resume: false,
          runtime_approvals_enabled: false,
          checkpointing_enabled: false,
          event_backend: "in_process",
        },
      }),
    );

    renderAt(<WorkflowDetail />, "/workflows/WF-1", "/workflows/:workflowId");

    expect(await screen.findByText("No versions yet")).toBeInTheDocument();
    expect(screen.queryByTestId("workflow-run-open")).not.toBeInTheDocument();

    await userEvent.click(screen.getByTestId("tab-versions"));
    expect(await screen.findByText("No versions")).toBeInTheDocument();

    await userEvent.click(screen.getByTestId("tab-deployments"));
    expect(await screen.findByText("No deployments")).toBeInTheDocument();
    expect(screen.getByTestId("deployment-promote")).toBeDisabled();

    await userEvent.click(screen.getByTestId("tab-runs"));
    expect(await screen.findByText("No runs recorded")).toBeInTheDocument();
    expect(screen.getByTestId("runs-table")).toHaveTextContent(
      "No published workflow version is available yet. Publish a version first, then queue the first execution from the run controls above.",
    );
    expect(screen.getByTestId("workflow-run-target")).toHaveTextContent("no version");
    expect(screen.getByTestId("workflow-run-start")).toBeDisabled();

    await userEvent.click(screen.getByTestId("tab-promotions"));
    expect(await screen.findByText("No promotion requests")).toBeInTheDocument();

    await userEvent.click(screen.getByTestId("tab-patches"));
    expect(await screen.findByText("No CALIBER patches yet")).toBeInTheDocument();
  });

  it("turns an empty run history into first-execution guidance when a runnable version exists", async () => {
    server.use(
      ...detailHandlers({
        runs: [],
        capabilities: {
          queue_enabled: true,
          supports_async_submit: true,
          supports_cancel: false,
          supports_retry: false,
          supports_resume: false,
          runtime_approvals_enabled: false,
          checkpointing_enabled: false,
          event_backend: "in_process",
        },
      }),
    );

    renderAt(<WorkflowDetail />, "/workflows/WF-1", "/workflows/:workflowId");

    await userEvent.click(await screen.findByTestId("tab-runs"));

    expect(await screen.findByText("No runs recorded")).toBeInTheDocument();
    expect(screen.getByTestId("runs-table")).toHaveTextContent(
      "Queue a run from the controls above or trigger the workflow from a deployed alias to create the first execution.",
    );
    expect(screen.getByTestId("runs-table")).toHaveTextContent(
      "this tab will show status, replay, checkpoints, lineage, and debugger history",
    );
    expect(screen.getByTestId("workflow-run-start")).toBeEnabled();
  });

  it("renders version rows and a draft editor shortcut", async () => {
    server.use(
      ...detailHandlers({
        versions: [
          makeVersion({ status: "published" }),
          makeVersion({
            version_id: "WFV-DRAFT",
            version_number: 2,
            status: "draft",
            created_by: "",
          }),
        ],
      }),
    );

    renderAt(<WorkflowDetail />, "/workflows/WF-1", "/workflows/:workflowId");

    expect(await screen.findByTestId("edit-draft")).toHaveTextContent("Edit Draft v2");
    await userEvent.click(screen.getByTestId("tab-versions"));
    const table = await screen.findByTestId("versions-table");
    expect(table).toHaveTextContent("v1");
    expect(table).toHaveTextContent("WFV-DRAFT");
    expect(table).toHaveTextContent("—");
  });

  it("surfaces calibration errors and allows selecting another enabled agent", async () => {
    let posted: Record<string, unknown> | null = null;
    server.use(
      ...detailHandlers({
        agents: [
          {
            agent_id: "disabled-agent",
            experiment_id: "exp-disabled",
            name: "Disabled Agent",
            owner: "@test",
            artifact_types: ["prompt"],
            eval_thresholds: {},
            optimizer_config: {},
            approval_policy: {},
            optimize_for: "quality",
            collaboration_mode: null,
            enabled: false,
            required_approvals: 1,
            created_at: NOW,
            updated_at: NOW,
          },
          {
            agent_id: "calibration-agent",
            experiment_id: "exp-calibration",
            name: "Calibration Agent",
            owner: "@test",
            artifact_types: ["prompt"],
            eval_thresholds: {},
            optimizer_config: {},
            approval_policy: {},
            optimize_for: "quality",
            collaboration_mode: null,
            enabled: true,
            required_approvals: 1,
            created_at: NOW,
            updated_at: NOW,
          },
          {
            agent_id: "review-agent",
            experiment_id: "exp-review",
            name: "Review Agent",
            owner: "@test",
            artifact_types: ["prompt"],
            eval_thresholds: {},
            optimizer_config: {},
            approval_policy: {},
            optimize_for: "quality",
            collaboration_mode: null,
            enabled: true,
            required_approvals: 1,
            created_at: NOW,
            updated_at: NOW,
          },
        ],
      }),
      http.post(`${API_BASE}/workflows/WF-1/calibration/runs`, async ({ request }) => {
        posted = (await request.json()) as Record<string, unknown>;
        return HttpResponse.json({ detail: "calibration service down" }, { status: 500 });
      }),
    );

    renderAt(<WorkflowDetail />, "/workflows/WF-1", "/workflows/:workflowId");
    await userEvent.click(await screen.findByTestId("workflow-calibrate"));
    await userEvent.selectOptions(await screen.findByTestId("workflow-calibration-agent"), "review-agent");
    await userEvent.click(screen.getByTestId("workflow-calibration-start"));

    await waitFor(() => expect(posted).toMatchObject({ agent_id: "review-agent" }));
    expect(await screen.findByTestId("workflow-calibration-error")).toHaveTextContent(
      "calibration service down",
    );
  });

  it("deploys a published version to an alias from the deployments tab", async () => {
    let deployBody: Record<string, unknown> | null = null;
    server.use(
      ...detailHandlers(),
      http.post(`${API_BASE}/workflows/WF-1/deployments/dev/promote`, async ({ request }) => {
        deployBody = (await request.json()) as Record<string, unknown>;
        return HttpResponse.json(
          envelope({
            rotated: true,
            deployment: {
              deployment_id: "WFD-1",
              workflow_id: "WF-1",
              alias: "dev",
              version_id: "WFV-1",
              environment: null,
              status: "active",
              deployed_by: "@test",
              deployed_at: NOW,
              rollback_checkpoint: [],
            },
            promotion: null,
            gate: { has_gate: false, passed: true, runs: [] },
          }),
        );
      }),
    );

    renderAt(<WorkflowDetail />, "/workflows/WF-1", "/workflows/:workflowId");
    await userEvent.click(await screen.findByTestId("tab-deployments"));
    await userEvent.selectOptions(screen.getByTestId("deployment-version-select"), "WFV-1");
    await userEvent.click(screen.getByTestId("deployment-promote"));

    await waitFor(() => expect(deployBody).toMatchObject({ version_id: "WFV-1" }));
    expect(await screen.findByTestId("deployment-message")).toHaveTextContent(
      "Alias dev now points to WFV-1.",
    );
  });

  it("shows deployment errors from the promote endpoint", async () => {
    server.use(
      ...detailHandlers(),
      http.post(`${API_BASE}/workflows/WF-1/deployments/dev/promote`, () =>
        HttpResponse.json({ detail: "deploy gate unavailable" }, { status: 500 }),
      ),
    );

    renderAt(<WorkflowDetail />, "/workflows/WF-1", "/workflows/:workflowId");
    await userEvent.click(await screen.findByTestId("tab-deployments"));
    await userEvent.click(screen.getByTestId("deployment-promote"));

    expect(await screen.findByTestId("deployment-message")).toHaveTextContent(
      "Deployment failed: deploy gate unavailable",
    );
  });

  it("submits prod deployments as promotion requests when gates require approval", async () => {
    let deployBody: Record<string, unknown> | null = null;
    server.use(
      ...detailHandlers(),
      http.post(`${API_BASE}/workflows/WF-1/deployments/prod/promote`, async ({ request }) => {
        deployBody = (await request.json()) as Record<string, unknown>;
        return HttpResponse.json(
          envelope({
            rotated: false,
            deployment: null,
            promotion: {
              promotion_id: "PROMO-PROD",
              workflow_id: "WF-1",
              alias: "prod",
              version_id: "WFV-1",
              status: "pending",
              gate_result: { has_gate: true, passed: true, runs: [] },
              requested_by: "@ops",
              requested_at: NOW,
              decided_by: null,
              decided_at: null,
              decision_reason: null,
            },
            gate: { has_gate: true, passed: true, runs: [] },
          }),
        );
      }),
    );

    renderAt(<WorkflowDetail />, "/workflows/WF-1", "/workflows/:workflowId");
    await userEvent.click(await screen.findByTestId("tab-deployments"));
    await userEvent.clear(screen.getByTestId("deployment-alias-input"));
    await userEvent.type(screen.getByTestId("deployment-alias-input"), " Prod ");
    await userEvent.selectOptions(screen.getByTestId("deployment-version-select"), "WFV-1");
    await userEvent.click(screen.getByTestId("deployment-promote"));

    await waitFor(() => expect(deployBody).toMatchObject({ version_id: "WFV-1" }));
    expect(await screen.findByTestId("promotions-list")).toBeInTheDocument();
    await userEvent.click(screen.getByTestId("tab-deployments"));
    expect(await screen.findByTestId("deployment-message")).toHaveTextContent(
      "Promotion request PROMO-PROD submitted for alias prod. Awaiting approval.",
    );
  });

  it("runs using the version currently deployed to prod", async () => {
    let runBody: Record<string, unknown> | null = null;
    server.use(
      ...detailHandlers({
        capabilities: {
          queue_enabled: true,
          supports_async_submit: true,
          supports_cancel: false,
          supports_retry: false,
          supports_resume: false,
          runtime_approvals_enabled: false,
          checkpointing_enabled: false,
          event_backend: "in_process",
        },
        versions: [
          makeVersion({ version_id: "WFV-2", version_number: 2, status: "published" }),
          makeVersion({ version_id: "WFV-1", version_number: 1, status: "published" }),
        ],
        deployments: [
          {
            deployment_id: "WFD-2",
            workflow_id: "WF-1",
            alias: "prod",
            version_id: "WFV-2",
            environment: null,
            status: "active",
            deployed_by: "@ops",
            deployed_at: NOW,
            rollback_checkpoint: [],
          },
        ],
      }),
      http.post(`${API_BASE}/workflow-runs`, async ({ request }) => {
        runBody = (await request.json()) as Record<string, unknown>;
        return HttpResponse.json(
          envelope(
            makeRun({
              workflow_run_id: "WR-2",
              status: "completed",
              workflow_version_id: "WFV-2",
              deployment_alias: "prod",
              completed_at: NOW,
            }),
          ),
        );
      }),
    );

    renderAt(<WorkflowDetail />, "/workflows/WF-1", "/workflows/:workflowId");
    await userEvent.click(await screen.findByTestId("workflow-run-open"));
    await userEvent.selectOptions(screen.getByTestId("workflow-run-alias"), "prod");
    await userEvent.type(screen.getByTestId("workflow-run-session-id"), "SESSION-prod");
    await userEvent.click(screen.getByTestId("workflow-run-start"));

    await waitFor(() =>
      expect(runBody).toMatchObject({
        workflow_id: "WF-1",
        alias: "prod",
        session_id: "SESSION-prod",
        source: "manual",
      }),
    );
    expect(screen.getByTestId("workflow-run-target")).toHaveTextContent("alias prod");
  });

  it("queues workflow runs through the async run endpoint for manual and alias targets", async () => {
    const postedRuns: Array<Record<string, unknown>> = [];
    server.use(
      ...detailHandlers({
        capabilities: {
          queue_enabled: true,
          supports_async_submit: true,
          supports_cancel: false,
          supports_retry: false,
          supports_resume: false,
          runtime_approvals_enabled: false,
          checkpointing_enabled: false,
          event_backend: "in_process",
        },
        deployments: [
          {
            deployment_id: "WFD-PROD",
            workflow_id: "WF-1",
            alias: "prod",
            version_id: "WFV-1",
            environment: null,
            status: "active",
            deployed_by: "@ops",
            deployed_at: NOW,
            rollback_checkpoint: [],
          },
        ],
      }),
      http.post(`${API_BASE}/workflow-runs`, async ({ request }) => {
        const body = (await request.json()) as Record<string, unknown>;
        postedRuns.push(body);
        return HttpResponse.json(
          envelope(
            makeRun({
              workflow_run_id: `WR-ASYNC-${postedRuns.length}`,
              status: "queued",
              deployment_alias: body.alias ?? null,
              workflow_version_id: body.workflow_version_id ?? "WFV-1",
            }),
          ),
          { status: 202 },
        );
      }),
    );

    renderAt(<WorkflowDetail />, "/workflows/WF-1", "/workflows/:workflowId");
    await userEvent.click(await screen.findByTestId("workflow-run-open"));
    await userEvent.clear(screen.getByTestId("workflow-run-input"));
    await userEvent.type(screen.getByTestId("workflow-run-input"), "async input");
    await userEvent.type(screen.getByTestId("workflow-run-session-id"), "SESSION-async");
    await userEvent.click(screen.getByTestId("workflow-run-start"));

    await waitFor(() => expect(postedRuns).toHaveLength(1));
    expect(postedRuns[0]).toMatchObject({
      workflow_version_id: "WFV-1",
      alias: "manual",
      input: "async input",
      session_id: "SESSION-async",
      source: "manual",
    });
    expect(await screen.findByTestId("workflow-run-message")).toHaveTextContent(
      "Run WR-ASYNC-1 is queued.",
    );
    expect(screen.getByTestId("runs-back")).toBeInTheDocument();

    await userEvent.selectOptions(screen.getByTestId("workflow-run-alias"), "prod");
    await userEvent.click(screen.getByTestId("workflow-run-start"));

    await waitFor(() => expect(postedRuns).toHaveLength(2));
    expect(postedRuns[1]).toMatchObject({
      workflow_id: "WF-1",
      alias: "prod",
      input: "async input",
      session_id: "SESSION-async",
      source: "manual",
    });
    expect(postedRuns[1]).not.toHaveProperty("workflow_version_id");
  });

  it("disables workflow launch controls when deployment run capabilities fail to load", async () => {
    const baseVersion = makeVersion({ status: "published" });
    const manifest = {
      ...baseVersion.manifest,
      nodes: {
        ...baseVersion.manifest.nodes,
        start: {
          ...baseVersion.manifest.nodes.start,
          trigger: { mode: "event", event_name: "object.created", enabled: true },
        },
      },
    };
    server.use(
      http.get(`${API_BASE}/capabilities`, () =>
        HttpResponse.json({ detail: "capabilities unavailable" }, { status: 500 }),
      ),
      ...detailHandlers({
        versions: [{ ...baseVersion, manifest }],
        capabilities: {
          queue_enabled: true,
          supports_async_submit: true,
          supports_cancel: true,
          supports_retry: true,
          supports_resume: true,
          runtime_approvals_enabled: true,
          checkpointing_enabled: true,
          event_backend: "nats",
        },
        deployments: [
          {
            deployment_id: "WFD-PROD",
            workflow_id: "WF-1",
            alias: "prod",
            version_id: "WFV-1",
            environment: null,
            status: "active",
            deployed_by: "@ops",
            deployed_at: NOW,
            rollback_checkpoint: [],
          },
        ],
      }),
    );

    renderAt(<WorkflowDetail />, "/workflows/WF-1", "/workflows/:workflowId");
    await userEvent.click(await screen.findByTestId("workflow-run-open"));

    const runButton = screen.getByTestId("workflow-run-start");
    await waitFor(() => expect(runButton).toBeDisabled());
    expect(runButton).toHaveAttribute(
      "title",
      "Workflow run capabilities could not be loaded. Refresh the page or verify deployment settings/API health.",
    );
    expect(await screen.findByTestId("workflow-run-capabilities-note")).toHaveTextContent(
      "Run controls are disabled until workflow run capabilities can be loaded.",
    );
    expect(screen.getByTestId("workflow-run-capabilities-note")).toHaveTextContent(
      "Verify the CALIBER API and workflow-run settings.",
    );
    await userEvent.click(screen.getByTestId("tab-deployments"));
    expect(await screen.findByTestId("workflow-trigger-capability-note")).toHaveTextContent(
      "Event-trigger launches are disabled until workflow run capabilities can be loaded.",
    );
    expect(screen.getByTestId("workflow-trigger-capability-note")).toHaveTextContent(
      "Verify the CALIBER API and workflow-run settings.",
    );
  });

  it("triggers event-start deployments from the deployments table", async () => {
    const baseVersion = makeVersion({ status: "published" });
    const manifest = {
      ...baseVersion.manifest,
      nodes: {
        ...baseVersion.manifest.nodes,
        start: {
          ...baseVersion.manifest.nodes.start,
          trigger: { mode: "event", event_name: "object.created", enabled: true },
        },
      },
    };
    let triggerBody: Record<string, unknown> | null = null;
    server.use(
      ...detailHandlers({
        versions: [{ ...baseVersion, manifest }],
        capabilities: {
          queue_enabled: true,
          supports_async_submit: true,
          supports_cancel: false,
          supports_retry: false,
          supports_resume: false,
          runtime_approvals_enabled: false,
          checkpointing_enabled: false,
          event_backend: "nats",
        },
        deployments: [
          {
            deployment_id: "WFD-PROD",
            workflow_id: "WF-1",
            alias: "prod",
            version_id: "WFV-1",
            environment: null,
            status: "active",
            deployed_by: "@ops",
            deployed_at: NOW,
            rollback_checkpoint: [],
          },
        ],
      }),
      http.post(`${API_BASE}/workflows/WF-1/trigger`, async ({ request }) => {
        triggerBody = (await request.json()) as Record<string, unknown>;
        return HttpResponse.json(
          envelope(
            makeRun({
              workflow_run_id: "WR-EVENT",
              status: "queued",
              deployment_alias: "prod",
            }),
          ),
          { status: 202 },
        );
      }),
    );

    renderAt(<WorkflowDetail />, "/workflows/WF-1", "/workflows/:workflowId");
    expect(await screen.findByTestId("workflow-trigger-badge")).toHaveTextContent(
      "Event: object.created",
    );
    await userEvent.click(screen.getByTestId("tab-deployments"));
    const triggerButton = await screen.findByTestId("trigger-now-prod");
    expect(triggerButton).toBeEnabled();

    await userEvent.click(triggerButton);

    await waitFor(() => expect(triggerBody).toMatchObject({ alias: "prod" }));
    expect(await screen.findByTestId("runs-back")).toBeInTheDocument();
    expect(await screen.findByTestId("workflow-run-message")).toHaveTextContent(
      "Run WR-EVENT triggered on prod (queued).",
    );
  });

  it("only shows Trigger now for deployment aliases targeted by the Start trigger", async () => {
    const baseVersion = makeVersion({ status: "published" });
    const manifest = {
      ...baseVersion.manifest,
      nodes: {
        ...baseVersion.manifest.nodes,
        start: {
          ...baseVersion.manifest.nodes.start,
          trigger: { mode: "event", event_name: "object.created", alias: "prod", enabled: true },
        },
      },
    };
    server.use(
      ...detailHandlers({
        versions: [{ ...baseVersion, manifest }],
        capabilities: {
          queue_enabled: true,
          supports_async_submit: true,
          supports_cancel: false,
          supports_retry: false,
          supports_resume: false,
          runtime_approvals_enabled: false,
          checkpointing_enabled: false,
          event_backend: "nats",
        },
        deployments: [
          {
            deployment_id: "WFD-PROD",
            workflow_id: "WF-1",
            alias: "prod",
            version_id: "WFV-1",
            environment: null,
            status: "active",
            deployed_by: "@ops",
            deployed_at: NOW,
            rollback_checkpoint: [],
          },
          {
            deployment_id: "WFD-STAGING",
            workflow_id: "WF-1",
            alias: "staging",
            version_id: "WFV-1",
            environment: null,
            status: "active",
            deployed_by: "@ops",
            deployed_at: NOW,
            rollback_checkpoint: [],
          },
        ],
      }),
    );

    renderAt(<WorkflowDetail />, "/workflows/WF-1", "/workflows/:workflowId");
    await userEvent.click(await screen.findByTestId("tab-deployments"));

    expect(await screen.findByTestId("trigger-now-prod")).toBeInTheDocument();
    expect(screen.queryByTestId("trigger-now-staging")).not.toBeInTheDocument();
  });

  it("keeps event trigger buttons disabled when the run queue is unavailable", async () => {
    const baseVersion = makeVersion({ status: "published" });
    const manifest = {
      ...baseVersion.manifest,
      nodes: {
        ...baseVersion.manifest.nodes,
        start: {
          ...baseVersion.manifest.nodes.start,
          trigger: { mode: "event", event_name: "object.created", enabled: true },
        },
      },
    };
    server.use(
      ...detailHandlers({
        versions: [{ ...baseVersion, manifest }],
        capabilities: {
          queue_enabled: false,
          supports_async_submit: true,
          supports_cancel: false,
          supports_retry: false,
          supports_resume: false,
          runtime_approvals_enabled: false,
          checkpointing_enabled: false,
          event_backend: "in_process",
        },
        deployments: [
          {
            deployment_id: "WFD-PROD",
            workflow_id: "WF-1",
            alias: "prod",
            version_id: "WFV-1",
            environment: null,
            status: "active",
            deployed_by: "@ops",
            deployed_at: NOW,
            rollback_checkpoint: [],
          },
        ],
      }),
    );

    renderAt(<WorkflowDetail />, "/workflows/WF-1", "/workflows/:workflowId");
    await userEvent.click(await screen.findByTestId("tab-deployments"));
    const triggerButton = await screen.findByTestId("trigger-now-prod");

    expect(triggerButton).toBeDisabled();
    expect(triggerButton).toHaveAttribute("title", "Enable the run queue to trigger runs");
  });

  it("keeps event trigger buttons disabled while a workflow is paused", async () => {
    const baseVersion = makeVersion({ status: "published" });
    const manifest = {
      ...baseVersion.manifest,
      nodes: {
        ...baseVersion.manifest.nodes,
        start: {
          ...baseVersion.manifest.nodes.start,
          trigger: { mode: "event", event_name: "object.created", enabled: true },
        },
      },
    };
    server.use(
      ...detailHandlers({
        workflow: { status: "paused" },
        versions: [{ ...baseVersion, manifest }],
        capabilities: {
          queue_enabled: true,
          supports_async_submit: true,
          supports_cancel: false,
          supports_retry: false,
          supports_resume: false,
          runtime_approvals_enabled: false,
          checkpointing_enabled: false,
          event_backend: "nats",
        },
        deployments: [
          {
            deployment_id: "WFD-PROD",
            workflow_id: "WF-1",
            alias: "prod",
            version_id: "WFV-1",
            environment: null,
            status: "active",
            deployed_by: "@ops",
            deployed_at: NOW,
            rollback_checkpoint: [],
          },
        ],
      }),
    );

    renderAt(<WorkflowDetail />, "/workflows/WF-1", "/workflows/:workflowId");
    await userEvent.click(await screen.findByTestId("tab-deployments"));
    const triggerButton = await screen.findByTestId("trigger-now-prod");

    expect(triggerButton).toBeDisabled();
    expect(triggerButton).toHaveAttribute(
      "title",
      "Resume this workflow before triggering runs",
    );
  });

  it("surfaces event trigger failures", async () => {
    const baseVersion = makeVersion({ status: "published" });
    const manifest = {
      ...baseVersion.manifest,
      nodes: {
        ...baseVersion.manifest.nodes,
        start: {
          ...baseVersion.manifest.nodes.start,
          trigger: { mode: "event", event_name: "object.created", enabled: true },
        },
      },
    };
    server.use(
      ...detailHandlers({
        versions: [{ ...baseVersion, manifest }],
        capabilities: {
          queue_enabled: true,
          supports_async_submit: true,
          supports_cancel: false,
          supports_retry: false,
          supports_resume: false,
          runtime_approvals_enabled: false,
          checkpointing_enabled: false,
          event_backend: "nats",
        },
        deployments: [
          {
            deployment_id: "WFD-PROD",
            workflow_id: "WF-1",
            alias: "prod",
            version_id: "WFV-1",
            environment: null,
            status: "active",
            deployed_by: "@ops",
            deployed_at: NOW,
            rollback_checkpoint: [],
          },
        ],
      }),
      http.post(`${API_BASE}/workflows/WF-1/trigger`, () =>
        HttpResponse.json({ detail: "event bridge offline" }, { status: 500 }),
      ),
    );

    renderAt(<WorkflowDetail />, "/workflows/WF-1", "/workflows/:workflowId");
    await userEvent.click(await screen.findByTestId("tab-deployments"));
    await userEvent.click(await screen.findByTestId("trigger-now-prod"));

    expect(await screen.findByTestId("trigger-message")).toHaveTextContent(
      "Trigger failed: event bridge offline",
    );
  });

  it("runs and pauses a workflow from the detail page", async () => {
    let runBody: Record<string, unknown> | null = null;
    let patchBody: Record<string, unknown> | null = null;
    const runs: Array<Record<string, unknown>> = [];
    server.use(
      ...detailHandlers({
        runs,
        capabilities: {
          queue_enabled: true,
          supports_async_submit: true,
          supports_cancel: false,
          supports_retry: false,
          supports_resume: false,
          runtime_approvals_enabled: false,
          checkpointing_enabled: false,
          event_backend: "in_process",
        },
      }),
      http.post(`${API_BASE}/workflow-runs`, async ({ request }) => {
        runBody = (await request.json()) as Record<string, unknown>;
        runs.unshift(
          makeRun({
            workflow_run_id: "WR-1",
            status: "completed",
            current_node_id: "support_agent",
            completed_at: NOW,
            summary: {
              output: "Policy-backed answer",
              node_path: ["start", "support_agent"],
              steps: [
                {
                  node_id: "start",
                  node_type: "start",
                  status: "ok",
                  output: "refund policy",
                  tool_calls: [],
                  handoff_target: null,
                  detail: "",
                  duration_ms: 0,
                },
                {
                  node_id: "support_agent",
                  node_type: "agent",
                  status: "ok",
                  output: "Policy-backed answer",
                  tool_calls: [{ tool: "lookup_policy", result: { policy: "30-day refund" } }],
                  handoff_target: null,
                  detail: "",
                  duration_ms: 0,
                },
              ],
              manifest_mode: "saved_version",
              manifest_hash: "hash1",
              workflow_version_number: 1,
            },
          }),
        );
        return HttpResponse.json(
          envelope(
            makeRun({
              workflow_run_id: "WR-1",
              status: "completed",
              workflow_version_id: "WFV-1",
              completed_at: NOW,
              summary: {
                output: "Policy-backed answer",
                node_path: ["start", "support_agent"],
                steps: [
                  {
                    node_id: "start",
                    node_type: "start",
                    status: "ok",
                    output: "refund policy",
                    tool_calls: [],
                    handoff_target: null,
                    detail: "",
                    duration_ms: 0,
                  },
                  {
                    node_id: "support_agent",
                    node_type: "agent",
                    status: "ok",
                    output: "Policy-backed answer",
                    tool_calls: [{ tool: "lookup_policy", result: { policy: "30-day refund" } }],
                    handoff_target: null,
                    detail: "",
                    duration_ms: 0,
                  },
                ],
                manifest_mode: "saved_version",
                manifest_hash: "hash1",
                workflow_version_number: 1,
              },
            }),
          ),
        );
      }),
      http.patch(`${API_BASE}/workflows/WF-1`, async ({ request }) => {
        patchBody = (await request.json()) as Record<string, unknown>;
        return HttpResponse.json(
          envelope({
            workflow_id: "WF-1",
            name: "Support Workflow",
            description: "",
            owner: "@test",
            status: patchBody.status,
            default_experiment_id: null,
            created_at: NOW,
            updated_at: NOW,
          }),
        );
      }),
    );

    renderAt(<WorkflowDetail />, "/workflows/WF-1", "/workflows/:workflowId");
    await userEvent.click(await screen.findByTestId("workflow-run-open"));
    await userEvent.clear(screen.getByTestId("workflow-run-input"));
    await userEvent.type(screen.getByTestId("workflow-run-input"), "refund policy");
    await userEvent.click(screen.getByTestId("workflow-run-start"));

    await waitFor(() =>
      expect(runBody).toMatchObject({
        workflow_version_id: "WFV-1",
        input: "refund policy",
        alias: "manual",
        source: "manual",
      }),
    );
    expect(await screen.findByTestId("runs-back")).toBeInTheDocument();
    const runLogs = await screen.findAllByTestId("workflow-run-logs");
    expect(runLogs.some((entry) => entry.textContent?.includes("support_agent"))).toBe(true);
    expect(screen.getByTestId("workflow-run-message")).toHaveTextContent("WR-1");

    await userEvent.click(screen.getByTestId("workflow-pause-toggle"));
    await waitFor(() => expect(patchBody).toMatchObject({ status: "paused" }));
  });

  it("disables manual runs while a workflow is paused", async () => {
    server.use(...detailHandlers({ workflow: { status: "paused" } }));

    renderAt(<WorkflowDetail />, "/workflows/WF-1", "/workflows/:workflowId");
    await userEvent.click(await screen.findByTestId("workflow-run-open"));

    expect(await screen.findAllByText("paused")).toHaveLength(2);
    expect(screen.getByTestId("workflow-run-start")).toBeDisabled();
  });

  it("shows workflow run errors without leaving the run panel silent", async () => {
    server.use(
      ...detailHandlers({
        capabilities: {
          queue_enabled: true,
          supports_async_submit: true,
          supports_cancel: false,
          supports_retry: false,
          supports_resume: false,
          runtime_approvals_enabled: false,
          checkpointing_enabled: false,
          event_backend: "in_process",
        },
      }),
      http.post(`${API_BASE}/workflow-runs`, () =>
        HttpResponse.json({ detail: "runner unavailable" }, { status: 500 }),
      ),
    );

    renderAt(<WorkflowDetail />, "/workflows/WF-1", "/workflows/:workflowId");
    await userEvent.click(await screen.findByTestId("workflow-run-open"));
    await userEvent.click(screen.getByTestId("workflow-run-start"));

    expect(await screen.findByTestId("workflow-run-message")).toHaveTextContent(
      "Run failed: runner unavailable",
    );
  });

  it("renders promotions and applies approve/reject actions", async () => {
    let approveCalls = 0;
    let rejectCalls = 0;
    server.use(
      ...detailHandlers({
        promotions: [
          {
            promotion_id: "PROMO-1",
            workflow_id: "WF-1",
            alias: "prod",
            version_id: "WFV-1",
            status: "pending",
            gate_result: null,
            requested_by: "@ops",
            requested_at: NOW,
            decided_by: null,
            decided_at: null,
            decision_reason: null,
          },
          {
            promotion_id: "PROMO-2",
            workflow_id: "WF-1",
            alias: "staging",
            version_id: "WFV-1",
            status: "pending",
            gate_result: null,
            requested_by: "@ops",
            requested_at: NOW,
            decided_by: null,
            decided_at: null,
            decision_reason: null,
          },
        ],
      }),
      http.post(`${API_BASE}/workflow-promotions/PROMO-1/approve`, () => {
        approveCalls += 1;
        return HttpResponse.json(
          envelope({
            promotion_id: "PROMO-1",
            workflow_id: "WF-1",
            alias: "prod",
            version_id: "WFV-1",
            status: "approved",
            gate_result: null,
            requested_by: "@ops",
            requested_at: NOW,
            decided_by: "@qa",
            decided_at: NOW,
            decision_reason: null,
          }),
        );
      }),
      http.post(`${API_BASE}/workflow-promotions/PROMO-2/reject`, () => {
        rejectCalls += 1;
        return HttpResponse.json(
          envelope({
            promotion_id: "PROMO-2",
            workflow_id: "WF-1",
            alias: "staging",
            version_id: "WFV-1",
            status: "rejected",
            gate_result: null,
            requested_by: "@ops",
            requested_at: NOW,
            decided_by: "@qa",
            decided_at: NOW,
            decision_reason: "Needs review",
          }),
        );
      }),
    );

    renderAt(<WorkflowDetail />, "/workflows/WF-1", "/workflows/:workflowId");
    await userEvent.click(await screen.findByTestId("tab-promotions"));
    await userEvent.click(await screen.findByTestId("approve-PROMO-1"));
    await userEvent.click(await screen.findByTestId("reject-PROMO-2"));

    await waitFor(() => expect(approveCalls).toBe(1));
    await waitFor(() => expect(rejectCalls).toBe(1));
  });

  it("renders non-empty patch list with graph diffs", async () => {
    server.use(
      ...detailHandlers({
        patches: [
          {
            patch_id: "PATCH-1",
            job_id: "JOB-1",
            workflow_id: "WF-1",
            base_version_id: "WFV-1",
            candidate_manifest: makeVersion().manifest,
            semantic_ops: [{ op: "add_guardrail" }],
            patch_summary: "Add guardrail before output",
            graph_diff: {
              added_nodes: [{ id: "guardrail", type: "guardrail" }],
              removed_nodes: [],
              modified_nodes: [],
              added_edges: ["e-new"],
              removed_edges: [],
              modified_edges: [],
              artifact_changes: [],
              deploy_gate_changes: [],
              empty: false,
            },
            risk_summary: "Low risk patch",
            created_at: NOW,
          },
        ],
      }),
    );

    renderAt(<WorkflowDetail />, "/workflows/WF-1", "/workflows/:workflowId");
    await userEvent.click(await screen.findByTestId("tab-patches"));
    expect(await screen.findByText("Add guardrail before output")).toBeInTheDocument();
    expect(screen.getByText("Low risk patch")).toBeInTheDocument();
    expect(screen.getByTestId("diff-added-node")).toHaveTextContent("guardrail");
  });

  it("supports cancel, retry, approve, and resume run actions from run details", async () => {
    let cancelCalls = 0;
    let retryCalls = 0;
    let approveCalls = 0;
    let resumeCalls = 0;
    let eventResumeBody: Record<string, unknown> | null = null;
    server.use(
      ...detailHandlers({
        capabilities: {
          queue_enabled: true,
          supports_async_submit: true,
          supports_cancel: true,
          supports_retry: true,
          supports_resume: true,
          runtime_approvals_enabled: true,
          checkpointing_enabled: true,
          event_backend: "in_process",
        },
        runs: [
          makeRun({ workflow_run_id: "WR-RUNNING", status: "running", trace_id: "trace-running" }),
          makeRun({ workflow_run_id: "WR-FAILED", status: "failed", trace_id: "trace-failed" }),
          makeRun({ workflow_run_id: "WR-WAIT", status: "waiting_approval", trace_id: "trace-wait" }),
          makeRun({ workflow_run_id: "WR-BLOCKED", status: "waiting_approval", trace_id: "trace-blocked" }),
          makeRun({
            workflow_run_id: "WR-RESUME",
            status: "waiting_approval",
            trace_id: "trace-resume",
            current_node_id: "approval",
          }),
          makeRun({
            workflow_run_id: "WR-EVENT",
            status: "waiting_event",
            trace_id: "trace-event",
            current_node_id: "wait_gate",
          }),
        ],
        runApprovalsById: {
          "WR-WAIT": [
            {
              runtime_approval_id: "RA-1",
              workflow_run_id: "WR-WAIT",
              project_id: null,
              node_id: "approval",
              status: "pending",
              requested_at: NOW,
              decided_at: null,
              decided_by: null,
              decision_reason: null,
              policy_snapshot: null,
            },
          ],
          "WR-BLOCKED": [],
          "WR-RESUME": [
            {
              runtime_approval_id: "RA-RESUME",
              workflow_run_id: "WR-RESUME",
              project_id: null,
              node_id: "approval",
              status: "approved",
              requested_at: NOW,
              decided_at: NOW,
              decided_by: "@approver",
              decision_reason: "Looks good.",
              policy_snapshot: null,
            },
          ],
        },
        runCheckpointsById: {
          "WR-RESUME": [
            {
              checkpoint_id: "CP-RESUME",
              workflow_run_id: "WR-RESUME",
              project_id: null,
              sequence: 1,
              node_id: "approval",
              state_blob: {
                kind: "human_approval",
                node_id: "approval",
                input_by_port: { input: "hello" },
              },
              created_at: NOW,
            },
          ],
          "WR-EVENT": [
            {
              checkpoint_id: "CP-EVENT",
              workflow_run_id: "WR-EVENT",
              project_id: null,
              sequence: 1,
              node_id: "wait_gate",
              state_blob: {
                kind: "wait_for_event",
                node_id: "wait_gate",
                input_by_port: { input: "hello" },
                expected_event_name: "ticket.approved",
              },
              created_at: NOW,
            },
          ],
        },
      }),
      http.post(`${API_BASE}/workflow-runs/WR-RUNNING/cancel`, () => {
        cancelCalls += 1;
        return HttpResponse.json(envelope(makeRun({ workflow_run_id: "WR-RUNNING", status: "cancelled" })));
      }),
      http.post(`${API_BASE}/workflow-runs/WR-FAILED/retry`, () => {
        retryCalls += 1;
        return HttpResponse.json(envelope(makeRun({ workflow_run_id: "WR-RETRY", status: "queued" })));
      }),
      http.post(`${API_BASE}/workflow-runs/WR-WAIT/approval/approve`, async ({ request }) => {
        const body = (await request.json()) as { runtime_approval_id?: string };
        expect(body.runtime_approval_id).toBe("RA-1");
        approveCalls += 1;
        return HttpResponse.json(envelope(makeRun({ workflow_run_id: "WR-WAIT", status: "running" })));
      }),
      http.post(`${API_BASE}/workflow-runs/WR-RESUME/resume`, () => {
        resumeCalls += 1;
        return HttpResponse.json(envelope(makeRun({ workflow_run_id: "WR-RESUME", status: "queued" })));
      }),
      http.post(`${API_BASE}/workflow-runs/WR-EVENT/resume`, async ({ request }) => {
        eventResumeBody = (await request.json()) as Record<string, unknown>;
        resumeCalls += 1;
        return HttpResponse.json(envelope(makeRun({ workflow_run_id: "WR-EVENT", status: "queued" })));
      }),
    );

    renderAt(<WorkflowDetail />, "/workflows/WF-1", "/workflows/:workflowId");
    await userEvent.click(await screen.findByTestId("tab-runs"));

    expect(await screen.findByTestId("run-open-link-WR-RUNNING")).toHaveAttribute(
      "href",
      "/workflow-runs/WR-RUNNING",
    );
    await userEvent.click(await screen.findByTestId("run-WR-RUNNING"));
    await userEvent.click(await screen.findByTestId("run-cancel"));
    await waitFor(() => expect(cancelCalls).toBe(1));
    await userEvent.click(screen.getByTestId("runs-back"));

    await userEvent.click(await screen.findByTestId("run-WR-FAILED"));
    await userEvent.click(await screen.findByTestId("run-retry"));
    await waitFor(() => expect(retryCalls).toBe(1));
    await userEvent.click(screen.getByTestId("runs-back"));

    await userEvent.click(await screen.findByTestId("run-WR-WAIT"));
    expect(screen.queryByTestId("run-resume")).not.toBeInTheDocument();
    await userEvent.click(await screen.findByTestId("run-approve"));
    await waitFor(() => expect(approveCalls).toBe(1));
    await userEvent.click(screen.getByTestId("runs-back"));

    await userEvent.click(await screen.findByTestId("run-WR-BLOCKED"));
    expect(screen.queryByTestId("run-resume")).not.toBeInTheDocument();
    expect(screen.getByTestId("workflow-run-recovery-warning")).toHaveTextContent(
      "no approved approval record is attached",
    );
    await userEvent.click(screen.getByTestId("runs-back"));

    await userEvent.click(await screen.findByTestId("run-WR-RESUME"));
    await userEvent.click(await screen.findByTestId("run-resume"));
    await waitFor(() => expect(resumeCalls).toBe(1));
    await userEvent.click(screen.getByTestId("runs-back"));

    await userEvent.click(await screen.findByTestId("run-WR-EVENT"));
    expect(await screen.findByTestId("run-waiting-event-chip")).toHaveTextContent("waiting event wait_gate");
    expect(screen.getByTestId("run-waiting-event-note")).toHaveTextContent(
      "This run is paused at an event gate.",
    );
    await userEvent.clear(screen.getByTestId("run-resume-event-name"));
    await userEvent.type(screen.getByTestId("run-resume-event-name"), "ticket.approved");
    await userEvent.clear(screen.getByTestId("run-resume-event-payload"));
    fireEvent.change(screen.getByTestId("run-resume-event-payload"), {
      target: { value: '{"ticket_id":"T-42","approved":true}' },
    });
    await userEvent.click(await screen.findByTestId("run-resume"));
    await waitFor(() => expect(resumeCalls).toBe(2));
    expect(eventResumeBody).toEqual({
      event_name: "ticket.approved",
      event_payload: { ticket_id: "T-42", approved: true },
    });
  });

  it("refreshes exact run-history counts after retry queues a new run", async () => {
    let runs = [
      makeRun({
        workflow_run_id: "WR-FAILED",
        status: "failed",
        trace_id: "trace-failed",
      }),
    ];

    server.use(
      ...detailHandlers({
        runs,
        capabilities: {
          queue_enabled: true,
          supports_async_submit: true,
          supports_cancel: false,
          supports_retry: true,
          supports_resume: false,
          runtime_approvals_enabled: false,
          checkpointing_enabled: true,
          event_backend: "in_process",
        },
      }),
    );
    server.use(
      http.get(`${API_BASE}/workflows/WF-1/runs`, ({ request }) => {
        const limit = new URL(request.url).searchParams.get("limit");
        if (!limit) {
          return HttpResponse.json(envelope(runs));
        }
        return HttpResponse.json({ data: runs, next_cursor: null });
      }),
      http.get(`${API_BASE}/workflows/WF-1/runs/stats`, ({ request }) => {
        const url = new URL(request.url);
        const artifactFilter = url.searchParams.get("artifact_persistence");
        return HttpResponse.json(
          envelope(
            buildWorkflowRunHistoryStats(runs, {
              search: url.searchParams.get("search"),
              artifactFilter:
                artifactFilter === "failed" || artifactFilter === "persisted"
                  ? artifactFilter
                  : null,
            }),
          ),
        );
      }),
      http.post(`${API_BASE}/workflow-runs/WR-FAILED/retry`, () => {
        const retriedRun = makeRun({
          workflow_run_id: "WR-RETRY",
          status: "queued",
          trace_id: "trace-retry",
          completed_at: null,
        });
        runs = [retriedRun, ...runs];
        return HttpResponse.json(envelope(retriedRun));
      }),
    );

    const user = userEvent.setup();
    renderAt(<WorkflowDetail />, "/workflows/WF-1", "/workflows/:workflowId");
    await user.click(await screen.findByTestId("tab-runs"));

    await waitFor(() =>
      expect(screen.getByTestId("runs-triage-summary")).toHaveTextContent(
        "Showing 1 of 1 run",
      ),
    );

    await user.click(screen.getByTestId("run-WR-FAILED"));
    await user.click(await screen.findByTestId("run-retry"));
    expect(await screen.findByTestId("workflow-run-message")).toHaveTextContent(
      "Retry queued as WR-RETRY.",
    );
    await user.click(screen.getByTestId("runs-back"));

    await waitFor(() =>
      expect(screen.getByTestId("runs-triage-summary")).toHaveTextContent(
        "Showing 2 of 2 runs",
      ),
    );
    expect(screen.getByTestId("run-WR-RETRY")).toBeInTheDocument();
  });

  it("surfaces artifact persistence badges in the runs table before operators open a run", async () => {
    server.use(
      ...detailHandlers({
        runs: [
          makeRun({
            workflow_run_id: "WR-UPLOADED",
            status: "completed",
            completed_at: NOW,
            trace_id: "trace-uploaded",
            summary: {
              node_path: ["start", "support_agent"],
              steps: [
                {
                  node_id: "start",
                  node_type: "start",
                  status: "ok",
                  output: "input",
                  tool_calls: [],
                  handoff_target: null,
                  detail: "",
                  duration_ms: 0,
                },
              ],
              artifact_persistence: {
                status: "persisted",
                bucket: "caliber-suite",
                object_count: 3,
                artifact_names: ["kg.json", "report.html"],
              },
            },
          }),
          makeRun({
            workflow_run_id: "WR-UPLOAD-FAILED",
            status: "completed",
            completed_at: NOW,
            trace_id: "trace-upload-failed",
            summary: {
              node_path: ["start", "support_agent"],
              steps: [
                {
                  node_id: "start",
                  node_type: "start",
                  status: "ok",
                  output: "input",
                  tool_calls: [],
                  handoff_target: null,
                  detail: "",
                  duration_ms: 0,
                },
              ],
              artifact_persistence: {
                status: "failed",
                bucket: "caliber-suite",
                object_count: 3,
                artifact_names: ["kg.json", "report.html"],
                error: "RuntimeError: object store offline",
              },
            },
          }),
        ],
      }),
    );

    renderAt(<WorkflowDetail />, "/workflows/WF-1", "/workflows/:workflowId");
    await userEvent.click(await screen.findByTestId("tab-runs"));

    expect(
      await screen.findByTestId("run-artifact-persistence-WR-UPLOADED"),
    ).toHaveTextContent("2 artifacts stored");
    expect(
      screen.getByTestId("run-artifact-persistence-WR-UPLOADED"),
    ).toHaveAttribute("title", expect.stringContaining("caliber-suite"));
    expect(
      screen.getByTestId("run-artifact-persistence-WR-UPLOAD-FAILED"),
    ).toHaveTextContent("Artifact upload failed");
    expect(
      screen.getByTestId("run-artifact-persistence-WR-UPLOAD-FAILED"),
    ).toHaveAttribute("title", expect.stringContaining("object store offline"));

    await userEvent.click(screen.getByTestId("run-WR-UPLOAD-FAILED"));
    expect(await screen.findByTestId("selected-run-artifact-persistence")).toHaveTextContent(
      "Artifact upload failed",
    );
  });

  it("filters run history by artifact upload outcome and searches artifact metadata", async () => {
    server.use(
      ...detailHandlers({
        runs: [
          makeRun({
            workflow_run_id: "WR-UPLOADED",
            status: "completed",
            completed_at: NOW,
            trace_id: "trace-uploaded",
            summary: {
              node_path: ["start", "support_agent"],
              steps: [
                {
                  node_id: "start",
                  node_type: "start",
                  status: "ok",
                  output: "input",
                  tool_calls: [],
                  handoff_target: null,
                  detail: "",
                  duration_ms: 0,
                },
              ],
              artifact_persistence: {
                status: "persisted",
                bucket: "caliber-suite",
                object_count: 3,
                artifact_names: ["kg.json", "report.html"],
              },
            },
          }),
          makeRun({
            workflow_run_id: "WR-UPLOAD-FAILED",
            status: "completed",
            completed_at: NOW,
            trace_id: "trace-upload-failed",
            summary: {
              node_path: ["start", "support_agent"],
              steps: [
                {
                  node_id: "start",
                  node_type: "start",
                  status: "ok",
                  output: "input",
                  tool_calls: [],
                  handoff_target: null,
                  detail: "",
                  duration_ms: 0,
                },
              ],
              artifact_persistence: {
                status: "failed",
                bucket: "caliber-suite",
                object_count: 3,
                artifact_names: ["kg.json", "report.html"],
                error: "RuntimeError: object store offline",
              },
            },
          }),
        ],
      }),
    );

    renderAt(<WorkflowDetail />, "/workflows/WF-1", "/workflows/:workflowId");
    await userEvent.click(await screen.findByTestId("tab-runs"));

    await waitFor(() =>
      expect(screen.getByTestId("runs-triage-summary")).toHaveTextContent(
        "Showing 2 of 2 runs · 1 upload failure across all runs · 1 stored artifact run across all runs",
      ),
    );

    await userEvent.click(screen.getByTestId("runs-filter-upload_failed"));
    await waitFor(() =>
      expect(screen.getByTestId("runs-triage-summary")).toHaveTextContent(
        "Showing 1 of 1 matching run",
      ),
    );
    expect(screen.getByTestId("run-WR-UPLOAD-FAILED")).toBeInTheDocument();
    expect(screen.queryByTestId("run-WR-UPLOADED")).not.toBeInTheDocument();

    const search = screen.getByRole("searchbox", { name: "Search runs" });
    await userEvent.type(search, "trace-uploaded");
    expect(await screen.findByTestId("runs-filtered-empty")).toHaveTextContent(
      "No runs match “trace-uploaded” within the artifact upload failed view.",
    );

    await userEvent.clear(search);
    await userEvent.click(screen.getByTestId("runs-filter-all"));
    await userEvent.type(search, "object store offline");
    expect(await screen.findByTestId("run-WR-UPLOAD-FAILED")).toBeInTheDocument();
    expect(screen.queryByTestId("run-WR-UPLOADED")).not.toBeInTheDocument();
  });

  it("loads paged run history and uses server-backed filters to find older runs", async () => {
    const recentRuns = [
      makeRun({ workflow_run_id: "WR-RECENT", trace_id: "trace-recent", completed_at: NOW }),
    ];
    const pagedNewest = makeRun({
      workflow_run_id: "WR-PAGED-1",
      trace_id: "trace-paged-1",
      completed_at: NOW,
    });
    const pagedNext = makeRun({
      workflow_run_id: "WR-PAGED-2",
      trace_id: "trace-paged-2",
      completed_at: NOW,
    });
    const searchOnlyRun = makeRun({
      workflow_run_id: "WR-SEARCH-OLD",
      trace_id: "trace-older-failed",
      completed_at: NOW,
      summary: {
        node_path: ["start", "support_agent"],
        artifact_persistence: {
          status: "failed",
          bucket: "caliber-suite",
          object_count: 1,
          artifact_names: ["trace-older.json"],
          error: "object store offline",
        },
      },
    });
    const queryLog: Array<{ search: string | null; filter: string | null; cursor: string | null }> = [];

    server.use(...detailHandlers({ runs: recentRuns }));
    server.use(
      http.get(`${API_BASE}/workflows/WF-1/runs`, ({ request }) => {
        const url = new URL(request.url);
        const search = url.searchParams.get("search");
        const filter = url.searchParams.get("artifact_persistence");
        const cursor = url.searchParams.get("cursor");
        const limit = url.searchParams.get("limit");
        if (!limit) {
          return HttpResponse.json(envelope(recentRuns));
        }
        queryLog.push({ search, filter, cursor });
        if (filter === "failed") {
          return HttpResponse.json({ data: [searchOnlyRun], next_cursor: null });
        }
        if (cursor === "2") {
          return HttpResponse.json({ data: [searchOnlyRun], next_cursor: null });
        }
        if (cursor === "1") {
          return HttpResponse.json({ data: [pagedNext], next_cursor: "2" });
        }
        return HttpResponse.json({ data: [pagedNewest], next_cursor: "1" });
      }),
      http.get(`${API_BASE}/workflows/WF-1/runs/stats`, ({ request }) => {
        const url = new URL(request.url);
        const search = url.searchParams.get("search");
        const filter = url.searchParams.get("artifact_persistence");
        return HttpResponse.json(
          envelope({
            workflow_id: "WF-1",
            total_runs: 3,
            matching_runs:
              filter === "failed"
                ? 1
                : search?.trim().toLowerCase() === "object store offline"
                  ? 1
                  : 3,
            waiting_event_runs: 0,
            artifact_persistence: {
              failed: 1,
              persisted: 0,
            },
          }),
        );
      }),
    );

    renderAt(<WorkflowDetail />, "/workflows/WF-1", "/workflows/:workflowId");
    await userEvent.click(await screen.findByTestId("tab-runs"));

    expect(await screen.findByTestId("run-WR-PAGED-1")).toBeInTheDocument();
    await waitFor(() =>
      expect(screen.getByTestId("runs-triage-summary")).toHaveTextContent(
        "Showing 1 of 3 runs",
      ),
    );
    expect(screen.getByTestId("runs-triage-summary")).toHaveTextContent("More runs available");

    await userEvent.click(screen.getByTestId("runs-load-more"));
    expect(await screen.findByTestId("run-WR-PAGED-2")).toBeInTheDocument();

    await userEvent.click(screen.getByTestId("runs-filter-upload_failed"));
    const search = screen.getByRole("searchbox", { name: "Search runs" });
    await userEvent.type(search, "object store offline");

    expect(await screen.findByTestId("run-WR-SEARCH-OLD")).toBeInTheDocument();
    expect(screen.getByTestId("runs-triage-summary")).toHaveTextContent(
      "Showing 1 of 1 matching run",
    );
    expect(queryLog).toEqual(
      expect.arrayContaining([
        expect.objectContaining({ cursor: null, filter: null, search: null }),
        expect.objectContaining({ cursor: "1", filter: null, search: null }),
        expect.objectContaining({ filter: "failed" }),
        expect.objectContaining({ filter: "failed", search: "object store offline" }),
      ]),
    );
  });

  it("falls back to the recent run index when full-history paging is unavailable", async () => {
    const recentRuns = [
      makeRun({
        workflow_run_id: "WR-RECENT-FAILED",
        trace_id: "trace-recent-failed",
        completed_at: NOW,
        summary: {
          node_path: ["start", "support_agent"],
          artifact_persistence: {
            status: "failed",
            bucket: "caliber-suite",
            object_count: 1,
            artifact_names: ["recent-failed.json"],
            error: "object store offline",
          },
        },
      }),
      makeRun({
        workflow_run_id: "WR-RECENT-STORED",
        trace_id: "trace-recent-stored",
        completed_at: NOW,
        summary: {
          node_path: ["start", "support_agent"],
          artifact_persistence: {
            status: "persisted",
            bucket: "caliber-suite",
            object_count: 2,
            artifact_names: ["recent-stored.json"],
          },
        },
      }),
    ];

    server.use(...detailHandlers({ runs: recentRuns }));
    server.use(
      http.get(`${API_BASE}/workflows/WF-1/runs`, ({ request }) => {
        const limit = new URL(request.url).searchParams.get("limit");
        if (!limit) {
          return HttpResponse.json(envelope(recentRuns));
        }
        return HttpResponse.json(
          { detail: "history paging unavailable" },
          { status: 503 },
        );
      }),
      http.get(`${API_BASE}/workflows/WF-1/runs/stats`, () =>
        HttpResponse.json(
          envelope({
            workflow_id: "WF-1",
            total_runs: 5,
            matching_runs: 5,
            waiting_event_runs: 0,
            artifact_persistence: {
              failed: 2,
              persisted: 3,
            },
          }),
        )),
    );

    renderAt(<WorkflowDetail />, "/workflows/WF-1", "/workflows/:workflowId");
    await userEvent.click(await screen.findByTestId("tab-runs"));

    expect(await screen.findByTestId("runs-query-fallback")).toHaveTextContent(
      "Full-history run search is temporarily unavailable. Showing the recent run index instead.",
    );
    expect(screen.queryByTestId("runs-stats-fallback")).not.toBeInTheDocument();
    await waitFor(() =>
      expect(screen.getByTestId("runs-triage-summary")).toHaveTextContent(
        "Showing 2 of 5 runs · 2 upload failures across all runs · 3 stored artifact runs across all runs · Full-history search unavailable; showing the recent run index",
      ),
    );
    expect(screen.getByTestId("run-WR-RECENT-FAILED")).toBeInTheDocument();
    expect(screen.getByTestId("run-WR-RECENT-STORED")).toBeInTheDocument();
  });

  it("surfaces recent-history estimates when exact run totals are unavailable", async () => {
    const recentRuns = [
      makeRun({
        workflow_run_id: "WR-HISTORY-FAILED",
        trace_id: "trace-history-failed",
        completed_at: NOW,
        summary: {
          node_path: ["start", "support_agent"],
          artifact_persistence: {
            status: "failed",
            bucket: "caliber-suite",
            object_count: 1,
            artifact_names: ["history-failed.json"],
            error: "object store offline",
          },
        },
      }),
      makeRun({
        workflow_run_id: "WR-HISTORY-STORED",
        trace_id: "trace-history-stored",
        completed_at: NOW,
        summary: {
          node_path: ["start", "support_agent"],
          artifact_persistence: {
            status: "persisted",
            bucket: "caliber-suite",
            object_count: 2,
            artifact_names: ["history-stored.json"],
          },
        },
      }),
    ];

    server.use(...detailHandlers({ runs: recentRuns }));
    server.use(
      http.get(`${API_BASE}/workflows/WF-1/runs`, ({ request }) => {
        const limit = new URL(request.url).searchParams.get("limit");
        if (!limit) {
          return HttpResponse.json(envelope(recentRuns));
        }
        return HttpResponse.json({ data: recentRuns, next_cursor: null });
      }),
      http.get(`${API_BASE}/workflows/WF-1/runs/stats`, () =>
        HttpResponse.json(
          { detail: "run totals unavailable" },
          { status: 503 },
        )),
    );

    renderAt(<WorkflowDetail />, "/workflows/WF-1", "/workflows/:workflowId");
    await userEvent.click(await screen.findByTestId("tab-runs"));

    expect(await screen.findByTestId("runs-stats-fallback")).toHaveTextContent(
      "Exact run totals are temporarily unavailable. Using recent history estimates instead.",
    );
    expect(screen.queryByTestId("runs-query-fallback")).not.toBeInTheDocument();
    await waitFor(() =>
      expect(screen.getByTestId("runs-triage-summary")).toHaveTextContent(
        "Showing 2 recent runs · 1 upload failure in recent history · 1 stored artifact run in recent history · Exact totals unavailable; using recent history estimates",
      ),
    );
    expect(screen.getByTestId("run-WR-HISTORY-FAILED")).toBeInTheDocument();
    expect(screen.getByTestId("run-WR-HISTORY-STORED")).toBeInTheDocument();
  });

  it("shows scheduled wait messaging for wait_until runs and resumes without event payload", async () => {
    let resumeCalls = 0;
    let resumeBody: Record<string, unknown> | null = null;
    const baseVersion = makeVersion({ status: "published" });
    const waitUntilManifest = {
      ...baseVersion.manifest,
      nodes: {
        ...baseVersion.manifest.nodes,
        wait_gate: {
          id: "wait_gate",
          type: "wait_until",
          wait_until: "2026-02-01T10:00:00",
          timezone: "UTC",
          inputs: { input: { type: "string" } },
          outputs: { output: { type: "string" } },
        },
      },
    };
    server.use(
      ...detailHandlers({
        versions: [{ ...baseVersion, manifest: waitUntilManifest }],
        capabilities: {
          queue_enabled: true,
          supports_async_submit: true,
          supports_cancel: false,
          supports_retry: false,
          supports_resume: true,
          runtime_approvals_enabled: false,
          checkpointing_enabled: true,
          event_backend: "in_process",
        },
        runs: [
          makeRun({
            workflow_run_id: "WR-WAIT-UNTIL",
            status: "waiting_event",
            trace_id: "trace-wait-until",
            current_node_id: "wait_gate",
          }),
        ],
        runCheckpointsById: {
          "WR-WAIT-UNTIL": [
            {
              checkpoint_id: "CP-WAIT-UNTIL",
              workflow_run_id: "WR-WAIT-UNTIL",
              project_id: null,
              sequence: 1,
              node_id: "wait_gate",
              state_blob: {
                kind: "wait_until",
                node_id: "wait_gate",
                wait_until: "2026-02-01T10:00:00",
                timezone: "UTC",
                input_by_port: { input: "hello" },
              },
              created_at: NOW,
            },
          ],
        },
      }),
      http.post(`${API_BASE}/workflow-runs/WR-WAIT-UNTIL/resume`, async ({ request }) => {
        resumeBody = (await request.json()) as Record<string, unknown>;
        resumeCalls += 1;
        return HttpResponse.json(
          envelope(makeRun({ workflow_run_id: "WR-WAIT-UNTIL", status: "queued" })),
        );
      }),
    );

    renderAt(<WorkflowDetail />, "/workflows/WF-1", "/workflows/:workflowId");
    await userEvent.click(await screen.findByTestId("tab-runs"));
    await userEvent.click(await screen.findByTestId("run-WR-WAIT-UNTIL"));

    expect(await screen.findByTestId("run-waiting-event-chip")).toHaveTextContent(
      "scheduled wait wait_gate",
    );
    expect(screen.getByTestId("run-wait-until-note")).toHaveTextContent(
      "paused until 2026-02-01T10:00:00 (UTC)",
    );
    expect(screen.queryByTestId("run-resume-event-name")).not.toBeInTheDocument();
    expect(screen.queryByTestId("run-resume-event-payload")).not.toBeInTheDocument();

    await userEvent.click(await screen.findByTestId("run-resume"));

    await waitFor(() => expect(resumeCalls).toBe(1));
    expect(resumeBody).toEqual({});
  });

  it("shows queue-disabled scheduled wait guidance when wait_until runs cannot resume", async () => {
    const baseVersion = makeVersion({ status: "published" });
    const waitUntilManifest = {
      ...baseVersion.manifest,
      nodes: {
        ...baseVersion.manifest.nodes,
        wait_gate: {
          id: "wait_gate",
          type: "wait_until",
          wait_until: "2026-02-01T10:00:00",
          timezone: "UTC",
          inputs: { input: { type: "string" } },
          outputs: { output: { type: "string" } },
        },
      },
    };
    server.use(
      ...detailHandlers({
        versions: [{ ...baseVersion, manifest: waitUntilManifest }],
        capabilities: {
          queue_enabled: false,
          supports_async_submit: true,
          supports_cancel: false,
          supports_retry: false,
          supports_resume: true,
          runtime_approvals_enabled: false,
          checkpointing_enabled: true,
          event_backend: "in_process",
        },
        runs: [
          makeRun({
            workflow_run_id: "WR-WAIT-UNTIL-QUEUE-DISABLED",
            status: "waiting_event",
            trace_id: "trace-wait-until-queue-disabled",
            current_node_id: "wait_gate",
          }),
        ],
      }),
    );

    renderAt(<WorkflowDetail />, "/workflows/WF-1", "/workflows/:workflowId");
    await userEvent.click(await screen.findByTestId("tab-runs"));
    await userEvent.click(await screen.findByTestId("run-WR-WAIT-UNTIL-QUEUE-DISABLED"));

    expect(await screen.findByTestId("run-wait-until-note")).toHaveTextContent(
      "Automatic and manual resume are unavailable until the workflow run queue is enabled for this deployment.",
    );
    expect(screen.queryByTestId("run-resume")).not.toBeInTheDocument();
  });

  it("falls back to checkpoint wait_until state when the run version is unavailable", async () => {
    let resumeCalls = 0;
    let resumeBody: Record<string, unknown> | null = null;
    server.use(
      ...detailHandlers({
        capabilities: {
          queue_enabled: true,
          supports_async_submit: true,
          supports_cancel: false,
          supports_retry: false,
          supports_resume: true,
          runtime_approvals_enabled: false,
          checkpointing_enabled: true,
          event_backend: "database",
        },
        runs: [
          makeRun({
            workflow_run_id: "WR-WAIT-CP",
            workflow_version_id: "WFV-MISSING",
            status: "waiting_event",
            trace_id: "trace-wait-cp",
            current_node_id: "wait_gate",
            summary: {
              output: "",
              node_path: ["start", "wait_gate"],
              steps: [],
              resume_checkpoint_id: "CP-2",
            },
          }),
        ],
        runCheckpointsById: {
          "WR-WAIT-CP": [
            {
              checkpoint_id: "CP-2",
              workflow_run_id: "WR-WAIT-CP",
              project_id: null,
              sequence: 2,
              node_id: "wait_gate",
              state_blob: {
                kind: "wait_until",
                node_id: "wait_gate",
                input_by_port: { input: "hello" },
                wait_until: "2026-01-02T12:00:00",
                timezone: "UTC",
              },
              created_at: NOW,
            },
          ],
        },
      }),
      http.post(`${API_BASE}/workflow-runs/WR-WAIT-CP/resume`, async ({ request }) => {
        resumeBody = (await request.json()) as Record<string, unknown>;
        resumeCalls += 1;
        return HttpResponse.json(
          envelope(makeRun({ workflow_run_id: "WR-WAIT-CP", status: "queued" })),
        );
      }),
    );

    renderAt(<WorkflowDetail />, "/workflows/WF-1", "/workflows/:workflowId");
    await userEvent.click(await screen.findByTestId("tab-runs"));
    await userEvent.click(await screen.findByTestId("run-WR-WAIT-CP"));

    expect(await screen.findByTestId("run-manifest-fallback-notice")).toHaveTextContent(
      "graph reconstructed from recorded run history and checkpoints",
    );
    expect(screen.getByTestId("run-manifest-fallback-notice")).toHaveTextContent(
      "rely on the recovery and checkpoint panels for authoritative resume state",
    );
    expect(await screen.findByTestId("workflow-run-debugger-empty")).toHaveTextContent(
      "No recorded step details yet",
    );
    expect(screen.queryByTestId("run-version-missing")).not.toBeInTheDocument();
    expect(screen.getByTestId("run-waiting-event-chip")).toHaveTextContent(
      "scheduled wait wait_gate",
    );
    expect(screen.getByTestId("run-wait-until-note")).toHaveTextContent(
      "paused until 2026-01-02T12:00:00 (UTC)",
    );
    expect(screen.queryByTestId("run-resume-event-name")).not.toBeInTheDocument();
    expect(screen.queryByTestId("run-resume-event-payload")).not.toBeInTheDocument();

    await userEvent.click(await screen.findByTestId("run-resume"));

    await waitFor(() => expect(resumeCalls).toBe(1));
    expect(resumeBody).toEqual({});
  });

  it("guides wait_for_event runs toward event matching when manual resume is disabled in run detail", async () => {
    let resumeByEventBody: Record<string, unknown> | null = null;
    const baseVersion = makeVersion({ status: "published" });
    const eventManifest = {
      ...baseVersion.manifest,
      nodes: {
        ...baseVersion.manifest.nodes,
        wait_gate: {
          id: "wait_gate",
          type: "wait_for_event",
          event_name: "ticket.approved",
          inputs: { input: { type: "string" } },
          outputs: { output: { type: "string" } },
        },
      },
    };
    const waitingRun = makeRun({
      workflow_run_id: "WR-EVENT-NO-RESUME",
      status: "waiting_event",
      trace_id: "trace-event-no-resume",
      current_node_id: "wait_gate",
      summary: {
        output: "",
        node_path: ["start", "wait_gate"],
        steps: [],
        resume_checkpoint_id: "CP-EVENT-NO-RESUME",
      },
    });
    const queuedRun = {
      ...waitingRun,
      status: "queued",
      summary: {
        ...(waitingRun.summary ?? {}),
        status: "queued",
      },
    };
    const runRows = [waitingRun];

    server.use(
      ...detailHandlers({
        versions: [{ ...baseVersion, manifest: eventManifest }],
        capabilities: {
          queue_enabled: true,
          supports_async_submit: true,
          supports_cancel: false,
          supports_retry: false,
          supports_resume: false,
          runtime_approvals_enabled: false,
          checkpointing_enabled: true,
          event_backend: "database",
        },
        runs: runRows,
        runCheckpointsById: {
          "WR-EVENT-NO-RESUME": [
            {
              checkpoint_id: "CP-EVENT-NO-RESUME",
              workflow_run_id: "WR-EVENT-NO-RESUME",
              project_id: null,
              sequence: 1,
              node_id: "wait_gate",
              state_blob: {
                kind: "wait_for_event",
                node_id: "wait_gate",
                input_by_port: { input: "hello" },
                expected_event_name: "ticket.approved",
              },
              created_at: NOW,
            },
          ],
        },
      }),
      http.post(`${API_BASE}/workflow-runs/resume-by-event`, async ({ request }) => {
        resumeByEventBody = (await request.json()) as Record<string, unknown>;
        runRows[0] = queuedRun;
        return HttpResponse.json(envelope(queuedRun));
      }),
    );

    renderAt(<WorkflowDetail />, "/workflows/WF-1", "/workflows/:workflowId");
    await userEvent.click(await screen.findByTestId("tab-runs"));
    await userEvent.click(await screen.findByTestId("run-WR-EVENT-NO-RESUME"));

    expect(await screen.findByTestId("run-resume-capability-note")).toHaveTextContent(
      "Use the event-match controls below to resume this event gate.",
    );
    expect(screen.queryByTestId("run-resume")).not.toBeInTheDocument();
    expect(screen.getByTestId("run-resume-event-name")).toHaveValue("ticket.approved");
    await userEvent.clear(screen.getByTestId("run-resume-event-payload"));
    fireEvent.change(screen.getByTestId("run-resume-event-payload"), {
      target: { value: '{"ticket_id":"T-42","approved":true}' },
    });
    await userEvent.click(screen.getByTestId("run-resume-by-event"));

    await waitFor(() =>
      expect(screen.getByText(/Matched event ticket\.approved to run WR-EVENT-NO-RESUME/)).toBeInTheDocument(),
    );
    expect(resumeByEventBody).toEqual({
      workflow_id: "WF-1",
      event_name: "ticket.approved",
      event_payload: { ticket_id: "T-42", approved: true },
    });
  });

  it("disables event matching in run detail when checkpoint persistence is disabled", async () => {
    const baseVersion = makeVersion({ status: "published" });
    const eventManifest = {
      ...baseVersion.manifest,
      nodes: {
        ...baseVersion.manifest.nodes,
        wait_gate: {
          id: "wait_gate",
          type: "wait_for_event",
          event_name: "ticket.approved",
          inputs: { input: { type: "string" } },
          outputs: { output: { type: "string" } },
        },
      },
    };
    const waitingRun = makeRun({
      workflow_run_id: "WR-EVENT-NO-CHECKPOINTING",
      status: "waiting_event",
      trace_id: "trace-event-no-checkpointing",
      current_node_id: "wait_gate",
      summary: {
        output: "",
        node_path: ["start", "wait_gate"],
        steps: [],
        resume_checkpoint_id: "CP-EVENT-NO-CHECKPOINTING",
      },
    });

    server.use(
      ...detailHandlers({
        versions: [{ ...baseVersion, manifest: eventManifest }],
        capabilities: {
          queue_enabled: true,
          supports_async_submit: true,
          supports_cancel: false,
          supports_retry: false,
          supports_resume: false,
          runtime_approvals_enabled: false,
          checkpointing_enabled: false,
          event_backend: "database",
        },
        runs: [waitingRun],
        runCheckpointsById: {
          "WR-EVENT-NO-CHECKPOINTING": [
            {
              checkpoint_id: "CP-EVENT-NO-CHECKPOINTING",
              workflow_run_id: "WR-EVENT-NO-CHECKPOINTING",
              project_id: null,
              sequence: 1,
              node_id: "wait_gate",
              state_blob: {
                kind: "wait_for_event",
                node_id: "wait_gate",
                input_by_port: { input: "hello" },
                expected_event_name: "ticket.approved",
              },
              created_at: NOW,
            },
          ],
        },
      }),
    );

    renderAt(<WorkflowDetail />, "/workflows/WF-1", "/workflows/:workflowId");
    await userEvent.click(await screen.findByTestId("tab-runs"));
    await userEvent.click(await screen.findByTestId("run-WR-EVENT-NO-CHECKPOINTING"));

    expect(await screen.findByTestId("run-resume-capability-note")).toHaveTextContent(
      "event-match resume are unavailable until checkpoint persistence is enabled",
    );
    expect(screen.getByTestId("run-resume-by-event")).toBeDisabled();
    expect(screen.queryByTestId("run-resume")).not.toBeInTheDocument();
  });

  it("prefills event-gate resume details from checkpoint state when the run version is unavailable", async () => {
    let resumeCalls = 0;
    let resumeBody: Record<string, unknown> | null = null;
    server.use(
      ...detailHandlers({
        capabilities: {
          queue_enabled: true,
          supports_async_submit: true,
          supports_cancel: false,
          supports_retry: false,
          supports_resume: true,
          runtime_approvals_enabled: false,
          checkpointing_enabled: true,
          event_backend: "database",
        },
        runs: [
          makeRun({
            workflow_run_id: "WR-EVENT-CP",
            workflow_version_id: "WFV-MISSING",
            status: "waiting_event",
            trace_id: "trace-event-cp",
            current_node_id: "wait_gate",
            summary: {
              output: "",
              node_path: ["start", "wait_gate"],
              steps: [],
              resume_checkpoint_id: "CP-1",
            },
          }),
        ],
        runCheckpointsById: {
          "WR-EVENT-CP": [
            {
              checkpoint_id: "CP-1",
              workflow_run_id: "WR-EVENT-CP",
              project_id: null,
              sequence: 1,
              node_id: "wait_gate",
              state_blob: {
                kind: "wait_for_event",
                node_id: "wait_gate",
                input_by_port: { input: "hello" },
                expected_event_name: "ticket.approved",
              },
              created_at: NOW,
            },
          ],
        },
      }),
      http.post(`${API_BASE}/workflow-runs/WR-EVENT-CP/resume`, async ({ request }) => {
        resumeBody = (await request.json()) as Record<string, unknown>;
        resumeCalls += 1;
        return HttpResponse.json(
          envelope(makeRun({ workflow_run_id: "WR-EVENT-CP", status: "queued" })),
        );
      }),
    );

    renderAt(<WorkflowDetail />, "/workflows/WF-1", "/workflows/:workflowId");
    await userEvent.click(await screen.findByTestId("tab-runs"));
    await userEvent.click(await screen.findByTestId("run-WR-EVENT-CP"));

    expect(await screen.findByTestId("run-manifest-fallback-notice")).toHaveTextContent(
      "graph reconstructed from recorded run history and checkpoints",
    );
    expect(screen.getByTestId("run-manifest-fallback-notice")).toHaveTextContent(
      "rely on the recovery and checkpoint panels for authoritative resume state",
    );
    expect(await screen.findByTestId("workflow-run-debugger-empty")).toHaveTextContent(
      "No recorded step details yet",
    );
    expect(screen.queryByTestId("run-version-missing")).not.toBeInTheDocument();
    expect(screen.getByTestId("run-waiting-event-note")).toHaveTextContent(
      "Resume it after ticket.approved has been handled.",
    );
    expect(screen.getByTestId("run-resume-event-name")).toHaveValue("ticket.approved");
    expect(screen.getByTestId("run-resume-event-payload")).toHaveValue(
      '{\n  "source": "manual_resume",\n  "node_id": "wait_gate"\n}',
    );

    await userEvent.click(await screen.findByTestId("run-resume"));

    await waitFor(() => expect(resumeCalls).toBe(1));
    expect(resumeBody).toEqual({
      event_name: "ticket.approved",
      event_payload: {
        source: "manual_resume",
        node_id: "wait_gate",
      },
    });
  });

  it("matches waiting runs by external event from workflow detail", async () => {
    let resumeByEventBody: Record<string, unknown> | null = null;
    const eventVersion = makeVersion({
      status: "published",
      manifest: {
        ...makeVersion({ status: "published" }).manifest,
        nodes: {
          ...makeVersion({ status: "published" }).manifest.nodes,
          wait_gate: {
            id: "wait_gate",
            type: "wait_for_event",
            event_name: "ticket.approved",
            inputs: { input: { type: "string" } },
            outputs: { output: { type: "string" } },
          },
        },
      },
    });
    const waitingRun = makeRun({
      workflow_run_id: "WR-EVENT-MATCH",
      status: "waiting_event",
      trace_id: "trace-event-match",
      current_node_id: "wait_gate",
      summary: {
        output: "",
        node_path: ["start", "wait_gate"],
        steps: [],
        resume_checkpoint_id: "CP-EVENT-MATCH",
      },
    });
    const queuedRun = {
      ...waitingRun,
      status: "queued",
      summary: {
        ...(waitingRun.summary ?? {}),
        status: "queued",
      },
    };
    const runRows = [waitingRun];
    server.use(
      ...detailHandlers({
        versions: [eventVersion],
        capabilities: {
          queue_enabled: true,
          supports_async_submit: true,
          supports_cancel: false,
          supports_retry: false,
          supports_resume: true,
          runtime_approvals_enabled: false,
          checkpointing_enabled: true,
          event_backend: "database",
        },
        runs: runRows,
        runCheckpointsById: {
          "WR-EVENT-MATCH": [
            {
              checkpoint_id: "CP-EVENT-MATCH",
              workflow_run_id: "WR-EVENT-MATCH",
              project_id: null,
              sequence: 1,
              node_id: "wait_gate",
              state_blob: {
                kind: "wait_for_event",
                node_id: "wait_gate",
                input_by_port: { input: "hello" },
                expected_event_name: "ticket.approved",
                correlation_key: "ticket_id",
                correlation_value: "T-42",
              },
              created_at: NOW,
            },
          ],
        },
      }),
      http.post(`${API_BASE}/workflow-runs/resume-by-event`, async ({ request }) => {
        resumeByEventBody = (await request.json()) as Record<string, unknown>;
        runRows[0] = queuedRun;
        return HttpResponse.json(envelope(queuedRun));
      }),
    );

    renderAt(<WorkflowDetail />, "/workflows/WF-1", "/workflows/:workflowId");
    await userEvent.click(await screen.findByTestId("tab-runs"));
    await userEvent.click(await screen.findByTestId("run-WR-EVENT-MATCH"));

    expect(await screen.findByTestId("run-waiting-event-note")).toHaveTextContent(
      "This run is paused at an event gate.",
    );
    expect(screen.getByTestId("run-resume-event-name")).toHaveValue("ticket.approved");
    await userEvent.clear(screen.getByTestId("run-resume-event-payload"));
    fireEvent.change(screen.getByTestId("run-resume-event-payload"), {
      target: { value: '{"ticket_id":"T-42","approved":true}' },
    });

    await userEvent.click(await screen.findByTestId("run-resume-by-event"));

    await waitFor(() =>
      expect(screen.getByText(/Matched event ticket\.approved to run WR-EVENT-MATCH/)).toBeInTheDocument(),
    );
    expect(resumeByEventBody).toEqual({
      workflow_id: "WF-1",
      event_name: "ticket.approved",
      event_payload: { ticket_id: "T-42", approved: true },
    });
  });

  it("turns stale workflow-wide event matching failures into recovery guidance in detail", async () => {
    const eventVersion = makeVersion({
      status: "published",
      manifest: {
        ...makeVersion({ status: "published" }).manifest,
        nodes: {
          ...makeVersion({ status: "published" }).manifest.nodes,
          wait_gate: {
            id: "wait_gate",
            type: "wait_for_event",
            event_name: "ticket.approved",
            inputs: { input: { type: "string" } },
            outputs: { output: { type: "string" } },
          },
        },
      },
    });
    const waitingRun = makeRun({
      workflow_run_id: "WR-EVENT-STALE",
      status: "waiting_event",
      trace_id: "trace-event-stale",
      current_node_id: "wait_gate",
      summary: {
        output: "",
        node_path: ["start", "wait_gate"],
        steps: [],
        resume_checkpoint_id: "CP-EVENT-STALE",
      },
    });

    server.use(
      ...detailHandlers({
        versions: [eventVersion],
        capabilities: {
          queue_enabled: true,
          supports_async_submit: true,
          supports_cancel: false,
          supports_retry: false,
          supports_resume: true,
          runtime_approvals_enabled: false,
          checkpointing_enabled: true,
          event_backend: "database",
        },
        runs: [waitingRun],
        runCheckpointsById: {
          "WR-EVENT-STALE": [
            {
              checkpoint_id: "CP-EVENT-STALE",
              workflow_run_id: "WR-EVENT-STALE",
              project_id: null,
              sequence: 1,
              node_id: "wait_gate",
              state_blob: {
                kind: "wait_for_event",
                node_id: "wait_gate",
                input_by_port: { input: "hello" },
                expected_event_name: "ticket.approved",
                correlation_key: "ticket_id",
                correlation_value: "T-42",
              },
              created_at: NOW,
            },
          ],
        },
      }),
      http.post(`${API_BASE}/workflow-runs/resume-by-event`, () =>
        HttpResponse.json(
          {
            detail:
              "event 'ticket.approved' reached waiting workflow runs with resume checkpoints missing correlation_value for their configured correlation_key: WR-EVENT-STALE",
          },
          { status: 409 },
        ),
      ),
    );

    renderAt(<WorkflowDetail />, "/workflows/WF-1", "/workflows/:workflowId");
    await userEvent.click(await screen.findByTestId("tab-runs"));
    await userEvent.click(await screen.findByTestId("run-WR-EVENT-STALE"));

    await userEvent.clear(screen.getByTestId("run-resume-event-payload"));
    fireEvent.change(screen.getByTestId("run-resume-event-payload"), {
      target: { value: '{"ticket_id":"T-42","approved":true}' },
    });
    await userEvent.click(await screen.findByTestId("run-resume-by-event"));

    expect(await screen.findByTestId("workflow-run-message")).toHaveTextContent(
      "External event resume failed because no safe waiting run could be selected for this event.",
    );
    expect(screen.getByTestId("workflow-run-message")).toHaveTextContent(
      "Inspect the recovery, checkpoint, and lineage panels, then resume the target run directly or add the required event correlation before retrying.",
    );
    expect(screen.getByTestId("workflow-run-message")).toHaveTextContent(
      "Latest backend detail: event 'ticket.approved' reached waiting workflow runs with resume checkpoints missing correlation_value for their configured correlation_key: WR-EVENT-STALE",
    );
  });

  it("requires the checkpoint correlation field before matching a wait_for_event run in detail", async () => {
    const eventVersion = makeVersion({
      status: "published",
      manifest: {
        ...makeVersion({ status: "published" }).manifest,
        nodes: {
          ...makeVersion({ status: "published" }).manifest.nodes,
          wait_gate: {
            id: "wait_gate",
            type: "wait_for_event",
            event_name: "ticket.approved",
            correlation_key: "ticket_id",
            inputs: { input: { type: "string" } },
            outputs: { output: { type: "string" } },
          },
        },
      },
    });
    const waitingRun = makeRun({
      workflow_run_id: "WR-EVENT-CORRELATION",
      status: "waiting_event",
      trace_id: "trace-event-correlation",
      current_node_id: "wait_gate",
      summary: {
        output: "",
        node_path: ["start", "wait_gate"],
        steps: [],
        resume_checkpoint_id: "CP-EVENT-CORRELATION",
      },
    });

    server.use(
      ...detailHandlers({
        versions: [eventVersion],
        capabilities: {
          queue_enabled: true,
          supports_async_submit: true,
          supports_cancel: false,
          supports_retry: false,
          supports_resume: true,
          runtime_approvals_enabled: false,
          checkpointing_enabled: true,
          event_backend: "database",
        },
        runs: [waitingRun],
        runCheckpointsById: {
          "WR-EVENT-CORRELATION": [
            {
              checkpoint_id: "CP-EVENT-CORRELATION",
              workflow_run_id: "WR-EVENT-CORRELATION",
              project_id: null,
              sequence: 1,
              node_id: "wait_gate",
              state_blob: {
                kind: "wait_for_event",
                node_id: "wait_gate",
                input_by_port: { input: "hello" },
                expected_event_name: "ticket.approved",
                correlation_key: "ticket_id",
                correlation_value: "T-42",
              },
              created_at: NOW,
            },
          ],
        },
      }),
    );

    renderAt(<WorkflowDetail />, "/workflows/WF-1", "/workflows/:workflowId");
    await userEvent.click(await screen.findByTestId("tab-runs"));
    await userEvent.click(await screen.findByTestId("run-WR-EVENT-CORRELATION"));

    expect(
      (await screen.findByTestId("run-resume-event-payload") as HTMLTextAreaElement).value,
    ).toContain('"ticket_id": "T-42"');

    fireEvent.change(screen.getByTestId("run-resume-event-payload"), {
      target: { value: '{"ticket_id":"T-99","approved":true}' },
    });

    expect(await screen.findByTestId("run-resume-by-event-capability-note")).toHaveTextContent(
      "requires correlation field ticket_id=T-42",
    );
    expect(screen.getByTestId("run-resume-by-event")).toBeDisabled();
    expect(screen.getByTestId("run-resume")).toBeEnabled();
  });

  it("disables workflow-wide event matching in detail when the checkpoint never captured the required correlation value", async () => {
    const eventVersion = makeVersion({
      status: "published",
      manifest: {
        ...makeVersion({ status: "published" }).manifest,
        nodes: {
          ...makeVersion({ status: "published" }).manifest.nodes,
          wait_gate: {
            id: "wait_gate",
            type: "wait_for_event",
            event_name: "ticket.approved",
            correlation_key: "ticket_id",
            inputs: { input: { type: "string" } },
            outputs: { output: { type: "string" } },
          },
        },
      },
    });
    const waitingRun = makeRun({
      workflow_run_id: "WR-EVENT-CORRELATION-MISSING",
      status: "waiting_event",
      trace_id: "trace-event-correlation-missing",
      current_node_id: "wait_gate",
      summary: {
        output: "",
        node_path: ["start", "wait_gate"],
        steps: [],
        resume_checkpoint_id: "CP-EVENT-CORRELATION-MISSING",
      },
    });

    server.use(
      ...detailHandlers({
        versions: [eventVersion],
        capabilities: {
          queue_enabled: true,
          supports_async_submit: true,
          supports_cancel: false,
          supports_retry: false,
          supports_resume: true,
          runtime_approvals_enabled: false,
          checkpointing_enabled: true,
          event_backend: "database",
        },
        runs: [waitingRun],
        runCheckpointsById: {
          "WR-EVENT-CORRELATION-MISSING": [
            {
              checkpoint_id: "CP-EVENT-CORRELATION-MISSING",
              workflow_run_id: "WR-EVENT-CORRELATION-MISSING",
              project_id: null,
              sequence: 1,
              node_id: "wait_gate",
              state_blob: {
                kind: "wait_for_event",
                node_id: "wait_gate",
                input_by_port: { input: "hello" },
                expected_event_name: "ticket.approved",
                correlation_key: "ticket_id",
              },
              created_at: NOW,
            },
          ],
        },
      }),
    );

    renderAt(<WorkflowDetail />, "/workflows/WF-1", "/workflows/:workflowId");
    await userEvent.click(await screen.findByTestId("tab-runs"));
    await userEvent.click(await screen.findByTestId("run-WR-EVENT-CORRELATION-MISSING"));

    expect(await screen.findByTestId("run-resume-by-event-capability-note")).toHaveTextContent(
      "requires correlation field ticket_id",
    );
    expect(screen.getByTestId("run-resume-by-event-capability-note")).toHaveTextContent(
      "did not capture a correlation value",
    );
    expect(screen.getByTestId("run-resume-by-event")).toBeDisabled();
    expect(screen.getByTestId("run-resume")).toBeEnabled();
  });

  it("disables workflow-wide event matching in detail for legacy wait_event checkpoints", async () => {
    const waitingRun = makeRun({
      workflow_run_id: "WR-EVENT-LEGACY",
      status: "waiting_event",
      trace_id: "trace-event-legacy",
      current_node_id: "wait_gate",
      summary: {
        output: "",
        node_path: ["start", "wait_gate"],
        steps: [],
        resume_checkpoint_id: "CP-EVENT-LEGACY",
      },
    });

    server.use(
      ...detailHandlers({
        capabilities: {
          queue_enabled: true,
          supports_async_submit: true,
          supports_cancel: false,
          supports_retry: false,
          supports_resume: true,
          runtime_approvals_enabled: false,
          checkpointing_enabled: true,
          event_backend: "database",
        },
        runs: [waitingRun],
        runCheckpointsById: {
          "WR-EVENT-LEGACY": [
            {
              checkpoint_id: "CP-EVENT-LEGACY",
              workflow_run_id: "WR-EVENT-LEGACY",
              project_id: null,
              sequence: 1,
              node_id: "wait_gate",
              state_blob: {
                kind: "wait_event",
                node_id: "wait_gate",
                event_name: "ticket.approved",
                input_by_port: { input: "hello" },
              },
              created_at: NOW,
            },
          ],
        },
      }),
    );

    renderAt(<WorkflowDetail />, "/workflows/WF-1", "/workflows/:workflowId");
    await userEvent.click(await screen.findByTestId("tab-runs"));
    await userEvent.click(await screen.findByTestId("run-WR-EVENT-LEGACY"));

    expect(await screen.findByTestId("run-resume-by-event-capability-note")).toHaveTextContent(
      "legacy wait_event shape",
    );
    expect(screen.getByTestId("run-resume-by-event")).toBeDisabled();
    expect(screen.getByTestId("run-resume")).toBeEnabled();
  });

  it("disables wait_for_event resume actions in run detail when the typed event name no longer matches the configured gate", async () => {
    const eventVersion = makeVersion({
      status: "published",
      manifest: {
        ...makeVersion({ status: "published" }).manifest,
        nodes: {
          ...makeVersion({ status: "published" }).manifest.nodes,
          wait_gate: {
            id: "wait_gate",
            type: "wait_for_event",
            event_name: "ticket.approved",
            inputs: { input: { type: "string" } },
            outputs: { output: { type: "string" } },
          },
        },
      },
    });
    const waitingRun = makeRun({
      workflow_run_id: "WR-EVENT-MISMATCH",
      status: "waiting_event",
      trace_id: "trace-event-mismatch",
      current_node_id: "wait_gate",
      summary: {
        output: "",
        node_path: ["start", "wait_gate"],
        steps: [],
        resume_checkpoint_id: "CP-EVENT-MISMATCH",
      },
    });

    server.use(
      ...detailHandlers({
        versions: [eventVersion],
        capabilities: {
          queue_enabled: true,
          supports_async_submit: true,
          supports_cancel: false,
          supports_retry: false,
          supports_resume: true,
          runtime_approvals_enabled: false,
          checkpointing_enabled: true,
          event_backend: "database",
        },
        runs: [waitingRun],
        runCheckpointsById: {
          "WR-EVENT-MISMATCH": [
            {
              checkpoint_id: "CP-EVENT-MISMATCH",
              workflow_run_id: "WR-EVENT-MISMATCH",
              project_id: null,
              sequence: 1,
              node_id: "wait_gate",
              state_blob: {
                kind: "wait_for_event",
                node_id: "wait_gate",
                input_by_port: { input: "hello" },
                expected_event_name: "ticket.approved",
              },
              created_at: NOW,
            },
          ],
        },
      }),
    );

    renderAt(<WorkflowDetail />, "/workflows/WF-1", "/workflows/:workflowId");
    await userEvent.click(await screen.findByTestId("tab-runs"));
    await userEvent.click(await screen.findByTestId("run-WR-EVENT-MISMATCH"));

    const eventNameInput = await screen.findByTestId("run-resume-event-name");
    await userEvent.clear(eventNameInput);
    await userEvent.type(eventNameInput, "ticket.rejected");

    expect(await screen.findByTestId("run-resume-capability-note")).toHaveTextContent(
      "configured for event ticket.approved",
    );
    expect(screen.getByTestId("run-resume-capability-note")).toHaveTextContent(
      "current event name is ticket.rejected",
    );
    expect(screen.getByTestId("run-resume")).toBeDisabled();
    expect(screen.getByTestId("run-resume-by-event")).toBeDisabled();
  });

  it("can reject a pending runtime approval", async () => {
    let rejectCalls = 0;
    server.use(
      ...detailHandlers({
        capabilities: {
          queue_enabled: true,
          supports_async_submit: true,
          supports_cancel: false,
          supports_retry: false,
          supports_resume: false,
          runtime_approvals_enabled: true,
          checkpointing_enabled: false,
          event_backend: "in_process",
        },
        runs: [makeRun({ workflow_run_id: "WR-REJECT", status: "waiting_approval", trace_id: "trace-reject" })],
        runApprovalsById: {
          "WR-REJECT": [
            {
              runtime_approval_id: "RA-REJECT",
              workflow_run_id: "WR-REJECT",
              project_id: null,
              node_id: "approval",
              status: "pending",
              requested_at: NOW,
              decided_at: null,
              decided_by: null,
              decision_reason: null,
              policy_snapshot: null,
            },
          ],
        },
      }),
      http.post(`${API_BASE}/workflow-runs/WR-REJECT/approval/reject`, async ({ request }) => {
        const body = (await request.json()) as { runtime_approval_id?: string };
        expect(body.runtime_approval_id).toBe("RA-REJECT");
        rejectCalls += 1;
        return HttpResponse.json(envelope(makeRun({ workflow_run_id: "WR-REJECT", status: "rejected" })));
      }),
    );

    renderAt(<WorkflowDetail />, "/workflows/WF-1", "/workflows/:workflowId");
    await userEvent.click(await screen.findByTestId("tab-runs"));
    await userEvent.click(await screen.findByTestId("run-WR-REJECT"));
    await userEvent.click(await screen.findByTestId("run-reject"));
    await waitFor(() => expect(rejectCalls).toBe(1));
  });

  it("disables workflow-detail resume actions when the active checkpoint drifts from the run node", async () => {
    server.use(
      ...detailHandlers({
        capabilities: {
          queue_enabled: true,
          supports_async_submit: true,
          supports_cancel: false,
          supports_retry: false,
          supports_resume: true,
          runtime_approvals_enabled: false,
          checkpointing_enabled: true,
          event_backend: "database",
        },
        runs: [
          makeRun({
            workflow_run_id: "WR-EVENT-DRIFT",
            status: "waiting_event",
            trace_id: "trace-event-drift",
            current_node_id: "wait_gate",
            summary: {
              output: "",
              node_path: ["start", "wait_gate"],
              steps: [],
              resume_checkpoint_id: "CP-EVENT-DRIFT",
            },
          }),
        ],
        runCheckpointsById: {
          "WR-EVENT-DRIFT": [
            {
              checkpoint_id: "CP-EVENT-DRIFT",
              workflow_run_id: "WR-EVENT-DRIFT",
              project_id: null,
              sequence: 1,
              node_id: "tool_gate",
              state_blob: {
                kind: "wait_for_event",
                node_id: "other_gate",
                expected_event_name: "ticket.approved",
              },
              created_at: NOW,
            },
          ],
        },
      }),
    );

    renderAt(<WorkflowDetail />, "/workflows/WF-1", "/workflows/:workflowId");
    await userEvent.click(await screen.findByTestId("tab-runs"));
    await userEvent.click(await screen.findByTestId("run-WR-EVENT-DRIFT"));

    expect(await screen.findByTestId("run-resume-capability-note")).toHaveTextContent(
      "stored checkpoint no longer matches this run's active node",
    );
    expect(screen.queryByTestId("run-resume")).not.toBeInTheDocument();
    expect(screen.getByTestId("run-resume-by-event")).toBeDisabled();
  });

  it("disables workflow-detail resume actions when a wait-for-event checkpoint loses its expected event name", async () => {
    server.use(
      ...detailHandlers({
        capabilities: {
          queue_enabled: true,
          supports_async_submit: true,
          supports_cancel: false,
          supports_retry: false,
          supports_resume: true,
          runtime_approvals_enabled: false,
          checkpointing_enabled: true,
          event_backend: "database",
        },
        runs: [
          makeRun({
            workflow_run_id: "WR-EVENT-MISSING-NAME",
            status: "waiting_event",
            trace_id: "trace-event-missing-name",
            current_node_id: "wait_gate",
            summary: {
              output: "",
              node_path: ["start", "wait_gate"],
              steps: [],
              resume_checkpoint_id: "CP-EVENT-MISSING-NAME",
            },
          }),
        ],
        runCheckpointsById: {
          "WR-EVENT-MISSING-NAME": [
            {
              checkpoint_id: "CP-EVENT-MISSING-NAME",
              workflow_run_id: "WR-EVENT-MISSING-NAME",
              project_id: null,
              sequence: 1,
              node_id: "wait_gate",
              state_blob: {
                kind: "wait_for_event",
                node_id: "wait_gate",
                input_by_port: { input: "hello" },
              },
              created_at: NOW,
            },
          ],
        },
      }),
    );

    renderAt(<WorkflowDetail />, "/workflows/WF-1", "/workflows/:workflowId");
    await userEvent.click(await screen.findByTestId("tab-runs"));
    await userEvent.click(await screen.findByTestId("run-WR-EVENT-MISSING-NAME"));

    expect(await screen.findByTestId("run-resume-capability-note")).toHaveTextContent(
      "wait-for-event checkpoint has no expected event name",
    );
    expect(screen.queryByTestId("run-resume")).not.toBeInTheDocument();
    expect(screen.getByTestId("run-resume-by-event")).toBeDisabled();
  });

  it("disables workflow-detail resume actions when the active wait checkpoint payload is corrupt", async () => {
    server.use(
      ...detailHandlers({
        capabilities: {
          queue_enabled: true,
          supports_async_submit: true,
          supports_cancel: false,
          supports_retry: false,
          supports_resume: true,
          runtime_approvals_enabled: false,
          checkpointing_enabled: true,
          event_backend: "database",
        },
        runs: [
          makeRun({
            workflow_run_id: "WR-EVENT-CORRUPT",
            status: "waiting_event",
            trace_id: "trace-event-corrupt",
            current_node_id: "wait_gate",
            summary: {
              output: "",
              node_path: ["start", "wait_gate"],
              steps: [],
              resume_checkpoint_id: "CP-EVENT-CORRUPT",
            },
          }),
        ],
        runCheckpointsById: {
          "WR-EVENT-CORRUPT": [
            {
              checkpoint_id: "CP-EVENT-CORRUPT",
              workflow_run_id: "WR-EVENT-CORRUPT",
              project_id: null,
              sequence: 1,
              node_id: "wait_gate",
              state_blob: ["corrupt-checkpoint-payload"],
              created_at: NOW,
            },
          ],
        },
      }),
    );

    renderAt(<WorkflowDetail />, "/workflows/WF-1", "/workflows/:workflowId");
    await userEvent.click(await screen.findByTestId("tab-runs"));
    await userEvent.click(await screen.findByTestId("run-WR-EVENT-CORRUPT"));

    expect(await screen.findByTestId("run-resume-capability-note")).toHaveTextContent(
      "checkpoint payload is corrupt",
    );
    expect(screen.queryByTestId("run-resume")).not.toBeInTheDocument();
    expect(screen.getByTestId("run-resume-by-event")).toBeDisabled();
  });

  it("explains how to unblock waiting approvals when runtime approval controls are disabled", async () => {
    server.use(
      ...detailHandlers({
        capabilities: {
          queue_enabled: true,
          supports_async_submit: true,
          supports_cancel: false,
          supports_retry: false,
          supports_resume: false,
          runtime_approvals_enabled: false,
          checkpointing_enabled: false,
          event_backend: "in_process",
        },
        runs: [
          makeRun({
            workflow_run_id: "WR-APPROVAL-DISABLED",
            status: "waiting_approval",
            trace_id: "trace-approval-disabled",
            current_node_id: "review",
          }),
        ],
        runApprovalsById: {
          "WR-APPROVAL-DISABLED": [
            {
              runtime_approval_id: "RA-DISABLED",
              workflow_run_id: "WR-APPROVAL-DISABLED",
              project_id: null,
              node_id: "review",
              status: "pending",
              requested_at: NOW,
              decided_at: null,
              decided_by: null,
              decision_reason: null,
              policy_snapshot: null,
            },
          ],
        },
        runCheckpointsById: {
          "WR-APPROVAL-DISABLED": [
            {
              checkpoint_id: "CP-APPROVAL-DISABLED",
              workflow_run_id: "WR-APPROVAL-DISABLED",
              project_id: null,
              sequence: 1,
              node_id: "review",
              state_blob: {
                kind: "human_approval",
                node_id: "review",
                input_by_port: { input: "hello" },
              },
              created_at: NOW,
            },
          ],
        },
      }),
    );

    renderAt(<WorkflowDetail />, "/workflows/WF-1", "/workflows/:workflowId");
    await userEvent.click(await screen.findByTestId("tab-runs"));
    await userEvent.click(await screen.findByTestId("run-WR-APPROVAL-DISABLED"));

    expect(await screen.findByTestId("run-approval-capability-note")).toHaveTextContent(
      "Enable runtime approvals for this deployment",
    );
    expect(screen.getByTestId("run-resume-capability-note")).toHaveTextContent(
      "Manual resume is unavailable until checkpoint persistence is enabled for workflow runs.",
    );
    expect(screen.queryByTestId("run-approve")).not.toBeInTheDocument();
    expect(screen.queryByTestId("run-reject")).not.toBeInTheDocument();
    expect(screen.queryByTestId("run-resume")).not.toBeInTheDocument();
  });

  it("disables paused approval resume in run detail when checkpoint persistence is disabled", async () => {
    server.use(
      ...detailHandlers({
        capabilities: {
          queue_enabled: true,
          supports_async_submit: true,
          supports_cancel: false,
          supports_retry: false,
          supports_resume: true,
          runtime_approvals_enabled: true,
          checkpointing_enabled: false,
          event_backend: "in_process",
        },
        runs: [
          makeRun({
            workflow_run_id: "WR-APPROVAL-NO-CHECKPOINTING",
            status: "waiting_approval",
            trace_id: "trace-approval-no-checkpointing",
            current_node_id: "review",
          }),
        ],
        runApprovalsById: {
          "WR-APPROVAL-NO-CHECKPOINTING": [
            {
              runtime_approval_id: "RA-NO-CHECKPOINTING",
              workflow_run_id: "WR-APPROVAL-NO-CHECKPOINTING",
              project_id: null,
              node_id: "review",
              status: "approved",
              requested_at: NOW,
              decided_at: NOW,
              decided_by: "@reviewer",
              decision_reason: "approved",
              policy_snapshot: null,
            },
          ],
        },
        runCheckpointsById: {
          "WR-APPROVAL-NO-CHECKPOINTING": [
            {
              checkpoint_id: "CP-APPROVAL-NO-CHECKPOINTING",
              workflow_run_id: "WR-APPROVAL-NO-CHECKPOINTING",
              project_id: null,
              sequence: 1,
              node_id: "review",
              state_blob: {
                kind: "human_approval",
                node_id: "review",
                input_by_port: { input: "hello" },
              },
              created_at: NOW,
            },
          ],
        },
      }),
    );

    renderAt(<WorkflowDetail />, "/workflows/WF-1", "/workflows/:workflowId");
    await userEvent.click(await screen.findByTestId("tab-runs"));
    await userEvent.click(await screen.findByTestId("run-WR-APPROVAL-NO-CHECKPOINTING"));

    expect(await screen.findByTestId("run-resume-capability-note")).toHaveTextContent(
      "Manual resume is unavailable until checkpoint persistence is enabled for workflow runs. Re-enable checkpointing for this deployment before continuing this paused approval run.",
    );
    expect(screen.queryByTestId("run-resume")).not.toBeInTheDocument();
  });

  it("disables paused event resume in run detail when the stored checkpoint is missing", async () => {
    const baseVersion = makeVersion({ status: "published" });
    const eventManifest = {
      ...baseVersion.manifest,
      nodes: {
        ...baseVersion.manifest.nodes,
        wait_gate: {
          id: "wait_gate",
          type: "wait_for_event",
          event_name: "ticket.approved",
          inputs: { input: { type: "string" } },
          outputs: { output: { type: "string" } },
        },
      },
    };

    server.use(
      ...detailHandlers({
        versions: [{ ...baseVersion, manifest: eventManifest }],
        capabilities: {
          queue_enabled: true,
          supports_async_submit: true,
          supports_cancel: false,
          supports_retry: false,
          supports_resume: true,
          runtime_approvals_enabled: true,
          checkpointing_enabled: true,
          event_backend: "database",
        },
        runs: [
          makeRun({
            workflow_run_id: "WR-EVENT-NO-CHECKPOINT",
            status: "waiting_event",
            trace_id: "trace-event-no-checkpoint",
            current_node_id: "wait_gate",
            summary: {
              output: "",
              node_path: ["start", "wait_gate"],
              steps: [],
              resume_checkpoint_id: "CP-MISSING",
            },
          }),
        ],
        runCheckpointsById: {
          "WR-EVENT-NO-CHECKPOINT": [],
        },
      }),
    );

    renderAt(<WorkflowDetail />, "/workflows/WF-1", "/workflows/:workflowId");
    await userEvent.click(await screen.findByTestId("tab-runs"));
    await userEvent.click(await screen.findByTestId("run-WR-EVENT-NO-CHECKPOINT"));

    expect(await screen.findByTestId("run-resume-capability-note")).toHaveTextContent(
      "paused run no longer has a stored checkpoint",
    );
    expect(screen.queryByTestId("run-resume")).not.toBeInTheDocument();
    expect(screen.getByTestId("run-resume-by-event")).toBeDisabled();
  });

  it("hides approval actions in run detail when the workflow run queue is disabled", async () => {
    server.use(
      ...detailHandlers({
        capabilities: {
          queue_enabled: false,
          supports_async_submit: true,
          supports_cancel: false,
          supports_retry: false,
          supports_resume: true,
          runtime_approvals_enabled: true,
          checkpointing_enabled: true,
          event_backend: "in_process",
        },
        runs: [
          makeRun({
            workflow_run_id: "WR-APPROVAL-QUEUE-DISABLED",
            status: "waiting_approval",
            trace_id: "trace-approval-queue-disabled",
            current_node_id: "review",
          }),
        ],
        runApprovalsById: {
          "WR-APPROVAL-QUEUE-DISABLED": [
            {
              runtime_approval_id: "RA-QUEUE-DISABLED",
              workflow_run_id: "WR-APPROVAL-QUEUE-DISABLED",
              project_id: null,
              node_id: "review",
              status: "pending",
              requested_at: NOW,
              decided_at: null,
              decided_by: null,
              decision_reason: null,
              policy_snapshot: null,
            },
          ],
        },
      }),
    );

    renderAt(<WorkflowDetail />, "/workflows/WF-1", "/workflows/:workflowId");
    await userEvent.click(await screen.findByTestId("tab-runs"));
    await userEvent.click(await screen.findByTestId("run-WR-APPROVAL-QUEUE-DISABLED"));

    expect(await screen.findByTestId("run-approval-capability-note")).toHaveTextContent(
      "Approval actions are unavailable until the workflow run queue is enabled",
    );
    expect(screen.queryByTestId("run-approve")).not.toBeInTheDocument();
    expect(screen.queryByTestId("run-reject")).not.toBeInTheDocument();
  });

  it("hides cancel actions in run detail when the workflow run queue is disabled", async () => {
    server.use(
      ...detailHandlers({
        capabilities: {
          queue_enabled: false,
          supports_async_submit: true,
          supports_cancel: true,
          supports_retry: true,
          supports_resume: true,
          runtime_approvals_enabled: true,
          checkpointing_enabled: true,
          event_backend: "in_process",
        },
        runs: [
          makeRun({
            workflow_run_id: "WR-CANCEL-QUEUE-DISABLED",
            status: "running",
            trace_id: "trace-cancel-queue-disabled",
          }),
        ],
      }),
    );

    renderAt(<WorkflowDetail />, "/workflows/WF-1", "/workflows/:workflowId");
    await userEvent.click(await screen.findByTestId("tab-runs"));

    await userEvent.click(await screen.findByTestId("run-WR-CANCEL-QUEUE-DISABLED"));
    expect(screen.queryByTestId("run-cancel")).not.toBeInTheDocument();
  });

  it("hides retry actions in run detail when the workflow run queue is disabled", async () => {
    server.use(
      ...detailHandlers({
        capabilities: {
          queue_enabled: false,
          supports_async_submit: true,
          supports_cancel: true,
          supports_retry: true,
          supports_resume: true,
          runtime_approvals_enabled: true,
          checkpointing_enabled: true,
          event_backend: "in_process",
        },
        runs: [
          makeRun({
            workflow_run_id: "WR-RETRY-QUEUE-DISABLED",
            status: "failed",
            trace_id: "trace-retry-queue-disabled",
            completed_at: NOW,
          }),
        ],
      }),
    );

    renderAt(<WorkflowDetail />, "/workflows/WF-1", "/workflows/:workflowId");
    await userEvent.click(await screen.findByTestId("tab-runs"));
    await userEvent.click(await screen.findByTestId("run-WR-RETRY-QUEUE-DISABLED"));
    expect(screen.queryByTestId("run-retry")).not.toBeInTheDocument();
  });

  it("disables detail-page run submission when the workflow run queue is disabled", async () => {
    server.use(
      ...detailHandlers({
        capabilities: {
          queue_enabled: false,
          supports_async_submit: true,
          supports_cancel: false,
          supports_retry: false,
          supports_resume: true,
          runtime_approvals_enabled: true,
          checkpointing_enabled: true,
          event_backend: "in_process",
        },
        versions: [makeVersion({ status: "published" })],
      }),
    );

    renderAt(<WorkflowDetail />, "/workflows/WF-1", "/workflows/:workflowId");
    await userEvent.click(await screen.findByTestId("tab-runs"));

    expect(await screen.findByTestId("workflow-run-start")).toBeDisabled();
    expect(screen.getByTestId("workflow-run-start")).toHaveAttribute(
      "title",
      "Enable the run queue to execute workflows",
    );
  });

  it("disables paused run resume controls in run detail when the workflow run queue is disabled", async () => {
    const baseVersion = makeVersion({ status: "published" });
    const waitingRun = makeRun({
      workflow_run_id: "WR-RESUME-QUEUE-DISABLED",
      status: "waiting_event",
      trace_id: "trace-resume-queue-disabled",
      current_node_id: "wait_gate",
      summary: {
        output: "",
        node_path: ["start", "wait_gate"],
        steps: [],
        resume_checkpoint_id: "CP-RESUME-QUEUE-DISABLED",
      },
    });

    server.use(
      ...detailHandlers({
        versions: [
          {
            ...baseVersion,
            manifest: {
              ...baseVersion.manifest,
              nodes: {
                ...baseVersion.manifest.nodes,
                wait_gate: {
                  id: "wait_gate",
                  type: "wait_for_event",
                  event_name: "ticket.approved",
                  inputs: { input: { type: "string" } },
                  outputs: { output: { type: "string" } },
                },
              },
            },
          },
        ],
        capabilities: {
          queue_enabled: false,
          supports_async_submit: true,
          supports_cancel: false,
          supports_retry: false,
          supports_resume: true,
          runtime_approvals_enabled: true,
          checkpointing_enabled: true,
          event_backend: "database",
        },
        runs: [waitingRun],
        runCheckpointsById: {
          "WR-RESUME-QUEUE-DISABLED": [
            {
              checkpoint_id: "CP-RESUME-QUEUE-DISABLED",
              workflow_run_id: "WR-RESUME-QUEUE-DISABLED",
              project_id: null,
              sequence: 1,
              node_id: "wait_gate",
              state_blob: {
                kind: "wait_for_event",
                node_id: "wait_gate",
                input_by_port: { input: "hello" },
                expected_event_name: "ticket.approved",
              },
              created_at: NOW,
            },
          ],
        },
      }),
    );

    renderAt(<WorkflowDetail />, "/workflows/WF-1", "/workflows/:workflowId");
    await userEvent.click(await screen.findByTestId("tab-runs"));
    await userEvent.click(await screen.findByTestId("run-WR-RESUME-QUEUE-DISABLED"));

    expect(await screen.findByTestId("run-resume-capability-note")).toHaveTextContent(
      "Manual and event-match resume are unavailable until the workflow run queue is enabled",
    );
    expect(screen.queryByTestId("run-resume")).not.toBeInTheDocument();
    expect(screen.getByTestId("run-resume-by-event")).toBeDisabled();
  });

  it("shows action errors for cancel, retry, approve, reject, and resume run controls", async () => {
    server.use(
      ...detailHandlers({
        capabilities: {
          queue_enabled: true,
          supports_async_submit: true,
          supports_cancel: true,
          supports_retry: true,
          supports_resume: true,
          runtime_approvals_enabled: true,
          checkpointing_enabled: true,
          event_backend: "in_process",
        },
        runs: [
          makeRun({ workflow_run_id: "WR-CANCEL", status: "running", trace_id: "trace-cancel" }),
          makeRun({ workflow_run_id: "WR-RETRY", status: "failed", trace_id: "trace-retry" }),
          makeRun({ workflow_run_id: "WR-APPROVE", status: "waiting_approval", trace_id: "trace-approve" }),
          makeRun({ workflow_run_id: "WR-REJECT", status: "waiting_approval", trace_id: "trace-reject" }),
          makeRun({
            workflow_run_id: "WR-RESUME",
            status: "waiting_approval",
            trace_id: "trace-resume",
            current_node_id: "approval",
          }),
        ],
        runApprovalsById: {
          "WR-APPROVE": [
            {
              runtime_approval_id: "RA-APPROVE",
              workflow_run_id: "WR-APPROVE",
              project_id: null,
              node_id: "approval",
              status: "pending",
              requested_at: NOW,
              decided_at: null,
              decided_by: null,
              decision_reason: null,
              policy_snapshot: null,
            },
          ],
          "WR-REJECT": [
            {
              runtime_approval_id: "RA-REJECT",
              workflow_run_id: "WR-REJECT",
              project_id: null,
              node_id: "approval",
              status: "pending",
              requested_at: NOW,
              decided_at: null,
              decided_by: null,
              decision_reason: null,
              policy_snapshot: null,
            },
          ],
          "WR-RESUME": [
            {
              runtime_approval_id: "RA-RESUME",
              workflow_run_id: "WR-RESUME",
              project_id: null,
              node_id: "approval",
              status: "approved",
              requested_at: NOW,
              decided_at: NOW,
              decided_by: "@approver",
              decision_reason: null,
              policy_snapshot: null,
            },
          ],
        },
        runCheckpointsById: {
          "WR-RESUME": [
            {
              checkpoint_id: "CP-RESUME",
              workflow_run_id: "WR-RESUME",
              project_id: null,
              sequence: 1,
              node_id: "approval",
              state_blob: {
                kind: "human_approval",
                node_id: "approval",
                input_by_port: { input: "hello" },
              },
              created_at: NOW,
            },
          ],
        },
      }),
      http.post(`${API_BASE}/workflow-runs/WR-CANCEL/cancel`, () =>
        HttpResponse.json({ detail: "cancel denied" }, { status: 500 }),
      ),
      http.post(`${API_BASE}/workflow-runs/WR-RETRY/retry`, () =>
        HttpResponse.json({ detail: "retry denied" }, { status: 500 }),
      ),
      http.post(`${API_BASE}/workflow-runs/WR-APPROVE/approval/approve`, () =>
        HttpResponse.json({ detail: "approve denied" }, { status: 500 }),
      ),
      http.post(`${API_BASE}/workflow-runs/WR-REJECT/approval/reject`, () =>
        HttpResponse.json({ detail: "reject denied" }, { status: 500 }),
      ),
      http.post(`${API_BASE}/workflow-runs/WR-RESUME/resume`, () =>
        HttpResponse.json({ detail: "resume denied" }, { status: 500 }),
      ),
    );

    renderAt(<WorkflowDetail />, "/workflows/WF-1", "/workflows/:workflowId");
    await userEvent.click(await screen.findByTestId("tab-runs"));

    await userEvent.click(await screen.findByTestId("run-WR-CANCEL"));
    await userEvent.click(await screen.findByTestId("run-cancel"));
    expect(await screen.findByTestId("workflow-run-message")).toHaveTextContent("Cancel failed: cancel denied");
    await userEvent.click(screen.getByTestId("runs-back"));

    await userEvent.click(await screen.findByTestId("run-WR-RETRY"));
    await userEvent.click(await screen.findByTestId("run-retry"));
    expect(await screen.findByTestId("workflow-run-message")).toHaveTextContent("Retry failed: retry denied");
    await userEvent.click(screen.getByTestId("runs-back"));

    await userEvent.click(await screen.findByTestId("run-WR-APPROVE"));
    await userEvent.click(await screen.findByTestId("run-approve"));
    expect(await screen.findByTestId("workflow-run-message")).toHaveTextContent("Approve failed: approve denied");
    await userEvent.click(screen.getByTestId("runs-back"));

    await userEvent.click(await screen.findByTestId("run-WR-REJECT"));
    await userEvent.click(await screen.findByTestId("run-reject"));
    expect(await screen.findByTestId("workflow-run-message")).toHaveTextContent("Reject failed: reject denied");
    await userEvent.click(screen.getByTestId("runs-back"));

    await userEvent.click(await screen.findByTestId("run-WR-RESUME"));
    await userEvent.click(await screen.findByTestId("run-resume"));
    expect(await screen.findByTestId("workflow-run-message")).toHaveTextContent("Resume failed: resume denied");
  });

  it("turns stale retry, approval, and resume failures into recovery guidance", async () => {
    server.use(
      ...detailHandlers({
        capabilities: {
          queue_enabled: true,
          supports_async_submit: true,
          supports_cancel: false,
          supports_retry: true,
          supports_resume: true,
          runtime_approvals_enabled: true,
          checkpointing_enabled: true,
          event_backend: "in_process",
        },
        runs: [
          makeRun({ workflow_run_id: "WR-RETRY-STALE", status: "failed", trace_id: "trace-retry-stale" }),
          makeRun({
            workflow_run_id: "WR-APPROVE-STALE",
            status: "waiting_approval",
            trace_id: "trace-approve-stale",
            current_node_id: "approval",
          }),
          makeRun({
            workflow_run_id: "WR-RESUME-STALE",
            status: "waiting_event",
            trace_id: "trace-resume-stale",
            current_node_id: "wait_gate",
            summary: {
              resume_checkpoint_id: "CP-RESUME-STALE",
            },
          }),
        ],
        runApprovalsById: {
          "WR-APPROVE-STALE": [
            {
              runtime_approval_id: "RA-APPROVE-STALE",
              workflow_run_id: "WR-APPROVE-STALE",
              project_id: null,
              node_id: "approval",
              status: "pending",
              requested_at: NOW,
              decided_at: null,
              decided_by: null,
              decision_reason: null,
              policy_snapshot: null,
            },
          ],
        },
        runCheckpointsById: {
          "WR-RESUME-STALE": [
            {
              checkpoint_id: "CP-RESUME-STALE",
              workflow_run_id: "WR-RESUME-STALE",
              project_id: null,
              sequence: 1,
              node_id: "wait_gate",
              state_blob: {
                kind: "wait_for_event",
                node_id: "wait_gate",
                expected_event_name: "ticket.approved",
                input_by_port: { ticket_id: "T-42" },
              },
              created_at: NOW,
            },
          ],
        },
      }),
      http.post(`${API_BASE}/workflow-runs/WR-RETRY-STALE/retry`, () =>
        HttpResponse.json(
          { detail: "workflow run retry checkpoint is missing its input snapshot" },
          { status: 409 },
        ),
      ),
      http.post(`${API_BASE}/workflow-runs/WR-APPROVE-STALE/approval/approve`, () =>
        HttpResponse.json(
          { detail: "workflow run approval checkpoint is missing its input snapshot" },
          { status: 409 },
        ),
      ),
      http.post(`${API_BASE}/workflow-runs/WR-RESUME-STALE/resume`, () =>
        HttpResponse.json({ detail: "workflow run has no resume checkpoint" }, { status: 409 }),
      ),
    );

    renderAt(<WorkflowDetail />, "/workflows/WF-1", "/workflows/:workflowId");
    await userEvent.click(await screen.findByTestId("tab-runs"));

    await userEvent.click(await screen.findByTestId("run-WR-RETRY-STALE"));
    await userEvent.click(await screen.findByTestId("run-retry"));
    expect(await screen.findByTestId("workflow-run-message")).toHaveTextContent(
      "Retry failed because this run's stored checkpoint or manifest snapshot is no longer healthy.",
    );
    expect(screen.getByTestId("workflow-run-message")).toHaveTextContent(
      "Inspect the recovery, checkpoint, lineage, and debugger panels before retrying from a different checkpoint or starting a new attempt.",
    );
    expect(screen.getByTestId("workflow-run-message")).toHaveTextContent(
      "Latest backend detail: workflow run retry checkpoint is missing its input snapshot",
    );
    await userEvent.click(screen.getByTestId("runs-back"));

    await userEvent.click(await screen.findByTestId("run-WR-APPROVE-STALE"));
    await userEvent.click(await screen.findByTestId("run-approve"));
    expect(await screen.findByTestId("workflow-run-message")).toHaveTextContent(
      "Approve failed because this paused approval state is no longer healthy.",
    );
    expect(screen.getByTestId("workflow-run-message")).toHaveTextContent(
      "Refresh approval history and inspect the recovery, checkpoint, and debugger panels before trying again.",
    );
    expect(screen.getByTestId("workflow-run-message")).toHaveTextContent(
      "Latest backend detail: workflow run approval checkpoint is missing its input snapshot",
    );
    await userEvent.click(screen.getByTestId("runs-back"));

    await userEvent.click(await screen.findByTestId("run-WR-RESUME-STALE"));
    await userEvent.click(await screen.findByTestId("run-resume"));
    expect(await screen.findByTestId("workflow-run-message")).toHaveTextContent(
      "Resume failed because this paused run is no longer resumable from its stored checkpoint.",
    );
    expect(screen.getByTestId("workflow-run-message")).toHaveTextContent(
      "Inspect the recovery, checkpoint, lineage, and debugger panels before retrying from a healthy checkpoint or starting a new attempt.",
    );
    expect(screen.getByTestId("workflow-run-message")).toHaveTextContent(
      "Latest backend detail: workflow run has no resume checkpoint",
    );
  });

  it.each([
    [
      "workflow.run.queued",
      { status: "queued" },
      "Run WR-STREAM queued.",
    ],
    [
      "workflow.run.recovered",
      { status: "queued", reason: "lease_expired", worker_id: "worker-7" },
      "Run WR-STREAM recovered and re-queued: worker lease expired.",
    ],
    [
      "workflow.run.started",
      {},
      "Run WR-STREAM started.",
    ],
    [
      "workflow.run.approval.approved",
      { runtime_approval_id: "RA-STREAM-APPROVED" },
      "Approval recorded for WR-STREAM.",
    ],
    [
      "workflow.run.approval.rejected",
      { runtime_approval_id: "RA-STREAM-REJECTED", reason: "unsafe tool scope" },
      "Runtime approval rejected for WR-STREAM: unsafe tool scope.",
    ],
    [
      "workflow.run.waiting_approval",
      { status: "waiting_approval" },
      "Run WR-STREAM is awaiting approval.",
    ],
    [
      "workflow.run.waiting_event",
      { status: "waiting_event" },
      "Run WR-STREAM is waiting for event.",
    ],
    [
      "workflow.run.resumed",
      { event_name: "ticket.approved" },
      "Run WR-STREAM resumed.",
    ],
    [
      "workflow.run.cancel_requested",
      { reason: "operator stop" },
      "Run WR-STREAM has a cancel request pending: operator stop.",
    ],
    [
      "workflow.run.completed",
      { status: "completed" },
      "Run WR-STREAM completed.",
    ],
    [
      "workflow.run.expired",
      { status: "expired", error: "worker lease lost" },
      "Run WR-STREAM expired: worker lease lost.",
    ],
    [
      "workflow.run.failed",
      { status: "failed", error: "vector index unavailable" },
      "Run WR-STREAM failed: vector index unavailable.",
    ],
  ])("reacts to %s event-stream messages", async (type, extra, expected) => {
    streamState.event = {
      type,
      workflow_id: "WF-1",
      workflow_run_id: "WR-STREAM",
      ...extra,
    };
    server.use(...detailHandlers());

    renderAt(<WorkflowDetail />, "/workflows/WF-1", "/workflows/:workflowId");
    if (!screen.queryByTestId("workflow-run-message")) {
      await userEvent.click(await screen.findByTestId("tab-runs"));
    }

    expect(await screen.findByTestId("workflow-run-message")).toHaveTextContent(expected);
    expect(screen.getByText("WR-STREAM")).toBeInTheDocument();
  });

  it("refreshes exact run-history counts when a started event is the first live signal for a new run", async () => {
    streamState.event = {
      type: "workflow.run.started",
      workflow_id: "WF-1",
      workflow_run_id: "WR-STARTED",
      status: "running",
    };
    const existingRun = makeRun({
      workflow_run_id: "WR-BASE",
      status: "completed",
      completed_at: NOW,
      trace_id: "trace-base",
    });
    const startedRun = makeRun({
      workflow_run_id: "WR-STARTED",
      status: "running",
      trace_id: "trace-started",
      completed_at: null,
    });
    let runListRequests = 0;

    server.use(
      ...detailHandlers({ runs: [existingRun] }),
    );
    server.use(
      http.get(`${API_BASE}/workflows/WF-1/runs`, ({ request }) => {
        runListRequests += 1;
        const currentRuns = runListRequests > 1 ? [startedRun, existingRun] : [existingRun];
        const limit = new URL(request.url).searchParams.get("limit");
        if (!limit) {
          return HttpResponse.json(envelope(currentRuns));
        }
        return HttpResponse.json({ data: currentRuns, next_cursor: null });
      }),
      http.get(`${API_BASE}/workflows/WF-1/runs/stats`, ({ request }) => {
        const url = new URL(request.url);
        const artifactFilter = url.searchParams.get("artifact_persistence");
        return HttpResponse.json(
          envelope(
            buildWorkflowRunHistoryStats([startedRun, existingRun], {
              search: url.searchParams.get("search"),
              artifactFilter:
                artifactFilter === "failed" || artifactFilter === "persisted"
                  ? artifactFilter
                  : null,
            }),
          ),
        );
      }),
    );

    renderAt(<WorkflowDetail />, "/workflows/WF-1", "/workflows/:workflowId");
    expect(await screen.findByTestId("workflow-run-message")).toHaveTextContent(
      "Run WR-STARTED started.",
    );

    await waitFor(() => expect(runListRequests).toBeGreaterThan(1));
    await waitFor(() =>
      expect(screen.getByTestId("runs-triage-summary")).toHaveTextContent(
        "Showing 2 of 2 runs",
      ),
    );
    expect(screen.getByTestId("run-WR-STARTED")).toBeInTheDocument();
  });

  it("reacts to workflow.run.retried by following the new queued attempt", async () => {
    streamState.event = {
      type: "workflow.run.retried",
      workflow_id: "WF-1",
      workflow_run_id: "WR-STREAM",
      retried_run_id: "WR-STREAM-RETRY",
      checkpoint_id: "CP-1",
    };
    server.use(...detailHandlers());

    renderAt(<WorkflowDetail />, "/workflows/WF-1", "/workflows/:workflowId");
    if (!screen.queryByTestId("workflow-run-message")) {
      await userEvent.click(await screen.findByTestId("tab-runs"));
    }

    expect(await screen.findByTestId("workflow-run-message")).toHaveTextContent(
      "Run WR-STREAM retried as WR-STREAM-RETRY.",
    );
    expect(screen.getByText("WR-STREAM-RETRY")).toBeInTheDocument();
  });

  it("streams valid workflow step events into live run logs", async () => {
    streamState.event = {
      type: "workflow.run.step",
      workflow_id: "WF-1",
      workflow_run_id: "WR-STEP",
      step: {
        node_id: "support_agent",
        node_type: "agent",
        status: "ok",
        output: "streamed response",
        tool_calls: [{ tool: "lookup_policy", result: { policy: "refund" } }],
        detail: "tool grounded",
        duration_ms: 12,
      },
    };
    server.use(...detailHandlers());

    renderAt(<WorkflowDetail />, "/workflows/WF-1", "/workflows/:workflowId");
    await userEvent.click(await screen.findByTestId("tab-runs"));

    expect(await screen.findByText("WR-STEP")).toBeInTheDocument();
    expect(screen.getByTestId("workflow-run-logs")).toHaveTextContent("support_agent");
    expect(screen.getByTestId("workflow-run-logs")).toHaveTextContent("streamed response");
    expect(screen.getByTestId("workflow-run-logs")).toHaveTextContent("lookup_policy");
  });

  it("turns empty run logs into active-gate guidance for waiting runs", async () => {
    const waitingRun = makeRun({
      workflow_run_id: "WR-NO-STEPS-WAIT",
      trace_id: "WR-NO-STEPS-WAIT",
      status: "waiting_approval",
      current_node_id: "approval",
      summary: {
        node_path: ["start", "approval"],
        steps: [],
      },
    });
    server.use(...detailHandlers({ runs: [waitingRun] }));

    const user = userEvent.setup();
    renderAt(<WorkflowDetail />, "/workflows/WF-1", "/workflows/:workflowId");
    await user.click(await screen.findByTestId("tab-runs"));
    await user.click(screen.getByTestId("run-WR-NO-STEPS-WAIT"));

    expect(await screen.findByText("Run Logs")).toBeInTheDocument();
    expect(screen.getByText(/This run only has a node path summary so far\./)).toBeInTheDocument();
    expect(screen.getByText(/Use the recovery, checkpoint, and debugger panels above to inspect the active gate/)).toBeInTheDocument();
  });

  it("turns empty run logs into completed-run guidance when step logs were never persisted", async () => {
    const completedRun = makeRun({
      workflow_run_id: "WR-NO-STEPS-DONE",
      trace_id: "WR-NO-STEPS-DONE",
      status: "completed",
      completed_at: NOW,
      current_node_id: "final",
      summary: {
        node_path: ["start", "support_agent", "final"],
        steps: [],
      },
    });
    server.use(...detailHandlers({ runs: [completedRun] }));

    const user = userEvent.setup();
    renderAt(<WorkflowDetail />, "/workflows/WF-1", "/workflows/:workflowId");
    await user.click(await screen.findByTestId("tab-runs"));
    await user.click(screen.getByTestId("run-WR-NO-STEPS-DONE"));

    expect(await screen.findByText("Run Logs")).toBeInTheDocument();
    expect(screen.getByText(/This run completed without persisted step logs\./)).toBeInTheDocument();
    expect(screen.getByText(/Use the debugger, final outputs, and generated artifacts above to reconstruct how execution finished\./)).toBeInTheDocument();
  });

  it("turns sparse active debugger step state into in-flight transition and port guidance", async () => {
    const runningRun = makeRun({
      workflow_run_id: "WR-SPARSE-ACTIVE",
      trace_id: "trace-sparse-active",
      status: "running",
      current_node_id: "start",
      summary: {
        node_path: ["start"],
        steps: [
          {
            node_id: "start",
            node_type: "start",
            status: "running",
            output: "capturing input",
            tool_calls: [],
            handoff_target: null,
            detail: "waiting on runtime persistence",
            duration_ms: 0,
          },
        ],
      },
    });
    server.use(...detailHandlers({ runs: [runningRun] }));

    const user = userEvent.setup();
    renderAt(<WorkflowDetail />, "/workflows/WF-1", "/workflows/:workflowId");
    await user.click(await screen.findByTestId("tab-runs"));
    await user.click(screen.getByTestId("run-WR-SPARSE-ACTIVE"));

    expect(await screen.findByTestId("workflow-run-debugger")).toBeInTheDocument();
    expect(screen.getByTestId("workflow-run-step-diff-transition")).toHaveTextContent(
      "Earliest persisted step so far",
    );
    expect(screen.getByTestId("workflow-run-step-diff-transition")).toHaveTextContent(
      "No previous recorded step is available yet.",
    );
    expect(screen.getByTestId("workflow-run-step-ports")).toHaveTextContent(
      "In · Snapshot pending",
    );
    expect(screen.getByTestId("workflow-run-step-input-ports-empty")).toHaveTextContent(
      "Input port snapshot has not been persisted for this step yet.",
    );
    expect(screen.getByTestId("workflow-run-step-output-ports-empty")).toHaveTextContent(
      "Output port snapshot has not been persisted for this step yet.",
    );
  });

  it("turns sparse completed debugger step state into reconstruction guidance", async () => {
    const completedRun = makeRun({
      workflow_run_id: "WR-SPARSE-DONE",
      trace_id: "trace-sparse-done",
      status: "completed",
      completed_at: NOW,
      current_node_id: "start",
      summary: {
        node_path: ["start"],
        steps: [
          {
            node_id: "start",
            node_type: "start",
            status: "ok",
            output: "finalized without stored snapshots",
            tool_calls: [],
            handoff_target: null,
            detail: "completed before step snapshots were flushed",
            duration_ms: 2,
          },
        ],
      },
    });
    server.use(...detailHandlers({ runs: [completedRun] }));

    const user = userEvent.setup();
    renderAt(<WorkflowDetail />, "/workflows/WF-1", "/workflows/:workflowId");
    await user.click(await screen.findByTestId("tab-runs"));
    await user.click(screen.getByTestId("run-WR-SPARSE-DONE"));

    expect(await screen.findByTestId("workflow-run-debugger")).toBeInTheDocument();
    expect(screen.getByTestId("workflow-run-step-diff-transition")).toHaveTextContent(
      "Earliest persisted step in this completed run",
    );
    expect(screen.getByTestId("workflow-run-step-diff-transition")).toHaveTextContent(
      "No previous recorded step was persisted for this completed run.",
    );
    expect(screen.getByTestId("workflow-run-step-ports")).toHaveTextContent(
      "Out · Snapshot unavailable",
    );
    expect(screen.getByTestId("workflow-run-step-input-ports-empty")).toHaveTextContent(
      "Input port snapshot was not persisted for this completed run step.",
    );
    expect(screen.getByTestId("workflow-run-step-output-ports-empty")).toHaveTextContent(
      "Output port snapshot was not persisted for this completed run step.",
    );
  });

  it("ignores live step events for a different run when a run is already in focus", async () => {
    const keepRun = makeRun({
      workflow_run_id: "WR-KEEP",
      session_id: "thread-keep",
      status: "running",
      trace_id: "trace-keep",
    });
    const otherRun = makeRun({
      workflow_run_id: "WR-OTHER",
      session_id: "thread-other",
      status: "running",
      trace_id: "trace-other",
    });
    streamState.event = {
      type: "workflow.run.started",
      workflow_id: "WF-1",
      workflow_run_id: "WR-KEEP",
    };
    server.use(
      ...detailHandlers({
        runs: [keepRun, otherRun],
      }),
    );

    const user = userEvent.setup();
    const view = renderAt(<WorkflowDetail />, "/workflows/WF-1", "/workflows/:workflowId");
    await user.click(await screen.findByTestId("tab-runs"));
    expect(await screen.findByTestId("workflow-run-message")).toHaveTextContent(
      "Run WR-KEEP started.",
    );
    await user.click(await screen.findByTestId("run-WR-KEEP"));
    expect(await screen.findByTestId("run-recovery-section")).toBeInTheDocument();

    streamState.event = {
      type: "workflow.run.step",
      workflow_id: "WF-1",
      workflow_run_id: "WR-OTHER",
      step: {
        node_id: "other_agent",
        node_type: "agent",
        status: "ok",
        output: "other output",
        tool_calls: [],
        handoff_target: null,
        detail: "other detail",
        duration_ms: 7,
      },
    };
    view.rerenderAt();

    await waitFor(() =>
      expect(screen.getByTestId("workflow-run-message")).toHaveTextContent(
        "Run WR-KEEP started.",
      ),
    );
    expect(screen.queryByText("WR-OTHER")).not.toBeInTheDocument();
    expect(screen.getByTestId("workflow-run-logs")).not.toHaveTextContent("other_agent");
    expect(screen.getByTestId("workflow-run-logs")).not.toHaveTextContent("other output");
  });

  it("refreshes the selected run debugger and session memory when a live step event arrives", async () => {
    const liveRun = makeRun({
      workflow_run_id: "WR-LIVE",
      session_id: "thread-live",
      status: "running",
      current_node_id: "support_agent",
      summary: {
        output: "",
        node_path: ["start", "support_agent"],
        steps: [],
      },
    });
    const runEventsById: Record<string, Array<Record<string, unknown>>> = {
      "WR-LIVE": [
        {
          event_id: 1,
          workflow_run_id: "WR-LIVE",
          project_id: null,
          sequence: 1,
          event_type: "workflow.run.started",
          node_id: "start",
          payload: {},
          created_at: NOW,
        },
      ],
    };
    let sessionEntries: Array<Record<string, unknown>> = [];
    server.use(
      ...detailHandlers({
        runs: [liveRun],
        runEventsById,
      }),
    );
    server.use(
      http.get(`${API_BASE}/workflows/WF-1/session-memory`, ({ request }) => {
        const url = new URL(request.url);
        const sessionId = url.searchParams.get("session_id");
        return HttpResponse.json(envelope(sessionId === "thread-live" ? sessionEntries : []));
      }),
    );

    const view = renderAt(<WorkflowDetail />, "/workflows/WF-1", "/workflows/:workflowId");
    await userEvent.click(await screen.findByTestId("tab-runs"));
    await userEvent.click(await screen.findByTestId("run-WR-LIVE"));

    expect(await screen.findByTestId("workflow-session-memory-empty")).toBeInTheDocument();

    sessionEntries = [
      {
        workflow_id: "WF-1",
        node_id: "support_agent",
        session_id: "thread-live",
        message_history: [
          { role: "user", content: "Need the refund policy" },
          { role: "assistant", content: "Policy-backed answer" },
        ],
        message_count: 2,
        turn_count: 1,
        created_at: NOW,
        updated_at: NOW,
        last_user_message: "Need the refund policy",
        last_assistant_message: "Policy-backed answer",
      },
    ];
    runEventsById["WR-LIVE"] = [
      ...runEventsById["WR-LIVE"],
      {
        event_id: 2,
        workflow_run_id: "WR-LIVE",
        project_id: null,
        sequence: 2,
        event_type: "workflow.run.step",
        node_id: "support_agent",
        payload: { status: "ok", detail: "policy grounded" },
        created_at: NOW,
      },
    ];
    streamState.event = {
      type: "workflow.run.step",
      workflow_id: "WF-1",
      workflow_run_id: "WR-LIVE",
      step: {
        node_id: "support_agent",
        node_type: "agent",
        status: "ok",
        output: "Policy-backed answer",
        tool_calls: [],
        handoff_target: null,
        detail: "policy grounded",
        duration_ms: 14,
      },
    };
    view.rerenderAt();

    await waitFor(() =>
      expect(screen.getByTestId("workflow-run-event-timeline")).toHaveTextContent(
        "workflow.run.step",
      ),
    );
    await waitFor(() =>
      expect(screen.getByTestId("workflow-session-memory-entry-support_agent")).toHaveTextContent(
        "Policy-backed answer",
      ),
      { timeout: 3500 },
    );
  });

  it("auto-follows the live node when a selected run receives workflow.run.node_started", async () => {
    const liveRun = makeRun({
      workflow_run_id: "WR-LIVE-NODE",
      status: "running",
      current_node_id: "support_agent",
      summary: {
        output: "",
        node_path: ["start", "support_agent"],
        steps: [
          {
            node_id: "start",
            node_type: "start",
            status: "ok",
            output: "customer message",
            tool_calls: [],
            handoff_target: null,
            detail: "captured input",
            duration_ms: 5,
            output_by_port: { user_message: "customer message" },
          },
          {
            node_id: "support_agent",
            node_type: "agent",
            status: "ok",
            output: "Policy-backed answer",
            tool_calls: [],
            handoff_target: null,
            detail: "grounded answer",
            duration_ms: 14,
            input_by_port: { input: "customer message" },
            output_by_port: { final_output: "Policy-backed answer" },
          },
        ],
      },
    });
    server.use(
      ...detailHandlers({
        runs: [liveRun],
        runEventsById: {
          "WR-LIVE-NODE": [
            {
              event_id: 1,
              workflow_run_id: "WR-LIVE-NODE",
              project_id: null,
              sequence: 1,
              event_type: "workflow.run.started",
              node_id: null,
              payload: {},
              created_at: NOW,
            },
            {
              event_id: 2,
              workflow_run_id: "WR-LIVE-NODE",
              project_id: null,
              sequence: 2,
              event_type: "workflow.run.step",
              node_id: "start",
              payload: {
                step: liveRun.summary.steps[0],
              },
              created_at: NOW,
            },
            {
              event_id: 3,
              workflow_run_id: "WR-LIVE-NODE",
              project_id: null,
              sequence: 3,
              event_type: "workflow.run.step",
              node_id: "support_agent",
              payload: {
                step: liveRun.summary.steps[1],
              },
              created_at: NOW,
            },
          ],
        },
      }),
    );

    const view = renderAt(<WorkflowDetail />, "/workflows/WF-1", "/workflows/:workflowId");
    await userEvent.click(await screen.findByTestId("tab-runs"));
    await userEvent.click(await screen.findByTestId("run-WR-LIVE-NODE"));

    expect(await screen.findByTestId("node-detail-panel")).toHaveTextContent("support-agent");

    streamState.event = {
      type: "workflow.run.node_started",
      workflow_id: "WF-1",
      workflow_run_id: "WR-LIVE-NODE",
      node_id: "final",
      node_type: "output",
    };
    view.rerenderAt();

    await waitFor(() =>
      expect(screen.getByTestId("node-detail-panel")).toHaveTextContent("final"),
    );
    expect(screen.getByTestId("workflow-run-step-snapshot")).toHaveTextContent("final");
  });

  it("keeps a manual node selection pinned while the live run advances", async () => {
    const liveRun = makeRun({
      workflow_run_id: "WR-LIVE-PINNED",
      status: "running",
      current_node_id: "support_agent",
      summary: {
        output: "",
        node_path: ["start", "support_agent"],
        steps: [
          {
            node_id: "start",
            node_type: "start",
            status: "ok",
            output: "customer message",
            tool_calls: [],
            handoff_target: null,
            detail: "captured input",
            duration_ms: 5,
            output_by_port: { user_message: "customer message" },
          },
          {
            node_id: "support_agent",
            node_type: "agent",
            status: "ok",
            output: "Policy-backed answer",
            tool_calls: [],
            handoff_target: null,
            detail: "grounded answer",
            duration_ms: 14,
            input_by_port: { input: "customer message" },
            output_by_port: { final_output: "Policy-backed answer" },
          },
        ],
      },
    });
    server.use(
      ...detailHandlers({
        runs: [liveRun],
        runEventsById: {
          "WR-LIVE-PINNED": [
            {
              event_id: 1,
              workflow_run_id: "WR-LIVE-PINNED",
              project_id: null,
              sequence: 1,
              event_type: "workflow.run.started",
              node_id: null,
              payload: {},
              created_at: NOW,
            },
            {
              event_id: 2,
              workflow_run_id: "WR-LIVE-PINNED",
              project_id: null,
              sequence: 2,
              event_type: "workflow.run.step",
              node_id: "start",
              payload: {
                step: liveRun.summary.steps[0],
              },
              created_at: NOW,
            },
            {
              event_id: 3,
              workflow_run_id: "WR-LIVE-PINNED",
              project_id: null,
              sequence: 3,
              event_type: "workflow.run.step",
              node_id: "support_agent",
              payload: {
                step: liveRun.summary.steps[1],
              },
              created_at: NOW,
            },
          ],
        },
      }),
    );

    const view = renderAt(<WorkflowDetail />, "/workflows/WF-1", "/workflows/:workflowId");
    await userEvent.click(await screen.findByTestId("tab-runs"));
    await userEvent.click(await screen.findByTestId("run-WR-LIVE-PINNED"));
    await userEvent.click(await screen.findByTestId("workflow-run-step-button-0"));

    expect(await screen.findByTestId("node-detail-panel")).toHaveTextContent("start");

    streamState.event = {
      type: "workflow.run.node_started",
      workflow_id: "WF-1",
      workflow_run_id: "WR-LIVE-PINNED",
      node_id: "final",
      node_type: "output",
    };
    view.rerenderAt();

    await waitFor(() =>
      expect(screen.getByTestId("workflow-run-step-snapshot")).toHaveTextContent("final"),
    );
    expect(screen.getByTestId("node-detail-panel")).toHaveTextContent("start");
  });

  it("refreshes approval checkpoints when a selected run enters waiting approval", async () => {
    const liveRun = makeRun({
      workflow_run_id: "WR-APPROVAL",
      status: "running",
      current_node_id: "support_agent",
      summary: {
        output: "",
        node_path: ["start", "support_agent"],
        steps: [],
      },
    });
    const runApprovalsById: Record<string, Array<Record<string, unknown>>> = {
      "WR-APPROVAL": [],
    };
    const runCheckpointsById: Record<string, Array<Record<string, unknown>>> = {
      "WR-APPROVAL": [],
    };
    server.use(
      ...detailHandlers({
        capabilities: {
          queue_enabled: true,
          supports_async_submit: true,
          supports_cancel: true,
          supports_retry: true,
          supports_resume: true,
          runtime_approvals_enabled: true,
          checkpointing_enabled: true,
          event_backend: "in_process",
        },
        runs: [liveRun],
        runApprovalsById,
        runCheckpointsById,
      }),
    );

    const view = renderAt(<WorkflowDetail />, "/workflows/WF-1", "/workflows/:workflowId");
    await userEvent.click(await screen.findByTestId("tab-runs"));
    await userEvent.click(await screen.findByTestId("run-WR-APPROVAL"));

    expect(await screen.findByTestId("workflow-run-recovery-panel")).toHaveTextContent(
      "Actively executing",
    );

    liveRun.status = "waiting_approval";
    liveRun.current_node_id = "human_gate";
    liveRun.summary = {
      output: "",
      node_path: ["start", "human_gate"],
      steps: [],
      resume_checkpoint_id: "WRCK-APPROVAL",
    };
    runApprovalsById["WR-APPROVAL"] = [
      {
        runtime_approval_id: "RA-1",
        workflow_run_id: "WR-APPROVAL",
        project_id: null,
        node_id: "human_gate",
        status: "pending",
        required_role: "caliber.approver",
        requested_at: NOW,
        decided_at: null,
        decided_by: null,
        reason: null,
        policy_snapshot: {
          required_role: "caliber.approver",
          approval_count: 1,
          timeout_behavior: "block",
        },
      },
    ];
    runCheckpointsById["WR-APPROVAL"] = [
      {
        checkpoint_id: "WRCK-APPROVAL",
        workflow_run_id: "WR-APPROVAL",
        project_id: null,
        sequence: 1,
        node_id: "human_gate",
        state_blob: {
          kind: "human_approval",
          node_id: "human_gate",
          approval_count: 1,
        },
        created_at: NOW,
      },
    ];
    streamState.event = {
      type: "workflow.run.waiting_approval",
      workflow_id: "WF-1",
      workflow_run_id: "WR-APPROVAL",
      status: "waiting_approval",
    };
    view.rerenderAt();

    await waitFor(() =>
      expect(screen.getByTestId("workflow-run-recovery-panel")).toHaveTextContent(
        "Awaiting approval",
      ),
    );
    expect(screen.getByTestId("workflow-run-recovery-panel")).toHaveTextContent("RA-1");
    await waitFor(() =>
      expect(screen.getByTestId("workflow-run-checkpoint-item-1")).toHaveTextContent(
        "Human approval",
      ),
    );
  });

  it("labels runtime approval gates distinctly when a selected run pauses on a tool gate", async () => {
    const liveRun = makeRun({
      workflow_run_id: "WR-TOOL-APPROVAL",
      status: "running",
      current_node_id: "support_agent",
      summary: {
        output: "",
        node_path: ["start", "support_agent"],
        steps: [],
      },
    });
    const runApprovalsById: Record<string, Array<Record<string, unknown>>> = {
      "WR-TOOL-APPROVAL": [],
    };
    const runCheckpointsById: Record<string, Array<Record<string, unknown>>> = {
      "WR-TOOL-APPROVAL": [],
    };
    server.use(
      ...detailHandlers({
        capabilities: {
          queue_enabled: true,
          supports_async_submit: true,
          supports_cancel: true,
          supports_retry: true,
          supports_resume: true,
          runtime_approvals_enabled: true,
          checkpointing_enabled: true,
          event_backend: "in_process",
        },
        runs: [liveRun],
        runApprovalsById,
        runCheckpointsById,
      }),
    );

    const view = renderAt(<WorkflowDetail />, "/workflows/WF-1", "/workflows/:workflowId");
    await userEvent.click(await screen.findByTestId("tab-runs"));
    await userEvent.click(await screen.findByTestId("run-WR-TOOL-APPROVAL"));

    expect(await screen.findByTestId("workflow-run-recovery-panel")).toHaveTextContent(
      "Actively executing",
    );

    liveRun.status = "waiting_approval";
    liveRun.current_node_id = "tool_gate";
    liveRun.summary = {
      output: "",
      node_path: ["start", "tool_gate"],
      steps: [],
      resume_checkpoint_id: "WRCK-TOOL-APPROVAL",
    };
    runApprovalsById["WR-TOOL-APPROVAL"] = [
      {
        runtime_approval_id: "RA-TOOL-1",
        workflow_run_id: "WR-TOOL-APPROVAL",
        project_id: null,
        node_id: "tool_gate",
        status: "pending",
        requested_at: NOW,
        decided_at: null,
        decided_by: null,
        reason: null,
        policy_snapshot: {
          timeout_behavior: "block",
        },
      },
    ];
    runCheckpointsById["WR-TOOL-APPROVAL"] = [
      {
        checkpoint_id: "WRCK-TOOL-APPROVAL",
        workflow_run_id: "WR-TOOL-APPROVAL",
        project_id: null,
        sequence: 1,
        node_id: "tool_gate",
        state_blob: {
          kind: "runtime_approval",
          node_id: "tool_gate",
          input_by_port: {
            input: "delete ticket T-300",
          },
          output: "delete ticket T-300",
        },
        created_at: NOW,
      },
    ];
    streamState.event = {
      type: "workflow.run.waiting_approval",
      workflow_id: "WF-1",
      workflow_run_id: "WR-TOOL-APPROVAL",
      status: "waiting_approval",
      node_id: "tool_gate",
      runtime_approval_id: "RA-TOOL-1",
    };
    view.rerenderAt();

    await waitFor(() =>
      expect(screen.getByTestId("workflow-run-recovery-panel")).toHaveTextContent(
        "Awaiting runtime approval",
      ),
    );
    expect(screen.getByTestId("workflow-run-recovery-panel")).toHaveTextContent("RA-TOOL-1");
    expect(screen.getByText("pending runtime approval RA-TOOL-1")).toBeInTheDocument();
    await waitFor(() =>
      expect(screen.getByTestId("workflow-run-checkpoint-item-1")).toHaveTextContent(
        "Runtime approval",
      ),
    );
  });

  it("refreshes selected run approval diagnostics when runtime approval is recorded live", async () => {
    const liveRun = makeRun({
      workflow_run_id: "WR-TOOL-APPROVAL-RECORDED",
      status: "waiting_approval",
      current_node_id: "tool_gate",
      summary: {
        output: "",
        node_path: ["start", "tool_gate"],
        steps: [],
        resume_checkpoint_id: "WRCK-TOOL-APPROVAL-RECORDED",
      },
    });
    const runApprovalsById: Record<string, Array<Record<string, unknown>>> = {
      "WR-TOOL-APPROVAL-RECORDED": [
        {
          runtime_approval_id: "RA-TOOL-RECORDED",
          workflow_run_id: "WR-TOOL-APPROVAL-RECORDED",
          project_id: null,
          node_id: "tool_gate",
          status: "pending",
          requested_at: NOW,
          decided_at: null,
          decided_by: null,
          decision_reason: null,
          policy_snapshot: {
            timeout_behavior: "block",
          },
        },
      ],
    };
    const runCheckpointsById: Record<string, Array<Record<string, unknown>>> = {
      "WR-TOOL-APPROVAL-RECORDED": [
        {
          checkpoint_id: "WRCK-TOOL-APPROVAL-RECORDED",
          workflow_run_id: "WR-TOOL-APPROVAL-RECORDED",
          project_id: null,
          sequence: 1,
          node_id: "tool_gate",
          state_blob: {
            kind: "runtime_approval",
            node_id: "tool_gate",
            input_by_port: {
              input: "delete ticket T-300",
            },
            output: "delete ticket T-300",
          },
          created_at: NOW,
        },
      ],
    };
    server.use(
      ...detailHandlers({
        capabilities: {
          queue_enabled: true,
          supports_async_submit: true,
          supports_cancel: true,
          supports_retry: true,
          supports_resume: true,
          runtime_approvals_enabled: true,
          checkpointing_enabled: true,
          event_backend: "in_process",
        },
        runs: [liveRun],
        runApprovalsById,
        runCheckpointsById,
      }),
    );

    const user = userEvent.setup();
    const view = renderAt(<WorkflowDetail />, "/workflows/WF-1", "/workflows/:workflowId");
    await user.click(await screen.findByTestId("tab-runs"));
    await user.click(await screen.findByTestId("run-WR-TOOL-APPROVAL-RECORDED"));

    expect(await screen.findByTestId("workflow-run-recovery-panel")).toHaveTextContent(
      "Awaiting runtime approval",
    );

    runApprovalsById["WR-TOOL-APPROVAL-RECORDED"] = [
      {
        runtime_approval_id: "RA-TOOL-RECORDED",
        workflow_run_id: "WR-TOOL-APPROVAL-RECORDED",
        project_id: null,
        node_id: "tool_gate",
        status: "approved",
        requested_at: NOW,
        decided_at: NOW,
        decided_by: "@ops",
        decision_reason: "policy reviewed",
        policy_snapshot: {
          timeout_behavior: "block",
        },
      },
    ];
    streamState.event = {
      type: "workflow.run.approval.approved",
      workflow_id: "WF-1",
      workflow_run_id: "WR-TOOL-APPROVAL-RECORDED",
      status: "waiting_approval",
      runtime_approval_id: "RA-TOOL-RECORDED",
      event_id: 9,
      sequence: 3,
      created_at: NOW,
    };
    view.rerenderAt();

    await waitFor(() =>
      expect(screen.getByTestId("workflow-run-message")).toHaveTextContent(
        "Approval recorded for WR-TOOL-APPROVAL-RECORDED.",
      ),
    );
    await waitFor(() =>
      expect(screen.getByTestId("workflow-run-recovery-panel")).toHaveTextContent(
        "Runtime approval recorded",
      ),
    );
    expect(screen.getByTestId("workflow-run-recovery-panel")).toHaveTextContent(
      "RA-TOOL-RECORDED is approved on tool_gate",
    );
  });

  it("ignores malformed workflow step events and refresh-only workflow status events", async () => {
    streamState.event = {
      type: "workflow.run.step",
      workflow_id: "WF-1",
      workflow_run_id: "WR-BAD",
      step: null,
    };
    server.use(...detailHandlers());

    renderAt(<WorkflowDetail />, "/workflows/WF-1", "/workflows/:workflowId");
    await userEvent.click(await screen.findByTestId("tab-runs"));

    expect(screen.queryByText("WR-BAD")).not.toBeInTheDocument();
    expect(screen.queryByTestId("workflow-run-logs")).not.toBeInTheDocument();
  });

  it("accepts workflow pause events from the live stream as refresh signals", async () => {
    streamState.event = {
      type: "workflow.paused",
      workflow_id: "WF-1",
      workflow_run_id: "WR-IGNORED",
    };
    server.use(...detailHandlers());

    renderAt(<WorkflowDetail />, "/workflows/WF-1", "/workflows/:workflowId");

    expect(await screen.findByTestId("workflow-detail")).toBeInTheDocument();
    expect(screen.queryByTestId("workflow-run-message")).not.toBeInTheDocument();
  });

  it("redirects back to the workflow list when the live stream reports deletion", async () => {
    server.use(...detailHandlers());

    const view = renderAt(<WorkflowDetail />, "/workflows/WF-1", "/workflows/:workflowId");
    expect(await screen.findByTestId("workflow-detail")).toBeInTheDocument();

    streamState.event = {
      type: "workflow.deleted",
      workflow_id: "WF-1",
      name: "Support",
      event_id: 24,
      sequence: 11,
      created_at: NOW,
    };
    view.rerenderAt();

    await waitFor(() =>
      expect(screen.getByText("WORKFLOWS ROUTE")).toBeInTheDocument(),
    );
    expect(showToast.info).toHaveBeenCalledWith('Workflow "Support" was deleted.');
  });

  it("applies workflow archive events even while a run is selected", async () => {
    let workflowStatus: "active" | "archived" = "active";
    const baseVersion = makeVersion({ status: "published" });
    const manifest = {
      ...baseVersion.manifest,
      nodes: {
        ...baseVersion.manifest.nodes,
        start: {
          ...baseVersion.manifest.nodes.start,
          trigger: { mode: "event", event_name: "object.created", alias: "prod", enabled: true },
        },
      },
    };
    const selectedRun = makeRun({
      workflow_run_id: "WR-LIVE-ARCHIVE",
      status: "running",
      current_node_id: "support_agent",
      summary: {
        output: "",
        node_path: ["start", "support_agent"],
        steps: [],
      },
    });

    server.use(
      ...detailHandlers({
        workflow: { status: workflowStatus },
        versions: [{ ...baseVersion, manifest }],
        runs: [selectedRun],
        capabilities: {
          queue_enabled: true,
          supports_async_submit: true,
          supports_cancel: false,
          supports_retry: false,
          supports_resume: false,
          runtime_approvals_enabled: false,
          checkpointing_enabled: false,
          event_backend: "nats",
        },
        deployments: [
          {
            deployment_id: "WFD-PROD",
            workflow_id: "WF-1",
            alias: "prod",
            version_id: "WFV-1",
            environment: null,
            status: "active",
            deployed_by: "@ops",
            deployed_at: NOW,
            rollback_checkpoint: [],
          },
        ],
      }),
      http.get(`${API_BASE}/workflows/WF-1`, () =>
        HttpResponse.json(envelope(makeWorkflow({ status: workflowStatus }))),
      ),
    );

    const view = renderAt(<WorkflowDetail />, "/workflows/WF-1", "/workflows/:workflowId");
    expect(await screen.findByTestId("workflow-detail")).toBeInTheDocument();

    await userEvent.click(await screen.findByTestId("workflow-run-open"));
    await userEvent.click(await screen.findByTestId("run-WR-LIVE-ARCHIVE"));
    await waitFor(() =>
      expect(screen.getByTestId("workflow-run-start")).toBeEnabled(),
    );

    workflowStatus = "archived";
    streamState.event = {
      type: "workflow.updated",
      workflow_id: "WF-1",
      status: "archived",
    };
    view.rerenderAt();

    await waitFor(() =>
      expect(screen.getByTestId("workflow-run-start")).toBeDisabled(),
    );
    await waitFor(() =>
      expect(screen.queryByTestId("workflow-pause-toggle")).not.toBeInTheDocument(),
    );
    expect(screen.getByText("archived")).toBeInTheDocument();

    await userEvent.click(screen.getByTestId("tab-deployments"));
    const triggerButton = await screen.findByTestId("trigger-now-prod");
    expect(triggerButton).toBeDisabled();
    expect(triggerButton).toHaveAttribute(
      "title",
      "Archived workflows cannot be triggered",
    );
  });

  it("renders a step debugger from persisted run events and lets operators inspect each node", async () => {
    server.use(
      ...detailHandlers({
        runs: [
          makeRun({
            workflow_run_id: "WR-DBG",
            status: "completed",
            trace_id: "trace-dbg",
            current_node_id: "support_agent",
            summary: {
              output: "Policy-backed answer",
              node_path: ["start", "support_agent"],
              steps: [],
            },
          }),
        ],
        runEventsById: {
          "WR-DBG": [
            {
              event_id: 1,
              workflow_run_id: "WR-DBG",
              project_id: null,
              sequence: 1,
              event_type: "workflow.run.started",
              node_id: null,
              payload: { workflow_id: "WF-1" },
              created_at: NOW,
            },
            {
              event_id: 2,
              workflow_run_id: "WR-DBG",
              project_id: null,
              sequence: 2,
              event_type: "workflow.run.step",
              node_id: "start",
              payload: {
                step: {
                  node_id: "start",
                  node_type: "start",
                  status: "ok",
                  output: "customer message",
                  tool_calls: [],
                  handoff_target: null,
                  detail: "captured trigger input",
                  duration_ms: 5,
                  input_by_port: {},
                  output_by_port: { user_message: "customer message" },
                },
              },
              created_at: NOW,
            },
            {
              event_id: 3,
              workflow_run_id: "WR-DBG",
              project_id: null,
              sequence: 3,
              event_type: "workflow.run.step",
              node_id: "support_agent",
              payload: {
                step: {
                  node_id: "support_agent",
                  node_type: "agent",
                  status: "ok",
                  output: "Policy-backed answer",
                  tool_calls: [{ tool: "lookup_policy", result: { policy: "refund" } }],
                  handoff_target: null,
                  detail: "tool grounded",
                  duration_ms: 18,
                  input_by_port: { input: "customer message" },
                  output_by_port: {
                    final_output: "Policy-backed answer",
                    tool_calls: [{ tool: "lookup_policy", result: { policy: "refund" } }],
                  },
                },
              },
              created_at: NOW,
            },
            {
              event_id: 4,
              workflow_run_id: "WR-DBG",
              project_id: null,
              sequence: 4,
              event_type: "workflow.run.completed",
              node_id: null,
              payload: { status: "completed" },
              created_at: NOW,
            },
          ],
        },
        runCheckpointsById: {
          "WR-DBG": [
            {
              checkpoint_id: "CP-DBG-1",
              workflow_run_id: "WR-DBG",
              project_id: null,
              sequence: 1,
              node_id: "support_agent",
              state_blob: {
                kind: "checkpoint",
                node_id: "support_agent",
                output_by_port: { final_output: "Policy-backed answer" },
              },
              created_at: NOW,
            },
          ],
        },
        runFilesById: {
          "WR-DBG": [
            {
              file_id: "FILE-SUPPORT",
              file_ref: "caliber://workflow-runs/WR-DBG/artifact/policy.json",
              name: "policy.json",
              kind: "artifact",
              relative_path: "policy.json",
              media_type: "application/json",
              size_bytes: 1024,
              sha256: "sha-support",
              status: "artifact",
              producer_node_id: "support_agent",
              created_at: NOW,
            },
            {
              file_id: "FILE-START",
              file_ref: "caliber://workflow-runs/WR-DBG/work/trigger.txt",
              name: "trigger.txt",
              kind: "work",
              relative_path: "trigger.txt",
              media_type: "text/plain",
              size_bytes: 128,
              sha256: "sha-start",
              status: "uploaded",
              producer_node_id: "start",
              created_at: NOW,
            },
          ],
        },
      }),
    );

    renderAt(<WorkflowDetail />, "/workflows/WF-1", "/workflows/:workflowId");
    await userEvent.click(await screen.findByTestId("tab-runs"));
    await userEvent.click(await screen.findByTestId("run-WR-DBG"));

    expect(await screen.findByTestId("workflow-run-debugger")).toBeInTheDocument();
    expect(screen.getByTestId("workflow-run-step-detail")).toHaveTextContent("support_agent");
    expect(screen.getByTestId("workflow-run-step-tools")).toHaveTextContent("lookup_policy");
    expect(screen.getByTestId("node-detail-panel")).toHaveTextContent("support-agent");
    expect(screen.getByTestId("workflow-run-step-button-1")).toHaveTextContent("Checkpoint");
    expect(screen.getByTestId("workflow-run-step-input-ports")).toHaveTextContent("customer message");
    expect(screen.getByTestId("workflow-run-step-output-ports")).toHaveTextContent("Policy-backed answer");
    expect(screen.getByTestId("workflow-run-step-diff-transition")).toHaveTextContent("user_message");
    expect(screen.getByTestId("workflow-run-step-diff-transition")).toHaveTextContent("input");
    expect(screen.getByTestId("workflow-run-step-diff-transform")).toHaveTextContent("final_output");
    expect(screen.getByTestId("workflow-run-step-diff-transform")).toHaveTextContent("tool_calls");
    expect(screen.getByTestId("workflow-run-event-timeline")).toHaveTextContent("workflow.run.completed");
    expect(screen.getByTestId("workflow-run-file-scope")).toHaveTextContent("Focused on support_agent");
    expect(await screen.findByTestId("workflow-run-file-FILE-SUPPORT")).toBeInTheDocument();
    expect(screen.queryByTestId("workflow-run-file-FILE-START")).not.toBeInTheDocument();

    await userEvent.click(screen.getByTestId("workflow-run-step-button-0"));

    const detail = screen.getByTestId("workflow-run-step-detail");
    expect(within(detail).getAllByText("start").length).toBeGreaterThan(0);
    expect(detail).toHaveTextContent("customer message");
    expect(detail).toHaveTextContent("captured trigger input");
    expect(screen.getByTestId("node-detail-panel")).toHaveTextContent("start");
    expect(screen.getByTestId("workflow-run-step-output-ports")).toHaveTextContent("user_message");
    expect(screen.getByTestId("workflow-run-step-diff-transform")).toHaveTextContent("user_message");
    expect(screen.getByTestId("workflow-run-file-scope")).toHaveTextContent("Focused on start");
    expect(await screen.findByTestId("workflow-run-file-FILE-START")).toBeInTheDocument();
    expect(screen.queryByTestId("workflow-run-file-FILE-SUPPORT")).not.toBeInTheDocument();

    await userEvent.click(screen.getByTestId("trace-path-step-1"));
    expect(screen.getByTestId("workflow-run-step-detail")).toHaveTextContent("support_agent");
    expect(screen.getByTestId("node-detail-panel")).toHaveTextContent("support-agent");
    expect(screen.getByTestId("workflow-run-file-scope")).toHaveTextContent("Focused on support_agent");
    expect(await screen.findByTestId("workflow-run-file-FILE-SUPPORT")).toBeInTheDocument();
  });

  it("shows workflow session memory for a run and lets operators clear it", async () => {
    server.use(
      ...detailHandlers({
        runs: [
          makeRun({
            workflow_run_id: "WR-MEM",
            status: "completed",
            trace_id: "trace-mem",
            session_id: "thread-42",
          }),
        ],
        sessionMemoryBySessionId: {
          "thread-42": [
            {
              workflow_id: "WF-1",
              node_id: "support_agent",
              session_id: "thread-42",
              message_history: [
                { role: "user", content: "Need the refund policy" },
                { role: "assistant", content: "Policy-backed answer" },
              ],
              message_count: 2,
              turn_count: 1,
              created_at: NOW,
              updated_at: NOW,
              last_user_message: "Need the refund policy",
              last_assistant_message: "Policy-backed answer",
            },
          ],
        },
      }),
    );

    renderAt(<WorkflowDetail />, "/workflows/WF-1", "/workflows/:workflowId");
    await userEvent.click(await screen.findByTestId("tab-runs"));
    await userEvent.click(await screen.findByTestId("run-WR-MEM"));

    expect(await screen.findByTestId("workflow-session-memory-panel")).toBeInTheDocument();
    expect(screen.getByTestId("workflow-session-memory-entry-support_agent")).toHaveTextContent(
      "Need the refund policy",
    );
    expect(screen.getByTestId("workflow-session-memory-entry-support_agent")).toHaveTextContent(
      "Policy-backed answer",
    );

    await userEvent.click(screen.getByTestId("workflow-session-memory-clear-session"));

    await waitFor(() =>
      expect(screen.getByTestId("workflow-session-memory-empty")).toHaveTextContent(
        "No persisted memory was recorded for this session during the completed run.",
      ),
    );
    expect(screen.getByTestId("workflow-session-memory-empty")).toHaveTextContent(
      "inspect the debugger or final outputs",
    );
    expect(screen.getByTestId("workflow-run-message")).toHaveTextContent(
      "Cleared 2 message(s) from session thread-42.",
    );
  });

  it("turns empty workflow session memory into stopped-run guidance", async () => {
    server.use(
      ...detailHandlers({
        runs: [
          makeRun({
            workflow_run_id: "WR-MEM-STOPPED",
            status: "failed",
            trace_id: "trace-mem-stopped",
            session_id: "thread-stopped",
          }),
        ],
        sessionMemoryBySessionId: {
          "thread-stopped": [],
        },
      }),
    );

    renderAt(<WorkflowDetail />, "/workflows/WF-1", "/workflows/:workflowId");
    await userEvent.click(await screen.findByTestId("tab-runs"));
    await userEvent.click(await screen.findByTestId("run-WR-MEM-STOPPED"));

    expect(await screen.findByTestId("workflow-session-memory-empty")).toHaveTextContent(
      "No persisted memory was recorded for this session before the run stopped.",
    );
    expect(screen.getByTestId("workflow-session-memory-empty")).toHaveTextContent(
      "Inspect the debugger and recovery panels",
    );
    expect(screen.getByTestId("workflow-session-memory-empty")).toHaveTextContent(
      "when you retry or rerun it",
    );
  });

  it("turns missing workflow session ids into stopped-run guidance", async () => {
    server.use(
      ...detailHandlers({
        runs: [
          makeRun({
            workflow_run_id: "WR-NO-SESSION-STOPPED",
            status: "failed",
            trace_id: "trace-no-session-stopped",
            session_id: null,
          }),
        ],
      }),
    );

    renderAt(<WorkflowDetail />, "/workflows/WF-1", "/workflows/:workflowId");
    await userEvent.click(await screen.findByTestId("tab-runs"));
    await userEvent.click(await screen.findByTestId("run-WR-NO-SESSION-STOPPED"));

    expect(await screen.findByTestId("workflow-session-memory-missing")).toHaveTextContent(
      "This run stopped without setting a shared session_id",
    );
    expect(screen.getByTestId("workflow-session-memory-missing")).toHaveTextContent(
      "Inspect the debugger and recovery panels",
    );
    expect(screen.getByTestId("workflow-session-memory-missing")).toHaveTextContent(
      "retry or rerun it with the session_id you want to reuse",
    );
  });

  it("turns session-memory lookup failures into stopped-run recovery guidance", async () => {
    server.use(
      http.get(`${API_BASE}/workflows/WF-1/session-memory`, () =>
        HttpResponse.json(
          { detail: "session memory backend timed out" },
          { status: 503 },
        ),
      ),
      ...detailHandlers({
        runs: [
          makeRun({
            workflow_run_id: "WR-SESSION-ERROR",
            status: "failed",
            trace_id: "trace-session-error",
            session_id: "thread-error",
          }),
        ],
      }),
    );

    renderAt(<WorkflowDetail />, "/workflows/WF-1", "/workflows/:workflowId");
    await userEvent.click(await screen.findByTestId("tab-runs"));
    await userEvent.click(await screen.findByTestId("run-WR-SESSION-ERROR"));

    expect(await screen.findByTestId("workflow-session-memory-error")).toHaveTextContent(
      "Session memory could not be loaded for this stopped run",
    );
    expect(screen.getByTestId("workflow-session-memory-error")).toHaveTextContent(
      "Inspect the debugger and recovery panels",
    );
    expect(screen.getByTestId("workflow-session-memory-error")).toHaveTextContent(
      "rerun with the same session_id",
    );
    expect(screen.getByTestId("workflow-session-memory-error")).toHaveTextContent(
      "Latest lookup error: session memory backend timed out",
    );
  });

  it("turns checkpoint lookup failures into stopped-run recovery guidance in workflow detail", async () => {
    server.use(
      http.get(`${API_BASE}/workflow-runs/WR-CHECKPOINT-ERROR/checkpoints`, () =>
        HttpResponse.json(
          { detail: "checkpoint store timed out" },
          { status: 503 },
        ),
      ),
      ...detailHandlers({
        runs: [
          makeRun({
            workflow_run_id: "WR-CHECKPOINT-ERROR",
            status: "failed",
            trace_id: "trace-checkpoint-error",
            summary: {
              output: "",
              node_path: ["start", "support_agent"],
              steps: [],
              resume_checkpoint_id: "CHK-error",
            },
          }),
        ],
      }),
    );

    renderAt(<WorkflowDetail />, "/workflows/WF-1", "/workflows/:workflowId");
    await userEvent.click(await screen.findByTestId("tab-runs"));
    await userEvent.click(await screen.findByTestId("run-WR-CHECKPOINT-ERROR"));

    expect(await screen.findByTestId("workflow-run-checkpoints-error")).toHaveTextContent(
      "Resume checkpoints could not be loaded for this stopped run.",
    );
    expect(screen.getByTestId("workflow-run-checkpoints-error")).toHaveTextContent(
      "Inspect the recovery, debugger, and lineage panels",
    );
    expect(screen.getByTestId("workflow-run-checkpoints-error")).toHaveTextContent(
      "checkpoint trail could be restored",
    );
    expect(screen.getByTestId("workflow-run-checkpoints-error")).toHaveTextContent(
      "Latest checkpoint error: checkpoint store timed out",
    );
  });

  it("turns approval lookup failures into stopped-run recovery guidance in workflow detail", async () => {
    server.use(
      http.get(`${API_BASE}/workflow-runs/WR-APPROVAL-ERROR/approvals`, () =>
        HttpResponse.json(
          { detail: "approval store timed out" },
          { status: 503 },
        ),
      ),
      ...detailHandlers({
        capabilities: {
          queue_enabled: true,
          supports_async_submit: true,
          supports_cancel: true,
          supports_retry: true,
          supports_resume: true,
          runtime_approvals_enabled: true,
          checkpointing_enabled: true,
          event_backend: "in_process",
        },
        runs: [
          makeRun({
            workflow_run_id: "WR-APPROVAL-ERROR",
            status: "failed",
            current_node_id: "tool_gate",
            trace_id: "trace-approval-error",
            summary: {
              output: "",
              node_path: ["start", "tool_gate"],
              steps: [],
              resume_checkpoint_id: "CHK-approval-error",
            },
          }),
        ],
        runCheckpointsById: {
          "WR-APPROVAL-ERROR": [
            {
              checkpoint_id: "CHK-approval-error",
              workflow_run_id: "WR-APPROVAL-ERROR",
              project_id: null,
              sequence: 1,
              node_id: "tool_gate",
              state_blob: {
                kind: "runtime_approval",
                node_id: "tool_gate",
              },
              created_at: NOW,
            },
          ],
        },
      }),
    );

    renderAt(<WorkflowDetail />, "/workflows/WF-1", "/workflows/:workflowId");
    await userEvent.click(await screen.findByTestId("tab-runs"));
    await userEvent.click(await screen.findByTestId("run-WR-APPROVAL-ERROR"));

    expect(await screen.findByTestId("workflow-run-recovery-approvals-error")).toHaveTextContent(
      "Runtime approval history could not be loaded for this stopped run.",
    );
    expect(screen.getByTestId("workflow-run-recovery-approvals-error")).toHaveTextContent(
      "Recovery diagnostics may still show checkpoints and lifecycle history",
    );
    expect(screen.getByTestId("workflow-run-recovery-approvals-error")).toHaveTextContent(
      "Latest approval error: approval store timed out",
    );
    expect(screen.getByTestId("workflow-run-recovery-panel")).toHaveTextContent("Terminal state");
  });

  it("shows an explicit resume note when approval records fail to load for a paused detail run", async () => {
    server.use(
      http.get(`${API_BASE}/workflow-runs/WR-APPROVAL-LOAD/approvals`, () =>
        HttpResponse.json(
          { detail: "approval store timed out" },
          { status: 503 },
        ),
      ),
      ...detailHandlers({
        capabilities: {
          queue_enabled: true,
          supports_async_submit: true,
          supports_cancel: false,
          supports_retry: false,
          supports_resume: true,
          runtime_approvals_enabled: true,
          checkpointing_enabled: true,
          event_backend: "in_process",
        },
        runs: [
          makeRun({
            workflow_run_id: "WR-APPROVAL-LOAD",
            status: "waiting_approval",
            current_node_id: "tool_gate",
            summary: {
              resume_checkpoint_id: "CHK-approval-load",
            },
          }),
        ],
        runCheckpointsById: {
          "WR-APPROVAL-LOAD": [
            {
              checkpoint_id: "CHK-approval-load",
              workflow_run_id: "WR-APPROVAL-LOAD",
              project_id: null,
              sequence: 1,
              node_id: "tool_gate",
              state_blob: {
                kind: "runtime_approval",
                node_id: "tool_gate",
              },
              created_at: NOW,
            },
          ],
        },
      }),
    );

    renderAt(<WorkflowDetail />, "/workflows/WF-1", "/workflows/:workflowId");
    await userEvent.click(await screen.findByTestId("tab-runs"));
    await userEvent.click(await screen.findByTestId("run-WR-APPROVAL-LOAD"));

    expect(await screen.findByTestId("run-resume-capability-note")).toHaveTextContent(
      "Manual resume is unavailable because runtime approval records could not be loaded.",
    );
    expect(screen.getByTestId("run-resume-capability-note")).toHaveTextContent(
      "inspect recovery diagnostics before continuing this run",
    );
    expect(screen.getByTestId("run-approval-capability-note")).toHaveTextContent(
      "Approval actions are unavailable because runtime approval records could not be loaded.",
    );
    expect(screen.getByTestId("run-approval-capability-note")).toHaveTextContent(
      "inspect recovery diagnostics before continuing this run",
    );
    expect(screen.queryByTestId("run-resume")).not.toBeInTheDocument();
  });

  it("turns inherited source-checkpoint lookup failures into stopped-run guidance in workflow detail", async () => {
    server.use(
      http.get(`${API_BASE}/workflow-runs/WR-SOURCE/checkpoints`, () =>
        HttpResponse.json(
          { detail: "source checkpoint store timed out" },
          { status: 503 },
        ),
      ),
      ...detailHandlers({
        runs: [
          makeRun({
            workflow_run_id: "WR-RETRIED",
            status: "failed",
            trace_id: "trace-retried",
            summary: {
              output: "",
              node_path: ["start", "support_agent"],
              steps: [],
              resume_checkpoint_id: "SRC-CHK-1",
              resume_checkpoint_run_id: "WR-SOURCE",
              retry_mode: "checkpoint",
            },
          }),
        ],
        runCheckpointsById: {
          "WR-RETRIED": [],
        },
      }),
    );

    renderAt(<WorkflowDetail />, "/workflows/WF-1", "/workflows/:workflowId");
    await userEvent.click(await screen.findByTestId("tab-runs"));
    await userEvent.click(await screen.findByTestId("run-WR-RETRIED"));

    expect(await screen.findByTestId("workflow-run-checkpoint-source-error")).toHaveTextContent(
      "CALIBER could not load the original source checkpoint details",
    );
    expect(screen.getByTestId("workflow-run-checkpoint-source-error")).toHaveTextContent(
      "Inspect the lineage, recovery, and debugger panels to trace where the inherited resume path failed",
    );
    expect(screen.getByTestId("workflow-run-checkpoint-source-error")).toHaveTextContent(
      "Latest source checkpoint error: source checkpoint store timed out",
    );
  });

  it("turns run-event lookup failures into stopped-run replay guidance in workflow detail", async () => {
    server.use(
      http.get(`${API_BASE}/workflow-runs/WR-EVENTS-ERROR/events`, () =>
        HttpResponse.json(
          { detail: "event store timed out" },
          { status: 503 },
        ),
      ),
      ...detailHandlers({
        runs: [
          makeRun({
            workflow_run_id: "WR-EVENTS-ERROR",
            status: "failed",
            trace_id: "trace-events-error",
            summary: {
              output: "",
              node_path: ["start", "support_agent"],
              steps: [],
            },
          }),
        ],
      }),
    );

    renderAt(<WorkflowDetail />, "/workflows/WF-1", "/workflows/:workflowId");
    await userEvent.click(await screen.findByTestId("tab-runs"));
    await userEvent.click(await screen.findByTestId("run-WR-EVENTS-ERROR"));

    expect(await screen.findByTestId("run-trace-replay-events-error")).toHaveTextContent(
      "Persisted run events could not be loaded for this stopped run.",
    );
    expect(screen.getByTestId("run-trace-replay-events-error")).toHaveTextContent(
      "inspect the recovery, checkpoint, and lineage panels",
    );
    expect(screen.getByTestId("run-trace-replay-events-error")).toHaveTextContent(
      "Latest event error: event store timed out",
    );
    expect(screen.getByTestId("run-debugger-events-error")).toHaveTextContent(
      "manifest-aware debugging are unavailable",
    );
    expect(screen.getByTestId("workflow-run-recovery-events-error")).toHaveTextContent(
      "Recovery timeline events could not be loaded for this stopped run.",
    );
    expect(screen.getByTestId("workflow-run-recovery-events-error")).toHaveTextContent(
      "Use the current status and debugger state above to trace where execution stopped.",
    );
    expect(screen.getByTestId("workflow-run-recovery-events-error")).toHaveTextContent(
      "Latest recovery event error: event store timed out",
    );
    expect(screen.getByTestId("run-recovery-section")).toBeInTheDocument();
    expect(screen.getByTestId("run-checkpoints-section")).toBeInTheDocument();
  });

  it("shows persisted resume checkpoints for a selected run", async () => {
    server.use(
      ...detailHandlers({
        runs: [
          makeRun({
            workflow_run_id: "WR-CP",
            status: "waiting_event",
            current_node_id: "wait_gate",
            summary: {
              output: "",
              node_path: ["start", "wait_gate"],
              steps: [
                {
                  node_id: "wait_gate",
                  node_type: "wait_for_event",
                  status: "ok",
                  output: "waiting",
                  tool_calls: [],
                  handoff_target: null,
                  detail: "paused at event gate",
                  duration_ms: 12,
                },
              ],
              resume_checkpoint_id: "CP-2",
            },
          }),
        ],
        runCheckpointsById: {
          "WR-CP": [
            {
              checkpoint_id: "CP-1",
              workflow_run_id: "WR-CP",
              project_id: null,
              sequence: 1,
              node_id: "wait_gate",
              state_blob: {
                kind: "wait_for_event",
                node_id: "wait_gate",
                expected_event_name: "ticket.created",
                input_by_port: { request: "initial payload" },
              },
              created_at: NOW,
            },
            {
              checkpoint_id: "CP-2",
              workflow_run_id: "WR-CP",
              project_id: null,
              sequence: 2,
              node_id: "wait_gate",
              state_blob: {
                kind: "wait_until",
                node_id: "wait_gate",
                wait_until: "2026-01-02T12:00:00Z",
                resume_at: "2026-01-02T12:00:00Z",
                timezone: "UTC",
                input_by_port: { request: "scheduled payload" },
              },
              created_at: NOW,
            },
          ],
        },
        runEventsById: {
          "WR-CP": [
            {
              event_id: 10,
              workflow_run_id: "WR-CP",
              project_id: null,
              sequence: 10,
              event_type: "workflow.run.waiting_event",
              node_id: "wait_gate",
              payload: { node_id: "wait_gate" },
              created_at: NOW,
            },
            {
              event_id: 11,
              workflow_run_id: "WR-CP",
              project_id: null,
              sequence: 11,
              event_type: "workflow.run.resumed",
              node_id: "wait_gate",
              payload: { node_id: "wait_gate" },
              created_at: NOW,
            },
          ],
        },
      }),
    );

    renderAt(<WorkflowDetail />, "/workflows/WF-1", "/workflows/:workflowId");
    await userEvent.click(await screen.findByTestId("tab-runs"));
    await userEvent.click(await screen.findByTestId("run-WR-CP"));

    expect(await screen.findByTestId("workflow-run-checkpoint-panel")).toBeInTheDocument();
    expect(screen.getByTestId("workflow-run-checkpoint-item-2")).toHaveTextContent("Scheduled wait");
    expect(screen.getByTestId("workflow-run-checkpoint-detail")).toHaveTextContent(
      "Current resume target",
    );
    expect(screen.getByTestId("workflow-run-checkpoint-detail")).toHaveTextContent(
      "2026-01-02T12:00:00Z",
    );
    expect(screen.getByTestId("workflow-run-checkpoint-json")).toHaveTextContent("\"timezone\": \"UTC\"");
    expect(screen.getByTestId("workflow-run-recovery-panel")).toHaveTextContent("Scheduled resume");
    expect(screen.getByTestId("workflow-run-recovery-events")).toHaveTextContent("Waiting for event");
    expect(screen.getByTestId("trace-path-step-1")).toHaveTextContent("Scheduled wait");
    expect(screen.getByTestId("trace-path-step-1")).toHaveTextContent("Resume target");
    expect(screen.getByTestId("trace-path-step-1")).toHaveTextContent("Paused for event");
    expect(screen.getByTestId("trace-path-step-1")).toHaveTextContent("Resumed");
    expect(screen.getByTestId("trace-path-step-1")).toHaveTextContent("Resume 2026-01-02T12:00:00Z (UTC)");
  });

  it("retries a failed run from the selected checkpoint", async () => {
    let retryBody: Record<string, unknown> | null = null;
    server.use(
      ...detailHandlers({
        capabilities: {
          queue_enabled: true,
          supports_async_submit: true,
          supports_cancel: true,
          supports_retry: true,
          supports_resume: true,
          runtime_approvals_enabled: true,
          checkpointing_enabled: true,
          event_backend: "database",
        },
        runs: [
          makeRun({
            workflow_run_id: "WR-CP-FAILED",
            status: "failed",
            current_node_id: "wait_gate",
            completed_at: NOW,
            summary: {
              output: "",
              node_path: ["start", "wait_gate"],
              steps: [
                {
                  node_id: "wait_gate",
                  node_type: "wait_until",
                  status: "error",
                  output: "timed out",
                  tool_calls: [],
                  handoff_target: null,
                  detail: "worker failed after pause",
                  duration_ms: 18,
                },
              ],
              resume_checkpoint_id: "CP-2",
            },
          }),
        ],
        runCheckpointsById: {
          "WR-CP-FAILED": [
            {
              checkpoint_id: "CP-1",
              workflow_run_id: "WR-CP-FAILED",
              project_id: null,
              sequence: 1,
              node_id: "wait_gate",
              state_blob: {
                kind: "wait_for_event",
                node_id: "wait_gate",
                expected_event_name: "ticket.created",
              },
              created_at: NOW,
            },
            {
              checkpoint_id: "CP-2",
              workflow_run_id: "WR-CP-FAILED",
              project_id: null,
              sequence: 2,
              node_id: "wait_gate",
              state_blob: {
                kind: "wait_until",
                node_id: "wait_gate",
                wait_until: "2026-01-02T12:00:00Z",
                timezone: "UTC",
              },
              created_at: NOW,
            },
          ],
        },
      }),
      http.post(`${API_BASE}/workflow-runs/WR-CP-FAILED/retry`, async ({ request }) => {
        retryBody = (await request.json()) as Record<string, unknown>;
        return HttpResponse.json(
          envelope(
            makeRun({
              workflow_run_id: "WR-CP-FAILED-RETRY",
              status: "queued",
              current_node_id: null,
              completed_at: null,
              summary: {
                output: "",
                node_path: [],
                steps: [],
                retry_of: "WR-CP-FAILED",
                retry_mode: "checkpoint",
                resume_checkpoint_id: "CP-1",
                resume_checkpoint_run_id: "WR-CP-FAILED",
              },
            }),
          ),
        );
      }),
    );

    const user = userEvent.setup();
    renderAt(<WorkflowDetail />, "/workflows/WF-1", "/workflows/:workflowId");
    await user.click(await screen.findByTestId("tab-runs"));
    await user.click(await screen.findByTestId("run-WR-CP-FAILED"));

    expect(await screen.findByTestId("workflow-run-checkpoint-retry")).toBeInTheDocument();
    await user.click(screen.getByTestId("workflow-run-checkpoint-item-1"));
    await user.click(screen.getByTestId("workflow-run-checkpoint-retry"));

    await waitFor(() =>
      expect(retryBody).toEqual({ checkpoint_id: "CP-1" }),
    );
    expect(await screen.findByTestId("workflow-run-message")).toHaveTextContent(
      "Retry from checkpoint CP-1 queued as WR-CP-FAILED-RETRY.",
    );
    expect(await screen.findByTestId("workflow-run-checkpoint-panel")).toHaveTextContent(
      "resumes from CP-1 captured on WR-CP-FAILED",
    );
    expect(screen.getByTestId("workflow-run-checkpoint-item-source")).toHaveTextContent(
      "Inherited",
    );
    expect(screen.queryByTestId("workflow-run-checkpoint-retry")).not.toBeInTheDocument();
  });

  it("pins run replay and debugger to the selected run's workflow version", async () => {
    const legacyManifest = {
      schema_version: 1,
      workflow_id: "WF-1",
      name: "Support v1",
      nodes: {
        start: { id: "start", type: "start", outputs: { user_message: { type: "string" } } },
        legacy_agent: {
          id: "legacy_agent",
          type: "agent",
          name: "legacy-agent",
          model: "inherit",
          instructions: { type: "inline", text: "legacy" },
          tools: [],
          inputs: { input: { type: "string" } },
          outputs: { final_output: { type: "string" } },
        },
        final: { id: "final", type: "output", inputs: { response: { type: "string" } } },
      },
      edges: [
        { id: "legacy-e1", from: "start", to: "legacy_agent", map: { user_message: "input" } },
        { id: "legacy-e2", from: "legacy_agent", to: "final", map: { final_output: "response" } },
      ],
    };

    server.use(
      ...detailHandlers({
        versions: [
          makeVersion({
            version_id: "WFV-2",
            version_number: 2,
            status: "published",
          }),
          makeVersion({
            version_id: "WFV-1",
            version_number: 1,
            status: "published",
            manifest: legacyManifest,
          }),
        ],
        runs: [
          makeRun({
            workflow_run_id: "WR-LEGACY",
            workflow_version_id: "WFV-1",
            status: "completed",
            summary: {
              node_path: ["start", "legacy_agent", "final"],
              steps: [
                {
                  node_id: "start",
                  node_type: "start",
                  status: "ok",
                  output: "legacy customer message",
                  tool_calls: [],
                  handoff_target: null,
                  detail: "captured trigger input",
                  duration_ms: 5,
                  input_by_port: {},
                  output_by_port: { user_message: "legacy customer message" },
                },
                {
                  node_id: "legacy_agent",
                  node_type: "agent",
                  status: "ok",
                  output: "Legacy answer",
                  tool_calls: [],
                  handoff_target: null,
                  detail: "legacy handler",
                  duration_ms: 11,
                  input_by_port: { input: "legacy customer message" },
                  output_by_port: { final_output: "Legacy answer" },
                },
              ],
            },
          }),
        ],
      }),
    );

    renderAt(<WorkflowDetail />, "/workflows/WF-1", "/workflows/:workflowId");
    await userEvent.click(await screen.findByTestId("tab-runs"));
    await userEvent.click(await screen.findByTestId("run-WR-LEGACY"));

    expect(await screen.findByTestId("run-version-chip")).toHaveTextContent("workflow v1");
    expect(await screen.findByTestId("run-version-notice")).toHaveTextContent(
      "latest published version is v2",
    );
    expect(await screen.findByTestId("workflow-run-debugger")).toBeInTheDocument();
    expect(screen.getByTestId("step-preview-input")).toHaveTextContent("start:");
    expect(screen.getByTestId("step-preview-input")).toHaveTextContent("legacy customer message");
  });

  it("falls back to directly loading a historical workflow version when the run manifest is unavailable", async () => {
    const legacyManifest = {
      schema_version: 1,
      workflow_id: "WF-1",
      name: "Support v1",
      nodes: {
        start: { id: "start", type: "start", outputs: { user_message: { type: "string" } } },
        legacy_agent: {
          id: "legacy_agent",
          type: "agent",
          name: "legacy-agent",
          model: "inherit",
          instructions: { type: "inline", text: "legacy" },
          tools: [],
          inputs: { input: { type: "string" } },
          outputs: { final_output: { type: "string" } },
        },
        final: { id: "final", type: "output", inputs: { response: { type: "string" } } },
      },
      edges: [
        { id: "legacy-e1", from: "start", to: "legacy_agent", map: { user_message: "input" } },
        { id: "legacy-e2", from: "legacy_agent", to: "final", map: { final_output: "response" } },
      ],
    };

    server.use(
      http.get(`${API_BASE}/workflow-versions/WFV-1`, () =>
        HttpResponse.json(
          envelope(
            makeVersion({
              version_id: "WFV-1",
              version_number: 1,
              status: "published",
              manifest: legacyManifest,
            }),
          ),
        ),
      ),
      ...detailHandlers({
        versions: [
          makeVersion({
            version_id: "WFV-2",
            version_number: 2,
            status: "published",
          }),
        ],
        runs: [
          makeRun({
            workflow_run_id: "WR-LEGACY-FALLBACK",
            workflow_version_id: "WFV-1",
            status: "completed",
            summary: {
              node_path: ["start", "legacy_agent", "final"],
              steps: [
                {
                  node_id: "start",
                  node_type: "start",
                  status: "ok",
                  output: "legacy customer message",
                  tool_calls: [],
                  handoff_target: null,
                  detail: "captured trigger input",
                  duration_ms: 5,
                  input_by_port: {},
                  output_by_port: { user_message: "legacy customer message" },
                },
                {
                  node_id: "legacy_agent",
                  node_type: "agent",
                  status: "ok",
                  output: "Legacy answer",
                  tool_calls: [],
                  handoff_target: null,
                  detail: "legacy handler",
                  duration_ms: 11,
                  input_by_port: { input: "legacy customer message" },
                  output_by_port: { final_output: "Legacy answer" },
                },
              ],
            },
          }),
        ],
        runManifestsById: {
          "WR-LEGACY-FALLBACK": null,
        },
      }),
    );

    renderAt(<WorkflowDetail />, "/workflows/WF-1", "/workflows/:workflowId");
    await userEvent.click(await screen.findByTestId("tab-runs"));
    await userEvent.click(await screen.findByTestId("run-WR-LEGACY-FALLBACK"));

    expect(await screen.findByTestId("run-version-chip")).toHaveTextContent("workflow v1");
    expect(await screen.findByTestId("run-version-notice")).toHaveTextContent(
      "latest published version is v2",
    );
    expect(await screen.findByTestId("run-manifest-fallback-notice")).toHaveTextContent(
      "workflow version v1 because the persisted run manifest could not be loaded separately",
    );
    expect(screen.getByTestId("run-manifest-fallback-notice")).toHaveTextContent(
      "confirm the terminal result with the debugger, final outputs, and generated artifacts",
    );
    expect(await screen.findByTestId("workflow-run-debugger")).toBeInTheDocument();
    expect(screen.getByTestId("trace-steps")).toHaveTextContent("legacy_agent");
    expect(screen.queryByTestId("run-version-missing")).not.toBeInTheDocument();
  });

  it("turns saved-version fallback into active-gate guidance when the run is still waiting", async () => {
    const legacyManifest = {
      schema_version: 1,
      workflow_id: "WF-1",
      name: "Support v1",
      nodes: {
        start: { id: "start", type: "start", outputs: { user_message: { type: "string" } } },
        wait_gate: {
          id: "wait_gate",
          type: "wait_for_event",
          event_name: "ticket.approved",
          correlation_key: "ticket_id",
        },
      },
      edges: [
        { id: "legacy-e1", from: "start", to: "wait_gate", map: { user_message: "ticket_id" } },
      ],
    };

    server.use(
      http.get(`${API_BASE}/workflow-versions/WFV-1`, () =>
        HttpResponse.json(
          envelope(
            makeVersion({
              version_id: "WFV-1",
              version_number: 1,
              status: "published",
              manifest: legacyManifest,
            }),
          ),
        ),
      ),
      ...detailHandlers({
        versions: [
          makeVersion({
            version_id: "WFV-2",
            version_number: 2,
            status: "published",
          }),
        ],
        runs: [
          makeRun({
            workflow_run_id: "WR-LEGACY-WAIT",
            workflow_version_id: "WFV-1",
            status: "waiting_event",
            current_node_id: "wait_gate",
            summary: {
              node_path: ["start", "wait_gate"],
              steps: [],
              resume_checkpoint_id: "CP-LEGACY-WAIT",
            },
          }),
        ],
        runManifestsById: {
          "WR-LEGACY-WAIT": null,
        },
        runCheckpointsById: {
          "WR-LEGACY-WAIT": [
            {
              checkpoint_id: "CP-LEGACY-WAIT",
              workflow_run_id: "WR-LEGACY-WAIT",
              project_id: null,
              sequence: 1,
              node_id: "wait_gate",
              state_blob: {
                kind: "wait_for_event",
                node_id: "wait_gate",
                expected_event_name: "ticket.approved",
              },
              created_at: NOW,
            },
          ],
        },
      }),
    );

    renderAt(<WorkflowDetail />, "/workflows/WF-1", "/workflows/:workflowId");
    await userEvent.click(await screen.findByTestId("tab-runs"));
    await userEvent.click(await screen.findByTestId("run-WR-LEGACY-WAIT"));

    expect(await screen.findByTestId("run-manifest-fallback-notice")).toHaveTextContent(
      "workflow version v1 because the persisted run manifest could not be loaded separately",
    );
    expect(screen.getByTestId("run-manifest-fallback-notice")).toHaveTextContent(
      "rely on the recovery and checkpoint panels for authoritative resume state",
    );
    expect(screen.queryByTestId("run-version-missing")).not.toBeInTheDocument();
  });

  it("keeps the persisted workflow version number visible when the run manifest exists but the version row is gone", async () => {
    const legacyManifest = {
      schema_version: 1,
      workflow_id: "WF-1",
      name: "Support v1",
      nodes: {
        start: { id: "start", type: "start", outputs: { user_message: { type: "string" } } },
        legacy_agent: {
          id: "legacy_agent",
          type: "agent",
          name: "legacy-agent",
          model: "inherit",
          instructions: { type: "inline", text: "legacy" },
          tools: [],
          inputs: { input: { type: "string" } },
          outputs: { final_output: { type: "string" } },
        },
        final: { id: "final", type: "output", inputs: { response: { type: "string" } } },
      },
      edges: [
        { id: "legacy-e1", from: "start", to: "legacy_agent", map: { user_message: "input" } },
        { id: "legacy-e2", from: "legacy_agent", to: "final", map: { final_output: "response" } },
      ],
    };

    server.use(
      ...detailHandlers({
        versions: [
          makeVersion({
            version_id: "WFV-2",
            version_number: 2,
            status: "published",
          }),
        ],
        runs: [
          makeRun({
            workflow_run_id: "WR-LEGACY-PERSISTED",
            workflow_version_id: "WFV-1",
            status: "completed",
            summary: {
              manifest_mode: "saved_version",
              workflow_version_number: 1,
              node_path: ["start", "legacy_agent", "final"],
              steps: [
                {
                  node_id: "start",
                  node_type: "start",
                  status: "ok",
                  output: "legacy customer message",
                  tool_calls: [],
                  handoff_target: null,
                  detail: "captured trigger input",
                  duration_ms: 5,
                  input_by_port: {},
                  output_by_port: { user_message: "legacy customer message" },
                },
                {
                  node_id: "legacy_agent",
                  node_type: "agent",
                  status: "ok",
                  output: "Legacy answer",
                  tool_calls: [],
                  handoff_target: null,
                  detail: "legacy handler",
                  duration_ms: 11,
                  input_by_port: { input: "legacy customer message" },
                  output_by_port: { final_output: "Legacy answer" },
                },
              ],
            },
          }),
        ],
        runManifestsById: {
          "WR-LEGACY-PERSISTED": {
            workflow_run_id: "WR-LEGACY-PERSISTED",
            workflow_id: "WF-1",
            workflow_version_id: "WFV-1",
            manifest_mode: "saved_version",
            manifest_hash: "hash-legacy-persisted",
            manifest: legacyManifest,
          },
        },
      }),
    );

    renderAt(<WorkflowDetail />, "/workflows/WF-1", "/workflows/:workflowId");
    await userEvent.click(await screen.findByTestId("tab-runs"));
    await userEvent.click(await screen.findByTestId("run-WR-LEGACY-PERSISTED"));

    expect(await screen.findByTestId("run-version-chip")).toHaveTextContent("workflow v1");
    expect(await screen.findByTestId("run-version-notice")).toHaveTextContent(
      "latest published version is v2",
    );
    expect(await screen.findByTestId("workflow-run-debugger")).toBeInTheDocument();
    expect(screen.getByTestId("trace-steps")).toHaveTextContent("legacy_agent");
    expect(screen.queryByTestId("run-version-missing")).not.toBeInTheDocument();
  });

  it("turns reconstructed run graphs into completed-run replay guidance", async () => {
    server.use(
      ...detailHandlers({
        versions: [],
        runs: [
          makeRun({
            workflow_run_id: "WR-SYNTH-DONE",
            workflow_version_id: null,
            status: "completed",
            completed_at: NOW,
            current_node_id: "final",
            summary: {
              output: "synthetic result",
              node_path: ["start", "final"],
              steps: [
                {
                  node_id: "start",
                  node_type: "start",
                  status: "ok",
                  output: "customer message",
                  tool_calls: [],
                  handoff_target: null,
                  detail: "captured input",
                  duration_ms: 4,
                  output_by_port: { user_message: "customer message" },
                },
              ],
            },
          }),
        ],
        runManifestsById: {
          "WR-SYNTH-DONE": null,
        },
      }),
    );

    renderAt(<WorkflowDetail />, "/workflows/WF-1", "/workflows/:workflowId");
    await userEvent.click(await screen.findByTestId("tab-runs"));
    await userEvent.click(await screen.findByTestId("run-WR-SYNTH-DONE"));

    expect(await screen.findByTestId("run-manifest-fallback-notice")).toHaveTextContent(
      "graph reconstructed from recorded run history and checkpoints",
    );
    expect(screen.getByTestId("run-manifest-fallback-notice")).toHaveTextContent(
      "confirm the terminal result with the debugger, final outputs, and generated artifacts",
    );
    expect(screen.queryByTestId("run-version-missing")).not.toBeInTheDocument();
  });

  it("pins run replay and debugger to the queued draft snapshot for a selected run", async () => {
    const snapshotManifest = {
      schema_version: 1,
      workflow_id: "WF-1",
      name: "Support draft snapshot",
      nodes: {
        start: { id: "start", type: "start", outputs: { user_message: { type: "string" } } },
        snapshot_agent: {
          id: "snapshot_agent",
          type: "agent",
          name: "snapshot-agent",
          model: "inherit",
          instructions: { type: "inline", text: "snapshot" },
          tools: [],
          inputs: { input: { type: "string" } },
          outputs: { final_output: { type: "string" } },
        },
        final: { id: "final", type: "output", inputs: { response: { type: "string" } } },
      },
      edges: [
        { id: "snapshot-e1", from: "start", to: "snapshot_agent", map: { user_message: "input" } },
        { id: "snapshot-e2", from: "snapshot_agent", to: "final", map: { final_output: "response" } },
      ],
    };

    server.use(
      ...detailHandlers({
        versions: [
          makeVersion({
            version_id: "WFV-2",
            version_number: 2,
            status: "published",
          }),
        ],
        runs: [
          makeRun({
            workflow_run_id: "WR-SNAPSHOT",
            workflow_version_id: "WFV-2",
            status: "completed",
            current_node_id: "snapshot_agent",
            summary: {
              manifest_mode: "snapshot",
              node_path: ["start", "snapshot_agent", "final"],
              steps: [
                {
                  node_id: "start",
                  node_type: "start",
                  status: "ok",
                  output: "snapshot customer message",
                  tool_calls: [],
                  handoff_target: null,
                  detail: "captured trigger input",
                  duration_ms: 5,
                  input_by_port: {},
                  output_by_port: { user_message: "snapshot customer message" },
                },
                {
                  node_id: "snapshot_agent",
                  node_type: "agent",
                  status: "ok",
                  output: "Snapshot answer",
                  tool_calls: [],
                  handoff_target: null,
                  detail: "snapshot handler",
                  duration_ms: 11,
                  input_by_port: { input: "snapshot customer message" },
                  output_by_port: { final_output: "Snapshot answer" },
                },
              ],
            },
          }),
        ],
        runManifestsById: {
          "WR-SNAPSHOT": {
            workflow_run_id: "WR-SNAPSHOT",
            workflow_id: "WF-1",
            workflow_version_id: "WFV-2",
            manifest_mode: "snapshot",
            manifest_hash: "hash-snapshot-run",
            manifest: snapshotManifest,
          },
        },
      }),
    );

    renderAt(<WorkflowDetail />, "/workflows/WF-1", "/workflows/:workflowId");
    await userEvent.click(await screen.findByTestId("tab-runs"));
    await userEvent.click(await screen.findByTestId("run-WR-SNAPSHOT"));

    expect(await screen.findByTestId("run-version-chip")).toHaveTextContent("workflow v2");
    expect(await screen.findByTestId("run-manifest-mode-chip")).toHaveTextContent("draft snapshot");
    expect(await screen.findByTestId("run-manifest-mode-notice")).toHaveTextContent(
      "queued draft snapshot captured for this run",
    );
    expect(await screen.findByTestId("workflow-run-debugger")).toBeInTheDocument();
    expect(screen.getByTestId("trace-steps")).toHaveTextContent("snapshot_agent");
    expect(screen.getByTestId("step-preview-input")).toHaveTextContent("snapshot customer message");
  });

  it("keeps selected run details visible when only the per-run snapshot remains", async () => {
    const snapshotManifest = {
      schema_version: 1,
      workflow_id: "WF-1",
      name: "Support draft snapshot",
      nodes: {
        start: { id: "start", type: "start", outputs: { user_message: { type: "string" } } },
        snapshot_agent: {
          id: "snapshot_agent",
          type: "agent",
          name: "snapshot-agent",
          model: "inherit",
          instructions: { type: "inline", text: "snapshot" },
          tools: [],
          inputs: { input: { type: "string" } },
          outputs: { final_output: { type: "string" } },
        },
        final: { id: "final", type: "output", inputs: { response: { type: "string" } } },
      },
      edges: [
        { id: "snapshot-e1", from: "start", to: "snapshot_agent", map: { user_message: "input" } },
        { id: "snapshot-e2", from: "snapshot_agent", to: "final", map: { final_output: "response" } },
      ],
    };

    server.use(
      ...detailHandlers({
        versions: [],
        runs: [
          makeRun({
            workflow_run_id: "WR-ORPHAN-SNAPSHOT",
            workflow_version_id: "WFV-2",
            status: "completed",
            current_node_id: "snapshot_agent",
            summary: {
              manifest_mode: "snapshot",
              workflow_version_number: 2,
              node_path: ["start", "snapshot_agent", "final"],
              steps: [
                {
                  node_id: "start",
                  node_type: "start",
                  status: "ok",
                  output: "snapshot customer message",
                  tool_calls: [],
                  handoff_target: null,
                  detail: "captured trigger input",
                  duration_ms: 5,
                  input_by_port: {},
                  output_by_port: { user_message: "snapshot customer message" },
                },
                {
                  node_id: "snapshot_agent",
                  node_type: "agent",
                  status: "ok",
                  output: "Snapshot answer",
                  tool_calls: [],
                  handoff_target: null,
                  detail: "snapshot handler",
                  duration_ms: 11,
                  input_by_port: { input: "snapshot customer message" },
                  output_by_port: { final_output: "Snapshot answer" },
                },
              ],
            },
          }),
        ],
        runManifestsById: {
          "WR-ORPHAN-SNAPSHOT": {
            workflow_run_id: "WR-ORPHAN-SNAPSHOT",
            workflow_id: "WF-1",
            workflow_version_id: "WFV-2",
            manifest_mode: "snapshot",
            manifest_hash: "hash-orphan-snapshot",
            manifest: snapshotManifest,
          },
        },
      }),
    );

    renderAt(<WorkflowDetail />, "/workflows/WF-1", "/workflows/:workflowId");
    await userEvent.click(await screen.findByTestId("tab-runs"));
    await userEvent.click(await screen.findByTestId("run-WR-ORPHAN-SNAPSHOT"));

    expect(await screen.findByTestId("run-version-chip")).toHaveTextContent("workflow v2");
    expect(await screen.findByTestId("run-manifest-mode-chip")).toHaveTextContent("draft snapshot");
    expect(await screen.findByTestId("run-manifest-mode-notice")).toHaveTextContent(
      "queued draft snapshot captured for this run",
    );
    expect(await screen.findByTestId("workflow-run-debugger")).toBeInTheDocument();
    expect(screen.getByTestId("trace-steps")).toHaveTextContent("snapshot_agent");
    expect(screen.queryByTestId("run-version-missing")).not.toBeInTheDocument();
    expect(screen.queryByTestId("run-version-notice")).not.toBeInTheDocument();
  });

  it("turns missing run graphs into recovery guidance when only checkpoint evidence remains", async () => {
    server.use(
      ...detailHandlers({
        versions: [],
        runs: [
          makeRun({
            workflow_run_id: "WR-NO-GRAPH",
            workflow_version_id: null,
            current_node_id: null,
            summary: {
              workflow_version_number: 4,
            },
          }),
        ],
        runManifestsById: {
          "WR-NO-GRAPH": null,
        },
        runCheckpointsById: {
          "WR-NO-GRAPH": [
            {
              checkpoint_id: "CHK-NO-GRAPH",
              workflow_run_id: "WR-NO-GRAPH",
              project_id: null,
              sequence: 1,
              node_id: null,
              state_blob: {
                kind: "runtime_approval",
                output: "awaiting approval",
              },
              created_at: NOW,
            },
          ],
        },
      }),
    );

    renderAt(<WorkflowDetail />, "/workflows/WF-1", "/workflows/:workflowId");
    await userEvent.click(await screen.findByTestId("tab-runs"));
    await userEvent.click(await screen.findByTestId("run-WR-NO-GRAPH"));

    expect(await screen.findByTestId("run-version-missing")).toHaveTextContent(
      "workflow version v4 is not available in the loaded workflow versions",
    );
    expect(screen.getByTestId("run-version-missing")).toHaveTextContent(
      "Use the checkpoint, recovery, and retry-lineage panels below to keep tracing the persisted execution evidence until the graph can be restored.",
    );
    expect(screen.queryByTestId("workflow-run-debugger")).not.toBeInTheDocument();
    expect(screen.getByTestId("run-checkpoints-section")).toBeInTheDocument();
    expect(screen.getByTestId("run-recovery-section")).toBeInTheDocument();
    expect(screen.getByTestId("run-lineage-section")).toBeInTheDocument();
  });

  it("shows retry lineage for a selected run and lets operators navigate attempts", async () => {
    server.use(
      ...detailHandlers({
        runs: [
          makeRun({
            workflow_run_id: "WR-A1",
            status: "failed",
            attempt_number: 1,
            parent_run_id: null,
            error_summary: "first attempt failed",
          }),
          makeRun({
            workflow_run_id: "WR-A2",
            status: "queued",
            attempt_number: 2,
            parent_run_id: "WR-A1",
          }),
          makeRun({
            workflow_run_id: "WR-A3",
            status: "running",
            attempt_number: 3,
            parent_run_id: "WR-A2",
          }),
        ],
      }),
    );

    renderAt(<WorkflowDetail />, "/workflows/WF-1", "/workflows/:workflowId");
    await userEvent.click(await screen.findByTestId("tab-runs"));
    await userEvent.click(await screen.findByTestId("run-WR-A2"));

    expect(await screen.findByTestId("workflow-run-lineage-panel")).toBeInTheDocument();
    expect(screen.getByTestId("workflow-run-lineage-panel")).toHaveTextContent("Attempt 2 of 3");
    expect(screen.getByTestId("workflow-run-lineage-item-WR-A1")).toHaveTextContent("root");
    expect(screen.getByTestId("workflow-run-lineage-item-WR-A3")).toHaveTextContent("child");

    await userEvent.click(screen.getByTestId("workflow-run-lineage-item-WR-A1"));
    expect(await screen.findByTestId("workflow-run-lineage-panel")).toHaveTextContent("Attempt 1 of 3");
  });

  it("keeps fallback retry lineage visible when canonical lineage lookup fails", async () => {
    server.use(
      http.get(`${API_BASE}/workflow-runs/WR-A2/lineage`, () =>
        HttpResponse.json(
          { detail: "lineage store timed out" },
          { status: 503 },
        ),
      ),
      ...detailHandlers({
        runs: [
          makeRun({
            workflow_run_id: "WR-A1",
            status: "failed",
            attempt_number: 1,
            parent_run_id: null,
            error_summary: "first attempt failed",
          }),
          makeRun({
            workflow_run_id: "WR-A2",
            status: "failed",
            attempt_number: 2,
            parent_run_id: "WR-A1",
            error_summary: "second attempt failed",
          }),
          makeRun({
            workflow_run_id: "WR-A3",
            status: "queued",
            attempt_number: 3,
            parent_run_id: "WR-A2",
          }),
        ],
      }),
    );

    renderAt(<WorkflowDetail />, "/workflows/WF-1", "/workflows/:workflowId");
    await userEvent.click(await screen.findByTestId("tab-runs"));
    await userEvent.click(await screen.findByTestId("run-WR-A2"));

    expect(await screen.findByTestId("workflow-run-lineage-error")).toHaveTextContent(
      "Canonical retry lineage could not be loaded for this stopped run.",
    );
    expect(screen.getByTestId("workflow-run-lineage-error")).toHaveTextContent(
      "CALIBER is showing the nearest retry chain reconstructed from the loaded runs instead",
    );
    expect(screen.getByTestId("workflow-run-lineage-error")).toHaveTextContent(
      "Latest lineage error: lineage store timed out",
    );
    expect(screen.getByTestId("workflow-run-lineage-panel")).toHaveTextContent("Attempt 2 of 3");
    expect(screen.getByTestId("workflow-run-lineage-item-WR-A1")).toHaveTextContent("root");
    expect(screen.getByTestId("workflow-run-lineage-item-WR-A3")).toHaveTextContent("child");
  });

  it("links selected runs to MLflow when the workflow experiment id is known", async () => {
    server.use(
      ...detailHandlers({
        workflow: { default_experiment_id: "42" },
        runs: [
          makeRun({
            workflow_run_id: "WR-MLFLOW",
            status: "completed",
            trace_id: "trace-mlflow",
            mlflow_run_id: "mlflow-run-1",
          }),
        ],
      }),
    );

    renderAt(<WorkflowDetail />, "/workflows/WF-1", "/workflows/:workflowId");
    await userEvent.click(await screen.findByTestId("tab-runs"));
    await userEvent.click(await screen.findByTestId("run-WR-MLFLOW"));

    expect(await screen.findByTestId("run-mlflow-run-id")).toHaveAttribute(
      "href",
      "/?ui=mlflow#/experiments/42/runs/mlflow-run-1",
    );
  });
});

import { ToolDetail } from "@/pages/ToolDetail";
import { WorkflowVersionDetail } from "@/pages/WorkflowVersionDetail";

describe("WorkflowVersionDetail page", () => {
  it("renders the manifest, validates, and shows export mode metadata", async () => {
    server.use(
      http.get(`${API_BASE}/workflow-versions/WFV-1`, () =>
        HttpResponse.json(
          envelope(
            makeVersion({
              compiled_bundle: {
                generated_python: "def run(input_text: str):\n    return input_text\n",
                compiler_report: { export_mode: "runtime_ir" },
              },
            }),
          ),
        ),
      ),
      http.post(`${API_BASE}/workflow-versions/WFV-1/validate`, () =>
        HttpResponse.json(envelope({ valid: true, errors: [], warnings: [] })),
      ),
    );
    renderAt(<WorkflowVersionDetail />, "/workflow-versions/WFV-1", "/workflow-versions/:versionId");
    expect(await screen.findByTestId("version-detail")).toBeInTheDocument();
    expect(screen.getByTestId("vd-manifest")).toHaveTextContent("support_agent");
    expect(screen.getByTestId("vd-export-mode")).toHaveTextContent("Full runtime export");
    await userEvent.click(screen.getByTestId("vd-validate"));
    await waitFor(() =>
      expect(screen.getByTestId("wf-problems")).toHaveTextContent("No problems"),
    );
  });

  it("shows fresh compile outputs, requirements, and refetches the persisted version", async () => {
    let getVersionCalls = 0;
    server.use(
      http.get(`${API_BASE}/workflow-versions/WFV-1`, () => {
        getVersionCalls += 1;
        if (getVersionCalls >= 2) {
          return HttpResponse.json(
            envelope(
              makeVersion({
                compiler_version: "compiler-2",
                compiled_artifact_uri: "s3://compiled/workflows/WFV-1.py",
                compiled_bundle: {
                  generated_python: "def persisted_run(input_text: str):\n    return input_text.upper()\n",
                  compiler_report: { export_mode: "runtime_ir", persisted: true },
                  requirements: ["openai-agents>=0.1.0", "mlflow>=3.12,<4"],
                },
              }),
            ),
          );
        }
        return HttpResponse.json(envelope(makeVersion()));
      }),
      http.post(`${API_BASE}/workflow-versions/WFV-1/compile`, () =>
        HttpResponse.json(
          envelope({
            version_id: "WFV-1",
            compiled_artifact_uri: "s3://compiled/workflows/WFV-1.py",
            compiler_version: "compiler-2",
            manifest_hash: "hash-compiled",
            report: { export_mode: "runtime_ir", compile_path: "fresh" },
            generated_python: "def fresh_compile(input_text: str):\n    return input_text.upper()\n",
            requirements: ["openai-agents>=0.1.0", "mlflow>=3.12,<4"],
            compile_ms: 182,
            cached: false,
          }),
        ),
      ),
    );

    renderAt(<WorkflowVersionDetail />, "/workflow-versions/WFV-1", "/workflow-versions/:versionId");
    expect(await screen.findByTestId("version-detail")).toBeInTheDocument();

    await userEvent.click(screen.getByTestId("vd-compile"));

    await waitFor(() =>
      expect(screen.getByTestId("vd-compiled-code")).toHaveTextContent("def fresh_compile"),
    );
    expect(screen.getByTestId("vd-compile-summary")).toHaveTextContent("hash-compiled");
    expect(screen.getByTestId("vd-compile-summary")).toHaveTextContent("compiler-2");
    expect(screen.getByTestId("vd-compile-summary")).toHaveTextContent("WFV-1.py");
    expect(screen.getByTestId("vd-compile-summary")).toHaveTextContent("Fresh compile");
    expect(screen.getByTestId("vd-compile-summary")).toHaveTextContent("182 ms");
    expect(screen.getByTestId("vd-compile-requirements")).toHaveTextContent(
      "openai-agents>=0.1.0",
    );
    expect(screen.getByTestId("vd-compile-report")).toHaveTextContent("runtime_ir");

    await waitFor(() => expect(getVersionCalls).toBeGreaterThanOrEqual(2));
  });
});

function makeTool(overrides: Record<string, unknown> = {}) {
  return {
    tool_id: "TL-1",
    name: "lookup_policy",
    version: "1.0",
    description: "Reads policy",
    module_path: "m",
    callable_name: "lookup_policy",
    input_schema: null,
    output_schema: { type: "object", properties: { policy: { type: "string" } } },
    side_effect_level: "read",
    requires_approval: false,
    allow_in_preview: true,
    secret_refs: [],
    owner: "@test",
    status: "active",
    deprecated_at: null,
    successor_tool_id: null,
    created_at: NOW,
    updated_at: NOW,
    ...overrides,
  };
}

describe("ToolDetail page", () => {
  it("renders the tool, schema, and usage", async () => {
    server.use(
      http.get(`${API_BASE}/tools/TL-1`, () => HttpResponse.json(envelope(makeTool()))),
      http.get(`${API_BASE}/tools/TL-1/usage`, () =>
        HttpResponse.json(
          envelope({
            tool_id: "TL-1",
            name: "lookup_policy",
            usage: [
              { workflow_id: "WF-1", version_id: "WFV-1", version_number: 1, status: "published" },
            ],
          }),
        ),
      ),
    );
    renderAt(<ToolDetail />, "/tools/TL-1", "/tools/:toolId");
    expect(await screen.findByTestId("tool-detail")).toBeInTheDocument();
    expect(screen.getByTestId("tool-edit-description")).toHaveValue("Reads policy");
    expect(screen.getByTestId("tool-agent-binding")).toHaveTextContent("tool.lookup_policy.v1");
    expect(screen.getByTestId("tool-usage")).toHaveTextContent("WF-1");
  });

  it("deprecates the tool", async () => {
    let patched = false;
    server.use(
      http.get(`${API_BASE}/tools/TL-1`, () => HttpResponse.json(envelope(makeTool()))),
      http.get(`${API_BASE}/tools/TL-1/usage`, () =>
        HttpResponse.json(envelope({ tool_id: "TL-1", name: "lookup_policy", usage: [] })),
      ),
      http.patch(`${API_BASE}/tools/TL-1`, () => {
        patched = true;
        return HttpResponse.json(envelope(makeTool({ status: "deprecated" })));
      }),
    );
    renderAt(<ToolDetail />, "/tools/TL-1", "/tools/:toolId");
    await userEvent.click(await screen.findByTestId("tool-deprecate"));
    await waitFor(() => expect(patched).toBe(true));
  });

  it("edits tool metadata from the detail page", async () => {
    let current = makeTool();
    let patchBody: Record<string, unknown> | null = null;
    server.use(
      http.get(`${API_BASE}/tools/TL-1`, () => HttpResponse.json(envelope(current))),
      http.get(`${API_BASE}/tools/TL-1/usage`, () =>
        HttpResponse.json(envelope({ tool_id: "TL-1", name: "lookup_policy", usage: [] })),
      ),
      http.patch(`${API_BASE}/tools/TL-1`, async ({ request }) => {
        patchBody = (await request.json()) as Record<string, unknown>;
        current = makeTool({ ...current, ...patchBody });
        return HttpResponse.json(envelope(current));
      }),
    );
    renderAt(<ToolDetail />, "/tools/TL-1", "/tools/:toolId");
    await screen.findByTestId("tool-detail");

    await userEvent.clear(screen.getByTestId("tool-edit-description"));
    await userEvent.type(screen.getByTestId("tool-edit-description"), "Updated policy lookup");
    await userEvent.selectOptions(screen.getByTestId("tool-edit-side-effect"), "write");
    await userEvent.selectOptions(screen.getByTestId("tool-edit-status"), "deprecated");
    await userEvent.clear(screen.getByTestId("tool-edit-owner"));
    await userEvent.type(screen.getByTestId("tool-edit-owner"), "@ops");
    await userEvent.click(screen.getByTestId("tool-edit-requires-approval"));
    await userEvent.click(screen.getByTestId("tool-edit-allow-preview"));
    await userEvent.click(screen.getByTestId("tool-save"));

    await waitFor(() => expect(patchBody).not.toBeNull());
    expect(patchBody).toMatchObject({
      description: "Updated policy lookup",
      side_effect_level: "write",
      status: "deprecated",
      owner: "@ops",
      requires_approval: true,
      allow_in_preview: false,
      successor_tool_id: null,
    });
    expect(await screen.findByTestId("tool-save-status")).toHaveTextContent("Saved");
  });

  it("runs a tool with JSON input from the detail page", async () => {
    let runBody: Record<string, unknown> | null = null;
    server.use(
      http.get(`${API_BASE}/tools/TL-1`, () => HttpResponse.json(envelope(makeTool()))),
      http.get(`${API_BASE}/tools/TL-1/usage`, () =>
        HttpResponse.json(envelope({ tool_id: "TL-1", name: "lookup_policy", usage: [] })),
      ),
      http.post(`${API_BASE}/tools/TL-1/test-run`, async ({ request }) => {
        runBody = (await request.json()) as Record<string, unknown>;
        return HttpResponse.json(
          envelope({
            tool_id: "TL-1",
            output: { policy: "Refunds are available.", query: (runBody.input as { query: string }).query },
            mocked: false,
            duration_ms: 2,
            error: null,
          }),
        );
      }),
    );
    renderAt(<ToolDetail />, "/tools/TL-1", "/tools/:toolId");
    await screen.findByTestId("tool-detail");

    fireEvent.change(screen.getByTestId("tool-run-input"), {
      target: { value: '{"query":"refund"}' },
    });
    await userEvent.click(screen.getByTestId("tool-run"));

    await waitFor(() => expect(runBody).toEqual({ input: { query: "refund" } }));
    expect(await screen.findByTestId("tool-run-result")).toHaveTextContent("Refunds are available.");
    expect(screen.getByTestId("tool-run-result")).toHaveTextContent("\"mocked\": false");
  });

  it("validates tool run input before calling the backend", async () => {
    let called = false;
    server.use(
      http.get(`${API_BASE}/tools/TL-1`, () => HttpResponse.json(envelope(makeTool()))),
      http.get(`${API_BASE}/tools/TL-1/usage`, () =>
        HttpResponse.json(envelope({ tool_id: "TL-1", name: "lookup_policy", usage: [] })),
      ),
      http.post(`${API_BASE}/tools/TL-1/test-run`, () => {
        called = true;
        return HttpResponse.json(envelope({ tool_id: "TL-1", output: {}, mocked: false, duration_ms: 1, error: null }));
      }),
    );
    renderAt(<ToolDetail />, "/tools/TL-1", "/tools/:toolId");
    await screen.findByTestId("tool-detail");

    fireEvent.change(screen.getByTestId("tool-run-input"), { target: { value: "[]" } });
    await userEvent.click(screen.getByTestId("tool-run"));

    expect(await screen.findByTestId("tool-run-input-error")).toHaveTextContent("Tool input must be a JSON object.");
    expect(called).toBe(false);
  });
});
