# Referencing inputs

Most nodes need to read a value that flowed in from an upstream node — the run's prompt, a field of a JSON object, a tool's result. You reference those values with one small token language built around `$in`. It is the same grammar everywhere it appears, and every mistake fails loudly at run time rather than passing through silently. This page covers it in full.

If you are new to how values move between nodes, read [nodes, ports and edges](../concepts/nodes-ports-edges.md) first.

## The idea

Every node reads its inputs through named **in-ports**. A `data` edge feeds a value onto an in-port; the node then references that port with a `$in` token. The token is always resolved **against the node it is written on** — `$in` on an `llm` node means "the value on *this* llm's input port", not some global.

## The three forms

| Token | Means |
|---|---|
| `$in` | The whole value on the node's default in-port, which is named `in`. |
| `$in.<port>` | The whole value on a named in-port. |
| `$in.<port>.<field>` | Drill into a field of that port's value (chain more `.field` steps to go deeper). |

Plus one escape: `$$` is a literal `$`.

The single rule that ties them together: **the first segment after `$in.` is always a port name.** It is never guessed to be a field. This is what makes the grammar unambiguous, and it is the one thing that trips people up — so it gets its own section.

## The default-port rule (why you often write `$in.in.field`)

Most nodes have exactly one input port, and it is named `in`. That naming is deliberate, and it is why `$in` alone works as a shorthand:

- `$in` — sugar for "the value on the default `in` port". Use it when you want the *whole* input.
- `$in.in` — the same thing, written explicitly (port `in`, no further drilling).
- `$in.in.userId` — drill into the `userId` field of the value on port `in`.

So when the run input is a JSON object and you want one field of it, you write `$in.in.<field>` — the first `in` selects the port, the second selects the field. Writing `$in.userId` would instead ask for a *port* named `userId`, which almost certainly doesn't exist, and you would get a loud error suggesting `$in.in.userId` instead.

!!! tip "Rule of thumb"
    Want the whole input? `$in`. Want one field of a JSON input? `$in.in.<field>`.

## Worked examples

### The whole prompt into a message

The run input is a plain string like `"Summarize this: …"`. An `llm` node with the default `in` port references it directly in a message:

```json
{ "role": "user", "content": "$in" }
```

### One field of a structured input

You run the agent with an object as its input:

```json
{ "userId": "u_42", "question": "What is my balance?" }
```

That object lands on the first node's `in` port. To use just the question in a message:

```json
{ "role": "user", "content": "The user asked: $in.in.question" }
```

`$in.in.userId` would give you `"u_42"` — useful, for example, as a [rate-limit](nodes/ratelimit-quota.md) key.

### Drilling into JSON a model produced

A model emits text that happens to be JSON, such as `{"intent": "billing"}`. A downstream node reading `$in.in.intent` gets `"billing"`: while descending a path, any JSON *string* is parsed first, so you can drill into structured output a model returned as text without an intermediate parse step. This is exactly how a [router](nodes/router.md)'s `select` reads a classifier's answer.

### Templating inside longer text

Tokens substitute inline anywhere in a string. In a message you can mix literal text and several tokens:

```text
Hello $in.in.name — your order $in.in.orderId ships today.
```

A resolved value renders as itself if it is a string, or as JSON if it is an object or array. `$$` gives you a literal dollar sign.

## Where you can use it

The same grammar is read by:

- **`llm` message content** — inline in text, and inside the URL of an image part (so an image can come from `$in.in.image`).
- **`tool` and `mcp_tool` `args`** — each value is either a literal or a `$in` reference.
- **`router.select`** — resolves to the *name* of one outgoing handle.
- **`loop.condition`** — a reference over each iteration's output.
- **HTTP tool `urlTemplate`, `headers`, and `bodyTemplate`.**
- **`ratelimit` and `quota` `keyExpr`** — resolves the caller key, e.g. `$in.in.userId`.

Anything in an `args` map that is *not* a `$in` reference is a literal — there is no expression language and no code execution. `{"limit": 10}` sends the number `10`; `{"who": "$in.in.name"}` sends the resolved value.

## When a value is missing

Absence is handled deliberately, and it differs from an outright error.

**A port can legitimately have no value** — an optional in-port left unwired, a port fed only by a branch that wasn't taken this run, or a port explicitly fed `null`. In all of these the port resolves to *absent*, and so does any drill into it. Absent renders as an empty string inline and as JSON `null` in a structured argument. Drilling into an absent value short-circuits to absent rather than erroring, because the author opted into that optionality.

**A missing field on a value that *is* present is still a loud error.** If `$in.in` holds `{"name": "Ada"}` and you write `$in.in.age`, the run fails with a message naming the node, the token, and the fields that *are* available.

## Loud failures

There is no silent literal pass-through and no green run with nonsense. Depending on where the token sits, an unresolved reference surfaces as a template error (in an `llm`), a router error (in a `router`), or binds the tool's `err` port (in a `tool`/`mcp_tool`) — and the message tells you exactly what went wrong:

| Mistake | What you see |
|---|---|
| Bare `$in` on a node with no `in` port | `node '<id>': bare $in needs a default in-port named 'in' … — address one as $in.<port>` |
| A port name that doesn't exist | `node '<id>': no in-port named '<port>'; in-ports: […]` — with `did you mean $in.in.<port>?` when the node has an `in` port |
| A field that doesn't exist on a present value | `node '<id>': cannot resolve $in.… : no field '<field>' in in-port '<port>'; available fields: …` |
| An unknown token root (for example a stray `$name`) | `node '<id>': unknown template token '$…' — the substitution token is $in, $in.<port>, or $in.<port>.<field> …` |

Because these fail at run time, the editor's validation cannot catch every dangling reference in advance — it flags what it can (like an `llm` naming an undeclared model), but a mistyped port name inside a message only shows up when the agent runs. Test-run your agent from the [Agents page](../running/index.md) after wiring it.

## Related pages

- [Nodes, ports and edges](../concepts/nodes-ports-edges.md) — how edges feed in-ports
- [The llm node](nodes/llm.md) — messages, where `$in` is used most
- [The router node](nodes/router.md) — `select` resolves to a handle name via `$in`
- [The tool node](nodes/tools.md) — `args` templating and the `err` port
