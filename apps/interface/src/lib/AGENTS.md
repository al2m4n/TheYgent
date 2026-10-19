# AGENTS.md — interface · `src/lib/`

Framework-light modules the whole SPA shares: the one HTTP seam, streaming, auth, the graph's
I/O boundary, client-side mirrors of backend rules, the transfer zip, notifications and theme.
Rules in [apps/interface/AGENTS.md](../../AGENTS.md) apply first.

## Map

<!-- agents-map: covers=. -->

| File | What it does | Used by |
|------|--------------|---------|
| [`api.ts`](api.ts) | **The only HTTP module.** Per-call base URLs (`controlPlaneUrl()`, `inferenceUrl()`, `gatewayUrl()`: localStorage override → `VITE_*` → localhost), the session bearer from `localStorage["theygent.token"]` (`authHeaders`), JSON error shaping, the `UNAUTHORIZED_EVENT`, the typed `api` object (sections: runs and sessions, artifacts, observability, agents, drafts, connections, triggers, RAG, MCP, crawlers, inference registry and catalog, liveness, credentials, settings, bench, auth, users, AI gateway), `streamRun` / `streamGet`, `flushDraftOnUnload`, `gatewayAdmin` (the AI gateway's own `/admin/*`) | Everything |
| [`sse.ts`](sse.ts) | SSE over fetch + `ReadableStream` (`parseSSEBuffer`, `readSSE`); `[DONE]` is a normal event | `api.ts`, chat, bench |
| [`auth.tsx`](auth.tsx) | `AuthProvider` / `useAuth`: the `/auth/status` boot probe (setup, signed out, signed in, unreachable), sign-in and out, `hasRole` | `main.tsx`, `router.tsx`, `auth/` |
| [`live.ts`](live.ts) | In-memory registry of live run streams (`useSyncExternalStore`) so a stream survives navigating to the run detail | Chat, run detail |
| [`modality.ts`](modality.ts) | The graph's I/O boundary as data: `boundaryOf(ir)`, `composerCapsFor`, `buildRunInput`, `checkRunInput`, `classifyRunOutput` | Chat, Run modal, Test panel, API dialog |
| [`agent.ts`](agent.ts), [`save.ts`](save.ts) | Registry envelope ↔ `IRDocument` (re-attaching `view`), the blank starter graph; Publish (`POST /agents`, add-version on 409) | Editor |
| [`canonical.ts`](canonical.ts) | Structural equality on view-stripped content for the "modified" badge. Never a hash | Editor, tests |
| [`validate.ts`](validate.ts), [`ir-validate.ts`](ir-validate.ts), [`kind.ts`](kind.ts), [`durable.ts`](durable.ts) | Client mirrors of backend rules: `validate_graph`, JSON-schema hints for the Code view, the guardrail's per-instance kind, the durable-only node set | Editor, Test panel, Agents page |
| [`transfer.ts`](transfer.ts) | Builds and unpacks the one-zip export (fflate): `POST /export` from the control plane, `GET /admin/export` from the inference plane, artifact bytes, a manifest; import runs it in reverse | Settings → Import / Export |
| [`gatewayConfig.ts`](gatewayConfig.ts), [`gatewayReasons.ts`](gatewayReasons.ts), [`gatewaySetup.ts`](gatewaySetup.ts) | AI gateway config drafts and early checks; the meaning of every routing reason code (exhaustive over the IR's vocabulary); copy-ready client setups | `components/gateway/` |
| [`notify.tsx`](notify.tsx) | Toasts with stable ids and the global notification center (downloads, ingest) | Everywhere |
| [`theme.tsx`](theme.tsx) | Dark / Light / System, applied before paint | `main.tsx`, profile |
| [`icons.tsx`](icons.tsx), [`iconsFull.ts`](iconsFull.ts) | Curated Lucide icons per node type (sync); the full set as a lazy chunk for the icon picker | Canvas, inspector |
| [`categories.ts`](categories.ts), [`format.ts`](format.ts) | Category colours shared by badges, chips and tables; byte, duration and status formatters | List pages |
| [`timeRange.ts`](timeRange.ts), [`useNow.ts`](useNow.ts), [`useInView.ts`](useInView.ts), [`viewMode.ts`](viewMode.ts) | Time-window filters, a ticking now, the infinite-scroll sentinel, the grid/list preference per page | List pages |
| [`runtypes.ts`](runtypes.ts) | Run, session and trace wire shapes (snake_case lists, camelCase SSE frames) | Runs, sessions, chat |
| [`utils.ts`](utils.ts) | shadcn's `cn` | UI |

## Rules specific to this folder

- A new backend call is a method here, not a `fetch` in a component.
- Mirrors (`validate.ts`, `kind.ts`, `durable.ts`, `gatewayReasons.ts`) change in the same PR as
  their backend counterpart.
- `localStorage` keys used here: `theygent.token`, `theygent.url.control`,
  `theygent.url.inference`, `theygent.url.gateway`, `theygent.theme`, `theygent.ui.*`,
  `theygent.view.<page>`. Never the IR.

## Tests

`tests/sse.test.ts`, `modality.test.ts`, `validate.test.ts`, `ir-validate.test.ts`,
`save.test.ts`, `registry.test.ts`, `transfer.test.ts`, `transfer-api.test.ts`,
`settings-endpoints.test.ts`, `rag-api.test.ts`, `durable-detect.test.ts`,
`gateway-setup.test.ts`.
