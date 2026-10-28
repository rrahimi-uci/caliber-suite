"""The migration-checksum tooling catches every way a shipped migration can be edited (B16-Upgrade.1, #702).

``tests/test_upgrade_from_alpha.py::test_shipped_migrations_unchanged`` runs the check against the real
migration files, where it must pass. Here the same check runs against copies that have been broken on
purpose, and the generator runs against throwaway git repositories, so a check that stopped detecting an
edit would fail here rather than silently pass there. Everything is offline and deterministic.
"""

from __future__ import annotations

import copy
import json
import os
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest

from tests import migration_checksums as mc
from tests.migration_checksums import (
    ManifestError,
    dump_manifest,
    generate_manifest,
    head_revision,
    load_manifest,
    parse_revision,
    sha256_hex,
    validate_manifest,
    verify_tree,
)

MANIFEST = load_manifest()
HEAD = head_revision(MANIFEST)
MIGRATIONS = MANIFEST["migrations_path"]


def _file_for(revision: str) -> str:
    return str(next(e["file"] for e in MANIFEST["migrations"] if e["revision"] == revision))


def _migration_source(revision: str, down: str | None, *, annotated: bool = True) -> bytes:
    down_literal = "None" if down is None else repr(down)
    if annotated:
        body = f'revision: str = "{revision}"\ndown_revision: str | None = {down_literal}\n'
    else:
        body = f'revision = "{revision}"\ndown_revision = {down_literal}\n'
    return f'"""synthetic."""\n\n{body}\n\ndef upgrade() -> None:\n    pass\n'.encode()


@pytest.fixture
def tree(tmp_path: Path) -> Path:
    """A private copy of the real migration files that a test may damage."""
    target = tmp_path / "versions"
    shutil.copytree(mc.VERSIONS_DIR, target, ignore=shutil.ignore_patterns("__pycache__"))
    return target


# --------------------------------------------------------------------------- the real tree and the copies


def test_an_untouched_copy_of_the_tree_is_intact(tree: Path) -> None:
    assert verify_tree(MANIFEST, tree) == []


def test_a_tampered_migration_is_reported_with_both_digests(tree: Path) -> None:
    name = _file_for("0050")
    path = tree / name
    path.write_bytes(path.read_bytes() + b"# harmless-looking comment\n")
    (problem,) = verify_tree(MANIFEST, tree)
    assert problem.startswith(f"{name}: changed since caliber-v0.1.0a1")
    assert sha256_hex(path.read_bytes()) in problem
    assert next(e["sha256"] for e in MANIFEST["migrations"] if e["file"] == name) in problem
    assert _restore_command(name) in problem
    assert "new migration that revises the current head" in problem


def test_a_line_ending_conversion_is_a_change(tree: Path) -> None:
    name = _file_for("0001")
    path = tree / name
    path.write_bytes(path.read_bytes().replace(b"\n", b"\r\n"))
    (problem,) = verify_tree(MANIFEST, tree)
    assert problem.startswith(f"{name}: changed")


def test_a_removed_migration_is_reported(tree: Path) -> None:
    name = _file_for("0077")
    (tree / name).unlink()
    (problem,) = verify_tree(MANIFEST, tree)
    assert problem.startswith(f"{name}: shipped in caliber-v0.1.0a1 but missing.")
    assert _restore_command(name) in problem


def _restore_command(name: str) -> str:
    """The recorded-commit restore, not a moveable tag name, so it works when the tag ref is absent."""
    return f"git show {MANIFEST['tag_commit']}:{MIGRATIONS}/{name} > {MIGRATIONS}/{name}"


def test_a_renamed_migration_is_reported_as_missing_and_as_a_rename(tree: Path) -> None:
    name = _file_for("0077")
    (tree / name).rename(tree / "0077_better_name.py")
    (problem,) = verify_tree(MANIFEST, tree)
    assert problem.startswith(f"{name}: shipped in caliber-v0.1.0a1 but missing")
    assert "0077_better_name.py: a rename" in problem


def test_a_migration_inserted_into_shipped_history_is_reported(tree: Path) -> None:
    """A new file that revises an old revision forks the history (and would make two heads)."""
    (tree / "0050a_inserted.py").write_bytes(_migration_source("0050a", "0050"))
    (problem,) = verify_tree(MANIFEST, tree)
    assert problem.startswith(
        "0050a_inserted.py: revision '0050a' is not in the caliber-v0.1.0a1 manifest"
    )
    assert f"does not descend from its head {HEAD!r}" in problem
    assert "re-parent it onto the head" in problem
    assert "git show" not in problem  # nothing to restore: the file is new


def test_a_new_root_migration_is_reported(tree: Path) -> None:
    (tree / "0000_second_root.py").write_bytes(_migration_source("0000", None, annotated=False))
    (problem,) = verify_tree(MANIFEST, tree)
    assert problem.startswith("0000_second_root.py: revision '0000' is not in the")


def test_a_second_file_for_a_shipped_revision_is_reported(tree: Path) -> None:
    (tree / "0113_duplicate.py").write_bytes(_migration_source(HEAD, "0112"))
    (problem,) = verify_tree(MANIFEST, tree)
    assert (
        problem
        == f"0113_duplicate.py: revision {HEAD!r} is already defined by another migration file"
    )


def test_two_new_files_with_one_revision_are_reported(tree: Path) -> None:
    (tree / "9001_a.py").write_bytes(_migration_source("9001", HEAD))
    (tree / "9001_b.py").write_bytes(_migration_source("9001", HEAD))
    (problem,) = verify_tree(MANIFEST, tree)
    assert problem == "9001_b.py: revision '9001' is already defined by another migration file"


def test_an_unreadable_extra_file_is_reported(tree: Path) -> None:
    (tree / "9002_broken.py").write_bytes(b"def upgrade(:\n")
    (problem,) = verify_tree(MANIFEST, tree)
    assert problem.startswith("9002_broken.py: not in the alpha manifest and unreadable")


def test_migrations_added_after_the_alpha_are_allowed_however_long_the_chain(tree: Path) -> None:
    """The manifest is frozen, so a new migration needs no regeneration: it only has to descend from the head."""
    (tree / "9001_first.py").write_bytes(_migration_source("9001", HEAD))
    (tree / "9002_second.py").write_bytes(_migration_source("9002", "9001"))
    (tree / "9003_third.py").write_bytes(_migration_source("9003", "9002", annotated=False))
    assert verify_tree(MANIFEST, tree) == []


def test_a_new_migration_that_skips_the_chain_is_reported(tree: Path) -> None:
    (tree / "9001_orphan.py").write_bytes(_migration_source("9001", "9000-not-defined-anywhere"))
    (problem,) = verify_tree(MANIFEST, tree)
    assert problem.startswith("9001_orphan.py: revision '9001' is not in the")


def test_a_cyclic_extra_chain_is_reported_not_followed_forever(tree: Path) -> None:
    (tree / "9001_a.py").write_bytes(_migration_source("9001", "9002"))
    (tree / "9002_b.py").write_bytes(_migration_source("9002", "9001"))
    problems = verify_tree(MANIFEST, tree)
    assert len(problems) == 2
    assert all("does not descend from its head" in p for p in problems)


def test_the_package_marker_and_non_python_files_are_not_migrations(tree: Path) -> None:
    (tree / "__init__.py").write_text("")
    (tree / "README.md").write_text("not a migration")
    (tree / "notes.txt").write_text("nor this")
    assert verify_tree(MANIFEST, tree) == []


def test_every_problem_in_one_run_is_listed(tree: Path) -> None:
    (tree / _file_for("0010")).write_bytes(b"x = 1\n")
    (tree / _file_for("0020")).unlink()
    (tree / "9001_fork.py").write_bytes(_migration_source("9001", "0030"))
    assert len(verify_tree(MANIFEST, tree)) == 3


# --------------------------------------------------------------------------- the manifest as a document


def test_the_real_manifest_validates() -> None:
    assert validate_manifest(MANIFEST) == []


def _broken(mutate: Any) -> list[str]:
    manifest = copy.deepcopy(MANIFEST)
    mutate(manifest)
    return validate_manifest(manifest)


@pytest.mark.parametrize(
    ("mutate", "needle"),
    [
        (lambda m: m.update(schema_version=2), "schema_version is 2"),
        (lambda m: m.update(algorithm="md5"), "algorithm is 'md5'"),
        (lambda m: m.update(head_revision="0999"), "head_revision is '0999'"),
        (lambda m: m.update(base_revision="0002"), "base_revision does not match"),
        (lambda m: m.update(migration_count=1), "migration_count is 1"),
        (lambda m: m["migrations"][3].update(sha256="ABC"), "sha256 is not 64 lowercase hex"),
        (lambda m: m["migrations"][3].update(sha256="g" * 64), "sha256 is not 64 lowercase hex"),
        (lambda m: m["migrations"][3].update(file=m["migrations"][2]["file"]), "appears twice"),
        (lambda m: m["migrations"].reverse(), "not listed in chain order"),
        (lambda m: m.update(migrations=[]), "empty or not a list"),
        (lambda m: m.update(migrations="nope"), "empty or not a list"),
        (lambda m: m["migrations"][5].update(down_revisions=["0001"]), "history forks"),
        (lambda m: m["migrations"][5].update(down_revisions=["0001", "0002"]), "merges"),
        (lambda m: m["migrations"][5].update(down_revisions=["nope"]), "which no file defines"),
        (lambda m: m["migrations"][5].update(down_revisions=[]), "exactly one root"),
        (lambda m: m["migrations"][5].update(revision="0001"), "defined by two files"),
    ],
)
def test_a_damaged_manifest_is_rejected(mutate: Any, needle: str) -> None:
    problems = _broken(mutate)
    assert any(needle in p for p in problems), problems


def test_a_manifest_whose_history_is_a_cycle_is_rejected() -> None:
    manifest = copy.deepcopy(MANIFEST)
    manifest["migrations"][0]["down_revisions"] = [manifest["migrations"][1]["revision"]]
    manifest["migrations"][1]["down_revisions"] = [manifest["migrations"][0]["revision"]]
    problems = validate_manifest(manifest)
    assert any("exactly one root" in p for p in problems), problems


def test_a_chain_with_a_detached_cycle_is_rejected() -> None:
    entries = [
        {"file": "a.py", "revision": "a", "down_revisions": [], "sha256": "0" * 64},
        {"file": "b.py", "revision": "b", "down_revisions": ["a"], "sha256": "0" * 64},
        {"file": "c.py", "revision": "c", "down_revisions": ["d"], "sha256": "0" * 64},
        {"file": "d.py", "revision": "d", "down_revisions": ["c"], "sha256": "0" * 64},
    ]
    with pytest.raises(ManifestError, match="do not form one chain"):
        mc._linear_chain(entries)


def test_the_head_is_derived_from_the_files_not_recorded() -> None:
    manifest = copy.deepcopy(MANIFEST)
    manifest["head_revision"] = "tampered"
    assert head_revision(manifest) == HEAD
    manifest["migrations"] = manifest["migrations"][:-1]
    assert head_revision(manifest) == manifest["migrations"][-1]["revision"] != HEAD


def test_two_heads_cannot_be_derived() -> None:
    manifest = copy.deepcopy(MANIFEST)
    manifest["migrations"][-1]["down_revisions"] = ["0001"]
    with pytest.raises(ManifestError, match="exactly one head"):
        head_revision(manifest)


# --------------------------------------------------------------------------- parsing a migration's source


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        (b'revision = "a"\ndown_revision = None\n', ("a", ())),
        (b'revision: str = "a"\ndown_revision: str | None = "b"\n', ("a", ("b",))),
        (b'revision = "m"\ndown_revision = ("x", "y")\n', ("m", ("x", "y"))),
        (b'revision = "m"\ndown_revision = ["x"]\n', ("m", ("x",))),
        # Other statements around the two assignments are ignored, as are annotations without a value.
        (
            b'import os\nlater: int\nfirst, second = 1, 2\nobj.attr = 3\nrevision = "a"\ndown_revision = None\n',
            ("a", ()),
        ),
    ],
)
def test_parse_revision_reads_the_assignments_without_importing(
    source: bytes, expected: tuple[Any, ...]
) -> None:
    assert parse_revision(source, "m.py") == expected


@pytest.mark.parametrize(
    ("source", "message"),
    [
        (b"def broken(:\n", "not valid Python"),
        (b'revision = "a"\n', "no `down_revision`"),
        (b"down_revision = None\n", "no string `revision`"),
        (b'revision = ""\ndown_revision = None\n', "no string `revision`"),
        (b"revision = 7\ndown_revision = None\n", "no string `revision`"),
        (b'revision = "a"\ndown_revision = compute()\n', "not a usable literal"),
        # ast.literal_eval raises TypeError (unhashable set member) and ValueError (null byte) on these.
        (b'revision = "a"\ndown_revision = {[1]}\n', "not a usable literal"),
        (b'revision = "a"\x00\ndown_revision = None\n', "not valid Python"),
        (b'revision = "a"\ndown_revision = 3\n', "unsupported down_revision"),
        (b'revision = "a"\ndown_revision = ("x", 3)\n', "unsupported down_revision"),
    ],
)
def test_parse_revision_rejects_what_it_cannot_trust(source: bytes, message: str) -> None:
    with pytest.raises(ManifestError, match=message):
        parse_revision(source, "m.py")


# --------------------------------------------------------------------------- generating from a git tag


def _git_env() -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    env.update(
        GIT_CONFIG_GLOBAL=os.devnull,
        GIT_CONFIG_SYSTEM=os.devnull,
        GIT_AUTHOR_NAME="t",
        GIT_AUTHOR_EMAIL="t@example.invalid",
        GIT_COMMITTER_NAME="t",
        GIT_COMMITTER_EMAIL="t@example.invalid",
    )
    return env


def _git(repo: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-c", "core.autocrlf=false", *args],
        cwd=repo,
        env=_git_env(),
        check=True,
        capture_output=True,
    )


MIGRATE_PY = b'VERSION_TABLE = "caliber_alembic_version"\n'


def _repo_with(
    tmp_path: Path,
    files: dict[str, bytes],
    tag: str = "rel-1",
    migrate_py: bytes | None = MIGRATE_PY,
) -> Path:
    """A throwaway repository whose ``tag`` commit contains ``files`` (paths relative to the migrations dir)."""
    repo = tmp_path / "repo"
    versions = repo / MIGRATIONS
    versions.mkdir(parents=True)
    _git(repo, "init", "-q")
    for name, data in files.items():
        (versions / name).write_bytes(data)
    if migrate_py is not None:
        (repo / mc.MIGRATE_MODULE_PATH).write_bytes(migrate_py)
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "release", "--allow-empty")
    _git(repo, "tag", tag)
    return repo


CHAIN = {
    "0001_a.py": _migration_source("0001", None),
    "0002_b.py": _migration_source("0002", "0001"),
    "0003_c.py": _migration_source("0003", "0002", annotated=False),
}


def test_the_manifest_is_built_from_the_tag_with_a_derived_head(tmp_path: Path) -> None:
    repo = _repo_with(tmp_path, {**CHAIN, "__init__.py": b"", "notes.txt": b"x"})
    manifest = generate_manifest(repo, "rel-1")
    assert manifest["head_revision"] == "0003"
    assert manifest["base_revision"] == "0001"
    assert manifest["migration_count"] == 3
    assert [e["file"] for e in manifest["migrations"]] == list(CHAIN)
    assert manifest["migrations"][1] == {
        "file": "0002_b.py",
        "revision": "0002",
        "down_revisions": ["0001"],
        "sha256": sha256_hex(CHAIN["0002_b.py"]),
    }
    assert len(manifest["tag_commit"]) == 40
    assert validate_manifest(manifest) == []
    assert verify_tree(manifest, repo / MIGRATIONS) == []


def test_the_manifest_records_the_tag_not_the_working_tree(tmp_path: Path) -> None:
    repo = _repo_with(tmp_path, CHAIN)
    versions = repo / MIGRATIONS
    (versions / "0002_b.py").write_bytes(CHAIN["0002_b.py"] + b"# edited after the release\n")
    (versions / "0004_new.py").write_bytes(_migration_source("0004", "0003"))
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "after")
    manifest = generate_manifest(repo, "rel-1")
    assert manifest["head_revision"] == "0003"
    assert manifest["migrations"][1]["sha256"] == sha256_hex(CHAIN["0002_b.py"])
    (problem,) = verify_tree(manifest, versions)
    assert problem.startswith(
        f"0002_b.py: changed since rel-1 (sha256 {sha256_hex(versions.joinpath('0002_b.py').read_bytes())}, "
        f"shipped {sha256_hex(CHAIN['0002_b.py'])}). "
    )
    assert f"git show {manifest['tag_commit']}:{MIGRATIONS}/0002_b.py" in problem


def test_checksums_are_of_the_committed_bytes_whatever_git_would_check_out(tmp_path: Path) -> None:
    crlf = CHAIN["0002_b.py"].replace(b"\n", b"\r\n")
    repo = _repo_with(tmp_path, {**CHAIN, "0002_b.py": crlf})
    assert generate_manifest(repo, "rel-1")["migrations"][1]["sha256"] == sha256_hex(crlf)


def test_an_annotated_tag_resolves_to_its_commit(tmp_path: Path) -> None:
    repo = _repo_with(tmp_path, CHAIN)
    _git(repo, "tag", "-a", "-m", "annotated", "rel-annotated")
    annotated = generate_manifest(repo, "rel-annotated")
    assert annotated | {"release_tag": "rel-1"} == generate_manifest(repo, "rel-1")


def test_a_missing_tag_is_an_error_that_names_it(tmp_path: Path) -> None:
    repo = _repo_with(tmp_path, CHAIN)
    with pytest.raises(ManifestError, match="rel-nope"):
        generate_manifest(repo, "rel-nope")


def test_a_tag_without_migrations_is_an_error(tmp_path: Path) -> None:
    repo = _repo_with(tmp_path, {})
    with pytest.raises(ManifestError, match="contains no migrations"):
        generate_manifest(repo, "rel-1")


@pytest.mark.parametrize(
    ("files", "message"),
    [
        (
            {**CHAIN, "0004_fork.py": _migration_source("0004", "0002")},
            "history forks",
        ),
        (
            {**CHAIN, "0004_merge.py": b'revision = "0004"\ndown_revision = ("0003", "0002")\n'},
            "merges",
        ),
        (
            {**CHAIN, "0000_root.py": _migration_source("0000", None)},
            "exactly one root",
        ),
        (
            {**CHAIN, "0003_other.py": _migration_source("0003", "0002")},
            "defined by two files",
        ),
        (
            {"0002_b.py": _migration_source("0002", "0001")},
            "which no file defines",
        ),
        ({"0001_a.py": b"def upgrade(:\n"}, "not valid Python"),
    ],
)
def test_a_tag_whose_history_is_not_one_chain_is_refused(
    tmp_path: Path, files: dict[str, bytes], message: str
) -> None:
    repo = _repo_with(tmp_path, files)
    with pytest.raises(ManifestError, match=message):
        generate_manifest(repo, "rel-1")


def test_dump_and_load_round_trip(tmp_path: Path) -> None:
    repo = _repo_with(tmp_path, CHAIN)
    manifest = generate_manifest(repo, "rel-1")
    text = dump_manifest(manifest)
    assert text.endswith("}\n")
    path = tmp_path / "m.json"
    path.write_text(text)
    assert load_manifest(path) == manifest
    assert json.loads(text) == manifest


def test_the_committed_fixture_is_the_dump_of_itself() -> None:
    """Formatting is part of the fixture, so a regenerate produces no spurious diff."""
    assert mc.MANIFEST_PATH.read_text(encoding="utf-8") == dump_manifest(MANIFEST)


# --------------------------------------------------------------------------- the command line


def test_cli_generate_writes_the_fixture_and_check_confirms_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    repo = _repo_with(tmp_path, CHAIN)
    out = tmp_path / "out.json"
    monkeypatch.setattr(mc, "REPO_ROOT", repo)
    assert mc._cli(["generate", "--tag", "rel-1", "--output", str(out)]) == 0
    assert json.loads(out.read_text())["head_revision"] == "0003"
    assert mc._cli(["generate", "--output", str(out), "--check"]) == 0
    assert "matches the release tag" in capsys.readouterr().out


def test_cli_generate_check_fails_on_an_absent_malformed_or_stale_fixture(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    repo = _repo_with(tmp_path, CHAIN)
    out = tmp_path / "out.json"
    monkeypatch.setattr(mc, "REPO_ROOT", repo)
    assert mc._cli(["generate", "--output", str(out), "--check"]) == 1
    assert "does not exist" in capsys.readouterr().err
    assert out.exists() is False
    out.write_text("not json")
    assert mc._cli(["generate", "--output", str(out), "--check"]) == 1
    assert "is not valid JSON" in capsys.readouterr().err
    out.write_text("{}\n")
    assert mc._cli(["generate", "--output", str(out), "--check"]) == 1
    err = capsys.readouterr().err
    assert "schema_version is None" in err
    assert "does not match the release tag" in err
    assert mc.RECOVERY_HINT in err
    assert out.read_text() == "{}\n"


def test_cli_check_fails_when_the_manifest_is_shortened_or_a_checksum_is_edited_to_match(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The working-tree check passes both of these; only the tag comparison catches them."""
    repo = _repo_with(tmp_path, CHAIN)
    monkeypatch.setattr(mc, "REPO_ROOT", repo)
    manifest = generate_manifest(repo, "rel-1")
    out = tmp_path / "out.json"

    shortened = copy.deepcopy(manifest)
    shortened["migrations"].pop()
    shortened.update(head_revision="0002", migration_count=2)
    assert validate_manifest(shortened) == []  # internally consistent, so only the tag can object
    out.write_text(dump_manifest(shortened))
    assert mc._cli(["generate", "--output", str(out), "--check"]) == 1
    err = capsys.readouterr().err
    assert "0003_c.py: shipped in" in err and "absent from the manifest" in err
    assert "head_revision: the manifest says '0002'" in err
    assert "migration_count: the manifest says 2" in err

    edited = copy.deepcopy(manifest)
    edited["migrations"][1]["sha256"] = sha256_hex(b"the edited migration")
    out.write_text(dump_manifest(edited))
    assert validate_manifest(edited) == []
    assert mc._cli(["generate", "--output", str(out), "--check"]) == 1
    assert "0002_b.py: the manifest records" in capsys.readouterr().err

    out.write_text(dump_manifest(manifest))
    assert mc._cli(["generate", "--output", str(out), "--check"]) == 0


def test_cli_check_names_a_file_the_manifest_lists_but_the_tag_never_shipped(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    repo = _repo_with(tmp_path, CHAIN)
    monkeypatch.setattr(mc, "REPO_ROOT", repo)
    padded = generate_manifest(repo, "rel-1")
    padded["migrations"].append(
        {"file": "0004_x.py", "revision": "0004", "down_revisions": ["0003"], "sha256": "0" * 64}
    )
    padded.update(head_revision="0004", migration_count=4)
    out = tmp_path / "out.json"
    out.write_text(dump_manifest(padded))
    assert mc._cli(["generate", "--output", str(out), "--check"]) == 1
    assert "0004_x.py: in the manifest but not shipped" in capsys.readouterr().err


def test_cli_generate_reports_a_missing_tag_without_a_traceback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(mc, "REPO_ROOT", _repo_with(tmp_path, CHAIN))
    assert mc._cli(["generate", "--tag", "rel-nope", "--output", str(tmp_path / "o.json")]) == 1
    assert capsys.readouterr().err.startswith("error: `git rev-parse")


def test_cli_verify_passes_on_an_intact_tree_and_fails_loudly_on_a_changed_one(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    repo = _repo_with(tmp_path, CHAIN)
    manifest_path = tmp_path / "m.json"
    manifest_path.write_text(dump_manifest(generate_manifest(repo, "rel-1")))
    monkeypatch.setattr(mc, "MANIFEST_PATH", manifest_path)
    monkeypatch.setattr(mc, "VERSIONS_DIR", repo / MIGRATIONS)
    assert mc._cli(["verify"]) == 0
    assert "shipped migrations unchanged" in capsys.readouterr().out

    (repo / MIGRATIONS / "0003_c.py").write_bytes(b"revision = 'x'\n")
    assert mc._cli(["verify"]) == 1
    err = capsys.readouterr().err
    assert "0003_c.py: changed since rel-1" in err
    assert mc.RECOVERY_HINT in err


def test_cli_verify_reports_an_unusable_manifest(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    broken = copy.deepcopy(MANIFEST)
    broken["migrations"] = []
    path = tmp_path / "m.json"
    path.write_text(json.dumps(broken))
    monkeypatch.setattr(mc, "MANIFEST_PATH", path)
    assert mc._cli(["verify"]) == 1
    assert capsys.readouterr().err.startswith("error: expected exactly one head")


def test_the_test_helper_agrees_with_the_real_tree_via_the_command_line(
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert mc._cli(["verify"]) == 0
    assert "shipped migrations unchanged" in capsys.readouterr().out


# --------------------------------------------------------------------------- the version table


def test_the_version_table_is_read_from_the_module_at_the_tag(tmp_path: Path) -> None:
    repo = _repo_with(tmp_path, CHAIN, migrate_py=b'VERSION_TABLE: str = "alpha_versions"\n')
    assert generate_manifest(repo, "rel-1")["version_table"] == "alpha_versions"


@pytest.mark.parametrize(
    "migrate_py", [None, b"OTHER = 1\n", b'VERSION_TABLE = ""\n', b"VERSION_TABLE = 7\n"]
)
def test_a_tag_without_a_usable_version_table_is_refused(
    tmp_path: Path, migrate_py: bytes | None
) -> None:
    repo = _repo_with(tmp_path, CHAIN, migrate_py=migrate_py)
    with pytest.raises(ManifestError, match="VERSION_TABLE|migrate.py"):
        generate_manifest(repo, "rel-1")


@pytest.mark.parametrize(
    ("mutate", "needle"),
    [
        (lambda m: m.pop("version_table"), "version_table is missing"),
        (lambda m: m.update(tag_commit=""), "tag_commit is missing"),
        (lambda m: m.pop("release_tag"), "release_tag is missing"),
        (lambda m: m.update(migrations_path="elsewhere"), "migrations_path is 'elsewhere'"),
    ],
)
def test_a_manifest_missing_a_provenance_field_is_rejected(mutate: Any, needle: str) -> None:
    assert any(needle in p for p in _broken(mutate))


# --------------------------------------------------------------------------- comparing with the tag


def _manifest_for(repo: Path) -> dict[str, Any]:
    return generate_manifest(repo, "rel-1")


def test_a_manifest_equal_to_the_tag_has_no_problems(tmp_path: Path) -> None:
    repo = _repo_with(tmp_path, CHAIN)
    assert mc.verify_manifest_against_tag(_manifest_for(repo), repo) == []


def test_the_commit_is_enough_when_the_tag_ref_is_absent(tmp_path: Path) -> None:
    """A checkout that fetched the commit but not the tag name still verifies, against the commit."""
    repo = _repo_with(tmp_path, CHAIN)
    manifest = _manifest_for(repo)
    _git(repo, "tag", "-d", "rel-1")
    assert mc.verify_manifest_against_tag(manifest, repo) == []
    tampered = copy.deepcopy(manifest)
    tampered["migrations"][0]["sha256"] = "f" * 64
    assert any("0001_a.py" in p for p in mc.verify_manifest_against_tag(tampered, repo))


def test_a_moved_tag_is_reported_and_the_recorded_commit_is_still_what_is_verified(
    tmp_path: Path,
) -> None:
    repo = _repo_with(tmp_path, CHAIN)
    manifest = _manifest_for(repo)
    (repo / MIGRATIONS / "0003_c.py").write_bytes(CHAIN["0003_c.py"] + b"# after\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "later")
    _git(repo, "tag", "-f", "rel-1")
    (problem,) = mc.verify_manifest_against_tag(manifest, repo)
    assert problem.startswith("rel-1 now points at ")
    assert f"generated from {manifest['tag_commit']}: the tag was moved" in problem


def test_the_tag_ref_is_used_when_only_the_recorded_commit_is_missing(tmp_path: Path) -> None:
    repo = _repo_with(tmp_path, CHAIN)
    manifest = _manifest_for(repo)
    manifest["tag_commit"] = "1" * 40
    problems = mc.verify_manifest_against_tag(manifest, repo)
    assert problems[0].startswith("rel-1 now points at ")
    assert any(p.startswith("tag_commit: the manifest says") for p in problems)


def test_a_checkout_with_neither_the_tag_nor_its_commit_is_unavailable_not_a_pass(
    tmp_path: Path,
) -> None:
    repo = _repo_with(tmp_path, CHAIN)
    manifest = _manifest_for(repo)
    manifest["tag_commit"] = "2" * 40
    manifest["release_tag"] = "rel-nope"
    with pytest.raises(mc.TagUnavailableError, match="rel-nope"):
        mc.verify_manifest_against_tag(manifest, repo)
    assert isinstance(mc.TagUnavailableError("x"), ManifestError)


def test_cli_check_reports_an_unavailable_tag_as_a_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    repo = _repo_with(tmp_path, CHAIN)
    manifest = _manifest_for(repo)
    manifest.update(tag_commit="3" * 40, release_tag="rel-nope")
    out = tmp_path / "out.json"
    out.write_text(dump_manifest(manifest))
    monkeypatch.setattr(mc, "REPO_ROOT", repo)
    assert mc._cli(["generate", "--output", str(out), "--check"]) == 1
    assert "error: neither rel-nope nor its recorded commit" in capsys.readouterr().err


def test_generate_can_read_a_commit_while_recording_the_tag_name(tmp_path: Path) -> None:
    repo = _repo_with(tmp_path, CHAIN)
    by_tag = generate_manifest(repo, "rel-1")
    by_commit = generate_manifest(repo, "rel-1", ref=by_tag["tag_commit"])
    assert by_commit == by_tag


def test_the_tag_check_is_required_only_when_the_ci_job_that_fetches_it_says_so() -> None:
    assert mc.require_tag_from_env({}) is False
    assert mc.require_tag_from_env({mc.REQUIRE_TAG_ENV: ""}) is False
    assert mc.require_tag_from_env({mc.REQUIRE_TAG_ENV: "1"}) is True


def test_the_real_tag_test_fails_rather_than_skips_when_the_tag_is_required_and_absent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Runs ``test_the_manifest_matches_the_release_tag`` in a checkout without the tag, as CI would."""
    from tests import test_upgrade_from_alpha as upgrade

    monkeypatch.setattr(upgrade, "REPO_ROOT", tmp_path)  # an empty directory: no repository, no tag
    monkeypatch.delenv(mc.REQUIRE_TAG_ENV, raising=False)
    # BaseException: a skip or a failure raised by pytest is not an Exception, and the test must tell them apart.
    with pytest.raises(BaseException) as unrequired:
        upgrade.test_the_manifest_matches_the_release_tag()
    assert unrequired.type is pytest.skip.Exception
    monkeypatch.setenv(mc.REQUIRE_TAG_ENV, "1")
    with pytest.raises(BaseException) as required:
        upgrade.test_the_manifest_matches_the_release_tag()
    assert required.type is pytest.fail.Exception
    assert "is set but the tag is unavailable" in str(required.value)


def test_a_missing_git_binary_is_an_error_not_a_traceback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def no_git(*args: Any, **kwargs: Any) -> Any:
        raise FileNotFoundError("git")

    monkeypatch.setattr(subprocess, "run", no_git)
    with pytest.raises(ManifestError, match="could not run"):
        mc._git(tmp_path, "status")
    assert mc._resolve_commit(tmp_path, "rel-1") is None


# --------------------------------------------------------------------------- odd things in versions/


def test_editor_lock_files_and_init_variants_are_not_migrations(tree: Path) -> None:
    """Alembic skips names matching ``(?!\\.\\#|__init__)``; a dangling ``.#`` symlink is what Emacs leaves."""
    (tree / ".#0001_initial.py").symlink_to(tree / "nowhere-at-all")
    (tree / ".#0002_other.py").write_bytes(b"not python at all (")
    (tree / "__init__helper.py").write_bytes(b"also not python (")
    assert mc.is_migration_name(".#x.py") is False
    assert mc.is_migration_name("__init__helper.py") is False
    assert mc.is_migration_name("0001_x.py") is True
    assert verify_tree(MANIFEST, tree) == []


def test_only_regular_files_are_migrations(tree: Path) -> None:
    (tree / "9001_directory.py").mkdir()
    (tree / "9001_directory.py" / "inside.py").write_bytes(b"revision = 'x'\n")
    (tree / "9002_dangling.py").symlink_to(tree / "nowhere-at-all")
    assert verify_tree(MANIFEST, tree) == []


def test_a_directory_in_the_place_of_a_shipped_migration_is_reported_missing(tree: Path) -> None:
    name = _file_for("0042")
    (tree / name).unlink()
    (tree / name).mkdir()
    (problem,) = verify_tree(MANIFEST, tree)
    assert problem.startswith(f"{name}: shipped in caliber-v0.1.0a1 but missing")


def test_a_hostile_down_revision_literal_is_a_problem_not_a_traceback(tree: Path) -> None:
    (tree / "9001_weird.py").write_bytes(b'revision = "9001"\ndown_revision = {[1]}\n')
    (problem,) = verify_tree(MANIFEST, tree)
    assert problem.startswith("9001_weird.py: not in the alpha manifest and unreadable")
    assert "not a usable literal" in problem


def test_an_unreadable_file_is_a_problem_not_a_traceback(
    tree: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    shipped, extra = _file_for("0042"), "9001_extra.py"
    (tree / extra).write_bytes(_migration_source("9001", HEAD))
    real_read = mc._read

    def flaky(path: Path) -> bytes:
        if path.name in {shipped, extra}:
            raise PermissionError(13, "Permission denied", path.name)
        return real_read(path)

    monkeypatch.setattr(mc, "_read", flaky)
    problems = verify_tree(MANIFEST, tree)
    assert sorted(p.split(":")[0] for p in problems) == sorted([shipped, extra])
    assert all(
        "cannot be read" in p for p in problems
    )  # and the shipped file is not also "missing"
