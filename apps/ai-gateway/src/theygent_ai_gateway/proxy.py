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
import time
from collections.abc import AsyncIterator, Coroutine
from datetime import UTC, datetime
from typing import Any

import anyio
import httpx
from fastapi import Request, Response
from fastapi.responses import JSONResponse, StreamingResponse
from starlette.background import BackgroundTask
from theygent_ir.gateway import Endpoint, Outcome, SeatAllowance, Timing

from theygent_ai_gateway.observe import (
    Observation,
    ObserveHook,
    ResponseObserver,
    ResponseSummary,
    deliver,
    seat_allowance,
)
from theygent_ai_gateway.signals import RequestSignals, read_request

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


class _Exchange:
    """One exchange's observation, assembled as the exchange happens and delivered to the
    observe hook exactly once, however it ends. Every step is guarded: observing must never
    change or interrupt what is relayed."""

    def __init__(self, request: Request, endpoint: Endpoint, signals: RequestSignals) -> None:
        self._hook: ObserveHook | None = getattr(request.app.state, "observe", None)
        self._started_at = datetime.now(UTC)
        self._t0 = time.perf_counter()
        self._method = request.method
        self._endpoint = endpoint
        self._signals = signals
        self._status: int | None = None
        self._headers_ms: int | None = None
        self._first_byte_ms: int | None = None
        self._observer: ResponseObserver | None = None
        self._seat: SeatAllowance | None = None
        self._done = False

    def _ms(self) -> int:
        return int((time.perf_counter() - self._t0) * 1000)

    def headers_arrived(self, upstream: httpx.Response) -> None:
        self._status = upstream.status_code
        self._headers_ms = self._ms()
        try:
            self._observer = ResponseObserver(
                upstream.headers.get("content-type"), upstream.headers.get("content-encoding")
            )
            self._seat = seat_allowance(upstream.headers)
        except Exception as exc:
            log.error("could not start reading the response: %s", type(exc).__name__)

    def chunk(self, data: bytes) -> None:
        if self._first_byte_ms is None:
            self._first_byte_ms = self._ms()
        if self._observer is not None:
            self._observer.feed(data)  # never raises

    def finish(self, outcome: Outcome) -> None:
        if self._done:
            return
        self._done = True
        try:
            summary = self._observer.finish() if self._observer else ResponseSummary()
            observation = Observation(
                started_at=self._started_at,
                method=self._method,
                endpoint=self._endpoint,
                request=self._signals,
                status=self._status,
                outcome=outcome,
                timing=Timing(
                    headers_ms=self._headers_ms,
                    first_byte_ms=self._first_byte_ms,
                    total_ms=self._ms(),
                ),
                response=summary,
                seat_allowance=self._seat,
            )
        except Exception as exc:
            log.error("could not assemble the observation: %s", type(exc).__name__)
            return
        deliver(self._hook, observation)


async def _relay(upstream: httpx.Response, exchange: _Exchange) -> AsyncIterator[bytes]:
    """Yield the upstream body as it arrives, letting the exchange read a copy of each chunk.

    If the client hangs up, the server cancels this generator and the upstream connection
    closes, which ends generation upstream. The HTTP client already closes a stream whose
    iteration is cancelled; the shielded ``aclose`` makes that close explicit and immune to a
    re-delivered cancellation. If the upstream breaks mid-body the error propagates and the
    client's response ends abruptly — never with a clean end that would pass a truncated stream
    off as complete."""
    outcome: Outcome = "complete"
    try:
        async for chunk in upstream.aiter_raw():
            exchange.chunk(chunk)
            yield chunk
    except (asyncio.CancelledError, GeneratorExit):
        outcome = "client_disconnected"
        raise
    except BaseException:
        outcome = "upstream_broke"
        raise
    finally:
        with anyio.CancelScope(shield=True):
            await upstream.aclose()
        exchange.finish(outcome)


async def _close_unrelayed(upstream: httpx.Response, exchange: _Exchange) -> None:
    """Runs after the response: covers a client that left before the body was ever iterated,
    so ``_relay``'s own cleanup never ran. Both steps are idempotent."""
    with anyio.CancelScope(shield=True):
        await upstream.aclose()
    exchange.finish("client_disconnected")


async def forward(request: Request) -> Response:
    """Relay one request to the upstream and its response back."""
    path = request.url.path
    if is_own_path(path):
        return gateway_error(404, "not_forwarded", f"{path} is the gateway's own path", retry=False)

    client: httpx.AsyncClient | None = request.app.state.upstream
    if client is None:
        return gateway_error(503, "not_ready", "the gateway is starting up", retry=True)

    body = await request.body()
    endpoint = classify(path)
    exchange = _Exchange(
        request,
        endpoint,
        read_request(
            {name.lower(): value for name, value in request.headers.items()},
            body,
            has_messages_body=endpoint in ("messages", "count_tokens"),
        ),
    )
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
        exchange.finish("client_disconnected")
        return Response(status_code=CLIENT_CLOSED_REQUEST)
    except httpx.TimeoutException as exc:
        log.warning("upstream timed out before answering: %s", type(exc).__name__)
        exchange.finish("transport_error")
        return gateway_error(
            504, "upstream_timeout", "the upstream did not answer in time", retry=True
        )
    except httpx.HTTPError as exc:
        log.warning("upstream unreachable: %s", type(exc).__name__)
        exchange.finish("transport_error")
        return gateway_error(
            502,
            "upstream_unreachable",
            f"the upstream could not be reached ({type(exc).__name__})",
            retry=True,
        )

    exchange.headers_arrived(upstream)
    response = StreamingResponse(
        _relay(upstream, exchange),
        status_code=upstream.status_code,
        background=BackgroundTask(_close_unrelayed, upstream, exchange),
    )
    response.raw_headers = client_response_headers(list(upstream.headers.raw))
    return response
