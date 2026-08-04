# CALIBER Scenario Pack — Feasibility & Recipe Conventions

This document is the **ground truth** behind every scenario recipe. It records,
from a direct read of the codebase (frontend `caliber/caliber-ui/src`, backend
`caliber/src/caliber`), exactly what CALIBER can do **today**, what is
environment-gated, and what remains deliberately bounded. The runtime-owned
catalog is exported to [`capabilities.json`](capabilities.json); that generated
inventory, rather than this prose, is the authoritative recipe list.

API base for all endpoints: `/ajax-api/2.0/mlflow/caliber`.
SPA routes are under `/caliber`. Dev sign-in is `admin` / `admin`.

---

## 1. Capability matrix (verified)

| Area | Capability | Status | Notes / where |
| --- | --- | --- | --- |
| **Prompts** | Author + version (MLflow Prompt Registry) | ✅ Works | `POST /prompts`, `POST /prompts/{name}/versions`. Active alias is `prod` only. |
| | Playground | ✅ Live chat | The Playground tab runs a **real model chat** using the prompt (`createAssistantSession` / `sendAssistantMessage`). `POST /prompts/{name}/test-render` is the separate variable-only render. For *persisted, scored* runs use the prompt **Runs** stage or **Evaluations**. |
| | Test Sets / Runs / baseline | ✅ Works | `POST /prompts/test-runs` persists a run; `POST /prompts/{name}/baseline` pins it. |
| | Calibration (optimize) | ⚠️ Queued | `POST /prompts/optimization/runs` enqueues a background refinement job (MetaPrompt/GEPA). It does **not** finish inside the request; you see a queued job, not an instant result. |
| | Bind | ✅ Works | `POST /prompts/{name}/bind` (agent / workflow_node / standalone). Workflow-manifest rewrite is deferred to deploy. |
| **Tools** | Register from importable Python | ✅ Works | `POST /tools` requires `module_path` + `callable_name` that the backend can import. **No inline-code authoring** in the registry. See §3. |
| | Sandbox test-run | ✅ Works | `POST /tools/{id}/test-run` imports and runs the real callable; `write`/`external_action` tools are **mocked**, `read` tools run live when `allow_in_preview`. |
| | Fixtures + deterministic hardening | ✅ Works (inline) | `PUT /tools/{id}/test-cases`, `POST /tools/{id}/calibrate` run saved cases through the sandbox and score assertions inline. |
| | LLM-judge hardening lane | ⚠️ Queued | Durable run row is created; judge execution is deferred to background jobs. |
| | Baseline / Publish | ✅ Works | `POST /tools/{id}/baseline`; deprecate/archive via `PATCH`/`DELETE` (blocked if a `prod` workflow references the tool). |
| **Skills** | Author + version | ✅ Works | `POST /skills`, `PATCH /skills/{id}` (content edit bumps version). Reserved prefixes `claude*`/`anthropic*` rejected. |
| | Render Preview | ✅ Works | `POST /skills/{id}/test-render` substitutes `{{vars}}`, reports unresolved. |
| | Trigger / selection tests | ✅ Works (deterministic) | `POST /skills/{id}/test-selection` uses a **deterministic** scorer (no LLM): `is_selected`, `selection_score`, `selection_reason`. |
| | Package export / import | ✅ Works | Export a ZIP and upload it directly in the Skill UI. Import is bounded and supports explicit reject, rename, or admin-only versioned merge. |
| | Calibration | ⚠️ Queued | `POST /skills/{id}/calibrate` enqueues a background job (same model as prompts). |
| | Scenario Sets | ⚠️ Scaffolded | UI present; rich scenario authoring is minimal today — use Trigger Tests + Runs. |
| **Workflows** | Cookbook system examples | ✅ Works (16) | **Cookbooks** installs any runtime-owned recipe atomically as a paused workflow and draft version after readiness review. |
| | Code-free deterministic transforms | ✅ Works | `data_transform` supports fixture, mapping, Draft 2020-12 JSON Schema, ordered decision tables, and weighted confidence. |
| | Review Queue enqueue | ✅ Works | `review_queue_enqueue` creates audited, idempotent trace-linked items and fails closed when the runtime binding is absent. |
| | Run monitor: execute/approve/reject/retry/resume | ✅ Works | `POST /workflow-runs`, `.../approval/approve`, `.../approval/reject`, `.../retry`, `.../resume`, `.../resume-by-event`. Checkpoints, retry lineage ("Attempt N of M"), recovery panel, debugger panel all real. |
| | HITL approval gate | ✅ Works | `human_approval` node → status `waiting_approval` → approve → `resuming`. RBAC role + multi-sig (`approval_count`) + `timeout_behavior`. |
| | Edit/patch workflow | ⚠️ Manual only | Edit the manifest in the editor and **save a new version**. A semantic patch API exists (`workflows/patch.py`) but is **not wired to the UI**; there is **no** `propose_workflow_patch` tool. |
| **Aria / Aria Plans** | Plan decompose → approve → execute | ⚠️ Deterministic planner | Plan **decomposition** is a deterministic `HeuristicPlanner` (keyword→capability), **not** LLM-backed — it emits steps with empty `inputs`, so auto-approving a planned mutation fails validation (drive each artifact via its own route; see ARIA-AUTONOMY + scenarios 12–15). The LLM engines (OpenAI/Anthropic + `fake` double) back Aria's conversational **assistant**, not the planner; no LLM planner is wired into `PlanService`. `POST /aria/plans`, `/approve`, `/execute`, `/poll`. |
| | Aria capability registry | ⚠️ Narrow | Limited to eval/governance scaffolding: `judge.create`, `eval_dataset.create`, `review_queue.create`, `review_queue.add_items`, `workflow.calibrate` (+ `judge.list`/`review_queue.list` reads). **No** skill/prompt/tool/workflow authoring. Aria **cannot** debug/patch a workflow as a capability — use the run-monitor surfaces. See [`ARIA-AUTONOMY.md`](ARIA-AUTONOMY.md) + scenarios 12–15. |
| **Evaluations** | Dataset + scorers → scorecard | ✅ Works (real LLM) | `POST /evaluations`; **no fake fallback** — without a configured provider it returns 400. Per-example detail + run-vs-run compare with deltas. |
| | Scorers available | ✅ Works | `exact_match`, `token_f1`, `contains_expected`, `non_empty`, plus `Judge.<name>` LLM judges. |
| | Add a trace to a dataset | ✅ Works | `POST /eval-datasets/{id}/examples/from-trace` (auto-extracts input/expected, tags lineage). |
| | Disagreement / low-confidence surfacing | ✅ Works | Evaluation deltas identify candidates; Human Alignment imports completed queue labels and computes agreement/kappa/confusion counts. |
| **Judges** | Create custom LLM judge | ✅ Works | `POST /judges`: name, model, `instructions` (must reference `{{ inputs }}` / `{{ outputs }}` / `{{ expectations }}`), `feedback_value_type` (bool/int/float/str). Executed via MLflow 3.14 `make_judge`. |
| | "deterministic" judge type | ❌ Not a judge | There is **only** the custom LLM judge type. Deterministic checks are **scorers** (`exact_match`, …) or **tool/skill assertions**, not a judge. Treat scenario `type: deterministic` as "use a deterministic scorer/assertion." |
| | Human alignment | ✅ Works | The Judges page computes agreement, Cohen's κ, and FP/FN and can import completed pass/fail Review Queue labels with queue/item/trace/question/reviewer provenance. |
| **Test Sets** | List `/eval-datasets` | ✅ Works | Create dataset, add rows / from-trace / MLflow sync. |
| | Detail `/eval-datasets/:id` | ✅ Works | `EvalDatasetDetail.tsx` is a full row editor: **+ Add example** (set `inputs` + `expectations`/`expected` directly), edit/revise, retire/supersede, **Add example from trace**, version filter. Backend: `POST …/examples`, `…/revise`, `…/supersede`, `…/examples/from-trace`. |
| **Review Queues** | Enqueue trace → answer → write back to trace | ✅ Works | `POST /review-queues`, `.../items` (trace_ids), submit answers → MLflow assessments/expectations on the trace. Trace-linked. |
| **Observability** | MLflow trace tree | ✅ Works | Read via `mlflow.get_trace`; span tree + timeline + token/cost attrs in the Observability page. |
| | Named semantic spans (`prompt_render`, `tool_lookup`, `retrieval_query`, `approval_gate`, `mcp_playground_invoke`, …) | ❌ Aspirational | Those exact span names are **not** emitted. Spans are named by workflow **node id** / generic operation. Treat every `monitoring.traces:` list in `verification.yaml` as "evidence to look for in the trace tree," **not** literal span names. |
| **MCP Servers** | Quick-connect catalog | ✅ Works (8) | GitHub, PostgreSQL, pgvector, Apache AGE, MinIO, Hugging Face, Ollama, Playwright. `data-testid="catalog-<id>"`. |
| | Test connection + discovery | ✅ Works | `POST /mcp-servers/{id}/test-connection`, `/discover-tools`, `GET /mcp-servers/{id}/tools` (tools + effective policy). |
| | Playground invoke | ✅ Works | `POST /mcp-servers/{id}/invoke-tool` (10s timeout; structured error on failure/blocked). |
| | Policy overlay | ✅ Works | Per-tool `allowed` (block), `side_effect_level` (`read`/`write`), `requires_approval`. `PATCH /mcp-servers/{id}/tools/{tool}/policy`. Enforced on invoke. |
| | Tool calibration panel | ✅ Works | `PUT .../tools/{tool}/test-cases`, `POST .../tools/{tool}/calibrate`. |
| **Knowledge Bases** | Build / Explore / Calibrate / Use | ✅ Works | Explore sub-views: **Query** (ask), **Chunks**, **Graph**. Use stage is "query via API" docs (not an ask box). |
| | Retrieval modes | ✅ Works | `dense`, `hybrid`, `graph_hybrid`, `age_graph` (AGE only when enabled). |
| | Calibrate metrics (inline) | ✅ Works | Recall@k, nDCG@k, Faithfulness, Answer-correctness — computed synchronously. |
| | AGE sync | ✅ Works (manual) | Build does **not** auto-sync; click **Sync to AGE** (`POST /knowledge-base-versions/{id}/age-sync`) then the version is graph-served. |
| **Object Store** | Upload / list / preview / extract / delete | ✅ Works (S3/MinIO) | Extract supports `.docx`/`.pptx`/`.xlsx` (not legacy `.doc/.ppt/.xls`). |
| **Releases** | Candidate / waiver / signoff | ✅ Works | Releases persists weighted criteria, evidence, blockers, admin waivers, rollback targets, and immutable go/no-go snapshots. |
| **Allure** | Release evidence job | ✅ Works | An authorized operator generates and monitors a durable in-product Allure-compatible JSON report job. The separate static HTML test report remains a CI publication artifact. |

---

## 2. The five things that most often trip up a recipe

1. **Prompt Playground is live chat, not a scored regression suite.** It calls
   the configured model. Persisted scored evidence belongs in prompt Runs or
   Evaluations over a Test Set.
2. **"Deterministic judge" is not a thing.** Use a deterministic **scorer**
   (`exact_match`, `token_f1`, `contains_expected`, `non_empty`) in Evaluations,
   or a **tool/skill assertion** in Hardening/Trigger Tests. Reserve **Judges**
   for LLM-graded criteria (faithfulness, tone, escalation correctness).
3. **Use Data Transform before custom code.** Mapping, schema validation,
   decision rules, confidence, and fixtures are visual components. A registered
   custom callable still must be importable Python.
4. **Calibration (prompts & skills) is queued, not instant.** It creates a
   background refinement job. For a live demo, show the queued job + its id; do
   not wait for an inline score. **KB calibration and tool/MCP deterministic
   calibration *are* inline** and return immediately.
5. **Span names in `verification.yaml` are labels, not literals.** Real traces
   exist (MLflow), but spans are named by workflow node id. In Observability,
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

This is the canonical "did it pass?" loop used by SCN-01, 06, 07, 08, 10:

1. **Create a dataset.** `POST /eval-datasets` → `{name, description}` (or
   **Evaluate → Test Sets → + New Test Set**). Add rows in the UI row editor at
   `/eval-datasets/:id → + Add example` (set `inputs` + `expectations`/`expected`
   directly), harvest from a trace via **Add example from trace** /
   `.../examples/from-trace`, or POST them with `POST /eval-datasets/{id}/examples`
   (`{inputs:{...}, expectations:{...}}`).
2. **Create judges if needed** (`Evaluate → Judges → New judge`): name, model,
   `instructions` referencing `{{ inputs }}` / `{{ outputs }}` / `{{ expectations }}`,
   pick `feedback_value_type`.
3. **Run it** (`Evaluate → Evaluations → Run evaluation`): pick the dataset,
   select scorers (deterministic) and/or `Judge.<name>`, run.
4. **Read the scorecard** (`/evaluations/{run_id}`): per-example pass/fail +
   overall. Pick a **baseline** run and compare deltas for regressions.
5. **Route hard cases to review** (`Observe → Review Queues`): enqueue the
   trace ids, answer the questions; answers write back onto the trace.
6. **Feed back**: add the failures to the dataset (`from-trace`) and re-run.

> Requires a configured model provider (`CALIBER_LLM_PROVIDER` / gateway). With
> no provider, Evaluations returns 400 — that is the expected "real-only" guard.

---

## 6. How to read each scenario's files

- `scenario.yaml` / `build.yaml` / `verification.yaml` are the **contract /
  target** (intent, schemas, gates). They may name aspirational artifacts
  (deterministic judges, semantic span names) — see the corrections above.
- `README.md` is the **executable recipe**: exact UI navigation + field values
  + API fallbacks, plus a per-scenario **Feasibility & substitutions** callout
  that reconciles the contract with what ships today.

If a recipe step says "(API)", the UI for that exact step is thin or missing and
you should use the endpoint shown.
