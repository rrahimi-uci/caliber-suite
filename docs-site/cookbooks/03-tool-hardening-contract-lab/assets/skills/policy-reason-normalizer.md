---
name: policy-reason-normalizer
summary: Map a machine refund reason_code to one stable, human-readable reason phrase — without changing the decision.
---

# Policy Reason Normalizer

Use this skill whenever a refund decision carries a machine `reason_code` that
needs to be stated to a person (customer message, review note, audit log). It
normalizes the code to ONE canonical sentence. It never alters the `decision`
and never adds commitments.

## Canonical mapping

| reason_code | Canonical reason phrase |
| --- | --- |
| `ELIGIBLE_AUTO_APPROVE` | The order is within the refund window with no risk flags, so the refund was approved automatically. |
| `AMOUNT_OVER_THRESHOLD` | The refund amount is above the automatic-approval limit, so it was sent for human review. |
| `RISK_FLAG_PRESENT` | A risk or fraud signal is associated with the account, so the refund was sent for human review. |
| `MISSING_RISK_DATA` | Required risk information was unavailable, so the refund was sent for human review to stay safe. |
| `UNKNOWN_ORDER_STATE` | The order's status could not be confirmed, so the refund was sent for human review. |
| `NOT_REFUNDABLE_STATE` | The order is not in a state that can be refunded, so the refund was declined. |
| `OUTSIDE_WINDOW` | The order is past the refund window, so the refund was declined. |
| `INVALID_AMOUNT` | There is no positive amount to refund, so the refund was declined. |

## Rules

- Output exactly one phrase from the table for the given `reason_code`.
- If the `reason_code` is unrecognized, output: "This decision was made by
  policy; please consult the review queue for details." — do not guess.
- Do not state or imply an outcome that conflicts with the `decision`
  (`approve` / `deny` / `manual_review`).
- Add no amounts, dates, or timelines. The phrase is a reason, not a promise.
