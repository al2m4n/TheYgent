"""llama.cpp launch settings: safe defaults, per-registration overrides, loud validation.

Left to its own defaults llama-server sizes a KV cache for the model's full trained context in
each of several slots (231,936 tokens for gpt-oss-20b), which ran a 24 GB Mac out of GPU memory,
and keeps a 512-token physical batch that rejects any longer embedding input. A registration
had no way to change either."""

from __future__ import annotations

import struct
import sys
from pathlib import Path

import pytest
from _fake_upstream import FakeUpstreamLauncher
from _payloads import managed_payload
from fastapi.testclient import TestClient
from theygent_inference_plane.launcher import LlamaCppLauncher
from theygent_inference_plane.weights import read_gguf_context_length
from theygent_ir import ManagedBinding


def _gguf(path: Path, *, architecture: str = "llama", context_length: int | None) -> str:
    """A metadata-only GGUF v3: the architecture and (optionally) its trained context."""

    def string(value: str) -> bytes:
        raw = value.encode()
        return struct.pack("<Q", len(raw)) + raw

    kvs = [string("general.architecture") + struct.pack("<I", 8) + string(architecture)]
    if context_length is not None:
        kvs.append(
            string(f"{architecture}.context_length")
            + struct.pack("<I", 4)
            + struct.pack("<I", context_length)
        )
    path.write_bytes(b"GGUF" + struct.pack("<IQQ", 3, 0, len(kvs)) + b"".join(kvs))
    return str(path)


def _binding(modality: str = "chat", model: str = "org/repo", **params: object) -> ManagedBinding:
    source = "local-path" if model.startswith("/") else "hf"
    return ManagedBinding(
        binding="llamacpp",
        source=source,  # ty: ignore[invalid-argument-type]
        model=model,
        modality=modality,  # ty: ignore[invalid-argument-type]
        params=dict(params),
    )


def _flags(binding: ManagedBinding) -> dict[str, str]:
    cmd = LlamaCppLauncher(binary_path=sys.executable)._build_command(binding, 9000)
    flags = {}
    for flag in ("-c", "-np", "-b", "-ub"):
        assert cmd.count(flag) <= 1, cmd
        if flag in cmd:
            flags[flag] = cmd[cmd.index(flag) + 1]
    return flags


# ── the GGUF header ──────────────────────────────────────────────────────────


def test_reads_the_trained_context_from_the_header(tmp_path: Path) -> None:
    assert read_gguf_context_length(_gguf(tmp_path / "m.gguf", context_length=131072)) == 131072
    assert read_gguf_context_length(_gguf(tmp_path / "n.gguf", context_length=None)) is None
    (tmp_path / "x.gguf").write_bytes(b"not a gguf")
    assert read_gguf_context_length(str(tmp_path / "x.gguf")) is None


# ── defaults ─────────────────────────────────────────────────────────────────


def test_chat_defaults_to_one_slot_and_a_capped_context(tmp_path: Path) -> None:
    big = _gguf(tmp_path / "big.gguf", architecture="gpt-oss", context_length=131072)
    small = _gguf(tmp_path / "small.gguf", context_length=4096)
    assert _flags(_binding(model=big)) == {"-c": "32768", "-np": "1"}
    assert _flags(_binding(model=small)) == {"-c": "4096", "-np": "1"}
    assert _flags(_binding()) == {"-c": "32768", "-np": "1"}  # hf source: header not at hand
    assert _flags(_binding("vision", model=big)) == {"-c": "32768", "-np": "1"}


def test_embeddings_default_to_a_batch_as_large_as_the_context(tmp_path: Path) -> None:
    nomic = _gguf(tmp_path / "nomic.gguf", architecture="nomic-bert", context_length=2048)
    long = _gguf(tmp_path / "long.gguf", context_length=32768)
    assert _flags(_binding("embeddings", model=nomic)) == {
        "-c": "2048",
        "-np": "1",
        "-b": "2048",
        "-ub": "2048",
    }
    assert _flags(_binding("embeddings", model=long))["-ub"] == "8192"  # capped
    assert _flags(_binding("embeddings"))["-ub"] == "2048"  # header not at hand


# ── overrides ────────────────────────────────────────────────────────────────


def test_registration_settings_override_the_defaults() -> None:
    binding = _binding(ctxSize=8192, parallel=2, batchSize=1024, ubatchSize=256)
    assert _flags(binding) == {"-c": "8192", "-np": "2", "-b": "1024", "-ub": "256"}


def test_an_embeddings_context_override_carries_the_batch_with_it() -> None:
    flags = _flags(_binding("embeddings", ctxSize=4096))
    assert (flags["-c"], flags["-b"], flags["-ub"]) == ("4096", "4096", "4096")


# ── registration validates; requests never carry launch settings ─────────────


@pytest.mark.parametrize(
    ("binding", "params"),
    [
        ("mlx", {"ctxSize": 8192}),
        ("llamacpp", {"ctxSize": "big"}),
        ("llamacpp", {"parallel": 0}),
        ("llamacpp", {"ubatchSize": True}),
    ],
)
def test_invalid_launch_settings_are_refused(
    client: TestClient, binding: str, params: dict[str, object]
) -> None:
    payload = managed_payload(binding=binding)
    payload["params"] = params
    r = client.put("/admin/models/m", json=payload)
    assert r.status_code == 422, r.text
    assert r.json()["error"]["code"] == "invalid_binding"


def test_launch_settings_never_reach_a_request(
    client: TestClient, launcher: FakeUpstreamLauncher
) -> None:
    payload = managed_payload()
    payload["params"] = {"ctxSize": 8192, "parallel": 1, "temperature": 0.3}
    assert client.put("/admin/models/m", json=payload).status_code == 200
    r = client.post(
        "/v1/chat/completions", json={"model": "m", "messages": [{"role": "user", "content": "hi"}]}
    )
    assert r.status_code == 200, r.text
    body = launcher.handles[0].last_chat_body
    assert body is not None
    assert body["temperature"] == 0.3
    assert not {"ctxSize", "parallel", "pooling", "mmproj"} & body.keys()
