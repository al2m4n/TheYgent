# AGENTS.md — deploy

How the stack runs outside `make up`: Kubernetes manifests, the sample OpenTelemetry stack, and
the contract tests that keep every Dockerfile, compose file and manifest in step with the code.
The two compose files live at the repo root but belong to this area. The repo-wide rules in the
root [AGENTS.md](../AGENTS.md) apply first; the design is in
[deployment.md](../docs/dev-docs/deployment.md).

## The three run modes

The same process types run in every mode (inference plane :8081, control plane :8080, interface
:5174, AI gateway :8082, worker, Postgres with pgvector); only the wiring changes.

| Mode | Entry | `THEYGENT_DURABLE` | Notes |
|------|-------|--------------------|-------|
| Bare metal | `make up` ([Makefile](../Makefile)) | `0` | Postgres in a docker container the Makefile owns; services tracked by port |
| Compose | `make docker-up` ([docker-compose.yml](../docker-compose.yml)) | `0` by default | Profiles `worker` and `gateway`; Postgres on host port 5433; [docker-compose.host-inference.yml](../docker-compose.host-inference.yml) keeps the inference plane on the host for MLX |
| Kubernetes | `make k8s-up` ([k8s/](k8s)) | `1` | The control plane scales behind an HPA, so schedules must run on DBOS (the in-process dispatcher would double-fire) |

## Map

<!-- agents-map: covers=. -->
<!-- agents-map: covers=k8s -->
<!-- agents-map: covers=otel -->
<!-- agents-map: covers=tests -->

| Path | What it is |
|------|------------|
| [`k8s/kustomization.yaml`](k8s/kustomization.yaml) | `kubectl apply -k deploy/k8s`; the resource list below |
| [`k8s/namespace.yaml`](k8s/namespace.yaml), [`k8s/config.yaml`](k8s/config.yaml) | Namespace `theygent`; `theygent-config` ConfigMap (shared non-secret env, `THEYGENT_DURABLE=1`) and the dev-grade `theygent-secrets` |
| [`k8s/storage.yaml`](k8s/storage.yaml) | PVCs: `theygent-data` (artifacts, shared by control plane and worker), `inference-data` (registry and weights) |
| [`k8s/postgres.yaml`](k8s/postgres.yaml) | In-cluster Postgres with pgvector (replace with a managed one in production) |
| [`k8s/inference-plane.yaml`](k8s/inference-plane.yaml) | Not horizontally scaled: it pins weights and owns local state. Can run outside the cluster instead |
| [`k8s/control-plane.yaml`](k8s/control-plane.yaml) | The API with its `migrate` init container (`alembic upgrade head`) and an HPA |
| [`k8s/worker.yaml`](k8s/worker.yaml) | The durable worker with an HPA |
| [`k8s/interface.yaml`](k8s/interface.yaml) | nginx serving the SPA; `VITE_*` URLs are baked in at image build |
| [`k8s/ai-gateway.yaml`](k8s/ai-gateway.yaml) | A StatefulSet in team mode with an HPA (one volume per replica for its spool and last-known config); waits for its Secret |
| [`otel/`](otel) | `make otel-up` ([`docker-compose.yml`](otel/docker-compose.yml)): the collector ([`otel-collector.yaml`](otel/otel-collector.yaml), :4318) → Tempo ([`tempo.yaml`](otel/tempo.yaml)) → Grafana (:3000; [`grafana/`](otel/grafana) provisions the Tempo datasource and a traces dashboard) |
| [`tests/`](tests) | Contract guards: `test_dockerfiles.py`, `test_compose.py`, `test_k8s.py`; shared constants in `contract_utils.py`, fixtures in `conftest.py` |

The images are built from each app's `Dockerfile` (`apps/control-plane`, `apps/inference-plane`,
`apps/worker`, `apps/interface`, `apps/ai-gateway`), tagged `theygent/<name>:dev`.

## Rules

- **A change to a service's ports, env vars, volumes, entrypoint or dependencies updates every
  mode that runs it** (Makefile, compose, k8s) and passes `make test-deploy`.
- **New env var:** add it to `.env.example`, to compose (as `${VAR:-}` so empty means unset) and to
  the ConfigMap or Secret that needs it, plus `docs/user-docs/content/reference/environment.md`.
- **If `THEYGENT_DURABLE` in k8s ever flips to `0`, drop the control-plane HPA in the same
  change.** The guard checks the value and that the ConfigMap reaches the pods.
- **Migrations run before the API starts** in every mode (compose `migrate` service, k8s init
  container, `make migrate`).
- Secrets in these files are dev-grade placeholders; real deployments replace the Secret.

## Tests

```bash
make test-deploy     # uv run --package theygent-control-plane pytest deploy/tests
```

Static analysis of the files against the working tree; docker and kubectl sub-checks skip when
the tools are absent.
