# Triggers & webhooks

A **trigger** gives a published agent an **unattended entry point** — a way to run without you sitting at the interface. There are three:

- **Token invoke** — a standing HTTP endpoint you call with a bearer token.
- **Webhooks** — an inbound URL a third-party system POSTs to, authenticated by a signature.
- **Cron schedules** — a recurring timetable that fires the agent on its own.

All three run the **same** graph on the **same** run path as an interactive run; they just start it differently.

## Prerequisites

- **The agent must be published.** A trigger always fires a published, versioned agent — never a pasted graph or a draft. See [Drafts & publishing](../building/saving-agents.md).
- **It runs a pinned version.** A webhook or schedule pins **exactly one** of `version` or `content_hash`, so an unattended deploy always runs an *immutable* artifact and never silently drifts to "latest". Pinning zero or both is a `400 invalid_trigger`, and the pin must resolve when you create the trigger (a dangling pin 404s then, not at fire time). See [Agent versioning](../concepts/versioning.md).

!!! note "Where triggers are managed"
    Webhook and schedule triggers are created and managed over the control-plane API (the endpoints below). The interface shows a **read-only** list of an agent's triggers on the `input` node's inspector panel — there is no in-app create/edit screen for them yet. The curl examples on this page are the way to set them up.

All examples target the control plane at `http://localhost:8080`.

## Token invoke

The simplest unattended entry point: a single endpoint, `POST /agents/{id}/invoke`, gated by one bearer token. Nothing to register — it is always available once the token is set.

**Set the token.** Provide `THEYGENT_INVOKE_TOKEN` in the control plane's environment (see [Environment variables](../reference/environment.md)):

```bash
THEYGENT_INVOKE_TOKEN=a-long-random-secret
```

!!! warning "Deny by default"
    When `THEYGENT_INVOKE_TOKEN` is **unset**, the invoke endpoint is **closed** — every call returns `401`. This is deliberate: an open unattended surface is never the default. Set the token to open it.

**Call it** with the token in the `Authorization` header:

```bash
curl -X POST http://localhost:8080/agents/agent.my-agent/invoke \
  -H "Authorization: Bearer a-long-random-secret" \
  -H "Content-Type: application/json" \
  -d '{"input": "summarize today'\''s incidents"}'
```

The body accepts `input`, an optional `version` or `content_hash` pin, and an optional `session_id`. Unlike an interactive run, invoke defaults to **non-streaming**, so it returns the finished result:

```json
{ "runId": "…", "status": "completed", "output": "…" }
```

A missing or wrong token returns `401 unauthorized`. Every invoke is correlatable afterwards via [`GET /runs/{id}`](index.md).

## Webhooks

A webhook is an inbound URL a third-party system (a CI pipeline, an issue tracker, a form backend) POSTs to. The request is authenticated not by the invoke token but by a **per-webhook signature**, because the caller is an external system rather than a credentialed user.

### 1. Create the webhook trigger

`POST /triggers` with `kind: "webhook"`. The config requires a `secret` — the shared signing key both sides use:

```bash
curl -X POST http://localhost:8080/triggers \
  -H "Content-Type: application/json" \
  -d '{
    "agent_id": "agent.my-agent",
    "kind": "webhook",
    "version": "1.0.0",
    "config": { "secret": "shared-signing-secret" }
  }'
```

This returns `201` with the trigger, including its **id** (which you need for the inbound URL). The `secret` is redacted to `"***"` in every response — store your copy of it safely; it is never readable back.

### 2. Sign and send the payload

The caller POSTs the raw JSON payload to `POST /hooks/{trigger_id}` with header **`X-Theygent-Signature`** set to the **HMAC-SHA256** of the **raw request body**, keyed by the `config.secret`. Both `sha256=<hex>` (a common convention) and a bare `<hex>` digest are accepted, and the comparison is constant-time.

```bash
SECRET='shared-signing-secret'
BODY='{"repo":"acme/app","branch":"main"}'
SIG=$(printf '%s' "$BODY" | openssl dgst -sha256 -hmac "$SECRET" | sed 's/^.* //')

curl -X POST http://localhost:8080/hooks/TRIGGER_ID \
  -H "Content-Type: application/json" \
  -H "X-Theygent-Signature: sha256=$SIG" \
  -d "$BODY"
```

!!! warning "Sign the exact bytes you send"
    The signature must be computed over the **raw body exactly as transmitted** — the same bytes, no reformatting. Signing a pretty-printed copy while sending a minified one (or vice versa) will fail verification.

### 3. The payload becomes the input

The parsed JSON body **is** the agent's run input. A multi-input agent drills into it with [`$in.in.<field>`](../building/input-references.md) — in the example above, `$in.in.repo` and `$in.in.branch`. On success the endpoint returns **`202`** with the run result:

```json
{ "runId": "…", "status": "completed", "output": "…" }
```

Webhook errors are specific:

| Status | Code | Meaning |
|---|---|---|
| 401 | `invalid_signature` | The signature is missing or doesn't match. |
| 404 | `trigger_not_found` | No webhook trigger with that id. |
| 409 | `trigger_disabled` | The trigger exists but is disabled. |
| 400 | `invalid_payload` | The body isn't valid JSON. |

## Cron schedules

A schedule fires the agent on a recurring timetable, with no external caller at all.

### Create the schedule trigger

`POST /triggers` with `kind: "schedule"`. The config requires a `cron` expression, and may include an `input` value passed to the agent on every fire:

```bash
curl -X POST http://localhost:8080/triggers \
  -H "Content-Type: application/json" \
  -d '{
    "agent_id": "agent.my-agent",
    "kind": "schedule",
    "version": "1.0.0",
    "config": {
      "cron": "0 9 * * *",
      "input": "daily digest"
    }
  }'
```

### Cron syntax and timezone

The `cron` field is a standard five-field cron expression:

```text
┌───────────── minute (0–59)
│ ┌─────────── hour (0–23)
│ │ ┌───────── day of month (1–31)
│ │ │ ┌─────── month (1–12)
│ │ │ │ ┌───── day of week (0–6, Sunday = 0)
│ │ │ │ │
0 9 * * *      → every day at 09:00
*/15 * * * *   → every 15 minutes
0 0 * * 1      → every Monday at midnight
30 8 1 * *     → 08:30 on the 1st of every month
```

The expression is validated when you create the trigger — a malformed cron is a `400 invalid_trigger` up front, never a silently never-firing schedule.

!!! note "Schedules are evaluated in UTC"
    Cron times are interpreted in **UTC**, not your local timezone. `0 9 * * *` fires at 09:00 UTC. Convert your intended local time to UTC when you write the expression.

### How firing behaves

- **No backfill.** Due-ness is computed from the last fire, so a long downtime doesn't replay every window you missed — the schedule simply resumes on its next due instant.
- **No retry.** A fire that fails is recorded honestly on its run row, and the schedule advances to the next window. There is no automatic re-attempt.
- **Restart-safe.** Enabled schedules are re-read from the database continuously, so a restart doesn't drop them and doesn't double-fire within a window.

!!! warning "Run the scheduler on one instance (non-durable mode)"
    In the default (non-durable) setup, the schedule dispatcher must run on **exactly one** control-plane instance — two would each fire the same window. In [durable mode](durable.md) this constraint is lifted: durable schedules de-duplicate across instances, so two workers firing the same instant run the agent once.

## Managing triggers

| Action | Endpoint | Notes |
|---|---|---|
| List | `GET /triggers` | Newest first; the `secret` is always redacted to `"***"`. |
| Get one | `GET /triggers/{id}` | — |
| Edit | `PATCH /triggers/{id}` | Only `enabled` and `config` are editable — **kind and the version pin are immutable**. A config edit is re-validated (a webhook can't drop its secret; a schedule can't take a bad cron). Re-pinning to a new version means creating a new trigger. |
| Delete | `DELETE /triggers/{id}` | Returns `204`. Historical runs keep their trigger lineage as a breadcrumb even after the trigger is gone. |

Disable a trigger without deleting it by patching `enabled` to `false`:

```bash
curl -X PATCH http://localhost:8080/triggers/TRIGGER_ID \
  -H "Content-Type: application/json" \
  -d '{"enabled": false}'
```

## Related pages

- [Drafts & publishing](../building/saving-agents.md) — a trigger fires a published, pinned agent.
- [Durable runs](durable.md) — make unattended fires crash-resumable.
- [Referencing inputs](../building/input-references.md) — how a webhook payload maps into the graph.
- [Runs](index.md) — every fire produces a run you can inspect.
- [Environment variables](../reference/environment.md) — `THEYGENT_INVOKE_TOKEN` and the rest.
