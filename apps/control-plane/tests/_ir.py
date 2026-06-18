"""The trivial 3-node IR document (input -> llm -> output) the graph fast suite drives.

This is the m5.md §4 envelope, inline. Helpers return a fresh deep copy each call so a test
can mutate one (a bad enum, a cycle, an engine-name binding) without disturbing another.
"""

from __future__ import annotations

import copy
from typing import Any

_TRIVIAL: dict[str, Any] = {
    "schemaVersion": "1.0",
    "id": "agt_01J9X8TRIVIAL",
    "name": "trivial-llm",
    "version": "0.1.0",
    "models": {
        "default": {
            "binding": "mlx",
            "model": "triage-fast",
            "params": {"temperature": 0.2, "maxTokens": 256},
        }
    },
    "tools": {},
    "nodes": [
        {
            "id": "n_in",
            "type": "input",
            "kind": "boundary",
            "ports": {"in": [], "out": [{"id": "out", "type": "any"}]},
        },
        {
            "id": "n_llm",
            "type": "llm",
            "kind": "activity",
            "config": {"model": "default", "messages": [{"role": "user", "content": "$input"}]},
            "ports": {
                "in": [{"id": "in", "type": "any"}],
                "out": [{"id": "ok", "type": "any"}, {"id": "err", "type": "error"}],
            },
        },
        {
            "id": "n_out",
            "type": "output",
            "kind": "boundary",
            "ports": {"in": [{"id": "in", "type": "any"}], "out": []},
        },
    ],
    "edges": [
        {
            "id": "e1",
            "source": "n_in",
            "sourceHandle": "out",
            "target": "n_llm",
            "targetHandle": "in",
            "channel": "data",
        },
        {
            "id": "e2",
            "source": "n_llm",
            "sourceHandle": "ok",
            "target": "n_out",
            "targetHandle": "in",
            "channel": "data",
        },
    ],
}


def trivial_ir() -> dict[str, Any]:
    return copy.deepcopy(_TRIVIAL)
