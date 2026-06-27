# SCN-03 assets — create these

Concrete, copy-pasteable artifacts for [the recipe](../README.md). Build order:

| # | Artifact | File | Create via UI + API |
| --- | --- | --- | --- |
| 1 | Tool `lookup_order` (read) | [`tools/lookup-order.tool.json`](tools/lookup-order.tool.json) | `Library → Tools → New tool` (Spec stage), paste the fields. API: `POST /tools` with the file body |
| 2 | Tool `initiate_refund` (write) | [`tools/initiate-refund.tool.json`](tools/initiate-refund.tool.json) | `Library → Tools → New tool` (Spec stage). API: `POST /tools` with the file body. `side_effect=write` → mocked in sandbox, approval-gated in the workflow |
| 3 | Decision node `decide_refund` | [`tools/decide_refund.py`](tools/decide_refund.py) | **Not a registered tool.** `Compose → Workflows` → drag a **Python Code** node, paste the file body. Versions with the workflow |
| 4 | Eval dataset `refund-fixtures` | [`dataset/refund-fixtures.jsonl`](dataset/refund-fixtures.jsonl) | `Evaluate → Test Sets → New dataset`, then add each row. API: `POST /eval-datasets {name}` → `POST /eval-datasets/{id}/examples` per line. Also reusable as tool fixtures (`PUT /tools/{id}/test-cases`) |
| 5 | Prompt `refund-explanation` | [`prompts/refund-explanation.md`](prompts/refund-explanation.md) | `Library → Prompts → New prompt`, paste the body below the frontmatter. API: `POST /prompts {name, template, commit_message}` |
| 6 | Skill `policy-reason-normalizer` | [`skills/policy-reason-normalizer.md`](skills/policy-reason-normalizer.md) | `Library → Skills → New skill`, paste name/summary/content. API: `POST /skills` |
| 7 | Skill `customer-safe-refund-language` | [`skills/customer-safe-refund-language.md`](skills/customer-safe-refund-language.md) | `Library → Skills → New skill`. API: `POST /skills` |
| 8 | Judge `ExplanationFaithfulness` | [`judges/explanation-faithfulness.judge.json`](judges/explanation-faithfulness.judge.json) | `Evaluate → Judges → New judge`, paste fields. API: `POST /judges` |

## How the pieces fit (read [`../README.md`](../README.md) for the full recipe)

- **Deterministic lane first.** Register the two tools (1–2), sandbox them
  (`lookup_order` runs live; the `initiate_refund` **sandbox** response carries a
  top-level `mocked:true` — this is the sandbox envelope, not a normal run), then
  scope each fixture input to just `{"order_id": ...}` and run the fixtures (4) as
  a deterministic Hardening suite (`POST /tools/{id}/calibrate`) and pin a
  baseline. Gate: fixture pass rate ≥ `0.97`.
- **`decide_refund` is a `python_code` node, not a registered tool.** It is the
  first node after START and parses the JSON run input directly (a python_code
  node receives the run input as an unparsed string). It holds the deterministic
  eligibility rules (refund window ≤ 30 days, auto-approve limit, fail-closed on
  missing risk data) and emits `{decision, reason_code, requires_approval}`
  (`requires_approval` is informational only — the gate does not read it).
- **The `rule_checks` are NOT a judge.** `deterministic_decision_preserved` and
  `approval_required_for_high_risk` (verification.yaml) are enforced by the
  `decide_refund` node **plus** the `human_approval` gate — wire
  `decide_refund → human_approval → initiate_refund` so the write fires only
  after the paused run is approved *and* resumed. Runtime approvals are a
  **deployment-level** setting (enable `workflow_run_runtime_approvals_enabled`
  alongside checkpointing and the run queue in Settings, not a per-run toggle); once on,
  the `human_approval` node pauses **every** run that reaches it (it does not
  depend on `requires_approval`). Clearing a paused run takes two steps: Approve
  records the decision (the run stays `waiting_approval`), then a separate Resume
  advances it. `lookup_order` stays as the Tools-sandbox demo and is not part of
  this decision chain.
- **Explanation is optional and last.** Add the `refund-explanation` prompt (5)
  and the two skills (6–7) to phrase the decision without changing it, then
  score it with `Judge.ExplanationFaithfulness` (8) in Evaluations. Gate:
  `explanation_faithfulness_min ≥ 0.92`.

## Conventions used across the pack

- **Tool files** (`tools/*.tool.json`): the `POST /tools` body — `module_path` +
  `callable_name` must be importable; `side_effect` ∈ `read`|`write`|`external_action`
  (`write`/`external_action` are mocked in the sandbox and approval-gated in a
  workflow). `tools/*.py` holds inline `python_code` node bodies (stdlib only,
  no registration).
- **Dataset files** (`dataset/*.jsonl`): one example per line,
  `{"inputs": {...}, "expectations": {...}}` — the shape the Evaluations scorers
  + judges read (`{{ inputs }}`, `{{ outputs }}`, `{{ expectations }}`).
- **Prompt files** (`prompts/*.md`): YAML frontmatter (name, model hint,
  variables) then the literal template body; variables are `{{ snake_case }}`.
- **Skill files** (`skills/*.md`): frontmatter (kebab-case `name`, one-line
  `summary`) then the content body.
- **Judge files** (`judges/*.judge.json`): `{name, model, instructions,
  feedback_value_type}`; instructions reference `{{ inputs }}`/`{{ outputs }}`/
  `{{ expectations }}` (the UI requires at least one). `feedback_value_type` ∈
  bool|int|float|str.
