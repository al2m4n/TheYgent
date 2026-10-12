"""Recorded Claude Code exchanges, rebuilt without content (see ``spike/make_fixtures.py``).

Each fixture is one request Claude Code 2.1.285 sent and the response it got, with every setting
and count the gateway reads kept exactly, and every piece of content replaced by a placeholder.
Fixtures sharing a ``conversation`` came from one Claude Code session, in ``sequence`` order.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from functools import cache
from pathlib import Path
from typing import Any

FIXTURE_DIR = Path(__file__).parent / "fixtures" / "exchanges"


@dataclass(frozen=True)
class Exchange:
    name: str
    description: str
    note: str | None
    source: str
    conversation: str
    sequence: int
    request_class: str
    method: str
    path: str
    query: str
    request_headers: tuple[tuple[str, str], ...]
    body: dict[str, Any]
    status: int
    response_headers: tuple[tuple[str, str], ...]
    events: tuple[str, ...] | None
    json_body: Any
    #: Set on a derived fixture whose upstream drops the connection after this many events.
    abort_after_events: int | None

    @property
    def streaming(self) -> bool:
        return self.events is not None

    @property
    def target(self) -> str:
        return self.path + (f"?{self.query}" if self.query else "")

    def request_bytes(self) -> bytes:
        return json.dumps(self.body, ensure_ascii=False, separators=(",", ":")).encode()

    def response_bytes(self) -> bytes:
        if self.events is not None:
            return "".join(self.events).encode()
        return json.dumps(self.json_body, ensure_ascii=False, separators=(",", ":")).encode()

    def request_header(self, name: str) -> str | None:
        return next((v for k, v in self.request_headers if k == name), None)

    def response_header(self, name: str) -> str | None:
        return next((v for k, v in self.response_headers if k == name), None)

    def sse(self) -> list[tuple[str, dict[str, Any]]]:
        """The response events as (event name, data) pairs."""
        parsed = []
        for raw in self.events or ():
            head, data = raw.split("\ndata: ", 1)
            parsed.append((head.removeprefix("event: "), json.loads(data)))
        return parsed


def _parse(raw: dict[str, Any]) -> Exchange:
    request, response = raw["request"], raw["response"]
    return Exchange(
        name=raw["name"],
        description=raw["description"],
        note=raw.get("note"),
        source=raw["source"],
        conversation=raw["conversation"],
        sequence=raw["sequence"],
        request_class=raw["request_class"],
        method=request["method"],
        path=request["path"],
        query=request["query"],
        request_headers=tuple((k, v) for k, v in request["headers"]),
        body=request["body"],
        status=response["status"],
        response_headers=tuple((k, v) for k, v in response["headers"]),
        events=tuple(response["events"]) if "events" in response else None,
        json_body=response.get("json"),
        abort_after_events=raw.get("abort_after_events"),
    )


@cache
def load(name: str) -> Exchange:
    return _parse(json.loads((FIXTURE_DIR / f"{name}.json").read_text()))


def all_exchanges() -> list[Exchange]:
    exchanges = [load(path.stem) for path in FIXTURE_DIR.glob("*.json")]
    return sorted(exchanges, key=lambda e: (e.conversation, e.sequence))
