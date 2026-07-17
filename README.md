# Theygent

TBD

## Getting started (development)

```bash
cp .env.example .env   # set DATABASE_URL (a reachable Postgres)
make engines           # local engine servers (macOS): llama.cpp, whisper.cpp, ffmpeg,
                       # mlx-lm, mlx-vlm, mlx-audio — chat, vision, embeddings,
                       # speech-to-text, text-to-speech
make up                # install deps, migrate, start inference-plane (8081) +
                       # control-plane (8080) + interface (5174)
```

`make help` lists everything else (status, logs, restart, test, lint). `/readyz` on the
inference plane reports which (engine, modality) slots this host can actually run.

## Running with Docker (the second way)

The same stack, containerized — Postgres (pgvector) included, same ports:

```bash
make docker-up         # postgres + migrations + inference-plane + control-plane + interface
make docker-down       # stop (data volumes survive; `docker compose down -v` drops them)
```

The containerized inference plane bundles a CPU llama.cpp build (chat, embeddings, vision).
MLX is Apple-Silicon-bare-metal only — to keep local MLX inference, run the inference plane
on the host and overlay `docker-compose.host-inference.yml` (see that file's header). The
standalone durable worker is opt-in: `THEYGENT_DURABLE=1 docker compose --profile worker up -d`.
JS-rendered crawling for retrieval sources (`render_js`) needs a browser baked into the
control-plane image — opt in with `WITH_JS_RENDER=1 docker compose build control-plane`
(~500MB); without it, static fetch still works and a `render_js` ingest fails with the
rebuild hint.

## Kubernetes (deploy/k8s)

Dev flow against minikube — images built locally, side-loaded, applied with kustomize:

```bash
make k8s-load          # docker compose build + minikube image load
make k8s-apply         # kubectl apply -k deploy/k8s
kubectl -n theygent port-forward svc/interface 5174:80       # then :8080 / :8081 likewise
```

The control-plane and worker carry HorizontalPodAutoscalers (scale up/down with load);
`deploy/tests/` guards the Dockerfiles, compose files, and manifests in CI.

## License

See [LICENSE.md](LICENSE.md).
