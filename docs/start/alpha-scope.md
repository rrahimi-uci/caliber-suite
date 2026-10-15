---
audience:
  - developer
  - operator
  - decision-maker
doc_type: concept
product_area: docs
stability: beta
prerequisites:
  - None
reviewed_on: 2026-10-15
version_applicability: 0.1.0a1 alpha
tags:
  - alpha
  - scope
  - limitations
---

# Alpha scope and known limitations

> **Release status (2026-10-14):** `0.1.0a1` is the version prepared on `main`.
> The `caliber-v0.1.0a1` tag and downloadable release assets have not been
> published. The capabilities below describe the current code and its CI proof;
> use [Alpha install](alpha-install.md) to build wheels from a checkout until
> the prerelease is available.

CALIBER `0.1.0a1` is an alpha. This page says what it does, what it does not do, and what to
watch for, so you judge it against what it claims. It is the short version of the
[alpha gap analysis](../caliber-alpha-gaps.md), which has the evidence and the reasoning.

## What the alpha does

A small team can self-host CALIBER from documented steps ([Alpha install](alpha-install.md)) and,
through the web app or the Python SDK, do this with an audit trail and honest failure reporting:

1. Author a prompt.
2. Build a test set.
3. Calibrate the prompt to get a candidate with evaluation evidence.
4. Review the candidate and explicitly apply it to the live MLflow alias.
5. See what is live and which job, dataset version and evaluation put it there.
6. Roll it back.

The same loop works for a skill, a tool and a workflow (the extended tier), and, where an object store
is configured, for building and querying a knowledge base.

Two things about the evidence. With the shipped defaults the language model and the evaluator are
simulated: calibration runs and returns a candidate, but the scores are canned, and every screen,
audit row and release record that carries them says so. Real evidence needs a real model and
evaluator ([Real LLM and evaluation](alpha-install.md#real-llm-and-evaluation)). And the browser
journey is proven in CI on every change, offline, on the simulated defaults.

## Supported setups

| Item | Alpha support |
| --- | --- |
| Database | PostgreSQL. SQLite works for evaluation and demos when MLflow uses its own, separate store. |
| MLflow | A separate `mlflow server` (3.15 or later) over HTTP. Embedded `--app-name caliber` is not supported. |
| Install | Release wheels with a native MLflow, or the Docker Compose stack. |
| Object store | Needed only for knowledge bases and the object-store console. |
| Python | 3.10, 3.11 or 3.12. |

## What is in, preview, and out

| Maturity | Areas |
| --- | --- |
| Core journey | Prompts, test sets, evaluations, calibration, review and Apply, projects and roles, access tokens, audit log, release timeline, readiness |
| Also in the alpha | Skills, tools (trusted authors), workflows, knowledge bases and the object-store console |
| Preview (marked in the app) | MCP servers, OpenAPI integrations, cookbooks, the Aria assistant and plans |
| Record-only | Release-candidate signoff on the Releases page: it records a decision and deploys nothing |
| Experimental | Workspace releases: release evaluation has no executor in this build |
| Not in the alpha | Memory, and the flagged-trace to refinement loop |

## Known limitations

These are real and deliberate for the alpha. Read them before you rely on the product.

- **No trace-to-refinement automation.** Nothing watches MLflow for flagged traces. There is no feedback
  poller and no "flag this trace" action in the app, and verifying an item in the queue does not start a
  job. Calibration starts from a prompt and a test set, which you create. Submitting feedback on a trace
  records an assessment in MLflow and an audit row, and does nothing else.
- **Traces and MLflow data are visible to every signed-in user.** The observability pages and the trace
  and metric routes are not scoped to a project, so anyone who can sign in can read traces, including
  their inputs and outputs. Do not put sensitive data in traces on a shared alpha deployment.
- **Apply has no two-person rule.** One person with the `owner` or `editor` role in the project can
  review and apply a candidate. The approval record is created at Apply time as the audit and rollback
  anchor, and it records who applied. It is not a second approver.
- **CSRF protection is off by default.** `CALIBER_CSRF_ENABLED` defaults to false, on the assumption of a
  loopback or trusted-network deployment. Turn it on, and set a signing secret, before exposing CALIBER
  beyond that.
- **Simulated by default.** The LLM and evaluator default to `fake`. The provider banner and the
  Settings page say so, and it cannot be dismissed while the evaluator or promoter is simulated.
- **SQLite needs a separate MLflow store.** CALIBER and MLflow can share one SQLite file in development,
  and startup warns when they do, but only one writer wins at a time. Use PostgreSQL for anything shared.
- **The tool sandbox is a process boundary, not isolation.** Tool code runs in a short-lived,
  environment-empty process with resource limits. Register tools only from authors you trust.
- **Knowledge bases need an S3-compatible bucket**, the `knowledge-local` extra (torch and
  sentence-transformers) and a one-time embedding-model download. Without a reachable object store,
  creating one fails fast with a 503, and without the extra every embedding model is listed as
  unavailable. The default docker image installs `knowledge` but not `knowledge-local`, so it cannot build
  a knowledge base until you add it to `CALIBER_INSTALL_EXTRAS`. The storage and build path is tested
  against a real MinIO and PostgreSQL with simulated embeddings; no CI job exercises a real model.
- **Gate verdicts are not project-scoped.** `/gate-verdicts` keys a verdict by a free-form string that
  names no artifact, so any operator can overwrite any verdict. It is advisory: no release or promotion
  decision reads it.
- **No HA, no performance targets, no SSO or MFA, no multi-tenant hosting.**
- **Backups are documented, not rehearsed.** See [Backup and recovery](../operate/backup-and-recovery.md).
- **No upgrade guarantees** from earlier tags, and the packages are not on PyPI. Build wheels from a
  checkout until the prerelease assets are published.
- **Preview areas may change** or be removed without notice.

## Where to go next

- [Alpha install](alpha-install.md)
- [Backup and recovery](../operate/backup-and-recovery.md)
- [Health and readiness](../operate/health-and-readiness.md)
- [The refinement loop](../refinement-loop.md)
