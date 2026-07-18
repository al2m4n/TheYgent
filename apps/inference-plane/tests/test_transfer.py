"""Fast suite for registry export/import (the inference-plane transfer surface).

Metadata only, both directions. ``/admin/export`` carries every registration verbatim (runtime
``state`` stripped) plus the installs.json re-download provenance and credential NAMES;
``/admin/import`` registers reachable/hf bindings directly, re-downloads catalog-installed
weights in-plane through the existing download path — registering the EXPORTED binding with only
the weights path rewritten — and loudly flags machine-specific local paths it cannot re-fetch.
Credential VALUES must never appear in a bundle, either direction.
"""

from __future__ import annotations

import json
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from _fake_upstream import FakeUpstreamLauncher
from _payloads import managed_payload, reachable_payload
from fastapi.testclient import TestClient
from theygent_inference_plane.app import create_app
from theygent_inference_plane.catalog import HuggingFaceProvider, InstallPlan

GB = 1_000_000_000
SECRET = "sk-super-secret-value"

REPO = "bartowski/Qwen2.5-7B-Instruct-GGUF"
VARIANT = "Qwen2.5-7B-Instruct-Q4_K_M.gguf"


# ── a minimal fake Hub + fetch (mirrors test_catalog's fakes where install needs them) ──


@dataclass
class _Sibling:
    rfilename: str
    size: int | None = None


@dataclass
class _RepoInfo:
    id: str
    pipeline_tag: str | None = None
    siblings: list[_Sibling] = field(default_factory=list)
    tags: list[str] = field(default_factory=list)


class _FakeHub:
    def __init__(self, by_repo: dict[str, _RepoInfo] | None = None) -> None:
        self._by_repo = by_repo or {}

    def list_models(self, **_kwargs: Any) -> list[Any]:
        return []

    def model_info(self, repo_id: str, *, files_metadata: bool = False, expand: Any = None):
        if repo_id in self._by_repo:
            return self._by_repo[repo_id]
        raise KeyError(repo_id)


def _fake_fetch(plan: InstallPlan, dest: Path) -> Path:
    dest.mkdir(parents=True, exist_ok=True)
    f = dest / (plan.filename or "snapshot")
    f.write_bytes(b"w")
    return f


def _app(tmp_path: Path):
    """An app with a real on-disk state dir (registry/installs/credentials survive a "restart" —
    a second ``_app`` over the same ``tmp_path``) and the network seams faked out."""
    state_dir = tmp_path / "state"
    # The repo's pipeline_tag is None (a chat repo) ON PURPOSE: an import that re-derived
    # modality from the pipeline tag would register "chat" and the template-fidelity test
    # below would catch it.
    hub = _FakeHub({REPO: _RepoInfo(id=REPO, siblings=[_Sibling(VARIANT, 4)], tags=["gguf"])})
    app = create_app(
        launcher=FakeUpstreamLauncher(),
        enable_reaper=False,
        state_path=state_dir / "registry.json",
        catalog_provider=HuggingFaceProvider(hf_api=hub, ram_bytes=16 * GB),
        model_dir=state_dir / "models",
    )
    app.state.downloader._fetch = _fake_fetch  # type: ignore[attr-defined]
    app.state.downloader._dir_size = lambda p: 1  # type: ignore[attr-defined]
    return app


def _poll_done(c: TestClient, job_id: str) -> str:
    deadline = time.time() + 5
    status = "downloading"
    while time.time() < deadline:
        status = c.get(f"/admin/catalog/downloads/{job_id}").json()["status"]
        if status in ("done", "error", "cancelled"):
            break
    return status


# ── export: verbatim bindings, no runtime state, names-only credentials ─────────────


def test_export_strips_state_and_never_carries_credential_values(tmp_path: Path) -> None:
    with TestClient(_app(tmp_path)) as c:
        assert c.put("/admin/models/local-fast", json=managed_payload()).status_code == 200
        reachable = reachable_payload(
            base_url="https://api.example.com/v1", credential_ref="secret://API_KEY"
        )
        assert c.put("/admin/models/hosted", json=reachable).status_code == 200
        assert c.put("/admin/credentials/API_KEY", json={"value": SECRET}).status_code == 200

        r = c.get("/admin/export")
        assert r.status_code == 200
        bundle = r.json()
        assert bundle["formatVersion"] == 1
        by_id = {m["logicalId"]: m for m in bundle["models"]}
        assert set(by_id) == {"local-fast", "hosted"}
        # Every model row is exactly {logicalId, binding, install} — runtime state never rides.
        for m in by_id.values():
            assert set(m) == {"logicalId", "binding", "install"}
            assert m["install"] is None  # not catalog-installed → no provenance
        # The binding is the verbatim registry entry (what /admin/models shows, minus state).
        assert by_id["local-fast"]["binding"] == c.get("/admin/models/local-fast").json()["binding"]
        assert by_id["hosted"]["binding"]["credentialRef"] == "secret://API_KEY"
        # Credentials: NAMES only; the value appears nowhere in the payload.
        assert bundle["credentialNames"] == ["API_KEY"]
        assert SECRET not in r.text


# ── import: register / skip / restart survival ─────────────────────────────────────


def test_import_registers_skips_duplicates_and_survives_restart(tmp_path: Path) -> None:
    bundle = {
        "formatVersion": 1,
        "models": [
            {
                "logicalId": "hf-qwen",
                "binding": managed_payload(binding="mlx", source="hf", model="mlx-community/Q"),
                "install": None,  # exported bundles carry an explicit null here
            },
            {
                "logicalId": "hosted",
                "binding": reachable_payload(base_url="https://api.example.com/v1"),
            },
        ],
        "credentialNames": ["HF_TOKEN"],
    }
    with TestClient(_app(tmp_path)) as c:
        report = c.post("/admin/import", json=bundle).json()
        assert report["registered"] == ["hf-qwen", "hosted"]
        assert report["skipped"] == []
        assert report["downloads"] == []
        assert report["warnings"] == []
        # The bundle's credential names echo back so the UI can prompt for re-entry.
        assert report["credentialNames"] == ["HF_TOKEN"]
        # The hf-source binding registered verbatim — weights fetch lazily at first spawn.
        assert c.get("/admin/models/hf-qwen").json()["binding"]["source"] == "hf"

        # Idempotent: a second import of the same bundle skips everything.
        report2 = c.post("/admin/import", json=bundle).json()
        assert report2["registered"] == []
        assert report2["skipped"] == ["hf-qwen", "hosted"]

    # Restart: a brand-new app over the SAME state dir still has the imports.
    with TestClient(_app(tmp_path)) as c2:
        ids = {m["logicalId"] for m in c2.get("/admin/models").json()["models"]}
        assert {"hf-qwen", "hosted"} <= ids


def test_import_local_path_without_provenance_warns_loudly(tmp_path: Path) -> None:
    bundle = {
        "models": [
            # A local-path binding with no install metadata: the path is machine-specific.
            {"logicalId": "orphan", "binding": managed_payload()},
            # vllm is not catalog-installable — provenance or not, it registers verbatim.
            {
                "logicalId": "vllm-model",
                "binding": managed_payload(binding="vllm"),
                "install": {"repo": REPO, "variantId": VARIANT},
            },
        ]
    }
    with TestClient(_app(tmp_path)) as c:
        report = c.post("/admin/import", json=bundle).json()
        assert report["registered"] == ["orphan", "vllm-model"]
        assert report["downloads"] == []
        codes = {w["logicalId"]: w["code"] for w in report["warnings"]}
        assert codes == {"orphan": "weights_unavailable", "vllm-model": "weights_unavailable"}
        # Registered verbatim — loud warning, not a silent drop or a silent path rewrite.
        assert c.get("/admin/models/orphan").json()["binding"]["model"] == "fake-model"


def test_import_invalid_binding_is_a_warning_not_fatal(tmp_path: Path) -> None:
    bundle = {
        "models": [
            {"logicalId": "bad", "binding": {"binding": "not-an-engine"}},
            {"logicalId": "good", "binding": reachable_payload(base_url="https://x.example/v1")},
        ]
    }
    with TestClient(_app(tmp_path)) as c:
        report = c.post("/admin/import", json=bundle).json()
        assert report["registered"] == ["good"]
        [warning] = report["warnings"]
        assert warning["logicalId"] == "bad"
        assert warning["code"] == "invalid_binding"
        assert c.get("/admin/models/bad").status_code == 404  # nothing half-registered


def test_import_rejects_malformed_bundles(tmp_path: Path) -> None:
    with TestClient(_app(tmp_path)) as c:
        for body in (
            {"nope": []},  # missing models
            {"models": "x"},  # wrong type
            {"models": [], "extra": 1},  # unknown key — rejected loudly, never dropped
            {"formatVersion": 2, "models": []},  # a future format fails, never half-understood
            {"models": [{"logicalId": "x", "binding": {}, "install": {"variantId": "f"}}]},
        ):
            r = c.post("/admin/import", json=body)
            assert r.status_code == 400, body
            assert r.json()["error"]["code"] == "invalid_bundle"


# ── the installs.json provenance sidecar ───────────────────────────────────────────


def test_catalog_install_records_provenance_and_delete_removes_it(tmp_path: Path) -> None:
    state_dir = tmp_path / "state"
    with TestClient(_app(tmp_path)) as c:
        r = c.post(
            "/admin/catalog/install",
            json={"repo": REPO, "engine": "llamacpp", "variantId": VARIANT, "logicalId": "qwen"},
        )
        assert r.status_code == 202
        assert _poll_done(c, r.json()["id"]) == "done"

        # The sidecar landed next to registry.json, in the wire shape.
        installs = json.loads((state_dir / "installs.json").read_text())
        assert installs == {"qwen": {"repo": REPO, "variantId": VARIANT}}

        # Export carries the provenance for this catalog-installed model.
        [model] = c.get("/admin/export").json()["models"]
        assert model["install"] == {"repo": REPO, "variantId": VARIANT}

    # Restart survival: the provenance persists like the registration itself.
    with TestClient(_app(tmp_path)) as c2:
        [model] = c2.get("/admin/export").json()["models"]
        assert model["install"] == {"repo": REPO, "variantId": VARIANT}

        # DELETE drops the registration AND its provenance.
        assert c2.delete("/admin/models/qwen").status_code == 204
        assert c2.get("/admin/export").json()["models"] == []
        assert json.loads((state_dir / "installs.json").read_text()) == {}


def test_put_over_catalog_install_severs_provenance(tmp_path: Path) -> None:
    state_dir = tmp_path / "state"
    with TestClient(_app(tmp_path)) as c:
        r = c.post(
            "/admin/catalog/install",
            json={"repo": REPO, "engine": "llamacpp", "variantId": VARIANT, "logicalId": "qwen"},
        )
        assert r.status_code == 202
        assert _poll_done(c, r.json()["id"]) == "done"
        [model] = c.get("/admin/export").json()["models"]
        assert model["install"] == {"repo": REPO, "variantId": VARIANT}

        # A manual PUT over the installed id points the binding at OTHER weights; keeping the
        # sidecar record would make export pair the new binding with the old repo/variant and
        # an import would silently install the catalog weights instead of what the user runs.
        assert c.put("/admin/models/qwen", json=managed_payload()).status_code == 200
        [model] = c.get("/admin/export").json()["models"]
        assert model["install"] is None
        assert json.loads((state_dir / "installs.json").read_text()) == {}


def test_import_verbatim_clears_stale_provenance(tmp_path: Path) -> None:
    # A sidecar record for an id that is NOT registered (a wiped or hand-edited
    # registry.json): a verbatim import of that id must not resurrect the old repo/variant
    # as the new registration's provenance.
    state_dir = tmp_path / "state"
    state_dir.mkdir(parents=True)
    (state_dir / "installs.json").write_text(
        json.dumps({"hosted": {"repo": REPO, "variantId": VARIANT}})
    )
    bundle = {
        "models": [
            {"logicalId": "hosted", "binding": reachable_payload(base_url="https://x.example/v1")}
        ]
    }
    with TestClient(_app(tmp_path)) as c:
        report = c.post("/admin/import", json=bundle).json()
        assert report["registered"] == ["hosted"]
        [model] = c.get("/admin/export").json()["models"]
        assert model["install"] is None
        assert json.loads((state_dir / "installs.json").read_text()) == {}


def test_corrupt_installs_sidecar_is_treated_as_empty(tmp_path: Path) -> None:
    state_dir = tmp_path / "state"
    state_dir.mkdir(parents=True)
    (state_dir / "installs.json").write_text("{not json")
    with TestClient(_app(tmp_path)) as c:
        c.put("/admin/models/local-fast", json=managed_payload())
        [model] = c.get("/admin/export").json()["models"]
        assert model["install"] is None  # corrupt sidecar = empty index, never a crash


# ── import with provenance: re-download registers the EXPORTED binding ─────────────


def test_import_with_provenance_redownloads_and_registers_exported_binding(
    tmp_path: Path,
) -> None:
    exported_binding = {
        "binding": "llamacpp",
        "source": "local-path",
        "model": "/old/machine/models/qwen.gguf",  # the exporting machine's path
        "modality": "vision",  # NOT what the repo's pipeline tag implies — must be preserved
        "params": {"mmproj": "auto", "temperature": 0.3},
        "lifecycle": {"keepWarm": True, "idleTimeoutSec": 120, "priority": 3},
        "fallback": "backup-model",
    }
    bundle = {
        "formatVersion": 1,
        "models": [
            {
                "logicalId": "qwen-vl",
                "binding": exported_binding,
                "install": {"repo": REPO, "variantId": VARIANT},
            }
        ],
        "credentialNames": ["HF_TOKEN"],
    }
    with TestClient(_app(tmp_path)) as c:
        report = c.post("/admin/import", json=bundle).json()
        assert report["registered"] == []
        assert report["warnings"] == []
        assert report["credentialNames"] == ["HF_TOKEN"]
        [dl] = report["downloads"]
        assert dl["logicalId"] == "qwen-vl"
        assert dl["repo"] == REPO
        assert _poll_done(c, dl["jobId"]) == "done"

        binding = c.get("/admin/models/qwen-vl").json()["binding"]
        # The EXPORTED binding registered faithfully — never re-derived from the pipeline tag.
        assert binding["modality"] == "vision"
        assert binding["params"] == {"mmproj": "auto", "temperature": 0.3}
        assert binding["lifecycle"] == {"keepWarm": True, "idleTimeoutSec": 120, "priority": 3}
        assert binding["fallback"] == "backup-model"
        assert binding["source"] == "local-path"
        # Only the weights path was rewritten, onto THIS machine's model dir.
        assert binding["model"] != exported_binding["model"]
        assert binding["model"].startswith(str(tmp_path / "state" / "models"))
        assert binding["model"].endswith(VARIANT)

        # Provenance recorded here too, so a re-export keeps the re-download chain intact.
        [model] = c.get("/admin/export").json()["models"]
        assert model["install"] == {"repo": REPO, "variantId": VARIANT}

        # Idempotent: a second import of the same bundle skips (the id is now registered).
        report2 = c.post("/admin/import", json=bundle).json()
        assert report2["skipped"] == ["qwen-vl"]
        assert report2["downloads"] == []


def test_import_skips_model_with_in_flight_download(tmp_path: Path) -> None:
    """A provenance import registers only when its download completes, so mid-download the id
    is still unregistered — a re-run of the import must skip it (with a loud warning), never
    spawn a second concurrent job into the same directory."""
    release = threading.Event()

    def _blocking_fetch(plan: InstallPlan, dest: Path) -> Path:
        assert release.wait(timeout=10)
        return _fake_fetch(plan, dest)

    app = _app(tmp_path)
    app.state.downloader._fetch = _blocking_fetch
    bundle = {
        "models": [
            {
                "logicalId": "qwen",
                "binding": managed_payload(),
                "install": {"repo": REPO, "variantId": VARIANT},
            }
        ]
    }
    with TestClient(app) as c:
        r = c.post(
            "/admin/catalog/install",
            json={"repo": REPO, "engine": "llamacpp", "variantId": VARIANT, "logicalId": "qwen"},
        )
        assert r.status_code == 202
        job_id = r.json()["id"]
        try:
            # The download is in flight and "qwen" is not registered yet.
            assert c.get("/admin/models/qwen").status_code == 404

            report = c.post("/admin/import", json=bundle).json()
            assert report["downloads"] == []
            assert report["registered"] == []
            assert report["skipped"] == ["qwen"]
            [warning] = report["warnings"]
            assert warning["logicalId"] == "qwen"
            assert warning["code"] == "download_in_progress"
            # No second job spawned — the original install is the only one.
            [job] = c.get("/admin/catalog/downloads").json()["downloads"]
            assert job["id"] == job_id
        finally:
            release.set()
        assert _poll_done(c, job_id) == "done"
        assert c.get("/admin/models/qwen").status_code == 200


def test_import_drops_machine_absolute_param_paths(tmp_path: Path) -> None:
    """Aux params that are absolute paths (an explicit mmproj) reference the SOURCE machine;
    the re-download must drop them loudly so the launcher's next-to-weights/auto fallback
    applies — while non-path params ride through untouched."""
    exported_binding = {
        "binding": "llamacpp",
        "source": "local-path",
        "model": "/old/machine/models/qwen.gguf",
        "modality": "vision",
        "params": {"mmproj": "/Users/old/mmproj.gguf", "temperature": 0.3},
    }
    bundle = {
        "models": [
            {
                "logicalId": "qwen-vl",
                "binding": exported_binding,
                "install": {"repo": REPO, "variantId": VARIANT},
            }
        ]
    }
    with TestClient(_app(tmp_path)) as c:
        report = c.post("/admin/import", json=bundle).json()
        [warning] = report["warnings"]
        assert warning["logicalId"] == "qwen-vl"
        assert warning["code"] == "params_path_dropped"
        assert "mmproj" in warning["message"]
        [dl] = report["downloads"]
        assert _poll_done(c, dl["jobId"]) == "done"

        binding = c.get("/admin/models/qwen-vl").json()["binding"]
        # The source machine's projector path is gone; everything else survived.
        assert binding["params"] == {"temperature": 0.3}
        assert binding["modality"] == "vision"
        assert binding["model"].startswith(str(tmp_path / "state" / "models"))


# ── plane guard (mirrors test_catalog_plane's AST check for the new modules) ───────


def test_transfer_modules_import_no_control_plane() -> None:
    import ast

    import theygent_inference_plane.installs as installs_mod
    import theygent_inference_plane.transfer as transfer_mod

    forbidden = {"sqlalchemy", "asyncpg", "psycopg", "theygent_control_plane", "theygent_worker"}
    for module in (installs_mod, transfer_mod):
        tree = ast.parse(Path(module.__file__).read_text())
        imported: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported.update(alias.name.split(".")[0] for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported.add(node.module.split(".")[0])
        bad = imported & forbidden
        assert not bad, f"{module.__name__} must not import control-plane/db modules: {bad}"
