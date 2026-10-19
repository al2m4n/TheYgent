# AGENTS.md — interface · `src/routes/`

One component per page, wired in [`../router.tsx`](../router.tsx) (TanStack Router) with a role
floor per route. The shell around them is [`Root.tsx`](Root.tsx). Rules in
[apps/interface/AGENTS.md](../../AGENTS.md) apply first; the long-form route descriptions are in
the "Route map" of [interface.md](../../../../docs/dev-docs/interface.md).

## Map

<!-- agents-map: covers=. -->

| Route | Component | Role | What it shows, and what it calls |
|-------|-----------|------|----------------------------------|
| (shell) | [`Root.tsx`](Root.tsx) | any | Sidebar nav (the nav list lives here), Runs/Chats recents, profile footer, notification center; auto-collapses on `/editor` |
| `/` | [`Dashboard.tsx`](Dashboard.tsx) | any | Both planes' `/readyz`, resident engines, KPI tiles (`/stats`), latest runs, chats, agents, models |
| `/agents` | [`Home.tsx`](Home.tsx) | any | Published agents (cards with `GraphPreview`) and the drafts strip; per-agent Run, bench, API access, export, delete |
| `/chat` | [`Chat.tsx`](Chat.tsx) | any | New Chat with a model (direct to the inference plane) or an agent (control-plane runs), through `chat/` |
| `/runs`, `/runs/$runId` | [`RunsList.tsx`](RunsList.tsx), [`RunDetail.tsx`](RunDetail.tsx) | editor | Run history (keyset-paginated, time range) and one run with the embedded waterfall and `ResumePanel` |
| `/sessions`, `/sessions/$sessionId` | [`SessionsList.tsx`](SessionsList.tsx), [`SessionDetail.tsx`](SessionDetail.tsx) | any | Chat sessions; a session reopened as a live conversation against the same target |
| `/editor` | [`Editor.tsx`](Editor.tsx) | editor | The canvas editor over one `IRDocument` (undo/redo reducer; palette · canvas · inspector; Visual ⇄ Code; validation; Test panel; Publish). `?agent&version` opens a version, `?draft` a draft (draft wins). The only full-bleed page |
| `/registries` | [`Registries.tsx`](Registries.tsx) | editor | Installed models and the Hugging Face catalog on the inference plane's `/admin/*`: register, warm, evict, delete, capability badges, fit badges, downloads, the per-model bench |
| `/mcp` | [`Mcp.tsx`](Mcp.tsx) | editor | Every MCP server (name-keyed registrations and `mcp_server` connections): hub installs, OpenAPI/GraphQL-generated servers, OAuth, settings, per-tool runner |
| `/rag` | [`Rag.tsx`](Rag.tsx) | editor | Retrieval sources: upload or crawl, ingest progress, documents, query tester |
| `/gateway` | [`Gateway.tsx`](Gateway.tsx) | any (admin tabs check role) | The AI Gateway page; tabs live in `components/gateway/` |
| `/settings` | [`Settings.tsx`](Settings.tsx) | admin | Platform settings; tabs live in `components/settings/` |

Role floors are UX only: the API refuses regardless. A deep link below the caller's role renders
an in-shell notice.

## Adding a page

Component here, a `createRoute` (with `requireRole` if needed) in `router.tsx`, a nav entry in
`Root.tsx`, wrap content in `Page` (only the editor is full-bleed), queries in `queries.ts`, a test
in `apps/interface/tests/`, and a user docs page.

## Tests

`tests/dashboard.test.tsx`, `home-drafts.test.tsx`, `agents-actions.test.tsx`,
`editor.test.tsx`, `registries.test.tsx`, `mcp-page.test.tsx`, `settings-page.test.tsx`,
`session-continue.test.tsx`, `infinite.test.tsx`, `gateway-*.test.tsx`.
