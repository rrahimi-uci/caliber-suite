---
audience:
  - operator
  - developer
doc_type: reference
product_area: operations
stability: ga
prerequisites:
  - A CALIBER deployment or integration configuration question
reviewed_on: 2026-08-10
version_applicability: current main branch docs contract
tags:
  - env
  - config
  - reference
  - operations
---

# Config and environment reference

This page is the operator and integrator lookup surface for the configuration
families that are exposed in the current repository. Use it as the fast index,
then move into the deeper runtime docs when you need subsystem detail.

## At a glance

| Family | Representative settings | Use when |
| --- | --- | --- |
| Local ports and launch | `CALIBER_PORT`, `MLFLOW_PORT`, `MLFLOW_GATEWAY_PORT` | bringing up the local suite |
| Core runtime | `CALIBER_DATABASE_URL`, `CALIBER_LOG_SINK` | wiring the control plane and logging |
| LLM provider and gateway | `CALIBER_LLM_PROVIDER`, `CALIBER_LLM_BASE_URL` | choosing provider routing |
| Object store | `CALIBER_OBJECT_STORE_*` | uploads, extraction, previews, log mirroring |
| Workflow storage | `CALIBER_WORKFLOW_STORAGE_*` | run workspaces, limits, retention, signed URLs |
| Event backend | `CALIBER_WORKFLOW_RUN_EVENT_BACKEND` | workflow queue and runtime eventing |

## Reference

## 1. Local launch and ports

| Setting | Purpose |
| --- | --- |
| `CALIBER_PORT` | host port for the CALIBER UI/API service |
| `MLFLOW_PORT` | host port for the MLflow UI; `start.sh` guards common collisions |
| `MLFLOW_GATEWAY_PORT` | host port for the MLflow AI Gateway surface |

## 2. Core runtime

| Setting | Purpose |
| --- | --- |
| `CALIBER_DATABASE_URL` | SQLAlchemy URL for the CALIBER metadata store |
| `CALIBER_LOG_SINK` | choose stderr-only or S3/object-store-backed JSONL mirroring |
| `CALIBER_STATIC_PREFIX` | serve behind a reverse-proxy prefix when applicable |

## 3. LLM and gateway

| Setting | Purpose |
| --- | --- |
| `CALIBER_LLM_PROVIDER` | select the default provider implementation |
| `CALIBER_LLM_DIAGNOSIS_MODEL` | default OpenAI model; `gpt-5.6-luna` when unset |
| `CALIBER_LLM_REASONING_EFFORT` | platform-wide reasoning default; `high` when unset |
| `CALIBER_GEPA_REFLECTION_MODEL` | GEPA reflection model; inherits the Luna default |
| `CALIBER_MEMORY_LLM_MODEL` | memory extraction model; inherits the Luna default |
| `CALIBER_LLM_BASE_URL` | override the OpenAI-compatible base URL |
| `CALIBER_GATEWAY_URI` | discover an MLflow AI Gateway surface |

The assistant uses the same OpenAI default through `CALIBER_ASSISTANT_MODEL`
and `CALIBER_ASSISTANT_REASONING`. Explicit per-asset and per-call selections
remain supported overrides.

## 4. Object store

| Setting | Purpose |
| --- | --- |
| `CALIBER_OBJECT_STORE_ENDPOINT_URL` | object-store endpoint |
| `CALIBER_OBJECT_STORE_REGION` | object-store region |
| `CALIBER_OBJECT_STORE_ACCESS_KEY_SOURCE` | secret source for access key |
| `CALIBER_OBJECT_STORE_SECRET_KEY_SOURCE` | secret source for secret key |
| `CALIBER_OBJECT_STORE_FORCE_PATH_STYLE` | path-style S3/MinIO behavior |

## 5. Workflow storage

| Setting | Purpose |
| --- | --- |
| `CALIBER_WORKFLOW_STORAGE_BACKEND` | `local` or `s3` |
| `CALIBER_WORKFLOW_STORAGE_BASE_URI` | local workspace root when using local backend |
| `CALIBER_WORKFLOW_STORAGE_BUCKET` | S3/MinIO bucket for workflow files |
| `CALIBER_WORKFLOW_STORAGE_PREFIX` | key prefix for workflow files |
| `CALIBER_WORKFLOW_STORAGE_INTERNAL_ENDPOINT_URL` | internal endpoint for worker-side access |
| `CALIBER_WORKFLOW_STORAGE_PUBLIC_ENDPOINT_URL` | public endpoint for signed URLs |
| `CALIBER_WORKFLOW_STORAGE_MAX_UPLOAD_BYTES` | per-upload limit |
| `CALIBER_WORKFLOW_STORAGE_MAX_RUN_BYTES` | per-run total byte limit |
| `CALIBER_WORKFLOW_STORAGE_MAX_FILES_PER_RUN` | per-run file-count limit |
| `CALIBER_WORKFLOW_STORAGE_SIGNED_URL_TTL_SECONDS` | default signed URL lifetime |
| `CALIBER_WORKFLOW_STORAGE_RETENTION_*` | run-artifact retention policy |

## 6. Event backend

| Setting | Purpose |
| --- | --- |
| `CALIBER_WORKFLOW_RUN_EVENT_BACKEND` | workflow event transport/backing mode |

## 7. Secret-source convention

Prefer settings ending in `*_SOURCE` for credentials and sensitive values. The
current runtime is designed around secret sources rather than spreading literal
keys throughout deployment files.

## 8. Related docs

- [Configuration and provider setup](../operate/configuration-and-provider-setup.md)
- [Storage and state](../operate/storage-and-state.md)
- [Health and readiness](../operate/health-and-readiness.md)
- [Platform](../01-caliber/architecture.md)
