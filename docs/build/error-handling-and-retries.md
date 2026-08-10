---
audience:
  - developer
doc_type: how-to
product_area: sdk
stability: ga
prerequisites:
  - A CALIBER integration using the SDK or REST API
reviewed_on: 2026-08-10
version_applicability: current main branch docs contract
tags:
  - errors
  - retries
  - waiters
  - sdk
---

# Error handling and retries

Use this page when you need the practical integration policy for failures:
which problems are auth or scope issues, which are transient, and when to use
waiters instead of building your own polling loop.

## At a glance

| Concern | Recommended default | Related docs |
| --- | --- | --- |
| Authentication and project scope | validate them first | [Auth and project scoping](../build/auth-and-project-scoping.md) |
| API errors | prefer typed SDK errors where available | [SDK API reference](../sdk/reference.md) |
| Long-running operations | use the SDK waiter/polling path | [CLI and async client](../sdk/cli.md) |
| Raw HTTP diagnostics | capture request ids and envelopes | [Authentication and conventions](../api/auth.md) |

## 1. Classify the failure before retrying

Start by separating:

- auth or permission failures
- wrong-project or wrong-scope failures
- request-shape or validation failures
- transient transport or dependency failures

Retrying the first three classes usually only creates noise.

## 2. Use waiters for long-running operations

If the product already exposes a waiter or polling contract, use that rather
than inventing an uncontrolled sleep loop in your application.

That keeps retry behavior aligned with the documented SDK surface.

## 3. Retry safely

For automated retries:

- prefer idempotent reads first
- be careful with writes that can change external state
- capture request identifiers and final error context

## 4. Related docs

- [Auth and project scoping](../build/auth-and-project-scoping.md)
- [Developer troubleshooting](../build/developer-troubleshooting.md)
- [SDK API reference](../sdk/reference.md)
- [Authentication and conventions](../api/auth.md)
