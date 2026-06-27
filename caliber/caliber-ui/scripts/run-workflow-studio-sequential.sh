#!/usr/bin/env bash

set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "${ROOT_DIR}"

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
run_slice "ToolDetail page" "ToolDetail page"
