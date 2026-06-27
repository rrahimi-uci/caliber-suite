#!/usr/bin/env bash

set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "${ROOT_DIR}"

test_files=()

if (($#)); then
  test_files=("$@")
else
  while IFS= read -r file; do
    test_files+=("${file}")
  done < <(
    rg --files src \
      -g '*.test.ts' \
      -g '*.test.tsx' \
      -g '*.spec.ts' \
      -g '*.spec.tsx' \
      | sort
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
