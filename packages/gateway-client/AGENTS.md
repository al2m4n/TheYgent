# AGENTS.md — packages/gateway-client

Rules for `packages/gateway-client` (`theygent_gateway_client`) — the one HTTP path from
the control plane and worker to the inference plane. The repo-wide rules in the root
[AGENTS.md](../../AGENTS.md) apply first; the design is documented in
[docs/dev-docs/ir-and-packages.md](../../docs/dev-docs/ir-and-packages.md).

- **Transport-only, and it stays that way.** `GatewayClient` wraps the async OpenAI SDK
  pointed at the inference plane's `/v1/*` surface: it forwards a logical model id plus
  messages/params and hands back chunks/objects. No `Run`, no registry, no model
  resolution, no retry-and-fallback policy — the caller owns run identity and error
  mapping. Nothing run-shaped enters the signatures: callers pass opaque `extra_headers`
  (the control plane puts `x-theygent-run-id` there).
- **Logical model ids only** — this client is part of the plane seam; engine names never
  appear here, and it imports no engine or control-plane code.
- **Errors surface, never swallowed.** A non-200 from inference raises at the `await`,
  *before* any chunk is yielded, so callers can map it to a clean status before
  committing to a streaming response. Keep that ordering when touching streaming paths.
- **Retry policy is the caller's choice:** the worker constructs it with zero SDK retries
  (the durable runtime owns retry — stacking retries is a hazard); don't bake a default
  that fights that.
- **The surface is the data plane, nothing more:** `open_stream`, `complete`, `embed`,
  `transcribe`, `speak`, `generate_image`, `models`. New data-plane capabilities extend
  this file and the inference plane together, as one named contract change, and shapes
  follow what real servers return — not what a spec or one SDK's convention suggests.

Tests: `uv run --package theygent-gateway-client pytest` (param cleaning / chat kwargs).
The end-to-end proof of the seam lives in the control-plane and inference-plane suites.
