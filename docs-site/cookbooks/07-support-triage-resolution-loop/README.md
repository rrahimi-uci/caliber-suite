# Support Triage Copilot

## Demo objective

An end-to-end support assistant that returns one of four outcomes:
`reply`, `clarify`, `escalate_support`, or `escalate_bug`, with grounded,
cited replies and approval-gated high-risk actions.

## Feasibility & substitutions

Read [`../FEASIBILITY.md`](../FEASIBILITY.md). This is the integration showcase —
it composes SCN-01/02/03/05/06. Key points:

- ✅ All pieces are real: prompts, skills (deterministic trigger), tools (shipped
  `demo_tools`), KB `knowledge_query`, MCP write tool behind approval, and the
  `human_approval` gate.
- 🔁 **Reuse, don't rebuild**: the `intake-classifier` prompt (SCN-01), the
  `support-tone-and-citation` skill (SCN-02), the `lookup_order`/`initiate_refund`
  tools (SCN-03), the GitHub MCP server (SCN-05), and the support KB (SCN-06).
- Tool mapping: `lookup_account_state` → `caliber.workflows.demo_tools:get_order`
  / `lookup_order`; the installed example includes an offline incident fixture
  and a GitHub incident/issue API Request starter. External write
  (`issue_write`) → the **GitHub MCP** tool, kept `requires_approval:true`.
- `GroundedSupportReply` (`custom_judge`) is a real LLM judge; the approval and
  citation `rule_checks` are enforced by the workflow gate + the prompt contract.

## Prerequisites & seed

- SCN-01/02/03/05/06 assets present (or build the minimal versions inline).
- Ticket corpus in [`test-data.yaml`](test-data.yaml); a configured provider.

## Recipe (UI-first, with API fallbacks)

1. **Confirm reusable assets.** Tools: `lookup_order`, `initiate_refund`
   (write). Skill: `support-tone-and-citation` (bound). Prompt: a reply prompt
   with the system rule *"Use only supplied evidence; cite every external fact;
   never leak internal process."* KB: the support KB version (SCN-06). MCP:
   GitHub server with `issue_write` set `requires_approval:true`.
2. **Build the workflow.** `Compose → Workflows → New`, template
   **`hitl_review`** (ships the approval gate). Wire nodes from
   [`build.yaml`](build.yaml):
   `ticket_intake (agent: classify intent+severity, emit decision) →
   account_lookup (tool: lookup_order) →
   incident_lookup (fixture or configured API Request preset) →
   kb_query (knowledge_query over support KB) →
   reply_generation (agent: cited reply + machine decision) →
   router (decision) →
   human_approval (only for refund / escalate_bug external write) →
   issue_write (mcp_resource: GitHub, on escalate_bug)`.
3. **Encode the four outcomes.** In the `router`, branch on the reply node's
   `decision`: `reply`/`clarify` finish at `output`; `escalate_support` opens an
   internal note; `escalate_bug` goes through `human_approval → issue_write`.
4. **Run the ticket cases.** Open the editor `Run Monitor`; `run-execute` each
   ticket from test-data. For a refund/escalate_bug ticket, confirm status
   `waiting_approval`; `run-approve` → `run-resume` → the GitHub issue is created
   only after approval. For a how-to ticket, confirm a cited `reply` with no
   approval.
5. **Verify the safety branches.** Show that with approval **rejected**
   (`run-reject`), no external write occurs (run ends failed/blocked).
6. **Evaluate.** Turn the ticket outcomes into a Test Set; run **Evaluations**
   with `Judge.GroundedSupportReply` + `contains_expected` (decision label).
   Route low-scoring runs to a **Review Queue** and answer them.
7. **Iterate.** Tighten the skill summary / prompt rules (per SCN-02 §4) before
   loosening anything; re-run until gates pass.

## Demo evidence to capture

- Workflow version id + published run ids.
- One **approved** high-risk branch trace and one **rejected/blocked** one.
- Grounded-reply score + decision accuracy from the evaluation.
- Review queue with trace-linked reviewer answers.

## Done when / gate

- Replies are evidence-backed/cited (`grounded_reply_score_min ≥ 0.90`).
- Refund/external-write always require approval (`approval_compliance = 1.0`).
- Escalation routing is correct (`escalation_precision_min ≥ 0.85`) and visible
  in traces.
