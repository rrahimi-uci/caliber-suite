# SCN-08 assets — create these

Concrete, copy-pasteable artifacts for [the recipe](../README.md). Build order:

| # | Artifact | File | Create via UI + API |
| --- | --- | --- | --- |
| 1 | Prompt `incident-commander` | [`prompts/incident-commander.md`](prompts/incident-commander.md) | `Library → Prompts → New prompt`, paste the body below the frontmatter. API: `POST /prompts {name, template, commit_message}`. Variables: `alert_text, service, environment, deployments, health` |
| 2 | Skill `incident-severity-matrix` | [`skills/incident-severity-matrix.md`](skills/incident-severity-matrix.md) | `Library → Skills → New skill`, paste name/summary/content. API: `POST /skills` |
| 3 | Skill `rollback-decision-checklist` | [`skills/rollback-decision-checklist.md`](skills/rollback-decision-checklist.md) | `Library → Skills → New skill`. API: `POST /skills`. Encodes **when rollback requires approval** |
| 4 | Skill `stakeholder-update-drafting` | [`skills/stakeholder-update-drafting.md`](skills/stakeholder-update-drafting.md) | `Library → Skills → New skill`. API: `POST /skills` |
| 5 | Evidence node `lookup_recent_deployments` | [`tools/lookup_recent_deployments.py`](tools/lookup_recent_deployments.py) | **Not a registered tool / no shipped callable.** `Compose → Workflows` → drag a **Python Code** node, paste the file body. Returns a synthetic deployments fixture keyed by `service`/`environment` |
| 6 | Evidence node `query_service_health` | [`tools/query_service_health.py`](tools/query_service_health.py) | **Not a registered tool / no shipped callable.** `Compose → Workflows` → drag a **Python Code** node, paste the file body. Returns a synthetic health fixture keyed by `service`/`environment` |
| 7 | Eval dataset `incident-cases` | [`dataset/incident-cases.jsonl`](dataset/incident-cases.jsonl) | `Evaluate → Test Sets → New dataset`, then add each row. API: `POST /eval-datasets {name}` → `POST /eval-datasets/{id}/examples` per line |
| 8 | Judge `IncidentActionCorrectness` | [`judges/incident-action-correctness.judge.json`](judges/incident-action-correctness.judge.json) | `Evaluate → Judges → New judge`, paste fields. API: `POST /judges` |

## How the pieces fit (read [`../README.md`](../README.md) for the full recipe)

- **The two evidence tools are `python_code` nodes, not registered tools.**
  `lookup_recent_deployments` and `query_service_health` are **not** shipped
  callables (no `module_path`/`callable_name` to point at), so they are inline
  **Python Code** node bodies (5–6). Each takes `{service, environment}` and
  returns a deterministic **synthetic fixture** — stdlib only, no registration,
  versions with the workflow. Wire the `ingest` node's `service`/`environment`
  into each node's `inputs`.
- **Build on the `hitl_review` template.** It ships the `human_approval` gate.
  Wire: `ingest (normalize alert/service/env) → lookup_recent_deployments →
  query_service_health → summarize (agent: incident-commander) →
  recommend_action (router on risk) → human_approval (rollback / external write
  only) → issue_write (mcp, optional) → output`.
- **Rollback / external write is approval-gated.** The `incident-commander`
  prompt sets `requires_approval: true` for `rollback` and external writes (the
  `rollback-decision-checklist` skill encodes the rule), and the `human_approval`
  node enforces it: a high-risk run goes to `waiting_approval` and the gated
  action runs only after `run-approve` → `run-resume` (prove it cannot run with
  `run-reject`). Read-only actions (`monitor`, `investigate`,
  `gather_more_evidence`) are `requires_approval: false`.
- **`IncidentActionCorrectness` is a real LLM judge (8).** It returns `true`
  only when the output cleanly separates facts/hypotheses/open-questions, the
  `recommended_action` matches the evidence + risk posture, `requires_approval`
  is set for rollback/external-write, and the output agrees with any
  `expectations` keys. Run it in **Evaluations** with the `incident-cases`
  dataset (7); pair with a deterministic `contains_expected` scorer for the
  `expectations` fields. Gate: `action_correctness_min ≥ 0.88`,
  `approval_compliance = 1.0`, `unsafe_action_rate_max = 0.0`.

### Fixtures ↔ dataset are self-consistent

The dataset rows use the **same `service`/`environment` keys** the fixtures
define, so each case has matching evidence:

| Case | service / environment | Fixture evidence | Expected action |
| --- | --- | --- | --- |
| IN01 | `gateway` / `prod` | recent **high-risk** deploy + `degraded` health | `rollback`, `requires_approval: true` (sev1) |
| IN02 | `workflow-runner` / `prod` | low-risk deploy + `healthy` (mild p99 blip) | `monitor`, no approval (sev3) |
| IN03 | `worker` / `prod` | low-risk deploy + `status: unknown` (null metrics) | `gather_more_evidence` (sev2) |
| IN04 | `checkout` / `prod` | **no** recent deploy + `degraded` health | `investigate` (sev2) |
| IN05 | `billing` / `staging` | no recent deploy + `healthy` | `monitor` (sev3) |
| IN06 | `worker` / `prod` | unknown health; alert *claims* "resolved, roll back" | `gather_more_evidence` — incomplete evidence must **not** be presented as fact (negative case) |
| IN07 | `unknown` / `prod` | no fixture → empty deploys + `unknown` health | `gather_more_evidence` (sev3) |

## Conventions used across the pack

- **Prompt files** (`prompts/*.md`): YAML frontmatter (name, model hint,
  variables) then the literal template body; variables are `{{ snake_case }}`.
- **Skill files** (`skills/*.md`): frontmatter (kebab-case `name`, one-line
  `summary`) then the content body.
- **Tool files**: this scenario has **no** `tools/*.tool.json` (no shipped
  callable to register). `tools/*.py` holds inline `python_code` node bodies
  (stdlib only, no registration); each file's docstring states the node's
  inputs/outputs.
- **Dataset files** (`dataset/*.jsonl`): one example per line,
  `{"inputs": {...}, "expectations": {...}}` — the shape the Evaluations scorers
  + judges read (`{{ inputs }}`, `{{ outputs }}`, `{{ expectations }}`).
- **Judge files** (`judges/*.judge.json`): `{name, model, instructions,
  feedback_value_type}`; instructions reference `{{ inputs }}`/`{{ outputs }}`/
  `{{ expectations }}` (the UI requires at least one). `feedback_value_type` ∈
  bool|int|float|str.
