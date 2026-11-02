#!/usr/bin/env python3
# ruff: noqa: T201 - a command-line tool: printing is its output
"""Mutation check for the reference-profile attack pack (B12.4c, #770).

A test that cannot fail proves nothing. This script breaks the refusal code on purpose, one mutant at a time, and runs the
attack pack (``tests/egi_scenarios/test_v14_reference_profile.py``) against each broken copy. A mutant is **killed** when the
pack fails, **survived** when it passes (a hole in the pack, or an equivalent mutant: a change that alters no behaviour a caller
can observe), and **broken** when the pack could not run (a mutant that does not import is not evidence).

Each mutant is a source edit of a throwaway copy of ``src/caliber`` (the working tree is never touched), and the pack runs
against that copy through ``PYTHONPATH``, including the CALIBER processes it starts.

    python scripts/attack_pack_mutants.py --check            # every edit's anchor exists exactly once (offline, instant)
    python scripts/attack_pack_mutants.py --list
    python scripts/attack_pack_mutants.py [--only NAME ...]  # needs CALIBER_TEST_POSTGRES_URL; minutes per surviving mutant

The exit status is non-zero if any mutant survived or was broken.
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path

CALIBER_DIR = Path(__file__).resolve().parents[1]
SRC = CALIBER_DIR / "src" / "caliber"
PACK = "tests/egi_scenarios/test_v14_reference_profile.py"


@dataclass(frozen=True)
class Edit:
    path: str  # relative to src/caliber
    old: str
    new: str


@dataclass(frozen=True)
class Mutant:
    name: str
    why: str  # what a refusal that broke this way would let an attacker do
    edits: tuple[Edit, ...]


def _e(path: str, old: str, new: str) -> Edit:
    return Edit(path, old, new)


MUTANTS: tuple[Mutant, ...] = (
    Mutant(
        "python_code_is_classified_inert",
        "the compiler's registry no longer marks python_code as executing code",
        (
            _e(
                "workflows/code_policy.py",
                "    why = CODE_NODE_KINDS.get(node_type)\n    if why is not None:",
                "    if node_type is NodeType.PYTHON_CODE:\n        return None\n"
                "    why = CODE_NODE_KINDS.get(node_type)\n    if why is not None:",
            ),
        ),
    ),
    Mutant(
        "external_app_is_classified_inert",
        "the compiler's registry no longer marks external_app as executing code",
        (
            _e(
                "workflows/code_policy.py",
                "    why = CODE_NODE_KINDS.get(node_type)\n    if why is not None:",
                "    if node_type is NodeType.EXTERNAL_APP:\n        return None\n"
                "    why = CODE_NODE_KINDS.get(node_type)\n    if why is not None:",
            ),
        ),
    ),
    Mutant(
        "policy_is_never_reference_from_a_configuration",
        "a configuration that says reference no longer yields a refusing policy",
        (
            _e(
                "workflows/code_policy.py",
                '            reference=getattr(config, "profile", "") == "reference",',
                "            reference=False,",
            ),
        ),
    ),
    Mutant(
        "app_does_not_bind_the_policy",
        "create_app no longer binds the process-wide policy the routes and the promoter read",
        (
            _e(
                "server.py",
                "    bind_code_policy(CodePolicy.from_config(resolved))",
                "    bind_code_policy(CodePolicy())",
            ),
        ),
    ),
    Mutant(
        "every_registered_python_tool_is_shipped",
        "an administrator's Python module is admitted as if CALIBER shipped it",
        (
            _e(
                "effects/refusals.py",
                "    return BUILTIN_TOOL_MODULE_BY_NAME.get(\n"
                "        callable_name\n"
                "    ) == module_path and not inventory_refuses_tool_name(callable_name)",
                "    return True",
            ),
        ),
    ),
    Mutant(
        "validation_does_not_check_code",
        "validate stops reporting code, so an author is told the workflow is clean",
        (
            _e(
                "workflows/validation.py",
                "    for refusal in manifest_refusals(manifest, resolver, code_policy):\n"
                "        report.add(refusal.code, refusal.path, refusal.message)",
                "    return",
            ),
        ),
    ),
    Mutant(
        "promotion_does_not_preflight_code",
        "an alias can be moved onto a version that holds code",
        (
            _e(
                "workflows/promoter.py",
                "    if not resolve_code_policy(policy).reference:\n",
                "    if True:\n",
            ),
        ),
    ),
    Mutant(
        "no_refusal_at_validation_or_compile",
        "validation and compile both stop refusing, leaving only promotion and the runtime entry",
        (
            _e(
                "workflows/validation.py",
                "    for refusal in manifest_refusals(manifest, resolver, code_policy):\n"
                "        report.add(refusal.code, refusal.path, refusal.message)",
                "    return",
            ),
            _e(
                "workflows/compiler.py",
                "    if not refusals:\n        return\n    raise CompileError(\n        summarize(refusals),",
                "    return\n    raise CompileError(\n        summarize(refusals),",
            ),
        ),
    ),
    Mutant(
        "inventory_refusals_never_fire",
        "every protected-path refusal (exports, output_folder, tool sandbox, assistant) stops refusing",
        (
            _e(
                "effects/refusals.py",
                '    return getattr(config, "profile", "") == "reference"',
                "    return False",
            ),
        ),
    ),
    Mutant(
        "direct_plugin_registration_is_not_admitted",
        "OptimizerRegistry.register stops applying the allowlist to a plugin handed to it",
        (
            _e(
                "extensibility/entrypoints.py",
                "        _reference_profile_active()\n"
                '        and _normalize(distribution or "") not in allowlisted_distributions()',
                "        False\n"
                '        and _normalize(distribution or "") not in allowlisted_distributions()',
            ),
        ),
    ),
    Mutant(
        "allowlist_matches_by_prefix",
        "naming a prefix of a distribution admits it",
        (
            _e(
                "extensibility/entrypoints.py",
                '        if _normalize(distribution or "") not in allowed:\n'
                "            labels.add(distribution or entry.name)",
                '        if not any(_normalize(distribution or "").startswith(item) for item in allowed):\n'
                "            labels.add(distribution or entry.name)",
            ),
        ),
    ),
    Mutant(
        "startup_ignores_an_unlisted_plugin",
        "a reference process starts with an installed plugin nobody trusted",
        (_e("profile.py", "    if refused_plugins:", "    if False:"),),
    ),
    Mutant(
        "startup_accepts_an_unconfined_file_root",
        "a reference process starts with CALIBER_WORKFLOW_FILE_ROOT=unconfined",
        (
            _e(
                "profile.py",
                "    if config.workflow_file_root.strip() == UNCONFINED_WORKFLOW_FILE_ROOT:",
                "    if False:",
            ),
        ),
    ),
    Mutant(
        "startup_accepts_csrf_off",
        "a reference process starts with CSRF protection disabled",
        (_e("profile.py", "    if not config.csrf_enabled:", "    if False:"),),
    ),
    Mutant(
        "file_root_does_not_confine",
        "a host-path node reads outside CALIBER_WORKFLOW_FILE_ROOT",
        (
            _e(
                "workflows/runtime.py",
                "    if resolved != root and root not in resolved.parents:",
                "    if False:",
            ),
        ),
    ),
    Mutant(
        "csrf_signature_is_not_compared",
        "any CSRF token with the right shape is accepted",
        (
            _e(
                "csrf.py",
                "        if not hmac.compare_digest(expected, signature):",
                "        if False:",
            ),
        ),
    ),
    Mutant(
        "proxy_secret_is_not_compared",
        "an X-CALIBER-User header is trusted without the proxy's secret",
        (
            _e(
                "auth.py",
                "    return hmac.compare_digest(presented, expected)",
                "    return True",
            ),
        ),
    ),
    Mutant(
        "egress_checks_only_the_first_address",
        "a name with a public and a metadata record is connected to",
        (
            _e(
                "egress.py",
                "    for address in addresses:\n        if address.lower() in resolved_policy.allowed_hosts:",
                "    for address in addresses[:1]:\n        if address.lower() in resolved_policy.allowed_hosts:",
            ),
        ),
    ),
    Mutant(
        "egress_does_not_pin_the_vetted_address",
        "the connection resolves the name again, so a rebinding answer is connected to",
        (
            _e(
                "egress.py",
                "        url=_replace_host(parts, addresses[0]),",
                "        url=parts.geturl(),",
            ),
        ),
    ),
    Mutant(
        "egress_allows_a_literal_metadata_address",
        "http://169.254.169.254/ is fetched",
        (
            _e(
                "egress.py",
                "    if direct is not None:\n        raise EgressBlockedError(_describe(lowered, lowered, direct))",
                "    if False:\n        raise EgressBlockedError(_describe(lowered, lowered, direct))",
            ),
        ),
    ),
)


def check_anchors() -> list[str]:
    """Problems with the mutant table: an edit whose anchor is missing or ambiguous, a duplicate name, a no-op edit."""
    problems: list[str] = []
    names = [m.name for m in MUTANTS]
    problems += [f"duplicate mutant name {n}" for n in set(names) if names.count(n) > 1]
    for mutant in MUTANTS:
        for edit in mutant.edits:
            text = (SRC / edit.path).read_text(encoding="utf-8")
            count = text.count(edit.old)
            if count != 1:
                problems.append(f"{mutant.name}: {edit.path} anchor found {count} times (need 1)")
            if edit.old == edit.new:
                problems.append(f"{mutant.name}: the edit changes nothing")
    return problems


def run_mutant(mutant: Mutant, pytest_args: list[str]) -> tuple[str, str]:
    """``(verdict, tail of the pack's output)`` for one mutant: ``killed``, ``survived`` or ``broken``."""
    with tempfile.TemporaryDirectory(prefix="caliber-mutant-") as scratch:
        mutated_src = Path(scratch) / "src"
        shutil.copytree(
            SRC.parent, mutated_src, ignore=shutil.ignore_patterns("__pycache__", "*.pyc")
        )
        for edit in mutant.edits:
            target = mutated_src / "caliber" / edit.path
            text = target.read_text(encoding="utf-8")
            if text.count(edit.old) != 1:
                return "broken", f"anchor not found exactly once in {edit.path}"
            target.write_text(text.replace(edit.old, edit.new), encoding="utf-8")
        env = dict(os.environ)
        env["PYTHONPATH"] = os.pathsep.join([str(mutated_src), str(CALIBER_DIR)])
        env["PYTHONDONTWRITEBYTECODE"] = "1"
        completed = subprocess.run(  # noqa: S603 - the interpreter running this script, fixed arguments
            [
                sys.executable,
                "-m",
                "pytest",
                PACK,
                "-x",
                "-q",
                "--no-cov",
                "-p",
                "no:cacheprovider",
                "-k",
                "postgresql",
                *pytest_args,
            ],
            cwd=CALIBER_DIR,
            env=env,
            capture_output=True,
            text=True,
            check=False,
        )
    tail = "\n".join(completed.stdout.splitlines()[-6:])
    if completed.returncode == 1:
        return "killed", tail
    if completed.returncode == 0:
        return "survived", tail
    return "broken", tail


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawTextHelpFormatter
    )
    parser.add_argument(
        "--check", action="store_true", help="only verify the mutant table against the source"
    )
    parser.add_argument("--list", action="store_true", help="list the mutants")
    parser.add_argument("--only", nargs="*", default=[], help="run only these mutants")
    args, pytest_args = parser.parse_known_args(argv)

    problems = check_anchors()
    if problems:
        print("\n".join(problems), file=sys.stderr)
        return 2
    if args.list:
        for mutant in MUTANTS:
            print(f"{mutant.name}: {mutant.why}")
        return 0
    if args.check:
        print(f"{len(MUTANTS)} mutants, every anchor found exactly once")
        return 0
    if not os.environ.get("CALIBER_TEST_POSTGRES_URL"):
        print(
            "CALIBER_TEST_POSTGRES_URL is required: the pack's refusals are PostgreSQL scenarios",
            file=sys.stderr,
        )
        return 2

    chosen = [m for m in MUTANTS if not args.only or m.name in args.only]
    verdicts: dict[str, str] = {}
    for mutant in chosen:
        verdict, tail = run_mutant(mutant, pytest_args)
        verdicts[mutant.name] = verdict
        print(f"{verdict.upper():9} {mutant.name}", flush=True)
        if verdict != "killed":
            print(tail, file=sys.stderr)
    killed = sum(1 for v in verdicts.values() if v == "killed")
    print(f"{killed}/{len(chosen)} mutants killed")
    return 0 if killed == len(chosen) else 1


if __name__ == "__main__":
    raise SystemExit(main())
