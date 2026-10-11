# Spike findings

Claude Code 2.1.285, macOS, claude.ai Pro login, 2026-10-03/04. Headless runs (`claude -p`) through
the capture proxy, in a scratch repository. Status per question: ✅ answered · 🟡 partial ·
⏳ needs interactive traffic.

## Design consequences (read this first)

1. **The setup must set `ENABLE_TOOL_SEARCH=true`.** Claude Code turns MCP tool search off for any
   custom `ANTHROPIC_BASE_URL`. With claude.ai connectors loaded, a one-line prompt went from
   **28,455 to 184,191 input tokens (6.5×)**: 201 tools sent upfront instead of 11. Pointing Claude
   Code at *any* gateway without this setting inflates every request. The gateway's setup snippet,
   its managed-settings generator, and the observer report's baseline must all account for it.
2. **Sonnet 5.5 is a drop-in target for Opus 5.5; Haiku 4.5 is not.**
   - Explore subagents rewritten to `claude-sonnet-5-5` returned 200 and finished the task.
   - Rewritten to `claude-haiku-4-5`, every request was rejected, because Claude Code's request shape
     assumes the 5.x surface:
     - adaptive thinking;
     - top-level effort;
     - `max_tokens` 128000 (Haiku's maximum is 64000);
     - per-turn effort in `messages`;
     - mid-conversation `role: system` messages.
   - Even with the top-level fields adapted, the last two remain. They live inside `messages`.
   - **v1 router tiers for main and subagent turns are therefore Opus 5.5 ↔ Sonnet 5.5.** Haiku needs a
     message-rewriting adapter: deferred, and only ever for targets that run no preserved-thinking
     check.
3. **A rejected routed request must never reach Claude Code.** Claude Code's recovery disables the
   rejected capability (thinking, then effort) **for the rest of that conversation**, so one bad
   route degrades every later turn, even after routing goes back to Opus. When retries are exhausted,
   the main agent works around it silently by doing the subagent's work itself on Opus, which costs
   more, or reports a "setup problem" to the user. These 400s arrive before any streaming starts, so
   the gateway can retry on the original model transparently. That retry is a router invariant.
4. **Subagent spawn is a no-penalty switch point, measured.** The same Explore task, 3 requests each
   with near-identical tokens: **$0.1621 on Opus 5.5 vs $0.0837 on Sonnet 5.5 (−48%)**, list price.
   A subagent starts a fresh conversation, so there is no cache to lose.
5. **Claude Code's own cost display assumes the requested model.** With subagents served by Sonnet,
   `modelUsage` still filed every token under `claude-opus-5-5`. Under routing, `/usage` and the
   status-line cost overstate spend. The gateway's report is the source of truth, and the
   documentation must say so.
6. **Seat-allowance savings are measurable per response.** Subscription responses carry the
   `anthropic-ratelimit-unified-*` headers. 5-hour utilization went 0.38 → 0.41 and 7-day
   0.04 → 0.06 over these runs.
7. **Compaction shares the main conversation's cache** (same system prompt and tools; 29K tokens read
   from cache). Routing it elsewhere forfeits that cache, so it stays on the main model.
8. **The desktop app is a different integration.** It ignores `ANTHROPIC_BASE_URL` and
   `settings.json`. It reaches a gateway only through its own "third-party inference" configuration:
   - `inferenceProvider: gateway`, with `inferenceGatewayBaseUrl`;
   - credentials as a static API key, SSO with OIDC tokens the gateway validates, or a credential
     helper;
   - distributed by MDM, or entered under Developer → Configure Third-Party Inference.

   That mode does **not** use a claude.ai subscription, and it runs local sessions only. So **seat
   users can go through the gateway from the CLI and IDEs; desktop users only with an org credential
   upstream.** In that mode:
   - tool search needs the app's `toolSearchEnabled` key (`ENABLE_TOOL_SEARCH` doesn't apply);
   - models come from the gateway's `GET /v1/models`, where an `anthropic_family_tier` field maps a
     model to a tier;
   - the SSO mode is a natural fit for team-mode identity.
9. **Side requests are many calls but little money.** In a real interactive session they were 3 of
   9 calls but **3.7% of spend** ($0.026 of $0.687). The title request costs about $0.005 even on
   Opus. Local routing of side requests cuts API calls and latency, not much cost. The money is in
   main and subagent turns, where the lever is Opus → Sonnet at no-penalty boundaries (point 4).

10. **Request bodies can carry the user's environment outside `messages`.**
    - In an interactive session, a side request carried a top-level `safeguards` field
      (`[{"type": "dangerous_tool_use", "classifier_context": …}]`): the auto-mode
      dangerous-tool-use check's context.
    - That context holds the working and home directories, the username, git state, trusted
      directories, permission rules and prior-turn context.
    - It is more content that must never leave the gateway. The wire types already refuse it.
    - It marks safety-classifier traffic, so **a request carrying `safeguards` is never
      routed.**
    - When building fixtures, every top-level field that isn't a known setting is treated as
      content.

## Proxy fidelity — ✅

Verified against a fake upstream:
- The request body reaching the upstream has the same SHA-256 as the one sent, and response bytes
  are identical.
- Responses are streamed, not buffered (first byte at 8 ms on a 1.8 s stream).
- Error bodies and `x-should-retry` pass through unchanged.
- A client disconnect closes the upstream call.
- No credential value reaches a capture file, even when an upstream echoes one back.

Against the real API:
- **Accounting matches Claude Code's exactly.** On the bug-fix run, cache read 67,247, cache write
  19,474 and output 800 all equal Claude Code's `modelUsage`.
- **Prompt caching works through the proxy,** including a cross-session hit of 9,896 tokens on a new
  session's first request.

## Q1 — headers — ✅

| Class | Hint headers seen |
|---|---|
| `main` | `request-class`, `session-id`, `prompt-id` |
| `subagent` | `request-class`, `agent-id`, `agent-type: Explore`, `prev-tool-durations` (`Read=4;Read=2`), `prompt-id` |
| `compaction` | `request-class`, `compaction: manual` (no `prompt-id`) |
| `auxiliary` | `request-class` and `session-id`; `prompt-id` on some |

### Auxiliary census (first interactive session, CLI)

| Fingerprint | What it is | Shape | Verdict |
|---|---|---|---|
| `b0a8480b0d` | Session title | Its own short system prompt; structured output with a `title` field; no tools, no thinking; 1,195 in → 16 out; ran on **Opus 5.5** | Local candidate: small, standalone, schema-checkable |
| `aae5b1e7c7` | Shares the main conversation's cached prefix (most likely the prompt suggestion) | 14 tools, adaptive thinking; 45,819 in (44,477 from cache) → 59 out | **Never route:** cheap in the cloud thanks to the cache, slow locally |
| `210f65a401` | Quota check | `max_tokens: 1`, one user message ("quota"), no system prompt | **Never route:** it exists to read rate-limit state. It returned a transient 429 (`x-should-retry: true`) |

A full day of use will add more kinds: summaries for `--resume`, classifiers, `/insights` and
others. New fingerprints default to pass-through until reviewed.

- **No auxiliary requests in headless runs.** Titles and prompt suggestions come from interactive
  sessions, so the auxiliary fingerprint census needs a day of real use.
- **Betas:** 13 `anthropic-beta` values per request, 14 with tool search on
  (`advanced-tool-use-2025-11-20`). Forwarding them as an open list is required.
- **Client identity:** the user-agent is `claude-cli/2.1.285 (external, sdk-cli)` and `x-app: cli`.
  The connection probe `HEAD /api/hello` comes from `Bun/1.4.3`.
- **Compression:** Claude Code asks for `gzip, deflate, br, zstd`. The spike forces `identity`
  upstream. The real gateway either does the same or decodes on the side.

## Q2 — request shape and token profile — ✅ (synthetic), ⏳ (real mix)

- **Main:**
  - `claude-opus-5-5` (the Pro default), `max_tokens` 128000, adaptive thinking (display omitted),
    effort `medium`, `context_management`.
  - System prompt in three blocks; block 0 is the attribution block
    (`x-anthropic-billing-header: cc_version=…`), so fingerprints skip it.
  - The last message is often a mid-conversation `role: system` message.
- **Subagent (Explore):** its own 9-tool set and system prompt, same thinking and effort settings.
  It ran on Opus because subagents inherit the session model here.
- **Cache TTL** (corrected by the A4 fixture tests):
  - Claude Code marks main-conversation requests, and side requests sharing that prefix, with
    explicit `"ttl": "1h"` cache markers.
  - Subagent and compaction requests get the 5-minute default.
  - `usage.cache_creation` bills exactly what the markers ask for.
  - This note originally said markers carry no TTL; that was wrong. The census tool wasn't yet
    recording TTLs when the main requests were inspected.
  - Routing consequence: a subagent's cache goes cold after 5 minutes, the main conversation's
    after an hour, so a "cold-cache boundary" depends on the request class. The cost model reads
    the billed split from `usage`.
- **Synthetic totals at list price:** main $2.72 (of which about $1.47 is the single tool-search-off
  request), subagent $0.25, compaction $0.03.

## Q4 — model rewrite behaviour — ✅

See design consequences 2–5. Claude Code accepts a response `model` different from the one it
requested.

## Q5 — local model (Qwen3-4B-Instruct-2507 Q4_K_M on llama.cpp, Apple Silicon) — ✅

The route is Anthropic format → LiteLLM's Anthropic adapter → inference plane `/v1` (logical id
`aux-local`) → llama-server.

- **The route works end to end, with one opt-out.** LiteLLM 1.89.7 sends `openai/` models to the
  OpenAI **Responses API** (`/v1/responses`), which the inference plane doesn't serve (404). The
  adapter needs `litellm.use_chat_completions_url_for_anthropic_messages = True`. That flag is
  process-wide, which suits the gateway's own process.
- **Small requests are fast and well-formed once warm.** A title-style prompt came back in 0.51 s.
  A one-tool request returned a correct structured `tool_use` with `stop_reason: tool_use` in
  1.24 s. Quirk: the adapter adds an empty `text` block before the `tool_use`.
- **Cold start is 62 s** (engine spawn and model load). The gateway must keep its local model warm
  (`lifecycle.keepWarm`), or send side requests to the cloud until it's ready.
- **Claude Code-sized agent turns are not viable locally.**
  - **Subagent requests** (Explore, 6–15K-token prompts): 6/6 answered with structured tool calls,
    but only **1/6 chose the same next action** as the cloud model, at **p50 34 s** (3.6–56 s)
    against about 2.3 s in the cloud. When the cloud model had already finished and answered, the
    local model kept calling tools.
  - **Main requests** (~28K tokens): every one timed out at 180 s. That run was contaminated by the
    point below, so it's an upper bound, not a clean measurement.
  - This confirms the plan: local routing is for small auxiliary requests only.
- **An abandoned non-streaming request keeps the engine busy.** The inference plane's non-streaming
  `/v1/chat/completions` path holds the lease and awaits the engine with no disconnect check. A
  client that times out or disconnects leaves the llama-server slot working until it finishes. Two
  replays of the oversized 184K-token capture kept slots busy long after the client was gone.
  LiteLLM's defaults (600 s timeout, then a retry) doubled one of them. **The gateway's local route
  must therefore stream** (a disconnect then reaches the engine), set an explicit timeout with no
  retries, and refuse prompts above a size cap.
- **The real session-title request replayed locally:** valid JSON with the `title` key,
  `{"title": "tests coverage increase"}` against the cloud's `{"title":"Repo test coverage"}`
  (similarity 0.76), in **1.25 s warm** against the cloud's 1.8 s (23.9 s cold). The adapter dropped
  `output_config.format` and the model followed the prompt's JSON instruction anyway. Production
  should map the schema onto llama.cpp's JSON-schema constraint so validity is guaranteed, not
  hoped for.

## Q7 — an Explore subagent served locally through the gateway (Phase 3, L7) — ✅

Claude Code (the Agent SDK) asked an Explore subagent to read a file and report its contents. The
gateway ran in router mode with `aux-local` (the same Qwen3-4B-Instruct-2507 Q4_K_M on llama.cpp)
listed as a tools model and the subagent dial at 100% for Explore. The main conversation stayed on
Anthropic.

- **It completes, end to end, twice out of two.** Both subagent turns were served locally at no
  API spend; the main conversation got the file's contents and Claude Code's `modelUsage` still
  equalled the gateway's totals.

  | Turn | Prompt | Local time | Output | Haiku 4.5 avoided |
  |---|---|---|---|---|
  | 1 (spawn: calls `Read`) | 7,221 tokens | 8.7–9.1 s | 76 tokens | $0.0076 |
  | 2 (answers from the result) | 7,326 tokens | 1.0–1.8 s | 31–59 tokens | $0.0076 |

  Turn 2 is fast because llama.cpp reuses the shared prefix from turn 1.
- **The first-output deadline must grow with the prompt.** The first attempt used a flat 15 s
  first-output deadline and fell back to the cloud (correctly, with no byte out): the model reads
  the whole prompt before its first token, at about 300 tokens a second on this laptop. The deadline
  now adds the prompt's reading time at 200 tokens a second, and once output starts, the commit (a
  first tool call whole) gets its own deadline from that moment.
- **Anthropic takes over a loop a local model began.** A local model writes no thinking block,
  so a fallback mid-loop sends Anthropic an assistant turn (a `toolu_local_…` tool call) without
  one, with thinking on. Measured with Opus 5.5 and `thinking: adaptive`: a fake local model
  answered the subagent's first turn with a `Read` call and failed the second, which went to
  Opus with that history and got a 200; the subagent finished and usage still matched. The
  gateway needs no history rewrite for it.
- **What this does and doesn't show.** It proves the path: tool definitions, a tool call and its
  result translate both ways and stream, and the fallback and accounting hold. It says nothing
  new about quality: a one-file read is the easiest Explore task, and Q5's 1-in-6 agreement on
  real Explore turns stands for this model. That is why shares start at zero and are raised on
  shadow evidence (*Same next action*), per local model.

## Q8 — a local trial in router mode (Phase 3, L8) — ✅

Router mode, the Explore dial at 0% served and 100% trial, `aux-local` as above; Claude Code on
Haiku 4.5. Anthropic answered every subagent turn; after the first, the gateway tried it on the
local model.

- **It measures without costing anything.** The trial answered with the same next action (a
  `Read` call) both times it ran; first output in 6.7 s on a warm engine (19.5 s colder), 8.7 s in
  all. Claude Code saw nothing of it, and its usage still equalled the gateway's.
- **Trials are samples.** The subagent's next turn ended while the first trial still held the
  engine, so it was skipped: with one engine slot, a trial costs about a turn's reading time, and a
  busy session is tried on a fraction of its turns. That is the intended trade: a served request
  always pre-empts a trial.
- **It found an accounting bug.** The API names the served model by its dated snapshot
  (`claude-haiku-4-5-20251001`); the price book knew only `claude-haiku-4-5`, so every such
  exchange was recorded unpriced. Prices and capabilities now resolve a snapshot to its model.

## Q3 — desktop app — ✅ (it does not use the project settings)

A desktop session in the test project (`entrypoint=claude-desktop`, 09:36:26–09:36:40 UTC) got
responses, but no request reached the proxy. The desktop app uses its own gateway configuration;
see design consequence 8.

## Q6 — identity header alongside a subscription login — ✅

`ANTHROPIC_CUSTOM_HEADERS: X-TheYgent-Gateway-Key: …` arrives on every request next to
`authorization: Bearer <oauth>` and the `oauth-2025-04-20` beta.

## Streaming — ✅

One `ping` per stream. The longest silent gap was 5.1 s (compaction, first byte at 6.9 s). A local
route must still synthesize pings for longer local prompt processing.

## Operational note — nested CLI login

A headless `claude` started from a desktop-app session uses the CLI's own stored login. It had
expired and wouldn't refresh, so `claude auth login` in a terminal fixed it. Synthetic runs use a
clean environment (`env -i`), so desktop-app variables don't leak in.

## Still open

- A fuller auxiliary census from longer interactive use. The first session already shows the
  three main kinds.
- The desktop app through its own third-party-inference gateway mode. It needs an API-key or SSO
  credential, not the subscription.
- Optional: a measured mid-conversation Opus → Sonnet → Opus round trip (cache cold-start cost).
  The documented API behaviour is clear; measuring it costs allowance.
