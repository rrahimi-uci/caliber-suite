---
name: stakeholder-update-drafting
summary: Draft a calm, honest stakeholder incident update that states what is known vs under investigation, with no internal jargon, no blame, and no promises beyond the evidence.
---

# Stakeholder Update Drafting

Use this skill to write the short `stakeholder_update` that accompanies an
incident recommendation. The update is read by non-engineers (support leads,
on-call managers, sometimes customers). It must be accurate to the evidence and
safe to forward — it is NOT the place to speculate about root cause.

## What a good update contains

1. **Impact in plain language** — what users are experiencing, scoped to what
   the evidence confirms ("checkout is failing for some users", not "the DB is
   down" unless that is a known_fact).
2. **Current status** — "under investigation", "mitigation in progress", or
   "monitoring". Use "under investigation" whenever root cause is still a
   hypothesis.
3. **What is being done** — the recommended next step in lay terms (e.g.
   "preparing a rollback of the recent change, pending approval") — note it is
   pending approval when it is.
4. **Next update** — a cadence, not a resolution time ("next update in 30
   minutes"). Never promise a fix time.

## Rules

- **Separate known from unknown.** State confirmed impact as fact; frame causes
  as "we are investigating whether ...". Never present a hypothesis as the cause.
- **No internal jargon.** No service/component codenames, deploy shas, queue or
  ticket ids, dashboards, team names, or model/provider names.
- **No blame.** Do not name a person, team, or "the bad deploy". Describe the
  change neutrally ("a recent update").
- **No promises beyond the evidence.** No fix ETAs, no "this is resolved", no
  compensation commitments. If an action needs approval, say it is "pending
  approval", not "being rolled back now".
- **Calm and brief.** 1–3 sentences, <= 280 characters. Acknowledge impact, give
  status + next step + next-update cadence.

## Output

Produce only the update text (no headers, no JSON). Example shape:

> We're seeing elevated errors affecting some checkout requests and are actively
> investigating. A mitigation is being prepared and is pending approval. Next
> update in 30 minutes.
