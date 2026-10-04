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

from census import fingerprint, load
from litellm.anthropic_interface import messages as anthropic_messages


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


def response_text(response: Any) -> str:
    data = response if isinstance(response, dict) else getattr(response, "model_dump", lambda: {})()
    return "".join(c.get("text", "") for c in data.get("content", []) if c.get("type") == "text")


async def replay(record: dict[str, Any], model: str, inference: str) -> dict[str, Any]:
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
            **request,
        )
        text, error = response_text(response), None
    except Exception as exc:  # every failure is a finding, reported per request
        text, error = "", f"{type(exc).__name__}: {exc}"[:200]
    latency = time.perf_counter() - t0
    valid: bool | None = None
    if keys and text:
        try:
            parsed = json.loads(text.strip().removeprefix("```json").removesuffix("```").strip())
            valid = isinstance(parsed, dict) and all(k in parsed for k in keys)
        except ValueError:
            valid = False
    reference = cloud_text(record)
    return {
        "fingerprint": fingerprint(record["request_summary"]),
        "latency_s": round(latency, 2),
        "ok": bool(text) and error is None,
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
    args = parser.parse_args()
    records = [
        r
        for r in load(args.captures.expanduser())
        if r.get("request_class") == "auxiliary"
        and r.get("status") == 200
        and r.get("request_body")
    ][: args.limit]
    if not records:
        sys.exit(
            "no auxiliary captures with stored bodies (run the proxy with --bodies auxiliary|all)"
        )
    results = [await replay(r, args.model, args.inference) for r in records]
    columns = ["fingerprint", "latency s", "ok", "schema valid", "similarity", "notes", "local"]
    columns += ["cloud", "error"]
    print("| " + " | ".join(columns) + " |")
    print("|" + "---|" * len(columns))
    for r in results:
        local = "" if args.no_previews else preview(r["local"])
        cloud = "" if args.no_previews else preview(r["cloud"])
        print(
            f"| {r['fingerprint']} | {r['latency_s']} | {r['ok']} | {r['schema_valid']} | "
            f"{r['similarity']} | {'; '.join(r['notes'])} | {local} | {cloud} | "
            f"{r['error'] or ''} |"
        )
    ok = [r for r in results if r["ok"]]
    print(
        f"\n{len(ok)}/{len(results)} answered; p50 latency "
        f"{sorted(r['latency_s'] for r in results)[len(results) // 2]} s"
    )


if __name__ == "__main__":
    asyncio.run(main())
