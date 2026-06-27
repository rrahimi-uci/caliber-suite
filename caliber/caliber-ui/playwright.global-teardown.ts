import fs from "node:fs/promises";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { request, type APIRequestContext } from "@playwright/test";

function errnoCode(error: unknown): string | undefined {
  if (typeof error !== "object" || error === null || !("code" in error)) {
    return undefined;
  }
  const code = (error as { code?: unknown }).code;
  return typeof code === "string" ? code : undefined;
}

async function removeIfEmpty(target: string): Promise<void> {
  try {
    const entries = await fs.readdir(target);
    if (entries.length === 0) {
      await fs.rmdir(target);
    }
  } catch (error) {
    const code = errnoCode(error);
    if (code === "ENOENT" || code === "ENOTEMPTY") {
      return;
    }
    throw error;
  }
}

// ---------------------------------------------------------------------------
// Test-data sweep.
//
// E2E specs create resources against the live server (Postgres + MinIO) and
// don't all clean up after themselves, so they accumulate in the shared dev
// stores — agents pollute the prompt inventory's "Needs prompt" backlog,
// buckets pile up in MinIO, etc. Every spec names its resources with a known
// marker (`uniqueSlug` → "playwright-"/"pw-" prefix; agents carry a fixed set
// of display-name prefixes), so we can identify and delete exactly the test
// data without touching real showcase content.
//
// Auth: the dev/E2E server runs with CALIBER_DEV_USER=@local-admin (an admin)
// and CSRF disabled, so a header-less request context resolves to that admin —
// the same path the in-test `page.request` calls rely on.
// ---------------------------------------------------------------------------
const API_BASE = "/ajax-api/2.0/mlflow/caliber";
const TEST_SLUG = /^(playwright-|pw-)/i;
const TEST_AGENT_NAME =
  /^(Playwright Fleet|Playwright Agent|Prompt Playground Agent|Prompt Version Agent|Prompt Optimization Agent|Prompt Calibration Agent) /;
// Registry prompts created by tests use the slug prefixes above, plus two
// extra shapes that optimization/refinement runs register straight into the
// MLflow registry: "caliber-gepa-{job_id}" (GEPA baselines) and candidate
// drafts under the canonical test-fixture agent ids. These are matched on top
// of TEST_SLUG so the prompt sweep catches them too. Showcase prompts
// (cortex-*) and any human-authored prompt are unaffected.
const TEST_PROMPT_NAME = /^(playwright-|pw-|caliber-gepa-)/i;
const TEST_FIXTURE_PROMPTS = new Set(["support-agent", "orders-agent", "travel-agent"]);
// MCP servers registered by the specs use a spaced display name
// ("Playwright GitHub MCP <ts>"), not the slug prefix — match either form so
// the real catalog servers (e.g. "GitHub") are left untouched.
const TEST_MCP_NAME = /^(playwright[ -]|pw-)/i;

type Row = Record<string, unknown>;

function str(value: unknown): string {
  return typeof value === "string" ? value : "";
}

async function listData(ctx: APIRequestContext, route: string): Promise<Row[]> {
  const res = await ctx.get(`${API_BASE}${route}`);
  if (!res.ok()) return [];
  const body = (await res.json()) as { data?: unknown };
  return Array.isArray(body.data) ? (body.data as Row[]) : [];
}

async function sweep(
  label: string,
  rows: Row[],
  remove: (row: Row) => Promise<boolean>,
): Promise<void> {
  let removed = 0;
  for (const row of rows) {
    try {
      if (await remove(row)) removed += 1;
    } catch {
      // Best-effort: skip a row that won't delete (FK left over, race, etc.).
    }
  }
  if (removed > 0) {
    console.log(`[global-teardown] removed ${removed} test ${label}`);
  }
}

async function deleteBucketRecursive(
  ctx: APIRequestContext,
  bucket: string,
): Promise<boolean> {
  const listing = await ctx.get(
    `${API_BASE}/object-store/buckets/${encodeURIComponent(bucket)}/objects?recursive=true`,
  );
  if (listing.ok()) {
    const payload = (await listing.json()) as {
      data?: { objects?: Array<{ key?: string }> };
    };
    const keys = (payload.data?.objects ?? [])
      .map((o) => str(o.key))
      .filter(Boolean);
    if (keys.length > 0) {
      await ctx.post(
        `${API_BASE}/object-store/buckets/${encodeURIComponent(bucket)}/objects/delete`,
        { data: { keys } },
      );
    }
  }
  const res = await ctx.delete(
    `${API_BASE}/object-store/buckets/${encodeURIComponent(bucket)}`,
  );
  return res.ok() || res.status() === 404;
}

async function sweepTestData(): Promise<void> {
  const PORT = Number(process.env["MLFLOW_PORT"] ?? 5150);
  const baseURL =
    process.env["CALIBER_E2E_BASE_URL"] ?? `http://127.0.0.1:${PORT}/caliber`;
  // The API lives at the host root (/ajax-api/...), not under /caliber.
  const origin = new URL(baseURL).origin;

  let ctx: APIRequestContext | undefined;
  try {
    ctx = await request.newContext({ baseURL: origin });
    // Reachability probe — bail quietly if the server is already down.
    const health = await ctx.get(`${API_BASE}/health`);
    if (!health.ok()) return;

    // Workflows first: deleting a workflow cascades its fleet agents + runs,
    // so this shrinks the agent sweep that follows.
    await sweep(
      "workflow(s)",
      (await listData(ctx, "/workflows")).filter((w) => TEST_SLUG.test(str(w["name"]))),
      async (w) => {
        const id = str(w["workflow_id"]);
        if (!id) return false;
        const res = await ctx!.delete(
          `${API_BASE}/workflows/${encodeURIComponent(id)}`,
        );
        return res.ok() || res.status() === 404;
      },
    );

    // Standalone agents registered directly by the specs (not via a workflow).
    await sweep(
      "agent(s)",
      (await listData(ctx, "/agents")).filter((a) => TEST_AGENT_NAME.test(str(a["name"]))),
      async (a) => {
        const id = str(a["agent_id"]);
        if (!id) return false;
        const res = await ctx!.delete(
          `${API_BASE}/agents/${encodeURIComponent(id)}`,
        );
        return res.ok() || res.status() === 404;
      },
    );

    // Prompts in the MLflow registry created by tests. Match by name — NOT by
    // has_prompt: optimizer baselines/drafts never get a prod alias, so they
    // surface as needs_prompt yet still exist in the registry and are
    // deletable. DELETE is 404-safe, so a promptless agent row that carries no
    // registry prompt is harmlessly skipped.
    await sweep(
      "prompt(s)",
      (await listData(ctx, "/prompts")).filter((p) => {
        const name = str(p["prompt_name"]) || str(p["agent_id"]);
        return TEST_PROMPT_NAME.test(name) || TEST_FIXTURE_PROMPTS.has(name.toLowerCase());
      }),
      async (p) => {
        const name = str(p["prompt_name"]) || str(p["agent_id"]);
        if (!name) return false;
        const res = await ctx!.delete(
          `${API_BASE}/prompts/${encodeURIComponent(name)}`,
        );
        return res.ok() || res.status() === 404;
      },
    );

    // Object-store buckets in MinIO created by the specs.
    await sweep(
      "bucket(s)",
      (await listData(ctx, "/object-store/buckets")).filter((b) =>
        TEST_SLUG.test(str(b["name"])),
      ),
      async (b) => {
        const name = str(b["name"]);
        if (!name) return false;
        return deleteBucketRecursive(ctx!, name);
      },
    );

    // MCP servers registered by the specs (e.g. the "mcp quick connect" test).
    // These have a hard DELETE and accumulate fast (one per run), so sweep them.
    await sweep(
      "mcp server(s)",
      (await listData(ctx, "/mcp-servers")).filter((m) => TEST_MCP_NAME.test(str(m["name"]))),
      async (m) => {
        const id = str(m["server_id"]);
        if (!id) return false;
        const res = await ctx!.delete(`${API_BASE}/mcp-servers/${encodeURIComponent(id)}`);
        return res.ok() || res.status() === 404;
      },
    );

    // Soft-delete resource types: skills, tools, eval-datasets, and knowledge
    // bases have no hard DELETE by platform design (status=archived preserves
    // the governance audit trail). Archive the test rows so they drop out of
    // every active view, which is what the inventory/library pages show.
    const archivable = (rows: Row[]) =>
      rows.filter(
        (r) => TEST_SLUG.test(str(r["name"])) && str(r["status"]) !== "archived",
      );
    const patchArchive = async (route: string): Promise<boolean> => {
      const res = await ctx!.patch(`${API_BASE}${route}`, {
        data: { status: "archived" },
      });
      return res.ok();
    };

    await sweep(
      "skill(s) (archived)",
      archivable(await listData(ctx, "/skills?status=all")),
      async (s) => {
        const id = str(s["skill_id"]);
        return id ? patchArchive(`/skills/${encodeURIComponent(id)}`) : false;
      },
    );

    await sweep(
      "tool(s) (archived)",
      archivable(await listData(ctx, "/tools?status=all")),
      async (t) => {
        const id = str(t["tool_id"]);
        if (!id) return false;
        // Tools use a dedicated archive endpoint; it 409s if a deployed
        // workflow still references the tool (skipped, not fatal).
        const res = await ctx!.post(`${API_BASE}/tools/${encodeURIComponent(id)}/archive`);
        return res.ok();
      },
    );

    await sweep(
      "dataset(s) (archived)",
      archivable(await listData(ctx, "/eval-datasets?status=all")),
      async (d) => {
        const id = str(d["dataset_id"]);
        return id ? patchArchive(`/eval-datasets/${encodeURIComponent(id)}`) : false;
      },
    );

    // Knowledge bases support a hard cascade delete (versions, chunks, graph,
    // runs, test-runs, + best-effort object-store artifacts + AGE), so fully
    // remove the test KBs rather than just archiving them. Match all statuses.
    await sweep(
      "knowledge base(s)",
      (await listData(ctx, "/knowledge-bases?status=all")).filter((k) =>
        TEST_SLUG.test(str(k["name"])),
      ),
      async (k) => {
        const id = str(k["knowledge_base_id"]);
        if (!id) return false;
        const res = await ctx!.delete(
          `${API_BASE}/knowledge-bases/${encodeURIComponent(id)}`,
        );
        return res.ok() || res.status() === 404;
      },
    );
  } catch {
    // Never fail the run over teardown: the server may be down, or auth may be
    // enabled in some environments.
  } finally {
    await ctx?.dispose();
  }
}

export default async function globalTeardown(): Promise<void> {
  const configDir = path.dirname(fileURLToPath(import.meta.url));
  const repoRoot = path.resolve(configDir, "..");
  await sweepTestData();
  await removeIfEmpty(path.join(repoRoot, ".tmp"));
}
