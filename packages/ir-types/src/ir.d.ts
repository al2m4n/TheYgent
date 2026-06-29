/* AUTO-GENERATED from packages/ir via pnpm --filter @theygent/ir-types generate. DO NOT EDIT. */

export type Contenthash = string | null;
export type Channel = "data" | "control";
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
export type Role = "data" | "control";
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
 * A saved agent — one JSON document (§8.2). ``id`` + ``version`` is the registry
 * coordinate; ``content_hash`` (computed over the view-stripped graph) gives content-addressed
 * deploys. ``view`` is the React-Flow layer and is **never** hashed (§8.6).
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
 * A directed connection (§8.3). ``channel: data`` passes a value along the edge;
 * ``channel: control`` is pure sequencing. ``condition`` is reserved for router-driven
 * edges (the first ``orchestration`` node, deferred — M5 §7).
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
 * A logical model the graph's nodes reference by key (§8.4) — the sovereignty dial.
 *
 * Nodes never hardcode a provider; they name a key in ``IRDocument.models`` and the
 * ``binding`` resolves at runtime through the L0 gateway. ``binding`` names only the engines
 * theygent manages (``mlx``/``vllm``/``llamacpp``) plus the catch-all ``openai-compatible``;
 * ``source`` (where the *weights* come from) is orthogonal and never an engine.
 *
 * ``model`` is the id forwarded to the inference seam. M5 has no registry/lifecycle manager
 * (M5 §7), so the walker forwards ``model`` as the logical id the inference plane already
 * knows (the §9.1.1 logical-id invariant still holds — an engine name here is rejected at
 * the seam). ``source`` is optional: it answers a lifecycle question (fetch the weights from
 * where?) that only matters once theygent manages the engine, which M5 does not — so the
 * trivial M5 graph omits it, while a §8.4 document that includes it still validates.
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
 * One graph node (§8.3). ``id`` is stable — it is what an OTel span binds to later
 * (M5 stamps it on per-node logs, the attach-point). ``config`` is ``type``-specific and
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
 * A declared (not inferred) connection point, so the compiler can type-check edges
 * (§8.3). ``type`` is advisory in M5 (``any``/``error``); real port typing lands with the
 * node types that need it.
 *
 * ``required`` is meaningful for **in**-ports (M10 §3): a required in-port must be fed by a
 * ``data`` edge or the graph fails validation; ``required: false`` declares an in-port that may
 * be left unfed and resolves to ``null`` at runtime. The default is required, so an unfed input
 * is a loud validation error rather than a silent ``None`` — the multi-input analogue of M9's
 * no-silent-pass-through rule. (Ignored on out-ports.)
 *
 * ``role`` (M19 §2.10) is the channel a handle carries: ``data`` (the default — threads a value)
 * or ``control`` (pure ordering: "run after this passes", no value). The editor renders the two
 * distinctly and only allows data→data / control→control connections; an edge's ``channel`` is
 * DERIVED from the handles it joins, not a separate toggle. The walker/compiler dispatch by edge
 * ``channel`` exactly as before — ``role`` is the per-handle declaration the channel follows, so a
 * pre-M19 IR (all ports default ``data``) is unchanged in meaning. This is the ONLY IR-shape
 * addition in M19 (§0/§5).
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
 * An http/REST/GraphQL tool binding (M19 §2.3) — the workhorse + the substrate for action tools
 * (§2.5) and crawler tools (§1.4). ``connection`` is the id of an ``http_auth`` connection (M19
 * §1.1) whose secret the handler injects SERVER-SIDE at step time — never inline in the IR. The
 * optional ``template`` names a provider request shape (e.g. ``slack.chat.postMessage``) for the
 * small built-in action set; everything else is the raw http tool config on the node (§2.3).
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
  urlTemplate?: Urltemplate;
}
export interface Bodytemplate {
  [k: string]: unknown;
}
export interface Headers {
  [k: string]: string;
}
/**
 * An MCP tool binding (M7 + M19 §2.4). ``tool`` is the named tool the server exposes; the
 * server is reached EITHER by the M7 ``server`` name (a registered ``mcp_server``) OR — M19 — by a
 * ``connection`` id (an ``mcp_server`` connection, stdio or http, auth via its secret_ref).
 * Exactly one of ``server`` / ``connection`` is set (validated below); no secret in the IR.
 */
export interface McpTool {
  connection?: Connection1;
  kind: Kind3;
  server?: Server;
  tool: Tool;
}
