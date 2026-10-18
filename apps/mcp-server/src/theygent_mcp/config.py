"""Where the planes are and how to authenticate, from the environment (empty means unset)."""

from __future__ import annotations

import os
from dataclasses import dataclass

DEFAULT_CONTROL_PLANE_URL = "http://127.0.0.1:8080"
DEFAULT_INFERENCE_PLANE_URL = "http://127.0.0.1:8081/v1"
DEFAULT_HTTP_PORT = 8083
#: Agent runs can take minutes (a tool loop on a local model); model calls and listings are quick.
DEFAULT_RUN_TIMEOUT_S = 600.0


def _env(name: str) -> str | None:
    value = os.environ.get(name)
    return value.strip() if value and value.strip() else None


@dataclass(frozen=True)
class Config:
    control_plane_url: str = DEFAULT_CONTROL_PLANE_URL
    #: The inference plane's OpenAI-compatible root, ending in ``/v1`` (the same convention as the
    #: worker's ``THEYGENT_INFERENCE_PLANE_URL``); its management routes live beside it, at
    #: ``/admin``.
    inference_plane_url: str = DEFAULT_INFERENCE_PLANE_URL
    api_key: str | None = None
    default_model: str | None = None
    run_timeout_s: float = DEFAULT_RUN_TIMEOUT_S

    @property
    def inference_admin_url(self) -> str:
        base = self.inference_plane_url.rstrip("/")
        return base[: -len("/v1")] if base.endswith("/v1") else base

    @classmethod
    def from_env(cls) -> Config:
        timeout = _env("THEYGENT_MCP_RUN_TIMEOUT_S")
        return cls(
            control_plane_url=(
                _env("THEYGENT_CONTROL_PLANE_URL") or DEFAULT_CONTROL_PLANE_URL
            ).rstrip("/"),
            inference_plane_url=(
                _env("THEYGENT_INFERENCE_PLANE_URL") or DEFAULT_INFERENCE_PLANE_URL
            ).rstrip("/"),
            api_key=_env("THEYGENT_API_KEY"),
            default_model=_env("THEYGENT_MCP_DEFAULT_MODEL"),
            run_timeout_s=float(timeout) if timeout else DEFAULT_RUN_TIMEOUT_S,
        )
