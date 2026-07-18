"""Run the inference plane: ``python -m theygent_inference_plane`` /
``theygent-inference-plane``."""

from __future__ import annotations

import os
from pathlib import Path

import uvicorn

from theygent_inference_plane.app import create_app


def _env(name: str) -> str | None:
    """Read an env var with the empty-means-unset convention (every read here uses it).

    Deployment manifests pass UI-settable knobs through as ``${VAR:-}`` so the settings
    UI stays live by default — an empty value must behave exactly like an absent one
    (never "a value", and never crash an int parse into a container restart loop)."""
    value = (os.environ.get(name) or "").strip()
    return value or None


def _env_int(name: str) -> int | None:
    """Integer env read; unset/empty → ``None``. A garbage non-numeric value still fails
    loudly — a typo should be seen, only emptiness is the sanctioned "unset" spelling."""
    raw = _env(name)
    return int(raw) if raw is not None else None


def _state_dir() -> Path:
    """The inference plane's local state dir (the user's trust domain), never the control-plane DB.
    ``THEYGENT_INFERENCE_PLANE_STATE_DIR`` overrides the default (``~/.theygent/inference``)."""
    base = _env("THEYGENT_INFERENCE_PLANE_STATE_DIR")
    return Path(base) if base else Path.home() / ".theygent" / "inference"


def _state_path() -> Path:
    """Where the logical-model registry persists — under the local state dir."""
    return _state_dir() / "registry.json"


def _model_dir() -> Path:
    """Where downloaded model weights land (in-plane download). Defaults under the state dir;
    ``THEYGENT_INFERENCE_PLANE_MODEL_DIR`` overrides it (e.g. a large external volume)."""
    base = _env("THEYGENT_INFERENCE_PLANE_MODEL_DIR")
    return Path(base) if base else _state_dir() / "models"


def main() -> None:
    app = create_app(
        # None when the env var is unset/empty, so create_app resolves the ceiling itself
        # (stored settings.json value, else the default) and reports the source honestly on
        # /admin/settings; a set value pins the knob.
        max_resident=_env_int("THEYGENT_MAX_RESIDENT"),
        state_path=_state_path(),
        model_dir=_model_dir(),
    )
    uvicorn.run(
        app,
        host=_env("THEYGENT_INFERENCE_PLANE_HOST") or "127.0.0.1",
        port=_env_int("THEYGENT_INFERENCE_PLANE_PORT") or 8081,
    )


if __name__ == "__main__":
    main()
