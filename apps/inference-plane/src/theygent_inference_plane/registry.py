"""Logical-model registry — the management-plane store.

Maps a logical id → its registered binding. M1 kept this purely in memory; **M9 §2.3** adds optional
local persistence so registrations survive a restart (finding F6.1) — but, critically, **local to
the inference plane**, never the control-plane's Postgres.

This is the plane boundary made concrete (``theygent-stack.md`` §10). The inference plane runs in
the **user's trust domain**; coupling it to the control-plane DB is a §4-class regression. So the
store here is a small JSON state file in the inference plane's own state dir — no SQLAlchemy, no
asyncpg, no shared database. The registry persists the *registration* (the binding), exactly as the
control-plane's MCP registry persists its registration — symmetric concern, deliberately different
store. When ``state_path`` is ``None`` the registry is pure in-memory (M1 behaviour — the fast suite
uses this so tests never touch the filesystem).
"""

from __future__ import annotations

import contextlib
import json
import os
import tempfile
from pathlib import Path

from theygent_ir import ManagedBinding, ReachableBinding, parse_registration

Binding = ManagedBinding | ReachableBinding


class UnknownLogicalId(KeyError):
    """Raised when a logical id is not registered. On /v1/* this is what makes
    an engine name (e.g. "llamacpp") an invalid model value."""

    def __init__(self, logical_id: str) -> None:
        super().__init__(logical_id)
        self.logical_id = logical_id


class Registry:
    def __init__(self, state_path: Path | None = None) -> None:
        self._models: dict[str, Binding] = {}
        self._state_path = state_path
        if state_path is not None:
            self._load()

    # ── local persistence (JSON state file, inference plane only) ────────────────

    def _load(self) -> None:
        """Rehydrate registrations from the local state file, if present. A missing file is the
        first-run case (empty registry); a corrupt file is ignored rather than crashing the plane
        on boot — a re-registration overwrites it (the registry is recoverable, not canonical)."""
        assert self._state_path is not None
        try:
            raw = json.loads(self._state_path.read_text())
        except (OSError, ValueError):
            return
        for logical_id, payload in raw.get("models", {}).items():
            try:
                self._models[logical_id] = parse_registration(payload)
            except Exception:  # a binding shape that no longer validates is skipped, not fatal
                continue

    def _persist(self) -> None:
        """Atomically write the whole registry to the state file (write-temp-then-rename), so a
        crash mid-write never leaves a half-file. camelCase payloads (``by_alias``) round-trip
        through ``parse_registration`` on load — the same wire shape /admin/models speaks."""
        if self._state_path is None:
            return
        self._state_path.parent.mkdir(parents=True, exist_ok=True)
        data = {
            "models": {lid: b.model_dump(by_alias=True) for lid, b in self._models.items()},
        }
        fd, tmp = tempfile.mkstemp(dir=self._state_path.parent, suffix=".tmp")
        try:
            with os.fdopen(fd, "w") as fh:
                json.dump(data, fh)
            os.replace(tmp, self._state_path)
        except BaseException:
            with contextlib.suppress(OSError):
                os.unlink(tmp)
            raise

    # ── registry operations ─────────────────────────────────────────────────────

    def put(self, logical_id: str, binding: Binding) -> None:
        self._models[logical_id] = binding
        self._persist()

    def get(self, logical_id: str) -> Binding | None:
        return self._models.get(logical_id)

    def require(self, logical_id: str) -> Binding:
        try:
            return self._models[logical_id]
        except KeyError:
            raise UnknownLogicalId(logical_id) from None

    def delete(self, logical_id: str) -> bool:
        existed = self._models.pop(logical_id, None) is not None
        if existed:
            self._persist()
        return existed

    def ids(self) -> list[str]:
        return list(self._models)

    def items(self) -> list[tuple[str, Binding]]:
        return list(self._models.items())
