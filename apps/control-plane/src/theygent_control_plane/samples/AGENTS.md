# AGENTS.md — control plane · `samples/`

The shipped example agents a user installs from Settings → Samples (`GET /samples`,
`POST /samples/install`). Each sample is a complete, runnable agent IR plus what it needs to run
on the user's install: model slots, connections, retrieval sources, seed data, and sometimes child
agents or a disabled trigger. Installed samples become ordinary registry agents. Rules in
[apps/control-plane/AGENTS.md](../../../AGENTS.md) apply first.

## Where it sits

- **Called by:** the `/samples` routes in [app.py](../app.py), whose install handler creates
  connections, RAG sources (ingested through [rag/](../rag/AGENTS.md)), child agents, the agent
  versions and any trigger.
- **Mirrored by:** the marketing site's template pages
  ([apps/web/templates/](../../../../web/AGENTS.md)) and the user docs page
  `docs/user-docs/content/samples/index.md`. A sample added, renamed or removed here changes
  both in the same PR.
- **Exercised by:** the interface's Wizard ⇄ Code parity bar (every node, field, port and edge
  in the samples must be editable from the forms).

## Map

<!-- agents-map: covers=. -->
<!-- agents-map: covers=data -->

| Path | What it does |
|------|--------------|
| [`catalog.py`](catalog.py) | Loads `data/*.json` in the curated order of `_SAMPLE_FILES` (the learning path, not alphabetical) into `SampleSpec`s; `render_ir` stamps the chosen logical model ids into the model slots and resolves `{connection:<key>}` placeholders; `render_connection_config` resolves `{seed:<name>}`; `seed_demo_file` / `_seed_crm_sqlite` build the deterministic demo SQLite database; `seed_document` serves the handbook. |
| [`data/*.json`](data) | One spec per sample: `id`, `title`, `description`, `capabilities`, `durable`, `input_example`, `model_slots`, `connections`, `ir`, and optionally `rag_sources`, `extra_agents` (children pinned by subgraph, loop or map), `trigger` (installed disabled). |
| [`data/handbook.md`](data/handbook.md) | The document the Handbook Q&A sample's retrieval source is seeded with. |

## The samples (in install order)

| Spec | Title | Shows |
|------|-------|-------|
| `hybrid_reasoner.json` | Hybrid reasoner | A local triage model and a router splitting work between a local and an API model |
| `private_sql_analyst.json` | Private SQL analyst | The API model sees only the schema; a rule guardrail checks its SQL; a stdio MCP tool runs it locally |
| `expense_approver.json` | Expense approver | A durable human approval step and a router on the decision (durable) |
| `tool_belt.json` | Tool belt | GraphQL and OpenAPI turned into MCP servers, a tool as a step vs. as an LLM tool, a rate limit |
| `second_opinion.json` | Second opinion | Two models answer independently; a judge joins both inputs |
| `pii_firewall.json` | PII firewall | A PII guardrail before any model; flagged requests stay local |
| `visual_inspector.json` | Visual inspector | A vision model's structured verdict checked by a JSON-schema guardrail |
| `voice_desk.json` | Voice desk | Speech-to-text, an answer, text-to-speech, with media error ports |
| `art_department.json` | Art department | Prompt refinement then local image generation, with a fallback |
| `handbook_qa.json` | Handbook Q&A | Retrieval as an LLM capability over a seeded pgvector source |
| `web_researcher.json` | Web researcher | A crawler node as a web-search tool with a spend cap and result budget |
| `editorial_loop.json` | Editorial loop | A bounded loop over a writer-critic child agent (durable) |
| `batch_briefing.json` | Batch briefing | A map fan-out over a child agent, a token quota, a disabled cron trigger (durable) |

## Invariants specific to this package

- **The IR is a template in exactly two ways**: model slots and `{connection:…}` /
  `{seed:…}` placeholders. Everything else is literal, valid IR that must pass `validate_graph`.
- **Logical model ids are never baked in.** They are an environment binding the user picks at
  install time.
- **Triggers install disabled.** A sample never starts firing on its own.

## Adding a sample

Add `data/<name>.json`, list it in `_SAMPLE_FILES` and in the table above, make sure `tests/test_samples.py` installs and
runs it, then add its page under `apps/web/templates/` and its entry in the user docs samples page.

## Tests

`tests/test_samples.py` (every sample validates, installs and, where its nodes allow, runs against
the fake inference plane).
