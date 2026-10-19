import {
  expect,
  test,
  type Browser,
  type BrowserContext,
  type Page,
} from "@playwright/test";

import { CaliberUi, uiOnlySlug } from "../ui-only-driver";

/**
 * Lab 18, "Run it through the UI": a team lead sets up a workspace, gives people roles, and reads
 * back who did what. The ten numbered steps in the lab README are the ten `test.step`s below.
 *
 * Each person signs in through the real login page in their own browser context, so the app is
 * seen the way that person sees it. Nothing here calls the API directly.
 *
 * The server needs the roster's platform scopes (the README, "Prerequisites and setup"):
 *   CALIBER_OPERATOR_USERS=admin,maya,omar,priya,sam,quinn,rosa,victor,nora
 *   CALIBER_APPROVER_USERS=admin,maya,omar,quinn
 * and `CALIBER_FEATURE_COOKBOOKS=true`. Re-running is safe: accounts that already exist are left
 * alone (and re-enabled if a cleanup disabled them), and every run makes a uniquely named
 * workspace and prompt.
 */

const PASSWORD = "lab18-demo-passphrase";

interface Person {
  user: string;
  role: "owner" | "editor" | "reviewer" | "viewer" | null;
}

/** The roster, in the order of `assets/dataset/team-roster.csv`. */
const ROSTER: Person[] = [
  { user: "maya", role: "owner" },
  { user: "omar", role: "owner" },
  { user: "priya", role: "editor" },
  { user: "sam", role: "editor" },
  { user: "quinn", role: "reviewer" },
  { user: "rosa", role: "reviewer" },
  { user: "victor", role: "viewer" },
  { user: "wen", role: "viewer" },
  { user: "nora", role: null },
];

const ROLE_LABEL = {
  owner: "Owner (Admin)",
  editor: "Editor",
  reviewer: "Reviewer",
  viewer: "Viewer",
} as const;

/** Set LAB18_SCREENSHOTS=<dir> to keep one picture per step (used to write the README). */
async function shot(page: Page, name: string): Promise<void> {
  const dir = process.env["LAB18_SCREENSHOTS"];
  if (dir)
    await page.screenshot({ path: `${dir}/${name}.png`, fullPage: false });
}

async function signedInAs(
  browser: Browser,
  baseURL: string,
  user: string,
): Promise<{ context: BrowserContext; page: Page }> {
  const context = await browser.newContext({
    baseURL,
    viewport: { width: 1440, height: 900 },
  });
  const page = await context.newPage();
  await new CaliberUi(page).signIn(user, PASSWORD);
  return { context, page };
}

/** Pick the workspace in the top bar's "Active workspace" selector, once the list has loaded. */
async function selectWorkspace(page: Page, name: string): Promise<void> {
  const selector = page.getByLabel("Active workspace");
  await expect(selector.locator("option", { hasText: name })).toHaveCount(1, {
    timeout: 30_000,
  });
  await selector.selectOption({ label: name });
  await expect(selector.locator("option:checked")).toHaveText(name);
}

/** The prompt builder: create a prompt from pasted text, as the Prompts page offers it. */
async function createPrompt(page: Page, name: string): Promise<void> {
  await page.goto("/caliber/prompts");
  await page.getByRole("button", { name: "New prompt" }).click();
  await page.getByRole("button", { name: /Write \/ paste/ }).click();
  await page
    .getByLabel("Prompt text")
    .fill("Reply to the customer politely.\n\nQuestion: {question}");
  await page.getByLabel("Prompt name").fill(name);
  await page.getByRole("button", { name: "Create and Open" }).click();
}

// The config sets no action timeout; fail a stuck click with a message instead of at the test's end.
test.use({ actionTimeout: 30_000, navigationTimeout: 60_000 });

test.describe("Lab 18: projects, roles, tokens and audit", () => {
  test("a team lead sets up a workspace, roles and a bound token, and reads the audit trail", async ({
    page: adminPage,
    browser,
  }, testInfo) => {
    test.setTimeout(420_000);
    const baseURL = String(testInfo.project.use.baseURL);
    const suffix = uiOnlySlug("lab18").replace("lab18-", "");
    const workspace = `lab18-ui-${suffix}`;
    const promptName = `lab18-ui-${suffix}-support-reply`;
    const tokenName = `support-ci-${suffix}`;
    const contexts: BrowserContext[] = [];
    let projectId = "";

    try {
      // ---- 1. The admin creates the team's accounts (Administration) -----------------------
      await test.step("1. Administration > Accounts: create the nine accounts", async () => {
        await new CaliberUi(adminPage).signIn("admin", "admin");
        await adminPage.goto("/caliber/administration");
        await expect(
          adminPage.getByRole("heading", { name: "Administration" }),
        ).toBeVisible();
        const accounts = adminPage.locator("#accounts-heading").locator("..");
        // The account table loads after the heading: wait for it (`admin` is always listed), or a
        // slow load looks like "no accounts yet" and an account the API script already created is
        // created a second time ("account 'maya' already exists").
        await expect(
          accounts.getByRole("cell", { name: "admin", exact: true }),
        ).toBeVisible({ timeout: 30_000 });
        for (const { user } of ROSTER) {
          const row = accounts.getByRole("row").filter({
            has: adminPage.getByRole("cell", { name: user, exact: true }),
          });
          if ((await row.count()) > 0) {
            // Left disabled by an earlier cleanup: turn it back on.
            if ((await row.getByText("Disabled").count()) > 0) {
              await row.getByRole("button", { name: "Enable" }).click();
              await expect(row.getByText("Active")).toBeVisible();
            }
            continue;
          }
          await accounts.getByLabel("User ID").fill(user);
          await accounts.getByLabel("Password", { exact: true }).fill(PASSWORD);
          await accounts
            .getByRole("button", { name: "Create account" })
            .click();
          await expect(adminPage.getByText(`Created ${user}.`)).toBeVisible();
        }
        for (const { user } of ROSTER) {
          await expect(
            accounts.getByRole("cell", { name: user, exact: true }),
          ).toBeVisible();
        }
        await shot(adminPage, "01-accounts");
      });

      // ---- 2. maya creates the workspace and is its primary owner --------------------------
      const maya = await signedInAs(browser, baseURL, "maya");
      contexts.push(maya.context);
      await test.step("2. Top bar > Create workspace", async () => {
        await maya.page
          .getByRole("button", { name: "Create workspace" })
          .click();
        await maya.page.getByLabel("Workspace name").fill(workspace);
        await maya.page
          .getByRole("button", { name: "Create and select" })
          .click();
        const selector = maya.page.getByLabel("Active workspace");
        await expect(selector.locator("option:checked")).toHaveText(workspace);
        projectId = await selector.inputValue();
        expect(projectId).toMatch(/^PRJ-/);
        await shot(maya.page, "02-workspace");
      });

      // ---- 3. maya adds the members with their roles ---------------------------------------
      await test.step("3. Administration > Project access: add the seven members", async () => {
        await maya.page.goto("/caliber/administration");
        const section = maya.page
          .locator("#project-access-heading")
          .locator("..");
        await expect(
          section.getByLabel("Project for access management"),
        ).toBeVisible();
        await section
          .getByLabel("Project for access management")
          .selectOption(projectId);
        for (const { user, role } of ROSTER) {
          if (role === null || user === "maya") continue;
          // The add-member form is the only form in this section: a User ID box, a Role select.
          const form = section.locator("form");
          await form.getByRole("textbox").fill(user);
          await form
            .getByRole("combobox")
            .selectOption({ label: ROLE_LABEL[role] });
          await form.getByRole("button", { name: "Add member" }).click();
          await expect(
            section.getByText(`Added ${user} to ${workspace}.`),
          ).toBeVisible();
        }
        // The table now is the roster: maya is the primary owner, everyone else has the role
        // the data file gave them, and the outsider is not listed.
        await expect(section.getByText("(primary owner)")).toBeVisible();
        for (const { user, role } of ROSTER) {
          if (role === null || user === "maya") continue;
          await expect(section.getByLabel(`Role for ${user}`)).toHaveValue(
            role,
          );
        }
        await expect(section.getByLabel("Role for nora")).toHaveCount(0);
        // Offboarding is one click: the primary owner's row has no Remove button.
        await expect(
          section
            .getByRole("row")
            .filter({ hasText: "(primary owner)" })
            .getByRole("button", { name: "Remove" }),
        ).toHaveCount(0);
        await shot(maya.page, "03-members");
      });

      // ---- 4. A viewer is refused; an editor can write; both see their own limits ---------
      const victor = await signedInAs(browser, baseURL, "victor");
      contexts.push(victor.context);
      await test.step("4a. victor (viewer): a prompt is refused, members cannot be managed", async () => {
        await selectWorkspace(victor.page, workspace);
        await createPrompt(victor.page, `${promptName}-denied`);
        await expect(
          victor.page.getByText(
            /project role 'viewer' cannot perform resource\.write\.runtime/,
          ),
        ).toBeVisible({ timeout: 30_000 });
        await victor.page.goto("/caliber/administration");
        const section = victor.page
          .locator("#project-access-heading")
          .locator("..");
        await section
          .getByLabel("Project for access management")
          .selectOption(projectId);
        await expect(
          section.getByText("Only a project Admin can manage members."),
        ).toBeVisible();
        await expect(
          section.getByRole("button", { name: "Add member" }),
        ).toHaveCount(0);
        await shot(victor.page, "04a-viewer-members");
      });

      const priya = await signedInAs(browser, baseURL, "priya");
      contexts.push(priya.context);
      await test.step("4b. priya (editor): the prompt is created and live as v1", async () => {
        await selectWorkspace(priya.page, workspace);
        await createPrompt(priya.page, promptName);
        await expect(priya.page.getByTestId("version-panel")).toBeVisible({
          timeout: 60_000,
        });
        await expect(
          priya.page
            .getByTestId("version-row-1")
            .getByTestId("version-live-badge"),
        ).toBeVisible();
        await shot(priya.page, "04b-editor-prompt");
      });

      // ---- 5. A non-member does not see the workspace --------------------------------------
      const nora = await signedInAs(browser, baseURL, "nora");
      contexts.push(nora.context);
      await test.step("5. nora (not a member): the workspace is not in her list", async () => {
        // Reload and wait for the workspace list the top bar asks for, so an empty answer is
        // an answer and not a list that has not arrived yet.
        const listed = nora.page.waitForResponse(
          (response) =>
            response.request().method() === "GET" &&
            /\/caliber\/projects(\?.*)?$/.test(response.url()),
        );
        await nora.page.reload();
        await listed;
        const selector = nora.page.getByLabel("Active workspace");
        await expect(selector).toBeVisible();
        await expect(
          selector.locator("option", { hasText: workspace }),
        ).toHaveCount(0);
        await expect(
          selector.locator("option", { hasText: "All workspaces" }),
        ).toHaveCount(1);
        await shot(nora.page, "05-non-member");
      });

      // ---- 6. priya issues a token bound to the workspace ----------------------------------
      let secret = "";
      await test.step("6. Settings > API tokens: a token bound to the workspace", async () => {
        await priya.page.goto("/caliber/settings");
        await priya.page.getByRole("button", { name: "API tokens" }).click();
        await priya.page.getByLabel("Token name").fill(tokenName);
        await priya.page
          .getByLabel("Token expiry")
          .selectOption({ label: "30 days" });
        await expect(
          priya.page
            .getByLabel("Token workspace")
            .locator("option", { hasText: workspace }),
        ).toHaveCount(1);
        await priya.page.getByLabel("Token workspace").selectOption(projectId);
        await priya.page
          .getByRole("checkbox", { name: "caliber.operator" })
          .check();
        await priya.page.getByRole("button", { name: "Create token" }).click();

        const banner = priya.page.getByTestId("api-token-secret");
        await expect(banner).toContainText("It will not be shown again");
        secret = (
          await priya.page.getByTestId("api-token-secret-value").innerText()
        ).trim();
        expect(secret).toMatch(/^calpat_/);
        await priya.page
          .getByRole("button", { name: "I have copied it" })
          .click();
        await expect(priya.page.getByTestId("api-token-secret")).toHaveCount(0);

        const row = priya.page
          .getByTestId("api-token-table")
          .getByRole("row")
          .filter({ hasText: tokenName });
        await expect(row).toContainText("Active");
        await expect(row).toContainText(workspace);
        await expect(row).toContainText("caliber.operator");
        // The secret is never shown again, not even in the list.
        await expect(priya.page.locator("body")).not.toContainText(secret);
        await shot(priya.page, "06-token");
      });

      // ---- 7. priya revokes it --------------------------------------------------------------
      await test.step("7. Settings > API tokens: revoke the token", async () => {
        await priya.page
          .getByRole("button", { name: `Revoke ${tokenName}` })
          .click();
        await expect(priya.page.getByTestId("confirm-dialog")).toContainText(
          "stops working immediately",
        );
        await priya.page.getByRole("button", { name: "Revoke token" }).click();
        const row = priya.page
          .getByTestId("api-token-table")
          .getByRole("row")
          .filter({ hasText: tokenName });
        await expect(row).toContainText("Revoked");
        await expect(
          priya.page.getByRole("button", { name: `Revoke ${tokenName}` }),
        ).toHaveCount(0);
        await shot(priya.page, "07-revoked");
      });

      // ---- 8. The admin reads the audit log ------------------------------------------------
      await test.step("8. Audit Log: filter by entity, actor and action", async () => {
        await adminPage.goto("/caliber/audit-log");
        await expect(adminPage.getByTestId("audit-log-page")).toBeVisible();
        const rows = adminPage.getByTestId("audit-row");

        // Find the workspace's id the way a person would: the create_project row names it.
        await adminPage.getByTestId("audit-filter-actor").fill("maya");
        await adminPage
          .getByTestId("audit-filter-action")
          .fill("create_project");
        const createRow = rows.filter({ hasText: workspace });
        await expect(createRow).toHaveCount(1);
        const entity = await createRow.locator("td").nth(3).innerText();
        const auditProjectId = entity.match(/PRJ-[0-9a-f]+/)?.[0] ?? "";
        expect(auditProjectId).toBe(projectId);
        await adminPage.getByTestId("audit-filter-actor").fill("");
        await adminPage.getByTestId("audit-filter-action").fill("");

        // By entity: everything that happened to the workspace.
        await adminPage
          .getByTestId("audit-filter-entity-id")
          .fill(auditProjectId);
        await expect(rows.filter({ hasText: "create_project" })).toHaveCount(1);
        await expect(
          rows.filter({ hasText: "add_project_member" }),
        ).toHaveCount(7);
        await expect(rows.first()).toContainText("maya");

        // By action: the seven grants name their roles.
        await adminPage
          .getByTestId("audit-filter-action")
          .fill("add_project_member");
        await expect(rows).toHaveCount(7);
        await expect(rows.filter({ hasText: '"role":"viewer"' })).toHaveCount(
          2,
        );
        await expect(rows.filter({ hasText: '"role":"editor"' })).toHaveCount(
          2,
        );

        // By actor and action: the token's creation, with its binding and without its secret.
        await adminPage.getByTestId("audit-filter-entity-id").fill("");
        await adminPage.getByTestId("audit-filter-actor").fill("priya");
        await adminPage
          .getByTestId("audit-filter-action")
          .fill("create_personal_access_token");
        const created = rows.filter({ hasText: projectId });
        await expect(created).toHaveCount(1);
        await shot(adminPage, "08-audit-token");
        await expect(adminPage.locator("body")).not.toContainText(secret);
        await adminPage
          .getByTestId("audit-filter-action")
          .fill("revoke_personal_access_token");
        await expect(rows.first()).toContainText("personal_access_token");

        // Only an admin may read it: maya is refused.
        await maya.page.goto("/caliber/audit-log");
        await expect(maya.page.getByTestId("audit-forbidden")).toContainText(
          "Admin access required",
        );
      });

      // ---- 9. The release timeline and live board ------------------------------------------
      await test.step("9. Releases: the timeline and the live board show the prompt release", async () => {
        await priya.page.goto("/caliber/releases");
        await expect(priya.page.getByTestId("releases-page")).toBeVisible();
        const live = priya.page.getByTestId(`releases-live-${promptName}`);
        await expect(live).toBeVisible({ timeout: 30_000 });
        await expect(live).toContainText("@prod");
        await expect(live).toContainText("v1");
        await expect(live).toContainText("priya");
        const event = priya.page
          .getByTestId("releases-timeline")
          .getByRole("listitem")
          .filter({ hasText: promptName });
        await expect(event).toHaveCount(1);
        await expect(event).toContainText("priya");
        await shot(priya.page, "09-releases");
      });

      // ---- 10. Readiness: the health dot and the simulated-provider banner -----------------
      await test.step("10. The top bar and banner say what is up and what is simulated", async () => {
        await priya.page.goto("/caliber/");
        await expect(
          priya.page.getByLabel("API and database reachable"),
        ).toBeVisible();
        const banner = priya.page.getByTestId("provider-banner");
        await expect(banner).toContainText("Simulated providers:");
        await expect(banner).toContainText("eval, llm");
        await expect(banner).toContainText(
          "Evaluation scores are canned, not measurements.",
        );
        await expect(banner).not.toContainText("Not ready");
        await shot(priya.page, "10-readiness");
      });

      // ---- The lab itself is in the gallery, with the same ten steps ------------------------
      await test.step("The Cookbooks gallery lists this lab", async () => {
        await maya.page.goto("/caliber/cookbooks");
        await expect(maya.page.getByTestId("cookbook-card-18")).toContainText(
          "Projects, Roles, Tokens & Audit",
        );
      });
    } finally {
      for (const context of contexts) await context.close();
    }
  });
});
