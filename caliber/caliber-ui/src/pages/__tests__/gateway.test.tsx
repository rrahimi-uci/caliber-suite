import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { http, HttpResponse } from "msw";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { afterAll, afterEach, beforeAll, describe, expect, it, vi } from "vitest";

import { Gateway } from "@/pages/Gateway";
import { server } from "@/test/server";

const API_BASE = "/ajax-api/2.0/mlflow/caliber";

function envelope<T>(data: T): { data: T } {
  return { data };
}

const REACHABLE = {
  configured: true,
  reachable: true,
  gateway_uri: "http://mlflow-gateway:5002",
  routing_through_gateway: false,
  llm_base_url: "",
  error: null,
  endpoints: [
    {
      name: "chat-openai",
      endpoint_type: "llm/v1/chat",
      provider: "openai",
      model: "gpt-4o-mini",
      endpoint_url: "/gateway/chat-openai/invocations",
      limit: null,
    },
    {
      name: "chat-anthropic",
      endpoint_type: "llm/v1/chat",
      provider: "anthropic",
      model: "claude-3-5-sonnet-20241022",
      endpoint_url: "/gateway/chat-anthropic/invocations",
      limit: null,
    },
  ],
};

function renderPage(): void {
  render(
    <MemoryRouter
      future={{ v7_startTransition: true, v7_relativeSplatPath: true }}
      initialEntries={["/gateway"]}
    >
      <Routes>
        <Route path="/gateway" element={<Gateway />} />
      </Routes>
    </MemoryRouter>,
  );
}

beforeAll(() => server.listen({ onUnhandledRequest: "error" }));
afterEach(() => server.resetHandlers());
afterAll(() => server.close());

describe("Gateway page", () => {
  it("lists the gateway endpoints when reachable", async () => {
    server.use(http.get(`${API_BASE}/gateway`, () => HttpResponse.json(envelope(REACHABLE))));
    renderPage();

    const rows = await screen.findAllByTestId("gateway-endpoint-row");
    expect(rows).toHaveLength(2);
    const status = screen.getByTestId("gateway-status");
    expect(within(status).getByText("Reachable")).toBeInTheDocument();
    expect(within(status).getByText("Direct to provider")).toBeInTheDocument();
    expect(rows[0]?.textContent).toContain("chat-openai");
    expect(rows[0]?.textContent).toContain("gpt-4o-mini");
  });

  it("shows the not-configured hint when no gateway is set", async () => {
    server.use(
      http.get(`${API_BASE}/gateway`, () =>
        HttpResponse.json(
          envelope({
            configured: false,
            reachable: false,
            gateway_uri: "",
            routing_through_gateway: false,
            llm_base_url: "",
            endpoints: [],
            error: null,
          }),
        ),
      ),
    );
    renderPage();
    expect(await screen.findByTestId("gateway-not-configured")).toBeInTheDocument();
  });

  it("surfaces an unreachable gateway without failing", async () => {
    server.use(
      http.get(`${API_BASE}/gateway`, () =>
        HttpResponse.json(
          envelope({
            configured: true,
            reachable: false,
            gateway_uri: "http://mlflow-gateway:5002",
            routing_through_gateway: false,
            llm_base_url: "",
            endpoints: [],
            error: "connection refused",
          }),
        ),
      ),
    );
    renderPage();
    await waitFor(() => {
      expect(screen.getByText(/connection refused/)).toBeInTheDocument();
    });
    expect(within(screen.getByTestId("gateway-status")).getByText("Unreachable")).toBeInTheDocument();
  });
});

// The endpoints tab mounts first and fetches /gateway; register it for tab tests.
function _endpointsHandler() {
  return http.get(`${API_BASE}/gateway`, () => HttpResponse.json(envelope(REACHABLE)));
}

const GUARDRAILS = {
  configured: true,
  reachable: true,
  error: null,
  guardrails: [
    { guardrail_id: "G1", name: "pii-before", stage: "BEFORE", action: "VALIDATION", scorer: "DetectPII", action_endpoint_name: null },
    { guardrail_id: "G2", name: "tox-after", stage: "AFTER", action: "SANITIZATION", scorer: "ToxicLanguage", action_endpoint_name: null },
  ],
  coverage: [
    {
      endpoint: "chat-openai",
      endpoint_id: "E1",
      guardrails: [{ guardrail_id: "G1", name: "pii-before", execution_order: 0, enabled: true }],
    },
  ],
};

const CATALOG = {
  configured: true,
  reachable: true,
  error: null,
  templates: [
    {
      type: "pii",
      label: "PII detection",
      summary: "Flag PII with a rule-based scorer.",
      scorer_class: "PIIDetection",
      deterministic: true,
      default_stage: "AFTER",
      default_action: "SANITIZATION",
      fields: [
        {
          name: "pii_types",
          label: "PII types",
          type: "multiselect",
          required: false,
          help: "Leave empty for all.",
          placeholder: null,
          options: ["email", "phone", "ssn", "credit_card", "ip_address"],
        },
      ],
    },
    {
      type: "guidelines",
      label: "Custom (natural-language guidelines)",
      summary: "LLM-judge guardrail from plain-English rules.",
      scorer_class: "Guidelines",
      deterministic: false,
      default_stage: "AFTER",
      default_action: "VALIDATION",
      fields: [
        {
          name: "guidelines",
          label: "Guidelines",
          type: "textarea",
          required: true,
          help: "One per line.",
          placeholder: null,
          options: [],
        },
      ],
    },
  ],
  scorers: [{ name: "existing_pii", scorer_id: "SC1", version: 3 }],
};

function _catalogHandler() {
  return http.get(`${API_BASE}/gateway/guardrails/catalog`, () =>
    HttpResponse.json(envelope(CATALOG)),
  );
}

describe("Gateway → Guardrails tab", () => {
  it("lists guardrails + coverage and attaches an available guardrail", async () => {
    let attached: Record<string, unknown> | null = null;
    server.use(
      _endpointsHandler(),
      _catalogHandler(),
      http.get(`${API_BASE}/gateway/guardrails`, () => HttpResponse.json(envelope(GUARDRAILS))),
      http.post(`${API_BASE}/gateway/endpoints/E1/guardrails`, async ({ request }) => {
        attached = (await request.json()) as Record<string, unknown>;
        return HttpResponse.json(
          envelope({ endpoint_id: "E1", guardrail_id: "G2", attached: true }),
          { status: 201 },
        );
      }),
    );
    const user = userEvent.setup();
    renderPage();
    await user.click(await screen.findByRole("button", { name: "Guardrails" }));

    const rows = await screen.findAllByTestId("guardrail-row");
    expect(rows).toHaveLength(2);
    expect(screen.getByTestId("guardrail-coverage").textContent).toContain("pii-before");

    // G2 is the only guardrail not yet on E1 → attachable.
    await user.selectOptions(screen.getByLabelText("Attach guardrail"), "G2");
    await user.click(screen.getByRole("button", { name: "Attach" }));
    await waitFor(() => expect(attached).not.toBeNull());
    expect((attached as Record<string, unknown>).guardrail_id).toBe("G2");
  });

  it("defines a new guardrail from a native scorer template", async () => {
    let created: Record<string, unknown> | null = null;
    server.use(
      _endpointsHandler(),
      _catalogHandler(),
      http.get(`${API_BASE}/gateway/guardrails`, () => HttpResponse.json(envelope(GUARDRAILS))),
      http.post(`${API_BASE}/gateway/guardrails`, async ({ request }) => {
        created = (await request.json()) as Record<string, unknown>;
        return HttpResponse.json(
          envelope({
            guardrail_id: "Gnew",
            name: "block-pii",
            stage: "AFTER",
            action: "SANITIZATION",
            scorer: "SCnew",
            action_endpoint_name: null,
          }),
          { status: 201 },
        );
      }),
    );
    const user = userEvent.setup();
    renderPage();
    await user.click(await screen.findByRole("button", { name: "Guardrails" }));
    await screen.findAllByTestId("guardrail-row");

    await user.click(screen.getByTestId("new-guardrail-toggle"));
    // pii template is selected by default; pick a PII type + name and submit.
    await user.click(await screen.findByRole("button", { name: "email" }));
    await user.type(screen.getByLabelText("Guardrail name"), "block-pii");
    await user.click(screen.getByTestId("create-guardrail-submit"));

    await waitFor(() => expect(created).not.toBeNull());
    const body = created as Record<string, unknown>;
    expect(body.name).toBe("block-pii");
    expect(body.scorer_type).toBe("pii");
    expect((body.config as Record<string, unknown>).pii_types).toEqual(["email"]);
    // pii template defaults flow through to stage/action
    expect(body.stage).toBe("AFTER");
    expect(body.action).toBe("SANITIZATION");
  });

  it("deletes a guardrail after confirmation", async () => {
    const confirmSpy = vi.spyOn(window, "confirm").mockReturnValue(true);
    let deleted = "";
    server.use(
      _endpointsHandler(),
      _catalogHandler(),
      http.get(`${API_BASE}/gateway/guardrails`, () => HttpResponse.json(envelope(GUARDRAILS))),
      http.delete(`${API_BASE}/gateway/guardrails/:gid`, ({ params }) => {
        deleted = String(params.gid);
        return HttpResponse.json(envelope({ guardrail_id: deleted, deleted: true }));
      }),
    );
    const user = userEvent.setup();
    renderPage();
    await user.click(await screen.findByRole("button", { name: "Guardrails" }));
    await screen.findAllByTestId("guardrail-row");

    await user.click(screen.getByRole("button", { name: "Delete tox-after" }));
    await waitFor(() => expect(deleted).toBe("G2"));
    confirmSpy.mockRestore();
  });

  it("degrades gracefully when the guardrail API is unavailable", async () => {
    server.use(
      _endpointsHandler(),
      _catalogHandler(),
      http.get(`${API_BASE}/gateway/guardrails`, () =>
        HttpResponse.json(
          envelope({ configured: true, reachable: false, guardrails: [], coverage: [], error: "gateway not enabled" }),
        ),
      ),
    );
    const user = userEvent.setup();
    renderPage();
    await user.click(await screen.findByRole("button", { name: "Guardrails" }));
    expect(await screen.findByTestId("gateway-guardrails-unavailable")).toBeInTheDocument();
  });
});

describe("Gateway → Pricing tab", () => {
  it("lists rates and creates a new one", async () => {
    let created: Record<string, unknown> | null = null;
    const rows = [
      {
        pricing_id: "LPRC-1",
        provider: "openai",
        model_id: "gpt-4o",
        prompt_price: 0.0025,
        completion_price: 0.01,
        cached_prompt_price: 0.00125,
        owner: "@test",
        tags: [],
        status: "active",
        created_at: "2026-06-25T00:00:00Z",
        updated_at: "2026-06-25T00:00:00Z",
      },
    ];
    server.use(
      _endpointsHandler(),
      http.get(`${API_BASE}/llm-pricing`, () => HttpResponse.json(envelope(rows))),
      http.post(`${API_BASE}/llm-pricing`, async ({ request }) => {
        created = (await request.json()) as Record<string, unknown>;
        return HttpResponse.json(envelope({ ...rows[0], pricing_id: "LPRC-2", model_id: "gpt-4o-mini" }), {
          status: 201,
        });
      }),
    );
    const user = userEvent.setup();
    renderPage();
    await user.click(await screen.findByRole("button", { name: "Pricing" }));

    expect((await screen.findAllByTestId("pricing-row"))[0]?.textContent).toContain("gpt-4o");

    await user.click(screen.getByRole("button", { name: /Add rate/ }));
    await user.type(screen.getByPlaceholderText("openai"), "openai");
    await user.type(screen.getByPlaceholderText("gpt-5.6-luna"), "gpt-4o-mini");
    await user.type(screen.getByPlaceholderText("0.0025"), "0.00015");
    await user.type(screen.getByPlaceholderText("0.01"), "0.0006");
    await user.click(screen.getByRole("button", { name: "Create" }));
    await waitFor(() => expect(created).not.toBeNull());
    expect((created as Record<string, unknown>).model_id).toBe("gpt-4o-mini");
  });
});

describe("Gateway → Usage tab", () => {
  it("renders totals + the by-model table from trace-derived usage", async () => {
    server.use(
      _endpointsHandler(),
      http.get(`${API_BASE}/gateway/usage`, () =>
        HttpResponse.json(
          envelope({
            buckets: [
              { ts: 1_700_000_000_000, count: 3, error_count: 0, error_rate: 0, p50_ms: 100, p95_ms: 200, tokens: 50, cost_usd: 0.4 },
            ],
            bucket_ms: 60_000,
            totals: { count: 3, error_rate: 0, p50_ms: 100, p95_ms: 200, tokens: 50, cost_usd: 0.4 },
            by_model: [{ model: "gpt-4o", calls: 3, tokens: 50, cost_usd: 0.4 }],
          }),
        ),
      ),
    );
    const user = userEvent.setup();
    renderPage();
    await user.click(await screen.findByRole("button", { name: "Usage" }));

    const byModel = await screen.findByTestId("gateway-usage-by-model");
    expect(byModel.textContent).toContain("gpt-4o");
    // A unique chart title confirms the charts rendered (ResizeObserver mocked).
    expect(screen.getByText("Request volume & errors")).toBeInTheDocument();
  });
});
