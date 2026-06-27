# Self-Healing Workflows

## Demo objective

A repeatable operator playbook for debugging workflow failures and applying
reviewed fixes, with a clear line between assistant suggestions and
human-approved changes.

## Feasibility & substitutions

Read [`../FEASIBILITY.md`](../FEASIBILITY.md). Key points:

- ✅ The recovery surfaces are real: **Run Monitor**, **checkpoint panel**,
  **recovery panel**, **debugger panel** (step trace), **retry lineage**
  ("Attempt N of M"), and `run-retry` / `run-resume` / `resume-by-event`.
- ⚠️ **Patching is manual**: edit the manifest in the editor and **save a new
  version**. There is **no** `propose_workflow_patch` tool and the semantic
  patch API is not wired to the UI (FEASIBILITY §1, Workflows).
- ⚠️ **Aria/Aria Plans cannot debug a workflow** as a first-class capability —
  its registry covers ops like judge/review-queue/skill creation. Use Aria (if
  at all) only to *narrate* the diagnosis; do the actual recovery in the Run
  Monitor.
- 🟢 Reliable reproducible failure for the demo: the **`hitl_review`** template
  driven through **reject → retry → approve → resume** (this is exactly the
  recovery loop covered by the e2e suite).

## Prerequisites & seed

- One workflow that can fail reproducibly (reuse SCN-07, or a fresh
  `hitl_review`) and a failing input from [`test-data.yaml`](test-data.yaml).

## Recipe (UI-first, with API fallbacks)

1. **Reproduce the failure.** Open the workflow editor → `Run Monitor` →
   `run-execute` the failing input. Drive it to a terminal/`failed` state (for
   `hitl_review`: reach `waiting_approval`, then `run-reject` → `failed`).
   - API: `POST /workflow-runs`, `.../approval/reject`.
2. **Capture the run + open diagnostics.** Note the failing run id. Open the
   **Recovery** panel (approval/event timeline, integrity warnings), the
   **Checkpoint** panel (active node + state blob), and the **Debugger** panel
   (per-step inputs/outputs + event markers).
3. **Localize the root cause.** In the debugger step trace, find the node that
   failed and read its inputs/outputs. Write a one-line root-cause note tied to
   that node's evidence.
4. **Retry from checkpoint / lineage.** `run-retry` → confirm the **retry
   lineage** shows "Attempt 2 of N" and the run re-enters from the checkpoint
   (for `hitl_review`: `run-approve` → `run-resume` → `completed`).
   - API: `POST /workflow-runs/{id}/retry`, `.../resume`.
5. **Apply a minimal patch (manual).** In the editor, make the smallest change
   that fixes the root cause (fix a node config / guardrail / branch) and **save
   a new version**. Keep scope minimal to isolate impact.
6. **Validate.** Run a **Preview** + a real run of the failing input on the new
   version → confirm it now succeeds. Re-run a small regression slice (a few
   prior-good inputs) to confirm no new failures.
7. **Compare.** Put the pre-fix and post-fix run ids side by side in
   Observability; confirm the failing node is now green and the slice passes.

## Demo evidence to capture

- Failing run id and patched (post-fix) run id.
- Root-cause note linked to a concrete failing node in the debugger trace.
- Post-fix regression-slice results.

## Done when / gate

- Root cause is explicit in trace evidence (`replay_success_rate = 1.0`).
- The fix is validated and does not regress prior behavior
  (`post_fix_regression_pass_rate_min ≥ 0.95`).
