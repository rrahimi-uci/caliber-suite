---
audience:
  - developer
doc_type: reference
product_area: sdk
stability: ga
prerequisites:
  - Python 3.10+
  - A CALIBER integration question
reviewed_on: 2026-08-10
version_applicability: current main branch docs contract
tags:
  - sdk
  - reference
  - api
  - models
---

# CALIBER Python SDK API reference

This reference is generated from the SDK source tree at build time. It follows
the same pattern as the MLflow Python API docs: start with the top-level
package, then drill into resource modules, models, errors, waiters, and the
async client.

This page is intentionally about the Python client, not the raw HTTP routes. If
you need headers, envelopes, or concrete endpoints, start with the
[REST API overview](../api/overview.md) and [HTTP reference](../api/reference.md).

Most developers should begin with:

- `caliber_sdk.CaliberClient` for the synchronous client
- `caliber_sdk.aio.AsyncCaliberClient` for async workflows
- [the SDK guide](guide.md) for setup and common flows
- [SDK recipes](cookbooks.md) for full runnable scenarios

The reference below is generated from the current SDK code, so the published
HTML stays aligned with the package the tests exercise.

## Reference

{{SDK_API_REFERENCE}}
