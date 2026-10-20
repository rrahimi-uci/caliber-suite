import { execFileSync } from "node:child_process";
import * as fs from "node:fs";
import * as path from "node:path";
import { fileURLToPath } from "node:url";
import { expect, test, type Page } from "@playwright/test";

import { CaliberUi, uiOnlySlug } from "../ui-only-driver";
import { askAria, closeAria, isRealProvider } from "./aria-real";

/**
 * Cookbook 13, "Run it through the UI": give Aria one sentence, answer its questions, and review
 * one of the traces it queued. It needs a server with the Aria flag on
 * (`scripts/cookbook-env.sh up lab13 --flags cookbooks,aria`); nothing here needs a model.
 *
 * The traces come from `assets/seed_traces.py` (it writes them to MLflow, not through the CALIBER
 * API), run with `CALIBER_LAB_PYTHON` (default: the repo's dev venv) against `MLFLOW_TRACKING_URI`
 * (from `.tmp/cookbook-env/NAME/env.sh`).
 *
 * On a real-provider stack (`scripts/cookbook-env.sh up lab13h --flags cookbooks,aria --real-llm`,
 * README "Run it with a real provider") one more test runs: the Ask Aria chat creates a queue in a
 * "Legacy auto" session and is not offered the tool in the default one. (The safety judge's
 * agreement with the reviewers is checked by the script; the Judges page can repeat it with
 * "Import from Review Queue" once a queue holds a dozen reviews.)
 */
const here = path.dirname(fileURLToPath(import.meta.url));
const REPO = path.resolve(here, "../../../../..");
const ASSETS = path.resolve(
  process.env["COOKBOOK_13_ASSETS"] ??
    path.join(
      REPO,
      "docs-site/cookbooks/13-aria-review-governance-queue/assets",
    ),
);

interface Reply {
  id: string;
  user_message: string;
  reply: string;
  review: Record<string, string | boolean>;
}
/** The twelve replies a person reviews (the same twelve as the SDK script). */
const REVIEWED_IDS = [
  "R01",
  "R03",
  "R05",
  "R09",
  "R11",
  "R12",
  "R13",
  "R15",
  "R18",
  "R22",
  "R23",
  "R27",
];
const REPLIES: Reply[] = fs
  .readFileSync(path.join(ASSETS, "dataset/agent-replies.jsonl"), "utf8")
  .split("\n")
  .filter((line) => line.trim())
  .map((line) => JSON.parse(line) as Reply);
const QUEUE = JSON.parse(
  fs.readFileSync(
    path.join(ASSETS, "review-queues/agent-reply-review.queue.json"),
    "utf8",
  ),
) as {
  name: string;
  description: string;
  reviewers: string[];
  questions: { key: string; title: string; type: string }[];
};
const SAFETY_JUDGE = JSON.parse(
  fs.readFileSync(path.join(ASSETS, "judges/reply-safety.judge.json"), "utf8"),
) as { name: string; instructions: string };
const INTENT = fs
  .readFileSync(path.join(ASSETS, "intent.md"), "utf8")
  .split("\n")
  .find((line) => line.startsWith("> Set up a review queue"))!
  .replace(/^> /, "");

function seedTraces(): Record<string, string> {
  const python =
    process.env["CALIBER_LAB_PYTHON"] ??
    path.join(REPO, "caliber/.venv/bin/python");
  const out = execFileSync(python, [path.join(ASSETS, "seed_traces.py")], {
    env: process.env,
    encoding: "utf8",
    stdio: ["ignore", "pipe", "ignore"],
    timeout: 120_000,
  });
  return JSON.parse(out) as Record<string, string>;
}

/** The label of a field in the "Aria needs information" form. */
function field(page: Page, label: RegExp) {
  return page
    .locator("label")
    .filter({ hasText: label })
    .locator("input, textarea, select");
}

async function openPlans(page: Page): Promise<void> {
  await page.goto("/caliber");
  const nav = page.getByLabel("CALIBER navigation");
  // Integrations only appears once the server's feature flags have loaded, and the sidebar
  // remembers which groups are open: open the group only if it is closed.
  const group = nav.getByRole("button", { name: /^Integrations/ });
  await expect(group).toBeVisible();
  if ((await group.getAttribute("aria-expanded")) !== "true") {
    await group.click();
  }
  await nav.getByRole("link", { name: "Plans" }).click();
  await expect(page.getByRole("heading", { name: "Aria Plans" })).toBeVisible();
}

test.describe("Cookbook 13: a human-review queue from one sentence", () => {
  test("states the intent, answers the questions, reviews twelve replies and checks a judge against them", async ({
    page,
  }) => {
    test.setTimeout(900_000);
    const suffix = uiOnlySlug("c13");
    const queueName = `${QUEUE.name}-${suffix}`;
    const judgeName = `${SAFETY_JUDGE.name}-${suffix}`;
    const traces = seedTraces();
    expect(Object.keys(traces)).toHaveLength(30);
    const queued = REPLIES.map((row) => traces[row.id]!);
    const ui = new CaliberUi(page);
    await ui.signIn();

    // ---- 1. Plans > Goal -----------------------------------------------------------------
    await openPlans(page);
    await page.getByLabel("Goal").fill(INTENT);
    await page
      .getByLabel("Autonomy")
      .selectOption({ label: "Stop before each change" });
    await page.getByRole("button", { name: "Decompose goal" }).click();

    // ---- 2. The drawn plan: create the queue, then enqueue (wired to it) -----------------
    const steps = page.locator("ol > li");
    await expect(steps).toHaveCount(2);
    await expect(steps.nth(0)).toContainText("review_queue.create");
    await expect(steps.nth(1)).toContainText("review_queue.add_items");
    await expect(steps.nth(1)).toContainText("depends on 1 prior step(s)");
    await expect(page.getByTitle("draft", { exact: true })).toBeVisible();

    // ---- 3. Approve ------------------------------------------------------------------------
    await page.getByRole("button", { name: "Approve plan" }).click();
    await expect(page.getByTitle("approved", { exact: true })).toBeVisible();

    // ---- 4. Execute: the queue's form, then an approval ---------------------------------
    await page.getByRole("button", { name: "Execute" }).click();
    await expect(page.getByText("Aria needs information")).toBeVisible();
    await expect(
      page.getByText(
        "Provide the required inputs for step 1: Create review queue.",
      ),
    ).toBeVisible();
    await field(page, /^name/).fill(queueName);
    await field(page, /^description/).fill(QUEUE.description);
    await field(page, /^questions/).fill(JSON.stringify(QUEUE.questions));
    await field(page, /^reviewers/).fill(JSON.stringify(QUEUE.reviewers));
    await page.getByRole("button", { name: "Continue plan" }).click();
    await expect(
      page.getByText(
        "Approve step 1: Create review queue (review_queue.create)?",
      ),
    ).toBeVisible();
    await page.getByRole("button", { name: "Approve", exact: true }).click();

    // ---- 5. The enqueue form asks for the trace ids only ----------------------------------
    await expect(
      page.getByText(
        "Provide the required inputs for step 2: Enqueue traces for review.",
      ),
    ).toBeVisible();
    await expect(field(page, /^queue id/)).toHaveCount(0);
    await field(page, /^trace ids/).fill(JSON.stringify(queued));
    await field(page, /^assigned to/).fill("admin");
    await page.getByRole("button", { name: "Continue plan" }).click();
    await expect(
      page.getByText(
        "Approve step 2: Enqueue traces for review (review_queue.add_items)?",
      ),
    ).toBeVisible();
    await page.getByRole("button", { name: "Approve", exact: true }).click();
    await expect(page.getByTitle("completed", { exact: true })).toBeVisible();
    await expect(steps.nth(0)).toContainText("done");
    await expect(steps.nth(1)).toContainText("done");

    // ---- 6. Review Queues: the queue and its thirty traces ---------------------------------
    await page.goto("/caliber/review-queues");
    const row = page.getByRole("row").filter({ hasText: queueName });
    await expect(row).toBeVisible();
    await expect(row).toContainText("0/30 reviewed");
    await expect(row).toContainText(QUEUE.reviewers.join(", "));

    // ---- 7. Review twelve of them, reading each reply first ---------------------------------
    await row.getByRole("button", { name: queueName }).click();
    await expect(page.getByText("Items (30)")).toBeVisible();
    const ask = (title: string) =>
      page.locator("label", { hasText: title }).locator("xpath=..");
    for (const id of REVIEWED_IDS) {
      const reply = REPLIES.find((r) => r.id === id)!;
      await page.getByRole("button", { name: traces[id]! }).click();
      // The reviewer is shown what the agent was asked and what it said.
      const content = page.getByTestId("review-trace-content");
      await expect(content).toContainText(reply.user_message);
      await expect(content).toContainText(reply.reply.slice(0, 25));
      for (const question of QUEUE.questions) {
        const answer = reply.review[question.key];
        if (answer === undefined) continue;
        if (question.type === "pass_fail") {
          await ask(question.title)
            .getByRole("button", {
              name: answer ? "Pass" : "Fail",
              exact: true,
            })
            .click();
        } else if (question.type === "categorical") {
          await page
            .getByLabel(question.title, { exact: true })
            .selectOption(String(answer));
        } else {
          await page
            .getByLabel(question.title, { exact: true })
            .fill(String(answer));
        }
      }
      await page.getByRole("button", { name: "Submit review" }).click();
      await expect(
        page.getByRole("button", { name: traces[id]! }),
      ).toContainText("completed");
    }
    await page.goto("/caliber/review-queues");
    await expect(
      page.getByRole("row").filter({ hasText: queueName }),
    ).toContainText(`${REVIEWED_IDS.length}/30 reviewed`);

    // ---- 8. A reviewed trace is read-only and says who reviewed it ---------------------------
    await page
      .getByRole("row")
      .filter({ hasText: queueName })
      .getByRole("button", { name: queueName })
      .click();
    await page.getByRole("button", { name: traces["R11"]! }).click();
    await expect(page.getByText("Reviewed by admin")).toBeVisible();
    await expect(
      page.getByText(/wrote \d+ assessment\(s\) back/),
    ).toBeVisible();
    await expect(
      page.getByRole("button", { name: "Submit review" }),
    ).toHaveCount(0);

    // Told apart by the provider banner (it navigates, so ask before opening the judge).
    const real = await isRealProvider(page);

    // ---- 9. Check a safety judge against the reviewers' labels --------------------------------
    await page.goto("/caliber/judges");
    await page.getByRole("button", { name: "+ New Judge" }).click();
    await page.getByPlaceholder("answer-faithfulness").fill(judgeName);
    await page
      .getByPlaceholder("Does {{ outputs }} faithfully answer")
      .fill(SAFETY_JUDGE.instructions);
    await page.getByRole("button", { name: "Create judge" }).click();
    const judgeRow = page.getByRole("row").filter({ hasText: judgeName });
    await expect(judgeRow).toBeVisible();
    await judgeRow.getByRole("button", { name: "Try it" }).click();
    await page.getByTestId("judge-mode-align").click();
    await page
      .getByTestId("align-review-queue")
      .selectOption({ label: queueName });
    await page.getByTestId("align-review-question").selectOption({ index: 1 });
    await page.getByTestId("align-import-review-labels").click();
    await expect(
      page.getByText(
        `Imported ${REVIEWED_IDS.length} completed label(s); skipped 0.`,
      ),
    ).toBeVisible();
    await page.getByTestId("align-run").click();
    const result = page.getByTestId("align-result");
    await expect(result).toBeVisible({ timeout: 600_000 });
    if (!real) {
      // The judge cannot run on a simulated stack: every row errors and is left out.
      await expect(result).toContainText(`over 0/${REVIEWED_IDS.length}`);
      await expect(page.getByTestId("align-errors")).toContainText(
        `${REVIEWED_IDS.length} example(s) could not be judged`,
      );
    } else {
      await expect(result).toContainText(
        `over ${REVIEWED_IDS.length}/${REVIEWED_IDS.length} examples`,
      );
      const text = (await result.textContent()) ?? "";
      const agreement = Number(/Agreement (\d+)%/.exec(text)![1]);
      const kappa = Number(/κ (-?\d+\.\d+)/.exec(text)![1]);
      expect(agreement, text).toBeGreaterThanOrEqual(75);
      expect(kappa, text).toBeGreaterThanOrEqual(0.4);
    }
  });

  test("a review that does not fit the questions is refused and stays open", async ({
    page,
  }) => {
    test.setTimeout(300_000);
    const suffix = uiOnlySlug("c13r");
    const queueName = `${QUEUE.name}-${suffix}`;
    const traces = seedTraces();
    const ui = new CaliberUi(page);
    await ui.signIn();
    await openPlans(page);
    await page.getByLabel("Goal").fill(INTENT);
    await page.getByRole("button", { name: "Decompose goal" }).click();
    await page.getByRole("button", { name: "Approve plan" }).click();
    await page.getByRole("button", { name: "Execute" }).click();
    await field(page, /^name/).fill(queueName);
    await field(page, /^questions/).fill(JSON.stringify(QUEUE.questions));
    await page.getByRole("button", { name: "Continue plan" }).click();
    await field(page, /^trace ids/).fill(JSON.stringify([traces["R02"]]));
    await page.getByRole("button", { name: "Continue plan" }).click();
    await expect(page.getByTitle("completed", { exact: true })).toBeVisible();

    await page.goto("/caliber/review-queues");
    await page
      .getByRole("row")
      .filter({ hasText: queueName })
      .getByRole("button", { name: queueName })
      .click();
    await page.getByRole("button", { name: traces["R02"]! }).click();
    // Only one of the three required questions is answered.
    await page
      .locator("label", { hasText: QUEUE.questions[0]!.title })
      .locator("xpath=..")
      .getByRole("button", { name: "Pass", exact: true })
      .click();
    await page.getByRole("button", { name: "Submit review" }).click();
    await expect(
      page.getByText(/missing required answer 'citation_ok'/),
    ).toBeVisible();
  });

  test("skipping the queue skips the enqueue step with it", async ({
    page,
  }) => {
    const ui = new CaliberUi(page);
    await ui.signIn();
    await openPlans(page);
    await page.getByLabel("Goal").fill(INTENT);
    await page.getByRole("button", { name: "Decompose goal" }).click();
    await page.getByRole("button", { name: "Approve plan" }).click();
    await page.getByRole("button", { name: "Execute" }).click();
    await page.getByRole("button", { name: "Skip step" }).click();
    await expect(page.getByTitle("completed", { exact: true })).toBeVisible();
    const steps = page.locator("ol > li");
    await expect(steps.nth(0)).toContainText("skipped");
    await expect(steps.nth(1)).toContainText("skipped");
    await expect(steps.nth(1)).toContainText(
      "skipped because step 1 (review_queue.create) was skipped",
    );
  });

  test("a categorical question with no options fails the plan and says which", async ({
    page,
  }) => {
    const suffix = uiOnlySlug("c13c");
    const ui = new CaliberUi(page);
    await ui.signIn();
    await openPlans(page);
    await page.getByLabel("Goal").fill(INTENT);
    await page.getByRole("button", { name: "Decompose goal" }).click();
    await page.getByRole("button", { name: "Approve plan" }).click();
    await page.getByRole("button", { name: "Execute" }).click();
    await field(page, /^name/).fill(`no-options-${suffix}`);
    await field(page, /^questions/).fill(
      JSON.stringify([{ key: "tone", title: "Tone?", type: "categorical" }]),
    );
    await page.getByRole("button", { name: "Continue plan" }).click();
    await expect(
      page.getByTitle("failed", { exact: true }).first(),
    ).toBeVisible();
    await expect(page.locator("ol > li").first()).toContainText(
      "invalid input: questions.0: categorical questions require a non-empty 'options' list",
    );
  });

  test("real provider: Ask Aria acts in a Legacy auto chat and only reads in the default one", async ({
    page,
  }) => {
    test.setTimeout(600_000);
    const ui = new CaliberUi(page);
    await ui.signIn();
    test.skip(
      !(await isRealProvider(page)),
      "Ask Aria needs a real engine (a simulated stack answers with a canned heuristic)",
    );
    const suffix = uiOnlySlug("c13chat");
    const acting = `c13-chat-queue-${suffix}`;
    const reading = `c13-chat-manual-${suffix}`;
    const ask = (name: string) =>
      `Create a review queue named ${name} with one pass_fail question: key safety, title ` +
      "'Is the reply safe to send?'. Then tell me its id.";
    await page.goto("/caliber/review-queues");
    await askAria(page, ask(reading));
    await closeAria(page);
    const acted = await askAria(page, ask(acting), { approval: "Legacy auto" });
    expect(acted).toMatch(/\d+ actions?/);
    await closeAria(page);
    await page.goto("/caliber/review-queues");
    await expect(
      page.getByRole("row").filter({ hasText: acting }),
    ).toBeVisible();
    await expect(
      page.getByRole("row").filter({ hasText: reading }),
    ).toHaveCount(0);
  });
});
