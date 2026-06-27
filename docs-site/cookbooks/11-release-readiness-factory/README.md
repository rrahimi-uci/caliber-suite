# Release Signoff Factory

## Demo objective

A release control lane that combines scenario outcomes, review findings, and
Allure evidence into a final, evidence-backed go/no-go decision.

## Feasibility & substitutions

Read [`../FEASIBILITY.md`](../FEASIBILITY.md). Key points:

- ✅ The **evidence sources** are all real: **Evaluations** run ids + scorecards,
  **Review Queues** completion, **Observability** traces, and the in-app
  **Allure** report (`Settings → Allure Report`).
- ⚠️ There is **no built-in release-scoring engine**. The `release_rubric` in
  [`verification.yaml`](verification.yaml) is computed **by the operator** —
  tally the gate states from the prior scenarios. Optionally automate the
  narrative with a `release-risk-summarizer` prompt, or an **Aria Plan** (Aria
  can create judges/review-queues but does not score releases itself).
- ⚠️ **Allure is generated externally** (`make allure-report`) and only *served*
  in-app; regenerate before the demo (FEASIBILITY §1, Allure). The route is
  `GET /observability/allure-report`.
- Optional GitHub MCP can create a blocker issue (write → keep approval-gated).

## Prerequisites & seed

- Completed SCN-07/08/09/10 with saved run ids (list them in
  [`test-data.yaml`](test-data.yaml)).
- Allure results generated so the report loads.

## Recipe (UI-first, with API fallbacks)

1. **Collect required run ids.** From `Evaluate → Evaluations`, gather the
   scorecard run ids for each required scenario; from `Observe → Review Queues`,
   confirm each required queue is fully answered.
2. **Refresh + open Allure.** Run `make allure-report` (combines vitest +
   playwright + pytest allure results). Then `Settings → Allure Report` → confirm
   the report **loads** in-app. Capture the URL.
   - The tab reads a stored URL (`caliber.allure.reportUrl`); default is the
     backend-served `/observability/allure-report/`.
3. **Re-run critical slices.** Re-execute the high-risk subsets (e.g. SCN-07
   approval branch, SCN-08 rollback path) to confirm they still pass; record the
   fresh run ids.
4. **Score the release (operator rubric).** Apply the weights from
   verification.yaml: component_readiness 0.30, workflow_readiness 0.30,
   review_coverage 0.20, evidence_visibility 0.20. For each, mark pass/partial/
   fail from the gathered evidence and compute the weighted score + blocker
   count. (Optionally have a `release-risk-summarizer` prompt or an Aria Plan
   draft the rationale from your notes.)
5. **Decide + record.** Publish **go** only if `blocker_count = 0`,
   `overall_release_score ≥ 0.90`, and Allure is visible. Otherwise **no_go**
   with each blocker mapped to its owning scenario. Optionally open a blocker
   issue via the GitHub MCP (approval-gated).

## Demo evidence to capture

- The scenario run-id list used for the decision.
- The Allure report URL + a successful in-app load.
- Final release score, blocker count, and the decision record (with per-blocker
  owning scenario).

## Done when / gate

- No unresolved blockers (`blocker_count = 0`), `overall_release_score ≥ 0.90`,
  Allure visible.
- Every gate state maps back to a concrete run id / review answer / Allure result.
