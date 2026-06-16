"""Resolve the ``llama-server`` binary — never build/download/bundle it.

Resolution order (theygent-stack.md §5; plan "Binary resolution"):
  1. explicit path (constructor arg / THEYGENT_LLAMACPP_BIN)
  2. PATH lookup (shutil.which)
  3. neither -> fail loudly; the service surfaces this as /readyz not-ready.

Bundling the binary is a Tauri-sidecar concern, deferred.
"""

from __future__ import annotations

import os
import shutil

ENV_VAR = "THEYGENT_LLAMACPP_BIN"


class LlamaServerNotFound(RuntimeError):
    pass


def resolve_llama_server_binary(explicit: str | None = None) -> str:
    """Return an absolute path to ``llama-server`` or raise loudly."""

    candidate = explicit or os.environ.get(ENV_VAR)
    if candidate:
        if os.path.isfile(candidate) and os.access(candidate, os.X_OK):
            return os.path.abspath(candidate)
        raise LlamaServerNotFound(
            f"llama-server path {candidate!r} (from {ENV_VAR}/arg) is not an executable file"
        )

    found = shutil.which("llama-server")
    if found:
        return found

    raise LlamaServerNotFound(
        "llama-server not found; install llama.cpp or set "
        f"{ENV_VAR}. This service launches and supervises the binary; "
        "it does not build, download, or bundle it."
    )
