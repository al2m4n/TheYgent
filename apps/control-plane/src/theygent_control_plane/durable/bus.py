"""The in-process delta bus — how streaming coexists with durability.

DBOS journals **step results**, not tokens. So live token streaming is a **non-durable
side-channel** over the durable spine: the ``llm`` step streams tokens to THIS bus *as a side
effect* during execution, while its return value (the final assembled output) is the only thing
journaled. The load-bearing consequence: **on crash + resume, the completed ``llm`` step is
replayed from the journal, not re-executed** — so its body (and this ``publish``) does not run
again, tokens are not regenerated, and nothing is re-streamed. A reconnecting cockpit renders from
the **persisted ``Run`` output**, never from an expected token re-stream.

The bus is process-local (in-process topology — the desktop sidecar where DBOS runs embedded in the
API). The durable ``fire()`` path is non-streaming, so for triggers there is simply
no subscriber and ``publish`` is a cheap no-op; the bus exists so a future durable *streaming* entry
(or an in-process worker) can observe live deltas without weakening the durability guarantee.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass


@dataclass(frozen=True)
class BusDelta:
    """One streamed piece on the bus: the producing node, the text, and whether it is the model's
    answer (``content``) or its thinking (``reasoning``) — the same split the SSE relay makes."""

    node_id: str
    content: str
    kind: str  # content | reasoning


class DeltaBus:
    """A per-run fan-out of :class:`BusDelta`. Subscribers register a queue per ``run_id``;
    ``publish`` drops a delta into every live subscriber's queue. No persistence, no replay — that
    is the whole point (durability lives in the journal; this is the ephemeral live view)."""

    def __init__(self) -> None:
        self._subscribers: dict[str, list[asyncio.Queue[BusDelta | None]]] = {}

    def subscribe(self, run_id: str) -> asyncio.Queue[BusDelta | None]:
        queue: asyncio.Queue[BusDelta | None] = asyncio.Queue()
        self._subscribers.setdefault(run_id, []).append(queue)
        return queue

    def unsubscribe(self, run_id: str, queue: asyncio.Queue[BusDelta | None]) -> None:
        subs = self._subscribers.get(run_id)
        if subs and queue in subs:
            subs.remove(queue)
            if not subs:
                self._subscribers.pop(run_id, None)

    def publish(self, run_id: str, node_id: str, content: str, kind: str) -> None:
        """Side-effect publish from inside the ``llm`` step. Best-effort: if no one is listening
        (the non-streaming trigger path), it is a no-op. Never raises into the step body."""
        subs = self._subscribers.get(run_id)
        if not subs:
            return
        delta = BusDelta(node_id=node_id, content=content, kind=kind)
        for queue in subs:
            queue.put_nowait(delta)

    def close(self, run_id: str) -> None:
        """Signal end-of-stream (a sentinel ``None``) to every subscriber of ``run_id``."""
        for queue in self._subscribers.get(run_id, []):
            queue.put_nowait(None)
