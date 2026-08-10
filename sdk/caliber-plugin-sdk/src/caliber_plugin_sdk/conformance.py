"""Check a plugin against the contract before an operator has to.

A plugin that violates the contract fails inside CALIBER, in a deployment its
author cannot see, and the operator gets a log line. This module moves that
discovery to the author's own test suite.

The checks return problems rather than raising, and return *all* of them rather
than the first. Fixing one violation only to be told about the next is the
slowest possible way to learn a contract, and the truncated-failure-list mistake
is easy to make in tooling too.
"""

from __future__ import annotations

from collections.abc import Sequence

from caliber_plugin_sdk.contracts import (
    Diagnosis,
    OptimizationRequest,
    OptimizationResult,
    Optimizer,
    OptimizerUnavailable,
)
from caliber_plugin_sdk.declaration import DeclarationError, PluginDeclaration

#: Names CALIBER ships. A plugin claiming one of these is refused by the server,
#: so it is worth failing here where the author is looking.
#:
#: Duplicated from the server rather than imported — importing it would make
#: every plugin depend on the CALIBER distribution, which is precisely what this
#: package exists to avoid. The cost is that this list can go stale; the server
#: refuses the name regardless, so a stale list here means a missed early
#: warning rather than a plugin that slips through.
RESERVED_NAMES = frozenset(
    {
        "MetaPrompt",
        "SkillMetaPrompt",
        "GEPA",
        "DSPyBootstrapFewShot",
        "DSPyMIPRO",
    }
)


def _sample_request(artifact_type: str, *, cold_start: bool = False) -> OptimizationRequest:
    return OptimizationRequest(
        artifact_type=artifact_type,
        current_content="" if cold_start else "You are a helpful assistant.",
        diagnosis=Diagnosis(
            root_cause="the prompt does not require calling lookup_policy",
            affected_components=[artifact_type],
            confidence=0.8,
            alternatives=[],
        ),
        job_id="RFN-conformance",
        agent_id="AGT-conformance",
    )


def check_declaration(declaration: object) -> list[str]:
    """Problems with a declaration, as a list of human-readable strings.

    Empty means the declaration is well-formed. It does *not* mean the optimizer
    works — :func:`check_optimizer` covers that.
    """
    problems: list[str] = []

    if not isinstance(declaration, PluginDeclaration):
        return [
            f"expected a PluginDeclaration, got {type(declaration).__name__}. "
            "The entry point must resolve to a declaration or a callable returning one."
        ]

    if declaration.name in RESERVED_NAMES:
        problems.append(
            f"name {declaration.name!r} is a CALIBER built-in. Plugins may add "
            "optimizers but never redefine one CALIBER ships, because every agent "
            "already configured for that name would silently start running your "
            "code instead. The server refuses this."
        )

    if declaration.name != declaration.name.strip():
        problems.append(
            f"name {declaration.name!r} has surrounding whitespace; it is stored on every "
            "job row, so it will not match what an operator types"
        )

    try:
        optimizer = declaration.factory()
    except OptimizerUnavailable:
        # A legitimate state: the optional dependency is missing in this
        # environment. The declaration is still fine, and CALIBER handles the
        # unavailability at run time.
        return problems
    except Exception as exc:
        problems.append(
            f"factory raised {type(exc).__name__}: {exc}. A factory is called by "
            "CALIBER during plugin load; raising here makes the whole plugin fail "
            "to register. Defer expensive or failure-prone setup into optimize()."
        )
        return problems

    # Widened to ``object`` deliberately. ``factory`` is *annotated* as returning
    # an Optimizer, so a type checker considers this branch unreachable -- but the
    # annotation is the plugin's claim about itself, and verifying claims from
    # code that may never have been type-checked is this module's entire job.
    built: object = optimizer
    if not isinstance(built, Optimizer):
        problems.append(
            f"factory returned {type(built).__name__}, which has no usable "
            "optimize(request) method"
        )

    return problems


def check_optimizer(
    declaration: PluginDeclaration,
    *,
    artifact_types: Sequence[str] | None = None,
) -> list[str]:
    """Run the optimizer against synthetic requests and check what comes back.

    Exercises every artifact kind the declaration claims, plus the cold-start
    case (empty ``current_content``) that a plugin developed against a populated
    deployment will not have tried. Cold start is the first refinement a new
    agent ever runs, so a plugin that assumes non-empty content fails on exactly
    the request a new user makes first.
    """
    problems: list[str] = []
    kinds = list(artifact_types or sorted(declaration.artifact_types))

    try:
        optimizer = declaration.factory()
    except OptimizerUnavailable as exc:
        return [
            f"optimizer is unavailable in this environment ({exc}); "
            "install its dependencies to run conformance checks against it"
        ]
    except Exception as exc:
        return [f"factory raised {type(exc).__name__}: {exc}"]

    for artifact_type in kinds:
        if not declaration.can_target(artifact_type):
            problems.append(
                f"asked to check {artifact_type!r}, which the declaration does not claim"
            )
            continue

        for cold_start in (False, True):
            label = f"{artifact_type}{' (cold start)' if cold_start else ''}"
            request = _sample_request(artifact_type, cold_start=cold_start)
            try:
                result = optimizer.optimize(request)
            except OptimizerUnavailable:
                # Declining is always allowed. CALIBER falls back and records a
                # note, which is the designed behaviour rather than a failure.
                continue
            except Exception as exc:
                problems.append(
                    f"{label}: optimize() raised {type(exc).__name__}: {exc}. Raise "
                    "OptimizerUnavailable when a precondition is missing; any other "
                    "exception fails the refinement job."
                )
                continue

            problems.extend(f"{label}: {problem}" for problem in _check_result(result))

    return problems


def _check_result(result: object) -> list[str]:
    problems: list[str] = []
    if not isinstance(result, OptimizationResult):
        return [f"optimize() returned {type(result).__name__}, expected an OptimizationResult"]
    if not result.content.strip():
        problems.append(
            "returned empty content. If you cannot improve the artifact, raise "
            "OptimizerUnavailable — an empty candidate would be promoted as a "
            "deletion if it passed the eval gate."
        )
    if not result.rationale.strip():
        problems.append(
            "returned no rationale. A human approves this candidate before it "
            "reaches production, and approving a diff with no stated reason makes "
            "that gate a formality."
        )
    if result.total_tokens is not None and result.total_tokens < 0:
        problems.append(f"reported {result.total_tokens} tokens; use None for 'not reported'")
    if result.cost_usd is not None and result.cost_usd < 0:
        problems.append(f"reported a negative cost ({result.cost_usd})")
    return problems


def check_plugin(declaration: object) -> list[str]:
    """Both passes, in order, reporting everything found.

    Stops after the declaration checks only when the declaration is unusable —
    running the optimizer against a malformed declaration produces confusing
    downstream errors rather than more information.
    """
    problems = check_declaration(declaration)
    if not isinstance(declaration, PluginDeclaration):
        return problems
    return problems + check_optimizer(declaration)


def assert_conformant(declaration: object) -> None:
    """Raise if the plugin does not conform. For use in a plugin's test suite.

    .. code-block:: python

        def test_the_plugin_conforms() -> None:
            assert_conformant(declaration)
    """
    problems = check_plugin(declaration)
    if problems:
        listed = "\n".join(f"  - {problem}" for problem in problems)
        raise DeclarationError(f"plugin does not conform to the CALIBER contract:\n{listed}")


__all__ = [
    "RESERVED_NAMES",
    "assert_conformant",
    "check_declaration",
    "check_optimizer",
    "check_plugin",
]
