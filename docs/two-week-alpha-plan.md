---
audience:
  - decision-maker
  - architect
  - developer
doc_type: strategy
product_area: strategy
stability: draft
summary: A proposed, lighter, earlier checkpoint than v0.1.0 -- one seeded journey, tagged and installable, in two weeks.
prerequisites:
  - Read docs/roadmap.md and docs/prd.md for the approved v1.0.0 plan this proposes a lighter run-up to
reviewed_on: 2026-09-19
version_applicability: proposal, not yet approved; no roadmap or PRD change until accepted
tags:
  - roadmap
  - alpha
  - proposal
  - two-week-plan
---

# Two-week alpha plan

## Status

**Proposal, not an approved plan.** This does not change `docs/roadmap.md` or
`docs/prd.md`. If accepted, the change-control rule those documents already
state still applies: any material change to the supported journey, scope, or
milestones gets folded into the roadmap, the PRD, and the GitHub Project
hierarchy in the same pull request. Until then, this is a standalone proposal
for a lighter, earlier checkpoint than `M1`'s pilot or `M4`'s `v0.1.0`
controlled-pilot gate.

## Why this exists

`docs/roadmap.md`'s own M1-M4 critical path is scoped at 35 person-days
against the plan's 1.5-developer assumption -- roughly three and a half
months. That is the right scope for a supportable `v0.1.0`. It is not
achievable in two weeks, and trying to compress it by cutting proportionally
across every M2-M4 workstream would produce a partial implementation of the
full architecture, not a working product. This proposes a different shape: one
seeded scenario, proven once, packaged as a real tagged release, with
everything about *supportability* -- contract testing, failure taxonomy,
SDK/CLI breadth, security, recovery, performance -- explicitly deferred to the
milestones that already own them.

**Grounding, not guessing.** The core release-governance machinery this
journey needs already exists in the current codebase (`release_operations.py`,
`routes/releases.py`, `workflows/deploy_gate.py`, `workflows/effect_ledger.py`,
`trace_client.py`, `llm/dspy_optimizer.py`, `assistant/service.py`) --
confirmed by direct inspection, not assumed from the roadmap's own description.
The 2026-08 product-completeness report found this machinery real, with known
bugs and unproven end-to-end reliability, not vaporware. That changes the
shape of the two weeks: this is mostly *wire, seed, and prove one path*, not
*build from zero*.

**One open item before work starts.** The codebase currently has two parallel
release-governance subsystems: the roadmap's own single-tenant path above, and
a separate, newer, more elaborate one built for a different multi-tenant
"Workspace" initiative (`workspace_release_service.py` /
`workspace_release_governance.py`, with evaluation gates, quality-signoff,
approve, and break-glass). They are architecturally distinct. This plan
targets the roadmap's own path -- the one the PRD and roadmap actually
reference -- not the Workspace one. Confirm this before day 1; building on the
wrong one wastes the two weeks.

## The one journey

Narrowed from the PRD's own supported-journey definition to a single fixed
scenario, driven start to finish through the UI:

```text
one seeded flagged trace (one fixture, committed to the repo)
  -> operator opens it, sees why it was flagged (existing diagnosis view)
  -> operator triggers candidate refinement (existing DSPy/candidate path)
  -> candidate runs against a fixed, small eval set; operator sees pass/fail evidence
  -> operator clicks Apply -- one explicit, auditable action
  -> result is either "applied" (live target changed, visible) or "reconcile_required"
     (labelled as such, never reported as a silent success)
  -> operator can roll back and see the prior state restored
```

One trace, one prompt, one eval set, one target, one provider profile, one
authorized-operator check. No environment matrix, no multi-provider choice,
no automation surface required to complete it -- a human, once, reproducibly,
through the browser.

## Essential, simplify, or defer

| From the plan | Treatment here | Why |
| --- | --- | --- |
| One governed journey: trace -> diagnose -> candidate -> evaluate -> apply -> release/rollback | **Essential**, narrowed to the one fixed seed above | This is the entire point; everything else exists to prove this once |
| M2's "durable... across every stage," full effect-ledger reconciliation semantics | **Simplify**: persist state so a restart doesn't lose the operator's decision; handle exactly three outcomes (applied / failed cleanly / `reconcile_required`) | Full reconciliation (duplicate Apply, timeout-before/after-effect, every retry path) is real engineering, not needed to show the journey once |
| M3's API/SDK/CLI contract, failure taxonomy, correlation IDs, readiness diagnostics | **Defer entirely** | Supportability infrastructure for other engineers/operators to automate against later -- a release needs the UI to work and an honest scope note, not a contract-test matrix |
| M3's worker/topology decision (`#151`) | **Defer** | Only matters at scale/HA; irrelevant to one operator running one journey once |
| M4's clean packaging, docs parity, bug bash, human decision record | **Simplify to a checklist**: clean-checkout build, a working wheel, one README paragraph, one scope note -- skip the formal bug-bash/decision ceremony | The ceremony matters for a real controlled-pilot decision; for a two-week alpha it's overhead the release doesn't need yet |
| Provider-profile selection (`#154`), multi-provider matrix | **Simplify**: pick the one provider path that already works in tests | Choosing and verifying a provider matrix is exploratory work that can eat days by itself |
| Full RBAC, roles, project isolation (M5, `#122-125`) | **Defer** | One hardcoded "authorized operator" check demonstrates "explicit authorized Apply is a human decision" without building a role system |
| Security boundary, backup/restore, workload measurement, recovery drills (M5/M6) | **Defer entirely** | Makes the journey safe to run in production, a different question from demonstrating it once |
| SDK/CLI parity for this journey | **Defer**; the UI satisfies "demonstrate core product value" on its own | A typed SDK path is a stretch goal, not core to this checkpoint |

## The two weeks

**Week 1 -- prove the backend path is real**

| Day | Deliverable | Dependency | Acceptance |
| --- | --- | --- | --- |
| 1 | Confirm which release-governance path this targets (the roadmap's, not Workspace's); pick the one provider profile; write the one fixed seed fixture (trace + prompt + eval set) | none | Fixture committed; provider choice written down in one paragraph |
| 2-3 | Wire diagnosis -> candidate -> evaluate for the seed fixture, backend only (script/API call, not UI yet) | Day 1 fixture | Running one script against a clean checkout produces a scored candidate, reproducibly |
| 4-5 | Wire Apply -> release -> the three outcomes (applied / failed / `reconcile_required`) and rollback, backend only | Days 2-3 | Apply against the seed target succeeds once; a forced-failure test shows `reconcile_required`, not a false success; rollback restores prior state |

**Week 2 -- make it demoable, then cut and publish the release**

| Day | Deliverable | Dependency | Acceptance |
| --- | --- | --- | --- |
| 6-8 | Wire the same path into the existing UI pages (diagnosis, candidate/evaluation, Apply, release/rollback views) | Week 1 backend | An operator runs the entire journey through the browser, no API calls by hand |
| 9 | One end-to-end regression test (a single Playwright journey, or one API-level integration test) that runs the seed fixture through to Apply and asserts the outcome | Days 6-8 | Test passes on a clean checkout; it is the one piece of "proof this still works" going forward |
| 10 | **Cut the release**: git tag, wheel build from the tag, a second person installs from the wheel/tag (not the dev checkout) and runs the journey; publish the one-page scope note (below) alongside the tag | All above | A second person reproduces the journey from the tagged wheel using only the scope note and README |

Total: ten working days, leaving slack for the surprise the completeness
report already predicts is likely -- a fixture that doesn't actually flag, or
a bug in the candidate path nobody has hit before.

## What makes this an alpha release, not just a demo

Three things, none of which require new engineering:

- **Tagged.** A real git tag (e.g. `v0.1.0-alpha.1`), not a commit hash someone has to be told about.
- **Installable by someone else.** The wheel build already exists and is already proven in CI (`Wheel build (with bundled SPA)`); day 10 points it at the tag instead of a dev checkout, and a second person -- not the author -- installs and runs the journey from it.
- **Honestly scoped in writing.** The one-page note below ships with the tag. This is the release notes doing the PRD's own job at alpha size: say what is true, not what is aspirational.

### Draft scope-and-limitations note

> **CALIBER `v0.1.0-alpha.1` -- scope and limitations**
>
> This release supports **one journey**: a named, seeded flagged trace taken
> through verification, diagnosis, candidate creation, evaluation, explicit
> authorized Apply, and release, reconciliation, or rollback, against **one
> fixed provider profile** and **one fixed fixture**. It is driven through the
> documented UI.
>
> **What is true today, stated plainly:**
> - Every operation this journey needs is reachable through the typed sync
>   Python SDK (confirmed: 0 untracked coverage gaps against the live API).
>   The SDK is **sync only** -- no async client parity, and no CLI parity for
>   this journey's operations.
> - There is no contract-tested retry, cancellation, restart, or correlation
>   behavior beyond what this one journey exercises. Unsupported operations
>   are not guaranteed to fail cleanly.
> - There is no security hardening, no measured workload limit, no rehearsed
>   backup/restore or recovery drill, and no multi-tenant, RBAC, or
>   multi-provider support.
>
> **What this proves:** the journey completes, once, reproducibly, from a
> clean install. **What this does not prove:** production readiness. That is
> `v1.0.0`'s job, per `docs/prd.md`.

## Relationship to the existing roadmap

This is not a replacement for `M1`'s pilot or `M4`'s controlled-pilot gate --
it is evidence that feeds `M1`'s own exit criteria ("MVP discovery evidence is
captured and approved") earlier and cheaper than running the full pilot
discovery process cold. If this two-week checkpoint surfaces a fixture problem
or a real bug in the candidate/evaluate/apply path, that is exactly the kind
of finding `M1` exists to catch -- just found in week one instead of month
one. Nothing here shortens `M2` through `M6`; it de-risks the assumption
those milestones are built on.

## Explicit non-goals for these two weeks

Failure taxonomy, correlation IDs, readiness probes, SDK/CLI contract tests,
async SDK parity, multi-provider support, RBAC/project isolation,
backup/restore, performance/workload limits, recovery drills, the formal
bug-bash/decision ceremony, and any second scenario or fixture. All of it
stays exactly as scoped in the existing `M2`-`M6` plan for later.
