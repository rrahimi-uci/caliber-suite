#!/usr/bin/env bash
#
# verify-restore.sh — run the deterministic Beta restore smoke.
#
# The test takes a SQLite backup after an external mutation is committed but before
# CALIBER records its receipt, restores the exact database artifact, and reconciles
# by the downstream key. It proves restored pins/checksums/artifact IDs survive and
# that recovery never resends the external write.
#
# Usage:
#   scripts/verify-restore.sh [--python PYTHON]
#
# Exit status is non-zero for any failed assertion. PostgreSQL migration parity and
# the complete EGI matrix remain separate CI gates; this smoke deliberately stays
# offline and uses the repository's deterministic downstream fixture.

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PYTHON="${PYTHON:-$REPO_ROOT/caliber/.venv/bin/python}"
while [[ $# -gt 0 ]]; do
  case "$1" in
    --python) PYTHON="${2:?--python needs an interpreter}"; shift 2 ;;
    -h|--help) awk 'NR == 1 { next } /^#/ { sub(/^# ?/, ""); print; next } { exit }' "$0"; exit 0 ;;
    *) echo "verify-restore: unknown argument: $1" >&2; exit 2 ;;
  esac
done

[[ -x "$PYTHON" ]] || { echo "verify-restore: Python interpreter not found: $PYTHON" >&2; exit 1; }
cd "$REPO_ROOT"
PYTHONPATH="$REPO_ROOT/caliber/src:$REPO_ROOT/caliber" \
  "$PYTHON" -m pytest \
  caliber/tests/egi_scenarios/test_restore_smoke.py::test_restore_mid_scenario_completes_once \
  --cov=caliber --cov-branch --cov-report=term-missing -q
