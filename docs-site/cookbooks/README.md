# CALIBER Cookbooks

Seventeen build-along labs (numbered 01 to 16 and 18; 17 is a standalone SDK automation, `cookbook_17_financial_analysis.py`, with no
catalog entry) that teach the CALIBER platform end-to-end. Each lab was run for real, step by step, against a disposable server
(`scripts/cookbook-env.sh`) through the API and through the browser, and its README says which parts run on the alpha's default
**simulated** model and evaluator and which are real. Every lab is also a versioned system example: with `CALIBER_FEATURE_COOKBOOKS=true`
set on the server (see [Feature flags](#feature-flags-read-first)), open
**Cookbooks**, review its live readiness checks, and install it as a paused
workflow plus editable draft. Live
models, connectors, workers, secrets, and traces remain deployment prerequisites;
the product does not represent an unavailable dependency as ready. The generated
[`capabilities.json`](capabilities.json) is derived from the runtime catalog.

> **🎓 Training guide:** open [`../m-16-cookbooks.html`](../m-16-cookbooks.html) — a
> polished, self-contained HTML guide with refreshed names, step-by-step UI
> walkthroughs, Mermaid flow diagrams, UI mockups, asset tables, and quality
> gates for all 17 cookbooks. (Regenerate with `python3 training/build.py`.)
>
> **Start here:** [`FEASIBILITY.md`](FEASIBILITY.md) is the verified capability
> matrix (what ships ✅ / is partial ⚠️ / is aspirational ❌) plus the reusable
> recipe conventions every `README.md` depends on. Read it before building.
>
> **Aria track (12–15):** needs `CALIBER_FEATURE_ARIA=true`.
> [`ARIA-AUTONOMY.md`](ARIA-AUTONOMY.md) covers the
> scenarios where you give **Aria** a one-line intent and it plans + creates the
> artifacts, with you filling each step's inputs in the plan's form and approving
> each change (bounded by Aria's real capability registry). The planner is rules,
> not a model, so these four labs are deterministic.
>
> **Verification:** [`FIRST-USER-VERIFICATION.md`](FIRST-USER-VERIFICATION.md) is the first-user run of every lab (fresh stack,
> real clicks, API and browser paths): what passed, on which model tier, the defects it found and how they were fixed.
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
| `CALIBER_FEATURE_COOKBOOKS` | the Cookbooks gallery and `/cookbooks` API (installing a lab) | the gallery step of all 17; the only flag labs 01, 02, 03, 04, 06, 07, 08, 09, 10, 16 and 18 need |
| `CALIBER_FEATURE_ARIA` | the Aria assistant and **Plans** | 12, 13, 14, 15 |
| `CALIBER_FEATURE_INTEGRATIONS` | MCP servers and OpenAPI integrations | 05 |
| `CALIBER_FEATURE_RELEASE_CANDIDATES` | release-candidate signoff on **Releases** (record-only) | 11 |

Every lab README repeats its own flags in a **Prerequisite flag** line under the title.
A disabled area answers 404 with `reason_code: feature_disabled`, and
`GET /capabilities` lists all four under `features`. Turning a flag on means you
accept that area's preview behavior; see
[Alpha scope](../../docs/start/alpha-scope.md#feature-flags).

Beyond the flags, some labs need server settings or helpers that `scripts/cookbook-env.sh` provides. It starts a
disposable stack (migrate, native MLflow, CALIBER, background workers, `/readiness` ready) on ports it picks, never touching
port 5001; `--flags aria,integrations,cookbooks,release_candidates` (or `all`) turns the preview areas on, `--s3` starts a
private S3-compatible endpoint (labs 04 and 06), `--real-llm` uses a real provider instead of the simulated one (the stand-in
model of labs 01 and 10). Sign in as `admin` / `admin`.

## Labs at a glance

Each lab's folder has a runnable SDK example, `sdk/caliber-sdk/examples/cookbooks/cookbook_NN_*.py` (it performs every step through
the typed SDK, prints one `ok` line per verified outcome and exits 0 only if all of them held), and a Playwright spec,
`caliber/caliber-ui/e2e/cookbooks/labs/NN-*.spec.ts` (the lab's "Run it through the UI" steps, run with
`CALIBER_LAB_BASE_URL="$CALIBER_URL/caliber" npx playwright test --config=playwright.labs.config.ts e2e/cookbooks/labs/NN-*.spec.ts`
from `caliber/caliber-ui`). The lab README holds the exact commands and the expected output.

| # | Flags (besides Cookbooks) | Dataset | Simulated or real |
| --- | --- | --- | --- |
| 01 | none | 44 labelled support messages (`intake-classifier.jsonl`) | Apply, registry, live board, timeline, audit and rollback are real. Scoring a prompt is refused on the simulated model (`400`); calibration evidence is canned and marked simulated. A stand-in model (`assets/stand-in-model/server.py`) shows the scored path; a hosted model (`lab01h`, `--real-llm`) or a local Ollama one runs it for real, and `lab01ha` (`--flags cookbooks,aria`) adds the Aria steps. |
| 02 | none | 40 trigger cases | Trigger selection is deterministic and real. Calibration is simulated (a skill has no test set). |
| 03 | none | 49 refund fixtures | No model anywhere: nothing simulated. Needs runtime approvals, the run queue and checkpointing, and `CALIBER_APPROVER_USERS=admin,approver1`. |
| 04 | none (needs an S3-compatible store: `--s3`) | 30 invoice documents and `extraction-cases.jsonl` | No model is called: the extractor is a Python Code node and the check a JSON Schema node. |
| 05 | `CALIBER_FEATURE_INTEGRATIONS` | the lab's own six-tool MCP server and 28 test cases | No model anywhere. A real MCP server over `stdio`; its ledger proves what a refusal stopped. |
| 06 | none (`--s3`) | 11 policy documents, 48 questions (`qa-eval.jsonl`), 41 calibration questions | Storage, chunking, embeddings and retrieval ranking are real; answer text and model-judged scores are simulated. |
| 07 | none | 44 realistic tickets | The classifier is rules, not a model; the `draft_reply` agent node is simulated. |
| 08 | none | 41 incidents | The decision is declarative (Preview and the deploy gate can run it); the `draft_update` agent node is simulated. |
| 09 | none (`CALIBER_EGRESS_ALLOWED_HOSTS=127.0.0.1` for the stand-in account service) | 35 refund tickets | Real failures from real code; the account service is a stand-in. |
| 10 | none | 50 reviewed support replies and incident summaries | A judge needs a real model: the default stack refuses, a stand-in model (`lab10m`, `--real-llm`) runs it. |
| 11 | `CALIBER_FEATURE_RELEASE_CANDIDATES` | 20 release candidates | No model anywhere; signing off records a decision and deploys nothing. |
| 12 | `CALIBER_FEATURE_ARIA` | 28 planner goals and 30 support questions | Rules-based planner. Scoring the judge is refused on the simulated stack. |
| 13 | `CALIBER_FEATURE_ARIA` | 26 planner goals and 30 flagged agent replies (12 reviewed) | Rules-based planner; no model involved. |
| 14 | `CALIBER_FEATURE_ARIA` | 28 planner goals and 30 policy questions | Rules-based planner. Scoring the judge is refused on the simulated stack. |
| 15 | `CALIBER_FEATURE_ARIA` (plus background workers) | 28 planner goals, a workflow with a 30-case test set, 12 flagged traces | Rules-based planner; the calibration finds no candidate on the simulated stack. |
| 16 | none (`CALIBER_EGRESS_ALLOWED_HOSTS=127.0.0.1`) | 40 production-like refund tickets (12 `ERROR`, 28 `OK`) | Real traces from lab 09's workflow; Evaluations is not used. |
| 18 | none | a nine-person roster and a 72-request role matrix | No model; `/readiness` names the simulated providers. |

## Folder contract

Every `cookbooks/<scenario>/` folder for scenarios 01–15 and 18 contains:

- `scenario.yaml`: goal, persona, prerequisites, demo profile
- `README.md`: **executable recipe** — what the lab demonstrates, what is simulated,
  setup, the dataset, the one-command SDK run, the same steps by hand with `curl`, the
  UI path, how to verify, troubleshooting, what is not covered and cleanup
- `build.yaml`: component contracts (prompt/skill/tool/MCP/workflow), with a
  `feasibility:` block at the top noting what was verified against the live code
- `test-data.yaml`: golden, edge, negative inputs
- `training-steps.json`: the numbered UI steps of the training guide
- `assets/`: the datasets, prompts, tools and scripts the lab uses, with an `assets/README.md`
- `verification.yaml`: judges, rule checks, quality gates, monitoring (note:
  `monitoring.traces` lists are evidence labels, **not** literal span names)

Scenarios 01 to 16 and 18 each have a runnable SDK example that drives the lab and asserts the outcomes
(scenario 18's data files, `assets/dataset/team-roster.csv`, `role-matrix.csv` and `failure-scenarios.json`, are read and asserted by it).

Scenario 16 (`16-observability-triage`) is README-only: it has a `README.md`
(with its quality gates), `training-steps.json`, and `assets/`, but no
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
| 18 | `18-projects-roles-tokens-audit` | Projects, Roles, Tokens & Audit | Gate |

## Recommended training sequences

- Quick reliability demo (35-50 min): `01 -> 03 -> 05`
- End-to-end copilot demo (70-100 min): `01 -> 03 -> 05 -> 07 -> 09`
- Release governance demo (90-130 min): `01 -> 03 -> 05 -> 07 -> 09 -> 10 -> 11`
- Aria autonomous demo (45-90 min): `12 -> 13 -> 14`, then `15` (it needs background workers
  and creates its own workflow and 12 flagged traces with two scripts in its `assets/`; it does not depend on lab 07).
  See [`ARIA-AUTONOMY.md`](ARIA-AUTONOMY.md).
- Operate a running workflow (60-90 min): `09 -> 16` (lab 16 replays lab 09's buggy version 1).
- Team set-up and governance (45-60 min, no model needed): `18` on its own, before anyone else
  uses the platform: roles, project-bound tokens, the audit log and readiness.

## How to execute one scenario

1. Read the lab's `README.md`: what it demonstrates, **You are done when**, and what is simulated.
2. Start a disposable stack with the lab's flags, for example
   `scripts/cookbook-env.sh up lab01 --flags cookbooks` then `source .tmp/cookbook-env/lab01/env.sh`
   (the lab README has the exact line, including any extra variables), or use your own server built per
   [`docs/start/alpha-install.md`](../../docs/start/alpha-install.md). With `CALIBER_FEATURE_COOKBOOKS=true`
   set, **Cookbooks** (sidebar group **Get started**) lists the lab's readiness checks.
3. Run the whole lab in one command with its SDK example
   (`PYTHONPATH=sdk/caliber-sdk caliber/.venv/bin/python sdk/caliber-sdk/examples/cookbooks/cookbook_NN_*.py`),
   or follow the README's steps by hand (`curl`) or through the UI.
4. Run the browser path with the lab's Playwright spec (command above), then compare with `verification.yaml`.
5. `scripts/cookbook-env.sh down NAME` removes the stack.

## Run every lab

`scripts/cookbook-run-all.sh` runs the labs one after another, each from a clean state, doing only what that lab's
README documents: a fresh stack (`scripts/cookbook-env.sh up ...` with the README's variables, flags and `--s3`), the
token the README mints, the lab's SDK example script (API path), the lab's Playwright spec (UI path, `playwright.labs.config.ts`),
then teardown. It prints a table (lab, API, UI, seconds) and exits non-zero if any step failed.

```bash
scripts/cookbook-run-all.sh                       # every lab (01-16 and 18), simulated providers, API and UI
scripts/cookbook-run-all.sh --labs 01,02,18       # a selection (always run in lab order)
scripts/cookbook-run-all.sh --api-only            # only the SDK example scripts (no browser)
scripts/cookbook-run-all.sh --ui-only             # only the Playwright specs
scripts/cookbook-run-all.sh --labs 06 --keep      # leave the last lab's stack running to look around
scripts/cookbook-run-all.sh --tier standin --labs 01,10   # labs 01 and 10 behind their scripted stand-in model servers
scripts/cookbook-run-all.sh --journey             # the cross-lab "alpha path" on one shared stack (below)
```

Prerequisites: `caliber/.venv` (`make install`), Node with `npm ci` done in `caliber/caliber-ui`, the Playwright browser
(`cd caliber/caliber-ui && npx playwright install chromium`), `curl` and `perl`; lab 06 needs the all-MiniLM-L6-v2 embedding
model in the Hugging Face cache (see its README). No model key is needed: the default tier is the simulated one every lab is
written for.

- **Time.** A full pass (all 17 labs, API and UI) took about 33 minutes on a laptop; the journey about 12 minutes; `--api-only`
  is faster. Most labs take one to four minutes; labs 09 and 16 take about four to five each. The runner keeps a Mac awake with
  `caffeinate`. A step that hangs is stopped after `COOKBOOK_RUN_ALL_API_TIMEOUT` / `COOKBOOK_RUN_ALL_UI_TIMEOUT` (default 1800 /
  2400 seconds). Run it in the background and poll its log (`scripts/cookbook-run-all.sh > run.log 2>&1 &`).
- **Flags.** `--labs` selects labs; `--tier standin` swaps in the stand-in model servers of labs 01 and 10 (the only labs that
  have one; the lab READMEs say how); `--api-only` / `--ui-only` run one path; `--keep` keeps the **last** selected lab's stack
  and helper processes up (earlier labs are still torn down: only one stack runs at a time); `--journey` is described below.
- **Isolation.** It only starts stacks named `ra-*`, never touches the Docker stack on port 5001 or ports 5000/5001/9000, refuses
  to start while another run holds its lock, and on exit (also on failure or Ctrl-C) stops its stack and every helper it started:
  the stand-in account service (port 8780, labs 09 and 16), the stand-in model servers (ports 8765 and 8770) and moto (`--s3`,
  labs 04 and 06). Those ports must be free.
- **Logs.** `.tmp/cookbook-run-all/<timestamp>/` (also `latest`): `labNN.up.log`, `labNN.api.log`, `labNN.ui.log`, the server
  log of a failed lab, and the Playwright output (`pw-labNN/`, traces and screenshots of failures). Access tokens are redacted
  from every log; the stack of a failed lab is left in `.tmp/cookbook-env/ra-labNN` for debugging.

### The journey: the alpha path on one stack

`scripts/cookbook-run-all.sh --journey` starts **one** stack with every feature flag on and the union of the role lists the labs
export, then runs the labs in the order a new team would: **18** (workspace, roles, token) -> **01** (prompt release loop) -> **02**
(skill) -> **03** (tool) -> **07** (workflow) -> **16** (observability of workflow traces). It then runs
`scripts/cookbook-journey-check.py`, which reads the shared stack as its admin and asserts that `/readiness`, the audit log, the
release timeline and Observability reflect everything the labs did. `--labs` overrides the list, `--api-only` / `--ui-only` apply,
`--keep` keeps the stack. Labs share a stack safely because each creates uniquely named things (a timestamp or random suffix), with two exceptions the runner
handles: lab 07 and lab 18 both use an account called `sam`, so on the shared stack lab 07 is given lab 18's password through
`COOKBOOK_07_PASSWORD` (the script and the spec both read it); and lab 03 needs `approver1` to hold the approver scope but not the
operator scope, so the shared role lists keep them apart.

Not on the shared stack, each for a concrete reason (they all pass on their own stacks): labs 04 and 06 need `--s3` (an object store);
05 needs its MCP server script on `CALIBER_MCP_STDIO_PYTHON_SCRIPT_ALLOWLIST`; 08 turns on a production approval policy that would
also gate other labs' promotions; 09 uses the same stand-in service port (8780) as 16 and 16 already replays its workflow; 10 to 15
are not part of the alpha path (a judge certification, release candidates and the Aria preview).

## Current UI notes (verified in the labs; see FEASIBILITY.md for the full matrix)

- **Sidebar groups:** **Prompt lifecycle** (Prompts, Test Sets, Evaluations, Releases), **Agent resources**
  (Skills, Tools, Workflows, Knowledge Bases, Object Store, Judges, Agents), **Operate** (Observability,
  Review Queues, Audit Log), **Integrations** (LLM Gateway, MCP Servers, OpenAPI Integrations, Cookbooks, Plans)
  and **Admin** (Administration, Settings). MCP Servers, OpenAPI Integrations, Cookbooks and Plans appear
  only with their flags on.
- **The default LLM and evaluator are fake** (`llm_provider` / `eval_provider` default to `fake`).
  **Evaluations** answers `400 evaluation requires a real LLM provider` (a missing test set is `404`), and
  prompt and skill calibration score candidates with canned numbers marked `simulated: true`
  (**Simulated evaluation — not evidence**). The labs assert structure, wiring and persistence on a default
  stack, never a score; each says which provider a real score needs.
- **The prompt Playground, Generate Test Cases, Run Tests & Judge and the Runs stage call the Aria assistant**
  (`CALIBER_FEATURE_ARIA` and a real model). Lab 01's main path uses a file import (**Calibration** stage, **Upload
  JSON/JSONL**) and **Evaluations › Compare to** a baseline; its README's appendix runs the Aria steps on a hosted model. `Evaluations` can score a **prompt**,
  **skill** or **workflow** version (pick it under *What to score*).
- **"Deterministic judge" is not a type**: use deterministic *scorers* (`exact_match`, `token_f1`,
  `contains_expected`, `non_empty`) or tool/skill assertions; reserve `Judges` for LLM-graded criteria.
- **Deterministic workflow logic is no-code** for mapping, JSON Schema, decision-table, confidence, and
  fixture operations through the Data Transform node (labs 04 and 08). Registered custom tools still require
  an importable callable.
- **Prompt/skill Calibration is queued** (a background job; wait for `candidate_ready`, then **Apply**
  explicitly); **KB and tool/MCP calibration run inline**.
- A second person approves: a run's starter cannot approve it (`403`) unless
  `CALIBER_APPROVAL_ALLOW_SELF_APPROVAL` is set; scopes come from server settings
  (`CALIBER_APPROVER_USERS`, `CALIBER_OPERATOR_USERS`, `CALIBER_ADMIN_USERS`) (labs 03, 04, 05, 07, 08).
- `Test Sets` rows link to `/eval-datasets/:id` (`EvalDatasetDetail.tsx`, a full row editor: add/revise/retire
  examples and add-from-trace); a file import is on a prompt's **Calibration** stage or through the API.
- Skill package: export in UI (`Download ZIP`) and upload the ZIP directly with explicit
  reject/rename/admin-merge conflict handling.
- `monitoring.traces` / `monitoring.evidence` names in `verification.yaml` are evidence labels, not
  literal MLflow span names (a workflow run's spans are `workflow.run` and `node.<id>`).
