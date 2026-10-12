"""Helpers shared by the request-log and config tests."""

from __future__ import annotations

import asyncio
import hashlib
import time
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

import httpx
from _fixtures import Exchange, load
from theygent_ai_gateway.observe import Observation, ResponseSummary
from theygent_ai_gateway.policy import Plan, to_decision
from theygent_ai_gateway.requestlog import ExportRefused
from theygent_ai_gateway.signals import read_request
from theygent_ir.gateway import (
    GatewayEventAck,
    GatewayEventBatch,
    GatewayKey,
    GatewayKeySet,
    GatewayRequestRecord,
    Timing,
)

WAIT_SEC = 10
T0 = datetime(2026, 10, 10, 9, tzinfo=UTC)


def spool_lines(directory: Path) -> list[bytes]:
    """Every line in the spool, oldest segment first."""
    lines: list[bytes] = []
    for path in sorted(directory.glob("requests-*.jsonl")):
        lines.extend(line for line in path.read_bytes().split(b"\n") if line)
    return lines


def spool_records(directory: Path) -> list[GatewayRequestRecord]:
    return [GatewayRequestRecord.model_validate_json(line) for line in spool_lines(directory)]


def observation(name: str = "subagent-explore-sonnet-1") -> Observation:
    """An observation of a recorded exchange, built without the app."""
    exchange = load(name)
    headers = {k.lower(): v for k, v in exchange.request_headers}
    return Observation(
        started_at=T0,
        method="POST",
        endpoint="messages",
        request=read_request(headers, exchange.request_bytes(), has_messages_body=True),
        status=200,
        outcome="complete",
        timing=Timing(),
        response=ResponseSummary(),
        seat_allowance=None,
        decision=to_decision(Plan.stay("mid_conversation"), None),
    )


def wait_for(condition: Callable[[], bool], what: str) -> None:
    deadline = time.monotonic() + WAIT_SEC
    while not condition():
        assert time.monotonic() < deadline, f"timed out waiting for {what}"
        time.sleep(0.01)


async def until(condition: Callable[[], bool], what: str) -> None:
    deadline = time.monotonic() + WAIT_SEC
    while not condition():
        assert time.monotonic() < deadline, f"timed out waiting for {what}"
        await asyncio.sleep(0.005)


def send(url: str, exchange: Exchange) -> httpx.Response | None:
    try:
        return httpx.request(
            exchange.method,
            f"{url}{exchange.target}",
            content=exchange.request_bytes(),
            headers=[*exchange.request_headers, ("x-fake-fixture", exchange.name)],
            timeout=WAIT_SEC,
        )
    except httpx.HTTPError:
        return None  # a fixture whose upstream drops the connection mid-stream


class FakeSink:
    """A control plane in-process: stores batches, and fails as told."""

    def __init__(
        self,
        failures: list[Exception] | None = None,
        *,
        lose_acks: int = 0,
        max_records: int | None = None,
    ) -> None:
        self.batches: list[GatewayEventBatch] = []
        self.calls = 0
        self._failures = list(failures or [])
        self._lose_acks = lose_acks
        self._max_records = max_records

    async def send(self, batch: GatewayEventBatch) -> GatewayEventAck:
        self.calls += 1
        if self._max_records is not None and len(batch.records) > self._max_records:
            raise ExportRefused(413)
        if self._failures:
            raise self._failures.pop(0)
        self.batches.append(batch)
        if self._lose_acks:
            self._lose_acks -= 1
            raise httpx.ReadTimeout("the acknowledgement never arrived")
        return GatewayEventAck(accepted=len(batch.records), duplicates=0)

    @property
    def ids(self) -> list[str]:
        return [r.id for batch in self.batches for r in batch.records]


TEAM_KEY = "tyg_seat_developer_key"


def team_keys() -> GatewayKeySet:
    return GatewayKeySet(
        version=1,
        generated_at=T0,
        keys=[
            GatewayKey(
                key_id="key_seat",
                token_sha256=hashlib.sha256(TEAM_KEY.encode()).hexdigest(),
                developer_id="usr_seat",
                credential_profile="passthrough",
            )
        ],
    )
