import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { http, HttpResponse } from "msw";
import { afterAll, afterEach, beforeAll, describe, expect, it, vi } from "vitest";
import { MemoryRouter, Route, Routes } from "react-router-dom";

import { WorkflowDetail } from "@/pages/WorkflowDetail";
import { server } from "@/test/server";

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
    manifest: { schema_version: 1, workflow_id: "WF-1", name: "Support", nodes: {}, edges: [] },
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

const DIFF_WITH_GATE = {
  added_nodes: [{ id: "reviewer", type: "agent" }],
  removed_nodes: [],
  modified_nodes: [],
  added_edges: [],
  removed_edges: [],
  modified_edges: [],
  artifact_changes: [],
  // Gate-only change must render (the bug this fixes): empty=false but the
  // old renderer skipped deploy_gate_changes.
  deploy_gate_changes: [{ name: "quality", from: { min: 0.8 }, to: { min: 0.9 } }],
  empty: false,
};

function versionsHandlers(
  overrides: { onRestore?: (id: string) => void } = {},
) {
  return [
    http.post(`${API_BASE}/workflow-versions/:id/restore`, ({ params }) => {
      overrides.onRestore?.(String(params.id));
      return HttpResponse.json(
        envelope(makeVersion({ version_id: "WFV-3", version_number: 3, status: "draft" })),
        { status: 201 },
      );
    }),
    http.get(`${API_BASE}/workflow-versions/:base/diff/:other`, () =>
      HttpResponse.json(envelope(DIFF_WITH_GATE)),
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
    http.get(`${API_BASE}/workflows/WF-1/versions`, () =>
      HttpResponse.json(
        envelope([
          makeVersion({ version_id: "WFV-2", version_number: 2, status: "draft" }),
          makeVersion({ version_id: "WFV-1", version_number: 1, status: "published" }),
        ]),
      ),
    ),
    http.get(`${API_BASE}/workflows/WF-1/deployments`, () => HttpResponse.json(envelope([]))),
    http.get(`${API_BASE}/workflows/WF-1/promotions`, () => HttpResponse.json(envelope([]))),
    http.get(`${API_BASE}/workflows/WF-1/patches`, () => HttpResponse.json(envelope([]))),
    http.get(`${API_BASE}/workflows/WF-1/runs`, () => HttpResponse.json(envelope([]))),
    http.get(`${API_BASE}/workflows/WF-1/service`, () =>
      HttpResponse.json({ detail: "not published" }, { status: 404 }),
    ),
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

function renderDetail(initialPath = "/workflows/WF-1?tab=versions") {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false, gcTime: 0 } } });
  return render(
    <QueryClientProvider client={qc}>
      <MemoryRouter
        future={{ v7_startTransition: true, v7_relativeSplatPath: true }}
        initialEntries={[initialPath]}
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

describe("WorkflowDetail Versions tab", () => {
  it("diffs two versions and renders node and deploy-gate changes", async () => {
    server.use(...versionsHandlers());
    renderDetail();

    await screen.findByTestId("versions-table");
    const panel = await screen.findByTestId("version-diff-panel");
    expect(panel).toBeInTheDocument();

    await userEvent.selectOptions(screen.getByTestId("version-diff-base"), "WFV-1");
    await userEvent.selectOptions(screen.getByTestId("version-diff-other"), "WFV-2");

    // Added-node row.
    expect(await screen.findByTestId("diff-added-node")).toHaveTextContent("reviewer");
    // Deploy-gate change row — the regression this fix addresses.
    expect(screen.getByTestId("diff-gate-change")).toHaveTextContent("quality");
  });

  it("mounts the shared VersionPanel with Promote/Roll back controls", async () => {
    server.use(...versionsHandlers());
    renderDetail();

    await screen.findByTestId("versions-table");
    const panel = await screen.findByTestId("version-panel");
    expect(panel).toBeInTheDocument();
  });

  it("restores a prior version as a new draft", async () => {
    const onRestore = vi.fn();
    server.use(...versionsHandlers({ onRestore }));
    renderDetail();

    await screen.findByTestId("versions-table");
    const restoreBtn = await screen.findByTestId("restore-version-btn-WFV-1");

    await userEvent.click(restoreBtn);

    await waitFor(() => expect(onRestore).toHaveBeenCalledWith("WFV-1"));
  });

  it("manages a published service, token, snippets, and unpublish flow", async () => {
    let serviceTokens = [
      {
        token_id: "tok-1",
        name: "existing",
        prefix: "cal_old",
        scopes: ["invoke"],
        created_by: "@test",
        created_at: NOW,
        expires_at: null,
        revoked_at: null,
      },
      {
        token_id: "tok-revoked",
        name: "revoked",
        prefix: "cal_dead",
        scopes: ["invoke"],
        created_by: "@test",
        created_at: NOW,
        expires_at: null,
        revoked_at: NOW,
      },
    ];
    let service: Record<string, unknown> = {
      service_id: "svc-1",
      workflow_id: "WF-1",
      alias: "prod",
      input_schema: {
        type: "object",
        properties: { question: { type: "string" } },
      },
      output_schema: { type: "object", properties: { answer: { type: "string" } } },
      enabled: true,
      auth_required: true,
      endpoint: "/ajax-api/2.0/mlflow/caliber/workflows/WF-1/invoke",
      created_by: "@test",
      created_at: NOW,
      updated_at: NOW,
      token_count: 2,
    };

    server.use(
      http.get(`${API_BASE}/workflows/WF-1/service`, () =>
        HttpResponse.json(envelope(service)),
      ),
      ...versionsHandlers(),
      http.get(`${API_BASE}/workflows/WF-1/service/tokens`, () =>
        HttpResponse.json(envelope(serviceTokens)),
      ),
      http.post(`${API_BASE}/workflows/WF-1/service/tokens`, async ({ request }) => {
        const body = (await request.json()) as { name?: string };
        const created = {
          token_id: "tok-new",
          name: body.name ?? "default",
          prefix: "cal_new",
          token: "cal_secret_once",
          scopes: ["invoke"],
          created_by: "@test",
          created_at: NOW,
          expires_at: null,
          revoked_at: null,
        };
        serviceTokens = [...serviceTokens, created];
        return HttpResponse.json(envelope(created), { status: 201 });
      }),
      http.delete(`${API_BASE}/workflows/WF-1/service/tokens/:tokenId`, () => {
        serviceTokens = serviceTokens.map((token) =>
          token.token_id === "tok-1" ? { ...token, revoked_at: NOW } : token,
        );
        return HttpResponse.json(envelope({ status: "revoked" }));
      }),
      http.get(`${API_BASE}/workflows/WF-1/service/openapi.json`, () =>
        HttpResponse.json({ openapi: "3.0.0" }),
      ),
      http.delete(`${API_BASE}/workflows/WF-1/service`, () => {
        service = { ...service, enabled: false };
        return HttpResponse.json(envelope({ status: "unpublished" }));
      }),
    );

    const user = userEvent.setup();
    renderDetail("/workflows/WF-1?tab=service");

    expect(await screen.findByTestId("service-token-panel")).toBeInTheDocument();
    expect(screen.getByTestId("service-auth-badge")).toHaveTextContent("Token required");
    expect(screen.getByTestId("service-token-list")).toHaveTextContent("existing");
    expect(screen.getByTestId("service-token-list")).toHaveTextContent("revoked");

    await user.click(screen.getByTestId("service-token-revoke-tok-1"));
    await screen.findByText("Access token revoked.");

    await user.clear(screen.getByTestId("service-token-name"));
    await user.type(screen.getByTestId("service-token-name"), "ci-client");
    await user.click(screen.getByTestId("service-token-create-btn"));
    expect(await screen.findByTestId("service-new-token")).toHaveTextContent(
      "cal_secret_once",
    );
    await user.click(screen.getByTestId("service-token-copy-btn"));
    await user.click(screen.getByText("I saved it"));

    await user.click(screen.getByTestId("service-snippet-lang-python"));
    expect(screen.getByTestId("service-snippet")).toHaveTextContent("requests.post");
    await user.click(screen.getByTestId("service-snippet-copy-btn"));
    await user.click(screen.getByTestId("service-openapi-download-btn"));
    await screen.findByText("OpenAPI specification downloaded.");

    await user.click(screen.getByTestId("service-unpublish-btn"));
    expect(await screen.findByText("Service unpublished.")).toBeInTheDocument();
  });

  it("publishes a deployed workflow and renders the explicit public-service warning", async () => {
    let published = false;
    const publicService = {
      service_id: "svc-public",
      workflow_id: "WF-1",
      alias: "prod",
      input_schema: { type: "object" },
      output_schema: { type: "object" },
      enabled: true,
      auth_required: false,
      endpoint: "/ajax-api/2.0/mlflow/caliber/workflows/WF-1/invoke",
      created_by: "@test",
      created_at: NOW,
      updated_at: NOW,
      token_count: 0,
    };
    server.use(
      http.get(`${API_BASE}/workflows/WF-1/deployments`, () =>
        HttpResponse.json(
          envelope([
            {
              deployment_id: "dep-1",
              workflow_id: "WF-1",
              alias: "prod",
              version_id: "WFV-1",
              environment: "prod",
              status: "active",
              deployed_by: "@test",
              deployed_at: NOW,
            },
          ]),
        ),
      ),
      http.get(`${API_BASE}/workflows/WF-1/service`, () =>
        published
          ? HttpResponse.json(envelope(publicService))
          : HttpResponse.json({ detail: "not published" }, { status: 404 }),
      ),
      ...versionsHandlers(),
      http.post(`${API_BASE}/workflows/WF-1/service`, async ({ request }) => {
        published = true;
        expect(await request.json()).toEqual({ auth_required: true });
        return HttpResponse.json(envelope(publicService), { status: 201 });
      }),
    );

    const user = userEvent.setup();
    renderDetail("/workflows/WF-1?tab=service");

    const publishButton = await screen.findByTestId("service-publish-btn");
    expect(publishButton).not.toBeDisabled();
    await user.click(publishButton);

    expect(await screen.findByText("Published as a token-protected callable service.")).toBeInTheDocument();
    expect(await screen.findByTestId("service-auth-badge")).toHaveTextContent("Open · no auth");
    expect(screen.getByText(/Explicitly public endpoint/)).toBeInTheDocument();
    expect(screen.queryByTestId("service-token-panel")).not.toBeInTheDocument();
  });

  it("renders empty run-history triage and the filtered-empty state", async () => {
    server.use(
      ...versionsHandlers(),
      http.get(`${API_BASE}/workflows/WF-1/runs/stats`, () =>
        HttpResponse.json(
          envelope({
            workflow_id: "WF-1",
            total_runs: 0,
            matching_runs: 0,
            waiting_event_runs: 0,
            artifact_persistence: { failed: 0, persisted: 0 },
          }),
        ),
      ),
    );

    const user = userEvent.setup();
    renderDetail("/workflows/WF-1?tab=runs");

    expect(await screen.findByTestId("runs-table")).toHaveTextContent(
      "No runs recorded",
    );
    expect(screen.getByTestId("workflow-run-target")).toHaveTextContent(
      "alias manual",
    );
    await user.type(screen.getByRole("searchbox", { name: "Search runs" }), "missing");
    expect(await screen.findByTestId("runs-filtered-empty")).toHaveTextContent(
      "No runs match",
    );
  });
});
