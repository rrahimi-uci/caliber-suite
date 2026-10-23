# Contributing to the CALIBER server package

Thanks for your interest in contributing. CALIBER is an alpha, open-source project, and there is room
to shape it. This page covers the Python package and web app in this directory. The repository-level
guide is [CONTRIBUTING.md](../CONTRIBUTING.md) (suite-wide setup, `make` targets, releasing); read both.

## Quick start for contributors

Package metadata supports **Python 3.10 to 3.12**, and you need Node.js 20.19 or later (or 22.12 or
later) for the web app. Use **Python 3.11**, which is the interpreter the full CI suite runs on. CI
also runs a smoke-test leg on 3.10 and on 3.12, not the full suite.

```bash
git clone https://github.com/rrahimi-uci/caliber-suite.git
cd caliber-suite/caliber

# Creates .venv and installs ".[dev,postgres,ingest,ocr,llm,knowledge]" (run `make install CALIBER_PYTHON=python3.11` to pick
# the interpreter). To match CI's extras exactly, run `make install-extended` instead.
make install
source .venv/bin/activate

# Optional: install the pre-commit hooks (hygiene checks, ruff, mypy, gitleaks).
pre-commit install

# A smoke check of the local environment. The pytest step is the full backend suite, so it takes a while.
./scripts/check-runtime-advisories.sh && pytest -n auto --dist loadgroup && ruff check . && mypy src
```

Those commands are a useful setup smoke test, but they are not the complete merge gate. From the
repository root, `./scripts/ci-local.sh` runs every executable CI job: lint, type check, the backend
smoke, core-journey and full suites, the PostgreSQL migration parity job, the Python 3.10 and 3.12
compatibility legs, the opt-in MLflow integration tests, docs validation, the web app, the cookbook and
alpha browser journeys, the Compose configuration, the wheel build, the install check, security, and the
SDK, plugin SDK and CLI jobs. `./scripts/ci-local.sh --fast` skips only the slow jobs (the full suite, the
compatibility legs, integration, the cookbook and alpha browser journeys, the wheel build and the install
check). A missing interpreter, Docker, or `gitleaks` is reported as **skipped**, never as a pass; review the
summary before you open a PR.

The `pre-commit` hooks pin their own ruff version, which can differ from CI's. CI's `ruff==0.17.1`
(installed by the `dev` extra) is authoritative, so run `ruff check . && ruff format --check .` before you push.

## Running the tests

| What | Command (from `caliber/`) |
| --- | --- |
| Full backend suite, as CI runs it | `pytest -n auto --dist loadgroup` |
| A targeted run | `pytest tests/test_x.py --no-cov` |
| Lint and format check | `ruff check . && ruff format --check .` |
| Type check | `mypy src` |
| Web app | in `caliber-ui/`: `npm run typecheck`, `npm run lint`, `npm run test:coverage`, `npm run build` |
| Browser tests | in `caliber-ui/`: `npm run test:e2e` (see [`caliber-ui/README.md`](caliber-ui/README.md)) |

The backend gate is **90% branch coverage** (`fail_under` in `pyproject.toml`). Coverage measures the whole
package, so a partial run fails the gate unless you pass `--no-cov`. The web app's unit-test thresholds are in
`caliber-ui/vitest.config.ts`.

Concurrent targeted `pytest` runs are safe by default: the repo preloads `caliber._pytest_cov_plugin`, which
assigns each pytest process its own coverage database under `.pytest_cache/coverage/`, so parallel
`pytest-cov` commands do not fight over one shared `.coverage` file. Set `CALIBER_PYTEST_UNIQUE_COVERAGE=0`
only if you explicitly need the legacy shared-file behavior.

## How we work

| Practice | What it means in this repo |
| --- | --- |
| **Trunk-based development** | All work lands on `main` through a pull request. No long-lived feature branches. |
| **Conventional Commits** | `feat:`, `fix:`, `docs:`, `refactor:`, `test:`, `chore:` prefixes, with an optional scope such as `fix(aria):`. |
| **A hand-written changelog** | User-visible changes get an entry in [CHANGELOG.md](CHANGELOG.md) under `[Unreleased]`, in the [Keep a Changelog](https://keepachangelog.com/en/1.1.0/) format. |
| **SemVer** | `MAJOR.MINOR.PATCH`, with pre-release suffixes during the alpha. Pre-1.0 minor bumps may include breaking changes (documented in the changelog). |
| **Reviewable history** | Keep each PR to one reviewable change; the maintainers merge it. |
| **CI must pass** | The parity script and the CI jobs (lint, type check, coverage tests, compatibility, integration, UI, Compose, package, security) must be green, or an explicit skip must be resolved. No `--no-verify`. |
| **Eval-driven changes** | Changes to optimizers or scorers ship with evaluation data showing the change is an improvement, not just a refactor. |

## What makes a good PR

- **One concern per PR.** A feature, a bug fix, or a refactor, not all three. Fill in the
  [pull request template](../.github/PULL_REQUEST_TEMPLATE.md), including the exact validation commands you ran.
- **Tests.** New code paths get tests. Bug fixes get a regression test that fails before and passes after.
- **Type hints.** Every function in `src/caliber/` has full type annotations. `mypy src` runs in strict mode and must pass.
- **Docstrings on public surfaces.** Modules, classes, and public functions. Keep them short and load-bearing: explain *why*, not *what*.
- **Docs in step with behavior.** A change to behavior described in [`docs/`](../docs) or [`ARCHITECTURE.md`](../ARCHITECTURE.md)
  updates those pages in the same PR. Edit the Markdown sources, never the generated `docs-site/m-*.html`; the CI
  docs-validation job regenerates the site and fails if the committed output differs. See the
  [documentation style guide](../docs/STYLE.md).
- **No commented-out code.** Use git history if you need to recover something.
- **Self-review the diff before requesting review.** It catches half the comments before they happen.

## Running integration tests

The default `pytest` run executes the hermetic suite: no MLflow server and no network. A second suite, in
`tests/test_integration_mlflow.py`, stands the real MLflow integrations up end to end against a fresh
SQLite-backed tracking store under `tmp_path`, so it needs the MLflow package but no server. Run it with:

```bash
CALIBER_INTEGRATION_TESTS=1 pytest -m integration --no-cov
```

The opt-in variable is required: without it the integration tests skip at collection time, so a contributor
without MLflow installed can still run the suite. CI runs them in the `integration` job of
[`ci.yml`](../.github/workflows/ci.yml) on pushes to `main`.

Integration tests cover:

- `MLflowArtifactStore`: read a prompt back through the `@prod` alias, and a missing prompt returns nothing.
- `MLflowPromoter`: register a new version, rotate the alias, and verify the new content is active.
- `MLflowEvalProvider`: run `mlflow.genai.evaluate` with a small synthetic dataset and a deterministic scorer.

## What to work on

- Browse the open issues, especially ones labeled `good first issue`.
- Read the [roadmap](../docs/roadmap.md) and [Alpha scope and known limitations](../docs/start/alpha-scope.md).
- Read the [guided walkthrough](../docs-site/walkthrough.html) for the user-visible end-to-end behavior.

If you want to propose something larger than a bug fix, open an issue first, using the feature request
template, so we can align on direction before code is written.

## Code style

We use `ruff` for both linting and formatting. `pre-commit` can run it for you; CI enforces it.

- **Line length 100.**
- **Double quotes** for strings (consistent with `ruff format`).
- **Imports sorted** by ruff's isort rule.
- **No `print()` in production code.** Use `logging`. (Tests are fine.)
- **No magic numbers** in production code. Name them as constants.

## Reporting bugs

Open an issue using the bug report template. Include:

- the CALIBER distribution version (`pip show caliber-suite`),
- the MLflow version (`mlflow --version`),
- the Python version,
- minimal reproduction steps,
- expected and actual behavior.

If the bug is security-related, **do not** open a public issue. See [SECURITY.md](SECURITY.md).

## Code of conduct

This project follows the [Contributor Covenant](CODE_OF_CONDUCT.md). By participating, you agree to uphold it.
