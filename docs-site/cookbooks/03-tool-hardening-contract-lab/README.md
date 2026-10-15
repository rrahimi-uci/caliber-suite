# Policy-Safe Decision Tool

## Demo objective

A deterministic policy tool (refund eligibility) with hard schemas, fixture
coverage, an approval-gated write, and an optional explanation layer.

## Feasibility & substitutions

Read [`../FEASIBILITY.md`](../FEASIBILITY.md). Key points:

- ✅ The deterministic policy is a visual **Data Transform → Decision table**
  node. Registered side-effect tools remain importable callables.
- ✅ **Sandbox** runs the real callable only for a `read` tool registered with
  `allow_in_preview=true`; a `read` tool without it is **mocked** too, and
  `write`/`external_action` tools are always **mocked** in preview and become
  **approval-gated** in a workflow — perfect for `initiate_refund`.
- ✅ **Fixtures + deterministic Hardening** run **inline** and score assertions.
- ⚠️ The **LLM-judge hardening lane** creates a durable run but the judge runs
  in the background.
- The `ExplanationFaithfulness` judge (`custom_judge`) is a real LLM judge; the
  `rule_checks` (`deterministic_decision_preserved`,
  `approval_required_for_high_risk`) are enforced by the decision-table node + the
  workflow approval gate, not by a "deterministic judge".
- Approving a paused run needs the `caliber.approver` scope (admins hold it)
  **and** an account other than the one that started the run: the run initiator
  gets a `403` unless the server sets `CALIBER_APPROVAL_ALLOW_SELF_APPROVAL=true`
  (single-operator installs). Reject needs the scope only, not a second person.

## Prerequisites & seed

- Tool registry (admin) access; fixtures with expected decisions in
  [`test-data.yaml`](test-data.yaml).
- To run the approval gate: the run queue, checkpointing and runtime approvals
  enabled, an approver account (`caliber.approver` scope) distinct from the run
  initiator, or `CALIBER_APPROVAL_ALLOW_SELF_APPROVAL=true` on a single-operator
  install.

## Recipe (UI-first, with API fallbacks)

1. **Register the lookup tools.** `Tools → Register Tool` (Spec stage):
   - `lookup_order` → `module_path=caliber.workflows.demo_tools`,
     `callable_name=lookup_order`, `side_effect_level=read`,
     `allow_in_preview=true` (otherwise the sandbox mocks it too),
     input `{order_id:string}`.
   - `initiate_refund` → same module, `callable_name=initiate_refund`,
     `side_effect_level=write`, `requires_approval=true` (it will be **mocked**
     in sandbox + gated in workflow). The wizard switches approval on for you.
   - API: `POST /tools {name, version, module_path, callable_name, input_schema, output_schema, side_effect_level, requires_approval, allow_in_preview}`
     (the request model is `extra="forbid"`, so the legacy `side_effect` key is
     rejected, and a `write`/`external_action` tool without
     `requires_approval=true` is rejected with a `400`).
2. **Sandbox.** `Tools → <lookup_order> → Sandbox → Test Run` with
   `{order_id:"O-1001"}`; confirm live read output (no `mocked` flag; with
   `allow_in_preview` off you would see `mocked:true` here too). Run
   `initiate_refund` and confirm the response is **mocked** (`mocked:true`).
3. **Fixtures.** `Tools → Fixtures` (or `PUT /tools/{id}/test-cases`): save
   deterministic cases `{name, input, assertion}` from test-data, using the real
   assertion types `no_error` / `output_contains` / `equals` — e.g. input
   `{"order_id": "ord_1120"}` with `output_contains: status` against
   `lookup_order`'s output `{order_id, items, status}`.
4. **Hardening (deterministic, inline).** `Tools → Hardening` → run the saved
   suite (`POST /tools/{id}/calibrate`); read `pass_rate` immediately. Pin a
   **baseline** (`POST /tools/{id}/baseline`). Edit a fixture / schema and re-run
   to show a regression delta in **Test Runs**.
5. **The decision logic.** Add **Data Transform**, operation **Decision table**.
   Enter the ordered amount/region rules and a default result. The node emits
   `{decision, matched, matched_rule}` (the matching rule's `result`, or the
   default with `matched:false`) without custom Python. It does **not** emit
   `reason_code` or `requires_approval`; the optional `python_code` body in
   [`assets/tools/decide_refund.py`](assets/tools/decide_refund.py) is an
   alternative that does.
6. **Bind into a workflow with an approval gate.** `Workflows → New Workflow`,
   template **`hitl_review`** (it ships the `human_approval` node). Wire:
   `lookup_order → policy_decision(data_transform) → human_approval → initiate_refund`.
   The `human_approval` node has no condition: when runtime approvals are enabled
   it pauses **every** run that reaches it. A `requires_approval` value, if you
   emit one from the `python_code` variant, is **informational only** (surfaced
   for the reviewer; the gate does not read it).
7. **Prove the gate in the run monitor.** First enable runtime approvals at the
   **deployment / Settings** level (turn on `workflow_run_runtime_approvals_enabled`
   **plus** checkpointing and the run queue — not a per-run checkbox). Then in the
   editor `Run Monitor`, click **Run** on a case → status `waiting_approval`;
   **Approve** marks the approval **approved** (the run stays `waiting_approval`),
   and a **separate** **Resume** advances the paused run so the refund fires.
   Approve as a different account than the one that clicked Run, holding the
   `caliber.approver` scope; the initiator gets a `403` unless
   `CALIBER_APPROVAL_ALLOW_SELF_APPROVAL=true`. **Reject** stops the run. Because
   the gate pauses every run, there is no "low-risk does not gate" path.
8. **Optional explanation.** Add a prompt/skill node that explains the decision
   *without changing it*; score it with the `ExplanationFaithfulness` judge in
   Evaluations only after the deterministic lane is green.

## Demo evidence to capture

- Tool versions + schema contracts.
- Deterministic suite run id + pass rate; baseline + one regression delta.
- Workflow run trace: run **paused at approval** (`waiting_approval`), then
  **Approve** (marks the approval approved, by a second approver account) followed
  by a separate **Resume** that advances the paused run so the refund fires.

## Done when / gate

- Deterministic pass rate ≥ `0.97`; `decision_mismatch_rate = 0`.
- Approval is enforced (every run pauses at the gate when runtime approvals are
  on) and the approve-then-resume sequence is visible in the run timeline.
- Explanation never contradicts the deterministic decision
  (`explanation_faithfulness_min ≥ 0.92`).
