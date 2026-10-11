// What each routing reason code means, for the AI Gateway's session timelines. The codes are the
// gateway's closed vocabulary (theygent_ir.gateway.ReasonCode): this record is exhaustive over
// them, so a code added there fails the type check here until it is explained.

import type { GatewayReasonCode } from "./api";

export const REASONS: Record<GatewayReasonCode, { label: string; detail: string }> = {
  mode_passthrough: {
    label: "passthrough",
    detail: "The gateway was in passthrough mode: nothing is decided or recorded.",
  },
  request_refused: {
    label: "refused",
    detail: "The gateway refused the request (for example an unknown gateway key) before deciding.",
  },
  policy_error: {
    label: "policy error",
    detail: "The routing policy failed on this request; it was forwarded unchanged and recorded.",
  },
  not_a_message_request: {
    label: "not a message",
    detail:
      "Only Messages requests are routed (this was token counting, model listing, or similar).",
  },
  safety_check: {
    label: "safety check",
    detail: "Auto mode's safety check carries the user's context and is never routed.",
  },
  unlabeled_request: {
    label: "unlabeled",
    detail: "Claude Code sent no request labels (CLAUDE_CODE_GATEWAY_HINT_HEADERS=1 is off).",
  },
  unrecognized_request_class: {
    label: "unknown class",
    detail: "A request class this gateway does not know; it passes through unrouted.",
  },
  compaction: {
    label: "compaction",
    detail: "Compaction reads the main conversation's cache, so it stays on the same model.",
  },
  quota_check: {
    label: "quota check",
    detail: "Claude Code's startup check reads rate-limit state; it is never routed.",
  },
  shares_main_prefix: {
    label: "rides main cache",
    detail:
      "A side request that starts like the main conversation and rides its cache: cheapest where it is.",
  },
  fingerprint_unreviewed: {
    label: "unreviewed kind",
    detail:
      "A side-request kind nobody has reviewed yet; it passes through until an admin decides.",
  },
  fingerprint_observe_only: {
    label: "observe only",
    detail: "An admin chose to observe this side-request kind without routing it.",
  },
  fingerprint_never_route: {
    label: "never route",
    detail: "An admin marked this side-request kind never to be routed.",
  },
  fingerprint_allow_cheaper: {
    label: "cheaper allowed",
    detail: "An admin allowed this side-request kind to go to a cheaper Claude model.",
  },
  local_candidate: {
    label: "local candidate",
    detail:
      "Small and self-contained (structured output, no tools): a local model could answer it.",
  },
  subagent_at_spawn: {
    label: "subagent spawn",
    detail:
      "A subagent starts with an empty cache, so moving it to another model costs nothing extra.",
  },
  mid_conversation: {
    label: "mid-conversation",
    detail:
      "Inside a tool loop the conversation keeps its segment's model: switching would rebuild the cache.",
  },
  cold_cache_boundary: {
    label: "cold cache",
    detail:
      "The conversation's cache was empty here (start, after compaction, or expired): a free switch point.",
  },
  warm_cache_switch_cost: {
    label: "warm cache",
    detail:
      "Switching now would rebuild the whole cached conversation on the other model, costing more than it saves.",
  },
  follows_warm_cache: {
    label: "follows cache",
    detail:
      "The conversation keeps the model its cache is on until the cache goes cold (compaction, or expiry).",
  },
  target_refused: {
    label: "target refused",
    detail:
      "The target refused this request before answering, or the local model could not answer it, so the gateway sent it to the requested model instead — Claude Code never saw the difference.",
  },
  local_share: {
    label: "local share",
    detail:
      "This conversation is in the share of its kind that the configuration sends to a local model.",
  },
  no_local_model_fits: {
    label: "no local model fits",
    detail:
      "The conversation is in the local share, but no listed local model can take this request (tool calls, images or prompt size), so it stays with Anthropic.",
  },
  target_not_cheaper: {
    label: "target not cheaper",
    detail:
      "The target is not known to cost less than the model Claude Code asked for (or that model has no known price), so moving there would not be shown to save anything.",
  },
  pinned_after_refusal: {
    label: "kept on requested",
    detail:
      "Its target refused an earlier request of this conversation, so the conversation stays on the model Claude Code asked for.",
  },
  warm_switch_pays: {
    label: "switch pays",
    detail:
      "Staying would cost at least the configured margin times rebuilding the cache on the other model, so it moves despite the warm cache.",
  },
  target_rejects_request_shape: {
    label: "target can't take it",
    detail:
      "The cheaper model would reject this request's shape (thinking, effort, output size, or system messages).",
  },
  context_exceeds_target: {
    label: "too long for target",
    detail: "The conversation is longer than the cheaper model's context window.",
  },
  already_on_target: {
    label: "already on target",
    detail: "The request already asks for the model routing would choose.",
  },
  no_target_configured: {
    label: "no target set",
    detail: "Routing would apply here, but the gateway config names no model to route to.",
  },
};

/** A reason's label and explanation; a code this interface does not know shows as itself. */
export function explainReason(code: string): { label: string; detail: string } {
  return REASONS[code as GatewayReasonCode] ?? { label: code, detail: code };
}
