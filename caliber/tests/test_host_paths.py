"""A shipped tool that opens a host path is confined to the file root in the reference profile (B12.4c, #770).

The attack pack (``tests/egi_scenarios/test_v14_reference_profile.py``) found that the shipped ``extract_document`` tool, admitted by
the reference profile as one of CALIBER's own tools, read any path the server could read: it runs in the tool-sandbox child, which has
an empty environment and so knew neither the profile nor ``CALIBER_WORKFLOW_FILE_ROOT``. A reference-profile sandbox now hands its
child exactly those two variables (``LocalSubprocessToolSandbox._child_environment``) and the tool confines its path itself
(``caliber.workflows.host_paths.confine_in_reference_profile``). The development profile is unchanged. These tests run the real
function, and the real child process.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from caliber.config import CaliberConfig
from caliber.tool_sandbox.models import ToolSandboxRunRequest
from caliber.tool_sandbox.service import LocalSubprocessToolSandbox
from caliber.workflows.host_paths import PathOutsideRoot, confine_in_reference_profile
from caliber.workflows.ingestion_tools import extract_document

ROOT_VARIABLE = "CALIBER_WORKFLOW_FILE_ROOT"
SECRET = "OUTSIDE-SECRET-9c0d"
INSIDE = "INSIDE-OK-51aa"
CODE = "unsupported_in_reference_profile"


@pytest.fixture
def tree(tmp_path: Path) -> dict[str, Path]:
    root = tmp_path / "root"
    outside = tmp_path / "outside"
    sibling = tmp_path / "root-evil"
    for directory in (root, outside, sibling):
        directory.mkdir()
    (root / "inside.txt").write_text(INSIDE, encoding="utf-8")
    (outside / "secret.txt").write_text(SECRET, encoding="utf-8")
    (sibling / "secret.txt").write_text(SECRET, encoding="utf-8")
    (root / "link.txt").symlink_to(outside / "secret.txt")
    (root / "dirlink").symlink_to(outside, target_is_directory=True)
    return {"root": root, "outside": outside, "sibling": sibling}


@pytest.fixture
def reference(tree: dict[str, Path], monkeypatch: pytest.MonkeyPatch) -> dict[str, Path]:
    monkeypatch.setenv("CALIBER_PROFILE", "reference")
    monkeypatch.setenv(ROOT_VARIABLE, str(tree["root"]))
    return tree


def test_outside_the_reference_profile_the_path_is_returned_unchanged(
    tree: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("CALIBER_PROFILE", raising=False)
    monkeypatch.setenv(ROOT_VARIABLE, str(tree["root"]))
    outside = tree["outside"] / "secret.txt"
    assert confine_in_reference_profile(outside, what="x") is outside
    monkeypatch.setenv("CALIBER_PROFILE", "development")
    assert confine_in_reference_profile(outside, what="x") is outside


def test_a_path_inside_the_root_is_returned_resolved(reference: dict[str, Path]) -> None:
    inside = reference["root"] / "sub" / ".." / "inside.txt"
    assert (
        confine_in_reference_profile(inside, what="x")
        == (reference["root"] / "inside.txt").resolve()
    )


@pytest.mark.parametrize(
    "name",
    [
        "outside/secret.txt",
        "root/../outside/secret.txt",
        "root-evil/secret.txt",
        "root/link.txt",
        "root/dirlink/secret.txt",
    ],
)
def test_a_path_outside_the_root_is_refused_by_components_not_by_string_prefix(
    reference: dict[str, Path], name: str
) -> None:
    path = reference["root"].parent / name
    with pytest.raises(PathOutsideRoot) as refused:
        confine_in_reference_profile(path, what="extract_document")
    assert "outside the configured workflow file root" in str(refused.value)
    assert refused.value.root == reference["root"].resolve()
    assert SECRET not in str(refused.value)


def test_no_root_refuses_the_read_and_unconfined_is_refused_with_the_code(
    reference: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    path = reference["root"] / "inside.txt"
    monkeypatch.delenv(ROOT_VARIABLE)
    with pytest.raises(ValueError, match="unmanaged host-filesystem reads are disabled"):
        confine_in_reference_profile(path, what="extract_document")
    monkeypatch.setenv(ROOT_VARIABLE, "unconfined")
    with pytest.raises(ValueError) as refused:
        confine_in_reference_profile(path, what="extract_document")
    assert str(refused.value).startswith(f"{CODE}: ")
    assert not isinstance(refused.value, PathOutsideRoot)


def test_extract_document_reads_inside_and_refuses_outside_in_the_reference_profile(
    reference: dict[str, Path],
) -> None:
    assert INSIDE in extract_document(str(reference["root"] / "inside.txt"))["text"]
    with pytest.raises(PathOutsideRoot):
        extract_document(str(reference["outside"] / "secret.txt"))


def test_extract_document_is_unchanged_in_the_development_profile(
    tree: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("CALIBER_PROFILE", raising=False)
    monkeypatch.setenv(ROOT_VARIABLE, str(tree["root"]))
    assert SECRET in extract_document(str(tree["outside"] / "secret.txt"))["text"]


def test_a_reference_sandbox_gives_its_child_the_profile_and_the_root_and_nothing_else(
    tree: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(ROOT_VARIABLE, str(tree["root"]))
    monkeypatch.setenv("CALIBER_SOME_SECRET", "must-not-cross")
    reference = LocalSubprocessToolSandbox.from_config(CaliberConfig(profile="reference"))
    development = LocalSubprocessToolSandbox.from_config(CaliberConfig(profile="development"))
    assert reference._child_environment() == {
        "CALIBER_PROFILE": "reference",
        ROOT_VARIABLE: str(tree["root"]),
    }
    monkeypatch.delenv(ROOT_VARIABLE)
    assert reference._child_environment() == {"CALIBER_PROFILE": "reference"}
    assert development._child_environment() == {}


def _run_extract_document(sandbox: LocalSubprocessToolSandbox, ref: Path):  # type: ignore[no-untyped-def]
    return sandbox.run_tool(
        ToolSandboxRunRequest(
            module_path="caliber.workflows.ingestion_tools",
            callable_name="extract_document",
            input={"ref": str(ref)},
            timeout_seconds=30,
        )
    )


def test_the_real_sandbox_child_confines_the_shipped_tool_in_the_reference_profile(
    reference: dict[str, Path],
) -> None:
    sandbox = LocalSubprocessToolSandbox.from_config(CaliberConfig(profile="reference"))

    inside = _run_extract_document(sandbox, reference["root"] / "inside.txt")
    assert inside.status == "completed", inside.error
    assert INSIDE in str(inside.output)

    for target in (
        reference["outside"] / "secret.txt",
        reference["sibling"] / "secret.txt",
        reference["root"] / "link.txt",
    ):
        refused = _run_extract_document(sandbox, target)
        assert refused.status != "completed", target
        assert "outside the configured workflow file root" in str(refused.error)
        assert SECRET not in str(refused.output) + str(refused.error)


def test_the_real_sandbox_child_does_not_confine_the_shipped_tool_in_the_development_profile(
    tree: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("CALIBER_PROFILE", raising=False)
    monkeypatch.setenv(ROOT_VARIABLE, str(tree["root"]))
    sandbox = LocalSubprocessToolSandbox.from_config(CaliberConfig(profile="development"))
    result = _run_extract_document(sandbox, tree["outside"] / "secret.txt")
    assert result.status == "completed", result.error
    assert SECRET in str(result.output)
