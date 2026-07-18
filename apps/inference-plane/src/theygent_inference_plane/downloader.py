"""In-plane model download with live progress.

The "convincing" half of browse-and-install: once the user
picks a variant, the weights are fetched **here, in the inference plane** (their machine), and on
completion the model is registered in the inference-plane-local registry — so it appears under
"Installed" and is immediately usable on ``/v1/*``. theygent-the-vendor never sees the download.

Two seams keep this testable and honest:

* ``fetch`` — the blocking download. Real impl uses ``huggingface_hub`` (``hf_hub_download`` for a
  single GGUF, ``snapshot_download`` for an MLX repo) into the inference plane's own model dir, and
  returns the path to register. The fast suite injects a fake that writes a stub file (no network).
* ``dir_size`` — the on-disk byte count of the download target. Progress is *observed* bytes-on-disk
  vs the variant's known total, not a fragile tqdm hook. The fast suite injects a deterministic one.

Downloads are **ephemeral** (an in-memory job table); only the *registration* persists, via the
existing :class:`~theygent_inference_plane.registry.Registry`. Nothing here touches the
control plane.
"""

from __future__ import annotations

import asyncio
import os
import re
from collections.abc import Callable
from pathlib import Path
from typing import Any, Literal

from theygent_ir import ManagedBinding

from theygent_inference_plane.catalog import InstallPlan
from theygent_inference_plane.credentials import CredentialStore
from theygent_inference_plane.installs import InstallStore
from theygent_inference_plane.registry import Registry

DownloadStatus = Literal["downloading", "registering", "done", "error", "cancelled"]

_POLL_INTERVAL_SEC = 0.5

#: Reserved credential name for gated-repo downloads. The user PUTs it to
#: ``/admin/credentials/HF_TOKEN`` (browser → inference plane directly — the token lives
#: in the user's trust domain and never transits the control plane) or exports it as an
#: env var; either way it resolves HERE and is handed straight to huggingface_hub.
HF_TOKEN_NAME = "HF_TOKEN"


def _hf_token(store: CredentialStore | None) -> str | None:
    """The gated-download token, when the user configured one — ``None`` otherwise
    (huggingface_hub's own default behavior is unchanged). Store first, then the process
    env: the same order ``secret://NAME`` refs resolve. The value goes straight into the
    hub call — never logged, never echoed."""
    token = store.get(HF_TOKEN_NAME) if store is not None else None
    return token or os.environ.get(HF_TOKEN_NAME) or None


def _sanitize(repo: str) -> str:
    """A filesystem-safe per-repo directory name (``org/name`` → ``org__name``)."""
    return re.sub(r"[^A-Za-z0-9._-]", "_", repo)


def _dir_size(path: Path) -> int:
    """Recursive byte count of everything under ``path`` (0 if it does not exist yet). Counts
    in-progress ``.incomplete`` files too, so the progress bar moves while a file streams in."""
    if not path.exists():
        return 0
    if path.is_file():
        try:
            return path.stat().st_size
        except OSError:
            return 0
    total = 0
    for root, _dirs, files in os.walk(path):
        for f in files:
            try:
                total += (Path(root) / f).stat().st_size
            except OSError:
                continue
    return total


def _hf_fetch(plan: InstallPlan, dest_dir: Path, *, token: str | None = None) -> Path:
    """Real download into ``dest_dir`` (under the plane's model dir). Returns the path to register:
    the GGUF file for llamacpp, the snapshot directory for mlx. ``token`` authenticates gated
    repos; ``None`` leaves huggingface_hub's own env/config behavior untouched."""
    dest_dir.mkdir(parents=True, exist_ok=True)
    if plan.engine == "llamacpp":
        from huggingface_hub import hf_hub_download

        assert plan.filename is not None
        out = hf_hub_download(
            repo_id=plan.repo, filename=plan.filename, local_dir=str(dest_dir), token=token
        )
        return Path(out)
    # mlx: snapshot the whole repo into a flat local dir; mlx_lm.server runs from the directory.
    from huggingface_hub import snapshot_download

    out = snapshot_download(repo_id=plan.repo, local_dir=str(dest_dir), token=token)
    return Path(out)


class DownloadJob:
    """One install in flight. Mutated by the running task; read by the progress endpoint.

    ``binding_template`` (set by the registry-import path) is the full binding to register on
    completion — modality/params/lifecycle/fallback preserved from the exporting machine, only
    the weights path rewritten. ``None`` (a plain catalog install) derives the binding from the
    plan as before."""

    def __init__(
        self,
        job_id: str,
        plan: InstallPlan,
        dest_dir: Path,
        *,
        binding_template: ManagedBinding | None = None,
    ) -> None:
        self.id = job_id
        self.plan = plan
        self.dest_dir = dest_dir
        self.binding_template = binding_template
        self.status: DownloadStatus = "downloading"
        self.done_bytes: int = 0
        self.total_bytes: int | None = plan.total_bytes
        self.error: str | None = None
        self.task: asyncio.Task[None] | None = None  # the running install task (for cancel)

    def view(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "logicalId": self.plan.logical_id,
            "repo": self.plan.repo,
            "engine": self.plan.engine,
            "status": self.status,
            "doneBytes": self.done_bytes,
            "totalBytes": self.total_bytes,
            "error": self.error,
        }


# ``list`` (the method) shadows the builtin in the class body; route the return type through a
# module-level alias so the annotation resolves to the builtin ``list``.
JobList = list[DownloadJob]


class Downloader:
    """Runs installs and tracks their progress. One per inference-plane process.

    On completion it registers the model (``binding=<engine>``, ``source=local-path``, ``model=<the
    downloaded path>``) in the local registry — uniform across engines, fully local (no re-fetch
    ambiguity). ``fetch`` / ``dir_size`` are injectable for the fast suite.
    """

    def __init__(
        self,
        registry: Registry,
        model_dir: Path,
        *,
        fetch: Callable[[InstallPlan, Path], Path] | None = None,
        dir_size: Callable[[Path], int] | None = None,
        credentials: CredentialStore | None = None,
        installs: InstallStore | None = None,
    ) -> None:
        self._registry = registry
        self._model_dir = model_dir
        self._credentials = credentials
        # The install-provenance sidecar (installs.json): a completed install records its
        # repo + variant here — the only durable capture point, since this job table is
        # in-memory — so /admin/export can carry faithful re-download metadata.
        self._installs = installs
        # The default fetch resolves the reserved HF_TOKEN credential PER DOWNLOAD (not at
        # construction), so a token added over /admin/credentials applies to the next
        # install without a restart. The fetch seam's (plan, dest) signature is unchanged —
        # injected test fakes never see the token.
        self._fetch = fetch or (
            lambda plan, dest: _hf_fetch(plan, dest, token=_hf_token(self._credentials))
        )
        self._dir_size = dir_size or _dir_size
        self._jobs: dict[str, DownloadJob] = {}
        self._tasks: set[asyncio.Task[None]] = set()
        self._seq = 0

    def _next_id(self) -> str:
        self._seq += 1
        return f"dl-{self._seq}"

    def start(
        self, plan: InstallPlan, *, binding_template: ManagedBinding | None = None
    ) -> DownloadJob:
        job_id = self._next_id()
        dest_dir = self._model_dir / _sanitize(plan.repo)
        job = DownloadJob(job_id, plan, dest_dir, binding_template=binding_template)
        self._jobs[job_id] = job
        task = asyncio.create_task(self._run(job))
        job.task = task
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return job

    def get(self, job_id: str) -> DownloadJob | None:
        return self._jobs.get(job_id)

    def active_for(self, logical_id: str) -> DownloadJob | None:
        """The non-terminal job for ``logical_id``, if one is in flight. Registration happens
        only on completion, so a registry lookup alone cannot see a pending install — anything
        that must not double-download (the import path) checks here first."""
        for job in self._jobs.values():
            if job.plan.logical_id == logical_id and job.status in ("downloading", "registering"):
                return job
        return None

    def list(self) -> JobList:
        return list(self._jobs.values())

    def cancel(self, job_id: str) -> DownloadJob | None:
        """Stop an in-flight install. The awaiting task is cancelled (so the model is never
        registered); the underlying HF transfer thread may finish in the background, but its result
        is discarded. A terminal job is left untouched."""
        job = self._jobs.get(job_id)
        if job is None:
            return None
        if job.status in ("downloading", "registering"):
            job.status = "cancelled"
            if job.task is not None:
                job.task.cancel()
        return job

    async def _run(self, job: DownloadJob) -> None:
        poller = asyncio.create_task(self._poll(job))
        try:
            try:
                path = await asyncio.to_thread(self._fetch, job.plan, job.dest_dir)
            finally:
                poller.cancel()
        except asyncio.CancelledError:
            # Cancelled via cancel(): status is already "cancelled"; end without registering.
            job.status = "cancelled"
            return
        except Exception as exc:
            job.status = "error"
            job.error = str(exc)
            return
        # Land the final byte count so the bar reads 100% even if the last poll missed it.
        if job.total_bytes:
            job.done_bytes = job.total_bytes
        job.status = "registering"
        try:
            if job.binding_template is not None:
                # A registry import re-download: register the EXPORTED binding faithfully —
                # modality/params/lifecycle/fallback are the source machine's truth — with
                # only the weights path rewritten onto this machine.
                binding = job.binding_template.model_copy(update={"model": str(path)})
            else:
                # Carry the plan's modality onto the binding so the manager spawns the right
                # server (e.g. an installed VLM registers modality="vision" → mlx_vlm.server);
                # default chat.
                binding = ManagedBinding(
                    binding=job.plan.engine,
                    source="local-path",
                    model=str(path),
                    modality=job.plan.modality,
                )
            self._registry.put(job.plan.logical_id, binding)
            if self._installs is not None:
                # Record repo + variant provenance durably alongside the registration — for
                # llamacpp the variant is the plan's gguf filename; "" is a whole MLX repo.
                self._installs.put(
                    job.plan.logical_id, repo=job.plan.repo, variant_id=job.plan.filename or ""
                )
        except Exception as exc:
            job.status = "error"
            job.error = f"download ok but registration failed: {exc}"
            return
        job.status = "done"

    async def _poll(self, job: DownloadJob) -> None:
        try:
            while True:
                job.done_bytes = self._dir_size(job.dest_dir)
                await asyncio.sleep(_POLL_INTERVAL_SEC)
        except asyncio.CancelledError:
            return
