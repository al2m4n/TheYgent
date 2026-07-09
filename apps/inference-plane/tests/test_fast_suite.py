"""The fast suite — everything real except the model weights.

Walks the loop: register -> /v1/chat/completions -> lazy spawn -> streamed
completion -> evict frees -> re-spawn. The FakeUpstreamLauncher is a real
in-process OpenAI-compatible server, so the manager really spawns/ports, LiteLLM
really proxies, and SSE really streams.
"""

from __future__ import annotations

import json

import pytest
from _fake_upstream import FULL_MESSAGE, FakeUpstreamLauncher
from _payloads import managed_payload, reachable_payload
from fastapi import FastAPI
from fastapi.testclient import TestClient
from theygent_inference_plane.app import create_app

_MESSAGES = [{"role": "user", "content": "hi"}]


def _chat(model: str, *, stream: bool = False) -> dict:
    return {"model": model, "messages": _MESSAGES, "stream": stream}


# 1. Register, no spawn — proves laziness.
def test_register_does_not_spawn(client: TestClient, launcher: FakeUpstreamLauncher) -> None:
    r = client.put("/admin/models/triage-fast", json=managed_payload())
    assert r.status_code == 200
    body = r.json()
    assert body["logicalId"] == "triage-fast"
    assert body["binding"]["binding"] == "llamacpp"
    assert body["state"]["resident"] is False

    echoed = client.get("/admin/models/triage-fast").json()
    assert echoed["binding"]["model"] == "fake-model"
    assert launcher.launch_count == 0


# 2. Lazy spawn on first call.
def test_lazy_spawn_on_first_call(
    client: TestClient, app: FastAPI, launcher: FakeUpstreamLauncher
) -> None:
    client.put("/admin/models/triage-fast", json=managed_payload())
    r = client.post("/v1/chat/completions", json=_chat("triage-fast"))
    assert r.status_code == 200
    data = r.json()
    assert data["object"] == "chat.completion"
    assert data["choices"][0]["message"]["content"] == FULL_MESSAGE
    assert app.state.manager.spawn_count == 1
    assert launcher.launch_count == 1
    assert app.state.manager.state("triage-fast")["resident"] is True


# 3. SSE streaming.
def test_streaming_sse(client: TestClient) -> None:
    client.put("/admin/models/triage-fast", json=managed_payload())
    contents: list[str] = []
    saw_done = False
    with client.stream("POST", "/v1/chat/completions", json=_chat("triage-fast", stream=True)) as r:
        assert r.status_code == 200
        assert r.headers["content-type"].startswith("text/event-stream")
        for line in r.iter_lines():
            if not line.startswith("data:"):
                continue
            payload = line[len("data:") :].strip()
            if payload == "[DONE]":
                saw_done = True
                continue
            delta = json.loads(payload)["choices"][0]["delta"]
            if delta.get("content"):
                contents.append(delta["content"])
    assert saw_done
    assert "".join(contents) == FULL_MESSAGE


# 4. Warm is idempotent with run — warm loads it, the call reuses it.
def test_warm_then_run_single_spawn(client: TestClient, app: FastAPI) -> None:
    client.put("/admin/models/triage-fast", json=managed_payload())
    w = client.post("/admin/models/triage-fast:warm")
    assert w.status_code == 200
    assert w.json()["state"]["resident"] is True
    assert app.state.manager.spawn_count == 1

    r = client.post("/v1/chat/completions", json=_chat("triage-fast"))
    assert r.status_code == 200
    assert app.state.manager.spawn_count == 1


# 5. Evict frees, then re-spawns — re-spawn proves a resource was released.
def test_evict_frees_then_respawns(
    client: TestClient, app: FastAPI, launcher: FakeUpstreamLauncher
) -> None:
    client.put("/admin/models/triage-fast", json=managed_payload())
    client.post("/v1/chat/completions", json=_chat("triage-fast"))
    assert app.state.manager.spawn_count == 1
    first_handle = launcher.handles[0]

    e = client.post("/admin/models/triage-fast:evict")
    assert e.status_code == 200
    assert e.json()["state"]["resident"] is False
    assert first_handle.terminated is True

    client.post("/v1/chat/completions", json=_chat("triage-fast"))
    assert app.state.manager.spawn_count == 2
    assert launcher.launch_count == 2


# 6. Capabilities before inference — the compiler depends on this.
def test_capabilities_without_completion(client: TestClient) -> None:
    client.put("/admin/models/triage-fast", json=managed_payload())
    r = client.get("/admin/models/triage-fast/capabilities")
    assert r.status_code == 200
    caps = r.json()
    assert caps["toolCalling"] is True
    assert caps["structuredOutput"] is True
    assert caps["vision"] is False
    assert caps["maxContext"] == 4096


# 7. Logical-id invariant — engine names are not valid models on /v1/*.
@pytest.mark.parametrize("engine", ["llamacpp", "mlx", "vllm"])
def test_engine_name_rejected_on_v1(client: TestClient, engine: str) -> None:
    r = client.post("/v1/chat/completions", json=_chat(engine))
    assert r.status_code == 404
    assert r.json()["error"]["code"] == "model_not_found"


# 10. Custom-method routing — {logical_id} captures correctly before :warm/:evict.
def test_custom_method_routing(client: TestClient) -> None:
    client.put("/admin/models/triage-fast", json=managed_payload())
    w = client.post("/admin/models/triage-fast:warm")
    assert w.status_code == 200
    assert w.json()["logicalId"] == "triage-fast"
    e = client.post("/admin/models/triage-fast:evict")
    assert e.status_code == 200
    assert e.json()["logicalId"] == "triage-fast"


# Surfaces stay separate: GET /v1/models is OpenAI-shaped over logical ids.
def test_v1_models_lists_logical_ids(client: TestClient) -> None:
    client.put("/admin/models/triage-fast", json=managed_payload())
    body = client.get("/v1/models").json()
    assert body["object"] == "list"
    assert [m["id"] for m in body["data"]] == ["triage-fast"]
    assert body["data"][0]["object"] == "model"


# Reachable (openai-compatible) is a passthrough — never managed, credential is local.
def test_reachable_passthrough_with_local_credential(
    app: FastAPI, client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    import asyncio

    from _fake_upstream import FakeUpstreamHandle
    from theygent_ir import Capabilities

    upstream = FakeUpstreamHandle(Capabilities())
    try:
        monkeypatch.setenv("MY_KEY", "sk-local-only")
        client.put(
            "/admin/models/hosted",
            json=reachable_payload(
                base_url=f"{upstream.base_url}/v1", credential_ref="secret://MY_KEY"
            ),
        )
        r = client.post("/v1/chat/completions", json=_chat("hosted"))
        assert r.status_code == 200
        assert r.json()["choices"][0]["message"]["content"] == FULL_MESSAGE
        assert app.state.manager.spawn_count == 0  # reachable is never lifecycle-managed
    finally:
        asyncio.run(upstream.terminate())


# Commit-before-dispatch: an upstream that REJECTS a streaming request (e.g. an
# unsupported generation param) must get a structured 4xx BEFORE the 200/text-event-stream
# header is committed — never a 200 followed by a torn stream that hides the real message.
def test_streaming_upstream_reject_returns_structured_error(client: TestClient) -> None:
    client.put("/admin/models/triage-fast", json=managed_payload())
    r = client.post(
        "/v1/chat/completions",
        json={
            "model": "triage-fast",
            "messages": [{"role": "user", "content": "__force_upstream_400__"}],
            "stream": True,
        },
    )
    assert r.status_code == 400
    assert r.headers["content-type"].startswith("application/json")
    err = r.json()["error"]
    assert err["code"] == "upstream_error"
    assert "temperature" in err["message"]  # the upstream's own message survives


# Same guarantee for a reachable (hosted openai-compatible) binding — the path a
# remote provider's param rejection actually travels.
def test_streaming_reachable_reject_returns_structured_error(client: TestClient) -> None:
    import asyncio

    from _fake_upstream import FakeUpstreamHandle
    from theygent_ir import Capabilities

    upstream = FakeUpstreamHandle(Capabilities())
    try:
        client.put(
            "/admin/models/hosted", json=reachable_payload(base_url=f"{upstream.base_url}/v1")
        )
        r = client.post(
            "/v1/chat/completions",
            json={
                "model": "hosted",
                "messages": [{"role": "user", "content": "__force_upstream_400__"}],
                "stream": True,
            },
        )
        assert r.status_code == 400
        err = r.json()["error"]
        assert err["code"] == "upstream_error"
        assert "temperature" in err["message"]
    finally:
        asyncio.run(upstream.terminate())


# An upstream that dies AFTER chunks started flowing can't un-send the 200 — the relay
# must end the stream with a structured error frame (SDK clients raise it with the cause),
# never a torn connection, and the engine lease must still release.
def test_streaming_midstream_abort_emits_error_frame(client: TestClient, app: FastAPI) -> None:
    client.put("/admin/models/triage-fast", json=managed_payload())
    frames: list[dict] = []
    with client.stream(
        "POST",
        "/v1/chat/completions",
        json={
            "model": "triage-fast",
            "messages": [{"role": "user", "content": "__abort_mid_stream__"}],
            "stream": True,
        },
    ) as r:
        assert r.status_code == 200
        assert r.headers["content-type"].startswith("text/event-stream")
        for line in r.iter_lines():
            payload = line[len("data:") :].strip() if line.startswith("data:") else ""
            if payload and payload != "[DONE]":
                frames.append(json.loads(payload))
    assert frames, "expected at least the error frame"
    error = frames[-1].get("error")
    assert error is not None, f"last frame should be a structured error, got {frames[-1]}"
    assert error["code"] == "upstream_error"
    assert app.state.manager.state("triage-fast")["inflight"] == 0  # lease released


# A lease that loses the capacity race AFTER warm succeeded (two requests racing one
# slot) must still map to a clean 503 — not escape the endpoint as an opaque 500.
def test_streaming_lease_capacity_race_maps_clean_503(
    client: TestClient, app: FastAPI, monkeypatch: pytest.MonkeyPatch
) -> None:
    import contextlib

    from theygent_inference_plane.manager import NoCapacityError

    client.put("/admin/models/triage-fast", json=managed_payload())

    @contextlib.asynccontextmanager
    async def raced_out(logical_id: str):
        raise NoCapacityError("no capacity: all resident engines are busy")
        yield  # pragma: no cover

    monkeypatch.setattr(app.state.manager, "lease", raced_out)
    r = client.post("/v1/chat/completions", json=_chat("triage-fast", stream=True))
    assert r.status_code == 503
    assert r.json()["error"]["code"] == "no_capacity"


# Commit-before-spawn: a STREAMING request that can't be spawned must get a clean
# 503 BEFORE the 200/text-event-stream header is committed — never a 200 with the
# error buried in the SSE body. max_resident=0 refuses every spawn deterministically.
def test_streaming_no_capacity_returns_clean_503(launcher: FakeUpstreamLauncher) -> None:
    app = create_app(launcher=launcher, max_resident=0, enable_reaper=False)
    with TestClient(app) as client:
        client.put("/admin/models/triage-fast", json=managed_payload())
        r = client.post("/v1/chat/completions", json=_chat("triage-fast", stream=True))
        assert r.status_code == 503
        # Headers committed as JSON error, not as an event-stream.
        assert r.headers["content-type"].startswith("application/json")
        assert "text/event-stream" not in r.headers["content-type"]
        assert r.json()["error"]["code"] == "no_capacity"
        assert launcher.launch_count == 0  # never even attempted a spawn


# Same guarantee on the non-streaming path.
def test_non_streaming_no_capacity_returns_503(launcher: FakeUpstreamLauncher) -> None:
    app = create_app(launcher=launcher, max_resident=0, enable_reaper=False)
    with TestClient(app) as client:
        client.put("/admin/models/triage-fast", json=managed_payload())
        r = client.post("/v1/chat/completions", json=_chat("triage-fast"))
        assert r.status_code == 503
        assert r.json()["error"]["code"] == "no_capacity"


# Missing binary surfaces as /readyz not-ready, never a 500 at first inference.
def test_readyz_not_ready_when_binary_missing() -> None:
    class _NotReadyLauncher:
        ready = False
        not_ready_reason = "llama-server not found"

        async def launch(self, binding):  # pragma: no cover - never reached
            raise RuntimeError("should not launch")

    app = create_app(launcher=_NotReadyLauncher(), enable_reaper=False)
    with TestClient(app) as c:
        assert c.get("/healthz").status_code == 200
        ready = c.get("/readyz")
        assert ready.status_code == 503
        assert ready.json()["status"] == "not-ready"
