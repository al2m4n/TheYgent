"""The request log: every exchange recorded locally, exported to the control plane at least once,
and never at the expense of a request.

What it must guarantee:

* recording never blocks a request — a stalled disk drops observations (counted), never waits;
* every exchange the gateway handles becomes one record, refused and failed ones included;
* the spool survives crashes and outages: a torn write is isolated, the export cursor only moves
  on an acknowledgement, a restart resumes where export stopped, and a lost acknowledgement
  re-sends the same record ids (the control plane's idempotency keys);
* no prompt or response content reaches the log or the control plane (the canary test).
"""

from __future__ import annotations

import asyncio
import dataclasses
import json
import stat
import threading
import time
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest
from _fake_anthropic import fake_anthropic, serve, sse
from _fixtures import all_exchanges, load
from _support import (
    TEAM_KEY,
    WAIT_SEC,
    FakeSink,
    observation,
    send,
    spool_lines,
    spool_records,
    team_keys,
    until,
    wait_for,
)
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route
from theygent_ai_gateway.app import create_app
from theygent_ai_gateway.configsync import StaticConfigSource
from theygent_ai_gateway.identity import StaticKeySource
from theygent_ai_gateway.observe import Observation, ResponseObserver
from theygent_ai_gateway.policy import Plan
from theygent_ai_gateway.requestlog import (
    ControlPlaneEventSink,
    Cursor,
    Exporter,
    ExportRefused,
    LogStats,
    RequestLog,
    Spool,
)
from theygent_ai_gateway.settings import GatewaySettings
from theygent_ir.gateway import (
    Decision,
    GatewayConfig,
    GatewayEventAck,
    GatewayEventBatch,
    GatewayRequestRecord,
)

T0 = datetime(2026, 10, 10, 9, tzinfo=UTC)
CANARY = "CANARY-7f3a91c2-never-leaves-the-gateway"
#: A gateway attached to a control plane records only once configured to observe.
OBSERVING = GatewayConfig(version=1, mode="observer", updated_at=T0)


# ── helpers ──────────────────────────────────────────────────────────────────


def record(n: int, gateway: str = "gw-test") -> GatewayRequestRecord:
    return GatewayRequestRecord(
        id=f"r{n:05d}",
        gateway_id=gateway,
        started_at=T0,
        endpoint="messages",
        outcome="complete",
        credential_kind="oauth",
        credential_profile="passthrough",
        request_class="main",
        decision=Decision(action="stay", reasons=["mid_conversation"]),
    )


def line_of(r: GatewayRequestRecord) -> bytes:
    return r.model_dump_json().encode() + b"\n"


def fast_exporter(spool: Spool, sink: FakeSink, stats: LogStats) -> Exporter:
    return Exporter(
        spool,
        sink,
        stats=stats,
        appended=asyncio.Event(),
        linger_seconds=0,
        poll_seconds=0.01,
        backoff_base_seconds=0.001,
        backoff_cap_seconds=0.005,
    )


def filled_spool(directory: Path, count: int, **options: int) -> tuple[Spool, LogStats, list[str]]:
    stats = LogStats()
    spool = Spool(directory, stats=stats, exporting=True, **options)
    records = [record(n) for n in range(count)]
    spool.append([line_of(r) for r in records])
    return spool, stats, [r.id for r in records]


class FakeControlPlane:
    """``POST /gateway/events`` as the control plane serves it: service key, strict batch,
    duplicates by record id acknowledged but not stored twice."""

    def __init__(self, service_key: str) -> None:
        self.service_key = service_key
        self.bodies: list[bytes] = []
        self.auth: list[str | None] = []
        self.stored: dict[str, GatewayRequestRecord] = {}
        self.status = 200

    def app(self) -> Starlette:
        async def events(request: Request) -> Response:
            body = await request.body()
            self.bodies.append(body)
            self.auth.append(request.headers.get("authorization"))
            if self.status != 200:
                return Response(status_code=self.status)
            if request.headers.get("authorization") != f"Bearer {self.service_key}":
                return Response(status_code=401)
            batch = GatewayEventBatch.model_validate_json(body)
            fresh = [r for r in batch.records if r.id not in self.stored]
            self.stored.update((r.id, r) for r in fresh)
            ack = GatewayEventAck(accepted=len(fresh), duplicates=len(batch.records) - len(fresh))
            return JSONResponse(ack.model_dump(mode="json"))

        return Starlette(routes=[Route("/gateway/events", events, methods=["POST"])])


@pytest.fixture
def control_plane() -> Iterator[tuple[FakeControlPlane, str]]:
    fake = FakeControlPlane("tygs_service_key")
    with serve(fake.app()) as url:
        yield fake, url


# ── every exchange becomes a record ──────────────────────────────────────────


def test_every_exchange_through_the_gateway_is_recorded(tmp_path: Path) -> None:
    exchanges = all_exchanges()
    with fake_anthropic() as upstream:
        settings = GatewaySettings(
            upstream_url=upstream.base_url, gateway_id="gw-test", state_dir=tmp_path
        )
        with serve(create_app(settings)) as url:
            for exchange in exchanges:
                send(url, exchange)
        # Leaving the block stops the gateway, which writes whatever is still queued.
    records = spool_records(tmp_path / "requests")
    assert len(records) == len(exchanges)
    assert len({r.id for r in records}) == len(records)
    assert {r.gateway_id for r in records} == {"gw-test"}
    assert {r.outcome for r in records} == {"complete", "upstream_broke"}
    assert sum(r.outcome == "upstream_broke" for r in records) == sum(
        e.abort_after_events is not None for e in exchanges
    )


def test_a_record_carries_the_exchanges_metadata(tmp_path: Path) -> None:
    exchange = load("subagent-explore-sonnet-1")
    config = GatewayConfig(version=7, mode="observer", updated_at=T0)
    seen: list[Observation] = []
    with fake_anthropic() as upstream:
        settings = GatewaySettings(
            upstream_url=upstream.base_url, gateway_id="gw-test", state_dir=tmp_path
        )
        with serve(create_app(settings, observe=seen.append, config=config)) as url:
            send(url, exchange)
    [r] = spool_records(tmp_path / "requests")
    observer = ResponseObserver(exchange.response_header("content-type"))
    observer.feed(exchange.response_bytes())
    summary = observer.finish()
    assert (r.config_version, r.endpoint, r.status, r.outcome) == (7, "messages", 200, "complete")
    assert r.request_class == "subagent"
    assert r.session_id == exchange.request_header("x-claude-code-session-id")
    assert r.agent_id == exchange.request_header("x-claude-code-agent-id")
    assert r.requested_model == exchange.body["model"]
    assert r.served_model == summary.model == "claude-sonnet-5-5"
    assert r.usage == summary.usage
    assert r.cost_usd is not None and r.cost_usd == pytest.approx(seen[0].cost_usd)
    assert r.pricing_version == "2026-09-25"
    assert r.decision == seen[0].decision
    assert r.shape is not None and r.fingerprint is not None
    assert (r.credential_kind, r.credential_profile, r.developer_id) == (
        "oauth",
        "passthrough",
        None,
    )


def test_team_records_name_the_developer_and_refusals_say_why(tmp_path: Path) -> None:
    with fake_anthropic() as upstream:
        settings = GatewaySettings(
            upstream_url=upstream.base_url,
            auth_mode="team",
            control_plane_url="http://127.0.0.1:1",
            service_key="tygs_service_key",
            gateway_id="gw-team",
            state_dir=tmp_path,
        )
        app = create_app(
            settings,
            key_source=StaticKeySource(team_keys()),
            event_sink=FakeSink(),
            config_source=StaticConfigSource(OBSERVING),
        )
        with serve(app) as url:
            wait_for(lambda: httpx.get(f"{url}/readyz").status_code == 200, "keys")
            body = json.dumps({"model": "claude-opus-5-5", "max_tokens": 1, "messages": []})
            oauth = "Bearer sk-ant-oat01-example"
            ok = httpx.post(
                f"{url}/v1/messages",
                content=body,
                headers={"authorization": oauth, "x-theygent-gateway-key": TEAM_KEY},
            )
            refused = httpx.post(
                f"{url}/v1/messages", content=body, headers={"authorization": oauth}
            )
    assert (ok.status_code, refused.status_code) == (200, 401)
    forwarded, rejected = sorted(spool_records(tmp_path / "requests"), key=lambda r: r.outcome)
    assert (forwarded.outcome, forwarded.developer_id) == ("complete", "usr_seat")
    assert (rejected.outcome, rejected.status, rejected.developer_id) == ("rejected", 401, None)
    assert rejected.decision.action == "stay"
    assert rejected.decision.reasons == ["request_refused"]


def test_a_policy_failure_is_recorded_not_lost(tmp_path: Path) -> None:
    class BrokenPolicy:
        def decide(self, *args: object, **kwargs: object) -> Plan:
            raise RuntimeError("policy bug")

    with fake_anthropic() as upstream:
        settings = GatewaySettings(
            upstream_url=upstream.base_url, gateway_id="gw-test", state_dir=tmp_path
        )
        app = create_app(settings)
        app.state.policy = BrokenPolicy()
        with serve(app) as url:
            send(url, load("subagent-explore-sonnet-1"))
    [r] = spool_records(tmp_path / "requests")
    assert r.outcome == "complete"
    assert r.decision.reasons == ["policy_error"]
    assert r.usage.output_tokens > 0, "the exchange's usage is kept"


def test_passthrough_mode_records_nothing(tmp_path: Path) -> None:
    config = GatewayConfig(version=3, mode="passthrough", updated_at=T0)
    seen: list[Observation] = []
    with fake_anthropic() as upstream:
        settings = GatewaySettings(
            upstream_url=upstream.base_url, gateway_id="gw-test", state_dir=tmp_path
        )
        with serve(create_app(settings, observe=seen.append, config=config)) as url:
            send(url, load("subagent-explore-sonnet-1"))
            wait_for(lambda: bool(seen), "the observation")
    assert seen[0].mode == "passthrough"
    assert spool_lines(tmp_path / "requests") == []


def test_the_log_is_private(tmp_path: Path) -> None:
    spool, _, _ = filled_spool(tmp_path / "requests", 3)
    spool.commit(Cursor(1, 10))
    assert stat.S_IMODE((tmp_path / "requests").stat().st_mode) == 0o700
    for path in (tmp_path / "requests").iterdir():
        assert stat.S_IMODE(path.stat().st_mode) == 0o600, path.name


# ── recording never waits ────────────────────────────────────────────────────


class StalledSpool(Spool):
    """A spool whose disk hangs until released."""

    def __init__(self, directory: Path, stats: LogStats) -> None:
        super().__init__(directory, stats=stats)
        self.release = threading.Event()
        self.entered = threading.Event()

    def append(self, lines: list[bytes]) -> None:
        self.entered.set()
        self.release.wait(WAIT_SEC)
        super().append(lines)


async def test_recording_never_waits_for_the_disk(tmp_path: Path) -> None:
    stats = LogStats()
    spool = StalledSpool(tmp_path, stats)
    log = RequestLog(spool, gateway_id="gw-test", stats=stats, max_queue=10)
    log.start()
    seen = observation()
    started = time.perf_counter()
    for _ in range(26):
        log.record(seen)
    assert time.perf_counter() - started < 0.05
    assert stats.dropped == 16  # the queue holds ten; the rest are dropped, not waited on
    await until(spool.entered.is_set, "the writer to reach the stalled disk")
    started = time.perf_counter()
    for _ in range(5):
        log.record(seen)  # the writer is stuck on the disk; the hook is not
    assert time.perf_counter() - started < 0.05
    assert stats.dropped == 16
    spool.release.set()
    await log.stop()
    assert stats.recorded == 15
    assert len(spool_lines(tmp_path)) == 15


def test_a_stalled_disk_never_holds_up_a_request(tmp_path: Path) -> None:
    stats = LogStats()
    spool = StalledSpool(tmp_path / "requests", stats)
    log = RequestLog(spool, gateway_id="gw-test", stats=stats)
    with fake_anthropic() as upstream:
        settings = GatewaySettings(
            upstream_url=upstream.base_url, gateway_id="gw-test", state_dir=tmp_path
        )
        with serve(create_app(settings, request_log=log)) as url:
            for _ in range(3):
                response = send(url, load("subagent-explore-sonnet-1"))
                assert response is not None and response.status_code == 200
            wait_for(spool.entered.is_set, "the writer to reach the stalled disk")
            spool.release.set()
    assert stats.recorded == 3


async def test_an_observation_that_makes_no_record_is_counted_not_fatal(tmp_path: Path) -> None:
    stats = LogStats()
    log = RequestLog(Spool(tmp_path, stats=stats), gateway_id="gw-test", stats=stats)
    log.start()
    log.record(observation())
    log.record(dataclasses.replace(observation(), status=42))  # no HTTP status: no valid record
    log.record(observation())
    await log.stop()
    assert (stats.recorded, stats.unrecordable) == (2, 1)
    assert len(spool_records(tmp_path)) == 2


# ── the spool ────────────────────────────────────────────────────────────────


def test_segments_rotate_and_the_oldest_are_pruned(tmp_path: Path) -> None:
    stats = LogStats()
    spool = Spool(tmp_path, stats=stats, max_segment_bytes=2_000, max_segments=3, exporting=True)
    for n in range(60):
        spool.append([line_of(record(n))])
    segments = spool.segments()
    assert len(segments) == 3 and segments[0] > 1
    assert spool_records(tmp_path)[-1].id == "r00059"
    assert stats.pruned_unexported == segments[0] - 1  # nothing was exported, so each one counts


def test_pruning_what_was_exported_loses_nothing(tmp_path: Path) -> None:
    stats = LogStats()
    spool = Spool(tmp_path, stats=stats, max_segment_bytes=2_000, max_segments=3, exporting=True)
    for n in range(60):
        spool.append([line_of(record(n))])
        last = spool.segments()[-1]
        spool.commit(Cursor(last, (tmp_path / f"requests-{last:06d}.jsonl").stat().st_size))
    assert spool.segments()[0] > 1
    assert stats.pruned_unexported == 0


async def test_a_write_torn_by_a_crash_is_isolated(tmp_path: Path) -> None:
    (tmp_path / "requests-000001.jsonl").write_bytes(line_of(record(1)) + b'{"id":"r0000')
    spool, stats, _ = filled_spool(tmp_path, 0)  # a restart: the torn line gets its own line
    spool.append([line_of(record(2))])
    sink = FakeSink()
    exporter = fast_exporter(spool, sink, stats)
    while await exporter.export_once(500):
        pass
    assert sink.ids == ["r00001", "r00002"]
    assert stats.unreadable == 1


# ── export ───────────────────────────────────────────────────────────────────


async def test_every_record_is_exported_once_in_order(tmp_path: Path) -> None:
    spool, stats, written = filled_spool(tmp_path, 1_200)
    sink = FakeSink()
    exporter = fast_exporter(spool, sink, stats)
    while await exporter.export_once(500):
        pass
    assert [len(b.records) for b in sink.batches] == [500, 500, 200]
    assert sink.ids == written
    assert stats.exported == 1_200
    assert await exporter.export_once(500) == 0


async def test_an_outage_is_replayed_once_the_control_plane_returns(tmp_path: Path) -> None:
    spool, stats, written = filled_spool(tmp_path, 40)
    outage: list[Exception] = [
        httpx.ConnectError("control plane down"),
        ExportRefused(503),
        ExportRefused(401),
    ]
    sink = FakeSink(outage)
    task = asyncio.create_task(fast_exporter(spool, sink, stats).run())
    try:
        await until(
            lambda: len(sink.ids) == 40 and stats.last_export_error is None, "a healthy replay"
        )
    finally:
        task.cancel()
    assert sink.calls == 4
    assert sink.ids == written  # each once, in order
    assert stats.last_export_error is None


async def test_a_lost_acknowledgement_resends_the_same_ids(tmp_path: Path) -> None:
    spool, stats, written = filled_spool(tmp_path, 30)
    sink = FakeSink(lose_acks=1)
    task = asyncio.create_task(fast_exporter(spool, sink, stats).run())
    try:
        await until(lambda: len(sink.batches) == 2, "the re-send")
    finally:
        task.cancel()
    first, second = sink.batches
    assert [r.id for r in first.records] == [r.id for r in second.records] == written


async def test_a_refused_batch_is_retried_never_skipped(tmp_path: Path) -> None:
    spool, stats, written = filled_spool(tmp_path, 10)
    sink = FakeSink([ExportRefused(422), ExportRefused(422)])
    task = asyncio.create_task(fast_exporter(spool, sink, stats).run())
    try:
        await until(lambda: len(sink.ids) == 10, "the export")
    finally:
        task.cancel()
    assert sink.ids == written


async def test_a_batch_over_a_size_limit_is_split(tmp_path: Path) -> None:
    spool, stats, written = filled_spool(tmp_path, 300)
    sink = FakeSink(max_records=100)
    task = asyncio.create_task(fast_exporter(spool, sink, stats).run())
    try:
        await until(lambda: len(sink.ids) == 300, "the export")
    finally:
        task.cancel()
    assert sink.ids == written
    assert max(len(b.records) for b in sink.batches) <= 100


async def test_a_restart_resumes_from_the_cursor(tmp_path: Path) -> None:
    spool, stats, written = filled_spool(tmp_path, 20)
    sink = FakeSink()
    while await fast_exporter(spool, sink, stats).export_once(500):
        pass
    assert sink.ids == written
    restarted = Spool(tmp_path, stats=stats, exporting=True)
    exporter = fast_exporter(restarted, sink, stats)
    assert await exporter.export_once(500) == 0
    restarted.append([line_of(record(100))])
    assert await exporter.export_once(500) == 1
    assert sink.ids == [*written, "r00100"]


async def test_batches_never_mix_gateways(tmp_path: Path) -> None:
    stats = LogStats()
    spool = Spool(tmp_path, stats=stats, exporting=True)
    order = ["gw-a", "gw-a", "gw-b", "gw-b", "gw-a"]
    spool.append([line_of(record(n, gateway)) for n, gateway in enumerate(order)])
    sink = FakeSink()
    exporter = fast_exporter(spool, sink, stats)
    while await exporter.export_once(500):
        pass
    assert [(b.gateway_id, len(b.records)) for b in sink.batches] == [
        ("gw-a", 2),
        ("gw-b", 2),
        ("gw-a", 1),
    ]


async def test_the_control_plane_sink_posts_with_the_service_key(
    control_plane: tuple[FakeControlPlane, str],
) -> None:
    fake, url = control_plane
    batch = GatewayEventBatch(gateway_id="gw-test", records=[record(1), record(2)])
    async with httpx.AsyncClient() as client:
        sink = ControlPlaneEventSink(client, url, "tygs_service_key")
        assert await sink.send(batch) == GatewayEventAck(accepted=2, duplicates=0)
        assert await sink.send(batch) == GatewayEventAck(accepted=0, duplicates=2)
        fake.status = 503
        with pytest.raises(ExportRefused) as refused:
            await sink.send(batch)
    assert refused.value.status == 503
    assert fake.auth[0] == "Bearer tygs_service_key"
    assert GatewayEventBatch.model_validate_json(fake.bodies[0]) == batch


# ── no content leaves the gateway ────────────────────────────────────────────


def _canary_upstream(received: list[bytes]) -> Starlette:
    """An upstream that repeats the canary everywhere a response carries content."""

    def block(index: int, block: dict[str, object]) -> bytes:
        return sse(
            "content_block_start",
            {"type": "content_block_start", "index": index, "content_block": block},
        )

    def delta(index: int, delta: dict[str, object]) -> bytes:
        return sse(
            "content_block_delta", {"type": "content_block_delta", "index": index, "delta": delta}
        )

    async def messages(request: Request) -> Response:
        body = await request.body()
        received.append(body)
        if not json.loads(body).get("stream"):
            return JSONResponse(
                {
                    "type": "error",
                    "error": {"type": "invalid_request_error", "message": f"bad: {CANARY}"},
                },
                status_code=400,
            )
        start = {
            "type": "message_start",
            "message": {
                "id": "msg_canary",
                "type": "message",
                "role": "assistant",
                "model": "claude-opus-5-5",
                "content": [],
                "usage": {"input_tokens": 12, "output_tokens": 1},
            },
        }
        events = [
            sse("message_start", start),
            block(0, {"type": "thinking", "thinking": ""}),
            delta(0, {"type": "thinking_delta", "thinking": CANARY}),
            block(1, {"type": "text", "text": CANARY}),
            block(2, {"type": "tool_use", "id": "toolu_1", "name": "Read", "input": {}}),
            delta(2, {"type": "input_json_delta", "partial_json": json.dumps(CANARY)}),
            sse(
                "message_delta",
                {
                    "type": "message_delta",
                    "delta": {"stop_reason": "tool_use"},
                    "usage": {"output_tokens": 30},
                },
            ),
            sse("message_stop", {"type": "message_stop"}),
        ]
        return Response(b"".join(events), media_type="text/event-stream")

    return Starlette(routes=[Route("/v1/messages", messages, methods=["POST"])])


def test_no_prompt_or_response_content_leaves_the_gateway(
    tmp_path: Path, control_plane: tuple[FakeControlPlane, str]
) -> None:
    fake, cp_url = control_plane
    received: list[bytes] = []
    request = {
        "model": "claude-opus-5-5",
        "max_tokens": 1024,
        "system": [{"type": "text", "text": f"You are helpful. {CANARY}"}],
        "messages": [{"role": "user", "content": [{"type": "text", "text": CANARY}]}],
        "tools": [
            {"name": "Read", "description": CANARY, "input_schema": {"type": "object"}},
        ],
        "metadata": {"user_id": CANARY},
        "output_config": {"format": {"type": "json_schema", "schema": {"title": CANARY}}},
    }
    headers = {
        "authorization": "Bearer sk-ant-oat01-example",
        "x-claude-code-request-class": "main",
        "x-claude-code-session-id": "8d6c2a51-1f0e-4f43-9f7e-2f8c6b1d0a11",
    }
    with serve(_canary_upstream(received)) as upstream_url:
        settings = GatewaySettings(
            upstream_url=upstream_url,
            control_plane_url=cp_url,
            service_key="tygs_service_key",
            gateway_id="gw-test",
            state_dir=tmp_path,
        )
        with serve(create_app(settings, config=OBSERVING)) as url:
            for stream in (True, False):
                httpx.post(
                    f"{url}/v1/messages",
                    content=json.dumps({**request, "stream": stream}),
                    headers=headers,
                    timeout=WAIT_SEC,
                )
            wait_for(lambda: len(fake.stored) == 2, "both records at the control plane")
    assert len(received) == 2 and all(CANARY.encode() in body for body in received)
    spool_bytes = b"".join(p.read_bytes() for p in (tmp_path / "requests").iterdir())
    assert spool_bytes and CANARY.encode() not in spool_bytes
    assert fake.bodies and all(CANARY.encode() not in body for body in fake.bodies)
    outcomes = sorted((r.status, r.stop_reason, r.error_type) for r in fake.stored.values())
    assert outcomes == [(200, "tool_use", None), (400, None, "invalid_request_error")]
