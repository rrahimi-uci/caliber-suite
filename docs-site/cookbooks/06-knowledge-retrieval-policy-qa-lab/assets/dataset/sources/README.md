# Policy corpus — SCN-06 Object Store seed

A small, self-consistent set of internal policy docs to ingest into a Knowledge
Base for the grounded-QA demo. They are plain Markdown so the KB build,
retrieval-mode comparison, and inline Calibrate all have something real to
chew on. Every file carries a stable `Source id:` in its header so answers and
the faithfulness judge can cite a concrete source.

| File | Source id | Policy domain | Covers |
| --- | --- | --- | --- |
| `refund-policy.md` | `REFUND-POLICY` | billing | refund eligibility, **window (30 days)**, how-to, proration |
| `refund-faq.md` | `REFUND-FAQ` | billing | customer FAQ; **window (14 days)** — the contradiction |
| `security-policy.md` | `SECURITY-POLICY` | security | MFA/SSO, encryption, access control, incident response |
| `data-retention-policy.md` | `DATA-RETENTION-POLICY` | security | audit logs (365 days), customer content, backups, legal holds |

## The deliberate contradiction (this is on purpose)

`refund-policy.md` and `refund-faq.md` **disagree on the refund window**:

- `REFUND-POLICY` (authoritative policy): a refund request must be submitted
  **within 30 days** of the charge.
- `REFUND-FAQ` (customer-facing FAQ): you can request a refund **within 14 days**.

This is the conflict pair that drives the **clarify / abstain** path. A grounded
assistant that retrieves both should *not* silently pick one; it should surface
the conflict and ask for clarification (see `kb-answer.md`, the
`contradiction-detector` skill, and the `clarify` rows in `qa-eval.jsonl`). Keep
**both** files in the corpus — deleting either removes the demo.

Everything else in the corpus is internally consistent and answerable, so the
same KB can demonstrate clean cited answers *and* the conflict behavior.

## Upload instructions

Ingest the corpus into the Object Store, then point the KB build at it.

1. `Object Store → Upload`. Create (or pick) a bucket, e.g.
   `policy-corpus`, and upload all four `*.md` files (skip this `README.md`).
   - API: `PUT` each object via the Object Store upload endpoint, or use the
     bucket UI's drag-and-drop.
2. (Optional) Confirm with `Object Store → <bucket>` that the four documents are
   listed and previewable.
3. In `Knowledge → Knowledge Base → New knowledge base`, choose this bucket as
   the source and select the four policy docs (see `../../README.md` step "Build
   a KB version").

> Reuse note: per the recipe you may instead reuse an SCN-04 corpus. These docs
> exist so SCN-06 is self-contained and the 30-vs-14-day conflict is guaranteed.
