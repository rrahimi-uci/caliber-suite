/**
 * Tool Wizard — comprehensive tests for the 5-step wizard flow.
 */

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { http, HttpResponse } from "msw";
import { afterAll, afterEach, beforeAll, describe, expect, it } from "vitest";
import { MemoryRouter, Route, Routes } from "react-router-dom";

import { ToolWizard } from "@/pages/ToolWizard";
import { server } from "@/test/server";

const API_BASE = "/ajax-api/2.0/mlflow/caliber";
const NOW = "2026-05-30T00:00:00Z";

function envelope<T>(data: T): { data: T } {
  return { data };
}

function renderWizard(onClose = () => {}): void {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false, gcTime: 0 } } });
  render(
    <QueryClientProvider client={qc}>
      <MemoryRouter future={{ v7_startTransition: true, v7_relativeSplatPath: true }} initialEntries={["/tools"]}>
        <Routes>
          <Route path="/tools" element={<ToolWizard onClose={onClose} />} />
          <Route path="/tools/:toolId" element={<div data-testid="tool-detail-route">DETAIL</div>} />
        </Routes>
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

function makeTool(overrides: Record<string, unknown> = {}) {
  return {
    tool_id: "TL-99",
    name: "test_tool",
    version: "1.0",
    description: "A test tool",
    module_path: "caliber.workflows.demo_tools",
    callable_name: "test_tool",
    input_schema: null,
    output_schema: null,
    side_effect_level: "read",
    requires_approval: false,
    allow_in_preview: false,
    secret_refs: [],
    owner: "@tester",
    status: "active",
    deprecated_at: null,
    successor_tool_id: null,
    created_at: NOW,
    updated_at: NOW,
    ...overrides,
  };
}

beforeAll(() => server.listen({ onUnhandledRequest: "error" }));
afterEach(() => server.resetHandlers());
afterAll(() => server.close());

describe("ToolWizard", () => {
  describe("Step navigation", () => {
    it("renders step 1 (Identity) by default", () => {
      renderWizard();
      expect(screen.getByTestId("tool-wizard")).toBeInTheDocument();
      expect(screen.getByTestId("step-identity")).toBeInTheDocument();
      expect(screen.getByTestId("wizard-steps")).toBeInTheDocument();
    });

    it("disables Next button when name is empty", () => {
      renderWizard();
      const next = screen.getByTestId("wizard-next");
      expect(next).toBeDisabled();
    });

    it("enables Next when name is provided and navigates to step 2", async () => {
      renderWizard();
      await userEvent.type(screen.getByTestId("wiz-name"), "my_tool");
      const next = screen.getByTestId("wizard-next");
      expect(next).not.toBeDisabled();
      await userEvent.click(next);
      expect(screen.getByTestId("step-implementation")).toBeInTheDocument();
    });

    it("goes back to step 1 when clicking Back from step 2", async () => {
      renderWizard();
      await userEvent.type(screen.getByTestId("wiz-name"), "my_tool");
      await userEvent.click(screen.getByTestId("wizard-next"));
      expect(screen.getByTestId("step-implementation")).toBeInTheDocument();
      await userEvent.click(screen.getByTestId("wizard-back"));
      expect(screen.getByTestId("step-identity")).toBeInTheDocument();
    });

    it("calls onClose when Cancel is clicked on step 1", async () => {
      let closed = false;
      renderWizard(() => { closed = true; });
      await userEvent.click(screen.getByTestId("wizard-back"));
      expect(closed).toBe(true);
    });

    it("calls onClose when X button is clicked", async () => {
      let closed = false;
      renderWizard(() => { closed = true; });
      await userEvent.click(screen.getByTestId("wizard-close"));
      expect(closed).toBe(true);
    });

    it("navigates through all 5 steps", async () => {
      renderWizard();
      // Step 1: Identity
      await userEvent.type(screen.getByTestId("wiz-name"), "order_tool");
      await userEvent.click(screen.getByTestId("wizard-next"));

      // Step 2: Implementation (module_path has default, callable auto-set)
      expect(screen.getByTestId("step-implementation")).toBeInTheDocument();
      expect((screen.getByTestId("wiz-callable") as HTMLInputElement).value).toBe("order_tool");
      await userEvent.click(screen.getByTestId("wizard-next"));

      // Step 3: Schema
      expect(screen.getByTestId("step-schema")).toBeInTheDocument();
      await userEvent.click(screen.getByTestId("wizard-next"));

      // Step 4: Playground
      expect(screen.getByTestId("step-playground")).toBeInTheDocument();
      await userEvent.click(screen.getByTestId("wizard-next"));

      // Step 5: Safety & Review
      expect(screen.getByTestId("step-safety")).toBeInTheDocument();
      // Should show Register Tool button instead of Next
      expect(screen.getByTestId("wizard-submit")).toBeInTheDocument();
      expect(screen.queryByTestId("wizard-next")).not.toBeInTheDocument();
    });
  });

  describe("Step 1: Identity", () => {
    it("auto-generates callable_name from name (snake_case)", async () => {
      renderWizard();
      await userEvent.type(screen.getByTestId("wiz-name"), "MyTool");
      await userEvent.click(screen.getByTestId("wizard-next"));
      expect((screen.getByTestId("wiz-callable") as HTMLInputElement).value).toBe("my_tool");
    });

    it("preserves version field", async () => {
      renderWizard();
      const versionInput = screen.getByTestId("wiz-version") as HTMLInputElement;
      expect(versionInput.value).toBe("1.0");
      await userEvent.clear(versionInput);
      await userEvent.type(versionInput, "2.0");
      expect(versionInput.value).toBe("2.0");
    });
  });

  describe("Step 2: Implementation", () => {
    it("disables Next when callable_name is empty", async () => {
      renderWizard();
      await userEvent.type(screen.getByTestId("wiz-name"), "test");
      await userEvent.click(screen.getByTestId("wizard-next"));

      // Clear the auto-populated callable_name
      const callable = screen.getByTestId("wiz-callable") as HTMLInputElement;
      await userEvent.clear(callable);
      expect(screen.getByTestId("wizard-next")).toBeDisabled();
    });
  });

  describe("Step 3: Schema", () => {
    async function goToSchemaStep(): Promise<void> {
      renderWizard();
      await userEvent.type(screen.getByTestId("wiz-name"), "test_tool");
      await userEvent.click(screen.getByTestId("wizard-next"));
      await userEvent.click(screen.getByTestId("wizard-next"));
    }

    it("renders input and output schema builders", async () => {
      await goToSchemaStep();
      expect(screen.getByTestId("step-schema")).toBeInTheDocument();
      expect(screen.getByTestId("input-schema-add-prop")).toBeInTheDocument();
      expect(screen.getByTestId("output-schema-add-prop")).toBeInTheDocument();
    });

    it("adds an input property row", async () => {
      await goToSchemaStep();
      await userEvent.click(screen.getByTestId("input-schema-add-prop"));
      // Check that a name input appeared for the new property
      const nameInputs = screen.getByTestId("step-schema").querySelectorAll("input[data-testid^='input-schema-prop-name-']");
      expect(nameInputs.length).toBe(1);
    });

    it("toggles to raw JSON mode", async () => {
      await goToSchemaStep();
      await userEvent.click(screen.getByTestId("input-schema-toggle-raw"));
      expect(screen.getByTestId("input-schema-raw")).toBeInTheDocument();
    });

    it("shows invalid-json state and gracefully exits raw mode", async () => {
      await goToSchemaStep();
      await userEvent.click(screen.getByTestId("input-schema-toggle-raw"));
      const raw = screen.getByTestId("input-schema-raw");
      fireEvent.change(raw, { target: { value: "{bad" } });
      expect(screen.getByText("Invalid JSON")).toBeInTheDocument();

      // Toggle back to visual mode; invalid raw JSON should be ignored.
      await userEvent.click(screen.getByTestId("input-schema-toggle-raw"));
      expect(screen.queryByTestId("input-schema-raw")).not.toBeInTheDocument();
    });

    it("parses raw output schema back into visual properties", async () => {
      await goToSchemaStep();
      await userEvent.click(screen.getByTestId("output-schema-toggle-raw"));
      const raw = screen.getByTestId("output-schema-raw");
      fireEvent.change(raw, {
        target: {
          value: '{"type":"object","required":["ok"],"properties":{"ok":{"type":"boolean","description":"status"}}}',
        },
      });
      await userEvent.click(screen.getByTestId("output-schema-toggle-raw"));

      const step = screen.getByTestId("step-schema");
      const row = step.querySelector("[data-testid^='output-schema-prop-']");
      expect(row).toBeTruthy();
      const nameField = step.querySelector(
        "[data-testid^='output-schema-prop-name-']",
      ) as HTMLInputElement | null;
      expect(nameField?.value).toBe("ok");
    });
  });

  describe("Step 4: Playground", () => {
    async function goToPlaygroundStep(): Promise<void> {
      renderWizard();
      await userEvent.type(screen.getByTestId("wiz-name"), "test_tool");
      await userEvent.click(screen.getByTestId("wizard-next"));
      await userEvent.click(screen.getByTestId("wizard-next"));
      await userEvent.click(screen.getByTestId("wizard-next"));
    }

    it("shows placeholder when tool is not yet registered", async () => {
      await goToPlaygroundStep();
      expect(screen.getByTestId("step-playground")).toBeInTheDocument();
      expect(screen.getByText(/Playground available after registration/)).toBeInTheDocument();
    });
  });

  describe("Step 5: Safety & Review", () => {
    async function goToSafetyStep(): Promise<void> {
      renderWizard();
      await userEvent.type(screen.getByTestId("wiz-name"), "test_tool");
      await userEvent.click(screen.getByTestId("wizard-next"));
      await userEvent.click(screen.getByTestId("wizard-next"));
      await userEvent.click(screen.getByTestId("wizard-next"));
      await userEvent.click(screen.getByTestId("wizard-next"));
    }

    it("renders side-effect level selector with default read", async () => {
      await goToSafetyStep();
      expect(screen.getByTestId("step-safety")).toBeInTheDocument();
      const readBtn = screen.getByTestId("wiz-side-effect-read");
      expect(readBtn.className).toContain("border-caliber-purple");
    });

    it("switches side-effect level to write", async () => {
      await goToSafetyStep();
      await userEvent.click(screen.getByTestId("wiz-side-effect-write"));
      const writeBtn = screen.getByTestId("wiz-side-effect-write");
      expect(writeBtn.className).toContain("border-caliber-purple");
    });

    it("toggles requires_approval", async () => {
      await goToSafetyStep();
      const checkbox = screen.getByTestId("wiz-requires-approval") as HTMLInputElement;
      expect(checkbox.checked).toBe(false);
      await userEvent.click(checkbox);
      expect(checkbox.checked).toBe(true);
    });

    it("toggles allow_in_preview", async () => {
      await goToSafetyStep();
      const checkbox = screen.getByTestId("wiz-allow-preview") as HTMLInputElement;
      expect(checkbox.checked).toBe(false);
      await userEvent.click(checkbox);
      expect(checkbox.checked).toBe(true);
    });

    it("adds and removes secret refs", async () => {
      await goToSafetyStep();
      const input = screen.getByTestId("wiz-secret-input");
      await userEvent.type(input, "MY_SECRET");
      await userEvent.click(screen.getByTestId("wiz-add-secret"));
      expect(screen.getAllByText("MY_SECRET").length).toBeGreaterThanOrEqual(1);

      // Remove it
      await userEvent.click(screen.getByLabelText("Remove MY_SECRET"));
      // The secret should no longer be in the tag list
      expect(screen.queryByLabelText("Remove MY_SECRET")).not.toBeInTheDocument();
    });

    it("adds secrets with Enter and does not duplicate refs", async () => {
      await goToSafetyStep();
      const input = screen.getByTestId("wiz-secret-input");
      await userEvent.type(input, "API_KEY{enter}");
      await userEvent.type(input, "API_KEY{enter}");
      expect(screen.getAllByLabelText("Remove API_KEY")).toHaveLength(1);
    });

    it("shows review summary with correct values", async () => {
      renderWizard();

      // Step 1: fill identity
      await userEvent.type(screen.getByTestId("wiz-name"), "order_lookup");
      await userEvent.clear(screen.getByTestId("wiz-version"));
      await userEvent.type(screen.getByTestId("wiz-version"), "2.0");
      await userEvent.type(screen.getByTestId("wiz-owner"), "@team");
      await userEvent.click(screen.getByTestId("wizard-next"));

      // Step 2: keep defaults
      await userEvent.click(screen.getByTestId("wizard-next"));

      // Step 3: skip schema
      await userEvent.click(screen.getByTestId("wizard-next"));

      // Step 4: skip playground
      await userEvent.click(screen.getByTestId("wizard-next"));

      // Step 5: check review summary
      const summary = screen.getByTestId("review-summary");
      // order_lookup appears for both Name and Callable
      expect(within(summary).getAllByText("order_lookup").length).toBeGreaterThanOrEqual(1);
      expect(within(summary).getByText("2.0")).toBeInTheDocument();
      expect(within(summary).getByText("@team")).toBeInTheDocument();
    });
  });

  describe("Full wizard submission", () => {
    it("registers a tool and navigates to detail page", async () => {
      let postedPayload: Record<string, unknown> | null = null;
      server.use(
        http.post(`${API_BASE}/tools`, async ({ request }) => {
          postedPayload = (await request.json()) as Record<string, unknown>;
          return HttpResponse.json(envelope(makeTool({ name: postedPayload.name })), {
            status: 201,
          });
        }),
      );

      renderWizard();

      // Step 1: Identity
      await userEvent.type(screen.getByTestId("wiz-name"), "order_lookup");
      await userEvent.type(screen.getByTestId("wiz-description"), "Look up order details");
      await userEvent.type(screen.getByTestId("wiz-owner"), "@ops");
      await userEvent.click(screen.getByTestId("wizard-next"));

      // Step 2: Implementation
      await userEvent.click(screen.getByTestId("wizard-next"));

      // Step 3: Schema — skip
      await userEvent.click(screen.getByTestId("wizard-next"));

      // Step 4: Playground — skip
      await userEvent.click(screen.getByTestId("wizard-next"));

      // Step 5: Safety — set to write + requires approval
      await userEvent.click(screen.getByTestId("wiz-side-effect-write"));
      await userEvent.click(screen.getByTestId("wiz-requires-approval"));

      // Submit
      await userEvent.click(screen.getByTestId("wizard-submit"));

      await waitFor(() => expect(postedPayload).not.toBeNull());
      expect(postedPayload!.name).toBe("order_lookup");
      expect(postedPayload!.description).toBe("Look up order details");
      expect(postedPayload!.side_effect_level).toBe("write");
      expect(postedPayload!.requires_approval).toBe(true);
      expect(postedPayload!.owner).toBe("@ops");

      // Should navigate to tool detail
      expect(await screen.findByTestId("tool-detail-route")).toBeInTheDocument();
    });

    it("shows error when registration fails", async () => {
      server.use(
        http.post(`${API_BASE}/tools`, () =>
          HttpResponse.json({ detail: "tool already exists" }, { status: 409 }),
        ),
      );

      renderWizard();
      await userEvent.type(screen.getByTestId("wiz-name"), "dup_tool");
      await userEvent.click(screen.getByTestId("wizard-next"));
      await userEvent.click(screen.getByTestId("wizard-next"));
      await userEvent.click(screen.getByTestId("wizard-next"));
      await userEvent.click(screen.getByTestId("wizard-next"));
      await userEvent.click(screen.getByTestId("wizard-submit"));

      expect(await screen.findByTestId("wizard-error")).toBeInTheDocument();
    });

    it("submits schema when defined via visual builder", async () => {
      let postedPayload: Record<string, unknown> | null = null;
      server.use(
        http.post(`${API_BASE}/tools`, async ({ request }) => {
          postedPayload = (await request.json()) as Record<string, unknown>;
          return HttpResponse.json(envelope(makeTool()), { status: 201 });
        }),
      );

      renderWizard();

      // Step 1
      await userEvent.type(screen.getByTestId("wiz-name"), "schema_tool");
      await userEvent.click(screen.getByTestId("wizard-next"));

      // Step 2
      await userEvent.click(screen.getByTestId("wizard-next"));

      // Step 3: Add an input property
      await userEvent.click(screen.getByTestId("input-schema-add-prop"));
      const row = screen.getByTestId("step-schema").querySelector("[data-testid^='input-schema-prop-']")!;
      const nameInput = row.querySelector("[data-testid^='input-schema-prop-name-']") as HTMLInputElement;
      await userEvent.type(nameInput, "order_id");
      await userEvent.click(screen.getByTestId("wizard-next"));

      // Step 4
      await userEvent.click(screen.getByTestId("wizard-next"));

      // Step 5: submit
      await userEvent.click(screen.getByTestId("wizard-submit"));

      await waitFor(() => expect(postedPayload).not.toBeNull());
      const inputSchema = postedPayload!.input_schema as Record<string, unknown>;
      expect(inputSchema).toBeTruthy();
      expect(inputSchema.type).toBe("object");
      const props = inputSchema.properties as Record<string, unknown>;
      expect(props.order_id).toBeTruthy();
    });

    it("allows jumping back via completed step indicators", async () => {
      renderWizard();
      await userEvent.type(screen.getByTestId("wiz-name"), "jump_tool");
      await userEvent.click(screen.getByTestId("wizard-next"));
      await userEvent.click(screen.getByTestId("wizard-next"));
      expect(screen.getByTestId("step-schema")).toBeInTheDocument();

      await userEvent.click(screen.getByTestId("wizard-step-0"));
      expect(screen.getByTestId("step-identity")).toBeInTheDocument();
    });
  });
});
