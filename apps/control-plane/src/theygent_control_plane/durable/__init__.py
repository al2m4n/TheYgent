"""The durable runtime (M13) — DBOS-backed resume + native durable schedules.

The ONE place the ``dbos`` import lives (decisions D4): ``walker.py``, the node handlers, and the IR
stay runtime-agnostic; this package wraps the runtime-agnostic activity executors as ``@DBOS.step``
and lowers the IR to the single registered ``theygent_run`` workflow. Importing this package applies
the DBOS decorators (registering the workflows/steps); a process activates them with
``DurableRuntime(...).launch()``.
"""

from __future__ import annotations

from theygent_control_plane.durable.bus import BusDelta, DeltaBus
from theygent_control_plane.durable.runtime import DurableRuntime, run_dbos_migrations

__all__ = ["BusDelta", "DeltaBus", "DurableRuntime", "run_dbos_migrations"]
