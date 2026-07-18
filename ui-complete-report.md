# CALIBER cookbook UI-completeness review

Date: 2026-07-17  
Scope: all 16 cookbooks under `docs-site/cookbooks/`, their generated training
guide, the current React UI, backend routes, runtime, deployment image, and
targeted tests.

## Executive verdict

No: the cookbook pack is not yet honestly "fully implementable in the product
UI, with no code changes or backend access."

The product already has a broad and credible UI. Eight cookbooks can achieve
their core result through the shipped UI on the standard container stack. One
is mostly complete but has a package round-trip UX gap. Four Aria cookbooks can
produce their final artifacts only by skipping the Aria actions and rebuilding
the artifacts manually on other pages. Three cookbooks have hard out-of-band or
runtime blockers.

| Result | Cookbooks | Meaning |
| --- | --- | --- |
| UI-complete on the standard stack | 01, 03, 06, 07, 08, 09, 10, 16 | The core result can be authored, run, inspected, and governed in CALIBER. Some documentation is stale. |
| Mostly UI-complete | 02 | Skill development is complete, but the exact export/import equivalence gate is awkward and leaves the app. |
| Intended workflow is only partial | 12, 13, 14, 15 | The final artifacts can be built manually in the UI, but Aria does not create them. The training guide denies/skips every planned mutation. |
| Not UI-complete | 04, 05, 11 | Host-local document paths, missing `npx` in the runtime image, and an external release-signoff process break the strict promise. |

The direct product call is: **add features before continuing to market all 16
as UI-only**. The minimum release-blocking work is:

1. make Aria plans collect and execute valid structured inputs;
2. let document workflows consume uploaded/object-store files without a host
   absolute path;
3. make the GitHub MCP catalog entry runnable in the shipped container;
4. add an in-product release-signoff record and gate calculation; and
5. repair the cookbook sources so one generated truth matches current code.

## What “UI-complete” means in this review

A cookbook is UI-complete only when a normal user on the shipped stack can:

1. satisfy product-level prerequisites from the UI or from normal credentials
   supplied to the product;
2. create every required CALIBER artifact;
3. configure the real runtime inputs, not placeholders;
4. run the workflow or evaluation that the cookbook claims to demonstrate;
5. handle approvals, failures, retries, and background work;
6. evaluate the actual subject under test;
7. record the evidence and final gate in CALIBER; and
8. finish without API calls, terminal commands, editing deployment files, or
   manipulating a file that exists only on the application host.

Typing Python into a governed `python_code` workflow node counts as UI
authoring. Editing the repository, changing `.env`, running `make`, copying a
file into a container, or calling a route manually does not.

Normal external dependencies do not automatically fail the test. Supplying an
LLM or GitHub credential is reasonable. Requiring a binary that is absent from
the shipped image, or a file path that the UI cannot create, is a product gap.

## Per-cookbook assessment

| # | Cookbook | Verdict | Current UI path | Gap or correction |
| --- | --- | --- | --- | --- |
| 01 | Trustworthy Intake Classifier | **Complete; docs stale** | Prompt Author, live Playground, Test Sets, Runs, baseline diff, Calibration, Observability | The docs say standalone Evaluations cannot score a prompt. Current `EvalRunCreateRequest` and `Evaluations.tsx` support `predict_target=prompt`, `skill`, and `workflow`. The prompt workspace path remains valid, but the limitation is obsolete. |
| 02 | Precision Skills | **Mostly complete** | Skill wizard, Render Preview, Trigger Tests, Runs/baseline, Calibration, Bind, package preview/download/import | `Download ZIP` is on the standalone detail route, not the normal workspace. Import accepts a selected unpacked directory, not the downloaded ZIP. Re-importing into the same environment also needs an out-of-app frontmatter rename to avoid a duplicate-name conflict. The generated training steps avoid the promised import-equivalence gate entirely. |
| 03 | Policy-Safe Decision Tool | **Complete on shipped compose stack** | Tool wizard, sandbox, fixtures, calibration, baseline, workflow `python_code`, HITL run monitor | The standard container enables queue/checkpoint/approval flags, so the example works there. Native defaults are off and Settings is read-only, so a non-container install needs deployment work. The README still shows invalid `side_effect`; the accepted field is `side_effect_level`. Arbitrary new registered tools still require importable backend Python. |
| 04 | Document-to-JSON Pipeline | **Blocked** | Object Store upload/preview/extract, tool wizard, workflow editor, Python validation | The workflow tool `extract_document(ref)` requires `Path(ref).is_file()` on the CALIBER host. Object Store upload does not produce that host path, and the input-bucket node decodes objects as text rather than handing a binary/file reference to the extractor. The training guide explicitly requires an absolute host path or `mc cp`. This is not UI-only and is especially broken in the container, where host cookbook assets are not mounted into `/app`. |
| 05 | Governed Tool Connectivity (MCP) | **Blocked in shipped container** | MCP catalog, connection test, discovery, playground, policy, test cases, calibration | The GitHub tile launches `npx -y @modelcontextprotocol/server-github`. `deploy/caliber/Dockerfile` uses Node only in the UI build stage; the final `python:3.12-slim` runtime has no Node/npm/npx. The catalog advertises a command the standard runtime cannot execute. Remote MCP URLs and first-party Python DB servers are viable, but they do not make this GitHub cookbook work as written. |
| 06 | Grounded Knowledge Assistant | **Complete with normal provider/storage prerequisites** | KB create/build/explore/query/graph/calibrate, AGE sync, workflow editor, review queue | The UI covers the lifecycle. AGE remains an explicit sync and provider/local-embedding readiness must be visible. These are acceptable deployed-service prerequisites, not authoring gaps. |
| 07 | Support Triage Copilot | **Complete on shipped compose stack** | Prompt/skill/tool/MCP/KB surfaces, workflow editor, router and approval gate, run monitor, evaluations/review | This is a large manual build, but the required primitives exist. It depends on the approval runtime being enabled and on whichever MCP path is selected actually being runnable. Use shipped Python tools or omit the broken GitHub `npx` path. |
| 08 | Incident Response Copilot | **Complete; evaluation docs stale** | Prompt/skills, UI-authored Python fixture nodes, router/HITL workflow, workflow-target Evaluations, review queue | The training guide says Evaluations scores only a generic model completion and not the workflow. Current backend and UI support a real workflow target, with a lower synchronous example cap. The cookbook should use that instead of teaching a workaround. |
| 09 | Self-Healing Workflows | **Complete as an operator recovery lab** | Run monitor, checkpoints, recovery/debugger panels, retry, approve/reject/resume, manual manifest edit, preview and publish | The loop is UI-complete, but “self-healing” is overstated: the patch is human-authored. A patch-proposal endpoint/client exists, yet the UI only lists existing patches and offers no create/apply flow. This is a product-positioning gap, not a blocker for the documented manual lab. |
| 10 | Trustworthy Evaluation | **Complete; cookbook workaround is stale** | Test Sets, custom Judges, Evaluations, Review Queues, Judges → Human alignment | The cookbook sends users to an outside alignment worksheet. The shipped Judges page already accepts human-labeled examples and computes agreement rate, Cohen’s kappa, false positives, and false negatives. It is UI-complete, though it does not yet ingest completed queue items automatically. |
| 11 | Release Signoff Factory | **Blocked** | Read-only Releases live board/timeline plus separate evidence pages | There is no release-signoff entity, evidence manifest editor, gate calculator, waiver log, or decision record. The training flow opens files outside the app, runs `make allure-report`, computes the rubric outside the app, and writes the decision outside the app. `Releases.tsx` only aggregates current live artifacts and audit events. |
| 12 | Aria Evaluation Harness | **Partial; advertised behavior fails** | Plans can decompose/approve/execute; Judges and Test Sets can be created manually | `HeuristicPlanner` creates steps with empty `inputs`. The interaction schema accepts only `approved`, `choice`, and `value`, and the executor records those values without merging them into step inputs. Approving `judge.create` or `eval_dataset.create` therefore fails validation. The training guide tells the user to deny both steps, then create both artifacts manually. |
| 13 | Aria Review Governance Queue | **Partial; advertised behavior fails** | Same Plan shell; Review Queues page can create/enqueue manually | Aria proposes both queue capabilities but cannot supply queue schema or trace IDs. The plan is completed by skipping actions; the user performs the real work elsewhere. |
| 14 | Aria Governance Starter Kit | **Partial; advertised behavior fails** | Same Plan shell; Judges/Test Sets/Review Queues work manually | The flagship “one sentence → whole kit” claim is false today. All mutation steps are denied/skipped and all three artifacts are manually rebuilt. The plan is a keyword-derived checklist, not the executor of record. |
| 15 | Aria Triage & Recalibrate Loop | **Partial; advertised behavior fails** | Same Plan shell; queue and workflow calibration can be driven manually | The real calibration job can be started and observed from Workflow detail, but the Aria plan skips queue creation, enqueue, and calibration. Consequently the claimed `waiting_job`/poll/resume behavior is not demonstrated by the cookbook path. |
| 16 | Production Observability & Triage | **Complete for an existing runtime** | Trace filtering/detail, Add to test set, Review Queue enqueue/answers, Evaluation rerun | This is correctly described as operations rather than authoring. Its dependency on pre-existing runs is inherent and clearly stated. |

## Load-bearing findings

### P0 — Aria plans cannot execute the examples they plan

This is the largest credibility gap because four cookbooks are sold as the
“Aria — Autonomous” track.

Current behavior:

- `caliber/src/caliber/assistant/plans.py` installs `HeuristicPlanner` as the
  default and emits `PlannedStep` objects without inputs.
- The heuristic selects every non-read capability whose domain word appears in
  the goal. It does not reason about parameters, dependencies, or outputs.
- `AriaInteractionAnswerRequest` forbids extra fields and carries only
  `approved`, `choice`, and `value`.
- `PlanExecutor.answer()` stores the response on the interaction, but never
  patches `CaliberAriaPlanStep.inputs`.
- `AriaPlans.tsx` renders approve/deny or predeclared choices; it has no
  capability-specific parameter form.
- The generated steps for 12–15 explicitly warn that Approve will fail, tell
  the user to click Deny on every action, and then build the artifacts manually.

Required feature:

- introduce a real planner that returns typed inputs and dependency/output
  references, or add an explicit structured-input collection stage;
- render fields from each capability’s input schema in Plans;
- validate every step before plan approval;
- allow a draft step’s inputs to be edited and persisted;
- resolve outputs such as `queue_id` into downstream steps;
- keep current risk-tier gates and separation-of-duties behavior; and
- show created artifact IDs as step results with deep links.

Acceptance test: cookbooks 12–15 must run with zero skipped mutation steps.
Cookbook 15 must visibly park on the real calibration job, poll it, and resume
the same plan to completion.

### P0 — object upload and workflow document ingestion are disconnected

CALIBER has two individually good features that do not compose:

- Object Store can upload and extract Office documents through its backend
  endpoint.
- The workflow `extract_document` tool supports PDF/DOCX/PPTX/XLSX/OCR.

The workflow tool accepts only a host-local filesystem path. The input-bucket
node returns decoded text plus metadata; it does not provide binary bytes, a
staged local path, or an object-store reference that `extract_document` can
open. Therefore uploading a DOCX in the UI cannot feed that DOCX into the
document workflow.

Required feature:

- define a first-class file reference understood by Object Store, run files,
  workflow ports, tools, and attachments;
- support at least `object-store://bucket/key` and workflow-file IDs;
- stage bytes safely for parsers when a callable requires a local file;
- add a file/object picker to the workflow run form and tool sandbox; and
- preserve filename, media type, checksum, size, and lineage in the trace.

Acceptance test: upload a DOCX to Object Store, select it in the workflow run
form, and complete cookbook 04 without exposing or typing any host path.

### P0 — the MCP catalog is not deploy-image aware

The catalog offers Node-based `npx` servers, but the runtime image contains no
Node executable. A UI tile should not be considered supported merely because it
can persist a command string.

Required feature:

- either bundle a pinned Node runtime and pinned server packages, run catalog
  servers in managed sidecars, or replace the GitHub entry with a supported
  remote transport;
- preflight command availability, version, network, and required secrets before
  saving;
- show “unsupported in this deployment” instead of allowing a guaranteed
  connection failure; and
- store credentials through secret references rather than ordinary persisted
  environment values.

Acceptance test: on the exact `deploy/caliber/Dockerfile` image, click the
GitHub catalog tile, supply a secret, test the connection, discover tools, and
run the cookbook’s policy/calibration path.

### P0 — release signoff has no product record

Cookbook 11 is a checklist spread across Evaluations, Review Queues,
Observability, Allure, files outside the application, and hand arithmetic. The
current Releases page is observational, not a signoff workspace.

Required feature:

- a durable release-candidate/signoff entity;
- selectors for evaluation runs, workflow runs, review queues, and evidence;
- freshness and completeness checks;
- a versioned weighted gate rubric with an automatically computed verdict;
- blockers, owners, waivers, expiry, comments, and approval history;
- a signed go/no-go decision record with an export; and
- Allure/CI status ingestion. CALIBER should consume a published report/status,
  not run an arbitrary local shell command from the browser.

Acceptance test: complete cookbook 11 and produce an audit-ready decision
record without opening or editing a local manifest, using a calculator, or
running `make allure-report`.

## P1 product improvements

### Add a Cookbook Runner / bundle importer

Today each lab requires many manual copy/paste operations and raw IDs. The
cookbook assets are documentation contracts, not executable product bundles.
A guided runner should:

- discover the 16 installed cookbooks in the UI;
- run a deployment readiness/preflight check;
- show prerequisites, estimated time, current step, and evidence still missing;
- instantiate supported prompts, skills, tools, datasets, judges, queues, and
  workflow manifests with a dry-run diff;
- request credentials and approvals at the correct step;
- deep-link to the actual editor rather than replacing it; and
- capture run IDs and gates automatically into a final completion report.

This is the highest-leverage usability feature after the P0 runtime fixes. It
also provides an executable contract that can be tested in CI.

### Remove raw-ID and one-row-at-a-time friction

- Replace the Evaluations `subject_ref` text box with searchable prompt, skill,
  and workflow-version pickers.
- Add JSONL/CSV/YAML preview-and-import to Test Sets.
- Let Review Queues select traces from Observability instead of copying IDs.
- Let Judge alignment import completed queue labels and evaluation predictions.
- Let skill import accept the ZIP that skill export creates and provide
  `replace`, `new version`, and `import as <name>` conflict choices.

### Make “self-healing” an actual proposal workflow

Cookbook 09 is a good debugger/recovery lab, but it is not self-healing. Wire a
safe UI around the existing patch proposal surface:

- propose a patch from a failed run;
- show manifest/graph diff and evidence;
- validate on a pinned regression set;
- require explicit apply/publish approval; and
- preserve rollback and patch lineage.

### Decide the boundary for custom tools

The exact tool cookbook works by registering shipped functions, but a developer
cannot create an arbitrary reusable Tool entirely in the UI. CALIBER should
make one of two positions explicit:

1. Tools are deployment extensions: provide an SDK/CI packaging flow and do not
   market tool development as UI-only; or
2. Tools are UI-authored: add a governed code/function artifact with sandboxed
   build, dependency policy, tests, review, versioning, and publish.

Do not add a casual inline-Python text box to the registry without packaging and
security controls. Workflow `python_code` is appropriate for bounded workflow
logic, not a substitute for a reusable integration lifecycle.

## Documentation and product-contract drift

The code is ahead of several cookbook claims, while other cookbook claims are
ahead of the code. This makes the existing `FEASIBILITY.md` and
`CRITIQUE-REPORT.md` unsafe as “ground truth.”

Confirmed drift:

- `docs-site/cookbooks/README.md` says Prompt Playground is render-only; the
  current prompt workspace uses a real assistant/model chat session.
- The same file says the Test Set detail route is not wired and skill import is
  API-only. Both UIs exist.
- Cookbook 01 says Evaluations cannot score prompts. It can score prompt and
  skill targets.
- Cookbook 08 says Evaluations cannot run workflows. It can compile and run a
  workflow version in preview mode with a bounded example count.
- Cookbook 10 requires an outside alignment worksheet. Human alignment,
  agreement, kappa, FP, and FN already ship in Judges.
- `FEASIBILITY.md` calls Aria planning “real LLM-backed,” while the installed
  default is `HeuristicPlanner` and no LLM planner is wired into `PlanService`.
- `CRITIQUE-REPORT.md` says all findings are fixed and reviews only the 15
  buildable scenario packs (cookbook 16 ships as a training-only guide with no
  `scenario.yaml`), so it misses current gaps such as the host-path document
  workflow and the absent `npx` runtime.
- Cookbook 03’s README still documents `side_effect` although the Pydantic
  request forbids that field; the accepted name is `side_effect_level`.
- The global training intro promises no terminal/outside work, while cookbook
  11 explicitly contains one terminal step and three outside-app steps.
- The cookbook README links to `training/index.html`, which does not exist. The
  generated index is `docs-site/m-16-cookbooks.html`.
- The generated training pages, per-cookbook README files, `content.py`, and
  `training-steps.json` overrides can each describe different behavior.

Required fix:

- define one structured source of truth per cookbook;
- generate README/training HTML/navigation from it;
- attach capability/version requirements to each step;
- validate route names, field names, UI labels, referenced assets, and feature
  flags in CI;
- run each UI-only recipe as a Playwright contract against the shipped image;
- forbid the “UI-only” badge when a step is marked terminal, outside-app,
  host-path, API-only, or skipped; and
- make `CRITIQUE-REPORT.md` generated evidence, not a manually aging assertion.

## What should not be rebuilt

The review found several strong surfaces that already solve the problem and
only need cookbook updates or small integration work:

- model-backed Prompt Playground;
- prompt Test Sets/Runs with baseline regression diff;
- artifact-target Evaluations for prompt, skill, and workflow;
- full Test Set row editor and add-from-trace path;
- Judge playground and human-alignment metrics;
- skill author/render/trigger/run/calibrate/bind and package preview;
- tool sandbox, fixtures, deterministic calibration, baseline, and governance;
- workflow visual/code editing, Python Code nodes, preview, publish, run monitor,
  approvals, checkpoints, retry/resume, debugger, and trace comparison;
- Review Queue creation, enqueue, answer, and trace write-back;
- KB build/query/graph/calibration/AGE sync; and
- Observability trace tree, status filtering, comparison, and test-set capture.

## Recommended implementation sequence

| Phase | Work | Why first | Exit criterion |
| --- | --- | --- | --- |
| 0 | Remove or qualify the global UI-only/autonomous claims; fix the broken guide link and stale capability statements | Prevents training users from hitting known false paths | Every cookbook has an honest status badge and current navigation |
| 1 | Object/file reference unification and MCP runtime preflight/support | Repairs two concrete build labs on the shipped stack | 04 and 05 pass containerized UI E2E |
| 2 | Aria typed planning inputs, dependency outputs, and plan parameter UI | Repairs four flagship scenarios | 12–15 execute real capabilities with no skipped mutations |
| 3 | Release signoff workspace and CI/Allure evidence ingestion | Removes all out-of-app work from 11 | Durable computed and approved decision record |
| 4 | Cookbook Runner, bulk imports, selectors, package clone UX, integrated alignment | Makes the pack usable rather than merely possible | A new user completes the full ladder without copying raw IDs or editing local files |
| 5 | Safe patch-proposal/apply workflow and explicit reusable-tool authoring boundary | Aligns “self-healing” and “developer platform” positioning | Failed-run-to-validated-patch flow is governed and reversible |

## Verification performed

Static and contract review covered:

- all 16 scenario directories and their README/build/test/verification/training
  files;
- `docs-site/cookbooks/{README,FEASIBILITY,CRITIQUE-REPORT,ARIA-AUTONOMY}.md`;
- generated cookbook HTML claims and the training generator/overrides;
- React routes and the relevant Prompts, Skills, Tools, Workflows, MCP, Object
  Store, Knowledge, Evaluations, Judges, Review Queues, Aria, Releases,
  Observability, and Settings surfaces;
- backend schemas/routes for the same areas;
- workflow runtime file/bucket behavior;
- Aria planner/executor/interaction behavior; and
- the shipped Dockerfile and Compose defaults.

Machine checks completed:

- 60 YAML, 51 JSON, and 13 JSONL cookbook files parsed successfully;
- frontend targeted suite: **47 passed** across Evaluations, Judges, Aria Plans,
  and Object Store; and
- backend targeted suite: **83 passed** across evaluation routes, judge routes,
  Aria planning/execution, and workflow bucket nodes.

These tests prove the inspected contracts, including features that the cookbook
docs incorrectly call missing. They do not prove a live GitHub MCP connection or
a provider-backed end-to-end cookbook run. Those need containerized Playwright
recipe tests after the blockers above are fixed.

## Final answer

CALIBER is already capable of UI-first development, but the cookbook pack is
not yet UI-complete as a whole. The issue is not a lack of general CRUD pages;
it is failure at the seams between those pages: file references, managed MCP
runtimes, typed Aria execution, release evidence, and a single executable
cookbook contract.

Until the four P0 product gaps are closed, market the collection as:

> UI-first cookbook pack: 8 complete labs, 1 mostly complete lab, 4
> operator-assisted Aria labs, and 3 labs requiring platform work.

After those gaps and the documentation generator are fixed, the stronger
“all 16 are UI-only” claim becomes defensible.
