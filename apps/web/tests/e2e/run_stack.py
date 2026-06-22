"""Bring up a real M8 stack and run the Playwright e2e suite against it.

This is the "live stack" the e2e smokes need (M8 §5) — NOT mocks:

  * **Postgres** — a real ephemeral container (testcontainers), schema via real Alembic.
  * **Inference plane** — the REAL `theygent-inference` app, with one logical model
    (`triage-fast`) registered as a reachable `openai-compatible` binding pointing at...
  * **a fake OpenAI upstream** (the control-plane fast suite's `FakeInference`) that streams
    a deterministic "hello world" — so the data plane is real HTTP end to end without
    needing a multi-GB engine or the network.
  * **Control-plane** — the REAL `theygent-control-plane` app over the real Postgres + the
    real inference plane.
  * **The SPA** — Playwright boots `pnpm dev` itself (playwright.config.ts), pointed at the
    two backends via VITE_* env.

Run from the repo root:

    uv run --with testcontainers --with playwright python apps/web/tests/e2e/run_stack.py
    # pass extra Playwright args after `--`, e.g. only the two smokes:
    uv run python apps/web/tests/e2e/run_stack.py -- --grep "agent loop|thread loop"

Exit code is Playwright's, so this is CI-usable. Everything is torn down on exit.
"""

from __future__ import annotations

import os
import subprocess
import sys
import threading
import time
from pathlib import Path

import httpx
import uvicorn

REPO = Path(__file__).resolve().parents[4]
WEB = REPO / "apps" / "web"
CP_TESTS = REPO / "apps" / "control-plane" / "tests"
INF_TESTS = REPO / "apps" / "inference" / "tests"

# Reuse the fast suites' real fakes: the control-plane's threaded OpenAI upstream and the
# inference plane's injectable fake EngineLauncher (real HTTP, fake weights → instant warm,
# so the registries test can prove warm→resident→evict without a multi-GB download).
sys.path.insert(0, str(CP_TESTS))
sys.path.insert(0, str(INF_TESTS))

INFERENCE_PORT = 8081
CONTROL_PORT = 8080
SLOW_PORT = 8082
MODEL = "triage-fast"
SLOW_MODEL = "triage-slow"


def _slow_upstream_app():
    """A real OpenAI-compatible upstream that streams many chunks slowly — so the live-stream
    resume test can navigate mid-stream and still observe tokens arriving."""
    import asyncio
    import json

    from fastapi import FastAPI
    from fastapi.responses import JSONResponse, StreamingResponse

    app = FastAPI()
    words = ["the", "quick", "brown", "fox", "jumps", "over", "the", "lazy", "dog"]

    @app.get("/readyz")
    async def readyz():
        return {"status": "ready"}

    @app.post("/v1/chat/completions")
    async def chat(req: dict):
        async def gen():
            for i, w in enumerate(words):
                delta = {"content": f"{w} "}
                if i == 0:
                    delta["role"] = "assistant"
                yield (
                    "data: "
                    + json.dumps(
                        {
                            "id": "slow",
                            "object": "chat.completion.chunk",
                            "created": 0,
                            "model": req.get("model", SLOW_MODEL),
                            "choices": [{"index": 0, "delta": delta, "finish_reason": None}],
                        }
                    )
                    + "\n\n"
                )
                await asyncio.sleep(0.4)
            yield (
                "data: "
                + json.dumps(
                    {
                        "id": "slow",
                        "object": "chat.completion.chunk",
                        "created": 0,
                        "model": req.get("model", SLOW_MODEL),
                        "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
                    }
                )
                + "\n\n"
            )
            yield "data: [DONE]\n\n"

        if req.get("stream"):
            return StreamingResponse(gen(), media_type="text/event-stream")
        return JSONResponse(
            {
                "id": "slow",
                "object": "chat.completion",
                "created": 0,
                "model": req.get("model", SLOW_MODEL),
                "choices": [
                    {
                        "index": 0,
                        "message": {"role": "assistant", "content": " ".join(words)},
                        "finish_reason": "stop",
                    }
                ],
                "usage": {"prompt_tokens": 1, "completion_tokens": 9, "total_tokens": 10},
            }
        )

    return app


class _Thread:
    """Run a FastAPI app under uvicorn in a daemon thread; wait for / signal shutdown."""

    def __init__(self, app, port: int) -> None:
        config = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning")
        self._server = uvicorn.Server(config)
        self._thread = threading.Thread(target=self._server.run, daemon=True)

    def start(self) -> None:
        self._thread.start()
        deadline = time.time() + 30
        while not self._server.started:
            if time.time() > deadline:
                raise TimeoutError("server did not start in time")
            time.sleep(0.05)

    def stop(self) -> None:
        self._server.should_exit = True
        self._thread.join(timeout=5)


def _wait_http(url: str, timeout: float = 30.0) -> None:
    deadline = time.time() + timeout
    last: Exception | None = None
    while time.time() < deadline:
        try:
            httpx.get(url, timeout=2.0)
            return
        except Exception as exc:  # polling for readiness
            last = exc
            time.sleep(0.2)
    raise TimeoutError(f"{url} never became reachable: {last}")


def main() -> int:
    extra_args = sys.argv[1:]
    if extra_args and extra_args[0] == "--":
        extra_args = extra_args[1:]

    from _fake_inference import FakeInference  # the real threaded fake upstream
    from alembic import command
    from alembic.config import Config

    try:
        from testcontainers.postgres import PostgresContainer
    except ImportError:
        print("testcontainers not installed; `uv run --with testcontainers ...`", file=sys.stderr)
        return 2

    from _fake_upstream import FakeUpstreamLauncher  # injectable fake EngineLauncher

    print("→ starting Postgres (testcontainers)…")
    pg = PostgresContainer("postgres:16", driver="asyncpg")
    pg.start()
    fake: FakeInference | None = None
    slow: _Thread | None = None
    inference: _Thread | None = None
    control: _Thread | None = None
    try:
        dsn = pg.get_connection_url()
        os.environ["DATABASE_URL"] = dsn
        ini = REPO / "apps" / "control-plane" / "alembic.ini"
        command.upgrade(Config(str(ini)), "head")
        print("  schema applied via Alembic")

        print("→ starting fake OpenAI upstreams (instant + slow)…")
        fake = FakeInference()
        fake.__enter__()
        slow = _Thread(_slow_upstream_app(), SLOW_PORT)
        slow.start()
        _wait_http(f"http://127.0.0.1:{SLOW_PORT}/readyz")
        slow_url = f"http://127.0.0.1:{SLOW_PORT}/v1"

        print(f"→ starting inference plane on :{INFERENCE_PORT}…")
        from theygent_inference.app import create_app as create_inference

        # Inject the fake EngineLauncher so a MANAGED model warms instantly (real residency,
        # fake weights) — the registries test proves warm→resident→evict this way. CORS for
        # the Vite dev origin is explicit so the harness is self-documenting.
        inference = _Thread(
            create_inference(
                launcher=FakeUpstreamLauncher(), cors_origins=["http://localhost:5173"]
            ),
            INFERENCE_PORT,
        )
        inference.start()
        _wait_http(f"http://127.0.0.1:{INFERENCE_PORT}/healthz")

        # Register two reachable logical models: an instant one (agent/thread/error tests) and
        # a slow one (the live-stream resume test). Managed models are registered later, via the
        # UI, by the registries test itself.
        for logical_id, base in ((MODEL, fake.v1_url), (SLOW_MODEL, slow_url)):
            resp = httpx.put(
                f"http://127.0.0.1:{INFERENCE_PORT}/admin/models/{logical_id}",
                json={"binding": "openai-compatible", "baseUrl": base, "model": "fake"},
                timeout=5.0,
            )
            resp.raise_for_status()
            print(f"  registered {logical_id!r} → {base}")

        print(f"→ starting control-plane on :{CONTROL_PORT}…")
        from theygent_control_plane.app import create_app as create_control

        control = _Thread(
            create_control(
                inference_base_url=f"http://127.0.0.1:{INFERENCE_PORT}/v1",
                database_url=dsn,
                cors_origins=["http://localhost:5173"],
            ),
            CONTROL_PORT,
        )
        control.start()
        _wait_http(f"http://127.0.0.1:{CONTROL_PORT}/healthz")

        env = {
            **os.environ,
            "VITE_CONTROL_PLANE_URL": f"http://localhost:{CONTROL_PORT}",
            "VITE_INFERENCE_URL": f"http://localhost:{INFERENCE_PORT}",
            "E2E_MODEL": MODEL,
            "E2E_SLOW_MODEL": SLOW_MODEL,
        }
        cmd = ["pnpm", "exec", "playwright", "test", *extra_args]
        print(f"→ running: {' '.join(cmd)}")
        result = subprocess.run(cmd, cwd=str(WEB), env=env, check=False)
        return result.returncode
    finally:
        print("→ tearing down stack…")
        if control is not None:
            control.stop()
        if inference is not None:
            inference.stop()
        if slow is not None:
            slow.stop()
        if fake is not None:
            fake.__exit__(None, None, None)
        pg.stop()


if __name__ == "__main__":
    raise SystemExit(main())
