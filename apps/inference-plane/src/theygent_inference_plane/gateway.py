"""L0 gateway — the OpenAI-compatible dispatch built on LiteLLM.

LiteLLM is the routing/fallback/cost/cache layer; this
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

import httpx
import litellm

from theygent_inference_plane import tool_parse
from theygent_inference_plane.manager import Upstream

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


def _voice_hint(params: dict[str, Any]) -> str:
    """Name the voice a failed synthesis actually asked for.

    Voice vocabularies are engine-specific — an OpenAI-style name like ``alloy`` is not a voice a
    local kokoro build has, and asking for one makes the server abort its already-200 chunked
    response with no message. Quoting the requested voice turns "some inputs trip an engine's text
    processing" into the one thing the caller can act on.
    """
    voice = params.get("voice")
    return f"voice ({voice!r} was requested)" if voice else "voice (none was requested)"


class UpstreamHttpError(RuntimeError):
    """An upstream HTTP error from a direct (non-dispatch-layer) call. Carries ``status_code`` +
    ``message`` in the same duck-typed shape the endpoint error mapping already reads, so a direct
    call's failure surfaces exactly like a dispatched one."""

    def __init__(self, status_code: int, message: str) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.message = message


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
        result = _to_dict(resp)
        # MLX-chat tool-call normalization: mlx_lm.server returns Llama's text tool
        # call as content, not structured tool_calls — rewrite it so the (engine-agnostic) control
        # plane sees the OpenAI shape. Gated to the MLX path + a tools-bearing request; a no-match
        # leaves the response byte-identical (see tool_parse).
        if upstream.needs_tool_parse and tool_parse.has_tools(params):
            return tool_parse.normalize_completion_dict(
                result, tool_parse.offered_tool_names(params)
            )
        return result

    async def stream(
        self, upstream: Upstream, messages: list[dict[str, Any]], params: dict[str, Any]
    ) -> AsyncIterator[str]:
        # The dispatch call is awaited HERE, not inside the returned generator. An async
        # generator runs nothing until its first __anext__ — by then the endpoint has already
        # committed a 200 SSE header, so an upstream rejection (bad param, bad credential,
        # unknown upstream model) could only surface as a torn stream with the real cause
        # lost. Raising from this await lets the endpoint map the error to the same
        # structured JSON as the non-stream path.
        resp = await litellm.acompletion(
            model=f"openai/{upstream.model}",
            api_base=upstream.api_base,
            api_key=upstream.api_key,
            messages=messages,
            stream=True,
            **params,
        )
        return self._sse_lines(resp, upstream, params)

    async def _sse_lines(
        self, resp: Any, upstream: Upstream, params: dict[str, Any]
    ) -> AsyncIterator[str]:
        if upstream.needs_tool_parse and tool_parse.has_tools(params):
            # Buffer + rewrite the MLX text tool call into synthetic structured tool_calls chunks;
            # a normal answer still streams (the rewriter flushes once it can't be a tool call).
            names = tool_parse.offered_tool_names(params)
            async for chunk in tool_parse.rewrite_mlx_tool_stream(resp, names, _to_dict):
                yield f"data: {json.dumps(chunk)}\n\n"
            yield "data: [DONE]\n\n"
            return
        async for chunk in resp:
            yield f"data: {json.dumps(_to_dict(chunk))}\n\n"
        yield "data: [DONE]\n\n"

    # ── Embeddings + audio (same engine-agnostic dispatch as chat) ──────────
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
        # TTS returns audio bytes (the OpenAI speech shape) — posted to the upstream DIRECTLY,
        # not through the dispatch layer, for two reasons observed against real engines:
        #   1. The dispatch layer hard-requires a voice string, but real engines don't — a local
        #      speech server synthesizes with its model's own default when no voice is named
        #      (voice vocabularies are engine-specific; inventing one here would name a voice
        #      the engine doesn't have).
        #   2. Speech servers stream the body and, on a synthesis error, abort AFTER the 200
        #      header (certain inputs trip an engine's text processing) — the dispatch layer
        #      swallows that abort into a silent empty body, where a direct read raises and maps
        #      to an honest, actionable upstream error.
        # An upstream that itself requires a voice answers with its own error, surfaced through
        # the same status mapping as every upstream error.
        try:
            async with httpx.AsyncClient(timeout=httpx.Timeout(600.0, connect=10.0)) as client:
                resp = await client.post(
                    f"{upstream.api_base}/audio/speech",
                    headers={"Authorization": f"Bearer {upstream.api_key}"},
                    json={"model": upstream.model, "input": text, **params},
                )
                if resp.status_code >= 400:
                    raise UpstreamHttpError(resp.status_code, resp.text[:500])
                audio = resp.content
        except httpx.HTTPError as exc:
            raise UpstreamHttpError(
                502,
                "the speech engine aborted mid-synthesis — the two usual causes are an unknown "
                f"{_voice_hint(params)} and an input its text processing can't handle; the engine "
                f"log names which. Transport: {exc}",
            ) from exc
        if not audio:
            raise UpstreamHttpError(
                502,
                f"the speech engine produced no audio — the two usual causes are an unknown "
                f"{_voice_hint(params)} and an input its text processing can't handle. If EVERY "
                "input fails, it is the voice or an unloadable model, not the text",
            )
        return audio

    async def generate_image(
        self, upstream: Upstream, prompt: str, params: dict[str, Any]
    ) -> dict[str, Any]:
        # Image generation returns the OpenAI images shape ({data: [{b64_json}]}) — posted to the
        # upstream DIRECTLY, like speak. The local generators are diffusion CLIs behind a bundled
        # wrapper server (not the dispatch layer), and a reachable image API is already this exact
        # OpenAI shape, so one direct POST covers both. Generation is minutes-scale per image and
        # loads weights per request, so the timeout is generous.
        try:
            async with httpx.AsyncClient(timeout=httpx.Timeout(1200.0, connect=10.0)) as client:
                resp = await client.post(
                    f"{upstream.api_base}/images/generations",
                    headers={"Authorization": f"Bearer {upstream.api_key}"},
                    json={"model": upstream.model, "prompt": prompt, **params},
                )
        except httpx.HTTPError as exc:
            raise UpstreamHttpError(
                502, f"the image engine was unreachable or aborted mid-generation: {exc}"
            ) from exc
        if resp.status_code >= 400:
            raise UpstreamHttpError(resp.status_code, resp.text[:500])
        result = _to_dict(resp.json())
        if not result.get("data"):
            raise UpstreamHttpError(
                502, "the image engine produced no image for this prompt (the engine log has why)"
            )
        return result
