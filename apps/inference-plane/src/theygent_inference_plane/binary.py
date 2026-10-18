"""Resolve external engine binaries — never build/download/bundle them.

Resolution order:
  1. explicit path (constructor arg / engine-specific env var)
  2. PATH lookup (shutil.which)
  3. optional ``python -m <module>`` fallback if the module is importable here
  4. none -> fail loudly; the service surfaces this as /readyz not-ready.

Bundling these binaries is a desktop-shell (Tauri sidecar) concern, out of scope here.
Each managed engine (llama.cpp, MLX, vLLM) is an external server the user installs; we
only launch and supervise it.
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


MLX_PYTHON_ENV_VAR = "THEYGENT_MLX_PYTHON"


def resolve_mlx_python(server_command: list[str]) -> str:
    """The Python interpreter mlx-lm is installed in, for servers TheYgent runs inside mlx-lm's
    environment (expert streaming). ``server_command`` is the resolved ``mlx_lm.server`` command.

    Resolution order: ``THEYGENT_MLX_PYTHON``; the interpreter of a ``python -m mlx_lm.server``
    command; the ``#!`` line of an ``mlx_lm.server`` script (an absolute interpreter, or
    ``/usr/bin/env <name>`` looked up on ``PATH``). Anything else fails loudly.
    """
    explicit = os.environ.get(MLX_PYTHON_ENV_VAR)
    if explicit:
        if os.path.isfile(explicit) and os.access(explicit, os.X_OK):
            return os.path.abspath(explicit)
        raise EngineBinaryNotFound(f"{MLX_PYTHON_ENV_VAR}={explicit!r} is not an executable file")
    if len(server_command) >= 3 and server_command[1] == "-m":
        return server_command[0]

    script = server_command[0]
    try:
        with open(script, "rb") as fh:
            first_line = fh.readline(512)
    except OSError as exc:
        raise EngineBinaryNotFound(f"cannot read {script} to find mlx-lm's Python: {exc}") from exc
    interpreter = None
    if first_line.startswith(b"#!"):
        parts = first_line[2:].decode(errors="replace").split()
        if parts and os.path.basename(parts[0]) == "env":
            names = [p for p in parts[1:] if not p.startswith("-")]
            interpreter = shutil.which(names[0]) if names else None
        elif parts:
            interpreter = parts[0]
    if interpreter and os.path.isfile(interpreter) and os.access(interpreter, os.X_OK):
        return interpreter
    raise EngineBinaryNotFound(
        f"cannot tell which Python runs {script} (no usable #! line); set {MLX_PYTHON_ENV_VAR} "
        "to the interpreter mlx-lm is installed in"
    )


def resolve_llama_server_binary(explicit: str | None = None) -> str:
    """Return an absolute path to ``llama-server`` or raise loudly."""
    return resolve_engine_command(
        exe_name="llama-server",
        env_var=ENV_VAR,
        explicit=explicit,
        install_hint="install llama.cpp",
        not_found_cls=LlamaServerNotFound,
    )[0]
