# AI gateway — Phase 1: observer mode

The gateway sits between Claude Code and its models. In observer mode, requests pass through
untouched while every one is recorded, explained and reported. That works in team and personal
mode, with both credential profiles (org API key; subscription seats via passthrough), and it is
managed from the control plane and the interface. **Nothing is routed in this phase.** The
evidence behind every rule here is in [spike/FINDINGS.md](spike/FINDINGS.md).

## Exit criteria

- Claude Code behaves identically through the gateway, proven by byte-identity tests and an
  end-to-end run.
- The gateway's token counts equal Claude Code's own per-model accounting.
- No prompt content leaves the gateway, enforced by a canary test.
- Every exchange is recorded locally even when the control plane is down.
- It runs in all three modes (bare metal, Compose, Kubernetes), with `make test` and `make lint`
  green.

## Decisions

| Topic | Decision |
|---|---|
| Placement | A separate app, `apps/ai-gateway`, on port 8082, configured and viewed from the control plane and interface |
| Billing | Both org API keys and subscription seats. Seats keep their claude.ai login; the developer's identity rides in `X-TheYgent-Gateway-Key` (via `ANTHROPIC_CUSTOM_HEADERS`) |
| Desktop app | Reaches the gateway only through its own third-party-inference gateway configuration, with an org credential (no subscription) |
| Modes | Team mode from the start. Personal mode is the same code with auth off (loopback only) and credentials passed through |
| Org upstream key | A deployment secret (Kubernetes Secret or env). Single-instance installs may also write it from the interface straight to the gateway; it never transits the control plane |
| Shadow runs | Local shadows on by default. Cloud shadows are off by default, API-key only, with a daily cap (Phase 2) |
| Wire types | `theygent_ir.gateway` (Pydantic), following the registration-types pattern; outside anything hashed |
| Response compression | Force `accept-encoding: identity` upstream; the client's value is recorded |
| Gateway admin auth | The gateway checks the user's TheYgent session or API key with the control plane (cached); admin role required |
| Local request log | JSONL: append-only, bounded, rotated, `0600`. Aggregation happens in the control plane |

## Tasks

Status: `todo` · `doing` · `done`.

### W0 — shared contract

| ID | Task | Depends | Done when | Status |
|---|---|---|---|---|
| A0 | Wire types for the decision record, gateway config, key sync, heartbeat and fingerprint, in `theygent_ir.gateway` | — | Strict models (unknown fields rejected); round-trip tests; the "no message text" rule written into the record type | done |

### W1 — gateway service (`apps/ai-gateway`)

| ID | Task | Depends | Done when | Status |
|---|---|---|---|---|
| A1 | Service skeleton: `create_app` factory with injectable seams, empty-safe `THEYGENT_AI_GATEWAY_*` settings, `__main__`/`asgi`, `/healthz` `/readyz`; added to `PY_TEST_DIRS` and the `ty` hook | — | It boots; the fast suite runs with no network | done |
| A2 | Byte-faithful pass-through, promoted from the spike, over `/v1/messages`, `count_tokens`, `/v1/models` and `HEAD /api/hello` | A1 | Body hash and response bytes identical; first byte not buffered; disconnect cancels upstream; no credential in logs | done |
| A3 | Fixtures from the Phase 0 captures (structure kept, prompt/tool text placeholders, no user content), plus a fake Anthropic upstream that replays them | A1 | Main, subagent, auxiliary, compaction, 400, 429 and mid-stream drop; no proprietary or user text committed | done |
| A4 | Signals and side parser: hint headers, client source, credential kind, auxiliary fingerprint (attribution block skipped), usage with 5-minute/1-hour cache writes, seat-allowance headers | A0, A3 | Table tests over fixtures; a parse failure never affects the forwarded bytes | todo |
| A5 | Identity and credential profiles: personal (loopback only) and team (gateway key as bearer or header); passthrough and org-key profiles | A2, C3 | Named 401s in Anthropic's error shape; revocation takes effect within one sync interval; a test for every mode × profile | todo |
| A6 | Conversation tracker: per (session, agent): last request, model, cache TTL from `usage`, warm/cold, segment boundaries | A4 | Bounded memory; deterministic tests with an injected clock | todo |
| A7 | Cost model with `pricing.json` (source, date): list, 5-minute/1-hour cache-write and cache-read rates; seat-allowance deltas | A4 | Matches hand-computed cases, including the Phase 0 pair ($0.1621 vs $0.0837) | todo |
| A8 | Dry-run policy v1, decisions recorded only. Rules: Explore subagent at spawn → Sonnet 5.5 candidate; session title → local candidate; main conversation at a cold boundary → cost-model check. Never routed: compaction, the quota check, requests carrying `safeguards`, requests sharing the main conversation's cache, unreviewed fingerprints. Never Haiku for main or subagent turns | A6, A7 | Table tests; request bytes unchanged whatever the decision | todo |
| A9 | Request log: local JSONL plus a batch exporter to the control plane (backoff, idempotent ids) | A0, C2 | Never blocks a request; replays after an outage; a canary prompt string never appears in the log or the export | todo |
| A10 | Config client and heartbeat: versioned pull (ETag), last-known-good on disk, pass-through only if never configured; config version stamped on every record | A0, C4 | Works with the control plane down; changes apply live | todo |
| A11 | Report CLI from the local log: actual vs would-have-been cost, calls, tokens by class, client and session; setup warnings (e.g. tool search off, detected from request shape) | A7, A8, A9 | Matches Claude Code's accounting on the e2e fixture | todo |
| A12 | Gateway `/admin/*` (camelCase): status, applied config version, credential status (whether set, never the value), single-instance upstream-key write; admin checked via the control plane | A5, C3 | CORS limited to the interface origin; never returns a secret | todo |

### W2 — control plane

| ID | Task | Depends | Done when | Status |
|---|---|---|---|---|
| C1 | Migration and models: `api_key.kind` (`personal` / `gateway` / `gateway-service`); tables for request log, daily usage, instances, config versions, fingerprints, developer profiles | A0 | Alembic up/down tested | todo |
| C2 | `POST /gateway/events`: gateway-service credentials only; batches; duplicates ignored; strict validation | C1 | Duplicate batches are no-ops; anything carrying message text is rejected | todo |
| C3 | Keys and sync: developers create and revoke their own gateway keys, admins manage all; `GET /gateway/keys/sync` (key hashes and a version); gateway keys refused on every management endpoint; token check for the gateway's admin surface | C1 | A gateway key cannot call `/agents` etc.; demoting a user narrows their keys | todo |
| C4 | Instances and config: heartbeat/registration; versioned `GET`/`PUT /gateway/config` (admin, with author and validation); instance list | C1 | Config versions immutable; ETag 304 when unchanged | todo |
| C5 | Reports: `/gateway/report` (time range, grouped by class / client / developer / session / model), `/gateway/sessions`, per-session view | C2 | Role rules (admin: everyone; user: own; viewer: totals); report SQL tested on seeded rows | todo |
| C6 | Fingerprint catalog: list with stats; admin sets status; seeded with the Phase 0 kinds | C1 | New fingerprints default to `unreviewed`, i.e. pass-through | todo |

### W3 — interface (`/gateway`, shared page layout)

| ID | Task | Depends | Done when | Status |
|---|---|---|---|---|
| I1 | Page and Overview tab: instances, mode, config version, health | C4 | Vitest plus a visual check | todo |
| I2 | Setup and keys: create a gateway key; copy-ready config per client. The CLI/IDE env block always sets `CLAUDE_CODE_GATEWAY_HINT_HEADERS=1` and `ENABLE_TOOL_SEARCH=true`; also the managed-settings JSON and the desktop app's gateway values (`inferenceGatewayBaseUrl`, API key, `toolSearchEnabled`) | C3 | The key is shown once; snippets differ per credential profile | todo |
| I3 | Report: savings band, calls, tokens, seat utilization by class / client / developer / model; a note that Claude Code's own cost display assumes the requested model | C5 | Empty and loading states; time-range filter | todo |
| I4 | Sessions: per-session timeline of requests and decisions | C5 | Shows reason codes | todo |
| I5 | Catalog and Settings: fingerprint review; mode (`observer` records every exchange; `passthrough` forwards without recording); upstream credential status, written directly to the gateway | C6, A12 | The credential never transits the control plane | todo |

### W4 — run modes, docs, end-to-end proof

| ID | Task | Depends | Done when | Status |
|---|---|---|---|---|
| E1 | Makefile start/stop/status on :8082; Dockerfile; Compose service; Kubernetes Deployment, Service and autoscaler; org key as a Kubernetes Secret; `.env.example` | A10 | Deploy guards (ports, env names, services, image tags) updated in the same change and green | todo |
| E2 | Dev docs (`ai-gateway.md`; architecture and deployment updated to four processes); user-docs AI Gateway page with nav (CLI/IDE/desktop setup, the tool-search note, the cost-display note); env reference | all | `make docs-build` strict passes | todo |
| E3 | End-to-end harness (opt-in, env-gated): the Agent SDK drives headless Claude Code through the gateway in both modes | A11, C5 | Gateway totals equal Claude Code's per-model usage; skips cleanly without credentials | todo |

## Order

1. **Week 1:** A0, then A1–A4, so personal pass-through with signals works early. C1 starts in
   parallel once A0 lands.
2. **Weeks 1–2:**
   - Gateway: A6–A9 and A11 → a personal observer with a working report, no control plane needed.
   - Control plane: C2–C4, A5 and A10 → team mode end to end.
3. **Week 3:** C5, C6, I1–I5 and A12, then E1–E3.

## Contract extensions in this phase

Each one is deliberate, named here, and covered by tests:

- A fourth process type, `ai-gateway`, on port 8082.
- An Anthropic-format ingress (Anthropic's wire shapes) and a gateway `/admin/*` surface
  (camelCase).
- The control plane's `/gateway/*` API (snake_case) and its tables.
- `api_key.kind`.
- New `THEYGENT_AI_GATEWAY_*` environment variables.

The binding enum, the IR, and the inference plane's `/v1/*` are unchanged.
