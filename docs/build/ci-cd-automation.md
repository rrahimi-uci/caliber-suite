---
audience:
  - developer
  - operator
doc_type: how-to
product_area: sdk
stability: ga
prerequisites:
  - A repository checkout or CALIBER integration pipeline
reviewed_on: 2026-08-10
version_applicability: current main branch docs contract
tags:
  - ci
  - cd
  - automation
  - docs
---

# CI/CD automation

Use this page when you need the practical automation path for CALIBER-related
development: local quality gates, docs generation, SDK examples, and the points
where human review is still deliberate rather than accidental.

## At a glance

| Task | Current repo path |
| --- | --- |
| backend and UI quality gate | `make test`, `make test-all` |
| focused docs rebuild | `node docs-site/build-docs.mjs` |
| sync served docs copies | `node caliber/caliber-ui/scripts/sync-docs.mjs` |
| focused docs validation | `caliber/.venv/bin/python -m pytest caliber/tests/test_docs_generation_contract.py caliber/tests/test_sdk_docs_contract.py caliber/tests/test_ci_published_site_gate_contract.py --no-cov` |

## 1. Keep docs and code in the same automation loop

The current repository already treats documentation as a tested build artifact.
Do not separate docs generation from normal development validation.

## 2. Recommended local sequence

For docs and integration changes in this repository:

1. rebuild the docs
2. sync the served copies
3. run focused docs contracts
4. run the broader quality gate that matches the change surface

## 3. Preserve human review where the product expects it

CI/CD should automate build, verification, and publication. It should not
silently bypass review, approval, or release boundaries that CALIBER exposes as
intentional product controls.

## 4. Related docs

- [Developer troubleshooting](../build/developer-troubleshooting.md)
- [Error handling and retries](../build/error-handling-and-retries.md)
- [SDK recipes](../sdk/cookbooks.md)
- [Operations runbook](../runbook.md)
