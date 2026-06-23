"""DBOS configuration — the one place the durable runtime's DB wiring is decided (M13 §3, D5/D6).

DBOS is **dual-database**: a *system* schema (workflow/step checkpoints) plus theygent's existing
*application* tables. The "zero new service" promise (m13-dbos.md §0) depends on putting DBOS's
system tables in the **same Postgres** M4 already requires — in a **separate schema** (``dbos``),
which DBOS owns and Alembic never touches. So the DBOS system DB url is, by default, the SAME
instance as the app's ``DATABASE_URL`` (overridable via ``DBOS_SYSTEM_DATABASE_URL`` for an operator
who wants the checkpoints elsewhere).

DBOS speaks **psycopg/sync** Postgres urls, while the app uses ``postgresql+asyncpg://`` for its
async engine. ``to_dbos_url`` strips the ``+asyncpg`` driver suffix so the same DSN drives both.
"""

from __future__ import annotations

import os

from dbos import DBOSConfig

#: The schema DBOS owns (D5). Kept out of theygent's Alembic chain; created by
#: ``run_dbos_database_migrations(schema=DBOS_SCHEMA)``.
DBOS_SCHEMA = "dbos"

#: The DBOS application name — also the recovery namespace. One per deployment.
APP_NAME = "theygent"


def to_dbos_url(database_url: str) -> str:
    """Normalise an app DSN to the plain ``postgresql://`` url DBOS (psycopg) expects. The app's
    async engine uses ``postgresql+asyncpg://``; DBOS is sync/psycopg — strip the driver suffix so
    one configured DSN drives both planes against the same Postgres instance (D6)."""
    return database_url.replace("+asyncpg", "").replace("+psycopg", "")


def system_database_url(database_url: str) -> str:
    """The DBOS system-database url: ``DBOS_SYSTEM_DATABASE_URL`` if set, else the app's own
    Postgres (same instance — the zero-new-service rule, §3). Either way DBOS confines its tables to
    the ``dbos`` schema, so sharing the instance never collides with ``public``."""
    override = os.environ.get("DBOS_SYSTEM_DATABASE_URL")
    return to_dbos_url(override) if override else to_dbos_url(database_url)


def build_config(
    database_url: str,
    *,
    fast_polling: bool = False,
) -> DBOSConfig:
    """Assemble the :class:`DBOSConfig` (D6). ``fast_polling`` lowers DBOS's notification/scheduler
    poll intervals so the fast suite isn't wall-clock-bound (a schedule due "now" fires within a
    test's patience, not on the 30s production default); production leaves them at the defaults.
    OTLP export is left off here (M13 stamps node ``id`` as the step/span name via DBOS's own tracer
    — wiring an OTLP endpoint is the deferred observability milestone)."""
    config: DBOSConfig = {
        "name": APP_NAME,
        "system_database_url": system_database_url(database_url),
        "dbos_system_schema": DBOS_SCHEMA,
        # The control-plane runs DBOS embedded; it has no need for DBOS's own admin HTTP server
        # (one more port to bundle on the desktop sidecar — keep it off).
        "run_admin_server": False,
    }
    if fast_polling:
        config["notification_listener_polling_interval_sec"] = 0.1
        config["scheduler_polling_interval_sec"] = 1.0
        # Tests launch + destroy many DBOS instances against one small testcontainers Postgres;
        # cap the system-DB pool so the suite never exhausts ``max_connections`` (D6 test hygiene).
        config["sys_db_pool_size"] = 2
        config["db_engine_kwargs"] = {"pool_size": 2, "max_overflow": 0, "pool_pre_ping": True}
    return config
