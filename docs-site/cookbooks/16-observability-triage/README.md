# Cookbook 16 — Production Observability & Triage

**Track:** Operate · **Level:** Core · **Time:** 30–45 min

**Surfaces:** Observability · Review Queues · Test Sets · Evaluations

An operator playbook for a workflow that is **already running**. It closes the
loop between *something went wrong in production* and *durable, actionable
evidence* — entirely in the CALIBER UI, no code:

1. **Watch** — open **Observability**, filter **Status = Error**, and drill into a
   failing trace's node tree to find the root cause (the `trace_id` is in the URL).
2. **Capture** — turn each failure into a regression row with **Add to test set →
   Add example** (test set `prod-regression`). You can also author/edit rows in
   the dataset editor at `/eval-datasets/:id → + Add example`; here every row
   comes from a real trace.
3. **Triage** — stand up the **prod-triage** Review Queue
   ([`assets/review-queues/triage-review.review-questions.json`](assets/review-queues/triage-review.review-questions.json)),
   **Add traces to review** → **Enqueue** the failing trace ids, then answer the
   questions and **Submit review**. Answers write back onto each trace as MLflow
   assessments/expectations.
4. **Baseline** — (optional) **Run evaluation** on `prod-regression` with the
   deterministic graders to put a number on the failure rate; re-run after a fix
   ships to prove it dropped.

## Prerequisite

You need real runs to observe. Build and run **cookbook 07** (support-copilot) or
**cookbook 09** (recovery target) first, executing a handful of inputs — including
at least one that errors or is rejected — so Observability has OK and Error traces.

## Assets

| # | Asset | File | UI surface |
| --- | --- | --- | --- |
| 1 | Triage question schema | [`assets/review-queues/triage-review.review-questions.json`](assets/review-queues/triage-review.review-questions.json) | `Observe → Review Queues → + New Queue` (key / title / type / options / required / target) |
| 2 | Regression-row shape | [`assets/dataset/prod-regression.sample.jsonl`](assets/dataset/prod-regression.sample.jsonl) | `Observe → Observability → Add to test set → Add example` (rows are captured from traces, not pasted) |

## Quality gates

| Gate | Target |
| --- | --- |
| Error traces captured | every error trace in the window lands in the test set or the triage queue |
| Root cause explainable | each flagged trace's failure is readable from its node tree |
| Triage labeled + written back | queue items answered; assessments visible on the traces |
| Regression baseline recorded | `prod-regression` scored in Evaluations (re-run after a fix) |

## Notes

- Observability **Status** options are *All statuses / OK / Error / In progress*;
  selecting a trace puts its id in the URL (`?trace=…`) — copy ids from there for
  the queue.
- Review answers write back as MLflow **assessments / expectations** on the trace.
- The **Evaluations** page scores datasets with the deterministic graders
  (`exact_match`, `token_f1`, `contains_expected`, `non_empty`) and any active LLM
  judges ticked under **Custom LLM judges** (run as `Judge.<id>` scorers) — the
  optional baseline here just uses **Contains expected**.
- This is **operations**, not authoring: it converts live production signal into
  durable evidence (a regression test set + reviewed labels) you can act on.
