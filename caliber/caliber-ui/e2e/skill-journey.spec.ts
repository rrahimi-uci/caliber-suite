import { expect, test } from "@playwright/test";

import { signIn } from "./helpers";

/**
 * The skill journey, driven the way a user would: author a skill, preview how it renders,
 * calibrate it, review the candidate, apply it, and roll back (alpha gap P1-20).
 *
 * Nothing is seeded. It runs on the harness defaults: fake LLM, fake evaluator, background
 * workers on. One step is done by API rather than through the page: the skill is created
 * with `POST /skills`, the endpoint the five-step wizard saves to, because the wizard is
 * a form with sixteen categories and a dozen optional panels and the journey is about what
 * happens next.
 *
 * What it proves, in order:
 *  1. The Render Preview stage substitutes variables into the skill.
 *  2. Calibrate queues a run that reaches a reviewable candidate.
 *  3. The review says the evaluation is simulated and counts zero examples (a skill
 *     calibration has no test set, and the page does not pretend otherwise).
 *  4. Apply makes the candidate the skill's content as version 2, and says so.
 *  5. Rollback restores the original content as version 3 (skills are forward-only), in
 *     the page and in the API.
 */
const API = "/ajax-api/2.0/mlflow/caliber";
const ORIGINAL = "Reply politely to the customer about {{topic}}.";

test.describe("Skill journey (alpha)", () => {
  test("renders a skill, calibrates it, applies the candidate and rolls back", async ({
    page,
  }) => {
    test.setTimeout(240_000);
    const name = `journey-skill-${Date.now().toString(36)}`;

    await signIn(page);

    // ---- 0. Author: the skill exists as version 1 (API; see the header) ------
    const created = await page.request.post(`${API}/skills`, {
      data: {
        name,
        description: "Handles refund questions.",
        summary: "Use when a customer asks about a refund.",
        content: ORIGINAL,
        owner: "admin",
      },
    });
    expect(created.ok(), await created.text()).toBeTruthy();
    const skillId = (await created.json()).data.skill_id as string;

    await page.goto("/caliber/skills");
    await page.getByTestId(`skill-open-${name}`).click();
    await expect(page.getByTestId("version-row-1")).toBeVisible();

    // ---- 1. Render Preview ---------------------------------------------------
    await page
      .getByRole("button", { name: "Render Preview", exact: true })
      .click();
    await page
      .getByTestId("skill-playground-variables")
      .fill('{"topic":"refunds"}');
    await page.getByTestId("skill-playground-render").click();
    await expect(
      page.getByRole("heading", { name: "Rendered Output" }),
    ).toBeVisible();
    await expect(
      page.getByText("Reply politely to the customer about refunds."),
    ).toBeVisible();

    // ---- 2. Calibrate: the worker reaches a candidate ------------------------
    await page.getByRole("button", { name: "Runs", exact: true }).click();
    await page.getByTestId("skill-calibrate-btn").click();
    await expect(page.getByTestId("skill-calibrate-result")).toContainText(
      "Calibration queued",
    );
    const review = page.getByTestId(/^skill-review-RFN-/).first();
    await expect(review).toBeVisible({ timeout: 120_000 });

    // ---- 3. Review: simulated evidence, honestly counted ---------------------
    await review.click();
    await expect(page.getByTestId("skill-simulated-evaluation")).toContainText(
      "Simulated evaluation — not evidence",
    );
    await expect(page.getByText("0 evaluation examples")).toBeVisible();
    await expect(page.getByTestId("skill-candidate-diff")).toContainText(
      "Refinement added by SkillMetaPrompt",
    );

    // ---- 4. Apply: the candidate becomes version 2 ---------------------------
    await page
      .getByTestId(/^skill-apply-RFN-/)
      .first()
      .click();
    await expect(page.getByTestId("skill-apply-result")).toContainText(
      "the skill is now version 2",
      { timeout: 60_000 },
    );
    expect(await skillContent(page, skillId)).toContain(
      "Refinement added by SkillMetaPrompt",
    );

    await page.getByRole("button", { name: "Author", exact: true }).click();
    await expect(
      page.getByTestId("version-row-2").getByTestId("version-live-badge"),
    ).toBeVisible();

    // ---- 5. Roll back: the original content is current again, as version 3 ---
    await page
      .getByTestId("version-row-2")
      .getByTestId("version-rollback")
      .click();
    await expect(page.getByTestId("confirm-dialog")).toBeVisible();
    await page.getByTestId("confirm-dialog-confirm").click();
    await expect(page.getByTestId("confirm-dialog")).toBeHidden();

    await expect(
      page.getByTestId("version-row-3").getByTestId("version-live-badge"),
    ).toBeVisible();
    await expect
      .poll(async () => skillContent(page, skillId), { timeout: 20_000 })
      .toBe(ORIGINAL);
  });
});

/** The skill's current content, read from the API. */
async function skillContent(
  page: import("@playwright/test").Page,
  skillId: string,
): Promise<string | null> {
  const response = await page.request.get(`${API}/skills/${skillId}`);
  if (!response.ok()) return null;
  return ((await response.json()) as { data: { content: string } }).data
    .content;
}
