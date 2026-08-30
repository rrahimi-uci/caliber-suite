# CALIBER Suite — top-level developer Makefile.
#
# This wraps the existing shell scripts so every common action is a single
# make target run from the repo root.  Existing scripts are NOT modified
# beyond what's needed; this file is purely additive.
#
#   make setup   — one-time: create venv, install deps, build UI
#   make start   — start mlflow + mlflow-gateway + caliber
#   make stop    — stop all services
#   make dev     — hot-reload mode instructions
#   make test    — run Python + UI tests
#   make lint    — ruff + mypy + typecheck
#   make ui      — rebuild the SPA bundle
#   make status  — check if services are running
#   make logs    — tail all log files
#   make clean   — remove build artifacts, caches, databases
#   make reset   — stop + clean + setup (full nuke and rebuild)

VENV       := .venv
PY         := $(VENV)/bin/python
PIP        := $(VENV)/bin/pip
PLUGIN_DIR := caliber
UI_DIR     := $(PLUGIN_DIR)/caliber-ui
ENV_FILES  := $(if $(wildcard deploy/.env),--env-file deploy/.env,) $(if $(wildcard .env),--env-file .env,)
COMPOSE    := docker compose $(ENV_FILES) -f deploy/compose.yaml
CALIBER_SETUP_EXTRAS ?= dev,s3,postgres,ingest,ocr,knowledge

# Colors
GREEN  := \033[0;32m
YELLOW := \033[1;33m
RED    := \033[0;31m
CYAN   := \033[0;36m
NC     := \033[0m

.PHONY: help check setup start stop dev test test-all test-allure allure-report allure-publish allure lint ui build status logs clean reset \
        infra-up infra-build infra-down infra-logs infra-status infra-reset seed-graph \
        sdk plugin-sdk cli sdk-coverage

help: ## show available commands
	@echo ""
	@echo "$(GREEN)═══════════════════════════════════════════$(NC)"
	@echo "$(GREEN)          CALIBER Suite Commands           $(NC)"
	@echo "$(GREEN)═══════════════════════════════════════════$(NC)"
	@echo ""
	@echo "$(CYAN)Setup:$(NC)"
	@echo "  $(GREEN)make setup$(NC)    — one-time: create venv, install deps, build UI"
	@echo "  $(GREEN)make check$(NC)    — verify python3, node, npm are installed"
	@echo ""
	@echo "$(CYAN)Run (containers):$(NC)"
	@echo "  $(GREEN)make start$(NC)    — start the suite (UI → :5001/caliber/; clean app start + rebuild by default)"
	@echo "  $(GREEN)make stop$(NC)     — stop the suite"
	@echo "  $(GREEN)make dev$(NC)      — native hot-reload mode for plugin + UI (see instructions)"
	@echo "  $(GREEN)make status$(NC)   — show running containers"
	@echo "  $(GREEN)make logs$(NC)     — tail container logs"
	@echo ""
	@echo "$(CYAN)Infra (containers):$(NC)"
	@echo "  $(GREEN)make infra-up$(NC)    — start MinIO + Postgres (NATS=1 bus · APP=1 app tier)"
	@echo "  $(GREEN)make infra-build$(NC) — build the app-tier images (mlflow, mlflow-gateway, caliber)"
	@echo "  $(GREEN)make infra-down$(NC)  — stop the infra stack"
	@echo "  $(GREEN)make infra-status$(NC)— show running infra containers"
	@echo "  $(GREEN)make infra-logs$(NC)  — tail infra container logs"
	@echo "  $(GREEN)make infra-reset$(NC) — stop AND delete data volumes"
	@echo "  $(GREEN)make seed-graph$(NC)  — load a sample knowledge graph into AGE (graph console :8082)"
	@echo ""
	@echo "$(CYAN)Build:$(NC)"
	@echo "  $(GREEN)make ui$(NC)       — rebuild the SPA bundle"
	@echo "  $(GREEN)make build$(NC)    — alias for ui"
	@echo ""
	@echo "$(CYAN)Quality:$(NC)"
	@echo "  $(GREEN)make test$(NC)     — run Python + UI tests"
	@echo "  $(GREEN)make test-all$(NC) — run backend + unit + e2e tests (builds Allure report by default)"
	@echo "  $(GREEN)make lint$(NC)     — ruff + mypy + typecheck"
	@echo ""
	@echo "$(CYAN)Cleanup:$(NC)"
	@echo "  $(GREEN)make clean$(NC)    — remove build artifacts and caches"
	@echo "  $(GREEN)make reset$(NC)    — stop + clean + setup (full rebuild)"
	@echo ""
	@echo "$(GREEN)═══════════════════════════════════════════$(NC)"
	@echo ""

# ── Setup ────────────────────────────────────────────────────

check: ## verify python3, node, npm are installed
	@printf "  python3 ... " && command -v python3 >/dev/null 2>&1 \
		&& echo "$(GREEN)ok$(NC) ($$(python3 --version 2>&1))" \
		|| { echo "$(RED)MISSING$(NC)"; exit 1; }
	@printf "  node    ... " && command -v node >/dev/null 2>&1 \
		&& echo "$(GREEN)ok$(NC) ($$(node --version 2>&1))" \
		|| { echo "$(RED)MISSING$(NC)"; exit 1; }
	@printf "  npm     ... " && command -v npm >/dev/null 2>&1 \
		&& echo "$(GREEN)ok$(NC) ($$(npm --version 2>&1))" \
		|| { echo "$(RED)MISSING$(NC)"; exit 1; }
	@printf "  java    ... " && command -v java >/dev/null 2>&1 \
		&& echo "$(GREEN)ok$(NC) ($$(java -version 2>&1 | head -1))" \
		|| echo "$(YELLOW)optional$(NC) (only needed to render Allure reports; 'make allure' falls back to Docker)"
	@echo "$(GREEN)All required tools are installed.$(NC)"

setup: check ## one-time: create venv, install deps, build UI
	@echo ""
	@echo "$(CYAN)Creating virtual environment ...$(NC)"
	python3 -m venv $(VENV)
	$(PIP) install --upgrade pip -q
	@echo "$(CYAN)Installing Python dependencies ...$(NC)"
	$(PIP) install -e "./caliber[$(CALIBER_SETUP_EXTRAS)]" -q
	@echo "$(CYAN)Installing frontend dependencies ...$(NC)"
	cd $(UI_DIR) && npm install --silent
	@echo "$(CYAN)Building UI ...$(NC)"
	$(MAKE) ui
	@echo ""
	@echo "$(GREEN)Ready. Run: make start$(NC)"

# ── Infra (containers) ───────────────────────────────────────
# Backing services live in deploy/ (see deploy/README.md).
#   NATS=1   also start the NATS message bus           (e.g. make infra-up NATS=1)
#   APP=1    also start the app tier (mlflow + mlflow-gateway + caliber) and default
#            the shared bus to NATS unless NATS=1 was already selected explicitly
#   BUILD=1  force-rebuild images (otherwise existing images are reused;
#            missing ones are still built automatically on first run)

DEFAULT_BUS_PROFILE := $(if $(APP),$(if $(NATS),, --profile nats),)
PROFILES     := $(DEFAULT_BUS_PROFILE) $(if $(NATS),--profile nats,) $(if $(APP),--profile app,)
ALL_PROFILES := --profile nats --profile app

infra-up: ## start MinIO + Postgres (NATS=1 bus · APP=1 app tier · BUILD=1 rebuild)
	@command -v docker >/dev/null 2>&1 || { echo "$(RED)docker not found — install Docker Desktop.$(NC)"; exit 1; }
	@# Ensure the Allure report dir exists (user-owned) before the bind mount, so
	@# `make allure-report` on the host can write into it (Docker would otherwise
	@# create the missing source dir as root on Linux).
	@mkdir -p $(UI_DIR)/allure-report
	$(COMPOSE) $(PROFILES) up -d $(if $(BUILD),--build,)
	@echo ""
	@echo "$(GREEN)Infra up.$(NC)  MinIO console: http://localhost:9001  ·  Adminer UI: http://localhost:8081  ·  Graph UI: http://localhost:8082"
	@if [ -n "$(APP)" ]; then echo "$(GREEN)App tier:$(NC) caliber :5001/caliber/  ·  mlflow :5000  ·  gateway :5002"; fi

infra-build: ## build the app-tier images (mlflow, mlflow-gateway, caliber)
	@command -v docker >/dev/null 2>&1 || { echo "$(RED)docker not found — install Docker Desktop.$(NC)"; exit 1; }
	$(COMPOSE) --profile app build

infra-down: ## stop the infra stack (keeps data volumes)
	$(COMPOSE) $(ALL_PROFILES) down

infra-status: ## show running infra containers
	@$(COMPOSE) $(ALL_PROFILES) ps

infra-logs: ## tail infra container logs
	$(COMPOSE) $(ALL_PROFILES) logs -f

infra-reset: ## stop the infra stack AND delete its data volumes
	$(COMPOSE) $(ALL_PROFILES) down -v

seed-graph: ## load a sample knowledge graph into Apache AGE (for the graph console :8082)
	@command -v docker >/dev/null 2>&1 || { echo "$(RED)docker not found.$(NC)"; exit 1; }
	docker exec -i caliber-mcp-postgres psql -U caliber -d caliber < deploy/age-viewer/seed-graph.sql
	@echo "$(GREEN)Graph 'knowledge_graph' loaded.$(NC)  Open the graph console: http://localhost:8082 (connect postgres/caliber, graph knowledge_graph)"

# ── Build ────────────────────────────────────────────────────

ui: ## build caliber-ui SPA and copy into Python package
	cd $(PLUGIN_DIR) && $(MAKE) ui

build: ui ## alias for ui

# ── Run ──────────────────────────────────────────────────────

start: ## start the suite (CLEAN_START=1 BUILD=1 by default; set both 0 for fast reuse)
	CLEAN_START=$(CLEAN_START) BUILD=$(BUILD) ./start.sh

stop: ## stop the suite
	./stop.sh

dev: ## hot-reload mode (backend + frontend)
	@echo ""
	@echo "$(CYAN)CALIBER dev mode — run these in two terminals:$(NC)"
	@echo ""
	@echo "  $(GREEN)Terminal 1 (backend):$(NC)"
	@echo "    cd $(PLUGIN_DIR) && make dev"
	@echo ""
	@echo "  $(GREEN)Terminal 2 (frontend):$(NC)"
	@echo "    cd $(UI_DIR) && npm run dev"
	@echo ""
	@echo "  Backend serves API on :5000 (or MLFLOW_PORT)"
	@echo "  Frontend dev server on :5173 with HMR"
	@echo ""

# ── Observe ──────────────────────────────────────────────────

status: ## show running suite containers
	@$(COMPOSE) $(ALL_PROFILES) ps

logs: ## tail suite container logs
	$(COMPOSE) $(ALL_PROFILES) logs -f

# ── Quality ──────────────────────────────────────────────────

test: ## run Python + UI tests
	cd $(PLUGIN_DIR) && $(MAKE) VENV=../$(VENV) test
	cd $(UI_DIR) && npm test

test-all: ## run backend + UI unit + Playwright e2e tests (ALLURE=0 skips report build)
	ALLURE=$(ALLURE) ./test-all.sh

test-allure: ## run all tests emitting Allure results (backend + UI unit)
	cd $(PLUGIN_DIR) && $(MAKE) VENV=../$(VENV) test-allure
	# Wipe stale FE results first (vitest/playwright reporters APPEND, otherwise
	# the report accumulates duplicate/old tests across runs). E2E (test-all.sh)
	# runs after this and appends into the same freshly-cleaned dir.
	cd $(UI_DIR) && rm -rf allure-results && npm test

allure-report: ## run backend, UI unit, and Playwright suites; then build the combined report
	ALLURE=0 ./test-all.sh
	cd $(UI_DIR) && npm run allure:generate:all
	@echo "$(GREEN)Combined Allure report built:$(NC) $(UI_DIR)/allure-report"
	@echo "Open it in-app: CALIBER → Settings → Allure Report (served by the backend)."

allure-publish: allure-report ## build + publish the report to object storage (multi-node serving)
	$(PY) caliber/scripts/publish_allure.py
	@echo "$(GREEN)Published.$(NC) Set CALIBER_ALLURE_REPORT_DIR=s3://<bucket>/<prefix> on the caliber service to serve it from object storage."

allure: ## render + open the Allure report via a local/Dockerized Java server
	cd $(UI_DIR) && bash scripts/allure-report.sh generate

lint: ## ruff + mypy + typecheck
	cd $(PLUGIN_DIR) && $(MAKE) VENV=../$(VENV) lint
	cd $(UI_DIR) && npm run typecheck

# ── SDK, CLI, and plugin SDK ─────────────────────────────────
#
# Each is a separate distribution with its own pyproject.toml (see
# sdk/*/pyproject.toml) and no reason to share a venv with the server or with
# each other -- caliber-sdk and caliber-plugin-sdk are CI-asserted to carry no
# server dependency (ci.yml), and mixing them into $(VENV) would make that
# invisible locally. Each target creates its own package-local .venv on first
# run and reuses it after. These mirror exactly what .github/workflows/ci.yml
# runs for the `sdk`, `plugin-sdk`, and `cli` jobs -- so `make sdk` failing
# locally means that CI job will fail too, and passing means it will not.

SDK_DIR        := sdk/caliber-sdk
CLI_DIR        := sdk/caliber-cli
PLUGIN_SDK_DIR := sdk/caliber-plugin-sdk

sdk: ## caliber-sdk: ruff + mypy + pytest (own venv, zero server deps)
	cd $(SDK_DIR) && test -x .venv/bin/python || python3 -m venv .venv
	cd $(SDK_DIR) && .venv/bin/pip install -q -e ".[dev]"
	cd $(SDK_DIR) && .venv/bin/ruff check src tests examples
	cd $(SDK_DIR) && .venv/bin/mypy src tests examples
	cd $(SDK_DIR) && .venv/bin/pytest

plugin-sdk: ## caliber-plugin-sdk: ruff + mypy + pytest (own venv, zero runtime deps)
	cd $(PLUGIN_SDK_DIR) && test -x .venv/bin/python || python3 -m venv .venv
	cd $(PLUGIN_SDK_DIR) && .venv/bin/pip install -q -e ".[dev]"
	cd $(PLUGIN_SDK_DIR) && .venv/bin/ruff check src tests
	cd $(PLUGIN_SDK_DIR) && .venv/bin/mypy src tests
	cd $(PLUGIN_SDK_DIR) && .venv/bin/pytest

cli: sdk ## caliber-cli: ruff + mypy + pytest (installs the local caliber-sdk checkout, not PyPI)
	cd $(CLI_DIR) && test -x .venv/bin/python || python3 -m venv .venv
	cd $(CLI_DIR) && .venv/bin/pip install -q -e "../caliber-sdk" -e ".[dev]"
	cd $(CLI_DIR) && .venv/bin/ruff check src tests
	cd $(CLI_DIR) && .venv/bin/mypy src tests
	cd $(CLI_DIR) && .venv/bin/pytest

sdk-coverage: ## the SDK<->API parity gate alone (needs the server venv: `make setup` first)
	cd $(PLUGIN_DIR) && ../$(PY) -m pytest tests/test_sdk_api_coverage.py -v --no-cov

# ── Cleanup ──────────────────────────────────────────────────

clean: ## remove build artifacts, caches, databases
	cd $(PLUGIN_DIR) && $(MAKE) clean
	rm -rf $(UI_DIR)/dist
	rm -rf logs/*.log
	rm -rf .run/*.pid

reset: stop clean setup ## stop + clean + setup (full nuke and rebuild)
