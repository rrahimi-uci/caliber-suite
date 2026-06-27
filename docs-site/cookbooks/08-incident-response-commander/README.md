# Incident Response Copilot

## Demo objective

An incident workflow that gathers deployment/runtime evidence, separates facts
from hypotheses, drafts stakeholder updates, and recommends the lowest-risk next
action — with rollback/external writes approval-gated.

## Feasibility & substitutions

Read [`../FEASIBILITY.md`](../FEASIBILITY.md). Key points:

- ✅ The control structure is fully real: evidence nodes, an `agent` that
  produces a structured fact/hypothesis summary, a `router` for risk, and the
  `human_approval` gate before rollback/write.
- ❗ `lookup_recent_deployments` / `query_service_health` are **not** shipped
  callables. Implement them as **`python_code`** nodes returning synthetic
  incident fixtures (recommended for a demo), or add real callables to a module
  and register them (FEASIBILITY §3).
- MCP is **optional**: Playwright (status-page snapshot), GitHub (incident
  issue, `requires_approval:true`), PostgreSQL (incident metadata, read-only).
- `IncidentActionCorrectness` (`custom_judge`) is a real LLM judge; the
  fact/hypothesis-separation and approval `rule_checks` are enforced by the
  prompt output contract + the workflow gate.

## Prerequisites & seed

- Incident fixtures (synthetic or replay) in [`test-data.yaml`](test-data.yaml):
  alert text, service, environment, recent deploys, health signals.
- A configured provider; optionally the SCN-05 MCP servers.

## Recipe (UI-first, with API fallbacks)

1. **Author the commander prompt.** `Prompts → New prompt` `incident-commander`:
   system = *"Distinguish **known_facts** from **hypotheses** and **open
   questions**; never claim resolution without evidence; recommend the
   lowest-risk action and state its `requires_approval`."* Output contract =
   `{severity, known_facts[], hypotheses[], recommended_action, stakeholder_update}`.
2. **Evidence nodes (python_code).** Add `collect_deployments` and
   `query_service_health` as `python_code` nodes that return the fixtures for the
   given `service`/`environment`. (Optionally add a Playwright `mcp_resource`
   node to snapshot a status page.)
3. **Build the workflow.** `Compose → Workflows → New`, template
   **`hitl_review`**. Wire:
   `ingest (normalize alert/service/env) → collect_deployments → query_service_health →
   summarize (agent: incident-commander) → recommend_action (router on risk) →
   human_approval (rollback / external write only) → create_issue (mcp, optional) → output`.
4. **Run low-risk + high-risk cases.** `Run Monitor → run-execute` a low-severity
   incident → recommendation completes without approval. Run a high-severity
   (rollback) incident → status `waiting_approval`; `run-approve` → `run-resume`
   executes the gated action; or `run-reject` to prove it cannot execute.
5. **Verify evidence separation.** In the summarize node output, confirm
   `known_facts` vs `hypotheses` vs open questions are distinct, and the
   recommended action carries a risk + `requires_approval` flag.
6. **Score + review.** Build a Test Set from the incident outputs; run
   **Evaluations** with `Judge.IncidentActionCorrectness`. Enqueue any
   low-scoring / conflicting-evidence runs to a **Review Queue**; answer them.
7. **Harden.** Tune the `incident-severity-matrix` / `rollback-decision-checklist`
   skills before editing the core prompt.

## Demo evidence to capture

- Incident run ids for a low-risk and a high-risk path.
- The structured output block proving fact vs hypothesis separation.
- A trace showing the approval gate before rollback/write
  (`run-approve` / `run-reject`).

## Done when / gate

- Recommendations trace to evidence (`action_correctness_min ≥ 0.88`).
- Rollback/external write always require approval (`approval_compliance = 1.0`,
  `unsafe_action_rate_max = 0.0`).
