---
audience:
  - developer
doc_type: how-to
product_area: sdk
stability: ga
prerequisites:
  - A planned CALIBER integration
reviewed_on: 2026-08-10
version_applicability: current main branch docs contract
tags:
  - sdk
  - api
  - integration
---

# SDK vs REST API

This page answers the first developer decision: should you integrate with
CALIBER through `caliber-sdk` or through the raw HTTP API?

## At a glance

| If you need... | Prefer |
| --- | --- |
| Python integration with typed models, retries, and waiters | [Python SDK](../sdk/guide.md) |
| non-Python integration | [REST API](../api/overview.md) |
| raw request/response debugging | [REST API](../api/reference.md) |
| the fastest path to working Python automation | [Python SDK](../sdk/guide.md) |
| OpenAPI-driven client generation | [REST API](../api/overview.md) |

## 1. Prefer the SDK when you are writing Python

The SDK already packages the common concerns a Python developer would otherwise
need to rebuild:

- typed client entry points
- auth and project-scoping helpers
- retries and waiters
- executable examples

Start from:

- [SDK guide](../sdk/guide.md)
- [SDK API reference](../sdk/reference.md)

## 2. Prefer the REST API when you need protocol-level control

Use the REST API directly when:

- you are integrating from another language
- you need the raw HTTP envelope
- you want OpenAPI-driven tooling
- you are comparing server behavior against SDK behavior

Start from:

- [REST API overview](../api/overview.md)
- [Authentication and conventions](../api/auth.md)
- [HTTP reference](../api/reference.md)

## 3. Mixed mode is valid

It is reasonable to use both:

- SDK for the common Python workflow
- REST API reference for exact route behavior, payload details, or newer surfaces

## 4. Questions that should push you toward the SDK

- Am I writing Python?
- Do I want a typed interface?
- Do I want waiters and higher-level helpers?
- Do I want examples that are already tied to the tested SDK example set?

If the answer is yes, start with the SDK.

## 5. Questions that should push you toward the REST API

- Am I working outside Python?
- Do I need exact HTTP shapes and headers?
- Do I need to compare wire behavior with what the SDK abstracts?

If the answer is yes, start with the REST API.
