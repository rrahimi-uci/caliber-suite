#!/usr/bin/env bash
# Render the Allure report from the suite's result dirs.
#
# Emitting allure-results/ (the test adapters) never needs Java — only this
# rendering step does. To make report generation work in EVERY CALIBER setup,
# this prefers a local JRE and otherwise falls back to a Dockerized JRE running
# the Allure distribution that ``allure-commandline`` already vendored into
# node_modules. So any host with *either* Java or Docker can produce the report.
#
# Usage: allure-report.sh [generate|serve]   (default: generate, then open)
set -euo pipefail

UI_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"   # caliber/caliber-ui
SUITE_DIR="$(cd "$UI_DIR/../.." && pwd)"                     # caliber-suite
ACTION="${1:-generate}"
REPORT_DIR="$UI_DIR/allure-report"
ALLURE_BIN="$UI_DIR/node_modules/allure-commandline/dist/bin/allure"
# Fixed host/port so the Settings "Open Allure report" link (default
# http://localhost:5252) reliably reaches the served report.
ALLURE_HOST="${ALLURE_HOST:-127.0.0.1}"
ALLURE_PORT="${ALLURE_PORT:-5252}"

# Collect the result dirs that exist (frontend unit + e2e share one; backend
# writes its own under caliber/).
RESULTS=()
[ -d "$UI_DIR/allure-results" ] && RESULTS+=("$UI_DIR/allure-results")
[ -d "$SUITE_DIR/caliber/allure-results" ] && RESULTS+=("$SUITE_DIR/caliber/allure-results")
if [ "${#RESULTS[@]}" -eq 0 ]; then
  cat >&2 <<'EOF'
No allure-results/ found. Run the tests first (they emit results without Java):
  cd caliber           && make test-allure        # backend (pytest)
  cd caliber/caliber-ui && npm test               # frontend unit (vitest)
  cd caliber/caliber-ui && npm run test:e2e       # e2e (playwright)
EOF
  exit 1
fi

if [ ! -x "$ALLURE_BIN" ]; then
  echo "Allure CLI not found at $ALLURE_BIN — run 'npm install' in caliber/caliber-ui." >&2
  exit 1
fi

# Probe a *working* JRE, not just a binary on PATH — macOS ships a /usr/bin/java
# stub that exists even with no runtime installed, so `command -v java` lies.
if java -version >/dev/null 2>&1; then
  echo "Rendering with local Java: $(java -version 2>&1 | head -1)"
  if [ "$ACTION" = "serve" ]; then
    echo "Serving on http://${ALLURE_HOST}:${ALLURE_PORT}"
    exec "$ALLURE_BIN" serve "${RESULTS[@]}" --host "$ALLURE_HOST" --port "$ALLURE_PORT"
  fi
  "$ALLURE_BIN" generate "${RESULTS[@]}" --clean -o "$REPORT_DIR"
  echo "Report generated: $REPORT_DIR/index.html"
  echo "Opening on http://${ALLURE_HOST}:${ALLURE_PORT}"
  exec "$ALLURE_BIN" open "$REPORT_DIR" --host "$ALLURE_HOST" --port "$ALLURE_PORT"
fi

if command -v docker >/dev/null 2>&1; then
  echo "No local Java — rendering with a Dockerized JRE (eclipse-temurin:21-jre)."
  # Translate host result dirs to their in-container paths under /work.
  REL_RESULTS=()
  for r in "${RESULTS[@]}"; do REL_RESULTS+=("/work/${r#"$SUITE_DIR"/}"); done
  docker run --rm \
    -u "$(id -u):$(id -g)" -e HOME=/tmp \
    -v "$SUITE_DIR:/work" -w /work \
    eclipse-temurin:21-jre \
    /work/caliber/caliber-ui/node_modules/allure-commandline/dist/bin/allure \
    generate "${REL_RESULTS[@]}" --clean -o "/work/caliber/caliber-ui/allure-report"
  echo "Report generated: $REPORT_DIR/index.html"
  echo "Open it with any static server, e.g.: npx --prefix \"$UI_DIR\" allure open allure-report"
  exit 0
fi

cat >&2 <<'EOF'
Allure report rendering needs a Java runtime (or Docker). Install one:
  macOS:          brew install --cask temurin      (or: brew install openjdk)
  Debian/Ubuntu:  sudo apt-get install -y default-jre
  Fedora/RHEL:    sudo dnf install -y java-21-openjdk-headless
  Windows:        choco install temurin            (or: scoop install temurin)
…or install Docker and re-run — this script will then render in a container.
EOF
exit 1
