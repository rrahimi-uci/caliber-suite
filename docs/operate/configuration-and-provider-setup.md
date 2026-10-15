---
audience:
  - operator
  - developer
doc_type: how-to
product_area: operations
stability: ga
prerequisites:
  - A CALIBER deployment plan or checkout
reviewed_on: 2026-10-15
version_applicability: current main branch docs contract
tags:
  - configuration
  - providers
  - env
  - operations
---

# Configuration and provider setup

Use this page when you need the operator path for configuring CALIBER: core
runtime, ports, database, LLM provider choice, object store, workflow storage,
and event backend.

## At a glance

| Area | Representative settings | Deep reference |
| --- | --- | --- |
| App ports and local bring-up | `CALIBER_PORT` (5001), `MLFLOW_PORT` (5000), `MLFLOW_GATEWAY_PORT` (5002) | [Local bring-up](../operate/local-bring-up.md) |
| Metadata database | `CALIBER_DATABASE_URL` | [Platform](../01-caliber/architecture.md) |
| Migrations | `caliber-migrate` (reads `CALIBER_DATABASE_URL`) | [Alpha install](../start/alpha-install.md) |
| Roles and first login | `CALIBER_ADMIN_USERS`, `CALIBER_OPERATOR_USERS`, `CALIBER_APPROVER_USERS`, `CALIBER_AUTH_BOOTSTRAP_*` | [Alpha install](../start/alpha-install.md) |
| LLM provider and gateway | `CALIBER_LLM_PROVIDER`, `CALIBER_LLM_BASE_URL` | [Gateways](../10-gateways/architecture.md) |
| Release path (promoter, artifact store) | `CALIBER_PROMOTER_PROVIDER`, `CALIBER_ARTIFACT_STORE_PROVIDER`, `CALIBER_ALLOW_SIMULATED_PROVIDERS` | [Refinement loop](../refinement-loop.md) |
| Object store (connection only) | `CALIBER_OBJECT_STORE_ENDPOINT_URL`, `_REGION`, `_ACCESS_KEY_SOURCE`, `_SECRET_KEY_SOURCE`, `_FORCE_PATH_STYLE` | [Object store](../07-object-store/architecture.md) |
| Workflow file storage | `CALIBER_WORKFLOW_STORAGE_*` | [Storage and state](../operate/storage-and-state.md) |
| Event backend | `CALIBER_WORKFLOW_RUN_EVENT_BACKEND` (`in_process` by default; `database`, `nats` or `redis`; the Compose stack sets `nats` and `./start.sh` rejects `redis`) | [Workflows architecture](../06-workflows/architecture.md) |
| Preview areas (off by default) | `CALIBER_FEATURE_ARIA`, `CALIBER_FEATURE_INTEGRATIONS`, `CALIBER_FEATURE_COOKBOOKS`, `CALIBER_FEATURE_RELEASE_CANDIDATES` | [Feature flags](../start/alpha-scope.md#feature-flags) |

## 1. Configure the core runtime first

Start with the basics that determine whether CALIBER can boot and persist
state:

- runtime ports
- metadata database
- object or workflow storage
- event backend

Do not start with optional tuning before these are correct.

## 2. Configure provider access deliberately

The current repository exposes configurable LLM provider and gateway settings.
For local development, the defaults are intentionally safer and smaller than a
production deployment.

Provider defaults are honest about what is simulated. `CALIBER_LLM_PROVIDER` and
`CALIBER_EVAL_PROVIDER` default to `fake`, so evaluation scores are canned and are marked as such.
`CALIBER_PROMOTER_PROVIDER` and `CALIBER_ARTIFACT_STORE_PROVIDER` default to `mlflow`; a `fake` one
is refused at startup unless `CALIBER_ALLOW_SIMULATED_PROVIDERS=true`, and so is a real LLM with the
`fake` evaluator. The bundled Compose stack sets the LLM to `fake` and the other three to `mlflow`.
`/readiness` lists whichever providers are simulated.

Use [Config and environment reference](../reference/config-and-environment.md)
when you need the representative variable names in one place.

Aria, MCP servers, OpenAPI integrations, cookbooks, and release-candidate signoff with Workspace releases are
**off by default**. Provider settings alone do not bring them back: Aria needs `CALIBER_FEATURE_ARIA=true`
before an engine matters, and an MCP server needs `CALIBER_FEATURE_INTEGRATIONS=true`. The four variables are
read at startup, so restart CALIBER after changing one. See
[Feature flags](../start/alpha-scope.md#feature-flags) and the
[Config and environment reference](../reference/config-and-environment.md#8-feature-flags).

## 3. Treat secret values as sources, not literals

The configuration surface includes `*_SOURCE` fields for secret-backed values.
That matters because CALIBER resolves secret sources rather than encouraging raw
credential literals throughout deployment configuration.

## 4. Verify after configuration

After any meaningful configuration change, verify:

- the service still boots
- readiness is healthy
- the storage and provider dependencies are reachable

Use:

- [Health and readiness](../operate/health-and-readiness.md)
- [Operator troubleshooting](../operate/troubleshooting.md)

## 5. Related docs

- [Config and environment reference](../reference/config-and-environment.md)
- [Storage and state](../operate/storage-and-state.md)
- [Health and readiness](../operate/health-and-readiness.md)
- [Local bring-up](../operate/local-bring-up.md)
