"""Tests for the platform-settings surface — the typed catalog over ``platform_setting``.

The load-bearing claims proven here:

* **Precedence** is exactly env > stored > default, with the two distinct env states: a
  *pinned* key is locked (writes rejected, env value wins), a *capped* key stays editable and
  resolves to min(env, stored-or-default). An EMPTY env var equals unset.
* **PATCH is atomic**: one invalid key rejects the whole patch — nothing is stored.
* **Sensitive values never round-trip**: the row holds a ``secret_ref`` envelope, reads return
  names only, reset deletes the secret row in the same transaction.
* **The kill switch works**: a stored ``telemetry.otlp_enabled=false`` stops export even when
  the ambient endpoint env var is set — the env pins the endpoint *value*, never export *on*.
* **"Live" is honest across processes**: the refresher picks up a change written directly to
  Postgres (as another replica would write it) and re-runs the hooks.
* **The MCP connect timeout lives INSIDE connect**: a wedged transport raises a clean
  ``McpConnectionError`` within the bound and leaves no orphaned actor task behind.
"""

from __future__ import annotations

import asyncio
import json
import sys
import time
from typing import Any

import asyncpg
import pytest
from _db import plain_dsn
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient
from theygent_control_plane import db
from theygent_control_plane.app import create_app
from theygent_control_plane.mcp.client import McpConnectionError, _ActorMcpClient
from theygent_control_plane.observability import Telemetry
from theygent_control_plane.observability.otlp import probe_otlp_endpoint
from theygent_control_plane.rag.crawl import CrawledPage
from theygent_control_plane.rag.retrieve import RagRetriever
from theygent_control_plane.secrets import SecretStore
from theygent_control_plane.settings import (
    CATALOG,
    SettingsService,
    SettingsValidationError,
    register_telemetry_hooks,
)

_KEY = Fernet.generate_key().decode()


class _ServiceHarness:
    """A SettingsService over the real test Postgres, with owned engine lifecycle. TTL 0 so
    every ``get`` re-resolves — precedence tests flip env vars mid-test and must never read a
    14-seconds-fresh cache."""

    def __init__(self, pg_url: str) -> None:
        self.engine = db.create_engine(pg_url)
        self.sessionmaker = db.create_sessionmaker(self.engine)
        self.secrets = SecretStore.from_keys([_KEY])
        self.service = SettingsService(
            sessionmaker=self.sessionmaker, secrets=self.secrets, cache_ttl_s=0.0
        )

    async def __aenter__(self) -> _ServiceHarness:
        await self.service.load()
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.service.stop_refresher()
        await self.engine.dispose()


async def _raw_upsert(pg_url: str, key: str, value: Any) -> None:
    """Write a row the way ANOTHER replica would (straight SQL) — the refresher's input."""
    conn = await asyncpg.connect(dsn=plain_dsn(pg_url))
    try:
        await conn.execute(
            "INSERT INTO platform_setting (key, value, updated_at) "
            "VALUES ($1, $2::jsonb, now()) "
            "ON CONFLICT (key) DO UPDATE SET value = $2::jsonb, updated_at = now()",
            key,
            json.dumps(value),
        )
    finally:
        await conn.close()


async def _row_count(pg_url: str, table: str) -> int:
    conn = await asyncpg.connect(dsn=plain_dsn(pg_url))
    try:
        return await conn.fetchval(f"SELECT count(*) FROM {table}")
    finally:
        await conn.close()


async def _row_value(pg_url: str, key: str) -> Any:
    conn = await asyncpg.connect(dsn=plain_dsn(pg_url))
    try:
        raw = await conn.fetchval("SELECT value FROM platform_setting WHERE key = $1", key)
        return json.loads(raw) if raw is not None else None
    finally:
        await conn.close()


def _entry(payload: dict[str, Any], key: str) -> dict[str, Any]:
    return next(e for e in payload["settings"] if e["key"] == key)


@pytest.fixture
def client(fake_inference, pg_url: str):  # type: ignore[no-untyped-def]
    app = create_app(
        inference_base_url=fake_inference.v1_url, database_url=pg_url, secret_key=[_KEY]
    )
    with TestClient(app) as test_client:
        yield test_client


# ── precedence (env pin / env cap / empty-env / default) ─────────────────────


async def test_precedence_env_pin_beats_stored_and_empty_env_is_unset(
    pg_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    async with _ServiceHarness(pg_url) as h:
        # Default (no env, no row).
        assert await h.service.get("mcp.call_timeout_s") == 120.0
        # Stored beats default.
        await h.service.patch({"mcp.call_timeout_s": 60})
        assert await h.service.get("mcp.call_timeout_s") == 60.0
        # Explicit env PINS: the env value wins over the stored one, and writes are rejected.
        monkeypatch.setenv("THEYGENT_MCP_CALL_TIMEOUT_S", "45")
        snap = await h.service.snapshot()
        entry = next(e for e in snap["settings"] if e["key"] == "mcp.call_timeout_s")
        assert entry["value"] == 45.0
        assert entry["source"] == "env"
        assert entry["env_pinned"] is True
        assert entry["stored_value"] == 60.0  # the stored intent stays visible
        with pytest.raises(SettingsValidationError) as excinfo:
            await h.service.patch({"mcp.call_timeout_s": 30})
        assert "locked" in excinfo.value.errors["mcp.call_timeout_s"]
        # ...but a RESET of a pinned key is allowed (it only deletes the stored row).
        assert await h.service.patch({"mcp.call_timeout_s": None}) == {}
        # EMPTY env == unset: back to the default, not pinned, not an int('') crash.
        monkeypatch.setenv("THEYGENT_MCP_CALL_TIMEOUT_S", "")
        snap = await h.service.snapshot()
        entry = next(e for e in snap["settings"] if e["key"] == "mcp.call_timeout_s")
        assert entry["value"] == 120.0
        assert entry["source"] == "default"
        assert entry["env_pinned"] is False


async def test_env_cap_is_min_of_env_and_stored(
    pg_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    async with _ServiceHarness(pg_url) as h:
        monkeypatch.setenv("THEYGENT_IO_CAPTURE_MAX_BYTES", "2048")
        # Stored ABOVE the cap: effective is the cap, entry says so honestly.
        await h.service.patch({"telemetry.io_capture_max_bytes": 8192})
        snap = await h.service.snapshot()
        entry = next(e for e in snap["settings"] if e["key"] == "telemetry.io_capture_max_bytes")
        assert entry["value"] == 2048
        assert entry["env_capped"] is True
        assert entry["env_cap_value"] == 2048
        assert entry["stored_value"] == 8192
        assert entry["source"] == "stored"
        # Stored UNDER the cap: the stored value applies (capped ≠ locked).
        await h.service.patch({"telemetry.io_capture_max_bytes": 1500})
        assert await h.service.get("telemetry.io_capture_max_bytes") == 1500
        # The enum cap uses the capture-level order, not lexicographic min.
        monkeypatch.setenv("THEYGENT_IO_CAPTURE", "metadata")
        assert await h.service.get("telemetry.io_capture") == "metadata"  # min(metadata, full)
        await h.service.patch({"telemetry.io_capture": "off"})
        assert await h.service.get("telemetry.io_capture") == "off"  # stored tightens further


async def test_stored_invalid_resolves_to_default_and_is_flagged(pg_url: str) -> None:
    await _raw_upsert(pg_url, "rag.default_top_k", "nope")  # e.g. written by a newer version
    async with _ServiceHarness(pg_url) as h:
        snap = await h.service.snapshot()
        entry = next(e for e in snap["settings"] if e["key"] == "rag.default_top_k")
        assert entry["value"] == 5  # the default applies — never a half-applied value
        assert entry["source"] == "default"
        assert entry["stored_invalid"] is True
        assert entry["stored_value"] == "nope"  # what is stored stays visible


# ── GET shape (catalog + boot block + orphaned) ──────────────────────────────


def test_get_settings_shape_with_boot_block_and_orphans(client: TestClient, pg_url: str) -> None:
    asyncio.run(_raw_upsert(pg_url, "old.retired_key", 3))
    resp = client.get("/settings")
    assert resp.status_code == 200
    payload = resp.json()
    assert {e["key"] for e in payload["settings"]} == {s.key for s in CATALOG}
    entry = _entry(payload, "telemetry.io_capture")
    assert set(entry) == {
        "key",
        "value",
        "stored_value",
        "default",
        "type",
        "source",
        "env_var",
        "env_pinned",
        "env_capped",
        "env_cap_value",
        "apply",
        "restart_pending",
        "stored_invalid",
        "constraints",
        "description",
        "group",
    }
    assert payload["orphaned"] == ["old.retired_key"]
    boot = {e["key"]: e for e in payload["boot"]}
    assert boot["durable"]["value"] is False
    assert boot["topology"]["value"] in ("local", "hosted")
    assert boot["inference_plane_url"]["value"].endswith("/v1")
    assert isinstance(boot["cors_origins"]["value"], list)
    assert isinstance(boot["artifact_dir"]["value"], str)
    # No invoke token in the test app → the surface-closed warning shows.
    assert boot["invoke_token_set"]["value"] is False
    assert boot["invoke_token_set"]["warning"]
    # An explicit key was injected → not ephemeral, no warning.
    assert boot["secret_key_ephemeral"]["value"] is False
    assert boot["secret_key_ephemeral"]["warning"] is None
    # Database diagnostics: host + database name ONLY — never credentials or the DSN.
    from sqlalchemy.engine import make_url

    url = make_url(pg_url)
    assert boot["database"]["value"] == {"host": url.host, "database": url.database}
    # No credential-shaped material anywhere on the page: the username never appears as a
    # field and the DSN (which would carry user:password@) is never echoed.
    assert set(boot["database"]["value"]) == {"host", "database"}
    assert f"{url.username}:{url.password}@" not in resp.text


# ── PATCH semantics (atomicity · reset · orphan delete) ──────────────────────


def test_patch_is_atomic_one_bad_key_stores_nothing(client: TestClient, pg_url: str) -> None:
    resp = client.patch("/settings", json={"rag.default_top_k": 7, "rag.chunk_max_tokens": 999_999})
    assert resp.status_code == 422
    body = resp.json()
    assert body["error"]["code"] == "invalid_setting"
    assert "rag.chunk_max_tokens" in body["error"]["message"]
    # NOTHING stored — including the valid key that rode in the same patch.
    assert asyncio.run(_row_count(pg_url, "platform_setting")) == 0
    payload = client.get("/settings").json()
    assert _entry(payload, "rag.default_top_k")["value"] == 5
    # Unknown keys with a non-null value are rejected loudly too.
    resp = client.patch("/settings", json={"no.such_key": 1})
    assert resp.status_code == 422
    assert "unknown setting" in resp.json()["error"]["message"]


def test_patch_null_resets_and_deletes_orphans(client: TestClient, pg_url: str) -> None:
    resp = client.patch("/settings", json={"rag.default_top_k": 9})
    assert resp.status_code == 200
    entry = _entry(resp.json(), "rag.default_top_k")
    assert (entry["value"], entry["source"], entry["stored_value"]) == (9, "stored", 9)
    resp = client.patch("/settings", json={"rag.default_top_k": None})
    assert resp.status_code == 200
    entry = _entry(resp.json(), "rag.default_top_k")
    assert (entry["value"], entry["source"], entry["stored_value"]) == (5, "default", None)
    assert asyncio.run(_row_count(pg_url, "platform_setting")) == 0
    # An orphaned row (key no longer in the catalog) is deletable via the same null patch —
    # the unknown-key rejection applies only to non-null values.
    asyncio.run(_raw_upsert(pg_url, "old.retired_key", 3))
    resp = client.patch("/settings", json={"old.retired_key": None})
    assert resp.status_code == 200
    assert resp.json()["orphaned"] == []
    assert asyncio.run(_row_count(pg_url, "platform_setting")) == 0


# ── sensitive values (the OTLP headers map) ──────────────────────────────────


def test_sensitive_roundtrip_names_only_and_reset_deletes_secret(
    client: TestClient, pg_url: str
) -> None:
    secret_value = "Bearer swordfish-9000"
    resp = client.patch(
        "/settings", json={"telemetry.otlp_headers": {"authorization": secret_value}}
    )
    assert resp.status_code == 200
    assert secret_value not in resp.text  # the value NEVER returns
    entry = _entry(resp.json(), "telemetry.otlp_headers")
    assert entry["value"] == {"set": True, "names": ["authorization"]}
    assert entry["stored_value"] == {"set": True, "names": ["authorization"]}
    assert entry["source"] == "stored"
    # At rest: the row holds a secret_ref envelope; the material lives encrypted in `secret`.
    row = asyncio.run(_row_value(pg_url, "telemetry.otlp_headers"))
    assert row["names"] == ["authorization"]
    assert row["secret_ref"].startswith("sec_")
    assert asyncio.run(_row_count(pg_url, "secret")) == 1
    assert secret_value not in client.get("/settings").text

    # Server-side resolution (what the exporter build uses) decrypts the map.
    async def _resolve() -> dict[str, str] | None:
        async with _ServiceHarness(pg_url) as h:
            return await h.service.resolve_sensitive("telemetry.otlp_headers")

    assert asyncio.run(_resolve()) == {"authorization": secret_value}
    # Reset deletes the secret row in the same transaction as the setting row.
    resp = client.patch("/settings", json={"telemetry.otlp_headers": None})
    assert resp.status_code == 200
    assert _entry(resp.json(), "telemetry.otlp_headers")["value"] is None
    assert asyncio.run(_row_count(pg_url, "secret")) == 0
    assert asyncio.run(_row_count(pg_url, "platform_setting")) == 0


# ── apply hooks (best-effort, never a rollback) ──────────────────────────────


async def test_failing_hook_reports_apply_error_but_value_persists(pg_url: str) -> None:
    async with _ServiceHarness(pg_url) as h:

        async def bad_hook() -> None:
            raise RuntimeError("collector exploded")

        h.service.register_hook("rag.default_top_k", bad_hook)
        apply_errors = await h.service.patch({"rag.default_top_k": 11})
        assert apply_errors == {"rag.default_top_k": "collector exploded"}
        # The stored value survives the hook failure — apply is best-effort by contract.
        assert await h.service.get("rag.default_top_k") == 11
        assert await _row_value(pg_url, "rag.default_top_k") == 11


async def test_refresher_retries_a_failed_hook_on_the_next_tick(pg_url: str) -> None:
    # A hook that fails on one refresh tick must be RETRIED on the next: the baseline only
    # advances for keys whose hooks ran clean, so the diff stays dirty until the apply
    # succeeds. Without that, a transient failure (collector briefly down) would leave the
    # process desynced until the stored value happened to change again.
    async with _ServiceHarness(pg_url) as h:
        attempts: list[int] = []
        succeeded = asyncio.Event()

        async def flaky_hook() -> None:
            attempts.append(await h.service.get("rag.default_top_k"))
            if len(attempts) == 1:
                raise RuntimeError("collector still down")
            succeeded.set()

        h.service.register_hook("rag.default_top_k", flaky_hook)
        # The value changes ONCE (another replica's write); the first apply attempt fails.
        await _raw_upsert(pg_url, "rag.default_top_k", 9)
        h.service.start_refresher(period_s=0.05)
        await asyncio.wait_for(succeeded.wait(), timeout=5)
        # Two attempts, same value both times — the RETRY applied it, not a second change.
        assert attempts == [9, 9]
        # Once applied, subsequent ticks diff clean: give the refresher a beat and confirm
        # the hook is not re-run for an unchanged value.
        await asyncio.sleep(0.2)
        assert attempts == [9, 9]


# ── extra registries (env parity with the boot parser) ───────────────────────


async def test_registries_env_with_extra_keys_resolves_normalized(
    pg_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A hand-written env entry may carry annotation keys ("note") the boot-time parser
    # tolerates; the setting must resolve the same value (source env), normalized to
    # exactly {id,label,url} — never degrade to "env unset" and strip the allowlist.
    monkeypatch.setenv(
        "THEYGENT_MCP_REGISTRIES",
        json.dumps(
            [
                {
                    "id": "mirror",
                    "label": "Mirror",
                    "url": "https://mcp.mirror.test",
                    "note": "air-gapped allowlist",
                }
            ]
        ),
    )
    async with _ServiceHarness(pg_url) as h:
        snap = await h.service.snapshot()
        entry = next(e for e in snap["settings"] if e["key"] == "mcp.extra_registries")
        assert entry["source"] == "env"
        assert entry["env_pinned"] is True
        assert entry["value"] == [
            {"id": "mirror", "label": "Mirror", "url": "https://mcp.mirror.test"}
        ]
        # Extra keys are tolerated on a stored write too — and the NORMALIZED rows are what
        # get stored and applied (env cleared so the key is writable again).
        monkeypatch.delenv("THEYGENT_MCP_REGISTRIES")
        await h.service.patch(
            {
                "mcp.extra_registries": [
                    {"id": "m2", "label": "Mirror 2", "url": "https://m2.test", "note": "kept out"}
                ]
            }
        )
        assert await h.service.get("mcp.extra_registries") == [
            {"id": "m2", "label": "Mirror 2", "url": "https://m2.test"}
        ]
        assert await _row_value(pg_url, "mcp.extra_registries") == [
            {"id": "m2", "label": "Mirror 2", "url": "https://m2.test"}
        ]
        # Missing/blank required keys still fail loudly.
        with pytest.raises(SettingsValidationError):
            await h.service.patch(
                {"mcp.extra_registries": [{"id": "x", "label": " ", "url": "https://u"}]}
            )


def test_boot_applies_env_registries_with_extra_keys(
    fake_inference, pg_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    # End to end: the boot apply hook must hand set_registries the operator's self-hosted
    # rows (normalized), so the browse surface keeps the mirror alongside the built-ins.
    monkeypatch.setenv(
        "THEYGENT_MCP_REGISTRIES",
        json.dumps(
            [{"id": "mirror", "label": "Mirror", "url": "https://mcp.mirror.test", "note": "x"}]
        ),
    )
    app = create_app(
        inference_base_url=fake_inference.v1_url, database_url=pg_url, secret_key=[_KEY]
    )
    with TestClient(app) as test_client:
        listed = test_client.get("/admin/mcp/registries").json()["registries"]
        by_id = {r["id"]: r for r in listed}
        assert {"official", "github", "mirror"} <= set(by_id)
        assert by_id["mirror"] == {
            "id": "mirror",
            "label": "Mirror",
            "url": "https://mcp.mirror.test",
        }


# ── OTLP: holder swap, old-sink flush, and the kill switch ───────────────────


class _FakeSink:
    def __init__(self, endpoint: str | None = None, headers: Any = None, redact: Any = None):
        self.endpoint = endpoint
        self.headers = headers
        self.redact_attrs = redact
        self.shutdowns = 0

    def shutdown(self) -> None:
        self.shutdowns += 1


async def test_otlp_hook_swaps_sink_flushes_old_and_kill_switch_wins(
    pg_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    built: list[_FakeSink] = []

    def fake_build(*, endpoint=None, send=None, headers=None, redact_attrs=None):  # type: ignore[no-untyped-def]
        sink = _FakeSink(endpoint, headers, redact_attrs)
        built.append(sink)
        return sink

    # otlp_sink_from_settings imports build_otlp_sink at call time — patch the source module.
    monkeypatch.setattr("theygent_control_plane.observability.otlp.build_otlp_sink", fake_build)
    async with _ServiceHarness(pg_url) as h:
        telemetry = Telemetry(sessionmaker=h.sessionmaker, ceiling="full", topology="full")
        old = _FakeSink()
        telemetry.otlp = old
        register_telemetry_hooks(h.service, telemetry)
        # Enable + point at a collector: the hook builds a NEW sink, swaps the holder, and
        # flushes the old one exactly once.
        errors = await h.service.patch(
            {
                "telemetry.otlp_enabled": True,
                "telemetry.otlp_endpoint": "http://collector:4318",
                "telemetry.otlp_redact_attrs": ["gen_ai.request.model"],
            }
        )
        assert errors == {}
        assert built and telemetry.otlp is built[-1]
        assert telemetry.otlp.endpoint == "http://collector:4318"
        assert telemetry.otlp.redact_attrs == frozenset({"gen_ai.request.model"})
        assert old.shutdowns == 1
        # THE KILL SWITCH: ambient env endpoint set, stored enabled=false ⇒ export OFF. The
        # env var pins the endpoint VALUE; it never pins export ON.
        monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://ambient:4318")
        current = telemetry.otlp
        errors = await h.service.patch({"telemetry.otlp_enabled": False})
        assert errors == {}
        assert telemetry.otlp is None
        assert current.shutdowns == 1  # the replaced sink was flushed, not leaked
        # And the capture hooks are plain live attribute sets.
        await h.service.patch({"telemetry.io_capture": "metadata"})
        assert telemetry.ceiling == "metadata"
        await h.service.patch({"telemetry.io_capture_max_bytes": 4096})
        assert telemetry.max_bytes == 4096


def test_app_boot_kill_switch_stored_false_beats_env_endpoint(
    fake_inference, pg_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Another replica already stored the kill switch; THIS process boots with the ambient
    # endpoint env var set — and must come up with export OFF.
    monkeypatch.setattr(
        "theygent_control_plane.observability.otlp.build_otlp_sink",
        lambda **kw: _FakeSink(kw.get("endpoint")),
    )
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://ambient:4318")
    asyncio.run(_raw_upsert(pg_url, "telemetry.otlp_enabled", False))
    app = create_app(
        inference_base_url=fake_inference.v1_url, database_url=pg_url, secret_key=[_KEY]
    )
    with TestClient(app) as test_client:
        assert test_client.app.state.telemetry.otlp is None  # type: ignore[attr-defined]
        # Flipping the switch back on over the API brings the env-pinned endpoint up live.
        resp = test_client.patch("/settings", json={"telemetry.otlp_enabled": True})
        assert resp.status_code == 200
        assert resp.json()["apply_errors"] == {}
        sink = test_client.app.state.telemetry.otlp  # type: ignore[attr-defined]
        assert sink is not None and sink.endpoint == "http://ambient:4318"


# ── the refresher (cross-process "live") ─────────────────────────────────────


async def test_refresher_applies_a_change_written_directly_to_the_db(pg_url: str) -> None:
    async with _ServiceHarness(pg_url) as h:
        applied: list[int] = []

        async def hook() -> None:
            applied.append(await h.service.get("rag.default_top_k"))

        fired = asyncio.Event()

        async def hook_event() -> None:
            await hook()
            fired.set()

        h.service.register_hook("rag.default_top_k", hook_event)
        h.service.start_refresher(period_s=0.05)
        # Simulate ANOTHER replica's PATCH: a plain row write this process never saw.
        await _raw_upsert(pg_url, "rag.default_top_k", 9)
        await asyncio.wait_for(fired.wait(), timeout=5)
        assert applied and applied[-1] == 9
        assert h.service.value("rag.default_top_k") == 9


# ── POST /settings/otlp:test ─────────────────────────────────────────────────


def test_otlp_test_route_maps_probe_result_and_requires_an_endpoint(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: dict[str, Any] = {}

    def fake_probe(*, endpoint, headers=None, redact_attrs=frozenset(), timeout_s=10.0):  # type: ignore[no-untyped-def]
        seen.update(endpoint=endpoint, headers=headers)
        return True, None

    monkeypatch.setattr("theygent_control_plane.app.probe_otlp_endpoint", fake_probe)
    resp = client.post(
        "/settings/otlp:test",
        json={"endpoint": "http://collector:4318", "headers": {"x-auth": "t"}},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is True and body["error"] is None
    assert isinstance(body["latency_ms"], (int, float)) and body["latency_ms"] >= 0
    assert seen == {"endpoint": "http://collector:4318", "headers": {"x-auth": "t"}}
    # No body endpoint + no stored/env endpoint → an honest 422, never a probe of nothing.
    resp = client.post("/settings/otlp:test", json={})
    assert resp.status_code == 422
    assert resp.json()["error"]["code"] == "invalid_setting"


def test_probe_reports_sdk_missing_honestly(monkeypatch: pytest.MonkeyPatch) -> None:
    # Simulate the exporter package being absent (None in sys.modules makes the import raise),
    # so this holds even on environments where the optional dep IS installed.
    monkeypatch.setitem(sys.modules, "opentelemetry.exporter.otlp.proto.http.trace_exporter", None)
    ok, error = probe_otlp_endpoint(endpoint="http://collector:4318")
    assert ok is False
    assert error is not None and "not installed" in error


# ── MCP connect timeout (inside connect; no zombie actor) ────────────────────


class _HangingClient(_ActorMcpClient):
    async def _open_transport(self, stack):  # type: ignore[no-untyped-def]
        await asyncio.Event().wait()  # a wedged spawn/handshake — never becomes ready


async def test_mcp_connect_timeout_raises_and_leaves_no_orphan_task() -> None:
    client = _HangingClient(connect_timeout=0.2)
    started = time.monotonic()
    with pytest.raises(McpConnectionError, match="timed out"):
        await client.connect()
    assert time.monotonic() - started < 2.0  # bounded by the knob, not a 5s grace drain
    assert client._task is None  # close() ran — the actor was torn down, not abandoned
    pending = [t for t in asyncio.all_tasks() if t is not asyncio.current_task() and not t.done()]
    assert pending == []  # no zombie actor task survives the failed connect


async def test_mcp_connect_timeout_accepts_a_live_accessor() -> None:
    client = _HangingClient(connect_timeout=lambda: 0.1)
    with pytest.raises(McpConnectionError, match="timed out"):
        await client.connect()
    assert client._task is None


# ── RAG knobs reach their sites ──────────────────────────────────────────────


def test_upload_cap_setting_is_read_per_request(client: TestClient) -> None:
    resp = client.post(
        "/rag/sources",
        json={"name": "docs", "kind": "upload", "config": {}, "embedding_model": "embed-x"},
    )
    assert resp.status_code == 201
    source_id = resp.json()["id"]
    body = b"x" * (1024 * 1024 + 1)
    # Under the default 50 MiB cap this size is fine; tighten the cap and the SAME request 413s
    # on the next call — no restart, no re-create.
    assert client.patch("/settings", json={"rag.max_upload_bytes": 1024 * 1024}).status_code == 200
    resp = client.post(
        f"/rag/sources/{source_id}/documents?filename=big.md",
        content=body,
        headers={"content-type": "text/markdown"},
    )
    assert resp.status_code == 413
    assert resp.json()["error"]["code"] == "document_too_large"
    assert "1 MiB" in resp.json()["error"]["message"]


def test_crawl_and_chunk_knobs_reach_their_sites(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    captured: dict[str, Any] = {}

    async def fake_crawl_site(config, on_page, *, on_visit=None):  # type: ignore[no-untyped-def]
        captured["crawl_config"] = config
        if on_visit is not None:
            await on_visit("http://127.0.0.1:1/docs/a")
        await on_page(
            CrawledPage(url="http://127.0.0.1:1/docs/a", title="A", markdown="hello world")
        )

    def fake_chunk_markdown(markdown, *, max_tokens, overlap_tokens):  # type: ignore[no-untyped-def]
        captured["chunk_kwargs"] = {"max_tokens": max_tokens, "overlap_tokens": overlap_tokens}
        return []  # no chunks → no embedding call → the settle records an honest failure

    monkeypatch.setattr("theygent_control_plane.rag.ingest.crawl_site", fake_crawl_site)
    monkeypatch.setattr("theygent_control_plane.rag.ingest.chunk_markdown", fake_chunk_markdown)

    resp = client.patch(
        "/settings",
        json={
            "rag.crawl_desired_concurrency": 1,
            "rag.crawl_max_concurrency": 2,
            "rag.chunk_max_tokens": 111,
            "rag.chunk_overlap_tokens": 11,
        },
    )
    assert resp.status_code == 200
    resp = client.post(
        "/rag/sources",
        json={
            "name": "site",
            "kind": "crawl",
            "config": {"root_url": "http://127.0.0.1:1/docs"},
            "embedding_model": "embed-x",
        },
    )
    assert resp.status_code == 201
    source_id = resp.json()["id"]
    assert client.post(f"/rag/sources/{source_id}:ingest").status_code == 202
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        status = client.get(f"/rag/sources/{source_id}").json()["status"]
        if status != "ingesting":
            break
        time.sleep(0.05)
    config = captured["crawl_config"]
    assert (config.desired_concurrency, config.max_concurrency) == (1, 2)
    assert captured["chunk_kwargs"] == {"max_tokens": 111, "overlap_tokens": 11}


async def test_rag_default_top_k_setting_applies_only_when_unset(pg_url: str) -> None:
    class _FakeStore:
        async def get_source(self, session, source_id):  # type: ignore[no-untyped-def]
            from theygent_control_plane.rag.store import RagSource

            return RagSource(
                id=source_id,
                name="s",
                kind="upload",
                config={},
                embedding_model="embed-x",
                embedding_dim=3,
                status="ready",
                chunks=1,
            )

        async def search(self, session, **kwargs):  # type: ignore[no-untyped-def]
            searches.append(kwargs["top_k"])
            return []

    class _FakeGateway:
        async def embed(self, *, model, inputs):  # type: ignore[no-untyped-def]
            data = [type("D", (), {"index": 0, "embedding": [0.1, 0.2, 0.3]})()]
            return type("R", (), {"data": data})()

    searches: list[int] = []
    engine = db.create_engine(pg_url)
    try:
        sm = db.create_sessionmaker(engine)
        retriever = RagRetriever(sm, _FakeGateway(), store=_FakeStore(), default_top_k=lambda: 17)
        await retriever.retrieve(source_id="rag_x", query="q", top_k=None)
        await retriever.retrieve(source_id="rag_x", query="q", top_k=2)  # explicit wins
        assert searches == [17, 2]
    finally:
        await engine.dispose()
