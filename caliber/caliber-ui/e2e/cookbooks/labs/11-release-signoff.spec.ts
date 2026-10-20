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
    test.setTimeout(600_000);
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

    // ---- 2. Create all 20 candidates from the dataset; the server's arithmetic must match ----
    const names = new Map<string, string>();
    for (const row of rows) {
      const name = `${row.name}${suffix}`;
      names.set(row.id, name);
      await createCandidate(page, row, name);
      const created = card(page, name);
      await expect(created).toBeVisible();
      await expect(created).toContainText(row.expect.status);
      await expect(created).toContainText(
        `${Math.round(row.expect.weighted_score * 100)}%`,
      );
      await expect(created).toContainText(
        `${row.expect.blockers.length} blockers`,
      );
      for (const key of row.expect.blockers) {
        await expect(
          created.getByTestId("release-candidate-blockers"),
        ).toContainText(key);
      }
    }
    const ready = rows.filter((row) => row.expect.status === "ready").length;
    expect([ready, rows.length - ready]).toEqual([14, 6]);

    // ---- 3. RC16: one blocker, named on the card; a waiver clears it, not the score ----------
    const drill = byId.get("RC16")!;
    const drillName = names.get("RC16")!;
    const drillCard = card(page, drillName);
    await expect(drillCard).toContainText("blocked");
    await expect(drillCard).toContainText("72%");
    await expect(drillCard).toContainText(
      "4 criteria · 1 evidence · 1 blockers · rollback 4",
    );
    await expect(
      drillCard.getByTestId("release-candidate-blockers"),
    ).toContainText("Blocked by rollback_rehearsed");
    // The blocker is unmet, so GO is not offered as a choice.
    await expect(
      drillCard.getByRole("button", { name: "Sign off GO" }),
    ).toBeDisabled();
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
    const expiry = new Date(Date.now() + 14 * 86_400_000)
      .toISOString()
      .slice(0, 10);
    await drillCard.getByLabel("Waiver expiry (optional)").fill(expiry);
    await drillCard.getByRole("button", { name: "Record waiver" }).click();
    await expect(drillCard).toContainText("ready");
    await expect(drillCard).toContainText("0 blockers");
    await expect(drillCard).toContainText("72%"); // a waiver does not change the score
    await expect(
      drillCard.getByTestId("release-candidate-waivers"),
    ).toContainText(`Waived rollback_rehearsed by admin, until ${expiry}`);

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
    const blockedName = names.get("RC05")!;
    const blockedCard = card(page, blockedName);
    await expect(blockedCard).toContainText("blocked");
    await expect(blockedCard).toContainText("95%");
    await expect(
      blockedCard.getByTestId("release-candidate-blockers"),
    ).toContainText("human_review");
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

    // ---- 7b. RC17: both blockers waived, still blocked: a waiver cannot raise a score ------
    const lowCard = card(page, names.get("RC17")!);
    for (const key of ["regression_vs_baseline", "human_review"]) {
      await lowCard.getByLabel("Waiver criterion key").fill(key);
      await lowCard
        .getByLabel("Waiver reason")
        .fill(`Accepted for the drill: ${key}, owner release-eng.`);
      await lowCard.getByRole("button", { name: "Record waiver" }).click();
      await expect(
        lowCard.getByTestId("release-candidate-waivers"),
      ).toContainText(key);
    }
    await expect(lowCard).toContainText("0 blockers");
    await expect(lowCard).toContainText("blocked");
    await expect(
      lowCard.getByTestId("release-candidate-under-bar"),
    ).toContainText("is under the required");
    await lowCard
      .getByLabel("Signoff rationale")
      .fill("Trying to ship a low score on waivers alone.");
    await expect(
      lowCard.getByRole("button", { name: "Sign off GO" }),
    ).toBeDisabled();

    // ---- 7c. Modify -> new version -> re-run -> compare ------------------------------------
    // Candidates are records: none can be edited. The fix for RC16's blocker is the rehearsal
    // itself, so the new package version gets a NEW candidate with the rehearsal's real score.
    const fixed: Row = {
      ...drill,
      version_ref: "6",
      rollback_version: "5",
      criteria: (drill.criteria as { key: string; score: number }[]).map(
        (criterion) =>
          criterion.key === "rollback_rehearsed"
            ? { ...criterion, score: 1 }
            : criterion,
      ),
    };
    const fixedName = `${drill.name} v6${suffix}`;
    await createCandidate(page, fixed, fixedName);
    const fixedCard = card(page, fixedName);
    await expect(fixedCard).toContainText("ready");
    await expect(fixedCard).toContainText("0 blockers");
    await expect(
      fixedCard.getByTestId("release-candidate-waivers"),
    ).toHaveCount(0);
    const pct = async (target: ReturnType<typeof card>) =>
      Number(/(\d+)%/.exec(await target.innerText())?.[1]);
    // the score went up with the evidence (72% before, higher now), with no waiver needed
    expect(await pct(fixedCard)).toBeGreaterThan(await pct(drillCard));

    // ---- 8. Everything survives a reload ---------------------------------------------
    await page.reload();
    await expect(card(page, drillName)).toContainText("signed_go");
    await expect(card(page, blockedName)).toContainText("signed_no_go");

    // ---- 9. Admin > Audit Log names who did what ------------------------------------------------
    await page.goto("/caliber/audit-log");
    for (const action of [
      "create_release_candidate",
      "waive_release_blocker",
      "generate_release_allure_report",
      "signoff_release_candidate",
    ]) {
      await page.getByTestId("audit-filter-action").fill(action);
      const entry = page.getByTestId("audit-row").first();
      await expect(entry).toContainText(action, { timeout: 30_000 });
      await expect(entry).toContainText("admin");
    }

    // ---- 10. A signed GO deploys nothing: the real release is Make live on the prompt ------------
    const promptName = `payments-assistant${suffix.replace(" ", "-")}`;
    await page.goto("/caliber/prompts");
    await page.getByRole("button", { name: "New prompt" }).click();
    await page.getByRole("button", { name: /Write \/ paste/ }).click();
    await page
      .getByLabel("Prompt text")
      .fill("You answer payments questions from the policy text only.\n");
    await page.getByLabel("Prompt name").fill(promptName);
    await page.getByRole("button", { name: "Create and Open" }).click();
    await expect(
      page.getByTestId("version-row-1").getByTestId("version-live-badge"),
    ).toBeVisible();
    // version 2 is saved from the card's Edit; saving never changes what is live
    await page.goto("/caliber/prompts");
    await page.getByLabel("Search prompts").fill(promptName);
    const promptCard = page
      .locator('[data-testid^="prompt-card-"]')
      .filter({ hasText: promptName })
      .first();
    await promptCard.getByRole("button", { name: "Edit" }).click();
    await page
      .locator("form textarea")
      .first()
      .fill(
        "You answer payments questions from the policy text only, and cite it.\n",
      );
    await page.getByTestId("prompt-edit-save").click();
    await expect(page.getByText(/Saved v2/)).toBeVisible();

    // a ready candidate for version 2 (RC01's scores), signed GO
    const goRow: Row = {
      ...byId.get("RC01")!,
      artifact_type: "prompt",
      artifact_ref: promptName,
      version_ref: "2",
      rollback_version: "1",
    };
    await page.goto("/caliber/releases");
    await page.getByTestId("releases-live-board").waitFor();
    await expect(page.getByTestId(`releases-live-${promptName}`)).toContainText(
      "v1",
    );
    const promptEvents = page
      .locator('[data-testid^="releases-event-"]')
      .filter({ hasText: promptName });
    await expect(promptEvents.first()).toContainText("v1");
    const eventsBefore = await promptEvents.count();
    const goName = `${goRow.name} signed${suffix}`;
    await createCandidate(page, goRow, goName);
    const goCard = card(page, goName);
    await expect(goCard).toContainText("ready");
    await goCard
      .getByLabel("Signoff rationale")
      .fill("Quality and review complete; rollback to v1 is ready.");
    await goCard.getByRole("button", { name: "Sign off GO" }).click();
    await expect(goCard).toContainText("signed_go");
    // nothing moved: the live board and the timeline read exactly as before the signoff
    await page.reload();
    await expect(page.getByTestId(`releases-live-${promptName}`)).toContainText(
      "v1",
    );
    await expect(
      page.getByTestId(`releases-live-${promptName}`),
    ).not.toContainText("v2");
    await expect(promptEvents).toHaveCount(eventsBefore);
    // the release is a separate act: Make live on version 2 moves it, and the timeline records it
    await page.goto("/caliber/prompts");
    await page.getByLabel("Search prompts").fill(promptName);
    await page
      .locator('[data-testid^="prompt-card-"]')
      .filter({ hasText: promptName })
      .first()
      .getByRole("button", { name: "Versions" })
      .click();
    await page.getByRole("button", { name: "Make live" }).click();
    await expect(page.getByRole("row").filter({ hasText: "v2" })).toContainText(
      "Live",
    );
    await page.goto("/caliber/releases");
    await expect(page.getByTestId(`releases-live-${promptName}`)).toContainText(
      "v2",
    );
    await expect(
      page
        .locator('[data-testid^="releases-event-"]')
        .filter({ hasText: promptName })
        .first(),
    ).toBeVisible();
    expect(
      await page
        .locator('[data-testid^="releases-event-"]')
        .filter({ hasText: promptName })
        .count(),
    ).toBeGreaterThan(eventsBefore);

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
