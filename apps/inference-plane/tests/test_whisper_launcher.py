"""The whisper.cpp speech-to-text launcher — the (llamacpp, audio.transcription) slot.

Fast-suite coverage (no binary, no weights): the weights-file resolution rules (a server takes a
FILE; a catalog install lands a directory), the not-ready posture when the binary is absent, and
the spawn command shape (the OpenAI inference path + ffmpeg conversion for browser recordings).
The REAL server run lives in test_integration_whisper.py.
"""

from __future__ import annotations

import pytest
from theygent_inference_plane.launcher import WhisperCppLauncher, locate_whisper_model
from theygent_ir import ManagedBinding


def _binding(model: str, source: str = "local-path") -> ManagedBinding:
    return ManagedBinding(
        binding="llamacpp", source=source, model=model, modality="audio.transcription"
    )


# ── weights-file resolution ──────────────────────────────────────────────────


def test_locate_direct_file(tmp_path) -> None:
    f = tmp_path / "ggml-base.en.bin"
    f.write_bytes(b"x")
    assert locate_whisper_model(_binding(str(f))) == str(f)


def test_locate_single_file_in_install_dir(tmp_path) -> None:
    f = tmp_path / "whisper-large-v3-turbo-Q8_0.gguf"
    f.write_bytes(b"x")
    assert locate_whisper_model(_binding(str(tmp_path))) == str(f)


def test_locate_prefers_the_ggml_native_form(tmp_path) -> None:
    # whisper-server reads whisper's native ggml-*.bin; a llama-style GGUF conversion fails its
    # magic check — when both exist, the native form wins.
    ggml = tmp_path / "ggml-base.en.bin"
    ggml.write_bytes(b"x")
    (tmp_path / "whisper-base-en.gguf").write_bytes(b"x")
    assert locate_whisper_model(_binding(str(tmp_path))) == str(ggml)


def test_locate_empty_dir_raises_loudly(tmp_path) -> None:
    with pytest.raises(RuntimeError, match="no whisper weights"):
        locate_whisper_model(_binding(str(tmp_path)))


def test_locate_ambiguous_raises_loudly(tmp_path) -> None:
    (tmp_path / "a.gguf").write_bytes(b"x")
    (tmp_path / "b.gguf").write_bytes(b"x")
    with pytest.raises(RuntimeError, match="ambiguous"):
        locate_whisper_model(_binding(str(tmp_path)))


# ── binary resolution / readiness ────────────────────────────────────────────


def test_missing_binary_is_not_ready_never_raises(monkeypatch) -> None:
    monkeypatch.setenv("PATH", "/nonexistent")
    monkeypatch.delenv(WhisperCppLauncher.ENV_VAR, raising=False)
    launcher = WhisperCppLauncher()
    assert launcher.ready is False
    assert "whisper-server" in (launcher.not_ready_reason or "")


def test_explicit_binary_path(monkeypatch, tmp_path) -> None:
    fake = tmp_path / "whisper-server"
    fake.write_text("#!/bin/sh\n")
    fake.chmod(0o755)
    launcher = WhisperCppLauncher(str(fake))
    assert launcher.ready is True


# ── the spawn command shape ──────────────────────────────────────────────────


def test_command_serves_the_openai_path(monkeypatch, tmp_path) -> None:
    fake = tmp_path / "whisper-server"
    fake.write_text("#!/bin/sh\n")
    fake.chmod(0o755)
    model = tmp_path / "ggml-tiny.bin"
    model.write_bytes(b"x")
    launcher = WhisperCppLauncher(str(fake))
    cmd = launcher._build_command(_binding(str(model)), 12345)
    joined = " ".join(cmd)
    # The gateway's api_base ends in /v1 and LiteLLM appends /audio/transcriptions — the server
    # must serve exactly that route.
    assert "--inference-path /v1/audio/transcriptions" in joined
    assert f"-m {model}" in joined
    assert "--port 12345" in joined


def test_command_converts_only_when_ffmpeg_exists(monkeypatch, tmp_path) -> None:
    fake = tmp_path / "whisper-server"
    fake.write_text("#!/bin/sh\n")
    fake.chmod(0o755)
    model = tmp_path / "ggml-tiny.bin"
    model.write_bytes(b"x")
    launcher = WhisperCppLauncher(str(fake))

    monkeypatch.setattr("theygent_inference_plane.launcher.shutil.which", lambda _: None)
    assert "--convert" not in launcher._build_command(_binding(str(model)), 1)

    monkeypatch.setattr(
        "theygent_inference_plane.launcher.shutil.which", lambda _: "/usr/bin/ffmpeg"
    )
    assert "--convert" in launcher._build_command(_binding(str(model)), 1)
