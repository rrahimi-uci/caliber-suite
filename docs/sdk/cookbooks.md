---
audience:
  - developer
  - operator
doc_type: example
product_area: sdk
stability: ga
prerequisites:
  - A running CALIBER deployment
reviewed_on: 2026-08-10
version_applicability: current main branch docs contract
tags:
  - sdk
  - cookbook
  - examples
  - workflows
---

# SDK recipes

These recipes are the SDK-native counterparts to the platform cookbook gallery.
Each example uses only `caliber-sdk` plus Python's standard library.

Design rule:

- use the built-in cookbook installer to materialize the versioned platform
  recipe that CALIBER already ships;
- then use typed SDK resources — never `client.raw` — to finish
  configuration, execution, and evidence capture. `client.raw` remains the
  SDK's permanent escape hatch for a route the typed layer has not wrapped
  yet (see the [SDK guide](guide.md#anything-not-yet-modelled)), but none of
  the sixteen recipes below currently need it: a prior pass through this
  gallery found three that reached for it out of habit rather than
  necessity, and each had an existing typed method all along.

Every code block on this page is generated from the source files under
`sdk/caliber-sdk/examples/cookbooks/` at build time. The example test suite
executes those files so the published docs stay tied to runnable SDK code.

For setup and typed client behavior, use the [SDK guide](guide.md) and the
[SDK API reference](reference.md). This page is only for end-to-end runnable
examples.

## Cookbook implementations

{{SDK_COOKBOOKS}}
