# theygent dev Makefile — one-command bring-up / tear-down of the local processes.
#
#   make up       install deps, run migrations, then spin up inference-plane + control-plane + interface
#   make restart  stop everything and start it again (use this to pick up backend code changes)
#   make down     stop everything started by `make up`
#
# Services are tracked by the PORT they listen on, not by a recorded pid: `start` records `$!` only
# as a hint (it is the uv/pnpm WRAPPER, not the server child that binds the port), so `down`/`status`
# resolve the live process via `lsof` on the port and are robust to stale/missing pidfiles.
#
# Mirrors the planned topology (CLAUDE.md): inference plane (8081), control-plane API (8080),
# and the visual interface SPA (5174). They run as detached background
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
PY_TEST_DIRS := packages/ir apps/inference-plane apps/control-plane

# Each service is identified by the PORT it listens on — that is the authoritative "is it up and
# which process is it", NOT a recorded pid. `start` records `$!`, but that is the `uv run`/`pnpm`
# WRAPPER, not the python/vite child that actually binds the port — so a pid-based `down`/`status`
# misses the real server (and a stale/reused pidfile makes it kill the wrong thing or nothing).
# `down`/`status`/`restart` therefore resolve the live process via `lsof` on the port. Ports come
# from .env (THEYGENT_*_PORT) with the same defaults `start` uses.
INFERENCE_PORT := $(or $(THEYGENT_INFERENCE_PLANE_PORT),8081)
CONTROL_PLANE_PORT := $(or $(THEYGENT_CONTROL_PLANE_PORT),8080)
INTERFACE_PORT := $(or $(THEYGENT_INTERFACE_PORT),5174)

.PHONY: help install engines migrate up start restart down status logs test test-py test-web test-deploy gen-ir-types hooks lint docs-serve docs-build docker-build docker-up docker-down docker-logs k8s-load k8s-apply k8s-delete

help: ## Show this help
	@grep -hE '^[a-zA-Z0-9_-]+:.*?## .*$$' $(MAKEFILE_LIST) \
		| awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-12s\033[0m %s\n", $$1, $$2}'

install: ## uv sync (Python workspace) + pnpm install (TS workspace)
	@echo "==> uv sync"
	uv sync
	@echo "==> pnpm install"
	pnpm install

# The engine servers behind every managed (engine, modality) slot. The platform launches and
# supervises these binaries but never builds/bundles them (a desktop-shell packaging concern), so
# a dev machine installs them once here. Idempotent — rerun after a pull to pick up new slots.
#   llama-server     chat + embeddings            (brew: llama.cpp)
#   whisper-server   speech-to-text               (brew: whisper-cpp; ffmpeg converts the browser
#                                                  microphone's webm/opus uploads)
#   mlx_lm.server    MLX chat (Apple Silicon)     (uv tool: mlx-lm)
#   mlx_vlm.server   MLX vision                   (uv tool: mlx-vlm)
#   mlx_audio.server MLX text-to-speech           (uv tool: mlx-audio — its server extras are not
#                                                  declared upstream, hence the --with list;
#                                                  webrtcvad still imports pkg_resources, hence
#                                                  Python 3.12 + setuptools<81; the misaki g2p
#                                                  needs spaCy's small English model wheel)
# vLLM is deliberately absent: CUDA-host only (`pip install vllm` there).
UV_TOOL_DIR ?= $(HOME)/.local/share/uv/tools
SPACY_EN_WHEEL := https://github.com/explosion/spacy-models/releases/download/en_core_web_sm-3.8.0/en_core_web_sm-3.8.0-py3-none-any.whl

engines: ## Install the local engine servers (macOS): chat, vision, embeddings, STT, TTS
ifeq ($(shell uname),Darwin)
	@echo "==> brew install llama.cpp whisper-cpp ffmpeg"
	brew install llama.cpp whisper-cpp ffmpeg
	@echo "==> uv tool install mlx-lm / mlx-vlm / mlx-audio"
	uv tool install mlx-lm --with 'transformers<5.13'
	uv tool install mlx-vlm
	uv tool install --python 3.12 mlx-audio \
		--with uvicorn --with fastapi --with webrtcvad --with python-multipart \
		--with 'setuptools<81' --with 'misaki[en]' --with 'transformers<5.13'
	uv pip install --python $(UV_TOOL_DIR)/mlx-audio/bin/python "en_core_web_sm @ $(SPACY_EN_WHEEL)"
	@echo "==> done — /readyz on the inference plane shows the per-(engine,modality) breakdown"
else
	@echo "Non-macOS host: install llama.cpp (llama-server), whisper.cpp (whisper-server) and"
	@echo "ffmpeg with your package manager; the MLX servers are Apple-Silicon-only; vLLM belongs"
	@echo "on a CUDA host (pip install vllm)."
endif

hooks: ## Install the git pre-commit hooks (.pre-commit-config.yaml — mirrors the CI gates)
	@echo "==> uvx pre-commit install"
	uvx pre-commit install

lint: ## Run all pre-commit hooks against every file (ruff · ty · biome · tsc · ir-types drift)
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
start: ## Start inference-plane + control-plane + interface as background processes
	@mkdir -p $(RUN_DIR)
	@if lsof -ti tcp:$(INFERENCE_PORT) -sTCP:LISTEN >/dev/null 2>&1; then \
		echo "==> inference-plane already on :$(INFERENCE_PORT) — skipping (use 'make restart')"; \
	else \
		echo "==> starting inference-plane (:$(INFERENCE_PORT))"; \
		nohup uv run --package theygent-inference-plane theygent-inference-plane > $(RUN_DIR)/inference-plane.log 2>&1 & \
		echo $$! > $(RUN_DIR)/inference-plane.pid; \
	fi
	@if lsof -ti tcp:$(CONTROL_PLANE_PORT) -sTCP:LISTEN >/dev/null 2>&1; then \
		echo "==> control-plane already on :$(CONTROL_PLANE_PORT) — skipping (use 'make restart')"; \
	else \
		echo "==> starting control-plane (:$(CONTROL_PLANE_PORT))"; \
		nohup uv run --package theygent-control-plane theygent-control-plane > $(RUN_DIR)/control-plane.log 2>&1 & \
		echo $$! > $(RUN_DIR)/control-plane.pid; \
	fi
	@if lsof -ti tcp:$(INTERFACE_PORT) -sTCP:LISTEN >/dev/null 2>&1; then \
		echo "==> interface already on :$(INTERFACE_PORT) — skipping (use 'make restart')"; \
	else \
		echo "==> starting interface (visual canvas, :$(INTERFACE_PORT))"; \
		nohup pnpm --filter @theygent/interface dev > $(RUN_DIR)/interface.log 2>&1 & \
		echo $$! > $(RUN_DIR)/interface.pid; \
	fi
	@echo ""
	@echo "Started (inference-plane :$(INFERENCE_PORT) · control-plane :$(CONTROL_PLANE_PORT) · interface :$(INTERFACE_PORT))."
	@echo "Logs: make logs  |  Status: make status  |  Stop: make down  |  Restart: make restart"

# The compose stack publishes the SAME host ports, and on macOS/Linux those are LISTENed by
# Docker's own process (com.docker.backend / docker-proxy) — killing it would take down the
# whole Docker engine, not "the service". So both kill paths here skip Docker-owned pids and
# point at `make docker-down` instead.
down: ## Stop all bare-metal services (resolved by PORT; never touches the docker stack)
	@for svc in "inference-plane:$(INFERENCE_PORT)" "control-plane:$(CONTROL_PLANE_PORT)" "interface:$(INTERFACE_PORT)"; do \
		name=$${svc%%:*}; port=$${svc##*:}; pidfile=$(RUN_DIR)/$$name.pid; \
		recorded=""; if [ -f "$$pidfile" ]; then recorded=$$(cat "$$pidfile" 2>/dev/null); fi; \
		listeners=""; docker_owned=""; \
		for pid in $$(lsof -ti tcp:$$port -sTCP:LISTEN 2>/dev/null); do \
			case "$$(ps -o comm= -p $$pid 2>/dev/null)" in \
				*[Dd]ocker*) docker_owned="$$pid";; \
				*) listeners="$$listeners $$pid";; \
			esac; \
		done; \
		listeners=$${listeners# }; \
		if [ -n "$$docker_owned" ] && [ -z "$$listeners" ]; then \
			echo "==> $$name: :$$port is published by Docker (the compose stack) — use 'make docker-down'"; rm -f "$$pidfile"; continue; \
		fi; \
		if [ -z "$$listeners" ] && { [ -z "$$recorded" ] || ! kill -0 "$$recorded" 2>/dev/null; }; then \
			echo "==> $$name: not running"; rm -f "$$pidfile"; continue; \
		fi; \
		echo "==> stopping $$name (:$$port)"; \
		if [ -n "$$recorded" ] && kill -0 "$$recorded" 2>/dev/null; then \
			pkill -TERM -P "$$recorded" 2>/dev/null || true; kill -TERM "$$recorded" 2>/dev/null || true; \
		fi; \
		if [ -n "$$listeners" ]; then kill -TERM $$listeners 2>/dev/null || true; fi; \
		n=0; while [ $$n -lt 20 ] && lsof -ti tcp:$$port -sTCP:LISTEN >/dev/null 2>&1; do sleep 0.25; n=$$((n+1)); done; \
		for pid in $$(lsof -ti tcp:$$port -sTCP:LISTEN 2>/dev/null); do \
			case "$$(ps -o comm= -p $$pid 2>/dev/null)" in \
				*[Dd]ocker*) ;; \
				*) echo "   force-killing :$$port (pid $$pid)"; kill -9 $$pid 2>/dev/null || true;; \
			esac; \
		done; \
		rm -f "$$pidfile"; \
	done

status: ## Show whether each service is running (resolved by PORT)
	@for svc in "inference-plane:$(INFERENCE_PORT)" "control-plane:$(CONTROL_PLANE_PORT)" "interface:$(INTERFACE_PORT)"; do \
		name=$${svc%%:*}; port=$${svc##*:}; \
		pid=$$(lsof -ti tcp:$$port -sTCP:LISTEN 2>/dev/null | head -1 || true); \
		if [ -z "$$pid" ]; then echo "  $$name: stopped"; continue; fi; \
		case "$$(ps -o comm= -p $$pid 2>/dev/null)" in \
			*[Dd]ocker*) echo "  $$name: running on :$$port (docker — the compose stack)";; \
			*) echo "  $$name: running on :$$port (pid $$pid)";; \
		esac; \
	done

test: test-py test-web test-deploy ## Run all tests (Python suites + interface unit tests + deploy guards)

test-py: ## Run Python tests for every package (excludes integration; pass ARGS=… to forward)
	@for d in $(PY_TEST_DIRS); do \
		echo "==> pytest $$d"; \
		( cd $$d && uv run pytest $(ARGS) ) || exit 1; \
	done

test-web: ## Run interface unit tests (vitest)
	@echo "==> vitest (apps/interface)"
	pnpm --filter @theygent/interface test

# Runs in the control-plane's env (pytest + pyyaml already there) — same precedent as
# smoke-interface. Static contract checks only; docker/kubectl sub-checks skip when absent.
test-deploy: ## Guard the containerization contracts (Dockerfiles · compose · deploy/k8s)
	@echo "==> pytest deploy/tests"
	uv run --package theygent-control-plane pytest deploy/tests

gen-ir-types: ## Regenerate @theygent/ir-types from packages/ir (schema + TS + node registry)
	@echo "==> generating ir-types from packages/ir"
	pnpm --filter @theygent/ir-types generate

smoke-interface: ## Hand-drive smoke for apps/interface vs the LIVE control-plane (needs `make up`)
	@echo "==> interface hand-drive smoke (drag-doesn't-hash · content-does · runs unchanged)"
	uv run --package theygent-control-plane python apps/interface/tests/smoke/hand_drive.py

logs: ## Tail the logs of all services
	@tail -n +1 -f $(RUN_DIR)/inference-plane.log $(RUN_DIR)/control-plane.log $(RUN_DIR)/interface.log

# ── The SECOND way of running the stack: containers. `make up` (bare-metal) stays the
# first; both are supported. Postgres comes with the compose stack (pgvector image) on host
# port 5433, clear of a bare-metal Postgres on 5432. For local MLX inference keep the
# inference plane on the host and use the docker-compose.host-inference.yml overlay (see
# that file's header).

docker-build: ## Build all container images (incl. the worker)
	docker compose --profile worker build

docker-up: ## Bring up the containerized stack (postgres + migrate + planes + interface)
	docker compose up -d --build
	@echo ""
	@echo "control-plane http://localhost:8080 · inference http://localhost:8081 · interface http://localhost:5174"
	@echo "Worker (durable): THEYGENT_DURABLE=1 docker compose --profile worker up -d"

docker-down: ## Stop the containerized stack (data volumes survive; drop them with `docker compose down -v`)
	docker compose --profile worker down

docker-logs: ## Tail logs of the containerized stack
	docker compose logs -f

# ── Kubernetes (deploy/k8s) — dev flow against minikube. The images are built locally by
# compose and side-loaded; imagePullPolicy stays IfNotPresent.

K8S_IMAGES := theygent/control-plane:dev theygent/inference-plane:dev theygent/worker:dev theygent/interface:dev

# NOT `minikube image load`: with the docker driver it silently keeps an EXISTING same-tag
# image (even with --overwrite), so a rebuilt :dev never reaches the cluster — pods keep
# running stale code. save + load against minikube's own daemon replaces the tag reliably.
k8s-load: docker-build ## Build images and load them into minikube (force-replaces same-tag images)
	@for img in $(K8S_IMAGES); do \
		echo "==> loading $$img into minikube"; \
		docker save $$img -o /tmp/theygent-k8s-img.tar; \
		( eval "$$(minikube -p minikube docker-env)" && docker load -i /tmp/theygent-k8s-img.tar ); \
		rm -f /tmp/theygent-k8s-img.tar; \
	done

k8s-apply: ## Apply the Kubernetes manifests (kubectl apply -k deploy/k8s)
	kubectl apply -k deploy/k8s

k8s-delete: ## Tear down the Kubernetes resources (PVC data included)
	kubectl delete -k deploy/k8s

docs-serve: ## Serve the user docs locally with live reload (http://127.0.0.1:8000)
	uv run --project docs/user-docs mkdocs serve -f docs/user-docs/mkdocs.yml

docs-build: ## Build the user docs site (strict — broken links fail the build)
	uv run --project docs/user-docs mkdocs build --strict -f docs/user-docs/mkdocs.yml
