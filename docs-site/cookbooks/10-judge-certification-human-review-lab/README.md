# Trustworthy Evaluation

## Demo objective

A governance lane where automated judge outcomes are compared against human
review before release decisions, so rubric drift and over-trusted scores are
caught.

## Feasibility & substitutions

Read [`../FEASIBILITY.md`](../FEASIBILITY.md). Key points:

- ✅ Create a custom LLM **judge** (name, model, `instructions` referencing
  `{{ inputs }}`/`{{ outputs }}`/`{{ expectations }}`, `feedback_value_type`),
  run **Evaluations** (candidate vs baseline), enqueue traces to a **Review
  Queue**, and write reviewer answers back onto the trace — all real and
  trace-linked.
- ✅ **Alignment is computed in-product** — the Judges page **Human alignment**
  mode returns agreement rate, Cohen's κ, and FP/FN from the judge outputs + the
  reviewer pass/fail labels. ⚠️ You still transcribe the labels: it does **not**
  auto-ingest completed Review Queue items, so `disagreement_rate`/`alignment_score`
  come from labels you enter by hand (FEASIBILITY §1, Judges → Human alignment).
- ✅ The **Test Sets detail page** (`/eval-datasets/:id`) is wired
  (`EvalDatasetDetail`) — author/edit dataset rows with **+ Add example**
  (revise / supersede / from-trace), or use the API.
- `FaithfulnessJudge` (`custom_judge`) is the real judge type.

## Prerequisites & seed

- Candidate artifact runs from build scenarios (SCN-07/08) and reviewers
  available. A configured provider for the judge.

## Recipe (UI-first, with API fallbacks)

1. **Prepare the dataset.** Aggregate representative outputs from SCN-07/08 into
   a dataset. Easiest: **harvest from traces** —
   `POST /eval-datasets/{id}/examples/from-trace {trace_id}` for each run you
   want graded (auto-extracts input + expected). Or `POST /eval-datasets` then
   add rows manually.
2. **Define the judge.** `Evaluate → Judges → New judge` `FaithfulnessJudge`:
   model + instructions = *"Given `{{ inputs }}`, the model `{{ outputs }}`, and
   `{{ expectations }}`, return true only if every claim in outputs is supported
   by the expectations/evidence."* `feedback_value_type = bool`. (Confirm the
   instructions reference at least one `{{ var }}` — the UI enforces this.)
3. **Run baseline + candidate.** `Evaluate → Evaluations → Run evaluation` on the
   same dataset twice: once on the baseline artifact, once on the candidate
   (e.g. before/after a prompt change). Capture both run ids.
4. **Inspect examples.** In each scorecard, open per-example results; in the
   candidate, select the baseline to compute deltas. Flag the low/false examples.
5. **Human review.** `Observe → Review Queues → New queue` with the same
   faithfulness question(s). Enqueue the **trace ids** of the flagged examples
   (`POST /review-queues/{id}/items {trace_ids}`); have reviewers answer. Answers
   write back to each trace.
6. **Compute alignment.** `Evaluate → Judges → FaithfulnessJudge → Human
   alignment`: enter each sampled trace's judge output + the reviewer's pass/fail
   label; CALIBER returns agreement rate, Cohen's κ, and FP/FN (`POST
   /judges/{id}/alignment`). You transcribe the labels by hand (no auto-pull from
   the Review Queue yet). Treat each disagreement as a calibration input — add the
   hard case to the dataset and/or reword the rubric, then re-run step 3.

## Demo evidence to capture

- Judge id + its template-variable instructions.
- Baseline and candidate evaluation run ids.
- A review queue with completed, trace-linked reviewer answers.
- The Judges Human-alignment result (agreement, Cohen's κ, FP/FN) over the
  sampled traces.

## Done when / gate

- Overall eval score ≥ `0.85`.
- Automated–human alignment ≥ `0.80` over ≥ `3` reviewed traces.
- Disagreements are documented and fed back into the dataset/rubric.
