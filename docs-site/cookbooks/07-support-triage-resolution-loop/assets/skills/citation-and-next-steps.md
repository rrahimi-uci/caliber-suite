---
name: citation-and-next-steps
summary: "Format inline citations and propose concrete next steps for a grounded support reply, escalation, or clarification; applies to answering tickets FROM retrieved evidence (account state, incidents, KB), NOT to drafting free-form prose or summarizing untrusted text."
category: support
tags: [citation, grounding, next-steps, escalation, support-qa]
render_variables: [decision, customer_reply, citations]
---

You take a grounded support result and make it auditable and actionable. You do
NOT change the underlying decision or the facts — the decision (`reply`,
`clarify`, `escalate_support`, or `escalate_bug`) and the evidence arrive in
`{{ decision }}`, `{{ customer_reply }}`, and `{{ citations }}`. Your job is to
attach clean citations and a single clear next step.

Apply this only when a result is grounded in retrieved evidence (account state,
incidents, or KB chunks). It does not apply to free-form drafting,
brainstorming, or summarizing untrusted text — defer to the relevant skill there.

## Citation rules

- Cite by the source/chunk ids present in `{{ citations }}` (e.g. `[KB-REFUNDS]`,
  `[KB-SSO #chunk_2]`). Place the citation inline, right after the claim it
  supports.
- Cite ONLY ids that appear in `{{ citations }}`. Never invent a source id and
  never cite an id you were not given.
- One claim, one (or more) citation. If a sentence asserts two facts from two
  sources, cite both.
- If `{{ decision }}` is `reply` and a claim has no matching entry in
  `{{ citations }}`, do not state that claim — drop it rather than leave it
  uncited.

## Next-step rules

End with exactly one concrete next step, chosen by `{{ decision }}`:

- **reply** — Offer the most relevant self-serve follow-up the cited evidence
  enables (e.g. "Want the step-by-step for enabling SSO in your workspace
  settings?").
- **clarify** — Ask the single missing thing that lets you act (e.g. "Could you
  share the order or account id this charge is on? I'll pull it up right away.").
  Ask one thing; do not improvise an answer to fill the gap.
- **escalate_support** — Tell the customer plainly you are routing them to someone
  who can authorize it and set a realistic next-step expectation — without naming
  the internal team or queue, and without promising the outcome.
- **escalate_bug** — Acknowledge the issue, say it is being logged for the
  engineering team to investigate, and give a realistic expectation (a follow-up,
  not a fix date). Never expose internal component or ticket names.

## Output

Produce only the finished, citation-annotated reply text plus its single next
step. No headers, no internal notes, no JSON, no commentary about these rules.

Decision: {{ decision }}
Citations available (source/chunk ids you may cite):
"""
{{ citations }}
"""
Draft reply to annotate:
"""
{{ customer_reply }}
"""

Write the final cited reply with its next step now.
