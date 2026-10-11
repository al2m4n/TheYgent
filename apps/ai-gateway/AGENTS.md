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
  single-instance installs; an environment-provided key cannot be replaced from it, and a
  replicated gateway (`THEYGENT_AI_GATEWAY_STORE_ORG_KEY=0`) neither stores nor adopts one —
  each instance would hold its own copy.
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

## Routing

Router mode applies the policy's decisions; observer mode only records them.

- **A routed request changes in one place: its top-level `model` value**, spliced into the
  original bytes (never re-serialized). A body that cannot be spliced unambiguously is not routed.

- **Routing is per conversation segment, never per request.** Caches are per model, so a
  mid-conversation switch usually costs more than it saves. A decision holds until the next cold
  boundary: "stay" means stay on the model the cache is on, which for a routed conversation is the
  target — moving back is a switch too.
- **Targets must accept the request's shape, and cost less.** Sonnet 5.5 is a drop-in for
  Opus 5.5; Haiku 4.5 is not. A route goes only to a target with no rate higher than the
  requested model's and one lower, at the config's prices. A request already on a cheaper model,
  or on an unpriced one, stays (`target_not_cheaper`).
- **A routed request the target rejects is retried on the original model before Claude Code sees
  anything.** Claude Code disables a rejected capability for the rest of the conversation. A
  refusal before the answer starts (400, 404, 413, 422, 429, 529) sends the original bytes to the
  requested model and pins the conversation there until its next cold boundary; 401/403 are
  credential problems and are not retried. A mid-stream error cannot be retried — bytes are out.
- **A local route answers only what translates faithfully, and only once the answer is checked.**
  An `allow_local` side request goes to the inference plane as a chat completion when it is text
  only (no tools, images or documents). The answer must be text, and JSON that fits the schema
  where one was asked for. Any failure sends the original bytes upstream (`target_refused`).
  Nothing is pinned, and Claude Code never sees a local error.
- **An applied route records what served it.** `served_model` from the response, `applied`,
  `cost_usd` priced on the served model and `baseline_usd` on the requested one. A local answer
  costs 0.0: no API spend, and local compute is not priced.
- **A shadow run never touches the real exchange.**
    - A copy of an unapplied route goes to its target only after the exchange ends. Nothing awaits
      it, its answer goes only to the record, and the record is delivered when it ends (without it
      if cut short).
    - A cloud shadow is real spend. It runs only on a key the operator holds (the org key, or
      personal mode's own API key), never a seat or a team developer's own key. It stops at the
      daily budget, and pauses under rate-limit pressure.
    - Shadows are sampled, never queued.
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

## Prices

- **`pricing.json` is generated, never hand-edited.** `scripts/update_pricing.py` writes it from
  Anthropic's published pricing page, stamped with when it was read; a weekly workflow opens a pull
  request when prices change. A page the parser cannot read fails; it is never half read.
- **Pin figures on the frozen copy.** A test that pins what an exchange costs uses
  `tests/_prices.py` (`FROZEN_PRICES`), so a price update never breaks the arithmetic's tests;
  tests of the packaged file check its shape and source.

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
env-gated; they skip cleanly when credentials are absent:

```bash
THEYGENT_E2E_CLAUDE_CODE=1 uv run --package theygent-ai-gateway \
    pytest -m integration -n0 tests/test_e2e_claude_code.py   # spends real (small) usage
```

It requires the gateway's per-model totals to equal Claude Code's own `modelUsage`, cache reads
and writes included: a change to how usage is parsed, recorded or summed must keep it green. Its
router runs (a conversation, and an Explore subagent, served by Sonnet 5.5 where Claude Code asked
for Opus 5.5) must too. The local route has its own opt-in test against a real local model
(`THEYGENT_E2E_LOCAL_MODEL=<logical id> … tests/test_e2e_local_model.py`), with a faked upstream.
