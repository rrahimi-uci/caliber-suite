# Trustworthy Evaluation

> **Prerequisite flag:** `CALIBER_FEATURE_COOKBOOKS=true` opens the Cookbooks gallery (a preview area,
> off by default). The lab itself uses only the supported alpha surface (test sets, judges,
> evaluations, review queues); the flag is needed only for the gallery card and for the SDK script's
> recipe check.

## What it demonstrates and the expected outcome

A governance lane in which an automated judge is checked against human reviewers before anyone
trusts it to block a release. You load a 50-row test set of support replies and incident summaries
(clear passes, clear failures and genuinely borderline cases, each with a reviewer's label), define
a custom judge (`FaithfulnessJudge`), score a baseline and a candidate with deterministic graders
plus the judge, and read the per-row results. You then have people review the same outputs, import
their labels, and let CALIBER compute how often the judge agrees with them (agreement rate, Cohen's
kappa, false positives and negatives). The judge is **certified** only if it clears the thresholds
on enough rows, otherwise **rejected**, and every disagreement goes back into the test set as a hard
case so the next calibration sees it.

**You are done when:**

- the test set holds 50 examples at version 51, and a template using an unsupported variable
  (`{{ evidence }}`) is refused with a 400 that names it;
- baseline and candidate Evaluations have per-row results for all 50 rows (on a stack with a model);
- 50 trace-linked review answers exist and **Import labels** brings back 50 labels with none skipped;
- `FaithfulnessJudge` is **certified** (agreement 0.82, Cohen's kappa 0.6365 with the lab's stand-in
  model) and `LenientJudge`, a deliberately weak judge, is **rejected** (agreement 0.58, kappa
  0.0933);
- nine disagreements are added back to the test set (version 60), and five rows alone are shown to be
  too few to certify.

That is the **offline** tier (the stand-in model). The **verified** tier is a real provider, which this lab has also been
run against ([Run it with a real provider](#run-it-with-a-real-provider)): there the numbers are your judge's and differ from run
to run, and the lab asserts invariants instead (the strict judge agrees with people more than the lenient one, the lenient one
is never certified, a candidate given the evidence states the expected fact far more often than a baseline that is not).

## Prerequisites and setup

You need a running CALIBER ([`docs/start/alpha-install.md`](../../../docs/start/alpha-install.md)), an
administrator login, `CALIBER_FEATURE_COOKBOOKS=true` for the gallery card, and Python with
`caliber-sdk` and `mlflow` installed (`mlflow` writes the traces; it is already there if CALIBER runs
from the same environment).

**Simulated or real model: read this first.** By default CALIBER runs simulated providers
(`CALIBER_LLM_PROVIDER=fake`, `CALIBER_EVAL_PROVIDER=fake`, see
[`docs/start/alpha-scope.md`](../../../docs/start/alpha-scope.md)). The judge is an LLM call and
Evaluations generate answers with an LLM, so on that default **neither can run**: Evaluations
fail closed with a 400 and the alignment check reports every row as an error. Nothing is faked. What a
simulated stack can still prove, and what needs a model:

| What | Simulated default | Stand-in model | Real provider |
| --- | --- | --- | --- |
| Test set import, versions, judge create and template validation | verified | verified | verified |
| Traces, review queue, label write-back, **Import labels** | verified | verified | verified |
| Evaluations (per-row scorecard, baseline vs candidate) | refused (400), verified as a refusal | runs, wiring verified | **verified**: 50 rows, per-row scores, candidate far above baseline when it is given the evidence |
| Judge verdicts and **Check alignment** metrics | every row errors (`scored` 0), verified as a refusal | runs; the arithmetic over the supplied labels is real | **verified**: `scored` 50 of 50, the server's agreement, kappa and confusion recomputed independently; the numbers are your judge's and vary |
| Whether `FaithfulnessJudge` is actually a good judge | not shown | **not shown** | the point of the lab: **verified** (3 runs, below). It sits on the certification threshold: certified twice, rejected once |
| Calibration job on the test set | runs; the score is canned and marked simulated | not run | not run in this lab (its cost is the evaluation's; see [`calibration.md`](../../../docs/use/calibration.md)) |

The **stand-in model** (`assets/stand-in-model/stand_in_model_server.py`) is a scripted
OpenAI-compatible server. CALIBER's own code is all real against it (MLflow `make_judge`, the
prompt, the structured reply, the metrics); only the model is scripted. For a judge request it applies a
mechanical rule (every number and identifier in the output must appear in the evidence, and the output must
not be empty or cut off), so its verdicts are explainable and the agreement and kappa below can be
checked by hand ([`alignment-worksheet.md`](assets/alignment/alignment-worksheet.md)). They say
**nothing** about how a real model would judge this data. For any other request it replays the recorded
answer for the row whose input it receives (candidate prompt) or a vague non-answer (baseline).

Start a stack. From the repo root, with the checkout's helper (a disposable stack on free ports;
sign in as `admin` / `admin`):

```bash
# A. the shipped simulated default
scripts/cookbook-env.sh up lab10 --flags cookbooks
source .tmp/cookbook-env/lab10/env.sh

# B. with the stand-in model behind it (start it first, in its own terminal or with &)
python3 docs-site/cookbooks/10-judge-certification-human-review-lab/assets/stand-in-model/stand_in_model_server.py 8770 &
CALIBER_LLM_PROVIDER=openai CALIBER_EVAL_PROVIDER=mlflow OPENAI_API_KEY=stand-in \
  OPENAI_BASE_URL=http://127.0.0.1:8770/v1 OPENAI_API_BASE=http://127.0.0.1:8770/v1 \
  scripts/cookbook-env.sh up lab10m --flags cookbooks --real-llm
source .tmp/cookbook-env/lab10m/env.sh
```

For a real provider use `--real-llm` ([Run it with a real provider](#run-it-with-a-real-provider) has the exact
command) or follow "Real LLM and evaluation" in `alpha-install.md` and also set `CALIBER_FEATURE_COOKBOOKS=true`.
Check which kind of stack you have:

```bash
curl -s $CALIBER_URL/ajax-api/2.0/mlflow/caliber/readiness | python3 -c 'import json,sys; print(json.load(sys.stdin)["data"]["simulated"])'
# ["eval", "llm"]  -> simulated default      []  -> a model is behind it
```

Then create a token for the SDK (or use **Settings > API tokens**):

```bash
B=$CALIBER_URL/ajax-api/2.0/mlflow/caliber
curl -s -c /tmp/lab10.jar -X POST $B/auth/login -H 'content-type: application/json' \
  -d '{"user_id":"admin","password":"admin"}' >/dev/null
export CALIBER_TOKEN=$(curl -s -b /tmp/lab10.jar -X POST $B/auth/tokens \
  -H 'content-type: application/json' -d '{"name":"lab10"}' \
  | python3 -c 'import json,sys; print(json.load(sys.stdin)["data"]["token"])')
export CALIBER_BASE_URL=$CALIBER_URL     # the SDK adds /ajax-api/2.0/mlflow/caliber itself
```

## Dataset and sample data

All files are in [`assets/`](assets/README.md).

| File | Contents | Why |
| --- | --- | --- |
| [`dataset/aggregated-outputs.jsonl`](assets/dataset/aggregated-outputs.jsonl) | 50 rows: 22 support replies (SCN-07: billing, lockouts, SSO, exports, roadmap questions) and 28 incident summaries (SCN-08: connection pools, disks, certificates, lag, cost, backups). Each row has `input`, the `expected` evidence, the recorded `outputs`, the reviewer's `human_label` (27 pass, 23 fail) and `human_label_reason`. 21 clear passes, 17 clear failures, 12 borderline. | the test set and the human labels |
| [`judges/faithfulness-judge.judge.json`](assets/judges/faithfulness-judge.judge.json) | the judge definition: true only if every claim is supported by `{{ expectations }}` | the judge under test |
| [`review/review-questions.json`](assets/review/review-questions.json) | `faithful` (pass/fail) and `evidence` (text) | the review queue |
| [`seed_traces.py`](assets/seed_traces.py) | writes one MLflow trace per row (request = `input`, response = the recorded output) | traces to review; your application produces these in production |
| [`stand-in-model/stand_in_model_server.py`](assets/stand-in-model/stand_in_model_server.py) | the scripted model described above | running without a provider key |
| [`alignment/alignment-worksheet.md`](assets/alignment/alignment-worksheet.md), [`expected-alignment.csv`](assets/alignment/expected-alignment.csv) | the arithmetic by hand, and both verdict columns for all 50 rows | checking the numbers |

The borderline rows are the point. Five (J03, J12, J19, J24, J33) are unsupported claims with no figure
in them (an invented cause, "I have also emailed a copy", "we will waive any cancellation fee"): a
person fails them, and a check that only compares figures to the evidence passes them. Four (J14, J17,
J22, J30) are correct answers a literal check fails (`$40` as the sum of two `$20` charges, `$20.00` for
`$20`, "5 days" for Nov 9 to Nov 14, "91%" for 910 of 1000 GB). Three more (J07, J25, J26) are hedged
statements both sides agree on.

Load it the documented way. Through the UI it is **Test Sets > + New Test Set > Import from file** (or **Prompts > Calibration > Upload JSON/JSONL**); through the API
it is one `POST /eval-datasets/{id}/examples` per row (`input`, `expected`, `tags`). Both were run from this
file. Each appended example is a new dataset version, so 50 rows is version 51.

## Configuration and environment

| Variable | Value used | Meaning |
| --- | --- | --- |
| `CALIBER_FEATURE_COOKBOOKS` | `true` | opens the Cookbooks gallery and `GET /cookbooks`; 404 `feature_disabled` without it |
| `CALIBER_LLM_PROVIDER` | `fake` (default) / `openai` | `fake`: Evaluations and the judge cannot run |
| `CALIBER_EVAL_PROVIDER` | `fake` (default) / `mlflow` | with a real LLM provider this must be `mlflow` or the server refuses to start |
| `OPENAI_API_KEY` | `stand-in` / your key | the key the judge and Evaluations send |
| `OPENAI_BASE_URL`, `OPENAI_API_BASE` | `http://127.0.0.1:8770/v1` | stand-in model only: point the OpenAI client (Evaluations) and MLflow's judge at it |
| `CALIBER_ALLOW_SIMULATED_PROVIDERS` | unset | would allow a real LLM with the fake evaluator; leave it off |
| `CALIBER_URL` | from `env.sh` | the server |
| `CALIBER_BASE_URL` | `$CALIBER_URL` | what the SDK script connects to |
| `CALIBER_TOKEN` | a personal access token | what the SDK script signs in with |
| `MLFLOW_TRACKING_URI` | from `env.sh` | where `seed_traces.py` writes traces; must be the MLflow CALIBER reads |
| `COOKBOOK_10_ASSETS` | the repo's `assets/` | optional: another copy of the assets |
| `CALIBER_LAB_BASE_URL` | `$CALIBER_URL/caliber` | the UI spec's server |
| `CALIBER_LAB10_MODEL` | `stand-in` (default), `real`, `simulated` | which kind of server the UI spec is told it has |
| `OPENAI_API_KEY` (real tier) | your key | read by `scripts/cookbook-env.sh up --real-llm` from your shell, else from the repository-root `.env`; never written to the state directory or the logs |
| `CALIBER_LAB_PYTHON` | the dev venv's python | the UI spec runs `seed_traces.py` with it |

The lab's certification policy (constants in the script, mirrored in `verification.yaml`):
agreement 0.80 or more, Cohen's kappa 0.60 or more, over at least 30 labelled rows, with every row
scored.

## Run it through the API

One script performs the whole lab and exits 0 only if every documented outcome held. From the repo
root, with the environment above:

```bash
# With a model behind the server (the stand-in): pin the exact numbers
PYTHONPATH=sdk/caliber-sdk python sdk/caliber-sdk/examples/cookbooks/cookbook_10_trustworthy_evaluation.py --expect-stand-in
# With a real provider: same command without --expect-stand-in (the numbers are your judge's; see "Run it with a real provider")
# On the simulated default: the same command; it detects the stack and verifies the refusals instead
```

Add `--cleanup` to archive what it created when it succeeds. Everything it creates carries a random
suffix in its name (`FaithfulnessJudge-ab12cd`), so it can run again. It does, in order: loads the test set and
checks version 51; creates the judges and shows `{{ evidence }}` refused; refuses an evaluation of an empty
test set; runs baseline and candidate; writes the traces; creates the queue, answers all 50 items and
imports the labels; checks alignment for both judges, recomputing agreement, kappa and the confusion counts
independently and comparing them with the server's; shows what the rationale leak and five rows do; and
adds the disagreements back.

Expected output with the stand-in model (ids and the suffix vary; trimmed):

```json
{
  "alignment": {
    "faithfulness": {"agreement": 0.82, "kappa": 0.6365, "n": 50, "scored": 50, "decision": "CERTIFIED",
      "confusion": {"true_pos": 23, "false_pos": 5, "true_neg": 18, "false_neg": 4},
      "disagreements": ["J03", "J12", "J14", "J17", "J19", "J22", "J24", "J30", "J33"]},
    "lenient": {"agreement": 0.58, "kappa": 0.0933, "n": 50, "scored": 50, "decision": "REJECTED",
      "confusion": {"true_pos": 27, "false_pos": 21, "true_neg": 2, "false_neg": 0}},
    "rationale_leak": {"agreement": 0.66, "kappa": 0.2952,
      "confusion": {"true_pos": 23, "false_pos": 13, "true_neg": 10, "false_neg": 4}},
    "too_few_rows": {"n": 5, "agreement": 0.8, "kappa": 0.6154, "decision": "NOT_CERTIFIABLE"}
  },
  "cookbook": {"id": "10", "steps": 6, "title": "Trustworthy Evaluation"},
  "dataset": {"examples": 50, "id": "ED-e47d886d", "version": 51},
  "empty_dataset_evaluation": {"status": 400},
  "evaluations": {
    "baseline": {"run_id": "EVR-182faaec", "overall": 0.6667, "pass_rate": 1.0,
                 "non_empty": 1.0, "contains_expected": 0.0, "judge": 1.0},
    "candidate": {"run_id": "EVR-b9d946d9", "overall": 0.74, "pass_rate": 0.7,
                  "non_empty": 1.0, "contains_expected": 0.66, "judge": 0.56},
    "delta_overall": 0.0733
  },
  "feedback": {"dataset_version": 60, "hard_cases_added": 9},
  "invalid_template": {"status": 400, "variable": "evidence"},
  "mode": "model",
  "result": "verified",
  "review": {"labels_imported": 50, "queue_id": "RVQ-cf78bf4f"}
}
```

Read it like this. The **baseline** is a vague non-answer: `FaithfulnessJudge` scores it a perfect 1.0
(it makes no claim to contradict) while **Contains expected** scores it 0.0 (it states none of the key facts).
An aggregate over only one of them would call the baseline either perfect or useless, which is how a
disagreement hides behind a score. The **candidate** states the facts, so the judge now has claims to
check: 56% faithful, 66% containing the key fact. The scorecard score is separate from the judge's
certification: certification is only about whether the judge agrees with people.

Expected output on the simulated default (trimmed):

```json
{
  "alignment": {"n": 50, "scored": 0, "agreement": 0.0, "kappa": 0.0, "decision": "NOT_CERTIFIABLE",
    "first_row_error": "judge invocation failed: OPENAI_API_KEY environment variable must be set to use the openai provider."},
  "calibration": {"status": "candidate_ready", "n_examples": 50, "dataset_version": 51,
    "provider": "fake", "simulated": true, "gate_passed_on_a_canned_score": true},
  "dataset": {"examples": 50, "version": 51},
  "empty_dataset_evaluation": {"status": 400},
  "evaluation_refused": {"status": 400,
    "detail": "evaluation requires a real LLM provider; set CALIBER_LLM_PROVIDER (openai/anthropic) and the matching API key, then retry."},
  "invalid_template": {"status": 400, "variable": "evidence"},
  "mode": "simulated",
  "result": "verified",
  "review": {"labels_imported": 50}
}
```

Here the judge never ran (`scored` 0 of 50), so no certification is possible, which is the correct
outcome. The calibration job is the other half of the point: the same test set feeds calibration, and on
the simulated stack its gate **passes on a canned 0.94** (`simulated: true`, `provider: fake`). The
only thing that makes a gate verdict mean anything is a judge you have already checked against people.

Behind the script, the calls (use them directly if you prefer `curl`; `$B` as above):

| Step | Call |
| --- | --- |
| create the test set, add rows | `POST /eval-datasets`, then `POST /eval-datasets/{id}/examples` with `{"input", "expected", "tags"}` |
| judge | `POST /judges` with `{"name", "instructions", "feedback_value_type": "bool"}` |
| evaluate | `POST /evaluations` with `{"dataset_id", "scorers": ["non_empty", "contains_expected", "Judge.<judge_id>"], "label"}`; add `"predict_target": "prompt", "subject_ref": "<name>@1"` for the candidate; `GET /evaluations/{run_id}` returns `results` |
| review | `POST /review-queues`, `POST /review-queues/{id}/items {"trace_ids": [...]}`, `POST .../items/{item}/submit {"answers": {...}}` |
| import labels | `GET /review-queues/{id}/alignment-examples?question_key=faithful` |
| check alignment | `POST /judges/{id}/alignment {"examples": [{"inputs", "outputs", "expectations", "label"}], "threshold": 0.5}` (1 to 100 rows) |
| feed back | `POST /eval-datasets/{id}/examples` with the row's `input`, `expected`, and the tag `judge-disagreement` |
| calibrate | `POST /prompts/calibration/runs {"agent_id", "eval_dataset_id", "optimizer_type", "scorers"}` |

## Run it with a real provider

This is the verified tier: every step below was run against a stack with OpenAI behind both the answers and the judge (model
`gpt-5.6-luna`, the product default), through the API script three times and through the browser spec.

**Start it.** You need an OpenAI key. Export your own; the helper reads `OPENAI_API_KEY` from your shell (else from a repository-root `.env`) and
never prints or stores it. Calls cost money: a full run is about 350 model calls (100 answers, 100 judged rows, 155 alignment verdicts).

```bash
export OPENAI_API_KEY=...          # your key; never commit it
scripts/cookbook-env.sh up lab10h --flags cookbooks --real-llm
source .tmp/cookbook-env/lab10h/env.sh
curl -s $CALIBER_API/readiness | python3 -c 'import json,sys; print(json.load(sys.stdin)["data"]["simulated"])'   # []
```

Create the token as in Setup, then run the same script without `--expect-stand-in`:

```bash
PYTHONPATH=sdk/caliber-sdk caliber/.venv/bin/python sdk/caliber-sdk/examples/cookbooks/cookbook_10_trustworthy_evaluation.py --cleanup
# UI: cd caliber/caliber-ui && CALIBER_LAB10_MODEL=real CALIBER_LAB_BASE_URL="$CALIBER_URL/caliber" npx playwright test --config=playwright.labs.config.ts e2e/cookbooks/labs/10-judge-certification.spec.ts
```

**What changes against the other tiers.** Evaluations run instead of answering `400`; the judge returns a verdict for every row (`scored` 50 of 50); and nothing
is pinned, because a model's answers and verdicts differ from run to run. Two things are different on purpose:

- **The candidate is given the evidence.** In the test set the evidence is in `expected`, which only the graders see. A live model that is shown just the ticket cannot state
  `two charges of $20 posted on the same day`, so a first real run scored the baseline and the candidate alike (Contains expected 0.08 and 0.08, judge 0.08 and 0.06): no candidate
  prompt could have won. The real tier therefore runs the candidate on a copy of the 50 rows whose input also carries `evidence` (the facts, without the grader's `expected` and `decision`),
  with the prompt extended by `Evidence you may use: {{ evidence }}`. The baseline still sees the ticket alone. That makes the comparison a real one: does grounding help.
- **What is asserted.** Ranges and orderings, not values: scores in 0 to 1; the candidate's Contains-expected rate at least 0.10 above the baseline's; for both judges `scored` = 50 and the server's
  agreement, kappa and confusion equal the script's independent recomputation; the strict judge's agreement above the lenient judge's; the lenient judge not certified.

**Real results** (run B of three; ids and values will vary; trimmed):

```json
{
  "mode": "real model",
  "evaluations": {
    "baseline":  {"overall": 0.4,  "pass_rate": 0.2,  "non_empty": 1.0, "contains_expected": 0.08, "judge": 0.12},
    "candidate": {"overall": 0.78, "pass_rate": 0.98, "non_empty": 1.0, "contains_expected": 0.82, "judge": 0.52},
    "delta_overall": 0.38
  },
  "alignment": {
    "faithfulness": {"agreement": 0.84, "kappa": 0.686, "n": 50, "scored": 50, "decision": "CERTIFIED",
      "confusion": {"true_pos": 19, "false_pos": 0, "true_neg": 23, "false_neg": 8},
      "disagreements": ["J05", "J07", "J08", "J21", "J36", "J40", "J42", "J43"]},
    "lenient": {"agreement": 0.58, "kappa": 0.0933, "decision": "REJECTED",
      "confusion": {"true_pos": 27, "false_pos": 21, "true_neg": 2, "false_neg": 0}},
    "rationale_leak": {"agreement": 0.84, "kappa": 0.686},
    "too_few_rows": {"n": 5, "agreement": 0.8, "kappa": 0.5455, "decision": "NOT_CERTIFIABLE"}
  },
  "feedback": {"hard_cases_added": 8, "dataset_version": 59},
  "result": "verified"
}
```

**Three runs, side by side** (A used the first version of this tier, whose candidate was not given the evidence, so only its judge columns compare):

| | A | B | C |
| --- | --- | --- | --- |
| `FaithfulnessJudge` agreement, kappa | 0.80, 0.61 | 0.84, 0.686 | 0.76, 0.532 |
| false positives, false negatives | 0, 10 | 0, 8 | 1, 11 |
| decision | CERTIFIED (on the bars exactly) | CERTIFIED | **REJECTED** |
| `LenientJudge` agreement, kappa | 0.58, 0.0933 | 0.58, 0.0933 | 0.58, 0.0933 |
| baseline: Contains expected, judge | 0.08, 0.08 | 0.08, 0.12 | 0.08, 0.06 |
| candidate: Contains expected, judge | 0.08, 0.06 (no evidence) | 0.82, 0.52 | 0.88, 0.46 |
| disagreements added back | 10 | 8 | 12 |
| wall time | 18 min | 18 min | 6 min |

Read it like this:

- **The lenient judge is deterministic in effect.** It passes everything well formed, so its numbers are identical to the stand-in's and never change: a useful negative control.
- **The strict judge is the interesting one, and it is not stable.** It never lets an unsupported claim through (false positives 0 to 1, against the stand-in's 5), including the five rows with no figure in them (J03, J12, J19, J24, J33) and the four derived numbers
  (J14, J17, J22, J30) that fooled the mechanical stand-in. But it is **stricter than the reviewers**: it fails 8 to 11 answers a person passed, mostly clear-pass incident summaries (J05, J07, J08, J21, J36, J40, J42, J43 in every run). Asked about J05 directly, it reasons that
  the reply "asserts that the regression was introduced by v2.9.1, which is not established by the evidence" and names an `on-call gateway owner` the evidence does not mention: a defensible reading the reviewer did not apply. Asked about J43 it said `true`
  although it had failed it in all three runs: borderline rows flip.
- **So the certification flips with it:** 0.84 and 0.80 certify, 0.76 does not. The thresholds (agreement 0.80, kappa 0.60) sit inside this judge's run-to-run spread. Treat a single certification as an observation, not a decision; repeat it, label more rows, or tighten the judge.
- **The rationale-leak effect was a stand-in artifact.** With the stand-in, adding the reviewer's quote of the unsupported claim to the evidence dropped agreement from 0.82 to 0.66; a real judge was not misled by it (0.84 to 0.84, 0.76 to 0.82). Keep explanations out of the queue anyway: nothing guarantees every judge resists.
- **Grounding works as expected:** the candidate states the expected fact 0.82 to 0.88 of the time against 0.08, and the judge, which reads the evidence, passes it 0.46 to 0.52 against 0.06 to 0.12.
- **Five rows still meet both bars by luck** (0.8, kappa 0.5455 here, 0.6154 in the stand-in's run): the minimum of 30 scored rows is what protects you.

In the UI, with `CALIBER_LAB10_MODEL=real`, steps 1 to 8 run unchanged. The Evaluations table reads `<passed>/50` (not `50/50`) and the scorecard shows percentages, not the stand-in's 74/66/56. On the ten reviewed rows the strict judge's **Agreement** is above the lenient judge's.

**Cost and time.** About 350 model calls per run; 6 to 18 minutes depending on the provider's latency at the time (the SDK script sets a 30-minute request timeout; the UI spec waits up to 15 minutes per Evaluation).

## Recipe: run it through the UI

Sign in at `$CALIBER_URL/caliber/` as `admin` / `admin`. The Playwright spec
[`10-judge-certification.spec.ts`](../../../caliber/caliber-ui/e2e/cookbooks/labs/10-judge-certification.spec.ts)
performs exactly these steps with real clicks; run it against a stack that is already up:

```bash
cd caliber/caliber-ui
CALIBER_LAB_BASE_URL="$CALIBER_URL/caliber" npx playwright test --config=playwright.labs.config.ts e2e/cookbooks/labs/10-judge-certification.spec.ts
# CALIBER_LAB10_MODEL says what is behind the server: stand-in (default), real, or simulated (the fake default).
# The spec reads the app's own "Simulated providers" banner and stops at once if you named the wrong kind.
```

First write the traces the review will use (the UI cannot create them; your application would):

```bash
python docs-site/cookbooks/10-judge-certification-human-review-lab/assets/seed_traces.py > traces.json   # needs MLFLOW_TRACKING_URI
```

1. **Load the test set.** Under **Prompt lifecycle > Test Sets** click **+ New Test Set**, name it `judge-certification-set`, owner `admin`, **Create**, open it and click **Import from file**; choose `aggregated-outputs.jsonl` and click **Import 50 rows**
   (`aggregated-outputs.jsonl: 50 rows ready to import.` first; a bad line is named and nothing is imported). The header ends on `v51` (one version per appended example) and `50 active examples`; each row shows the
   input and the expected evidence (the recorded `outputs` and the reviewer's `human_label` stay in the file: they are what the lab's traces and review use). The same file can also be loaded from a prompt's **Calibration** tab
   (**Upload JSON/JSONL**), and under **Integrations > Cookbooks** the *Trustworthy Evaluation* card lists the recipe (optional: installing it only creates a paused workflow draft, which this lab does not use).
2. **Create the candidate prompt.** Under **Prompt lifecycle > Prompts** click **New prompt**, **Write / paste**, paste
   `You answer support tickets and incident alerts using ONLY the evidence you are given. State what the evidence says and nothing more; if it does not cover the question, say so and escalate to a person.`
   into **Prompt text**, name it `grounded-reply` and click **Create and Open**: version 1, live. This is the candidate the Evaluations will score.
3. **Define the judge.** Under **Agent resources > Judges** click **+ New Judge**. Enter a **Name**
   (`FaithfulnessJudge`), keep **Returns** `bool`, and as **Instructions** type
   `Is {{ outputs }} faithful to {{ evidence }}?`; click **Create judge**. It is refused, in red:
   `request body validation failed: instructions use unsupported template variable(s) {{ evidence }}; only {{ inputs }}, {{ outputs }}, {{ expectations }}, {{ conversation }}, {{ trace }} are allowed`.
   Replace the instructions with the text of [`faithfulness-judge.judge.json`](assets/judges/faithfulness-judge.judge.json)
   and click **Create judge**: an active `FaithfulnessJudge` appears. Add `LenientJudge` the same way, with
   `Return true when {{ outputs }} is a complete, well-formed answer to {{ inputs }}. Return false only if it is empty or cut off.`
4. **Run baseline and candidate.** Under **Prompt lifecycle > Evaluations** click **Run evaluation**. Pick **Test set** `judge-certification-set (v51)`,
   **Label** `baseline`, leave **What to score** on *Model completion (generic)*. Under **Graders** untick **Exact match** and
   **Token F1**, keep **Contains expected**, tick **Non-empty**, and under **Custom LLM judges** tick **FaithfulnessJudge**; click **Run**.
   A `baseline` row appears, `completed`, 50/50. Repeat with **Label** `candidate`, **What to score** *Prompt version*, **Prompt (name@version)**
   `grounded-reply@1`. On the simulated default, **Run** instead shows `evaluation requires a real LLM provider` in the panel and nothing is saved.
5. **Inspect examples.** Open the `candidate` row: one result row per example with the input, expected evidence, prediction and a score per grader.
   Pick the baseline in **Compare to**; the context line reads `target llm` for the baseline and the cards show the change. With the stand-in model the
   candidate's cards read 74% overall, 66% for Contains expected and 56% for the judge.
6. **Modify the prompt, new version, re-run, compare** (needs a model: skipped on the simulated default). On **Prompts**, search `grounded-reply`, click **Edit** on its card and replace the template with
   `You answer support tickets and incident alerts using ONLY the evidence you are given. Quote every figure, date and identifier exactly as the evidence gives it; if the evidence does not cover the question, say so and escalate to a person.`
   then **Save new version**: `Saved v2. Nothing is live yet` (saving never changes what is live; the card's **Versions** lists v1 live and v2, and **Compare templates** shows the text diff). Run **Prompt lifecycle > Evaluations > Run evaluation** again
   with the same test set and graders, **Label** `candidate v2`, **What to score** *Prompt version*, **Prompt (name@version)** `grounded-reply@2`. Open the `candidate v2` row and pick the `candidate` run (v1) in **Compare to**: the context line reads
   `Dataset/scorer-compatible comparison: dataset v51 and grader suite match; ... Current: target prompt, subject grounded-reply@2 ... Baseline: target prompt, subject grounded-reply@1 ...` and the cards show the change per grader.
   What the comparison proves is that v1 and v2 were scored on the same rows with the same graders; whether v2 is better is the model's answer (with the stand-in, which replays recorded answers, the two are identical).
7. **Human review.** Under **Operate > Review Queues** click **New queue**, name it `faithfulness-certification`; Question 1: key `faithful`, type `pass_fail`;
   click **+ Add question**: key `evidence`, type `text`; **Create queue**, then open it. Paste the trace ids of J01, J02, J03, J04, J05, J08, J09, J11, J14 and J26
   (from `traces.json`) into **Add traces to review** and click **Enqueue**: `Items (10)`. For each item click **Pass** or **Fail** as that row's `human_label` says, paste
   that row's `expected` JSON into the **evidence** box and click **Submit review**. Keep that box to the evidence: whatever is in it reaches the judge as
   `{{ expectations }}`. The page ends on `All items reviewed.`
8. **Compute alignment, certify or reject, close the loop.** Under **Agent resources > Judges** click **Try it** on `FaithfulnessJudge`, switch to **Human alignment**,
   choose `faithfulness-certification` under **Import from Review Queue…** and the pass/fail question, click **Import labels** (`Imported 10 completed label(s); skipped 0.`)
   and **Check alignment**. With the stand-in model it reads **Agreement 80%**, **Cohen's κ 0.60**, `over 10/10 examples`, **FP 1 · FN 1**. Ten rows meet both numbers but
   not the 30-row minimum: a readout, not a certification (the API run over 50 certifies at 0.82 and 0.6365). Do the same for `LenientJudge`: **Agreement 60%**, **Cohen's κ 0.20**,
   **FP 4 · FN 0**, so it is rejected. If the judge cannot run (the simulated default) the panel reads `Agreement 0%` and adds `10 example(s) could not be judged and are left out of the numbers
   above: judge invocation failed: OPENAI_API_KEY environment variable must be set …`. Finally, under **Prompt lifecycle > Test Sets** open `judge-certification-set`, click **+ Add example**, paste J03's `input`
   and `expected`, tag it `judge-disagreement`, **Save**; do the same for J14. The set now shows `52 active examples` at `v53`. (**Capture from trace** would store the trace's own reply as the expected answer, which is
   the wrong evidence here.)

## Verify it worked

- **The numbers match.** Re-derive them from
  [`expected-alignment.csv`](assets/alignment/expected-alignment.csv) with the worked example in
  [`alignment-worksheet.md`](assets/alignment/alignment-worksheet.md): TP 23 · FP 5 · TN 18 · FN 4, agreement (23 + 18) / 50 = 0.82,
  chance agreement 0.56 x 0.54 + 0.44 x 0.46 = 0.5048, kappa = (0.82 - 0.5048) / 0.4952 = 0.6365. For `LenientJudge`: TP 27 · FP 21 · TN 2 · FN 0, agreement 0.58, kappa 0.0933.
- **Per-row results persisted.** `GET $B/evaluations/<run_id>` returns `n_examples` 50, `aggregate` with `non_empty`, `contains_expected` and `Judge.<id>`, and 50 `results`, each with a
  `prediction`, per-grader `scores` and `error` null. `evidence.slices` breaks the run down by dataset tag (try `expected_disagreement`).
- **Review is trace-linked.** `GET $B/review-queues/<queue_id>/alignment-examples?question_key=faithful` returns 50 `examples`, each with `provenance.trace_id`, and `skipped: []`. The
  `faithful` answer is also an assessment on the trace in MLflow.
- **Disagreements fed back.** `GET $B/eval-datasets/<id>` shows `version` 60 (51 + 9); the nine rows are the `judge-disagreement`-tagged examples.
- **A gate on a canned score is not evidence** (simulated stack): `GET $B/jobs/<job_id>` shows `eval_results.simulated` `true` and `gate.passed` `true` on a canned 0.94, over `n_examples` 50 pinned to `eval_dataset_version` 51.
- **Real provider (invariants, not numbers):** the script exits 0 only if `scored` equals `n` for both judges, the server's agreement, kappa and confusion match the independent recomputation,
  the strict judge's agreement is above the lenient judge's, the lenient judge is not certified, and the candidate's Contains-expected rate is at least 0.10 above the baseline's.
- **Tests that guard this lab:**
  `caliber/.venv/bin/python -m pytest caliber/tests/test_cookbook_10_judge_certification.py caliber/tests/test_cookbook_doc_contract.py -k "10 or alignment" --no-cov` (dataset, stand-in rules and the documented numbers) and
  `caliber/.venv/bin/python -m pytest sdk/caliber-sdk/tests/test_cookbook_examples.py -k cookbook_10 --no-cov` (the script against an offline stand-in server).

## Common errors and troubleshooting

| You see | Cause | Fix |
| --- | --- | --- |
| `404` `Cookbooks is disabled in this deployment`, `reason_code: feature_disabled` | the Cookbooks flag is off (the shipped default) | start with `CALIBER_FEATURE_COOKBOOKS=true`. The other steps of the lab do not need it |
| `400` `evaluation requires a real LLM provider; set CALIBER_LLM_PROVIDER ...` on `POST /evaluations` | the simulated default: Evaluations fail closed rather than invent scores | use a real provider, or the stand-in model, as in Setup |
| alignment returns `scored: 0`, every row's `error` is `judge invocation failed: OPENAI_API_KEY environment variable must be set ...`; the UI says `could not be judged` | same cause: the judge is a model call | same fix. With the stand-in, check `OPENAI_BASE_URL` and `OPENAI_API_BASE` were set when the **server** started (not only your shell) |
| the server will not start; the log says `CALIBER_LLM_PROVIDER=openai is a real provider but CALIBER_EVAL_PROVIDER=fake would score its candidates with canned numbers and pass the gate on them` | a real LLM provider with the fake evaluator is refused at startup | set `CALIBER_EVAL_PROVIDER=mlflow` (or `CALIBER_ALLOW_SIMULATED_PROVIDERS=true` to run the combination on purpose). `scripts/cookbook-env.sh up` then reports it never became healthy; read `caliber.log` |
| `CALIBER_LLM_PROVIDER=fake` with `CALIBER_EVAL_PROVIDER=mlflow` | starts, but Evaluations still answer the same `400` (they check only the LLM provider) | give it a model; a real evaluator does not replace one |
| `400` `request body validation failed` on **Create judge**, naming `{{ evidence }}` | the instructions use a variable other than `{{ inputs }}`, `{{ outputs }}`, `{{ expectations }}`, `{{ conversation }}`, `{{ trace }}`; or none at all (`must reference at least one evaluation variable`) | fix the template. (It used to be saved and only fail later, as an Evaluations 400 or an alignment 502.) |
| `400` `eval dataset ... has no examples to score` | an Evaluation against an empty test set | add examples first |
| `409` `... name ... is already in use` | dataset, judge and queue names are global, and archived ones keep theirs | use a new name; the script appends a random suffix |
| a row's `error` is `Must specify 'expectations' - required by template variables in instructions.` | a row lacks a field the judge's template uses (here `{{ expectations }}`); such rows are left out of the numbers and `scored` is below `n` | give every row the fields the template needs; the lab requires `scored` to equal `n` |
| agreement falls (0.82 to 0.66) after you add a reviewer note | **Import labels** passes every answer other than the pass/fail one to the judge as `{{ expectations }}`, so a reviewer's quote of the unsupported claim becomes evidence it then agrees with | keep the queue to the label and the evidence; keep explanations out of it |
| **Import labels** skips rows, `trace response unavailable` | the trace has no response, or MLflow is not the one CALIBER reads | seed with the same `MLFLOW_TRACKING_URI` as the server |
| `400` `alignment import requires pass_fail labels` | the chosen question is not a `pass_fail` question | pick the `faithful` question |
| five rows give agreement 0.8 and kappa 0.6154 | the product returns numbers for any sample size (1 to 100 rows); both bars clear on five rows by luck | the lab's rule demands 30 scored rows |
| `Uploaded N examples` never appears / the dataset name is rejected | a dataset with that name exists | choose another **Dataset name** |
| the UI still shows an old message after a code fix | the web bundle is rebuilt only on start | `scripts/cookbook-env.sh restart NAME` |
| (real provider) the evaluation's status is `failed` with `Error code: 401 ... Incorrect API key provided`; alignment rows say `judge invocation failed: Failed to invoke judge model: Incorrect API key provided` and `scored` is 0 | the key was rejected. Reproduced with `OPENAI_API_KEY=sk-invalid scripts/cookbook-env.sh up lab10bad --flags cookbooks --real-llm`: the run is saved as `failed` with the provider's message, so `GET /evaluations/<id>` names it | export a valid key and `restart`; the script stops with `baseline run is failed: ...401...` |
| (real provider) `429` / `rate limit` in a failed run or in a row's `error` | the provider's rate limit; the lab makes about 350 calls in minutes | wait and rerun (the script is not idempotent but appends a suffix), or lower concurrency by running one stack at a time |
| (real provider) `model ... does not exist` / `404` from the provider | the server's model (`gpt-5.6-luna` by default) is not available to your account | set the model the server uses (`CALIBER_LLM_DIAGNOSIS_MODEL`) before `up` |
| (real provider) the SDK script ends with `CaliberTransportError ... timed out` | an Evaluation is one request that makes a call per row (about 5 to 18 minutes for 50 rows) and the SDK's default is 30 seconds. The script sets 30 minutes; your own client needs `CaliberClient(..., timeout=1800)`. (Before this lab's fix the server also froze every other request while it ran) | use the script, or raise the timeout |
| (real provider) agreement and kappa change between runs, and the strict judge is **certified one time and rejected the next** | the judge is a model: borderline rows flip. On this data it sits on the bars (see the three runs) | do not certify on one run: repeat, or label more rows, or tighten the judge's instructions, and record the runs you relied on |
| (real provider) the candidate and baseline score the same on Contains expected | the candidate was not shown the evidence: in the test set the evidence is only in `expected`, which a live model never sees (the first real run: both 0.08) | give the candidate the evidence in its input, as the script's real tier does |

## Cleanup and reset

Archive what the run created (names stay reserved, so a rerun needs a new suffix; the API script does this with `--cleanup`):

```bash
B=$CALIBER_URL/ajax-api/2.0/mlflow/caliber
curl -s -b /tmp/lab10.jar -X PATCH $B/judges/<judge_id>       -H 'content-type: application/json' -d '{"status":"archived"}'
curl -s -b /tmp/lab10.jar -X PATCH $B/eval-datasets/<id>      -H 'content-type: application/json' -d '{"status":"archived"}'
curl -s -b /tmp/lab10.jar -X PATCH $B/review-queues/<queue_id> -H 'content-type: application/json' -d '{"status":"archived"}'
curl -s -b /tmp/lab10.jar -X DELETE $B/prompts/grounded-reply
MLFLOW_TRACKING_URI=... mlflow experiments delete --experiment-id <id of cookbook-10-judge-certification>
```

To reset everything, stop the disposable stacks and delete their state, and stop the stand-in model:

```bash
scripts/cookbook-env.sh down lab10;  rm -rf .tmp/cookbook-env/lab10
scripts/cookbook-env.sh down lab10m; rm -rf .tmp/cookbook-env/lab10m
kill %1    # the stand-in model, if you started it with & in this shell
```

## Related

[`docs/use/evaluation-and-test-sets.md`](../../../docs/use/evaluation-and-test-sets.md),
[`docs/use/calibration.md`](../../../docs/use/calibration.md),
[`docs/14-evaluation/architecture.md`](../../../docs/14-evaluation/architecture.md).
