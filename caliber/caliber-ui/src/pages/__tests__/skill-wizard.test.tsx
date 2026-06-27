/**
 * Skill Wizard — comprehensive tests for the 5-step skill builder.
 */

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { http, HttpResponse } from "msw";
import { afterAll, afterEach, beforeAll, describe, expect, it } from "vitest";
import { MemoryRouter, Route, Routes } from "react-router-dom";

import { SkillWizard } from "@/pages/SkillWizard";
import { Skills } from "@/pages/Skills";
import { server } from "@/test/server";

const API_BASE = "/ajax-api/2.0/mlflow/caliber";
const NOW = "2026-06-01T00:00:00Z";

function envelope<T>(data: T): { data: T } {
  return { data };
}

function renderWizard(onClose = () => {}): void {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false, gcTime: 0 } } });
  render(
    <QueryClientProvider client={qc}>
      <MemoryRouter future={{ v7_startTransition: true, v7_relativeSplatPath: true }} initialEntries={["/skills"]}>
        <Routes>
          <Route path="/skills" element={<SkillWizard onClose={onClose} />} />
        </Routes>
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

function renderSkillsPage(): void {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false, gcTime: 0 } } });
  render(
    <QueryClientProvider client={qc}>
      <MemoryRouter future={{ v7_startTransition: true, v7_relativeSplatPath: true }} initialEntries={["/skills"]}>
        <Routes>
          <Route path="/skills" element={<Skills />} />
        </Routes>
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

function makeSkill(overrides: Record<string, unknown> = {}) {
  return {
    skill_id: "SK-test1",
    name: "reasoning-v1",
    description: "A reasoning skill",
    summary: "Chain-of-thought reasoning rubric",
    content: "Think step by step.",
    owner: "@tester",
    category: "custom",
    tags: ["reasoning"],
    skill_metadata: {},
    allowed_tools: null,
    depends_on: [],
    status: "active",
    version: 1,
    created_at: NOW,
    updated_at: NOW,
    ...overrides,
  };
}

beforeAll(() => {
  server.listen({ onUnhandledRequest: "bypass" });
});
afterEach(() => server.resetHandlers());
afterAll(() => server.close());

describe("SkillWizard", () => {
  // Provide default handler for skill list (composability autocomplete)
  beforeAll(() => {
    server.use(
      http.get(`${API_BASE}/skills`, () => HttpResponse.json(envelope([]))),
    );
  });

  describe("Step navigation", () => {
    it("renders step 1 (Identity) by default", () => {
      renderWizard();
      expect(screen.getByTestId("skill-wizard")).toBeInTheDocument();
      expect(screen.getByTestId("skill-step-identity")).toBeInTheDocument();
      expect(screen.getByTestId("skill-wizard-steps")).toBeInTheDocument();
    });

    it("disables Next when name and owner are empty", () => {
      renderWizard();
      expect(screen.getByTestId("skill-wizard-next")).toBeDisabled();
    });

    it("auto-lowercases input and enables Next for valid kebab", async () => {
      renderWizard();
      await userEvent.type(screen.getByTestId("skill-wiz-name"), "My Skill");
      await userEvent.type(screen.getByTestId("skill-wiz-owner"), "@owner");
      // "My Skill" typed char-by-char auto-lowercases to "myskill" (valid kebab)
      expect(screen.getByTestId("skill-wizard-next")).not.toBeDisabled();
    });

    it("enables Next when name and owner are valid, navigates to step 2", async () => {
      renderWizard();
      await userEvent.type(screen.getByTestId("skill-wiz-name"), "my-skill");
      await userEvent.type(screen.getByTestId("skill-wiz-owner"), "@team");
      const next = screen.getByTestId("skill-wizard-next");
      expect(next).not.toBeDisabled();
      await userEvent.click(next);
      expect(screen.getByTestId("skill-step-content")).toBeInTheDocument();
    });

    it("goes back from step 2 to step 1", async () => {
      renderWizard();
      await userEvent.type(screen.getByTestId("skill-wiz-name"), "my-skill");
      await userEvent.type(screen.getByTestId("skill-wiz-owner"), "@team");
      await userEvent.click(screen.getByTestId("skill-wizard-next"));
      expect(screen.getByTestId("skill-step-content")).toBeInTheDocument();
      await userEvent.click(screen.getByTestId("skill-wizard-back"));
      expect(screen.getByTestId("skill-step-identity")).toBeInTheDocument();
    });

    it("calls onClose when Cancel is clicked on step 1", async () => {
      let closed = false;
      renderWizard(() => { closed = true; });
      await userEvent.click(screen.getByTestId("skill-wizard-back"));
      expect(closed).toBe(true);
    });

    it("calls onClose when X button is clicked", async () => {
      let closed = false;
      renderWizard(() => { closed = true; });
      await userEvent.click(screen.getByTestId("skill-wizard-close"));
      expect(closed).toBe(true);
    });

    it("navigates through all 5 steps", async () => {
      renderWizard();

      // Step 1: Identity
      await userEvent.type(screen.getByTestId("skill-wiz-name"), "test-skill");
      await userEvent.type(screen.getByTestId("skill-wiz-owner"), "@team");
      await userEvent.click(screen.getByTestId("skill-wizard-next"));

      // Step 2: Content
      expect(screen.getByTestId("skill-step-content")).toBeInTheDocument();
      await userEvent.type(screen.getByTestId("skill-wiz-content"), "Think step by step.");
      await userEvent.click(screen.getByTestId("skill-wizard-next"));

      // Step 3: Composability
      expect(screen.getByTestId("skill-step-composability")).toBeInTheDocument();
      await userEvent.click(screen.getByTestId("skill-wizard-next"));

      // Step 4: Triggers
      expect(screen.getByTestId("skill-step-triggers")).toBeInTheDocument();
      await userEvent.click(screen.getByTestId("skill-wizard-next"));

      // Step 5: Review
      expect(screen.getByTestId("skill-step-review")).toBeInTheDocument();
      expect(screen.getByTestId("skill-wizard-submit")).toBeInTheDocument();
      expect(screen.queryByTestId("skill-wizard-next")).not.toBeInTheDocument();
    });

    it("shows the 'Step N of 5' progress text in the footer", async () => {
      renderWizard();
      // Step 1 footer
      expect(screen.getByText(/1 of 5/)).toBeInTheDocument();
      // Advance to step 2
      await userEvent.type(screen.getByTestId("skill-wiz-name"), "test-skill");
      await userEvent.type(screen.getByTestId("skill-wiz-owner"), "@team");
      await userEvent.click(screen.getByTestId("skill-wizard-next"));
      expect(screen.getByText(/2 of 5/)).toBeInTheDocument();
    });

    it("gives Next a contextual 'Continue to {next step}' label", () => {
      renderWizard();
      // On step 1 the next step is Content.
      expect(screen.getByTestId("skill-wizard-next")).toHaveTextContent("Continue to Content");
    });

    it("labels the final stepper item 'Review & create'", () => {
      renderWizard();
      const steps = screen.getByTestId("skill-wizard-steps");
      expect(within(steps).getByText("Review & create")).toBeInTheDocument();
    });
  });

  describe("Step 1: Identity & Classification", () => {
    it("auto-lowercases typed characters for kebab-case", async () => {
      renderWizard();
      const nameInput = screen.getByTestId("skill-wiz-name") as HTMLInputElement;
      await userEvent.type(nameInput, "My Cool Skill");
      // Character-by-character: spaces consumed immediately, letters lowercased
      expect(nameInput.value).toBe("mycoolskill");
    });

    it("shows category card picker with custom selected by default", () => {
      renderWizard();
      const customBtn = screen.getByTestId("skill-wiz-category-custom");
      expect(customBtn.className).toContain("border-caliber-purple");
    });

    it("switches category to document_creation", async () => {
      renderWizard();
      await userEvent.click(screen.getByTestId("skill-wiz-category-document_creation"));
      const btn = screen.getByTestId("skill-wiz-category-document_creation");
      expect(btn.className).toContain("border-caliber-purple");
    });

    it("adds and removes tags", async () => {
      renderWizard();
      const tagInput = screen.getByTestId("skill-wiz-tag-input");
      await userEvent.type(tagInput, "reasoning");
      await userEvent.click(screen.getByTestId("skill-wiz-add-tag"));
      expect(screen.getByTestId("skill-wiz-tags")).toBeInTheDocument();
      expect(screen.getByText("reasoning")).toBeInTheDocument();

      // Remove it
      await userEvent.click(screen.getByLabelText("Remove tag reasoning"));
      expect(screen.queryByTestId("skill-wiz-tags")).not.toBeInTheDocument();
    });
  });

  describe("Step 2: Content (Progressive Disclosure)", () => {
    async function goToContentStep(): Promise<void> {
      renderWizard();
      await userEvent.type(screen.getByTestId("skill-wiz-name"), "test-skill");
      await userEvent.type(screen.getByTestId("skill-wiz-owner"), "@team");
      await userEvent.click(screen.getByTestId("skill-wizard-next"));
    }

    it("renders summary, description, and content fields", async () => {
      await goToContentStep();
      expect(screen.getByTestId("skill-wiz-summary")).toBeInTheDocument();
      expect(screen.getByTestId("skill-wiz-description")).toBeInTheDocument();
      expect(screen.getByTestId("skill-wiz-content")).toBeInTheDocument();
    });

    it("shows character counter for summary", async () => {
      await goToContentStep();
      expect(screen.getByText("0/1024")).toBeInTheDocument();
      await userEvent.type(screen.getByTestId("skill-wiz-summary"), "Hello");
      expect(screen.getByText("5/1024")).toBeInTheDocument();
    });

    it("disables Next when content is empty", async () => {
      await goToContentStep();
      expect(screen.getByTestId("skill-wizard-next")).toBeDisabled();
    });

    it("enables Next when content is provided", async () => {
      await goToContentStep();
      await userEvent.type(screen.getByTestId("skill-wiz-content"), "Think step by step.");
      expect(screen.getByTestId("skill-wizard-next")).not.toBeDisabled();
    });

    it("shows line count for content", async () => {
      await goToContentStep();
      await userEvent.type(screen.getByTestId("skill-wiz-content"), "Line 1\nLine 2\nLine 3");
      expect(screen.getByText("3 lines")).toBeInTheDocument();
    });
  });

  describe("Step 3: Composability", () => {
    async function goToComposabilityStep(): Promise<void> {
      renderWizard();
      await userEvent.type(screen.getByTestId("skill-wiz-name"), "test-skill");
      await userEvent.type(screen.getByTestId("skill-wiz-owner"), "@team");
      await userEvent.click(screen.getByTestId("skill-wizard-next"));
      await screen.findByTestId("skill-step-content");
      await userEvent.type(screen.getByTestId("skill-wiz-content"), "Content here.");
      await userEvent.click(screen.getByTestId("skill-wizard-next"));
      await screen.findByTestId("skill-step-composability");
    }

    it("renders depends-on, allowed-tools, and metadata fields", async () => {
      await goToComposabilityStep();
      expect(screen.getByTestId("skill-step-composability")).toBeInTheDocument();
      expect(screen.getByTestId("skill-wiz-dep-input")).toBeInTheDocument();
      expect(screen.getByTestId("skill-wiz-allowed-tools")).toBeInTheDocument();
      expect(screen.getByTestId("skill-wiz-add-meta")).toBeInTheDocument();
    });

    it("adds a dependency via Enter", async () => {
      await goToComposabilityStep();
      const depInput = screen.getByTestId("skill-wiz-dep-input");
      await userEvent.type(depInput, "base-reasoning{enter}");
      expect(screen.getByTestId("skill-wiz-deps")).toBeInTheDocument();
      expect(screen.getByText("base-reasoning")).toBeInTheDocument();
    });

    it("adds and removes metadata rows", async () => {
      await goToComposabilityStep();
      await userEvent.click(screen.getByTestId("skill-wiz-add-meta"));
      // A metadata row should appear
      const metaRows = screen.getByTestId("skill-step-composability").querySelectorAll("[data-testid^='skill-wiz-meta-key-']");
      expect(metaRows.length).toBe(1);
    });

    it("allows typing in allowed tools field", async () => {
      await goToComposabilityStep();
      const toolsInput = screen.getByTestId("skill-wiz-allowed-tools") as HTMLInputElement;
      await userEvent.type(toolsInput, "Bash(python:*) WebFetch");
      expect(toolsInput.value).toBe("Bash(python:*) WebFetch");
    });
  });

  describe("Step 4: Trigger Testing", () => {
    async function goToTriggerStep(): Promise<void> {
      renderWizard();
      await userEvent.type(screen.getByTestId("skill-wiz-name"), "test-skill");
      await userEvent.type(screen.getByTestId("skill-wiz-owner"), "@team");
      await userEvent.click(screen.getByTestId("skill-wizard-next"));
      await userEvent.type(screen.getByTestId("skill-wiz-content"), "Content here.");
      await userEvent.click(screen.getByTestId("skill-wizard-next"));
      await userEvent.click(screen.getByTestId("skill-wizard-next"));
    }

    it("renders trigger and anti-trigger inputs", async () => {
      await goToTriggerStep();
      expect(screen.getByTestId("skill-step-triggers")).toBeInTheDocument();
      expect(screen.getByTestId("skill-wiz-trigger-input")).toBeInTheDocument();
      expect(screen.getByTestId("skill-wiz-anti-input")).toBeInTheDocument();
    });

    it("adds should-trigger phrases", async () => {
      await goToTriggerStep();
      await userEvent.type(screen.getByTestId("skill-wiz-trigger-input"), "help me plan a sprint");
      await userEvent.click(screen.getByTestId("skill-wiz-add-trigger"));
      expect(screen.getByTestId("skill-wiz-triggers")).toBeInTheDocument();
      expect(screen.getByText("help me plan a sprint")).toBeInTheDocument();
    });

    it("adds should-not-trigger phrases", async () => {
      await goToTriggerStep();
      await userEvent.type(screen.getByTestId("skill-wiz-anti-input"), "what is the weather");
      await userEvent.click(screen.getByTestId("skill-wiz-add-anti"));
      expect(screen.getByTestId("skill-wiz-anti-triggers")).toBeInTheDocument();
      expect(screen.getByText("what is the weather")).toBeInTheDocument();
    });

    it("removes trigger phrases", async () => {
      await goToTriggerStep();
      await userEvent.type(screen.getByTestId("skill-wiz-trigger-input"), "test phrase");
      await userEvent.click(screen.getByTestId("skill-wiz-add-trigger"));
      await userEvent.click(screen.getByLabelText("Remove trigger test phrase"));
      expect(screen.queryByText("test phrase")).not.toBeInTheDocument();
    });
  });

  describe("Step 5: Review & Create", () => {
    async function goToReviewStep(): Promise<void> {
      renderWizard();
      // Step 1
      await userEvent.type(screen.getByTestId("skill-wiz-name"), "review-skill");
      await userEvent.type(screen.getByTestId("skill-wiz-owner"), "@reviewer");
      await userEvent.click(screen.getByTestId("skill-wizard-next"));
      // Step 2
      await userEvent.type(screen.getByTestId("skill-wiz-summary"), "A test summary");
      await userEvent.type(screen.getByTestId("skill-wiz-content"), "Think step by step.");
      await userEvent.click(screen.getByTestId("skill-wizard-next"));
      // Step 3
      await userEvent.click(screen.getByTestId("skill-wizard-next"));
      // Step 4
      await userEvent.click(screen.getByTestId("skill-wizard-next"));
    }

    it("renders checklist and summary", async () => {
      await goToReviewStep();
      expect(screen.getByTestId("skill-step-review")).toBeInTheDocument();
      expect(screen.getByTestId("skill-wiz-checklist")).toBeInTheDocument();
      expect(screen.getByTestId("skill-wiz-summary")).toBeInTheDocument();
    });

    it("shows correct values in summary grid", async () => {
      await goToReviewStep();
      const summary = screen.getByTestId("skill-wiz-summary");
      expect(within(summary).getByText("review-skill")).toBeInTheDocument();
      expect(within(summary).getByText("@reviewer")).toBeInTheDocument();
      expect(within(summary).getByText("Custom")).toBeInTheDocument();
    });

    it("shows passing checklist items", async () => {
      await goToReviewStep();
      const checklist = screen.getByTestId("skill-wiz-checklist");
      expect(within(checklist).getByText("Name is kebab-case")).toBeInTheDocument();
      expect(within(checklist).getByText("Content is provided")).toBeInTheDocument();
    });

    it("shows content preview", async () => {
      await goToReviewStep();
      expect(screen.getByText("Think step by step.")).toBeInTheDocument();
    });
  });

  describe("Full wizard submission", () => {
    it("creates a skill with all fields and closes", async () => {
      let postedPayload: Record<string, unknown> | null = null;
      server.use(
        http.get(`${API_BASE}/skills`, () => HttpResponse.json(envelope([]))),
        http.post(`${API_BASE}/skills`, async ({ request }) => {
          postedPayload = (await request.json()) as Record<string, unknown>;
          return HttpResponse.json(envelope(makeSkill({ name: postedPayload.name })), {
            status: 201,
          });
        }),
      );

      let closed = false;
      renderWizard(() => { closed = true; });

      // Step 1: Identity
      await userEvent.type(screen.getByTestId("skill-wiz-name"), "sprint-planner");
      await userEvent.type(screen.getByTestId("skill-wiz-owner"), "@ops");
      await userEvent.click(screen.getByTestId("skill-wiz-category-workflow_automation"));
      // Add a tag
      await userEvent.type(screen.getByTestId("skill-wiz-tag-input"), "planning");
      await userEvent.click(screen.getByTestId("skill-wiz-add-tag"));
      await userEvent.click(screen.getByTestId("skill-wizard-next"));

      // Step 2: Content
      await userEvent.type(screen.getByTestId("skill-wiz-summary"), "Sprint planning helper");
      await userEvent.type(screen.getByTestId("skill-wiz-content"), "# Instructions\n\nPlan the sprint.");
      await userEvent.click(screen.getByTestId("skill-wizard-next"));

      // Step 3: Composability — add allowed tools
      await userEvent.type(screen.getByTestId("skill-wiz-allowed-tools"), "Bash(python:*)");
      await userEvent.click(screen.getByTestId("skill-wizard-next"));

      // Step 4: Triggers — add one
      await userEvent.type(screen.getByTestId("skill-wiz-trigger-input"), "plan my sprint");
      await userEvent.click(screen.getByTestId("skill-wiz-add-trigger"));
      await userEvent.click(screen.getByTestId("skill-wizard-next"));

      // Step 5: Submit
      await userEvent.click(screen.getByTestId("skill-wizard-submit"));

      await waitFor(() => expect(postedPayload).not.toBeNull());
      expect(postedPayload!.name).toBe("sprint-planner");
      expect(postedPayload!.owner).toBe("@ops");
      expect(postedPayload!.category).toBe("workflow_automation");
      expect(postedPayload!.tags).toEqual(["planning"]);
      expect(postedPayload!.summary).toBe("Sprint planning helper");
      expect(postedPayload!.content).toBe("# Instructions\n\nPlan the sprint.");
      expect(postedPayload!.allowed_tools).toBe("Bash(python:*)");
      const metadata = postedPayload!.skill_metadata as Record<string, unknown>;
      expect(metadata.test_triggers).toEqual({
        should_trigger: ["plan my sprint"],
        should_not_trigger: [],
      });
      const openaiPackage = metadata.openai_package as Record<string, unknown>;
      expect(openaiPackage.format).toBe("openai-skill");
      expect(openaiPackage.source).toBe("wizard");
      const agents = openaiPackage.agents as Record<string, unknown>;
      const agentInterface = agents.interface as Record<string, unknown>;
      expect(agentInterface.display_name).toBe("Sprint Planner");
      expect(agentInterface.short_description).toBe("Sprint planning helper");
      expect(agentInterface.default_prompt).toContain("$sprint-planner");

      await waitFor(() => expect(closed).toBe(true));
    });

    it("shows error when creation fails", async () => {
      server.use(
        http.get(`${API_BASE}/skills`, () => HttpResponse.json(envelope([]))),
        http.post(`${API_BASE}/skills`, () =>
          HttpResponse.json({ detail: "name already exists" }, { status: 409 }),
        ),
      );

      renderWizard();
      await userEvent.type(screen.getByTestId("skill-wiz-name"), "dup-skill");
      await userEvent.type(screen.getByTestId("skill-wiz-owner"), "@team");
      await userEvent.click(screen.getByTestId("skill-wizard-next"));
      await userEvent.type(screen.getByTestId("skill-wiz-content"), "Content.");
      await userEvent.click(screen.getByTestId("skill-wizard-next"));
      await userEvent.click(screen.getByTestId("skill-wizard-next"));
      await userEvent.click(screen.getByTestId("skill-wizard-next"));
      await userEvent.click(screen.getByTestId("skill-wizard-submit"));

      expect(await screen.findByTestId("skill-wizard-error")).toBeInTheDocument();
    });
  });
});

describe("Skills page with wizard integration", () => {
  it("opens the wizard from the Build Skill action", async () => {
    server.use(
      http.get(`${API_BASE}/skills`, () => HttpResponse.json(envelope([]))),
    );
    renderSkillsPage();
    await userEvent.click(await screen.findByRole("button", { name: "Build Skill" }));
    expect(screen.getByTestId("skill-wizard")).toBeInTheDocument();
    expect(screen.getByTestId("skill-step-identity")).toBeInTheDocument();
  });

  it("lists skills with category badges", async () => {
    server.use(
      http.get(`${API_BASE}/skills`, () =>
        HttpResponse.json(envelope([makeSkill()])),
      ),
    );
    renderSkillsPage();
    const name = await screen.findByText("reasoning-v1");
    // "Custom" now appears both as a category filter chip and the card's
    // category pill — scope to the skill card so we assert the badge on the card.
    const card = name.closest("[data-testid^='skill-card-']") as HTMLElement;
    expect(within(card).getByText("Custom")).toBeInTheDocument();
  });

  it("opens a skill into its Workspace and renders Render Preview with manual variables", async () => {
    let renderBody: Record<string, unknown> | null = null;
    server.use(
      http.get(`${API_BASE}/skills`, () =>
        HttpResponse.json(envelope([makeSkill({ content: "Use {{topic}} with care." })])),
      ),
      http.get(`${API_BASE}/skills/SK-test1`, () =>
        HttpResponse.json(envelope(makeSkill({ content: "Use {{topic}} with care." }))),
      ),
      http.get(`${API_BASE}/skills/SK-test1/workspace`, () =>
        HttpResponse.json(
          envelope({
            version: 1,
            category: "custom",
            status: "active",
            lifecycle: "Tested",
            last_run: null,
            baseline_run_id: null,
            baseline_run: null,
            bound_to: null,
          }),
        ),
      ),
      http.post(`${API_BASE}/skills/SK-test1/test-render`, async ({ request }) => {
        renderBody = (await request.json()) as Record<string, unknown>;
        return HttpResponse.json(
          envelope({
            skill_id: "SK-test1",
            skill_name: "reasoning-v1",
            rendered_content: "Use refunds with care.",
            original_content: "Use {{topic}} with care.",
            detected_variables: ["topic"],
            unresolved_variables: [],
            variables_applied: { topic: "refunds" },
            summary: "Chain-of-thought reasoning rubric",
            word_count: 4,
            char_count: 22,
            duration_ms: 3,
          }),
        );
      }),
    );

    const user = userEvent.setup();
    renderSkillsPage();
    await user.click(await screen.findByTestId("skill-open-reasoning-v1"));
    await screen.findByTestId("skill-workspace-header");

    await user.click(screen.getByRole("button", { name: "Render Preview" }));
    expect(await screen.findByTestId("skill-playground-panel")).toHaveTextContent("topic");
    fireEvent.change(screen.getByTestId("skill-playground-variables"), {
      target: { value: '{"topic":"refunds"}' },
    });
    await user.click(screen.getByTestId("skill-playground-render"));

    expect(await screen.findByText("Use refunds with care.")).toBeInTheDocument();
    expect(renderBody).toMatchObject({ variables: { topic: "refunds" } });
  });
});
