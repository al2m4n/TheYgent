"""``content_hash`` — content-addressed graph identity (theygent-graph-schema.md §8.2/§8.0).

The hash is computed over the **canonical, view-stripped, key-sorted JSON** of the document.
Three deliberate exclusions, each load-bearing:

* ``view`` — React-Flow layout (positions, zoom, collapsed state). Dragging a node must never
  produce a "new version" (§8.0 rule 2), so layout is never hashed.
* ``contentHash`` itself — it is *derived from* the document; hashing it would be circular.
* whitespace and key order — normalized by ``sort_keys=True`` + compact separators, so the
  same logical graph always yields the same hash regardless of how the JSON was formatted.

M5 *computes and records* this on the ``Run`` but does not yet gate execution on it — that is
a registry concern (M5 §3.3). Recording it now means the field is already correct when the
registry consumes it. The prefix is ``sha256:`` to keep the algorithm explicit on the wire.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

from theygent_ir.graph import IRDocument

#: Top-level keys excluded from the hash (§8.2). ``view`` is layout; ``contentHash`` is derived.
_EXCLUDED = ("view", "contentHash", "content_hash")


def _canonical(doc: dict[str, Any]) -> str:
    stripped = {k: v for k, v in doc.items() if k not in _EXCLUDED}
    # sort_keys normalizes key order; the compact separators drop insignificant whitespace.
    return json.dumps(stripped, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def content_hash(ir: IRDocument) -> str:
    """``sha256:<hex>`` over the canonical, view-stripped, key-sorted JSON of ``ir`` (§8.2).

    Computed from the *validated* document dumped by wire alias (camelCase), so the hash is
    over the document's canonical wire form — independent of how the input JSON was ordered or
    spaced. A ``view`` block or a reordered/ re-spaced equivalent yields the same hash; any real
    content change yields a different one.
    """

    doc = ir.model_dump(mode="json", by_alias=True, exclude_none=False)
    digest = hashlib.sha256(_canonical(doc).encode("utf-8")).hexdigest()
    return f"sha256:{digest}"
