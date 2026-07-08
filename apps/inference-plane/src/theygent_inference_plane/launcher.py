"""The EngineLauncher seam + the managed engines (llama.cpp, MLX, vLLM).

This is the load-bearing seam (the ``EngineLauncher`` protocol): every managed
engine implements the *same* ``EngineLauncher`` protocol, so the fast suite is
everything-real-except-the-weights via a fake, and the ``EngineManager`` stays
engine-agnostic. Adding MLX (engine #2) and vLLM (#3) required **zero EngineManager
caller changes** — the manager still calls a single ``launch(binding)``; dispatch to
the right engine lives in ``ManagedLauncherSet``.

Engine-specific reality (do not assume one engine's shape for another):
  * llama.cpp — capabilities via ``/props`` (real ``n_ctx``).
  * MLX       — ``/health`` exists but there is **no** ``/props``; max context is read
                from the model config, other caps are conservative + ``approximate``.
  * vLLM      — CUDA-canonical and UNPROVEN here; see ``VllmLauncher``.
"""

from __future__ import annotations

import asyncio
import contextlib
import glob
import json
import os
import shutil
import socket
import subprocess
import tempfile
from dataclasses import dataclass
from typing import IO, Protocol, runtime_checkable

import httpx
from theygent_ir import Capabilities, ManagedBinding

from theygent_inference_plane.binary import (
    EngineBinaryNotFound,
    LlamaServerNotFound,
    resolve_engine_command,
    resolve_llama_server_binary,
)
from theygent_inference_plane.capabilities import (
    REASONING_MARKERS,
    template_implies_reasoning,
)


@runtime_checkable
class EngineHandle(Protocol):
    """A live engine instance. The manager tracks one of these per resident model."""

    @property
    def base_url(self) -> str:
        """Root URL of the OpenAI-compatible upstream (no trailing /v1)."""
        ...

    async def health(self) -> bool: ...

    async def capabilities(self) -> Capabilities: ...

    async def terminate(self) -> None: ...


class EngineLauncher(Protocol):
    """Launches a managed engine for a binding. Implementations: LlamaCpp, MLX (real),
    vLLM (interface-only/unproven), FakeUpstream (test)."""

    @property
    def ready(self) -> bool:
        """False means a prerequisite (e.g. the binary) is missing -> /readyz."""
        ...

    @property
    def not_ready_reason(self) -> str | None: ...

    async def launch(self, binding: ManagedBinding) -> EngineHandle: ...


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


async def _health_ok(base_url: str, health_path: str = "/health") -> bool:
    try:
        async with httpx.AsyncClient(timeout=2.0) as client:
            return (await client.get(f"{base_url}{health_path}")).status_code == 200
    except httpx.HTTPError:
        return False


def _output_tail(log: IO[bytes], *, limit: int = 2000) -> str:
    """Best-effort tail of a dead/terminated child's captured output, for the crash message.
    Only safe to call once the child is gone (parent and child share the file offset)."""
    try:
        log.seek(0)
        data = log.read()
    except OSError:
        return ""
    if not data:
        return ""
    text = data.decode("utf-8", "replace").strip()
    return f"\n--- engine output (tail) ---\n{text[-limit:]}" if text else ""


async def _spawn_openai_server(
    cmd: list[str], base_url: str, *, startup_timeout: float, health_path: str = "/health"
) -> subprocess.Popen[bytes]:
    """Spawn an OpenAI-compatible server subprocess and wait until it is healthy.

    stdout+stderr go to an anonymous temp file rather than being discarded: when the engine
    crashes at startup (a missing Metal/CUDA runtime, an unresolvable model, bad flags) its
    traceback is the only thing that says *why*, so we fold the tail into the raised error. A
    temp file (not a PIPE) is used deliberately — a long-lived server whose output nobody drains
    would eventually block on a full pipe buffer. The parent's handle is dropped once startup
    resolves; the child keeps its own fd and the file is reclaimed when the engine exits.
    """
    log = tempfile.TemporaryFile()
    try:
        proc = subprocess.Popen(  # noqa: ASYNC220 — supervised long-lived child
            cmd, stdout=log, stderr=subprocess.STDOUT
        )
        loop = asyncio.get_event_loop()
        deadline = loop.time() + startup_timeout
        while loop.time() < deadline:
            if proc.poll() is not None:
                raise RuntimeError(
                    f"engine exited early (code {proc.returncode}): {cmd[0]}{_output_tail(log)}"
                )
            if await _health_ok(base_url, health_path):
                return proc
            await asyncio.sleep(0.25)
        proc.terminate()
        raise TimeoutError(
            f"engine {cmd[0]} did not become ready within {startup_timeout}s{_output_tail(log)}"
        )
    finally:
        # Drop the parent's copy of the fd. On success the child keeps its own dup and logs into
        # the (now-anonymous) file for its lifetime; on failure we've already read the tail above.
        log.close()


class _SubprocessHandle:
    """Shared lifecycle for a spawned OpenAI-compatible server. Subclasses provide
    the engine-specific ``capabilities`` probe. ``health_path`` covers servers with no
    ``/health`` route (any stable 200 route works — liveness, not readiness)."""

    def __init__(
        self, proc: subprocess.Popen[bytes], base_url: str, *, health_path: str = "/health"
    ) -> None:
        self._proc = proc
        self._base_url = base_url
        self._health_path = health_path

    @property
    def base_url(self) -> str:
        return self._base_url

    @property
    def pid(self) -> int:
        return self._proc.pid

    async def health(self) -> bool:
        return await _health_ok(self._base_url, self._health_path)

    async def capabilities(self) -> Capabilities:  # pragma: no cover - overridden
        raise NotImplementedError

    async def terminate(self) -> None:
        if self._proc.poll() is not None:
            return
        self._proc.terminate()
        for _ in range(50):
            if self._proc.poll() is not None:
                return
            await asyncio.sleep(0.1)
        self._proc.kill()
        with contextlib.suppress(Exception):
            self._proc.wait(timeout=5)


# ── llama.cpp (managed, proven) ─────────────────────────────────────────


class LlamaCppHandle(_SubprocessHandle):
    def __init__(
        self, proc: subprocess.Popen[bytes], base_url: str, *, modality: str = "chat"
    ) -> None:
        super().__init__(proc, base_url)
        self._modality = modality

    async def capabilities(self) -> Capabilities:
        # llama-server exposes context size + the chat template via /props; tool-calling and
        # structured output are model-dependent, advertised conservatively (real probe TBD).
        max_context: int | None = None
        reasoning = False
        with contextlib.suppress(httpx.HTTPError, KeyError, ValueError):
            async with httpx.AsyncClient(timeout=2.0) as client:
                props = (await client.get(f"{self._base_url}/props")).json()
                max_context = props.get("default_generation_settings", {}).get("n_ctx")
                reasoning = _template_implies_reasoning(props.get("chat_template"))
        if self._modality == "embeddings":
            # An embeddings server (llama-server --embeddings) serves /v1/embeddings only — it
            # declares the embeddings modality EXPLICITLY (derivation leaves an explicit list
            # alone); chat-shaped flags don't apply. approximate: the chat caps aren't probed here.
            return Capabilities(
                modalities=["embeddings"], max_context=max_context, approximate=True
            )
        return Capabilities(
            tool_calling=True,
            structured_output=True,
            vision=False,
            reasoning=reasoning,
            max_context=max_context,
        )


class LlamaCppLauncher:
    """Spawns ``llama-server`` for the bound model and waits until it is ready."""

    def __init__(self, binary_path: str | None = None, *, startup_timeout: float = 120.0) -> None:
        self._startup_timeout = startup_timeout
        try:
            self._binary: str | None = resolve_llama_server_binary(binary_path)
            self._reason: str | None = None
        except LlamaServerNotFound as exc:
            self._binary = None
            self._reason = str(exc)

    @property
    def ready(self) -> bool:
        return self._binary is not None

    @property
    def not_ready_reason(self) -> str | None:
        return self._reason

    def _build_command(self, binding: ManagedBinding, port: int) -> list[str]:
        assert self._binary is not None
        cmd = [self._binary, "--host", "127.0.0.1", "--port", str(port)]
        if binding.source == "hf":
            cmd += ["-hf", binding.model]
        else:  # local-path | url
            cmd += ["-m", binding.model]
        if binding.modality == "embeddings":
            # llama.cpp is the "one binary, many modalities via flags" engine: the SAME
            # llama-server serves /v1/embeddings once embeddings mode is on. Pooling must not be
            # `none` for the OpenAI endpoint (it returns one vector per input) → `mean`.
            cmd += ["--embeddings", "--pooling", "mean"]
        return cmd

    async def launch(self, binding: ManagedBinding) -> EngineHandle:
        if self._binary is None:
            raise LlamaServerNotFound(self._reason or "llama-server unavailable")
        port = _free_port()
        base_url = f"http://127.0.0.1:{port}"
        proc = await _spawn_openai_server(
            self._build_command(binding, port), base_url, startup_timeout=self._startup_timeout
        )
        return LlamaCppHandle(proc, base_url, modality=binding.modality)


# ── whisper.cpp (the ggml-family speech-to-text server — the llamacpp audio.transcription
#    modality). Like mlx vision, this is a DIFFERENT program dispatched by the (engine, modality)
#    key: whisper-server is whisper.cpp's HTTP server, sibling to llama-server. It natively
#    exposes /health and serves its inference route at a configurable path, so
#    `--inference-path /v1/audio/transcriptions` makes it OpenAI-conformant for the gateway
#    (multipart in → {"text": …} out, verified against the real binary). `--convert` (when ffmpeg
#    is present) transcodes non-wav uploads — notably the browser microphone's webm/opus. ─────────


def locate_whisper_model(binding: ManagedBinding) -> str:
    """The whisper weights FILE for a binding — whisper-server takes a file, not a dir/repo.

    A catalog install lands a directory; pick the single weights file inside, preferring the
    whisper.cpp-native ``ggml-*.bin`` form (this server reads ggml .bin whisper models; a
    llama-style GGUF conversion of whisper fails its magic check). Ambiguity or absence raises
    loudly — a guessed model file is a silent wrong-model bug.
    """
    model, source = binding.model, binding.source
    if source == "local-path":
        if os.path.isfile(model):
            return model
        candidates = sorted(
            glob.glob(os.path.join(model, "*.bin")) + glob.glob(os.path.join(model, "*.gguf"))
        )
    else:  # hf — only what the hub cache already holds; weights are installed via the catalog.
        repo = "models--" + model.replace("/", "--")
        candidates = sorted(
            glob.glob(os.path.join(_hf_hub_dir(), repo, "snapshots", "*", "*.bin"))
            + glob.glob(os.path.join(_hf_hub_dir(), repo, "snapshots", "*", "*.gguf"))
        )
    if not candidates:
        raise RuntimeError(
            f"no whisper weights file (*.bin / *.gguf) found for {model!r} — install the model "
            "locally first (whisper.cpp reads its native ggml-*.bin form, e.g. from the "
            "ggerganov/whisper.cpp repository)"
        )
    ggml = [c for c in candidates if os.path.basename(c).startswith("ggml")]
    if len(ggml) == 1:
        return ggml[0]
    if len(candidates) == 1:
        return candidates[0]
    raise RuntimeError(
        f"ambiguous whisper weights for {model!r}: {[os.path.basename(c) for c in candidates]} — "
        "point the binding at the exact file"
    )


class WhisperCppHandle(_SubprocessHandle):
    async def capabilities(self) -> Capabilities:
        # A transcription server serves exactly one task; chat-shaped capability probes don't
        # apply (approximate: nothing beyond the declared modality is probed).
        return Capabilities(modalities=["audio.transcription"], approximate=True)


class WhisperCppLauncher:
    """Spawns ``whisper-server`` (whisper.cpp) for a speech-to-text binding."""

    ENV_VAR = "THEYGENT_WHISPERCPP_BIN"

    def __init__(self, binary_path: str | None = None, *, startup_timeout: float = 120.0) -> None:
        self._startup_timeout = startup_timeout
        try:
            self._command: list[str] | None = resolve_engine_command(
                exe_name="whisper-server",
                env_var=self.ENV_VAR,
                explicit=binary_path,
                install_hint="install whisper.cpp (e.g. `brew install whisper-cpp`)",
            )
            self._reason: str | None = None
        except EngineBinaryNotFound as exc:
            self._command = None
            self._reason = str(exc)

    @property
    def ready(self) -> bool:
        return self._command is not None

    @property
    def not_ready_reason(self) -> str | None:
        return self._reason

    def _build_command(self, binding: ManagedBinding, port: int) -> list[str]:
        assert self._command is not None
        cmd = [
            *self._command,
            "--host",
            "127.0.0.1",
            "--port",
            str(port),
            # Serve the OpenAI transcriptions route directly — the gateway's api_base ends in /v1
            # and LiteLLM appends /audio/transcriptions, landing exactly here.
            "--inference-path",
            "/v1/audio/transcriptions",
            "-m",
            locate_whisper_model(binding),
        ]
        if shutil.which("ffmpeg"):
            # Browser recordings arrive as webm/opus; whisper-server transcodes via ffmpeg when
            # told to. Without ffmpeg the server still runs — wav uploads only.
            cmd += ["--convert", "--tmp-dir", tempfile.gettempdir()]
        return cmd

    async def launch(self, binding: ManagedBinding) -> EngineHandle:
        if self._command is None:
            raise EngineBinaryNotFound(self._reason or "whisper-server unavailable")
        port = _free_port()
        base_url = f"http://127.0.0.1:{port}"
        proc = await _spawn_openai_server(
            self._build_command(binding, port), base_url, startup_timeout=self._startup_timeout
        )
        return WhisperCppHandle(proc, base_url)


# ── mlx-audio (the MLX audio.speech server — text-to-speech on Apple Silicon). A different
#    program dispatched by the (engine, modality) key, like mlx vision and llamacpp
#    transcription: mlx_audio.server natively serves the OpenAI /v1/audio/speech route (kokoro
#    and friends), loading the requested model per request from the `model` field the gateway
#    forwards (the registered binding.model rides through unchanged). It exposes no /health —
#    /v1/models is the stable liveness route. Its STT route exists but is NOT registered here:
#    its whisper loader failed against a real model, so transcription stays with whisper.cpp
#    until an mlx-audio run proves green ("type-checks" is not "works"). ─────────────────────────


class MlxAudioHandle(_SubprocessHandle):
    def __init__(self, proc: subprocess.Popen[bytes], base_url: str) -> None:
        super().__init__(proc, base_url, health_path="/v1/models")

    async def capabilities(self) -> Capabilities:
        # A speech server serves exactly one registered task; nothing else is probed.
        return Capabilities(modalities=["audio.speech"], approximate=True)


class MlxAudioLauncher:
    """Spawns ``mlx_audio.server`` for a text-to-speech binding (Apple Silicon)."""

    ENV_VAR = "THEYGENT_MLX_AUDIO_BIN"

    def __init__(self, binary_path: str | None = None, *, startup_timeout: float = 120.0) -> None:
        self._startup_timeout = startup_timeout
        try:
            self._command: list[str] | None = resolve_engine_command(
                exe_name="mlx_audio.server",
                env_var=self.ENV_VAR,
                module="mlx_audio.server",
                explicit=binary_path,
                install_hint=(
                    "install mlx-audio with its server extras (e.g. `uv tool install mlx-audio "
                    "--with uvicorn --with fastapi --with webrtcvad --with python-multipart "
                    "--with 'setuptools<81' --with 'misaki[en]'` plus spaCy's en_core_web_sm)"
                ),
            )
            self._reason: str | None = None
        except EngineBinaryNotFound as exc:
            self._command = None
            self._reason = str(exc)

    @property
    def ready(self) -> bool:
        return self._command is not None

    @property
    def not_ready_reason(self) -> str | None:
        return self._reason

    def _build_command(self, binding: ManagedBinding, port: int) -> list[str]:
        assert self._command is not None
        # The server is model-agnostic at spawn: it loads whatever `model` each request names,
        # and the gateway forwards the registered binding.model — so no model flag here.
        del binding
        return [*self._command, "--host", "127.0.0.1", "--port", str(port)]

    async def launch(self, binding: ManagedBinding) -> EngineHandle:
        if self._command is None:
            raise EngineBinaryNotFound(self._reason or "mlx_audio.server unavailable")
        port = _free_port()
        base_url = f"http://127.0.0.1:{port}"
        proc = await _spawn_openai_server(
            self._build_command(binding, port),
            base_url,
            startup_timeout=self._startup_timeout,
            health_path="/v1/models",
        )
        return MlxAudioHandle(proc, base_url)


# ── MLX (managed, the Tier-A milestone — proven on Apple Silicon) ────────


def _hf_hub_dir() -> str:
    if cache := os.environ.get("HUGGINGFACE_HUB_CACHE"):
        return cache
    if home := os.environ.get("HF_HOME"):
        return os.path.join(home, "hub")
    return os.path.expanduser("~/.cache/huggingface/hub")


def _fetch_hf_config(model: str) -> str | None:
    """Path to ``config.json`` for an HF repo, fetching just that file if not cached.

    Reads from the same HF hub cache the engine uses; downloads only the small config
    (never weights). Returns None on any failure (offline, hub error, no such file)."""
    try:
        from huggingface_hub import hf_hub_download

        return hf_hub_download(repo_id=model, filename="config.json")
    except Exception:
        return None


# ── reasoning ("thinking") detection from the chat template ─────────────────
# The pure detector lives in ``capabilities.py`` — shared with the HF catalog browser, which runs
# the SAME heuristic against Hugging Face metadata at browse time (no download). Re-exported here
# under the module-private names the probe and tests already use, so the probe's honest *local*
# signal (no network, no model-name guessing) and the catalog's browse-time hint can never drift
# apart.
_REASONING_MARKERS = REASONING_MARKERS
_template_implies_reasoning = template_implies_reasoning


def _locate_repo_file(model: str, source: str, filename: str) -> str | None:
    """Path to ``filename`` for a managed model, mirroring ``_read_model_max_context``'s lookup:
    next to a local model dir/file, else the HF hub snapshot cache, else fetch just that file."""
    if source == "local-path":
        base = model if os.path.isdir(model) else os.path.dirname(model)
        path = os.path.join(base, filename)
        return path if os.path.exists(path) else None
    repo = "models--" + model.replace("/", "--")
    hits = glob.glob(os.path.join(_hf_hub_dir(), repo, "snapshots", "*", filename))
    if hits:
        return hits[0]
    try:
        from huggingface_hub import hf_hub_download

        return hf_hub_download(repo_id=model, filename=filename)
    except Exception:
        return None


def _read_mlx_reasoning(model: str, source: str) -> bool:
    """Best-effort reasoning flag for an MLX model from its ``tokenizer_config.json`` chat template
    (mlx_lm exposes no capability endpoint, so we read the static config — like max context)."""
    path = _locate_repo_file(model, source, "tokenizer_config.json")
    if not path:
        return False
    try:
        with open(path) as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return False
    return _template_implies_reasoning(data.get("chat_template"))


def _read_model_max_context(model: str, source: str) -> int | None:
    """Real max context from the model's ``config.json`` (max_position_embeddings).

    mlx_lm.server exposes no capability endpoint (no /props), so the honest source of
    a *real* value is the model config — present locally because the engine serves it.
    Returns None when not locatable; the caller then falls back to a conservative value.
    """
    candidates: list[str] = []
    if source == "local-path":
        base = model if os.path.isdir(model) else os.path.dirname(model)
        candidates.append(os.path.join(base, "config.json"))
    else:  # hf
        repo = "models--" + model.replace("/", "--")
        candidates += glob.glob(os.path.join(_hf_hub_dir(), repo, "snapshots", "*", "config.json"))
        if not candidates:
            # mlx_lm.server fetches weights lazily (on first completion), so the local
            # snapshot can be absent at probe time and /health already 200s. Pull just
            # config.json — tiny metadata, not weights — so the REAL max context is
            # available without forcing a generation. Best-effort: None if offline.
            if fetched := _fetch_hf_config(model):
                candidates.append(fetched)
    for path in candidates:
        try:
            with open(path) as fh:
                data = json.load(fh)
        except (OSError, ValueError):
            continue
        mpe = data.get("max_position_embeddings")
        if isinstance(mpe, int) and mpe > 0:
            return mpe
    return None


class MlxHandle(_SubprocessHandle):
    def __init__(
        self, proc: subprocess.Popen[bytes], base_url: str, model: str, source: str
    ) -> None:
        super().__init__(proc, base_url)
        self._model = model
        self._source = source

    async def capabilities(self) -> Capabilities:
        # MLX has /health but NO /props (verified). max_context + reasoning come from the model's
        # static config (real); tool/structured/vision are conservative -> approximate=True.
        max_context = _read_model_max_context(self._model, self._source)
        reasoning = _read_mlx_reasoning(self._model, self._source)
        return Capabilities(
            tool_calling=True,
            structured_output=False,
            vision=False,
            reasoning=reasoning,
            max_context=max_context,
            approximate=True,
        )


class MlxLauncher:
    """Spawns ``mlx_lm.server`` (Apple Silicon, unified memory). Same EngineLauncher
    protocol and lifecycle shape as llama.cpp — the manager cannot tell them apart."""

    ENV_VAR = "THEYGENT_MLX_BIN"

    def __init__(self, binary_path: str | None = None, *, startup_timeout: float = 180.0) -> None:
        self._startup_timeout = startup_timeout
        try:
            self._command: list[str] | None = resolve_engine_command(
                exe_name="mlx_lm.server",
                env_var=self.ENV_VAR,
                module="mlx_lm.server",
                explicit=binary_path,
                install_hint="install mlx-lm (e.g. `uv tool install mlx-lm`)",
            )
            self._reason: str | None = None
        except EngineBinaryNotFound as exc:
            self._command = None
            self._reason = str(exc)

    @property
    def ready(self) -> bool:
        return self._command is not None

    @property
    def not_ready_reason(self) -> str | None:
        return self._reason

    def _build_command(self, binding: ManagedBinding, port: int) -> list[str]:
        assert self._command is not None
        # mlx_lm.server takes the model id/path directly; for local-path it is the
        # model dir. Unified memory => no GPU/VRAM flags (that's vLLM's concern).
        return [
            *self._command,
            "--model",
            binding.model,
            "--host",
            "127.0.0.1",
            "--port",
            str(port),
        ]

    async def launch(self, binding: ManagedBinding) -> EngineHandle:
        if self._command is None:
            raise EngineBinaryNotFound(self._reason or "mlx_lm.server unavailable")
        port = _free_port()
        base_url = f"http://127.0.0.1:{port}"
        proc = await _spawn_openai_server(
            self._build_command(binding, port), base_url, startup_timeout=self._startup_timeout
        )
        return MlxHandle(proc, base_url, model=binding.model, source=binding.source)


# ── MLX vision (managed — the `mlx` engine, `vision` modality) ───────────
# A DIFFERENT program from mlx_lm.server: mlx-vlm ships its own OpenAI-compatible server for
# vision-language models, served on /v1/chat/completions with image_url content parts (vision is a
# sub-capability of chat). Same EngineLauncher protocol + spawn lifecycle as mlx_lm; the
# manager dispatches to it purely by the (engine, modality) key and never learns there are two MLX
# servers (zero EngineManager caller changes).


class MlxVlmHandle(_SubprocessHandle):
    def __init__(
        self, proc: subprocess.Popen[bytes], base_url: str, model: str, source: str
    ) -> None:
        super().__init__(proc, base_url)
        self._model = model
        self._source = source

    async def capabilities(self) -> Capabilities:
        # vision=True → _derive_modalities yields ["chat", "vision"] (vision rides chat, not its own
        # endpoint). Like mlx_lm.server, mlx_vlm.server exposes no /props, so caps are conservative
        # + approximate; max context comes from the model config when locatable (None otherwise).
        max_context = _read_model_max_context(self._model, self._source)
        return Capabilities(
            tool_calling=False,
            structured_output=False,
            vision=True,
            max_context=max_context,
            approximate=True,
        )


class MlxVlmLauncher:
    """Spawns ``mlx_vlm.server`` for an Apple-Silicon VLM — the MLX ``vision`` modality.

    UNPROVEN until its env-gated integration test runs green here (mlx-vlm is not installed by
    default); "written + type-checks" is not "works" — a lesson learned from the first engine
    integration. Same protocol/lifecycle shape as ``MlxLauncher`` — the manager cannot tell them
    apart.
    """

    ENV_VAR = "THEYGENT_MLX_VLM_BIN"

    def __init__(self, binary_path: str | None = None, *, startup_timeout: float = 180.0) -> None:
        self._startup_timeout = startup_timeout
        try:
            self._command: list[str] | None = resolve_engine_command(
                exe_name="mlx_vlm.server",
                env_var=self.ENV_VAR,
                module="mlx_vlm.server",
                explicit=binary_path,
                install_hint="install mlx-vlm (e.g. `uv tool install mlx-vlm`)",
            )
            self._reason: str | None = None
        except EngineBinaryNotFound as exc:
            self._command = None
            self._reason = str(exc)

    @property
    def ready(self) -> bool:
        return self._command is not None

    @property
    def not_ready_reason(self) -> str | None:
        return self._reason

    def _build_command(self, binding: ManagedBinding, port: int) -> list[str]:
        assert self._command is not None
        # mlx_vlm.server takes the model id/path directly, like mlx_lm.server. Unified memory => no
        # GPU/VRAM flags (vLLM's concern). VLM weights/projector live in the one repo/dir.
        return [
            *self._command,
            "--model",
            binding.model,
            "--host",
            "127.0.0.1",
            "--port",
            str(port),
        ]

    async def launch(self, binding: ManagedBinding) -> EngineHandle:
        if self._command is None:
            raise EngineBinaryNotFound(self._reason or "mlx_vlm.server unavailable")
        port = _free_port()
        base_url = f"http://127.0.0.1:{port}"
        proc = await _spawn_openai_server(
            self._build_command(binding, port), base_url, startup_timeout=self._startup_timeout
        )
        return MlxVlmHandle(proc, base_url, model=binding.model, source=binding.source)


# ── engine dispatch (keeps EngineManager engine-agnostic) ───────────────


class EngineUnavailableError(RuntimeError):
    """Requested engine has no launcher registered, or its prerequisite is missing."""


@dataclass(frozen=True)
class EngineReadiness:
    ready: bool
    reason: str | None


class ManagedLauncherSet:
    """Routes ``launch(binding)`` to the per-``(engine, modality)`` launcher.

    Implements ``EngineLauncher`` itself, so ``EngineManager`` is handed a single launcher and never
    learns there are several engines OR several modalities — the whole reason engine #2/#3 (and now
    the non-chat modalities) dropped in without touching the manager. ``launch(binding)`` keeps its
    exact signature; the key is private to this set.

    The dispatch key spans ``(engine, modality)`` so one engine can
    serve several modalities by *different programs* (``mlx``: ``mlx_lm.server`` chat vs
    ``mlx_vlm.server`` vision) or the *same binary with different flags* (``llamacpp``:
    ``llama-server``, ``+ --embeddings``). A bare-string key (``"llamacpp"``) is normalized to
    ``(engine, "chat")``, so a chat-only set (and the fast-suite stubs) construct unchanged.

    **Fail-closed dispatch (reject, don't serve wrong).** Lookup is on the EXACT
    ``(engine, modality)`` key — there is NO fallback to the chat launcher. An ``(mlx, embeddings)``
    binding when no ``(mlx, embeddings)`` launcher is registered is *rejected* (503), never silently
    spawned on ``mlx_lm.server`` (where an embeddings request would get a wrong, chat shape with no
    error). Chat needs no fallback: a chat binding's key is literally
    ``(engine, "chat")`` and matches directly. (llama.cpp chat+embeddings are BOTH registered keys
    pointing at the same launcher — explicit, not a fallback.)

    ``ready`` is aggregate (the service can serve if *any* (engine, modality) is available);
    ``readiness()`` gives the per-``(engine, modality)`` breakdown for /readyz, displaying the chat
    default as the bare engine name (so the existing /readyz shape is unchanged) and a non-chat
    modality as ``engine:modality`` (e.g. ``mlx:vision``).
    """

    def __init__(self, by_key: dict[tuple[str, str] | str, EngineLauncher]) -> None:
        self._by_key: dict[tuple[str, str], EngineLauncher] = {
            (k if isinstance(k, tuple) else (k, "chat")): v for k, v in by_key.items()
        }

    async def launch(self, binding: ManagedBinding) -> EngineHandle:
        # EXACT (engine, modality) — no chat fallback (fail-closed: reject, don't serve wrong).
        launcher = self._by_key.get((binding.binding, binding.modality))
        if launcher is None:
            raise EngineUnavailableError(
                f"no launcher registered for engine {binding.binding!r} modality "
                f"{binding.modality!r} — a non-chat modality is rejected here, never served by the "
                f"chat engine (fail-closed)"
            )
        if not launcher.ready:
            raise EngineUnavailableError(
                f"engine {binding.binding!r} ({binding.modality!r}) is not available on this host: "
                f"{launcher.not_ready_reason}"
            )
        return await launcher.launch(binding)

    @property
    def ready(self) -> bool:
        return any(launcher.ready for launcher in self._by_key.values())

    @property
    def not_ready_reason(self) -> str | None:
        if self.ready:
            return None
        return "; ".join(
            f"{self._display(key)}: {launcher.not_ready_reason}"
            for key, launcher in self._by_key.items()
        )

    @staticmethod
    def _display(key: tuple[str, str]) -> str:
        """The /readyz key: bare engine for chat (unchanged shape), else ``engine:modality``."""
        engine, modality = key
        return engine if modality == "chat" else f"{engine}:{modality}"

    def readiness(self) -> dict[str, EngineReadiness]:
        return {
            self._display(key): EngineReadiness(launcher.ready, launcher.not_ready_reason)
            for key, launcher in self._by_key.items()
        }
