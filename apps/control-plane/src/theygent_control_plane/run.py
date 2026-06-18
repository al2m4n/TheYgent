"""The ``Run`` domain entity (M3 §3.4) — the API/logic shape, free of storage coupling.

M3 modelled ``Run`` as a clean Pydantic entity that maps 1:1 to a future table; M4 makes
that table real. ``Run`` stays the **domain** shape and is *not* the ORM row (§1.3): it is
mapped to/from ``RunRow`` in ``store.py``. The M3 wire contract is unchanged — ``thread_id``
is the one additive field (optional; ``None`` = a one-shot run, exactly M3's behavior).

The in-memory ``RunRegistry`` is gone: persistence now lives in the Postgres-backed
``RunStore`` (``store.py``), so runs survive a restart and are shared state across
horizontally-scaled control-plane instances (§5/§8).
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Literal

from pydantic import BaseModel, Field
from ulid import ULID

RunStatus = Literal["created", "streaming", "completed", "failed"]


def now() -> datetime:
    return datetime.now(UTC)


def new_ulid() -> str:
    return str(ULID())


class Run(BaseModel):
    """A single request, from creation to a terminal status.

    ``model`` is the **logical** model id forwarded to inference (never an engine name —
    §3.2). ``thread_id`` is ``None`` for a one-shot run. The status lifecycle is
    ``created`` -> ``streaming`` -> ``completed`` | ``failed``.

    M5 adds three nullable graph fields — a *deliberate* contract extension (m5.md §3.4 /
    §8 step 4), the second after M4's ``thread_id``. They are ``None`` for a non-graph
    ``/runs`` run and populated for a ``/graphs/runs`` run: ``graph_id`` + ``graph_version``
    are the IR's registry coordinate (§8.2), ``content_hash`` its content-addressed identity.
    Recorded now (not gated on yet — §3.3) so the field is correct when the registry consumes it.
    """

    id: str = Field(default_factory=new_ulid)
    thread_id: str | None = None
    status: RunStatus = "created"
    model: str
    graph_id: str | None = None
    graph_version: str | None = None
    content_hash: str | None = None
    created_at: datetime = Field(default_factory=now)
    updated_at: datetime = Field(default_factory=now)
    error: str | None = None
