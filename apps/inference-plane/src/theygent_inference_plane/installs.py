"""Install-provenance sidecar — ``installs.json``, a peer of ``registry.json``.

A catalog install registers ``source=local-path`` with an absolute weights path, but the HF repo
id and variant filename exist only on the in-memory download job, which evaporates on restart.
This sidecar durably records that provenance (``{"<logicalId>": {"repo", "variantId"}}``) at the
one moment repo + variant + logical id coexist — download completion — so ``/admin/export`` can
carry faithful re-download metadata and ``/admin/import`` can re-fetch the same weights on another
machine. Local to the inference plane like every state file here (never the control plane's
Postgres); atomic whole-file writes; a missing or corrupt file is an empty index (recoverable, not
canonical — a registration without an entry simply exports ``install: null``).
"""

from __future__ import annotations

import contextlib
import json
import os
import tempfile
from pathlib import Path


class InstallStore:
    """Logical id → ``{"repo", "variantId"}`` re-download provenance, persisted next to
    ``registry.json``. ``state_path=None`` keeps it purely in-memory (the fast suite)."""

    def __init__(self, state_path: Path | None = None) -> None:
        self._records: dict[str, dict[str, str]] = {}
        self._state_path = state_path
        if state_path is not None:
            self._load()

    # ── local persistence (JSON state file, inference plane only) ────────────────

    def _load(self) -> None:
        """Rehydrate from the local state file, if present. Missing = first run; a corrupt file
        or entry is skipped rather than fatal (the index is recoverable, not canonical)."""
        assert self._state_path is not None
        try:
            raw = json.loads(self._state_path.read_text())
        except (OSError, ValueError):
            return
        if not isinstance(raw, dict):
            return
        for logical_id, rec in raw.items():
            if (
                isinstance(rec, dict)
                and isinstance(rec.get("repo"), str)
                and isinstance(rec.get("variantId"), str)
            ):
                self._records[str(logical_id)] = {
                    "repo": rec["repo"],
                    "variantId": rec["variantId"],
                }

    def _persist(self) -> None:
        """Atomic whole-file rewrite (write-temp-then-rename), same discipline as the registry —
        a crash mid-write never leaves a half-file. The on-disk shape IS the wire shape
        (camelCase ``variantId``), so entries ride into the export bundle verbatim."""
        if self._state_path is None:
            return
        self._state_path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=self._state_path.parent, suffix=".tmp")
        try:
            with os.fdopen(fd, "w") as fh:
                json.dump(self._records, fh)
            os.replace(tmp, self._state_path)
        except BaseException:
            with contextlib.suppress(OSError):
                os.unlink(tmp)
            raise

    # ── index operations ─────────────────────────────────────────────────────────

    def put(self, logical_id: str, *, repo: str, variant_id: str) -> None:
        self._records[logical_id] = {"repo": repo, "variantId": variant_id}
        self._persist()

    def get(self, logical_id: str) -> dict[str, str] | None:
        rec = self._records.get(logical_id)
        return dict(rec) if rec is not None else None

    def delete(self, logical_id: str) -> bool:
        existed = self._records.pop(logical_id, None) is not None
        if existed:
            self._persist()
        return existed
