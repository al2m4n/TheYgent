"""Build the gateway's test fixtures from capture-proxy records, keeping structure and dropping
content.

    uv run --package theygent-ai-gateway python apps/ai-gateway/spike/make_fixtures.py \
        ~/.theygent/ai-gateway-spike

Each fixture is one real Claude Code exchange rebuilt field by field. What the gateway's logic
reads is kept exactly: model, thinking and effort settings, cache markers and their TTLs, tool
counts and deferred-loading flags, the message and content-block structure, beta headers, the
hint headers, response event order, stop reasons, and token usage. Everything that is content —
prompts, code, tool descriptions and schemas' prose, tool inputs and results, model output,
Claude Code's own system prompt — becomes a placeholder such as ``[text 3f9a0c1e 418ch]``.

Placeholders are keyed HMACs with a fresh random key per run: equal originals map to equal
placeholders (so "this side request starts with the main conversation's first message" survives),
yet nothing in a fixture can be reversed or matched against outside text. Identifiers (session,
agent and prompt ids, message and tool-use ids, request ids) map to fixture ids, consistently
across the whole set, so a conversation's requests still group together. Tool names that are not
Claude Code built-ins (e.g. a user's MCP connectors) become ``mcp__fixture__tool_N``.
"""

from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import os
import re
from pathlib import Path
from typing import Any

OUT_DIR = Path(__file__).resolve().parents[1] / "tests" / "fixtures" / "exchanges"

#: (capture subdir, file name, fixture name, conversation, sequence, description, note)
MANIFEST: list[tuple[str, str, str, str, int, str, str | None]] = [
    (
        "captures-synthetic",
        "215821.080-00010-main.json",
        "main-bugfix-1",
        "bugfix",
        1,
        "Main loop, first turn of a bug-fix task (cold cache).",
        None,
    ),
    (
        "captures-synthetic",
        "215824.650-00011-main.json",
        "main-bugfix-2",
        "bugfix",
        2,
        "Main loop, second turn: tool results returned, cache warm.",
        None,
    ),
    (
        "captures-synthetic",
        "215827.452-00012-main.json",
        "main-bugfix-3",
        "bugfix",
        3,
        "Main loop, final turn of the bug-fix task.",
        None,
    ),
    (
        "captures-synthetic",
        "215934.438-00021-compaction.json",
        "compaction-manual",
        "bugfix",
        4,
        "Manual /compact on the bug-fix conversation; reads the main conversation's cache.",
        None,
    ),
    (
        "captures-synthetic",
        "215905.837-00014-main.json",
        "main-explore-1",
        "explore",
        1,
        "Main loop asks for an Explore subagent.",
        None,
    ),
    (
        "captures-synthetic",
        "215908.217-00015-subagent.json",
        "subagent-explore-1",
        "explore",
        2,
        "Explore subagent, first request (fresh conversation, cold cache).",
        None,
    ),
    (
        "captures-synthetic",
        "215910.663-00016-subagent.json",
        "subagent-explore-2",
        "explore",
        3,
        "Explore subagent, second request.",
        None,
    ),
    (
        "captures-synthetic",
        "215916.801-00017-subagent.json",
        "subagent-explore-3",
        "explore",
        4,
        "Explore subagent, final request.",
        None,
    ),
    (
        "captures-synthetic",
        "215919.016-00018-main.json",
        "main-explore-2",
        "explore",
        5,
        "Main loop after the subagent returned.",
        None,
    ),
    (
        "captures-synthetic",
        "215922.214-00019-main.json",
        "main-explore-3",
        "explore",
        6,
        "Main loop, final turn.",
        None,
    ),
    (
        "captures-synthetic",
        "220023.005-00003-subagent.json",
        "subagent-explore-sonnet-1",
        "explore-sonnet",
        1,
        "The same Explore work, first request.",
        "Served by claude-sonnet-5-5: the capture proxy rewrote the model (spike experiment).",
    ),
    (
        "captures-synthetic",
        "220025.600-00004-subagent.json",
        "subagent-explore-sonnet-2",
        "explore-sonnet",
        2,
        "The same Explore work, second request.",
        "Served by claude-sonnet-5-5: the capture proxy rewrote the model (spike experiment).",
    ),
    (
        "captures-synthetic",
        "220032.065-00005-subagent.json",
        "subagent-explore-sonnet-3",
        "explore-sonnet",
        3,
        "The same Explore work, final request.",
        "Served by claude-sonnet-5-5: the capture proxy rewrote the model (spike experiment).",
    ),
    (
        "captures-synthetic",
        "220110.660-00003-subagent.json",
        "subagent-haiku-adaptive-400",
        "haiku-rejections",
        1,
        "Haiku 4.5 rejects adaptive thinking.",
        "Sent upstream as claude-haiku-4-5 by the capture proxy (spike experiment).",
    ),
    (
        "captures-synthetic",
        "220110.913-00004-subagent.json",
        "subagent-haiku-effort-400",
        "haiku-rejections",
        2,
        "Haiku 4.5 rejects effort (Claude Code had dropped thinking).",
        "Sent upstream as claude-haiku-4-5 by the capture proxy (spike experiment).",
    ),
    (
        "captures-synthetic",
        "220111.151-00005-subagent.json",
        "subagent-haiku-max-tokens-400",
        "haiku-rejections",
        3,
        "Haiku 4.5 rejects max_tokens above 64000.",
        "Sent upstream as claude-haiku-4-5 by the capture proxy (spike experiment).",
    ),
    (
        "captures-synthetic",
        "220240.077-00003-subagent.json",
        "subagent-haiku-per-turn-effort-400",
        "haiku-rejections-adapted",
        1,
        "Haiku 4.5 rejects per-turn effort carried in messages.",
        "Sent upstream as claude-haiku-4-5 with top-level thinking/effort/max_tokens adapted.",
    ),
    (
        "captures-synthetic",
        "220240.321-00004-subagent.json",
        "subagent-haiku-system-role-400",
        "haiku-rejections-adapted",
        2,
        "Haiku 4.5 rejects mid-conversation role: system messages.",
        "Sent upstream as claude-haiku-4-5 with top-level thinking/effort/max_tokens adapted.",
    ),
    (
        "captures-synthetic",
        "214214.493-00003-main.json",
        "main-expired-login-401",
        "expired-login",
        1,
        "A stored claude.ai login that has expired.",
        None,
    ),
    (
        "captures-synthetic",
        "215715.246-00006-main.json",
        "main-tool-search-off",
        "tool-search-off",
        1,
        "One-line prompt with tool search off: every connector tool sent upfront.",
        None,
    ),
    (
        "captures-synthetic",
        "215758.426-00008-main.json",
        "main-tool-search-on",
        "tool-search-on",
        1,
        "The same prompt with ENABLE_TOOL_SEARCH=true: tools deferred.",
        None,
    ),
    (
        "captures",
        "093659.334-00004-auxiliary.json",
        "auxiliary-quota-check-429",
        "interactive",
        1,
        "Startup quota check (max_tokens 1), throttled with a retryable 429.",
        None,
    ),
    (
        "captures",
        "093720.892-00005-auxiliary.json",
        "auxiliary-session-title",
        "interactive",
        2,
        "Session title: structured output with a title field, no tools.",
        None,
    ),
    (
        "captures",
        "094041.951-00010-auxiliary.json",
        "auxiliary-with-safeguards",
        "interactive",
        3,
        "Side request sharing the main conversation's cached prefix, carrying a `safeguards` field "
        "(type dangerous_tool_use: the auto-mode safety check).",
        None,
    ),
]
#: A fixture derived from a captured one, for a failure no capture recorded.
DROPPED = (
    "main-bugfix-1",
    "main-dropped-mid-stream",
    3,
    "The main-bugfix-1 stream with the upstream dropping after three events.",
)

#: Strings kept verbatim wherever they appear: vocabulary and settings, never content.
KEEP_KEYS = frozenset(
    {
        "type",
        "role",
        "media_type",
        "stop_reason",
        "stop_sequence",
        "model",
        "display",
        "effort",
        "ttl",
        "service_tier",
        "inference_geo",
        "speed",
        "scope",
    }
)
REQUEST_HEADERS_KEPT = frozenset(
    {
        "content-type",
        "accept",
        "accept-encoding",
        "anthropic-version",
        "anthropic-beta",
        "anthropic-dangerous-direct-browser-access",
        "user-agent",
        "x-app",
        "x-claude-code-request-class",
        "x-claude-code-agent-type",
        "x-claude-code-compaction",
        "x-claude-code-context-compacted",
    }
)
REQUEST_ID_HEADERS = frozenset(
    {
        "x-claude-code-session-id",
        "x-claude-code-agent-id",
        "x-claude-code-parent-agent-id",
        "x-claude-code-prompt-id",
    }
)
RESPONSE_HEADERS_KEPT = frozenset({"content-type", "retry-after", "x-should-retry"})
BUILTIN_TOOL = re.compile(r"^[A-Z][A-Za-z]+$")
#: Top-level request fields that are configuration, kept as-is (prose fields aside). Every other
#: top-level field is reduced to an outline: e.g. ``safeguards`` carries the auto-mode
#: classifier's context — working directory, home directory, username, git state.
SETTINGS_KEYS = frozenset(
    {
        "model",
        "max_tokens",
        "stream",
        "thinking",
        "output_config",
        "context_management",
        "temperature",
        "top_p",
        "top_k",
        "stop_sequences",
        "tool_choice",
        "service_tier",
        "speed",
        "inference_geo",
    }
)
#: A dict key kept as-is inside content: a plain snake_case or camelCase field name. Anything
#: else (a path, a session id used as a key) is content too.
FIELD_NAME = re.compile(r"^[A-Za-z][A-Za-z0-9_]{0,40}$")
#: Stand-in schema for tools that are not Claude Code built-ins: their schemas belong to the
#: user's connectors and can reflect the user's own data.
STUB_SCHEMA = {"type": "object", "properties": {}}


class Scrubber:
    def __init__(self) -> None:
        self.key = os.urandom(32)
        self.ids: dict[str, str] = {}
        self.uuids: dict[str, str] = {}
        self.tools: dict[str, str] = {}

    def text(self, value: str) -> str:
        if not value:
            return value
        digest = hmac.new(self.key, value.encode(), hashlib.sha256).hexdigest()[:8]
        return f"[text {digest} {len(value)}ch]"

    def uuid(self, value: str) -> str:
        if value not in self.uuids:
            self.uuids[value] = f"00000000-0000-4000-8000-{len(self.uuids) + 1:012x}"
        return self.uuids[value]

    def ident(self, value: str) -> str:
        if value not in self.ids:
            prefix = value.split("_", 1)[0] if "_" in value else "id"
            self.ids[value] = f"{prefix}_fixture{len(self.ids) + 1:04d}"
        return self.ids[value]

    def agent(self, value: str) -> str:
        if value not in self.ids:
            self.ids[value] = f"a{len(self.ids) + 1:016x}"
        return self.ids[value]

    def tool(self, name: str) -> str:
        if BUILTIN_TOOL.match(name):
            return name
        if name not in self.tools:
            self.tools[name] = f"mcp__fixture__tool_{len(self.tools) + 1}"
        return self.tools[name]

    def walk(self, node: Any, key: str | None = None) -> Any:
        """Content scrub: structure and vocabulary kept, every other string a placeholder."""
        if isinstance(node, dict):
            return {
                (k if FIELD_NAME.match(k) else self.text(k)): self.walk(v, k)
                for k, v in node.items()
            }
        if isinstance(node, list):
            return [self.walk(item, key) for item in node]
        if not isinstance(node, str):
            return node
        if key in KEEP_KEYS:
            return node
        if key in ("id", "tool_use_id", "request_id"):
            return self.ident(node)
        if key in ("name", "tool_name"):
            return self.tool(node)
        if key == "signature":
            return "fixture-signature"
        if key == "data":
            return "fixture-data"
        return self.text(node)

    def settings(self, node: Any, key: str | None = None) -> Any:
        """Request settings are configuration, kept as-is except prose fields."""
        if isinstance(node, dict):
            return {k: self.settings(v, k) for k, v in node.items()}
        if isinstance(node, list):
            return [self.settings(item, key) for item in node]
        if isinstance(node, str) and key in ("description", "title", "text", "content"):
            return self.text(node)
        return node

    def system(self, system: Any) -> Any:
        if isinstance(system, str):
            return self.text(system)
        blocks = []
        for index, block in enumerate(system):
            text = block.get("text", "")
            if index == 0 and text.startswith("x-anthropic-billing-header:"):
                fields = dict(
                    part.strip().split("=", 1)
                    for part in text.split(":", 1)[1].split(";")
                    if "=" in part
                )
                kept = "; ".join(
                    f"{k}={fields[k]}" for k in ("cc_version", "cc_entrypoint") if k in fields
                )
                blocks.append({**block, "text": f"x-anthropic-billing-header: {kept};"})
            else:
                blocks.append(self.walk(block))
        return blocks

    def tool_definition(self, tool: dict[str, Any]) -> dict[str, Any]:
        out: dict[str, Any] = {}
        for key, value in tool.items():
            if key == "name":
                out[key] = self.tool(value)
            elif key == "description":
                out[key] = self.text(value)
            elif key == "input_schema":
                builtin = BUILTIN_TOOL.match(tool.get("name", ""))
                out[key] = self.settings(value) if builtin else dict(STUB_SCHEMA)
            else:
                out[key] = self.settings(value, key)
        return out

    def body(self, body: dict[str, Any]) -> dict[str, Any]:
        out: dict[str, Any] = {}
        for key, value in body.items():
            if key == "system":
                out[key] = self.system(value)
            elif key == "tools":
                out[key] = [self.tool_definition(tool) for tool in value]
            elif key == "messages":
                out[key] = self.walk(value)
            elif key == "metadata":
                out[key] = {k: "fixture" for k in value}
            elif key in SETTINGS_KEYS:
                out[key] = self.settings(value, key)
            else:
                out[key] = self.outline(value)
        return out

    def outline(self, value: Any) -> Any:
        """An unknown top-level field, reduced to its presence: lists keep one entry per item
        with only its ``type``. Its contents (e.g. the classifier context's environment and the
        client's internal flags) are not needed by any test and are not committed."""
        if isinstance(value, list):
            return [
                {"type": item["type"]} if isinstance(item, dict) and "type" in item else {}
                for item in value
            ]
        if isinstance(value, dict):
            return {"type": value["type"]} if "type" in value else {}
        return self.text(str(value))

    def request_headers(self, headers: dict[str, str]) -> list[list[str]]:
        out: list[list[str]] = []
        for name, value in headers.items():
            if name in REQUEST_HEADERS_KEPT or name.startswith("x-stainless-"):
                out.append([name, value])
            elif name in ("x-claude-code-session-id", "x-claude-code-prompt-id"):
                out.append([name, self.uuid(value)])
            elif name in REQUEST_ID_HEADERS:
                out.append([name, self.agent(value)])
            elif name == "x-claude-code-prev-tool-durations":
                pairs = [p.split("=", 1) for p in value.split(";") if "=" in p]
                out.append([name, ";".join(f"{self.tool(n)}={ms}" for n, ms in pairs)])
            elif name == "authorization":
                kind = "oat01" if "oauth" in value else "api03"
                out.append([name, f"Bearer sk-ant-{kind}-fixture"])
            elif name == "x-api-key":
                out.append([name, "sk-ant-api03-fixture"])
            elif name == "x-theygent-gateway-key":
                out.append([name, "tyg_fixture"])
        return out

    def response_headers(self, headers: dict[str, str]) -> list[list[str]]:
        out: list[list[str]] = []
        for name, value in headers.items():
            if name in RESPONSE_HEADERS_KEPT or name.startswith("anthropic-ratelimit-"):
                out.append([name, value])
            elif name == "request-id":
                out.append([name, self.ident(value)])
        return out

    def events(self, raw: str) -> list[str]:
        parsed: list[tuple[str, dict[str, Any]]] = []
        for chunk in raw.split("\n\n"):
            if not chunk.strip():
                continue
            name, data = "message", ""
            for line in chunk.split("\n"):
                if line.startswith("event:"):
                    name = line[6:].strip()
                elif line.startswith("data:"):
                    data += line[5:].strip()
            parsed.append((name, json.loads(data) if data else {}))
        # Tool inputs stream as JSON fragments; scrub the whole input once, send it in the first
        # fragment, and empty the rest, so the concatenation stays valid JSON.
        fragments: dict[int, str] = {}
        for _, data in parsed:
            delta = data.get("delta") or {}
            if delta.get("type") == "input_json_delta":
                fragments[data["index"]] = fragments.get(data["index"], "") + delta["partial_json"]
        emitted: set[int] = set()
        out: list[str] = []
        for name, data in parsed:
            delta = data.get("delta") or {}
            kind = delta.get("type")
            if kind == "input_json_delta":
                index = data["index"]
                if index in emitted:
                    delta["partial_json"] = ""
                else:
                    emitted.add(index)
                    full = fragments[index]
                    delta["partial_json"] = json.dumps(
                        self.walk(json.loads(full)) if full else {}, ensure_ascii=False
                    )
                scrubbed = data
            elif kind == "text_delta":
                delta["text"] = self.text(delta["text"])
                scrubbed = data
            elif kind == "thinking_delta":
                delta["thinking"] = self.text(delta["thinking"])
                scrubbed = data
            elif kind == "signature_delta":
                delta["signature"] = "fixture-signature"
                scrubbed = data
            elif name == "error":
                scrubbed = data  # the API's own error text, kept for verbatim-error tests
            else:
                scrubbed = self.walk(data)
            data_json = json.dumps(scrubbed, ensure_ascii=False, separators=(",", ":"))
            out.append(f"event: {name}\ndata: {data_json}\n\n")
        return out

    def json_response(self, raw: str) -> Any:
        data = json.loads(raw)
        if data.get("type") == "error":
            if data.get("request_id"):
                data["request_id"] = self.ident(data["request_id"])
            return data
        return self.walk(data)


def build(captures: Path) -> list[dict[str, Any]]:
    scrubber = Scrubber()
    fixtures: list[dict[str, Any]] = []
    for subdir, filename, name, conversation, sequence, description, note in MANIFEST:
        [path] = list((captures / subdir).rglob(filename))
        record = json.loads(path.read_text())
        body = json.loads(record["request_body"])
        response_body = record.get("response_body") or ""
        content_type = (record.get("response_headers") or {}).get("content-type", "")
        response: dict[str, Any] = {
            "status": record["status"],
            "headers": scrubber.response_headers(record.get("response_headers") or {}),
        }
        if content_type.startswith("text/event-stream"):
            response["events"] = scrubber.events(response_body)
        else:
            response["json"] = scrubber.json_response(response_body)
        fixtures.append(
            {
                "name": name,
                "description": description,
                "note": note,
                "source": "headless" if subdir == "captures-synthetic" else "interactive",
                "client_version": "2.1.285",
                "conversation": conversation,
                "sequence": sequence,
                "request_class": record["request_class"],
                "request": {
                    "method": record["method"],
                    "path": record["path"],
                    "query": record.get("query") or "",
                    "headers": scrubber.request_headers(record["request_headers"]),
                    "body": scrubber.body(body),
                },
                "response": response,
                "abort_after_events": None,
            }
        )
    base_name, name, keep, description = DROPPED
    base = next(f for f in fixtures if f["name"] == base_name)
    dropped = json.loads(json.dumps(base))
    dropped.update(
        name=name,
        description=description,
        note="Derived: no capture recorded an upstream dropping mid-stream.",
        source="derived",
        conversation="dropped",
        sequence=1,
        abort_after_events=keep,
    )
    dropped["response"]["events"] = base["response"]["events"][:keep]
    fixtures.append(dropped)
    return fixtures


def main() -> None:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("captures", type=Path, help="the directory holding captures*/ folders")
    args = parser.parse_args()
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    for old in OUT_DIR.glob("*.json"):
        old.unlink()
    for fixture in build(args.captures.expanduser()):
        target = OUT_DIR / f"{fixture['name']}.json"
        target.write_text(json.dumps(fixture, ensure_ascii=False, indent=1) + "\n")
        print(f"wrote {target.relative_to(OUT_DIR.parents[2])} ({target.stat().st_size} bytes)")


if __name__ == "__main__":
    main()
