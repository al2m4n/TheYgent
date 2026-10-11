# AI Gateway

The AI Gateway sits between **Claude Code** — the CLI, IDE extensions, and the desktop app — and
the Anthropic API. It records what each exchange cost, what it would have cost if subagents and
cold-cache turns had run on a cheaper model, and why each request was or wasn't a candidate — and,
once you choose to, it **routes** them there: to a cheaper Anthropic model, or to
[models you run yourself](local-models.md). You see where your team's model spend goes, per day,
model, developer and session, before you move anything.

!!! note "Observe first, then route"
    In **observer** mode every request reaches Anthropic exactly as Claude Code sent it; the
    gateway reports what routing *would* save. When those figures convince you, an admin switches
    to **router** mode, and the saving becomes real: a subagent asking for Opus 5.5 runs on
    Sonnet 5.5, for example. Only the model changes — never the prompt — and a model that refuses
    a request hands it back to the one Claude Code asked for before Claude Code notices. The one
    setting the setup adds that changes Claude Code's own behavior, [tool search](setup.md#why-each-setting),
    turns back on what a custom gateway address otherwise turns off.

## What you get

- **Spend and what routing would save — or saved** — per day, request class (main
  conversation, subagents, compaction, side requests), model, client, developer and session. See
  [Reports & policy](reports.md).
- **Session timelines** — every request of a Claude Code session in order, with what the
  policy would have done and the reasons, in plain words.
- **Setup warnings** — for example, Claude Code sending every tool's definition with every
  request because tool search is off, which can multiply input tokens several times.
- **Subscription seats** — for developers on a claude.ai plan, how much of the plan's
  five-hour and weekly allowance their traffic used.
- **Local models** — a share of the work, per kind of request, answered by models on your
  inference plane at no API spend, each measured on your own traffic first. See
  [Local models](local-models.md).

## What it sees, and what leaves it

The gateway relays prompts, code, model responses and credentials, so it runs **where you run
it** — your machine or your cluster — in the same trust domain as Claude Code. It never writes a
prompt or a response anywhere, and never stores a credential except an organization key you give
it.

What it sends to the control plane is **metadata only**: timings, model names, token counts and
costs, the routing decision and its reasons, Claude Code's session and agent ids, and the kind of
each request. Never prompt or response text. Everyone sees their own records in full; admins see
everyone's; other roles see organization totals without individual detail.

## Personal or team

| | Personal | Team |
|---|---|---|
| Who it serves | You, on your own machine | Every developer who points Claude Code at it |
| Listens on | Loopback only (`127.0.0.1`) | Any address you choose |
| Identifies callers by | Nobody — it is yours | Each developer's **gateway key** (`tyg_…`) |
| Billing | Your own Claude login or API key | Each developer's own credential, or the organization's key |
| Records | Kept locally; read them with the `report` command, or attach it to the control plane | Sent to the control plane: the **AI Gateway** page |

[Running the gateway](running.md) covers both, on a machine, in Docker Compose, and in
Kubernetes.

## Observer, router and passthrough

An admin chooses the gateway's **mode** on the AI Gateway **Settings** tab:

- **Observer** records every exchange — its cost and what routing would have done — without
  changing it.
- **Router** records every exchange and applies the routing decisions: routed requests run on the
  target model — a cheaper Anthropic model with the rest of the request exactly as Claude Code
  sent it, or a [local model](local-models.md). A target that refuses
  a request sends it back to the requested model, unseen, and that conversation stays there until
  its cache goes cold. Switching back to observer stops routing within one heartbeat (30 seconds).
- **Passthrough** forwards traffic without recording anything.

A gateway attached to the control plane forwards in passthrough until an admin saves a
configuration: your organization's traffic is recorded only once someone chose to record it. A
personal gateway on its own records from the start.

## Try it on your own machine

A personal gateway needs no keys and no setup beyond Claude Code's settings:

1. **Start TheYgent** with `make up` ([Running TheYgent](../getting-started/running.md)). The
   gateway listens on `127.0.0.1:8082`.
2. **Point Claude Code at it**, in the shell you start Claude Code from:

    ```bash
    export ANTHROPIC_BASE_URL='http://localhost:8082'
    export CLAUDE_CODE_GATEWAY_HINT_HEADERS='1'
    export ENABLE_TOOL_SEARCH='true'
    ```

3. **Work as usual.** Your own Claude login or API key goes through the gateway unchanged.
4. **Read the report** — what your traffic cost, and what routing would save:

    ```bash
    uv run --package theygent-ai-gateway theygent-ai-gateway report --since 24h
    ```

5. **To see it on the AI Gateway page, and to route**, attach the gateway to the control plane
   with a service key ([Running the gateway](running.md#attach-it-to-the-control-plane)). Then
   save a configuration on **AI Gateway → Settings**: observer mode to record, router mode to
   route.

## Getting started with a team

1. **Run a gateway** — [Running the gateway](running.md).
2. **Turn recording on** — an admin opens **AI Gateway → Settings** and saves the configuration
   with **Observer** selected.
3. **Set up Claude Code** — each developer opens **AI Gateway → Setup & keys**, creates a gateway
   key, and copies the setup for their terminal, IDE or desktop app —
   [Set up Claude Code](setup.md).
4. **Work as usual** — then watch **Overview**, **Report** and **Sessions**.
5. **Route** — when the Report's saving is one you want, an admin selects **Router** on the
   Settings tab (with a subagent target such as `claude-sonnet-5-5`). The Report then shows what
   routing *saved*, against what the same traffic would have cost on the models Claude Code asked
   for.

## The AI Gateway page

| Tab | Who | What it shows |
|---|---|---|
| **Overview** | everyone | The mode, the configuration version, each gateway instance's health (online, running the latest configuration, upstream reachable, records waiting to export) |
| **Report** | everyone | Spend, what it would have cost with routing, the saving, spend by day, breakdowns, seats, setup warnings |
| **Sessions** | everyone | Claude Code sessions, most recently active first, and each one's timeline of requests and decisions |
| **Setup & keys** | everyone | How you are billed, your gateway keys, and copy-ready setups per client |
| **Catalog** | admins | Kinds of side requests Claude Code sends (titles, suggestions, checks) and what routing may do with each |
| **Settings** | admins | The configuration and its history, the organization's Anthropic key, developers' billing, service keys |

## Related pages

- [Set up Claude Code](setup.md) — the settings, per client, and why each one is there.
- [Reports & policy](reports.md) — reading the figures, and how the policy decides.
- [Local models](local-models.md) — sending work to models you run, measured first.
- [Running the gateway](running.md) — run modes, the organization's key, errors.
- [Environment variables](../reference/environment.md#ai-gateway) — the gateway's settings.
