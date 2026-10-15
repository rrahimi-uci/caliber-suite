---
audience:
  - developer
  - operator
doc_type: example
product_area: sdk
stability: ga
prerequisites:
  - A running CALIBER deployment
reviewed_on: 2026-10-15
version_applicability: current main branch docs contract
tags:
  - sdk
  - cookbook
  - examples
  - workflows
---

# SDK recipes

> **Disabled by default.** The cookbooks are a preview area and are off by default; enable with
> `CALIBER_FEATURE_COOKBOOKS=true` on the server and restart it. Recipes 01 to 16 each install their catalog
> entry with `caliber.cookbooks.install`, so against a default server they fail with a 404 whose `reason_code`
> is `feature_disabled`. Some recipes use other preview areas as well: recipe 05 needs
> `CALIBER_FEATURE_INTEGRATIONS` (MCP servers), recipe 11 needs `CALIBER_FEATURE_RELEASE_CANDIDATES`, and
> recipes 12 to 15 and 17 need `CALIBER_FEATURE_ARIA`. Read `features` from the capabilities response first
> (see the [SDK guide](guide.md#feature-flags-and-disabled-areas)). See also
> [Feature flags](../start/alpha-scope.md#feature-flags).

These recipes are the SDK-native counterparts to the platform cookbook gallery.
Each example uses only `caliber-sdk` plus Python's standard library.

Design rule:

- use the built-in cookbook installer when the example extends one of the
  versioned platform recipes CALIBER already ships;
- create standalone SDK automation examples directly through typed resources
  when there is no corresponding installable workflow recipe;
- use typed SDK resources — never `client.raw` — for configuration, execution,
  file persistence, and evidence capture. `client.raw` remains the SDK's
  permanent escape hatch for a route the typed layer has not wrapped yet (see
  the [SDK guide](guide.md#anything-not-yet-modelled)), but none of the recipes
  below currently need it.

Every code block on this page is generated from the source files under
`sdk/caliber-sdk/examples/cookbooks/` at build time. The example test suite
executes those files so the published docs stay tied to runnable SDK code.

For setup and typed client behavior, use the [SDK guide](guide.md) and the
[SDK API reference](reference.md). This page is only for end-to-end runnable
examples.

## Cookbook implementations

{{SDK_COOKBOOKS}}
