"""Resolve external engine binaries — never build/download/bundle them.

Resolution order (theygent-stack.md §5):
  1. explicit path (constructor arg / engine-specific env var)
  2. PATH lookup (shutil.which)
  3. optional ``python -m <module>`` fallback if the module is importable here
  4. none -> fail loudly; the service surfaces this as /readyz not-ready.

Bundling these binaries is a Tauri-sidecar concern, deferred. Each managed engine
(llama.cpp, MLX, vLLM) is an external server the user installs; we only launch and
supervise it.
"""

from __future__ import annotations

import importlib.util
import os
import shutil
import sys

ENV_VAR = "THEYGENT_LLAMACPP_BIN"


def _module_importable(module: str) -> bool:
    """True if ``module`` can be resolved. ``importlib.util.find_spec`` imports the
    parent package for a dotted name (e.g. ``mlx_lm.server`` imports ``mlx_lm``); a
    missing parent raises ``ModuleNotFoundError`` rather than returning ``None``, so we
    must catch it and treat the engine module as simply absent (the not-found path)."""
    try:
        return importlib.util.find_spec(module) is not None
    except (ImportError, ValueError):
        return False


class EngineBinaryNotFound(RuntimeError):
    """An engine's server binary/module could not be resolved on this host."""


class LlamaServerNotFound(EngineBinaryNotFound):
    pass


def resolve_engine_command(
    *,
    exe_name: str,
    env_var: str,
    module: str | None = None,
    explicit: str | None = None,
    install_hint: str,
    not_found_cls: type[EngineBinaryNotFound] = EngineBinaryNotFound,
) -> list[str]:
    """Resolve an engine server to a command prefix (argv list), or raise loudly.

    Returns a list so module-style servers (``python -m mlx_lm.server``) and plain
    binaries (``llama-server``) share one shape. The caller appends its own flags.
    """
    candidate = explicit or os.environ.get(env_var)
    if candidate:
        if os.path.isfile(candidate) and os.access(candidate, os.X_OK):
            return [os.path.abspath(candidate)]
        raise not_found_cls(
            f"{exe_name} path {candidate!r} (from {env_var}/arg) is not an executable file"
        )

    found = shutil.which(exe_name)
    if found:
        return [found]

    if module is not None and _module_importable(module):
        return [sys.executable, "-m", module]

    raise not_found_cls(
        f"{exe_name} not found; {install_hint} or set {env_var}. This service launches "
        "and supervises the binary; it does not build, download, or bundle it."
    )


def resolve_llama_server_binary(explicit: str | None = None) -> str:
    """Return an absolute path to ``llama-server`` or raise loudly (M1 compatibility)."""
    return resolve_engine_command(
        exe_name="llama-server",
        env_var=ENV_VAR,
        explicit=explicit,
        install_hint="install llama.cpp",
        not_found_cls=LlamaServerNotFound,
    )[0]
