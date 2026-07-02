"""Cross-plane composition — the core cross-plane differentiator.

One graph, two planes: a local `mlx` model and a reachable `openai-compatible`
(hosted) model, both answering through the same `/v1/*` surface via logical ids.
Neither call site knows which plane it hit — the gateway flattens the difference.
This hermetic version fakes both sides; the real-MLX version is an @integration test.

Needs no new schema: per-node bindings + one OpenAI surface already cover it.
"""

from __future__ import annotations

import asyncio

import pytest
from _fake_upstream import FULL_MESSAGE, FakeUpstreamHandle
from _payloads import managed_payload, reachable_payload
from fastapi import FastAPI
from fastapi.testclient import TestClient
from theygent_ir import Capabilities

_MESSAGES = [{"role": "user", "content": "hi"}]


def _content(resp) -> str:
    return resp.json()["choices"][0]["message"]["content"]


def test_cross_plane_subgraph_as_tool_both_directions(
    app: FastAPI, client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Local plane: an `mlx` logical model (served here by the manager's fake launcher).
    client.put("/admin/models/local-mlx", json=managed_payload(binding="mlx"))

    # Hosted plane: a reachable `openai-compatible` logical model — a standalone fake
    # upstream standing in for the hosted API, with a credential reference.
    hosted = FakeUpstreamHandle(Capabilities())
    try:
        monkeypatch.setenv("HOSTED_KEY", "sk-local-only")
        client.put(
            "/admin/models/hosted",
            json=reachable_payload(
                base_url=f"{hosted.base_url}/v1", credential_ref="secret://HOSTED_KEY"
            ),
        )

        # Both planes answer through the SAME /v1 surface via logical ids.
        local = client.post(
            "/v1/chat/completions", json={"model": "local-mlx", "messages": _MESSAGES}
        )
        hosted_resp = client.post(
            "/v1/chat/completions", json={"model": "hosted", "messages": _MESSAGES}
        )
        assert local.status_code == hosted_resp.status_code == 200

        # Direction 1 — hosted agent calls the local mlx model as a subgraph/tool: the
        # hosted output crosses planes into a local call through the same gateway.
        compose_into_local = client.post(
            "/v1/chat/completions",
            json={
                "model": "local-mlx",
                "messages": [*_MESSAGES, {"role": "user", "content": _content(hosted_resp)}],
            },
        )
        assert compose_into_local.status_code == 200
        assert _content(compose_into_local) == FULL_MESSAGE

        # Direction 2 — local mlx agent calls the hosted model as a subgraph/tool.
        compose_into_hosted = client.post(
            "/v1/chat/completions",
            json={
                "model": "hosted",
                "messages": [*_MESSAGES, {"role": "user", "content": _content(local)}],
            },
        )
        assert compose_into_hosted.status_code == 200
        assert _content(compose_into_hosted) == FULL_MESSAGE

        # Only the local plane was lifecycle-managed; the hosted plane never was.
        assert app.state.manager.spawn_count == 1

        # The credential resolved LOCALLY (from env) and reached only the user-configured
        # upstream — never a theygent boundary (there is none in the path).
        assert hosted.last_authorization == "Bearer sk-local-only"
    finally:
        asyncio.run(hosted.terminate())
