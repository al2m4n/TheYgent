# AI gateway — Phase 3: local models, by your choice

Phase 2 routes to cheaper Anthropic models, and sends a local model only the side requests an
admin allows (a session title, in practice). Side requests are about 4% of spend
([spike/FINDINGS.md](spike/FINDINGS.md), point 9): the money is in subagent and main-conversation
turns. This phase lets a team decide **how much** of that work its own models do — per kind of
request, across several local models (a very small one for side requests, a medium one for agent
work) — while Claude Code still never sees a local failure.

The spike measured why this needs care: on a 4B model, Explore subagent turns chose the same next
action as the cloud model 1 time in 6, at a p50 of 34 s; main-conversation turns timed out. A
capable medium model changes that, but only measurement can say by how much. So every local share
starts at zero, shadow runs measure a local model on real traffic before it serves any, and the
report shows what each tier is worth.

## Exit criteria

- An admin lists the local models (logical ids on the inference plane) with what each can do —
  context size, tool calling, images — and chooses, per kind of request (side requests,
  subagents, the main conversation), what share goes local and to which model.
- A local route streams: Claude Code sees the answer as it is written, a client that hangs up
  stops the engine, and a local model that has not started answering in time hands the request
  to Anthropic before any byte reaches Claude Code.
- Claude Code's tool calls work through a local model: tool definitions, tool use and tool
  results translate both ways, streamed.
- A conversation is either local or not for a whole segment (sticky by conversation, at cold
  boundaries); after a local failure it stays on Anthropic until its next cold boundary.
- Shadow runs measure a local model on agent turns (did it pick the same next action, how fast),
  and the report shows, per local model and kind of request, its share, its quality and the
  saving.
- A real run: Claude Code's Explore subagent served by a local model end to end.
- `make test`, `make lint` and the docs build green; every contract extension named here.

## Decisions

| Topic | Decision |
|---|---|
| The dial | Per kind of request — side requests, subagents (optionally only some agent types), main conversation — a **share** from 0 to 100% of eligible conversations, and the local model that serves them (or "the smallest that fits"). Everything starts at 0% |
| Which model | Each listed local model declares its context size, whether it follows tool calls, and whether it takes images. A request goes only to a model that can take it (tools, images, prompt size); "smallest that fits" picks the first such model in the list, which an admin orders small to large |
| Sticky | The share is sampled per conversation (a hash of its key), and applied only at cold boundaries — a subagent's spawn, a conversation's start or a post-compaction turn — so a conversation never moves mid-turn and never loses a warm cache it would have kept |
| Streaming | The local route streams from the inference plane and relays Anthropic server-sent events as tokens arrive, with keep-alive pings; a client disconnect closes the stream, which stops the engine. A prompt over the model's context is never sent |
| Falling back | Before the first token (an error, a refusal, nothing within the first-token timeout), the original bytes go to Anthropic and the conversation stays there until its next cold boundary. After the first token there is no fallback: bytes are out, as with any upstream |
| Quality | Shadow runs measure agent turns locally — answered, same next action (tool name, or a final answer where the cloud gave one), time to first token, total time — and the report shows them per local model and kind of request, so a share is raised on evidence |
| Side requests | The existing `allow_local` review stays. The side-request share applies to reviewed kinds; an unreviewed kind is never routed |

## Groundwork done before this phase

| ID | Task | Status |
|---|---|---|
| G1 | Prices: `pricing.json` read from Anthropic's published page and stamped with when (2026-10-11T13:56:42Z); fast mode and long-prompt tiers priced; `scripts/update_pricing.py` and a weekly workflow that opens a pull request when prices change; the gateway's suite in CI | done |
| G2 | Request-row retention: request rows past `ai_gateway.request_retention_days` (90) archived as gzip JSON lines per day, downloadable; the daily aggregates carry every report grouping, so reports read the same before and after (migration `0025`) | done |
| G3 | Run I/O retention: captured `node_io` past the agent's `io_retention_seconds`, or `telemetry.io_retention_days`, is deleted | done |

## Tasks

Status: `todo` · `doing` · `done`.

| ID | Task | Depends | Done when | Status |
|---|---|---|---|---|
| L1 | Stream the local route: open a stream to the inference plane, relay Anthropic server-sent events incrementally (text deltas, pings), a first-token timeout that falls back before any byte is sent, the client's disconnect closing the engine's stream, a prompt-size cap from the model's context | R6 | Tests: incremental relay (bytes reach the client before the model finishes), fallback on a slow first token, disconnect cancels the upstream stream, oversized prompt never sent; R6's tests still green. Checked answers (schema, or a non-streaming client) are streamed from the engine and read whole; others relay once visible text starts within 15 s — opening the stream shares that deadline. Pings every 5 s, a leading reasoning block filtered across pieces, prompts over 32K tokens refused; closing an answer never read stops the engine. Real run (Qwen3-4B, llama.cpp): first words in 0.29 s, whole answer in 1.85 s, 74 deltas, usage recorded. Mutants caught | done |
| L2 | Contract: `PolicyConfig.local_models` (logical id, context tokens, tools, images) and per-kind local routing (share, model or smallest-that-fits, subagent types); `local_target` stays as the single side-request model it is today; reason codes for local routes and their refusals | L1 | Wire tests; a Phase 2 config still loads and behaves the same; validation names the field. `LocalModel` (logical id, `context_tokens` ≥ 1,024, `tools`, `images`), `LocalShare` (share 0–1, model or smallest-that-fits, subagent `agent_types`), `LocalRouting` (side requests default 1.0 — the Catalog review is the opt-in — agent turns 0.0); validated: unique ids, never an engine name, a dial's model must be listed (or be `local_target`), an agent-turn share needs a model that follows tool calls. Reason codes `local_share`, `no_local_model_fits`. The interface passes them through its editor unchanged until L6 | done |
| L3 | Tool calling both ways: Anthropic `tools`/`tool_choice` → OpenAI tools; `tool_use`/`tool_result` blocks → assistant `tool_calls` and `tool` messages; streamed `tool_calls` deltas → `tool_use` blocks with `input_json_delta`; thinking and cache markers dropped; images to a model that takes them; server tools and anything else untranslatable | L1 | Translation tests on the recorded subagent and main exchanges (round-trip shapes, streamed tool calls reassembled to the same input), refusal tests. `translation.py`: every recorded Claude Code request translates for a model with tools and none without; `tool_addition` blocks (tool search) add tools, deferred ones stay out, `$schema` is stripped, tool results follow their calls (errors marked), thinking dropped, images only to a model that takes them, server tools/documents/unknown blocks refused. The writer emits each tool call whole; an answer that opens with an unoffered tool or broken arguments falls back; a checked answer carries text and tool calls as a message, and a shadow attempt records the tools called and time to first output. Real run (Qwen3-4B, llama.cpp): turn 1 called `Bash` `{"command": "ls -la"}` in 2.8 s, turn 2 answered from the tool result in 0.75 s. Ten mutants caught | done |
| L4 | The policy: per-kind share sampled per conversation, the model chosen by fit, cold boundaries only, pinned to Anthropic after a local failure; observer mode records what local routing would do | L2, L3 | Policy tests: share 0 and 100, stickiness across a tool loop, fit (tools, images, context), pin after failure, observer counterfactuals. `in_share` (SHA-256 of the conversation key; a partial share lands each conversation the same way every time) at cold boundaries only; `local_model_for` (the dial's model, else the smallest listed that follows tool calls with the prompt in its context); side requests keep `local_target`; `after_local_failure` re-decides on Anthropic's side and holds it. Through the gateway: an Explore subagent's spawn and loop served locally with tools, recorded applied at no API spend; a failing local model sends the subagent to Sonnet instead; observer mode only records. Ten mutants caught | done |
| L5 | Shadow runs for agent turns: same-next-action, time to first token and total time recorded per local model; the report and the interface show share, quality and saving per local model and kind | L4, R7 | Report tests; interface tests; a visual check. `ShadowRun.agreed` (same next action — the same first tool, or answering where the reply answered — for a request that offered tools) and `first_output_ms`; the real answer's tool names are held in memory only. `GatewayTotals.shadow_compared_calls`/`shadow_agreed_calls` (migration `0026`). Report: *Same next action* per target; CLI calibration column; the Sessions shadow line gives the verdict (none for shadows recorded before verdicts). Measurement happens in observer mode; a local trial alongside router mode is a follow-up. Visual check folded into L6's | done |
| L6 | Settings: the local models list and the per-kind dials, with each dial's shadow evidence beside it | L2, L5 | Interface tests; control-plane validation tests; a visual check. Settings → Local models: a table of models (logical id, context, tool calls, images) with each one's last-7-days evidence (served, same next action), and three dials (share %, model or smallest-that-fits, subagent types), validated like the control plane (engine names, duplicates, context ≥ 1,024, an agent-turn share needs a tools model). Visual check on a seeded stack (desktop and phone): it caught the editor crashing on a config without the new fields, now defaulted (with a test); also showed L5's *Same next action* column | done |
| L7 | End to end: Claude Code's Explore subagent served by a real local model through the gateway, measured against the cloud | L1–L4 | Opt-in test green on a capable local model; numbers recorded in FINDINGS. `test_router_mode_serves_an_explore_subagent_on_a_local_model` (`THEYGENT_E2E_LOCAL_MODEL`): green twice on Qwen3-4B (llama.cpp) — both subagent turns local at no API spend (about 7.2K-token prompts; 8.7 s for the `Read` call, 1–1.8 s for the answer; $0.015 of Haiku 4.5 avoided per subagent), the task completed and Claude Code's usage equalled the gateway's. The first attempt fell back on the flat 15 s first-output deadline; it now grows with the prompt (200 tokens a second of reading) and the commit is timed from the first output, with tests and mutants caught. `test_a_local_subagent_that_fails_mid_loop_finishes_on_anthropic`: a local turn without a thinking block, then a fallback to Opus 5.5 with thinking on — the API takes it (200) and the subagent finishes ([FINDINGS](spike/FINDINGS.md), Q7) | done |
| L8 | A local trial in router mode: per dial, a **trial** share — the conversations just above the served share — whose requests, once Anthropic has answered them, are also tried on the local model (a shadow run marked `trial`), so router mode gathers the evidence for raising a share; a served local request always pre-empts a trial on the engine; the report groups shadows by the model they ran on | L4, L5, L6 | Policy tests (the trial slice sits above the share and is sticky; none after a local failure), proxy tests (a trial runs after a stay and after an applied Anthropic route, never after a local answer; preemption; the decision's own shadow first), wire tests (validator, share + trial ≤ 100%), control-plane report and retention parity for `shadow-model` (migration `0027`), interface tests, a visual check, and a real router run with a trial on a real local model Built: `LocalShare.trial`, `in_trial` (the slice `[share, share + trial)`), `Plan.trial` held across the loop and warm prompts; the exchange offers its decision's own shadow first, then the trial; `ShadowRun.trial`; trials run whatever `shadow_local` says; a served local request cancels running local shadows and none start while it serves (`Shadows.serving_started`/`serving_ended`). Reports: `shadow-model` grouping in the gateway's and the control plane's reports, the daily aggregates keyed by it (migration `0027`: kept days rebuilt exactly, archived days keyed by their decision's target, where every earlier shadow ran). Interface: a Trial % per dial (validated: 0–100, share + trial ≤ 100, a tools model), the evidence and the Report's Shadow runs card read the new grouping, the timeline names a *Local trial*; visual check on a seeded router stack (desktop and phone). Real run (Qwen3-4B, llama.cpp, Claude Code on Haiku 4.5): the Explore subagent served by Anthropic, its first turn tried locally — answered, the same next action, first output in 6.7 s (19.5 s on a colder engine), 8.7 s in all; the next turn was skipped while the engine was busy, as designed. Ten mutants caught. The run also found that a dated snapshot id (`claude-haiku-4-5-20251001`, as the API names the served model) was unpriced; fixed separately | done |

## Contract extensions in this phase

Each one is deliberate, named here, and covered by tests:

- `PolicyConfig.local_models` (`LocalModel`) and `PolicyConfig.local_routing` (`LocalRouting`,
  `LocalShare`) (L2).
- New `ReasonCode` values `local_share` and `no_local_model_fits` (L2).
- `ShadowRun.agreed` and `ShadowRun.first_output_ms`; `GatewayTotals.shadow_compared_calls` and
  `shadow_agreed_calls` (migration `0026_ai_gateway_shadow_agreement`) (L5).
- `LocalShare.trial`, `ShadowRun.trial` (a trial shadow targets a local model its decision did
  not serve), and the `shadow-model` report grouping, with the daily aggregates keyed by it
  (migration `0027_ai_gateway_shadow_model`) (L8).

The binding enum, the IR graph, and the inference plane's `/v1/*` are unchanged.
