# Reports & policy

What the AI Gateway page's figures mean, how the routing policy decides what moves, and how admins
tune it. In observer mode the gateway only records each decision; in router mode it also applies
it — changing nothing but the model.

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
served it, the routing decision or its reason, the model a shadow run ran on — and, where you may
see individuals, by developer or session. Grouping by requested model matches Claude Code's own usage summary, which files every
token under the model it asked for.

**Shadow runs.** A saving is an estimate: it assumes the target would have written as much as
the model Claude Code asked for. Shadow runs check it. After a request that routing would have sent
elsewhere, the gateway sends a copy to that target; after a request in a local dial's *trial* (see
Settings below), it sends a copy to the local model. Claude Code never sees the copy, and it never
delays Claude Code's answer. The **Shadow runs** table shows, per model, how many copies ran,
how many of their answers routing could have used, and how much the target wrote compared with
the model Claude Code asked for. For agent turns (subagents and the main conversation) it also
shows how often the target picked the same next action as the real reply: the same tool first, or
the answer where the reply answered. That is the measure to watch before trusting a local model
with agent work. It also shows what the copies cost, which is kept apart from the spend above. The
Sessions timeline shows the shadow run under each request that had one, a trial named as such.

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
| target not cheaper | The target doesn't cost less than the model Claude Code asked for |
| unreviewed kind | A side request whose kind an admin has not reviewed in the Catalog |
| local share | The conversation is in the share its kind sends to a [local model](local-models.md) |
| no local model fits | In the local share, but no listed local model can take this request (tool calls, images or prompt size) |
| target refused | The target, or the local model, could not take the request: it went to the model Claude Code asked for, unseen |

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

Some traffic never moves: Claude Code's quota check, side requests that ride the main
conversation's cache, auto mode's safety checks, and side requests of a kind nobody has reviewed.
Compaction reads the main conversation's cache, so it moves only with the conversation: it runs
on the model the conversation is on — the one asked for, a cheaper one, or a local one. And a target must accept the request: Sonnet 5.5
takes Claude Code's requests to Opus 5.5 as they are; Haiku 4.5 rejects them, so a move to it is
turned down as *target can't take it*. A target must also cost less, at your contracted prices
where you set them. A request that already asks for a cheaper model, such as a subagent on
Haiku 4.5, or for a model with no known price, stays as *target not cheaper*.

**Estimates.** A "with routing" figure assumes the target model would have used the same number
of tokens. Models differ in how much they write, so treat savings as estimates; the actual
figures are exact.

**Archived days.** Requests older than the retention period (90 days by default) are archived:
the Report reads those days from daily totals, so every figure and grouping stays the same. Its
footer says from which day that applies. Setup warnings, seat readings and the prices used cover
only the days still kept in detail. A Sessions timeline lists only the requests still kept in
detail, and its totals still count every request.

**Prices.** Figures use Anthropic's published list prices. The Report's footer names the date
they were read (*Prices: 2026-10-11*), and TheYgent checks for changes every week. Requests in
Claude Code's fast mode are priced at fast-mode rates. A model priced by prompt length, such as
Haiku 5.5, is priced at its higher rates for a long prompt. Where your organization pays other
rates, an admin enters them under **Settings → Contracted prices**.

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

In router mode, a *local model* side request goes to a model on your inference plane, the
**Side requests** dial's (see [Local models](local-models.md)). Claude Code gets the local
model's answer in the form the Anthropic API would have given it, but only after the gateway has
checked it: for a session title, JSON with a `title`. A request the local model cannot take goes to
the model Claude Code asked for, marked *target refused*, and Claude Code never sees the
difference. That covers a request carrying something that doesn't translate (a document, for
example), an error, no answer in time, an answer of the wrong shape, or no inference plane
running. A local answer costs no API spend; its *without routing* figure is an estimate, counted
with the local model's tokens.

## Settings (admins)

- **Mode** — observer, router or passthrough (see
  [AI Gateway](index.md#observer-router-and-passthrough)). Saving refuses a target no Claude Code
  request could go to — a model the gateway has no data for, or one that rejects Claude Code's
  requests, such as Haiku 4.5.
- **Routing targets** — the Anthropic model a subagent could start on (for example
  `claude-sonnet-5-5`), the model for the main conversation at cold points (empty: never
  considered), and the local model for side requests the Catalog allows to run locally (a
  [logical model id](../concepts/models-and-engines.md)).
- **Local models** — the models on your inference plane that may take Claude Code's work, listed
  small to large, each with its context size, the most it may write in a turn, whether it follows
  tool calls and takes images, and optionally what it costs to run.
  Beside each one is what the last week shows: the requests it served, and how often it picked the
  same next action as Claude. Then one dial per kind of request (side requests, subagents, the
  main conversation), each a **share** served locally, a **trial** measured locally while
  Anthropic answers, and the **model** (or "smallest that fits"). How to set them, and their
  limits: [Local models](local-models.md).
- **Warm switch margin** — a conversation whose cache is warm moves only where staying would cost
  at least this many times rebuilding its cache on the other model (at least 1). Cache reads cost
  a fraction of cache writes, so in practice warm conversations stay put.
- **Shadow runs** — which targets get shadow runs (see the Report tab above).
    - *Local model* is on by default. It costs no API spend, only the local model's time. (A
      dial's trial is its own choice and runs whatever this says.)
    - *Cloud targets* are off by default, because each copy is a real API call. Cloud shadow runs
      use only the organization's key (on a personal gateway, your own API key), never a
      subscription seat's login or a developer's own key. They stop for the day at the **budget**
      (per gateway instance, counted from its last restart), and pause for ten minutes when the
      target's rate limit runs low.
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
- [Local models](local-models.md) — the local dials, trials and their limits.
- [Running the gateway](running.md) — the command-line report for a personal gateway.
