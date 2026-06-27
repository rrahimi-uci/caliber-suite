# Scenario Pack — Critique & Implementability Review

A correctness/implementability review of all 15 scenarios against the **shipped
code** (`caliber/caliber-ui/src`, `caliber/src/caliber`). Method: a self-review
plus an independent adversarial agent, every load-bearing claim checked against
the source (capabilities/planner/executor, runtime, schemas, routes, sandbox,
template catalog, eval/scorecard). **All findings below were fixed**; the pack
re-validates clean (60 YAML / 34 JSON / 12 JSONL / 5 Python parse; the 3 fixed
`python_code` bodies execute and return the expected dicts).

## Findings (all resolved)

| # | Sev | Finding | Reality (code) | Fix |
| --- | --- | --- | --- | --- |
| 1 | **blocker** | Aria scenarios said you supply each step's spec by answering the interaction with `inputs:{…}` | `AriaInteractionAnswerRequest` is `{approved,choice,value}` (`extra="forbid"`); `answer()` never merges into `step.inputs`; the default `HeuristicPlanner` emits `inputs={}`. An auto-run `judge.create` would get `{}` → ValidationError. | Reframed the Aria track honestly: **Aria plans from intent (real); artifacts are created via each capability's own route** with the `assets/` specs. Full hands-off autonomy needs an LLM planner (Protocol slot exists, **not wired**). Updated `ARIA-AUTONOMY.md` §Execution status + all 4 READMEs, assets READMEs, build.yaml feasibility, judge notes. |
| 2 | **blocker** | 3 `python_code` bodies (`decide_refund`, `lookup_recent_deployments`, `query_service_health`) ended with a module-level `result = …`/call | The runtime wraps a body lacking `def run_python_node(` into a function with **no return** → output `None`. | Rewrote all 3 to define `def run_python_node(...): return {"text", "result"}` (the SCN‑04/09 contract). **Verified by executing them.** |
| 3 | **major** | `tools/*.tool.json` used `"side_effect"` | `ToolRegisterRequest` field is **`side_effect_level`** (`extra="forbid"`) → 422. | Renamed in all 5 tool.json + the FEASIBILITY §3 example. |
| 4 | **major** | read tool.json claimed "runs live" but omitted `allow_in_preview` | Read tools are **mocked unless `allow_in_preview: true`** (`sandbox.should_mock_in_preview`). | Added `allow_in_preview: true` to the 4 read tools (write `initiate_refund` stays mocked). |
| 5 | minor | `FEASIBILITY.md` listed `skill.create` as an Aria capability; SCN‑14 invented `eval_dataset.list`; SCN‑05 said `requires_approval` is enforced on direct invoke | No `skill.create`/`eval_dataset.list` in the registry; only `allowed:false` blocks on direct invoke (`requires_approval` gates at workflow time). | Corrected the registry list, removed `eval_dataset.list`, reworded SCN‑05. |

Minor doc imprecisions noted by the reviewer and left as-is (non-blocking): the
prompt test-render path param is `{agent_id}` not `{name}`; the register model is
`ToolRegisterRequest`; judges also accept `{{ conversation }}`/`{{ trace }}` vars.

## Per-scenario verdict

| # | Scenario | Implementable today | Key caveat (in its Feasibility note) |
| --- | --- | --- | --- |
| 01 | Prompt classifier | ✅ | provider required; Playground renders only → score via Evaluations; calibration queued |
| 02 | Skill triggering | ✅ | selection is deterministic (no LLM judge); package round-trip real |
| 03 | Tool hardening | ✅ (fixed) | tool reg needs `side_effect_level`+`allow_in_preview`; `decide_refund` is a `python_code` node; approval gate at workflow time |
| 04 | Doc → JSON | ✅ (fixed) | extract supports docx/pptx/xlsx; unsupported error comes from the Object Store **endpoint**, not the tool |
| 05 | MCP governance | ✅ (fixed) | only `allowed:false` blocks on direct invoke; assertion types `no_error`/`output_contains`/`equals` |
| 06 | KB assistant | ✅ | AGE needs manual Sync; provider for the faithfulness judge |
| 07 | Support triage | ✅ (fixed) | reuses 01/02/03/05/06; read tools need `allow_in_preview`; external write approval-gated in the workflow |
| 08 | Incident commander | ✅ (fixed) | evidence tools are `python_code` fixtures (now return correctly) |
| 09 | Workflow debugger | ✅ | patch is manual (no `propose_workflow_patch`); Aria narration-only |
| 10 | Judge governance | ✅ | alignment tally is manual; `/eval-datasets/:id` detail page + row editor now shipped (`EvalDatasetDetail.tsx`); LLM judges run as `Judge.<id>` scorers in Evaluations |
| 11 | Release signoff | ✅ | no release-scoring engine (operator rubric); Allure generated externally |
| 12 | **Aria** eval harness | ⚠️→✅* | *Aria **plans** from intent; create artifacts via routes. Full autonomy needs the LLM planner (not wired) — see ARIA-AUTONOMY §Execution status |
| 13 | **Aria** review queue | ⚠️→✅* | same Aria caveat |
| 14 | **Aria** starter kit | ⚠️→✅* | same Aria caveat (flagship 3-capability plan) |
| 15 | **Aria** triage loop | ⚠️→✅* | same Aria caveat + needs EXISTING workflow/agent/trace ids (SCN‑07); async calibrate parks + polls |

`✅* ` = the autonomous **planning** is real and demoable; **execution** is
operator-assisted via the capability routes until an LLM planner is wired.

## What's verified-correct (sampled)

Capability registry + tiers, the 13 workflow templates + node types, eval
scorers, MCP assertion types, `allowed:false`-only-blocks-direct-invoke,
"no deterministic judge type", KB
retrieval modes + inline calibration metrics, the manual-patch / no-release-engine
/ external-Allure disclaimers, every `tool.json` callable (`lookup_order`,
`get_order`, `initiate_refund`, `search_knowledge_base`, `extract_document`), and
every judge instruction referencing a template variable.

> **Update:** the earlier `/eval-datasets/:id` "not wired" caveat is no longer
> accurate — the detail page + row editor have shipped (`EvalDatasetDetail.tsx`:
> Add-example / revise / supersede / from-trace), and custom LLM judges are now
> selectable as `Judge.<id>` scorers in Evaluations.

## One product recommendation (to make the Aria track fully hands-off)

The capability **handlers** are real; the missing piece is **input population**.
Either wire an LLM-backed `Planner` that fills `PlannedStep.inputs` (the Protocol
slot already exists in `assistant/plans.py`), or extend
`AriaInteractionAnswerRequest`/`answer()` to merge a per-step `inputs` payload
into the step before it runs. Either change turns scenarios 12–15 from
"plan-then-create-via-routes" into true end-to-end-from-intent.
