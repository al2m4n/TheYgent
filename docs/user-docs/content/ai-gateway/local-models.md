# Local models

In router mode, the AI Gateway can answer some of Claude Code's work with models you run on your
[inference plane](../concepts/models-and-engines.md), instead of the Anthropic API. A request a
local model answers costs no API spend. How much goes local is your choice, per kind of request,
and you measure a model on your own traffic before it serves any.

!!! note "Measure before you serve"
    A small model follows Claude Code's agent work poorly. In one measurement, a 4-billion-parameter
    model picked the same next step as Claude on 1 real exploration turn in 6. A larger model does
    better, but only your own traffic can say by how much. So every agent share starts at 0%, and a
    **trial** measures a model on conversations Anthropic still answers.

## How it works

Each kind of request has a **dial** on **AI Gateway → Settings → Local models**:

| Dial | Default | What goes local |
|---|---|---|
| Side requests | 100% | The kinds of side request (session titles, for example) an admin reviewed *local model* in the [Catalog](reports.md#catalog-admins) |
| Subagents | 0% | A share of subagents, from their first request to their last. You can limit it to some agent types, such as `Explore` |
| Main conversation | 0% | A share of conversations, from their start or their last compaction |

Each dial has three settings:

- **Share.** The percentage of conversations served by a local model. A conversation is in or out
  of the share the same way every time, and only where its prompt cache is cold anyway: when a
  subagent starts, when a conversation starts, or after compaction. It then stays on that model for
  its whole task, so a conversation never changes model mid-turn. A conversation already running
  when you raise a share moves at its next cold point.
- **Trial.** A further percentage of conversations, just above the share. Anthropic still answers
  them. Afterwards the gateway sends each of their requests to the local model and records whether
  it picked the **same next action** as the real answer: the same tool first, or answering where
  the real answer answered. A trial costs no API spend, works in router mode too, and never delays
  anyone: a request a local model is serving always comes first, and a trial running then is
  dropped. Raising the share by the trial serves exactly the conversations that were tried. The
  share and the trial add up to 100% at most.
- **Model.** A listed model, or *smallest that fits*: the first model in the list that can take the
  request. A subagent or main-conversation turn needs a model that follows tool calls. A request
  with images needs one that takes images. Every model needs the prompt to fit within its context.

When a local model can't take a request, Anthropic answers it before Claude Code sees anything.
That covers an error, no answer in time, an answer that isn't usable, or a prompt that doesn't fit.
The request is marked *target refused*, and the conversation stays with Anthropic until its next
cold point.

## Set it up

### 1. Run a model

Register the model on the inference plane under a [logical model id](../concepts/models-and-engines.md),
such as `coder-medium`. It can be:

- a model you [install and run locally](../models/installing.md) (llama.cpp or MLX), or
- a model served elsewhere, on your own GPU servers for example, registered as a
  [remote, OpenAI-compatible model](../models/remote.md): its URL and, if it needs one, its key.

For subagents and the main conversation, choose a model that follows tool calls well and has a
large context: Claude Code's subagent turns carry about 7,000 to 20,000 tokens, and a main
conversation grows well beyond that.

The gateway reaches the inference plane at `THEYGENT_INFERENCE_PLANE_URL`
(`http://127.0.0.1:8081/v1` by default). A gateway on another machine needs that address.

### 2. List it

On **AI Gateway → Settings → Local models**, choose **Add a local model** and fill in:

| Field | Set it to |
|---|---|
| Logical id | The id you registered on the inference plane |
| Context (tokens) | The context the model is served with: the gateway never sends a larger prompt |
| Max output (tokens) | The most it may write in one subagent or main-conversation turn (16,384 unless you change it). Raise it for a large model that writes whole files |
| Tool calls | Ticked if it follows tool calls well enough for agent work |
| Images | Ticked if it takes images |
| Cost | Optional: what running it costs, in USD per million input and output tokens (your hardware, power or rental, spread over what it serves). Leave both empty and its answers count as costing nothing |

List models small to large: *smallest that fits* picks the first that can take a request. A very
small model for side requests and a medium one for agent work is a common pair.

### 3. Measure it with a trial

Set a trial on the dial you want to grow, for example **Subagents: share 0%, trial 20%**, with
**Only these agent types** set to `Explore` to start with the simplest subagents. Save. Then read
the evidence:

- **Settings → Local models** shows, beside each model, the requests it served in the last week
  and how often it picked the same next action.
- **Report → Shadow runs** shows the same per model: how many copies ran, how many answers were
  usable, the *same next action* rate, and how much it wrote against the model Claude Code asked
  for.
- **Sessions** shows each tried request with *Local trial on …* under it, and the verdict.

Trials are samples. A gateway runs one local trial at a time, and a request that finishes while
one is running is not tried. A busy team therefore has a fraction of its turns tried.

### 4. Raise the share

When the *same next action* rate is one you trust, move the trial into the share: **share 20%,
trial 20%** serves the conversations you measured and starts measuring the next ones. Raise it
step by step. The Report then counts what the local model served as routed, at no API spend, and
shows it against what the same work would have cost on the model Claude Code asked for.

## What always stays with Anthropic

Some requests never go to a local model, whatever the dials say:

- Claude Code's quota check, auto mode's safety checks, side requests that reuse the main
  conversation's cache, and side-request kinds nobody has reviewed.
- **Compaction of a conversation Anthropic serves.** Compaction reads the conversation's cache, so
  it runs where the conversation runs: a conversation on a local model is compacted by that model.
- **Requests that can't be translated faithfully**: documents such as PDFs, Anthropic's server
  tools (web search, code execution) and their results, and images for a model that doesn't take
  them.
- **Token counting** and **model listing**.

The gateway therefore still needs an Anthropic credential: local models extend it, they don't
replace it.

## Limits to know

- **Answers have time limits.** A local model's answer must start within 15 seconds, plus the time
  to read the prompt (counted at 200 tokens a second), or Anthropic answers instead. An answer that
  opens with a tool call is held back until the call is whole, so Anthropic can still step in: the
  model may pause no more than 30 seconds while writing it, and the call must be whole within 5
  minutes of the request. A large file written steadily is waited for; a model that stalls is
  not. Once an answer has gone out, Claude Code gets it to the end. A side request's answer is
  checked whole, so it must be complete within 30 seconds plus the reading time.
- **Answers have length limits.** A subagent or main-conversation turn can write up to the model's
  **max output** (16,384 tokens unless you raise it), and a side request up to 4,096.
- **Claude Code compacts by the model it asked for.** It knows its model's context window (200,000
  tokens or more), not the local model's. A conversation that outgrows the local model goes to
  Anthropic, which reads the whole history without a cache: that request costs more than a cached
  one would.
- **Local compute is priced only if you price it.** Without a **cost**, the Report counts a local
  answer as costing nothing, and the saving it shows is larger than your real saving if your
  models run on hardware you pay for. With one, local answers, trials and what routing would cost
  in observer mode are all counted at it.
- **Concurrency is the engine's.** Each developer's requests reach the model as they come. An engine
  serving one request at a time makes the others wait, and a request that waits past the time limit
  goes to Anthropic. For a team, serve the model with an engine that handles many requests at once
  (vLLM is experimental), or give a llama.cpp model several slots with its `parallel` setting
  ([engine settings](../models/engines.md)). The slots share the context, so raise `ctxSize` with
  them.

## Send as much as you can to one large model

For a team running a large model on its own servers:

1. Register it on the inference plane as a remote model, and list it with its real context and
   **Tool calls** ticked.
2. Measure it first: a trial on **Subagents**, then on **Main conversation**.
3. Set **Subagents** and **Main conversation** to a 100% share, and review the side-request kinds
   you want local as *local model* in the Catalog.
4. Run in **Router** mode.

Every subagent and main-conversation turn then goes to the local model, except what [stays with
Anthropic](#what-always-stays-with-anthropic) and what falls back under the
[limits above](#limits-to-know). Watch **Report → Calls** and the *target refused* reason on the
Sessions tab to see how much still reaches Anthropic, and why.

## Related pages

- [Reports & policy](reports.md) — the figures, the reasons, and the rest of the Settings tab.
- [Installing local models](../models/installing.md) and [Remote models](../models/remote.md) —
  getting a model onto the inference plane.
- [Engines](../models/engines.md) — engine settings, such as parallel slots.
