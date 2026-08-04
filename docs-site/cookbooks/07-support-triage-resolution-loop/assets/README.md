# SCN-07 assets — create these

Concrete, copy-pasteable artifacts for [the recipe](../README.md): a full-stack
support copilot that **classifies → gathers evidence → drafts a cited reply →
escalates safely**, returning one of four outcomes: `reply`, `clarify`,
`escalate_support`, `escalate_bug`. This is the integration showcase — it
**composes SCN-01/02/03/05/06**, so several pieces are reused rather than rebuilt.

External write (`issue_write`) is **approval-gated via the `human_approval`
node** — a write tool is gated at WORKFLOW time through that node, never on the
direct-invoke path (which only honors `allowed:false`). **Citations come from the
prompt contract**: `support-reply` requires every external fact in
`customer_reply` to be cited inline from supplied evidence, and the
`GroundedSupportReply` judge fails any reply that asserts an uncited external
claim.

## Build order

| # | Artifact | File | Create via UI + API |
| --- | --- | --- | --- |
| 1 | Tool `lookup_account_state` (read) | [`tools/lookup-account-state.tool.json`](tools/lookup-account-state.tool.json) | `Library → Tools → New tool` (Spec stage), paste the fields. API: `POST /tools` with the file body. `side_effect_level=read` runs live in the sandbox. |
| 2 | Tool `lookup_recent_incidents` (read) | [`tools/lookup-recent-incidents.tool.json`](tools/lookup-recent-incidents.tool.json) | `Library → Tools → New tool` (Spec stage). API: `POST /tools` with the file body |
| 3 | Prompt `ticket-intake` | [`prompts/ticket-intake.md`](prompts/ticket-intake.md) | `Library → Prompts → New prompt`, paste the body below the frontmatter. API: `POST /prompts {name, template, commit_message}`. Bind to the `ticket_intake` agent node |
| 4 | Prompt `support-reply` | [`prompts/support-reply.md`](prompts/support-reply.md) | `Library → Prompts → New prompt`, paste the body. API: `POST /prompts`. Bind to the `reply_generation` agent node — this is the prompt that carries the citation + approval contract |
| 5 | Skill `support-tone-and-deflection` | [`skills/support-tone-and-deflection.md`](skills/support-tone-and-deflection.md) | `Library → Skills → New skill`, paste `summary` + `content` + `category` + `tags`. API: `POST /skills` |
| 6 | Skill `high-risk-escalation-checklist` | [`skills/high-risk-escalation-checklist.md`](skills/high-risk-escalation-checklist.md) | `Library → Skills → New skill`. API: `POST /skills`. Holds the decision rules for when refund/external-write needs approval and when to `escalate_bug` |
| 7 | Skill `citation-and-next-steps` | [`skills/citation-and-next-steps.md`](skills/citation-and-next-steps.md) | `Library → Skills → New skill`. API: `POST /skills`. **Shared with SCN-06** (see Reuse below) — this copy is retuned for the four support outcomes |
| 8 | Eval dataset `support-ticket-cases` | [`dataset/ticket-cases.jsonl`](dataset/ticket-cases.jsonl) | `Evaluate → Test Sets → New dataset`, then add each row. API: `POST /eval-datasets {name}` → `POST /eval-datasets/{id}/examples` per line |
| 9 | Judge `GroundedSupportReply` | [`judges/grounded-support-reply.judge.json`](judges/grounded-support-reply.judge.json) | `Evaluate → Judges → New judge`, paste fields. API: `POST /judges`. The real `custom_judge` from [`../verification.yaml`](../verification.yaml) |

External write tool (`issue_write`) is **not** authored here — it is the
**GitHub MCP** server's tool, governed in SCN-05 (see Reuse). Set it
`requires_approval:true` and wire it after the `human_approval` node.

## Reused vs new

This scenario deliberately reuses the starter assets. Author the **new** rows
above; for the **reused** rows, point the workflow at the existing artifact (or
build the minimal version from the referenced scenario).

| Piece | Reused from | New here? | Notes |
| --- | --- | --- | --- |
| Prompt `ticket-intake` | extends SCN-01 `intake-classifier` | **New** | Same classifier idea, but emits the 4-outcome `decision` (`reply`/`clarify`/`escalate_support`/`escalate_bug`) instead of `needs_review`. Reuse SCN-01's prompt directly if you prefer; this one adds routing |
| Prompt `support-reply` | new (echoes SCN-06 `kb-answer` grounding + SCN-03 reply discipline) | **New** | Carries the citation + `requires_approval` contract the judge and the gate read |
| Skill `support-tone-and-deflection` | adapts SCN-02 `support-tone-and-citation` | **New** | Same voice rules, adds the self-serve deflection nudge for the `reply` outcome. You may bind SCN-02's skill instead |
| Skill `high-risk-escalation-checklist` | new (encodes SCN-03's `requires_approval` rules at the ticket level) | **New** | The decision rules: refund/credit/account-change/external-write → approval; confirmed defect/outage → `escalate_bug` |
| Skill `citation-and-next-steps` | **SCN-06** | Copy | Shared skill; this copy is retuned for the four support outcomes (`reply`/`clarify`/`escalate_support`/`escalate_bug`). If the SCN-06 skill is already registered, reuse it and skip step 7 |
| Tool `lookup_account_state` (read) | SCN-03 `lookup_order` (sibling callable) | **New spec** | Maps to `demo_tools:get_order` per [`../build.yaml`](../build.yaml); SCN-03 registered the richer `lookup_order`. Either read tool works |
| Tool `lookup_recent_incidents` (read) | new | **New** | Maps to `demo_tools:search_knowledge_base` |
| Tool `initiate_refund` (write) | **SCN-03** | Reused | Only needed if the `escalate_support` branch actually issues the refund inside the workflow; keep it after `human_approval` (mocked in sandbox, approval-gated in the workflow) |
| MCP `issue_write` (write) | **SCN-05** | Reused | The GitHub MCP server + its `requires_approval:true` policy ([`../../05-mcp-connectivity-governance-lab/assets/policy/github-issue-write.approval.policy.json`](../../05-mcp-connectivity-governance-lab/assets/policy/github-issue-write.approval.policy.json)). Wire as the `issue_write` `mcp_resource` node after `human_approval` |
| Support KB version | **SCN-06** | Reused | The support/policy KB built in SCN-06; the `kb_query` (`knowledge_query`) node retrieves from it into `support-reply`'s `{{ kb_chunks }}` |

## How the pieces fit (read [`../README.md`](../README.md) for the full recipe)

- **Start from the `hitl_review` template.** It ships the `human_approval` gate
  already wired. `Compose → Workflows → New` → template `hitl_review`, then add
  the nodes from [`../build.yaml`](../build.yaml):
  `ticket_intake (agent: prompt ticket-intake) → account_lookup (tool:
  lookup_account_state) → incident_lookup (tool: lookup_recent_incidents) →
  kb_query (knowledge_query over the SCN-06 support KB) → reply_generation
  (agent: prompt support-reply) → router (branch on decision) → human_approval →
  issue_write (mcp_resource: GitHub, on escalate_bug)`.
- **The router encodes the four outcomes.** Branch on `reply_generation`'s
  `decision`: `reply`/`clarify` finish at `output`; `escalate_support` opens an
  internal note (and, if you wire the refund, goes through `human_approval →
  initiate_refund`); `escalate_bug` goes through `human_approval → issue_write`.
- **Approval is enforced by the gate, not the prompt.** `support-reply` only
  *flags* `requires_approval`; the actual block is the `human_approval` node. With
  approval **rejected** (`run-reject`), no external write fires (run ends
  blocked/failed) — that is the `approval_required_for_refund_or_external_write`
  rule_check, observable in the trace.
- **Citations come from the contract.** `support-reply` forbids any uncited
  external fact and any invented source id; `GroundedSupportReply` (step 9) fails
  a reply that asserts an uncited claim, leaks internal jargon, or self-authorizes
  a refund/write — covering the `citations_required_for_external_claims` rule_check.
- **Evaluate.** Create the `support-ticket-cases` dataset from
  [`dataset/ticket-cases.jsonl`](dataset/ticket-cases.jsonl), then `Evaluate →
  Evaluations → Run evaluation` with scorers = `contains_expected` (on
  `expectations.decision`) + `Judge.GroundedSupportReply`. Route low-scoring runs
  to a **Review Queue** and answer them. Gates ([`../verification.yaml`](../verification.yaml)):
  `grounded_reply_score_min ≥ 0.90`, `approval_compliance = 1.0`,
  `escalation_precision_min ≥ 0.85`.

## Conventions used across the pack

- **Prompt files** (`prompts/*.md`): YAML frontmatter (name, model hint,
  variables) then the literal template body. Paste the body into the authoring
  textarea; variables are `{{ snake_case }}` — here `ticket-intake` reads
  `{{ ticket_text }}`/`{{ channel }}`; `support-reply` reads `{{ ticket_text }}`,
  `{{ account_state }}`, `{{ incidents }}`, `{{ kb_chunks }}`.
- **Skill files** (`skills/*.md`): SKILL.md style — frontmatter (kebab-case
  `name` that must **not** start with `claude`/`anthropic`, one-line `summary`,
  `category`, `tags`, `render_variables`) then the literal content. The `summary`
  is the narrow one-line trigger text the deterministic selector reads.
- **Tool files** (`tools/*.tool.json`): the `POST /tools` body —
  `{name, version, module_path, callable_name, input_schema, output_schema,
  side_effect}`; `module_path` + `callable_name` must be importable. `side_effect`
  ∈ `read`|`write`|`external_action` (`read` runs live in the sandbox;
  `write`/`external_action` are mocked there and approval-gated in a workflow).
- **Dataset files** (`dataset/*.jsonl`): one example per line,
  `{"id", "tags", "inputs", "expectations"}` — the shape Evaluations scorers +
  judges read (`{{ inputs }}`, `{{ outputs }}`, `{{ expectations }}`). `inputs` is
  `{ticket_text, channel}`; `expectations` is `{decision, requires_approval,
  must_cite}`.
- **Judge files** (`judges/*.judge.json`): `{name, model, instructions,
  feedback_value_type}`; instructions reference `{{ inputs }}`/`{{ outputs }}`/
  `{{ expectations }}` (the UI requires at least one). `feedback_value_type` ∈
  bool|int|float|str. `GroundedSupportReply` is `bool` (pass/fail) and is the real
  `custom_judge` from `verification.yaml`.
