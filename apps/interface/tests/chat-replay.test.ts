// The transcript replay must stay template-legal: user/assistant strictly alternating, failed
// pairs dropped whole (a lone user message from a failed exchange would wedge strict chat
// templates on every later turn), images as content blocks.

import { describe, expect, it } from "vitest";
import type { ChatMessage } from "../src/chat/types";
import { toWireMessages } from "../src/chat/useInferenceChat";

function msg(partial: Partial<ChatMessage> & Pick<ChatMessage, "role" | "content">): ChatMessage {
  return { id: crypto.randomUUID(), ...partial };
}

describe("toWireMessages", () => {
  it("replays alternating turns with an optional system prompt", () => {
    const wire = toWireMessages(
      [
        msg({ role: "user", content: "hi" }),
        msg({ role: "assistant", content: "hello" }),
        msg({ role: "user", content: "again" }),
      ],
      "be brief",
    );
    expect(wire.map((m) => m.role)).toEqual(["system", "user", "assistant", "user"]);
  });

  it("drops a failed pair WHOLE — no consecutive user messages", () => {
    const wire = toWireMessages(
      [
        msg({ role: "user", content: "first" }),
        msg({ role: "assistant", content: "", error: "boom" }),
        msg({ role: "user", content: "second" }),
        msg({ role: "assistant", content: "ok" }),
        msg({ role: "user", content: "third" }),
      ],
      undefined,
    );
    expect(wire).toEqual([
      { role: "user", content: "second" },
      { role: "assistant", content: "ok" },
      { role: "user", content: "third" },
    ]);
  });

  it("keeps the in-flight user message that has no assistant yet", () => {
    const wire = toWireMessages([msg({ role: "user", content: "new question" })], undefined);
    expect(wire).toEqual([{ role: "user", content: "new question" }]);
  });

  it("builds image content blocks for vision turns", () => {
    const wire = toWireMessages(
      [
        msg({
          role: "user",
          content: "what is this?",
          attachments: [{ kind: "image", url: "data:image/jpeg;base64,xxx" }],
        }),
      ],
      undefined,
    );
    expect(wire[0].content).toEqual([
      { type: "text", text: "what is this?" },
      { type: "image_url", image_url: { url: "data:image/jpeg;base64,xxx" } },
    ]);
  });
});
