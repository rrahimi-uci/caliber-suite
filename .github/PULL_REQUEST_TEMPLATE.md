<!--
Thank you for contributing to CALIBER. Keep this PR focused and complete the
sections that apply. Remove HTML comments before requesting review.
-->

## Summary

<!-- What changed, and why? Keep this to 1–3 sentences. -->

## Related issue

Closes #

## Scope

- [ ] Backend / API / database
- [ ] Frontend / UI
- [ ] SDK (`sdk/caliber-sdk`)
- [ ] Plugin SDK (`sdk/caliber-plugin-sdk`)
- [ ] CLI (`sdk/caliber-cli`)
- [ ] Documentation or generated docs
- [ ] Deployment / Compose / CI / dependencies
- [ ] Optimizer, scorer, evaluation, or other behavior-sensitive change

## User-visible and operational impact

<!--
Describe changed interfaces, behavior, configuration, migrations, or rollout
requirements. If there is no user-visible impact, say so. Call out backwards
incompatibility, security/privacy implications, performance concerns, and
feature flags or environment variables.
-->

## Verification

<!-- List the exact commands run and summarize the result. Link CI runs when available. -->

- [ ] Focused tests for the changed behavior
- [ ] Regression test added for a bug fix, where applicable
- [ ] Backend gates (`cd caliber && make ci-local ARGS="--fast"` or equivalent)
- [ ] UI gates (`cd caliber/caliber-ui && npm run typecheck && npx eslint . && npm test && npm run build`), if applicable
- [ ] SDK / plugin SDK / CLI package tests and build, if applicable
- [ ] Integration tests, if MLflow, external services, or persistence boundaries changed
- [ ] Browser cookbook journeys, if user workflows or cookbook UI changed
- [ ] Compose validation, if deployment files or environment configuration changed
- [ ] Security checks, if dependencies, auth, secrets, data handling, or network behavior changed
- [ ] Not run — explain why: <!-- reason and follow-up -->

## Documentation and generated artifacts

- [ ] Documentation updated for changed behavior, configuration, API, or workflow
- [ ] Generated documentation or bundled UI artifacts are synchronized, if affected
- [ ] `CHANGELOG.md` updated under `[Unreleased]` for user-facing changes
- [ ] No documentation or generated-artifact change is needed

## Data, migrations, and release safety

- [ ] Database migration included and tested, if schema/data behavior changed
- [ ] Fixtures, seed data, or test data updated, if needed
- [ ] No secrets, credentials, tokens, PII, or sensitive traces were added
- [ ] Rollback or compatibility considerations documented, if applicable
- [ ] No data, migration, or release-safety impact

## Evaluation evidence

<!-- Required for optimizer/scorer/evaluation changes; otherwise write “Not applicable.” -->

Not applicable.

<!-- Include baseline vs. changed results, dataset/config, slices, and known limitations. -->

## Reviewer notes

<!-- Highlight risky areas, design decisions, deferred follow-ups, or places where focused review is useful. -->

## Author checklist

- [ ] The PR is focused on one concern and the description reflects the current diff.
- [ ] Public Python APIs have type annotations and load-bearing docstrings.
- [ ] I self-reviewed the diff and removed debug code, commented-out code, and unrelated changes.
- [ ] The PR title uses a Conventional Commits prefix (`feat:`, `fix:`, `docs:`, `refactor:`, `test:`, or `chore:`).
- [ ] Required CI checks pass, or every skipped/unrun check is explained above.
