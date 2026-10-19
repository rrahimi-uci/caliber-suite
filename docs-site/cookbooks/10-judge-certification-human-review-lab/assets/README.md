# SCN-10 assets

Everything [the lab README](../README.md) loads, runs or checks. This lab certifies that an
automated judge and human reviewers agree before the judge is trusted to block releases.

| File | What it is | Used by |
| --- | --- | --- |
| [`dataset/aggregated-outputs.jsonl`](dataset/aggregated-outputs.jsonl) | The 50-row test set: 22 support replies (SCN-07) and 28 incident summaries (SCN-08), each with the evidence the answer must stay within, the recorded output, and a reviewer's pass/fail label with the reason. 21 clear passes, 17 clear failures, 12 borderline cases. | API script, UI upload, `seed_traces.py`, stand-in model |
| [`judges/faithfulness-judge.judge.json`](judges/faithfulness-judge.judge.json) | The `FaithfulnessJudge` definition (`name`, `instructions`, `feedback_value_type: bool`). | API script, Judges > + New Judge |
| [`review/review-questions.json`](review/review-questions.json) | The review queue: `faithful` (pass/fail) and `evidence` (text). | API script, Review Queues > New queue |
| [`seed_traces.py`](seed_traces.py) | Writes the 50 recorded outputs as MLflow traces (one per row, tagged `cookbook10.row`) and prints a row-to-trace-id map. In production your application produces these. | API script, UI path |
| [`stand-in-model/stand_in_model_server.py`](stand-in-model/stand_in_model_server.py) | A scripted OpenAI-compatible server so Evaluations and the judge can run without a provider key. It proves wiring and arithmetic, not judge quality. | any run without a real model |
| [`alignment/alignment-worksheet.md`](alignment/alignment-worksheet.md) | The agreement and kappa arithmetic worked by hand, the certification rule, and what to do with a disagreement. | reading |
| [`alignment/expected-alignment.csv`](alignment/expected-alignment.csv) | Both verdict columns for all 50 rows (human, and the stand-in judge) so the numbers can be recomputed. | reading, tests |
| [`skills/rubric-question-writer.md`](skills/rubric-question-writer.md) | Optional: a skill that helps word single-criterion review questions. Not needed to run the lab. | optional |

## Dataset format

One JSON object per line:

```json
{"id": "J03", "tags": ["scn-07", "partial_evidence", "borderline", "..."],
 "input":    {"ticket_text": "...", "channel": "chat", "account_id": "acct_2101"},
 "expected": {"observed": "...", "root_cause": null, "expected": "stuck", "decision": "escalate_bug"},
 "outputs":  {"reply": "...", "decision": "reply"},
 "human_label": "fail", "human_label_reason": "..."}
```

- `input`, `expected` and `tags` are what CALIBER stores (the UI upload and
  `POST /eval-datasets/{id}/examples` read exactly these). `expected` holds the **evidence** the
  judge treats as the only source of truth, plus one `expected` string: the key fact a correct
  answer must state, which the deterministic **Contains expected** grader looks for.
- `outputs` is the recorded answer under test (`reply` or `summary`). It is not stored in the test
  set (an Evaluation generates its own answers); it becomes the response of the trace
  `seed_traces.py` writes, and so the output the judge reads during alignment.
- `human_label` / `human_label_reason` are the reviewer's decision. They are not CALIBER fields; the
  API script submits them as the review answers.
- Tags: `clear_pass` / `clear_fail` / `borderline` say how hard a row is. `expected_disagreement`
  marks the nine rows the lab's stand-in judge gets wrong (five it should have failed, four it
  should have passed). The other tags name the failure mode (`wrong_number`, `hallucinated_step`,
  `derived_number`, `malformed_output`, ...).

## Conventions

- **Judge files**: `{name, model, instructions, feedback_value_type}`. Instructions may use only
  `{{ inputs }}`, `{{ outputs }}`, `{{ expectations }}`, `{{ conversation }}`, `{{ trace }}`;
  anything else is refused on save. `feedback_value_type` is one of bool, int, float, str (bool
  here: pass/fail).
- **Review files**: an array of questions `{key, title, type, required, target}` with `type` one of
  `pass_fail`, `categorical`, `numeric`, `text`.
- **Skill files**: YAML frontmatter (name, summary, optional category/tags/render_variables) then the
  literal skill body; variables are `{{ snake_case }}`. Kebab-case names; `claude*` and
  `anthropic*` prefixes are rejected.
