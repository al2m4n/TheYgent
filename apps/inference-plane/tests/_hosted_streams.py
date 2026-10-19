"""Chat streams recorded from real hosted OpenAI-compatible servers (``fixtures/hosted-streams``).

Each fixture holds one stream's SSE events, ids and timestamps anonymized and everything else as
the server sent it, plus the request that produced it. The dropping fake replays them
(``REPLAY`` / ``REPLAY_CUT``), so the fast suite checks the gateway's cut-off check against what
hosted servers actually return, not against an idealized stream.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

FIXTURE_DIR = Path(__file__).parent / "fixtures" / "hosted-streams"


def _load() -> dict[str, dict[str, Any]]:
    fixtures = (json.loads(path.read_text()) for path in sorted(FIXTURE_DIR.glob("*.json")))
    return {fixture["name"]: fixture for fixture in fixtures}


_FIXTURES = _load()

#: Fixture name -> the recorded SSE events, in order.
HOSTED_STREAMS: dict[str, list[bytes]] = {
    name: [event.encode() for event in fixture["events"]] for name, fixture in _FIXTURES.items()
}
#: Fixture name -> the request that produced it (model, messages and generation params).
HOSTED_REQUESTS: dict[str, dict[str, Any]] = {
    name: fixture["request"] for name, fixture in _FIXTURES.items()
}
