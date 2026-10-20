# Precision Skills: a support-tone skill that triggers only when it should

> **Prerequisite flag:** `CALIBER_FEATURE_COOKBOOKS=true` and nothing else. It only opens the Cookbooks gallery (a preview area, off by default);
> Skills themselves are part of the supported alpha. Every step below was run against a real server (a fresh stack from
> `scripts/cookbook-env.sh up lab02 --flags cookbooks`) through the API and through the browser, on the alpha's default
> **simulated** language model and evaluator, and again on a stack with **real providers** (`... up lab02h --flags cookbooks --real-llm`):
> see [Run it with a real provider](#run-it-with-a-real-provider).

## What this lab demonstrates

A narrow skill, `support-tone-and-citation`, that gives customer-facing support replies one voice (calm, no over-promising, cited
knowledge-base ids, an escalation hint) and must **not** fire on engineering work. You author it, preview how its variables render, run a
**40-case trigger test set** against the real, deterministic skill selector, see the starter configuration get 10 of the 40 cases wrong,
tighten the trigger phrases until all 40 are right, pin that run as the **baseline**, then **calibrate** the skill, **review** the
candidate, **Apply** it, **roll it back** from the version history, **export and re-import** it as a ZIP package, exercise the failure paths
(invalid content, a calibration with no baseline, a viewer who cannot apply) and **archive** it.

**You are done when:**

- the skill has version 1 live, and trigger round 1 (the starter phrases) gets `30/40` cases right, with false positives `T27 T28 T29 T30 T31 T32 T34 T35`
  and false negatives `T14 T23`;
- after the tightened phrases, round 2 gets `40/40` right, which is above the `trigger_accuracy_min` gate of `0.95`, and that run is the pinned baseline;
- a calibration run reaches `candidate_ready`; on the simulated stack its evidence says `simulated: true` and `n_examples: 0`; on a real-provider stack it says
  `simulated: false` and `n_examples: 8` (the skill's test set);
- Apply makes the candidate version 2 and rolling back makes the original text version 3 (the history is `3, 2, 1`, and 3 has the text of 1);
- the exported ZIP holds exactly `<name>/SKILL.md` and `<name>/agents/openai.yaml`; re-importing under the same name is `409`, importing under a new
  name keeps the instructions but **not** the tags, category or trigger phrases;
- a viewer's Apply, Calibrate, Roll back and Create are each `403`; invalid skill content is `400` (a duplicate name `409`) and stores nothing;
- every skill the lab created is `archived`.

### What is simulated, and what a real provider changes

Trigger selection (`POST /skills/{id}/test-selection`) is deterministic code, not a model: it is real on every stack. Calibration is the
part that depends on the provider. Under `CALIBER_LLM_PROVIDER=fake` / `CALIBER_EVAL_PROVIDER=fake` (the default):

| Step | On the simulated stack (what you see) | On a real-provider stack (run and verified, see below) |
| --- | --- | --- |
| **Calibrate** candidate | A canned candidate: the skill's text plus three comment lines (`# Refinement added by SkillMetaPrompt`, `# Diagnosis: [fake] ...`, `+3 / -0 lines`). | A model-written rewrite. With a note on what to fix it is a targeted edit (a few lines changed); with no note the model has no evidence of a defect and wrote a speculative extra section (see below). |
| **Calibrate** evidence | Canned scores (candidate `0.94`, baseline `0.88`). Marked `provider: fake`, `simulated: true` in the API and **"Simulated evaluation — not evidence"** in the review. `n_examples` is `0` because **a skill has no test set** there. | Scores measured by the evaluator over the **test set you pass** (`eval_dataset_id`): `provider: mlflow`, `simulated: false`, `n_examples` equal to its size, the dataset id and version pinned. Without a test set the run **fails**: a real evaluator has nothing to score against. |

So on the simulated stack this lab proves the wiring: a run is queued, reaches a candidate, shows a diff and the evidence flags, applies as a version and rolls back; it proves
nothing about whether the candidate is better. On a real stack the same wiring holds and the evidence is measured, with the limits stated in the real-provider section (eight rows).

## Prerequisites and setup

1. A server up per [`docs/start/alpha-install.md`](../../../docs/start/alpha-install.md), with `CALIBER_FEATURE_COOKBOOKS=true` for the gallery step.
   From a checkout, the disposable stack the lab was run on is:

   ```bash
   scripts/cookbook-env.sh up lab02 --flags cookbooks
   source .tmp/cookbook-env/lab02/env.sh      # CALIBER_URL, CALIBER_API, CALIBER_BASE_URL, ...
   ```

2. Sign in as `admin` / `admin` (the stack's bootstrap account). The lab needs `caliber.admin` and `caliber.operator`: creating a skill needs
   operator, changing its metadata or rolling it back needs admin.
3. `caliber/.venv/bin/python` with `caliber_sdk`, `caliber` and `pyyaml` (the repo's dev venv has all three; `pip install caliber-sdk pyyaml` elsewhere),
   `curl` and `jq` for the by-hand commands.

## Dataset and sample files

All under [`assets/`](assets/README.md).

| File | What it holds | Why |
| --- | --- | --- |
| [`skills/support-tone-and-citation.md`](assets/skills/support-tone-and-citation.md) | The skill: front matter (`name`, `summary`, `category`, `tags`, the six starter `should_not_trigger` phrases, `render_variables`) and the instructions below it. | The summary is what the selector reads, with the tags, name and trigger phrases; the body is never read by it. |
| [`dataset/trigger-cases.jsonl`](assets/dataset/trigger-cases.jsonl) | **40 cases** `T01`-`T40`: 19 expected to select the skill, 21 not. Tags: `positive` (19, 3 of them also `edge`: terse or urgent phrasing) and `negative` (21, 8 of them also `near-miss`: engineering requests that use support words such as billing, invoice, customer, refund). Each row is `{id, tags, inputs: {user_message, artifact_type?, session_goal?}, expectations: {is_selected}}`. | Realistic phrasings, including the hard ones: near misses that fool a keyword selector, and positives that share no word with the summary. |
| [`skills/trigger-phrases.json`](assets/skills/trigger-phrases.json) | The tightened `test_triggers`: 3 `should_trigger` and 14 `should_not_trigger` phrases. | Round 2 of the trigger test. |
| [`dataset/render-vars.json`](assets/dataset/render-vars.json) | The three template variables (`user_message`, `audience`, `policy_context`), and a `partial` set with only `audience`. | Render Preview, with nothing unresolved and with two things unresolved. |
| [`skills/invalid-skills.json`](assets/skills/invalid-skills.json) | Five skill payloads the server must refuse, each with the status and message to expect. | The invalid-content failure path. |

### How the selector scores (so you can read the results)

`score_skill_for_query` in `caliber/src/caliber/assistant/skill_runtime.py`; a case is selected when its score is above `0`. It scores the
**words of the message, the artifact type and the session goal together**:

- a **`should_not_trigger` phrase that matches ends it at score `0`** (`excluded:negative_trigger:<phrase>`), whatever else matches;
- a `should_trigger` phrase match adds `8`; a word of the skill **name** adds `5`; **tag** words add `4` plus one each; the `artifact_type` inside the category adds `3`;
  **summary** word overlap adds at most `3`; **description** overlap at most `2`;
- a phrase matches when it is a **substring** of the normalised message (so `refund` matches `refunds` and `refund-eligibility`), or when at least 80% of its words
  (every word of a one-word phrase) are in the message;
- the long-form **content is never read**, and stop words are only `a an and are for from how in is it of on please the to use with`.

Two consequences drove the dataset. One summary word is enough to select (the summary names `engineering`, `code`, `API`, `schema` only to exclude them, but
the selector counts them), and a positive that shares no word with the name, tags or summary is **not** selected (`T14`, `T23` below). Both are fixed with phrases, not
with wording in the content. Scores depend on the skill's name too (a name containing `support` adds 5 to anything mentioning support), so the lab asserts decisions, never exact scores.

## Configuration and environment

| Variable | Value used | Meaning |
| --- | --- | --- |
| `CALIBER_FEATURE_COOKBOOKS` | `true` (via `--flags cookbooks`) | Opens the Cookbooks gallery. Nothing else in the lab needs a flag. |
| `CALIBER_LLM_PROVIDER` / `CALIBER_EVAL_PROVIDER` | `fake` (the `cookbook-env.sh` default); `openai` / `mlflow` with `--real-llm` | Simulated model and evaluator; real ones; see the table above. |
| `OPENAI_API_KEY` | unset on the simulated tier; **your own key** for `--real-llm` | Read by `cookbook-env.sh` from your environment (or the gitignored repo-root `.env`). Never put it in a file you commit. |
| `CALIBER_BACKGROUND_TASKS_ENABLED` | `true` (set by `cookbook-env.sh`) | A worker has to run for calibration to leave `queued`. If it is off the run stays queued forever. |
| `CALIBER_BASE_URL` | the server root, e.g. `http://127.0.0.1:5001` | Where the SDK example connects (the SDK appends `/ajax-api/2.0/mlflow/caliber`). |
| `CALIBER_API` | `$CALIBER_BASE_URL/ajax-api/2.0/mlflow/caliber` | Used by the `curl` commands. |
| `CALIBER_TOKEN` | a personal access token for `admin` | Used by the SDK example. Create one below. |
| `COOKBOOK_02_SKILL_NAME` | unset (the example appends a random suffix) | Set it to pick the skill's name; the name must be unused, archived skills keep theirs. |
| `COOKBOOK_02_ASSETS` | unset (this lab's `assets/`) | Point the example and the spec at another copy of the assets. |

## Run it through the API

### One command: the whole lab, verified

`sdk/caliber-sdk/examples/cookbooks/cookbook_02_precision_skills.py` performs every step from the dataset files through the verification, and **exits `0` only
when each outcome in "You are done when" holds**. Run it from the repository root:

```bash
source .tmp/cookbook-env/lab02/env.sh
export CALIBER_TOKEN=$(curl -sS -c .tmp/jar -X POST "$CALIBER_API/auth/login" -H 'content-type: application/json' \
    -d '{"user_id":"admin","password":"admin"}' >/dev/null \
  && curl -sS -b .tmp/jar -X POST "$CALIBER_API/auth/tokens" -H 'content-type: application/json' \
    -d '{"name":"lab02-admin"}' | jq -r .data.token)
PYTHONPATH=sdk/caliber-sdk caliber/.venv/bin/python -m examples.cookbooks.cookbook_02_precision_skills
echo "exit=$?"
```

It also creates a plain account `lab02-viewer` (password `lab02-viewer-pw-1`) to prove the 403s; running it again resets that account's password. Real output, trimmed
(`skill_name`, ids, run ids and the random suffix vary):

```json
{
  "archived": 3,
  "calibration": {"gate_passed": true, "job_id": "RFN-c8a7fd60", "n_examples": 0, "provider": "fake", "simulated": true, "status": "candidate_ready"},
  "cases": 40,
  "failures": {
    "invalid": {
      "angle brackets in the summary": "400 summary must not contain XML angle brackets (security restriction)",
      "empty content": "400 content: String should have at least 1 character",
      "name already in use": "409 skill name 'support-tone-and-citation-51ee5a' is already in use by 'SK-986675f0'",
      "name is not kebab-case": "400 name: String should match pattern '^[a-z0-9]+(?:-[a-z0-9]+)*$'",
      "reserved name prefix": "400 skill names starting with 'claude' are reserved",
      "unknown field": "400 bogus: Extra inputs are not permitted"
    },
    "no_baseline": {"baseline_run_id": null, "job_id": "RFN-46715676", "status": "candidate_ready"},
    "viewer": {
      "apply": "403 missing required scope; expected one of ['caliber.operator'], have ['caliber.viewer']",
      "calibrate": "403 missing required scope; expected one of ['caliber.operator'], have ['caliber.viewer']",
      "create": "403 missing required scope; expected one of ['caliber.operator'], have ['caliber.viewer']",
      "rollback": "403 missing required scope; expected one of ['caliber.admin'], have ['caliber.viewer']"
    }
  },
  "package": {
    "content_identical": true,
    "copy_name": "support-tone-and-citation-51ee5a-copy",
    "decisions_differing_after_metadata_restored": [],
    "decisions_differing_before_metadata_restored": ["T10", "T11", "T12", "T14", "T23", "T27", "T28", "T29", "T30", "T31", "T32", "T34", "T35"],
    "members": ["support-tone-and-citation-51ee5a/SKILL.md", "support-tone-and-citation-51ee5a/agents/openai.yaml"],
    "reimport_same_name": "409 skill name 'support-tone-and-citation-51ee5a' already exists; choose rename or merge explicitly"
  },
  "render": {"detected_variables": ["policy_context", "audience", "user_message"], "partial_unresolved": ["policy_context", "user_message"], "unresolved": [], "word_count": 640},
  "round1": {"accuracy": 0.75, "cases": 40, "correct": 30, "false_negatives": ["T14", "T23"], "false_positives": ["T27", "T28", "T29", "T30", "T31", "T32", "T34", "T35"]},
  "round2": {"accuracy": 1.0, "cases": 40, "correct": 40, "false_negatives": [], "false_positives": []},
  "round2_message_only": {"accuracy": 1.0, "cases": 40, "correct": 40, "false_negatives": [], "false_positives": []},
  "runs": {"baseline": "SKR-ac456588", "round1": "SKR-bfeab1ea"},
  "skill_id": "SK-986675f0",
  "skill_name": "support-tone-and-citation-51ee5a",
  "verified": true,
  "versions": {"after_apply": 2, "after_rollback": 3, "history": [3, 2, 1]}
}
```

`round2_message_only` sends the message without the case's `artifact_type` and `session_goal`, which is what the Scenario Sets runner on the page does; both modes are clean.
The list of cases that differ in the copy (`decisions_differing_before_metadata_restored`) depends a little on the skill's name; its being non-empty does not.
The same walk-through is replayed offline by `sdk/caliber-sdk/tests/test_cookbook_examples.py` (against a small stand-in for the skills routes), and the selector-level claims
(the 30/40 and 40/40, the exact miss lists, the render output, the invalid payloads, the package round trip) are pinned against the **real** routes and selector by
`caliber/tests/test_cookbook_02_trigger_dataset.py`.

### The same steps by hand, with `curl`

Every command below was run, in this order, on a fresh stack. Set up once:

```bash
source .tmp/cookbook-env/lab02/env.sh
LAB=docs-site/cookbooks/02-skill-trigger-packaging-lab/assets
JAR=.tmp/jar
curl -sS -c $JAR -X POST "$CALIBER_API/auth/login" -H 'content-type: application/json' -d '{"user_id":"admin","password":"admin"}'
api() { curl -sS -b $JAR -H 'content-type: application/json' "$@"; }
```

**1. Create the skill with only the six starter exclusions** (the payload is built from the asset file; the name must be new):

```bash
NAME=support-tone-and-citation-$RANDOM
caliber/.venv/bin/python - "$LAB" "$NAME" > .tmp/skill.json <<'PY'
import json, sys, yaml
lab, name = sys.argv[1], sys.argv[2]
_, front, body = open(f"{lab}/skills/support-tone-and-citation.md").read().split("---\n", 2)
meta = yaml.safe_load(front)
print(json.dumps({"name": name, "owner": "admin", "summary": meta["summary"], "category": meta["category"],
    "tags": meta["tags"], "content": body.strip(),
    "skill_metadata": {"test_triggers": {"should_not_trigger": meta["should_not_trigger"]}}}))
PY
SKILL=$(api -X POST "$CALIBER_API/skills" -d @.tmp/skill.json | jq -r .data.skill_id); echo "skill $SKILL"
```

**2. Render preview:**

```bash
jq '{variables}' $LAB/dataset/render-vars.json | api -X POST "$CALIBER_API/skills/$SKILL/test-render" -d @- \
  | jq -c '.data | {detected_variables, unresolved_variables, word_count}'
jq '{variables: .partial.variables}' $LAB/dataset/render-vars.json | api -X POST "$CALIBER_API/skills/$SKILL/test-render" -d @- \
  | jq -c '.data.unresolved_variables'
```

```text
{"detected_variables":["policy_context","audience","user_message"],"unresolved_variables":[],"word_count":640}
["policy_context","user_message"]
```

**3. Trigger round 1.** `round` runs all 40 cases through `test-selection` and writes a run body (one result per case, `pass` when the decision is as expected):

```bash
round() {
  jq -c . $LAB/dataset/trigger-cases.jsonl | while read -r case; do
    got=$(jq -c .inputs <<<"$case" | api -X POST "$CALIBER_API/skills/$SKILL/test-selection" -d @- | jq -r .data.is_selected)
    jq -c --argjson got "$got" '{name: .id, input: .inputs, output: {is_selected: $got},
      verdict: (if .expectations.is_selected == $got then "pass" else "fail" end),
      score: (if .expectations.is_selected == $got then 1 else 0 end)}' <<<"$case"
  done | jq -s .
}
round > .tmp/round1.json
jq -r '"round 1: \(map(select(.verdict=="pass"))|length)/\(length) right; wrong: \(map(select(.verdict=="fail")|.name)|join(" "))"' .tmp/round1.json
```

```text
round 1: 30/40 right; wrong: T14 T23 T27 T28 T29 T30 T31 T32 T34 T35
```

**4. Tighten, then round 2** (`PATCH` replaces `skill_metadata`, so this sends the whole `test_triggers` block; it changes metadata, not content, so the version stays 1):

```bash
jq '{skill_metadata: {test_triggers: .test_triggers}}' $LAB/skills/trigger-phrases.json \
  | api -X PATCH "$CALIBER_API/skills/$SKILL" -d @- | jq -c '.data | {version, skill_metadata: (.skill_metadata.test_triggers | map_values(length))}'
round > .tmp/round2.json
jq -r '"round 2: \(map(select(.verdict=="pass"))|length)/\(length) right"' .tmp/round2.json
```

```text
{"version":1,"skill_metadata":{"should_trigger":3,"should_not_trigger":14}}
round 2: 40/40 right
```

**5. Record round 2 as a run and pin it as the baseline:**

```bash
RUN=$(jq --arg id "$SKILL" '{skill_id: $id, kind: "selection", skill_version: 1, results: .}' .tmp/round2.json \
  | api -X POST "$CALIBER_API/skills/test-runs" -d @- | jq -r .data.test_run_id)
api -X POST "$CALIBER_API/skills/$SKILL/baseline" -d "{\"test_run_id\":\"$RUN\"}" | jq -c .data
api "$CALIBER_API/skills/$SKILL/workspace" | jq -c '.data | {lifecycle, baseline_run_id, last_run: .last_run.overall_score}'
```

```text
{"baseline_run_id":"SKR-d3e2e387"}
{"lifecycle":"Tested","baseline_run_id":"SKR-d3e2e387","last_run":1.0}
```

**6. Calibrate and review the candidate** (the worker takes a couple of seconds):

```bash
JOB=$(api -X POST "$CALIBER_API/skills/$SKILL/calibrate" -d '{}' | jq -r .data.job.job_id)
until [ "$(api "$CALIBER_API/jobs/$JOB" | jq -r .data.status)" = candidate_ready ]; do sleep 1; done
api "$CALIBER_API/jobs/$JOB" | jq -c '.data | {job_id, status, simulated: .eval_results.simulated, provider: .eval_results.provider,
  n_examples: .eval_results.n_examples, gate: .eval_results.gate.passed, diff_summary: .candidate.diff_summary}'
```

```text
{"job_id":"RFN-01c3931d","status":"candidate_ready","simulated":true,"provider":"fake","n_examples":0,"gate":true,"diff_summary":"+3 / -0 lines"}
```

**7. Apply, then roll back:**

```bash
api -X POST "$CALIBER_API/jobs/$JOB/apply" -d '{}' | jq -c '.data | {status, new_version: .promotion.details.new_version}'
api -X POST "$CALIBER_API/skills/$SKILL/rollback" | jq -c '.data | {version}'
api "$CALIBER_API/skills/$SKILL/versions" | jq -c '[.data[] | {version_number, chars: (.content|length)}]'
```

```text
{"status":"applied","new_version":2}
{"version":3}
[{"version_number":3,"chars":2699},{"version_number":2,"chars":2853},{"version_number":1,"chars":2699}]
```

Version 3 has the same length (and text) as version 1; version 2 is 154 characters longer, the three appended comment lines.

**8. Package: export, then import.** The reject import fails, the rename import succeeds, and a **merge** import (admin-only) updates the same-named skill as a new version:

```bash
curl -sS -b $JAR "$CALIBER_API/skills/$SKILL/package.zip" -o .tmp/$NAME.zip
unzip -l .tmp/$NAME.zip | tail -4
curl -sS -b $JAR -X POST "$CALIBER_API/skills/import-package.zip" -F "file=@.tmp/$NAME.zip" -F conflict_strategy=reject | jq -c .
curl -sS -b $JAR -X POST "$CALIBER_API/skills/import-package.zip" -F "file=@.tmp/$NAME.zip" \
  -F conflict_strategy=rename -F rename_to="$NAME-copy" | jq -c '.data | {name, version, category, tags, summary}'
```

```text
     2928  ...   support-tone-and-citation-31090/SKILL.md
      312  ...   support-tone-and-citation-31090/agents/openai.yaml
{"detail":"skill name 'support-tone-and-citation-31090' already exists; choose rename or merge explicitly","status_code":409}
{"name":"support-tone-and-citation-31090-copy","version":1,"category":"custom","tags":[],"summary":"Voice and citation rules for customer-facing support replies..."}
```

The copy keeps the name, description and instructions, but its `category` is `custom`, `tags` is empty, the trigger phrases are gone and the summary is the package's 64-character
interface line. Run the 40 cases against the copy and the decisions differ from the source's (13 of them in the run above); `PATCH` the tags, category, summary and `test_triggers` back
and they agree again (the example script does exactly this and asserts it). The package is an *instruction* package in the OpenAI skill folder shape; selection metadata is CALIBER's
and does not travel in it.

```bash
# optional, and it changes the source skill: merge the package back into the same-named skill
curl -sS -b $JAR -X POST "$CALIBER_API/skills/import-package.zip" -F "file=@.tmp/$NAME.zip" -F conflict_strategy=merge | jq -c '.data | {name, version}'
```

```text
{"name":"support-tone-and-citation-31090","version":4}
```

A merge writes the package's description, instructions **and its shortened summary** over the skill as the next version (here 4), so merging your own export degrades the summary and with it the selection.

**9. Failure: invalid content.** The first three payloads of `invalid-skills.json`:

```bash
jq -c '.[0:3][] | .payload' $LAB/skills/invalid-skills.json | while read -r payload; do
  api -X POST "$CALIBER_API/skills" -d "$payload" | jq -c '{detail, errors: [.errors[]? | .msg]}'
done
```

```text
{"detail":"request body validation failed","errors":["String should match pattern '^[a-z0-9]+(?:-[a-z0-9]+)*$'"]}
{"detail":"skill names starting with 'claude' are reserved","errors":[]}
{"detail":"summary must not contain XML angle brackets (security restriction)","errors":[]}
```

**10. Archive:**

```bash
api -X PATCH "$CALIBER_API/skills/$SKILL" -d '{"status":"archived"}' | jq -c '.data | {name, status}'
```

```text
{"name":"support-tone-and-citation-31090","status":"archived"}
```

The viewer and no-baseline checks are in the example script (a plain account is `caliber.viewer` only; a skill nobody has recorded a run for calibrates exactly the same way).

## Recipe: run it through the UI

`caliber/caliber-ui/e2e/cookbooks/labs/02-skill-trigger-packaging.spec.ts` performs these twelve steps with real clicks and asserts what each says you will see. With the stack
above up:

```bash
cd caliber/caliber-ui
CALIBER_LAB_BASE_URL="$CALIBER_URL/caliber" npx playwright test --config=playwright.labs.config.ts e2e/cookbooks/labs/02-skill-trigger-packaging.spec.ts
```

Open `$CALIBER_URL/caliber/` and sign in as `admin` / `admin` first. The sidebar groups are **Get started** (Cookbooks is here, with the flag on), **Prompt lifecycle**, **Agent resources**
(Skills is here), **Operate**, **Integrations** and **Admin** (Administration is here).

1. **Open the gallery, then create a workspace.** Get started › **Cookbooks**: the **Cookbook 02 / Precision Skills** card says **Ready** and lists `skills`, `trigger tests`, `packages`, `calibration`
   (you do not install it; the lab runs on Skills). Then, in the top bar, click **+** (Create workspace), type a name, **Create and select**. The lab runs inside a workspace so that a viewer can
   be added in step 11.
2. **Build the skill.** Agent resources › **Skills** › **Build Skill**. *Identity:* Name `support-tone-and-citation-<suffix>` (the box turns spaces and underscores into hyphens), Owner `admin`,
   category **Customer Support**, tags `support`, `tone`, `citation`, `escalation`, `customer-facing` (type each, **Add**). *Content:* the `summary` line and the body of
   [`support-tone-and-citation.md`](assets/skills/support-tone-and-citation.md). *Composability:* skip. *Triggers:* under **Should NOT trigger** add the six starter phrases (`stack trace`,
   `signing key`, `database index`, `sql migration`, `latency metrics`, `unit test`). *Review & Create* then **Create Skill**. You are back on the list; **Open** the skill. The header shows
   `v1`, and the **Author** stage's version history shows version 1 as live.
3. **Render Preview.** Stage **Render Preview**: paste the `variables` object of [`render-vars.json`](assets/dataset/render-vars.json) into *Variables JSON*, **Render Skill**. *Rendered Output* shows the policy
   text with `Cite [KB-1042] (duplicate-charge refunds)` and `Audience: non-technical customer`, no `{{ }}` left, and no "unresolved" badge. Replace the JSON with the `partial` variables (only `audience`)
   and render again: the badge reads **2 unresolved** and `{{ policy_context }}` stays in the output.
4. **Check selection: a positive, a negative, a near miss.** Stage **Trigger Tests**, fill *User message* (and the optional artifact type and session goal) from `trigger-cases.jsonl`, **Check selection**. `T01`
   (a refund question) shows **selected**, with a score and a reason such as `auto:name+tag+summary`. `T07` (rotate the JWT signing key) shows **not selected**, reason
   `excluded:negative_trigger:signing key`. `T27` (a crash-looping billing service) shows **selected**, reason `auto:summary`: the starter phrases let it through, a false positive.
5. **Scenario Sets, round 1.** Stage **Scenario Sets**: add the twelve cases `T01 T07 T28 T14 T15 T23 T27 T29 T31 T32 T34 T35` (type the message, untick **Expected: skill should be selected** for the engineering ones,
   **Add scenario**; it lists `12 scenarios`). Back on **Trigger Tests**, **Run 12 scenarios**. The runner sends the message alone, so the verdicts are: **9 `regression`** markers (`T14 T15 T23 T27 T29 T31 T32 T34 T35`) and
   **3 `matches expectation`** (`T01 T07 T28`). **Save as run**: `Saved run SKR-…`. (Scenarios live in the page; leaving it clears them, which is why step 7 adds them again.)
6. **Tighten the trigger phrases.** Click **Details & package** in the skill header (the skill's detail page, `/skills/<skill_id>`). Its `skill_metadata` panel shows `should_not_trigger` with the six phrases. **Edit**, and in *Metadata JSON* replace
   the `test_triggers` block with the one in [`trigger-phrases.json`](assets/skills/trigger-phrases.json) (3 positive and 14 negative phrases), keeping the rest of the JSON; **Save**. The page says **Saved.** (metadata only: the version does not change) and the panel now lists `kubernetes`.
7. **Round 2, then pin the baseline.** Open the skill again, add the same twelve scenarios, **Run 12 scenarios**: **12 `matches expectation`**, no `regression`. **Save as run**. Stage **Runs**: *Run history* lists two runs; the latest is `100%` (`12 pass`); click **Set as baseline** and it gets the **Baseline**
   marker. Select the older run in the history: **Vs. baseline** shows `Net score -75%` and `9 regressions`.
8. **Calibrate and review.** (On a real-provider stack first create the test set and pick it next to **Calibrate**: see [Run it with a real provider](#run-it-with-a-real-provider).) On **Runs**, **Calibrate**: `Calibration queued — job RFN-…`. Under **Calibration runs** the job reaches `candidate_ready` (seconds) and shows **Review & apply**. Open it: the amber banner
   **Simulated evaluation — not evidence**, `0 evaluation examples`, the Baseline / Candidate / Change scores, `gate passed`, and the diff with `Refinement added by SkillMetaPrompt`.
9. **Apply, then roll back.** **Apply to skill**: `Applied RFN-… — the skill is now version 2.` Stage **Author**: version 2 has the **live** badge and the *Skill content* box ends with the `# Refinement added by SkillMetaPrompt` lines. In the *Version history*, on version 2 click
   **Roll back**, confirm: version **3** is live and the content is the original again (no refinement lines).
10. **Package: export, reject, rename.** **Details & package** again. **Download ZIP** saves `<name>.zip`. With *Reject conflicts* selected, **Import ZIP** with that file shows `Import failed: ... already exists; choose rename or merge explicitly`. Choose **Rename import**, type
    `<name>-copy` in *Renamed skill name*, **Import ZIP** again: you land on the copy's detail page. Its `skill_metadata` has `openai_package` but no `test_triggers`. Open the copy › **Trigger Tests** and check `T28` (add a customer_id foreign key to the invoices table): **selected**, where the original says **not selected**.
11. **Failure paths.** (a) Skills › **Build Skill**: a name starting with a hyphen shows *Must be kebab-case...* and **Continue** is disabled. Use `claude-helper-<suffix>`, any summary and content, **Create Skill**: the wizard shows `skill names starting with 'claude' are reserved`. Rename it, leave `<b>bold</b>` in the summary, **Create Skill**:
    `summary must not contain XML angle brackets`. **Close** (nothing was created). (b) Build a second skill with no baseline, open **Runs**: *No saved runs yet* and no Baseline marker. **Calibrate** reaches `candidate_ready` and the review is the same (simulated, `0 evaluation examples`): a baseline is not a precondition of calibration.
    (c) Admin › **Administration** › *Accounts*: create a user (password of 12 or more characters); *Project access*: select your workspace and **Add member** with role **Viewer**. In a private window sign in as that user, pick the workspace in the top bar, open the second skill › **Runs**, **Review & apply**, **Apply to skill**: the red alert shows the server's
    `403` message (`missing required scope ...`) and no `Applied` banner appears; **Calibrate** shows the same refusal. A viewer can read the skill and its runs but cannot change anything.
12. **Archive.** For each skill you built: **Details & package** › **Archive**: `Skill archived.` On the Skills list its card now offers **Restore**.

## Run it with a real provider

Everything above runs on simulated providers. This section is the same lab on a stack with a real model and the real evaluator, and what changes. Run on 2026-10-19 with the
default model (`gpt-5.6-luna`); generated text and scores **will vary** from run to run, so the script and the spec assert invariants only (ranges, counts, markers), never wording.

**1. Start a real-provider stack.** You need an OpenAI key of your own; the key is read from your shell and is never printed or written to the stack's files:

```bash
export OPENAI_API_KEY=sk-...          # your key; do not commit it, do not paste it into a README
scripts/cookbook-env.sh up lab02h --flags cookbooks --real-llm
source .tmp/cookbook-env/lab02h/env.sh
curl -s $CALIBER_API/readiness | jq -c '.data | {simulated, all_real, providers}'
```

```text
{"simulated":[],"all_real":true,"providers":{"llm":"openai","eval":"mlflow","promoter":"mlflow","artifact_store":"mlflow"}}
```

`simulated: []` is what the script and the spec read to pick the real branch. Then make the admin token as in the API section.

**2. What changes, and what does not.** Trigger rounds, render preview, baseline, apply, rollback, packages and the failure paths are identical (they never touch a model). Three things differ:

| | Simulated | Real |
| --- | --- | --- |
| Calibrate needs | nothing | a **test set**: `POST /skills/{id}/calibrate {"eval_dataset_id": "...", "notes": "..."}` (UI: **Test set** and **What should change** next to Calibrate) |
| Calibration run time | 2 to 3 seconds | 45 to 70 seconds |
| Candidate / evidence | canned, flagged simulated, `n_examples` 0 | model-written, `simulated: false`, `n_examples` 8, scores in `[0, 1]` |
| The gate | passes with the canned scores | **can reject** a real candidate (see troubleshooting) |

**3. The test set** is [`dataset/skill-eval-cases.jsonl`](assets/dataset/skill-eval-cases.jsonl): eight support-reply cases, each `{input: {user_message, audience, policy_context}, expected: {behavior}}`.
The evaluator uses the skill's text as the system instruction (its `{{ policy_context }}`, `{{ audience }}` and `{{ user_message }}` are filled from the case) and scores each reply against `behavior`.

```bash
DS=$(api -X POST "$CALIBER_API/eval-datasets" -d '{"name":"lab02-skill-replies-'$RANDOM'","owner":"admin"}' | jq -r .data.dataset_id)
while read -r line; do jq -c '{input, expected, tags}' <<<"$line" \
  | api -X POST "$CALIBER_API/eval-datasets/$DS/examples" -d @- >/dev/null; done < $LAB/dataset/skill-eval-cases.jsonl
api "$CALIBER_API/eval-datasets/$DS" | jq -c '.data | {dataset_id, version}'
```

```text
{"dataset_id":"ED-a010467a","version":9}
```

**4. Calibrate against it, with a note on what to fix** ([`skills/calibration-notes.txt`](assets/skills/calibration-notes.txt)), then read the evidence (continuing from step 1 of the `curl` walk-through, which created `$SKILL`):

```bash
JOB=$(api -X POST "$CALIBER_API/skills/$SKILL/calibrate" \
  -d "$(jq -nc --arg ds "$DS" --rawfile n $LAB/skills/calibration-notes.txt '{eval_dataset_id:$ds, notes:$n}')" | jq -r .data.job.job_id)
until [ "$(api "$CALIBER_API/jobs/$JOB" | jq -r .data.status)" != running ] && [ "$(api "$CALIBER_API/jobs/$JOB" | jq -r .data.status)" != queued ]; do sleep 3; done
api "$CALIBER_API/jobs/$JOB" | jq '.data | {status, diagnosis: .diagnosis.root_cause, rationale: .candidate.rationale, diff: .candidate.diff_summary,
  eval: (.eval_results | {provider, simulated, n_examples, eval_dataset_id, eval_dataset_version, candidate: .candidate.overall, baseline: .baseline.overall, dimensions: (.candidate.dimensions | keys), gate: .gate.passed})}'
```

Real output, trimmed (ids, wording and scores will vary):

```json
{
  "status": "candidate_ready",
  "diagnosis": "The skill does not require replies to derive dates and amounts exclusively from policy_context or to cite only KB IDs explicitly supplied in the available context.",
  "rationale": "The edit makes `{{ policy_context }}` the exclusive source for every date and amount, prohibiting guesses, calculations, or customer-provided values. It also restricts citations to KB IDs explicitly supplied in that context or other available input.",
  "diff": "+7 / -2 lines",
  "eval": {"provider": "mlflow", "simulated": false, "n_examples": 8, "eval_dataset_id": "ED-a010467a", "eval_dataset_version": 9,
           "candidate": 0.9583, "baseline": 0.9583, "dimensions": ["correctness", "relevance_to_query", "safety"], "gate": true}
}
```

The candidate, the Review step shows as a diff (this one added a rule and tightened a second; `+7 / -2`):

```diff
+- Derive every date and amount in the reply exclusively from `{{ policy_context }}`.
+  Do not guess, infer, calculate, or use dates or amounts from the customer
+  message or any other source. If the policy context does not support one,
+  omit it and say: "I'm confirming the exact policy and will follow up."
-- Only cite article ids that appear in `{{ policy_context }}`. Never invent a
-  KB id, and never cite an id you were not given.
+- Only cite article ids explicitly supplied in `{{ policy_context }}`. Never use
+  ids from the customer message, model knowledge, or any other source; never
+  invent a KB id or cite an id you were not given.
```

That is a sensible new version: it does what the note asked and nothing else. Apply and roll back are the same calls as before and gave `{"status":"applied","new_version":2}`,
`{"version":3}` and the history `[3, 2, 1]`.

What the evidence does and does not tell you (read this before you trust it): the baseline and the candidate both scored `0.9583` here, so **the run measured no improvement**; it measured
that the candidate did not regress on eight cases. A repeat run on the same skill scored baseline `0.9167`, candidate `1.0`. Eight rows is coarse: one row moves a score by `0.125`, and the
gate (`min_aggregate_score` `0.85`, `max_regression_delta` `0.02`) tolerates no drop at all, so it also rejects on noise.

**Without a note, the model has nothing to fix.** The first real calibration of the same skill, with the test set but no `notes`, produced a diagnosis that said the verification record contained
no behavioural evidence, and a candidate that appended a "Verification trace" section asking the assistant to emit a non-customer-facing record next to the reply (`+9 / -0 lines`, gate passed, scores
identical). It applies fine and it is not an improvement; the review is where you catch that. Give calibration a note naming the defect you saw, or use a test set row for it.

**5. The failure that only a real provider has: no test set.**

```bash
api -X POST "$CALIBER_API/skills/$SKILL/calibrate" -d '{}' | jq -r .data.job.job_id      # then read the job as above
```

```json
{"status": "failed", "current_stage": "eval", "error_message": "eval provider error: failed to load eval dataset 'default': eval dataset 'default' has no active examples"}
```

The candidate was generated but there was nothing to score it against. Naming a test set that is wrong is refused up front, before a job exists:
`404 eval dataset 'ED-nope' not found`, `400 eval dataset '...' has no active examples to calibrate against`, `400 eval_dataset_version must be between 1 and 9 for dataset '...', got 99`, `400 eval dataset '...' is not active`.

**6. The script and the spec run in both modes.** The script reads `GET /readiness`; the spec reads the provider banner the app shows ("Simulated providers: ...", absent on a real stack); nothing else changes in how you call them:

```bash
PYTHONPATH=sdk/caliber-sdk caliber/.venv/bin/python -m examples.cookbooks.cookbook_02_precision_skills      # about 2 to 2.5 minutes
cd caliber/caliber-ui && CALIBER_LAB_BASE_URL="$CALIBER_URL/caliber" \
  npx playwright test --config=playwright.labs.config.ts e2e/cookbooks/labs/02-skill-trigger-packaging.spec.ts   # about 4 minutes
```

In real mode the script prints `"mode": "real"` and a calibration block like this (and `failures.no_test_set` for the run above); it exits `0` only if `simulated` is false, `provider` is not `fake`,
`n_examples` is 8, the dataset id matches, both scores are in `[0, 1]`, the gate passed, the candidate differs from the skill, apply and rollback give versions `[3, 2, 1]`, and a calibration with
no test set fails with `no active examples`. If the gate rejects the candidate it calibrates again, up to three attempts in all (`attempts` says how many it took); more than that fails loudly:

```json
{
  "mode": "real",
  "calibration": {"attempts": 1, "baseline_overall": 0.9167, "candidate_overall": 1.0, "diff_summary": "+4 / -2 lines",
                  "dimensions": ["correctness", "relevance_to_query", "safety"], "gate_passed": true, "job_id": "RFN-e5c8e4c5",
                  "n_examples": 8, "provider": "mlflow", "simulated": false, "status": "candidate_ready"},
  "failures": {"no_baseline": {"baseline_run_id": null, "job_id": "RFN-6603a24f", "status": "candidate_ready"},
               "no_test_set": {"job_id": "RFN-d254020d", "status": "failed"}, "...": "viewer and invalid as in the simulated output"},
  "versions": {"after_apply": 2, "after_rollback": 3, "history": [3, 2, 1]},
  "archived": 3,
  "verified": true
}
```

The UI spec does the real branch with clicks: step 9 first goes to **Test Sets**, creates `lab02-skill-replies-<suffix>` and adds the eight rows through the example editor, then on **Runs** picks the set in **Test set**,
types the note, **Calibrate**, and (if the gate rejects the run, which stays in the list with its reason) calibrates again, at most three times. The review shows **no** "Simulated evaluation" banner,
`8 evaluation examples` and `gate passed`; the skill then has a model-written version 2, which Roll back replaces with version 3 (the original text). Step 12b does the no-test-set run
(the run row says `failed ... no active examples`) and then calibrates the baseline-less skill with the test set.

**Cost and time.** One 8-row calibration is a diagnosis call, a candidate call, then 16 predictions (the skill and the candidate on eight cases) and the evaluator's scorers over them: dozens of small model
calls, 45 to 70 seconds, a few cents at the default model's price (the job reports `total_tokens: 0`, so this is an estimate, not a measurement). The full script does about four calibrations (the lab skill, a
second skill with no baseline, and the failing one that stops after the candidate call); the spec about as many, and either reruns when the gate rejects. Budget a few minutes and a small amount of money per run.

**Troubleshooting the real tier.**

| What you see | Cause and fix |
| --- | --- |
| Calibration `failed` with `diagnosis LLM call failed: Error code: 401 ... Incorrect API key provided` (also `status: failed` on `POST /evaluations` with the same text) | The key is wrong or revoked. Provoked on purpose with `OPENAI_API_KEY=sk-invalid scripts/cookbook-env.sh up lab02bad --flags cookbooks --real-llm`. OpenAI echoes a masked copy of your key in this message: redact it before pasting. Fix the key, `scripts/cookbook-env.sh restart` (it re-reads it), and calibrate again. |
| Calibration `failed` with `... has no active examples` | You did not pass `eval_dataset_id` (or the set is empty). Create the test set and pass it. |
| Calibration `rejected` with `regression gate failed: dimension 'correctness' regressed by -0.375 (max allowed -0.020)` or `overall 0.833 below min_aggregate_score 0.850` | The gate is doing its job on a real candidate: the model-written skill scored worse than the current one on at least one row, or the skill scores below `0.85` outright on this set (a one-line skill does). It is noisy at eight rows: calibrate again, or add rows. Seen in about one real run in three here. |
| `429` or timeouts in the job's `error_message` | Provider rate limit or a slow model. The script retries a job whose error mentions 429, rate limit, timeout; otherwise wait and rerun. |
| `model not found` / `404` from the provider | The default model name is not available to your key. Set `CALIBER_LLM_DIAGNOSIS_MODEL` to a model your key can use, before `up`. |
| The review still says "Simulated evaluation" | The stack is simulated: `curl $CALIBER_API/readiness` must show `"simulated": []`. Restart it with `--real-llm`. |

## Verify it worked

| Check | Expected |
| --- | --- |
| `GET /skills/{id}/versions` | version numbers `[3, 2, 1]`; the content of 3 equals the content of 1; 2 ends with `# Refinement added by SkillMetaPrompt` lines |
| `GET /skills/{id}/workspace` | `baseline_run_id` is the round-2 run id; `last_run.overall_score` is `1.0`; the no-baseline skill has `baseline_run_id: null` |
| `GET /skills/test-runs?skill_id={id}` | the example script saves two `selection` runs of 40 results: `30 pass / 10 fail` (round 1) and `40 pass` (round 2); the `curl` walk-through saves only round 2 |
| `GET /jobs/{job_id}` | `status: candidate_ready` (then `applied`); simulated stack: `eval_results.simulated: true`, `provider: "fake"`, `n_examples: 0`; real stack: `simulated: false`, `provider: "mlflow"`, `n_examples: 8`, `eval_dataset_id` set |
| `GET /skills/{id}/package.zip` | a ZIP of exactly `<name>/SKILL.md` and `<name>/agents/openai.yaml` |
| `GET /audit-log` (or Operate › **Audit Log**) | rows by `admin` for `create_skill`, `update_skill`, `create_skill_test_run`, `set_skill_baseline`, `create_job`, `candidate_ready`, `apply_candidate`, `rollback_skill`, `import_skill_package_zip` (and `merge_skill_package_zip` if you merged) |
| `GET /skills?status=archived` | every skill the lab created |
| `python -m pytest caliber/tests/test_cookbook_02_trigger_dataset.py` | passes offline: the 30/40 and 40/40 and the exact miss lists, with the real selector |

## Common errors and troubleshooting

| What you see | Cause and fix |
| --- | --- |
| `404 feature_disabled` on `/cookbooks`, and no **Cookbooks** entry in the sidebar | `CALIBER_FEATURE_COOKBOOKS` is off. Start with `--flags cookbooks`. Skills themselves work without the flag. |
| `503` from `/readiness`, or the calibration run stays `queued` | The stack is not ready, or no worker runs (`CALIBER_BACKGROUND_TASKS_ENABLED`). `cookbook-env.sh` starts one; a bare `caliber-server` needs the variable. |
| `403 missing required scope; expected one of ['caliber.operator']` (or `['caliber.admin']`) | Your account is a viewer. Create, calibrate, record runs, baseline, apply and import need `caliber.operator`; patching a skill and rolling back need `caliber.admin`. |
| `404` on a skill you can see as admin | The skill is in a workspace you are not a member of (an admin is not an implicit member), or you are sending the wrong `X-CALIBER-Project`. |
| `409 skill name '...' is already in use` / `already exists; choose rename or merge explicitly` | Names are unique across the deployment, and an archived skill keeps its name. Pick a new one (`COOKBOOK_02_SKILL_NAME`), or use `rename`. |
| `400 request body validation failed` with `String should match pattern` | The name is not kebab-case (`^[a-z0-9]+(?:-[a-z0-9]+)*$`). The wizard's name box fixes spaces and underscores itself, the API does not. |
| `400 ... reserved` / `must not contain XML angle brackets` | A name starting with `claude` or `anthropic`; `<` or `>` in the summary or description. |
| Selection scores are not the ones in this README | Scores depend on the skill's name (a name word in the query adds 5). Compare decisions and reasons, not scores. |
| A near miss selects the skill (`auto:summary`) | One summary word is enough. Add a specific `should_not_trigger` phrase (not a short one: `api` would match `rapid`), or narrow the summary. Editing the content changes nothing. |
| A positive is not selected (empty reason) | The message shares no word with the name, tags or summary. Add a `should_trigger` phrase (`refund`, `subscription`, ...). |
| `Import failed: SKILL.md frontmatter requires 'description'` | A package without a description. Skills exported by this build always carry one (the summary stands in when the description is empty), so this only happens for a hand-made ZIP. |
| Scenarios disappeared | They live in the page; opening Details & package or reloading clears them. Re-add them, or run the cases with the example script. |
| The review says `Simulated evaluation — not evidence` | Expected on the default stack: see the table at the top. Apply only if you have judged the diff yourself. For measured evidence use a real-provider stack: [Run it with a real provider](#run-it-with-a-real-provider). |

## Not covered by this lab

- **Bind** (`POST /skills/{id}/bind`, the Bind stage) is not part of the lab; it is a separate step that attaches the skill to an agent or workflow node.
- A calibration that demonstrably **improves** the skill: the simulated stack cannot say, and on a real stack the eight-row test set is too coarse to show a gain (the baseline and the candidate scored the same in one run, `0.9167` against `1.0` in another).
- Project roles other than Viewer. The viewer in step 11 and in the script are checked at the API (`403`); the project role matrix is in
  [Auth and project scoping](../../../docs/build/auth-and-project-scoping.md).
- The Aria assistant picking the skill: Aria is a preview area behind its own flag.

## Cleanup and reset

Archive what the lab created (the example script does it itself; the UI path ends with it). A skill cannot be deleted, so its name stays taken: use a new suffix next time.

```bash
# archive by hand
curl -sS -b $JAR -X PATCH "$CALIBER_API/skills/$SKILL" -H 'content-type: application/json' -d '{"status":"archived"}'
# disable the viewer account the script created
curl -sS -b $JAR -X PATCH "$CALIBER_API/auth/accounts/lab02-viewer" -H 'content-type: application/json' -d '{"disabled":true}'
# revoke the token you made
curl -sS -b $JAR "$CALIBER_API/auth/tokens" | jq -r '.data.tokens[] | select(.name=="lab02-admin") | .token_id'   # then DELETE /auth/tokens/<id>
# throw the whole disposable stack away
scripts/cookbook-env.sh down lab02 && rm -rf .tmp/cookbook-env/lab02
```
