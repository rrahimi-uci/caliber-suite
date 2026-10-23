---
audience:
  - developer
doc_type: reference
product_area: api
stability: ga
prerequisites:
  - A CALIBER API integration question
reviewed_on: 2026-10-15
version_applicability: current main branch docs contract
tags:
  - api
  - reference
  - routes
  - resources
---

# REST API resource catalog

This page is the domain map for the CALIBER management API. It answers "where
does this capability live?" before you drop into per-route details.

## Platform and bootstrap

| Route family | Purpose |
| --- | --- |
| `/health`, `/readiness` | Liveness (a bare `status`/`version`/`db` body; 503 when the database probe fails) and dependency readiness (503 while a required dependency, including the database schema, is not ready) |
| `/auth`, `/csrf`, `/me` | Authentication, token/session plumbing, caller identity |
| `/capabilities`, `/openapi.json`, `/settings` | Surface discovery, stability metadata (`/capabilities` also reports which preview areas are on, under `features`), runtime configuration |
| `/dashboard/summary` | Overview aggregates used by the UI |

## Authoring and runtime assets

| Route family | Purpose |
| --- | --- |
| `/prompts` | Prompt registry, aliases, versions (each version row carries the `provenance` Apply recorded: job, dataset, evaluation provider, simulated or not), and refinement-related authoring |
| `/tools` | Tool registry, tests, calibration, and policy; list and detail responses add `builtin` (seeded by CALIBER), `availability` and `availability_reason` (for example the agent-memory tools while memory is off) |
| `/skills` | Skill registry, versions, packaging, tests, and calibration |
| `/mcp-servers` | Managed MCP definitions, inventories, connection tests, and governed tool invocation. Preview: off by default (`CALIBER_FEATURE_INTEGRATIONS`) |
| `/workflows`, `/workflow-components`, `/workflow-templates` | Workflow registry, composition, and templates; `/workflow-components` lists each node type with its `requires` ids and `requirements` (`id`, `label`, `met`), resolved against the running configuration |
| `/workflow-versions`, `/workflow-runs`, `/workflows/{id}/deployments` | Version lifecycle, execution, approvals, and deployment |
| `/services` and `/workflows/{id}/service` | Publish a workflow as an external HTTP service, invoke it, and inspect service tokens/OpenAPI |
| `/workflow-files`, `/workflow-cron-preview`, `/workflow-promotions`, `/workflow-benchmark-reports` | Run-workspace files, schedule preview, promotion approval/rejection, and benchmark reports |
| `/agents` | Agent records that verification items, refinement jobs, and approvals hang off |
| `/openapi-integrations` | Governed import of external OpenAPI contracts: pinned snapshots, operation classification, dependency detection, curated tool drafts, and publication into the tool registry. Preview: off by default (`CALIBER_FEATURE_INTEGRATIONS`) |

## Data, knowledge, and evidence

| Route family | Purpose |
| --- | --- |
| `/projects` and project file routes | Workspace/project inventory, effective access metadata, membership management, archive/restore and ownership transfer, revisions, import jobs, Change Requests, environments, source connection, and the managed file registry |
| `/object-store/*` | Raw bucket/object storage operations |
| `/knowledge-bases`, `/knowledge-base-versions`, `/knowledge/query` | RAG corpora, versions, graph state, and retrieval |
| `/eval-datasets` | Datasets, examples, trace import, sync, and restore |
| `/evaluations` | Scored evaluation runs |
| `/judges` | Model-backed graders and alignment |
| `/review-queues` | Human review queues and adjudication outputs |
| `/verification-queue` | Stage ① Verify: manually-flagged concerns, list/create/verify/dismiss/mark-duplicate/batch. Does not create a refinement job — see the SDK reference |
| `/knowledge-runs/{id}/events` | Live build-progress events for a knowledge-base run |
| `/playground-runs/{id}/files` | Files produced by an ad-hoc playground run |
| `/memory` | Long-term agent memory: add, search, list, and delete, always scoped by agent/user/run. Not part of the alpha: memory is off by default (`CALIBER_MEMORY_ENABLED=false`) and these routes answer 503 until it is configured |

## Operations and governance

| Route family | Purpose |
| --- | --- |
| `/jobs` | Durable background jobs (`eval_results` carries `provider` and `simulated`, and for workflow evaluations `scorer`), apply targets (an applied job also returns `approval_id`, `applied_by` and `applied_version`), request-changes (send a `candidate_ready` job back for another pass), and quality-reviews (a human go/no-go on the candidate, distinct from the machine eval gate) |
| `/rework-tasks` and `/projects/{id}/rework-tasks` | Owned, recoverable work auto-created when a refinement job is terminally rejected: list/get/claim/resolve/reassign; project routes enforce workspace visibility and role policy |
| `/releases` | Candidates, evaluation, waivers, signoff, reporting, and reconcile operations; `GET /releases/live` lists what is live (workflow deployments, each knowledge base's active version, and the prompt version last released to each alias). The candidate and report-job routes (`/releases/candidates*`, `/releases/report-jobs*`) are record-only and off by default (`CALIBER_FEATURE_RELEASE_CANDIDATES`, which also gates `/projects/{id}/releases*`); live, timeline and operations are core |
| `/{family}/{id}/rollback` | Per-family rollback operations on `/prompts`, `/skills`, `/agents`, `/knowledge-bases`, and `/workflows/{id}/deployments/{alias}` |
| `/observability/*`, `/metrics`, `/events/stream` | Traces, experiments, metrics, and SSE |
| `/audit-log` | Read-only audit explorer and export |
| `/gateway/*` and `/llm-pricing` | Gateway discovery, guardrails, usage, and pricing overrides |
| `/system/services`, `/system/incidents`, `/system/effects`, `/system/webhook-dead-letters` | Runtime health and operational recovery surfaces |
| `/gate-verdicts/{artifact_type}/{version_key}` | Version-addressable advisory evaluation-gate verdicts read before a promotion |
| `/secrets` | Write-only secret references and lifecycle |
| `/admin/platform-admins` | Metadata-only inventory of who holds the config-driven platform scopes (`caliber.admin`); grants no access to project content |
| `/webhooks/github` | GitHub webhook ingress; not bearer-authenticated, admitted by HMAC signature verification only |

## Assistant and agentic surfaces

These families are previews, each gated by a flag: a disabled one answers 404 with
`reason_code: feature_disabled`. `GET /capabilities` reports each under `features`.

| Route family | Purpose |
| --- | --- |
| `/assistant/*` | Session-based assistant interactions, drafts, queue, and attachments. On by default (`CALIBER_FEATURE_ARIA`) |
| `/aria/plans`, `/aria/interactions`, `/aria/capabilities` | Goal-plan decomposition, approval, execution, polling, and human questions. On by default (`CALIBER_FEATURE_ARIA`) |
| `/cookbooks` | Cookbook catalog and installation. Off by default (`CALIBER_FEATURE_COOKBOOKS`) |

## How this maps to the SDK

The [SDK guide](../sdk/guide.md) and [SDK API reference](../sdk/reference.md)
wrap the same route families above with typed models, error translation,
automatic CSRF handling, and polling helpers. The SDK docs should explain the
Python abstraction; this page is the raw route inventory.

## Project access routes

Project responses include the caller's effective `access_role` and a sorted
`permissions` list. The membership endpoints return `members` objects with the
member id, user id, role, active status, and audit timestamps:

| Route | Purpose | Required project role |
| --- | --- | --- |
| `GET /projects/{project_id}/members` | List active members | any visible member |
| `POST /projects/{project_id}/members` | Add or reactivate a member | `owner` |
| `PATCH /projects/{project_id}/members/{user_id}` | Change role or status | `owner` |
| `DELETE /projects/{project_id}/members/{user_id}` | Deactivate a member | `owner` |

Project roles are `owner`, `editor`, `reviewer`, and `viewer`. These roles are
project-level decisions; the caller's bearer-token scope remains a separate
global ceiling. The three write routes above also need the global
`caliber.operator` scope. The project's primary owner cannot be changed or
removed through the member routes (409; use ownership transfer), and granting the
`owner` role needs the target to hold both `caliber.operator` and
`caliber.approver` (409 otherwise). A caller who is not a member, `caliber.admin`
included, gets 404; a member whose role is too weak gets 403.
