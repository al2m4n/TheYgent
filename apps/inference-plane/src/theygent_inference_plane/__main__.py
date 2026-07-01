"""Run the inference plane: ``python -m theygent_inference_plane`` /
``theygent-inference-plane``."""

from __future__ import annotations

import os
from pathlib import Path

import uvicorn

from theygent_inference_plane.app import create_app


def _state_dir() -> Path:
    """The inference plane's local state dir (the user's trust domain), never the control-plane DB.
    ``THEYGENT_INFERENCE_PLANE_STATE_DIR`` overrides the default (``~/.theygent/inference``)."""
    base = os.environ.get("THEYGENT_INFERENCE_PLANE_STATE_DIR")
    return Path(base) if base else Path.home() / ".theygent" / "inference"


def _state_path() -> Path:
    """Where the logical-model registry persists (M9 §2.3) — under the local state dir."""
    return _state_dir() / "registry.json"


def _model_dir() -> Path:
    """Where M16-installed model weights land (downloaded in-plane). Defaults under the state dir;
    ``THEYGENT_INFERENCE_PLANE_MODEL_DIR`` overrides it (e.g. a large external volume)."""
    base = os.environ.get("THEYGENT_INFERENCE_PLANE_MODEL_DIR")
    return Path(base) if base else _state_dir() / "models"


def main() -> None:
    app = create_app(
        max_resident=int(os.environ.get("THEYGENT_MAX_RESIDENT", "2")),
        state_path=_state_path(),
        model_dir=_model_dir(),
    )
    uvicorn.run(
        app,
        host=os.environ.get("THEYGENT_INFERENCE_PLANE_HOST", "127.0.0.1"),
        port=int(os.environ.get("THEYGENT_INFERENCE_PLANE_PORT", "8081")),
    )


if __name__ == "__main__":
    main()
