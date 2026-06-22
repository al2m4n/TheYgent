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
import tempfile
import time
from pathlib import Path

import httpx
import pytest
from _db import fetch_messages, truncate
from _http import ThreadedHTTP
from alembic import command
from alembic.config import Config
from fastapi.testclient import TestClient
from theygent_control_plane.app import create_app
from ulid import ULID

pytestmark = pytest.mark.integration

_MLX_MODEL = os.environ.get("THEYGENT_MLX_MODEL")
_HAVE_MLX = bool(shutil.which("mlx_lm.server") or os.environ.get("THEYGENT_MLX_BIN"))
_HAVE_NPX = bool(shutil.which("npx"))
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


def _trivial_ir(model_id: str) -> dict:
    """The m5.md §4 trivial graph (input -> llm -> output), bound to a registered logical id."""
    return {
        "schemaVersion": "1.0",
        "id": "agt_01J9X8MLXDEMO",
        "name": "trivial-llm",
        "version": "0.1.0",
        "models": {"default": {"binding": "mlx", "model": model_id, "params": {"maxTokens": 16}}},
        "tools": {},
        "nodes": [
            {
                "id": "n_in",
                "type": "input",
                "kind": "boundary",
                "ports": {"in": [], "out": [{"id": "out", "type": "any"}]},
            },
            {
                "id": "n_llm",
                "type": "llm",
                "kind": "activity",
                "config": {"model": "default", "messages": [{"role": "user", "content": "$in"}]},
                "ports": {
                    "in": [{"id": "in", "type": "any"}],
                    "out": [{"id": "ok", "type": "any"}, {"id": "err", "type": "error"}],
                },
            },
            {
                "id": "n_out",
                "type": "output",
                "kind": "boundary",
                "ports": {"in": [{"id": "in", "type": "any"}], "out": []},
            },
        ],
        "edges": [
            {
                "id": "e1",
                "source": "n_in",
                "sourceHandle": "out",
                "target": "n_llm",
                "targetHandle": "in",
                "channel": "data",
            },
            {
                "id": "e2",
                "source": "n_llm",
                "sourceHandle": "ok",
                "target": "n_out",
                "targetHandle": "in",
                "channel": "data",
            },
        ],
    }


@_skip
def test_two_turn_thread_against_real_mlx_via_graph() -> None:
    # The demoable M5 proof (m5.md §6): the two-turn thread, end to end across real Postgres AND
    # a real model — but driven by POST /graphs/runs with the trivial IR instead of /runs. Real
    # model recall through a real graph. The logical id "local" is registered on the inference
    # plane, so the graph's models["default"].model = "local" resolves at the seam unchanged.
    db_url = _prepare_db()
    thread_id = str(ULID())
    ir = _trivial_ir("local")
    with _real_inference_plane() as base:
        app = create_app(inference_base_url=f"{base}/v1", database_url=db_url)
        with TestClient(app) as client:

            def ask(text: str) -> dict:
                r = client.post(
                    "/graphs/runs",
                    json={"ir": ir, "input": text, "stream": False, "thread_id": thread_id},
                )
                assert r.status_code == 200, r.text
                return r.json()

            turn1 = ask("Remember this fact: my favorite fruit is banana. Reply with just: OK")
            assert turn1["status"] == "completed"
            turn2 = ask("What is my favorite fruit? Answer with one word.")
            assert turn2["status"] == "completed"
            assert turn2["output"].strip(), "expected real generated text from MLX"

            # The Run records the graph's identity (content-addressed — §4).
            got = client.get(f"/runs/{turn2['runId']}").json()
            assert got["graph_id"] == "agt_01J9X8MLXDEMO"
            assert got["content_hash"].startswith("sha256:")

    rows = asyncio.run(fetch_messages(db_url, thread_id))
    assert [p for _, _, p in rows] == [0, 1, 2, 3]
    assert [role for role, _, _ in rows] == ["user", "assistant", "user", "assistant"]
    assert rows[0][1].startswith("Remember this fact")
    assert rows[2][1].startswith("What is my favorite fruit")
    # Turn 2 saw turn 1 (replayed from Postgres) through the graph path: the model recalls it.
    assert "banana" in turn2["output"].lower()


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


def _agent_http_ir() -> dict:
    """The M6 agent shape (m6.md §6): input(decision) -> router -> tool(http_fetch) -> llm -> out.

    The route is driven by the JSON the *input* carries (``$in.handle``), not by the tiny model's
    text — so the router selection is deterministic while every other hop is genuinely real: a real
    outbound HTTP GET to a threaded local server (the tool), then real MLX summarizing the fetched
    body (the llm). Real-MLX-as-router is the §8 hand-drive demo, where flaky tiny-model JSON is
    acceptable; an automated test must not hinge on it."""
    return {
        "schemaVersion": "1.0",
        "id": "agt_01J9X8MLXAGENT",
        "name": "agent-router-tool",
        "version": "0.1.0",
        "models": {"default": {"binding": "mlx", "model": "local", "params": {"maxTokens": 24}}},
        "tools": {},
        "nodes": [
            {
                "id": "n_in",
                "type": "input",
                "kind": "boundary",
                "ports": {"in": [], "out": [{"id": "out", "type": "any"}]},
            },
            {
                "id": "n_route",
                "type": "router",
                "kind": "orchestration",
                "config": {"select": "$in.handle"},
                "ports": {
                    "in": [{"id": "in", "type": "any"}],
                    "out": [{"id": "yes", "type": "any"}, {"id": "no", "type": "any"}],
                },
            },
            {
                "id": "n_fetch",
                "type": "tool",
                "kind": "activity",
                "config": {"tool": "http_fetch", "args": {"url": "$in.payload.url"}},
                "ports": {
                    "in": [{"id": "in", "type": "any"}],
                    "out": [{"id": "ok", "type": "any"}, {"id": "err", "type": "error"}],
                },
            },
            {
                "id": "n_llm",
                "type": "llm",
                "kind": "activity",
                "config": {
                    "model": "default",
                    "messages": [
                        {
                            "role": "user",
                            "content": "Reply with one word naming the capital city in: $in",
                        }
                    ],
                },
                "ports": {
                    "in": [{"id": "in", "type": "any"}],
                    "out": [{"id": "ok", "type": "any"}, {"id": "err", "type": "error"}],
                },
            },
            {
                "id": "n_out",
                "type": "output",
                "kind": "boundary",
                "ports": {"in": [{"id": "in", "type": "any"}], "out": []},
            },
            {
                "id": "n_no",
                "type": "output",
                "kind": "boundary",
                "ports": {"in": [{"id": "in", "type": "any"}], "out": []},
            },
        ],
        "edges": [
            {
                "id": "e1",
                "source": "n_in",
                "sourceHandle": "out",
                "target": "n_route",
                "targetHandle": "in",
                "channel": "data",
            },
            {
                "id": "e2",
                "source": "n_route",
                "sourceHandle": "yes",
                "target": "n_fetch",
                "targetHandle": "in",
                "channel": "data",
            },
            {
                "id": "e3",
                "source": "n_fetch",
                "sourceHandle": "ok",
                "target": "n_llm",
                "targetHandle": "in",
                "channel": "data",
            },
            {
                "id": "e4",
                "source": "n_llm",
                "sourceHandle": "ok",
                "target": "n_out",
                "targetHandle": "in",
                "channel": "data",
            },
            {
                "id": "e5",
                "source": "n_route",
                "sourceHandle": "no",
                "target": "n_no",
                "targetHandle": "in",
                "channel": "data",
            },
        ],
    }


@_skip
def test_agent_router_tool_against_real_mlx() -> None:
    # The demoable M6 proof (m6.md §6/§8): a graph that is recognizably an *agent* — router +
    # real outbound HTTP fetch + real MLX — end to end, persisted as a Run. The threaded local
    # HTTP server returns a known body; the model reads the fetched body and answers from it.
    db_url = _prepare_db()
    with (
        _real_inference_plane() as base,
        ThreadedHTTP(body="The capital of France is Paris.") as url,
    ):
        app = create_app(inference_base_url=f"{base}/v1", database_url=db_url)
        with TestClient(app) as client:
            decision = json.dumps({"handle": "yes", "payload": {"url": url}})
            r = client.post(
                "/graphs/runs", json={"ir": _agent_http_ir(), "input": decision, "stream": False}
            )
            assert r.status_code == 200, r.text
            body = r.json()
            assert body["status"] == "completed"
            # Real MLX read the real fetched body and answered from it (soft end — tiny model).
            assert body["output"].strip(), "expected real generated text from MLX"
            assert "paris" in body["output"].lower()

            got = client.get(f"/runs/{body['runId']}").json()
            assert got["graph_id"] == "agt_01J9X8MLXAGENT"
            assert got["content_hash"].startswith("sha256:")


def _fs_agent_ir(read_tool: str) -> dict:
    """input(absolute path) → mcp_tool(fs, <read_tool>, {path: $in}) → llm(read it) → output.

    The M7 demo shape (m7.md §6/§8): a real external MCP server reads a real file, then a real model
    reasons over the contents. The path comes from the input (deterministic); the read-tool name is
    discovered from the server's capability list (it differs across server-filesystem versions)."""
    return {
        "schemaVersion": "1.0",
        "id": "agt_01J9X8MCPDEMO",
        "name": "fs-read-agent",
        "version": "0.1.0",
        "models": {"default": {"binding": "mlx", "model": "local", "params": {"maxTokens": 24}}},
        "tools": {},
        "nodes": [
            {
                "id": "n_in",
                "type": "input",
                "kind": "boundary",
                "ports": {"in": [], "out": [{"id": "out", "type": "any"}]},
            },
            {
                "id": "n_read",
                "type": "mcp_tool",
                "kind": "activity",
                "config": {"server": "fs", "tool": read_tool, "args": {"path": "$in"}},
                "ports": {
                    "in": [{"id": "in", "type": "any"}],
                    "out": [{"id": "ok", "type": "any"}, {"id": "err", "type": "error"}],
                },
            },
            {
                "id": "n_llm",
                "type": "llm",
                "kind": "activity",
                "config": {
                    "model": "default",
                    "messages": [
                        {"role": "user", "content": "Name the fruit mentioned, one word: $in"}
                    ],
                },
                "ports": {
                    "in": [{"id": "in", "type": "any"}],
                    "out": [{"id": "ok", "type": "any"}, {"id": "err", "type": "error"}],
                },
            },
            {
                "id": "n_out",
                "type": "output",
                "kind": "boundary",
                "ports": {"in": [{"id": "in", "type": "any"}], "out": []},
            },
        ],
        "edges": [
            {
                "id": "e1",
                "source": "n_in",
                "sourceHandle": "out",
                "target": "n_read",
                "targetHandle": "in",
                "channel": "data",
            },
            {
                "id": "e2",
                "source": "n_read",
                "sourceHandle": "ok",
                "target": "n_llm",
                "targetHandle": "in",
                "channel": "data",
            },
            {
                "id": "e3",
                "source": "n_llm",
                "sourceHandle": "ok",
                "target": "n_out",
                "targetHandle": "in",
                "channel": "data",
            },
        ],
    }


@pytest.mark.skipif(
    not _HAVE_NPX or not _MLX_MODEL or not _HAVE_MLX or not _DATABASE_URL,
    reason="needs npx, THEYGENT_MLX_MODEL, mlx_lm.server, and DATABASE_URL",
)
def test_agent_reads_file_via_real_mcp_and_mlx() -> None:
    # The demoable M7 proof (m7.md §6/§8): a real agent reads a real file through the official
    # filesystem MCP server, then a real MLX model answers from its contents — the first time the
    # platform is *useful*. Skips clean if the MCP server can't be fetched/spawned.
    db_url = _prepare_db()
    with tempfile.TemporaryDirectory() as tmp:
        # Resolve symlinks (macOS /var -> /private/var): the server's allowed-dir check compares
        # realpaths, so both the registration dir and the read path must be resolved or it denies.
        root = os.path.realpath(tmp)
        note = Path(root) / "note.txt"
        note.write_text("Project status: the secret fruit is banana. All systems nominal.")
        with _real_inference_plane() as base:
            app = create_app(inference_base_url=f"{base}/v1", database_url=db_url)
            with TestClient(app) as client:
                reg = client.put(
                    "/admin/mcp/servers/fs",
                    json={
                        "transport": "stdio",
                        "command": "npx",
                        "args": ["-y", "@modelcontextprotocol/server-filesystem", root],
                    },
                )
                assert reg.status_code == 200, reg.text
                # Capability probe also drives the (slow, network) connect; skip clean if the
                # server can't be fetched/spawned (no network in CI — m7.md §8).
                tools_resp = client.get("/admin/mcp/servers/fs/tools")
                if tools_resp.status_code == 503:
                    pytest.skip("filesystem MCP server unavailable (npx fetch/spawn failed)")
                names = [t["name"] for t in tools_resp.json()["tools"]]
                read_tool = next(
                    (n for n in ("read_text_file", "read_file", "read_media_file") if n in names),
                    None,
                )
                assert read_tool, f"no read tool exposed by server-filesystem: {names}"

                r = client.post(
                    "/graphs/runs",
                    json={"ir": _fs_agent_ir(read_tool), "input": str(note), "stream": False},
                )
                assert r.status_code == 200, r.text
                body = r.json()
                assert body["status"] == "completed", body
                # Real MLX read the real file (via real MCP) and answered from it — soft end (tiny
                # model); the read + persist wiring is the hard, deterministic part.
                assert body["output"].strip(), "expected real generated text from MLX"
                assert "banana" in body["output"].lower()

                got = client.get(f"/runs/{body['runId']}").json()
                assert got["graph_id"] == "agt_01J9X8MCPDEMO"
