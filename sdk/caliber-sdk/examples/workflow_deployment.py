"""Promote a version to a deployment alias, record release evidence for it,
and undo the promotion if it turns out to be wrong.
"""

from __future__ import annotations

from typing import Any

from caliber_sdk import CaliberClient


def promote_and_rollback(
    caliber: CaliberClient,
    *,
    workflow_id: str,
    live_version_id: str,
    candidate_version_id: str,
    alias: str = "staging",
) -> dict[str, Any]:
    """Ship a candidate version, confirm it took, then undo it.

    Rollback undoes the *last* promotion; it does not invent a prior version
    out of nothing. So this promotes twice on purpose: first the version
    already presumed live (`live_version_id`, standing in for whatever an
    earlier release put there), which gives the alias a checkpoint to keep;
    then the candidate. Only after that does rollback have something real to
    restore -- a single call, rather than a second promotion you have to get
    right under pressure.
    """
    caliber.workflows.promote_deployment(workflow_id, alias, version_id=live_version_id)
    caliber.workflows.promote_deployment(workflow_id, alias, version_id=candidate_version_id)

    deployments = caliber.workflows.deployments(workflow_id)
    live = next(row for row in deployments if row["alias"] == alias)
    assert live["version_id"] == candidate_version_id

    restored = caliber.workflows.rollback_deployment(workflow_id, alias)

    return {"promoted_to": live["version_id"], "restored_to": restored.get("version_id")}


def record_gate_verdict(caliber: CaliberClient, *, version_id: str, passed: bool) -> dict[str, Any]:
    """Attach advisory release evidence to a version.

    Advisory, not enforced: CALIBER does not block a promotion on this by
    itself (see `ARCHITECTURE.md` Sec 4's "Gate semantics" column) -- it is
    a recorded verdict for the Version panel and for release tooling that
    chooses to check it, such as `caliberctl gate-verdict record`.
    """
    state = "pass" if passed else "fail"
    verdict = caliber.gate_verdicts.record("workflow", version_id, state=state)
    assert isinstance(verdict, dict)
    return verdict
