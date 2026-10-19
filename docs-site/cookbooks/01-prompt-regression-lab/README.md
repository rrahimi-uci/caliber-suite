# Trustworthy Intake Classifier: release a prompt change you can trust

> **Prerequisite flag:** `CALIBER_FEATURE_COOKBOOKS=true`, and nothing else. No Aria, no integrations (the optional [appendix](#appendix-the-aria-steps-verified-on-a-hosted-model)
> adds `CALIBER_FEATURE_ARIA=true`).
> Every step below was run against a real server (a fresh stack from `scripts/cookbook-env.sh up lab01 --flags cookbooks`)
> through the API and through the browser, in four tiers:
>
> 1. the alpha's default **simulated** language model and evaluator (the main path below; offline, deterministic);
> 2. a **stand-in** model server ([Run the evaluation with a stand-in model](#run-the-evaluation-with-a-stand-in-model); offline, deterministic, not a model);
> 3. a **hosted model**, OpenAI `gpt-5.6-luna` with MLflow's default judge ([Run it with a hosted provider](#run-it-with-a-hosted-provider-verified)): **verified**, API and browser, with the real optimizer;
> 4. a **local model**, `qwen2.5:7b` served by Ollama ([Run it with a local model](#run-it-with-a-local-model-ollama-verified)): verified, free, slower and much weaker.

## What this lab demonstrates

The alpha's one promise, end to end, on a realistic job: a support-desk classifier that turns every inbound
message into one strict JSON record (`intent`, `priority`, `confidence`, `needs_review`, `reason`).

You author the prompt, **import a 44-case labelled test set from a file**, **evaluate** the prompt version, introduce
a **regression** (a friendlier v2 that drops the "JSON only" rules) and see it caught against the baseline, then
**calibrate** the prompt, wait for the candidate, **review its evidence and diff**, **Apply** it explicitly,
verify the live alias really moved in the MLflow registry and that the release shows on the live board, the release timeline
and the audit log with who applied it, and finally **roll back** and verify the alias returned.

**You are done when:**

- the prompt `cookbook-01-intake-classifier` has v1 (strict) live and v2 (weak) registered but not live;
- the test set holds 44 examples, imported from `assets/dataset/intake-classifier.jsonl`;
- on a simulated model, scoring a prompt answers `400 evaluation requires a real LLM provider` (and a missing test set answers `404`);
  with a model that answers, v1 and v2 are scored on all 44 cases and compared (the stand-in model: v1's answers are 44/44 strict JSON and v2's are 0/44, v2 fails
  the regression gate; a real model, hosted or local: v1 is 44/44 strict JSON, v2 **still answers in JSON** and the scorecard shows only a small, noisy difference,
  see [Run it with a real model](#run-it-with-a-real-model));
- a calibration run reaches `candidate_ready`; its evidence reports `44` examples and dataset version `45`, and says `simulated: true` on the simulated stack
  and `simulated: false` with measured baseline and candidate scores on a real model. On a real model the gate may instead say no: the run is `rejected` with the
  judge's real reason and Apply is `409` (an honest outcome, and one the script and the browser spec both accept);
- a viewer's Apply is `403`, a candidate below the gate is `rejected` and its Apply is `409`;
- after Apply, the MLflow registry's `prod` alias is v3, the live board shows `v3` by you, the timeline shows `v1 → v3`, the audit log shows
  `promote_prompt` and `apply_candidate` by you;
- after rollback, `prod` is v1 again in the registry and on the live board.

### What is simulated, and what a real provider changes

Under the default `CALIBER_LLM_PROVIDER=fake` / `CALIBER_EVAL_PROVIDER=fake` there is no model.

| Step | On the simulated stack (what you see) | With a real model and `CALIBER_EVAL_PROVIDER=mlflow` (verified on hosted `gpt-5.6-luna` and on a local `qwen2.5:7b`) |
| --- | --- | --- |
| **Evaluations** (score a prompt version) | Refused: `400 evaluation requires a real LLM provider`. CALIBER never invents a scorecard. | A scorecard with the model's real answer for each of the 44 examples; a failure (model unreachable, wrong model name, rejected key) is a `failed` run with the server's error, not a hang. |
| **Calibration** candidate | A canned candidate: the live prompt plus two comment lines (`# Refinement added by MetaPrompt`, `# Diagnosis: [fake] ...`, `+3 / -0 lines`). | What the real optimizer returns, which varies run to run: on the strong hosted model most candidates were the live prompt **unchanged** (`+0 / -0 lines`; there was nothing to fix) and one added a rule unrelated to a support desk (about "a verification or diagnostic report"); a 7B model rewrote it clumsily and, once, invented a "patient history" rule. The review step exists for exactly this. |
| **Calibration** evidence | Canned scores (candidate `0.94`, baseline `0.88`). Marked `provider: fake`, `simulated: true` in the API, **"Simulated evaluation — not evidence"** in the review dialog, `eval_simulated: true` on the registry version and in the audit and timeline rows. The example count (`44`) and dataset version are real. | Measured baseline and candidate scores from the judge, `provider: mlflow`, `simulated: false`, `eval_simulated: false` in the registry, timeline and audit rows, and no banner. The gate is decided by those scores, so it can honestly say no, and a judge that is noisier than the gate's `0.02` allowance can reject an unchanged prompt (see the hosted results). |
| **Apply, registry, live board, timeline, audit, rollback** | Real. The promoter is the real MLflow one: the alias really moves. | Same. |

So on the simulated stack the checks assert **structure, wiring and persistence** (the markers, the counts, the alias), never a score. The `min_aggregate_score`
gate compares a canned `0.94` to your threshold, so it proves the gate *blocks*; it says nothing about quality. On a real model the checks assert **invariants**
(answers parse as the contract, scores are in `0..1`, the evidence is not marked simulated and has `44` examples), never an exact score or answer, because a model's output varies.

## Prerequisites and setup

1. A running CALIBER built per [`docs/start/alpha-install.md`](../../../docs/start/alpha-install.md) (migrate, native MLflow, CALIBER, `/readiness` ready), with the
   Cookbooks flag on: `export CALIBER_FEATURE_COOKBOOKS=true` next to the other settings, then restart. Nothing else is needed; the defaults
   (`CALIBER_LLM_PROVIDER=fake`, `CALIBER_EVAL_PROVIDER=fake`) are what this lab runs on.
2. From a checkout you can instead start a disposable stack that does all of it (this is what the lab was verified on):

   ```bash
   scripts/cookbook-env.sh up lab01 --flags cookbooks      # ports are picked for you; the stack never touches port 5001
   source .tmp/cookbook-env/lab01/env.sh                    # CALIBER_URL, CALIBER_API, MLFLOW_TRACKING_URI, ...
   curl -s $CALIBER_API/readiness | jq -c '.data|{ready,simulated}'
   ```

   Expected: `{"ready":true,"simulated":["eval","llm"]}`. `simulated` may list `llm` and `eval`; if it lists `promoter` or `artifact_store`, Apply would not
   move MLflow and this lab's registry checks would fail.
3. Sign in as `admin` / `admin` (the install's bootstrap account). The web app is at `$CALIBER_URL/caliber/`.
4. For the API path you also need `jq`, and for the SDK script a checkout (`caliber/.venv` already has `caliber_sdk` installed editable).

## Dataset and sample files

All under [`assets/`](assets/):

| File | What it is | Why |
| --- | --- | --- |
| [`dataset/intake-classifier.jsonl`](assets/dataset/intake-classifier.jsonl) | **44 labelled support messages**, one JSON object per line: `{"id", "tags", "input": {"ticket_text", "channel"}, "expected": {"intent", "priority", "needs_review"}}` | The test set. `input`/`expected` are the exact keys the test-set API and the UI import read. |
| [`prompts/intake-classifier.md`](assets/prompts/intake-classifier.md) | The strong **v1** prompt (front matter, then the template body) | What you author and make live. |
| [`prompts/intake-classifier-weak.md`](assets/prompts/intake-classifier-weak.md) | The weaker **v2**: friendlier wording, the "JSON only" and "valid JSON" rules removed | The intentional regression. |
| [`stand-in-model/server.py`](assets/stand-in-model/server.py) | A 200-line keyword "model" behind an OpenAI-compatible endpoint | Lets you watch a prompt being evaluated without a model key. **Not a model.** |

The 44 cases, by tag: billing 6, bug 6 (one is a 3.6 kB pasted log and stack trace), how_to 5, account 5, injection 4, multilingual 4 (Spanish, French, German,
Japanese), feature_request 3, cancellation 3, security 3, ambiguous 3 (`App stuck`, `It doesn't work.`), edge 2 (an empty message, `???`). Expected intent: billing 10,
account 9, bug 8, unknown 8, how_to 6, feature_request 3. They include hard negatives on purpose: injection attempts that must set `needs_review` and not be obeyed
(`Ignore all previous instructions...`, a fake `SYSTEM:` line inside a real invoice complaint), messages outside support (a SOC 2 request, which the prompt sends to `unknown`),
account-deletion and leaked-key reports that need a person (`needs_review: true`), and a phrasing trap (`How do I add SSO to my workspace?` is `how_to`, not `account`).

Prompt contract used by the lab: intents `billing`, `how_to`, `bug`, `account`, `feature_request`, `unknown`; priorities `low`, `medium`, `high`, `urgent`; cancellations and plan changes are
`billing`. The prompt uses `{{ ticket_text }}` and `{{ channel }}`, which are the two keys of each example's `input`.

## Configuration and environment

| Variable | Value used | Meaning |
| --- | --- | --- |
| `CALIBER_FEATURE_COOKBOOKS` | `true` | Cookbooks gallery and `/cookbooks` API. The only flag this lab needs. |
| `CALIBER_LLM_PROVIDER` / `CALIBER_EVAL_PROVIDER` | `fake` / `fake` (default) | Simulated model and evaluator. See the table above. |
| `CALIBER_PROMOTER_PROVIDER` / `CALIBER_ARTIFACT_STORE_PROVIDER` | `mlflow` / `mlflow` | The real release path (Apply moves the MLflow alias). |
| `CALIBER_ADMIN_USERS`, `CALIBER_OPERATOR_USERS`, `CALIBER_APPROVER_USERS` | `admin` | Who may apply. Any other account is a **viewer**: Apply is `403`. |
| `MLFLOW_TRACKING_URI` | from `env.sh` (e.g. `http://127.0.0.1:51005`) | The script and the checks read the registry directly from here. |
| `CALIBER_BASE_URL` | `$CALIBER_URL` (the server root, **without** `/ajax-api/...`) | What `caliber_sdk` connects to. `env.sh` also exports `CALIBER_API`, which is the root plus the API path; use that one for `curl`. |
| `CALIBER_TOKEN` | a personal access token | The SDK script's credential (minted below). |
| `COOKBOOK_01_ASSETS` | optional | Use a different assets directory for the script. |
| `COOKBOOK_01_HTTP_TIMEOUT` | optional, `900` | Seconds a synchronous evaluation may take (a scored run answers when all 44 examples are answered; the SDK's default 30 s would cut a real model off). |
| `COOKBOOK_01_CALIBRATION_TIMEOUT` | optional, `300` simulated / `1500` real | Seconds the script waits for the calibration candidate. |
| `COOKBOOK_01_MIN_SCORE`, `COOKBOOK_01_MAX_REGRESSION` | unset (product gate `0.85` / `0.02`); `0.3` / `0.25` on the local 7B tier; `0.25` for the hosted release run | The calibration gate on a **real** model only (ignored on the simulated one). Also read by the Playwright spec. |
| script flag `--accept-rejection` | hosted tier | On a real model, a calibration the gate rejects (with its reason, on real evidence) is a verified outcome and the script stops there with exit 0, instead of failing. |
| `OPENAI_API_KEY` | only for [a hosted model](#run-it-with-a-hosted-provider-verified) | Your key, exported in your shell (or read from the repository's gitignored `.env` by `cookbook-env.sh`, which reads only the two key variables). Never put it in a file the repository tracks. |
| `OPENAI_BASE_URL`, `OPENAI_API_BASE`, `CALIBER_LLM_PROVIDER`, `CALIBER_EVAL_PROVIDER`, `CALIBER_LLM_DIAGNOSIS_MODEL`, `CALIBER_GEPA_REFLECTION_MODEL`, `MLFLOW_GENAI_JUDGE_DEFAULT_MODEL` | only for [a local model](#run-it-with-a-local-model-ollama-verified) | Every model setting the answers, the optimizer, the diagnosis and the judge read; see that section for the values used. The hosted tier uses the defaults (`gpt-5.6-luna` for answers, optimizer and diagnosis; `gpt-4.1-mini` for the judge). |

## Run it through the API

Everything runs from the repository root. First sign in and mint a token (the browser equivalent is **Settings → API tokens**):

```bash
source .tmp/cookbook-env/lab01/env.sh
JAR=$CALIBER_STATE_DIR/cookbook01.jar
B=$CALIBER_API
curl -s -c $JAR -X POST $B/auth/login -H 'content-type: application/json' -d '{"user_id":"admin","password":"admin"}'
export CALIBER_TOKEN=$(curl -s -b $JAR -X POST $B/auth/tokens -H 'content-type: application/json' -d '{"name":"cookbook-01"}' | jq -r .data.token)
export CALIBER_BASE_URL=$CALIBER_URL
```

### One command: the whole lab, verified

```bash
PYTHONPATH=sdk/caliber-sdk caliber/.venv/bin/python \
  sdk/caliber-sdk/examples/cookbooks/cookbook_01_trustworthy_intake_classifier.py
```

It performs every step below through the SDK, prints one `ok` line per verified outcome, and **exits 0 only if all of them held** (exit 1, `VERIFICATION FAILED: ...` otherwise).
It reads `GET /readiness` and works on every tier: against a real model it runs the evaluations for real and asserts invariants instead (see [Run it with a real model](#run-it-with-a-real-model)).
Expected output on the simulated stack (ids and the timestamp suffix vary; the line text and the counts do not):

```text
ok   cookbook 01 is listed with guided steps  [11 steps]
ok   v1 is live in the MLflow registry
ok   every row of the file was imported  [44/44]
ok   v2 registered
ok   v2 is NOT live (registering is not deploying)
ok   evaluation is refused on a simulated model (400)  [[400] evaluation requires a real LLM provider; set CALIBER_LLM_PROVIDE]
ok   evaluating a missing test set is a 404  [[404] eval dataset 'ED-does-not-exist' not found (POST http://127.0.0.1:...]
ok   calibration reached candidate_ready  [candidate_ready]
ok   the candidate differs from the live prompt  [+3 / -0 lines]
ok   the evidence reports the real test-set size and version  [44 examples, dataset v45]
ok   the gate passed
ok   the evidence is marked simulated  [provider=fake simulated=True]
ok   a viewer cannot apply (403)  [[403] missing required scope; expected one of ['caliber.operator'], ha]
ok   a candidate below the gate is rejected  [rejected]
ok   applying a job that is not candidate_ready is a 409  [[409] job 'RFN-...' cannot be applied: current status is 'rejecte]
ok   apply registered a new version  [v3]
ok   the live alias moved in the MLflow registry  [prod -> v3]
ok   the registry version carries the job and the evaluation provenance  [job RFN-..., eval_simulated=true]
ok   the live board shows the new version, applied by you  [v3 by admin]
ok   the release timeline shows the promotion by you  [v1 -> v3 by admin]
ok   the audit log has the promotion and the apply, both by you
ok   applying the same job twice is a 409  [[409] job 'RFN-...' cannot be applied: current status is 'applied]
ok   rollback restores v1
ok   the live alias returned to v1 in the MLflow registry  [prod -> v1]
ok   the live board shows v1 again
ok   the audit log records the rollback by you

26/26 checks passed
```

followed by a JSON summary (`applied_version: 3`, `examples: 44`, `evaluation: "refused: needs a real model"`, `evidence: {provider: fake, simulated: true, n_examples: 44}`, ...).
The version numbers are v1 strong, v2 weak (never live), v3 the applied candidate (calibration rewrites the **live** prompt, v1).

### The same steps by hand, with `curl`

Names below are fixed (`cookbook-01-intake-classifier`, `cookbook-01-intake-golden`); [Cleanup](#cleanup-and-reset) removes them. `A` is the assets directory and `body` strips the
front matter from a prompt file.

```bash
A=docs-site/cookbooks/01-prompt-regression-lab/assets
body() { awk '/^---$/{c++; next} c>=2' "$1"; }
```

**1. Author v1 and make it live** (expect `"version":1`, then `release_status":"applied"`):

```bash
jq -Rs '{name:"cookbook-01-intake-classifier", template:., commit_message:"v1 strict-JSON intake classifier"}' < <(body $A/prompts/intake-classifier.md) \
  | curl -s -b $JAR -X POST $B/prompts -H 'content-type: application/json' -d @- | jq -c '{name:.data.name,version:.data.version,uri:.data.uri}'
curl -s -b $JAR -X POST $B/prompts/cookbook-01-intake-classifier/aliases/prod -H 'content-type: application/json' -d '{"version":1}' | jq -c .data
```

```text
{"name":"cookbook-01-intake-classifier","version":1,"uri":"prompts:/cookbook-01-intake-classifier/1"}
{"name":"cookbook-01-intake-classifier","alias":"prod","version":1,"previous_live_version":null,"operation_id":"REL-...","release_status":"applied"}
```

**2. Import the test set from the file.** `POST /eval-datasets` creates the set, then one `POST /eval-datasets/{id}/examples` per line. Each example bumps the set's version, so 44 rows give version `45`:

```bash
DS=$(curl -s -b $JAR -X POST $B/eval-datasets -H 'content-type: application/json' \
  -d '{"name":"cookbook-01-intake-golden","owner":"admin","description":"44 labelled support messages (Cookbook 01)"}' | jq -r .data.dataset_id)
while IFS= read -r line; do
  jq -c '{input, expected, tags}' <<<"$line" | curl -s -b $JAR -X POST $B/eval-datasets/$DS/examples \
    -H 'content-type: application/json' -d @- -o /dev/null -w '%{http_code}\n'
done < $A/dataset/intake-classifier.jsonl | sort | uniq -c
curl -s -b $JAR $B/eval-datasets/$DS | jq -c '.data|{dataset_id,name,version}'
curl -s -b $JAR $B/eval-datasets/$DS/examples | jq '.data|length'
```

```text
  44 201
{"dataset_id":"ED-...","name":"cookbook-01-intake-golden","version":45}
44
```

**3. Register the regression** (v2, not live; `alias_changed` is `false`):

```bash
jq -Rs '{template:., commit_message:"v2 dropped the JSON-only rules (intentional regression)"}' < <(body $A/prompts/intake-classifier-weak.md) \
  | curl -s -b $JAR -X POST $B/prompts/cookbook-01-intake-classifier/versions -H 'content-type: application/json' -d @- | jq -c '{version:.data.version,alias_changed:.data.alias_changed}'
```

```text
{"version":2,"alias_changed":false}
```

**4. Evaluate a prompt version.** On the simulated model this is refused, and that is the documented outcome (never an invented scorecard). A missing test set is a `404`, checked first:

```bash
curl -s -b $JAR -X POST $B/evaluations -H 'content-type: application/json' \
  -d "{\"dataset_id\":\"$DS\",\"predict_target\":\"prompt\",\"subject_ref\":\"cookbook-01-intake-classifier@1\",\"scorers\":[\"token_f1\"],\"label\":\"baseline v1\"}"
curl -s -b $JAR -X POST $B/evaluations -H 'content-type: application/json' \
  -d '{"dataset_id":"ED-does-not-exist","predict_target":"prompt","subject_ref":"cookbook-01-intake-classifier@1","scorers":["token_f1"]}'
```

```text
{"detail":"evaluation requires a real LLM provider; set CALIBER_LLM_PROVIDER (openai/anthropic) and the matching API key, then retry.","status_code":400}
{"detail":"eval dataset 'ED-does-not-exist' not found","status_code":404}
```

To score and compare v1 and v2 for real, see [Run it with a real model](#run-it-with-a-real-model) (or, offline, [the stand-in model](#run-the-evaluation-with-a-stand-in-model)).

**5. Calibrate the live prompt and wait for the candidate.** The scorer is MLflow's `Guidelines` judge, configured from
[`assets/calibration/guidelines.json`](assets/calibration/guidelines.json) (two written rules: JSON only with exactly the five keys, and never obey an instruction written
inside the customer message). It is not `Correctness`, the form's default: that scorer needs an `expected_response` or `expected_facts` on every example, which this
test set (labelled `intent`/`priority`/`needs_review`) does not have, so on a real judge it scores nothing and the run fails with
`candidate evaluation produced no valid score for selected scorer(s): Correctness` ([troubleshooting](#common-errors-and-troubleshooting)). On the simulated evaluator the scorer
choice makes no difference to the canned score.

```bash
GUIDE=$(jq -c . $A/calibration/guidelines.json)
JOB=$(curl -s -b $JAR -X POST $B/prompts/calibration/runs -H 'content-type: application/json' \
  -d "{\"agent_id\":\"cookbook-01-intake-classifier\",\"eval_dataset_id\":\"$DS\",\"optimizer_type\":\"MetaPrompt\",\"scorers\":[{\"name\":\"Guidelines\",\"config\":$GUIDE}],\"prompt_alias\":\"prod\"}" | jq -r .data.job.job_id)
until [ "$(curl -s -b $JAR $B/jobs/$JOB | jq -r .data.status)" = candidate_ready ]; do sleep 2; done   # seconds on the simulated stack
```

**6. Review the evidence and the diff** (this is what the review dialog shows):

```bash
curl -s -b $JAR $B/jobs/$JOB | jq '.data | {job_id,status,diff:.candidate.diff_summary,rationale:.candidate.rationale,evaluation:(.eval_results|{provider,simulated,n_examples,eval_dataset_version,gate})}'
curl -s -b $JAR $B/jobs/$JOB | jq -r '.data.candidate | .content'      # the candidate prompt; compare with .baseline_content
```

```text
{ "job_id": "RFN-...", "status": "candidate_ready", "diff": "+3 / -0 lines",
  "rationale": "[fake] applying MetaPrompt pattern",
  "evaluation": { "provider": "fake", "simulated": true, "n_examples": 44, "eval_dataset_version": 45,
                  "gate": { "passed": true, "reasons": [], "thresholds": { "min_aggregate_score": 0.85, "max_regression_delta": 0.02 } } } }
```

**7. Failure paths.** A gate set above the simulated `0.94` blocks its candidate; a viewer cannot apply:

```bash
STRICT=$(curl -s -b $JAR -X POST $B/prompts/calibration/runs -H 'content-type: application/json' \
  -d "{\"agent_id\":\"cookbook-01-intake-classifier\",\"eval_dataset_id\":\"$DS\",\"optimizer_type\":\"MetaPrompt\",\"scorers\":[{\"name\":\"Guidelines\",\"config\":$GUIDE}],\"gate\":{\"min_aggregate_score\":0.99}}" | jq -r .data.job.job_id)
until [ "$(curl -s -b $JAR $B/jobs/$STRICT | jq -r .data.status)" = rejected ]; do sleep 2; done
curl -s -b $JAR $B/jobs/$STRICT | jq -c '.data|{status,gate:.eval_results.gate}'
curl -s -b $JAR -X POST $B/jobs/$STRICT/apply -H 'content-type: application/json' -d '{}'          # not candidate_ready: 409

curl -s -b $JAR -X POST $B/auth/accounts -H 'content-type: application/json' -d '{"user_id":"cookbook01-viewer","password":"cookbook01-viewer-pass"}'
curl -s -c $CALIBER_STATE_DIR/viewer.jar -X POST $B/auth/login -H 'content-type: application/json' -d '{"user_id":"cookbook01-viewer","password":"cookbook01-viewer-pass"}' >/dev/null
curl -s -b $CALIBER_STATE_DIR/viewer.jar -X POST $B/jobs/$JOB/apply -H 'content-type: application/json' -d '{}'   # viewer: 403
```

```text
{"status":"rejected","gate":{"passed":false,"reasons":["overall 0.940 below min_aggregate_score 0.990"],"thresholds":{"min_aggregate_score":0.99,"max_regression_delta":0.02}}}
{"detail":"job 'RFN-...' cannot be applied: current status is 'rejected' (expected 'candidate_ready')","status_code":409}
{"detail":"missing required scope; expected one of ['caliber.operator'], have ['caliber.viewer']","status_code":403}
```

**8. Apply, explicitly.** Nothing is applied until you make this call (calibration only produces the candidate):

```bash
curl -s -b $JAR -X POST $B/jobs/$JOB/apply -H 'content-type: application/json' -d '{}' | jq -c '.data|{job_id,status,artifact_ref:.promotion.artifact_ref,version:.promotion.details.version,version_before:.promotion.details.version_before}'
curl -s -b $JAR -X POST $B/jobs/$JOB/apply -H 'content-type: application/json' -d '{}'               # again: 409, already applied
```

```text
{"job_id":"RFN-...","status":"applied","artifact_ref":"prompts:/cookbook-01-intake-classifier/3","version":3,"version_before":1}
{"detail":"job 'RFN-...' cannot be applied: current status is 'applied' (expected 'candidate_ready')","status_code":409}
```

**9. Verify the release in the registry, on the live board, the timeline and the audit log:**

```bash
# the MLflow registry itself, not CALIBER: the alias must point at v3
curl -s "$MLFLOW_TRACKING_URI/ajax-api/2.0/mlflow/registered-models/alias?name=cookbook-01-intake-classifier&alias=prod" | jq -c '.model_version|{name,version}'
curl -s -b $JAR $B/prompts/cookbook-01-intake-classifier/versions | jq -c '.data[]|{version,aliases,current,provenance}'
curl -s -b $JAR $B/releases/live | jq -c '.data[]|select(.artifact_id=="cookbook-01-intake-classifier")'
curl -s -b $JAR "$B/releases/timeline?entity_type=prompt&limit=5" | jq -c '.data[]|select(.entity_id=="cookbook-01-intake-classifier")|{action,actor,from:.details.from_version,to:.details.to_version,gate_state:.details.gate_state,eval_simulated:.details.eval_simulated}'
curl -s -b $JAR "$B/audit-log?entity_id=cookbook-01-intake-classifier&action=promote_prompt" | jq -c '.data.entries[]|{action,actor}'
curl -s -b $JAR "$B/audit-log?entity_id=$JOB&action=apply_candidate" | jq -c '.data.entries[]|{action,actor,entity_id}'
```

```text
{"name":"cookbook-01-intake-classifier","version":"3"}
{"version":3,"aliases":["prod"],"current":true,"provenance":{"approval_id":"AP-...","job_id":"RFN-...","eval_dataset_id":"ED-...","eval_dataset_version":"45","eval_provider":"fake","eval_simulated":"true"}}
{"version":2,"aliases":[],"current":false,"provenance":{}}
{"version":1,"aliases":[],"current":false,"provenance":{}}
{"artifact_type":"prompt","artifact_id":"cookbook-01-intake-classifier","artifact_name":"cookbook-01-intake-classifier","alias":"prod","version_id":"v3","since":"...","by":"admin"}
{"action":"promote_prompt","actor":"admin","from":1,"to":3,"gate_state":"pass","eval_simulated":"true"}
{"action":"promote_prompt","actor":"admin","from":null,"to":1,"gate_state":"none","eval_simulated":null}
{"action":"promote_prompt","actor":"admin"}
{"action":"promote_prompt","actor":"admin"}
{"action":"apply_candidate","actor":"admin","entity_id":"RFN-..."}
```

**10. Roll back and verify the alias returned:**

```bash
curl -s -b $JAR -X POST $B/prompts/cookbook-01-intake-classifier/rollback -H 'content-type: application/json' -d '{"alias":"prod"}' | jq -c .data
curl -s "$MLFLOW_TRACKING_URI/ajax-api/2.0/mlflow/registered-models/alias?name=cookbook-01-intake-classifier&alias=prod" | jq -c '.model_version|{name,version}'
curl -s -b $JAR $B/releases/live | jq -c '.data[]|select(.artifact_id=="cookbook-01-intake-classifier")|{version_id,by}'
```

```text
{"name":"cookbook-01-intake-classifier","alias":"prod","version":1,"rolled_back_from":3,"operation_id":"REL-...","release_status":"applied"}
{"name":"cookbook-01-intake-classifier","version":"1"}
{"version_id":"v1","by":"admin"}
```

### Run the evaluation with a stand-in model

CALIBER's scorecard needs a model that answers. Without a key you can still watch a prompt being evaluated and a regression being caught, by pointing CALIBER at
[`assets/stand-in-model/server.py`](assets/stand-in-model/server.py), a standard-library HTTP server that speaks just enough of OpenAI's `/v1/chat/completions`.
It reads the prompt version CALIBER sends, classifies the message with a few English keywords, and answers with the **bare JSON record** if the prompt says
"JSON ONLY" (v1), or with a chatty sentence and a markdown code fence if it does not (v2). The only difference between the two evaluated runs is the prompt text.
That proves the pipeline (test set → prompt version → scorer → stored per-example answers → comparison); it proves nothing about a real model, whose accuracy and failure
modes will differ. The stand-in does not read other languages, for instance, so its multilingual rows miss: 37 of 44 labels match on v1.

```bash
python3 docs-site/cookbooks/01-prompt-regression-lab/assets/stand-in-model/server.py --port 8765 &
CALIBER_LLM_PROVIDER=openai OPENAI_API_KEY=stand-in OPENAI_BASE_URL=http://127.0.0.1:8765/v1 CALIBER_EVAL_PROVIDER=mlflow \
  scripts/cookbook-env.sh up lab01eval --flags cookbooks --real-llm
source .tmp/cookbook-env/lab01eval/env.sh     # a second stack, so lab01 keeps its simulated model
curl -s $CALIBER_API/readiness | jq -c '.data|{providers,simulated}'
```

Mint a token as above (against this stack's `$CALIBER_API`), then run the script with `--skip-release` (calibration needs an optimizer model, which the stand-in is not) and
`--expect-regression` (the stand-in is deterministic, so the script may also assert that the weaker v2 breaks the contract on every answer; a real model does not, see
[Run it with a real model](#run-it-with-a-real-model)):

```bash
PYTHONPATH=sdk/caliber-sdk caliber/.venv/bin/python \
  sdk/caliber-sdk/examples/cookbooks/cookbook_01_trustworthy_intake_classifier.py --skip-release --expect-regression
```

The script reads `GET /readiness`, sees that the model is not simulated, and runs the evaluation instead of verifying the refusal. Expected (the contract checks are the deterministic rule
checks; `token_f1` is the platform's built-in scorer; `info` lines are reported, not asserted):

```text
ok   cookbook 01 is listed with guided steps  [11 steps]
ok   v1 is live in the MLflow registry
ok   every row of the file was imported  [44/44]
ok   v2 registered
ok   v2 is NOT live (registering is not deploying)
ok   evaluation baseline v1 completed on every example  [EVR-...]
ok   evaluation candidate v2 (weaker) completed on every example  [EVR-...]
ok   v1 answers are valid JSON (the contract)  [44/44, at least 39 needed]
ok   v1 uses only allowed intents and priorities  [44/44 intents, 44/44 priorities]
ok   v1 has exactly the schema keys
ok   the platform scorecard scored both runs (token_f1 in 0..1)  [token_f1 0.5354 -> 0.2671, pass rate 0.7727 -> 0.0]
ok   the weaker v2 does not beat the baseline on the scorecard  [token_f1 0.535 -> 0.267, pass rate 0.77 -> 0.00]
info v2 broke the contract on 44/44 answers; JSON-valid v1 44/44, v2 0/44; labels agreeing v1 37/44, v2 0/44
info regression gate (allowed pass-rate drop 0.05): FAILS v2 (actual drop 0.77)
ok   v2 breaks the JSON contract on every answer  [0/44 valid]
ok   every example that met the contract on v1 regressed on v2  [44]
ok   the platform scorecard drops against the baseline  [token_f1 0.535 -> 0.267]
ok   the regression gate fails v2  [allowed drop 0.05, actual 0.77]
ok   evaluating a missing test set is a 404  [[404] eval dataset 'ED-does-not-exist' not found (POST http://127.0.0.]

17/17 checks passed
```

The rule checks the script applies to every stored answer (`prediction`), with no model involved:

| Check | v1 (strict) | v2 (weaker) |
| --- | --- | --- |
| `valid_json`: the whole answer parses as one JSON object | 44/44 | 0/44 (prose and a code fence) |
| `allowed_intent_values` and `allowed_priority_values` | 44/44 | 0/44 (nothing parsed) |
| `schema_keys`: exactly `intent, priority, confidence, needs_review, reason` | 44/44 | 0/44 |
| `label_agreement`: intent, priority and `needs_review` equal the labelled expectation | 37/44 (the stand-in's misses) | 0/44 |

CALIBER's built-in scorers are text-level (`exact_match`, `token_f1`, `contains_expected`, `non_empty`); none parses JSON, so the contract checks live in the script, not in the scorecard.
`token_f1` is a monotone stand-in for "closer to the expected record" and is what the baseline comparison in the UI shows. The regression gate is the one a CI job would use:
fail the candidate if its pass rate falls more than 0.05 below the baseline's.

## Run it with a real model

This is the lab on a model that really answers: every evaluation, the optimizer, the judge and the answers the prompt produces come from a language model, and `/readiness`
reports `"simulated": []`. Two variants were run and verified, through the API and through the browser: a **hosted** provider (below, the main one) and a **local** model
([free alternative](#run-it-with-a-local-model-ollama-verified)). What changes compared with the simulated tier:

- **Evaluations** stops answering `400 evaluation requires a real LLM provider` and scores both prompt versions on all 44 cases.
- **Calibration** produces the real optimizer's candidate (often *no change*, on a strong model), evidence with **measured** baseline and candidate scores (`provider: mlflow`,
  `simulated: false`, no "Simulated evaluation" banner), and a gate that is decided by the judge's scores, so it can honestly say no.
- Apply, the registry alias, the live board, the timeline, the audit log and rollback are the same code as on the simulated tier (they were already real).

### Run it with a hosted provider (verified)

Verified on OpenAI: `gpt-5.6-luna` (the default) answers the prompts and runs the optimizer and the diagnosis, and MLflow's default judge, `gpt-4.1-mini`, scores the calibration
with the `Guidelines` scorer. Nothing but your key is configured.

```bash
export OPENAI_API_KEY=<your key>       # in your own shell only; never commit it or paste it into a file the repository tracks
scripts/cookbook-env.sh up lab01h --flags cookbooks --real-llm         # CALIBER_LLM_PROVIDER=openai, CALIBER_EVAL_PROVIDER=mlflow, model gpt-5.6-luna
source .tmp/cookbook-env/lab01h/env.sh
curl -s $CALIBER_API/readiness | jq -c '.data|{ready,simulated,all_real}'      # {"ready":true,"simulated":[],"all_real":true}
```

(From this checkout `cookbook-env.sh` also falls back to the gitignored repository-root `.env`, reading only `OPENAI_API_KEY` and `ANTHROPIC_API_KEY` from it and never printing or writing them.)
Mint a token as in [Run it through the API](#run-it-through-the-api), then run the script. The calibration gate is the product's (`0.85` / `0.02`), so a real judge may say no;
`--accept-rejection` makes that a verified outcome (exit 0, nothing applied) instead of a failure:

```bash
export CALIBER_BASE_URL=$CALIBER_URL
PYTHONPATH=sdk/caliber-sdk caliber/.venv/bin/python \
  sdk/caliber-sdk/examples/cookbooks/cookbook_01_trustworthy_intake_classifier.py --accept-rejection
```

Real captured output (trimmed; ids and every score will vary, the `ok` lines must not). 4 min 46 s on the test machine; this run's candidate cleared the gate:

```text
ok   cookbook 01 is listed with guided steps  [11 steps]
ok   v1 is live in the MLflow registry
ok   every row of the file was imported  [44/44]
ok   v2 registered
ok   v2 is NOT live (registering is not deploying)
ok   evaluation baseline v1 completed on every example  [EVR-...]
ok   evaluation candidate v2 (weaker) completed on every example  [EVR-...]
ok   v1 answers are valid JSON (the contract)  [44/44, at least 39 needed]
ok   v1 uses only allowed intents and priorities  [44/44 intents, 44/44 priorities]
ok   v1 has exactly the schema keys
ok   the platform scorecard scored both runs (token_f1 in 0..1)  [token_f1 0.3715 -> 0.3688, pass rate 0.0227 -> 0.0227]
ok   the weaker v2 does not beat the baseline on the scorecard  [token_f1 0.371 -> 0.369, pass rate 0.02 -> 0.02]
info v2 broke the contract on 0/44 answers; JSON-valid v1 44/44, v2 44/44; labels agreeing v1 30/44, v2 31/44
info regression gate (allowed pass-rate drop 0.05): does not fire (actual drop 0.00)
ok   evaluating a missing test set is a 404  [[404] eval dataset 'ED-does-not-exist' not found (POST http://127.0.0.1:...]
ok   calibration reached candidate_ready  [candidate_ready]
ok   the optimizer returned a candidate  [0 / 0 lines]
info the optimizer proposed no edit (the candidate equals the live prompt); applying it would register an identical version
ok   the evidence reports the real test-set size and version  [44 examples, dataset v45]
ok   the gate passed
ok   the evidence is real: not marked simulated, with measured scores  [provider=mlflow simulated=False baseline 0.9545 -> candidate 0.9545]
ok   a viewer cannot apply (403)  [[403] missing required scope; expected one of ['caliber.operator'], ha]
ok   apply registered a new version  [v3]
ok   the live alias moved in the MLflow registry  [prod -> v3]
ok   the registry version carries the job and the evaluation provenance  [job RFN-..., eval_simulated=false]
ok   the live board shows the new version, applied by you  [v3 by admin]
ok   the release timeline shows the promotion by you  [v1 -> v3 by admin]
ok   the audit log has the promotion and the apply, both by you
ok   applying the same job twice is a 409  [[409] job 'RFN-...' cannot be applied: current status is 'applied]
ok   rollback restores v1
ok   the live alias returned to v1 in the MLflow registry  [prod -> v1]
ok   the live board shows v1 again
ok   the audit log records the rollback by you

30/30 checks passed
```

What is asserted is **invariants**, never an exact value: both evaluations `completed` on all 44 examples, at least 90% of v1's answers parse as the contract, scores in `0..1`, the weaker v2 does not
*clearly* beat the baseline (a margin of 0.05, about two of the 44 rows, because the text-overlap scorecard wobbles and a strong model answers the weaker wording just as well), the candidate is
returned, the evidence has `44` examples, dataset version `45`, `simulated: false` and measured scores in `0..1`, and the registry version, the timeline row and the audit row say `eval_simulated: false`.
The simulated-only checks (the `400` refusal, the `simulated` markers, the 0.99 gate that always blocks) are skipped automatically, because a real judge could score 1.0.

If the gate says no instead (`regression gate failed: ...`), `--accept-rejection` verifies the rejection and stops: `the gate rejected the candidate and says why`, `the rejection rests on real evidence`, the
Apply is a `409`, exit `0`, and it prints that nothing was applied. (That branch is covered by the SDK stand-in tests; the live rejections below were observed through the API and the browser spec, not through
the script.) To exercise Apply, the release screens and rollback regardless of the judge, loosen only the regression allowance for that run: `COOKBOOK_01_MAX_REGRESSION=0.25`.

The browser path is the same spec as every other tier; it reads `/readiness` and follows the real branch (4.0 minutes with the loosened allowance; 4.1 minutes when the gate rejected the candidate, in which
case it verifies the rejection and stops):

```bash
cd caliber/caliber-ui
COOKBOOK_01_MAX_REGRESSION=0.25 CALIBER_LAB_BASE_URL="$CALIBER_URL/caliber" \
  npx playwright test --config=playwright.labs.config.ts e2e/cookbooks/labs/01-prompt-release-loop.spec.ts
```

#### Results on the hosted model (real; will vary)

| What | Observed | What it means |
| --- | --- | --- |
| v1 (strict prompt), 44 answers | 44/44 valid JSON with the five keys, every run; intent, priority and `needs_review` all equal the label on **30 or 31 of 44**; `token_f1` `0.37` to `0.38`, pass rate `0.02` to `0.05` (1 to 2 of 44) | The contract holds. The labels are strict (all three fields must agree) and several rows are ambiguous or adversarial on purpose, and `token_f1` also penalises the free-text `reason`, so the scorecard is low even where the labels agree. |
| v2 (weaker prompt), 44 answers | **Still 44/44 valid JSON** in all three runs (the "JSON only" rules were dropped and the model answers in JSON anyway); labels agree on 30 or 31; `token_f1` `0.36` to `0.37` | The regression the stand-in shows (prose and a code fence, 0/44 valid) does **not** reproduce on a strong model. Across three runs v2's `token_f1` was `-0.003`, `-0.016` and `-0.024` below v1's, and in one run one more row passed than on v1. Do not read this as proof the v2 wording is safe: it is the honest finding that this classifier is robust to that edit, and the scorecard cannot tell the two apart beyond noise. |
| Calibration, product gate (`0.85` / `0.02`), `Guidelines` judge, five completed runs | Candidate **unchanged** (`+0 / -0 lines`) in four of five; baseline `0.9318`, `0.9545`, `0.9773`, `0.9545`, `0.8636`, candidate `0.8636`, `0.9773`, `0.9545`, `0.9545`, `0.9545`. Outcomes: `rejected` (`regression gate failed: dimension 'Guidelines' regressed by -0.068 (max allowed -0.020)`), `candidate_ready`, `rejected` (`... regressed by -0.023 ...`), `candidate_ready`, `candidate_ready` (loosened allowance) | The optimizer found nothing to fix in a prompt that already scores `0.86` to `0.98` and mostly returns it unchanged, so the **same prompt** was scored `0.8636` and `0.9545`, or `0.9318` and `0.8636`: the judge's run-to-run noise (`0.02` to `0.09` on 44 rows) is as large as or larger than the gate's `0.02` allowance. The first rejection is therefore an identical prompt "regressing" by `-0.068`. On a prompt this good the gate's verdict is unreliable in both directions, which is why a candidate is read, not trusted. |
| The one candidate that did change | `+3 / -0 lines`: *"If a verification or diagnostic report provides no observed failure, expected behavior, or trace evidence, do not infer a component or root cause; set `intent: unknown`, `needs_review: true`..."* (rejected by the gate, `-0.023`) | A rule that has nothing to do with a support desk: the diagnosis step has no failure evidence to work from and the model invented a theme. A human reviewer would reject it too; this is what the review step is for. |
| Apply of an unchanged candidate | Allowed: v3 registers, the alias moves, every release check passes (`candidate_ready` runs 2, 4 and 5) | CALIBER does not stop a no-op candidate: Apply of `+0 / -0 lines` registers a version identical to v1. The script prints `info the optimizer proposed no edit` so you notice. |

Time and cost, per run on one laptop over the public internet (the provider answers one call at a time per example, about 1.7 seconds each): one evaluation of 44 answers about 1 minute 40 seconds; one calibration
run 30 to 60 seconds in total (triage, diagnosis and candidate, then the eval stage's baseline pass and candidate pass of 44 answers plus 44 judge calls each, about 25 seconds); the whole script 4 min 46 s; the browser spec 4.0 to 4.1 minutes.
About 90 model calls for the two evaluations and about 180 for one calibration (88 answers on `gpt-5.6-luna`, 88 judge calls on `gpt-4.1-mini`, a handful for triage, diagnosis and the candidate): roughly 270 small calls in all,
the same count as the local run. I did not read the provider's billing, so there is no dollar figure: multiply by your model's price, and do not loop the script.

Failure scenarios, each provoked on a throwaway stack (the failed evaluation is a `201` with `status: failed` and the error, within seconds, never a hang; the script prints `evaluation failed: <error>` and exits 1):

| Provoked by | What the API / script says |
| --- | --- |
| A key the provider rejects (`OPENAI_API_KEY=sk-invalid cookbook-env.sh up lab01hbad --flags cookbooks --real-llm`) | `FAIL evaluation baseline v1 completed on every example  [evaluation failed: Error code: 401 - {'error': {'message': 'Incorrect API key provided: sk-invalid. You can find your API key at https://platform.openai.com/account/api-keys.', 'type': 'invalid_request_error', 'code': 'invalid_api_key', ...}}]`. A genuine rejected key is echoed in the same message **masked** (`sk-proj-****...`): do not paste it. |
| A model name the account cannot use (`CALIBER_LLM_DIAGNOSIS_MODEL=gpt-nonexistent`) | `FAIL evaluation baseline v1 completed on every example  [evaluation failed: Error code: 404 - {'error': {'message': 'The model `gpt-nonexistent` does not exist or you do not have access to it.', ..., 'code': 'model_not_found'}}]` |
| Not provoked | A `429` rate limit and a provider timeout were not provoked (they cannot be on demand). Both surface the same way, as a `failed` run with the provider's error; the server retries transient errors itself before failing. |

A defect this tier exposed, now fixed: the first hosted calibration stopped without an error and the janitor failed it five minutes later as `heartbeat stale ... worker presumed crashed during stage 'eval'`. The
evaluation threads of `mlflow.genai.evaluate` deadlocked on Python import locks when the provider answered fast (a slower local model hides it). `MLflowEvalProvider` now resolves those imports on the calling thread first
(`caliber/src/caliber/eval/mlflow_runner.py`, `_prime_mlflow_lazy_imports`, with unit tests); the same calibration then finished in under a minute. A second fix: the Runs stage filed a run under the version the page opened with, not the live one
after **Save & promote** (it now uses the workspace's live version).

### Run it with a local model (Ollama, verified)

This is the free alternative to the hosted tier, also run and verified: a local `qwen2.5:7b` through [Ollama](https://ollama.com)'s OpenAI-compatible endpoint. It is free, needs no key and no network, and is slow.
A 7B model is weak, so what the lab shows is the **machinery** working on real model output, not a good classifier (see "Results" below).

```bash
ollama pull qwen2.5:7b
curl -s http://127.0.0.1:11434/v1/models | jq -r '.data[].id'                      # qwen2.5:7b
# the first call loads the model (about 1m45s cold on the test machine); warm it and keep it loaded:
curl -s http://127.0.0.1:11434/api/generate -d '{"model":"qwen2.5:7b","prompt":"OK","stream":false,"keep_alive":"2h"}' | jq -r .response
```

Start a stack whose **every** model setting points at Ollama (nothing may fall back to `api.openai.com` or to the default `gpt-5.6-luna`):

```bash
export OPENAI_API_KEY=ollama                                   # any non-empty value; Ollama ignores it
export OPENAI_BASE_URL=http://127.0.0.1:11434/v1               # the OpenAI Python client (answers, triage, diagnosis, candidate)
export OPENAI_API_BASE=http://127.0.0.1:11434/v1               # the name LiteLLM-based clients (MLflow's judge) read; both are set, each was not tested alone
export CALIBER_LLM_PROVIDER=openai CALIBER_EVAL_PROVIDER=mlflow CALIBER_ASSISTANT_ENGINE=fake
export CALIBER_LLM_DIAGNOSIS_MODEL=qwen2.5:7b                  # answers being evaluated, triage, diagnosis, the MetaPrompt candidate
export CALIBER_GEPA_REFLECTION_MODEL=openai:/qwen2.5:7b        # only used if you pick GEPA; set so nothing defaults to gpt-5.6-luna
export MLFLOW_GENAI_JUDGE_DEFAULT_MODEL=openai:/qwen2.5:7b     # the judge behind the Guidelines scorer
scripts/cookbook-env.sh up lab01l --flags cookbooks --real-llm
source .tmp/cookbook-env/lab01l/env.sh
curl -s $CALIBER_API/readiness | jq -c '.data|{ready,simulated,all_real}'          # {"ready":true,"simulated":[],"all_real":true}
```

A non-empty `OPENAI_API_KEY` in your environment means the script never reads the repository's `.env`. `scripts/cookbook-env.sh restart lab01l` starts the server again
with the variables **of the shell you run it in**, so export them again first. After a run, confirm nothing went to the hosted API:
`grep -c api.openai.com $CALIBER_SERVER_LOG` prints `0`, and Ollama's log (`/opt/homebrew/var/log/ollama.log` for a Homebrew install) lists every request
(`POST /v1/chat/completions` for answers and judge, `POST /v1/responses` for the optimizer's agents).

Mint a token as above, then run the same script as everywhere else. `COOKBOOK_01_MIN_SCORE` and `COOKBOOK_01_MAX_REGRESSION` loosen the calibration gate **for this small, noisy judge only**
(the hosted tier keeps the product defaults, `0.85` and `0.02`; see "Results" for why this one does not):

```bash
export CALIBER_BASE_URL=$CALIBER_URL
COOKBOOK_01_MIN_SCORE=0.3 COOKBOOK_01_MAX_REGRESSION=0.25 PYTHONPATH=sdk/caliber-sdk caliber/.venv/bin/python \
  sdk/caliber-sdk/examples/cookbooks/cookbook_01_trustworthy_intake_classifier.py
```

Real captured output (trimmed; ids, timestamps and every score will vary, the `ok` lines must not). About 7 minutes on the test machine:

```text
ok   cookbook 01 is listed with guided steps  [11 steps]
ok   v1 is live in the MLflow registry
ok   every row of the file was imported  [44/44]
ok   v2 registered
ok   v2 is NOT live (registering is not deploying)
ok   evaluation baseline v1 completed on every example  [EVR-...]
ok   evaluation candidate v2 (weaker) completed on every example  [EVR-...]
ok   v1 answers are valid JSON (the contract)  [44/44, at least 39 needed]
ok   v1 uses only allowed intents and priorities  [44/44 intents, 44/44 priorities]
ok   v1 has exactly the schema keys
ok   the platform scorecard scored both runs (token_f1 in 0..1)  [token_f1 0.4276 -> 0.3501, pass rate 0.2045 -> 0.0227]
ok   the weaker v2 does not beat the baseline on the scorecard  [token_f1 0.428 -> 0.350, pass rate 0.20 -> 0.02]
info v2 broke the contract on 0/44 answers; JSON-valid v1 44/44, v2 44/44; labels agreeing v1 14/44, v2 13/44
info regression gate (allowed pass-rate drop 0.05): FAILS v2 (actual drop 0.18)
ok   evaluating a missing test set is a 404  [[404] eval dataset 'ED-does-not-exist' not found (POST http://127.0.0.]
ok   calibration reached candidate_ready  [candidate_ready]
ok   the candidate differs from the live prompt  [+3 / -0 lines]
ok   the evidence reports the real test-set size and version  [44 examples, dataset v45]
ok   the gate passed
ok   the evidence is real: not marked simulated, with measured scores  [provider=mlflow simulated=False baseline 0.5455 -> candidate 0.5]
ok   a viewer cannot apply (403)  [[403] missing required scope; expected one of ['caliber.operator'], ha]
ok   apply registered a new version  [v3]
ok   the live alias moved in the MLflow registry  [prod -> v3]
ok   the registry version carries the job and the evaluation provenance  [job RFN-..., eval_simulated=false]
ok   the live board shows the new version, applied by you  [v3 by admin]
ok   the release timeline shows the promotion by you  [v1 -> v3 by admin]
ok   the audit log has the promotion and the apply, both by you
ok   applying the same job twice is a 409  [[409] job 'RFN-...' cannot be applied: current status is 'applied]
ok   rollback restores v1
ok   the live alias returned to v1 in the MLflow registry  [prod -> v1]
ok   the live board shows v1 again
ok   the audit log records the rollback by you

30/30 checks passed
```

The browser path is the same spec as the simulated tier; it reads `/readiness` and follows the real branch (it passed in 6.8 minutes). The two environment variables are read by the spec too:

```bash
cd caliber/caliber-ui
COOKBOOK_01_MIN_SCORE=0.3 COOKBOOK_01_MAX_REGRESSION=0.25 CALIBER_LAB_BASE_URL="$CALIBER_URL/caliber" \
  npx playwright test --config=playwright.labs.config.ts e2e/cookbooks/labs/01-prompt-release-loop.spec.ts
```

What differs from the simulated tier is asserted as **invariants**: both evaluations `completed` on all 44 examples, at least 90% of v1's answers parse as the contract, scores are in `0..1`,
the weaker v2 does not beat the baseline on the scorecard, the evidence has `44` examples, dataset version `45`, `simulated: false` and a measured baseline and candidate score, and the registry
version, the timeline row and the audit row say `eval_simulated: false`. The simulated-only checks (the `400` refusal, the `simulated` markers, the 0.99 gate that always blocks) are skipped
automatically, because a real judge could score 1.0.

#### Results on `qwen2.5:7b` (real; will vary)

Honest reading first: **a 7B model is a weak classifier and a noisy judge, and this lab does not hide it.**

| What | Observed | What it means |
| --- | --- | --- |
| v1 (strict prompt), 44 answers | 44/44 valid JSON with the five keys; intent, priority and `needs_review` all equal the label on **14/44**; `token_f1` `0.428`, pass rate `0.20` (9/44) | The contract holds. Accuracy is poor: it lacks the judgement the prompt asks for (injections, ambiguous, multilingual rows). |
| v2 (weaker prompt), 44 answers | **Still 44/44 valid JSON** (the "JSON only" rules were dropped and this model answers in JSON anyway); labels agree on 13/44; `token_f1` `0.350`, pass rate `0.02` (1/44) | The regression the stand-in shows (prose and a code fence, 0/44 valid) does **not** reproduce on this model. The scorecard still drops (wordier `reason` text lowers the text-overlap score) and the pass-rate gate (`0.05`) fires, but "v2 broke the contract" is not what happened. Do not read this as proof the v2 wording is safe. |
| Calibration, default gate (`0.85`), `Guidelines` judge | `rejected`: `regression gate failed: overall 0.568 below min_aggregate_score 0.850` (candidate `0.568`, baseline `0.523`, `44` examples, `simulated: false`) | The honest outcome with the product's own bar: the judge never scores this prompt anywhere near `0.85`. The candidate's diff was `+10 / -0 lines` and added `Consider relevant patient history from past interactions...`, a rule that has nothing to do with a support desk: the diagnosis step has no failure evidence to work from and a 7B model hallucinated one. A human reviewer would reject it, and this is the kind of candidate the review step exists to stop. |
| Calibration, gate `0.5` | `candidate_ready`: candidate `0.523`, baseline `0.386` (`+0.136`) | Same prompt, same judge, but the **baseline** scored `0.523` in the run above and `0.386` here. |
| Calibration, gate `0.3` / `0.25` (the script and spec) | `candidate_ready`: baseline `0.545` → candidate `0.500` (`-0.045`), diff `+3 / -0 lines`, Apply, rollback and all release checks pass | The candidate is *not better* than the live prompt by the judge's own numbers; it clears only the loosened gate. |

Why the gate is loosened for this tier: the same baseline prompt scored `0.523`, `0.386` and `0.545` on three runs (the judge, not the answers, varies: `token_f1` was identical on repeated runs,
temperature 0), so a `max_regression_delta` of `0.02` rejects or accepts a candidate by chance. With the defaults the run ends `rejected` with a real reason; with `0.3` / `0.25` it reaches a candidate,
and the rest of the journey (review, Apply, registry, live board, timeline, audit, rollback) is exercised on **real, non-simulated** evidence. Do not apply a candidate on the strength of these numbers.

Time and cost: no money (local). Per run on an Apple-silicon laptop under other load: one evaluation of 44 answers about 45 seconds; one calibration run 5 to 8 minutes (about `270` model calls
in the script's whole run: `88` for the two evaluations, `88` answers and `88` judge calls for the baseline and the candidate, three optimizer calls); the whole script about 7 minutes; the browser spec about 7.

Failure scenarios, each provoked on a throwaway stack (the failed evaluation is a `201` with `status: failed` and the error, within seconds, never a hang):

| Provoked by | What the API / script / UI says |
| --- | --- |
| Ollama not reachable (`OPENAI_BASE_URL` pointing at a closed port) | Evaluation run `failed`, `error_message: "Connection error."` (5 seconds); the script prints `FAIL evaluation baseline v1 completed on every example  [evaluation failed: Connection error.]` and exits 1; the Evaluations list shows the run **FAILED** and its page says `This run failed: Connection error.` |
| Wrong model name (`CALIBER_LLM_DIAGNOSIS_MODEL=qwen2.5:99b`) | Evaluation `failed`: `Error code: 404 - ... model 'qwen2.5:99b' not found`; a calibration job `failed` at `diagnosis` with `llm provider error: diagnosis LLM call failed: Error code: 404 ... model 'qwen2.5:99b' not found` |
| A key the provider rejects (`OPENAI_API_KEY=sk-invalid`, no base URL: this one goes to the hosted API on purpose, with a key that is not a real one) | Evaluation `failed`: `Error code: 401 - Incorrect API key provided: sk-invalid ...` (2 seconds) |
| `Correctness` selected as the calibration scorer | Job `failed`: `eval provider error: candidate evaluation produced no valid score for selected scorer(s): Correctness. Check the scorer requirements and eval dataset fields.` followed by a hint that Correctness needs `expected_response` or `expected_facts` and that Guidelines does not (the test set has no `expected_response`) |

### Troubleshooting a real run (hosted or local)

| You see | Cause | Fix |
| --- | --- | --- |
| `Connection error.` on a run, or `connection refused` | Ollama (or the base URL) is not reachable from the CALIBER server | `curl http://127.0.0.1:11434/v1/models`; start Ollama (`ollama serve`); check `OPENAI_BASE_URL` was exported **before** `cookbook-env.sh up` / `restart`. |
| `Error code: 404 ... model '...' not found` | The model name in `CALIBER_LLM_DIAGNOSIS_MODEL` / `MLFLOW_GENAI_JUDGE_DEFAULT_MODEL` is not pulled | `ollama list`; `ollama pull qwen2.5:7b`; the judge model needs the `openai:/` prefix, the others do not. |
| The first call takes minutes | Ollama is loading the model into memory (about 1m45s cold here) | Warm it up first (the `keep_alive` call above). |
| `Error code: 401 ... Incorrect API key` | A hosted run with a key the provider rejects (a revoked, expired or mistyped key), or Ollama's variables were not exported so the call went to `api.openai.com` | Check `echo ${OPENAI_API_KEY:+set}` (never print it); create a fresh key; restart the stack in the shell that exports it (`restart` reads the shell's variables). On the Ollama tier `grep -c api.openai.com $CALIBER_SERVER_LOG` must be `0`. |
| `Error code: 404 ... model ... does not exist` (hosted) | The model name in `CALIBER_LLM_DIAGNOSIS_MODEL` / `MLFLOW_GENAI_JUDGE_DEFAULT_MODEL` is wrong or your project cannot use it | List what your key may use; use the default or a model you have. |
| `429` / rate limit on a hosted run | Too many calls for your tier | Retry later; the server retries transient errors itself (`llm_call_max_attempts`). The lab's calls are sequential, so a low tier is slow, not blocked. |
| A hosted run "hangs", then the calibration job is `failed` with `heartbeat stale ... during stage 'eval'` | Fixed in this checkout (an import-lock deadlock in MLflow's evaluation threads). On an older build: any stage that makes no progress for 5 minutes is failed by the janitor | Update; otherwise start a new run. Also `caffeinate -dimsu` if the machine slept. |
| A timeout on the first call or on a long evaluation | A slow provider or network; the script's client timeout is 900 s | Raise `COOKBOOK_01_HTTP_TIMEOUT`; check the provider's status page. |
| The script dies with `ReadTimeout` | A synchronous evaluation outlasted the client timeout | The script already sets 900 s; raise `COOKBOOK_01_HTTP_TIMEOUT` for a slower machine. |
| A calibration job `failed`, `reaped stale job` in the server log | The machine slept or the server was suspended, the janitor saw no heartbeat for minutes and failed the job | Keep the machine awake (`caffeinate -dimsu`) and start a new run. |
| Calibration ends `rejected` | The judge's score is under the gate, or the candidate lost more than `max_regression_delta` (on a strong model usually judge noise: an unchanged prompt can "regress" by `0.05` to `0.09`) | Read `error_message` and the diff; rerun with `--accept-rejection` to treat it as a verified outcome, loosen `COOKBOOK_01_MAX_REGRESSION` for a release rehearsal, or accept the rejection: it is the honest answer. |
| Baseline scores differ from run to run | A small model is a noisy judge | Do not compare runs by a single score; read the diff. |

## Recipe: run it through the UI

The same journey in the browser, with the labels the app shows (sidebar groups: **Prompt lifecycle** holds Prompts, Test Sets, Evaluations, Releases; **Operate** holds Audit Log).
It is automated by `caliber/caliber-ui/e2e/cookbooks/labs/01-prompt-release-loop.spec.ts`, which reads `GET /readiness` and follows the simulated branch or the real-model branch
(the places where they differ are marked below), and by `01b-evaluation-regression.spec.ts` (the stand-in stack).

1. **Create the prompt.** Prompt lifecycle › **Prompts** › **New prompt** › **Write / paste**. *Prompt name*: `cookbook-01-intake-classifier`; *Prompt text*: the body of `assets/prompts/intake-classifier.md` (below the front matter); **Create and Open**. See: the workspace opens on **Author**; the version history lists **v1** with a **live** badge.
2. **Save the weaker v2 as a draft.** On **Author**, replace *Template* with the body of `assets/prompts/intake-classifier-weak.md`, *Commit message*: `v2 dropped the JSON-only rules (intentional regression)`, then **Save draft** (not *Save & promote*). See: **v2** appears in the history; **v1** keeps the live badge.
3. **Import the test set from the file.** Open the **Calibration** stage. Under *Eval Dataset* choose the file with *Upload JSON/JSONL* (`assets/dataset/intake-classifier.jsonl`), *Dataset name*: `cookbook-01-intake-golden`, then **Upload Dataset**. See: `Uploaded 44 examples to cookbook-01-intake-golden.` and the dataset is selected under *Existing Dataset*.
4. **Check the imported test set.** Prompt lifecycle › **Test Sets**, search `cookbook-01-intake-golden`, open it. See: the list shows the set at **v45**, and opened it lists its examples, e.g. `I was charged twice after upgrading to the Team plan this morning.` and the `Ignore all previous instructions...` injection.
5. **Evaluate a prompt version.** Prompt lifecycle › **Evaluations** › **Run evaluation**: *Test set* `cookbook-01-intake-golden (v45)`, *Label* `baseline v1`, *What to score* **Prompt version**, *Subject under test* `cookbook-01-intake-classifier@1`, turn off **Exact match** and **Contains expected** (leave **Token F1**), **Run**. On the simulated stack you see the red message `evaluation requires a real LLM provider; set CALIBER_LLM_PROVIDER ...`: expected, and the only honest answer. On the stand-in stack or a real model the **Run** button works for as long as the model takes to answer all 44 examples (about 1 minute 40 seconds on the hosted model, about 45 seconds on a local `qwen2.5:7b`), then the run appears in the list as `completed` with a `n/44` examples-passed count (a row passes at a mean score of 0.5; the stand-in gives `34/44`, the hosted model `1/44` to `2/44`, the real 7B model about `9/44`: its answers carry a `reason` the labels do not, so a text-overlap score is low even when the labels agree). If the model cannot be reached the run is listed as `failed` and its page says `This run failed: <the server's error>`.
6. **See the comparison against the baseline** (stand-in stack or real model). Run a second evaluation the same way with *Label* `weaker v2` and *Subject* `...@2`. Open **weaker v2**, choose the baseline under **Compare to**. See: the context box (`Dataset/scorer-compatible comparison: ...`) and `pp` deltas on **Overall** and **Token F1**. With the stand-in the deltas are red, the pass rate falls to 0%, and every prediction starts `Happy to help! Here is how I would route this message:` with a **Fail** result; with a real model v2's predictions are still JSON records (see [Run it with a real model](#run-it-with-a-real-model)) and the difference is small or nil (hosted: token F1 `0.37 → 0.37`, the chips can read `±0` or a point or two either way; 7B model: token F1 `0.428 → 0.350`, pass rate `20% → 2%`). Open **baseline v1**: its predictions are bare `{"intent": "billing", ...}` records.
7. **Calibrate and wait for the candidate.** Back on the prompt's **Calibration** stage: *Calibration dataset* `cookbook-01-intake-golden`, *Calibration strategy* **MetaPrompt**. Under *Scoring* untick **Correctness**, **Relevance** and **Safety**, tick **Guidelines** and paste the contents of `assets/calibration/guidelines.json` into its config box (see [step 5 of the API path](#the-same-steps-by-hand-with-curl) for why not Correctness). Simulated stack: first set *Minimum aggregate score* to `0.99` and **Start Calibration Run**: the run ends **rejected** and has no **Review & apply**, the gate blocked it. Set the score back to `0.85` and **Start Calibration Run** again. See: within seconds a run with **Review & apply**. Hosted model: leave the gate at its defaults (`0.85` and `0.02`), **Start Calibration Run** and wait 30 to 60 seconds: the run either gets **Review & apply**, or is listed as **rejected** with the judge's reason and offers nothing to apply (the judge's noise can reject an unchanged prompt; that is an honest end of the lab, see [Run it with a hosted provider](#run-it-with-a-hosted-provider-verified)); to rehearse Apply and rollback anyway set *Maximum regression delta* to `0.25` first and run again. Local 7B model: lower *Minimum aggregate score* to `0.3` and *Maximum regression delta* to `0.25` first (see [Run it with a local model](#run-it-with-a-local-model-ollama-verified) for why), then **Start Calibration Run** and wait about 5 to 8 minutes for **Review & apply**.
8. **Review the evidence and the diff, then Apply.** **Review & apply**. See the dialog **Review candidate before applying**: the prompt diff (simulated: `+3 / -0 lines`; hosted: usually `+0 / -0 lines`, the optimizer had nothing to add), `44 evaluation examples`, **Gate: Passed**, and either the banner **Simulated evaluation — not evidence** (simulated stack) or no banner and measured **Baseline**, **Candidate** and **Change** scores (real model). Then **Apply candidate** (the button is the explicit decision; closing the dialog applies nothing). See: `Candidate applied.`; on **Author** the live badge has moved to **v3**.
9. **Verify the release on Releases.** Prompt lifecycle › **Releases**. *What's live now* has a `prompt` row `cookbook-01-intake-classifier  v3  @prod ... · admin`. The **Timeline** lists `Promote  prompt  cookbook-01-intake-classifier  v1 → v3 ... admin`.
10. **Verify the audit log.** Operate › **Audit Log**, *Entity ID* `cookbook-01-intake-classifier`. See `promote_prompt` rows by `admin`; the newest carries `"eval_simulated":"true"` in its details (`"false"` on a real model).
11. **Roll back.** Prompt lifecycle › **Prompts** › open the prompt › **Author**, on the **v3** row **Roll back**, confirm. See: the live badge returns to **v1**. On **Releases** the live row says `v1` and the timeline's newest entry is `Roll back ... v3 → v1 ... admin`; the audit log shows `rollback_prompt` by `admin`.

## Verify it worked

| Check | How | Expected |
| --- | --- | --- |
| Test set imported | `curl -s -b $JAR $B/eval-datasets/$DS/examples \| jq '.data\|length'` | `44` (dataset version `45`) |
| v2 not live | registry alias before Apply | `prod` → `1` |
| Evidence is marked simulated | `curl -s -b $JAR $B/jobs/$JOB \| jq .data.eval_results` | simulated stack: `provider: "fake"`, `simulated: true`, `n_examples: 44`; real model: `provider: "mlflow"`, `simulated: false`, `n_examples: 44`, measured `baseline` and `candidate` scores |
| Gate blocks | strict job | `status: "rejected"`, Apply `409` |
| Apply is gated by role | viewer's `POST /jobs/$JOB/apply` | `403 missing required scope` |
| Alias moved | `GET $MLFLOW_TRACKING_URI/ajax-api/2.0/mlflow/registered-models/alias?name=...&alias=prod` | `version: "3"` |
| Who applied it | live board `by`, timeline `actor`, audit `actor` | your user (`admin`) |
| Rolled back | same registry read after `POST .../rollback` | `version: "1"` |
| Everything at once | the one-command script | exit `0`; `26/26 checks passed` (simulated), `30/30` (real model: hosted 4 min 46 s, local about 7 minutes), `17/17` with `--skip-release --expect-regression` (stand-in); on a real model whose gate says no, `--accept-rejection` ends `exit 0` with the rejection verified |
| Browser path | `CALIBER_LAB_BASE_URL="$CALIBER_URL/caliber" npx playwright test --config=playwright.labs.config.ts e2e/cookbooks/labs/01-prompt-release-loop.spec.ts` (from `caliber/caliber-ui`; follows the simulated or the real branch by itself) | `1 passed` (about 12 s simulated, 4 minutes hosted, about 7 minutes on a local 7B model) |
| Evaluation failure is readable | a run against an unreachable model | `status: failed`, `error_message: "Connection error."`; the Evaluations page says `This run failed: Connection error.` |

## Common errors and troubleshooting

| You see | Cause | Fix |
| --- | --- | --- |
| `404` with `reason_code: feature_disabled` on `/cookbooks`, or no Cookbooks entry in the sidebar | `CALIBER_FEATURE_COOKBOOKS` is off (the default) | Export it, restart CALIBER (`scripts/cookbook-env.sh restart lab01`). The prompt, test-set, calibration and release routes work without it; only the gallery needs it. |
| `400 evaluation requires a real LLM provider` | The language model is simulated (`CALIBER_LLM_PROVIDER=fake`) | Expected here. Configure a real provider ([Run it with a real model](#run-it-with-a-real-model)), or use the stand-in model section. |
| A calibration job `failed` with `candidate evaluation produced no valid score for selected scorer(s): Correctness` | `Correctness` (the form's default scorer) needs `expected_response` or `expected_facts` on every example; this test set is labelled `intent`/`priority`/`needs_review` | Untick **Correctness**, **Relevance** and **Safety**, tick **Guidelines** with the config from `assets/calibration/guidelines.json` (API: `"scorers":[{"name":"Guidelines","config":{...}}]`). |
| An evaluation run `failed` with `Connection error.`, a `404 ... model ... not found` or a `401` (and the script says `evaluation failed: <that error>`) | The real model is unreachable, misnamed, or the key is rejected | See [Troubleshooting a real run](#troubleshooting-a-real-run-hosted-or-local). |
| `404 eval dataset '...' not found` | The test-set id does not exist, or is not visible to you | List `GET /eval-datasets`; import the file first (step 2). |
| `403 missing required scope; expected one of ['caliber.operator']` | You are signed in as a viewer | Apply and calibration need the operator role: add the user to `CALIBER_OPERATOR_USERS` or use an operator token. |
| `409 ... current status is 'rejected' (expected 'candidate_ready')` | The candidate missed the gate, or the job was already applied | Read `eval_results.gate.reasons`. Fix the prompt or the test set and start a new calibration run. A rejected job can never be applied. |
| Calibration never leaves `queued` | Background workers are off | The stack from `cookbook-env.sh up` has them on; otherwise set `CALIBER_BACKGROUND_TASKS_ENABLED=true`. |
| `/readiness` lists `promoter` or `artifact_store` under `simulated` | A fake release path | Use `CALIBER_PROMOTER_PROVIDER=mlflow` and `CALIBER_ARTIFACT_STORE_PROVIDER=mlflow`; with the fake promoter Apply does not change MLflow and the registry checks fail. |
| `503` from `/readiness` or the script cannot connect | MLflow or the database is down | Start MLflow first, then CALIBER; see `docs/start/alpha-install.md#if-something-is-wrong`. |
| The score in the review dialog says "Simulated evaluation — not evidence" | By design, with the simulated evaluator | Do not read `0.94` as quality. Use a real provider and `CALIBER_EVAL_PROVIDER=mlflow`. |
| The SDK script fails with a 404 on every call | `CALIBER_BASE_URL` includes `/ajax-api/2.0/mlflow/caliber` | Set it to the server root, `$CALIBER_URL`. |
| `VERIFICATION FAILED: ...` | A documented outcome did not hold | The failed check names itself; the lines above it show what was observed. |
| UI import shows `Uploaded 44 examples` but rows look empty | The file uses `inputs`/`expectations` keys | The importer reads `input` and `expected` (the shape of `assets/dataset/intake-classifier.jsonl`). |

## Appendix: the Aria steps (verified on a hosted model)

The original recipe also used the prompt workspace's assistant-backed steps. They need **two** things the main lab does not: `CALIBER_FEATURE_ARIA=true` (the Playground stage and the assistant-guided
controls only exist with it) and a real model (with the simulated assistant, test generation fails with "LLM did not return a valid JSON array", so they are not offered on the simulated stack and the spec skips itself there).
They are UI-only (they call the assistant's `/assistant/*` sessions; the SDK script does not cover them). Verified, API-free, on the hosted tier:

```bash
export OPENAI_API_KEY=<your key>
scripts/cookbook-env.sh up lab01ha --flags cookbooks,aria --real-llm      # both flags; --real-llm for the assistant engine and the judge
source .tmp/cookbook-env/lab01ha/env.sh
cd caliber/caliber-ui
CALIBER_LAB_BASE_URL="$CALIBER_URL/caliber" \
  npx playwright test --config=playwright.labs.config.ts e2e/cookbooks/labs/01c-aria-appendix.spec.ts      # 1 passed, 1.7 to 2.1 minutes
```

By hand, on a prompt `cookbook-01-aria-...` made as in step 1 of the UI recipe (the spec creates its own, with a timestamp suffix):

1. **Playground (live chat).** Open the prompt, the **Playground** stage, **Start Chat Session**, type `I was charged twice after upgrading to the Team plan this morning.` and send. *Verified:* the header
   counts `2 messages` and the assistant answers (a few seconds). *What it says varies, and this matters:* the chat is the **Aria assistant seeded with your prompt**, not a raw run of it. One run answered with the
   contract's record (`{"intent":"billing","priority":"high","confidence":0.99,"needs_review":false,"reason":"..."}`); another answered `Created the intake-classifier prompt draft with strict JSON-only output ...`, because
   Aria took the prompt as something to author and created an assistant draft. Use the Playground as a quick conversation about the prompt; use **Evaluations** to measure it.
2. **Generate Test Cases, Run Tests & Judge, Save to Test Sets.** The **Test Sets** stage: set *Number of test cases* to `4`, **Generate Test Cases** (the assistant writes `4` cases with an input, an expected behaviour and tags,
   for example `How do I configure SSO for my team?`), **Run Tests & Judge**. *Verified:* each case is answered by the assistant with your prompt and scored by an LLM judge pass / partial / fail; a score summary (`Overall Score`, `Total 4`, `Pass`, `Partial`, `Fail`) and
   `Run saved to history.` appear; **Save to Test Sets** answers `Saved to Test Sets.` A typical run: `3 pass, 1 fail, 75%`; the one failure was `Can you help?` (expected: `intent: unknown`), because the assistant answered as an assistant
   (`Absolutely - what would you like to build or refine? ...`), not as the classifier. The judge is a model, so these verdicts will vary; they are not part of the lab's pass/fail. This path uses *generated* cases, **not** the 44-row
   file: the **Runs** stage reads the `user_message` / `input` key of each example, and the lab's file uses `ticket_text`.
3. **Runs: baseline and "Vs. baseline".** The **Runs** stage, **Run tests** (it runs the saved test set against the live prompt), then **Set as baseline** (a `Baseline` marker appears). To get something to compare, go to **Author**, replace the template with
   `assets/prompts/intake-classifier-weak.md`, **Save & promote** (so the weaker wording is live and Runs scores it), return to **Runs** and **Run tests** again. *Verified:* a **Vs. baseline** panel with the score delta and a list of regressions. Typical: baseline
   `100%` (4 pass) against the weaker prompt `92%` (3 pass, 1 partial), delta `-8` points, no case turned from pass to fail. On a strong model the weaker prompt barely moves the score, exactly as in the main lab. Then **Roll back** the weaker version
   (the spec does).
4. **Assistant-guided calibration.** The **Calibration** stage shows **Assistant-Guided Calibration** with Aria on. Type a request such as `Calibrate cookbook-01-aria-... against the test set saved from this prompt, scoring with Guidelines.`, **Analyze Intent**, **Build Plan**,
   **Execute Confirmed Plan**. *Verified:* **Resolved Intent** `run_prompt_optimization` (`Confidence 72%`), a plan (`Plan ID APLN-...`, `Status ready`, `Applied 8 slot values to the run form`), and, after **Execute Confirmed Plan**, an operation (`Last action: ...`) that starts a real
   calibration job, which ended `candidate_ready` on the 4-case set (baseline `0.9167`, candidate `0.9167`, will vary). It applies nothing: the job is only a candidate to review, exactly as in step 8 of the recipe above.

Cost and time of the appendix: a few dozen model calls (the chat, the generation, 3 runs of 4 cases with a judge call each, the assistant's intent calls, one small calibration), 1.7 to 2.1 minutes.

What did **not** work or was not done, plainly: with the **simulated** assistant none of the four steps work (they are not part of the offline lab); the Playground is not a faithful run of the prompt (above); the Runs stage cannot run the 44-row file; the `InstructionCompliance` LLM judge
([`assets/judges/`](assets/judges/instruction-compliance.judge.json), a reference file) was not exercised, and neither was the Observability trace walkthrough of the original recipe. The baseline comparison in the main lab is the Evaluations page's **Compare to** instead.

## Not covered by this lab

The `InstructionCompliance` LLM judge (a model-graded check; [`assets/judges/`](assets/judges/instruction-compliance.judge.json) is kept as a reference and was not exercised) and the Observability trace walkthrough of the earlier recipe. The
Aria steps are in the [appendix](#appendix-the-aria-steps-verified-on-a-hosted-model).

## Cleanup and reset

Remove what this lab created. The script's `--cleanup` removes everything named `cookbook-01-*` (the script's runs, which carry a timestamp suffix, and the hand-made
`cookbook-01-intake-classifier`); the viewer account is disabled with the last command (a later script run enables it again):

```bash
PYTHONPATH=sdk/caliber-sdk caliber/.venv/bin/python \
  sdk/caliber-sdk/examples/cookbooks/cookbook_01_trustworthy_intake_classifier.py --cleanup    # deletes prompts cookbook-01-*, archives test sets cookbook-01-*
curl -s -b $JAR -X PATCH $B/auth/accounts/cookbook01-viewer -H 'content-type: application/json' -d '{"disabled":true}'
```

(Prompt `cookbook-01-intake-classifier` and test set `cookbook-01-intake-golden` are the hand-made ones; the script's runs add a timestamp suffix. A real run also leaves its
two evaluation runs and its calibration jobs, which are history.) By hand instead: `curl -s -b $JAR -X DELETE $B/prompts/cookbook-01-intake-classifier` and
`curl -s -b $JAR -X PATCH $B/eval-datasets/$DS -H 'content-type: application/json' -d '{"status":"archived"}'` (a deleted prompt answers `502 ... RESOURCE_DOES_NOT_EXIST` if you delete it twice).

Audit rows and refinement jobs are history and stay. To reset everything, stop and delete the disposable stack:

```bash
scripts/cookbook-env.sh down lab01 && rm -rf .tmp/cookbook-env/lab01          # likewise lab01eval, lab01h, lab01ha and lab01l; stop the stand-in model with: kill %1
```
