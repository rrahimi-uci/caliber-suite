"""The conformance suite, checked against plugins that are wrong on purpose.

A checker that passes everything is worse than none: it converts "untested" into
"certified". So most of these tests build a deliberately broken plugin and assert
the checker catches it.
"""

from __future__ import annotations

from typing import Any

import pytest

from caliber_plugin_sdk import (
    DeclarationError,
    OptimizationRequest,
    OptimizationResult,
    OptimizerUnavailable,
    declare,
)
from caliber_plugin_sdk.conformance import (
    RESERVED_NAMES,
    assert_conformant,
    check_declaration,
    check_optimizer,
    check_plugin,
)
from caliber_plugin_sdk.reference import RequirementAppender, declaration


def declaration_for(factory: Any, *, name: str = "Acme", **overrides: Any) -> Any:
    fields: dict[str, Any] = {
        "summary": "Does a thing.",
        "artifact_types": ("prompt",),
        "factory": factory,
    }
    fields.update(overrides)
    return declare(name, **fields)


# --- the reference plugin passes ------------------------------------------


def test_the_reference_plugin_conforms() -> None:
    """The suite's own credibility: if this fails, the checker is broken."""
    assert check_plugin(declaration) == []
    assert_conformant(declaration)


def test_the_reference_plugin_is_never_selected_automatically() -> None:
    """It is a demonstration, and an automatic rule choosing it would put one
    in a production refinement path."""
    assert declaration.explicit_only


# --- declaration-level catches --------------------------------------------


@pytest.mark.parametrize("reserved", sorted(RESERVED_NAMES))
def test_claiming_a_builtin_name_is_caught(reserved: str) -> None:
    """The substitution case, caught where the author is looking.

    The server refuses it too, but by then the author is reading someone else's
    deployment log.
    """
    problems = check_declaration(declaration_for(RequirementAppender, name=reserved))
    assert any("built-in" in problem for problem in problems)


def test_something_that_is_not_a_declaration_is_reported_clearly() -> None:
    problems = check_declaration({"name": "Acme"})
    assert len(problems) == 1
    assert "expected a PluginDeclaration" in problems[0]


def test_a_factory_that_raises_is_caught_with_advice() -> None:
    """A raising factory kills the whole plugin's registration, not one run."""

    def exploding() -> Any:
        raise RuntimeError("no credentials")

    problems = check_declaration(declaration_for(exploding))
    assert any("factory raised RuntimeError" in problem for problem in problems)
    assert any("Defer expensive" in problem for problem in problems)


def test_a_factory_declining_because_a_dependency_is_missing_is_not_a_violation() -> None:
    """A legitimate state: the environment lacks the extra, the contract is fine."""

    def unavailable() -> Any:
        raise OptimizerUnavailable("dspy is not installed")

    assert check_declaration(declaration_for(unavailable)) == []


def test_a_factory_returning_something_without_optimize_is_caught() -> None:
    problems = check_declaration(declaration_for(lambda: object()))
    assert any("no usable optimize" in problem for problem in problems)


# --- optimizer-level catches ----------------------------------------------


class ReturnsEmptyContent:
    def optimize(self, request: OptimizationRequest) -> OptimizationResult:
        return OptimizationResult(content="   ", rationale="did the thing")


class ReturnsNoRationale:
    def optimize(self, request: OptimizationRequest) -> OptimizationResult:
        return OptimizationResult(content="new prompt", rationale="")


class ReturnsTheWrongType:
    def optimize(self, request: OptimizationRequest) -> Any:
        return {"content": "new prompt"}


class RaisesOnColdStart:
    def optimize(self, request: OptimizationRequest) -> OptimizationResult:
        # The bug a plugin developed against a populated deployment ships with.
        return OptimizationResult(
            content=request.current_content.splitlines()[0],
            rationale="kept the first line",
        )


class ReportsNegativeTokens:
    def optimize(self, request: OptimizationRequest) -> OptimizationResult:
        return OptimizationResult(content="x", rationale="y", total_tokens=-1)


def test_empty_content_is_caught_because_it_would_promote_as_a_deletion() -> None:
    problems = check_optimizer(declaration_for(ReturnsEmptyContent))
    assert any("empty content" in problem for problem in problems)
    assert any("OptimizerUnavailable" in problem for problem in problems)


def test_a_missing_rationale_is_caught_because_a_human_approves_the_diff() -> None:
    problems = check_optimizer(declaration_for(ReturnsNoRationale))
    assert any("no rationale" in problem for problem in problems)


def test_returning_the_wrong_type_is_caught() -> None:
    problems = check_optimizer(declaration_for(ReturnsTheWrongType))
    assert any("expected an OptimizationResult" in problem for problem in problems)


def test_the_cold_start_case_is_exercised() -> None:
    """Empty ``current_content`` is the first refinement a new agent runs.

    A plugin tested only against a populated deployment never sees it, which is
    why the suite sends it rather than trusting the author to have tried.
    """
    problems = check_optimizer(declaration_for(RaisesOnColdStart))
    assert problems, "the cold-start request was not exercised"
    assert all("cold start" in problem for problem in problems)
    assert any("IndexError" in problem for problem in problems)


def test_negative_telemetry_is_caught() -> None:
    problems = check_optimizer(declaration_for(ReportsNegativeTokens))
    assert any("use None for 'not reported'" in problem for problem in problems)


def test_declining_a_request_is_always_allowed() -> None:
    """CALIBER falls back and records a note; that is designed, not a failure."""

    class AlwaysDeclines:
        def optimize(self, request: OptimizationRequest) -> OptimizationResult:
            raise OptimizerUnavailable("not applicable to this artifact")

    assert check_optimizer(declaration_for(AlwaysDeclines)) == []


def test_every_declared_artifact_kind_is_exercised() -> None:
    """A plugin claiming skills must actually handle a skill request."""
    seen: list[str] = []

    class RecordsKinds:
        def optimize(self, request: OptimizationRequest) -> OptimizationResult:
            seen.append(request.artifact_type)
            return OptimizationResult(content="x", rationale="y")

    check_optimizer(declaration_for(RecordsKinds, artifact_types=("prompt", "skill")))
    assert set(seen) == {"prompt", "skill"}


def test_checking_an_unclaimed_artifact_kind_is_reported() -> None:
    problems = check_optimizer(declaration_for(RequirementAppender), artifact_types=["workflow"])
    assert any("does not claim" in problem for problem in problems)


# --- reporting behaviour --------------------------------------------------


def test_every_problem_is_reported_rather_than_only_the_first() -> None:
    """Fixing one violation to be told about the next is the slowest way to
    learn a contract."""

    class WrongInTwoWays:
        def optimize(self, request: OptimizationRequest) -> OptimizationResult:
            return OptimizationResult(content="", rationale="")

    problems = check_optimizer(declaration_for(WrongInTwoWays))
    assert any("empty content" in problem for problem in problems)
    assert any("no rationale" in problem for problem in problems)


def test_assert_conformant_lists_every_problem_in_the_message() -> None:
    with pytest.raises(DeclarationError) as caught:
        assert_conformant(declaration_for(ReturnsEmptyContent, name="GEPA"))
    message = str(caught.value)
    assert "built-in" in message
    assert "empty content" in message


def test_check_plugin_stops_at_an_unusable_declaration() -> None:
    """Running the optimizer against a malformed declaration adds confusion,
    not information."""
    problems = check_plugin("not a declaration at all")
    assert len(problems) == 1
