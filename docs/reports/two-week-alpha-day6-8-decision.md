---
audience:
  - decision-maker
  - architect
  - developer
doc_type: strategy
product_area: strategy
stability: draft
summary: Days 6-8 of docs/two-week-alpha-plan.md -- wiring the diagnose -> candidate -> evaluate -> Apply -> release/rollback path into the existing UI, and the one real gap this slice closed (a place to open a flagged item and see why it was flagged).
prerequisites:
  - Read docs/two-week-alpha-plan.md's Week 2 table (Days 6-8 row) for the deliverable and acceptance this record satisfies
  - Read docs/reports/two-week-alpha-day1-decision.md, two-week-alpha-day2-3-decision.md, and two-week-alpha-day4-5-decision.md for the fixture, path, provider, DSPy, and Apply/rollback decisions this builds on
tags:
  - roadmap
  - alpha
  - two-week-plan
---

# Two-week alpha plan -- Days 6-8 decision record

Last updated: 2026-09-19

## What this delivers

The plan's own Week 2 table, Days 6-8 row: "Wire the same path into the
existing UI pages (diagnosis, candidate/evaluation, Apply, release/rollback
views)." Acceptance: "An operator runs the entire journey through the
browser, no API calls by hand."

## Starting point: most of the UI already existed

Direct inspection of `caliber/caliber-ui/src/pages/Prompts.tsx` and
`caliber/caliber-ui/src/pages/Releases.tsx` before writing any code confirmed
this session's earlier research: the candidate/evaluate/Apply and
release/rollback ends of the journey are real, backend-wired UI today, not
just backend endpoints:

* `PromptOptimizationTab` (the per-prompt Workspace's **Calibration** tab)
  already renders an "Active Run" panel driven by `GET /jobs?agent_id=...`,
  polls a running job every 2s (`caliber-ui/src/pages/Prompts.tsx:6642-6680`),
  shows the candidate diff and eval score comparison
  (`CalibrationApplyReviewDialog`), and Applies through the real
  `POST /jobs/{id}/apply` endpoint (`data-testid="job-apply-btn"`).
* `caliber-ui/src/pages/Releases.tsx` is a real, DB-backed timeline/rollback
  hub over `routes/releases.py`, and rollback is wired end to end through
  `components/versioning/adapters.ts` -> `caliberApi.rollbackPrompt` ->
  `POST /prompts/{name}/rollback`.
* Critically, `PromptOptimizationTab`'s existing run-restore logic
  (`refreshRuns`, `caliber-ui/src/pages/Prompts.tsx:6566-6610`) already
  queries jobs by `agent_id` and auto-selects the most recent non-terminal
  (or most recent, if all terminal) job as the "Active Run" on every mount --
  including a page reload. This means the Day 1 seed fixture's job (seeded
  against a fixed `agent_id`, advanced to `candidate_ready` by the Day 2-3
  pipeline) already surfaces in this tab with zero new plumbing, the moment
  an operator opens that prompt's Workspace -> Calibration tab. Nothing
  needed to change here to satisfy "operator sees candidate generation... and
  eval pass/fail evidence... clicks Apply."

## The one real gap: nowhere to open a flagged item and see why it was flagged

`caliber-ui/src/api/caliberApi.ts` already has full typed methods for the
verification queue (`listVerificationItems`, `getVerificationItem`,
`verifyItem`, `dismissItem`, `markDuplicate`, `batchVerificationAction`), and
`PipelineProgress` (`caliber-ui/src/components/PipelineProgress.tsx`) already
renders the exact triage -> evidence -> diagnosis -> candidate -> eval stage
indicator this journey's first steps need. Neither was reachable from
anywhere in the SPA: `Overview.tsx` reads only the aggregate
`verification_pending` count, and `PipelineProgress` was imported nowhere
outside its own test file. `pages/ReviewQueues.tsx` (routed at
`/review-queues`) is a different, unrelated feature -- MLflow-style
structured trace review with a reviewer-defined label schema, not the
`CaliberVerificationItem` "flagged concern" queue this journey's step 1-2
("operator opens it, sees why it was flagged") needs.

This is the gap this slice closes.

## What was built

**`caliber-ui/src/components/PromptDiagnosisTab.tsx`** -- a new component,
and a new **Diagnosis** tab on the per-prompt Workspace
(`caliber-ui/src/pages/Prompts.tsx`), positioned between **Runs** and
**Calibration** (pipeline order: triage/evidence/diagnosis precede
candidate/eval, which the existing Calibration tab already owns). It:

* Lists `GET /verification-queue?agent_id=<prompt.agent_id>` items for the
  open prompt's agent, newest first, each showing category, severity
  (`SeverityBadge`), and status (`StatusBadge`).
* On selecting an item, renders its full reason (`free_text`), trace/session
  id, artifact ref, and who verified it -- the "sees why it was flagged" step
  the plan names explicitly.
* Looks up the refinement job diagnosing that item by
  `RefinementJob.primary_item_id === item.item_id` (`GET
  /jobs?agent_id=<prompt.agent_id>`, filtered to `artifact_type === "prompt"`)
  -- the same FK `day1_seed_fixture.py::seed_flagged_job` sets when it seeds
  the item + job pair. When a job is linked, it renders `PipelineProgress`
  with that job's live `current_stage`/`status`, plus a compact eval summary
  (overall score, gate passed/failed) read off `job.eval_results`, and a
  button that switches the Workspace to the Calibration tab -- where the
  same job is already the Active Run, per the "starting point" section above.
* For a `pending` item (the general, not-pre-linked case
  `routes/verification.py` documents), exposes **Verify** and **Dismiss**
  buttons wired to the existing `caliberApi.verifyItem` /
  `caliberApi.dismissItem` methods, so an operator can process a flagged item
  from this tab without leaving it -- even though, per that route's own
  documented limitation, verifying a general item does not itself create a
  job (job creation happens in the four existing job-creation paths, which
  is out of scope here and unchanged).
* Handles empty (`"No flagged items for this prompt yet"`), loading, and
  error states, following this file's own existing patterns elsewhere
  (`apiErrorText`, manual `useState`/`useEffect`/`AbortController` fetch
  effects, matching `PromptRunsStage`'s own style in the same file).

## Why this integration point, not a new page

The task brief named two candidates: a tab/panel on the per-prompt Workspace,
or a standalone page. The per-prompt Workspace won on every axis actually
checked against this repo's own conventions:

* **Routing convention.** Every route in `App.tsx` is a top-level page (no
  nested/detail sub-routing for this kind of per-artifact drill-down); the
  established pattern for "focus on one artifact and walk it through several
  pipeline-adjacent views" is already the Workspace's own tab set (Author,
  Playground, Test Sets, Runs, Calibration, Bind) -- not a new route. Adding
  a seventh tab follows the grain; a new `/diagnosis/:agentId` route would
  fight it (a second place with its own header/back-button/loading chrome
  duplicating `PromptWorkspace`'s existing one).
* **Least awkward plumbing.** A new page would need its own way to resolve
  "which prompt does this verification item belong to" and then hand off to
  the Workspace anyway to reach Calibration -- two navigations and two
  fetches for what is naturally one scoped view. As a Workspace tab, the
  `prompt` (and therefore `agent_id`) is already in scope, and "continue to
  Calibration" is a one-line `setStage("calibration")` call already available
  on the parent component -- no route param, no lookup, no second fetch.
  This is exactly the "requires the least structurally-awkward plumbing"
  criterion the task brief asked to weigh.
* **Matches `PipelineProgress`'s own intended scope.** Its doc comment
  describes the calibration/optimization pipeline for *one job*; the
  Workspace is already job-scoped by prompt, so the component's existing
  props (`currentStage`, `status`) plug in directly with no new adapter.

A standalone page was rejected specifically because the two most-invoked
downstream actions -- "trigger candidate refinement" and "click Apply" --
already live one tab away in the same Workspace; a separate page would need
to either duplicate that surface or send the operator on an extra hop the
plan's own acceptance ("no API calls by hand," implicitly: no more clicks
than the journey needs) argues against.

## What this does not change

No backend code changed. `caliberApi.ts`'s verification-queue and jobs
methods, `PipelineProgress`, `PromptOptimizationTab`, and
`caliber-ui/src/pages/Releases.tsx` are used exactly as they already existed
-- this slice is wiring, matching the "wire the same path into the existing
UI pages" framing of the plan's own Days 6-8 row, not new product surface
beyond the one missing tab.

## Verification

* **New component tests**
  (`caliber-ui/src/components/__tests__/prompt-diagnosis-tab.test.tsx`, 8
  tests, MSW-mocked `caliberApi` calls): empty state; a load error surfaced
  without crashing; opening a flagged item and reading its reason/trace id;
  the linked-job pipeline-progress + eval-evidence render and the
  "Review candidate in Calibration" hand-off calling `onOpenCalibration`;
  the no-linked-job fallback message and its own hand-off button; switching
  between two flagged items; Verify and Dismiss both succeeding and updating
  the rendered state; and a Verify failure surfacing an error without
  blanking the detail panel.
* **Existing suites re-run unmodified and green**: `prompts.test.tsx` (70
  tests), `prompts-advanced-flows.test.tsx` (16), `prompts-test-run-history.test.tsx`
  (4), `prompts-utils.test.ts` (10), `prompts-identity-utils.test.ts` (2),
  `sidebar-build-routes.test.tsx` (1 -- opens every wired sidebar route,
  confirming the new tab didn't regress routing), and
  `pipeline-progress.test.tsx` (7) -- 110 tests, all passing, confirming the
  new tab and its wiring didn't regress the existing Workspace or
  `PipelineProgress`.
* **Type check**: `npx tsc --noEmit -p tsconfig.app.json` -- clean.
* **Lint**: `npx eslint .` -- clean.
* **Build**: `npm run build` -- succeeds (`vite build`, `Prompts-*.js` chunk
  emitted; the pre-existing `INEFFECTIVE_DYNAMIC_IMPORT` warning about
  `caliberApi.ts` predates this change and already names several other
  static importers).
* **Full suite with coverage**: `npm run test:coverage` (CI's own command,
  `.github/workflows/ci.yml`'s "UI (test + build)" job) -- run against this
  branch; see the PR's validation-commands section for the exact pass/fail
  result and coverage numbers against the repo's ratcheted thresholds
  (lines 89% / statements 87% / functions 87% / branches 78%).
* **Manual dev-server check**: not performed against a live backend in this
  slice -- the component-test suite above exercises the real request/response
  shapes (`VerificationItem`, `RefinementJob`, including a `candidate_ready`
  job with `eval_results.gate.passed`) against the actual `caliberApi`
  client and MSW, which is the more reliable check available in this
  environment (per the task's own guidance to prefer whichever is more
  reliable here) since standing up the Python backend, seeding the Day 1
  fixture, and pointing a dev Vite server at it is exactly the Day 9
  Playwright journey's job, not this slice's.

## Explicit non-goals for this slice

The Day 9 Playwright end-to-end journey test (browser-driven, asserting the
full click-through from a seeded flagged item to Apply to rollback) is
explicitly deferred to the next slice, per the task brief. This slice's job
was to make sure that journey has something real to drive; it does not
itself add browser automation.
