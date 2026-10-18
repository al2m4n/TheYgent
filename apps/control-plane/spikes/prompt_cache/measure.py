"""Does an agent's tool loop reuse the engine's prompt cache from one model call to the next?

Replays one agent the way the control plane's walker runs it: a system prompt, the tool schemas,
the user's question, then two tool rounds. After each model turn it appends the assistant
tool-call message and the tool result with the walker's own helpers
(``assistant_tool_calls_message`` / ``tool_result_message``), so every request has the shape
TheYgent sends. A second run then asks the same agent a different question. Every call
streams with ``stream_options.include_usage``, as the walker does, and records the prompt size,
how many prompt tokens the engine served from its cache
(``usage.prompt_tokens_details.cached_tokens``), and the time to the first streamed token.

Run under the control plane's environment, against an OpenAI-compatible base URL (an engine
directly, or the inference plane):

    uv run --package theygent-control-plane python \
        apps/control-plane/spikes/prompt_cache/measure.py \
        --base http://127.0.0.1:8081/v1 --model <id> --out result.json
"""

from __future__ import annotations

import argparse
import json
import time
from pathlib import Path
from typing import Any

import httpx
from theygent_control_plane.walker import (
    ActivityOutcome,
    ToolCall,
    assistant_tool_calls_message,
    tool_result_message,
)

SYSTEM = (
    "You are the operations assistant for a mid-sized online store. You help staff answer "
    "questions about orders, inventory, shipping and refunds. Always look facts up with the "
    "tools instead of guessing: order details come from get_order, stock levels from "
    "check_inventory, shipping status from track_shipment, and policy questions from "
    "search_policies. When a tool returns an error, say what failed and what to try next. "
    "Keep answers short: state the answer first, then the evidence, then any action the staff "
    "member should take. Quote order numbers and SKUs exactly as the tools return them. Never "
    "promise a refund or a delivery date the tools do not confirm. If a question needs several "
    "lookups, do them one at a time and explain how the results combine. Refund rules: items "
    "may be returned within 30 days of delivery if unused; damaged items are refunded in full "
    "including shipping; custom orders are final sale. Shipping rules: standard shipping takes "
    "3 to 5 business days, express 1 to 2; carriers update tracking once a day; a parcel with "
    "no scan for 7 days is considered lost and is reshipped at no cost. Inventory rules: stock "
    "below the reorder point is flagged; items can be reserved for 48 hours for a customer. "
) * 2

TOOLS: list[dict[str, Any]] = [
    {
        "type": "function",
        "function": {
            "name": name,
            "description": description,
            "parameters": {
                "type": "object",
                "properties": {arg: {"type": "string", "description": arg_doc}},
                "required": [arg],
            },
        },
    }
    for name, description, arg, arg_doc in [
        ("get_order", "Look up an order: items, totals, status, dates.", "order_id", "e.g. A-1042"),
        ("check_inventory", "Current stock for a SKU, with reorder point.", "sku", "e.g. MUG-12"),
        ("track_shipment", "Carrier status and scans for an order.", "order_id", "e.g. A-1042"),
        ("search_policies", "Search the store's written policies.", "query", "free text"),
    ]
]

TOOL_RESULT = json.dumps(
    {
        "order_id": "A-1042",
        "status": "shipped",
        "items": [
            {"sku": "MUG-12", "name": "Stoneware mug, 12 oz", "qty": 2, "price": 18.0},
            {"sku": "TEA-SAMP", "name": "Tea sampler, 6 flavours", "qty": 1, "price": 24.5},
        ],
        "placed": "2026-10-02",
        "delivered": None,
        "carrier": "ParcelCo",
        "scans": [
            {"at": "2026-10-03T09:12", "where": "Origin hub", "event": "picked up"},
            {"at": "2026-10-04T18:40", "where": "Regional hub", "event": "in transit"},
            {"at": "2026-10-05T07:02", "where": "Regional hub", "event": "departed"},
        ],
        "notes": "Customer asked twice about delivery; no scan since 2026-10-05.",
    }
)

QUESTIONS = [
    "Order A-1042 still hasn't arrived. Is it lost, and what should I tell the customer?",
    "Do we have enough MUG-12 in stock to replace the two mugs in order A-1042?",
]
# The calls each tool round makes when the model does not choose one itself (a small model may
# answer instead of calling): the loop still needs a realistic second and third request.
FALLBACK_CALLS = [("get_order", {"order_id": "A-1042"}), ("track_shipment", {"order_id": "A-1042"})]


def stream_call(client: httpx.Client, base: str, model: str, messages, max_tokens: int) -> dict:
    body = {
        "model": model,
        "messages": messages,
        "stream": True,
        "stream_options": {"include_usage": True},
        "tools": TOOLS,
        "tool_choice": "auto",
        "temperature": 0,
        "max_tokens": max_tokens,
    }
    started = time.perf_counter()
    first = None
    usage: dict[str, Any] = {}
    calls: dict[int, dict[str, Any]] = {}
    with client.stream("POST", f"{base}/chat/completions", json=body, timeout=600) as resp:
        resp.raise_for_status()
        for line in resp.iter_lines():
            if not line.startswith("data:") or line.strip() == "data: [DONE]":
                continue
            chunk = json.loads(line[5:])
            if chunk.get("usage"):
                usage = chunk["usage"]
            for choice in chunk.get("choices") or []:
                delta = choice.get("delta") or {}
                if first is None and (
                    delta.get("content")
                    or delta.get("reasoning_content")
                    or delta.get("reasoning")
                    or delta.get("tool_calls")
                ):
                    first = time.perf_counter() - started
                for frag in delta.get("tool_calls") or []:
                    acc = calls.setdefault(frag.get("index", 0), {"name": "", "arguments": ""})
                    fn = frag.get("function") or {}
                    acc["name"] += fn.get("name") or ""
                    acc["arguments"] += fn.get("arguments") or ""
    details = usage.get("prompt_tokens_details") or {}
    return {
        "prompt_tokens": usage.get("prompt_tokens"),
        "cached_tokens": details.get("cached_tokens"),
        "ttft_s": round(first, 3) if first is not None else None,
        "total_s": round(time.perf_counter() - started, 3),
        "tool_calls": list(calls.values()),
    }


def run_agent(client: httpx.Client, base: str, model: str, question: str, max_tokens: int) -> list:
    messages: list[dict[str, Any]] = [
        {"role": "system", "content": SYSTEM},
        {"role": "user", "content": question},
    ]
    steps = []
    for round_index in range(3):
        step = stream_call(client, base, model, messages, max_tokens)
        steps.append(
            {"step": round_index + 1, **{k: v for k, v in step.items() if k != "tool_calls"}}
        )
        if round_index == 2:
            break
        chosen = step["tool_calls"][:1]
        if chosen and chosen[0]["name"] in {t["function"]["name"] for t in TOOLS}:
            try:
                args = json.loads(chosen[0]["arguments"] or "{}")
            except ValueError:
                args = {}
            name = chosen[0]["name"]
        else:
            name, args = FALLBACK_CALLS[round_index]
        call = ToolCall(id=f"call_{round_index}", name=name, arguments=args)
        messages.append(assistant_tool_calls_message([call]))
        messages.append(tool_result_message(call, ActivityOutcome(ok=True, value=TOOL_RESULT)))
    return steps


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--base", required=True, help="OpenAI-compatible base URL, ending in /v1")
    parser.add_argument("--model", required=True)
    parser.add_argument("--max-tokens", type=int, default=48)
    parser.add_argument("--label", default="")
    parser.add_argument("--out")
    args = parser.parse_args()

    with httpx.Client() as client:
        runs = []
        for run_index, question in enumerate(QUESTIONS, start=1):
            for step in run_agent(client, args.base, args.model, question, args.max_tokens):
                row = {"run": run_index, **step}
                runs.append(row)
                print(json.dumps(row))
    if args.out:
        Path(args.out).write_text(
            json.dumps(
                {"label": args.label, "base": args.base, "model": args.model, "calls": runs},
                indent=2,
            )
        )


if __name__ == "__main__":
    main()
