<!--
Thanks for your contribution! Please complete this checklist before requesting review.
Delete sections that don't apply.
-->

## What this PR does

<!-- 1-3 sentences. Use Conventional Commits style if helpful. -->

## Why

<!-- The user-facing reason. Link to the issue this closes. -->

Closes #

## How it was tested

<!-- Briefly describe how you verified the change. -->

- [ ] Unit tests added / updated
- [ ] Integration tests added / updated (if behavior crosses the MLflow boundary)
- [ ] Manual verification: <!-- describe -->

## Spec alignment

<!--
If this PR changes behavior described in caliber-suite/ docs, link the relevant
sections and confirm the docs are updated in this PR (or note the follow-up PR).
-->

- docs/architecture/backend.md §
- docs/architecture/frontend.md §
- docs/demo/demo-story.md §

## Checklist

- [ ] My branch is rebased on the latest `main`
- [ ] `pytest`, `ruff check`, `ruff format --check`, `mypy src` all pass locally
- [ ] New code has type annotations (mypy strict-mode clean)
- [ ] Public functions/classes have docstrings
- [ ] Conventional Commits-style title (e.g. `feat: ...`, `fix: ...`, `docs: ...`)
- [ ] CHANGELOG.md updated under `[Unreleased]` if user-facing
- [ ] No commented-out code or stray `print()` statements
- [ ] No secrets, keys, or PII in code, comments, fixtures, or trace examples
