# Example agents

One complete, runnable agent per kind of MCP server — a local **stdio** subprocess, a **remote**
HTTP server, a server **generated from an OpenAPI spec**, a server **generated from a GraphQL
endpoint**, and a bonus example where the **model calls the tool on its own** (capability mode).
Every example was run for real against public services before it landed here.

Each example is the body for [`POST /graphs/runs`](../reference/api.md) — the same graph you can
build visually in the [editor](../building/editor.md) (a Tool node with Kind = MCP, wired between
input and output). Run one with:

```bash
curl -X POST http://localhost:8080/graphs/runs \
  -H 'Content-Type: application/json' \
  --data-binary @agent.json
```

Every example references its server by **connection id** (`con_…`). Create the server on the MCP
page first (each example says how), then copy the id from the connection — the API returns it on
creation, and `GET /connections` lists it.

!!! note "Why the id and not the name?"
    Agents reference a connection by its stable id, so renaming the connection — or rotating its
    secret — never changes any agent. The id is the one thing you substitute in these examples.

## Stdio — read a file through a local subprocess

**Setup:** *Add server → Stdio* with command `npx` and args
`-y @modelcontextprotocol/server-filesystem /path/you/allow`. The server is a subprocess on your
machine; it can only see the directory you pass.

The agent takes a file path as input and returns the file's contents. `$in.in` is the run input
(the [input reference grammar](../building/input-references.md)).

```json
{
 "ir": {
  "schemaVersion": "1.0",
  "id": "agt_mcp_stdio_files",
  "name": "mcp-stdio-file-reader",
  "version": "0.1.0",
  "models": {},
  "tools": {},
  "nodes": [
   {"id": "n_in", "type": "input", "kind": "boundary", "config": {},
    "ports": {"in": [], "out": [{"id": "out"}]}},
   {"id": "n_tool", "type": "mcp_tool", "kind": "activity",
    "config": {"connection": "<connection-id>", "tool": "read_text_file",
               "args": {"path": "$in.in"}},
    "ports": {"in": [{"id": "in"}],
              "out": [{"id": "out"}, {"id": "err", "type": "error"}]}},
   {"id": "n_out", "type": "output", "kind": "boundary", "config": {},
    "ports": {"in": [{"id": "in"}], "out": []}}
  ],
  "edges": [
   {"id": "e1", "source": "n_in", "sourceHandle": "out", "target": "n_tool", "targetHandle": "in"},
   {"id": "e2", "source": "n_tool", "sourceHandle": "out", "target": "n_out", "targetHandle": "in"}
  ]
 },
 "input": "/path/you/allow/notes.txt",
 "stream": false
}
```

## Remote — ask a hosted server over streamable-HTTP

**Setup:** *Add server → Remote* with URL `https://mcp.deepwiki.com/mcp` and auth **None**.
DeepWiki is a free public MCP server that answers questions about GitHub repositories — a good
zero-setup way to try a remote server. (A server that needs auth is the same recipe plus an auth
type; an OAuth server additionally gets a **Connect** press.)

Input is a repository name; the agent returns the documentation topics DeepWiki knows for it.
Same graph as above — only the connection and tool change:

```json
   {"id": "n_tool", "type": "mcp_tool", "kind": "activity",
    "config": {"connection": "<connection-id>", "tool": "read_wiki_structure",
               "args": {"repoName": "$in.in"}},
    "ports": {"in": [{"id": "in"}],
              "out": [{"id": "out"}, {"id": "err", "type": "error"}]}}
```

with `"input": "modelcontextprotocol/python-sdk"`.

## OpenAPI — current weather from a generated server

**Setup:** *Add server → OpenAPI*. Spec URL
`https://raw.githubusercontent.com/open-meteo/open-meteo/main/openapi/forecast.yml`, base URL
`https://api.open-meteo.com`, auth **None**, then **Preview** → **Create**. The spec's one
operation carries no `operationId`, so the derived tool is named from its summary:
`Open_Meteo_Weather_Forecast_API`.

The input is an object, drilled per field — `$in.in.latitude` reads the `latitude` key of the run
input. The `current` argument is a plain literal (anything that isn't a `$in` reference passes
through unchanged):

```json
   {"id": "n_tool", "type": "mcp_tool", "kind": "activity",
    "config": {"connection": "<connection-id>", "tool": "Open_Meteo_Weather_Forecast_API",
               "args": {"latitude": "$in.in.latitude",
                        "longitude": "$in.in.longitude",
                        "current": ["temperature_2m", "wind_speed_10m"]}},
    "ports": {"in": [{"id": "in"}],
              "out": [{"id": "out"}, {"id": "err", "type": "error"}]}}
```

with `"input": {"latitude": 52.52, "longitude": 13.405}` — the current weather in Berlin comes
back as JSON.

## GraphQL — a fixed query with variables from the input

**Setup:** *Add server → GraphQL* with endpoint `https://countries.trevorblades.com/graphql`
(a free public API) and auth **None**. The generated server exposes `introspect_schema` and
`run_query`.

Argument values are **whole references or literals** — there is no string interpolation inside
`args`. So the query text is a literal, and the whole `variables` object arrives as the run
input via one reference:

```json
   {"id": "n_tool", "type": "mcp_tool", "kind": "activity",
    "config": {"connection": "<connection-id>", "tool": "run_query",
               "args": {"query": "query($code: ID!) { country(code: $code) { name capital currency } }",
                        "variables": "$in.in"}},
    "ports": {"in": [{"id": "in"}],
              "out": [{"id": "out"}, {"id": "err", "type": "error"}]}}
```

with `"input": {"code": "DE"}` →
`{"country": {"name": "Germany", "capital": "Berlin", "currency": "EUR"}}`.

## Capability mode — the model calls the tool itself

The step-mode examples above run the tool at a fixed place in the graph. In **capability mode**
the tool hangs off the llm's `tools` port instead, and the model decides when to call it and
with what arguments — here it turns "Paris" into coordinates on its own. Reuse the OpenAPI
weather connection from above; `default` must be a chat model registered on your inference
plane.

```json
{
 "ir": {
  "schemaVersion": "1.0",
  "id": "agt_mcp_capability_weather",
  "name": "mcp-capability-weather-assistant",
  "version": "0.1.0",
  "models": {"default": {"binding": "mlx", "model": "<your-model>",
                          "params": {"temperature": 0.0, "maxTokens": 2048}}},
  "tools": {},
  "nodes": [
   {"id": "n_in", "type": "input", "kind": "boundary", "config": {},
    "ports": {"in": [], "out": [{"id": "out", "type": "any"}]}},
   {"id": "n_llm", "type": "llm", "kind": "activity",
    "config": {"model": "default",
               "messages": [{"role": "user", "content": "$in"}],
               "maxToolIterations": 4},
    "ports": {"in": [{"id": "in", "type": "any"},
                     {"id": "tools", "type": "any", "required": false, "role": "tool"}],
              "out": [{"id": "ok", "type": "any"}, {"id": "err", "type": "error"}]}},
   {"id": "n_weather", "type": "mcp_tool", "kind": "activity",
    "config": {"connection": "<connection-id>",
               "tool": "Open_Meteo_Weather_Forecast_API",
               "description": "Get current weather for a latitude/longitude. Pass current=[\"temperature_2m\",\"wind_speed_10m\"]."},
    "ports": {"in": [{"id": "in", "type": "any", "required": false}],
              "out": [{"id": "use", "type": "any", "role": "tool"}]}},
   {"id": "n_out", "type": "output", "kind": "boundary", "config": {},
    "ports": {"in": [{"id": "in", "type": "any"}], "out": []}}
  ],
  "edges": [
   {"id": "e1", "source": "n_in", "sourceHandle": "out",
    "target": "n_llm", "targetHandle": "in", "channel": "data"},
   {"id": "e2", "source": "n_llm", "sourceHandle": "ok",
    "target": "n_out", "targetHandle": "in", "channel": "data"},
   {"id": "e_cap", "source": "n_weather", "sourceHandle": "use",
    "target": "n_llm", "targetHandle": "tools", "channel": "tool"}
  ]
 },
 "input": "What is the current temperature and wind speed in Paris? Use the weather tool.",
 "stream": false
}
```

In the editor this is the violet `use` handle wired into the llm's `tools` port. Note the
capability node's `in` port is declared `"required": false` — nothing feeds it; the model
supplies the arguments.

## Related pages

- [MCP servers](index.md) — setting up every kind of server
- [The mcp_tool node](../building/nodes/mcp.md) — ports, config, step vs capability
- [Input references](../building/input-references.md) — the `$in` grammar used in `args`
