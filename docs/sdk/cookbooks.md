# SDK recipes

These recipes are the SDK-native counterparts to the platform cookbook gallery.
Each example uses only `caliber-sdk` plus Python's standard library.

Design rule:

- use the built-in cookbook installer to materialize the versioned platform
  recipe that CALIBER already ships;
- then use typed SDK resources — and `client.raw` only where the typed layer
  does not yet wrap the live route — to finish configuration, execution, and
  evidence capture.

Every code block on this page is generated from the source files under
`sdk/caliber-sdk/examples/cookbooks/` at build time. The example test suite
executes those files so the published docs stay tied to runnable SDK code.

For setup and typed client behavior, use the [SDK guide](guide.md) and the
[SDK API reference](reference.md). This page is only for end-to-end runnable
examples.

## Cookbook implementations

{{SDK_COOKBOOKS}}
