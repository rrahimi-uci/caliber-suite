"""Named fault barriers: places where a scenario test can make a process die on purpose.

A recovery guarantee ("a worker killed after the downstream commit and before the receipt is stored does
not repeat the effect") can only be shown by killing a real process at that exact point. Production code
calls :func:`barrier` there; the call does nothing unless the process was started with the barrier's name
in ``CALIBER_FAULT_BARRIERS`` (a comma list), in which case the process exits at once with status 137,
the status of a SIGKILL, without flushing buffers or running cleanup, as a crash would.

The reference profile refuses to start while ``CALIBER_FAULT_BARRIERS`` is set (``profile.py``), so a
production deployment cannot carry an armed barrier by accident.
"""

from __future__ import annotations

import os
from collections.abc import Mapping

ENV_VAR = "CALIBER_FAULT_BARRIERS"
#: The status a killed process reports; the harness asserts on it.
EXIT_CODE = 137
#: The barriers the platform defines. Later work places ``barrier(<name>)`` calls: the dispatcher and the
#: effect ledger (#522), the evaluation claim (#528) and the outbox (#536).
BARRIERS = (
    "before_dispatch",
    "after_downstream_commit",
    "before_receipt_persist",
    "after_outbox_commit",
    "after_evaluation_claim",
)


def armed_barriers(environ: Mapping[str, str] | None = None) -> frozenset[str]:
    """The barrier names listed in ``CALIBER_FAULT_BARRIERS``, empty when it is unset or blank."""
    raw = (os.environ if environ is None else environ).get(ENV_VAR, "")
    return frozenset(name.strip() for name in raw.split(",") if name.strip())


def barrier(name: str) -> None:
    """Return at once, unless ``name`` is armed: then exit the process with :data:`EXIT_CODE`."""
    if name in armed_barriers():
        os._exit(EXIT_CODE)
