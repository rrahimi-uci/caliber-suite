import { http, HttpResponse } from "msw";
import {
  afterAll,
  afterEach,
  beforeAll,
  describe,
  expect,
  it,
  vi,
} from "vitest";

import { caliberApi } from "@/api/caliberApi";
import type {
  GatewayGuardrailCatalog,
  GatewayGuardrailsStatus,
} from "@/api/types";
import { GatewayGuardrailsTab } from "@/components/gateway/GatewayGuardrailsTab";
import { render, screen, userEvent, waitFor, within } from "@/test/utils";
import { server } from "@/test/server";

vi.mock("@/lib/toast", () => ({
  showToast: {
    success: vi.fn(),
    error: vi.fn(),
    info: vi.fn(),
    warning: vi.fn(),
  },
}));

const API_BASE = "/ajax-api/2.0/mlflow/caliber";

function envelope<T>(data: T): { data: T } {
  return { data };
}

const EMPTY_CATALOG: GatewayGuardrailCatalog = {
  configured: true,
  reachable: true,
  templates: [],
  scorers: [],
  error: null,
};

const FULL_CATALOG: GatewayGuardrailCatalog = {
  configured: true,
  reachable: true,
  templates: [
    {
      type: "pii",
      label: "PII detector",
      summary: "Detects personally identifiable information.",
      scorer_class: "PII",
      deterministic: true,
      default_stage: "BEFORE",
      default_action: "VALIDATION",
      fields: [
        {
          name: "threshold",
          label: "Threshold",
          type: "text",
          required: true,
          help: "0-1 confidence",
          placeholder: "0.5",
          options: [],
        },
        {
          name: "notes",
          label: "Notes",
          type: "textarea",
          required: false,
          help: null,
          placeholder: "context",
          options: [],
        },
        {
          name: "severity",
          label: "Severity",
          type: "select",
          required: false,
          help: null,
          placeholder: null,
          options: ["low", "high"],
        },
        {
          name: "block",
          label: "Block on match",
          type: "boolean",
          required: false,
          help: "Reject the request outright",
          placeholder: null,
          options: [],
        },
        {
          name: "categories",
          label: "Categories",
          type: "multiselect",
          required: false,
          help: "Pick one or more",
          placeholder: null,
          options: ["email", "ssn", "phone"],
        },
      ],
    },
    {
      type: "toxicity",
      label: "Toxicity judge",
      summary: "LLM-judged toxicity scoring.",
      scorer_class: "Toxicity",
      deterministic: false,
      default_stage: "AFTER",
      default_action: "SANITIZATION",
      fields: [],
    },
  ],
  scorers: [{ name: "custom-scorer", scorer_id: "SCORER-1", version: 2 }],
  error: null,
};

function statusFixture(
  overrides: Partial<GatewayGuardrailsStatus> = {},
): GatewayGuardrailsStatus {
  return {
    configured: true,
    reachable: true,
    guardrails: [],
    coverage: [],
    error: null,
    ...overrides,
  };
}

function stub(
  status: GatewayGuardrailsStatus,
  catalog: GatewayGuardrailCatalog = EMPTY_CATALOG,
): void {
  server.use(
    http.get(`${API_BASE}/gateway/guardrails`, () =>
      HttpResponse.json(envelope(status)),
    ),
    http.get(`${API_BASE}/gateway/guardrails/catalog`, () =>
      HttpResponse.json(envelope(catalog)),
    ),
  );
}

beforeAll(() => server.listen({ onUnhandledRequest: "error" }));
afterEach(() => {
  server.resetHandlers();
  vi.restoreAllMocks();
});
afterAll(() => server.close());

describe("GatewayGuardrailsTab", () => {
  it("shows the loading placeholder before data resolves", async () => {
    const deferred: { resolve: () => void } = { resolve: () => undefined };
    const gate = new Promise<void>((resolve) => {
      deferred.resolve = resolve;
    });
    server.use(
      http.get(`${API_BASE}/gateway/guardrails`, async () => {
        await gate;
        return HttpResponse.json(envelope(statusFixture()));
      }),
      http.get(`${API_BASE}/gateway/guardrails/catalog`, () =>
        HttpResponse.json(envelope(EMPTY_CATALOG)),
      ),
    );

    render(<GatewayGuardrailsTab />);
    expect(screen.getByText("Loading guardrails…")).toBeInTheDocument();

    deferred.resolve();
    await waitFor(() =>
      expect(screen.queryByText("Loading guardrails…")).not.toBeInTheDocument(),
    );
  });

  it("surfaces a load error instead of the table", async () => {
    server.use(
      http.get(`${API_BASE}/gateway/guardrails`, () =>
        HttpResponse.json({ detail: "gateway store unavailable" }, { status: 500 }),
      ),
      http.get(`${API_BASE}/gateway/guardrails/catalog`, () =>
        HttpResponse.json(envelope(EMPTY_CATALOG)),
      ),
    );

    render(<GatewayGuardrailsTab />);

    expect(await screen.findByText("Failed to load guardrails")).toBeInTheDocument();
    expect(screen.getByText(/gateway store unavailable/i)).toBeInTheDocument();
  });

  it("shows the unavailable state with the server-provided reason", async () => {
    stub(statusFixture({ configured: true, reachable: false, error: "gateway offline" }));

    render(<GatewayGuardrailsTab />);

    expect(await screen.findByTestId("gateway-guardrails-unavailable")).toHaveTextContent(
      "gateway offline",
    );
  });

  it("falls back to a default explanation when the API gives no reason", async () => {
    stub(statusFixture({ configured: false, reachable: true, error: null }));

    render(<GatewayGuardrailsTab />);

    expect(await screen.findByTestId("gateway-guardrails-unavailable")).toHaveTextContent(
      /MLflow ≥3\.13/,
    );
  });

  it("shows empty states for guardrails and endpoint coverage", async () => {
    stub(statusFixture({ guardrails: [], coverage: [] }));

    render(<GatewayGuardrailsTab />);

    expect(
      await screen.findByText("No guardrails configured on the gateway yet."),
    ).toBeInTheDocument();
    expect(screen.getByText("No gateway endpoints to protect.")).toBeInTheDocument();
  });

  it("renders guardrail rows with dashes for a missing action/scorer", async () => {
    stub(
      statusFixture({
        guardrails: [
          {
            guardrail_id: "GR-1",
            name: "block-pii",
            stage: "BEFORE",
            action: "VALIDATION",
            scorer: "pii-scorer",
            action_endpoint_name: null,
          },
          {
            guardrail_id: "GR-2",
            name: "bare-guardrail",
            stage: "",
            action: "",
            scorer: null,
            action_endpoint_name: null,
          },
        ],
      }),
    );

    render(<GatewayGuardrailsTab />);

    const rows = await screen.findAllByTestId("guardrail-row");
    expect(rows).toHaveLength(2);
    expect(within(rows[0]!).getByText("BEFORE")).toBeInTheDocument();
    expect(within(rows[0]!).getByText("pii-scorer")).toBeInTheDocument();
    // Row 2 has no stage/action/scorer — every optional field falls back to "—".
    const dashes = within(rows[1]!).getAllByText("—");
    expect(dashes.length).toBe(3); // stage badge, action, scorer
  });

  it("deletes a guardrail after confirmation and leaves it in place when cancelled", async () => {
    let deleted = false;
    server.use(
      http.delete(`${API_BASE}/gateway/guardrails/GR-1`, () => {
        deleted = true;
        return HttpResponse.json(envelope({ guardrail_id: "GR-1", deleted: true }));
      }),
    );
    stub(
      statusFixture({
        guardrails: [
          {
            guardrail_id: "GR-1",
            name: "block-pii",
            stage: "BEFORE",
            action: "VALIDATION",
            scorer: "pii-scorer",
            action_endpoint_name: null,
          },
        ],
      }),
    );
    const confirmSpy = vi.spyOn(window, "confirm").mockReturnValueOnce(false);
    const user = userEvent.setup();
    render(<GatewayGuardrailsTab />);

    const deleteButton = await screen.findByRole("button", { name: "Delete block-pii" });
    await user.click(deleteButton);
    expect(confirmSpy).toHaveBeenCalledWith(
      'Delete guardrail "block-pii"? It will be detached from every endpoint.',
    );
    expect(deleted).toBe(false);
    expect(await screen.findAllByTestId("guardrail-row")).toHaveLength(1);

    confirmSpy.mockReturnValueOnce(true);
    await user.click(deleteButton);
    await waitFor(() => expect(deleted).toBe(true));
  });

  it("surfaces a generic failure message when the rejection is not an ApiError", async () => {
    stub(
      statusFixture({
        guardrails: [
          {
            guardrail_id: "GR-1",
            name: "block-pii",
            stage: "BEFORE",
            action: "VALIDATION",
            scorer: "pii-scorer",
            action_endpoint_name: null,
          },
        ],
      }),
    );
    vi.spyOn(caliberApi, "deleteGatewayGuardrail").mockRejectedValueOnce(
      new Error("boom"),
    );
    vi.spyOn(window, "confirm").mockReturnValue(true);
    const { showToast } = await import("@/lib/toast");
    const user = userEvent.setup();
    render(<GatewayGuardrailsTab />);

    await user.click(await screen.findByRole("button", { name: "Delete block-pii" }));

    await waitFor(() =>
      expect(showToast.error).toHaveBeenCalledWith("Gateway request failed"),
    );
  });

  it("shows the no-guardrails-on-this-endpoint state, then attaches and detaches a guardrail", async () => {
    // Stateful handlers (rather than re-stubbing mid-test) so the GET refetch that
    // `run()` triggers after each mutation always reflects the latest server state,
    // instead of racing a test-side `server.use()` call against the app's refetch.
    let attachBody: unknown;
    let detached = false;
    let attached = false;
    server.use(
      http.get(`${API_BASE}/gateway/guardrails`, () =>
        HttpResponse.json(
          envelope(
            statusFixture({
              guardrails: [
                {
                  guardrail_id: "GR-1",
                  name: "block-pii",
                  stage: "BEFORE",
                  action: "VALIDATION",
                  scorer: "pii-scorer",
                  action_endpoint_name: null,
                },
              ],
              coverage: [
                {
                  endpoint: "chat-endpoint",
                  endpoint_id: "ep-1",
                  guardrails: attached
                    ? [{ guardrail_id: "GR-1", name: "block-pii", execution_order: 1, enabled: true }]
                    : [],
                },
              ],
            }),
          ),
        ),
      ),
      http.get(`${API_BASE}/gateway/guardrails/catalog`, () =>
        HttpResponse.json(envelope(EMPTY_CATALOG)),
      ),
      http.post(`${API_BASE}/gateway/endpoints/ep-1/guardrails`, async ({ request }) => {
        attachBody = await request.json();
        attached = true;
        return HttpResponse.json(
          envelope({ endpoint_id: "ep-1", guardrail_id: "GR-1", attached: true }),
        );
      }),
      http.delete(`${API_BASE}/gateway/endpoints/ep-1/guardrails/GR-1`, () => {
        detached = true;
        attached = false;
        return HttpResponse.json(
          envelope({ endpoint_id: "ep-1", guardrail_id: "GR-1", detached: true }),
        );
      }),
    );
    const user = userEvent.setup();
    render(<GatewayGuardrailsTab />);

    const coverageCard = await screen.findByTestId("guardrail-coverage");
    expect(within(coverageCard).getByText("No guardrails on this endpoint.")).toBeInTheDocument();

    await user.selectOptions(within(coverageCard).getByLabelText("Attach guardrail"), "GR-1");
    await user.click(within(coverageCard).getByRole("button", { name: "Attach" }));

    await waitFor(() => expect(attachBody).toEqual({ guardrail_id: "GR-1" }));

    // The endpoint now shows the attached guardrail and "All guardrails attached"
    // (there is only one guardrail total, and it is now attached).
    expect(await within(coverageCard).findByText("All guardrails attached")).toBeInTheDocument();
    expect(within(coverageCard).getByText("· order 1")).toBeInTheDocument();

    await user.click(within(coverageCard).getByRole("button", { name: "Detach block-pii" }));
    await waitFor(() => expect(detached).toBe(true));
    expect(
      await within(coverageCard).findByText("No guardrails on this endpoint."),
    ).toBeInTheDocument();
  });

  it("toggles the create-guardrail form open and closed", async () => {
    stub(statusFixture());
    const user = userEvent.setup();
    render(<GatewayGuardrailsTab />);

    await screen.findByText("No guardrails configured on the gateway yet.");
    expect(screen.queryByTestId("create-guardrail-form")).not.toBeInTheDocument();

    await user.click(screen.getByTestId("new-guardrail-toggle"));
    expect(await screen.findByTestId("create-guardrail-form")).toBeInTheDocument();
    expect(screen.getByTestId("new-guardrail-toggle")).toHaveTextContent("Close");

    await user.click(screen.getByTestId("new-guardrail-toggle"));
    expect(screen.queryByTestId("create-guardrail-form")).not.toBeInTheDocument();
  });

  it("rejects an empty name before creating a guardrail", async () => {
    stub(statusFixture(), FULL_CATALOG);
    const { showToast } = await import("@/lib/toast");
    const user = userEvent.setup();
    render(<GatewayGuardrailsTab />);

    await user.click(await screen.findByTestId("new-guardrail-toggle"));
    await screen.findByTestId("create-guardrail-form");
    await user.click(screen.getByTestId("create-guardrail-submit"));

    expect(showToast.error).toHaveBeenCalledWith("Name is required.");
  });

  it("adopts a template's default stage/action, exposes its fields, and creates a native-scorer guardrail", async () => {
    let createdBody: unknown;
    server.use(
      http.post(`${API_BASE}/gateway/guardrails`, async ({ request }) => {
        createdBody = await request.json();
        return HttpResponse.json(
          envelope({
            guardrail_id: "GR-NEW",
            name: "my-guardrail",
            stage: "BEFORE",
            action: "VALIDATION",
            scorer: "pii",
            action_endpoint_name: null,
          }),
        );
      }),
    );
    stub(statusFixture(), FULL_CATALOG);
    const user = userEvent.setup();
    render(<GatewayGuardrailsTab />);

    await user.click(await screen.findByTestId("new-guardrail-toggle"));
    const form = await screen.findByTestId("create-guardrail-form");

    // First template (pii) is selected by default; its stage/action are adopted.
    expect(within(form).getByLabelText("Threshold")).toBeInTheDocument();
    expect(within(form).getByText(/Detects personally identifiable/)).toBeInTheDocument();

    await user.type(within(form).getByLabelText("Guardrail name"), "my-guardrail");
    await user.type(within(form).getByLabelText("Threshold"), "0.9");
    await user.type(within(form).getByLabelText("Notes"), "context notes");
    await user.selectOptions(within(form).getByLabelText("Severity"), "high");
    await user.click(within(form).getByLabelText("Block on match"));
    // Toggle a multiselect chip on, then off, then back on — exercises both the
    // "add" and "remove" branches of the chip's click handler.
    await user.click(within(form).getByRole("button", { name: "email" }));
    await user.click(within(form).getByRole("button", { name: "email" }));
    await user.click(within(form).getByRole("button", { name: "email" }));
    // Manually override the stage/action the template defaulted to.
    await user.selectOptions(within(form).getByLabelText("Stage"), "AFTER");
    await user.selectOptions(within(form).getByLabelText("Action"), "VALIDATION");
    await user.selectOptions(within(form).getByLabelText("Stage"), "BEFORE");
    await user.selectOptions(within(form).getByLabelText("Action"), "VALIDATION");

    await user.click(within(form).getByTestId("create-guardrail-submit"));

    await waitFor(() => expect(createdBody).toBeTruthy());
    expect(createdBody).toMatchObject({
      name: "my-guardrail",
      stage: "BEFORE",
      action: "VALIDATION",
      action_endpoint_id: null,
      scorer_type: "pii",
      config: {
        threshold: "0.9",
        notes: "context notes",
        severity: "high",
        block: true,
        categories: ["email"],
      },
    });
    // A successful create closes the form.
    await waitFor(() =>
      expect(screen.queryByTestId("create-guardrail-form")).not.toBeInTheDocument(),
    );
  });

  it("switches to a non-deterministic template, revealing the SANITIZATION rewrite-endpoint picker", async () => {
    stub(
      statusFixture({
        coverage: [{ endpoint: "chat-endpoint", endpoint_id: "ep-1", guardrails: [] }],
      }),
      FULL_CATALOG,
    );
    const user = userEvent.setup();
    render(<GatewayGuardrailsTab />);

    await user.click(await screen.findByTestId("new-guardrail-toggle"));
    const form = await screen.findByTestId("create-guardrail-form");

    await user.selectOptions(within(form).getByLabelText("Guardrail type"), "toxicity");
    // The toxicity template defaults to AFTER / SANITIZATION, which reveals the
    // rewrite-endpoint picker and hides the pii-only fields.
    expect(within(form).queryByLabelText("Threshold")).not.toBeInTheDocument();
    expect(await within(form).findByLabelText("Rewrite endpoint")).toBeInTheDocument();
    expect(within(form).getByLabelText("Action")).toHaveValue("SANITIZATION");
    await user.selectOptions(within(form).getByLabelText("Rewrite endpoint"), "ep-1");
    expect(within(form).getByLabelText("Rewrite endpoint")).toHaveValue("ep-1");
  });

  it("requires picking a scorer for the existing-scorer source, then creates from it", async () => {
    let createdBody: unknown;
    server.use(
      http.post(`${API_BASE}/gateway/guardrails`, async ({ request }) => {
        createdBody = await request.json();
        return HttpResponse.json(
          envelope({
            guardrail_id: "GR-EXIST",
            name: "from-existing",
            stage: "BEFORE",
            action: "VALIDATION",
            scorer: "custom-scorer",
            action_endpoint_name: null,
          }),
        );
      }),
    );
    stub(statusFixture(), FULL_CATALOG);
    const { showToast } = await import("@/lib/toast");
    const user = userEvent.setup();
    render(<GatewayGuardrailsTab />);

    await user.click(await screen.findByTestId("new-guardrail-toggle"));
    const form = await screen.findByTestId("create-guardrail-form");

    await user.selectOptions(within(form).getByLabelText("Guardrail type"), "__existing__");
    expect(within(form).getByLabelText("Scorer")).toBeInTheDocument();
    await user.type(within(form).getByLabelText("Guardrail name"), "from-existing");

    await user.click(within(form).getByTestId("create-guardrail-submit"));
    expect(showToast.error).toHaveBeenCalledWith("Pick a scorer.");

    await user.selectOptions(within(form).getByLabelText("Existing scorer"), "SCORER-1::2");
    await user.click(within(form).getByTestId("create-guardrail-submit"));

    await waitFor(() => expect(createdBody).toBeTruthy());
    expect(createdBody).toMatchObject({
      name: "from-existing",
      scorer_id: "SCORER-1",
      scorer_version: 2,
    });
  });
});
