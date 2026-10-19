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
> `CALIBER_FEATURE_COOKBOOKS=true` on the server and restart it. Recipes 03 to 08, 10 and 18 install their catalog
> entry with `caliber.cookbooks.install` (recipe 01 lists it), so against a default server they fail with a 404 whose
> `reason_code` is `feature_disabled`. Some recipes use other preview areas as well: recipe 05 needs
> `CALIBER_FEATURE_INTEGRATIONS` (MCP servers), recipe 11 needs `CALIBER_FEATURE_RELEASE_CANDIDATES`, and
> recipes 12 to 15 and 17 need `CALIBER_FEATURE_ARIA`. Read `features` from the capabilities response first
> (see the [SDK guide](guide.md#feature-flags-and-disabled-areas)). See also
> [Feature flags](../start/alpha-scope.md#feature-flags).

Each recipe is the SDK counterpart of a lab under `docs-site/cookbooks/`, which was run for real against a disposable
server (`scripts/cookbook-env.sh up NAME --flags ...`); the lab's README holds the exact setup, the expected `ok` lines, and
what is simulated. On the alpha's default simulated model and evaluator the scripts assert structure and wiring (for
example that **Evaluations** refuses with `400 evaluation requires a real LLM provider`), never a score. Each script
prints one `ok` line per verified outcome and exits 0 only if all of them held; run it with
`PYTHONPATH=sdk/caliber-sdk caliber/.venv/bin/python sdk/caliber-sdk/examples/cookbooks/cookbook_NN_*.py` from a checkout.

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
