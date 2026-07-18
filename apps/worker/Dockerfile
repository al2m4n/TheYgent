# theygent worker — the standalone durable runtime (DBOS) against the shared Postgres.
#
# Build from the REPO ROOT (the uv workspace needs every member's manifest):
#   docker build -f apps/worker/Dockerfile -t theygent/worker:dev .
#
# No ports: the worker consumes the durable queue and recovers pending workflows; it reaches
# inference only over THEYGENT_INFERENCE_PLANE_URL (the plane boundary holds in containers too).

ARG PYTHON_BASE=python:3.12-slim-trixie
# Stdio MCP launchers (npx/uvx) ship by default — the worker spawns hub-installed servers
# in ITS trust domain during durable runs, so it mirrors the control-plane image. Build
# with WITH_MCP_LAUNCHERS=0 for a slimmer image.
ARG WITH_MCP_LAUNCHERS=1

FROM ghcr.io/astral-sh/uv:0.11 AS uv
FROM node:22-slim AS node-src

FROM ${PYTHON_BASE} AS builder
COPY --from=uv /uv /bin/uv
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_PYTHON_DOWNLOADS=never
WORKDIR /app

COPY pyproject.toml uv.lock ./
COPY apps/control-plane/pyproject.toml apps/control-plane/
COPY apps/inference-plane/pyproject.toml apps/inference-plane/
COPY apps/worker/pyproject.toml apps/worker/
COPY packages/ir/pyproject.toml packages/ir/
COPY packages/gateway-client/pyproject.toml packages/gateway-client/
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --locked --no-dev --no-install-workspace --package theygent-worker --extra otlp

# The worker imports the control-plane's durable package + stores, so its source ships too.
COPY packages/ir/src packages/ir/src
COPY packages/gateway-client/src packages/gateway-client/src
COPY apps/control-plane/src apps/control-plane/src
COPY apps/worker/src apps/worker/src
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --locked --no-dev --no-editable --package theygent-worker --extra otlp

# runtime-base-0: no launchers (WITH_MCP_LAUNCHERS=0).
FROM ${PYTHON_BASE} AS runtime-base-0

# runtime-base-1: node + npm/npx copied from the slim node image (see the control-plane
# Dockerfile for the full rationale) — keep the two images' launcher layers identical.
FROM ${PYTHON_BASE} AS runtime-base-1
RUN apt-get update && apt-get install -y --no-install-recommends libstdc++6 ca-certificates \
    && rm -rf /var/lib/apt/lists/*
COPY --from=node-src /usr/local/bin/node /usr/local/bin/node
COPY --from=node-src /usr/local/lib/node_modules/npm /usr/local/lib/node_modules/npm
RUN ln -s /usr/local/lib/node_modules/npm/bin/npm-cli.js /usr/local/bin/npm \
    && ln -s /usr/local/lib/node_modules/npm/bin/npx-cli.js /usr/local/bin/npx \
    && node --version && npx --version

FROM runtime-base-${WITH_MCP_LAUNCHERS}
COPY --from=uv /uv /uvx /bin/
# NUMERIC uid/gid (10001): Kubernetes securityContext (runAsUser/fsGroup in deploy/k8s) must
# reference the same ids so freshly-provisioned PVCs become writable — keep them in lockstep.
# HOME lives on the /data volume: stdio MCP servers spawn with a scrubbed environment (the
# SDK passes only HOME/PATH/…), so launcher caches (~/.npm, ~/.cache/uv) derive from HOME —
# it must be writable and should persist across restarts.
RUN groupadd -r -g 10001 theygent \
    && useradd -r -u 10001 -g theygent -d /data/home -s /usr/sbin/nologin theygent \
    && mkdir -p /data/artifacts /data/home && chown -R theygent:theygent /data
WORKDIR /app
COPY --from=builder --chown=theygent:theygent /app/.venv /app/.venv

ENV PATH="/app/.venv/bin:$PATH" \
    HOME=/data/home \
    THEYGENT_ARTIFACT_DIR=/data/artifacts
USER theygent
CMD ["theygent-worker"]
