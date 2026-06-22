"""Run the inference plane: ``python -m theygent_inference`` / ``theygent-inference``."""

from __future__ import annotations

import os
from pathlib import Path

import uvicorn

from theygent_inference.app import create_app


def _state_path() -> Path:
    """Where the logical-model registry persists (M9 §2.3) — LOCAL to the inference plane (the
    user's trust domain), never the control-plane DB. ``THEYGENT_INFERENCE_STATE_DIR`` overrides
    the default (``~/.theygent/inference``); the registry file lives under it."""
    base = os.environ.get("THEYGENT_INFERENCE_STATE_DIR")
    state_dir = Path(base) if base else Path.home() / ".theygent" / "inference"
    return state_dir / "registry.json"


def main() -> None:
    app = create_app(
        max_resident=int(os.environ.get("THEYGENT_MAX_RESIDENT", "2")),
        state_path=_state_path(),
    )
    uvicorn.run(
        app,
        host=os.environ.get("THEYGENT_INFERENCE_HOST", "127.0.0.1"),
        port=int(os.environ.get("THEYGENT_INFERENCE_PORT", "8081")),
    )


if __name__ == "__main__":
    main()
