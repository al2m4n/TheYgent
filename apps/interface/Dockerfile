# theygent interface — the visual builder SPA, built with the pnpm workspace and served
# as static files by nginx.
#
# Build from the REPO ROOT (the pnpm workspace needs the root manifests):
#   docker build -f apps/interface/Dockerfile -t theygent/interface:dev .
#
# The SPA calls the control-plane and inference-plane straight from the BROWSER, so the URLs
# are baked at build time (Vite inlines import.meta.env). The defaults —
# http://localhost:8080 and http://localhost:8081 — are right whenever those ports are
# published on the machine running the browser (docker compose, kubectl port-forward).
# Override for a real deployment:
#   docker build --build-arg VITE_CONTROL_PLANE_URL=https://api.example.com ...

ARG NODE_BASE=node:22-alpine
ARG NGINX_BASE=nginx:1.29-alpine-slim

FROM ${NODE_BASE} AS build
# corepack activates the exact pnpm pinned by package.json's packageManager field.
ENV COREPACK_ENABLE_DOWNLOAD_PROMPT=0
RUN corepack enable
WORKDIR /src

# Dependency layer: workspace manifests + lockfile only.
COPY package.json pnpm-lock.yaml pnpm-workspace.yaml ./
COPY apps/interface/package.json apps/interface/
COPY packages/ir-types/package.json packages/ir-types/
RUN pnpm install --frozen-lockfile

# Source layer. ir-types ships its generated output in-tree (the CI drift guard keeps it in
# lockstep with packages/ir), so no Python is needed to build the SPA.
COPY packages/ir-types packages/ir-types
COPY apps/interface apps/interface

ARG VITE_CONTROL_PLANE_URL=
ARG VITE_INFERENCE_URL=
# An EMPTY build arg must not reach Vite: it would inline "" and defeat the SPA's own
# localhost fallbacks — so unset the blanks before building.
RUN sh -c '[ -n "$VITE_CONTROL_PLANE_URL" ] || unset VITE_CONTROL_PLANE_URL; \
    [ -n "$VITE_INFERENCE_URL" ] || unset VITE_INFERENCE_URL; \
    pnpm --filter @theygent/interface build'

FROM ${NGINX_BASE}
COPY apps/interface/nginx.conf /etc/nginx/conf.d/default.conf
COPY --from=build /src/apps/interface/dist /usr/share/nginx/html
EXPOSE 80
