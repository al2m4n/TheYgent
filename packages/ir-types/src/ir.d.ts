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
export type Type = string;
export type In = Port[];
export type Out = Port[];
export type Type1 = string;
export type Nodes = Node[];
export type Schemaversion = string;
export type Kind1 = "builtin";
export type Ref = string;
export type Kind2 = "mcp";
export type Server = string;
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
 */
export interface Port {
  id: Id3;
  required?: Required;
  type?: Type;
}
export interface Tools {
  [k: string]: BuiltinTool | McpTool;
}
export interface BuiltinTool {
  kind: Kind1;
  ref: Ref;
}
export interface McpTool {
  kind: Kind2;
  server: Server;
  tool: Tool;
}
