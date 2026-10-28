"""Checksums of the migrations that shipped in ``caliber-v0.1.0a1``, and the checks that keep them frozen.

Not collected by pytest (no ``test_`` prefix). ``tests/test_upgrade_from_alpha.py`` and
``tests/test_migration_checksums.py`` use it; the alpha-upgrade tests that follow (seed a database at the
alpha head, upgrade it to head) read the alpha head from the same manifest.

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

Commands (from the repository root)::

    python caliber/tests/migration_checksums.py generate            # rewrite the fixture from the tag
    python caliber/tests/migration_checksums.py generate --check    # exit 1 if the fixture differs from the tag
    python caliber/tests/migration_checksums.py verify              # exit 1 if the working tree broke the manifest

``generate`` needs the tag locally (``git fetch --tags``). It only ever needs re-running if the fixture
itself is wrong; it must never be re-run to make a changed migration pass.
"""

from __future__ import annotations

import argparse
import ast
import hashlib
import json
import subprocess
import sys
from pathlib import Path
from typing import Any, TextIO

ALPHA_TAG = "caliber-v0.1.0a1"
SCHEMA_VERSION = 1
MIGRATIONS_PATH = "caliber/src/caliber/db/migrations/versions"

REPO_ROOT = Path(__file__).resolve().parents[2]
VERSIONS_DIR = REPO_ROOT / MIGRATIONS_PATH
MANIFEST_PATH = (
    Path(__file__).resolve().parent / "fixtures" / "alpha_0_1_0a1_migration_checksums.json"
)

RECOVERY_HINT = (
    f"A migration that shipped in {ALPHA_TAG} must never change: databases created by the alpha have "
    f"already run it. Restore it (`git checkout {ALPHA_TAG} -- <path>`) and put the change in a new "
    "migration that revises the current head; a new migration needs no manifest update. Regenerate the "
    "manifest (`python caliber/tests/migration_checksums.py generate`, from the repository root) only "
    "if the fixture itself was wrong."
)


class ManifestError(RuntimeError):
    """The manifest cannot be generated or is not a valid description of a linear migration history."""


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _top_level_literals(source: bytes, name: str) -> dict[str, Any]:
    """The literal values of the module-level ``revision`` / ``down_revision`` assignments."""
    try:
        tree = ast.parse(source, filename=name)
    except SyntaxError as exc:
        raise ManifestError(f"{name}: not valid Python ({exc.msg})") from exc
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
        if target in ("revision", "down_revision") and value is not None:
            try:
                found[target] = ast.literal_eval(value)
            except ValueError as exc:
                raise ManifestError(f"{name}: {target} is not a literal") from exc
    return found


def parse_revision(source: bytes, name: str) -> tuple[str, tuple[str, ...]]:
    """Read ``revision`` and ``down_revision`` from a migration's source without importing it."""
    found = _top_level_literals(source, name)
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
    return completed.stdout


def generate_manifest(repo_root: Path = REPO_ROOT, tag: str = ALPHA_TAG) -> dict[str, Any]:
    """Build the manifest from the migration files recorded in ``tag``'s commit (not from the working tree)."""
    commit = _git(repo_root, "rev-parse", "--verify", f"{tag}^{{commit}}").decode().strip()
    listing = _git(repo_root, "ls-tree", "-r", "-z", commit, "--", MIGRATIONS_PATH)
    entries: list[dict[str, Any]] = []
    for record in filter(None, listing.split(b"\0")):
        meta, _, path = record.decode().partition("\t")
        mode, kind, blob = meta.split()
        name = path.rsplit("/", 1)[-1]
        if kind != "blob" or not name.endswith(".py") or name == "__init__.py":
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
    manifest: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "release_tag": tag,
        "tag_commit": commit,
        "migrations_path": MIGRATIONS_PATH,
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


def validate_manifest(manifest: dict[str, Any]) -> list[str]:
    """Problems with the manifest as a document (hand edits, a truncated file)."""
    problems: list[str] = []
    if manifest.get("schema_version") != SCHEMA_VERSION:
        problems.append(
            f"schema_version is {manifest.get('schema_version')!r}, expected {SCHEMA_VERSION}"
        )
    if manifest.get("algorithm") != "sha256":
        problems.append(f"algorithm is {manifest.get('algorithm')!r}, expected 'sha256'")
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


def verify_tree(manifest: dict[str, Any], versions_dir: Path = VERSIONS_DIR) -> list[str]:
    """Every way the migration files in ``versions_dir`` break the frozen alpha history; empty when intact."""
    problems: list[str] = []
    expected = {e["file"]: e for e in manifest["migrations"]}
    present = {p.name: p for p in sorted(versions_dir.glob("*.py")) if p.name != "__init__.py"}
    by_digest = {sha256_hex(p.read_bytes()): n for n, p in present.items() if n not in expected}
    renamed_to: set[str] = set()

    for name, entry in expected.items():
        path = present.get(name)
        if path is None:
            moved = by_digest.get(entry["sha256"])
            if moved:
                renamed_to.add(moved)
            problems.append(
                f"{name}: shipped in {manifest['release_tag']} but missing"
                + (f" (the same bytes are now {moved}: a rename)" if moved else "")
            )
            continue
        digest = sha256_hex(path.read_bytes())
        if digest != entry["sha256"]:
            problems.append(
                f"{name}: changed since {manifest['release_tag']} "
                f"(sha256 {digest}, shipped {entry['sha256']})"
            )

    alpha_head = head_revision(manifest)
    downs_of = {e["revision"]: tuple(e["down_revisions"]) for e in manifest["migrations"]}
    extras: dict[str, tuple[str, tuple[str, ...]]] = {}
    for name, path in present.items():
        if (
            name in expected or name in renamed_to
        ):  # a rename is already reported against its old name
            continue
        try:
            revision, downs = parse_revision(path.read_bytes(), name)
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
    for name, (revision, _) in sorted(extras.items()):
        if alpha_head not in _ancestors(revision, downs_of):
            problems.append(
                f"{name}: revision {revision!r} is not in the {manifest['release_tag']} manifest and does "
                f"not descend from its head {alpha_head!r}, so it inserts into or branches off shipped history"
            )
    return problems


def _say(message: str, stream: TextIO | None = None) -> None:
    (stream or sys.stdout).write(message + "\n")


def _cli(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="migration_checksums.py", description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    generate = sub.add_parser("generate", help="write the manifest from the release tag")
    generate.add_argument("--tag", default=ALPHA_TAG)
    generate.add_argument(
        "--check", action="store_true", help="compare with the fixture; write nothing"
    )
    generate.add_argument("--output", type=Path, default=MANIFEST_PATH)
    sub.add_parser("verify", help="check the working tree against the manifest")
    args = parser.parse_args(argv)

    try:
        if args.command == "generate":
            text = dump_manifest(generate_manifest(REPO_ROOT, args.tag))
            if args.check:
                current = args.output.read_text(encoding="utf-8") if args.output.exists() else None
                if current != text:
                    _say(f"{args.output} does not match {args.tag}", sys.stderr)
                    return 1
                _say(f"{args.output} matches {args.tag}")
                return 0
            args.output.write_text(text, encoding="utf-8")
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
