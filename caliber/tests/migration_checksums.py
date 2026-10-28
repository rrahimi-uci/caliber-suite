"""Checksums of the migrations that shipped in ``caliber-v0.1.0a1``, and the checks that keep them frozen.

Not collected by pytest (no ``test_`` prefix). ``tests/test_upgrade_from_alpha.py`` and
``tests/test_migration_checksums.py`` use it; the alpha-upgrade tests that follow (seed a database at the
alpha head, upgrade it to head) read the alpha head and the version table from the same manifest.

Why this exists
---------------
An operator running the alpha has a database at the alpha's head revision. Everything the beta does to that
database is "run the migrations after that head". That is only safe while the migrations *up to* that head are
exactly the ones the alpha ran: an edited, renamed, removed or inserted historical migration would leave a
fresh install and an upgraded alpha install with different schemas, and neither would fail loudly.

The manifest (``tests/fixtures/alpha_0_1_0a1_migration_checksums.json``) is generated from the git tag, not
from the working tree, so it records what the alpha *shipped* even if the tree has since been edited. The
alpha head is derived from the migration files (the one revision nothing revises), never written in by hand.
Migrations added after the alpha do not change the manifest: it is frozen, and a new migration only has to
descend from the alpha head.

Two checks, because each one alone can be defeated:

* ``verify`` (``test_shipped_migrations_unchanged``) compares the working tree with the manifest. It needs
  no git history, so it runs everywhere and never skips. A change that edits a migration *and* its manifest
  entry passes it.
* ``generate --check`` (``test_the_manifest_matches_the_release_tag``) regenerates the manifest from the tag and
  compares. That closes the hole above, but needs the tag, which a CI checkout does not have. The backend-core
  CI job fetches it and sets ``CALIBER_REQUIRE_ALPHA_TAG=1`` so the test fails there rather than skipping.
  ``.github/CODEOWNERS`` additionally routes any change to the migrations or the manifest to the owner.

Commands (from the repository root)::

    python caliber/tests/migration_checksums.py generate            # rewrite the fixture from the tag
    python caliber/tests/migration_checksums.py generate --check    # exit 1 if the fixture differs from the tag
    python caliber/tests/migration_checksums.py verify              # exit 1 if the working tree broke the manifest

``generate`` needs the tag locally (``git fetch --tags``). It only ever needs re-running if the fixture
itself is wrong; it must never be re-run to make a changed migration pass.

Seeding an alpha database (B16-Upgrade.2/.3)
--------------------------------------------
Create it by running ``alembic upgrade <alpha_head>`` through the working-tree ``versions/`` directory, with the
manifest's ``version_table`` as the alembic version table (the table ``caliber.db.migrate`` and
``db/migrations/env.py`` use). That reproduces the alpha schema only because those files are frozen, which the
never-skipping ``verify`` check enforces. Do not read migrations from the tag in CI: it is not in the checkout.
"""

from __future__ import annotations

import argparse
import ast
import hashlib
import json
import os
import subprocess
import sys
from collections.abc import Mapping
from pathlib import Path
from typing import Any, TextIO

ALPHA_TAG = "caliber-v0.1.0a1"
SCHEMA_VERSION = 1
MIGRATIONS_PATH = "caliber/src/caliber/db/migrations/versions"
MIGRATE_MODULE_PATH = "caliber/src/caliber/db/migrate.py"
#: Set to a non-empty value by the CI job that fetches the tag: the tag check then fails instead of skipping.
REQUIRE_TAG_ENV = "CALIBER_REQUIRE_ALPHA_TAG"

REPO_ROOT = Path(__file__).resolve().parents[2]
VERSIONS_DIR = REPO_ROOT / MIGRATIONS_PATH
MANIFEST_PATH = (
    Path(__file__).resolve().parent / "fixtures" / "alpha_0_1_0a1_migration_checksums.json"
)

#: Printed after the per-file problems. Each problem also carries its own kind-specific recovery.
RECOVERY_HINT = (
    f"A migration that shipped in {ALPHA_TAG} must never change: databases created by the alpha have "
    "already run it. A new migration needs no manifest update. Regenerate the manifest "
    "(`python caliber/tests/migration_checksums.py generate`, from the repository root, with the tag "
    "fetched) only if the fixture itself was wrong."
)

#: What alembic itself skips in a versions directory: ``(?!\.\#|__init__)(.*\.py)$``.
_IGNORED_PREFIXES = (".#", "__init__")

# Errors ``ast`` raises for hostile or odd source, none of which may become a traceback.
_PARSE_ERRORS = (SyntaxError, ValueError, TypeError, RecursionError, MemoryError)


class ManifestError(RuntimeError):
    """The manifest cannot be generated or is not a valid description of a linear migration history."""


class TagUnavailableError(ManifestError):
    """Neither the release tag nor the commit the manifest recorded is in this checkout."""


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def is_migration_name(name: str) -> bool:
    """Whether alembic would treat a file of this name in ``versions/`` as a migration."""
    return name.endswith(".py") and not name.startswith(_IGNORED_PREFIXES)


def _top_level_literals(source: bytes, name: str, wanted: tuple[str, ...]) -> dict[str, Any]:
    """The literal values of the module-level assignments to the ``wanted`` names."""
    try:
        tree = ast.parse(source, filename=name)
    except _PARSE_ERRORS as exc:
        raise ManifestError(f"{name}: not valid Python ({exc})") from exc
    found: dict[str, Any] = {}
    for node in tree.body:
        if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            target, value = node.target.id, node.value
        elif (
            isinstance(node, ast.Assign)
            and len(node.targets) == 1
            and isinstance(node.targets[0], ast.Name)
        ):
            target, value = node.targets[0].id, node.value
        else:
            continue
        if target in wanted and value is not None:
            try:
                found[target] = ast.literal_eval(value)
            except _PARSE_ERRORS as exc:
                raise ManifestError(f"{name}: {target} is not a usable literal ({exc})") from exc
    return found


def parse_revision(source: bytes, name: str) -> tuple[str, tuple[str, ...]]:
    """Read ``revision`` and ``down_revision`` from a migration's source without importing it."""
    found = _top_level_literals(source, name, ("revision", "down_revision"))
    revision = found.get("revision")
    if not isinstance(revision, str) or not revision:
        raise ManifestError(f"{name}: no string `revision` assignment")
    if "down_revision" not in found:
        raise ManifestError(f"{name}: no `down_revision` assignment")
    down = found["down_revision"]
    if down is None:
        return revision, ()
    if isinstance(down, str):
        return revision, (down,)
    if isinstance(down, (tuple, list)) and all(isinstance(d, str) for d in down):
        return revision, tuple(down)
    raise ManifestError(f"{name}: unsupported down_revision {down!r}")


def _linear_chain(entries: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """The entries ordered root to head; raises unless they form exactly one chain."""
    by_revision: dict[str, dict[str, Any]] = {}
    for entry in entries:
        if entry["revision"] in by_revision:
            raise ManifestError(f"revision {entry['revision']!r} is defined by two files")
        by_revision[entry["revision"]] = entry
    children: dict[str, list[str]] = {}
    roots: list[str] = []
    for entry in entries:
        downs = entry["down_revisions"]
        if len(downs) > 1:
            raise ManifestError(
                f"{entry['file']} merges {downs}; the alpha history is a single chain"
            )
        if not downs:
            roots.append(entry["revision"])
            continue
        if downs[0] not in by_revision:
            raise ManifestError(f"{entry['file']} revises {downs[0]!r}, which no file defines")
        children.setdefault(downs[0], []).append(entry["revision"])
    if len(roots) != 1:
        raise ManifestError(f"expected exactly one root revision, found {sorted(roots)}")
    forked = sorted(r for r, kids in children.items() if len(kids) > 1)
    if forked:
        raise ManifestError(f"history forks after {forked}; the alpha history is a single chain")
    ordered: list[dict[str, Any]] = []
    cursor: str | None = roots[0]
    while cursor is not None:
        ordered.append(by_revision[cursor])
        kids = children.get(cursor)
        cursor = kids[0] if kids else None
    if len(ordered) != len(entries):  # a cycle never reaches the root, so this is the cycle guard
        raise ManifestError("the revisions do not form one chain from the root")
    return ordered


def head_revision(manifest: dict[str, Any]) -> str:
    """The alpha head, derived: the one revision no other migration in the manifest revises."""
    entries = manifest["migrations"]
    revised = {d for e in entries for d in e["down_revisions"]}
    heads = [e["revision"] for e in entries if e["revision"] not in revised]
    if len(heads) != 1:
        raise ManifestError(f"expected exactly one head revision, found {sorted(heads)}")
    return str(heads[0])


def _git(repo_root: Path, *args: str) -> bytes:
    try:
        completed = subprocess.run(["git", *args], cwd=repo_root, capture_output=True, check=True)
    except subprocess.CalledProcessError as exc:
        detail = exc.stderr.decode("utf-8", "replace").strip()
        raise ManifestError(f"`git {' '.join(args)}` failed: {detail}") from exc
    except OSError as exc:
        raise ManifestError(f"`git {' '.join(args)}` could not run: {exc}") from exc
    return completed.stdout


def _resolve_commit(repo_root: Path, ref: str) -> str | None:
    """The commit ``ref`` (a tag, a branch or a commit id) names in this checkout, or ``None``."""
    try:
        out = _git(repo_root, "rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}")
    except ManifestError:
        return None
    return out.decode().strip() or None


def generate_manifest(
    repo_root: Path = REPO_ROOT, tag: str = ALPHA_TAG, ref: str | None = None
) -> dict[str, Any]:
    """Build the manifest from the migration files recorded in a commit (not from the working tree).

    ``tag`` is the name the manifest records; ``ref`` is what is read (default: the tag itself). A commit id
    as ``ref`` lets a checkout that has the commit but no tag ref still be checked.
    """
    commit = _git(repo_root, "rev-parse", "--verify", f"{ref or tag}^{{commit}}").decode().strip()
    listing = _git(repo_root, "ls-tree", "-r", "-z", commit, "--", MIGRATIONS_PATH)
    entries: list[dict[str, Any]] = []
    for record in filter(None, listing.split(b"\0")):
        meta, _, path = record.decode().partition("\t")
        _mode, kind, blob = meta.split()
        name = path.rsplit("/", 1)[-1]
        if kind != "blob" or not is_migration_name(name):
            continue
        source = _git(repo_root, "cat-file", "blob", blob)  # raw bytes: no EOL or filter conversion
        revision, downs = parse_revision(source, name)
        entries.append(
            {
                "file": name,
                "revision": revision,
                "down_revisions": list(downs),
                "sha256": sha256_hex(source),
            }
        )
    if not entries:
        raise ManifestError(f"{tag} ({commit}) contains no migrations under {MIGRATIONS_PATH}")
    chain = _linear_chain(entries)
    migrate_source = _git(repo_root, "show", f"{commit}:{MIGRATE_MODULE_PATH}")
    version_table = _top_level_literals(
        migrate_source, MIGRATE_MODULE_PATH, ("VERSION_TABLE",)
    ).get("VERSION_TABLE")
    if not isinstance(version_table, str) or not version_table:
        raise ManifestError(f"{MIGRATE_MODULE_PATH} at {commit} has no string VERSION_TABLE")
    manifest: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "release_tag": tag,
        "tag_commit": commit,
        "migrations_path": MIGRATIONS_PATH,
        "version_table": version_table,
        "algorithm": "sha256",
        "generated_by": "python caliber/tests/migration_checksums.py generate (from the repository root)",
        "base_revision": chain[0]["revision"],
        "head_revision": chain[-1]["revision"],
        "migration_count": len(chain),
        "migrations": chain,
    }
    return manifest


def dump_manifest(manifest: dict[str, Any]) -> str:
    return json.dumps(manifest, indent=2) + "\n"


def load_manifest(path: Path = MANIFEST_PATH) -> dict[str, Any]:
    data: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
    return data


def _header_problems(manifest: dict[str, Any]) -> list[str]:
    problems: list[str] = []
    if manifest.get("schema_version") != SCHEMA_VERSION:
        problems.append(
            f"schema_version is {manifest.get('schema_version')!r}, expected {SCHEMA_VERSION}"
        )
    if manifest.get("algorithm") != "sha256":
        problems.append(f"algorithm is {manifest.get('algorithm')!r}, expected 'sha256'")
    for key in ("release_tag", "tag_commit", "version_table"):
        if not isinstance(manifest.get(key), str) or not manifest[key]:
            problems.append(f"{key} is missing or not a string")
    if manifest.get("migrations_path") != MIGRATIONS_PATH:
        problems.append(f"migrations_path is {manifest.get('migrations_path')!r}")
    return problems


def validate_manifest(manifest: dict[str, Any]) -> list[str]:
    """Problems with the manifest as a document (hand edits, a truncated file)."""
    problems = _header_problems(manifest)
    entries = manifest.get("migrations")
    if not isinstance(entries, list) or not entries:
        return [*problems, "migrations is empty or not a list"]
    names = [e.get("file") for e in entries]
    if len(set(names)) != len(names):
        problems.append("a file name appears twice")
    for entry in entries:
        digest = entry.get("sha256", "")
        if len(digest) != 64 or any(c not in "0123456789abcdef" for c in digest):
            problems.append(f"{entry.get('file')}: sha256 is not 64 lowercase hex characters")
    try:
        chain = _linear_chain(entries)
        derived_head = head_revision(manifest)
    except ManifestError as exc:
        return [*problems, str(exc)]
    if chain[-1]["revision"] != derived_head or manifest.get("head_revision") != derived_head:
        problems.append(
            f"head_revision is {manifest.get('head_revision')!r} but the files' own head is {derived_head!r}"
        )
    if manifest.get("base_revision") != chain[0]["revision"]:
        problems.append("base_revision does not match the root revision of the files")
    if manifest.get("migration_count") != len(entries):
        problems.append(
            f"migration_count is {manifest.get('migration_count')} for {len(entries)} files"
        )
    if [e["revision"] for e in entries] != [e["revision"] for e in chain]:
        problems.append("migrations are not listed in chain order, root first")
    return problems


def _ancestors(revision: str, downs_of: dict[str, tuple[str, ...]]) -> set[str]:
    seen: set[str] = set()
    pending = [revision]
    while pending:
        current = pending.pop()
        for parent in downs_of.get(current, ()):
            if parent not in seen:
                seen.add(parent)
                pending.append(parent)
    return seen


def _read(path: Path) -> bytes:
    return path.read_bytes()


def _restore_hint(manifest: dict[str, Any], name: str) -> str:
    path = f"{MIGRATIONS_PATH}/{name}"
    return (
        f"Restore it from the shipped commit (`git show {manifest['tag_commit']}:{path} > {path}`, "
        "from the repository root) and put the change in a new migration that revises the current head."
    )


def _reparent_hint() -> str:
    return (
        "A migration added after the alpha must revise the current head (`alembic heads`), "
        "never a shipped revision: re-parent it onto the head."
    )


def _read_migration_files(versions_dir: Path) -> tuple[dict[str, bytes], list[str]]:
    """The bytes of every migration file in the directory, and a problem for each one that cannot be read."""
    contents: dict[str, bytes] = {}
    problems: list[str] = []
    for path in sorted(versions_dir.iterdir()):
        # Only regular files that alembic itself would load: a directory named x.py, a dangling symlink or an
        # editor's `.#lock` file is not a migration.
        if not is_migration_name(path.name) or not path.is_file():
            continue
        try:
            contents[path.name] = _read(path)
        except OSError as exc:
            problems.append(f"{path.name}: cannot be read ({exc})")
    return contents, problems


def verify_tree(manifest: dict[str, Any], versions_dir: Path = VERSIONS_DIR) -> list[str]:
    """Every way the migration files in ``versions_dir`` break the frozen alpha history; empty when intact.

    Never raises for what is in the directory: an unreadable file, a hostile ``down_revision`` literal or a
    stray entry is a problem in the list, not a traceback.
    """
    tag = manifest["release_tag"]
    expected = {e["file"]: e for e in manifest["migrations"]}
    contents, problems = _read_migration_files(versions_dir)
    unreadable = {p.split(":", 1)[0] for p in problems}
    by_digest = {sha256_hex(data): n for n, data in contents.items() if n not in expected}
    renamed_to: set[str] = set()

    for name, entry in expected.items():
        if name in unreadable:
            continue
        data = contents.get(name)
        if data is None:
            moved = by_digest.get(entry["sha256"])
            if moved:
                renamed_to.add(moved)
            problems.append(
                f"{name}: shipped in {tag} but missing"
                + (f" (the same bytes are now {moved}: a rename)" if moved else "")
                + f". {_restore_hint(manifest, name)}"
            )
            continue
        digest = sha256_hex(data)
        if digest != entry["sha256"]:
            problems.append(
                f"{name}: changed since {tag} (sha256 {digest}, shipped {entry['sha256']}). "
                f"{_restore_hint(manifest, name)}"
            )

    alpha_head = head_revision(manifest)
    downs_of = {e["revision"]: tuple(e["down_revisions"]) for e in manifest["migrations"]}
    extras: dict[str, tuple[str, tuple[str, ...]]] = {}
    for name, data in contents.items():
        if (
            name in expected or name in renamed_to
        ):  # a rename is already reported against its old name
            continue
        try:
            revision, downs = parse_revision(data, name)
        except ManifestError as exc:
            problems.append(f"{name}: not in the alpha manifest and unreadable ({exc})")
            continue
        if revision in downs_of or any(r == revision for r, _ in extras.values()):
            problems.append(
                f"{name}: revision {revision!r} is already defined by another migration file"
            )
            continue
        extras[name] = (revision, downs)
        downs_of[revision] = downs
    # Two new migrations that both revise the alpha head (siblings) descend from it, so they pass here; the
    # single-head test in test_upgrade_from_alpha.py and alembic itself catch that fork.
    for name, (revision, _) in sorted(extras.items()):
        if alpha_head not in _ancestors(revision, downs_of):
            problems.append(
                f"{name}: revision {revision!r} is not in the {tag} manifest and does not descend "
                f"from its head {alpha_head!r}, so it inserts into or branches off shipped history. "
                f"{_reparent_hint()}"
            )
    return problems


def _differences(manifest: dict[str, Any], regenerated: dict[str, Any], source: str) -> list[str]:
    """How the manifest differs from what ``source`` (the tag's commit) actually shipped."""
    problems: list[str] = []
    for key in sorted((set(manifest) | set(regenerated)) - {"migrations"}):
        if manifest.get(key) != regenerated.get(key):
            problems.append(
                f"{key}: the manifest says {manifest.get(key)!r} but {source} has {regenerated.get(key)!r}"
            )
    recorded = {e.get("file"): e for e in manifest.get("migrations", [])}
    actual = {e["file"]: e for e in regenerated["migrations"]}
    for name in sorted(set(recorded) | set(actual), key=str):
        if name not in recorded:
            problems.append(f"{name}: shipped in {source} but absent from the manifest")
        elif name not in actual:
            problems.append(f"{name}: in the manifest but not shipped in {source}")
        elif recorded[name] != actual[name]:
            problems.append(
                f"{name}: the manifest records {recorded[name]} but {source} shipped {actual[name]}"
            )
    return problems


def verify_manifest_against_tag(manifest: dict[str, Any], repo_root: Path = REPO_ROOT) -> list[str]:
    """Problems found comparing the manifest with the migrations the release tag shipped; empty when equal.

    The tag is read through the commit the manifest recorded when this checkout has it (so a checkout with
    the commit but no tag ref still verifies); if the tag ref also resolves it must point at that commit.
    Raises :class:`TagUnavailableError` when neither exists, so the caller decides between skip and fail.
    """
    tag, recorded_commit = manifest["release_tag"], manifest["tag_commit"]
    tag_commit = _resolve_commit(repo_root, tag)
    have_recorded = _resolve_commit(repo_root, recorded_commit) is not None
    if tag_commit is None and not have_recorded:
        raise TagUnavailableError(
            f"neither {tag} nor its recorded commit {recorded_commit} is in this checkout "
            f"(`git fetch --no-tags --depth=1 origin tag {tag}`)"
        )
    problems: list[str] = []
    if tag_commit is not None and tag_commit != recorded_commit:
        problems.append(
            f"{tag} now points at {tag_commit} but the manifest was generated from {recorded_commit}: "
            "the tag was moved"
        )
    source = recorded_commit if have_recorded else str(tag_commit)
    regenerated = generate_manifest(repo_root, tag, ref=source)
    return [*problems, *_differences(manifest, regenerated, source)]


def require_tag_from_env(environ: Mapping[str, str] | None = None) -> bool:
    return bool((environ if environ is not None else os.environ).get(REQUIRE_TAG_ENV))


def _say(message: str, stream: TextIO | None = None) -> None:
    (stream or sys.stdout).write(message + "\n")


def _check_fixture(path: Path) -> list[str]:
    """``generate --check``: the fixture at ``path`` against the tag it names."""
    if not path.exists():
        return [f"{path} does not exist"]
    try:
        manifest = load_manifest(path)
    except ValueError as exc:
        return [f"{path} is not valid JSON ({exc})"]
    return validate_manifest(manifest) or verify_manifest_against_tag(manifest, REPO_ROOT)


def _cli(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="migration_checksums.py", description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    generate = sub.add_parser("generate", help="write the manifest from the release tag")
    generate.add_argument("--tag", default=ALPHA_TAG)
    generate.add_argument(
        "--check",
        action="store_true",
        help="compare the fixture with the tag it names (read from its recorded commit); write nothing",
    )
    generate.add_argument("--output", type=Path, default=MANIFEST_PATH)
    sub.add_parser("verify", help="check the working tree against the manifest")
    args = parser.parse_args(argv)

    try:
        if args.command == "generate" and args.check:
            problems = _check_fixture(args.output)
            if problems:
                for problem in problems:
                    _say(problem, sys.stderr)
                _say(f"{args.output} does not match the release tag", sys.stderr)
                _say(RECOVERY_HINT, sys.stderr)
                return 1
            _say(f"{args.output} matches the release tag")
            return 0
        if args.command == "generate":
            args.output.write_text(
                dump_manifest(generate_manifest(REPO_ROOT, args.tag)), encoding="utf-8"
            )
            _say(f"wrote {args.output}")
            return 0
        problems = verify_tree(load_manifest(MANIFEST_PATH), VERSIONS_DIR)
    except ManifestError as exc:
        _say(f"error: {exc}", sys.stderr)
        return 1
    for problem in problems:
        _say(problem, sys.stderr)
    if problems:
        _say(RECOVERY_HINT, sys.stderr)
        return 1
    _say("shipped migrations unchanged")
    return 0


if __name__ == "__main__":
    raise SystemExit(_cli())
