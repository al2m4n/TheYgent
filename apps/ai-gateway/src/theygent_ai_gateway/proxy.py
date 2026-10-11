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

Deliberate deviations: upstream ``accept-encoding`` is forced to ``identity`` so the gateway can
read the stream it relays; the gateway's own credential header (``x-theygent-gateway-key``) is
never forwarded — it authenticates a developer to the gateway, not to the upstream; and in router
mode a routed request's top-level ``model`` value is replaced, nothing else (see ``routing``) — a
target that refuses it before answering gets the original bytes sent to the requested model
instead. A side request routed to a local model is answered by the inference plane (see
``local``); when it cannot answer, the original bytes go upstream as if it had never been tried.

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
from theygent_ir.gateway import (
    CredentialProfile,
    Decision,
    Endpoint,
    GatewayConfig,
    GatewayMode,
    Outcome,
    PolicyConfig,
    ReasonCode,
    SeatAllowance,
    Timing,
)

from theygent_ai_gateway.configsync import ConfigState
from theygent_ai_gateway.conversations import ConversationTracker, Position
from theygent_ai_gateway.costs import PriceBook
from theygent_ai_gateway.credentials import OrgKey
from theygent_ai_gateway.identity import (
    GATEWAY_KEY_HEADER,
    GATEWAY_KEY_PREFIX,
    Identity,
    KeyRing,
    presented_gateway_key,
)
from theygent_ai_gateway.local import LocalAnswer, LocalRoute
from theygent_ai_gateway.observe import (
    Observation,
    ObserveHook,
    ResponseObserver,
    ResponseSummary,
    deliver,
    seat_allowance,
)
from theygent_ai_gateway.policy import Plan, Policy, counterfactual_usd, to_decision
from theygent_ai_gateway.routing import RETRY_ON_REQUESTED, with_model
from theygent_ai_gateway.settings import GatewaySettings
from theygent_ai_gateway.shadow import ShadowJob, Shadows
from theygent_ai_gateway.signals import RequestSignals, credential_kind, read_request

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
#: Headers that carry a credential for the upstream.
_CREDENTIAL_HEADERS = frozenset({"authorization", "x-api-key"})
#: The endpoints an org_key developer may reach with the organization's key: Claude Code's own.
#: Anything else (files, batches, admin APIs) would let a gateway key spend the org's key freely.
ORG_KEY_ENDPOINTS = frozenset({"messages", "count_tokens", "models"})
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


def _is_gateway_token(name: str, value: bytes) -> bool:
    text = value.decode("latin-1").strip()
    if name == "authorization" and text.lower().startswith("bearer "):
        text = text[7:].strip()
    return text.startswith(GATEWAY_KEY_PREFIX)


def upstream_request_headers(
    raw: list[tuple[bytes, bytes]],
    *,
    profile: CredentialProfile = "passthrough",
    org_api_key: str | None = None,
) -> list[tuple[bytes, bytes]]:
    """The client's headers as the upstream should see them: order and duplicates kept,
    connection-level headers dropped, compression turned off.

    A gateway key never goes upstream, in whichever header it came. On the ``org_key`` profile
    every client credential is dropped and the organization's key goes in its place."""
    kept: list[tuple[bytes, bytes]] = []
    for name, value in raw:
        lowered = name.decode("latin-1").lower()
        if lowered in HOP_BY_HOP or lowered in ("accept-encoding", GATEWAY_KEY_HEADER):
            continue
        if lowered in _CREDENTIAL_HEADERS and (
            profile == "org_key" or _is_gateway_token(lowered, value)
        ):
            continue
        kept.append((name, value))
    if profile == "org_key" and org_api_key is not None:
        kept.append((b"x-api-key", org_api_key.encode("latin-1")))
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
        self.identity: Identity | None = None
        self._tracker: ConversationTracker | None = getattr(request.app.state, "tracker", None)
        self._policy: Policy | None = getattr(request.app.state, "policy", None)
        # One snapshot for the whole exchange: a config applied mid-request never mixes in.
        config_state: ConfigState | None = getattr(request.app.state, "config_state", None)
        snapshot = config_state.current if config_state is not None else None
        self._config: GatewayConfig | None = snapshot.config if snapshot else None
        self._mode: GatewayMode = snapshot.mode if snapshot else "observer"
        self._prices: PriceBook | None = snapshot.prices if snapshot else None
        self.plan: Plan | None = None
        self.position: Position | None = None
        #: Router mode: the decision was applied — the target served the request.
        self._applied = False
        #: The applied target was a local model: no API spend.
        self._local = False
        self._shadows: Shadows | None = getattr(request.app.state, "shadows", None)
        #: What a shadow run needs, kept from the forward path (see ``keep_for_shadow``).
        self._shadow_request: tuple[bytes, str, list[tuple[bytes, bytes]]] | None = None
        self._cloud_shadow_allowed = False

    @property
    def credential(self) -> str:
        return self._signals.credential

    @property
    def requested_model(self) -> str | None:
        body = self._signals.body
        return body.model if body is not None else None

    def route_to(self) -> str | None:
        """Router mode: the Anthropic model the request goes to instead, if any. A local target
        is not applied by this path."""
        plan = self.plan
        if self._mode != "router" or plan is None or plan.action != "route":
            return None
        if plan.target is None or plan.target.kind != "anthropic":
            return None
        return plan.target.model

    def local_route(self) -> str | None:
        """Router mode: the local model a side request goes to instead, if any."""
        plan = self.plan
        if self._mode != "router" or self._endpoint != "messages" or plan is None:
            return None
        if plan.action != "route" or plan.target is None or plan.target.kind != "local":
            return None
        return plan.target.model

    def applied(self) -> None:
        self._applied = True

    def keep_for_shadow(
        self, body: bytes, path: str, headers: list[tuple[bytes, bytes]], *, cloud_allowed: bool
    ) -> None:
        """Keep what a shadow of this request would send, should its decision be a route that is
        not applied. ``cloud_allowed``: the credential is one the gateway's operator holds."""
        self._shadow_request = (body, path, headers)
        self._cloud_shadow_allowed = cloud_allowed

    def _shadow_job(self, outcome: Outcome) -> ShadowJob | None:
        """A shadow of this exchange: only of a complete, successful Messages exchange whose
        decision routed it somewhere it did not go."""
        plan, kept = self.plan, self._shadow_request
        if kept is None or plan is None or self._applied or outcome != "complete":
            return None
        if self._endpoint != "messages" or self._status is None or not 200 <= self._status < 300:
            return None
        if plan.action != "route" or plan.target is None:
            return None
        body, path, headers = kept
        return ShadowJob(plan.target, body, path, headers)

    def served_locally(self, answer: LocalAnswer) -> None:
        """The local model answered: the exchange is that answer, read like an upstream one."""
        self._applied = self._local = True
        self._status = 200
        self._headers_ms = self._ms()
        try:
            self._observer = ResponseObserver(answer.content_type, None)
        except Exception as exc:
            log.error("could not start reading the response: %s", type(exc).__name__)
        self.chunk(answer.body)

    def local_failed(self) -> None:
        """The local model could not answer; the request goes to the requested model. Unlike a
        refused Anthropic route nothing is pinned: a side request holds no conversation cache."""
        if self.plan is not None:
            self.plan = Plan.stay("target_refused", *self.plan.reasons)

    def target_refused(self) -> None:
        """The target refused the routed request before answering; it goes to the requested model
        instead, and the conversation stays there until its next cold boundary."""
        if self.plan is not None:
            self.plan = Plan.stay("target_refused", *self.plan.reasons)
        if self._policy is not None:
            try:
                self._policy.refused(self.position, self.requested_model)
            except Exception as exc:
                log.error("could not pin the conversation: %s", type(exc).__name__)

    def place(self) -> None:
        """Locate a request in its conversation and decide what routing would do with it, before
        it is forwarded. Observer mode records the decision and never applies it."""
        if self._tracker is not None and self._endpoint == "messages":
            try:
                self.position = self._tracker.begin(self._signals, self.identity)
            except Exception as exc:
                log.error("could not place the request: %s", type(exc).__name__)
        if self._mode == "passthrough":
            # Covers a gateway that has never been configured, which has no config to decide on.
            self.plan = Plan.stay("mode_passthrough")
        elif self._policy is not None:
            try:
                self.plan = self._policy.decide(
                    self._signals,
                    self.position,
                    self._config,
                    endpoint_is_messages=self._endpoint == "messages",
                    prices=self._prices,
                )
            except Exception as exc:
                log.error("the policy failed: %s", type(exc).__name__)

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

    def _cost(self, summary: ResponseSummary) -> tuple[float | None, str | None]:
        """What the exchange cost, priced on the model that served it (else the one asked for).
        A local model's answer costs no API spend; local compute is not priced here."""
        if self._local:
            return 0.0, self._prices.version if self._prices is not None else None
        usage = summary.usage
        used = usage.input_tokens + usage.output_tokens + usage.cache_read_tokens
        used += usage.cache_write_5m_tokens + usage.cache_write_1h_tokens
        if self._prices is None or not used:
            return None, None
        body = self._signals.body
        model = summary.model or (body.model if body else None)
        cost = self._prices.cost(model, usage)
        return (cost, self._prices.version) if cost is not None else (None, None)

    def _decision(self, summary: ResponseSummary, outcome: Outcome) -> Decision:
        """The plan as a wire decision, with what it would have cost. Every exchange gets one: a
        request refused before the policy ran is ``request_refused``, and a policy failure is
        recorded as ``policy_error`` rather than losing the exchange."""
        if self.plan is None:
            reason: ReasonCode = "request_refused" if outcome == "rejected" else "policy_error"
            return to_decision(Plan.stay(reason), None)
        try:
            if self._applied:
                return to_decision(self.plan, None, applied=True)
            counterfactual = (
                counterfactual_usd(self.plan, summary.usage, self.position, self._prices)
                if self._prices is not None
                else None
            )
            return to_decision(self.plan, counterfactual)
        except Exception as exc:
            log.error("could not record the decision: %s", type(exc).__name__)
            return to_decision(Plan.stay("policy_error"), None)

    def reject(self, response: JSONResponse) -> JSONResponse:
        """The gateway refused the request before forwarding it."""
        self._status = response.status_code
        self.finish("rejected")
        return response

    def finish(self, outcome: Outcome) -> None:
        if self._done:
            return
        self._done = True
        try:
            summary = self._observer.finish() if self._observer else ResponseSummary()
            if self._tracker is not None and self.position is not None and outcome == "complete":
                try:
                    self._tracker.complete(self.position, self._status, summary)
                except Exception as exc:
                    log.error("could not update the conversation: %s", type(exc).__name__)
            cost_usd, pricing_version = self._cost(summary)
            decision = self._decision(summary, outcome)
            baseline_usd = (
                self._prices.cost(self.requested_model, summary.usage)
                if self._applied and self._prices is not None and cost_usd is not None
                else None
            )
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
                identity=self.identity,
                position=self.position,
                cost_usd=cost_usd,
                baseline_usd=baseline_usd,
                pricing_version=pricing_version,
                decision=decision,
                config_version=self._config.version if self._config else None,
                mode=self._mode,
            )
        except Exception as exc:
            log.error("could not assemble the observation: %s", type(exc).__name__)
            return
        if self._shadow(observation, outcome):
            return  # delivered when the shadow ends
        deliver(self._hook, observation)

    def _shadow(self, observation: Observation, outcome: Outcome) -> bool:
        """Start a shadow of this exchange, which then delivers its observation; ``False`` when
        none runs."""
        shadows = self._shadows
        try:
            job = self._shadow_job(outcome)
            if shadows is None or job is None:
                return False
            return shadows.start(
                job,
                observation,
                self._hook,
                policy=self._config.policy if self._config is not None else PolicyConfig(),
                prices=self._prices,
                cloud_allowed=self._cloud_shadow_allowed,
            )
        except Exception as exc:
            log.error("could not start a shadow run: %s", type(exc).__name__)
            return False
        finally:
            self._shadow_request = None  # the body and credentials go with the exchange


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


def _authenticate(
    exchange: _Exchange,
    settings: GatewaySettings,
    ring: KeyRing | None,
    headers: dict[str, str],
    endpoint: Endpoint,
    org_api_key: str | None,
) -> JSONResponse | None:
    """Team mode: identify the developer and check their profile can serve this request.
    Returns the refusal, or ``None`` to forward. Personal mode has no identities to check."""
    if settings.auth_mode != "team":
        return None
    if ring is None or not ring.loaded:
        return gateway_error(
            503, "gateway_keys_unavailable", "developer keys are not loaded yet", retry=True
        )
    token = presented_gateway_key(headers)
    if token is None:
        return gateway_error(
            401,
            "gateway_key_missing",
            "this gateway needs a TheYgent gateway key (x-theygent-gateway-key header, or a "
            "tyg_ token in ANTHROPIC_AUTH_TOKEN)",
            retry=False,
        )
    identity = ring.lookup(token)
    if identity is None:
        return gateway_error(
            401, "gateway_key_invalid", "unknown or revoked gateway key", retry=False
        )
    exchange.identity = identity
    if identity.credential_profile == "org_key":
        if org_api_key is None:
            return gateway_error(
                503,
                "org_key_not_configured",
                "this developer is billed to the organization, but the gateway has no org key",
                retry=False,
            )
        if endpoint not in ORG_KEY_ENDPOINTS:
            return gateway_error(
                403,
                "endpoint_not_allowed",
                "the organization's key serves Claude Code's endpoints only",
                retry=False,
            )
        return None
    upstream_headers = {
        k: v
        for k, v in headers.items()
        if k in _CREDENTIAL_HEADERS and not _is_gateway_token(k, v.encode("latin-1"))
    }
    if credential_kind(upstream_headers) == "none":
        return gateway_error(
            401,
            "upstream_credential_missing",
            "this developer brings their own Anthropic credential (claude.ai login or API key), "
            "and the request carries none",
            retry=False,
        )
    return None


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
    headers = {name.lower(): value for name, value in request.headers.items()}
    settings: GatewaySettings = request.app.state.settings
    if settings.auth_mode == "team" and endpoint == "hello":
        # Claude Code's connection warm-up probe carries no credentials; answer it here rather
        # than refuse it or forward it unauthenticated.
        return Response(status_code=200)
    exchange = _Exchange(
        request,
        endpoint,
        read_request(headers, body, has_messages_body=endpoint in ("messages", "count_tokens")),
    )
    org_key: OrgKey | None = getattr(request.app.state, "org_key", None)
    org_api_key = org_key.value if org_key is not None else None
    refusal = _authenticate(
        exchange, settings, request.app.state.keyring, headers, endpoint, org_api_key
    )
    if refusal is not None:
        return exchange.reject(refusal)
    exchange.place()
    profile: CredentialProfile = (
        exchange.identity.credential_profile if exchange.identity else "passthrough"
    )
    raw_path: bytes = request.scope.get("raw_path") or path.encode()
    query: bytes = request.scope.get("query_string", b"")
    target = raw_path.decode("latin-1") + (f"?{query.decode('latin-1')}" if query else "")
    outbound_headers = upstream_request_headers(
        list(request.headers.raw), profile=profile, org_api_key=org_api_key
    )
    if endpoint == "messages" and exchange.plan is not None and exchange.plan.action == "route":
        # A cloud shadow spends only a key the gateway's operator holds: the organization's, or in
        # personal mode the user's own API key. Never a seat's login, never a team developer's key.
        own_key = settings.auth_mode == "personal" and exchange.credential == "api_key"
        exchange.keep_for_shadow(
            body,
            target.lstrip("/"),
            outbound_headers,
            cloud_allowed=own_key or profile == "org_key",
        )

    def outbound(content: bytes) -> httpx.Request:
        # Relative to the upstream base URL, so a base URL with a path prefix keeps it.
        return client.build_request(
            request.method, target.lstrip("/"), headers=outbound_headers, content=content
        )

    local_model = exchange.local_route()
    if local_model is not None:
        local: LocalRoute | None = getattr(request.app.state, "local", None)
        answered: LocalAnswer | None = None
        if local is not None:
            try:
                answered = await _unless_client_disconnects(
                    request, local.answer(local_model, body)
                )
            except ClientDisconnected:
                exchange.finish("client_disconnected")
                return Response(status_code=CLIENT_CLOSED_REQUEST)
        if answered is not None:
            exchange.served_locally(answered)
            exchange.finish("complete")
            return Response(answered.body, status_code=200, media_type=answered.content_type)
        exchange.local_failed()

    routed_to = exchange.route_to()
    routed = with_model(body, routed_to) if routed_to is not None else None
    if routed_to is not None and routed is None:
        log.warning("not routed: the request's model could not be replaced unambiguously")
    answer = await _send(request, client, outbound(routed or body), exchange)
    if isinstance(answer, Response):
        return answer
    upstream = answer
    if routed is not None:
        if upstream.status_code in RETRY_ON_REQUESTED:
            # The target said no before answering: Claude Code gets the requested model's answer
            # instead, never the refusal.
            with anyio.CancelScope(shield=True):
                await upstream.aclose()
            exchange.target_refused()
            answer = await _send(request, client, outbound(body), exchange)
            if isinstance(answer, Response):
                return answer
            upstream = answer
        else:
            exchange.applied()

    exchange.headers_arrived(upstream)
    response = StreamingResponse(
        _relay(upstream, exchange),
        status_code=upstream.status_code,
        background=BackgroundTask(_close_unrelayed, upstream, exchange),
    )
    response.raw_headers = client_response_headers(list(upstream.headers.raw))
    return response


async def _send(
    request: Request, client: httpx.AsyncClient, outbound: httpx.Request, exchange: _Exchange
) -> httpx.Response | Response:
    """The upstream's answer to ``outbound``, or the response the client gets when there is none
    (the client left, the upstream timed out or could not be reached)."""
    try:
        return await _unless_client_disconnects(request, client.send(outbound, stream=True))
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
