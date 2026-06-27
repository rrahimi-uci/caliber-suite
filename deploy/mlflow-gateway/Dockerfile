# syntax=docker/dockerfile:1
#
# MLflow AI Gateway (a.k.a. LLM Gateway / Deployments Server).
#
# A standalone FastAPI app (``mlflow.gateway.app:create_app_from_env``) that
# fronts the configured LLM providers behind one set of OpenAI-style endpoints
# at ``/gateway/<name>/invocations`` and lists them at ``/api/2.0/endpoints/``.
# CALIBER discovers + (opt-in) routes through it; guardrails attach here so they
# apply to all traffic without touching CALIBER.
#
# Kept separate from the CALIBER image on purpose: the gateway extra
# (fastapi/slowapi/uvicorn/tiktoken/watchfiles) and any guardrails-ai stack stay
# out of CALIBER's dspy/mem0/torch environment.
#
# Self-contained: needs no repo source, only PyPI.

FROM python:3.12-slim
ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1
RUN pip install --upgrade pip \
 && pip install "mlflow[gateway]>=3.14.0,<4" "aiohttp>=3.9"

# The endpoints config (providers + models). Mounted/overridable at runtime via
# the MLFLOW_GATEWAY_CONFIG env var; baked in as the default so the image runs
# out of the box.
COPY gateway.yaml /etc/mlflow/gateway.yaml
COPY entrypoint.sh /usr/local/bin/entrypoint.sh
RUN chmod +x /usr/local/bin/entrypoint.sh

EXPOSE 5002
ENTRYPOINT ["/usr/local/bin/entrypoint.sh"]
