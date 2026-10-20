# First-user verification of the cookbooks

Verified on 2026-10-19 against the code of this branch. Every lab was run the way a new user meets it: a fresh
stack (`scripts/cookbook-env.sh`, new database, new MLflow, new browser context), the lab's own README
instructions, real clicks in Chromium at 1280×720, and no API call, database edit or injected state to get past a
step. The lab's SDK script (its API path) was run on the same fresh stack as well.

Status words: **PASS** the documented journey was executed and its results checked; **FAIL** executed, a required
behaviour is still wrong; **BLOCKED** cannot be completed.

## Matrix

"Simulated" means the alpha's default simulated model and evaluator, which every lab README is written for; it
refuses to score a prompt or a judge (`400 evaluation requires a real LLM provider`), and each README says so. The
scored paths were run on the **stand-in** model tier (labs 01 and 10) and on a **hosted** model (labs 01 and 17).

| Cookbook | UI flow | Assets created in the UI | Execution | Evaluation / judge | Versioning | Results verified | E2E test | Issues found | Issues fixed | Final status |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| 01 Intake classifier | Prompts → Test Sets → Evaluations → Calibration → Releases → Audit Log | prompt, 44-case test set (file import), eval runs, calibration candidate | simulated, stand-in and **hosted** | scored on stand-in and hosted; refused on simulated, as documented | v1, v2 draft, applied v3, rollback to v1; registry alias checked | live board, timeline, audit log, registry | `01`, `01b`, `01c` | 0 new | n/a | **PASS** |
| 02 Precision skills | Skills → scenario sets → Calibrate → package | workspace, skill, scenario sets, package | simulated | calibration simulated (a skill has no test set) | baseline pin, apply, rollback | trigger results, export, archive | `02` | 0 new | n/a | **PASS** |
| 03 Policy-safe tool | Tools → workspace (Sandbox, Fixtures, Hardening, Test Runs, Publish) → Workflows → Approvals | 2 tool names, one with v1.0 and v1.1; 49 fixtures (file load), workflow, second person | no model: real | 49-case suite, baseline and a regression drill; suite repeated on v1.1 | baseline pin, compare, New version | pass rates, version history, approvals, audit log | `03` | 3 | 3 | **PASS** |
| 04 Document extraction | Administration → Object Store → Cookbooks → Workflows | reviewer, workspace, 2 buckets, 30 invoices, workflow | no model: real | JSON Schema check node | v1 published, v2 saved, re-run on v2, compare | output bucket, approvals | `04` | 0 new | n/a | **PASS** |
| 05 MCP governance | MCP Servers → Playground → Workflows | server, 6 tool policies, calibration cases, workflow | no model: real MCP server | calibration suite (pass and a failing case) | workflow v1 published, v2 restored, loosened, refused by the policy gate | server ledger, audit log, traces | `05` | 5 | 5 | **PASS** |
| 06 Knowledge Q&A | Object Store → Knowledge Bases → Workflows → Review Queues | documents, KB v1 and v2, answer prompt, workflow | simulated answers, real retrieval | model-judged scores simulated | KB v1, tuned v2, active version | chunks, rankings, trace in queue | `06` | 0 new | n/a | **PASS** |
| 07 Support triage | Administration → Test Sets → Workflows → Runs | users, 44-ticket test set, workflow | rules, simulated draft node | n/a | publish, modify, v2, re-run, compare, rollback | approvals, audit log | `07` | 0 new | n/a | **PASS** |
| 08 Incident commander | same shape as 07 | users, test set, workflow, agent | rules, simulated draft node | Evaluations and Calibrate | publish, modify, v2, re-run, compare, rollback | approvals, audit log | `08` | 0 new | n/a | **PASS** |
| 09 Workflow debugger | Workflows → Run Monitor → editor | workflow v1, patched nodes | real failures | n/a | v1 published, patch, v2, compare, promote | run status, node outputs | `09` | 1 | 1 | **PASS** |
| 10 Judge certification | Test Sets → Prompts → Judges → Evaluations → Review Queues | test set, prompts, judge, review queue | stand-in model | judge scored on stand-in; refused on simulated | prompt v2, re-run, compare | alignment, certify or reject | `10` | 0 new | n/a | **PASS** |
| 11 Release readiness | Releases → release candidates | candidate, waiver, signoff | no model | criteria, blockers, waivers | modify, new candidate, re-run | signoff log, viewer view | `11` | 0 new | n/a | **PASS** |
| 12 Aria evaluation harness | Plans → Judges → Test Sets → Evaluations | judge, 30-row test set (file import) via Aria | rules planner | judge listed; scoring refused on simulated | test-set row edit makes a new version | plan states, judge, test set | `12` | 1 | 1 | **PASS** (simulated, and on a hosted model; see note 2) |
| 13 Aria review queue | Plans → Review Queues | queue, 30 flagged items | rules planner | n/a | n/a (a queue is not versioned) | reviews, a review that does not fit | `13` | 0 new | n/a | **PASS** |
| 14 Aria starter kit | Plans → Judges, Review Queues, Test Sets | judge, queue, test set, rows | rules planner | judge listed; scoring refused on simulated | test-set rows add a version | plan states | `14` | 0 new | n/a | **PASS** (simulated, and on a hosted model; see note 2) |
| 15 Aria triage loop | Plans → Tools → Test Sets → Workflows → Agents | tools, test set, workflow, agent, queue, calibration job | rules planner, background worker | calibration job settles | workflow published | plan parks and settles | `15` | 1 | 1 | **PASS** |
| 16 Observability triage | Workflows → Observability → Test Sets → Review Queues | workflow v1 and v2, 40 traces, test set, queue | real traces | n/a (Evaluations not used) | v1, fix, v2, re-run 16 tickets, compare | 12 ERROR and 28 OK, then 44 OK | `16` | 3 | 3 | **PASS** |
| 17 Financial analysis | SDK only by design; the UI half inspects what the script made | project, bucket, objects, prompt (all by script) | **hosted model**, 31 s | the model's JSON is validated by the script | prompt v1 at `prod` | objects, prompt, Playground answer read in the UI | `17` | 2 | 2 | **PASS** (SDK by design) |
| 18 Projects, roles, tokens | Administration → Settings → Audit Log | 9 accounts, projects, roles, tokens | no model | n/a | n/a | 72-request role matrix, audit log | `18` | 0 new | n/a | **PASS** |

Notes:

1. "0 new" means no defect was found in this pass; the defects fixed by the earlier pass on this branch (file import
   of test sets and calibration cases, the trace in a review, release waiver expiry, the prompt Playground running the
   prompt, a publish refreshing the header) are listed under "Cross-cutting" and also have regression tests.
2. Labs 12 and 14 end in a judge that Aria created and that the UI lists; *scoring* that judge needs a real model and
   is refused on the simulated stack by design. Both were re-run on a **hosted model** (`gpt-5.6-luna`, fresh stack,
   `scripts/cookbook-env.sh up NAME --flags cookbooks,aria --real-llm`): every test of lab 12 (5 of 5) and lab 14 (4 of 4)
   passes, including the real scored evaluation of the judge and the baseline comparison. That run found two problems in
   the specs, not the product: the judge's score card was located by its label instead of the block holding the
   percentage, and the Ask Aria helper could lose its text to the panel's own reset between filling it and sending.
   Both are fixed. Long real-model runs are also sensitive to the machine sleeping (a multi-minute request is
   dropped with "Failed to fetch"): run them under `caffeinate -i` on a laptop. The stand-in tier (labs 01, 10) and
   labs 01 and 17 on a hosted model were verified as above.
3. Nothing is BLOCKED or FAIL. The cookbook-labs workflow on a clean GitHub runner is an independent run: its first pass found the lab 16 canvas defect above, which no local run could (macOS scrollbars take no space).

## Defects found by running the labs, and their fixes

Each has a regression test; the ones marked ✗→✓ were shown to fail without the fix.

| Where it was found | Root cause | Fix | Test |
| --- | --- | --- | --- |
| Lab 03 spec idled 16 minutes | Playwright's `actionTimeout` defaults to no limit; a click on a control that never appeared waited for the whole 20-minute test | `actionTimeout` 30 s and `navigationTimeout` 60 s in the lab and cookbook configs | the run that failed fast |
| Lab 05: Run Monitor covered the canvas; the specs clicked around it with `dispatchEvent` | the panel had a max height but no scrolling, and its rows shrank (the input was one line, output was clipped) | the panel scrolls, rows keep their height, a ✕ closes it | `workflow-editor-handlers` ✗→✓ |
| Lab 05: Publish and Run Monitor off screen at 1280 px | non-wrapping toolbar plus long name plus two status chips | toolbar wraps, chips bounded | `workflow-editor-handlers` ✗→✓, spec asserts `toBeInViewport` |
| Lab 05: stale "Published." chip on the new draft | chips of the version left behind were not cleared on restore | cleared on restore | `workflow-editor-handlers` ✗→✓ |
| Lab 05: no way to find a binding's Approval switch | it is in workflow settings; the Tool node said nothing | the node says where | `workflow-components` |
| Lab 05: README, catalog and SDK script lacked the versioning step | the spec had been extended, the docs not | step 9 added to the README, training steps, catalog and the script's count | step-count contract, API run |
| Lab 09: first node cropped behind the palette | graph wider than the canvas; React Flow's minimum zoom 0.5 stops the fit | minimum zoom 0.15; refit on resize until the person pans | `canvas` |
| Lab 16 on the clean GitHub runner (the local run passed): every canvas node stayed hidden | the refit added for lab 09 ran on every resize; on Linux, where scrollbars take layout width, each refit moved the graph, a scrollbar appeared or vanished, the canvas changed by ~15 px and requested the next refit (the Playwright trace showed ~100 flips between two transforms) | a change under 24 px is ignored and the view refits at most four times by itself | `canvas` ✗→✓ (scrollbar-sized jitter, cap) |
| Labs 04, 09, 16 on the GitHub runner, again, after the refit fix: nodes hidden and unclickable while a run polls | the canvas hands React Flow new node objects on every render (selection, run status and validation are layered on); without a size on them React Flow treats each as never measured, hides it (`visibility: hidden`) and re-fits when it re-measures. Locally during a run the nodes were hidden in 69 of 127 samples; a slow machine shows it as a graph that cannot be clicked | each node is handed back the size React Flow measured for it; and after every render React Flow is asked to measure any node it still holds no size for (on a slow machine its one-time observation can land before the store adopts the node, and the element's size never changes again, so it stays hidden although it is sized and on screen; reproduced locally with the browser's CPU throttled 6x: 196 of 196 samples hidden after Restore as draft, 0 of 185 after) | `canvas` ✗→✓; in the browser 0 of 161 samples hidden during a run, and throttled 6x 0 of 185 after a restore |
| Lab 12: catalog had 10 steps, README 12 | README edited, catalog not | catalog brought in step | step-count contract |
| Labs 03, 15: registering a tool lands on a read-only page; the next steps are in the workspace | two tool surfaces, no link between them | the detail page is gone: `/tools/:id` redirects to the workspace, registering lands in it, and the workspace is a URL (`?tool=`), so a reload and Back work | `tool-detail`, `tool-workspace` |
| Lab 16: Error filter showed 11 failures of 12 | the last run reads `IN_PROGRESS` for a moment; the old rule refreshed only while a *listed* trace was in progress, never true under a filter | the list refreshes every 5 s | `observability` ✗→✓ |
| Lab 16 spec: counted rows before the list settled | race in the spec | wait for the filtered list, then poll the count | the run |
| Lab 17: Playground reply not found | the locator matched the prompt's own text in a hidden panel | chat messages carry `data-testid` and `data-role`; the spec reads the assistant's | `prompts-advanced-flows` |
| Lab 17: SDK on a simulated model | would create assets then fail at the model | refuses first, with the fix | `test_cookbook_17_refuses_to_start_against_a_simulated_model` |
| Cross-cutting: empty Dashboard was a wall of zeros | no first-run guidance | "where to start" panel (links to Cookbooks when on) | `overview` |
| Cross-cutting: empty Workflows list pointed at "a template above" | templates open only after New Workflow | the message says so | `workflows` |
| Earlier pass, same branch | | file import for test sets and calibration cases; trace request and response in a review; waiver expiry and blockers on a release candidate; the prompt Playground runs the prompt; plan text inputs as textareas; `equals` accepts equivalent JSON; a binding with approval is a gate | their own tests in the PR |

## Using `dispatchEvent` and API setup in the specs

An audit of the lab specs this pass started from found 25 `dispatchEvent("click")` / `evaluate` workarounds. All were removed and the specs
run on real clicks. The Run Monitor, canvas-fit and toolbar defects above are what they were hiding. The only remaining
direct HTTP use is lab 04's cleanup of its own buckets after the journey, which is not a step of the lab.

## Product gaps found and not fixed

- ~~Cookbooks and Plans live under Integrations~~ **Fixed:** Cookbooks leads the sidebar under **Get started** (open by
  default), and Plans sits under **Operate** with the other things that wait for a person.
- ~~Two tool pages overlap~~ **Fixed:** a tool has one page, its workspace. Edit, version history and calibration jobs moved into its Publish stage, `/tools/:id` redirects there, and a **New version** action makes the next version of a tool (name fixed, version one on).
- **The Playground needs the Aria flag, by design, and now says so.** It chats through the Aria assistant, so a default install cannot use it. Its tab used to vanish without a word; it is now always listed and, with Aria off, says what it needs (`CALIBER_FEATURE_ARIA=true`) and points to **Evaluations** to run the prompt on real examples. Taking the chat out from under the Aria flag would put a model-calling endpoint outside it, which is a product decision, not a fix.
- **Scoring a judge or a prompt needs a real model.** Simulated results are labelled as such everywhere, by design.

## How the verification was kept resource-safe

- One stack, one browser, one worker at a time (`workers: 1`); labs run in batches of at most four, each on its own
  disposable stack that the runner stops and checks for leftover processes (none after any batch).
- No full Playwright suite was launched to check a single fix: a focused spec on a throwaway stack did that.
- The one stuck run (lab 03) was stopped, its stack taken down, and the cause removed (the action timeout).
- Frontend unit tests ran with at most 3 workers: 159 files, 2,542 tests, all passing.

## Reproduce

```bash
scripts/cookbook-run-all.sh --labs 01,02,03,04      # one stack at a time; UI and API for each
scripts/cookbook-run-all.sh --labs 01,10 --tier standin
scripts/cookbook-run-all.sh --ui-only --labs 05     # the browser path alone
```
