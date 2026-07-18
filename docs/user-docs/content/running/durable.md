# Durable runs

A **durable run** checkpoints each step as it completes, so if the process crashes — or the machine reboots — the run picks up from the last completed step instead of being lost. Durable execution is also what lets a run **pause for a person** at a [human approval](human-in-the-loop.md) node and wait, potentially for days, before continuing.

Most agents don't need this. The everyday interactive path — chatting, running an agent from the Bench — is fast and fully sufficient. Reach for durable mode when a run is long-lived, unattended, or needs to survive interruption.

## What durability gives you

- **Crash-safe resume.** A completed step is journaled. If a crash interrupts the run, the durable runtime replays the completed steps from the journal (it does **not** re-execute them) and continues from where it stopped.
- **Indefinite pauses.** A run can pause at a [human node](human-in-the-loop.md), persist as `waiting`, and resume whenever the input arrives — even across a restart.
- **The advanced node types.** [Subgraphs, loops, and maps](../building/nodes/orchestration.md) and human approval only run durably (see below).

### The execution guarantee, stated honestly

Durability is **exactly-once for completed (journaled) steps**, and **at-least-once for the step that was in flight at the moment of a crash**. That in-flight step re-executes on resume. Read-shaped work — calling a model, an HTTP tool, or an MCP tool — is safe to repeat. If you build an agent whose step has a non-idempotent side effect (say, charging a card), be aware that a crash at exactly the wrong moment can repeat it.

## Which nodes require durable mode

Four node types **only** run on the durable runtime. On the interactive path they are rejected up front with a `durable_required` error:

| Node | Why it needs durability |
|---|---|
| [`human`](human-in-the-loop.md) | Pauses the run indefinitely awaiting input — an in-process wait can't survive a restart. |
| [`subgraph`](../building/nodes/orchestration.md) | Runs another published agent as a resumable child workflow. |
| [`loop`](../building/nodes/orchestration.md) | Repeats a body agent; completed iterations must not re-run after a crash. |
| [`map`](../building/nodes/orchestration.md) | Fans out one child run per element; a crash resumes only the incomplete branches. |

If an agent contains any of these, the Bench shows a single **Run durably** button (labelled *"durable-only — runs on the durable runtime"*) instead of the normal run button.

## Turning durable mode on

Durability is **off by default**. There are two ways to enable it, and they are equivalent — one runtime, two ways to host it.

=== "On your own machine"

    Set the environment variable in your `.env` and restart:

    ```bash
    THEYGENT_DURABLE=1
    ```

    (`true`, `yes`, and `on` also work, case-insensitively.) The control plane then runs the durable runtime **in-process** — there is no separate program to launch. This is the right choice for desktop and single-machine use.

=== "Server / air-gapped deployment"

    Run the standalone **durable worker** as its own process, pointed at the same Postgres:

    ```bash
    uv run --package theygent-worker theygent-worker
    ```

    The worker is **not** started by `make up` — you start it yourself. It is the right choice when you want unattended runs to keep going independently of the API process, or to scale that work across machines.

Either way, the durable runtime keeps its bookkeeping in a **separate `dbos` schema** inside the same Postgres you already use, and migrates it automatically. There is no extra migration step.

!!! note "Interactive runs stay non-durable in both modes"
    Enabling durable mode does **not** change your chats or interactive agent runs. The streaming surfaces — New Chat, the Bench's normal run, and the direct run/invoke endpoints — always execute on the fast in-process path, whether or not durable mode is on. Only unattended fires and the durable-run endpoint use the durable runtime.

## The worker process

When it starts, the standalone worker:

1. Connects the durable runtime to your shared Postgres.
2. Rehydrates the [MCP server](../building/nodes/mcp.md) registry, so `mcp_tool` steps can spawn their servers in the worker's trust domain.
3. Reconciles [cron schedules](triggers.md) to the enabled schedule triggers.
4. Blocks, consuming the durable work queue and recovering any workflows a crash left pending.

It reads `DATABASE_URL` (required) and `THEYGENT_INFERENCE_PLANE_URL` (default `http://127.0.0.1:8081/v1`) — the same variables the control plane uses. See [Environment variables](../reference/environment.md).

## Running an agent durably

### From the interface

Open a published agent's **Run** modal from the [Agents page](../building/saving-agents.md). The Run button is a split control:

- Its main segment runs the agent normally (streaming).
- Its caret menu offers **Run durably** — *"checkpoints each step so it resumes after a crash (no token streaming)."*

A durable run is enqueued and then polled — it does not stream tokens; you watch its progress in the trace waterfall and read the persisted output when it finishes. If the server isn't in durable mode, you get an amber note: *"The control-plane isn't running in durable mode — start it with `THEYGENT_DURABLE=1` to run durably."*

### Over the API

`POST /agents/{id}/durable-runs` runs a published agent durably. It is fire-and-poll: it returns **`202`** with the run id, and you poll [`GET /runs/{id}`](index.md) for progress and output.

```bash
curl -X POST http://localhost:8080/agents/agent.my-agent/durable-runs \
  -H "Content-Type: application/json" \
  -d '{"input": "process the overnight queue"}'
```

The body is the same shape as a normal saved-agent run — `input`, optional `version` or `content_hash` to pin a version, optional `session_id`. The response:

```json
{ "run_id": "…" }
```

A few behaviours worth knowing:

- **It requires durable mode.** Without it, you get `400 durable_required`.
- **The version is pinned at enqueue.** If you don't pin one explicitly, the endpoint pins the content hash it just validated, so a version published between your request and the worker picking it up can't swap the graph out from under you.
- **The run row appears promptly.** The endpoint waits up to ~2 seconds for the run row to exist before returning, so an immediate `GET /runs/{id}` doesn't 404.

## Related pages

- [Human-in-the-loop](human-in-the-loop.md) — the human approval flow, which requires durable mode.
- [Subgraph, loop & map nodes](../building/nodes/orchestration.md) — the other durable-only node types.
- [Triggers & webhooks](triggers.md) — unattended fires become crash-resumable in durable mode.
- [Environment variables](../reference/environment.md) — `THEYGENT_DURABLE` and the worker's variables.
