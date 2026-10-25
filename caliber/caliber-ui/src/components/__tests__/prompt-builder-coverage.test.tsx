import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { http, HttpResponse } from "msw";
import {
  afterAll,
  afterEach,
  beforeAll,
  beforeEach,
  describe,
  expect,
  it,
  vi,
} from "vitest";
import type { ComponentProps } from "react";

import {
  buildPreviewRequest,
  estimateRows,
  getModifierDisabledReason,
  humanizeFacet,
  parsePreviewVariables,
  parseRuntimeVariables,
  PromptBuilder,
} from "@/components/PromptBuilder";
import { caliberApi } from "@/api/caliberApi";
import { capabilitiesBody } from "@/test/handlers";
import { server } from "@/test/server";

const API_BASE = "/ajax-api/2.0/mlflow/caliber";

function envelope<T>(data: T): { data: T } {
  return { data };
}

function renderBuilder(
  props: ComponentProps<typeof PromptBuilder>,
): ReturnType<typeof render> {
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false, gcTime: 0 } },
  });
  return render(
    <QueryClientProvider client={queryClient}>
      <PromptBuilder {...props} />
    </QueryClientProvider>,
  );
}

beforeAll(() => server.listen({ onUnhandledRequest: "error" }));
beforeEach(() => {
  server.use(
    http.post(`${API_BASE}/prompts/:name/aliases/:alias`, ({ params }) =>
      HttpResponse.json(
        envelope({
          name: String(params.name),
          version: 1,
          alias: String(params.alias),
        }),
      ),
    ),
  );
});
afterEach(() => {
  server.resetHandlers();
  window.localStorage.clear();
  vi.restoreAllMocks();
});
afterAll(() => server.close());

const modifier = (overrides: Record<string, unknown> = {}) => ({
  id: "tone",
  compatible_base_ids: [],
  incompatible_modifier_ids: [],
  ...overrides,
}) as never;

describe("PromptBuilder pure decision helpers", () => {
  it("normalizes preview and runtime variables across valid and invalid input", () => {
    expect(parseRuntimeVariables(" a, b\n\n c ")).toEqual(["a", "b", "c"]);
    expect(parseRuntimeVariables(" ")).toEqual([]);
    expect(parsePreviewVariables(" ")).toEqual({ ok: true, value: {} });
    expect(parsePreviewVariables('{"count":2,"enabled":true}')).toEqual({
      ok: true,
      value: { count: "2", enabled: "true" },
    });
    expect(parsePreviewVariables("[]")).toEqual({
      ok: false,
      error: "Preview variables must be a JSON object.",
    });
    expect(parsePreviewVariables("null")).toEqual({
      ok: false,
      error: "Preview variables must be a JSON object.",
    });
    expect(parsePreviewVariables("not-json")).toEqual({
      ok: false,
      error: "Preview variables must be valid JSON.",
    });
  });

  it("builds a preview request only after validating its base and variables", () => {
    expect(
      buildPreviewRequest({
        baseTemplateId: "",
        modifierIds: [],
        builderValues: {},
        previewVariablesText: "{}",
        runtimeVariablesText: "",
        templateOverride: "",
        sectionOverrides: {},
      }),
    ).toEqual({ ok: false, error: "Select a base template first." });

    expect(
      buildPreviewRequest({
        baseTemplateId: "base",
        modifierIds: ["tone"],
        builderValues: { audience: "support" },
        previewVariablesText: "bad",
        runtimeVariablesText: "x",
        templateOverride: "ignored",
        sectionOverrides: {},
      }),
    ).toEqual({ ok: false, error: "Preview variables must be valid JSON." });

    expect(
      buildPreviewRequest({
        baseTemplateId: "base",
        modifierIds: ["tone"],
        builderValues: { audience: "support" },
        previewVariablesText: '{"name":"Ada"}',
        runtimeVariablesText: " user_id, locale ",
        templateOverride: "  custom template  ",
        sectionOverrides: { safety: "strict" },
      }),
    ).toEqual({
      ok: true,
      payload: {
        base_template_id: "base",
        modifier_ids: ["tone"],
        builder_values: { audience: "support" },
        preview_variables: { name: "Ada" },
        runtime_variables: ["user_id", "locale"],
        template_override: "custom template",
        section_overrides: { safety: "strict" },
      },
    });
  });

  it("explains modifier compatibility and conflict decisions", () => {
    expect(humanizeFacet("customer-support")).toBe("Customer Support");
    expect(humanizeFacet("__tone__")).toBe("Tone");
    expect(estimateRows("one")).toBe(3);
    expect(estimateRows("1\n2\n3\n4\n5\n6\n7\n8\n9\n10\n11")).toBe(10);

    expect(
      getModifierDisabledReason(
        modifier({ compatible_base_ids: ["other"] }),
        "base",
        [],
        [],
      ),
    ).toContain("Not compatible");
    expect(
      getModifierDisabledReason(
        modifier({ incompatible_modifier_ids: ["selected"] }),
        "base",
        ["selected"],
        [],
      ),
    ).toContain("Conflicts");
    expect(
      getModifierDisabledReason(
        modifier(),
        "base",
        ["selected"],
        [modifier({ id: "selected", incompatible_modifier_ids: ["tone"] })],
      ),
    ).toContain("Conflicts");
    expect(getModifierDisabledReason(modifier(), "base", [], [])).toBeNull();
  });
});

describe("PromptBuilder component states", () => {
  it("covers the paste path, runtime-variable hint, validation, and create callback", async () => {
    const user = userEvent.setup();
    const onCreated = vi.fn();
    const onCancel = vi.fn();

    renderBuilder(
      {
        prefillName: "",
        onCancel: onCancel,
        onCreated: onCreated,
      },
    );

    await user.click(await screen.findByRole("button", { name: /^Write \/ paste/ }));
    const promptText = screen.getByRole("textbox", { name: "Prompt text" });
    fireEvent.change(promptText, {
      target: { value: "Answer {user_input} with evidence." },
    });
    expect(screen.getByText("{user_input}")).toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: /^Create and Open/ }));
    expect(await screen.findByText("Prompt name is required.")).toBeInTheDocument();

    await user.type(screen.getByRole("textbox", { name: "Prompt name" }), "support-variant");
    await user.click(screen.getByRole("checkbox", { name: /Open Prompt Calibration/ }));
    expect(screen.getByRole("button", { name: /^Create( in staging)?$/ })).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: /^Create( in staging)?$/ }));

    await waitFor(() => {
      expect(onCreated).toHaveBeenCalled();
    });
    await user.click(screen.getByRole("button", { name: "Close" }));
    expect(onCancel).toHaveBeenCalledTimes(1);
  });

  it("covers clone loading, version switching, provenance, and clone validation", async () => {
    const user = userEvent.setup();
    const onCreated = vi.fn();

    renderBuilder({ onCancel: vi.fn(), onCreated });
    await user.click(await screen.findByRole("button", { name: /^Start from existing/ }));

    const source = await screen.findByRole("combobox", { name: "Source prompt" });
    await user.selectOptions(source, "support-agent");
    expect(await screen.findByTestId("clone-provenance")).toHaveTextContent(
      "Forked from support-agent v4",
    );

    await user.selectOptions(screen.getByRole("combobox", { name: "Source version" }), "3");
    expect(await screen.findByTestId("clone-provenance")).toHaveTextContent(
      "Forked from support-agent v3",
    );

    const name = screen.getByRole("textbox", { name: "Prompt name" });
    await user.clear(name);
    await user.type(name, "support-agent");
    await user.click(screen.getByRole("button", { name: /^Create and Open/ }));
    expect(
      await screen.findByText("Choose a new name — a clone is a variant, not a new version of the source."),
    ).toBeInTheDocument();

    await user.clear(name);
    await user.type(name, "support-agent-variant");
    await user.click(screen.getByRole("button", { name: /^Create and Open/ }));
    await waitFor(() => expect(onCreated).toHaveBeenCalled());
  });

  it("covers the assistant describe handoff and template compose controls", async () => {
    const user = userEvent.setup();
    const onCreated = vi.fn();
    server.use(
      http.get(`${API_BASE}/capabilities`, () =>
        HttpResponse.json(capabilitiesBody({ aria: true })),
      ),
      http.post(`${API_BASE}/assistant/prompt-draft`, () =>
        HttpResponse.json(
          envelope({
            name: "ticket-router",
            template: "Route {ticket} to the correct queue.",
            variables: ["ticket"],
          }),
        ),
      ),
    );

    renderBuilder({ onCancel: vi.fn(), onCreated });
    await user.click(await screen.findByRole("button", { name: /^Describe it/ }));
    await user.type(
      screen.getByRole("textbox", { name: "Task description" }),
      "Route support tickets to the right queue.",
    );
    await user.click(screen.getByRole("button", { name: "Draft with CALIBER" }));
    expect(await screen.findByTestId("assistant-draft-banner")).toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: /^← Change start/ }));
    await user.click(screen.getByRole("button", { name: /^Build from template/ }));
    await user.click(screen.getByRole("button", { name: /^1Start/ }));
    await screen.findByRole("button", { name: /^zs-instruct/ });
    await user.click(screen.getByRole("button", { name: /^zs-instruct/ }));
    await user.click(screen.getByRole("button", { name: /^2Compose/ }));
    expect(await screen.findByText("Add behavior")).toBeInTheDocument();

    const advanced = screen.getByText(
      "Advanced: runtime variables, preview values, manual override",
    );
    await user.click(advanced);
    await user.clear(screen.getByRole("textbox", { name: "Runtime variables" }));
    await user.type(screen.getByRole("textbox", { name: "Runtime variables" }), "customer_id");
    await user.clear(screen.getByRole("textbox", { name: "Preview variables" }));
    await user.type(screen.getByRole("textbox", { name: "Preview variables" }), "not-json");
    expect(await screen.findByText("Preview variables must be valid JSON.")).toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: /^3Save/ }));
    expect((await screen.findAllByText("Waiting")).length).toBeGreaterThan(0);
  });

  it("covers the template builder's filter, composition, override, review, and save states", async () => {
    const user = userEvent.setup();
    const onCreated = vi.fn();

    renderBuilder({ onCancel: vi.fn(), onCreated });
    await user.click(await screen.findByRole("button", { name: /^Build from template/ }));
    await screen.findByRole("button", { name: "zs-instruct" });

    // Exercise the single-select goal facet, multi-select method facet, search
    // empty state, and the clear-filters branch before restoring the catalog.
    await user.click(screen.getByRole("button", { name: "Reasoning" }));
    expect(screen.getByText("zs-cot-trigger")).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Zero Shot" }));
    await user.click(screen.getByRole("button", { name: "Reasoning" }));
    const search = screen.getByRole("textbox", { name: "Search templates" });
    await user.type(search, "no-template-matches");
    expect(screen.getByText(/No templates or recipes match/)).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Clear filters" }));
    await user.clear(search);

    await user.click(screen.getByRole("button", { name: "zs-instruct" }));
    await user.click(screen.getByRole("button", { name: "Next: Compose →" }));
    await screen.findByText("Add behavior");
    await screen.findByText("Compiled prompt");

    // Add the recommended modifier, browse the full modifier matrix, then
    // exercise ordering/removal and the incompatible modifier explanation.
    await user.click(screen.getByRole("button", { name: /Add Format Enforce/ }));
    await user.click(screen.getByText(/Browse all modifiers/));
    expect(screen.getByText("Few-Shot Examples")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /Markdown Output/ })).toBeDisabled();
    await user.click(screen.getByRole("button", { name: /^Self-Critique/ }));
    expect(screen.getByRole("list", { name: "Behavior layers" })).toBeInTheDocument();

    const selfCritique = screen.getByRole("button", { name: /Move Self-Critique earlier/ });
    await user.click(selfCritique);
    await user.click(screen.getByRole("button", { name: /Remove Self-Critique/ }));
    await user.click(screen.getByRole("button", { name: /^Self-Critique/ }));

    // Fill a required modifier field and override one composed element. This
    // moves preview from "Needs review" to a valid, saveable review state.
    await screen.findByRole("textbox", { name: "Required format" });
    await user.type(screen.getByRole("textbox", { name: "Required format" }), "JSON object");
    await user.clear(screen.getByRole("textbox", { name: "Output indicator element" }));
    await user.type(screen.getByRole("textbox", { name: "Output indicator element" }), "Return JSON only.");
    expect(await screen.findByText("overridden")).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Reset all" }));
    await user.type(screen.getByRole("textbox", { name: "Output indicator element" }), "Return JSON only.");
    await user.click(screen.getByRole("button", { name: "Reset Output indicator element" }));

    await user.click(screen.getByRole("button", { name: "Next: Review →" }));
    expect(await screen.findByText("Goal & methods")).toBeInTheDocument();
    await user.clear(screen.getByRole("combobox", { name: "Prompt goal" }));
    await user.type(screen.getByRole("combobox", { name: "Prompt goal" }), "custom-routing");
    const methods = screen.getByLabelText("Prompt methods");
    const zeroShot = within(methods).getByRole("button", { name: "Zero Shot" });
    await user.click(zeroShot);
    await user.click(screen.getByRole("textbox", { name: "Add custom method" }));
    await user.type(screen.getByRole("textbox", { name: "Add custom method" }), "operator-review");
    await user.keyboard("{Enter}");
    expect(screen.getByRole("button", { name: "Operator Review" })).toHaveAttribute("aria-pressed", "true");

    await user.clear(screen.getByRole("textbox", { name: "Prompt name" }));
    await user.type(screen.getByRole("textbox", { name: "Prompt name" }), "builder-matrix");
    await user.click(screen.getByRole("checkbox", { name: /Open Prompt Calibration/ }));
    await user.click(screen.getByRole("button", { name: /^Create( in staging)?$/ }));
    await waitFor(() => expect(onCreated).toHaveBeenCalled());
  });

  it("covers empty and failed catalogs plus failed preview compilation", async () => {
    const user = userEvent.setup();
    const onCreated = vi.fn();
    server.use(
      http.get(`${API_BASE}/prompts/template-library`, () =>
        HttpResponse.json(
          envelope({
            catalog_version: "empty",
            base_templates: [],
            modifiers: [],
            starter_recipes: [],
          }),
        ),
      ),
    );
    const emptyCatalog = renderBuilder({ onCancel: vi.fn(), onCreated });
    await user.click(screen.getByRole("button", { name: /^Build from template/ }));
    expect(await screen.findByText("No templates match the selected goal/method filters.")).toBeInTheDocument();
    expect(screen.getByText("Waiting")).toBeInTheDocument();
    emptyCatalog.unmount();

    server.use(
      http.get(`${API_BASE}/prompts/template-library`, () =>
        HttpResponse.json({ detail: "catalog unavailable" }, { status: 503 }),
      ),
    );
    const failedCatalog = renderBuilder({ onCancel: vi.fn(), onCreated });
    await user.click(screen.getByRole("button", { name: /^Build from template/ }));
    expect(await screen.findByText("catalog unavailable")).toBeInTheDocument();
    failedCatalog.unmount();

    server.resetHandlers();
    vi.restoreAllMocks();
    vi.spyOn(caliberApi, "previewPromptTemplate").mockRejectedValueOnce(
      new Error("preview unavailable"),
    );
    const failedPreview = renderBuilder({ onCancel: vi.fn(), onCreated });
    await user.click(await screen.findByRole("button", { name: /^Build from template/ }));
    await screen.findByRole("button", { name: /^zs-instruct/ });
    await user.click(screen.getByRole("button", { name: /^zs-instruct/ }));
    expect(await screen.findByText("preview unavailable")).toBeInTheDocument();
    failedPreview.unmount();
  });

  it("handles sparse catalog metadata and an invalid compiled preview", async () => {
    const user = userEvent.setup();
    const onCreated = vi.fn();
    let previewCalls = 0;
    vi.spyOn(caliberApi, "previewPromptTemplate").mockImplementation(async () => {
      previewCalls += 1;
      if (previewCalls === 1) {
        return {
          catalog_version: "sparse",
          base_template: {} as never,
          modifiers: [],
          generated_template: "valid preview",
          compiled_template: "valid preview",
          rendered_preview: "valid preview",
          validation_report: { valid: true, errors: [], warnings: [] },
          runtime_variables: [],
          builder_variables: [],
          composed_sections: {},
          overridden_sections: [],
          detected_variables: [],
          unresolved_variables: [],
          preview_variables_applied: {},
          word_count: 2,
          char_count: 13,
          recommended_scorers: [],
        } as never;
      }
      return {
        catalog_version: "sparse",
        base_template: {} as never,
        modifiers: [],
        generated_template: "",
        compiled_template: "",
        rendered_preview: "",
        validation_report: {
          valid: false,
          errors: ["Compiled output is empty."],
          warnings: [],
        },
        runtime_variables: [],
        builder_variables: [],
        composed_sections: {},
        overridden_sections: [],
        detected_variables: [],
        unresolved_variables: [],
        preview_variables_applied: {},
        word_count: 0,
        char_count: 0,
        recommended_scorers: [],
      } as never;
    });
    server.use(
      http.get(`${API_BASE}/prompts/template-library`, () =>
        HttpResponse.json(
          envelope({
            catalog_version: "sparse",
            base_templates: [
              {
                id: "bare-template",
                title: "Bare template",
                summary: "No optional facets",
                source_kind: "core",
                domain: null,
                technique: null,
                version: null,
                recommended_modifiers: [],
                variables: [],
                sections: [],
              },
            ],
            modifiers: [
              {
                id: "orphan-modifier",
                title: "Orphan modifier",
                summary: "Not compatible with the base",
                compatible_base_ids: ["different-base"],
                incompatible_modifier_ids: [],
                fields: [],
              },
            ],
            starter_recipes: [
              {
                id: "bare-recipe",
                title: "Bare recipe",
                summary: "No optional recipe metadata",
                support_level: "builder",
                base_template_id: "bare-template",
                domain: null,
                technique: null,
                modifier_ids: [],
                builder_values: {},
                runtime_variables: [],
                preview_variables: {},
                template_override: null,
                composable_with: null,
                support_reason: null,
                suggested_modifier_ids: [],
                execution_note: null,
              },
            ],
          }),
        ),
      ),
    );

    renderBuilder({ onCancel: vi.fn(), onCreated });
    await user.click(await screen.findByRole("button", { name: /^Build from template/ }));
    expect(await screen.findByRole("button", { name: "Bare template" })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Reasoning" })).not.toBeInTheDocument();
    expect(screen.queryByText("workflow-only recipes")).not.toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: "Bare template" }));
    await user.click(screen.getByRole("button", { name: "Next: Compose →" }));
    await user.click(screen.getByRole("button", { name: "Next: Review →" }));
    await user.type(screen.getByRole("textbox", { name: "Prompt name" }), "invalid-preview");
    const create = screen.getByRole("button", { name: "Create and Open" });
    await waitFor(() => expect(create).toBeEnabled());
    await user.click(create);

    expect(previewCalls).toBeGreaterThan(1);
    expect(await screen.findByText("Fix the validation errors before creating the prompt.")).toBeInTheDocument();
    expect(onCreated).not.toHaveBeenCalled();
  });

  it("surfaces describe validation and assistant failures", async () => {
    const user = userEvent.setup();
    server.use(
      http.post(`${API_BASE}/assistant/prompt-draft`, () =>
        HttpResponse.json({ detail: "assistant unavailable" }, { status: 503 }),
      ),
    );

    renderBuilder({ onCancel: vi.fn(), onCreated: vi.fn() });
    await user.click(await screen.findByRole("button", { name: /^Describe it/ }));
    await user.type(
      screen.getByRole("textbox", { name: "Task description" }),
      "Route the ticket.",
    );
    await user.click(screen.getByRole("button", { name: "Draft with CALIBER" }));
    expect(await screen.findByText("assistant unavailable")).toBeInTheDocument();
  });

  it("covers clone source loading failures and promptless version lists", async () => {
    const user = userEvent.setup();
    server.use(
      http.get(`${API_BASE}/prompts`, () =>
        HttpResponse.json({ detail: "prompt registry unavailable" }, { status: 503 }),
      ),
    );

    const failedList = renderBuilder({ onCancel: vi.fn(), onCreated: vi.fn() });
    await user.click(await screen.findByRole("button", { name: /^Start from existing/ }));
    expect(await screen.findByText("prompt registry unavailable")).toBeInTheDocument();
    failedList.unmount();

    server.use(
      http.get(`${API_BASE}/prompts`, () =>
        HttpResponse.json(
          envelope([
            {
              agent_id: "promptless-source-agent",
              agent_name: "Promptless Source",
              agent_enabled: true,
              prompt_name: "promptless-source",
              has_prompt: true,
              version: null,
              alias: "prod",
              available_aliases: [],
              template_preview: null,
              template_length: 0,
              approval_id: null,
              artifact_ref: null,
              needs_prompt: false,
              source: "caliber",
            },
          ]),
        ),
      ),
      http.get(`${API_BASE}/prompts/promptless-source/versions`, () =>
        HttpResponse.json(envelope([])),
      ),
    );
    const noVersions = renderBuilder({ onCancel: vi.fn(), onCreated: vi.fn() });
    await user.click(await screen.findByRole("button", { name: /^Start from existing/ }));
    const source = await screen.findByRole("combobox", { name: "Source prompt" });
    await user.selectOptions(source, "promptless-source");
    expect(await screen.findByText("That prompt has no versions to clone.")).toBeInTheDocument();
    noVersions.unmount();
  });

  it("handles an empty assistant draft by falling back to the description", async () => {
    const user = userEvent.setup();
    server.use(
      http.post(`${API_BASE}/assistant/prompt-draft`, () =>
        HttpResponse.json(
          envelope({ name: "", template: "", variables: [] }),
        ),
      ),
    );

    renderBuilder({ onCancel: vi.fn(), onCreated: vi.fn() });
    await user.click(await screen.findByRole("button", { name: /^Describe it/ }));
    expect(screen.getByRole("button", { name: "Draft with CALIBER" })).toBeDisabled();

    await user.type(
      screen.getByRole("textbox", { name: "Task description" }),
      "Route support tickets.",
    );
    await user.click(screen.getByRole("button", { name: "Draft with CALIBER" }));
    expect(await screen.findByTestId("assistant-draft-banner")).toBeInTheDocument();
    expect(
      await screen.findByRole("textbox", { name: "Custom prompt" }),
    ).toHaveValue("Route support tickets.");
  });

  it("reports clone version fetch failures and missing clone text", async () => {
    const user = userEvent.setup();
    server.use(
      http.get(`${API_BASE}/prompts`, () =>
        HttpResponse.json(
          envelope([
            {
              agent_id: "source-agent",
              agent_name: "Source Agent",
              has_prompt: true,
              prompt_name: "source-agent",
              version: 2,
              alias: "prod",
              available_aliases: [],
              template_preview: "source",
              template_length: 6,
              approval_id: null,
              artifact_ref: null,
              needs_prompt: false,
              source: "caliber",
            },
          ]),
        ),
      ),
      http.get(`${API_BASE}/prompts/source-agent/versions`, () =>
        HttpResponse.json(
          envelope([
            {
              name: "source-agent",
              version: 2,
              aliases: ["prod"],
              creation_timestamp: 1,
              updated_timestamp: 1,
              run_id: null,
              source: "models:/source-agent/2",
              commit_message: "source",
              current: true,
            },
          ]),
        ),
      ),
      http.get(`${API_BASE}/prompts/source-agent/versions/2`, () =>
        HttpResponse.json({ detail: "source version unavailable" }, { status: 503 }),
      ),
    );

    renderBuilder({ onCancel: vi.fn(), onCreated: vi.fn() });
    await user.click(await screen.findByRole("button", { name: /^Start from existing/ }));
    await user.selectOptions(
      await screen.findByRole("combobox", { name: "Source prompt" }),
      "source-agent",
    );
    expect(await screen.findByText("source version unavailable")).toBeInTheDocument();
  });

  it("requires pasted text and reports paste creation failures", async () => {
    const user = userEvent.setup();
    server.use(
      http.post(`${API_BASE}/prompts`, () =>
        HttpResponse.json({ detail: "prompt creation unavailable" }, { status: 503 }),
      ),
    );
    renderBuilder({ onCancel: vi.fn(), onCreated: vi.fn() });
    await user.click(await screen.findByRole("button", { name: /^Write \/ paste/ }));
    await user.type(screen.getByRole("textbox", { name: "Prompt name" }), "paste-failure");
    await user.click(screen.getByRole("button", { name: /^Create and Open/ }));
    expect(await screen.findByText("Prompt text is required.")).toBeInTheDocument();
    await user.type(screen.getByRole("textbox", { name: "Prompt text" }), "Use evidence.");
    await user.click(screen.getByRole("button", { name: /^Create and Open/ }));
    expect(await screen.findByText("prompt creation unavailable")).toBeInTheDocument();
  });
});
