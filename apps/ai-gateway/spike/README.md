# AI gateway spike: Claude Code traffic census

Throwaway tooling that answers the questions the gateway design rests on, from real Claude Code
traffic, before any of the real gateway is built. Findings accumulate in [FINDINGS.md](FINDINGS.md).

| Script | What it does |
|---|---|
| `capture_proxy.py` | Byte-faithful passthrough to `api.anthropic.com` that records every exchange (credentials redacted) |
| `census.py` | Turns captures into a report: classes, hint headers, token profile, auxiliary fingerprints, rate-limit headers |
| `local_replay.py` | Replays captured auxiliary requests against a local model through the inference plane |
| `make_fixtures.py` | Rebuilds selected captures as the content-free fixtures in `tests/fixtures/exchanges/` |

## What gets recorded, and where

Captures go to `~/.theygent/ai-gateway-spike/captures` (outside the repository; directory `0700`,
files `0600`). They hold:

- **Request headers.** Credential headers are reduced to their kind and length, and any credential
  value that appears anywhere else in the record is scrubbed too.
- **A structural summary of each request body:** model, tool names, hashes of the system prompt and
  tool list, thinking and effort settings, message count, cache TTLs.
- **The response.** The event timeline, usage, stop reason and response headers.
- **Full bodies,** for auxiliary requests only by default (`--bodies auxiliary`). These contain
  prompt and code content.
  - `--bodies none` keeps only summaries.
  - `--bodies all` also keeps main-conversation bodies. These are large, since each main request
    carries the whole context.

Delete the directory when the spike is over: `rm -rf ~/.theygent/ai-gateway-spike`.

## Run it

Start the proxy (it binds to loopback only):

```bash
uv run --package theygent-ai-gateway python apps/ai-gateway/spike/capture_proxy.py
```

Then point Claude Code at it. Opt in **one project at a time** by putting this in that project's
`.claude/settings.local.json`:

```json
{"env": {"ANTHROPIC_BASE_URL": "http://127.0.0.1:8082", "CLAUDE_CODE_GATEWAY_HINT_HEADERS": "1", "ENABLE_TOOL_SEARCH": "true"}}
```

`ENABLE_TOOL_SEARCH` matters. Claude Code turns MCP tool search off for any custom base URL, and
without it every request carries every connector's tool definitions: 6.5× the tokens in the spike.

Keep the proxy running while you use that project, because Claude Code in that project fails while
the proxy is down. Remove the block to go direct again.

To check that the developer-identity header survives next to a claude.ai login, add
`"ANTHROPIC_CUSTOM_HEADERS": "X-TheYgent-Gateway-Key: tyg_spike_probe"` to the same `env` block.

### Desktop app check

With the block above in a project, start a **new** desktop-app session in that folder and send one
prompt. Then run the census. If a request shows up, the desktop app honours the project's `env`
block.

## Report

```bash
uv run --package theygent-ai-gateway python apps/ai-gateway/spike/census.py ~/.theygent/ai-gateway-spike/captures --out /tmp/census.md
```

Add `--previews` to see the first characters of each auxiliary fingerprint's system prompt.

## Experiments

`--rewrite CLASS[:AGENT_TYPE]=MODEL` sends matching requests to another model. Only the top-level
`model` value changes; the edit is verified by re-parsing, and any doubt forwards the original.

```bash
uv run --package theygent-ai-gateway python apps/ai-gateway/spike/capture_proxy.py --rewrite auxiliary=claude-haiku-4-5
```

Run experiments on throwaway sessions, not on real work.

## Local replay

This needs a chat model registered on the inference plane, for example as logical id `aux-local`:

```bash
uv run --package theygent-inference-plane python apps/ai-gateway/spike/local_replay.py ~/.theygent/ai-gateway-spike/captures --model aux-local
```
