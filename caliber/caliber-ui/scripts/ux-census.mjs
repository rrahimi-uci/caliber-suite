/**
 * UX census — the re-runnable source of the structural counts the UX
 * remediation plan is measured against.
 *
 * `ux-analysis-report.md` §11 proposes navigation changes and §9.5 proposes
 * deletions, both resting on counts (routes, destinations, page weight,
 * duplicated helpers, breadcrumb and PageHeader adoption). §14.1 rule 2 and
 * §14.5 both forbid acting on those numbers as prose: they have to be
 * reproducible, and re-measurable after each wave, or "trend downward" is not
 * a claim anyone can check.
 *
 * This script is UX-00's first deliverable. It reads source only — no network,
 * no credentials, no running app — and emits JSON (for diffing) and Markdown
 * (for pasting into a PR).
 *
 * Usage:
 *   node scripts/ux-census.mjs                      # Markdown to stdout
 *   node scripts/ux-census.mjs --json               # JSON to stdout
 *   node scripts/ux-census.mjs --out docs/ux/x.json # write JSON
 *   node scripts/ux-census.mjs --diff baseline.json # compare against a baseline
 *
 * Every exported function takes file *contents*, not paths, so the parsing
 * rules can be unit-tested against fixtures rather than against whatever the
 * repository happens to look like today. `collectCensus` is the only part that
 * touches the filesystem.
 */

import { readFileSync, readdirSync, writeFileSync, mkdirSync, existsSync } from "node:fs";
import { dirname, join, resolve } from "node:path";

/** Files whose top-level helpers §15.1 counts for duplication. */
export const WORKFLOW_SURFACES = [
  "src/components/workflows/WorkflowRunDebugger.tsx",
  "src/components/workflows/TraceReplayGraph.tsx",
  "src/pages/WorkflowDetail.tsx",
  "src/pages/WorkflowEditor.tsx",
];

/**
 * Route paths registered in `App.tsx`.
 *
 * Deliberately counts every `path=` occurrence including the `*` catch-all and
 * the redirect entries, then reports them separately — "31 addressable routes"
 * in the report body and "35 path= entries" are both true of the same file,
 * and a census that silently picks one invites the disagreement §15.1 exists
 * to prevent.
 */
export function parseRoutes(appSource) {
  const all = [...appSource.matchAll(/path="([^"]+)"/g)].map((m) => m[1]);
  const wildcard = all.filter((p) => p === "*");
  const addressable = all.filter((p) => p !== "*");
  const parameterized = addressable.filter((p) => p.includes(":"));
  const distinct = [...new Set(addressable)].sort();
  return {
    total: all.length,
    addressable: addressable.length,
    // `/login` is registered twice -- once in the unauthenticated branch and
    // once as an authenticated redirect -- so "how many routes" has two
    // defensible answers. Reporting both is what stops the census and the
    // report body disagreeing without either being wrong.
    distinct: distinct.length,
    wildcard: wildcard.length,
    parameterized: parameterized.length,
    paths: distinct,
    detailRoutes: [...new Set(parameterized)].sort(),
  };
}

/**
 * Page components with no route in `App.tsx`.
 *
 * A page is matched by its JSX usage (`<Overview`), which is how `App.tsx`
 * references them. `Overview.tsx` exports `Dashboard`, so it reports as
 * unrouted even though it is mounted — the alias is the finding, not a bug in
 * the census, and the report says so.
 */
export function findUnroutedPages(appSource, pageNames) {
  return pageNames.filter((name) => !appSource.includes(`<${name}`)).sort();
}

/** Does this source pass a real breadcrumb trail to PageHeader? */
export function passesCrumbs(source) {
  return /\bcrumbs=\{/.test(source);
}

/** Does this source use the shared PageHeader chrome at all? */
export function usesPageHeader(source) {
  return /\bPageHeader\b/.test(source);
}

/**
 * Top-level `function name(` declarations, which is the granularity the
 * duplication finding is about — a helper copied between two files, not a
 * nested closure or a method.
 */
export function topLevelFunctionNames(source) {
  return [...source.matchAll(/^function ([A-Za-z0-9_]+)/gm)].map((m) => m[1]);
}

/** Names declared at top level in more than one of the given sources. */
export function duplicatedHelpers(sourcesByFile) {
  const seen = new Map();
  for (const [file, source] of Object.entries(sourcesByFile)) {
    for (const name of topLevelFunctionNames(source)) {
      if (!seen.has(name)) seen.set(name, new Set());
      seen.get(name).add(file);
    }
  }
  return [...seen.entries()]
    .filter(([, files]) => files.size > 1)
    .map(([name, files]) => ({ name, files: [...files].sort() }))
    .sort((a, b) => a.name.localeCompare(b.name));
}

/**
 * Client calls that mutate server state, as a proxy for "controls a viewer
 * would be offered and then refused".
 *
 * A deliberately coarse heuristic: `caliberApi.<verb>...(` where the verb
 * prefix implies a write. §7.4's "at least 33" is a floor for exactly this
 * reason — the precise number needs the endpoint-to-affordance matrix (UX-06),
 * which pairs each control with the scope its endpoint requires. Reporting
 * this as "mutating call sites" rather than "ungated controls" keeps the
 * distinction honest.
 */
const MUTATING_PREFIXES = [
  "create",
  "update",
  "delete",
  "archive",
  "promote",
  "rollback",
  "publish",
  "set",
  "save",
  "append",
  "bind",
  "approve",
  "reject",
  "cancel",
  "retry",
  "resume",
  "activate",
  "deprecate",
  "import",
  "upload",
  "sync",
  "waive",
  "signoff",
];

export function countMutatingCalls(source) {
  const calls = [...source.matchAll(/caliberApi\.([A-Za-z0-9_]+)\s*\(/g)].map((m) => m[1]);
  return calls.filter((name) =>
    MUTATING_PREFIXES.some(
      (prefix) => name.startsWith(prefix) && name.length > prefix.length,
    ),
  ).length;
}

/** Line count matching `wc -l` (newline count, not segment count). */
export function countLines(source) {
  let n = 0;
  for (const ch of source) if (ch === "\n") n += 1;
  return n;
}

function listPageFiles(uiRoot) {
  return readdirSync(join(uiRoot, "src/pages"))
    .filter((f) => f.endsWith(".tsx"))
    .sort();
}

function read(uiRoot, rel) {
  return readFileSync(join(uiRoot, rel), "utf8");
}

/** Walk the repository and produce the full census. */
export function collectCensus(uiRoot) {
  const appSource = read(uiRoot, "src/App.tsx");
  const pageFiles = listPageFiles(uiRoot);
  const pageNames = pageFiles.map((f) => f.replace(/\.tsx$/, ""));

  const pages = pageFiles.map((file) => {
    const source = read(uiRoot, `src/pages/${file}`);
    return {
      file,
      // `wc -l` semantics -- it counts newlines, so a file ending in one has
      // the same count either way and `split("\n").length` would be one
      // higher. The report quotes `wc -l`, so match it rather than inventing a
      // second convention that disagrees by one on every file.
      lines: countLines(source),
      usesPageHeader: usesPageHeader(source),
      passesCrumbs: passesCrumbs(source),
      mutatingCalls: countMutatingCalls(source),
    };
  });

  const workflowSources = {};
  for (const rel of WORKFLOW_SURFACES) {
    workflowSources[rel] = read(uiRoot, rel);
  }

  const routes = parseRoutes(appSource);
  const duplicates = duplicatedHelpers(workflowSources);

  return {
    routes,
    unroutedPages: findUnroutedPages(appSource, pageNames),
    pages: {
      total: pages.length,
      withPageHeader: pages.filter((p) => p.usesPageHeader).length,
      passingCrumbs: pages.filter((p) => p.passesCrumbs).length,
      totalLines: pages.reduce((sum, p) => sum + p.lines, 0),
      heaviest: [...pages]
        .sort((a, b) => b.lines - a.lines)
        .slice(0, 5)
        .map(({ file, lines }) => ({ file, lines })),
    },
    duplicatedHelpers: {
      count: duplicates.length,
      names: duplicates.map((d) => d.name),
    },
    mutatingCallSites: pages.reduce((sum, p) => sum + p.mutatingCalls, 0),
  };
}

export function toMarkdown(census) {
  const { routes, pages, duplicatedHelpers: dup } = census;
  const lines = [
    "| Measure | Value |",
    "| --- | --- |",
    `| Addressable routes (registrations) | ${routes.addressable} |`,
    `| Addressable routes (distinct paths) | ${routes.distinct} |`,
    `| — of which detail (parameterized) | ${routes.parameterized} |`,
    `| \`path=\` entries in App.tsx | ${routes.total} |`,
    `| Page components | ${pages.total} |`,
    `| — using PageHeader | ${pages.withPageHeader} of ${pages.total} |`,
    `| — passing real crumbs | ${pages.passingCrumbs} of ${pages.total} |`,
    `| Unrouted page components | ${census.unroutedPages.length}${
      census.unroutedPages.length ? ` (${census.unroutedPages.join(", ")})` : ""
    } |`,
    `| Total page lines | ${pages.totalLines.toLocaleString("en-US")} |`,
    `| Duplicated workflow helpers | ${dup.count} |`,
    `| Mutating \`caliberApi\` call sites in pages | ${census.mutatingCallSites} |`,
    "",
    "Heaviest pages:",
    "",
    ...pages.heaviest.map((p) => `- \`${p.file}\` — ${p.lines.toLocaleString("en-US")} lines`),
  ];
  return lines.join("\n");
}

/** Compare two censuses, reporting only what moved. */
export function diffCensus(baseline, current) {
  const rows = [
    ["Addressable routes", baseline.routes.addressable, current.routes.addressable],
    ["Distinct route paths", baseline.routes.distinct, current.routes.distinct],
    ["Detail routes", baseline.routes.parameterized, current.routes.parameterized],
    ["Pages", baseline.pages.total, current.pages.total],
    ["Pages using PageHeader", baseline.pages.withPageHeader, current.pages.withPageHeader],
    ["Pages passing crumbs", baseline.pages.passingCrumbs, current.pages.passingCrumbs],
    ["Total page lines", baseline.pages.totalLines, current.pages.totalLines],
    ["Duplicated helpers", baseline.duplicatedHelpers.count, current.duplicatedHelpers.count],
    ["Mutating call sites", baseline.mutatingCallSites, current.mutatingCallSites],
  ];
  return rows
    .filter(([, before, after]) => before !== after)
    .map(([measure, before, after]) => ({
      measure,
      before,
      after,
      delta: after - before,
    }));
}

function main(argv) {
  const uiRoot = resolve(dirname(new URL(import.meta.url).pathname), "..");
  const census = collectCensus(uiRoot);

  const outIndex = argv.indexOf("--out");
  if (outIndex !== -1 && argv[outIndex + 1]) {
    const out = resolve(argv[outIndex + 1]);
    if (!existsSync(dirname(out))) mkdirSync(dirname(out), { recursive: true });
    writeFileSync(out, `${JSON.stringify(census, null, 2)}\n`, "utf8");
    process.stdout.write(`[ux-census] wrote ${out}\n`);
    return 0;
  }

  const diffIndex = argv.indexOf("--diff");
  if (diffIndex !== -1 && argv[diffIndex + 1]) {
    const baseline = JSON.parse(readFileSync(resolve(argv[diffIndex + 1]), "utf8"));
    const moved = diffCensus(baseline, census);
    if (moved.length === 0) {
      process.stdout.write("[ux-census] no change against baseline\n");
      return 0;
    }
    for (const row of moved) {
      const sign = row.delta > 0 ? "+" : "";
      process.stdout.write(`${row.measure}: ${row.before} -> ${row.after} (${sign}${row.delta})\n`);
    }
    return 0;
  }

  if (argv.includes("--json")) {
    process.stdout.write(`${JSON.stringify(census, null, 2)}\n`);
    return 0;
  }

  process.stdout.write(`${toMarkdown(census)}\n`);
  return 0;
}

// Only run when invoked directly, so the module stays importable by tests.
if (process.argv[1] && process.argv[1].endsWith("ux-census.mjs")) {
  process.exit(main(process.argv.slice(2)));
}
