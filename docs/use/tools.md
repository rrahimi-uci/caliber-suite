---
audience:
  - system-user
  - developer
  - operator
doc_type: how-to
product_area: tools
stability: ga
prerequisites:
  - A CALIBER deployment with tool access
reviewed_on: 2026-08-10
version_applicability: current main branch docs contract
tags:
  - tools
  - sandbox
  - testing
  - workflows
---

# Tools

Use this page when you need the practical path for callable tools: define the
tool, test it, wire it into a workflow or assistant surface, and understand the
sandbox and approval boundaries.

## At a glance

| Task | Start here | Deep reference |
| --- | --- | --- |
| define or register a tool | shape the callable contract first | [Tools architecture](../03-tools/architecture.md) |
| verify tool behavior | use fixture-backed tests | [Tools architecture](../03-tools/architecture.md) |
| call the tool from a workflow | connect it through a workflow node | [Workflows](../use/workflows.md) |
| govern side effects | inspect approval and runtime policy boundaries | [Trust and governance](../use/trust-and-governance.md) |

## 1. What tools are for in CALIBER

Tools are versioned callable assets. They are not just snippets attached to a
prompt. CALIBER tracks their contract, tests, and execution boundaries because
they may touch real external systems.

## 2. Common tasks

| You want to... | Read this next |
| --- | --- |
| package a new callable | [Tools architecture](../03-tools/architecture.md) |
| use a tool in a flow | [Workflows](../use/workflows.md) |
| expose the tool to an agentic surface | [Aria assistant](../use/aria-assistant.md) |
| audit why a tool call was blocked or paused | [Trust and governance](../use/trust-and-governance.md) |

## 3. Common failure modes

| Symptom | First thing to check |
| --- | --- |
| The tool works locally but not in CALIBER | packaging, sandbox assumptions, or missing dependencies |
| The tool appears but cannot run live | approval policy or runtime permission boundary |
| The workflow compiles but the tool step fails | tool schema/inputs do not match the runtime call shape |

## 4. Related docs

- [Workflows](../use/workflows.md)
- [Aria assistant](../use/aria-assistant.md)
- [Tools architecture](../03-tools/architecture.md)
- [Developer troubleshooting](../build/developer-troubleshooting.md)
