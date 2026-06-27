import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { http, HttpResponse } from "msw";
import { afterAll, afterEach, beforeAll, describe, expect, it, vi } from "vitest";
import { MemoryRouter, Route, Routes } from "react-router-dom";

import { WorkflowDetail } from "@/pages/WorkflowDetail";
import { server } from "@/test/server";

// The Service tab is shown in both single- and multi-environment mode. Pin the
// single-environment default so the suite mirrors what ships.
vi.mock("@/lib/environment", () => ({
  SINGLE_ENVIRONMENT: true,
  LIVE_ALIAS: "prod",
  DEPLOYMENT_ALIASES: ["prod"],
}));

vi.mock("@/hooks/useEventStream", () => ({
  useEventStream: () => null,
}));

vi.mock("@/lib/toast", () => ({
  showToast: { success: vi.fn(), error: vi.fn(), info: vi.fn(), warning: vi.fn() },
}));

const API_BASE = "/ajax-api/2.0/mlflow/caliber";
const NOW = "2026-05-30T00:00:00Z";

function envelope<T>(data: T): { data: T } {
  return { data };
}

function makeVersion(overrides: Record<string, unknown> = {}) {
  return {
    version_id: "WFV-1",
    workflow_id: "WF-1",
    version_number: 1,
    status: "published",
    manifest: {
      schema_version: 1,
      workflow_id: "WF-1",
      name: "Support",
      nodes: {
        start: { id: "start", type: "start", outputs: { user_message: { type: "string" } } },
        final: { id: "final", type: "output", inputs: { response: { type: "string" } } },
      },
      edges: [{ id: "e1", from: "start", to: "final", map: { user_message: "response" } }],
    },
    manifest_hash: "hash1",
    compiler_version: null,
    compiled_artifact_uri: null,
    compiled_bundle: null,
    validation_report: null,
    created_by: "@test",
    created_at: NOW,
    published_by: "@test",
    published_at: NOW,
    ...overrides,
  };
}

function makeDeployment(overrides: Record<string, unknown> = {}) {
  return {
    deployment_id: "DEP-1",
    workflow_id: "WF-1",
    alias: "prod",
    version_id: "WFV-1",
    environment: "prod",
    status: "active",
    deployed_by: "@test",
    deployed_at: NOW,
    ...overrides,
  };
}

function makeService(overrides: Record<string, unknown> = {}) {
  return {
    service_id: "SVC-1",
    workflow_id: "WF-1",
    alias: "prod",
    input_schema: { type: "object", properties: { query: { type: "string" } } },
    output_schema: { type: "object", properties: { answer: { type: "string" } } },
    enabled: true,
    auth_required: false,
    endpoint: "/ajax-api/2.0/mlflow/caliber/services/WF-1/invoke",
    created_by: "@test",
    created_at: NOW,
    updated_at: NOW,
    token_count: 0,
    ...overrides,
  };
}

/**
 * Register every endpoint WorkflowDetail queries on mount plus the service
 * routes. `service` controls the GET /service response (null → 404). The
 * publish/unpublish handlers are pushed first so they win over later
 * registrations within a single `server.use` call.
 */
function detailHandlers(
  overrides: {
    deployments?: Array<Record<string, unknown>>;
    serviceResponses?: Array<Record<string, unknown> | null>;
    onPublish?: () => void;
    onUnpublish?: () => void;
  } = {},
) {
  const deployments = overrides.deployments ?? [];
  // Successive GET /service responses (each is consumed once, then the last
  // value sticks) — lets a test return 404 first, then a service after publish.
  const serviceResponses = overrides.serviceResponses ?? [null];
  let serviceCallIndex = 0;

  return [
    http.post(`${API_BASE}/workflows/WF-1/service`, () => {
      overrides.onPublish?.();
      return HttpResponse.json(envelope(makeService()));
    }),
    http.delete(`${API_BASE}/workflows/WF-1/service`, () => {
      overrides.onUnpublish?.();
      return HttpResponse.json(envelope({ status: "unpublished" }));
    }),
    http.get(`${API_BASE}/workflows/WF-1/service`, () => {
      const idx = Math.min(serviceCallIndex, serviceResponses.length - 1);
      serviceCallIndex += 1;
      const body = serviceResponses[idx];
      if (body === null) {
        return HttpResponse.json({ detail: "service not published" }, { status: 404 });
      }
      return HttpResponse.json(envelope(body));
    }),
    http.get(`${API_BASE}/capabilities`, () =>
      HttpResponse.json(
        envelope({
          workflow_runs: {
            queue_enabled: false,
            supports_async_submit: false,
            supports_cancel: false,
            supports_retry: false,
            supports_resume: false,
            runtime_approvals_enabled: false,
            checkpointing_enabled: false,
            event_backend: "in_process",
          },
          sync_workflow_version_run: true,
        }),
      ),
    ),
    http.get(`${API_BASE}/workflows/WF-1`, () =>
      HttpResponse.json(
        envelope({
          workflow_id: "WF-1",
          name: "Support Workflow",
          description: "",
          owner: "@test",
          status: "active",
          default_experiment_id: null,
          created_at: NOW,
          updated_at: NOW,
        }),
      ),
    ),
    http.get(`${API_BASE}/workflows/WF-1/versions`, () =>
      HttpResponse.json(envelope([makeVersion()])),
    ),
    http.get(`${API_BASE}/workflows/WF-1/deployments`, () =>
      HttpResponse.json(envelope(deployments)),
    ),
    http.get(`${API_BASE}/workflows/WF-1/promotions`, () => HttpResponse.json(envelope([]))),
    http.get(`${API_BASE}/workflows/WF-1/patches`, () => HttpResponse.json(envelope([]))),
    http.get(`${API_BASE}/workflows/WF-1/runs`, () => HttpResponse.json(envelope([]))),
    http.get(`${API_BASE}/workflows/WF-1/calibration/options`, () =>
      HttpResponse.json(
        envelope({
          supported_objectives: ["quality"],
          supported_move_set: [],
          scorer_options: [],
          default_budget: { max_candidates: 3, max_eval_examples: 20, min_examples: 2 },
          data: { available: false, reason: "n/a" },
        }),
      ),
    ),
  ];
}

function renderDetail() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false, gcTime: 0 } } });
  return render(
    <QueryClientProvider client={qc}>
      <MemoryRouter
        future={{ v7_startTransition: true, v7_relativeSplatPath: true }}
        initialEntries={["/workflows/WF-1?tab=service"]}
      >
        <Routes>
          <Route path="/workflows/:workflowId" element={<WorkflowDetail />} />
        </Routes>
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

beforeAll(() => server.listen({ onUnhandledRequest: "bypass" }));
afterEach(() => {
  cleanup();
  vi.clearAllMocks();
  server.resetHandlers();
});
afterAll(() => server.close());

describe("WorkflowDetail Service tab", () => {
  it("shows the deploy-first notice with a disabled publish button when there is no deployment", async () => {
    server.use(...detailHandlers({ deployments: [], serviceResponses: [null] }));
    renderDetail();

    await screen.findByTestId("service-tab");

    expect(await screen.findByTestId("service-no-deployment-notice")).toHaveTextContent(
      "Deploy a published version first",
    );
    const publishBtn = screen.getByTestId("service-publish-btn");
    expect(publishBtn).toBeDisabled();
  });

  it("publishes a service and reveals the endpoint, curl snippet, and unpublish button", async () => {
    const onPublish = vi.fn();
    server.use(
      ...detailHandlers({
        deployments: [makeDeployment()],
        // 404 on first load; after publish invalidates, return the service.
        serviceResponses: [null, makeService()],
        onPublish,
      }),
    );
    renderDetail();

    await screen.findByTestId("service-tab");
    const publishBtn = await screen.findByTestId("service-publish-btn");
    expect(publishBtn).toBeEnabled();

    await userEvent.click(publishBtn);

    await waitFor(() => expect(onPublish).toHaveBeenCalledTimes(1));

    const endpoint = await screen.findByTestId("service-endpoint");
    expect(endpoint).toHaveTextContent("/ajax-api/2.0/mlflow/caliber/services/WF-1/invoke");

    const curl = screen.getByTestId("service-curl");
    expect(curl).toHaveTextContent("curl -X POST");
    expect(curl).toHaveTextContent('{"input": {}}');

    expect(screen.getByTestId("service-auth-badge")).toHaveTextContent("Open");
    expect(screen.getByTestId("service-unpublish-btn")).toBeInTheDocument();
  });

  it("unpublishes a published service and returns to the publish state", async () => {
    const onUnpublish = vi.fn();
    server.use(
      ...detailHandlers({
        deployments: [makeDeployment()],
        // Published on first load; 404 after unpublish invalidates.
        serviceResponses: [makeService(), null],
        onUnpublish,
      }),
    );
    renderDetail();

    await screen.findByTestId("service-tab");
    const unpublishBtn = await screen.findByTestId("service-unpublish-btn");

    await userEvent.click(unpublishBtn);

    await waitFor(() => expect(onUnpublish).toHaveBeenCalledTimes(1));

    expect(await screen.findByTestId("service-publish-btn")).toBeEnabled();
  });
});
