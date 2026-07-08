// Session bookkeeping for the chat surfaces. Every chat records into a session so it shows up
// under Recents and can be reopened later. Two write paths exist by design:
//   - control-plane transports pass `session_id` on the run and the server appends the turns;
//   - direct data-plane transports (the bench) run in the user's trust domain, so the client
//     appends the finished turn pair itself afterwards.
// Recording is bookkeeping around the conversation — it must never break the live chat, so the
// helpers here warn instead of throwing.

import { api } from "../lib/api";

export type SessionKind = "chat" | "bench.model" | "bench.agent";

/** Stored opaquely on the session (JSONB); read back to re-open a session with the same target. */
export interface SessionMeta {
  kind: SessionKind;
  /** Logical model id (kind chat / bench.model). */
  model?: string;
  modality?: string;
  agent_id?: string;
  agent_name?: string;
  title?: string;
  [key: string]: unknown;
}

export function newSessionId(): string {
  return `ses_${crypto.randomUUID().replace(/-/g, "")}`;
}

/** Create (or re-assert) the session row with its target metadata. Returns the id, null on failure. */
export async function openSession(meta: SessionMeta, id?: string): Promise<string | null> {
  try {
    const created = await api.createSession({ id: id ?? newSessionId(), metadata: meta });
    return created.id;
  } catch (e) {
    console.warn("session create failed — chat continues unrecorded", e);
    return null;
  }
}

/** Append one finished user/assistant pair (the client-write path for direct data-plane chats). */
export async function recordTurn(
  sessionId: string,
  userContent: string,
  assistantContent: string,
): Promise<void> {
  // The no-blank-turns rule holds client-side too: an empty half means there is no pair to store.
  if (!userContent || !assistantContent) return;
  try {
    await api.appendSessionTurns(sessionId, {
      user_content: userContent,
      assistant_content: assistantContent,
    });
  } catch (e) {
    console.warn("session turn append failed — chat continues unrecorded", e);
  }
}

/** How a stored turn describes attachments that only existed client-side (bytes never persist). */
export function describeAttachments(text: string, notes: string[]): string {
  if (notes.length === 0) return text;
  const suffix = notes.map((n) => `[${n}]`).join(" ");
  return text ? `${text}\n${suffix}` : suffix;
}
