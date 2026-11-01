import { expect, test, type Page } from "@playwright/test";

import { signIn } from "./helpers";

/**
 * The exact-intent approval panel (B06.5, #743) against the real server and the real decision routes.
 *
 * The runs come from `caliber/scripts/approval_panel_e2e_seed.py` (run by `run-playwright-server.sh` before the server
 * starts): parked runs whose approval requests are bound to an effect proposal, recorded through the runtime's own
 * writers. Each scenario uses its own run, so the specs do not depend on each other's state.
 *
 * What is asserted is what a keyboard or screen-reader user gets: roles, names, descriptions, focus and the announced
 * text, plus that the browser never receives or shows an argument value.
 */

const API = "/ajax-api/2.0/mlflow/caliber";
// Carried by the seeded proposal's arguments; it must never reach the page or the view response.
const SECRET_ARGUMENT = "e2e-secret-argument-value-do-not-show";

async function openRun(page: Page, runId: string): Promise<void> {
  await signIn(page);
  await page.goto(`/caliber/workflow-runs/${runId}`);
  await expect(page.getByTestId("approval-decision-panel").first()).toBeVisible({ timeout: 45_000 });
}

function decisionResponse(page: Page, decision: "approve" | "reject") {
  return page.waitForResponse(
    (response) =>
      response.url().includes(`/approval/${decision}`) && response.request().method() === "POST",
  );
}

test.describe("approval panel", () => {
  test("shows what is being decided and never an argument value", async ({ page }) => {
    const views: string[] = [];
    page.on("response", async (response) => {
      if (/\/approvals\/RAP-[^/]+$/.test(response.url())) views.push(await response.text());
    });
    await openRun(page, "WR-E2E-QUORUM");

    const panel = page.getByTestId("approval-decision-panel");
    await expect(
      panel.getByRole("heading", {
        name: "Approval request for cases/MORT-2026-000184/document-requests",
      }),
    ).toBeVisible();
    await expect(panel.getByTestId("approval-risk-badge")).toHaveText(/Irreversible write/);
    await expect(panel.getByText("document-request", { exact: false }).first()).toBeVisible();
    await expect(panel.getByText("Valid for version 7")).toBeVisible();
    await expect(panel.getByLabel(/^Intent digest sha256:[0-9a-f]{64}$/)).toBeVisible();
    await expect(panel.getByLabel(/^Evidence digest sha256:e{64}$/)).toBeVisible();
    await expect(panel.getByLabel(/^Policy digest sha256:[0-9a-f]{64}$/)).toBeVisible();
    await expect(panel.getByTestId("approval-countdown")).toHaveText(/^\(expires in (\d+ min|\d+ h( \d+ min)?)\)$/);
    await expect(panel.getByTestId("approval-quorum")).toHaveText("0 of 2 approved");
    await expect(panel.getByTestId("approval-args")).toContainText("document_type");
    await expect(panel.getByTestId("approval-args")).toContainText("(string)");

    // The countdown sits outside both live regions.
    expect(
      await panel
        .getByTestId("approval-countdown")
        .evaluate((node) => node.closest("[aria-live], [role='alert'], [role='status']") === null),
    ).toBe(true);
    // A bound request is decided here, not with the run header's blind buttons.
    await expect(page.getByTestId("run-approve")).toHaveCount(0);
    await expect(page.getByTestId("run-reject")).toHaveCount(0);
    await expect(page.getByTestId("run-bound-approval-note")).toBeVisible();

    // The argument value is on no byte the browser received or rendered.
    expect(await page.content()).not.toContain(SECRET_ARGUMENT);
    expect(views.length).toBeGreaterThan(0);
    for (const body of views) expect(body).not.toContain(SECRET_ARGUMENT);

    await test.info().attach("approval-panel-aria-snapshot", {
      body: await panel.ariaSnapshot(),
      contentType: "text/plain",
    });
  });

  test("approves from the keyboard alone and announces the result", async ({ page }) => {
    await openRun(page, "WR-E2E-KEYBOARD");
    const panel = page.getByTestId("approval-decision-panel");
    const heading = panel.getByTestId("approval-panel-heading");
    await expect(heading).toBeFocused();

    // Tab order is the visual order: digests, the reason, then Approve and Reject.
    const approve = panel.getByRole("button", { name: "Approve" });
    let reached = false;
    for (let step = 0; step < 15 && !reached; step += 1) {
      await page.keyboard.press("Tab");
      reached = await approve.evaluate((node) => node === document.activeElement);
    }
    expect(reached).toBe(true);

    const response = decisionResponse(page, "approve");
    await page.keyboard.press("Enter");
    const answered = await response;
    expect(answered.status()).toBe(200);
    // Who decides is the authenticated principal: the body is the request id and nothing else.
    expect(Object.keys(answered.request().postDataJSON() as object)).toEqual(["runtime_approval_id"]);

    const status = panel.getByTestId("approval-status");
    await expect(status).toContainText("Approved. The quorum is met.");
    await expect(status).toBeFocused();
    await expect(status).toHaveAttribute("role", "status");
    await expect(status).toHaveAttribute("aria-live", "polite");
    // Decided and waiting for dispatch: the panel is read-only and says so.
    await expect(panel.getByRole("button", { name: "Approve" })).toHaveCount(0);
    await expect(panel.getByTestId("approval-quorum")).toHaveText("1 of 1 approved");
  });

  test("a second approval by the same person is unavailable and says why, and a reject needs a reason and a confirmation", async ({
    page,
  }) => {
    await openRun(page, "WR-E2E-QUORUM");
    const panel = page.getByTestId("approval-decision-panel");

    const approved = decisionResponse(page, "approve");
    await panel.getByLabel("Reason").fill("evidence checked");
    await panel.getByRole("button", { name: "Approve" }).click();
    expect((await approved).status()).toBe(200);
    await expect(panel.getByTestId("approval-status")).toHaveText("Approved. 1 more approval needed.");
    await expect(panel.getByTestId("approval-quorum")).toHaveText("1 of 2 approved");
    await expect(panel.getByText("admin (you)")).toBeVisible();

    // The server now says this person may not approve again: aria-disabled (still focusable), with the reason attached.
    const approve = panel.getByRole("button", { name: "Approve" });
    await expect(approve).toHaveAttribute("aria-disabled", "true");
    const why = panel.getByTestId("approval-approve-why");
    await expect(why).toHaveText(
      "Approve is unavailable. You already approved this step. It needs 2 different approvers; 1 more is needed.",
    );
    const describedBy = await approve.getAttribute("aria-describedby");
    expect(describedBy).toBe(await why.getAttribute("id"));
    await approve.focus();
    await expect(approve).toBeFocused();
    let extraApprovals = 0;
    page.on("request", (request) => {
      if (request.url().includes("/approval/approve")) extraApprovals += 1;
    });
    await page.keyboard.press("Enter");
    await approve.click({ force: true });
    expect(extraApprovals).toBe(0);

    // Reject without a reason is announced before any call is made, and focus goes to the reason box.
    const reject = panel.getByRole("button", { name: "Reject" });
    await reject.click();
    await expect(panel.getByTestId("approval-reason-error")).toHaveText(
      "A reason is required to reject. Type one in the reason box.",
    );
    await expect(panel.getByTestId("approval-reason-error")).toHaveAttribute("role", "alert");
    await expect(panel.getByLabel("Reason")).toBeFocused();
    await expect(panel.getByLabel("Reason")).toHaveAttribute("aria-invalid", "true");

    // With a reason, rejecting asks first, and focus lands on the confirm button.
    await panel.getByLabel("Reason").fill("not the right case");
    await reject.click();
    const confirm = panel.getByRole("button", { name: "Confirm reject" });
    await expect(panel.getByText("Reject and fail this run?")).toBeVisible();
    await expect(confirm).toBeFocused();
    const rejected = decisionResponse(page, "reject");
    await page.keyboard.press("Enter");
    const rejectAnswer = await rejected;
    expect(rejectAnswer.status()).toBe(200);
    expect(rejectAnswer.request().postDataJSON()).toMatchObject({ reason: "not the right case" });
    await expect(panel.getByTestId("approval-status")).toHaveText("Rejected. The run was failed.");
    await expect(panel.getByTestId("approval-status")).toBeFocused();
    await expect(panel.getByRole("button", { name: "Reject" })).toHaveCount(0);
  });

  test("an expired request is read-only with the exact message announced as an alert", async ({
    page,
  }) => {
    await openRun(page, "WR-E2E-EXPIRED");
    const panel = page.getByTestId("approval-decision-panel");
    const alert = panel.getByRole("alert").filter({ hasText: "expired" });
    await expect(alert).toHaveText(
      /^This request expired at \d{4}-\d{2}-\d{2} \d{2}:\d{2} UTC\. Nothing was done\. Ask the requester to send it again\.$/,
    );
    await expect(panel.getByRole("button", { name: "Approve" })).toHaveCount(0);
    await expect(panel.getByRole("button", { name: "Reject" })).toHaveCount(0);
    await expect(panel.getByLabel("Reason")).toHaveCount(0);
    await expect(panel.getByTestId("approval-countdown")).toHaveText("(expired)");
  });

  test("a request that a newer one replaced is history, and the newer request is the one to decide", async ({
    page,
  }) => {
    await openRun(page, "WR-E2E-SUPERSEDED");
    // Only the live request is the full panel; the replaced one is a compact card that says what happened to it.
    await expect(page.getByTestId("approval-decision-panel")).toHaveCount(1);
    await expect(page.getByTestId("approval-decision-panel").getByRole("button", { name: "Approve" })).toBeVisible();
    await expect(page.getByTestId("workflow-run-recovery-approvals")).toContainText("superseded");
  });

  test("the requester cannot approve their own request: Approve is unavailable with the reason, Reject is not", async ({
    page,
  }) => {
    await openRun(page, "WR-E2E-SELF");
    const panel = page.getByTestId("approval-decision-panel");
    const approve = panel.getByRole("button", { name: "Approve" });
    await expect(approve).toHaveAttribute("aria-disabled", "true");
    await expect(panel.getByTestId("approval-approve-why")).toHaveText(
      "Approve is unavailable. You started this run, so you cannot approve it. A second person must decide.",
    );
    await expect(panel.getByRole("button", { name: "Reject" })).not.toHaveAttribute("aria-disabled");
    // The server agrees: the route refuses the same call, with nothing recorded.
    const listed = await page.request.get(`${API}/workflow-runs/WR-E2E-SELF/approvals`);
    const approvals = ((await listed.json()) as { data: Array<{ runtime_approval_id: string }> }).data;
    expect(approvals).toHaveLength(1);
    const forced = await page.request.post(`${API}/workflow-runs/WR-E2E-SELF/approval/approve`, {
      data: { runtime_approval_id: approvals[0]!.runtime_approval_id },
    });
    expect(forced.status()).toBe(403);
  });
});
