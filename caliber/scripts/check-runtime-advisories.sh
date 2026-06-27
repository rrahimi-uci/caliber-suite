#!/usr/bin/env bash

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

VENV_DIR="${VENV_DIR:-.venv}"
if [[ ! -x "$VENV_DIR/bin/python" ]]; then
    echo "error: virtualenv not found at $VENV_DIR — run 'make install' first" >&2
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
