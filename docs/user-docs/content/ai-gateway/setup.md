# Set up Claude Code

Pointing Claude Code at the AI Gateway is a handful of environment settings. The **Setup & keys**
tab on the AI Gateway page builds them for you — with your key filled in, right after you create
it — so this page explains what they are and why each one is there.

## Before you start

- **The gateway's address.** By default `http://localhost:8082`. If your team runs a shared
  gateway, use its address; the Setup & keys tab lets you set the address its snippets use.
- **A gateway key** (team gateways only). Create one on **Setup & keys → New key**. It is shown
  **once**, in the dialog that creates it, together with setups that already contain it. A
  gateway key identifies you to the gateway and opens nothing else in TheYgent; revoke it there
  when a machine is retired.
- **How you are billed.** An admin sets it per developer (**Settings → Developers**):

    | Billing | Your traffic reaches Anthropic with | Your gateway key travels as |
    |---|---|---|
    | **Your own Claude login or API key** | your claude.ai subscription or your own API key, as today | a header of its own (`X-TheYgent-Gateway-Key`) |
    | **The organization's key** | the organization's Anthropic key, which the gateway adds in place of yours | your only credential (`ANTHROPIC_AUTH_TOKEN`) |

    The Setup & keys tab shows which one applies to you and builds the matching setup.

## Terminal and IDEs

For the CLI — and IDE extensions launched from a terminal that has these set — export them in
your shell profile. Billed to your own credential:

```bash
export ANTHROPIC_BASE_URL='http://localhost:8082'
export ANTHROPIC_CUSTOM_HEADERS='X-TheYgent-Gateway-Key: tyg_…your gateway key'
export CLAUDE_CODE_GATEWAY_HINT_HEADERS='1'
export ENABLE_TOOL_SEARCH='true'
```

Billed to the organization's key, the gateway key *is* your credential:

```bash
export ANTHROPIC_BASE_URL='http://localhost:8082'
export ANTHROPIC_AUTH_TOKEN='tyg_…your gateway key'
export CLAUDE_CODE_GATEWAY_HINT_HEADERS='1'
export ENABLE_TOOL_SEARCH='true'
```

On a **personal** gateway there is no key: set `ANTHROPIC_BASE_URL` and the last two lines.

## `settings.json`

The same settings work as the `env` block of `~/.claude/settings.json`, which every Claude Code
session reads, however it was started:

```json
{
  "env": {
    "ANTHROPIC_BASE_URL": "http://localhost:8082",
    "ANTHROPIC_CUSTOM_HEADERS": "X-TheYgent-Gateway-Key: tyg_…your gateway key",
    "CLAUDE_CODE_GATEWAY_HINT_HEADERS": "1",
    "ENABLE_TOOL_SEARCH": "true"
  }
}
```

## The desktop app

The desktop app ignores `ANTHROPIC_BASE_URL` and `settings.json`; it reaches a gateway only
through its own third-party inference settings, which carry an API key and never a claude.ai
login. So it works through the gateway **for developers billed to the organization's key**:

| Setting | Value |
|---|---|
| Inference provider | Gateway |
| Gateway base URL | the gateway's address |
| API key | your gateway key |
| Tool search | On |

Developers on their own claude.ai login keep using the CLI and IDEs through the gateway.

## For admins: managed settings

The **Managed settings** view on Setup & keys gives the `env` block for everyone at once — the
gateway's address and the two flags, **never a key**; each developer adds their own. Distribute
it the way you distribute Claude Code's managed settings today.

## Why each setting

- **`ANTHROPIC_BASE_URL`** sends Claude Code's model traffic to the gateway instead of straight
  to Anthropic.
- **`CLAUDE_CODE_GATEWAY_HINT_HEADERS=1`** makes Claude Code label each request: the main
  conversation, a subagent, compaction, a side request, and the session and agent it belongs
  to. Without the labels the gateway cannot tell those apart, so no request is a routing
  candidate. The Report warns when they are missing.
- **`ENABLE_TOOL_SEARCH=true`** keeps MCP tool search on. Claude Code turns it **off for any
  custom base URL**, and then every request carries the full definition of every connected tool.
  In one measurement with many connectors loaded, a one-line prompt went from about 28,000 to
  184,000 input tokens — 6.5 times as many. The Report flags this as *tool search is off*.

## Claude Code's own cost display

Claude Code prices every token at the model it **asked for**. In observer mode that is also the
model that answered, so Claude Code's figures and the gateway's agree (at list prices — the
gateway also applies any contracted rates an admin entered). In **router** mode, routed requests
run on a cheaper model, but Claude Code still prices them at the model it asked for: its own cost
display and usage summary overstate spend. The AI Gateway **Report**, which records the model that
actually served each request, is the figure to trust. On a subscription, dollar figures are the
API-equivalent value of the traffic, not a bill — the Report shows plan usage separately.

## Check that it works

1. Run any Claude Code prompt.
2. On **AI Gateway → Sessions**, your session appears with its requests, once the gateway is in
   observer mode (records reach the page within a few seconds).
3. If Claude Code reports an error from the gateway instead, the message names what is missing
   — see [errors the gateway returns](running.md#errors-the-gateway-returns).

## Related pages

- [AI Gateway](index.md) — what the gateway does and what leaves it.
- [Reports & policy](reports.md) — what the figures mean.
- [Running the gateway](running.md) — starting one, and its errors.
