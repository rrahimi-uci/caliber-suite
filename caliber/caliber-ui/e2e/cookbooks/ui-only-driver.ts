import { expect, test, type Page } from "@playwright/test";

export interface UiOnlyCookbook {
  id: string;
  title: string;
  execute(ui: CaliberUi): Promise<void>;
}

export interface SkillRecipe {
  name: string;
  owner: string;
  summary: string;
  content: string;
  renderVariables: Record<string, unknown>;
  positiveTrigger: string;
  negativeTrigger: string;
}

export interface ReviewQueueRecipe {
  name: string;
  description: string;
  reviewer: string;
  question: {
    key: string;
    title: string;
  };
}

export function uiOnlySlug(prefix: string): string {
  return `${prefix}-${Date.now()}-${Math.random().toString(36).slice(2, 7)}`.toLowerCase();
}

/**
 * Browser-only CALIBER driver for Cookbook automation.
 *
 * This class intentionally has no API request context, fixture seeding, database
 * calls, or manifest imports. Every platform mutation passes through the same UI
 * a human operator uses. Keep new Cookbook adapters at this boundary.
 */
export class CaliberUi {
  constructor(readonly page: Page) {}

  async signIn(username = "admin", password = "admin"): Promise<void> {
    await this.page.goto("/caliber/login");
    await expect(
      this.page.getByPlaceholder("Enter your username"),
    ).toBeVisible();
    await this.page.getByPlaceholder("Enter your username").fill(username);
    await this.page.getByPlaceholder("Enter your password").fill(password);
    await this.page.getByRole("button", { name: "Sign in" }).click();
    await expect(this.page).toHaveURL(/\/caliber\/?$/);
    await expect(this.page.getByLabel("CALIBER navigation")).toBeVisible();
  }

  async createSkill(recipe: SkillRecipe): Promise<void> {
    await this.page.goto("/caliber/skills");
    await expect(
      this.page.getByRole("heading", { name: "Skills" }),
    ).toBeVisible();
    await this.page.getByTestId("new-skill").click();
    await expect(
      this.page.getByRole("heading", { name: "Build a new skill" }),
    ).toBeVisible();

    await this.page.getByTestId("skill-wiz-name").fill(recipe.name);
    await this.page.getByTestId("skill-wiz-owner").fill(recipe.owner);
    await this.page.getByTestId("skill-wiz-category-customer_support").click();
    await this.page.getByTestId("skill-wiz-tag-input").fill("cookbook-ui-only");
    await this.page.getByTestId("skill-wiz-add-tag").click();
    await this.page.getByTestId("skill-wizard-next").click();

    await this.page.getByTestId("skill-wiz-summary").fill(recipe.summary);
    await this.page.getByTestId("skill-wiz-content").fill(recipe.content);
    await this.page.getByTestId("skill-wizard-next").click();
    await this.page.getByTestId("skill-wizard-next").click();

    await this.page
      .getByTestId("skill-wiz-trigger-input")
      .fill(recipe.positiveTrigger);
    await this.page.getByTestId("skill-wiz-add-trigger").click();
    await this.page.getByTestId("skill-wizard-next").click();
    await this.page.getByTestId("skill-wizard-submit").click();

    await expect(
      this.page.getByRole("heading", { name: "Skills" }),
    ).toBeVisible();
    await this.page.getByLabel("Search skills").fill(recipe.name);
    await expect(
      this.page.getByText(recipe.name, { exact: true }),
    ).toBeVisible();
    await this.page.getByRole("button", { name: "Open" }).click();
    await expect(this.page.getByTestId("skill-workspace-header")).toContainText(
      recipe.name,
    );
  }

  async proveSkillBehavior(recipe: SkillRecipe): Promise<void> {
    await this.page.getByRole("button", { name: "Render Preview" }).click();
    await this.page
      .getByTestId("skill-playground-variables")
      .fill(JSON.stringify(recipe.renderVariables));
    await this.page.getByTestId("skill-playground-render").click();
    await expect(this.page.getByText("Rendered Output")).toBeVisible();

    await this.page.getByRole("button", { name: "Trigger Tests" }).click();
    const results = this.page.getByTestId("skill-trigger-result");
    const positiveCount = await results.count();
    await this.page
      .getByTestId("skill-trigger-message")
      .fill(recipe.positiveTrigger);
    await this.page.getByTestId("skill-trigger-run").click();
    await expect(results).toHaveCount(positiveCount + 1);
    const positiveResult = results.filter({ hasText: recipe.positiveTrigger });
    await expect(positiveResult).toHaveCount(1);
    await expect(
      positiveResult.getByTestId("skill-trigger-selected"),
    ).toContainText("selected");

    const negativeCount = await results.count();
    await this.page
      .getByTestId("skill-trigger-message")
      .fill(recipe.negativeTrigger);
    await this.page.getByTestId("skill-trigger-run").click();
    await expect(results).toHaveCount(negativeCount + 1);
    const negativeResult = results.filter({ hasText: recipe.negativeTrigger });
    await expect(negativeResult).toHaveCount(1);
    await expect(
      negativeResult.getByTestId("skill-trigger-selected"),
    ).toContainText("not selected");
    await this.page.getByTestId("skill-trigger-save").click();
  }

  async archiveCurrentSkill(name: string): Promise<void> {
    await this.page.getByRole("button", { name: "Back to skills" }).click();
    await this.page.getByLabel("Search skills").fill(name);
    const card = this.page
      .locator('[data-testid^="skill-card-"]')
      .filter({ hasText: name });
    await expect(card).toHaveCount(1);
    await card.getByRole("button", { name: "Archive" }).click();
    await expect(card.getByRole("button", { name: "Restore" })).toBeVisible();
  }

  async createReviewQueue(recipe: ReviewQueueRecipe): Promise<void> {
    await this.page.goto("/caliber/review-queues");
    await expect(
      this.page.getByRole("heading", { name: "Review Queues" }),
    ).toBeVisible();
    await this.page.getByRole("button", { name: "+ New Queue" }).click();
    const panel = this.page
      .getByRole("heading", { name: "New review queue" })
      .locator("..");
    await panel.getByPlaceholder("answer-quality").fill(recipe.name);
    await panel.getByPlaceholder("@sarah, @alex").fill(recipe.reviewer);
    await panel
      .getByPlaceholder("Human review of answer correctness and tone.")
      .fill(recipe.description);
    await panel.getByPlaceholder("key").fill(recipe.question.key);
    await panel
      .getByPlaceholder("Question shown to the reviewer")
      .fill(recipe.question.title);
    await panel.getByRole("button", { name: "Create queue" }).click();
    await expect(
      this.page.getByRole("button", { name: recipe.name }),
    ).toBeVisible();
  }
}

export async function runUiOnlyCookbook(
  page: Page,
  cookbook: UiOnlyCookbook,
): Promise<void> {
  const ui = new CaliberUi(page);
  await test.step(`${cookbook.id}: sign in through the UI`, async () =>
    ui.signIn());
  await test.step(`${cookbook.id}: ${cookbook.title}`, async () =>
    cookbook.execute(ui));
}
