"""Local platform settings — ``settings.json`` beside ``registry.json``.

The settings file is the *writable* half of the plane's configuration: knobs the user can
change at runtime over ``/admin/settings`` (v1: the resident-engine ceiling). It persists
in the inference plane's OWN state dir — the user's trust domain, never the control-plane's
Postgres — exactly like the model registry and the credential store. ``state_path=None``
keeps it purely in-memory (the fast suite never touches disk).

Resolution precedence for every knob: explicit env (set AND non-empty — an empty env var
means *unset*, never a value) > the stored ``settings.json`` value > the coded default.
The winning source is reported honestly on the wire (``"env" | "stored" | "default"``): an
env-pinned knob renders locked in the UI instead of silently ignoring writes.
"""

from __future__ import annotations

import contextlib
import json
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

SettingSource = Literal["env", "stored", "default"]

MAX_RESIDENT_DEFAULT = 2
MAX_RESIDENT_ENV_VAR = "THEYGENT_MAX_RESIDENT"
#: Sanity bounds for the resident-engine ceiling. Zero would refuse every managed model;
#: anything past the upper bound is a typo on hardware this plane targets.
MAX_RESIDENT_MIN = 1
MAX_RESIDENT_MAX = 16

#: The wire/file key (camelCase — the management-plane convention; the file speaks the
#: same dialect as ``/admin/settings`` so a user reading it recognizes the knob).
_MAX_RESIDENT_KEY = "maxResident"


class SettingsStore:
    """The local settings file. Only user-set values are stored — an absent key means
    "use the default", which is what lets the resolved *source* stay honest."""

    def __init__(self, state_path: Path | None = None) -> None:
        self._values: dict[str, Any] = {}
        self._state_path = state_path
        if state_path is not None:
            self._load()

    # ── local persistence (JSON state file, inference plane only) ────────────────

    def _load(self) -> None:
        """Rehydrate from the local state file, if present. A missing file is first-run
        (no stored settings); a corrupt file is ignored rather than crashing the plane on
        boot — the next write overwrites it (the store is recoverable, not canonical)."""
        assert self._state_path is not None
        try:
            raw = json.loads(self._state_path.read_text())
        except (OSError, ValueError):
            return
        if isinstance(raw, dict):
            self._values = raw

    def _persist(self) -> None:
        """Atomically write the whole store (write-temp-then-rename), so a crash mid-write
        never leaves a half-file — the same pattern as the registry beside it."""
        if self._state_path is None:
            return
        self._state_path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=self._state_path.parent, suffix=".tmp")
        try:
            with os.fdopen(fd, "w") as fh:
                json.dump(self._values, fh)
            os.replace(tmp, self._state_path)
        except BaseException:
            with contextlib.suppress(OSError):
                os.unlink(tmp)
            raise

    # ── the resident-engine ceiling ───────────────────────────────────────────────

    @property
    def max_resident(self) -> int | None:
        """The stored ceiling, or ``None`` when not user-set. A stored value that no
        longer validates (wrong type, out of bounds — e.g. hand-edited) resolves as
        unset rather than half-applying: the default wins and a write repairs the file."""
        raw = self._values.get(_MAX_RESIDENT_KEY)
        if isinstance(raw, bool) or not isinstance(raw, int):
            return None
        if not (MAX_RESIDENT_MIN <= raw <= MAX_RESIDENT_MAX):
            return None
        return raw

    def set_max_resident(self, value: int | None) -> None:
        """Store the ceiling; ``None`` deletes the stored value (reset to default)."""
        if value is None:
            self._values.pop(_MAX_RESIDENT_KEY, None)
        else:
            self._values[_MAX_RESIDENT_KEY] = value
        self._persist()


@dataclass(frozen=True)
class MaxResident:
    """The resolved ceiling plus where it came from, so the wire can report both."""

    value: int
    source: SettingSource

    @property
    def pinned(self) -> bool:
        """Env-pinned: the knob is locked (writes are refused with a clear error)."""
        return self.source == "env"


def resolve_max_resident(explicit: int | None, store: SettingsStore) -> MaxResident:
    """Apply the precedence: explicit (the env-injection seam) > stored > default.

    ``explicit`` is the ``create_app`` argument — the entrypoint passes the env var
    through only when it is set and non-empty, so a non-``None`` value here IS the
    env-pinned state (tests that pass a literal ceiling get the same pinning, which is
    correct: an injected ceiling is not user-editable either).
    """
    if explicit is not None:
        return MaxResident(explicit, "env")
    stored = store.max_resident
    if stored is not None:
        return MaxResident(stored, "stored")
    return MaxResident(MAX_RESIDENT_DEFAULT, "default")
