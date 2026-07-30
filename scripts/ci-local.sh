#!/usr/bin/env bash
#
# ci-local.sh — run the CI gates on this machine.
#
# Why this exists: GitHub Actions is metered per *account*. This repository has twice
# had a green code verdict reported as a red run for reasons it does not control —
# first artifact storage ("Artifact storage quota has been hit"), then the minutes
# budget ("The job was not started because an Actions budget is preventing further
# use", which skipped two jobs before they ran a single step). When that happens there
# is no remote signal to wait for, so the local run becomes the only verdict available.
#
# What it mirrors: every job in .github/workflows/ci.yml that executes code.
# `tests/test_ci_local_parity.py` asserts this list stays in step with the workflow, so
# a job added to CI and not here fails the suite rather than quietly going unrun.
#
# What it deliberately does NOT mirror:
#   * artifact upload / Allure rendering / Pages publish — those publish evidence to
#     GitHub and have no local meaning;
#   * the runner OS. This is macOS or whatever you are on, not ubuntu-latest, and the
#     supported Python range is 3.10–3.12 while a dev box is often on something else.
#     A green local run is strong evidence, not release proof, and it says so at the end.
#
# Usage:
#   scripts/ci-local.sh              # everything
#   scripts/ci-local.sh --fast       # skip integration + package (the slow two)
#   scripts/ci-local.sh lint test    # only the named jobs
#
# Exit status is non-zero if any job fails. Every job runs regardless, so one failure
# does not hide the rest — the point is a full picture, which is what CI gives you.

set -uo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
CALIBER_DIR="$REPO_ROOT/caliber"
UI_DIR="$CALIBER_DIR/caliber-ui"
VENV_PY="$CALIBER_DIR/.venv/bin/python"

# Mirrors CALIBER_CI_EXTRAS in the workflow, so a locally-missing optional dependency
# fails here the same way it would there.
CI_EXTRAS="dev,postgres,ingest,ocr,llm,knowledge,dspy,knowledge-local,memory"

ALL_JOBS=(lint type-check test integration ui package security)
FAST_SKIP=(integration package)

bold() { printf '\033[1m%s\033[0m\n' "$*"; }
red() { printf '\033[31m%s\033[0m\n' "$*"; }
green() { printf '\033[32m%s\033[0m\n' "$*"; }
yellow() { printf '\033[33m%s\033[0m\n' "$*"; }

declare -a RESULTS=()
FAILED=0
SKIPPED=0
SKIP_REASON=""
#: A job exits with this to mean "I could not run", distinct from success and failure.
#: 77 is the conventional EX_NOPERM-adjacent "skipped" code used by autotools suites.
EXIT_SKIPPED=77

# Run one job, record its outcome, and keep going. Timing is printed because "which
# gate is slow" is the first thing you want when deciding what to run before a commit.
run_job() {
  local name="$1"
  shift
  bold "── $name ─────────────────────────────────────────────"
  local start
  start=$(date +%s)
  "$@"
  local status=$?
  local elapsed=$(($(date +%s) - start))
  case "$status" in
    0)
      green "✓ $name (${elapsed}s)"
      RESULTS+=("PASS  $name  ${elapsed}s")
      ;;
    "$EXIT_SKIPPED")
      # Not a pass. The summary must never let an unrun check read as a green one, so
      # skipping is its own outcome rather than a successful return with a note.
      yellow "– $name SKIPPED (${elapsed}s)"
      RESULTS+=("SKIP  $name  ${SKIP_REASON:-no reason given}")
      SKIPPED=$((SKIPPED + 1))
      ;;
    *)
      red "✗ $name (${elapsed}s)"
      RESULTS+=("FAIL  $name  ${elapsed}s")
      FAILED=1
      ;;
  esac
  SKIP_REASON=""
  echo
}

job_lint() {
  cd "$CALIBER_DIR" || return 1
  "$VENV_PY" -m ruff check . && "$VENV_PY" -m ruff format --check .
}

job_type_check() {
  cd "$CALIBER_DIR" || return 1
  "$VENV_PY" -m mypy src
}

# Coverage on, matching CI: the repo gate is 80% and running without it locally is how
# a coverage regression reaches CI unnoticed. -n auto mirrors the workflow's xdist use.
job_test() {
  cd "$CALIBER_DIR" || return 1
  "$VENV_PY" -m pytest -n auto --dist loadscope
}

# The 6 tests that skip by default. They are the ones a local run silently omits, so
# they are exactly the ones worth having a local switch for.
job_integration() {
  cd "$CALIBER_DIR" || return 1
  CALIBER_INTEGRATION_TESTS=1 "$VENV_PY" -m pytest tests/test_integration_mlflow.py -p no:randomly --no-cov
}

job_ui() {
  cd "$UI_DIR" || return 1
  npm ci --silent \
    && npm run typecheck \
    && npx eslint . \
    && npx vitest run \
    && npm run build
}

# Builds the wheel and asserts the SPA is inside it — the check that catches a
# packaging change that would ship an empty UI.
job_package() {
  cd "$CALIBER_DIR" || return 1
  [ -f "caliber-ui/dist/index.html" ] || {
    yellow "  SPA dist missing; building it first"
    (cd "$UI_DIR" && npm run build) || return 1
  }
  rm -rf src/caliber/ui && mkdir -p src/caliber/ui && cp -R caliber-ui/dist/. src/caliber/ui/ || return 1
  "$VENV_PY" -m build --outdir dist . >/dev/null 2>&1 || {
    red "  python -m build failed (is 'build' installed? $VENV_PY -m pip install build)"
    return 1
  }
  local whl
  whl=$(ls -t dist/*.whl 2>/dev/null | head -1)
  [ -n "$whl" ] || { red "  no wheel produced"; return 1; }
  "$VENV_PY" -m zipfile -l "$whl" | grep -qE "caliber/ui/(index\.html|assets/)" || {
    red "  wheel is missing the bundled SPA"
    return 1
  }
  echo "  wheel contains the SPA: $(basename "$whl")"
}

# gitleaks is what CI uses. Absent locally it is reported as skipped rather than passed:
# a check that did not run must never look like one that did.
job_security() {
  if ! command -v gitleaks >/dev/null 2>&1; then
    SKIP_REASON="gitleaks not installed (brew install gitleaks); CI still runs it"
    return "$EXIT_SKIPPED"
  fi
  cd "$REPO_ROOT" || return 1
  gitleaks detect --no-banner --redact
}

main() {
  local -a requested=()
  local fast=0
  for arg in "$@"; do
    case "$arg" in
      --fast) fast=1 ;;
      -h | --help)
        sed -n '2,30p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'
        return 0
        ;;
      *) requested+=("$arg") ;;
    esac
  done

  local -a jobs=()
  if [ ${#requested[@]} -gt 0 ]; then
    jobs=("${requested[@]}")
  else
    for j in "${ALL_JOBS[@]}"; do
      if [ "$fast" = 1 ] && printf '%s\n' "${FAST_SKIP[@]}" | grep -qx "$j"; then
        RESULTS+=("SKIP  $j  (--fast)")
        continue
      fi
      jobs+=("$j")
    done
  fi

  # Refuse to pile a second full suite onto a machine already running one. Two
  # concurrent runs starve the sandbox subprocesses of CPU and produce timeout failures
  # that look like product defects — that happened while building this script, and cost
  # a diagnosis cycle. Overridable, because sometimes you know what you are doing.
  if [ "${CI_LOCAL_ALLOW_CONCURRENT:-0}" != "1" ] \
    && pgrep -f "pytest" >/dev/null 2>&1; then
    red "pytest is already running on this machine."
    yellow "Two concurrent suites starve the sandbox subprocesses and produce spurious"
    yellow "timeout failures. Wait for it, or set CI_LOCAL_ALLOW_CONCURRENT=1 to override."
    return 1
  fi

  [ -x "$VENV_PY" ] || {
    red "no venv at $VENV_PY — run 'make install-extended' in caliber/ first"
    return 1
  }

  bold "CALIBER local CI — $(basename "$("$VENV_PY" -c 'import sys; print(sys.executable)')") $("$VENV_PY" -V 2>&1)"
  echo

  for job in "${jobs[@]}"; do
    case "$job" in
      lint) run_job lint job_lint ;;
      type-check) run_job type-check job_type_check ;;
      test) run_job test job_test ;;
      integration) run_job integration job_integration ;;
      ui) run_job ui job_ui ;;
      package) run_job package job_package ;;
      security) run_job security job_security ;;
      *)
        red "unknown job '$job' (known: ${ALL_JOBS[*]})"
        return 2
        ;;
    esac
  done

  bold "── summary ───────────────────────────────────────────"
  printf '%s\n' "${RESULTS[@]}"
  echo
  if [ "$FAILED" != 0 ]; then
    red "local CI FAILED"
  elif [ "$SKIPPED" != 0 ]; then
    # Deliberately not the word "passed" on its own: $SKIPPED gates did not run, and a
    # summary that hides that is the same defect as a control with no surface.
    yellow "local CI passed what it ran — $SKIPPED gate(s) SKIPPED, see above"
  else
    green "local CI passed"
  fi
  # Stated every time, pass or fail. A green local run on an unsupported interpreter is
  # not the same claim as a green CI run, and the difference has mattered in this repo.
  local pyver
  pyver="$("$VENV_PY" -c 'import sys; print("%d.%d" % sys.version_info[:2])')"
  case "$pyver" in
    3.10 | 3.11 | 3.12) ;;
    *) yellow "note: Python $pyver is outside the supported 3.10–3.12 range; this is local evidence, not release proof" ;;
  esac
  return "$FAILED"
}

main "$@"
