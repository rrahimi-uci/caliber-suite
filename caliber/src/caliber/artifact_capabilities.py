"""Machine-readable per-family guarantee surface.

The shared UI substrate does not imply shared lifecycle semantics. This registry
is the executable counterpart to the paper's family table: routes and clients can
disclose what a family supports without inferring guarantees from which component
happens to render it.

Two properties make it a control rather than documentation.

**The rollback mechanism is carried, not just a boolean.** Four families declare
``rollbackable`` and no two of them mean the same thing --- an alias restore, a
checkpoint-stack pop, a derivation from activation history, and a restore of a
prior snapshot *as a new version*. A client shown only the boolean would infer one
guarantee from four, which is the specific operator trap the shared version-history
panel sets. ``rollback`` names which one, so the distinction survives the trip to
the client.

**The declarations are checked against each other at import.** A registry whose
fields can disagree is a description, not a contract: nothing stopped a family from
declaring ``promotable`` with no live target to promote, or ``rollbackable`` with no
mechanism. :func:`_verify` runs at import, so a contradictory edit fails the module
that every route and every test imports rather than shipping as a plausible-looking
row. This is deliberately a check on the *declaration*, not on the wiring behind it
--- see the note at the bottom of this module for why the obvious stronger check is
not implemented here.
"""

from __future__ import annotations

from typing import Final

#: Every field a family must declare. A partial row is a contract violation
#: rather than a family with fewer capabilities: "not declared" and "declared
#: absent" are different claims, and only the second one is checkable.
CAPABILITY_FIELDS: Final[frozenset[str]] = frozenset(
    {
        "kind",
        "history",
        "live_target",
        "promotable",
        "rollbackable",
        "rollback",
        "evidence_bearing",
        "gate_mode",
        "calibration",
    }
)

#: What kind of thing the family is. The nine are not the same kind, and reading
#: all of them as deployable artifacts is the second most common misreading of
#: this architecture.
KINDS: Final[frozenset[str]] = frozenset(
    {"runtime_asset", "evidence_asset", "scoring_asset", "anchor_record"}
)

#: How rollback is actually performed. ``none`` is a positive statement that the
#: family has no rollback path, not a gap in the table.
ROLLBACK_MECHANISMS: Final[frozenset[str]] = frozenset(
    {
        "alias_restore",
        "checkpoint_stack_pop",
        "derived_from_activation_history",
        "snapshot_restored_as_new_version",
        "none",
    }
)

ARTIFACT_FAMILY_CAPABILITIES: Final[dict[str, dict[str, object]]] = {
    "prompt": {
        "kind": "runtime_asset",
        "history": "immutable_registry_versions",
        "live_target": "alias",
        "promotable": True,
        "rollbackable": True,
        "rollback": "alias_restore",
        "evidence_bearing": True,
        "gate_mode": "enforced_refinement_advisory_direct",
        "calibration": "provider_optimizer_and_eval",
    },
    "workflow": {
        "kind": "runtime_asset",
        "history": "immutable_published_versions",
        "live_target": "deployment_alias",
        "promotable": True,
        "rollbackable": True,
        "rollback": "checkpoint_stack_pop",
        "evidence_bearing": True,
        "gate_mode": "enforced_deployment_gate",
        "calibration": "manifest_replay",
    },
    "knowledge_base": {
        "kind": "runtime_asset",
        "history": "immutable_build_versions",
        "live_target": "active_version_id",
        "promotable": True,
        "rollbackable": True,
        "rollback": "derived_from_activation_history",
        "evidence_bearing": True,
        "gate_mode": "none",
        "calibration": "retrieval_quality",
    },
    "skill": {
        "kind": "runtime_asset",
        "history": "forward_only_snapshots",
        "live_target": "current_record",
        "promotable": False,
        "rollbackable": True,
        # Not a restore in place: the prior snapshot is written back as a *new*
        # current version, so history only ever grows. Distinct from the other
        # three, and the reason a boolean is not enough.
        "rollback": "snapshot_restored_as_new_version",
        "evidence_bearing": True,
        "gate_mode": "enforced_refinement_only",
        "calibration": "agent_free_optimizer",
    },
    "tool": {
        "kind": "runtime_asset",
        "history": "named_version_rows",
        "live_target": "none",
        "promotable": False,
        "rollbackable": False,
        "rollback": "none",
        "evidence_bearing": True,
        "gate_mode": "none",
        "calibration": "revision_fenced_suites",
    },
    "test_set": {
        "kind": "evidence_asset",
        "history": "versioned_examples",
        "live_target": "none",
        "promotable": False,
        "rollbackable": False,
        "rollback": "none",
        "evidence_bearing": True,
        "gate_mode": "evidence_asset",
        "calibration": "not_applicable",
    },
    "mcp_server": {
        "kind": "runtime_asset",
        "history": "audited_edits",
        "live_target": "managed_definition",
        "promotable": False,
        "rollbackable": False,
        "rollback": "none",
        "evidence_bearing": True,
        "gate_mode": "workflow_preflight",
        "calibration": "connection_and_policy_tests",
    },
    "judge": {
        "kind": "scoring_asset",
        "history": "reusable_named_scorer",
        "live_target": "none",
        "promotable": False,
        "rollbackable": False,
        "rollback": "none",
        "evidence_bearing": True,
        "gate_mode": "scoring_asset",
        "calibration": "human_alignment",
    },
    "agent": {
        "kind": "anchor_record",
        "history": "anchor_record",
        "live_target": "enabled_flag",
        "promotable": False,
        "rollbackable": False,
        # The agent-scoped rollback route restores the *artifact* a promotion
        # changed -- a prompt alias, a skill snapshot -- not the agent record.
        # The anchor is what the checkpoint hangs off, so a route path naming a
        # family is not evidence that the family is rollbackable.
        "rollback": "none",
        "evidence_bearing": False,
        "gate_mode": "not_applicable",
        "calibration": "not_applicable",
    },
}


class CapabilityContractError(ValueError):
    """A family's declarations contradict each other or the closed vocabulary."""


def _verify(registry: dict[str, dict[str, object]]) -> None:
    """Check every declaration a family can make against the others it made.

    Raises on the first coherent set of problems rather than one at a time, so a
    bad edit is reported completely.
    """
    problems: list[str] = []
    for family, contract in sorted(registry.items()):
        declared = set(contract)
        if missing := CAPABILITY_FIELDS - declared:
            problems.append(f"{family}: does not declare {sorted(missing)}")
        if unknown := declared - CAPABILITY_FIELDS:
            problems.append(f"{family}: declares unknown field(s) {sorted(unknown)}")
        if missing or unknown:
            continue

        kind = contract["kind"]
        rollback = contract["rollback"]
        if kind not in KINDS:
            problems.append(f"{family}: kind {kind!r} is not one of {sorted(KINDS)}")
        if rollback not in ROLLBACK_MECHANISMS:
            problems.append(
                f"{family}: rollback {rollback!r} is not one of {sorted(ROLLBACK_MECHANISMS)}"
            )
        for flag in ("promotable", "rollbackable", "evidence_bearing"):
            if not isinstance(contract[flag], bool):
                problems.append(f"{family}: {flag} must be a bool, got {contract[flag]!r}")

        # A boolean and a mechanism are two spellings of one fact, so they cannot
        # be allowed to disagree -- that disagreement is exactly what a client
        # reading only the boolean would never see.
        if (rollback == "none") is contract["rollbackable"]:
            problems.append(
                f"{family}: rollbackable={contract['rollbackable']} contradicts "
                f"rollback={rollback!r}"
            )
        # Promotion moves a live target. Without one there is nothing to move,
        # and a client would render an affordance that cannot resolve.
        if contract["live_target"] == "none" and contract["promotable"]:
            problems.append(f"{family}: promotable with live_target='none' -- nothing to promote")
        # Evidence, scoring, and anchor assets are not things one deploys.
        if kind != "runtime_asset" and (contract["promotable"] or rollback != "none"):
            problems.append(f"{family}: kind={kind!r} cannot be promotable or rollbackable")

    if problems:
        raise CapabilityContractError(
            "artifact family capability declarations are inconsistent:\n  " + "\n  ".join(problems)
        )


_verify(ARTIFACT_FAMILY_CAPABILITIES)


# A note on what this module deliberately does not check.
#
# The obvious stronger control is to project these declarations onto the mounted
# route table -- assert that a family declaring ``rollbackable`` has a rollback
# route, and that one declaring ``rollback='none'`` has none. That check was
# written and then discarded, because it is wrong: ``/agents/{agent_id}/rollback``
# is agent-*scoped* but restores a prompt alias or a skill snapshot, so a path
# match would report a contradiction against a declaration that is correct.
#
# Making the projection sound needs each family to name its own release and
# rollback handlers -- the ``Releasable`` / ``Rollbackable`` capability interfaces
# the paper's Section 5.1 argues for and records as future work. This registry is
# the data half of that design. Until the handler half exists, a route-shaped
# check would assert something it cannot see, which is the failure mode the whole
# module is here to prevent.
