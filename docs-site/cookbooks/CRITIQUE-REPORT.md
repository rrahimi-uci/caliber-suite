# Scenario Pack — Critique & Implementability Review

A correctness/implementability review of all 16 scenarios against the **shipped
code** (`caliber/caliber-ui/src`, `caliber/src/caliber`). Method: a self-review
plus an independent adversarial agent, every load-bearing claim checked against
the source (capabilities/planner/executor, runtime, schemas, routes, sandbox,
template catalog, eval/scorecard). **All findings enumerated below were fixed**;
the pack re-validated clean at review time (60 YAML / 34 JSON / 12 JSONL / 5
Python parse; the 3 fixed `python_code` bodies execute and return the expected
dicts). Scenarios 01–15 were the original review set; scenario 16 (which has only
a `README.md`, `training-steps.json`, and `assets/`) was checked later against the
same UI/route sources, and the Aria track (12–15) was re-verified when step inputs
became answerable (see finding 1). Platform limitations discovered *after* this review — not among the
findings below — are tracked in [§ Open gaps](#open-gaps-platform-limitations).

## Findings (all resolved)

| # | Sev | Finding | Reality (code) | Fix |
| --- | --- | --- | --- | --- |
| 1 | **blocker** (superseded) | Aria scenarios said you supply each step's spec by answering the interaction with `inputs:{…}` | At review time `AriaInteractionAnswerRequest` was `{approved,choice,value}` (`extra="forbid"`), `answer()` never merged into `step.inputs`, and the default `HeuristicPlanner` emitted `inputs={}`, so an auto-run `judge.create` got `{}` → ValidationError. **Since then the code closed the gap:** the answer body has an `inputs` field, the executor raises an `input` interaction for missing required fields and merges + schema-validates the answer (`executor.py` `answer()`), the planner links dependent fields (`plans.py`), and the **Plans** UI renders an **Aria needs information** form (**Continue plan** / **Skip step**). | The scenarios were first reframed as "plan, then create each artifact via its own route", and have now been rewritten to the real flow: execute the plan, fill each input form, approve. `ARIA-AUTONOMY.md` §Execution status and the 12–15 READMEs, assets, `build.yaml`, `verification.yaml` and training steps describe it. |
| 2 | **blocker** | 3 `python_code` bodies (`decide_refund`, `lookup_recent_deployments`, `query_service_health`) ended with a module-level `result = …`/call | The runtime wraps a body lacking `def run_python_node(` into a function with **no return** → output `None`. | Rewrote all 3 to define `def run_python_node(...): return {"text", "result"}` (the SCN‑04/09 contract). **Verified by executing them.** |
| 3 | **major** | `tools/*.tool.json` used `"side_effect"` | `ToolRegisterRequest` field is **`side_effect_level`** (`extra="forbid"`) → 422. | Renamed in all 5 tool.json + the FEASIBILITY §3 example. |
| 4 | **major** | read tool.json claimed "runs live" but omitted `allow_in_preview` | Read tools are **mocked unless `allow_in_preview: true`** (`sandbox.should_mock_in_preview`). | Added `allow_in_preview: true` to the 4 read tools (write `initiate_refund` stays mocked). |
| 5 | minor | `FEASIBILITY.md` listed `skill.create` as an Aria capability; SCN‑14 invented `eval_dataset.list`; SCN‑05 said `requires_approval` is enforced on direct invoke | No `skill.create`/`eval_dataset.list` in the registry; only `allowed:false` blocks on direct invoke (`requires_approval` gates at workflow time). | Corrected the registry list, removed `eval_dataset.list`, reworded SCN‑05. |

Minor doc imprecisions noted by the reviewer and left as-is (non-blocking): the
prompt test-render path param is `{agent_id}` not `{name}`; the register model is
`ToolRegisterRequest`; judges also accept `{{ conversation }}`/`{{ trace }}` vars.

## Open gaps (platform limitations)

Found *after* this review by the `ui-complete-report.md` UI-completeness audit.
These are **product** gaps, not recipe defects, so they are not among the fixed
findings above:

- **Document workflow needs a host-local path.** `extract_document(ref)` requires
  `Path(ref).is_file()` (`workflows/ingestion_tools.py`); Object Store upload and
  the input-bucket node hand back decoded text / object keys, not a local file the
  extractor can open. Cookbook 04 lands the file on the host (`mc cp`) as a
  workaround.
- **External GitHub MCP remains environment-gated.** The catalog now uses
  GitHub's official remote Streamable HTTP endpoint, so it no longer depends on
  the retired npm server or on `npx`. Operators still must allowlist
  `api.githubcopilot.com` in `CALIBER_MCP_REMOTE_HOST_ALLOWLIST` and provide a
  GitHub personal-access token before connection and discovery can succeed.
- **Aria's planner is deterministic-only.** The default `HeuristicPlanner` is
  keyword/synonym-driven (no LLM planner is wired), so intents must contain the
  literal domain words. It links a step's required field to an earlier step's
  output of the same name, which includes `name`: in cookbooks 12 and 14 the test
  set (and, in 14, the review queue) is created under the judge's name and those
  steps do not prompt for one. Inputs themselves are collected through the plan's
  input forms (see finding #1).

## Per-scenario verdict

| # | Scenario | Implementable today | Key caveat (in its Feasibility note) |
| --- | --- | --- | --- |
| 01 | Prompt classifier | ✅ | provider required; Playground is a live chat, scored runs via the prompt Runs stage / Evaluations (which can now score a Prompt version); calibration queued |
| 02 | Skill triggering | ✅ | selection is deterministic (no LLM judge); package round-trip real |
| 03 | Tool hardening | ✅ (fixed) | tool reg needs `side_effect_level`+`allow_in_preview`; `decide_refund` is a `python_code` node; approval gate at workflow time |
| 04 | Doc → JSON | ✅ (fixed) | extract supports docx/pptx/xlsx; unsupported error comes from the Object Store **endpoint**, not the tool |
| 05 | MCP governance | ✅ (fixed) | only `allowed:false` blocks on direct invoke; assertion types `no_error`/`output_contains`/`equals` |
| 06 | KB assistant | ✅ | AGE needs manual Sync; provider for the faithfulness judge |
| 07 | Support triage | ✅ (fixed) | reuses 01/02/03/05/06; read tools need `allow_in_preview`; external write approval-gated in the workflow |
| 08 | Incident commander | ✅ (fixed) | evidence tools are `python_code` fixtures (now return correctly) |
| 09 | Workflow debugger | ✅ | patch is manual in the Run Monitor (the patch engine and the `propose_workflow_patch` assistant tool are not wired to a Run Monitor control); Aria narration-only |
| 10 | Judge governance | ✅ | Human Alignment imports completed Review Queue labels with provenance and computes agreement/κ/FP/FN; `/eval-datasets/:id` provides the row editor; LLM judges run as `Judge.<id>` scorers in Evaluations |
| 11 | Release signoff | ✅ | durable candidates, server-computed rubric/blockers, admin waivers, immutable signoff, rollback target, and in-product Allure-compatible report jobs |
| 12 | **Aria** eval harness | ✅ | plan → input form → approve works; the planner links the test set's `name` to the judge's, so the set is created as `AnswerFaithfulness`; evaluating needs a real model provider |
| 13 | **Aria** review queue | ✅ | `review_queue.add_items` is also planned (shared domain) — **Skip step** if there are no traces; `queue_id` is linked |
| 14 | **Aria** starter kit | ✅ | flagship 3-capability plan; the queue and test set are created under the judge's name (linked `name`); skip `add_items` |
| 15 | **Aria** triage loop | ✅ | needs EXISTING workflow/agent/trace ids (SCN‑07); async calibrate parks in `waiting_job` and the plan worker resumes it at `candidate_ready`/`applied`; default fake evaluator = canned scores |
| 16 | Observability triage | ✅ | needs real runs (07/09) with an error trace; no scenario/build/verification YAML — steps derive from its four quality gates |

Every ✅ on 12–15 now means the plan **executes** end to end through its input
forms and approval prompts; the planner itself is still the deterministic
`HeuristicPlanner` (no LLM planner).

## What's verified-correct (sampled)

Capability registry + tiers, the 13 workflow templates + node types, eval
scorers, MCP assertion types, `allowed:false`-only-blocks-direct-invoke,
"no deterministic judge type", KB
retrieval modes + inline calibration metrics, the operator-guided recovery
boundary, the durable release domain, every `tool.json` callable (`lookup_order`,
`get_order`, `initiate_refund`, `search_knowledge_base`, `extract_document`), and
every judge instruction referencing a template variable.

> **Update:** the earlier `/eval-datasets/:id` "not wired" caveat is no longer
> accurate — the detail page + row editor have shipped (`EvalDatasetDetail.tsx`:
> Add-example / revise / supersede / from-trace), and custom LLM judges are now
> selectable as `Judge.<id>` scorers in Evaluations.

## Product recommendation — resolved

The earlier recommendation (let the interaction answer carry per-step `inputs`)
shipped: `AriaInteractionAnswerRequest.inputs`, the executor's `input`
interactions and merge, and the **Plans** input form turn scenarios 12–15 into
end-to-end-from-intent runs. What remains is an LLM-backed `Planner` that could
fill `PlannedStep.inputs` itself and map natural phrasing; the `Planner` Protocol
slot in `assistant/plans.py` is where it would plug in.
