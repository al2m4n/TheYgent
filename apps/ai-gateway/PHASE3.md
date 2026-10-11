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
| L3 | Tool calling both ways: Anthropic `tools`/`tool_choice` → OpenAI tools; `tool_use`/`tool_result` blocks → assistant `tool_calls` and `tool` messages; streamed `tool_calls` deltas → `tool_use` blocks with `input_json_delta`; thinking and cache markers dropped; images to a model that takes them; server tools and anything else untranslatable | L1 | Translation tests on the recorded subagent and main exchanges (round-trip shapes, streamed tool calls reassembled to the same input), refusal tests | todo |
| L4 | The policy: per-kind share sampled per conversation, the model chosen by fit, cold boundaries only, pinned to Anthropic after a local failure; observer mode records what local routing would do | L2, L3 | Policy tests: share 0 and 100, stickiness across a tool loop, fit (tools, images, context), pin after failure, observer counterfactuals | todo |
| L5 | Shadow runs for agent turns: same-next-action, time to first token and total time recorded per local model; the report and the interface show share, quality and saving per local model and kind | L4, R7 | Report tests; interface tests; a visual check | todo |
| L6 | Settings: the local models list and the per-kind dials, with each dial's shadow evidence beside it | L2, L5 | Interface tests; control-plane validation tests; a visual check | todo |
| L7 | End to end: Claude Code's Explore subagent served by a real local model through the gateway, measured against the cloud | L1–L4 | Opt-in test green on a capable local model; numbers recorded in FINDINGS | todo |

## Contract extensions in this phase

Each one is deliberate, named here, and covered by tests:

- `PolicyConfig.local_models` (`LocalModel`) and `PolicyConfig.local_routing` (`LocalRouting`,
  `LocalShare`) (L2).
- New `ReasonCode` values `local_share` and `no_local_model_fits` (L2).
- Shadow runs record agent-turn agreement and timing (L5).

The binding enum, the IR graph, and the inference plane's `/v1/*` are unchanged.
