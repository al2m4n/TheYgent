"""ASGI entrypoint for production servers: ``uvicorn theygent_inference_plane.asgi:app``.

Kept separate from ``app.py`` so importing the ``create_app`` factory (e.g. in tests)
doesn't construct a default app + real launchers as an import side effect.
"""

from __future__ import annotations

from theygent_inference_plane.app import create_app

app = create_app()
