import { execFileSync } from "node:child_process";
import * as fs from "node:fs";
import * as path from "node:path";
import { fileURLToPath } from "node:url";
import { expect, test, type Page } from "@playwright/test";

import { CaliberUi, uiOnlySlug } from "../ui-only-driver";

/**
 * Cookbook 10, "Run it through the UI": load the 50-row test set, define the judge, score a
 * baseline and a candidate, have a person review ten traces, import their labels and check the
 * judge against them.
 *
 * It needs a server with the Cookbooks flag on, and a model behind it for the Evaluations and
 * alignment steps. `CALIBER_LAB10_MODEL` says which kind of server this is:
 *
 *   stand-in   (default) a server started against the lab's scripted stand-in model, so every
 *              step runs and the exact numbers in the README are asserted:
 *                python docs-site/cookbooks/10-judge-certification-human-review-lab/assets/stand-in-model/stand_in_model_server.py 8770
 *                CALIBER_LLM_PROVIDER=openai CALIBER_EVAL_PROVIDER=mlflow OPENAI_API_KEY=stand-in \
 *                  OPENAI_BASE_URL=http://127.0.0.1:8770/v1 OPENAI_API_BASE=http://127.0.0.1:8770/v1 \
 *                  scripts/cookbook-env.sh up lab10m --flags cookbooks --real-llm
 *   real       a server with a real provider: the same steps, structure asserted, not the numbers.
 *   simulated  the default stack (fake provider): Evaluations and the judge cannot run, and the
 *              spec asserts the refusal the README documents instead.
 *
 * The ten traces come from `assets/seed_traces.py` (it writes them to MLflow, not through the
 * CALIBER API), run with `CALIBER_LAB_PYTHON` (default: the repo's dev venv) against
 * `MLFLOW_TRACKING_URI` (from `.tmp/cookbook-env/NAME/env.sh`).
 */
const here = path.dirname(fileURLToPath(import.meta.url));
const REPO = path.resolve(here, "../../../../..");
const ASSETS = path.resolve(
  process.env["COOKBOOK_10_ASSETS"] ??
    path.join(
      REPO,
      "docs-site/cookbooks/10-judge-certification-human-review-lab/assets",
    ),
);
const DATASET_FILE = path.join(ASSETS, "dataset/aggregated-outputs.jsonl");
const MODEL = process.env["CALIBER_LAB10_MODEL"] ?? "stand-in";
/** A real model makes a call per row (about 6 minutes for 50 rows); the stand-in answers at once. */
const EVAL_TIMEOUT = MODEL === "real" ? 900_000 : 240_000;

interface Row {
  id: string;
  input: Record<string, unknown>;
  expected: Record<string, unknown>;
  outputs: Record<string, unknown>;
  human_label: "pass" | "fail";
}
const ROWS: Row[] = fs
  .readFileSync(DATASET_FILE, "utf8")
  .split("\n")
  .filter((line) => line.trim())
  .map((line) => JSON.parse(line) as Row);

/** The ten rows a person reviews in the UI: 4 clear passes, 4 clear failures, 1 FP, 1 FN. */
const REVIEWED = [
  "J01",
  "J02",
  "J03",
  "J04",
  "J05",
  "J08",
  "J09",
  "J11",
  "J14",
  "J26",
];

const JUDGE = JSON.parse(
  fs.readFileSync(
    path.join(ASSETS, "judges/faithfulness-judge.judge.json"),
    "utf8",
  ),
) as { instructions: string };

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

async function createJudge(
  page: Page,
  name: string,
  instructions: string,
): Promise<void> {
  await page.getByRole("button", { name: "+ New Judge" }).click();
  await page.getByPlaceholder("answer-faithfulness").fill(name);
  await page
    .getByPlaceholder("Does {{ outputs }} faithfully answer")
    .fill(instructions);
}

async function openAlignment(page: Page, judgeName: string): Promise<void> {
  await page.goto("/caliber/judges");
  const row = page.getByRole("row").filter({ hasText: judgeName });
  await row.getByRole("button", { name: "Try it" }).click();
  await page.getByTestId("judge-mode-align").click();
}

test.describe("Cookbook 10: judge certification and human review", () => {
  test("loads the test set, defines the judge, reviews traces and checks alignment", async ({
    page,
  }) => {
    test.setTimeout(MODEL === "real" ? 2_700_000 : 600_000);
    const suffix = uiOnlySlug("c10");
    const promptName = `grounded-reply-${suffix}`;
    const datasetName = `judge-certification-set-${suffix}`;
    const judgeName = `FaithfulnessJudge-${suffix}`;
    const lenientName = `LenientJudge-${suffix}`;
    const queueName = `faithfulness-certification-${suffix}`;
    const baselineLabel = `baseline ${suffix}`;
    const candidateLabel = `candidate ${suffix}`;
    const traces = seedTraces();
    expect(Object.keys(traces)).toHaveLength(ROWS.length);
    const ui = new CaliberUi(page);
    await ui.signIn();
    // The app says whether a model is behind it; the spec must be told which kind (README: Setup).
    const simulatedStack = await ui.isSimulatedStack();
    expect(
      simulatedStack,
      simulatedStack
        ? "this stack is simulated (fake providers): run with CALIBER_LAB10_MODEL=simulated"
        : "this stack has a model behind it: CALIBER_LAB10_MODEL must be stand-in or real",
    ).toBe(MODEL === "simulated");

    // The Cookbooks gallery (a preview area, behind its flag) lists this recipe and its checklist.
    await page.goto("/caliber/cookbooks");
    const card = page.getByTestId("cookbook-card-10");
    await expect(card).toContainText("Trustworthy Evaluation");
    await expect(card).toContainText("human alignment");

    // ---- 1. Prepare the dataset (Prompt lifecycle > Test Sets > Import from file) ---------
    await page.goto("/caliber/eval-datasets");
    await page.getByRole("button", { name: "+ New Test Set" }).click();
    await page.getByPlaceholder("factual-checks").fill(datasetName);
    await page.getByPlaceholder("@sarah").fill("admin");
    await page.getByRole("button", { name: "Create", exact: true }).click();
    await page.getByText(datasetName, { exact: true }).first().click();
    await expect(page.getByTestId("eval-dataset-detail")).toBeVisible();
    await page.getByTestId("import-toggle").click();
    await page.getByTestId("import-file").setInputFiles(DATASET_FILE);
    await expect(page.getByTestId("import-summary")).toContainText(
      `${ROWS.length} rows ready to import`,
    );
    await page.getByTestId("import-save").click();
    // one dataset version per appended example: 50 rows is version 51
    await expect(page.getByTestId("eval-dataset-version")).toHaveText(
      `v${ROWS.length + 1}`,
      { timeout: 120_000 },
    );
    await expect(page.getByTestId("eval-dataset-detail")).toContainText(
      `${ROWS.length} active examples`,
    );
    await expect(page.getByTestId("example-row")).toHaveCount(ROWS.length);

    // The candidate prompt (Prompt lifecycle > Prompts > New prompt): version 1.
    await page.goto("/caliber/prompts");
    await page.getByRole("button", { name: "New prompt" }).click();
    await page.getByRole("button", { name: /Write \/ paste/ }).click();
    await page
      .getByLabel("Prompt text")
      .fill(
        "You answer support tickets and incident alerts using ONLY the evidence you are given. " +
          "State what the evidence says and nothing more; if it does not cover the question, " +
          "say so and escalate to a person.\n",
      );
    await page.getByLabel("Prompt name").fill(promptName);
    await page.getByRole("button", { name: "Create and Open" }).click();
    await expect(page.getByTestId("version-panel")).toBeVisible();

    // ---- 2. Define the judge (Agent resources > Judges > + New Judge) ---------------------
    await page.goto("/caliber/judges");
    await createJudge(
      page,
      judgeName,
      "Is {{ outputs }} faithful to {{ evidence }}?",
    );
    await page.getByRole("button", { name: "Create judge" }).click();
    // An unsupported variable is refused on save, naming it (it used to be saved and only fail
    // when the judge was first used).
    await expect(
      page.getByText(/unsupported template variable.*\{\{ evidence \}\}/),
    ).toBeVisible();
    await page
      .getByPlaceholder("Does {{ outputs }} faithfully answer")
      .fill(JUDGE.instructions);
    await page.getByRole("button", { name: "Create judge" }).click();
    await expect(
      page.getByRole("row").filter({ hasText: judgeName }),
    ).toBeVisible();
    await createJudge(
      page,
      lenientName,
      "Return true when {{ outputs }} is a complete, well-formed answer to {{ inputs }}. " +
        "Return false only if it is empty or cut off.",
    );
    await page.getByRole("button", { name: "Create judge" }).click();
    await expect(
      page.getByRole("row").filter({ hasText: lenientName }),
    ).toBeVisible();

    // ---- 3. Run baseline and candidate (Prompt lifecycle > Evaluations) -------------------
    async function runEvaluation(
      label: string,
      subject: string | null,
    ): Promise<void> {
      await page.goto("/caliber/evaluations");
      await page.getByRole("button", { name: "Run evaluation" }).click();
      await page
        .getByLabel("Test set", { exact: true })
        .selectOption({ label: `${datasetName} (v${ROWS.length + 1})` });
      await page.getByLabel("Label", { exact: true }).fill(label);
      if (subject) {
        await page.getByLabel("What to score").selectOption("prompt");
        await page.getByLabel("Subject under test").fill(subject);
      }
      // Deterministic graders: Contains expected and Non-empty; plus the judge.
      await page.getByText("Exact match", { exact: true }).click();
      await page.getByText("Token F1", { exact: true }).click();
      await page.getByText("Non-empty", { exact: true }).click();
      await page
        .getByTestId("judge-scorer-options")
        .getByText(judgeName)
        .click();
      await page.getByRole("button", { name: "Run", exact: true }).click();
    }

    await runEvaluation(baselineLabel, null);
    if (MODEL === "simulated") {
      // No model behind the server: Evaluations fails closed instead of inventing scores.
      await expect(
        page.getByText(/evaluation requires a real LLM provider/),
      ).toBeVisible({ timeout: 60_000 });
    } else {
      const baselineRow = page
        .getByTestId("eval-run-row")
        .filter({ hasText: baselineLabel });
      await expect(baselineRow).toBeVisible({ timeout: EVAL_TIMEOUT });
      await expect(baselineRow).toContainText("completed");
      // the cell reads "<passed>/<scored>": all 50 pass with the stand-in, a real model fails some
      await expect(baselineRow).toContainText(
        MODEL === "real"
          ? new RegExp(`\\d+/${ROWS.length}`)
          : `${ROWS.length}/${ROWS.length}`,
      );

      await runEvaluation(candidateLabel, `${promptName}@1`);
      const candidateRow = page
        .getByTestId("eval-run-row")
        .filter({ hasText: candidateLabel });
      await expect(candidateRow).toBeVisible({ timeout: EVAL_TIMEOUT });
      await expect(candidateRow).toContainText("completed");

      // ---- 4. Inspect examples: per-row verdicts, and the candidate against the baseline ---
      await candidateRow.getByRole("link").click();
      await expect(page.getByText(`prompt: ${promptName}@1`)).toBeVisible();
      await expect(page.getByTestId("eval-result-row")).toHaveCount(
        ROWS.length,
      );
      const compare = page.getByLabel("Compare to baseline run");
      const baselineValue = await compare
        .locator("option", { hasText: baselineLabel })
        .getAttribute("value");
      await compare.selectOption(baselineValue!);
      await expect(
        page.getByTestId("baseline-comparison-context"),
      ).toContainText("target llm");
      if (MODEL === "real") {
        // a real model: scores are percentages in range, nothing is pinned
        const aggregate = page.getByTestId("eval-aggregate");
        await expect(aggregate).toContainText(/\d+%/);
        await expect(page.getByTestId("eval-result-row").first()).toBeVisible();
        await expect(
          page.getByTestId("eval-result-row").first(),
        ).not.toContainText("Thanks for flagging this.");
      }
      if (MODEL === "stand-in") {
        const aggregate = page.getByTestId("eval-aggregate");
        await expect(aggregate).toContainText("74%"); // overall 0.74
        await expect(aggregate).toContainText("56%"); // the judge's pass rate
        await expect(aggregate).toContainText("66%"); // contains expected
        await expect(page.getByTestId("eval-result-row").first()).toContainText(
          "Thanks for flagging this.",
        );
      }
    }

    if (MODEL !== "simulated") {
      // ---- 4b. Modify -> new version -> re-run -> compare ---------------------------------
      // Edit the candidate prompt (Prompts > the card's Edit > Save new version: v2), score
      // v2 over the same test set, and read it against the v1 run. Saving moves nothing live.
      const v2Label = `candidate v2 ${suffix}`;
      await page.goto("/caliber/prompts");
      await page.getByLabel("Search prompts").fill(promptName);
      await page
        .locator('[data-testid^="prompt-card-"]')
        .filter({ hasText: promptName })
        .first()
        .getByRole("button", { name: "Edit" })
        .click();
      await page
        .locator("form textarea")
        .first()
        .fill(
          "You answer support tickets and incident alerts using ONLY the evidence you are given. " +
            "Quote every figure, date and identifier exactly as the evidence gives it; if the " +
            "evidence does not cover the question, say so and escalate to a person.\n",
        );
      await page.getByTestId("prompt-edit-save").click();
      await expect(page.getByText(/Saved v2/)).toBeVisible();
      await runEvaluation(v2Label, `${promptName}@2`);
      const v2Row = page
        .getByTestId("eval-run-row")
        .filter({ hasText: v2Label });
      await expect(v2Row).toBeVisible({ timeout: EVAL_TIMEOUT });
      await expect(v2Row).toContainText("completed");
      await v2Row.getByRole("link").click();
      await expect(page.getByText(`prompt: ${promptName}@2`)).toBeVisible();
      await expect(page.getByTestId("eval-result-row")).toHaveCount(
        ROWS.length,
      );
      const compareV2 = page.getByLabel("Compare to baseline run");
      const v1Value = await compareV2
        .locator("option", { hasText: candidateLabel })
        .getAttribute("value");
      await compareV2.selectOption(v1Value!);
      // the comparison names both subjects: this prompt's v2 against its v1
      const context = page.getByTestId("baseline-comparison-context");
      await expect(context).toContainText(`${promptName}@2`);
      await expect(context).toContainText(
        `Baseline: target prompt, subject ${promptName}@1`,
      );
      await expect(page.getByTestId("eval-aggregate")).toBeVisible();
    }

    // ---- 5. Human review (Operate > Review Queues) ----------------------------------------
    await page.goto("/caliber/review-queues");
    await page.getByRole("button", { name: "New queue" }).click();
    await page.getByPlaceholder("answer-quality").fill(queueName);
    await page.getByLabel("Question 1 key").fill("faithful");
    await page
      .getByLabel("Question 1 title")
      .fill("Is EVERY factual claim supported by the evidence?");
    await page.getByRole("button", { name: "+ Add question" }).click();
    await page.getByLabel("Question 2 key").fill("evidence");
    await page
      .getByLabel("Question 2 title")
      .fill("Evidence you checked the output against (paste it)");
    await page.getByLabel("Question 2 type").selectOption("text");
    await page.getByRole("button", { name: "Create queue" }).click();
    await page.getByRole("button", { name: queueName }).click();
    await page
      .getByLabel("Trace ids")
      .fill(REVIEWED.map((id) => traces[id]).join(" "));
    await page.getByRole("button", { name: "Enqueue" }).click();
    await expect(page.getByText(`Items (${REVIEWED.length})`)).toBeVisible();

    // The form on the right is for the first pending item; read its trace id to know which row
    // it is, answer it (Pass/Fail and the evidence), and wait for the next item to appear.
    const reviewing = page
      .getByText("Reviewing trace")
      .locator("xpath=following-sibling::div");
    let previous = "";
    for (let i = 0; i < REVIEWED.length; i++) {
      await expect(reviewing).toBeVisible();
      if (previous) await expect(reviewing).not.toHaveText(previous);
      const traceId = (await reviewing.textContent())!.trim();
      const row = ROWS.find((r) => traces[r.id] === traceId);
      expect(
        row,
        `trace ${traceId} is not one of the reviewed rows`,
      ).toBeTruthy();
      await page
        .getByRole("button", {
          name: row!.human_label === "pass" ? "Pass" : "Fail",
          exact: true,
        })
        .click();
      await page
        .getByLabel("Evidence you checked the output against (paste it)")
        .fill(JSON.stringify(row!.expected));
      await page.getByRole("button", { name: "Submit review" }).click();
      previous = traceId;
    }
    await expect(page.getByText("All items reviewed.")).toBeVisible();

    // ---- 6. Compute alignment (Agent resources > Judges > Try it > Human alignment) -------
    async function importAndCheck(judge: string): Promise<void> {
      await openAlignment(page, judge);
      await page
        .getByTestId("align-review-queue")
        .selectOption({ label: queueName });
      await page
        .getByTestId("align-review-question")
        .selectOption({ index: 1 });
      await page.getByTestId("align-import-review-labels").click();
      await expect(
        page.getByText(
          `Imported ${REVIEWED.length} completed label(s); skipped 0.`,
        ),
      ).toBeVisible();
      await page.getByTestId("align-run").click();
      await expect(page.getByTestId("align-result")).toBeVisible({
        timeout: MODEL === "real" ? 600_000 : 120_000,
      });
    }

    await importAndCheck(judgeName);
    const result = page.getByTestId("align-result");
    if (MODEL === "simulated") {
      // The judge cannot run: every row errors and is left out of the metrics.
      await expect(result).toContainText(`over 0/${REVIEWED.length} examples`);
      await expect(result).toContainText("Agreement 0%");
      await expect(page.getByTestId("align-errors")).toContainText(
        `${REVIEWED.length} example(s) could not be judged`,
      );
    } else {
      await expect(result).toContainText(
        `over ${REVIEWED.length}/${REVIEWED.length} examples`,
      );
      if (MODEL === "stand-in") {
        // 8 of 10 agree (one false positive, one false negative); kappa 0.60.
        await expect(result).toContainText("Agreement 80%");
        await expect(result).toContainText("0.60");
        await expect(result).toContainText("FP 1 · FN 1");
      }
      const agreementOf = async (): Promise<number> => {
        const text =
          (await page.getByTestId("align-result").textContent()) ?? "";
        const match = /Agreement (\d+)%/.exec(text);
        expect(match, `no agreement figure in: ${text}`).toBeTruthy();
        return Number(match![1]);
      };
      const strictAgreement = await agreementOf();
      // The lenient judge passes everything that is well formed: it should be rejected.
      await importAndCheck(lenientName);
      if (MODEL === "stand-in") {
        const lenient = page.getByTestId("align-result");
        await expect(lenient).toContainText("Agreement 60%");
        await expect(lenient).toContainText("0.20");
        await expect(lenient).toContainText("FP 4 · FN 0");
      }
      if (MODEL === "real") {
        // The invariant for any model that can do the job: the judge told to check every claim
        // agrees with the reviewers more than the one told only to check the reply is well formed.
        await expect(page.getByTestId("align-result")).toContainText(
          `over ${REVIEWED.length}/${REVIEWED.length} examples`,
        );
        expect(await agreementOf()).toBeLessThan(strictAgreement);
      }

      // ---- Close the loop: each disagreement becomes a permanent hard case in the test set --
      // J03 (judge passed, a person failed it) and J14 (judge failed, a person passed it).
      await page.goto("/caliber/eval-datasets");
      await page.getByRole("link", { name: datasetName }).click();
      await expect(page.getByTestId("eval-dataset-version")).toHaveText(
        `v${ROWS.length + 1}`,
      );
      for (const id of ["J03", "J14"]) {
        const row = ROWS.find((r) => r.id === id)!;
        await page.getByTestId("example-add-toggle").click();
        await page.getByTestId("example-input").fill(JSON.stringify(row.input));
        await page
          .getByTestId("example-expected")
          .fill(JSON.stringify(row.expected));
        await page.getByTestId("example-tags").fill("judge-disagreement");
        await page.getByTestId("example-save").click();
      }
      await expect(page.getByTestId("eval-dataset-version")).toHaveText(
        `v${ROWS.length + 3}`,
      );
      await expect(
        page.getByText(`${ROWS.length + 2} active examples`),
      ).toBeVisible();
    }
  });
});
