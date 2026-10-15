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
reviewed_on: 2026-10-14
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

## What each node needs

All thirty-one node types are in the palette, and most work on a base install. These need something more,
and the palette marks each one that this server does not yet have with a "Needs ..." badge ("Needs a real LLM",
"Needs an object store", "Needs an MCP server", "Needs an allowlist", "Needs the run queue"). The full sentence
is on hover and in the component reference. The MCP badge always shows, because a registered MCP server is data
that configuration cannot check.

| Node | Needs | If it is missing |
| --- | --- | --- |
| `agent` | a real LLM provider | the default fake provider echoes a canned reply, and a calibration of the workflow is marked "Simulated evaluation" |
| `input_bucket`, `output_bucket` | a remote object store (MinIO or S3) | the bucket cannot be read or written |
| `knowledge_query`, `knowledge_build` | a remote object store, and for a query a built knowledge base version | the node fails at run time |
| `mcp_resource` | a registered MCP server that CALIBER can reach | the node fails at run time |
| `external_app` | `CALIBER_EXTERNAL_APP_ENTRYPOINT_ALLOWLIST` naming the entrypoint | every entrypoint is refused until the allowlist is set |
| `human_approval`, `wait_until`, `wait_for_event` | the run queue and checkpointing (`CALIBER_WORKFLOW_RUN_QUEUE_ENABLED=true` and `CALIBER_WORKFLOW_RUN_CHECKPOINTING_ENABLED=true`) | the run cannot pause; see [Waits, approvals and queued runs](../start/alpha-install.md#waits-approvals-and-queued-runs) |

"Configured" in the palette means the setting is present, not that the service answers; `/readiness` is what
probes reachability.

Preview refuses a workflow that contains any of the file, folder, bucket, `mcp_resource`, `python_code`,
`external_app`, `webhook` or `api_request` nodes, because it cannot isolate them from the host (a file input
pinned to a managed file is the one exception). A `python_code` node is authorable by operators. Preview refuses it and a published run blocks `import`, but
that is containment, not isolation, and it carries the same trust as a registered tool: the author is
trusted. See [Who is trusted to write tools](../use/tools.md#who-is-trusted-to-write-tools).

## How a workflow calibration is scored

Calibrating a workflow replays the baseline and each candidate through the executor and scores them. Under
the default fake LLM the executor replays canned outputs, so the run is marked **Simulated evaluation**
(`eval_results.simulated` is `true`, and `eval_results.provider` names the provider). Unless the LLM judge is
enabled and available the score is **structural** (`eval_results.scorer` is `structural`, otherwise
`llm_judge`): it checks that the run completed and used its tools, not that the output was good. The workflow's
calibration runs show these as a **Simulated evaluation** badge and a **Structural score** badge. Read both
markers before you apply a candidate.

## Who can do what

Inside a project, creating or changing a workflow or a workflow version, and clearing a workflow's session
memory, need `editor` or `owner`. Publishing a version, moving an alias and publishing a workflow as a
service need `editor` or `owner`. Running a workflow, starting a calibration and uploading a file into a run
need `reviewer`, `editor` or `owner`. Approving a run that is waiting for approval needs `reviewer` or
`owner`. Applying a calibration candidate needs `editor` or `owner`. A member whose role is too weak gets 403 and a
person outside the project gets 404. See [Auth and project scoping](../build/auth-and-project-scoping.md).

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
