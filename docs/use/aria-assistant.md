---
audience:
  - system-user
  - operator
  - developer
doc_type: how-to
product_area: assistant
stability: beta
prerequisites:
  - A CALIBER deployment with Aria access (CALIBER_FEATURE_ARIA=true)
reviewed_on: 2026-10-15
version_applicability: current main branch docs contract
tags:
  - aria
  - assistant
  - approvals
  - agentic
---

# Aria assistant

> **Disabled by default.** Aria is a preview area and is off by default; enable with `CALIBER_FEATURE_ARIA=true`
> and restart CALIBER. While it is off, the `/assistant/*` and `/aria/*` routes answer 404
> (`reason_code: feature_disabled`) and the Aria panel and the Plans page are hidden. Once it is on,
> `CALIBER_ASSISTANT_ENABLED` and a configured engine still decide whether Aria answers. Enabling the flag means
> you accept preview behavior. See [Feature flags](../start/alpha-scope.md#feature-flags).

Use this page when you need the practical operator or user path for Aria:
start a session, understand supervised tool use, and know why a session paused,
waited, or required review.

With Aria off, a few helpers inside otherwise supported pages go away too, because they call the assistant
(`/assistant/*`): the **Ask Aria** button, the Aria tab in Settings, **Describe it** prompt drafting, the prompt
Playground (its stage stays listed and says it needs `CALIBER_FEATURE_ARIA=true`, and points to **Evaluations**),
generating and running test cases inside the prompt workspace, the tool unit-test generator, and
Assistant-Guided Calibration. Each is hidden or shows a notice that says it needs Aria and names `CALIBER_FEATURE_ARIA`. The core journey does
not need them: build a test set by hand on the Test Sets page, calibrate, review and Apply.

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

Two controls decide what Aria may do on its own. The interaction mode is **Chat** (answer questions, create
nothing), **Design** (author and edit artifacts; the value is `build` in the API) or **Plan** (outline an
approach first). The approval mode is one of:

| Approval mode | What Aria does on its own |
| --- | --- |
| Ask first (`manual`) | proposes changes and asks before every validate, test, approve or publish action |
| Approve for me (`auto_safe`) | auto-runs safe validation and test steps, and still asks before approval or publish |
| Legacy auto (`auto_all`) | also runs safe, reversible mutation tools; approval and publish still need a separate authority |
| Agent review (`agent_review`) | an independent approver-scoped agent reviews the draft; publishing stays separate |
| Full autonomy (`full_autonomy`) | an independent reviewer agent approves a passing draft and a separate release service publishes it |

Agent review and Full autonomy are disabled in the selector ("Not configured") until the deployment can support
them. Read tools are offered in every mode. Safe tools are offered only in Design mode with an approval mode
other than Ask first, and mutating tools only in Design mode with Legacy auto or Full autonomy. Publish, promote,
deploy and spend actions are never callable by the model during the authoring turn.

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
| A tool Aria does not offer, or refuses to call | its tier is not allowed in the current interaction and approval mode, or it requires approval (a published OpenAPI tool that requires approval is never offered as a direct Aria tool) |
| Aria answered, but you cannot justify the answer | inspect the evidence, tool trace, or retrieval path |
| There is no Aria panel or Plans page, or a call answers 404 with `reason_code: feature_disabled` | `CALIBER_FEATURE_ARIA` is off (the default); set it to `true` and restart |

## 4. Related docs

- [Trust and governance](../use/trust-and-governance.md)
- [Tools](../use/tools.md)
- [Skills](../use/skills.md)
- [Assistant architecture](../12-assistant/architecture.md)
