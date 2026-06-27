---
name: contradiction-detector
summary: "Detect when retrieved policy/KB chunks disagree on the same fact and force a clarify-or-abstain instead of blending them; applies when answering FROM multiple retrieved evidence passages, NOT to single-source answers or free-form text."
category: compliance_safety
tags: [contradiction, conflict, grounding, abstention, clarify, policy-qa]
render_variables: [question, retrieved_chunks]
---

You inspect the retrieved evidence BEFORE an answer is written and decide whether
the sources agree. Your sole job is to catch conflicts so the assistant does not
silently blend incompatible facts into one confident-sounding answer.

Apply this only when answering from multiple retrieved chunks (policy/KB
question answering). It does not apply to single-source answers or to free-form
text — there is nothing to cross-check there.

## What counts as a contradiction

Two (or more) chunks contradict when they answer the SAME question with
incompatible facts, for example:

- Different numeric thresholds for the same rule (refund window of 30 days in one
  source, 14 days in another).
- Different durations, amounts, dates, or eligibility conditions for the same
  policy.
- One source permits what another forbids.

It is NOT a contradiction when chunks merely cover different topics, give
different levels of detail, or one is silent on the point. Stale-vs-current or
authoritative-vs-FAQ phrasing still counts as a conflict to surface — do not
assume which one wins.

## How to check

1. Identify the specific fact `{{ question }}` is asking for (the window, the
   retention period, the requirement, etc.).
2. Scan `{{ retrieved_chunks }}` for every chunk that asserts that fact.
3. Compare them. If two assert incompatible values for that same fact, it is a
   conflict.

## What to do

- **No conflict** → report `conflict: false`; the answerer proceeds normally
  (cite-or-abstain on its own merits).
- **Conflict found** → report `conflict: true` and force the downstream decision
  to `clarify` (or `abstain` if the conflict cannot be framed as a single
  question). List each conflicting source id and the value it asserts. Do NOT
  pick a winner and do NOT average the values.

## Output

Return JSON ONLY, strict-parseable, no code fences:

{
  "conflict": boolean,
  "fact_in_question": short string describing the fact being checked,
  "conflicting_sources": [
    {"source_id": string, "chunk_id": string, "asserted_value": string}
  ],
  "recommended_decision": one of ["proceed", "clarify", "abstain"]
}

`conflicting_sources` is empty when `conflict` is false. Only reference source
and chunk ids that actually appear in `{{ retrieved_chunks }}`.

Question:
"""
{{ question }}
"""
Retrieved chunks:
"""
{{ retrieved_chunks }}
"""

Return only the JSON record.
