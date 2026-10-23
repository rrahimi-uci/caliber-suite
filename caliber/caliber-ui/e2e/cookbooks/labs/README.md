# Cookbook lab specs

One spec per lab, named `NN-<slug>.spec.ts`, matching the lab's folder under
[`docs-site/cookbooks/`](../../../../../docs-site/cookbooks/README.md). Lab 01 also has the extra specs `01b` (evaluation
regression) and `01c` (the Aria appendix); lab 17, the SDK financial-analysis lab, has a spec of its own.
Each spec performs the lab README's "Run it through the UI" steps against a running server. Keep specs
independent: create uniquely named assets (use `uiOnlySlug` from `../ui-only-driver`) and clean up what you
create.

## Run them

The specs need a server with the labs' feature flags on, and they start none themselves
(`playwright.labs.config.ts` requires `CALIBER_LAB_BASE_URL`). From the repository root, with
`caliber/.venv` created by `make install`:

```bash
scripts/cookbook-env.sh up labs --flags all     # disposable stack, every flag on; builds the web app if needed
source .tmp/cookbook-env/labs/env.sh            # exports CALIBER_URL and friends
cd caliber/caliber-ui
CALIBER_LAB_BASE_URL="$CALIBER_URL/caliber" npx playwright test --config=playwright.labs.config.ts
cd ../..
scripts/cookbook-env.sh down labs               # when you are done
```

Add `--real-llm` to `up` only if you want real model calls, which cost money. The simulated language model
and evaluator are the default. Run `scripts/cookbook-env.sh` with no arguments for every option.
