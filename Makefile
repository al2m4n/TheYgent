# theygent dev Makefile — one-command bring-up / tear-down of the local processes.
#
#   make up     install deps, run migrations, then spin up inference + control-plane + web + interface
#   make down   stop everything started by `make up`
#
# Mirrors the planned topology (CLAUDE.md): inference plane (8081), control-plane API (8080),
# cockpit SPA (5173), and the M15 visual interface SPA (5174). They run as detached background
# processes; PIDs and logs land under .run/ (gitignored). Config is read from .env (see
# .env.example); this Makefile sources it for the recipes that need DATABASE_URL / host+port.
#
# Prereqs not managed here: a running Postgres reachable at DATABASE_URL (migrations + the
# control-plane need it), plus `uv` and `pnpm` on PATH.

SHELL := /bin/bash
.ONESHELL:
.DEFAULT_GOAL := help

RUN_DIR := .run
ENV_FILE := .env

# Load .env at parse time and export every key into the recipe environment, so
# DATABASE_URL / THEYGENT_* are available to alembic, uvicorn, and vite without
# sourcing it per-recipe. `include` reads simple KEY=value lines (our .env format).
ifneq (,$(wildcard ./$(ENV_FILE)))
include $(ENV_FILE)
export
endif

# Python test suites run per-package: the apps share test-file basenames
# (test_fast_suite.py, test_integration_mlx.py) with no package __init__, so a single
# root pytest collides on import. Each entry is run with its own dir as rootdir.
PY_TEST_DIRS := packages/ir apps/inference apps/control-plane

.PHONY: help install migrate up start down status logs test test-py test-web gen-ir-types

help: ## Show this help
	@grep -hE '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) \
		| awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-10s\033[0m %s\n", $$1, $$2}'

install: ## uv sync (Python workspace) + pnpm install (TS workspace)
	@echo "==> uv sync"
	uv sync
	@echo "==> pnpm install"
	pnpm install

migrate: ## Apply control-plane Alembic migrations (alembic upgrade head)
	@echo "==> alembic upgrade head"
	if [ -z "$$DATABASE_URL" ]; then \
		echo "DATABASE_URL is not set (copy .env.example to .env)"; exit 1; \
	fi
	cd apps/control-plane && uv run alembic upgrade head

up: install migrate start ## Full bring-up: install, migrate, then start all services

start: ## Start inference + control-plane + web + interface as background processes
	@mkdir -p $(RUN_DIR)
	echo "==> starting inference plane (:$${THEYGENT_INFERENCE_PORT:-8081})"
	nohup uv run --package theygent-inference theygent-inference \
		> $(RUN_DIR)/inference.log 2>&1 & echo $$! > $(RUN_DIR)/inference.pid
	echo "==> starting control-plane (:$${THEYGENT_CONTROL_PLANE_PORT:-8080})"
	nohup uv run --package theygent-control-plane theygent-control-plane \
		> $(RUN_DIR)/control-plane.log 2>&1 & echo $$! > $(RUN_DIR)/control-plane.pid
	echo "==> starting cockpit (web, :5173)"
	nohup pnpm --filter @theygent/web dev \
		> $(RUN_DIR)/web.log 2>&1 & echo $$! > $(RUN_DIR)/web.pid
	echo "==> starting interface (visual canvas, :5174)"
	nohup pnpm --filter @theygent/interface dev \
		> $(RUN_DIR)/interface.log 2>&1 & echo $$! > $(RUN_DIR)/interface.pid
	echo ""
	echo "All started (inference :8081 · control-plane :8080 · web :5173 · interface :5174)."
	echo "Logs: make logs  |  Status: make status  |  Stop: make down"

down: ## Stop all services started by `make up`/`make start`
	@for svc in inference control-plane web interface; do \
		pidfile=$(RUN_DIR)/$$svc.pid; \
		if [ -f $$pidfile ]; then \
			pid=$$(cat $$pidfile); \
			if kill -0 $$pid 2>/dev/null; then \
				echo "==> stopping $$svc (pid $$pid)"; \
				kill $$pid 2>/dev/null || true; \
				pkill -P $$pid 2>/dev/null || true; \
			else \
				echo "==> $$svc not running (stale pid $$pid)"; \
			fi; \
			rm -f $$pidfile; \
		else \
			echo "==> $$svc: no pidfile"; \
		fi; \
	done

status: ## Show whether each service is running
	@for svc in inference control-plane web interface; do \
		pidfile=$(RUN_DIR)/$$svc.pid; \
		if [ -f $$pidfile ] && kill -0 $$(cat $$pidfile) 2>/dev/null; then \
			echo "  $$svc: running (pid $$(cat $$pidfile))"; \
		else \
			echo "  $$svc: stopped"; \
		fi; \
	done

test: test-py test-web ## Run all tests (Python suites + web unit tests)

test-py: ## Run Python tests for every package (excludes integration; pass ARGS=… to forward)
	@set -e
	for d in $(PY_TEST_DIRS); do \
		echo "==> pytest $$d"; \
		( cd $$d && uv run pytest $(ARGS) ); \
	done

test-web: ## Run web + interface unit tests (vitest)
	@echo "==> vitest (apps/web)"
	pnpm --filter @theygent/web test
	@echo "==> vitest (apps/interface — M15)"
	pnpm --filter @theygent/interface test

gen-ir-types: ## Regenerate @theygent/ir-types from packages/ir (schema + TS + node registry)
	@echo "==> generating ir-types from packages/ir"
	pnpm --filter @theygent/ir-types generate

smoke-interface: ## Hand-drive smoke for apps/interface vs the LIVE control-plane (needs `make up`)
	@echo "==> interface hand-drive smoke (drag-doesn't-hash · content-does · runs unchanged)"
	uv run --package theygent-control-plane python apps/interface/tests/smoke/hand_drive.py

logs: ## Tail the logs of all four services
	@tail -n +1 -f $(RUN_DIR)/inference.log $(RUN_DIR)/control-plane.log $(RUN_DIR)/web.log $(RUN_DIR)/interface.log
