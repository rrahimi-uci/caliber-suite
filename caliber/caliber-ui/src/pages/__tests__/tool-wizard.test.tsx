import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { http, HttpResponse } from "msw";
import { afterAll, afterEach, beforeAll, describe, expect, it, vi } from "vitest";
import { MemoryRouter } from "react-router-dom";

import { ToolWizard } from "@/pages/ToolWizard";
import { server } from "@/test/server";

const API_BASE = "/ajax-api/2.0/mlflow/caliber";
const NOW = "2026-06-08T12:00:00Z";

function envelope<T>(data: T): { data: T } {
  return { data };
}

/**
 * No <Routes>/<Route> here on purpose: the wizard calls ``navigate()`` on a
 * successful registration, and since nothing in this tree switches on the
 * current location, the component keeps rendering afterward — letting tests
 * follow a real flow of "register, then step back into Playground to
 * exercise the now-registered tool" instead of unmounting on navigate.
 */
function renderWizard(onClose: () => void = () => {}) {
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false, gcTime: 0 }, mutations: { retry: false } },
  });
  return render(
    <QueryClientProvider client={queryClient}>
      <MemoryRouter future={{ v7_startTransition: true, v7_relativeSplatPath: true }}>
        <ToolWizard onClose={onClose} />
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

beforeAll(() => server.listen({ onUnhandledRequest: "error" }));
afterEach(() => {
  server.resetHandlers();
  vi.restoreAllMocks();
});
afterAll(() => server.close());

describe("ToolWizard", () => {
  it("walks all five steps, edits the schema, and registers the tool", async () => {
    const registerCalls: Array<Record<string, unknown>> = [];
    server.use(
      http.post(`${API_BASE}/tools`, async ({ request }) => {
        const body = (await request.json()) as Record<string, unknown>;
        registerCalls.push(body);
        return HttpResponse.json(
          envelope({
            tool_id: "TL-new",
            name: body.name,
            version: body.version,
            description: body.description,
            module_path: body.module_path,
            callable_name: body.callable_name,
            input_schema: body.input_schema,
            output_schema: body.output_schema,
            side_effect_level: body.side_effect_level,
            requires_approval: body.requires_approval,
            allow_in_preview: body.allow_in_preview,
            secret_refs: body.secret_refs,
            test_cases: [],
            last_calibration: null,
            owner: body.owner,
            status: "active",
            deprecated_at: null,
            successor_tool_id: null,
            created_at: NOW,
            updated_at: NOW,
          }),
          { status: 201 },
        );
      }),
      http.post(`${API_BASE}/tools/:toolId/test-run`, async ({ request }) => {
        const body = (await request.json()) as { input: Record<string, unknown> };
        return HttpResponse.json(
          envelope({ tool_id: "TL-new", output: { echoed: body.input }, mocked: true, duration_ms: 6, error: null }),
        );
      }),
    );

    const user = userEvent.setup();
    renderWizard();

    // ── Step 1: Identity — Next is gated on a name.
    expect(screen.getByTestId("step-identity")).toBeInTheDocument();
    expect(screen.getByTestId("wizard-next")).toBeDisabled();
    await user.type(screen.getByTestId("wiz-name"), "Lookup Order");
    expect(screen.getByTestId("wizard-next")).toBeEnabled();
    await user.clear(screen.getByTestId("wiz-version"));
    await user.type(screen.getByTestId("wiz-version"), "2.0");
    await user.type(screen.getByTestId("wiz-owner"), "@team-orders");
    await user.click(screen.getByTestId("wizard-next"));

    // ── Step 2: Implementation — Next is gated on module + callable.
    expect(screen.getByTestId("step-implementation")).toBeInTheDocument();
    expect(screen.getByTestId("wiz-module")).toHaveValue("caliber.workflows.demo_tools");
    // Callable name auto-derived from the tool name while untouched.
    expect(screen.getByTestId("wiz-callable")).toHaveValue("lookup_order");
    await user.clear(screen.getByTestId("wiz-callable"));
    expect(screen.getByTestId("wizard-next")).toBeDisabled();
    await user.type(screen.getByTestId("wiz-callable"), "lookup_order");
    expect(screen.getByTestId("wizard-next")).toBeEnabled();
    await user.click(screen.getByTestId("wizard-next"));

    // ── Step 3: Schema — visual builder, raw-JSON toggle both ways.
    expect(screen.getByTestId("step-schema")).toBeInTheDocument();
    await user.click(screen.getByTestId("input-schema-add-prop"));
    const inputPropRow = screen.getByTestId(/input-schema-prop-name-/);
    await user.type(inputPropRow, "order_id");
    await user.click(screen.getByTestId(/input-schema-prop-req-/));

    await user.click(screen.getByTestId("output-schema-add-prop"));
    await user.type(screen.getByTestId(/output-schema-prop-name-/), "status");

    // Toggle input to raw JSON — it should serialize the visual property.
    await user.click(screen.getByTestId("input-schema-toggle-raw"));
    expect(screen.getByTestId("input-schema-raw")).toHaveValue(
      JSON.stringify(
        { type: "object", properties: { order_id: { type: "string" } }, required: ["order_id"] },
        null,
        2,
      ),
    );
    // Toggle back to visual — the edited property survives the round trip.
    await user.click(screen.getByTestId("input-schema-toggle-raw"));
    expect(screen.getByTestId(/input-schema-prop-name-/)).toHaveValue("order_id");

    // Output: toggle to raw, type invalid JSON (surfaces an inline error),
    // then toggle back to visual — invalid raw JSON is discarded rather than
    // corrupting the previously-built properties.
    await user.click(screen.getByTestId("output-schema-toggle-raw"));
    const outputRaw = screen.getByTestId("output-schema-raw");
    fireEvent.change(outputRaw, { target: { value: "{bad" } });
    expect(await screen.findByText("Invalid JSON")).toBeInTheDocument();
    await user.click(screen.getByTestId("output-schema-toggle-raw"));
    expect(screen.getByTestId(/output-schema-prop-name-/)).toHaveValue("status");

    await user.click(screen.getByTestId("wizard-next"));

    // ── Step 4: Playground — unregistered yet, so it's a placeholder.
    expect(screen.getByTestId("step-playground")).toBeInTheDocument();
    expect(
      screen.getByText(/Playground available after registration/),
    ).toBeInTheDocument();
    expect(screen.queryByTestId("playground-run")).not.toBeInTheDocument();
    await user.click(screen.getByTestId("wizard-next"));

    // ── Step 5: Safety & Review.
    expect(screen.getByTestId("step-safety")).toBeInTheDocument();
    // Raising to "write" forces approval on and locks the checkbox.
    await user.click(screen.getByTestId("wiz-side-effect-write"));
    expect(screen.getByTestId("wiz-requires-approval")).toBeChecked();
    expect(screen.getByTestId("wiz-requires-approval")).toBeDisabled();
    // Lowering back to "read" unlocks it again (approval stays on until the
    // user turns it off explicitly).
    await user.click(screen.getByTestId("wiz-side-effect-read"));
    expect(screen.getByTestId("wiz-requires-approval")).toBeEnabled();
    await user.click(screen.getByTestId("wiz-requires-approval"));
    expect(screen.getByTestId("wiz-requires-approval")).not.toBeChecked();
    await user.click(screen.getByTestId("wiz-allow-preview"));

    // Secret refs: add via the button, add another via Enter, then remove one.
    await user.type(screen.getByTestId("wiz-secret-input"), "STRIPE_KEY");
    await user.click(screen.getByTestId("wiz-add-secret"));
    await user.type(screen.getByTestId("wiz-secret-input"), "DB_URL{Enter}");
    expect(screen.getByText("STRIPE_KEY")).toBeInTheDocument();
    expect(screen.getByText("DB_URL")).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Remove DB_URL" }));
    expect(screen.queryByText("DB_URL")).not.toBeInTheDocument();

    // Review summary reflects the assembled form.
    const summary = screen.getByTestId("review-summary");
    expect(summary).toHaveTextContent("Lookup Order");
    expect(summary).toHaveTextContent("2.0");
    expect(summary).toHaveTextContent("@team-orders");
    expect(summary).toHaveTextContent("STRIPE_KEY");

    // Submit — registers with the assembled payload.
    await user.click(screen.getByTestId("wizard-submit"));
    await waitFor(() => expect(registerCalls).toHaveLength(1));
    expect(registerCalls[0]).toMatchObject({
      name: "Lookup Order",
      version: "2.0",
      module_path: "caliber.workflows.demo_tools",
      callable_name: "lookup_order",
      side_effect_level: "read",
      requires_approval: false,
      allow_in_preview: true,
      secret_refs: ["STRIPE_KEY"],
      owner: "@team-orders",
      input_schema: { type: "object", properties: { order_id: { type: "string" } }, required: ["order_id"] },
      output_schema: { type: "object", properties: { status: { type: "string" } } },
    });

    // Step back into Playground now that the tool is registered — the
    // real input form + run button replace the placeholder.
    await user.click(screen.getByTestId("wizard-step-3"));
    expect(await screen.findByTestId("playground-input-order_id")).toBeInTheDocument();
    await user.type(screen.getByTestId("playground-input-order_id"), "ORD-1");
    await user.click(screen.getByTestId("playground-run"));
    expect(await screen.findByTestId("playground-result")).toBeInTheDocument();
    expect(screen.getByText("Sandboxed (mocked)")).toBeInTheDocument();
    expect(screen.getByTestId("playground-output")).toHaveTextContent("ORD-1");
  });

  it("shows a registration error and lets the user go back or cancel", async () => {
    server.use(
      http.post(`${API_BASE}/tools`, () => HttpResponse.json({ detail: "name already registered" }, { status: 409 })),
    );
    const onClose = vi.fn();
    const user = userEvent.setup();
    renderWizard(onClose);

    await user.type(screen.getByTestId("wiz-name"), "dup_tool");
    await user.click(screen.getByTestId("wizard-next"));
    await user.click(screen.getByTestId("wizard-next"));
    await user.click(screen.getByTestId("wizard-next"));
    await user.click(screen.getByTestId("wizard-next"));
    expect(screen.getByTestId("step-safety")).toBeInTheDocument();

    await user.click(screen.getByTestId("wizard-submit"));
    expect(await screen.findByTestId("wizard-error")).toHaveTextContent("name already registered");

    // Back navigates within the wizard (not yet closing it).
    await user.click(screen.getByTestId("wizard-back"));
    expect(screen.getByTestId("step-playground")).toBeInTheDocument();
    expect(onClose).not.toHaveBeenCalled();

    // Step-indicator lets you jump back to any completed step directly.
    await user.click(screen.getByTestId("wizard-step-0"));
    expect(screen.getByTestId("step-identity")).toBeInTheDocument();
    // A not-yet-reached step is inert.
    await user.click(screen.getByTestId("wizard-step-4"));
    expect(screen.getByTestId("step-identity")).toBeInTheDocument();

    // Cancel (Back at step 0) calls onClose instead of navigating a step.
    await user.click(screen.getByTestId("wizard-back"));
    expect(onClose).toHaveBeenCalledTimes(1);
  });
});
