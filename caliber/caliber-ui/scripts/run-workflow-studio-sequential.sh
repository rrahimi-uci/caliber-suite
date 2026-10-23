#!/usr/bin/env bash
# Run src/pages/__tests__/workflow-studio.test.tsx in slices (one describe block, and the
# WorkflowDetail calibration tests two at a time), because the file is too heavy for a single run.
# Called by run-vitest-sequential.sh; can also be run alone:
#   caliber/caliber-ui/scripts/run-workflow-studio-sequential.sh [-h|--help]
# Needs Node.js and the UI dependencies (`npm ci` in caliber/caliber-ui).

set -euo pipefail

case "${1:-}" in
  -h|--help)
    awk 'NR == 1 { next } /^#/ { sub(/^# ?/, ""); print; next } { exit }' "${BASH_SOURCE[0]}"
    exit 0
    ;;
esac

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "${ROOT_DIR}"

if ! command -v node >/dev/null 2>&1; then
  echo "node was not found. Install Node.js (see 'engines' in package.json), then run 'npm ci' in caliber/caliber-ui." >&2
  exit 1
fi
if [[ ! -d node_modules ]]; then
  echo "The UI dependencies are not installed. Run 'npm ci' in caliber/caliber-ui first." >&2
  exit 1
fi

FILE="src/pages/__tests__/workflow-studio.test.tsx"

run_slice() {
  local label="$1"
  local pattern="$2"
  echo
  echo "=== ${label} ==="
  npx vitest run --reporter=dot "${FILE}" -t "${pattern}"
}

run_slice "Workflows page" "Workflows page"
run_slice "ToolRegistry page" "ToolRegistry page"
run_slice "McpServers playground" "McpServers playground"
run_slice "WorkflowEditor page" "WorkflowEditor page"

chunk_index=0
# shellcheck disable=SC2016  # the single quotes are deliberate: this is JavaScript, not shell
chunk_patterns="$(
  node -e '
    const fs = require("fs");
    const file = "src/pages/__tests__/workflow-studio.test.tsx";
    const lines = fs.readFileSync(file, "utf8").split(/\r?\n/);
    const names = [];
    let inBlock = false;

    for (const line of lines) {
      if (line.startsWith(`describe("WorkflowDetail calibration"`)) {
        inBlock = true;
        continue;
      }
      if (inBlock && line.startsWith(`describe("`)) {
        break;
      }
      if (!inBlock) continue;
      const match = line.match(/^\s*it\("([^"]+)"/);
      if (match) names.push(match[1]);
    }

    const chunkSize = 2;
    for (let index = 0; index < names.length; index += chunkSize) {
      const chunk = names
        .slice(index, index + chunkSize)
        .map((name) => name.replace(/[.*+?^${}()|[\]\\]/g, "\\$&"))
        .join("|");
      console.log(chunk);
    }
  '
)"
while IFS= read -r chunk_pattern; do
  [[ -z "${chunk_pattern}" ]] && continue
  chunk_index=$((chunk_index + 1))
  run_slice "WorkflowDetail calibration chunk ${chunk_index}" "${chunk_pattern}"
done <<< "${chunk_patterns}"

run_slice "WorkflowVersionDetail page" "WorkflowVersionDetail page"
