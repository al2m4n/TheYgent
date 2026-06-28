"""Fast suite for M16 — discovery + install (the HF model adapter, the seam, the install path).

Everything-real-except-the-Hub-and-the-weights: a ``FakeHfApi`` returns canned ``ModelInfo``-shaped
objects and a fake fetcher writes a stub file instead of downloading GB of weights. This proves the
contract M16 §5 asks for: normalization (raw → identical ``CatalogEntry``), the engine filter (never
surface an unrunnable model), the fit hint, install dispatch (right binding per engine), and the
download → register state machine — without touching the network or the filesystem cache.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest
from _fake_upstream import FakeUpstreamLauncher
from fastapi.testclient import TestClient
from theygent_inference.app import create_app
from theygent_inference.catalog import (
    CatalogQuery,
    HuggingFaceProvider,
    InstallPlan,
    fit_for,
)
from theygent_inference.downloader import Downloader

GB = 1_000_000_000


# ── a fake Hugging Face Hub (the shape huggingface_hub actually returns) ──────────


@dataclass
class FakeSibling:
    rfilename: str
    size: int | None = None


@dataclass
class FakeSafetensors:
    total: int


@dataclass
class FakeModelInfo:
    id: str
    downloads: int | None = None
    likes: int | None = None
    author: str | None = None
    pipeline_tag: str | None = None
    library_name: str | None = None
    tags: list[str] = field(default_factory=list)
    siblings: list[FakeSibling] = field(default_factory=list)
    gated: object = None  # "auto" | "manual" | False | None (HF's shape)
    safetensors: FakeSafetensors | None = None
    last_modified: object = None


class FakeHfApi:
    """Records each ``list_models`` call's ``filter`` + ``num_parameters`` (to assert the engine +
    size filters) and serves per-library fixtures + per-repo ``model_info``."""

    def __init__(
        self,
        by_library: dict[str, list[FakeModelInfo]] | None = None,
        by_repo: dict[str, FakeModelInfo] | None = None,
    ) -> None:
        self._by_library = by_library or {}
        self._by_repo = by_repo or {}
        self.list_filters: list[Any] = []
        self.num_params: list[Any] = []

    def list_models(
        self, *, filter=None, search=None, sort=None, limit=None, expand=None, num_parameters=None
    ):
        self.list_filters.append(filter)
        self.num_params.append(num_parameters)
        return list(self._by_library.get(filter, []))

    def model_info(self, repo_id, *, files_metadata=False):
        if repo_id in self._by_repo:
            return self._by_repo[repo_id]
        raise KeyError(repo_id)


def _qwen_gguf() -> FakeModelInfo:
    return FakeModelInfo(
        id="bartowski/Qwen2.5-7B-Instruct-GGUF",
        downloads=120_000,
        likes=300,
        author="bartowski",
        tags=["gguf", "license:apache-2.0"],
        gated=False,
        siblings=[
            FakeSibling("Qwen2.5-7B-Instruct-Q4_K_M.gguf", 4 * GB),
            FakeSibling("Qwen2.5-7B-Instruct-Q5_K_M.gguf", 6 * GB),
            FakeSibling("Qwen2.5-7B-Instruct-Q8_0.gguf", 8 * GB),
            FakeSibling("README.md", 2000),
        ],
    )


def _qwen_mlx() -> FakeModelInfo:
    return FakeModelInfo(
        id="mlx-community/Qwen2.5-0.5B-Instruct-4bit",
        downloads=50_000,
        likes=80,
        author="mlx-community",
        library_name="mlx",
        tags=["mlx", "license:apache-2.0"],
        gated="auto",  # a gated repo (needs an HF token)
        safetensors=FakeSafetensors(total=494_000_000),
        siblings=[
            FakeSibling("model.safetensors", 300_000_000),
            FakeSibling("config.json", 1000),
            FakeSibling("tokenizer.json", 2_000_000),
        ],
    )


# ── normalization ─────────────────────────────────────────────────────────────


def test_list_normalizes_to_catalog_entry() -> None:
    api = FakeHfApi(by_library={"gguf": [_qwen_gguf()], "mlx": [_qwen_mlx()]})
    p = HuggingFaceProvider(hf_api=api, ram_bytes=16 * GB)
    entries = p.list(CatalogQuery(engines=["mlx", "llamacpp"]))
    by_ref = {e.ref: e for e in entries}

    gguf = by_ref["bartowski/Qwen2.5-7B-Instruct-GGUF"]
    assert gguf.provider == "huggingface"
    assert gguf.title == "Qwen2.5-7B-Instruct-GGUF"
    assert gguf.badges == {"downloads": 120_000, "likes": 300}
    assert gguf.engines == ["llamacpp"]
    assert gguf.sovereignty == "in-domain"
    assert gguf.variants == []  # list() never sizes; that's get()'s job

    mlx = by_ref["mlx-community/Qwen2.5-0.5B-Instruct-4bit"]
    assert mlx.engines == ["mlx"]


# ── engine filter (§6: never surface an unrunnable model) ────────────────────────


def test_engine_filter_excludes_unavailable_engines() -> None:
    api = FakeHfApi(by_library={"gguf": [_qwen_gguf()], "mlx": [_qwen_mlx()]})
    p = HuggingFaceProvider(hf_api=api, ram_bytes=16 * GB)

    only_mlx = p.list(CatalogQuery(engines=["mlx"]))
    assert [e.ref for e in only_mlx] == ["mlx-community/Qwen2.5-0.5B-Instruct-4bit"]
    # The gguf library was never even queried — we don't list what we can't run.
    assert api.list_filters == ["mlx"]


def test_no_ready_engine_lists_nothing() -> None:
    api = FakeHfApi(by_library={"gguf": [_qwen_gguf()]})
    p = HuggingFaceProvider(hf_api=api, ram_bytes=16 * GB)
    assert p.list(CatalogQuery(engines=[])) == []
    assert api.list_filters == []  # no query issued at all


# ── get(): variants + fit (the LM Studio picker) ─────────────────────────────────


def test_get_builds_gguf_variants_with_fit() -> None:
    api = FakeHfApi(by_repo={"bartowski/Qwen2.5-7B-Instruct-GGUF": _qwen_gguf()})
    p = HuggingFaceProvider(hf_api=api, ram_bytes=8 * GB)  # an 8GB machine → all three bands appear
    entry = p.get("bartowski/Qwen2.5-7B-Instruct-GGUF", CatalogQuery(engines=["llamacpp"]))

    labels = {v.label: v for v in entry.variants}
    assert set(labels) == {"Q4_K_M", "Q5_K_M", "Q8_0"}  # the README is not a variant
    assert labels["Q4_K_M"].engine == "llamacpp"
    assert labels["Q4_K_M"].filename == "Qwen2.5-7B-Instruct-Q4_K_M.gguf"
    assert labels["Q4_K_M"].fit == "fits"  # 4GB on 8GB
    assert labels["Q5_K_M"].fit == "tight"  # 6GB on 8GB
    assert labels["Q8_0"].fit == "too-large"  # 8GB on 8GB
    # Recommended = the largest that *fits* → Q4_K_M (the others are tight / too-large).
    assert labels["Q4_K_M"].recommended is True
    assert labels["Q5_K_M"].recommended is False


def test_get_builds_single_mlx_variant() -> None:
    api = FakeHfApi(by_repo={"mlx-community/Qwen2.5-0.5B-Instruct-4bit": _qwen_mlx()})
    p = HuggingFaceProvider(hf_api=api, ram_bytes=16 * GB)
    entry = p.get("mlx-community/Qwen2.5-0.5B-Instruct-4bit", CatalogQuery(engines=["mlx"]))
    assert len(entry.variants) == 1
    v = entry.variants[0]
    assert v.engine == "mlx"
    assert v.id == ""  # the whole repo is the variant
    assert v.label == "4-bit"
    assert v.size_bytes == 300_000_000  # only weight files counted (config/tokenizer excluded)
    assert v.recommended is True


def test_fit_thresholds() -> None:
    assert fit_for(2 * GB, 16 * GB) == "fits"
    assert fit_for(10 * GB, 16 * GB) == "tight"
    assert fit_for(40 * GB, 16 * GB) == "too-large"
    assert fit_for(None, 16 * GB) == "unknown"
    assert fit_for(2 * GB, 0) == "unknown"


# ── install_plan (the right binding per engine) ──────────────────────────────────


def test_install_plan_llamacpp_targets_the_chosen_gguf() -> None:
    api = FakeHfApi(by_repo={"bartowski/Qwen2.5-7B-Instruct-GGUF": _qwen_gguf()})
    p = HuggingFaceProvider(hf_api=api)
    plan = p.install_plan(
        "bartowski/Qwen2.5-7B-Instruct-GGUF",
        "llamacpp",
        "Qwen2.5-7B-Instruct-Q4_K_M.gguf",
        "qwen-fast",
    )
    assert plan == InstallPlan(
        logical_id="qwen-fast",
        engine="llamacpp",
        repo="bartowski/Qwen2.5-7B-Instruct-GGUF",
        filename="Qwen2.5-7B-Instruct-Q4_K_M.gguf",
        total_bytes=4 * GB,
    )


def test_install_plan_mlx_snapshots_the_repo() -> None:
    api = FakeHfApi(by_repo={"mlx-community/Qwen2.5-0.5B-Instruct-4bit": _qwen_mlx()})
    p = HuggingFaceProvider(hf_api=api)
    plan = p.install_plan("mlx-community/Qwen2.5-0.5B-Instruct-4bit", "mlx", "", "qwen-mlx")
    assert plan.engine == "mlx"
    assert plan.filename is None  # whole repo, not one file
    assert plan.total_bytes == 300_000_000


# ── downloader: the download → register state machine ────────────────────────────


def test_downloader_completes_and_registers(tmp_path: Path) -> None:
    from theygent_inference.registry import Registry

    registry = Registry()
    written: dict[str, Path] = {}

    def fake_fetch(plan: InstallPlan, dest: Path) -> Path:
        dest.mkdir(parents=True, exist_ok=True)
        target = dest / (plan.filename or "snapshot")
        target.write_bytes(b"weights")
        written[plan.logical_id] = target
        return target

    dl = Downloader(registry, tmp_path, fetch=fake_fetch, dir_size=lambda p: 7)

    import anyio

    async def _drive() -> dict[str, Any]:
        plan = InstallPlan(
            logical_id="local-qwen", engine="mlx", repo="mlx-community/x", total_bytes=7
        )
        job = dl.start(plan)
        for _ in range(200):
            if job.status in ("done", "error"):
                break
            await anyio.sleep(0.01)
        return job.view()

    view = anyio.run(_drive)
    assert view["status"] == "done", view
    assert view["doneBytes"] == 7
    # The registration landed in the LOCAL registry — usable on /v1/* immediately.
    binding = registry.require("local-qwen")
    assert binding.binding == "mlx"
    assert binding.source == "local-path"
    assert binding.model == str(written["local-qwen"])


def test_downloader_reports_fetch_failure(tmp_path: Path) -> None:
    from theygent_inference.registry import Registry

    registry = Registry()

    def boom(plan: InstallPlan, dest: Path) -> Path:
        raise RuntimeError("network down")

    dl = Downloader(registry, tmp_path, fetch=boom, dir_size=lambda p: 0)

    import anyio

    async def _drive() -> dict[str, Any]:
        job = dl.start(InstallPlan(logical_id="x", engine="mlx", repo="r"))
        for _ in range(200):
            if job.status in ("done", "error"):
                break
            await anyio.sleep(0.01)
        return job.view()

    view = anyio.run(_drive)
    assert view["status"] == "error"
    assert "network down" in view["error"]
    assert registry.get("x") is None  # a failed download registers nothing


# ── route wiring (the management-plane surface) ──────────────────────────────────


def test_catalog_routes_list_get_and_install(tmp_path: Path) -> None:
    api = FakeHfApi(
        by_library={"gguf": [_qwen_gguf()], "mlx": [_qwen_mlx()]},
        by_repo={
            "bartowski/Qwen2.5-7B-Instruct-GGUF": _qwen_gguf(),
            "mlx-community/Qwen2.5-0.5B-Instruct-4bit": _qwen_mlx(),
        },
    )
    catalog = HuggingFaceProvider(hf_api=api, ram_bytes=16 * GB)

    def fake_fetch(plan: InstallPlan, dest: Path) -> Path:
        dest.mkdir(parents=True, exist_ok=True)
        f = dest / (plan.filename or "snapshot")
        f.write_bytes(b"w")
        return f

    # Let the app build a downloader against ITS own registry, then repoint the fetch/size seams at
    # our fakes (so the install lands in the registry the /admin/models endpoint reads).
    app = create_app(
        launcher=FakeUpstreamLauncher(),
        enable_reaper=False,
        catalog_provider=catalog,
        downloader=None,  # let the app build a downloader against ITS registry
        model_dir=tmp_path,
    )
    # Re-point the app's downloader at our fake fetch (keep its registry).
    app.state.downloader._fetch = fake_fetch  # type: ignore[attr-defined]
    app.state.downloader._dir_size = lambda p: 1  # type: ignore[attr-defined]

    with TestClient(app) as c:
        # list — both engines ready (the fake launcher serves every binding) → both entries.
        r = c.get("/admin/catalog/models", params={"search": "qwen", "sort": "downloads"})
        assert r.status_code == 200
        body = r.json()
        assert set(body["engines"]) == {"mlx", "llamacpp"}
        assert len(body["entries"]) == 2

        # get — variants + fit for the picker.
        r = c.get("/admin/catalog/models/bartowski/Qwen2.5-7B-Instruct-GGUF")
        assert r.status_code == 200
        assert {v["label"] for v in r.json()["variants"]} == {"Q4_K_M", "Q5_K_M", "Q8_0"}

        # install — 202 + a download job.
        r = c.post(
            "/admin/catalog/install",
            json={
                "repo": "mlx-community/Qwen2.5-0.5B-Instruct-4bit",
                "engine": "mlx",
                "variantId": "",
                "logicalId": "qwen-local",
            },
        )
        assert r.status_code == 202
        job_id = r.json()["id"]

        # poll until done — each GET spins the app loop, letting the install task resume.
        deadline = time.time() + 5
        status = "downloading"
        while time.time() < deadline:
            status = c.get(f"/admin/catalog/downloads/{job_id}").json()["status"]
            if status in ("done", "error"):
                break
        assert status == "done"

        # the installed model now shows under /admin/models — usable on /v1/*.
        ids = [m["logicalId"] for m in c.get("/admin/models").json()["models"]]
        assert "qwen-local" in ids


def test_install_rejects_bad_engine_and_duplicate_id(tmp_path: Path) -> None:
    catalog = HuggingFaceProvider(hf_api=FakeHfApi())
    app = create_app(
        launcher=FakeUpstreamLauncher(),
        enable_reaper=False,
        catalog_provider=catalog,
        model_dir=tmp_path,
    )
    with TestClient(app) as c:
        # openai-compatible is reachable, not installable.
        r = c.post(
            "/admin/catalog/install",
            json={"repo": "r", "engine": "openai-compatible", "logicalId": "x"},
        )
        assert r.status_code == 422
        assert r.json()["error"]["code"] == "invalid_install"

        # a logical id already in the registry is a 409.
        from _payloads import managed_payload

        c.put("/admin/models/taken", json=managed_payload())
        r = c.post(
            "/admin/catalog/install",
            json={"repo": "r", "engine": "mlx", "logicalId": "taken"},
        )
        assert r.status_code == 409
        assert r.json()["error"]["code"] == "logical_id_exists"


# ── UX enrichment: metadata badges, variant quality, filters, installed, cancel ──


def test_list_enriches_metadata() -> None:
    api = FakeHfApi(by_library={"gguf": [_qwen_gguf()], "mlx": [_qwen_mlx()]})
    p = HuggingFaceProvider(hf_api=api, ram_bytes=16 * GB)
    by_ref = {e.ref: e for e in p.list(CatalogQuery(engines=["mlx", "llamacpp"]))}

    gguf = by_ref["bartowski/Qwen2.5-7B-Instruct-GGUF"]
    assert gguf.params == "7B"  # parsed from the repo id (no safetensors on a GGUF repo)
    assert gguf.license == "apache-2.0"
    assert gguf.gated is False

    mlx = by_ref["mlx-community/Qwen2.5-0.5B-Instruct-4bit"]
    assert mlx.params == "494M"  # from safetensors total
    assert mlx.gated is True  # "auto" → gated


def test_get_sets_variant_quality_and_fit_reason() -> None:
    api = FakeHfApi(by_repo={"bartowski/Qwen2.5-7B-Instruct-GGUF": _qwen_gguf()})
    p = HuggingFaceProvider(hf_api=api, ram_bytes=16 * GB)
    entry = p.get("bartowski/Qwen2.5-7B-Instruct-GGUF", CatalogQuery(engines=["llamacpp"]))
    by_label = {v.label: v for v in entry.variants}
    assert by_label["Q4_K_M"].quality == "balanced"
    assert by_label["Q8_0"].quality == "max quality · large"
    assert "GB" in (by_label["Q4_K_M"].fit_reason or "")


def test_size_filter_forwards_num_parameters(tmp_path: Path) -> None:
    api = FakeHfApi(by_library={"mlx": [_qwen_mlx()], "gguf": [_qwen_gguf()]})
    app = create_app(
        launcher=FakeUpstreamLauncher(),
        enable_reaper=False,
        catalog_provider=HuggingFaceProvider(hf_api=api, ram_bytes=16 * GB),
        model_dir=tmp_path,
    )
    with TestClient(app) as c:
        assert c.get("/admin/catalog/models", params={"size": "small"}).status_code == 200
    # the size bucket became an HF num_parameters range on the underlying call
    assert "max:3B" in api.num_params


def test_engine_override_narrows_within_ready(tmp_path: Path) -> None:
    api = FakeHfApi(by_library={"mlx": [_qwen_mlx()], "gguf": [_qwen_gguf()]})
    app = create_app(
        launcher=FakeUpstreamLauncher(),
        enable_reaper=False,
        catalog_provider=HuggingFaceProvider(hf_api=api, ram_bytes=16 * GB),
        model_dir=tmp_path,
    )
    with TestClient(app) as c:
        # narrow to mlx only → only the mlx library is queried; `engines` still reports all ready.
        body = c.get("/admin/catalog/models", params={"engines": "mlx"}).json()
        assert [e["ref"] for e in body["entries"]] == ["mlx-community/Qwen2.5-0.5B-Instruct-4bit"]
        assert set(body["engines"]) == {"mlx", "llamacpp"}
        assert api.list_filters == ["mlx"]


def test_installed_cross_reference(tmp_path: Path) -> None:
    api = FakeHfApi(by_library={"mlx": [_qwen_mlx()], "gguf": [_qwen_gguf()]})
    app = create_app(
        launcher=FakeUpstreamLauncher(),
        enable_reaper=False,
        catalog_provider=HuggingFaceProvider(hf_api=api, ram_bytes=16 * GB),
        model_dir=tmp_path,
    )
    with TestClient(app) as c:
        # Register a model exactly as an install would: source=local-path with the sanitized repo
        # as a path segment under the model dir.
        installed_path = str(tmp_path / "mlx-community_Qwen2.5-0.5B-Instruct-4bit")
        c.put(
            "/admin/models/my-qwen",
            json={"binding": "mlx", "source": "local-path", "model": installed_path},
        )
        by_ref = {e["ref"]: e for e in c.get("/admin/catalog/models").json()["entries"]}
        mlx = by_ref["mlx-community/Qwen2.5-0.5B-Instruct-4bit"]
        assert mlx["installed"] is True
        assert mlx["installedAs"] == "my-qwen"
        # a different repo is not marked installed
        assert by_ref["bartowski/Qwen2.5-7B-Instruct-GGUF"]["installed"] is False


# ── M20: modality derived from pipeline_tag → recorded on plan / variant / binding ───


def _qwen_vl_mlx() -> FakeModelInfo:
    # A VLM repo: HF tags it image-text-to-text. Its modality is vision, whatever the engine.
    return FakeModelInfo(
        id="mlx-community/Qwen2.5-VL-7B-Instruct-4bit",
        library_name="mlx",
        pipeline_tag="image-text-to-text",
        tags=["mlx"],
        safetensors=FakeSafetensors(total=4 * GB),
        siblings=[FakeSibling("model.safetensors", 4 * GB), FakeSibling("config.json", 1000)],
    )


def _nomic_embed_gguf() -> FakeModelInfo:
    return FakeModelInfo(
        id="nomic-ai/nomic-embed-text-v1.5-GGUF",
        pipeline_tag="feature-extraction",
        tags=["gguf"],
        siblings=[FakeSibling("nomic-embed-text-v1.5.Q4_K_M.gguf", 100_000_000)],
    )


@pytest.mark.parametrize(
    ("pipeline_tag", "expected"),
    [
        # the five frozen Modality keys, each mapped from exactly one HF pipeline_tag
        ("image-text-to-text", "vision"),
        ("feature-extraction", "embeddings"),
        ("sentence-similarity", "embeddings"),
        ("automatic-speech-recognition", "audio.transcription"),
        ("text-to-speech", "audio.speech"),
        # the dominant case + the catch-all: text-generation / unknown / missing → chat
        ("text-generation", "chat"),
        ("text-generation-inference", "chat"),
        (None, "chat"),
        ("", "chat"),
        # RESERVED / non-runnable tasks must NOT be silently mis-mapped onto a non-chat key — they
        # fall to the chat catch-all (listing-level exclusion of these is a deferred refinement).
        ("text-to-image", "chat"),
        ("image-classification", "chat"),
    ],
)
def test_modality_for_pipeline_maps_only_the_frozen_vocab(
    pipeline_tag: str | None, expected: str
) -> None:
    from theygent_inference.catalog import _modality_for_pipeline

    mod = _modality_for_pipeline(pipeline_tag)
    assert mod == expected
    # whatever it returns is always one of the five frozen keys (never an out-of-vocab string)
    assert mod in {"chat", "vision", "embeddings", "audio.transcription", "audio.speech"}


def test_install_plan_derives_vision_modality_for_vlm() -> None:
    api = FakeHfApi(by_repo={"mlx-community/Qwen2.5-VL-7B-Instruct-4bit": _qwen_vl_mlx()})
    p = HuggingFaceProvider(hf_api=api)
    plan = p.install_plan("mlx-community/Qwen2.5-VL-7B-Instruct-4bit", "mlx", "", "qwen-vl")
    assert plan.modality == "vision"


def test_install_plan_derives_embeddings_modality() -> None:
    api = FakeHfApi(by_repo={"nomic-ai/nomic-embed-text-v1.5-GGUF": _nomic_embed_gguf()})
    p = HuggingFaceProvider(hf_api=api)
    plan = p.install_plan(
        "nomic-ai/nomic-embed-text-v1.5-GGUF",
        "llamacpp",
        "nomic-embed-text-v1.5.Q4_K_M.gguf",
        "embed",
    )
    assert plan.modality == "embeddings"


def test_install_plan_defaults_chat_for_text_models() -> None:
    # A plain chat repo (no special pipeline_tag) → chat, identical to pre-M20 behaviour.
    api = FakeHfApi(by_repo={"mlx-community/Qwen2.5-0.5B-Instruct-4bit": _qwen_mlx()})
    p = HuggingFaceProvider(hf_api=api)
    plan = p.install_plan("mlx-community/Qwen2.5-0.5B-Instruct-4bit", "mlx", "", "qwen")
    assert plan.modality == "chat"


def test_get_stamps_variant_modality_from_pipeline_tag() -> None:
    api = FakeHfApi(by_repo={"mlx-community/Qwen2.5-VL-7B-Instruct-4bit": _qwen_vl_mlx()})
    p = HuggingFaceProvider(hf_api=api, ram_bytes=32 * GB)
    entry = p.get("mlx-community/Qwen2.5-VL-7B-Instruct-4bit", CatalogQuery(engines=["mlx"]))
    assert entry.variants
    assert all(v.modality == "vision" for v in entry.variants)


def test_downloader_registers_modality(tmp_path: Path) -> None:
    import anyio
    from theygent_inference.registry import Registry

    registry = Registry()

    def fake_fetch(plan: InstallPlan, dest: Path) -> Path:
        dest.mkdir(parents=True, exist_ok=True)
        f = dest / "snapshot"
        f.write_bytes(b"w")
        return f

    dl = Downloader(registry, tmp_path, fetch=fake_fetch, dir_size=lambda p: 1)

    async def _drive() -> str:
        job = dl.start(InstallPlan(logical_id="vlm", engine="mlx", repo="r", modality="vision"))
        for _ in range(200):
            if job.status in ("done", "error"):
                break
            await anyio.sleep(0.01)
        return job.status

    status = anyio.run(_drive)
    assert status == "done"
    # The installed VLM registers with modality=vision → the manager will spawn mlx_vlm.server.
    binding = registry.require("vlm")
    assert binding.binding == "mlx"
    assert binding.modality == "vision"


def test_cancel_download(tmp_path: Path) -> None:
    import anyio
    from theygent_inference.registry import Registry

    registry = Registry()

    def slow_fetch(plan: InstallPlan, dest: Path) -> Path:
        time.sleep(0.5)  # still "downloading" when we cancel
        dest.mkdir(parents=True, exist_ok=True)
        f = dest / "snapshot"
        f.write_bytes(b"w")
        return f

    dl = Downloader(registry, tmp_path, fetch=slow_fetch, dir_size=lambda p: 0)

    async def _drive() -> dict[str, Any]:
        job = dl.start(InstallPlan(logical_id="cancel-me", engine="mlx", repo="r"))
        await anyio.sleep(0.05)
        dl.cancel(job.id)
        await anyio.sleep(0.1)
        return job.view()

    view = anyio.run(_drive)
    assert view["status"] == "cancelled"
    assert registry.get("cancel-me") is None  # a cancelled install registers nothing
