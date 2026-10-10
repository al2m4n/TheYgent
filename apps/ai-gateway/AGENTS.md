# AGENTS.md — AI gateway

Rules specific to `apps/ai-gateway`. The repo-wide rules in the root [AGENTS.md](../../AGENTS.md)
apply first. The evidence behind these rules is in [spike/FINDINGS.md](spike/FINDINGS.md); the
current work plan is [PHASE1.md](PHASE1.md).

The gateway sits on a coding agent's model path (Claude Code → the Anthropic Messages API). A
mistake here breaks developers' everyday tool, leaks their code, or quietly makes every request
more expensive. Treat every rule below as load-bearing.

## The trust boundary

- **The gateway lives in the user's trust domain.** It sees prompts, code and credentials. It
  never persists a credential, and it never sends content to the control plane: what crosses is
  the metadata in `theygent_ir.gateway`, whose types refuse free text. Do not widen those types
  to carry content.
- **Personal mode listens on loopback only.** It accepts requests without a gateway key. Team
  mode needs keys from the control plane. Settings enforce both; don't weaken the checks.
- **Team mode fails closed.** No gateway key, an unknown or revoked key, or no key set loaded yet
  is a refusal (a named 401/503 in Anthropic's error shape), never a pass. Keys are compared as
  SHA-256 digests; the persisted key set holds digests only.
- **A gateway key (`tyg_…`) never goes upstream**, whichever header carried it. On the `org_key`
  profile every client credential is replaced by the organization's key, which serves Claude
  Code's endpoints only (messages, count_tokens, models).
- **Never read `ANTHROPIC_BASE_URL`.** That variable points Claude Code *at* the gateway; honouring
  it would forward to itself. The upstream is `THEYGENT_AI_GATEWAY_UPSTREAM_URL`.

## Pass-through is exact

Claude Code depends on the bytes it sends arriving unchanged:

- **Prompt caching and preserved thinking.** Any edit to `system`, `tools` or `messages` changes
  the cached prefix, and Claude Code then strips thinking or loses the cache.
- **Paired features.** Betas and the body fields they enable must arrive together.
- **Positional attribution.** The attribution block is recognised by its position.

The rules that follow:

- Forward request bodies byte-for-byte. Observer mode never changes a request.
- Forward `anthropic-*` headers and body fields as **open lists**. Never allowlist what you see
  today.
- Return upstream errors verbatim, including `retry-after`, `x-should-retry` and the
  `anthropic-ratelimit-unified-*` headers. Claude Code's retry logic matches on error wording.
- Never buffer a stream. Forward `ping` events. Relay through `message_delta` and `message_stop`.
- A client disconnect cancels the upstream call.

## Configuration

- **One snapshot per exchange.** An exchange reads the config, mode and prices once, when it
  starts; a config applied mid-request never mixes in, and the record names the version it was
  decided under.
- **Versions only move forward**, and the last known good config is kept on disk, so a restart
  with the control plane down serves under the same config.
- **Attached to a control plane, an unconfigured gateway records nothing** (passthrough): an
  organization's traffic is recorded only once an admin has chosen observer mode. A standalone
  personal gateway observes from the start.

## The admin surface

- **It fails closed.** `/admin/*` admits only callers the control plane vouches for as admins; if
  the control plane cannot answer, the answer is 503, never a pass. Only a standalone personal
  gateway (loopback, no control plane) is open.
- **It never returns a secret.** Credentials are reported as set or not, with their source; no
  response, repr or log line carries a key or a bearer. Cached answers are keyed by the bearer's
  digest.
- **A key the deployment set wins.** The admin surface writes the org key only for
  single-instance installs; an environment-provided key cannot be replaced from it.
- **Browsers reach `/admin/*` only.** CORS is answered there, for the interface's origins, and
  never on a proxied path.

## The request log

- **Recording never blocks a request.** The observe hook only queues the observation; a writer
  task does the disk work in a thread. A full queue drops (and counts) observations; it never
  waits.
- **Every exchange gets a record**, refused and failed ones included (`request_refused`,
  `policy_error`). Passthrough mode records nothing.
- **The export cursor moves only on an acknowledgement.** A refused batch is retried, never
  skipped: the records were validated against the same wire types, so a refusal means the two
  sides disagree, and dropping the batch would lose data that is accepted once that is fixed.

## Routing (later phases)

These hold whenever the router lands:

- **Routing is per conversation segment, never per request.** Caches are per model, so a
  mid-conversation switch usually costs more than it saves.
- **Targets must accept the request's shape.** Sonnet 5.5 is a drop-in for Opus 5.5; Haiku 4.5
  is not.
- **A routed request the target rejects is retried on the original model before Claude Code sees
  anything.** Claude Code disables a rejected capability for the rest of the conversation.
- **Never route** compaction, the quota check, requests that share the main conversation's cache,
  requests carrying a `safeguards` field (auto-mode classifier context), or unreviewed auxiliary
  fingerprints.

## Conventions

- **Settings:** an empty environment value means unset. A bad value fails at startup naming its
  variable.
- **Wire casing:** the gateway's own surface (`/healthz`, `/readyz`, `/admin/*`) is camelCase, like
  the inference plane's management surface. The Anthropic-format paths keep Anthropic's shapes.
- **`spike/` is research tooling, not product code.** The type check skips it. Nothing under
  `src/` imports from it.

## Fixtures

`tests/fixtures/exchanges/` holds real Claude Code exchanges rebuilt without content by
`spike/make_fixtures.py`. Settings and counts are kept exactly; prompts, code, tool text, model
output, Claude Code's prompt and the user's environment are placeholders. The repository is
public: `test_fixtures.py` scans every fixture for paths, emails, credentials, foreign ids and
prose, and any new fixture must pass it.

## Tests

```bash
uv run --package theygent-ai-gateway pytest   # fast suite: the real app, no network
uv run --package theygent-ai-gateway theygent-ai-gateway report --since 24h   # the observer report
```

The report reads the records as written and never re-prices them, so a figure in it is the figure
the control plane receives.

The fast suite injects the upstream transport (`create_app(upstream_transport=…)`) and must run on
any machine offline. Tests against the real Anthropic API or a real Claude Code are opt-in and
env-gated; they skip cleanly when credentials are absent.
