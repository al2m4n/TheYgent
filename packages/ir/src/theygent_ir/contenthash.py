"""``content_hash`` — content-addressed graph identity.

The hash is computed over the **canonical, view-stripped, key-sorted JSON** of the document.
Three exclusions, each load-bearing:

* ``view`` — React-Flow layout (positions, zoom, collapsed state). Dragging a node must never
  produce a "new version" (layout must not affect graph identity), so layout is never hashed.
* ``contentHash`` itself — it is *derived from* the document; hashing it would be circular.
* whitespace and key order — normalized by ``sort_keys=True`` + compact separators, so the
  same logical graph always yields the same hash regardless of how the JSON was formatted.

This hash is computed and recorded on each ``Run`` but does not gate execution — that is a
registry concern. Recording it at run time means the field is already correct when the
registry consumes it. The prefix is ``sha256:`` to keep the algorithm explicit on the wire.

**Canonicalize the hydrated, default-filled model, NOT the source bytes.** The hash runs over
``ir.model_dump(...)`` of the *validated* Pydantic model, so every field with a schema default
is present at its effective value — e.g. ``Port.required: bool = True`` appears whether or not
the author wrote it. Consequence: an IR that omits a defaulted field hashes **identically** to
one that writes the default explicitly, so two semantically identical agents never mint two
registry versions (the load-bearing guarantee that keeps the registry stable). This is one
function — the walker and the registry both call it, so they can never disagree. The default-fill
set is the model's own ``schemaVersion``; adding a new defaulted field is a schema change that
never silently re-hashes content stored under an older version.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

from theygent_ir.graph import IRDocument

#: Top-level keys excluded from the hash. ``view`` is layout; ``contentHash`` is derived.
_EXCLUDED = ("view", "contentHash", "content_hash")


def _canonical(doc: dict[str, Any]) -> str:
    stripped = {k: v for k, v in doc.items() if k not in _EXCLUDED}
    # sort_keys normalizes key order; the compact separators drop insignificant whitespace.
    return json.dumps(stripped, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def content_hash(ir: IRDocument) -> str:
    """``sha256:<hex>`` over the canonical, view-stripped, key-sorted JSON of ``ir``.

    Computed from the *validated* document dumped by wire alias (camelCase), so the hash is
    over the document's canonical wire form — independent of how the input JSON was ordered or
    spaced. A ``view`` block or a reordered/ re-spaced equivalent yields the same hash; any real
    content change yields a different one.
    """

    doc = ir.model_dump(mode="json", by_alias=True, exclude_none=False)
    digest = hashlib.sha256(_canonical(doc).encode("utf-8")).hexdigest()
    return f"sha256:{digest}"
