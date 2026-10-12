# AI gateway — Phase 2: router mode

Phase 1 records what routing would save; this phase makes the saving real. In a new **router**
mode the gateway applies the policy's decisions: a subagent asking for Opus 5.5 starts on
Sonnet 5.5, a main conversation moves only where its cache is cold, a reviewed side request may run
on a local model — and Claude Code never notices, because a target that rejects a request is
retried on the original model before Claude Code sees anything. The evidence is in
[spike/FINDINGS.md](spike/FINDINGS.md); the invariants in [AGENTS.md](AGENTS.md#routing).

## Exit criteria

- In router mode, routed requests reach the target model and Claude Code completes its work
  unchanged, proven end to end with the Agent SDK (Opus requested, subagents served by Sonnet).
- No request a target rejects ever reaches Claude Code as an error: it is retried on the requested
  model, and the conversation stays there.
- A conversation never changes model with a warm cache unless the configured margin says the
  saving outweighs the rebuild.
- The report shows realized savings: what the traffic cost against what it would have cost without
  routing, per request, class, model and developer.
- Observer mode keeps every Phase 1 guarantee (byte-identical pass-through); passthrough records
  nothing; an admin switching back to observer stops routing within one heartbeat.
- `make test`, `make lint` and the docs build green; every contract extension named here.

## Decisions

| Topic | Decision |
|---|---|
| Opt-in | Router mode is a third `GatewayMode`, chosen by an admin per config version. Nothing routes before an admin picks it, and switching back applies within one heartbeat |
| What changes in a routed request | The top-level `model` value only, spliced into the original bytes — every other byte, header and beta stays as Claude Code sent it, so caching, preserved thinking and paired betas are untouched |
| Stickiness | A conversation stays on the model its cache lives on until a cold boundary (start, after compaction, cache expired). "Stay" means stay on the *serving* model, not on the requested one |
| Rejected routes | A target's refusal before the response starts (400, 404, 413, 422, 429, 529) is retried once on the requested model, and the conversation is pinned there for the rest of its segment. 401/403 are not retried: they are about the credential, not the model |
| Accounting | Records keep `cost_usd` = what the request cost as served, and gain `baseline_usd` = what it would have cost on the requested model, so observer and router reports read the same way: *without routing* vs *spend* |
| Seats | Router mode applies to subscription seats too: a seat's routed traffic draws on its own plan's allowance for the target model, which the seats report shows |
| Local route | `allow_local` side requests go to the inference plane's OpenAI-compatible data plane (translated both ways), with the Anthropic upstream as the fallback on any failure |
| Shadow runs | Local shadows on by default; cloud shadows off by default, API-key traffic only (never seats), capped per day — as decided in Phase 1 |

## Tasks

Status: `todo` · `doing` · `done`.

| ID | Task | Depends | Done when | Status |
|---|---|---|---|---|
| R1 | Sticky serving model: the policy decides at cold boundaries and keeps the decision until the next one; a warm new prompt stays on the serving model (moving back to the requested model is a switch too), unless staying would cost at least `warm_switch_margin` times rebuilding the cache on the other model — the margin, documented since Phase 1, takes effect. Claude Code changing the requested model itself re-decides. New reason codes `follows_warm_cache` and `warm_switch_pays` | — | Policy tests: a routed main conversation stays routed across new prompts; compaction and cache expiry re-decide; the margin moves a warm conversation only when it pays; observer counterfactuals price a cold cache only for a warm switch. The policy holds each conversation's decision (by conversation, not segment) until a cold boundary; a warm prompt with no candidate names the real reason; the margin compares reading the cached prefix where it is against writing it on the other model at its TTL. Seven mutants caught | done |
| R2 | Contract: `GatewayMode` gains `router`; `GatewayRequestRecord` and `GatewayTotals` gain `baseline_usd`; reason codes for retried routes; control-plane migration and SQL totals; interface types regenerated | R1 | Named in this file; wire tests, the no-free-text scan and the totals pinned; a Phase 1 record still validates. Also `GatewayTotals.applied_calls`; an applied decision carries no counterfactual (validated), and with-routing counts what ran for it; saving is baseline minus with-routing (unchanged for Phase 1 records). Migration `0023_ai_gateway_router` (existing daily rows: baseline = cost); the report SQL is checked against the shared totals in both modes; the session timeline carries `baseline_usd`; reason codes `target_refused` and `pinned_after_refusal`; the interface shows router mode wherever modes appear (one exhaustive summary map) | done |
| R3 | Apply decisions: in router mode a routed request's `model` is spliced into the original bytes; `served_model` and `applied` recorded; transparent retry on the requested model for a target's refusal before the response starts; the segment pinned after a refusal | R1, R2 | Byte tests: only the model value differs; retry tests against the fake upstream (refusal → original model, Claude Code sees the original's answer); never-route invariants hold in router mode. `routing.with_model` walks top-level keys with the C decoder and refuses ambiguity (duplicates, non-string, trailing data, invalid UTF-8); across every fixture a request changes exactly when its decision is applied; 401/403 not retried; a local target not applied yet (R6). Real run: the opt-in e2e has Claude Code ask for Opus 5.5 and finish served by Sonnet 5.5, totals matching. Ten mutants caught | done |
| R4 | Reports for realized savings: control-plane report and sessions, the local report CLI, and the interface (Report tiles *without routing* / *spend* / *saved*, Sessions showing *routed → served by*) | R2, R3 | Report tests on seeded rows of all three modes; interface tests; a visual check. The control plane's report and timeline already carry the R2 fields; the CLI adds *Without routing* and routed / would-route counts when routes were applied; the interface measures saving against the baseline, shows a *Without routing* column, draws each day against its baseline, and says *routed → target (saved …)* / *routing saved* — observer-only data reads exactly as before | done |
| R5 | Router mode in the interface Settings tab: the mode choice with what it does, per-target guard rails (a target the policy knows rejects Claude Code's shape is refused at save) | R2 | Interface tests; control-plane validation tests. The capability table moved to `theygent_ir.gateway` (`TARGET_CAPABILITIES`, `base_model`, `target_problem`), shared by the policy and the control plane; `PUT /gateway/config` refuses a subagent or main target with no capability data or one that rejects Claude Code's requests (`422 invalid_gateway_config`, naming the field, never the value) — on save only, so earlier versions still load. Settings offers Router, warns when no target would route anything, and shows a save error beside the button. Visual check on a seeded stack (observed and routed days, sessions, the Haiku refusal, router saved and shown on the Overview); it caught a routed session's summary saying "would route", fixed with a test. User docs now describe router mode | done |
| R6 | Local route: Anthropic Messages ⇄ OpenAI chat completions for `allow_local` side requests (structured output, streaming), via `THEYGENT_INFERENCE_PLANE_URL`; any failure falls back to the Anthropic upstream | R3 | Translation tests on the session-title fixture; fallback tests; a real run against a local model. `local.py` translates text-only requests (the billing block dropped, output capped at 4,096, thinking off, the schema as a `json_schema` response format) through `packages/gateway-client`, checks the answer (text; JSON fitting the schema, reasoning and fences stripped) and returns it as a message or a stream. Fallback on an untranslatable request, no plane, an error, a 30 s timeout or a misfit answer: the original bytes go upstream (`target_refused`, nothing pinned). Records: applied, served by the logical id, cost 0.0, baseline estimated on the local token counts. Compose, the host-inference overlay and k8s (from the shared ConfigMap) pass the URL; no `depends_on`. Real run: Qwen3-4B-Instruct (llama.cpp) wrote the title in 0.6 s streamed, nothing upstream; opt-in `THEYGENT_E2E_LOCAL_MODEL` test, since Claude Code sends no side requests headless. Fifteen mutants caught | done |
| R7 | Shadow runs: a copy of a routable request to its target, after the real one, to calibrate counterfactual output lengths; local on, cloud off and capped, never on seats; results feed the report as calibration, never the response | R3 | Shadow never delays or alters the real exchange; cap and seat rules tested. `shadow.py`: after a complete 2xx exchange whose route was not applied, a background copy to its target, the record delivered when it ends (without it if cut short). Local through the local route's `attempt()`; cloud is the routed bytes on the exchange's outbound headers, only on a key the operator holds (org key, or personal mode's own API key: never a seat or a team developer's own key), stopped for the UTC day at the per-instance budget, paused ten minutes under rate-limit pressure; one local and two cloud at once, skipped beyond. Outcomes `answered` / `unusable` / `failed` / `untranslatable`. Reports: the CLI's calibration table, the control plane's shadow sums (migration `0024`), the Report's *Shadow runs* card, the timeline's shadow line, Settings controls. Seventeen mutants caught | done |
| R8 | End to end and docs: the Agent SDK drives Claude Code in router mode (Opus requested, Explore subagent served by Sonnet, the task completes, totals match); user docs for router mode, the cost-display note made concrete, dev docs | R3, R4 | Opt-in e2e green; `make docs-build` strict passes. `test_router_mode_serves_an_explore_subagent_on_sonnet`: subagents-only routing, Claude Code asks for Opus 5.5 throughout, the Explore subagent is served by Sonnet 5.5 from spawn (`subagent_at_spawn`, applied), the main conversation stays on Opus, nothing refused, totals equal `modelUsage` — green against the real API. The cost-display section gives the measured Explore example and names where Claude Code's figures appear; dev and testing docs list every opt-in run. Preparing it found the policy could route up to a dearer target: fixed first (`target_not_cheaper`) | done |

## Contract extensions in this phase

Each one is deliberate, named here, and covered by tests:

- `GatewayMode` gains `router`.
- `GatewayRequestRecord.baseline_usd`, `GatewayTotals.baseline_usd` and `GatewayTotals.applied_calls`
  (migration `0023_ai_gateway_router`); an applied `Decision` has no `counterfactual_usd`.
- New `ReasonCode` values: `follows_warm_cache` and `warm_switch_pays` (R1); a route the target
  refused and the retry that followed (R3).
- `TARGET_CAPABILITIES`, `base_model` and `target_problem` move into `theygent_ir.gateway`, and
  `PUT /gateway/config` refuses a target no Claude Code request could go to (R5).
- `ReasonCode` gains `target_not_cheaper`: a route goes only to a target known to cost less than the
  requested model, so nothing is routed up to a dearer one.
- Shadow runs (R7): `ShadowOutcome`, `ShadowRun` and `GatewayRequestRecord.shadow` (only on a
  route not applied, on its target); `PolicyConfig.shadow_local`, `shadow_cloud`,
  `shadow_cloud_daily_usd`; `GatewayTotals.shadow_calls`, `shadow_answered_calls`,
  `shadow_cost_usd`, `shadow_output_tokens`, `shadowed_output_tokens` (migration
  `0024_ai_gateway_shadows`); the session timeline entry carries `shadow`.
- The local route reads `THEYGENT_INFERENCE_PLANE_URL` (the name every other service already uses,
  with the same default), and the gateway depends on `packages/gateway-client` to reach it.

The binding enum, the IR, and the inference plane's `/v1/*` are unchanged.
