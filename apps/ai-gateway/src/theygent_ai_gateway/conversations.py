"""Where each request sits in its conversation — the fact routing decisions depend on.

Prompt caches belong to one model, so moving a conversation to another model mid-stream throws a
warm cache away and pays to rebuild it. Switching is free only where the cache is cold anyway:

* ``conversation_start`` — the first request of a conversation (a subagent's first request too);
* ``after_compaction`` — the first request after Claude Code compacted the conversation;
* ``cache_expired`` — longer since the last request than that conversation's cache lives.

``new_prompt`` (a new user prompt while the cache is still warm) is a candidate that costs a cache
rebuild; ``continuation`` (inside one prompt's tool loop) never moves.

A conversation is one developer's session and agent: the main loop and its compaction share the
main conversation, each subagent has its own. Side requests are not conversations; the tracker
reports whether one starts with the main conversation's first message, i.e. rides its cache.

The cache lifetime comes from the request's own markers — Claude Code asks for one hour on the
main loop and the five-minute default on subagents and compaction, and is billed for exactly that.
Lifetimes are measured between request starts, so a position is an estimate, not a cache probe.
The tracker runs on the event loop only and its memory is bounded.
"""

from __future__ import annotations

import time
from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass
from typing import Literal, cast

from theygent_ai_gateway.identity import Identity
from theygent_ai_gateway.observe import ResponseSummary
from theygent_ai_gateway.signals import RequestSignals

Boundary = Literal[
    "conversation_start",
    "after_compaction",
    "cache_expired",
    "new_prompt",
    "continuation",
    "side_request",
    "untracked",
]
ConversationKind = Literal["main", "subagent", "workflow"]

#: Cache lifetimes by marker TTL.
TTL_SECONDS = {"5m": 300, "1h": 3600}
DEFAULT_MAX_CONVERSATIONS = 10_000
#: A conversation idle this long is forgotten: well past the longest cache lifetime.
DEFAULT_IDLE_SECONDS = 2 * 3600

ConversationKey = tuple[str | None, str, str]


@dataclass(frozen=True)
class Position:
    boundary: Boundary
    key: ConversationKey | None = None
    kind: ConversationKind | None = None
    #: Counts boundaries: requests in one segment could share a model without a cache rebuild.
    segment: int = 0
    request_index: int = 0
    seconds_since_last: float | None = None
    #: Whether the previous request's cache should still be alive.
    cache_warm: bool = False
    #: Lifetime of the previous request's cache, from its markers (0: it asked for none).
    ttl_seconds: int = 0
    #: Tokens the previous request left cached — what a switch here would have to rebuild.
    cached_prefix_tokens: int = 0
    #: For a side request: whether it starts with the main conversation's first message.
    shares_main_prefix: bool | None = None

    @property
    def cold(self) -> bool:
        """A switch here costs no cache: the conversation starts or its cache is gone."""
        return self.boundary in ("conversation_start", "after_compaction", "cache_expired")


@dataclass
class _Conversation:
    kind: ConversationKind
    last_started: float
    prompt_id: str | None
    ttl_seconds: int
    first_message_digest: str | None
    segment: int = 0
    requests: int = 1
    cached_tokens: int = 0
    model: str | None = None


def _ttl(signals: RequestSignals) -> int:
    ttls = signals.body.cache_ttls if signals.body else ()
    # The longest-lived marker: the stable prefix (system, tools, history) is what it covers.
    return max((TTL_SECONDS.get(ttl, 0) for ttl in ttls), default=0)


class ConversationTracker:
    def __init__(
        self,
        *,
        clock: Callable[[], float] = time.monotonic,
        max_conversations: int = DEFAULT_MAX_CONVERSATIONS,
        idle_seconds: float = DEFAULT_IDLE_SECONDS,
    ) -> None:
        self._clock = clock
        self._max = max_conversations
        self._idle = idle_seconds
        self._conversations: OrderedDict[ConversationKey, _Conversation] = OrderedDict()

    def __len__(self) -> int:
        return len(self._conversations)

    def begin(self, signals: RequestSignals, identity: Identity | None) -> Position:
        """Place a request before it is forwarded, and record it as the conversation's latest."""
        now = self._clock()
        self._forget_idle(now)
        hints = signals.hints
        developer = identity.developer_id if identity else None
        if hints.session_id is None or hints.request_class in ("unlabeled", "unrecognized"):
            return Position(boundary="untracked")
        if hints.request_class == "auxiliary":
            return self._side_request(developer, hints.session_id, signals)

        if hints.request_class in ("subagent", "workflow") and hints.agent_id:
            kind = cast(ConversationKind, hints.request_class)
            agent = hints.agent_id
        else:
            kind, agent = cast(ConversationKind, "main"), "main"
        key: ConversationKey = (developer, hints.session_id, agent)
        digest = signals.body.first_message_digest if signals.body else None
        state = self._conversations.get(key)
        if state is None:
            state = _Conversation(
                kind=kind,
                last_started=now,
                prompt_id=hints.prompt_id,
                ttl_seconds=_ttl(signals),
                first_message_digest=digest,
            )
            self._remember(key, state)
            return Position(boundary="conversation_start", key=key, kind=kind)

        gap = now - state.last_started
        warm = state.ttl_seconds > 0 and gap <= state.ttl_seconds
        boundary: Boundary
        if hints.context_compacted is not None:
            boundary, warm = "after_compaction", False
        elif not warm:
            boundary = "cache_expired"
        elif hints.prompt_id is not None and hints.prompt_id != state.prompt_id:
            boundary = "new_prompt"
        else:
            boundary = "continuation"

        position = Position(
            boundary=boundary,
            key=key,
            kind=state.kind,
            segment=state.segment + (boundary != "continuation"),
            request_index=state.requests,
            seconds_since_last=gap,
            cache_warm=warm,
            ttl_seconds=state.ttl_seconds,
            cached_prefix_tokens=state.cached_tokens if warm else 0,
        )
        state.segment = position.segment
        state.requests += 1
        state.last_started = now
        state.prompt_id = hints.prompt_id or state.prompt_id
        state.ttl_seconds = _ttl(signals)
        if boundary == "after_compaction" or state.first_message_digest is None:
            # Compaction restarts the conversation from a summary: a new first message.
            state.first_message_digest = digest
        self._remember(key, state)
        return position

    def complete(self, position: Position, status: int | None, summary: ResponseSummary) -> None:
        """Record what a finished request left cached and which model served it."""
        if position.key is None or status != 200:
            return
        state = self._conversations.get(position.key)
        if state is None:
            return
        usage = summary.usage
        cached = usage.cache_read_tokens + usage.cache_write_5m_tokens + usage.cache_write_1h_tokens
        if cached:
            state.cached_tokens = cached
        if summary.model:
            state.model = summary.model

    def _side_request(
        self, developer: str | None, session: str, signals: RequestSignals
    ) -> Position:
        main = self._conversations.get((developer, session, "main"))
        digest = signals.body.first_message_digest if signals.body else None
        shares = None
        if main is not None and main.first_message_digest is not None and digest is not None:
            shares = digest == main.first_message_digest
        return Position(boundary="side_request", shares_main_prefix=shares)

    def _remember(self, key: ConversationKey, state: _Conversation) -> None:
        self._conversations[key] = state
        self._conversations.move_to_end(key)
        while len(self._conversations) > self._max:
            self._conversations.popitem(last=False)

    def _forget_idle(self, now: float) -> None:
        while self._conversations:
            key, state = next(iter(self._conversations.items()))
            if now - state.last_started <= self._idle:
                break
            del self._conversations[key]
