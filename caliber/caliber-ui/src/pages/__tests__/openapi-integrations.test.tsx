import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { http, HttpResponse } from "msw";
import { afterAll, afterEach, beforeAll, describe, expect, it } from "vitest";
import { MemoryRouter, Route, Routes } from "react-router-dom";

import { OpenApiIntegrations } from "@/pages/OpenApiIntegrations";
import type { OpenApiToolDraft } from "@/api/workflowTypes";
import { server } from "@/test/server";

const API_BASE = "/ajax-api/2.0/mlflow/caliber";
const NOW = "2026-06-07T18:00:00Z";

function envelope<T>(data: T): { data: T } {
  return { data };
}

function baseIntegration() {
  return {
    integration_id: "OAI-1",
    name: "Ticketing",
    description: "External ticket API",
    owner: "@qa",
    status: "review" as const,
    project_id: null,
    visibility: "user",
    last_imported_version_id: "OAIV-1",
    created_at: NOW,
    updated_at: NOW,
  };
}

function baseVersion() {
  return {
    version_id: "OAIV-1",
    integration_id: "OAI-1",
    source_kind: "inline_text" as const,
    source_ref: "",
    spec_sha256: "abc123",
    openapi_version: "3.0.3",
    title: "Ticket API",
    spec_version: "1",
    spec_description: "",
    server_urls: ["https://tickets.example.com"],
    auth_schemes: ["bearerAuth"],
    import_warnings: [],
    operation_count: 2,
    normalized_summary: {},
    dependency_detected_at: NOW,
    created_by: "@qa",
    created_at: NOW,
  };
}

function baseOperation() {
  return {
    operation_id: "OAIO-1",
    integration_version_id: "OAIV-1",
    operation_key: "GET /tickets/{ticket_id}",
    method: "GET",
    path: "/tickets/{ticket_id}",
    spec_operation_id: "getTicket",
    summary: "Get one ticket",
    description: "",
    tags: ["tickets"],
    deprecated: false,
    side_effect_level: "read" as const,
    auth_schemes: ["bearerAuth"],
    request_body_required: false,
    request_content_types: [],
    response_statuses: ["200"],
    normalized_operation: {},
    created_at: NOW,
  };
}

function baseDraft(): OpenApiToolDraft {
  return {
    draft_id: "OATD-1",
    integration_id: "OAI-1",
    integration_version_id: "OAIV-1",
    operation_id: "OAIO-1",
    additional_operation_ids: [],
    name: "get_ticket",
    description: "Ticketing: Get one ticket",
    owner: "@qa",
    status: "ready" as const,
    server_url: "https://tickets.example.com",
    auth_binding: { kind: "bearer" as const, secret_ref: "env://TICKET_TOKEN" },
    input_schema: { type: "object", properties: {} },
    output_schema: { type: "object", properties: {} },
    execution_config: { kind: "openapi_http" },
    side_effect_level: "read" as const,
    requires_approval: false,
    allow_in_preview: true,
    secret_refs: ["env://TICKET_TOKEN"],
    published_tool_id: null,
    created_at: NOW,
    updated_at: NOW,
  };
}

function baseDependency() {
  return {
    dependency_id: "OAID-1",
    integration_version_id: "OAIV-1",
    from_operation_id: "OAIO-1",
    to_operation_id: "OAIO-2",
    dependency_type: "produces_identifier_for" as const,
    confidence: "medium" as const,
    source: "path_parameter_match",
    required: true,
    binding_field_map: { ticket_id: "id" },
    notes: "Create returns the id later consumed by get.",
    status: "suggested" as const,
    confirmed_by: null,
    confirmed_at: null,
    created_at: NOW,
  };
}

function baseGraphSnapshot() {
  return {
    integration_id: "OAI-1",
    integration_version_id: "OAIV-1",
    nodes: [
      {
        id: "operation:OAIO-1",
        type: "operation",
        label: "POST /tickets",
        data: {
          method: "POST",
          path: "/tickets",
          side_effect_level: "write",
        },
      },
      {
        id: "operation:OAIO-2",
        type: "operation",
        label: "GET /tickets/{ticket_id}",
        data: {
          method: "GET",
          path: "/tickets/{ticket_id}",
          side_effect_level: "read",
        },
      },
      {
        id: "dependency:OAID-1",
        type: "dependency",
        label: "produces_identifier_for",
        data: {
          dependency_type: "produces_identifier_for",
          confidence: "high",
          status: "auto_wired",
          source: "openapi_link",
        },
      },
    ],
    edges: [
      {
        id: "operation:OAIO-1->dependency:OAID-1",
        type: "returns_identifier_for",
        from: "operation:OAIO-1",
        to: "dependency:OAID-1",
        data: {},
      },
      {
        id: "dependency:OAID-1->operation:OAIO-2",
        type: "returns_identifier_for",
        from: "dependency:OAID-1",
        to: "operation:OAIO-2",
        data: {},
      },
    ],
    summary: {
      node_count: 3,
      edge_count: 2,
      operation_count: 2,
      dependency_count: 1,
    },
  };
}

function renderPage(): void {
  render(
    <MemoryRouter
      future={{ v7_startTransition: true, v7_relativeSplatPath: true }}
      initialEntries={["/openapi-integrations"]}
    >
      <Routes>
        <Route path="/openapi-integrations" element={<OpenApiIntegrations />} />
      </Routes>
    </MemoryRouter>,
  );
}

beforeAll(() => server.listen({ onUnhandledRequest: "error" }));
afterEach(() => server.resetHandlers());
afterAll(() => server.close());

describe("OpenApiIntegrations", () => {
  it("lists integrations and opens a detail view", async () => {
    server.use(
      http.get(`${API_BASE}/openapi-integrations`, () =>
        HttpResponse.json(envelope([baseIntegration()])),
      ),
      http.get(`${API_BASE}/openapi-integrations/OAI-1`, () =>
        HttpResponse.json(envelope(baseIntegration())),
      ),
      http.get(`${API_BASE}/openapi-integrations/OAI-1/versions`, () =>
        HttpResponse.json(envelope([baseVersion()])),
      ),
    );

    const user = userEvent.setup();
    renderPage();

    await screen.findByRole("heading", { name: "OpenAPI Integrations" });
    expect(await screen.findByText("Ticketing")).toBeInTheDocument();

    await user.click(screen.getByText("Ticketing"));
    expect(
      await screen.findByRole("heading", { name: /Ticketing/ }),
    ).toBeInTheDocument();
    // Import tab is the default and shows the already-imported version.
    expect(await screen.findByText("OAIV-1")).toBeInTheDocument();
  });

  it("creates a new integration and opens it", async () => {
    let createBody: Record<string, unknown> | null = null;
    server.use(
      http.get(`${API_BASE}/openapi-integrations`, () =>
        HttpResponse.json(envelope([])),
      ),
      http.post(`${API_BASE}/openapi-integrations`, async ({ request }) => {
        createBody = (await request.json()) as Record<string, unknown>;
        return HttpResponse.json(envelope(baseIntegration()), { status: 201 });
      }),
      http.get(`${API_BASE}/openapi-integrations/OAI-1`, () =>
        HttpResponse.json(envelope(baseIntegration())),
      ),
      http.get(`${API_BASE}/openapi-integrations/OAI-1/versions`, () =>
        HttpResponse.json(envelope([])),
      ),
    );

    const user = userEvent.setup();
    renderPage();
    await screen.findByRole("heading", { name: "OpenAPI Integrations" });

    await user.click(screen.getByRole("button", { name: "+ New Integration" }));
    await user.type(screen.getByPlaceholderText("Ticketing API"), "Ticketing");
    await user.type(
      screen.getByPlaceholderText("External ticket API"),
      "External ticket API",
    );
    await user.click(screen.getByRole("button", { name: "Create" }));

    await waitFor(() => expect(createBody).not.toBeNull());
    expect(createBody).toMatchObject({
      name: "Ticketing",
      description: "External ticket API",
    });
    // Navigates straight into the new integration's detail view.
    expect(
      await screen.findByRole("heading", { name: /Ticketing/ }),
    ).toBeInTheDocument();
  });

  it("imports a pasted spec from the Import tab", async () => {
    let importBody: Record<string, unknown> | null = null;
    server.use(
      http.get(`${API_BASE}/openapi-integrations`, () =>
        HttpResponse.json(
          envelope([{ ...baseIntegration(), last_imported_version_id: null }]),
        ),
      ),
      http.get(`${API_BASE}/openapi-integrations/OAI-1`, () =>
        HttpResponse.json(envelope({ ...baseIntegration(), last_imported_version_id: null })),
      ),
      http.get(`${API_BASE}/openapi-integrations/OAI-1/versions`, () =>
        HttpResponse.json(envelope([])),
      ),
      http.post(`${API_BASE}/openapi-integrations/OAI-1/import`, async ({ request }) => {
        importBody = (await request.json()) as Record<string, unknown>;
        return HttpResponse.json(envelope(baseVersion()), { status: 201 });
      }),
    );

    const user = userEvent.setup();
    renderPage();

    await screen.findByRole("heading", { name: "OpenAPI Integrations" });
    await user.click(await screen.findByText("Ticketing"));
    await screen.findByRole("heading", { name: /Ticketing/ });

    const textarea = screen.getByPlaceholderText(
      "paste an OpenAPI 3.x document (JSON or YAML)…",
    );
    await user.type(textarea, "openapi: 3.0.3");
    // Two elements are named "Import": the tab button and this submit button.
    await user.click(screen.getByTestId("import-submit"));

    await waitFor(() => expect(importBody).not.toBeNull());
    expect(importBody).toMatchObject({
      source_kind: "inline_text",
      spec_text: "openapi: 3.0.3",
    });
    expect(await screen.findByText(/Imported OAIV-1/)).toBeInTheDocument();
  });

  it("imports an uploaded spec file without overflowing the browser call stack", async () => {
    let importBody: Record<string, unknown> | null = null;
    server.use(
      http.get(`${API_BASE}/openapi-integrations`, () =>
        HttpResponse.json(
          envelope([{ ...baseIntegration(), last_imported_version_id: null }]),
        ),
      ),
      http.get(`${API_BASE}/openapi-integrations/OAI-1`, () =>
        HttpResponse.json(envelope({ ...baseIntegration(), last_imported_version_id: null })),
      ),
      http.get(`${API_BASE}/openapi-integrations/OAI-1/versions`, () =>
        HttpResponse.json(envelope([])),
      ),
      http.post(`${API_BASE}/openapi-integrations/OAI-1/import`, async ({ request }) => {
        importBody = (await request.json()) as Record<string, unknown>;
        return HttpResponse.json(envelope(baseVersion()), { status: 201 });
      }),
    );

    const bytes = new Uint8Array(300_000);
    for (let index = 0; index < bytes.length; index += 1) {
      bytes[index] = index % 251;
    }
    const expectedBase64 = Buffer.from(bytes).toString("base64");
    const file = new File([bytes], "ticket-api.yaml", { type: "application/yaml" });

    const user = userEvent.setup();
    renderPage();

    await screen.findByRole("heading", { name: "OpenAPI Integrations" });
    await user.click(await screen.findByText("Ticketing"));
    await screen.findByRole("heading", { name: /Ticketing/ });

    await user.click(screen.getByRole("button", { name: "Upload" }));
    await user.upload(screen.getByLabelText("OpenAPI spec file"), file);
    await user.click(screen.getByTestId("import-submit"));

    await waitFor(() => expect(importBody).not.toBeNull());
    expect(importBody).toMatchObject({
      source_kind: "upload",
      source_ref: "ticket-api.yaml",
      spec_base64: expectedBase64,
    });
    expect(await screen.findByText(/Imported OAIV-1/)).toBeInTheDocument();
  });

  it("selects operations and generates a tool draft", async () => {
    let generateBody: Record<string, unknown> | null = null;
    server.use(
      http.get(`${API_BASE}/openapi-integrations`, () =>
        HttpResponse.json(envelope([baseIntegration()])),
      ),
      http.get(`${API_BASE}/openapi-integrations/OAI-1`, () =>
        HttpResponse.json(envelope(baseIntegration())),
      ),
      http.get(`${API_BASE}/openapi-integrations/OAI-1/versions`, () =>
        HttpResponse.json(envelope([baseVersion()])),
      ),
      http.get(`${API_BASE}/openapi-integrations/OAI-1/operations`, () =>
        HttpResponse.json(envelope([baseOperation()])),
      ),
      http.post(
        `${API_BASE}/openapi-integrations/OAI-1/tool-drafts/generate`,
        async ({ request }) => {
          generateBody = (await request.json()) as Record<string, unknown>;
          return HttpResponse.json(envelope([baseDraft()]), { status: 201 });
        },
      ),
    );

    const user = userEvent.setup();
    renderPage();
    await screen.findByRole("heading", { name: "OpenAPI Integrations" });
    await user.click(await screen.findByText("Ticketing"));
    await screen.findByRole("heading", { name: /Ticketing/ });

    await user.click(screen.getByRole("button", { name: "Operations" }));
    await screen.findByText("GET /tickets/{ticket_id}");
    await user.click(screen.getByRole("checkbox"));
    await user.click(screen.getByRole("button", { name: "Generate tool draft(s)" }));

    await waitFor(() => expect(generateBody).not.toBeNull());
    expect(generateBody).toMatchObject({
      operation_ids: ["OAIO-1"],
      group_as_pack: false,
    });
    expect(await screen.findByText(/Generated 1 tool draft/)).toBeInTheDocument();
  });

  it("renders dependencies as operation relationships instead of opaque ids", async () => {
    server.use(
      http.get(`${API_BASE}/openapi-integrations`, () =>
        HttpResponse.json(envelope([baseIntegration()])),
      ),
      http.get(`${API_BASE}/openapi-integrations/OAI-1`, () =>
        HttpResponse.json(envelope(baseIntegration())),
      ),
      http.get(`${API_BASE}/openapi-integrations/OAI-1/versions`, () =>
        HttpResponse.json(envelope([baseVersion()])),
      ),
      http.get(`${API_BASE}/openapi-integrations/OAI-1/dependencies`, () =>
        HttpResponse.json(envelope([baseDependency()])),
      ),
      http.get(`${API_BASE}/openapi-integrations/OAI-1/operations`, () =>
        HttpResponse.json(
          envelope([
            {
              ...baseOperation(),
              operation_id: "OAIO-1",
              operation_key: "POST /tickets",
              method: "POST",
              path: "/tickets",
              summary: "Create a ticket",
              side_effect_level: "write",
            },
            {
              ...baseOperation(),
              operation_id: "OAIO-2",
              operation_key: "GET /tickets/{ticket_id}",
              method: "GET",
              path: "/tickets/{ticket_id}",
              summary: "Get a ticket",
              side_effect_level: "read",
            },
          ]),
        ),
      ),
    );

    const user = userEvent.setup();
    renderPage();
    await screen.findByRole("heading", { name: "OpenAPI Integrations" });
    await user.click(await screen.findByText("Ticketing"));
    await screen.findByRole("heading", { name: /Ticketing/ });

    await user.click(screen.getByRole("button", { name: "Dependencies" }));

    expect(await screen.findByText("Awaiting Review")).toBeInTheDocument();
    expect(await screen.findByText("Produces identifier for")).toBeInTheDocument();
    expect((await screen.findAllByText("POST /tickets")).length).toBeGreaterThan(0);
    expect((await screen.findAllByText("GET /tickets/{ticket_id}")).length).toBeGreaterThan(0);
    expect(await screen.findByText("ticket_id ← id")).toBeInTheDocument();
    expect(await screen.findByRole("button", { name: "Confirm" })).toBeInTheDocument();
  });

  it("publishes a ready tool draft from the Tool Drafts tab", async () => {
    let publishBody: Record<string, unknown> | null = null;
    server.use(
      http.get(`${API_BASE}/openapi-integrations`, () =>
        HttpResponse.json(envelope([baseIntegration()])),
      ),
      http.get(`${API_BASE}/openapi-integrations/OAI-1`, () =>
        HttpResponse.json(envelope(baseIntegration())),
      ),
      http.get(`${API_BASE}/openapi-integrations/OAI-1/versions`, () =>
        HttpResponse.json(envelope([baseVersion()])),
      ),
      http.get(`${API_BASE}/openapi-integrations/OAI-1/tool-drafts`, () =>
        HttpResponse.json(envelope([baseDraft()])),
      ),
      http.post(
        `${API_BASE}/openapi-integrations/OAI-1/tool-drafts/OATD-1/publish`,
        async ({ request }) => {
          publishBody = (await request.json()) as Record<string, unknown>;
          return HttpResponse.json(
            envelope({
              draft: { ...baseDraft(), status: "published", published_tool_id: "TL-1" },
              tool: { tool_id: "TL-1", name: "get_ticket", version: "1.0", execution_backend: "openapi_http" },
            }),
            { status: 201 },
          );
        },
      ),
    );

    const user = userEvent.setup();
    renderPage();
    await screen.findByRole("heading", { name: "OpenAPI Integrations" });
    await user.click(await screen.findByText("Ticketing"));
    await screen.findByRole("heading", { name: /Ticketing/ });

    await user.click(screen.getByRole("button", { name: "Tool Drafts" }));
    await user.click(await screen.findByText("get_ticket"));
    await user.click(screen.getByRole("button", { name: "Publish" }));

    await waitFor(() => expect(publishBody).not.toBeNull());
    expect(await screen.findByTestId("openapi-publication-success")).toHaveTextContent(
      "Tool published successfully",
    );
    expect(screen.getByTestId("openapi-publication-success")).toHaveTextContent("TL-1");
    expect(screen.getByTestId("openapi-publication-success")).toHaveTextContent("openapi_http");
    expect(screen.getByRole("link", { name: "Open in Tool Registry" })).toHaveAttribute(
      "href",
      "/tools/TL-1",
    );
  });

  it("shows readable input and output signatures for a tool draft", async () => {
    const draft = {
      ...baseDraft(),
      input_schema: {
        type: "object",
        required: ["ticket_id"],
        properties: {
          ticket_id: { type: "string" },
          include_comments: { type: "boolean" },
        },
      },
      output_schema: {
        type: "object",
        properties: {
          ticket: { type: "object" },
          status: { type: "string" },
        },
      },
    };

    server.use(
      http.get(`${API_BASE}/openapi-integrations`, () =>
        HttpResponse.json(envelope([baseIntegration()])),
      ),
      http.get(`${API_BASE}/openapi-integrations/OAI-1`, () =>
        HttpResponse.json(envelope(baseIntegration())),
      ),
      http.get(`${API_BASE}/openapi-integrations/OAI-1/versions`, () =>
        HttpResponse.json(envelope([baseVersion()])),
      ),
      http.get(`${API_BASE}/openapi-integrations/OAI-1/tool-drafts`, () =>
        HttpResponse.json(envelope([draft])),
      ),
    );

    const user = userEvent.setup();
    renderPage();
    await screen.findByRole("heading", { name: "OpenAPI Integrations" });
    await user.click(await screen.findByText("Ticketing"));
    await screen.findByRole("heading", { name: /Ticketing/ });

    await user.click(screen.getByRole("button", { name: "Tool Drafts" }));
    await user.click(await screen.findByText("get_ticket"));

    expect(await screen.findByText("Callable Signature")).toBeInTheDocument();
    expect(await screen.findByText(/get_ticket\(input:/)).toBeInTheDocument();
    expect((await screen.findAllByText(/ticket_id/)).length).toBeGreaterThan(0);
    expect((await screen.findAllByText(/include_comments/)).length).toBeGreaterThan(0);
    expect(await screen.findByText("Input Signature")).toBeInTheDocument();
    expect(await screen.findByText("Output Signature")).toBeInTheDocument();
  });

  it("saves an OAuth client-credentials auth binding for a draft", async () => {
    let updateBody: Record<string, unknown> | null = null;
    let currentDraft = baseDraft();
    server.use(
      http.get(`${API_BASE}/openapi-integrations`, () =>
        HttpResponse.json(envelope([baseIntegration()])),
      ),
      http.get(`${API_BASE}/openapi-integrations/OAI-1`, () =>
        HttpResponse.json(envelope(baseIntegration())),
      ),
      http.get(`${API_BASE}/openapi-integrations/OAI-1/versions`, () =>
        HttpResponse.json(envelope([baseVersion()])),
      ),
      http.get(`${API_BASE}/openapi-integrations/OAI-1/tool-drafts`, () =>
        HttpResponse.json(envelope([currentDraft])),
      ),
      http.patch(
        `${API_BASE}/openapi-integrations/OAI-1/tool-drafts/OATD-1`,
        async ({ request }) => {
          updateBody = (await request.json()) as Record<string, unknown>;
          currentDraft = {
            ...currentDraft,
            auth_binding: {
              kind: "oauth_client_credentials",
              token_url: "https://issuer.example.com/oauth/token",
              client_id: "ticket-bot",
              client_secret_ref: "env://OAUTH_CLIENT_SECRET",
              scopes: ["tickets.read", "tickets.write"],
              audience: "tickets-api",
              resource: "tickets",
              client_auth_method: "body",
            },
          };
          return HttpResponse.json(envelope(currentDraft));
        },
      ),
    );

    const user = userEvent.setup();
    renderPage();
    await screen.findByRole("heading", { name: "OpenAPI Integrations" });
    await user.click(await screen.findByText("Ticketing"));
    await screen.findByRole("heading", { name: /Ticketing/ });

    await user.click(screen.getByRole("button", { name: "Tool Drafts" }));
    await user.click(await screen.findByText("get_ticket"));

    await user.selectOptions(screen.getByRole("combobox", { name: "Auth kind" }), [
      "oauth_client_credentials",
    ]);
    await user.type(
      screen.getByPlaceholderText("https://issuer.example.com/oauth/token"),
      "https://issuer.example.com/oauth/token",
    );
    await user.selectOptions(screen.getByRole("combobox", { name: "Client auth method" }), [
      "body",
    ]);
    await user.type(screen.getByPlaceholderText("client_id"), "ticket-bot");
    await user.type(
      screen.getByPlaceholderText("env://OAUTH_CLIENT_SECRET (optional for refresh token)"),
      "env://OAUTH_CLIENT_SECRET",
    );
    await user.type(
      screen.getByPlaceholderText("scopes (comma-separated)"),
      "tickets.read, tickets.write",
    );
    await user.type(screen.getByPlaceholderText("audience (optional)"), "tickets-api");
    await user.type(screen.getByPlaceholderText("resource (optional)"), "tickets");
    await user.click(screen.getByRole("button", { name: "Save" }));

    await waitFor(() => expect(updateBody).not.toBeNull());
    expect(updateBody).toMatchObject({
      auth_binding: {
        kind: "oauth_client_credentials",
        token_url: "https://issuer.example.com/oauth/token",
        client_id: "ticket-bot",
        client_secret_ref: "env://OAUTH_CLIENT_SECRET",
        scopes: ["tickets.read", "tickets.write"],
        audience: "tickets-api",
        resource: "tickets",
        client_auth_method: "body",
      },
    });
    expect(await screen.findByText("Saved.")).toBeInTheDocument();
  });

  it("renders the API dependency graph and knowledge graph JSON", async () => {
    server.use(
      http.get(`${API_BASE}/openapi-integrations`, () =>
        HttpResponse.json(envelope([baseIntegration()])),
      ),
      http.get(`${API_BASE}/openapi-integrations/OAI-1`, () =>
        HttpResponse.json(envelope(baseIntegration())),
      ),
      http.get(`${API_BASE}/openapi-integrations/OAI-1/versions`, () =>
        HttpResponse.json(envelope([baseVersion()])),
      ),
      http.get(`${API_BASE}/openapi-integrations/OAI-1/graph`, () =>
        HttpResponse.json(envelope(baseGraphSnapshot())),
      ),
    );

    const user = userEvent.setup();
    renderPage();
    await screen.findByRole("heading", { name: "OpenAPI Integrations" });
    await user.click(await screen.findByText("Ticketing"));
    await screen.findByRole("heading", { name: /Ticketing/ });

    await user.click(screen.getByRole("button", { name: "Graph" }));

    expect(await screen.findByText("API Dependency Graph")).toBeInTheDocument();
    expect(await screen.findByTestId("openapi-graph-canvas")).toBeInTheDocument();
    expect(await screen.findByText("API Knowledge Graph JSON")).toBeInTheDocument();
    expect(await screen.findByText("Dependency Edges")).toBeInTheDocument();
    const jsonPanel = await screen.findByLabelText("API knowledge graph JSON");
    expect(jsonPanel.textContent).toContain('"operations"');
    expect(jsonPanel.textContent).toContain('"dependencies"');
    expect(jsonPanel.textContent).toContain("POST /tickets");
    expect(jsonPanel.textContent).toContain("GET /tickets/{ticket_id}");

    await user.click(screen.getByRole("button", { name: "Inspect POST /tickets" }));
    expect(await screen.findByTestId("openapi-graph-inspector")).toHaveTextContent(
      "Supplies context to",
    );
    expect(screen.getByTestId("openapi-graph-inspector")).toHaveTextContent("GET /tickets/{ticket_id}");

    await user.type(screen.getByLabelText("Search operations"), "ticket_id");
    expect(screen.getByTestId("openapi-graph-canvas")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Inspect POST /tickets" })).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Inspect GET /tickets/{ticket_id}" })).toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: "Reset" }));
    await user.selectOptions(screen.getByLabelText("Filter dependencies by status"), "confirmed");
    expect(screen.getByText("0 of 1 relationship")).toBeInTheDocument();

    await user.selectOptions(screen.getByLabelText("Filter dependencies by status"), "all");
    await user.click(screen.getByRole("button", { name: "tree" }));
    expect(await screen.findByTestId("openapi-tree-view")).toHaveTextContent("POST /tickets");
    expect(screen.getByTestId("openapi-tree-view")).toHaveTextContent("GET /tickets/{ticket_id}");

    await user.click(screen.getByRole("button", { name: "flow" }));
    expect(await screen.findByTestId("openapi-flow-view")).toHaveTextContent("POST /tickets");
    expect(screen.getByTestId("openapi-flow-view")).toHaveTextContent("GET /tickets/{ticket_id}");
  });

  it("navigates back to the list from the detail breadcrumb", async () => {
    server.use(
      http.get(`${API_BASE}/openapi-integrations`, () =>
        HttpResponse.json(envelope([baseIntegration()])),
      ),
      http.get(`${API_BASE}/openapi-integrations/OAI-1`, () =>
        HttpResponse.json(envelope(baseIntegration())),
      ),
      http.get(`${API_BASE}/openapi-integrations/OAI-1/versions`, () =>
        HttpResponse.json(envelope([baseVersion()])),
      ),
    );

    const user = userEvent.setup();
    renderPage();
    await screen.findByRole("heading", { name: "OpenAPI Integrations" });
    await user.click(await screen.findByText("Ticketing"));
    await screen.findByRole("heading", { name: /Ticketing/ });

    await user.click(screen.getByRole("button", { name: "OpenAPI Integrations" }));

    expect(await screen.findByRole("heading", { name: "OpenAPI Integrations" })).toBeInTheDocument();
    expect(screen.queryByRole("heading", { name: /Ticketing/ })).not.toBeInTheDocument();
  });

  it("cancels the create-integration panel without creating anything", async () => {
    let createCalled = false;
    server.use(
      http.get(`${API_BASE}/openapi-integrations`, () =>
        HttpResponse.json(envelope([])),
      ),
      http.post(`${API_BASE}/openapi-integrations`, () => {
        createCalled = true;
        return HttpResponse.json(envelope(baseIntegration()), { status: 201 });
      }),
    );

    const user = userEvent.setup();
    renderPage();
    await screen.findByRole("heading", { name: "OpenAPI Integrations" });

    await user.click(screen.getByRole("button", { name: "+ New Integration" }));
    await user.type(screen.getByPlaceholderText("Ticketing API"), "Ticketing");

    // Two "Cancel" buttons exist while the panel is open: the header toggle and
    // the panel's own Cancel action. Click the panel's, exercising its onCancel.
    const cancelButtons = screen.getAllByRole("button", { name: "Cancel" });
    await user.click(cancelButtons[cancelButtons.length - 1]!);

    expect(screen.queryByPlaceholderText("Ticketing API")).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "+ New Integration" })).toBeInTheDocument();
    expect(createCalled).toBe(false);
  });

  it("shows an error when creating an integration fails", async () => {
    server.use(
      http.get(`${API_BASE}/openapi-integrations`, () =>
        HttpResponse.json(envelope([])),
      ),
      http.post(`${API_BASE}/openapi-integrations`, () =>
        HttpResponse.json({ detail: "name already exists" }, { status: 400 }),
      ),
    );

    const user = userEvent.setup();
    renderPage();
    await screen.findByRole("heading", { name: "OpenAPI Integrations" });

    await user.click(screen.getByRole("button", { name: "+ New Integration" }));
    await user.type(screen.getByPlaceholderText("Ticketing API"), "Ticketing");
    await user.click(screen.getByRole("button", { name: "Create" }));

    expect(await screen.findByText("name already exists")).toBeInTheDocument();
  });

  it("archives an integration from the detail header", async () => {
    let status: string = "review";
    let archiveCalled = false;
    server.use(
      http.get(`${API_BASE}/openapi-integrations`, () =>
        HttpResponse.json(envelope([{ ...baseIntegration(), status }])),
      ),
      http.get(`${API_BASE}/openapi-integrations/OAI-1`, () =>
        HttpResponse.json(envelope({ ...baseIntegration(), status })),
      ),
      http.get(`${API_BASE}/openapi-integrations/OAI-1/versions`, () =>
        HttpResponse.json(envelope([baseVersion()])),
      ),
      http.post(`${API_BASE}/openapi-integrations/OAI-1/archive`, () => {
        archiveCalled = true;
        status = "archived";
        return HttpResponse.json(envelope({ ...baseIntegration(), status }));
      }),
    );

    const user = userEvent.setup();
    renderPage();
    await screen.findByRole("heading", { name: "OpenAPI Integrations" });
    await user.click(await screen.findByText("Ticketing"));
    await screen.findByRole("heading", { name: /Ticketing/ });

    await user.click(screen.getByRole("button", { name: "Archive" }));

    await waitFor(() => expect(archiveCalled).toBe(true));
    await waitFor(() =>
      expect(screen.queryByRole("button", { name: "Archive" })).not.toBeInTheDocument(),
    );
  });

  it("checks a spec URL for reachability, both when usable and not", async () => {
    let probeCount = 0;
    server.use(
      http.get(`${API_BASE}/openapi-integrations`, () =>
        HttpResponse.json(envelope([baseIntegration()])),
      ),
      http.get(`${API_BASE}/openapi-integrations/OAI-1`, () =>
        HttpResponse.json(envelope(baseIntegration())),
      ),
      http.get(`${API_BASE}/openapi-integrations/OAI-1/versions`, () =>
        HttpResponse.json(envelope([baseVersion()])),
      ),
      http.post(`${API_BASE}/openapi-integrations/OAI-1/validate-spec-source`, () => {
        probeCount += 1;
        return probeCount === 1
          ? HttpResponse.json(
              envelope({
                source_kind: "url",
                reachable: true,
                allowed: true,
                status_code: 200,
                detail: "ok",
              }),
            )
          : HttpResponse.json(
              envelope({
                source_kind: "url",
                reachable: false,
                allowed: false,
                detail: "blocked by allowlist",
              }),
            );
      }),
    );

    const user = userEvent.setup();
    renderPage();
    await screen.findByRole("heading", { name: "OpenAPI Integrations" });
    await user.click(await screen.findByText("Ticketing"));
    await screen.findByRole("heading", { name: /Ticketing/ });

    await user.click(screen.getByRole("button", { name: "URL" }));
    await user.type(
      screen.getByPlaceholderText("https://example.com/openapi.json"),
      "https://example.com/openapi.json",
    );
    await user.click(screen.getByRole("button", { name: "Check" }));
    expect(await screen.findByText("Reachable (200)")).toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: "Check" }));
    expect(await screen.findByText("Not usable: blocked by allowlist")).toBeInTheDocument();
  });

  it("imports a spec from a URL source", async () => {
    let importBody: Record<string, unknown> | null = null;
    server.use(
      http.get(`${API_BASE}/openapi-integrations`, () =>
        HttpResponse.json(
          envelope([{ ...baseIntegration(), last_imported_version_id: null }]),
        ),
      ),
      http.get(`${API_BASE}/openapi-integrations/OAI-1`, () =>
        HttpResponse.json(envelope({ ...baseIntegration(), last_imported_version_id: null })),
      ),
      http.get(`${API_BASE}/openapi-integrations/OAI-1/versions`, () =>
        HttpResponse.json(envelope([])),
      ),
      http.post(`${API_BASE}/openapi-integrations/OAI-1/import`, async ({ request }) => {
        importBody = (await request.json()) as Record<string, unknown>;
        return HttpResponse.json(envelope(baseVersion()), { status: 201 });
      }),
    );

    const user = userEvent.setup();
    renderPage();
    await screen.findByRole("heading", { name: "OpenAPI Integrations" });
    await user.click(await screen.findByText("Ticketing"));
    await screen.findByRole("heading", { name: /Ticketing/ });

    await user.click(screen.getByRole("button", { name: "URL" }));
    await user.type(
      screen.getByPlaceholderText("https://example.com/openapi.json"),
      "https://example.com/openapi.json",
    );
    await user.click(screen.getByTestId("import-submit"));

    await waitFor(() => expect(importBody).not.toBeNull());
    expect(importBody).toMatchObject({
      source_kind: "url",
      spec_url: "https://example.com/openapi.json",
    });
    expect(await screen.findByText(/Imported OAIV-1/)).toBeInTheDocument();
  });

  it("shows an error when the import request fails", async () => {
    server.use(
      http.get(`${API_BASE}/openapi-integrations`, () =>
        HttpResponse.json(envelope([baseIntegration()])),
      ),
      http.get(`${API_BASE}/openapi-integrations/OAI-1`, () =>
        HttpResponse.json(envelope(baseIntegration())),
      ),
      http.get(`${API_BASE}/openapi-integrations/OAI-1/versions`, () =>
        HttpResponse.json(envelope([baseVersion()])),
      ),
      http.post(`${API_BASE}/openapi-integrations/OAI-1/import`, () =>
        HttpResponse.json({ detail: "invalid spec" }, { status: 422 }),
      ),
    );

    const user = userEvent.setup();
    renderPage();
    await screen.findByRole("heading", { name: "OpenAPI Integrations" });
    await user.click(await screen.findByText("Ticketing"));
    await screen.findByRole("heading", { name: /Ticketing/ });

    await user.type(
      screen.getByPlaceholderText("paste an OpenAPI 3.x document (JSON or YAML)…"),
      "openapi: 3.0.3",
    );
    await user.click(screen.getByTestId("import-submit"));

    expect(await screen.findByText("invalid spec")).toBeInTheDocument();
  });

  it("reimports the last URL source and shows the diff summary", async () => {
    server.use(
      http.get(`${API_BASE}/openapi-integrations`, () =>
        HttpResponse.json(envelope([baseIntegration()])),
      ),
      http.get(`${API_BASE}/openapi-integrations/OAI-1`, () =>
        HttpResponse.json(envelope(baseIntegration())),
      ),
      http.get(`${API_BASE}/openapi-integrations/OAI-1/versions`, () =>
        HttpResponse.json(envelope([baseVersion()])),
      ),
      http.post(`${API_BASE}/openapi-integrations/OAI-1/reimport`, () =>
        HttpResponse.json(
          envelope({
            version: { ...baseVersion(), version_id: "OAIV-2" },
            previous_version_id: "OAIV-1",
            diff: {
              from_version_id: "OAIV-1",
              to_version_id: "OAIV-2",
              added: ["POST /tickets/{id}/close"],
              removed: [],
              changed: [],
              unchanged: [],
              breaking: [],
              summary: {
                added_count: 1,
                removed_count: 0,
                changed_count: 2,
                unchanged_count: 5,
                breaking_count: 0,
              },
            },
          }),
        ),
      ),
    );

    const user = userEvent.setup();
    renderPage();
    await screen.findByRole("heading", { name: "OpenAPI Integrations" });
    await user.click(await screen.findByText("Ticketing"));
    await screen.findByRole("heading", { name: /Ticketing/ });

    await user.click(screen.getByRole("button", { name: "Reimport & diff" }));

    expect(
      await screen.findByText("Re-imported OAIV-2: +1 -0 changed 2 (0 breaking)"),
    ).toBeInTheDocument();
  });

  it("shows an error when reimport fails", async () => {
    server.use(
      http.get(`${API_BASE}/openapi-integrations`, () =>
        HttpResponse.json(envelope([baseIntegration()])),
      ),
      http.get(`${API_BASE}/openapi-integrations/OAI-1`, () =>
        HttpResponse.json(envelope(baseIntegration())),
      ),
      http.get(`${API_BASE}/openapi-integrations/OAI-1/versions`, () =>
        HttpResponse.json(envelope([baseVersion()])),
      ),
      http.post(`${API_BASE}/openapi-integrations/OAI-1/reimport`, () =>
        HttpResponse.json({ detail: "source unreachable" }, { status: 502 }),
      ),
    );

    const user = userEvent.setup();
    renderPage();
    await screen.findByRole("heading", { name: "OpenAPI Integrations" });
    await user.click(await screen.findByText("Ticketing"));
    await screen.findByRole("heading", { name: /Ticketing/ });

    await user.click(screen.getByRole("button", { name: "Reimport & diff" }));

    expect(await screen.findByText("source unreachable")).toBeInTheDocument();
  });

  it("shows an error state when operations fail to load", async () => {
    server.use(
      http.get(`${API_BASE}/openapi-integrations`, () =>
        HttpResponse.json(envelope([baseIntegration()])),
      ),
      http.get(`${API_BASE}/openapi-integrations/OAI-1`, () =>
        HttpResponse.json(envelope(baseIntegration())),
      ),
      http.get(`${API_BASE}/openapi-integrations/OAI-1/versions`, () =>
        HttpResponse.json(envelope([baseVersion()])),
      ),
      http.get(`${API_BASE}/openapi-integrations/OAI-1/operations`, () =>
        HttpResponse.json({ detail: "operations unavailable" }, { status: 500 }),
      ),
    );

    const user = userEvent.setup();
    renderPage();
    await screen.findByRole("heading", { name: "OpenAPI Integrations" });
    await user.click(await screen.findByText("Ticketing"));
    await screen.findByRole("heading", { name: /Ticketing/ });

    await user.click(screen.getByRole("button", { name: "Operations" }));
    expect(await screen.findByText("operations unavailable")).toBeInTheDocument();
  });

  it("generates a grouped tool-draft pack from multiple selected operations", async () => {
    let generateBody: Record<string, unknown> | null = null;
    server.use(
      http.get(`${API_BASE}/openapi-integrations`, () =>
        HttpResponse.json(envelope([baseIntegration()])),
      ),
      http.get(`${API_BASE}/openapi-integrations/OAI-1`, () =>
        HttpResponse.json(envelope(baseIntegration())),
      ),
      http.get(`${API_BASE}/openapi-integrations/OAI-1/versions`, () =>
        HttpResponse.json(envelope([baseVersion()])),
      ),
      http.get(`${API_BASE}/openapi-integrations/OAI-1/operations`, () =>
        HttpResponse.json(
          envelope([
            baseOperation(),
            {
              ...baseOperation(),
              operation_id: "OAIO-2",
              operation_key: "POST /tickets",
              method: "POST",
              path: "/tickets",
              summary: "Create a ticket",
              side_effect_level: "write",
            },
          ]),
        ),
      ),
      http.post(
        `${API_BASE}/openapi-integrations/OAI-1/tool-drafts/generate`,
        async ({ request }) => {
          generateBody = (await request.json()) as Record<string, unknown>;
          return HttpResponse.json(envelope([baseDraft()]), { status: 201 });
        },
      ),
    );

    const user = userEvent.setup();
    renderPage();
    await screen.findByRole("heading", { name: "OpenAPI Integrations" });
    await user.click(await screen.findByText("Ticketing"));
    await screen.findByRole("heading", { name: /Ticketing/ });

    await user.click(screen.getByRole("button", { name: "Operations" }));
    await screen.findByText("GET /tickets/{ticket_id}");
    const checkboxes = screen.getAllByRole("checkbox");
    await user.click(checkboxes[0]!);
    await user.click(checkboxes[1]!);
    await user.click(screen.getByLabelText("bind as one tool pack"));
    await user.click(screen.getByRole("button", { name: "Generate tool draft(s)" }));

    await waitFor(() => expect(generateBody).not.toBeNull());
    expect(generateBody).toMatchObject({ group_as_pack: true });
    expect((generateBody!.operation_ids as string[]).sort()).toEqual(["OAIO-1", "OAIO-2"]);
    expect(await screen.findByText(/Generated 1 tool draft/)).toBeInTheDocument();
  });

  it("shows an error message when tool draft generation fails", async () => {
    server.use(
      http.get(`${API_BASE}/openapi-integrations`, () =>
        HttpResponse.json(envelope([baseIntegration()])),
      ),
      http.get(`${API_BASE}/openapi-integrations/OAI-1`, () =>
        HttpResponse.json(envelope(baseIntegration())),
      ),
      http.get(`${API_BASE}/openapi-integrations/OAI-1/versions`, () =>
        HttpResponse.json(envelope([baseVersion()])),
      ),
      http.get(`${API_BASE}/openapi-integrations/OAI-1/operations`, () =>
        HttpResponse.json(envelope([baseOperation()])),
      ),
      http.post(`${API_BASE}/openapi-integrations/OAI-1/tool-drafts/generate`, () =>
        HttpResponse.json({ detail: "cannot generate" }, { status: 400 }),
      ),
    );

    const user = userEvent.setup();
    renderPage();
    await screen.findByRole("heading", { name: "OpenAPI Integrations" });
    await user.click(await screen.findByText("Ticketing"));
    await screen.findByRole("heading", { name: /Ticketing/ });

    await user.click(screen.getByRole("button", { name: "Operations" }));
    await screen.findByText("GET /tickets/{ticket_id}");
    await user.click(screen.getByRole("checkbox"));
    await user.click(screen.getByRole("button", { name: "Generate tool draft(s)" }));

    expect(await screen.findByText("Failed: cannot generate")).toBeInTheDocument();
  });

  it("shows an error state when dependencies fail to load", async () => {
    server.use(
      http.get(`${API_BASE}/openapi-integrations`, () =>
        HttpResponse.json(envelope([baseIntegration()])),
      ),
      http.get(`${API_BASE}/openapi-integrations/OAI-1`, () =>
        HttpResponse.json(envelope(baseIntegration())),
      ),
      http.get(`${API_BASE}/openapi-integrations/OAI-1/versions`, () =>
        HttpResponse.json(envelope([baseVersion()])),
      ),
      http.get(`${API_BASE}/openapi-integrations/OAI-1/dependencies`, () =>
        HttpResponse.json({ detail: "dependencies unavailable" }, { status: 500 }),
      ),
      http.get(`${API_BASE}/openapi-integrations/OAI-1/operations`, () =>
        HttpResponse.json(envelope([])),
      ),
    );

    const user = userEvent.setup();
    renderPage();
    await screen.findByRole("heading", { name: "OpenAPI Integrations" });
    await user.click(await screen.findByText("Ticketing"));
    await screen.findByRole("heading", { name: /Ticketing/ });

    await user.click(screen.getByRole("button", { name: "Dependencies" }));
    expect(await screen.findByText("dependencies unavailable")).toBeInTheDocument();
  });

  it("filters dependency rows by status and reviews a suggested one", async () => {
    let reviewedId: string | null = null;
    let reviewedStatus: string | null = null;
    server.use(
      http.get(`${API_BASE}/openapi-integrations`, () =>
        HttpResponse.json(envelope([baseIntegration()])),
      ),
      http.get(`${API_BASE}/openapi-integrations/OAI-1`, () =>
        HttpResponse.json(envelope(baseIntegration())),
      ),
      http.get(`${API_BASE}/openapi-integrations/OAI-1/versions`, () =>
        HttpResponse.json(envelope([baseVersion()])),
      ),
      http.get(`${API_BASE}/openapi-integrations/OAI-1/dependencies`, () =>
        HttpResponse.json(
          envelope([
            baseDependency(),
            { ...baseDependency(), dependency_id: "OAID-2", status: "auto_wired" },
            { ...baseDependency(), dependency_id: "OAID-3", status: "confirmed" },
            { ...baseDependency(), dependency_id: "OAID-4", status: "rejected" },
          ]),
        ),
      ),
      http.get(`${API_BASE}/openapi-integrations/OAI-1/operations`, () =>
        HttpResponse.json(envelope([baseOperation()])),
      ),
      http.patch(
        `${API_BASE}/openapi-integrations/OAI-1/dependencies/:depId`,
        async ({ request, params }) => {
          reviewedId = params.depId as string;
          const body = (await request.json()) as { status: string };
          reviewedStatus = body.status;
          return HttpResponse.json(envelope({ ...baseDependency(), status: body.status }));
        },
      ),
    );

    const user = userEvent.setup();
    renderPage();
    await screen.findByRole("heading", { name: "OpenAPI Integrations" });
    await user.click(await screen.findByText("Ticketing"));
    await screen.findByRole("heading", { name: /Ticketing/ });

    await user.click(screen.getByRole("button", { name: "Dependencies" }));
    await screen.findByText("Awaiting Review");

    // Default filter ("awaiting review") shows only the suggested row, with
    // Confirm/Reject actions.
    expect(await screen.findByRole("button", { name: "Confirm" })).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Confirm" }));
    await waitFor(() => expect(reviewedId).toBe("OAID-1"));
    expect(reviewedStatus).toBe("confirmed");

    // The mock list is static, so the row (and its Reject action) is still
    // showing under the "awaiting review" filter — exercise the reject path too.
    await user.click(screen.getByRole("button", { name: "Reject" }));
    await waitFor(() => expect(reviewedStatus).toBe("rejected"));

    // Auto-wired rows cannot be reviewed.
    await user.click(screen.getByText("Auto-wired").closest("button")!);
    await waitFor(() =>
      expect(screen.queryByRole("button", { name: "Confirm" })).not.toBeInTheDocument(),
    );
    expect(screen.queryByRole("button", { name: "Reject" })).not.toBeInTheDocument();

    await user.click(screen.getAllByText("Rejected")[0]!.closest("button")!);
    expect(await screen.findByText("ticket_id ← id")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Reject" })).not.toBeInTheDocument();

    await user.click(screen.getByText("All Rows").closest("button")!);
    await waitFor(() => expect(screen.getAllByText("ticket_id ← id").length).toBe(4));
  });

  it("fills auth binding fields across api_key, header, basic, bearer, and oauth_refresh_token kinds", async () => {
    let updateBody: Record<string, unknown> | null = null;
    server.use(
      http.get(`${API_BASE}/openapi-integrations`, () =>
        HttpResponse.json(envelope([baseIntegration()])),
      ),
      http.get(`${API_BASE}/openapi-integrations/OAI-1`, () =>
        HttpResponse.json(envelope(baseIntegration())),
      ),
      http.get(`${API_BASE}/openapi-integrations/OAI-1/versions`, () =>
        HttpResponse.json(envelope([baseVersion()])),
      ),
      http.get(`${API_BASE}/openapi-integrations/OAI-1/tool-drafts`, () =>
        HttpResponse.json(envelope([baseDraft()])),
      ),
      http.patch(
        `${API_BASE}/openapi-integrations/OAI-1/tool-drafts/OATD-1`,
        async ({ request }) => {
          updateBody = (await request.json()) as Record<string, unknown>;
          return HttpResponse.json(envelope(baseDraft()));
        },
      ),
    );

    const user = userEvent.setup();
    renderPage();
    await screen.findByRole("heading", { name: "OpenAPI Integrations" });
    await user.click(await screen.findByText("Ticketing"));
    await screen.findByRole("heading", { name: /Ticketing/ });
    await user.click(screen.getByRole("button", { name: "Tool Drafts" }));
    await user.click(await screen.findByText("get_ticket"));

    const authKind = screen.getByRole("combobox", { name: "Auth kind" });

    await user.selectOptions(authKind, ["api_key"]);
    await user.type(
      screen.getByPlaceholderText("env://MY_TOKEN or secret://name"),
      "env://API_KEY",
    );
    await user.type(screen.getByPlaceholderText("X-API-Key header name"), "X-Api-Key");
    await user.type(screen.getByPlaceholderText("api_key query param (optional)"), "api_key");
    expect(screen.getByPlaceholderText("X-API-Key header name")).toHaveValue("X-Api-Key");

    await user.selectOptions(authKind, ["header"]);
    await user.type(screen.getByPlaceholderText("header name"), "X-Custom");
    await user.type(screen.getByPlaceholderText("prefix (optional)"), "Token ");
    expect(screen.getByPlaceholderText("header name")).toHaveValue("X-Custom");

    await user.selectOptions(authKind, ["basic"]);
    await user.type(screen.getByPlaceholderText("username"), "svc-user");
    await user.type(screen.getByPlaceholderText("env://MY_PASSWORD"), "env://PASSWORD");
    expect(screen.getByPlaceholderText("username")).toHaveValue("svc-user");

    await user.selectOptions(authKind, ["bearer"]);
    await user.type(screen.getByPlaceholderText("Bearer prefix (optional)"), "Bearer ");
    expect(screen.getByPlaceholderText("Bearer prefix (optional)")).toHaveValue("Bearer ");

    await user.selectOptions(authKind, ["oauth_refresh_token"]);
    await user.type(
      screen.getByPlaceholderText("https://issuer.example.com/oauth/token"),
      "https://issuer.example.com/token",
    );
    await user.type(screen.getByPlaceholderText("client_id"), "ticket-bot");
    await user.type(
      screen.getByPlaceholderText("env://OAUTH_CLIENT_SECRET (optional for refresh token)"),
      "env://SECRET",
    );
    await user.type(
      screen.getByPlaceholderText("env://OAUTH_REFRESH_TOKEN"),
      "env://REFRESH",
    );
    await user.click(screen.getByRole("button", { name: "Save" }));

    await waitFor(() => expect(updateBody).not.toBeNull());
    expect(updateBody).toMatchObject({
      auth_binding: {
        kind: "oauth_refresh_token",
        token_url: "https://issuer.example.com/token",
        client_id: "ticket-bot",
        client_secret_ref: "env://SECRET",
        refresh_token_secret_ref: "env://REFRESH",
      },
    });
  });

  it("shows an error when saving a tool draft's configuration fails", async () => {
    server.use(
      http.get(`${API_BASE}/openapi-integrations`, () =>
        HttpResponse.json(envelope([baseIntegration()])),
      ),
      http.get(`${API_BASE}/openapi-integrations/OAI-1`, () =>
        HttpResponse.json(envelope(baseIntegration())),
      ),
      http.get(`${API_BASE}/openapi-integrations/OAI-1/versions`, () =>
        HttpResponse.json(envelope([baseVersion()])),
      ),
      http.get(`${API_BASE}/openapi-integrations/OAI-1/tool-drafts`, () =>
        HttpResponse.json(envelope([baseDraft()])),
      ),
      http.patch(`${API_BASE}/openapi-integrations/OAI-1/tool-drafts/OATD-1`, () =>
        HttpResponse.json({ detail: "invalid server url" }, { status: 400 }),
      ),
    );

    const user = userEvent.setup();
    renderPage();
    await screen.findByRole("heading", { name: "OpenAPI Integrations" });
    await user.click(await screen.findByText("Ticketing"));
    await screen.findByRole("heading", { name: /Ticketing/ });
    await user.click(screen.getByRole("button", { name: "Tool Drafts" }));
    await user.click(await screen.findByText("get_ticket"));

    await user.click(screen.getByRole("button", { name: "Save" }));

    expect(await screen.findByText("invalid server url")).toBeInTheDocument();
  });

  it("toggles the allow-in-preview flag for a tool draft", async () => {
    let updateBody: Record<string, unknown> | null = null;
    server.use(
      http.get(`${API_BASE}/openapi-integrations`, () =>
        HttpResponse.json(envelope([baseIntegration()])),
      ),
      http.get(`${API_BASE}/openapi-integrations/OAI-1`, () =>
        HttpResponse.json(envelope(baseIntegration())),
      ),
      http.get(`${API_BASE}/openapi-integrations/OAI-1/versions`, () =>
        HttpResponse.json(envelope([baseVersion()])),
      ),
      http.get(`${API_BASE}/openapi-integrations/OAI-1/tool-drafts`, () =>
        HttpResponse.json(envelope([baseDraft()])),
      ),
      http.patch(
        `${API_BASE}/openapi-integrations/OAI-1/tool-drafts/OATD-1`,
        async ({ request }) => {
          updateBody = (await request.json()) as Record<string, unknown>;
          return HttpResponse.json(envelope({ ...baseDraft(), allow_in_preview: false }));
        },
      ),
    );

    const user = userEvent.setup();
    renderPage();
    await screen.findByRole("heading", { name: "OpenAPI Integrations" });
    await user.click(await screen.findByText("Ticketing"));
    await screen.findByRole("heading", { name: /Ticketing/ });
    await user.click(screen.getByRole("button", { name: "Tool Drafts" }));
    await user.click(await screen.findByText("get_ticket"));

    const previewToggle = screen.getByLabelText("allow live preview (fires a real request)");
    expect(previewToggle).toBeChecked();
    await user.click(previewToggle);

    await waitFor(() => expect(updateBody).toMatchObject({ allow_in_preview: false }));
  });

  it("runs a live preview against a tool draft and surfaces failures", async () => {
    let previewCount = 0;
    server.use(
      http.get(`${API_BASE}/openapi-integrations`, () =>
        HttpResponse.json(envelope([baseIntegration()])),
      ),
      http.get(`${API_BASE}/openapi-integrations/OAI-1`, () =>
        HttpResponse.json(envelope(baseIntegration())),
      ),
      http.get(`${API_BASE}/openapi-integrations/OAI-1/versions`, () =>
        HttpResponse.json(envelope([baseVersion()])),
      ),
      http.get(`${API_BASE}/openapi-integrations/OAI-1/tool-drafts`, () =>
        HttpResponse.json(envelope([baseDraft()])),
      ),
      http.post(
        `${API_BASE}/openapi-integrations/OAI-1/tool-drafts/OATD-1/preview`,
        () => {
          previewCount += 1;
          if (previewCount === 1) {
            return HttpResponse.json(
              envelope({
                draft_id: "OATD-1",
                result: {
                  status_code: 200,
                  text: "{}",
                  json: { ok: true },
                  headers: {},
                  attempts: 1,
                },
              }),
            );
          }
          return HttpResponse.json({ detail: "upstream timed out" }, { status: 504 });
        },
      ),
    );

    const user = userEvent.setup();
    renderPage();
    await screen.findByRole("heading", { name: "OpenAPI Integrations" });
    await user.click(await screen.findByText("Ticketing"));
    await screen.findByRole("heading", { name: /Ticketing/ });
    await user.click(screen.getByRole("button", { name: "Tool Drafts" }));
    await user.click(await screen.findByText("get_ticket"));

    await user.click(screen.getByRole("button", { name: "Run preview" }));
    expect(await screen.findByText(/"ok": true/)).toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: "Run preview" }));
    expect(await screen.findByText("Error: upstream timed out")).toBeInTheDocument();

    // Malformed JSON input never reaches the API — it fails client-side.
    const previewInput = screen.getByDisplayValue("{}");
    await user.clear(previewInput);
    await user.type(previewInput, "not json");
    await user.click(screen.getByRole("button", { name: "Run preview" }));
    expect(await screen.findByText("Preview failed")).toBeInTheDocument();
    expect(previewCount).toBe(2);
  });

  it("shows an error when publishing a tool draft fails", async () => {
    server.use(
      http.get(`${API_BASE}/openapi-integrations`, () =>
        HttpResponse.json(envelope([baseIntegration()])),
      ),
      http.get(`${API_BASE}/openapi-integrations/OAI-1`, () =>
        HttpResponse.json(envelope(baseIntegration())),
      ),
      http.get(`${API_BASE}/openapi-integrations/OAI-1/versions`, () =>
        HttpResponse.json(envelope([baseVersion()])),
      ),
      http.get(`${API_BASE}/openapi-integrations/OAI-1/tool-drafts`, () =>
        HttpResponse.json(envelope([baseDraft()])),
      ),
      http.post(`${API_BASE}/openapi-integrations/OAI-1/tool-drafts/OATD-1/publish`, () =>
        HttpResponse.json({ detail: "missing required scope" }, { status: 403 }),
      ),
    );

    const user = userEvent.setup();
    renderPage();
    await screen.findByRole("heading", { name: "OpenAPI Integrations" });
    await user.click(await screen.findByText("Ticketing"));
    await screen.findByRole("heading", { name: /Ticketing/ });
    await user.click(screen.getByRole("button", { name: "Tool Drafts" }));
    await user.click(await screen.findByText("get_ticket"));

    await user.click(screen.getByRole("button", { name: "Publish" }));

    expect(await screen.findByText("missing required scope")).toBeInTheDocument();
    expect(screen.queryByTestId("openapi-publication-success")).not.toBeInTheDocument();
  });

  it("renders object, array, enum, and unknown-shaped nested schema fields", async () => {
    const draft = {
      ...baseDraft(),
      input_schema: {
        type: "object",
        properties: {
          tag: { enum: ["open", "closed"] },
          meta: { properties: { x: { type: "string" } } },
          items_field: { items: { type: "string" } },
          wild: {},
        },
      },
    };
    server.use(
      http.get(`${API_BASE}/openapi-integrations`, () =>
        HttpResponse.json(envelope([baseIntegration()])),
      ),
      http.get(`${API_BASE}/openapi-integrations/OAI-1`, () =>
        HttpResponse.json(envelope(baseIntegration())),
      ),
      http.get(`${API_BASE}/openapi-integrations/OAI-1/versions`, () =>
        HttpResponse.json(envelope([baseVersion()])),
      ),
      http.get(`${API_BASE}/openapi-integrations/OAI-1/tool-drafts`, () =>
        HttpResponse.json(envelope([draft])),
      ),
    );

    const user = userEvent.setup();
    renderPage();
    await screen.findByRole("heading", { name: "OpenAPI Integrations" });
    await user.click(await screen.findByText("Ticketing"));
    await screen.findByRole("heading", { name: /Ticketing/ });
    await user.click(screen.getByRole("button", { name: "Tool Drafts" }));
    await user.click(await screen.findByText("get_ticket"));

    expect(await screen.findByText("Input Signature")).toBeInTheDocument();
    // enum → `"open" | "closed"`, object → `{ x?: string }`, array → `string[]`.
    expect(screen.getAllByText(/"open" \| "closed"/).length).toBeGreaterThan(0);
    expect(screen.getAllByText(/\{ x\?: string \}/).length).toBeGreaterThan(0);
    expect(screen.getAllByText(/string\[\]/).length).toBeGreaterThan(0);
  });

  it("shows empty states across graph view modes and clears the node inspector", async () => {
    const snapshot = {
      ...baseGraphSnapshot(),
      edges: [
        ...baseGraphSnapshot().edges,
        {
          id: "extra-edge",
          type: "requires_auth",
          from: "operation:OAIO-1",
          to: "operation:OAIO-2",
          data: {},
        },
      ],
    };
    server.use(
      http.get(`${API_BASE}/openapi-integrations`, () =>
        HttpResponse.json(envelope([baseIntegration()])),
      ),
      http.get(`${API_BASE}/openapi-integrations/OAI-1`, () =>
        HttpResponse.json(envelope(baseIntegration())),
      ),
      http.get(`${API_BASE}/openapi-integrations/OAI-1/versions`, () =>
        HttpResponse.json(envelope([baseVersion()])),
      ),
      http.get(`${API_BASE}/openapi-integrations/OAI-1/graph`, () =>
        HttpResponse.json(envelope(snapshot)),
      ),
    );

    const user = userEvent.setup();
    renderPage();
    await screen.findByRole("heading", { name: "OpenAPI Integrations" });
    await user.click(await screen.findByText("Ticketing"));
    await screen.findByRole("heading", { name: /Ticketing/ });
    await user.click(screen.getByRole("button", { name: "Graph" }));
    await screen.findByTestId("openapi-graph-canvas");

    // Select then clear a node in the inspector.
    await user.click(screen.getByRole("button", { name: "Inspect POST /tickets" }));
    expect(await screen.findByTestId("openapi-graph-inspector")).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Clear selection" }));
    expect(screen.queryByTestId("openapi-graph-inspector")).not.toBeInTheDocument();

    // A search matching no operations empties the graph canvas.
    await user.type(screen.getByLabelText("Search operations"), "zzz-no-match");
    expect(
      await screen.findByText("No API nodes are available in this graph snapshot yet."),
    ).toBeInTheDocument();
    expect(screen.getByText(/No operations match/)).toBeInTheDocument();

    // Same empty search, but in tree view.
    await user.click(screen.getByRole("button", { name: "tree" }));
    expect(
      await screen.findByText("No operations match the current filters."),
    ).toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: "Reset" }));

    // A status filter that matches nothing empties the flow view.
    await user.click(screen.getByRole("button", { name: "flow" }));
    await user.selectOptions(screen.getByLabelText("Filter dependencies by status"), "rejected");
    expect(
      await screen.findByText(
        "No dependency steps match the current filters. Select an operation or change the filters.",
      ),
    ).toBeInTheDocument();
  });
});
