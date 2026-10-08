"""ASGI entrypoint for production servers: ``uvicorn theygent_ai_gateway.asgi:app``.

Kept separate from ``app.py`` so importing the ``create_app`` factory (e.g. in tests) doesn't
read the environment and build a default app as an import side effect.
"""

from __future__ import annotations

from theygent_ai_gateway.app import create_app

app = create_app()
