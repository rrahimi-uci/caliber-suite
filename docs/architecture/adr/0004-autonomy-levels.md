# ADR 0004 — Autonomy levels: which roles an agent may hold, per use case

## Status

Accepted, 2026-10-26 (proposed 2026-10-25). Accepted with B01 (#565), which landed the ADR index and 0001–0002;
0003 (#566) follows in D1 and this ADR builds on it. Enforced by package M27 (D10); the vocabulary applies from
D2 so earlier packages use it.

## Context

CALIBER has two role layers, both closed vocabularies in code:

- **Platform scopes** (`auth.py`): `caliber.viewer`, `caliber.operator`, `caliber.approver`, `caliber.admin`.
- **Project roles** (`resource_access.py`): `owner`, `editor`, `reviewer`, `viewer`, each mapped to a fixed set of
  project actions.

Who holds a role is a person today. Aria already lets a service identity hold the approver role for one surface
(`AssistantApprovalMode`: `manual`, `auto_safe`, `auto_all`, `agent_review`, `full_autonomy`, with
`assistant_reviewer_user` required to be a distinct approver-scoped identity). Nothing else in the platform says
when an agent may hold a role, and nothing records at decision time whether a role-holder was a person or an
agent.

The book treats autonomy as a property of a use case, not of the platform: the GovernanceRecord carries an
`autonomy` field (Chapter 26), the risk owner re-scores the tier whenever autonomy changes, and "evidence before
autonomy" is a stated principle (Chapter 1). Chapter 13 forbids self-approval; Chapter 14 puts people where their
judgment matters.

The owner asked for an autonomy ladder modelled on driving automation: define the roles first, then replace human
role-holders with agents level by level.

## Decision

**Autonomy is the set of roles an agent principal may hold for one use case, bounded by effect class and risk
tier.** It is not a global switch. The permission model does not change with the level; the kind of principal in
each chair does.

### Principals

Every account has a `principal_type`: `human` or `agent`. An agent principal is a service identity with the
behaviour pins B02/B08 define (model route, prompt or skill digest, adapter version). Evidence records name the
principal and, for an agent, its pins, for every role decision.

### Levels

One level per use case (the GovernanceRecord's `autonomy` field, M24), chosen from this closed vocabulary. The
table says who may hold each role in a decision chain (propose → review → approve → apply → verify).

| Level | `editor` | `reviewer` | `approver` | `owner` |
| --- | --- | --- | --- | --- |
| `L0_manual` | human | human | human | human |
| `L1_assisted` | human; agents may produce drafts and candidates the human submits | human | human | human |
| `L2_agent_editor` | agent | human | human | human |
| `L3_agent_review` | agent | agent | human, within the approval SLA (M06 timers and escalation) | human |
| `L4_bounded_agent_approval` | agent | agent | agent for effect classes `read` and `reversible_write` at or below the tier's ceiling; human for `irreversible_write` and `export` | human |
| `L5_governed_autonomy` | agent | agent | agent for every effect class the use case's risk tier allows | human |

The `owner` role is human at every level. It sets policy, grants waivers (M24), executes the scoped stop (M06) and
audits evidence. "Fully autonomous" means the humans moved from the decision loop into governance, not out.

### Invariants, enforced at the approval boundary (B06) and at dispatch (B04)

1. **Distinct principals.** The same principal never fills two roles in one decision chain. The existing
   `assert_distinct_actor` check (B06, #523) applies to agent principals as it does to humans.
2. **Independence for agent reviewers and approvers.** An agent reviewer or approver must differ from the agent
   editor in at least one behaviour pin (model route, or prompt/skill digest), and the chain must contain at least
   one deterministic check that no model output can satisfy by text alone: the acceptance predicate (B09) and,
   for releases, the trajectory gate (M09).
3. **Effect-class ceiling.** An agent approver is refused above the level's ceiling with reason
   `autonomy_level_exceeded`; the request falls back to a human approver, it does not fail.
4. **Risk tier bounds the level.** M24's risk tier declares the maximum level a use case may hold; a tier change
   that lowers the maximum demotes the use case immediately.
5. **Promotion is a governed release.** Raising a level needs: a measured period at the current level with no
   tripped demotion metric, a passing trajectory gate (M09) over that period's decisions, and a human owner
   decision. It is recorded like any release (M02): digest, approvers, rollback.
6. **Demotion is automatic and asymmetric.** A tripped metric lowers the level one step at once and records the
   evidence; humans promote, the system demotes. Initial metrics: acceptance failures after agent approval,
   rollbacks of agent-approved applies, incidents on the use case, and trajectory-gate failures. Thresholds live
   on the GovernanceRecord.
7. **Every decision shows its principals.** The evidence package (B14) and the UI name, for each role in the
   chain, the principal, its type and (for agents) its pins.

### What this is not

- Not a change to what any role may do. `PROJECT_ACTIONS` and the scope implications stay as they are.
- Not a global setting. Two use cases in one project may sit at different levels.
- Not a replacement for Aria's `AssistantApprovalMode`: that mode maps onto these levels (`manual`,
  `auto_safe`, `auto_all` are L1 behaviours; `agent_review` is L3; `full_autonomy` is L4/L5 within the use case's
  ceiling) and M27 makes it read the use case's level instead of its own setting.

## Consequences

- B03's agent identity and grants (D2) are a prerequisite; until M27 (D10) the platform behaves as L1 everywhere,
  with Aria's own mode as the only exception, and that is what the Beta documents.
- M24's GovernanceRecord gains `autonomy_level`, `max_autonomy_level` (from the tier) and the demotion thresholds;
  no second store.
- Agent-held roles cost model calls on every review and approval; budgets (M08) apply to them.
- Independence between agents is weaker than between people. The deterministic checks in invariant 2 and the
  demotion metrics in invariant 6 are what make L3–L5 defensible; a use case that cannot express an acceptance
  predicate cannot go above L2.
- Validation scenario V35 (gap analysis §13) exercises invariants 1, 3, 5 and 6.
