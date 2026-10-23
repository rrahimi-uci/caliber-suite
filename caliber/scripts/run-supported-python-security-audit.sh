#!/usr/bin/env bash

set -euo pipefail

usage() {
  cat <<'EOF'
Usage: ./scripts/run-supported-python-security-audit.sh [options]

Create or reuse a clean security-audit virtualenv on a supported Python
interpreter (3.10-3.12), install CALIBER plus the requested optional extras,
and run `pip-audit --strict` against the resolved third-party package set.

Options:
  --python PATH       Python interpreter (a path, or a command on PATH such as python3.12)
                      to use for the audit virtualenv. Default: caliber/.venv if it is
                      3.10-3.12, else the first of python3.12, python3.11, python3.10 on PATH.
  --venv-dir PATH     Virtualenv directory to create/reuse (default: caliber/.venv-security-audit).
                      A directory that exists but is not a virtualenv is never deleted: the script
                      refuses it. A virtualenv on another Python version is recreated.
  --extras LIST       Comma-separated extras to install (default: llm,knowledge
                      for the production-safe profile).
  -h, --help          Show this help text.

Environment:
  CALIBER_SECURITY_AUDIT_PYTHON_BIN  Override the Python interpreter path.
  CALIBER_SECURITY_AUDIT_VENV_DIR    Override the audit virtualenv directory.
  CALIBER_SECURITY_AUDIT_EXTRAS      Override the extras list.

Needs network access (pip installs CALIBER and its extras, and pip-audit queries the advisory
database). Exit status: 0 no vulnerable dependency, 1 findings or a missing prerequisite.
EOF
}

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "${REPO_ROOT}"

REQUESTED_PYTHON="${CALIBER_SECURITY_AUDIT_PYTHON_BIN:-}"
SECURITY_VENV_DIR="${CALIBER_SECURITY_AUDIT_VENV_DIR:-${REPO_ROOT}/.venv-security-audit}"
SECURITY_EXTRAS="${CALIBER_SECURITY_AUDIT_EXTRAS:-llm,knowledge}"

# CVE-2026-69247 affects only cryptography's PKCS#7 EnvelopedData decryption
# APIs. CALIBER does not call or expose those APIs, while MLflow 3.14-3.15 pins
# cryptography below the fixed 50.0.0 release. Keep this explicit, narrow
# exception until MLflow permits cryptography>=50; pip-audit still reports and
# fails on every other advisory.
PIP_AUDIT_IGNORES=("CVE-2026-69247")

# CVE-2026-71211 was fixed in MLflow 3.15.0 and is absent from the resolved
# 3.15.2 release, but pip-audit currently reports it for that release. The
# application's minimum version is fixed; remove this exception when the
# advisory database's affected-version range is corrected.
PIP_AUDIT_IGNORES+=("CVE-2026-71211")

while (($#)); do
  case "$1" in
    --python | --venv-dir | --extras)
      if [[ $# -lt 2 ]]; then
        echo "Option $1 needs a value." >&2
        usage >&2
        exit 1
      fi
      case "$1" in
        --python) REQUESTED_PYTHON="$2" ;;
        --venv-dir) SECURITY_VENV_DIR="$2" ;;
        --extras) SECURITY_EXTRAS="$2" ;;
      esac
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
    # Accept a bare command name (python3.12) as well as a path.
    path="$(command -v "${REQUESTED_PYTHON}" 2>/dev/null || true)"
    if [[ -z "${path}" || ! -x "${path}" ]]; then
      echo "Requested Python is not found or not executable: ${REQUESTED_PYTHON}" >&2
      exit 1
    fi
    version="$(python_minor_version "${path}")"
    if ! is_supported_python_minor "${version}"; then
      echo "Requested Python ${REQUESTED_PYTHON} is unsupported (${version}); use 3.10-3.12." >&2
      exit 1
    fi
    echo "${path}"
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
  echo "Install one (macOS: brew install python@3.12; Debian/Ubuntu: sudo apt-get install python3.12 python3.12-venv) or pass --python." >&2
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
    # Only ever delete something that is demonstrably a virtualenv. A mistyped --venv-dir that
    # points at a real directory must not be wiped.
    if [[ -e "${SECURITY_VENV_DIR}" && ! -f "${SECURITY_VENV_DIR}/pyvenv.cfg" ]]; then
      echo "Refusing to delete ${SECURITY_VENV_DIR}: it exists but is not a virtualenv (no pyvenv.cfg). Choose another --venv-dir." >&2
      exit 1
    fi
    rm -rf "${SECURITY_VENV_DIR}"
    "${python_bin}" -m venv "${SECURITY_VENV_DIR}" \
      || { echo "Could not create a virtualenv with ${python_bin} (on Debian/Ubuntu install the matching python3-venv package)." >&2; exit 1; }
  fi
}

install_security_stack() {
  local install_target="."
  if [[ -n "${SECURITY_EXTRAS}" ]]; then
    install_target=".[${SECURITY_EXTRAS}]"
  fi

  PIP_DISABLE_PIP_VERSION_CHECK=1 "${SECURITY_VENV_DIR}/bin/python" -m pip install -q --upgrade \
    pip 'setuptools>=83' wheel pip-audit
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
  local requirements_file cache_dir audit_status vulnerability_id
  local -a audit_args
  requirements_file="$(mktemp "${REPO_ROOT}/.tmp-pip-audit.XXXXXX")"
  cache_dir="$(mktemp -d "${REPO_ROOT}/.tmp-pip-audit-cache.XXXXXX")"
  AUDIT_TMP_FILE="${requirements_file}"
  AUDIT_TMP_DIR="${cache_dir}"

  "${SECURITY_VENV_DIR}/bin/python" -m pip freeze \
    | grep -vE '^(#|$|-e .*#egg=caliber|caliber @ )' \
    > "${requirements_file}"

  audit_args=(
    --strict
    --no-deps
    --progress-spinner off
    --cache-dir "${cache_dir}"
    -r "${requirements_file}"
  )
  for vulnerability_id in "${PIP_AUDIT_IGNORES[@]}"; do
    audit_args+=(--ignore-vuln "${vulnerability_id}")
  done

  set +e
  "${SECURITY_VENV_DIR}/bin/pip-audit" "${audit_args[@]}"
  audit_status=$?
  set -e
  return "${audit_status}"
}

# Scratch files live in the repository (gitignored .tmp-*) and are removed on every exit path.
AUDIT_TMP_FILE=""
AUDIT_TMP_DIR=""
cleanup_audit_tmp() {
  if [[ -n "${AUDIT_TMP_FILE}" ]]; then rm -f "${AUDIT_TMP_FILE}"; fi
  if [[ -n "${AUDIT_TMP_DIR}" ]]; then rm -rf "${AUDIT_TMP_DIR}"; fi
  return 0
}
trap cleanup_audit_tmp EXIT

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
