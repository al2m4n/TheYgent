"""Mixture-of-experts in the catalog: the browse-time ``moe`` hint, and installing with params.

The hint lets the hub browser filter to MoE models and offer expert streaming for an MLX one that
does not fit in memory; the install request's ``params`` carry ``streamExperts`` onto the binding
the finished download registers. Hugging Face's listing metadata has no MoE flag of its own, so
the hint is read from what it does carry (verified against real responses): expert-count config
keys, the model type and architectures, and the repo name.
"""

from __future__ import annotations

import time
from pathlib import Path

import pytest
from _fake_upstream import FakeUpstreamLauncher
from fastapi.testclient import TestClient
from test_catalog import GB, FakeHfApi, FakeModelInfo, FakeSafetensors, FakeSibling
from theygent_inference_plane.app import create_app
from theygent_inference_plane.capabilities import detect_moe
from theygent_inference_plane.catalog import CatalogQuery, HuggingFaceProvider, InstallPlan

# ── the detector ─────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "signals",
    [
        # Real Hugging Face `expand=config` shapes:
        {
            "name": "mlx-community/Qwen3.6-35B-A3B-4bit",
            "architectures": ["Qwen3_5MoeForConditionalGeneration"],
            "model_type": "qwen3_5_moe",
            "config_keys": ["architectures", "model_type", "quantization_config"],
        },
        {
            "name": "mlx-community/gpt-oss-20b-MXFP4-Q8",
            "architectures": ["GptOssForCausalLM"],
            "model_type": "gpt_oss",
            "config_keys": ["architectures", "model_type", "num_experts_per_tok"],
        },
        {"name": "unsloth/Qwen3-30B-A3B-GGUF", "config_keys": ["num_experts"]},
        # A GGUF architecture alone:
        {"name": "someone/model-GGUF", "architectures": ["deepseek2"]},
        {"name": "someone/model", "model_type": "mixtral"},
        {"name": "Jundot/DeepSeek-V4.1-Flash-oQ4e-mtp", "model_type": "deepseek_v41"},
        {"name": "someone/model", "architectures": ["DeepseekV3ForCausalLM"]},
        # The name alone:
        {"name": "mlx-community/gemma-4-26B-A4B-it-4bit"},
        {"name": "org/Qwen1.5-MoE-A2.7B-Chat"},
    ],
)
def test_moe_signals_are_recognized(signals: dict) -> None:
    assert detect_moe(**signals) is True


@pytest.mark.parametrize(
    "signals",
    [
        {
            "name": "mlx-community/Qwen3-0.6B-4bit",
            "architectures": ["Qwen3ForCausalLM"],
            "model_type": "qwen3",
            "config_keys": ["architectures", "model_type", "quantization_config"],
        },
        {"name": "bartowski/Qwen2.5-7B-Instruct-GGUF", "architectures": ["qwen2"]},
        {"name": "mlx-community/Qwen2.5-VL-3B-Instruct-4bit"},
        {"name": "meta-llama/Llama-3.1-8B-Instruct"},
        {
            "name": "mlx-community/DeepSeek-R1-Distill-Qwen-32B-4bit",
            "architectures": ["Qwen2ForCausalLM"],
            "model_type": "qwen2",
        },
        {"name": "mistralai/Mistral-7B-Instruct-v0.3"},
        {"name": "org/llama-3b-a"},
        {},
    ],
)
def test_dense_models_are_not_moe(signals: dict) -> None:
    assert detect_moe(**signals) is False


# ── the hint on catalog entries ──────────────────────────────────────────────


def _moe_mlx() -> FakeModelInfo:
    return FakeModelInfo(
        id="mlx-community/Qwen3.6-35B-A3B-4bit",
        library_name="mlx",
        tags=["mlx", "qwen3_5_moe"],
        safetensors=FakeSafetensors(total=20 * GB),
        siblings=[
            FakeSibling("model-00001-of-00002.safetensors", 10 * GB),
            FakeSibling("model-00002-of-00002.safetensors", 10 * GB),
            FakeSibling("config.json", 1000),
        ],
        config={
            "architectures": ["Qwen3_5MoeForConditionalGeneration"],
            "model_type": "qwen3_5_moe",
        },
    )


def _dense_mlx() -> FakeModelInfo:
    return FakeModelInfo(
        id="mlx-community/Qwen3-0.6B-4bit",
        library_name="mlx",
        tags=["mlx"],
        siblings=[FakeSibling("model.safetensors", GB)],
        config={"architectures": ["Qwen3ForCausalLM"], "model_type": "qwen3"},
    )


def test_listing_marks_moe_models_and_serializes_the_hint() -> None:
    api = FakeHfApi(by_library={"mlx": [_moe_mlx(), _dense_mlx()]})
    entries = HuggingFaceProvider(hf_api=api, ram_bytes=16 * GB).list(CatalogQuery(engines=["mlx"]))
    by_ref = {e.ref: e for e in entries}
    assert by_ref["mlx-community/Qwen3.6-35B-A3B-4bit"].moe is True
    assert by_ref["mlx-community/Qwen3-0.6B-4bit"].moe is False
    assert by_ref["mlx-community/Qwen3.6-35B-A3B-4bit"].model_dump(by_alias=True)["moe"] is True


def test_the_name_alone_marks_moe_without_expanded_metadata() -> None:
    bare = FakeModelInfo(id="unsloth/Qwen3-30B-A3B-GGUF", tags=["gguf"])
    [entry] = HuggingFaceProvider(
        hf_api=FakeHfApi(by_library={"gguf": [bare]}), ram_bytes=16 * GB
    ).list(CatalogQuery(engines=["llamacpp"]))
    assert entry.moe is True


# ── installing with registration params ──────────────────────────────────────


def _app(tmp_path: Path):
    api = FakeHfApi(by_repo={"mlx-community/Qwen3.6-35B-A3B-4bit": _moe_mlx()})
    app = create_app(
        launcher=FakeUpstreamLauncher(),
        enable_reaper=False,
        catalog_provider=HuggingFaceProvider(hf_api=api, ram_bytes=16 * GB),
        model_dir=tmp_path,
    )

    def fake_fetch(plan: InstallPlan, dest: Path) -> Path:
        dest.mkdir(parents=True, exist_ok=True)
        (dest / "config.json").write_text("{}")
        return dest

    app.state.downloader._fetch = fake_fetch  # type: ignore[attr-defined]
    app.state.downloader._dir_size = lambda p: 1  # type: ignore[attr-defined]
    return app


def _install(c: TestClient, **extra: object):
    return c.post(
        "/admin/catalog/install",
        json={
            "repo": "mlx-community/Qwen3.6-35B-A3B-4bit",
            "engine": "mlx",
            "variantId": "",
            "logicalId": "big-moe",
            **extra,
        },
    )


def test_install_params_land_on_the_registered_binding(tmp_path: Path) -> None:
    with TestClient(_app(tmp_path)) as c:
        r = _install(c, params={"streamExperts": True, "expertCacheGb": 6})
        assert r.status_code == 202, r.text
        job_id = r.json()["id"]
        deadline = time.time() + 5
        status = "downloading"
        while time.time() < deadline and status not in ("done", "error"):
            status = c.get(f"/admin/catalog/downloads/{job_id}").json()["status"]
        assert status == "done"
        binding = c.get("/admin/models/big-moe").json()["binding"]
        assert binding["params"] == {"streamExperts": True, "expertCacheGb": 6}
        assert binding["binding"] == "mlx" and binding["source"] == "local-path"


def test_install_without_params_registers_none(tmp_path: Path) -> None:
    with TestClient(_app(tmp_path)) as c:
        job_id = _install(c).json()["id"]
        deadline = time.time() + 5
        while time.time() < deadline:
            if c.get(f"/admin/catalog/downloads/{job_id}").json()["status"] == "done":
                break
        assert c.get("/admin/models/big-moe").json()["binding"]["params"] == {}


def test_invalid_install_params_are_refused_before_any_download(tmp_path: Path) -> None:
    with TestClient(_app(tmp_path)) as c:
        r = _install(c, params={"streamExperts": True, "expertCacheGb": 0})
        assert r.status_code == 422, r.text
        assert r.json()["error"]["code"] == "invalid_binding"
        r = _install(c, params=["streamExperts"])
        assert r.status_code == 422
        assert r.json()["error"]["code"] == "invalid_install"
        assert c.get("/admin/catalog/downloads").json()["downloads"] == []
