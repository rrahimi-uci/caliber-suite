# Grounded Knowledge Assistant

## Demo objective

A retrieval-backed assistant that answers with citations, handles conflicting
sources, and routes uncertainty to a review queue.

## Feasibility & substitutions

Read [`../FEASIBILITY.md`](../FEASIBILITY.md). Key points:

- ✅ KB **Build / Explore / Calibrate / Use** are real. Explore sub-views are
  **Query**, **Chunks**, **Graph**. Retrieval modes: `dense`, `hybrid`,
  `graph_hybrid`, `age_graph` (AGE only when enabled, and only after a manual
  **Sync to AGE**).
- ✅ **Calibrate runs inline** and returns Recall@k, nDCG@k, Faithfulness,
  Answer-correctness — so "retrieval recall improvement after calibration" is
  directly demoable.
- ✅ The answer workflow uses a **`knowledge_query`** node + an `agent`/prompt +
  an approval/branch; route low-confidence runs to a **Review Queue** (real,
  trace-linked).
- `CitationFaithfulness` (`custom_judge`) is a real LLM judge selectable in Evaluations under **Custom LLM judges** (runs as a `Judge.<id>` scorer alongside the four deterministic scorers).

## Prerequisites & seed

- A small corpus in Object Store (reuse SCN-04 outputs or upload a few policy
  docs). A provider configured for the answer agent + the faithfulness judge.

## Recipe (UI-first, with API fallbacks)

1. **Build a KB version.** `Knowledge → Knowledge Base → New knowledge base`.
   Pick the source bucket, select the policy docs, choose an embedding model
   under **Advanced configuration**, **Create**. Wait for the build run to reach
   `completed`.
   - API: `POST /knowledge-bases`, `POST /knowledge-bases/{id}/versions`.
2. **Explore → Chunks.** Select the built version in the header switcher, open
   **Explore → Chunks**; confirm chunk quality + source lineage.
3. **Explore → Query (mode comparison).** In the Query view, run the same
   question across `dense`, then `hybrid`, then `graph_hybrid`; note which
   retrieves the right chunk. (If AGE is enabled: Explore → Graph → **Sync to
   AGE** → re-query as `age_graph`.)
   - API: `POST /knowledge/query {knowledge_base_id, question, retrieval_modes}`.
4. **Calibrate (inline metrics).** `Knowledge → Calibrate`: supply a few
   question→expected pairs, run; read Recall@k / nDCG@k / Faithfulness /
   Answer-correctness. Tune chunking/mode, re-run, and **show recall improve**.
   Pin a baseline (`POST /knowledge-bases/{id}/baseline`).
   - API: `POST /knowledge-bases/{id}/calibrate`.
5. **Build the answer workflow.** `Compose → Workflows → New`, template
   **`knowledge_rag`** (or `graph_hybrid_rag`). Nodes:
   `knowledge_query → data_transform (confidence) → agent (draft_answer with
   "cite only retrieved chunks; if sources conflict, present the conflict and
   abstain") → router (abstain_or_answer) → output`, with a branch that enqueues
   a review item when confidence is low.
6. **Run scenario queries.** Execute an answerable question (cited answer), a
   missing-evidence question (abstain + missing-evidence list), and a
   conflicting-sources question (abstain/clarify).
7. **Evaluate faithfulness.** Turn the scenario queries into a Test Set; run
   **Evaluations** with the deterministic `contains_expected` grader plus the
   `CitationFaithfulness` judge ticked under **Custom LLM judges** (it runs as a
   `Judge.<id>` scorer for an automatic per-row verdict).
8. **Route to review.** Add a **Review Queue Enqueue** workflow node on the
   low-confidence branch, or enqueue manually from Review Queues. Answer the
   trace-linked citation/abstention questions.
   Answers write back onto the trace.
   - API: `POST /review-queues`, `POST /review-queues/{id}/items {trace_ids}`.

## Demo evidence to capture

- KB version id + active version.
- Retrieval mode-comparison result (which mode found the chunk) + the
  pre/post-calibration recall delta.
- Run ids showing answer, abstain, and conflict behavior.
- Review queue with completed trace-linked answers.

## Done when / gate

- Substantive answers are citation-backed (`faithfulness_min ≥ 0.90`).
- Missing/conflicting evidence triggers abstention
  (`abstention_policy_compliance = 1.0`).
- Calibration improves recall without lowering faithfulness.
