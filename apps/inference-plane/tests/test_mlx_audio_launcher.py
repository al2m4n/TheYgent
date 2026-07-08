"""The mlx-audio text-to-speech launcher — the (mlx, audio.speech) slot.

Fast-suite coverage (no binary, no weights): the not-ready posture when the server entry point is
absent, the spawn command shape (model-agnostic: the server loads whatever `model` each request
names, which is the registered binding.model the gateway forwards), and the declared capability.
The REAL server run lives in test_integration_mlx_audio.py.
"""

from __future__ import annotations

from theygent_inference_plane.launcher import MlxAudioLauncher
from theygent_ir import ManagedBinding


def _binding() -> ManagedBinding:
    return ManagedBinding(
        binding="mlx", source="hf", model="org/some-tts-model", modality="audio.speech"
    )


def test_missing_entry_point_is_not_ready_never_raises(monkeypatch) -> None:
    monkeypatch.setenv("PATH", "/nonexistent")
    monkeypatch.delenv(MlxAudioLauncher.ENV_VAR, raising=False)
    monkeypatch.setattr("theygent_inference_plane.binary._module_importable", lambda _: False)
    launcher = MlxAudioLauncher()
    assert launcher.ready is False
    assert "mlx_audio.server" in (launcher.not_ready_reason or "")


def test_explicit_binary_path(tmp_path) -> None:
    fake = tmp_path / "mlx_audio.server"
    fake.write_text("#!/bin/sh\n")
    fake.chmod(0o755)
    launcher = MlxAudioLauncher(str(fake))
    assert launcher.ready is True


def test_command_is_model_agnostic(tmp_path) -> None:
    # No model flag: the server loads per request from the `model` field; a spawn-time pin would
    # be a silent lie (the gateway forwards binding.model on every call).
    fake = tmp_path / "mlx_audio.server"
    fake.write_text("#!/bin/sh\n")
    fake.chmod(0o755)
    launcher = MlxAudioLauncher(str(fake))
    cmd = launcher._build_command(_binding(), 23456)
    assert "--port 23456" in " ".join(cmd)
    assert "org/some-tts-model" not in " ".join(cmd)
