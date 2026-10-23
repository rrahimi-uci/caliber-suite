# Contributing

Thanks for your interest in contributing to **caliber-suite**.

This page is the short entry point. The detailed engineering guide (coding standards, the CI-parity
script, what makes a good PR) is [caliber/CONTRIBUTING.md](caliber/CONTRIBUTING.md).

## Dev setup

You need Python 3.10, 3.11 or 3.12 (3.11 is what CI runs), Node.js with npm, and Docker for the full
suite. `make check` verifies the first two.

| Goal | Command |
| --- | --- |
| Install the Python package and build the web app (one time) | `make setup` (creates `.venv/`, installs `caliber[dev,s3,postgres,ingest,ocr,knowledge]` and builds the UI) |
| Run the whole suite in containers (MinIO, PostgreSQL, MLflow, gateway, CALIBER) | `make start`, then open `http://localhost:5001/caliber/` and sign in as `admin` / `admin`; stop with `make stop` (or use [start.sh](start.sh) / [stop.sh](stop.sh)) |
| Edit CALIBER with a short inner loop | the standalone path in [Local bring-up](docs/operate/local-bring-up.md#2-standalone-development-path): MLflow in Compose, then `uvicorn caliber.server:create_app --factory --reload --port 5001` |
| Edit the web app with hot reload | `cd caliber/caliber-ui && npm install && npm run dev` (proxies API calls to `:5001`) |

CALIBER runs as a standalone ASGI service beside a separate MLflow server. The embedded
`mlflow server --app-name caliber` topology is not supported, so do not use `make dev` or
`caliber/scripts/run-dev.sh` for new work: they start that internal compatibility launcher.

Note that the root `make setup` creates `.venv/` at the repository root, while `cd caliber && make install`
creates `caliber/.venv/`, which several scripts under `scripts/` and the SDK recipes expect. Use the
second when you run those scripts.

## Before you open a PR

- Add or update tests for any behavior change.
- Run the checks for the areas you changed. `make test` runs the backend and web-app unit tests,
  `make lint` runs ruff, mypy and the TypeScript type check, `make sdk`, `make plugin-sdk` and `make cli`
  check the three Python packages under `sdk/`, and `make test-all` (or [test-all.sh](test-all.sh)) adds the
  Playwright end-to-end suite. `scripts/ci-local.sh` mirrors every executable job in
  [.github/workflows/ci.yml](.github/workflows/ci.yml).
- Run `git diff --check`.
- Keep [ARCHITECTURE.md](ARCHITECTURE.md) and `docs/` in sync with code changes. The files under
  `docs-site/` (except `docs-site/cookbooks/`) and `caliber/caliber-ui/public/docs/` are generated from
  `docs/`; the CI `docs-validation` job fails when they drift, so regenerate them as
  [CI/CD automation](docs/build/ci-cd-automation.md) describes.
- Use clear, descriptive commit messages (Conventional Commits prefixes such as `feat:`, `fix:`, `docs:`).
- Fill in the [pull request template](.github/PULL_REQUEST_TEMPLATE.md): what changed and why, the
  validation commands you ran and their results, and the risks.

## Releasing

Cutting a release is manual and written down in [Releasing](docs/operate/releasing.md): the version and tag naming, what must be green first, how the wheels are built and attached, and how to verify the result.

## Reporting bugs / requesting features

Use the issue templates under [.github/ISSUE_TEMPLATE](.github/ISSUE_TEMPLATE). For security issues, follow [SECURITY.md](SECURITY.md) instead of opening a public issue.

By contributing you agree your contributions are licensed under the project's [Apache License 2.0](LICENSE).
