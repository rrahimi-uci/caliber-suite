# CALIBER Cookbooks

Sixteen build-along recipes that teach the CALIBER platform end-to-end — each one
**fully implementable in the product UI**, with no code changes or backend access.
Built for public demos, product training, and developer onboarding.

> **🎓 Training guide:** open [`training/index.html`](training/index.html) — a
> polished, self-contained HTML guide with refreshed names, step-by-step UI
> walkthroughs, Mermaid flow diagrams, UI mockups, asset tables, and quality
> gates for all 16 cookbooks. (Regenerate with `python3 training/build.py`.)
>
> **Start here:** [`FEASIBILITY.md`](FEASIBILITY.md) is the verified capability
> matrix (what ships ✅ / is partial ⚠️ / is aspirational ❌) plus the reusable
> recipe conventions every `README.md` depends on. Read it before building.
>
> **Aria track (12–15):** [`ARIA-AUTONOMY.md`](ARIA-AUTONOMY.md) covers the
> scenarios where you give **Aria** a one-line intent and it plans + builds the
> artifacts itself (bounded by Aria's real capability registry).
>
> **Review:** [`CRITIQUE-REPORT.md`](CRITIQUE-REPORT.md) is the implementability
> critique — every scenario checked against the shipped code, the defects found
> + fixed, and a per-scenario verdict.

## Folder contract

Every `cookbooks/<scenario>/` folder contains:

- `scenario.yaml`: goal, persona, prerequisites, demo profile
- `README.md`: **executable recipe** — exact UI navigation + field values + API
  fallbacks + a per-scenario *Feasibility & substitutions* callout
- `build.yaml`: component contracts (prompt/skill/tool/MCP/workflow) — the
  *target*; reconcile aspirational items against `FEASIBILITY.md`
- `test-data.yaml`: golden, edge, negative inputs
- `verification.yaml`: judges, rule checks, quality gates, monitoring (note:
  `monitoring.traces` lists are evidence labels, **not** literal span names)

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

## Recommended training sequences

- Quick reliability demo (35-50 min): `01 -> 03 -> 05`
- End-to-end copilot demo (70-100 min): `01 -> 03 -> 05 -> 07 -> 09`
- Release governance demo (90-130 min): `01 -> 03 -> 05 -> 07 -> 09 -> 10 -> 11`
- Aria autonomous demo (25-45 min): `12 -> 13 -> 14` (then `15` if SCN-07 exists
  for real trace/workflow ids). See [`ARIA-AUTONOMY.md`](ARIA-AUTONOMY.md).

## How to execute one scenario

1. Read `scenario.yaml` for objective, dependencies, and demo scope.
2. Skim [`FEASIBILITY.md`](FEASIBILITY.md) for any ⚠️/❌ items the scenario touches.
3. Follow `README.md` step-by-step (it carries the exact UI nav + API fallbacks).
4. Build to `build.yaml` (the contract); each `build.yaml` has a `feasibility:`
   block at the top noting substitutions verified against the live code.
5. Run the cases in `test-data.yaml` and gate with `verification.yaml`.

## Current UI notes (verified — see FEASIBILITY.md for the full matrix)

- **Prompt Playground renders only; scored runs go through `Evaluations`.**
- **"Deterministic judge" is not a type** — use deterministic *scorers*
  (`exact_match`, `token_f1`, `contains_expected`, `non_empty`) or tool/skill
  assertions; reserve `Judges` for LLM-graded criteria.
- **Custom tools must be importable Python** (`module_path`+`callable_name`) or
  inline **`python_code`** workflow nodes — there is no inline tool editor.
  Shipped callables live in `caliber.workflows.demo_tools` / `ingestion_tools` /
  `file_tools`.
- **Prompt/skill Calibration is queued** (background job); **KB and tool/MCP
  calibration run inline**.
- `Test Sets` rows link to `/eval-datasets/:id`, but only the list `/eval-datasets`
  is wired; manage dataset rows via API or the `Evaluations` flow.
- Skill package: export in UI (`Download ZIP`); import via `POST /skills/import-package`.
- `monitoring.traces` names in `verification.yaml` are evidence labels, not
  literal MLflow span names (spans are named by workflow node id).
