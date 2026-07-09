# Troubleshooting

Common problems, what causes them, and how to fix them. Most issues fall into one of a few buckets: a service didn't start, a model won't load, or a run surfaced an error code. When you hit an error code you don't recognize, the [API reference](api.md) lists where each one comes from.

!!! tip "First stop: the health checks"
    Before digging in, check both planes are up and ready:

    ```bash
    curl http://localhost:8081/readyz   # inference plane
    curl http://localhost:8080/readyz   # control plane
    ```

    The inference plane's `/readyz` tells you exactly which `(engine, modality)` slots are missing and why; the control plane's tells you if Postgres or the inference plane is unreachable. See [Running TheYgent](../getting-started/running.md).

## Services and startup

### A service didn't start — "already on :PORT — skipping"

**Cause.** Something is already listening on that port (a stale instance, or another app on `:8080`, `:8081`, or `:5174`). `make up` refuses to clobber it and skips that service loudly.

**Fix.** Stop the other process, then `make restart`. To stop TheYgent's own services, `make down`. Confirm what's up with `make status`, and read the per-service logs under `.run/` (`.run/control-plane.log`, `.run/inference-plane.log`, `.run/interface.log`) with `make logs`.

### The control plane won't come up / `/readyz` says the database is down

**Cause.** `DATABASE_URL` is unset, wrong, or points at a Postgres that isn't running. The control plane requires Postgres and fails readiness without it.

**Fix.** Set `DATABASE_URL` in `.env` to a reachable async DSN — `postgresql+asyncpg://user:pass@host:5432/dbname` — and make sure the database and user exist. You supply your own Postgres; TheYgent does not provision one. See [Installation](../getting-started/installation.md) and [`DATABASE_URL`](environment.md#required).

## Database and migrations

### "DATABASE_URL is not set (copy .env.example to .env)"

**Cause.** `make migrate` (or `make up`) ran without `DATABASE_URL` in the environment.

**Fix.** `cp .env.example .env`, set `DATABASE_URL`, and re-run. Migrations use the same variable — the migration tool raises `DATABASE_URL must be set for migrations` when it's missing.

### Migrations fail or the schema looks out of date

**Cause.** The control-plane schema hasn't been applied to your database, or a pull added new migrations.

**Fix.** Apply them:

```bash
make migrate
# or, directly:
uv run --package theygent-control-plane alembic upgrade head
```

Durable-runtime tables live in a separate `dbos` schema on the same Postgres and are migrated automatically when durable mode starts — there is no separate step for them.

## Models and engines

### `model_not_found` (404)

**Cause.** Either the `model` field is a [logical id](glossary.md#logical-model-id) you never registered, or you put an **engine name** (like `llamacpp` or `mlx`) where a logical id belongs. The `model` field is always a logical id, never an engine name.

**Fix.** Register the model on the [Registries](../models/index.md) page (or `PUT /admin/models/{id}`), and use that logical id. Check what's registered with `GET /admin/models` or `GET /v1/models`.

### `engine_unavailable` (503)

**Cause.** The model is registered, but the engine binary it needs isn't installed on this machine (for example an `mlx` model on a host without the MLX servers).

**Fix.** Install the engine. On macOS, `make engines` installs the local engine servers; otherwise install `llama-server`, `whisper-server`, and `ffmpeg` from your package manager. `GET /readyz` on the inference plane names the missing slot with an install hint. See [Engines](../models/engines.md).

### `/readyz` shows a slot as not-ready

**Cause.** That `(engine, modality)` combination has no runnable binary — for example `llamacpp:audio.transcription` needs `whisper-server`, and image generation needs `sd-cli` or `mflux-generate`, which `make engines` does **not** install.

**Fix.** Install the named binary, or point the matching [binary-override variable](environment.md#engine-binary-overrides) (e.g. `THEYGENT_WHISPERCPP_BIN`, `THEYGENT_SDCPP_BIN`) at your build, then restart the inference plane. The service stays generally "ready" as long as any one engine works, so a single missing slot doesn't take everything down.

### `no_capacity` (503)

**Cause.** Every resident engine slot is full and busy — the inference plane keeps at most `THEYGENT_MAX_RESIDENT` engines loaded (default 2), and none could be evicted because they're all handling requests.

**Fix.** Wait for an in-flight request to finish, evict a model you're not using (the **Evict** action on Registries, or `POST /admin/models/{id}:evict`), or raise `THEYGENT_MAX_RESIDENT` if you have the RAM.

### `modality_not_supported` (404)

**Cause.** The engine answered the endpoint with a 404 — usually you called a modality the model can't do (embeddings against a chat-only model), or, for a remote `openai-compatible` model, the `baseUrl` is missing its `/v1` suffix.

**Fix.** Use a model registered for the right modality, or fix the base URL. Hosted APIs usually need a trailing `/v1`. See [Remote models](../models/remote.md).

### A model download shows a "too large" fit badge, or loads slowly / fails

**Cause.** The variant's estimated memory need exceeds a safe fraction of your RAM. The fit badge warns before you install.

**Fix.** Pick a smaller quantization variant (a lower-precision quant), or a smaller model. The catalog marks one variant `recommended` — the largest that comfortably fits. See [Installing local models](../models/installing.md).

### `logical_id_exists` (409) when installing from the catalog

**Cause.** You chose a logical id that's already registered.

**Fix.** Pick a different logical id in the install dialog, or delete the existing registration first.

### Text-to-speech returns a 502 "try rephrasing"

**Cause.** The speech engine aborted mid-synthesis or produced empty audio — some input texts trip the engine's text processing.

**Fix.** Rephrase or shorten the input text and try again. This is a clean, non-fatal error, not a crash.

### Speech-to-text won't load the weights

**Cause.** The transcription engine expects `whisper.cpp`'s native `ggml-*.bin` weights; a chat-style GGUF conversion won't work.

**Fix.** Point the model at the exact native weights file. See [Engines](../models/engines.md).

## Runs

### A run is stuck at status `waiting`

**Cause.** The run reached a `human` node and is paused for input. This is expected — waiting runs are excluded from the crash sweep and can wait indefinitely.

**Fix.** Deliver the input: on the run detail page use the **Resume** panel, or `POST /runs/{id}/resume` with `{input: …}`. See [Human-in-the-loop](../running/human-in-the-loop.md).

### `durable_required` (400)

**Cause.** You tried to run an agent that contains a `human`, `subgraph`, `loop`, or `map` node — or you asked for a durable run — but the control plane isn't in durable mode.

**Fix.** Set `THEYGENT_DURABLE=1` in `.env` and `make restart`, then run it with **Run durably** (or `POST /agents/{id}/durable-runs`). See [Durable runs](../running/durable.md).

### A run failed with "interrupted: control-plane restarted while run was in-flight"

**Cause.** The control plane restarted while a non-durable run was still executing. On startup, any run left `created` or `streaming` is swept to `failed` because its progress is unknown.

**Fix.** Re-run it. To make unattended runs survive restarts and resume from the last completed step, run them in [durable mode](../running/durable.md) — durable and waiting runs are excluded from the sweep.

### `template_error`, `router_error`, or `transform_error` (422)

**Cause.** An input-reference problem. `template_error` = a `$in` token that didn't resolve (an unknown port or a missing field). `router_error` = a `router`'s `select` resolved to no handle or an undeclared one — there is no fallback guessing. `transform_error` = a `transform` node's expression is invalid or references something absent.

**Fix.** Check the token against the node's actual ports. Remember the first segment after `$in.` is always a **port name** (`$in.in.field` drills into the default `in` port). See [Referencing inputs](../building/input-references.md).

### The run completed but the output is empty (with an amber note)

**Cause.** The graph reached no `output` node on the taken branch, an upstream node errored, or a reasoning model spent its whole token budget thinking (`finish_reason=length`). The run stays `completed` and the note explains which.

**Fix.** If it's the token budget, raise `maxTokens` on the model binding (or Max tokens in chat). Otherwise check that your branches route to an output node. See [Runs & sessions](../concepts/runs-and-sessions.md).

### "a second output node executed — the run output would be ambiguous"

**Cause.** Two `output` nodes were live in the same run. TheYgent refuses to guess which one is the answer.

**Fix.** Route exclusive branches (with a `router` or `guardrail`) so at most one output node is reachable per run. Having multiple output nodes is fine as long as only one executes.

### A tool node shows red in the waterfall but the run is green

**This is normal, not a bug.** A tool or MCP error is delivered as structured output on the node's `err` port and the run continues (`completed`). The node's bar shows red while the run stays green.

**Fix (if you want it to stop the run).** Wire the `err` port somewhere that handles the failure, rather than leaving it dead. See [Tools](../building/nodes/tools.md).

## Agents and saving

### `version_conflict` (409) when saving

**Cause.** You tried to publish different content under a version string that already exists. Published versions are immutable.

**Fix.** Bump the `version` field in the editor toolbar and save again. (Re-saving *identical* content under the same version is a harmless no-op.)

### `agent_exists` (409)

**Cause.** You tried to create a brand-new agent with an id that's already taken.

**Fix.** Publish it as a new *version* of that agent instead (`POST /agents/{id}/versions`). The editor does this automatically when you save a graph whose id already exists. See [Saving agents](../building/saving-agents.md).

## Triggers, webhooks, and invocation

### Webhook returns `invalid_signature` (401)

**Cause.** The `X-Theygent-Signature` header didn't match. The signature must be the HMAC-SHA256 of the **exact raw request body**, keyed by the trigger's `config.secret`.

**Fix.** Sign the raw bytes you send (any reformatting of the JSON changes the signature). Both `sha256=<hex>` and a bare hex digest are accepted. Confirm you're using the right secret. See [Triggers & webhooks](../running/triggers.md).

### `invoke` returns `unauthorized` (401)

**Cause.** `POST /agents/{id}/invoke` requires the bearer token `THEYGENT_INVOKE_TOKEN`, and it's either unset (the endpoint is closed by default) or your `Authorization` header doesn't match.

**Fix.** Set `THEYGENT_INVOKE_TOKEN` in `.env`, restart, and send `Authorization: Bearer <that token>`.

### `trigger_disabled` (409) or `trigger_not_found` (404) on a webhook

**Cause.** The trigger is disabled, or the id isn't a webhook trigger.

**Fix.** Enable it (`PATCH /triggers/{id}` with `{enabled: true}`) or check the id. A schedule that stops firing after downtime is expected — schedules don't backfill missed windows.

## Secrets and credentials

### Connection secrets disappear after a restart

**Cause.** `THEYGENT_SECRET_KEY` is unset, so the secret store uses an ephemeral in-memory key (you'll have seen a loud warning at startup). Encrypted secrets don't survive a restart with an ephemeral key.

**Fix.** Generate a real Fernet key and set `THEYGENT_SECRET_KEY` in `.env`. To rotate later, supply a comma-separated list — the first key encrypts, all decrypt.

### `credential_error` (502) from a remote model

**Cause.** A model's `credentialRef` (`secret://NAME`) couldn't be resolved — the named credential isn't in the local store and isn't a process environment variable.

**Fix.** Add the credential under **Settings → Local credentials** (or `PUT /admin/credentials/{name}`). Credential values are write-only and resolve on your machine only. See [Remote models](../models/remote.md).

### A tool with a connection binds an `err` at run time

**Cause.** The connection was deleted, disabled, or its secret can't be decrypted (for example after rotating away the key that encrypted it). Connections resolve at step time, not at save time, so this surfaces as a clean `err` — never a crash.

**Fix.** Re-enable or re-create the connection, or set the correct `THEYGENT_SECRET_KEY`.

## Browser and interface

### The interface can't reach a plane / CORS errors in the console

**Cause.** Your browser origin isn't in the allowed CORS list, or a plane is on a non-default port the interface wasn't built to call.

**Fix.** Add your origin to `THEYGENT_CORS_ORIGINS` (comma-separated), and if you moved a plane's port, rebuild the interface with the matching `VITE_CONTROL_PLANE_URL` / `VITE_INFERENCE_URL`. See [Environment variables](environment.md).

### The model catalog is empty — "No local engine is ready"

**Cause.** The catalog only lists models you can actually run, and no local engine is installed yet.

**Fix.** Install at least one engine (`make engines` on macOS, or `llama.cpp` on any platform), then refresh. See [Installing local models](../models/installing.md).

## Related pages

- [API reference](api.md) — where each error code originates.
- [Environment variables](environment.md) — the settings behind most configuration issues.
- [Running TheYgent](../getting-started/running.md) — starting, stopping, and checking the services.
