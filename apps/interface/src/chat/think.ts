// Incremental separation of a model's inline thinking from its answer.
//
// Engines differ in how a reasoning model's thinking reaches the stream: some emit a separate
// `reasoning_content` delta field (handled by the transports directly), others leave it inline in
// `content` as <think>…</think>. Content deltas are fed through this parser so the chat renders one
// consistent shape either way: thinking accumulates in `reasoning` (the collapsible block), the
// answer in `content` — and a tag split across two chunks never leaks half a tag into the answer.
//
// ONE LEADING BLOCK ONLY: `<think>` counts as markup only while the answer is still
// whitespace-empty — where every engine that inlines thinking puts it. Once any visible answer
// text exists (or the leading block has closed), both tags are literal text, so an answer that
// merely PRINTS the tags (code, docs about reasoning models) is never eaten. Engines that
// interleave thinking mid-answer use the separate `reasoning_content` field, which this parser
// never sees.

const OPEN = "<think>";
const CLOSE = "</think>";

/** Length of the longest suffix of `s` that is a proper prefix of `tag`. */
function partialTagSuffix(s: string, tag: string): number {
  const max = Math.min(s.length, tag.length - 1);
  for (let n = max; n > 0; n--) {
    if (s.endsWith(tag.slice(0, n))) return n;
  }
  return 0;
}

export class ThinkParser {
  content = "";
  reasoning = "";
  private mode: "content" | "reasoning" = "content";
  /** The one leading block has closed — every tag from here on is literal. */
  private blockDone = false;
  /** Held-back stream tail that might be the start of a tag. */
  private pending = "";

  /** Feed one streamed content delta. Returns true when `content` or `reasoning` changed. */
  push(delta: string): boolean {
    this.pending += delta;
    let changed = false;
    for (;;) {
      if (this.mode === "reasoning") {
        const idx = this.pending.indexOf(CLOSE);
        if (idx >= 0) {
          changed = this.emit(this.pending.slice(0, idx)) || changed;
          this.pending = this.pending.slice(idx + CLOSE.length);
          this.mode = "content";
          this.blockDone = true;
          continue;
        }
        const hold = partialTagSuffix(this.pending, CLOSE);
        changed = this.emit(this.pending.slice(0, this.pending.length - hold)) || changed;
        this.pending = hold ? this.pending.slice(this.pending.length - hold) : "";
        return changed;
      }

      // Answer mode: the opening tag is markup only while nothing visible has been answered.
      if (!this.blockDone && this.content.trim() === "") {
        const idx = this.pending.indexOf(OPEN);
        if (idx >= 0) {
          const before = this.pending.slice(0, idx);
          if (before.trim() === "") {
            changed = this.emit(before) || changed;
            this.pending = this.pending.slice(idx + OPEN.length);
            this.mode = "reasoning";
            continue;
          }
          // Visible answer text precedes the tag — it (and everything after) is literal.
        } else {
          const hold = partialTagSuffix(this.pending, OPEN);
          const release = this.pending.slice(0, this.pending.length - hold);
          if ((this.content + release).trim() === "") {
            // Still in the leading-whitespace window: release what's safe, keep the maybe-tag.
            changed = this.emit(release) || changed;
            this.pending = hold ? this.pending.slice(this.pending.length - hold) : "";
            return changed;
          }
          // The released text already starts the visible answer — no tag can follow as markup.
        }
      }
      changed = this.emit(this.pending) || changed;
      this.pending = "";
      return changed;
    }
  }

  /**
   * Stream ended: a held-back partial tag was plain text after all. An unclosed leading <think>
   * block (e.g. the budget ran out mid-thought) stays in `reasoning`, which is the honest reading.
   */
  flush(): boolean {
    const changed = this.emit(this.pending);
    this.pending = "";
    return changed;
  }

  private emit(text: string): boolean {
    if (!text) return false;
    if (this.mode === "content") this.content += text;
    else this.reasoning += text;
    return true;
  }
}
