import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
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
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";

import { Prompts } from "@/pages/Prompts";
import { server } from "@/test/server";

// This suite exercises the dormant multi-stage (dev/staging/prod) prompt UI:
// the deployment-alias selector and the "Promote to @prod" flow. The shipping
// single-environment default (selector hidden, "Make live") is covered in
// environment-single-env.test.tsx.
vi.mock("@/lib/environment", () => ({
  SINGLE_ENVIRONMENT: false,
  LIVE_ALIAS: "prod",
  DEPLOYMENT_ALIASES: ["dev", "staging", "prod"],
}));

const API_BASE = "/ajax-api/2.0/mlflow/caliber";

function envelope<T>(data: T): { data: T } {
  return { data };
}

function renderPrompts(): void {
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false, gcTime: 0 } },
  });
  render(
    <QueryClientProvider client={queryClient}>
      <MemoryRouter
        future={{ v7_startTransition: true, v7_relativeSplatPath: true }}
        initialEntries={["/prompts"]}
      >
        <Routes>
          <Route path="/prompts" element={<Prompts />} />
        </Routes>
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

// The Create Prompt builder is a 3-step wizard (Start → Compose → Save).
// Name / commit message / deployment alias / submit all live on the Save step.
async function gotoComposeStep(
  user: ReturnType<typeof userEvent.setup>,
): Promise<void> {
  // The builder opens on the intent fork; enter the guided wizard first.
  const buildFromTemplate = screen.queryByRole("button", {
    name: /Build from template/i,
  });
  if (buildFromTemplate) {
    await user.click(buildFromTemplate);
  }
  const next = await screen.findByRole("button", { name: /Next: Compose/i });
  await waitFor(() => expect(next).toBeEnabled());
  await user.click(next);
}
async function gotoSaveStep(
  user: ReturnType<typeof userEvent.setup>,
): Promise<void> {
  await user.click(
    await screen.findByRole("button", { name: /Next: Review/i }),
  );
}

// P3 IA: creating a prompt now opens a create-mode Workspace whose Author stage
// hosts the builder. "New prompt" replaces the old top-level "Create Prompt"
// tab; the builder content (intent fork, wizard, paste/clone/describe) is
// unchanged once open.
async function openCreate(
  user: ReturnType<typeof userEvent.setup>,
): Promise<void> {
  await user.click(await screen.findByRole("button", { name: "New prompt" }));
  await screen.findByRole("heading", { name: "Create Prompt" });
}

// P3 IA: a prompt's Playground/Calibration/Test Sets now live inside its
// Workspace. Open the (deployed) support-agent prompt, then switch to a stage.
// The stage components are locked to the open prompt, so there is no in-tab
// prompt/agent picker.
async function openWorkspaceStage(
  user: ReturnType<typeof userEvent.setup>,
  stage: "Author" | "Playground" | "Test Sets" | "Runs" | "Calibration" | "Bind",
  promptName = "Support Agent",
): Promise<void> {
  // The card title opens the Workspace (deployed prompts only).
  await user.click(await screen.findByRole("button", { name: promptName }));
  await screen.findByTestId("workspace-header");
  if (stage !== "Author") {
    await user.click(screen.getByRole("button", { name: stage }));
  }
}

beforeAll(() => server.listen({ onUnhandledRequest: "error" }));
afterEach(() => {
  server.resetHandlers();
  window.localStorage.clear();
});
afterAll(() => server.close());

describe("Prompts", () => {
  it("renders the heading and table", async () => {
    renderPrompts();
    expect(
      await screen.findByRole("heading", { name: "Prompts" }),
    ).toBeInTheDocument();
    expect(screen.getByText("Support Agent")).toBeInTheDocument();
  });

  it("shows deployed status for agents with a prompt", async () => {
    renderPrompts();
    // "Deployed" appears as both the group heading and the card badge.
    expect((await screen.findAllByText("Deployed")).length).toBeGreaterThan(0);
    expect(screen.getByText(/v3/)).toBeInTheDocument();
  });

  it("shows needs-prompt status for agents without a prompt", async () => {
    renderPrompts();
    // Promptless agents now surface in the backlog group with a "Needs prompt"
    // heading + badge instead of being dropped from the inventory.
    expect(
      (await screen.findAllByText("Needs prompt")).length,
    ).toBeGreaterThan(0);
  });

  it("prefills create form from a promptless workflow row", async () => {
    server.use(
      http.get(`${API_BASE}/prompts`, () =>
        HttpResponse.json(
          envelope([
            {
              agent_id: "wf-demo-travel-booking-triage_agent",
              agent_name: "Travel Booking Pipeline / Triage Agent",
              agent_enabled: null,
              prompt_name: "wf-demo-travel-booking-triage_agent",
              version: null,
              alias: "prod",
              available_aliases: [],
              template_preview:
                "You triage travel requests and route to specialists.",
              template_length: 52,
              approval_id: null,
              artifact_ref: null,
              has_prompt: false,
              needs_prompt: true,
              source: "mlflow",
            },
          ]),
        ),
      ),
    );

    const user = userEvent.setup();
    renderPrompts();
    await screen.findByRole("heading", { name: "Prompts" });

    // The promptless row lives in the "Needs prompt" group with a "Create
    // prompt" CTA that opens the builder prefilled with the agent's name.
    expect(
      await screen.findByTestId("prompt-group-needs-prompt"),
    ).toBeInTheDocument();
    await user.click(
      await screen.findByRole("button", { name: "Create prompt" }),
    );
    expect(
      screen.getByRole("heading", { name: "Create Prompt" }),
    ).toBeInTheDocument();

    // Name + deployment alias live on the final Save step of the wizard.
    await gotoComposeStep(user);
    await gotoSaveStep(user);

    const nameInput = (await screen.findByLabelText(
      "Prompt name",
    )) as HTMLInputElement;
    expect(nameInput.value).toBe("wf-demo-travel-booking-triage_agent");
    expect(
      screen.getByText(/safe default for testing and calibration/i),
    ).toBeInTheDocument();
  });

  it("renders summary tiles and prompt cards with source folded into the agent card", async () => {
    renderPrompts();
    await screen.findByRole("heading", { name: "Prompts" });

    expect(screen.getByTestId("prompt-tile-agents")).toHaveTextContent("2");
    expect(screen.getByTestId("prompt-tile-deployed")).toHaveTextContent("1");
    expect(screen.getByTestId("prompt-tile-draftless")).toHaveTextContent("1");
    // Deployed prompts render as PromptCards in the "Deployed" group; the
    // promptless billing-agent renders in the "Needs prompt" backlog group.
    expect(screen.getByTestId("prompt-card-support-agent")).toBeInTheDocument();
    expect(
      screen.getByTestId("needs-prompt-card-billing-agent"),
    ).toBeInTheDocument();
    expect(screen.getByTestId("prompt-group-deployed")).toBeInTheDocument();
    expect(screen.getByTestId("prompt-group-needs-prompt")).toBeInTheDocument();

    expect(screen.queryByText(/\bchars\b/)).not.toBeInTheDocument();

    // Source is still surfaced, now folded into the agent card.
    expect(screen.getByText("both")).toBeInTheDocument();
  });

  it("renders empty state when no agents exist", async () => {
    server.use(
      http.get(`${API_BASE}/prompts`, () => HttpResponse.json(envelope([]))),
    );
    renderPrompts();
    expect(
      await screen.findByText("No agents registered yet."),
    ).toBeInTheDocument();
  });

  it("shows error when API fails", async () => {
    server.use(
      http.get(`${API_BASE}/prompts`, () =>
        HttpResponse.json({ error: "boom" }, { status: 500 }),
      ),
    );
    renderPrompts();
    expect(
      await screen.findByText("Failed to load prompts"),
    ).toBeInTheDocument();
  });

  it("shows the approval ID as plain text (approval detail route removed)", async () => {
    renderPrompts();
    const node = await screen.findByText(/apr-abc1/);
    // The approval detail page was removed, so the ID is no longer a link.
    expect(node.closest("a")).toBeNull();
  });

  it("groups the inventory into Deployed and Needs prompt, and the CTA prefills the builder", async () => {
    const user = userEvent.setup();
    renderPrompts();
    await screen.findByRole("heading", { name: "Prompts" });

    // Both groups render; the promptless billing-agent sits in the backlog.
    const needsGroup = await screen.findByTestId("prompt-group-needs-prompt");
    expect(screen.getByTestId("prompt-group-deployed")).toBeInTheDocument();
    expect(
      screen.getByTestId("needs-prompt-card-billing-agent"),
    ).toBeInTheDocument();

    // The "Create prompt" CTA opens the builder prefilled with the agent name.
    await user.click(
      within(needsGroup).getByRole("button", { name: "Create prompt" }),
    );
    expect(
      screen.getByRole("heading", { name: "Create Prompt" }),
    ).toBeInTheDocument();
    await gotoComposeStep(user);
    await gotoSaveStep(user);
    const nameInput = (await screen.findByLabelText(
      "Prompt name",
    )) as HTMLInputElement;
    expect(nameInput.value).toBe("billing-agent");
  });

  it("keeps promptless placeholders in the backlog (not openable as a workspace)", async () => {
    // ``billing-agent`` is a pure placeholder (null ``prompt_name``); it lives in
    // the needs-prompt backlog and has no "Open" affordance — only ``support-agent``
    // (a real, deployed prompt) opens a workspace.
    const user = userEvent.setup();
    renderPrompts();
    await screen.findByRole("heading", { name: "Prompts" });

    // The promptless agent renders as a backlog card, not a deployed/openable one.
    expect(
      screen.getByTestId("needs-prompt-card-billing-agent"),
    ).toBeInTheDocument();
    // No "register an agent" friction copy anywhere in the inventory.
    expect(screen.queryByText(/register/i)).not.toBeInTheDocument();
    expect(screen.queryByText(/no agents/i)).not.toBeInTheDocument();

    // Opening the deployed prompt's workspace and visiting the Playground stage
    // shows no in-tab prompt picker — the prompt is fixed by the workspace.
    await openWorkspaceStage(user, "Playground");
    await screen.findByLabelText("Select model");
    expect(screen.queryByLabelText("Select a prompt")).not.toBeInTheDocument();
  });

  it("locks the calibration stage to the open prompt (no in-tab picker)", async () => {
    const user = userEvent.setup();
    renderPrompts();
    await screen.findByRole("heading", { name: "Prompts" });

    await openWorkspaceStage(user, "Calibration");
    // The calibration run config renders, scoped to the open prompt — there is
    // neither a "Select a prompt" nor a "Calibration prompt" picker.
    expect(await screen.findByText("Run Configuration")).toBeInTheDocument();
    expect(screen.queryByLabelText("Select a prompt")).not.toBeInTheDocument();
    expect(screen.queryByLabelText("Calibration prompt")).not.toBeInTheDocument();
  });

  it("clones a deployed prompt into a new variant via Start from existing", async () => {
    let createPayload: Record<string, unknown> | null = null;
    server.use(
      http.get(`${API_BASE}/prompts/support-agent/versions`, () =>
        HttpResponse.json(
          envelope([
            {
              name: "support-agent",
              version: 3,
              aliases: ["prod"],
              creation_timestamp: "2025-01-02T00:00:00Z",
              updated_timestamp: null,
              run_id: null,
              source: "mlflow",
              commit_message: "Current",
              current: true,
            },
            {
              name: "support-agent",
              version: 2,
              aliases: [],
              creation_timestamp: "2025-01-01T00:00:00Z",
              updated_timestamp: null,
              run_id: null,
              source: "mlflow",
              commit_message: "Earlier",
              current: false,
            },
          ]),
        ),
      ),
      http.get(
        `${API_BASE}/prompts/support-agent/versions/:version`,
        ({ params }) =>
          HttpResponse.json(
            envelope({
              name: "support-agent",
              version: Number(params.version),
              template: `Forked source template v${String(params.version)}`,
              template_length: 30,
              artifact_ref: `prompts:/support-agent/${String(params.version)}`,
            }),
          ),
      ),
      http.post(`${API_BASE}/prompts`, async ({ request }) => {
        createPayload = (await request.json()) as Record<string, unknown>;
        return HttpResponse.json(
          envelope({
            name: "support-agent-variant",
            version: 1,
            uri: "prompts:/support-agent-variant/1",
            template_preview: "Forked source template v3",
            template_length: 25,
            alias_changed: true,
            active_alias: "staging",
          }),
          { status: 201 },
        );
      }),
    );

    const user = userEvent.setup();
    renderPrompts();
    await screen.findByRole("heading", { name: "Prompts" });

    await openCreate(user);
    await user.click(
      screen.getByRole("button", { name: /Start from existing/i }),
    );

    // Pick the source prompt: it fetches the latest version's template, prefills
    // it, suggests a {source}-variant name, and stamps clone provenance.
    await user.selectOptions(
      await screen.findByLabelText("Source prompt"),
      "support-agent",
    );
    const templateBox = (await screen.findByLabelText(
      "Prompt text",
    )) as HTMLTextAreaElement;
    await waitFor(() =>
      expect(templateBox.value).toBe("Forked source template v3"),
    );
    expect(await screen.findByTestId("clone-provenance")).toHaveTextContent(
      /Forked from .*support-agent.* v3/i,
    );
    const nameInput = screen.getByLabelText("Prompt name") as HTMLInputElement;
    expect(nameInput.value).toBe("support-agent-variant");

    // Submit clones into the NEW name via createPrompt with the cloned content.
    await user.click(
      screen.getByRole("button", { name: /Create and Open staging/i }),
    );
    await waitFor(() =>
      expect(createPayload).toEqual(
        expect.objectContaining({
          name: "support-agent-variant",
          template: "Forked source template v3",
          target_alias: "staging",
        }),
      ),
    );
    const tags =
      (createPayload as { tags?: Record<string, string> }).tags ?? {};
    expect(tags["caliber.builder.source"]).toBe("clone");
    expect(tags["caliber.builder.forked_from"]).toBe("support-agent");
    expect(tags["caliber.builder.forked_from_version"]).toBe("3");
  });

  it("shows the inventory landing with a New prompt action and no top-level stage tabs", async () => {
    const user = userEvent.setup();
    renderPrompts();
    await screen.findByRole("heading", { name: "Prompts" });
    const inventoryLabels = screen
      .getAllByRole("button")
      .map((b) => b.textContent?.trim());
    // The landing is the inventory: a "New prompt" action, never the old
    // top-level Create/Playground/Calibration tabs (those moved into a prompt's
    // Workspace).
    expect(inventoryLabels).toContain("New prompt");
    expect(inventoryLabels).not.toContain("Create Prompt");
    expect(inventoryLabels).not.toContain("Playground");
    expect(inventoryLabels).not.toContain("Prompt Calibration");

    // Opening a prompt reveals its six Workspace stage tabs.
    await openWorkspaceStage(user, "Author");
    for (const stage of [
      "Author",
      "Playground",
      "Test Sets",
      "Runs",
      "Calibration",
      "Bind",
    ]) {
      expect(screen.getByRole("button", { name: stage })).toBeInTheDocument();
    }
  });

  it("validates, creates, and cancels prompt creation", async () => {
    let createPayload: Record<string, unknown> | null = null;
    server.use(
      http.post(`${API_BASE}/prompts`, async ({ request }) => {
        createPayload = (await request.json()) as Record<string, unknown>;
        return HttpResponse.json(
          envelope({
            name: "new-support-agent",
            version: 1,
            uri: "prompts:/new-support-agent/1",
            template_preview: "You are helpful.",
            template_length: 16,
            alias_changed: true,
            active_alias: "staging",
          }),
          { status: 201 },
        );
      }),
    );

    const user = userEvent.setup();
    renderPrompts();
    await screen.findByRole("heading", { name: "Prompts" });

    await openCreate(user);
    await user.click(screen.getByRole("button", { name: /Build from template/i }));
    await user.click(
      screen.getByRole("button", { name: /^Grounded Answer/i }),
    );
    await gotoComposeStep(user);
    await user.type(
      await screen.findByLabelText(/Answering goal/i),
      "Help answer support questions.",
    );
    await gotoSaveStep(user);

    await waitFor(() =>
      expect(
        screen.getByRole("button", { name: /Create and Open staging/i }),
      ).toBeEnabled(),
    );
    await user.click(
      screen.getByRole("button", { name: /Create and Open staging/i }),
    );
    expect(
      await screen.findByText("Prompt name is required."),
    ).toBeInTheDocument();

    await user.type(screen.getByLabelText(/Prompt name/i), "new-support-agent");
    await user.type(
      screen.getByLabelText(/Commit message/i),
      "Initial release",
    );
    await user.click(
      screen.getByRole("button", { name: /Create and Open staging/i }),
    );

    await waitFor(() =>
      expect(createPayload).toEqual(
        expect.objectContaining({
          name: "new-support-agent",
          template: expect.stringContaining("Help answer support questions."),
          commit_message: "Initial release",
          target_alias: "staging",
        }),
      ),
    );
    // After create, the page flips into the saved prompt's Workspace (Author
    // stage) — the create surface is gone and the status header is shown.
    expect(await screen.findByTestId("workspace-header")).toBeInTheDocument();
    expect(
      screen.queryByRole("heading", { name: "Create Prompt" }),
    ).not.toBeInTheDocument();

    // "← Back to prompts" returns to the inventory landing.
    await user.click(screen.getByRole("button", { name: /Back to prompts/i }));
    expect(
      await screen.findByRole("button", { name: "New prompt" }),
    ).toBeInTheDocument();
  });

  it("registers an existing prompt via the Write/paste fast path", async () => {
    let createPayload: Record<string, unknown> | null = null;
    server.use(
      http.post(`${API_BASE}/prompts`, async ({ request }) => {
        createPayload = (await request.json()) as Record<string, unknown>;
        return HttpResponse.json(
          envelope({
            name: "pasted-agent",
            version: 1,
            uri: "prompts:/pasted-agent/1",
            template_preview: "You are a pasted prompt.",
            template_length: 24,
            alias_changed: true,
            active_alias: "staging",
          }),
          { status: 201 },
        );
      }),
    );

    const user = userEvent.setup();
    renderPrompts();
    await screen.findByRole("heading", { name: "Prompts" });

    await openCreate(user);
    // The fast path skips templates entirely — paste, name, ship.
    await user.click(screen.getByRole("button", { name: /Write \/ paste/i }));

    await user.type(
      await screen.findByLabelText("Prompt text"),
      // userEvent treats "{" as a special-key delimiter — double it for a literal.
      "You are a helpful assistant. Answer {{user_input}.",
    );
    // Detected runtime placeholders are surfaced as a hint.
    expect(screen.getByText("{user_input}")).toBeInTheDocument();

    await user.type(screen.getByLabelText("Prompt name"), "pasted-agent");
    await user.click(
      screen.getByRole("button", { name: /Create and Open staging/i }),
    );

    await waitFor(() =>
      expect(createPayload).toEqual(
        expect.objectContaining({
          name: "pasted-agent",
          template: "You are a helpful assistant. Answer {user_input}.",
          target_alias: "staging",
        }),
      ),
    );
  });

  it("prefills extraction templates with starter examples instead of raw required-field errors", async () => {
    const user = userEvent.setup();
    renderPrompts();
    await screen.findByRole("heading", { name: "Prompts" });

    await openCreate(user);
    await user.click(screen.getByRole("button", { name: /Build from template/i }));
    await user.click(
      screen.getByRole("button", { name: /Extract Structured Data/i }),
    );
    await gotoComposeStep(user);

    const extractionGoal = (await screen.findByLabelText(
      /Extraction goal/i,
    )) as HTMLTextAreaElement;
    const targetSchema = screen.getByLabelText(
      /Target schema/i,
    ) as HTMLTextAreaElement;

    expect(extractionGoal.value).toContain(
      "downstream workflow can ingest the record",
    );
    expect(targetSchema.value).toContain('"invoice_number": "string | null"');
    expect(
      screen.getAllByText(/Starter example loaded/i).length,
    ).toBeGreaterThan(0);
    expect(
      screen.queryByText(/Builder field 'task_description' is required\./i),
    ).not.toBeInTheDocument();
    expect(
      screen.queryByText(/Builder field 'schema' is required\./i),
    ).not.toBeInTheDocument();
  });

  it("supports starting from the custom prompt template", async () => {
    let createPayload: Record<string, unknown> | null = null;
    server.use(
      http.post(`${API_BASE}/prompts`, async ({ request }) => {
        createPayload = (await request.json()) as Record<string, unknown>;
        return HttpResponse.json(
          envelope({
            name: "custom-billing-agent",
            version: 1,
            uri: "prompts:/custom-billing-agent/1",
            template_preview: "You are a billing assistant.",
            template_length: 29,
            alias_changed: true,
            active_alias: "staging",
          }),
          { status: 201 },
        );
      }),
    );

    const user = userEvent.setup();
    renderPrompts();
    await screen.findByRole("heading", { name: "Prompts" });

    await openCreate(user);
    await user.click(screen.getByRole("button", { name: /Build from template/i }));
    await user.click(screen.getByRole("button", { name: "Custom Prompt" }));
    await gotoComposeStep(user);

    const customPrompt = await screen.findByLabelText(/Custom prompt/i);
    await user.clear(customPrompt);
    await user.type(
      customPrompt,
      "You are a billing assistant.\n\nExplain payment status clearly and ask for missing invoice details before refund actions.",
    );

    await gotoSaveStep(user);
    await user.type(
      screen.getByLabelText(/Prompt name/i),
      "custom-billing-agent",
    );
    await waitFor(() =>
      expect(
        screen.getByRole("button", { name: /Create and Open staging/i }),
      ).toBeEnabled(),
    );
    await user.click(
      screen.getByRole("button", { name: /Create and Open staging/i }),
    );

    await waitFor(() =>
      expect(createPayload).toEqual(
        expect.objectContaining({
          name: "custom-billing-agent",
          template: expect.stringContaining("You are a billing assistant."),
          target_alias: "staging",
        }),
      ),
    );
  });

  it("shows library templates directly from template_library.json", async () => {
    const user = userEvent.setup();
    renderPrompts();
    await screen.findByRole("heading", { name: "Prompts" });

    await openCreate(user);
    await user.click(screen.getByRole("button", { name: /Build from template/i }));
    expect(screen.getByText(/Library Quick Starts/i)).toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: /rag-grounded-qa/i }));
    await gotoComposeStep(user);

    expect(screen.getByText(/Loaded from library template/i)).toBeInTheDocument();
    expect(screen.getByText(/Suggested Fusions/i)).toBeInTheDocument();
    expect(screen.getByText(/Library Composition Hooks/i)).toBeInTheDocument();
    expect(
      screen.getByRole("button", { name: /Add Format Enforce/i }),
    ).toBeInTheDocument();
    expect(screen.queryByLabelText(/Answering goal/i)).not.toBeInTheDocument();

    await gotoSaveStep(user);
    expect(screen.queryByText(/Fields still to fill/i)).not.toBeInTheDocument();
  });

  it("keeps workflow-shaped patterns as real library templates", async () => {
    const user = userEvent.setup();
    renderPrompts();
    await screen.findByRole("heading", { name: "Prompts" });

    await openCreate(user);
    await user.click(screen.getByRole("button", { name: /Build from template/i }));
    await user.click(screen.getByRole("button", { name: /react-tool-loop/i }));
    await gotoComposeStep(user);

    expect(screen.getByText(/Loaded from library template/i)).toBeInTheDocument();
    expect(screen.getByText(/Library Composition Hooks/i)).toBeInTheDocument();
    expect(screen.getByText(/reflexion-retry/i)).toBeInTheDocument();
  });

  it("narrows the gallery by goal and method facets", async () => {
    const user = userEvent.setup();
    renderPrompts();
    await screen.findByRole("heading", { name: "Prompts" });

    await openCreate(user);
    await user.click(screen.getByRole("button", { name: /Build from template/i }));

    // Both library quick starts are visible before filtering.
    expect(
      screen.getByRole("button", { name: /rag-grounded-qa/i }),
    ).toBeInTheDocument();
    expect(
      screen.getByRole("button", { name: /zs-cot-trigger/i }),
    ).toBeInTheDocument();

    const methodRow = screen.getByLabelText("Filter by method");
    await user.click(within(methodRow).getByRole("button", { name: "Rag" }));

    // The RAG template stays; the zero-shot-cot reasoning template drops out.
    expect(
      screen.getByRole("button", { name: /rag-grounded-qa/i }),
    ).toBeInTheDocument();
    expect(
      screen.queryByRole("button", { name: /zs-cot-trigger/i }),
    ).not.toBeInTheDocument();
  });

  it("supports selecting multiple methods (match any of the selected)", async () => {
    const user = userEvent.setup();
    renderPrompts();
    await screen.findByRole("heading", { name: "Prompts" });

    await openCreate(user);
    await user.click(screen.getByRole("button", { name: /Build from template/i }));

    const methodRow = screen.getByLabelText("Filter by method");
    // Methods are multi-select: pick two distinct techniques.
    await user.click(within(methodRow).getByRole("button", { name: "Rag" }));
    await user.click(
      within(methodRow).getByRole("button", { name: "Zero Shot Cot" }),
    );

    // Both selected method families remain; an unselected one is filtered out.
    expect(
      screen.getByRole("button", { name: /rag-grounded-qa/i }),
    ).toBeInTheDocument();
    expect(
      screen.getByRole("button", { name: /zs-cot-trigger/i }),
    ).toBeInTheDocument();
    expect(
      screen.queryByRole("button", { name: /react-tool-loop/i }),
    ).not.toBeInTheDocument();
  });

  it("tags the saved prompt with a custom goal and multiple methods", async () => {
    let createPayload: Record<string, unknown> | null = null;
    server.use(
      http.post(`${API_BASE}/prompts`, async ({ request }) => {
        createPayload = (await request.json()) as Record<string, unknown>;
        return HttpResponse.json(
          envelope({
            name: "triage-bot",
            version: 1,
            uri: "prompts:/triage-bot/1",
            template_preview: "…",
            template_length: 3,
            alias_changed: true,
            active_alias: "staging",
          }),
          { status: 201 },
        );
      }),
    );

    const user = userEvent.setup();
    renderPrompts();
    await screen.findByRole("heading", { name: "Prompts" });

    await openCreate(user);
    await user.click(screen.getByRole("button", { name: /Build from template/i }));
    await user.click(screen.getByRole("button", { name: /rag-grounded-qa/i }));
    await gotoComposeStep(user);
    await gotoSaveStep(user);

    // Goal prefills from the base template's domain, then takes a custom value.
    const goalInput = (await screen.findByLabelText(
      "Prompt goal",
    )) as HTMLInputElement;
    expect(goalInput.value).toBe("question-answering");
    await user.clear(goalInput);
    await user.type(goalInput, "customer-triage");

    // The base technique is pre-tagged; add a second, custom method.
    const methods = screen.getByLabelText("Prompt methods");
    expect(within(methods).getByRole("button", { name: "Rag" })).toHaveAttribute(
      "aria-pressed",
      "true",
    );
    await user.type(screen.getByLabelText("Add custom method"), "agentic");
    await user.click(screen.getByRole("button", { name: "Add" }));

    // The changeset reflects the custom classification.
    const lineage = screen.getByRole("region", { name: "Lineage" });
    expect(
      within(lineage).getByText(/Goal: customer-triage/i),
    ).toBeInTheDocument();
    expect(within(lineage).getByText(/Methods: rag, agentic/i)).toBeInTheDocument();

    await user.type(screen.getByLabelText(/Prompt name/i), "triage-bot");
    await waitFor(() =>
      expect(
        screen.getByRole("button", { name: /Create and Open staging/i }),
      ).toBeEnabled(),
    );
    await user.click(
      screen.getByRole("button", { name: /Create and Open staging/i }),
    );

    await waitFor(() => expect(createPayload).not.toBeNull());
    const tags = (createPayload as { tags?: Record<string, string> }).tags ?? {};
    expect(tags["caliber.builder.goal"]).toBe("customer-triage");
    expect(tags["caliber.builder.methods"]).toBe("rag,agentic");
  });

  it("overrides a single prompt element and resets it", async () => {
    const user = userEvent.setup();
    renderPrompts();
    await screen.findByRole("heading", { name: "Prompts" });

    await openCreate(user);
    await user.click(screen.getByRole("button", { name: /Build from template/i }));
    await user.click(screen.getByRole("button", { name: /rag-grounded-qa/i }));
    await gotoComposeStep(user);

    const instruction = (await screen.findByLabelText(
      "Instruction element",
    )) as HTMLTextAreaElement;
    expect(instruction.value).toContain("Answer the question using ONLY");
    // Not overridden yet → no Reset affordance.
    expect(
      screen.queryByRole("button", { name: /Reset Instruction element/i }),
    ).not.toBeInTheDocument();

    await user.clear(instruction);
    await user.type(instruction, "Reply only with the answer.");

    // Editing flips the element to overridden and the compiled prompt updates.
    const reset = await screen.findByRole("button", {
      name: /Reset Instruction element/i,
    });
    await waitFor(() =>
      expect(
        screen.getAllByText(/Reply only with the answer\./).length,
      ).toBeGreaterThan(0),
    );

    await user.click(reset);
    await waitFor(() =>
      expect(
        (screen.getByLabelText("Instruction element") as HTMLTextAreaElement)
          .value,
      ).toContain("Answer the question using ONLY"),
    );
    expect(
      screen.queryByRole("button", { name: /Reset Instruction element/i }),
    ).not.toBeInTheDocument();
  });

  it("layers, reorders, and removes behaviors as an ordered stack", async () => {
    const user = userEvent.setup();
    renderPrompts();
    await screen.findByRole("heading", { name: "Prompts" });

    await openCreate(user);
    await user.click(screen.getByRole("button", { name: /Build from template/i }));
    await user.click(screen.getByRole("button", { name: /rag-grounded-qa/i }));
    await gotoComposeStep(user);

    await user.click(screen.getByRole("button", { name: /Add Self-Critique/i }));
    await user.click(screen.getByRole("button", { name: /Add Format Enforce/i }));

    let items = within(
      screen.getByRole("list", { name: "Behavior layers" }),
    ).getAllByRole("listitem");
    expect(items).toHaveLength(2);
    expect(items[0]!).toHaveTextContent("Self-Critique");
    expect(items[1]!).toHaveTextContent("Format Enforce");

    // Move the first layer down — order drives the backend's append order.
    await user.click(
      within(items[0]!).getByRole("button", { name: /Move Self-Critique later/i }),
    );
    items = within(
      screen.getByRole("list", { name: "Behavior layers" }),
    ).getAllByRole("listitem");
    expect(items[0]!).toHaveTextContent("Format Enforce");
    expect(items[1]!).toHaveTextContent("Self-Critique");

    // Remove a layer.
    await user.click(
      within(items[0]!).getByRole("button", { name: /Remove Format Enforce/i }),
    );
    items = within(
      screen.getByRole("list", { name: "Behavior layers" }),
    ).getAllByRole("listitem");
    expect(items).toHaveLength(1);
    expect(items[0]!).toHaveTextContent("Self-Critique");
  });

  it("summarizes lineage and the changeset on the save step", async () => {
    const user = userEvent.setup();
    renderPrompts();
    await screen.findByRole("heading", { name: "Prompts" });

    await openCreate(user);
    await user.click(screen.getByRole("button", { name: /Build from template/i }));
    await user.click(screen.getByRole("button", { name: /rag-grounded-qa/i }));
    await gotoComposeStep(user);

    await user.click(screen.getByRole("button", { name: /Add Self-Critique/i }));
    const instruction = (await screen.findByLabelText(
      "Instruction element",
    )) as HTMLTextAreaElement;
    await user.clear(instruction);
    await user.type(instruction, "Answer tersely.");

    await gotoSaveStep(user);

    const lineage = screen.getByRole("region", { name: "Lineage" });
    expect(within(lineage).getByText(/Derived from/i)).toBeInTheDocument();
    expect(within(lineage).getByText("rag-grounded-qa")).toBeInTheDocument();
    expect(
      within(lineage).getByText(/Layered behavior: Self-Critique/i),
    ).toBeInTheDocument();
    expect(
      within(lineage).getByText(/Instruction overridden/i),
    ).toBeInTheDocument();
  });

  it("drafts a prompt from a description and hands off to the manual builder", async () => {
    let draftBody: Record<string, unknown> | null = null;
    server.use(
      http.post(`${API_BASE}/assistant/prompt-draft`, async ({ request }) => {
        draftBody = (await request.json()) as Record<string, unknown>;
        return HttpResponse.json(
          envelope({
            reply: "Here is a starting prompt.",
            name: "ticket-classifier",
            template:
              "Classify the support ticket into billing or technical:\n\n{{ticket}}",
            variables: ["ticket"],
            summary: "",
          }),
        );
      }),
    );

    const user = userEvent.setup();
    renderPrompts();
    await screen.findByRole("heading", { name: "Prompts" });

    await openCreate(user);
    await user.click(screen.getByRole("button", { name: /Describe it/i }));

    await user.type(
      screen.getByLabelText("Task description"),
      "Classify support tickets as billing or technical.",
    );
    await user.click(screen.getByRole("button", { name: /Draft with CALIBER/i }));

    // The assistant was asked to draft from the description.
    await waitFor(() =>
      expect(draftBody).toEqual(
        expect.objectContaining({
          description: "Classify support tickets as billing or technical.",
        }),
      ),
    );

    // It lands in the MANUAL builder (Compose) with provenance + the drafted text.
    expect(
      await screen.findByTestId("assistant-draft-banner"),
    ).toBeInTheDocument();
    const customPrompt = (await screen.findByLabelText(
      /Custom prompt/i,
    )) as HTMLTextAreaElement;
    expect(customPrompt.value).toContain(
      "Classify the support ticket into billing or technical",
    );

    // The same manual validate -> save flow applies; the drafted name prefills.
    await gotoSaveStep(user);
    const nameField = screen.getByLabelText(/Prompt name/i) as HTMLInputElement;
    expect(nameField.value).toBe("ticket-classifier");
  });

  it("opens version history, compares templates, promotes a version, and closes the panel", async () => {
    const versionRequests: Array<number> = [];
    let promoted: Record<string, unknown> | null = null;
    server.use(
      http.get(`${API_BASE}/prompts/support-agent/versions`, () =>
        HttpResponse.json(
          envelope([
            {
              name: "support-agent",
              version: 4,
              aliases: ["staging"],
              creation_timestamp: "2025-01-02T00:00:00Z",
              updated_timestamp: null,
              run_id: null,
              source: "mlflow",
              commit_message: "Try concise tone",
              current: false,
            },
            {
              name: "support-agent",
              version: 3,
              aliases: ["prod"],
              creation_timestamp: "2025-01-01T00:00:00Z",
              updated_timestamp: null,
              run_id: null,
              source: "mlflow",
              commit_message: "Current prod",
              current: true,
            },
          ]),
        ),
      ),
      http.get(
        `${API_BASE}/prompts/support-agent/versions/:version`,
        ({ params }) => {
          const version = Number(params.version);
          versionRequests.push(version);
          return HttpResponse.json(
            envelope({
              name: "support-agent",
              version,
              template:
                version === 4 ? "Support prompt v4" : "Support prompt v3",
              template_length: 17,
              artifact_ref: `prompts:/support-agent/${version}`,
            }),
          );
        },
      ),
      http.post(
        `${API_BASE}/prompts/support-agent/aliases/prod`,
        async ({ request }) => {
          promoted = (await request.json()) as Record<string, unknown>;
          return HttpResponse.json(
            envelope({
              name: "support-agent",
              alias: "prod",
              version: promoted.version,
            }),
          );
        },
      ),
    );

    const user = userEvent.setup();
    renderPrompts();
    await screen.findByRole("heading", { name: "Prompts" });

    await user.click(
      (await screen.findAllByRole("button", { name: "Versions" }))[0]!,
    );
    expect(
      await screen.findByText("Versions: Support Agent"),
    ).toBeInTheDocument();
    expect(await screen.findByText("Try concise tone")).toBeInTheDocument();
    expect(
      await screen.findByText("Selected versions differ."),
    ).toBeInTheDocument();

    await user.selectOptions(
      screen.getByLabelText("Compare right version"),
      "4",
    );
    await user.click(screen.getByRole("button", { name: "Compare" }));
    expect(
      await screen.findByText("Selected versions are text-identical."),
    ).toBeInTheDocument();
    expect(versionRequests).toContain(4);

    await user.click(screen.getByRole("button", { name: "Promote to @prod" }));
    await waitFor(() => expect(promoted).toEqual({ version: 4 }));

    await user.click(screen.getByRole("button", { name: "Close" }));
    expect(
      screen.queryByText("Versions: Support Agent"),
    ).not.toBeInTheDocument();
  });

  it("surfaces prompt version loading, comparison, and promotion errors", async () => {
    let failVersions = true;
    let failCompare = false;
    server.use(
      http.get(`${API_BASE}/prompts/support-agent/versions`, () => {
        if (failVersions) {
          return HttpResponse.json(
            { detail: "version list failed" },
            { status: 500 },
          );
        }
        return HttpResponse.json(
          envelope([
            {
              name: "support-agent",
              version: 4,
              aliases: [],
              creation_timestamp: null,
              updated_timestamp: null,
              run_id: null,
              source: "mlflow",
              commit_message: null,
              current: false,
            },
            {
              name: "support-agent",
              version: 3,
              aliases: ["prod"],
              creation_timestamp: null,
              updated_timestamp: null,
              run_id: null,
              source: "mlflow",
              commit_message: null,
              current: true,
            },
          ]),
        );
      }),
      http.get(
        `${API_BASE}/prompts/support-agent/versions/:version`,
        ({ params }) => {
          if (failCompare && String(params.version) === "4") {
            return HttpResponse.json(
              { detail: "compare failed" },
              { status: 500 },
            );
          }
          return HttpResponse.json(
            envelope({
              name: "support-agent",
              version: Number(params.version),
              template: `Prompt ${String(params.version)}`,
              template_length: 8,
              artifact_ref: `prompts:/support-agent/${String(params.version)}`,
            }),
          );
        },
      ),
      http.post(`${API_BASE}/prompts/support-agent/aliases/prod`, () =>
        HttpResponse.json({ detail: "promotion failed" }, { status: 500 }),
      ),
    );

    const user = userEvent.setup();
    renderPrompts();
    await screen.findByRole("heading", { name: "Prompts" });

    await user.click(
      (await screen.findAllByRole("button", { name: "Versions" }))[0]!,
    );
    expect(await screen.findByText("version list failed")).toBeInTheDocument();

    failVersions = false;
    await user.click(screen.getByRole("button", { name: "Close" }));
    await user.click(
      (await screen.findAllByRole("button", { name: "Versions" }))[0]!,
    );
    expect((await screen.findAllByText("v4")).length).toBeGreaterThan(0);

    failCompare = true;
    await user.selectOptions(
      screen.getByLabelText("Compare left version"),
      "4",
    );
    await user.click(screen.getByRole("button", { name: "Compare" }));
    expect(await screen.findByText("compare failed")).toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: "Promote to @prod" }));
    expect(await screen.findByText("promotion failed")).toBeInTheDocument();
  });

  it("starts a prompt calibration run from the calibration tab", async () => {
    let runPayload: Record<string, unknown> | null = null;

    server.use(
      http.get(`${API_BASE}/prompts/calibration/options`, () =>
        HttpResponse.json(
          envelope({
            optimizers: ["MetaPrompt", "MIPROv2"],
            default_optimizer: "MetaPrompt",
            scorers: [
              {
                name: "helpfulness",
                label: "Helpfulness",
                description: "Rates whether the response is helpful.",
                requires_config: false,
                provider: "mlflow",
                category: "core",
                available: true,
                unavailable_reason: null,
                install_command: null,
                config_template: null,
              },
              {
                name: "DeepEval.Faithfulness",
                label: "Faithfulness",
                description:
                  "DeepEval metric for factual faithfulness to supplied context.",
                requires_config: false,
                provider: "deepeval",
                category: "deepeval_beta",
                available: false,
                unavailable_reason:
                  "deepeval is not installed in this environment",
                install_command: "pip install -U deepeval",
                config_template: null,
              },
            ],
            default_scorers: ["helpfulness"],
            default_gate: {
              min_aggregate_score: 0.85,
              max_regression_delta: 0.02,
            },
            runtime: {
              deepeval: {
                available: false,
                package: "deepeval",
                install_policy: "latest",
                install_command: "pip install -U deepeval",
                reason: "deepeval is not installed in this environment",
              },
            },
          }),
        ),
      ),
      http.get(`${API_BASE}/eval-datasets`, () =>
        HttpResponse.json(
          envelope([
            {
              dataset_id: "eds-opt-1",
              name: "Prompt Calibration Dataset",
              description: "Dataset for calibration tab tests",
              owner: "@test",
              tags: ["prompt-calibration"],
              status: "active",
              version: 1,
              created_at: "2025-01-01T00:00:00Z",
              updated_at: "2025-01-01T00:00:00Z",
            },
          ]),
        ),
      ),
      http.get(`${API_BASE}/jobs`, () => HttpResponse.json(envelope([]))),
      http.post(`${API_BASE}/prompts/calibration/runs`, async ({ request }) => {
        runPayload = (await request.json()) as Record<string, unknown>;
        return HttpResponse.json(
          envelope({
            item: {
              item_id: "item-opt-001",
              agent_id: "support-agent",
              assessment_id: null,
              trace_id: null,
              experiment_id: null,
              session_id: null,
              workflow_id: null,
              category: "prompt_optimization",
              free_text: "Prompt calibration run",
              severity: "standard",
              artifact_type_hint: "prompt",
              artifact_ref: "prompts:/support-agent@prod",
              submitted_context: {},
              status: "verified",
              priority: 100,
              assigned_to: null,
              verified_by: "@test",
              verified_at: "2025-01-01T00:00:00Z",
              verification_notes: null,
              refinement_target: "prompt",
              duplicate_of_id: null,
              created_at: "2025-01-01T00:00:00Z",
            },
            job: {
              job_id: "opt-job-001",
              agent_id: "support-agent",
              workflow_id: null,
              primary_item_id: "item-opt-001",
              mlflow_run_id: null,
              artifact_type: "prompt",
              optimizer_type: "MetaPrompt",
              status: "queued",
              current_stage: "triage",
              attempt_count: 0,
              error_message: null,
              total_tokens: 0,
              cost_usd: 0,
              bundle_targets: [],
              bundle_expansion_count: 1,
              diagnosis: null,
              candidate: null,
              eval_results: null,
              created_at: "2025-01-01T00:00:00Z",
              updated_at: "2025-01-01T00:00:00Z",
            },
          }),
          { status: 201 },
        );
      }),
      http.get(`${API_BASE}/jobs/:jobId`, ({ params }) =>
        HttpResponse.json(
          envelope({
            job_id: String(params.jobId),
            agent_id: "support-agent",
            workflow_id: null,
            primary_item_id: "item-opt-001",
            mlflow_run_id: null,
            artifact_type: "prompt",
            optimizer_type: "MetaPrompt",
            status: "queued",
            current_stage: "triage",
            attempt_count: 0,
            error_message: null,
            total_tokens: 0,
            cost_usd: 0,
            bundle_targets: [],
            bundle_expansion_count: 1,
            diagnosis: null,
            candidate: null,
            eval_results: null,
            created_at: "2025-01-01T00:00:00Z",
            updated_at: "2025-01-01T00:00:00Z",
          }),
        ),
      ),
    );

    const user = userEvent.setup();
    renderPrompts();
    await screen.findByRole("heading", { name: "Prompts" });

    await openWorkspaceStage(user, "Calibration");
    expect(await screen.findByText("Run Configuration")).toBeInTheDocument();
    expect(
      screen.getByText(/DeepEval runtime: Not installed/),
    ).toBeInTheDocument();
    expect(
      screen.getAllByText(/pip install -U deepeval/).length,
    ).toBeGreaterThan(0);
    expect(
      screen.getByRole("checkbox", { name: "Faithfulness" }),
    ).toBeDisabled();

    await user.type(
      screen.getByLabelText("Calibration run notes"),
      "Investigate support prompt regressions",
    );
    await user.click(
      screen.getByRole("button", { name: "Start Calibration Run" }),
    );

    expect(runPayload).not.toBeNull();
    if (runPayload === null) {
      throw new Error("Expected calibration run payload to be captured");
    }
    const payload = runPayload as {
      agent_id: string;
      prompt_alias?: string;
      eval_dataset_id: string;
      optimizer_type: string;
      notes?: string;
      scorers: Array<{ name: string; weight: number }>;
    };
    expect(payload.agent_id).toBe("support-agent");
    expect(payload.prompt_alias).toBe("prod");
    expect(payload.eval_dataset_id).toBe("eds-opt-1");
    expect(payload.optimizer_type).toBe("MetaPrompt");
    expect(payload.notes).toBe("Investigate support prompt regressions");
    expect(payload.scorers).toHaveLength(1);
    expect(payload.scorers[0]?.name).toBe("helpfulness");

    expect(await screen.findByText("opt-job-001")).toBeInTheDocument();
    // Prompt run status renders inline — there is no "Open job details"
    // link, and a freshly queued run is not candidate_ready so no Apply shows.
    expect(screen.queryByRole("link", { name: "Open job details" })).toBeNull();
    expect(screen.queryByTestId("job-apply-btn")).not.toBeInTheDocument();
    expect(screen.getByText("Run Provenance")).toBeInTheDocument();
    expect(screen.getByText("prompts:/support-agent@prod")).toBeInTheDocument();
    expect(screen.getByText("helpfulness (1)")).toBeInTheDocument();
    expect(screen.getByText(/min=0.85 \/ regression=0.02/)).toBeInTheDocument();
    const provenance = screen.getByText("Run Provenance").parentElement;
    expect(provenance).not.toBeNull();
    if (provenance === null) {
      throw new Error("Expected run provenance panel");
    }
    expect(
      within(provenance).getByText("Investigate support prompt regressions"),
    ).toBeInTheDocument();
  });

  it("supports assistant-guided resolve, plan, and execute for calibration", async () => {
    let resolvePayload: Record<string, unknown> | null = null;
    let planPayload: Record<string, unknown> | null = null;
    let executePayload: Record<string, unknown> | null = null;

    server.use(
      http.get(`${API_BASE}/prompts/calibration/options`, () =>
        HttpResponse.json(
          envelope({
            optimizers: ["MetaPrompt", "MIPROv2"],
            default_optimizer: "MetaPrompt",
            scorers: [
              {
                name: "helpfulness",
                label: "Helpfulness",
                description: "Rates whether the response is helpful.",
                requires_config: false,
                provider: "mlflow",
                category: "core",
                available: true,
                unavailable_reason: null,
                install_command: null,
                config_template: null,
              },
            ],
            default_scorers: ["helpfulness"],
            default_gate: {
              min_aggregate_score: 0.85,
              max_regression_delta: 0.02,
            },
          }),
        ),
      ),
      http.get(`${API_BASE}/eval-datasets`, () =>
        HttpResponse.json(
          envelope([
            {
              dataset_id: "eds-opt-assistant",
              name: "Assistant Calibration Dataset",
              description: "Dataset for assistant-guided calibration tests",
              owner: "@test",
              tags: ["prompt-calibration"],
              status: "active",
              version: 1,
              created_at: "2025-01-01T00:00:00Z",
              updated_at: "2025-01-01T00:00:00Z",
            },
          ]),
        ),
      ),
      http.get(`${API_BASE}/jobs`, () => HttpResponse.json(envelope([]))),
      http.post(`${API_BASE}/assistant/sessions`, () =>
        HttpResponse.json(
          envelope({
            session_id: "asst-opt-001",
            title: "Prompt calibration workbench",
            goal: "Intent planning",
            status: "active",
            metadata_: {},
            created_at: "2025-01-01T00:00:00Z",
            updated_at: "2025-01-01T00:00:00Z",
          }),
          { status: 201 },
        ),
      ),
      http.get(`${API_BASE}/assistant/sessions/:sessionId/plans/latest`, () =>
        HttpResponse.json({ error: "not found" }, { status: 404 }),
      ),
      http.post(
        `${API_BASE}/assistant/sessions/:sessionId/intent/resolve`,
        async ({ request }) => {
          resolvePayload = (await request.json()) as Record<string, unknown>;
          return HttpResponse.json(
            envelope({
              mode: "intent_plan",
              intent: {
                name: "run_prompt_optimization",
                confidence: 0.96,
                rationale:
                  "User asks to calibrate prompt with explicit metrics and dataset.",
              },
              alternatives: [],
              slots: [
                {
                  name: "agent_id",
                  value: "support-agent",
                  required: true,
                  source: "inferred",
                  confidence: 0.9,
                  needs_confirmation: false,
                },
              ],
              assumptions: ["Use existing prompt alias @prod."],
              questions: [],
              evidence: ["Calibrate", "dataset", "scorers"],
            }),
          );
        },
      ),
      http.post(
        `${API_BASE}/assistant/sessions/:sessionId/plans`,
        async ({ request }) => {
          planPayload = (await request.json()) as Record<string, unknown>;
          return HttpResponse.json(
            envelope({
              mode: "intent_plan",
              plan_id: "plan-opt-001",
              intent: {
                name: "run_prompt_optimization",
                confidence: 0.98,
                rationale: "All required calibration slots are present.",
              },
              actions: [
                {
                  action: "run_prompt_optimization",
                  description:
                    "Create calibration verification item and queue job.",
                  status: "ready",
                },
              ],
              slots: [
                {
                  name: "agent_id",
                  value: "support-agent",
                  required: true,
                  source: "inferred",
                  confidence: 0.96,
                  needs_confirmation: false,
                },
                {
                  name: "eval_dataset_id",
                  value: "eds-opt-assistant",
                  required: true,
                  source: "inferred",
                  confidence: 0.94,
                  needs_confirmation: false,
                },
                {
                  name: "optimizer_type",
                  value: "MIPROv2",
                  required: true,
                  source: "default",
                  confidence: 0.92,
                  needs_confirmation: false,
                },
                {
                  name: "scorers",
                  value: [{ name: "helpfulness", weight: 2 }],
                  required: true,
                  source: "inferred",
                  confidence: 0.88,
                  needs_confirmation: false,
                },
                {
                  name: "gate.min_aggregate_score",
                  value: 0.91,
                  required: true,
                  source: "inferred",
                  confidence: 0.86,
                  needs_confirmation: false,
                },
                {
                  name: "gate.max_regression_delta",
                  value: 0.01,
                  required: true,
                  source: "inferred",
                  confidence: 0.85,
                  needs_confirmation: false,
                },
              ],
              missing_slots: [],
              assumptions: ["Use default prompt alias @prod"],
              questions: [],
              ready: true,
              requires_confirmation: true,
            }),
          );
        },
      ),
      http.post(
        `${API_BASE}/assistant/sessions/:sessionId/plans/execute`,
        async ({ request }) => {
          executePayload = (await request.json()) as Record<string, unknown>;
          return HttpResponse.json(
            envelope({
              operation_id: "op-opt-001",
              plan_id: "plan-opt-001",
              intent_name: "run_prompt_optimization",
              status: "completed",
              executed_action: "run_prompt_optimization",
              result: {
                result_type: "optimization_run",
                status: "completed",
                summary: "Queued prompt calibration run.",
                trace_id: "trace-opt-assistant",
                correlation_id: "acorr-opt-assistant",
                warnings: [],
                job: {
                  job_id: "opt-job-assistant-001",
                  agent_id: "support-agent",
                  workflow_id: null,
                  primary_item_id: "item-opt-assistant-001",
                  mlflow_run_id: null,
                  artifact_type: "prompt",
                  optimizer_type: "MIPROv2",
                  status: "queued",
                  current_stage: "triage",
                  attempt_count: 0,
                  error_message: null,
                  total_tokens: 0,
                  cost_usd: 0,
                  bundle_targets: [],
                  bundle_expansion_count: 1,
                  diagnosis: null,
                  candidate: null,
                  eval_results: null,
                  created_at: "2025-01-01T00:00:00Z",
                  updated_at: "2025-01-01T00:00:00Z",
                },
              },
              run: null,
            }),
          );
        },
      ),
      http.get(`${API_BASE}/jobs/:jobId`, ({ params }) =>
        HttpResponse.json(
          envelope({
            job_id: String(params.jobId),
            agent_id: "support-agent",
            workflow_id: null,
            primary_item_id: "item-opt-assistant-001",
            mlflow_run_id: null,
            artifact_type: "prompt",
            optimizer_type: "MIPROv2",
            status: "queued",
            current_stage: "triage",
            attempt_count: 0,
            error_message: null,
            total_tokens: 0,
            cost_usd: 0,
            bundle_targets: [],
            bundle_expansion_count: 1,
            diagnosis: null,
            candidate: null,
            eval_results: null,
            created_at: "2025-01-01T00:00:00Z",
            updated_at: "2025-01-01T00:00:00Z",
          }),
        ),
      ),
    );

    const user = userEvent.setup();
    renderPrompts();
    await screen.findByRole("heading", { name: "Prompts" });

    await openWorkspaceStage(user, "Calibration");
    expect(
      await screen.findByText("Assistant-Guided Calibration"),
    ).toBeInTheDocument();

    await user.type(
      screen.getByLabelText("Assistant intent request"),
      "Calibrate support-agent using the assistant dataset with MIPROv2 and strict gate.",
    );

    await user.click(screen.getByRole("button", { name: "Analyze Intent" }));
    expect(
      await screen.findByText("run_prompt_optimization"),
    ).toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: "Build Plan" }));
    expect(await screen.findByText("plan-opt-001")).toBeInTheDocument();

    const algorithmSelect = screen.getByLabelText(
      "Calibration strategy",
    ) as HTMLSelectElement;
    const datasetSelect = screen.getByLabelText(
      "Calibration dataset",
    ) as HTMLSelectElement;
    const minScoreInput = screen.getByLabelText(
      "Minimum aggregate score",
    ) as HTMLInputElement;
    const maxRegressionInput = screen.getByLabelText(
      "Maximum regression delta",
    ) as HTMLInputElement;
    const weightInput = screen.getByLabelText(
      "helpfulness weight",
    ) as HTMLInputElement;

    expect(algorithmSelect.value).toBe("MIPROv2");
    expect(datasetSelect.value).toBe("eds-opt-assistant");
    expect(minScoreInput.value).toBe("0.91");
    expect(maxRegressionInput.value).toBe("0.01");
    expect(weightInput.value).toBe("2");

    await user.click(
      screen.getByRole("button", { name: "Execute Confirmed Plan" }),
    );
    expect(await screen.findByText("op-opt-001")).toBeInTheDocument();
    expect(
      await screen.findByText("opt-job-assistant-001"),
    ).toBeInTheDocument();
    expect(
      await screen.findByText("Queued prompt calibration run."),
    ).toBeInTheDocument();
    expect(screen.getByText("trace-opt-assistant")).toBeInTheDocument();
    expect(screen.getByText("acorr-opt-assistant")).toBeInTheDocument();

    expect(resolvePayload).not.toBeNull();
    expect(planPayload).not.toBeNull();
    expect(executePayload).not.toBeNull();
    if (
      resolvePayload === null ||
      planPayload === null ||
      executePayload === null
    ) {
      throw new Error("Expected assistant request payloads to be captured");
    }

    const resolvedPayload = resolvePayload as { content?: string };
    const plannedPayload = planPayload as { slot_overrides?: unknown };
    const executedPayload = executePayload as {
      plan_id?: string;
      confirm?: boolean;
    };

    expect(resolvedPayload.content).toContain("Calibrate support-agent");
    expect(plannedPayload.slot_overrides).toBeTruthy();
    expect(executedPayload).toEqual({ plan_id: "plan-opt-001", confirm: true });
  });

  it("restores latest assistant plan automatically when reopening calibration", async () => {
    let createSessionCalls = 0;
    let latestPlanCalls = 0;

    window.localStorage.setItem(
      "caliber.prompts.optimization.assistantSession.support-agent",
      "asst-opt-restore-001",
    );

    server.use(
      http.get(`${API_BASE}/prompts/calibration/options`, () =>
        HttpResponse.json(
          envelope({
            optimizers: ["MetaPrompt", "MIPROv2"],
            default_optimizer: "MetaPrompt",
            scorers: [
              {
                name: "helpfulness",
                label: "Helpfulness",
                description: "Rates whether the response is helpful.",
                requires_config: false,
                provider: "mlflow",
                category: "core",
                available: true,
                unavailable_reason: null,
                install_command: null,
                config_template: null,
              },
            ],
            default_scorers: ["helpfulness"],
            default_gate: {
              min_aggregate_score: 0.85,
              max_regression_delta: 0.02,
            },
          }),
        ),
      ),
      http.get(`${API_BASE}/eval-datasets`, () =>
        HttpResponse.json(
          envelope([
            {
              dataset_id: "eds-opt-restore",
              name: "Restored Calibration Dataset",
              description: "Dataset restored from assistant plan",
              owner: "@test",
              tags: ["prompt-calibration"],
              status: "active",
              version: 1,
              created_at: "2025-01-01T00:00:00Z",
              updated_at: "2025-01-01T00:00:00Z",
            },
          ]),
        ),
      ),
      http.get(`${API_BASE}/jobs`, () => HttpResponse.json(envelope([]))),
      http.post(`${API_BASE}/assistant/sessions`, () => {
        createSessionCalls += 1;
        return HttpResponse.json(
          envelope({
            session_id: "asst-opt-created-unexpected",
            title: "Prompt calibration workbench",
            goal: "Intent planning",
            status: "active",
            metadata_: {},
            created_at: "2025-01-01T00:00:00Z",
            updated_at: "2025-01-01T00:00:00Z",
          }),
          { status: 201 },
        );
      }),
      http.get(
        `${API_BASE}/assistant/sessions/:sessionId/plans/latest`,
        ({ params }) => {
          latestPlanCalls += 1;
          expect(String(params.sessionId)).toBe("asst-opt-restore-001");
          return HttpResponse.json(
            envelope({
              mode: "intent_plan",
              plan_id: "plan-opt-restore-001",
              intent: {
                name: "run_prompt_optimization",
                confidence: 0.95,
                rationale: "Recovered from stored assistant session.",
              },
              actions: [
                {
                  action: "run_prompt_optimization",
                  description: "Queue calibration run",
                  status: "ready",
                },
              ],
              slots: [
                {
                  name: "agent_id",
                  value: "support-agent",
                  required: true,
                  source: "memory",
                  confidence: 1,
                  needs_confirmation: false,
                },
                {
                  name: "eval_dataset_id",
                  value: "eds-opt-restore",
                  required: true,
                  source: "memory",
                  confidence: 1,
                  needs_confirmation: false,
                },
                {
                  name: "optimizer_type",
                  value: "MIPROv2",
                  required: true,
                  source: "memory",
                  confidence: 1,
                  needs_confirmation: false,
                },
                {
                  name: "scorers",
                  value: [{ name: "helpfulness", weight: 3 }],
                  required: true,
                  source: "memory",
                  confidence: 1,
                  needs_confirmation: false,
                },
                {
                  name: "gate.min_aggregate_score",
                  value: 0.9,
                  required: true,
                  source: "memory",
                  confidence: 1,
                  needs_confirmation: false,
                },
                {
                  name: "gate.max_regression_delta",
                  value: 0.015,
                  required: true,
                  source: "memory",
                  confidence: 1,
                  needs_confirmation: false,
                },
                {
                  name: "notes",
                  value: "Restored from latest assistant plan",
                  required: false,
                  source: "memory",
                  confidence: 1,
                  needs_confirmation: false,
                },
              ],
              missing_slots: [],
              assumptions: [],
              questions: [],
              ready: true,
              requires_confirmation: true,
            }),
          );
        },
      ),
    );

    const user = userEvent.setup();
    renderPrompts();
    await screen.findByRole("heading", { name: "Prompts" });

    await openWorkspaceStage(user, "Calibration");

    expect(await screen.findByText("plan-opt-restore-001")).toBeInTheDocument();

    const algorithmSelect = screen.getByLabelText(
      "Calibration strategy",
    ) as HTMLSelectElement;
    const datasetSelect = screen.getByLabelText(
      "Calibration dataset",
    ) as HTMLSelectElement;
    const minScoreInput = screen.getByLabelText(
      "Minimum aggregate score",
    ) as HTMLInputElement;
    const maxRegressionInput = screen.getByLabelText(
      "Maximum regression delta",
    ) as HTMLInputElement;
    const weightInput = screen.getByLabelText(
      "helpfulness weight",
    ) as HTMLInputElement;
    const notesInput = screen.getByLabelText(
      "Calibration run notes",
    ) as HTMLTextAreaElement;

    expect(algorithmSelect.value).toBe("MIPROv2");
    expect(datasetSelect.value).toBe("eds-opt-restore");
    expect(minScoreInput.value).toBe("0.9");
    expect(maxRegressionInput.value).toBe("0.015");
    expect(weightInput.value).toBe("3");
    expect(notesInput.value).toBe("Restored from latest assistant plan");
    expect(latestPlanCalls).toBeGreaterThan(0);
    expect(createSessionCalls).toBe(0);
  });

  it("opens edit form and saves a new prompt version", async () => {
    let saved = false;
    server.use(
      http.get(`${API_BASE}/prompts/support-agent`, () =>
        HttpResponse.json(
          envelope({
            name: "support-agent",
            version: 3,
            alias: "prod",
            template: "You are support-agent v3",
            template_length: 24,
            artifact_ref: "prompts:/support-agent@prod",
          }),
        ),
      ),
      http.post(
        `${API_BASE}/prompts/support-agent/versions`,
        async ({ request }) => {
          const body = (await request.json()) as Record<string, unknown>;
          if (body.template === "You are support-agent v4") {
            saved = true;
          }
          return HttpResponse.json(
            envelope({
              name: "support-agent",
              version: 4,
              uri: "prompts:/support-agent/4",
              template_preview: "You are support-agent v4",
              template_length: 24,
            }),
            { status: 201 },
          );
        },
      ),
    );

    const user = userEvent.setup();
    renderPrompts();
    await screen.findByRole("heading", { name: "Prompts" });

    const editButtons = await screen.findAllByRole("button", { name: "Edit" });
    await user.click(editButtons[0]!);

    expect(
      await screen.findByText("Edit Prompt: Support Agent"),
    ).toBeInTheDocument();
    expect(
      screen.getByText(/updates the alias you selected above/i),
    ).toBeInTheDocument();

    const templateInput = screen.getByPlaceholderText(
      "Prompt template",
    ) as HTMLTextAreaElement;
    expect(templateInput.value).toBe("You are support-agent v3");

    await user.clear(templateInput);
    await user.type(templateInput, "You are support-agent v4");

    await user.click(
      screen.getByRole("button", { name: "Save as New Version" }),
    );
    expect(saved).toBe(true);
  });

  it("can switch to another prompt while editor is open", async () => {
    server.use(
      http.get(`${API_BASE}/prompts`, () =>
        HttpResponse.json(
          envelope([
            {
              agent_id: "support-agent",
              agent_name: "Support Agent",
              agent_enabled: true,
              prompt_name: "support-agent",
              version: 3,
              alias: "prod",
              template_preview: "Support prompt",
              template_length: 14,
              approval_id: null,
              artifact_ref: "prompts:/support-agent@prod",
              has_prompt: true,
              source: "both",
            },
            {
              agent_id: "other-agent",
              agent_name: "Other Agent",
              agent_enabled: true,
              prompt_name: "other-agent",
              version: 1,
              alias: "prod",
              template_preview: "Other prompt",
              template_length: 12,
              approval_id: null,
              artifact_ref: "prompts:/other-agent@prod",
              has_prompt: true,
              source: "both",
            },
          ]),
        ),
      ),
      http.get(`${API_BASE}/prompts/support-agent`, () =>
        HttpResponse.json(
          envelope({
            name: "support-agent",
            version: 3,
            alias: "prod",
            template: "Support template",
            template_length: 16,
            artifact_ref: "prompts:/support-agent@prod",
          }),
        ),
      ),
      http.get(`${API_BASE}/prompts/other-agent`, () =>
        HttpResponse.json(
          envelope({
            name: "other-agent",
            version: 1,
            alias: "prod",
            template: "Other template",
            template_length: 14,
            artifact_ref: "prompts:/other-agent@prod",
          }),
        ),
      ),
    );

    const user = userEvent.setup();
    renderPrompts();
    await screen.findByRole("heading", { name: "Prompts" });

    const editButtons = await screen.findAllByRole("button", { name: "Edit" });
    await user.click(editButtons[0]!);

    const switchSelect = await screen.findByLabelText("Switch prompt");
    await user.selectOptions(switchSelect, "other-agent");

    expect(
      await screen.findByText("Edit Prompt: Other Agent"),
    ).toBeInTheDocument();
    const templateInput = screen.getByPlaceholderText(
      "Prompt template",
    ) as HTMLTextAreaElement;
    expect(templateInput.value).toBe("Other template");
  });

  it("asks before discarding unsaved changes on prompt switch", async () => {
    const confirmSpy = vi.spyOn(window, "confirm").mockReturnValue(false);
    server.use(
      http.get(`${API_BASE}/prompts`, () =>
        HttpResponse.json(
          envelope([
            {
              agent_id: "support-agent",
              agent_name: "Support Agent",
              agent_enabled: true,
              prompt_name: "support-agent",
              version: 3,
              alias: "prod",
              template_preview: "Support prompt",
              template_length: 14,
              approval_id: null,
              artifact_ref: "prompts:/support-agent@prod",
              has_prompt: true,
              source: "both",
            },
            {
              agent_id: "other-agent",
              agent_name: "Other Agent",
              agent_enabled: true,
              prompt_name: "other-agent",
              version: 1,
              alias: "prod",
              template_preview: "Other prompt",
              template_length: 12,
              approval_id: null,
              artifact_ref: "prompts:/other-agent@prod",
              has_prompt: true,
              source: "both",
            },
          ]),
        ),
      ),
      http.get(`${API_BASE}/prompts/support-agent`, () =>
        HttpResponse.json(
          envelope({
            name: "support-agent",
            version: 3,
            alias: "prod",
            template: "Support template",
            template_length: 16,
            artifact_ref: "prompts:/support-agent@prod",
          }),
        ),
      ),
      http.get(`${API_BASE}/prompts/other-agent`, () =>
        HttpResponse.json(
          envelope({
            name: "other-agent",
            version: 1,
            alias: "prod",
            template: "Other template",
            template_length: 14,
            artifact_ref: "prompts:/other-agent@prod",
          }),
        ),
      ),
    );

    const user = userEvent.setup();
    renderPrompts();
    await screen.findByRole("heading", { name: "Prompts" });

    const editButtons = await screen.findAllByRole("button", { name: /Edit/i });
    await user.click(editButtons[0]!);

    const templateInput = await screen.findByPlaceholderText("Prompt template");
    await user.clear(templateInput);
    await user.type(templateInput, "Changed unsaved template");

    const switchSelect = screen.getByLabelText("Switch prompt");
    await user.selectOptions(switchSelect, "other-agent");

    expect(confirmSpy).toHaveBeenCalled();
    expect(screen.getByText("Edit Prompt: Support Agent")).toBeInTheDocument();

    confirmSpy.mockRestore();
  });

  it("asks before discarding unsaved changes on cancel", async () => {
    const confirmSpy = vi.spyOn(window, "confirm").mockReturnValue(false);
    server.use(
      http.get(`${API_BASE}/prompts/support-agent`, () =>
        HttpResponse.json(
          envelope({
            name: "support-agent",
            version: 3,
            alias: "prod",
            template: "Support template",
            template_length: 16,
            artifact_ref: "prompts:/support-agent@prod",
          }),
        ),
      ),
    );

    const user = userEvent.setup();
    renderPrompts();
    await screen.findByRole("heading", { name: "Prompts" });

    const editButtons = await screen.findAllByRole("button", { name: /Edit/i });
    await user.click(editButtons[0]!);

    const templateInput = await screen.findByPlaceholderText("Prompt template");
    await user.clear(templateInput);
    await user.type(templateInput, "Changed unsaved template");

    await user.click(screen.getByRole("button", { name: "Cancel" }));

    expect(confirmSpy).toHaveBeenCalled();
    expect(screen.getByText("Edit Prompt: Support Agent")).toBeInTheDocument();

    confirmSpy.mockRestore();
  });

  it("does not switch tabs if unsaved changes are rejected", async () => {
    const confirmSpy = vi.spyOn(window, "confirm").mockReturnValue(false);
    server.use(
      http.get(`${API_BASE}/prompts/support-agent`, () =>
        HttpResponse.json(
          envelope({
            name: "support-agent",
            version: 3,
            alias: "prod",
            template: "Support template",
            template_length: 16,
            artifact_ref: "prompts:/support-agent@prod",
          }),
        ),
      ),
    );

    const user = userEvent.setup();
    renderPrompts();
    await screen.findByRole("heading", { name: "Prompts" });

    const editButtons = await screen.findAllByRole("button", { name: /Edit/i });
    await user.click(editButtons[0]!);

    const templateInput = await screen.findByPlaceholderText("Prompt template");
    await user.clear(templateInput);
    await user.type(templateInput, "Unsaved changes");

    // Closing the editor with unsaved changes prompts a discard confirm; a
    // rejected confirm keeps the editor open.
    await user.click(screen.getByRole("button", { name: "Cancel" }));

    expect(confirmSpy).toHaveBeenCalled();
    expect(screen.getByText("Edit Prompt: Support Agent")).toBeInTheDocument();

    confirmSpy.mockRestore();
  });

  it("closes the editor when the discard confirm is accepted", async () => {
    const confirmSpy = vi.spyOn(window, "confirm").mockReturnValue(true);
    server.use(
      http.get(`${API_BASE}/prompts/support-agent`, () =>
        HttpResponse.json(
          envelope({
            name: "support-agent",
            version: 3,
            alias: "prod",
            template: "Support template",
            template_length: 16,
            artifact_ref: "prompts:/support-agent@prod",
          }),
        ),
      ),
    );

    const user = userEvent.setup();
    renderPrompts();
    await screen.findByRole("heading", { name: "Prompts" });

    const editButtons = await screen.findAllByRole("button", { name: /Edit/i });
    await user.click(editButtons[0]!);

    const templateInput = await screen.findByPlaceholderText("Prompt template");
    await user.clear(templateInput);
    await user.type(templateInput, "Unsaved changes");

    await user.click(screen.getByRole("button", { name: "Cancel" }));

    expect(confirmSpy).toHaveBeenCalled();
    expect(
      screen.queryByText("Edit Prompt: Support Agent"),
    ).not.toBeInTheDocument();

    confirmSpy.mockRestore();
  });

  it("opens versions panel and shows version rows", async () => {
    server.use(
      http.get(`${API_BASE}/prompts/support-agent/versions`, () =>
        HttpResponse.json(
          envelope([
            {
              name: "support-agent",
              version: 4,
              aliases: ["prod"],
              creation_timestamp: 1_700_000_000_000,
              updated_timestamp: 1_700_000_000_100,
              run_id: "run-004",
              source: "models:/support-agent/4",
              commit_message: "Improve greeting style",
              current: true,
            },
            {
              name: "support-agent",
              version: 3,
              aliases: [],
              creation_timestamp: 1_690_000_000_000,
              updated_timestamp: 1_690_000_000_100,
              run_id: "run-003",
              source: "models:/support-agent/3",
              commit_message: "Initial commit",
              current: false,
            },
          ]),
        ),
      ),
    );

    const user = userEvent.setup();
    renderPrompts();
    await screen.findByRole("heading", { name: "Prompts" });

    await user.click(screen.getAllByRole("button", { name: "Versions" })[0]!);

    expect(
      await screen.findByText("Versions: Support Agent"),
    ).toBeInTheDocument();
    expect(screen.getAllByText("v4").length).toBeGreaterThan(0);
    expect(screen.getAllByText("v3").length).toBeGreaterThan(0);
    expect(screen.getByText("Live")).toBeInTheDocument();
  });

  it("promotes a non-live version to @prod", async () => {
    let promotedVersion: number | null = null;
    let versionCalls = 0;
    server.use(
      http.get(`${API_BASE}/prompts/support-agent/versions`, () => {
        versionCalls += 1;
        if (versionCalls < 2) {
          return HttpResponse.json(
            envelope([
              {
                name: "support-agent",
                version: 4,
                aliases: ["prod"],
                creation_timestamp: 1_700_000_000_000,
                updated_timestamp: 1_700_000_000_100,
                run_id: "run-004",
                source: "models:/support-agent/4",
                commit_message: "Improve greeting style",
                current: true,
              },
              {
                name: "support-agent",
                version: 3,
                aliases: [],
                creation_timestamp: 1_690_000_000_000,
                updated_timestamp: 1_690_000_000_100,
                run_id: "run-003",
                source: "models:/support-agent/3",
                commit_message: "Initial commit",
                current: false,
              },
            ]),
          );
        }
        return HttpResponse.json(
          envelope([
            {
              name: "support-agent",
              version: 4,
              aliases: [],
              creation_timestamp: 1_700_000_000_000,
              updated_timestamp: 1_700_000_000_100,
              run_id: "run-004",
              source: "models:/support-agent/4",
              commit_message: "Improve greeting style",
              current: false,
            },
            {
              name: "support-agent",
              version: 3,
              aliases: ["prod"],
              creation_timestamp: 1_690_000_000_000,
              updated_timestamp: 1_690_000_000_100,
              run_id: "run-003",
              source: "models:/support-agent/3",
              commit_message: "Initial commit",
              current: true,
            },
          ]),
        );
      }),
      http.post(
        `${API_BASE}/prompts/support-agent/aliases/prod`,
        async ({ request }) => {
          const body = (await request.json()) as { version: number };
          promotedVersion = body.version;
          return HttpResponse.json(
            envelope({
              name: "support-agent",
              alias: "prod",
              version: body.version,
            }),
          );
        },
      ),
    );

    const user = userEvent.setup();
    renderPrompts();
    await screen.findByRole("heading", { name: "Prompts" });

    await user.click(screen.getAllByRole("button", { name: "Versions" })[0]!);
    await user.click(
      await screen.findByRole("button", { name: "Promote to @prod" }),
    );

    expect(promotedVersion).toBe(3);
    expect(await screen.findAllByText("Live")).toHaveLength(1);
  });

  it("loads side-by-side template compare when versions panel opens", async () => {
    server.use(
      http.get(`${API_BASE}/prompts/support-agent/versions`, () =>
        HttpResponse.json(
          envelope([
            {
              name: "support-agent",
              version: 5,
              aliases: ["prod"],
              creation_timestamp: 1_710_000_000_000,
              updated_timestamp: 1_710_000_000_100,
              run_id: "run-005",
              source: "models:/support-agent/5",
              commit_message: "Current prod",
              current: true,
            },
            {
              name: "support-agent",
              version: 4,
              aliases: [],
              creation_timestamp: 1_700_000_000_000,
              updated_timestamp: 1_700_000_000_100,
              run_id: "run-004",
              source: "models:/support-agent/4",
              commit_message: "Previous",
              current: false,
            },
          ]),
        ),
      ),
      http.get(
        `${API_BASE}/prompts/support-agent/versions/:version`,
        ({ params }) => {
          const version = Number(params.version);
          return HttpResponse.json(
            envelope({
              name: "support-agent",
              version,
              template: `System prompt v${version}`,
              template_length: `System prompt v${version}`.length,
              artifact_ref: `prompts:/support-agent/${version}`,
            }),
          );
        },
      ),
    );

    const user = userEvent.setup();
    renderPrompts();
    await screen.findByRole("heading", { name: "Prompts" });

    await user.click(screen.getAllByRole("button", { name: "Versions" })[0]!);

    expect(await screen.findByText("Compare Templates")).toBeInTheDocument();
    expect(await screen.findByText("System prompt v5")).toBeInTheDocument();
    expect(await screen.findByText("System prompt v4")).toBeInTheDocument();
  });

  it("re-runs compare after selecting different versions", async () => {
    const requestedVersions: number[] = [];
    server.use(
      http.get(`${API_BASE}/prompts/support-agent/versions`, () =>
        HttpResponse.json(
          envelope([
            {
              name: "support-agent",
              version: 5,
              aliases: ["prod"],
              creation_timestamp: 1_710_000_000_000,
              updated_timestamp: 1_710_000_000_100,
              run_id: "run-005",
              source: "models:/support-agent/5",
              commit_message: "Current prod",
              current: true,
            },
            {
              name: "support-agent",
              version: 4,
              aliases: [],
              creation_timestamp: 1_700_000_000_000,
              updated_timestamp: 1_700_000_000_100,
              run_id: "run-004",
              source: "models:/support-agent/4",
              commit_message: "Previous",
              current: false,
            },
            {
              name: "support-agent",
              version: 3,
              aliases: [],
              creation_timestamp: 1_690_000_000_000,
              updated_timestamp: 1_690_000_000_100,
              run_id: "run-003",
              source: "models:/support-agent/3",
              commit_message: "Baseline",
              current: false,
            },
          ]),
        ),
      ),
      http.get(
        `${API_BASE}/prompts/support-agent/versions/:version`,
        ({ params }) => {
          const version = Number(params.version);
          requestedVersions.push(version);
          return HttpResponse.json(
            envelope({
              name: "support-agent",
              version,
              template: `Template v${version} unique`,
              template_length: `Template v${version} unique`.length,
              artifact_ref: `prompts:/support-agent/${version}`,
            }),
          );
        },
      ),
    );

    const user = userEvent.setup();
    renderPrompts();
    await screen.findByRole("heading", { name: "Prompts" });

    await user.click(screen.getAllByRole("button", { name: "Versions" })[0]!);
    await screen.findByText("Template v5 unique");
    await screen.findByText("Template v4 unique");

    await user.selectOptions(
      screen.getByLabelText("Compare left version"),
      "3",
    );
    await user.selectOptions(
      screen.getByLabelText("Compare right version"),
      "5",
    );
    await user.click(screen.getByRole("button", { name: "Compare" }));

    expect(requestedVersions).toContain(3);
    expect(await screen.findByText("Template v3 unique")).toBeInTheDocument();
    expect(await screen.findAllByText("Template v5 unique")).toHaveLength(1);
  });
});

describe("Prompts — Workspace Playground stage", () => {
  it("locks the Playground stage to the open prompt (model picker, no prompt picker)", async () => {
    const user = userEvent.setup();
    renderPrompts();
    await screen.findByRole("heading", { name: "Prompts" });

    await openWorkspaceStage(user, "Playground");

    // Model selector renders, but the in-tab prompt picker is gone — the prompt
    // is fixed by the open Workspace.
    expect(await screen.findByLabelText("Select model")).toBeInTheDocument();
    expect(screen.queryByLabelText("Select a prompt")).not.toBeInTheDocument();
  });

  it("shows the inventory empty state when no prompts exist", async () => {
    server.use(
      http.get(`${API_BASE}/prompts`, () => HttpResponse.json(envelope([]))),
    );
    renderPrompts();
    await screen.findByRole("heading", { name: "Prompts" });

    // With nothing to open there is no workspace — the inventory invites
    // creating a prompt, never registering an agent.
    expect(
      await screen.findByText("No agents registered yet."),
    ).toBeInTheDocument();
    expect(screen.queryByText(/register an agent/i)).not.toBeInTheDocument();
  });

  it("starts playground sessions with prompt metadata and shows immutable prompt identity", async () => {
    let sessionPayload: Record<string, unknown> | null = null;
    server.use(
      http.post(`${API_BASE}/assistant/sessions`, async ({ request }) => {
        sessionPayload = (await request.json()) as Record<string, unknown>;
        return HttpResponse.json(
          envelope({
            session_id: "ASST-playground001",
            title: "Prompt Playground: Support Agent",
            owner: "@test",
            status: "active",
            goal: "Prompt context",
            metadata_:
              (sessionPayload?.metadata_ as Record<string, unknown>) ?? {},
            active_draft_id: null,
            created_at: "2025-01-01T00:00:00Z",
            updated_at: "2025-01-01T00:00:00Z",
          }),
          { status: 201 },
        );
      }),
    );

    const user = userEvent.setup();
    renderPrompts();
    await screen.findByRole("heading", { name: "Prompts" });

    await openWorkspaceStage(user, "Playground");
    await screen.findByLabelText("Select model");
    await user.click(
      screen.getByRole("button", { name: "Start Chat Session" }),
    );

    expect(sessionPayload).not.toBeNull();
    if (sessionPayload === null) {
      throw new Error("Expected playground session payload");
    }

    const metadata = (sessionPayload["metadata_"] ?? {}) as {
      model?: string;
      prompt_context?: {
        prompt_name?: string;
        alias?: string;
        version?: number;
        artifact_ref?: string;
      };
    };
    expect(sessionPayload["goal"]).toContain(
      "FULL PROD TEMPLATE for support-agent",
    );
    expect(metadata.model).toBeTruthy();
    expect(metadata.prompt_context?.prompt_name).toBe("support-agent");
    expect(metadata.prompt_context?.alias).toBe("prod");
    expect(metadata.prompt_context?.version).toBe(3);
    expect(metadata.prompt_context?.artifact_ref).toBe(
      "prompts:/support-agent@prod",
    );

    expect(
      await screen.findByTestId("playground-prompt-ref"),
    ).toHaveTextContent("prompts:/support-agent@prod");
    // The chat header surfaces the locked prompt identity. (The Workspace header
    // above also shows a "Version:" line, so match on presence, not uniqueness.)
    expect(screen.getAllByText(/Alias:/).length).toBeGreaterThan(0);
    expect(screen.getAllByText(/Version:/).length).toBeGreaterThan(0);
    expect(screen.getByText(/Ref:/)).toBeInTheDocument();
  });
});

describe("Prompts — Workspace Test Sets stage", () => {
  it("shows test case controls locked to the open prompt", async () => {
    const user = userEvent.setup();
    renderPrompts();
    await screen.findByRole("heading", { name: "Prompts" });

    await openWorkspaceStage(user, "Test Sets");

    // The prompt is fixed by the Workspace — no in-tab prompt picker.
    expect(screen.queryByLabelText("Select a prompt")).not.toBeInTheDocument();
    // Model selector renders after config loads.
    expect(await screen.findByLabelText("Select model")).toBeInTheDocument();
    // Number-of-test-cases selector + Generate button render.
    expect(screen.getByLabelText("Number of test cases")).toBeInTheDocument();
    expect(
      screen.getByRole("button", { name: /Generate Test Cases/i }),
    ).toBeInTheDocument();
  });

  it("shows the empty-state placeholder before generating tests", async () => {
    const user = userEvent.setup();
    renderPrompts();
    await screen.findByRole("heading", { name: "Prompts" });

    await openWorkspaceStage(user, "Test Sets");

    expect(await screen.findByText("Prompt Test Cases")).toBeInTheDocument();
    expect(screen.getByText(/Select a prompt and click/)).toBeInTheDocument();
  });

  it("supports a custom number of test cases for generation", async () => {
    let generatedGoal: string | null = null;

    server.use(
      http.post(`${API_BASE}/assistant/sessions`, async ({ request }) => {
        const body = (await request.json()) as Record<string, unknown>;
        generatedGoal = typeof body.goal === "string" ? body.goal : null;
        return HttpResponse.json(
          envelope({
            session_id: "ASST-custom-count-001",
            title: "Test Gen",
            owner: "@test",
            status: "active",
            goal: body.goal ?? "",
            metadata_: {},
            active_draft_id: null,
            created_at: new Date().toISOString(),
            updated_at: new Date().toISOString(),
          }),
          { status: 201 },
        );
      }),
      http.post(`${API_BASE}/assistant/sessions/:sessionId/messages`, () =>
        HttpResponse.json(
          envelope({
            assistant_message: {
              message_id: "AMSG-custom-count-001",
              session_id: "ASST-custom-count-001",
              role: "assistant",
              content:
                '[{"input":"Edge input","expectedBehavior":"Be concise","tags":["edge-case"]}]',
              metadata_: {},
              sequence_number: 1,
              created_at: new Date().toISOString(),
            },
            questions: [],
            draft_updates: [],
            run: null,
          }),
          { status: 201 },
        ),
      ),
    );

    const user = userEvent.setup();
    renderPrompts();
    await screen.findByRole("heading", { name: "Prompts" });

    await openWorkspaceStage(user, "Test Sets");

    await screen.findByLabelText("Number of test cases");
    const increaseButton = screen.getByLabelText("Increase test case count");
    for (let i = 0; i < 7; i++) {
      await user.click(increaseButton);
    }
    await user.click(
      screen.getByRole("button", { name: /Generate Test Cases/i }),
    );

    expect(await screen.findByText("Edge input")).toBeInTheDocument();
    expect(generatedGoal).not.toBeNull();
    if (generatedGoal === null) {
      throw new Error("Expected generation goal to be captured");
    }
    expect(generatedGoal).toContain("Generate exactly 12 test cases");
    expect(generatedGoal).toContain("FULL PROD TEMPLATE for support-agent");
  });
});

describe("Prompts — Workspace Runs stage", () => {
  // A run with parseable judge JSON: the agent reply IS a verdict object, and
  // the judge regex extracts it. Shared session+message handlers cover both the
  // agent turn and the judge turn.
  function installRunnableJudge(verdict: "pass" | "fail" | "partial", score: number): void {
    server.use(
      http.post(`${API_BASE}/assistant/sessions`, async ({ request }) => {
        const body = (await request.json().catch(() => ({}))) as Record<string, unknown>;
        return HttpResponse.json(
          envelope({
            session_id: "ASST-run-001",
            title: typeof body.title === "string" ? body.title : "Run",
            owner: "@test",
            status: "active",
            goal: typeof body.goal === "string" ? body.goal : "",
            metadata_: {},
            active_draft_id: null,
            created_at: new Date().toISOString(),
            updated_at: new Date().toISOString(),
          }),
          { status: 201 },
        );
      }),
      http.post(`${API_BASE}/assistant/sessions/:sessionId/messages`, () =>
        HttpResponse.json(
          envelope({
            assistant_message: {
              message_id: "AMSG-run-001",
              session_id: "ASST-run-001",
              role: "assistant",
              content: `{"verdict":"${verdict}","score":${score},"reasoning":"judged"}`,
              metadata_: {},
              sequence_number: 1,
              created_at: new Date().toISOString(),
            },
            questions: [],
            draft_updates: [],
            run: null,
          }),
          { status: 201 },
        ),
      ),
    );
  }

  it("runs the pinned test set and persists a durable run", async () => {
    const saved: Array<Record<string, unknown>> = [];
    server.use(
      http.get(`${API_BASE}/prompts/support-agent/workspace`, () =>
        HttpResponse.json(
          envelope({
            model: "gpt-4o-mini",
            version: 3,
            status: "Tested",
            bound_to: null,
            dataset_id: "DS-1",
            last_run: null,
            baseline_run_id: null,
            baseline_run: null,
          }),
        ),
      ),
      http.get(`${API_BASE}/eval-datasets/DS-1/examples`, () =>
        HttpResponse.json(
          envelope([
            {
              example_id: "ex-1",
              dataset_id: "DS-1",
              dataset_version: 1,
              input: { user_message: "How do refunds work?" },
              expected: { behavior: "Explain the refund policy" },
              weight: 1,
              tags: [],
              created_at: "2025-01-01T00:00:00Z",
              superseded_at: null,
            },
          ]),
        ),
      ),
      http.get(`${API_BASE}/prompts/test-runs`, () => HttpResponse.json(envelope([]))),
      http.post(`${API_BASE}/prompts/test-runs`, async ({ request }) => {
        const body = (await request.json().catch(() => ({}))) as Record<string, unknown>;
        saved.push(body);
        return HttpResponse.json(
          envelope({
            test_run_id: "PTR-fresh",
            agent_id: "support-agent",
            prompt_name: "support-agent",
            prompt_alias: "prod",
            prompt_version: 3,
            model: "gpt-4o-mini",
            eval_dataset_id: "DS-1",
            test_set_size: 1,
            passed_count: 1,
            failed_count: 0,
            partial_count: 0,
            overall_score: 1,
            trace_id: null,
            mlflow_run_id: null,
            created_by: "@test",
            status: "completed",
            created_at: "2025-02-01T00:00:00Z",
            completed_at: "2025-02-01T00:00:00Z",
          }),
          { status: 201 },
        );
      }),
      http.get(`${API_BASE}/prompts/test-runs/PTR-fresh`, () =>
        HttpResponse.json(
          envelope({
            test_run_id: "PTR-fresh",
            agent_id: "support-agent",
            prompt_name: "support-agent",
            prompt_alias: "prod",
            prompt_version: 3,
            model: "gpt-4o-mini",
            eval_dataset_id: "DS-1",
            test_set_size: 1,
            passed_count: 1,
            failed_count: 0,
            partial_count: 0,
            overall_score: 1,
            trace_id: null,
            mlflow_run_id: null,
            created_by: "@test",
            status: "completed",
            created_at: "2025-02-01T00:00:00Z",
            completed_at: "2025-02-01T00:00:00Z",
            results: [
              {
                testCaseId: "ex-1",
                input: "How do refunds work?",
                expectedBehavior: "Explain the refund policy",
                actualResponse: "Refunds are processed in 5 days.",
                verdict: "pass",
                score: 1,
                reasoning: "judged",
              },
            ],
          }),
        ),
      ),
    );
    installRunnableJudge("pass", 1);

    const user = userEvent.setup();
    renderPrompts();
    await screen.findByRole("heading", { name: "Prompts" });
    await openWorkspaceStage(user, "Runs");

    await user.click(await screen.findByRole("button", { name: "Run tests" }));

    // A durable run is persisted against the pinned dataset.
    await waitFor(() => expect(saved.length).toBe(1));
    expect(saved[0]!.eval_dataset_id).toBe("DS-1");
    // Per-case results render for the fresh run.
    expect(await screen.findByTestId("workspace-run-results")).toBeInTheDocument();
    expect(await screen.findByText("How do refunds work?")).toBeInTheDocument();
  });

  it("sets a run as baseline and surfaces the diff/regression vs the viewed run", async () => {
    const pinned: Array<Record<string, unknown>> = [];
    // Two runs in history: PTR-base (the baseline, all pass) and PTR-cur (the
    // viewed run, regressed to fail).
    const summary = (id: string, passed: number, failed: number, score: number) => ({
      test_run_id: id,
      agent_id: "support-agent",
      prompt_name: "support-agent",
      prompt_alias: "prod",
      prompt_version: 3,
      model: "gpt-4o-mini",
      eval_dataset_id: "DS-1",
      test_set_size: passed + failed,
      passed_count: passed,
      failed_count: failed,
      partial_count: 0,
      overall_score: score,
      trace_id: null,
      mlflow_run_id: null,
      created_by: "@test",
      status: "completed",
      created_at: id === "PTR-cur" ? "2025-02-02T00:00:00Z" : "2025-02-01T00:00:00Z",
      completed_at: null,
    });
    server.use(
      http.get(`${API_BASE}/prompts/support-agent/workspace`, () =>
        HttpResponse.json(
          envelope({
            model: "gpt-4o-mini",
            version: 3,
            status: "Tested",
            bound_to: null,
            dataset_id: "DS-1",
            last_run: null,
            baseline_run_id: "PTR-base",
            baseline_run: {
              test_run_id: "PTR-base",
              overall_score: 1,
              test_set_size: 1,
              passed_count: 1,
              failed_count: 0,
              partial_count: 0,
              created_at: "2025-02-01T00:00:00Z",
            },
          }),
        ),
      ),
      // Newest-first: PTR-cur is the default viewed run, PTR-base is the baseline.
      http.get(`${API_BASE}/prompts/test-runs`, () =>
        HttpResponse.json(envelope([summary("PTR-cur", 0, 1, 0), summary("PTR-base", 1, 0, 1)])),
      ),
      http.get(`${API_BASE}/prompts/test-runs/PTR-cur`, () =>
        HttpResponse.json(
          envelope({
            ...summary("PTR-cur", 0, 1, 0),
            results: [
              {
                testCaseId: "ex-1",
                input: "How do refunds work?",
                expectedBehavior: "Explain the refund policy",
                actualResponse: "I do not know.",
                verdict: "fail",
                score: 0,
                reasoning: "missed",
              },
            ],
          }),
        ),
      ),
      http.get(`${API_BASE}/prompts/test-runs/PTR-base`, () =>
        HttpResponse.json(
          envelope({
            ...summary("PTR-base", 1, 0, 1),
            results: [
              {
                testCaseId: "ex-1",
                input: "How do refunds work?",
                expectedBehavior: "Explain the refund policy",
                actualResponse: "Refunds take 5 days.",
                verdict: "pass",
                score: 1,
                reasoning: "good",
              },
            ],
          }),
        ),
      ),
      http.post(`${API_BASE}/prompts/:name/baseline`, async ({ request }) => {
        const body = (await request.json().catch(() => ({}))) as Record<string, unknown>;
        pinned.push(body);
        return HttpResponse.json(
          envelope({ baseline_run_id: String(body.test_run_id ?? "") }),
        );
      }),
    );

    const user = userEvent.setup();
    renderPrompts();
    await screen.findByRole("heading", { name: "Prompts" });
    await openWorkspaceStage(user, "Runs");

    // The comparison panel renders: net score delta + a regression.
    const comparison = await screen.findByTestId("workspace-run-comparison");
    expect(comparison).toBeInTheDocument();
    expect(within(comparison).getByTestId("run-score-delta")).toHaveTextContent("-100%");
    expect(within(comparison).getByTestId("run-regressions")).toHaveTextContent(/1 regression/);

    // The baseline run is markable from history; pinning calls setPromptBaseline.
    await user.click(screen.getByRole("button", { name: "View run PTR-base" }));
    expect(await screen.findByTestId("run-baseline-marker")).toBeInTheDocument();
    // Switch back and pin the viewed (current) run as the new baseline.
    await user.click(screen.getByRole("button", { name: "View run PTR-cur" }));
    await user.click(await screen.findByRole("button", { name: "Set as baseline" }));
    await waitFor(() => expect(pinned.length).toBe(1));
    expect(pinned[0]!.test_run_id).toBe("PTR-cur");
  });
});

describe("Prompts — Workspace Bind stage", () => {
  it("lists agents and binds the prompt to a selected agent", async () => {
    const bound: Array<Record<string, unknown>> = [];
    let workspaceCalls = 0;
    server.use(
      http.get(`${API_BASE}/prompts/support-agent/workspace`, () => {
        workspaceCalls += 1;
        // First load: Tested + unbound. After bind: Bound (the refetch).
        const isBound = workspaceCalls > 1;
        return HttpResponse.json(
          envelope({
            model: "gpt-4o-mini",
            version: 3,
            status: isBound ? "Bound" : "Tested",
            bound_to: isBound ? { kind: "agent", agent_id: "billing-agent" } : null,
            dataset_id: null,
            last_run: null,
            baseline_run_id: null,
            baseline_run: null,
          }),
        );
      }),
      http.get(`${API_BASE}/agents`, () =>
        HttpResponse.json(
          envelope([
            {
              agent_id: "billing-agent",
              experiment_id: "exp-billing",
              name: "Billing Agent",
              owner: "@sarah",
              artifact_types: ["prompt"],
              eval_thresholds: {},
              optimizer_config: {},
              approval_policy: {},
              optimize_for: "quality",
              collaboration_mode: null,
              enabled: true,
              required_approvals: 1,
              created_at: "2025-01-01T00:00:00Z",
              updated_at: "2025-01-02T00:00:00Z",
            },
          ]),
        ),
      ),
      http.post(`${API_BASE}/prompts/:name/bind`, async ({ request }) => {
        const body = (await request.json().catch(() => ({}))) as Record<string, unknown>;
        bound.push(body);
        return HttpResponse.json(envelope({ bound_to: body, status: "Bound" }));
      }),
    );

    const user = userEvent.setup();
    renderPrompts();
    await screen.findByRole("heading", { name: "Prompts" });
    await openWorkspaceStage(user, "Bind");

    // Agent picker lists the real agent fleet (hidden targets excluded server-side).
    const agentSelect = await screen.findByLabelText("Select agent to bind");
    expect(within(agentSelect).getByRole("option", { name: /Billing Agent/ })).toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: "Bind prompt target" }));

    // Bind is called with the agent payload.
    await waitFor(() => expect(bound.length).toBe(1));
    expect(bound[0]).toEqual({ kind: "agent", agent_id: "billing-agent" });

    // The workspace was refetched and the header now reads Bound.
    await waitFor(() =>
      expect(screen.getByTestId("workspace-status-badge")).toHaveTextContent("Bound"),
    );
    expect(await screen.findByTestId("workspace-bound-to")).toHaveTextContent(
      /billing-agent/,
    );
  });

  it("binds to a workflow node with a free-text node id", async () => {
    const bound: Array<Record<string, unknown>> = [];
    server.use(
      http.post(`${API_BASE}/prompts/:name/bind`, async ({ request }) => {
        const body = (await request.json().catch(() => ({}))) as Record<string, unknown>;
        bound.push(body);
        return HttpResponse.json(envelope({ bound_to: body, status: "Bound" }));
      }),
    );

    const user = userEvent.setup();
    renderPrompts();
    await screen.findByRole("heading", { name: "Prompts" });
    await openWorkspaceStage(user, "Bind");

    await user.click(await screen.findByRole("button", { name: /Workflow node/ }));
    // Workflow picker lists the default workflow; node id is free text.
    expect(await screen.findByLabelText("Select workflow to bind")).toBeInTheDocument();
    await user.type(screen.getByLabelText("Workflow node id"), "classifier");
    await user.click(screen.getByRole("button", { name: "Bind prompt target" }));

    await waitFor(() => expect(bound.length).toBe(1));
    expect(bound[0]).toEqual({
      kind: "workflow_node",
      workflow_id: "WF-001",
      node_id: "classifier",
    });
  });
});

describe("Prompts — create/edit error flows", () => {
  it("validates required fields and surfaces create failures", async () => {
    server.use(
      http.post(`${API_BASE}/prompts`, () =>
        HttpResponse.json({ detail: "prompt already exists" }, { status: 409 }),
      ),
    );
    const user = userEvent.setup();
    renderPrompts();
    await screen.findByRole("heading", { name: "Prompts" });

    await openCreate(user);
    await user.click(screen.getByRole("button", { name: /Build from template/i }));
    await user.click(
      screen.getByRole("button", { name: /^Grounded Answer/i }),
    );
    await gotoComposeStep(user);
    await user.type(
      await screen.findByLabelText(/Answering goal/i),
      "Resolve support tickets.",
    );
    await gotoSaveStep(user);

    await waitFor(() =>
      expect(
        screen.getByRole("button", { name: /Create and Open staging/i }),
      ).toBeEnabled(),
    );
    await user.click(
      screen.getByRole("button", { name: /Create and Open staging/i }),
    );
    expect(
      await screen.findByText("Prompt name is required."),
    ).toBeInTheDocument();

    await user.type(screen.getByLabelText(/Prompt name/i), "support-agent");
    await user.click(
      screen.getByRole("button", { name: /Create and Open staging/i }),
    );
    expect(
      await screen.findByText("prompt already exists"),
    ).toBeInTheDocument();
  });

  it("handles edit-load failure and empty template validation", async () => {
    server.use(
      http.get(`${API_BASE}/prompts/support-agent`, () =>
        HttpResponse.json({ detail: "prompt fetch failed" }, { status: 500 }),
      ),
    );
    const user = userEvent.setup();
    renderPrompts();
    await screen.findByRole("heading", { name: "Prompts" });

    const editButtons = await screen.findAllByRole("button", { name: "Edit" });
    await user.click(editButtons[0]!);
    expect(await screen.findByText("prompt fetch failed")).toBeInTheDocument();

    server.use(
      http.get(`${API_BASE}/prompts/support-agent`, () =>
        HttpResponse.json(
          envelope({
            name: "support-agent",
            version: 3,
            alias: "prod",
            template: "Existing template",
            template_length: 17,
            artifact_ref: "prompts:/support-agent@prod",
          }),
        ),
      ),
      http.post(`${API_BASE}/prompts/support-agent/versions`, () =>
        HttpResponse.json({ detail: "save failed" }, { status: 500 }),
      ),
    );

    await user.click(screen.getByRole("button", { name: "Cancel" }));
    await user.click(
      (await screen.findAllByRole("button", { name: "Edit" }))[0]!,
    );
    const template = await screen.findByPlaceholderText("Prompt template");
    await user.clear(template);
    await user.click(
      screen.getByRole("button", { name: "Save as New Version" }),
    );
    expect(
      await screen.findByText("Template is required."),
    ).toBeInTheDocument();

    await user.type(template, "Updated template");
    await user.click(
      screen.getByRole("button", { name: "Save as New Version" }),
    );
    expect(await screen.findByText("save failed")).toBeInTheDocument();
  });

  it("closes the versions panel via its Close button", async () => {
    const user = userEvent.setup();
    renderPrompts();
    await screen.findByRole("heading", { name: "Prompts" });

    const versionsButtons = await screen.findAllByRole("button", {
      name: "Versions",
    });
    await user.click(versionsButtons[0]!);
    expect(
      await screen.findByText(/Versions: Support Agent/),
    ).toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: "Close" }));
    expect(
      screen.queryByText(/Versions: Support Agent/),
    ).not.toBeInTheDocument();
  });
});

describe("Prompts — search", () => {
  it("filters the prompt list by query and restores it when cleared", async () => {
    const user = userEvent.setup();
    renderPrompts();
    await screen.findByRole("heading", { name: "Prompts" });

    // Both agents render before any search.
    expect(screen.getByText("Support Agent")).toBeInTheDocument();
    expect(screen.getByText("Billing Agent")).toBeInTheDocument();

    const searchBox = screen.getByRole("searchbox", { name: "Search prompts" });
    await user.type(searchBox, "billing");

    expect(screen.getByText("Billing Agent")).toBeInTheDocument();
    expect(screen.queryByText("Support Agent")).not.toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: "Clear search" }));
    expect(screen.getByText("Support Agent")).toBeInTheDocument();
    expect(screen.getByText("Billing Agent")).toBeInTheDocument();
  });

  it("shows a no-match message when the query matches nothing", async () => {
    const user = userEvent.setup();
    renderPrompts();
    await screen.findByRole("heading", { name: "Prompts" });

    await user.type(
      screen.getByRole("searchbox", { name: "Search prompts" }),
      "zzz-no-such-agent",
    );

    expect(screen.queryByText("Support Agent")).not.toBeInTheDocument();
    expect(screen.getByText(/No agents match/)).toBeInTheDocument();
  });

  it("renders the message-square-text icon on prompt cards (same mark as the sidebar)", async () => {
    renderPrompts();
    const card = await screen.findByTestId("prompt-card-support-agent");

    expect(card.querySelector("svg.lucide-message-square-text")).not.toBeNull();
    // The previous brand-image mark must be gone.
    expect(card.querySelector('img[alt="CALIBER"]')).toBeNull();
  });

  it("lets an admin delete a prompt from the card", async () => {
    const confirmSpy = vi.spyOn(window, "confirm").mockReturnValue(true);
    let deleted: string | null = null;
    server.use(
      http.delete(`${API_BASE}/prompts/support-agent`, () => {
        deleted = "support-agent";
        return HttpResponse.json(envelope({ deleted: "support-agent" }));
      }),
    );

    const user = userEvent.setup();
    renderPrompts();
    await screen.findByText("Support Agent");

    // The destructive affordance is admin-only; the test `/me` handler is admin.
    const deleteButton = await screen.findByRole("button", {
      name: "Delete prompt Support Agent",
    });
    await user.click(deleteButton);

    await waitFor(() => expect(deleted).toBe("support-agent"));
    expect(confirmSpy).toHaveBeenCalled();
    // A page-level banner confirms the delete (the card itself may persist as a
    // promptless agent row, which previously made the action look like a no-op).
    expect(await screen.findByText(/Deleted prompt/)).toBeInTheDocument();
    confirmSpy.mockRestore();
  });

  it("surfaces a delete failure as a page-level banner", async () => {
    const confirmSpy = vi.spyOn(window, "confirm").mockReturnValue(true);
    server.use(
      http.delete(`${API_BASE}/prompts/support-agent`, () =>
        HttpResponse.json({ detail: "registry refused" }, { status: 502 }),
      ),
    );

    const user = userEvent.setup();
    renderPrompts();
    await screen.findByText("Support Agent");

    await user.click(
      await screen.findByRole("button", {
        name: "Delete prompt Support Agent",
      }),
    );

    expect(await screen.findByText(/Failed to delete/)).toBeInTheDocument();
    confirmSpy.mockRestore();
  });

  it("lets an admin delete every deployed prompt at once", async () => {
    const confirmSpy = vi.spyOn(window, "confirm").mockReturnValue(true);
    const deleted: string[] = [];
    server.use(
      http.delete(`${API_BASE}/prompts/:name`, ({ params }) => {
        const name = String(params.name);
        deleted.push(name);
        return HttpResponse.json(envelope({ deleted: name }));
      }),
    );

    const user = userEvent.setup();
    renderPrompts();
    await screen.findByText("Support Agent");

    // Only the deployed prompt (support-agent) is bulk-deletable; the promptless
    // billing-agent row has nothing to delete.
    await user.click(
      await screen.findByRole("button", { name: /Delete all \(1\)/ }),
    );

    await waitFor(() => expect(deleted).toContain("support-agent"));
    expect(
      await screen.findByText(/Deleted all 1 deployed prompt/),
    ).toBeInTheDocument();
    confirmSpy.mockRestore();
  });

  it("hides the card delete affordance for non-admins", async () => {
    server.use(
      http.get(`${API_BASE}/me`, () =>
        HttpResponse.json(
          envelope({
            user_id: "@viewer",
            scopes: ["caliber.viewer"],
            is_admin: false,
          }),
        ),
      ),
    );

    renderPrompts();
    await screen.findByText("Support Agent");

    await waitFor(() =>
      expect(
        screen.queryByRole("button", { name: "Delete prompt Support Agent" }),
      ).not.toBeInTheDocument(),
    );
  });
});

describe("Prompts — Workspace", () => {
  it("opens a prompt into the Workspace with a status header and six stage tabs, and Back returns", async () => {
    server.use(
      http.get(`${API_BASE}/prompts/support-agent/workspace`, () =>
        HttpResponse.json(
          envelope({
            model: "gpt-4.1-mini",
            version: 7,
            status: "Calibrated",
            bound_to: null,
            dataset_id: "eds-1",
            last_run: null,
          }),
        ),
      ),
    );

    const user = userEvent.setup();
    renderPrompts();
    await screen.findByRole("heading", { name: "Prompts" });

    // Open the prompt from the inventory (the card title).
    await user.click(await screen.findByRole("button", { name: "Support Agent" }));

    // The header reflects the workspace payload: name, model, version, status.
    const header = await screen.findByTestId("workspace-header");
    expect(
      within(header).getByRole("heading", { name: "Support Agent" }),
    ).toBeInTheDocument();
    await waitFor(() =>
      expect(within(header).getByText("gpt-4.1-mini")).toBeInTheDocument(),
    );
    expect(within(header).getByText("v7")).toBeInTheDocument();
    expect(screen.getByTestId("workspace-status-badge")).toHaveTextContent(
      "Calibrated",
    );

    // All six stage tabs render, in order.
    for (const stage of [
      "Author",
      "Playground",
      "Test Sets",
      "Runs",
      "Calibration",
      "Bind",
    ]) {
      expect(screen.getByRole("button", { name: stage })).toBeInTheDocument();
    }

    // "← Back to prompts" returns to the inventory.
    await user.click(screen.getByRole("button", { name: /Back to prompts/i }));
    expect(
      await screen.findByRole("button", { name: "New prompt" }),
    ).toBeInTheDocument();
    expect(screen.queryByTestId("workspace-header")).not.toBeInTheDocument();
  });

  it("renders the null model/version as em dashes in the header", async () => {
    server.use(
      http.get(`${API_BASE}/prompts/support-agent/workspace`, () =>
        HttpResponse.json(
          envelope({
            model: null,
            version: null,
            status: "Draft",
            bound_to: null,
            dataset_id: null,
            last_run: null,
          }),
        ),
      ),
    );

    const user = userEvent.setup();
    renderPrompts();
    await screen.findByRole("heading", { name: "Prompts" });
    await user.click(await screen.findByRole("button", { name: "Support Agent" }));

    const header = await screen.findByTestId("workspace-header");
    await waitFor(() =>
      expect(screen.getByTestId("workspace-status-badge")).toHaveTextContent(
        "Draft",
      ),
    );
    // The null model has no inventory fallback, so it renders as "—".
    // (Version falls back to the inventory row's version when the payload is null.)
    expect(within(header).getAllByText("—").length).toBeGreaterThanOrEqual(1);
  });

  it("switches stage tabs and renders each stage with no prompt/agent picker", async () => {
    const user = userEvent.setup();
    renderPrompts();
    await screen.findByRole("heading", { name: "Prompts" });
    await user.click(await screen.findByRole("button", { name: "Support Agent" }));
    await screen.findByTestId("workspace-header");

    // Author (default): the edit-in-place template surface, no picker.
    expect(
      await screen.findByRole("heading", { name: "Author" }),
    ).toBeInTheDocument();
    expect(screen.queryByLabelText("Select a prompt")).not.toBeInTheDocument();

    // Playground: model selector, no prompt picker.
    await user.click(screen.getByRole("button", { name: "Playground" }));
    expect(await screen.findByLabelText("Select model")).toBeInTheDocument();
    expect(screen.queryByLabelText("Select a prompt")).not.toBeInTheDocument();

    // Test Sets: the generator surface, no prompt picker.
    await user.click(screen.getByRole("button", { name: "Test Sets" }));
    expect(
      await screen.findByRole("button", { name: /Generate Test Cases/i }),
    ).toBeInTheDocument();
    expect(screen.queryByLabelText("Select a prompt")).not.toBeInTheDocument();

    // Runs: the run history surface.
    await user.click(screen.getByRole("button", { name: "Runs" }));
    expect(await screen.findByTestId("workspace-run-history")).toBeInTheDocument();

    // Calibration: the run config, no prompt/agent picker.
    await user.click(screen.getByRole("button", { name: "Calibration" }));
    expect(await screen.findByText("Run Configuration")).toBeInTheDocument();
    expect(screen.queryByLabelText("Select a prompt")).not.toBeInTheDocument();
    expect(screen.queryByLabelText("Calibration prompt")).not.toBeInTheDocument();

    // Bind: the binding surface with real kind pickers.
    await user.click(screen.getByRole("button", { name: "Bind" }));
    expect(
      await screen.findByRole("heading", { name: "Bind" }),
    ).toBeInTheDocument();
    expect(
      screen.getByRole("button", { name: "Bind prompt target" }),
    ).toBeInTheDocument();
  });

  it("shows the current binding from the workspace payload on the Bind stage", async () => {
    server.use(
      http.get(`${API_BASE}/prompts/support-agent/workspace`, () =>
        HttpResponse.json(
          envelope({
            model: "gpt-4.1-mini",
            version: 3,
            status: "Bound",
            bound_to: { kind: "agent", agent_id: "support-agent" },
            dataset_id: null,
            last_run: null,
          }),
        ),
      ),
    );

    const user = userEvent.setup();
    renderPrompts();
    await screen.findByRole("heading", { name: "Prompts" });
    await user.click(await screen.findByRole("button", { name: "Support Agent" }));
    await screen.findByTestId("workspace-header");

    await user.click(screen.getByRole("button", { name: "Bind" }));
    expect(await screen.findByTestId("workspace-bound-to")).toHaveTextContent(
      "agent",
    );
  });

  it("hosts search, both filters, clear, and the view toggle inside the shared FilterBar", async () => {
    const user = userEvent.setup();
    renderPrompts();
    await screen.findByRole("heading", { name: "Prompts" });

    const bar = await screen.findByTestId("filter-bar");
    // Search box + both filter dropdowns + the grid/list toggle all live in
    // the one shared toolbar (no per-page layout drift).
    expect(within(bar).getByRole("searchbox", { name: "Search prompts" })).toBeInTheDocument();
    expect(within(bar).getByRole("combobox", { name: "Filter by state" })).toBeInTheDocument();
    expect(within(bar).getByRole("combobox", { name: "Filter by source" })).toBeInTheDocument();
    expect(within(bar).getByTestId("view-toggle")).toBeInTheDocument();

    // Clear surfaces inside the bar once a filter is active.
    await user.type(within(bar).getByRole("searchbox", { name: "Search prompts" }), "agent");
    expect(within(bar).getByRole("button", { name: "Clear filters" })).toBeInTheDocument();
  });

  it("mounts the shared VersionPanel and the draft/promote actions on the Author stage", async () => {
    const user = userEvent.setup();
    renderPrompts();
    await screen.findByRole("heading", { name: "Prompts" });

    await openWorkspaceStage(user, "Author");

    // The shared version-history panel renders (versions are served by MSW), and
    // the developer-draft flow surfaces both save actions.
    expect(await screen.findByTestId("version-panel")).toBeInTheDocument();
    expect(
      screen.getByRole("button", { name: "Save draft" }),
    ).toBeInTheDocument();
    expect(
      screen.getByRole("button", { name: "Save & promote" }),
    ).toBeInTheDocument();
  });
});
