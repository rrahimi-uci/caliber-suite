import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { http, HttpResponse } from "msw";
import { afterAll, afterEach, beforeAll, describe, expect, it } from "vitest";
import { MemoryRouter, Route, Routes } from "react-router-dom";

import { McpServers } from "@/pages/McpServers";
import { server } from "@/test/server";

const API_BASE = "/ajax-api/2.0/mlflow/caliber";
const NOW = "2026-06-07T18:00:00Z";

function envelope<T>(data: T): { data: T } {
  return { data };
}

function baseServer() {
  return {
    server_id: "MCP-1",
    name: "Docs",
    description: "Docs MCP server",
    transport: "stdio" as const,
    uri: "",
    command: "npx",
    args: ["docs-server"],
    env: {},
    headers: {},
    auth_type: "none" as const,
    auth_config: {},
    tool_policies: {},
    tool_test_cases: {},
    tool_calibrations: {},
    icon: "book",
    status: "active" as const,
    last_connected_at: null,
    connection_error: null,
    owner: "@qa",
    discovered_tools: [
      {
        name: "search_docs",
        description: "Search docs",
        input_schema: {
          type: "object",
          properties: {
            query: { type: "string", description: "query" },
            limit: { type: "integer", description: "limit" },
            threshold: { type: "number", description: "threshold" },
            exact: { type: "boolean", description: "exact" },
          },
          required: ["query"],
        },
        output_schema: {
          type: "object",
          properties: {
            results: { type: "array" },
          },
        },
      },
    ],
    created_at: NOW,
    updated_at: NOW,
  };
}

function renderPage(): void {
  render(
    <MemoryRouter future={{ v7_startTransition: true, v7_relativeSplatPath: true }} initialEntries={["/mcp-servers"]}>
      <Routes>
        <Route path="/mcp-servers" element={<McpServers />} />
      </Routes>
    </MemoryRouter>,
  );
}

beforeAll(() => server.listen({ onUnhandledRequest: "error" }));
afterEach(() => server.resetHandlers());
afterAll(() => server.close());

describe("McpServers", () => {
  it("narrows the server table with search, status, and transport filters", async () => {
    const docs = baseServer(); // active / stdio / "Docs"
    const remote = {
      ...baseServer(),
      server_id: "MCP-2",
      name: "Remote Graph",
      description: "Graph query server",
      transport: "streamable-http" as const,
      uri: "http://localhost:7777/mcp",
      command: "",
      args: [],
      status: "error" as const,
      discovered_tools: [],
    };
    server.use(
      http.get(`${API_BASE}/mcp-servers`, () =>
        HttpResponse.json(envelope([docs, remote])),
      ),
    );

    const user = userEvent.setup();
    renderPage();
    // Both rows visible by default (no filter active).
    expect(await screen.findByTestId("mcp-row-MCP-1")).toBeInTheDocument();
    expect(screen.getByTestId("mcp-row-MCP-2")).toBeInTheDocument();

    // Text search narrows to the matching server.
    await user.type(screen.getByRole("searchbox", { name: "Search MCP servers" }), "graph");
    expect(screen.queryByTestId("mcp-row-MCP-1")).not.toBeInTheDocument();
    expect(screen.getByTestId("mcp-row-MCP-2")).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Clear search" }));

    // Status filter narrows to active-only.
    await user.selectOptions(screen.getByRole("combobox", { name: "Filter by status" }), "active");
    expect(screen.getByTestId("mcp-row-MCP-1")).toBeInTheDocument();
    expect(screen.queryByTestId("mcp-row-MCP-2")).not.toBeInTheDocument();
    await user.selectOptions(screen.getByRole("combobox", { name: "Filter by status" }), "");

    // Transport filter narrows to streamable-http.
    await user.selectOptions(
      screen.getByRole("combobox", { name: "Filter by transport" }),
      "streamable-http",
    );
    expect(screen.queryByTestId("mcp-row-MCP-1")).not.toBeInTheDocument();
    expect(screen.getByTestId("mcp-row-MCP-2")).toBeInTheDocument();

    // Combining transport + a non-matching status empties the table.
    await user.selectOptions(screen.getByRole("combobox", { name: "Filter by status" }), "active");
    expect(screen.queryByTestId("mcp-row-MCP-2")).not.toBeInTheDocument();
    expect(screen.getByText("No MCP servers match the current filters.")).toBeInTheDocument();
  });

  it("opens quick-connect templates and registers a prefilled server", async () => {
    let listCalls = 0;
    let createBody: Record<string, unknown> | null = null;
    server.use(
      http.get(`${API_BASE}/mcp-servers`, () => {
        listCalls += 1;
        if (listCalls === 1) {
          return HttpResponse.json(envelope([]));
        }
        return HttpResponse.json(envelope([baseServer()]));
      }),
      http.post(`${API_BASE}/mcp-servers`, async ({ request }) => {
        createBody = (await request.json()) as Record<string, unknown>;
        return HttpResponse.json(envelope(baseServer()), { status: 201 });
      }),
    );

    const user = userEvent.setup();
    renderPage();
    await screen.findByRole("heading", { name: "MCP Servers" });
    expect(screen.getByTestId("catalog-ollama")).toBeInTheDocument();
    await user.click(screen.getByTestId("catalog-github"));

    const dialog = await screen.findByTestId("add-server-dialog");
    expect(dialog).toBeInTheDocument();
    expect(screen.getByTestId("server-name-input")).toHaveValue("GitHub");
    expect(screen.getByTestId("server-command-input")).toHaveValue("npx");
    expect(screen.getByRole("combobox", { name: "Authentication type" })).toHaveValue("token");
    await user.click(screen.getByTestId("server-submit-btn"));

    expect(await screen.findByTestId("mcp-row-MCP-1")).toBeInTheDocument();
    expect(createBody).toMatchObject({
      name: "GitHub",
      transport: "stdio",
      command: "npx",
      auth_type: "token",
    });
    // Catalog templates seed the server with their known tools on connect, so
    // tools are visible without needing a live test-connection first.
    const tools = (createBody as { discovered_tools?: Array<{ name: string }> }).discovered_tools;
    expect(tools?.length).toBeGreaterThan(0);
    expect(tools?.map((t) => t.name)).toContain("create_issue");
  });

  it("registers the Playwright quick-connect template with browser tools", async () => {
    let createBody: Record<string, unknown> | null = null;
    server.use(
      http.get(`${API_BASE}/mcp-servers`, () => HttpResponse.json(envelope([]))),
      http.post(`${API_BASE}/mcp-servers`, async ({ request }) => {
        createBody = (await request.json()) as Record<string, unknown>;
        return HttpResponse.json(envelope(baseServer()), { status: 201 });
      }),
    );

    const user = userEvent.setup();
    renderPage();
    await screen.findByRole("heading", { name: "MCP Servers" });

    await user.click(screen.getByTestId("catalog-playwright"));
    const dialog = await screen.findByTestId("add-server-dialog");
    expect(dialog).toBeInTheDocument();
    expect(screen.getByTestId("server-name-input")).toHaveValue("Playwright");
    expect(screen.getByTestId("server-command-input")).toHaveValue("npx");
    expect(screen.getByRole("combobox", { name: "Authentication type" })).toHaveValue("none");

    await user.click(screen.getByTestId("server-submit-btn"));

    await waitFor(() =>
      expect(createBody).toMatchObject({
        name: "Playwright",
        transport: "stdio",
        command: "npx",
        args: ["@playwright/mcp@latest"],
        auth_type: "none",
        icon: "playwright",
      }),
    );

    const tools = (createBody as { discovered_tools?: Array<{ name: string }> }).discovered_tools;
    expect(tools?.map((t) => t.name)).toContain("browser_navigate");
    expect(tools?.map((t) => t.name)).toContain("browser_take_screenshot");
    expect(tools?.map((t) => t.name)).toContain("browser_tabs");
  });

  it("shows registration errors when server creation fails", async () => {
    server.use(
      http.get(`${API_BASE}/mcp-servers`, () => HttpResponse.json(envelope([]))),
      http.post(`${API_BASE}/mcp-servers`, () =>
        HttpResponse.json({ detail: "invalid command" }, { status: 500 }),
      ),
    );

    const user = userEvent.setup();
    renderPage();
    await screen.findByRole("heading", { name: "MCP Servers" });
    await user.click(screen.getByTestId("add-server-btn"));
    await user.type(screen.getByTestId("server-name-input"), "Broken Server");
    await user.type(screen.getByTestId("server-command-input"), "npx");
    await user.click(screen.getByTestId("server-submit-btn"));

    expect(await screen.findByText("invalid command")).toBeInTheDocument();
  });

  it("handles test failures and coerces invocation argument types in playground", async () => {
    let testCalls = 0;
    let invokeBody: Record<string, unknown> | null = null;
    const discoveredTools = baseServer().discovered_tools;
    server.use(
      http.get(`${API_BASE}/mcp-servers`, () =>
        HttpResponse.json(envelope([baseServer()])),
      ),
      http.get(`${API_BASE}/mcp-servers/MCP-1/tools`, () =>
        HttpResponse.json(
          envelope({
            server_id: "MCP-1",
            tools: discoveredTools.map((tool) => ({
              ...tool,
              policy: {
                allowed: true,
                side_effect_level: "read",
                requires_approval: false,
                rate_limit_per_minute: null,
              },
            })),
          }),
        ),
      ),
      http.post(`${API_BASE}/mcp-servers/MCP-1/discover-tools`, () =>
        HttpResponse.json(
          envelope({
            server_id: "MCP-1",
            tools: discoveredTools,
            tool_count: discoveredTools.length,
            discovered_at: NOW,
          }),
        ),
      ),
      http.post(`${API_BASE}/mcp-servers/MCP-1/test-connection`, () => {
        testCalls += 1;
        if (testCalls === 1) {
          return HttpResponse.json({ detail: "network" }, { status: 500 });
        }
        return HttpResponse.json(
          envelope({
            server_id: "MCP-1",
            success: true,
            error: null,
            tools: discoveredTools,
          }),
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
            result: { results: ["doc-1"] },
            duration_ms: 7,
          }),
        );
      }),
    );

    const user = userEvent.setup();
    renderPage();
    await screen.findByRole("heading", { name: "MCP Servers" });

    await user.click(screen.getByTestId("test-btn-MCP-1"));
    expect(await screen.findByText("network")).toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: "Playground" }));
    await user.click(await screen.findByRole("button", { name: "Test Connection" }));
    expect(await screen.findByText("Connection Successful")).toBeInTheDocument();

    const toolLabel = await screen.findByText("search_docs");
    const toolButton = toolLabel.closest("button");
    expect(toolButton).toBeTruthy();
    await user.click(toolButton as HTMLButtonElement);
    await user.type(screen.getByPlaceholderText("query"), "refund");
    await user.type(screen.getByPlaceholderText("limit"), "10");
    await user.type(screen.getByPlaceholderText("threshold"), "0.75");
    await user.type(screen.getByPlaceholderText("exact"), "true");
    await user.click(screen.getByRole("button", { name: "Invoke Tool" }));

    expect(await screen.findByText("Success")).toBeInTheDocument();
    expect(invokeBody).toMatchObject({
      tool_name: "search_docs",
      arguments: {
        query: "refund",
        limit: 10,
        threshold: 0.75,
        exact: true,
      },
    });

    expect(await screen.findByRole("button", { name: "Clear" })).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Clear" }));
    await waitFor(() =>
      expect(screen.queryByRole("button", { name: "Clear" })).not.toBeInTheDocument(),
    );
  });

  it("shows an empty playground state when no MCP servers are registered", async () => {
    server.use(
      http.get(`${API_BASE}/mcp-servers`, () => HttpResponse.json(envelope([]))),
    );

    const user = userEvent.setup();
    renderPage();
    await screen.findByRole("heading", { name: "MCP Servers" });
    await user.click(screen.getByRole("button", { name: "Playground" }));

    expect(await screen.findByText("No MCP servers registered yet.")).toBeInTheDocument();
    expect(screen.getByText("Switch to the Servers tab to add one.")).toBeInTheDocument();
  });

  it("updates MCP tool policy from the playground", async () => {
    let patchBody: Record<string, unknown> | null = null;
    const discoveredTools = baseServer().discovered_tools;
    server.use(
      http.get(`${API_BASE}/mcp-servers`, () =>
        HttpResponse.json(envelope([baseServer()])),
      ),
      http.get(`${API_BASE}/mcp-servers/MCP-1/tools`, () =>
        HttpResponse.json(
          envelope({
            server_id: "MCP-1",
            tools: discoveredTools.map((tool) => ({
              ...tool,
              policy: {
                allowed: true,
                side_effect_level: "read",
                requires_approval: false,
                rate_limit_per_minute: null,
              },
            })),
          }),
        ),
      ),
      http.patch(`${API_BASE}/mcp-servers/MCP-1/tools/search_docs/policy`, async ({ request }) => {
        patchBody = (await request.json()) as Record<string, unknown>;
        return HttpResponse.json(
          envelope({
            server_id: "MCP-1",
            tool_name: "search_docs",
            policy: {
              allowed: true,
              side_effect_level: "write",
              requires_approval: true,
              rate_limit_per_minute: 25,
            },
          }),
        );
      }),
    );

    const user = userEvent.setup();
    renderPage();
    await screen.findByRole("heading", { name: "MCP Servers" });
    await user.click(screen.getByRole("button", { name: "Playground" }));

    const toolLabel = await screen.findByText("search_docs");
    const toolButton = toolLabel.closest("button");
    expect(toolButton).toBeTruthy();
    await user.click(toolButton as HTMLButtonElement);

    await user.click(screen.getByRole("checkbox", { name: "Requires approval" }));
    await user.selectOptions(screen.getByRole("combobox", { name: /side effect level/i }), "write");
    await user.type(screen.getByRole("spinbutton", { name: /rate limit/i }), "25");
    await user.click(screen.getByRole("button", { name: "Save Policy" }));

    expect(await screen.findByText("Policy saved.")).toBeInTheDocument();
    expect(patchBody).toMatchObject({
      allowed: true,
      side_effect_level: "write",
      requires_approval: true,
      rate_limit_per_minute: 25,
    });
  });

  it("registers remote servers and opens detail dialogs for connected catalog servers", async () => {
    const connectedGithub = {
      ...baseServer(),
      server_id: "MCP-GH",
      name: "GitHub",
      description: "GitHub issue and PR tools",
      transport: "sse" as const,
      uri: "https://github.example/mcp/sse",
      command: "",
      args: [],
      env: { GITHUB_TOKEN: "${GITHUB_TOKEN}" },
      auth_type: "token" as const,
      auth_config: { token_env_var: "GITHUB_TOKEN" },
      status: "error" as const,
      connection_error: "Token expired",
      discovered_tools: [],
    };
    const remoteGraph = {
      ...baseServer(),
      server_id: "MCP-GRAPH",
      name: "Remote Graph",
      description: "Graph query server",
      transport: "streamable-http" as const,
      uri: "http://localhost:7777/mcp",
      command: "",
      args: [],
      auth_type: "oauth" as const,
      icon: "graph",
    };
    let listCalls = 0;
    let createBody: Record<string, unknown> | null = null;
    server.use(
      http.get(`${API_BASE}/mcp-servers`, () => {
        listCalls += 1;
        return HttpResponse.json(
          envelope(listCalls === 1 ? [connectedGithub] : [connectedGithub, remoteGraph]),
        );
      }),
      http.post(`${API_BASE}/mcp-servers`, async ({ request }) => {
        createBody = (await request.json()) as Record<string, unknown>;
        return HttpResponse.json(envelope(remoteGraph), { status: 201 });
      }),
    );

    const user = userEvent.setup();
    renderPage();

    const githubCard = await screen.findByTestId("catalog-github");
    expect(screen.getByText("Connected")).toBeInTheDocument();
    await user.click(githubCard);
    const addDialog = await screen.findByTestId("add-server-dialog");
    expect(within(addDialog).getByTestId("server-name-input")).toHaveValue("GitHub");
    await user.click(within(addDialog).getByRole("button", { name: "Cancel" }));
    await waitFor(() => expect(screen.queryByTestId("add-server-dialog")).not.toBeInTheDocument());

    fireEvent.doubleClick(await screen.findByTestId("mcp-row-MCP-GH"));
    const dialog = await screen.findByTestId("mcp-detail-dialog");
    expect(within(dialog).getByText("Token expired")).toBeInTheDocument();
    expect(within(dialog).getByText("GITHUB_TOKEN")).toBeInTheDocument();
    expect(within(dialog).getByText(/No tools discovered yet/)).toBeInTheDocument();
    await user.click(within(dialog).getByLabelText("Close"));
    await waitFor(() => expect(screen.queryByTestId("mcp-detail-dialog")).not.toBeInTheDocument());

    await user.click(screen.getByTestId("add-server-btn"));
    await user.type(screen.getByTestId("server-name-input"), "Remote Graph");
    await user.selectOptions(screen.getByTestId("server-transport-select"), "streamable-http");
    await user.type(screen.getByTestId("server-uri-input"), "http://localhost:7777/mcp");
    await user.selectOptions(screen.getByRole("combobox", { name: "Authentication type" }), "oauth");
    await user.selectOptions(screen.getByRole("combobox", { name: "Server icon" }), "graph");
    await user.click(screen.getByTestId("server-submit-btn"));

    await waitFor(() =>
      expect(createBody).toMatchObject({
        name: "Remote Graph",
        transport: "streamable-http",
        uri: "http://localhost:7777/mcp",
        command: "",
        args: [],
        auth_type: "oauth",
        auth_config: {},
      }),
    );
    expect(await screen.findByTestId("mcp-row-MCP-GRAPH")).toBeInTheDocument();
  });

  it("falls back to discovered tools when policy loading and discovery fail", async () => {
    const manyTools = [
      ...baseServer().discovered_tools,
      { name: "alpha_tool", description: "Alpha", input_schema: { type: "object", properties: {} }, output_schema: {} },
      { name: "beta_tool", description: "Beta", input_schema: { type: "object", properties: {} }, output_schema: {} },
      { name: "gamma_tool", description: "Gamma", input_schema: { type: "object", properties: {} }, output_schema: {} },
    ];
    server.use(
      http.get(`${API_BASE}/mcp-servers`, () =>
        HttpResponse.json(envelope([{ ...baseServer(), discovered_tools: manyTools }])),
      ),
      http.get(`${API_BASE}/mcp-servers/MCP-1/tools`, () =>
        HttpResponse.json({ detail: "policies unavailable" }, { status: 503 }),
      ),
      http.post(`${API_BASE}/mcp-servers/MCP-1/discover-tools`, () =>
        HttpResponse.json({ detail: "discovery failed" }, { status: 500 }),
      ),
      http.post(`${API_BASE}/mcp-servers/MCP-1/test-connection`, () =>
        HttpResponse.json(
          envelope({
            server_id: "MCP-1",
            success: true,
            error: null,
            tools: manyTools,
          }),
        ),
      ),
      http.post(`${API_BASE}/mcp-servers/MCP-1/invoke-tool`, () =>
        HttpResponse.json({ detail: "tool failed" }, { status: 500 }),
      ),
    );

    const user = userEvent.setup();
    renderPage();
    await screen.findByRole("heading", { name: "MCP Servers" });
    await user.click(screen.getByRole("button", { name: "Playground" }));

    expect(await screen.findByText("Could not load policies. Showing discovered tools only.")).toBeInTheDocument();
    await user.type(screen.getByPlaceholderText("Filter…"), "missing");
    expect(await screen.findByText(/No tools match/)).toBeInTheDocument();
    await user.clear(screen.getByPlaceholderText("Filter…"));

    await user.click(screen.getByRole("button", { name: "Test Connection" }));
    expect(await screen.findByText("Connection Successful")).toBeInTheDocument();

    const toolButton = (await screen.findByText("alpha_tool")).closest("button");
    expect(toolButton).toBeTruthy();
    await user.click(toolButton as HTMLButtonElement);
    expect(await screen.findByText("No parameters required")).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Invoke Tool" }));

    expect(await screen.findByText("Failed")).toBeInTheDocument();
    expect(screen.getByText("tool failed")).toBeInTheDocument();
  });

  it("cleans structured MCP invocation errors before showing them in the playground", async () => {
    const discoveredTools = baseServer().discovered_tools;
    server.use(
      http.get(`${API_BASE}/mcp-servers`, () =>
        HttpResponse.json(envelope([baseServer()])),
      ),
      http.get(`${API_BASE}/mcp-servers/MCP-1/tools`, () =>
        HttpResponse.json(
          envelope({
            server_id: "MCP-1",
            tools: discoveredTools.map((tool) => ({
              ...tool,
              policy: {
                allowed: true,
                side_effect_level: "read",
                requires_approval: false,
                rate_limit_per_minute: null,
              },
            })),
          }),
        ),
      ),
      http.post(`${API_BASE}/mcp-servers/MCP-1/invoke-tool`, () =>
        HttpResponse.json(
          envelope({
            server_id: "MCP-1",
            tool_name: "search_docs",
            success: false,
            error:
              'MCP tools/call failed: Invalid input: [{"code":"invalid_type","expected":"string","received":"undefined","path":["query"],"message":"Required"}]',
            result: null,
            duration_ms: 7,
          }),
        ),
      ),
    );

    const user = userEvent.setup();
    renderPage();
    await screen.findByRole("heading", { name: "MCP Servers" });
    await user.click(screen.getByRole("button", { name: "Playground" }));

    const toolButton = (await screen.findByText("search_docs")).closest("button");
    expect(toolButton).toBeTruthy();
    await user.click(toolButton as HTMLButtonElement);
    await user.click(screen.getByRole("button", { name: "Invoke Tool" }));

    expect(await screen.findByText("Failed")).toBeInTheDocument();
    expect(screen.getByText("Invalid input: query is required")).toBeInTheDocument();
    expect(
      screen.queryByText(/MCP tools\/call failed: Invalid input:/),
    ).not.toBeInTheDocument();
  });

  it("cleans low-level MCP execution errors before showing them in the playground", async () => {
    const discoveredTools = baseServer().discovered_tools;
    server.use(
      http.get(`${API_BASE}/mcp-servers`, () =>
        HttpResponse.json(envelope([baseServer()])),
      ),
      http.get(`${API_BASE}/mcp-servers/MCP-1/tools`, () =>
        HttpResponse.json(
          envelope({
            server_id: "MCP-1",
            tools: discoveredTools.map((tool) => ({
              ...tool,
              policy: {
                allowed: true,
                side_effect_level: "read",
                requires_approval: false,
                rate_limit_per_minute: null,
              },
            })),
          }),
        ),
      ),
      http.post(`${API_BASE}/mcp-servers/MCP-1/invoke-tool`, () =>
        HttpResponse.json(
          envelope({
            server_id: "MCP-1",
            tool_name: "search_docs",
            success: false,
            error: "[Errno 2] No such file or directory",
            result: null,
            duration_ms: 7,
          }),
        ),
      ),
    );

    const user = userEvent.setup();
    renderPage();
    await screen.findByRole("heading", { name: "MCP Servers" });
    await user.click(screen.getByRole("button", { name: "Playground" }));

    const toolButton = (await screen.findByText("search_docs")).closest("button");
    expect(toolButton).toBeTruthy();
    await user.click(toolButton as HTMLButtonElement);
    await user.click(screen.getByRole("button", { name: "Invoke Tool" }));

    expect(await screen.findByText("Failed")).toBeInTheDocument();
    expect(
      screen.getByText("Request failed — check the server is installed or available"),
    ).toBeInTheDocument();
    expect(
      screen.queryByText("[Errno 2] No such file or directory"),
    ).not.toBeInTheDocument();
  });

  it("surfaces MCP tool policy save errors", async () => {
    const discoveredTools = baseServer().discovered_tools;
    server.use(
      http.get(`${API_BASE}/mcp-servers`, () =>
        HttpResponse.json(envelope([baseServer()])),
      ),
      http.get(`${API_BASE}/mcp-servers/MCP-1/tools`, () =>
        HttpResponse.json(
          envelope({
            server_id: "MCP-1",
            tools: discoveredTools.map((tool) => ({
              ...tool,
              policy: {
                allowed: true,
                side_effect_level: "read",
                requires_approval: false,
                rate_limit_per_minute: null,
              },
            })),
          }),
        ),
      ),
      http.patch(`${API_BASE}/mcp-servers/MCP-1/tools/search_docs/policy`, () =>
        HttpResponse.json({ detail: "policy denied" }, { status: 500 }),
      ),
    );

    const user = userEvent.setup();
    renderPage();
    await screen.findByRole("heading", { name: "MCP Servers" });
    await user.click(screen.getByRole("button", { name: "Playground" }));

    const toolButton = (await screen.findByText("search_docs")).closest("button");
    expect(toolButton).toBeTruthy();
    await user.click(toolButton as HTMLButtonElement);
    await user.click(screen.getByRole("checkbox", { name: "Allow tool" }));
    await user.click(screen.getByRole("button", { name: "Save Policy" }));

    expect(await screen.findByText("policy denied")).toBeInTheDocument();
  });

  it("lets an admin edit a server and PATCHes the changes (name immutable)", async () => {
    let patchBody: Record<string, unknown> | null = null;
    server.use(
      http.get(`${API_BASE}/mcp-servers`, () => HttpResponse.json(envelope([baseServer()]))),
      http.patch(`${API_BASE}/mcp-servers/MCP-1`, async ({ request }) => {
        patchBody = (await request.json()) as Record<string, unknown>;
        return HttpResponse.json(envelope({ ...baseServer(), description: "Updated docs" }));
      }),
    );

    const user = userEvent.setup();
    renderPage();
    await screen.findByTestId("mcp-row-MCP-1");

    await user.click(await screen.findByTestId("edit-btn-MCP-1"));
    const dialog = await screen.findByTestId("edit-server-dialog");
    // Prefilled from the existing server; the name is immutable in edit mode.
    const nameInput = within(dialog).getByTestId("server-name-input");
    expect(nameInput).toHaveValue("Docs");
    expect(nameInput).toBeDisabled();
    expect(within(dialog).getByTestId("server-command-input")).toHaveValue("npx");

    const description = within(dialog).getByPlaceholderText("What does this server provide?");
    await user.clear(description);
    await user.type(description, "Updated docs");
    await user.click(within(dialog).getByTestId("server-submit-btn"));

    await waitFor(() => expect(patchBody).toMatchObject({ description: "Updated docs", command: "npx" }));
    // ``name`` is immutable on the backend, so it is intentionally not sent.
    expect(patchBody && "name" in patchBody).toBe(false);
    await waitFor(() => expect(screen.queryByTestId("edit-server-dialog")).not.toBeInTheDocument());
  });

  it("lets an admin delete a server after an inline confirmation", async () => {
    let deleteCalled = false;
    let listCalls = 0;
    server.use(
      http.get(`${API_BASE}/mcp-servers`, () => {
        listCalls += 1;
        return HttpResponse.json(envelope(listCalls === 1 ? [baseServer()] : []));
      }),
      http.delete(`${API_BASE}/mcp-servers/MCP-1`, () => {
        deleteCalled = true;
        return HttpResponse.json(envelope({ deleted: "MCP-1" }));
      }),
    );

    const user = userEvent.setup();
    renderPage();
    await screen.findByTestId("mcp-row-MCP-1");

    // First click reveals an inline confirm — it does not delete immediately.
    await user.click(await screen.findByTestId("delete-btn-MCP-1"));
    expect(deleteCalled).toBe(false);
    await user.click(await screen.findByTestId("confirm-delete-btn-MCP-1"));

    await waitFor(() => expect(deleteCalled).toBe(true));
    await waitFor(() => expect(screen.queryByTestId("mcp-row-MCP-1")).not.toBeInTheDocument());
  });

  it("hides edit and delete controls from non-admins", async () => {
    server.use(
      http.get(`${API_BASE}/mcp-servers`, () => HttpResponse.json(envelope([baseServer()]))),
      http.get(`${API_BASE}/me`, () =>
        HttpResponse.json(
          envelope({ user_id: "@viewer", scopes: ["caliber.viewer"], is_admin: false }),
        ),
      ),
    );

    renderPage();
    await screen.findByTestId("mcp-row-MCP-1");
    // The read-only Test action stays; the mutating controls are gone.
    expect(screen.getByTestId("test-btn-MCP-1")).toBeInTheDocument();
    await waitFor(() =>
      expect(screen.queryByTestId("edit-btn-MCP-1")).not.toBeInTheDocument(),
    );
    expect(screen.queryByTestId("delete-btn-MCP-1")).not.toBeInTheDocument();
  });
});
