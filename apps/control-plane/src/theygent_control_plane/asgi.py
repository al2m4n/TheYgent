"""ASGI entrypoint: ``uvicorn theygent_control_plane.asgi:app``.

Kept separate from ``app.py`` so importing the ``create_app`` factory (e.g. in tests)
doesn't construct a default app + gateway client as an import side effect.
"""

from __future__ import annotations

import os

from theygent_control_plane.app import create_app

app = create_app(
    inference_base_url=os.environ.get("THEYGENT_INFERENCE_PLANE_URL", "http://127.0.0.1:8081/v1"),
)
