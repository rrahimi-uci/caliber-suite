#!/usr/bin/env bash
# Run the frontend Vitest suite one test file at a time (a stabler harness than one big run).
#
# Usage: caliber/caliber-ui/scripts/run-vitest-sequential.sh [-h|--help] [TEST_FILE...]
#   With no arguments it runs every src/**/*.{test,spec}.{ts,tsx}; with arguments, only those files
#   (paths relative to caliber/caliber-ui). Also reachable as: cd caliber/caliber-ui && npm run test:full:stable
# Needs Node.js and the UI dependencies (`npm ci` in caliber/caliber-ui). Exit status is non-zero at
# the first failing file.

set -euo pipefail

case "${1:-}" in
  -h|--help)
    awk 'NR == 1 { next } /^#/ { sub(/^# ?/, ""); print; next } { exit }' "${BASH_SOURCE[0]}"
    exit 0
    ;;
esac

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "${ROOT_DIR}"

if ! command -v npx >/dev/null 2>&1; then
  echo "npx was not found. Install Node.js (see 'engines' in package.json), then run 'npm ci' in caliber/caliber-ui." >&2
  exit 1
fi
if [[ ! -d node_modules ]]; then
  echo "The UI dependencies are not installed. Run 'npm ci' in caliber/caliber-ui first." >&2
  exit 1
fi

test_files=()

if (($#)); then
  test_files=("$@")
else
  # find + sort (no ripgrep needed); LC_ALL=C keeps the order the same on every machine.
  while IFS= read -r file; do
    test_files+=("${file}")
  done < <(
    find src -type f \( -name '*.test.ts' -o -name '*.test.tsx' -o -name '*.spec.ts' -o -name '*.spec.tsx' \) \
      | LC_ALL=C sort
  )
fi

if [[ ${#test_files[@]} -eq 0 ]]; then
  echo "No frontend Vitest files found." >&2
  exit 1
fi

echo "Running ${#test_files[@]} frontend Vitest files sequentially"

index=0
for file in "${test_files[@]}"; do
  index=$((index + 1))
  echo
  echo "=== [${index}/${#test_files[@]}] ${file} ==="
  if [[ "${file}" == "src/pages/__tests__/workflow-studio.test.tsx" ]]; then
    bash ./scripts/run-workflow-studio-sequential.sh
  else
    npx vitest run --reporter=dot "${file}"
  fi
done
