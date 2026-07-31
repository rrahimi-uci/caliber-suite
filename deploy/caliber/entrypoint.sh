#!/usr/bin/env bash
# Migrate the CALIBER metadata DB, then serve the standalone ASGI app.
set -euo pipefail
cd /app

: "${CALIBER_HOST:=0.0.0.0}"
: "${CALIBER_PORT:=5001}"
: "${CALIBER_DATABASE_URL:=sqlite:////data/caliber.db}"
: "${CALIBER_KNOWLEDGE_GRAPH_EXTRACTOR_BACKEND:=spacy}"
: "${CALIBER_KNOWLEDGE_GRAPH_SPACY_MODEL:=en_core_web_sm}"
export CALIBER_DATABASE_URL
export CALIBER_KNOWLEDGE_GRAPH_EXTRACTOR_BACKEND
export CALIBER_KNOWLEDGE_GRAPH_SPACY_MODEL

mkdir -p /data

if [[ "${CALIBER_KNOWLEDGE_GRAPH_EXTRACTOR_BACKEND}" == "spacy" ]]; then
  echo "[entrypoint] ensuring spaCy model ${CALIBER_KNOWLEDGE_GRAPH_SPACY_MODEL} is available"
  python - <<'PY'
import importlib
import os

model = os.environ["CALIBER_KNOWLEDGE_GRAPH_SPACY_MODEL"]
try:
    importlib.import_module(model)
except ImportError:
    raise SystemExit(
        f"spaCy model {model!r} is not installed in this read-only image; "
        "rebuild with CALIBER_DEFAULT_SPACY_MODEL set to that model"
    ) from None
PY
fi

# Do not print the URL: PostgreSQL URLs commonly embed the database password.
echo "[entrypoint] alembic upgrade head"
alembic upgrade head

# Tracking URIs can also embed credentials; log topology without echoing the value.
echo "[entrypoint] starting CALIBER on ${CALIBER_HOST}:${CALIBER_PORT} (MLflow integration configured; knowledge graph backend ${CALIBER_KNOWLEDGE_GRAPH_EXTRACTOR_BACKEND}/${CALIBER_KNOWLEDGE_GRAPH_SPACY_MODEL})"
exec uvicorn caliber.server:create_app --factory --host "${CALIBER_HOST}" --port "${CALIBER_PORT}"
