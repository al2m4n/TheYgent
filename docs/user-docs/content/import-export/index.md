# Import & export

**Settings → Import / Export** moves an install — or any part of it — between machines as a single zip file. Export packs your agents, runs, traces, chats, MCP servers, RAG sources, and model registrations into one download; import unpacks a bundle on another machine, shows you a preview of exactly what it will create, and only writes after you confirm. Two things never travel in a bundle, by design: **secret values** and **model weights** — the import preview tells you what has to be re-entered or re-downloaded on the target.

You can also export and import a **single agent** as a plain JSON file, straight from the Agents page — see [Drafts & publishing](../building/saving-agents.md#exporting-and-deleting-agents).

## What's in a bundle

The Export card offers seven sections; each maps to a slice of your install:

| Section | What travels |
|---|---|
| **Agents** | Every published agent with **all** its versions (graph + canvas layout), plus your drafts, triggers, and per-agent I/O capture policies. |
| **Runs** | The full run history — status, model, parameters, output, timestamps. |
| **Traces** | Per-run spans and node I/O, plus the audio/image artifacts your runs produced (packed as raw files in the zip). Selecting Traces automatically includes Runs — a trace without its runs would be unreadable. |
| **Chats** | Sessions and their messages, in order. |
| **MCP servers** | Connections and name-keyed MCP server registrations — definitions only. |
| **RAG sources** | Source definitions (name, kind, embedding model, crawl config) — no documents or vectors. |
| **Model registry** | Your model registrations (logical id → binding), the catalog-install provenance needed to re-download weights, and credential **names**. |

!!! warning "What is never in a bundle"
    Bundles carry definitions, never credentials or weights. Specifically excluded, always:

    - **Secret values of any kind** — connection secrets, webhook signing secrets, and MCP env/header *values* (only the key names travel, so the target knows what to ask you for).
    - **API credential values** — the inference plane exports credential *names* only (e.g. `HF_TOKEN`), never values.
    - **Model weights** — registrations re-download their weights on the target, or point at weights you reinstall yourself.
    - **RAG content** — documents, chunks, and vectors stay behind; sources re-ingest on the target.

    There is no way to opt secrets or weights into a bundle. An export zip is safe to store or hand over in the sense that it contains no credential material — but it does contain your prompts, run outputs, and chat history, so treat it with the same care as the install itself.

## Exporting

1. Open **Settings** from the sidebar and switch to the **Import / Export** tab.
2. In the **Export** card, tick the sections you want — **All** is pre-selected. Ticking **Traces** pulls **Runs** along automatically (and unticking Runs drops Traces).
3. Click **Export**. Your browser downloads one file, named like `theygent-export-2026-07-18.zip`.

If an artifact referenced by a trace no longer exists on disk, the export skips it and tells you in a toast — the zip still completes.

!!! note "The zip is assembled in your browser"
    The browser fetches the control-plane sections from `:8080` and the model registry **directly** from your inference plane on `:8081`, then zips them together locally. Registry state never passes through the control plane — the browser is the only place both halves meet, which is why the export lives in the interface rather than behind a single server endpoint.

The curl equivalents fetch the two halves separately. The control plane serves its bundle from `POST /export` (pick any subset of `agents`, `runs`, `traces`, `sessions`, `mcp`, `rag` — an empty or unknown selection is `400 invalid_include`):

```bash
curl -X POST http://localhost:8080/export \
  -H 'Content-Type: application/json' \
  -d '{"include": ["agents", "runs", "traces", "sessions", "mcp", "rag"]}' \
  -o control-plane.json
```

The inference plane serves its registry bundle from `GET /admin/export`:

```bash
curl http://localhost:8081/admin/export -o models.json
```

## Importing

1. In the **Import** card, click **Choose file…** and pick a theygent export zip — or a single exported agent `.json`.
2. Read the **preview**: per-section counts plus prominent warnings — how many models will download weights from Hugging Face, how many connections will need credentials re-entered or re-authorized, which MCP servers need env values, which RAG sources need re-crawling or re-uploading, and the credential **names** the source inference plane had (values never travel — re-enter them on the target). **Nothing is written at this point.**
3. Click **Import…** and confirm in the dialog.
4. The report shows what was created, what was skipped, and every warning. Any model weight downloads the import kicked off appear as progress cards in the notification center (bottom-right), exactly like a catalog install.

### Import rules

- **Existing entities are kept, never overwritten.** Anything whose id (or name, for MCP servers) already exists on the target is skipped. That makes an import **safe to re-run** — importing the same bundle twice changes nothing the second time.
- **Content hashes are recomputed by the server.** An imported agent version is validated and re-hashed exactly like a publish; the hash in the file is never trusted. If the target already has the same agent version with *different* content, that version is reported as a `version_conflict` and skipped — the rest of the bundle still imports.
- **One bad entry never aborts the rest.** An invalid agent version, an unparseable row — each is reported in the warnings list while everything else lands.
- **Ids and timestamps are preserved**, so runs, traces, and chats on the new machine correlate exactly like the originals.

### After an import

Because secrets and weights never travel, some things need your attention on the target:

- **Connections** import without their secret — re-enter it (or re-run the OAuth authorization) on the [MCP page](../mcp/index.md) before the connection works.
- **Webhook triggers** import **disabled** and without a signing secret. Set a new secret (`PATCH /triggers/{id}`), then enable the trigger. Schedule triggers import as they were.
- **MCP servers** import with their env/header values empty — open the server's settings and re-enter them.
- **RAG sources** import empty: re-run the crawl, or re-upload the documents.
- **Model weights** re-download automatically for catalog-installed models (watch the notification center). A model that was registered against a local file path with no install provenance is registered anyway but warns `weights_unavailable` — it will fail at first use until you put weights at that path or reinstall it. See [Moving models to another machine](../models/index.md#moving-models-to-another-machine).

The curl equivalents import the two JSON halves the zip contains (the zip itself has no single server endpoint — the browser unpacks it). A body that isn't a bundle is `400 invalid_bundle`; a bundle from a future TheYgent is `400 unsupported_bundle_version`:

```bash
curl -X POST http://localhost:8080/import \
  -H 'Content-Type: application/json' \
  --data-binary @control-plane.json
```

```bash
curl -X POST http://localhost:8081/admin/import \
  -H 'Content-Type: application/json' \
  --data-binary @models.json
```

Artifacts (the raw files under `artifacts/` in the zip) restore under their original refs with `PUT /artifacts/{ref}` — a ref that already exists on the target is kept, never overwritten (the response says `created: false`):

```bash
curl -X PUT http://localhost:8080/artifacts/art_01J9ZS3NDEKTSV4RRFFQ69G5FA \
  -H 'Content-Type: audio/webm' \
  --data-binary @art_01J9ZS3NDEKTSV4RRFFQ69G5FA
```

## Related pages

- [Drafts & publishing](../building/saving-agents.md) — per-agent Export and Delete on the Agents page
- [Registries](../models/index.md) — the model registry the inference half of a bundle covers
- [MCP servers](../mcp/index.md) — re-entering connection secrets and env values after an import
- [RAG sources](../rag/index.md) — re-ingesting an imported source
- [API reference](../reference/api.md) — `/export`, `/import`, `/admin/export`, `/admin/import`
- [Troubleshooting](../reference/troubleshooting.md) — the import/export error codes
