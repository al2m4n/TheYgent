// Save-as-agent — reuse the registry's existing semantics; invent none.
//
// A new agent → POST /agents (create); an existing one → POST /agents/{id}/versions (add a new
// immutable version). For a graph that began "new" but whose id already exists, the registry
// returns 409 `agent_exists` and the COCKPIT composes that into an add-version (the raw API does
// not auto-route). We mirror that compose here. Version bumping is the author's job
// via the editable `version` field — re-saving identical (id, version) content is an idempotent
// 200; different content under the same version is a 409 `version_conflict` ("bump the version"),
// surfaced verbatim. The server strips `view`, hashes, and returns the persisted contentHash.

import type { IRDocument } from "@theygent/ir-types";
import { type AgentDetail, ApiError, api } from "./api";

export async function saveAgent(ir: IRDocument, existing: boolean): Promise<AgentDetail> {
  if (existing) return api.addAgentVersion(ir.id, { ir });
  try {
    return await api.createAgent({ ir, name: ir.name });
  } catch (e) {
    if (e instanceof ApiError && e.status === 409 && e.code === "agent_exists") {
      return api.addAgentVersion(ir.id, { ir });
    }
    throw e;
  }
}

/** The newest version's server-computed contentHash from a registry response (versions are
 * newest-first). The frontend never computes this — it only displays what the server returns. */
export function latestHash(detail: AgentDetail): { version: string; contentHash: string } | null {
  const v = detail.versions[0];
  return v ? { version: v.version, contentHash: v.content_hash } : null;
}
