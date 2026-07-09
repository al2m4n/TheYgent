"""GatewayClient — the transport-only OpenAI-compatible client.

Wraps the official async ``openai`` SDK pointed at the inference plane's ``/v1/*``
data plane. The SDK is deliberate: it speaks the exact OpenAI shape the inference-plane
seam promises, so using it from the control-plane *is* the end-to-end proof that the seam
is OpenAI-compatible (and we get streaming + types for free).

Invariants this module holds:

* **Transport-only.** No ``Run``, no registry, no model resolution, no error policy.
  It forwards a logical model id + messages/params and hands back chunks/objects. The
  caller (control-plane) owns run identity and decides how to map errors. We even keep
  ``run_id`` out of the signature — callers pass an opaque ``extra_headers`` dict (the
  control-plane puts ``x-theygent-run-id`` there), so nothing run-shaped leaks in.
* **Errors surface, not swallowed.** A non-200 from inference (e.g. ``503
  engine_unavailable``, ``404 model_not_found``) raises ``openai.APIStatusError`` at the
  ``open_stream`` / ``complete`` ``await`` — *before* any chunk is yielded — so the
  caller can map it to a clean status before committing to a streaming response.
"""

from __future__ import annotations

import base64
from collections.abc import Mapping
from typing import Any

from openai import AsyncOpenAI
from openai.types.chat import ChatCompletion, ChatCompletionChunk

# OpenAI request fields that are routing/identity, not generation params. The caller
# passes generation params (temperature, max_tokens, …) via ``params``; these would
# collide with our explicit kwargs, so we drop them defensively.
_RESERVED = frozenset({"model", "messages", "stream", "extra_headers"})

# OpenAI-standard chat generation params — passed as named kwargs to ``chat.completions.create``.
# Anything else (an engine knob like ``chat_template_kwargs``, which toggles a model's hidden
# thinking phase on servers that gate it in the chat template) goes through ``extra_body`` so the
# SDK doesn't reject it — the SDK validates named kwargs against the OpenAI schema, exactly the
# constraint ``generate_image`` already handles this way.
_OPENAI_CHAT_PARAMS = frozenset(
    {
        "temperature",
        "top_p",
        "max_tokens",
        "max_completion_tokens",
        "stop",
        "n",
        "presence_penalty",
        "frequency_penalty",
        "seed",
        "user",
        "logit_bias",
        "logprobs",
        "top_logprobs",
        "response_format",
        "parallel_tool_calls",
        "reasoning_effort",
        "stream_options",
        "metadata",
        "modalities",
        "audio",
        "prediction",
        "service_tier",
        "store",
    }
)

# OpenAI-standard image-generation params — passed as named kwargs to ``images.generate``. Anything
# else (an engine knob like ``steps``) goes through ``extra_body`` so the SDK doesn't reject it.
_OPENAI_IMAGE_PARAMS = frozenset(
    {"size", "n", "quality", "style", "user", "background", "output_format", "output_compression"}
)
# The two fields ``generate_image`` always sets itself as named kwargs — dropped from a caller's
# params so they never collide with the explicit kwarg. Local to image generation: keeping them out
# of the shared ``_RESERVED`` means they still ride through on ``speak``/``transcribe`` (where
# ``response_format`` selects the audio/text format), which ``_clean_params`` must not strip.
_IMAGE_FORCED = frozenset({"prompt", "response_format"})


def _clean_params(params: Mapping[str, Any] | None) -> dict[str, Any]:
    if not params:
        return {}
    return {k: v for k, v in params.items() if k not in _RESERVED}


def _chat_kwargs(params: Mapping[str, Any] | None) -> dict[str, Any]:
    """Split chat params into OpenAI-standard named kwargs + an ``extra_body`` for everything
    else, so a non-standard engine param rides the request body instead of raising a
    ``TypeError`` on the SDK's typed ``create``. A caller-supplied ``extra_body`` dict is
    merged (its keys win over loose extras of the same name)."""
    clean = _clean_params(params)
    supplied = clean.pop("extra_body", None)
    standard = {k: v for k, v in clean.items() if k in _OPENAI_CHAT_PARAMS}
    extra = {k: v for k, v in clean.items() if k not in _OPENAI_CHAT_PARAMS}
    if isinstance(supplied, Mapping):
        extra.update(supplied)
    if extra:
        standard["extra_body"] = extra
    return standard


def _tool_kwargs(tools: list[dict[str, Any]] | None, tool_choice: Any) -> dict[str, Any]:
    """The OpenAI function-calling kwargs, forwarded to ``create`` only when set — so a
    tools-less call is byte-identical to a call without tools."""
    out: dict[str, Any] = {}
    if tools:
        out["tools"] = tools
        if tool_choice is not None:
            out["tool_choice"] = tool_choice
    return out


class GatewayClient:
    """Stateless OpenAI-compatible client for one inference ``baseUrl``.

    ``base_url`` must include the ``/v1`` suffix (the data-plane root), e.g.
    ``http://127.0.0.1:8081/v1``. ``api_key`` is a placeholder by default — the
    inference plane resolves any real upstream credential locally; the
    control-plane never holds one.
    """

    def __init__(
        self,
        base_url: str,
        *,
        api_key: str = "sk-noauth",
        timeout: float = 60.0,
        max_retries: int | None = None,
    ) -> None:
        # ``max_retries`` defaults to the OpenAI SDK's own (None → SDK default). The **durable**
        # runtime passes ``0`` so the SDK does NOT silently retry a failed inference call —
        # DBOS owns retry on the durable path, and a provider-side retry on top of a DBOS step retry
        # is a double-retry hazard.
        kwargs: dict[str, Any] = {"base_url": base_url, "api_key": api_key, "timeout": timeout}
        if max_retries is not None:
            kwargs["max_retries"] = max_retries
        self._client = AsyncOpenAI(**kwargs)

    async def open_stream(
        self,
        *,
        model: str,
        messages: list[dict[str, Any]],
        params: Mapping[str, Any] | None = None,
        extra_headers: Mapping[str, str] | None = None,
        tools: list[dict[str, Any]] | None = None,
        tool_choice: Any = None,
        include_usage: bool = False,
    ) -> Any:
        """Open a streaming completion and return the async stream of chunks.

        The ``await`` here sends the request and validates the response status, so a
        non-200 raises now (before iteration). The caller iterates the returned stream;
        a mid-stream failure raises during iteration.

        ``tools``/``tool_choice`` carry the OpenAI function-calling protocol verbatim —
        a NAMED transport-seam extension (not buried in ``params``) so the function-calling
        contract is explicit and type-checked. The inference plane forwards them to the engine
        (no inference-side change); a model that ignores them simply streams content. Forwarded
        only when set, so a tools-less call is byte-identical to a call without tools.

        ``include_usage`` requests token accounting on the stream via the OpenAI
        ``stream_options`` field: the server appends a final chunk carrying ``usage``
        (verified against real local engines, which emit it only when asked). Another
        named transport-seam extension — off by default, so a plain call is unchanged;
        an upstream that ignores the field simply streams without usage (the caller must
        treat missing usage as "not reported", never fail on it).
        """
        # The SDK's typed overloads can't model a dynamic **params splat alongside the
        # stream literal, so the checker can't pick an overload; the call is correct.
        return await self._client.chat.completions.create(  # ty: ignore[no-matching-overload]
            model=model,
            messages=messages,  # type: ignore[arg-type]
            stream=True,
            extra_headers=dict(extra_headers) if extra_headers else None,
            **({"stream_options": {"include_usage": True}} if include_usage else {}),
            **_tool_kwargs(tools, tool_choice),
            **_chat_kwargs(params),
        )

    async def complete(
        self,
        *,
        model: str,
        messages: list[dict[str, Any]],
        params: Mapping[str, Any] | None = None,
        extra_headers: Mapping[str, str] | None = None,
        tools: list[dict[str, Any]] | None = None,
        tool_choice: Any = None,
    ) -> ChatCompletion:
        """Non-streaming completion — the whole answer in one object. ``tools``/``tool_choice``
        carry the OpenAI function-calling protocol; see :meth:`open_stream`."""
        return await self._client.chat.completions.create(  # ty: ignore[no-matching-overload]
            model=model,
            messages=messages,  # type: ignore[arg-type]
            stream=False,
            extra_headers=dict(extra_headers) if extra_headers else None,
            **_tool_kwargs(tools, tool_choice),
            **_chat_kwargs(params),
        )

    async def embed(
        self,
        *,
        model: str,
        inputs: str | list[str],
        params: Mapping[str, Any] | None = None,
        extra_headers: Mapping[str, str] | None = None,
    ) -> Any:
        """Embeddings — one-or-many inputs → vectors. ``model`` is a logical id; transport
        only (no normalization). Errors surface at this ``await``, like ``complete``."""
        return await self._client.embeddings.create(
            model=model,
            input=inputs,
            extra_headers=dict(extra_headers) if extra_headers else None,
            **_clean_params(params),
        )

    async def transcribe(
        self,
        *,
        model: str,
        file: Any,
        params: Mapping[str, Any] | None = None,
        extra_headers: Mapping[str, str] | None = None,
    ) -> Any:
        """Speech-to-text. ``file`` is the OpenAI ``FileTypes`` (a ``(filename, bytes,
        content_type)`` tuple or a file-like) — the SDK owns the multipart encoding. ``model`` is a
        logical id; transport only."""
        return await self._client.audio.transcriptions.create(
            model=model,
            file=file,
            extra_headers=dict(extra_headers) if extra_headers else None,
            **_clean_params(params),
        )

    async def speak(
        self,
        *,
        model: str,
        text: str,
        voice: str = "alloy",
        params: Mapping[str, Any] | None = None,
        extra_headers: Mapping[str, str] | None = None,
    ) -> bytes:
        """Text-to-speech → audio bytes. ``model`` is a logical id; ``response_format`` /
        ``speed`` ride in ``params``. Returns the whole audio body (the binary response content)."""
        response = await self._client.audio.speech.create(
            model=model,
            input=text,
            voice=voice,  # type: ignore[arg-type]
            extra_headers=dict(extra_headers) if extra_headers else None,
            **_clean_params(params),
        )
        return response.content

    # Local diffusion generation is minutes-scale and loads weights per request, far exceeding the
    # client's default timeout (tuned for chat). Image generation gets its own generous per-request
    # timeout so a legitimately slow render isn't cut off as a spurious "request timed out".
    IMAGE_TIMEOUT_SEC = 1200.0

    async def generate_image(
        self,
        *,
        model: str,
        prompt: str,
        params: Mapping[str, Any] | None = None,
        extra_headers: Mapping[str, str] | None = None,
    ) -> bytes:
        """Text-to-image → image bytes. ``model`` is a logical id; ``size``/``n`` ride in ``params``
        as OpenAI-standard fields, while engine-specific knobs (e.g. ``steps``) go through
        ``extra_body`` so the SDK doesn't reject them (it validates named kwargs against the OpenAI
        schema). Requests base64 (the bytes come back inline, never a URL to a server in a different
        trust domain) and returns the first image's decoded bytes — symmetric to ``speak``, so the
        caller stores them as an artifact ref. Uses a long per-request timeout (generation is
        minutes-scale) that overrides the chat-tuned client default without touching other calls."""
        # Drop the fields this method sets itself (prompt, response_format) so a caller-supplied one
        # can't duplicate the explicit kwarg; everything else splits into OpenAI-standard kwargs vs.
        # engine knobs (extra_body).
        clean = {k: v for k, v in _clean_params(params).items() if k not in _IMAGE_FORCED}
        standard = {k: v for k, v in clean.items() if k in _OPENAI_IMAGE_PARAMS}
        extra = {k: v for k, v in clean.items() if k not in _OPENAI_IMAGE_PARAMS}
        response = await self._client.images.generate(
            model=model,
            prompt=prompt,
            response_format="b64_json",
            extra_headers=dict(extra_headers) if extra_headers else None,
            extra_body=extra or None,
            timeout=self.IMAGE_TIMEOUT_SEC,
            **standard,
        )
        data = response.data or []
        b64 = data[0].b64_json if data else None
        if not b64:
            raise RuntimeError("image endpoint returned no image data")
        return base64.b64decode(b64)

    async def models(self) -> list[str]:
        """List logical model ids the inference plane exposes (GET ``/v1/models``).

        Used by the control-plane ``/readyz`` reachability probe — a successful call
        proves the inference plane is reachable over HTTP.
        """
        page = await self._client.models.list()
        return [m.id for m in page.data]

    async def aclose(self) -> None:
        await self._client.close()


# Static type sanity: chunk type is importable for callers that want to annotate.
_: type[ChatCompletionChunk] = ChatCompletionChunk
