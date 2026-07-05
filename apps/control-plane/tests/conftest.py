"""Fixtures for the fast suite: control-plane against a real (fake-model) inference plane
AND a REAL ephemeral Postgres applied through REAL Alembic migrations.

The rule: never SQLite, never metadata.create_all, never a fake DB. A single
testcontainers Postgres per session, schema applied via ``alembic upgrade head`` (so the
migration chain itself is exercised), truncated between tests. Skips clean with a clear
message when Docker/Postgres is unavailable locally; CI always provides Postgres.
"""

from __future__ import annotations

import asyncio
import os
from collections.abc import Iterator
from pathlib import Path

import pytest
from _db import truncate
from _fake_inference import FakeInference
from alembic import command
from alembic.config import Config
from fastapi.testclient import TestClient
from theygent_control_plane.app import create_app

_CONTROL_PLANE = Path(__file__).resolve().parents[1]
_ALEMBIC_INI = _CONTROL_PLANE / "alembic.ini"


def _alembic_config(url: str) -> Config:
    cfg = Config(str(_ALEMBIC_INI))
    # env.py reads DATABASE_URL; set it too so app + migrations share one DSN.
    os.environ["DATABASE_URL"] = url
    return cfg


@pytest.fixture(scope="session")
def pg_url() -> Iterator[str]:
    """A real Postgres for the session, schema applied via Alembic (never create_all)."""
    try:
        from testcontainers.postgres import PostgresContainer
    except ImportError:  # pragma: no cover
        pytest.skip("testcontainers not installed")

    try:
        container = PostgresContainer("postgres:16", driver="asyncpg")
        container.start()
    except Exception as exc:  # Docker not available locally — skip clean.
        pytest.skip(f"Docker/Postgres unavailable: {exc}")

    try:
        url = container.get_connection_url()
        command.upgrade(_alembic_config(url), "head")
        # The DBOS system schema (`dbos`) is migrated alongside Alembic's `public`, against
        # the SAME throwaway Postgres — proving the two schemas coexist and the durable suite gets
        # checkpoint tables. DBOS owns `dbos`; Alembic never touches it. Idempotent,
        # so a DurableRuntime.launch() re-running it in a test is a no-op.
        from theygent_control_plane.durable import run_dbos_migrations

        run_dbos_migrations(url)
        yield url
    finally:
        container.stop()


@pytest.fixture(autouse=True)
def clean_db(request: pytest.FixtureRequest) -> Iterator[None]:
    # Every fast-suite test starts from an empty schema; autouse so even inline-app tests
    # get a DB (and DATABASE_URL) and are isolated. asyncio.run is fine here — sync
    # fixture, no running loop.
    #
    # Integration tests are excluded: they run on hosts without Docker (the macOS real-MLX
    # job) and bring their own real Postgres via DATABASE_URL, so they must NOT pull in the
    # testcontainers `pg_url` (which would force-skip them when Docker is absent).
    if request.node.get_closest_marker("integration"):
        yield
        return
    pg_url = request.getfixturevalue("pg_url")
    asyncio.run(truncate(pg_url))
    yield


@pytest.fixture
def fake_inference() -> Iterator[FakeInference]:
    with FakeInference() as server:
        yield server


@pytest.fixture
def client(fake_inference: FakeInference, pg_url: str) -> Iterator[TestClient]:
    app = create_app(inference_base_url=fake_inference.v1_url, database_url=pg_url)
    with TestClient(app) as test_client:
        yield test_client
