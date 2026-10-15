---
audience:
  - developer
doc_type: how-to
product_area: sdk
stability: ga
prerequisites:
  - A CALIBER SDK or REST API integration issue
reviewed_on: 2026-10-14
version_applicability: current main branch docs contract
tags:
  - troubleshooting
  - sdk
  - api
  - workflows
---

# Developer troubleshooting

Use this page for first-pass developer diagnosis across SDK and REST usage:
auth, project scoping, CSRF, uploads, waiters, and workflow invocation.

## At a glance

| Symptom | First thing to check |
| --- | --- |
| SDK call authenticates but sees the wrong objects | project scope/header alignment |
| Browser write works but automation fails | auth model and CSRF expectations |
| Raw HTTP works but SDK code fails | client configuration or version mismatch |
| Upload or file-backed workflow calls fail | workflow storage and media constraints |
| Long-running operation never settles | use the documented waiter/polling path |
| Workflow invocation fails after compile | runtime inputs, downstream tools, or approvals |
| A write returns 403 with a project role in the message | the caller's project role; a `viewer` can read but not write, publish, run or calibrate |
| A read or write returns 404 for an object you know exists | the caller is not a member of its project (a `caliber.admin` token is not an implicit member) |
| 403 saying the token is bound to a project | a project-bound personal access token is naming a different project |
| Every route fails with `no such table`, or `/readiness` is 503 on `schema` | the database was never migrated: run `caliber-migrate` |
| Prompt create or Apply returns 503 naming `MLFLOW_TRACKING_URI` | MLflow is not running or the URL is wrong; nothing was changed |
| `pip install caliber-sdk` finds nothing | the packages are not on PyPI yet; install from the release wheels with `pip install --find-links ./wheels caliber-sdk` |

## 1. Auth and scope issues are the most common root cause

Before debugging business logic, confirm:

- base URL
- bearer token or session model
- project scope

Use [Auth and project scoping](../build/auth-and-project-scoping.md) for the
canonical first-pass checklist.

## 2. Keep browser and automation contracts separate

The browser flow may involve session and CSRF behavior that your service
integration should not copy unless you are intentionally emulating the UI.

## 3. Use the right surface for the question

| If the question is... | Use this next |
| --- | --- |
| what route family exists? | [Resource catalog](../api/resources.md) |
| what exact method or model exists in Python? | [SDK API reference](../sdk/reference.md) |
| what runtime path does a workflow use? | [Workflows](../use/workflows.md) |
| why is the operator runtime degraded? | [Operator troubleshooting](../operate/troubleshooting.md) |

## 4. Related docs

- [Auth and project scoping](../build/auth-and-project-scoping.md)
- [Error handling and retries](../build/error-handling-and-retries.md)
- [SDK API reference](../sdk/reference.md)
- [Resource catalog](../api/resources.md)
