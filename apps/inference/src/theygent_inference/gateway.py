"""L0 gateway — the OpenAI-compatible dispatch built on LiteLLM.

LiteLLM is the routing/fallback/cost/cache layer (theygent-stack.md §7/§9); this
module is the thin adapter that points it at a resolved ``Upstream`` and reshapes
streaming chunks into SSE. It is engine-agnostic: it never sees `mlx`/`vllm`/
`llamacpp` — only an upstream URL produced by the manager (managed) or the
reachable binding (passthrough). The logical-id -> engine resolution already
happened before we get here.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from typing import Any

import litellm

from theygent_inference.manager import Upstream

# OpenAI request fields that are routing/identity, not generation params.
_RESERVED = frozenset({"model", "messages", "stream"})
# Minimal camelCase -> OpenAI snake_case normalization for binding default params.
_PARAM_ALIASES = {"maxTokens": "max_tokens", "topP": "top_p"}


def _to_dict(obj: Any) -> dict[str, Any]:
    if hasattr(obj, "model_dump"):
        return obj.model_dump()
    if hasattr(obj, "json"):
        return json.loads(obj.json())
    return dict(obj)


def merge_params(binding_params: dict[str, Any], request: dict[str, Any]) -> dict[str, Any]:
    """Binding defaults first, then the request's generation fields override."""
    merged: dict[str, Any] = {_PARAM_ALIASES.get(k, k): v for k, v in binding_params.items()}
    for key, value in request.items():
        if key not in _RESERVED:
            merged[key] = value
    return merged


class Gateway:
    """Stateless dispatcher. One instance shared across requests."""

    async def complete(
        self, upstream: Upstream, messages: list[dict[str, Any]], params: dict[str, Any]
    ) -> dict[str, Any]:
        resp = await litellm.acompletion(
            model=f"openai/{upstream.model}",
            api_base=upstream.api_base,
            api_key=upstream.api_key,
            messages=messages,
            **params,
        )
        return _to_dict(resp)

    async def stream(
        self, upstream: Upstream, messages: list[dict[str, Any]], params: dict[str, Any]
    ) -> AsyncIterator[str]:
        resp = await litellm.acompletion(
            model=f"openai/{upstream.model}",
            api_base=upstream.api_base,
            api_key=upstream.api_key,
            messages=messages,
            stream=True,
            **params,
        )
        async for chunk in resp:
            yield f"data: {json.dumps(_to_dict(chunk))}\n\n"
        yield "data: [DONE]\n\n"

    # ── M18: embeddings + audio (same engine-agnostic dispatch as chat) ──────
    # These are OpenAI data-plane shapes the gateway forwards to the resolved upstream exactly like
    # chat: it never sees `mlx`/`vllm`/`llamacpp`, only the upstream URL the manager produced. The
    # `openai/` prefix makes LiteLLM append the right path (`/embeddings`, `/audio/*`) to api_base.

    async def embed(
        self, upstream: Upstream, inputs: str | list[str], params: dict[str, Any]
    ) -> dict[str, Any]:
        resp = await litellm.aembedding(
            model=f"openai/{upstream.model}",
            api_base=upstream.api_base,
            api_key=upstream.api_key,
            input=inputs,
            **params,
        )
        return _to_dict(resp)

    async def transcribe(
        self, upstream: Upstream, file: Any, params: dict[str, Any]
    ) -> dict[str, Any]:
        # `file` is the OpenAI FileTypes tuple (filename, bytes, content_type) the endpoint builds
        # from the multipart upload. STT is request/response (no token stream), so one awaited call.
        resp = await litellm.atranscription(
            model=f"openai/{upstream.model}",
            api_base=upstream.api_base,
            api_key=upstream.api_key,
            file=file,
            **params,
        )
        return _to_dict(resp)

    async def speak(self, upstream: Upstream, text: str, params: dict[str, Any]) -> bytes:
        # TTS returns audio bytes (the OpenAI speech shape). LiteLLM hands back an
        # HttpxBinaryResponseContent; `.content` is the whole audio body.
        resp = await litellm.aspeech(
            model=f"openai/{upstream.model}",
            api_base=upstream.api_base,
            api_key=upstream.api_key,
            input=text,
            **params,
        )
        return resp.content
