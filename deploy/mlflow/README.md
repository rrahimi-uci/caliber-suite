# MLflow (vanilla tracking server)

> Part of the loopback-only development stack. The bundled credentials and
> permissive host setting are not a production configuration.

A **stock** MLflow tracking server — no CALIBER plugin. CALIBER runs as its own
container ([`deploy/caliber/`](../caliber/)) and connects to this one over HTTP,
so MLflow stays plain and independently upgradable.

Part of the `app` profile:

```bash
docker compose -f deploy/compose.yaml --profile app --profile nats up -d --build
make infra-up APP=1
```

- Tracking UI / API: <http://localhost:5000>
- Artifacts → MinIO bucket `mlflow` at `s3://mlflow/mlruns/<exp>/<run>/artifacts/` (`http://minio:9000`)
- Metadata → a dedicated **`mlflow` database** on the Postgres container. It is
  separate from CALIBER's `caliber` database so the two Alembic histories cannot
  collide; the connection pins MLflow to the `public` schema. Override with
  `MLFLOW_BACKEND_URI` (for example, back to SQLite).

[`entrypoint.sh`](./entrypoint.sh) creates the dedicated database when necessary
and then starts `mlflow server`; the server creates and stamps a fresh schema. It
deliberately does **not** run `mlflow db upgrade` against an empty database.
Existing deployments that require a future migration must run the MLflow upgrade
command explicitly. Image deps (PyPI only): `mlflow` + `boto3` +
`psycopg2-binary`.
