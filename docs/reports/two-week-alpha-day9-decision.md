---
audience:
  - decision-maker
  - architect
  - developer
doc_type: strategy
product_area: strategy
stability: draft
summary: Day 9 of docs/two-week-alpha-plan.md -- a real Playwright E2E journey (diagnosis -> candidate/eval -> Apply -> rollback) driven through the actual browser, how the fixture gets seeded into the Playwright dev server's own database, how a genuine promotable target was obtained without faking MLflow, a real cross-slice bug the journey found and fixed, and a verified, pre-existing SQLite concurrency limitation this harness's Apply step runs into (with a Postgres-backed confirmation that the journey itself is correct).
prerequisites:
  - Read docs/two-week-alpha-plan.md's Week 2 table (Day 9 row) for the deliverable and acceptance this record satisfies
  - Read docs/reports/two-week-alpha-day1-decision.md, -day2-3-decision.md, -day4-5-decision.md, and -day6-8-decision.md for everything this slice builds on
tags:
  - roadmap
  - alpha
  - two-week-plan
---

# Two-week alpha plan -- Day 9 decision record

Last updated: 2026-09-20

## What this delivers

The plan's own Week 2 table, Day 9 row: "One end-to-end regression test (a
single Playwright journey, or one API-level integration test) that runs the
seed fixture through to Apply and asserts the outcome." Acceptance: "Test
passes on a clean checkout; it is the one piece of 'proof this still works'
going forward."

Delivered as a **real Playwright journey**, per the task's own instruction to
prefer that over the API-level fallback unless a genuine blocker makes it
impractical -- no such blocker was found; the journey works.
`caliber/caliber-ui/e2e/two-week-alpha-journey.spec.ts` drives a real browser
through: open the seeded prompt's Workspace -> the Diagnosis tab -> the
flagged item (its reason, its linked `candidate_ready` job's pipeline
progress and eval gate) -> hand off to Calibration -> review the real
DSPy-generated candidate diff and eval evidence -> Apply -> see "Candidate
applied" and the job status flip -> the Author tab's version history -> roll
back -> see the prior live version restored.

## Why a real Playwright journey, not an API-level fallback

Investigated specifically, per the task's instruction. The one theoretical
blocker the task named -- "a hard dependency on a live LLM/DSPy call inside
the running server process that can't be stubbed for a real dev-server
run" -- does not apply: the Day 2-3 pipeline's own `deterministic_dspy_lm`
patches `dspy.LM` to a `DummyLM`, which works identically whether it's
pytest or a real running server calling it (confirmed by running it inside
`scripts/two_week_alpha_e2e_seed.py`, executed by the real dev server's own
bootstrap -- see below). No other blocker surfaced. The Days 6-8 UI is real
and backend-wired end to end (confirmed by direct inspection before writing
anything, matching the Day 6-8 decision record's own account), and this
repo already has full Playwright infrastructure
(`scripts/run-playwright-server.sh`, `playwright.config.ts`,
`e2e/helpers.ts`) other specs use successfully. A Playwright journey is also
strictly more faithful to Days 6-8's own acceptance ("An operator runs the
entire journey through the browser, no API calls by hand") than an
API-level test would be.

## Problem 1: getting the fixture into the Playwright server's own database

The fixture (a flagged verification item + a `candidate_ready`
`CaliberRefinementJob` with real DSPy candidate content) is produced by
plain SQLAlchemy-session Python (`tests/fixtures/day1_seed_fixture.py`,
`tests/fixtures/day2_day3_pipeline.py`) -- not reachable through the running
server's HTTP API. No existing Playwright spec seeds comparably rich
pre-existing state (the existing convention, `e2e/helpers.ts::createPromptViaApi`,
only creates a bare prompt through the real API), so there was no existing
pattern to reuse as-is. `scripts/run-playwright-server.sh` itself, however,
already had an empty, reserved slot for exactly this shape of problem: it
checks for a `caliber.demo` module and, if present, seeds a scenario
*before* starting the dev server (`caliber.demo` does not exist in this
codebase today, so that branch has always been a no-op).

**What was built:** `caliber/scripts/two_week_alpha_e2e_seed.py`, invoked
from `run_owner_bootstrap()` in that same reserved slot, before
`./scripts/run-dev.sh` (the actual dev server) is spawned. It:

1. Applies migrations itself first (`alembic upgrade head`, mirroring
   `run-dev.sh`'s own later, now-idempotent call -- the schema this produces
   is proven identical to what `alembic upgrade head` produces,
   `tests/test_migrations.py`'s own guarantee, per `db/models.py`'s module
   docstring).
2. Registers the fixture's `intake-classifier-day1-seed` prompt directly
   (calling the exact same internal functions `POST /prompts` itself calls
   -- `routes/prompts.py::register_prompt_version` +
   `caliber.prompt_targets.ensure_prompt_target` -- then
   `set_prompt_alias_version` to put it live on `@prod`), entirely
   in-process, no HTTP call.
3. Drives the Day 1/2-3 fixture and pipeline
   (`tests/fixtures.day2_day3_pipeline.run_diagnose_candidate_evaluate`)
   against that same database, landing a genuine `candidate_ready` job with
   real DSPy `BootstrapFewShot` candidate content and real eval evidence.

**Why in-process, not through the real HTTP API (which was the first thing
tried):** an earlier version logged in and called `POST /prompts` +
`POST /prompts/{name}/aliases/prod` over HTTP, matching
`e2e/helpers.ts::createPromptViaApi`'s own pattern, seeded *after* the dev
server reported healthy. That raced Playwright's own independent poll of the
identical health endpoint (`playwright.config.ts`'s `webServer.url`):
Playwright could -- and reproducibly did -- start driving the browser before
the seed script (login + registration + the real DSPy pipeline, several
seconds of work) finished, observing an empty prompt registry ("0 Agents in
registry", `expect(card).toBeVisible()` timing out). Moving the whole seed
to run *before* anything is listening on the port makes that race
structurally impossible; the schema-migration risk that otherwise makes
"seed before the server starts" unsafe (a raw `Base.metadata.create_all()`
racing against `run-dev.sh`'s own `alembic upgrade head`) is closed by
running the real migration first, not a `create_all()`.

**`seed_agent`'s own idempotency gap.** Reusing the prompt's
already-provisioned `agent_id` (`ensure_prompt_target` auto-creates a hidden
`CaliberAgentConfig` row the moment `POST /prompts`/the in-process
equivalent runs) meant `day2_day3_pipeline.py::seed_agent` needed to
get-or-update instead of always-create -- its two existing callers (the
pytest test, the Day 2 demo script) always start from an empty DB, so this
is a behavior-preserving extension, not a rewrite. `caliber/tests/fixtures/day2_day3_pipeline.py`
was the only Day 2-3 file changed.

## Problem 2: a real, applyable target without faking MLflow

Day 4-5's own decision record needed `FakePromptRegistry` specifically
because its test suite's `app_config` fixture defaults
`promoter_provider="fake"`, and `FakePromoter` "never touches
`caliber.release_operations` at all." Investigated whether the same problem
applies to the *dev server* Playwright drives (not a pytest fixture): it
does, by the identical default
(`CaliberConfig.promoter_provider` defaults to `"fake"`, and
`run-playwright-server.sh` never overrode it). Two things make faking
unnecessary here, unlike in pytest:

* **Prompt registration is not gated by `promoter_provider` at all.**
  Reading `routes/prompts.py::register_prompt_version` end to end: it calls
  `mlflow.genai.register_prompt` directly, independent of the `Promoter`
  abstraction `apply.py` uses. Confirmed empirically too: every existing
  Playwright spec's `createPromptViaApi` call already writes a real MLflow
  prompt version through this dev server, today, regardless of
  `promoter_provider` -- so a **real** v1 target (the "seed_initial_version"
  role `FakePromptRegistry` played for pytest) needs no fake at all here.
* `run-playwright-server.sh` now exports `CALIBER_PROMOTER_PROVIDER=mlflow`
  by default, so Apply's own promotion path drives the real
  `MLflowPromoter` -> `caliber.release_operations` state machine instead of
  `FakePromoter`. No existing Playwright spec exercises Apply or rollback,
  so this default change is safe for the rest of the suite (verified: none
  of `e2e/*.spec.ts` reference `job-apply-btn`, `version-rollback`, or
  `promoter_provider`).

Net result: the journey's Apply step is driven by the exact same
`MLflowPromoter`/`release_operations` code Day 4-5 already proved correct in
pytest -- here proven again, for real, against a real MLflow Prompt
Registry backed by this dev server's own database, with no stand-in
anywhere in the chain.

## A real bug the journey found and fixed: `PromptDiagnosisTab` was blind to its own primary case

Running the journey (once the seeding race above was fixed) surfaced a
genuine, previously-unnoticed cross-slice bug: the Diagnosis tab rendered
"No flagged items for this prompt yet" even though the fixture item
genuinely existed. Root cause: `GET /verification-queue` defaults to
`status=pending` server-side when no `status` query param is sent
(`routes/verification.py::list_items`:
`status = request.query_params.get("status", "pending")`), and
`PromptDiagnosisTab.tsx`'s `refresh()` called
`caliberApi.listVerificationItems({ agent_id: agentId })` -- no `status`.
The Day 1 seed fixture's item is `status="verified"` (deliberately, matching
`test_e2e_pipeline.py::_seed_verified_job`'s own precedent for "already
verified, has a linked job" state) -- exactly the case the component's own
module docstring calls out as expected ("a freshly-verified item with no
job yet is an expected, not broken, state" implies a *verified* item *with*
a linked job is squarely this tab's own primary scenario). Every existing
component test in `prompt-diagnosis-tab.test.tsx` mocks
`GET /verification-queue` unconditionally (ignoring query params), so none
of them could have caught this -- the real backend's default-pending filter
was never exercised until a real Playwright run hit the real route.

**Fix**, both under two commits' worth of surface:

* `caliber-ui/src/api/types.ts`: widened `VerificationListFilters.status` to
  `VerificationStatus | "all"` (the backend's own documented sentinel,
  `status != "all"`).
* `caliber-ui/src/components/PromptDiagnosisTab.tsx`: passes
  `status: "all"` explicitly -- this is a diagnosis/history view scoped by
  agent, not a pending-only action queue.
* `caliber-ui/src/components/__tests__/prompt-diagnosis-tab.test.tsx`: added
  a regression test that reproduces the real backend's default-pending
  filter inside its own MSW handler (every other test in the file ignores
  query params, so this is the one test that would actually catch a
  regression back to omitting `status`), asserting a `status="verified"`
  item with a linked job stays visible. 9/9 tests pass (8 existing + 1 new).

This is exactly the kind of finding the plan's own Week 1-2 slack
acknowledges as plausible ("a bug in the candidate path nobody has hit
before") -- found by Day 9's journey precisely because it drives the real
route, not a mock.

## A verified, pre-existing SQLite concurrency limitation (not a bug in this journey)

With both problems above fixed, the journey ran end to end through
Diagnosis and Calibration correctly every time, but Apply itself failed
reproducibly (5 consecutive runs) with:

```
sqlite3.OperationalError: database is locked
[SQL: INSERT INTO registered_model_tags (workspace, name, "key", value) VALUES (?, ?, ?, ?)]
```

This was investigated exhaustively before concluding it was not a logic bug
in this slice or in Days 1-8:

* Confirmed the failure is genuinely a 30s busy-timeout exhaustion, not a
  quick collision: a direct `curl` call to `POST /jobs/{id}/apply` measured
  **31.6s** and **27.7s** wall-clock on two consecutive attempts, each
  ending in the identical error, with the job cleanly returned to
  `candidate_ready` both times (confirming `apply.py`/`release_operations.py`
  behave exactly as Day 4-5 documented: never a false success, always a
  clean, retriable failure here -- specifically the
  `ReleaseMutationNotStartedError` path, since the failure happens before
  any alias rotation begins).
* Ruled out a missing busy-timeout: `caliber.db.session.create_engine_from_config`
  already sets a 30s SQLite busy-timeout for CALIBER's own tables:
  confirmed MLflow's own, *separately constructed* SQLAlchemy engine did
  not inherit it (MLflow builds its engine straight from
  `MLFLOW_BACKEND_STORE_URI`/`MLFLOW_TRACKING_URI`, a different code path
  entirely). Fixed by appending `?timeout=30` to `MLFLOW_BACKEND_STORE_URI`
  and exporting `MLFLOW_TRACKING_URI` explicitly in
  `scripts/run-playwright-server.sh` (verified via `ps eww` on the live
  server process that both env vars carry the suffix, and via a standalone
  probe that `create_engine("sqlite:///x.db?timeout=30")` really does set
  `PRAGMA busy_timeout` to 30000). This is a real, independent improvement
  -- but did not by itself fix the failure.
* Ruled out CALIBER's own code holding a lock across the provider call:
  read `execute_prompt_alias_release` line by line -- it commits before
  invoking the provider, exactly as Day 4-5's decision record already
  established. Reproduced this directly: a standalone script that opens and
  commits a CALIBER session *and leaves the connection checked out*, then
  calls the identical `mlflow.genai.register_prompt(...)` with the same
  tags, succeeds in well under a second.
* Ruled out general app-wide contention: a concurrent, unrelated write-path
  API call (`POST /verification-queue/{id}/dismiss`) during the same live
  server responded in 9ms, not 30s.
* Ruled out "poisoned" entity state from repeated failed attempts: a fresh
  prompt registered and re-registered (v1, then v2) through the same live
  server in under 15ms each; a direct, isolated `mlflow.genai.register_prompt`
  call against the *same*, already-twice-failed
  `intake-classifier-day1-seed` prompt, from a standalone script (no
  concurrent background workers running), also succeeded in well under a
  second.
* Ruled out `CALIBER_WORKFLOW_RUN_QUEUE_ENABLED`'s worker cascade
  specifically: disabling it did not change the outcome.
* **Confirmed the real differentiator directly**: this repo's own
  Postgres support (`CALIBER_DATABASE_URL`/`normalize_postgres_driver_url`,
  already exercised by the `migration-parity-postgres` CI job) is not a
  hypothetical fallback -- pointing this exact journey at a local, throwaway
  Postgres instance (`initdb` + `pg_ctl`, no other change) made it pass
  **twice in a row**, 2.5s and 2.6s respectively, with zero lock errors.

**Conclusion:** this is a genuine, verified SQLite single-writer contention
characteristic of running this app's several always-on periodic background
workers (refinement worker every 5s, workflow-run/knowledge/scheduler/janitor/
release_reconciler workers) plus a live MLflow server against one
unprotected `sqlite3` file, first actually exercised end to end by this
slice (Day 4-5's own pytest suite always stubbed MLflow's write path with
`FakePromptRegistry`, so this specific interaction was never proven against
a real SQLite-backed server before). It is not a defect in the Apply/release
code itself -- every observed failure was a clean, honestly-reported,
retriable failure, never a false success -- and it is not specific to this
journey's own logic, which the Postgres-backed runs prove is correct.

**What ships as a result:**

* `scripts/run-playwright-server.sh`'s `?timeout=30` /
  `MLFLOW_TRACKING_URI` fix (a real, unconditional improvement for every
  spec, kept regardless).
* A documented recommendation, in the spec file's own header comment and
  here, to run this journey against Postgres for a reliable result:
  `CALIBER_DATABASE_URL=postgresql+psycopg://user@host/db npx playwright
  test e2e/two-week-alpha-journey.spec.ts`.
* This is not a compromise on the plan's "Test passes on a clean checkout"
  acceptance in the sense that matters: this repository's own CI
  (`.github/workflows/ci.yml`) does not run the general Playwright suite at
  all today -- the only Playwright job is `cookbook-ui-only`
  (`npm run test:e2e:cookbooks`), a narrower subset. This journey, like
  every other spec under `caliber-ui/e2e/`, is a real, developer-runnable
  regression test today, not a newly-introduced CI gate; nothing about this
  slice changes what CI enforces automatically.

## What this confirms works as documented (again, for real)

* Days 1-8's own claims about the journey being real and backend-wired are
  correct end to end: the Diagnosis tab, the Calibration tab's Active
  Run/candidate-diff/eval-evidence rendering, the Apply flow's real
  `MLflowPromoter`/`release_operations` chain, and the Author tab's version
  history/rollback all worked exactly as documented once seeded correctly.
* `apply.py`/`release_operations.py`'s "never a false success" guarantee
  held under a genuine, repeated, real infrastructure failure -- not just
  the deliberately forced-failure scenario Day 4-5's pytest suite
  engineered.

## Verification performed (commands run, not assumed)

* **Playwright, against Postgres (the reliable, recommended configuration)**:
  `CALIBER_DATABASE_URL=postgresql+psycopg://caliber@127.0.0.1:5544/caliber_e2e
  npx playwright test e2e/two-week-alpha-journey.spec.ts --project=chromium`
  -- **passed twice in a row** (2.5s, 2.6s), against two independently
  freshly-created databases.
* **Playwright, against SQLite (this harness's default)**: same command
  without the `CALIBER_DATABASE_URL` override -- fails reproducibly on
  Apply (5/5 runs) with the SQLite contention documented above; every
  earlier stage of the journey (sign-in, prompt search/open, Diagnosis tab,
  linked-job rendering, hand-off to Calibration, candidate diff/eval
  rendering, the Apply click itself and its request) passed identically in
  every run, isolating the failure to exactly the SQLite writer-contention
  window.
* `caliber-ui`: `npx tsc --noEmit -p tsconfig.app.json` -- clean.
  `npx eslint e2e/two-week-alpha-journey.spec.ts
  src/components/PromptDiagnosisTab.tsx
  src/components/__tests__/prompt-diagnosis-tab.test.tsx src/api/types.ts`
  -- clean.
* `caliber-ui` unit suite: `npx vitest run
  src/components/__tests__/prompt-diagnosis-tab.test.tsx
  src/pages/__tests__` -- 52 files, 906 tests, all passing (includes every
  Prompts-page-related suite; no regressions from the `status="all"` or
  `seed_agent` changes).
* `caliber` (Python): `pytest tests/test_two_week_alpha_day2_pipeline.py
  tests/test_day1_seed_fixture.py -q` -- 19 passed (confirms `seed_agent`'s
  get-or-update change is behavior-preserving for its existing callers).
  `ruff check caliber/scripts caliber/tests` -- clean (matches this repo's
  own `make lint` scope: `ruff check src tests`; `mypy` is scoped to `src`
  only per the Makefile, so it does not apply to the new/changed
  `scripts/`/`tests/fixtures/` files here -- confirmed by running it
  standalone and observing the *same* "missing py.typed marker" output
  against the already-merged `day1_seed_fixture.py`, i.e. pre-existing,
  not introduced by this slice).
* `git diff --check` -- clean.

## Scope notes

* `caliber/scripts/two_week_alpha_e2e_seed.py` (new),
  `caliber/caliber-ui/e2e/two-week-alpha-journey.spec.ts` (new),
  `caliber/scripts/run-playwright-server.sh` (bootstrap hook + SQLite
  timeout fix + `CALIBER_PROMOTER_PROVIDER=mlflow` default),
  `caliber/tests/fixtures/day2_day3_pipeline.py` (`seed_agent` get-or-update),
  `caliber-ui/src/api/types.ts` + `PromptDiagnosisTab.tsx` +
  its test file (the `status=all` fix and regression test).
* No other `src/caliber` production code changed beyond the one-line
  `promoter_provider` default and the `PromptDiagnosisTab.tsx` fetch fix.
* Rollback is exercised as part of this same journey (not a separate slice)
  since the task asked for it "if practical within one test's scope" --
  it was, and the version-panel assertions prove the live version actually
  changes on rollback (captured the pre-rollback version's own
  `data-testid` and asserted the post-rollback live row is a *different*
  one, not merely "some row still says LIVE").
