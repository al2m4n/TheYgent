"""Audio/blob artifact storage — the artifact reference seam.

Non-text payloads (audio in/out) are passed as **references**, never multi-MB blobs in step args
(durable step args must be small and serialisable). A reference is
``{"ref": <id|url|path>, "contentType": <mime>}``. ``transcribe`` FETCHES bytes from its input ref
(a stored artifact, an http url, or a local path) and streams them to the inference plane;
``speak`` PUTS the produced bytes as a new artifact and returns the ref (the bytes are an artifact,
not journaled — a resumed run replays the
ref, not the audio).

This is the **minimal honest** local-filesystem store (the user's trust domain), the same posture as
the inference plane's local model dir. A cloud-aware blob store (signed URLs, retention, per-tenant
buckets) is the deferred upgrade — only this module's internals change; the ref contract does not.
"""

from __future__ import annotations

import asyncio
import os
import tempfile

import httpx
from ulid import ULID

#: Env override for the artifact directory; defaults to a per-host temp dir (dev). A real deployment
#: points this at durable local storage in the user's domain.
ARTIFACT_DIR_ENV = "THEYGENT_ARTIFACT_DIR"


def default_artifact_dir() -> str:
    return os.environ.get(ARTIFACT_DIR_ENV) or os.path.join(
        tempfile.gettempdir(), "theygent-artifacts"
    )


def _write_blocking(path: str, data: bytes, content_type: str) -> None:
    with open(path, "wb") as f:
        f.write(data)
    # A tiny sidecar keeps the content type with the bytes, so fetching a stored id recovers the
    # real mime (an id alone is otherwise typeless). Best-effort — its absence just means octet.
    with open(f"{path}.type", "w") as f:
        f.write(content_type)


def _read_local_blocking(base_dir: str, target: str) -> tuple[bytes, str | None]:
    """Read a stored artifact id (relative to ``base_dir``) or an absolute/local path, plus the
    stored content type when a sidecar exists. Blocking — run off the loop via to_thread."""
    stored = os.path.join(base_dir, target)
    path = stored if os.path.exists(stored) else target
    if not os.path.exists(path):
        raise FileNotFoundError(f"audio artifact not found for ref {target!r}")
    with open(path, "rb") as f:
        data = f.read()
    sidecar = f"{path}.type"
    content_type = None
    if os.path.exists(sidecar):
        with open(sidecar) as f:
            content_type = f.read().strip() or None
    return data, content_type


class LocalArtifactStore:
    """Local-filesystem artifact storage. ``put`` writes bytes under a fresh
    ``art_<ulid>`` id and returns the reference; ``fetch`` resolves a reference (stored id, url, or
    local path) to ``(bytes, content_type)``. Injected into the walker/durable steps so handlers
    stay runtime-agnostic — they call this, the store owns the I/O (file ops off the event loop)."""

    def __init__(self, base_dir: str | None = None) -> None:
        self._dir = base_dir or default_artifact_dir()
        os.makedirs(self._dir, exist_ok=True)

    @property
    def base_dir(self) -> str:
        """Where artifacts land — read-only diagnostics (the settings boot block)."""
        return self._dir

    async def put(self, data: bytes, content_type: str) -> dict[str, object]:
        """Store ``data``, return its reference (``{ref, contentType, bytes}``). The bytes live on
        disk (an artifact); only the reference is journaled/returned."""
        ref_id = f"art_{ULID()}"
        await asyncio.to_thread(
            _write_blocking, os.path.join(self._dir, ref_id), data, content_type
        )
        return {"ref": ref_id, "contentType": content_type, "bytes": len(data)}

    async def put_with_ref(self, ref: str, data: bytes, content_type: str) -> dict[str, object]:
        """Store ``data`` under a CALLER-SUPPLIED ``art_`` id — the preserve-ref restore path a
        bundle import uses, so refs embedded in imported run outputs / node payloads keep
        resolving. An EXISTING ref is never overwritten (artifacts are immutable history — a
        bundle must not rewrite bytes another run produced; a same-bundle re-import carries
        identical bytes anyway): the stored artifact's metadata returns with ``created`` False.
        The caller validates the ref shape (the route owns the 400)."""

        def _put_blocking() -> dict[str, object]:
            path = os.path.join(self._dir, ref)
            if os.path.exists(path):
                size = os.path.getsize(path)
                sidecar = f"{path}.type"
                stored_type = "application/octet-stream"
                if os.path.exists(sidecar):
                    with open(sidecar) as f:
                        stored_type = f.read().strip() or stored_type
                return {"ref": ref, "contentType": stored_type, "bytes": size, "created": False}
            _write_blocking(path, data, content_type)
            return {"ref": ref, "contentType": content_type, "bytes": len(data), "created": True}

        return await asyncio.to_thread(_put_blocking)

    async def fetch(self, ref: object) -> tuple[bytes, str]:
        """Resolve an audio reference to ``(bytes, content_type)``. ``ref`` is a ``{ref,
        contentType}`` dict (or a bare string). A stored artifact id reads from the local dir; an
        ``http(s)`` url is fetched (in the user's trust domain); a local path is read. Raises on an
        unresolvable ref — the caller binds an honest ``err``."""
        if isinstance(ref, dict):
            target = ref.get("ref")
            content_type = str(ref.get("contentType") or "application/octet-stream")
        else:
            target = ref
            content_type = "application/octet-stream"
        if not isinstance(target, str) or not target:
            raise ValueError(f"audio reference has no resolvable 'ref': {ref!r}")
        if target.startswith(("http://", "https://")):
            async with httpx.AsyncClient(follow_redirects=True) as client:
                resp = await client.get(target, timeout=30.0)
            resp.raise_for_status()
            return resp.content, resp.headers.get("content-type", content_type)
        data, stored_type = await asyncio.to_thread(_read_local_blocking, self._dir, target)
        # A stored artifact's own recorded type wins over a caller-supplied default (a bare-id
        # fetch has none); a local path with no sidecar falls back to the default.
        return data, stored_type or content_type
