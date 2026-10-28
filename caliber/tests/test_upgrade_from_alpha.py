"""Upgrading a ``caliber-v0.1.0a1`` database to the current head (B16-Upgrade, #594).

This file starts with the guard the rest of the chain stands on (B16-Upgrade.1, #702): the migrations the
alpha shipped are frozen. A database an operator created with the alpha sits at the alpha's head revision;
the beta's job is to run the migrations *after* that head. If any migration at or before the head could
change, a fresh install and an upgraded alpha install would end up with different schemas and nothing would
fail. ``tests/fixtures/alpha_0_1_0a1_migration_checksums.json`` records the byte checksum of every migration
file in the tag, and the alpha head derived from those files; ``tests/migration_checksums.py`` holds the
generator and the checks (``tests/test_migration_checksums.py`` proves the checks catch each kind of edit).

Later tests in this file seed a database at the alpha head and upgrade it; they read the head from
``alpha_head()`` below, never from a literal.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest
from alembic.config import Config
from alembic.script import ScriptDirectory

from tests.migration_checksums import (
    ALPHA_TAG,
    RECOVERY_HINT,
    REPO_ROOT,
    VERSIONS_DIR,
    ManifestError,
    dump_manifest,
    generate_manifest,
    head_revision,
    load_manifest,
    validate_manifest,
    verify_tree,
)

PROJECT_ROOT = Path(__file__).resolve().parent.parent
MIGRATIONS_SCRIPT_LOCATION = PROJECT_ROOT / "src" / "caliber" / "db" / "migrations"


def alpha_head() -> str:
    """The revision a ``caliber-v0.1.0a1`` database is at, derived from the manifest's migration files."""
    return head_revision(load_manifest())


def _script_directory() -> ScriptDirectory:
    config = Config()
    config.set_main_option("script_location", str(MIGRATIONS_SCRIPT_LOCATION))
    return ScriptDirectory.from_config(config)


def test_shipped_migrations_unchanged() -> None:
    """Every migration the alpha shipped is still here, byte for byte, and nothing was slipped into its history."""
    problems = verify_tree(load_manifest(), VERSIONS_DIR)
    assert not problems, "\n".join([*problems, "", RECOVERY_HINT])


def test_the_manifest_is_a_well_formed_linear_history() -> None:
    manifest = load_manifest()
    assert validate_manifest(manifest) == []
    assert manifest["release_tag"] == ALPHA_TAG
    assert manifest["migrations_path"] == "caliber/src/caliber/db/migrations/versions"
    # The recorded head is the one the files themselves say; it is derived, so no literal is compared.
    assert manifest["head_revision"] == alpha_head()
    assert manifest["migration_count"] == len(manifest["migrations"])


def test_alembic_agrees_the_alpha_history_is_exactly_the_manifest() -> None:
    """Alembic's own view of the graph: the ancestors of the alpha head are the manifest's revisions, no more."""
    scripts = _script_directory()
    manifest = load_manifest()
    head = alpha_head()
    ancestry = {script.revision for script in scripts.walk_revisions(base="base", head=head)}
    assert ancestry == {entry["revision"] for entry in manifest["migrations"]}
    assert scripts.get_revision(head) is not None


def test_the_current_head_descends_from_the_alpha_head() -> None:
    """An alpha database can be upgraded: the one current head is reachable from the alpha head."""
    scripts = _script_directory()
    heads = scripts.get_heads()
    assert len(heads) == 1, f"the migration history has {len(heads)} heads: {heads}"
    ancestry = {script.revision for script in scripts.walk_revisions(base="base", head=heads[0])}
    assert alpha_head() in ancestry


def _tag_commit_available(commit: str) -> bool:
    result = subprocess.run(
        ["git", "cat-file", "-e", f"{commit}^{{commit}}"],
        cwd=REPO_ROOT,
        capture_output=True,
        check=False,
    )
    return result.returncode == 0


def test_the_manifest_matches_the_release_tag() -> None:
    """The fixture is what the tag shipped, regenerated independently of the working tree.

    CI checks out a single commit without tags, where this cannot run; it runs wherever the tag is fetched
    (a normal clone). The checksum test above is what CI enforces on every change.
    """
    manifest = load_manifest()
    if not _tag_commit_available(manifest["tag_commit"]):
        pytest.skip(
            f"{ALPHA_TAG} ({manifest['tag_commit']}) is not in this checkout; `git fetch --tags`"
        )
    try:
        regenerated = generate_manifest(REPO_ROOT, ALPHA_TAG)
    except ManifestError as exc:  # the tag was moved or deleted locally: say so, do not skip
        pytest.fail(f"the fixture cannot be checked against {ALPHA_TAG}: {exc}")
    assert dump_manifest(regenerated) == dump_manifest(manifest)
