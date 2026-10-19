# Scenario Pack — Critique & Implementability Review

A correctness/implementability review of the scenarios against the **shipped
code** (`caliber/caliber-ui/src`, `caliber/src/caliber`). Method: a self-review
plus an independent adversarial agent, every load-bearing claim checked against
the source (capabilities/planner/executor, runtime, schemas, routes, sandbox,
template catalog, eval/scorecard). **All findings enumerated below were fixed**;
the pack re-validated clean at review time (60 YAML / 34 JSON / 12 JSONL / 5
Python parse; the 3 fixed `python_code` bodies execute and return the expected
dicts). Scenarios 01–15 were the original review set; scenario 16 (which has only
a `README.md`, `training-steps.json`, and `assets/`) was checked later against the
same UI/route sources, and the Aria track (12–15) was re-verified when step inputs
became answerable (see finding 1). Afterwards every lab (01 to 16, plus the new lab 18)
was **rewritten after being run for real** against a disposable server, through the API
and the browser; the per-lab verdict below reflects those runs, and a few findings
(2 to 4, which name files of the original pack) are kept as the review-time record. Platform limitations discovered *after* this review — not among the
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

- **Document workflow reads text only.** `extract_document(ref)` requires
  `Path(ref).is_file()` (`workflows/ingestion_tools.py`), so the original
  Office-document path was dropped. Lab 04 reads **text** invoices through the
  `input_bucket` node (UTF-8, 100,000 bytes per object) and extracts with a
  deterministic `python_code` node; binary `.docx`/`.xlsx` cannot flow through it.
- **External GitHub MCP remains environment-gated and was not run.** Lab 05 uses its
  own `stdio` MCP server, because the egress policy refuses loopback HTTP. The
  GitHub variant (`https://api.githubcopilot.com/mcp/`) needs network access, a
  personal-access token and `api.githubcopilot.com` on
  `CALIBER_MCP_REMOTE_HOST_ALLOWLIST`; its request body is kept in
  `05-mcp-connectivity-governance-lab/assets/real-world/` and nothing about it
  was executed.
- **Aria's planner is deterministic-only.** The default `HeuristicPlanner` is
  keyword/synonym-driven (no LLM planner is wired), so intents must contain the
  literal domain words. It links a step's required field to an earlier step's
  output only when the field is a reference (ends in `_id`, today
  `review_queue.add_items.queue_id`); a `name` is never linked, so each create step
  in labs 12 and 14 asks for its own name. Inputs are collected through the plan's
  input forms (see finding #1).
- **A model is needed for any score.** On the default simulated stack Evaluations
  refuses (`400 evaluation requires a real LLM provider`) and prompt, skill and
  workflow calibration score with canned numbers marked `simulated`. The labs verify
  the refusal and the markers; labs 01 and 10 add a stand-in model to show the
  scored path.
- **No UI for aliases or promotions.** The alpha UI has one live alias (`prod`);
  other aliases, gated promotion and rollback are API only (labs 07, 08, 09).
- **`propose-patch` is API only and agent-shaped** (lab 09): it cannot repair a
  code node and no page calls it.

## Per-scenario verdict

Every lab was run end to end for real (SDK example, `curl` steps and Playwright spec);
the caveat column is what its README says is simulated, refused or not covered.

| # | Scenario | Implementable today | Key caveat (in its README) |
| --- | --- | --- | --- |
| 01 | Trustworthy Intake Classifier | ✅ | 44-case file import; scoring a prompt is refused on the simulated model (`400`), shown with a stand-in model; calibration evidence is canned and marked simulated; Apply, registry, live board, timeline, audit and rollback are real; the Playground, Generate Test Cases and Runs stage (Aria assistant) are not used |
| 02 | Precision Skills | ✅ | trigger selection is deterministic (30/40, then 40/40 on 40 cases); calibration is simulated and a skill has no test set; package round-trip real, a rename keeps instructions but not tags, category or triggers |
| 03 | Policy-Safe Decision Tool | ✅ | shipped `demo_tools` callables; 49 fixtures; the sandbox is a process boundary (network still reachable); a second person must approve; needs runtime approvals, the run queue, checkpointing and the approver scope |
| 04 | Document-to-JSON Pipeline | ✅ | text documents only (`input_bucket`), deterministic extractor and JSON Schema node, no model; needs an S3-compatible store; only the gate's text reaches nodes after a resume |
| 05 | Governed Tool Connectivity (MCP) | ✅ | the lab's own six-tool `stdio` server; every tool starts denied; only `allowed:false` blocks on the admin Playground, `requires_approval` holds a tool in a workflow; GitHub variant not run |
| 06 | Grounded Knowledge Assistant | ✅ | retrieval is real; answer text is an extractive digest, Faithfulness and Answer-correctness are blank and no answer/abstain/clarify decision is produced on the simulated stack; needs an object store and the local embedding model |
| 07 | Support Triage Copilot | ✅ | the classifier is rules, `draft_reply` is simulated; KB retrieval, GitHub MCP write and the `GroundedSupportReply` judge are not exercised; aliases API only |
| 08 | Incident Response Copilot | ✅ | declarative workflow, so Preview and the deploy gate can run it (41 incidents); `draft_update` simulated; calibration finds no candidate; live evidence and MCP steps removed |
| 09 | Self-Healing Workflows | ✅ | 35 tickets, 24 fail on version 1 and none on version 2; patch is manual (`propose-patch` is API only and cannot repair a code node); Preview and deploy gates cannot run a Python-node workflow |
| 10 | Trustworthy Evaluation | ✅ | a judge needs a real model: the default stack refuses, a stand-in model runs it; certifies `FaithfulnessJudge` and rejects `LenientJudge` on 50 rows; Human Alignment imports completed Review Queue labels |
| 11 | Release Signoff Factory | ✅ | 20 candidates, 14 ready and 6 blocked; scores are typed in by people, not read from Evaluations; signing off deploys nothing; a signed candidate is final |
| 12 | **Aria** eval harness | ✅ | rules-based planner (28 goals); each step asks for its own name; the 30 rows are added separately; scoring the judge is refused on a simulated stack |
| 13 | **Aria** review queue | ✅ | 26 goals; `queue_id` is linked, the trace ids are asked; 30 flagged replies, 12 reviewed; the enqueue step does not check the ids exist |
| 14 | **Aria** starter kit | ✅ | four-step plan (28 goals), `add_items` skipped, three different names; a denied step takes its dependants with it and a failed step does not undo what was made |
| 15 | **Aria** triage loop | ✅ | needs EXISTING workflow, agent and traces (the lab's scripts create them) and the background workers; the plan parks in `waiting_job` and the worker resumes it; on the simulated stack the job is rejected and the plan fails with that reason |
| 16 | Production Observability & Triage | ✅ | replays 40 tickets through lab 09's version 1 (12 `ERROR`, 28 `OK`); no Evaluations baseline; the MLflow-down `503` was checked through the API only; no scenario/build/verification YAML |
| 18 | Projects, Roles, Tokens & Audit | ✅ | no model; 72 role-matrix requests, project-bound token refusals (`403`, `401`) and audit rows; needs accounts and the operator/approver scopes in server settings |

Every ✅ on 12–15 means the plan **executes** end to end through its input
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
