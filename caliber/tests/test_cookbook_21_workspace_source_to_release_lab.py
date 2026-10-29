"""Cookbook 21: the claims its README makes, pinned against CALIBER's own code.

This lab ships no data files -- everything it demonstrates is created live -- so its backend
suite pins the *claims* instead: the flag split its banner states, the release state machine its
diagram follows, the digest convention its release requests use, and -- the lab's hardest claim --
that the evaluator is disabled by default and that incomplete plans fail closed. Execution itself
is covered by ``test_workspace_release_evaluation_execution.py``; this suite keeps the cookbook's
default-off boundary aligned with the backend without pretending the executor is absent.
"""

from __future__ import annotations

import ast
import json
from pathlib import Path

from caliber.features import feature_for_path
from caliber.workspace_release_service import (
    _DIGEST_RE,
    RELEASE_TRANSITIONS,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
SRC = REPO_ROOT / "caliber" / "src" / "caliber"
LAB = REPO_ROOT / "docs-site/cookbooks/21-workspace-source-to-release-lab"
PREFIX = "/ajax-api/2.0/mlflow/caliber"


def test_the_flag_split_is_what_the_lab_documents() -> None:
    """Releases share the release_candidates flag; the rest of the journey is ungated."""
    gated = feature_for_path(f"{PREFIX}/projects/PRJ-1/releases")
    assert gated is not None and gated.id == "release_candidates"
    operations = feature_for_path(f"{PREFIX}/projects/PRJ-1/releases/WSREL-1/operations")
    assert operations is not None and operations.id == "release_candidates"
    for ungated in (
        "/projects/PRJ-1/revisions",
        "/projects/PRJ-1/revisions:snapshot",
        "/projects/PRJ-1/revisions/WSR-1/diff",
        "/projects/PRJ-1/change-requests",
        "/projects/PRJ-1/version-tags",
        "/projects/PRJ-1/environments",
        "/projects/PRJ-1/source",
    ):
        assert feature_for_path(f"{PREFIX}{ungated}") is None, ungated


def test_the_release_state_machine_matches_the_readmes_diagram() -> None:
    """draft -> evaluating -> {blocked, rejected, approved, awaiting_quality_signoff} -> ..."""
    assert RELEASE_TRANSITIONS["draft"] == {"evaluating"}
    assert RELEASE_TRANSITIONS["evaluating"] == {
        "blocked",
        "rejected",
        "approved",
        "awaiting_quality_signoff",
    }
    assert RELEASE_TRANSITIONS["awaiting_quality_signoff"] == {
        "rejected",
        "approved",
        "awaiting_approval",
    }
    # Terminal states stay terminal: nothing resurrects a rejected or approved release.
    assert RELEASE_TRANSITIONS["rejected"] == frozenset()
    assert RELEASE_TRANSITIONS["approved"] == frozenset()


def test_the_digest_convention_is_64_lowercase_hex() -> None:
    assert _DIGEST_RE.fullmatch("a" * 64)
    assert not _DIGEST_RE.fullmatch("A" * 64)
    assert not _DIGEST_RE.fullmatch("a" * 63)
    assert not _DIGEST_RE.fullmatch("g" * 64)


def _function_calls_in(tree: ast.AST) -> set[str]:
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            func = node.func
            if isinstance(func, ast.Name):
                names.add(func.id)
            elif isinstance(func, ast.Attribute):
                names.add(func.attr)
    return names


def test_release_evaluations_have_a_registered_executor_but_the_lab_is_default_off() -> None:
    """The lab's boundary is default-off, while the server owns the executor path.

    ``claim_workspace_release_evaluation`` remains the queue boundary and the evaluator is the
    only runtime caller of the claim/complete functions. The evaluator is off by default, so this
    lab's incomplete fixture remains queued; the execution test covers the enabled path.
    """
    claim = "claim_workspace_release_evaluation"
    executors = {"complete_workspace_release_evaluation", "settle_machine_evaluation"}
    claimers: list[str] = []
    callers: list[str] = []
    for path in SRC.rglob("*.py"):
        if path.name == "workspace_release_service.py":
            continue
        called = _function_calls_in(ast.parse(path.read_text(encoding="utf-8")))
        if claim in called:
            claimers.append(str(path.relative_to(REPO_ROOT)))
        if executors & called:
            callers.append(str(path.relative_to(REPO_ROOT)))
    assert claimers == ["caliber/src/caliber/orchestrator/workspace_release_evaluator.py"]
    assert callers == ["caliber/src/caliber/orchestrator/workspace_release_evaluator.py"]
    config_text = (SRC / "config.py").read_text(encoding="utf-8")
    assert "workspace_release_evaluator_enabled: bool = Field(" in config_text
    assert "default=False" in config_text


def test_the_training_steps_and_catalog_agree() -> None:
    """Ten steps in the guide, ten in the catalog -- the steps-contract test pins the README."""
    from caliber.workflows.cookbook_catalog import build_cookbook_catalog

    training = json.loads((LAB / "training-steps.json").read_text(encoding="utf-8"))
    recipe = next(r for r in build_cookbook_catalog()["recipes"] if r["id"] == "21")
    assert len(training["steps"]) == len(recipe["steps"]) == 10
