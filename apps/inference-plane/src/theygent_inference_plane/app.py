"""The two HTTP surfaces (management plane and data plane), never conflated.

  * /admin/* — management plane (theygent-native): registry, lifecycle, caps, health
  * /v1/*    — data plane (OpenAI-compatible): the `model` field is a LOGICAL id

``create_app`` takes injectable seams (launcher, clock, probe, policy) so the fast
suite runs everything-real-except-the-weights via a FakeUpstreamLauncher.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
from pathlib import Path
from typing import Annotated, Any

from fastapi import FastAPI, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, ConfigDict, Field, StrictInt, ValidationError
from starlette.background import BackgroundTask
from theygent_ir import Capabilities, ManagedBinding, Modality, parse_registration

from theygent_inference_plane.catalog import (
    ENGINE_LIBRARY,
    CatalogError,
    CatalogProvider,
    CatalogQuery,
    HuggingFaceProvider,
    Sort,
)
from theygent_inference_plane.clock import Clock
from theygent_inference_plane.credentials import (
    CredentialResolutionError,
    CredentialStore,
    InvalidCredentialName,
    resolve_credential,
)
from theygent_inference_plane.downloader import Downloader, _sanitize
from theygent_inference_plane.eviction import EvictionPolicy, ResourceProbe
from theygent_inference_plane.gateway import Gateway, merge_params
from theygent_inference_plane.installs import InstallStore
from theygent_inference_plane.launcher import (
    EngineLauncher,
    EngineUnavailableError,
    ImageServerLauncher,
    LlamaCppLauncher,
    ManagedLauncherSet,
    MlxAudioLauncher,
    MlxLauncher,
    MlxVlmLauncher,
    WhisperCppLauncher,
    _hf_hub_dir,
)
from theygent_inference_plane.manager import (
    EngineManager,
    NoCapacityError,
    NotManagedError,
    Upstream,
)
from theygent_inference_plane.registry import Registry, UnknownLogicalId
from theygent_inference_plane.settings import (
    MAX_RESIDENT_DEFAULT,
    MAX_RESIDENT_ENV_VAR,
    MAX_RESIDENT_MAX,
    MAX_RESIDENT_MIN,
    SettingsStore,
    resolve_max_resident,
)
from theygent_inference_plane.transfer import ImportBundle, apply_import, build_export
from theygent_inference_plane.vllm_engine import VllmLauncher

_REAP_INTERVAL_SEC = 30.0


class ChatRequest(BaseModel):
    model_config = ConfigDict(extra="allow")

    model: str
    messages: list[dict[str, Any]]
    stream: bool = False


class EmbeddingsRequest(BaseModel):
    # The embeddings data-plane shape follows the OpenAI contract. `model` is a LOGICAL id;
    # extras (dimensions / encoding_format) flow through to the upstream as generation params.
    model_config = ConfigDict(extra="allow")

    model: str
    input: str | list[str]


class SpeechRequest(BaseModel):
    # The OpenAI text-to-speech shape. `model` is a LOGICAL id. `voice`/`response_format`/
    # `speed` ride through as params; the response is audio bytes. `voice` is OPTIONAL
    # with no invented default: voice vocabularies are per-engine (an OpenAI voice name
    # forced onto a local speech server names a voice it doesn't have), so an omitted voice falls
    # through to the binding's registered `params` default, then the engine's own default.
    model_config = ConfigDict(extra="allow")

    model: str
    input: str
    voice: str | None = None


class ImagesRequest(BaseModel):
    # The OpenAI image-generation shape. `model` is a LOGICAL id; `prompt` is the text to render.
    # `size`/`n`/`response_format` and any engine knob (`steps`) ride through as generation params.
    # The response is the OpenAI images shape ({data: [{b64_json}]}).
    model_config = ConfigDict(extra="allow")

    model: str
    prompt: str


class SettingsPatch(BaseModel):
    # The writable settings surface (management plane, camelCase). Strict and closed:
    # an unknown key, a non-integer (bools included), or an out-of-bounds ceiling is a
    # 422, never silently ignored. `null` = reset to default (delete the stored value).
    model_config = ConfigDict(extra="forbid")

    max_resident: Annotated[StrictInt, Field(ge=MAX_RESIDENT_MIN, le=MAX_RESIDENT_MAX)] | None = (
        Field(alias="maxResident")
    )


# Map an OpenAI `response_format` to the audio MIME type for the TTS response body.
_AUDIO_MIME = {
    "mp3": "audio/mpeg",
    "opus": "audio/ogg",
    "aac": "audio/aac",
    "flac": "audio/flac",
    "wav": "audio/wav",
    "pcm": "audio/pcm",
}


def _openai_error(message: str, *, status: int, type_: str, code: str) -> JSONResponse:
    return JSONResponse(
        status_code=status,
        content={"error": {"message": message, "type": type_, "code": code}},
    )


def _engine_unavailable(exc: Exception) -> JSONResponse:
    # The model is registered but its engine isn't installed/ready on this host
    # (e.g. an `mlx` model on a box without mlx-lm, or `vllm` without CUDA).
    return _openai_error(str(exc), status=503, type_="server_error", code="engine_unavailable")


async def _guarded_sse(lines: Any, model: str, close: Any = None):
    """Relay SSE lines, converting a mid-stream failure into a final structured error
    frame. Once the body runs, the 200 header is committed — raising here can only tear
    the connection, and the caller would see a transport error ("incomplete chunked
    read") with the real cause lost. An OpenAI-style ``{"error": ...}`` data frame
    instead ends the stream honestly: SDK clients raise it as an API error carrying the
    upstream's message. ``close`` releases the engine lease (managed bindings) and runs
    even when the client disconnects mid-stream."""
    try:
        async for line in lines:
            yield line
    except Exception as exc:
        status = getattr(exc, "status_code", None)
        message = getattr(exc, "message", None) or str(exc)
        kind = (
            "invalid_request_error"
            if isinstance(status, int) and 400 <= status < 500
            else "server_error"
        )
        frame = {
            "error": {
                "message": f"the stream for {model!r} aborted upstream: {message}",
                "type": kind,
                "code": "upstream_error",
            }
        }
        yield f"data: {json.dumps(frame)}\n\n"
    finally:
        if close is not None:
            await close()


def _cockpit_cors_origins() -> list[str]:
    # The interface SPA calls the management plane (/admin/models, /admin/engines) DIRECTLY
    # from the browser — the inference plane is user-controlled and reachable, not proxied
    # through the control-plane (the two-plane split). So the browser needs CORS for the dev
    # origin here too, symmetric with the control-plane's. Narrow dev-origin only (never `*`);
    # override via THEYGENT_CORS_ORIGINS (comma-separated). Empty list disables it entirely.
    raw = os.environ.get("THEYGENT_CORS_ORIGINS")
    if raw is not None:
        return [o.strip() for o in raw.split(",") if o.strip()]
    # Both the control-plane cockpit (:5173) and the visual interface (:5174) call
    # /admin/models directly.
    return [
        "http://localhost:5173",
        "http://127.0.0.1:5173",
        "http://localhost:5174",
        "http://127.0.0.1:5174",
    ]


def create_app(
    *,
    launcher: EngineLauncher | None = None,
    clock: Clock | None = None,
    probe: ResourceProbe | None = None,
    policy: EvictionPolicy | None = None,
    max_resident: int | None = None,
    enable_reaper: bool = True,
    cors_origins: list[str] | None = None,
    state_path: Path | None = None,
    catalog_provider: CatalogProvider | None = None,
    downloader: Downloader | None = None,
    model_dir: Path | None = None,
) -> FastAPI:
    # Persist the logical-model registry LOCALLY to the inference plane (never the
    # control-plane's Postgres — the plane boundary). `state_path=None` keeps it in-memory (the
    # fast suite never touches disk); the real entrypoint passes a path under the plane's state dir.
    registry = Registry(state_path)
    # User-side credential store for reachable bindings' `secret://NAME` refs — local to the
    # inference plane (the user's trust domain), never the control plane. Sits next to registry.json
    # in the state dir; `state_path=None` keeps it in-memory (the fast suite never touches disk).
    credential_store = CredentialStore(
        state_path.with_name("credentials.json") if state_path is not None else None
    )
    # Writable platform settings — settings.json, a peer of registry.json/credentials.json in the
    # same local state dir (never the control plane); in-memory when `state_path` is None.
    settings_store = SettingsStore(
        state_path.with_name("settings.json") if state_path is not None else None
    )
    # Install provenance — installs.json, another peer of registry.json: which HF repo + variant
    # a catalog-installed logical id came from, so /admin/export can carry faithful re-download
    # metadata. In-memory when `state_path` is None.
    install_store = InstallStore(
        state_path.with_name("installs.json") if state_path is not None else None
    )
    # The resident-engine ceiling: explicit argument (the env-injection seam — the entrypoint
    # passes THEYGENT_MAX_RESIDENT through only when set AND non-empty, so a value here is
    # env-PINNED and /admin/settings refuses writes) > the stored settings.json value > the
    # coded default. The resolved source is what /admin/settings reports.
    max_resident_setting = resolve_max_resident(max_resident, settings_store)
    # One launcher per (engine, modality), behind a single dispatcher so the manager stays
    # engine-agnostic (MLX/vLLM — and now the non-chat modalities — added with zero EngineManager
    # changes). Tests inject a single fake launcher that serves every binding. llama.cpp serves
    # chat + embeddings from the SAME llama-server (one instance, flags differ — two keys); MLX chat
    # (mlx_lm.server) and vision (mlx_vlm.server) are distinct programs.
    _llamacpp = LlamaCppLauncher()
    engine_launcher = launcher or ManagedLauncherSet(
        {
            ("llamacpp", "chat"): _llamacpp,
            ("llamacpp", "embeddings"): _llamacpp,
            # Vision is the SAME llama-server + a multimodal projector (image_url on
            # /v1/chat/completions) — one binary, many modalities via flags, so it shares the
            # llama.cpp launcher instance, like embeddings.
            ("llamacpp", "vision"): _llamacpp,
            # Speech-to-text is the ggml family's OTHER program (whisper-server), dispatched by
            # the modality key exactly like mlx vision — same engine name, different server.
            ("llamacpp", "audio.transcription"): WhisperCppLauncher(),
            # Image generation is the one modality with no ready OpenAI server — the local diffusion
            # generators are CLIs. A bundled wrapper server shells out to stable-diffusion.cpp's
            # sd-cli (the ggml family, under the llamacpp engine).
            ("llamacpp", "images.generation"): ImageServerLauncher("sdcpp"),
            ("mlx", "chat"): MlxLauncher(),
            ("mlx", "vision"): MlxVlmLauncher(),
            # Text-to-speech is MLX's other program (mlx_audio.server), same dispatch pattern.
            ("mlx", "audio.speech"): MlxAudioLauncher(),
            # Image generation on Apple Silicon is mflux (FLUX), wrapped the same way as sd-cli.
            ("mlx", "images.generation"): ImageServerLauncher("mflux"),
            ("vllm", "chat"): VllmLauncher(),
        }
    )
    manager = EngineManager(
        registry,
        engine_launcher,
        clock=clock,
        probe=probe,
        policy=policy,
        max_resident=max_resident_setting.value,
    )
    gateway = Gateway()
    # Discovery + in-plane install. The catalog provider (HF today; the seam takes MCP/Apify
    # adapters later) and the downloader are injectable so the fast suite runs with a fake Hub + a
    # fake fetcher — no network, no weights on disk. Both live HERE, in the inference plane: install
    # downloads in the user's trust domain and registers into the local registry, never the control
    # plane. `model_dir` is where installed weights land (used by the real downloader).
    catalog: CatalogProvider = catalog_provider or HuggingFaceProvider()
    _model_dir = model_dir or (Path.home() / ".theygent" / "inference" / "models")
    # The credential store rides into the downloader so the reserved HF_TOKEN credential can
    # authenticate gated-repo downloads — resolved locally, per download, never logged. The
    # install store rides in too: a completed install records its repo + variant provenance.
    downloads = downloader or Downloader(
        registry, _model_dir, credentials=credential_store, installs=install_store
    )

    @contextlib.asynccontextmanager
    async def lifespan(app: FastAPI):
        reaper: asyncio.Task[None] | None = None
        if enable_reaper:

            async def _reap_loop() -> None:
                while True:
                    await asyncio.sleep(_REAP_INTERVAL_SEC)
                    with contextlib.suppress(Exception):
                        await manager.reap()

            reaper = asyncio.create_task(_reap_loop())
        try:
            yield
        finally:
            if reaper is not None:
                reaper.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await reaper
            await manager.shutdown()

    app = FastAPI(title="theygent inference plane", lifespan=lifespan)
    _origins = cors_origins if cors_origins is not None else _cockpit_cors_origins()
    if _origins:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=_origins,
            allow_methods=["*"],
            allow_headers=["*"],
        )
    app.state.registry = registry
    app.state.manager = manager
    app.state.gateway = gateway
    app.state.launcher = engine_launcher
    app.state.catalog = catalog
    app.state.downloader = downloads
    app.state.credential_store = credential_store
    app.state.settings_store = settings_store
    app.state.install_store = install_store
    # Environment facts for /admin/diagnostics (and anything else that needs to know where
    # this plane keeps its state): the registry file path (None = in-memory) + the model dir.
    app.state.state_path = state_path
    app.state.model_dir = _model_dir

    # ── management plane: /admin/* ──────────────────────────────────────

    def _model_view(logical_id: str) -> dict[str, Any]:
        binding = registry.require(logical_id)
        return {
            "logicalId": logical_id,
            "binding": binding.model_dump(by_alias=True),
            "state": manager.state(logical_id),
        }

    @app.put("/admin/models/{logical_id}")
    async def put_model(logical_id: str, request: Request) -> JSONResponse:
        raw = await request.json()
        try:
            binding = parse_registration(raw)
        except ValidationError as exc:
            return _openai_error(
                f"invalid registration payload: {exc.errors()}",
                status=422,
                type_="invalid_request_error",
                code="invalid_binding",
            )
        registry.put(logical_id, binding)
        # A manual registration severs catalog provenance: the binding no longer points at
        # the sidecar's recorded install, and a stale record would make /admin/export pair
        # the NEW binding with the OLD repo/variant — an import would then silently fetch
        # different weights than this machine runs.
        install_store.delete(logical_id)
        return JSONResponse(_model_view(logical_id))

    @app.get("/admin/models")
    async def list_models() -> dict[str, Any]:
        return {"models": [_model_view(lid) for lid in registry.ids()]}

    @app.get("/admin/engines")
    async def list_engines() -> dict[str, Any]:
        # Running managed engines + resident state. Count-based arbitration,
        # so no RAM/VRAM bytes yet — maxResident is the ceiling the policy enforces.
        # `maxResident` here is a read-only SNAPSHOT duplicate: this route is the
        # runtime view (what ceiling is the manager enforcing right now, next to the engines
        # it applies to), while /admin/settings is the writable home carrying value+source.
        # Existing consumers of this shape keep working; writes go through /admin/settings.
        return {"maxResident": manager.max_resident, "resident": manager.resident_engines()}

    @app.get("/admin/models/{logical_id}")
    async def get_model(logical_id: str) -> Response:
        if registry.get(logical_id) is None:
            return _openai_error(
                f"unknown logical id {logical_id!r}",
                status=404,
                type_="invalid_request_error",
                code="model_not_found",
            )
        return JSONResponse(_model_view(logical_id))

    @app.delete("/admin/models/{logical_id}", status_code=204)
    async def delete_model(logical_id: str) -> Response:
        if registry.get(logical_id) is None:
            return _openai_error(
                f"unknown logical id {logical_id!r}",
                status=404,
                type_="invalid_request_error",
                code="model_not_found",
            )
        await manager.evict(logical_id)
        registry.delete(logical_id)
        # The install provenance belongs to the registration — a deleted logical id must not
        # keep exporting re-download metadata for a model that no longer exists here.
        install_store.delete(logical_id)
        return Response(status_code=204)

    @app.get("/admin/models/{logical_id}/capabilities")
    async def get_capabilities(logical_id: str) -> Response:
        binding = registry.get(logical_id)
        if binding is None:
            return _openai_error(
                f"unknown logical id {logical_id!r}",
                status=404,
                type_="invalid_request_error",
                code="model_not_found",
            )
        if isinstance(binding, ManagedBinding):
            try:
                caps = await manager.capabilities(logical_id)
            except EngineUnavailableError as exc:
                return _engine_unavailable(exc)
        else:
            # Reachable upstreams aren't probed locally; advertise the DECLARED task (the only
            # signal there is — the chat/bench surfaces route their UI on it), all else defaults.
            caps = Capabilities(modalities=[binding.modality])
        return JSONResponse(caps.model_dump(by_alias=True))

    @app.post("/admin/models/{logical_id}:warm")
    async def warm_model(logical_id: str) -> Response:
        binding = registry.get(logical_id)
        if binding is None:
            return _openai_error(
                f"unknown logical id {logical_id!r}",
                status=404,
                type_="invalid_request_error",
                code="model_not_found",
            )
        if isinstance(binding, ManagedBinding):
            try:
                await manager.warm(logical_id)
            except EngineUnavailableError as exc:
                return _engine_unavailable(exc)
            except NoCapacityError as exc:
                return _openai_error(str(exc), status=503, type_="server_error", code="no_capacity")
        return JSONResponse(_model_view(logical_id))

    @app.post("/admin/models/{logical_id}:evict")
    async def evict_model(logical_id: str) -> Response:
        if registry.get(logical_id) is None:
            return _openai_error(
                f"unknown logical id {logical_id!r}",
                status=404,
                type_="invalid_request_error",
                code="model_not_found",
            )
        await manager.evict(logical_id)
        return JSONResponse(_model_view(logical_id))

    # ── management plane: /admin/settings + /admin/diagnostics ──────────
    # The settings resource carries ONLY settable knobs (read-write symmetric: everything in a
    # GET can be PATCHed); read-only environment facts live in /admin/diagnostics. Both are
    # named additive extensions of the management plane — camelCase, strict shapes.

    def _settings_view() -> dict[str, Any]:
        # Source is recomputed per request so it stays honest across writes: env-pinned wins
        # for the process lifetime; otherwise a stored value, else the coded default.
        if max_resident_setting.pinned:
            source = "env"
        elif settings_store.max_resident is not None:
            source = "stored"
        else:
            source = "default"
        return {
            "maxResident": {
                # The manager's ceiling is the live truth (the PATCH hook updates it in place).
                "value": manager.max_resident,
                "default": MAX_RESIDENT_DEFAULT,
                "source": source,
                "envVar": MAX_RESIDENT_ENV_VAR,
                # "live": a change applies NOW — idle excess engines are evicted immediately,
                # busy ones drain and tear down when their in-flight work completes.
                "apply": "live",
            }
        }

    @app.get("/admin/settings")
    async def get_settings() -> dict[str, Any]:
        return _settings_view()

    @app.patch("/admin/settings")
    async def patch_settings(request: Request) -> Response:
        raw = await request.json()
        try:
            patch = SettingsPatch.model_validate(raw)
        except ValidationError as exc:
            return _openai_error(
                f"invalid settings payload: {exc.errors()}",
                status=422,
                type_="invalid_request_error",
                code="invalid_settings",
            )
        if max_resident_setting.pinned:
            return _openai_error(
                f"maxResident is pinned by the {MAX_RESIDENT_ENV_VAR} environment variable on "
                "this host and cannot be changed here; unset the variable (or leave it empty) "
                "and restart the inference plane to make it editable",
                status=422,
                type_="invalid_request_error",
                code="env_pinned",
            )
        # Persist (null deletes the stored value = reset to default), then apply live: point
        # the manager at the resolved ceiling and enforce it immediately. Never evicts an
        # engine with an in-flight request — busy excess engines drain and die on idle.
        settings_store.set_max_resident(patch.max_resident)
        manager.set_max_resident(resolve_max_resident(None, settings_store).value)
        await manager.enforce_ceiling()
        return JSONResponse(_settings_view())

    @app.get("/admin/diagnostics")
    async def diagnostics() -> dict[str, Any]:
        # Read-only environment facts. Binary paths reuse each launcher's construction-time
        # resolution — nothing is spawned, nothing raises; a missing binary is reported as
        # status "missing" with a null path (the same fact /readyz folds into not-ready).
        binaries: list[dict[str, Any]] = []
        if isinstance(engine_launcher, ManagedLauncherSet):
            binaries = [
                {
                    "engine": b.engine,
                    "modality": b.modality,
                    "path": b.path,
                    "status": "resolved" if b.resolved else "missing",
                }
                for b in engine_launcher.binaries()
            ]
        return {
            "stateDir": str(state_path.parent) if state_path is not None else None,
            "modelDir": str(_model_dir),
            "hfHome": _hf_hub_dir(),
            "persistent": state_path is not None,
            "binaries": binaries,
        }

    # ── management plane: /admin/credentials (user-side named secrets) ───
    # A local named-secret store for reachable bindings' `secret://NAME` refs. Values are
    # WRITE-ONLY: the listing returns names + `hasValue` only, never a value, and nothing here
    # crosses to the control plane (the sovereignty invariant). A ref resolves NAME from this store
    # first, then the process environment.

    @app.get("/admin/credentials")
    async def list_credentials() -> dict[str, Any]:
        return {"credentials": [{"name": n, "hasValue": True} for n in credential_store.names()]}

    @app.put("/admin/credentials/{name}")
    async def put_credential(name: str, request: Request) -> Response:
        raw = await request.json()
        value = raw.get("value") if isinstance(raw, dict) else None
        if not isinstance(value, str) or value == "":
            return _openai_error(
                "credential 'value' must be a non-empty string",
                status=422,
                type_="invalid_request_error",
                code="invalid_credential",
            )
        try:
            credential_store.set(name, value)
        except InvalidCredentialName as exc:
            return _openai_error(
                str(exc), status=422, type_="invalid_request_error", code="invalid_credential"
            )
        return JSONResponse({"name": name, "hasValue": True})

    @app.delete("/admin/credentials/{name}", status_code=204)
    async def delete_credential(name: str) -> Response:
        if not credential_store.delete(name):
            return _openai_error(
                f"unknown credential {name!r}",
                status=404,
                type_="invalid_request_error",
                code="credential_not_found",
            )
        return Response(status_code=204)

    # ── management plane: /admin/catalog/* (discovery + install) ─────────
    # Browse a provider, then install the chosen variant by downloading it HERE and registering it
    # locally. Engine-compatibility is enforced server-side: the listing is filtered to the
    # engines this host actually has ready, so an unrunnable model is never surfaced.

    def _ready_engines() -> list[str]:
        # Map the launcher's readiness onto installable engines. The real app's dispatcher reports
        # per-engine readiness; a single test launcher that is ready serves every binding.
        if isinstance(engine_launcher, ManagedLauncherSet):
            return [
                name
                for name, r in engine_launcher.readiness().items()
                if r.ready and name in ENGINE_LIBRARY
            ]
        return list(ENGINE_LIBRARY) if getattr(engine_launcher, "ready", False) else []

    def _mark_installed(entries: list[Any]) -> None:
        # Cross-reference the local registry so the UI can show "✓ Installed" instead of re-offering
        # a download. Installed weights register as source=local-path with the sanitized repo as a
        # path segment (see Downloader), so we match that segment against each entry's ref.
        seg_to_lid: dict[str, str] = {}
        for lid, binding in registry.items():
            if getattr(binding, "source", None) == "local-path":
                for part in Path(binding.model).parts:
                    seg_to_lid.setdefault(part, lid)
        for e in entries:
            lid = seg_to_lid.get(_sanitize(e.ref))
            if lid:
                e.installed = True
                e.installed_as = lid

    # Param-size buckets → HF ``num_parameters`` ranges (the size filter).
    _SIZE_NUM_PARAMS = {"small": "max:3B", "medium": "min:3B,max:15B", "large": "min:15B"}

    @app.get("/admin/catalog/models")
    async def catalog_list(
        search: str = "",
        sort: Sort = "trending",
        limit: int = 30,
        engines: str | None = None,
        size: str | None = None,
        modality: Modality | None = None,
    ) -> Response:
        # `sort`/`modality` are validated at the edge (an unknown value → 422) — both are
        # literals; `modality` narrows the listing to one task (chat/vision/embeddings/audio.*).
        ready = _ready_engines()
        # An optional `engines` override narrows to a subset — but only ever within what's ready, so
        # the invariant (never surface an unrunnable model) holds even if the client asks wider.
        if engines is not None:
            requested = {e.strip() for e in engines.split(",") if e.strip()}
            selected = [e for e in ready if e in requested]
        else:
            selected = ready
        q = CatalogQuery(
            search=search,
            sort=sort,
            limit=min(max(limit, 1), 100),
            engines=selected,
            num_params=_SIZE_NUM_PARAMS.get(size or ""),
            modality=modality,
        )
        try:
            entries = await asyncio.to_thread(catalog.list, q)
        except CatalogError as exc:
            return _openai_error(str(exc), status=502, type_="server_error", code="catalog_error")
        _mark_installed(entries)
        return JSONResponse(
            {"entries": [e.model_dump(by_alias=True) for e in entries], "engines": ready}
        )

    @app.get("/admin/catalog/models/{repo:path}")
    async def catalog_get(repo: str) -> Response:
        engines = _ready_engines()
        q = CatalogQuery(engines=engines)
        try:
            entry = await asyncio.to_thread(catalog.get, repo, q)
        except CatalogError as exc:
            return _openai_error(str(exc), status=502, type_="server_error", code="catalog_error")
        return JSONResponse(entry.model_dump(by_alias=True))

    @app.post("/admin/catalog/install", status_code=202)
    async def catalog_install(request: Request) -> Response:
        body = await request.json()
        repo = body.get("repo")
        engine = body.get("engine")
        variant_id = body.get("variantId", "")
        logical_id = body.get("logicalId")
        if not repo or not logical_id or engine not in ENGINE_LIBRARY:
            return _openai_error(
                "install requires `repo`, `logicalId`, and an installable `engine` "
                f"(one of {sorted(ENGINE_LIBRARY)})",
                status=422,
                type_="invalid_request_error",
                code="invalid_install",
            )
        if registry.get(logical_id) is not None:
            return _openai_error(
                f"logical id {logical_id!r} is already registered",
                status=409,
                type_="invalid_request_error",
                code="logical_id_exists",
            )
        try:
            plan = await asyncio.to_thread(
                catalog.install_plan, repo, engine, variant_id, logical_id
            )
        except CatalogError as exc:
            return _openai_error(str(exc), status=502, type_="server_error", code="catalog_error")
        job = downloads.start(plan)
        return JSONResponse(job.view(), status_code=202)

    @app.get("/admin/catalog/downloads")
    async def catalog_downloads() -> dict[str, Any]:
        return {"downloads": [j.view() for j in downloads.list()]}

    @app.get("/admin/catalog/downloads/{job_id}")
    async def catalog_download(job_id: str) -> Response:
        job = downloads.get(job_id)
        if job is None:
            return _openai_error(
                f"unknown download {job_id!r}",
                status=404,
                type_="invalid_request_error",
                code="download_not_found",
            )
        return JSONResponse(job.view())

    @app.post("/admin/catalog/downloads/{job_id}:cancel")
    async def catalog_download_cancel(job_id: str) -> Response:
        job = downloads.cancel(job_id)
        if job is None:
            return _openai_error(
                f"unknown download {job_id!r}",
                status=404,
                type_="invalid_request_error",
                code="download_not_found",
            )
        return JSONResponse(job.view())

    # ── management plane: /admin/export + /admin/import (registry transfer) ──────
    # The browser assembles the cross-plane export bundle and reads this plane DIRECTLY —
    # registry state never transits the control plane. A named additive extension of the
    # management plane: camelCase, strict shapes, metadata only. Weights re-download here on
    # import (the installs.json provenance); credentials travel as names, never values.

    @app.get("/admin/export")
    async def export_registry() -> dict[str, Any]:
        return build_export(registry, install_store, credential_store)

    @app.post("/admin/import")
    async def import_registry(request: Request) -> Response:
        try:
            raw = await request.json()
        except Exception:
            raw = None
        try:
            bundle = ImportBundle.model_validate(raw)
        except ValidationError as exc:
            return _openai_error(
                f"invalid import bundle: {exc.errors()}",
                status=400,
                type_="invalid_request_error",
                code="invalid_bundle",
            )
        report = await apply_import(
            bundle,
            registry=registry,
            catalog=catalog,
            downloads=downloads,
            installs=install_store,
        )
        return JSONResponse(report)

    @app.get("/healthz")
    async def healthz() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/readyz")
    async def readyz() -> Response:
        body: dict[str, Any] = {}
        # Per-engine breakdown when the launcher is the dispatcher (the real app):
        # one engine missing (e.g. vLLM on a Mac) must not make the service not-ready.
        if isinstance(engine_launcher, ManagedLauncherSet):
            body["engines"] = {
                name: {"ready": r.ready, "reason": r.reason}
                for name, r in engine_launcher.readiness().items()
            }
        if engine_launcher.ready:
            body["status"] = "ready"
            return JSONResponse(body)
        body["status"] = "not-ready"
        body["reason"] = engine_launcher.not_ready_reason
        return JSONResponse(status_code=503, content=body)

    # ── data plane: /v1/* (OpenAI-compatible) ───────────────────────────

    @app.get("/v1/models")
    async def openai_models() -> dict[str, Any]:
        return {
            "object": "list",
            "data": [
                {"id": lid, "object": "model", "owned_by": "theygent"} for lid in registry.ids()
            ],
        }

    @app.post("/v1/chat/completions")
    async def chat_completions(req: ChatRequest) -> Response:
        # The `model` field is a LOGICAL id. An engine name (e.g. "llamacpp") is
        # simply not a registered id -> model_not_found. Engine names never reach here.
        try:
            binding = registry.require(req.model)
        except UnknownLogicalId:
            return _openai_error(
                f"unknown logical id {req.model!r} (the model field is a logical id, "
                "not an engine name)",
                status=404,
                type_="invalid_request_error",
                code="model_not_found",
            )

        params = merge_params(binding.params, req.model_dump())

        # Every error from the first dispatch call must map to a clean OpenAI-style error,
        # not an opaque 500 — and that now covers streams too: gateway.stream awaits the upstream
        # call BEFORE the serve helpers commit the SSE response, so a rejected request (bad param,
        # bad credential) raises here instead of tearing an already-started stream. The full
        # data-plane mapper also covers a lease acquired at this level failing capacity/engine
        # checks that warm passed a moment earlier (two requests racing one slot). Errors after
        # the stream body has begun are the guarded relay's job — a final structured error frame
        # (see _guarded_sse).
        try:
            if isinstance(binding, ManagedBinding):
                return await _serve_managed(req, params)
            return await _serve_reachable(binding, req, params)
        except Exception as exc:
            mapped = _data_plane_error(exc, req.model)
            if mapped is None:
                raise
            return mapped

    async def _serve_managed(req: ChatRequest, params: dict[str, Any]) -> Response:
        # Spawn + resolve capacity BEFORE committing to a response. For a stream,
        # the 200/text-event-stream status flushes before the body generator runs,
        # so a spawn/capacity failure must surface here as a clean error, not as a
        # 200 followed by a broken stream.
        try:
            await manager.warm(req.model)
        except EngineUnavailableError as exc:
            return _engine_unavailable(exc)
        except NoCapacityError as exc:
            return _openai_error(str(exc), status=503, type_="server_error", code="no_capacity")
        except NotManagedError:  # pragma: no cover - guarded by isinstance above
            return _openai_error(
                "binding is not managed", status=500, type_="server_error", code="not_managed"
            )

        if req.stream:
            # The lease finds the warmed engine resident (no re-spawn) and holds the
            # in-flight slot for the whole stream; it lives on an exit stack because it
            # must span both the eager upstream call below AND the response body. On
            # close a draining engine is torn down. Three close paths, because aclose is
            # idempotent: a rejected request closes here; a consumed stream closes in the
            # relay's finally; and the response background task covers the edge where the
            # committed body generator is never iterated at all (a client gone before the
            # first body step) — a never-started generator's finally never runs.
            stack = contextlib.AsyncExitStack()
            upstream = await stack.enter_async_context(manager.lease(req.model))
            try:
                # Awaited BEFORE the SSE response is committed — an upstream rejection
                # raises to the endpoint's error mapping instead of tearing the stream.
                lines = await gateway.stream(upstream, req.messages, params)
            except BaseException:
                await stack.aclose()
                raise
            return StreamingResponse(
                _guarded_sse(lines, req.model, close=stack.aclose),
                media_type="text/event-stream",
                background=BackgroundTask(stack.aclose),
            )

        async with manager.lease(req.model) as upstream:
            result = await gateway.complete(upstream, req.messages, params)
        return JSONResponse(result)

    async def _serve_reachable(binding: Any, req: ChatRequest, params: dict[str, Any]) -> Response:
        try:
            api_key = resolve_credential(binding.credential_ref, credential_store) or "sk-noauth"
        except CredentialResolutionError as exc:
            return _openai_error(
                str(exc), status=502, type_="server_error", code="credential_error"
            )
        upstream = Upstream(api_base=binding.base_url, model=binding.model, api_key=api_key)
        if req.stream:
            # Awaited BEFORE the SSE response is committed — a hosted API rejecting the
            # request (an unsupported param, a bad key) raises to the endpoint's error
            # mapping with its real message instead of tearing an already-started stream.
            lines = await gateway.stream(upstream, req.messages, params)
            return StreamingResponse(_guarded_sse(lines, req.model), media_type="text/event-stream")
        result = await gateway.complete(upstream, req.messages, params)
        return JSONResponse(result)

    # ── data plane: embeddings + audio ──────────────────────────────────
    # Same logical-id resolution + managed/reachable dispatch as chat, factored into one lease
    # helper. The `model` field is a LOGICAL id on these too — an engine name is simply not a
    # registered id (model_not_found), never rewritten onto the wire. These are non-streaming
    # awaited calls, so a spawn/capacity failure surfaces as a clean error before the response is
    # built (no pre-commit dance needed).

    @contextlib.asynccontextmanager
    async def _lease_for(model: str):
        """Yield ``(binding, upstream)`` for a logical id — warming + leasing a managed engine, or
        resolving the reachable upstream's credential locally (credentials stay in the inference
        plane's trust domain, never crossing to the control plane). Raises ``UnknownLogicalId`` /
        ``EngineUnavailableError`` / ``NoCapacityError`` / ``CredentialResolutionError`` for the
        caller to map, exactly like the chat path."""
        binding = registry.require(model)
        if isinstance(binding, ManagedBinding):
            await manager.warm(model)
            async with manager.lease(model) as upstream:
                yield binding, upstream
        else:
            api_key = resolve_credential(binding.credential_ref, credential_store) or "sk-noauth"
            yield binding, Upstream(api_base=binding.base_url, model=binding.model, api_key=api_key)

    def _upstream_error(exc: Exception, model: str) -> JSONResponse | None:
        """Map a provider/engine HTTP error (raised by litellm inside the gateway) to a CLEAN
        OpenAI-style error instead of letting it bubble to an opaque 500. Duck-typed on
        ``status_code`` so it covers litellm/openai/httpx status errors without importing their
        class hierarchy. A 404 from the engine almost always means the engine doesn't serve THIS
        endpoint/modality (e.g. embeddings or audio against a chat-only mlx_lm/llama.cpp text
        engine) — surface that honestly so a builder knows the cause, not a stacktrace."""
        status = getattr(exc, "status_code", None)
        if not isinstance(status, int):
            return None
        upstream_msg = getattr(exc, "message", None) or str(exc)
        if status == 404:
            return _openai_error(
                f"the engine serving {model!r} returned 404 for this endpoint — it likely does "
                "not support this modality (managed mlx_lm / llama.cpp text engines serve chat "
                "only; embeddings/audio need a model whose engine supports them), or, for a "
                "reachable openai-compatible binding, the baseUrl may be missing its API prefix "
                f"(hosted APIs usually need a trailing /v1). upstream: {upstream_msg}",
                status=404,
                type_="invalid_request_error",
                code="modality_not_supported",
            )
        # Relay other provider errors honestly: pass client-class statuses through, fold the rest
        # to a 502 (a bad upstream response), never a bare 500.
        out_status = status if status in (400, 401, 403, 408, 409, 422, 429, 503) else 502
        kind = "invalid_request_error" if out_status < 500 else "server_error"
        return _openai_error(upstream_msg, status=out_status, type_=kind, code="upstream_error")

    def _data_plane_error(exc: Exception, model: str) -> JSONResponse | None:
        if isinstance(exc, UnknownLogicalId):
            return _openai_error(
                f"unknown logical id {model!r} (the model field is a logical id, "
                "not an engine name)",
                status=404,
                type_="invalid_request_error",
                code="model_not_found",
            )
        if isinstance(exc, EngineUnavailableError):
            return _engine_unavailable(exc)
        if isinstance(exc, NoCapacityError):
            return _openai_error(str(exc), status=503, type_="server_error", code="no_capacity")
        if isinstance(exc, CredentialResolutionError):
            return _openai_error(
                str(exc), status=502, type_="server_error", code="credential_error"
            )
        # Fall through to the provider/engine HTTP-error mapper (litellm) — no opaque 500.
        return _upstream_error(exc, model)

    @app.post("/v1/embeddings")
    async def embeddings(req: EmbeddingsRequest) -> Response:
        try:
            async with _lease_for(req.model) as (binding, upstream):
                params = merge_params(binding.params, req.model_dump())
                params.pop("input", None)
                result = await gateway.embed(upstream, req.input, params)
            return JSONResponse(result)
        except Exception as exc:
            mapped = _data_plane_error(exc, req.model)
            if mapped is None:
                raise
            return mapped

    @app.post("/v1/audio/transcriptions")
    async def transcriptions(request: Request) -> Response:
        form = await request.form()
        model = form.get("model")
        upload = form.get("file")
        if not isinstance(model, str):
            return _openai_error(
                "`model` (a logical id) is required",
                status=422,
                type_="invalid_request_error",
                code="invalid_request",
            )
        if upload is None or isinstance(upload, str):
            return _openai_error(
                "`file` (multipart audio) is required",
                status=422,
                type_="invalid_request_error",
                code="invalid_request",
            )
        data = await upload.read()
        file_tuple = (
            upload.filename or "audio.wav",
            data,
            upload.content_type or "application/octet-stream",
        )
        # Remaining form fields (language / prompt / temperature / response_format / …) forward as
        # transcription params; model + file are routing/payload, not params.
        extra = {k: v for k, v in form.items() if k not in ("model", "file")}
        try:
            async with _lease_for(model) as (binding, upstream):
                params = merge_params(binding.params, extra)
                result = await gateway.transcribe(upstream, file_tuple, params)
            return JSONResponse(result)
        except Exception as exc:
            mapped = _data_plane_error(exc, model)
            if mapped is None:
                raise
            return mapped

    @app.post("/v1/audio/speech")
    async def speech(req: SpeechRequest) -> Response:
        try:
            async with _lease_for(req.model) as (binding, upstream):
                # exclude_none: an omitted voice must not shadow the binding's registered default.
                # Voice resolution: request voice → binding params default → NONE, in which case
                # the gateway posts voiceless and the engine's own default speaks (voice names are
                # engine-specific; nothing here invents one).
                params = merge_params(binding.params, req.model_dump(exclude_none=True))
                params.pop("input", None)
                audio = await gateway.speak(upstream, req.input, params)
            fmt = str(params.get("response_format", "mp3"))
            return Response(
                content=audio, media_type=_AUDIO_MIME.get(fmt, "application/octet-stream")
            )
        except Exception as exc:
            mapped = _data_plane_error(exc, req.model)
            if mapped is None:
                raise
            return mapped

    @app.post("/v1/images/generations")
    async def images_generations(req: ImagesRequest) -> Response:
        try:
            async with _lease_for(req.model) as (binding, upstream):
                params = merge_params(binding.params, req.model_dump())
                params.pop("prompt", None)
                result = await gateway.generate_image(upstream, req.prompt, params)
            return JSONResponse(result)
        except Exception as exc:
            mapped = _data_plane_error(exc, req.model)
            if mapped is None:
                raise
            return mapped

    return app
