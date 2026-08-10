---
audience:
  - system-user
  - developer
  - operator
doc_type: how-to
product_area: mcp
stability: beta
prerequisites:
  - A CALIBER deployment with MCP access
reviewed_on: 2026-08-10
version_applicability: current main branch docs contract
tags:
  - mcp
  - integrations
  - tools
  - governance
---

# MCP servers

Use this page when the practical question is how to connect CALIBER to an MCP
server, verify the discovered tools, and govern what remote tools may do.

## At a glance

| Task | Start here | Deep reference |
| --- | --- | --- |
| register or configure a server | define connection and transport details | [MCP architecture](../05-mcp/architecture.md) |
| verify tool discovery | inspect the discovered inventory | [MCP architecture](../05-mcp/architecture.md) |
| govern remote actions | apply approval and policy boundaries | [Trust and governance](../use/trust-and-governance.md) |
| use MCP tools in assistant or workflows | wire them into the target surface | [Aria assistant](../use/aria-assistant.md), [Workflows](../use/workflows.md) |

## 1. What MCP adds to CALIBER

MCP lets CALIBER connect to externally hosted tool surfaces without baking each
integration directly into the core product. That increases power, but it also
raises the need for connection tests, inventories, and policy enforcement.

## 2. Common tasks

| You want to... | Read this next |
| --- | --- |
| connect a new MCP server | [MCP architecture](../05-mcp/architecture.md) |
| confirm the right tools are visible | [MCP architecture](../05-mcp/architecture.md) |
| allow only a narrow capability such as issue creation | [Trust and governance](../use/trust-and-governance.md) |
| debug an agentic flow using MCP tools | [Developer troubleshooting](../build/developer-troubleshooting.md) |

## 3. Common failure modes

| Symptom | First thing to check |
| --- | --- |
| The server connects but tools do not appear | discovery or transport mismatch |
| Tools appear but cannot run | policy, approval, or secret-source configuration |
| The same tool behaves differently across environments | endpoint, credentials, or server version drift |

## 4. Related docs

- [Trust and governance](../use/trust-and-governance.md)
- [Aria assistant](../use/aria-assistant.md)
- [Workflows](../use/workflows.md)
- [MCP architecture](../05-mcp/architecture.md)
