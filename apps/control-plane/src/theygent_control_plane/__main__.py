"""Run the control-plane: ``python -m theygent_control_plane`` / ``theygent-control-plane``.

``THEYGENT_INFERENCE_PLANE_URL`` points at the inference plane's OpenAI-compatible data-plane
root (must include ``/v1``). The control-plane talks to it only over HTTP.

``DATABASE_URL`` is the Postgres store (async DSN, ``postgresql+asyncpg://…``);
the control-plane fails readiness if it is unset/unreachable. Apply migrations first with
``uv run --package theygent-control-plane alembic upgrade head``.

``THEYGENT_INVOKE_TOKEN`` is the bearer token gating the unattended deploy surfaces
(``POST /agents/{id}/invoke``). When UNSET those surfaces are closed (deny-by-default): set it
before exposing an agent for non-interactive invocation. The schedule dispatcher starts with
the app (in-process); webhook ``/hooks/{id}`` firing is authed per-webhook by the
signing secret in the trigger's config, not this token.

``THEYGENT_DURABLE=1`` opts the unattended fire path (triggers) AND the durable-run endpoint onto
the embedded DBOS runtime, so durable-only agents (loop/map/subgraph/human) can actually run.
Default OFF — the interactive ``/runs`` path is byte-for-byte the same in both modes.
"""

from __future__ import annotations

import os

import uvicorn

from theygent_control_plane.app import create_app


def _env_flag(name: str) -> bool:
    """A boolean env var: ``1``/``true``/``yes``/``on`` (case-insensitive) is True; anything else
    (including unset) is False."""
    return os.environ.get(name, "").strip().lower() in {"1", "true", "yes", "on"}


def _env_int(name: str, default: int) -> int:
    """An int env var where EMPTY equals UNSET (the deployment-manifest convention:
    ``VAR: ${VAR:-}`` passes an empty string through, and ``int("")`` would crash-loop the
    container)."""
    raw = (os.environ.get(name) or "").strip()
    return int(raw) if raw else default


def main() -> None:
    app = create_app(
        # `or` (not a get() default) so an EMPTY env var also falls back — empty == unset.
        inference_base_url=os.environ.get("THEYGENT_INFERENCE_PLANE_URL")
        or "http://127.0.0.1:8081/v1",
        durable=_env_flag("THEYGENT_DURABLE"),
    )
    uvicorn.run(
        app,
        host=os.environ.get("THEYGENT_CONTROL_PLANE_HOST") or "127.0.0.1",
        port=_env_int("THEYGENT_CONTROL_PLANE_PORT", 8080),
    )


if __name__ == "__main__":
    main()
