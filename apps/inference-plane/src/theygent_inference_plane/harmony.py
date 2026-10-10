"""The harmony response format (gpt-oss), parsed into reasoning, answer text and tool calls.

gpt-oss writes every reply as a run of harmony messages, each a header and a body:

    <|channel|>analysis<|message|>Need the RFC. Use the tool.<|end|>
    <|start|>assistant<|channel|>commentary to=functions.find <|constrain|>json<|message|>{"q":1}

``mlx_lm.server`` returns that text verbatim as ``content`` — it has no harmony parser — so
without this module an agent's tools never run and the hidden reasoning lands in the answer.
llama.cpp parses harmony itself, so this applies only to the gated MLX chat path.

Channel → OpenAI shape:
  * ``analysis`` → ``reasoning_content`` (the field llama.cpp uses and the control plane reads)
  * ``final`` → ``content``
  * ``commentary`` addressed ``to=functions.<name>`` → a ``tool_calls`` entry, when ``<name>`` is
    a tool the request offered; any other message body (a preamble, an unknown recipient) →
    ``content``.

A message ends at ``<|end|>``, ``<|call|>`` or ``<|return|>``, or where the next ``<|start|>``
begins; the stop token ending the reply is often stripped, so the last message may simply run
to the end of the text. The parser is incremental (``HarmonyStream``) so reasoning and answer
text stream as they arrive; tool-call arguments are held until their message ends.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass

_START = "<|start|>"
_MESSAGE = "<|message|>"
_TERMINATORS = ("<|end|>", "<|call|>", "<|return|>")
#: A reply in harmony opens with one of these (the prompt already ended in ``<|start|>assistant``).
_OPENERS = ("<|channel|>", _START)

_CHANNEL = re.compile(r"<\|channel\|>\s*(\w+)")
_RECIPIENT = re.compile(r"\bto=([^\s<]+)")
_FUNCTION_PREFIX = "functions."
#: What the first part of a special token (``<|end|>``) looks like when a chunk cuts it.
_PARTIAL_TOKEN = re.compile(r"<(?:\|\w*\|?)?")


def could_open_harmony(text: str) -> bool:
    """Whether ``text`` (the reply so far) is, or may still become, the start of a harmony reply.
    False as soon as it cannot — the caller then treats the reply as plain text."""
    stripped = text.lstrip()
    return any(opener.startswith(stripped) or stripped.startswith(opener) for opener in _OPENERS)


def is_harmony(text: str | None) -> bool:
    if not text:
        return False
    stripped = text.lstrip()
    return any(stripped.startswith(opener) for opener in _OPENERS)


@dataclass
class HarmonyCall:
    name: str
    arguments: str


@dataclass
class _Header:
    channel: str | None
    recipient: str | None


def _parse_header(header: str) -> _Header:
    channel = _CHANNEL.search(header)
    recipient = _RECIPIENT.search(header)
    return _Header(
        channel=channel.group(1) if channel else None,
        recipient=recipient.group(1) if recipient else None,
    )


def _arguments(body: str) -> str:
    """The call's arguments as the OpenAI JSON string. A body that is not JSON becomes ``{}``,
    as for the other text tool-call formats (``tool_parse``)."""
    text = body.strip()
    try:
        json.loads(text)
    except ValueError:
        return json.dumps({})
    return text


class HarmonyStream:
    """Incremental harmony parser. ``feed`` returns ``(kind, text)`` pieces ready to emit —
    ``kind`` is ``"reasoning"`` or ``"content"`` — and ``finish`` flushes the rest; tool calls
    accumulate on ``calls``. ``offered`` is the set of function names the request offered."""

    def __init__(self, offered: set[str]) -> None:
        self.offered = offered
        self.calls: list[HarmonyCall] = []
        self._buffer = ""
        self._in_body = False
        self._header = _Header(None, None)
        self._call_body = ""

    def _kind(self) -> str:
        if self._header.channel == "analysis":
            return "reasoning"
        if self._call_name() is not None:
            return "call"
        return "content"

    def _call_name(self) -> str | None:
        recipient = self._header.recipient or ""
        if not recipient.startswith(_FUNCTION_PREFIX):
            return None
        name = recipient[len(_FUNCTION_PREFIX) :]
        return name if name in self.offered else None

    def _emit(self, text: str, out: list[tuple[str, str]]) -> None:
        if not text:
            return
        kind = self._kind()
        if kind == "call":
            self._call_body += text
        elif out and out[-1][0] == kind:
            out[-1] = (kind, out[-1][1] + text)
        else:
            out.append((kind, text))

    def _end_message(self) -> None:
        name = self._call_name()
        if name is not None:
            self.calls.append(HarmonyCall(name=name, arguments=_arguments(self._call_body)))
        self._call_body = ""
        self._in_body = False
        self._header = _Header(None, None)

    def feed(self, text: str) -> list[tuple[str, str]]:
        self._buffer += text
        out: list[tuple[str, str]] = []
        while self._buffer:
            if not self._in_body:
                at = self._buffer.find(_MESSAGE)
                if at == -1:
                    return out  # the header is still arriving
                self._header = _parse_header(self._buffer[:at])
                self._buffer = self._buffer[at + len(_MESSAGE) :]
                self._in_body = True
                continue
            ends = [
                (i, token)
                for token in (*_TERMINATORS, _START)
                if (i := self._buffer.find(token)) != -1
            ]
            if ends:
                at, token = min(ends)
                self._emit(self._buffer[:at], out)
                # A <|start|> opens the next header, so it stays for the header parse.
                self._buffer = self._buffer[at + (0 if token == _START else len(token)) :]
                self._end_message()
                continue
            # Hold back a trailing "<", "<|", "<|en"… that may be the start of a special token.
            hold = self._buffer.rfind("<")
            if hold != -1 and _PARTIAL_TOKEN.fullmatch(self._buffer[hold:]):
                self._emit(self._buffer[:hold], out)
                self._buffer = self._buffer[hold:]
                return out
            self._emit(self._buffer, out)
            self._buffer = ""
        return out

    def finish(self) -> list[tuple[str, str]]:
        """The reply ended: flush what is held and close the last message (its stop token is
        often stripped). A header that never reached its body carries nothing to show."""
        out: list[tuple[str, str]] = []
        if self._in_body:
            self._emit(self._buffer, out)
            self._end_message()
        self._buffer = ""
        return out


def parse(text: str, offered: set[str]) -> tuple[str, str, list[HarmonyCall]]:
    """A whole harmony reply → ``(reasoning, content, calls)``."""
    stream = HarmonyStream(offered=offered)
    pieces = stream.feed(text) + stream.finish()
    reasoning = "".join(t for kind, t in pieces if kind == "reasoning")
    content = "".join(t for kind, t in pieces if kind == "content")
    return reasoning, content, stream.calls
