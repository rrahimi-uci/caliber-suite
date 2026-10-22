#!/usr/bin/env bash
#
# cookbook-run-all.sh - run every cookbook lab, one after another, from a clean state.
#
# For each lab it does exactly what that lab's README documents, and nothing else:
#
#   fresh stack (scripts/cookbook-env.sh up ..., with the README's variables, flags and --s3)
#   -> mint the token the way the README does
#   -> run the lab's SDK example script (the API path)
#   -> run the lab's Playwright spec (the UI path, playwright.labs.config.ts)
#   -> tear the stack and any helper process down
#
# and prints a table (lab, API, UI, seconds). The exit status is non-zero if any step failed.
#
# Usage:
#   scripts/cookbook-run-all.sh [--labs 01,02,...] [--tier simulated|standin]
#                               [--api-only | --ui-only] [--keep] [--journey]
#
#   --labs LIST    comma list of lab numbers (default: 01-16 and 18). Always run in lab order.
#   --tier T       simulated (default): the shipped simulated LLM and evaluator, which every lab
#                  is written for. standin: labs 01 and 10 only, behind their scripted
#                  OpenAI-compatible stand-in model servers (the other labs have no such tier).
#   --api-only     run only the SDK example scripts.
#   --ui-only      run only the Playwright specs.
#   --keep         leave the stack (and its helper processes) of the LAST lab running, and print
#                  how to use it. Earlier labs are still torn down: only one stack runs at a time.
#   --journey      one shared stack with every flag on; runs the alpha path in the order a new
#                  team would (18, 01, 02, 03, 07, 16; --labs overrides the list), then checks that
#                  /readiness, the audit log, the release timeline and Observability reflect all of
#                  it (scripts/cookbook-journey-check.py). With --keep the stack stays up.
#
# Prerequisites: caliber/.venv (make install), Node with `npm ci` done in caliber/caliber-ui and the
# Playwright chromium installed (cd caliber/caliber-ui && npx playwright install chromium), curl,
# perl. Lab 06 needs the all-MiniLM-L6-v2 embedding model in the Hugging Face cache; the runner
# warms the cache itself (online, once, only when the model is not already cached), so a cold
# machine works. After that the lab runs with HF_HUB_OFFLINE=1, as its README documents.
#
# Safety: it only ever starts stacks named ra-* (never touches any other stack, the Docker stack on
# port 5001, or ports 5000/5001/9000), runs one stack at a time (a lock refuses a second runner),
# redacts access tokens from every log it writes, and on exit (also on failure or Ctrl-C) stops its
# stack and every helper process it started. Logs: .tmp/cookbook-run-all/<timestamp>/ (latest ->).
# Environment: COOKBOOK_RUN_ALL_API_TIMEOUT / _UI_TIMEOUT / _UP_TIMEOUT (seconds per step).

set -o pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PY="${CALIBER_COOKBOOK_VENV:-$REPO_ROOT/caliber/.venv}/bin/python"
UI_DIR="$REPO_ROOT/caliber/caliber-ui"
ENVSH="$REPO_ROOT/scripts/cookbook-env.sh"
COOKBOOKS="$REPO_ROOT/docs-site/cookbooks"
RUN_ROOT="$REPO_ROOT/.tmp/cookbook-run-all"
ENV_ROOT="$REPO_ROOT/.tmp/cookbook-env"
ALL_LABS="01 02 03 04 05 06 07 08 09 10 11 12 13 14 15 16 18 19"
JOURNEY_LABS="18 01 02 03 07 16"
API_TIMEOUT="${COOKBOOK_RUN_ALL_API_TIMEOUT:-1800}"
UI_TIMEOUT="${COOKBOOK_RUN_ALL_UI_TIMEOUT:-2400}"
UP_TIMEOUT="${COOKBOOK_RUN_ALL_UP_TIMEOUT:-900}"

# Nothing from the caller's shell may leak into a lab (a stray CALIBER_TOKEN, provider keys, ...).
for v in $(compgen -e | grep -E '^(CALIBER_|OPENAI_|ANTHROPIC_|MLFLOW_|AWS_|COOKBOOK_[0-9]|LAB[0-9]|HF_HUB)' || true); do
  [[ "$v" == CALIBER_COOKBOOK_VENV ]] || unset "$v"
done

die() { printf 'cookbook-run-all: %s\n' "$*" >&2; exit 2; }
log() { printf '[%s] %s\n' "$(date +%H:%M:%S)" "$*"; }
redact() {
  perl -pe 'BEGIN{$|=1} s/calpat_[^\s"\x27\\]+/calpat_***/g; s/(Bearer\s+)[A-Za-z0-9._~+\/=-]{20,}/$1***/g; s/\bsk-[A-Za-z0-9_-]{20,}/sk-***/g'
}

# ---- arguments --------------------------------------------------------------------------------
LABS_ARG=""; TIER=simulated; WANT_API=1; WANT_UI=1; KEEP=0; JOURNEY=0
while [[ $# -gt 0 ]]; do
  case "$1" in
    --labs) LABS_ARG="${2:?--labs needs a list}"; shift 2 ;;
    --tier) TIER="${2:?--tier needs simulated or standin}"; shift 2 ;;
    --api-only) WANT_UI=0; shift ;;
    --ui-only) WANT_API=0; shift ;;
    --keep) KEEP=1; shift ;;
    --journey) JOURNEY=1; shift ;;
    -h|--help) sed -n '2,38p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) die "unknown argument: $1 (see --help)" ;;
  esac
done
[[ "$TIER" == simulated || "$TIER" == standin ]] || die "--tier must be simulated or standin"
[[ $WANT_API == 1 || $WANT_UI == 1 ]] || die "--api-only and --ui-only are mutually exclusive"

SELECTED=""
if [[ -n "$LABS_ARG" ]]; then
  requested=""
  for n in ${LABS_ARG//,/ }; do
    n="$(printf '%02d' "$((10#$n))" 2>/dev/null)" || die "bad lab number: $n"
    [[ " $ALL_LABS " == *" $n "* ]] || die "no lab $n (labs: $ALL_LABS)"
    requested="$requested $n"
  done
  if [[ $JOURNEY == 1 ]]; then SELECTED="${requested# }"
  else for n in $ALL_LABS; do [[ " $requested " == *" $n "* ]] && SELECTED="$SELECTED $n"; done; SELECTED="${SELECTED# }"; fi
elif [[ $JOURNEY == 1 ]]; then SELECTED="$JOURNEY_LABS"
else SELECTED="$ALL_LABS"; fi
if [[ $TIER == standin ]]; then
  kept=""; for n in $SELECTED; do [[ "$n" == 01 || "$n" == 10 ]] && kept="$kept $n"; done
  SELECTED="${kept# }"; [[ -n "$SELECTED" ]] || die "--tier standin only has labs 01 and 10"
fi

# ---- preflight --------------------------------------------------------------------------------
[[ -x "$PY" ]] || die "no virtualenv at ${PY%/bin/python} (run 'make install')"
command -v curl >/dev/null || die "curl is required"
command -v perl >/dev/null || die "perl is required"
if [[ $WANT_UI == 1 ]]; then
  command -v node >/dev/null && command -v npx >/dev/null || die "Node (node, npx) is required for the UI specs"
  [[ -d "$UI_DIR/node_modules" ]] || die "run 'npm ci' in caliber/caliber-ui first"
  (cd "$UI_DIR" && node -e 'const {chromium}=require("@playwright/test");process.exit(require("fs").existsSync(chromium.executablePath())?0:1)') 2>/dev/null \
    || die "Playwright chromium is missing: cd caliber/caliber-ui && npx playwright install chromium"
fi

mkdir -p "$RUN_ROOT"
if ! mkdir "$RUN_ROOT/.lock" 2>/dev/null; then
  other="$(cat "$RUN_ROOT/.lock/pid" 2>/dev/null || true)"
  if [[ -n "$other" ]] && kill -0 "$other" 2>/dev/null; then die "another run is in progress (pid $other); one lab stack at a time"; fi
  rm -rf "$RUN_ROOT/.lock"; mkdir "$RUN_ROOT/.lock" || die "cannot take the lock"
fi
echo $$ >"$RUN_ROOT/.lock/pid"
RUN_DIR="$RUN_ROOT/$(date +%Y%m%d-%H%M%S)"
mkdir -p "$RUN_DIR"; ln -sfn "$RUN_DIR" "$RUN_ROOT/latest"

# ---- process hygiene ---------------------------------------------------------------------------
HELPER_PIDS=(); CURRENT_STACK=""; STEP_PID=""; KEPT=0
kill_tree() { local p="$1" c; for c in $(pgrep -P "$p" 2>/dev/null); do kill_tree "$c"; done; kill -TERM "$p" 2>/dev/null; return 0; }
stop_helpers() {
  local pid
  for pid in ${HELPER_PIDS[@]+"${HELPER_PIDS[@]}"}; do kill_tree "$pid"; done
  sleep 1
  for pid in ${HELPER_PIDS[@]+"${HELPER_PIDS[@]}"}; do kill -9 "$pid" 2>/dev/null || true; done
  HELPER_PIDS=()
}
stop_stack() { [[ -n "$CURRENT_STACK" ]] && "$ENVSH" down "$CURRENT_STACK" >/dev/null 2>&1; CURRENT_STACK=""; return 0; }
cleanup() {
  trap - EXIT INT TERM
  local c
  if [[ $KEPT == 0 ]]; then
    # everything this run started (a step, its browser, a stand-in server), then the stack
    for c in $(pgrep -P $$ 2>/dev/null); do [[ "$c" == "${CAFFEINATE_PID:-}" ]] || kill_tree "$c"; done
    stop_stack; stop_helpers
  elif [[ -n "$STEP_PID" ]]; then kill_tree "$STEP_PID"; fi
  [[ -n "${CAFFEINATE_PID:-}" ]] && kill "$CAFFEINATE_PID" 2>/dev/null
  rm -rf "$RUN_ROOT/.lock"
}
trap cleanup EXIT
trap 'log "interrupted"; exit 130' INT TERM
if command -v caffeinate >/dev/null; then caffeinate -dimsu -w $$ >/dev/null 2>&1 & CAFFEINATE_PID=$!; disown "$CAFFEINATE_PID" 2>/dev/null; fi

port_free() { "$PY" -c 'import socket,sys; s=socket.socket(); s.settimeout(0.3); sys.exit(1 if s.connect_ex(("127.0.0.1",int(sys.argv[1]))) == 0 else 0)' "$1"; }
wait_port() { local i; for ((i = 0; i < 30; i++)); do port_free "$1" || return 0; sleep 1; done; return 1; }
start_helper() { # start_helper NAME PORT cmd...
  local name="$1" port="$2"; shift 2
  port_free "$port" || { log "port $port is already in use; the $name stand-in needs it"; return 1; }
  "$@" >"$RUN_DIR/helper-$name.log" 2>&1 &
  HELPER_PIDS+=($!)
  wait_port "$port" || { log "the $name stand-in did not start on port $port (see $RUN_DIR/helper-$name.log)"; return 1; }
}

# run_step LOG TIMEOUT cmd...   (cmd is a function or program; output redacted into LOG)
run_step() {
  local logf="$1" tmo="$2" start=$SECONDS rc; shift 2
  ( set -o pipefail; "$@" 2>&1 | redact >"$logf" ) &
  STEP_PID=$!
  STEP_TIMED_OUT=0
  while kill -0 "$STEP_PID" 2>/dev/null; do
    if (( SECONDS - start > tmo )); then
      kill_tree "$STEP_PID"; STEP_TIMED_OUT=1; sleep 2; break
    fi
    sleep 2
  done
  wait "$STEP_PID"; rc=$?
  STEP_PID=""
  [[ $STEP_TIMED_OUT == 1 ]] && { printf '\nTIMED OUT after %ss\n' "$tmo" >>"$logf"; return 124; }
  return $rc
}
tail_log() { printf '    ---- last lines of %s\n' "${1#$REPO_ROOT/}"; tail -n "${2:-25}" "$1" | sed 's/^/    | /'; }

# ---- per-lab configuration (taken from each lab README's setup section) -----------------------
# Sets: LAB_TITLE LAB_FLAGS LAB_S3 LAB_UP_ENV(array) LAB_HELPERS LAB_TOKEN LAB_API_CWD LAB_API_ARGV
#       (array) LAB_UI LAB_UI_ENV(array) LAB_TIER_STACK
ARIA_USERS="admin,olga"
lab_config() {
  local n="$1" sdk="$REPO_ROOT/sdk/caliber-sdk/examples/cookbooks"
  LAB_TITLE=""; LAB_FLAGS="cookbooks"; LAB_S3=0; LAB_UP_ENV=(); LAB_HELPERS=""; LAB_TOKEN="plain:lab$n"
  LAB_API_CWD="."; LAB_API_ARGV=(); LAB_API_ENV=(); LAB_UI=""; LAB_UI_ENV=(); LAB_REAL=0
  case "$n" in
    01) LAB_TITLE="prompt regression"; LAB_TOKEN="plain:cookbook-01"
        LAB_API_ARGV=("$sdk/cookbook_01_trustworthy_intake_classifier.py"); LAB_UI="01-prompt-release-loop.spec.ts"
        if [[ $TIER == standin ]]; then
          LAB_HELPERS="standin01"; LAB_REAL=1
          LAB_UP_ENV=(CALIBER_LLM_PROVIDER=openai OPENAI_API_KEY=stand-in ANTHROPIC_API_KEY=stand-in
                      OPENAI_BASE_URL=http://127.0.0.1:8765/v1 CALIBER_EVAL_PROVIDER=mlflow)
          LAB_API_ARGV+=(--skip-release --expect-regression); LAB_UI="01b-evaluation-regression.spec.ts"
        fi ;;
    02) LAB_TITLE="skill packaging"; LAB_TOKEN="plain:lab02-admin"
        LAB_API_ARGV=(-m examples.cookbooks.cookbook_02_precision_skills); LAB_UI="02-skill-trigger-packaging.spec.ts" ;;
    03) LAB_TITLE="tool hardening"
        LAB_UP_ENV=(CALIBER_APPROVER_USERS=admin,approver1 CALIBER_REGISTERED_TOOL_SANDBOX_TIMEOUT_SECONDS=5)
        LAB_API_CWD="sdk/caliber-sdk"; LAB_API_ARGV=(-m examples.cookbooks.cookbook_03_policy_safe_decision_tool)
        LAB_UI="03-tool-hardening.spec.ts" ;;
    04) LAB_TITLE="document extraction"; LAB_S3=1; LAB_TOKEN="scoped04"
        LAB_UP_ENV=(CALIBER_APPROVER_USERS=admin,reviewer CALIBER_OPERATOR_USERS=admin,reviewer)
        LAB_API_ARGV=(-m examples.cookbooks.cookbook_04_document_to_json_pipeline); LAB_UI="04-document-extraction.spec.ts" ;;
    05) LAB_TITLE="MCP governance"; LAB_FLAGS="integrations,cookbooks"
        LAB_UP_ENV=("CALIBER_MCP_STDIO_PYTHON_SCRIPT_ALLOWLIST=$COOKBOOKS/05-mcp-connectivity-governance-lab/assets/server/repo_tracker_mcp.py"
                    CALIBER_OPERATOR_USERS=admin,lab05-operator CALIBER_APPROVER_USERS=admin,lab05-approver)
        LAB_API_CWD="sdk/caliber-sdk"; LAB_API_ARGV=(-m examples.cookbooks.cookbook_05_governed_tool_connectivity)
        LAB_UI="05-mcp-governance.spec.ts" ;;
    06) LAB_TITLE="knowledge retrieval"; LAB_S3=1; LAB_UP_ENV=(HF_HUB_OFFLINE=1)
        LAB_API_ARGV=(-m examples.cookbooks.cookbook_06_grounded_knowledge_assistant); LAB_UI="06-knowledge-retrieval.spec.ts" ;;
    07) LAB_TITLE="support triage"
        LAB_UP_ENV=(CALIBER_OPERATOR_USERS=admin,lena,sam CALIBER_APPROVER_USERS=admin,lena
                    CALIBER_RELEASE_REQUIRE_HUMAN_APPROVAL_FOR_ENVIRONMENT_CLASSES=staging)
        LAB_API_ARGV=(-m examples.cookbooks.cookbook_07_support_triage_copilot); LAB_UI="07-support-triage.spec.ts" ;;
    08) LAB_TITLE="incident response"
        LAB_UP_ENV=(CALIBER_OPERATOR_USERS=admin,lena,sam CALIBER_APPROVER_USERS=admin,lena
                    CALIBER_RELEASE_REQUIRE_HUMAN_APPROVAL_FOR_ENVIRONMENT_CLASSES=production
                    CALIBER_RELEASE_REQUIRE_GRADED_EXECUTOR_FOR_ENVIRONMENT_CLASSES=)
        LAB_API_ARGV=(-m examples.cookbooks.cookbook_08_incident_response_copilot); LAB_UI="08-incident-response.spec.ts" ;;
    09) LAB_TITLE="workflow debugger"; LAB_HELPERS="acct"; LAB_UP_ENV=(CALIBER_EGRESS_ALLOWED_HOSTS=127.0.0.1)
        LAB_API_ARGV=("$sdk/cookbook_09_self_healing_workflows.py"); LAB_UI="09-workflow-debugger.spec.ts" ;;
    10) LAB_TITLE="judge certification"
        LAB_API_ARGV=("$sdk/cookbook_10_trustworthy_evaluation.py"); LAB_UI="10-judge-certification.spec.ts"
        LAB_UI_ENV=(CALIBER_LAB10_MODEL=simulated)
        if [[ $TIER == standin ]]; then
          LAB_HELPERS="standin10"; LAB_REAL=1
          LAB_UP_ENV=(CALIBER_LLM_PROVIDER=openai CALIBER_EVAL_PROVIDER=mlflow OPENAI_API_KEY=stand-in ANTHROPIC_API_KEY=stand-in
                      OPENAI_BASE_URL=http://127.0.0.1:8770/v1 OPENAI_API_BASE=http://127.0.0.1:8770/v1)
          LAB_API_ARGV+=(--expect-stand-in); LAB_UI_ENV=()
        fi ;;
    11) LAB_TITLE="release signoff"; LAB_FLAGS="release_candidates,cookbooks"
        LAB_UP_ENV=(CALIBER_OPERATOR_USERS=admin,lab11-operator)
        LAB_API_CWD="sdk/caliber-sdk"; LAB_API_ARGV=(-m examples.cookbooks.cookbook_11_release_signoff_factory)
        LAB_UI="11-release-signoff.spec.ts" ;;
    12) LAB_TITLE="Aria evaluation harness"; LAB_FLAGS="cookbooks,aria"; LAB_UP_ENV=(CALIBER_OPERATOR_USERS=$ARIA_USERS)
        LAB_API_ARGV=(-m examples.cookbooks.cookbook_12_aria_evaluation_harness); LAB_UI="12-aria-evaluation-harness.spec.ts" ;;
    13) LAB_TITLE="Aria review queue"; LAB_FLAGS="cookbooks,aria"; LAB_UP_ENV=(CALIBER_OPERATOR_USERS=$ARIA_USERS)
        LAB_API_ARGV=(-m examples.cookbooks.cookbook_13_aria_review_queue); LAB_UI="13-aria-review-queue.spec.ts" ;;
    14) LAB_TITLE="Aria governance kit"; LAB_FLAGS="cookbooks,aria"; LAB_UP_ENV=(CALIBER_OPERATOR_USERS=$ARIA_USERS)
        LAB_API_ARGV=(-m examples.cookbooks.cookbook_14_aria_governance_starter_kit); LAB_UI="14-aria-governance-starter-kit.spec.ts" ;;
    15) LAB_TITLE="Aria recalibrate loop"; LAB_FLAGS="cookbooks,aria"; LAB_UP_ENV=(CALIBER_OPERATOR_USERS=$ARIA_USERS)
        LAB_API_ARGV=(-m examples.cookbooks.cookbook_15_aria_triage_recalibrate_loop); LAB_UI="15-aria-triage-recalibrate-loop.spec.ts" ;;
    16) LAB_TITLE="observability triage"; LAB_HELPERS="acct"; LAB_UP_ENV=(CALIBER_EGRESS_ALLOWED_HOSTS=127.0.0.1)
        LAB_API_ARGV=("$sdk/cookbook_16_observability_triage.py"); LAB_UI="16-observability-triage.spec.ts" ;;
    18) LAB_TITLE="projects, roles, tokens"; LAB_TOKEN="bearer18"
        LAB_UP_ENV=(CALIBER_ADMIN_USERS=admin CALIBER_OPERATOR_USERS=admin,maya,omar,priya,sam,quinn,rosa,victor,nora
                    CALIBER_APPROVER_USERS=admin,maya,omar,quinn)
        LAB_API_ARGV=(-m examples.cookbooks.cookbook_18_projects_roles_tokens_audit); LAB_UI="18-projects-roles-tokens-audit.spec.ts" ;;
    19) LAB_TITLE="OpenAPI governance"; LAB_FLAGS="integrations,cookbooks"; LAB_HELPERS="ticket19"
        export OPENAPI_TICKET_TOKEN="${OPENAPI_TICKET_TOKEN:-lab19-ticket-token}"
        export TICKET_API_LEDGER="$RUN_DIR/lab19-ticket-ledger.jsonl"
        LAB_UP_ENV=(CALIBER_EGRESS_ALLOWED_HOSTS=127.0.0.1 "OPENAPI_TICKET_TOKEN=$OPENAPI_TICKET_TOKEN"
                    CALIBER_OPERATOR_USERS=admin,lab19-operator,lab19-reviewer CALIBER_APPROVER_USERS=admin,lab19-reviewer)
        LAB_API_CWD="sdk/caliber-sdk"; LAB_API_ARGV=(-m examples.cookbooks.cookbook_19_openapi_integration_governance)
        LAB_UI="19-openapi-governance.spec.ts" ;;
    *) die "no configuration for lab $n" ;;
  esac
  LAB_STACK="ra-lab$n"; [[ $TIER == standin ]] && LAB_STACK="ra-lab${n}m"
  return 0
}

start_lab_helpers() {
  local h
  for h in $1; do
    case "$h" in
      acct) start_helper account-service 8780 python3 "$COOKBOOKS/09-workflow-debugger-self-healing-lab/assets/upstream/account_service.py" 8780 || return 1 ;;
      standin01) start_helper standin-01 8765 python3 "$COOKBOOKS/01-prompt-regression-lab/assets/stand-in-model/server.py" --port 8765 || return 1 ;;
      standin10) start_helper standin-10 8770 python3 "$COOKBOOKS/10-judge-certification-human-review-lab/assets/stand-in-model/stand_in_model_server.py" 8770 || return 1 ;;
      ticket19) start_helper ticket-api-19 8781 python3 "$COOKBOOKS/19-openapi-integration-governance-lab/assets/server/ticket_api.py" 8781 || return 1 ;;
    esac
  done
}

# ---- stack lifecycle and tokens ----------------------------------------------------------------
fresh_name_ok() { [[ "$1" == ra-* ]]; }
stack_up() { # stack_up NAME FLAGS S3 [VAR=VALUE...]
  local name="$1" flags="$2" s3="$3"; shift 3
  fresh_name_ok "$name" || die "refusing to manage a stack not named ra-*: $name"
  "$ENVSH" down "$name" >/dev/null 2>&1 || true
  rm -rf "${ENV_ROOT:?}/$name"
  local args=(up "$name" --flags "$flags")
  [[ "$s3" == 1 ]] && args+=(--s3)
  [[ ${LAB_REAL:-0} == 1 ]] && args+=(--real-llm)
  env "$@" "$ENVSH" "${args[@]}"
}
stack_vars() { source "$ENV_ROOT/$1/env.sh"; }   # always called inside a subshell

mint_token() { # prints the token on stdout (and nothing else); run in a subshell with env.sh sourced
  local kind="$1" jar="$CALIBER_STATE_DIR/runall.jar" tok
  local H='content-type: application/json' login='{"user_id":"admin","password":"admin"}'
  local pick='import json,sys; print(json.load(sys.stdin)["data"]["token"])'
  case "$kind" in
    plain:*)
      curl -sS -c "$jar" -X POST "$CALIBER_API/auth/login" -H "$H" -d "$login" >/dev/null || return 1
      tok="$(curl -sS -b "$jar" -X POST "$CALIBER_API/auth/tokens" -H "$H" -d "{\"name\":\"${kind#plain:}\"}" | "$PY" -c "$pick")" ;;
    scoped04)
      curl -sS -c "$jar" -X POST "$CALIBER_API/auth/login" -H "$H" -d "$login" >/dev/null || return 1
      curl -sS -b "$jar" -X POST "$CALIBER_API/auth/accounts" -H "$H" -d '{"user_id":"reviewer","password":"reviewer-pass-123"}' >/dev/null || return 1
      tok="$(curl -sS -b "$jar" -X POST "$CALIBER_API/auth/tokens" -H "$H" \
        -d '{"name":"cookbook-04","scopes":["caliber.admin","caliber.operator","caliber.approver"]}' | "$PY" -c "$pick")" ;;
    bearer18)
      local session
      session="$(curl -s -c - -o /dev/null -X POST "$CALIBER_API/auth/login" -H "$H" -d "$login" | awk '/caliber_session/ {print $7}')"
      tok="$(curl -s -X POST "$CALIBER_API/auth/tokens" -H "Authorization: Bearer $session" -H "$H" -d '{"name":"lab18-admin"}' | "$PY" -c "$pick")" ;;
    *) return 1 ;;
  esac
  [[ "$tok" == calpat_* ]] || return 1
  printf '%s' "$tok"
}
readiness_check() { # /readiness must say ready: true; lab 04 also needs the object store and the queue (its README checks both)
  local out
  out="$(curl -sS "$CALIBER_API/readiness" -H "Authorization: Bearer $CALIBER_TOKEN")" || return 1
  printf '%s' "$out" | "$PY" -c '
import json, sys
d = json.load(sys.stdin)["data"]
print("readiness: ready=%s simulated=%s" % (d["ready"], d.get("simulated")))
need = [c for c in d.get("checks", []) if c["name"] in ("object_store", "workflow_queue") and "ra-lab04" in sys.argv[1:]]
bad = [c["name"] + ": " + str(c.get("detail")) for c in need if not c.get("ready", True)]
if not d["ready"] or bad:
    print("NOT READY:", bad); sys.exit(1)' "$LAB_STACK"
}

# step bodies (run inside run_step, i.e. in a subshell)
step_up() { stack_up "$LAB_STACK" "$LAB_FLAGS" "$LAB_S3" ${LAB_UP_ENV[@]+"${LAB_UP_ENV[@]}"}; }
step_api() {
  stack_vars "$LAB_STACK"; export CALIBER_TOKEN="$TOKEN"
  export PYTHONPATH="$REPO_ROOT/sdk/caliber-sdk"
  local kv; for kv in ${LAB_API_ENV[@]+"${LAB_API_ENV[@]}"}; do export "$kv"; done
  readiness_check || exit 1
  cd "$REPO_ROOT/$LAB_API_CWD" && exec "$PY" "${LAB_API_ARGV[@]}"
}
step_ui() {
  stack_vars "$LAB_STACK"; export CALIBER_TOKEN="$TOKEN"
  local kv; for kv in ${LAB_UI_ENV[@]+"${LAB_UI_ENV[@]}"}; do export "$kv"; done
  export CALIBER_LAB_BASE_URL="$CALIBER_URL/caliber"
  cd "$UI_DIR" && exec npx playwright test --config=playwright.labs.config.ts "e2e/cookbooks/labs/$LAB_UI" --output="$RUN_DIR/pw-lab$LAB_NUM"
}
save_server_log() { # save_server_log NAME DEST : the last lines of the stack's server log, redacted
  local f="$ENV_ROOT/$1/caliber.log"
  [[ -f "$f" ]] && tail -n 400 "$f" | redact >"$2"
  return 0
}
mint_into_TOKEN() { # mints in a subshell so the token only ever lives in this shell's memory
  TOKEN="$( (stack_vars "$1" >/dev/null; mint_token "$2") 2>/dev/null )"
  [[ "$TOKEN" == calpat_* ]]
}

# ---- the embedding model lab 06 builds with ------------------------------------------------------
EMBED_MODEL="sentence-transformers/all-MiniLM-L6-v2"
EMBED_WARM_PY='import sys; from sentence_transformers import SentenceTransformer as S; S(sys.argv[1])'
EMBED_MODEL_OK=1
# Lab 06 runs offline (HF_HUB_OFFLINE=1) so a server start never asks huggingface.co whether the model is current.
# That is only right once the model is cached, so: if an offline load works, the cache is warm; if not, load it
# once online (this downloads about 90 MB). A host that is offline AND cold cannot run lab 06: say so, plainly.
warm_embedding_model() {
  if HF_HUB_OFFLINE=1 "$PY" -c "$EMBED_WARM_PY" "$EMBED_MODEL" >"$RUN_DIR/embedding-model.log" 2>&1; then
    log "embedding model $EMBED_MODEL: already cached"; return 0
  fi
  log "embedding model $EMBED_MODEL: not cached, downloading it once (about 90 MB)"
  if "$PY" -c "$EMBED_WARM_PY" "$EMBED_MODEL" >>"$RUN_DIR/embedding-model.log" 2>&1; then
    log "embedding model $EMBED_MODEL: cached"; return 0
  fi
  log "embedding model $EMBED_MODEL: could not be downloaded (lab 06 needs it; see ${RUN_DIR#$REPO_ROOT/}/embedding-model.log)"
  tail_log "$RUN_DIR/embedding-model.log" 8
  return 1
}

# ---- results -----------------------------------------------------------------------------------
ROWS=(); FAILED=0
add_row() { # add_row LAB NAME API UI SECONDS NOTES
  ROWS+=("$1|$2|$3|$4|$5|$6"); [[ "$3" == FAIL || "$4" == FAIL ]] && FAILED=1; return 0
}
print_table() {
  local lab name api ui secs notes
  printf '\n%-5s %-26s %-6s %-6s %8s  %s\n' LAB NAME API UI SECONDS NOTES
  for r in ${ROWS[@]+"${ROWS[@]}"}; do
    IFS='|' read -r lab name api ui secs notes <<<"$r"
    printf '%-5s %-26s %-6s %-6s %8s  %s\n' "$lab" "$name" "$api" "$ui" "$secs" "$notes"
  done
  printf '\nlogs: %s\n' "${RUN_DIR#$REPO_ROOT/}"
}

# run the API and UI steps of the lab described by lab_config; sets API_RES and UI_RES
run_lab_steps() { # run_lab_steps NUM STACK
  local n="$1" stack="$2" rc
  LAB_NUM="$n"; API_RES=SKIP; UI_RES=SKIP; NOTE=""
  if [[ $WANT_API == 1 ]]; then
    local api_name="${LAB_API_ARGV[0]}"; [[ "$api_name" == -m ]] && api_name="${LAB_API_ARGV[1]}"
    log "lab $n: API path ($(basename "$api_name"))"
    run_step "$RUN_DIR/lab$n.api.log" "$API_TIMEOUT" step_api; rc=$?
    if [[ $rc == 0 ]]; then API_RES=PASS; else API_RES=FAIL; NOTE="$NOTE api(rc=$rc)"; tail_log "$RUN_DIR/lab$n.api.log"; fi
  fi
  if [[ $WANT_UI == 1 ]]; then
    log "lab $n: UI path ($LAB_UI)"
    run_step "$RUN_DIR/lab$n.ui.log" "$UI_TIMEOUT" step_ui; rc=$?
    if [[ $rc == 0 ]]; then UI_RES=PASS; else UI_RES=FAIL; NOTE="$NOTE ui(rc=$rc)"; tail_log "$RUN_DIR/lab$n.ui.log" 30; fi
  fi
  [[ "$API_RES" == FAIL || "$UI_RES" == FAIL ]] && save_server_log "$stack" "$RUN_DIR/lab$n.server.log"
  return 0
}

run_lab() { # run_lab NUM IS_LAST
  local n="$1" last="$2" t0=$SECONDS rc
  lab_config "$n"
  if [[ "$n" == 06 && $EMBED_MODEL_OK != 1 ]]; then
    add_row "$n" "$LAB_TITLE" FAIL FAIL 0 "setup: embedding model not cached"; return 0
  fi
  log "=== lab $n ($LAB_TITLE)  stack $LAB_STACK  flags $LAB_FLAGS$([[ $LAB_S3 == 1 ]] && echo ' +s3')$([[ $TIER == standin ]] && echo '  tier standin')"
  if ! start_lab_helpers "$LAB_HELPERS"; then
    stop_helpers; add_row "$n" "$LAB_TITLE" FAIL FAIL "$((SECONDS - t0))" "helper"; return 0
  fi
  CURRENT_STACK="$LAB_STACK"
  run_step "$RUN_DIR/lab$n.up.log" "$UP_TIMEOUT" step_up; rc=$?
  if [[ $rc != 0 ]]; then
    log "lab $n: the stack did not come up"; tail_log "$RUN_DIR/lab$n.up.log"
    save_server_log "$LAB_STACK" "$RUN_DIR/lab$n.server.log"
    stop_stack; stop_helpers; add_row "$n" "$LAB_TITLE" FAIL FAIL "$((SECONDS - t0))" "setup: stack up"; return 0
  fi
  if ! mint_into_TOKEN "$LAB_STACK" "$LAB_TOKEN"; then
    log "lab $n: could not mint the token the README describes"
    stop_stack; stop_helpers; add_row "$n" "$LAB_TITLE" FAIL FAIL "$((SECONDS - t0))" "setup: token"; return 0
  fi
  run_lab_steps "$n" "$LAB_STACK"
  TOKEN=""
  if [[ $KEEP == 1 && $last == 1 ]]; then
    KEPT=1
    log "lab $n: stack kept: source ${ENV_ROOT#$REPO_ROOT/}/$LAB_STACK/env.sh  (stop it with: scripts/cookbook-env.sh down $LAB_STACK; helpers: kill ${HELPER_PIDS[*]-})"
  else
    stop_stack; stop_helpers
    if [[ "$API_RES" != FAIL && "$UI_RES" != FAIL ]]; then rm -rf "${ENV_ROOT:?}/$LAB_STACK"
    else log "lab $n: stack state kept for debugging in ${ENV_ROOT#$REPO_ROOT/}/$LAB_STACK"; fi
  fi
  add_row "$n" "$LAB_TITLE" "$API_RES" "$UI_RES" "$((SECONDS - t0))" "${NOTE# }"
  log "lab $n: API $API_RES, UI $UI_RES in $((SECONDS - t0))s"
}

# ---- the journey: one stack, every flag, the alpha path in the order a new team follows it -------
JOURNEY_STACK="ra-journey"
journey() {
  local t0=$SECONDS rc n first_lab_cfg helpers="" lab_ui_env=()
  # one stack for all of them: the union of what each lab's README exports before `up`
  local up_env=(
    CALIBER_ADMIN_USERS=admin
    CALIBER_OPERATOR_USERS=admin,maya,omar,priya,sam,quinn,rosa,victor,nora,lena
    CALIBER_APPROVER_USERS=admin,maya,omar,quinn,approver1,lena
    CALIBER_EGRESS_ALLOWED_HOSTS=127.0.0.1
    CALIBER_REGISTERED_TOOL_SANDBOX_TIMEOUT_SECONDS=5
    CALIBER_RELEASE_REQUIRE_HUMAN_APPROVAL_FOR_ENVIRONMENT_CLASSES=staging
  )
  for n in $SELECTED; do lab_config "$n"; [[ " $helpers " != *" $LAB_HELPERS "* && -n "$LAB_HELPERS" ]] && helpers="$helpers $LAB_HELPERS"; done
  log "=== journey: labs $SELECTED on one stack ($JOURNEY_STACK, all flags)"
  start_lab_helpers "$helpers" || { stop_helpers; add_row journey "shared stack" FAIL FAIL "$((SECONDS - t0))" "helper"; return 0; }
  CURRENT_STACK="$JOURNEY_STACK"
  LAB_STACK="$JOURNEY_STACK"; LAB_FLAGS="all"; LAB_S3=0; LAB_REAL=0; LAB_UP_ENV=("${up_env[@]}")
  run_step "$RUN_DIR/journey.up.log" "$UP_TIMEOUT" step_up; rc=$?
  if [[ $rc != 0 ]]; then
    tail_log "$RUN_DIR/journey.up.log"; stop_stack; stop_helpers
    add_row journey "shared stack" FAIL FAIL "$((SECONDS - t0))" "setup: stack up"; return 0
  fi
  for n in $SELECTED; do
    local lt=$SECONDS
    lab_config "$n"; LAB_STACK="$JOURNEY_STACK"
    # lab 18's roster already holds `sam`, with its own password; lab 07 uses the same account
    if [[ "$n" == 07 ]]; then LAB_API_ENV=(COOKBOOK_07_PASSWORD=lab18-demo-passphrase); LAB_UI_ENV=("${LAB_API_ENV[@]}"); fi
    log "=== journey step: lab $n ($LAB_TITLE)"
    if ! mint_into_TOKEN "$JOURNEY_STACK" "$LAB_TOKEN"; then
      add_row "$n" "$LAB_TITLE" FAIL FAIL "$((SECONDS - lt))" "setup: token"; continue
    fi
    run_lab_steps "$n" "$JOURNEY_STACK"
    TOKEN=""
    add_row "$n" "$LAB_TITLE" "$API_RES" "$UI_RES" "$((SECONDS - lt))" "${NOTE# }"
  done
  log "=== journey: cross-lab checks (readiness, audit log, release timeline, observability)"
  JOURNEY_RES=PASS
  mint_into_TOKEN "$JOURNEY_STACK" "plain:journey-check" || JOURNEY_RES=FAIL
  if [[ $JOURNEY_RES == PASS ]]; then
    run_step "$RUN_DIR/journey.check.log" 600 journey_check_step || JOURNEY_RES=FAIL
    cat "$RUN_DIR/journey.check.log"
  fi
  TOKEN=""
  save_server_log "$JOURNEY_STACK" "$RUN_DIR/journey.server.log"
  add_row journey "cross-lab checks" "$JOURNEY_RES" SKIP "$((SECONDS - t0))" "total for the whole journey"
  if [[ $KEEP == 1 ]]; then
    KEPT=1; log "journey stack kept: source ${ENV_ROOT#$REPO_ROOT/}/$JOURNEY_STACK/env.sh  (stop it with: scripts/cookbook-env.sh down $JOURNEY_STACK; helpers: kill ${HELPER_PIDS[*]-})"
  else stop_stack; stop_helpers; fi
}
journey_check_step() {
  stack_vars "$JOURNEY_STACK"; export CALIBER_TOKEN="$TOKEN"
  export PYTHONPATH="$REPO_ROOT/sdk/caliber-sdk"
  exec "$PY" "$REPO_ROOT/scripts/cookbook-journey-check.py" --labs "${SELECTED// /,}"
}

# ---- main --------------------------------------------------------------------------------------
T_START=$SECONDS
log "cookbook-run-all: labs [$SELECTED] tier $TIER $([[ $WANT_API == 0 ]] && echo '(ui only) ')$([[ $WANT_UI == 0 ]] && echo '(api only) ')$([[ $JOURNEY == 1 ]] && echo '(journey) ')-> ${RUN_DIR#$REPO_ROOT/}"
log "building the web bundle if its sources changed"
"$ENVSH" build-ui >"$RUN_DIR/build-ui.log" 2>&1 || { tail_log "$RUN_DIR/build-ui.log"; die "web bundle build failed"; }
if [[ " $SELECTED " == *" 06 "* ]]; then warm_embedding_model || EMBED_MODEL_OK=0; fi
if [[ $JOURNEY == 1 ]]; then
  journey
else
  count=0; total=$(wc -w <<<"$SELECTED")
  for n in $SELECTED; do count=$((count + 1)); last=0; [[ $count == "$total" ]] && last=1; run_lab "$n" "$last"; done
fi
print_table
printf 'total: %ss\n' "$((SECONDS - T_START))"
if [[ $FAILED == 0 ]]; then echo "RESULT: every step passed"; exit 0; fi
echo "RESULT: FAILED (see the logs above)"; exit 1
