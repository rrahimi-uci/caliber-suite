# Cookbook lab specs

One spec per lab, named `NN-<slug>.spec.ts`. Each performs the lab README's "Run it through the UI"
steps against a running server (see `playwright.labs.config.ts`). Keep specs independent: create
uniquely named assets (use `uiOnlySlug` from `../ui-only-driver`) and clean up what you create.
