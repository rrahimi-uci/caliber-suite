"""Unit tests for the frozen, provider-neutral Change Request contract."""

from __future__ import annotations

from dataclasses import FrozenInstanceError

import pytest

from caliber.workspace_change_requests import (
    INITIAL_STATE,
    STATES,
    TERMINAL_STATES,
    TRANSITIONS,
    Transition,
    reachable_from,
)


def test_frozen_contract_has_one_initial_state_and_eighteen_edges() -> None:
    assert INITIAL_STATE in STATES
    assert len(STATES) == 8
    assert len(TRANSITIONS) == 18
    assert all(edge.source in STATES and edge.target in STATES for edge in TRANSITIONS)


def test_initial_state_reaches_every_declared_state() -> None:
    assert reachable_from(INITIAL_STATE) == STATES


def test_terminal_states_have_no_outgoing_edges() -> None:
    for state in TERMINAL_STATES:
        assert not [edge for edge in TRANSITIONS if edge.source == state]


def test_every_nonterminal_state_has_an_outgoing_edge() -> None:
    nonterminal_states = STATES - TERMINAL_STATES
    assert {
        edge.source for edge in TRANSITIONS if edge.source in nonterminal_states
    } == nonterminal_states


def test_unconnected_starting_point_is_returned_without_inventing_edges() -> None:
    assert reachable_from("not-a-change-request-state") == frozenset({"not-a-change-request-state"})


def test_transition_is_value_comparable_and_immutable() -> None:
    assert Transition("draft", "open", "submit") == TRANSITIONS[0]
    with pytest.raises(FrozenInstanceError):
        TRANSITIONS[0].target = "closed"  # type: ignore[misc]


def test_terminal_edge_labels_are_explicitly_empty() -> None:
    closed_edges = [edge for edge in TRANSITIONS if edge.target == "closed"]
    assert len(closed_edges) == 5
    assert {edge.trigger for edge in closed_edges} == {""}
