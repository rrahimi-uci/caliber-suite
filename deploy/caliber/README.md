# CALIBER (standalone service)

> This image belongs to the loopback-only development stack. Its default first
> login is `admin` / `admin` because Compose explicitly sets
> `CALIBER_AUTH_BOOTSTRAP_ALLOW_INSECURE_DEFAULT=true`. Change it immediately.
> Any network-reachable deployment must set that flag to `false`, supply a strong
> bootstrap password source, enable TLS/Secure cookies, and use a hardened ingress.

CALIBER as its **own ASGI service** (`uvicorn caliber.server:create_app`), kept
separate from MLflow. It reads/writes MLflow over HTTP via `MLFLOW_TRACKING_URI`
— no plugin coupling — which is the cleaner architecture (CALIBER and MLflow as
separate services behind the gateway).

Part of the `app` profile (runs alongside MLflow + gateway):

```bash
docker compose -f deploy/compose.yaml --profile app --profile nats up -d --build
make infra-up APP=1
```

- UI + API: <http://localhost:5001/caliber/> (CALIBER serves its own SPA)
- MLflow connection: `MLFLOW_TRACKING_URI=http://mlflow:5000`
- Object/workflow storage → MinIO (`http://minio:9000`)
- Metadata → the **Postgres** container
  (`postgresql+psycopg://caliber:caliber@postgres:5432/caliber`, psycopg v3).
  Override with `CALIBER_DATABASE_URL` (e.g. SQLite for a throwaway run).

The image builds the CALIBER SPA (multi-stage), installs the safer default
extras profile
`caliber-suite[s3,postgres,llm,anthropic,nats,ingest,ocr,knowledge]` + `uvicorn`,
preloads the default spaCy model for knowledge-graph extraction, and on boot
runs `alembic upgrade head` before serving. Background loops (feedback poller,
webhook dispatcher/recovery, refinement and calibration drains, workflow/Aria/knowledge
workers, scheduler, and janitor) run in-process; set
`CALIBER_BACKGROUND_TASKS_ENABLED=false` for an API-only replica.

Published workflow-service invocation bodies are capped at 1 MiB of raw JSON by default.
Override `CALIBER_SERVICE_INVOKE_MAX_BODY_BYTES` in the suite-root `.env` and restart the
service if a reviewed workload needs more; prefer managed storage references for large
content. Token-protected services reject an invalid Bearer token before consuming the body,
but this per-request bound is not a substitute for ingress connection/IP rate controls.

The runtime image executes as UID/GID 65532. The bundled Compose service additionally
drops all Linux capabilities, sets `no-new-privileges`, mounts a read-only root filesystem,
uses a bounded `/tmp` tmpfs, and provides a named `/data` volume. Those controls reduce
container privilege; they do not turn
the development Compose file or its known service credentials into a production
deployment.

## Allure report

The caliber service bind-mounts the host's `caliber/caliber-ui/allure-report`
(read-only) at `/app/allure-report` and sets `CALIBER_ALLURE_REPORT_DIR` so the
backend serves it in-app at `…/observability/allure-report` (Settings → Allure
Report links there). Generate it on the host with `make allure-report`; the
container serves the files live (no restart needed). Point the mount elsewhere
with `CALIBER_ALLURE_REPORT_HOST_DIR`. Until generated, the route shows a
"not generated yet" hint.

By default the app profile enables the richer knowledge-base extractor with:

- `CALIBER_KNOWLEDGE_GRAPH_EXTRACTOR_BACKEND=spacy`
- `CALIBER_KNOWLEDGE_GRAPH_SPACY_MODEL=en_core_web_sm`

Override either in the suite-root `.env` before rebuilding if you want a
different spaCy model or want to fall back to `heuristic`.

To ship local Hugging Face embeddings in the deployed image, rebuild with:

```bash
CALIBER_INSTALL_EXTRAS=s3,postgres,llm,anthropic,nats,ingest,ocr,knowledge,knowledge-local \
docker compose --env-file deploy/.env --env-file .env -f deploy/compose.yaml --profile app --profile nats up -d --build
```

That keeps the default image free of the optional torch stack and makes the
local-embedding runtime an explicit build-time choice. If you need a specific
torch selector for the opt-in path, also set `CALIBER_TORCH_SPEC=...` before
rebuilding. If the selected torch build is still flagged by CALIBER's runtime
advisory, local embeddings remain blocked unless you deliberately set
`CALIBER_ALLOW_FLAGGED_LOCAL_EMBEDDINGS=true` for an accepted-risk deployment.
