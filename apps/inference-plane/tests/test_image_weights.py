"""The guard that keeps a *piece* of a diffusion model from being registered as a model.

Text-to-image repos publish denoisers on their own — same extension, same pipeline tag, same
multi-gigabyte weight as a whole model — and the CLI that loads one reports it as a failure to
detect the file's version, naming none of the parts that are absent. These tests pin the header
read that tells the two apart, and the three places that act on it: the catalog (before the
download), registration (before the binding is stored), and the launch path (for the bindings
that predate the guard).

GGUF fixtures are built byte by byte here rather than checked in: a real checkpoint is gigabytes,
and the header is the only part any of this reads.
"""

from __future__ import annotations

import struct
from pathlib import Path

import pytest
from _fake_upstream import FakeUpstreamLauncher
from fastapi.testclient import TestClient
from theygent_inference_plane.app import create_app
from theygent_inference_plane.launcher import locate_image_model
from theygent_inference_plane.weights import (
    IncompleteWeights,
    diffusion_defect,
    is_component_architecture,
    read_gguf_summary,
)
from theygent_ir import ManagedBinding

# Tensor sets standing in for the checkpoint families, trimmed to the sections that decide the
# answer. A complete checkpoint carries all three parts; the rest each miss something.
COMPLETE_SD = [
    "model.diffusion_model.input_blocks.0.0.weight",
    "first_stage_model.encoder.conv_in.weight",
    "cond_stage_model.transformer.text_model.embeddings.weight",
]
COMPLETE_FLUX = [
    "model.diffusion_model.double_blocks.0.img_attn.qkv.weight",
    "first_stage_model.decoder.conv_out.weight",
    "text_encoders.t5xxl.encoder.block.0.layer.0.weight",
]
DENOISER_ONLY = [
    "blocks.0.attn.qknorm.knorm.scale",
    "blocks.0.mod.lin",
    "last.linear.weight",
]
LANGUAGE_MODEL = ["token_embd.weight", "blk.0.attn_q.weight", "output_norm.weight"]


def _string(text: str) -> bytes:
    raw = text.encode()
    return struct.pack("<Q", len(raw)) + raw


def build_gguf(
    tensor_names: list[str],
    *,
    architecture: str | None = None,
    version: int = 3,
    noisy_metadata: bool = True,
) -> bytes:
    """A GGUF header with the given tensors, and by default a spread of metadata value types the
    reader must step over to reach them (a real file's tokenizer arrays dwarf everything else)."""
    metadata = b""
    count = 0
    if architecture is not None:
        metadata += _string("general.architecture") + struct.pack("<I", 8) + _string(architecture)
        count += 1
    if noisy_metadata:
        metadata += _string("general.file_type") + struct.pack("<I", 4) + struct.pack("<I", 10)
        metadata += _string("general.alignment") + struct.pack("<I", 10) + struct.pack("<Q", 32)
        metadata += _string("some.flag") + struct.pack("<I", 7) + struct.pack("<?", True)
        # an array of fixed-width scalars (skipped in one jump) …
        metadata += (
            _string("tokenizer.ggml.token_type")
            + struct.pack("<I", 9)
            + struct.pack("<I", 5)
            + struct.pack("<Q", 3)
            + struct.pack("<iii", 1, 2, 3)
        )
        # … and an array of strings (skipped element by element)
        metadata += (
            _string("tokenizer.ggml.tokens")
            + struct.pack("<I", 9)
            + struct.pack("<I", 8)
            + struct.pack("<Q", 2)
            + _string("<s>")
            + _string("</s>")
        )
        count += 5
    header = (
        b"GGUF"
        + struct.pack("<I", version)
        + struct.pack("<Q", len(tensor_names))
        + struct.pack("<Q", count)
        + metadata
    )
    for name in tensor_names:
        header += (
            _string(name)
            + struct.pack("<I", 2)  # dimension count
            + struct.pack("<QQ", 16, 16)  # extents
            + struct.pack("<I", 0)  # ggml type
            + struct.pack("<Q", 0)  # offset into the tensor-data block
        )
    return header


@pytest.fixture
def gguf(tmp_path: Path):
    def write(name: str, *args, **kwargs) -> str:
        path = tmp_path / name
        path.write_bytes(build_gguf(*args, **kwargs))
        return str(path)

    return write


# ── reading the header ───────────────────────────────────────────────────────


def test_reader_reaches_the_tensors_past_every_metadata_type(gguf) -> None:
    summary = read_gguf_summary(gguf("m.gguf", DENOISER_ONLY, architecture="qwen_image"))
    assert summary is not None
    assert summary.architecture == "qwen_image"
    assert summary.tensor_prefixes == frozenset({"blocks", "last"})


def test_complete_checkpoints_carry_a_vae(gguf) -> None:
    for name, tensors in (("sd.gguf", COMPLETE_SD), ("flux.gguf", COMPLETE_FLUX)):
        assert diffusion_defect(gguf(name, tensors)) is None


def test_denoiser_only_names_both_missing_parts(gguf) -> None:
    defect = diffusion_defect(gguf("krea.gguf", DENOISER_ONLY, architecture="qwen_image"))
    assert defect is not None
    assert "qwen_image" in defect
    assert "a VAE and a text encoder" in defect


def test_a_bundled_text_encoder_narrows_the_message(gguf) -> None:
    # Missing only the VAE: the message must not send the user after a text encoder they have.
    tensors = [*DENOISER_ONLY, "text_encoders.t5xxl.encoder.block.0.layer.0.weight"]
    defect = diffusion_defect(gguf("half.gguf", tensors, architecture="flux"))
    assert defect is not None
    assert "missing a VAE." in defect
    assert "text encoder" not in defect.split("missing a VAE.")[0]


def test_an_sdxl_text_encoder_counts_as_one(gguf) -> None:
    # SDXL nests its encoder two segments deep (`conditioner.embedders.*`) where the other
    # families nest it one. A section is matched by its leading segment, so the deeper name must
    # not read as "no text encoder here" and send the user after a part the file already has.
    tensors = [*DENOISER_ONLY, "conditioner.embedders.0.transformer.text_model.embeddings.weight"]
    defect = diffusion_defect(gguf("sdxl.gguf", tensors))
    assert defect is not None
    assert "missing a VAE." in defect
    assert "text encoder" not in defect.split("missing a VAE.")[0]


def test_a_language_model_gets_the_other_correction(gguf) -> None:
    # Registering a chat model as an image model needs the opposite fix from a missing VAE, so
    # the two must not share a message.
    defect = diffusion_defect(gguf("chat.gguf", LANGUAGE_MODEL, architecture="qwen2"))
    assert defect is not None
    assert "is a language model" in defect
    # It must send the user to a different model, never off hunting for the missing halves of
    # a diffusion model this file was never part of.
    assert "holds a diffusion model's denoiser only" not in defect
    assert "install a complete text-to-image checkpoint" not in defect


@pytest.mark.parametrize(
    "payload",
    [
        b"",
        b"not a gguf at all",
        b"GGUF" + struct.pack("<I", 3) + b"\x00\x00",  # truncated before the counts
        b"GGUF" + struct.pack("<I", 1) + struct.pack("<QQ", 1, 0),  # v1: 32-bit counts
        b"GGUF" + struct.pack("<I", 3) + struct.pack("<QQ", 2**40, 0),  # absurd tensor count
    ],
    ids=["empty", "not-gguf", "truncated", "v1", "absurd-count"],
)
def test_unreadable_files_are_never_called_defective(tmp_path: Path, payload: bytes) -> None:
    # The guard may only ever add precision: anything this reader cannot classify has to reach
    # the engine exactly as it did before, so "cannot tell" must never become "refused".
    path = tmp_path / "weird.gguf"
    path.write_bytes(payload)
    assert read_gguf_summary(str(path)) is None
    assert diffusion_defect(str(path)) is None


def test_a_missing_file_is_not_a_defect(tmp_path: Path) -> None:
    assert diffusion_defect(str(tmp_path / "nope.gguf")) is None


def test_nested_metadata_cannot_exhaust_the_stack(tmp_path: Path) -> None:
    # Each nesting level costs 12 header bytes, so a corrupt or hostile file can claim far more
    # of them than the interpreter has frames. The reader owes its callers a verdict or a
    # "cannot tell" — never an exception the registration route turns into a 500.
    value = struct.pack("<I", 9)  # the key's value type: array
    for _ in range(5000):
        value += struct.pack("<I", 9) + struct.pack("<Q", 1)  # …of arrays, one element each
    value += struct.pack("<I", 4) + struct.pack("<Q", 1) + struct.pack("<I", 7)  # innermost
    path = tmp_path / "nested.gguf"
    path.write_bytes(
        b"GGUF" + struct.pack("<I", 3) + struct.pack("<QQ", 0, 1) + _string("x") + value
    )
    assert read_gguf_summary(str(path)) is None
    assert diffusion_defect(str(path)) is None


def test_a_header_larger_than_the_first_read_is_still_read(tmp_path: Path) -> None:
    # Headers are read in growing windows so a registration never pages in megabytes it does not
    # need; a checkpoint whose tensor table outgrows the first window must still be classified.
    padding = [f"model.diffusion_model.pad.{i}.weight" for i in range(30_000)]
    path = tmp_path / "big.gguf"
    path.write_bytes(build_gguf([*COMPLETE_SD, *padding]))
    assert path.stat().st_size > 1024 * 1024
    summary = read_gguf_summary(str(path))
    assert summary is not None and summary.has_section("first_stage_model.")
    assert diffusion_defect(str(path)) is None


@pytest.mark.parametrize(
    ("architecture", "expected"),
    [("qwen_image", True), ("FLUX", True), ("  sd3 ", True), ("qwen2", False), (None, False)],
)
def test_component_architectures_are_matched_loosely(architecture, expected) -> None:
    assert is_component_architecture(architecture) is expected


# ── the launch path ──────────────────────────────────────────────────────────


def _binding(model: str, **kwargs) -> ManagedBinding:
    return ManagedBinding.model_validate(
        {
            "binding": "llamacpp",
            "source": "local-path",
            "model": model,
            "modality": "images.generation",
            **kwargs,
        }
    )


def test_launch_refuses_a_denoiser_before_reaching_the_cli(gguf) -> None:
    path = gguf("krea.gguf", DENOISER_ONLY, architecture="qwen_image")
    with pytest.raises(IncompleteWeights, match="denoiser only"):
        locate_image_model(_binding(path), "sdcpp")


def test_launch_resolves_a_complete_checkpoint_in_a_directory(tmp_path: Path) -> None:
    # The catalog lands a directory; the single weights file inside it is checked, not just found.
    (tmp_path / "sd.gguf").write_bytes(build_gguf(COMPLETE_SD))
    assert locate_image_model(_binding(str(tmp_path)), "sdcpp") == str(tmp_path / "sd.gguf")


def test_launch_refuses_a_denoiser_found_in_a_directory(tmp_path: Path) -> None:
    (tmp_path / "krea.gguf").write_bytes(build_gguf(DENOISER_ONLY, architecture="qwen_image"))
    with pytest.raises(IncompleteWeights):
        locate_image_model(_binding(str(tmp_path)), "sdcpp")


def test_the_check_is_scoped_to_the_gguf_engine(gguf) -> None:
    # mflux is handed repo names and safetensors directories it resolves itself — a GGUF header
    # check has nothing to say about those.
    path = gguf("krea.gguf", DENOISER_ONLY, architecture="qwen_image")
    assert locate_image_model(_binding(path), "mflux") == path


def test_a_remote_reference_rides_through_unchecked() -> None:
    binding = _binding("black-forest-labs/FLUX.1-schnell", source="hf")
    assert locate_image_model(binding, "sdcpp") == "black-forest-labs/FLUX.1-schnell"


# ── registration ─────────────────────────────────────────────────────────────


def test_registration_refuses_a_denoiser(client: TestClient, gguf) -> None:
    path = gguf("krea.gguf", DENOISER_ONLY, architecture="qwen_image")
    response = client.put(
        "/admin/models/painter",
        json={
            "binding": "llamacpp",
            "source": "local-path",
            "model": path,
            "modality": "images.generation",
        },
    )
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "incomplete_weights"
    assert "a VAE and a text encoder" in response.json()["error"]["message"]
    assert client.get("/admin/models/painter").status_code == 404


def test_registration_accepts_a_complete_checkpoint(client: TestClient, gguf) -> None:
    path = gguf("sd.gguf", COMPLETE_SD)
    response = client.put(
        "/admin/models/painter",
        json={
            "binding": "llamacpp",
            "source": "local-path",
            "model": path,
            "modality": "images.generation",
        },
    )
    assert response.status_code == 200


def test_the_guard_only_speaks_for_image_bindings(client: TestClient, gguf) -> None:
    # The same file under a text modality is not this guard's business — only the modality that
    # would hand it to a diffusion CLI is checked.
    path = gguf("krea.gguf", DENOISER_ONLY, architecture="qwen_image")
    body = {"binding": "llamacpp", "source": "local-path", "model": path, "modality": "chat"}
    assert client.put("/admin/models/text", json=body).status_code == 200


def test_registration_still_allows_weights_that_are_not_there_yet(client: TestClient) -> None:
    # Narrow on purpose: only a file that IS readable and IS provably a fragment is refused, so a
    # binding registered against a path that is still downloading is left alone.
    response = client.put(
        "/admin/models/painter",
        json={
            "binding": "llamacpp",
            "source": "local-path",
            "model": "/nowhere/at/all.gguf",
            "modality": "images.generation",
        },
    )
    assert response.status_code == 200


# ── the data plane, for bindings registered before the guard existed ──────────


class _IncompleteLauncher(FakeUpstreamLauncher):
    async def launch(self, binding):
        raise IncompleteWeights("krea.gguf holds a diffusion model's denoiser only")


def test_generation_reports_incomplete_weights_not_a_bad_gateway(clock) -> None:
    # A registry written before this guard still holds such bindings (nothing rewrites them), so
    # the first generation must explain itself rather than relaying the CLI's stderr as a 502.
    app = create_app(launcher=_IncompleteLauncher(), clock=clock, enable_reaper=False)
    with TestClient(app) as client:
        client.put(
            "/admin/models/painter",
            json={
                "binding": "llamacpp",
                "source": "hf",
                "model": "vantagewithai/Krea-2-Turbo-GGUF",
                "modality": "images.generation",
            },
        )
        response = client.post(
            "/v1/images/generations", json={"model": "painter", "prompt": "a cat"}
        )
    assert response.status_code == 503
    assert response.json()["error"]["code"] == "incomplete_weights"
    assert "denoiser only" in response.json()["error"]["message"]
