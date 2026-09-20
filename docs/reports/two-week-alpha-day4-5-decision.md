---
audience:
  - decision-maker
  - architect
  - developer
doc_type: strategy
product_area: strategy
stability: draft
summary: Days 4-5 of docs/two-week-alpha-plan.md -- wiring Apply -> release -> the three outcomes (applied / failed / reconcile_required) and rollback for the seed fixture, and how a real, offline, promotable prompt target was built.
prerequisites:
  - Read docs/two-week-alpha-plan.md's Week 1 table (Days 4-5 row) for the deliverable and acceptance this record satisfies
  - Read docs/reports/two-week-alpha-day1-decision.md and docs/reports/two-week-alpha-day2-3-decision.md for the fixture, path, provider, and DSPy decisions this builds on
tags:
  - roadmap
  - alpha
  - two-week-plan
---

# Two-week alpha plan -- Days 4-5 decision record

Last updated: 2026-09-19

## What this delivers

The plan's own Week 1 table, Days 4-5 row: "Wire Apply -> release -> the
three outcomes (applied / failed / `reconcile_required`) and rollback,
backend only." Acceptance: "Apply against the seed target succeeds once; a
forced-failure test shows `reconcile_required`, not a false success;
rollback restores prior state."

All three are met by:

* `caliber/tests/fixtures/day4_day5_pipeline.py` -- a shared driver module
  providing `FakePromptRegistry`, a stateful, offline stand-in for the MLflow
  Prompt Registry surface `caliber.promoter.MLflowPromoter` and
  `caliber.routes.prompts` depend on, plus `mlflow_promoter_app_config`, a
  one-line `app_config` override that points the test app's `Promoter` at the
  real `MLflowPromoter` instead of the suite-default `FakePromoter`.
* `caliber/tests/test_two_week_alpha_day4_5_pipeline.py` -- the CI-enforced
  regression test. Three scenario classes drive a real, DSPy-generated
  `candidate_ready` job (built on Day 2-3's `run_diagnose_candidate_evaluate`)
  through the real HTTP API:
  * `TestApplySucceeds` -- Apply promotes the real candidate; the release
    reaches `applied`; the live target genuinely moved (registered version 2,
    alias rotated); a second Apply on the same job is rejected (409), not
    silently re-promoted.
  * `TestApplyForcedFailureReconcileRequired` -- a provider failure forced
    *after* the new version is registered but *before* the alias rotation
    lands ends at `reconcile_required`, never a false `applied`; the job
    stays at the honest, non-terminal `applying` status; the live target is
    provably untouched; and the periodic reconciler (exercised via its own
    HTTP trigger) closes the loop, settling the operation `failed` and
    returning the job to `candidate_ready` for a clean retry.
  * `TestRollbackRestoresPriorState` -- after a successful Apply, rollback
    restores the exact prior version, verified by reading the live target
    back independently of the rollback response; a prompt with no recorded
    prior promotion is refused (409), never guessed.
  * `TestFakePromptRegistry` -- direct unit coverage of the fake registry's
    own edge cases (unset alias, unsupported ref, a version-less
    `set_prompt_alias` call, the single-shot forced-failure lever), so a bug
    in the fake itself can't silently undermine confidence in the three
    scenario classes above.

No production code in `src/caliber` needed a fix -- Apply, the release
state machine, and rollback all behaved exactly as `apply.py`,
`promoter.py`, and `release_operations.py` document. See "What this
confirms works as documented" below.

## The tension this slice resolves: a real target, offline

Day 2-3's tension was "prove the real DSPy path runs" against "stay
deterministic and offline." Day 4-5's parallel tension: the plan's own
acceptance demands Apply succeed "against the seed target" -- a *real*
promotable target, not a stand-in that bypasses the release machinery this
slice exists to prove -- while every other slice of this plan stays offline,
with no live MLflow server and no network call.

**Confirming which promoter path this actually needs.** Reading
`apply.py`, `promoter.py`, and `bundle.py` end to end: the Day 1/2-3 seed
job's candidate is a plain prompt (`job.artifact_type == "prompt"`, no
`promotion_type` on the candidate payload), so `apply_candidate` always
dispatches to `_apply_bundle` -> `resolve_bundle_targets` (a single target,
since `job.bundle_targets` is empty) -> `caliber.bundle.promote_bundle` ->
whatever `Promoter` the app is configured with. The test suite's own
`app_config` fixture defaults `promoter_provider="fake"` (`FakePromoter`),
which is right for most of this repo's tests but wrong here: `FakePromoter`
never touches `caliber.release_operations` at all, so a test built on it
would never prove the `prepare_prompt_alias_release` /
`execute_prompt_alias_release` intent-first state machine this slice's
acceptance criteria are actually about. Day 4-5's tests instead configure
`promoter_provider="mlflow"` (`caliber.promoter.MLflowPromoter`) -- the one
promoter that genuinely drives that state machine (confirmed by reading
`promoter.py:194-234`, matching this session's own prior research).

**Building a real target without a real MLflow server.** `MLflowPromoter`
and `caliber.routes.prompts` both reach the MLflow Prompt Registry through a
small, consistent surface: `mlflow.genai.register_prompt`,
`mlflow.genai.set_prompt_alias`, `mlflow.genai.load_prompt`. This repo
already has two established, non-network ways to stand that surface in for
tests: `tests/test_promoter.py` stubs `sys.modules["mlflow"]` per call with
one-shot canned returns, and `tests/test_routes_prompts.py::_install_mlflow`
stubs it with a static, hand-updated `load_refs` dict that each test
re-installs to reflect the next expected read. Both are correct for what
they test: a single promote/rollback call in isolation.

Day 4-5 needs something those two patterns don't provide: proof that a
promotion's own effects are what an *independent, later* call reads back --
Apply registers v2 and rotates the alias, and then, without any test code
manually re-scripting the expected state, a separate `POST
/prompts/{name}/rollback` HTTP call must observe that real v2 and restore
v1. `FakePromptRegistry` (`day4_day5_pipeline.py`) extends the existing
stub-`sys.modules` technique with genuine internal state instead of a
static dict: a per-prompt-name version history (monotonically increasing,
matching the real registry semantics `MLflowPromoter` itself documents
relying on) and a `(name, alias) -> version` alias table that
`register_prompt` / `set_prompt_alias` / `load_prompt` all read and write
consistently. This is what makes the apply -> rollback round trip in
`TestRollbackRestoresPriorState` a genuine end-to-end proof rather than two
independently-asserted expectations -- while staying exactly as offline and
deterministic as the two existing patterns it builds on.

**Seeding a real "v1" before the journey runs.** A brand-new,
never-promoted prompt has no prior version (`apply.py::_build_checkpoint`'s
own "v1 promotions... return `None`" comment, and `MLflowPromoter.rollback`
refuses a `version_before=None` checkpoint outright). Rollback would have
nothing to demonstrate against a cold-start target. Every Day 4-5 test that
needs a promotable target therefore calls
`FakePromptRegistry.seed_initial_version` first, registering the Day 1
fixture's own `intake-classifier` prompt template
(`day1_seed_fixture.load_prompt_template()`) as version 1 on the `prod`
alias -- the fixture's target genuinely exists and is genuinely live before
Apply runs, exactly as the plan's "Apply against the seed target" wording
implies.

## Engineering the forced-failure scenario

The plan's acceptance requires a forced-failure test that shows
`reconcile_required`, "not a false success." Reading
`release_operations.execute_prompt_alias_release` end to end: it commits
`applying` *before* invoking the provider mutation, then on any exception
raised *during or after* that call, marks the operation
`reconcile_required` and re-raises -- it never both mutates the alias and
reports failure, and it never silently reports success when the mutation
raised. This is exactly the "indeterminate outcome" window a real MLflow
alias-rotation failure occupies: the new version is already registered
(irrevocable -- the registry keeps every version), but whether the alias
itself moved before the provider call failed is genuinely unknown to the
caller.

`FakePromptRegistry.raise_on_next_set_alias` reproduces that exact window:
setting it to an exception instance makes the *next* `set_prompt_alias`
call raise instead of mutating the alias table, consumed exactly once (so a
retry after reconciliation succeeds normally). Because `register_prompt`
and `set_prompt_alias` are two separate registry calls -- matching real
MLflow's own two-call promotion shape -- forcing the failure this way means
the new version genuinely gets registered (`registry.register_calls` proves
it) while the alias genuinely does not move (`registry.current_version`
proves it), reproducing the precise gap
`release_operations.py`'s own module docstring describes: "Any exception
after the provider call begins is recorded as `reconcile_required`."

`TestApplyForcedFailureReconcileRequired` drives this through the real HTTP
stack (`POST /jobs/{id}/apply` -> `apply_candidate` -> `_apply_bundle` ->
`promote_bundle` -> `MLflowPromoter.promote` -> `execute_prompt_alias_release`),
asserting at every layer that observed:

1. the HTTP response is `502`, not `200` -- the caller is never told Apply
   succeeded;
2. the job's own status is the honest, non-terminal `applying` -- not
   `applied`, and not silently rewound to `candidate_ready` either;
3. the `CaliberReleaseOperation` row is `reconcile_required`, with the
   provider's error message recorded on `last_error`;
4. the live registry target is unchanged (`current_version` still 1);
5. and, closing the loop, calling the existing
   `POST /releases/operations/reconcile` endpoint (which this repo's release
   reconciler already exercises on a 60s sweep in production --
   `orchestrator/release_reconciler.py`) observes the alias never moved,
   settles the operation `failed` (not `applied`), and returns the job to
   `candidate_ready` for a clean retry.

This is new test composition, not new production code: `release_operations.py`
already has dedicated unit tests for the `reconcile_required` transition in
isolation (`test_release_operations.py::test_provider_error_leaves_reconciliation_obligation`)
and for the reconciler's own settlement logic
(`test_reconciler_returns_the_linked_job_to_candidate_ready_when_release_was_not_applied`).
What was missing, and what this slice adds, is the same guarantee proven
through the *real* `apply_job` route and the *real* `MLflowPromoter`, not
just at the `release_operations` function-call level -- the composition the
task brief explicitly called out as new work.

## What this confirms works as documented

Reading `apply.py`, `promoter.py`, and `release_operations.py` against the
plan's own grounding claim ("the core release-governance machinery...
confirmed by direct inspection, not assumed"), and now against a real,
passing, end-to-end test: no bug was found. Specifically confirmed working
as documented, not merely as claimed:

* `MLflowPromoter.promote()` captures `version_before` from the alias's
  *actual* live state before rotating (not `version_after - 1`), so the
  rollback checkpoint and the `promote_prompt` audit row both record the
  exact outgoing target.
* `apply_job`'s conditional-UPDATE claim (`candidate_ready` ->
  `applying`) correctly rejects a second Apply on the same job with 409,
  without ever calling the promoter a second time.
* `rollback_prompt`'s audit-trail walk (`_previous_live_version`) correctly
  resolves the exact prior version from the `promote_prompt` row Apply's own
  `MLflowPromoter.promote()` writes via `execute_prompt_alias_release` --
  the Apply path and the rollback path agree on the same audit shape with no
  glue code needed between them.
* `execute_prompt_alias_release` never mutates the alias and reports
  failure in the same call -- the forced-failure scenario's registry state
  (new version registered, alias untouched) is exactly what the state
  machine's own commit-before-effect design predicts.

This slice is therefore "wire and prove," matching the plan's own framing --
not "build from zero," and not "fix a bug nobody had hit before" either,
though the plan's own slack acknowledges either outcome was plausible going
in.

## Scope notes

* No `src/caliber` production code was changed. Every file in this slice is
  new test/fixture code (`caliber/tests/fixtures/day4_day5_pipeline.py`,
  `caliber/tests/test_two_week_alpha_day4_5_pipeline.py`, and this record),
  matching the pattern Days 1 and 2-3 already established.
* Per the plan's own Day 4-5 row ("backend only," with no "script" framing
  the way Days 2-3's row has), this slice does not add a standalone demo
  script. Day 2-3's script exists because its own acceptance wording is
  "running one script... produces a scored candidate" -- a literal script
  requirement. Day 4-5's acceptance is phrased entirely in terms of what
  Apply/rollback *do* ("succeeds," "shows `reconcile_required`,"
  "restores prior state"), which the CI-enforced test itself proves; a
  script would only re-narrate the same three HTTP calls the test already
  makes, with no independent evidence value the test doesn't already carry.
* `TestFakePromptRegistry`'s inclusion is a deliberate coverage decision
  (not a formal production-coverage gate, since no `src/caliber` line
  changed): a bug in the fake registry's own edge-case handling could
  silently produce a false-positive journey test, so its edge cases
  (missing alias, unsupported ref, a version-less `set_prompt_alias` call,
  the single-shot forced-failure lever) get direct unit coverage rather than
  being exercised only incidentally.
