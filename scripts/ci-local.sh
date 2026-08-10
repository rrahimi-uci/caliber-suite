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
SDK_DIR="$REPO_ROOT/sdk/caliber-sdk"
SDK_VENV_PY="$SDK_DIR/.venv-sdk/bin/python"
PLUGIN_SDK_DIR="$REPO_ROOT/sdk/caliber-plugin-sdk"
PLUGIN_SDK_VENV_PY="$PLUGIN_SDK_DIR/.venv-plugin-sdk/bin/python"
CLI_DIR="$REPO_ROOT/sdk/caliber-cli"
CLI_VENV="$CLI_DIR/.venv-cli"
CLI_VENV_PY="$CLI_VENV/bin/python"
VENV_PY="$CALIBER_DIR/.venv/bin/python"

# Mirrors CALIBER_CI_EXTRAS in the workflow, so a locally-missing optional dependency
# fails here the same way it would there.
CI_EXTRAS="dev,postgres,ingest,ocr,llm,knowledge,dspy,knowledge-local,memory"

ALL_JOBS=(lint type-check test compatibility integration docs-validation ui cookbook-ui-only compose package security sdk plugin-sdk cli)
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
# a coverage regression reaches CI unnoticed. Explicit xdist groups serialize only the
# subprocess/loopback-sensitive tests instead of pinning every large module to one worker.
# The cross-package suites (SDK-against-server, plugin contract) skip when their
# packages are absent, and a skipped test is not coverage -- so install them
# rather than letting the strongest tests in the suite quietly not run.
job_test() {
  cd "$CALIBER_DIR" || return 1
  "$VENV_PY" -m pip install -q -e "$SDK_DIR" || return 1
  "$VENV_PY" -m pip install -q -e "$PLUGIN_SDK_DIR" || return 1
  "$VENV_PY" -m pytest -n auto --dist loadgroup
}

# The full suite is canonical on Python 3.11 in CI. This bounded edge-version gate makes
# the declared 3.10-3.12 package range executable rather than metadata-only. Local virtual
# environments are persistent/ignored so repeated runs reuse installed wheels; a missing
# interpreter is reported as SKIPPED, never as a pass.
job_compatibility() {
  cd "$CALIBER_DIR" || return 1
  local version interpreter compat_dir compat_python
  for version in 3.10 3.12; do
    interpreter="python${version}"
    if ! command -v "$interpreter" >/dev/null 2>&1; then
      SKIP_REASON="${interpreter} is unavailable; CI still runs the ${version} compatibility leg"
      return "$EXIT_SKIPPED"
    fi
    compat_dir="$CALIBER_DIR/.venv-compat-${version}"
    compat_python="$compat_dir/bin/python"
    if [ ! -x "$compat_python" ]; then
      "$interpreter" -m venv "$compat_dir" || return 1
    fi
    "$compat_python" -m pip install --disable-pip-version-check --quiet -e ".[dev]" \
      || return 1
    "$compat_python" -m pytest \
      tests/test_config.py \
      tests/test_migrations.py \
      tests/test_auth_sessions.py \
      --no-cov -q || return 1
  done
}

# The 6 tests that skip by default. They are the ones a local run silently omits, so
# they are exactly the ones worth having a local switch for.
job_integration() {
  cd "$CALIBER_DIR" || return 1
  CALIBER_INTEGRATION_TESTS=1 "$VENV_PY" -m pytest tests/test_integration_mlflow.py -p no:randomly --no-cov
}

job_ui() {
  cd "$UI_DIR" || return 1
  # A pre-commit run may already contain intentional, synchronized documentation edits.
  # Comparing generated files to HEAD would reject that valid dirty tree. Fingerprint the
  # docs-only diff/status instead: the build must be idempotent relative to the caller's
  # starting state, while a clean CI checkout still requires no generated drift.
  local docs_before docs_after
  docs_before=$(
    {
      git -C "$REPO_ROOT" diff --binary -- \
        docs-site caliber/caliber-ui/public/docs caliber/src/caliber/ui/docs
      git -C "$REPO_ROOT" status --porcelain=v1 --untracked-files=all -- \
        docs-site caliber/caliber-ui/public/docs caliber/src/caliber/ui/docs
    } | git hash-object --stdin
  ) || return 1
  npm ci --silent \
    && npm run typecheck \
    && npx eslint . \
    && npx vitest run \
    && npm run build || return 1
  docs_after=$(
    {
      git -C "$REPO_ROOT" diff --binary -- \
        docs-site caliber/caliber-ui/public/docs caliber/src/caliber/ui/docs
      git -C "$REPO_ROOT" status --porcelain=v1 --untracked-files=all -- \
        docs-site caliber/caliber-ui/public/docs caliber/src/caliber/ui/docs
    } | git hash-object --stdin
  ) || return 1
  if [ "$docs_before" != "$docs_after" ]; then
    red "  UI build changed generated documentation; commit synchronized copies"
    git -C "$REPO_ROOT" status --short -- \
      docs-site caliber/caliber-ui/public/docs caliber/src/caliber/ui/docs
    return 1
  fi
}

# Browser-only Cookbook journeys use the same isolated application/database
# configuration as CI. The Playwright config owns server startup; this wrapper
# owns browser availability and cleanup so a failed run does not leave state in
# the repository.
job_cookbook_ui_only() {
  cd "$UI_DIR" || return 1
  npx playwright install chromium || return 1
  npm run test:e2e:cookbooks
  local status=$?
  CALIBER_E2E_TMP_ROOT="$CALIBER_DIR/.tmp/cookbooks-ui-only" \
    MLFLOW_PORT="${CALIBER_COOKBOOK_E2E_PORT:-5160}" \
    bash "$CALIBER_DIR/scripts/run-playwright-server.sh" --force-cleanup || status=1
  rm -rf \
    "$CALIBER_DIR/.tmp/cookbooks-ui-only" \
    "$UI_DIR/test-results" \
    "$UI_DIR/playwright-report"
  return "$status"
}

job_docs_validation() {
  cd "$REPO_ROOT" || return 1
  CALIBER_DOCS_PYTHON="$VENV_PY" CALIBER_DOCS_STRICT=1 node caliber/caliber-ui/scripts/sync-docs.mjs \
    || return 1
  "$VENV_PY" -m pytest \
    caliber/tests/test_docs_generation_contract.py \
    caliber/tests/test_sdk_docs_contract.py \
    caliber/tests/test_cookbook_doc_contract.py \
    caliber/tests/test_cookbook_steps_contract.py \
    caliber/tests/test_design_principles_contract.py \
    caliber/tests/test_ci_published_site_gate_contract.py \
    caliber/tests/test_docs_executable_spec_contract.py \
    --no-cov || return 1
}

# Parse the fully merged development stack using only committed example values. This
# catches cross-fragment interpolation, include, profile, and dependency defects that
# per-file YAML parsing cannot see, without printing expanded values into CI logs.
job_compose() {
  cd "$REPO_ROOT" || return 1
  command -v docker >/dev/null 2>&1 || {
    SKIP_REASON="docker is unavailable; CI still validates the merged Compose model"
    return "$EXIT_SKIPPED"
  }
  COMPOSE_DISABLE_ENV_FILE=1 docker compose \
    --env-file deploy/.env.example \
    --env-file .env.example \
    -f deploy/compose.yaml \
    --profile app \
    --profile nats \
    config --quiet
}

# Builds the wheel and asserts the SPA is inside it — the check that catches a
# packaging change that would ship an empty UI.
job_package() {
  cd "$CALIBER_DIR" || return 1
  # A pre-existing dist/ may come from an older source tree. Rebuild every time
  # so the local package gate has the same commit binding as the remote UI
  # artifact/rebuild path.
  (cd "$UI_DIR" && npm run build) || return 1
  rm -rf src/caliber/ui && mkdir -p src/caliber/ui && cp -R caliber-ui/dist/. src/caliber/ui/ || return 1
  "$VENV_PY" -m build --outdir dist . >/dev/null 2>&1 || {
    red "  python -m build failed (is 'build' installed? $VENV_PY -m pip install build)"
    return 1
  }
  local whl="" sdist="" candidate
  for candidate in dist/*.whl; do
    [ -e "$candidate" ] || continue
    [ -z "$whl" ] || [ "$candidate" -nt "$whl" ] || continue
    whl="$candidate"
  done
  [ -n "$whl" ] || { red "  no wheel produced"; return 1; }
  "$VENV_PY" - "$whl" <<'PY' || return 1
import sys
import zipfile

with zipfile.ZipFile(sys.argv[1]) as archive:
    names = archive.namelist()
if "caliber/ui/index.html" not in names or not any(
    name.startswith("caliber/ui/assets/") for name in names
):
    raise SystemExit("wheel is missing the bundled SPA")
required_docs = {
    "caliber/ui/docs/m-00-layered-architecture.html",
    "caliber/ui/docs/m-00-layered-architecture.md",
}
missing_docs = required_docs.difference(names)
if missing_docs:
    raise SystemExit(
        "wheel is missing layered architecture documentation: "
        + ", ".join(sorted(missing_docs))
    )
PY
  for candidate in dist/*.tar.gz; do
    [ -e "$candidate" ] || continue
    [ -z "$sdist" ] || [ "$candidate" -nt "$sdist" ] || continue
    sdist="$candidate"
  done
  [ -n "$sdist" ] || { red "  no sdist produced"; return 1; }
  "$VENV_PY" -m twine check "$whl" "$sdist" || return 1
  "$VENV_PY" - "$sdist" <<'PY'
from pathlib import Path
import sys
import tarfile

forbidden = {
    "node_modules", "allure-results", "allure-report", "playwright-report",
    "test-results", "htmlcov", "mlruns", ".pytest_cache", ".mypy_cache",
    ".ruff_cache", ".tmp",
}
with tarfile.open(sys.argv[1]) as archive:
    members = archive.getnames()
leaked = [
    name for name in members
    if forbidden.intersection(Path(name).parts)
    or any(part.startswith((".coverage", ".venv")) for part in Path(name).parts)
]
if leaked:
    raise SystemExit("sdist contains local evidence/cache files: " + ", ".join(leaked[:10]))
PY
  echo "  distributions are clean and the wheel contains the SPA: $(basename "$whl")"
}

# gitleaks is what CI uses. Absent locally it is reported as skipped rather than passed:
# a check that did not run must never look like one that did.
# The Python CI runs the packaged distributions on. Matched deliberately: these
# three packages configure mypy for ``python_version = "3.10"`` while their venvs
# were built from whatever ``python3`` happened to be, so a local pass on 3.14
# said nothing about CI on 3.11 -- and it hid a real one (``tomllib`` is stdlib
# only from 3.11, and the SDK's declared floor is 3.10). Falls back with a
# warning rather than failing, because a missing interpreter should not block a
# local run.
CI_PYTHON_VERSION="3.11"

sdk_interpreter() {
  if command -v "python${CI_PYTHON_VERSION}" >/dev/null 2>&1; then
    printf 'python%s' "$CI_PYTHON_VERSION"
    return 0
  fi
  yellow "  python${CI_PYTHON_VERSION} not found; using python3 ($(python3 -V 2>&1)). CI uses ${CI_PYTHON_VERSION}." >&2
  printf 'python3'
}

# caliber-sdk is a separate distribution with its own ruff/mypy/pytest config, so it
# gets its own virtualenv rather than borrowing the server's. The wheel assertion is
# the point of the package existing: installing it must not drag in the server runtime,
# and a dependency added carelessly would only show up here.
job_sdk() {
  cd "$SDK_DIR" || return 1
  if [ ! -x "$SDK_VENV_PY" ]; then
    "$(sdk_interpreter)" -m venv "$SDK_DIR/.venv-sdk" || return 1
    "$SDK_VENV_PY" -m pip install -q --upgrade pip hatchling build || return 1
  fi
  "$SDK_VENV_PY" -m pip install -q -e ".[dev]" || return 1
  "$SDK_VENV_PY" -m ruff check . || return 1
  "$SDK_VENV_PY" -m mypy || return 1
  "$SDK_VENV_PY" -m pytest || return 1
  rm -rf "$SDK_DIR/dist"
  "$SDK_VENV_PY" -m build --wheel --outdir "$SDK_DIR/dist" || return 1
  "$SDK_VENV_PY" - <<'CHECK'
import glob, re, sys, zipfile
wheel = sorted(glob.glob("dist/*.whl"))[-1]
archive = zipfile.ZipFile(wheel)
name = next(n for n in archive.namelist() if n.endswith("METADATA"))
metadata = archive.read(name).decode()
runtime = [
    line for line in re.findall(r"^Requires-Dist: (.+)$", metadata, re.M)
    if "extra ==" not in line
]
banned = [r for r in runtime if re.search(r"mlflow|sqlalchemy|starlette", r, re.I)]
print("runtime requirements:", runtime)
if banned:
    sys.exit(f"caliber-sdk must not depend on the server runtime: {banned}")
CHECK
}


# caliber-plugin-sdk is the experimental extension contract. Its own venv and
# config for the same reason as caliber-sdk, plus one assertion the server jobs
# cannot make: the contract must have *no* runtime dependencies at all.
job_plugin_sdk() {
  cd "$PLUGIN_SDK_DIR" || return 1
  if [ ! -x "$PLUGIN_SDK_VENV_PY" ]; then
    "$(sdk_interpreter)" -m venv "$PLUGIN_SDK_DIR/.venv-plugin-sdk" || return 1
    "$PLUGIN_SDK_VENV_PY" -m pip install -q --upgrade pip hatchling build || return 1
  fi
  "$PLUGIN_SDK_VENV_PY" -m pip install -q -e ".[dev]" || return 1
  "$PLUGIN_SDK_VENV_PY" -m ruff check . || return 1
  "$PLUGIN_SDK_VENV_PY" -m mypy || return 1
  "$PLUGIN_SDK_VENV_PY" -m pytest || return 1
  rm -rf "$PLUGIN_SDK_DIR/dist"
  "$PLUGIN_SDK_VENV_PY" -m build --wheel --outdir "$PLUGIN_SDK_DIR/dist" || return 1
  "$PLUGIN_SDK_VENV_PY" - <<'CHECK'
import glob, re, sys, zipfile
wheel = sorted(glob.glob("dist/*.whl"))[-1]
archive = zipfile.ZipFile(wheel)
name = next(n for n in archive.namelist() if n.endswith("METADATA"))
metadata = archive.read(name).decode()
runtime = [
    line for line in re.findall(r"^Requires-Dist: (.+)$", metadata, re.M)
    if "extra ==" not in line
]
print("runtime requirements:", runtime)
if runtime:
    sys.exit(f"caliber-plugin-sdk must have no runtime dependencies: {runtime}")
CHECK
}


# caliberctl is installed against the *local* SDK rather than the published one,
# because the CLI has to work with the SDK in this commit. The console-script
# smoke test is the only check that ``[project.scripts]`` points somewhere real:
# the unit tests call main() directly and would pass with a broken entry point.
job_cli() {
  cd "$CLI_DIR" || return 1
  if [ ! -x "$CLI_VENV_PY" ]; then
    "$(sdk_interpreter)" -m venv "$CLI_VENV" || return 1
    "$CLI_VENV_PY" -m pip install -q --upgrade pip hatchling build || return 1
  fi
  "$CLI_VENV_PY" -m pip install -q -e "$SDK_DIR" || return 1
  "$CLI_VENV_PY" -m pip install -q -e ".[dev]" || return 1
  "$CLI_VENV_PY" -m ruff check . || return 1
  "$CLI_VENV_PY" -m mypy || return 1
  "$CLI_VENV_PY" -m pytest || return 1
  "$CLI_VENV/bin/caliberctl" --version || return 1
  "$CLI_VENV/bin/caliberctl" --help | grep -q "awaiting a human" || return 1
  # A bare invocation prints help and exits USAGE (2), never 0.
  if "$CLI_VENV/bin/caliberctl" >/dev/null 2>&1; then
    red "  a bare caliberctl exited 0 having done nothing"
    return 1
  fi
  rm -rf "$CLI_DIR/dist"
  "$CLI_VENV_PY" -m build --wheel --outdir "$CLI_DIR/dist" || return 1
}


job_security() {
  cd "$REPO_ROOT" || return 1
  "$CALIBER_DIR/scripts/run-supported-python-security-audit.sh" \
    --venv-dir "${CALIBER_SECURITY_AUDIT_VENV_DIR:-$CALIBER_DIR/.venv-security-audit}" \
    --extras dev || return 1
  if ! command -v gitleaks >/dev/null 2>&1; then
    SKIP_REASON="dependency audit passed; gitleaks not installed (brew install gitleaks), so the secret scan did not run"
    return "$EXIT_SKIPPED"
  fi
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
      compatibility) run_job compatibility job_compatibility ;;
      integration) run_job integration job_integration ;;
      docs-validation) run_job docs-validation job_docs_validation ;;
      ui) run_job ui job_ui ;;
      cookbook-ui-only) run_job cookbook-ui-only job_cookbook_ui_only ;;
      compose) run_job compose job_compose ;;
      package) run_job package job_package ;;
      security) run_job security job_security ;;
      sdk) run_job sdk job_sdk ;;
      plugin-sdk) run_job plugin-sdk job_plugin_sdk ;;
      cli) run_job cli job_cli ;;
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
