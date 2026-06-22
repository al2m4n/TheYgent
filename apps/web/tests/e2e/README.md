# e2e smoke tests

Five Playwright tests (M8 §5) that run against a **real running stack** — not mocks:

1. **agent loop** — paste IR in graph mode, submit, watch it stream to `completed`.
2. **thread loop** — two runs in one thread, both turns shown in order.
3. **live-stream resume** — start a (slow) run, navigate away mid-stream via an in-app link,
   come back, and the parked stream is still attached and tokens are still arriving (the whole
   point of `lib/live.ts`).
4. **error-state visibility** — a run that fails (an unregistered model) surfaces the backend's
   mapped error payload readably, in the composer *and* the run detail (not a bare "404").
5. **registries mutate state** — register → warm → evict → delete a model (real residency via a
   fake engine launcher), and register/delete an MCP server. The only UI-as-control-surface test.

## Run it (one command)

`run_stack.py` brings up the whole stack in-process — real Postgres (testcontainers/Docker),
the real inference plane with a fake EngineLauncher (instant warm, real residency) + a fake
OpenAI upstream, the real control-plane — then runs Playwright against it (Playwright boots Vite
itself), and tears everything down. Exit code is Playwright's, so it's CI-usable.

```bash
# one-time: the browser
pnpm --filter @theygent/web exec playwright install chromium

# from the repo root — runs all five:
uv run --package theygent-control-plane python apps/web/tests/e2e/run_stack.py
# only the two original smokes:
uv run --package theygent-control-plane python apps/web/tests/e2e/run_stack.py -- --grep "agent loop|thread loop"
```

Requires Docker (for Postgres) and a cached Chromium. Skips nothing — if the stack can't come
up, it fails loudly.

## Run against your own stack

Point Playwright at an already-running SPA with `E2E_BASE_URL`, and the SPA at your backends
with `VITE_CONTROL_PLANE_URL` / `VITE_INFERENCE_URL` (a model id must be registered; set
`E2E_MODEL`). The control-plane **and** the inference plane must allow the SPA's origin via CORS
(both default to `http://localhost:5173`; override with `THEYGENT_CORS_ORIGINS`).

## Why these five (and not more)

Each guards a daily-driver behavior: the agent/thread loops (does it work at all), live-stream
resume (cross-view state — where SPAs subtly break), error visibility (the M3–M7 error fidelity
made useful), and registries mutation (UI as control surface). Beyond these you'd be testing CSS
or "looks right" — the brief (§5/§6) says skip that. No component or snapshot tests: M8 has one
user.
