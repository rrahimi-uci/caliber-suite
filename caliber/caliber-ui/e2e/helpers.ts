import { expect, type APIResponse, type Page } from "@playwright/test";

export interface UploadFixture {
  name: string;
  mimeType: string;
  body: string | Buffer;
}

interface Envelope<T> {
  data: T;
}

interface ObjectStoreListingResponse {
  objects: Array<{ key: string }>;
}

interface KnowledgeOptionsResponse {
  age_enabled: boolean;
  age_graph_name: string | null;
}

interface KnowledgeBaseListItem {
  knowledge_base_id: string;
  name: string;
  status: string;
}

const API_BASE = "/ajax-api/2.0/mlflow/caliber";
const RUN_STATUS_LABEL_TO_VALUE = new Map<string, string>([
  ["awaiting approval", "waiting_approval"],
  ["waiting for event", "waiting_event"],
]);

async function parseEnvelope<T>(
  responsePromise: Promise<APIResponse>,
  context: string,
): Promise<T> {
  const response = await responsePromise;
  if (!response.ok()) {
    throw new Error(`${context} failed (${response.status()}): ${await response.text()}`);
  }
  const payload = (await response.json()) as Envelope<T>;
  return payload.data;
}

export async function signIn(page: Page): Promise<void> {
  await expect
    .poll(
      async () => {
        const response = await page.request.get("/ajax-api/2.0/mlflow/caliber/health");
        return response.ok() ? "ok" : String(response.status());
      },
      { timeout: 30_000 },
    )
    .toBe("ok");
  await page.goto("/caliber/login");
  await expect(page.getByPlaceholder("Enter your username")).toBeVisible();
  await expect(page.getByPlaceholder("Enter your password")).toBeVisible();

  await page.getByPlaceholder("Enter your username").fill("admin");
  await page.getByPlaceholder("Enter your password").fill("admin");
  await page.getByRole("button", { name: "Sign in" }).click();

  await expect(page).toHaveURL(/\/caliber\/?$/);
  await expect(page.getByLabel("CALIBER navigation")).toBeVisible();
}

export async function registerAgent(
  page: Page,
  options?: {
    name?: string;
    // Retained for call-site compatibility; agent registration no longer takes
    // a prompt template (the dedicated Agent Fleet UI was removed).
    promptTemplate?: string;
  },
): Promise<string> {
  const stamp = `${Date.now()}-${Math.random().toString(36).slice(2, 7)}`;
  const name = options?.name ?? `Playwright Agent ${stamp}`;
  // The Agent Fleet registration page was removed in favour of the
  // asset-workspace model (agents are hidden runtime identities). Register the
  // agent directly through the API instead. The display name keeps a
  // sweep-matched prefix so global-teardown cleans it up.
  await parseEnvelope<{ agent_id: string }>(
    page.request.post(`${API_BASE}/agents`, {
      data: {
        agent_id: `pw-${stamp}`,
        experiment_id: `pw-exp-${stamp}`,
        name,
        owner: "admin",
      },
    }),
    `register agent ${name}`,
  );
  return name;
}

export async function createPromptViaApi(
  page: Page,
  options?: { name?: string; template?: string },
): Promise<{ name: string; template: string }> {
  const name = options?.name ?? uniqueSlug("pw-prompt");
  const template =
    options?.template ?? "You are a calm support agent. Resolve the escalation and cite context.";
  await parseEnvelope<{ name: string }>(
    page.request.post(`${API_BASE}/prompts`, {
      data: { name, template, commit_message: "playwright seed" },
    }),
    `create prompt ${name}`,
  );
  return { name, template };
}

export async function goToSidebarRoute(
  page: Page,
  label: string,
  expectedPath: string,
): Promise<void> {
  await page
    .locator('aside[aria-label="CALIBER navigation"] a.nav-item', { hasText: label })
    .first()
    .click();
  await expect(page).toHaveURL(new RegExp(`${expectedPath}(?:/)?(?:\\?.*)?$`));
}

export function uniqueSlug(prefix: string): string {
  const suffix = `${Date.now()}-${Math.random().toString(36).slice(2, 7)}`;
  return `${prefix}-${suffix}`.toLowerCase();
}

export async function readRunStatus(page: Page): Promise<string> {
  const badge = page.getByTestId("run-status-badge");
  const statusValue = (await badge.getAttribute("data-status"))?.trim().toLowerCase();
  if (statusValue) return statusValue;

  const statusText = ((await badge.textContent()) ?? "").trim().toLowerCase();
  return RUN_STATUS_LABEL_TO_VALUE.get(statusText) ?? statusText.replace(/\s+/g, "_");
}

export async function expectRunStatus(
  page: Page,
  expectedStatus: string,
  options?: {
    timeout?: number;
    intervals?: number[];
  },
): Promise<void> {
  await expect
    .poll(
      async () => readRunStatus(page),
      {
        timeout: options?.timeout ?? 90_000,
        intervals: options?.intervals ?? [1_000, 2_000, 5_000],
      },
    )
    .toBe(expectedStatus);
}

export async function knowledgeOptions(
  page: Page,
): Promise<KnowledgeOptionsResponse> {
  return parseEnvelope<KnowledgeOptionsResponse>(
    page.request.get(`${API_BASE}/knowledge-bases/options`),
    "load knowledge-base options",
  );
}

export async function deploymentAgeEnabled(page: Page): Promise<boolean> {
  const options = await knowledgeOptions(page);
  return Boolean(options.age_enabled);
}

export async function createObjectStoreBucket(
  page: Page,
  bucket: string,
): Promise<void> {
  await page.goto("/caliber/object-store");
  await expect(page.getByRole("heading", { name: "Object Store" })).toBeVisible();
  await page.getByTestId("new-bucket-input").fill(bucket);
  await page.getByTitle("Create bucket").click();
  await expect(page.getByTestId(`bucket-${bucket}`)).toBeVisible();
}

export async function openObjectStoreBucket(
  page: Page,
  bucket: string,
): Promise<void> {
  await page.getByTestId(`bucket-${bucket}`).click();
  await expect(page.getByTestId(`bucket-${bucket}`)).toContainText("Active workspace");
  await expect(
    page.locator('main nav[aria-label="Breadcrumb"]').getByRole("button", { name: bucket }),
  ).toBeVisible();
  await expect(page.getByRole("button", { name: "Upload" })).toBeEnabled();
}

export async function uploadObjectStoreFixtures(
  page: Page,
  bucket: string,
  fixtures: UploadFixture[],
): Promise<void> {
  await expect(
    page.locator('main nav[aria-label="Breadcrumb"]').getByRole("button", { name: bucket }),
  ).toBeVisible();
  const chooserPromise = page.waitForEvent("filechooser");
  await page.getByRole("button", { name: "Upload" }).click();
  const chooser = await chooserPromise;
  await chooser.setFiles(
    fixtures.map((fixture) => ({
      name: fixture.name,
      mimeType: fixture.mimeType,
      buffer: Buffer.isBuffer(fixture.body) ? fixture.body : Buffer.from(fixture.body, "utf-8"),
    })),
  );

  await expect
    .poll(async () => page.locator('tr[data-testid^="object-"]').count(), {
      timeout: 20000,
    })
    .toBe(fixtures.length);
  for (const fixture of fixtures) {
    await expect(page.getByTestId(`object-${fixture.name}`)).toBeVisible();
  }
}

export async function archiveKnowledgeBaseByName(
  page: Page,
  knowledgeBaseName: string,
): Promise<void> {
  const knowledgeBases = await parseEnvelope<KnowledgeBaseListItem[]>(
    page.request.get(`${API_BASE}/knowledge-bases?status=all`),
    "list knowledge bases",
  );
  const target = knowledgeBases.find((item) => item.name === knowledgeBaseName);
  if (!target || target.status === "archived") return;

  await parseEnvelope<KnowledgeBaseListItem>(
    page.request.patch(`${API_BASE}/knowledge-bases/${encodeURIComponent(target.knowledge_base_id)}`, {
      data: { status: "archived" },
    }),
    `archive knowledge base ${knowledgeBaseName}`,
  );
}

export async function deleteObjectStoreBucketRecursive(
  page: Page,
  bucket: string,
): Promise<void> {
  const listingResponse = await page.request.get(
    `${API_BASE}/object-store/buckets/${encodeURIComponent(bucket)}/objects?recursive=true`,
  );
  if (listingResponse.status() === 404) {
    return;
  }
  if (!listingResponse.ok()) {
    throw new Error(
      `list object-store objects in ${bucket} failed (${listingResponse.status()}): ${await listingResponse.text()}`,
    );
  }
  const listingPayload = (await listingResponse.json()) as Envelope<ObjectStoreListingResponse>;
  const keys = listingPayload.data.objects.map((item) => item.key).filter(Boolean);
  if (keys.length > 0) {
    await parseEnvelope<{ deleted: number; errors: string[] }>(
      page.request.post(
        `${API_BASE}/object-store/buckets/${encodeURIComponent(bucket)}/objects/delete`,
        { data: { keys } },
      ),
      `delete object-store objects in ${bucket}`,
    );
  }

  const deleteResponse = await page.request.delete(
    `${API_BASE}/object-store/buckets/${encodeURIComponent(bucket)}`,
  );
  if (deleteResponse.status() === 404 || deleteResponse.status() === 204) {
    return;
  }
  throw new Error(
    `delete bucket ${bucket} failed (${deleteResponse.status()}): ${await deleteResponse.text()}`,
  );
}
