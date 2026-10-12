"""Replay captured auxiliary requests against a local model, to measure whether side requests can
be answered locally: latency, whether an answer comes back at all, structured-output validity, and
how close the text is to what the cloud model returned.

The request goes Anthropic-format → LiteLLM's Anthropic adapter → the inference plane's
OpenAI-compatible ``/v1`` by logical id, i.e. the same path the gateway's local route would take,
with the inference plane's frozen contract untouched. Run it in the inference plane's environment,
which already carries LiteLLM:

    uv run --package theygent-inference-plane python apps/ai-gateway/spike/local_replay.py \
        ~/.theygent/ai-gateway-spike/captures --model aux-local [--limit 20] [--no-previews]

Two shape adaptations are applied and reported, because a local chat template accepts neither:
mid-conversation ``role: system`` messages are folded into the system prompt, and Anthropic-only
fields (thinking, context management, output_config, metadata) are dropped.
"""

from __future__ import annotations

import argparse
import asyncio
import difflib
import json
import sys
import time
from pathlib import Path
from typing import Any

import litellm
from census import fingerprint, load
from litellm.anthropic_interface import messages as anthropic_messages

# LiteLLM sends `openai/` models through the OpenAI Responses API by default; the inference plane
# serves chat completions only, so the adapter must take the chat-completions path.
litellm.use_chat_completions_url_for_anthropic_messages = True


def cloud_text(record: dict[str, Any]) -> str:
    body = record.get("response_body") or ""
    parts: list[str] = []
    for raw in body.split("\n\n"):
        for line in raw.split("\n"):
            if not line.startswith("data:"):
                continue
            try:
                data = json.loads(line[5:].strip())
            except ValueError:
                continue
            delta = data.get("delta") or {}
            if delta.get("type") == "text_delta":
                parts.append(delta.get("text", ""))
            elif data.get("type") == "message" and isinstance(data.get("content"), list):
                parts += [c.get("text", "") for c in data["content"] if c.get("type") == "text"]
    if not parts and body.lstrip().startswith("{"):
        try:
            data = json.loads(body)
            parts = [c.get("text", "") for c in data.get("content", []) if c.get("type") == "text"]
        except ValueError:
            pass
    return "".join(parts)


def adapt(body: dict[str, Any]) -> tuple[dict[str, Any], list[str]]:
    notes: list[str] = []
    system = body.get("system")
    blocks = [system] if isinstance(system, str) else list(system or [])
    texts = [b if isinstance(b, str) else b.get("text", "") for b in blocks]
    if texts and texts[0].startswith("x-anthropic-billing-header"):
        texts = texts[1:]
    kept: list[dict[str, Any]] = []
    for message in body.get("messages", []):
        if message.get("role") == "system":
            content = message.get("content")
            texts.append(
                content
                if isinstance(content, str)
                else " ".join(c.get("text", "") for c in content or [] if isinstance(c, dict))
            )
            notes.append("folded mid-conversation system message")
        else:
            kept.append(message)
    dropped = [
        k for k in ("thinking", "context_management", "output_config", "metadata") if k in body
    ]
    if dropped:
        notes.append("dropped " + ",".join(dropped))
    request = {
        "messages": kept,
        "system": "\n\n".join(t for t in texts if t),
        "max_tokens": min(int(body.get("max_tokens") or 1024), 2048),
    }
    if body.get("tools"):
        request["tools"] = body["tools"]
    return request, notes


def expected_schema_keys(body: dict[str, Any]) -> list[str]:
    fmt = (body.get("output_config") or {}).get("format") or {}
    return sorted(((fmt.get("schema") or {}).get("properties") or {}).keys())


def response_parts(response: Any) -> tuple[str, list[str], dict[str, Any]]:
    """Text, tool-call names, and usage of an Anthropic-format response."""
    data = response if isinstance(response, dict) else getattr(response, "model_dump", lambda: {})()
    content = [c for c in data.get("content") or [] if isinstance(c, dict)]
    text = "".join(c.get("text", "") for c in content if c.get("type") == "text")
    tools = [c.get("name", "") for c in content if c.get("type") == "tool_use"]
    return text, tools, data.get("usage") or {}


async def replay(
    record: dict[str, Any], model: str, inference: str, request_timeout_s: float
) -> dict[str, Any]:
    body = json.loads(record["request_body"])
    request, notes = adapt(body)
    keys = expected_schema_keys(body)
    t0 = time.perf_counter()
    try:
        response = await anthropic_messages.acreate(
            model=f"openai/{model}",
            api_base=f"{inference.rstrip('/')}/v1",
            api_key="sk-noauth",
            stream=False,
            # An abandoned non-streaming call keeps the engine busy, and LiteLLM's default is a
            # 600 s timeout followed by a retry — so bound it and never retry.
            timeout=request_timeout_s,
            num_retries=0,
            **request,
        )
        text, tools, usage = response_parts(response)
        error = None
    except Exception as exc:  # every failure is a finding, reported per request
        text, tools, usage, error = "", [], {}, f"{type(exc).__name__}: {exc}"[:200]
    latency = time.perf_counter() - t0
    valid: bool | None = None
    if keys and text:
        try:
            parsed = json.loads(text.strip().removeprefix("```json").removesuffix("```").strip())
            valid = isinstance(parsed, dict) and all(k in parsed for k in keys)
        except ValueError:
            valid = False
    reference = cloud_text(record)
    cloud_tools = (record.get("response_summary") or {}).get("tool_uses") or []
    return {
        "fingerprint": fingerprint(record["request_summary"]),
        "latency_s": round(latency, 2),
        "ok": bool(text or tools) and error is None,
        "input_tokens": usage.get("input_tokens"),
        "output_tokens": usage.get("output_tokens"),
        "local_tools": tools,
        "cloud_tools": cloud_tools,
        # The proposed shadow metric for agent turns: did both pick the same next action?
        "same_next_action": (tools[:1] == cloud_tools[:1]) if error is None else None,
        "schema_valid": valid,
        "similarity": round(difflib.SequenceMatcher(None, reference, text).ratio(), 2)
        if reference
        else None,
        "notes": notes,
        "error": error,
        "local": text,
        "cloud": reference,
    }


def preview(text: str) -> str:
    return " ".join(text.split())[:60].replace("|", "\\|")


async def main() -> None:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("captures", type=Path)
    parser.add_argument(
        "--model", required=True, help="logical id registered on the inference plane"
    )
    parser.add_argument("--inference", default="http://127.0.0.1:8081")
    parser.add_argument("--limit", type=int, default=20)
    parser.add_argument("--no-previews", action="store_true")
    parser.add_argument(
        "--request-class",
        default="auxiliary",
        help="which captured class to replay (auxiliary, subagent, main, compaction)",
    )
    parser.add_argument("--timeout", type=float, default=180.0)
    parser.add_argument(
        "--max-body-bytes",
        type=int,
        default=300_000,
        help="skip requests larger than this (a laptop-sized model cannot take the largest)",
    )
    args = parser.parse_args()
    records = [
        r
        for r in load(args.captures.expanduser())
        if r.get("request_class") == args.request_class
        and r.get("status") == 200
        and r.get("request_body")
        and len(r["request_body"]) <= args.max_body_bytes
    ][: args.limit]
    if not records:
        sys.exit(
            f"no {args.request_class} captures with stored bodies "
            "(run the proxy with --bodies auxiliary|all)"
        )
    results = [await replay(r, args.model, args.inference, args.timeout) for r in records]
    columns = ["fingerprint", "latency s", "ok", "in/out tokens", "schema valid", "similarity"]
    columns += [
        "local tools",
        "cloud tools",
        "same next action",
        "notes",
        "local",
        "cloud",
        "error",
    ]
    print("| " + " | ".join(columns) + " |")
    print("|" + "---|" * len(columns))
    for r in results:
        local = "" if args.no_previews else preview(r["local"])
        cloud = "" if args.no_previews else preview(r["cloud"])
        print(
            f"| {r['fingerprint']} | {r['latency_s']} | {r['ok']} | "
            f"{r['input_tokens']}/{r['output_tokens']} | {r['schema_valid']} | {r['similarity']} | "
            f"{','.join(r['local_tools']) or '-'} | {','.join(r['cloud_tools']) or '-'} | "
            f"{r['same_next_action']} | {'; '.join(r['notes'])} | {local} | {cloud} | "
            f"{r['error'] or ''} |"
        )
    ok = [r for r in results if r["ok"]]
    agree = [r for r in results if r["same_next_action"]]
    print(
        f"\n{len(ok)}/{len(results)} answered; {len(agree)}/{len(results)} same next action; "
        f"p50 latency {sorted(r['latency_s'] for r in results)[len(results) // 2]} s"
    )


if __name__ == "__main__":
    asyncio.run(main())
