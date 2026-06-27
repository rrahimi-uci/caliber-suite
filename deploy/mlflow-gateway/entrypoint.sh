#!/usr/bin/env sh
# Launch the MLflow AI Gateway (FastAPI app) under uvicorn.
#
# ``create_app_from_env`` reads the endpoint config from MLFLOW_GATEWAY_CONFIG;
# default to the image's baked-in config when the caller doesn't override it.
set -eu

export MLFLOW_GATEWAY_CONFIG="${MLFLOW_GATEWAY_CONFIG:-/etc/mlflow/gateway.yaml}"
GATEWAY_HOST="${MLFLOW_GATEWAY_HOST:-0.0.0.0}"
GATEWAY_PORT="${MLFLOW_GATEWAY_PORT:-5002}"

echo "[mlflow-gateway] serving ${MLFLOW_GATEWAY_CONFIG} on ${GATEWAY_HOST}:${GATEWAY_PORT}"
exec uvicorn "mlflow.gateway.app:create_app_from_env" \
    --factory \
    --host "${GATEWAY_HOST}" \
    --port "${GATEWAY_PORT}"
