# CALIBER

> **Human-in-the-loop refinement pipeline for AI agents.**
> An MLflow-integrated control plane that turns production feedback into governed prompt fixes with verification plus an explicit operator Apply action, reusing MLflow's registries and evidence surfaces.

[![Python](https://img.shields.io/badge/Python-3.10%20%E2%80%93%203.12-3776AB?logo=python&logoColor=white)](pyproject.toml)
[![License: Apache 2.0](https://img.shields.io/badge/License-Apache_2.0-blue.svg)](LICENSE)

---

## What CALIBER does

When someone flags a production AI agent's response as wrong (via an MLflow Assessment), CALIBER:

1. **Verifies** — a human confirms the feedback is actionable (one click).
2. **Diagnoses** — an LLM agent identifies the root cause from the trace.
3. **Optimizes a fix** — through one of five implemented provider paths. The prompt form exposes MetaPrompt and GEPA; policy can select SkillMetaPrompt and DSPy BootstrapFewShot; DSPy MIPRO requires explicit configuration and the DSPy dependency profile.
4. **Evaluates** — the configured `EvalProvider` compares the candidate with the baseline on a pinned dataset, then the aggregate/per-dimension gate decides whether it reaches `candidate_ready`.
5. **Awaits Apply** — an operator can inspect the diff, eval comparison, and root-cause summary, then invoke Apply. This is one action boundary, not a separate vote/quorum/reject approval workflow.
6. **Promotes across a dual-write boundary** — applying a prompt candidate rotates its alias in external MLflow before the CALIBER database transaction commits its checkpoint, audit, and provenance rows. No distributed transaction joins those systems, so a failure after the alias write can require reconciliation. Explicit rollback restores the recorded outgoing target when that database record exists. Prompt version authoring can opt out of the default alias rotation with `promote: false`.

The canonical loop has **two human decisions**: verification and Apply. Its runtime depends on the provider, dataset, and optimizer;
the project does not publish a universal time-to-fix measurement.

---

## Why it exists

Modern LLM agents fail in ways unit tests can't catch — hallucinated policies, missed tool calls, multi-agent handoff conflicts, context drift across long conversations. Catching these in production is well-tooled (MLflow Tracing, OpenTelemetry GenAI, LangSmith). Doing something about them at scale is not. CALIBER's canonical prompt path connects production trace → verified feedback → refined artifact → evaluation evidence → explicit review → alias update → measurement.

CALIBER's design center of gravity:

- **MLflow-integrated.** Runs as a standalone CALIBER ASGI service that integrates with MLflow over HTTP via `MLFLOW_TRACKING_URI`. Embedded `mlflow.app` mode is unsupported.
- **Evidence-gated.** Aggregate and per-dimension thresholds gate candidate readiness; prompt release surfaces carry the resulting evidence as an advisory verdict into an explicitly authorized promotion.
- **Auditable.** Refinement-originated prompt changes retain signal-to-job lineage; direct prompt edits and releases follow their own audit path.
- **Honest about bundle scope.** Multi-target promotion plumbing exists, but automatic multi-agent bundle optimization (`MultiAgentCoord`) remains roadmap work; current submission paths are single-target.

See the suite-level [layered architecture](../ARCHITECTURE.md), its
[generated HTML page](../docs-site/m-00-layered-architecture.html), and
[`caliber-suite/`](../) for the full design specs and demo stories.

---

## Status

The implementation spans the refinement pipeline, workflow studio and queued execution, project/file
directories, settings, pattern detection, RBAC, and the SPA. Verification is not uniform across those
surfaces. The suite-level [`product-complete-report.md`](../product-complete-report.md) is the maintained
evidence ledger for current repairs, residual risks, historical test runs, and production-readiness limits;
dated totals there must not be read as a current full-suite result.

---

---

## Contributors

- [GitHub Copilot](https://github.com/features/copilot)

---

## Installation

Package metadata supports Python **3.10–3.12**, with **3.11 recommended** for local development. The
canonical GitHub functional suite currently runs on Python 3.11 rather than a three-version matrix; the
supported-Python dependency-audit helper selects 3.10–3.12. Python 3.13+ is not supported.

The repository does not currently publish a PyPI release. The distribution metadata is
named `caliber-suite` (the unrelated `caliber` name is already occupied), but until a
release is published install from a source checkout:

```bash
git clone https://github.com/rrahimi-uci/caliber-suite.git
cd caliber-suite
python -m pip install -e "./caliber"
```

For supported local development, start backing services and vanilla MLflow first, then run CALIBER separately:

```bash
# Start backing services (Postgres, MinIO) and vanilla MLflow via Compose
docker compose --env-file ../deploy/.env -f ../deploy/compose.yaml up -d
# Or with the full app profile (includes CALIBER container):
# docker compose --env-file ../deploy/.env -f ../deploy/compose.yaml --profile app up -d

# Run standalone CALIBER (backend reload enabled)
uvicorn caliber.server:create_app --factory --reload --host 127.0.0.1 --port 5001
```

The CALIBER UI is served at `http://127.0.0.1:5001/caliber/`. Set `MLFLOW_TRACKING_URI=http://127.0.0.1:5000` so CALIBER reaches the running MLflow server.

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

Install the S3 extra from the `caliber/` source directory (`pip install -e ".[s3]"`),
create the bucket in MinIO, then restart the CALIBER server.

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

# 4. Open http://127.0.0.1:5001/caliber/, verify the item, wait for the
#    refinement job to reach candidate_ready, inspect it, and invoke Apply.
#    CALIBER then rotates the prompt alias.
#    Your code keeps loading "support-agent@prod" — the next call gets
#    the new version with no code change.
```

End-to-end walkthrough: [`walkthrough.html`](../docs-site/walkthrough.html).

---

## Development

```bash
git clone https://github.com/rrahimi-uci/caliber-suite.git
cd caliber-suite/caliber

# Use a supported interpreter (3.11 recommended and used by the canonical CI suite).
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

CALIBER's supported topology is a standalone ASGI service. Uvicorn serves the CALIBER
API/SPA and its in-process background loops on `:5001`, MLflow runs separately on
`:5000`, and CALIBER reaches vanilla MLflow through `MLFLOW_TRACKING_URI`. The embedded
`mlflow.app` path remains only as unsupported internal compatibility coverage. The
service registers:

- **ASGI routes** under `/ajax-api/2.0/mlflow/caliber/*` for the verification queue, refinement jobs, Apply/rollback, workflows, and dashboard.
- **Static-file routes** under `/caliber/` for the React SPA (built from `caliber-ui/`).
- **A background feedback poller** that turns new MLflow Assessments into verification-queue items.
- **A refinement task adapter** that runs queued triage → evidence → diagnosis → candidate → eval work. A passing eval ends at `candidate_ready`; the later operator-scoped Apply action runs on the request path, not as a worker approval stage.
- **Alembic-managed CALIBER tables** through `CALIBER_DATABASE_URL`, which may and in the
  bundled stack does point to a separate logical database from MLflow's backend store.

```
Supported standalone topology:
Browser / SPA ──> CALIBER :5001 ──HTTP (`MLFLOW_TRACKING_URI`)──> MLflow :5000
       │                   │                                        │
       │                   ├──> CALIBER DB (`CALIBER_DATABASE_URL`) │
       │                   └──> CALIBER storage service             ├──> MLflow backend store
       │                                                            └──> MLflow artifact root

Unsupported internal compatibility coverage:
MLflow `mlflow.app` may still load CALIBER in-process for compatibility testing, but it is
not a supported product or developer deployment path.
```

The development Compose stack uses separate `caliber` and `mlflow` databases. See
[`deploy/caliber/README.md`](../deploy/caliber/README.md).

Architectural reading order: the [layered architecture](../ARCHITECTURE.md) first
([generated HTML](../docs-site/m-00-layered-architecture.html)), then the deeper
[platform implementation reference](../docs/01-caliber/architecture.md),
[workflow architecture](../docs/06-workflows/architecture.md), and the
[rendered documentation site](../docs-site/index.html).

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
| Security | `CALIBER_ADMIN_USERS`, `CALIBER_OPERATOR_USERS`, `CALIBER_APPROVER_USERS`, `CALIBER_CSRF_ENABLED`, `CALIBER_RATE_LIMIT_ENABLED`, `CALIBER_SERVICE_INVOKE_MAX_BODY_BYTES` |
| Operations | `CALIBER_BACKGROUND_TASKS_ENABLED`, `CALIBER_WEBHOOK_URLS`, `CALIBER_LOG_LEVEL`, `CALIBER_DATABASE_URL` |

## Deployment Guidance

Use the bundled loopback Compose stack or the documented standalone Uvicorn workflow for
local development. For a production design, run CALIBER as its own ASGI service against a
separately running vanilla MLflow server over HTTP, use PostgreSQL with independently owned
CALIBER and MLflow databases (the bundled stack uses `caliber` and `mlflow`; sharing one
schema creates Alembic ownership collisions), S3/MinIO or a shared volume for workflow
files, explicit admin/operator/approver lists, and environment-backed secret sources. The
embedded `mlflow.app` path is unsupported and should be treated only as internal
compatibility coverage. The repository's Compose files are development evidence, not a
production topology.
Queue-based workflow execution should enable
`CALIBER_WORKFLOW_RUN_QUEUE_ENABLED=true`; only replicas intended to consume the
queue should set `CALIBER_WORKFLOW_RUN_WORKER_ENABLED=true`. The loopback launchers may
opt into the first-boot `admin` / `admin` convenience. A network-reachable deployment
must keep `CALIBER_AUTH_BOOTSTRAP_ALLOW_INSECURE_DEFAULT=false`, provide a strong
bootstrap password source, use TLS/Secure cookies, and terminate direct unauthenticated
access at an appropriate identity boundary.
Published workflow-service calls accept at most 1 MiB of raw JSON by default
(`CALIBER_SERVICE_INVOKE_MAX_BODY_BYTES`). Protected endpoints validate their Bearer
token before consuming the body and revalidate policy under the enqueue lock. Use managed
storage references for larger content; the byte ceiling does not replace ingress/IP rate
controls.

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
| Published workflow service returns 413 | The raw JSON envelope exceeded `CALIBER_SERVICE_INVOKE_MAX_BODY_BYTES` (1 MiB by default). Use managed storage references or deliberately increase the deployment limit and restart. |

---

## License

[Apache 2.0](LICENSE).
