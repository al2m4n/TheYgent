"""What the gateway learns from a request without changing it.

Everything here reads a copy of what is being forwarded and never feeds back into it: a parser
that meets something unexpected yields ``None`` (and a named parse error), never an exception into
the forwarding path. Values that will leave the gateway are checked against the wire ``Token``
rule here, so a malformed header can never make a record unsendable later.

The routing-relevant facts come from three places:

* Claude Code's hint headers (``x-claude-code-*``, sent when ``CLAUDE_CODE_GATEWAY_HINT_HEADERS``
  is on): the request class, session/agent/prompt ids, agent type, compaction, tool durations;
* the user-agent and credential headers: which client sent it and how it is billed;
* the body's *structure*: model, thinking, effort, tool and message counts, whether a
  mid-conversation ``system`` message is present (some models reject those), whether it carries a
  ``safeguards`` field (auto-mode safety traffic), and an auxiliary-request fingerprint.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from typing import Any, cast, get_args
from urllib.parse import unquote

from pydantic import TypeAdapter, ValidationError
from theygent_ir.gateway import (
    CompactionTrigger,
    CredentialKind,
    RequestClass,
    RequestShape,
    Token,
)

_TOKEN = TypeAdapter(Token)
_KNOWN_CLASSES = frozenset(get_args(RequestClass)) - {"unlabeled", "unrecognized"}
_COMPACTION = frozenset(get_args(CompactionTrigger))
#: ``claude-cli/2.1.285 (external, sdk-cli)`` → version ``2.1.285``, entrypoint ``sdk-cli``.
_CLAUDE_CODE_UA = re.compile(
    r"^claude-cli/(?P<version>[^\s(]+)\s*\((?:[^,)]*,\s*)?(?P<entry>[^,)]+)"
)
_ATTRIBUTION_PREFIX = "x-anthropic-billing-header:"
_MCP_PREFIX = "mcp__"


def token_or_none(value: str | None) -> str | None:
    """``value`` if it may leave the gateway as a wire token, else ``None``."""
    if value is None:
        return None
    try:
        return _TOKEN.validate_python(value)
    except ValidationError:
        return None


def as_token(value: str) -> str:
    """Force an arbitrary string into a valid token (for values that must be recorded, like an
    unrecognized request class): disallowed characters become ``-``, length capped."""
    cleaned = re.sub(r"[^A-Za-z0-9._:@+/\[\]-]+", "-", value).strip("-.")[:128]
    return cleaned if cleaned and cleaned[0].isalnum() else f"x{cleaned}"[:128]


def _digest(value: Any) -> str:
    canonical = json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(canonical.encode()).hexdigest()[:16]


@dataclass(frozen=True)
class Hints:
    """Claude Code's routing hints, from its ``x-claude-code-*`` headers."""

    request_class: RequestClass = "unlabeled"
    request_class_raw: str | None = None
    session_id: str | None = None
    agent_id: str | None = None
    parent_agent_id: str | None = None
    agent_type: str | None = None
    prompt_id: str | None = None
    compaction: CompactionTrigger | None = None
    context_compacted: CompactionTrigger | None = None
    #: ``(tool name, milliseconds)`` for the tool calls whose results this request carries.
    prev_tool_durations: tuple[tuple[str, int], ...] = ()


def parse_hints(headers: dict[str, str]) -> Hints:
    raw_class = headers.get("x-claude-code-request-class")
    raw: str | None = None
    if raw_class is None:
        request_class: RequestClass = "unlabeled"
    elif raw_class in _KNOWN_CLASSES:
        request_class = cast(RequestClass, raw_class)
    else:
        request_class, raw = "unrecognized", as_token(raw_class)

    def trigger(name: str) -> CompactionTrigger | None:
        value = headers.get(name)
        return cast(CompactionTrigger, value) if value in _COMPACTION else None

    durations: list[tuple[str, int]] = []
    for entry in (headers.get("x-claude-code-prev-tool-durations") or "").split(";"):
        name, sep, ms = entry.partition("=")
        if sep and ms.isdigit() and (tool := token_or_none(unquote(name))):
            durations.append((tool, int(ms)))

    return Hints(
        request_class=request_class,
        request_class_raw=raw,
        session_id=token_or_none(headers.get("x-claude-code-session-id")),
        agent_id=token_or_none(headers.get("x-claude-code-agent-id")),
        parent_agent_id=token_or_none(headers.get("x-claude-code-parent-agent-id")),
        agent_type=token_or_none(headers.get("x-claude-code-agent-type")),
        prompt_id=token_or_none(headers.get("x-claude-code-prompt-id")),
        compaction=trigger("x-claude-code-compaction"),
        context_compacted=trigger("x-claude-code-context-compacted"),
        prev_tool_durations=tuple(durations),
    )


@dataclass(frozen=True)
class ClientIdentity:
    """Which client sent the request: Claude Code's entrypoint (``cli``, ``sdk-cli``, …) and
    version, or the user-agent's product name for anything else."""

    client: str | None
    version: str | None


def client_identity(user_agent: str | None) -> ClientIdentity:
    if not user_agent:
        return ClientIdentity(None, None)
    match = _CLAUDE_CODE_UA.match(user_agent)
    if match:
        return ClientIdentity(
            token_or_none(match["entry"].strip()), token_or_none(match["version"])
        )
    product = user_agent.split("/", 1)[0].split(" ", 1)[0]
    return ClientIdentity(token_or_none(product), None)


def credential_kind(headers: dict[str, str]) -> CredentialKind:
    """The credential the request would reach the upstream with. The gateway's own
    ``x-theygent-gateway-key`` identifies a developer; it is not this."""
    authorization = headers.get("authorization", "").strip()
    token = authorization[7:].strip() if authorization.lower().startswith("bearer ") else ""
    token = token or headers.get("x-api-key", "").strip()
    if not token:
        # An Authorization header of another scheme is still a credential, just not a known one.
        return "other" if authorization else "none"
    if token.startswith("sk-ant-oat"):
        return "oauth"
    if token.startswith("sk-ant-api"):
        return "api_key"
    if token.startswith("tyg_"):
        return "gateway_key"
    return "other"


@dataclass(frozen=True)
class BodySignals:
    """Structural facts about a Messages (or count-tokens) request body."""

    model: str | None
    stream: bool
    shape: RequestShape
    #: Identifies a *kind* of side request across sessions: Claude Code's system prompt (minus the
    #: per-request attribution block), its built-in tool names, and the output schema. A user's
    #: MCP tools are left out, so the same kind matches across users.
    fingerprint: str
    #: The first message, digested: requests continuing one conversation share it.
    first_message_digest: str | None
    #: ``type`` of each ``safeguards`` entry (e.g. ``dangerous_tool_use``): auto-mode safety
    #: traffic, which is never routed.
    safeguards: tuple[str, ...] = ()
    #: Claude Code's startup quota check: one token, one message, no system prompt, no tools.
    is_quota_probe: bool = False
    #: Every ``cache_control`` TTL in the request (``5m`` where the marker names none).
    cache_ttls: tuple[str, ...] = field(default=())


def _system_texts(system: Any) -> list[str]:
    if isinstance(system, str):
        return [system]
    if not isinstance(system, list):
        return []
    texts = [b.get("text", "") if isinstance(b, dict) else str(b) for b in system]
    if texts and texts[0].startswith(_ATTRIBUTION_PREFIX):
        texts = texts[1:]  # client version + per-conversation fingerprint: not the kind
    return texts


def _cache_ttls(node: Any) -> list[str]:
    if isinstance(node, dict):
        marker = node.get("cache_control")
        own = [str(marker.get("ttl", "5m"))] if isinstance(marker, dict) else []
        return own + [t for value in node.values() for t in _cache_ttls(value)]
    if isinstance(node, list):
        return [t for item in node for t in _cache_ttls(item)]
    return []


def body_signals(body: bytes) -> BodySignals | None:
    """Read a request body's structure. ``None`` when it is not a JSON object."""
    try:
        data = json.loads(body)
    except (ValueError, UnicodeDecodeError):
        return None
    if not isinstance(data, dict):
        return None
    tools = [t for t in data.get("tools") or [] if isinstance(t, dict)]
    messages = [m for m in data.get("messages") or [] if isinstance(m, dict)]
    thinking = data.get("thinking") if isinstance(data.get("thinking"), dict) else {}
    output_config = data.get("output_config") if isinstance(data.get("output_config"), dict) else {}
    output_format = output_config.get("format")
    max_tokens = data.get("max_tokens") if isinstance(data.get("max_tokens"), int) else None
    system_texts = _system_texts(data.get("system"))
    builtin_tools = sorted(
        str(t.get("name") or t.get("type"))
        for t in tools
        if not str(t.get("name", "")).startswith(_MCP_PREFIX)
    )
    safeguards = data.get("safeguards")
    shape = RequestShape(
        max_tokens=max_tokens if max_tokens is None or max_tokens >= 0 else None,
        tool_count=len(tools),
        deferred_tool_count=sum(1 for t in tools if t.get("defer_loading")),
        message_count=len(messages),
        thinking=token_or_none(thinking.get("type")),
        effort=token_or_none(output_config.get("effort")),
        has_output_schema=output_format is not None,
        has_mid_conversation_system=any(m.get("role") == "system" for m in messages),
        has_context_management="context_management" in data,
        body_bytes=len(body),
    )
    return BodySignals(
        model=token_or_none(data.get("model")) if isinstance(data.get("model"), str) else None,
        stream=data.get("stream") is True,
        shape=shape,
        fingerprint=_digest([system_texts, builtin_tools, output_format]),
        first_message_digest=_digest(messages[0]) if messages else None,
        safeguards=tuple(
            t
            for item in (safeguards if isinstance(safeguards, list) else [])
            if isinstance(item, dict) and (t := token_or_none(item.get("type")))
        ),
        is_quota_probe=(max_tokens == 1 and not system_texts and not tools and len(messages) == 1),
        cache_ttls=tuple(_cache_ttls(data)),
    )


@dataclass(frozen=True)
class RequestSignals:
    hints: Hints
    client: ClientIdentity
    credential: CredentialKind
    #: ``None`` for endpoints without a Messages body, or a body that is not JSON.
    body: BodySignals | None
    #: Names of the parsers that failed, for the record; never the failing content.
    parse_errors: tuple[str, ...] = ()


def read_request(
    headers: dict[str, str], body: bytes, *, has_messages_body: bool
) -> RequestSignals:
    """All request-side signals. Each part fails on its own: a bad body still yields the
    headers' signals, and nothing here raises."""
    errors: list[str] = []
    try:
        hints = parse_hints(headers)
    except Exception:
        hints = Hints()
        errors.append("hints")
    try:
        client = client_identity(headers.get("user-agent"))
    except Exception:
        client = ClientIdentity(None, None)
        errors.append("client")
    credential: CredentialKind
    try:
        credential = credential_kind(headers)
    except Exception:
        credential = "other"
        errors.append("credential")
    parsed: BodySignals | None = None
    if has_messages_body:
        try:
            parsed = body_signals(body)
            if parsed is None:
                errors.append("body_not_json")
        except Exception:
            errors.append("body")
    return RequestSignals(hints, client, credential, parsed, tuple(errors))
