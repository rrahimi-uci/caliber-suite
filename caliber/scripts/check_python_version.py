#!/usr/bin/env python3
"""Refuse to build a development venv on an unsupported interpreter.

``python3 -m venv`` takes whatever ``python3`` resolves to, which is how a
development environment silently lands outside the range this package declares
(``requires-python = ">=3.10,<3.13"``). The contributor then gets green local
runs on an interpreter CI never exercises — confidence without coverage, which
is worse than a red run because nothing prompts them to look.

pip does eventually refuse the editable install, but only after creating the
venv and resolving dependencies, and its message points at the constraint rather
than at what to do about it.

A separate file rather than an inline ``-c`` one-liner on purpose: the first
version of this check was written inline as
``sys.stderr.write(...) or sys.exit(1)``, and because ``write`` returns a
character count the ``or`` short-circuited and the process exited 0. It printed
a rejection and allowed the build. A gate that does not gate is the failure this
script exists to prevent, so it is written to be read rather than to be short.
"""

from __future__ import annotations

import sys

MINIMUM = (3, 10)
BELOW = (3, 13)


def main() -> int:
    version = sys.version_info[:2]
    if MINIMUM <= version < BELOW:
        return 0

    supported = f">={MINIMUM[0]}.{MINIMUM[1]},<{BELOW[0]}.{BELOW[1]}"
    sys.stderr.write(
        f"\n  {sys.executable}\n"
        f"  is Python {version[0]}.{version[1]}, outside the supported range "
        f"{supported}\n"
        f"  that caliber-suite declares and CI verifies.\n\n"
        f"  Install Python 3.12 and retry, or choose one explicitly:\n\n"
        f"      make install CALIBER_PYTHON=python3.12\n\n"
    )
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
