"""The request log: every exchange as a ``GatewayRequestRecord``, kept in a local JSONL spool and
exported to the control plane in batches.

Recording must never slow or break a request. The observe hook only puts the observation on a
bounded in-memory queue; a writer task turns queued observations into records and appends them to
the spool from a worker thread. If the disk stalls long enough to fill the queue, new observations
are dropped and counted, never waited on.

The spool is a directory of numbered segments (``requests-000001.jsonl``, …), each ``0600`` in a
``0700`` directory, rotated by size and pruned by count, with the export cursor (segment, byte
offset) persisted beside them. The exporter reads from the cursor, sends up to
``MAX_EVENT_BATCH`` records, and moves the cursor only once the control plane has acknowledged the
batch. After an outage it resumes from the cursor, so nothing still in the spool is lost; a batch
whose acknowledgement was lost is sent again, which is safe because record ids are the control
plane's idempotency keys. A batch the control plane refuses is retried, never skipped: every record
was validated against the same wire types before it was written, so a refusal means the two sides
disagree (a version skew, a bug), and the records are accepted once that is fixed.

Records are metadata (the wire types refuse free text): nothing here reads or writes a prompt, a
response or a credential, and failures are logged by exception type only.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import random
import re
import threading
import uuid
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

import httpx
from pydantic import ValidationError
from theygent_ir.gateway import (
    MAX_EVENT_BATCH,
    GatewayEventAck,
    GatewayEventBatch,
    GatewayRequestRecord,
)

from theygent_ai_gateway.observe import Observation
from theygent_ai_gateway.signals import token_or_none

log = logging.getLogger(__name__)

#: A segment is closed once it reaches this size; the next record starts a new one.
DEFAULT_SEGMENT_BYTES = 16 << 20
#: Segments kept; the oldest is removed beyond this (256 MiB in all, roughly 170,000 records).
DEFAULT_MAX_SEGMENTS = 16
#: Observations waiting for the writer. Past this, new ones are dropped rather than waited on.
DEFAULT_MAX_QUEUE = 10_000
#: Observations the writer turns into records per trip to the worker thread.
WRITE_BATCH = 500
#: How much of a segment one read takes. A record is a few KB; a line longer than this is not one.
READ_BYTES = 1 << 20
#: How long the writer may take to drain on shutdown before what is left is abandoned.
STOP_SECONDS = 10.0
#: After new records arrive, the exporter waits this long so they travel in one batch.
EXPORT_LINGER_SECONDS = 1.0
#: The exporter checks the spool at least this often, even when no record was announced.
EXPORT_POLL_SECONDS = 30.0
EXPORT_BACKOFF_BASE_SECONDS = 1.0
EXPORT_BACKOFF_CAP_SECONDS = 300.0

_SEGMENT = re.compile(r"^requests-(\d{6,})\.jsonl$")
_CURSOR_FILE = "export-cursor.json"


def to_record(observation: Observation, *, record_id: str, gateway_id: str) -> GatewayRequestRecord:
    """An observation as the wire record. Raises ``ValidationError`` for an observation that does
    not make a valid record; the caller counts it and moves on."""
    request = observation.request
    hints = request.hints
    body = request.body
    response = observation.response
    identity = observation.identity
    position = observation.position
    return GatewayRequestRecord(
        id=record_id,
        gateway_id=gateway_id,
        config_version=observation.config_version,
        started_at=observation.started_at,
        endpoint=observation.endpoint,
        status=observation.status,
        outcome=observation.outcome,
        error_type=token_or_none(response.error_type),
        developer_id=identity.developer_id if identity else None,
        credential_kind=request.credential,
        credential_profile=identity.credential_profile if identity else "passthrough",
        client=request.client.client,
        client_version=request.client.version,
        session_id=hints.session_id,
        agent_id=hints.agent_id,
        parent_agent_id=hints.parent_agent_id,
        agent_type=hints.agent_type,
        prompt_id=hints.prompt_id,
        request_class=hints.request_class,
        request_class_raw=hints.request_class_raw,
        compaction=hints.compaction,
        context_compacted=hints.context_compacted,
        fingerprint=body.fingerprint if body else None,
        shares_main_prefix=position.shares_main_prefix if position else None,
        shape=body.shape if body else None,
        requested_model=token_or_none(body.model) if body else None,
        served_model=token_or_none(response.model),
        stop_reason=token_or_none(response.stop_reason),
        tool_use_count=response.tool_use_count,
        usage=response.usage,
        timing=observation.timing,
        seat_allowance=observation.seat_allowance,
        cost_usd=observation.cost_usd,
        baseline_usd=observation.baseline_usd,
        pricing_version=token_or_none(observation.pricing_version),
        decision=observation.decision,
        shadow=observation.shadow,
    )


@dataclass
class LogStats:
    """Counters for the gateway's status surface. Every way a record can go missing has one."""

    #: Records appended to the spool.
    recorded: int = 0
    #: Observations dropped because the writer's queue was full.
    dropped: int = 0
    #: Observations that did not make a valid record.
    unrecordable: int = 0
    #: Records lost to a failed write.
    unwritten: int = 0
    #: Records the control plane acknowledged (duplicates included).
    exported: int = 0
    #: Spool lines that were not a record (a write torn by a crash); skipped.
    unreadable: int = 0
    #: Segments removed before the exporter reached them.
    pruned_unexported: int = 0
    #: The exception type of the last failed export, until an export succeeds.
    last_export_error: str | None = None


@dataclass(frozen=True, order=True)
class Cursor:
    """A position in the spool: just past the last exported line. Segment 0 is "from the start"."""

    segment: int
    offset: int


class Spool:
    """The segment directory. One lock serialises appends, reads and pruning, which run in worker
    threads; each is short."""

    def __init__(
        self,
        directory: Path,
        *,
        stats: LogStats,
        max_segment_bytes: int = DEFAULT_SEGMENT_BYTES,
        max_segments: int = DEFAULT_MAX_SEGMENTS,
        exporting: bool = False,
    ) -> None:
        self.directory = directory
        self._stats = stats
        self._max_bytes = max_segment_bytes
        self._max_segments = max_segments
        self._exporting = exporting
        self._lock = threading.Lock()
        self._opened = False

    def _path(self, segment: int) -> Path:
        return self.directory / f"requests-{segment:06d}.jsonl"

    def segments(self) -> list[int]:
        if not self.directory.is_dir():
            return []
        found = (_SEGMENT.match(entry.name) for entry in self.directory.iterdir())
        return sorted(int(match[1]) for match in found if match)

    def _open(self) -> None:
        """Create the directory privately, and end a line a crash left half-written so the next
        record starts on a line of its own."""
        if self._opened:
            return
        self.directory.mkdir(parents=True, exist_ok=True)
        self.directory.chmod(0o700)
        segments = self.segments()
        if segments:
            path = self._path(segments[-1])
            with path.open("rb") as handle:
                size = handle.seek(0, os.SEEK_END)
                if size:
                    handle.seek(size - 1)
                    if handle.read(1) != b"\n":
                        self._write(path, b"\n")
        self._opened = True

    @staticmethod
    def _write(path: Path, data: bytes) -> None:
        fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
        with os.fdopen(fd, "ab") as handle:
            handle.write(data)

    def append(self, lines: list[bytes]) -> None:
        """Append complete lines (each ending in a newline) to the current segment."""
        with self._lock:
            self._open()
            segments = self.segments()
            current = segments[-1] if segments else 1
            path = self._path(current)
            if path.exists() and path.stat().st_size >= self._max_bytes:
                current += 1
                path = self._path(current)
            self._write(path, b"".join(lines))
            self._prune(current)

    def _prune(self, current: int) -> None:
        segments = self.segments()
        excess = segments[: max(len(segments) - self._max_segments, 0)]
        if not excess:
            return
        exported_through = self._cursor().segment
        for segment in excess:
            if segment == current:
                break
            self._path(segment).unlink(missing_ok=True)
            if self._exporting and segment >= exported_through:
                self._stats.pruned_unexported += 1
                log.warning("the request log is full: a segment was removed before export")

    def _cursor(self) -> Cursor:
        path = self.directory / _CURSOR_FILE
        try:
            raw = json.loads(path.read_text())
            return Cursor(int(raw["segment"]), int(raw["offset"]))
        except FileNotFoundError:
            return Cursor(0, 0)
        except (OSError, ValueError, KeyError, TypeError) as exc:
            log.error(
                "the export cursor is unreadable (%s); exporting from the start",
                type(exc).__name__,
            )
            return Cursor(0, 0)

    def cursor(self) -> Cursor:
        with self._lock:
            return self._cursor()

    def commit(self, cursor: Cursor) -> None:
        """Persist the export cursor (atomically, ``0600``)."""
        with self._lock:
            self.directory.mkdir(parents=True, exist_ok=True)
            path = self.directory / _CURSOR_FILE
            temporary = path.with_suffix(".tmp")
            fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "w") as handle:
                handle.write(json.dumps({"segment": cursor.segment, "offset": cursor.offset}))
            temporary.replace(path)

    def backlog(self) -> int:
        """Lines written past the export cursor: the records waiting to be exported (a line torn
        by a crash counts too, so it can overstate by one per crash)."""
        with self._lock:
            cursor = self._cursor()
            count = 0
            for segment in self.segments():
                if segment < cursor.segment:
                    continue
                with self._path(segment).open("rb") as handle:
                    handle.seek(cursor.offset if segment == cursor.segment else 0)
                    while chunk := handle.read(READ_BYTES):
                        count += chunk.count(b"\n")
            return count

    def read(self, limit: int) -> list[tuple[bytes, Cursor]]:
        """Up to ``limit`` complete lines from the export cursor, each with the cursor just past
        it. A segment that a later one follows is finished: a torn tail there is skipped."""
        with self._lock:
            cursor = self._cursor()
            segments = self.segments()
            lines: list[tuple[bytes, Cursor]] = []
            while len(lines) < limit:
                later = [s for s in segments if s > cursor.segment]
                if cursor.segment not in segments:
                    if not later:
                        break
                    cursor = Cursor(later[0], 0)
                    continue
                with self._path(cursor.segment).open("rb") as handle:
                    handle.seek(cursor.offset)
                    data = handle.read(READ_BYTES)
                end = data.rfind(b"\n")
                if end < 0 and len(data) == READ_BYTES:
                    # No line ends within a whole read: not a record. Skip past it; the rest of
                    # the line reads as one that does not parse.
                    cursor = Cursor(cursor.segment, cursor.offset + len(data))
                    continue
                offset = cursor.offset
                for line in data[: end + 1].split(b"\n")[:-1]:
                    offset += len(line) + 1
                    lines.append((line, Cursor(cursor.segment, offset)))
                    if len(lines) >= limit:
                        break
                cursor = Cursor(cursor.segment, offset)
                if len(lines) >= limit or len(data) == READ_BYTES:
                    continue
                if not later:
                    break
                cursor = Cursor(later[0], 0)
            return lines


class RequestLog:
    """The observe hook's end of the log: queue, write, and announce new records to the exporter."""

    def __init__(
        self,
        spool: Spool,
        *,
        gateway_id: str,
        stats: LogStats,
        max_queue: int = DEFAULT_MAX_QUEUE,
        new_id: Callable[[], str] = lambda: uuid.uuid4().hex,
    ) -> None:
        self.spool = spool
        self.stats = stats
        self._gateway_id = gateway_id
        self._max_queue = max_queue
        self._new_id = new_id
        self._queue: deque[Observation] = deque()
        self._wake = asyncio.Event()
        self._closing = False
        self._task: asyncio.Task[None] | None = None
        #: Set whenever records reach the spool; the exporter waits on it. Remade by ``start`` so
        #: it belongs to the loop the log runs on.
        self.appended = asyncio.Event()

    def record(self, observation: Observation) -> None:
        """The observe hook. Runs on the event loop and never waits: passthrough mode records
        nothing, and a full queue drops the observation."""
        if observation.mode == "passthrough":
            return
        if len(self._queue) >= self._max_queue:
            self.stats.dropped += 1
            if self.stats.dropped & (self.stats.dropped - 1) == 0:  # 1, 2, 4, 8, …: not a flood
                log.warning(
                    "the request log is behind; %d observations dropped", self.stats.dropped
                )
            return
        self._queue.append(observation)
        self._wake.set()

    def start(self) -> None:
        self._wake = asyncio.Event()
        self.appended = asyncio.Event()
        self._closing = False
        self._task = asyncio.create_task(self._run())

    async def stop(self) -> None:
        """Write what is queued, then stop. Gives up after ``STOP_SECONDS``."""
        if self._task is None:
            return
        self._closing = True
        self._wake.set()
        try:
            await asyncio.wait_for(asyncio.shield(self._task), STOP_SECONDS)
        except TimeoutError:
            log.error(
                "the request log did not drain in time; %d observations lost", len(self._queue)
            )
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task

    async def _run(self) -> None:
        while True:
            if not self._queue:
                if self._closing:
                    return
                await self._wake.wait()
                self._wake.clear()
                continue
            batch = [self._queue.popleft() for _ in range(min(len(self._queue), WRITE_BATCH))]
            try:
                written = await asyncio.to_thread(self._write, batch)
            except Exception as exc:
                self.stats.unwritten += len(batch)
                log.error("could not write the request log: %s", type(exc).__name__)
                continue
            if written:
                self.appended.set()

    def _write(self, batch: list[Observation]) -> int:
        """Worker thread: records out of observations, appended in one write."""
        lines: list[bytes] = []
        for observation in batch:
            try:
                record = to_record(
                    observation, record_id=self._new_id(), gateway_id=self._gateway_id
                )
            except Exception as exc:
                self.stats.unrecordable += 1
                log.error("an exchange did not make a valid record: %s", type(exc).__name__)
                continue
            lines.append(record.model_dump_json().encode() + b"\n")
        if lines:
            self.spool.append(lines)
            self.stats.recorded += len(lines)
        return len(lines)


class ExportRefused(Exception):
    """The control plane answered a batch with an error status."""

    def __init__(self, status: int) -> None:
        super().__init__(f"the control plane answered {status}")
        self.status = status

    @property
    def too_large(self) -> bool:
        """The batch is over a body-size limit (often a proxy's): smaller batches will pass."""
        return self.status == 413


class EventSink(Protocol):
    async def send(self, batch: GatewayEventBatch) -> GatewayEventAck: ...


class ControlPlaneEventSink:
    """Posts batches to the control plane's ``POST /gateway/events``, authenticated with the
    gateway's service key. A 2xx answer is a ``GatewayEventAck``; anything else raises
    ``ExportRefused``."""

    def __init__(self, client: httpx.AsyncClient, control_plane_url: str, service_key: str) -> None:
        self._client = client
        self._url = f"{control_plane_url}/gateway/events"
        self._service_key = service_key

    async def send(self, batch: GatewayEventBatch) -> GatewayEventAck:
        response = await self._client.post(
            self._url,
            content=batch.model_dump_json(),
            headers={
                "authorization": f"Bearer {self._service_key}",
                "content-type": "application/json",
            },
        )
        if not response.is_success:
            raise ExportRefused(response.status_code)
        return GatewayEventAck.model_validate_json(response.content)


class Exporter:
    """Moves records from the spool to the control plane, in order, at least once."""

    def __init__(
        self,
        spool: Spool,
        sink: EventSink,
        *,
        stats: LogStats,
        appended: asyncio.Event,
        max_batch: int = MAX_EVENT_BATCH,
        linger_seconds: float = EXPORT_LINGER_SECONDS,
        poll_seconds: float = EXPORT_POLL_SECONDS,
        backoff_base_seconds: float = EXPORT_BACKOFF_BASE_SECONDS,
        backoff_cap_seconds: float = EXPORT_BACKOFF_CAP_SECONDS,
    ) -> None:
        self._spool = spool
        self._sink = sink
        self._stats = stats
        self._appended = appended
        self._max_batch = max_batch
        self._linger = linger_seconds
        self._poll = poll_seconds
        self._backoff_base = backoff_base_seconds
        self._backoff_cap = backoff_cap_seconds

    def _parse(self, entries: list[tuple[bytes, Cursor]]) -> tuple[list[GatewayRequestRecord], int]:
        """The leading run of records from one gateway (a batch has one ``gateway_id``), and how
        many lines that run spans. Lines that are not records are skipped (and counted)."""
        records: list[GatewayRequestRecord] = []
        consumed = 0
        for line, _ in entries:
            if line.strip():
                try:
                    record = GatewayRequestRecord.model_validate_json(line)
                except ValidationError:
                    self._stats.unreadable += 1
                    log.error("skipping a request-log line that is not a record")
                else:
                    if records and record.gateway_id != records[0].gateway_id:
                        break
                    records.append(record)
            consumed += 1
        return records, consumed

    async def export_once(self, limit: int) -> int:
        """Send the next batch of at most ``limit`` records and move the cursor past it. Returns
        how many spool lines that consumed (0 when nothing is pending). A failure raises, and the
        cursor stays where it was."""
        entries = await asyncio.to_thread(self._spool.read, limit)
        records, consumed = self._parse(entries)
        if not consumed:
            return 0
        if records:
            batch = GatewayEventBatch(gateway_id=records[0].gateway_id, records=records)
            ack = await self._sink.send(batch)
            self._stats.exported += ack.accepted + ack.duplicates
        await asyncio.to_thread(self._spool.commit, entries[consumed - 1][1])
        return consumed

    def _backoff(self, failures: int) -> float:
        ceiling = min(self._backoff_cap, self._backoff_base * 2 ** min(failures - 1, 30))
        return ceiling * random.uniform(0.5, 1.0)

    async def _idle(self) -> None:
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(self._appended.wait(), self._poll)
        self._appended.clear()
        if self._linger:
            await asyncio.sleep(self._linger)

    async def run(self) -> None:
        """Export until cancelled. A batch over a size limit is halved (and batches stay that
        size); every other failure backs off and retries the same records."""
        failures = 0
        limit = self._max_batch
        while True:
            try:
                consumed = await self.export_once(limit)
            except ExportRefused as exc:
                if exc.too_large and limit > 1:
                    limit //= 2
                    continue
                failures += 1
                self._stats.last_export_error = f"http_{exc.status}"
            except Exception as exc:
                failures += 1
                self._stats.last_export_error = type(exc).__name__
            else:
                failures = 0
                self._stats.last_export_error = None
                if not consumed:
                    await self._idle()
                continue
            delay = self._backoff(failures)
            log.warning(
                "exporting the request log failed (%s); retrying in %.1fs",
                self._stats.last_export_error,
                delay,
            )
            await asyncio.sleep(delay)
