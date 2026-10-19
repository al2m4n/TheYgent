# AGENTS.md — control plane · `ai_gateway/`

The control plane's side of the [AI gateway](../../../../ai-gateway/AGENTS.md): it stores what
gateways report (per-request metadata records, heartbeats), serves them their versioned config
and the developer key set, and builds the reports, session timelines and fingerprint catalog the
interface's AI Gateway page shows. It never sees prompt content. The wire types refuse free text.
Rules in [apps/control-plane/AGENTS.md](../../../AGENTS.md) apply first; the full design is in
[ai-gateway.md](../../../../../docs/dev-docs/ai-gateway.md) ("The other sides").

## Where it sits

- **Wire contract:** `theygent_ir.gateway` in [packages/ir](../../../../../packages/ir/AGENTS.md)
  (`GatewayEventBatch`, `GatewayConfig`, `GatewayKeySet`, `GatewayHeartbeat`, `GatewayTotals`, …),
  snake_case, never hashed. Change it there first; both sides move together.
- **Routes** (in [app.py](../app.py), section "AI gateway service surface"): the gateway's own
  calls, authenticated with a gateway **service key** (`tygs_…`, `require_gateway_service`):
  `POST /gateway/events`, `GET /gateway/keys/sync`, `POST /gateway/introspect`,
  `POST /gateway/heartbeat`, `GET /gateway/config`. Then the people-facing calls, behind
  session or API-key auth and role checks: config versions (`PUT /gateway/config` with
  `If-Match`), instances, report, sessions, fingerprints, developer keys (`tyg_…`), service
  keys, developer credential profiles, the request archive, `/gateway/me`.
- **Tables:** `ai_gateway_request`, `ai_gateway_daily_usage`, `ai_gateway_request_archive`,
  `ai_gateway_instance`, `ai_gateway_config`, `ai_gateway_fingerprint`, `ai_gateway_developer`,
  `ai_gateway_state` (migrations `0020`–`0027`). Developer gateway keys reuse the `api_key` table.
- **Neighbours:** [auth.py](../auth.py) mints and resolves keys; [retention.py](../retention.py)
  archives old request rows into gzip JSON lines per day.

## Map

<!-- agents-map: covers=. -->

| File | What it does |
|------|--------------|
| [`store.py`](store.py) | `AiGatewayStore`. `ingest` inserts records keyed by id (a re-sent batch is a no-op), adds only the new ones to the per-day aggregates, and updates catalog stats. Also: versioned configs (a read-check-save under one lock, `ConfigConflict` → 412), the key set (digests plus each developer's credential profile; its version moves by DB trigger), instances from heartbeats, developer profiles. |
| [`reports.py`](reports.py) | Reports as SQL sums over "units": request rows for days still kept in detail, daily aggregates for archived days, so a report reads the same before and after archiving. `Scope` decides whose records a caller sees. Sessions list, session timeline, setup warnings, seat usage. |
| [`catalog.py`](catalog.py) | The fingerprint catalog: observed stats joined with the latest config's review rules, and the shape-based recognisers that *suggest* (never set) a status, such as `quota_check` and `main_prefix_side_request`. |

## Invariants specific to this package

- **Every total is `GatewayTotals` summed.** The gateway's local report uses the same definition,
  and tests check the SQL against the Python.
- **Configs are immutable versions.** A change saves a new version; a gateway applies it on its
  next poll. Review status lives in the config, not in catalog rows.
- **Ingest is idempotent and lock-ordered.** Aggregate and catalog upserts go in key order so
  concurrent batches cannot deadlock.

## Tests

`tests/test_ai_gateway_events.py`, `test_ai_gateway_config.py`, `test_ai_gateway_keys.py`,
`test_ai_gateway_reports.py`, `test_ai_gateway_catalog.py`, and `test_retention.py`.
