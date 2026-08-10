---
audience:
  - system-user
  - developer
  - operator
doc_type: how-to
product_area: workflows
stability: ga
prerequisites:
  - A CALIBER deployment with workflow access
reviewed_on: 2026-08-10
version_applicability: current main branch docs contract
tags:
  - workflows
  - deployments
  - runtime
  - debugging
---

# Workflows

Use this page for the practical workflow path: author, preview, run, deploy,
inspect, and recover. Use the deep architecture page only when you need the
underlying implementation model.

## At a glance

| Task | Start here | Deep reference |
| --- | --- | --- |
| design a workflow | start from the workflow mental model | [Workflows architecture](../06-workflows/architecture.md) |
| look up available nodes | inspect the component catalog | [Workflow components](../06-workflows/components.md) |
| run or deploy a workflow | use preview, run, and deployment surfaces | [Workflows architecture](../06-workflows/architecture.md) |
| debug a failing step | inspect runtime evidence and queue state | [Observability](../09-observability/architecture.md) |
| recover a stuck or unsafe run | use the operator recovery docs | [Operations runbook](../runbook.md) |

## 1. What workflows are for in CALIBER

Workflows are the orchestration surface for multi-step automation. They connect
prompts, tools, skills, MCP access, files, and approvals into a governed run
model.

## 2. Common tasks

| You want to... | Read this next |
| --- | --- |
| understand the workflow model | [Workflows architecture](../06-workflows/architecture.md) |
| choose the right node | [Workflow components](../06-workflows/components.md) |
| publish a workflow service | [REST API overview](../api/overview.md) |
| inspect run failures | [Health and readiness](../operate/health-and-readiness.md), [Operator troubleshooting](../operate/troubleshooting.md) |

## 3. Common failure modes

| Symptom | First thing to check |
| --- | --- |
| The workflow compiles but a live run fails | runtime inputs, approvals, or downstream dependencies |
| Runs are queued but not moving | event backend, worker health, or queue state |
| A deployment exists but consumers cannot call it | service path, auth, or project scoping |

## 4. Related docs

- [Workflow components](../06-workflows/components.md)
- [Health and readiness](../operate/health-and-readiness.md)
- [Operator troubleshooting](../operate/troubleshooting.md)
- [Workflows architecture](../06-workflows/architecture.md)
