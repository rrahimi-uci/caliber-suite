---
audience:
  - developer
  - operator
  - decision-maker
doc_type: reference
product_area: strategy
stability: beta
summary: Release notes for CALIBER Suite Alpha (caliber-suite-alpha, 0.1.0a1) - what it does, how it was verified, what it does not do, and where it goes next.
prerequisites:
  - Read the Alpha scope and limitations page, which is the authority on what the alpha claims
reviewed_on: 2026-10-23
tags:
  - release-notes
  - alpha
---

# CALIBER Suite — Alpha (`caliber-suite-alpha`, 0.1.0a1)

This is the first alpha of CALIBER, the MLflow-integrated control plane for trusted agentic workflows. It is a
**prerelease** for people who are willing to install it, use it, and report what breaks. It is not a pilot or
production release, it makes no stability promise, and it is not on PyPI.

| | |
| --- | --- |
| Release name | `caliber-suite-alpha` |
| Git tag | `caliber-v0.1.0a1` (the repository's `caliber-v<version>` convention; see [Releasing](../operate/releasing.md#the-names)) |
| Packages | `caliber-suite`, `caliber-sdk`, `caliber-cli`, `caliber-plugin-sdk`, all `0.1.0a1` (the web app is bundled in `caliber-suite`) |
| Requires | Python 3.10–3.12, MLflow 3.15 or later, PostgreSQL (SQLite for demos), Docker with Compose v2.20+ for the container path |

Read [Alpha scope and limitations](../start/alpha-scope.md) before you rely on it: it is short and it is the
authority on what the alpha does and does not do.

## Overview

CALIBER is a standalone ASGI service and a React web app that sit next to an MLflow server. For each thing an AI
agent is made of (prompts, skills, tools, workflows, knowledge bases) it gives one loop — **author, test,
calibrate, review, apply, see what is live, roll back** — with role-based access, an audit trail of every
governed action, and a person, not the model, deciding when a change goes live. It reuses MLflow's Prompt
Registry, tracing, artifact store and `genai.evaluate`, and talks to MLflow only over HTTP.

## What is in this release

| Tier | Capabilities |
| --- | --- |
| **Core journey** (proven in CI on every change) | Prompts, test sets, evaluations, calibration, review and Apply to a live MLflow alias, rollback, the release timeline and live board, projects and roles, access tokens, the audit log, readiness |
| **Also in the alpha** | Skills; tools (a process sandbox, for trusted authors); workflows (studio, queued runs, human approval, wait for event, deploy gates); knowledge bases; the object-store console; judges; observability; the LLM gateway page with model pricing and exact usage cost; the Python SDK, `caliberctl` and the plugin SDK |
| **Preview, on by default** | **Aria**, the assistant: chat, rule-based plans with approval gates, and the helpers in the prompt workspace (Describe it, Ask Aria, Playground, test-case generation) |
| **Preview, off by default** | MCP servers and OpenAPI integrations (`CALIBER_FEATURE_INTEGRATIONS`); the cookbook gallery (`CALIBER_FEATURE_COOKBOOKS`); release-candidate signoff and workspace releases (`CALIBER_FEATURE_RELEASE_CANDIDATES`) |

### Real models, not placeholders

CALIBER does not pretend. Put `OPENAI_API_KEY` (and optionally `ANTHROPIC_API_KEY`) in `.env` and the Docker
stack's `auto` setting makes the workflow agents, calibration, judges, evaluation, Aria and knowledge-base answers
real. With no key the stack still starts, but every part that needs a model is simulated **and says so**:
`GET /readiness` returns `ai_components` and `all_ai_real`, `./start.sh` prints the same list when it finishes,
the web app's provider banner cannot be dismissed while something is simulated, and evaluation is refused (400)
rather than scored with canned numbers. An Anthropic key makes Aria, knowledge-base answers and Claude-model
judges real; workflow agents and calibration need OpenAI in this alpha. Aria's planner is rule-based by design.

## Architecture

```text
Browser ── CALIBER web app (/caliber/) ─┐
Python SDK / caliberctl / REST ─────────┤
                                        ▼
                       CALIBER ASGI service  :5001  (API, in-process workers, bundled UI)
                         │        │         │
        HTTP (MLFLOW_TRACKING_URI)│         └── LLM providers: OpenAI · Anthropic · MLflow AI Gateway
                         ▼        ▼
                  MLflow :5000   CALIBER database (PostgreSQL / SQLite)
            prompt registry · traces · evaluate        Object store (S3 / MinIO) for files
```

CALIBER and MLflow are separate processes with separate databases and migration owners. Embedding CALIBER in the
MLflow server (`--app-name caliber`) is not supported. The server never creates its own tables: `caliber-migrate`
does (the Docker image runs it for you). The layered design is in [`ARCHITECTURE.md`](../../ARCHITECTURE.md).

## Workflows you can run

1. **Release a prompt** — write it, add a test set, calibrate, review the diff and the evidence, Apply, watch the
   release timeline, roll back. The same loop works for a skill and a tool.
2. **Run a governed workflow** — build it in the studio, publish, run, approve at a human gate, wait for an event or a time.
3. **Ground an assistant in your documents** — create a knowledge base, build a version, query it, roll it back.
4. **Operate it** — set up projects, roles and tokens; read the audit log; inspect traces; meter LLM spend per model.
5. **Let Aria help** — draft a prompt from a sentence, generate test cases, plan a multi-step change and approve each step.

## Cookbooks

Twenty runnable labs (01–16 and 18–21) plus one standalone SDK script (17) in
[`docs-site/cookbooks/`](../../docs-site/cookbooks/README.md). Each has sample data, an SDK script that exits 0 only
when every claimed outcome held, a Playwright browser spec where the feature has a UI, and a step-by-step README.
Labs run on a disposable stack with `scripts/cookbook-env.sh`; they are deterministic and simulated by default, and
run on real models with `--real-llm`. `scripts/cookbook-run-all.sh` runs every lab, API and browser.

## Install and quick start

```bash
git clone https://github.com/rrahimi-uci/caliber-suite.git && cd caliber-suite
cp .env.example .env        # set OPENAI_API_KEY (and optionally ANTHROPIC_API_KEY)
./start.sh                  # builds, starts, waits for health, prints URLs and the AI-component report
```

Open `http://127.0.0.1:5001/caliber/` and sign in as `admin` / `admin` (first boot only; change it at once).
`./stop.sh` stops the stack and keeps your data; `make infra-reset` deletes it. Without Docker, install the wheels
from the release assets: [Alpha install](../start/alpha-install.md). The step-by-step first workflow is in the
[README](../../README.md#your-first-workflow).

## Configuration

Everything is an environment variable; `.env.example` documents each one and the Docker stack reads `.env`. The
important ones: `OPENAI_API_KEY`, `ANTHROPIC_API_KEY`, `CALIBER_LLM_PROVIDER` / `CALIBER_EVAL_PROVIDER` /
`CALIBER_ASSISTANT_ENGINE` (`auto` by default in the stack), `CALIBER_ADMIN_USERS` / `_OPERATOR_USERS` /
`_APPROVER_USERS`, and the four `CALIBER_FEATURE_*` flags. The server never reads `.env` itself (Docker and
`scripts/cookbook-env.sh` do), and keys are never stored in the database or returned by the API. See
[Configuration and provider setup](../operate/configuration-and-provider-setup.md).

## Testing performed

Run on the release candidate commit, on macOS (Apple Silicon, Docker Desktop, Python 3.12) unless noted.

| Area | Result |
| --- | --- |
| Backend suite (`pytest -n 6`) | 8,944 passed, 15 skipped (the PostgreSQL-only parity tests, which CI runs against a real database). The one failure, a generated-docs freshness check, passes after regenerating; the files touched by the last two fixes were re-run (167 passed) |
| Web-app unit tests (Vitest) | 2,567 of 2,568 passed; the one failure was a 5 s timeout while the machine was starved, and that file passes in isolation (20 of 20) |
| Lint, formatting and type checks (ruff, mypy, `tsc`, ESLint) | pass (one pre-existing ESLint warning in a test file) |
| SDK, CLI and plugin-SDK checks (`scripts/ci-local.sh sdk cli plugin-sdk`) | pass (SDK, plugin SDK and CLI: lint, mypy, tests, build) |
| Documentation contract suites (`docs-validation`) | 101 passed |
| Fresh-clone Docker path from the README (`./start.sh`, login, readiness, UI, stop, restart, data persisted) | pass; every AI component `real`, `all_ai_real: true` |
| Real-model runs with the owner's OpenAI key (not a CI gate) | Aria chat, Describe it (prompt draft), skill/tool/MCP drafts, test-case generation, Lab 01 (30 verified outcomes, real `mlflow.genai.evaluate`, 44 examples), Lab 12 (38 verified outcomes, a real judge over 30 rows) |
| Cookbook labs on a fresh native stack, API and browser (`cookbook-run-all.sh --labs 01,18`) | pass; the full set runs in CI and was run during the first-user verification |
| `scripts/verify-alpha-install.sh` (wheels, no checkout on the import path) | pass |
| `scripts/verify-wheel-boot.sh` (SPA and migrations in the wheel, boots alone) | pass |
| `scripts/verify-compose-stack.sh` (both images built, stack booted, login, project, prompt, registry) | pass |
| Shell scripts: audited line by line, `bash -n` under macOS bash 3.2, ShellCheck, and the operator scripts (`start.sh`, `stop.sh`, `cookbook-env.sh`, `cookbook-run-all.sh`, the three `verify-*.sh`) executed | pass |
| The README's Docker quickstart, SDK token snippet and cookbook setup, executed verbatim in a fresh clone of the branch; the no-Docker path via `verify-alpha-install.sh` | pass |

CI additionally runs the browser journeys, the SDK journeys against a real MLflow registry, project isolation and
role checks on every mutating route, and a weekly Docker stack smoke. No CI job calls a live model.

## Stability expectations

Alpha means: **the core journey is supported and tested; everything else may change**. Database schema, REST
shapes (outside the routes marked `GA` in the OpenAPI document at `/ajax-api/2.0/mlflow/caliber/openapi.json`), the SDK, the CLI and the preview areas can
change between alphas without a deprecation period. There is no upgrade guarantee from earlier tags (run
`caliber-migrate` before starting a new version; `/readiness` reports a `schema` blocker if you did not). Pin the tag.

## Breaking changes

None against a published release (there is none). Against earlier development builds: Aria ships **on** by default
(`CALIBER_FEATURE_ARIA=false` turns it off); the other three preview areas are off and need their flag; the Docker
stack's LLM, evaluator and assistant now default to `auto` instead of simulated or hard-coded values; Describe it
and test-case generation return an error (503) instead of a placeholder when no model can answer.

## Known limitations

The full list is on [Alpha scope and limitations](../start/alpha-scope.md#known-limitations). The ones that most
often surprise people: nothing watches production traces and starts a refinement; traces and MLflow data are
visible to every signed-in user; Apply has no second approver; CSRF protection is off by default; tools run in a
process sandbox, not a container; knowledge bases need an object store and, for real embeddings, the
`knowledge-local` extra (absent from the default image); `/gate-verdicts` is not project-scoped; no HA, SSO or MFA.

## Known issues

- `CALIBER_LLM_PROVIDER=anthropic` cannot boot; Anthropic keys power Aria, knowledge-base answers and Claude judges only.
- Guardrail LLM judges fail open when their client is unavailable, and cache a missing client for the process.
- The container image has no DSPy, memory or `knowledge-local` extras; add them with `CALIBER_INSTALL_EXTRAS`.
- The developer shortcuts `make dev` and `run-dev.sh` use the unsupported embedded mode; use `./start.sh` or the wheel install.
- `CALIBER_LOG_SINK` defaults to `s3`, which logs noise on a native install without an object store; the install guide sets `stderr`.
- The AGE graph viewer image tracks PostgreSQL 16 while the database is 17 (the viewer is a convenience only).
- A cold `./start.sh` can fail on a transient Docker Hub timeout while pulling base images; run it again.
- Python 3.13 and newer are not supported by the server package.
- After you **Resume** an approved workflow run, the Execution Debugger can keep showing the approval step instead of jumping to the final step; reselect the step or reload the page. The Lab 08 browser spec fails on this (it also fails on `main` before this release), and the Lab 07 browser spec is intermittently affected in CI. The API path of every lab passes.

## Troubleshooting

[README troubleshooting](../../README.md#troubleshooting), [Local bring-up](../operate/local-bring-up.md),
[Health and readiness](../operate/health-and-readiness.md) and the
[install guide's table](../start/alpha-install.md#if-something-is-wrong). First checks: `docker compose ps`,
`GET /readiness` (`ready`, `blockers`, `ai_components`), and `docker compose logs caliber`.

## Feedback

Open an issue at <https://github.com/rrahimi-uci/caliber-suite/issues/new/choose> with the version (`0.1.0a1` and
the commit), how you ran it (Docker or wheels), the `/readiness` output with secrets removed, what you expected and
what happened. Questions and ideas are welcome in Discussions. For a security report, follow
[`SECURITY.md`](../../SECURITY.md) and do not put details in a public issue.

## Out of scope for the alpha

Trace-to-refinement automation, memory, a two-person Apply rule, per-project trace isolation, high availability,
performance targets, SSO and MFA, multi-tenant hosting, an automatic upgrade path, PyPI publication, and running
workflow-release evaluations (the lab for that area demonstrates the boundary).

## Toward Beta

The [roadmap](../roadmap.md) is the plan. For the Beta the work that matters most is closing the limits above that
block a controlled pilot: project-scoped traces, a second approver for Apply, CSRF on by default, a container
sandbox for tools, the trace-to-refinement loop, Postgres-first deployment and backup rehearsal, an upgrade path
between tags, and promoting the preview areas the cookbooks prove (integrations, release signoff) to supported.

## Assets

Four wheels and four source distributions, `caliber-suite-alpha-0.1.0a1.tar.gz` (the same files plus the install
guide, scope note and licence in one download) and `SHA256SUMS`. Install from the downloaded files:
`pip install --find-links ./wheels caliber-sdk caliber-cli caliber-suite`. `pip install caliber-sdk` finds nothing:
the names are not reserved on PyPI.
