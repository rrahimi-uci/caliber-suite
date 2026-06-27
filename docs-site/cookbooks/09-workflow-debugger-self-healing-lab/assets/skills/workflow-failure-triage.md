---
name: workflow-failure-triage
summary: "Triage a failed CALIBER workflow run: classify the symptom (guardrail block / approval reject / node exception / wait timeout), point to the likely node, and name the next probe in the Run Monitor. Diagnosis only — patching is a manual editor edit."
category: operations
tags: [workflow, debugging, run-monitor, recovery, triage]
render_variables: [run_status, failing_node, node_error, recent_events]
---

# Workflow Failure Triage

Use this checklist when a workflow run is in a terminal `failed` (or stuck)
state and you need to localize the cause fast in the **Run Monitor**. It maps a
symptom to the likely node type and the next concrete probe. It does NOT change
the workflow: the fix is always a manual edit of the manifest in the editor
followed by **save a new version** (there is no `propose_workflow_patch` tool),
and any assistant text here is narration — the actual recovery is done with the
run-retry / run-approve / run-resume controls.

## First, read the run state

- Open the run in **Run Monitor**. Note `{{ run_status }}` and the active node
  from the **Checkpoint** panel (active node id + state blob).
- Open the **Debugger** panel and find the last node with an `error` marker —
  that node id is your prime suspect. Read its inputs/outputs.
- Open the **Recovery** panel for the approval/event timeline and any integrity
  warnings.

## Symptom -> likely node -> next probe

| Symptom (from status / debugger) | Likely failure kind | Likely node | Next probe |
| --- | --- | --- | --- |
| Run sat at `waiting_approval`, then went `failed` after a reject; error mentions approval/rejected | **approval_rejected** | a `human_approval` node (e.g. `review`) | Recovery panel: confirm the reject event + reviewer/reason. Recovery = `run-retry` then `run-approve` -> `run-resume`. |
| A node shows a raised exception in the debugger (`KeyError`, `ValueError`, traceback) | **node_exception** | a `python_code` / `tool` node | Debugger: read that node's input; reproduce with the same input. Fix is a manual edit to that one node (validate/guard the bad field), save new version. |
| Run failed at/after a `guardrail` node; output was blocked or redacted | **guardrail_block** | a `guardrail` node (e.g. `pii_guard`) | Inspect the guardrail node's check + `on_failure`. Decide: tighten upstream output, or adjust the guardrail scope. Manual edit + new version. |
| Run never reached terminal success; stuck/expired waiting on an event or clock | **wait_timeout** | a `wait_for_event` / `wait_until` node | Recovery panel: check the awaited event + timeout. Use `resume-by-event` if the event is now available; else adjust the timeout (manual edit). |
| Run failed before the first node; "workflow_not_found" / bad input shape | **bad_request** (not a node fault) | none (run setup) | Re-check the workflow id and the input payload against the input schema before re-running. |

## Localize, don't guess

- Tie every conclusion to a node id and the actual error text from the
  debugger. If the evidence does not name a node and a reason, say so and gather
  more trace detail before proposing a change.
- Keep the fix minimal and scoped to the failing node. A smaller patch isolates
  impact and is easier to validate.

## After you localize

1. Reproduce the failing input once more to confirm it is deterministic.
2. Make the smallest manual edit in the editor that addresses the root cause;
   **save a new version**.
3. Validate: Preview + a real run of the failing input on the new version, then
   re-run a small regression slice of prior-good inputs.
4. Convert the incident into a regression case (add the reproducer to the
   scenario dataset) so it cannot silently come back.

Inputs you may be given for narration:
- run status: `{{ run_status }}`
- failing node: `{{ failing_node }}`
- node error: `{{ node_error }}`
- recent events: `{{ recent_events }}`
