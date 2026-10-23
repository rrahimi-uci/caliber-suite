#!/usr/bin/env bash
# Report installed runtime dependencies whose versions match a known advisory
# (caliber.runtime_advisories). Usage: caliber/scripts/check-runtime-advisories.sh [-h|--help]
#
# Environment:
#   VENV_DIR                              virtualenv to inspect (default: caliber/.venv, else the
#                                         repository .venv created by `make setup`)
#   CALIBER_RUNTIME_ADVISORIES_WARN_ONLY  1/true/yes: print the findings but exit 0
# Exit status: 0 clean (or warn-only), 1 flagged versions found or no virtualenv.

set -euo pipefail

case "${1:-}" in
    -h|--help) awk 'NR == 1 { next } /^#/ { sub(/^# ?/, ""); print; next } { exit }' "${BASH_SOURCE[0]}"; exit 0 ;;
    "") ;;
    *) echo "error: unknown argument: $1 (see --help)" >&2; exit 2 ;;
esac

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

# The virtualenv, relative to caliber/ unless absolute: $VENV_DIR, else caliber/.venv (`make install`
# in caliber/), else the repository .venv that `make setup` creates.
if [[ -z "${VENV_DIR:-}" ]]; then
    if [[ -x ".venv/bin/python" ]]; then VENV_DIR=".venv"
    elif [[ -x "../.venv/bin/python" ]]; then VENV_DIR="../.venv"
    else VENV_DIR=".venv"; fi
fi
if [[ ! -x "$VENV_DIR/bin/python" ]]; then
    echo "error: virtualenv not found at $VENV_DIR. Create it: 'make setup' in the repository root, or 'make install' in caliber/" >&2
    exit 1
fi

PYTHONPATH="src${PYTHONPATH:+:$PYTHONPATH}" "$VENV_DIR/bin/python" - <<'PY'
from __future__ import annotations

import os
import sys

from caliber.runtime_advisories import get_runtime_dependency_advisories

advisories = get_runtime_dependency_advisories()
if not advisories:
    print("runtime advisories: clean")
    raise SystemExit(0)

print("runtime advisories: flagged dependency versions detected", file=sys.stderr)
for advisory in advisories:
    advisory_ids = ", ".join(advisory.advisory_ids)
    print(
        f"- {advisory.package_name} {advisory.installed_version} ({advisory_ids}): "
        f"{advisory.summary} {advisory.recommended_action}",
        file=sys.stderr,
    )

if os.getenv("CALIBER_RUNTIME_ADVISORIES_WARN_ONLY", "").lower() in {"1", "true", "yes"}:
    raise SystemExit(0)
raise SystemExit(1)
PY
