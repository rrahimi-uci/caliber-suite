import { expect, test, type APIResponse, type Page } from "@playwright/test";

import { expectRunStatus, signIn, uniqueSlug } from "./helpers";

const API_BASE = "/ajax-api/2.0/mlflow/caliber";

async function parseEnvelope<T>(
  responsePromise: Promise<APIResponse>,
  context: string,
): Promise<T> {
  const response = await responsePromise;
  if (!response.ok()) {
    throw new Error(`${context} failed (${response.status()}): ${await response.text()}`);
  }
  const payload = (await response.json()) as { data: T };
  return payload.data;
}

function waitEventPayloadManifest(workflowId: string, name: string): Record<string, unknown> {
  return {
    schema_version: 1,
    workflow_id: workflowId,
    name,
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
      wait_gate: {
        id: "wait_gate",
        type: "wait_for_event",
        event_name: "ticket.approved",
        correlation_key: "ticket_id",
        inputs: { input: { type: "string" } },
        outputs: {
          output: { type: "string" },
          event_payload: { type: "structured" },
          event_name: { type: "string" },
        },
      },
      render_event: {
        id: "render_event",
        type: "python_code",
        code:
          'payload = inputs.get("payload") or {}\n'
          + 'event_name = inputs.get("event_name") or ""\n'
          + 'return {"text": f"{event_name}::{payload.get(\'ticket_id\')}::{payload.get(\'approved\')}", "result": {"payload": payload}}',
        inputs: {
          payload: { type: "structured" },
          event_name: { type: "string" },
        },
        outputs: {
          text: { type: "string" },
          result: { type: "structured" },
          metadata: { type: "structured" },
        },
      },
      final: {
        id: "final",
        type: "output",
        inputs: { response: { type: "string" } },
      },
    },
    edges: [
      { id: "e1", from: "start", to: "wait_gate", map: { msg: "input" } },
      {
        id: "e2",
        from: "wait_gate",
        to: "render_event",
        map: { event_payload: "payload" },
      },
      {
        id: "e3",
        from: "wait_gate",
        to: "render_event",
        map: { event_name: "event_name" },
      },
      {
        id: "e4",
        from: "render_event",
        to: "final",
        map: { text: "response" },
      },
    ],
    tools: {},
  };
}

function waitUntilManifest(workflowId: string, name: string): Record<string, unknown> {
  return {
    schema_version: 1,
    workflow_id: workflowId,
    name,
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
      wait_gate: {
        id: "wait_gate",
        type: "wait_until",
        wait_until: "2099-01-01T00:00:00Z",
        timezone: "UTC",
        inputs: { input: { type: "string" } },
        outputs: { output: { type: "string" } },
      },
      final: {
        id: "final",
        type: "output",
        inputs: { response: { type: "string" } },
      },
    },
    edges: [
      { id: "e1", from: "start", to: "wait_gate", map: { msg: "input" } },
      { id: "e2", from: "wait_gate", to: "final", map: { output: "response" } },
    ],
    tools: {},
  };
}

async function createWorkflowFromTemplate(
  page: Page,
  name: string,
  templateTestId: string,
): Promise<void> {
  await page.goto("/caliber/workflows");
  await expect(page.getByRole("heading", { name: "Workflows" })).toBeVisible();

  await page.getByTestId("new-workflow").click();
  await page.getByTestId("new-workflow-name").fill(name);
  await page.getByTestId(templateTestId).click();

  await expect(page).toHaveURL(/\/caliber\/workflows\/[^/]+\/editor\/[^/?]+$/);
  await expect(page.getByTestId("workflow-editor")).toBeVisible();
}

async function createWorkflowFromManifest(
  page: Page,
  name: string,
  manifestBuilder: (workflowId: string, workflowName: string) => Record<string, unknown>,
): Promise<{ workflowId: string; versionId: string }> {
  const workflow = await parseEnvelope<{ workflow_id: string }>(
    page.request.post(`${API_BASE}/workflows`, {
      data: { name, owner: "@playwright" },
    }),
    "create workflow",
  );
  const workflowId = workflow.workflow_id;
  const version = await parseEnvelope<{ version_id: string }>(
    page.request.post(`${API_BASE}/workflows/${encodeURIComponent(workflowId)}/versions`, {
      data: { manifest: manifestBuilder(workflowId, name) },
    }),
    "create workflow version",
  );
  const versionId = version.version_id;
  await page.goto(`/caliber/workflows/${workflowId}/editor/${versionId}`);
  await expect(page.getByTestId("workflow-editor")).toBeVisible();
  return { workflowId, versionId };
}

test.describe("Workflow Studio", () => {
  test("creates a workflow from a template, validates it, previews it, and runs it", async ({
    page,
  }) => {
    test.setTimeout(180_000);

    await signIn(page);

    const workflowName = uniqueSlug("pw-workflow");

    await createWorkflowFromTemplate(page, workflowName, "template-single_agent");
    await expect(page.getByTestId("wf-canvas")).toBeVisible();
    await expect(page.getByTestId("outline-start")).toBeVisible();
    await expect(page.getByTestId("outline-agent")).toBeVisible();
    await expect(page.getByTestId("outline-final")).toBeVisible();

    await page.getByTestId("editor-validate").click();
    await expect(page.getByTestId("editor-message")).toContainText("Valid.");
    await expect(page.getByTestId("wf-problems")).toContainText("No problems");

    // The code/canvas view is now a segmented control (Visual · Code · Plan).
    await page.getByTestId("editor-view-code").click();
    await expect(page.getByTestId("code-overlay")).toBeVisible();
    await page.getByTestId("editor-view-visual").click();
    await expect(page.getByTestId("code-overlay")).toHaveCount(0);

    await page.getByTestId("editor-preview").click();
    await expect(page.getByTestId("preview-panel")).toBeVisible();
    await page
      .getByTestId("preview-input")
      .fill("Summarize the refund guidance for order A-100.");
    const previewResponse = page.waitForResponse(
      (response) =>
        response.url().includes("/preview-run")
        && response.request().method() === "POST"
        && response.status() === 200,
      { timeout: 90_000 },
    );
    await page.getByTestId("preview-run").click();
    await previewResponse;

    await expect(page.getByTestId("preview-result")).toBeVisible({ timeout: 60_000 });
    await expect(page.getByTestId("preview-result")).toContainText("completed");
    await expect(page.getByTestId("preview-step-agent")).toBeVisible();
    await page.getByTestId("preview-close").click();
    await expect(page.getByTestId("preview-panel")).toHaveCount(0);

    await page.getByTestId("editor-run-monitor").click();
    await expect(page.getByTestId("run-monitor-panel")).toBeVisible();
    await page
      .getByTestId("run-input")
      .fill("Summarize the refund guidance for order A-100.");
    const runResponse = page.waitForResponse(
      (response) =>
        response.url().includes("/workflow-runs")
        && response.request().method() === "POST"
        && response.status() === 202,
      { timeout: 90_000 },
    );
    await page.getByTestId("run-execute").click();
    await runResponse;

    await expect(page.getByTestId("run-status-badge")).toBeVisible();
    await expectRunStatus(page, "completed");

    await expect(page.getByTestId("run-active-summary")).toBeVisible();
    await expect(page.getByTestId("run-history-list")).toContainText("WR-");
    await expect(page.getByTestId("run-trace-replay-section")).toContainText("Trace Replay");
    await expect(page.getByTestId("run-debugger-section")).toContainText("Execution Debugger");
    await expect(page.getByTestId("run-files-section")).toContainText("Files & Artifact Lineage");
  });

  test("runs the human approval template through approve and resume", async ({
    page,
  }) => {
    test.setTimeout(180_000);

    await signIn(page);

    const workflowName = uniqueSlug("pw-hitl");
    await createWorkflowFromTemplate(page, workflowName, "template-hitl_review");

    await page.getByTestId("editor-run-monitor").click();
    await expect(page.getByTestId("run-monitor-panel")).toBeVisible();
    await page
      .getByTestId("run-input")
      .fill("Approve a refund update for customer@example.com and share the final response.");

    const runResponse = page.waitForResponse(
      (response) =>
        response.url().includes("/workflow-runs")
        && response.request().method() === "POST"
        && response.status() === 202,
      { timeout: 90_000 },
    );
    await page.getByTestId("run-execute").click();
    await runResponse;

    await expectRunStatus(page, "waiting_approval");

    await expect(page.getByTestId("run-approval-actions")).toBeVisible();
    const approveResponse = page.waitForResponse(
      (response) =>
        response.url().includes("/approval/approve")
        && response.request().method() === "POST"
        && response.status() === 200,
      { timeout: 90_000 },
    );
    await page.getByTestId("run-approve").click();
    await approveResponse;

    await expect(page.getByTestId("run-resume")).toBeEnabled({ timeout: 30_000 });
    const resumeResponse = page.waitForResponse(
      (response) =>
        response.url().includes("/resume")
        && response.request().method() === "POST"
        && response.ok(),
      { timeout: 90_000 },
    );
    // The run-monitor's sticky header overlaps the run-control buttons' hit-test
    // point, so a real (or forced) click lands on the header. Dispatch the click
    // straight to the button instead (same pattern as build-advanced-flows).
    await page.getByTestId("run-resume").dispatchEvent("click");
    await resumeResponse;

    await expectRunStatus(page, "completed");

    await expect(page.getByTestId("run-history-list")).toContainText("WR-");
    await expect(page.getByTestId("run-lineage-section")).toContainText("Retry Lineage");
    await expect(page.getByTestId("run-debugger-section")).toContainText("Execution Debugger");
  });

  test("resumes a wait-for-event workflow from the run monitor using event correlation", async ({
    page,
  }) => {
    test.setTimeout(180_000);

    await signIn(page);

    const workflowName = uniqueSlug("pw-wait-event");
    await createWorkflowFromManifest(page, workflowName, waitEventPayloadManifest);

    await page.getByTestId("editor-run-monitor").click();
    await expect(page.getByTestId("run-monitor-panel")).toBeVisible();
    await page
      .getByTestId("run-input")
      .fill('{"ticket_id":"T-42","approved":false}');

    const runResponse = page.waitForResponse(
      (response) =>
        response.url().includes("/workflow-runs")
        && response.request().method() === "POST"
        && response.status() === 202,
      { timeout: 90_000 },
    );
    await page.getByTestId("run-execute").click();
    await runResponse;

    await expectRunStatus(page, "waiting_event");

    await expect(page.getByTestId("run-waiting-event-config")).toBeVisible();
    await expect(page.getByTestId("run-resume-event-name")).toHaveValue("ticket.approved");
    await page
      .getByTestId("run-resume-event-payload")
      .fill('{"ticket_id":"T-42","approved":true}');

    const resumeByEventResponse = page.waitForResponse(
      (response) =>
        response.url().includes("/workflow-runs/resume-by-event")
        && response.request().method() === "POST"
        && response.ok(),
      { timeout: 90_000 },
    );
    await page.getByTestId("run-resume-by-event").click();
    await resumeByEventResponse;

    await expectRunStatus(page, "completed");

    await expect(page.getByTestId("run-output")).toContainText("ticket.approved::T-42::True");
    await expect(page.getByTestId("run-trace-replay-section")).toContainText("Trace Replay");
    await expect(page.getByTestId("run-debugger-section")).toContainText("Execution Debugger");
  });

  test("resumes a wait-until workflow from the run monitor as a manual override", async ({
    page,
  }) => {
    test.setTimeout(180_000);

    await signIn(page);

    const workflowName = uniqueSlug("pw-wait-until");
    await createWorkflowFromManifest(page, workflowName, waitUntilManifest);

    await page.getByTestId("editor-run-monitor").click();
    await expect(page.getByTestId("run-monitor-panel")).toBeVisible();
    await page.getByTestId("run-input").fill("remind me later");

    const runResponse = page.waitForResponse(
      (response) =>
        response.url().includes("/workflow-runs")
        && response.request().method() === "POST"
        && response.status() === 202,
      { timeout: 90_000 },
    );
    await page.getByTestId("run-execute").click();
    await runResponse;

    await expectRunStatus(page, "waiting_event");

    await expect(page.getByTestId("run-wait-until-config")).toBeVisible();
    const resumeResponse = page.waitForResponse(
      (response) =>
        response.url().includes("/resume")
        && response.request().method() === "POST"
        && response.ok(),
      { timeout: 90_000 },
    );
    // The run-monitor's sticky header overlaps the run-control buttons' hit-test
    // point, so a real (or forced) click lands on the header. Dispatch the click
    // straight to the button instead (same pattern as build-advanced-flows).
    await page.getByTestId("run-resume").dispatchEvent("click");
    await resumeResponse;

    await expectRunStatus(page, "completed");

    await expect(page.getByTestId("run-output")).toContainText("remind me later");
    await expect(page.getByTestId("run-lineage-section")).toContainText("Retry Lineage");
  });
});
