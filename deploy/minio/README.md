# MinIO (object storage)

> Development service: both published ports bind to `127.0.0.1`, and the
> example credentials are intentionally well known. Do not expose this Compose
> service or reuse those credentials outside a local workstation.

S3-compatible object storage that backs three things in the suite:

| Use | App env var | Default bucket |
| --- | --- | --- |
| MLflow artifacts (`s3://mlflow/mlruns/…`) | `MLFLOW_ARTIFACT_ROOT=s3://…`, `MLFLOW_S3_ENDPOINT_URL` | `mlflow` |
| CALIBER workflow/run files (Object Store) | `CALIBER_WORKFLOW_STORAGE_*` | `caliber-workspaces` |
| CALIBER S3 log sink | `CALIBER_LOG_SINK=s3`, `CALIBER_LOG_BUCKET` | `caliber-log` |

## Run

```bash
# standalone
docker compose -f deploy/minio/compose.yaml up -d

# or as part of the umbrella stack
docker compose -f deploy/compose.yaml up -d
```

- S3 API: <http://localhost:9000>
- Console: <http://localhost:9001> (login with `MINIO_ROOT_USER` / `MINIO_ROOT_PASSWORD`)

The `createbuckets` job runs once on startup and creates the three buckets
above (idempotent). Override names/ports/creds via `deploy/.env` — see
[`deploy/.env.example`](../.env.example).

## Point the apps at it

In the suite `.env` (defaults already match this compose):

```dotenv
MINIO_ROOT_USER=minioadmin
MINIO_ROOT_PASSWORD=minioadmin
AWS_ACCESS_KEY_ID=minioadmin
AWS_SECRET_ACCESS_KEY=minioadmin

CALIBER_WORKFLOW_STORAGE_BACKEND=s3
CALIBER_WORKFLOW_STORAGE_BUCKET=caliber-workspaces
CALIBER_WORKFLOW_STORAGE_INTERNAL_ENDPOINT_URL=http://localhost:9000
CALIBER_WORKFLOW_STORAGE_PUBLIC_ENDPOINT_URL=http://localhost:9000
CALIBER_WORKFLOW_STORAGE_FORCE_PATH_STYLE=true

MLFLOW_TRACKING_URI=http://localhost:5000
MLFLOW_ARTIFACT_ROOT=s3://mlflow/mlruns
MLFLOW_S3_ENDPOINT_URL=http://localhost:9000
```

> Note: `INTERNAL_ENDPOINT_URL` is what the server uses for I/O; when the apps
> themselves run in containers on this compose network, set it to
> `http://minio:9000` (the service name) instead of `localhost`.
