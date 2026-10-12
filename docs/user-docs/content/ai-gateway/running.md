# Running the gateway

The AI Gateway is the fourth TheYgent service, on port **8082**. It runs the same way as the
others — on a machine with `make up`, in Docker Compose, or in Kubernetes — and in one of two
[modes](index.md#personal-or-team): **personal** (yours, on loopback, no keys) or **team** (shared,
developers identified by gateway keys).

## On your machine

`make up` starts the gateway with the other services, configured by your `.env`. As shipped in
`.env.example` it is a **personal gateway**: it listens on `127.0.0.1:8082`, forwards your own
Claude login or API key, and records from the start, locally.

Read what it recorded with the report command:

```bash
uv run --package theygent-ai-gateway theygent-ai-gateway report --since 24h
```

`--by class,client,model` (also `session`, `served-model`, `decision`, `reason`, `shadow-model`) picks the
groupings, `--since`/`--until` take `30m`, `24h`, `7d`, `2w` or a date, and `--json` prints JSON.
It computes the same totals as the Report tab, with the same setup warnings.

### Attach it to the control plane

To see a gateway on the **AI Gateway** page, it reports to the control plane with a **service
key**:

1. An admin mints one under **AI Gateway → Settings → Service keys → New service key**. It is
   shown once, beside the line to paste into `.env`.
2. In `.env`, set:

    ```bash
    THEYGENT_CONTROL_PLANE_URL=http://127.0.0.1:8080
    THEYGENT_AI_GATEWAY_SERVICE_KEY=tygs_…
    ```

3. `make restart`. The gateway appears on the **Overview** within one heartbeat (30 seconds).

Attached, a gateway forwards without recording until an admin saves a configuration in observer
mode — see [Settings](reports.md#settings-admins).

### Serve other machines (team mode)

A personal gateway accepts requests without a key, so it refuses to listen beyond loopback. To
serve your team from this machine, attach it as above and add:

```bash
THEYGENT_AI_GATEWAY_AUTH_MODE=team
THEYGENT_AI_GATEWAY_HOST=0.0.0.0
```

Every request then needs a developer's gateway key; developers create theirs on **Setup & keys**
and point Claude Code at `http://<this machine>:8082`. If the interface is served from another
origin than `http://localhost:5174`, add it to `THEYGENT_AI_GATEWAY_ADMIN_ORIGINS` so the
Settings tab can reach the gateway.

## Docker Compose

The containerized gateway always runs in team mode, so it starts only with a service key. Bring
the stack up, mint a service key as an admin, put it in `.env` as
`THEYGENT_AI_GATEWAY_SERVICE_KEY`, then:

```bash
docker compose --profile gateway up -d
```

It publishes `:8082`, keeps its state in a Docker volume, and waits for open streams to finish
when it stops. The organization's key can come from `.env`
(`THEYGENT_AI_GATEWAY_ORG_API_KEY`) or from the Settings tab.

## Kubernetes

`kubectl apply -k deploy/k8s` creates the gateway with the rest of the stack, and it waits until
its Secret exists. Mint a service key as an admin, then:

```bash
kubectl -n theygent create secret generic theygent-ai-gateway \
  --from-literal=THEYGENT_AI_GATEWAY_SERVICE_KEY=tygs_… \
  --from-literal=THEYGENT_AI_GATEWAY_ORG_API_KEY=sk-ant-…   # optional
```

The gateway starts on its own once the Secret is there. It scales with load, each replica keeping
its own volume (records not yet delivered, the last known configuration and keys) and a stable
name, so it stays one instance on the Overview. Because there can be several replicas, the
organization's key comes **from the Secret only**: the Settings tab shows it and says so. After
changing the Secret, restart the gateway: `kubectl -n theygent rollout restart statefulset/ai-gateway`.

For developers outside the cluster, expose the `ai-gateway` Service with an Ingress or a load
balancer; in development, `kubectl -n theygent port-forward svc/ai-gateway 8082:8082`.

## The organization's key

Developers billed to the organization reach Anthropic with its key, which the gateway adds in
place of theirs. The key belongs to the gateway alone — it never passes through the control plane
— and it is never shown again once set:

- **From the deployment** — `THEYGENT_AI_GATEWAY_ORG_API_KEY` in `.env`, Compose or the Kubernetes
  Secret. A key set this way cannot be replaced from the page.
- **From the page** — a single gateway (on a machine, or in Compose) also accepts it on
  **AI Gateway → Settings → Organization's Anthropic key**: your browser sends it straight to the
  gateway, which keeps it in its own state directory.

Until a key is set, requests from org-billed developers are refused (`org_key_not_configured`). A
personal gateway has no organization key: everyone uses their own credential.

## Health

| Endpoint | Answers |
|---|---|
| `GET /healthz` | Liveness: `{"status":"ok"}` |
| `GET /readyz` | `200` when ready, `503` while starting or — in team mode — until developers' keys are loaded; with the gateway's id, mode, configuration version and control-plane address |

```bash
curl http://localhost:8082/readyz
```

## Errors the gateway returns

When the gateway itself refuses a request, Claude Code shows the message, and the response carries
an `x-theygent-gateway-error` header naming the reason. Anything else — Anthropic's own errors,
rate limits — comes back exactly as Anthropic sent it.

| Status | Code | What to do |
|---|---|---|
| 401 | `gateway_key_missing` | The gateway is in team mode: add your gateway key to the setup ([Set up Claude Code](setup.md)) |
| 401 | `gateway_key_invalid` | The key is unknown or revoked: create a new one on Setup & keys |
| 503 | `gateway_keys_unavailable` | The gateway has not loaded developers' keys yet — it is starting, or cannot reach the control plane and has none saved. Retry shortly; check its service key |
| 401 | `upstream_credential_missing` | You are billed to your own credential and Claude Code sent none: sign in to Claude Code, or set your API key |
| 503 | `org_key_not_configured` | You are billed to the organization, and the gateway has no organization key: an admin sets it |
| 403 | `endpoint_not_allowed` | The organization's key serves Claude Code's model endpoints only |
| 504 | `upstream_timeout` | Anthropic did not answer in time (retryable) |
| 502 | `upstream_unreachable` | The gateway could not reach Anthropic: check its network |

## Where it keeps its data

A gateway keeps a small state directory — `~/.theygent/ai-gateway` on a machine, a volume in
Compose and Kubernetes (`THEYGENT_AI_GATEWAY_STATE_DIR` moves it): the records it has not
delivered yet (so nothing is lost while the control plane is down), the last known configuration,
developers' keys as one-way digests (never the keys themselves), and a stored organization key.
Every file is readable by its owner only.

The control plane keeps each request in detail for 90 days by default (**AI Gateway → Settings →
Request records**; 0 keeps everything). After that, a whole day at a time moves to a compressed
archive in the same database. Reports read archived days from daily totals that keep every
grouping, so no figure changes. Only those days' per-request timelines leave the Sessions tab,
and the card lists each archived day for download. A day's records arrive as JSON lines, one
record each, exactly as the gateway sent it.

## Related pages

- [Environment variables](../reference/environment.md#ai-gateway) — every gateway setting.
- [Set up Claude Code](setup.md) — the client side.
- [Running TheYgent](../getting-started/running.md) — the other services.
