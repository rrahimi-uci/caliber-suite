# D2 acceptance report

Control issue #610 (D2: Beta). Packages: B02 #517, B03 #518, B07 #524, B11 #530, B12 #533, B13 #536. This report is the single D2 evidence record; each child contributes through its own pull request and the table below.

## Decision

**PASS for engineering acceptance, with the scope exceptions listed below.** Every D2 child is merged and closed, the integrated candidate passes the full backend, UI and browser suites on the local runs except one failure that depends on the local machine (explained below), and changed-line coverage over all of D2 is 99.43%. One check is not yet a clean pass on the exact candidate: the push CI run on main for the candidate failed once in the full backend suite on an unrelated workspace-import test (see "Validation on the candidate"); it was re-run and the result is recorded below. Unmet #610 Definition-of-Done items, stated plainly: the children were not merged by the owner (they were merged by the implementing agent under the standing instruction); the reforecast is not done; no external review has taken place. Three things this report does not and cannot claim:

- **No human or external review.** Every child PR was reviewed by a fresh independent agent (not the author) against `git diff origin/main...HEAD`, and each PR body carries its findings and resolutions. The owner is named as reviewer in each PR body (GitHub cannot request a review from a PR's own author, and the owner's token authored them). The PRs were merged by the implementing agent under the standing instruction to merge after full validation, which replaces the "owner merges" wording of the child Definitions of Done. No external security review has taken place; the issue scopes that to the named pilot/release gate (D15 M16.2).
- **No reforecast.** #608 owns staffing, availability, live-provider caps and calendar; none of that is recorded here and none is invented. The only measured cycle data is below.
- **No protected production dispatch.** That remains D3's, as the control says.

## Candidate

| item | value |
| --- | --- |
| Candidate commit | `35c4e1a0136c3abf58cd86015aa79b3d0d1f2eb9` (main after #975) |
| D2 range | `c225785942c..35c4e1a0136`: 29 squash-merged PRs, the first being `94050c42a34` (#941) |
| Migration head | `0121` (`0114` run manifests, `0115` run grants, `0116` execution owner, `0117` policy decisions, `0118` experiment owners, `0119` event outbox, `0120` approved context sources, `0121` context manifests) |
| `run_manifest.v1.json` SHA-256 | `b030a8d4fb825de480eb82a5819cfc4faebf192d4f0dd693a36cda4e7f0c731b` |
| `context_manifest.v1.json` SHA-256 | `f9125e32f92a4868dcbfb8688d79cd0cac27fddf7b082ebf6d7be86f6abe82a9` |
| Built artifacts | Not built or digested for this report. The CI "Wheel build (with bundled SPA)" job passed on the final PR head (#975: 23 checks passed, 5 skipped and 2 neutral). The jobs skipped on pull requests by design are those that run only on push to main (the full backend suite on Python 3.11, compatibility, integration, cookbook UI-only, Allure); they ran on main for the candidate, see below. The wheel digest belongs to the release candidate (D15 M16.4), not to D2. |

## What each exit criterion rests on

| exit criterion (control #610) | where it is implemented | observable test |
| --- | --- | --- |
| Immutable admission intent and pins | `run_manifest.py`, `workflows/admission.py`, `run_pins.py`, `run_launch.py`; ADR 0003 | `tests/test_run_manifest.py`, `egi_scenarios/test_b02_admission.py` (10), `test_v06_admission_idempotency.py` (4), `test_v10_pins_unchanged.py` (12: an alias moved after admission changes nothing the worker runs) |
| Run-bound policy grants and decisions | `policy/principal.py`, `grants.py`, `decisions.py`, `workflows/run_identity.py` | `tests/test_policy_principal.py`, `test_policy_decisions.py`, `test_run_identity.py`, `egi_scenarios/test_b03_run_identity.py` (8), `test_v02_unauthorized_access.py` (8) |
| Captured owner lease and outbox | `ownership.py` (single-owner lease and fencing), `execution_role.py` (lifespan and role), `events/outbox.py` | `tests/test_execution_owner.py`, `test_worker_inventory.py`, `test_execution_role.py`, `egi_scenarios/test_v09_outbox_inbox.py` (6), `test_v14_reference_profile.py` (2) |
| Authorized context | `context/sources.py`, `authorize.py`, `manifest.py`, `assembly.py`, `rule_bundle.py` | `tests/test_context_sources.py`, `test_context_authorize.py`, `test_context_manifest.py`, `test_context_assembly.py`, `test_context_assembly_runtime.py`, `test_rule_bundle.py`, `egi_scenarios/test_b07_admission_sources.py` (12), `test_v03_context_injection.py` (2: an injected instruction in retrieved text stays data) |
| Canonical trace ownership and correlation | `observability/experiment_registry.py`, `trace_access.py` | `tests/test_trace_project_scoping.py`, `test_trace_experiment_ownership.py`, `test_trace_redaction.py`, `egi_scenarios/test_b11_experiment_ownership.py` (4) |
| Memory and profile refusals | `profile.py`, `request_limits.py`, `memory/service.py`, `routes/memory.py` | `tests/test_memory_reference_scope.py`, `test_request_limits.py`, `test_secret_canary.py` |

The whole `tests/egi_scenarios` directory collects 237 tests; the D2 scenarios (V02, V03, V06, V09, V10, V14 and the B02, B03, B07, B11 files) run in both the legacy and reference profiles where the behaviour differs, with the PostgreSQL cases run against the CI "EGI scenarios (PostgreSQL)" job and locally against a PostgreSQL 16 container.

## Child tasks

Every child is closed and its PR is merged to main. Each PR body records its own validation and an independent-review section with findings and resolutions (headed "Review resolution" on the first twelve PRs and "Independent review and resolution" on the later ones; coverage figures are in most bodies, not all, and the figure that counts is the one measured above over the whole range).

| epic | issue | PR | merge commit | what it delivered |
| --- | --- | --- | --- | --- |
| B02 | #656 | #944 | `05eb7bc8b2f` | canonical manifest bytes/digest, one row per run, UPDATE/DELETE guards |
| B02 | #676 | #955 | `7b34b437462` | pure admission builder |
| B02 | #680 | #957 | `da13efc13e1` | release, policy version and rule bundle resolved before one atomic run/manifest insert |
| B02 | #683 | #961 | `6a59a8c1aff` | worker reads pins (V10) |
| B02 | #687 | #963 | `9623dd24cac` | request digest and retry intent |
| B02 | #691 | #966 | `1dabe63adc0` | lineage required in the reference profile |
| B02 | #695 | #970 | `1e1235e3198` | `GET /workflow-runs/{id}/admission-manifest`, SDK and console client |
| B03 | #657 | #943 | `f9e072f342e` | pure principal types |
| B03 | #664 | #948 | `6c3677ca0f9` | run-bound grants, never above the role |
| B03 | #671 | #952 | `7e107ab5007` | typed `PolicyDecision` with exact run and grant |
| B03 | #684 | #964 | `74e7e184e6d` | policy decision in admission; worker identity check |
| B03 | #688 | #965 | `589a434cde1` | decisions audited (V02) |
| B07 | #726 | #950 | `3972e51bf63` | pure rule-bundle loader |
| B07 | #692 | #969 | `3e416de1de1` | approved context source inventory |
| B07 | #696 | #971 | `3b5e0f2e317` | admission check of approved sources |
| B07 | #699 | #972 | `bdc2ea7465d` | authorize before load |
| B07 | #704 | #974 | `563319f53f6` | ContextManifest v1 contract and immutable storage |
| B07 | #710 | #975 | `35c4e1a0136` | manifest recorded before the provider sees any context (V03) |
| B11 | #658 | #940 | `be2df4f8163` | canonical experiment claims across aliases |
| B11 | #665 | #954 | `31718c13b96` | atomic exclusive experiment owner registry |
| B11 | #672 | #956 | `a17fa5bc37b` | trace payload permission |
| B11 | #677 | #959 | `7d5491f99de` | trace payload redaction |
| B12 | #659 | #941 | `94050c42a34` | request body limit |
| B12 | #666 | #945 | `0cd482cb98e` | secret canary |
| B12 | #667 | #946 | `e29dc2f3d9b` | unowned memory refused in the strict profile |
| B13 | #660 | #942 | `4d69c4ed360` | background-loop inventory |
| B13 | #668 | #951 | `9047acc052a` | single-owner lease with fencing |
| B13 | #673 | #953 | `dcfde22a293` | lifespan: a second owner is API-only |
| B13 | #685 | #962 | `be306376824` | outbox and inbox |

## Validation on the candidate

Run on 2026-10-27 against `35c4e1a0136`, PostgreSQL 16 test database available (`CALIBER_TEST_POSTGRES_URL`).

| check | command | result |
| --- | --- | --- |
| Whole repository | `./test-all.sh --no-allure` (main checkout, with the PostgreSQL URL) | backend **10727 passed**, 2 failed, 1 error (see below); UI unit (vitest) **2846 passed in 195 files**; Playwright **33 passed, 1 skipped** (`@age`, needs Apache AGE) |
| Backend with coverage, clean worktree at the candidate | `PYTHONPATH=$PWD/src pytest tests -n 4 --cov=caliber --cov-report=xml` | **10728 passed, 16 skipped, 1 failed** (the S3 log-handler case below) |
| Coverage of the lines D2 added (`c225785942c..35c4e1a0136`) | repository changed-line check over the coverage XML | **99.43% (3669 of 3690 lines)** across the source files D2 changed (migrations are not measured); the package as a whole is 95.73% line coverage (62457 of 65242) and 88.56% branch coverage |
| CI on each PR head | GitHub Actions, required pull-request checks | green on every merged PR. These runs do not include the push-only full backend suite, so that suite is evidenced by the local runs above and by the main run below |
| CI on main at the candidate | push run 40574044870 | **failed once**: "Full backend suite (Python 3.11)" ended 1 failed, 10482 passed, 262 skipped; the failure is `tests/test_workspace_routes.py::test_import_submission_is_durable_idempotent_and_digest_pinned` (`assert 409 == 202` on a replayed import). That test is in the workspace source-import feature (#376, #338), not in D2 code, passes when run alone, and the requests in the log are seconds apart (a very slow runner), which points to a timing dependence; it was not diagnosed here (#979). Re-running that job (attempts 2 and 3) failed differently, with a `PytestDeprecationWarning` INTERNALERROR from pytest-asyncio 1.4.1 on a fresh dependency install; that broke every fresh-dependency run on main and was fixed in #981. A later run (40607908735, head `d88090ec5c8`) passed 10482 and failed 1: the S3 log handler test (#978), which depends on a root-logger handler left by an earlier `create_app` in the same process; #982 made it hermetic. **The run on `3e395a0c741` (40617047113), which contains all of D2 and both fixes, is green: 24 jobs, 21 success, 3 skipped by design; Full backend suite (Python 3.11) 10483 passed, 262 skipped, 0 failed (57m27s).** #979 (the workspace import timing test) did not recur and remains open as an undiagnosed flake. The push runs for the other 27 D2 commits on main were cancelled by later pushes, so only the candidate and #964 have a completed main run |
| Lint, format, types | `ruff check`, `ruff format --check`, `mypy src` | clean at each PR head; clean on the final head |
| Whitespace | `git diff --check` | clean on every PR |

### Failures and unexecuted checks, all explained

- `tests/test_observability_logging.py::test_s3_log_handler_does_not_log_about_its_own_delivery_at_debug` **fails in every full local run (three of three, including the clean worktree) and when run alone in the main checkout**, with a MinIO container listening on localhost:9000 and 24 deliveries recorded where one is expected; it passes when run alone in a clean worktree. D2 added this test and the `_delivering` guard in `S3LogHandler` (#945, B12.5b), so this is D2's own test, not an unrelated one. The suspected cause is a real S3 log handler left on the root logger by earlier code or the checkout's environment while a reachable MinIO accepts its deliveries; that is not diagnosed. It passed in CI on the PR heads that contain it, where no MinIO listens. **Unresolved**; it should be made independent of the environment (#978).
- `tests/test_docs_generation_contract.py::test_all_materialized_docs_copies_match_docs_site` failed once in `./test-all.sh` because the gitignored packaged UI docs (`src/caliber/ui`) were stale (they said 473 HTTP operations, the committed docs say 476). It passes once the UI step has rebuilt them, and the committed docs pass the "Docs validation" CI job.
- `tests/test_extended_family_audit.py` (two different cases over two runs), `tests/egi_scenarios/test_v00_harness_smoke.py` (real-process start) and `tests/test_execution_role.py::test_the_outbox_publisher_runs_and_stops_with_the_owner_loops` failed only in parallel full runs on a loaded machine and pass when their files are run alone. Running the scenario directory together with non-scenario files also gives "fixture 'reference_app' not found"; run them per directory (as CI does).
- 21 added lines are not executed. Nineteen are defensive paths: database-failure branches (`context/authorize.py` 152, 153, 202, 204, 205, 208), a grant that cannot be read at worker start (`workflow_run_worker.py` 1610, 1611, 1617), the retry race and error mappings in `routes/workflow_runs.py` (899-902, 928, 929, 1195-1197, 1247). Two (`observability/experiment_registry.py` 344, 345) are the whole body of `registry_owned_ids()`, which has no caller and no test: dead code added in #954, removed by #980. Nothing was excluded from the measurement and no coverage setting was changed.
- Not run: live-provider tests (`CALIBER_INTEGRATION_TESTS=1`), the knowledge-stack journey (needs its own S3 and PostgreSQL), the Apache AGE browser case, and the packaged-UI route test. They are outside the D2 packages and are skipped by their own markers.
- The GitHub "Dependabot Updates" run on main failed; it is dependency-update automation, unrelated to this code.

## Independent review

Each child PR got a fresh-agent review of its diff, and each found defects that were fixed before merge; the findings are in each PR body. The classes that recurred and are now built in from the start: payload leaks in error text (outbox errors, context denial), swallowed or mislabelled refusals, claim-ownership and stop/drain races in loops, SQLite versus PostgreSQL transaction differences, line-pinned inventories, and documentation that claimed more than the code enforced (the last review, on #975, found 3 high and 4 medium defects in context assembly; each was fixed or, where the fix needed a new read path, documented and tracked in #976). These are agent reviews; no human or third-party reviewed the code.

## Scope exceptions and unresolved findings

Open and visible; none blocks D2's exit criteria, each is owned by an issue.

| issue | what is not covered |
| --- | --- |
| #958, #967 | Cron-scheduler and service-token runs are legacy runs: they are not admitted, have no run grant and (in the reference profile) no runtime lineage. A service-principal model for run grants is needed. |
| #968 | The console cannot start runs in the reference profile until the Run and Trigger actions can name a release and environment. |
| #973 | Knowledge-base reads outside an admitted run (preview, evaluations, gates, refinement, assistant, export, `knowledge_build`) are not guarded by `context.read`. |
| #976 | The context manifest records the step's initial context only: session memory is recorded as history without a memory authorization, manifests are not linked to the step, tool specifications and tool-loop results are not recorded, a registry prompt is resolved twice, retrieved text that passes through another node loses its provenance, a resumed run that replays a recorded knowledge-query output is refused, and other model calls (query answer synthesis, guardrail judges) are not recorded. |
| #960 | Trace payloads in review-queue items and example-from-trace are not governed. |
| #978 | `test_s3_log_handler_does_not_log_about_its_own_delivery_at_debug` depends on the environment (see above). |
| #979 | `test_import_submission_is_durable_idempotent_and_digest_pinned` is timing-dependent and turned the push CI on main red once. |
| #947 | The memory service passes entity ids that mem0 2.x rejects (search and list fail). |
| design limits | A grant holds at most 256 resources; the context source guard is point-in-time; the token budget is an accounting estimate (enforcing it on a provider call is B08, a D3 package whose tasks are still open); the in-run memory tools exist, but a `context.read` authorization on `memory:<ref>` is not yet part of loading memory (#976). |

## Measured limits

- Throughput and latency are not a D2 criterion and were not measured here beyond the existing tests' own timing assertions.
- Cycle data: the 28 D2 PRs merged between 2026-10-26 and 2026-10-27 in one automated stream, each with a CI run and at least one independent review pass; that is an agent's rate, not a three-person team's, and is not a basis for a forecast. The reforecast is #608's.

## Cross-component integration

The path is covered in `egi_scenarios` by separate public-path tests rather than one continuous test: admission, pins and grants (`test_b02_admission.py`, `test_v06_admission_idempotency.py`, `test_v10_pins_unchanged.py`), the worker verifying identity and context sources and recording the context manifest through a real worker run (`test_b03_run_identity.py`, `test_b07_admission_sources.py`, `test_v03_context_injection.py`), the outbox and inbox (`test_v09_outbox_inbox.py`), and two processes with a single owner (`test_v14_reference_profile.py`, which does not run a workflow). The browser suite (33 passed) exercises the legacy profile, which the D2 changes leave unchanged.

## Gate

Pass, with the exceptions above. Closing the epics and #610 records that the engineering evidence is complete; it does not release anything, and the D15 release decision, the independent security review and the #608 reforecast stay open.
