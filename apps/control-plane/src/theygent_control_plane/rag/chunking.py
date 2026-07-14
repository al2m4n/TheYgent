"""Heading-aware markdown chunking — pure functions, no I/O.

Structure-first: chunks never cross a heading boundary (a section break is a meaning break),
blocks under one heading pack greedily up to the token budget, and only a single oversized
block falls back to blind splitting (with overlap, since a blind cut has no structural seam).
Each chunk records its heading path ("Install > macOS"); the *embedded* text is the
heading-path-prefixed chunk (cheap context that measurably lifts retrieval), while the stored
``text`` stays the raw chunk the user reads in results.

Token counts are estimated at ~4 chars/token. Retrieval budgets don't need exact tokenizer
parity with the embedding model — they need a stable, model-agnostic ceiling comfortably under
every candidate model's context window.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

_HEADING = re.compile(r"^(#{1,6})\s+(.*)$")
_FENCE = re.compile(r"^(```|~~~)")

#: Default chunk budget (~450 tokens) and the overlap carried across blind splits (~60).
DEFAULT_MAX_TOKENS = 450
DEFAULT_OVERLAP_TOKENS = 60
#: A trailing fragment smaller than this merges into the previous chunk instead of standing
#: alone — sub-25-token crumbs embed as noise.
MIN_TAIL_TOKENS = 25


@dataclass(frozen=True)
class Chunk:
    """One retrieval unit. ``heading`` is the joined heading path at the chunk's position
    (``None`` before the first heading); ``position`` orders chunks within their document."""

    text: str
    heading: str | None
    position: int


def estimate_tokens(text: str) -> int:
    return max(1, len(text) // 4)


def embedding_text(chunk: Chunk) -> str:
    """What actually gets embedded: the heading path prepended to the chunk text, so a chunk
    that says "click Install" still embeds near queries about the product/section it lives in."""
    if chunk.heading:
        return f"{chunk.heading}\n\n{chunk.text}"
    return chunk.text


@dataclass(frozen=True)
class _Block:
    text: str
    heading: str | None


def _split_blocks(markdown: str) -> list[_Block]:
    """Markdown → blocks (paragraph / fenced-code units), each tagged with its heading path.
    Fences are kept whole — a heading marker inside a code fence is code, not structure."""
    blocks: list[_Block] = []
    stack: list[tuple[int, str]] = []  # (level, title) — the open heading path
    lines: list[str] = []
    in_fence = False
    fence_marker = ""

    def flush() -> None:
        text = "\n".join(lines).strip()
        if text:
            heading = " > ".join(t for _, t in stack) or None
            blocks.append(_Block(text=text, heading=heading))
        lines.clear()

    for line in markdown.splitlines():
        fence = _FENCE.match(line.strip())
        if fence and not in_fence:
            in_fence, fence_marker = True, fence.group(1)
            lines.append(line)
            continue
        if in_fence:
            lines.append(line)
            if line.strip().startswith(fence_marker):
                in_fence = False
            continue
        heading = _HEADING.match(line)
        if heading:
            flush()
            level = len(heading.group(1))
            while stack and stack[-1][0] >= level:
                stack.pop()
            # Docs generators append a permalink marker to headings — noise in a heading path.
            title = heading.group(2).strip().removesuffix("¶").rstrip()
            if title:
                stack.append((level, title))
            continue
        if not line.strip():
            flush()
            continue
        lines.append(line)
    flush()
    return blocks


def _split_oversized(text: str, max_tokens: int, overlap_tokens: int) -> list[str]:
    """Blind-split one block that alone exceeds the budget: sentence-ish boundaries first,
    hard cuts as the last resort, with a tail overlap so a fact straddling the cut survives
    in at least one piece."""
    max_chars = max_tokens * 4
    overlap_chars = overlap_tokens * 4
    sentences = re.split(r"(?<=[.!?])\s+|\n", text)
    pieces: list[str] = []
    current = ""
    for sentence in sentences:
        if not sentence.strip():
            continue
        while len(sentence) > max_chars:  # a single monster "sentence" (minified text, tables)
            if current:
                pieces.append(current)
                current = ""
            pieces.append(sentence[:max_chars])
            sentence = sentence[max_chars - overlap_chars :]
        if current and len(current) + len(sentence) + 1 > max_chars:
            pieces.append(current)
            current = current[-overlap_chars:] if overlap_chars else ""
            current = current[current.find(" ") + 1 :] if " " in current else current
        current = f"{current} {sentence}".strip() if current else sentence
    if current:
        pieces.append(current)
    return pieces


def chunk_markdown(
    markdown: str,
    *,
    max_tokens: int = DEFAULT_MAX_TOKENS,
    overlap_tokens: int = DEFAULT_OVERLAP_TOKENS,
) -> list[Chunk]:
    """The one chunking entry point: markdown → ordered chunks. Deterministic and pure."""
    blocks = _split_blocks(markdown)
    chunks: list[Chunk] = []
    current: list[str] = []
    current_heading: str | None = None

    def flush() -> None:
        if not current:
            return
        text = "\n\n".join(current)
        chunks.append(Chunk(text=text, heading=current_heading, position=len(chunks)))
        current.clear()

    for block in blocks:
        if current and block.heading != current_heading:
            flush()  # structural boundary — never pack across headings
        current_heading = block.heading
        if estimate_tokens(block.text) > max_tokens:
            flush()
            for piece in _split_oversized(block.text, max_tokens, overlap_tokens):
                chunks.append(Chunk(text=piece, heading=block.heading, position=len(chunks)))
            continue
        packed = "\n\n".join([*current, block.text])
        if current and estimate_tokens(packed) > max_tokens:
            flush()
        current.append(block.text)
    flush()

    # Merge a trailing crumb into its predecessor (same heading only).
    merged: list[Chunk] = []
    for chunk in chunks:
        if (
            merged
            and estimate_tokens(chunk.text) < MIN_TAIL_TOKENS
            and merged[-1].heading == chunk.heading
        ):
            prev = merged.pop()
            merged.append(
                Chunk(
                    text=f"{prev.text}\n\n{chunk.text}",
                    heading=prev.heading,
                    position=prev.position,
                )
            )
            continue
        merged.append(Chunk(text=chunk.text, heading=chunk.heading, position=len(merged)))
    return merged
