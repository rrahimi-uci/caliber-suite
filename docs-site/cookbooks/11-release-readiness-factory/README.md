# Release Signoff Factory: a record of the decision, not a deployment

> **Prerequisite flags:** `CALIBER_FEATURE_RELEASE_CANDIDATES=true` (the **Release signoff factory** section of Releases and the
> `/releases/candidates` API) and, for the gallery step, `CALIBER_FEATURE_COOKBOOKS=true`. Both are preview areas and are off by
> default. No model is involved anywhere in this lab, so nothing in it is simulated. Every step was run against a real CALIBER server
> (a fresh stack from `scripts/cookbook-env.sh up lab11 --flags release_candidates,cookbooks`) through the API and through the browser.

## What this lab demonstrates

A release candidate is a **durable record** of a go/no-go decision: which artifact and version, what its criteria scored, which blockers
stand, who waived what, and who signed. The server does the arithmetic and enforces the rules; people supply the scores.

**What it is not, stated plainly:**

- **Signing off deploys nothing.** A GO writes a signoff row and a frozen snapshot. It does not promote, alias, publish or roll back
  anything. The release path is **Apply** and **Rollback** on a prompt, a workflow's alias, and so on; the lab shows a signed GO beside
  an untouched prompt, then a separate **Promote** that actually moves the alias.
- **The scores are typed in.** CALIBER does not read them from Evaluations, Review Queues or traces. The `evidence` list is a list of
  references (an id and a label); CALIBER stores them and does not open them. A candidate whose artifact does not exist is accepted
  (the lab creates one on purpose).
- **The report is a snapshot.** **Generate Allure** produces Allure-compatible JSON with a SHA-256 of the candidate as it was at that
  moment; it is not a CI test report, and a report made before a waiver is stale once the candidate changes.

You create the 20 candidates in a dataset (clean releases, a blocker that outweighs a high score, a score just under the bar, exact
boundaries, one criterion and twelve), check the server's weighted score, status and blockers against figures worked out independently,
then run the governance on a few of them: waivers, the report job, go and no-go signoffs, and a signed candidate that can no longer
change.

**You are done when:**

- all 20 candidates are created and the server's weighted score, status and blockers match the dataset for every one (**14 ready, 6
  blocked**);
- a candidate is refused for a zero weight, a score above 1, a bad key, no criteria, a required score above 1 or an unknown field
  (`400`), and a person without the operator scope cannot create one (`403`); a candidate for an artifact that does not exist is
  **accepted**;
- a waiver needs the admin scope, a reason of at least 8 characters, an existing criterion key and an expiry in the future; it clears a
  blocker but not a low score (RC16 becomes `ready` at the same `0.721`; RC17 stays `blocked` after both its blockers are waived);
- a GO is refused on a blocked candidate (`409`), on a ready candidate with no planned action or rollback target (`409`) and by anyone
  without the admin scope (`403`); a NO-GO on a blocked candidate is recorded;
- a **signed candidate cannot be evaluated, waived or signed again** (`409`), and its signoff keeps the frozen snapshot;
- after a signed GO the prompt's aliases, the live board and the release timeline are unchanged; a separate **Promote** moves the alias
  and appears on the timeline;
- the audit log names who created, waived, reported and signed.

## Prerequisites and setup

1. A running CALIBER built per [`docs/start/alpha-install.md`](../../../docs/start/alpha-install.md) with the **release_candidates** flag
   on.
2. Two accounts: `lab11-operator` (listed in `CALIBER_OPERATOR_USERS`; may create, re-evaluate and generate reports) and `lab11-viewer`
   (no scope). Only `admin` holds the admin scope that waivers and signoffs need.
3. From a checkout, one command starts a disposable stack with all of that (this is what the lab was verified on):

   ```bash
   CALIBER_OPERATOR_USERS=admin,lab11-operator scripts/cookbook-env.sh up lab11 --flags release_candidates,cookbooks
   source .tmp/cookbook-env/lab11/env.sh
   curl -s $CALIBER_API/readiness | jq -c '.data|{ready,simulated}'
   ```

   Expected: `{"ready":true,"simulated":["eval","llm"]}`. The simulated providers play no part in this lab.
4. For the UI path you need `npx` and Chromium (`cd caliber/caliber-ui && npx playwright install chromium`).

## Dataset and sample files

All under [`assets/`](assets/) (the folder's [README](assets/README.md) says more):

| File | What it is | Why |
| --- | --- | --- |
| [`dataset/release-candidates.jsonl`](assets/dataset/release-candidates.jsonl) | 20 candidates: name, artifact, versions, rollback version, required score, criteria, evidence, a note on what each teaches, and the expected score, status and blockers | what you create, and the figures the server must reproduce |
| [`decision/decision-record.template.md`](assets/decision/decision-record.template.md) | a drafting aid for a signoff rationale | optional; the signoff row is the record |

The criteria are the ones a real release asks for, by artifact type. A prompt: pass rate on a sealed set, no regression against the
pinned baseline, expert review of the changed cases, p95 latency. A workflow: scenario pass rate, the approval gate held in a
rehearsal, rollback rehearsed, trace coverage. A skill: trigger precision and recall, package validity. A knowledge base: retrieval hit
rate, groundedness, freshness. Some are blocking, some are not. The expectations were worked out with exact fractions, not copied from
the server's output, and `caliber/tests/test_cookbook_11_release_signoff_lab.py` checks every row against the server's own
`_evaluate_candidate`, so the dataset cannot drift.

The rule the server applies: `weighted_score = Σ weight × score ÷ Σ weight` (rounded to six places); a **blocker** is a `blocking`
criterion whose `score` is below its `threshold` and which is not waived; the candidate is `ready` only when `weighted_score ≥
required_score` **and** there is no blocker. A score exactly at its threshold is not a blocker, and a weighted score exactly equal to the
required score is ready.

| Candidate | What it teaches | Score | Status |
| --- | --- | --- | --- |
| RC01-RC04 | a clean prompt, workflow, skill and knowledge base release | 0.8865 to 0.9775 | ready |
| RC05 | a high score (0.9455) with a blocking gate unmet (human review 0.90 of 1.00) | 0.9455 | **blocked** (`human_review`) |
| RC06 | every gate met, weighted score just under the bar (0.922 against 0.93) | 0.922 | **blocked**, nothing to waive |
| RC07 | weak everywhere | 0.613 | **blocked** (three blockers) |
| RC08 | scores exactly at their thresholds; weighted score exactly equal to the bar | 0.935 | ready |
| RC09 | precision and recall exactly at threshold | 0.9155 | ready |
| RC10 | a non-blocking criterion far under its threshold costs score but never blocks | 0.7705 | ready |
| RC11, RC12, RC13 | one criterion; weights 90/10; twelve criteria | 0.84, 0.812, 0.93942 | ready |
| RC14, RC15 | a perfect bar (1.0) met; no bar at all (required score 0 and threshold 0) | 1.0, 0.0 | ready |
| RC16 | the waiver drill: one blocker, score above the bar | 0.721 | **blocked** (`rollback_rehearsed`) |
| RC17 | two blockers and a score under the bar | 0.6575 | **blocked** |
| RC18, RC19, RC20 | the signoff drills: a GO, a NO-GO, and the walkthrough candidate | 0.919, 0.761, 0.9375 | ready, blocked, ready |

RC15 is there as a warning: a required score of 0 and a threshold of 0 describe a candidate that nothing can fail. `ready` means the
record satisfies the criteria you wrote, no more.

## Configuration and environment

| Variable | Value used | Why |
| --- | --- | --- |
| `CALIBER_FEATURE_RELEASE_CANDIDATES` | `true` (via `--flags release_candidates`) | the **Release signoff factory** section and `/releases/candidates`; without it they answer `404 feature_disabled` |
| `CALIBER_FEATURE_COOKBOOKS` | `true` (via `--flags cookbooks`) | only the gallery |
| `CALIBER_OPERATOR_USERS` | `admin,lab11-operator` | creating, re-evaluating and reporting need the operator scope |
| `CALIBER_ADMIN_USERS` | `admin` (the script's default) | waivers and signoffs need the admin scope |
| `CALIBER_BASE_URL`, `CALIBER_TOKEN` | from `env.sh` and the next step | what the script reads |
| `LAB11_SUFFIX` | unset | set it (`_2`) to re-run the script on a stack that already holds an earlier run |

## Run it through the API

Everything below is in one script that exits `0` only when every outcome above was observed. Mint a token, then run it from the repo
root:

```bash
source .tmp/cookbook-env/lab11/env.sh
curl -s -c /tmp/lab11.cookies -X POST $CALIBER_API/auth/login -H 'content-type: application/json' \
  -d '{"user_id":"admin","password":"admin"}' > /dev/null
export CALIBER_TOKEN=$(curl -s -b /tmp/lab11.cookies -X POST $CALIBER_API/auth/tokens \
  -H 'content-type: application/json' -d '{"name":"lab11"}' | jq -r .data.token)

(cd sdk/caliber-sdk && ../../caliber/.venv/bin/python -m examples.cookbooks.cookbook_11_release_signoff_factory)
```

It takes a few seconds. Real output, trimmed (ids vary):

```text
  created account lab11-operator
  created account lab11-viewer
== 1. a real artifact to release: a prompt with two versions
  ok  payments-assistant: version 1 and 2 exist, no alias points at either
== 2. create the 20 candidates as the operator
  ok  RC01 0.961   ready   blockers=[]
  ok  RC02 0.9775  ready   blockers=[]
  ok  RC03 0.9485  ready   blockers=[]
  ok  RC04 0.8865  ready   blockers=[]
  ok  RC05 0.9455  blocked blockers=['human_review']
  ok  RC06 0.922   blocked blockers=[]
  ok  RC07 0.613   blocked blockers=['eval_pass_rate', 'regression_vs_baseline', 'human_review']
  ok  RC08 0.935   ready   blockers=[]
  ok  RC09 0.9155  ready   blockers=[]
  ok  RC10 0.7705  ready   blockers=[]
  ok  RC11 0.84    ready   blockers=[]
  ok  RC12 0.812   ready   blockers=[]
  ok  RC13 0.93942 ready   blockers=[]
  ok  RC14 1.0     ready   blockers=[]
  ok  RC15 0.0     ready   blockers=[]
  ok  RC16 0.721   blocked blockers=['rollback_rehearsed']
  ok  RC17 0.6575  blocked blockers=['regression_vs_baseline', 'human_review']
  ok  RC18 0.919   ready   blockers=[]
  ok  RC19 0.761   blocked blockers=['trigger_precision', 'trigger_recall']
  ok  RC20 0.9375  ready   blockers=[]
  ok  14 ready, 6 blocked, as the dataset predicts
  ok  all 20 are listed
== 3. what a candidate is refused for
  ok  a criterion weight of 0 is rejected -> HTTP 400
  ok  a score above 1 is rejected -> HTTP 400
  ok  a key with a space is rejected -> HTTP 400
  ok  no criteria at all is rejected -> HTTP 400
  ok  a required score above 1 is rejected -> HTTP 400
  ok  an unknown field is rejected -> HTTP 400
  ok  a viewer cannot create a candidate -> HTTP 403
  ok  a candidate for an artifact that does not exist is accepted: CALIBER records the reference and does not look it up
== 4. waivers
  ok  an operator cannot record a waiver (admin only) -> HTTP 403
  ok  an unknown criterion key is rejected -> HTTP 400
  ok  a reason under 8 characters is rejected -> HTTP 400
  ok  an expiry in the past is rejected -> HTTP 400
  ok  RC16 report before the waiver: rollback_rehearsed failed
  ok  RC16: the waiver clears the only blocker; the weighted score is still 0.721
  ok  the waiver records who approved it, when, and its expiry
  ok  Re-evaluate recomputes from the stored criteria and waivers: unchanged
  ok  RC16 report after the waiver: the criterion is skipped (waived)
  ok  the first report's snapshot hash is stale once the candidate changed
  ok  RC17 after waiving regression_vs_baseline: 1 blocker left, still blocked (weighted 0.6575 < 0.85)
  ok  RC17 after waiving human_review: 0 blocker left, still blocked (weighted 0.6575 < 0.85)
  ok  GO on RC17 is refused: no blocker is left, but the score is under the bar -> HTTP 409
  ok  GO on RC05 is refused: a blocking criterion (human_review) is unmet -> HTTP 409
== 5. sign off
  ok  an operator cannot sign off (admin only) -> HTTP 403
  ok  a rationale under 8 characters is rejected -> HTTP 400
  ok  RC20 report job RRJ-9c0ee8e6: completed, 4 passed, snapshot hash recorded
  ok  the job can be read back by id
  ok  RC20 signed GO by admin (RSO-591bdbc4); the snapshot froze score 0.9375
  ok  RC19 (blocked) signed NO-GO: a refusal is a decision too
  ok  RC18 signed GO
  ok  the candidate shows signed_go with its signoff and report job
== 6. a signed candidate is final
  ok  Re-evaluate is refused (409) -> HTTP 409
  ok  a waiver is refused (409) -> HTTP 409
  ok  a second signoff is refused (409) -> HTTP 409
  ok  it is still signed_go
  ok  GO on a ready candidate with no planned action or rollback target is refused -> HTTP 409
== 7. signing off deployed nothing
  ok  payments-assistant: no alias moved (still {2: [], 1: []})
  ok  the live board and the release timeline did not change
  ok  the real release is a separate act: Promote on the prompt moves the alias
  ok  and that, not the signoff, is what the timeline records
== 8. who did what
  ok  audit: create_release_candidate by ['lab11-operator']
  ok  audit: waive_release_blocker by ['admin']
  ok  audit: generate_release_allure_report by ['admin', 'lab11-operator']
  ok  audit: signoff_release_candidate by ['admin']
  ok  audit: evaluate_release_candidate by ['lab11-operator']
== done
```

What each stage does, by hand (the script wraps exactly these calls; `$C` is a candidate id, `$H` is
`-H "Authorization: Bearer $CALIBER_TOKEN"`):

```bash
curl -s $H -X POST $CALIBER_API/releases/candidates -H 'content-type: application/json' -d '{
  "name": "payments-api prompt v15", "artifact_type": "prompt", "artifact_ref": "payments-assistant", "version_ref": "2",
  "required_score": 0.9,
  "criteria": [{"key": "eval_pass_rate", "title": "Evaluation pass rate", "weight": 40, "score": 0.94,
                "threshold": 0.9, "blocking": true, "evidence_refs": ["EVR-940"]}, ...],
  "evidence": [{"evidence_type": "evaluation_run", "evidence_ref": "EVR-940", "label": "Sealed evaluation"}],
  "planned_action": {"action": "promote_prompt", "target": "prod"},
  "rollback_target": {"version_ref": "1", "target": "prod"}}'                              # 201: weighted_score, status, blockers
curl -s $H -X POST $CALIBER_API/releases/candidates/$C/evaluate                              # recompute from what is stored
curl -s $H -X POST $CALIBER_API/releases/candidates/$C/waivers -H 'content-type: application/json' \
  -d '{"criterion_key":"rollback_rehearsed","reason":"Rehearsal booked for the 25th. Owner: release-eng.",
       "expires_at":"2026-11-01T00:00:00Z"}'                                               # admin only
curl -s $H -X POST $CALIBER_API/releases/candidates/$C/reports -H 'content-type: application/json' -d '{}'   # the report job
curl -s $H -X POST $CALIBER_API/releases/candidates/$C/signoffs -H 'content-type: application/json' \
  -d '{"decision":"go","rationale":"Quality 0.94, no regression, review complete, rollback v1 ready."}'  # admin only
curl -s $H $CALIBER_API/releases/candidates/$C        # {candidate, signoffs, report_jobs}
```

Things the script taught while it was written, and one thing it found and fixed:

- **Validation errors are `400`, not `422`.** A weight of 0, a score of 1.2, a key with a space, no criteria and an unknown field all
  answer `400 request body validation failed` with the offending field named.
- **A waiver does not change the score.** It removes a blocker from the list; the weighted score is the same, so a candidate under its
  bar stays blocked (RC17). Do not raise a score to clear a blocker: create a new candidate for new evidence, because there is no route
  that edits a candidate. **Re-evaluate** recomputes from what is stored and changes nothing unless a waiver has lapsed.
- **A report generated before a change is a stale snapshot.** RC16's report before its waiver lists `rollback_rehearsed` as `failed`; after
  the waiver, `skipped` (waived) and with a different `candidate_snapshot_sha256`. Generate the report you intend to keep last.
- **Waiver expiry used to be ignored, and a signed candidate used to be changeable.** Both are fixed: the API accepted `expires_at` and
  then treated the waiver as permanent (a lapsed waiver still cleared its blocker, and a go decision could rest on it); and recording a
  waiver on a signed candidate re-evaluated it and turned `signed_go` back into `ready`, which allowed a second signoff. A past
  `expires_at` is now refused (`400`), an expiry is honoured every time the candidate is evaluated (including at signoff), and a signed
  candidate answers `409 signed release candidates are immutable` to a waiver as to an evaluation.
- **The SDK's `get_candidate` returned an empty candidate** (it decoded the server's `{candidate, signoffs, report_jobs}` envelope as the
  candidate). It now returns the candidate with its signoffs and report jobs in `.extra`.
- **A candidate created outside a workspace is private to its creator.** The viewer account does not see the admin's candidates in the
  list; create them inside a workspace to share them with its members.

## Recipe: run it through the UI

Sign in at `$CALIBER_URL/caliber/` as `admin` / `admin` (who holds the operator and admin scopes the buttons need). Under **Administration**
(group **Admin**) create `lab11-viewer` for the last check. Then:

1. **Open the section.** **Prompt lifecycle → Releases**, scroll to **Release signoff factory**. A violet note says *Signing off a
   release candidate records a decision. It does not deploy or roll back anything*.
2. **Create a candidate with one blocker.** **New candidate**. Name it, choose **Workflow**, enter the **Artifact ID or registry name**
   `WF-claims-intake`, **Candidate version** `5`, **Rollback version** `4`, required score `0.70`, and paste RC16's `criteria` array
   (from `release-candidates.jsonl`) into the criteria box and its `evidence` array into the evidence box. **Create and evaluate
   candidate**. The card reads **blocked**, `72%`, and *4 criteria · 1 evidence · 1 blockers · rollback 4*; **Sign off GO** is disabled.
   (The form records the planned action for you, `promote_workflow` to `prod`; it is not a field.)
3. **Record a waiver.** Under the card type the criterion key `rollback_rehearsed` and a reason. **Record waiver** stays disabled until the
   reason has 8 characters. Enter *Rollback rehearsal booked for the 25th; on-call holds the previous image. Owner: release-eng.* and click
   it: the card reads **ready**, *0 blockers*, still `72%`. (The form has no expiry field, so write the expiry into the reason; the API
   takes `expires_at`.)
4. **Re-evaluate.** **Re-evaluate**: still **ready**, still `72%`.
5. **Generate Allure.** **Generate Allure**: the card says *Allure report RRJ-… generated and retained.* Note the id.
6. **Sign off GO.** Type a **Signoff rationale** (two characters leave **Sign off GO** disabled; at least 8 enable it) and **Sign off GO**:
   the badge reads **signed_go**, and the rationale box and both sign-off buttons are gone: a signed candidate is final.
7. **Sign off NO-GO on a blocked candidate.** Create RC05 the same way (type **Prompt**, `pricing-quote`, version `9`, rollback `8`,
   required score `0.85`): **blocked**, `95%`, because human review is below its blocking threshold. **Sign off GO** is disabled;
   with a rationale, **Sign off NO-GO** records the decision: **signed_no_go**.
8. **Reload.** Both candidates still read **signed_go** and **signed_no_go**: the decision is persisted, not page state.

As `lab11-viewer` (no scopes), open **Releases**: the section is there, but there is no **New candidate** button and no **Generate Allure**,
and the admin's candidates are not listed.

The whole path is `caliber/caliber-ui/e2e/cookbooks/labs/11-release-signoff.spec.ts`; run it against a running stack:

```bash
cd caliber/caliber-ui
CALIBER_LAB_BASE_URL="$CALIBER_URL/caliber" npx playwright test --config=playwright.labs.config.ts e2e/cookbooks/labs/11-release-signoff.spec.ts
```

The card shows the status, the score, the counts and the report message. It does not list the blockers' names, the waivers, the signoff
id or the report's contents: read those from `GET /releases/candidates/{id}` and `GET /releases/report-jobs/{id}` (the script does).

## Verify it worked

| Check | Where | Expected |
| --- | --- | --- |
| Scoring | `POST /releases/candidates` for each row | `weighted_score`, `status` and `blockers` equal the row's `expect`: 14 ready, 6 blocked |
| Refusals | the same with a bad body; as `lab11-viewer` | `400` naming the field; `403 missing required scope; expected one of ['caliber.operator']` |
| A waiver | `POST .../waivers` as admin | RC16: `status: ready`, `blockers: []`, `weighted_score: 0.721`, the waiver carries `approved_by: admin`, `approved_at` and `expires_at`; as the operator `403`; unknown key `400`; reason under 8 characters `400`; past expiry `400` |
| A waiver is not a score | RC17 after both waivers | `status: blocked`, `blockers: []`, `weighted_score: 0.6575` |
| Report | `POST .../reports`, `GET /releases/report-jobs/{id}` | `status: completed`, `format: allure-json`, `report.format: allure-compatible-results`, a 64 character `candidate_snapshot_sha256`, one result per criterion (`passed`, `failed` or `skipped`) |
| GO refused | `POST .../signoffs` on RC05, RC17 | `409 go signoff requires a ready candidate` |
| GO without a way back | a ready candidate with empty `planned_action` and `rollback_target` | `409 go signoff requires a planned release action and rollback target` |
| Signoff | `POST .../signoffs` on RC20 as admin | `201`: `decision: go`, `decided_by: admin`, `candidate_snapshot.weighted_score: 0.9375`; the candidate reads `signed_go` |
| Final | evaluate, waive or sign RC20 again | `409` each |
| Deploys nothing | `GET /prompts/{name}/versions`, `GET /releases/live`, `GET /releases/timeline` before and after the signoff | unchanged: no alias on either prompt version, no event |
| The real release | `POST /prompts/{name}/aliases/prod` | version 2 carries `prod`, and `promote_prompt` appears on the timeline |
| Audit | `GET /audit-log` | `create_release_candidate` by `lab11-operator`; `waive_release_blocker` and `signoff_release_candidate` by `admin`; `generate_release_allure_report`; `evaluate_release_candidate` |

### The trust boundary, honestly

- **The record is as honest as the person entering it.** Nothing ties a score to an evaluation, and nothing checks that an evidence
  reference or an artifact exists. What CALIBER adds is that the arithmetic is the server's, the blockers cannot be argued away without
  an admin, every exception names who approved it, and the decision cannot be changed afterwards.
- **Waivers and signoff are admin-only and audited**, and an operator, who can create and re-evaluate candidates, can do neither.
- **Candidates are private unless made in a workspace.** There is no project role check on these routes beyond visibility.
- **Immutability is of the record, not of the world.** A signed `go` for `payments-assistant` version 2 does not stop anyone promoting
  version 3 or rolling back; it records that a person decided version 2 was good enough on that date, for those reasons.

## Common errors and troubleshooting

| You see | Why | Fix |
| --- | --- | --- |
| `404 feature_disabled` on `/releases/candidates`, no **Release signoff factory** section | the release-candidates flag is off (the default) | start with `--flags release_candidates` (`CALIBER_FEATURE_RELEASE_CANDIDATES=true`) and restart |
| `403 missing required scope; expected one of ['caliber.operator']` on create, evaluate or report | the account has no operator scope | add it to `CALIBER_OPERATOR_USERS` and restart |
| `403 ... ['caliber.admin']` on a waiver or signoff | only admins record those | use `admin` |
| `400 request body validation failed ... criteria.0.weight: Input should be greater than 0` (or the score, key or an extra field) | the criteria JSON is invalid | fix the named field; keys are letters, digits, `_` and `-`; scores are 0 to 1 |
| The form's **Create and evaluate candidate** stays disabled | the name, artifact, version and rollback version are all required | fill them |
| a red message under the form (the JSON parser's error, or *Criteria and evidence must be JSON arrays.*) | the JSON boxes are not valid arrays | paste the `criteria` and `evidence` arrays from the dataset row |
| `400 waiver criterion does not exist` | the key is not one of the candidate's criteria | use a `key` from the candidate |
| `400 waiver expires_at must be in the future` | the expiry is in the past | give a future date, or none |
| **Record waiver** is disabled | the reason is under 8 characters | write the reason, expiry and compensating control |
| `409 go signoff requires a ready candidate` | a blocker stands, or the score is under the bar | waive an admin-approved blocker, or create a new candidate with new evidence; a waiver cannot raise a score |
| `409 go signoff requires a planned release action and rollback target` | an API-created candidate has neither | send `planned_action` and `rollback_target` (the form does) |
| `409 release candidate already has a final signoff` / `signed release candidates are immutable` | the candidate is signed | create a new candidate for a changed release package |
| the candidate is **ready** but a re-evaluation says **blocked** | a waiver with an `expires_at` has lapsed | record a new waiver, or fix the cause |
| the candidate list is empty for another account | candidates outside a workspace are private to their creator | create them inside a workspace |
| the report's `skipped` and hash differ from last time | the candidate changed after the report was made | generate the report again, last |

## Cleanup and reset

Release candidates are an audit record: they cannot be deleted, and a signed one cannot change. The script's `--cleanup` removes the one
real artifact it made (the prompt) and says so:

```bash
(cd sdk/caliber-sdk && ../../caliber/.venv/bin/python -m examples.cookbooks.cookbook_11_release_signoff_factory --cleanup)
```

To run again on the same stack, set `LAB11_SUFFIX` (above); the 20 earlier candidates stay in the list. For a clean slate, discard the
whole disposable stack:

```bash
scripts/cookbook-env.sh down lab11 && rm -rf .tmp/cookbook-env/lab11
```
