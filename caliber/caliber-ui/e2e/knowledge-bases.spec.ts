import { expect, test, type Page } from "@playwright/test";

import {
  archiveKnowledgeBaseByName,
  createObjectStoreBucket,
  deploymentAgeEnabled,
  deleteObjectStoreBucketRecursive,
  openObjectStoreBucket,
  signIn,
  uniqueSlug,
  uploadObjectStoreFixtures,
} from "./helpers";

const KNOWLEDGE_FIXTURE = `# Caliber Incident Playbook

Alice leads Support for the Caliber deployment.
Bob owns Platform reliability and receives escalations from Alice.
Knowledge bases convert object-store documents into chunks, embeddings, and graph-aware retrieval.
During a production incident, Alice pages Bob and shares the latest runbook.
`;

const KNOWLEDGE_BUILD_TEST_TIMEOUT_MS = 360_000;
const KNOWLEDGE_BUILD_COMPLETION_TIMEOUT_MS = 300_000;

async function openNewKnowledgeBaseBuild(page: Page): Promise<void> {
  await page.goto("/caliber/knowledge-bases");
  await page.getByRole("button", { name: "New knowledge base" }).click();
  await expect(page.getByTestId("kb-workspace-header")).toBeVisible();
  // The Build stage hosts a "Create new" blueprint; selecting it reveals the form.
  await page.getByRole("button", { name: /^Create new\b/ }).click();
  await expect(page.getByLabel("Knowledge-base name")).toBeVisible();
}

function knowledgeBaseTabs(page: Page) {
  return page.locator('[role="tablist"]').first();
}

test.describe("Knowledge Bases", () => {
  test("exposes the build, explore, calibrate, and use surfaces", async ({
    page,
  }) => {
    await signIn(page);
    await page.goto("/caliber/knowledge-bases");

    await expect(page.getByRole("heading", { name: "Knowledge Bases" })).toBeVisible();
    await expect(page.getByRole("button", { name: "New knowledge base" })).toBeVisible();
    await expect(page.getByLabel("Search knowledge bases")).toBeVisible();

    // Open the per-KB Workspace (Library landing → Build · Explore · Calibrate · Use).
    await page.getByRole("button", { name: "New knowledge base" }).click();
    await expect(page.getByTestId("kb-workspace-header")).toBeVisible();
    const tabs = knowledgeBaseTabs(page);
    for (const tab of ["Build", "Explore", "Calibrate", "Use"]) {
      await expect(tabs.getByText(tab, { exact: true })).toBeVisible();
    }

    // The Build stage exposes the source picker + the create form.
    await expect(page.getByText("Select files or folders")).toBeVisible();
    await page.getByRole("button", { name: /^Create new\b/ }).click();
    await expect(page.getByLabel("Knowledge-base name")).toBeVisible();
    await expect(page.getByRole("button", { name: "Create knowledge base" })).toBeVisible();
  });

  test("builds a knowledge base from object store files and queries it with graph retrieval", async ({
    page,
  }) => {
    test.setTimeout(KNOWLEDGE_BUILD_TEST_TIMEOUT_MS);

    await signIn(page);

    const bucket = uniqueSlug("pw-kb");
    const knowledgeBaseName = uniqueSlug("pw-kb-docs");
    const sourceFile = "incident-playbook.md";
    try {
      await createObjectStoreBucket(page, bucket);
      await openObjectStoreBucket(page, bucket);
      await uploadObjectStoreFixtures(page, bucket, [
        {
          name: sourceFile,
          mimeType: "text/markdown",
          body: KNOWLEDGE_FIXTURE,
        },
      ]);

      await openNewKnowledgeBaseBuild(page);

      await page.getByLabel("Bucket").selectOption(bucket);
      await expect(page.getByText(sourceFile)).toBeVisible();
      // Sources are now added by checking the file in the bucket tree (the
      // separate "Add" button was removed).
      await page.getByRole("checkbox", { name: `Select file ${sourceFile}` }).check();

      await expect(page.getByText("1 selected")).toBeVisible();
      await page.getByLabel("Knowledge-base name").fill(knowledgeBaseName);
      await page
        .getByLabel("Description")
        .fill("Playwright validation corpus for chunking, embeddings, and retrieval.");
      // The embedding-model select lives inside the (collapsed-by-default)
      // Advanced configuration disclosure; expand it before choosing a model.
      await page.getByRole("button", { name: /^Advanced configuration/ }).click();
      await page
        .getByLabel("Embedding model")
        .selectOption("sentence-transformers/all-MiniLM-L6-v2");

      // The build form's sticky header overlaps the submit button's hit-test
      // point (a normal click lands on the header), and dispatchEvent on a
      // type=submit button is untrusted so it never triggers the form's
      // onSubmit. Submit the form directly with the button as submitter.
      await page.getByRole("button", { name: "Create knowledge base" }).evaluate((btn) => {
        const form = (btn as HTMLButtonElement).closest("form");
        if (form) (form as HTMLFormElement).requestSubmit(btn as HTMLButtonElement);
      });
      await expect(page.getByText("Pipeline executions")).toBeVisible({ timeout: 120_000 });
      const runRow = page.getByRole("button").filter({ hasText: /^KBR-/ }).first();
      await expect(runRow).toBeVisible({ timeout: 120_000 });

      await expect
        .poll(
          async () => {
            const text = ((await runRow.textContent()) ?? "").toLowerCase();
            if (text.includes("failed")) return "failed";
            if (text.includes("completed")) return "completed";
            return "pending";
          },
          {
            timeout: KNOWLEDGE_BUILD_COMPLETION_TIMEOUT_MS,
            intervals: [1_000, 2_000, 5_000],
          },
        )
        .toBe("completed");

      // Select the freshly built version in the workspace header switcher so the
      // per-version stages operate on it. The interactive retrieval playground
      // lives under the Explore stage's default "Query" view — the Use stage is
      // just "query via API" documentation, not an ask box.
      // Select the (only) built version by index — its label carries an
      // "(active)" suffix once the build promotes it, so match positionally.
      await page
        .getByTestId("kb-workspace-version-switcher")
        .selectOption({ index: 0 });
      await knowledgeBaseTabs(page).getByRole("button", { name: "Explore", exact: true }).click();
      await page.getByTestId("kb-explore-view-ask").click();
      await expect(
        page.getByPlaceholder("Ask a question about the selected documents…"),
      ).toBeVisible();
      // The simplified Query view's retrieval-mode segmented control is
      // single-select; pick the graph-aware mode so the answer carries graph
      // context. (Multi-mode compare lives in a separate disclosure.)
      await page.getByTestId("kb-explore-mode-graph_hybrid").click();
      await page
        .getByPlaceholder("Ask a question about the selected documents…")
        .fill("Who leads Support?");
      const queryResponse = page.waitForResponse(
        (response) =>
          response.url().includes("/knowledge/query")
          && response.request().method() === "POST"
          && response.status() === 200,
        { timeout: 90_000 },
      );
      // Submit the form directly with the scoped "Ask" button as submitter:
      // a name-based click would be ambiguous with the banner's "Ask Aria"
      // button, and the sticky header can intercept a positional click.
      await page
        .getByTestId("kb-explore-ask")
        .getByRole("button", { name: "Ask" })
        .evaluate((btn) => {
          const form = (btn as HTMLButtonElement).closest("form");
          if (form) (form as HTMLFormElement).requestSubmit(btn as HTMLButtonElement);
        });
      await queryResponse;

      await expect(page.getByText("Graph context")).toBeVisible({ timeout: 60_000 });
      await expect(page.getByText("Retrieved chunks").first()).toBeVisible();
      await expect(page.getByText(/Alice leads Support/).first()).toBeVisible();
    } finally {
      await archiveKnowledgeBaseByName(page, knowledgeBaseName);
      await deleteObjectStoreBucketRecursive(page, bucket);
    }
  });

  test("builds a knowledge base and runs Apache AGE retrieval from the graph view @age", async ({
    page,
  }) => {
    test.setTimeout(KNOWLEDGE_BUILD_TEST_TIMEOUT_MS);

    await signIn(page);

    const expectedAge = process.env["CALIBER_EXPECT_AGE"];
    const ageEnabled = await deploymentAgeEnabled(page);
    if (expectedAge === "1" && !ageEnabled) {
      throw new Error("CALIBER_EXPECT_AGE=1 was set, but the target deployment reports AGE as disabled.");
    }
    if (expectedAge === "0" || !ageEnabled) {
      test.skip(true, "AGE-backed e2e runs only when the target stack exposes Apache AGE.");
    }

    const bucket = uniqueSlug("pw-kb-age");
    const knowledgeBaseName = uniqueSlug("pw-kb-age-docs");
    const sourceFile = "incident-playbook-age.md";
    try {
      await createObjectStoreBucket(page, bucket);
      await openObjectStoreBucket(page, bucket);
      await uploadObjectStoreFixtures(page, bucket, [
        {
          name: sourceFile,
          mimeType: "text/markdown",
          body: KNOWLEDGE_FIXTURE,
        },
      ]);

      await openNewKnowledgeBaseBuild(page);

      await page.getByLabel("Bucket").selectOption(bucket);
      await expect(page.getByText(sourceFile)).toBeVisible();
      await page.getByRole("checkbox", { name: `Select file ${sourceFile}` }).check();

      await expect(page.getByText("1 selected")).toBeVisible();
      // Embedding + GraphRAG/AGE settings live in the collapsed-by-default
      // Advanced configuration disclosure; expand it before asserting/choosing them.
      await page.getByRole("button", { name: /^Advanced configuration/ }).click();
      await expect(page.getByText(/AGE → knowledge_graph/)).toBeVisible();
      await page.getByLabel("Knowledge-base name").fill(knowledgeBaseName);
      await page
        .getByLabel("Description")
        .fill("Playwright AGE validation corpus for graph-native retrieval.");
      await page
        .getByLabel("Embedding model")
        .selectOption("sentence-transformers/all-MiniLM-L6-v2");

      // The build form's sticky header overlaps the submit button's hit-test
      // point (a normal click lands on the header), and dispatchEvent on a
      // type=submit button is untrusted so it never triggers the form's
      // onSubmit. Submit the form directly with the button as submitter.
      await page.getByRole("button", { name: "Create knowledge base" }).evaluate((btn) => {
        const form = (btn as HTMLButtonElement).closest("form");
        if (form) (form as HTMLFormElement).requestSubmit(btn as HTMLButtonElement);
      });
      await expect(page.getByText("Pipeline executions")).toBeVisible({ timeout: 120_000 });
      const runRow = page.getByRole("button").filter({ hasText: /^KBR-/ }).first();
      await expect(runRow).toBeVisible({ timeout: 120_000 });

      await expect
        .poll(
          async () => {
            const text = ((await runRow.textContent()) ?? "").toLowerCase();
            if (text.includes("failed")) return "failed";
            if (text.includes("completed")) return "completed";
            return "pending";
          },
          {
            timeout: KNOWLEDGE_BUILD_COMPLETION_TIMEOUT_MS,
            intervals: [1_000, 2_000, 5_000],
          },
        )
        .toBe("completed");

      // Select the built version, then open the Explore stage's Graph subnav
      // (Explore now defaults to the "Query" view, so the graph inspector must
      // be selected explicitly).
      await page
        .getByTestId("kb-workspace-version-switcher")
        .selectOption({ index: 0 });
      await knowledgeBaseTabs(page).getByRole("button", { name: "Explore", exact: true }).click();
      await page.getByTestId("kb-explore-view-graph").click();
      await expect(page.getByText("Inspect the version-scoped knowledge graph")).toBeVisible();

      // The build stages AGE but does not auto-sync; sync the version into
      // Apache AGE so graph retrieval is served from AGE. Wait for the sync to
      // settle — the action relabels to "Resync to AGE" and the playground
      // shortcut appears once the version is AGE-ready.
      await page.getByRole("button", { name: "Sync to AGE" }).click();
      await expect(
        page.getByRole("button", { name: "Open AGE in Playground" }),
      ).toBeVisible({ timeout: 60_000 });
      await expect(page.getByText("Served from Apache AGE")).toBeVisible({ timeout: 60_000 });
      await page.getByRole("button", { name: "Open AGE in Playground" }).click();

      // "Open AGE in Playground" lands on the Explore → Query view with the
      // age_graph retrieval mode preselected.
      await expect(
        page.getByPlaceholder("Ask a question about the selected documents…"),
      ).toBeVisible();
      await page
        .getByPlaceholder("Ask a question about the selected documents…")
        .fill("Who owns Platform reliability?");
      const queryResponse = page.waitForResponse(
        (response) =>
          response.url().includes("/knowledge/query")
          && response.request().method() === "POST"
          && response.status() === 200,
        { timeout: 90_000 },
      );
      // Submit via the scoped form button (avoids the banner's "Ask Aria" and
      // any sticky-header click interception).
      await page
        .getByTestId("kb-explore-ask")
        .getByRole("button", { name: "Ask" })
        .evaluate((btn) => {
          const form = (btn as HTMLButtonElement).closest("form");
          if (form) (form as HTMLFormElement).requestSubmit(btn as HTMLButtonElement);
        });
      const response = await queryResponse;
      const payload = await response.json();

      expect(payload.data?.versions?.[0]?.retrieval_mode).toBe("age_graph");
      await expect(page.getByText("Apache AGE context")).toBeVisible({ timeout: 60_000 });
      await expect(page.getByText(/knowledge_graph/).first()).toBeVisible();
    } finally {
      await archiveKnowledgeBaseByName(page, knowledgeBaseName);
      await deleteObjectStoreBucketRecursive(page, bucket);
    }
  });
});
