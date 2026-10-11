# AI gateway

`apps/ai-gateway` is an Anthropic-Messages proxy that sits between a coding agent (Claude Code:
the CLI, IDE extensions, the desktop app) and its models. It records what every exchange cost
and what a routing policy would have done with it, so a team can see where its model spend goes
and what moving work to cheaper models would save — before anything is moved. It is the fourth
process type, on :8082.

Not to be confused with the inference plane's OpenAI-compatible data plane (`/v1/*`, reached
through `packages/gateway-client`): that serves TheYgent's own agents; this one serves a coding
agent's Anthropic-format traffic. The two never call each other today.

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
- **Observing never changes a request.** The gateway decides what a router would do and records
  the decision with its reasons and counterfactual cost; it never applies it. Routing — applying
  decisions — is the next stage, and its rules are already fixed in the app's AGENTS.md.
- **Recording never blocks a request.** Observations go on a bounded queue; a full queue drops
  and counts, never waits.
- **Team mode fails closed** — on developer keys and on its admin surface alike.
- **Empty env means unset; a bad setting fails at startup naming its variable.**

## Two independent axes

| Axis | Values | Set by |
|------|--------|--------|
| Auth mode | `personal` — loopback only, no keys, the caller's own credential · `team` — developers authenticated by gateway keys from the control plane | `THEYGENT_AI_GATEWAY_AUTH_MODE` at startup |
| Traffic mode | `observer` — record every exchange with a decision · `passthrough` — forward without recording | the gateway config (an admin, on the control plane) |

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
| `policy.py` | The routing policy: a decision per request with reason codes |
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
Haiku 4.5 rejects Claude Code's request shape.

Each decision carries reason codes (`ReasonCode`, 25 of them; the interface's
`lib/gatewayReasons.ts` explains every one, and a test in `packages/ir` keeps the two in step) and
a counterfactual cost, priced once the response's usage is known. Counterfactuals assume the other
model would use the same tokens — an estimate.

### Auxiliary requests and the fingerprint catalog

Side requests (titles, suggestions, checks) are told apart by a fingerprint of their shape — a
digest of the client's own prompt, so it changes with client releases. A kind's review status
(`unreviewed`, `observe_only`, `allow_cheaper`, `allow_local`, `never_route`) is a rule in the
gateway config; the control plane's catalog joins the rules with what was observed, and suggests a
status for kinds it recognises by shape (quota check, main-prefix side request, session title).

## Configuration, keys and records

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

One opt-in test runs against the real thing: `THEYGENT_E2E_CLAUDE_CODE=1 uv run --package
theygent-ai-gateway pytest -m integration -n0 tests/test_e2e_claude_code.py` drives headless Claude
Code through the Agent SDK and a real gateway, in personal and team mode, and requires the
gateway's per-model totals — input, output, cache reads and cache writes — to equal Claude Code's
own `modelUsage`. It skips cleanly without the opt-in or without credentials.
