"""The real cross-process proof — control-plane -> real inference -> real MLX.

The fast suite fakes the model; this is the thing it can't give: a real prompt going
across the process boundary to a real ``mlx_lm.server`` and streaming back. We launch
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
# The real cross-process proof now includes real Postgres. On the macOS MLX job (no
# Docker, so no testcontainers) the DB comes from a brew-installed PG via DATABASE_URL.
_DATABASE_URL = os.environ.get("DATABASE_URL")

_skip = pytest.mark.skipif(
    not _MLX_MODEL or not _HAVE_MLX or not _DATABASE_URL,
    reason=("needs THEYGENT_MLX_MODEL, mlx_lm.server on PATH/THEYGENT_MLX_BIN, and DATABASE_URL"),
)


def _prepare_db() -> str:
    """Apply real Alembic migrations to the real DATABASE_URL, then start clean."""
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
        "THEYGENT_INFERENCE_PLANE_PORT": str(port),
        "THEYGENT_INFERENCE_PLANE_HOST": "127.0.0.1",
    }
    proc = subprocess.Popen(
        [sys.executable, "-m", "theygent_inference_plane"],
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

    Same genuine process boundary as the test above (not an in-process import), factored
    out so the two-turn memory proof can reuse it.
    """
    port = _free_port()
    base = f"http://127.0.0.1:{port}"
    env = {
        **os.environ,
        "THEYGENT_INFERENCE_PLANE_PORT": str(port),
        "THEYGENT_INFERENCE_PLANE_HOST": "127.0.0.1",
    }
    proc = subprocess.Popen(
        [sys.executable, "-m", "theygent_inference_plane"],
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
    """The trivial graph (input -> llm -> output), bound to a registered logical id."""
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
def test_two_turn_session_against_real_mlx_via_graph() -> None:
    # Two-turn session, end to end across real Postgres AND a real model — but driven by POST
    # /graphs/runs with the trivial IR instead of /runs. Real model recall through a real graph.
    # The logical id "local" is registered on the inference plane, so the graph's
    # models["default"].model = "local" resolves at the seam unchanged.
    db_url = _prepare_db()
    session_id = str(ULID())
    ir = _trivial_ir("local")
    with _real_inference_plane() as base:
        app = create_app(inference_base_url=f"{base}/v1", database_url=db_url)
        with TestClient(app) as client:

            def ask(text: str) -> dict:
                r = client.post(
                    "/graphs/runs",
                    json={"ir": ir, "input": text, "stream": False, "session_id": session_id},
                )
                assert r.status_code == 200, r.text
                return r.json()

            turn1 = ask("Remember this fact: my favorite fruit is banana. Reply with just: OK")
            assert turn1["status"] == "completed"
            turn2 = ask("What is my favorite fruit? Answer with one word.")
            assert turn2["status"] == "completed"
            assert turn2["output"].strip(), "expected real generated text from MLX"

            # The Run records the graph's identity (content-addressed).
            got = client.get(f"/runs/{turn2['runId']}").json()
            assert got["graph_id"] == "agt_01J9X8MLXDEMO"
            assert got["content_hash"].startswith("sha256:")

    rows = asyncio.run(fetch_messages(db_url, session_id))
    assert [p for _, _, p in rows] == [0, 1, 2, 3]
    assert [role for role, _, _ in rows] == ["user", "assistant", "user", "assistant"]
    assert rows[0][1].startswith("Remember this fact")
    assert rows[2][1].startswith("What is my favorite fruit")
    # Turn 2 saw turn 1 (replayed from Postgres) through the graph path: the model recalls it.
    assert "banana" in turn2["output"].lower()


@_skip
def test_two_turn_session_against_real_mlx() -> None:
    # The thing the fast suite can't give: a two-turn session end to end across real
    # Postgres AND a real model. Turn 2 replays turn 1's turns (loaded from the DB) over
    # the process boundary into a real mlx_lm.server.
    db_url = _prepare_db()
    session_id = str(ULID())
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
                        "session_id": session_id,
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
    rows = asyncio.run(fetch_messages(db_url, session_id))
    assert [p for _, _, p in rows] == [0, 1, 2, 3]
    assert [role for role, _, _ in rows] == ["user", "assistant", "user", "assistant"]
    assert rows[0][1].startswith("Remember this fact")  # turn 1 input stored verbatim
    assert rows[2][1].startswith("What is my favorite fruit")  # turn 2 input stored verbatim
    # Turn 2 saw turn 1 (replayed from Postgres across the process boundary): the model
    # recalls the fact. Tiny model + non-blocking job, so this is the soft end of the proof;
    # the persistence/replay wiring above is the hard, deterministic part.
    assert "banana" in turn2["output"].lower()


def _agent_http_ir() -> dict:
    """Router-tool-llm agent shape: input(decision) -> router -> tool(http_fetch) -> llm -> out.

    The route is driven by the JSON the *input* carries (``$in.in.handle``), not by the tiny model's
    text — so the router selection is deterministic while every other hop is genuinely real: a real
    outbound HTTP GET to a threaded local server (the tool), then real MLX summarizing the fetched
    body (the llm). Real-MLX-as-router is the hand-driven demo shape, where flaky tiny-model JSON is
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
                "config": {"select": "$in.in.handle"},
                "ports": {
                    "in": [{"id": "in", "type": "any"}],
                    "out": [{"id": "yes", "type": "any"}, {"id": "no", "type": "any"}],
                },
            },
            {
                "id": "n_fetch",
                "type": "tool",
                "kind": "activity",
                "config": {"tool": "http_fetch", "args": {"url": "$in.in.payload.url"}},
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
    # A graph that is recognizably an *agent* — router + real outbound HTTP fetch + real MLX —
    # end to end, persisted as a Run. The threaded local HTTP server returns a known body; the
    # model reads the fetched body and answers from it.
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

    A real external MCP server reads a real file, then a real model reasons over the contents.
    The path comes from the input (deterministic); the read-tool name is discovered from the
    server's capability list (it differs across server-filesystem versions)."""
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
    # A real agent reads a real file through the official filesystem MCP server, then a real MLX
    # model answers from its contents. Skips clean if the MCP server can't be fetched/spawned.
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
                # server can't be fetched/spawned (no network in CI).
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


def _fs_qa_agent_ir(read_tool: str) -> dict:
    """Multi-input file-and-question agent: the run input is an object ``{path, question}``; it fans
    out to a real MCP file read (``$in.in.path``) and an ``echo`` carrying the question
    (``$in.in.question``); the ``llm`` node declares TWO in-ports — ``file`` and ``question`` — and
    composes ``$in.file`` AND ``$in.question`` into one prompt. The answer is determined by
    file∩question: the model must read the file (via the ``mcp_tool`` port) to know the facts AND
    parse the question to pick the right one. ``read_tool`` is discovered from the server's caps
    (it varies across server-filesystem)."""
    return {
        "schemaVersion": "1.0",
        "id": "agt_01J9X8MULTIIN",
        "name": "ask-about-file",
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
                "config": {"server": "fs", "tool": read_tool, "args": {"path": "$in.in.path"}},
                "ports": {
                    "in": [{"id": "in", "type": "any"}],
                    "out": [{"id": "ok", "type": "any"}, {"id": "err", "type": "error"}],
                },
            },
            {
                "id": "n_q",
                "type": "tool",
                "kind": "activity",
                "config": {"tool": "echo", "args": {"value": "$in.in.question"}},
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
                            "content": (
                                "Using ONLY the file below, answer the question with a single "
                                "word.\n\nFILE:\n$in.file\n\nQUESTION: $in.question"
                            ),
                        }
                    ],
                },
                "ports": {
                    "in": [{"id": "file", "type": "any"}, {"id": "question", "type": "any"}],
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
                "source": "n_in",
                "sourceHandle": "out",
                "target": "n_q",
                "targetHandle": "in",
                "channel": "data",
            },
            {
                "id": "e3",
                "source": "n_read",
                "sourceHandle": "ok",
                "target": "n_llm",
                "targetHandle": "file",
                "channel": "data",
            },
            {
                "id": "e4",
                "source": "n_q",
                "sourceHandle": "ok",
                "target": "n_llm",
                "targetHandle": "question",
                "channel": "data",
            },
            {
                "id": "e5",
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
def test_multi_input_file_and_question_against_real_mcp_and_mlx() -> None:
    # A graph reads a file on one in-port AND takes a question on another, composes BOTH into one
    # real MLX prompt, and the answer is determined by file∩question — visibly using file content
    # it could only have gotten via the mcp_tool port. The deterministic half (the rendered prompt
    # carries both values, in the right slots) is pinned in the fast suite (test_multi_input.py);
    # this is the real-path behavioral half. Skips clean if the MCP server can't be fetched/spawned.
    db_url = _prepare_db()
    with tempfile.TemporaryDirectory() as tmp:
        root = os.path.realpath(tmp)  # macOS /var -> /private/var: the server compares realpaths
        note = Path(root) / "facts.txt"
        # Three distinct facts; the question selects ONE. Answering "blue" (not "banana"/"fox")
        # requires BOTH the file (to know blue) AND the question (to pick color) — the composition.
        note.write_text("secret color: blue\nsecret fruit: banana\nsecret animal: fox\n")
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
                tools_resp = client.get("/admin/mcp/servers/fs/tools")
                if tools_resp.status_code == 503:
                    pytest.skip("filesystem MCP server unavailable (npx fetch/spawn failed)")
                names = [t["name"] for t in tools_resp.json()["tools"]]
                read_tool = next(
                    (n for n in ("read_text_file", "read_file", "read_media_file") if n in names),
                    None,
                )
                assert read_tool, f"no read tool exposed by server-filesystem: {names}"

                run_input = {"path": str(note), "question": "What is the secret color?"}
                r = client.post(
                    "/graphs/runs",
                    json={"ir": _fs_qa_agent_ir(read_tool), "input": run_input, "stream": False},
                )
                assert r.status_code == 200, r.text
                body = r.json()
                assert body["status"] == "completed", body
                # The real MLX answer is determined by the file AND the question composed into one
                # prompt: it picks "blue" (the color), proving both in-ports reached the model.
                out = body["output"].lower()
                assert out.strip(), "expected real generated text from MLX"
                assert "blue" in out, (
                    f"answer should use the file's color fact via composition: {out!r}"
                )

                got = client.get(f"/runs/{body['runId']}").json()
                assert got["graph_id"] == "agt_01J9X8MULTIIN"


@pytest.mark.skipif(
    not _HAVE_NPX or not _MLX_MODEL or not _HAVE_MLX or not _DATABASE_URL,
    reason="needs npx, THEYGENT_MLX_MODEL, mlx_lm.server, and DATABASE_URL",
)
def test_save_agent_then_invoke_by_id_across_restart() -> None:
    # Save the multi-input file+question agent under a name, invoke it BY ID (never pasting the IR),
    # get a real MLX answer; then "restart" control-plane (a fresh app on the same Postgres) and
    # invoke it AGAIN — it still resolves and runs, proving the registry persists across restarts.
    # Skips clean if the MCP server can't be fetched/spawned.
    db_url = _prepare_db()
    with tempfile.TemporaryDirectory() as tmp:
        root = os.path.realpath(tmp)  # macOS /var -> /private/var: the server compares realpaths
        note = Path(root) / "facts.txt"
        note.write_text("secret color: blue\nsecret fruit: banana\nsecret animal: fox\n")
        with _real_inference_plane() as base:
            # Register the MCP server + discover the read tool through the FIRST app instance.
            app1 = create_app(inference_base_url=f"{base}/v1", database_url=db_url)
            with TestClient(app1) as client:
                reg = client.put(
                    "/admin/mcp/servers/fs",
                    json={
                        "transport": "stdio",
                        "command": "npx",
                        "args": ["-y", "@modelcontextprotocol/server-filesystem", root],
                    },
                )
                assert reg.status_code == 200, reg.text
                tools_resp = client.get("/admin/mcp/servers/fs/tools")
                if tools_resp.status_code == 503:
                    pytest.skip("filesystem MCP server unavailable (npx fetch/spawn failed)")
                names = [t["name"] for t in tools_resp.json()["tools"]]
                read_tool = next(
                    (n for n in ("read_text_file", "read_file", "read_media_file") if n in names),
                    None,
                )
                assert read_tool, f"no read tool exposed by server-filesystem: {names}"

                # SAVE the agent once (the IR is pasted exactly here, and never again).
                ir = _fs_qa_agent_ir(read_tool)
                created = client.post("/agents", json={"ir": ir, "name": "ask-about-file"})
                assert created.status_code == 201, created.text
                agent_id = created.json()["id"]

                # INVOKE BY ID — no IR in the body, just the input.
                run_input = {"path": str(note), "question": "What is the secret color?"}
                r1 = client.post(
                    f"/agents/{agent_id}/runs", json={"input": run_input, "stream": False}
                )
                assert r1.status_code == 200, r1.text
                assert r1.json()["status"] == "completed"
                assert "blue" in r1.json()["output"].lower()
                assert client.get(f"/runs/{r1.json()['runId']}").json()["graph_id"] == agent_id

            # "RESTART": a fresh app on the same Postgres. The saved agent (and the persisted MCP
            # registration) rehydrate; invoking by id still works — never touching the IR JSON.
            app2 = create_app(inference_base_url=f"{base}/v1", database_url=db_url)
            with TestClient(app2) as client:
                assert client.get(f"/agents/{agent_id}").status_code == 200
                run_input = {"path": str(note), "question": "What is the secret animal?"}
                r2 = client.post(
                    f"/agents/{agent_id}/runs", json={"input": run_input, "stream": False}
                )
                assert r2.status_code == 200, r2.text
                assert r2.json()["status"] == "completed"
                # A different question over the SAME saved agent → the animal fact this time.
                assert "fox" in r2.json()["output"].lower()


@_skip
def test_invoke_token_authed_against_real_mlx() -> None:
    # With NO cockpit, a token-authed POST /agents/{id}/invoke runs the saved agent on real MLX and
    # returns a real result — and an UNAUTHENTICATED invoke is refused (401). This is the unattended
    # invoke surface: a deployed agent a program can call directly.
    db_url = _prepare_db()
    ir = _trivial_ir("local")
    with _real_inference_plane() as base:
        app = create_app(
            inference_base_url=f"{base}/v1",
            database_url=db_url,
            invoke_token="deploy-tok",
            start_dispatcher=False,
        )
        with TestClient(app) as client:
            created = client.post("/agents", json={"ir": ir, "name": "deployable"})
            assert created.status_code == 201, created.text
            agent_id = created.json()["id"]
            body = {"input": "Say hello in one word.", "stream": False}

            # Anonymous (no token) → 401: the unattended surface is gated.
            assert client.post(f"/agents/{agent_id}/invoke", json=body).status_code == 401

            # Token-authed → a real MLX result, cockpit-free.
            r = client.post(
                f"/agents/{agent_id}/invoke",
                json=body,
                headers={"Authorization": "Bearer deploy-tok"},
            )
            assert r.status_code == 200, r.text
            assert r.json()["status"] == "completed"
            assert r.json()["output"].strip(), "expected real generated text from MLX"
            got = client.get(f"/runs/{r.json()['runId']}").json()
            assert got["graph_id"] == agent_id
            assert (
                got["trigger_id"] is None
            )  # a direct invoke is not trigger-fired (no trigger lineage)


@_skip
def test_schedule_fires_against_real_mlx_on_its_own() -> None:
    # Register a near-future cron schedule pinned to the saved agent, then WATCH a Run appear and
    # complete on its own — the real in-process dispatcher loop firing real MLX, no human in the
    # cockpit. Drives the actual background loop (start_dispatcher default on), so this genuinely
    # demonstrates unattended firing — polls up to ~90s because a one-minute cron resolves at the
    # next minute boundary.
    db_url = _prepare_db()
    ir = _trivial_ir("local")
    with _real_inference_plane() as base:
        app = create_app(
            inference_base_url=f"{base}/v1", database_url=db_url, dispatcher_interval_s=2.0
        )
        with TestClient(app) as client:
            agent_id = client.post("/agents", json={"ir": ir}).json()["id"]
            # Warm the engine BEFORE registering the schedule: the first data-plane call pays the
            # full engine spawn + model load, and the poll window below must absorb up to ~60s of
            # cron-boundary wait already — a cold start on top of that blows the deadline on a
            # loaded runner. What this test proves is unattended firing, not cold-start latency.
            warm = client.post(
                f"/agents/{agent_id}/runs",
                json={"input": "Say hello in one word.", "stream": False},
            )
            assert warm.status_code == 200, warm.text
            assert warm.json()["status"] == "completed"
            trig = client.post(
                "/triggers",
                json={
                    "agent_id": agent_id,
                    "kind": "schedule",
                    "version": "0.1.0",
                    "config": {"cron": "* * * * *", "input": "Say hello in one word."},
                },
            )
            assert trig.status_code == 201, trig.text
            tid = trig.json()["id"]

            deadline = time.monotonic() + 90.0
            fired = None
            while time.monotonic() < deadline:
                runs = client.get("/runs").json()["runs"]
                mine = [r for r in runs if r.get("trigger_id") == tid]
                if mine and mine[0]["status"] in ("completed", "failed"):
                    fired = mine[0]
                    break
                time.sleep(2.0)
            assert fired is not None, "the schedule never fired on its own within the deadline"
            assert fired["status"] == "completed"
            assert fired["graph_version"] == "0.1.0"  # the pinned version ran
