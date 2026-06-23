"""theygent durable worker (M13, §8 process type 2).

The durable worker is the one process that "earns its own service" in the server / air-gapped
topology: it launches DBOS, registers the single ``theygent_run`` workflow (+ its steps and the
scheduled-fire workflow) via the control-plane's ``durable`` package, and consumes the durable
queue the control-plane enqueues onto. In the **desktop sidecar** topology this process is not
needed at all — the control-plane launches the very same DBOS runtime *in-process* (DBOS is a
library), so there is never a second architecture, only a second deployable.

The worker reaches inference only over the gateway HTTP seam (the plane boundary holds — it never
imports an engine), and shares M4's Postgres with the control-plane (app tables in ``public``, DBOS
state in ``dbos``).
"""

from theygent_worker.app import build_runtime, run_worker

__all__ = ["build_runtime", "run_worker"]
