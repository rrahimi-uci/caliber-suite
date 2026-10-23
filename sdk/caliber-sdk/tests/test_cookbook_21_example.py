"""Cookbook 21's runnable example: unit tests for its own conventions.

The lab's business logic (snapshot idempotency, the diff, the review state machine, the
no-executor boundary) lives in CALIBER and is pinned by
``caliber/tests/test_cookbook_21_workspace_source_to_release_lab.py`` against the real service
code. This file guards the script's own conventions: the digest constants its release requests
use, and the name agreement between ``run()`` and ``cleanup()`` that keeps ``--cleanup`` able to
find what a run created. The lab's README captures a verified, real run of ``run()`` end to end
against a live CALIBER server.
"""

from __future__ import annotations

import inspect
import re

from examples.cookbooks import cookbook_21_workspace_source_to_release as lab

HEX64 = re.compile(r"^[0-9a-f]{64}$")


def test_the_digest_constants_follow_the_documented_convention() -> None:
    for digest in (lab.ENV_SHA, lab.DEPS_SHA, lab.POLICY_SHA):
        assert HEX64.fullmatch(digest)
    # Three distinct addresses: the release request names three different artifacts.
    assert len({lab.ENV_SHA, lab.DEPS_SHA, lab.POLICY_SHA}) == 3


def test_cleanup_archives_the_workspace_run_creates() -> None:
    """One name, two functions: cleanup must look for exactly what run() creates."""
    run_source = inspect.getsource(lab.run)
    cleanup_source = inspect.getsource(lab.cleanup)
    name = 'f"release-train{suffix}"'
    assert name in run_source
    assert name in cleanup_source


def test_the_lab_never_waits_on_the_evaluation() -> None:
    """wait_for_evaluation would sit out its full timeout on an alpha server (no executor);
    the script must assert the queued state instead of waiting for a terminal one."""
    assert "wait_for_evaluation" not in inspect.getsource(lab)
