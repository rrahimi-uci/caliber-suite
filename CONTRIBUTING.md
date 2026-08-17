# Contributing

Thanks for your interest in contributing to **caliber-suite**.

## Dev setup

See [README.md](README.md) and the [Makefile](Makefile) targets for environment setup: `make setup` (one-time: create venv, install deps, build UI), `make start` (start the suite), `make stop` (stop the suite), `make dev` (hot-reload mode). Alternatively use [start.sh](start.sh) / [stop.sh](stop.sh).

## Before you open a PR

- Add or update tests for any behavior change.
- Run `make test-all` (or [test-all.sh](test-all.sh)) before submitting.
- Keep [ARCHITECTURE.md](ARCHITECTURE.md) and `docs/` in sync with code changes.
- Use clear, descriptive commit messages.

## Reporting bugs / requesting features

Use the issue templates under [.github/ISSUE_TEMPLATE](.github/ISSUE_TEMPLATE). For security issues, follow [SECURITY.md](SECURITY.md) instead of opening a public issue.

By contributing you agree your contributions are licensed under the project's [MIT License](LICENSE).
