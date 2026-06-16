"""The EngineLauncher seam — the manager depends on this, never on Popen.

This is the load-bearing seam (plan §"EngineLauncher protocol"): the real
``LlamaCppLauncher`` and the test ``FakeUpstreamLauncher`` are interchangeable,
so the fast suite is everything-real-except-the-weights, and MLX / vLLM launchers
slot in later without touching the manager.
"""

from __future__ import annotations

import asyncio
import contextlib
import socket
import subprocess
from typing import Protocol, runtime_checkable

import httpx
from theygent_ir import Capabilities, ManagedBinding

from theygent_inference.binary import LlamaServerNotFound, resolve_llama_server_binary


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
    """Launches a managed engine for a binding. Implementations: LlamaCpp (real),
    FakeUpstream (test); later MLX, vLLM."""

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


class LlamaCppHandle:
    def __init__(self, proc: subprocess.Popen[bytes], base_url: str) -> None:
        self._proc = proc
        self._base_url = base_url

    @property
    def base_url(self) -> str:
        return self._base_url

    @property
    def pid(self) -> int:
        return self._proc.pid

    async def health(self) -> bool:
        try:
            async with httpx.AsyncClient(timeout=2.0) as client:
                resp = await client.get(f"{self._base_url}/health")
                return resp.status_code == 200
        except httpx.HTTPError:
            return False

    async def capabilities(self) -> Capabilities:
        # llama-server exposes context size via /props; tool-calling and
        # structured output are supported by the server (model-dependent, so we
        # advertise them conservatively). A richer per-model probe lands later.
        max_context: int | None = None
        with contextlib.suppress(httpx.HTTPError, KeyError, ValueError):
            async with httpx.AsyncClient(timeout=2.0) as client:
                props = (await client.get(f"{self._base_url}/props")).json()
                max_context = props.get("default_generation_settings", {}).get("n_ctx")
        return Capabilities(
            tool_calling=True,
            structured_output=True,
            vision=False,
            max_context=max_context,
        )

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
        return cmd

    async def launch(self, binding: ManagedBinding) -> EngineHandle:
        if self._binary is None:
            raise LlamaServerNotFound(self._reason or "llama-server unavailable")
        port = _free_port()
        proc = subprocess.Popen(  # noqa: ASYNC220 — supervised long-lived child
            self._build_command(binding, port),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        handle = LlamaCppHandle(proc, base_url=f"http://127.0.0.1:{port}")
        deadline = asyncio.get_event_loop().time() + self._startup_timeout
        while asyncio.get_event_loop().time() < deadline:
            if proc.poll() is not None:
                raise RuntimeError(f"llama-server exited early (code {proc.returncode})")
            if await handle.health():
                return handle
            await asyncio.sleep(0.25)
        await handle.terminate()
        raise TimeoutError(f"llama-server for {binding.model!r} did not become ready")
