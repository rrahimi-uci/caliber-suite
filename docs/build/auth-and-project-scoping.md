---
audience:
  - developer
doc_type: how-to
product_area: api
stability: ga
prerequisites:
  - A running CALIBER deployment
  - A project to access
reviewed_on: 2026-08-10
version_applicability: current main branch docs contract
tags:
  - auth
  - project
  - sdk
  - api
---

# Auth and project scoping

This guide explains the minimum authentication and scoping decisions a developer
must make before calling CALIBER programmatically.

## At a glance

| Topic | Current contract |
| --- | --- |
| Base management prefix | `/ajax-api/2.0/mlflow/caliber` |
| Automation auth | `Authorization: Bearer <token>` |
| Project scoping | `X-CALIBER-Project: <project_id>` |
| Browser-style writes | `X-CALIBER-CSRF: <token>` when the deployment uses session/CSRF flows |
| Best Python entry point | [`caliber-sdk`](../sdk/guide.md) |
| Best HTTP entry point | [REST API overview](../api/overview.md) |

## 1. Choose the auth model

For automation and SDK usage, prefer Bearer-token authentication.

Use the browser/session path only when you are intentionally emulating the UI
contract.

For the full auth conventions and envelope details, use:

- [REST API overview](../api/overview.md)
- [Authentication and conventions](../api/auth.md)

## 2. Always decide project scope explicitly

Most meaningful calls need a project context. In HTTP, that means setting
`X-CALIBER-Project`. In the SDK, that means constructing or selecting a client
scope that sends that header consistently.

If a workflow works in one project but not another, project scoping is one of
the first things to verify.

## 3. SDK path

If you are writing Python, use the SDK unless you specifically need the raw
wire layer.

Recommended path:

1. [SDK guide](../sdk/guide.md)
2. [SDK API reference](../sdk/reference.md)
3. [CLI and async client](../sdk/cli.md) for operator-style automation

## 4. Raw HTTP path

Use the REST API directly when:

- you are integrating from another language
- you need OpenAPI-driven tooling
- you are debugging request/response behavior

Start from:

1. [REST API overview](../api/overview.md)
2. [Authentication and conventions](../api/auth.md)
3. [Resource catalog](../api/resources.md)
4. [HTTP reference](../api/reference.md)

## 5. Common failure modes

| Symptom | First thing to check |
| --- | --- |
| Request is authenticated but sees the wrong objects | `X-CALIBER-Project` or SDK project scope |
| Write request fails in a browser-like flow | missing or stale CSRF token |
| HTTP integration works but Python code does not | SDK client config, auth setup, or server compatibility |
| Python code works but raw HTTP does not | missing auth header, wrong base path, or missing project header |

## 6. Related docs

- [SDK guide](../sdk/guide.md)
- [REST API overview](../api/overview.md)
- [Authentication and conventions](../api/auth.md)
- [SDK API reference](../sdk/reference.md)
