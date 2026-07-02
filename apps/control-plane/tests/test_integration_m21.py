"""The M21 real-engine gate (m21.md Phase 5) — autonomous tool-calling against a LIVE inference
plane + a tool-calling-capable engine.

The fast suite (``test_m21_tool_calling.py``) proves the loop end-to-end with a scripted fake. This
env-gated test is the actual "supported" gate: a real model, offered a tool, DECIDES to call it, the
walker runs it, feeds the result back, and the model answers — proven on the real surface, not a
fake (per [[prove-real-path-before-abstracting]]).

Engine support is the precondition, and it is engine-specific (m21.md §1): **llama.cpp is PROVEN**;
mlx_lm.server is unproven/approximate (point THEYGENT_INFERENCE_PLANE_BASE_URL at a llama.cpp plane
and set THEYGENT_LOGICAL_MODEL to a tool-calling GGUF). Skipped by default
(``-m 'not integration'``);
skips clean without prerequisites. Run::

    DATABASE_URL=postgresql+asyncpg://localhost/theygent \
    THEYGENT_INFERENCE_PLANE_BASE_URL=http://127.0.0.1:8081/v1 \
    THEYGENT_LOGICAL_MODEL=qwen2.5-7b-instruct \
        uv run --package theygent-control-plane pytest -m integration \
        apps/control-plane/tests/test_integration_m21.py
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest
from _ir import llm_ir
from fastapi.testclient import TestClient

pytestmark = pytest.mark.integration

_DATABASE_URL = os.environ.get("DATABASE_URL")
_INFERENCE_BASE_URL = os.environ.get("THEYGENT_INFERENCE_PLANE_BASE_URL")
_MODEL = os.environ.get("THEYGENT_LOGICAL_MODEL", "triage-fast")

_skip = pytest.mark.skipif(
    not _DATABASE_URL or not _INFERENCE_BASE_URL,
    reason="needs DATABASE_URL (real PG) + THEYGENT_INFERENCE_PLANE_BASE_URL "
    "(a live tool-calling plane)",
)


def _prepare_db(url: str) -> None:
    from alembic import command
    from alembic.config import Config

    ini = Path(__file__).resolve().parents[1] / "alembic.ini"
    os.environ["DATABASE_URL"] = url
    command.upgrade(Config(str(ini)), "head")


@_skip
def test_real_model_calls_a_tool_then_answers() -> None:
    from theygent_control_plane.app import create_app

    assert _DATABASE_URL and _INFERENCE_BASE_URL
    _prepare_db(_DATABASE_URL)

    # input → llm(bound to a real tool-calling model, offered the http_fetch builtin) → output.
    ir = llm_ir("$in")
    ir["models"]["default"]["model"] = _MODEL
    ir["tools"] = {"http_fetch": {"kind": "builtin", "ref": "http_fetch"}}
    ir["nodes"][1]["config"]["tools"] = ["http_fetch"]
    ir["nodes"][1]["config"]["maxToolIterations"] = 4

    app = create_app(inference_base_url=_INFERENCE_BASE_URL, database_url=_DATABASE_URL)
    prompt = (
        "Use the http_fetch tool to GET https://example.com and then tell me, in one sentence, "
        "what the page is about."
    )
    with TestClient(app) as client:
        # Stream so we can SEE the model decide to call the tool (an `event: tool_call` frame), then
        # answer using the result — the real end-to-end proof.
        events: list[tuple[str | None, str]] = []
        with client.stream(
            "POST", "/graphs/runs", json={"ir": ir, "input": prompt, "stream": True}
        ) as resp:
            ev: str | None = None
            for line in "".join(resp.iter_text()).splitlines():
                if line.startswith("event:"):
                    ev = line[len("event:") :].strip()
                elif line.startswith("data:"):
                    events.append((ev, line[len("data:") :].strip()))
                    ev = None

    tool_calls = [json.loads(d)["toolCall"] for e, d in events if e == "tool_call"]
    answer = "".join(json.loads(d)["delta"] for e, d in events if e == "delta")
    final = [json.loads(d) for e, d in events if e == "run"][-1]
    assert tool_calls == ["http_fetch"], f"model did not call the tool; events={events}"
    assert final["status"] == "completed"
    assert answer.strip(), "model produced no final answer after the tool result"
