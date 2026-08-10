"""The types a CALIBER optimizer plugin implements.

Protocols rather than base classes. A plugin author should not have to import
and subclass CALIBER's internals to be a valid plugin — structural typing lets
them write a plain class, check it against this contract with ``mypy``, and stay
independent of how the server happens to be organised this release.

The dataclasses here mirror what the server passes and expects back. They are
duplicated deliberately: importing ``caliber.llm.provider`` would make every
plugin depend on the whole server distribution, and the conformance suite would
then be testing an integration rather than a contract.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable


@dataclass(frozen=True)
class Diagnosis:
    """Why the agent failed, as the diagnosis stage concluded.

    ``confidence`` is the field worth reading rather than skipping: a plugin
    that treats a 0.4-confidence root cause the same as a 0.95 one is guessing
    with extra steps. Low confidence is the case for a conservative edit.
    """

    root_cause: str
    affected_components: Sequence[str] = ()
    confidence: float = 1.0
    alternatives: Sequence[str] = ()


@dataclass(frozen=True)
class OptimizationRequest:
    """Everything a plugin gets, and nothing it should not.

    Notably absent: a database session, the agent row, and any credential. A
    plugin receives the artifact and the diagnosis, and returns a candidate. It
    does not get to write to CALIBER's state — the promotion path, the eval
    gate, and the audit record are all on CALIBER's side of this boundary, and
    handing a plugin a session would put them on the plugin's side.
    """

    #: ``"prompt"``, ``"skill"``, or ``"workflow"``.
    artifact_type: str
    #: Current content of the artifact being optimized. Empty on cold start,
    #: which a plugin must handle rather than assume away.
    current_content: str
    diagnosis: Diagnosis
    #: Opaque identifiers, for logging and for correlating with CALIBER's own
    #: records. Not meant to be parsed.
    job_id: str = ""
    agent_id: str = ""
    #: Reviewer guidance from a prior rejected candidate. Present only on retry
    #: passes; when set, ignoring it means proposing the same thing again.
    review_notes: str | None = None
    #: Rows of ``{"input": ..., "expected": ...}`` when the agent has an eval
    #: dataset. Empty is normal, so an optimizer that needs examples must say so
    #: rather than fail.
    trainset: Sequence[Mapping[str, object]] = ()
    #: Per-agent knobs from ``optimizer_config``. Free-form by design: CALIBER
    #: does not know what a third-party optimizer needs to be told.
    options: Mapping[str, object] = field(default_factory=dict)


@dataclass(frozen=True)
class OptimizationResult:
    """The candidate, and the reasoning a reviewer will read.

    ``rationale`` is required rather than optional because a human approves this
    candidate before it reaches production. A diff with no stated reason makes
    that approval a formality, which is the opposite of what the gate is for.
    """

    content: str
    rationale: str
    #: Short human summary of the change, e.g. ``"+5 / -3 lines"``. Shown in the
    #: review queue where a reviewer sizes up the change before opening it.
    diff_summary: str = ""
    #: Anything the plugin wants recorded alongside the candidate. Stored and
    #: displayed, never interpreted.
    metadata: Mapping[str, object] = field(default_factory=dict)
    #: Tokens consumed, when the plugin knows. CALIBER's cost reporting reads
    #: this; ``None`` means "not reported", which is honest, whereas 0 would
    #: claim the run was free.
    total_tokens: int | None = None
    cost_usd: float | None = None


class OptimizerUnavailable(Exception):
    """The optimizer cannot run right now, and CALIBER should fall back.

    Distinct from any other exception on purpose. Raising this says "my optional
    dependency is missing" or "the trainset is empty" — conditions where running
    MetaPrompt instead is the right outcome and the operator needs a note, not a
    failed job. Every other exception is a bug in the plugin and is recorded as
    a job failure, because silently falling back past a bug would hide it.
    """


@runtime_checkable
class Optimizer(Protocol):
    """What a plugin must implement.

    One method. The narrowness is the point: the extension surface is "given a
    diagnosis and an artifact, propose a better artifact", and everything else —
    selection, evaluation, gating, promotion, audit — stays with CALIBER, where
    it can be governed.
    """

    def optimize(self, request: OptimizationRequest) -> OptimizationResult:
        """Produce a candidate artifact.

        Raise :class:`OptimizerUnavailable` when a precondition is missing and
        falling back is correct. Any other exception fails the job.
        """
        ...


__all__ = [
    "Diagnosis",
    "OptimizationRequest",
    "OptimizationResult",
    "Optimizer",
    "OptimizerUnavailable",
]
