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

from caliber.db.migrate import VERSION_TABLE
from tests.migration_checksums import (
    ALPHA_TAG,
    MIGRATIONS_PATH,
    RECOVERY_HINT,
    REPO_ROOT,
    REQUIRE_TAG_ENV,
    VERSIONS_DIR,
    TagUnavailableError,
    head_revision,
    load_manifest,
    require_tag_from_env,
    validate_manifest,
    verify_manifest_against_tag,
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


def test_alpha_databases_are_versioned_in_the_table_the_code_still_reads() -> None:
    """If the version table were renamed, an alpha database would look unversioned and be migrated from zero."""
    assert load_manifest()["version_table"] == VERSION_TABLE


def test_line_endings_of_shipped_migrations_are_never_rewritten() -> None:
    """A Windows ``core.autocrlf`` checkout would otherwise change every byte the checksums cover."""
    result = subprocess.run(
        ["git", "check-attr", "text", "--", f"{MIGRATIONS_PATH}/0001_initial.py"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=True,
    )
    assert result.stdout.strip().endswith("text: unset"), result.stdout


def test_codeowners_routes_the_frozen_migrations_and_their_manifest_to_the_owner() -> None:
    """The checksum test cannot stop one change from editing a migration and its manifest entry; review can."""
    owned = {
        line.split()[0]: line.split()[1:]
        for line in (REPO_ROOT / ".github" / "CODEOWNERS").read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.startswith("#")
    }
    for pattern in (
        f"/{MIGRATIONS_PATH}/",
        "/caliber/tests/fixtures/alpha_0_1_0a1_migration_checksums.json",
    ):
        assert owned.get(pattern), f"CODEOWNERS has no owner for {pattern}"


def test_the_manifest_matches_the_release_tag() -> None:
    """The fixture is what the tag shipped, regenerated independently of the working tree.

    ``test_shipped_migrations_unchanged`` alone passes a change that edits a migration *and* its manifest
    entry; this is the check that does not, because it reads the tag's own objects. A checkout without the
    tag (the full-suite CI job, a shallow clone) skips it. The CI job that fetches the tag sets
    ``CALIBER_REQUIRE_ALPHA_TAG``, so there a missing tag fails instead of skipping.
    """
    manifest = load_manifest()
    try:
        problems = verify_manifest_against_tag(manifest, REPO_ROOT)
    except TagUnavailableError as exc:
        if require_tag_from_env():
            pytest.fail(f"{REQUIRE_TAG_ENV} is set but the tag is unavailable: {exc}")
        pytest.skip(str(exc))
    assert not problems, "\n".join([*problems, "", RECOVERY_HINT])
