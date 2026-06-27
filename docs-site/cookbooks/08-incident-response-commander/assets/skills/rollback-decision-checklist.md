---
name: rollback-decision-checklist
summary: Decide whether a rollback is justified and encode when it requires approval; rollback is a state-changing action and ALWAYS requires approval before it runs.
---

# Rollback Decision Checklist

Use this skill before recommending a **rollback**. Rollback reverts production
to a prior deploy — it is a high-impact, state-changing action. This checklist
decides (a) whether rollback is the right action and (b) the `requires_approval`
flag that gates it.

## Rollback is justified ONLY when all hold

1. **A recent deploy exists** for this service/environment in
   `{{ deployments }}` (newest first), and
2. **Its timing precedes the alert** — the deploy shipped shortly before the
   symptoms started, and
3. **Health is degraded (or worse)** — `status: "degraded"`/down, or a clearly
   abnormal `error_rate`/`latency_p99_ms`, and
4. **The deploy plausibly explains the symptom** — bonus confidence when
   `risk: "high"` and the `change_summary` touches the failing area.

If any of these is missing, rollback is NOT justified yet:

- No recent deploy → prefer `investigate` (or `scale` if it is a pure capacity
  problem) instead of rolling back something that did not change.
- Health `status: "unknown"` / null metrics → prefer `gather_more_evidence`;
  you cannot confirm a regression you cannot measure.
- Healthy with only a minor blip → prefer `monitor`.

## Approval rule (the part that must always hold)

- **Rollback ALWAYS requires approval.** Whenever the recommended action is
  `rollback`, set `requires_approval: true`. There is no "auto-rollback" path in
  this workflow — a human_approval gate must clear first.
- **Any external write requires approval too** — filing/closing an incident
  issue, or restarting/scaling production capacity that changes live state →
  `requires_approval: true`.
- **Read-only / observe-only actions do not** — `monitor`, `investigate`, and
  `gather_more_evidence` are `requires_approval: false`.
- When uncertain whether an action changes production state, treat it as a write
  and require approval.

## Output

State the rollback verdict, the specific deploy sha you would revert (from
`{{ deployments }}`), the evidence tying it to the symptom, and
`requires_approval: true`. Never describe the rollback as already done — it is a
recommendation pending the approval gate.
