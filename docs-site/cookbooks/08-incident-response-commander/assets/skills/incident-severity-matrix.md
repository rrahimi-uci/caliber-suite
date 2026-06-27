---
name: incident-severity-matrix
summary: Assign a consistent incident severity (sev1/sev2/sev3) from confirmed impact only; never inflate severity on unconfirmed signals.
---

# Incident Severity Matrix

Use this skill to assign a single, consistent severity to an incident. Severity
reflects **confirmed customer/business impact**, not how alarming the alert text
sounds. If impact is not yet confirmed by evidence, pick the lower tier and
record the gap as an open question — do not round up.

## Severity tiers

| Severity | Use when (confirmed by evidence) | Typical signals |
| --- | --- | --- |
| `sev1` | Broad customer-facing outage, data loss, or a security/integrity breach. Core flow unusable for many users. | `status: degraded`/down on a critical path, high error_rate across users, checkout/auth/data-write failing. |
| `sev2` | Significant degradation with partial impact or a workaround. Some users or one surface affected; service still mostly usable. | Elevated error_rate or latency on one service, saturation high but not failing, a risky deploy implicated. |
| `sev3` | Minor or localized blip, no broad impact. Within or just above normal noise. | Small latency blip, single transient error, `status: healthy` with a metric slightly elevated. |

## Rules

- Severity is driven by **confirmed impact**, not the loudest word in the alert.
  "SEV1!!" in `alert_text` does not make it sev1 if the evidence does not.
- Missing or `unknown` health metrics do NOT justify a high severity. Unconfirmed
  impact means you pick the lower plausible tier and raise an open question
  ("impact scope unconfirmed — metrics unavailable").
- A `risk: high` recent deploy plus `degraded` health on a critical service is at
  least `sev2`, and `sev1` if the failure is broad/customer-facing.
- When evidence is genuinely ambiguous between two tiers, choose the lower one
  and note why. You can always escalate later with more evidence; you cannot
  un-page people.
- Output only the severity token (`sev1` | `sev2` | `sev3`) and, if asked, a
  one-line justification that cites the specific signal you used.
