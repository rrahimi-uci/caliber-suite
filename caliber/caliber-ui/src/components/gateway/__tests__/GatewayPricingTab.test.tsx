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

import type { LlmPricing } from "@/api/types";
import { GatewayPricingTab } from "@/components/gateway/GatewayPricingTab";
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

function makeRow(overrides: Partial<LlmPricing> = {}): LlmPricing {
  return {
    pricing_id: "PRICE-1",
    provider: "openai",
    model_id: "gpt-4o",
    prompt_price: 0.0025,
    completion_price: 0.01,
    cached_prompt_price: null,
    owner: "admin",
    tags: [],
    status: "active",
    created_at: "2026-01-01T00:00:00Z",
    updated_at: "2026-01-01T00:00:00Z",
    ...overrides,
  };
}

beforeAll(() => server.listen({ onUnhandledRequest: "error" }));
afterEach(() => server.resetHandlers());
afterAll(() => server.close());

describe("GatewayPricingTab", () => {
  it("shows the built-in-defaults placeholder when there are no custom rates", async () => {
    server.use(
      http.get(`${API_BASE}/llm-pricing`, () =>
        HttpResponse.json(envelope([])),
      ),
    );

    render(<GatewayPricingTab />);

    expect(
      await screen.findByText(/No custom rates.*Add a rate to override\./),
    ).toBeInTheDocument();
    expect(screen.queryByTestId("pricing-row")).not.toBeInTheDocument();
  });

  it("renders one row per rate, with a dash for an unset cached price", async () => {
    server.use(
      http.get(`${API_BASE}/llm-pricing`, () =>
        HttpResponse.json(
          envelope([
            makeRow(),
            makeRow({
              pricing_id: "PRICE-2",
              model_id: "gpt-4o-mini",
              cached_prompt_price: 0.0005,
              status: "archived",
            }),
          ]),
        ),
      ),
    );

    render(<GatewayPricingTab />);

    const rows = await screen.findAllByTestId("pricing-row");
    expect(rows).toHaveLength(2);
    expect(within(rows[0]!).getByText("—")).toBeInTheDocument();
    expect(within(rows[0]!).getByText("active")).toBeInTheDocument();
    expect(within(rows[1]!).getByText("0.0005")).toBeInTheDocument();
    expect(within(rows[1]!).getByText("archived")).toBeInTheDocument();
  });

  it("surfaces a load error instead of the table", async () => {
    server.use(
      http.get(`${API_BASE}/llm-pricing`, () =>
        HttpResponse.json(
          { detail: "pricing store unavailable" },
          { status: 500 },
        ),
      ),
    );

    render(<GatewayPricingTab />);

    expect(
      await screen.findByText("Failed to load pricing"),
    ).toBeInTheDocument();
    expect(screen.getByText(/pricing store unavailable/i)).toBeInTheDocument();
  });

  it("creates a new rate through the add-rate form and refreshes the table", async () => {
    let created: Record<string, unknown> | null = null;
    server.use(
      http.get(`${API_BASE}/llm-pricing`, () =>
        HttpResponse.json(
          envelope(
            created
              ? [makeRow(), makeRow({ pricing_id: "PRICE-2", ...created })]
              : [makeRow()],
          ),
        ),
      ),
      http.post(`${API_BASE}/llm-pricing`, async ({ request }) => {
        created = (await request.json()) as Record<string, unknown>;
        return HttpResponse.json(
          envelope(makeRow({ pricing_id: "PRICE-2", ...created })),
        );
      }),
    );

    const user = userEvent.setup();
    render(<GatewayPricingTab />);
    await screen.findAllByTestId("pricing-row");

    await user.click(screen.getByRole("button", { name: /add rate/i }));
    const form = await screen.findByTestId("pricing-form");
    await user.type(within(form).getByPlaceholderText("openai"), "anthropic");
    await user.type(
      within(form).getByPlaceholderText("gpt-5.6-luna"),
      "claude-x",
    );
    await user.type(within(form).getByPlaceholderText("0.0025"), "0.003");
    await user.type(within(form).getByPlaceholderText("0.01"), "0.015");
    await user.click(within(form).getByRole("button", { name: "Create" }));

    await waitFor(() =>
      expect(screen.queryByTestId("pricing-form")).not.toBeInTheDocument(),
    );
    expect(created).toMatchObject({
      provider: "anthropic",
      model_id: "claude-x",
      prompt_price: 0.003,
      completion_price: 0.015,
      cached_prompt_price: null,
    });
    await waitFor(() =>
      expect(screen.getAllByTestId("pricing-row")).toHaveLength(2),
    );
  });

  it("disables Create until both provider and model are filled in", async () => {
    server.use(
      http.get(`${API_BASE}/llm-pricing`, () =>
        HttpResponse.json(envelope([])),
      ),
    );

    const user = userEvent.setup();
    render(<GatewayPricingTab />);
    await screen.findByText(/No custom rates/);

    await user.click(screen.getByRole("button", { name: /add rate/i }));
    const form = await screen.findByTestId("pricing-form");
    const submit = within(form).getByRole("button", { name: "Create" });
    expect(submit).toBeDisabled();

    await user.type(within(form).getByPlaceholderText("openai"), "openai");
    expect(submit).toBeDisabled();

    await user.type(within(form).getByPlaceholderText("gpt-5.6-luna"), "gpt-5");
    expect(submit).not.toBeDisabled();
  });

  it("closes the add-rate form on Cancel without submitting", async () => {
    server.use(
      http.get(`${API_BASE}/llm-pricing`, () =>
        HttpResponse.json(envelope([])),
      ),
    );

    const user = userEvent.setup();
    render(<GatewayPricingTab />);
    await screen.findByText(/No custom rates/);

    await user.click(screen.getByRole("button", { name: /add rate/i }));
    await screen.findByTestId("pricing-form");
    await user.click(screen.getByRole("button", { name: "Cancel" }));
    expect(screen.queryByTestId("pricing-form")).not.toBeInTheDocument();
  });

  it("edits an existing rate, including its status, through the row's edit action", async () => {
    let patchBody: Record<string, unknown> | null = null;
    server.use(
      http.get(`${API_BASE}/llm-pricing`, () =>
        HttpResponse.json(envelope([makeRow()])),
      ),
      http.patch(`${API_BASE}/llm-pricing/PRICE-1`, async ({ request }) => {
        patchBody = (await request.json()) as Record<string, unknown>;
        return HttpResponse.json(envelope(makeRow({ ...patchBody })));
      }),
    );

    const user = userEvent.setup();
    render(<GatewayPricingTab />);
    await screen.findAllByTestId("pricing-row");

    await user.click(
      screen.getByRole("button", { name: "Edit openai/gpt-4o" }),
    );
    const form = await screen.findByTestId("pricing-form");
    expect(within(form).getByText("Edit openai/gpt-4o")).toBeInTheDocument();

    await user.selectOptions(within(form).getByRole("combobox"), "archived");
    await user.click(within(form).getByRole("button", { name: "Save" }));

    await waitFor(() =>
      expect(screen.queryByTestId("pricing-form")).not.toBeInTheDocument(),
    );
    expect(patchBody).toMatchObject({
      status: "archived",
      provider: "openai",
      model_id: "gpt-4o",
    });
  });

  it("shows the save error inline and keeps the form open when the update call fails", async () => {
    server.use(
      http.get(`${API_BASE}/llm-pricing`, () =>
        HttpResponse.json(envelope([makeRow()])),
      ),
      http.patch(`${API_BASE}/llm-pricing/PRICE-1`, () =>
        HttpResponse.json({ detail: "rate is locked" }, { status: 409 }),
      ),
    );

    const user = userEvent.setup();
    render(<GatewayPricingTab />);
    await screen.findAllByTestId("pricing-row");

    await user.click(
      screen.getByRole("button", { name: "Edit openai/gpt-4o" }),
    );
    const form = await screen.findByTestId("pricing-form");
    await user.click(within(form).getByRole("button", { name: "Save" }));

    expect(
      await within(form).findByText(/rate is locked/i),
    ).toBeInTheDocument();
    expect(screen.getByTestId("pricing-form")).toBeInTheDocument();
  });
});
