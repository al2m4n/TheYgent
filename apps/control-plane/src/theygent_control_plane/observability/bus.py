"""The in-process span bus — the live side-channel for the growing waterfall (M17 §5/§6).

The durable ``span`` table is the **durable record** the static ``GET /runs/{id}/trace`` reads; this
bus is the **ephemeral live view** ``GET /runs/{id}/trace/stream`` subscribes to so the waterfall
*grows* as a run executes — exactly the relationship :class:`~...durable.bus.DeltaBus` has to the
persisted ``Run`` output (D7): durability lives in the store, this is the live observation, no
persistence and no replay. The wrapper publishes an ``open`` event when a span starts (an in-flight
amber bar appears) and a ``close`` event when it ends (the bar settles green/red, gaining its
duration). A reconnecting cockpit renders completed spans from ``/trace`` and the rest from here.

Process-local, like the DeltaBus (the in-process / desktop-sidecar topology). The durable ``fire()``
path is non-streaming, so for unattended runs there is simply no subscriber and ``publish`` is a
cheap no-op; the bus exists so the cockpit's *interactive* graph runs stream their waterfall live.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class SpanEvent:
    """One live span lifecycle event: ``open`` (a bar appears, in-flight) or ``close`` (it settles).
    ``payload`` is the lightweight span shape the SSE relay forwards — timing + status + ids, never
    a payload (those are lazy-loaded on click, §1.3)."""

    kind: str  # "open" | "close"
    payload: dict[str, Any]


class SpanBus:
    """A per-run fan-out of :class:`SpanEvent`. Subscribers register a queue per ``run_id``;
    ``publish`` drops an event into every live subscriber's queue. No persistence, no replay — the
    durable ``span`` table is that (this is the live view)."""

    def __init__(self) -> None:
        self._subscribers: dict[str, list[asyncio.Queue[SpanEvent | None]]] = {}

    def subscribe(self, run_id: str) -> asyncio.Queue[SpanEvent | None]:
        queue: asyncio.Queue[SpanEvent | None] = asyncio.Queue()
        self._subscribers.setdefault(run_id, []).append(queue)
        return queue

    def unsubscribe(self, run_id: str, queue: asyncio.Queue[SpanEvent | None]) -> None:
        subs = self._subscribers.get(run_id)
        if subs and queue in subs:
            subs.remove(queue)
            if not subs:
                self._subscribers.pop(run_id, None)

    def publish(self, run_id: str, kind: str, payload: dict[str, Any]) -> None:
        """Side-effect publish from inside the wrapper. Best-effort: no subscriber → a no-op (the
        non-streaming durable path). Never raises into the wrapper."""
        subs = self._subscribers.get(run_id)
        if not subs:
            return
        event = SpanEvent(kind=kind, payload=payload)
        for queue in subs:
            queue.put_nowait(event)

    def close(self, run_id: str) -> None:
        """Signal end-of-stream (a sentinel ``None``) to every subscriber of ``run_id``."""
        for queue in self._subscribers.get(run_id, []):
            queue.put_nowait(None)
