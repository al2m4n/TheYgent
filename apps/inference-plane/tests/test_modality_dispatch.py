"""M20 fast suite — the (engine, modality) launcher dispatch + per-(engine,modality) readiness.

The load-bearing seam: ``ManagedLauncherSet`` grew its key from the engine alone to
``(engine, modality)`` so one engine can serve several modalities (mlx: ``mlx_lm.server`` chat vs
``mlx_vlm.server`` vision; llama.cpp: ``llama-server`` ± ``--embeddings``) — and the EngineManager
stays UNTOUCHED (it still calls one ``launch(binding)``). These prove routing, the chat fallback,
the bare-key back-compat, the FAIL-CLOSED rejection (no chat fallback for a non-chat modality), the
composite /readyz keys, and the new launchers' command shapes — nothing about real MLX/llama.cpp
weights (those are the env-gated integration tests).
"""

from __future__ import annotations

import asyncio
import sys

import pytest
from _fake_upstream import FakeUpstreamLauncher
from fastapi.testclient import TestClient
from theygent_inference_plane.app import create_app
from theygent_inference_plane.launcher import (
    EngineUnavailableError,
    LlamaCppLauncher,
    ManagedLauncherSet,
    MlxVlmLauncher,
)
from theygent_ir import Capabilities, ManagedBinding


class _RecordingLauncher:
    """A stub EngineLauncher that records the bindings it was asked to launch."""

    def __init__(self, ready: bool = True, reason: str | None = None) -> None:
        self._ready = ready
        self._reason = reason
        self.launched: list[ManagedBinding] = []

    @property
    def ready(self) -> bool:
        return self._ready

    @property
    def not_ready_reason(self) -> str | None:
        return self._reason

    async def launch(self, binding: ManagedBinding):
        self.launched.append(binding)
        return object()  # the manager never reaches here in these unit tests


def _binding(binding: str, modality: str) -> ManagedBinding:
    # str args (the tests pass literals) → the Literal fields; suppress the type-arg narrowing.
    return ManagedBinding(
        binding=binding,  # ty: ignore[invalid-argument-type]
        source="hf",
        model="m",
        modality=modality,  # ty: ignore[invalid-argument-type]
    )


# ── dispatch: the right launcher per (engine, modality) ──────────────────────


def test_dispatch_picks_the_modality_specific_launcher() -> None:
    chat = _RecordingLauncher()
    vision = _RecordingLauncher()
    s = ManagedLauncherSet({("mlx", "chat"): chat, ("mlx", "vision"): vision})

    asyncio.run(s.launch(_binding("mlx", "vision")))
    assert len(vision.launched) == 1 and len(chat.launched) == 0

    asyncio.run(s.launch(_binding("mlx", "chat")))
    assert len(chat.launched) == 1


def test_unregistered_non_chat_modality_is_rejected_not_served_by_chat() -> None:
    # Fail-closed (M19 reject-don't-serve-wrong): an (engine, embeddings) binding with NO embeddings
    # launcher must be REJECTED, never spawned on the chat launcher (where an embeddings request
    # would silently get a chat-shaped answer). The chat launcher is NOT touched.
    chat = _RecordingLauncher()
    s = ManagedLauncherSet({("llamacpp", "chat"): chat})
    with pytest.raises(EngineUnavailableError, match="embeddings"):
        asyncio.run(s.launch(_binding("llamacpp", "embeddings")))
    assert len(chat.launched) == 0  # the chat engine was never spawned for an embeddings binding


def test_bare_string_key_normalizes_to_chat() -> None:
    # Back-compat: a chat-only set built with bare engine keys (the M2 shape, still used by the
    # /readyz stubs) routes a default (chat) binding unchanged.
    chat = _RecordingLauncher()
    s = ManagedLauncherSet({"mlx": chat})
    asyncio.run(s.launch(_binding("mlx", "chat")))
    assert len(chat.launched) == 1


def test_not_ready_modality_launcher_raises_engine_unavailable() -> None:
    s = ManagedLauncherSet(
        {
            ("mlx", "chat"): _RecordingLauncher(ready=True),
            ("mlx", "vision"): _RecordingLauncher(ready=False, reason="mlx_vlm.server not found"),
        }
    )
    with pytest.raises(EngineUnavailableError, match="vision"):
        asyncio.run(s.launch(_binding("mlx", "vision")))


def test_unknown_engine_raises_engine_unavailable() -> None:
    s = ManagedLauncherSet({("mlx", "chat"): _RecordingLauncher()})
    with pytest.raises(EngineUnavailableError):
        asyncio.run(s.launch(_binding("vllm", "chat")))


# ── readiness: composite keys, chat shown bare (unchanged /readyz shape) ──────


def test_readiness_keys_chat_bare_nonchat_suffixed() -> None:
    s = ManagedLauncherSet(
        {
            ("llamacpp", "chat"): _RecordingLauncher(True),
            ("llamacpp", "embeddings"): _RecordingLauncher(True),
            ("mlx", "vision"): _RecordingLauncher(False, "no mlx-vlm"),
        }
    )
    r = s.readiness()
    assert r["llamacpp"].ready is True  # chat → bare engine name (unchanged shape)
    assert r["llamacpp:embeddings"].ready is True  # non-chat → engine:modality
    assert r["mlx:vision"].ready is False
    assert s.ready is True  # aggregate: ready if ANY (engine, modality) is ready


def test_readyz_reports_composite_modality_keys() -> None:
    s = ManagedLauncherSet(
        {
            ("llamacpp", "chat"): _RecordingLauncher(True),
            ("llamacpp", "embeddings"): _RecordingLauncher(True),
            ("mlx", "chat"): _RecordingLauncher(True),
            ("mlx", "vision"): _RecordingLauncher(False, "mlx_vlm.server not found"),
        }
    )
    with TestClient(create_app(launcher=s, enable_reaper=False)) as c:
        body = c.get("/readyz").json()
        assert body["status"] == "ready"  # mlx chat + llamacpp ready ⇒ service ready
        assert body["engines"]["llamacpp"]["ready"] is True
        assert body["engines"]["llamacpp:embeddings"]["ready"] is True
        assert body["engines"]["mlx:vision"]["ready"] is False


# ── the data plane resolves a non-chat binding (managed) ─────────────────────


def test_vision_binding_round_trips_and_reports_modalities() -> None:
    # The fake launcher serves any binding; advertise vision caps so the derived modalities surface.
    launcher = FakeUpstreamLauncher(advertised=Capabilities(vision=True))
    with TestClient(create_app(launcher=launcher, enable_reaper=False)) as c:
        r = c.put(
            "/admin/models/vlm",
            json={"binding": "mlx", "source": "hf", "model": "qwen-vl", "modality": "vision"},
        )
        assert r.status_code == 200
        assert r.json()["binding"]["modality"] == "vision"
        caps = c.get("/admin/models/vlm/capabilities").json()
        assert caps["modalities"] == ["chat", "vision"]  # vision rides chat (M18 §1.3)


def test_embeddings_binding_round_trips() -> None:
    launcher = FakeUpstreamLauncher(advertised=Capabilities(modalities=["embeddings"]))
    with TestClient(create_app(launcher=launcher, enable_reaper=False)) as c:
        c.put(
            "/admin/models/embed",
            json={
                "binding": "llamacpp",
                "source": "local-path",
                "model": "/w",
                "modality": "embeddings",
            },
        )
        caps = c.get("/admin/models/embed/capabilities").json()
        assert caps["modalities"] == ["embeddings"]


# ── fail-closed at the ROUTE: a non-chat modality with no launcher must not spawn chat ──


def _set_with_chat_only() -> tuple[ManagedLauncherSet, _RecordingLauncher]:
    chat = _RecordingLauncher(ready=True)
    return ManagedLauncherSet({("mlx", "chat"): chat}), chat


def test_embeddings_request_to_unservable_modality_returns_503_not_chat() -> None:
    # (mlx, embeddings) is registered but NO embeddings launcher exists → the embeddings request
    # must 503 (engine_unavailable), and the chat launcher (mlx_lm.server) must NEVER be spawned.
    s, chat = _set_with_chat_only()
    with TestClient(create_app(launcher=s, enable_reaper=False)) as c:
        c.put(
            "/admin/models/embed",
            json={"binding": "mlx", "source": "hf", "model": "m", "modality": "embeddings"},
        )
        r = c.post("/v1/embeddings", json={"model": "embed", "input": "x"})
        assert r.status_code == 503
        assert r.json()["error"]["code"] == "engine_unavailable"
        assert (
            len(chat.launched) == 0
        )  # the chat engine was never spawned for an embeddings binding


def test_capabilities_for_unservable_modality_is_503_not_false_chat() -> None:
    # The picker/compiler must not get a stale chat capability set for a binding the host can't
    # actually serve — probing it 503s rather than falsely advertising ["chat"].
    s, _chat = _set_with_chat_only()
    with TestClient(create_app(launcher=s, enable_reaper=False)) as c:
        c.put(
            "/admin/models/whisper",
            json={
                "binding": "mlx",
                "source": "hf",
                "model": "m",
                "modality": "audio.transcription",
            },
        )
        r = c.get("/admin/models/whisper/capabilities")
        assert r.status_code == 503
        assert r.json()["error"]["code"] == "engine_unavailable"


def test_vllm_modality_off_cuda_fails_clean_not_import_explosion() -> None:
    # The vllm-metal guard: a vLLM binding on a non-CUDA host must 503 cleanly (binary not found),
    # never an import-time explosion. Both chat (launcher present-but-not-ready) and a non-chat
    # modality (no launcher at all) resolve to a clean engine_unavailable.
    s = ManagedLauncherSet(
        {
            ("mlx", "chat"): _RecordingLauncher(ready=True),
            ("vllm", "chat"): _RecordingLauncher(ready=False, reason="vllm not found; CUDA host"),
        }
    )
    with TestClient(create_app(launcher=s, enable_reaper=False)) as c:
        c.put("/admin/models/v-chat", json={"binding": "vllm", "source": "hf", "model": "m"})
        r = c.post("/v1/chat/completions", json={"model": "v-chat", "messages": []})
        assert r.status_code == 503
        assert r.json()["error"]["code"] == "engine_unavailable"

        c.put(
            "/admin/models/v-embed",
            json={"binding": "vllm", "source": "hf", "model": "m", "modality": "embeddings"},
        )
        r2 = c.post("/v1/embeddings", json={"model": "v-embed", "input": "x"})
        assert r2.status_code == 503
        assert r2.json()["error"]["code"] == "engine_unavailable"


def test_chat_and_embeddings_same_weights_are_separate_residents() -> None:
    # llama.cpp --embeddings changes server mode; the resident cache keys on LOGICAL ID, so the same
    # weights under two logical ids (chat + embeddings) are two separate residents — no collision,
    # no cross-contamination. (Fake launcher serves both endpoints, so both round-trip.)
    with TestClient(create_app(launcher=FakeUpstreamLauncher(), enable_reaper=False)) as c:
        c.put(
            "/admin/models/w-chat",
            json={"binding": "llamacpp", "source": "local-path", "model": "/w"},
        )
        c.put(
            "/admin/models/w-embed",
            json={
                "binding": "llamacpp",
                "source": "local-path",
                "model": "/w",
                "modality": "embeddings",
            },
        )
        assert c.post("/v1/chat/completions", json={"model": "w-chat", "messages": []}).status_code
        assert c.post("/v1/embeddings", json={"model": "w-embed", "input": "x"}).status_code == 200
        engines = c.get("/admin/engines").json()["resident"]
        ids = {e["logicalId"] for e in engines}
        assert {"w-chat", "w-embed"} <= ids  # two distinct residents for the same weights


# ── the new launchers build the right command (no spawn) ─────────────────────
# binary_path=sys.executable is a real executable on every host, so resolution succeeds without the
# engine being installed; we never spawn it, only inspect the argv the launcher would run.


def test_llamacpp_embeddings_adds_flags() -> None:
    launcher = LlamaCppLauncher(binary_path=sys.executable)
    cmd = launcher._build_command(_binding("llamacpp", "embeddings"), 9001)
    assert "--embeddings" in cmd
    assert cmd[cmd.index("--pooling") + 1] == "mean"


def test_llamacpp_chat_omits_embedding_flags() -> None:
    launcher = LlamaCppLauncher(binary_path=sys.executable)
    cmd = launcher._build_command(_binding("llamacpp", "chat"), 9002)
    assert "--embeddings" not in cmd


def test_mlx_vlm_command_passes_model() -> None:
    launcher = MlxVlmLauncher(binary_path=sys.executable)
    cmd = launcher._build_command(_binding("mlx", "vision"), 9003)
    assert cmd[cmd.index("--model") + 1] == "m"
    assert cmd[cmd.index("--port") + 1] == "9003"
