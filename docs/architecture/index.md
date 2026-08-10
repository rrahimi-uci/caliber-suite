---
audience:
  - architect
  - developer
  - evaluator
doc_type: concept
product_area: architecture
stability: ga
prerequisites:
  - A technical question about how CALIBER works
reviewed_on: 2026-08-10
version_applicability: current main branch docs contract
tags:
  - architecture
  - topology
  - trust
  - execution
---

# Architecture reader guide

This page is the curated entry point into the architecture corpus. Use it when
you need the right deep page quickly instead of reading the full sidebar tree.

## At a glance

| Question | Start here |
| --- | --- |
| What is the overall platform shape? | [Layered architecture](../../ARCHITECTURE.md) |
| How is the server assembled and deployed? | [Platform](../01-caliber/architecture.md) |
| How does governed change move from evidence to apply? | [The refinement loop](../refinement-loop.md) |
| How do workflows execute? | [Workflows](../06-workflows/architecture.md) |
| How does trust and quality work? | [Evaluation](../14-evaluation/architecture.md), [Calibration](../15-calibration/architecture.md), [QA plan](../13-qa-plan/architecture.md) |
| How does runtime evidence and recovery work? | [Observability](../09-observability/architecture.md), [Operations runbook](../runbook.md) |

## 1. Read in layers, not alphabetically

The architecture corpus is easiest to navigate in this order:

1. the system-wide layered model
2. the platform runtime and trust boundary
3. the refinement and governance path
4. only the subsystem pages that matter for your question

## 2. For deployment and runtime questions

Start with:

- [Layered architecture](../../ARCHITECTURE.md)
- [Platform](../01-caliber/architecture.md)

Then follow the subsystem page that matches the resource you care about.

## 3. For workflow and agentic behavior

Start with:

- [Workflows](../06-workflows/architecture.md)
- [Workflow components](../06-workflows/components.md)
- [Aria assistant](../12-assistant/architecture.md)

## 4. For trust, scoring, and governance

Start with:

- [The refinement loop](../refinement-loop.md)
- [Evaluation](../14-evaluation/architecture.md)
- [Calibration](../15-calibration/architecture.md)
- [QA plan](../13-qa-plan/architecture.md)

## 5. For operations and recovery

Start with:

- [Observability](../09-observability/architecture.md)
- [Operations runbook](../runbook.md)
- [Local bring-up](../operate/local-bring-up.md)
