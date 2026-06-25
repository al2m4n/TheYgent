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
