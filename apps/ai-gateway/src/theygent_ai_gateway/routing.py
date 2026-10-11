"""Applying a routing decision to a request — router mode only.

Claude Code depends on what it sends arriving unchanged (see ``proxy``), so a routed request differs
from the original in exactly one place: the top-level ``model`` value. It is spliced into the
original text rather than re-serialized, so every other byte — key order, spacing, escapes, the
cached prefix — is what Claude Code sent. Anything that makes the splice ambiguous (no top-level
``model`` string, a duplicate one, a body that is not a JSON object) means the request is not
routed; it goes as it came.

A target can refuse what the requested model would accept. When it refuses before answering, the
gateway sends the original bytes to the requested model instead, so Claude Code never sees the
refusal — it disables a rejected capability for the rest of its conversation otherwise.
"""

from __future__ import annotations

import json
from json.decoder import scanstring

#: A target's answers that mean "not here", given before any of the response: the request is
#: retried on the requested model. 401 and 403 are about the credential, not the model, and would
#: fail there too.
RETRY_ON_REQUESTED = frozenset({400, 404, 413, 422, 429, 529})

_DECODER = json.JSONDecoder()
_WHITESPACE = " \t\n\r"


def _skip(text: str, index: int) -> int:
    while index < len(text) and text[index] in _WHITESPACE:
        index += 1
    return index


def _model_span(text: str) -> tuple[int, int] | None:
    """Where the top-level ``model`` string value sits in ``text`` (quotes included), walking only
    the object's top-level keys; other values are skipped by the C decoder. ``None`` unless there
    is exactly one, and it is a string."""
    index = _skip(text, 0)
    if index >= len(text) or text[index] != "{":
        return None
    index = _skip(text, index + 1)
    span: tuple[int, int] | None = None
    if index < len(text) and text[index] == "}":
        return None
    while True:
        if index >= len(text) or text[index] != '"':
            return None
        key, index = scanstring(text, index + 1)
        index = _skip(text, index)
        if index >= len(text) or text[index] != ":":
            return None
        start = _skip(text, index + 1)
        value, end = _DECODER.raw_decode(text, start)
        if key == "model":
            if span is not None or not isinstance(value, str):
                return None
            span = (start, end)
        index = _skip(text, end)
        if index < len(text) and text[index] == ",":
            index = _skip(text, index + 1)
            continue
        if index < len(text) and text[index] == "}":
            break
        return None
    return span if _skip(text, index + 1) == len(text) else None


def with_model(body: bytes, model: str) -> bytes | None:
    """``body`` with its top-level ``model`` replaced by ``model`` and nothing else changed;
    ``None`` when the body cannot be routed unambiguously."""
    try:
        text = body.decode("utf-8")
        span = _model_span(text)
    except (UnicodeDecodeError, ValueError):
        return None
    if span is None:
        return None
    start, end = span
    return (text[:start] + json.dumps(model) + text[end:]).encode("utf-8")
