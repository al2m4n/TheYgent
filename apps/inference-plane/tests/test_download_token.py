"""Gated-download auth: the reserved HF_TOKEN credential reaches the hub calls.

The token resolves locally (credential store first, then the process env — the same
order as `secret://NAME` refs), rides into hf_hub_download/snapshot_download as
``token=``, and is None when absent (huggingface_hub's own behavior unchanged). The
value must never surface in logs or job views.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

import pytest
from _fake_upstream import FakeUpstreamLauncher
from theygent_inference_plane import downloader as downloader_module
from theygent_inference_plane.app import create_app
from theygent_inference_plane.catalog import EngineName, InstallPlan
from theygent_inference_plane.credentials import CredentialStore
from theygent_inference_plane.downloader import Downloader, _hf_fetch, _hf_token
from theygent_inference_plane.registry import Registry

SECRET = "hf_sekret-token-value"


def _plan(engine: EngineName = "llamacpp") -> InstallPlan:
    return InstallPlan(
        logical_id="gated-model",
        engine=engine,
        repo="org/gated",
        filename="weights.gguf" if engine == "llamacpp" else None,
        total_bytes=4,
    )


# ── resolution (store first, then env, else None) ────────────────────────────


def test_hf_token_prefers_store_then_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("HF_TOKEN", raising=False)
    store = CredentialStore()
    assert _hf_token(store) is None
    assert _hf_token(None) is None

    monkeypatch.setenv("HF_TOKEN", "env-token")
    assert _hf_token(store) == "env-token"  # store miss → env

    store.set("HF_TOKEN", "store-token")
    assert _hf_token(store) == "store-token"  # store shadows env


# ── the default fetch passes the token through ───────────────────────────────


def test_default_fetch_passes_token_when_credential_set(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.delenv("HF_TOKEN", raising=False)
    seen: dict[str, object] = {}

    def fake_hf_fetch(plan: InstallPlan, dest: Path, *, token: str | None = None) -> Path:
        seen["token"] = token
        return dest

    monkeypatch.setattr(downloader_module, "_hf_fetch", fake_hf_fetch)
    store = CredentialStore()
    store.set("HF_TOKEN", SECRET)
    dl = Downloader(Registry(), tmp_path, credentials=store)
    dl._fetch(_plan(), tmp_path)
    assert seen["token"] == SECRET

    # Resolution happens PER DOWNLOAD: deleting the credential un-authenticates the next
    # fetch without rebuilding the downloader.
    store.delete("HF_TOKEN")
    dl._fetch(_plan(), tmp_path)
    assert seen["token"] is None


def test_hf_fetch_forwards_token_to_the_hub_calls(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    calls: dict[str, object] = {}

    def fake_hf_hub_download(*, repo_id, filename, local_dir, token=None):
        calls["single"] = token
        out = Path(local_dir) / filename
        out.write_bytes(b"gguf")
        return str(out)

    def fake_snapshot_download(*, repo_id, local_dir, token=None):
        calls["snapshot"] = token
        return local_dir

    import huggingface_hub

    monkeypatch.setattr(huggingface_hub, "hf_hub_download", fake_hf_hub_download)
    monkeypatch.setattr(huggingface_hub, "snapshot_download", fake_snapshot_download)

    _hf_fetch(_plan("llamacpp"), tmp_path, token=SECRET)
    assert calls["single"] == SECRET
    _hf_fetch(_plan("mlx"), tmp_path, token=None)
    assert calls["snapshot"] is None  # absent token → hf_hub's own behavior unchanged


# ── the app wires its credential store into the downloader ───────────────────


def test_create_app_wires_credential_store_into_downloader() -> None:
    app = create_app(launcher=FakeUpstreamLauncher(), enable_reaper=False)
    assert app.state.downloader._credentials is app.state.credential_store


# ── the value never leaks into logs or job views ─────────────────────────────


async def test_token_never_in_logs_or_job_views(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    def fake_hf_fetch(plan: InstallPlan, dest: Path, *, token: str | None = None) -> Path:
        dest.mkdir(parents=True, exist_ok=True)
        out = dest / "weights.gguf"
        out.write_bytes(b"gguf")
        return out

    monkeypatch.setattr(downloader_module, "_hf_fetch", fake_hf_fetch)
    store = CredentialStore()
    store.set("HF_TOKEN", SECRET)
    dl = Downloader(Registry(), tmp_path, credentials=store, dir_size=lambda p: 4)
    with caplog.at_level(logging.DEBUG):
        job = dl.start(_plan())
        assert job.task is not None
        await job.task
    assert job.status == "done"
    assert SECRET not in caplog.text
    assert SECRET not in json.dumps(job.view())
