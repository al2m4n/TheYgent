"""The real cross-process proof (M3 §6) — control-plane -> real inference -> real MLX.

The fast suite fakes the model; this is the thing it can't give: a real prompt going
across the §8 process boundary to a real ``mlx_lm.server`` and streaming back. We launch
the **real inference plane as a subprocess** (not an in-process import — that keeps the
control-plane free of any inference code dependency and makes the boundary genuine),
register a tiny MLX model over its ``/admin`` surface, then drive ``/runs``.

Skipped by default (``-m 'not integration'``) and skips clean when prerequisites are
absent. Run (Apple Silicon)::

    THEYGENT_MLX_MODEL=mlx-community/Qwen2.5-0.5B-Instruct-4bit \
        uv run --package theygent-control-plane pytest -m integration \
        apps/control-plane/tests/test_integration_mlx.py

Prereqs: ``mlx_lm.server`` resolvable (PATH or ``THEYGENT_MLX_BIN``) + the model cached.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import shutil
import socket
import subprocess
import sys
import time
from pathlib import Path

import httpx
import pytest
from _db import fetch_messages, truncate
from alembic import command
from alembic.config import Config
from fastapi.testclient import TestClient
from theygent_control_plane.app import create_app
from ulid import ULID

pytestmark = pytest.mark.integration

_MLX_MODEL = os.environ.get("THEYGENT_MLX_MODEL")
_HAVE_MLX = bool(shutil.which("mlx_lm.server") or os.environ.get("THEYGENT_MLX_BIN"))
# M4: the real cross-process proof now includes real Postgres. On the macOS MLX job (no
# Docker, so no testcontainers) the DB comes from a brew-installed PG via DATABASE_URL.
_DATABASE_URL = os.environ.get("DATABASE_URL")

_skip = pytest.mark.skipif(
    not _MLX_MODEL or not _HAVE_MLX or not _DATABASE_URL,
    reason=("needs THEYGENT_MLX_MODEL, mlx_lm.server on PATH/THEYGENT_MLX_BIN, and DATABASE_URL"),
)


def _prepare_db() -> str:
    """Apply real Alembic migrations to the real DATABASE_URL, then start clean (§0)."""
    ini = Path(__file__).resolve().parents[1] / "alembic.ini"
    assert _DATABASE_URL is not None
    os.environ["DATABASE_URL"] = _DATABASE_URL  # env.py reads this
    command.upgrade(Config(str(ini)), "head")
    asyncio.run(truncate(_DATABASE_URL))
    return _DATABASE_URL


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _mlx_payload() -> dict:
    return {
        "binding": "mlx",
        "source": "hf",
        "model": _MLX_MODEL,
        "params": {"maxTokens": 16},
        "lifecycle": {"keepWarm": False, "idleTimeoutSec": 900, "priority": 1},
    }


def _wait_ready(base: str, deadline_s: float = 60.0) -> None:
    deadline = time.monotonic() + deadline_s
    while time.monotonic() < deadline:
        try:
            if httpx.get(f"{base}/readyz", timeout=2.0).status_code == 200:
                return
        except httpx.HTTPError:
            pass
        time.sleep(0.25)
    raise RuntimeError("inference plane did not become ready")


@_skip
def test_runs_against_real_mlx_subprocess() -> None:
    db_url = _prepare_db()
    port = _free_port()
    base = f"http://127.0.0.1:{port}"
    env = {
        **os.environ,
        "THEYGENT_INFERENCE_PORT": str(port),
        "THEYGENT_INFERENCE_HOST": "127.0.0.1",
    }
    proc = subprocess.Popen(
        [sys.executable, "-m", "theygent_inference"],
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )
    try:
        _wait_ready(base)
        # Register a logical MLX model over the inference management plane.
        reg = httpx.put(f"{base}/admin/models/local", json=_mlx_payload(), timeout=10.0)
        assert reg.status_code == 200, reg.text

        # Control-plane points at the real inference data plane and owns the run.
        app = create_app(inference_base_url=f"{base}/v1", database_url=db_url)
        with TestClient(app) as client:
            with client.stream(
                "POST",
                "/runs",
                json={
                    "input": "Say hello in one word.",
                    "model": "local",
                    "stream": True,
                },
            ) as resp:
                assert resp.status_code == 200
                assert resp.headers["content-type"].startswith("text/event-stream")
                content, run_id, terminal = "", None, None
                for line in "".join(resp.iter_text()).splitlines():
                    if not line.startswith("data:"):
                        continue
                    payload = line[len("data:") :].strip()
                    if payload == "[DONE]":
                        continue
                    obj = json.loads(payload)
                    if "delta" in obj:
                        content += obj["delta"]
                    elif obj.get("status") == "streaming":
                        run_id = obj["runId"]
                    elif obj.get("status") in ("completed", "failed"):
                        terminal = obj

            assert content.strip(), "expected real generated text from MLX"
            assert terminal is not None and terminal["status"] == "completed"
            assert client.get(f"/runs/{run_id}").json()["status"] == "completed"
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()


@contextlib.contextmanager
def _real_inference_plane():
    """Spawn the real inference plane as a subprocess and register the MLX model.

    Same genuine §8 process boundary as the test above (not an in-process import), factored
    out so the two-turn memory proof can reuse it.
    """
    port = _free_port()
    base = f"http://127.0.0.1:{port}"
    env = {
        **os.environ,
        "THEYGENT_INFERENCE_PORT": str(port),
        "THEYGENT_INFERENCE_HOST": "127.0.0.1",
    }
    proc = subprocess.Popen(
        [sys.executable, "-m", "theygent_inference"],
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )
    try:
        _wait_ready(base)
        reg = httpx.put(f"{base}/admin/models/local", json=_mlx_payload(), timeout=10.0)
        assert reg.status_code == 200, reg.text
        yield base
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()


@_skip
def test_two_turn_thread_against_real_mlx() -> None:
    # The thing the fast suite can't give: a two-turn thread end to end across real
    # Postgres AND a real model. Turn 2 replays turn 1's turns (loaded from the DB) over
    # the §8 process boundary into a real mlx_lm.server (M4 §6 integration).
    db_url = _prepare_db()
    thread_id = str(ULID())
    with _real_inference_plane() as base:
        app = create_app(inference_base_url=f"{base}/v1", database_url=db_url)
        with TestClient(app) as client:

            def ask(text: str) -> dict:
                r = client.post(
                    "/runs",
                    json={
                        "input": text,
                        "model": "local",
                        "stream": False,
                        "thread_id": thread_id,
                    },
                )
                assert r.status_code == 200, r.text
                return r.json()

            turn1 = ask("Remember this fact: my favorite fruit is banana. Reply with just: OK")
            assert turn1["status"] == "completed"
            turn2 = ask("What is my favorite fruit? Answer with one word.")
            assert turn2["status"] == "completed"
            assert turn2["output"].strip(), "expected real generated text from MLX"

    # Real DB, end to end: both turns persisted in order with the model's real replies.
    rows = asyncio.run(fetch_messages(db_url, thread_id))
    assert [p for _, _, p in rows] == [0, 1, 2, 3]
    assert [role for role, _, _ in rows] == ["user", "assistant", "user", "assistant"]
    assert rows[0][1].startswith("Remember this fact")  # turn 1 input stored verbatim
    assert rows[2][1].startswith("What is my favorite fruit")  # turn 2 input stored verbatim
    # Turn 2 saw turn 1 (replayed from Postgres across the process boundary): the model
    # recalls the fact. Tiny model + non-blocking job, so this is the soft end of the proof;
    # the persistence/replay wiring above is the hard, deterministic part.
    assert "banana" in turn2["output"].lower()
