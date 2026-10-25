import { http, HttpResponse } from "msw";
import { afterAll, afterEach, beforeAll, describe, expect, it, vi } from "vitest";

import { Inspector } from "@/components/workflows/Inspector";
import { render, screen, userEvent, waitFor } from "@/test/utils";
import { server } from "@/test/server";

const API_BASE = "/ajax-api/2.0/mlflow/caliber";

function envelope<T>(data: T): { data: T } {
  return { data };
}

function manifest(node: Record<string, unknown>) {
  return {
    schema_version: 1,
    workflow_id: "parent-workflow",
    name: "Parent",
    nodes: { [String(node.id)]: node },
    edges: [],
  } as never;
}

function renderSubworkflow(node: Record<string, unknown>) {
  const onChangeNode = vi.fn();
  const view = render(
    <Inspector
      manifest={manifest(node)}
      selectedNodeId={String(node.id)}
      tools={[]}
      onChangeNode={onChangeNode}
      onChangeWorkflow={vi.fn()}
    />,
  );
  return { ...view, onChangeNode };
}

function childWorkflow(workflowId = "child-workflow") {
  return {
    workflow_id: workflowId,
    name: "Child workflow",
    description: "A child",
    owner: "@test",
    status: "active",
    default_experiment_id: null,
    created_at: "2026-01-01T00:00:00Z",
    updated_at: "2026-01-01T00:00:00Z",
  };
}

function childVersion(overrides: Record<string, unknown> = {}) {
  return {
    version_id: "child-v1",
    workflow_id: "child-workflow",
    version_number: 1,
    status: "draft",
    manifest: {
      schema_version: 1,
      workflow_id: "child-workflow",
      name: "Child workflow",
      nodes: {
        start: { id: "start", type: "start" },
        output: { id: "output", type: "output" },
      },
      edges: [],
      tools: {},
    },
    validation_report: { valid: false, errors: [{ message: "needs work" }], warnings: [] },
    ...overrides,
  };
}

beforeAll(() => server.listen({ onUnhandledRequest: "error" }));
afterEach(() => {
  server.resetHandlers();
  vi.restoreAllMocks();
});
afterAll(() => server.close());

describe("Inspector subworkflow states", () => {
  it("explains empty, self-referential, and manually typed workflow ids", async () => {
    server.use(
      http.get(`${API_BASE}/workflows`, () => HttpResponse.json(envelope([childWorkflow()]))),
      http.get(`${API_BASE}/workflows/:workflowId/deployments`, () => HttpResponse.json(envelope([]))),
      http.get(`${API_BASE}/workflows/:workflowId/versions`, () => HttpResponse.json(envelope([]))),
    );

    const empty = renderSubworkflow({ id: "sub", type: "subworkflow", workflow_id: "", alias: "" });
    expect(await screen.findByText("Pick a child workflow from the library or type one manually below.")).toBeInTheDocument();
    expect(screen.getByText("Pick or type a child workflow before checking deployment aliases.")).toBeInTheDocument();
    empty.unmount();

    const self = renderSubworkflow({ id: "sub-self", type: "subworkflow", workflow_id: "parent-workflow", alias: "prod" });
    expect(await screen.findByText(/points back to the current workflow/)).toBeInTheDocument();
    expect(screen.getByText("Choose a different child workflow before checking deployment aliases.")).toBeInTheDocument();
    self.unmount();

    const manual = renderSubworkflow({ id: "sub-manual", type: "subworkflow", workflow_id: "typed-child", alias: "prod" });
    expect(await screen.findByText(/Using manual workflow ID typed-child/)).toBeInTheDocument();
    expect(screen.getAllByText(/not in the current workflow library response/).length).toBeGreaterThan(0);
    expect(screen.getByText("Pick a child workflow from the library to inspect its current runtime contract.")).toBeInTheDocument();
    manual.unmount();
  });

  it("covers library, deployment, and version failures without hiding editable fields", async () => {
    server.use(
      http.get(`${API_BASE}/workflows`, () => HttpResponse.error()),
    );
    const libraryError = renderSubworkflow({ id: "sub-error", type: "subworkflow", workflow_id: "typed-child", alias: "prod" });
    expect(await screen.findByText(/Workflow library is unavailable right now/)).toBeInTheDocument();
    expect(screen.getByText("Alias checks are unavailable until the workflow library loads again.")).toBeInTheDocument();
    expect(screen.getByTestId("inspector-subworkflow-contract")).toHaveTextContent("Pick a child workflow from the library");
    libraryError.unmount();

    server.use(
      http.get(`${API_BASE}/workflows`, () => HttpResponse.json(envelope([childWorkflow()]))),
      http.get(`${API_BASE}/workflows/:workflowId/deployments`, () => HttpResponse.error()),
      http.get(`${API_BASE}/workflows/:workflowId/versions`, () => HttpResponse.error()),
    );
    const loadError = renderSubworkflow({ id: "sub-load-error", type: "subworkflow", workflow_id: "child-workflow", alias: "prod" });
    expect(await screen.findByText(/Deployment aliases could not be loaded right now/)).toBeInTheDocument();
    expect(await screen.findByText(/Child workflow versions could not be loaded right now/)).toBeInTheDocument();
    expect(screen.getByTestId("inspector-subworkflow-open")).toHaveAttribute("href", "/caliber/workflows/child-workflow");
    loadError.unmount();
  });

  it("covers manual and active-alias resolution, missing aliases, and unpublished warnings", async () => {
    server.use(
      http.get(`${API_BASE}/workflows`, () => HttpResponse.json(envelope([childWorkflow()]))),
      http.get(`${API_BASE}/workflows/:workflowId/deployments`, () => HttpResponse.json(envelope([
        { deployment_id: "dep-prod", workflow_id: "child-workflow", alias: "prod", version_id: "child-v1", status: "active" },
        { deployment_id: "dep-staging", workflow_id: "child-workflow", alias: "staging", version_id: "child-v0", status: "active" },
      ]))),
      http.get(`${API_BASE}/workflows/:workflowId/versions`, () => HttpResponse.json(envelope([
        childVersion(),
        childVersion({ version_id: "child-v2", version_number: 2, status: "published", validation_report: { valid: true, errors: [], warnings: [{ message: "one" }] } }),
      ]))),
    );
    const view = renderSubworkflow({ id: "sub-resolve", type: "subworkflow", workflow_id: "child-workflow", alias: "prod" });
    expect(await screen.findByText("Alias prod resolves to active deployment dep-prod.")).toBeInTheDocument();
    expect(screen.getByTestId("inspector-subworkflow-contract")).toHaveTextContent("v1 · draft");
    expect(screen.getByTestId("inspector-subworkflow-contract")).toHaveTextContent("1 error");

    await userEvent.click(screen.getByTestId("inspector-subworkflow-alias-manual"));
    expect(view.onChangeNode).toHaveBeenCalledWith("sub-resolve", { alias: "manual" });
    expect(screen.getByTestId("inspector-subworkflow-alias-staging")).toBeInTheDocument();
    await userEvent.click(screen.getByTestId("inspector-subworkflow-alias-staging"));
    expect(view.onChangeNode).toHaveBeenCalledWith("sub-resolve", { alias: "staging" });
    view.unmount();

    server.use(
      http.get(`${API_BASE}/workflows/:workflowId/versions`, () => HttpResponse.json(envelope([childVersion()]))),
    );
    const manual = renderSubworkflow({ id: "sub-manual-draft", type: "subworkflow", workflow_id: "child-workflow", alias: "manual" });
    expect(await screen.findByText("Manual runs resolve the latest saved child version directly.")).toBeInTheDocument();
    expect(await screen.findByText("v1 · draft")).toBeInTheDocument();
    expect(screen.getByTestId("inspector-subworkflow-contract")).toHaveTextContent("Manual resolution currently targets an unpublished child version");
    manual.unmount();

    const noActive = renderSubworkflow({ id: "sub-no-active", type: "subworkflow", workflow_id: "child-workflow", alias: "missing" });
    expect(await screen.findByText(/No active deployment currently serves alias missing/)).toBeInTheDocument();
    expect(screen.getByTestId("inspector-subworkflow-contract")).toHaveTextContent("does not currently resolve to an active deployed child version");
    noActive.unmount();
  });

  it("covers a known child with no versions and a child with no active deployments", async () => {
    server.use(
      http.get(`${API_BASE}/workflows`, () => HttpResponse.json(envelope([childWorkflow()]))),
      http.get(`${API_BASE}/workflows/:workflowId/deployments`, () => HttpResponse.json(envelope([]))),
      http.get(`${API_BASE}/workflows/:workflowId/versions`, () => HttpResponse.json(envelope([]))),
    );
    const view = renderSubworkflow({ id: "sub-empty-child", type: "subworkflow", workflow_id: "child-workflow", alias: "missing" });
    expect(await screen.findByText(/No active deployments exist for child-workflow/)).toBeInTheDocument();
    expect(screen.getByTestId("inspector-subworkflow-contract")).toHaveTextContent("does not currently resolve to an active deployed child version");
    await waitFor(() => expect(screen.getByTestId("inspector-subworkflow-workflow-shortcut")).toHaveValue("child-workflow"));
    view.unmount();

    const manual = renderSubworkflow({ id: "sub-empty-manual", type: "subworkflow", workflow_id: "child-workflow", alias: "manual" });
    expect(await screen.findByText("Manual runs resolve the latest saved child version directly.")).toBeInTheDocument();
    await waitFor(() => expect(screen.getByTestId("inspector-subworkflow-workflow-shortcut")).toHaveValue("child-workflow"));
    expect(await screen.findByText(/has no saved versions yet/)).toBeInTheDocument();
    manual.unmount();
  });
});
