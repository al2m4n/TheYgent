"""Traffic census over capture-proxy records: what Claude Code sends, by request class, with the
token profile and an API-list-price cost estimate, plus the auxiliary-request fingerprints the
routing allowlist will be built from.

    uv run --package theygent-ai-gateway python apps/ai-gateway/spike/census.py \
        ~/.theygent/ai-gateway-spike/captures [--previews] [--out census.md]

Cost figures are estimates at list price from a pricing table embedded below (per million tokens,
from Anthropic's published table as of 2026-09-25) — verify current rates before quoting them. On a
subscription they are an API-equivalent value, not a bill. ``--previews`` adds the first characters
of each auxiliary fingerprint's system prompt (client-authored text) from stored bodies.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import statistics
import sys
from collections import Counter, defaultdict
from collections.abc import Iterable
from pathlib import Path
from typing import Any

# $ per million tokens: input, output, 5-minute cache write, 1-hour cache write, cache read.
PRICING: dict[str, tuple[float, float, float, float, float]] = {
    "claude-fable-5-1": (10.0, 50.0, 12.5, 20.0, 0.25),
    "claude-opus-5-5": (4.0, 20.0, 5.0, 8.0, 0.20),
    "claude-opus-5": (5.0, 25.0, 6.25, 10.0, 0.50),
    "claude-sonnet-5-5": (2.0, 10.0, 2.5, 4.0, 0.20),
    "claude-sonnet-5": (2.0, 10.0, 2.5, 4.0, 0.20),
    "claude-haiku-4-5": (1.0, 5.0, 1.25, 2.0, 0.10),
}
FAMILY_FALLBACK = {
    "fable": "claude-fable-5-1",
    "opus": "claude-opus-5-5",
    "sonnet": "claude-sonnet-5-5",
    "haiku": "claude-haiku-4-5",
}


def price_for(model: str | None) -> tuple[float, float, float, float, float] | None:
    if not model:
        return None
    base = model.split("[")[0]
    if base in PRICING:
        return PRICING[base]
    for family, fallback in FAMILY_FALLBACK.items():
        if family in base:
            return PRICING[fallback]
    return None


def cost(model: str | None, usage: dict[str, Any]) -> float | None:
    rates = price_for(model)
    if rates is None or not usage:
        return None
    rin, rout, rcw5, rcw1, rcr = rates
    creation = usage.get("cache_creation") or {}
    cw_total = usage.get("cache_creation_input_tokens") or 0
    cw_1h = creation.get("ephemeral_1h_input_tokens") or 0
    cw_5m = creation.get("ephemeral_5m_input_tokens", cw_total - cw_1h) or 0
    return (
        (usage.get("input_tokens") or 0) * rin
        + (usage.get("output_tokens") or 0) * rout
        + cw_5m * rcw5
        + cw_1h * rcw1
        + (usage.get("cache_read_input_tokens") or 0) * rcr
    ) / 1_000_000


def load(root: Path) -> list[dict[str, Any]]:
    records = []
    for path in sorted(root.rglob("*.json")):
        try:
            records.append(json.loads(path.read_text()))
        except (OSError, ValueError):
            print(f"skipping unreadable capture {path}", file=sys.stderr)
    return records


def median(values: Iterable[float | int | None]) -> float | None:
    clean = [v for v in values if v is not None]
    return statistics.median(clean) if clean else None


def fmt(value: Any, digits: int = 0) -> str:
    if value is None:
        return "-"
    if isinstance(value, float):
        return f"{value:,.{digits}f}"
    if isinstance(value, int):
        return f"{value:,}"
    return str(value)


def table(headers: list[str], rows: list[list[Any]]) -> str:
    lines = ["| " + " | ".join(headers) + " |", "|" + "---|" * len(headers)]
    lines += [
        "| " + " | ".join(fmt(c) if not isinstance(c, str) else c for c in r) + " |" for r in rows
    ]
    return "\n".join(lines)


def fingerprint(summary: dict[str, Any]) -> str:
    key = "|".join(
        str(summary.get(k)) for k in ("system_hash_tail", "tools_hash", "output_format_hash")
    )
    if summary.get("system_hash_tail") is None:
        key += "|" + str(summary.get("system_hash_all"))
    return hashlib.sha256(key.encode()).hexdigest()[:10]


def system_preview(record: dict[str, Any], chars: int = 160) -> str:
    body = record.get("request_body")
    if not body:
        return "(body not stored)"
    try:
        system = json.loads(body).get("system")
    except ValueError:
        return "(unparsable)"
    blocks = [system] if isinstance(system, str) else (system or [])
    texts = [b if isinstance(b, str) else (b.get("text") or "") for b in blocks]
    tail = (
        texts[1:] if len(texts) > 1 and texts[0].startswith("x-anthropic-billing-header") else texts
    )
    return " ".join(" ".join(tail).split())[:chars].replace("|", "\\|") or "(empty)"


def report(records: list[dict[str, Any]], previews: bool) -> str:
    out: list[str] = []
    messages = [r for r in records if r.get("path", "").rstrip("/").endswith("/v1/messages")]
    sessions = {r["hints"].get("x-claude-code-session-id") for r in records if r.get("hints")}
    out.append("# Claude Code traffic census\n")
    started = sorted(r["started"] for r in records if r.get("started"))
    out.append(
        f"{len(records)} captured requests ({len(messages)} to /v1/messages) across "
        f"{len(sessions - {None})} sessions, {started[0] if started else '-'} → "
        f"{started[-1] if started else '-'}.\n"
    )

    out.append("## Endpoints and status\n")
    by_path = Counter((r.get("method"), r.get("path"), r.get("status")) for r in records)
    out.append(
        table(["method", "path", "status", "count"], [[*k, v] for k, v in by_path.most_common()])
    )

    out.append("\n\n## Client, credential and identity headers\n")
    agents = Counter(
        r["request_headers"].get("user-agent") for r in records if r.get("request_headers")
    )
    creds = Counter(
        (
            r["request_headers"].get("authorization")
            or r["request_headers"].get("x-api-key")
            or "none"
        ).split(" len=")[0]
        for r in records
        if r.get("request_headers")
    )
    custom = sum(1 for r in records if "x-theygent-gateway-key" in (r.get("request_headers") or {}))
    out.append(table(["user-agent", "count"], [[k or "-", v] for k, v in agents.most_common()]))
    out.append("\n\n" + table(["credential", "count"], [[k, v] for k, v in creds.most_common()]))
    out.append(f"\n\n`x-theygent-gateway-key` present on {custom}/{len(records)} requests.\n")

    out.append("\n## Hint-header coverage (/v1/messages)\n")
    names = sorted({k for r in messages for k in (r.get("hints") or {})})
    rows = [
        [n, sum(1 for r in messages if n in (r.get("hints") or {})), len(messages)] for n in names
    ]
    out.append(table(["header", "present", "of"], rows))
    for name in (
        "x-claude-code-request-class",
        "x-claude-code-agent-type",
        "x-claude-code-compaction",
        "x-claude-code-context-compacted",
    ):
        values = Counter((r.get("hints") or {}).get(name) for r in messages)
        out.append(
            f"\n\n`{name}`: " + ", ".join(f"{k or '∅'}={v}" for k, v in values.most_common())
        )

    betas = Counter(
        b.strip()
        for r in messages
        for b in ((r.get("request_headers") or {}).get("anthropic-beta") or "").split(",")
        if b.strip()
    )
    out.append("\n\n## anthropic-beta values\n")
    out.append(table(["value", "requests"], [[k, v] for k, v in betas.most_common()]))

    out.append("\n\n## Token profile by class (/v1/messages, status 200)\n")
    groups: dict[tuple[str, str, str], list[dict[str, Any]]] = defaultdict(list)
    for r in messages:
        if r.get("status") != 200:
            continue
        summary = r.get("request_summary") or {}
        sent_model = (
            (r.get("rewrite") or {}).get("to")
            if (r.get("rewrite") or {}).get("applied")
            else summary.get("model")
        )
        groups[
            (r.get("request_class") or "unlabeled", r.get("agent_type") or "-", sent_model or "-")
        ].append(r)
    rows, total_cost = [], 0.0
    for (klass, agent, model), items in sorted(groups.items()):
        usages = [((i.get("response_summary") or {}).get("usage") or {}) for i in items]
        served = (items[0].get("response_summary") or {}).get("model") or model
        costs = [cost(served, u) for u in usages]
        group_cost = sum(c for c in costs if c is not None)
        total_cost += group_cost
        rows.append(
            [
                klass,
                agent,
                model,
                len(items),
                sum(u.get("input_tokens") or 0 for u in usages),
                sum(u.get("cache_read_input_tokens") or 0 for u in usages),
                sum(u.get("cache_creation_input_tokens") or 0 for u in usages),
                sum(u.get("output_tokens") or 0 for u in usages),
                f"${group_cost:.4f}",
                fmt(median(i.get("first_byte_ms") for i in items)),
                fmt(median(i.get("total_ms") for i in items)),
            ]
        )
    out.append(
        table(
            [
                "class",
                "agent type",
                "model",
                "reqs",
                "input",
                "cache read",
                "cache write",
                "output",
                "est. $ (list)",
                "p50 first byte ms",
                "p50 total ms",
            ],
            rows,
        )
    )
    out.append(f"\n\nEstimated total at list price: **${total_cost:.4f}**.\n")

    out.append("\n## Auxiliary fingerprints\n")
    main_prefixes = defaultdict(set)
    for r in messages:
        if r.get("request_class") == "main":
            main_prefixes[(r.get("hints") or {}).get("x-claude-code-session-id")].add(
                (r.get("request_summary") or {}).get("msg0_hash")
            )
    fps: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for r in messages:
        if r.get("request_class") == "auxiliary" and r.get("request_summary"):
            fps[fingerprint(r["request_summary"])].append(r)
    rows = []
    for fp, items in sorted(fps.items(), key=lambda kv: -len(kv[1])):
        s0 = items[0]["request_summary"]
        shares = sum(
            1
            for i in items
            if i["request_summary"].get("msg0_hash")
            in main_prefixes.get((i.get("hints") or {}).get("x-claude-code-session-id"), set())
        )
        usages = [((i.get("response_summary") or {}).get("usage") or {}) for i in items]
        row = [
            fp,
            len(items),
            ", ".join(sorted({str(i["request_summary"].get("model")) for i in items})),
            ", ".join(sorted({str(i["request_summary"].get("max_tokens")) for i in items})),
            s0.get("tool_count"),
            ", ".join(s0.get("output_format_keys") or []) or "-",
            ", ".join(sorted({json.dumps(i["request_summary"].get("thinking")) for i in items})),
            f"{shares}/{len(items)}",
            fmt(
                median(
                    (u.get("input_tokens") or 0)
                    + (u.get("cache_read_input_tokens") or 0)
                    + (u.get("cache_creation_input_tokens") or 0)
                    for u in usages
                )
            ),
            fmt(median(u.get("output_tokens") for u in usages)),
            ", ".join(sorted({str(i.get("status")) for i in items})),
        ]
        if previews:
            row.append(system_preview(items[0]))
        rows.append(row)
    headers = [
        "fingerprint",
        "reqs",
        "models",
        "max_tokens",
        "tools",
        "schema keys",
        "thinking",
        "shares main prefix",
        "p50 input",
        "p50 output",
        "status",
    ]
    out.append(
        table(headers + (["system preview"] if previews else []), rows) if rows else "(none)"
    )

    out.append("\n\n## Response headers\n")
    resp_names = Counter(k for r in records for k in (r.get("response_headers") or {}))
    out.append(table(["header", "responses"], [[k, v] for k, v in sorted(resp_names.items())]))
    limits = defaultdict(Counter)
    for r in records:
        for k, v in (r.get("response_headers") or {}).items():
            if k.startswith("anthropic-ratelimit"):
                limits[k][v] += 1
    if limits:
        out.append("\n\nRate-limit header values (latest few):\n")
        out.append(
            table(
                ["header", "values"],
                [[k, ", ".join(list(c)[-4:])] for k, c in sorted(limits.items())],
            )
        )

    rewrites = [r for r in messages if r.get("rewrite")]
    out.append("\n\n## Rewrite experiments\n")
    if rewrites:
        out.append(
            table(
                ["class", "from", "to", "applied", "status", "served model", "stop", "error"],
                [
                    [
                        r.get("request_class"),
                        r["rewrite"].get("from"),
                        r["rewrite"].get("to"),
                        str(r["rewrite"].get("applied")),
                        r.get("status"),
                        (r.get("response_summary") or {}).get("model"),
                        (r.get("response_summary") or {}).get("stop_reason"),
                        json.dumps((r.get("response_summary") or {}).get("error"))[:120],
                    ]
                    for r in rewrites
                ],
            )
        )
    else:
        out.append("(none)")

    out.append("\n\n## Streaming\n")
    sse = [r for r in messages if (r.get("response_summary") or {}).get("event_counts")]
    pings = sum((r["response_summary"]["event_counts"].get("ping") or 0) for r in sse)
    gaps = [g for g in (r["response_summary"].get("max_event_gap_ms") for r in sse) if g]
    outcomes = Counter(r.get("outcome") for r in messages)
    out.append(
        f"{len(sse)} streamed responses, {pings} pings, max silent gap {fmt(max(gaps or [0]))} ms; "
        "outcomes: " + ", ".join(f"{k}={v}" for k, v in outcomes.items())
    )

    errors = Counter(
        (r.get("status"), json.dumps((r.get("response_summary") or {}).get("error"))[:140])
        for r in records
        if (r.get("status") or 0) >= 400
    )
    out.append("\n\n## Errors\n")
    out.append(
        table(["status", "error", "count"], [[*k, v] for k, v in errors.most_common()])
        if errors
        else "(none)"
    )
    return "\n".join(out) + "\n"


def main() -> None:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("captures", type=Path)
    parser.add_argument("--previews", action="store_true")
    parser.add_argument("--out", type=Path)
    args = parser.parse_args()
    text = report(load(args.captures.expanduser()), args.previews)
    if args.out:
        args.out.write_text(text)
        print(f"wrote {args.out}")
    else:
        print(text)


if __name__ == "__main__":
    main()
