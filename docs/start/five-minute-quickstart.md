---
audience:
  - developer
  - system-user
  - operator
doc_type: tutorial
product_area: docs
stability: ga
prerequisites:
  - Either a local repository checkout or a reachable CALIBER deployment
reviewed_on: 2026-08-10
version_applicability: current main branch docs contract
tags:
  - quickstart
  - onboarding
  - setup
---

# 5-minute quickstart

Use this page when you want the shortest path to a real CALIBER success before
you decide which deeper guide you need.

## At a glance

| Goal | Start here | Then go to |
| --- | --- | --- |
| Run CALIBER locally | `make setup`, `make start` | [Local bring-up](../operate/local-bring-up.md) |
| Verify a shared deployment | open the UI and check readiness | [Choose your CALIBER path](../start/choose-your-path.md) |
| Make the first Python call | [SDK guide](../sdk/guide.md) | [Auth and project scoping](../build/auth-and-project-scoping.md) |
| Tour the product before integrating | [Guided walkthrough](../../docs-site/walkthrough.html) | [Trust and governance](../use/trust-and-governance.md) |

## 1. If you have a local checkout

Bring up the default local stack:

```bash
make setup
make start
```

Then verify:

- CALIBER UI loads at `http://127.0.0.1:5001/caliber/`
- the readiness surface responds
- you can log in and reach the product shell

Use [Local bring-up](../operate/local-bring-up.md) if you need the operator path
instead of the shortest happy-path summary.

## 2. If CALIBER is already running somewhere

Do not start with architecture. Start by answering one practical question:
what are you here to do?

Use one of these next pages:

- [Choose your CALIBER path](../start/choose-your-path.md)
- [SDK guide](../sdk/guide.md)
- [Trust and governance](../use/trust-and-governance.md)
- [Guided walkthrough](../../docs-site/walkthrough.html)

## 3. Verify that the system is usable

For operators, the meaningful check is readiness rather than simple process
health.

Use:

- [Health and readiness](../operate/health-and-readiness.md)
- [Local bring-up](../operate/local-bring-up.md)

## 4. Pick the right next track

| If you are... | Read this next |
| --- | --- |
| Integrating from Python | [SDK guide](../sdk/guide.md) |
| Working at the raw HTTP layer | [REST API overview](../api/overview.md) |
| Operating the stack | [Configuration and provider setup](../operate/configuration-and-provider-setup.md) |
| Using CALIBER features | [Prompts](../use/prompts.md), [Workflows](../use/workflows.md), [Knowledge bases](../use/knowledge-bases.md) |
| Reviewing trust and release posture | [Trust and governance](../use/trust-and-governance.md) |
