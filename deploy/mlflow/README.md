# MLflow (vanilla tracking server)

A **stock** MLflow tracking server — no CALIBER plugin. CALIBER runs as its own
container ([`deploy/caliber/`](../caliber/)) and connects to this one over HTTP,
so MLflow stays plain and independently upgradable.

Part of the `app` profile:

```bash
docker compose -f deploy/compose.yaml --profile app up -d --build
make infra-up APP=1
```

- Tracking UI / API: <http://localhost:5000>
- Artifacts → MinIO bucket `mlflow` at `s3://mlflow/mlruns/<exp>/<run>/artifacts/` (`http://minio:9000`)
- Metadata → the **Postgres** container (`postgresql://caliber:caliber@postgres:5432/caliber`).
  MLflow's tables don't collide with CALIBER's `caliber_*` tables, so they share
  one database. Override with `MLFLOW_BACKEND_URI` (e.g. back to SQLite).

[`entrypoint.sh`](./entrypoint.sh) runs `mlflow db upgrade`, then
`mlflow server`. Image deps (PyPI only): `mlflow` + `boto3` + `psycopg2-binary`.
