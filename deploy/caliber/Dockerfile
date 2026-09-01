# syntax=docker/dockerfile:1
#
# Standalone CALIBER service — its own ASGI app (uvicorn caliber.server:create_app),
# NOT mounted inside MLflow. It reads/writes MLflow over HTTP (MLFLOW_TRACKING_URI),
# which keeps the two cleanly separated. Build context is the suite root.
#
#   docker compose -f deploy/compose.yaml --profile app --profile nats up -d --build

# ── Stage 1: build the CALIBER SPA bundle ──────────────────────────────
FROM node:20-slim AS ui
WORKDIR /ui
COPY caliber/caliber-ui/package*.json ./
RUN npm ci --no-audit --no-fund
COPY caliber/caliber-ui/ ./
RUN npm run build

# ── Stage 2: runtime ───────────────────────────────────────────────────
FROM python:3.12-slim AS runtime
ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1
WORKDIR /app

ARG CALIBER_DEFAULT_SPACY_MODEL=en_core_web_sm
ARG CALIBER_INSTALL_EXTRAS=s3,postgres,llm,deepeval,anthropic,nats,ingest,ocr,knowledge
ARG CALIBER_TORCH_SPEC=torch>=1.11,<3
ARG TORCH_CPU_INDEX_URL=https://download.pytorch.org/whl/cpu

COPY caliber/ /app/
RUN rm -rf /app/src/caliber/ui /app/caliber-ui/node_modules
COPY --from=ui /ui/dist/ /app/src/caliber/ui/

# OCR-backed knowledge ingestion needs the system Tesseract binary in addition
# to the Python extras installed below.
RUN apt-get update \
    && apt-get install -y --no-install-recommends tesseract-ocr \
    && rm -rf /var/lib/apt/lists/*

# Plugin with S3 + Postgres + LLM + NATS extras (MinIO storage, Postgres
# metadata DB, real OpenAI-backed LLM provider and DeepEval scorer support,
# knowledge-base chunking + graph extraction, document ingestion + OCR, and
# shared event fanout)
# + uvicorn + alembic.
#
# Keep the default image on the safer `knowledge` profile. When a deployment
# explicitly opts into `knowledge-local`, preinstall the requested CPU torch
# build before resolving the rest of the extras.
RUN set -eux; \
    retry() { \
      attempt=1; \
      max_attempts="${RETRY_MAX_ATTEMPTS:-5}"; \
      until "$@"; do \
        if [ "$attempt" -ge "$max_attempts" ]; then \
          return 1; \
        fi; \
        sleep_seconds=$((attempt * 5)); \
        echo "command failed; retrying in ${sleep_seconds}s (attempt ${attempt}/${max_attempts})"; \
        sleep "$sleep_seconds"; \
        attempt=$((attempt + 1)); \
      done; \
    }; \
    retry pip install --upgrade pip; \
    retry pip install --default-timeout=120 --retries 10 alembic "uvicorn[standard]>=0.30"; \
    case ",${CALIBER_INSTALL_EXTRAS}," in \
      *,knowledge-local,*) \
        retry pip install --default-timeout=240 --retries 10 --index-url "${TORCH_CPU_INDEX_URL}" --extra-index-url https://pypi.org/simple "${CALIBER_TORCH_SPEC}" \
        ;; \
    esac; \
    retry pip install --default-timeout=240 --retries 10 -e "/app[${CALIBER_INSTALL_EXTRAS}]"; \
    retry python -m spacy download "${CALIBER_DEFAULT_SPACY_MODEL}"

COPY deploy/caliber/entrypoint.sh /usr/local/bin/entrypoint.sh
RUN chmod +x /usr/local/bin/entrypoint.sh \
    && mkdir -p /data \
    && groupadd --gid 65532 caliber \
    && useradd --uid 65532 --gid 65532 --no-create-home --home-dir /nonexistent --shell /usr/sbin/nologin caliber \
    && chown -R 65532:65532 /app /data

EXPOSE 5001
USER 65532:65532
ENTRYPOINT ["/usr/local/bin/entrypoint.sh"]
