# check=skip=SecretsUsedInArgOrEnv
# (The build check reads "AUTH" in THEYGENT_AI_GATEWAY_AUTH_MODE as a secret; it names a mode.
# The gateway's secrets are never baked: they arrive at run time.)
#
# theygent ai-gateway — the Anthropic-Messages proxy between coding agents and their models.
#
# Build from the REPO ROOT (the uv workspace needs every member's manifest):
#   docker build -f apps/ai-gateway/Dockerfile -t theygent/ai-gateway:dev .
#
# A container only runs team mode: personal mode accepts requests without a gateway key, so it
# listens on loopback only, which a published container port cannot reach. Team mode needs the
# control plane's address and a gateway service key at run time:
#   THEYGENT_CONTROL_PLANE_URL, THEYGENT_AI_GATEWAY_SERVICE_KEY

ARG PYTHON_BASE=python:3.12-slim-trixie

FROM ghcr.io/astral-sh/uv:0.11 AS uv

FROM ${PYTHON_BASE} AS builder
COPY --from=uv /uv /bin/uv
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_PYTHON_DOWNLOADS=never
WORKDIR /app

# Dependency layer: lockfile + every workspace member's manifest (uv validates the lock
# against all of them), no sources yet — third-party deps cache until the lock changes.
COPY pyproject.toml uv.lock ./
COPY apps/ai-gateway/pyproject.toml apps/ai-gateway/
COPY apps/control-plane/pyproject.toml apps/control-plane/
COPY apps/inference-plane/pyproject.toml apps/inference-plane/
COPY apps/worker/pyproject.toml apps/worker/
COPY packages/ir/pyproject.toml packages/ir/
COPY packages/gateway-client/pyproject.toml packages/gateway-client/
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --locked --no-dev --no-install-workspace --package theygent-ai-gateway

# Source layer: only the members the gateway imports (its wire types live in packages/ir; the
# local route reaches the inference plane through packages/gateway-client).
COPY packages/ir/src packages/ir/src
COPY packages/gateway-client/src packages/gateway-client/src
COPY apps/ai-gateway/src apps/ai-gateway/src
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --locked --no-dev --no-editable --package theygent-ai-gateway

FROM ${PYTHON_BASE}
# NUMERIC uid/gid (10001): Kubernetes securityContext (runAsUser/fsGroup in deploy/k8s) must
# reference the same ids so freshly-provisioned PVCs become writable — keep them in lockstep.
RUN groupadd -r -g 10001 theygent \
    && useradd -r -u 10001 -g theygent -d /app -s /usr/sbin/nologin theygent \
    && mkdir -p /data && chown -R theygent:theygent /data
WORKDIR /app
# The venv path must match the builder's — console-script shebangs are absolute.
COPY --from=builder --chown=theygent:theygent /app/.venv /app/.venv

# /data holds the request-log spool (records not yet exported), the last known config and key
# set, and a single-instance org key: mount a volume so a restart keeps them.
ENV PATH="/app/.venv/bin:$PATH" \
    THEYGENT_AI_GATEWAY_HOST=0.0.0.0 \
    THEYGENT_AI_GATEWAY_AUTH_MODE=team \
    THEYGENT_AI_GATEWAY_STATE_DIR=/data
USER theygent
EXPOSE 8082
CMD ["theygent-ai-gateway"]
