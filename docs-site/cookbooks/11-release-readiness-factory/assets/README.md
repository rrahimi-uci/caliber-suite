# SCN-11 assets — create these

Concrete, copy-pasteable artifacts for [the recipe](../README.md). This scenario
is an **operator process**, not a built workflow graph: you collect evidence from
the upstream scenarios, refresh and open Allure, compute the `release_rubric`
**by hand**, and publish a go/no-go decision record.

> **No release-scoring engine.** CALIBER does **not** score releases for you.
> Every dimension below is tallied manually from real evidence (Evaluations run
> ids/scorecards, Review Queue completion, Observability traces, in-app Allure).
> The optional `release-risk-summarizer` prompt (or an Aria Plan) only **drafts
> the narrative** from evidence you supply — it does not gather or verify it, and
> it must not invent passing evidence.
>
> **Allure is generated externally.** The app only *serves* the static report at
> `Settings → Allure Report` (`GET /observability/allure-report`). You must run
> `make allure-report` yourself before the demo so the report loads.

Build order:

| # | Step / Artifact | File | Do this |
| --- | --- | --- | --- |
| 1 | Fill the run-id manifest | [`dataset/run-id-manifest.json`](dataset/run-id-manifest.json) | From `Evaluate → Evaluations`, copy each upstream scenario's scorecard run id into `eval_run_id`; from `Observe → Review Queues`, confirm each required queue is fully answered and set `review_queue_complete`. Record the re-run ids for the critical slices (SCN-07 approval branch, SCN-08 rollback) in `approval_branch_run_id`. |
| 2 | Refresh + open Allure | — | Run `make allure-report` (combines vitest + playwright + pytest allure results), then `Settings → Allure Report` and confirm the report **loads** in-app. Paste the URL into the manifest's `allure` block and set `loaded: true`. Default URL is the backend-served `/observability/allure-report/`. |
| 3 | (Optional) Author the summarizer prompt | [`prompts/release-risk-summarizer.md`](prompts/release-risk-summarizer.md) | `Library → Prompts → New prompt`, paste the template body (text below the frontmatter). API: `POST /prompts {name, template, commit_message}`. Feed it your filled manifest + review/Allure status to draft the rationale. |
| 4 | (Optional) Add blocker-triage questions | [`review/blocker-questions.json`](review/blocker-questions.json) | If you need to triage candidate blockers, create a Review Queue with these questions. API: `POST /review-queues` then `.../items` with the trace ids. Answers write back onto the trace. |
| 5 | Score the release **by hand** | [`rubric/release-rubric.json`](rubric/release-rubric.json) | Apply the weights (component_readiness 0.30, workflow_readiness 0.30, review_coverage 0.20, evidence_visibility 0.20). For each dimension, mark pass/partial/fail from the gathered evidence using the per-dimension checklist; compute the weighted score and the blocker count. The file includes a worked example. |
| 6 | Publish the decision record | [`decision/decision-record.template.md`](decision/decision-record.template.md) | Copy the template, fill the run-id list, Allure URL, per-dimension scores, blocker list (each mapped to its owning scenario), and the final decision + rationale + waiver log. |

Publish **go** only when **all three gates hold**: `blocker_count = 0`,
`overall_release_score ≥ 0.90`, and Allure is visible. Otherwise **no_go**, with
each blocker mapped to its owning scenario. You may open a blocker issue via the
GitHub MCP `create_issue` tool — keep it **approval-gated** (it is a `write`
side-effect).

## Conventions used across the pack

- **Prompt files** (`prompts/*.md`): YAML frontmatter (name, model hint,
  variables) then the literal template body. Paste the body into the authoring
  textarea; variables are `{{ snake_case }}`.
- **Dataset files** (`dataset/*.json`): here a **fill-in manifest**, not eval
  rows — the operator pastes the real run ids / review states before scoring.
- **Rubric / decision files** (`rubric/*.json`, `decision/*.md`): operator
  scoring config and the human-authored signoff record. CALIBER stores neither;
  they live with the scenario as the audit trail for the decision.
- **Review files** (`review/*.json`): the question schema for a Review Queue
  (`{key, type}` with `type` ∈ bool|int|float|str); answers write back to the
  trace as MLflow assessments.
