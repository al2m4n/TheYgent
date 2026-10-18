"""How to author a TheYgent agent: the guide text, the node-type registry, and local validation."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from pydantic import ValidationError
from theygent_ir import (
    EXECUTABLE_TYPES,
    NODE_TYPE_KIND,
    GraphValidationError,
    parse_document,
    validate_graph,
)

#: The generated node registry the visual editor's palette is built from (kind, default ports,
#: default config and config schema per type). A source checkout carries it next to this app.
_REGISTRY_PATH = (
    Path(__file__).resolve().parents[4] / "packages" / "ir-types" / "src" / "node-types.json"
)

GUIDE = """\
# Authoring a TheYgent agent

An agent is one JSON document (the IR). TheYgent runs it on its own runtime, against models
registered on the inference plane. Workflow: `list_models` → write the IR → `validate_agent` →
`run_graph` (test it, nothing saved) → `create_agent` (publish) → `run_agent`. To change a
published agent, bump `version` and call `publish_agent_version`.

## Document

```json
{
  "schemaVersion": "1.0",
  "id": "agt_release_notes",          // the agent's stable id; agt_ + snake_case
  "name": "release-notes",
  "version": "0.1.0",                  // semver; bump it for every published change
  "models": {                          // model slots that llm nodes name
    "writer": {"binding": "mlx", "model": "<logical id from list_models>",
               "params": {"temperature": 0.2, "maxTokens": 1024}}
  },
  "tools": {},
  "nodes": [ ... ],
  "edges": [ ... ]
}
```

- `models.<slot>.binding` is the engine the logical id runs on (`list_models` reports it:
  `mlx`, `llamacpp`, `vllm` or `openai-compatible`); `model` is the logical id, never an engine
  name.
- A node is `{"id", "type", "kind", "config", "ports": {"in": [...], "out": [...]}}`. `kind` must
  match the type (`agent_authoring_guide` lists each type's kind and default ports — copy them).
  A port is `{"id", "type": "any"}`; tool-capability ports also carry `"role": "tool"`.
- An edge is `{"id", "source", "sourceHandle", "target", "targetHandle", "channel"}`. Handles
  are port ids. `channel` is `"data"` for flow, `"tool"` for a tool wired to an llm's `tools` port.
- Every agent starts at one `input` node and ends at one `output` node.

## Passing values: `$in`

String fields in a node's config are templates. `$in` is the whole value on the node's default
in-port (`in`); `$in.<port>` is another named in-port; `$in.in.<field>` is a field of the default
port's value (`$in.in.question` for input `{"question": "…"}`). An unknown reference fails the
run loudly — there is no silent pass-through.

## Patterns

1. **Prompt → model → answer**: `input` → `llm` → `output`, with the llm's messages
   `[{"role": "system", "content": "…"}, {"role": "user", "content": "$in"}]`.
2. **A model that calls tools**: wire `tool`, `rag` or `mcp_tool` nodes into the llm's `tools`
   in-port from their `use` out-port with `"channel": "tool"`; the model decides when to call them
   (`maxToolIterations` bounds the loop). The tool node's `description` tells the model when to use
   it. Built-in tools: `http_fetch` (GET a URL), `echo`.
3. **Retrieval**: a `rag` node with `{"source": "<rag source id>", "topK": 4, "description": "…"}`
   — as a tool (pattern 2) or as a step on the data channel.
4. **Branching**: a `router` node sends its input down one named out-port; a `guardrail` passes or
   blocks; `transform` reshapes data without I/O.

`human`, `subgraph`, `loop` and `map` nodes need the durable runtime, which `run_agent` does not
use; build with the other types unless the user runs agents durably.

## Example: answer from a retrieval source with a local model

```json
{
  "schemaVersion": "1.0", "id": "agt_docs_answerer", "name": "docs-answerer", "version": "0.1.0",
  "models": {"assistant": {"binding": "mlx", "model": "<logical id>",
                           "params": {"temperature": 0.0, "maxTokens": 700}}},
  "tools": {},
  "nodes": [
    {"id": "n_in", "type": "input", "kind": "boundary",
     "ports": {"in": [], "out": [{"id": "out", "type": "any"}]}},
    {"id": "n_llm", "type": "llm", "kind": "activity",
     "config": {"model": "assistant", "maxToolIterations": 4, "messages": [
       {"role": "system",
        "content": "Answer ONLY from the docs; search them with n_docs first."},
       {"role": "user", "content": "$in"}]},
     "ports": {"in": [{"id": "in", "type": "any"},
                      {"id": "tools", "type": "any", "required": false, "role": "tool"}],
               "out": [{"id": "out", "type": "any"}]}},
    {"id": "n_docs", "type": "rag", "kind": "activity",
     "config": {"source": "<rag source id>", "topK": 4,
                "description": "Search the project docs. Pass a short search query."},
     "ports": {"in": [{"id": "in", "type": "any", "required": false}],
               "out": [{"id": "use", "type": "any", "role": "tool"}]}},
    {"id": "n_out", "type": "output", "kind": "boundary",
     "ports": {"in": [{"id": "in", "type": "any"}], "out": []}}
  ],
  "edges": [
    {"id": "e_q", "source": "n_in", "sourceHandle": "out",
     "target": "n_llm", "targetHandle": "in", "channel": "data"},
    {"id": "e_cap", "source": "n_docs", "sourceHandle": "use",
     "target": "n_llm", "targetHandle": "tools", "channel": "tool"},
    {"id": "e_out", "source": "n_llm", "sourceHandle": "out",
     "target": "n_out", "targetHandle": "in", "channel": "data"}
  ]
}
```
"""


def _registry() -> dict[str, Any]:
    try:
        return json.loads(_REGISTRY_PATH.read_text())["types"]
    except (OSError, ValueError, KeyError):
        return {}


def node_types(
    types: list[str] | None = None, include_schema: bool = False
) -> list[dict[str, Any]]:
    """Every runnable node type (or the named ones): its kind, default ports and default config,
    plus its config JSON schema when ``include_schema``."""
    registry = _registry()
    wanted = sorted(types) if types else sorted(EXECUTABLE_TYPES)
    out = []
    for node_type in wanted:
        if node_type not in NODE_TYPE_KIND:
            out.append({"type": node_type, "error": "unknown node type"})
            continue
        entry = registry.get(node_type, {})
        item: dict[str, Any] = {
            "type": node_type,
            "kind": entry.get("kind", NODE_TYPE_KIND[node_type]),
            "runnable": node_type in EXECUTABLE_TYPES,
        }
        if "ports" in entry:
            item["ports"] = {
                side: [p.get("id") for p in entry["ports"].get(side, [])] for side in ("in", "out")
            }
        if "defaultConfig" in entry:
            item["defaultConfig"] = entry["defaultConfig"]
        if include_schema and "configSchema" in entry:
            item["configSchema"] = entry["configSchema"]
        out.append(item)
    return out


def validate(ir: dict[str, Any]) -> dict[str, Any]:
    """Check an IR document the way the control plane will, without running anything."""
    try:
        doc = parse_document(ir)
    except ValidationError as exc:
        problems = [
            f"{'.'.join(str(p) for p in err['loc']) or 'document'}: {err['msg']}"
            for err in exc.errors(include_url=False)[:20]
        ]
        return {"valid": False, "problems": problems}
    try:
        validate_graph(doc)
    except GraphValidationError as exc:
        return {"valid": False, "problems": [str(exc)]}
    not_runnable = sorted({n.type for n in doc.nodes if n.type not in EXECUTABLE_TYPES})
    if not_runnable:
        return {
            "valid": False,
            "problems": [
                f"node types {not_runnable} are declared in the IR but no runtime runs them"
            ],
        }
    return {"valid": True, "id": doc.id, "version": doc.version, "nodes": len(doc.nodes)}
