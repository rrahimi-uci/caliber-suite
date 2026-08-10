"""A reference optimizer, complete enough to copy and small enough to read.

Deliberately not an LLM call. A reference plugin whose behaviour depended on a
model and an API key could not be tested deterministically, and an author
copying it would inherit that problem. This one does something real, mechanical,
and inspectable: it appends the diagnosis's root cause to the artifact as an
explicit requirement.

That is a genuinely useful baseline — "the prompt never told the agent to call
lookup_policy, so tell it to" is a large share of real refinements — while
staying a function of its inputs.

Ship your own plugin as its own distribution; this module exists so the
conformance suite has something honest to check and so the entry-point wiring has
a worked example.
"""

from __future__ import annotations

from caliber_plugin_sdk.contracts import (
    OptimizationRequest,
    OptimizationResult,
    OptimizerUnavailable,
)
from caliber_plugin_sdk.declaration import PluginDeclaration, declare


class RequirementAppender:
    """Turns a diagnosis's root cause into an explicit instruction."""

    #: Below this, the diagnosis is a guess. Appending a hard requirement from a
    #: guess makes the artifact worse in a way the eval gate may not catch, so
    #: declining is the honest move.
    MIN_CONFIDENCE = 0.5

    def optimize(self, request: OptimizationRequest) -> OptimizationResult:
        if request.diagnosis.confidence < self.MIN_CONFIDENCE:
            raise OptimizerUnavailable(
                f"diagnosis confidence {request.diagnosis.confidence} is too low to "
                "derive a requirement from; CALIBER should fall back"
            )

        root_cause = request.diagnosis.root_cause.strip()
        if not root_cause:
            raise OptimizerUnavailable("the diagnosis names no root cause")

        requirement = f"You MUST address the following: {root_cause}"
        base = request.current_content.strip()

        if requirement in base:
            # Already applied on a previous pass. Returning the artifact
            # unchanged would produce a no-op candidate for a human to review,
            # which wastes the scarcest resource in the loop.
            raise OptimizerUnavailable("the requirement is already present in the artifact")

        # Cold start: no existing artifact to extend, so the requirement *is*
        # the artifact. A plugin that assumed non-empty content would fail on
        # the first refinement a new agent ever runs.
        content = f"{base}\n\n{requirement}" if base else requirement

        notes = ""
        if request.review_notes:
            # A reviewer already rejected a candidate and said why. Ignoring
            # that would propose the same shape of change again.
            notes = f"\n\nReviewer guidance from the previous pass: {request.review_notes.strip()}"
            content = f"{content}{notes}"

        return OptimizationResult(
            content=content,
            rationale=(
                f"The diagnosis identified: {root_cause}. Added it to the artifact as an "
                "explicit requirement so the behaviour is stated rather than assumed."
                + (" Incorporated the reviewer's guidance from the prior pass." if notes else "")
            ),
            diff_summary=f"+{2 if base else 1} lines",
            metadata={"confidence": request.diagnosis.confidence},
            # No model call, so no tokens. Reported as 0 rather than None
            # because that is the true number here, whereas None means
            # "unknown" and would misreport a mechanical optimizer as opaque.
            total_tokens=0,
            cost_usd=0.0,
        )


def build_declaration() -> PluginDeclaration:
    """The declaration an entry point would point at.

    A function rather than a module constant so it mirrors the factory form real
    plugins want, where a declaration may need to probe for a dependency before
    stating what it can do.
    """
    return declare(
        "ReferenceRequirementAppender",
        summary="Appends the diagnosis root cause to the artifact as an explicit requirement.",
        artifact_types=("prompt", "skill"),
        factory=RequirementAppender,
        # Never chosen automatically: it is a demonstration, and an automatic
        # rule selecting it would put a reference implementation in a production
        # refinement path.
        explicit_only=True,
        reference=True,
    )


#: What a real plugin's ``pyproject.toml`` would point at:
#:
#: .. code-block:: toml
#:
#:     [project.entry-points."caliber.optimizers"]
#:     reference = "caliber_plugin_sdk.reference:declaration"
declaration = build_declaration()


__all__ = ["RequirementAppender", "build_declaration", "declaration"]
