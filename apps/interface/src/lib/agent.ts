// Agent-document helpers: the seam between the agent registry envelopes and the typed IRDocument the
// canvas edits. Loading re-attaches the separately-stored `view` onto the IR; saving sends the IR
// (with its `view`) to the registry, which strips/hashes/persists. No new version semantics are
// invented here — we reuse whatever the registry exposes.

import { type IRDocument, NODE_TYPES } from "@theygent/ir-types";
import type { StoredVersion } from "./api";

export const SCHEMA_VERSION = "1.0";

/** A minimal valid starting graph: an `input` boundary wired to an `output` boundary. Both ports
 * are declared from the registry, and the single data edge feeds `output`'s required in-port — so
 * the blank graph already passes backend validation and runs (returning its input unchanged). */
export function blankGraph(id: string, name: string): IRDocument {
  const inputSpec = NODE_TYPES.input;
  const outputSpec = NODE_TYPES.output;
  return {
    schemaVersion: SCHEMA_VERSION,
    id,
    name,
    version: "0.1.0",
    models: {},
    tools: {},
    nodes: [
      {
        id: "n_in",
        type: "input",
        kind: inputSpec.kind,
        label: "input",
        config: {},
        ports: structuredClone(inputSpec.ports),
      },
      {
        id: "n_out",
        type: "output",
        kind: outputSpec.kind,
        label: "output",
        config: {},
        ports: structuredClone(outputSpec.ports),
      },
    ],
    edges: [
      {
        id: "e_in_out",
        source: "n_in",
        sourceHandle: "out",
        target: "n_out",
        targetHandle: "in",
        channel: "data",
        condition: null,
      },
    ],
    view: {
      nodes: {
        n_in: { position: { x: 80, y: 120 } },
        n_out: { position: { x: 420, y: 120 } },
      },
    },
  };
}

/** Merge a loaded registry version (stored IR + separate `view`) into one IRDocument the adapter
 * can render. The stored IR is view-stripped with `contentHash` stamped by the server; we re-attach
 * the layout so dragging picks up where the author left off. */
export function fromStoredVersion(sv: StoredVersion): IRDocument {
  return { ...sv.ir, view: (sv.view ?? undefined) as IRDocument["view"] };
}

/** The save body for the agent registry: `{ ir }` where the IR carries its `view`. The server strips
 * `view`, canonicalizes, hashes, and persists — the frontend computes NO hash. */
export function toSavePayload(ir: IRDocument): { ir: IRDocument } {
  return { ir };
}
