# REST API resource catalog

This page is the domain map for the CALIBER management API. It answers "where
does this capability live?" before you drop into per-route details.

## Platform and bootstrap

| Route family | Purpose |
| --- | --- |
| `/health`, `/readiness` | Liveness and dependency posture |
| `/auth`, `/csrf`, `/me` | Authentication, token/session plumbing, caller identity |
| `/capabilities`, `/openapi.json`, `/settings` | Surface discovery, stability metadata, runtime configuration |
| `/dashboard/summary` | Overview aggregates used by the UI |

## Authoring and runtime assets

| Route family | Purpose |
| --- | --- |
| `/prompts` | Prompt registry, aliases, versions, and refinement-related authoring |
| `/tools` | Tool registry, tests, calibration, and policy |
| `/skills` | Skill registry, versions, packaging, tests, and calibration |
| `/mcp-servers` | Managed MCP definitions, inventories, connection tests, and governed tool invocation |
| `/workflows`, `/workflow-components`, `/workflow-templates` | Workflow registry, composition, and templates |
| `/workflow-versions`, `/workflow-runs`, `/workflows/{id}/deployments` | Version lifecycle, execution, approvals, and deployment |
| `/services` and `/workflows/{id}/service` | Publish a workflow as an external HTTP service, invoke it, and inspect service tokens/OpenAPI |

## Data, knowledge, and evidence

| Route family | Purpose |
| --- | --- |
| `/projects` and project file routes | Workspace/project inventory and managed file registry |
| `/object-store/*` | Raw bucket/object storage operations |
| `/knowledge-bases`, `/knowledge-base-versions`, `/knowledge/query` | RAG corpora, versions, graph state, and retrieval |
| `/eval-datasets` | Datasets, examples, trace import, sync, and restore |
| `/evaluations` | Scored evaluation runs |
| `/judges` | Model-backed graders and alignment |
| `/review-queues` | Human review queues and adjudication outputs |

## Operations and governance

| Route family | Purpose |
| --- | --- |
| `/jobs` | Durable background jobs and apply targets |
| `/releases` | Candidates, evaluation, waivers, signoff, reporting, and reconcile operations |
| `/rollback` | Shared rollback helpers where an asset family exposes them |
| `/observability/*`, `/metrics`, `/events/stream` | Traces, experiments, metrics, and SSE |
| `/audit-log` | Read-only audit explorer and export |
| `/gateway/*` and `/llm-pricing` | Gateway discovery, guardrails, usage, and pricing overrides |
| `/system/services`, `/system/incidents`, `/system/effects`, `/system/webhook-dead-letters` | Runtime health and operational recovery surfaces |
| `/secrets` | Write-only secret references and lifecycle |

## Assistant and agentic surfaces

| Route family | Purpose |
| --- | --- |
| `/assistant/*` | Session-based assistant interactions, drafts, queue, and attachments |
| `/aria/plans`, `/aria/interactions`, `/aria/capabilities` | Goal-plan decomposition, approval, execution, polling, and human questions |
| `/cookbooks` | Cookbook catalog and installation |

## How this maps to the SDK

The [SDK guide](../sdk/guide.md) and [SDK API reference](../sdk/reference.md)
wrap the same route families above with typed models, error translation,
automatic CSRF handling, and polling helpers. The SDK docs should explain the
Python abstraction; this page is the raw route inventory.
