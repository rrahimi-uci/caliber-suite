# CALIBER Scenario Pack — Feasibility & Recipe Conventions

This document is the **ground truth** behind every scenario recipe. It records,
from a direct read of the codebase (frontend `caliber/caliber-ui/src`, backend
`caliber/src/caliber`), exactly what CALIBER can do **today**, what is
environment-gated, and what remains deliberately bounded. The runtime-owned
catalog is exported to [`capabilities.json`](capabilities.json); that generated
inventory, rather than this prose, is the authoritative recipe list.

> **Preview areas are off by default (Aria ships on).** Aria and Plans, MCP servers and OpenAPI
> integrations, the Cookbooks gallery, and release-candidate signoff sit behind
> `CALIBER_FEATURE_ARIA`, `CALIBER_FEATURE_INTEGRATIONS`, `CALIBER_FEATURE_COOKBOOKS`
> and `CALIBER_FEATURE_RELEASE_CANDIDATES`. Every ✅ below that touches one of them
> assumes its flag is on; each recipe README names the flags it needs, and the
> table is in [`README.md`](README.md#feature-flags-read-first). The Playground
> and the prompt workspace's test generation and runs call the Aria assistant, so
> they need `CALIBER_FEATURE_ARIA` too.

API base for all endpoints: `/ajax-api/2.0/mlflow/caliber`.
SPA routes are under `/caliber`. A fresh local install seeds an `admin` / `admin` bootstrap login only when `auth_bootstrap_allow_insecure_default` is enabled (off by default; a local loopback launcher may opt in); otherwise provide the bootstrap admin password through the configured secret source.

---

## 1. Capability matrix (verified)

| Area | Capability | Status | Notes / where |
| --- | --- | --- | --- |
| **Prompts** | Author + version (MLflow Prompt Registry) | ✅ Works | `POST /prompts`, `POST /prompts/{name}/versions`. Active alias is `prod` only. |
| | Playground | ⚠️ Needs Aria + a model | The Playground tab runs a **chat through the Aria assistant** (`createAssistantSession` / `sendAssistantMessage`), so it needs `CALIBER_FEATURE_ARIA` and a model that answers. `POST /prompts/{name}/test-render` is the separate variable-only render. Lab 01 does not use the Playground, **Generate Test Cases**, **Run Tests & Judge** or the Runs stage for this reason (with the fake assistant, test generation fails with "LLM did not return a valid JSON array"). |
| | Test Sets / Runs / baseline | ✅ Works | A labelled set is imported from a file on the **Calibration** stage (**Upload JSON/JSONL**, one `{input, expected}` per line; 44 cases in lab 01). `POST /prompts/test-runs` persists a run and `POST /prompts/{name}/baseline` pins it, but the Runs stage calls the Aria assistant. The regression check lab 01 uses is **Evaluations › Compare to** a baseline run (needs a model that answers). |
| | Calibration (optimize) | ⚠️ Queued | `POST /prompts/optimization/runs` enqueues a background refinement job (MetaPrompt/GEPA). It does **not** finish inside the request; wait for `candidate_ready`. On the default fake provider the candidate is canned (the live prompt plus comment lines) and its evidence is marked `simulated: true` (**Simulated evaluation — not evidence**); the example count and dataset version are real. **Apply** is explicit (a viewer gets `403`, a candidate below the gate is `rejected` and its Apply is `409`) and moves the real MLflow alias; rollback restores it (lab 01). |
| | Bind | ✅ Works | `POST /prompts/{name}/bind` (agent / workflow_node / standalone). Workflow-manifest rewrite is deferred to deploy. |
| **Tools** | Register from importable Python | ✅ Works | `POST /tools` requires `module_path` + `callable_name` that the backend can import. **No inline-code authoring** in the registry. See §3. |
| | Sandbox test-run | ✅ Works | `POST /tools/{id}/test-run` imports and runs the real callable in a separate `python -I` process (`isolation: subprocess`); `write`/`external_action` tools are **mocked** (`mocked: true`), `read` tools run live when `allow_in_preview`. The sandbox is a process boundary, not isolation: a stalled tool is killed at the deadline, a large file write is refused and the environment is empty, but **the network is still reachable** (lab 03). |
| | Fixtures + deterministic hardening | ✅ Works (inline) | `PUT /tools/{id}/test-cases`, `POST /tools/{id}/calibrate` run saved cases through the sandbox and score assertions inline (lab 03 saves 49 fixtures, scores 49/49, pins the baseline and sees exactly one regression after one wrong fixture). A write tool registered without `requires_approval` is refused (`400`). |
| | LLM-judge hardening lane | ⚠️ Queued | Durable run row is created; judge execution is deferred to background jobs. |
| | Baseline / Publish | ✅ Works | `POST /tools/{id}/baseline`; deprecate/archive via `PATCH`/`DELETE` (blocked if a `prod` workflow references the tool). |
| **Skills** | Author + version | ✅ Works | `POST /skills`, `PATCH /skills/{id}` (content edit bumps version). Reserved prefixes `claude*`/`anthropic*` rejected. |
| | Render Preview | ✅ Works | `POST /skills/{id}/test-render` substitutes `{{vars}}`, reports unresolved. |
| | Trigger / selection tests | ✅ Works (deterministic) | `POST /skills/{id}/test-selection` uses a **deterministic** scorer (no LLM, so it is real on every stack): `is_selected`, `selection_score`, `selection_reason`. Lab 02 runs a 40-case trigger set: 30/40 right on the starter phrases, 40/40 after tightening. |
| | Package export / import | ✅ Works | Export a ZIP (exactly `<name>/SKILL.md` and `<name>/agents/openai.yaml`) and upload it directly in the Skill UI. Import is bounded and supports explicit reject (`409` for the same name), rename, or admin-only versioned merge; a rename keeps the instructions but **not** the tags, category or trigger phrases (lab 02). |
| | Calibration | ⚠️ Queued | `POST /skills/{id}/calibrate` enqueues a background job (same model as prompts). A skill has **no test set** (`n_examples` is `0`) and on the default fake provider the candidate and its scores are canned and marked `simulated: true`; the lab proves the wiring (queue, diff, Apply as a new version, rollback as a further version), not quality (lab 02). |
| | Scenario Sets | ⚠️ Scaffolded | UI present; rich scenario authoring is minimal today — use Trigger Tests + Runs. |
| **Workflows** | Cookbook system examples | ✅ Works (17) | **Cookbooks** installs any runtime-owned recipe atomically as a paused workflow and draft version after readiness review. |
| | Code-free deterministic transforms | ✅ Works | `data_transform` supports fixture, mapping, Draft 2020-12 JSON Schema, ordered decision tables, and weighted confidence. |
| | Review Queue enqueue | ✅ Works | `review_queue_enqueue` creates audited, idempotent trace-linked items and fails closed when the runtime binding is absent. |
| | Run monitor: execute/approve/reject/retry/resume | ✅ Works | `POST /workflow-runs`, `.../approval/approve`, `.../approval/reject`, `.../retry`, `.../resume`, `.../resume-by-event`. Checkpoints, retry lineage ("Attempt N of M"), recovery panel, debugger panel all real. |
| | HITL approval gate | ✅ Works | `human_approval` node → status `waiting_approval` → approve → `resuming`. RBAC role + multi-sig (`approval_count`) + `timeout_behavior`. |
| | Edit/patch workflow | ⚠️ Manual in the Run Monitor | To fix a failed run, edit the manifest in the editor and **save a new version**; no Run Monitor control turns a failure into a patch (lab 09 writes version 2 by hand, then reruns all 35 tickets). A semantic patch engine (`workflows/patch.py`) exists: `POST /workflow-versions/{id}/propose-patch` (operator) stores a patch candidate from failure evidence (listed on the workflow's **CALIBER Patches** tab), workflow **Calibrate** generates candidates you can apply, and Aria's chat assistant has a `propose_workflow_patch` tool (proposal only, for an assistant workflow *draft*). `propose-patch` only knows four agent-shaped fixes: for a Python code node it returns a generic prompt suggestion with no graph change, and a guardrail patch aimed at a code node is a `400`. None of these is an Aria Plans capability, and no UI page calls the propose-patch route. |
| | Preview, deploy gates and aliases | ⚠️ Partly | A workflow made only of declarative nodes (Data Transform, Join, Template, Router; lab 08) can be run in **Preview** and its deploy gate can replay a test set before `prod` moves (41 incidents in lab 08). A workflow with Python or API nodes cannot, and the gate fails closed (lab 09). The alpha UI shows one live alias (`prod`) and hides Deployments and Promotions: other aliases, promotions and rollback are API only (labs 07, 08, 09). |
| **Aria / Aria Plans** | Plan decompose → approve → execute | ✅ Works (deterministic planner) | Plan **decomposition** is a deterministic `HeuristicPlanner` (keyword/synonym→capability, e.g. "test set"→`eval_dataset.create`), **not** LLM-backed; no LLM planner is wired into `PlanService`. It links a step's required field to an earlier step's output only when the field is a reference (its name ends in `_id`, today `queue_id`); a `name` is never linked, so each create step asks for its own, and the rest is left empty; at run time the executor raises an `input` interaction and the **Plans** UI shows an **Aria needs information** form (**Continue plan** / **Skip step**; API `answer {inputs}`), which is merged and schema-validated before the step runs. A goal that matches nothing plans no steps, and approve/execute return 409. Async steps (`workflow.calibrate`) park in `waiting_job` and the plan worker resumes them automatically (`/poll` also works; the UI has no Poll button). The LLM engines (OpenAI/Anthropic + `fake` double) back Aria's conversational **assistant**, not the planner. `POST /aria/plans`, `/approve`, `/execute`, `/poll`; Plans belongs to the Aria preview area (on by default; `CALIBER_FEATURE_ARIA=false` disables it). |
| | Aria capability registry | ⚠️ Narrow | Nine capabilities: `judge.create`, `eval_dataset.create`, `review_queue.create`, `review_queue.add_items`, `workflow.calibrate`, plus reads `judge.list`, `review_queue.list`, `openapi_tool.list` and `openapi_tool.invoke` (read-only OpenAPI tools). **No** skill/prompt/tool/workflow authoring. Aria **cannot** debug/patch a workflow as a capability — use the run-monitor surfaces. See [`ARIA-AUTONOMY.md`](ARIA-AUTONOMY.md) + scenarios 12–15. |
| **Evaluations** | Dataset + scorers → scorecard | ✅ Works (real LLM) | `POST /evaluations`; **no fake fallback** — without a configured provider it returns `400 evaluation requires a real LLM provider` (and a missing test set is `404`). Labs 01, 06, 10, 12 and 14 verify that refusal on the default stack instead of a score; lab 01 also runs against a stand-in model (`assets/stand-in-model/server.py`, an OpenAI-compatible keyword stub, not a model) so the baseline-vs-weaker-v2 comparison can be seen. Per-example detail + run-vs-run compare with deltas. |
| | Scorers available | ✅ Works | `exact_match`, `token_f1`, `contains_expected`, `non_empty`, plus `Judge.<name>` LLM judges. |
| | Add a trace to a dataset | ✅ Works | `POST /eval-datasets/{id}/examples/from-trace` (auto-extracts input/expected, tags lineage). |
| | Disagreement / low-confidence surfacing | ✅ Works | Evaluation deltas identify candidates; Human Alignment imports completed queue labels and computes agreement/kappa/confusion counts. |
| **Judges** | Create custom LLM judge | ✅ Works | `POST /judges`: name, model, `instructions` (must reference `{{ inputs }}` / `{{ outputs }}` / `{{ expectations }}`), `feedback_value_type` (bool/int/float/str). Executed via MLflow `make_judge` (CALIBER requires `mlflow>=3.15.0,<4`). |
| | "deterministic" judge type | ❌ Not a judge | There is **only** the custom LLM judge type. Deterministic checks are **scorers** (`exact_match`, …) or **tool/skill assertions**, not a judge. Treat scenario `type: deterministic` as "use a deterministic scorer/assertion." |
| | Human alignment | ✅ Works | The Judges page computes agreement, Cohen's κ, and FP/FN and can import completed pass/fail Review Queue labels with queue/item/trace/question/reviewer provenance. **Import labels** passes every answer other than the pass/fail one to the judge as `{{ expectations }}`, so keep reviewer notes out of the queue. Lab 10 certifies `FaithfulnessJudge` (agreement 0.82, κ 0.6365 with its stand-in model) and rejects a weak `LenientJudge` (0.58, κ 0.0933). |
| **Test Sets** | List `/eval-datasets` | ✅ Works | Create dataset, add rows / from-trace / MLflow sync. |
| | Detail `/eval-datasets/:id` | ✅ Works | `EvalDatasetDetail.tsx` is a full row editor: **+ Add example** (set `inputs` + `expectations`/`expected` directly), edit/revise, retire/supersede, **Add example from trace**, version filter. Backend: `POST …/examples`, `…/revise`, `…/supersede`, `…/examples/from-trace`. |
| **Review Queues** | Enqueue trace → answer → write back to trace | ✅ Works | `POST /review-queues`, `.../items` (trace_ids), submit answers → MLflow assessments/expectations on the trace. Trace-linked. The enqueue step does not check that the trace ids exist, and a queue created outside a workspace is visible to its owner only (labs 13, 15, 16). Verifying an item starts no refinement job. |
| **Observability** | MLflow trace tree | ✅ Works | Read via `mlflow.get_trace`; span tree + timeline + token/cost attrs in the Observability page. |
| | Named semantic spans (`prompt_render`, `tool_lookup`, `retrieval_query`, `approval_gate`, `mcp_playground_invoke`, …) | ❌ Aspirational | Those exact span names are **not** emitted. A workflow run's root span is `workflow.run` and each node's span is `node.<id>` (for example `node.validate`); an MCP call that passes policy leaves `mcp.<server>.<tool>`. Treat every `monitoring.traces:` or `monitoring.evidence:` list in `verification.yaml` as "evidence to look for in the trace tree," **not** literal span names. An unknown trace id is `404`, a filter that matches nothing is an empty list, and with MLflow down the routes answer `503` (lab 16). |
| **MCP Servers** | Quick-connect catalog | ✅ Works (8) | GitHub, PostgreSQL, pgvector, Apache AGE, MinIO, Hugging Face, Ollama, Playwright. `data-testid="catalog-<id>"`. |
| | Test connection + discovery | ✅ Works | `POST /mcp-servers/{id}/test-connection`, `/discover-tools`, `GET /mcp-servers/{id}/tools` (tools + effective policy). |
| | Playground invoke | ✅ Works | `POST /mcp-servers/{id}/invoke-tool` (10s timeout). A refusal (unclassified, blocked, unknown, rate limited) and a tool's own error are `200` with `success: false`; a call that passes policy leaves an `mcp.<server>.<tool>` trace, a refusal leaves none (lab 05). |
| | Policy overlay | ✅ Works | Per-tool `allowed` (block), `side_effect_level` (`read`/`write`), `requires_approval`. `PATCH /mcp-servers/{id}/tools/{tool}/policy`. A tool is denied until classified; `allowed:false` is enforced on invoke; `requires_approval` holds a tool in a **workflow** (`waiting_approval`) but not on the admin Playground (lab 05). |
| | Tool calibration panel | ✅ Works | `PUT .../tools/{tool}/test-cases`, `POST .../tools/{tool}/calibrate`. |
| **Knowledge Bases** | Build / Explore / Calibrate / Use | ✅ Works | Explore sub-views: **Query** (ask), **Chunks**, **Graph**. Use stage is "query via API" docs (not an ask box). |
| | Retrieval modes | ✅ Works | `dense`, `hybrid`, `graph_hybrid`, `age_graph` (AGE only when enabled). |
| | Calibrate metrics (inline) | ✅ Works | Recall@k, nDCG@k, Faithfulness, Answer-correctness — computed synchronously. Recall@k and nDCG@k are real arithmetic on real retrieval; Faithfulness and Answer-correctness are blank (`null`) on the simulated judge, and the default assistant's answer text is an extractive digest of the retrieved chunks (lab 06). |
| | AGE sync | ✅ Works (manual) | Build does **not** auto-sync; click **Sync to AGE** (`POST /knowledge-base-versions/{id}/age-sync`) then the version is graph-served. |
| **Object Store** | Upload / list / preview / extract / delete | ✅ Works (S3/MinIO) | Extract supports `.docx`/`.pptx`/`.xlsx` (not legacy `.doc/.ppt/.xls`). A workflow's `input_bucket` node reads text only (UTF-8, 100,000 bytes per object), so binary Office files cannot flow through it (lab 04). |
| **Releases** | Candidate / waiver / signoff | ✅ Works (Preview, record-only) | Releases persists weighted criteria, evidence, blockers, admin waivers, rollback targets, and immutable go/no-go snapshots. Signing off records a decision; it does not deploy or roll back anything. Scores and evidence references are entered by people (not read from Evaluations); waiver expiry is honoured at evaluation and a signed candidate is final (lab 11). |
| **Administration** | Workspaces (projects), roles, access tokens, audit log | ✅ Works | The UI says *workspace*, the API says *project*. Roles are `owner`, `editor`, `reviewer`, `viewer`; lab 18 proves 72 requests (9 people x 8 actions) against `role-matrix.csv`, a project-bound token acts in its own workspace and gets `403` in another and `401` once revoked or expired, the primary owner cannot be removed (`409`), and the audit log, release timeline and live board record each action without a token secret (lab 18). `/readiness` names the simulated providers. |
| **Allure** | Release evidence job | ✅ Works | An authorized operator generates and monitors a durable in-product Allure-compatible JSON report job. The separate static HTML test report remains a CI publication artifact. |

---

## 2. The five things that most often trip up a recipe

1. **Prompt Playground is an Aria-assistant chat, not a scored regression suite.**
   It needs `CALIBER_FEATURE_ARIA` and a model that answers. Persisted scored
   evidence belongs in Evaluations over a Test Set (lab 01).
2. **"Deterministic judge" is not a thing.** Use a deterministic **scorer**
   (`exact_match`, `token_f1`, `contains_expected`, `non_empty`) in Evaluations,
   or a **tool/skill assertion** in Hardening/Trigger Tests. Reserve **Judges**
   for LLM-graded criteria (faithfulness, tone, escalation correctness).
3. **Use Data Transform before custom code.** Mapping, schema validation,
   decision rules, confidence, and fixtures are visual components, and a workflow
   made only of them can be previewed and gated (lab 08). A registered custom
   callable still must be importable Python.
4. **Calibration (prompts & skills) is queued, not instant.** It creates a
   background refinement job; wait for `candidate_ready`. On the default fake
   provider the candidate and its scores are canned and marked simulated, so the
   lab checks structure and wiring, never a score. **KB calibration and tool/MCP
   deterministic calibration *are* inline** and return immediately.
5. **Span names in `verification.yaml` are labels, not literals.** Real traces
   exist (MLflow), but spans are `workflow.run` and `node.<id>`. In Observability,
   open the run's trace and read the node tree — don't grep for `approval_gate`.

---

## 3. Recipe convention: making tools real

Every scenario that lists `tools:` can be implemented one of three ways. Pick
per tool:

**A. Register a shipped callable (fastest, no code).** Shipped modules you can
point at:

| `module_path` | useful `callable_name`s |
| --- | --- |
| `caliber.workflows.demo_tools` | `lookup_order`, `get_order`, `lookup_policy`, `track_shipment`, `escalate`, `initiate_refund` (write→mocked), `lookup_product`, `check_inventory`, `create_order` (write→mocked), `send_confirmation_email` (write→mocked), `search_knowledge_base` |
| `caliber.workflows.ingestion_tools` | `extract_document` (PDF/DOCX/PPTX/XLSX/MD + OCR) |
| `caliber.workflows.file_tools` | `read_workdir_file`, `list_workdir_files`, `write_workdir_file`, `create_artifact`, `get_file_metadata` |

Register via the **Tools → Spec** form (or API):

```json
POST /tools  (ToolRegisterRequest; extra fields are rejected)
{ "name": "lookup_order", "version": "1",
  "module_path": "caliber.workflows.demo_tools", "callable_name": "lookup_order",
  "input_schema": {"type":"object","required":["order_id"],"properties":{"order_id":{"type":"string"}}},
  "output_schema": {"type":"object"},
  "side_effect_level": "read",
  "allow_in_preview": true }
```

The field is **`side_effect_level`** (not `side_effect`) — `read` | `write` |
`external_action`. Read tools are **mocked in the sandbox unless
`allow_in_preview: true`**, so set it when you want the real lookup output in a
Test Run. Use `side_effect_level: "write"` (or `external_action`) for
`initiate_refund`, `create_order`, etc. — those are **always mocked in the
sandbox** and become approval-gated in a workflow, which is what the safety
scenarios want.

**B. Add your own callable.** Drop a pure function into a module (e.g.
`caliber/src/caliber/workflows/demo_tools.py`), restart the backend, then
register it as in (A). Keep it deterministic and side-effect-typed.

**C. Inline `python_code` node.** Reserve this for bounded custom logic outside
the Data Transform vocabulary. Decision tables, JSON Schema, field mapping,
confidence, and fixtures should use the visual component so the policy remains
structured and inspectable.

---

## 4. Workflow templates (exact ids)

`GET /workflow-templates` returns these (use the matching `data-testid="template-<id>"`):

`single_agent`, `multi_agent_handoff`, `guarded_pipeline`, `parallel_fanout`,
`hitl_review`, `for_each_loop`, `refinement_loop`, `knowledge_rag`,
`graph_hybrid_rag`, `knowledge_age`, `knowledge_age_build`, `event_resume`,
`blank`.

Scenario → starting template:

- Approval / refund / incident → **`hitl_review`** (has the human-approval gate wired) then add nodes.
- KB-grounded answer → **`knowledge_rag`** or **`graph_hybrid_rag`**.
- Event-driven pause/resume → **`event_resume`**.
- Anything bespoke → **`blank`** + drag nodes from the component catalog.

---

## 5. Recipe convention: running a scored evaluation

This is the canonical "did it pass?" loop used by SCN-01 and 10 (06 scores its retrieval inline on the KB Calibrate stage, and 07 and 08 check routing against a dataset by running the workflow through the queue):

1. **Create a dataset.** `POST /eval-datasets` → `{name, description}` (or
   **Prompt lifecycle → Test Sets → + New Test Set**). Add rows in the UI row editor at
   `/eval-datasets/:id → + Add example` (set `inputs` + `expectations`/`expected`
   directly), harvest from a trace via **Add example from trace** /
   `.../examples/from-trace`, or POST them with `POST /eval-datasets/{id}/examples`
   (`{inputs:{...}, expectations:{...}}`).
2. **Create judges if needed** (`Agent resources → Judges → New judge`): name, model,
   `instructions` referencing `{{ inputs }}` / `{{ outputs }}` / `{{ expectations }}`,
   pick `feedback_value_type`.
3. **Run it** (`Prompt lifecycle → Evaluations → Run evaluation`): pick the dataset,
   select scorers (deterministic) and/or `Judge.<name>`, run.
4. **Read the scorecard** (`/evaluations/{run_id}`): per-example pass/fail +
   overall. Pick a **baseline** run and compare deltas for regressions.
5. **Route hard cases to review** (`Operate → Review Queues`): enqueue the
   trace ids, answer the questions; answers write back onto the trace.
6. **Feed back**: add the failures to the dataset (`from-trace`) and re-run.

> Requires a configured model provider (`CALIBER_LLM_PROVIDER` / gateway). With
> no provider, Evaluations returns `400 evaluation requires a real LLM provider` —
> that is the expected "real-only" guard, and the labs verify it instead of a
> score on the default simulated stack.

---

## 6. How to read each scenario's files

- `scenario.yaml` / `build.yaml` / `verification.yaml` are the **contract /
  target** (intent, schemas, gates). They may name aspirational artifacts
  (deterministic judges, semantic span names) — see the corrections above.
- `README.md` is the **executable recipe**: exact UI navigation + field values
  + API fallbacks, a "what is simulated" statement, and the commands that were run
  (each lab has a runnable SDK example `sdk/caliber-sdk/examples/cookbooks/cookbook_NN_*.py`
  and a Playwright spec `caliber/caliber-ui/e2e/cookbooks/labs/NN-*.spec.ts`; the
  disposable environment they ran on is `scripts/cookbook-env.sh`). Where the
  contract and what ships differ, the lab README and the `feasibility:` block at
  the top of its `build.yaml` say so.

If a recipe step says "(API)", the UI for that exact step is thin or missing and
you should use the endpoint shown.
