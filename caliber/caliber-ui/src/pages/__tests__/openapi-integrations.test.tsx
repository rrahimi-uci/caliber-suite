import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { http, HttpResponse } from "msw";
import { afterAll, afterEach, beforeAll, describe, expect, it } from "vitest";
import { MemoryRouter, Route, Routes } from "react-router-dom";

import { OpenApiIntegrations } from "@/pages/OpenApiIntegrations";
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

function baseDraft() {
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
});
