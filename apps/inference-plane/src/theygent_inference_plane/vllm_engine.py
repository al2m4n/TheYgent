"""vLLM launcher — interface-only, CUDA-canonical, and UNPROVEN on this machine.

vLLM's slot in theygent is NVIDIA/CUDA high-throughput GPU serving (stack §9). This
launcher targets ``vllm serve`` on a CUDA host. Its real path has **never run green
here** — its integration test is CUDA-gated and skips clean. "Written and type-checks"
is NOT "works" (the M1 lesson).

The governing rule (brief §3): **no CUDA/VRAM assumption may escape this module.** The
``EngineManager``, ``EvictionPolicy`` and gateway stay engine-agnostic; vLLM's GPU-ness
is this launcher's private business. In particular, ``vllm-metal`` (Apple Silicon, MLX
underneath) must NOT shape or "verify" this launcher — proving it on a Mac would be a
false green for the exact CUDA coupling (spawn flags, separate VRAM arbitration, driver
assumptions) that stays untested. That confusion is what this module is built to avoid.
"""

from __future__ import annotations

import subprocess

from theygent_ir import Capabilities, ManagedBinding

from theygent_inference_plane.binary import EngineBinaryNotFound, resolve_engine_command
from theygent_inference_plane.launcher import (
    EngineHandle,
    _free_port,
    _spawn_openai_server,
    _SubprocessHandle,
)


class VllmHandle(_SubprocessHandle):
    async def capabilities(self) -> Capabilities:
        # UNVERIFIED: vllm serve exposes no /props; a real probe (and real context
        # length) must be confirmed against a CUDA host. Until then, conservative.
        return Capabilities(
            tool_calling=True,
            structured_output=False,
            vision=False,
            max_context=None,
            approximate=True,
        )


class VllmLauncher:
    """Spawns ``vllm serve`` on a CUDA host. CUDA-canonical; UNPROVEN here.

    All NVIDIA/CUDA assumptions are confined to this class. The manager treats it as
    any other ``EngineLauncher``.
    """

    ENV_VAR = "THEYGENT_VLLM_BIN"

    def __init__(self, binary_path: str | None = None, *, startup_timeout: float = 600.0) -> None:
        self._startup_timeout = startup_timeout
        try:
            self._command: list[str] | None = resolve_engine_command(
                exe_name="vllm",
                env_var=self.ENV_VAR,
                explicit=binary_path,
                install_hint="install vLLM on a CUDA host (`pip install vllm`)",
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
        # `vllm serve <model>` exposes an OpenAI-compatible surface on a CUDA device.
        # Any CUDA-specific flags (tensor-parallel size, gpu-memory-utilization, dtype)
        # belong HERE and nowhere else. Kept minimal until validated on real hardware.
        return [*self._command, "serve", binding.model, "--host", "127.0.0.1", "--port", str(port)]

    async def launch(self, binding: ManagedBinding) -> EngineHandle:
        if self._command is None:
            raise EngineBinaryNotFound(self._reason or "vllm unavailable")
        port = _free_port()
        base_url = f"http://127.0.0.1:{port}"
        proc: subprocess.Popen[bytes] = await _spawn_openai_server(
            self._build_command(binding, port), base_url, startup_timeout=self._startup_timeout
        )
        return VllmHandle(proc, base_url)
