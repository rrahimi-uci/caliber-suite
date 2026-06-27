# Contributing to CALIBER

Thanks for your interest in contributing. CALIBER is an early-stage open-source project — there is room to shape it.

## Quick start for contributors

CALIBER is currently supported and CI-validated on **Python 3.10-3.12**. Use **Python 3.11** unless you have a reason to target another version in that range.

```bash
git clone https://github.com/<your-org>/caliber-mlflow
cd caliber

# Set up a virtualenv with dev dependencies.
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"

# Install pre-commit hooks (runs lint + type-check on every commit).
pre-commit install

# Verify your environment.
./scripts/check-runtime-advisories.sh && pytest && ruff check . && mypy src
```

If all four commands pass, you're ready to make changes.

Concurrent targeted `pytest` runs are safe by default: the repo preloads
`caliber._pytest_cov_plugin`, which assigns each pytest process its own
coverage DB under `.pytest_cache/coverage/` so parallel `pytest-cov` commands
do not fight over one shared `.coverage` SQLite file. Set
`CALIBER_PYTEST_UNIQUE_COVERAGE=0` only if you explicitly need the legacy
shared-file behavior.

## How we work

| Practice | What it means in this repo |
| --- | --- |
| **Trunk-based development** | All work lands on `main` via pull request. No long-lived feature branches. |
| **Conventional Commits** | `feat:`, `fix:`, `docs:`, `refactor:`, `test:`, `chore:` prefixes. Auto-generates the changelog. |
| **SemVer** | `MAJOR.MINOR.PATCH`. Pre-1.0 minor bumps may include breaking changes (documented in CHANGELOG). |
| **Squash merge** | Each PR is one commit on `main`. Keeps history readable. |
| **CI must pass** | Lint, type-check, tests, and security scan must all be green before merge. No `--no-verify`. |
| **Eval-driven changes** | Changes to optimizers or scorers ship with eval data showing the change is an improvement, not just a refactor. |

## What makes a good PR

- **One concern per PR.** A feature, a bug fix, or a refactor — not all three.
- **Tests.** New code paths get tests. Bug fixes get a regression test that fails before and passes after.
- **Type hints.** Every public function in `src/caliber/` has full type annotations. `mypy --strict` must pass.
- **Docstrings on public surfaces.** Modules, classes, and public functions. Keep them short and load-bearing — explain *why*, not *what*.
- **Spec alignment.** Changes that affect behavior described in `caliber-suite/` design docs are accompanied by spec updates in the same PR (or a follow-up PR explicitly cross-linked).
- **No commented-out code.** Use git history if you need to recover something.
- **Self-review the diff before requesting review.** It catches half the comments before they happen.

## Running integration tests

The default `pytest` run executes the unit suite only — fast, hermetic, no MLflow tracking store required. A second suite (in `tests/test_integration_mlflow.py`) stands the real MLflow integrations up end-to-end against a fresh SQLite-backed tracking store under `tmp_path`. Run it with:

```bash
CALIBER_INTEGRATION_TESTS=1 pytest -m integration --no-cov
```

The opt-in env var is required — without it the integration tests skip at collection time so a contributor without MLflow installed can still run the suite. CI runs the integration job in a separate workflow step (see [`.github/workflows/test.yml`](.github/workflows/test.yml)).

Integration tests cover:

- `MLflowArtifactStore` — register prompt, read it back via the `@prod` alias.
- `MLflowPromoter` — register a new version, rotate the alias, verify the new content is now active.
- `MLflowEvalProvider` — run `mlflow.genai.evaluate` with a small synthetic dataset and a deterministic scorer.

## What to work on

- Browse the project's open issues, especially ones tagged `good-first-issue`.
- Read [`caliber-suite/docs/roadmap/development-plan.md`](../docs/roadmap/development-plan.md) for the phase roadmap. Each phase lists concrete deliverables.
- Read [`caliber-suite/docs/demo/demo-story.md`](../docs/demo/demo-story.md) for the user-visible behavior we're building toward.

If you want to propose something larger than a bug fix, open an issue first — labeled `proposal` — so we can align on direction before code is written.

## Code style

We use `ruff` for both linting and formatting. `pre-commit` runs it automatically; CI enforces it.

- **Line length 100.** Not 88, not 120.
- **Double quotes** for strings (consistent with `ruff format`).
- **Imports sorted** by `ruff`'s isort rule.
- **No `print()` in production code.** Use `logging`. (Tests are fine.)
- **No magic numbers** in production code. Name them as constants.

## Reporting bugs

Open an issue using the `bug` template. Include:

- CALIBER version (`pip show caliber`)
- MLflow version (`mlflow --version`)
- Python version
- Minimal reproduction steps
- Expected vs. actual behavior

If the bug is security-related, **do not** open a public issue — see [SECURITY.md](SECURITY.md).

## Code of conduct

This project follows the [Contributor Covenant](CODE_OF_CONDUCT.md). By participating, you agree to uphold it.
