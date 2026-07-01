"""Run the control-plane: ``python -m theygent_control_plane`` / ``theygent-control-plane``.

``THEYGENT_INFERENCE_PLANE_URL`` points at the inference plane's OpenAI-compatible data-plane
root (must include ``/v1``). The control-plane talks to it only over HTTP (§3.1).

``DATABASE_URL`` is the Postgres store (async DSN, ``postgresql+asyncpg://…`` — M4 §1.4);
the control-plane fails readiness if it is unset/unreachable. Apply migrations first with
``uv run --package theygent-control-plane alembic upgrade head``.

``THEYGENT_INVOKE_TOKEN`` (M12 §1.3) is the bearer token gating the unattended deploy surfaces
(``POST /agents/{id}/invoke``). When UNSET those surfaces are closed (deny-by-default): set it
before exposing an agent for non-interactive invocation. The M12 schedule dispatcher starts with
the app (in-process — m12.md §3); webhook ``/hooks/{id}`` firing is authed per-webhook by the
signing secret in the trigger's config, not this token.
"""

from __future__ import annotations

import os

import uvicorn

from theygent_control_plane.app import create_app


def main() -> None:
    app = create_app(
        inference_base_url=os.environ.get(
            "THEYGENT_INFERENCE_PLANE_URL", "http://127.0.0.1:8081/v1"
        ),
    )
    uvicorn.run(
        app,
        host=os.environ.get("THEYGENT_CONTROL_PLANE_HOST", "127.0.0.1"),
        port=int(os.environ.get("THEYGENT_CONTROL_PLANE_PORT", "8080")),
    )


if __name__ == "__main__":
    main()
