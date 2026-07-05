"""Eviction is a policy OBJECT the manager calls — never inline manager logic.

``CountAndPriorityPolicy`` ships as the default: a count ceiling as the deterministic
primary mechanism, priority-then-LRU ordering, and an optional off-by-default
RAM watermark as a secondary trigger. The ``select_victims`` signature is the
seam a real ``ResourceBudgetPolicy`` (RAM/VRAM accounting) drops into later
without touching the manager.

The manager only ever offers *evictable* engines (inflight == 0, not draining)
to the policy, so an engine serving an in-flight request can never be selected.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True)
class ResidentView:
    """Read-only view of a resident engine handed to the policy."""

    logical_id: str
    priority: int
    last_used: float


@dataclass(frozen=True)
class PendingLoad:
    logical_id: str
    priority: int


@dataclass(frozen=True)
class ResourceBudget:
    max_resident: int
    #: secondary, off-by-default trigger fed by a ResourceProbe.
    over_watermark: bool = False
    #: total resident engines INCLUDING non-evictable ones (in-flight / draining). The policy is
    #: only handed the *evictable* subset (``resident``), so without this it cannot see slots held
    #: by a busy/stuck engine and would under-evict — letting a single permanently-draining engine
    #: wedge the whole plane (an idle evictable engine never gets freed). Defaults to 0 → the policy
    #: falls back to ``len(resident)`` (the pre-fix behavior) when a caller does not supply it.
    resident_total: int = 0


class ResourceProbe(Protocol):
    def over_watermark(self) -> bool: ...


class NullProbe:
    """Default: no memory-pressure signal. Count ceiling is the only trigger."""

    def over_watermark(self) -> bool:
        return False


class PsutilProbe:
    """Opportunistic RAM high-watermark — a coarse RSS proxy, off by default.

    Never the thing correctness depends on (it ignores VRAM and lags lazy
    allocation); the count ceiling stays the deterministic primary mechanism.
    """

    def __init__(self, threshold_percent: float = 90.0) -> None:
        self._threshold = threshold_percent

    def over_watermark(self) -> bool:
        import psutil

        return psutil.virtual_memory().percent >= self._threshold


class EvictionPolicy(Protocol):
    def select_victims(
        self,
        resident: list[ResidentView],
        incoming: PendingLoad,
        budget: ResourceBudget,
    ) -> list[str]:
        """Return logical ids to evict so ``incoming`` fits. ``resident`` contains
        only evictable engines, already excluding in-flight ones."""
        ...


class CountAndPriorityPolicy:
    def select_victims(
        self,
        resident: list[ResidentView],
        incoming: PendingLoad,
        budget: ResourceBudget,
    ) -> list[str]:
        need = 0
        # The incoming load takes one slot: free enough to stay within the ceiling. Count ALL
        # resident engines (including non-evictable in-flight/draining ones), not just the evictable
        # subset handed to us — otherwise a stuck draining engine's slot is invisible and we
        # under-evict, wedging the plane while an idle engine sits evictable. ``resident_total``
        # defaults to 0 → fall back to ``len(resident)`` for callers that don't supply it.
        total_resident = max(budget.resident_total, len(resident))
        over_count = (total_resident + 1) - budget.max_resident
        if over_count > 0:
            need = over_count
        if budget.over_watermark:
            need = max(need, 1)
        if need <= 0:
            return []
        # Lowest priority first; tie-break least-recently-used.
        order = sorted(resident, key=lambda r: (r.priority, r.last_used))
        return [r.logical_id for r in order[:need]]
