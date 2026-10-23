import { expect, test, type Page } from "@playwright/test";

import { CaliberUi } from "../ui-only-driver";

/**
 * Cookbook 20, the UI path: the LLM Gateway page end to end -- endpoint discovery and its
 * degraded state, model pricing, usage with exact cost, a deterministic guardrail definition --
 * plus the secrets store on Administration.
 *
 * Follows docs-site/cookbooks/20-llm-gateway-cost-guardrails-lab/README.md ("Recipe: run it
 * through the UI"). Needs a stack started as that README describes (--real-llm, with
 * CALIBER_GATEWAY_URI and CALIBER_LLM_BASE_URL pointing at the lab's stand-in gateway and
 * CALIBER_SECRET_ENCRYPTION_KEY_SOURCE set) and the stand-in (assets/server/llm_gateway.py)
 * already running. The spec resets the stand-in and sets the right provider key itself, so it is
 * self-contained no matter what ran against the same stack before it.
 */
const GATEWAY_PORT = Number(process.env["LAB20_GATEWAY_PORT"] ?? 8791);
const GATEWAY_URL = `http://127.0.0.1:${GATEWAY_PORT}`;
const GATEWAY_KEY = process.env["LAB20_GATEWAY_KEY"] ?? "lab20-gateway-key";

const PROMPT_PRICE = 1.0;
const COMPLETION_PRICE = 2.0;
const TOKENS_PER_CALL = 2000;
const COST_PER_CALL = 3.0;

const MANIFEST = (name: string) => `schema_version: 1
workflow_id: ${name}
name: ${name}
description: Cookbook 20 UI run.
nodes:
  start:
    id: start
    type: start
    outputs:
      user_message:
        type: string
  reply_agent:
    id: reply_agent
    type: agent
    name: reply-agent
    model: lab-chat-mini
    instructions:
      type: inline
      text: Answer in one short sentence.
    inputs:
      input:
        type: string
    outputs:
      final_output:
        type: string
  final:
    id: final
    type: output
    inputs:
      response:
        type: string
edges:
  - id: e_start_agent
    from: start
    to: reply_agent
    map:
      user_message: input
  - id: e_agent_final
    from: reply_agent
    to: final
    map:
      final_output: response
`;

async function usageRow(
  page: Page,
  api: string,
  model: string,
): Promise<{ calls: number; tokens: number; cost_usd: number }> {
  const response = await page.request.get(`${api}/gateway/usage?since_ms=0`);
  const usage = (
    (await response.json()) as {
      data: {
        by_model: {
          model: string;
          calls: number;
          tokens: number;
          cost_usd: number;
        }[];
      };
    }
  ).data;
  return (
    usage.by_model.find((row) => row.model === model) ?? {
      calls: 0,
      tokens: 0,
      cost_usd: 0,
    }
  );
}

test.describe("Cookbook 20: metered LLM gateway (UI)", () => {
  test("discovers, degrades, prices, meters and guards", async ({ page }) => {
    test.setTimeout(600_000);
    const suffix = `_${Date.now().toString(36)}`;
    const ui = new CaliberUi(page);
    const api = "/ajax-api/2.0/mlflow/caliber";

    // ---- Setup: a fresh stand-in and a known-good provider key -----------------
    expect(
      (await page.request.post(`${GATEWAY_URL}/admin/reset`)).ok(),
    ).toBeTruthy();
    await ui.signIn();
    const patched = await page.request.patch(`${api}/settings/llm`, {
      data: { openai_api_key: GATEWAY_KEY },
    });
    expect(patched.ok()).toBeTruthy();
    // The metered model's rate: adopt the row if an earlier run created it (global uniqueness).
    const priced = await page.request.post(`${api}/llm-pricing`, {
      data: {
        provider: "lab",
        model_id: "lab-chat-mini",
        prompt_price: PROMPT_PRICE,
        completion_price: COMPLETION_PRICE,
      },
    });
    expect([201, 409]).toContain(priced.status());

    // ---- 1. Endpoints: configured, reachable, routed through -------------------
    await page.goto("/caliber/gateway");
    await expect(
      page.getByRole("heading", { name: "LLM Gateway" }),
    ).toBeVisible();
    const status = page.getByTestId("gateway-status");
    await expect(status).toContainText("Reachable");
    await expect(status).toContainText("Through gateway");
    await expect(page.getByTestId("gateway-endpoint-row")).toHaveCount(2);
    await expect(page.getByText("chat-lab")).toBeVisible();

    // ---- 2. The page degrades while the gateway is down, and recovers ----------
    await page.request.post(`${GATEWAY_URL}/admin/pause`);
    await page.reload();
    await expect(page.getByTestId("gateway-status")).toContainText(
      "Unreachable",
    );
    await page.request.post(`${GATEWAY_URL}/admin/resume`);
    await page.reload();
    await expect(page.getByTestId("gateway-status")).toContainText("Reachable");

    // ---- 3. Pricing: add a rate for a lab-unique model --------------------------
    await page.getByRole("button", { name: "Pricing" }).click();
    await expect(page.getByTestId("gateway-pricing")).toBeVisible();
    await page.getByRole("button", { name: "Add rate" }).click();
    const form = page.getByTestId("pricing-form");
    await form.locator("input").nth(0).fill("lab");
    await form.locator("input").nth(1).fill(`ui-model${suffix}`);
    await form.locator("input").nth(2).fill("4");
    await form.locator("input").nth(3).fill("8");
    await form.getByRole("button", { name: "Create" }).click();
    await expect(
      page.getByTestId("pricing-row").filter({ hasText: `ui-model${suffix}` }),
    ).toBeVisible();

    // ---- 4. A metered run, and Usage shows tokens and exact cost ----------------
    const before = await usageRow(page, api, "lab-chat-mini");
    const imported = await page.request.post(`${api}/workflows/import`, {
      data: {
        manifest_yaml: MANIFEST(`metered-ui${suffix}`),
        name: `metered-ui${suffix}`,
      },
    });
    expect(imported.ok()).toBeTruthy();
    const workflow = (
      (await imported.json()) as {
        data: { workflow: { workflow_id: string } };
      }
    ).data.workflow;
    const versions = await page.request.get(
      `${api}/workflows/${workflow.workflow_id}/versions`,
    );
    const versionId = (
      (await versions.json()) as { data: { version_id: string }[] }
    ).data[0].version_id;
    await page.request.post(`${api}/workflow-versions/${versionId}/publish`, {
      data: {},
    });
    const run = await page.request.post(
      `${api}/workflow-versions/${versionId}/run`,
      {
        data: { input: "Hello from the UI path" },
      },
    );
    expect(run.ok()).toBeTruthy();
    const outcome = (
      (await run.json()) as { data: { status: string; output: string } }
    ).data;
    expect(outcome.status).toBe("completed");
    expect(outcome.output).toContain("metered gateway");

    // Spans land asynchronously; poll the API, then read the tab.
    await expect
      .poll(async () => (await usageRow(page, api, "lab-chat-mini")).calls, {
        timeout: 45_000,
      })
      .toBe(before.calls + 1);
    const after = await usageRow(page, api, "lab-chat-mini");
    expect(after.tokens).toBe(before.tokens + TOKENS_PER_CALL);
    expect(after.cost_usd - before.cost_usd).toBeCloseTo(COST_PER_CALL, 6);

    await page.getByRole("button", { name: "Usage" }).click();
    await expect(page.getByTestId("gateway-usage")).toBeVisible();
    const byModel = page.getByTestId("gateway-usage-by-model");
    await expect(byModel).toContainText("lab-chat-mini");
    await expect(byModel).toContainText("$");

    // ---- 5. A deterministic guardrail definition, created and deleted -----------
    await page.getByRole("button", { name: "Guardrails" }).click();
    await expect(page.getByTestId("gateway-guardrails")).toBeVisible();
    await page.getByTestId("new-guardrail-toggle").click();
    const guardrailForm = page.getByTestId("create-guardrail-form");
    await guardrailForm.getByLabel("Guardrail type").selectOption("regex");
    await guardrailForm
      .getByLabel("Guardrail name")
      .fill(`no-internal-hosts${suffix}`);
    await guardrailForm.getByLabel("Pattern").fill("internal\\.corp");
    await page.getByTestId("create-guardrail-submit").click();
    const row = page
      .getByTestId("guardrail-row")
      .filter({ hasText: `no-internal-hosts${suffix}` });
    await expect(row).toBeVisible();
    page.once("dialog", (dialog) => void dialog.accept());
    await page
      .getByRole("button", { name: `Delete no-internal-hosts${suffix}` })
      .click();
    await expect(row).toHaveCount(0);

    // ---- 6. The secrets store on Administration ---------------------------------
    await page.goto("/caliber/administration");
    await page.getByPlaceholder("stripe-key").fill(`lab20-ui-key${suffix}`);
    await page.getByLabel("Value").fill("a-value-the-ui-will-never-see-again");
    await page.getByRole("button", { name: "Store / rotate" }).click();
    await expect(
      page.getByRole("cell", { name: `lab20-ui-key${suffix}`, exact: true }),
    ).toBeVisible();
    await expect(
      page.getByText("a-value-the-ui-will-never-see-again"),
    ).toHaveCount(0);
  });
});
