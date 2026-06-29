"""``ToolRegistry`` + the two M6 built-in tools (m6.md §3.1).

The registry is the M6 tool fork made concrete: a name → async-callable map, populated **in
code**. The walker resolves a ``tool`` node's ``config.tool`` to a callable here and invokes it
with templated ``args``. The control-plane checks membership *up front* (before a ``Run`` is
created → 400 ``tool_not_found``), mirroring how the engine-name binding is rejected before a Run
in M5 — so an unknown tool never reaches the walker.

M6 ships exactly two tools, just enough to prove the surface and write a real demo:
  * ``echo``       — return the input unchanged. Network-free; proves dispatch + arg templating.
  * ``http_fetch`` — an outbound HTTP GET. Real I/O and real failure modes. A non-200 is a normal
                     return value (bound to ``ok``); only a transport failure (timeout, bad host)
                     raises → the walker binds it to the node's ``err`` handle.

`file_read`/`code_exec`/etc. are deferred — each is its own design surface (sandboxing, path/exec
safety). They are additive against this registry whenever genuinely needed (§7).
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

import httpx

#: A tool is an async callable invoked with keyword args (the templated ``config.args``) that
#: returns a JSON-serializable value. Keyword-only on purpose: ``args`` is a name→value map.
ToolFn = Callable[..., Awaitable[Any]]


class ToolNotFound(KeyError):
    """A ``tool`` node named a tool not in the registry. The control-plane maps this to a 400
    ``tool_not_found`` at up-front validation — no ``Run`` is created (m6.md §5)."""


class ToolRegistry:
    """A name → async-callable map. Built-in for M6 (no runtime registration API — §3.1).

    M21: a builtin may carry an OpenAI function ``(description, parameters)`` schema so it is
    **model-callable** in an llm tool loop. Self-description is the invariant (m21.md §6 Q2): a
    builtin without a schema can still be wired as a ``tool`` node, but the up-front check rejects
    it from an llm's ``tools`` (a model needs the description to decide the call)."""

    def __init__(self) -> None:
        self._tools: dict[str, ToolFn] = {}
        self._schemas: dict[str, tuple[str, dict[str, Any]]] = {}

    def register(
        self,
        name: str,
        *,
        description: str | None = None,
        parameters: dict[str, Any] | None = None,
    ) -> Callable[[ToolFn], ToolFn]:
        """Decorator: register ``fn`` under ``name``. Reused by MCP in M7 (different transport, same
        contract). ``description`` + ``parameters`` (M21) make the tool model-callable — supply BOTH
        to self-describe (the model reads the description to decide how to call it)."""

        def deco(fn: ToolFn) -> ToolFn:
            if name in self._tools:
                raise ValueError(f"tool {name!r} already registered")
            self._tools[name] = fn
            if description is not None and parameters is not None:
                self._schemas[name] = (description, parameters)
            return fn

        return deco

    def get(self, name: str) -> ToolFn:
        try:
            return self._tools[name]
        except KeyError as exc:
            raise ToolNotFound(name) from exc

    def schema(self, name: str) -> tuple[str, dict[str, Any]] | None:
        """The ``(description, parameters)`` OpenAI function schema for a model-callable builtin, or
        ``None`` if the builtin doesn't self-describe (then it isn't allowed in an llm's tools)."""
        return self._schemas.get(name)

    def __contains__(self, name: object) -> bool:
        return name in self._tools

    def names(self) -> frozenset[str]:
        return frozenset(self._tools)


#: The process-wide registry the walker dispatches against. The two built-ins register on import.
DEFAULT_REGISTRY = ToolRegistry()
register = DEFAULT_REGISTRY.register


@register(
    "echo",
    description="Return the provided value unchanged — a no-op probe for testing tool calls.",
    parameters={
        "type": "object",
        "properties": {"value": {"description": "any value to echo back verbatim"}},
    },
)
async def echo(*, value: Any = None) -> Any:
    """Return ``value`` unchanged. The trivial tool the tests use to prove dispatch/templating
    without touching the network."""

    return value


@register(
    "http_fetch",
    description=(
        "HTTP GET a URL and return its response as an object with keys `status` (int), `body` "
        "(the response text), and `headers` (an object). Use this to fetch the contents of a web "
        "page or a REST endpoint. A non-200 status is returned normally (not an error)."
    ),
    parameters={
        "type": "object",
        "properties": {
            "url": {"type": "string", "description": "the absolute http(s) URL to GET"},
            "timeout_s": {
                "type": "number",
                "description": "per-request timeout in seconds (default 10)",
            },
        },
        "required": ["url"],
    },
)
async def http_fetch(*, url: str, timeout_s: float = 10.0) -> dict[str, Any]:
    """HTTP GET ``url`` and return ``{status, body, headers}``. A non-200 is a *return value*,
    not an error (no ``raise_for_status``) — only a transport failure (timeout, DNS, refused)
    raises, and the walker binds that to the node's ``err`` handle (m6.md §3.1/§4). ``timeout_s``
    is the per-request timeout forwarded to httpx (named ``_s`` to keep it a leaf-level knob, not
    a caller-cancellation handle — ASYNC109)."""

    async with httpx.AsyncClient(follow_redirects=True) as client:
        resp = await client.get(url, timeout=timeout_s)
    return {
        "status": resp.status_code,
        "body": resp.text,
        "headers": dict(resp.headers),
    }
