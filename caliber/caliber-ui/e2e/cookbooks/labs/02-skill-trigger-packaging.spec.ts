import * as fs from "node:fs";
import * as path from "node:path";
import { fileURLToPath } from "node:url";
import { expect, test, type Locator, type Page } from "@playwright/test";

import { CaliberUi, uiOnlySlug } from "../ui-only-driver";
import { stackIsSimulated } from "./lab-mode";

/**
 * Cookbook 02 (`docs-site/cookbooks/02-skill-trigger-packaging-lab/README.md`), "Run it through the UI".
 *
 * Needs a server started with `scripts/cookbook-env.sh up lab02 --flags cookbooks` (simulated
 * language model and evaluator, the alpha default) or `... up lab02h --flags cookbooks --real-llm`
 * (a real provider). Nothing is seeded and no API call is made from this file: the mode is read
 * from the provider banner the app shows, and every step is a click in the web app.
 *
 * Mode differences (steps 9, 10 and 12b; everything else is the same on both):
 *   simulated: Calibrate needs no test set; the review says "Simulated evaluation — not evidence"
 *     and "0 evaluation examples", and the candidate is the canned refinement.
 *   real: the 8-row test set `assets/dataset/skill-eval-cases.jsonl` is created on Test Sets, then
 *     Calibrate is run with that test set and a note on what to fix; the review has no simulated
 *     banner and says "8 evaluation examples"; the candidate is model-written, so the spec asserts
 *     invariants (it differs from the skill, the gate passed, it applies and rolls back), never text
 *     or a score. A calibration with no test set fails, and the run says so.
 *
 * What it proves, in the README's order:
 *   1. the Cookbooks gallery lists Precision Skills (the `cookbooks` flag is on);
 *   2. a workspace is created for the lab;
 *   3. the skill is authored in the Build Skill wizard from `assets/skills/support-tone-and-citation.md`
 *      (with the six starter should-not-trigger phrases);
 *   4. Render Preview fills the variables and reports what is unresolved;
 *   5. Trigger Tests: a positive, a negative and a near miss that wrongly selects the skill;
 *   6. Scenario Sets + Run: the starter phrases get nine of twelve cases wrong, saved as a run;
 *   7. Details & package: the tightened trigger phrases are pasted into the metadata, and the same
 *      twelve cases are all right; that run is saved and pinned as the baseline;
 *   8. a run compared with the baseline shows the regressions;
 *   9. Calibrate reaches a candidate, flagged simulated, with zero evaluation examples;
 *  10. Apply makes the candidate version 2; Roll back restores the original as version 3;
 *  11. the package is downloaded, re-importing it under the same name is rejected, and importing it
 *      under a new name keeps the instructions but not the selection metadata;
 *  12. failure paths: an invalid name and a rejected summary in the wizard, a calibration with no
 *      baseline, and a viewer (a workspace member with role Viewer) who cannot apply;
 *  13. the skills are archived.
 */
const here = path.dirname(fileURLToPath(import.meta.url));
const ASSETS = path.resolve(
  process.env["COOKBOOK_02_ASSETS"] ??
    path.join(
      here,
      "../../../../../docs-site/cookbooks/02-skill-trigger-packaging-lab/assets",
    ),
);

interface TriggerCase {
  id: string;
  inputs: {
    user_message: string;
    artifact_type?: string;
    session_goal?: string;
  };
  expectations: { is_selected: boolean };
}

const CASES: TriggerCase[] = fs
  .readFileSync(path.join(ASSETS, "dataset/trigger-cases.jsonl"), "utf8")
  .split("\n")
  .filter((line) => line.trim())
  .map((line) => JSON.parse(line) as TriggerCase);
const byId = (id: string): TriggerCase => CASES.find((c) => c.id === id)!;

const SKILL_FILE = fs.readFileSync(
  path.join(ASSETS, "skills/support-tone-and-citation.md"),
  "utf8",
);
const SKILL_PARTS = SKILL_FILE.replace(/^---\n/, "").split("\n---\n");
const FRONTMATTER = SKILL_PARTS[0]!;
const BODY = SKILL_PARTS.slice(1).join("\n---\n");
const SKILL_CONTENT = BODY.trim();
const frontmatterValue = (key: string): string =>
  FRONTMATTER.split("\n")
    .find((line) => line.startsWith(`${key}:`))!
    .slice(key.length + 1)
    .trim();
const frontmatterList = (key: string): string[] =>
  frontmatterValue(key)
    .replace(/^\[|\]$/g, "")
    .split(",")
    .map((item) => item.trim())
    .filter(Boolean);
const SUMMARY = frontmatterValue("summary").replace(/^"|"$/g, "");
const TAGS = frontmatterList("tags");
const STARTER_EXCLUSIONS = frontmatterList("should_not_trigger");
const TIGHTENED = JSON.parse(
  fs.readFileSync(path.join(ASSETS, "skills/trigger-phrases.json"), "utf8"),
) as { test_triggers: Record<string, string[]> };
const TEST_SET: {
  id: string;
  tags: string[];
  input: object;
  expected: object;
}[] = fs
  .readFileSync(path.join(ASSETS, "dataset/skill-eval-cases.jsonl"), "utf8")
  .split("\n")
  .filter((line) => line.trim())
  .map((line) => JSON.parse(line));
const CALIBRATION_NOTES = fs
  .readFileSync(path.join(ASSETS, "skills/calibration-notes.txt"), "utf8")
  .trim();
const RENDER = JSON.parse(
  fs.readFileSync(path.join(ASSETS, "dataset/render-vars.json"), "utf8"),
) as {
  variables: Record<string, string>;
  partial: { variables: Record<string, string> };
};

/**
 * The twelve cases the page runs as scenarios. The Scenario Sets runner sends the message alone
 * (no artifact type or session goal), so these are the cases whose starter decision is wrong in
 * that mode (nine) plus three the starter phrases already get right.
 */
const SCENARIO_IDS = [
  "T01",
  "T07",
  "T28",
  "T14",
  "T15",
  "T23",
  "T27",
  "T29",
  "T31",
  "T32",
  "T34",
  "T35",
];
const STARTER_MISSES_MESSAGE_ONLY = 9;

async function buildSkillThroughTheWizard(
  page: Page,
  skill: {
    name: string;
    summary: string;
    content: string;
    tags?: string[];
    exclusions?: string[];
  },
): Promise<void> {
  await page.goto("/caliber/skills");
  await page.getByTestId("new-skill").click();
  await expect(
    page.getByRole("heading", { name: "Build a new skill" }),
  ).toBeVisible();
  await page.getByTestId("skill-wiz-name").fill(skill.name);
  await page.getByTestId("skill-wiz-owner").fill("admin");
  await page.getByTestId("skill-wiz-category-customer_support").click();
  for (const tag of skill.tags ?? []) {
    await page.getByTestId("skill-wiz-tag-input").fill(tag);
    await page.getByTestId("skill-wiz-add-tag").click();
  }
  await page.getByTestId("skill-wizard-next").click();
  await page.getByTestId("skill-wiz-summary").fill(skill.summary);
  await page.getByTestId("skill-wiz-content").fill(skill.content);
  await page.getByTestId("skill-wizard-next").click();
  await page.getByTestId("skill-wizard-next").click();
  for (const phrase of skill.exclusions ?? []) {
    await page.getByTestId("skill-wiz-anti-input").fill(phrase);
    await page.getByTestId("skill-wiz-add-anti").click();
  }
  await page.getByTestId("skill-wizard-next").click();
  await page.getByTestId("skill-wizard-submit").click();
  await expect(
    page.getByRole("heading", { name: "Skills", exact: true }),
  ).toBeVisible();
}

async function openSkill(page: Page, name: string): Promise<void> {
  await page.goto("/caliber/skills");
  await page.getByLabel("Search skills").fill(name);
  await page.getByTestId(`skill-open-${name}`).first().click();
  await expect(page.getByTestId("skill-workspace-header")).toContainText(name);
}

async function stage(page: Page, label: string): Promise<void> {
  await page
    .getByRole("tablist")
    .getByRole("button", { name: label, exact: true })
    .click();
}

async function checkSelection(page: Page, c: TriggerCase): Promise<Locator> {
  await page.getByTestId("skill-trigger-message").fill(c.inputs.user_message);
  await page
    .getByLabel("Trigger test artifact type")
    .fill(c.inputs.artifact_type ?? "");
  await page
    .getByLabel("Trigger test session goal")
    .fill(c.inputs.session_goal ?? "");
  await page.getByTestId("skill-trigger-run").click();
  const result = page
    .getByTestId("skill-trigger-result")
    .filter({ hasText: c.inputs.user_message })
    .first();
  await expect(result).toBeVisible();
  return result;
}

async function addScenarios(page: Page, ids: string[]): Promise<void> {
  await stage(page, "Scenario Sets");
  for (const id of ids) {
    const c = byId(id);
    await page.getByLabel("Scenario user message").fill(c.inputs.user_message);
    const expected = page.getByLabel("Expected: skill should be selected");
    if ((await expected.isChecked()) !== c.expectations.is_selected)
      await expected.click();
    await page.getByTestId("skill-scenario-add").click();
  }
  await expect(page.getByTestId("skill-scenario-case")).toHaveCount(ids.length);
}

async function runScenarios(page: Page, count: number): Promise<void> {
  await stage(page, "Trigger Tests");
  await page.getByRole("button", { name: `Run ${count} scenarios` }).click();
  await expect(page.getByTestId("skill-trigger-result")).toHaveCount(count);
}

/** Test Sets: create the skill's test set and import its rows from `assets/dataset/skill-eval-cases.jsonl`. */
async function createTestSet(page: Page, name: string): Promise<void> {
  await page.goto("/caliber/eval-datasets");
  await page.getByRole("button", { name: "+ New Test Set" }).click();
  await page.getByPlaceholder("factual-checks").fill(name);
  await page.getByPlaceholder("@sarah").fill("admin");
  await page.getByRole("button", { name: "Create", exact: true }).click();
  await page.getByText(name, { exact: true }).first().click();
  await expect(page.getByTestId("eval-dataset-detail")).toBeVisible();
  await page.getByTestId("import-toggle").click();
  await page
    .getByTestId("import-file")
    .setInputFiles(path.join(ASSETS, "dataset/skill-eval-cases.jsonl"));
  await expect(page.getByTestId("import-summary")).toContainText(
    `${TEST_SET.length} rows ready to import`,
  );
  await page.getByTestId("import-save").click();
  await expect(page.getByTestId("example-row")).toHaveCount(TEST_SET.length);
}

/** Runs stage: pick the test set (real provider) and the note, then Calibrate. */
async function calibrateWith(
  page: Page,
  testSetLabel: string | null,
  notes: string | null,
): Promise<void> {
  if (testSetLabel !== null) {
    await page
      .getByTestId("skill-calibrate-test-set")
      .selectOption({ label: testSetLabel });
  }
  if (notes !== null)
    await page.getByTestId("skill-calibrate-notes").fill(notes);
  await page.getByTestId("skill-calibrate-btn").click();
  await expect(page.getByTestId("skill-calibrate-result")).toContainText(
    "Calibration queued",
  );
}

/**
 * Calibrate until a candidate is ready to review. A real evaluator can reject a candidate (the
 * regression gate fires when any dimension is worse than the skill by more than 0.02, and eight
 * test rows are coarse), so a real run is retried up to twice; the rejected run stays in the list and says
 * why. Returns when the review button is on the page.
 */
async function calibrateToCandidate(
  page: Page,
  real: boolean,
  testSetLabel: string | null,
  notes: string | null,
): Promise<void> {
  const review = page.getByTestId(/^skill-review-RFN-/);
  const rejected = page
    .getByTestId(/^skill-job-RFN-/)
    .filter({ hasText: "regression gate failed" });
  for (let attempt = 1; attempt <= (real ? 3 : 1); attempt++) {
    const rejectedBefore = await rejected.count();
    await calibrateWith(page, testSetLabel, notes);
    await expect
      .poll(
        async () =>
          (await review.count()) > 0
            ? "ready"
            : (await rejected.count()) > rejectedBefore
              ? "rejected"
              : "pending",
        { timeout: real ? 600_000 : 120_000, intervals: [2_000] },
      )
      .not.toBe("pending");
    if ((await review.count()) > 0) break;
  }
  await expect(review.first()).toBeVisible();
}

function liveBadge(page: Page, version: number): Locator {
  return page
    .getByTestId(`version-row-${version}`)
    .getByTestId("version-live-badge");
}

test.describe("Cookbook 02: precision skills", () => {
  // a missing control must fail the step in seconds, not wait out the whole-test timeout
  test.use({ actionTimeout: 30_000 });

  test("authors, tests, tightens, calibrates, applies, rolls back, packages and archives", async ({
    page,
  }) => {
    test.setTimeout(1_800_000);
    const suffix = uiOnlySlug("c02");
    const testSetName = `lab02-skill-replies-${suffix}`;
    const skillName = `support-tone-and-citation-${suffix}`;
    const copyName = `${skillName}-copy`;
    const bareName = `no-baseline-${suffix}`;
    const workspaceName = `lab02-${suffix}`;
    const viewerId = `lab02v-${suffix}`.slice(0, 40);
    const viewerPassword = "lab02-viewer-pw-1";
    await new CaliberUi(page).signIn();
    const real = !(await stackIsSimulated(page));

    // ---- 1. Get started > Cookbooks lists Precision Skills ------------------------------
    await page.goto("/caliber/cookbooks");
    const card = page.getByTestId("cookbook-card-02");
    await expect(card).toContainText("Precision Skills");
    await expect(card).toContainText("trigger tests");
    await expect(card).toContainText("Ready");

    // ---- 2. A workspace for the lab, so a viewer can be added later ----------------------
    await page.goto("/caliber/skills");
    await page.getByRole("button", { name: "Create workspace" }).click();
    await page.getByLabel("Workspace name").fill(workspaceName);
    await page.getByRole("button", { name: "Create and select" }).click();
    await expect(page.getByLabel("Active workspace")).toContainText(
      workspaceName,
    );

    // ---- 3. Agent resources > Skills > Build Skill ---------------------------------------
    await buildSkillThroughTheWizard(page, {
      name: skillName,
      summary: SUMMARY,
      content: SKILL_CONTENT,
      tags: TAGS,
      exclusions: STARTER_EXCLUSIONS,
    });
    await openSkill(page, skillName);
    await expect(page.getByTestId("skill-workspace-header")).toContainText(
      "v1",
    );
    await expect(page.getByTestId("version-row-1")).toBeVisible();
    await expect(liveBadge(page, 1)).toBeVisible();

    // ---- 4. Render Preview: all variables, then only one ---------------------------------
    await stage(page, "Render Preview");
    await page
      .getByTestId("skill-playground-variables")
      .fill(JSON.stringify(RENDER.variables));
    await page.getByTestId("skill-playground-render").click();
    await expect(
      page.getByRole("heading", { name: "Rendered Output" }),
    ).toBeVisible();
    const rendered = page
      .locator("pre")
      .filter({ hasText: "Write the reply now." });
    await expect(rendered).toContainText(
      "Cite [KB-1042] (duplicate-charge refunds)",
    );
    await expect(rendered).toContainText("Audience: non-technical customer");
    await expect(rendered).not.toContainText("{{");
    await expect(page.getByText(/\d+ unresolved/)).toHaveCount(0);
    await page
      .getByTestId("skill-playground-variables")
      .fill(JSON.stringify(RENDER.partial.variables));
    await page.getByTestId("skill-playground-render").click();
    await expect(page.getByText("2 unresolved")).toBeVisible();
    await expect(rendered).toContainText("{{ policy_context }}");
    await expect(rendered).toContainText("Audience: non-technical customer");

    // ---- 5. Trigger Tests: a positive, a negative, and a near miss that gets through -----
    await stage(page, "Trigger Tests");
    const positive = await checkSelection(page, byId("T01"));
    await expect(positive.getByTestId("skill-trigger-selected")).toHaveText(
      "selected",
    );
    await expect(positive).toContainText(/Score \d+/);
    const negative = await checkSelection(page, byId("T07"));
    await expect(negative.getByTestId("skill-trigger-selected")).toHaveText(
      "not selected",
    );
    await expect(negative).toContainText(
      "excluded:negative_trigger:signing key",
    );
    const nearMiss = await checkSelection(page, byId("T27"));
    await expect(nearMiss.getByTestId("skill-trigger-selected")).toHaveText(
      "selected",
    );
    await expect(nearMiss).toContainText("auto:summary");

    // ---- 6. Scenario Sets + Run: round 1 with the starter phrases ------------------------
    await addScenarios(page, SCENARIO_IDS);
    await runScenarios(page, SCENARIO_IDS.length);
    await expect(page.getByText("regression", { exact: true })).toHaveCount(
      STARTER_MISSES_MESSAGE_ONLY,
    );
    await expect(page.getByText("matches expectation")).toHaveCount(
      SCENARIO_IDS.length - STARTER_MISSES_MESSAGE_ONLY,
    );
    await page.getByTestId("skill-trigger-save").click();
    await expect(page.getByTestId("skill-trigger-saved")).toContainText(
      "Saved run SKR-",
    );

    // ---- 7. Details & package: tighten the trigger phrases, then round 2 -----------------
    await page.getByTestId("skill-open-detail").click();
    await expect(page.getByTestId("skill-metadata")).toContainText(
      '"should_not_trigger"',
    );
    await expect(page.getByTestId("skill-metadata")).not.toContainText(
      "kubernetes",
    );
    await page.getByTestId("skill-edit-btn").click();
    const metadata = page.locator('textarea[data-testid="skill-metadata"]');
    const current = JSON.parse(await metadata.inputValue()) as Record<
      string,
      unknown
    >;
    await metadata.fill(
      JSON.stringify(
        { ...current, test_triggers: TIGHTENED.test_triggers },
        null,
        2,
      ),
    );
    await page.getByTestId("skill-save").click();
    await expect(page.getByText("Saved.", { exact: true })).toBeVisible();
    await expect(page.getByTestId("skill-metadata")).toContainText(
      "kubernetes",
    );

    await openSkill(page, skillName);
    await addScenarios(page, SCENARIO_IDS);
    await runScenarios(page, SCENARIO_IDS.length);
    await expect(page.getByText("matches expectation")).toHaveCount(
      SCENARIO_IDS.length,
    );
    await expect(page.getByText("regression", { exact: true })).toHaveCount(0);
    await page.getByTestId("skill-trigger-save").click();
    await expect(page.getByTestId("skill-trigger-saved")).toContainText(
      "Saved run SKR-",
    );

    // ---- 8. Runs: pin round 2 as the baseline, compare round 1 against it ----------------
    await stage(page, "Runs");
    const history = page.getByTestId("skill-workspace-run-history");
    await expect(
      history.getByRole("button", { name: /View run SKR-/ }),
    ).toHaveCount(2);
    await expect(history).toContainText("100%");
    await expect(history).toContainText("12 pass");
    await page.getByRole("button", { name: "Set as baseline" }).click();
    await expect(page.getByTestId("skill-run-baseline-marker")).toBeVisible();
    await history
      .getByRole("button", { name: /View run SKR-/ })
      .last()
      .click();
    await expect(page.getByTestId("skill-run-score-delta")).toContainText(
      "Net score -75%",
    );
    await expect(page.getByTestId("skill-run-regressions")).toContainText(
      `${STARTER_MISSES_MESSAGE_ONLY} regressions`,
    );

    // ---- 9. Calibrate: a candidate to review ----------------------------------------------
    // Simulated: canned evidence, flagged, with no evaluation examples. Real provider: the
    // candidate is scored against the skill's test set, so create that first.
    let testSetOption: string | null = null;
    if (real) {
      await createTestSet(page, testSetName);
      await page.goto("/caliber/eval-datasets");
      await page.getByLabel("Search test sets").fill(testSetName);
      await expect(page.getByText(testSetName, { exact: true })).toBeVisible();
      await openSkill(page, skillName);
      await stage(page, "Runs");
      await expect(page.getByTestId("skill-calibrate-test-set")).toContainText(
        testSetName,
      );
      testSetOption = await page
        .getByTestId("skill-calibrate-test-set")
        .locator("option", { hasText: testSetName })
        .innerText();
    }
    await calibrateToCandidate(
      page,
      real,
      testSetOption,
      real ? CALIBRATION_NOTES : null,
    );
    await page
      .getByTestId(/^skill-review-RFN-/)
      .first()
      .click();
    if (real) {
      await expect(page.getByTestId("skill-simulated-evaluation")).toHaveCount(
        0,
      );
      await expect(
        page.getByText(`${TEST_SET.length} evaluation examples`),
      ).toBeVisible();
      await expect(page.getByTestId("skill-review-panel")).toContainText(
        "gate passed",
      );
      await expect(page.getByTestId("skill-candidate-diff")).toBeVisible();
    } else {
      await expect(
        page.getByTestId("skill-simulated-evaluation"),
      ).toContainText("Simulated evaluation — not evidence");
      await expect(page.getByText("0 evaluation examples")).toBeVisible();
      await expect(page.getByTestId("skill-candidate-diff")).toContainText(
        "Refinement added by SkillMetaPrompt",
      );
    }

    // ---- 10. Apply, then roll back from the version history ------------------------------
    await page
      .getByTestId(/^skill-apply-RFN-/)
      .first()
      .click();
    await expect(page.getByTestId("skill-apply-result")).toContainText(
      "the skill is now version 2",
      { timeout: 60_000 },
    );
    await stage(page, "Author");
    await expect(liveBadge(page, 2)).toBeVisible();
    if (real) {
      // model-written: the live text is no longer the original instructions
      await expect(page.getByLabel("Skill content")).not.toHaveValue(
        SKILL_CONTENT,
      );
    } else {
      await expect(page.getByLabel("Skill content")).toHaveValue(
        /Refinement added by SkillMetaPrompt/,
      );
    }
    await page
      .getByTestId("version-row-2")
      .getByTestId("version-rollback")
      .click();
    await expect(page.getByTestId("confirm-dialog")).toBeVisible();
    await page.getByTestId("confirm-dialog-confirm").click();
    await expect(liveBadge(page, 3)).toBeVisible();
    await expect(page.getByLabel("Skill content")).not.toHaveValue(
      /Refinement added by SkillMetaPrompt/,
    );
    await expect(page.getByLabel("Skill content")).toHaveValue(
      /Write the reply now\./,
    );
    await expect(page.getByLabel("Skill content")).toHaveValue(SKILL_CONTENT);

    // ---- 11. Package: download, re-import (rejected, then renamed) -----------------------
    await page.getByTestId("skill-open-detail").click();
    const [download] = await Promise.all([
      page.waitForEvent("download"),
      page.getByTestId("skill-package-download").click(),
    ]);
    expect(download.suggestedFilename()).toBe(`${skillName}.zip`);
    const zipPath = path.join(
      test.info().outputDir,
      download.suggestedFilename(),
    );
    await download.saveAs(zipPath);
    const zipBytes = fs.readFileSync(zipPath);
    expect(zipBytes.subarray(0, 2).toString()).toBe("PK");
    for (const member of [
      `${skillName}/SKILL.md`,
      `${skillName}/agents/openai.yaml`,
    ]) {
      expect(zipBytes.includes(Buffer.from(member))).toBe(true);
    }
    const importInput = page.getByLabel("Import skill package ZIP");
    await importInput.setInputFiles(zipPath);
    await expect(page.getByTestId("skill-package-import-error")).toContainText(
      "already exists; choose rename or merge explicitly",
    );
    await page
      .getByTestId("skill-package-conflict-strategy")
      .selectOption("rename");
    await page.getByTestId("skill-package-rename-to").fill(copyName);
    await importInput.setInputFiles(zipPath);
    await expect(page.getByRole("heading", { name: copyName })).toBeVisible();
    await expect(page).toHaveURL(/\/caliber\/skills\/SK-/);
    // the copy has the instructions but not the selection metadata
    await expect(page.getByTestId("skill-metadata")).not.toContainText(
      "test_triggers",
    );
    await expect(page.getByTestId("skill-metadata")).toContainText(
      "openai_package",
    );
    await openSkill(page, copyName);
    await stage(page, "Trigger Tests");
    const driftedCopy = await checkSelection(page, byId("T28"));
    await expect(driftedCopy.getByTestId("skill-trigger-selected")).toHaveText(
      "selected",
    );
    await openSkill(page, skillName);
    await stage(page, "Trigger Tests");
    const original = await checkSelection(page, byId("T28"));
    await expect(original.getByTestId("skill-trigger-selected")).toHaveText(
      "not selected",
    );

    // ---- 12a. Invalid skill content is refused -------------------------------------------
    await page.goto("/caliber/skills");
    await page.getByTestId("new-skill").click();
    // the name box turns spaces and underscores into hyphens, so a leading hyphen is the way to
    // type a name that is not kebab-case
    await page.getByTestId("skill-wiz-name").fill("-support-tone-");
    await expect(page.getByTestId("skill-name-error")).toBeVisible();
    await expect(page.getByTestId("skill-wizard-next")).toBeDisabled();
    // a reserved prefix passes the page and is refused by the server on Create Skill
    await page.getByTestId("skill-wiz-name").fill(`claude-helper-${suffix}`);
    await page.getByTestId("skill-wiz-owner").fill("admin");
    await page.getByTestId("skill-wizard-next").click();
    await page.getByTestId("skill-wiz-summary").fill("Use <b>bold</b> replies");
    await page.getByTestId("skill-wiz-content").fill("Reply politely.");
    for (let i = 0; i < 3; i += 1)
      await page.getByTestId("skill-wizard-next").click();
    await expect(page.getByTestId("skill-wiz-checklist")).toContainText(
      "No reserved name prefix (claude, anthropic)",
    );
    await page.getByTestId("skill-wizard-submit").click();
    await expect(page.getByTestId("skill-wizard-error")).toContainText(
      "skill names starting with 'claude' are reserved",
    );
    // fix the name: now the angle brackets in the summary are refused
    for (let i = 0; i < 4; i += 1)
      await page.getByTestId("skill-wizard-back").click();
    await page.getByTestId("skill-wiz-name").fill(`xml-summary-${suffix}`);
    for (let i = 0; i < 4; i += 1)
      await page.getByTestId("skill-wizard-next").click();
    await page.getByTestId("skill-wizard-submit").click();
    await expect(page.getByTestId("skill-wizard-error")).toContainText(
      "summary must not contain XML angle brackets",
    );
    await page.getByTestId("skill-wizard-close").click();
    await page.getByLabel("Search skills").fill("xml-summary");
    await expect(
      page.getByTestId(`skill-open-xml-summary-${suffix}`),
    ).toHaveCount(0);

    // ---- 12b. A calibration with no baseline pinned ---------------------------------------
    // The real evaluator gates on an absolute score, so the real run uses the lab's own
    // instructions (a one-line skill scores below the gate on the support cases).
    await buildSkillThroughTheWizard(page, {
      name: bareName,
      summary: real ? SUMMARY : "Reply kindly.",
      content: real ? SKILL_CONTENT : "Reply kindly to {{ who }}.",
    });
    await openSkill(page, bareName);
    await stage(page, "Runs");
    await expect(page.getByText("No saved runs yet")).toBeVisible();
    await expect(page.getByTestId("skill-run-baseline-marker")).toHaveCount(0);
    if (real) {
      // With no test set a real evaluator has nothing to score against: the run fails and says why.
      await calibrateWith(page, null, null);
      await expect(page.getByTestId(/^skill-job-RFN-/).first()).toContainText(
        "no active examples",
        { timeout: 300_000 },
      );
      await expect(page.getByTestId(/^skill-job-RFN-/).first()).toContainText(
        "failed",
      );
      await expect(page.getByTestId(/^skill-review-RFN-/)).toHaveCount(0);
      // With the test set the same skill, with no baseline, calibrates.
      await calibrateToCandidate(page, true, testSetOption, CALIBRATION_NOTES);
    } else {
      await calibrateToCandidate(page, false, null, null);
    }
    const bareReview = page.getByTestId(/^skill-review-RFN-/).first();
    await bareReview.click();
    if (real) {
      await expect(page.getByTestId("skill-simulated-evaluation")).toHaveCount(
        0,
      );
      await expect(
        page.getByText(`${TEST_SET.length} evaluation examples`),
      ).toBeVisible();
    } else {
      await expect(
        page.getByTestId("skill-simulated-evaluation"),
      ).toBeVisible();
      await expect(page.getByText("0 evaluation examples")).toBeVisible();
    }

    // ---- 12c. A viewer cannot apply -------------------------------------------------------
    await page.goto("/caliber/administration");
    const accounts = page.getByRole("region", { name: "Accounts" });
    await accounts.getByLabel("User ID").fill(viewerId);
    await accounts.getByLabel("Password", { exact: true }).fill(viewerPassword);
    await accounts.getByRole("button", { name: "Create account" }).click();
    await expect(accounts.getByText(`Created ${viewerId}.`)).toBeVisible();
    const access = page.getByRole("region", { name: "Project access" });
    await access
      .getByLabel("Project for access management")
      .selectOption({ label: `${workspaceName} (owner)` });
    await access.getByLabel("User ID").fill(viewerId);
    await access.locator("form select").selectOption("viewer");
    await access.getByRole("button", { name: "Add member" }).click();
    await expect(access.getByRole("cell", { name: viewerId })).toBeVisible();

    const viewerContext = await page
      .context()
      .browser()!
      .newContext({
        baseURL: new URL(page.url()).origin,
        viewport: { width: 1440, height: 900 },
      });
    try {
      const viewerPage = await viewerContext.newPage();
      await new CaliberUi(viewerPage).signIn(viewerId, viewerPassword);
      await viewerPage.goto("/caliber/skills");
      await viewerPage
        .getByLabel("Active workspace")
        .selectOption({ label: workspaceName });
      await openSkill(viewerPage, bareName);
      await stage(viewerPage, "Runs");
      // the viewer can read the skill and its calibration runs, but not apply a candidate
      const viewerReview = viewerPage.getByTestId(/^skill-review-RFN-/).first();
      await expect(viewerReview).toBeVisible();
      await viewerReview.click();
      await viewerPage
        .getByTestId(/^skill-apply-RFN-/)
        .first()
        .click();
      await expect(viewerPage.getByRole("alert")).toContainText(
        /missing required scope|forbidden|not allowed|403/i,
      );
      await expect(viewerPage.getByTestId("skill-apply-result")).toHaveCount(0);
      await viewerPage.getByTestId("skill-calibrate-btn").click();
      await expect(
        viewerPage
          .getByText(/missing required scope|forbidden|not allowed|403/i)
          .first(),
      ).toBeVisible();
    } finally {
      await viewerContext.close();
    }

    // ---- 13. Archive everything this run created -----------------------------------------
    for (const name of [skillName, copyName, bareName]) {
      await openSkill(page, name);
      await page.getByTestId("skill-open-detail").click();
      await page.getByTestId("skill-status-btn").click();
      await expect(page.getByText("Skill archived.")).toBeVisible();
    }
    await page.goto("/caliber/skills");
    await page.getByLabel("Search skills").fill(skillName);
    await expect(
      page
        .locator('[data-testid^="skill-card-"]')
        .filter({ has: page.getByText(skillName, { exact: true }) })
        .getByRole("button", { name: "Restore" }),
    ).toBeVisible();
  });
});
