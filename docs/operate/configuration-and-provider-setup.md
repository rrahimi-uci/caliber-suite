---
audience:
  - operator
  - developer
doc_type: how-to
product_area: operations
stability: ga
prerequisites:
  - A CALIBER deployment plan or checkout
reviewed_on: 2026-08-10
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
| App ports and local bring-up | `CALIBER_PORT`, `MLFLOW_PORT`, `MLFLOW_GATEWAY_PORT` | [Local bring-up](../operate/local-bring-up.md) |
| Metadata database | `CALIBER_DATABASE_URL` | [Platform](../01-caliber/architecture.md) |
| LLM provider and gateway | `CALIBER_LLM_PROVIDER`, `CALIBER_LLM_BASE_URL` | [Gateways](../10-gateways/architecture.md) |
| Object store | `CALIBER_OBJECT_STORE_*` | [Object store](../07-object-store/architecture.md) |
| Workflow file storage | `CALIBER_WORKFLOW_STORAGE_*` | [Storage and state](../operate/storage-and-state.md) |
| Event backend | `CALIBER_WORKFLOW_RUN_EVENT_BACKEND` | [Workflows architecture](../06-workflows/architecture.md) |

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

Use [Config and environment reference](../reference/config-and-environment.md)
when you need the representative variable names in one place.

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
