"""Engine-agnostic separation of a reasoning model's thinking from its answer.

OpenAI-compatible servers disagree on WHERE a reasoning model's thinking travels. Some
parse the chat template server-side and emit the thinking as a separate
``reasoning_content`` delta field (llama.cpp's server does this when reasoning formatting
is enabled) — the run paths already relay that field as ``event: reasoning``. Others pass
the raw template output straight through, so the thinking arrives INLINE in
``delta.content`` wrapped in ``<think>…</think>`` tags (the MLX chat server behaves this
way). Left unsplit, that inline form leaks into the answer stream, into ``run.output``,
and into stored session turns — breaking the invariant that reasoning is *never* folded
into the answer, and polluting session replay (past thinking would be re-sent as prompt
history on the next turn).

:class:`ThinkSplitter` is the one stateful splitter every ``content`` delta is routed
through, on both run paths (``/runs`` and the graph walker's ``execute_llm``), so the
split is engine-agnostic:

* **One LEADING block max.** ``<think>`` counts as markup only while the answer side is
  still empty-or-whitespace-only (effective stream start — where every real engine that
  inlines thinking puts it). Once any non-whitespace answer text has been emitted, and
  once the leading block has closed, ALL tags are literal answer text. This keeps a model
  that merely *prints* the tag mid-answer — code or docs about reasoning models — from
  having the rest of its answer silently swallowed as reasoning. (Models that interleave
  thinking with answering use the separate ``reasoning_content`` field, which is relayed
  independently of this splitter.)
* Inside the leading block, text up to the first ``</think>`` is **reasoning**; the tags
  themselves are emitted to neither side.
* A tag split across chunk boundaries never leaks half a tag: while recognition is live,
  the longest tail of the pending text that is a proper prefix of the sought tag
  (``<think>`` while armed, ``</think>`` inside the block) is held back until the next
  chunk decides it.
* The leading block left UNCLOSED at stream end stays reasoning — a model that spent its
  whole token budget thinking produced no answer, and the empty-output honesty path
  reports it.
* A held-back tail that turns out NOT to be a tag (``"<th"`` then end-of-stream) is
  released to the CURRENT side at :meth:`flush` — answer text is never dropped.
* A stray ``</think>`` with no opening tag is literal answer text.
"""

from __future__ import annotations

_OPEN = "<think>"
_CLOSE = "</think>"

# Splitter states: ARMED = at effective stream start, a leading <think> still counts as
# markup; THINKING = inside the leading block, seeking </think>; LITERAL = the answer has
# started (or the block closed) — everything is answer text, tags included.
_ARMED = "armed"
_THINKING = "thinking"
_LITERAL = "literal"


def _partial_tag_tail(text: str, tag: str) -> int:
    """Length of the longest tail of ``text`` that is a PROPER prefix of ``tag`` (0 = none).
    A full ``tag`` occurrence is found by ``str.find`` before this runs, so only strictly
    shorter prefixes are considered."""
    for k in range(min(len(text), len(tag) - 1), 0, -1):
        if text.endswith(tag[:k]):
            return k
    return 0


class ThinkSplitter:
    """Stateful ``<think>``-tag splitter for one stream. Feed every content delta through
    :meth:`push` and call :meth:`flush` once at stream end; each returns
    ``(answer_part, reasoning_part)`` — the text *released* by that call (either side may
    be empty while a potential tag is held back). Recognizes at most ONE leading think
    block (see the module docstring for why)."""

    def __init__(self) -> None:
        self._state = _ARMED
        self._held = ""  # tail held back because it may be the start of the sought tag

    def push(self, text: str) -> tuple[str, str]:
        answer: list[str] = []
        reasoning: list[str] = []
        data = self._held + text
        self._held = ""
        while data:
            if self._state == _LITERAL:
                # The answer has started / the leading block closed: tags are literal text.
                answer.append(data)
                break
            if self._state == _ARMED:
                i = data.find(_OPEN)
                if i != -1 and not data[:i].strip():
                    # A leading tag (nothing but whitespace before it): the block opens.
                    answer.append(data[:i])
                    data = data[i + len(_OPEN) :]
                    self._state = _THINKING
                    continue
                # A potential tag start at the tail is held only while everything before
                # it is still whitespace — recognition is only alive at stream start.
                k = _partial_tag_tail(data, _OPEN) if i == -1 else 0
                if k and not data[: len(data) - k].strip():
                    self._held = data[len(data) - k :]
                    answer.append(data[: len(data) - k])
                    break
                if not data.strip():
                    answer.append(data)  # still nothing but whitespace — stay armed
                    break
                # Non-whitespace answer text arrived first: any <think> is literal now.
                self._state = _LITERAL
                answer.append(data)
                break
            # _THINKING: inside the leading block, seeking the first close tag.
            i = data.find(_CLOSE)
            if i != -1:
                reasoning.append(data[:i])
                data = data[i + len(_CLOSE) :]
                self._state = _LITERAL  # one leading block max — all later tags literal
                continue
            k = _partial_tag_tail(data, _CLOSE)
            if k:
                self._held = data[len(data) - k :]
                data = data[: len(data) - k]
            reasoning.append(data)
            break
        return "".join(answer), "".join(reasoning)

    def flush(self) -> tuple[str, str]:
        """Release whatever is still held at stream end. A held false-partial tag belongs
        to the side the stream ended in: armed → answer text (never dropped); thinking →
        part of the unclosed leading block."""
        held, self._held = self._held, ""
        if not held:
            return "", ""
        return ("", held) if self._state == _THINKING else (held, "")


def split_think(text: str) -> tuple[str, str]:
    """Split one WHOLE message (the non-stream case) into ``(answer, reasoning)`` with the
    same semantics as streaming the text through a :class:`ThinkSplitter` — including the
    one-leading-block rule."""
    splitter = ThinkSplitter()
    answer, reasoning = splitter.push(text)
    tail_answer, tail_reasoning = splitter.flush()
    return answer + tail_answer, reasoning + tail_reasoning
