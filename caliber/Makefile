# Developer entry points for the caliber plugin.
#
#   make install         create .venv and install caliber editable + dev deps
#   make migrate         run alembic upgrade head against $CALIBER_DATABASE_URL
#   make dev             run mlflow server with --app-name caliber (alembic first)
#   make tracking-server run vanilla mlflow server on the same backend store
#                        for agent ingestion (different port; see target body)
#   make test            run the pytest suite
#   make lint            ruff + mypy
#   make ui              build the caliber-ui SPA into src/caliber/ui/
#   make clean           remove local dev databases, artifacts, build cache
#
# Override the venv location with VENV=/path/to/venv if you don't use .venv/.

VENV ?= .venv
PY := $(VENV)/bin/python
PIP := $(VENV)/bin/pip
PYTEST := $(VENV)/bin/pytest
RUFF := $(VENV)/bin/ruff
MYPY := $(VENV)/bin/mypy
ALEMBIC := $(VENV)/bin/alembic
CALIBER_DEV_EXTRAS ?= dev,postgres,ingest,ocr,llm,knowledge
# ``memory`` is here because tests/test_memory_service.py imports mem0 at module
# scope. CI installs it (see the env comment in .github/workflows/ci.yml) but the
# Makefile did not, so ``make install-extended`` produced a red suite for a
# missing extra rather than a real failure — the profile CI documents and the
# profile the Makefile builds have to be the same thing.
CALIBER_DEV_EXTENDED_EXTRAS ?= dspy,knowledge-local,memory

.PHONY: help install install-extended migrate dev tracking-server test test-allure lint ui clean

# Local-dev defaults. Mirrored from scripts/run-dev.sh so `make
# tracking-server` and `make dev` agree on which database and
# artifact root they share. Override via env: e.g.
#   CALIBER_DATABASE_URL=postgresql://… make tracking-server
CALIBER_DATABASE_URL ?= sqlite:///./caliber.db
MLFLOW_ARTIFACT_ROOT ?= s3://mlflow/mlruns
MLFLOW_S3_ENDPOINT_URL ?= http://127.0.0.1:9000
MINIO_ROOT_USER ?= minioadmin
MINIO_ROOT_PASSWORD ?= minioadmin
AWS_ACCESS_KEY_ID ?= $(MINIO_ROOT_USER)
AWS_SECRET_ACCESS_KEY ?= $(MINIO_ROOT_PASSWORD)
MLFLOW_HOST ?= 127.0.0.1
MLFLOW_TRACKING_PORT ?= 5001

help:
	@awk 'BEGIN { FS = ":.*##" } /^[a-zA-Z_-]+:.*##/ { printf "  %-12s %s\n", $$1, $$2 }' $(MAKEFILE_LIST)

# Interpreter used to create the venv. Bare ``python3`` is deliberately NOT the
# default: it resolves to whatever the machine happens to ship, which is how a
# development venv silently lands outside the range this package declares
# (``requires-python = ">=3.10,<3.13"``). A contributor then gets green local
# runs on an interpreter CI never exercises, which is worse than a red one —
# it is confidence without coverage.
#
# Prefers 3.12, the top of the supported range and a CI compatibility leg.
# Override with ``make install CALIBER_PYTHON=python3.10`` to develop against
# the floor instead.
CALIBER_PYTHON ?= $(shell command -v python3.12 || command -v python3.11 || command -v python3.10 || command -v python3)

$(VENV)/bin/python:
	@$(CALIBER_PYTHON) scripts/check_python_version.py
	$(CALIBER_PYTHON) -m venv $(VENV)
	$(PIP) install --upgrade pip

install: $(VENV)/bin/python  ## create venv + editable install with the production-safe dev extras
	$(PIP) install -e ".[${CALIBER_DEV_EXTRAS}]"

install-extended: $(VENV)/bin/python  ## add DSPy + local-HF embedding extras on top of the safe dev profile
	$(PIP) install -e ".[${CALIBER_DEV_EXTRAS},${CALIBER_DEV_EXTENDED_EXTRAS}]"

migrate:  ## apply alembic migrations to CALIBER_DATABASE_URL
	$(ALEMBIC) upgrade head

dev:  ## run alembic + start mlflow server with --app-name caliber
	@VENV_DIR=$(VENV) ./scripts/run-dev.sh

tracking-server:  ## start a vanilla mlflow server on $(MLFLOW_TRACKING_PORT) sharing the caliber DB
	@echo ">> mlflow tracking server (vanilla — agents POST traces here)"
	@echo "   database         : configured via CALIBER_DATABASE_URL"
	@echo "   artifact root    : configured via MLFLOW_ARTIFACT_ROOT"
	@echo "   s3 endpoint      : configured via MLFLOW_S3_ENDPOINT_URL"
	@echo "   listen           : http://$(MLFLOW_HOST):$(MLFLOW_TRACKING_PORT)"
	@echo "   tracking URI     : http://$(MLFLOW_HOST):$(MLFLOW_TRACKING_PORT)"
	@AWS_ACCESS_KEY_ID="$(AWS_ACCESS_KEY_ID)" \
	AWS_SECRET_ACCESS_KEY="$(AWS_SECRET_ACCESS_KEY)" \
	MLFLOW_S3_ENDPOINT_URL="$(MLFLOW_S3_ENDPOINT_URL)" \
	$(VENV)/bin/mlflow server \
		--backend-store-uri "$(CALIBER_DATABASE_URL)" \
		--default-artifact-root "$(MLFLOW_ARTIFACT_ROOT)" \
		--host "$(MLFLOW_HOST)" \
		--port "$(MLFLOW_TRACKING_PORT)"

test:  ## run the pytest suite
	# Routed through scripts/pytest_clean_exit.py so a PyMuPDF/Python-3.14
	# interpreter-finalization segfault (which fires *after* the suite passes)
	# can't corrupt the exit code. Pass extra args via ARGS="-k foo -x".
	$(PY) scripts/pytest_clean_exit.py $(ARGS)

test-allure:  ## run pytest and emit Allure results to allure-results/
	# Adds --alluredir on top of the normal run; generate the HTML report with
	# the npm allure CLI: cd caliber-ui && npm run allure:generate:all
	# --clean-alluredir wipes stale results first so the report reflects exactly
	# this run (allure-pytest otherwise APPENDS, inflating/duplicating the report).
	$(PY) scripts/pytest_clean_exit.py --alluredir=allure-results --clean-alluredir $(ARGS)

ci-local:  ## run every CI gate on this machine (add --fast to skip integration+package)
	../scripts/ci-local.sh $(ARGS)

lint:  ## ruff + mypy
	$(RUFF) check src tests
	$(MYPY) src

ui:  ## build the SPA bundle into src/caliber/ui/
	cd caliber-ui && npm install && npm run build
	rm -rf src/caliber/ui
	mkdir -p src/caliber/ui
	cp -R caliber-ui/dist/* src/caliber/ui/

clean:  ## remove local dev databases, artifacts, caches
	rm -f caliber.db caliber.db-journal caliber.db-wal caliber.db-shm
	rm -rf mlruns/ mlartifacts/ .pytest_cache/ .ruff_cache/ .mypy_cache/ build/ dist/
