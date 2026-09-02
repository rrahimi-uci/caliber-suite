/**
 * Workflows list surface — behavioural coverage for the restyled page:
 * summary tiles + status filters, search, empty/error states, the template
 * create flow, card navigation, and the (hardened) delete confirmation modal.
 *
 * Inline-rename submit semantics live in `workflows-inline-rename.test.tsx`;
 * this file covers the rename-commit guard (no double mutation) and the rest.
 */

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import {
  fireEvent,
  render,
  screen,
  waitFor,
  within,
} from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { http, HttpResponse } from "msw";
import { MemoryRouter, Route, Routes, useParams } from "react-router-dom";
import {
  afterAll,
  afterEach,
  beforeAll,
  describe,
  expect,
  it,
  vi,
} from "vitest";

import { Workflows } from "@/pages/Workflows";
import type {
  Workflow,
  WorkflowDeploymentBundle,
  WorkflowManifest,
  WorkflowTemplate,
} from "@/api/workflowTypes";
import { server } from "@/test/server";

const API_BASE = "/ajax-api/2.0/mlflow/caliber";
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

function envelope<T>(data: T): { data: T } {
  return { data };
}

function makeWorkflow(overrides: Partial<Workflow>): Workflow {
  return {
    workflow_id: "WF-x",
    name: "Workflow",
    description: "A workflow",
    owner: "@owner",
    status: "active",
    default_experiment_id: null,
    created_at: "2025-01-01T00:00:00Z",
    updated_at: "2025-01-01T00:00:00Z",
    ...overrides,
  };
}

/** 2 active, 1 paused, 1 archived → tile/chip counts of 4 / 2 / 1 / 1. */
const FIXTURE: Workflow[] = [
  makeWorkflow({
    workflow_id: "WF-1",
    name: "Customer Triage",
    status: "active",
    owner: "@alice",
    description: "Routes inbound tickets",
  }),
  makeWorkflow({
    workflow_id: "WF-2",
    name: "Refund Pipeline",
    status: "paused",
    owner: "@bob",
    description: "   ",
  }),
  makeWorkflow({
    workflow_id: "WF-3",
    name: "Legacy Export",
    status: "archived",
    owner: "",
    description: "Old exporter",
  }),
  makeWorkflow({
    workflow_id: "WF-4",
    name: "Invoice Sync",
    status: "active",
    owner: "@carol",
    description: "Syncs invoices",
  }),
];

/**
 * Register handlers backed by a mutable in-memory list so create/delete/rename
 * are reflected on the next list refetch. Returns counters the test can assert.
 */
function useWorkflowHandlers(initial: Workflow[] = FIXTURE) {
  const state = {
    list: initial.map((w) => ({ ...w })),
  };
  const calls = {
    delete: 0,
    patch: 0,
    createWorkflow: 0,
    createVersion: 0,
    previewImport: 0,
    importWorkflow: 0,
    createdManifests: [] as WorkflowManifest[],
    importPayloads: [] as Array<{
      manifest?: WorkflowManifest;
      manifest_yaml?: string;
      deployment_bundle?: WorkflowDeploymentBundle;
      name?: string;
    }>,
    addWorkflow(workflow: Workflow) {
      state.list = [workflow, ...state.list];
    },
    renameWorkflow(id: string, name: string) {
      state.list = state.list.map((w) =>
        w.workflow_id === id ? { ...w, name } : w,
      );
    },
    removeWorkflow(id: string) {
      state.list = state.list.filter((w) => w.workflow_id !== id);
    },
    setStatus(id: string, status: Workflow["status"]) {
      state.list = state.list.map((w) =>
        w.workflow_id === id ? { ...w, status } : w,
      );
    },
  };

  server.use(
    http.get(`${API_BASE}/workflows`, () =>
      HttpResponse.json(envelope(state.list)),
    ),

    http.post(`${API_BASE}/workflows`, async ({ request }) => {
      calls.createWorkflow += 1;
      const body = (await request.json()) as { name: string; owner?: string };
      const created = makeWorkflow({
        workflow_id: "WF-NEW",
        name: body.name,
        owner: body.owner ?? "",
      });
      state.list = [...state.list, created];
      return HttpResponse.json(envelope(created));
    }),

    http.post(`${API_BASE}/workflows/:id/versions`, async ({ request }) => {
      calls.createVersion += 1;
      const body = (await request.json()) as { manifest: WorkflowManifest };
      calls.createdManifests.push(body.manifest);
      return HttpResponse.json(
        envelope({
          version_id: "V-NEW",
          workflow_id: "WF-NEW",
          version_number: 1,
        }),
      );
    }),

    http.get(`${API_BASE}/workflows/:id/versions`, ({ params }) => {
      const source = state.list.find(
        (workflow) => workflow.workflow_id === params.id,
      );
      const manifest = {
        schema_version: 1,
        workflow_id: String(params.id),
        name: source?.name ?? "Source",
        nodes: {},
        edges: [],
        tools: {
          lookup: {
            type: "registered_function",
            registry_ref: "tool.lookup.v1",
            version_constraint: "~=1.2",
          },
        },
      } as WorkflowManifest;
      return HttpResponse.json(
        envelope([
          {
            version_id: `WFV-${String(params.id)}`,
            workflow_id: String(params.id),
            version_number: 3,
            status: "published",
            manifest,
            manifest_hash: "hash",
            compiler_version: null,
            compiled_artifact_uri: null,
            compiled_bundle: null,
            validation_report: null,
            created_by: "@owner",
            created_at: "2025-01-01T00:00:00Z",
            published_by: "@owner",
            published_at: "2025-01-01T00:00:00Z",
          },
        ]),
      );
    }),

    http.post(`${API_BASE}/workflows/import/preview`, async ({ request }) => {
      calls.previewImport += 1;
      const body = (await request.json()) as {
        manifest?: WorkflowManifest;
        manifest_yaml?: string;
        deployment_bundle?: WorkflowDeploymentBundle;
        name?: string;
      };
      calls.importPayloads.push(body);
      return HttpResponse.json(
        envelope({
          source_workflow_id: body.manifest?.workflow_id ?? "uploaded-source",
          name: body.name ?? body.manifest?.name ?? "Imported",
          description: "",
          node_count: 3,
          edge_count: 2,
          validation: { valid: true, errors: [], warnings: [] },
          bundle_verification: body.deployment_bundle
            ? {
                valid: true,
                errors: [],
                digest: "bundle-digest",
                dependency_count: 1,
                ready_to_deploy: true,
              }
            : null,
          dependencies: [
            {
              kind: "tool",
              reference: "tool.lookup.v1",
              path: "tools.lookup",
              status: "resolved",
              version: "1.2.4",
              detail: "Resolved to tool.lookup.v1 1.2.4.",
            },
          ],
          ready_to_import: true,
        }),
      );
    }),

    http.post(`${API_BASE}/workflows/import`, async ({ request }) => {
      calls.importWorkflow += 1;
      const body = (await request.json()) as {
        manifest?: WorkflowManifest;
        manifest_yaml?: string;
        deployment_bundle?: WorkflowDeploymentBundle;
        name?: string;
      };
      calls.importPayloads.push(body);
      const workflow = makeWorkflow({
        workflow_id: "WF-IMPORTED",
        name: body.name ?? "Imported",
      });
      state.list = [workflow, ...state.list];
      return HttpResponse.json(
        envelope({
          workflow,
          version: {
            version_id: "WFV-IMPORTED",
            workflow_id: workflow.workflow_id,
            version_number: 1,
          },
        }),
      );
    }),

    http.patch(`${API_BASE}/workflows/:id`, async ({ params, request }) => {
      calls.patch += 1;
      const body = (await request.json()) as { name?: string };
      state.list = state.list.map((w) =>
        w.workflow_id === params.id ? { ...w, name: body.name ?? w.name } : w,
      );
      const updated = state.list.find((w) => w.workflow_id === params.id)!;
      return HttpResponse.json(envelope(updated));
    }),

    http.delete(`${API_BASE}/workflows/:id`, ({ params }) => {
      calls.delete += 1;
      state.list = state.list.filter((w) => w.workflow_id !== params.id);
      return new HttpResponse(null, { status: 204 });
    }),
  );

  return calls;
}

function DetailStub(): JSX.Element {
  const { workflowId } = useParams();
  return <div>Detail Page {workflowId}</div>;
}

function RunDetailStub(): JSX.Element {
  const { runId } = useParams();
  return <div>Run Detail {runId}</div>;
}

function renderPage() {
  const client = new QueryClient({
    defaultOptions: {
      queries: { retry: false },
      mutations: { retry: false },
    },
  });
  const renderTree = () => (
    <QueryClientProvider client={client}>
      <MemoryRouter
        future={{ v7_startTransition: true, v7_relativeSplatPath: true }}
        initialEntries={["/workflows"]}
      >
        <Routes>
          <Route path="/workflows" element={<Workflows />} />
          <Route path="/workflows/:workflowId" element={<DetailStub />} />
          <Route path="/workflow-runs/:runId" element={<RunDetailStub />} />
          <Route
            path="/workflows/:workflowId/editor/:versionId"
            element={<div>Editor Page</div>}
          />
        </Routes>
      </MemoryRouter>
    </QueryClientProvider>
  );
  const rendered = render(renderTree());
  return {
    ...rendered,
    rerenderPage: () => rendered.rerender(renderTree()),
  };
}

beforeAll(() => server.listen({ onUnhandledRequest: "error" }));
afterEach(() => {
  streamState.event = null;
  window.localStorage.clear();
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
  server.resetHandlers();
});
afterAll(() => server.close());

describe("Workflows — summary tiles & status filters", () => {
  it("renders per-status counts on the tiles", async () => {
    useWorkflowHandlers();
    renderPage();
    await screen.findByText("Customer Triage");

    expect(
      within(screen.getByTestId("workflow-tile-all")).getByText("4"),
    ).toBeInTheDocument();
    expect(
      within(screen.getByTestId("workflow-tile-active")).getByText("2"),
    ).toBeInTheDocument();
    expect(
      within(screen.getByTestId("workflow-tile-paused")).getByText("1"),
    ).toBeInTheDocument();
    expect(
      within(screen.getByTestId("workflow-tile-archived")).getByText("1"),
    ).toBeInTheDocument();
  });

  it("filters the grid when a summary tile is clicked", async () => {
    useWorkflowHandlers();
    renderPage();
    const user = userEvent.setup();
    await screen.findByText("Customer Triage");

    await user.click(screen.getByTestId("workflow-tile-active"));

    expect(screen.getByTestId("workflow-card-WF-1")).toBeInTheDocument();
    expect(screen.getByTestId("workflow-card-WF-4")).toBeInTheDocument();
    expect(screen.queryByTestId("workflow-card-WF-2")).not.toBeInTheDocument();
    expect(screen.queryByTestId("workflow-card-WF-3")).not.toBeInTheDocument();
    expect(screen.getByTestId("workflow-tile-active")).toHaveAttribute(
      "aria-pressed",
      "true",
    );
  });

  it("filters via the chip toolbar and reflects counts", async () => {
    useWorkflowHandlers();
    renderPage();
    const user = userEvent.setup();
    await screen.findByText("Customer Triage");

    expect(
      within(screen.getByTestId("workflow-filter-paused")).getByText("1"),
    ).toBeInTheDocument();
    await user.click(screen.getByTestId("workflow-filter-paused"));

    expect(screen.getByTestId("workflow-card-WF-2")).toBeInTheDocument();
    expect(screen.queryByTestId("workflow-card-WF-1")).not.toBeInTheDocument();
    expect(screen.getByTestId("workflow-filter-paused")).toHaveAttribute(
      "aria-pressed",
      "true",
    );
  });
});

describe("Workflows — search", () => {
  it("filters by name, case-insensitively", async () => {
    useWorkflowHandlers();
    renderPage();
    const user = userEvent.setup();
    await screen.findByText("Customer Triage");

    await user.type(screen.getByPlaceholderText("Search workflows…"), "REFUND");

    expect(screen.getByTestId("workflow-card-WF-2")).toBeInTheDocument();
    expect(screen.queryByTestId("workflow-card-WF-1")).not.toBeInTheDocument();
    expect(screen.queryByTestId("workflow-card-WF-4")).not.toBeInTheDocument();
  });

  it("combines a status filter with a search term", async () => {
    useWorkflowHandlers();
    renderPage();
    const user = userEvent.setup();
    await screen.findByText("Customer Triage");

    await user.click(screen.getByTestId("workflow-filter-active"));
    await user.type(
      screen.getByPlaceholderText("Search workflows…"),
      "invoice",
    );

    expect(screen.getByTestId("workflow-card-WF-4")).toBeInTheDocument();
    // WF-1 is active but its name doesn't match the search term.
    expect(screen.queryByTestId("workflow-card-WF-1")).not.toBeInTheDocument();
  });

  it("filters by owner and can clear the toolbar state", async () => {
    useWorkflowHandlers();
    renderPage();
    const user = userEvent.setup();
    await screen.findByText("Customer Triage");

    await user.selectOptions(
      screen.getByLabelText("Filter by owner"),
      "@carol",
    );
    expect(screen.getByTestId("workflow-card-WF-4")).toBeInTheDocument();
    expect(screen.queryByTestId("workflow-card-WF-1")).not.toBeInTheDocument();
    expect(screen.queryByTestId("workflow-card-WF-2")).not.toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: "Clear filters" }));
    expect(screen.getByTestId("workflow-card-WF-1")).toBeInTheDocument();
    expect(screen.getByTestId("workflow-card-WF-2")).toBeInTheDocument();
    expect(screen.getByTestId("workflow-card-WF-3")).toBeInTheDocument();
    expect(screen.getByTestId("workflow-card-WF-4")).toBeInTheDocument();
  });
});

describe("Workflows — live status refresh", () => {
  it("refreshes the list when a workflow is renamed elsewhere", async () => {
    const calls = useWorkflowHandlers();
    const view = renderPage();
    await screen.findByText("Customer Triage");

    calls.renameWorkflow("WF-1", "Priority Triage");
    streamState.event = {
      type: "workflow.updated",
      workflow_id: "WF-1",
      status: "active",
      changed_fields: ["name"],
    };
    view.rerenderPage();

    expect(await screen.findByText("Priority Triage")).toBeInTheDocument();
    expect(screen.queryByText("Customer Triage")).not.toBeInTheDocument();
  });

  it("refreshes the list when a workflow is created elsewhere", async () => {
    const calls = useWorkflowHandlers();
    const view = renderPage();
    await screen.findByText("Customer Triage");

    calls.addWorkflow(
      makeWorkflow({
        workflow_id: "WF-NEW-LIVE",
        name: "Escalation Bot",
        status: "active",
        owner: "@ops",
      }),
    );
    streamState.event = {
      type: "workflow.created",
      workflow_id: "WF-NEW-LIVE",
      status: "active",
    };
    view.rerenderPage();

    expect(
      await screen.findByTestId("workflow-card-WF-NEW-LIVE"),
    ).toBeInTheDocument();
    expect(
      within(screen.getByTestId("workflow-tile-all")).getByText("5"),
    ).toBeInTheDocument();
    expect(
      within(screen.getByTestId("workflow-tile-active")).getByText("3"),
    ).toBeInTheDocument();
  });

  it("refreshes the list when a workflow is deleted elsewhere", async () => {
    const calls = useWorkflowHandlers();
    const view = renderPage();
    await screen.findByText("Customer Triage");

    calls.removeWorkflow("WF-2");
    streamState.event = {
      type: "workflow.deleted",
      workflow_id: "WF-2",
      status: "paused",
    };
    view.rerenderPage();

    await waitFor(() =>
      expect(
        screen.queryByTestId("workflow-card-WF-2"),
      ).not.toBeInTheDocument(),
    );
    expect(
      within(screen.getByTestId("workflow-tile-all")).getByText("3"),
    ).toBeInTheDocument();
    expect(
      within(screen.getByTestId("workflow-tile-paused")).getByText("0"),
    ).toBeInTheDocument();
  });

  it("refreshes counts and filtered cards when a workflow is archived elsewhere", async () => {
    const calls = useWorkflowHandlers();
    const user = userEvent.setup();
    const view = renderPage();
    await screen.findByText("Customer Triage");

    await user.click(screen.getByTestId("workflow-filter-active"));
    expect(screen.getByTestId("workflow-card-WF-1")).toBeInTheDocument();
    expect(
      within(screen.getByTestId("workflow-tile-active")).getByText("2"),
    ).toBeInTheDocument();
    expect(
      within(screen.getByTestId("workflow-tile-archived")).getByText("1"),
    ).toBeInTheDocument();

    calls.setStatus("WF-1", "archived");
    streamState.event = {
      type: "workflow.updated",
      workflow_id: "WF-1",
      status: "archived",
    };
    view.rerenderPage();

    await waitFor(() =>
      expect(
        screen.queryByTestId("workflow-card-WF-1"),
      ).not.toBeInTheDocument(),
    );
    expect(screen.getByTestId("workflow-card-WF-4")).toBeInTheDocument();
    expect(
      within(screen.getByTestId("workflow-tile-active")).getByText("1"),
    ).toBeInTheDocument();
    expect(
      within(screen.getByTestId("workflow-tile-archived")).getByText("2"),
    ).toBeInTheDocument();
  });
});

describe("Workflows — empty & error states", () => {
  it("shows the first-run empty state when there are no workflows", async () => {
    useWorkflowHandlers([]);
    renderPage();
    expect(await screen.findByTestId("workflows-empty")).toHaveTextContent(
      "No workflows yet",
    );
  });

  it("shows the no-match empty state when filters exclude everything", async () => {
    useWorkflowHandlers();
    renderPage();
    const user = userEvent.setup();
    await screen.findByText("Customer Triage");

    await user.type(screen.getByPlaceholderText("Search workflows…"), "zzzzz");

    expect(await screen.findByTestId("workflows-empty")).toHaveTextContent(
      "No workflows match your filters",
    );
  });

  it("surfaces an error when the list request fails", async () => {
    server.use(
      http.get(`${API_BASE}/workflows`, () =>
        HttpResponse.json({ detail: "list exploded" }, { status: 500 }),
      ),
    );
    renderPage();
    expect(await screen.findByText(/list exploded/)).toBeInTheDocument();
  });
});

describe("Workflows — card content & navigation", () => {
  it("renders owner and description fallbacks", async () => {
    useWorkflowHandlers();
    renderPage();
    await screen.findByText("Customer Triage");

    // WF-2 description is whitespace-only → fallback copy.
    expect(
      within(screen.getByTestId("workflow-card-WF-2")).getByText(
        "No description provided.",
      ),
    ).toBeInTheDocument();
    // WF-3 owner is empty.
    expect(
      within(screen.getByTestId("workflow-card-WF-3")).getByText("No owner"),
    ).toBeInTheDocument();
  });

  it("opens the detail view from a card's Open button", async () => {
    useWorkflowHandlers();
    renderPage();
    const user = userEvent.setup();
    await screen.findByText("Customer Triage");

    const card = screen.getByTestId("workflow-card-WF-1");
    await user.click(
      within(card).getByRole("button", { name: "Open Customer Triage" }),
    );

    expect(await screen.findByText("Detail Page WF-1")).toBeInTheDocument();
  });
});

describe("Workflows — template create flow", () => {
  it("toggles the gallery and gates templates on a name", async () => {
    useWorkflowHandlers();
    renderPage();
    const user = userEvent.setup();
    await screen.findByText("Customer Triage");

    expect(screen.queryByTestId("template-gallery")).not.toBeInTheDocument();
    await user.click(screen.getByTestId("new-workflow"));
    expect(screen.getByTestId("template-gallery")).toBeInTheDocument();

    expect(screen.getByTestId("template-blank")).toBeDisabled();
    await user.type(screen.getByTestId("new-workflow-name"), "My Flow");
    expect(screen.getByTestId("template-blank")).toBeEnabled();
    expect(
      screen.getByTestId("template-multi_agent_handoff"),
    ).toBeInTheDocument();
    expect(screen.getByTestId("template-event_resume")).toBeInTheDocument();
    expect(screen.getByTestId("template-parallel_fanout")).toBeInTheDocument();
    expect(screen.getByTestId("template-for_each_loop")).toBeInTheDocument();
    expect(screen.getByTestId("template-graph_hybrid_rag")).toBeInTheDocument();
    expect(
      screen.getByTestId("template-knowledge_age_build"),
    ).toBeInTheDocument();
    expect(screen.getByTestId("template-refinement_loop")).toBeInTheDocument();
  });

  it("creates a workflow from a template and navigates to the editor", async () => {
    const calls = useWorkflowHandlers();
    renderPage();
    const user = userEvent.setup();
    await screen.findByText("Customer Triage");

    await user.click(screen.getByTestId("new-workflow"));
    await user.type(screen.getByTestId("new-workflow-name"), "My Flow");
    await user.click(screen.getByTestId("template-single_agent"));

    expect(await screen.findByText("Editor Page")).toBeInTheDocument();
    expect(calls.createWorkflow).toBe(1);
    expect(calls.createVersion).toBe(1);
  });

  it("creates an AGE-native knowledge workflow starter", async () => {
    const calls = useWorkflowHandlers();
    renderPage();
    const user = userEvent.setup();
    await screen.findByText("Customer Triage");

    await user.click(screen.getByTestId("new-workflow"));
    await user.type(screen.getByTestId("new-workflow-name"), "AGE Flow");
    await user.click(screen.getByTestId("template-knowledge_age"));

    expect(await screen.findByText("Editor Page")).toBeInTheDocument();
    expect(calls.createdManifests).toHaveLength(1);
    expect(calls.createdManifests[0]).toMatchObject({
      workflow_id: "WF-NEW",
      name: "AGE Flow",
      nodes: {
        knowledge: {
          type: "knowledge_query",
          retrieval_modes: ["age_graph"],
        },
      },
    });
  });

  it("creates a graph-hybrid knowledge workflow starter", async () => {
    const calls = useWorkflowHandlers();
    renderPage();
    const user = userEvent.setup();
    await screen.findByText("Customer Triage");

    await user.click(screen.getByTestId("new-workflow"));
    await user.type(
      screen.getByTestId("new-workflow-name"),
      "Graph Hybrid Flow",
    );
    await user.click(screen.getByTestId("template-graph_hybrid_rag"));

    expect(await screen.findByText("Editor Page")).toBeInTheDocument();
    expect(calls.createdManifests).toHaveLength(1);
    expect(calls.createdManifests[0]).toMatchObject({
      workflow_id: "WF-NEW",
      name: "Graph Hybrid Flow",
      nodes: {
        knowledge: {
          type: "knowledge_query",
          retrieval_modes: ["graph_hybrid"],
        },
      },
    });
  });

  it("creates an event-resume workflow starter", async () => {
    const calls = useWorkflowHandlers();
    renderPage();
    const user = userEvent.setup();
    await screen.findByText("Customer Triage");

    await user.click(screen.getByTestId("new-workflow"));
    await user.type(
      screen.getByTestId("new-workflow-name"),
      "Event Resume Flow",
    );
    await user.click(screen.getByTestId("template-event_resume"));

    expect(await screen.findByText("Editor Page")).toBeInTheDocument();
    expect(calls.createdManifests).toHaveLength(1);
    expect(calls.createdManifests[0]).toMatchObject({
      workflow_id: "WF-NEW",
      name: "Event Resume Flow",
      nodes: {
        wait_gate: {
          type: "wait_for_event",
          event_name: "documents.ready",
          correlation_key: "document_id",
          timeout_seconds: 3600,
        },
        agent: {
          type: "agent",
          name: "release-agent",
        },
      },
    });
  });

  it("creates an AGE knowledge-build workflow starter", async () => {
    const calls = useWorkflowHandlers();
    renderPage();
    const user = userEvent.setup();
    await screen.findByText("Customer Triage");

    await user.click(screen.getByTestId("new-workflow"));
    await user.type(screen.getByTestId("new-workflow-name"), "AGE Build Flow");
    await user.click(screen.getByTestId("template-knowledge_age_build"));

    expect(await screen.findByText("Editor Page")).toBeInTheDocument();
    expect(calls.createdManifests).toHaveLength(1);
    expect(calls.createdManifests[0]).toMatchObject({
      workflow_id: "WF-NEW",
      name: "AGE Build Flow",
      nodes: {
        build_graph: {
          type: "knowledge_build",
          chunking_strategy: "recursive",
          embedding_model: "sentence-transformers/all-MiniLM-L6-v2",
          graph_config: {
            output_target: "object_store_and_age",
            default_retrieval_mode: "age_graph",
          },
          activate_when_complete: true,
          wait_for_completion: true,
        },
      },
    });
  });

  it("creates a multi-agent handoff workflow starter", async () => {
    const calls = useWorkflowHandlers();
    renderPage();
    const user = userEvent.setup();
    await screen.findByText("Customer Triage");

    await user.click(screen.getByTestId("new-workflow"));
    await user.type(screen.getByTestId("new-workflow-name"), "Delegation Flow");
    await user.click(screen.getByTestId("template-multi_agent_handoff"));

    expect(await screen.findByText("Editor Page")).toBeInTheDocument();
    expect(calls.createdManifests).toHaveLength(1);
    expect(calls.createdManifests[0]).toMatchObject({
      workflow_id: "WF-NEW",
      name: "Delegation Flow",
      nodes: {
        agent: {
          type: "agent",
          handoffs: [
            {
              target: "billing",
              description: "Handle billing, invoices, and refunds.",
            },
          ],
        },
        billing: {
          type: "agent",
          name: "billing-agent",
        },
      },
    });
  });

  it("creates a bounded refinement loop workflow starter", async () => {
    const calls = useWorkflowHandlers();
    renderPage();
    const user = userEvent.setup();
    await screen.findByText("Customer Triage");

    await user.click(screen.getByTestId("new-workflow"));
    await user.type(screen.getByTestId("new-workflow-name"), "Refinement Flow");
    await user.click(screen.getByTestId("template-refinement_loop"));

    expect(await screen.findByText("Editor Page")).toBeInTheDocument();
    expect(calls.createdManifests).toHaveLength(1);
    expect(calls.createdManifests[0]).toMatchObject({
      workflow_id: "WF-NEW",
      name: "Refinement Flow",
      nodes: {
        loop: {
          type: "loop",
          target_node_id: "editor",
          max_iterations: 3,
          stop_condition: "iteration >= 2",
        },
        editor: {
          type: "agent",
          name: "editor-agent",
        },
      },
    });
  });

  it("prefers backend workflow templates when creating the first version", async () => {
    const calls = useWorkflowHandlers();
    server.use(
      http.get(`${API_BASE}/workflow-templates`, () =>
        HttpResponse.json(
          envelope({
            schema_version: 1,
            templates: [
              {
                kind: "single_agent",
                label: "Single Agent",
                description: "Server-defined starter.",
                icon: "🤖",
                gradient: "from-violet-500/10 to-caliber-500/10",
                manifest_template: {
                  schema_version: 1,
                  workflow_id: "__CALIBER_WORKFLOW_ID__",
                  name: "__CALIBER_WORKFLOW_NAME__",
                  description: "Provisioned by the workflow template API",
                  runtime: {
                    sdk: "openai-agents-python",
                    sdk_version_policy: "runtime-pinned",
                    compiler_version: "caliber-workflow-compiler-v1",
                    default_model_ref: "CALIBER_WORKFLOW_DEFAULT_MODEL",
                    session: { type: "none" },
                  },
                  nodes: {
                    start: {
                      id: "start",
                      type: "start",
                      outputs: { user_message: { type: "string" } },
                    },
                    agent: {
                      id: "agent",
                      type: "agent",
                      name: "server-agent",
                      model: "inherit",
                      instructions: {
                        type: "inline",
                        text: "Use the server template.",
                      },
                      tools: [],
                      inputs: {
                        input: { type: "string" },
                      },
                      outputs: {
                        final_output: { type: "string" },
                      },
                    },
                    final: {
                      id: "final",
                      type: "output",
                      inputs: { response: { type: "string" } },
                    },
                  },
                  edges: [
                    {
                      id: "e_start_agent",
                      from: "start",
                      to: "agent",
                      map: { user_message: "input" },
                    },
                    {
                      id: "e_agent_final",
                      from: "agent",
                      to: "final",
                      map: { final_output: "response" },
                    },
                  ],
                  tools: {},
                },
              },
            ] satisfies WorkflowTemplate[],
          }),
        ),
      ),
    );
    renderPage();
    const user = userEvent.setup();
    await screen.findByText("Customer Triage");

    await user.click(screen.getByTestId("new-workflow"));
    await user.type(screen.getByTestId("new-workflow-name"), "Server Flow");
    await user.click(screen.getByTestId("template-single_agent"));

    expect(await screen.findByText("Editor Page")).toBeInTheDocument();
    expect(calls.createdManifests).toHaveLength(1);
    expect(calls.createdManifests[0]).toMatchObject({
      workflow_id: "WF-NEW",
      name: "Server Flow",
      description: "Provisioned by the workflow template API",
      nodes: {
        agent: {
          name: "server-agent",
          instructions: {
            type: "inline",
            text: "Use the server template.",
          },
        },
      },
    });
  });
});

describe("Workflows — clone and manifest import", () => {
  it("preflights an uploaded manifest before importing it as a new workflow", async () => {
    const calls = useWorkflowHandlers();
    renderPage();
    const user = userEvent.setup();
    await screen.findByText("Customer Triage");

    await user.click(screen.getByTestId("import-workflow"));
    await user.type(
      screen.getByTestId("workflow-import-manifest"),
      "schema_version: 1\nworkflow_id: uploaded\nname: Uploaded Flow",
    );
    await user.type(
      screen.getByTestId("workflow-import-name"),
      "Imported Copy",
    );
    await user.click(screen.getByTestId("workflow-import-validate"));

    expect(await screen.findByText("Preflight passed")).toBeInTheDocument();
    expect(screen.getByText("tool.lookup.v1 @ 1.2.4")).toBeInTheDocument();
    await user.click(screen.getByTestId("workflow-import-submit"));

    expect(await screen.findByText("Editor Page")).toBeInTheDocument();
    expect(calls.previewImport).toBe(1);
    expect(calls.importWorkflow).toBe(1);
    expect(calls.importPayloads.at(-1)).toMatchObject({
      name: "Imported Copy",
    });
  });

  it("clones a selected version through preflight and preserves its dependency constraints", async () => {
    const calls = useWorkflowHandlers();
    renderPage();
    const user = userEvent.setup();
    await screen.findByText("Customer Triage");

    await user.click(screen.getByTestId("clone-workflow-WF-1"));
    expect(
      await screen.findByDisplayValue("Customer Triage Copy"),
    ).toBeInTheDocument();
    await user.click(screen.getByTestId("workflow-import-validate"));
    expect(await screen.findByText("Preflight passed")).toBeInTheDocument();
    await user.click(screen.getByTestId("workflow-import-submit"));

    expect(await screen.findByText("Editor Page")).toBeInTheDocument();
    const importPayload = calls.importPayloads.at(-1);
    expect(importPayload?.manifest).toMatchObject({
      workflow_id: "WF-1",
      tools: {
        lookup: {
          registry_ref: "tool.lookup.v1",
          version_constraint: "~=1.2",
        },
      },
    });
    expect(importPayload?.name).toBe("Customer Triage Copy");
  });

  it("detects and verifies a deployment bundle before import", async () => {
    const calls = useWorkflowHandlers();
    renderPage();
    const user = userEvent.setup();
    await screen.findByText("Customer Triage");
    const bundle = {
      kind: "caliber.workflow_deployment_bundle",
      schema_version: 1,
    };

    await user.click(screen.getByTestId("import-workflow"));
    fireEvent.change(screen.getByTestId("workflow-import-manifest"), {
      target: { value: JSON.stringify(bundle) },
    });
    await user.type(screen.getByTestId("workflow-import-name"), "Bundle Copy");
    await user.click(screen.getByTestId("workflow-import-validate"));

    expect(await screen.findByText("Bundle integrity")).toBeInTheDocument();
    expect(screen.getByText("verified")).toBeInTheDocument();
    expect(calls.importPayloads.at(-1)).toMatchObject({
      deployment_bundle: bundle,
      name: "Bundle Copy",
    });
  });
});

describe("Workflows — delete confirmation (hardened)", () => {
  it("confirms a delete and removes the card", async () => {
    const calls = useWorkflowHandlers();
    renderPage();
    const user = userEvent.setup();
    await screen.findByText("Customer Triage");

    await user.click(screen.getByTestId("delete-workflow-WF-1"));
    expect(screen.getByRole("dialog")).toHaveTextContent("Customer Triage");

    await user.click(screen.getByTestId("confirm-delete"));

    await waitFor(() =>
      expect(
        screen.queryByTestId("workflow-card-WF-1"),
      ).not.toBeInTheDocument(),
    );
    expect(calls.delete).toBe(1);
  });

  it("cancels without issuing a request", async () => {
    const calls = useWorkflowHandlers();
    renderPage();
    const user = userEvent.setup();
    await screen.findByText("Customer Triage");

    await user.click(screen.getByTestId("delete-workflow-WF-1"));
    await user.click(screen.getByRole("button", { name: "Cancel" }));

    await waitFor(() =>
      expect(screen.queryByRole("dialog")).not.toBeInTheDocument(),
    );
    expect(screen.getByTestId("workflow-card-WF-1")).toBeInTheDocument();
    expect(calls.delete).toBe(0);
  });

  it("dismisses on Escape", async () => {
    const calls = useWorkflowHandlers();
    renderPage();
    const user = userEvent.setup();
    await screen.findByText("Customer Triage");

    await user.click(screen.getByTestId("delete-workflow-WF-1"));
    expect(screen.getByRole("dialog")).toBeInTheDocument();
    await user.keyboard("{Escape}");

    await waitFor(() =>
      expect(screen.queryByRole("dialog")).not.toBeInTheDocument(),
    );
    expect(calls.delete).toBe(0);
  });

  it("dismisses on backdrop click but not on dialog click", async () => {
    const calls = useWorkflowHandlers();
    renderPage();
    const user = userEvent.setup();
    await screen.findByText("Customer Triage");

    await user.click(screen.getByTestId("delete-workflow-WF-1"));
    const dialog = screen.getByRole("dialog");

    // Clicking inside the dialog must NOT close it.
    await user.click(dialog);
    expect(screen.getByRole("dialog")).toBeInTheDocument();

    // Clicking the backdrop (the dialog's parent) closes it.
    await user.click(dialog.parentElement as HTMLElement);
    await waitFor(() =>
      expect(screen.queryByRole("dialog")).not.toBeInTheDocument(),
    );
    expect(calls.delete).toBe(0);
  });
});

describe("Workflows — inline rename commit guard", () => {
  it("commits a single rename on blur and no-ops an unchanged blur", async () => {
    const calls = useWorkflowHandlers();
    renderPage();
    const user = userEvent.setup();
    await screen.findByText("Customer Triage");

    // Changed name on blur → exactly one PATCH.
    await user.click(screen.getByTestId("edit-workflow-WF-1"));
    const input = screen.getByPlaceholderText("Workflow name");
    await user.clear(input);
    await user.type(input, "Customer Triage v2");
    await user.tab(); // blur

    await waitFor(() => expect(calls.patch).toBe(1));
    expect(await screen.findByText("Customer Triage v2")).toBeInTheDocument();

    // Re-open and blur without changing → no additional PATCH.
    await user.click(screen.getByTestId("edit-workflow-WF-1"));
    await user.tab();
    await waitFor(() =>
      expect(
        screen.queryByPlaceholderText("Workflow name"),
      ).not.toBeInTheDocument(),
    );
    expect(calls.patch).toBe(1);
  });
});
