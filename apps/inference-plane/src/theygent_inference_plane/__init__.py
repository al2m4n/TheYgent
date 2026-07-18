"""theygent inference plane.

Two HTTP surfaces, never conflated:
  * data plane  /v1/*    — OpenAI-compatible; the `model` field is a LOGICAL id
  * management plane /admin/* — theygent-native registry / lifecycle / cache

See ``apps/inference-plane/CLAUDE.md`` for the frozen contract and the guardrails.
"""

from theygent_inference_plane.app import create_app

__all__ = ["create_app"]
