# CALIBER Suite

> **MLflow-native control plane for trusted agentic workflows — design docs, plugin source, and documentation site.**

CALIBER is an MLflow server plugin and React application for designing,
evaluating, calibrating, approving, deploying, and observing AI-agent
resources — prompts, tools, skills, MCP servers, workflows, knowledge bases, and
the embedded Aria copilot. This directory holds the suite's design specs, the
published documentation site (architecture pages + step-by-step Cookbooks), the
deployment stack, and the plugin source itself.

Live docs: **`docs-site/index.html`** (open locally)
Plugin package: **`caliber-mlflow`**

---

## Layout

| Path | What it is |
| --- | --- |
| [`caliber/`](./caliber/) | The MLflow plugin Python package — runtime code, alembic migrations, SPA, tests, packaging. See its [README](./caliber/README.md) for install + dev. |
| [`docs-site/`](./docs-site/) | The published documentation site (GitHub Pages): the [landing page](./docs-site/index.html); 15 generated architecture module pages (`m-01`…`m-15`, built from [`docs/`](./docs/) by [`build-docs.mjs`](./docs-site/build-docs.mjs)); the **Cookbooks** — a [card index](./docs-site/m-16-cookbooks.html) plus 16 step-by-step, UI-only recipe pages sourced from [`docs-site/cookbooks/`](./docs-site/cookbooks/); the [walkthrough](./docs-site/walkthrough.html) bring-up runbook; and the click-through slide decks ([`presentation.html`](./docs-site/presentation.html), [`presentation_timed.html`](./docs-site/presentation_timed.html)) at native 1920 × 1080. The site is also synced into the SPA and served in-app at `/caliber/docs/`. |
| [`docs/`](./docs/) | Source-of-truth design specs — one `architecture.md` per numbered area (platform, registries, workflows, data, observability, QA, evaluation, calibration, assistant) plus the [workflow components reference](./docs/06-workflows/components.md). Each page opens with an **At a glance** summary and a typed-color diagram, then a banded **Reference** tier. Authoring conventions live in [`docs/STYLE.md`](./docs/STYLE.md). Start with [`01-caliber/architecture.md`](./docs/01-caliber/architecture.md), or read the rendered [architecture pages](./docs-site/m-01-platform.html). A machine index for agents is published at `docs-site/llms.txt`, linking each page's raw Markdown source. |
| [`deploy/`](./deploy/) | The Docker stack behind a full local/production bring-up (Postgres 17 + pgvector + Apache AGE, MinIO, MLflow, the MLflow AI Gateway, NATS, Redis) plus `compose.yaml`. See [`deploy/README.md`](./deploy/README.md). |
| [`overview-video/`](./overview-video/) | Narration script and the screenshot/video-generation scripts that render the embedded overview video (`caliber.mp4`) on the docs site. |

---

## Quick links

- **Read first:** [docs-site landing page](./docs-site/index.html) — what CALIBER is, why it exists, how it fits with MLflow.
- **Bring it up:** [walkthrough](./docs-site/walkthrough.html) — copy-pasteable runbook that brings up MLflow + CALIBER against object storage and a real LLM provider, tours every page of the SPA, and builds a governed artifact with Aria.
- **Learn by doing:** [Cookbooks](./docs-site/m-16-cookbooks.html) — 16 step-by-step, UI-only recipes that teach the platform end to end (prompt regression, precision skills, policy-safe tools, document-to-JSON, governed MCP, grounded knowledge, support/incident copilots, self-healing workflows, production observability & triage, trustworthy evaluation, release signoff, and four Aria goal-plan recipes), each verified implementable in the shipped product UI.
- **Build the plugin:** [caliber README](./caliber/README.md) — `pip install -e ".[dev,s3]"`, then `make dev`.
- **Contribute:** [CONTRIBUTING.md](./CONTRIBUTING.md) — local setup, the quality gate, the extension seams, and PR conventions.
- **Design context:** (browse [`docs/`](./docs/), or the rendered [architecture pages](./docs-site/m-01-platform.html))
  - [docs/01-caliber](./docs/01-caliber/) — platform architecture, data flows, deployment topology, and security model
  - [docs/12-assistant](./docs/12-assistant/) — Aria's agentic copilot architecture (engines, tool loop, modes, approvals, and the goal-plan orchestrator: plan → supervised execution → pause/interaction → async resume → self-correction)
  - [docs/14-evaluation](./docs/14-evaluation/), [docs/15-calibration](./docs/15-calibration/) — per-asset evaluation + calibration algorithms

---

## Status

The platform spans prompts, tools, skills, MCP servers, workflows (Studio + queued runs with runtime approvals and checkpointing), knowledge bases (hybrid retrieval + calibration), object storage, test sets, observability (MLflow tracing), evaluation scorecards, custom LLM judges, structured human-review queues, per-asset calibration (a nine-optimizer pool — MetaPrompt, GEPA, DSPy, and more — auto-selected by artifact type and diagnosis shape, covering both prompts and skills), the LLM Gateway governance surface (endpoint discovery, guardrails, per-model pricing, and usage), settings inventory, RBAC, and Aria — the embedded agentic copilot that runs a permissioned tool loop on OpenAI and Claude and can drive a stated goal through a durable, supervised goal-plan (pause for permission, separation of duties, async resume, self-correction). All are implemented and tested.

### MLflow 3.14 / PostgreSQL 17

The suite tracks the current open-source MLflow (`>=3.14`) and PostgreSQL 17 (pgvector + Apache AGE). Five MLflow 3.14 GenAI capabilities are wired into CALIBER, each adopting genuinely-native OSS APIs (native Review Queues are Databricks-only, so that surface is built CALIBER-native on the OSS assessment primitives):

- **Test Set → MLflow dataset sync** — push a test set to MLflow's native `mlflow.genai.datasets` registry for the dataset UI + source-trace lineage, while Postgres stays the source of truth (Test Sets page → *Sync to MLflow*, with a synced/stale badge).
- **Custom LLM judges** (`mlflow.genai.make_judge`) — author reusable judges from natural-language instructions on the **Judges** page. They grade through the calibration/refinement path and human review, and are also selectable directly on the Evaluations scorecard as `Judge.<judge_id>` scorer tokens, alongside the deterministic heuristics (`exact_match`, `token_f1`, `contains_expected`, `non_empty`).
- **Multimodal tracing** — extraction spans carry the source document as a trace attachment so the KG/OCR pipeline's inputs render in the trace viewer.
- **Trace retention / auto-archival** — opt-in server-owned archival of aged traces to object storage (configured in `deploy/mlflow/`).
- **Review Queues** — define a label schema (pass/fail, categorical, numeric, free-text), enqueue traces, and review them; answers are written back onto each trace as MLflow assessments/expectations (the **Review Queues** page).

Latest recorded local validation on 2026-06-15 (mirrors [`caliber/README.md`](./caliber/README.md)):

| Area | Result |
| --- | --- |
| Backend | 3,037 pytest tests passed, 6 skipped, plus an opt-in integration suite (9 passed); Ruff, Mypy, and the package build are green; coverage 87.88%. A supported-Python `pip-audit` follow-up stays open — transitive `diskcache`/`torch` in the optional DSPy/local-embedding stacks on Python 3.12. |
| Frontend | 903 Vitest tests passed across 60 files; TypeScript, the production Vite build, and 23 Chromium Playwright E2E tests passed. |

The requested 97% coverage target is not yet met; the current docs call out the remaining coverage gaps rather than hiding them.

## Quick Start

```bash
cd caliber
python -m venv .venv
source .venv/bin/activate
pip install -e ".[dev,s3]"

cd caliber-ui
npm install
npm run dev
```

In another shell, start the backend from `caliber/`:

```bash
source .venv/bin/activate
make dev
```

`make dev` now defaults MLflow artifacts to `s3://mlflow/mlruns` via the
local MinIO endpoint at `http://127.0.0.1:9000`, so start the suite MinIO stack
first or override `MLFLOW_ARTIFACT_ROOT` / `MLFLOW_S3_ENDPOINT_URL` explicitly.

Open the local UI at `http://127.0.0.1:5001/caliber/login`. For a full containerized bring-up (Postgres 17, MinIO, MLflow, the MLflow AI Gateway, NATS, Redis), use the stack in [`deploy/`](./deploy/) — see [`deploy/README.md`](./deploy/README.md).

## Testing

```bash
cd caliber
ruff check src tests && mypy src
pytest

cd caliber-ui
npm run lint
npm run typecheck
npm run test:coverage
npm run test:e2e
```

### Allure reports

All three test layers can emit [Allure](https://allurereport.org/) results:

| Layer | Emit results |
| --- | --- |
| Backend (pytest) | `cd caliber && make test-allure` |
| Frontend unit (vitest) | `cd caliber/caliber-ui && npm test` (emits automatically) |
| E2E (playwright) | `cd caliber/caliber-ui && npm run test:e2e` (emits + attaches screenshots/traces) |

**Emitting `allure-results/` needs no Java** — only *rendering* the HTML report does.
Render with one command from the suite root:

```bash
make allure          # combined backend + UI report, then opens it
```

`make allure` uses a local Java runtime when present and **falls back to a
Dockerized JRE** otherwise, so it works on any host that has *either* Java or
Docker. `make check` reports whether Java is available (it's optional). To
install Java directly: `brew install --cask temurin` (macOS),
`sudo apt-get install -y default-jre` (Debian/Ubuntu), or `choco install temurin`
(Windows).

The npm `allure:*` scripts (`allure:serve`, `allure:generate`, `allure:generate:all`)
run a preflight that prints install hints if no JRE is found.

**CI:** results are produced without Java, so upload `allure-results/` as an
artifact (or render in-job). To render in a GitHub Actions job, add Java first:

```yaml
- uses: actions/setup-java@v4
  with: { distribution: temurin, java-version: "21" }
- run: cd caliber/caliber-ui && npm run allure:generate:all
```

## Configuration

CALIBER is environment-configured through `CaliberConfig`. The Settings page shows a safe grouped inventory of assistant, LLM, storage, security, worker, webhook, and sandbox configuration. Common production variables include `CALIBER_DATABASE_URL`, `CALIBER_ADMIN_USERS`, `CALIBER_WORKFLOW_STORAGE_*`, `CALIBER_WORKFLOW_RUN_QUEUE_ENABLED`, and provider selections such as `CALIBER_LLM_PROVIDER`, `CALIBER_EVAL_PROVIDER`, and `CALIBER_PROMOTER_PROVIDER`.

### Single development environment (v1)

The initial product ships a **single environment**. A build deploys straight to one
live alias — there is no `dev → staging → prod` promotion ladder, no stage selector,
and no promotion-approval queue. Deploy gates still run as an optional eval check when
a workflow attaches one. The infrastructure was already single-stack (one
`deploy/compose.yaml`, one `.env`, one Postgres/MinIO/MLflow); this just collapses the
application-level promotion lifecycle to match.

The multi-stage governance machinery (eval-gated promotion + human prod sign-off) is
left intact but dormant, so it can be turned back on without code surgery:

- Frontend: flip `SINGLE_ENVIRONMENT` in [`caliber/caliber-ui/src/lib/environment.ts`](caliber/caliber-ui/src/lib/environment.ts).
- Backend: re-add the gated alias to `GATED_ALIASES` in [`caliber/src/caliber/workflows/promoter.py`](caliber/src/caliber/workflows/promoter.py), and list the extra aliases in `_PROMPT_DISCOVERY_ALIASES` in [`caliber/src/caliber/routes/prompts.py`](caliber/src/caliber/routes/prompts.py).

## Troubleshooting

| Symptom | Check |
| --- | --- |
| MinIO/S3 shows not configured | Set `CALIBER_WORKFLOW_STORAGE_BUCKET` plus endpoint, region, path-style, and credential-source variables; install the `s3` extra. |
| UI deep link returns 404 | Use the CALIBER backend `/caliber/` route or the Vite dev server with the correct `CALIBER_UI_BASE`. |
| Playwright cannot start | Run `npm run playwright:install` in `caliber/caliber-ui`. |
| Backend tests fail on Starlette TestClient | Ensure dev dependencies include `httpx2>=2.3` and reinstall with `pip install -e ".[dev]"`. |

## License

Apache 2.0 — see [caliber/LICENSE](./caliber/LICENSE).
