#!/usr/bin/env sh
# Launch the MLflow AI Gateway (FastAPI app) under uvicorn.
#
# ``create_app_from_env`` reads the endpoint config from MLFLOW_GATEWAY_CONFIG;
# default to the image's baked-in config when the caller doesn't override it.
#
# The config is rendered first: the gateway validates every endpoint up front,
# so a provider whose key is absent would otherwise abort startup and take the
# configured endpoints down with it (see render_config.py). Rendering drops the
# unconfigured ones and serves the rest. Set MLFLOW_GATEWAY_SKIP_RENDER=1 to
# serve the config verbatim and get the gateway's own strict validation back.
set -eu

export MLFLOW_GATEWAY_CONFIG="${MLFLOW_GATEWAY_CONFIG:-/etc/mlflow/gateway.yaml}"
GATEWAY_HOST="${MLFLOW_GATEWAY_HOST:-0.0.0.0}"
GATEWAY_PORT="${MLFLOW_GATEWAY_PORT:-5002}"

if [ "${MLFLOW_GATEWAY_SKIP_RENDER:-0}" != "1" ]; then
    RENDERED="${TMPDIR:-/tmp}/gateway.rendered.yaml"
    python3 /usr/local/bin/render_config.py "${MLFLOW_GATEWAY_CONFIG}" "${RENDERED}"
    export MLFLOW_GATEWAY_CONFIG="${RENDERED}"
fi

echo "[mlflow-gateway] serving ${MLFLOW_GATEWAY_CONFIG} on ${GATEWAY_HOST}:${GATEWAY_PORT}"
exec uvicorn "mlflow.gateway.app:create_app_from_env" \
    --factory \
    --host "${GATEWAY_HOST}" \
    --port "${GATEWAY_PORT}"
