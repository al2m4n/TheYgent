"""GatewayClient — the transport-only OpenAI-compatible client (M3 §3.3).

Wraps the official async ``openai`` SDK pointed at the inference plane's ``/v1/*``
data plane. The SDK is deliberate: it speaks the exact OpenAI shape the §9.1 seam
promises, so using it from the control-plane *is* the end-to-end proof that the seam
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

from collections.abc import Mapping
from typing import Any

from openai import AsyncOpenAI
from openai.types.chat import ChatCompletion, ChatCompletionChunk

# OpenAI request fields that are routing/identity, not generation params. The caller
# passes generation params (temperature, max_tokens, …) via ``params``; these would
# collide with our explicit kwargs, so we drop them defensively.
_RESERVED = frozenset({"model", "messages", "stream", "extra_headers"})


def _clean_params(params: Mapping[str, Any] | None) -> dict[str, Any]:
    if not params:
        return {}
    return {k: v for k, v in params.items() if k not in _RESERVED}


def _tool_kwargs(tools: list[dict[str, Any]] | None, tool_choice: Any) -> dict[str, Any]:
    """The OpenAI function-calling kwargs, forwarded to ``create`` only when set (M21) — so a
    tools-less call is byte-identical to pre-M21."""
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
    inference plane resolves any real upstream credential locally (§10); the
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
        # runtime (M13 §2) passes ``0`` so the SDK does NOT silently retry a failed inference call —
        # DBOS owns retry on the durable path, and a provider-side retry on top of a DBOS step retry
        # is the double-retry hazard the milestone explicitly forbids.
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

        ``tools``/``tool_choice`` (M21) carry the OpenAI function-calling protocol verbatim —
        a NAMED transport-seam extension (not buried in ``params``) so the function-calling
        contract is explicit and type-checked. The inference plane forwards them to the engine
        (no inference-side change); a model that ignores them simply streams content. Forwarded
        only when set, so a tools-less call is byte-identical to pre-M21.

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
            **_clean_params(params),
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
        (M21) carry the OpenAI function-calling protocol; see :meth:`open_stream`."""
        return await self._client.chat.completions.create(  # ty: ignore[no-matching-overload]
            model=model,
            messages=messages,  # type: ignore[arg-type]
            stream=False,
            extra_headers=dict(extra_headers) if extra_headers else None,
            **_tool_kwargs(tools, tool_choice),
            **_clean_params(params),
        )

    async def embed(
        self,
        *,
        model: str,
        inputs: str | list[str],
        params: Mapping[str, Any] | None = None,
        extra_headers: Mapping[str, str] | None = None,
    ) -> Any:
        """Embeddings (M18) — one-or-many inputs → vectors. ``model`` is a logical id; transport
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
        """Speech-to-text (M18 §1.1). ``file`` is the OpenAI ``FileTypes`` (a ``(filename, bytes,
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
        """Text-to-speech (M18 §1.1) → audio bytes. ``model`` is a logical id; ``response_format`` /
        ``speed`` ride in ``params``. Returns the whole audio body (the binary response content)."""
        response = await self._client.audio.speech.create(
            model=model,
            input=text,
            voice=voice,  # type: ignore[arg-type]
            extra_headers=dict(extra_headers) if extra_headers else None,
            **_clean_params(params),
        )
        return response.content

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
