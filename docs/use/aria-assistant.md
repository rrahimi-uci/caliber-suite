---
audience:
  - system-user
  - operator
  - developer
doc_type: how-to
product_area: assistant
stability: beta
prerequisites:
  - A CALIBER deployment with Aria access
reviewed_on: 2026-08-10
version_applicability: current main branch docs contract
tags:
  - aria
  - assistant
  - approvals
  - agentic
---

# Aria assistant

Use this page when you need the practical operator or user path for Aria:
start a session, understand supervised tool use, and know why a session paused,
waited, or required review.

## At a glance

| Task | Start here | Deep reference |
| --- | --- | --- |
| understand what Aria can do | start with the execution and approval model | [Assistant architecture](../12-assistant/architecture.md) |
| inspect supervised tool use | follow the session and approval path | [Assistant architecture](../12-assistant/architecture.md) |
| connect Aria to tools, skills, or MCP | use the feature guides first | [Tools](../use/tools.md), [Skills](../use/skills.md), [MCP servers](../use/mcp-servers.md) |
| reason about queued human review | use the governance path | [Trust and governance](../use/trust-and-governance.md) |

## 1. What Aria is for

Aria is CALIBER's permissioned assistant surface. It is designed to stay
observable and supervised rather than acting as an opaque background agent.

## 2. Common tasks

| You want to... | Read this next |
| --- | --- |
| understand why the assistant stopped or waited | [Assistant architecture](../12-assistant/architecture.md) |
| attach skills, tools, or MCP capabilities | [Tools](../use/tools.md), [Skills](../use/skills.md), [MCP servers](../use/mcp-servers.md) |
| reason about trust and review | [Trust and governance](../use/trust-and-governance.md) |

## 3. Common failure modes

| Symptom | First thing to check |
| --- | --- |
| The assistant paused instead of finishing | a human approval or review boundary was reached |
| A tool is visible but unavailable | the tool is blocked by policy or missing runtime access |
| Aria answered, but you cannot justify the answer | inspect the evidence, tool trace, or retrieval path |

## 4. Related docs

- [Trust and governance](../use/trust-and-governance.md)
- [Tools](../use/tools.md)
- [Skills](../use/skills.md)
- [Assistant architecture](../12-assistant/architecture.md)
