# Alignment worksheet (manual)

CALIBER does **not** auto-compute alignment or disagreement rate (FEASIBILITY
§1, Evaluations). You tally the automated judge verdicts against the human
reviewer answers by hand here, on the sampled traces.

- **judge_verdict** — `Judge.FaithfulnessJudge` result for the row (bool → pass /
  fail), read from the Evaluations scorecard per-example detail.
- **human_verdict** — the reviewer's `faithful` answer from
  [`../review/review-questions.json`](../review/review-questions.json) (yes →
  pass, no → fail), read off the trace after the review queue is answered.
- **agree?** — `yes` when both verdicts match, `no` when they differ.

## Fill-in table

One row per sampled trace. Use the eval run's per-example trace id (or the row
`id` if you are tracking by dataset row). Fill `judge_verdict` and
`human_verdict` from the two surfaces; mark `agree?`.

| trace_id (or row id) | judge_verdict (pass/fail) | human_verdict (pass/fail) | agree? (yes/no) |
| --- | --- | --- | --- |
| J01 |  |  |  |
| J02 |  |  |  |
| J05 |  |  |  |
| J06 |  |  |  |
| J08 |  |  |  |
| J03 |  |  |  |
| J07 |  |  |  |
| J04 |  |  |  |

> Sample at least 3 traces (the gate's `reviewed_trace_count_min`). Bias the
> sample toward the examples the judge flagged false and a few it passed, so you
> are testing both failure and success agreement — not just the easy passes.

## Compute alignment

```
alignment = agreements / total_reviewed
```

Count the `agree? = yes` rows, divide by the number of rows you reviewed.

**Gate** (verification.yaml): `alignment ≥ 0.80` over `≥ 3` reviewed traces, and
`overall_eval_score ≥ 0.85`.

### Worked example (using this scenario's dataset)

The dataset in [`../dataset/aggregated-outputs.jsonl`](../dataset/aggregated-outputs.jsonl)
is built so a strict faithfulness judge and the human reviewer line up on most
rows but **deliberately disagree on two** — the `expected_disagreement`
partial-evidence rows `J03` and `J07` (human passed them as reasonable; the
strict judge fails them for an unsupported cause/ETA). Expected verdicts:

| row | judge_verdict | human_verdict | agree? |
| --- | --- | --- | --- |
| J01 | pass | pass | yes |
| J02 | fail | fail | yes |
| J05 | pass | pass | yes |
| J06 | fail | fail | yes |
| J08 | pass | pass | yes |
| J03 | fail | pass | **no** |
| J07 | fail | pass | **no** |
| J04 | fail | fail | yes |

- Review the **5 clear rows** (J01, J02, J05, J06, J08): `5/5 = 1.00` → **passes**
  the ≥ 0.80 gate over ≥ 3 traces.
- Review **all 8**: `6/8 = 0.75` → **below** the 0.80 gate. That dip is the
  signal, not noise: the two partial-evidence rows are a genuine rubric
  ambiguity to resolve before this judge gates releases.

(Your live numbers will depend on the actual judge model and reviewer answers;
fill the blank table above from real runs — this worked example just shows the
arithmetic and the expected shape.)

## What counts as a disagreement (and what to do with it)

A **disagreement** is any row where `agree? = no`:

- **Judge fail, human pass** (e.g. J03, J07) — the rubric is too strict, or the
  output is borderline. Either reword the judge instructions
  ([`../judges/faithfulness-judge.judge.json`](../judges/faithfulness-judge.judge.json))
  to define how much hedged/partial evidence is acceptable, or accept it as a
  hard case the judge should learn from.
- **Judge pass, human fail** — more dangerous: the judge is over-trusting. Tighten
  the rubric so the missed unsupported claim is caught.

For every disagreement:

1. **Add the hard case back to the dataset.** Harvest the reviewed trace into the
   dataset (`POST /eval-datasets/{id}/examples/from-trace {trace_id}`), or add the
   row manually with the human verdict as `human_label`. This grows the
   certification set with exactly the cases that exposed drift.
2. **Reconcile the rubric.** Reword the judge instructions and/or the review
   question so both express the same, now-clarified criterion (keep them in
   sync). Use [`../skills/rubric-question-writer.md`](../skills/rubric-question-writer.md)
   to keep the question single-criterion.
3. **Re-run** the evaluation (recipe step 3) and re-tally here until alignment
   clears the gate. Document the disagreements you resolved — that record is part
   of the gate ("Disagreements are documented and fed back").
