# Installation

This page takes you from nothing to an installed TheYgent: clone the repository, create your configuration, and install the local model engines and dependencies. When you are done here, move on to [Running TheYgent](running.md) to start the services.

## Before you start

Make sure the prerequisites from the [getting-started overview](index.md#what-you-need) are in place: **Python 3.12** (managed for you by `uv`), **uv** and **pnpm** on your `PATH`, a reachable **PostgreSQL** server, and — on macOS — **Homebrew** for the engine install.

A quick sanity check:

```bash
uv --version
pnpm --version
```

!!! note "You supply Postgres"
    TheYgent does not provision a database. Point it at any Postgres you already run — a native install, a container, or a managed instance. All it needs is a database it can reach at the `DATABASE_URL` you set below.

## 1. Clone the repository

```bash
git clone https://github.com/al2m4n/theygent.git
cd theygent
```

Run every command below (and every `make` target) from this repository root.

## 2. Create your `.env`

Copy the example file and open it:

```bash
cp .env.example .env
```

The one value you **must** set is `DATABASE_URL` — an async Postgres connection string in the `postgresql+asyncpg://` form. The example ships with a sensible default:

```bash
DATABASE_URL=postgresql+asyncpg://theygent:theygent@localhost:5432/theygent
```

The other keys already have working defaults; you rarely need to change them for a local install:

| Key | Default | What it does |
|---|---|---|
| `DATABASE_URL` | *(the example DSN above)* | Postgres connection for the control plane. Migrations and the control plane fail loudly if this is unset or unreachable. |
| `THEYGENT_INFERENCE_PLANE_URL` | `http://127.0.0.1:8081/v1` | Where the control plane reaches the inference plane. Must include the `/v1` suffix. |
| `THEYGENT_INFERENCE_PLANE_HOST` / `_PORT` | `127.0.0.1` / `8081` | The inference plane's listen address. |
| `THEYGENT_CONTROL_PLANE_HOST` / `_PORT` | `127.0.0.1` / `8080` | The control plane's listen address. |
| `THEYGENT_MAX_RESIDENT` | `2` | How many local model engines may be loaded at once. |
| `THEYGENT_CORS_ORIGINS` | localhost / 127.0.0.1 on `:5173` and `:5174` when unset | Browser origins allowed to call both planes. The shipped `.env.example` pins it to `http://localhost:5174`; leave it unless you serve the interface from another port. |

The full list, including durable-mode and secret-store variables, is in the [environment reference](../reference/environment.md).

## 3. Install the local engines

The local model engines (llama.cpp, whisper.cpp, ffmpeg, and the MLX servers) are separate binaries. On macOS, one target installs them:

```bash
make engines
```

It is idempotent — rerun it after pulling new code. What it installs:

- **`brew install llama.cpp whisper-cpp ffmpeg`** — `llama-server` (chat, embeddings, and vision), `whisper-server` (speech-to-text), and `ffmpeg` (converts microphone recordings from the browser).
- **`uv tool install mlx-lm`** — MLX chat (Apple Silicon only).
- **`uv tool install mlx-vlm`** — MLX vision (Apple Silicon only).
- **`uv tool install mlx-audio` (with extras)** — MLX text-to-speech (Apple Silicon only).

vLLM is deliberately **not** installed by this target — it belongs on an NVIDIA/CUDA host.

=== "Apple Silicon Mac"
    `make engines` installs the full set above. The MLX servers give you fast local chat, vision, and text-to-speech; llama.cpp and whisper.cpp cover the rest.

=== "Linux / other"
    `make engines` prints guidance instead of installing. Install `llama-server` (llama.cpp), `whisper-server` (whisper.cpp), and `ffmpeg` with your package manager. The MLX servers are unavailable off Apple Silicon.

=== "NVIDIA GPU host"
    Install vLLM directly on the GPU host with `pip install vllm`. It is not part of `make engines`.

!!! tip "Engines are optional if you only use remote models"
    You can skip local engines entirely and register any OpenAI-compatible server or hosted API as a [remote model](../models/remote.md). To follow [Your first chat](first-chat.md), though, install at least one local engine. Which engine fits which machine is covered in [Engines](../models/engines.md).

## 4. Install dependencies and create the schema

The recommended single command, [`make up`](running.md), installs dependencies, applies the database schema, and starts all three services in one step — so from a fresh clone with `.env` set and `make engines` done, you can jump straight to it.

If you prefer to run the steps separately:

```bash
make install   # uv sync (Python) + pnpm install (interface)
make migrate   # apply the control-plane database schema
```

`make migrate` runs the database migrations against your `DATABASE_URL`; it stops with a clear error if that variable is unset.

## Where things land

After installation, TheYgent keeps your data in three places, all under your control:

- **`~/.theygent/inference/`** — the local model registry, downloaded model weights (under `models/`), and local credentials for remote APIs.
- **Your Postgres** (`DATABASE_URL`) — agents and their versions, runs, chat sessions, triggers, MCP registrations, connections, and observability traces.
- **`.run/`** — development log files and process ids (ignored by git).

## Next

Head to [Running TheYgent](running.md) to bring the services up and confirm they're healthy.
