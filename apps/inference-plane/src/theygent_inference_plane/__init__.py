"""theygent inference plane.

Two HTTP surfaces, never conflated:
  * data plane  /v1/*    — OpenAI-compatible; the `model` field is a LOGICAL id
  * management plane /admin/* — theygent-native registry / lifecycle / cache

See ``apps/inference-plane/CLAUDE.md`` for the frozen contract and the guardrails.

``create_app`` is re-exported lazily: every submodule import (and every ``python -m
theygent_inference_plane.<module>`` spawn, such as the stdlib-only ``image_server``
wrapper) executes this file first, so it must not pull in ``app`` and its LiteLLM-backed
gateway — that import alone costs seconds on each cold engine spawn.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from theygent_inference_plane.app import create_app

__all__ = ["create_app"]


def __getattr__(name: str) -> Any:
    if name == "create_app":
        from theygent_inference_plane.app import create_app

        return create_app
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
