"""A reachable upstream's capabilities: probed when it is llama.cpp, unknown otherwise.

A llama-server started by hand and registered as openai-compatible reported
``toolCalling: false, maxContext: null, approximate: false`` — a confident "unsupported" for
features that worked — so the editor hid its tool settings."""

from __future__ import annotations

import asyncio

import pytest
from _fake_upstream import FakeUpstreamHandle
from _payloads import reachable_payload
from fastapi.testclient import TestClient
from theygent_ir import Capabilities


@pytest.fixture
def llama_like() -> object:
    upstream = FakeUpstreamHandle(Capabilities())  # answers /props like llama-server
    yield upstream
    asyncio.run(upstream.terminate())


def test_a_reachable_llamacpp_server_is_probed(client: TestClient, llama_like) -> None:
    client.put("/admin/models/oss", json=reachable_payload(base_url=f"{llama_like.base_url}/v1"))
    caps = client.get("/admin/models/oss/capabilities").json()
    assert caps["toolCalling"] is True
    assert caps["maxContext"] == 4096
    assert caps["reasoning"] is True  # from the chat template
    assert caps["approximate"] is True  # started with flags this plane did not choose


def test_the_probe_carries_the_upstream_credential(
    client: TestClient, llama_like, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("LLAMA_KEY", "sk-local")
    client.put(
        "/admin/models/oss",
        json=reachable_payload(
            base_url=f"{llama_like.base_url}/v1", credential_ref="secret://LLAMA_KEY"
        ),
    )
    client.get("/admin/models/oss/capabilities")
    assert llama_like.last_authorization == "Bearer sk-local"


def test_any_other_upstream_reports_unknown_not_unsupported(client: TestClient) -> None:
    client.put("/admin/models/hosted", json=reachable_payload(base_url="http://127.0.0.1:1/v1"))
    caps = client.get("/admin/models/hosted/capabilities").json()
    assert caps["approximate"] is True
    assert caps["modalities"] == ["chat"]
    assert caps["maxContext"] is None
