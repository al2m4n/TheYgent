"""Helpers for the M13 durable fast suite: a streaming, *blockable* fake inference (so a run can be
frozen mid-activity to simulate a crash) and small builders for saving agents + resetting the DBOS
schema between durable tests.

DBOS is a process-global singleton (decisions D2), so each durable test launches and destroys it,
and must start from a clean ``dbos`` schema — else a workflow a prior test left pending would be
*recovered* by the next test's launch. ``reset_dbos_schema`` drops + re-migrates the ``dbos`` schema
(never ``public`` — that is Alembic's, truncated by the existing ``clean_db`` fixture).
"""

from __future__ import annotations

import asyncio
import json
import threading
import time
from typing import Any

import asyncpg
import uvicorn
from _db import plain_dsn
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, StreamingResponse
from theygent_control_plane.tools import DEFAULT_REGISTRY
from theygent_ir import content_hash, parse_document


class BlockingInference:
    """A real OpenAI-compatible SSE server whose responses are keyed by the user prompt, and which
    can BLOCK a chosen prompt's first call until released — the crash-victim activity. Counts calls
    per prompt so a test can assert a completed step was not re-executed (no duplicated effect).

    ``responses`` maps a prompt string → the content to stream back. ``block_prompt`` (if set) makes
    the FIRST call with that prompt block forever (until ``release`` at teardown); subsequent calls
    with the same prompt stream normally (the recovered run). This models: llm1 completes + is
    journaled; llm2's first execution is interrupted by the crash; on resume llm2 runs afresh.
    """

    def __init__(self, responses: dict[str, str], *, block_prompt: str | None = None) -> None:
        self.responses = responses
        self.block_prompt = block_prompt
        self.calls: dict[str, int] = {}
        self._release = threading.Event()
        self.blocked_entered = threading.Event()
        app = FastAPI()

        @app.get("/v1/models")
        async def models() -> dict[str, Any]:
            return {"object": "list", "data": [{"id": "triage-fast", "object": "model"}]}

        @app.post("/v1/chat/completions")
        async def chat(request: Request):
            body = await request.json()
            prompt = body["messages"][-1]["content"]
            self.calls[prompt] = self.calls.get(prompt, 0) + 1
            first = self.calls[prompt] == 1
            if self.block_prompt is not None and prompt == self.block_prompt and first:
                # The crash victim: freeze here so the test can destroy DBOS mid-activity. Never
                # released during the test (only at teardown), so this abandoned request can never
                # write a zombie outcome — only the RECOVERED (2nd) call proceeds.
                self.blocked_entered.set()
                while not self._release.is_set():  # noqa: ASYNC110 — cross-thread test gate
                    await asyncio.sleep(0.02)
                return JSONResponse({"error": "released"}, status_code=503)
            content = self.responses.get(prompt, f"<no-response-for:{prompt}>")
            if body.get("stream"):
                return StreamingResponse(_sse(content), media_type="text/event-stream")
            return JSONResponse(
                {
                    "id": "x",
                    "object": "chat.completion",
                    "created": 0,
                    "model": body.get("model"),
                    "choices": [
                        {
                            "index": 0,
                            "message": {"role": "assistant", "content": content},
                            "finish_reason": "stop",
                        }
                    ],
                }
            )

        self._server = uvicorn.Server(
            uvicorn.Config(app, host="127.0.0.1", port=0, log_level="warning")
        )
        self._thread = threading.Thread(target=self._server.run, daemon=True)

    def __enter__(self) -> BlockingInference:
        self._thread.start()
        deadline = time.monotonic() + 10
        while not self._server.started:
            if time.monotonic() > deadline:
                raise RuntimeError("blocking inference did not start")
            time.sleep(0.01)
        self.port = self._server.servers[0].sockets[0].getsockname()[1]
        return self

    def __exit__(self, *exc: object) -> None:
        self._release.set()  # let any parked victim request unwind so the server can stop
        self._server.should_exit = True
        self._thread.join(timeout=5)

    @property
    def v1_url(self) -> str:
        return f"http://127.0.0.1:{self.port}/v1"


class TransientFailInference:
    """A real SSE server that returns 503 ``engine_unavailable`` for the first ``fail_count`` calls,
    then streams ``content``. Models a transient inference failure (an engine warming up) so a test
    can prove DBOS step-level retry covers what the M12 gateway retry used to (M13 §2 / the 503
    regression the durable gateway's ``max_retries=0`` would otherwise re-introduce)."""

    def __init__(self, *, fail_count: int = 1, content: str = "RECOVERED") -> None:
        self.fail_count = fail_count
        self.content = content
        self.calls = 0
        app = FastAPI()

        @app.get("/v1/models")
        async def models() -> dict[str, Any]:
            return {"object": "list", "data": [{"id": "triage-fast", "object": "model"}]}

        @app.post("/v1/chat/completions")
        async def chat(request: Request):
            self.calls += 1
            if self.calls <= self.fail_count:
                return JSONResponse(
                    {"error": {"message": "engine warming up", "code": "engine_unavailable"}},
                    status_code=503,
                )
            return StreamingResponse(_sse(self.content), media_type="text/event-stream")

        self._server = uvicorn.Server(
            uvicorn.Config(app, host="127.0.0.1", port=0, log_level="warning")
        )
        self._thread = threading.Thread(target=self._server.run, daemon=True)

    def __enter__(self) -> TransientFailInference:
        self._thread.start()
        while not self._server.started:
            time.sleep(0.01)
        self.port = self._server.servers[0].sockets[0].getsockname()[1]
        return self

    def __exit__(self, *exc: object) -> None:
        self._server.should_exit = True
        self._thread.join(timeout=5)

    @property
    def v1_url(self) -> str:
        return f"http://127.0.0.1:{self.port}/v1"


# A non-idempotent, OBSERVABLE external side effect registered as a built-in tool, used to prove the
# honest durability guarantee (decision D9): exactly-once for COMPLETED (journaled) steps,
# at-least-once for an INTERRUPTED one. ``_SIDE_EFFECT["count"]`` is the effect; the FIRST call (the
# crash victim) blocks forever so the test can destroy DBOS while the tool step is in-flight; the
# RECOVERED call (count==2) returns — i.e. the effect happened TWICE, the at-least-once property.
_SIDE_EFFECT: dict[str, Any] = {"count": 0, "entered": threading.Event()}


def reset_side_effect() -> None:
    _SIDE_EFFECT["count"] = 0
    _SIDE_EFFECT["entered"] = threading.Event()


def side_effect_count() -> int:
    return _SIDE_EFFECT["count"]


def side_effect_entered() -> bool:
    return _SIDE_EFFECT["entered"].is_set()


async def _durable_sideeffect_tool(*, value: Any = None) -> dict[str, Any]:
    _SIDE_EFFECT["count"] += 1
    n = _SIDE_EFFECT["count"]
    _SIDE_EFFECT["entered"].set()
    if n == 1:  # the crash victim: block forever (the test destroys DBOS mid-step; never returns)
        while True:  # noqa: ASYNC110 — deliberately un-returning; the process is "crashed"
            await asyncio.sleep(0.05)
    return {"count": n, "value": value}


if "_durable_sideeffect" not in DEFAULT_REGISTRY:  # register once (module import is cached)
    DEFAULT_REGISTRY.register("_durable_sideeffect")(_durable_sideeffect_tool)


def _sse(content: str):
    def gen():
        yield _chunk({"role": "assistant", "content": content})
        yield _chunk({}, finish="stop")
        yield "data: [DONE]\n\n"

    return gen()


def _chunk(delta: dict, finish: str | None = None) -> str:
    return (
        "data: "
        + json.dumps(
            {
                "id": "x",
                "object": "chat.completion.chunk",
                "created": 0,
                "model": "triage-fast",
                "choices": [{"index": 0, "delta": delta, "finish_reason": finish}],
            }
        )
        + "\n\n"
    )


def canonical_ir(ir_dict: dict[str, Any]) -> tuple[Any, str, dict[str, Any]]:
    """(parsed IRDocument, contentHash, canonical view-stripped dict) — the registry storage shape
    (M11 §1.2), so a test can save an agent exactly as the API does."""
    doc = parse_document(ir_dict)
    chash = content_hash(doc)
    canon = doc.model_dump(mode="json", by_alias=True, exclude_none=False)
    canon.pop("view", None)
    canon["contentHash"] = chash
    return doc, chash, canon


async def save_agent(sessionmaker: Any, agents: Any, ir_dict: dict[str, Any]) -> tuple[str, str]:
    """Persist an agent + first version directly (the registry storage path). Returns
    (agent_id, version)."""
    doc, chash, canon = canonical_ir(ir_dict)
    async with sessionmaker() as session, session.begin():
        await agents.create_agent(session, agent_id=doc.id, name=doc.name)
        await agents.add_version(
            session, agent_id=doc.id, version=doc.version, content_hash=chash, ir=canon, view=None
        )
    return doc.id, doc.version


async def reset_dbos_schema(pg_url: str) -> None:
    """Drop + recreate the DBOS ``dbos`` schema so each durable test starts with no pending or
    recovered workflows from a prior test (D2 isolation). Never touches ``public`` (Alembic's)."""
    conn = await asyncpg.connect(dsn=plain_dsn(pg_url))
    try:
        await conn.execute("DROP SCHEMA IF EXISTS dbos CASCADE")
    finally:
        await conn.close()
    from theygent_control_plane.durable import run_dbos_migrations

    run_dbos_migrations(pg_url)
