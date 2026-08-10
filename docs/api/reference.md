---
audience:
  - developer
doc_type: reference
product_area: api
stability: ga
prerequisites:
  - A CALIBER API integration question
reviewed_on: 2026-08-10
version_applicability: current main branch docs contract
tags:
  - api
  - reference
  - routes
  - openapi
---

# REST API HTTP reference

This is the wire-level reference for the CALIBER management API. It is not the
Python SDK reference: it names the HTTP routes, their common operations, and
where to fetch the machine-readable contract.

## Management API root

All management routes are mounted under:

```text
/ajax-api/2.0/mlflow/caliber
```

The served management OpenAPI document lives at:

```text
GET /ajax-api/2.0/mlflow/caliber/openapi.json
```

## Core discovery routes

| Route | Why you call it |
| --- | --- |
| `GET /health` | Cheap liveness probe |
| `GET /readiness` | Dependency/readiness posture |
| `GET /capabilities` | Surface discovery and stability tiers |
| `GET /openapi.json` | Full management API contract |
| `GET /settings/runtime` | Runtime configuration summary |
| `GET /me` | Caller identity and effective session information |

## Representative resource families

| Family | Representative routes | Notes |
| --- | --- | --- |
| Prompts | `GET/POST /prompts`, `GET/PATCH /prompts/{id}`, alias and release paths | Authoring and release are deliberately separate |
| Skills | `GET/POST /skills`, `POST /skills/{id}/test-render`, `POST /skills/{id}/calibrate` | Packaging, tests, baseline, and calibration live here |
| Tools | `GET/POST /tools`, test/calibration routes | Deterministic test and calibration flows |
| Workflows | `GET/POST /workflows`, `GET/POST /workflows/{id}/versions`, `POST /workflow-versions/{version_id}/run` | Registry, version lifecycle, execution, and deployment are split across route families |
| Workflow services | `POST /workflows/{workflow_id}/service`, `POST /services/{workflow_id}/invoke`, `GET /services/{workflow_id}/openapi.json` | Publish a workflow externally and inspect its invocation contract |
| Datasets and evaluations | `GET/POST /eval-datasets`, `GET/POST /evaluations`, `GET/POST /judges` | Evaluation evidence and scoring |
| Knowledge | `GET/POST /knowledge-bases`, `POST /knowledge/query` | Corpus lifecycle plus retrieval |
| MCP | `GET/POST /mcp-servers`, tool inventory and invoke routes | Governed external tool connectivity |
| Releases | `GET/POST /releases/candidates`, `POST /releases/candidates/{id}/signoffs`, `GET /releases/operations` | Signoff, waivers, and reconcile workflows |
| Observability | `GET /observability/traces`, `GET /metrics`, `GET /events/stream` | Runtime evidence and live telemetry |

## Workflow-service OpenAPI

Published workflow services have a second OpenAPI surface:

```text
GET /ajax-api/2.0/mlflow/caliber/services/{workflow_id}/openapi.json
```

That document is about the externally invocable service for one workflow, not
the CALIBER management API itself.

{{REST_API_ROUTE_INVENTORY}}

## Practical integration order

1. Call `GET /capabilities` to learn what the deployment exposes.
2. Fetch `GET /openapi.json` if you are generating or inspecting the raw HTTP
   contract.
3. Use Bearer auth plus `X-CALIBER-Project` when you need scoped automation.
4. Switch to the [SDK guide](../sdk/guide.md) if you want typed models,
   retries, error classes, waiters, and CSRF handling instead of raw HTTP.
