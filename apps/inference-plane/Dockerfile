# theygent inference plane — engine manager + L0 gateway, with a CPU llama-server bundled.
#
# Build from the REPO ROOT (the uv workspace needs every member's manifest):
#   docker build -f apps/inference-plane/Dockerfile -t theygent/inference-plane:dev .
#
# Engine coverage inside a Linux container:
#   * llamacpp — bundled (the CPU build below): chat, embeddings, vision.
#   * openai-compatible — always available; it is just HTTP + a URL.
#   * mlx — Apple-Silicon Metal only, it CANNOT run in a Linux container. To use MLX, run the
#     inference plane bare-metal on the Mac (make up / uv run theygent-inference-plane) and point
#     the containerized control-plane at it (see docker-compose.host-inference.yml).
#   * vllm — belongs on a CUDA host; reach it as openai-compatible, or extend this image on a
#     GPU node.
#   /readyz reports the honest per-(engine, modality) breakdown either way.

ARG PYTHON_BASE=python:3.12-slim-trixie
# The upstream CPU server build (multi-arch: amd64 + arm64). We copy the binary + its shared
# libraries out of it rather than building llama.cpp from source here. PINNED to a specific
# upstream build (the bare `server` tag floats with every upstream commit); bump deliberately
# and re-run a real chat when doing so.
ARG LLAMACPP_IMAGE=ghcr.io/ggml-org/llama.cpp:server-b9994

FROM ${LLAMACPP_IMAGE} AS llamacpp

FROM ghcr.io/astral-sh/uv:0.11 AS uv

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
    uv sync --locked --no-dev --no-install-workspace --package theygent-inference-plane

COPY packages/ir/src packages/ir/src
COPY apps/inference-plane/src apps/inference-plane/src
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --locked --no-dev --no-editable --package theygent-inference-plane

FROM ${PYTHON_BASE}
# llama-server links libgomp (OpenMP) and libcurl; curl doubles as the container healthcheck.
RUN apt-get update \
    && apt-get install -y --no-install-recommends libgomp1 curl ca-certificates \
    && rm -rf /var/lib/apt/lists/* \
    # NUMERIC uid/gid (10001): Kubernetes securityContext (runAsUser/fsGroup in deploy/k8s)
    # must reference the same ids so freshly-provisioned PVCs become writable.
    && groupadd -r -g 10001 theygent && useradd -r -u 10001 -g theygent -d /app -s /usr/sbin/nologin theygent \
    && mkdir -p /data && chown -R theygent:theygent /data
WORKDIR /app
COPY --from=llamacpp /app /opt/llamacpp
COPY --from=builder --chown=theygent:theygent /app/.venv /app/.venv

ENV PATH="/app/.venv/bin:$PATH" \
    LD_LIBRARY_PATH=/opt/llamacpp \
    THEYGENT_LLAMACPP_BIN=/opt/llamacpp/llama-server \
    THEYGENT_INFERENCE_PLANE_HOST=0.0.0.0 \
    THEYGENT_INFERENCE_PLANE_STATE_DIR=/data \
    HF_HOME=/data/hf \
    LLAMA_CACHE=/data/llama-cache

# Fail the build loudly if the copied binary cannot execute on this base (glibc/arch drift).
RUN /opt/llamacpp/llama-server --version

USER theygent
EXPOSE 8081
CMD ["theygent-inference-plane"]
