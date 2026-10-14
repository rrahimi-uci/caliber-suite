---
audience:
  - developer
  - operator
  - decision-maker
doc_type: reference
product_area: strategy
stability: beta
summary: Release notes for CALIBER 0.1.0a1, the alpha prerelease tagged caliber-v0.1.0a1 - what is in it, how to install it, what was verified and how, and what is known not to work.
prerequisites:
  - Read the Alpha scope and limitations page, which is the authority on what the alpha claims
tags:
  - release-notes
  - alpha
---

# CALIBER 0.1.0a1 (alpha)

Tag `caliber-v0.1.0a1`, a **prerelease**. It is for people who are willing to install it, use it and report
what breaks. It is not a pilot or production release, makes no stability promise, and is not on PyPI.

Read [Alpha scope and limitations](../start/alpha-scope.md) before you rely on it. It is short, and it is the
authority on what the alpha does and does not do. [Alpha install](../start/alpha-install.md) is the one install
path that is tested end to end.

## What is attached

Four wheels and their source distributions:

| Package | What it is |
| --- | --- |
| `caliber_suite-0.1.0a1` | the server and the web app (the UI is bundled in the wheel) |
| `caliber_sdk-0.1.0a1` | the Python SDK |
| `caliber_cli-0.1.0a1` | `caliberctl`, which requires `caliber-sdk>=0.1.0a1,<0.2` |
| `caliber_plugin_sdk-0.1.0a1` | the plugin SDK |

Install from the downloaded files (`pip install --find-links ./wheels caliber-sdk caliber-cli caliber-suite`).
`pip install caliber-sdk` finds nothing: the names are not reserved on PyPI.

## What it does

A small team can self-host CALIBER and, through the web app or the Python SDK, author a prompt, build a test
set, calibrate the prompt to get a candidate with evaluation evidence, review it, apply it to the live MLflow
alias, see what is live and what put it there, and roll it back, with an audit trail. Skills, tools,
workflows and knowledge bases are in the alpha too, each with its own journey. Projects, roles and access
tokens decide who may do what, and a person outside a project cannot see its resources.

With the default configuration the model and the evaluator are simulated. Calibration still runs and returns a
candidate, but the scores are canned and every screen, audit row and release record says so.

## What was verified

All of this ran in CI on the commit that was tagged, or is a script anyone can run:

- **The browser journeys**, offline, on the simulated defaults: a prompt (create, calibrate, review, apply,
  roll back), a skill (the same loop), a tool (register, run, calibrate, baseline, use in a workflow), and the
  workflow studio (template to run, human approval, wait for event, wait until). They are the `Alpha journey
  (browser)` check, which runs on every pull request.
- **The SDK journeys** for a prompt and a skill, against a real server and the real MLflow registry, in the
  `Backend core journey` check.
- **The install**, from the built wheels with no checkout on the import path: migrate, start MLflow and
  CALIBER, log in, create a project and a prompt, and read the prompt back from the registry
  (`scripts/verify-alpha-install.sh`, the `Alpha install` check).
- **The wheel**: it contains the UI and the migrations, installs alone, migrates, boots and serves the UI
  (`scripts/verify-wheel-boot.sh`, in the `Wheel build` check).
- **The docker stack**, weekly and on changes to `deploy/`: both application images are built, the stack is
  started, and a login, a project, a prompt, the registry and the UI are exercised
  (`scripts/verify-compose-stack.sh`).
- **Project isolation and roles**: every mutating route of prompts, jobs, skills, tools, workflows and
  knowledge bases is exercised as an outsider, a viewer, a reviewer, an editor and a non-member admin.
- **Knowledge bases** create, build, version, query and roll back against a real MinIO and PostgreSQL, with
  simulated embeddings (`scripts/verify-knowledge-stack.sh`).

## What was not verified

- Any real LLM or real evaluator. No CI job calls a live model.
- A knowledge base built with a real embedding model. The default docker image cannot build one: it installs
  the `knowledge` extra but not `knowledge-local`.
- Backups and restores, high availability, performance, SSO or MFA.
- Upgrade from any earlier tag. There is no upgrade guarantee.

## Known limitations

They are listed on [Alpha scope and limitations](../start/alpha-scope.md). The ones that most often surprise
people: traces and MLflow data are visible to every signed-in user; Apply has no second approver; CSRF
protection is off by default; a killed worker keeps `/readiness` red until its row is deleted; and
`/gate-verdicts` is not project-scoped.

What was found and deliberately left for later is in the P2 backlog of the
[alpha gap analysis](../caliber-alpha-gaps.md).

## Upgrading the database

The server never migrates on its own. Run `caliber-migrate` with the same `CALIBER_DATABASE_URL` before you
start it, and `/readiness` reports a `schema` blocker if the database is behind this release.
