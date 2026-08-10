---
audience:
  - operator
  - developer
doc_type: how-to
product_area: operations
stability: ga
prerequisites:
  - A running CALIBER deployment
reviewed_on: 2026-08-10
version_applicability: current main branch docs contract
tags:
  - health
  - readiness
  - observability
  - operations
---

# Health and readiness

Use this page when you need the operational meaning of CALIBER health signals:
what basic liveness proves, what readiness proves, and where to go next when a
runtime is up but not actually usable.

## At a glance

| Signal | What it answers | Use it for |
| --- | --- | --- |
| `/health` | Is the service process alive? | liveness and coarse orchestration checks |
| `/ajax-api/2.0/mlflow/caliber/readiness` | Is the runtime usable with its dependencies? | operator go/no-go decisions |
| traces, logs, metrics, incidents | What happened and why? | diagnosis and recovery |

## 1. Readiness matters more than liveness

An alive process is not the same thing as a usable CALIBER deployment.

For operator decisions, prefer the readiness surface because it captures whether
the surrounding runtime posture is actually suitable for real use.

## 2. What to verify after boot

After bring-up or configuration changes, verify:

- liveness responds
- readiness responds with the expected posture
- the UI is reachable
- the dependencies you actually need are healthy

## 3. When health is green but the system still feels broken

Use the deeper evidence surfaces:

- [Observability](../09-observability/architecture.md)
- [Operator troubleshooting](../operate/troubleshooting.md)
- [Operations runbook](../runbook.md)

## 4. Related docs

- [Local bring-up](../operate/local-bring-up.md)
- [Operator troubleshooting](../operate/troubleshooting.md)
- [Observability](../09-observability/architecture.md)
- [Operations runbook](../runbook.md)
