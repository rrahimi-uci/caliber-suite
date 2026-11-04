# D5 Beta acceptance report

**Control:** [#613 — Beta release gate](https://github.com/rrahimi-uci/caliber-suite/issues/613)  
**Evidence package:** [#890 — SHA-bound Beta evidence manifest](https://github.com/rrahimi-uci/caliber-suite/issues/890)  
**Decision:** **HOLD**  
**Report status:** candidate evidence only; this report is not a release approval

## Candidate identity

| Field | Recorded value |
|---|---|
| Candidate implementation SHA | `e795c9cb298288d1d969c857e143a4817b2bac9f` |
| Candidate parent | `47e02fc4a0c4e7dfb8aa51f0718659a3aa2bdebc` (`origin/main` when this slice started) |
| Candidate change | Make the Sidebar capability-loading regression test deterministic; no production UI behavior change |
| Package/tag | None; no Beta artifact was built or published |
| Artifact digest | None; artifact production is correctly withheld while this gate is on hold |
| Evidence report commit | This document is added after the implementation SHA above and is not part of the release candidate identity |

The candidate is intentionally a reviewable readiness slice, not a claim that the
repository is Beta-release-ready. The report records the exact code state tested and
keeps the release decision separate from author, agent, or Copilot review.

## Executed checks and evidence manifest

| Check | Result | Evidence boundary |
|---|---|---|
| `CI=true npm test -- --run src/components/__tests__/Sidebar.test.tsx` | **34 passed** | Targeted UI regression test |
| `npm run typecheck` | **Passed** | UI TypeScript checks |
| `npx eslint .` | **Passed with one pre-existing warning** | Warning is the existing unused `waitFor` in `skill-calibration-jobs.test.tsx`; no error |
| `npm run build` | **Passed** | Vite production build; the repository docs sync used committed docs because the local CALIBER runtime was unavailable |
| `node docs/planning/validate-execution-plan.mjs` | **Passed** | `58 epics, 291 tasks, 22 gaps; full task/epic/gate DAG passes` |
| Focused backend/contracts pytest | **65 passed, 4 skipped** | Skips require `CALIBER_TEST_POSTGRES_URL`; they are not counted as passes |
| Full local `tests/egi_scenarios` pytest | **571 passed, 559 skipped** | PostgreSQL/reference-profile, row-lock, and multi-process cases were skipped because `CALIBER_TEST_POSTGRES_URL` was not configured |
| `git diff --check` | **Passed** | No whitespace errors |

The targeted UI validation was run from the candidate worktree with a clean
`npm ci`. The local EGI run is useful offline evidence, but it is not a substitute
for the PostgreSQL CI job or an independent review.

## Current CI and skipped-check record

The latest observed `main` CI run before this candidate was run `41770734549` at
SHA `47e02fc4a0c4e7dfb8aa51f0718659a3aa2bdebc`. Its EGI PostgreSQL job passed, but
the UI test-and-build job failed one capability-loading test. The candidate fixes
that test and must still obtain a fresh green CI result on its PR.

That run also had path-filtered suites marked **skipped**, including SDK, Plugin
SDK, CLI, full backend, compatibility, integration, and cookbook UI-only checks.
Skipped checks are recorded as unexecuted and do not support a Beta release claim.

The nested governed-context PR [#1130](https://github.com/rrahimi-uci/caliber-suite/pull/1130)
has a follow-up commit for the concrete resume-preflight defect found in its
PostgreSQL scenarios. Its fresh CI run is pending; it is not represented as a
passed check in this report.

## Gate ledger

| Gate | Status | Evidence or blocker |
|---|---|---|
| D1 staffing, thresholds, provider budget, environment, and reforecast | **Blocked** | [#608](https://github.com/rrahimi-uci/caliber-suite/issues/608) requires external named inputs; none are fabricated here |
| D1 acceptance report | **Missing** | No complete `D1-acceptance.md` was found |
| D2 acceptance report | **Missing** | Implementation evidence exists in issue history, but no owner acceptance report was found |
| D3 governed integration | **Incomplete** | Nested-context PR #1130 remains open; B04 routing/refusal follow-ups [#1086](https://github.com/rrahimi-uci/caliber-suite/issues/1086) and [#1087](https://github.com/rrahimi-uci/caliber-suite/issues/1087) remain open |
| D4 acceptance/replay and release eligibility | **Missing** | No complete `D4-acceptance.md` was found |
| D5 evidence package | **Partial** | This report records the current hold and local evidence; required external and PostgreSQL evidence remains open |
| Alpha-to-Beta upgrade and restore | **Open** | [#594](https://github.com/rrahimi-uci/caliber-suite/issues/594) remains open; no candidate-specific end-to-end upgrade/restore digest is recorded |
| Integration/support promotion | **Open** | [#595](https://github.com/rrahimi-uci/caliber-suite/issues/595) remains open |
| Independent Beta security review | **Not performed** | Existing `beta-security-review.md` is an offline evidence fragment and explicitly not independent sign-off |
| Bounded real-provider evidence | **Missing** | Current scenarios use fake/scripted providers and disposable local downstreams |
| Beta artifact, tag, and release notes | **Withheld** | Current package state is alpha `0.1.0a1`; no Beta release/tag exists |

## Scope exceptions and limitations

This report deliberately does not convert any of the following into a pass:

- local PostgreSQL-dependent skips;
- path-filtered or otherwise unexecuted CI jobs;
- author self-review, agent review, or Copilot activity as independent approval;
- the offline security evidence fragment as an independent security review;
- fake-provider results as real-provider quality, spend, availability, or isolation evidence;
- an open PR as a merged or deployed change; or
- a planned release artifact as a published artifact.

No migration is introduced by this report. The UI test change has no deployment
configuration or runtime behavior migration. If the D5 decision later becomes
**PASS**, the release must still be cut from a newly verified exact SHA using the
operator procedure in [`docs/operate/releasing.md`](../../operate/releasing.md),
with version lockstep, package checksums, install verification, upgrade/restore
evidence, and the final acceptance digest recorded before publication.

## Decision

**HOLD.** The candidate is suitable for review and continued CI verification, but
the Beta release gate is not complete. Release work must not create a Beta tag or
publish artifacts until the missing D1-D4 acceptance evidence, PostgreSQL and
deployment checks, bounded real-provider evidence, independent security review,
external coordination inputs, and owner decision required by #613 are recorded.
