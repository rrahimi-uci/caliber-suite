# `deploy/` — containerized infra for the CALIBER suite

This is the home for the suite's open-source backing services, each
**containerized** and each in its **own folder**, tied together by one umbrella
compose file. The goal: stand up everything the apps depend on with a single
command, and make adding a new service (MinIO, NATS, MLflow, Ollama, …) trivial.

```text
deploy/
├── compose.yaml        # umbrella — `include`s every service below
├── .env.example        # infra ports / bucket names (optional deploy/.env)
├── minio/              # S3-compatible object storage  (+ auto bucket creation)
├── adminer/            # Postgres web UI (DB console, like MinIO's)
├── age-viewer/         # Apache AGE graph console (renders the graph DB)
├── mlflow/             # vanilla MLflow tracking server   (app profile)
├── mlflow-gateway/     # MLflow AI Gateway — LLM routing (:5002, app profile)
├── caliber/            # standalone CALIBER service — serves its own SPA (app profile)
├── nats/               # NATS message bus (optional profile)
└── (../caliber/deploy/mcp/)  # Postgres+pgvector+AGE, referenced in place
```

## App tier — MLflow and CALIBER are **separated**

The `app` profile runs independent containers, mirroring the native
`make start` topology, rather than mounting CALIBER inside MLflow:

```text
        CALIBER UI ──► caliber (:5001/caliber/)  ──HTTP──►  mlflow (:5000, vanilla)
                            │                                    │
                            ├──────────► MinIO (:9000) ◄─────────┘   (artifacts + object storage)
                            └──────────► mlflow-gateway (:5002)       (LLM routing)
```

- **MLflow stays vanilla** — a stock tracking server, independently upgradable.
- **CALIBER is its own ASGI service** that serves its SPA at `/caliber` and
  reads/writes MLflow over `MLFLOW_TRACKING_URI` — no plugin coupling.
- Each service is reached **directly** (CALIBER at :5001, MLflow at :5000, …);
  the **Settings → Services** tab in CALIBER lists them all with live health.

## Quick start

```bash
cp .env.example .env                      # put app secrets here, e.g. OPENAI_API_KEY
cp deploy/.env.example deploy/.env        # optional: infra port/bucket overrides
./start.sh                                # or: make start
```

That brings up **MinIO** (with buckets pre-created), **Postgres** (pgvector +
Apache AGE), **MLflow**, the **MLflow AI Gateway**, **CALIBER**, and the shared event bus
required by the deployed app backend. `./start.sh` selects **NATS**.

For backing services only:

```bash
docker compose --env-file deploy/.env -f deploy/compose.yaml up -d
docker compose --env-file deploy/.env -f deploy/compose.yaml --profile nats up -d
docker compose --env-file deploy/.env -f deploy/compose.yaml down
```

| Service | Ports | Console / notes |
| --- | --- | --- |
| MinIO | 9000 (S3), 9001 (console) | <http://localhost:9001> — login `minioadmin` / `minioadmin` |
| Postgres (pgvector + AGE) | 5432 | `postgresql://caliber:caliber@localhost:5432/caliber` |
| Adminer (Postgres UI) | 8081 | [prefill link](http://localhost:8081/?pgsql=postgres&username=caliber&db=caliber) fills all but password (`caliber`) |
| AGE Viewer (graph console) | 8082 | <http://localhost:8082> — **auto-connects** to graph `knowledge_graph` (no login) |
| NATS *(profile `nats`)* | 4222 (client), 8222 (monitor) | <http://localhost:8222> |
| CALIBER *(profile `app`)* | 5001 | <http://localhost:5001/caliber/> ← start here |
| MLflow *(profile `app`)* | 5000 | vanilla tracking server |
| MLflow AI Gateway *(profile `app`)* | 5002 | <http://localhost:5002/api/2.0/endpoints/> (LLM gateway) |

Run the full, separated app tier in containers too:

```bash
docker compose --env-file deploy/.env --env-file .env -f deploy/compose.yaml --profile app --profile nats up -d --build
# → open http://localhost:5001/caliber/
```

## Why containers?

The apps themselves (MLflow + CALIBER) run today as native
Python processes via `make start` for fast local dev. Their **stateful
dependencies** — object storage, the graph/vector database, and (later) a
broker — are the right things to containerize: pinned versions, isolated data
volumes, and reproducible across machines without per-developer install steps.
This `deploy/` folder is exactly that boundary.

## Add a new service

1. `mkdir deploy/<service>` and add `deploy/<service>/compose.yaml` with a
   single service (give it a `volumes:` entry for any state, and a `profiles:`
   entry if it should be opt-in).
2. Add `- <service>/compose.yaml` to the `include:` list in
   [`compose.yaml`](./compose.yaml).
3. Document its env keys in [`.env.example`](./.env.example) and a short
   `deploy/<service>/README.md`.

## Pointing the apps at these services

See [`minio/README.md`](./minio/README.md) for the exact `CALIBER_*` / `MLFLOW_*`
env mapping. Use `deploy/.env` for infra-only knobs such as ports and bucket
names; use the suite-root `.env` for app/provider settings and secrets such as
`OPENAI_API_KEY`. `./start.sh`, `./stop.sh`, and the top-level `Makefile` load
both files, with the suite-root `.env` taking precedence.

The containerized CALIBER app enables Apache AGE-backed knowledge-base sync and
retrieval by default (`CALIBER_KNOWLEDGE_AGE_ENABLED=true`) so the Knowledge
Base Build and Playground flows can expose the `Apache AGE graph` retrieval
mode against the shared `knowledge_graph` graph out of the box. For workflow
live events, the deploy stack uses NATS as the shared cross-instance fan-out
backend. The app image also ships the
document-ingestion and OCR stack (`pypdf`, `python-pptx`, `python-docx`,
`openpyxl`, `PyMuPDF`, `pytesseract`, and the `tesseract-ocr` binary) so
knowledge-base builds can parse common office documents and scanned PDFs inside
the container without extra manual layering.

> The app tier (MLflow, MLflow AI Gateway, CALIBER) is containerized too — see the
> `app`-profile services above. They mirror the native `make start` topology, so
> that dev loop keeps working unchanged; the containers are an alternative, not a
> replacement.
