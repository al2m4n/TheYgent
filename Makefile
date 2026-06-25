# theygent dev Makefile — one-command bring-up / tear-down of the local processes.
#
#   make up       install deps, run migrations, then spin up inference + control-plane + web + interface
#   make restart  stop everything and start it again (use this to pick up backend code changes)
#   make down     stop everything started by `make up`
#
# Services are tracked by the PORT they listen on, not by a recorded pid: `start` records `$!` only
# as a hint (it is the uv/pnpm WRAPPER, not the server child that binds the port), so `down`/`status`
# resolve the live process via `lsof` on the port and are robust to stale/missing pidfiles.
#
# Mirrors the planned topology (CLAUDE.md): inference plane (8081), control-plane API (8080),
# cockpit SPA (5173), and the M15 visual interface SPA (5174). They run as detached background
# processes; PIDs and logs land under .run/ (gitignored). Config is read from .env (see
# .env.example); this Makefile sources it for the recipes that need DATABASE_URL / host+port.
#
# Prereqs not managed here: a running Postgres reachable at DATABASE_URL (migrations + the
# control-plane need it), plus `uv` and `pnpm` on PATH.

SHELL := /bin/bash
.DEFAULT_GOAL := help
# NOTE: no .ONESHELL — macOS ships GNU Make 3.81, which predates it (3.82). Each recipe line runs in
# its own shell, so multi-statement recipes use backslash line-continuations (one logical line).

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

# Each service is identified by the PORT it listens on — that is the authoritative "is it up and
# which process is it", NOT a recorded pid. `start` records `$!`, but that is the `uv run`/`pnpm`
# WRAPPER, not the python/vite child that actually binds the port — so a pid-based `down`/`status`
# misses the real server (and a stale/reused pidfile makes it kill the wrong thing or nothing).
# `down`/`status`/`restart` therefore resolve the live process via `lsof` on the port. Ports come
# from .env (THEYGENT_*_PORT) with the same defaults `start` uses.
INFERENCE_PORT := $(or $(THEYGENT_INFERENCE_PORT),8081)
CONTROL_PLANE_PORT := $(or $(THEYGENT_CONTROL_PLANE_PORT),8080)
WEB_PORT := $(or $(THEYGENT_WEB_PORT),5173)
INTERFACE_PORT := $(or $(THEYGENT_INTERFACE_PORT),5174)

.PHONY: help install migrate up start restart down status logs test test-py test-web gen-ir-types hooks lint

help: ## Show this help
	@grep -hE '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) \
		| awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-10s\033[0m %s\n", $$1, $$2}'

install: ## uv sync (Python workspace) + pnpm install (TS workspace)
	@echo "==> uv sync"
	uv sync
	@echo "==> pnpm install"
	pnpm install

hooks: ## Install the git pre-commit hooks (.pre-commit-config.yaml — mirrors the CI gates)
	@echo "==> uvx pre-commit install"
	uvx pre-commit install

lint: ## Run all pre-commit hooks against every file (ruff · ty · biome)
	@echo "==> uvx pre-commit run --all-files"
	uvx pre-commit run --all-files

migrate: ## Apply control-plane Alembic migrations (alembic upgrade head)
	@echo "==> alembic upgrade head"
	if [ -z "$$DATABASE_URL" ]; then \
		echo "DATABASE_URL is not set (copy .env.example to .env)"; exit 1; \
	fi
	cd apps/control-plane && uv run alembic upgrade head

up: install migrate start ## Full bring-up: install, migrate, then start all services

restart: down start ## Stop everything, then start it again (picks up code changes)

# Each service launches only if its port is free, so a relaunch over a still-running instance is
# skipped LOUDLY instead of silently failing to bind and leaving the OLD process serving (the trap
# that hid stale code). $$! (the uv/pnpm wrapper pid) is recorded only as a hint; `down` resolves the
# real server by port.
start: ## Start inference + control-plane + web + interface as background processes
	@mkdir -p $(RUN_DIR)
	@if lsof -ti tcp:$(INFERENCE_PORT) -sTCP:LISTEN >/dev/null 2>&1; then \
		echo "==> inference already on :$(INFERENCE_PORT) — skipping (use 'make restart')"; \
	else \
		echo "==> starting inference plane (:$(INFERENCE_PORT))"; \
		nohup uv run --package theygent-inference theygent-inference > $(RUN_DIR)/inference.log 2>&1 & \
		echo $$! > $(RUN_DIR)/inference.pid; \
	fi
	@if lsof -ti tcp:$(CONTROL_PLANE_PORT) -sTCP:LISTEN >/dev/null 2>&1; then \
		echo "==> control-plane already on :$(CONTROL_PLANE_PORT) — skipping (use 'make restart')"; \
	else \
		echo "==> starting control-plane (:$(CONTROL_PLANE_PORT))"; \
		nohup uv run --package theygent-control-plane theygent-control-plane > $(RUN_DIR)/control-plane.log 2>&1 & \
		echo $$! > $(RUN_DIR)/control-plane.pid; \
	fi
	@if lsof -ti tcp:$(WEB_PORT) -sTCP:LISTEN >/dev/null 2>&1; then \
		echo "==> web already on :$(WEB_PORT) — skipping (use 'make restart')"; \
	else \
		echo "==> starting cockpit (web, :$(WEB_PORT))"; \
		nohup pnpm --filter @theygent/web dev > $(RUN_DIR)/web.log 2>&1 & \
		echo $$! > $(RUN_DIR)/web.pid; \
	fi
	@if lsof -ti tcp:$(INTERFACE_PORT) -sTCP:LISTEN >/dev/null 2>&1; then \
		echo "==> interface already on :$(INTERFACE_PORT) — skipping (use 'make restart')"; \
	else \
		echo "==> starting interface (visual canvas, :$(INTERFACE_PORT))"; \
		nohup pnpm --filter @theygent/interface dev > $(RUN_DIR)/interface.log 2>&1 & \
		echo $$! > $(RUN_DIR)/interface.pid; \
	fi
	@echo ""
	@echo "Started (inference :$(INFERENCE_PORT) · control-plane :$(CONTROL_PLANE_PORT) · web :$(WEB_PORT) · interface :$(INTERFACE_PORT))."
	@echo "Logs: make logs  |  Status: make status  |  Stop: make down  |  Restart: make restart"

down: ## Stop all services (resolved by PORT — robust to stale/missing/wrapper pidfiles)
	@for svc in "inference:$(INFERENCE_PORT)" "control-plane:$(CONTROL_PLANE_PORT)" "web:$(WEB_PORT)" "interface:$(INTERFACE_PORT)"; do \
		name=$${svc%%:*}; port=$${svc##*:}; pidfile=$(RUN_DIR)/$$name.pid; \
		recorded=""; if [ -f "$$pidfile" ]; then recorded=$$(cat "$$pidfile" 2>/dev/null); fi; \
		listeners=$$(lsof -ti tcp:$$port -sTCP:LISTEN 2>/dev/null || true); \
		if [ -z "$$listeners" ] && { [ -z "$$recorded" ] || ! kill -0 "$$recorded" 2>/dev/null; }; then \
			echo "==> $$name: not running"; rm -f "$$pidfile"; continue; \
		fi; \
		echo "==> stopping $$name (:$$port)"; \
		if [ -n "$$recorded" ] && kill -0 "$$recorded" 2>/dev/null; then \
			pkill -TERM -P "$$recorded" 2>/dev/null || true; kill -TERM "$$recorded" 2>/dev/null || true; \
		fi; \
		if [ -n "$$listeners" ]; then kill -TERM $$listeners 2>/dev/null || true; fi; \
		n=0; while [ $$n -lt 20 ] && lsof -ti tcp:$$port -sTCP:LISTEN >/dev/null 2>&1; do sleep 0.25; n=$$((n+1)); done; \
		straggler=$$(lsof -ti tcp:$$port -sTCP:LISTEN 2>/dev/null || true); \
		if [ -n "$$straggler" ]; then echo "   force-killing :$$port (pid $$straggler)"; kill -9 $$straggler 2>/dev/null || true; fi; \
		rm -f "$$pidfile"; \
	done

status: ## Show whether each service is running (resolved by PORT)
	@for svc in "inference:$(INFERENCE_PORT)" "control-plane:$(CONTROL_PLANE_PORT)" "web:$(WEB_PORT)" "interface:$(INTERFACE_PORT)"; do \
		name=$${svc%%:*}; port=$${svc##*:}; \
		pid=$$(lsof -ti tcp:$$port -sTCP:LISTEN 2>/dev/null | head -1 || true); \
		if [ -n "$$pid" ]; then echo "  $$name: running on :$$port (pid $$pid)"; else echo "  $$name: stopped"; fi; \
	done

test: test-py test-web ## Run all tests (Python suites + web unit tests)

test-py: ## Run Python tests for every package (excludes integration; pass ARGS=… to forward)
	@for d in $(PY_TEST_DIRS); do \
		echo "==> pytest $$d"; \
		( cd $$d && uv run pytest $(ARGS) ) || exit 1; \
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
