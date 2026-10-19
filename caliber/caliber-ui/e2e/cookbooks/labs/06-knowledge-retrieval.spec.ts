import * as fs from "node:fs";
import * as path from "node:path";
import { fileURLToPath } from "node:url";
import { expect, test, type Page } from "@playwright/test";

import { CaliberUi, uiOnlySlug } from "../ui-only-driver";

/**
 * Cookbook 06 (`docs-site/cookbooks/06-knowledge-retrieval-policy-qa-lab/README.md`), "Run it through
 * the UI".
 *
 * Needs a server started with `scripts/cookbook-env.sh up lab06 --flags cookbooks --s3` (an
 * S3-compatible object store, the local embedding model available). Nothing is seeded and no API call
 * is made while the steps run: every step is a click in the web app.
 */
const here = path.dirname(fileURLToPath(import.meta.url));
const ASSETS = path.resolve(
  process.env["COOKBOOK_06_ASSETS"] ??
    path.join(
      here,
      "../../../../../docs-site/cookbooks/06-knowledge-retrieval-policy-qa-lab/assets",
    ),
);
const SOURCES = path.join(ASSETS, "dataset/sources");
const CALIBRATION_FILE = path.join(
  ASSETS,
  "dataset/calibration-questions.jsonl",
);
const POLICY_FILES = fs
  .readdirSync(SOURCES)
  .filter((name) => name.endsWith(".md") && name !== "README.md")
  .sort();
const CALIBRATION_ROWS = fs
  .readFileSync(CALIBRATION_FILE, "utf8")
  .split("\n")
  .filter((line) => line.trim()).length;
const PROMPT_TEMPLATE = fs
  .readFileSync(path.join(ASSETS, "prompts/kb-answer.md"), "utf8")
  .split("---\n")
  .slice(2)
  .join("---\n")
  .trim();

const MODEL = "sentence-transformers/all-MiniLM-L6-v2";
/**
 * `CALIBER_LAB06_MODEL=real` says the server was started with `--real-llm` (README, "Run it with a
 * real provider"): answers are written by a model and calibration's Faithfulness and
 * Answer-correctness are real numbers. Unset (the default) is the simulated stack, where they are
 * blank. A calibration run makes three model calls per question, so it takes minutes, not seconds.
 */
const REAL = process.env["CALIBER_LAB06_MODEL"] === "real";
const CALIBRATE_TIMEOUT = REAL ? 1_500_000 : 120_000;
const QUESTION = "How long are security audit logs retained?";
const OUTSIDE_QUESTION = "What is the dress code for client meetings?";

/** Submit the Ask form through its own button (the sticky header can intercept a positional click). */
async function ask(page: Page, question: string, mode: string): Promise<void> {
  await page.getByTestId(`kb-explore-mode-${mode}`).click();
  const box = page.getByPlaceholder(
    "Ask a question about the selected documents…",
  );
  await box.fill(question);
  const answered = page.waitForResponse(
    (r) =>
      r.url().includes("/knowledge/query") &&
      r.request().method() === "POST" &&
      r.status() === 200,
    { timeout: 90_000 },
  );
  await page
    .getByTestId("kb-explore-ask")
    .getByRole("button", { name: "Ask" })
    .evaluate((btn) => {
      const form = (btn as HTMLButtonElement).closest("form");
      if (form)
        (form as HTMLFormElement).requestSubmit(btn as HTMLButtonElement);
    });
  await answered;
}

async function submitBuild(page: Page): Promise<void> {
  await page
    .getByRole("button", { name: /Create knowledge base|Create version of/ })
    .evaluate((btn) => {
      const form = (btn as HTMLButtonElement).closest("form");
      if (form)
        (form as HTMLFormElement).requestSubmit(btn as HTMLButtonElement);
    });
}

async function waitForBuild(page: Page, runIndex: number): Promise<void> {
  const row = page
    .getByRole("button")
    .filter({ hasText: /^KBR-/ })
    .nth(runIndex);
  await expect(row).toBeVisible({ timeout: 120_000 });
  // A failed build never becomes `completed`: stop at once and say why (the run's own error text),
  // instead of polling for the whole timeout. The usual cause on a fresh machine is a cold
  // embedding-model cache under HF_HUB_OFFLINE=1 (README, Prerequisites step 3).
  await expect
    .poll(
      async () => {
        const text = ((await row.textContent()) ?? "").toLowerCase();
        if (text.includes("failed")) {
          await row.click();
          // The run's error text is the longest red message on the page (status chips are short).
          const messages = await page
            .locator(".border-red-200.bg-red-50")
            .allTextContents();
          const reason = messages
            .map((message) => message.trim())
            .sort((left, right) => right.length - left.length)[0];
          throw new Error(
            `the knowledge-base build failed: ${reason || "(no error text shown)"}`,
          );
        }
        return text.includes("completed") ? "completed" : "pending";
      },
      { timeout: 300_000, intervals: [1_000, 2_000, 5_000] },
    )
    .toBe("completed");
}

test.describe("Cookbook 06: grounded knowledge assistant", () => {
  test("upload the policies, build, explore, calibrate, tune, activate and review", async ({
    page,
  }) => {
    test.setTimeout(
      Number(process.env["LAB_TIMEOUT_MS"] ?? (REAL ? 3_600_000 : 900_000)),
    );
    const ui = new CaliberUi(page);
    const suffix = uiOnlySlug("c06");
    const bucket = `policy-corpus-${suffix}`;
    const kbName = `policy-qa-kb-${suffix}`;
    const promptName = `kb-answer-${suffix}`;
    const datasetName = `policy-qa-calibration-${suffix}`;

    await ui.signIn();

    // ---- 1. Object Store: a bucket, a `policies` folder, the eleven documents ----------------
    await page.goto("/caliber/object-store");
    await expect(page.getByTestId("object-store-page")).toBeVisible();
    await expect(page.getByTestId("object-store-offline")).toHaveCount(0);
    await page.getByTestId("new-bucket-input").fill(bucket);
    await page.getByRole("button", { name: "Create bucket" }).click();
    await expect(page.getByTestId(`bucket-${bucket}`)).toBeVisible();
    await page.getByTestId(`bucket-${bucket}`).click();
    await page.getByRole("button", { name: "New folder" }).click();
    await page.getByTestId("new-folder-input").fill("policies");
    await page.getByRole("button", { name: "Create", exact: true }).click();
    await page.getByTitle("Open folder").first().click();
    await page
      .getByTestId("upload-input")
      .setInputFiles(POLICY_FILES.map((name) => path.join(SOURCES, name)));
    for (const name of POLICY_FILES) {
      await expect(page.getByTestId(`object-policies/${name}`)).toBeVisible({
        timeout: 60_000,
      });
    }
    // preview one stored document
    await page
      .getByTestId("object-policies/data-retention-policy.md")
      .getByTitle("View preview")
      .click();
    const preview = page.getByTestId("object-preview-modal");
    await expect(preview).toContainText("Data Retention and Deletion Policy");
    await expect(preview).toContainText("Security audit logs");
    await page.keyboard.press("Escape");

    // ---- 2. Knowledge Bases > New knowledge base: pick the folder, build version 1 -------------
    await page.goto("/caliber/knowledge-bases");
    await page.getByRole("button", { name: "New knowledge base" }).click();
    await expect(page.getByTestId("kb-workspace-header")).toBeVisible();
    await page.getByRole("button", { name: /^Create new\b/ }).click();
    await page.getByLabel("Bucket").selectOption(bucket);
    await page
      .getByRole("checkbox", { name: "Select folder policies" })
      .check();
    await expect(page.getByText("1 selected")).toBeVisible();
    await page.getByLabel("Knowledge-base name").fill(kbName);
    await page
      .getByLabel("Description")
      .fill("Policy QA lab: 11 HR, IT, finance, billing and security policies");
    await page.getByRole("button", { name: /^Advanced configuration/ }).click();
    await page.getByLabel("Chunking strategy").selectOption("markdown");
    await page.getByLabel("Embedding model").selectOption(MODEL);
    await page.getByLabel("Chunk size").fill("1400");
    await page.getByLabel("Overlap").fill("120");
    await submitBuild(page);
    await expect(page.getByText("Pipeline executions")).toBeVisible({
      timeout: 120_000,
    });
    await waitForBuild(page, 0);

    // ---- 3. Explore > Chunks: every document is there, with its source -------------------------
    const tabs = page.locator('[role="tablist"]').first();
    await page
      .getByTestId("kb-workspace-version-switcher")
      .selectOption({ index: 0 });
    await expect(
      page.getByTestId("kb-workspace-version-switcher"),
    ).toContainText("v1 (active)");
    await tabs.getByRole("button", { name: "Explore", exact: true }).click();
    await page.getByTestId("kb-explore-view-chunks").click();
    await expect(
      page.getByText("Inspect retrieved units for v1"),
    ).toBeVisible();
    await expect(page.getByText(/ · chunk \d+/).first()).toBeVisible();
    await page.getByLabel("Search chunks").fill("365 days");
    await expect(
      page.getByText(/data-retention-policy\.md · chunk/).first(),
    ).toBeVisible();
    await page.getByLabel("Search chunks").fill("");
    const sources = page
      .locator("select")
      .filter({ hasText: "All source documents" });
    await expect
      .poll(
        async () =>
          (await sources.first().locator("option").allTextContents()).sort(),
        {
          timeout: 30_000,
        },
      )
      .toEqual(["All source documents", ...POLICY_FILES].sort());

    // ---- 4. Explore > Query: the same question in three modes, with citations -------------------
    await page.getByTestId("kb-explore-view-ask").click();
    for (const mode of ["dense", "hybrid", "graph_hybrid"]) {
      await ask(page, QUESTION, mode);
      await expect(page.getByText("Citations").first()).toBeVisible();
      await expect(page.getByText(/^\[1\] .*\.md$/).first()).toBeVisible();
      await expect(page.getByText("Retrieved chunks").first()).toBeVisible();
      await expect(page.getByText(/365 days/).first()).toBeVisible();
    }
    // a question the corpus does not cover still retrieves chunks: a person has to notice
    await ask(page, OUTSIDE_QUESTION, "dense");
    await expect(page.getByText("Retrieved chunks").first()).toBeVisible();

    // ---- 5. Prompts: the answer prompt, and the calibration questions imported from the file ----
    await page.goto("/caliber/prompts");
    await page.getByRole("button", { name: "New prompt" }).click();
    await page.getByRole("button", { name: /Write \/ paste/ }).click();
    await page.getByLabel("Prompt text").fill(PROMPT_TEMPLATE);
    await page.getByLabel("Prompt name").fill(promptName);
    await page.getByRole("button", { name: "Create and Open" }).click();
    await expect(page.getByTestId("version-panel")).toBeVisible();
    await page
      .getByRole("button", { name: "Calibration", exact: true })
      .click();
    await page
      .getByLabel("Upload calibration dataset")
      .setInputFiles(CALIBRATION_FILE);
    await page.getByPlaceholder("Dataset name").fill(datasetName);
    await page.getByRole("button", { name: "Upload Dataset" }).click();
    await expect(
      page.getByText(
        `Uploaded ${CALIBRATION_ROWS} examples to ${datasetName}.`,
      ),
    ).toBeVisible({ timeout: 120_000 });

    // ---- 6. Knowledge Bases > Calibrate: a baseline, then a better configuration ---------------
    const openKb = async (): Promise<void> => {
      await page.goto("/caliber/knowledge-bases");
      await page
        .locator('[data-testid^="kb-card-"]')
        .filter({ hasText: kbName })
        .first()
        .click();
      await expect(page.getByTestId("kb-workspace-header")).toBeVisible();
    };
    await openKb();
    await tabs.getByRole("button", { name: "Calibrate", exact: true }).click();
    await page
      .getByTestId("kb-calibrate-dataset")
      .selectOption({ label: datasetName });
    await page.getByTestId("kb-calibrate-mode").selectOption("dense");
    await page.getByTestId("kb-calibrate-topk").fill("1");
    await page.getByTestId("kb-calibrate-run").click();
    await expect(
      page.getByTestId("kb-calibrate-metric-recall_at_k"),
    ).toContainText("88%", {
      timeout: CALIBRATE_TIMEOUT,
    });
    await expect(
      page.getByTestId("kb-calibrate-metric-ndcg_at_k"),
    ).toContainText("90%");
    // the judged metrics need a real model: blank on the simulated stack, percentages on a real one
    if (REAL) {
      for (const metric of ["faithfulness", "answer_correctness"]) {
        await expect(
          page.getByTestId(`kb-calibrate-metric-${metric}`),
        ).toContainText(/\d+%/);
      }
    } else {
      await expect(
        page.getByTestId("kb-calibrate-metric-faithfulness"),
      ).toContainText("—");
    }
    await page.getByTestId("kb-calibrate-set-baseline").click();
    await expect(
      page.getByTestId("kb-calibrate-baseline-marker"),
    ).toBeVisible();
    await page.getByTestId("kb-calibrate-mode").selectOption("hybrid");
    await page.getByTestId("kb-calibrate-run").click();
    await expect(
      page.getByTestId("kb-calibrate-metric-recall_at_k"),
    ).toContainText("93%", {
      timeout: CALIBRATE_TIMEOUT,
    });
    await expect(
      page.getByTestId("kb-calibrate-delta-recall_at_k"),
    ).toContainText("+5%");
    await expect(
      page.getByTestId("kb-calibrate-delta-ndcg_at_k"),
    ).toContainText("+5%");

    // ---- 7. Build > New version: smaller chunks; a finished build becomes the active version ---
    await tabs.getByRole("button", { name: "Build", exact: true }).click();
    await page.getByRole("button", { name: /^New version\b/ }).click();
    await page
      .getByRole("checkbox", { name: "Select folder policies" })
      .check();
    await page.getByRole("button", { name: /^Advanced configuration/ }).click();
    await page.getByLabel("Chunking strategy").selectOption("markdown");
    await page.getByLabel("Embedding model").selectOption(MODEL);
    await page.getByLabel("Chunk size").fill("500");
    await page.getByLabel("Overlap").fill("60");
    await submitBuild(page);
    const switcher = page.getByTestId("kb-workspace-version-switcher");
    await expect(switcher).toContainText("v2", { timeout: 60_000 });
    await waitForBuild(page, 0);
    await expect(
      switcher.locator("option", { hasText: "v2 (active)" }),
    ).toHaveCount(1, {
      timeout: 60_000,
    });

    // ---- 8. Set active: v1 live again, then v2 -----------------------------------------------
    await switcher.selectOption({ label: "v1" });
    await page.getByTestId("kb-workspace-set-active").click();
    await expect(
      switcher.locator("option", { hasText: "v1 (active)" }),
    ).toHaveCount(1);
    await switcher.selectOption({ label: "v2" });
    await page.getByTestId("kb-workspace-set-active").click();
    await expect(
      switcher.locator("option", { hasText: "v2 (active)" }),
    ).toHaveCount(1);

    // ---- 9. Cookbooks: install the answer workflow, point its Knowledge node at the KB ---------
    const workflowName = `cookbook-06-kb-assistant-${suffix}`;
    await ui.installCookbookDraft("06", workflowName);
    await page.getByTestId("outline-knowledge").click();
    await page
      .getByTestId("inspector-knowledge-base")
      .selectOption({ label: kbName });
    await page.getByTestId("inspector-knowledge-top-k").fill("3");
    await page.getByTestId("editor-save").click();
    await page.getByTestId("editor-validate").click();
    await expect(page.getByTestId("editor-message")).toContainText("Valid.");
    const workflowId = /\/workflows\/(WF-[^/]+)\//.exec(page.url())![1]!;

    // ---- 10. Workflows: resume it, ask it a question, read the run ----------------------------
    await page.goto(`/caliber/workflows/${workflowId}`);
    await page.getByTestId("workflow-pause-toggle").click();
    await expect(page.getByTestId("workflow-pause-toggle")).toHaveText("Pause");
    await page.getByTestId("workflow-open-editor").click();
    await page.getByTestId("editor-run-monitor").click();
    await page.getByTestId("run-input").fill(QUESTION);
    await page.getByTestId("run-execute").click();
    await expect(page.getByTestId("run-status-badge")).toContainText(
      /completed/i,
      {
        timeout: 120_000,
      },
    );
    if (REAL) {
      // a model wrote the answer: the output is its sentence, not the extractive digest
      await expect(page.getByTestId("run-output")).not.toContainText(
        "Most relevant retrieved evidence",
      );
    } else {
      await expect(page.getByTestId("run-output")).toContainText(
        "data-retention-policy.md",
      );
    }
    await expect(page.getByTestId("run-output")).toContainText("365 days");

    // ---- 11. Observability, then Review Queues: send that run's trace to a person --------------
    await page.goto("/caliber/observability");
    await page.getByTestId("observability-trace-row").first().click();
    await expect(page).toHaveURL(/trace=tr-[0-9a-f]+/);
    const traceId = new URL(page.url()).searchParams.get("trace")!;
    const queueName = `policy-qa-review-${suffix}`;
    await ui.createReviewQueue({
      name: queueName,
      description: "Runs the confidence gate sent to a person",
      reviewer: "@admin",
      question: {
        key: "decision_justified",
        title:
          "Is abstaining or clarifying justified by the retrieved evidence?",
      },
    });
    await page.getByRole("button", { name: queueName }).click();
    await page.getByLabel("Trace ids").fill(traceId);
    await page.getByRole("button", { name: "Enqueue" }).click();
    await expect(page.getByText(traceId).first()).toBeVisible();
    await page.getByRole("button", { name: "Pass" }).click();
    await page.getByRole("button", { name: "Submit review" }).click();
    // wait for the submit to land (the write-back to MLflow is slower on a real stack) before
    // opening the completed item
    await expect(page.getByText("All items reviewed.")).toBeVisible();
    await page.getByText(traceId).first().click();
    await expect(
      page.getByText(/wrote 1 assessment\(s\) back to the trace/),
    ).toBeVisible();
  });
});
