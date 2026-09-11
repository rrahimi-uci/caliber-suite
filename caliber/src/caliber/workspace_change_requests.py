"""Change Request state-machine contract (`P0-B`, Phase 0 item 10).

Section 3.2 of ``docs/workspace-plan.md`` ("The PR-like application
lifecycle") already contains a fully-written Change Request design: base/head
package identity, head-generation invalidation, reviewer eligibility,
stale-base compare-and-set, SemVer reservation, and a Mermaid state diagram.
None of it is implemented today -- there is no ``Workspace`` model, no
``caliber_workspace_change_requests`` table, no route. Real implementation is
Phase 4 (items 12-17 of the phased plan), not Phase 0.

Phase 0 item 10 asks to "freeze... contracts with model-based transition
fixtures." This module is that fixture: the state diagram transcribed
verbatim as data, not as a database model or a route. It exists so:

* this repo's own tests can already assert the frozen design is internally
  consistent (every state reachable, no transition to an undeclared state,
  the two states the diagram sends to ``[*]`` really are terminal) --
  before any of Phase 4's real code exists to check it against;
* Phase 4's eventual real state machine has one source of truth to build
  against and diff its own transition table against, rather than a second,
  hand-copied version of the same diagram drifting from this one.

Do not add enforcement, persistence, or route wiring here. That is Phase 4's
job. This module is intentionally inert data.
"""

from __future__ import annotations

from dataclasses import dataclass

#: Every Change Request state, exactly as section 3.2's Mermaid diagram
#: (docs/workspace-plan.md, "Change Request state machine") names them.
STATES: frozenset[str] = frozenset(
    {
        "draft",
        "open",
        "changes_requested",
        "technically_approved",
        "qa_in_progress",
        "out_of_date",
        "accepted",
        "closed",
    }
)

#: The one state the diagram reaches directly from `[*]` (Mermaid's start
#: pseudostate): `[*] --> draft`.
INITIAL_STATE = "draft"

#: States with no outgoing transition in the diagram -- `accepted --> [*]`
#: and `closed --> [*]`. `accepted` means the package passed change review
#: and QA and is eligible for staging; it does not mean deployed. A
#: production release still needs its own environment-specific evidence and
#: Admin final approval (section 3.2's own text, not modeled here -- that is
#: the release/operation state split, section 16 item 8's separate scope).
TERMINAL_STATES: frozenset[str] = frozenset({"accepted", "closed"})


@dataclass(frozen=True)
class Transition:
    source: str
    target: str
    #: The diagram's own edge label, verbatim. Empty string for the five
    #: unlabeled `X --> closed` edges -- the diagram genuinely doesn't name
    #: a trigger for those, so this stays empty rather than inventing one.
    trigger: str


#: Every transition arrow in section 3.2's Mermaid diagram, verbatim, in the
#: diagram's own order. 18 edges; `test_change_request_transitions.py` pins
#: this count as a ratchet.
TRANSITIONS: tuple[Transition, ...] = (
    Transition("draft", "open", "submit"),
    Transition("open", "changes_requested", "Reviewer or QA no_go"),
    Transition("changes_requested", "open", "append new head generation"),
    Transition("open", "technically_approved", "selected backend proves checks and review"),
    Transition(
        "technically_approved", "changes_requested", "Reviewer withdraws via request_changes"
    ),
    Transition("technically_approved", "open", "satisfying Reviewer assignment removed"),
    Transition("technically_approved", "qa_in_progress", "exact head applied to qa"),
    Transition("qa_in_progress", "changes_requested", "machine gate or QA no_go"),
    Transition("qa_in_progress", "accepted", "machine gate passes and QA go"),
    Transition("qa_in_progress", "out_of_date", "QA go but base CAS is stale"),
    Transition("open", "out_of_date", "accepted base moved"),
    Transition("technically_approved", "out_of_date", "accepted base moved"),
    Transition("out_of_date", "open", "new base and head generation"),
    Transition("draft", "closed", ""),
    Transition("open", "closed", ""),
    Transition("changes_requested", "closed", ""),
    Transition("technically_approved", "closed", ""),
    Transition("out_of_date", "closed", ""),
)


def reachable_from(state: str) -> frozenset[str]:
    """Every state reachable from `state` (inclusive), following
    `TRANSITIONS` forward via breadth-first search."""
    seen = {state}
    frontier = [state]
    while frontier:
        current = frontier.pop()
        for transition in TRANSITIONS:
            if transition.source == current and transition.target not in seen:
                seen.add(transition.target)
                frontier.append(transition.target)
    return frozenset(seen)


__all__ = [
    "INITIAL_STATE",
    "STATES",
    "TERMINAL_STATES",
    "TRANSITIONS",
    "Transition",
    "reachable_from",
]
