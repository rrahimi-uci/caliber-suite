---
name: customer-safe-refund-language
summary: Guardrails for phrasing a refund decision to a customer safely — respectful tone, no new promises, never override the decision.
---

# Customer-Safe Refund Language

Apply this skill when turning a refund `decision` into customer-facing wording.
It governs TONE and SAFETY only; the `decision` and `reason_code` are inputs
you must preserve exactly.

## Allowed phrasing by decision

- **approve** — Confirm warmly that the refund is being processed. You may say
  it has been approved. Do not invent the exact arrival date or amount unless
  it was explicitly provided.
- **deny** — State plainly and kindly that the refund cannot be processed, and
  give the canonical reason. Do not imply it might still happen, and do not
  invite an appeal that policy does not offer.
- **manual_review** — Say the request is being reviewed and a team member will
  follow up. Do NOT predict the outcome (no "you'll likely get it"), no ETA.

## Never do

- Never change, hedge, or reverse the `decision`.
- Never promise amounts, dates, timelines, credits, or exceptions that are not
  already implied by `decision` + `reason_code`.
- Never blame the customer or disclose internal risk/fraud signals; for
  risk-driven reviews say only that "additional review" is required.
- Never output JSON or internal codes to the customer — use plain language.

## Style

Address the customer by name once, keep it to 2–4 sentences, be courteous and
concrete. When in doubt, say less rather than promise more.
