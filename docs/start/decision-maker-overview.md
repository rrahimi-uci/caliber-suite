---
audience:
  - decision-maker
  - architect
doc_type: concept
product_area: strategy
stability: ga
prerequisites:
  - A platform-evaluation question
reviewed_on: 2026-08-10
version_applicability: current main branch docs contract
tags:
  - strategy
  - overview
  - adoption
---

# Decision-maker overview

This page is the shortest non-implementation path through what CALIBER is, what
problems it solves, and where to go next for technical proof.

## At a glance

| Topic | Current position |
| --- | --- |
| Product shape | MLflow-integrated control plane for governed agentic workflows |
| Primary problem | Building, evaluating, calibrating, governing, and observing LLM workflows from one platform |
| Deployment topology | Standalone CALIBER ASGI service; communicates with MLflow over HTTP via `MLFLOW_TRACKING_URI`. Embedded mode is unsupported. |
| Governance posture | Explicit evidence, review, approval, and apply/release control paths |
| Best technical proof | [Layered architecture](../../ARCHITECTURE.md) |
| Market context | [Competitive analysis](../competitive-analysis.md) |
| Forward plan | [Roadmap](../roadmap.md) |

## 1. What CALIBER is

CALIBER is not only an evaluation tool and not only a workflow canvas. It is a
governed platform for the full lifecycle around LLM-powered artifacts and
workflows:

- author
- test
- calibrate
- review
- apply or release
- observe

## 2. Why teams adopt it

The platform is most useful when a team needs more than isolated prompt testing.
Its value comes from keeping the lifecycle, evidence, and control surfaces in
one system rather than scattering them across separate point tools.

## 3. Deployment model

CALIBER runs as a **standalone ASGI service** that communicates with a separately running vanilla MLflow server over HTTP via `MLFLOW_TRACKING_URI`. CALIBER owns its own metadata database (`CALIBER_DATABASE_URL`), separate from MLflow's backend store.

The embedded `mlflow.app` mode is **not a supported deployment or developer path**. See the [architecture](../../ARCHITECTURE.md) for the full topology support matrix.

## 4. Governance posture

CALIBER is designed to keep authority explicit.

In practical terms, that means:

- evidence generation is not the same as live release
- calibration is not hidden autopilot
- approvals and apply/release paths remain visible control points

For the full trust model, use:

- [Trust and governance](../use/trust-and-governance.md)
- [The refinement loop](../refinement-loop.md)

## 5. Recommended next reads

| If you need... | Read this next |
| --- | --- |
| the technical proof | [Layered architecture](../../ARCHITECTURE.md) |
| the architecture entry path | [Architecture reader guide](../architecture/index.md) |
| the product/user journey | [Choose your CALIBER path](choose-your-path.md) |
| market position | [Competitive analysis](../competitive-analysis.md) |
| roadmap and execution direction | [Roadmap](../roadmap.md) |
