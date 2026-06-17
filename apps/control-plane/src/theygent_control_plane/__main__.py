"""Run the control-plane: ``python -m theygent_control_plane`` / ``theygent-control-plane``.

``THEYGENT_INFERENCE_URL`` points at the inference plane's OpenAI-compatible data-plane
root (must include ``/v1``). The control-plane talks to it only over HTTP (§3.1).

``DATABASE_URL`` is the Postgres store (async DSN, ``postgresql+asyncpg://…`` — M4 §1.4);
the control-plane fails readiness if it is unset/unreachable. Apply migrations first with
``uv run --package theygent-control-plane alembic upgrade head``.
"""

from __future__ import annotations

import os

import uvicorn

from theygent_control_plane.app import create_app


def main() -> None:
    app = create_app(
        inference_base_url=os.environ.get("THEYGENT_INFERENCE_URL", "http://127.0.0.1:8081/v1"),
    )
    uvicorn.run(
        app,
        host=os.environ.get("THEYGENT_CONTROL_PLANE_HOST", "127.0.0.1"),
        port=int(os.environ.get("THEYGENT_CONTROL_PLANE_PORT", "8080")),
    )


if __name__ == "__main__":
    main()
