# CALIBER Cookbooks

Sixteen build-along recipes that teach the CALIBER platform end-to-end. Every
recipe is also a versioned system example: with `CALIBER_FEATURE_COOKBOOKS=true`
set on the server (see [Feature flags](#feature-flags-read-first)), open
**Cookbooks**, review its live readiness checks, and install it as a paused
workflow plus editable draft. Live
models, connectors, workers, secrets, and traces remain deployment prerequisites;
the product does not represent an unavailable dependency as ready. The generated
[`capabilities.json`](capabilities.json) is derived from the runtime catalog.

> **🎓 Training guide:** open [`../m-16-cookbooks.html`](../m-16-cookbooks.html) — a
> polished, self-contained HTML guide with refreshed names, step-by-step UI
> walkthroughs, Mermaid flow diagrams, UI mockups, asset tables, and quality
> gates for all 16 cookbooks. (Regenerate with `python3 training/build.py`.)
>
> **Start here:** [`FEASIBILITY.md`](FEASIBILITY.md) is the verified capability
> matrix (what ships ✅ / is partial ⚠️ / is aspirational ❌) plus the reusable
> recipe conventions every `README.md` depends on. Read it before building.
>
> **Aria track (12–15):** needs `CALIBER_FEATURE_ARIA=true`.
> [`ARIA-AUTONOMY.md`](ARIA-AUTONOMY.md) covers the
> scenarios where you give **Aria** a one-line intent and it plans + creates the
> artifacts, with you filling each step's inputs in the plan's form and approving
> each change (bounded by Aria's real capability registry).
>
> **Review:** [`CRITIQUE-REPORT.md`](CRITIQUE-REPORT.md) is the implementability
> critique — every scenario checked against the shipped code, the defects found
> + fixed, and a per-scenario verdict.

## Feature flags (read first)

The Cookbooks gallery, and several of the areas the labs build on, are preview or
record-only areas of the alpha and are **off by default** on the server. Each is a
variable you set to `true` before starting CALIBER (the Docker stack passes them
through from the environment), and a restart picks it up:

| Variable | Turns on | Labs that need it |
| --- | --- | --- |
| `CALIBER_FEATURE_COOKBOOKS` | the Cookbooks gallery and `/cookbooks` API (installing a lab) | all 16 |
| `CALIBER_FEATURE_ARIA` | the Aria assistant and **Plans** | 12, 13, 14, 15; 01 (steps 3, 4, 6 and 8: Playground, test generation and test runs call the Aria assistant); 09 only if Aria narrates the diagnosis |
| `CALIBER_FEATURE_INTEGRATIONS` | MCP servers and OpenAPI integrations | 05; 07 (the GitHub MCP `issue_write` step); 08 only for its optional MCP evidence nodes |
| `CALIBER_FEATURE_RELEASE_CANDIDATES` | release-candidate signoff on **Releases** (record-only) | 11 |

Labs 02, 03, 04, 06, 10 and 16 need only `CALIBER_FEATURE_COOKBOOKS`. Every lab
README repeats its own flags in a **Prerequisite flags** line under the title. A
disabled area answers 404 with `reason_code: feature_disabled`, and
`GET /capabilities` lists all four under `features`. Turning a flag on means you
accept that area's preview behavior; see
[Alpha scope](../../docs/start/alpha-scope.md#feature-flags).

## Folder contract

Every `cookbooks/<scenario>/` folder for scenarios 01–15 contains:

- `scenario.yaml`: goal, persona, prerequisites, demo profile
- `README.md`: **executable recipe** — exact UI navigation + field values + API
  fallbacks + a per-scenario *Feasibility & substitutions* callout
- `build.yaml`: component contracts (prompt/skill/tool/MCP/workflow) — the
  *target*; reconcile aspirational items against `FEASIBILITY.md`
- `test-data.yaml`: golden, edge, negative inputs
- `verification.yaml`: judges, rule checks, quality gates, monitoring (note:
  `monitoring.traces` lists are evidence labels, **not** literal span names)

Scenario 16 (`16-observability-triage`) is README-only: it has a `README.md`
(with its four quality gates), `training-steps.json`, and `assets/`, but no
`scenario.yaml`, `build.yaml`, `test-data.yaml` or `verification.yaml`.

## Cookbook ladder

| # | Folder | Scenario | Track |
| --- | --- | --- | --- |
| 01 | `01-prompt-regression-lab` | Trustworthy Intake Classifier | Starter |
| 02 | `02-skill-trigger-packaging-lab` | Precision Skills | Starter |
| 03 | `03-tool-hardening-contract-lab` | Policy-Safe Decision Tool | Starter |
| 04 | `04-document-extraction-structuring-lab` | Document-to-JSON Pipeline | Build |
| 05 | `05-mcp-connectivity-governance-lab` | Governed Tool Connectivity (MCP) | Starter |
| 06 | `06-knowledge-retrieval-policy-qa-lab` | Grounded Knowledge Assistant | Build |
| 07 | `07-support-triage-resolution-loop` | Support Triage Copilot | Build |
| 08 | `08-incident-response-commander` | Incident Response Copilot | Build |
| 09 | `09-workflow-debugger-self-healing-lab` | Self-Healing Workflows | Build |
| 10 | `10-judge-certification-human-review-lab` | Trustworthy Evaluation | Gate |
| 11 | `11-release-readiness-factory` | Release Signoff Factory | Gate |
| 12 | `12-aria-evaluation-harness` | Aria: Evaluation Harness from Intent | Aria |
| 13 | `13-aria-review-governance-queue` | Aria: Human-Review Queue from Intent | Aria |
| 14 | `14-aria-governance-starter-kit` | Aria: Governance Starter Kit from Intent | Aria |
| 15 | `15-aria-triage-recalibrate-loop` | Aria: Triage & Recalibrate Loop | Aria |
| 16 | `16-observability-triage` | Production Observability & Triage | Operate |

## Recommended training sequences

- Quick reliability demo (35-50 min): `01 -> 03 -> 05`
- End-to-end copilot demo (70-100 min): `01 -> 03 -> 05 -> 07 -> 09`
- Release governance demo (90-130 min): `01 -> 03 -> 05 -> 07 -> 09 -> 10 -> 11`
- Aria autonomous demo (25-45 min): `12 -> 13 -> 14` (then `15` if SCN-07 exists
  for real trace/workflow ids). See [`ARIA-AUTONOMY.md`](ARIA-AUTONOMY.md).

## How to execute one scenario

1. Read `scenario.yaml` for objective, dependencies, and demo scope.
2. With `CALIBER_FEATURE_COOKBOOKS=true` set, open **Cookbooks** and inspect the
   recipe's readiness checks.
3. Follow `README.md` step-by-step (it carries the exact UI nav + API fallbacks).
4. Build to `build.yaml` (the contract); each `build.yaml` has a `feasibility:`
   block at the top noting substitutions verified against the live code.
5. Run the cases in `test-data.yaml` and gate with `verification.yaml`.

## Current UI notes (verified — see FEASIBILITY.md for the full matrix)

- **Prompt Playground is a live model chat** (it calls the model, not just a
  render); *persisted, scored* regression runs go through the prompt's `Runs`
  stage or `Evaluations`. `Evaluations` can score a **prompt**, **skill**, or
  **workflow** version directly (pick it under *What to score*).
- **"Deterministic judge" is not a type** — use deterministic *scorers*
  (`exact_match`, `token_f1`, `contains_expected`, `non_empty`) or tool/skill
  assertions; reserve `Judges` for LLM-graded criteria.
- **Deterministic workflow logic is no-code** for mapping, JSON Schema,
  decision-table, confidence, and fixture operations through the Data Transform
  node. Registered custom tools still require an importable callable.
- **Prompt/skill Calibration is queued** (background job); **KB and tool/MCP
  calibration run inline**.
- **The default LLM and evaluator are fake** (`llm_provider` / `eval_provider`
  default to `fake`): **Evaluations** returns a 400 until a real model provider is
  configured, and calibration jobs in a default install score candidates with
  canned numbers. Recipes that score or calibrate say which provider they need.
- `Test Sets` rows link to `/eval-datasets/:id`; the detail route is wired
  (`EvalDatasetDetail.tsx` — a full row editor: add/revise/retire examples and
  add-from-trace). The API (`POST …/examples`, `…/from-trace`) still works too.
- Skill package: export in UI (`Download ZIP`) and upload the ZIP directly with
  explicit reject/rename/admin-merge conflict handling.
- `monitoring.traces` names in `verification.yaml` are evidence labels, not
  literal MLflow span names (spans are named by workflow node id).
