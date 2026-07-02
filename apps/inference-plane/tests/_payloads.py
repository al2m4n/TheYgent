"""Registration payload builders, camelCase over the wire."""

from __future__ import annotations

from typing import Any


def managed_payload(
    *,
    binding: str = "llamacpp",
    model: str = "fake-model",
    source: str = "local-path",
    priority: int = 0,
    idle_timeout_sec: int = 900,
    keep_warm: bool = False,
) -> dict[str, Any]:
    return {
        "binding": binding,
        "source": source,
        "model": model,
        "params": {"temperature": 0.2},
        "lifecycle": {
            "keepWarm": keep_warm,
            "idleTimeoutSec": idle_timeout_sec,
            "priority": priority,
        },
    }


def reachable_payload(
    *, base_url: str, model: str = "gpt-fake", credential_ref: str | None = None
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "binding": "openai-compatible",
        "baseUrl": base_url,
        "model": model,
        "params": {"temperature": 0.1},
    }
    if credential_ref is not None:
        payload["credentialRef"] = credential_ref
    return payload
