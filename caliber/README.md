# CALIBER server package

> The Python package behind **CALIBER Suite**: a standalone ASGI service (API, web app and
> background workers) that adds governed prompt, skill, tool and workflow releases to
> [MLflow](https://mlflow.org).

[![Python](https://img.shields.io/badge/Python-3.10%20%E2%80%93%203.12-3776AB?logo=python&logoColor=white)](pyproject.toml)
[![License: Apache 2.0](https://img.shields.io/badge/License-Apache_2.0-blue.svg)](LICENSE)

This directory holds the server: the `caliber` import package (`src/caliber`), its Alembic
migrations, the React web app (`caliber-ui/`) and the backend tests. It is published as the
Python distribution **`caliber-suite`**, version `0.1.0a1`. This is an **alpha**, and this repository
does not currently publish a PyPI release, so you install from a source checkout or from wheels you
build (see below).

For what CALIBER is, the product tour and the user guides, start at the
[repository README](../README.md). What the alpha does and does not support, including the
feature flags, is in [Alpha scope and known limitations](../docs/start/alpha-scope.md). This page
covers only the package: install it, configure it, run it, test it.

## What runs

CALIBER is a **standalone ASGI service**. You run it with `uvicorn`, next to a separately
running MLflow server that it reaches over HTTP.

| Piece | Default address | Notes |
| --- | --- | --- |
| CALIBER (API, web app, workers) | `http://127.0.0.1:5001` | API under `/ajax-api/2.0/mlflow/caliber/`, web app at `/caliber/` |
| MLflow tracking and prompt registry | `http://127.0.0.1:5000` | `mlflow server`, version 3.15 or later; set `MLFLOW_TRACKING_URI` |
| CALIBER database | `CALIBER_DATABASE_URL` | PostgreSQL (supported) or SQLite (evaluation and development). Keep it separate from MLflow's own store |

Running CALIBER embedded inside MLflow (`mlflow server --app-name caliber`, or `make dev` in this
directory) is **not supported**. The server never creates its own tables: run `caliber-migrate`
before the first start and after every upgrade.

## Install

You need Python 3.10, 3.11 or 3.12 (3.11 is what CI runs the full suite on; 3.13 and later are not
supported). To try the alpha from wheels with no checkout, follow
[Alpha install](../docs/start/alpha-install.md). To work on the package, install from a checkout.
You also need Node.js 20.19 or later (or 22.12 or later) with npm to build the web app.

```bash
git clone https://github.com/rrahimi-uci/caliber-suite.git
cd caliber-suite/caliber
make install             # creates .venv and installs ".[dev,postgres,ingest,ocr,llm,knowledge]"
source .venv/bin/activate
make ui                  # builds the web app into src/caliber/ui (without it, /caliber/ answers 503)
```

`make install` picks `python3.12`, then `python3.11`, then `python3.10`; run `make install CALIBER_PYTHON=python3.11` to
choose one. Without `make`, create a virtual environment and run
`pip install -e ".[dev,postgres,ingest,ocr,llm,knowledge]"` yourself.

Optional features are extras of the `caliber-suite` distribution. The base install is enough to
run the server and the core journey.

| Extra | Adds |
| --- | --- |
| `dev` | pytest, ruff, mypy, build and test helpers |
| `postgres` | the PostgreSQL driver (`psycopg`), also used by the bundled database MCP server |
| `llm` | the OpenAI-backed provider, which real calibration and the Aria assistant's OpenAI engine need |
| `anthropic` | the Anthropic engine for the Aria assistant |
| `knowledge` | knowledge-base chunking and graph tooling (spaCy, LangChain splitters) |
| `knowledge-local` | local Hugging Face embeddings for knowledge bases (pulls in torch) |
| `ingest`, `ocr` | PDF, Office and markdown ingestion; OCR fallback (also needs the `tesseract` binary: `brew install tesseract`, or `apt-get install tesseract-ocr`) |
| `s3` | S3 and MinIO storage backends (`boto3`) |
| `dspy`, `deepeval`, `memory`, `nats`, `redis`, `docs` | the DSPy optimizers, the DeepEval scorers, agent memory, the NATS and Redis event backends, and the MkDocs toolchain |

## Run

These steps start CALIBER from your checkout on SQLite, with a native MLflow server, the simulated
language model and evaluator, and a bootstrap `admin` login. They are for a machine only you can
reach. The same steps, run against built wheels, are checked by
[`scripts/verify-alpha-install.sh`](../scripts/verify-alpha-install.sh).

Use two terminals, each with the virtual environment active (`source <your checkout>/caliber/.venv/bin/activate`)
and each in the same empty scratch directory outside the checkout (for example `mkdir ~/caliber-run && cd ~/caliber-run`),
so the database and artifact files stay out of the source tree.

```bash
# Terminal 1: MLflow, on its own SQLite file.
mlflow server \
  --backend-store-uri sqlite:///$PWD/mlflow.db \
  --default-artifact-root $PWD/mlflow-artifacts \
  --host 127.0.0.1 --port 5000
```

```bash
# Terminal 2: CALIBER.
export CALIBER_DATABASE_URL=sqlite:///$PWD/caliber.db
caliber-migrate                      # creates or upgrades the tables

export MLFLOW_TRACKING_URI=http://127.0.0.1:5000
export CALIBER_ADMIN_USERS=admin CALIBER_OPERATOR_USERS=admin CALIBER_APPROVER_USERS=admin
export CALIBER_LOG_SINK=stderr
export CALIBER_AUTH_SESSION_COOKIE_SECURE=false
export CALIBER_AUTH_BOOTSTRAP_ALLOW_INSECURE_DEFAULT=true
uvicorn caliber.server:create_app --factory --host 127.0.0.1 --port 5001
```

Open `http://127.0.0.1:5001/caliber/` and sign in as `admin` with password `admin`. Check the
server with:

```bash
curl -s http://127.0.0.1:5001/ajax-api/2.0/mlflow/caliber/readiness   # expect "ready": true
```

Notes:

- The server reads its configuration from the **process environment**. It does not read a
  `.env` file (only the repository's Docker and launcher scripts do).
- The three role variables decide what a user may do. With none set, the first login is a viewer who
  cannot create anything.
- `CALIBER_LOG_SINK=stderr` keeps logs on stderr. The default, `s3`, also mirrors them to a bucket, which
  needs the `s3` extra and an object store.
- The last two `export` lines before `uvicorn` create the weak first-boot `admin` / `admin` account. Change that
  password under Administration at once, and never use those lines on a network-reachable
  deployment: there, provide a strong bootstrap password source and keep Secure cookies.
- To use PostgreSQL instead, point `CALIBER_DATABASE_URL` at a database of its own, for example
  `postgresql+psycopg://user:password@host:5432/caliber`, and install the `postgres` extra.
- To run the whole stack in containers instead (MLflow, PostgreSQL, MinIO and CALIBER), use
  `make start` from the repository root; see [Local bring-up](../docs/operate/local-bring-up.md).
- Backend development: add `--reload --reload-dir <your checkout>/caliber/src` to the `uvicorn` command.
- Web app development: run the server as above, then `npm run dev` in `caliber-ui/`. See
  [`caliber-ui/README.md`](caliber-ui/README.md).

## Configure

Settings are `CALIBER_*` environment variables, loaded by `caliber.config.CaliberConfig`. The
authoritative list, with every default, is the variable table near the bottom of
[`src/caliber/config.py`](src/caliber/config.py); admins can also read a safe grouped inventory on the
Settings page. The task-oriented guide is
[Configuration and provider setup](../docs/operate/configuration-and-provider-setup.md). The ones you
meet first:

| Group | Variables | Shipped default |
| --- | --- | --- |
| Database | `CALIBER_DATABASE_URL` | `sqlite:///./caliber.db` |
| MLflow | `MLFLOW_TRACKING_URI` (not a `CALIBER_` setting; MLflow reads it) | unset: set it to your MLflow server |
| Roles | `CALIBER_ADMIN_USERS`, `CALIBER_OPERATOR_USERS`, `CALIBER_APPROVER_USERS` | empty |
| Providers | `CALIBER_LLM_PROVIDER`, `CALIBER_EVAL_PROVIDER` | `fake` and `fake`: simulated, with canned evaluation scores |
| | `CALIBER_PROMOTER_PROVIDER`, `CALIBER_ARTIFACT_STORE_PROVIDER` | `mlflow` (the real registry). A `fake` one is refused at startup unless `CALIBER_ALLOW_SIMULATED_PROVIDERS=true` |
| Feature flags | `CALIBER_FEATURE_ARIA` | on |
| | `CALIBER_FEATURE_INTEGRATIONS`, `CALIBER_FEATURE_COOKBOOKS`, `CALIBER_FEATURE_RELEASE_CANDIDATES` | off. Read once at startup; see [Feature flags](../docs/start/alpha-scope.md#feature-flags) |
| Aria assistant | `CALIBER_ASSISTANT_ENABLED`, `CALIBER_ASSISTANT_ENGINE`, `CALIBER_ASSISTANT_MODEL` | enabled, engine `auto` (needs `OPENAI_API_KEY` or `ANTHROPIC_API_KEY` to answer) |
| Workflow files | `CALIBER_WORKFLOW_STORAGE_BACKEND` and the other `CALIBER_WORKFLOW_STORAGE_*` settings | backend `local`, under `./caliber-workspaces` |
| Workflow runs | `CALIBER_WORKFLOW_RUN_QUEUE_ENABLED`, `CALIBER_WORKFLOW_RUN_WORKER_ENABLED`, and the checkpointing and runtime-approval settings | see [Alpha install](../docs/start/alpha-install.md#waits-approvals-and-queued-runs) |
| Knowledge bases | `CALIBER_KNOWLEDGE_GRAPH_EXTRACTOR_BACKEND`, `CALIBER_KNOWLEDGE_AGE_ENABLED`, `CALIBER_KNOWLEDGE_AGE_GRAPH_NAME` | `heuristic` extractor; see [Knowledge bases](../docs/use/knowledge-bases.md) |
| Security | `CALIBER_AUTH_SESSION_COOKIE_SECURE`, `CALIBER_AUTH_BOOTSTRAP_ALLOW_INSECURE_DEFAULT`, `CALIBER_CSRF_ENABLED`, `CALIBER_RATE_LIMIT_ENABLED`, `CALIBER_SERVICE_INVOKE_MAX_BODY_BYTES` | Secure cookie on; insecure bootstrap off; the invoke body limit is 1 MiB |
| Operations | `CALIBER_BACKGROUND_TASKS_ENABLED`, `CALIBER_LOG_LEVEL`, `CALIBER_LOG_SINK`, `CALIBER_WEBHOOK_URLS` | background tasks on, level `INFO`, log sink `s3` (set `stderr` unless you run an object store) |

With the shipped `fake` language model and evaluator, calibration runs and returns a candidate, but
the scores are canned, and every screen and record that carries them says so. For real evidence set
`CALIBER_LLM_PROVIDER=openai` (with the `llm` extra and an API key) and `CALIBER_EVAL_PROVIDER=mlflow`; see
[Real LLM and evaluation](../docs/start/alpha-install.md#real-llm-and-evaluation). OpenAI-backed paths
default to the model `gpt-5.6-luna` with `high` reasoning effort; an explicit model or reasoning
setting at the environment, asset or call level wins.

## Deployment

The Compose files in this repository are development evidence, not a production topology. For a
production design, run CALIBER as its own ASGI service against a separately running MLflow server over
HTTP, with PostgreSQL databases that CALIBER and MLflow own separately (the bundled stack uses the
databases `caliber` and `mlflow`; sharing one schema causes Alembic ownership collisions), S3 or MinIO
(or a shared volume) for workflow files, explicit role lists, and environment-backed secret sources.
Keep `CALIBER_AUTH_BOOTSTRAP_ALLOW_INSECURE_DEFAULT=false`, supply a strong bootstrap password source,
and use TLS with Secure cookies. Only replicas meant to consume the workflow queue should set
`CALIBER_WORKFLOW_RUN_WORKER_ENABLED=true`. Published workflow-service calls accept at most 1 MiB of raw
JSON by default (`CALIBER_SERVICE_INVOKE_MAX_BODY_BYTES`); this does not replace ingress rate limits.
See [Local bring-up](../docs/operate/local-bring-up.md), [Health and readiness](../docs/operate/health-and-readiness.md)
and [Backup and recovery](../docs/operate/backup-and-recovery.md).

## Test

Run these from this directory with the virtual environment active. CI runs the same checks (see
[`.github/workflows/ci.yml`](../.github/workflows/ci.yml)); [CONTRIBUTING.md](CONTRIBUTING.md) has the full
contributor workflow.

```bash
pytest -n auto --dist loadgroup      # the backend suite, with branch coverage (the gate is 90%)
ruff check . && ruff format --check .
mypy src
```

A targeted run such as `pytest tests/test_x.py` fails the coverage gate on its own, because it measures
the whole package: add `--no-cov`. The web app has its own checks (`npm run typecheck`, `npm run lint`,
`npm run test:coverage`, `npm run build`) and browser tests; see [`caliber-ui/README.md`](caliber-ui/README.md).
From the repository root, `./scripts/ci-local.sh` runs every executable CI job.

## Troubleshooting

| Symptom | Check |
| --- | --- |
| Login returns 500, or `/readiness` reports a `schema` blocker, and the log says `no such table` | Run `caliber-migrate` with the same `CALIBER_DATABASE_URL`, then restart. |
| Creating a prompt answers 503 "MLflow tracking server unreachable" | Start MLflow, or correct `MLFLOW_TRACKING_URI`. Nothing was changed. |
| `/caliber/` answers 503 "CALIBER SPA not bundled" | Run `make ui` (this builds `caliber-ui` into `src/caliber/ui`), or run `npm run dev` in `caliber-ui/` and use `http://localhost:5173/caliber/`. The API works either way. |
| Login works but you cannot create a project (403) | No role list names you: set `CALIBER_ADMIN_USERS` and restart. |
| `admin` / `admin` is rejected | No account was created. Set the two bootstrap `export` lines (or a password source) on a fresh database. |
| The server refuses to start naming a `fake` promoter or artifact store | Use the real `mlflow` ones, or deliberately set `CALIBER_ALLOW_SIMULATED_PROVIDERS=true`. |
| A page or route answers 404 with `reason_code: feature_disabled` | That area is behind a feature flag that is off. Set its `CALIBER_FEATURE_*` variable and restart. |
| Knowledge-base builds cannot parse PDF, PPTX, DOCX or XLSX files | Install the `ingest` extra (and `ocr` plus the `tesseract` binary for scanned PDFs), then restart. |
| The knowledge graph falls back to the heuristic extractor | Set `CALIBER_KNOWLEDGE_GRAPH_EXTRACTOR_BACKEND=spacy`, install the `knowledge` extra, and install the configured spaCy model. |
| A knowledge-base build says local embeddings are unavailable | Install the `knowledge-local` extra. |
| MinIO or S3 shows "Not configured" | Set `CALIBER_WORKFLOW_STORAGE_BUCKET` and the endpoint, region, path-style and credential-source settings, and install the `s3` extra. |
| A queued workflow run stays queued | Check `CALIBER_WORKFLOW_RUN_QUEUE_ENABLED` and `CALIBER_WORKFLOW_RUN_WORKER_ENABLED`, the database connection, and the worker log for lease or claim errors. |
| A published workflow service answers 413 | The JSON body exceeded `CALIBER_SERVICE_INVOKE_MAX_BODY_BYTES` (1 MiB by default). Use managed storage references, or raise the limit and restart. |
| Tests fail on starlette `TestClient` warnings | Reinstall the `dev` extra so `httpx2>=2.3` is present. |

More on operating and diagnosing a deployment: [Troubleshooting](../docs/operate/troubleshooting.md).

## License

[Apache 2.0](LICENSE).
