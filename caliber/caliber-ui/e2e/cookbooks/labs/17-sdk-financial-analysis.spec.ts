import { expect, test } from "@playwright/test";

import { CaliberUi } from "../ui-only-driver";

/**
 * Cookbook 17 is an SDK-only automation by design (`cookbook_17_financial_analysis.py`: no lab
 * folder, no catalog entry, no guided UI steps). It has no UI *journey*, because nothing in it is
 * a thing a person does in the browser. What this spec checks is the other half: that everything
 * the script creates can be found and read in the UI by the person who owns the result.
 *
 * Run the script first (see "Cookbook 17" in docs/sdk), then pass the run key it used:
 *
 *   COOKBOOK_17_RUN_KEY=my-run CALIBER_LAB_BASE_URL="$CALIBER_URL/caliber" \
 *     npx playwright test --config=playwright.labs.config.ts e2e/cookbooks/labs/17-sdk-financial-analysis.spec.ts
 *
 * Skipped when `COOKBOOK_17_RUN_KEY` is not set. Nothing here calls the API.
 */
const KEY = process.env["COOKBOOK_17_RUN_KEY"];

test.describe("Cookbook 17: what the SDK script created is inspectable in the UI", () => {
  test.skip(!KEY, "set COOKBOOK_17_RUN_KEY to the run key the script used");

  test("project, bucket, input and output objects, and the released prompt", async ({
    page,
  }) => {
    const project = `SDK Financial Analysis ${KEY}`;
    const bucket = `sdk-financial-${KEY}`;
    const prompt = `sdk-financial-analysis-${KEY}`;
    await new CaliberUi(page).signIn();

    // The project is a workspace: select it in the top bar.
    const selector = page.getByLabel("Active workspace");
    await expect(selector.locator("option", { hasText: project })).toHaveCount(
      1,
      { timeout: 30_000 },
    );
    await selector.selectOption({ label: project });
    await expect(selector.locator("option:checked")).toHaveText(project);

    // Object Store: the bucket, then the input CSV and the result JSON under the project's keys.
    await page.goto("/caliber/object-store");
    await page.getByTestId(`bucket-${bucket}`).click();
    // The script keys objects under projects/<project id>/inputs|outputs/: search every folder.
    await page.getByLabel("Search all folders").check();
    await page.getByTestId("object-search").fill("monthly-financial");
    await expect(
      page.getByTestId(
        /^object-projects\/PRJ-.+\/inputs\/monthly-financials\.csv$/,
      ),
    ).toBeVisible({ timeout: 30_000 });
    await expect(
      page.getByTestId(
        /^object-projects\/PRJ-.+\/outputs\/monthly-financial-analysis\.json$/,
      ),
    ).toBeVisible();
    // Open the result: the model's analysis, as the script validated and stored it.
    await page
      .getByTestId(
        /^object-projects\/PRJ-.+\/outputs\/monthly-financial-analysis\.json$/,
      )
      .getByTitle("View preview")
      .click();
    const preview = page.getByTestId("object-preview-modal");
    await expect(preview).toBeVisible();
    await expect(preview).toContainText('"currency": "USD"', {
      timeout: 30_000,
    });
    await expect(preview).toContainText(`"prompt_name": "${prompt}"`);
    if (process.env["C17_SHOTS"])
      await page.screenshot({ path: `${process.env["C17_SHOTS"]}/result.png` });

    // Prompts: registered and live on @prod as v1.
    await page.goto("/caliber/prompts");
    await page.getByLabel("Search prompts").fill(prompt);
    await expect(page.getByText(prompt).first()).toBeVisible({
      timeout: 30_000,
    });
    await page.getByRole("button", { name: "Open" }).first().click();
    await expect(
      page.getByTestId("version-row-1").getByTestId("version-live-badge"),
    ).toBeVisible({ timeout: 30_000 });
  });
  test("the Playground runs the released prompt (real provider only)", async ({
    page,
  }) => {
    const project = `SDK Financial Analysis ${KEY}`;
    const prompt = `sdk-financial-analysis-${KEY}`;
    await new CaliberUi(page).signIn();
    // On a simulated stack the model is not running, so there is no answer to read.
    test.skip(
      await page
        .getByTestId("provider-banner")
        .filter({ hasText: "Simulated providers" })
        .isVisible(),
      "needs a real model provider (scripts/cookbook-env.sh up ... --real-llm)",
    );
    const selector = page.getByLabel("Active workspace");
    await expect(selector.locator("option", { hasText: project })).toHaveCount(
      1,
      { timeout: 30_000 },
    );
    await selector.selectOption({ label: project });

    await page.goto("/caliber/prompts");
    await page.getByLabel("Search prompts").fill(prompt);
    await page.getByRole("button", { name: "Open" }).first().click();
    await page.getByRole("button", { name: "Playground", exact: true }).click();
    await page.getByRole("button", { name: "Start Chat Session" }).click();
    await page
      .getByPlaceholder("Type a message…")
      .fill(
        "Analyze this CSV:\nmonth,revenue,operating_expenses,payroll,cloud_compute_costs,other_expenses,total_expenses,operating_income,cash_balance,accounts_receivable,accounts_payable\n2025-01,1000,100,300,50,50,500,500,2000,300,200\n2025-02,1200,110,310,55,45,520,680,2200,310,210",
      );
    await page.getByTitle("Send message").click();
    // The prompt answers (one JSON object, per its own instructions); Aria does not draft a
    // prompt instead ("Created a prompt that ...").
    // (Read the chat's own assistant message: the prompt's instructions, shown above it, name
    // the same fields.)
    const reply = page
      .locator('[data-testid="playground-message"][data-role="assistant"]')
      .filter({ hasText: /"percentile_method"/ });
    await expect(reply).toBeVisible({ timeout: 180_000 });
    await expect(page.getByText(/Created a prompt/)).toHaveCount(0);
  });
});
