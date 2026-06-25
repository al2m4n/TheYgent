"""Generate the frontend's IR contract artifacts from ``packages/ir`` — the single source of
truth (M15 §1.3, theygent-graph-schema.md §8).

The frontend (``apps/interface``, ``apps/web``) must NEVER hand-write IR types: a second,
drifting definition of the IR is exactly the corruption M15 §1 guards against. This script is
the one-way generator: it imports the Pydantic models and emits

  * ``src/ir.schema.json``    — ``IRDocument.model_json_schema()`` (the §8.2 envelope schema).
                                ``json-schema-to-typescript`` turns this into ``src/ir.d.ts``.
  * ``src/node-types.json``   — the **node-type registry** (§2.2): for every executable node
                                ``type``, its determinism ``kind`` (from ``NODE_TYPE_KIND`` —
                                so React-Flow nodes never carry ``kind``, they look it up), its
                                per-type ``config`` JSON Schema (from ``_CONFIG_MODELS``), a
                                default ``config`` filled from that schema, and the default
                                ``ports`` a freshly-dropped node declares. The palette derives
                                its list from this file, so an M14-style type added in Python
                                appears on the canvas for free — never hardcoded in the FE.

Run via ``pnpm --filter @theygent/ir-types generate`` (which shells ``uv run`` then ``json2ts``).
The CI drift guard re-runs this and ``git diff --exit-code`` — if Python's IR moved and the
checked-in artifacts didn't, the build fails (M15 §1.3 / §4 type-drift guard).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from theygent_ir.graph import (
    _CONFIG_MODELS,
    EXECUTABLE_TYPES,
    NODE_TYPE_KIND,
    IRDocument,
)

# ── default ports per node type (§8.3) ────────────────────────────────────────
# Ports are declared per-node-instance in the IR, not in the per-type config model, so the
# *default* set a freshly-dropped node carries lives here — next to NODE_TYPE_KIND, the backend
# side of the seam. A type without an explicit entry falls back to one ``in`` + one ``out`` port,
# so a new executable type still drops onto the canvas as a connectable node (M15 §2.2: "new
# types appear automatically"). ``err`` is the second out-port the tool ok/err contract needs
# (m6.md §4); ``input`` has no in-port and ``output`` no out-port (graph boundaries).
_DEFAULT_PORTS: dict[str, dict[str, list[str]]] = {
    "input": {"in": [], "out": ["out"]},
    "output": {"in": ["in"], "out": []},
    "llm": {"in": ["in"], "out": ["out"]},
    "tool": {"in": ["in"], "out": ["out", "err"]},
    "mcp_tool": {"in": ["in"], "out": ["out", "err"]},
    "router": {"in": ["in"], "out": ["out"]},
    "human": {"in": ["in"], "out": ["out"]},
    "subgraph": {"in": ["in"], "out": ["out"]},
    "loop": {"in": ["in"], "out": ["out"]},
    "map": {"in": ["in"], "out": ["out"]},
    # M19 §2 — the node palette. transcribe/speak carry an ``err`` out-port (the tool ok/err
    # contract — §2.2); guardrail emits ``pass``/``block`` (§2.6); the gates emit ``allow``/``deny``
    # (§2.8); transform is a plain reshape (§2.9).
    "transcribe": {"in": ["audio"], "out": ["text", "err"]},
    "speak": {"in": ["text"], "out": ["audio", "err"]},
    "guardrail": {"in": ["in"], "out": ["pass", "block"]},
    "ratelimit": {"in": ["in"], "out": ["allow", "deny"]},
    "quota": {"in": ["in"], "out": ["allow", "deny"]},
    "transform": {"in": ["in"], "out": ["out"]},
}
_FALLBACK_PORTS: dict[str, list[str]] = {"in": ["in"], "out": ["out"]}


def _default_for(prop_schema: dict[str, Any]) -> Any:
    """A type-appropriate empty value for a config property with no declared default.

    A freshly-dropped node should produce a *shaped* (if not yet valid) config the inspector can
    edit — ``{"model": "", "messages": []}`` rather than ``{}`` — so the form has fields to show.
    The graph won't validate until the user fills required values; that loud failure is the point
    (M15 §2.2 / M9 no-silent-pass). ``default`` always wins when the schema declares one."""

    if "default" in prop_schema:
        return prop_schema["default"]
    # Resolve a couple of common JSON-Schema shapes; unknowns get ``null``.
    t = prop_schema.get("type")
    if isinstance(t, list):  # e.g. ["string", "null"] — an Optional field
        t = next((x for x in t if x != "null"), None)
    if t == "string":
        return ""
    if t == "integer" or t == "number":
        return 0
    if t == "boolean":
        return False
    if t == "array":
        return []
    if t == "object":
        return {}
    if "anyOf" in prop_schema or "$ref" in prop_schema:
        return None
    return None


def _default_config(config_schema: dict[str, Any]) -> dict[str, Any]:
    props: dict[str, Any] = config_schema.get("properties", {})
    return {name: _default_for(sub) for name, sub in props.items()}


def build_node_types() -> dict[str, Any]:
    types: dict[str, Any] = {}
    # Derive the palette from the types the walker actually executes (EXECUTABLE_TYPES), sorted
    # for a stable diff. Every entry's ``kind`` comes from NODE_TYPE_KIND — never re-stated in TS.
    for node_type in sorted(EXECUTABLE_TYPES):
        kind = NODE_TYPE_KIND[node_type]
        config_model = _CONFIG_MODELS.get(node_type)
        config_schema = config_model.model_json_schema() if config_model is not None else {}
        ports = _DEFAULT_PORTS.get(node_type, _FALLBACK_PORTS)
        types[node_type] = {
            "type": node_type,
            "kind": kind,
            "configSchema": config_schema,
            "defaultConfig": _default_config(config_schema),
            "ports": {
                "in": [{"id": pid, "type": "any", "required": True} for pid in ports["in"]],
                # An out-port named ``err`` is error-typed (the tool/llm/transcribe/speak ok-err
                # contract — m6.md §4); the walker keys ``_error_handles`` off ``type == "error"``,
                # so the palette default must match how real IRs declare it (tests/_ir.py).
                "out": [
                    {"id": pid, "type": "error" if pid == "err" else "any"} for pid in ports["out"]
                ],
            },
        }
    return {"types": types}


def main() -> None:
    out = Path(__file__).resolve().parent.parent / "src"
    out.mkdir(parents=True, exist_ok=True)

    schema = IRDocument.model_json_schema()
    (out / "ir.schema.json").write_text(json.dumps(schema, indent=2, sort_keys=True) + "\n")

    node_types = build_node_types()
    (out / "node-types.json").write_text(json.dumps(node_types, indent=2, sort_keys=True) + "\n")

    print(f"wrote {out / 'ir.schema.json'} and {out / 'node-types.json'}")


if __name__ == "__main__":
    main()
