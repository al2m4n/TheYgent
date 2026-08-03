# Sample agents

**Ready-made agents you install in one click** — each one a complete, runnable graph that
demonstrates a core TheYgent pattern. Install them from **Settings → Samples**, pick which of
your models fill each role, then open them on the canvas, run them, change them, or delete them:
an installed sample is an ordinary agent, not a special object. Every sample's name starts with
`sample-` so they stay easy to spot on the Agents page.

!!! tip "Why start here"
    Reading the [Node Reference](../building/nodes/index.md) tells you what each piece does;
    the samples show the pieces **composed** — conditional branches, multi-input joins, tool
    wiring, governance gates — as working graphs you can pull apart in the editor.

## The catalog

### Hybrid reasoner — two models in one run

`sample-hybrid-reasoner` · *local model + API model*

A local triage model classifies every request and emits `{"route": ..., "question": ...}`; a
[router](../building/nodes/router.md) follows the matching handle. Greetings and easy questions
are answered by your **local** model; coding, analysis, and long-form work go to the **API**
model. Two model bindings, one graph, no proxying — each branch calls its model directly.
Seen end to end in [Run Local + Remote Models in One Agent](https://www.youtube.com/watch?v=eHIHABKbwVg) (7:01).

### Private SQL analyst — data privacy by construction

`sample-private-sql-analyst` · *local model + API model · creates the `sample-crm-sqlite`
connection*

The privacy pattern in one graph:

1. The **API model sees only the schema** (it is pasted into the system prompt) and writes one
   SQLite `SELECT`.
2. A deterministic [guardrail](../building/nodes/guardrail.md) (`regex`, allow-mode) refuses any
   statement that does not **start with** `SELECT` (no CTEs, no write verbs) — the blocked branch
   answers with a refusal and the database is never touched. A prefix check is not a SQL parser;
   the second gate is that `mcp-server-sqlite`'s `read_query` tool only accepts `SELECT`
   statements at all.
3. The query runs on a **local** SQLite [MCP server](../mcp/index.md) (`uvx mcp-server-sqlite`)
   against a small demo CRM database the installer seeds for you.
4. A **local model** reads the result rows and answers the question.

The rows never leave your machine; the API provider only ever sees the schema and the question
you typed (so don't paste sensitive data into the question itself).

!!! note "The demo database"
    The installer writes a deterministic SQLite file (8 customers, 20 orders) under the
    control plane's artifact directory (`THEYGENT_ARTIFACT_DIR`). Reinstalling never overwrites
    it. The stdio server needs `uvx` on the control plane's machine — the Docker images ship it.

### Expense approver — human in the loop

`sample-expense-approver` · *local model · durable runtime only*

A local model assesses the risk of an expense request, then the run **waits for a real human
decision** at a [human node](../building/nodes/human.md) — durably: the paused run survives
restarts. Resuming with `{"decision": "approve"}` routes into a receipt + a placeholder
execution step (an `echo` tool — swap it for an [http tool](../building/nodes/tools.md) that
calls your real payment API); `{"decision": "reject"}` routes to a refusal. The shipped wait
times out after **24 hours** (`timeout: 86400`, `onTimeout: "fail"`) — raise it, or set
`timeout` to `null` to wait forever, if your approvals take longer.

!!! warning "Needs the durable runtime"
    Human nodes run only on the durable runtime — start the stack with `THEYGENT_DURABLE=1`
    (the Kubernetes deploy enables it by default) and use **Run durably**. See
    [Durable runs](../running/durable.md) and
    [Human-in-the-loop](../running/human-in-the-loop.md) for the resume flow.

### Tool belt — step tools vs. LLM-driven tools

`sample-tool-belt` · *local model · creates the `sample-countries-graphql` and
`sample-open-meteo` connections*

The two ways to use a tool, side by side, behind a
[rate limit](../building/nodes/ratelimit-quota.md):

- A **GraphQL API turned into an MCP server** (`countries.trevorblades.com`) runs as a flow
  **step**: it always executes, with args bound from the input — the model has no say.
- A **REST API turned into an MCP server** (Open-Meteo, generated from an OpenAPI spec) is
  **wired to the model's `tools` port**: the model decides whether to call it, and works out
  the capital's coordinates itself. Ask about the weather and it calls; ask about the currency
  and it doesn't.

Pick a local model with tool-calling support for this one.

### Second opinion — multi-model consensus

`sample-second-opinion` · *local model + API model*

The same question goes to your local model **and** the API model, each answering
independently (the graph runs its nodes one after another); a local judge then merges the two
answers into one, flagging any disagreement. Multi-provider consensus as a graph — no
orchestration code, and the judge (who sees both answers) runs locally.

### PII firewall — privacy as a router

`sample-pii-firewall` · *local model + API model*

A deterministic PII [guardrail](../building/nodes/guardrail.md) inspects every request
**before any model runs**: clean requests may use the API model; anything containing an email,
SSN-style id, or card-like number is answered by the **local** model instead. The personal data
never leaves the machine — and the run trace shows which path fired. (The default patterns
catch emails, US-SSN-shaped ids, and card-like digit runs; tune `spec.patterns` for your own.)

### Visual inspector — vision in a pipeline

`sample-visual-inspector` · *vision model*

A local vision model inspects an image URL against your acceptance criteria and emits a
minified `{"verdict": "pass"|"fail", "reason": ...}`; a `json_schema` guardrail catches a
malformed verdict (fences, prose) and routes it to **Rejected**, and a router sends well-formed
verdicts down the matching branch. The skeleton of a visual QC line — vision as a *pipeline
step with structured output*, not a chat window.

### Voice desk — voice in, voice out

`sample-voice-desk` · *speech-to-text + local model + text-to-speech*

STT transcribes the audio, a model answers in spoken prose, TTS reads it back. The pickers
offer every registered model with the right modality — **pick local models for all three slots
and the audio never leaves your machine**. Each media step's `err` port routes to an honest
failure output. Input is an audio artifact (`POST /artifacts`) or an audio URL; New Chat's
voice mode drives the same shape end-to-end. Built and used in
[Voice In, Voice Out](https://www.youtube.com/watch?v=leoPQLaWMeU) (5:54).

### Art department — local image generation

`sample-art-department` · *local model + image model*

A local model rewrites your rough brief into one vivid image prompt, and a local diffusion
model paints it. If generation fails (no image engine resident), the `imagine` node's `err`
port routes to an honest text fallback instead of a broken run.

### Handbook Q&A — retrieval as a model capability

`sample-handbook-qa` · *local model + embedding model · creates the `sample-handbook` RAG
source*

The installer creates a retrieval source in your own Postgres (pgvector), seeds it with a
fictional company handbook, and wires it to a local model as a **search tool the model decides
to call** — answers cite the handbook section they came from. Seeding embeds in the
background — check the source's status on the [RAG page](../rag/index.md) after installing; if
it failed (e.g. the embedding model wasn't reachable), upload the handbook document to the
source again from there, or delete the source and reinstall the sample.

### Editorial loop — a self-correcting agent

`sample-editorial-loop` · *local model · installs 2 agents · durable runtime only*

A bounded [loop](../building/nodes/orchestration.md) re-runs a writer-critic child agent —
draft, harsh self-critique, rewrite — until the child approves its own draft or three rounds
are up. The loop is durable (it survives restarts mid-revision) and every round is a separate
child run in the waterfall. The parent pins the child by version: editing the child publishes a
new version, and the parent keeps running the pinned one until you re-pin. The loop's stop
condition reads the child's JSON output — a round that emits something other than the
`{"approved": ..., "draft": ..., "critique": ...}` shape fails the run loudly (the waterfall
shows which round), the platform's no-silent-nonsense rule.

### Batch briefing — durable fan-out with a cost ceiling

`sample-batch-briefing` · *local model · installs 2 agents · durable runtime only · ships a
disabled weekly trigger*

A `map` node fans one child agent out per topic (two at a time, `onError: collect`); a
crashed worker resumes only the unfinished branches. Each **worker** starts with a token-budget
[quota](../building/nodes/ratelimit-quota.md) — quota is scoped per agent over its runs'
recorded usage, and every branch runs the worker agent, so the shared daily budget genuinely
accumulates; over-budget branches return a budget notice instead of a brief. It ships with a
**weekly schedule trigger that is disabled** — arm it with the
[triggers API](../running/triggers.md) (`PATCH /triggers/{id} {"enabled": true}`; the agent's
API dialog shows the trigger and its id) when you actually want unattended Monday-morning
briefs. Like the agent itself, the trigger fires only on the durable runtime.

## Installing

1. Open **Settings → Samples** (admin only — Settings is an admin page; the API itself needs
   the editor role).
2. Fill the **model slots**. *Local model* lists your engine-served chat registrations; *API
   model* lists `openai-compatible` ones — add one under [Remote models](../models/remote.md)
   if the list is empty. The special slots (*Vision*, *Speech-to-text*, *Text-to-speech*,
   *Image*, *Embedding*) filter your registry by the registered modality — a sample's Install
   button stays disabled until every slot it declares is filled.
3. Click **Install** on a sample. The installer creates any MCP connections and RAG sources the
   sample needs (reusing them by name on reinstall), seeds demo data, stamps your chosen models
   into the graph, publishes the agent(s) through the normal registry gate — children first, so
   a composition sample's pins always resolve — and registers any shipped trigger **disabled**.

Everything the API does:

| Method | Path | Purpose |
|---|---|---|
| GET | `/samples` | The catalog with per-sample install state. |
| POST | `/samples/install` | Install samples: `{ids, models: {slot: {logical_id, binding}}}`. |

A sample whose model slots are not filled fails loudly with `400 sample_models_required`, and a
binding outside `mlx | vllm | llamacpp | openai-compatible` with `400 sample_model_invalid` —
nothing is written in either case. Installing an already-installed sample reports
`already_installed` — delete the agent first if you want a fresh copy with different models. If
one sample in a batch fails, its report entry says `error` (that sample persists nothing) and
the rest of the batch still installs.

## Making them yours

- **Edit**: open the agent in the editor and publish new versions like any agent — see
  [Drafts & publishing](../building/saving-agents.md).
- **Duplicate**: export the agent from the Agents page, change the `id`/`name` in the JSON, and
  import it (or paste it into a new agent in the editor's code view).
- **Delete**: from the Agents page. The connections a sample created stay (other agents may use
  them); remove them on the MCP page if you no longer want them.

## Related pages

- [Your first agent](../getting-started/first-agent.md)
- [Node Reference](../building/nodes/index.md)
- [MCP example agents](../mcp/examples.md)
- [Durable runs](../running/durable.md)
