#!/usr/bin/env bash
# Vanilla MLflow tracking server. Upgrades its schema, then serves.
set -euo pipefail

: "${MLFLOW_HOST:=0.0.0.0}"
: "${MLFLOW_PORT:=5000}"
: "${MLFLOW_BACKEND_URI:=sqlite:////data/mlflow.db}"
: "${MLFLOW_ARTIFACT_ROOT:=s3://mlflow/mlruns}"

mkdir -p /data

# For a Postgres backend, ensure the target database exists — `mlflow db upgrade`
# won't create it. (On a fresh `down -v` the server only has the `caliber` db.)
if [[ "${MLFLOW_BACKEND_URI}" == postgresql* ]]; then
  python - <<'PY'
import os, urllib.parse, psycopg2
u = urllib.parse.urlparse(os.environ["MLFLOW_BACKEND_URI"])
db = (u.path or "/mlflow").lstrip("/")
conn = psycopg2.connect(host=u.hostname, port=u.port or 5432,
                        user=u.username, password=u.password, dbname="postgres")
conn.autocommit = True
with conn.cursor() as cur:
    cur.execute("SELECT 1 FROM pg_database WHERE datname=%s", (db,))
    if cur.fetchone():
        print(f"[entrypoint] database {db!r} exists")
    else:
        cur.execute('CREATE DATABASE "%s"' % db.replace('"', '""'))
        print(f"[entrypoint] created database {db!r}")
conn.close()
PY
fi

# NOTE: we do NOT run `mlflow db upgrade` here. On a fresh database that command
# fails (it only migrates an existing schema; it never creates the base tables).
# The MLflow server's SqlAlchemyStore creates+stamps the schema itself on first
# start. If a future MLflow version needs a migration on an existing DB, the
# server will say so and you run `mlflow db upgrade <uri>` once (tables exist).

# Opt-in server-owned trace retention/archival (MLflow 3.13+). When
# MLFLOW_TRACE_ARCHIVAL_CONFIG points at a YAML config, pass it through so the
# server archives aged traces to object storage. Unset → archival stays off.
archival_args=()
if [[ -n "${MLFLOW_TRACE_ARCHIVAL_CONFIG:-}" ]]; then
  echo "[entrypoint] trace archival enabled via ${MLFLOW_TRACE_ARCHIVAL_CONFIG}"
  archival_args=(--trace-archival-config "${MLFLOW_TRACE_ARCHIVAL_CONFIG}")
fi

echo "[entrypoint] starting mlflow server on ${MLFLOW_HOST}:${MLFLOW_PORT}"
exec mlflow server \
  --host "${MLFLOW_HOST}" \
  --port "${MLFLOW_PORT}" \
  --backend-store-uri "${MLFLOW_BACKEND_URI}" \
  --default-artifact-root "${MLFLOW_ARTIFACT_ROOT}" \
  "${archival_args[@]}"
