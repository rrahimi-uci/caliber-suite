#!/usr/bin/env bash

set -euo pipefail

usage() {
  cat <<'EOF'
Usage: ./scripts/run-workflow-platform-release-gate.sh [options]

Runs the broad local workflow-platform release gate across backend, frontend,
integration, and packaging surfaces.

Options:
  --npm-ci            Run `npm ci` in caliber-ui before frontend checks.
  --with-security     Also run the supported-Python dependency audit for the
                      production-safe extras profile.
  --with-playwright   Also run the workflow-platform Playwright suite in caliber-ui.
  --full-backend-pytest
                      Also run the full backend `pytest -q` suite after the
                      workflow-platform smoke batch.
  --full-frontend-vitest
                      Also run the full frontend Vitest corpus through the
                      stable sequential harness after the workflow-platform
                      smoke batch.
  --skip-integration  Skip `pytest -q -m integration --no-cov`.
  --skip-package      Skip the Python sdist/wheel build step.
  -h, --help          Show this help text.
EOF
}

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
BACKEND_DIR="${ROOT_DIR}/caliber"
FRONTEND_DIR="${BACKEND_DIR}/caliber-ui"
VENV_DIR="${BACKEND_DIR}/.venv"
RUFF_BIN="${VENV_DIR}/bin/ruff"
MYPY_BIN="${VENV_DIR}/bin/mypy"
PYTEST_BIN="${VENV_DIR}/bin/pytest"
PYTHON_BIN="${VENV_DIR}/bin/python"
PIP_AUDIT_BIN="${VENV_DIR}/bin/pip-audit"
SECURITY_AUDIT_SCRIPT="${BACKEND_DIR}/scripts/run-supported-python-security-audit.sh"
PYTEST_CLEAN_EXIT_SCRIPT="${BACKEND_DIR}/scripts/pytest_clean_exit.py"

RUN_NPM_CI=0
RUN_SECURITY=0
RUN_PLAYWRIGHT=0
RUN_FULL_BACKEND_PYTEST=0
RUN_FULL_FRONTEND_VITEST=0
SKIP_INTEGRATION=0
SKIP_PACKAGE=0

BACKEND_WORKFLOW_PLATFORM_SMOKE_TESTS=(
  "tests/test_routes_workflows.py::test_list_workflow_components_catalog"
  "tests/test_routes_workflows.py::test_workflow_components_catalog_entries_are_self_describing"
  "tests/test_workflow_validation.py::test_parallel_and_join_warn_when_branch_structure_is_insufficient"
  "tests/test_workflow_validation.py::test_router_requires_branches_and_matching_edges"
  "tests/test_routes_workflow_runs_async.py::test_create_and_get_workflow_run"
  "tests/test_routes_workflow_runs_async.py::test_runtime_approval_routes_approve_and_resume"
  "tests/test_routes_workflow_runs_async.py::test_waiting_event_resume_by_correlated_event_matches_single_run"
  "tests/test_routes_workflow_runs_async.py::test_wait_until_run_can_resume_and_stores_manual_resume_payload"
  "tests/test_workflow_runtime.py::test_knowledge_query_node_executes_with_age_graph_payload"
  "tests/test_workflow_runtime.py::test_knowledge_build_node_launches_and_publishes_result"
  "tests/test_routes_knowledge_bases.py::test_knowledge_base_options_expose_chunkers_and_models"
  "tests/test_routes_knowledge_bases.py::test_knowledge_base_build_versions_query_and_rollback"
  "tests/test_routes_knowledge_bases.py::test_knowledge_base_age_graph_sync_and_retrieval"
  "tests/test_routes_object_store.py::test_status_buckets_objects_full_crud"
  "tests/test_routes_object_store.py::test_preview_object_supports_text_and_binary"
)

while (($#)); do
  case "$1" in
    --npm-ci)
      RUN_NPM_CI=1
      ;;
    --with-security)
      RUN_SECURITY=1
      ;;
    --with-playwright)
      RUN_PLAYWRIGHT=1
      ;;
    --full-backend-pytest)
      RUN_FULL_BACKEND_PYTEST=1
      ;;
    --full-frontend-vitest)
      RUN_FULL_FRONTEND_VITEST=1
      ;;
    --skip-integration)
      SKIP_INTEGRATION=1
      ;;
    --skip-package)
      SKIP_PACKAGE=1
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      echo "Unknown option: $1" >&2
      usage >&2
      exit 1
      ;;
  esac
  shift
done

run_step() {
  local label="$1"
  shift
  echo
  echo "==> ${label}"
  "$@"
}

run_in_dir() {
  local label="$1"
  local dir="$2"
  shift 2
  echo
  echo "==> ${label}"
  (
    cd "${dir}"
    "$@"
  )
}

run_backend_security_audit() {
  if [[ -x "${SECURITY_AUDIT_SCRIPT}" ]]; then
    "${SECURITY_AUDIT_SCRIPT}"
    return $?
  fi

  local requirements_file
  local audit_status
  requirements_file="$(mktemp "${BACKEND_DIR}/.tmp-pip-audit.XXXXXX.txt")"

  (
    cd "${BACKEND_DIR}"
    "${PYTHON_BIN}" -m pip freeze \
      | grep -vE '^(#|$|-e .*#egg=caliber|caliber @ )' \
      > "${requirements_file}"
    "${PIP_AUDIT_BIN}" --strict --no-deps -r "${requirements_file}"
  )
  audit_status=$?
  rm -f "${requirements_file}"
  return "${audit_status}"
}

sync_packaged_ui_from_dist() {
  (
    cd "${BACKEND_DIR}"
    rm -rf src/caliber/ui
    mkdir -p src/caliber/ui
    cp -R caliber-ui/dist/. src/caliber/ui/
  )
}

echo "Workflow platform release gate"
echo "  repo root : ${ROOT_DIR}"
echo "  backend   : ${BACKEND_DIR}"
echo "  frontend  : ${FRONTEND_DIR}"

if [[ ! -x "${RUFF_BIN}" || ! -x "${MYPY_BIN}" || ! -x "${PYTEST_BIN}" || ! -x "${PYTHON_BIN}" ]]; then
  echo "Expected backend toolchain in ${VENV_DIR}/bin. Run the local install first." >&2
  exit 1
fi

BACKEND_PYTEST_CMD=("${PYTEST_BIN}")
if [[ -f "${PYTEST_CLEAN_EXIT_SCRIPT}" ]]; then
  BACKEND_PYTEST_CMD=("${PYTHON_BIN}" "scripts/pytest_clean_exit.py")
fi

run_in_dir "Backend ruff check" "${BACKEND_DIR}" "${RUFF_BIN}" check .
run_in_dir "Backend ruff format --check" "${BACKEND_DIR}" "${RUFF_BIN}" format --check .
run_in_dir "Backend mypy" "${BACKEND_DIR}" "${MYPY_BIN}" src
run_in_dir \
  "Backend workflow-platform smoke pytest" \
  "${BACKEND_DIR}" \
  "${BACKEND_PYTEST_CMD[@]}" --no-cov -q -x "${BACKEND_WORKFLOW_PLATFORM_SMOKE_TESTS[@]}"

if [[ "${RUN_FULL_BACKEND_PYTEST}" -eq 1 ]]; then
  run_in_dir "Frontend build for packaged-ui backend checks" "${FRONTEND_DIR}" npm run build
  run_step "Sync packaged UI bundle" sync_packaged_ui_from_dist
  run_in_dir "Backend full pytest" "${BACKEND_DIR}" "${BACKEND_PYTEST_CMD[@]}" -q
else
  echo
  echo "==> Skipping backend full pytest (use --full-backend-pytest to enable)"
fi

if [[ "${SKIP_INTEGRATION}" -eq 0 ]]; then
  run_in_dir \
    "Backend integration pytest" \
    "${BACKEND_DIR}" \
    env CALIBER_INTEGRATION_TESTS=1 "${BACKEND_PYTEST_CMD[@]}" -q -m integration --no-cov
else
  echo
  echo "==> Skipping backend integration pytest"
fi

if [[ "${RUN_NPM_CI}" -eq 1 ]]; then
  run_in_dir "Frontend npm ci" "${FRONTEND_DIR}" npm ci
fi

run_in_dir "Frontend eslint" "${FRONTEND_DIR}" npm run lint
run_in_dir "Frontend typecheck" "${FRONTEND_DIR}" npm run typecheck
run_in_dir "Frontend workflow-platform vitest smoke" "${FRONTEND_DIR}" npm run test:workflow-platform:smoke

if [[ "${RUN_FULL_FRONTEND_VITEST}" -eq 1 ]]; then
  run_in_dir "Frontend full vitest" "${FRONTEND_DIR}" npm run test:full:stable
else
  echo
  echo "==> Skipping frontend full vitest (use --full-frontend-vitest to enable)"
fi
run_in_dir "Frontend build" "${FRONTEND_DIR}" npm run build

if [[ "${RUN_PLAYWRIGHT}" -eq 1 ]]; then
  run_in_dir "Frontend Playwright" "${FRONTEND_DIR}" npm run test:e2e:workflow-platform
else
  echo
  echo "==> Skipping Playwright E2E"
fi

if [[ "${SKIP_PACKAGE}" -eq 0 ]]; then
  run_in_dir "Python package build" "${BACKEND_DIR}" bash -lc '
    rm -rf src/caliber/ui
    mkdir -p src/caliber/ui
    cp -R caliber-ui/dist/. src/caliber/ui/
    "'"${PYTHON_BIN}"'" -m build
  '
else
  echo
  echo "==> Skipping Python package build"
fi

if [[ "${RUN_SECURITY}" -eq 1 ]]; then
  if [[ -x "${SECURITY_AUDIT_SCRIPT}" ]]; then
    echo
    echo "==> Supported-Python dependency audit"
    run_backend_security_audit
  elif [[ -x "${PIP_AUDIT_BIN}" ]]; then
    echo
    echo "==> pip-audit --strict (current third-party pinned environment)"
    run_backend_security_audit
  else
    echo
    echo "==> Security scan requested, but no supported audit helper or pip-audit is available" >&2
    exit 1
  fi
else
  echo
  echo "==> Skipping pip-audit security scan"
fi

echo
echo "Release gate completed successfully."
