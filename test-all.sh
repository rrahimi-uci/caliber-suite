#!/usr/bin/env bash
# Run the full CALIBER test suite from repo root:
#   1) backend pytest, emitting Allure results (make -C caliber test-allure)
#   2) UI unit tests (vitest), emitting Allure results
#   3) UI Playwright E2E tests (starts its own disposable server on a private port)
#   4) combined Allure HTML report (unless --no-allure / ALLURE=0)
#
# Test failures do NOT abort the run: every step executes, the Allure report is
# still generated (you most want it WHEN something failed), and the script exits
# non-zero at the end if any step failed.
#
# Prerequisites (checked before the first test runs): `make setup` done (it creates the .venv, installs
# the UI dependencies and builds the UI), make, node and npm, the Playwright chromium browser
# (cd caliber/caliber-ui && npx playwright install chromium), and for the Allure report a working
# Java runtime (macOS: brew install --cask temurin; Debian/Ubuntu: sudo apt-get install default-jre) --
# or pass --no-allure.
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

Needs: `make setup` done, make, node, npm, the Playwright chromium browser, and (for the Allure
report) a working Java runtime. Exit status: 0 all steps passed, 1 a step failed or a prerequisite
is missing, 2 a usage error.
EOF
}

case "${ALLURE:-1}" in
  0|false|FALSE|no|NO|off|OFF) WANT_ALLURE=0 ;;
  *) WANT_ALLURE=1 ;;
esac

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

# ---- prerequisites: fail before the first (long) step, and say what to do ----------------------
missing() { echo "error: $*" >&2; exit 1; }

command -v make >/dev/null 2>&1 \
  || missing "'make' is required but not found in PATH (macOS: xcode-select --install; Debian/Ubuntu: sudo apt-get install make)"
command -v node >/dev/null 2>&1 \
  || missing "'node' is required but not found in PATH. Install Node.js (see 'engines' in caliber/caliber-ui/package.json)"
command -v npm >/dev/null 2>&1 \
  || missing "'npm' is required but not found in PATH. It ships with Node.js"

backend_venv=".venv"
if [[ -x "$REPO_ROOT/.venv/bin/python" ]]; then
  # ``make -C caliber`` resolves VENV relative to caliber/. Prefer the suite-root
  # environment created by ``make setup`` when present, while keeping the
  # package-local caliber/.venv default for contributors who installed there.
  backend_venv="../.venv"
elif [[ ! -x "$REPO_ROOT/caliber/.venv/bin/python" ]]; then
  missing "no Python virtualenv found (looked for .venv and caliber/.venv). Run 'make setup' in the repository root first"
fi

[[ -d "$REPO_ROOT/caliber/caliber-ui/node_modules" ]] \
  || missing "the UI dependencies are not installed. Run 'make setup' (or 'npm ci' in caliber/caliber-ui) first"

(cd caliber/caliber-ui && node -e 'const {chromium}=require("@playwright/test");process.exit(require("fs").existsSync(chromium.executablePath())?0:1)') 2>/dev/null \
  || missing "the Playwright chromium browser is not installed. Run: cd caliber/caliber-ui && npx playwright install chromium"

if [[ "$WANT_ALLURE" == "1" ]]; then
  # A *working* JRE, not just a binary on PATH: macOS ships a /usr/bin/java stub that exists with no runtime.
  java -version >/dev/null 2>&1 \
    || missing "the Allure report needs a Java runtime (macOS: brew install --cask temurin; Debian/Ubuntu: sudo apt-get install default-jre). Install one, or re-run with --no-allure to skip the report (the tests still run and emit results)"
fi

# Track failures across steps without aborting (so the report still builds and
# every suite runs). ``if ! cmd`` keeps ``set -e`` from tripping on test failures.
overall_rc=0

echo "[1/4] Running backend tests with Allure result emission (make -C caliber test-allure)..."
if ! make -C caliber VENV="$backend_venv" test-allure; then
  echo "  ✗ backend tests reported failures (continuing so the report still builds)" >&2
  overall_rc=1
fi

echo "[2/4] Running frontend unit tests (vitest)..."
# Wipe stale frontend results first: the vitest/playwright reporters APPEND, so the report would
# otherwise accumulate old tests. The E2E step below appends into the same freshly cleaned directory.
rm -rf "$REPO_ROOT/caliber/caliber-ui/allure-results"
if ! (cd caliber/caliber-ui && npm test); then
  echo "  ✗ frontend unit tests reported failures (continuing so the report still builds)" >&2
  overall_rc=1
fi

echo "[3/4] Running frontend E2E tests (Playwright)..."
if ! (cd caliber/caliber-ui && VENV_DIR="$backend_venv" npm run test:e2e); then
  echo "  ✗ E2E tests reported failures (continuing so the report still builds)" >&2
  overall_rc=1
fi

if [[ "$WANT_ALLURE" == "1" ]]; then
  echo "[4/4] Generating combined Allure report..."
  if (cd caliber/caliber-ui && npm run allure:generate:all); then
    echo "Allure report generated at: caliber/caliber-ui/allure-report"
    echo "  View in-app: CALIBER → Settings → Allure Report (served by the backend)."
  else
    echo "  ✗ failed to generate the Allure report" >&2
    overall_rc=1
  fi
else
  echo "[4/4] Skipping Allure report generation (--no-allure or ALLURE=0)."
fi

if [[ "$overall_rc" -eq 0 ]]; then
  echo "Full test suite completed — all steps passed."
else
  echo "Full test suite completed — one or more steps FAILED (see above)." >&2
fi
exit "$overall_rc"
