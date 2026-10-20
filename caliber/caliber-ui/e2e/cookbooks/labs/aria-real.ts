import { expect, type Page } from "@playwright/test";

import { stackIsSimulated } from "./lab-mode";

/**
 * Helpers the Aria labs (12 to 15) share for their real-provider tier.
 *
 * The labs run in one of two modes: on the default stack the language model and the evaluator are
 * simulated (Evaluations are refused with a 400); on a stack started with `--real-llm` they are
 * real. The specs tell them apart by the provider banner on screen, and make no API call at all;
 * everything is clicks.
 */

/**
 * Is the language model and the evaluator real on this stack? Read the way a person reads it: the
 * amber "Simulated providers" banner at the top of every page (see `lab-mode.ts`). Sign in first.
 */
export async function isRealProvider(page: Page): Promise<boolean> {
  return !(await stackIsSimulated(page));
}

/**
 * One Ask Aria chat turn: open the drawer (top bar, "Ask Aria"), optionally set how Aria's actions
 * are approved ("Legacy auto" is the `auto_all` mode, the one that lets the chat change things),
 * send the message, and wait for the reply. Returns the process chips the reply shows ("1 action",
 * "2 actions", ...), which say how many tools the model called.
 */
export async function askAria(
  page: Page,
  message: string,
  options: { approval?: string } = {},
): Promise<string> {
  await page.getByRole("button", { name: "Ask Aria" }).click();
  const panel = page.getByTestId("assistant-panel");
  await expect(panel).toBeVisible();
  // A fresh conversation each time, so a turn never inherits an earlier turn's settings.
  await panel.getByRole("button", { name: "New Chat" }).click();
  if (options.approval) {
    await panel.getByTestId("assistant-approval-selector").click();
    await page
      .getByRole("menu")
      .getByRole("menuitemradio", { name: new RegExp(`^${options.approval}`) })
      .click();
  }
  const input = panel.getByPlaceholder("Ask Aria for follow-up changes...");
  const send = panel.getByRole("button", { name: "Send message" });
  // The panel resets itself while a new chat starts; retry until the text has stuck and the
  // send button has noticed it.
  await expect(async () => {
    await input.fill(message);
    await expect(input).toHaveValue(message, { timeout: 2_000 });
    await expect(send).toBeEnabled({ timeout: 2_000 });
  }).toPass({ timeout: 30_000 });
  await send.click();
  // While the turn runs the input invites a queued follow-up; it is over when the usual prompt
  // is back. (A quick turn may skip the first state, so a miss there is fine.)
  await expect(input)
    .toHaveAttribute("placeholder", /Type to queue/, { timeout: 15_000 })
    .catch(() => undefined);
  await expect(input).toHaveAttribute("placeholder", /Ask Aria for follow-up/, {
    timeout: 240_000,
  });
  // The reply carries chips for how the turn resolved ("Thinking", "2 actions", ...).
  const steps = panel.getByTestId("assistant-process-steps").last();
  await expect(steps).toBeVisible({ timeout: 240_000 });
  return (await steps.textContent()) ?? "";
}

/** Close the Ask Aria drawer. */
export async function closeAria(page: Page): Promise<void> {
  await page
    .getByTestId("assistant-panel")
    .getByRole("button", { name: "Close", exact: true })
    .click();
}
