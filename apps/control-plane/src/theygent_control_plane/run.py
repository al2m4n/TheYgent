"""The ``Run`` entity + in-memory registry (M3 §3.4).

A ``Run`` is the request identity the whole path threads through. For M3 the registry
is **in-memory** — no Postgres, no migrations (§7). But ``Run`` is modelled as a clean
Pydantic entity that maps 1:1 to a future table, so when persistence arrives (with the
memory section) it is an additive drop-in, not a reshape. The entity is deliberately
free of any framework/storage coupling.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Literal

from pydantic import BaseModel, Field
from ulid import ULID

RunStatus = Literal["created", "streaming", "completed", "failed"]


def _now() -> datetime:
    return datetime.now(UTC)


class Run(BaseModel):
    """A single request, from creation to a terminal status.

    ``model`` is the **logical** model id forwarded to inference (never an engine
    name — §3.2). The status lifecycle is ``created`` -> ``streaming`` ->
    ``completed`` | ``failed``.
    """

    id: str = Field(default_factory=lambda: str(ULID()))
    status: RunStatus = "created"
    model: str
    created_at: datetime = Field(default_factory=_now)
    updated_at: datetime = Field(default_factory=_now)
    error: str | None = None


class RunRegistry:
    """In-memory run store. Single FastAPI event loop, so a plain dict is enough.

    Maps 1:1 to a future ``runs`` table; ``create``/``get``/``set_status`` become the
    persistence boundary later.
    """

    def __init__(self) -> None:
        self._runs: dict[str, Run] = {}

    def create(self, *, model: str) -> Run:
        run = Run(model=model)
        self._runs[run.id] = run
        return run

    def get(self, run_id: str) -> Run | None:
        return self._runs.get(run_id)

    def set_status(self, run_id: str, status: RunStatus, *, error: str | None = None) -> Run:
        run = self._runs[run_id]
        run.status = status
        run.error = error
        run.updated_at = _now()
        return run
