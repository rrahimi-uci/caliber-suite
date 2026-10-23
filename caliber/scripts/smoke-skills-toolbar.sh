#!/usr/bin/env bash
# Smoke-check that a RUNNING CALIBER instance serves the Prompts page's test-case-count toolbar
# controls in its built frontend bundle (the controls that replaced a fixed "15 test cases" dropdown).
#
# Usage:
#   caliber/scripts/smoke-skills-toolbar.sh [BASE_URL]      (default http://127.0.0.1:5001)
#   caliber/scripts/smoke-skills-toolbar.sh -h | --help
# Example:
#   caliber/scripts/smoke-skills-toolbar.sh http://127.0.0.1:5001
#
# Needs curl and a reachable server that serves the packaged web app at BASE_URL/caliber/ (the Docker
# stack from ./start.sh, or `cd caliber && make dev` after `make ui`). Exit status: 0 the bundle has
# the controls, 1 it does not or the server cannot be reached, 2 a usage error.
#
# The file keeps its old name for compatibility: the toolbar lives in the Prompts page now
# (caliber-ui/src/pages/Prompts.tsx), so that is the chunk this script inspects.
set -euo pipefail

case "${1:-}" in
  -h|--help)
    awk 'NR == 1 { next } /^#/ { sub(/^# ?/, ""); print; seen = 1; next } seen { exit }' "${BASH_SOURCE[0]}"
    exit 0
    ;;
  -*)
    echo "unknown option: $1 (see --help)" >&2
    exit 2
    ;;
esac

command -v curl >/dev/null 2>&1 || {
  echo "FAIL: curl is required (for example 'brew install curl' or 'sudo apt-get install curl')" >&2
  exit 1
}

BASE_URL="${1:-http://127.0.0.1:5001}"
BASE_URL="${BASE_URL%/}"
PAGE_URL="${BASE_URL}/caliber/prompts"

fetch() { # fetch URL: the body, or a clear failure naming the URL
  curl -fsSL "$1" || {
    echo "FAIL: could not fetch $1 (is CALIBER running at ${BASE_URL}? did the web app get built?)" >&2
    return 1
  }
}

html="$(fetch "${PAGE_URL}")"

index_path="$(printf '%s' "${html}" | tr '"' '\n' | grep -E '^/caliber/assets/index-.*\.js$' | head -n1 || true)"
if [[ -z "${index_path}" ]]; then
  echo "FAIL: could not find index bundle path from ${PAGE_URL}" >&2
  exit 1
fi

index_js="$(fetch "${BASE_URL}${index_path}")"

chunk_name="$(printf '%s' "${index_js}" | grep -Eo 'Prompts-[A-Za-z0-9_-]+\.js' | head -n1 || true)"
if [[ -z "${chunk_name}" ]]; then
  echo "FAIL: could not find the Prompts chunk reference in ${index_path}" >&2
  exit 1
fi

chunk_path="/caliber/assets/${chunk_name}"
chunk_js="$(fetch "${BASE_URL}${chunk_path}")"

required_tokens=(
  "Decrease test case count"
  "Increase test case count"
  "Number of test cases"
  "Choose any value from "
)

for token in "${required_tokens[@]}"; do
  if ! printf '%s' "${chunk_js}" | grep -Fq "${token}"; then
    echo "FAIL: missing token in ${chunk_path}: ${token}" >&2
    exit 1
  fi
done

if printf '%s' "${chunk_js}" | grep -Fq "15 test cases"; then
  echo "FAIL: stale fixed-dropdown token detected in ${chunk_path} ('15 test cases')" >&2
  exit 1
fi

echo "PASS: Prompts test-case toolbar bundle check succeeded."
echo "  base: ${BASE_URL}"
echo "  index: ${index_path}"
echo "  chunk: ${chunk_path}"
