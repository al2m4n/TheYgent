"""``reasoning_effort`` reaches the engine instead of failing the run.

The dispatch layer validates ``reasoning_effort`` against its own list of model names, and a
local engine's model is a file path or repo id it has never heard of — so it rejected the call
before any token was generated. Local chat templates (gpt-oss on llama.cpp and MLX) read the
effort as a template variable, so managed engines also get it in ``chat_template_kwargs``."""

from __future__ import annotations

import asyncio

import pytest
from _fake_upstream import FULL_MESSAGE, FakeUpstreamHandle, FakeUpstreamLauncher
from _payloads import managed_payload, reachable_payload
from fastapi.testclient import TestClient
from theygent_ir import Capabilities


def _chat(model: str, **extra: object) -> dict[str, object]:
    return {"model": model, "messages": [{"role": "user", "content": "hi"}], **extra}


@pytest.mark.parametrize("stream", [False, True], ids=["complete", "stream"])
@pytest.mark.parametrize("binding", ["llamacpp", "mlx"])
def test_managed_engine_gets_effort_in_template_kwargs(
    client: TestClient, launcher: FakeUpstreamLauncher, binding: str, stream: bool
) -> None:
    payload = managed_payload(binding=binding, model="/models/gpt-oss-20b-MXFP4.gguf")
    payload["params"] = {"reasoning_effort": "medium"}
    assert client.put("/admin/models/oss", json=payload).status_code == 200

    r = client.post("/v1/chat/completions", json=_chat("oss", stream=stream))

    assert r.status_code == 200, r.text
    if not stream:
        assert r.json()["choices"][0]["message"]["content"] == FULL_MESSAGE
    body = launcher.handles[0].last_chat_body
    assert body is not None
    assert body["reasoning_effort"] == "medium"
    assert body["chat_template_kwargs"] == {"reasoning_effort": "medium"}
    assert "allowed_openai_params" not in body  # a dispatch-layer knob, never on the wire


def test_request_effort_wins_and_other_template_kwargs_stay(
    client: TestClient, launcher: FakeUpstreamLauncher
) -> None:
    payload = managed_payload(model="/models/gpt-oss-20b-MXFP4.gguf")
    payload["params"] = {"reasoning_effort": "low"}
    client.put("/admin/models/oss", json=payload)

    r = client.post(
        "/v1/chat/completions",
        json=_chat(
            "oss",
            reasoning_effort="high",
            chat_template_kwargs={"enable_thinking": True},
        ),
    )

    assert r.status_code == 200, r.text
    body = launcher.handles[0].last_chat_body
    assert body is not None
    assert body["reasoning_effort"] == "high"
    assert body["chat_template_kwargs"] == {"enable_thinking": True, "reasoning_effort": "high"}


def test_reachable_upstream_gets_effort_as_the_openai_field_only(client: TestClient) -> None:
    # A hosted API knows reasoning_effort natively and would reject an unknown
    # chat_template_kwargs field, so a reachable upstream gets the OpenAI field alone.
    upstream = FakeUpstreamHandle(Capabilities())
    try:
        payload = reachable_payload(base_url=f"{upstream.base_url}/v1", model="my-reasoner")
        payload["params"] = {"reasoning_effort": "medium"}
        client.put("/admin/models/hosted", json=payload)

        r = client.post("/v1/chat/completions", json=_chat("hosted"))

        assert r.status_code == 200, r.text
        body = upstream.last_chat_body
        assert body is not None
        assert body["reasoning_effort"] == "medium"
        assert "chat_template_kwargs" not in body
        assert "allowed_openai_params" not in body
    finally:
        asyncio.run(upstream.terminate())
