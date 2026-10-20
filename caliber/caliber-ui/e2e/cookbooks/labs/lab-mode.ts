import { expect, type Page } from "@playwright/test";

/**
 * Is the language model or the evaluator simulated on this stack?
 *
 * Read the way a person reads it: the amber provider banner at the top of every page of the web
 * app ("Simulated providers: eval, llm are running in fake mode ..."). The banner is rendered
 * from the same `/readiness` answer the app fetches on load, so the spec waits for that fetch and
 * then looks at the page; it makes no API call of its own. Sign in first.
 *
 * A stack with a real provider and nothing blocking shows no banner at all.
 */
export async function stackIsSimulated(page: Page): Promise<boolean> {
  const readiness = page.waitForResponse(
    (r) => r.url().includes("/caliber/readiness"),
    {
      timeout: 30_000,
    },
  );
  await page.goto("/caliber/");
  await readiness;
  await expect(page.getByLabel("CALIBER navigation")).toBeVisible();
  // The banner renders on the next tick after the fetch resolves.
  const banner = page.getByTestId("provider-banner");
  try {
    await banner.waitFor({ state: "visible", timeout: 5_000 });
  } catch {
    return false;
  }
  const text = (await banner.innerText()).toLowerCase();
  return text.includes("simulated providers") && /\b(llm|eval)\b/.test(text);
}
