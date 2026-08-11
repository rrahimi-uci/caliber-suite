/**
 * CALIBER documentation site generator.
 *
 * Reads the repository-level layered architecture plus the architecture series
 * under `docs/` and renders each into a polished, self-navigating HTML page
 * under `docs-site/`, reusing the shared design system (`docs.css` + `docs.js`).
 * It also emits `docs-nav.js`, the single source of truth for the sidebar
 * navigation shared by every page (including the hand-authored landing
 * `index.html`).
 *
 * The renderer is intentionally dependency-free so this can run inside the
 * `prebuild` hook in any context (including Docker stages with no node_modules).
 * It supports exactly the markdown constructs the docs use: ATX headings, GFM
 * tables, fenced code (with `mermaid` diagrams rendered client-side and
 * `diagram-svg` assets inlined at build time), ordered and unordered lists with
 * nesting + wrapped continuations, blockquotes (which may themselves contain
 * tables), and inline code / bold / italic / links.
 *
 * Cross-references between docs (`../11-test-sets/architecture.md`) are rewritten
 * to the generated page; links into source files are downgraded to inline code
 * so the docs never carry a broken hyperlink.
 *
 * Usage:  node docs-site/build-docs.mjs
 */

import { readFileSync, writeFileSync, readdirSync, existsSync, rmSync, renameSync, statSync } from "node:fs";
import { dirname, isAbsolute, relative, resolve, posix } from "node:path";
import { fileURLToPath } from "node:url";
import { execFileSync } from "node:child_process";

const here = dirname(fileURLToPath(import.meta.url));
const REPO_ROOT = resolve(here, ".."); // caliber-suite
const DOCS_DIR = resolve(here, "../docs"); // caliber-suite/docs
const OUT_DIR = here; // caliber-suite/docs-site
const SITE_MAP_PATH = resolve(here, "docs-site-map.json");
const GITHUB_SOURCE_BASE = "https://github.com/rrahimi-uci/caliber-suite";
const BRAND_SHORT = "CALIBER";
const BRAND_FULL = "CALIBER : Contextual Adaptive Lifecycle for Intelligent Build, Evaluation, and Refinement";
const DOCS_HOME_LABEL = `${BRAND_FULL} docs home`;
const STRICT = process.env.CALIBER_DOCS_STRICT === "1";

function writeTextAtomic(dest, contents) {
  const tmp = `${dest}.tmp-${process.pid}-${Date.now()}`;
  writeFileSync(tmp, contents, "utf8");
  try {
    renameSync(tmp, dest);
  } catch (err) {
    try {
      rmSync(tmp, { force: true });
    } catch {}
    throw err;
  }
}

// ---------------------------------------------------------------------------
// Module manifest — ordering, grouping, output filenames, and nav labels.
// This is the single source of truth the generator and the sidebar share.
// ---------------------------------------------------------------------------

const GROUPS = [
  { id: "platform", title: "Platform docs", eyebrow: "Architecture reference" },
  { id: "api", title: "REST API", eyebrow: "HTTP reference" },
  { id: "sdk", title: "Python SDK", eyebrow: "Developer reference" },
  { id: "examples", title: "Examples", eyebrow: "Runnable examples" },
  { id: "strategy", title: "Strategy & roadmap" },
];

const MODULES = [
  { md: "../ARCHITECTURE.md", out: "m-00-layered-architecture.html", group: "platform", label: "Layered architecture", blurb: "The six-layer platform stack, abstract lifecycle chain, governed-asset anatomy, per-family guarantees, deployment topologies, state ownership, trust boundaries, and extension seams." },
  { md: "01-caliber/architecture.md", out: "m-01-platform.html", group: "platform", label: "Platform", blurb: "Boot and dependency graph, embedded-or-standalone topology choice, shared runtime state, async workers, and the trust boundary the whole product stands on." },
  { md: "refinement-loop.md", out: "m-00-refinement-loop.html", group: "platform", label: "The refinement loop", blurb: "The canonical prompt-refinement path — verify, diagnose, optimize, evaluate, review/apply, and durable release — plus the evidence and recovery boundaries that differ across other asset families. Read this first." },
  { md: "02-prompts/architecture.md", out: "m-02-prompts.html", group: "platform", label: "Prompts", blurb: "Non-live MLflow Prompt Registry authoring, render/test history, hidden runtime targets, queued optimizer-backed calibration, and durable alias release/reconciliation." },
  { md: "03-tools/architecture.md", out: "m-03-tools.html", group: "platform", label: "Tools", blurb: "Versioned callable registry, bounded subprocess test runs, fixture suites and baselines, and deterministic replay calibration." },
  { md: "04-skills/architecture.md", out: "m-04-skills.html", group: "platform", label: "Skills", blurb: "Reusable instruction assets with packaging, render/selection tests, deterministic runtime selection, and agent-free calibration." },
  { md: "05-mcp/architecture.md", out: "m-05-mcp.html", group: "platform", label: "MCP servers", blurb: "Managed MCP server definitions, transport-aware configuration, connection tests, discovered tool inventories, and policy-managed remote tool use." },
  { md: "06-workflows/architecture.md", out: "m-06-workflows.html", group: "platform", label: "Workflows", blurb: "Manifest authoring, compile/preview/run, versioning and deployments, queued runtime execution, checkpoints, and workflow-as-a-service." },
  { md: "06-workflows/components.md", out: "m-06-workflows-components.html", group: "platform", label: "Workflow components", blurb: "The building blocks of a workflow: component anatomy, the typed data-type port system, adding/configuring/connecting/coding/running nodes, and a complete reference for all 31 built-in components." },
  { md: "07-object-store/architecture.md", out: "m-07-object-store.html", group: "platform", label: "Object store", blurb: "MinIO/S3 console and the storage substrate behind uploads, previews, extraction, and artifact browsing." },
  { md: "08-knowledge-bases/architecture.md", out: "m-08-knowledge-bases.html", group: "platform", label: "Knowledge bases", blurb: "Versioned RAG corpora with ingestion, chunking, embeddings, Apache AGE graph extraction, hybrid retrieval, pgvector ANN + cross-encoder reranking at scale, and calibration." },
  { md: "11-test-sets/architecture.md", out: "m-11-test-sets.html", group: "platform", label: "Test sets", blurb: "Versioned evaluation datasets with a hand-curation row editor, trace-to-example capture, MLflow GenAI dataset sync, and the shared evidence base for scoring." },
  { md: "14-evaluation/architecture.md", out: "m-14-evaluation.html", group: "platform", label: "Evaluation", blurb: "Dataset scorecards with selectable custom LLM judges, artifact-targeted runs (prompt/skill), a judge playground + human-alignment (agreement/kappa), per-example results, and fail-closed evaluation." },
  { md: "15-calibration/architecture.md", out: "m-15-calibration.html", group: "platform", label: "Calibration", blurb: "Asset-specific evidence loops: provider-and-EvalProvider refinement for prompts and skills, manifest replay for workflows, and revision-fenced deterministic tool suites inline or queued." },
  { md: "09-observability/architecture.md", out: "m-09-observability.html", group: "platform", label: "Observability", blurb: "MLflow traces, feedback, Prometheus metrics, SSE, durable SLO incidents, webhook settlement/dead letters, service visibility, and trace retention." },
  { md: "10-gateways/architecture.md", out: "m-10-gateways.html", group: "platform", label: "Gateways", blurb: "External MLflow AI Gateway discovery, governed guardrail configuration, trace-derived usage, per-model pricing, and CALIBER routing visibility." },
  { md: "13-qa-plan/architecture.md", out: "m-13-qa-plan.html", group: "platform", label: "QA plan", blurb: "Runtime QA state, runtime approvals, engineering validation suites, and the merged Allure evidence model." },
  { md: "runbook.md", out: "m-19-runbook.html", group: "platform", label: "Operations runbook", blurb: "The on-call procedure for recoveries CALIBER cannot complete alone — an unsettled release intent, an indeterminate external effect, a queue that stopped draining, a lost at-most-once job, and rollback whose semantics differ per asset family — plus what each triage surface does not prove." },
  { md: "12-assistant/architecture.md", out: "m-12-assistant.html", group: "platform", label: "Aria assistant", blurb: "Aria's session model, the permissioned agentic tool loop, interaction and approval modes, governed drafts, and transparent execution." },
  { md: "api/overview.md", out: "m-26-rest-api-overview.html", group: "api", label: "Overview", blurb: "Where the management API lives, when to integrate at the HTTP layer, and where the served OpenAPI documents fit." },
  { md: "api/auth.md", out: "m-27-rest-api-auth.html", group: "api", label: "Authentication and conventions", blurb: "Bearer authentication, trusted-header deployments, project scoping, CSRF, envelopes, pagination, request ids, and error shapes." },
  { md: "api/resources.md", out: "m-28-rest-api-resources.html", group: "api", label: "Resource catalog", blurb: "The route families mounted under the CALIBER management API, grouped by the domains developers actually integrate with." },
  { md: "api/reference.md", out: "m-29-rest-api-reference.html", group: "api", label: "HTTP reference", blurb: "The concrete contract map: core endpoints, representative operations, and the served OpenAPI entry points for management and workflow services." },
  { md: "start/choose-your-path.md", out: "m-30-choose-your-path.html", group: "platform", label: "Choose your CALIBER path", blurb: "The role-first entry point into the docs: choose the right path for integration, operations, governance, architecture, or product fit." },
  { md: "start/decision-maker-overview.md", out: "m-35-decision-maker-overview.html", group: "strategy", label: "Decision-maker overview", blurb: "A concise non-implementation path through what CALIBER is, what it solves, and where to go next for technical proof." },
  { md: "start/five-minute-quickstart.md", out: "m-37-five-minute-quickstart.html", group: "platform", label: "5-minute quickstart", blurb: "The shortest path to a real CALIBER success: bring up the local stack, verify readiness, or jump into the right next guide." },
  { md: "build/auth-and-project-scoping.md", out: "m-31-auth-and-project-scoping.html", group: "api", label: "Auth and project scoping", blurb: "The minimum authentication and project-scoping decisions a developer must get right before calling CALIBER programmatically." },
  { md: "build/sdk-vs-rest-api.md", out: "m-36-sdk-vs-rest-api.html", group: "sdk", label: "SDK vs REST API", blurb: "The first integration decision for developers: when to use the typed Python SDK and when to work at the raw HTTP layer." },
  { md: "build/error-handling-and-retries.md", out: "m-53-error-handling-and-retries.html", group: "sdk", label: "Error handling and retries", blurb: "The practical integration policy for CALIBER failures: classify them correctly, use waiters where available, and retry only what is actually safe to retry." },
  { md: "build/ci-cd-automation.md", out: "m-54-ci-cd-automation.html", group: "sdk", label: "CI/CD automation", blurb: "The repository-grounded automation path for CALIBER development: docs generation, focused contract tests, and the human-review boundaries that remain deliberate." },
  { md: "build/developer-troubleshooting.md", out: "m-55-developer-troubleshooting.html", group: "sdk", label: "Developer troubleshooting", blurb: "The first-pass diagnosis path for CALIBER SDK and REST integrations: auth, project scoping, CSRF, uploads, waiters, and workflow invocation." },
  { md: "operate/local-bring-up.md", out: "m-32-local-bring-up.html", group: "platform", label: "Local bring-up", blurb: "The operator-first path for starting CALIBER locally, verifying readiness, and choosing the next guide based on your job." },
  { md: "operate/configuration-and-provider-setup.md", out: "m-48-configuration-and-provider-setup.html", group: "platform", label: "Configuration and provider setup", blurb: "The operator path for configuring CALIBER: ports, database, LLM provider and gateway, object store, workflow storage, and event backend." },
  { md: "operate/storage-and-state.md", out: "m-49-storage-and-state.html", group: "platform", label: "Storage and state", blurb: "The operator guide to where CALIBER keeps metadata, files, workflow workspaces, and retention-controlled artifacts." },
  { md: "operate/health-and-readiness.md", out: "m-50-health-and-readiness.html", group: "platform", label: "Health and readiness", blurb: "The operator meaning of CALIBER liveness, readiness, and the deeper evidence surfaces to use when a runtime is up but not actually usable." },
  { md: "operate/troubleshooting.md", out: "m-51-operator-troubleshooting.html", group: "platform", label: "Operator troubleshooting", blurb: "The first-pass diagnosis path for degraded CALIBER deployments before a failure escalates into full runbook recovery." },
  { md: "operate/backup-and-recovery.md", out: "m-52-backup-and-recovery.html", group: "platform", label: "Backup and recovery", blurb: "The operator view of what CALIBER state must be preserved, what can be rebuilt, and where disaster recovery stops and asset-specific rollback begins." },
  { md: "use/trust-and-governance.md", out: "m-33-trust-and-governance.html", group: "platform", label: "Trust and governance", blurb: "A curated reader path across the trust surfaces that connect evidence, calibration, review, and apply." },
  { md: "use/prompts.md", out: "m-38-use-prompts.html", group: "platform", label: "Prompts", blurb: "The task-oriented entry point for prompt assets in CALIBER: author, evaluate, calibrate, release, and recover without starting from deep architecture." },
  { md: "use/tools.md", out: "m-39-use-tools.html", group: "platform", label: "Tools", blurb: "The practical guide to defining, testing, governing, and using callable tools in workflows and assistant surfaces." },
  { md: "use/skills.md", out: "m-40-use-skills.html", group: "platform", label: "Skills", blurb: "The task-oriented guide to packaging reusable instructions, testing selection behavior, and using skills with assistant and workflow flows." },
  { md: "use/mcp-servers.md", out: "m-41-use-mcp-servers.html", group: "platform", label: "MCP servers", blurb: "The practical path for connecting CALIBER to MCP servers, verifying discovered tools, and governing remote tool use." },
  { md: "use/workflows.md", out: "m-42-use-workflows.html", group: "platform", label: "Workflows", blurb: "The user and operator path for authoring, previewing, running, deploying, and debugging workflows in CALIBER." },
  { md: "use/knowledge-bases.md", out: "m-43-use-knowledge-bases.html", group: "platform", label: "Knowledge bases", blurb: "The practical guide for ingesting knowledge, improving retrieval quality, and connecting grounded content to assistant or workflow behavior." },
  { md: "use/evaluation-and-test-sets.md", out: "m-44-use-evaluation-and-test-sets.html", group: "platform", label: "Evaluation and test sets", blurb: "The practical trust loop: define evidence, run scorecards, inspect judges, and decide what still needs review before live change." },
  { md: "use/calibration.md", out: "m-45-use-calibration.html", group: "platform", label: "Calibration", blurb: "The practical calibration path: choose the asset family, generate candidates, inspect evidence, and move to explicit review or apply." },
  { md: "use/aria-assistant.md", out: "m-46-use-aria-assistant.html", group: "platform", label: "Aria assistant", blurb: "The operator and user guide for supervised assistant sessions, governed tool use, and explicit review boundaries." },
  { md: "use/review-and-release-flows.md", out: "m-47-use-review-and-release-flows.html", group: "platform", label: "Review and release flows", blurb: "The practical path from candidate evidence to human review, explicit apply, release, rollback, and operator recovery." },
  { md: "architecture/index.md", out: "m-34-architecture-reader-guide.html", group: "platform", label: "Architecture reader guide", blurb: "The curated entry point into the architecture corpus: start with the right deep page instead of reading the whole tree." },
  { md: "reference/config-and-environment.md", out: "m-56-config-and-environment-reference.html", group: "api", label: "Config and environment reference", blurb: "The operator and integrator lookup surface for the configuration families exposed in the current repository." },
  { md: "sdk/guide.md", out: "m-20-sdk-guide.html", group: "sdk", label: "Guide", blurb: "Install, authenticate, scope to a project, and call the management API from Python \u2014 with every snippet taken from the executable examples the SDK test suite runs." },
  { md: "sdk/reference.md", out: "m-21-sdk-reference.html", group: "sdk", label: "API reference", blurb: "Every GA resource module the client exposes, the models they decode into, error types, waiters, and the stability tier each surface carries." },
  { md: "sdk/beta.md", out: "m-22-sdk-beta.html", group: "sdk", label: "Beta and agentic surfaces", blurb: "Integrations, operations, cookbooks, and the Aria loop — and the property that shapes all of them: work that stops for a person is neither running nor finished." },
  { md: "sdk/plugins.md", out: "m-23-sdk-plugins.html", group: "sdk", label: "Writing a plugin", blurb: "Add an optimizer to CALIBER from your own distribution — the contract, the conformance suite, and why installing a plugin deliberately does not enable it." },
  { md: "sdk/cli.md", out: "m-24-sdk-cli.html", group: "sdk", label: "CLI and async client", blurb: "caliberctl for operator flows, where its six exit codes come from, and the asynchronous client — what it covers, what it deliberately does not, and how one polling policy serves both." },
  { md: "sdk/cookbooks.md", out: "m-25-sdk-cookbooks.html", group: "examples", label: "SDK recipes", blurb: "Full runnable cookbook implementations that use only caliber-sdk: inspect readiness, install the maintained recipe, and finish the scenario through typed SDK calls and raw fallbacks where needed." },
  { md: "competitive-analysis.md", out: "m-17-competitive-analysis.html", group: "strategy", label: "Competitive analysis", blurb: "How CALIBER compares to Langflow, Flowise, Dify, n8n, Flowable, the LLMOps/eval tools, MLflow GenAI, and the AWS/Google/Microsoft cloud stacks — strengths, weaknesses, and the defensible wedge, with every competitor claim grounded in primary sources." },
  { md: "roadmap.md", out: "m-18-roadmap.html", group: "strategy", label: "Roadmap", blurb: "The feasibility-grounded, quarter-by-quarter plan derived from the competitive analysis and verified against the architecture — themes, deliverables, ownership, and the adversarial feasibility review." },
];

function isWithin(base, target) {
  const rel = relative(base, target);
  return (
    rel === "" ||
    (!isAbsolute(rel) && rel !== ".." && !rel.startsWith("../") && !rel.startsWith("..\\"))
  );
}

function validateModuleManifest(modules) {
  const rootArchitecture = resolve(REPO_ROOT, "ARCHITECTURE.md");
  const sources = new Set();
  const outputs = new Set();

  for (const mod of modules) {
    const source = resolve(DOCS_DIR, mod.md);
    const output = resolve(OUT_DIR, mod.out);
    const sourceAllowed =
      source === rootArchitecture ||
      (source !== DOCS_DIR && isWithin(DOCS_DIR, source));
    if (!sourceAllowed || !source.endsWith(".md")) {
      throw new Error(`[build-docs] unsafe module source ${mod.md}`);
    }
    if (dirname(output) !== OUT_DIR || !/^m-[a-z0-9-]+\.html$/.test(mod.out)) {
      throw new Error(`[build-docs] unsafe module output ${mod.out}`);
    }
    if (sources.has(source) || outputs.has(output)) {
      throw new Error(`[build-docs] duplicate module mapping ${mod.md} -> ${mod.out}`);
    }
    sources.add(source);
    outputs.add(output);
  }
}

validateModuleManifest(MODULES);

// Resolve sources canonically rather than comparing docs/-relative strings. The
// repository-level ARCHITECTURE.md links into docs/, while the nested modules
// link relative to their own directories; absolute keys make both forms agree.
const sourceToModule = new Map(
  MODULES.map((m) => [resolve(DOCS_DIR, m.md), m])
);
const outputToModule = new Map(
  MODULES.map((m) => [resolve(OUT_DIR, m.out), m])
);
const moduleByOutput = new Map(
  MODULES.map((m) => [m.out, m])
);

const ALLOWED_AUDIENCES = new Set([
  "developer",
  "system-user",
  "operator",
  "architect",
  "evaluator",
  "decision-maker",
]);
const ALLOWED_DOC_TYPES = new Set([
  "tutorial",
  "how-to",
  "concept",
  "reference",
  "runbook",
  "example",
  "strategy",
]);
const ALLOWED_STABILITY = new Set(["ga", "beta", "experimental", "internal"]);
const DOCS_SECTION_FALLBACK_SUMMARY = "Browse this documentation section.";

function readJsonFile(path) {
  return JSON.parse(readFileSync(path, "utf8"));
}

function ensureStringArray(values, where) {
  if (!Array.isArray(values) || !values.every((item) => typeof item === "string" && item.trim())) {
    throw new Error(`[build-docs] ${where} must be an array of non-empty strings`);
  }
  return values.map((item) => item.trim());
}

function validateDocMetadata(metadata, where) {
  if (!metadata || typeof metadata !== "object") return;
  if (metadata.audience !== undefined) {
    for (const audience of ensureStringArray(metadata.audience, `${where}.audience`)) {
      if (!ALLOWED_AUDIENCES.has(audience)) {
        throw new Error(`[build-docs] ${where}.audience has invalid value ${audience}`);
      }
    }
  }
  if (metadata.doc_type !== undefined && !ALLOWED_DOC_TYPES.has(metadata.doc_type)) {
    throw new Error(`[build-docs] ${where}.doc_type has invalid value ${metadata.doc_type}`);
  }
  if (metadata.stability !== undefined && !ALLOWED_STABILITY.has(metadata.stability)) {
    throw new Error(`[build-docs] ${where}.stability has invalid value ${metadata.stability}`);
  }
  if (metadata.prerequisites !== undefined) {
    ensureStringArray(metadata.prerequisites, `${where}.prerequisites`);
  }
  if (metadata.tags !== undefined) {
    ensureStringArray(metadata.tags, `${where}.tags`);
  }
}

function loadSiteMap() {
  if (!existsSync(SITE_MAP_PATH)) {
    throw new Error(`[build-docs] required site map ${relative(REPO_ROOT, SITE_MAP_PATH)} not found`);
  }
  const raw = readJsonFile(SITE_MAP_PATH);
  const defaults = raw.default_metadata && typeof raw.default_metadata === "object" ? raw.default_metadata : {};
  validateDocMetadata(defaults, "default_metadata");

  if (!Array.isArray(raw.sections) || !raw.sections.length) {
    throw new Error("[build-docs] docs-site-map.json must define a non-empty sections array");
  }
  if (!Array.isArray(raw.pages) || !raw.pages.length) {
    throw new Error("[build-docs] docs-site-map.json must define a non-empty pages array");
  }

  const sections = raw.sections.map((section, idx) => {
    if (!section || typeof section !== "object") {
      throw new Error(`[build-docs] sections[${idx}] must be an object`);
    }
    if (!section.id || !section.title) {
      throw new Error(`[build-docs] sections[${idx}] must define id and title`);
    }
    const metadataDefaults =
      section.metadata_defaults && typeof section.metadata_defaults === "object"
        ? section.metadata_defaults
        : {};
    validateDocMetadata(metadataDefaults, `sections[${idx}].metadata_defaults`);
    return {
      id: String(section.id),
      title: String(section.title),
      summary: typeof section.summary === "string" && section.summary.trim()
        ? section.summary.trim()
        : DOCS_SECTION_FALLBACK_SUMMARY,
      order: Number.isFinite(section.order) ? section.order : (idx + 1) * 10,
      metadata_defaults: metadataDefaults,
    };
  });

  const sectionIds = new Set();
  for (const section of sections) {
    if (sectionIds.has(section.id)) {
      throw new Error(`[build-docs] duplicate section id ${section.id} in docs-site-map.json`);
    }
    sectionIds.add(section.id);
  }

  const pages = raw.pages.map((page, idx) => {
    if (!page || typeof page !== "object") {
      throw new Error(`[build-docs] pages[${idx}] must be an object`);
    }
    if (!page.href || !page.label || !page.section) {
      throw new Error(`[build-docs] pages[${idx}] must define href, label, and section`);
    }
    if (!sectionIds.has(page.section)) {
      throw new Error(`[build-docs] pages[${idx}] references unknown section ${page.section}`);
    }
    validateDocMetadata(page, `pages[${idx}]`);
    return {
      href: String(page.href),
      label: String(page.label),
      section: String(page.section),
      nav_order: Number.isFinite(page.nav_order) ? page.nav_order : (idx + 1) * 10,
      summary: typeof page.summary === "string" && page.summary.trim() ? page.summary.trim() : "",
      audience: page.audience,
      doc_type: page.doc_type,
      product_area: typeof page.product_area === "string" ? page.product_area : "",
      prerequisites: page.prerequisites,
      stability: page.stability,
      reviewed_on: typeof page.reviewed_on === "string" ? page.reviewed_on : "",
      version_applicability:
        typeof page.version_applicability === "string" ? page.version_applicability : "",
      tags: page.tags,
      nav_hidden: Boolean(page.nav_hidden),
      newtab: Boolean(page.newtab),
    };
  });

  const hrefs = new Set();
  for (const page of pages) {
    if (hrefs.has(page.href)) {
      throw new Error(`[build-docs] duplicate page href ${page.href} in docs-site-map.json`);
    }
    hrefs.add(page.href);
  }

  return { defaults, sections, pages };
}

const SITE_MAP = loadSiteMap();
const SITE_SECTION_BY_ID = new Map(
  SITE_MAP.sections.map((section) => [section.id, section])
);
const SITE_PAGE_BY_HREF = new Map(
  SITE_MAP.pages.map((page) => [page.href, page])
);

// ---------------------------------------------------------------------------
// Small helpers
// ---------------------------------------------------------------------------

function escapeHtml(s) {
  return s
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;");
}

function escapeAttr(s) {
  return escapeHtml(s).replace(/"/g, "&quot;");
}

function slugify(text) {
  return text
    .toLowerCase()
    .replace(/`/g, "")
    .replace(/[^\w\s-]/g, "")
    .trim()
    .replace(/\s+/g, "-")
    .replace(/-+/g, "-");
}

function leadingSpaces(line) {
  const m = line.match(/^(\s*)/);
  return m ? m[1].replace(/\t/g, "    ").length : 0;
}

function humanizeSlug(value) {
  return String(value || "")
    .split(/[-_]/)
    .filter(Boolean)
    .map((part) => part.charAt(0).toUpperCase() + part.slice(1))
    .join(" ");
}

function parseFrontMatterScalar(raw) {
  const value = String(raw || "").trim();
  if (!value) return "";
  if (
    (value.startsWith('"') && value.endsWith('"')) ||
    (value.startsWith("'") && value.endsWith("'"))
  ) {
    return value.slice(1, -1);
  }
  if (value === "true") return true;
  if (value === "false") return false;
  if (/^-?\d+$/.test(value)) return Number(value);
  return value;
}

function parseFrontMatter(markdown) {
  const text = markdown.replace(/^\uFEFF/, "");
  if (!text.startsWith("---\n")) {
    return { metadata: {}, body: markdown };
  }
  const lines = text.replace(/\r\n/g, "\n").split("\n");
  const end = lines.indexOf("---", 1);
  if (end <= 0) {
    return { metadata: {}, body: markdown };
  }
  const metadata = {};
  let currentKey = null;
  for (let i = 1; i < end; i++) {
    const line = lines[i];
    if (!line.trim()) continue;
    const listItem = line.match(/^\s*-\s+(.*)$/);
    if (listItem && currentKey) {
      if (!Array.isArray(metadata[currentKey])) metadata[currentKey] = [];
      metadata[currentKey].push(parseFrontMatterScalar(listItem[1]));
      continue;
    }
    const match = line.match(/^([A-Za-z0-9_]+):(?:\s*(.*))?$/);
    if (!match) {
      throw new Error(`[build-docs] unsupported front matter line "${line}"`);
    }
    const [, key, rawValue] = match;
    currentKey = key;
    if (rawValue === undefined || rawValue === "") {
      metadata[key] = [];
      continue;
    }
    metadata[key] = parseFrontMatterScalar(rawValue);
  }
  validateDocMetadata(metadata, "front matter");
  return { metadata, body: lines.slice(end + 1).join("\n") };
}

function normalizeArray(value) {
  if (value === undefined || value === null || value === "") return [];
  if (Array.isArray(value)) return value.map((item) => String(item).trim()).filter(Boolean);
  return [String(value).trim()].filter(Boolean);
}

function mergePageMetadata(...layers) {
  const merged = {};
  for (const layer of layers) {
    if (!layer || typeof layer !== "object") continue;
    for (const [key, value] of Object.entries(layer)) {
      if (value === undefined || value === null || value === "") continue;
      if ((key === "audience" || key === "prerequisites" || key === "tags")) {
        const items = normalizeArray(value);
        if (items.length) merged[key] = items;
        continue;
      }
      merged[key] = value;
    }
  }
  return merged;
}

function plainTextFromHtml(html) {
  return String(html || "")
    .replace(/<script\b[\s\S]*?<\/script>/gi, " ")
    .replace(/<style\b[\s\S]*?<\/style>/gi, " ")
    .replace(/<[^>]+>/g, " ")
    .replace(/&nbsp;/g, " ")
    .replace(/&amp;/g, "&")
    .replace(/&lt;/g, "<")
    .replace(/&gt;/g, ">")
    .replace(/&quot;/g, '"')
    .replace(/&#39;/g, "'")
    .replace(/\s+/g, " ")
    .trim();
}

function markdownHeadings(markdown) {
  return markdown
    .replace(/\r\n/g, "\n")
    .split("\n")
    .map((line) => line.match(/^#{2,4}\s+(.*)$/))
    .filter(Boolean)
    .map((match) => match[1].trim());
}

/**
 * Remove presentation-only HTML used by the repository landing documents.
 * The dependency-free renderer deliberately does not accept arbitrary raw
 * HTML, and the flattened Markdown must not retain an image path that only
 * makes sense from the repository root. Keep the semantic Markdown inside the
 * wrappers and normalize non-breaking-space entities to ordinary whitespace.
 */
function normalizePublishedMarkdown(markdown, fromMdRel) {
  if (resolve(DOCS_DIR, fromMdRel) !== resolve(REPO_ROOT, "ARCHITECTURE.md")) {
    return markdown;
  }
  const lines = markdown.replace(/\r\n/g, "\n").split("\n");
  const normalized = lines.filter((line) => {
    const trimmed = line.trim();
    if (
      trimmed === '<div align="center">' ||
      trimmed === "</div>" ||
      trimmed === '<div align="center"><sub>' ||
      trimmed === "</sub></div>"
    ) {
      return false;
    }
    return !/^<img\s+src="docs-site\/caliber\.png"[^>]*\/?>$/i.test(trimmed);
  });
  return normalized.join("\n").replaceAll("&nbsp;", " ");
}

/**
 * The provenance banner stamped onto every generated file.
 *
 * Both the `m-*.html` pages and their `m-*.md` siblings are build output, but
 * nothing about the filename says so — an editor opening `m-01-platform.md`
 * sees ordinary prose and no reason not to change it. Those edits are silently
 * reverted by the next build, and the parity gate that catches them reports the
 * failure against the generator, not against the author. Naming the real source
 * in the file itself is what turns that into a one-line fix.
 *
 * `sourceRel` is repository-relative so it is copy-pasteable from any cwd.
 */
function generatedBanner(sourceRel) {
  return (
    `<!-- Generated by docs-site/build-docs.mjs from ${sourceRel}. ` +
    "Do not edit this file: edit the source and re-run the build " +
    "(caliber/caliber-ui: npm run sync:docs). -->"
  );
}

/** Repository-relative path of a module's Markdown source. */
function moduleSourceRel(mod) {
  return relative(REPO_ROOT, resolve(DOCS_DIR, mod.md)).replaceAll("\\", "/");
}

function sectionForPage(pageConfig) {
  const section = pageConfig ? SITE_SECTION_BY_ID.get(pageConfig.section) : null;
  if (!section) {
    throw new Error(`[build-docs] page ${pageConfig && pageConfig.href ? pageConfig.href : "(unknown)"} is missing a valid section assignment`);
  }
  return section;
}

function pageConfigForHref(href) {
  const page = SITE_PAGE_BY_HREF.get(href);
  if (!page) {
    throw new Error(`[build-docs] docs-site-map.json has no page entry for ${href}`);
  }
  return page;
}

function pageMetadataForHref(href, overrides = {}) {
  const page = pageConfigForHref(href);
  const section = sectionForPage(page);
  return mergePageMetadata(
    SITE_MAP.defaults,
    section.metadata_defaults,
    page,
    overrides,
  );
}

function titleCaseDocType(docType) {
  return humanizeSlug(docType);
}

function formatAudienceLabel(audience) {
  switch (audience) {
    case "system-user":
      return "System user";
    case "decision-maker":
      return "Decision-maker";
    default:
      return humanizeSlug(audience);
  }
}

function renderMetadataBlock(metadata) {
  const chips = [];
  for (const audience of normalizeArray(metadata.audience)) {
    chips.push(`<span class="doc-meta-chip">${escapeHtml(formatAudienceLabel(audience))}</span>`);
  }
  if (metadata.doc_type) {
    chips.push(`<span class="doc-meta-chip doc-meta-chip-type">${escapeHtml(titleCaseDocType(metadata.doc_type))}</span>`);
  }
  if (metadata.stability) {
    chips.push(`<span class="doc-meta-chip doc-meta-chip-stability">${escapeHtml(String(metadata.stability).toUpperCase())}</span>`);
  }
  const prerequisites = normalizeArray(metadata.prerequisites);
  if (!chips.length && !prerequisites.length && !metadata.reviewed_on && !metadata.version_applicability) {
    return "";
  }
  const notes = [];
  if (metadata.reviewed_on) notes.push(`Reviewed ${escapeHtml(metadata.reviewed_on)}`);
  if (metadata.version_applicability) notes.push(escapeHtml(metadata.version_applicability));
  return `<div class="doc-meta">` +
    (chips.length ? `<div class="doc-meta-chips">${chips.join("")}</div>` : "") +
    (prerequisites.length
      ? `<div class="doc-meta-extra"><span class="doc-meta-label">Prerequisites</span><span class="doc-meta-prereqs">${prerequisites.map((item) => escapeHtml(item)).join(" · ")}</span></div>`
      : "") +
    (notes.length ? `<div class="doc-meta-notes">${notes.join(" · ")}</div>` : "") +
    `</div>`;
}

function loadModuleSource(mod) {
  const srcPath = resolve(DOCS_DIR, mod.md);
  const sourceRel = moduleSourceRel(mod);
  const authored = materializeGeneratedMarkdown(
    normalizePublishedMarkdown(readFileSync(srcPath, "utf8"), mod.md)
  );
  const { metadata, body } = parseFrontMatter(authored);
  const lines = body.replace(/\r\n/g, "\n").split("\n");

  let title = mod.label;
  let startIdx = 0;
  for (let j = 0; j < lines.length; j++) {
    if (/^\s*$/.test(lines[j])) continue;
    const h1 = lines[j].match(/^#\s+(.*)$/);
    if (h1) {
      title = h1[1].trim();
      startIdx = j + 1;
    }
    break;
  }
  const bodyMd = lines.slice(startIdx).join("\n");
  return {
    sourceRel,
    metadata,
    authoredMarkdown: authored,
    bodyMarkdown: bodyMd,
    title,
  };
}

function isThematicBreak(line) {
  return /^\s*(?:-{3,}|\*{3,}|_{3,})\s*$/.test(line);
}

function resolveDocAsset(ref, fromMdRel) {
  const assetPath = resolve(DOCS_DIR, posix.dirname(fromMdRel), ref);
  const rel = relative(DOCS_DIR, assetPath);
  if (!rel || rel === "." || rel.startsWith("..")) {
    throw new Error(`[build-docs] asset ${ref} in ${fromMdRel} resolves outside docs/`);
  }
  if (!existsSync(assetPath)) {
    throw new Error(`[build-docs] missing asset ${ref} referenced from ${fromMdRel}`);
  }
  return assetPath;
}

/**
 * Inline a named Python function from a source file, verbatim.
 *
 * The SDK plan requires documentation snippets to come from tested code rather
 * than hand-written prose. A copied snippet is correct exactly once; this reads
 * the function out of the file the test suite executes, so a signature change
 * either updates the docs or fails the build.
 *
 * Fence body is `path/to/file.py#symbol`, repository-root relative.
 */
function renderPythonExample(ref, fromMdRel) {
  const spec = ref.trim();
  const [relPath, symbol] = spec.split("#");
  if (!relPath || !symbol) {
    throw new Error(`[build-docs] python-example fence in ${fromMdRel} must be "path.py#symbol", got ${spec!==""?spec:"(empty)"}`);
  }
  const filePath = resolve(REPO_ROOT, relPath);
  if (!isWithin(REPO_ROOT, filePath) || !filePath.endsWith(".py")) {
    throw new Error(`[build-docs] python-example ${relPath} in ${fromMdRel} is outside the repository or not a .py file`);
  }
  if (!existsSync(filePath)) {
    throw new Error(`[build-docs] python-example source ${relPath} referenced from ${fromMdRel} does not exist`);
  }
  const lines = readFileSync(filePath, "utf8").replace(/\r\n/g, "\n").split("\n");
  const start = lines.findIndex((line) => new RegExp(`^def ${symbol}\\b`).test(line));
  if (start === -1) {
    throw new Error(`[build-docs] python-example ${relPath} has no top-level def ${symbol} (referenced from ${fromMdRel})`);
  }
  let end = lines.length;
  for (let i = start + 1; i < lines.length; i++) {
    // A top-level statement ends the function body.
    if (lines[i].trim() !== "" && !/^\s/.test(lines[i])) { end = i; break; }
  }
  const body = lines.slice(start, end).join("\n").replace(/\s+$/, "");
  return renderCodeFigure(body, "python", {
    figureClass: "code-example",
    dataAttrs: ` data-example-src="${escapeAttr(spec)}"`,
    caption: `From <code>${escapeHtml(relPath)}</code> \u2014 executed by the SDK test suite.`,
  });
}

function renderPythonFile(ref, fromMdRel) {
  const relPath = ref.trim();
  if (!relPath) {
    throw new Error(`[build-docs] empty python-file fence in ${fromMdRel}`);
  }
  const filePath = resolve(REPO_ROOT, relPath);
  if (!isWithin(REPO_ROOT, filePath) || !filePath.endsWith(".py")) {
    throw new Error(`[build-docs] python-file ${relPath} in ${fromMdRel} is outside the repository or not a .py file`);
  }
  if (!existsSync(filePath)) {
    throw new Error(`[build-docs] python-file source ${relPath} referenced from ${fromMdRel} does not exist`);
  }
  const body = readFileSync(filePath, "utf8").replace(/\r\n/g, "\n").replace(/\s+$/, "");
  return renderCodeFigure(body, "python", {
    figureClass: "code-example",
    dataAttrs: ` data-example-src="${escapeAttr(relPath)}"`,
    caption: `Full source from <code>${escapeHtml(relPath)}</code> \u2014 executed by the SDK example test suite.`,
  });
}

const GENERATED_DOCS = new Map();
const SDK_DOCS_GENERATOR = resolve(here, "generate_sdk_docs.py");
const REST_API_DOCS_GENERATOR = resolve(here, "generate_rest_api_docs.py");

function caliberDocsPython() {
  const repositoryPython = resolve(REPO_ROOT, "caliber/.venv/bin/python");
  return process.env.CALIBER_DOCS_PYTHON ||
    (existsSync(repositoryPython) ? repositoryPython : null);
}

function generatedSdkMarkdown(kind) {
  if (GENERATED_DOCS.has(kind)) return GENERATED_DOCS.get(kind);
  if (!existsSync(SDK_DOCS_GENERATOR)) {
    throw new Error(`[build-docs] required SDK docs generator ${relative(REPO_ROOT, SDK_DOCS_GENERATOR)} not found`);
  }
  const output = execFileSync("python3", ["-B", SDK_DOCS_GENERATOR, kind], {
    encoding: "utf8",
  });
  GENERATED_DOCS.set(kind, output);
  return output;
}

function generatedRestApiMarkdown() {
  const kind = "rest-api-reference";
  if (GENERATED_DOCS.has(kind)) return GENERATED_DOCS.get(kind);
  if (!existsSync(REST_API_DOCS_GENERATOR)) {
    throw new Error(`[build-docs] required REST API docs generator ${relative(REPO_ROOT, REST_API_DOCS_GENERATOR)} not found`);
  }
  const runtimePython = caliberDocsPython();
  if (!runtimePython) {
    throw new Error("[build-docs] CALIBER runtime python is unavailable for REST API docs generation");
  }
  const output = execFileSync(runtimePython, ["-B", REST_API_DOCS_GENERATOR], {
    encoding: "utf8",
    env: {
      ...process.env,
      CALIBER_LOG_LEVEL: process.env.CALIBER_LOG_LEVEL || "ERROR",
    },
  });
  GENERATED_DOCS.set(kind, output);
  return output;
}

function materializeGeneratedMarkdown(markdown) {
  return markdown
    .replace("{{SDK_API_REFERENCE}}", generatedSdkMarkdown("reference").trim())
    .replace("{{SDK_COOKBOOKS}}", generatedSdkMarkdown("cookbooks").trim())
    .replace("{{REST_API_ROUTE_INVENTORY}}", generatedRestApiMarkdown().trim());
}

function codeLanguage(lang) {
  const normalized = String(lang || "").trim().toLowerCase();
  if (normalized === "py") return "python";
  if (normalized === "yml") return "yaml";
  if (!normalized) return "";
  return normalized;
}

function spanToken(kind, text) {
  return `<span class="tok-${kind}">${escapeHtml(text)}</span>`;
}

function renderCodeFigure(code, lang, { figureClass = "code-example", dataAttrs = "", caption = "" } = {}) {
  const language = codeLanguage(lang);
  const block = renderCodeBlock(code, language);
  return `<figure class="${figureClass}"${dataAttrs}>` +
    `${block}` +
    (caption ? `<figcaption>${caption}</figcaption>` : "") +
    `</figure>`;
}

function renderCodeBlock(code, lang = "") {
  const language = codeLanguage(lang);
  const cls = language ? ` class="language-${language}"` : "";
  return `<pre${cls}><code${cls}>${highlightCode(code, language)}</code></pre>`;
}

function highlightCode(code, lang = "") {
  switch (codeLanguage(lang)) {
    case "python":
      return highlightPython(code);
    case "json":
      return highlightJson(code);
    case "yaml":
      return highlightYaml(code);
    default:
      return escapeHtml(code);
  }
}

function isWordChar(ch) {
  return /[A-Za-z0-9_]/.test(ch);
}

function readQuotedString(code, start, { allowPrefixes = false } = {}) {
  let prefixEnd = start;
  if (allowPrefixes) {
    while (
      prefixEnd < code.length &&
      /[rRbBuUfF]/.test(code[prefixEnd]) &&
      prefixEnd - start < 3
    ) {
      prefixEnd++;
    }
  }
  const quote = code[prefixEnd];
  if (quote !== "'" && quote !== "\"") return null;
  if (allowPrefixes && start > 0 && isWordChar(code[start - 1])) return null;
  const triple = code.slice(prefixEnd, prefixEnd + 3) === quote.repeat(3);
  let end = prefixEnd + (triple ? 3 : 1);
  while (end < code.length) {
    if (!triple && code[end] === "\\") {
      end += 2;
      continue;
    }
    if (triple) {
      if (code.slice(end, end + 3) === quote.repeat(3)) {
        end += 3;
        return { text: code.slice(start, end), end };
      }
      end += 1;
      continue;
    }
    if (code[end] === quote) {
      end += 1;
      return { text: code.slice(start, end), end };
    }
    end += 1;
  }
  return { text: code.slice(start), end: code.length };
}

const PYTHON_KEYWORDS = new Set([
  "and", "as", "assert", "async", "await", "break", "case", "class", "continue", "def",
  "del", "elif", "else", "except", "finally", "for", "from", "global", "if", "import", "in",
  "is", "lambda", "match", "nonlocal", "not", "or", "pass", "raise", "return", "try", "while",
  "with", "yield",
]);
const PYTHON_BOOLEAN_LITERALS = new Set(["True", "False"]);

function highlightPython(code) {
  let out = "";
  let i = 0;
  let expectingName = "";
  while (i < code.length) {
    const ch = code[i];
    const stringToken = readQuotedString(code, i, { allowPrefixes: true });
    if (stringToken) {
      out += spanToken("string", stringToken.text);
      i = stringToken.end;
      expectingName = "";
      continue;
    }
    if (ch === "#") {
      let end = i;
      while (end < code.length && code[end] !== "\n") end++;
      out += spanToken("comment", code.slice(i, end));
      i = end;
      expectingName = "";
      continue;
    }
    if (ch === "@" && (i === 0 || code[i - 1] === "\n")) {
      let end = i + 1;
      while (end < code.length && /[A-Za-z0-9_\.]/.test(code[end])) end++;
      out += spanToken("decorator", code.slice(i, end));
      i = end;
      expectingName = "";
      continue;
    }
    if (/[A-Za-z_]/.test(ch)) {
      let end = i + 1;
      while (end < code.length && /[A-Za-z0-9_]/.test(code[end])) end++;
      const word = code.slice(i, end);
      if (word === "None") {
        out += spanToken("null", word);
        expectingName = "";
      } else if (PYTHON_BOOLEAN_LITERALS.has(word)) {
        out += spanToken("boolean", word);
        expectingName = "";
      } else if (PYTHON_KEYWORDS.has(word)) {
        out += spanToken("keyword", word);
        expectingName = word === "def" || word === "class" ? word : "";
      } else if (expectingName === "def") {
        out += spanToken("function", word);
        expectingName = "";
      } else if (expectingName === "class") {
        out += spanToken("class", word);
        expectingName = "";
      } else {
        let lookahead = end;
        while (lookahead < code.length && /\s/.test(code[lookahead])) lookahead++;
        if (code[lookahead] === "(") out += spanToken("function", word);
        else out += escapeHtml(word);
        expectingName = "";
      }
      i = end;
      continue;
    }
    if (/[0-9]/.test(ch)) {
      let end = i + 1;
      while (end < code.length && /[0-9_\.eE\+\-]/.test(code[end])) end++;
      out += spanToken("number", code.slice(i, end));
      i = end;
      expectingName = "";
      continue;
    }
    if ("()[]{}:,.".includes(ch)) {
      out += spanToken("punctuation", ch);
      expectingName = ch === "." ? expectingName : "";
      i++;
      continue;
    }
    if ("=+-*/%<>!&|^~".includes(ch)) {
      out += spanToken("operator", ch);
      expectingName = "";
      i++;
      continue;
    }
    out += escapeHtml(ch);
    if (!/\s/.test(ch)) expectingName = "";
    i++;
  }
  return out;
}

function highlightJson(code) {
  let out = "";
  let i = 0;
  while (i < code.length) {
    const ch = code[i];
    const stringToken = readQuotedString(code, i);
    if (stringToken) {
      let lookahead = stringToken.end;
      while (lookahead < code.length && /\s/.test(code[lookahead])) lookahead++;
      out += spanToken(lookahead < code.length && code[lookahead] === ":" ? "key" : "string", stringToken.text);
      i = stringToken.end;
      continue;
    }
    if (/[0-9\-]/.test(ch)) {
      let end = i + 1;
      while (end < code.length && /[0-9eE\+\-\.]/.test(code[end])) end++;
      out += spanToken("number", code.slice(i, end));
      i = end;
      continue;
    }
    if (code.startsWith("true", i) || code.startsWith("false", i)) {
      const literal = code.startsWith("true", i) ? "true" : "false";
      out += spanToken("boolean", literal);
      i += literal.length;
      continue;
    }
    if (code.startsWith("null", i)) {
      out += spanToken("null", "null");
      i += 4;
      continue;
    }
    if ("{}[]:,".includes(ch)) {
      out += spanToken("punctuation", ch);
      i++;
      continue;
    }
    out += escapeHtml(ch);
    i++;
  }
  return out;
}

function splitYamlComment(line) {
  let quote = "";
  for (let i = 0; i < line.length; i++) {
    const ch = line[i];
    if (!quote && (ch === "'" || ch === "\"")) {
      quote = ch;
      continue;
    }
    if (quote) {
      if (ch === "\\" && quote === "\"") {
        i++;
        continue;
      }
      if (ch === quote) {
        quote = "";
      }
      continue;
    }
    if (ch === "#") {
      return { body: line.slice(0, i), comment: line.slice(i) };
    }
  }
  return { body: line, comment: "" };
}

function highlightYamlValue(text) {
  let out = "";
  let i = 0;
  while (i < text.length) {
    const ch = text[i];
    const stringToken = readQuotedString(text, i);
    if (stringToken) {
      out += spanToken("string", stringToken.text);
      i = stringToken.end;
      continue;
    }
    if (/[0-9\-]/.test(ch)) {
      let end = i + 1;
      while (end < text.length && /[0-9_\.]/.test(text[end])) end++;
      out += spanToken("number", text.slice(i, end));
      i = end;
      continue;
    }
    if (/^[A-Za-z_]/.test(ch)) {
      let end = i + 1;
      while (end < text.length && /[A-Za-z0-9_\-]/.test(text[end])) end++;
      const word = text.slice(i, end);
      if (["true", "false", "yes", "no", "on", "off"].includes(word.toLowerCase())) {
        out += spanToken("boolean", word);
      } else if (["null", "~"].includes(word.toLowerCase())) {
        out += spanToken("null", word);
      } else {
        out += escapeHtml(word);
      }
      i = end;
      continue;
    }
    if ("[]{}:,-".includes(ch)) {
      out += spanToken("punctuation", ch);
      i++;
      continue;
    }
    out += escapeHtml(ch);
    i++;
  }
  return out;
}

function highlightYaml(code) {
  return code.split("\n").map((line) => {
    const { body, comment } = splitYamlComment(line);
    const match = body.match(/^(\s*-\s+)?([A-Za-z0-9_.-]+)(\s*:)(.*)$/);
    let rendered;
    if (match) {
      rendered =
        escapeHtml(match[1] || "") +
        spanToken("key", match[2]) +
        spanToken("punctuation", match[3]) +
        highlightYamlValue(match[4] || "");
    } else {
      rendered = highlightYamlValue(body);
    }
    if (comment) rendered += spanToken("comment", comment);
    return rendered;
  }).join("\n");
}

function renderSvgDiagramAsset(ref, fromMdRel) {
  const assetRef = ref.trim();
  if (!assetRef) {
    throw new Error(`[build-docs] empty diagram-svg fence in ${fromMdRel}`);
  }
  const assetPath = resolveDocAsset(assetRef, fromMdRel);
  const raw = readFileSync(assetPath, "utf8").replace(/^\uFEFF/, "");
  const match = raw.match(/<svg\b[\s\S]*<\/svg>/i);
  if (!match) {
    throw new Error(`[build-docs] diagram-svg asset ${assetRef} in ${fromMdRel} does not contain a root <svg>`);
  }
  const svg = match[0].trim();
  if (/<script\b/i.test(svg) || /\son[a-z]+\s*=/i.test(svg)) {
    throw new Error(`[build-docs] diagram-svg asset ${assetRef} in ${fromMdRel} must be a static SVG`);
  }
  return `<figure class="diagram diagram-svg" data-diagram-src="${escapeAttr(assetRef)}">${svg}</figure>`;
}

// ---------------------------------------------------------------------------
// Link resolution — cross-doc refs become page links; source files become code.
// ---------------------------------------------------------------------------

function classifyLink(href, fromMdRel) {
  const [path, ...hashParts] = href.split("#");
  const hash = hashParts.length ? "#" + hashParts.join("#") : "";

  if (/^https?:\/\//.test(href) || href.startsWith("mailto:")) {
    return { kind: "external", href };
  }
  if (path === "" && hash) {
    return { kind: "anchor", href: hash };
  }

  const fromSource = resolve(DOCS_DIR, fromMdRel);
  const target = resolve(dirname(fromSource), path);

  const sourceModule = sourceToModule.get(target);
  if (sourceModule) {
    return {
      kind: "page",
      href: sourceModule.out + hash,
      markdownHref: sourceModule.out.replace(/\.html$/, ".md") + hash,
    };
  }

  // The repository-level architecture links to already-published docs-site
  // pages. Keep those links inside the flat site, preferring the raw Markdown
  // sibling for generated modules and HTML for hand-authored views.
  const outputModule = outputToModule.get(target);
  if (outputModule) {
    return {
      kind: "page",
      href: outputModule.out + hash,
      markdownHref: outputModule.out.replace(/\.html$/, ".md") + hash,
    };
  }
  const siteRel = relative(OUT_DIR, target);
  const insideSite =
    siteRel === "" ||
    (siteRel !== ".." && !siteRel.startsWith("../") && !siteRel.startsWith("..\\"));
  if (insideSite && existsSync(target)) {
    const relativeHref = statSync(target).isDirectory()
      ? `${siteRel ? siteRel.replaceAll("\\", "/") + "/" : ""}index.html`
      : siteRel.replaceAll("\\", "/");
    return {
      kind: "page",
      href: relativeHref + hash,
      markdownHref: relativeHref + hash,
    };
  }

  // HTML pages intentionally render source-code references as labels rather
  // than navigation out of the docs shell. The machine-readable Markdown keeps
  // those references useful by pointing to stable repository blob/tree URLs.
  const repoRel = relative(REPO_ROOT, target);
  const insideRepo =
    repoRel !== "" &&
    repoRel !== ".." &&
    !repoRel.startsWith("../") &&
    !repoRel.startsWith("..\\");
  if (insideRepo && existsSync(target)) {
    const sourceKind = statSync(target).isDirectory() ? "tree" : "blob";
    const encodedPath = repoRel
      .replaceAll("\\", "/")
      .split("/")
      .map(encodeURIComponent)
      .join("/");
    return {
      kind: "code",
      markdownHref: `${GITHUB_SOURCE_BASE}/${sourceKind}/main/${encodedPath}${hash}`,
    };
  }

  // Source files and any other repo-relative path: show as code, not a link.
  return { kind: "code" };
}

/**
 * Rewrite links between source documentation modules for the flattened raw
 * Markdown copies served beside the generated HTML pages.
 *
 * The source tree is nested (for example
 * `14-evaluation/architecture.md -> ../11-test-sets/architecture.md`), while
 * the published Markdown files are siblings (`m-14-evaluation.md ->
 * m-11-test-sets.md`). Copying source bytes verbatim therefore leaves links in
 * the machine-readable docs broken. Reuse the same manifest-aware resolution
 * as the HTML renderer and preserve anchors, angle brackets, and optional
 * titles. Published docs-site pages stay local; other repository references
 * become stable GitHub blob/tree URLs instead of broken flat-site paths.
 */
function rewriteMarkdownCrossReferences(markdown, fromMdRel) {
  return markdown.replace(/(!?\[[^\]\n]*\]\()([^\n)]*)(\))/g, (whole, prefix, rawDestination, suffix) => {
    const leading = rawDestination.match(/^\s*/)?.[0] ?? "";
    const body = rawDestination.slice(leading.length);
    if (!body) return whole;

    let href;
    let remainder;
    let angleWrapped = false;
    if (body.startsWith("<")) {
      const close = body.indexOf(">");
      if (close < 0) return whole;
      href = body.slice(1, close);
      remainder = body.slice(close + 1);
      angleWrapped = true;
    } else {
      const match = body.match(/^(\S+)([\s\S]*)$/);
      if (!match) return whole;
      [, href, remainder] = match;
    }

    const info = classifyLink(href, fromMdRel);
    if (!info.markdownHref) return whole;
    const rewritten = info.markdownHref;
    const destination = angleWrapped ? `<${rewritten}>` : rewritten;
    return `${prefix}${leading}${destination}${remainder}${suffix}`;
  });
}

// ---------------------------------------------------------------------------
// Inline rendering (code spans, escaping, links, bold, italic)
// ---------------------------------------------------------------------------

function renderInline(text, fromMdRel) {
  const codeSpans = [];
  // 1. Pull out inline code first so nothing inside it is reinterpreted. The
  //    @@C<n>@@ sentinel survives HTML escaping and the emphasis passes and
  //    adds no whitespace around code that abuts punctuation, e.g. `server.py`.
  let work = text.replace(/`([^`]+)`/g, (_, code) => {
    const i = codeSpans.length;
    codeSpans.push(`<code>${escapeHtml(code)}</code>`);
    return `@@C${i}@@`;
  });

  // 2. Escape the rest of the HTML-significant characters.
  work = escapeHtml(work);

  // 3. Bold then italic (asterisk form only — underscores appear in identifiers).
  work = work.replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>");
  work = work.replace(/\*([^*\n]+)\*/g, "<em>$1</em>");

  // 4. Links: [label](href). The label is already escaped + emphasized; a link
  //    into a source file is downgraded to its (often code-formatted) label so
  //    the docs never carry a broken hyperlink.
  work = work.replace(/\[([^\]]+)\]\(([^)]+)\)/g, (_, label, rawHref) => {
    const info = classifyLink(rawHref.trim(), fromMdRel);
    if (info.kind === "code") return label;
    if (info.kind === "external") {
      return `<a href="${escapeAttr(info.href)}" target="_blank" rel="noopener">${label}</a>`;
    }
    return `<a href="${escapeAttr(info.href)}">${label}</a>`;
  });

  // 5. Restore code spans.
  work = work.replace(/@@C(\d+)@@/g, (_, n) => codeSpans[Number(n)]);
  return work;
}

// The diagram color key. Emitted by an empty ```legend``` fence. Dot classes
// (.legend-dot.user/.ui/.ctrl/.store/.ext/.async) are styled in docs.css and
// mirror the semantic node classDefs docs.js injects into flowcharts.
const LEGEND_HTML = `<div class="diagram-legend" role="note" aria-label="Diagram color key">
  <span class="legend-item"><span class="legend-dot user"></span>Actor</span>
  <span class="legend-item"><span class="legend-dot ui"></span>UI / SPA</span>
  <span class="legend-item"><span class="legend-dot ctrl"></span>Control plane</span>
  <span class="legend-item"><span class="legend-dot store"></span>Storage</span>
  <span class="legend-item"><span class="legend-dot ext"></span>External</span>
  <span class="legend-item"><span class="legend-dot async"></span>Async worker</span>
</div>`;

// ---------------------------------------------------------------------------
// Block rendering
// ---------------------------------------------------------------------------

function splitTableRow(row) {
  const PIPE = "\u0001";
  let s = row.trim().replace(/\\\|/g, PIPE); // protect escaped pipes
  if (s.startsWith("|")) s = s.slice(1);
  if (s.endsWith("|")) s = s.slice(0, -1);
  return s.split("|").map((c) => c.split(PIPE).join("|").trim());
}

function isTableSeparator(line) {
  return (
    line.includes("-") &&
    /^\s*\|?[\s:|-]+\|?\s*$/.test(line) &&
    line.includes("|")
  );
}

function renderTable(headerCells, alignRow, rows, fromMdRel) {
  const aligns = alignRow.map((c) => {
    const left = c.startsWith(":");
    const right = c.endsWith(":");
    if (left && right) return "center";
    if (right) return "right";
    if (left) return "left";
    return "";
  });
  const th = headerCells
    .map((c, idx) => {
      const a = aligns[idx] ? ` style="text-align:${aligns[idx]}"` : "";
      return `<th${a}>${renderInline(c, fromMdRel)}</th>`;
    })
    .join("");
  const body = rows
    .map((cells) => {
      const tds = cells
        .map((c, idx) => {
          const a = aligns[idx] ? ` style="text-align:${aligns[idx]}"` : "";
          return `<td${a}>${renderInline(c, fromMdRel)}</td>`;
        })
        .join("");
      return `<tr>${tds}</tr>`;
    })
    .join("");
  return `<div class="table-wrap"><table><thead><tr>${th}</tr></thead><tbody>${body}</tbody></table></div>`;
}

// Parse a list (ordered or unordered) starting at `start`. Handles wrapped
// continuation lines and nested sublists via indentation. Returns rendered HTML
// and the index of the first line after the list.
function parseList(lines, start, fromMdRel) {
  const firstIndent = leadingSpaces(lines[start]);
  const ordered = /^\s*\d+\.\s/.test(lines[start]);
  const tag = ordered ? "ol" : "ul";
  const items = [];
  let i = start;

  while (i < lines.length) {
    const line = lines[i];
    if (/^\s*$/.test(line)) {
      const next = lines[i + 1];
      if (next && !/^\s*$/.test(next) && leadingSpaces(next) > firstIndent) {
        i++; // blank line inside an item's nested block
        continue;
      }
      break;
    }
    const indent = leadingSpaces(line);
    const m = line.match(/^(\s*)([-*+]|\d+\.)\s+(.*)$/);
    if (!m || indent !== firstIndent) break;

    let content = m[3];
    i++;
    const childLines = [];
    while (i < lines.length) {
      const l = lines[i];
      if (/^\s*$/.test(l)) {
        const n = lines[i + 1];
        if (n && !/^\s*$/.test(n) && leadingSpaces(n) > firstIndent) {
          childLines.push("");
          i++;
          continue;
        }
        break;
      }
      if (leadingSpaces(l) <= firstIndent) break;
      childLines.push(l.slice(firstIndent));
      i++;
    }
    items.push({ content, childLines });
  }

  const lis = items
    .map((it) => {
      // Leading non-marker child lines are wrapped continuations of the item.
      let k = 0;
      const cont = [];
      while (
        k < it.childLines.length &&
        it.childLines[k] !== "" &&
        !/^\s*([-*+]|\d+\.)\s+/.test(it.childLines[k])
      ) {
        cont.push(it.childLines[k].trim());
        k++;
      }
      const merged = (it.content + " " + cont.join(" ")).trim();
      let inner = renderInline(merged, fromMdRel);
      const rest = it.childLines.slice(k);
      if (rest.some((l) => l.trim() !== "")) {
        inner += renderBlocks(rest.join("\n"), fromMdRel);
      }
      return `<li>${inner}</li>`;
    })
    .join("");

  return { html: `<${tag}>${lis}</${tag}>`, next: i };
}

function renderBlocks(md, fromMdRel) {
  const lines = md.replace(/\r\n/g, "\n").split("\n");
  const out = [];
  let i = 0;

  while (i < lines.length) {
    const line = lines[i];

    if (/^\s*$/.test(line)) {
      i++;
      continue;
    }

    // Markdown thematic break. Handle this before lists so `---` is not
    // rendered as a literal paragraph in architecture and strategy pages.
    if (isThematicBreak(line)) {
      out.push("<hr>");
      i++;
      continue;
    }

    // Fenced code block
    const fence = line.match(/^\s*```\s*([\w-]*)\s*$/);
    if (fence) {
      const lang = fence[1].trim().toLowerCase();
      i++;
      const buf = [];
      while (i < lines.length && !/^\s*```\s*$/.test(lines[i])) {
        buf.push(lines[i]);
        i++;
      }
      i++; // closing fence
      const code = buf.join("\n");
      if (lang === "mermaid") {
        // Authors sometimes write a literal `\n` for a node-label line break, but
        // Mermaid expects `<br/>` (a bare `\n` collapses the label onto one line).
        // Normalize before escaping; docs.js restores the escaped tag via
        // textContent at render time so Mermaid receives a real `<br/>`.
        const diagram = code.replace(/ *\\n */g, "<br/>");
        out.push(`<div class="diagram"><pre class="mermaid">${escapeHtml(diagram)}</pre></div>`);
      } else if (lang === "diagram-svg") {
        // Presentation-grade diagrams live as checked-in SVG assets near the
        // doc that uses them. We inline the exported SVG so theme variables from
        // docs.css can restyle it live without adding a new asset pipeline.
        out.push(renderSvgDiagramAsset(code, fromMdRel));
      } else if (lang === "python-example") {
        // Snippet pulled from the file the tests run, not retyped here.
        out.push(renderPythonExample(code, fromMdRel));
      } else if (lang === "python-file") {
        // Full Python source file, used for complete runnable cookbook examples.
        out.push(renderPythonFile(code, fromMdRel));
      } else if (lang === "legend") {
        // An empty ```legend``` fence renders the shared diagram color key. The
        // dot colors are defined (theme-aware) in docs.css and mirror the
        // semantic classDefs docs.js injects into flowcharts.
        out.push(LEGEND_HTML);
      } else {
        out.push(renderCodeBlock(code, lang));
      }
      continue;
    }

    // Heading
    const h = line.match(/^(#{1,6})\s+(.*)$/);
    if (h) {
      const level = h[1].length;
      const raw = h[2].trim().replace(/\s+#+\s*$/, "");
      const id = slugify(raw);
      out.push(
        `<h${level} id="${id}">${renderInline(raw, fromMdRel)}` +
          `<a class="anchor" href="#${id}" aria-hidden="true" tabindex="-1">#</a></h${level}>`
      );
      i++;
      continue;
    }

    // Blockquote (may contain block content, e.g. a table)
    if (/^\s*>/.test(line)) {
      const buf = [];
      while (i < lines.length && /^\s*>/.test(lines[i])) {
        buf.push(lines[i].replace(/^\s*>\s?/, ""));
        i++;
      }
      out.push(`<blockquote class="callout">${renderBlocks(buf.join("\n"), fromMdRel)}</blockquote>`);
      continue;
    }

    // GFM table
    if (
      line.includes("|") &&
      i + 1 < lines.length &&
      isTableSeparator(lines[i + 1])
    ) {
      const headerCells = splitTableRow(line);
      const alignRow = splitTableRow(lines[i + 1]);
      i += 2;
      const rows = [];
      while (i < lines.length && lines[i].includes("|") && lines[i].trim() !== "") {
        rows.push(splitTableRow(lines[i]));
        i++;
      }
      out.push(renderTable(headerCells, alignRow, rows, fromMdRel));
      continue;
    }

    // List
    if (/^\s*([-*+]|\d+\.)\s+/.test(line)) {
      const { html, next } = parseList(lines, i, fromMdRel);
      out.push(html);
      i = next;
      continue;
    }

    // Paragraph — gather consecutive plain lines.
    const buf = [line];
    i++;
    while (
      i < lines.length &&
      !/^\s*$/.test(lines[i]) &&
      !isThematicBreak(lines[i]) &&
      !/^(#{1,6})\s/.test(lines[i]) &&
      !/^\s*```/.test(lines[i]) &&
      !/^\s*>/.test(lines[i]) &&
      !/^\s*([-*+]|\d+\.)\s+/.test(lines[i]) &&
      !(lines[i].includes("|") && i + 1 < lines.length && isTableSeparator(lines[i + 1]))
    ) {
      buf.push(lines[i]);
      i++;
    }
    out.push(`<p>${renderInline(buf.join(" "), fromMdRel)}</p>`);
  }

  return out.join("\n");
}

// ---------------------------------------------------------------------------
// Reference tier — split each page into an Overview tier (everything above the
// `## Reference` heading) and a banded, progressively-disclosed Reference tier.
// This runs on the already-rendered body HTML so the core markdown parser stays
// untouched. If a doc has no `## Reference` marker, the body is returned as-is.
// ---------------------------------------------------------------------------

function applyReferenceTier(html) {
  const marker = html.search(/<h2 id="reference">/);
  if (marker === -1) return html;

  const before = html.slice(0, marker);
  const rest = html.slice(marker);

  // The marker heading itself becomes a banded tier header; preserve its id and
  // visible text so the TOC entry and #reference anchor keep working.
  const head = rest.match(/^<h2 id="reference">([\s\S]*?)(<a class="anchor"[\s\S]*?)?<\/h2>/);
  const title = head ? head[1].trim() : "Reference";
  const afterHeadIdx = rest.indexOf("</h2>") + "</h2>".length;
  const afterRef = rest.slice(afterHeadIdx);

  const banner =
    `<div class="ref-tier-header">` +
    `<span class="ref-tier-eyebrow">Deep reference · data models, APIs &amp; lifecycle</span>` +
    `<h2 id="reference" class="ref-tier-title">${title}` +
    `<a class="anchor" href="#reference" aria-hidden="true" tabindex="-1">#</a></h2>` +
    `</div>`;

  // Wrap each `##` section below the marker in a default-open <details> so the
  // deep tier is skimmable but collapsible. Content before the first such
  // heading (a lead paragraph under `## Reference`) renders outside the panels.
  let wrapped = "";
  for (const part of afterRef.split(/(?=<h2 )/)) {
    if (!part.trim()) continue;
    if (part.startsWith("<h2 ")) {
      const e = part.indexOf("</h2>") + "</h2>".length;
      const summary = part.slice(0, e);
      const body = part.slice(e).trim();
      wrapped +=
        `<details class="ref-section" open>` +
        `<summary class="ref-section-summary">${summary}</summary>` +
        `<div class="ref-section-body">${body}</div></details>`;
    } else {
      wrapped += `<div class="ref-tier-lead">${part}</div>`;
    }
  }

  return `${before}<section class="ref-tier">${banner}${wrapped}</section>`;
}

// ---------------------------------------------------------------------------
// Page assembly
// ---------------------------------------------------------------------------

const THEME_BOOT = `<script>
    (function () {
      try {
        var t = localStorage.getItem("caliber-docs-theme");
        if (t !== "light" && t !== "dark") {
          t = window.matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light";
        }
        document.documentElement.dataset.theme = t;
      } catch (e) {
        document.documentElement.dataset.theme = "light";
      }
    })();
  </script>`;

function topbar() {
  return `<header class="topbar">
    <div class="topbar-row">
      <a class="topbar-brand" href="index.html" aria-label="${escapeAttr(DOCS_HOME_LABEL)}" title="${escapeAttr(DOCS_HOME_LABEL)}" style="text-decoration:none;color:inherit">
        <strong>${escapeHtml(BRAND_SHORT)}</strong>
      </a>
      <nav class="docs-section-tabs" id="docsSectionTabs" aria-label="Browse documentation sections"></nav>
      <div class="topbar-actions">
        <button type="button" class="topbar-search-trigger" id="topbarSearch" aria-label="Search docs" title="Search docs">
          <svg class="topbar-search-icon" width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><circle cx="11" cy="11" r="7" /><line x1="21" y1="21" x2="16.65" y2="16.65" /></svg>
          <span class="topbar-search-label">Search docs</span>
          <span class="keycap" data-doc-search-key>⌘K</span>
        </button>
        <a class="topbar-cta" href="index.html#playbook-build">Quickstart</a>
        <button type="button" class="theme-toggle" id="themeToggle" aria-label="Toggle dark mode" title="Toggle light / dark theme">
          <svg class="icon-moon" width="17" height="17" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M21 12.79A9 9 0 1 1 11.21 3 7 7 0 0 0 21 12.79z" /></svg>
          <svg class="icon-sun" width="17" height="17" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="5" /><line x1="12" y1="1" x2="12" y2="3" /><line x1="12" y1="21" x2="12" y2="23" /><line x1="4.22" y1="4.22" x2="5.64" y2="5.64" /><line x1="18.36" y1="18.36" x2="19.78" y2="19.78" /><line x1="1" y1="12" x2="3" y2="12" /><line x1="21" y1="12" x2="23" y2="12" /><line x1="4.22" y1="19.78" x2="5.64" y2="18.36" /><line x1="18.36" y1="5.64" x2="19.78" y2="4.22" /></svg>
        </button>
      </div>
    </div>
  </header>`;
}

function pageHtml({ title, summary, groupTitle, label, bodyHtml, sourceRel }) {
  return `<!DOCTYPE html>
${generatedBanner(sourceRel)}
<html lang="en">

<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>${escapeHtml(title)} | ${escapeHtml(BRAND_FULL)}</title>
  <meta name="description" content="${escapeAttr(summary || `${title} — part of the ${BRAND_FULL} documentation.`)}">
  <link rel="icon" type="image/png" href="caliber-icon.png">
  ${THEME_BOOT}
  <link rel="preconnect" href="https://fonts.googleapis.com">
  <link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
  <link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700;800&family=JetBrains+Mono:wght@400;500&display=swap" rel="stylesheet">
  <link rel="stylesheet" href="docs.css">
  <script src="https://cdn.jsdelivr.net/npm/mermaid@11/dist/mermaid.min.js"></script>
</head>

<body data-doc-page="module">
  <a class="skip-link" href="#docsContent">Skip to content</a>
  ${topbar()}

  <button type="button" class="menu-toggle" id="menuToggle" aria-label="Toggle menu" aria-controls="docsSidebar" aria-expanded="false">
    <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
      <line x1="3" y1="6" x2="21" y2="6" />
      <line x1="3" y1="12" x2="21" y2="12" />
      <line x1="3" y1="18" x2="21" y2="18" />
    </svg>
  </button>

  <div class="layout">
    <aside class="sidebar" id="docsSidebar">
      <a class="sidebar-brand" href="index.html" aria-label="${escapeAttr(DOCS_HOME_LABEL)}" title="${escapeAttr(DOCS_HOME_LABEL)}" style="text-decoration:none;color:inherit">
        <img src="caliber-icon.png" alt="${escapeAttr(BRAND_SHORT)} logo">
        <div class="sidebar-brand-title">${escapeHtml(BRAND_SHORT)}</div>
      </a>
      <div class="sidebar-search">
        <input id="nav-filter" type="search" placeholder="Search docs and sections..." aria-label="Search docs and sections">
      </div>
      <nav id="docs-nav" aria-label="Documentation"></nav>
    </aside>

    <main class="main" id="docsContent">
      <article class="content doc-body">
        <nav class="doc-breadcrumb" aria-label="Breadcrumb">
          <span>${escapeHtml(groupTitle)}</span>
          <span aria-hidden="true">/</span>
          <span class="current">${escapeHtml(label)}</span>
        </nav>
        ${bodyHtml}
        <footer>
          <p>
            ${escapeHtml(BRAND_FULL)} —
            this page is generated from the authoritative Markdown sources in <code>docs/</code> and the repository-level <code>ARCHITECTURE.md</code>.
          </p>
        </footer>
      </article>
    </main>

    <aside class="toc" aria-label="On this page">
      <div class="toc-card">
        <div class="toc-title">On this page</div>
        <nav class="toc-links" id="page-toc"></nav>
      </div>
    </aside>
  </div>

  <script defer src="docs-nav.js"></script>
  <script defer src="docs.js"></script>
</body>

</html>
`;
}

// Render one markdown module to a full HTML page.
function renderModule(mod, moduleSource) {
  const page = pageConfigForHref(mod.out);
  const section = sectionForPage(page);
  const metadata = pageMetadataForHref(mod.out, moduleSource.metadata);
  const title = moduleSource.title || page.label || mod.label;
  const bodyMd = moduleSource.bodyMarkdown;
  const groupTitle = section.title;
  const blurb =
    metadata.summary ||
    (typeof mod.blurb === "string" ? mod.blurb.trim() : "");
  const eyebrow = groupTitle;

  const header = `<header class="doc-header">
          <div class="doc-eyebrow">${escapeHtml(eyebrow)}</div>
          <h1 id="top">${escapeHtml(title)}</h1>
          ${blurb ? `<p class="doc-summary">${escapeHtml(blurb)}</p>` : ""}
          ${renderMetadataBlock(metadata)}
          <div class="doc-actions">
            <button type="button" class="doc-copy-button" data-copy-page data-copy-default="Copy page" data-copy-success="Copied" data-copy-failure="Copy failed">
              Copy page
            </button>
          </div>
        </header>`;

  const bodyHtml = header + "\n" + applyReferenceTier(renderBlocks(bodyMd, mod.md));
  return pageHtml({
    title,
    summary: blurb,
    groupTitle,
    label: page.label || mod.label,
    bodyHtml,
    sourceRel: moduleSource.sourceRel,
  });
}

function pageRecordForModule(mod, moduleSource) {
  const page = pageConfigForHref(mod.out);
  const section = sectionForPage(page);
  const metadata = pageMetadataForHref(mod.out, moduleSource.metadata);
  return {
    href: mod.out,
    label: page.label || mod.label,
    sectionId: section.id,
    sectionTitle: section.title,
    sectionSummary: section.summary,
    nav_order: page.nav_order,
    summary: metadata.summary || (typeof mod.blurb === "string" ? mod.blurb.trim() : ""),
    audience: normalizeArray(metadata.audience),
    doc_type: metadata.doc_type || "",
    prerequisites: normalizeArray(metadata.prerequisites),
    stability: metadata.stability || "",
    reviewed_on: metadata.reviewed_on || "",
    version_applicability: metadata.version_applicability || "",
    tags: normalizeArray(metadata.tags),
    newtab: Boolean(page.newtab),
    nav_hidden: Boolean(page.nav_hidden),
    search_hidden: false,
    sourceRel: moduleSource.sourceRel,
    product_area: metadata.product_area || section.id,
  };
}

function manualPageRecords() {
  const manualHrefs = [
    "index.html",
    "interactive-layered-architecture.html",
    "walkthrough.html",
    "presentation.html",
    "presentation_timed.html",
  ];
  return manualHrefs
    .filter((href) => existsSync(resolve(OUT_DIR, href)))
    .map((href) => {
      const page = pageConfigForHref(href);
      const section = sectionForPage(page);
      const metadata = pageMetadataForHref(href);
      return {
        href,
        label: page.label,
        sectionId: section.id,
        sectionTitle: section.title,
        sectionSummary: section.summary,
        nav_order: page.nav_order,
        summary: metadata.summary || "",
        audience: normalizeArray(metadata.audience),
        doc_type: metadata.doc_type || "",
        prerequisites: normalizeArray(metadata.prerequisites),
        stability: metadata.stability || "",
        reviewed_on: metadata.reviewed_on || "",
        version_applicability: metadata.version_applicability || "",
        tags: normalizeArray(metadata.tags),
        newtab: Boolean(page.newtab),
        nav_hidden: Boolean(page.nav_hidden),
        search_hidden: false,
        sourceRel: relative(REPO_ROOT, resolve(OUT_DIR, href)).replaceAll("\\", "/"),
        product_area: metadata.product_area || section.id,
      };
    });
}

function cookbookPageRecords() {
  const section = SITE_SECTION_BY_ID.get("examples");
  if (!section) return [];
  const defaults = mergePageMetadata(SITE_MAP.defaults, section.metadata_defaults);
  const manifest = resolve(OUT_DIR, "cookbooks-nav.json");
  if (!existsSync(manifest)) return [];
  try {
    const parsed = JSON.parse(readFileSync(manifest, "utf8"));
    if (!Array.isArray(parsed)) return [];
    return parsed
      .filter((item) => item && typeof item.href === "string" && existsSync(resolve(OUT_DIR, item.href)))
      .map((item, idx) => ({
        href: item.href,
        label: item.href === "m-16-cookbooks.html" ? "Cookbook gallery" : (item.label || item.href),
        sectionId: section.id,
        sectionTitle: section.title,
        sectionSummary: section.summary,
        nav_order: 20 + idx,
        summary: "",
        audience: normalizeArray(defaults.audience),
        doc_type: defaults.doc_type || "example",
        prerequisites: normalizeArray(defaults.prerequisites),
        stability: defaults.stability || "",
        reviewed_on: defaults.reviewed_on || "",
        version_applicability: defaults.version_applicability || "",
        tags: ["cookbook", "example"],
        newtab: false,
        nav_hidden: false,
        search_hidden: false,
        sourceRel: relative(REPO_ROOT, resolve(OUT_DIR, item.href)).replaceAll("\\", "/"),
        product_area: "examples",
      }));
  } catch {
    return [];
  }
}

function buildNavData(pageRecords) {
  return SITE_MAP.sections
    .slice()
    .sort((a, b) => a.order - b.order)
    .map((section) => {
      const links = pageRecords
        .filter((page) => page.sectionId === section.id && !page.nav_hidden)
        .sort((a, b) => a.nav_order - b.nav_order || a.label.localeCompare(b.label))
        .map((page) => ({ href: page.href, label: page.label, newtab: page.newtab }));
      return {
        section: section.title,
        summary: section.summary,
        links,
      };
    })
    .filter((section) => section.links.length);
}

function searchHeadingEntriesFromHtml(html) {
  return Array.from(
    String(html || "").matchAll(/<h([1-3])([^>]*)>([\s\S]*?)<\/h\1>/gi),
    (match) => {
      const level = Number(match[1]);
      const attrs = match[2] || "";
      const idMatch = attrs.match(/\sid="([^"]+)"/i);
      const text = plainTextFromHtml(match[3]).trim();
      return {
        id: idMatch ? idMatch[1] : "",
        level,
        text,
      };
    },
  ).filter((entry) => entry.text);
}

function searchHeadingsFromHtml(html) {
  return searchHeadingEntriesFromHtml(html).map((entry) => entry.text);
}

function firstParagraphFromHtml(html) {
  const match = String(html || "").match(/<p[^>]*>([\s\S]*?)<\/p>/i);
  return match ? plainTextFromHtml(match[1]).trim() : "";
}

function extractCodeTokensFromHtml(html) {
  const tokens = new Set();
  for (const match of String(html || "").matchAll(/<code[^>]*>([\s\S]*?)<\/code>/gi)) {
    const codeText = plainTextFromHtml(match[1]).trim();
    if (!codeText) continue;
    for (const token of codeText.match(/\b[A-Za-z_][A-Za-z0-9_.]{2,}\b/g) || []) {
      if (/[A-Z]/.test(token) || token.includes("_") || token.includes(".")) {
        tokens.add(token);
      }
    }
  }
  return Array.from(tokens).sort((a, b) => a.localeCompare(b));
}

function extractRouteTokensFromHtml(html) {
  const routes = new Set();
  const source = plainTextFromHtml(String(html || ""));
  for (const match of source.match(/\/[A-Za-z0-9._{}:-]+(?:\/[A-Za-z0-9._{}:-]+)+/g) || []) {
    routes.add(match);
  }
  return Array.from(routes).sort((a, b) => a.localeCompare(b));
}

function buildSearchIndex(pageRecords) {
  const pages = pageRecords
    .filter((page) => !page.search_hidden && existsSync(resolve(OUT_DIR, page.href)))
    .map((page) => {
      const html = readFileSync(resolve(OUT_DIR, page.href), "utf8");
      const headingEntries = searchHeadingEntriesFromHtml(html);
      const headings = headingEntries.map((entry) => entry.text);
      const body = plainTextFromHtml(html);
      const symbols = extractCodeTokensFromHtml(html);
      const routes = extractRouteTokensFromHtml(html);
      const pageEntry = {
        href: page.href,
        label: page.label,
        result_type: "page",
        section: page.sectionTitle,
        section_id: page.sectionId,
        summary: page.summary || firstParagraphFromHtml(html),
        audience: page.audience,
        doc_type: page.doc_type,
        stability: page.stability,
        reviewed_on: page.reviewed_on,
        version_applicability: page.version_applicability,
        tags: page.tags,
        product_area: page.product_area,
        symbols,
        routes,
        headings,
        body,
      };
      const anchorEntries = headingEntries
        .filter((entry) => entry.id && entry.level >= 2)
        .map((entry) => ({
          href: `${page.href}#${entry.id}`,
          label: entry.text,
          page_label: page.label,
          result_type: "anchor",
          section: page.sectionTitle,
          section_id: page.sectionId,
          summary: page.summary || firstParagraphFromHtml(html) || page.label,
          audience: page.audience,
          doc_type: page.doc_type,
          stability: page.stability,
          reviewed_on: page.reviewed_on,
          version_applicability: page.version_applicability,
          tags: page.tags,
          product_area: page.product_area,
          symbols: [],
          routes: [],
          headings: [entry.text],
          body,
        }));
      return [pageEntry, ...anchorEntries];
    });
  return {
    pages: pages.flat(),
  };
}

// ---------------------------------------------------------------------------
// Run
// ---------------------------------------------------------------------------

function main() {
  // Export the public capability inventory from the runtime-owned Cookbook
  // catalog before rendering prose. This JSON is derived evidence, not a second
  // hand-maintained feasibility matrix.
  const cookbookCapabilityExport = resolve(
    REPO_ROOT,
    "caliber/scripts/export_cookbook_capabilities.py",
  );
  if (existsSync(cookbookCapabilityExport)) {
    const cookbookPython = caliberDocsPython();
    if (cookbookPython) {
      try {
        execFileSync(cookbookPython, ["-B", cookbookCapabilityExport], {
          stdio: "inherit",
        });
      } catch (err) {
        if (STRICT) throw err;
        console.warn(
          `[build-docs] Cookbook capability export skipped (${err.message}) — using committed inventory.`,
        );
      }
    } else {
      console.warn(
        "[build-docs] CALIBER runtime is unavailable — using the committed Cookbook capability inventory.",
      );
    }
  }

  // Cookbooks are generated by cookbooks/training/build.py (index + one detail
  // page per cookbook + cookbooks-nav.json + appended cookbook CSS). Run it first
  // so buildNavData() can pick up the manifest. Best-effort: if python3 is absent
  // (e.g. a minimal build image) we keep the committed cookbook pages.
  const cookbookBuild = resolve(here, "cookbooks/training/build.py");
  if (existsSync(cookbookBuild)) {
    try {
      // Keep the published docs tree byte-only. Without -B, importing the
      // cookbook content module leaves __pycache__/*.pyc under docs-site/, and
      // the Pages workflow uploads that entire directory.
      execFileSync("python3", ["-B", cookbookBuild], { stdio: "inherit" });
    } catch (err) {
      if (STRICT) throw err;
      console.warn(`[build-docs] cookbook build skipped (${err.message}) — using committed cookbook pages.`);
    }
  }

  if (!existsSync(DOCS_DIR)) {
    if (STRICT) throw new Error(`[build-docs] required source directory ${DOCS_DIR} not found`);
    console.log(`[build-docs] ${DOCS_DIR} not found — nothing to generate.`);
    return;
  }

  const present = [];
  const pageRecords = [];
  for (const mod of MODULES) {
    const src = resolve(DOCS_DIR, mod.md);
    if (!existsSync(src)) {
      if (STRICT) throw new Error(`[build-docs] required source ${mod.md} not found`);
      console.warn(`[build-docs] missing source ${mod.md} — skipping ${mod.out}`);
      const staleOut = resolve(OUT_DIR, mod.out);
      const staleMarkdown = staleOut.replace(/\.html$/, ".md");
      for (const stalePath of [staleOut, staleMarkdown]) {
        if (!existsSync(stalePath)) continue;
        rmSync(stalePath);
        console.warn(`[build-docs] removed stale page ${relative(OUT_DIR, stalePath)}`);
      }
      continue;
    }
    const moduleSource = loadModuleSource(mod);
    const html = renderModule(mod, moduleSource);
    if (!html.trim() || !html.includes("<!DOCTYPE html>")) {
      throw new Error(`[build-docs] refusing to write invalid output for ${mod.out}`);
    }
    writeTextAtomic(resolve(OUT_DIR, mod.out), html);
    // Emit clean, agent-consumable Markdown beside each page. The output is
    // flattened, so rewrite known module cross-references to their sibling
    // m-*.md names; otherwise source-relative links would escape docs-site or
    // point at directories that do not exist in the published layout.
    const markdown = moduleSource.authoredMarkdown;
    // The banner is an HTML comment, so it stays invisible wherever the file is
    // rendered as Markdown (including the in-app docs and the `Copy page`
    // payload) while being the first thing an editor sees.
    writeTextAtomic(
      resolve(OUT_DIR, mod.out.replace(/\.html$/, ".md")),
      generatedBanner(moduleSourceRel(mod)) +
        "\n\n" +
        rewriteMarkdownCrossReferences(moduleSource.bodyMarkdown ? moduleSource.authoredMarkdown.replace(/^---\n[\s\S]*?\n---\n?/, "") : markdown, mod.md)
    );
    present.push(mod);
    pageRecords.push(pageRecordForModule(mod, moduleSource));
  }

  const cookbookRecords = cookbookPageRecords().filter(
    (page) => !pageRecords.some((existing) => existing.href === page.href)
  );
  const manualRecords = manualPageRecords();
  pageRecords.push(...manualRecords, ...cookbookRecords);

  const nav = buildNavData(pageRecords);
  const pageMeta = pageRecords.map((page) => ({
    href: page.href,
    label: page.label,
    section: page.sectionTitle,
    section_id: page.sectionId,
    summary: page.summary,
    audience: page.audience,
    doc_type: page.doc_type,
    prerequisites: page.prerequisites,
    stability: page.stability,
    reviewed_on: page.reviewed_on,
    version_applicability: page.version_applicability,
    tags: page.tags,
    newtab: page.newtab,
    nav_hidden: page.nav_hidden,
  }));
  const navJs =
    "/* Generated by build-docs.mjs — do not edit by hand. */\n" +
    "window.DOCS_DATA = " +
    JSON.stringify({ sections: nav, pages: pageMeta }, null, 2) +
    ";\n" +
    "window.DOCS_NAV = window.DOCS_DATA.sections;\n";
  writeTextAtomic(resolve(OUT_DIR, "docs-nav.js"), navJs);
  writeTextAtomic(
    resolve(OUT_DIR, "search-index.json"),
    JSON.stringify(buildSearchIndex(pageRecords), null, 2) + "\n"
  );

  // llms.txt — a machine index of the documentation (https://llmstxt.org/). Links
  // point at the raw .md siblings (clean source, no chrome) so an agent can read
  // the docs programmatically. Grouped to mirror the sidebar.
  const llmsLines = [
    "# CALIBER",
    "",
    "> MLflow-integrated control plane for building, evaluating, calibrating, governing, and observing trusted agentic workflows — prompts, tools, skills, MCP servers, workflows, knowledge bases, and the Aria copilot — in embedded-plugin or standalone-service topologies.",
    "",
    "The pages below are the published architecture, workflow, and strategy documentation. Each link is a flattened Markdown copy built for programmatic access.",
    "",
  ];
  for (const section of SITE_MAP.sections.slice().sort((a, b) => a.order - b.order)) {
    const pages = pageRecords
      .filter((page) => (
        page.sectionId === section.id &&
        page.href.startsWith("m-") &&
        page.href.endsWith(".html") &&
        existsSync(resolve(OUT_DIR, page.href.replace(/\.html$/, ".md")))
      ))
      .sort((a, b) => a.nav_order - b.nav_order || a.label.localeCompare(b.label));
    if (!pages.length) continue;
    llmsLines.push(`## ${section.title}`, "");
    for (const page of pages) {
      const mdName = page.href.replace(/\.html$/, ".md");
      const blurb = page.summary ? page.summary.trim().replace(/\s+/g, " ") : "";
      llmsLines.push(`- [${page.label}](${mdName})${blurb ? ": " + blurb : ""}`);
    }
    llmsLines.push("");
  }
  writeTextAtomic(resolve(OUT_DIR, "llms.txt"), llmsLines.join("\n"));

  console.log(`[build-docs] generated ${present.length} module pages + ${present.length} .md + docs-nav.js + search-index.json + llms.txt`);
}

main();

export {
  renderBlocks,
  renderInline,
  rewriteMarkdownCrossReferences,
  normalizePublishedMarkdown,
  MODULES,
};
