---
audience:
  - operator
  - developer
doc_type: how-to
product_area: operations
stability: ga
prerequisites:
  - A CALIBER failure or degraded runtime question
reviewed_on: 2026-08-20
version_applicability: current main branch docs contract
tags:
  - troubleshooting
  - operations
  - readiness
  - recovery
---

# Operator troubleshooting

Use this page for first-pass diagnosis. If the failure is already severe or
indeterminate, jump straight to the [Operations runbook](../runbook.md).

## At a glance

| Symptom | First thing to check | Escalate to |
| --- | --- | --- |
| UI loads but product behavior is degraded | readiness, dependency posture | [Health and readiness](../operate/health-and-readiness.md) |
| Workflow runs are not draining | event backend and worker state | [Operations runbook](../runbook.md) |
| File upload or extraction fails | object-store and workflow-storage config | [Storage and state](../operate/storage-and-state.md) |
| Local stack will not boot cleanly | port and dependency configuration | [Configuration and provider setup](../operate/configuration-and-provider-setup.md) |
| Auth works in the browser but not automation | token, project scope, or CSRF model | [Developer troubleshooting](../build/developer-troubleshooting.md) |
| Gateway page reports the LLM gateway unreachable | whether the gateway container is actually serving | [Gateways](../10-gateways/architecture.md) |

## 1. Start with the smallest reliable signal

Before changing configuration, confirm:

- whether the process is alive
- whether readiness is degraded
- which dependency boundary is failing

That avoids masking the real issue with unrelated restarts.

## 2. Known local bring-up issues

The local launcher already guards one common failure mode: host-port collisions
around `MLFLOW_PORT`, `CALIBER_PORT`, and `MLFLOW_GATEWAY_PORT`.

If the stack still fails to start cleanly, check:

- port ownership
- `deploy/.env` and `.env`
- provider and storage settings

## 3. The gateway reports unreachable

An unreachable LLM gateway is more often a startup failure than a network one.
The MLflow AI Gateway validates every endpoint before it serves any of them, so
one unresolved `$VAR` placeholder can stop the whole server.

Check, in this order:

- **Is the container up?** A gateway that exits and restarts leaves its port
  unbound, which CALIBER can only observe as unreachable.
- **What did it log at startup?** The bundled image drops endpoints whose
  provider keys are unset and names each one it skipped. That is expected
  behavior, not a fault.
- **Did it exit non-zero?** With no endpoint configured at all the container
  exits and names the keys it looked for. Set one of those keys in the
  suite-root `.env` and restart.

A discovered inventory smaller than `deploy/mlflow-gateway/gateway.yaml` means a
provider key is missing, not that the gateway is broken. See
[Gateways](../10-gateways/architecture.md) for the full behavior.

## 4. When to leave troubleshooting and use the runbook

If the failure affects release safety, queue settlement, rollback semantics, or
indeterminate external effects, use the runbook immediately.

## 5. Related docs

- [Health and readiness](../operate/health-and-readiness.md)
- [Configuration and provider setup](../operate/configuration-and-provider-setup.md)
- [Storage and state](../operate/storage-and-state.md)
- [Gateways](../10-gateways/architecture.md)
- [Operations runbook](../runbook.md)
