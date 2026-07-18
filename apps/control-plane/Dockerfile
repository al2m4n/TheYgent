# theygent control-plane — FastAPI API surface (+ Alembic migrations baked in).
#
# Build from the REPO ROOT (the uv workspace needs every member's manifest):
#   docker build -f apps/control-plane/Dockerfile -t theygent/control-plane:dev .
#
# The runtime image carries only the built virtualenv (workspace packages installed as
# wheels via --no-editable) plus the Alembic tree, so migrations run from the same image:
#   docker run --rm theygent/control-plane:dev alembic -c /app/alembic.ini upgrade head

ARG PYTHON_BASE=python:3.12-slim-trixie
# Hub-installed stdio MCP servers spawn via their package launcher: npx (npm) and uvx
# (pypi). The runtime image bundles both by default — build with WITH_MCP_LAUNCHERS=0 for a
# slimmer image (the value picks the runtime-base stage below). docker/dnx launchers are
# never bundled: the install route fails those fast, and remote (http/sse) or generated
# (openapi/graphql) connections work regardless.
ARG WITH_MCP_LAUNCHERS=1

FROM ghcr.io/astral-sh/uv:0.11 AS uv
FROM node:22-slim AS node-src

FROM ${PYTHON_BASE} AS builder
COPY --from=uv /uv /bin/uv
# Compile bytecode at build time (faster cold start), copy instead of hardlinking out of the
# cache mount, and never let uv download its own interpreter — the image's python is the one
# the final stage has.
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_PYTHON_DOWNLOADS=never
WORKDIR /app

# Dependency layer: lockfile + every workspace member's manifest (uv validates the lock
# against all of them), no sources yet — third-party deps cache until the lock changes.
COPY pyproject.toml uv.lock ./
COPY apps/control-plane/pyproject.toml apps/control-plane/
COPY apps/inference-plane/pyproject.toml apps/inference-plane/
COPY apps/worker/pyproject.toml apps/worker/
COPY packages/ir/pyproject.toml packages/ir/
COPY packages/gateway-client/pyproject.toml packages/gateway-client/
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --locked --no-dev --no-install-workspace --package theygent-control-plane --extra otlp

# Source layer: only the members this service actually imports.
COPY packages/ir/src packages/ir/src
COPY packages/gateway-client/src packages/gateway-client/src
COPY apps/control-plane/src apps/control-plane/src
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --locked --no-dev --no-editable --package theygent-control-plane --extra otlp

# runtime-base-0: no launchers (WITH_MCP_LAUNCHERS=0).
FROM ${PYTHON_BASE} AS runtime-base-0

# runtime-base-1: node + npm/npx copied from the slim node image — a fraction of the size
# of the distro nodejs+npm dependency tree, and a current node. The npm CLI is kept at its
# real path so its relative requires resolve; npx/npm are symlink shims to it.
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
# The venv path must match the builder's — console-script shebangs are absolute.
COPY --from=builder --chown=theygent:theygent /app/.venv /app/.venv
# Alembic runs from this image (compose one-shot / k8s initContainer); alembic.ini resolves
# script_location relative to itself (%(here)s/alembic).
COPY --chown=theygent:theygent apps/control-plane/alembic.ini /app/alembic.ini
COPY --chown=theygent:theygent apps/control-plane/alembic /app/alembic

# Optional JS-rendered crawling (retrieval sources with render_js): bake chromium + its
# system libraries into the image — a runtime `playwright install` cannot work in a
# container (system libs need root; the download dies with the container filesystem).
# Off by default (~500MB); enable with --build-arg WITH_JS_RENDER=1. The browsers path is
# system-wide (outside /data) so a data volume never shadows it; set unconditionally so a
# browserless build still fails with the deployment-aware hint, not a half-install.
ARG WITH_JS_RENDER=0
ENV PLAYWRIGHT_BROWSERS_PATH=/opt/ms-playwright
RUN if [ "$WITH_JS_RENDER" = "1" ]; then \
        /app/.venv/bin/playwright install --with-deps chromium \
        && rm -rf /var/lib/apt/lists/* \
        && chmod -R a+rX /opt/ms-playwright; \
    fi

ENV PATH="/app/.venv/bin:$PATH" \
    HOME=/data/home \
    THEYGENT_CONTROL_PLANE_HOST=0.0.0.0 \
    THEYGENT_ARTIFACT_DIR=/data/artifacts
USER theygent
EXPOSE 8080
CMD ["theygent-control-plane"]
