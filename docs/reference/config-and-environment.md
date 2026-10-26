---
audience:
  - operator
  - developer
doc_type: reference
product_area: operations
stability: ga
prerequisites:
  - A CALIBER deployment or integration configuration question
reviewed_on: 2026-10-15
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
| Access and auth | `CALIBER_AUTH_MODE`, `CALIBER_ADMIN_USERS`, `CALIBER_CSRF_ENABLED` | who may sign in and what each user may do |
| LLM provider and gateway | `CALIBER_LLM_PROVIDER`, `CALIBER_LLM_BASE_URL` | choosing provider routing |
| Release path | `CALIBER_PROMOTER_PROVIDER`, `CALIBER_ARTIFACT_STORE_PROVIDER`, `CALIBER_EVAL_PROVIDER`, `CALIBER_ALLOW_SIMULATED_PROVIDERS` | choosing whether Apply performs a real MLflow release (the default) and whether evaluation scores are real |
| Object store | `CALIBER_OBJECT_STORE_*` | uploads, extraction, previews, log mirroring |
| Workflow storage | `CALIBER_WORKFLOW_STORAGE_*` | run workspaces, limits, retention, signed URLs |
| Event backend | `CALIBER_WORKFLOW_RUN_EVENT_BACKEND` | workflow queue and runtime eventing |
| Feature flags | `CALIBER_FEATURE_ARIA`, `CALIBER_FEATURE_INTEGRATIONS`, `CALIBER_FEATURE_COOKBOOKS`, `CALIBER_FEATURE_RELEASE_CANDIDATES` | turning on the preview, record-only and experimental areas the alpha leaves off |

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
| `CALIBER_DATABASE_URL` | SQLAlchemy URL for the CALIBER metadata store; `sqlite:///./caliber.db` when unset. The server never creates its own tables: run `caliber-migrate` before the first start and after every upgrade |
| `CALIBER_LOG_SINK` | `s3` (the default) keeps stderr and mirrors JSONL to the object store when available; `stderr` disables the mirroring |
| `CALIBER_STATIC_PREFIX` | serve behind a reverse-proxy prefix when applicable |

## 3. LLM and gateway

| Setting | Purpose |
| --- | --- |
| `CALIBER_LLM_PROVIDER` | select the default provider implementation; `fake` (a deterministic stub) when unset, `openai` needs the `llm` extra |
| `CALIBER_LLM_DIAGNOSIS_MODEL` | default OpenAI model; `gpt-5.6-luna` when unset |
| `CALIBER_LLM_REASONING_EFFORT` | platform-wide reasoning default; `high` when unset |
| `CALIBER_GEPA_REFLECTION_MODEL` | GEPA reflection model; inherits the Luna default |
| `CALIBER_MEMORY_LLM_MODEL` | memory extraction model; inherits the Luna default |
| `CALIBER_LLM_BASE_URL` | override the OpenAI-compatible base URL |
| `CALIBER_GATEWAY_URI` | discover an MLflow AI Gateway surface |
| `CALIBER_PROMOTER_PROVIDER` | `mlflow` (default) registers a new prompt version and moves the live alias on Apply; `fake` only simulates and needs the opt-in below |
| `CALIBER_ARTIFACT_STORE_PROVIDER` | `mlflow` (default) reads prompts from the registry; `fake` needs the opt-in below |
| `CALIBER_EVAL_PROVIDER` | `fake` (the default) returns canned scores that are marked `simulated` everywhere they are shown or stored; `mlflow` wraps `mlflow.genai.evaluate` |
| `CALIBER_ALLOW_SIMULATED_PROVIDERS` | set `true` to permit a `fake` promoter or artifact store, or a real LLM provider combined with the `fake` evaluator (tests, demos); startup refuses each of those otherwise, and every simulated Apply is marked `simulated` |

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
| `CALIBER_WORKFLOW_RUN_EVENT_BACKEND` | `in_process` (the default), `database`, `nats` or `redis` |
| `CALIBER_WORKFLOW_RUN_QUEUE_ENABLED` | `false` by default; the run queue that wait and human-approval nodes need, together with `CALIBER_WORKFLOW_RUN_CHECKPOINTING_ENABLED` |

## 7. Access and auth

| Setting | Purpose |
| --- | --- |
| `CALIBER_PROFILE` | `development` (the default: start with whatever is set) or `reference` (refuse to start while any setting breaks the reference deployment: a SQLite database, CSRF off, the dev identity fallback, the `admin/admin` bootstrap, `trusted_header` without a proxy secret, egress policy off, plain-HTTP MCP, self-approval; every violation is listed with the variable to change) |
| `CALIBER_AUTH_MODE` | `session` (the default: server-side sessions and personal access tokens) or `trusted_header` (trusts `X-CALIBER-User`; only behind a proxy) |
| `CALIBER_ADMIN_USERS`, `CALIBER_OPERATOR_USERS`, `CALIBER_APPROVER_USERS` | comma-separated user ids granted `caliber.admin`, `caliber.operator` and `caliber.approver`; all empty by default, so a first login is a viewer. These are global scopes, not project roles, and `caliber.admin` is not an implicit project member |
| `CALIBER_CSRF_ENABLED` | `false` by default; when `true`, every `POST`, `PATCH`, `PUT` and `DELETE` (bearer-token calls included) needs an `X-CALIBER-CSRF` token, signed with the secret named by `CALIBER_CSRF_SIGNING_SECRET_ENV` and valid for `CALIBER_CSRF_TOKEN_TTL_SECONDS` (3600 by default) |

Optimizer plugins are enabled by a separate allowlist; see [Writing a CALIBER plugin](../sdk/plugins.md).

## 8. Feature flags

Four settings gate the areas the alpha does not stand behind. Three are **off by default**; `CALIBER_FEATURE_ARIA`
is the exception and defaults **on**. Each is read once at startup, so restart CALIBER after changing one. A
set variable accepts `true`, `1`, `yes` or `on` (any case) as true and anything else as false (so a typo turns
a flag off, even Aria's); leaving it unset falls back to the field's own default. Each is a
`feature_<id>_enabled` field in `config.py`.

| Setting | Default | What it turns on |
| --- | --- | --- |
| `CALIBER_FEATURE_ARIA` | `true` | The Aria assistant and plans (preview): the `/assistant/*` and `/aria/*` routes and the Plans page. `CALIBER_ASSISTANT_ENABLED` still decides whether an enabled assistant accepts requests |
| `CALIBER_FEATURE_INTEGRATIONS` | `false` | MCP servers and OpenAPI integrations (preview): the `/mcp-servers*` and `/openapi-integrations*` routes and their pages. While it is off, a workflow MCP node is reported as unavailable ("A registered MCP server that CALIBER can reach (needs CALIBER_FEATURE_INTEGRATIONS=true)") |
| `CALIBER_FEATURE_COOKBOOKS` | `false` | Cookbooks (preview): the `/cookbooks*` routes and the Cookbooks page. A lab can need another flag as well; its README says which |
| `CALIBER_FEATURE_RELEASE_CANDIDATES` | `false` | Release-candidate signoff and workspace releases (record-only and experimental): the `/releases/candidates*`, `/releases/report-jobs*` and `/projects/{id}/releases*` routes and the candidate UI. The live board, the timeline and release operations are core and always on |

While a flag is off, every route of its area answers HTTP 404 with `reason_code: feature_disabled`, and the web
app hides the area's navigation entries. `GET /capabilities` reports all four under `features` (`enabled`,
`env_var`, `label`, `stability`). In the Docker Compose stack the four variables pass through from the
environment with the same defaults; `.env.example` documents them. See
[Feature flags](../start/alpha-scope.md#feature-flags) for what changing one means.

## 9. Secret-source convention

Prefer settings ending in `*_SOURCE` for credentials and sensitive values. The
current runtime is designed around secret sources rather than spreading literal
keys throughout deployment files.

## 10. Related docs

- [Configuration and provider setup](../operate/configuration-and-provider-setup.md)
- [Storage and state](../operate/storage-and-state.md)
- [Health and readiness](../operate/health-and-readiness.md)
- [Platform](../01-caliber/architecture.md)
