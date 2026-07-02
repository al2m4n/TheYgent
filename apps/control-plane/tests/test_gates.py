"""M19 §2.8 / §1.6 — the gate seam (ratelimit · quota), per-KEY (per-user deferred).

A gate emits ``allow`` (input through) or ``deny`` (a clean result — never a hang). ``ratelimit`` is
a trivial per-key counter; ``quota`` READS accumulated token usage off M17 spans (no new metering).
The guards: deny past the limit/budget, per-key independence, and per-USER is NOT implemented (the
key is the M12-token/keyExpr bucket; the identity work is deferred — by scoping, not faked).
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

import asyncpg
from _db import plain_dsn
from fastapi.testclient import TestClient
from theygent_control_plane.run import new_ulid, now


def _gate_agent(gate: dict[str, Any]) -> dict[str, Any]:
    """input -> gate -> {allow -> out_allow, deny -> out_deny}. The run output is whichever branch
    fired (only one output node runs), so a test reads allow-vs-deny directly from the output."""
    gate = {
        **gate,
        "id": "n_gate",
        "ports": {
            "in": [{"id": "in", "type": "any"}],
            "out": [{"id": "allow", "type": "any"}, {"id": "deny", "type": "any"}],
        },
    }
    return {
        "schemaVersion": "1.0",
        "id": "agt_gate",
        "name": "gate-agent",
        "version": "0.1.0",
        "models": {},
        "tools": {},
        "nodes": [
            {
                "id": "n_in",
                "type": "input",
                "kind": "boundary",
                "ports": {"in": [], "out": [{"id": "out", "type": "any"}]},
            },
            gate,
            {
                "id": "out_allow",
                "type": "output",
                "kind": "boundary",
                "ports": {"in": [{"id": "in", "type": "any"}], "out": []},
            },
            {
                "id": "out_deny",
                "type": "output",
                "kind": "boundary",
                "ports": {"in": [{"id": "in", "type": "any"}], "out": []},
            },
        ],
        "edges": [
            _edge("e1", "n_in", "out", "n_gate", "in"),
            _edge("e2", "n_gate", "allow", "out_allow", "in"),
            _edge("e3", "n_gate", "deny", "out_deny", "in"),
        ],
    }


def _edge(eid: str, src: str, sh: str, tgt: str, th: str) -> dict[str, Any]:
    return {
        "id": eid,
        "source": src,
        "sourceHandle": sh,
        "target": tgt,
        "targetHandle": th,
        "channel": "data",
    }


def _run(client: TestClient, ir: dict[str, Any], input_value: Any) -> dict[str, Any]:
    return client.post(
        "/graphs/runs", json={"ir": ir, "input": input_value, "stream": False}
    ).json()


# ── ratelimit ────────────────────────────────────────────────────────────────────────────────────


def test_ratelimit_allows_then_denies(client: TestClient) -> None:
    ir = _gate_agent(
        {
            "type": "ratelimit",
            "kind": "activity",
            "config": {"keyExpr": "$in.in.user", "limit": 2, "windowSeconds": 3600},
        }
    )
    # First two for user u1 allow (carry the input through), the third denies.
    for _ in range(2):
        out = _run(client, ir, {"user": "u1"})
        assert out["status"] == "completed"
        assert json.loads(out["output"]) == {"user": "u1"}
    denied = _run(client, ir, {"user": "u1"})
    assert "denied" in denied["output"]
    assert "rate limit exceeded" in denied["output"]


def test_ratelimit_is_per_key(client: TestClient) -> None:
    ir = _gate_agent(
        {
            "type": "ratelimit",
            "kind": "activity",
            "config": {"keyExpr": "$in.in.user", "limit": 1, "windowSeconds": 3600},
        }
    )
    assert json.loads(_run(client, ir, {"user": "a"})["output"]) == {"user": "a"}  # a: allow
    assert "denied" in _run(client, ir, {"user": "a"})["output"]  # a: 2nd → deny
    assert json.loads(_run(client, ir, {"user": "b"})["output"]) == {"user": "b"}  # b: independent


# ── quota (reads accumulated span token usage; per-user attribution deferred) ──────────


def test_quota_allows_with_no_usage(client: TestClient) -> None:
    ir = _gate_agent(
        {
            "type": "quota",
            "kind": "activity",
            "config": {"keyExpr": "$caller.token", "budgetTokens": 1000, "windowSeconds": 3600},
        }
    )
    out = _run(client, ir, {"q": "hi"})
    assert json.loads(out["output"]) == {"q": "hi"}  # no usage recorded → allow


async def _seed_usage(pg_url: str, agent_id: str, tokens: int) -> None:
    """Insert a prior run + a span carrying token usage (so quota's usage-read sees it)."""
    conn = await asyncpg.connect(dsn=plain_dsn(pg_url))
    try:
        run_id = new_ulid()
        ts = now()
        await conn.execute(
            "INSERT INTO run (id, status, model, graph_id, created_at, updated_at) "
            "VALUES ($1,'completed','m',$2,$3,$3)",
            run_id,
            agent_id,
            ts,
        )
        await conn.execute(
            "INSERT INTO span (id, run_id, trace_id, otel_span_id, name, status, start_ns, "
            "seq, created_at, attributes) "
            "VALUES ($1,$2,'t','s','n','ok',0,0,$3,$4)",
            new_ulid(),
            run_id,
            ts,
            json.dumps({"gen_ai.usage.total_tokens": tokens}),
        )
    finally:
        await conn.close()


def test_quota_denies_over_budget(client: TestClient, pg_url: str) -> None:
    asyncio.run(_seed_usage(pg_url, "agt_gate", tokens=5000))  # already over a 1000 budget
    ir = _gate_agent(
        {
            "type": "quota",
            "kind": "activity",
            "config": {"keyExpr": "$caller.token", "budgetTokens": 1000, "windowSeconds": 3600},
        }
    )
    out = _run(client, ir, {"q": "hi"})
    assert "denied" in out["output"]
    assert "token budget exceeded" in out["output"]


def _quota_llm_agent(budget_tokens: int) -> dict[str, Any]:
    """input -> quota -> (allow) -> llm -> out_allow; (deny) -> out_deny — the real cost-control
    shape: the gate meters the very model calls the graph makes."""
    return {
        "schemaVersion": "1.0",
        "id": "agt_quota_llm",
        "name": "quota-llm-agent",
        "version": "0.1.0",
        "models": {"default": {"binding": "mlx", "model": "triage-fast", "params": {}}},
        "tools": {},
        "nodes": [
            {
                "id": "n_in",
                "type": "input",
                "kind": "boundary",
                "ports": {"in": [], "out": [{"id": "out", "type": "any"}]},
            },
            {
                "id": "n_gate",
                "type": "quota",
                "kind": "activity",
                "config": {
                    "keyExpr": "$caller.token",
                    "budgetTokens": budget_tokens,
                    "windowSeconds": 3600,
                },
                "ports": {
                    "in": [{"id": "in", "type": "any"}],
                    "out": [{"id": "allow", "type": "any"}, {"id": "deny", "type": "any"}],
                },
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
                "id": "out_allow",
                "type": "output",
                "kind": "boundary",
                "ports": {"in": [{"id": "in", "type": "any"}], "out": []},
            },
            {
                "id": "out_deny",
                "type": "output",
                "kind": "boundary",
                "ports": {"in": [{"id": "in", "type": "any"}], "out": []},
            },
        ],
        "edges": [
            _edge("e1", "n_in", "out", "n_gate", "in"),
            _edge("e2", "n_gate", "allow", "n_llm", "in"),
            _edge("e3", "n_llm", "ok", "out_allow", "in"),
            _edge("e4", "n_gate", "deny", "out_deny", "in"),
        ],
    }


def test_quota_meters_real_llm_usage_and_denies(client: TestClient) -> None:
    # The closed loop, with NO hand-seeded spans: run 1 passes the gate (no usage yet) and its llm
    # streams — the model.generate span records the usage the (fake-engine) server reported on the
    # stream's final chunk. Run 2 reads that accumulated usage back through the gate and DENIES.
    # This is the fail-closed proof: if the capture ever stops writing usage, run 2 would allow
    # and this test fails.
    ir = _quota_llm_agent(budget_tokens=2)  # one fake turn reports total_tokens=3
    first = _run(client, ir, "hi")
    assert first["status"] == "completed"
    assert first["output"] == "hello world"
    spans = client.get(f"/runs/{first['runId']}/trace").json()["spans"]
    gen = next(s for s in spans if s["phase"] == "model.generate")
    attrs = gen["attributes"] or {}
    assert attrs["gen_ai.usage.total_tokens"] == 3  # streamed usage landed on the span
    assert attrs["gen_ai.usage.input_tokens"] == 1
    assert attrs["gen_ai.usage.output_tokens"] == 2
    node = next(s for s in spans if s["name"] == "n_llm" and s["phase"] is None)
    assert "gen_ai.usage.total_tokens" not in (node["attributes"] or {})  # no double-count mirror

    denied = _run(client, ir, "hi")
    assert "denied" in denied["output"]
    assert "token budget exceeded" in denied["output"]


def test_usage_is_read_from_both_streaming_wire_shapes() -> None:
    # The two REAL shapes of the terminal usage chunk: engines emit it with an EMPTY choices
    # list; a proxying router re-emits it with a non-empty choices list whose delta is all-null.
    # Both must yield the same normalized usage — reading usage gated on the choices shape is
    # exactly the bug that would silently drop one of them.
    from types import SimpleNamespace as NS

    from theygent_control_plane.walker import parse_chunk

    usage = NS(prompt_tokens=31, completion_tokens=5, total_tokens=36)
    engine_shape = NS(choices=[], usage=usage)
    proxy_shape = NS(
        choices=[
            NS(finish_reason=None, delta=NS(reasoning_content=None, content=None, tool_calls=None))
        ],
        usage=usage,
    )
    expected = {"prompt_tokens": 31, "completion_tokens": 5, "total_tokens": 36}
    assert parse_chunk(engine_shape).usage == expected
    assert parse_chunk(proxy_shape).usage == expected
    # And an ordinary content chunk (no usage attribute value) reports None, never zeros.
    plain = NS(
        choices=[
            NS(finish_reason=None, delta=NS(reasoning_content=None, content="hi", tool_calls=None))
        ],
        usage=None,
    )
    assert parse_chunk(plain).usage is None
