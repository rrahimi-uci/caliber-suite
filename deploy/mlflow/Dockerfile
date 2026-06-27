# syntax=docker/dockerfile:1
#
# Vanilla MLflow tracking server — NO CALIBER plugin. CALIBER runs as its own
# service (see deploy/caliber/) and talks to this one over HTTP. Keeping MLflow
# plain means it stays a stock, independently-upgradable tracking server.
#
# Self-contained build: needs no repo source, only PyPI.

FROM python:3.12-slim
ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1
RUN pip install --upgrade pip \
 && pip install "mlflow>=3.14.0,<4" "boto3>=1.34" "psycopg2-binary>=2.9"

# --- Guardrails AI — backend for MLflow's gateway guardrails -----------------
# MLflow's gateway guardrails (``mlflow.gateway.guardrails``, enforced pre/post
# LLM in ``mlflow/server/gateway_api.py``) and the guardrail scorers
# (``mlflow.genai.scorers.guardrails``: ToxicLanguage / DetectPII /
# DetectJailbreak / NSFWText / SecretsPresent / GibberishText) are backed by the
# ``guardrails-ai`` package + its Hub validators.
#
# Installed HERE — in the standalone MLflow image (python 3.12, only
# mlflow+boto3+psycopg2) — on purpose: it keeps guardrails-ai (which downgrades
# huggingface_hub/rich/typer) OUT of the CALIBER image, so CALIBER's
# dspy/mem0/torch stack is untouched. Guardrails run at the gateway, which all
# CALIBER LLM traffic already routes through.
#
# The Hub validators download models at build and require a (free) Guardrails
# Hub token. Pass it as a build arg; when absent the package is still installed
# and validators can be added at runtime via ``guardrails hub install``.
ARG GUARDRAILS_HUB_TOKEN=""
RUN pip install "guardrails-ai>=0.5"
RUN if [ -n "$GUARDRAILS_HUB_TOKEN" ]; then \
        guardrails configure --token "$GUARDRAILS_HUB_TOKEN" --disable-metrics \
     && guardrails hub install hub://guardrails/toxic_language \
     && guardrails hub install hub://guardrails/detect_pii \
     && guardrails hub install hub://guardrails/detect_jailbreak \
     && guardrails hub install hub://guardrails/nsfw_text \
     && guardrails hub install hub://guardrails/secrets_present \
     && guardrails hub install hub://guardrails/gibberish_text ; \
    else \
        echo "[build] GUARDRAILS_HUB_TOKEN unset — guardrails-ai installed without Hub validators." ; \
        echo "[build] Add them later: guardrails configure --token <T> && guardrails hub install hub://guardrails/<validator>" ; \
    fi

# Server-owned trace retention/archival config (MLflow 3.13+). Baked in as the
# default path; activated only when MLFLOW_TRACE_ARCHIVAL_CONFIG points at it
# (see compose.yaml + entrypoint.sh) so archival stays opt-in.
COPY trace-archival.yaml /etc/mlflow/trace-archival.yaml
COPY entrypoint.sh /usr/local/bin/entrypoint.sh
RUN chmod +x /usr/local/bin/entrypoint.sh
EXPOSE 5000
ENTRYPOINT ["/usr/local/bin/entrypoint.sh"]
