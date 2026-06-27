#!/usr/bin/env bash
set -euo pipefail

# Smoke-check that a running CALIBER instance serves the updated Skills
# Test Cases toolbar controls in the built frontend bundle.
#
# Usage:
#   ./scripts/smoke-skills-toolbar.sh [BASE_URL]
# Example:
#   ./scripts/smoke-skills-toolbar.sh http://127.0.0.1:5001

BASE_URL="${1:-http://127.0.0.1:5001}"
SKILLS_URL="${BASE_URL}/caliber/skills"

html="$(curl -fsSL "${SKILLS_URL}")"

index_path="$(printf '%s' "${html}" | tr '"' '\n' | grep -E '^/caliber/assets/index-.*\.js$' | head -n1 || true)"
if [[ -z "${index_path}" ]]; then
  echo "FAIL: could not find index bundle path from ${SKILLS_URL}" >&2
  exit 1
fi

index_js="$(curl -fsSL "${BASE_URL}${index_path}")"

skills_name="$(printf '%s' "${index_js}" | grep -Eo 'Skills-[A-Za-z0-9_-]+\.js' | head -n1 || true)"
if [[ -z "${skills_name}" ]]; then
  echo "FAIL: could not find Skills chunk reference in ${index_path}" >&2
  exit 1
fi

skills_js_path="/caliber/assets/${skills_name}"
skills_js="$(curl -fsSL "${BASE_URL}${skills_js_path}")"

required_tokens=(
  "Decrease test case count"
  "Increase test case count"
  "Number of test cases"
  "Choose any value from "
)

for token in "${required_tokens[@]}"; do
  if ! printf '%s' "${skills_js}" | grep -Fq "${token}"; then
    echo "FAIL: missing token in ${skills_js_path}: ${token}" >&2
    exit 1
  fi
done

if printf '%s' "${skills_js}" | grep -Fq "15 test cases"; then
  echo "FAIL: stale fixed-dropdown token detected in ${skills_js_path} ('15 test cases')" >&2
  exit 1
fi

echo "PASS: Skills Test Cases toolbar bundle check succeeded."
echo "  base: ${BASE_URL}"
echo "  index: ${index_path}"
echo "  skills: ${skills_js_path}"
