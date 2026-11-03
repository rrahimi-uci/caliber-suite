import { expect, test } from "@playwright/test";

import { CaliberUi, uiOnlySlug } from "../ui-only-driver";

/**
 * Cookbook 22, "Run it through the UI": the governed evidence-review journey.
 *
 * This is intentionally a blocked acceptance scaffold on the current main baseline. The
 * dependency named by issue #869, #863 (the Lab 22 package), is not merged, and #857 (the V01
 * governed-effect scenario that supplies the deterministic Harborlight run) is also not merged.
 * Current main therefore has no Lab 22 catalog entry or browser-visible fixture that can submit
 * the run. It would be incorrect to seed a synthetic run here or to invent a second route/API.
 *
 * The existing UI seams that this spec must use after those dependencies land are deliberately
 * recorded below: `/caliber/cookbooks` and the generic workflow-run/approval surfaces. The
 * evidence download and historical replay are currently backend routes only; they have no
 * browser controls or UI API methods on this baseline. The implementation must add or consume
 * those producer seams before this fixme is removed.
 *
 * Required journey, from the Lab 22 README and issue #869:
 *   1. editor submits the Harborlight evidence-review run and sees the exact proposal diff;
 *   2. the editor cannot approve it; a distinct approver approves the proposal;
 *   3. the operator resumes the run and it reaches `accepted`;
 *   4. evidence export downloads the scope-bound archive;
 *   5. replay of that captured evidence reports `matched` without a second mutation;
 *   6. where the installed harness exposes the downstream, project isolation and stale `412`
 *      behavior are checked without creating a parallel downstream route.
 */
const LAB22_ID = "22";
const LAB22_TITLE = "Governed Evidence Review";
const DEPENDENCY_BLOCKER =
  "Blocked until #863 and #857 are merged: the current main baseline has no Lab 22 catalog/workflow fixture, and the browser lacks evidence-export/replay controls. Do not fabricate runtime APIs or seed a parallel run.";

test.describe(`Cookbook ${LAB22_ID}: governed evidence review`, () => {
  test.fixme(true, DEPENDENCY_BLOCKER);

  test("editor submits, approver reviews and approves, then evidence replays as matched", async ({
    page,
  }) => {
    test.setTimeout(600_000);
    const workflowName = `cookbook-22-${uiOnlySlug("harborlight")}`;
    const ui = new CaliberUi(page);

    // This is the first real seam available after the Lab 22 producer is merged. Keep the
    // catalog assertion here so a future implementation cannot silently fall back to another
    // cookbook or a hand-built manifest.
    // Use the harness' existing default account until the Lab 22 package defines the exact
    // editor/approver provisioning contract. Do not invent user ids or passwords here.
    await ui.signIn();
    await page.goto("/caliber/cookbooks");
    await expect(
      page.getByRole("heading", { name: "Cookbooks" }),
    ).toBeVisible();
    const card = page.getByTestId(`cookbook-card-${LAB22_ID}`);
    await expect(card).toBeVisible();
    await expect(card).toContainText(LAB22_TITLE);
    await page.getByTestId(`install-cookbook-${LAB22_ID}`).click();
    await page.getByTestId("cookbook-install-name").fill(workflowName);
    const acknowledgement = page.getByTestId("cookbook-prerequisites-ack");
    if (await acknowledgement.isVisible()) await acknowledgement.check();
    await page.getByTestId("confirm-cookbook-install").click();

    // The remaining steps are intentionally not guessed until #857/#863 provide the exact
    // workflow manifest, identity setup, and evidence/replay UI contract. This assertion keeps
    // the scaffold tied to the existing install seam while the dependency is outstanding.
    await expect(page).toHaveURL(
      /\/caliber\/workflows\/WF-[^/]+\/editor\/WFV-/,
    );
  });
});
