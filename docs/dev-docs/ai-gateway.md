# AI gateway

`apps/ai-gateway` is an Anthropic-Messages proxy that sits between a coding agent (Claude Code:
the CLI, IDE extensions, the desktop app) and its models. It records what every exchange cost
and what a routing policy would have done with it, so a team can see where its model spend goes
and what moving work to cheaper models would save — before anything is moved. It is the fourth
process type, on :8082.

Not to be confused with the inference plane's OpenAI-compatible data plane (`/v1/*`, reached
through `packages/gateway-client`): that serves TheYgent's own agents; this one serves a coding
agent's Anthropic-format traffic. The gateway calls it in one case only: in router mode, for the
side requests an admin allows to run on a local model (see *Router mode* below).

Rules for changing it live in [apps/ai-gateway/AGENTS.md](../../apps/ai-gateway/AGENTS.md); the
measurements behind them in [apps/ai-gateway/spike/FINDINGS.md](../../apps/ai-gateway/spike/FINDINGS.md).

## Design rules

- **It lives in the user's trust domain.** It sees prompts, code and credentials, and never
  persists a credential or sends content anywhere but upstream. What reaches the control plane is
  metadata in `theygent_ir.gateway`, whose types refuse free text (see
  [ir-and-packages.md](./ir-and-packages.md#the-ai-gateway-wire-types)).
- **Pass-through is exact.** Request bodies go upstream byte-for-byte, headers as an open list,
  responses (errors included) come back verbatim, streams are relayed unbuffered, and a client
  hang-up cancels the upstream call. Claude Code's prompt caching, preserved thinking, paired
  betas and retry logic all depend on it.
- **Only router mode changes a request, and only its model.** Observer mode records each decision
  with its reasons and counterfactual cost and applies none. Router mode applies them: a routed
  request's top-level `model` value is spliced into the original bytes, and nothing else changes.
- **Recording never blocks a request.** Observations go on a bounded queue; a full queue drops
  and counts, never waits.
- **Team mode fails closed** — on developer keys and on its admin surface alike.
- **Empty env means unset; a bad setting fails at startup naming its variable.**

## Two independent axes

| Axis | Values | Set by |
|------|--------|--------|
| Auth mode | `personal` — loopback only, no keys, the caller's own credential · `team` — developers authenticated by gateway keys from the control plane | `THEYGENT_AI_GATEWAY_AUTH_MODE` at startup |
| Traffic mode | `observer` — record every exchange with a decision · `passthrough` — forward without recording · `router` — record and apply the decisions | the gateway config (an admin, on the control plane) |

A standalone personal gateway (no control plane) has no one to configure it, so it observes from
the start with the default policy. A gateway attached to a control plane forwards in passthrough
until an admin saves a config: an organization's traffic is recorded only once someone chose to.

## Layout

| Module | Role |
|--------|------|
| `__main__.py` | `theygent-ai-gateway` (serve) and `theygent-ai-gateway report` |
| `settings.py` | Every env setting, validated on construction (loopback rule, team needs a control plane and a service key, the gateway never forwards to itself) |
| `app.py` | `create_app`: wiring, the lifespan (upstream client, key sync, config sync and heartbeat, exporter), `/healthz`, `/readyz`; every injectable seam the tests use |
| `proxy.py` | The forward path: team-mode identity check, exact relay, the gateway's own refusals in Anthropic's error shape with `x-theygent-gateway-error` |
| `identity.py` | Gateway keys: the three ways one arrives, SHA-256 comparison, the key set synced from the control plane and kept on disk |
| `signals.py` | What a request says without changing it: Claude Code's `x-claude-code-*` hint headers, client and credential kind, the body's structure, the auxiliary-request fingerprint |
| `observe.py` | What a response says: usage, stop reason, timing, the plan-allowance headers — parsed from a copy of the relayed bytes, bounded, never raising |
| `conversations.py` | Where a request sits in its conversation (start, after compaction, cache expired, new prompt, continuation) |
| `policy.py` | The routing policy: a decision per request with reason codes, held per conversation until its next cold boundary |
| `routing.py` | Router mode: the `model` splice, and which target answers send the request back to the requested model |
| `costs.py` + `pricing.json` | List prices (versioned, dated) plus the config's contracted rates; actual and counterfactual cost |
| `configsync.py` | The config in effect (one snapshot per exchange), the last known good on disk, the heartbeat |
| `requestlog.py` | The JSONL spool and the exporter to the control plane |
| `report.py` | The observer report over the local log |
| `admin.py`, `credentials.py` | The `/admin/*` surface and the organization's Anthropic key |

## The life of a request

```mermaid
sequenceDiagram
  participant CC as Claude Code
  participant GW as AI gateway
  participant A as Anthropic API
  participant CP as control plane

  CC->>GW: POST /v1/messages (+ hint headers, gateway key)
  GW->>GW: team mode: identify the developer (key digest)
  GW->>GW: snapshot config · read signals · place in conversation · decide
  GW->>A: the same bytes (gateway key removed; org key swapped in for org-billed developers)
  A-->>GW: stream
  GW-->>CC: the same stream, as it arrives
  GW->>GW: parse a copy: usage, timing, allowance → cost, counterfactual
  GW->>GW: queue the record → spool (JSONL)
  GW-)CP: POST /gateway/events (batches, cursor moves on ack)
```

### Identity (team mode)

A gateway key (`tyg_…`) arrives as `x-theygent-gateway-key` (set through
`ANTHROPIC_CUSTOM_HEADERS`, for developers who keep their own claude.ai login or API key in
`Authorization`) or as the credential itself (`ANTHROPIC_AUTH_TOKEN` / `ANTHROPIC_API_KEY`, for
developers billed to the organization). It is compared as a SHA-256 digest against the key set the
control plane serves (`GET /gateway/keys/sync`, re-synced every `THEYGENT_AI_GATEWAY_KEY_SYNC_SECONDS`,
30 s by default) and never forwarded. A developer's credential profile decides the upstream
credential: `passthrough` keeps theirs; `org_key` replaces every client credential with the
organization's key, which then serves Claude Code's endpoints only.

The gateway's own refusals (Anthropic's error shape, `x-theygent-gateway-error`):

| Status | Code | Means |
|--------|------|-------|
| 503 | `gateway_keys_unavailable` | No key set loaded yet (retryable) |
| 401 | `gateway_key_missing` / `gateway_key_invalid` | No key, or an unknown or revoked one |
| 503 | `org_key_not_configured` | An org-billed developer, and the gateway has no org key |
| 403 | `endpoint_not_allowed` | The org key serves messages, count_tokens and models only |
| 401 | `upstream_credential_missing` | A passthrough developer sent no Anthropic credential |
| 504 / 502 | `upstream_timeout` / `upstream_unreachable` | Before the upstream answered (retryable) |

Every exchange gets a record, refused and failed ones included.

### Signals, conversations and the policy

Claude Code labels each request when `CLAUDE_CODE_GATEWAY_HINT_HEADERS=1`: its class (main,
subagent, compaction, auxiliary, workflow), session, agent and prompt ids. Without the hints
nothing is a routing candidate. The tracker places each request in its conversation (the main loop
and its compaction share one; each subagent has its own), from the request's own cache-lifetime
markers. Caches belong to one model, so the policy only ever moves a conversation where its cache
is cold anyway — a subagent at spawn, the main conversation at its start, after compaction or
after its cache expired. A decision then holds until the conversation's next cold boundary: its
tool loops and warm new prompts stay on the model their cache is on, routed or not (moving back to
the requested model is a switch too). A warm move happens only where staying would cost at least
`warm_switch_margin` times rebuilding the cached prefix on the other model — and then the
counterfactual prices that rebuild. Claude Code switching the requested model itself (`/model`, its
fallback model) is a fresh decision, since the cache there is cold anyway.
Some traffic never moves: compaction, the quota check, side requests that ride the main
conversation's cache, requests carrying `safeguards`, and side requests whose fingerprint an admin
has not reviewed. A target must accept the request's shape: Sonnet 5.5 is a drop-in for Opus 5.5;
Haiku 4.5 rejects Claude Code's request shape. A target must also cost less than the requested
model (`costs_less`), at the config's contracted prices: no rate higher, and at least one lower.
An unpriced model on either side is never moved (`target_not_cheaper`), so nothing is ever
routed up to a dearer model.

Each decision carries reason codes (`ReasonCode`, 28 of them; the interface's
`lib/gatewayReasons.ts` explains every one, and a test in `packages/ir` keeps the two in step) and
a counterfactual cost, priced once the response's usage is known. Counterfactuals assume the other
model would use the same tokens — an estimate.

### Router mode

In router mode the forward path applies an Anthropic-target route: `routing.with_model` splices
the target into the body's top-level `model` value — the rest of the body, every header and beta
stay as Claude Code sent them — and the request goes to the target. If the target answers 400,
404, 413, 422, 429 or 529 before the response starts, the gateway closes that answer and sends the
original bytes to the requested model; Claude Code sees only that answer (`target_refused`), and
the policy pins the conversation to the requested model until its next cold boundary
(`pinned_after_refusal`). An applied route's record says `applied`, names the served model, prices
`cost_usd` on it and `baseline_usd` on the requested model, so the report shows what routing
saved. Proven against the real API by the opt-in end-to-end test: Claude Code asking for
Opus 5.5 completes its work served by Sonnet 5.5.

A local-target route (an `allow_local` side request — the session title, typically) is answered
by the inference plane instead (`local.py`, at `THEYGENT_INFERENCE_PLANE_URL` through
`packages/gateway-client`). `translation.py` turns the request into a chat completion, and does so
faithfully or not at all:

- **What carries over:** the system prompt less the billing attribution, mid-conversation system
  messages in place, text, sampling settings, and the output schema (as a `json_schema` response
  format). Thinking is off, and the output budget is capped at 4,096, or 16,384 for an agent turn.
- **For a model that follows tool calls:** the tools not deferred, plus those `tool_addition`
  blocks add mid-conversation (how tool search loads them), as OpenAI functions; `tool_choice`
  mapped; `tool_use` blocks as an assistant's `tool_calls`, each `tool_result` as a `tool` message
  right after its call (an error marked `[error]`). Thinking blocks are dropped.
- **For a model that takes images:** images, as `image_url` parts.
- **Never:** documents, server tools and their results, an image inside a tool result, or any
  block type it does not know.

The answer comes back through a writer that turns OpenAI chunks into Anthropic events. Text
streams as deltas; each tool call is emitted as one whole `tool_use` block (input in a single
`input_json_delta`) once its arguments are complete. A local `toolu_local_…` id replaces the
model's own, and the stop reason is `tool_use` when it called one. A prompt over 32K tokens (at about four bytes a token) is
never sent.

The gateway always streams from the engine, so a client that hangs up closes the stream and the
model stops; a non-streaming request would hold the engine to the end. What happens next depends
on the request:

- **Structured output, or a client that did not stream.** The answer is read whole and checked —
  text, and JSON that fits the schema, with reasoning blocks and code fences stripped. It is
  returned as the Anthropic message Claude Code asked for.
- **Any other request.** The answer is relayed as Anthropic server-sent events as it is written,
  with pings while the model is quiet and a leading reasoning block dropped. Nothing is relayed
  until the answer is committed: its first visible text, or its first tool call once whole and
  checked (an offered tool, arguments that are a JSON object). The first output must begin within
  15 seconds — opening the stream shares that deadline — and the commit must come within 30. A bad
  tool call after the commit still goes out, with empty input, and Claude Code answers it with a
  tool error as it would for any model.

Anything that fails before an answer is committed sends the original bytes upstream and records
`target_refused`, without pinning anything (a side request holds no conversation cache). That
covers a request that does not translate, no inference plane, an error, no first token in time, a
checked answer over 30 seconds or one that does not fit. A local answer is recorded as
applied, served by the logical id, `cost_usd` 0.0 (no API spend; local compute is not priced) and
`baseline_usd` priced on the requested model with the local model's token counts — a different
tokenizer, so an estimate. Claude Code sends side requests only in interactive sessions, so its
opt-in test (`THEYGENT_E2E_LOCAL_MODEL`) drives the gateway with the recorded title request's
shape against a real local model and a fake upstream.

### Local models, by share

`PolicyConfig.local_models` lists the models on the inference plane the gateway may use, small to
large. Each has a logical id, the context it takes, and whether it follows tool calls and takes
images. `local_routing` sets a dial per kind of request: a share of conversations, the model or
"the smallest that fits", and for subagents an optional agent-type filter.

- **Sampling (`policy.in_share`).** A conversation is in a share when a SHA-256 of its key falls
  below it, so it lands the same way every time it is decided. Sampling happens only at cold
  boundaries: a subagent's spawn, the main conversation's start or after compaction. The decision
  then holds across the tool loop, and a warm prompt stays where its cache is. Moving to a local
  model mid-cache is never priced as paying.
- **Choosing the model (`local_model_for`).** An agent turn needs a model that follows tool calls,
  with the prompt (about four bytes a token) within its context. When nothing fits, the request
  takes Anthropic's decision with `no_local_model_fits` added.
- **Side requests.** They keep their Catalog review. A Phase 2 config's `local_target` stays their
  model, and their dial starts at 100%.
- **What the route receives.** The chosen model's capabilities (`capabilities_of`), so tools,
  images and the prompt cap match the model.
- **When the local model cannot take the request.** `Policy.after_local_failure` re-decides on
  Anthropic's side (a subagent may still start on the cheaper target), with `target_refused`
  first, and holds that until the next cold boundary.

### Shadow runs

A counterfactual assumes the target writes as many tokens as the requested model did. Shadow runs
(`shadow.py`) measure that. A complete, 2xx Messages exchange whose decision routed it somewhere it
did not go has a copy sent to the decision's target, after the exchange ends. In practice that
means observer mode. Nothing awaits the copy, and its answer goes only to the record: the
exchange's observation is delivered when the shadow ends, with a `ShadowRun` holding the outcome,
usage, cost and time. If the gateway stops first, it is delivered without one.

- **Local targets** (`PolicyConfig.shadow_local`, default on) go through the local route's
  `attempt()`. The outcome says whether the answer would have been used (`answered`), would have
  fallen back (`unusable`), never came (`failed`), or the request does not translate
  (`untranslatable`).
- **Cloud targets** (`shadow_cloud`, default off) get the routed bytes (`with_model`), sent with
  the exchange's outbound headers.
    - They run only on a key the gateway's operator holds: the org key (the `org_key` profile),
      or in personal mode the user's own API key. Never an OAuth seat, never a team developer's
      own key.
    - They stop for the UTC day at `shadow_cloud_daily_usd`, counted per instance in memory.
    - They pause for ten minutes after a 429/529, or when any `anthropic-ratelimit-*` limit has
      under half remaining.
    - Outcome mapping: 400/404/413/422 is `unusable`; anything else that is not 2xx, a transport
      error or the 300 s timeout is `failed`.
- **Limits.** At most one local and two cloud shadows run at once. Beyond that a shadow is skipped,
  never queued.

`GatewayTotals` sums them: `shadow_calls`, `shadow_answered_calls`, `shadow_cost_usd`
(calibration spend, outside `cost_usd`), and `shadow_output_tokens` against
`shadowed_output_tokens` (the real exchanges' output for the same answered shadows). The report
shows that ratio per target. It does not adjust counterfactuals.

### Auxiliary requests and the fingerprint catalog

Side requests (titles, suggestions, checks) are told apart by a fingerprint of their shape — a
digest of the client's own prompt, so it changes with client releases. A kind's review status
(`unreviewed`, `observe_only`, `allow_cheaper`, `allow_local`, `never_route`) is a rule in the
gateway config; the control plane's catalog joins the rules with what was observed, and suggests a
status for kinds it recognises by shape (quota check, main-prefix side request, session title).

## Configuration, keys and records

- **Retention.** The control plane keeps request rows in detail for
  `ai_gateway.request_retention_days` (90 by default), then archives whole days into compressed
  JSON lines. The daily aggregates carry every report grouping, session and reason included, so a
  report over archived days reads exactly as it did. See
  [control-plane.md](./control-plane.md#retention).
- **Config.** Immutable versions on the control plane (`GET /gateway/config`, the version as ETag).
  Every heartbeat (`POST /gateway/heartbeat`, `THEYGENT_AI_GATEWAY_HEARTBEAT_SECONDS`, 30 s)
  learns the latest version and the gateway pulls only when it differs. The applied config is kept
  on disk as the last known good, so a restart with the control plane down serves under the same
  config. An exchange reads one snapshot (config, mode, prices) when it starts, and its record names
  that version.
- **Request log.** Records append to numbered `0600` JSONL segments in the state directory; the
  exporter sends batches from a persisted cursor and moves it only on an acknowledgement. A lost
  acknowledgement means a re-send, which the control plane dedupes by record id; a refused batch is
  retried, never skipped (both sides validate the same wire types, so a refusal is a skew to fix,
  not data to drop). On shutdown the queue drains to the spool and the next start exports it.
- **State directory** (`THEYGENT_AI_GATEWAY_STATE_DIR`, default `~/.theygent/ai-gateway`): the
  spool and cursor, `config.json`, `keyset.json` (digests only), and a stored org key.

## The admin surface

`/admin/status`, `/admin/credentials`, and `PUT`/`DELETE /admin/credentials/org-api-key`
(camelCase) back the interface's AI Gateway Settings tab, called straight from the browser so the
org key never transits the control plane. Callers present their TheYgent bearer; the gateway asks
the control plane (`POST /gateway/introspect`, with its service key) whether it is a live admin's,
and caches the answer briefly by the bearer's digest — 503 when the control plane cannot answer.
A standalone personal gateway (loopback, no control plane) has an open admin surface. CORS is
answered on `/admin/*` only, for `THEYGENT_AI_GATEWAY_ADMIN_ORIGINS`.

The org key is a deployment secret (`THEYGENT_AI_GATEWAY_ORG_API_KEY`), and a key the deployment
set cannot be replaced from the admin surface. A single-instance install may instead set it there;
it is kept in the state directory (`0600`). A replicated gateway turns that off
(`THEYGENT_AI_GATEWAY_STORE_ORG_KEY=0`): each instance would keep its own copy. `orgApiKey.readOnly`
tells the page which of the three applies (`env`, `personal_mode`, `deployment_only`).

## Prices

`pricing.json` is Anthropic's published price list, read from the docs' Markdown copy of the
pricing page by `pricing_source.py`, never hand-edited. It is stamped with `updated_at` (date and
time, UTC), and `version` is that date. Besides each model's five rates it holds two published
modifiers that change what a Claude Code request costs:

- `long_context`: a model priced by prompt length charges higher rates once the prompt is over a
  threshold. The prompt counts input, cache reads and cache writes. Haiku 5.5 has this tier.
- `fast`: a request sent with `speed: "fast"` (Claude Code's fast mode, recorded as
  `RequestShape.speed`) pays the model's fast rates. The cache rates keep the model's own ratios.
  Only models whose `TargetCapabilities.fast_mode` is set may serve such a request, so a fast
  request is never routed to a model without one.

A contracted override in the config replaces a model's base rates, and its tiers keep their
published ratio to them. `make ai-gateway-prices` (or `scripts/update_pricing.py`) rewrites the
file when a price changed, and `CHECK=1` only reports. `.github/workflows/ai-gateway-pricing.yml`
runs it every Monday and opens a pull request when prices change, so a change is reviewed before
it reaches anyone's figures. A page the parser cannot read fails loudly rather than being half
read. Tests that pin dollar figures price them on a frozen copy (`tests/fixtures/pricing-*.json`),
so a price update changes the gateway's figures without breaking them.

## The report

`theygent-ai-gateway report [--since 24h] [--by class,client,model] [--json]` reads the local log
without the gateway's cooperation and answers what the traffic cost, what it would have cost with
the policy's decisions applied, how many calls and tokens routing would have moved (to a cheaper
model, or to a local one), and what is wrong with the setup (`SetupWarningCode`, e.g. tool search
off). It never re-prices: every record carries its own cost and counterfactual. Claude Code files
every token under the model it *requested*, so `model` matches Claude Code's own `modelUsage`;
`served-model` shows where tokens went. On a subscription the dollar figures are API-equivalent
value, and plan use comes from the seat's utilization readings. The control plane's
`GET /gateway/report` computes the same totals (`GatewayTotals`) over everyone's records.

## The other sides

- **Control plane** — the `/gateway/*` API (keys, service keys, developers' credential profiles,
  config versions, instances, ingest, reports, sessions, the catalog) and its tables:
  [control-plane.md](./control-plane.md).
- **Interface** — the AI Gateway page (Overview, Report, Sessions, Setup & keys; Catalog and
  Settings for admins): [interface.md](./interface.md).
- **Wire types** — `theygent_ir.gateway`: [ir-and-packages.md](./ir-and-packages.md).
- **Run modes** — bare metal, Compose (profile `gateway`), Kubernetes (a StatefulSet):
  [deployment.md](./deployment.md).

## Testing

`uv run --package theygent-ai-gateway pytest` runs the fast suite: the real app against a fake
Anthropic upstream (`tests/_fake_anthropic.py`, a real local server so streams really stream),
injected key, config and event seams, no network. `tests/fixtures/exchanges/` holds real Claude
Code exchanges rebuilt without content; `test_fixtures.py` scans every fixture for paths, emails,
credentials and prose, since the repository is public. The suite starts every test from an empty
gateway environment, because `make test` exports the developer's `.env`.

Opt-in tests run against the real thing. `THEYGENT_E2E_CLAUDE_CODE=1 uv run --package
theygent-ai-gateway pytest -m integration -n0 tests/test_e2e_claude_code.py` drives headless Claude
Code through the Agent SDK and a real gateway, four times:

- personal mode;
- team mode;
- router mode with Opus 5.5 requested and the whole conversation served by Sonnet 5.5;
- router mode with subagents only: the main conversation stays on Opus 5.5, and the Explore
  subagent is served by Sonnet 5.5 from its spawn.

Each run requires the gateway's per-model totals (input, output, cache reads and cache writes) to
equal Claude Code's own `modelUsage`. Claude Code files routed tokens under the model it
requested, which is why its cost display overstates routed spend. It skips cleanly without the
opt-in or without credentials. `THEYGENT_E2E_LOCAL_MODEL` runs the local route against a real
model on a running inference plane, with a fake upstream.
