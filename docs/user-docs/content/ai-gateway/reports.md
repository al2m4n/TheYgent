# Reports & policy

What the AI Gateway page's figures mean, how the routing policy decides what *would* move, and
how admins tune it. Nothing here changes a request: the gateway records each decision and leaves
the traffic as Claude Code sent it.

## The Report tab

The report covers the last week unless you pick another range. Admins start on everyone's
traffic; everyone else on their own (and can switch to organization totals).

| Tile | Means |
|---|---|
| **Spend** | What the traffic cost, at list prices plus any contracted rates an admin entered. Once the gateway routes, its hint shows what the same traffic would have cost *without routing* — on the models Claude Code asked for |
| **With routing** | What it costs with the policy's decisions applied: a request the gateway routed costs what it cost, one it only would have routed costs the target's price, everything else as it ran |
| **Saving** | Without routing minus with routing: what routing saved (router mode) or would save (observer mode), and its share |
| **Calls** | API calls, how many were routed, how many would have been, and how many could have been answered by a local model instead |

Below them: spend by day (the full bar is what each day cost as requested, the filled part with
routing; the gap is the saving), and a
breakdown you can group by request class, client, the model Claude Code requested, the model that
served it, the routing decision or its reason — and, where you may see individuals, by developer
or session. Grouping by requested model matches Claude Code's own usage summary, which files every
token under the model it asked for.

**Subscription seats.** For developers on a claude.ai plan, the dollar figures are the
API-equivalent value of their traffic, not a bill. The seats table shows each developer's latest
reading of the plan's five-hour and weekly allowance and how much of it was used in the range.

**Setup warnings** appear above the figures when the traffic shows a problem, each with its fix:

| Warning | Fix |
|---|---|
| Tool search is off — every tool's definition went with every request | Set `ENABLE_TOOL_SEARCH=true` (desktop app: tool search on) |
| Claude Code is not labelling its requests | Set `CLAUDE_CODE_GATEWAY_HINT_HEADERS=1` |
| Unknown request classes | A newer Claude Code than this gateway knows: update TheYgent |
| No price known for a model | An admin adds its rates under **Settings → Contracted prices** |

## The Sessions tab

Sessions are listed most recently active first — yours, or everyone's for admins. Open one for its
timeline: each request in order with its offset from the session start, its class (main,
subagent, compaction, auxiliary), the agent type for subagents, the model requested and the one
that served it, tokens, cost, errors, and the decision — *stays*; *routed →* a target, with what that
saved against the requested model (router mode); or *would route →* a target, with what that would
have cost (observer mode). Every reason is a short label; hover it for the explanation. A few of
them:

| Reason | Means |
|---|---|
| subagent spawn | A subagent starts with an empty cache, so moving it costs nothing extra |
| cold cache | The main conversation's cache is cold (its start, after compaction, or expired) |
| mid-conversation | Inside one prompt's tool loop: a conversation never switches mid-turn |
| warm cache | Moving would throw away a warm cache and pay to rebuild it |
| follows cache | The conversation keeps the model its cache is on until the cache goes cold |
| switch pays | Staying would cost at least the configured margin times rebuilding the cache elsewhere |
| no target set | The configuration names no target for this kind of request |
| target can't take it | The target model would reject this request's shape |
| unreviewed kind | A side request whose kind an admin has not reviewed in the Catalog |

## How the policy decides

Prompt caches belong to one model, so moving a conversation mid-stream throws a warm cache away
and pays to rebuild it. The policy therefore only considers a move where the cache is cold anyway:

- **A subagent, when it starts.** Its cache is empty either way — the clearest saving. In
  measurements, the same exploration subagent cost about half as much on Sonnet 5.5 as on
  Opus 5.5.
- **The main conversation, at a cold point** — its start, after compaction, or after its cache
  expired — and only if the configuration names a main target.
- **Then it stays where its cache is.** Every later request — the tool loop, and new prompts while
  the cache is warm — follows the same decision until the next cold point (*follows cache*), so a
  conversation never changes model mid-turn, and a routed one isn't pulled back to the requested
  model either. A warm move happens only where staying would cost at least the **warm switch
  margin** times rebuilding the cache on the other model (*switch pays*). If Claude Code switches
  model itself, the policy decides afresh.

Some traffic never moves: compaction (it reads the main conversation's cache), Claude Code's
quota check, side requests that ride the main conversation's cache, auto mode's safety checks,
and side requests of a kind nobody has reviewed. And a target must accept the request: Sonnet 5.5
takes Claude Code's requests to Opus 5.5 as they are; Haiku 4.5 rejects them, so a move to it is
turned down as *target can't take it*.

**Estimates.** A "with routing" figure assumes the target model would have used the same number
of tokens. Models differ in how much they write, so treat savings as estimates; the actual
figures are exact.

## Catalog (admins)

Claude Code also sends small **side requests** — session titles, suggestions, checks. The Catalog
lists each kind it has seen, told apart by the shape of the request, with how often it was seen,
the Claude Code versions that sent it, and its status:

| Status | Routing may |
|---|---|
| unreviewed | do nothing — it passes through untouched until someone decides |
| observe only | record and report it, never route it |
| cheaper model | send it to the configuration's subagent target |
| local model | answer it with the configuration's local model |
| never route | do nothing — it always stays on the model Claude Code asked for |

Kinds the gateway recognises by their shape come with a **suggestion** — the quota check and
side requests riding the main conversation's cache: *never route*; the session title: *local
model* — which you can apply in one click or override with **Review**. A suggestion is only that:
the status is your decision, and saving one creates a new configuration version.

## Settings (admins)

- **Mode** — observer or passthrough (see [AI Gateway](index.md#observer-and-passthrough)).
- **Routing targets** — the Anthropic model a subagent could start on (for example
  `claude-sonnet-5-5`), the model for the main conversation at cold points (empty: never
  considered), and the local model for side requests the Catalog allows to run locally (a
  [logical model id](../concepts/models-and-engines.md)).
- **Warm switch margin** — a conversation whose cache is warm moves only where staying would cost
  at least this many times rebuilding its cache on the other model (at least 1). Cache reads cost
  a fraction of cache writes, so in practice warm conversations stay put.
- **Contracted prices** — per-model rates (USD per million tokens: input, output, 5-minute and
  1-hour cache writes, cache reads) where yours differ from list price.

Every save creates a new, immutable **version**; gateways pick it up at their next heartbeat
(every 30 seconds by default). The **History** lists every version; loading an older one starts
an edit from it, and saving makes it the newest. If someone else saved meanwhile, the page keeps
your edit and asks before replacing theirs — their Catalog reviews are kept either way.

The same tab holds the [organization's Anthropic key](running.md#the-organizations-key),
**Developers** (who pays for each developer's traffic) and the gateways' **service keys**
([Running the gateway](running.md#attach-it-to-the-control-plane)).

## Related pages

- [AI Gateway](index.md) — what the gateway does and what leaves it.
- [Set up Claude Code](setup.md) — the settings the warnings refer to.
- [Running the gateway](running.md) — the command-line report for a personal gateway.
