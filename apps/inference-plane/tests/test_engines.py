"""M2 fast suite — the manager treats every managed engine identically.

Parametrized over all three managed bindings via the engine-agnostic fake launcher.
This proves *routing* (the binding selects an engine and the manager lifecycle is the
same for each); it proves NOTHING about real MLX or real vLLM — those are the
integration tests (MLX green on Apple Silicon; vLLM CUDA-gated and UNVERIFIED).
"""

from __future__ import annotations

import json

import pytest
from _fake_upstream import FULL_MESSAGE, FakeUpstreamLauncher
from _payloads import managed_payload
from fastapi import FastAPI
from fastapi.testclient import TestClient
from theygent_inference_plane.app import create_app
from theygent_inference_plane.launcher import ManagedLauncherSet

_MESSAGES = [{"role": "user", "content": "hi"}]
_MANAGED_BINDINGS = ["llamacpp", "mlx", "vllm"]


def _chat(model: str, *, stream: bool = False) -> dict:
    return {"model": model, "messages": _MESSAGES, "stream": stream}


# The full lifecycle loop is identical for every managed engine (fake launcher).
@pytest.mark.parametrize("binding", _MANAGED_BINDINGS)
def test_managed_binding_full_loop(
    client: TestClient, app: FastAPI, launcher: FakeUpstreamLauncher, binding: str
) -> None:
    r = client.put("/admin/models/m", json=managed_payload(binding=binding))
    assert r.status_code == 200
    assert r.json()["binding"]["binding"] == binding
    assert app.state.manager.state("m")["resident"] is False  # lazy

    # Non-stream call -> lazy spawn exactly once, OpenAI-shaped response.
    r = client.post("/v1/chat/completions", json=_chat("m"))
    assert r.status_code == 200
    assert r.json()["choices"][0]["message"]["content"] == FULL_MESSAGE
    assert app.state.manager.spawn_count == 1
    assert app.state.manager.state("m")["resident"] is True

    # Stream call reuses the resident engine.
    saw_done = False
    content = ""
    with client.stream("POST", "/v1/chat/completions", json=_chat("m", stream=True)) as resp:
        assert resp.headers["content-type"].startswith("text/event-stream")
        for line in resp.iter_lines():
            if not line.startswith("data:"):
                continue
            payload = line[len("data:") :].strip()
            if payload == "[DONE]":
                saw_done = True
                continue
            delta = json.loads(payload)["choices"][0]["delta"]
            content += delta.get("content") or ""
    assert saw_done and content == FULL_MESSAGE
    assert app.state.manager.spawn_count == 1

    # Evict frees, next call re-spawns.
    assert client.post("/admin/models/m:evict").status_code == 200
    assert app.state.manager.state("m")["resident"] is False
    client.post("/v1/chat/completions", json=_chat("m"))
    assert app.state.manager.spawn_count == 2


# /admin/engines reflects resident state across the lazy-spawn / evict lifecycle.
def test_admin_engines_lists_resident_state(client: TestClient) -> None:
    assert client.get("/admin/engines").json() == {"maxResident": 2, "resident": []}

    client.put("/admin/models/m", json=managed_payload(binding="mlx"))
    # Registered but not resident yet (lazy).
    assert client.get("/admin/engines").json()["resident"] == []

    client.post("/v1/chat/completions", json=_chat("m"))
    resident = client.get("/admin/engines").json()["resident"]
    assert len(resident) == 1
    assert resident[0]["logicalId"] == "m"
    assert resident[0]["engine"] == "mlx"

    client.post("/admin/models/m:evict")
    assert client.get("/admin/engines").json()["resident"] == []


# ── /readyz per-engine breakdown ─────────────────────────────────────────


class _StubLauncher:
    """A launcher that only reports readiness; launch is never exercised here."""

    def __init__(self, ready: bool, reason: str | None = None) -> None:
        self._ready = ready
        self._reason = reason

    @property
    def ready(self) -> bool:
        return self._ready

    @property
    def not_ready_reason(self) -> str | None:
        return self._reason

    async def launch(self, binding):  # pragma: no cover - not called
        raise RuntimeError("stub")


def test_readyz_reports_per_engine_and_stays_ready_if_any_engine_ready() -> None:
    # A Mac: llamacpp + mlx present, vLLM absent (no CUDA). Service is still READY;
    # a single missing engine must not flip readiness, but it shows in the breakdown.
    launchers = ManagedLauncherSet(
        {
            "llamacpp": _StubLauncher(True),
            "mlx": _StubLauncher(True),
            "vllm": _StubLauncher(False, "vllm not found; install vLLM on a CUDA host"),
        }
    )
    app = create_app(launcher=launchers, enable_reaper=False)
    with TestClient(app) as client:
        body = client.get("/readyz").json()
        assert client.get("/readyz").status_code == 200
        assert body["status"] == "ready"
        assert body["engines"]["llamacpp"]["ready"] is True
        assert body["engines"]["vllm"]["ready"] is False
        assert "CUDA" in body["engines"]["vllm"]["reason"]


def test_readyz_not_ready_only_when_all_engines_missing() -> None:
    launchers = ManagedLauncherSet(
        {"llamacpp": _StubLauncher(False, "no llama"), "mlx": _StubLauncher(False, "no mlx")}
    )
    app = create_app(launcher=launchers, enable_reaper=False)
    with TestClient(app) as client:
        resp = client.get("/readyz")
        assert resp.status_code == 503
        assert resp.json()["status"] == "not-ready"


def test_request_for_unavailable_engine_returns_clean_503() -> None:
    # mlx model registered, but mlx engine not installed -> clean 503 engine_unavailable,
    # never a 500. (vLLM on a Mac is the real-world case.)
    launchers = ManagedLauncherSet(
        {
            "llamacpp": _StubLauncher(True),
            "mlx": _StubLauncher(False, "mlx_lm.server not found"),
        }
    )
    app = create_app(launcher=launchers, enable_reaper=False)
    with TestClient(app) as client:
        client.put("/admin/models/local-mlx", json=managed_payload(binding="mlx"))
        r = client.post("/v1/chat/completions", json=_chat("local-mlx"))
        assert r.status_code == 503
        assert r.json()["error"]["code"] == "engine_unavailable"
