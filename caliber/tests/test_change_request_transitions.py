"""Tests for the Change Request state-machine fixture (`P0-B`, Phase 0 item 10).

`workspace_change_requests.py` transcribes docs/workspace-plan.md section
3.2's Mermaid state diagram as data. These tests assert that transcription
is internally consistent -- they do not (and cannot) test any real Change
Request behavior, because none exists yet; that is Phase 4's job.
"""

from __future__ import annotations

from caliber.workspace_change_requests import (
    INITIAL_STATE,
    STATES,
    TERMINAL_STATES,
    TRANSITIONS,
    reachable_from,
)


def test_every_transition_references_a_declared_state() -> None:
    for transition in TRANSITIONS:
        assert transition.source in STATES, f"undeclared source state: {transition.source!r}"
        assert transition.target in STATES, f"undeclared target state: {transition.target!r}"


def test_every_state_is_reachable_from_draft() -> None:
    unreachable = STATES - reachable_from(INITIAL_STATE)
    assert not unreachable, f"unreachable from {INITIAL_STATE!r}: {sorted(unreachable)}"


def test_terminal_states_have_no_outgoing_transition() -> None:
    """`accepted --> [*]` and `closed --> [*]` in the diagram -- neither has
    an outgoing arrow."""
    sources = {transition.source for transition in TRANSITIONS}
    for state in TERMINAL_STATES:
        assert state not in sources, f"{state!r} is terminal but has an outgoing transition"


def test_every_non_terminal_state_has_at_least_one_outgoing_transition() -> None:
    """A non-terminal state with no way out would be a dead end the diagram
    never intended -- every real state in section 3.2's diagram has one."""
    sources = {transition.source for transition in TRANSITIONS}
    stuck = STATES - TERMINAL_STATES - sources
    assert not stuck, f"non-terminal state(s) with no outgoing transition: {sorted(stuck)}"


def test_transition_count_matches_the_diagram() -> None:
    """Ratchet: section 3.2's diagram has exactly 18 arrows. A change here
    must be a conscious update to match a real diagram edit, not a typo."""
    assert len(TRANSITIONS) == 18


def test_accepted_is_reachable_only_through_qa_in_progress() -> None:
    """`accepted` guarantees the package passed change review and QA
    (section 3.2's own text) -- the diagram has exactly one incoming edge
    into it, and it originates from `qa_in_progress`."""
    incoming = [t for t in TRANSITIONS if t.target == "accepted"]
    assert len(incoming) == 1
    assert incoming[0].source == "qa_in_progress"


def test_out_of_date_can_only_return_to_open() -> None:
    """`out_of_date --> open: new base and head generation` is the only way
    out -- a stale-base Change Request must re-enter review, not silently
    become technically approved or QA-eligible again."""
    outgoing = [t for t in TRANSITIONS if t.source == "out_of_date"]
    targets = {t.target for t in outgoing}
    assert targets == {"open", "closed"}
