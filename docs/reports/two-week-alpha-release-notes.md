---
audience:
  - decision-maker
  - architect
  - developer
doc_type: strategy
product_area: strategy
stability: draft
summary: The scope-and-limitations note for the two-week alpha checkpoint (docs/two-week-alpha-plan.md), adapted from the plan's own draft against what Days 1-9 actually built and proved, published alongside the tag the repo owner cuts for Day 10.
prerequisites:
  - Read docs/two-week-alpha-plan.md, especially "What makes this an alpha release, not just a demo" and its own draft scope-and-limitations note, which this document adapts
  - Read docs/reports/two-week-alpha-day1-decision.md through -day9-decision.md for the full journey this note summarizes
  - Read docs/reports/two-week-alpha-day10-decision.md for what Day 10 verified about the release cut itself (build reproducibility, wheel installability) and the handoff steps still required to actually publish the tag
tags:
  - roadmap
  - alpha
  - two-week-plan
  - release-notes
---

# CALIBER `v0.1.0-alpha.1` -- scope and limitations

Last updated: 2026-09-20

This is the one-page scope note the plan's own "What makes this an alpha
release, not just a demo" section calls for, adapted from that section's
draft against what Days 1-9 actually built and proved (not the draft's
aspirational wording where reality has since diverged -- see "What changed
from the plan's draft" below). It ships alongside the git tag the repo owner
cuts for Day 10 (see `docs/reports/two-week-alpha-day10-decision.md` for
exactly what was verified about the release artifact itself, and the
handoff steps still required to publish it).

## What this release supports

**One journey.** A named, seeded flagged trace (the `intake-classifier`
support-ticket classification prompt, reused from the first-milestone pilot
fixture pack) taken through: an operator opens the flagged item and sees why
it was flagged (the Diagnosis tab) -> a DSPy `BootstrapFewShot` candidate is
generated against a fixed 12-case eval set -> the operator reviews the
candidate diff and eval pass/fail evidence (the Calibration tab) -> explicit
authorized Apply -> the release reaches `applied` (live target genuinely
moved) or, on a provider failure, the honest `reconcile_required` outcome,
never a false success -> the operator can roll back and see the prior live
version restored. It is driven through the documented UI, browser-only, no
API calls by hand (`caliber/caliber-ui/e2e/two-week-alpha-journey.spec.ts`
proves this end to end).

**One fixed provider profile.** OpenAI (`llm_provider="openai"`) is the one
provider profile this journey's DSPy candidate path is wired against -- it
is the only provider implementation in this codebase that loads DSPy at all
(`docs/reports/two-week-alpha-day1-decision.md`'s provider decision).
**Stated plainly:** the proven, CI-runnable journey (the Playwright spec and
its backing pytest suites) never makes a live OpenAI network call. Diagnosis
and eval use deterministic fakes (`FakeLLMProvider`, `FakeEvalProvider`), and
the DSPy optimizer itself runs for real -- `BootstrapFewShot` genuinely
bootstraps few-shot demonstrations from the fixture's own data -- against
`dspy.LM` patched to a deterministic `DummyLM`, per this repo's standing
rule that ordinary validation stays offline
(`docs/reports/two-week-alpha-day2-3-decision.md`). This is a deliberate
choice to keep the one piece of "proof this still works" reproducible and
network-free, not a claim that a live OpenAI call was exercised end to end
as part of this alpha's own proof.

**One fixed fixture.** One trace, one prompt, one eval set, one promotable
target -- no second scenario.

## What is true today, stated plainly

- Every server operation exists behind a typed **sync** Python SDK
  (`caliber-sdk`) with **0 untracked coverage gaps** against the live API,
  confirmed by re-checking `sdk/caliber-sdk/coverage_allowlist.toml` on the
  commit this note ships against: the file's `[[gap]]` list is empty (only
  permanent `[[exclusion]]` entries remain), matching its own header's
  claim that "no gaps remain." This coverage gate
  (`caliber/tests/test_sdk_api_coverage.py`) is scoped to the sync client.
  Directly re-confirmed this journey's own five operations (verification
  queue list/verify/dismiss, job listing, job Apply, prompt rollback,
  release timeline) each resolve to a typed sync method
  (`caliber_sdk.resources.quality.VerificationAPI`,
  `caliber_sdk.resources.operations.JobsAPI.{list,apply}`,
  `caliber_sdk.resources.assets.PromptsAPI.rollback`,
  `caliber_sdk.resources.operations.ReleasesAPI.timeline`).
- **The SDK is sync-first by design, and that gap is real for this
  journey, not just a caveat about async generally.** The async client
  (`AsyncCaliberClient`) types only where async changes the outcome
  (streaming, concurrency) -- confirmed by reading its own module docstring
  and code: it covers job listing/get/wait, but **not** job Apply,
  verification-queue actions, prompt rollback, or the release timeline;
  those four of this journey's five operations are reachable only through
  `client.raw`, untyped. There is likewise **no CLI (`caliberctl`) parity
  for this journey**: it has commands for job listing/wait and
  release-candidate list/sign, but no verification-queue commands, no job
  Apply command, and no prompt-rollback command. `caliberctl`'s existing
  `workspace release apply`/`workspace release rollback` commands belong to
  the separate, architecturally distinct multi-tenant Workspace
  release-governance subsystem this alpha explicitly does not target (per
  Day 1's path decision) -- they are not parity for this journey's
  single-tenant job-apply or prompt-rollback operations, and should not be
  read as such.
- There is no contract-tested retry, cancellation, restart, or correlation
  behavior beyond what this one journey exercises. Unsupported operations
  are not guaranteed to fail cleanly.
- There is no security hardening beyond this repo's baseline auth/session
  controls, no measured workload limit, no rehearsed backup/restore or
  recovery drill, and no multi-tenant, RBAC, or multi-provider support for
  this journey.
- This journey's Apply step depends on this repository's own real
  `caliber.release_operations` state machine and a real MLflow Prompt
  Registry -- proven correct under both a deliberately forced provider
  failure (`docs/reports/two-week-alpha-day4-5-decision.md`) and a genuine,
  repeated infrastructure failure encountered while proving the browser
  journey (`docs/reports/two-week-alpha-day9-decision.md`'s SQLite
  writer-contention finding): every observed failure was a clean, honestly
  reported, retriable failure, never a false success. Running this journey
  against this harness's default SQLite-backed dev server can hit that same
  single-writer contention under Apply; running it against PostgreSQL
  (`CALIBER_DATABASE_URL=postgresql+psycopg://...`) does not. This is a
  verified characteristic of the harness's default local database, not a
  defect in the Apply/release code itself.
- The bundled SPA the wheel ships (including the Diagnosis tab this journey
  needs) is genuinely present in the built wheel today -- re-verified as
  part of Day 10, directly re-checking the specific known failure mode
  `docs/reports/product-completeness-report.md` §3c records ("the wheel
  shipped without the bundled SPA for 12 CI runs undetected"). See
  `docs/reports/two-week-alpha-day10-decision.md` for the exact commands
  and output.

## What changed from the plan's draft

The plan's own draft scope note (`docs/two-week-alpha-plan.md`, "Draft
scope-and-limitations note") is the starting point for this document. Two
things were adapted against what Days 1-9 actually built, not copied
verbatim:

1. The draft's "0 untracked coverage gaps" claim was **re-checked, not
   assumed** -- it is still true today (see above), but this note states
   how that was confirmed on this specific commit rather than repeating the
   plan's own forward-looking wording unchanged.
2. The draft did not anticipate the SQLite writer-contention finding Day 9
   surfaced, or the fact that the proven journey's DSPy path runs offline
   against a deterministic `DummyLM` rather than a live OpenAI call. Both
   are now stated explicitly above, because the plan's own standard for this
   note is "say what is true, not what is aspirational."

## What this proves and does not prove

**What this proves:** the journey completes, once, reproducibly, from a
built wheel, against a real release-governance state machine and a real
MLflow Prompt Registry -- not just from the dev checkout (see
`docs/reports/two-week-alpha-day10-decision.md` for the wheel-build and
wheel-install verification this claim rests on).

**What this does not prove:** production readiness, a live-model run of
this exact journey, or a second person's independent reproduction from the
tagged artifact -- that reproduction is the one step this alpha checkpoint
deliberately leaves to the repo owner to perform in person (see Day 10's
decision record for exactly why, and the remaining handoff steps).
Production readiness is `v1.0.0`'s job, per `docs/prd.md`.
