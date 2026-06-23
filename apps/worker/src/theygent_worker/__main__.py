"""Run the durable worker: ``python -m theygent_worker`` / ``theygent-worker``.

Reads ``DATABASE_URL`` (shared with the control-plane; DBOS state goes in the ``dbos`` schema) and
``THEYGENT_INFERENCE_BASE_URL`` (the gateway data-plane root). One deploy-time note (m13-dbos.md
§3): the DBOS system schema is migrated automatically on launch (``run_dbos_database_migrations``),
so there is no separate ``dbos migrate`` step to remember — but it is a real, idempotent migration,
not a running service.
"""

from __future__ import annotations

import asyncio

from theygent_worker.app import run_worker


def main() -> None:
    try:
        asyncio.run(run_worker())
    except KeyboardInterrupt:  # graceful Ctrl-C — the worker's finally tears DBOS down
        pass


if __name__ == "__main__":
    main()
