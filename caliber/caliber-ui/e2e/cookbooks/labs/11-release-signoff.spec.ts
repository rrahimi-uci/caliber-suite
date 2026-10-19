import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

import { expect, test, type Page } from "@playwright/test";

import { CaliberUi } from "../ui-only-driver";

/**
 * Cookbook 11, the UI path: a release candidate from the New candidate form to a signed decision.
 *
 * Follows docs-site/cookbooks/11-release-readiness-factory/README.md ("Run it through the UI").
 * Needs a stack started with `scripts/cookbook-env.sh up lab11 --flags release_candidates,cookbooks`
 * (CALIBER_OPERATOR_USERS=admin,lab11-operator is only needed by the API script). The browser is
 * signed in as admin, who holds the operator and admin scopes the buttons need; a second session as
 * lab11-viewer (no scopes) checks what a person without them sees. No API request context is used.
 */
const here = path.dirname(fileURLToPath(import.meta.url));
const LAB = path.resolve(
  here,
  "../../../../../docs-site/cookbooks/11-release-readiness-factory",
);
const VIEWER = { user: "lab11-viewer", password: "lab11-viewer-password" };

interface Row {
  id: string;
  name: string;
  artifact_type: string;
  artifact_ref: string;
  version_ref: string;
  rollback_version: string;
  required_score: number;
  criteria: unknown[];
  evidence: unknown[];
  expect: { weighted_score: number; status: string; blockers: string[] };
}
const rows = fs
  .readFileSync(
    path.join(LAB, "assets/dataset/release-candidates.jsonl"),
    "utf-8",
  )
  .split("\n")
  .filter((line) => line.trim())
  .map((line) => JSON.parse(line) as Row);
const byId = new Map(rows.map((row) => [row.id, row]));

async function createCandidate(
  page: Page,
  row: Row,
  name: string,
): Promise<void> {
  await page.getByRole("button", { name: "New candidate" }).click();
  await page.getByTestId("release-candidate-name").fill(name);
  await page
    .getByTestId("release-candidate-type")
    .selectOption(row.artifact_type);
  await page.getByTestId("release-candidate-artifact").fill(row.artifact_ref);
  await page.getByTestId("release-candidate-version").fill(row.version_ref);
  await page
    .getByTestId("release-candidate-rollback")
    .fill(row.rollback_version);
  await page
    .getByTestId("release-candidate-required-score")
    .fill(String(row.required_score));
  await page
    .getByTestId("release-candidate-criteria")
    .fill(JSON.stringify(row.criteria, null, 2));
  await page
    .getByTestId("release-candidate-evidence")
    .fill(JSON.stringify(row.evidence, null, 2));
  await page.getByTestId("release-candidate-create").click();
}

function card(page: Page, name: string) {
  return page
    .locator('[data-testid^="release-candidate-RC-"]')
    .filter({ hasText: name })
    .first();
}

test.describe("Cookbook 11: release signoff factory (UI)", () => {
  test("creates, waives, reports and signs candidates", async ({
    page,
    browser,
  }) => {
    test.setTimeout(300_000);
    const suffix = ` ${Date.now().toString(36)}`;
    const ui = new CaliberUi(page);
    await ui.signIn();

    // ---- Setup: the viewer account -----------------------------------------------
    await page.goto("/caliber/administration");
    const accounts = page.getByLabel("Accounts");
    if (
      (await accounts
        .getByRole("cell", { name: VIEWER.user, exact: true })
        .count()) === 0
    ) {
      await page.getByLabel("User ID").first().fill(VIEWER.user);
      await page.getByLabel("Password").first().fill(VIEWER.password);
      await page.getByRole("button", { name: "Create account" }).click();
      await expect(
        accounts.getByRole("cell", { name: VIEWER.user, exact: true }),
      ).toBeVisible();
    }

    // ---- 1. The section, and what it says about itself ---------------------------
    await page.goto("/caliber/releases");
    await expect(page.getByTestId("release-signoff-factory")).toBeVisible();
    await expect(page.getByTestId("release-signoff-record-only")).toContainText(
      "It does not deploy or roll back anything",
    );

    // ---- 2. A candidate with one blocker (RC16) -----------------------------------
    const drill = byId.get("RC16")!;
    const drillName = `${drill.name}${suffix}`;
    await createCandidate(page, drill, drillName);
    const drillCard = card(page, drillName);
    await expect(drillCard).toBeVisible();
    await expect(drillCard).toContainText("blocked");
    await expect(drillCard).toContainText("72%");
    await expect(drillCard).toContainText(
      "4 criteria · 1 evidence · 1 blockers · rollback 4",
    );
    // The blocker is unmet, so GO is not offered as a choice.
    await expect(
      drillCard.getByRole("button", { name: "Sign off GO" }),
    ).toBeDisabled();

    // ---- 3. A waiver needs a key and a reason of at least 8 characters -----------
    await drillCard
      .getByLabel("Waiver criterion key")
      .fill("rollback_rehearsed");
    await drillCard.getByLabel("Waiver reason").fill("short");
    await expect(
      drillCard.getByRole("button", { name: "Record waiver" }),
    ).toBeDisabled();
    await drillCard
      .getByLabel("Waiver reason")
      .fill(
        "Rollback rehearsal booked for the 25th; on-call holds the previous image. Owner: release-eng.",
      );
    await drillCard.getByRole("button", { name: "Record waiver" }).click();
    await expect(drillCard).toContainText("ready");
    await expect(drillCard).toContainText("0 blockers");
    await expect(drillCard).toContainText("72%"); // a waiver does not change the score

    // ---- 4. Re-evaluate changes nothing --------------------------------------------
    await drillCard.getByRole("button", { name: "Re-evaluate" }).click();
    await expect(drillCard).toContainText("ready");
    await expect(drillCard).toContainText("72%");

    // ---- 5. The Allure report job ---------------------------------------------------
    await drillCard.getByRole("button", { name: "Generate Allure" }).click();
    await expect(drillCard).toContainText(
      /Allure report RRJ-[0-9a-f]+ generated and retained\./,
    );

    // ---- 6. Sign off GO: the rationale needs 8 characters ---------------------------
    await drillCard.getByLabel("Signoff rationale").fill("ok");
    await expect(
      drillCard.getByRole("button", { name: "Sign off GO" }),
    ).toBeDisabled();
    await drillCard
      .getByLabel("Signoff rationale")
      .fill(
        "Strong scores, rollback waived with an owner and a date; image held by on-call.",
      );
    await drillCard.getByRole("button", { name: "Sign off GO" }).click();
    await expect(drillCard).toContainText("signed_go");
    // A signed candidate is final: no decision controls remain.
    await expect(drillCard.getByLabel("Signoff rationale")).toHaveCount(0);
    await expect(
      drillCard.getByRole("button", { name: "Sign off GO" }),
    ).toHaveCount(0);

    // ---- 7. A blocked candidate (RC05) can only be signed NO-GO ---------------------
    const blocked = byId.get("RC05")!;
    const blockedName = `${blocked.name}${suffix}`;
    await createCandidate(page, blocked, blockedName);
    const blockedCard = card(page, blockedName);
    await expect(blockedCard).toContainText("blocked");
    await expect(blockedCard).toContainText("95%");
    await blockedCard
      .getByLabel("Signoff rationale")
      .fill(
        "Human review of the changed cases is not done; the score is high but the gate is unmet.",
      );
    await expect(
      blockedCard.getByRole("button", { name: "Sign off GO" }),
    ).toBeDisabled();
    await blockedCard.getByRole("button", { name: "Sign off NO-GO" }).click();
    await expect(blockedCard).toContainText("signed_no_go");

    // ---- 8. Everything survives a reload ---------------------------------------------
    await page.reload();
    await expect(card(page, drillName)).toContainText("signed_go");
    await expect(card(page, blockedName)).toContainText("signed_no_go");

    // ---- Failure: a person without the scopes gets no actions (and does not see another person's candidates) --------------------
    const context = await browser.newContext();
    const viewerPage = await context.newPage();
    await new CaliberUi(viewerPage).signIn(VIEWER.user, VIEWER.password);
    await viewerPage.goto("/caliber/releases");
    await expect(
      viewerPage.getByTestId("release-signoff-factory"),
    ).toBeVisible();
    // A candidate created outside a workspace is private to its creator: the viewer does not see it.
    await expect(card(viewerPage, drillName)).toHaveCount(0);
    await expect(
      viewerPage.getByRole("button", { name: "New candidate" }),
    ).toHaveCount(0);
    await expect(
      viewerPage.getByRole("button", { name: "Generate Allure" }),
    ).toHaveCount(0);
  });
});
