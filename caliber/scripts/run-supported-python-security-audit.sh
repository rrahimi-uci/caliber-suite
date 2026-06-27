#!/usr/bin/env bash

set -euo pipefail

usage() {
  cat <<'EOF'
Usage: ./scripts/run-supported-python-security-audit.sh [options]

Create or reuse a clean security-audit virtualenv on a supported Python
interpreter (3.10-3.12), install CALIBER plus the requested optional extras,
and run `pip-audit --strict` against the resolved third-party package set.

Options:
  --python PATH       Python interpreter to use for the audit virtualenv.
  --venv-dir PATH     Virtualenv directory to create/reuse.
  --extras LIST       Comma-separated extras to install (default: llm,knowledge
                      for the production-safe profile).
  -h, --help          Show this help text.

Environment:
  CALIBER_SECURITY_AUDIT_PYTHON_BIN  Override the Python interpreter path.
  CALIBER_SECURITY_AUDIT_VENV_DIR    Override the audit virtualenv directory.
  CALIBER_SECURITY_AUDIT_EXTRAS      Override the extras list.
EOF
}

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "${REPO_ROOT}"

REQUESTED_PYTHON="${CALIBER_SECURITY_AUDIT_PYTHON_BIN:-}"
SECURITY_VENV_DIR="${CALIBER_SECURITY_AUDIT_VENV_DIR:-${REPO_ROOT}/.venv-security-audit}"
SECURITY_EXTRAS="${CALIBER_SECURITY_AUDIT_EXTRAS:-llm,knowledge}"

while (($#)); do
  case "$1" in
    --python)
      REQUESTED_PYTHON="${2:-}"
      shift 2
      ;;
    --venv-dir)
      SECURITY_VENV_DIR="${2:-}"
      shift 2
      ;;
    --extras)
      SECURITY_EXTRAS="${2:-}"
      shift 2
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
done

python_minor_version() {
  "$1" - <<'PY'
import sys

print(f"{sys.version_info.major}.{sys.version_info.minor}")
PY
}

is_supported_python_minor() {
  case "$1" in
    3.10|3.11|3.12)
      return 0
      ;;
    *)
      return 1
      ;;
  esac
}

resolve_python_bin() {
  local candidate path version
  if [[ -n "${REQUESTED_PYTHON}" ]]; then
    if [[ ! -x "${REQUESTED_PYTHON}" ]]; then
      echo "Requested Python is not executable: ${REQUESTED_PYTHON}" >&2
      exit 1
    fi
    version="$(python_minor_version "${REQUESTED_PYTHON}")"
    if ! is_supported_python_minor "${version}"; then
      echo "Requested Python ${REQUESTED_PYTHON} is unsupported (${version}); use 3.10-3.12." >&2
      exit 1
    fi
    echo "${REQUESTED_PYTHON}"
    return 0
  fi

  if [[ -x "${REPO_ROOT}/.venv/bin/python" ]]; then
    version="$(python_minor_version "${REPO_ROOT}/.venv/bin/python")"
    if is_supported_python_minor "${version}"; then
      echo "${REPO_ROOT}/.venv/bin/python"
      return 0
    fi
  fi

  for candidate in python3.12 python3.11 python3.10; do
    path="$(command -v "${candidate}" || true)"
    if [[ -z "${path}" ]]; then
      continue
    fi
    version="$(python_minor_version "${path}")"
    if is_supported_python_minor "${version}"; then
      echo "${path}"
      return 0
    fi
  done

  echo "No supported Python 3.10-3.12 interpreter was found for the security audit." >&2
  exit 1
}

ensure_security_venv() {
  local python_bin="$1"
  local requested_version current_version recreate=0
  requested_version="$(python_minor_version "${python_bin}")"
  if [[ ! -x "${SECURITY_VENV_DIR}/bin/python" ]]; then
    recreate=1
  else
    current_version="$(python_minor_version "${SECURITY_VENV_DIR}/bin/python" || true)"
    if [[ "${current_version}" != "${requested_version}" ]]; then
      recreate=1
    fi
  fi

  if [[ "${recreate}" -eq 1 ]]; then
    rm -rf "${SECURITY_VENV_DIR}"
    "${python_bin}" -m venv "${SECURITY_VENV_DIR}"
  fi
}

install_security_stack() {
  local install_target="."
  if [[ -n "${SECURITY_EXTRAS}" ]]; then
    install_target=".[${SECURITY_EXTRAS}]"
  fi

  PIP_DISABLE_PIP_VERSION_CHECK=1 "${SECURITY_VENV_DIR}/bin/python" -m pip install -q --upgrade \
    pip 'setuptools<82' wheel pip-audit
  PIP_DISABLE_PIP_VERSION_CHECK=1 "${SECURITY_VENV_DIR}/bin/python" -m pip install -q -e "${install_target}"
}

print_selected_versions() {
  "${SECURITY_VENV_DIR}/bin/python" - <<'PY'
from importlib.metadata import PackageNotFoundError, version

for package_name in (
    "litellm",
    "dspy",
    "sentence-transformers",
    "torch",
    "diskcache",
    "starlette",
):
    try:
        print(f"{package_name}={version(package_name)}")
    except PackageNotFoundError:
        continue
PY
}

run_security_audit() {
  local requirements_file cache_dir audit_status
  requirements_file="$(mktemp "${REPO_ROOT}/.tmp-pip-audit.XXXXXX")"
  cache_dir="$(mktemp -d "${REPO_ROOT}/.tmp-pip-audit-cache.XXXXXX")"

  cleanup() {
    rm -f "${requirements_file}"
    rm -rf "${cache_dir}"
  }
  trap cleanup RETURN

  "${SECURITY_VENV_DIR}/bin/python" -m pip freeze \
    | grep -vE '^(#|$|-e .*#egg=caliber|caliber @ )' \
    > "${requirements_file}"

  set +e
  "${SECURITY_VENV_DIR}/bin/pip-audit" \
    --strict \
    --no-deps \
    --progress-spinner off \
    --cache-dir "${cache_dir}" \
    -r "${requirements_file}"
  audit_status=$?
  set -e
  return "${audit_status}"
}

PYTHON_BIN="$(resolve_python_bin)"
PYTHON_VERSION="$(python_minor_version "${PYTHON_BIN}")"

ensure_security_venv "${PYTHON_BIN}"

echo "Supported-Python security audit"
echo "  repo root : ${REPO_ROOT}"
echo "  python    : ${PYTHON_BIN} (${PYTHON_VERSION})"
echo "  venv      : ${SECURITY_VENV_DIR}"
echo "  extras    : ${SECURITY_EXTRAS:-<none>}"

install_security_stack

echo
echo "==> Resolved key package versions"
print_selected_versions

echo
echo "==> pip-audit --strict (supported-Python clean environment)"
run_security_audit
