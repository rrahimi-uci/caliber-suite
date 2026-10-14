import { expect, test } from "@playwright/test";

import { signIn } from "./helpers";

/**
 * The tool journey, driven the way a user would: register a tool, run it, save fixtures and
 * calibrate, pin a baseline, and see it used by a workflow (alpha gap P1-21).
 *
 * It runs on the harness defaults. The registered callable is `lookup_order` from the demo
 * tools module, which the tool sandbox runs in a child process like any registered module.
 * Two steps are done by API rather than through the page, because the page's editor for them
 * is a larger form than the journey is about: the fixture cases (`PUT /tools/{id}/test-cases`,
 * the endpoint the Fixtures editor saves to) and the workflow that references the tool
 * (`POST /workflows` and a version, as the workflow editor does).
 *
 * What it proves, in order:
 *  1. The registration wizard creates a tool, and the detail page runs it.
 *  2. Saved fixtures are scored by Run calibration, and the result says how many passed.
 *  3. A test run can be pinned as the baseline.
 *  4. A workflow that references the tool makes it show under "Where used".
 *  5. The Tools list labels CALIBER's own tools "Built-in" and says why a memory tool cannot
 *     be used yet, while the tool registered here carries neither label.
 */
const API = "/ajax-api/2.0/mlflow/caliber";

test.describe("Tool journey (alpha)", () => {
  test("registers a tool, calibrates it, pins a baseline and shows it in a workflow", async ({
    page,
  }) => {
    test.setTimeout(240_000);
    const suffix = Date.now().toString(36);
    const toolName = `journey_tool_${suffix}`;

    await signIn(page);

    // ---- 1. Register through the wizard, then run it -------------------------
    await page.goto("/caliber/tools");
    await page.getByRole("button", { name: "Register Tool" }).click();
    await expect(page.getByTestId("tool-wizard")).toBeVisible();
    await page.getByTestId("wiz-name").fill(toolName);
    await page
      .getByTestId("wiz-description")
      .fill("Looks an order up by id, for the tool journey.");
    await page.getByTestId("wiz-owner").fill("@journey");
    await page.getByTestId("wizard-next").click();

    await page.getByTestId("wiz-module").fill("caliber.workflows.demo_tools");
    await page.getByTestId("wiz-callable").fill("lookup_order");
    await page.getByTestId("wizard-next").click();

    await page.getByTestId("input-schema-add-prop").click();
    const property = page
      .locator('[data-testid^="input-schema-prop-"]')
      .first();
    await property
      .locator('[data-testid^="input-schema-prop-name-"]')
      .fill("order_id");
    await property.locator('[data-testid^="input-schema-prop-req-"]').check();
    await page.getByTestId("wizard-next").click();
    await page.getByTestId("wizard-next").click();
    await page.getByTestId("wiz-allow-preview").check();
    await page.getByTestId("wizard-submit").click();

    await expect(page).toHaveURL(/\/caliber\/tools\/[^/]+$/);
    const toolId = page.url().split("/").pop() as string;
    await expect(page.getByTestId("tool-detail")).toContainText(toolName);

    await page.getByTestId("tool-run-input").fill('{"order_id":"A-100"}');
    await page.getByTestId("tool-run").click();
    await expect(page.getByTestId("tool-run-result")).toContainText(
      '"status": "delivered"',
    );

    // ---- 2. Fixtures, then Run calibration in the workspace ------------------
    const saved = await page.request.put(`${API}/tools/${toolId}/test-cases`, {
      data: {
        test_cases: [
          {
            name: "delivered",
            input: { order_id: "A-100" },
            assertion: { type: "output_contains", value: "delivered" },
          },
          { name: "no_error", input: { order_id: "B-200" } },
        ],
      },
    });
    expect(saved.ok(), await saved.text()).toBeTruthy();

    await page.goto("/caliber/tools");
    await page.getByTestId(`tool-open-${toolName}`).click();
    await page.getByRole("button", { name: "Fixtures", exact: true }).click();
    await page.getByTestId("tool-fixtures-calibrate-btn").click();
    await expect(
      page.getByTestId("tool-fixtures-calibration-result"),
    ).toContainText("Pass rate 100% — 2/2 passed", { timeout: 60_000 });

    // ---- 3. Pin a baseline ---------------------------------------------------
    const run = await page.request.post(`${API}/tools/test-runs`, {
      data: {
        tool_id: toolId,
        kind: "suite",
        tool_version: "1.0",
        results: [
          {
            name: "delivered",
            input: { order_id: "A-100" },
            output: { status: "delivered" },
            verdict: "pass",
            score: 1,
            duration_ms: 3,
            reasoning: "journey run",
          },
        ],
      },
    });
    expect(run.status(), await run.text()).toBe(201);
    await page.getByRole("button", { name: "Test Runs", exact: true }).click();
    await page.getByRole("button", { name: "Set as baseline" }).click();
    await expect(page.getByTestId("tool-run-baseline-marker")).toBeVisible();

    // ---- 4. A workflow that uses it, shown under "Where used" ----------------
    const workflow = await page.request.post(`${API}/workflows`, {
      data: { name: `journey-tool-workflow-${suffix}`, owner: "admin" },
    });
    expect(workflow.ok(), await workflow.text()).toBeTruthy();
    const workflowId = (await workflow.json()).data.workflow_id as string;
    const version = await page.request.post(
      `${API}/workflows/${workflowId}/versions`,
      {
        data: { manifest: manifestUsing(workflowId, toolName) },
      },
    );
    expect(version.status(), await version.text()).toBe(201);

    await page.goto("/caliber/tools");
    await page.getByTestId(`tool-open-${toolName}`).click();
    await page.getByRole("button", { name: "Publish", exact: true }).click();
    await expect(page.getByTestId("tool-publish-usage")).toContainText(
      `${workflowId} v1 (draft)`,
    );

    // ---- 5. The list says which tools CALIBER seeded, and which cannot work yet
    await page.getByRole("button", { name: "Back to tools" }).click();
    const mine = page.getByTestId(`tool-row-${toolName}`);
    await expect(mine).toBeVisible();
    await expect(mine.getByTestId("tool-builtin-badge")).toHaveCount(0);
    const memory = page.getByTestId("tool-row-memory_search");
    await expect(memory.getByTestId("tool-builtin-badge")).toBeVisible();
    await expect(memory.getByTestId("tool-unavailable-badge")).toBeVisible();
    await expect(memory.getByTestId("tool-unavailable-reason")).toContainText(
      "CALIBER_MEMORY_ENABLED",
    );
    await expect(
      page.getByTestId("tool-row-grep_files").getByTestId("tool-builtin-badge"),
    ).toBeVisible();
  });
});

/** The smallest workflow that calls one registered tool from an agent node. */
function manifestUsing(
  workflowId: string,
  toolName: string,
): Record<string, unknown> {
  return {
    schema_version: 1,
    workflow_id: workflowId,
    name: "Tool journey workflow",
    runtime: {
      sdk: "openai-agents-python",
      sdk_version_policy: "runtime-pinned",
      compiler_version: "caliber-workflow-compiler-v1",
      default_model_ref: "CALIBER_WORKFLOW_DEFAULT_MODEL",
    },
    nodes: {
      start: {
        id: "start",
        type: "start",
        outputs: { msg: { type: "string" } },
      },
      agent: {
        id: "agent",
        type: "agent",
        name: "order-agent",
        model: "inherit",
        instructions: { type: "inline", text: "Look the order up." },
        tools: [toolName],
        inputs: { input: { type: "string" } },
        outputs: { final_output: { type: "string" } },
      },
      final: {
        id: "final",
        type: "output",
        inputs: { response: { type: "string" } },
      },
    },
    edges: [
      { id: "e1", from: "start", to: "agent", map: { msg: "input" } },
      {
        id: "e2",
        from: "agent",
        to: "final",
        map: { final_output: "response" },
      },
    ],
    tools: {
      [toolName]: {
        registry_ref: `tool.${toolName}.v1`,
        version_constraint: ">=1.0,<2.0",
      },
    },
  };
}
