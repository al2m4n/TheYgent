"""Exact pass-through to the upstream Anthropic-format API.

Claude Code depends on what it sends arriving unchanged. Prompt caching and preserved thinking
key on the exact ``system`` / ``tools`` / ``messages`` content; beta headers and the body fields
they enable must travel together; the attribution block is recognised by its position. So:

* the request body is forwarded byte-for-byte, and every request header except connection-level
  ones is forwarded as an open list (never an allowlist of today's values);
* the response — status, headers, and body — comes back as the upstream sent it, error bodies
  included, because Claude Code's retry logic matches on the upstream's wording;
* the body is relayed as it arrives, never buffered, so streams and their keep-alive pings reach
  the client in real time;
* a client that hangs up cancels the upstream call, whether before the upstream answered or
  mid-stream, so an abandoned request stops consuming tokens at once.

Two deliberate deviations: upstream ``accept-encoding`` is forced to ``identity`` so the gateway
can read the stream it relays, and the gateway's own credential header
(``x-theygent-gateway-key``) is never forwarded — it authenticates a developer to the gateway, not
to the upstream.

Every path other than the gateway's own (``/healthz``, ``/readyz``, ``/admin/…``) is forwarded:
Claude Code's endpoints grow over releases, and an allowlist would break the next one.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator, Coroutine
from typing import Any

import anyio
import httpx
from fastapi import Request, Response
from fastapi.responses import JSONResponse, StreamingResponse
from theygent_ir.gateway import Endpoint

log = logging.getLogger(__name__)

#: Connection-level headers that describe one hop, never the message; ``host`` and
#: ``content-length`` are recomputed for the next hop.
HOP_BY_HOP = frozenset(
    {
        "connection",
        "keep-alive",
        "proxy-authenticate",
        "proxy-authorization",
        "proxy-connection",
        "te",
        "trailer",
        "trailers",
        "transfer-encoding",
        "upgrade",
        "host",
        "content-length",
    }
)
#: Authenticates a developer to the gateway (team mode). Never forwarded upstream.
GATEWAY_KEY_HEADER = "x-theygent-gateway-key"
#: The gateway's own surface; requests here are answered by the gateway or refused, never forwarded.
OWN_PATH_PREFIXES = ("/healthz", "/readyz", "/admin")
#: Header naming why the gateway itself answered with an error (as opposed to relaying one).
GATEWAY_ERROR_HEADER = "x-theygent-gateway-error"
#: Nobody receives this: the client is gone. It only keeps the access log honest.
CLIENT_CLOSED_REQUEST = 499


def classify(path: str) -> Endpoint:
    """Which Anthropic-format endpoint a path is, in the wire vocabulary."""
    if path == "/v1/messages":
        return "messages"
    if path == "/v1/messages/count_tokens":
        return "count_tokens"
    if path == "/v1/models" or path.startswith("/v1/models/"):
        return "models"
    if path == "/api/hello":
        return "hello"
    return "other"


def is_own_path(path: str) -> bool:
    return any(path == prefix or path.startswith(prefix + "/") for prefix in OWN_PATH_PREFIXES)


def upstream_request_headers(raw: list[tuple[bytes, bytes]]) -> list[tuple[bytes, bytes]]:
    """The client's headers as the upstream should see them: order and duplicates kept,
    connection-level headers and the gateway's own key dropped, compression turned off."""
    kept = [
        (name, value)
        for name, value in raw
        if (lowered := name.decode("latin-1").lower()) not in HOP_BY_HOP
        and lowered not in ("accept-encoding", GATEWAY_KEY_HEADER)
    ]
    kept.append((b"accept-encoding", b"identity"))
    return kept


def client_response_headers(raw: list[tuple[bytes, bytes]]) -> list[tuple[bytes, bytes]]:
    """The upstream's response headers as the client should see them (connection-level
    headers dropped; the body is relayed with this hop's own framing)."""
    return [
        (name, value) for name, value in raw if name.decode("latin-1").lower() not in HOP_BY_HOP
    ]


def gateway_error(status: int, code: str, message: str, *, retry: bool) -> JSONResponse:
    """An error the gateway itself produced, in Anthropic's error shape so Claude Code handles it
    like any upstream error. ``code`` is a stable name in ``x-theygent-gateway-error``."""
    return JSONResponse(
        {
            "type": "error",
            "error": {"type": "api_error", "message": f"TheYgent AI gateway: {message}"},
        },
        status_code=status,
        headers={GATEWAY_ERROR_HEADER: code, "x-should-retry": "true" if retry else "false"},
    )


class ClientDisconnected(Exception):
    """The client hung up before the upstream answered."""


async def _client_disconnected(request: Request) -> None:
    """Return once the client has gone away. The body has been read in full before this runs,
    so the next ASGI message is ``http.disconnect``; awaiting it notices a hang-up at once."""
    while (await request.receive())["type"] != "http.disconnect":
        pass


async def _unless_client_disconnects[T](request: Request, call: Coroutine[Any, Any, T]) -> T:
    """Await ``call``, cancelling it if the client hangs up first.

    The server never cancels a handler whose client went away before the response started, so
    without this a request the upstream answers slowly (a long non-streaming generation, a slow
    first byte) would keep running — and billing — after its client was gone. Cancelling the
    call closes the upstream connection, the upstream's only signal that the request was
    abandoned. ``ClientDisconnected`` is raised only once the cancelled call has unwound."""
    work = asyncio.ensure_future(call)
    gone = asyncio.ensure_future(_client_disconnected(request))
    try:
        await asyncio.wait((work, gone), return_when=asyncio.FIRST_COMPLETED)
    except BaseException:
        # The handler itself was cancelled (server shutdown): the upstream call goes with it.
        work.cancel()
        gone.cancel()
        await asyncio.gather(work, gone, return_exceptions=True)
        raise
    if work.done():
        gone.cancel()
        await asyncio.gather(gone, return_exceptions=True)
        return work.result()
    work.cancel()
    await asyncio.gather(work, return_exceptions=True)
    gone.result()  # a failed listener raises here instead of passing for a disconnect
    raise ClientDisconnected


async def _relay(upstream: httpx.Response) -> AsyncIterator[bytes]:
    """Yield the upstream body as it arrives. If the client hangs up, the server cancels this
    generator and the upstream connection closes, which ends generation upstream. The HTTP
    client already closes a stream whose iteration is cancelled; the shielded ``aclose`` makes
    that close explicit and immune to a re-delivered cancellation. If the upstream breaks
    mid-body the error propagates and the client's response ends abruptly — never with a clean
    end that would pass a truncated stream off as complete."""
    try:
        async for chunk in upstream.aiter_raw():
            yield chunk
    finally:
        with anyio.CancelScope(shield=True):
            await upstream.aclose()


async def forward(request: Request) -> Response:
    """Relay one request to the upstream and its response back."""
    path = request.url.path
    if is_own_path(path):
        return gateway_error(404, "not_forwarded", f"{path} is the gateway's own path", retry=False)

    client: httpx.AsyncClient | None = request.app.state.upstream
    if client is None:
        return gateway_error(503, "not_ready", "the gateway is starting up", retry=True)

    body = await request.body()
    raw_path: bytes = request.scope.get("raw_path") or path.encode()
    query: bytes = request.scope.get("query_string", b"")
    target = raw_path.decode("latin-1") + (f"?{query.decode('latin-1')}" if query else "")
    outbound = client.build_request(
        request.method,
        # Relative to the upstream base URL, so a base URL with a path prefix keeps it.
        target.lstrip("/"),
        headers=upstream_request_headers(list(request.headers.raw)),
        content=body,
    )
    try:
        upstream = await _unless_client_disconnects(request, client.send(outbound, stream=True))
    except ClientDisconnected:
        return Response(status_code=CLIENT_CLOSED_REQUEST)
    except httpx.TimeoutException as exc:
        log.warning("upstream timed out before answering: %s", type(exc).__name__)
        return gateway_error(
            504, "upstream_timeout", "the upstream did not answer in time", retry=True
        )
    except httpx.HTTPError as exc:
        log.warning("upstream unreachable: %s", type(exc).__name__)
        return gateway_error(
            502,
            "upstream_unreachable",
            f"the upstream could not be reached ({type(exc).__name__})",
            retry=True,
        )

    response = StreamingResponse(_relay(upstream), status_code=upstream.status_code)
    response.raw_headers = client_response_headers(list(upstream.headers.raw))
    return response
