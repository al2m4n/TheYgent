/* AUTO-GENERATED from packages/ir via pnpm --filter @theygent/ir-types generate. DO NOT EDIT. */

export type Contenthash = string | null;
export type Channel = "data" | "control" | "tool";
export type Condition = string | null;
export type Id = string;
export type Source = string;
export type Sourcehandle = string;
export type Target = string;
export type Targethandle = string;
export type Edges = Edge[];
export type Id1 = string;
export type Metadata = {
  [k: string]: unknown;
} | null;
export type Binding = "mlx" | "vllm" | "llamacpp" | "openai-compatible";
export type Model = string;
export type Source1 = ("hf" | "local-path" | "url") | null;
export type Name = string;
export type Id2 = string;
export type Kind = "activity" | "orchestration" | "boundary";
export type Label = string | null;
export type Id3 = string;
export type Required = boolean;
export type Role = "data" | "control" | "tool";
export type Type = string;
export type In = Port[];
export type Out = Port[];
export type Type1 = string;
export type Nodes = Node[];
export type Schemaversion = string;
export type Kind1 = "builtin";
export type Ref = string;
export type Connection = string;
export type Description = string | null;
export type Idempotencykey = string | null;
export type Kind2 = "http";
export type Method = string | null;
export type Parameterschema = {
  [k: string]: unknown;
} | null;
export type Responsemap = string | null;
export type Template = string | null;
export type Timeoutseconds = number | null;
export type Urltemplate = string | null;
export type Connection1 = string | null;
export type Kind3 = "mcp";
export type Server = string | null;
export type Tool = string;
export type Version = string;
export type View = {
  [k: string]: unknown;
} | null;

/**
 * A saved agent — one JSON document. ``id`` + ``version`` is the registry coordinate;
 * ``content_hash`` (computed over the view-stripped graph) gives content-addressed deploys.
 * ``view`` is the React-Flow layer and is **never** hashed.
 */
export interface IRDocument {
  contentHash?: Contenthash;
  edges?: Edges;
  id: Id1;
  metadata?: Metadata;
  models?: Models;
  name: Name;
  nodes?: Nodes;
  schemaVersion: Schemaversion;
  tools?: Tools;
  version: Version;
  view?: View;
}
/**
 * A directed connection. ``channel: data`` passes a value along the edge;
 * ``channel: control`` is pure sequencing. ``condition`` is reserved for router-driven
 * edges.
 */
export interface Edge {
  channel?: Channel;
  condition?: Condition;
  id: Id;
  source: Source;
  sourceHandle: Sourcehandle;
  target: Target;
  targetHandle: Targethandle;
}
export interface Models {
  [k: string]: ModelBinding;
}
/**
 * A logical model the graph's nodes reference by key — the sovereignty dial.
 *
 * Nodes never hardcode a provider; they name a key in ``IRDocument.models`` and the
 * ``binding`` resolves at runtime through the L0 gateway. ``binding`` names only the engines
 * theygent manages (``mlx``/``vllm``/``llamacpp``) plus the catch-all ``openai-compatible``;
 * ``source`` (where the *weights* come from) is orthogonal and never an engine.
 *
 * ``model`` is the id forwarded to the inference seam. The walker forwards ``model`` as the
 * logical id the inference plane already knows (the logical-id invariant holds — an engine
 * name here is rejected at the seam). ``source`` is optional: it answers a lifecycle question
 * (fetch the weights from where?) that only matters once theygent manages the engine.
 */
export interface ModelBinding {
  binding: Binding;
  model: Model;
  params?: Params;
  source?: Source1;
}
export interface Params {
  [k: string]: unknown;
}
/**
 * One graph node. ``id`` is stable — it is what an OTel span binds to later
 * (stamped on per-node logs as the attach-point). ``config`` is ``type``-specific and
 * validated against a per-``type`` schema in ``validate_graph`` — Pydantic at the boundary,
 * not a free-form dict the walker trusts blindly.
 */
export interface Node {
  config?: Config;
  id: Id2;
  kind: Kind;
  label?: Label;
  ports?: Ports;
  type: Type1;
}
export interface Config {
  [k: string]: unknown;
}
export interface Ports {
  in?: In;
  out?: Out;
}
/**
 * A declared (not inferred) connection point, so the compiler can type-check edges.
 * ``type`` is advisory (``any``/``error``); real port typing lands with the node types
 * that need it.
 *
 * ``required`` is meaningful for **in**-ports: a required in-port must be fed by a ``data`` edge
 * or the graph fails validation; ``required: false`` declares an in-port that may be left unfed
 * and resolves to ``null`` at runtime. The default is required, so an unfed input is a loud
 * validation error rather than a silent ``None`` — the multi-input analogue of the
 * no-silent-pass-through rule. (Ignored on out-ports.)
 *
 * ``role`` is the channel a handle carries: ``data`` (the default — threads a value) or
 * ``control`` (pure ordering: "run after this passes", no value). The editor renders the two
 * distinctly and only allows data→data / control→control connections; an edge's ``channel`` is
 * DERIVED from the handles it joins, not a separate toggle. The walker/compiler dispatch by edge
 * ``channel`` exactly as before — ``role`` is the per-handle declaration the channel follows, so a
 * port that omits ``role`` defaults to ``data`` and is unchanged in meaning.
 */
export interface Port {
  id: Id3;
  required?: Required;
  role?: Role;
  type?: Type;
}
export interface Tools {
  [k: string]: BuiltinTool | HttpTool | McpTool;
}
export interface BuiltinTool {
  kind: Kind1;
  ref: Ref;
}
/**
 * An http/REST/GraphQL tool binding — the workhorse + the substrate for action tools
 * and crawler tools. ``connection`` is the id of an ``http_auth`` connection whose secret the
 * handler injects SERVER-SIDE at step time — never inline in the IR. The optional ``template``
 * names a provider request shape (e.g. ``slack.chat.postMessage``) for the small built-in action
 * set; everything else is the raw http tool config on the node.
 */
export interface HttpTool {
  bodyTemplate?: Bodytemplate;
  connection: Connection;
  description?: Description;
  headers?: Headers;
  idempotencyKey?: Idempotencykey;
  kind: Kind2;
  method?: Method;
  parameterSchema?: Parameterschema;
  responseMap?: Responsemap;
  template?: Template;
  timeoutSeconds?: Timeoutseconds;
  urlTemplate?: Urltemplate;
}
export interface Bodytemplate {
  [k: string]: unknown;
}
export interface Headers {
  [k: string]: string;
}
/**
 * An MCP tool binding. ``tool`` is the named tool the server exposes; the server is reached
 * EITHER by the ``server`` name (a registered ``mcp_server``) OR by a ``connection`` id (an
 * ``mcp_server`` connection, stdio or http, auth via its secret_ref). Exactly one of
 * ``server`` / ``connection`` is set (validated below); no secret in the IR.
 */
export interface McpTool {
  connection?: Connection1;
  kind: Kind3;
  server?: Server;
  tool: Tool;
}
