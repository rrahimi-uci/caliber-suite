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
    auth_required: true,
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
    onPublish?: (body: unknown) => void;
    onUnpublish?: () => void;
    onCreateToken?: (body: unknown) => void;
    onDownloadOpenApi?: (request: Request) => void;
    onRevokeToken?: (tokenId: string) => void;
    tokens?: Array<Record<string, unknown>>;
  } = {},
) {
  const deployments = overrides.deployments ?? [];
  // Successive GET /service responses (each is consumed once, then the last
  // value sticks) — lets a test return 404 first, then a service after publish.
  const serviceResponses = overrides.serviceResponses ?? [null];
  let serviceCallIndex = 0;

  return [
    http.post(`${API_BASE}/workflows/WF-1/service`, async ({ request }) => {
      overrides.onPublish?.(await request.json());
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
    http.get(`${API_BASE}/workflows/WF-1/service/openapi.json`, ({ request }) => {
      overrides.onDownloadOpenApi?.(request);
      return HttpResponse.json({
        openapi: "3.0.3",
        info: { title: "Support Workflow — CALIBER service", version: "1.0.0" },
        paths: { [`${API_BASE}/services/WF-1/invoke`]: { post: {} } },
      });
    }),
    http.get(`${API_BASE}/workflows/WF-1/service/tokens`, () =>
      HttpResponse.json(envelope(overrides.tokens ?? [])),
    ),
    http.post(`${API_BASE}/workflows/WF-1/service/tokens`, async ({ request }) => {
      const body = await request.json();
      overrides.onCreateToken?.(body);
      return HttpResponse.json(
        envelope({
          token_id: "SVT-1",
          name: "production-client",
          prefix: "cal_svc_example",
          scopes: ["invoke"],
          created_by: "@test",
          created_at: NOW,
          expires_at: null,
          revoked_at: null,
          token: "cal_svc_example_secret",
        }),
        { status: 201 },
      );
    }),
    http.delete(`${API_BASE}/workflows/WF-1/service/tokens/:tokenId`, ({ params }) => {
      overrides.onRevokeToken?.(String(params.tokenId));
      return HttpResponse.json(envelope({ status: "revoked" }));
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
  vi.restoreAllMocks();
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
    expect(onPublish).toHaveBeenCalledWith({ auth_required: true });

    const endpoint = await screen.findByTestId("service-endpoint");
    expect(endpoint).toHaveTextContent("/ajax-api/2.0/mlflow/caliber/services/WF-1/invoke");

    // Default snippet language is curl, built from the service input schema.
    const snippet = screen.getByTestId("service-snippet");
    expect(snippet).toHaveTextContent("curl -X POST");
    expect(snippet).toHaveTextContent("/services/WF-1/runs/RUN_ID");
    expect(snippet.textContent).toContain('"query"');

    expect(screen.getByTestId("service-auth-badge")).toHaveTextContent("Token required");
    expect(screen.getByTestId("service-token-panel")).toBeInTheDocument();
    expect(screen.getByTestId("service-unpublish-btn")).toBeInTheDocument();
  });

  it("creates an access token and displays its secret exactly once", async () => {
    const onCreateToken = vi.fn();
    server.use(
      ...detailHandlers({
        deployments: [makeDeployment()],
        serviceResponses: [makeService()],
        onCreateToken,
      }),
    );
    renderDetail();

    await screen.findByTestId("service-token-panel");
    await userEvent.clear(screen.getByTestId("service-token-name"));
    await userEvent.type(screen.getByTestId("service-token-name"), "production-client");
    await userEvent.click(screen.getByTestId("service-token-create-btn"));

    await waitFor(() =>
      expect(onCreateToken).toHaveBeenCalledWith({
        name: "production-client",
        scopes: ["invoke"],
      }),
    );
    expect(await screen.findByTestId("service-new-token")).toHaveTextContent(
      "cal_svc_example_secret",
    );
    expect(screen.getByText(/will not show it again/i)).toBeInTheDocument();
  });

  it("downloads protected-service OpenAPI through the internal workflow route", async () => {
    const onDownloadOpenApi = vi.fn();
    const createObjectUrl = vi.spyOn(URL, "createObjectURL").mockReturnValue("blob:openapi");
    const revokeObjectUrl = vi.spyOn(URL, "revokeObjectURL").mockImplementation(() => undefined);
    let downloadedFilename: string | null = null;
    const anchorClick = vi
      .spyOn(HTMLAnchorElement.prototype, "click")
      .mockImplementation(function (this: HTMLAnchorElement) {
        downloadedFilename = this.download;
      });
    server.use(
      ...detailHandlers({
        deployments: [makeDeployment()],
        serviceResponses: [makeService()],
        onDownloadOpenApi,
      }),
    );
    renderDetail();

    const downloadButton = await screen.findByTestId("service-openapi-download-btn");
    expect(downloadButton.tagName).toBe("BUTTON");
    await userEvent.click(downloadButton);

    await waitFor(() => expect(onDownloadOpenApi).toHaveBeenCalledTimes(1));
    const request = onDownloadOpenApi.mock.calls[0][0] as Request;
    expect(new URL(request.url).pathname).toBe(
      `${API_BASE}/workflows/WF-1/service/openapi.json`,
    );
    expect(request.headers.get("authorization")).toBeNull();
    expect(createObjectUrl).toHaveBeenCalledTimes(1);
    const downloadedBlob = createObjectUrl.mock.calls[0][0];
    expect(downloadedBlob.type).toBe("application/json");
    await expect(downloadedBlob.text()).resolves.toContain('"openapi":"3.0.3"');
    expect(anchorClick).toHaveBeenCalledTimes(1);
    expect(revokeObjectUrl).toHaveBeenCalledWith("blob:openapi");
    expect(downloadedFilename).toBe("caliber-WF-1-openapi.json");
    expect(await screen.findByTestId("service-message")).toHaveTextContent(
      "OpenAPI specification downloaded",
    );
  });

  it("lists masked service tokens and revokes an active token", async () => {
    const onRevokeToken = vi.fn();
    server.use(
      ...detailHandlers({
        deployments: [makeDeployment()],
        serviceResponses: [makeService({ token_count: 2 })],
        tokens: [
          {
            token_id: "SVT-active",
            name: "production-client",
            prefix: "cal_svc_active",
            scopes: ["invoke"],
            created_by: "@test",
            created_at: NOW,
            expires_at: null,
            revoked_at: null,
          },
          {
            token_id: "SVT-revoked",
            name: "old-client",
            prefix: "cal_svc_old",
            scopes: ["invoke"],
            created_by: "@test",
            created_at: NOW,
            expires_at: null,
            revoked_at: NOW,
          },
        ],
        onRevokeToken,
      }),
    );
    renderDetail();

    const list = await screen.findByTestId("service-token-list");
    expect(list).toHaveTextContent("production-client");
    expect(list).toHaveTextContent("cal_svc_active…");
    expect(list).toHaveTextContent("old-client");
    expect(list).toHaveTextContent("revoked");
    expect(screen.queryByTestId("service-token-revoke-SVT-revoked")).not.toBeInTheDocument();

    await userEvent.click(screen.getByTestId("service-token-revoke-SVT-active"));
    await waitFor(() => expect(onRevokeToken).toHaveBeenCalledWith("SVT-active"));
    expect(await screen.findByTestId("service-message")).toHaveTextContent(
      "Access token revoked",
    );
  });

  it("clearly labels an explicitly public legacy service", async () => {
    server.use(
      ...detailHandlers({
        deployments: [makeDeployment()],
        serviceResponses: [makeService({ auth_required: false })],
      }),
    );
    renderDetail();

    expect(await screen.findByTestId("service-auth-badge")).toHaveTextContent("Open");
    expect(screen.getByText(/explicitly public endpoint/i)).toBeInTheDocument();
    expect(screen.queryByTestId("service-token-panel")).not.toBeInTheDocument();
  });

  it("switches the client snippet between curl, Python, and JavaScript", async () => {
    server.use(
      ...detailHandlers({
        deployments: [makeDeployment()],
        serviceResponses: [makeService()],
      }),
    );
    renderDetail();

    await screen.findByTestId("service-tab");
    const snippet = await screen.findByTestId("service-snippet");
    expect(snippet).toHaveTextContent("curl -X POST");

    await userEvent.click(screen.getByTestId("service-snippet-lang-python"));
    expect(screen.getByTestId("service-snippet").textContent).toContain("import requests");

    await userEvent.click(screen.getByTestId("service-snippet-lang-javascript"));
    expect(screen.getByTestId("service-snippet").textContent).toContain("await fetch(BASE");
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
