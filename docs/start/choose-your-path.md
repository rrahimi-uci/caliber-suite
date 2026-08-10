---
audience:
  - developer
  - system-user
  - operator
  - architect
  - evaluator
  - decision-maker
doc_type: tutorial
product_area: docs
stability: ga
prerequisites:
  - A CALIBER question to answer
reviewed_on: 2026-08-10
version_applicability: current main branch docs contract
tags:
  - onboarding
  - navigation
  - start
---

# Choose your CALIBER path

This page exists so a reader can choose the right documentation path first,
before dropping into deep reference or architecture pages.

## At a glance

| Reader | Start here | Then go to |
| --- | --- | --- |
| Developer / integrator | [SDK guide](../sdk/guide.md) | [Auth and project scoping](../build/auth-and-project-scoping.md), [SDK vs REST API](../build/sdk-vs-rest-api.md), [Developer troubleshooting](../build/developer-troubleshooting.md) |
| Operator / admin | [Local bring-up guide](../operate/local-bring-up.md) | [Configuration and provider setup](../operate/configuration-and-provider-setup.md), [Health and readiness](../operate/health-and-readiness.md), [Operations runbook](../runbook.md) |
| System user | [Workflows](../use/workflows.md) | [Prompts](../use/prompts.md), [Knowledge bases](../use/knowledge-bases.md), [Aria assistant](../use/aria-assistant.md) |
| Evaluator / governance user | [Trust and governance guide](../use/trust-and-governance.md) | [Evaluation](../14-evaluation/architecture.md), [Calibration](../15-calibration/architecture.md), [QA plan](../13-qa-plan/architecture.md) |
| Architect | [Architecture reader guide](../architecture/index.md) | [Layered architecture](../../ARCHITECTURE.md), [Platform](../01-caliber/architecture.md), [Refinement loop](../refinement-loop.md) |
| Decision-maker | [Decision-maker overview](../start/decision-maker-overview.md) | [Competitive analysis](../competitive-analysis.md), [Roadmap](../roadmap.md) |

## 1. If you are integrating with CALIBER

Start with the Python SDK unless you have a concrete reason to work at the raw
HTTP layer.

Use the REST API directly when:

- you are not integrating from Python
- you need to compare wire behavior against the SDK
- you are working with an endpoint before a typed SDK method exists

Recommended path:

1. [SDK guide](../sdk/guide.md)
2. [Auth and project scoping](../build/auth-and-project-scoping.md)
3. [SDK vs REST API](../build/sdk-vs-rest-api.md)
4. [Error handling and retries](../build/error-handling-and-retries.md)
5. [SDK API reference](../sdk/reference.md)

## 2. If you are operating a deployment

Start from bring-up and readiness, not from architecture.

Recommended path:

1. [Local bring-up guide](../operate/local-bring-up.md)
2. [Configuration and provider setup](../operate/configuration-and-provider-setup.md)
3. [Health and readiness](../operate/health-and-readiness.md)
4. [Operator troubleshooting](../operate/troubleshooting.md)
5. [Operations runbook](../runbook.md)

## 3. If you are evaluating trust, governance, or release posture

Start from the trust surfaces that connect evidence, review, and apply.

Recommended path:

1. [Trust and governance guide](../use/trust-and-governance.md)
2. [Evaluation and test sets](../use/evaluation-and-test-sets.md)
3. [Calibration](../use/calibration.md)
4. [Review and release flows](../use/review-and-release-flows.md)

## 4. If you need system architecture

Start from the layered model, then read only the deep subsystem pages that
matter for the question you are answering.

Recommended path:

1. [Architecture reader guide](../architecture/index.md)
2. [Layered architecture](../../ARCHITECTURE.md)
3. [Platform](../01-caliber/architecture.md)
4. the subsystem pages that matter for your topic

## 5. If you are deciding whether CALIBER fits

Start from the platform shape and the market position, not the route modules.

Recommended path:

1. [Decision-maker overview](../start/decision-maker-overview.md)
2. [Competitive analysis](../competitive-analysis.md)
3. [Roadmap](../roadmap.md)
