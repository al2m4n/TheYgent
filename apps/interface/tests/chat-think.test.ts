// The reasoning/answer splitter must be chunk-boundary-proof: a <think> tag split across two
// stream chunks may never leak half a tag into the answer, an unclosed block stays thinking, and
// — the corruption guard — tags are markup ONLY as the leading block: an answer that merely
// prints the tags (code, docs) is never eaten.

import { describe, expect, it } from "vitest";
import { ThinkParser } from "../src/chat/think";

function feed(chunks: string[]): ThinkParser {
  const p = new ThinkParser();
  for (const c of chunks) p.push(c);
  p.flush();
  return p;
}

describe("ThinkParser", () => {
  it("passes plain content through untouched", () => {
    const p = feed(["hello ", "world"]);
    expect(p.content).toBe("hello world");
    expect(p.reasoning).toBe("");
  });

  it("splits a whole leading block arriving in one chunk", () => {
    const p = feed(["<think>let me see</think>the answer"]);
    expect(p.reasoning).toBe("let me see");
    expect(p.content).toBe("the answer");
  });

  it("handles tags split across chunk boundaries", () => {
    const p = feed(["<th", "ink>rea", "soning</thi", "nk>ans", "wer"]);
    expect(p.reasoning).toBe("reasoning");
    expect(p.content).toBe("answer");
  });

  it("accepts leading whitespace before the block", () => {
    const p = feed(["  \n<think>x</think> there"]);
    expect(p.content).toBe("  \n there");
    expect(p.reasoning).toBe("x");
  });

  it("leaves an unclosed block in reasoning (budget ran out mid-thought)", () => {
    const p = feed(["<think>half a tho", "ught"]);
    expect(p.reasoning).toBe("half a thought");
    expect(p.content).toBe("");
  });

  it("releases a false partial tag as text on flush", () => {
    const p = feed(["a < b and a <th"]);
    expect(p.content).toBe("a < b and a <th");
  });

  it("does not treat a stray close tag as markup in answer mode", () => {
    const p = feed(["no thinking </think> here"]);
    expect(p.content).toBe("no thinking </think> here");
    expect(p.reasoning).toBe("");
  });

  it("treats a tag AFTER visible answer text as literal — answers about the tags are never eaten", () => {
    const p = feed(["The tag is <think> and it closes with </think>. Done."]);
    expect(p.content).toBe("The tag is <think> and it closes with </think>. Done.");
    expect(p.reasoning).toBe("");
  });

  it("treats a second block after the leading one as literal (one leading block max)", () => {
    const p = feed(["<think>a</think>one<think>b</think>two"]);
    expect(p.reasoning).toBe("a");
    expect(p.content).toBe("one<think>b</think>two");
  });

  it("keeps code samples containing both tags intact mid-answer", () => {
    const p = feed(["<think>plan</think>Use `<think>` like this: ", "<think>steps</think>."]);
    expect(p.reasoning).toBe("plan");
    expect(p.content).toBe("Use `<think>` like this: <think>steps</think>.");
  });

  it("does not hold back a partial tag once the visible answer has started", () => {
    const p = new ThinkParser();
    p.push("5 <t");
    // "<t" could no longer open a block (the answer has begun) — it must not be held back.
    expect(p.content).toBe("5 <t");
    p.push("en items");
    p.flush();
    expect(p.content).toBe("5 <ten items");
  });

  it("reports when nothing changed", () => {
    const p = new ThinkParser();
    expect(p.push("")).toBe(false);
    expect(p.push("<thi")).toBe(false); // held back, nothing released yet
    expect(p.push("nk>")).toBe(false); // tag consumed, still nothing visible
    expect(p.push("x")).toBe(true);
  });
});
