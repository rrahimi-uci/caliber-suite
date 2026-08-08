"""The per-family capability declarations must not be able to contradict themselves.

``artifact_capabilities`` is the executable counterpart to the paper's family
table. Its value depends entirely on the declarations being true, and nothing
about a dict literal makes them true -- a family could claim ``promotable`` with
no live target to promote, or ``rollbackable`` with no mechanism, and the row
would render exactly like a correct one.

These tests pin the invariants that make it a contract. Each one is written
against a deliberately corrupted copy of the registry rather than against the
shipped one, so it fails when the *check* is removed rather than only when the
data is wrong.
"""

from __future__ import annotations

import pytest

from caliber.artifact_capabilities import (
    ARTIFACT_FAMILY_CAPABILITIES,
    CAPABILITY_FIELDS,
    KINDS,
    ROLLBACK_MECHANISMS,
    CapabilityContractError,
    _verify,
)


def _corrupt(family: str, **patch: object) -> dict[str, dict[str, object]]:
    probe = {name: dict(contract) for name, contract in ARTIFACT_FAMILY_CAPABILITIES.items()}
    probe[family].update(patch)
    return probe


def test_the_shipped_registry_satisfies_its_own_contract() -> None:
    _verify(ARTIFACT_FAMILY_CAPABILITIES)


def test_every_family_declares_every_field() -> None:
    for family, contract in ARTIFACT_FAMILY_CAPABILITIES.items():
        assert set(contract) == set(CAPABILITY_FIELDS), family
        assert contract["kind"] in KINDS, family
        assert contract["rollback"] in ROLLBACK_MECHANISMS, family


def test_a_partial_declaration_is_refused() -> None:
    probe = {name: dict(c) for name, c in ARTIFACT_FAMILY_CAPABILITIES.items()}
    del probe["prompt"]["rollback"]

    with pytest.raises(CapabilityContractError, match="does not declare"):
        _verify(probe)


def test_a_rollback_boolean_cannot_disagree_with_its_mechanism() -> None:
    """Both directions, because both are reachable by an ordinary edit.

    Adding a family by copying a neighbouring row and flipping one field is how
    this happens, and the resulting row looks entirely plausible.
    """
    with pytest.raises(CapabilityContractError, match="contradicts"):
        _verify(_corrupt("tool", rollbackable=True))

    with pytest.raises(CapabilityContractError, match="contradicts"):
        _verify(_corrupt("prompt", rollback="none"))


def test_a_family_cannot_be_promotable_without_a_live_target() -> None:
    with pytest.raises(CapabilityContractError, match="nothing to promote"):
        _verify(_corrupt("tool", promotable=True))


def test_an_unknown_rollback_mechanism_is_refused() -> None:
    with pytest.raises(CapabilityContractError, match="is not one of"):
        _verify(_corrupt("prompt", rollback="restore_somehow"))


def test_evidence_and_scoring_assets_cannot_be_deployed() -> None:
    """A test set *is* the evidence; giving it a release path produces a field nothing reads."""
    with pytest.raises(CapabilityContractError, match="cannot be promotable"):
        _verify(_corrupt("judge", rollback="alias_restore", rollbackable=True))

    with pytest.raises(CapabilityContractError, match="cannot be promotable"):
        _verify(_corrupt("test_set", live_target="alias", promotable=True))


def test_the_nine_families_are_the_ones_the_paper_documents() -> None:
    """A tenth family is a real architectural change, not an incidental edit.

    Pinned literally on purpose: this set is what the paper's guarantee table,
    the capability endpoint, and the SPA all enumerate, so adding to it should
    require touching this list deliberately.
    """
    assert set(ARTIFACT_FAMILY_CAPABILITIES) == {
        "prompt",
        "workflow",
        "knowledge_base",
        "skill",
        "tool",
        "test_set",
        "mcp_server",
        "judge",
        "agent",
    }
