import { describe, expect, it } from "vitest";
import { type SSEEvent, parseSSEBuffer, readSSE } from "../src/lib/sse";

describe("parseSSEBuffer", () => {
  it("parses event + data frames and keeps the incomplete tail", () => {
    const buf = 'event: run\ndata: {"runId":"r1","status":"streaming"}\n\nevent: delta\ndata: hel';
    const { events, rest } = parseSSEBuffer(buf);
    expect(events).toEqual([{ event: "run", data: '{"runId":"r1","status":"streaming"}' }]);
    // The second frame is incomplete (no blank-line terminator) → returned as `rest`.
    expect(rest).toBe("event: delta\ndata: hel");
  });

  it("defaults the event name to 'message' and joins multi-line data", () => {
    const { events } = parseSSEBuffer("data: line1\ndata: line2\n\n");
    expect(events).toEqual([{ event: "message", data: "line1\nline2" }]);
  });

  it("strips exactly one leading space after the colon and ignores comments", () => {
    const { events } = parseSSEBuffer(": keep-alive\ndata:  two-spaces\n\n");
    expect(events).toEqual([{ event: "message", data: " two-spaces" }]);
  });

  it("normalizes CRLF line endings", () => {
    const { events } = parseSSEBuffer("event: run\r\ndata: x\r\n\r\n");
    expect(events).toEqual([{ event: "run", data: "x" }]);
  });

  it("treats the DONE sentinel as an ordinary data frame", () => {
    const { events } = parseSSEBuffer("data: [DONE]\n\n");
    expect(events).toEqual([{ event: "message", data: "[DONE]" }]);
  });
});

// Build a ReadableStream from string chunks to drive readSSE — proves chunk boundaries that
// split a frame are reassembled (the property that matters for token streaming).
function streamOf(chunks: string[]): ReadableStream<Uint8Array> {
  const enc = new TextEncoder();
  let i = 0;
  return new ReadableStream({
    pull(controller) {
      if (i < chunks.length) controller.enqueue(enc.encode(chunks[i++]));
      else controller.close();
    },
  });
}

describe("readSSE", () => {
  it("reassembles a frame split across chunk boundaries", async () => {
    const stream = streamOf([
      "event: run\nda",
      'ta: {"runId":"r1"}\n\nevent: delta\nda',
      'ta: {"runId":"r1","delta":"hi"}\n\n',
      "data: [DONE]\n\n",
    ]);
    const got: SSEEvent[] = [];
    for await (const ev of readSSE(stream)) got.push(ev);
    expect(got).toEqual([
      { event: "run", data: '{"runId":"r1"}' },
      { event: "delta", data: '{"runId":"r1","delta":"hi"}' },
      { event: "message", data: "[DONE]" },
    ]);
  });
});
