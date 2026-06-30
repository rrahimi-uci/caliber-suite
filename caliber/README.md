# CALIBER

> **Human-in-the-loop refinement pipeline for AI agents.**
> An MLflow server plugin that turns production feedback into deployed prompt fixes — with two human decisions and zero new infrastructure.

[![PyPI version](https://img.shields.io/pypi/v/caliber-mlflow.svg)](https://pypi.org/project/caliber-mlflow/)
[![Python](https://img.shields.io/pypi/pyversions/caliber-mlflow.svg)](https://pypi.org/project/caliber-mlflow/)
[![License: Apache 2.0](https://img.shields.io/badge/License-Apache_2.0-blue.svg)](LICENSE)

---

## What CALIBER does

When someone flags a production AI agent's response as wrong (via an MLflow Assessment), CALIBER:

1. **Verifies** — a human confirms the feedback is actionable (one click).
2. **Diagnoses** — an LLM agent identifies the root cause from the trace.
3. **Generates a fix** — using one of nine optimizers (MetaPrompt, TextGrad, GEPA, PromptDistill, MemAlign, MultiAgentCoord, DSPy MIPRO, DSPy BootstrapFewShot, SkillMetaPrompt), selected automatically by artifact type, diagnosis shape, and optimization landscape.
4. **Evaluates** — `mlflow.genai.evaluate()` scores the candidate against a held-out dataset with per-dimension regression gates.
5. **Asks for approval** — a human reviews the diff, eval comparison, and root-cause summary, then approves or rejects.
6. **Promotes atomically** — approved artifacts are deployed via MLflow Prompt Registry alias rotation; each rotation is audited (with an advisory gate verdict), and an explicit rollback restores the exact previously-live version. Prompts can also register a draft version without rotating the live alias (`promote: false`).

Time from feedback to deployed fix: typically **under 30 minutes** with **two human decisions**.

---

## Why it exists

Modern LLM agents fail in ways unit tests can't catch — hallucinated policies, missed tool calls, multi-agent handoff conflicts, context drift across long conversations. Catching these in production is well-tooled (MLflow Tracing, OpenTelemetry GenAI, LangSmith). Doing something about them at scale is not. CALIBER closes the loop: production trace → verified feedback → refined artifact → eval-gated deploy → measure → repeat.

CALIBER's design center of gravity:

- **MLflow-native.** No standalone services. Uses MLflow's Experiments, Traces, Assessments, Prompt Registry, Artifact Store, and `mlflow.genai.evaluate`. One deployment unit.
- **Eval-gated.** Nothing ships without a quantified, per-dimension improvement on a held-out dataset.
- **Auditable.** Every prompt change traces back to the production feedback that triggered it.
- **Multi-agent aware.** Bundles refine collaborating agents jointly when the bug spans handoffs; scopes to a single agent when the bug doesn't.

See [`caliber-suite/`](../) for the full design specs and demo stories.

---

## Status

**Phase 4 complete, Phase 5 in progress.** The full refinement pipeline (triage → evidence → diagnosis → candidate → eval → approval → promotion), GEPA optimizer integration, skill optimization workflow, atomic bundle promotion, workflow studio, queued workflow runs, project/file directories, settings inventory, pattern detection, RBAC, and the SPA are implemented and tested.

Latest local validation on 2026-06-15:

| Gate | Result |
| --- | --- |
| Backend tests | `3037 passed, 6 skipped`; coverage 87.88%. Opt-in integration suite: `9 passed`. |
| Backend static/security | `ruff check src tests`, `mypy src`, and package builds are green. Security follow-up remains open: the supported-Python audit helper now resolves LiteLLM to a fixed line, but supported Python 3.12 still flags transitive `diskcache` and `torch` in the optional DSPy/local-embedding stacks. |
| Frontend tests | `903 passed` across 60 Vitest files. |
| Frontend static/E2E | TypeScript, production Vite build, and 23 Chromium Playwright E2E tests passed. |

The repository does not yet meet a 97% coverage target; current gaps are concentrated in large UI pages/editors, assistant provider engines, assistant service edge branches, workflow-run worker branches, storage edge cases, and workflow runtime branches.

---

## Installation

Supported Python versions: **3.10-3.12**. The project is currently packaged and CI-validated against that range, with **3.11 recommended** for local development. Python 3.13+ is not yet part of the supported matrix, and some optional LLM-stack remediations are not published for Python 3.14 yet.

```bash
pip install caliber
```

Then start MLflow with the CALIBER plugin enabled:

```bash
mlflow server --app-name caliber
```

The CALIBER UI is served at `http://localhost:5000/caliber/`. The CALIBER API is mounted at `http://localhost:5000/ajax-api/2.0/mlflow/caliber/*`.

For local repo entrypoints, `make dev`, `./scripts/run-dev.sh`, and the
Playwright bootstrap now default MLflow artifacts to
`s3://mlflow/mlruns` through the local MinIO endpoint at
`http://127.0.0.1:9000` so new runs do not fall back to filesystem-backed
`mlruns` / `mlartifacts` trees unless you override `MLFLOW_ARTIFACT_ROOT`.
Set `MLFLOW_TRACKING_URI=http://127.0.0.1:5000` (suite-root `.env`) so ad-hoc
host scripts log run/trace records to the Postgres-backed server rather than a
local `mlruns/` tree.

### File Directory storage

The File Directory page can create directories backed by the local filesystem or by MinIO/S3-compatible storage. `CALIBER_WORKFLOW_STORAGE_BACKEND` sets the default backend for workflow/run files, while the File Directory UI offers MinIO/S3 as an additional choice when the S3 fields are configured.

For local plus MinIO in development, keep local as the default and set the MinIO fields in `.env`:

```bash
CALIBER_WORKFLOW_STORAGE_BACKEND=local
CALIBER_WORKFLOW_STORAGE_BASE_URI=file://./caliber-workspaces
CALIBER_WORKFLOW_STORAGE_BUCKET=caliber-workspaces
CALIBER_WORKFLOW_STORAGE_INTERNAL_ENDPOINT_URL=http://localhost:9000
CALIBER_WORKFLOW_STORAGE_PUBLIC_ENDPOINT_URL=http://localhost:9000
CALIBER_WORKFLOW_STORAGE_REGION=us-east-1
CALIBER_WORKFLOW_STORAGE_FORCE_PATH_STYLE=true
CALIBER_WORKFLOW_STORAGE_ACCESS_KEY_SOURCE=MINIO_ROOT_USER
CALIBER_WORKFLOW_STORAGE_SECRET_KEY_SOURCE=MINIO_ROOT_PASSWORD
MINIO_ROOT_USER=minioadmin
MINIO_ROOT_PASSWORD=minioadmin
```

Install the S3 extra (`pip install -e ".[s3]"` for local development or `pip install "caliber[s3]"` for a package install), create the bucket in MinIO, then restart the CALIBER server.

---

## Quickstart (post-Phase-1)

```python
# 1. Instrument your agent with MLflow Tracing.
import mlflow
mlflow.openai.autolog()
mlflow.set_experiment("exp-support-prod")

# 2. Run the agent. Traces flow to MLflow.
from agents import Agent, Runner
support_agent = Agent(
    name="support-agent",
    instructions=mlflow.load_prompt("support-agent@prod").content,
    tools=[lookup_policy, get_order],
)
result = Runner.run_sync(support_agent, user_message)

# 3. A QA reviewer thumbs-down a trace in the MLflow UI and adds an
#    Assessment with category "hallucination". CALIBER's feedback poller
#    creates a verification-queue item automatically.

# 4. Open http://localhost:5000/caliber/, verify the item, approve the
#    resulting refinement candidate. CALIBER rotates the prompt alias.
#    Your code keeps loading "support-agent@prod" — the next call gets
#    the new version with no code change.
```

End-to-end walkthrough: [`demo-story.md`](../docs/demo/demo-story.md).

---

## Development

```bash
git clone https://github.com/<your-org>/caliber-mlflow
cd caliber

# Use a supported interpreter (3.11 recommended; CI validates 3.10-3.12).
#
# Use uv for dependency management (10–100× faster than pip).
# Falls back to pip + venv if uv is not installed. The default safe dev extras
# include the OpenAI-backed provider, KB chunking/graph tooling, OCR-backed
# ingestion, and the first-party DB MCP server without pulling the flagged
# DSPy or local-Hugging-Face embedding stacks.
uv venv && uv pip install -e ".[dev,postgres,ingest,ocr,llm,knowledge]"
# or:  python -m venv .venv && source .venv/bin/activate && pip install -e ".[dev,postgres,ingest,ocr,llm,knowledge]"

# Add these only when you explicitly want the higher-risk optional stacks:
# - DSPy teleprompters / LiteLLM-backed optimizers
# - local Hugging Face embedding builds (sentence-transformers / torch)
uv pip install -e ".[dspy,knowledge-local]"
# or:  pip install -e ".[dspy,knowledge-local]"

# OCR fallback also needs the system Tesseract binary.
# macOS:  brew install tesseract
# Debian/Ubuntu:  sudo apt-get install -y tesseract-ocr

# Install pre-commit hooks (runs ruff + mypy on every commit).
pre-commit install

# Run the test suite (xdist parallel; the opt-in integration tests need external servers).
pytest -n auto -m "not integration"

# Lint + format.
ruff check src tests && ruff format src tests

# Type-check.
mypy src

# Broad local workflow-platform release gate (backend + frontend + package).
./scripts/run-workflow-platform-release-gate.sh
# Optional heavier variants:
#   ./scripts/run-workflow-platform-release-gate.sh --with-security
#   ./scripts/run-workflow-platform-release-gate.sh --with-playwright

# Run the supported-Python security audit directly. This creates or reuses
# .venv-security-audit/ on Python 3.10-3.12 and audits the default
# production-safe llm+knowledge profile.
./scripts/run-supported-python-security-audit.sh

# Audit the extended DSPy + local-embedding profile explicitly.
CALIBER_SECURITY_AUDIT_EXTRAS=llm,knowledge,dspy,knowledge-local \
  ./scripts/run-supported-python-security-audit.sh
```

Frontend development:

```bash
cd caliber-ui
npm install
npm run dev
npm run lint
npm run typecheck
npm run test:coverage
npm run test:e2e
npm run test:e2e:age   # against the running Docker stack on http://127.0.0.1:5001/caliber
```

See [CONTRIBUTING.md](CONTRIBUTING.md) for the full contributor workflow.

---

## Architecture

CALIBER is a single MLflow `mlflow.app` plugin that registers:

- **ASGI routes** under `/ajax-api/2.0/mlflow/caliber/*` for the verification queue, refinement jobs, approvals, workflows, and dashboard.
- **Static-file routes** under `/caliber/` for the React SPA (built from `caliber-ui/`).
- **A background feedback poller** that turns new MLflow Assessments into verification-queue items.
- **A refinement task adapter** that runs the 6-stage pipeline (triage → evidence → diagnosis → candidate → eval → approval) on background workers.
- **Alembic-managed extension tables** in the same database MLflow uses.

```
┌─────────────────────────────────────────────────────────┐
│                  MLflow Server Process                  │
│  ┌────────────────────────┐  ┌──────────────────────┐   │
│  │  MLflow Core           │  │  CALIBER Plugin      │   │
│  │  - Experiments         │  │  - /caliber/*  (SPA) │   │
│  │  - Runs                │  │  - /caliber/* (API)  │   │
│  │  - Traces              │  │  - Feedback poller   │   │
│  │  - Prompt Registry     │  │  - Refinement worker │   │
│  └────────────────────────┘  └──────────────────────┘   │
└──────────────────────┬──────────────────────────────────┘
                       │
                       ▼
              ┌─────────────────┐
              │  PostgreSQL     │  ← shared (MLflow + CALIBER tables)
              │  or SQLite      │
              └─────────────────┘
              ┌─────────────────┐
              │  Artifact Store │  ← MLflow-managed (S3 / MinIO / GCS / local)
              └─────────────────┘
```

Full architectural reference: [`../docs/architecture.md`](../docs/architecture.md),
[`backend.md`](../docs/architecture/backend.md), and
[`frontend.md`](../docs/architecture/frontend.md).

## Configuration Summary

CALIBER is configured through environment variables loaded by
`caliber.config.CaliberConfig`. The Settings page exposes a safe, grouped runtime
inventory for admins and super users. Important groups include:

| Group | Examples |
| --- | --- |
| Providers | `CALIBER_LLM_PROVIDER`, `CALIBER_EVAL_PROVIDER`, `CALIBER_PROMOTER_PROVIDER`, `CALIBER_ARTIFACT_STORE_PROVIDER` |
| Assistant | `CALIBER_ASSISTANT_ENABLED`, `CALIBER_ASSISTANT_ENGINE`, `CALIBER_ASSISTANT_MODEL`, `CALIBER_ASSISTANT_DISABLED_INTENTS` |
| Storage | `CALIBER_WORKFLOW_STORAGE_BACKEND`, `CALIBER_WORKFLOW_STORAGE_BUCKET`, `CALIBER_WORKFLOW_STORAGE_BASE_URI`, `CALIBER_WORKFLOW_STORAGE_INTERNAL_ENDPOINT_URL` |
| Workflow runs | `CALIBER_WORKFLOW_RUN_QUEUE_ENABLED`, `CALIBER_WORKFLOW_RUN_WORKER_ENABLED`, `CALIBER_WORKFLOW_RUN_LEASE_SECONDS`, `CALIBER_WORKFLOW_RUN_MAX_ATTEMPTS` |
| Knowledge bases | `CALIBER_KNOWLEDGE_GRAPH_EXTRACTOR_BACKEND`, `CALIBER_KNOWLEDGE_GRAPH_SPACY_MODEL`, `CALIBER_KNOWLEDGE_AGE_ENABLED`, `CALIBER_KNOWLEDGE_AGE_GRAPH_NAME` |
| Security | `CALIBER_ADMIN_USERS`, `CALIBER_OPERATOR_USERS`, `CALIBER_APPROVER_USERS`, `CALIBER_CSRF_ENABLED`, `CALIBER_RATE_LIMIT_ENABLED` |
| Operations | `CALIBER_BACKGROUND_TASKS_ENABLED`, `CALIBER_WEBHOOK_URLS`, `CALIBER_LOG_LEVEL`, `CALIBER_DATABASE_URL` |

## Deployment Guidance

For local or single-node deployments, run `mlflow server --app-name caliber` with
the package installed and the built SPA included. For production, use PostgreSQL
for the MLflow/CALIBER backend store, S3/MinIO or a shared volume for workflow
files, explicit admin/operator/approver lists, and environment-backed secret
sources. Queue-based workflow execution should enable
`CALIBER_WORKFLOW_RUN_QUEUE_ENABLED=true`; only replicas intended to consume the
queue should set `CALIBER_WORKFLOW_RUN_WORKER_ENABLED=true`.

## Troubleshooting

| Symptom | Check |
| --- | --- |
| MinIO/S3 shows "Not configured" | Set `CALIBER_WORKFLOW_STORAGE_BUCKET` and the S3 endpoint/region/path-style/credential-source fields; install `.[s3]`. |
| Knowledge-base builds cannot parse PDFs / PPTX / DOCX / XLSX | Install the document-ingestion extras (`.[ingest]`), add `.[ocr]` plus the `tesseract` binary if you need OCR fallback for scanned PDFs, then restart CALIBER. |
| Knowledge graph falls back to heuristic | Confirm `CALIBER_KNOWLEDGE_GRAPH_EXTRACTOR_BACKEND=spacy`, install `.[knowledge]`, and ensure the configured spaCy model is installed. |
| Knowledge-base build says local embeddings are unavailable | Install `.[knowledge-local]` for the local Hugging Face embedder stack, or keep the safer default profile and leave local embeddings disabled in this deployment. |
| Apache AGE retrieval is missing from the KB UI | Confirm `CALIBER_KNOWLEDGE_AGE_ENABLED=true`, the metadata DB is PostgreSQL with AGE enabled, and `CALIBER_KNOWLEDGE_AGE_GRAPH_NAME` matches the shared graph you expect to query. Native `run-dev.sh` now auto-enables AGE when `CALIBER_DATABASE_URL` points at PostgreSQL; `npm run test:e2e:age` targets the running Docker stack on `http://127.0.0.1:5001/caliber`. |
| Static `/caliber/` route returns an operator-facing 503 | Build the SPA with `npm run build` and package/copy `caliber-ui/dist` into `src/caliber/ui`, or run Vite dev server locally. |
| Starlette TestClient warnings fail tests | Reinstall dev dependencies so `httpx2>=2.3` is present. |
| Workflow queue run stays queued | Confirm `CALIBER_WORKFLOW_RUN_QUEUE_ENABLED` and `CALIBER_WORKFLOW_RUN_WORKER_ENABLED`, check database connectivity, and inspect worker logs for lease/claim errors. |

---

## License

[Apache 2.0](LICENSE).
