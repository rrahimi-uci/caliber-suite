#!/usr/bin/env bash
# Run the full CALIBER test suite from repo root:
#   1) backend pytest + UI unit tests (via make test-allure)
#   2) UI Playwright E2E tests
#   3) combined Allure HTML report (unless --no-allure / ALLURE=0)
#
# Test failures do NOT abort the run: every step executes, the Allure report is
# still generated (you most want it WHEN something failed), and the script exits
# non-zero at the end if any step failed.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$REPO_ROOT"

print_usage() {
  cat <<'EOF'
Usage: ./test-all.sh [--no-allure] [-h|--help]

Runs backend + frontend unit + frontend e2e tests.

Options:
  --no-allure Skip combined Allure HTML report generation
  -h, --help  Show this help

Env:
  ALLURE=0    Same as --no-allure
EOF
}

WANT_ALLURE="${ALLURE:-1}"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --no-allure)
      WANT_ALLURE=0
      shift
      ;;
    -h|--help)
      print_usage
      exit 0
      ;;
    *)
      echo "error: unknown argument '$1'" >&2
      print_usage >&2
      exit 2
      ;;
  esac
done

if ! command -v make >/dev/null 2>&1; then
  echo "error: 'make' is required but not found in PATH" >&2
  exit 1
fi
if ! command -v npm >/dev/null 2>&1; then
  echo "error: 'npm' is required but not found in PATH" >&2
  exit 1
fi

# Track failures across steps without aborting (so the report still builds and
# every suite runs). ``if ! cmd`` keeps ``set -e`` from tripping on test failures.
overall_rc=0

backend_venv=".venv"
if [[ -x "$REPO_ROOT/.venv/bin/python" ]]; then
  # ``make -C caliber`` resolves VENV relative to caliber/. Prefer the suite-root
  # environment created by ``make setup`` when present, while keeping the
  # package-local caliber/.venv default for contributors who installed there.
  backend_venv="../.venv"
fi

echo "[1/3] Running backend + UI unit tests with Allure result emission (make test-allure)..."
if ! make -C caliber VENV="$backend_venv" test-allure; then
  echo "  ✗ unit tests reported failures (continuing so the report still builds)" >&2
  overall_rc=1
fi

echo "[2/3] Running frontend E2E tests (Playwright)..."
if ! (cd caliber/caliber-ui && VENV_DIR="$backend_venv" npm run test:e2e); then
  echo "  ✗ E2E tests reported failures (continuing so the report still builds)" >&2
  overall_rc=1
fi

if [[ "$WANT_ALLURE" == "1" ]]; then
  echo "[3/3] Generating combined Allure report..."
  if (cd caliber/caliber-ui && npm run allure:generate:all); then
    echo "Allure report generated at: caliber/caliber-ui/allure-report"
    echo "  View in-app: CALIBER → Settings → Allure Report (served by the backend)."
  else
    echo "  ✗ failed to generate the Allure report" >&2
    overall_rc=1
  fi
else
  echo "[3/3] Skipping Allure report generation (--no-allure or ALLURE=0)."
fi

if [[ "$overall_rc" -eq 0 ]]; then
  echo "Full test suite completed — all steps passed."
else
  echo "Full test suite completed — one or more steps FAILED (see above)." >&2
fi
exit "$overall_rc"
