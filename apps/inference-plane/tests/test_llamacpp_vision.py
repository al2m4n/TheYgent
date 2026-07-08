"""The (llamacpp, vision) slot — llama-server + a multimodal projector.

Vision is the same llama-server as chat with an mmproj loaded (image_url on /v1/chat/completions),
so it shares the LlamaCppLauncher instance. Fast-suite coverage: the projector is located beside
the weights (or taken from params), the command carries the mmproj flag, and a projector-less
binding degrades to --mmproj-auto rather than a silent text run. The real run is env-gated.
"""

from __future__ import annotations

from theygent_inference_plane.launcher import LlamaCppLauncher, locate_llama_mmproj
from theygent_ir import ManagedBinding


def _binding(
    model: str, *, source: str = "local-path", params: dict | None = None
) -> ManagedBinding:
    return ManagedBinding(
        binding="llamacpp", source=source, model=model, modality="vision", params=params or {}
    )


def test_locate_mmproj_beside_weights(tmp_path) -> None:
    (tmp_path / "model-Q4.gguf").write_bytes(b"x")
    proj = tmp_path / "mmproj-model-f16.gguf"
    proj.write_bytes(b"x")
    assert locate_llama_mmproj(_binding(str(tmp_path / "model-Q4.gguf"))) == str(proj)


def test_locate_mmproj_absent_is_none(tmp_path) -> None:
    (tmp_path / "model-Q4.gguf").write_bytes(b"x")
    assert locate_llama_mmproj(_binding(str(tmp_path / "model-Q4.gguf"))) is None


def test_vision_command_carries_located_mmproj(tmp_path) -> None:
    (tmp_path / "m.gguf").write_bytes(b"x")
    proj = tmp_path / "mmproj-m.gguf"
    proj.write_bytes(b"x")
    launcher = LlamaCppLauncher.__new__(LlamaCppLauncher)  # skip binary resolution
    launcher._binary = "/usr/bin/llama-server"
    cmd = " ".join(launcher._build_command(_binding(str(tmp_path / "m.gguf")), 9000))
    assert f"--mmproj {proj}" in cmd
    assert "-m " in cmd


def test_vision_command_explicit_params_mmproj_wins(tmp_path) -> None:
    (tmp_path / "m.gguf").write_bytes(b"x")
    (tmp_path / "mmproj-m.gguf").write_bytes(b"x")  # a beside-file exists...
    launcher = LlamaCppLauncher.__new__(LlamaCppLauncher)
    launcher._binary = "/usr/bin/llama-server"
    b = _binding(str(tmp_path / "m.gguf"), params={"mmproj": "/explicit/proj.gguf"})
    cmd = " ".join(launcher._build_command(b, 9000))
    assert "--mmproj /explicit/proj.gguf" in cmd  # ...but the explicit param wins


def test_vision_command_no_projector_falls_back_to_auto(tmp_path) -> None:
    (tmp_path / "m.gguf").write_bytes(b"x")  # no mmproj beside it
    launcher = LlamaCppLauncher.__new__(LlamaCppLauncher)
    launcher._binary = "/usr/bin/llama-server"
    cmd = " ".join(launcher._build_command(_binding(str(tmp_path / "m.gguf")), 9000))
    assert "--mmproj-auto" in cmd
