# CALIBER UI-complete Cookbook implementation report

**Status:** implemented and locally verified
**Audit date:** 2026-08-04
**Scope:** all 16 numbered Cookbooks, the React UI, backend contracts, durable data, documentation, and browser-only automation

## Outcome

All 16 Cookbooks now exist in CALIBER as versioned, installable system examples.
The new **Cookbooks** page loads the server-owned catalog, exposes readiness and
prerequisite checks, and installs a selected recipe atomically as one **paused**
workflow with one editable **draft** version. Installation never publishes,
deploys, or executes the example; the operator must acknowledge prerequisites
and review bindings before activation.

The earlier 9 full / 6 partial / 1 missing classification is retired for the
shipped product path. Every recipe can now be installed and authored through the
UI without database writes, API seeding, shell-authored manifests, or custom
Python. External systems and model-backed outcomes remain environment-gated:
CALIBER can make their configuration and readiness UI-complete, but it cannot
make an unavailable provider, network route, secret, trace, or worker ready.

| Result | Cookbooks | Count |
|---|---|---:|
| Installable governed system example | 01–16 | 16 |
| Offline-safe deterministic example path | 02, 03, 07, 08, 12, 13, 14 | 7 |
| Environment-gated live outcome | 01, 04–11, 15, 16 | 9 |
| Missing product domain | none | 0 |

“Environment-gated” is not a product implementation gap. It means the UI and
durable contract exist, while successful live inference or integration still
depends on operator-supplied infrastructure or evidence.

## Delivered platform capabilities

### System Cookbook catalog and installer

- `GET /caliber/cookbooks` returns exactly 16 versioned recipes from one
  validated server catalog.
- `POST /caliber/cookbooks/{id}/install` performs workflow, draft-version, and
  audit creation in one transaction.
- Every installed workflow is paused and carries a visible Cookbook guide note.
- The UI shows prerequisites, runtime-approval readiness, and an explicit
  acknowledgement before installation.
- The installer is RBAC protected; viewers can inspect the catalog but cannot
  create examples.

### Code-free deterministic transforms

Workflow Studio now ships a `data_transform` component with five closed-vocabulary
operations:

| Operation | Product use |
|---|---|
| `fixture` | Safe, repeatable example evidence without an external dependency |
| `mapping` | Declarative field projection and renaming |
| `json_schema` | Draft 2020-12 validation with fail-closed or routable invalid output |
| `decision_table` | Ordered deterministic policy rules with a default result |
| `confidence` | Weighted signals and an explicit review threshold |

Cookbook 03 uses a decision table, 04 uses JSON Schema, 06 uses deterministic
confidence, and 07/08 include safe evidence fixtures. These nodes compile,
preview, execute, export, validate, and round-trip through the visual editor.

### Reusable operational connectors

The API Request inspector includes credential-free starters for:

- GitHub incident / issue lookup;
- deployment-health retrieval; and
- service-health retrieval.

Cookbooks 07 and 08 install both an offline fixture path and clearly labeled live
connector nodes. Placeholder hosts are deliberately non-routable until the
operator replaces the uppercase segments and configures egress. Authentication
belongs in governed MCP/secret configuration; no credential is embedded in a
workflow manifest.

The GitHub MCP quick-connect tile now uses GitHub's official remote Streamable
HTTP endpoint. The retired npm preset and its unshipped `npx` runtime dependency
were removed. A live connection remains correctly environment-gated on the
`api.githubcopilot.com` remote-host allowlist and an operator-provided personal
access token.

### Skill package and selection lifecycle

- The UI uploads an OpenAI-compatible ZIP directly.
- Bounded parsing rejects path traversal, symlinks, encrypted members, binary
  content, oversized archives, and excessive members.
- Conflict handling is explicit: reject, rename, or admin-only forward-versioned
  merge.
- Positive trigger phrases boost selection; negative phrases are stopword-aware
  exclusions, with exact matched signals returned by the test surface.

### Review queues and judge alignment

- `review_queue_enqueue` is a first-class workflow component backed by the
  existing idempotent, audited queue service.
- Standalone execution fails closed when no queue enqueuer is bound.
- Judges can import completed pass/fail queue labels directly into Human
  Alignment; imported rows retain queue, item, trace, question, reviewer, and
  assessment provenance.

### Runtime approval readiness

The platform capability response and Cookbook checks now disclose:

- queue, runtime-approval, and checkpoint enablement;
- concrete blockers and the Settings path;
- the per-node `required_role` decision scope;
- whether self-approval is enabled; and
- the audit actions emitted by the approval lifecycle.

This makes the deployment boundary visible. It does not silently enable runtime
approvals or weaken the configured approval policy.

### Release Signoff Factory and in-product Allure evidence

Cookbook 11 now has a real domain model rather than a dashboard-only imitation:

- durable release candidates with typed weighted criteria and evidence links;
- server-recomputed aggregate score and blocking criteria;
- admin waivers with reason, expiry, approver, and timestamp;
- immutable final go/no-go signoff snapshots;
- mandatory planned action and rollback target for a go decision;
- audited, durable report jobs; and
- retained Allure-compatible JSON containing criterion status, evidence links,
  and a SHA-256 candidate-snapshot identity.

The current report job completes in-process and is monitorable through its
durable row. It does not invoke a shell or claim to be a distributed rendering
worker.

## Per-Cookbook implementation map

| # | Installed system example | Principal product path | Live prerequisite boundary |
|---:|---|---|---|
| 01 | Trustworthy Intake Classifier | guarded workflow, prompt/evaluation/calibration guide | model and refinement providers |
| 02 | Precision Skills | skill trigger/package guide | none for deterministic trigger/package checks |
| 03 | Policy-Safe Decision Tool | decision table → agent → approval | queued runtime approvals for a live gate |
| 04 | Document-to-JSON Pipeline | structured extraction → JSON Schema | uploaded document and extractor/model as selected |
| 05 | Governed Tool Connectivity | official GitHub remote MCP connection/policy/calibration guide | `api.githubcopilot.com` allowlist, network, personal-access token |
| 06 | Grounded Knowledge Assistant | retrieval plus deterministic confidence | embeddings/chat providers and built KB |
| 07 | Support Triage Copilot | incident fixture/live starter → governed flow | live incident and issue systems when selected |
| 08 | Incident Response Copilot | deployment/service fixtures plus live starters | live deployment and health endpoints when selected |
| 09 | Self-Healing Workflows | checkpointed operator-guided recovery | queue worker and prior-good run evidence |
| 10 | Trustworthy Evaluation | judge, review queue, direct label import | judge provider and reviewed traces |
| 11 | Release Signoff Factory | candidate, blocker/waiver, signoff, report job | evaluation/review/trace evidence supplied by operator |
| 12 | Aria Evaluation Harness | typed, approval-aware governed plan | provider for later evaluation |
| 13 | Aria Review Governance Queue | typed queue creation and optional population | trace IDs only for populated items |
| 14 | Aria Governance Starter Kit | judge, Test Set, and queue plan | provider for later scoring |
| 15 | Aria Triage & Recalibrate Loop | queue/calibration plan with job polling | artifacts, traces, provider, refinement worker |
| 16 | Production Observability & Triage | trace-to-review-to-regression guide | existing successful and failed traces |

## Browser-only automation contract

The Cookbook Playwright suite uses visible controls only. Its adapters are
statically guarded against `page.request`, `fetch`, HTTP verbs, direct API paths,
database fixtures, and manifest seeding.

Verified journeys:

1. Cookbook adapter source guard.
2. Cookbook 02: sign in, create a skill, render it, run positive and negative
   trigger checks, persist a run, and archive it.
3. Catalog/all Cookbooks: verify all 16 cards, acknowledge each applicable
   prerequisite boundary, install every real example as its own paused draft,
   and observe each editor route and draft name.
4. Cookbook 13: create a review queue and required citation question.

The `cookbook-ui-only` CI job runs on its own migrated SQLite state and is mirrored
by `scripts/ci-local.sh cookbook-ui-only`. Cleanup terminates the entire detached
MLflow/uvicorn process group before deleting state, test results, and the
Playwright report.

## Verification evidence

Observed on the 2026-08-04 working tree:

- focused new backend surfaces: **67 passed**;
- migrations/model/manifest migration: **18 passed**;
- integration regression slice after event-loop/CI/catalog fixes: **85 passed**;
- final complete backend suite: **5,964 passed, 9 skipped**, with **93.16%**
  statement/branch coverage and no failures;
- complete frontend suite: **1,570 passed** across 115 files;
- focused changed frontend surfaces: **142 passed**;
- final Cookbook documentation contract: **15 passed**, including the official
  GitHub MCP endpoint/retired-preset regression guard;
- final MCP/Cookbook/sidebar UI regression slice: **21 passed**;
- browser-only Cookbook suite: **4 passed**;
- Ruff, MyPy (**322 source files**), frontend TypeScript, and ESLint: passed;
- strict docs generation/synchronization: passed with both served copies current;
- reproducible paper build: **75 pages**, **8 structural checks passed**, no
  undefined references/citations, no Type 3 fonts, and no overfull boxes.

The nine backend skips are environment-gated MLflow/Postgres integration tests,
not counted as passes. The final full run used 14 workers with load-group
serialization and completed after the code, documentation, and paper updates.

## Residual limits and non-claims

- A configured UI path is not proof that an external provider or connector is
  reachable in a particular deployment.
- Fixture-based examples prove workflow behavior, not production data fidelity.
- The operational connector presets do not embed or resolve credentials.
- Runtime self-approval follows deployment configuration and is disclosed; this
  work does not force a safer default into an existing installation.
- In-product Allure-compatible JSON is durable release evidence, not the same as
  a rendered Allure HTML site or an asynchronous worker farm.
- Aria remains bounded by its registered capabilities and typed interactions;
  these Cookbooks do not establish open-ended autonomous planning.
- Local Python 3.12 evidence is within the supported 3.10–3.12 range, but remote
  CI remains the release-certification boundary.

## Correctness conclusion

The defensible claim is now:

> CALIBER ships all 16 Cookbooks as governed, installable, UI-authored system
> examples. Every required product domain exists end to end; live model,
> connector, worker, secret, and trace outcomes remain explicitly
> environment-gated and must be verified in the target deployment.

This is stronger and more precise than claiming that infrastructure prerequisites
disappear. The UI owns authoring, readiness disclosure, durable state, evidence,
and review; the environment still owns the availability of external dependencies.
