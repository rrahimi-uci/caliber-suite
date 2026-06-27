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
import subprocess
import sys

model = os.environ["CALIBER_KNOWLEDGE_GRAPH_SPACY_MODEL"]
try:
    importlib.import_module(model)
except ImportError:
    subprocess.check_call([sys.executable, "-m", "spacy", "download", model])
PY
fi

echo "[entrypoint] alembic upgrade head (CALIBER_DATABASE_URL=${CALIBER_DATABASE_URL})"
alembic upgrade head

echo "[entrypoint] starting CALIBER on ${CALIBER_HOST}:${CALIBER_PORT} (MLflow at ${MLFLOW_TRACKING_URI:-unset}; knowledge graph backend ${CALIBER_KNOWLEDGE_GRAPH_EXTRACTOR_BACKEND}/${CALIBER_KNOWLEDGE_GRAPH_SPACY_MODEL})"
exec uvicorn caliber.server:create_app --factory --host "${CALIBER_HOST}" --port "${CALIBER_PORT}"
