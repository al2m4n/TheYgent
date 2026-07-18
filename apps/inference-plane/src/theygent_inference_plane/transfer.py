"""Registry export/import — the inference-plane half of the transfer bundle.

The export artifact is assembled in the BROWSER: the interface reads this plane's
``/admin/export`` directly and zips it alongside the control-plane bundle, because registry
state lives in this plane's local files and must never transit the control plane. Both
directions are metadata only:

* bindings travel verbatim (the durable truth in ``registry.json``; runtime ``state`` never
  rides along),
* weights never travel — a catalog-installed model exports its ``installs.json`` provenance
  and re-downloads in-plane on import via the existing download path,
* credentials travel as NAMES only; values are write-only and never leave the trust domain.
"""

from __future__ import annotations

import asyncio
from pathlib import PurePosixPath, PureWindowsPath
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError
from pydantic.alias_generators import to_camel
from theygent_ir import ManagedBinding, parse_registration

from theygent_inference_plane.catalog import (
    ENGINE_LIBRARY,
    CatalogError,
    CatalogProvider,
    InstallPlan,
)
from theygent_inference_plane.credentials import CredentialStore
from theygent_inference_plane.downloader import Downloader
from theygent_inference_plane.installs import InstallStore
from theygent_inference_plane.registry import Registry

FORMAT_VERSION = 1


class _Wire(BaseModel):
    """camelCase over the wire, unknown keys rejected loudly — a bundle carrying a field this
    plane doesn't understand fails as ``invalid_bundle``, never a silent drop."""

    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True, extra="forbid")


class InstallRef(_Wire):
    """Re-download provenance for a catalog-installed model: the HF repo plus the variant id
    (a gguf filename for llamacpp; ``""`` for a whole MLX repo)."""

    repo: str
    variant_id: str = ""


class ImportModel(_Wire):
    """One model in an import bundle. The binding stays a raw dict at this layer: per-model
    validation failures must become warnings, not a fatal bundle rejection, so
    ``parse_registration`` runs per entry inside :func:`apply_import`."""

    logical_id: str
    binding: dict[str, Any]
    install: InstallRef | None = None


class ImportBundle(_Wire):
    """``POST /admin/import`` body — the same shape ``GET /admin/export`` emits, so an exported
    bundle round-trips unedited. ``formatVersion``/``credentialNames`` are optional (a
    hand-built models-only bundle imports too); a future format version fails loudly as
    ``invalid_bundle`` rather than being half-understood."""

    format_version: Literal[1] | None = None
    models: list[ImportModel]
    credential_names: list[str] = Field(default_factory=list)


def _is_machine_path(value: Any) -> bool:
    """True for a string shaped like an absolute filesystem path on either path family
    (posix ``/...`` or Windows ``C:\\...``) — a reference to the EXPORTING machine's disk
    that cannot be valid here."""
    return isinstance(value, str) and (
        PurePosixPath(value).is_absolute() or PureWindowsPath(value).is_absolute()
    )


def build_export(
    registry: Registry, installs: InstallStore, credentials: CredentialStore
) -> dict[str, Any]:
    """The export bundle: every registration verbatim (the binding sub-object is the durable
    truth — runtime state is stripped entirely), install provenance when known (``null`` for
    pre-sidecar installs and non-catalog registrations), and credential NAMES only."""
    return {
        "formatVersion": FORMAT_VERSION,
        "models": [
            {
                "logicalId": logical_id,
                "binding": binding.model_dump(by_alias=True),
                "install": installs.get(logical_id),
            }
            for logical_id, binding in registry.items()
        ],
        "credentialNames": credentials.names(),
    }


async def apply_import(
    bundle: ImportBundle,
    *,
    registry: Registry,
    catalog: CatalogProvider,
    downloads: Downloader,
    installs: InstallStore,
) -> dict[str, Any]:
    """Apply an import bundle, per-model (one bad entry never aborts the rest):

    * an already-registered logical id is skipped (import is idempotent),
    * a logical id whose provenance download is still in flight (a re-run or double-submit
      of the same import) is skipped with a ``download_in_progress`` warning — never a
      duplicate concurrent job into the same directory,
    * a non-``local-path`` binding (hf / url / openai-compatible) registers verbatim — hf-source
      weights fetch lazily at first spawn, the engine's own path,
    * a ``local-path`` binding with install provenance and an installable engine re-downloads
      in-plane exactly like ``POST /admin/catalog/install``, and the completed download
      registers the EXPORTED binding with only the weights path rewritten — machine-absolute
      ``params`` paths (e.g. an explicit llamacpp mmproj) are dropped with a loud
      ``params_path_dropped`` warning so the launcher's own next-to-weights fallback applies,
    * a ``local-path`` binding without provenance (or on vllm, which has no install path)
      registers verbatim with a loud ``weights_unavailable`` warning — the machine-specific
      path will 503 at spawn until the user reinstalls the weights.

    Any register-verbatim path also clears a stale ``installs.json`` record for the id: the
    imported binding's weights are not the sidecar's recorded catalog install, and a leftover
    record would pair the new binding with the OLD repo/variant on the next export.
    """
    registered: list[str] = []
    skipped: list[str] = []
    download_jobs: list[dict[str, Any]] = []
    warnings: list[dict[str, str]] = []

    for entry in bundle.models:
        logical_id = entry.logical_id
        if registry.get(logical_id) is not None:
            skipped.append(logical_id)
            continue
        if downloads.active_for(logical_id) is not None:
            # A provenance import registers only when its download COMPLETES, so the id is
            # still unregistered for the whole download window — a re-run of the import must
            # not spawn a second concurrent job into the same destination directory.
            skipped.append(logical_id)
            warnings.append(
                {
                    "logicalId": logical_id,
                    "code": "download_in_progress",
                    "message": (
                        f"{logical_id!r} already has a download in flight; skipped — the "
                        "pending job registers the model when it completes"
                    ),
                }
            )
            continue
        try:
            binding = parse_registration(entry.binding)
        except ValidationError as exc:
            warnings.append(
                {
                    "logicalId": logical_id,
                    "code": "invalid_binding",
                    "message": f"binding failed validation and was not imported: {exc.errors()}",
                }
            )
            continue
        if not isinstance(binding, ManagedBinding) or binding.source != "local-path":
            registry.put(logical_id, binding)
            installs.delete(logical_id)
            registered.append(logical_id)
            continue
        if entry.install is not None and binding.binding in ENGINE_LIBRARY:
            # Auxiliary params that are absolute paths (e.g. an explicit mmproj) reference the
            # SOURCE machine's disk; registered verbatim they would fail at spawn here. Drop
            # them loudly — the launcher's file-next-to-weights / "auto" fallback applies.
            dropped_keys = sorted(
                key for key, value in binding.params.items() if _is_machine_path(value)
            )
            if dropped_keys:
                binding = binding.model_copy(
                    update={
                        "params": {k: v for k, v in binding.params.items() if k not in dropped_keys}
                    }
                )
                for key in dropped_keys:
                    warnings.append(
                        {
                            "logicalId": logical_id,
                            "code": "params_path_dropped",
                            "message": (
                                f"{logical_id!r}: params.{key} is an absolute path on the "
                                "exporting machine and was dropped; the engine's own fallback "
                                "(e.g. a projector found next to the weights) applies here"
                            ),
                        }
                    )
            # Same in-plane download path as POST /admin/catalog/install; the provider sizes
            # the plan (progress denominator). The exported binding rides along as the
            # registration template — modality/params/lifecycle/fallback are the source
            # machine's truth, never re-derived from the HF pipeline tag.
            try:
                plan = await asyncio.to_thread(
                    catalog.install_plan,
                    entry.install.repo,
                    binding.binding,
                    entry.install.variant_id,
                    logical_id,
                )
            except CatalogError:
                # The provider couldn't size the plan (offline, repo moved). The download
                # itself still fetches-or-fails loudly; only the progress denominator is
                # lost, and the exported binding already supplies everything else.
                plan = InstallPlan(
                    logical_id=logical_id,
                    engine=binding.binding,
                    repo=entry.install.repo,
                    modality=binding.modality,
                    filename=entry.install.variant_id or None,
                )
            job = downloads.start(plan, binding_template=binding)
            download_jobs.append(
                {"jobId": job.id, "logicalId": logical_id, "repo": entry.install.repo}
            )
            continue
        registry.put(logical_id, binding)
        installs.delete(logical_id)
        registered.append(logical_id)
        warnings.append(
            {
                "logicalId": logical_id,
                "code": "weights_unavailable",
                "message": (
                    f"{logical_id!r} points at a machine-specific local path with no re-download "
                    "provenance; it is registered but will return 503 at spawn until the weights "
                    "are reinstalled on this machine"
                ),
            }
        )

    return {
        "registered": registered,
        "skipped": skipped,
        "downloads": download_jobs,
        "warnings": warnings,
        "credentialNames": list(bundle.credential_names),
    }
