"""Capture proxy for the Claude Code traffic census.

A byte-faithful passthrough between Claude Code and the Anthropic API that records every exchange:
request headers (credentials redacted), a structural summary and fingerprint of each request body,
the response event stream, and timings. ``census.py`` turns the captures into a report.

    uv run --package theygent-ai-gateway python apps/ai-gateway/spike/capture_proxy.py
    ANTHROPIC_BASE_URL=http://127.0.0.1:8082 CLAUDE_CODE_GATEWAY_HINT_HEADERS=1 claude

Request bodies reach the upstream byte-for-byte unless a ``--rewrite`` experiment is active, and
then only the top-level ``model`` value changes (verified by re-parsing; any other difference
forwards the original). The one standing deviation is the upstream ``accept-encoding``, forced to
``identity`` so the side parser reads plain SSE; the client's value is recorded.

Captures hold prompt and code content, so they live in a private directory outside the
repository (0700 directory, 0600 files), and credential headers are redacted before anything is
written. ``--bodies`` bounds what is kept: request/response bodies of auxiliary requests only by
default, because main-conversation bodies carry the whole context on every request.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import hashlib
import itertools
import json
import os
import re
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
import uvicorn
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, Response, StreamingResponse
from starlette.routing import Route

HOP_BY_HOP = frozenset(
    {
        "connection",
        "keep-alive",
        "proxy-authenticate",
        "proxy-authorization",
        "te",
        "trailer",
        "trailers",
        "transfer-encoding",
        "upgrade",
        "host",
        "content-length",
    }
)
# Any header whose NAME looks credential-bearing is redacted in captures; values are never written.
SECRET_HEADER = re.compile(r"auth|key|token|secret|cookie|session-token", re.IGNORECASE)
# Identity headers that are safe to keep verbatim for the census (no credential material).
HINT_PREFIX = "x-claude-code-"


def _sha(data: bytes | str) -> str:
    raw = data.encode() if isinstance(data, str) else data
    return hashlib.sha256(raw).hexdigest()[:16]


def _credential_kind(value: str) -> str:
    token = value.split(" ", 1)[1] if value.lower().startswith("bearer ") else value
    if token.startswith("sk-ant-oat"):
        kind = "oauth"
    elif token.startswith("sk-ant-api"):
        kind = "api-key"
    elif token.startswith("sk-ant-admin"):
        kind = "admin-key"
    else:
        kind = "other"
    scheme = "bearer " if value.lower().startswith("bearer ") else ""
    return f"<redacted {scheme}{kind} len={len(token)}>"


def redact_headers(headers: list[tuple[bytes, bytes]]) -> dict[str, str]:
    out: dict[str, str] = {}
    for raw_name, raw_value in headers:
        name = raw_name.decode("latin-1").lower()
        value = raw_value.decode("latin-1")
        safe = _credential_kind(value) if SECRET_HEADER.search(name) else value
        out[name] = f"{out[name]}, {safe}" if name in out else safe
    return out


def credential_values(headers: list[tuple[bytes, bytes]]) -> list[str]:
    """Raw credential strings carried on a request, so the writer can scrub them from every other
    field of the record (an upstream or tool could echo one back in a header or body)."""
    values: list[str] = []
    for raw_name, raw_value in headers:
        if not SECRET_HEADER.search(raw_name.decode("latin-1")):
            continue
        value = raw_value.decode("latin-1")
        token = value.split(" ", 1)[1] if value.lower().startswith("bearer ") else value
        values.extend(v for v in (value, token) if len(v) >= 8)
    return values


def _block_text(block: Any) -> str:
    if isinstance(block, str):
        return block
    if isinstance(block, dict):
        text = block.get("text")
        return text if isinstance(text, str) else json.dumps(block, sort_keys=True)
    return json.dumps(block, sort_keys=True)


def _cache_ttls(node: Any) -> list[str]:
    """The TTL of every cache_control marker (a marker without one is the 5-minute default)."""
    if isinstance(node, dict):
        marker = node.get("cache_control")
        own = [str(marker.get("ttl", "5m"))] if isinstance(marker, dict) else []
        return own + [t for v in node.values() for t in _cache_ttls(v)]
    if isinstance(node, list):
        return [t for v in node for t in _cache_ttls(v)]
    return []


def summarize_request(body: bytes) -> dict[str, Any] | None:
    """Structure + fingerprint of a Messages request — no prompt text leaves this function except
    the first characters of system block 0 (the client-authored attribution block)."""
    try:
        data = json.loads(body)
    except (ValueError, UnicodeDecodeError):
        return None
    if not isinstance(data, dict):
        return None
    system = data.get("system")
    blocks = [system] if isinstance(system, str) else (system if isinstance(system, list) else [])
    texts = [_block_text(b) for b in blocks]
    tools = data.get("tools") if isinstance(data.get("tools"), list) else []
    tool_names = [t.get("name") or t.get("type") for t in tools if isinstance(t, dict)]
    messages = data.get("messages") if isinstance(data.get("messages"), list) else []
    last = messages[-1] if messages else {}
    last_content = last.get("content") if isinstance(last, dict) else None
    last_types = (
        [c.get("type") for c in last_content if isinstance(c, dict)]
        if isinstance(last_content, list)
        else (["text"] if isinstance(last_content, str) else [])
    )
    first_content = messages[0].get("content") if messages and isinstance(messages[0], dict) else ""
    output_config = data.get("output_config") if isinstance(data.get("output_config"), dict) else {}
    out_format = output_config.get("format") if isinstance(output_config, dict) else None
    thinking = data.get("thinking") if isinstance(data.get("thinking"), dict) else None
    metadata = data.get("metadata") if isinstance(data.get("metadata"), dict) else {}
    return {
        "model": data.get("model"),
        "stream": data.get("stream"),
        "max_tokens": data.get("max_tokens"),
        "top_level_keys": sorted(data.keys()),
        "thinking": thinking,
        "output_config_keys": sorted(output_config.keys()),
        "output_format_hash": _sha(json.dumps(out_format, sort_keys=True)) if out_format else None,
        "output_format_keys": sorted(
            ((out_format or {}).get("schema") or {}).get("properties", {}).keys()
        )
        if isinstance(out_format, dict)
        else [],
        "effort": output_config.get("effort"),
        "system_blocks": len(texts),
        "system_block_chars": [len(t) for t in texts],
        "system_block0_prefix": texts[0][:48] if texts else None,
        "system_hash_all": _sha("\x1e".join(texts)) if texts else None,
        "system_hash_tail": _sha("\x1e".join(texts[1:])) if len(texts) > 1 else None,
        "tool_count": len(tools),
        "tool_names": tool_names,
        "tools_hash": _sha(json.dumps(tools, sort_keys=True)) if tools else None,
        "message_count": len(messages),
        "first_role": messages[0].get("role")
        if messages and isinstance(messages[0], dict)
        else None,
        "last_role": last.get("role") if isinstance(last, dict) else None,
        "last_content_types": last_types,
        "msg0_hash": _sha(json.dumps(first_content, sort_keys=True)) if messages else None,
        "metadata_keys": sorted(metadata.keys()),
        "has_context_management": "context_management" in data,
        "cache_ttls": _cache_ttls(data),
        "body_bytes": len(body),
    }


class SseTee:
    """Incremental side parser for the relayed bytes: event timeline, usage, stop reason.
    It only observes — a parse failure is recorded and never touches the forwarded stream."""

    def __init__(self, t0: float) -> None:
        self.t0 = t0
        self.pending = b""
        self.events: list[tuple[int, str]] = []
        self.model: str | None = None
        self.usage: dict[str, Any] = {}
        self.stop_reason: str | None = None
        self.block_types: list[str] = []
        self.tool_uses: list[str] = []
        self.error: Any = None
        self.parse_errors = 0

    def feed(self, chunk: bytes) -> None:
        self.pending += chunk.replace(b"\r\n", b"\n")
        while b"\n\n" in self.pending:
            raw, self.pending = self.pending.split(b"\n\n", 1)
            self._event(raw)

    def _event(self, raw: bytes) -> None:
        name, data_lines = "message", []
        for line in raw.decode("utf-8", "replace").split("\n"):
            if line.startswith("event:"):
                name = line[6:].strip()
            elif line.startswith("data:"):
                data_lines.append(line[5:].lstrip())
        self.events.append((int((time.perf_counter() - self.t0) * 1000), name))
        if not data_lines:
            return
        try:
            data = json.loads("\n".join(data_lines))
        except ValueError:
            self.parse_errors += 1
            return
        if name == "message_start":
            message = data.get("message", {})
            self.model = message.get("model")
            self.usage.update(message.get("usage") or {})
        elif name == "message_delta":
            self.usage.update(data.get("usage") or {})
            reason = (data.get("delta") or {}).get("stop_reason")
            if reason:
                self.stop_reason = reason
        elif name == "content_block_start":
            block = data.get("content_block") or {}
            self.block_types.append(block.get("type"))
            if block.get("type") in ("tool_use", "server_tool_use"):
                self.tool_uses.append(block.get("name"))
        elif name == "error":
            self.error = data.get("error")

    def summary(self) -> dict[str, Any]:
        counts: dict[str, int] = {}
        for _, name in self.events:
            counts[name] = counts.get(name, 0) + 1
        gaps = [b[0] - a[0] for a, b in itertools.pairwise(self.events)]
        return {
            "model": self.model,
            "usage": self.usage,
            "stop_reason": self.stop_reason,
            "block_types": self.block_types,
            "tool_uses": self.tool_uses,
            "error": self.error,
            "event_counts": counts,
            "max_event_gap_ms": max(gaps) if gaps else None,
            "parse_errors": self.parse_errors,
        }


def summarize_json_response(body: bytes) -> dict[str, Any]:
    try:
        data = json.loads(body)
    except (ValueError, UnicodeDecodeError):
        return {"unparsed": True}
    if not isinstance(data, dict):
        return {"shape": type(data).__name__}
    content = data.get("content") if isinstance(data.get("content"), list) else []
    return {
        "model": data.get("model"),
        "usage": data.get("usage"),
        "stop_reason": data.get("stop_reason"),
        "block_types": [c.get("type") for c in content if isinstance(c, dict)],
        "tool_uses": [
            c.get("name") for c in content if isinstance(c, dict) and c.get("type") == "tool_use"
        ],
        "error": data.get("error"),
        "input_tokens": data.get("input_tokens"),
    }


# Per-target limits observed from real 400s: Haiku 4.5 rejects adaptive thinking and effort and
# caps output at 64000 tokens.
HAIKU_MAX_OUTPUT = 64000


def adapt_for_target(body: bytes, target: str) -> tuple[bytes, list[str]]:
    """Experiment-only shape adaptation for a lower-tier target. It re-serializes the body, so
    it is never byte-faithful, and it changes only top-level request settings — never system,
    tools, or messages."""
    if not target.startswith("claude-haiku-4-5"):
        return body, []
    data = json.loads(body)
    changes: list[str] = []
    if isinstance(data.get("thinking"), dict) and data["thinking"].get("type") == "adaptive":
        del data["thinking"]
        changes.append("dropped adaptive thinking")
    config = data.get("output_config")
    if isinstance(config, dict) and "effort" in config:
        del config["effort"]
        changes.append("dropped effort")
        if not config:
            del data["output_config"]
    if isinstance(data.get("max_tokens"), int) and data["max_tokens"] > HAIKU_MAX_OUTPUT:
        data["max_tokens"] = HAIKU_MAX_OUTPUT
        changes.append(f"max_tokens capped at {HAIKU_MAX_OUTPUT}")
    return json.dumps(data, ensure_ascii=False, separators=(",", ":")).encode(), changes


def apply_rewrite(body: bytes, target: str) -> tuple[bytes, str | None]:
    """Change only the top-level ``model`` value, as a targeted byte edit, then prove by
    re-parsing that nothing else moved. Returns (body, error) — on any doubt the original body."""
    try:
        original = json.loads(body)
    except ValueError:
        return body, "body is not JSON"
    source = original.get("model") if isinstance(original, dict) else None
    if not isinstance(source, str):
        return body, "no top-level model"
    pattern = re.compile(rb'"model"\s*:\s*"' + re.escape(source.encode()) + rb'"')
    edited, count = pattern.subn(b'"model":"' + target.encode() + b'"', body, count=1)
    if count != 1:
        return body, "model value not found"
    try:
        reparsed = json.loads(edited)
    except ValueError:
        return body, "edit broke the JSON"
    if reparsed.get("model") != target or {k: v for k, v in reparsed.items() if k != "model"} != {
        k: v for k, v in original.items() if k != "model"
    }:
        return body, "edit touched more than the top-level model"
    return edited, None


class CaptureWriter:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.seq = itertools.count(1)
        root.mkdir(parents=True, exist_ok=True)
        os.chmod(root, 0o700)

    def write(self, record: dict[str, Any], secrets: list[str]) -> Path:
        now = datetime.now(UTC)
        day = self.root / now.strftime("%Y-%m-%d")
        day.mkdir(exist_ok=True)
        os.chmod(day, 0o700)
        klass = record.get("request_class") or "unlabeled"
        path = day / f"{now.strftime('%H%M%S.%f')[:-3]}-{next(self.seq):05d}-{klass}.json"
        text = json.dumps(record, indent=1, default=str)
        # Longest first, so a bearer value is scrubbed whole before its bare token is.
        for secret in sorted(set(secrets), key=len, reverse=True):
            text = text.replace(secret, "<redacted>")
            text = text.replace(json.dumps(secret)[1:-1], "<redacted>")
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w") as fh:
            fh.write(text)
        return path


def parse_rewrites(specs: list[str]) -> dict[str, str]:
    rules: dict[str, str] = {}
    for spec in specs:
        key, sep, target = spec.partition("=")
        if not sep or not key or not target:
            raise SystemExit(f"--rewrite expects CLASS[:AGENT_TYPE]=MODEL, got {spec!r}")
        rules[key.strip().lower()] = target.strip()
    return rules


def build_app(
    upstream: str,
    writer: CaptureWriter,
    bodies: str,
    rewrites: dict[str, str],
    adapt: bool = False,
) -> Starlette:
    client = httpx.AsyncClient(
        timeout=httpx.Timeout(connect=15.0, read=None, write=120.0, pool=30.0),
        limits=httpx.Limits(max_connections=200, max_keepalive_connections=50),
        follow_redirects=False,
    )

    async def proxy(request: Request) -> Response:
        started = datetime.now(UTC).isoformat()
        t0 = time.perf_counter()
        body = await request.body()
        raw_headers = list(request.headers.raw)
        secrets = credential_values(raw_headers)
        hints = {
            k.decode("latin-1").lower(): v.decode("latin-1")
            for k, v in raw_headers
            if k.decode("latin-1").lower().startswith(HINT_PREFIX)
        }
        request_class = hints.get("x-claude-code-request-class")
        agent_type = hints.get("x-claude-code-agent-type")
        summary = summarize_request(body) if body else None
        keep_bodies = bodies == "all" or (bodies == "auxiliary" and request_class == "auxiliary")

        rewrite: dict[str, Any] | None = None
        rule = None
        if request_class and request.url.path.endswith("/v1/messages"):
            rule = rewrites.get(f"{request_class}:{(agent_type or '').lower()}") or rewrites.get(
                request_class
            )
        forward_body = body
        if rule:
            forward_body, problem = apply_rewrite(body, rule)
            adapted: list[str] = []
            if adapt and problem is None:
                forward_body, adapted = adapt_for_target(forward_body, rule)
            rewrite = {
                "to": rule,
                "from": (summary or {}).get("model"),
                "applied": problem is None,
                "problem": problem,
                "adapted": adapted,
            }

        forward_headers = [
            (k, v)
            for k, v in raw_headers
            if k.decode("latin-1").lower() not in HOP_BY_HOP and k.lower() != b"accept-encoding"
        ]
        forward_headers.append((b"accept-encoding", b"identity"))
        url = upstream + request.url.path + (f"?{request.url.query}" if request.url.query else "")

        record: dict[str, Any] = {
            "started": started,
            "method": request.method,
            "path": request.url.path,
            "query": request.url.query,
            "request_class": request_class,
            "agent_type": agent_type,
            "hints": hints,
            "request_headers": redact_headers(raw_headers),
            "request_summary": summary,
            "request_body": body.decode("utf-8", "replace") if keep_bodies and body else None,
            "rewrite": rewrite,
        }

        try:
            upstream_response = await client.send(
                client.build_request(
                    request.method, url, headers=forward_headers, content=forward_body
                ),
                stream=True,
            )
        except httpx.HTTPError as exc:
            record |= {"status": 502, "transport_error": repr(exc), "total_ms": _ms(t0)}
            _finish(writer, record, secrets)
            return JSONResponse(
                {
                    "type": "error",
                    "error": {
                        "type": "api_error",
                        "message": f"capture proxy: upstream unreachable: {exc}",
                    },
                },
                status_code=502,
            )

        ttfb_headers = _ms(t0)
        response_headers = [
            (k, v)
            for k, v in upstream_response.headers.raw
            if k.decode("latin-1").lower() not in HOP_BY_HOP
        ]
        content_type = upstream_response.headers.get("content-type", "")
        encoded = bool(upstream_response.headers.get("content-encoding"))
        is_sse = content_type.startswith("text/event-stream")
        tee = SseTee(t0)
        record |= {
            "status": upstream_response.status_code,
            "response_headers": redact_headers(list(upstream_response.headers.raw)),
            "headers_ms": ttfb_headers,
        }

        async def relay():
            captured = bytearray()
            first_byte: int | None = None
            outcome = "complete"
            try:
                async for chunk in upstream_response.aiter_raw():
                    if first_byte is None:
                        first_byte = _ms(t0)
                    if is_sse and not encoded:
                        tee.feed(chunk)
                    captured.extend(chunk)
                    yield chunk
            except asyncio.CancelledError:
                outcome = "client_disconnected"
                raise
            except httpx.HTTPError as exc:
                outcome = f"upstream_broke: {exc!r}"
                raise
            finally:
                await upstream_response.aclose()
                if is_sse and not encoded:
                    response_summary = tee.summary()
                elif not encoded:
                    response_summary = summarize_json_response(bytes(captured))
                else:
                    response_summary = {
                        "undecoded": upstream_response.headers.get("content-encoding")
                    }
                record.update(
                    {
                        "first_byte_ms": first_byte,
                        "total_ms": _ms(t0),
                        "outcome": outcome,
                        "response_bytes": len(captured),
                        "response_summary": response_summary,
                        "response_body": captured.decode("utf-8", "replace")
                        if keep_bodies
                        else None,
                    }
                )
                _finish(writer, record, secrets)

        response = StreamingResponse(relay(), status_code=upstream_response.status_code)
        response.raw_headers = response_headers
        return response

    @contextlib.asynccontextmanager
    async def lifespan(_app: Starlette):
        yield
        await client.aclose()

    return Starlette(
        routes=[
            Route(
                "/{path:path}",
                proxy,
                methods=["GET", "POST", "HEAD", "PUT", "PATCH", "DELETE", "OPTIONS"],
            )
        ],
        lifespan=lifespan,
    )


def _ms(t0: float) -> int:
    return int((time.perf_counter() - t0) * 1000)


def client_source(headers: dict[str, str]) -> str:
    """Which client sent the request: the entrypoint Claude Code names in its user-agent
    (``claude-cli/2.1.285 (external, cli)`` -> ``cli``), else the user-agent product."""
    agent = headers.get("user-agent", "")
    match = re.search(r"\(external, ([^,)]+)", agent)
    if match:
        return match.group(1)
    return agent.split("/", 1)[0] or "-"


def _finish(writer: CaptureWriter, record: dict[str, Any], secrets: list[str]) -> None:
    writer.write(record, secrets)
    summary = record.get("request_summary") or {}
    response = record.get("response_summary") or {}
    usage = response.get("usage") or {}
    rewrite = record.get("rewrite") or {}
    requested = summary.get("model") or "-"
    sent = rewrite["to"] if rewrite.get("applied") else requested
    served = response.get("model") or "-"
    route = requested if sent == requested else f"{requested} => {sent}"
    if served not in ("-", sent):
        route += f" (served {served})"
    session = (record.get("hints") or {}).get("x-claude-code-session-id") or "-"
    tokens = (
        f"in={usage.get('input_tokens')} cr={usage.get('cache_read_input_tokens')} "
        f"cw={usage.get('cache_creation_input_tokens')} out={usage.get('output_tokens')}"
        if usage
        else ""
    )
    print(
        f"{record['started'][11:19]} {client_source(record.get('request_headers') or {}):14} "
        f"{session[:8]:8} {record['method']:4} {record['path']:20} "
        f"{(record.get('request_class') or '-'):10} {(record.get('agent_type') or '-'):12} "
        f"{route:34} {record.get('status')} ttfb={record.get('first_byte_ms')}ms "
        f"total={record.get('total_ms')}ms {tokens}",
        flush=True,
    )


def main() -> None:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8082)
    parser.add_argument("--upstream", default="https://api.anthropic.com")
    parser.add_argument(
        "--capture-dir",
        type=Path,
        default=Path.home() / ".theygent" / "ai-gateway-spike" / "captures",
    )
    parser.add_argument("--bodies", choices=["none", "auxiliary", "all"], default="auxiliary")
    parser.add_argument(
        "--rewrite",
        action="append",
        default=[],
        metavar="CLASS[:AGENT_TYPE]=MODEL",
        help="experiment: send matching requests to MODEL (only the top-level model value changes)",
    )
    parser.add_argument(
        "--adapt",
        action="store_true",
        help="experiment: also adapt request settings a rewritten lower-tier target rejects",
    )
    args = parser.parse_args()
    if args.host not in ("127.0.0.1", "::1", "localhost"):
        sys.exit(
            "the capture proxy relays credentials and records content; it binds to loopback only"
        )
    rewrites = parse_rewrites(args.rewrite)
    writer = CaptureWriter(args.capture_dir.expanduser())
    print(
        f"capture proxy on http://{args.host}:{args.port} -> {args.upstream}\n"
        f"captures: {writer.root} (bodies: {args.bodies}; they contain prompt/code content)\n"
        f"rewrites: {rewrites or 'none'}",
        flush=True,
    )
    uvicorn.run(
        build_app(args.upstream, writer, args.bodies, rewrites, args.adapt),
        host=args.host,
        port=args.port,
        log_level="warning",
    )


if __name__ == "__main__":
    main()
