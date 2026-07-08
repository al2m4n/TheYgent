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

## License

See [LICENSE.md](LICENSE.md).
