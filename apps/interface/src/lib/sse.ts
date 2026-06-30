// SSE over fetch + ReadableStream (M8 §3.2 — the recorded fork). `EventSource` can't POST a
// body, send custom headers (we need x-theygent-run-id-class headers), or abort cleanly, and
// the run composer *creates and streams from the same POST*. So every streaming surface goes
// through this one parser. It is transport-agnostic: hand it any byte stream of SSE frames.

export interface SSEEvent {
  /** The `event:` field; defaults to "message" per the SSE spec when omitted. */
  event: string;
  /** The joined `data:` lines (multiple `data:` lines are newline-joined). */
  data: string;
}

/**
 * Parse a raw SSE text buffer into complete events. Returns the events found and the
 * leftover (incomplete trailing frame) to be prepended to the next chunk. Pure + sync so it
 * is trivially unit-testable (M8 §5) — the streaming I/O lives in {@link readSSE}.
 */
export function parseSSEBuffer(buffer: string): { events: SSEEvent[]; rest: string } {
  // Frames are separated by a blank line. Normalize CRLF first.
  const normalized = buffer.replace(/\r\n/g, "\n");
  const parts = normalized.split("\n\n");
  // The last part is either "" (buffer ended on a boundary) or an incomplete frame.
  const rest = parts.pop() ?? "";
  const events: SSEEvent[] = [];
  for (const frame of parts) {
    if (frame.trim() === "") continue;
    let event = "message";
    const dataLines: string[] = [];
    for (const line of frame.split("\n")) {
      if (line.startsWith(":")) continue; // comment line
      const colon = line.indexOf(":");
      const field = colon === -1 ? line : line.slice(0, colon);
      // Per spec a single leading space after the colon is stripped.
      let value = colon === -1 ? "" : line.slice(colon + 1);
      if (value.startsWith(" ")) value = value.slice(1);
      if (field === "event") event = value;
      else if (field === "data") dataLines.push(value);
    }
    events.push({ event, data: dataLines.join("\n") });
  }
  return { events, rest };
}

/**
 * Read an SSE `Response` body to completion, yielding each parsed event. The control-plane's
 * terminal `data: [DONE]` sentinel is yielded as a normal event (data === "[DONE]") so the
 * caller decides what "done" means; it does not special-case it here.
 */
export async function* readSSE(
  body: ReadableStream<Uint8Array>,
  signal?: AbortSignal,
): AsyncGenerator<SSEEvent> {
  const reader = body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  try {
    while (true) {
      if (signal?.aborted) return;
      const { done, value } = await reader.read();
      if (done) break;
      buffer += decoder.decode(value, { stream: true });
      const { events, rest } = parseSSEBuffer(buffer);
      buffer = rest;
      for (const ev of events) yield ev;
    }
    // Flush any complete frame left in the buffer at stream end.
    const { events } = parseSSEBuffer(buffer.endsWith("\n\n") ? buffer : `${buffer}\n\n`);
    for (const ev of events) yield ev;
  } finally {
    reader.releaseLock();
  }
}
