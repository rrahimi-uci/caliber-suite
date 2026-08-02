"""Confinement for unmanaged host-filesystem workflow nodes.

The promotion gate keeps these nodes out of production aliases, but that is a
single layer: a development alias, a preview path, or a future caller reaches
them with no boundary. ``CALIBER_WORKFLOW_FILE_ROOT`` is the second layer.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from caliber.workflows.runtime import _path_from_inputs, confine_to_file_root


def test_unset_root_is_unconfined_so_existing_workflows_keep_working() -> None:
    """The shipped default must not break a development workflow reading /etc."""
    resolved = confine_to_file_root(Path("/etc/hosts"), what="file/folder input")
    assert resolved == Path("/etc/hosts")


def test_a_path_inside_the_root_is_allowed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CALIBER_WORKFLOW_FILE_ROOT", str(tmp_path))
    target = tmp_path / "inputs" / "data.txt"
    assert confine_to_file_root(target, what="file/folder input") == target.resolve()


def test_the_root_itself_is_allowed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A folder_input pointed at the root is inside it, not outside."""
    monkeypatch.setenv("CALIBER_WORKFLOW_FILE_ROOT", str(tmp_path))
    assert confine_to_file_root(tmp_path, what="file/folder input") == tmp_path.resolve()


def test_a_path_outside_the_root_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CALIBER_WORKFLOW_FILE_ROOT", str(tmp_path / "workspace"))
    (tmp_path / "workspace").mkdir()
    with pytest.raises(ValueError, match="outside the configured workflow file root"):
        confine_to_file_root(Path("/etc/passwd"), what="file/folder input")


def test_traversal_out_of_the_root_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``..`` has to be resolved, not string-matched."""
    root = tmp_path / "workspace"
    root.mkdir()
    monkeypatch.setenv("CALIBER_WORKFLOW_FILE_ROOT", str(root))
    with pytest.raises(ValueError, match="outside the configured workflow file root"):
        confine_to_file_root(root / ".." / "escaped.txt", what="file/folder input")


def test_a_symlink_pointing_out_of_the_root_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The case a prefix comparison cannot catch, and the reason resolve() runs first.

    ``<root>/link`` is textually inside the root while its target is not. A check
    that compared the supplied string would pass this and hand back /etc.
    """
    root = tmp_path / "workspace"
    root.mkdir()
    outside = tmp_path / "secrets"
    outside.mkdir()
    (outside / "key.txt").write_text("sensitive", encoding="utf-8")
    (root / "link").symlink_to(outside)
    monkeypatch.setenv("CALIBER_WORKFLOW_FILE_ROOT", str(root))

    with pytest.raises(ValueError, match="outside the configured workflow file root"):
        confine_to_file_root(root / "link" / "key.txt", what="file/folder input")


def test_a_symlinked_root_still_matches_its_own_contents(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The root is resolved too, so configuring it via a symlink is not a footgun."""
    real = tmp_path / "real"
    real.mkdir()
    link = tmp_path / "via-link"
    link.symlink_to(real)
    monkeypatch.setenv("CALIBER_WORKFLOW_FILE_ROOT", str(link))

    assert confine_to_file_root(real / "data.txt", what="file/folder input") == (
        real / "data.txt"
    ).resolve()


def test_the_node_path_resolver_enforces_the_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The confinement reaches the actual node entry point, not just the helper.

    ``_path_from_inputs`` takes the caller-supplied ``path`` port in preference to
    the authored path, which is the input that made this reachable at run time.
    """
    root = tmp_path / "workspace"
    root.mkdir()
    monkeypatch.setenv("CALIBER_WORKFLOW_FILE_ROOT", str(root))

    with pytest.raises(ValueError, match="outside the configured workflow file root"):
        _path_from_inputs({"path": "/etc/passwd"}, configured_path="", run_input="")
