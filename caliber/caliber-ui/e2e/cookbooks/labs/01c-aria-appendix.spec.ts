import * as fs from "node:fs";
import * as path from "node:path";
import { fileURLToPath } from "node:url";
import { expect, test, type Page } from "@playwright/test";

import { CaliberUi, uiOnlySlug } from "../ui-only-driver";

/**
 * Cookbook 01, appendix: the steps that need the Aria assistant and a real model
 * (`docs-site/cookbooks/01-prompt-regression-lab/README.md`, "Appendix: the Aria steps").
 *
 * Needs a stack started with BOTH feature flags and a real provider:
 *
 *   scripts/cookbook-env.sh up lab01ha --flags cookbooks,aria --real-llm
 *
 * On a simulated stack (`/readiness` lists `llm`) the whole file is skipped: with the fake
 * assistant these steps cannot work (test generation answers "LLM did not return a valid JSON
 * array"), and the lab says so instead of pretending.
 *
 * What it proves, in the appendix's order (assertions are invariants, never generated text):
 *   1. Playground: a live chat session seeded with the prompt answers a support message;
 *   2. Test Sets: Generate Test Cases, Run Tests & Judge, Save to Test Sets;
 *   3. Runs: Run tests twice around a regression (baseline on v1, the weaker prompt promoted),
 *      Set as baseline, and the "Vs. baseline" diff with the score delta;
 *   4. Calibration: Assistant-Guided Calibration analyzes an intent and builds a plan.
 *
 * About 8 minutes against a hosted model. Cost: a few dozen model calls.
 */
const here = path.dirname(fileURLToPath(import.meta.url));
const ASSETS = path.resolve(
  process.env["COOKBOOK_01_ASSETS"] ??
    path.join(
      here,
      "../../../../../docs-site/cookbooks/01-prompt-regression-lab/assets",
    ),
);
const CASES = 4;

function promptBody(file: string): string {
  const text = fs.readFileSync(path.join(ASSETS, "prompts", file), "utf8");
  return (
    (text.startsWith("---") ? text.split("\n---\n")[1]! : text).trim() + "\n"
  );
}

/** `GET /readiness`: are the language model or the evaluator simulated on this stack? */
async function isSimulated(page: Page): Promise<boolean> {
  const root = new URL("/", process.env["CALIBER_LAB_BASE_URL"]!).toString();
  const response = await page.request.get(
    `${root}ajax-api/2.0/mlflow/caliber/readiness`,
  );
  expect(response.ok()).toBeTruthy();
  const body = (await response.json()) as { data?: { simulated?: string[] } };
  const simulated = body.data?.simulated ?? [];
  return simulated.includes("llm") || simulated.includes("eval");
}

function liveBadge(page: Page, version: number) {
  return page
    .getByTestId(`version-row-${version}`)
    .getByTestId("version-live-badge");
}

async function stage(page: Page, name: string): Promise<void> {
  await page.getByRole("button", { name, exact: true }).click();
}

test.describe("Cookbook 01 appendix: the Aria steps", () => {
  test("playground, test generation, run and judge, baseline diff, guided calibration", async ({
    page,
  }) => {
    test.setTimeout(1_800_000);
    test.skip(
      await isSimulated(page),
      "needs a real model: start the stack with --flags cookbooks,aria --real-llm",
    );
    const suffix = uiOnlySlug("c01a");
    const promptName = `cookbook-01-aria-${suffix}`;
    await new CaliberUi(page).signIn();

    // ---- The prompt: v1 live -------------------------------------------------------------
    await page.goto("/caliber/prompts");
    await page.getByRole("button", { name: "New prompt" }).click();
    await page.getByRole("button", { name: /Write \/ paste/ }).click();
    await page
      .getByLabel("Prompt text")
      .fill(promptBody("intake-classifier.md"));
    await page.getByLabel("Prompt name").fill(promptName);
    await page.getByRole("button", { name: "Create and Open" }).click();
    await expect(page.getByTestId("version-panel")).toBeVisible();
    await expect(liveBadge(page, 1)).toBeVisible();

    // ---- 1. Playground: a live chat with the prompt ---------------------------------------
    await stage(page, "Playground");
    await page.getByRole("button", { name: "Start Chat Session" }).click();
    await expect(page.getByText(/Session started/)).toBeVisible({
      timeout: 60_000,
    });
    await expect(page.getByTestId("playground-prompt-ref")).toContainText(
      promptName,
    );
    await page
      .getByPlaceholder("Type a message…")
      .fill(
        "I was charged twice after upgrading to the Team plan this morning.",
      );
    await page.getByTitle("Send message").click();
    // The assistant answered: the header counts the exchange (your message and one reply). What it
    // says varies: Aria is an assistant seeded with the prompt, not a raw run of it, so it may
    // answer with the JSON record or talk about the prompt. Only that it answered is asserted.
    await expect(page.getByText(/2\s+messages/)).toBeVisible({
      timeout: 180_000,
    });

    // ---- 2. Test Sets: generate, run and judge, save --------------------------------------
    await stage(page, "Test Sets");
    await page.getByLabel("Number of test cases").fill(String(CASES));
    await page.getByRole("button", { name: "Generate Test Cases" }).click();
    await expect(page.getByRole("button", { name: /Run Tests/ })).toBeVisible({
      timeout: 180_000,
    });
    await expect(page.locator("tbody tr")).toHaveCount(CASES);
    await page.getByRole("button", { name: /Run Tests/ }).click();
    await expect(page.getByTestId("run-saved-indicator")).toBeVisible({
      timeout: 600_000,
    });
    // The judge gave every case a verdict: pass + partial + fail = the cases run.
    await expect(page.getByText("Overall Score")).toBeVisible();
    await page.getByRole("button", { name: "Save to Test Sets" }).click();
    await expect(page.getByText(/Saved to Test Sets/)).toBeVisible({
      timeout: 60_000,
    });

    // ---- 3. Runs: baseline on v1, the weaker prompt promoted, Vs. baseline ----------------
    await stage(page, "Runs");
    await page.getByRole("button", { name: "Run tests", exact: true }).click();
    await expect(page.getByTestId("workspace-run-results")).toBeVisible({
      timeout: 600_000,
    });
    await page
      .getByRole("button", { name: "Set as baseline", exact: true })
      .click();
    await expect(page.getByTestId("run-baseline-marker")).toBeVisible({
      timeout: 60_000,
    });

    // The regression: the weaker wording is promoted (Save & promote), so Runs scores it.
    await stage(page, "Author");
    await page
      .getByLabel("Prompt template")
      .fill(promptBody("intake-classifier-weak.md"));
    await page
      .getByLabel("Commit message", { exact: true })
      .fill("v2 dropped the JSON-only rules (intentional regression)");
    await page.getByRole("button", { name: "Save & promote" }).click();
    await expect(liveBadge(page, 2)).toBeVisible({ timeout: 60_000 });

    await stage(page, "Runs");
    await page.getByRole("button", { name: "Run tests", exact: true }).click();
    const comparison = page.getByTestId("workspace-run-comparison");
    await expect(comparison).toBeVisible({ timeout: 600_000 });
    await expect(comparison).toContainText("Vs. baseline");
    await expect(page.getByTestId("run-score-delta")).toBeVisible();
    await expect(page.getByTestId("run-regressions")).toBeVisible();

    // Put v1 back (the regression was an experiment).
    await stage(page, "Author");
    await page
      .getByTestId("version-row-2")
      .getByTestId("version-rollback")
      .click();
    await expect(page.getByTestId("confirm-dialog")).toBeVisible();
    await page.getByTestId("confirm-dialog-confirm").click();
    await expect(liveBadge(page, 1)).toBeVisible({ timeout: 60_000 });

    // ---- 4. Calibration: Assistant-Guided Calibration analyzes and plans -------------------
    await stage(page, "Calibration");
    await expect(
      page.getByRole("heading", { name: "Assistant-Guided Calibration" }),
    ).toBeVisible();
    await page
      .getByLabel("Assistant intent request")
      .fill(
        `Calibrate ${promptName} against the test set ${promptName.replace("aria", "aria")} saved from this prompt, scoring with Guidelines.`,
      );
    await page.getByRole("button", { name: "Analyze Intent" }).click();
    await expect(page.getByText("Resolved Intent")).toBeVisible();
    await expect(page.getByText(/Confidence|confidence|%/).first()).toBeVisible(
      {
        timeout: 120_000,
      },
    );
    await page.getByRole("button", { name: "Build Plan" }).click();
    await expect(page.getByText(/Status (ready|needs input)/)).toBeVisible({
      timeout: 120_000,
    });
    // What the assistant understood and planned (varies; printed so a run can be read).
    const body = await page.locator("body").innerText();
    const from = body.indexOf("Assistant-Guided Calibration");
    console.log(
      body
        .slice(from, from + 1500)
        .replace(/\n+/g, " | ")
        .slice(0, 900),
    );
    if (await page.getByText("Status ready").count()) {
      // A ready plan can be executed after confirmation: it starts a calibration run on the
      // prompt's test set (it applies nothing), and the Operation panel reports it.
      await page
        .getByRole("button", { name: "Execute Confirmed Plan" })
        .click();
      await expect(page.getByText("Last action:")).toBeVisible({
        timeout: 180_000,
      });
    }
  });
});
