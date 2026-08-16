# theygent dev Makefile — one-command bring-up / tear-down of the local processes.
#
#   make up       install deps, start Postgres (docker), run migrations, then spin up
#                 inference-plane + control-plane + interface
#   make restart  stop the app services and start them again (picks up backend code changes;
#                 Postgres keeps running — only `make down` stops it)
#   make down     stop everything started by `make up`, including the Postgres container
#   make otel-up  spin up the sample observability stack (OTel collector → Tempo → Grafana)
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

# The marketing site (apps/web) is static — a plain file server is the whole story, but it must be
# a server (not file://) because the page uses root-relative /styles.css and /assets/... paths.
WEB_PORT ?= 4321

# The dev Postgres is a single docker container this Makefile owns end-to-end: `pg-up` adopts an
# existing container by NAME (so a hand-made one keeps its data) or creates it from the pgvector
# image (the same one the test suite uses — migration 0015 needs CREATE EXTENSION vector) with a
# named volume. `make down` stops it; `make restart` deliberately does NOT (bouncing the DB on
# every code-change restart would drop pools for no reason).
PG_CONTAINER ?= theygent-pg
PG_IMAGE ?= pgvector/pgvector:pg16
PG_HOST_PORT ?= 5432
PG_VOLUME ?= theygent-pgdata

# The sample observability stack (deploy/otel): OTel collector (:4318) → Tempo → Grafana (:3000).
OTEL_COMPOSE := deploy/otel/docker-compose.yml

.PHONY: help install engines migrate up start restart down down-apps status logs pg-up pg-down otel-up otel-down otel-logs test test-py test-web test-deploy gen-ir-types hooks lint docs-serve docs-build web-up web-down docker-build docker-up docker-down docker-logs k8s-load k8s-apply k8s-up k8s-forward k8s-forward-stop k8s-down k8s-delete

help: ## Show this help
	@grep -hE '^[a-zA-Z0-9_-]+:.*?## .*$$' $(MAKEFILE_LIST) \
		| awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-12s\033[0m %s\n", $$1, $$2}'

# --all-extras: the OTLP export deps are an optional extra (theygent-control-plane[otlp]) so the
# core keeps no hard OTel dependency — but a dev machine wants them installed, and a bare
# `uv sync` would UNINSTALL them again (sync is exact). The only extras in the workspace are the
# two otlp ones, so this stays cheap.
install: ## uv sync incl. extras (Python workspace) + pnpm install (TS workspace)
	@echo "==> uv sync"
	uv sync --all-extras
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

up: install pg-up migrate start ## Full bring-up: install, Postgres, migrate, then start all services

restart: down-apps start ## Stop the app services, then start them again (Postgres keeps running)

pg-up: ## Start the dev Postgres container (adopts an existing one; creates it on first run)
	@if [ -n "$$(docker ps -q -f name=^$(PG_CONTAINER)$$)" ]; then \
		echo "==> postgres: already running ($(PG_CONTAINER))"; \
	elif [ -n "$$(docker ps -aq -f name=^$(PG_CONTAINER)$$)" ]; then \
		echo "==> postgres: starting existing container $(PG_CONTAINER)"; \
		docker start $(PG_CONTAINER) >/dev/null; \
	else \
		echo "==> postgres: creating $(PG_CONTAINER) ($(PG_IMAGE), :$(PG_HOST_PORT), volume $(PG_VOLUME))"; \
		docker run -d --name $(PG_CONTAINER) \
			-e POSTGRES_USER=theygent -e POSTGRES_PASSWORD=theygent -e POSTGRES_DB=theygent \
			-p $(PG_HOST_PORT):5432 -v $(PG_VOLUME):/var/lib/postgresql/data \
			$(PG_IMAGE) >/dev/null; \
	fi
	@n=0; until docker exec $(PG_CONTAINER) pg_isready -U theygent -d theygent >/dev/null 2>&1; do \
		n=$$((n+1)); if [ $$n -gt 60 ]; then echo "postgres did not become ready in 30s"; exit 1; fi; sleep 0.5; \
	done; echo "==> postgres: ready on :$(PG_HOST_PORT)"

pg-down: ## Stop the dev Postgres container (data survives in its volume)
	@if [ -n "$$(docker ps -q -f name=^$(PG_CONTAINER)$$)" ]; then \
		echo "==> stopping postgres ($(PG_CONTAINER))"; docker stop $(PG_CONTAINER) >/dev/null; \
	else \
		echo "==> postgres: not running"; \
	fi

# Each service launches only if its port is free, so a relaunch over a still-running instance is
# skipped LOUDLY instead of silently failing to bind and leaving the OLD process serving (the trap
# that hid stale code). $$! (the uv/pnpm wrapper pid) is recorded only as a hint; `down` resolves the
# real server by port.
start: pg-up ## Start inference-plane + control-plane + interface as background processes
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
down: down-apps pg-down ## Stop all bare-metal services AND the dev Postgres container

down-apps: ## Stop the app services only (resolved by PORT; never touches docker stacks or Postgres)
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

status: ## Show whether each service (and the dev Postgres) is running
	@if [ -n "$$(docker ps -q -f name=^$(PG_CONTAINER)$$ 2>/dev/null)" ]; then \
		echo "  postgres: running (docker $(PG_CONTAINER), :$(PG_HOST_PORT))"; \
	else \
		echo "  postgres: stopped"; \
	fi
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

# ── Sample observability stack (deploy/otel) — the quickest way to SEE the OTLP export the
# Settings → Telemetry page configures: an OTel collector receiving on :4318, forwarding to
# Tempo (trace store), with Grafana on :3000 pre-provisioned (Tempo datasource + a traces
# dashboard, anonymous login). Entirely separate from the app compose stack.

otel-up: ## Start the sample OTel stack (collector :4318 → Tempo → Grafana :3000)
	docker compose -f $(OTEL_COMPOSE) up -d
	@echo ""
	@echo "Collector ready. In Settings → Telemetry set the OTLP endpoint to:"
	@echo "    http://127.0.0.1:4318/v1/traces"
	@echo "enable export, hit 'Test connection', then run any agent."
	@echo "Grafana: http://localhost:3000 (dashboard: TheYgent → Traces, no login needed)"

otel-down: ## Stop the sample OTel stack (Tempo's trace volume survives)
	docker compose -f $(OTEL_COMPOSE) down

otel-logs: ## Tail the sample OTel stack (the collector's debug exporter prints every span)
	docker compose -f $(OTEL_COMPOSE) logs -f otel-collector

# ── Kubernetes (deploy/k8s) — dev flow against minikube. The images are built locally by
# compose and side-loaded; imagePullPolicy stays IfNotPresent.

K8S_IMAGES := theygent/control-plane:dev theygent/inference-plane:dev theygent/worker:dev theygent/interface:dev
K8S_DEPLOYS := control-plane inference-plane worker interface

# NOT `minikube image load`: with the docker driver it silently keeps an EXISTING same-tag
# image (even with --overwrite), so a rebuilt :dev never reaches the cluster — pods keep
# running stale code. save + load against minikube's own daemon replaces the tag reliably.
# The `docker save` runs with the minikube docker-env explicitly stripped (`env -u`): if the
# caller's shell already ran `eval $(minikube docker-env)`, an unguarded save would read the
# OLD image straight from minikube and load it back — a silent no-op that strands stale pods.
# So: save from the host daemon, load into minikube. `--shell bash` forces sh-parsable output
# regardless of $SHELL (fish/zsh emit different syntax that a POSIX `eval` can't apply).
k8s-load: docker-build ## Build images and load them into minikube (force-replaces same-tag images)
	@for img in $(K8S_IMAGES); do \
		echo "==> loading $$img into minikube"; \
		env -u DOCKER_HOST -u DOCKER_TLS_VERIFY -u DOCKER_CERT_PATH -u MINIKUBE_ACTIVE_DOCKERD \
			docker save $$img -o /tmp/theygent-k8s-img.tar; \
		( eval "$$(minikube -p minikube docker-env --shell bash)" && docker load -i /tmp/theygent-k8s-img.tar ); \
		rm -f /tmp/theygent-k8s-img.tar; \
	done

# Depends on k8s-load so an apply always ships freshly built images. `apply -k` alone is a
# no-op when the manifests are unchanged (same :dev tag, imagePullPolicy IfNotPresent), so a
# reloaded image would never reach running pods — `rollout restart` is what rebinds them.
k8s-apply: k8s-load ## Build+load latest images, apply manifests, and roll pods onto them
	kubectl apply -k deploy/k8s
	kubectl -n theygent rollout restart deployment $(K8S_DEPLOYS)
	@for d in $(K8S_DEPLOYS); do \
		echo "==> waiting for rollout: $$d"; \
		kubectl -n theygent rollout status deployment/$$d --timeout=180s; \
	done

# One-shot dev flow: build → load → deploy → refresh pods → port-forward. ClusterIP services
# are unreachable from the host, so the forward is what actually makes the stack usable.
k8s-up: k8s-apply k8s-forward ## Deploy latest images to minikube and port-forward the stack

# Host ports mirror the make/compose contract (control 8080, inference 8081, interface 5174)
# so the interface image's baked-in localhost URLs resolve. The interface Service listens on
# :80, hence 5174:80. `trap 'kill 0'` tears down all three forwards on Ctrl-C — but only this
# invocation's group, so an ORPHANED forward from a crashed/prior run keeps holding a port and
# the next start dies with "address already in use". k8s-forward-stop reaps any such strays
# first (matched by the namespace flag, so only our forwards are touched, never other kubectl).
k8s-forward: k8s-forward-stop ## Port-forward control-plane:8080, inference:8081, interface:5174 (Ctrl-C to stop)
	@echo "port-forwarding — control http://localhost:8080  inference http://localhost:8081  interface http://localhost:5174"
	@echo "(Ctrl-C to stop)"
	@trap 'kill 0' INT TERM EXIT; \
	kubectl -n theygent port-forward svc/control-plane 8080:8080 & \
	kubectl -n theygent port-forward svc/inference-plane 8081:8081 & \
	kubectl -n theygent port-forward svc/interface 5174:80 & \
	wait

k8s-forward-stop: ## Kill any lingering theygent port-forwards (frees 8080/8081/5174)
	@pkill -f 'kubectl -n theygent port-forward' 2>/dev/null && echo "stopped stray port-forwards" || echo "no port-forwards running"

# Full teardown: `delete -k` removes every listed manifest — the namespace and all three PVCs
# (theygent-data, inference-data, pgdata), so registry, weights, artifacts, and the Postgres
# volume all go with it. --ignore-not-found makes a re-run (or a partial cluster) a clean no-op.
# Also reaps port-forwards (host processes, not cluster resources — a delete alone leaves them
# looping, retrying against a namespace that no longer exists and holding the host ports).
k8s-down: k8s-forward-stop ## Tear down everything in the cluster, PVC data included (namespace, planes, Postgres, volumes)
	kubectl delete -k deploy/k8s --ignore-not-found

# Back-compat alias for the teardown.
k8s-delete: k8s-down ## Alias for k8s-down

docs-serve: ## Serve the user docs locally with live reload (http://127.0.0.1:8000)
	uv run --project docs/user-docs mkdocs serve -f docs/user-docs/mkdocs.yml

docs-build: ## Build the user docs site (strict — broken links fail the build)
	uv run --project docs/user-docs mkdocs build --strict -f docs/user-docs/mkdocs.yml

web-up: ## Serve the marketing site (apps/web) in the background (http://localhost:4321)
	@mkdir -p $(RUN_DIR)
	@if lsof -ti tcp:$(WEB_PORT) -sTCP:LISTEN >/dev/null 2>&1; then \
		echo "==> web already on :$(WEB_PORT) — skipping ('make web-down' stops it)"; \
	else \
		echo "==> starting web (marketing site, :$(WEB_PORT))"; \
		nohup python3 -m http.server $(WEB_PORT) --directory apps/web > $(RUN_DIR)/web.log 2>&1 & \
		echo $$! > $(RUN_DIR)/web.pid; \
	fi
	@echo "Marketing site: http://localhost:$(WEB_PORT)  |  Log: $(RUN_DIR)/web.log  |  Stop: make web-down"

web-down: ## Stop the marketing-site server (resolved by port, like down-apps)
	@pids=$$(lsof -ti tcp:$(WEB_PORT) -sTCP:LISTEN 2>/dev/null || true); \
	if [ -z "$$pids" ]; then echo "==> web: not running"; rm -f $(RUN_DIR)/web.pid; exit 0; fi; \
	echo "==> stopping web (:$(WEB_PORT))"; \
	kill -TERM $$pids 2>/dev/null || true; \
	n=0; while [ $$n -lt 20 ] && lsof -ti tcp:$(WEB_PORT) -sTCP:LISTEN >/dev/null 2>&1; do sleep 0.25; n=$$((n+1)); done; \
	for pid in $$(lsof -ti tcp:$(WEB_PORT) -sTCP:LISTEN 2>/dev/null); do \
		echo "   force-killing :$(WEB_PORT) (pid $$pid)"; kill -9 $$pid 2>/dev/null || true; \
	done; \
	rm -f $(RUN_DIR)/web.pid
