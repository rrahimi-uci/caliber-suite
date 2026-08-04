# SCN-10 assets — create these

Concrete, copy-pasteable artifacts for [the recipe](../README.md). This gate
certifies that an automated judge and human reviewers agree before the judge is
trusted to block releases. Build order:

| # | Artifact | File | Create via |
| --- | --- | --- | --- |
| 1 | Eval dataset `judge-certification-set` | [`dataset/aggregated-outputs.jsonl`](dataset/aggregated-outputs.jsonl) | **Prefer harvest-from-trace.** For each SCN-07/08 run you want graded: `Evaluate → Test Sets → New dataset`, then `POST /eval-datasets/{id}/examples/from-trace {trace_id}` (auto-extracts `inputs`/`outputs`/`expectations`). To seed without live traces, `POST /eval-datasets/{id}/examples` per line. |
| 2 | Judge `FaithfulnessJudge` | [`judges/faithfulness-judge.judge.json`](judges/faithfulness-judge.judge.json) | `Evaluate → Judges → New judge`, paste fields. API: `POST /judges`. |
| 3 | Review queue `faithfulness-certification` | [`review/review-questions.json`](review/review-questions.json) | `Observe → Review Queues → New queue`, add the questions. API: `POST /review-queues`. |
| 4 | (optional) Skill `rubric-question-writer` | [`skills/rubric-question-writer.md`](skills/rubric-question-writer.md) | `Library → Skills → New skill`, paste body. API: `POST /skills`. Helps draft single-criterion questions; not required to run the gate. |
| 5 | Alignment evidence | [`alignment/alignment-worksheet.md`](alignment/alignment-worksheet.md) | Use the worksheet only as an optional export/check; the Judges UI imports completed queue labels and computes the metrics. |

## Run order (the certification loop)

1. **Prepare the dataset (step 1).** Aggregate representative candidate outputs
   from SCN-07 (support replies) and SCN-08 (incident summaries). Harvest from
   traces where you can (`.../examples/from-trace`) so each row keeps its
   `{inputs, outputs, expectations}` shape and trace lineage; the shipped
   `aggregated-outputs.jsonl` is that same shape for seeding/reference.
2. **Define the judge (step 2).** Create `FaithfulnessJudge`
   (`feedback_value_type: bool`) — true only if **every** claim in `{{ outputs }}`
   is supported by `{{ expectations }}`. The instructions reference
   `{{ inputs }}`/`{{ outputs }}`/`{{ expectations }}` (the UI requires at least
   one). `FaithfulnessJudge` is the real `custom_judge` from `verification.yaml`.
3. **Run baseline + candidate (step 3).** `Evaluate → Evaluations → Run
   evaluation` on the dataset with scorer `Judge.FaithfulnessJudge` — once on the
   baseline artifact, once on the candidate (e.g. before/after a prompt change).
   Capture both run ids; in the candidate scorecard, select the baseline to see
   per-example deltas. Flag the rows the judge scores false.
4. **Enqueue sampled trace ids to the review queue (step 4).** Sample the flagged
   traces plus a few passes and `POST /review-queues/{id}/items {trace_ids:[...]}`.
   Reviewers answer the `faithful` question; answers **write back onto each
   trace** (real, trace-linked).
5. **Answer, then MANUALLY tally alignment (step 5).** Open
   [`alignment/alignment-worksheet.md`](alignment/alignment-worksheet.md). For each
   sampled trace, put the judge verdict next to the reviewer's `faithful` answer,
   mark agree/disagree, and compute `alignment = agreements / total`. Add each
   disagreement back to the dataset and reconcile the rubric, then re-run step 3.

## Two facts to state up front

- **Alignment / disagreement rate is MANUAL.** CALIBER does not auto-compute an
  alignment or disagreement metric (FEASIBILITY §1, Evaluations). You tally the
  judge scorecard against the reviewer answers by hand on the worksheet — the
  gate numbers (`alignment ≥ 0.80` over `≥ 3` traces, `overall_eval_score ≥ 0.85`)
  come from that hand tally, not from a built-in readout.
- **The Test Sets detail page is wired.** `/eval-datasets/:id`
  (`EvalDatasetDetail`) is a full row editor — author/edit rows with **+ Add
  example** (revise / supersede / from-trace), or use the API
  (`POST /eval-datasets/{id}/examples`, `.../examples/from-trace`).

The dataset is built so the judge and the reviewer **deliberately disagree** on
the two `expected_disagreement` partial-evidence rows (`J03`, `J07`), so the
manual tally has something real to surface; see the worked example in the
worksheet.

## Conventions used across the pack

- **Dataset files** (`dataset/*.jsonl`): one example per line. Here they use the
  **harvested** shape `{"id", "tags", "inputs": {...}, "outputs": {...},
  "expectations": {...}}` — the same shape `.../examples/from-trace` produces, so
  the judge can read `{{ inputs }}`/`{{ outputs }}`/`{{ expectations }}` per row.
  The extra `human_label` (+ `human_label_reason`) field is **not** a CALIBER
  field — it is the intended ground-truth pass/fail this pack carries so you can
  drive the manual alignment tally; ignore it on import.
- **Judge files** (`judges/*.judge.json`): `{name, model, instructions,
  feedback_value_type}`; instructions reference `{{ inputs }}`/`{{ outputs }}`/
  `{{ expectations }}`. `feedback_value_type` ∈ bool|int|float|str (bool here →
  pass/fail).
- **Review files** (`review/*.json`): the questions you configure on the queue —
  an array of `{key, prompt, type, required}` with `type` ∈ bool|int|float|text.
  Keep the gating `faithful` question worded to match the judge criterion.
- **Skill files** (`skills/*.md`): YAML frontmatter (name, summary, optional
  category/tags/render_variables) then the literal SKILL body; variables are
  `{{ snake_case }}`. Kebab-case name; `claude*`/`anthropic*` prefixes rejected.
