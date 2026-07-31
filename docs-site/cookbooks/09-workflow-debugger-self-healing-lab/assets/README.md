# SCN-09 assets — create these

Concrete, copy-pasteable artifacts for [the recipe](../README.md). These support
the operator playbook: **reproduce a failure → localize it in the Run Monitor →
apply a manual patch → validate**. Build order:

| # | Artifact | File | Create via |
| --- | --- | --- | --- |
| 1 | Python Code node body `failing_node` (manufactures a reproducible node fault) | [`tools/failing_node.py`](tools/failing_node.py) | `Compose → Workflows` → drag a **Python Code** node → paste the module body. No registration; it versions with the workflow. |
| 2 | Eval dataset `failing-inputs` (inputs that reproduce failures + expected root cause) | [`dataset/failing-inputs.jsonl`](dataset/failing-inputs.jsonl) | `Evaluate → Test Sets → New dataset`, then add each row. API: `POST /eval-datasets {name}` → `POST /eval-datasets/{id}/examples` per line. |
| 3 | Prompt `diagnosis-summary` (optional — narrates the diagnosis) | [`prompts/diagnosis-summary.md`](prompts/diagnosis-summary.md) | `Library → Prompts → New prompt`, paste the body below the frontmatter. API: `POST /prompts {name, template, commit_message}`. |
| 4 | Skill `workflow-failure-triage` (optional — symptom→node→probe checklist) | [`skills/workflow-failure-triage.md`](skills/workflow-failure-triage.md) | `Library → Skills → New skill`, paste content below the frontmatter. API: `POST /skills`. |
| 5 | Judge `RootCauseQuality` (optional — grades diagnosis grounding) | [`judges/root-cause-quality.judge.json`](judges/root-cause-quality.judge.json) | `Evaluate → Judges → New judge`, paste fields. API: `POST /judges`. |

> Items 3–5 are **optional**: they let you score *whether a diagnosis is
> evidence-grounded* in Evaluations. The core demo (reproduce → localize → patch
> → validate) is done entirely in the **Run Monitor** and needs only items 1–2.

## What ships (be faithful — read [`../../FEASIBILITY.md`](../../FEASIBILITY.md))

- ✅ **Recovery surfaces are real**: **Run Monitor**, **Checkpoint** panel
  (active node + state blob), **Recovery** panel (approval/event timeline +
  integrity warnings), **Debugger** panel (per-step inputs/outputs + event
  markers), **retry lineage** ("Attempt N of M"), and the controls
  `run-execute` / `run-reject` / `run-approve` / `run-retry` / `run-resume` /
  `resume-by-event`.
- ⚠️ **Patching is MANUAL**: there is **no** `propose_workflow_patch` tool and
  the semantic patch API is not wired to the UI. A "patch" = edit the manifest
  in the editor and **save a new version**. Every fix below is a manual editor
  edit.
- ⚠️ **Aria / Aria Plans cannot debug or patch a workflow** as a capability. Use
  Aria, if at all, **only to narrate** the diagnosis (or generate the
  `diagnosis-summary` text). The actual recovery is the human operator driving
  the Run Monitor.
- ⚠️ The `verification.yaml` `monitoring.traces` entries (`failing_run`,
  `replay_session`, `patched_run`) are **evidence labels**, not literal span
  names — read the node tree in Observability, don't grep for them.

## Set up a reproducible failure

Two reliable, reproducible failures. **Option A** (approval reject on the
`hitl_review` template) is the most reliable and matches the e2e suite — do this
first. **Option B** adds a real *node-level* exception so you can show a code
fault and a manual code patch.

### Option A — approval reject → retry → approve → resume (most reliable)

This exercises the full recovery loop on a shipped template, no code needed.

1. **Create the workflow.** `Compose → Workflows → New from template →
   `hitl_review`. Its nodes are `start → agent → pii_guard (guardrail) → review
   (human_approval) → final`; the approval node id is **`review`**.
   - API: `POST /workflows {template_id: "hitl_review", ...}`.
2. **Reproduce the failure.** Open the workflow → **Run Monitor** →
   `run-execute` with a failing input from
   [`dataset/failing-inputs.jsonl`](dataset/failing-inputs.jsonl) (row **WF01**
   or **WF05**). The run advances to **`waiting_approval`** at `review`. Then
   **`run-reject`** it → the run goes to **`failed`** with an
   `approval_rejected` reason.
   - API: `POST /workflow-runs` → `POST /workflow-runs/{id}/approval/reject`.
3. **Capture + open diagnostics.** Note the failing run id. Open the
   **Recovery** panel (see the reject event + reason on the approval timeline),
   the **Checkpoint** panel (active node = `review`, state blob), and the
   **Debugger** panel (the `review` step shows the rejection).
4. **Localize.** In the Debugger trace, the failing node is **`review`**; the
   evidence is the reject reason. Write a one-line root cause tied to that node
   (the `workflow-failure-triage` skill maps this symptom → `human_approval`
   node → recovery probe; `diagnosis-summary` can render the note).
5. **Retry from checkpoint (lineage).** **`run-retry`** → confirm the **retry
   lineage** shows **"Attempt 2 of 2"** and the run re-enters from the
   checkpoint back at `waiting_approval`.
   - API: `POST /workflow-runs/{id}/retry`.
6. **Approve → resume → completed.** **`run-approve`** the `review` gate, then
   **`run-resume`** → the run reaches **`completed`**.
   - API: `POST /workflow-runs/{id}/approval/approve` → `POST
     /workflow-runs/{id}/resume`.

> Note for WF05: the reject reason is an *unsupported commitment* in the drafted
> reply. The "patch" there is a manual edit to the upstream `agent` prompt/draft
> (save new version) before the retry — same recovery loop, with a real fix.

### Option B — node-level exception → manual code patch (real node fault)

This manufactures a genuine node exception, then fixes it by **editing the node
and saving a new version** (the manual patch — there is no patch tool).

1. **Build the workflow.** New `blank` workflow → drag a **Python Code** node,
   id **`failing_node`** → paste the body of
   [`tools/failing_node.py`](tools/failing_node.py). Wire `start`'s input to the
   node's `payload` input port; wire the node's `result`/`text` to `final`.
2. **Reproduce the failure.** **Run Monitor** → `run-execute` with row **WF02**
   (`payload` missing `account_id`) or **WF03** (`amount: "N/A"`). The
   `failing_node` raises and the run lands in **`failed`** with a real
   exception, e.g. `ValueError: failing_node: invalid input -> missing or empty
   required field 'account_id'`.
3. **Localize in the Debugger.** Open the **Debugger** panel: the last node with
   an `error` marker is **`failing_node`**. Read its input (the malformed
   `payload`) and the raised error text — that node id + that exception **is**
   the concrete evidence the `diagnosis-summary` prompt and the
   `RootCauseQuality` judge must cite.
4. **Apply the minimal patch (MANUAL).** Open the `failing_node` node in the
   editor and make the smallest change that fixes the root cause: flip the
   sentinel `STRICT = True` → `STRICT = False` (the file's documented fix line),
   which switches the node from *indexing* the required keys (raises) to
   *validating* them and returning a structured `rejected_input` result instead
   of throwing. **Save a new version** of the workflow. This save **is** the
   patch — there is no `propose_workflow_patch` tool.
5. **Validate.** Run a **Preview** + a real `run-execute` of the SAME failing
   input (WF02 / WF03) on the new version → confirm it now **`completed`s** with
   `status = "rejected_input"` instead of crashing. Then re-run a small
   **regression slice** of prior-good inputs (e.g. a well-formed
   `{"account_id": "A-1007", "amount": 50}`) and confirm they still succeed.
6. **Compare.** Put the pre-fix (failed) and post-fix (completed) run ids side
   by side in **Observability**; confirm `failing_node` is now green and the
   slice passes.

## Optional: score the diagnosis (Evaluations)

To put a number on diagnosis quality:

1. Create the dataset from [`dataset/failing-inputs.jsonl`](dataset/failing-inputs.jsonl)
   (each row's `inputs` carry `failing_node` / `node_input` / `node_error` /
   `recent_events`; `expectations` carry `failure_kind` + `expected_root_cause`).
2. Author the [`diagnosis-summary`](prompts/diagnosis-summary.md) prompt; its
   variables map 1:1 onto the dataset `inputs` keys.
3. Create the [`RootCauseQuality`](judges/root-cause-quality.judge.json) judge.
4. `Evaluate → Evaluations → Run evaluation`: dataset = `failing-inputs`,
   candidate = the `diagnosis-summary` output, deterministic graders plus the
   `RootCauseQuality` judge ticked under **Custom LLM judges** (it runs as a
   `Judge.<id>` scorer for an automatic per-row verdict): a row passes only when
   the diagnosis blames the actual failing node, cites the real error, and
   proposes a minimal scoped fix.

> Evaluations requires a configured model provider; with none it returns 400
> (the expected real-only guard).

## Conventions used across the pack

- **Prompt files** (`prompts/*.md`): YAML frontmatter (name, model hint,
  variables) then the literal template body. Paste the body into the authoring
  textarea; variables are `{{ snake_case }}`.
- **Skill files** (`skills/*.md`): YAML frontmatter (kebab-case `name`,
  `summary`, optional `render_variables`) then the SKILL content. Reserved
  prefixes `claude*`/`anthropic*` are rejected.
- **Dataset files** (`dataset/*.jsonl`): one example per line,
  `{"id", "tags", "inputs": {...}, "expectations": {...}}` — the shape the
  Evaluations scorers + judges read (`{{ inputs }}`, `{{ outputs }}`,
  `{{ expectations }}`).
- **Judge files** (`judges/*.judge.json`): `{name, model, instructions,
  feedback_value_type}`; instructions reference `{{ inputs }}`/`{{ outputs }}`/
  `{{ expectations }}` (the UI requires at least one). `feedback_value_type` ∈
  bool|int|float|str.
- **Tool files** (`tools/*.py`): inline `python_code` node bodies. The runtime
  wraps the body as `run_python_node(input=None, context=None, inputs=None,
  run_input='')`; read upstream ports from the `inputs` dict and return a dict
  whose `text`/`result` keys become output ports. Stdlib only; runs in a
  subprocess sandbox.
