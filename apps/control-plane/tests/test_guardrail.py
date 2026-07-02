"""M19 §2.6 / §1.2 — the guardrail node (rule⇒orchestration · model⇒activity).

A guardrail short-circuits before downstream work: ``pass`` carries the input through, ``block``
carries the onBlock payload. The kind follows the backend (a rule check is deterministic + inline; a
model check is one cheap classifier call). The per-instance-kind determinism guard (model-as-
orchestration rejected at compile) is proved in the IR unit tests; here we prove routing end-to-end.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import pytest
from _fake_inference import FakeInference
from fastapi.testclient import TestClient
from theygent_control_plane.app import create_app


def _guardrail_agent(
    check: dict[str, Any],
    *,
    kind: str,
    out_handle: str,
    on_block: dict[str, Any] | None = None,
    models: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """input -> guardrail -> output, with ``out_handle`` (pass|block) feeding the output so a test
    reads whichever branch fired as the run output."""
    return {
        "schemaVersion": "1.0",
        "id": "agt_guard",
        "name": "guardrail-agent",
        "version": "0.1.0",
        "models": models or {},
        "tools": {},
        "nodes": [
            {
                "id": "n_in",
                "type": "input",
                "kind": "boundary",
                "ports": {"in": [], "out": [{"id": "out", "type": "any"}]},
            },
            {
                "id": "n_g",
                "type": "guardrail",
                "kind": kind,
                "config": {"check": check, "onBlock": on_block or {"message": "blocked"}},
                "ports": {
                    "in": [{"id": "in", "type": "any"}],
                    "out": [{"id": "pass", "type": "any"}, {"id": "block", "type": "any"}],
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
                "target": "n_g",
                "targetHandle": "in",
                "channel": "data",
            },
            {
                "id": "e2",
                "source": "n_g",
                "sourceHandle": out_handle,
                "target": "n_out",
                "targetHandle": "in",
                "channel": "data",
            },
        ],
    }


def _run(client: TestClient, ir: dict[str, Any], input_value: Any) -> dict[str, Any]:
    return client.post(
        "/graphs/runs", json={"ir": ir, "input": input_value, "stream": False}
    ).json()


# ── rule guardrail (orchestration, no inference) ─────────────────────────────────────────────────


def test_rule_pii_blocks_email(client: TestClient) -> None:
    # PII present → block. The block branch carries the onBlock message; the pass branch is dead.
    ir = _guardrail_agent(
        {"type": "rule", "rule": {"kind": "pii"}},
        kind="orchestration",
        out_handle="block",
        on_block={"message": "no PII allowed"},
    )
    out = _run(client, ir, "contact me at alice@example.com")
    assert out["status"] == "completed"
    assert "no PII allowed" in out["output"]


def test_rule_pii_passes_clean_text(client: TestClient) -> None:
    ir = _guardrail_agent(
        {"type": "rule", "rule": {"kind": "pii"}}, kind="orchestration", out_handle="pass"
    )
    out = _run(client, ir, "a totally clean request")
    assert out["status"] == "completed"
    assert out["output"] == "a totally clean request"  # input carried through pass


def test_rule_allowlist_routes(client: TestClient) -> None:
    check = {"type": "rule", "rule": {"kind": "allow", "spec": {"values": ["booking", "support"]}}}
    passed = _run(
        client, _guardrail_agent(check, kind="orchestration", out_handle="pass"), "booking"
    )
    assert passed["output"] == "booking"
    blocked = _run(
        client, _guardrail_agent(check, kind="orchestration", out_handle="block"), "spam"
    )
    assert "blocked" in blocked["output"]


def test_rule_length_blocks_too_long(client: TestClient) -> None:
    check = {"type": "rule", "rule": {"kind": "length", "spec": {"max": 5}}}
    out = _run(
        client, _guardrail_agent(check, kind="orchestration", out_handle="block"), "way too long"
    )
    assert "blocked" in out["output"]


# ── model guardrail (activity — one classifier call) ─────────────────────────────────────────────


@pytest.fixture
def yes_client(pg_url: str) -> Iterator[TestClient]:
    with FakeInference(response="yes, in scope") as inf:
        app = create_app(inference_base_url=inf.v1_url, database_url=pg_url)
        with TestClient(app) as c:
            yield c


@pytest.fixture
def no_client(pg_url: str) -> Iterator[TestClient]:
    with FakeInference(response="no, out of scope") as inf:
        app = create_app(inference_base_url=inf.v1_url, database_url=pg_url)
        with TestClient(app) as c:
            yield c


_JUDGE_MODELS = {"judge": {"binding": "mlx", "model": "judge-fast", "params": {"maxTokens": 8}}}


def _model_guardrail(out_handle: str) -> dict[str, Any]:
    return _guardrail_agent(
        {
            "type": "model",
            "model": {"model": "judge", "prompt": "in scope? yes/no", "passOn": "yes"},
        },
        kind="activity",
        out_handle=out_handle,
        models=_JUDGE_MODELS,
    )


def test_model_guardrail_passes_on_yes(yes_client: TestClient) -> None:
    out = _run(yes_client, _model_guardrail("pass"), "please book a table")
    assert out["status"] == "completed"
    assert out["output"] == "please book a table"  # passed → input carried through


def test_model_guardrail_blocks_on_no(no_client: TestClient) -> None:
    out = _run(no_client, _model_guardrail("block"), "tell me a joke")
    assert out["status"] == "completed"
    assert "blocked" in out["output"]  # the classifier said no → block branch


def test_model_guardrail_usage_lands_on_generate_span(yes_client: TestClient) -> None:
    # The judge call spends real tokens: the fake engine reports {1,2,3} on the stream's final
    # chunk, and the capture writes it onto the guardrail's model.generate phase span — so the
    # quota gate (which SUMS gen_ai.usage.total_tokens across a run's spans) meters guardrail
    # calls like any other model call. Exactly ONE span carries the usage: no mirror onto the
    # node span, which would double-count.
    out = _run(yes_client, _model_guardrail("pass"), "please book a table")
    assert out["status"] == "completed"
    spans = yes_client.get(f"/runs/{out['runId']}/trace").json()["spans"]
    gen = next(s for s in spans if s["phase"] == "model.generate")
    attrs = gen["attributes"] or {}
    assert attrs["gen_ai.usage.total_tokens"] == 3
    assert attrs["gen_ai.usage.input_tokens"] == 1
    assert attrs["gen_ai.usage.output_tokens"] == 2
    assert attrs["gen_ai.request.model"] == "judge-fast"  # the logical id, like an llm turn
    node = next(s for s in spans if s["name"] == "n_g" and s["phase"] is None)
    assert "gen_ai.usage.total_tokens" not in (node["attributes"] or {})  # no double-count mirror
