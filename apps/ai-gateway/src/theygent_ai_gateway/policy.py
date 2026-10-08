"""The routing policy: what the gateway would do with a request.

In observer mode a decision is recorded and never applied — the request is forwarded exactly as
it arrived. The policy still decides as a router would, so the report can say what routing would
have saved and why each request was or was not a candidate.

The rules follow what the spike measured:

* some traffic is never routed: requests carrying ``safeguards`` (auto mode's safety check),
  compaction (it reads the main conversation's cache), the quota check, side requests that ride
  the main conversation's cache, and side requests whose fingerprint is not reviewed;
* a subagent may move at spawn, where its cache is cold anyway;
* the main conversation may move only where its cache is cold (start, after compaction, after
  the cache expired), and only if the config names a main target; a warm new prompt stays put,
  because a switch would rebuild the whole cached prefix;
* inside a segment, every request inherits the segment's decision — a conversation never switches
  in the middle of a tool loop;
* a target must accept the request's shape: Claude Code's turns use adaptive thinking, effort,
  128K output and mid-conversation system messages, which Haiku 4.5 rejects.

Decisions are made before forwarding (as a router must); the counterfactual cost is added once
the response's usage is known.
"""

from __future__ import annotations

from collections import OrderedDict
from dataclasses import dataclass

from theygent_ir.gateway import (
    Decision,
    DecisionAction,
    FingerprintStatus,
    GatewayConfig,
    GatewayMode,
    PolicyConfig,
    ReasonCode,
    Target,
    Usage,
)

from theygent_ai_gateway.conversations import ConversationKey, Position
from theygent_ai_gateway.costs import PriceBook, base_model, ttl_for
from theygent_ai_gateway.signals import RequestSignals

#: The dry-run defaults when no config has arrived: the measured subagent candidate, nothing else.
#: Observer mode never applies a decision, so a default target only shapes the report.
DEFAULT_POLICY = PolicyConfig(subagent_target="claude-sonnet-5-5")
DEFAULT_MAX_SEGMENTS = 10_000


@dataclass(frozen=True)
class Capabilities:
    """What a target model accepts, for the request-shape check."""

    adaptive_thinking: bool
    effort: bool
    max_output_tokens: int
    mid_conversation_system: bool
    context_tokens: int


#: Observed or published per model. Haiku 4.5's row is the spike's measured rejections.
CAPABILITIES: dict[str, Capabilities] = {
    "claude-fable-5-1": Capabilities(True, True, 128_000, True, 1_000_000),
    "claude-opus-5-5": Capabilities(True, True, 128_000, True, 1_000_000),
    "claude-opus-5": Capabilities(True, True, 128_000, True, 1_000_000),
    "claude-opus-4-8": Capabilities(True, True, 128_000, True, 1_000_000),
    "claude-sonnet-5-5": Capabilities(True, True, 128_000, True, 1_000_000),
    "claude-sonnet-5": Capabilities(True, True, 128_000, False, 1_000_000),
    "claude-haiku-4-5": Capabilities(False, False, 64_000, False, 200_000),
}


@dataclass(frozen=True)
class Plan:
    """A decision before the response: what, where, why."""

    action: DecisionAction
    reasons: tuple[ReasonCode, ...]
    target: Target | None = None

    @classmethod
    def stay(cls, *reasons: ReasonCode) -> Plan:
        return cls("stay", reasons)

    @classmethod
    def route(cls, target: Target, *reasons: ReasonCode) -> Plan:
        return cls("route", reasons, target)


def accepts(target_model: str, signals: RequestSignals) -> bool:
    """Whether an Anthropic target accepts this request's shape. Unknown models are refused:
    no capability data is no evidence the request would work."""
    capabilities = CAPABILITIES.get(base_model(target_model))
    body = signals.body
    if capabilities is None or body is None:
        return False
    shape = body.shape
    if shape.thinking == "adaptive" and not capabilities.adaptive_thinking:
        return False
    if shape.effort is not None and not capabilities.effort:
        return False
    if shape.max_tokens is not None and shape.max_tokens > capabilities.max_output_tokens:
        return False
    return not (shape.has_mid_conversation_system and not capabilities.mid_conversation_system)


def fits(target_model: str, signals: RequestSignals, position: Position | None) -> bool:
    """Whether the request's context fits the target's window (estimated: what the conversation
    has cached, else ~4 bytes per token of body)."""
    capabilities = CAPABILITIES.get(base_model(target_model))
    if capabilities is None:
        return False
    cached = position.cached_prefix_tokens if position else 0
    size = signals.body.shape.body_bytes // 4 if signals.body else 0
    return max(cached, size) <= capabilities.context_tokens


def _standalone_structured(signals: RequestSignals, position: Position | None) -> bool:
    """A side request that asks for structured output with no tools and does not ride the main
    cache — the shape of Claude Code's session title, small enough to answer locally."""
    body = signals.body
    return (
        body is not None
        and body.shape.has_output_schema
        and body.shape.tool_count == 0
        and not (position and position.shares_main_prefix)
    )


class Policy:
    """Decides per request, remembering each conversation segment's decision so its requests
    move together."""

    def __init__(self, *, max_segments: int = DEFAULT_MAX_SEGMENTS) -> None:
        self._segments: OrderedDict[ConversationKey, tuple[int, Plan]] = OrderedDict()
        self._max = max_segments

    def decide(
        self,
        signals: RequestSignals,
        position: Position | None,
        config: GatewayConfig | None,
        *,
        endpoint_is_messages: bool,
    ) -> Plan:
        mode: GatewayMode = config.mode if config is not None else "observer"
        policy = config.policy if config is not None else DEFAULT_POLICY
        rules: dict[str, FingerprintStatus] = (
            {rule.fingerprint: rule.status for rule in config.fingerprints} if config else {}
        )
        if mode == "passthrough":
            return Plan.stay("mode_passthrough")
        if not endpoint_is_messages:
            return Plan.stay("not_a_message_request")
        request_class = signals.hints.request_class
        if request_class == "unlabeled":
            return Plan.stay("unlabeled_request")
        if request_class == "unrecognized":
            return Plan.stay("unrecognized_request_class")
        body = signals.body
        if body is not None and body.safeguards:
            return Plan.stay("safety_check")
        if request_class == "compaction":
            return Plan.stay("compaction")
        if body is not None and body.is_quota_probe:
            return Plan.stay("quota_check")
        if request_class == "auxiliary":
            return self._side_request(signals, position, policy, rules)
        return self._conversation(signals, position, policy)

    def _side_request(
        self,
        signals: RequestSignals,
        position: Position | None,
        policy: PolicyConfig,
        rules: dict[str, FingerprintStatus],
    ) -> Plan:
        if position is not None and position.shares_main_prefix:
            return Plan.stay("shares_main_prefix")
        fingerprint = signals.body.fingerprint if signals.body else None
        status = rules.get(fingerprint, "unreviewed") if fingerprint else "unreviewed"
        candidate: tuple[ReasonCode, ...] = (
            ("local_candidate",) if _standalone_structured(signals, position) else ()
        )
        if status == "never_route":
            return Plan.stay("fingerprint_never_route")
        if status == "unreviewed":
            return Plan.stay("fingerprint_unreviewed", *candidate)
        if status == "observe_only":
            return Plan.stay("fingerprint_observe_only", *candidate)
        if status == "allow_local":
            if policy.local_target is None:
                return Plan.stay("local_candidate", "no_target_configured")
            return Plan.route(Target(kind="local", model=policy.local_target), "local_candidate")
        # allow_cheaper: a reviewed side request may go to the cheaper Anthropic target.
        return self._anthropic_target(
            signals, position, policy.subagent_target, ("fingerprint_allow_cheaper",)
        )

    def _conversation(
        self, signals: RequestSignals, position: Position | None, policy: PolicyConfig
    ) -> Plan:
        if position is None or position.key is None:
            return Plan.stay("unlabeled_request")
        if position.boundary == "continuation":
            remembered = self._segments.get(position.key)
            if remembered is not None and remembered[0] == position.segment:
                plan = remembered[1]
                return Plan(plan.action, ("mid_conversation", *plan.reasons), plan.target)
            return Plan.stay("mid_conversation")
        if position.boundary == "new_prompt":
            plan = Plan.stay("warm_cache_switch_cost")
        elif position.kind in ("subagent", "workflow"):
            plan = (
                self._anthropic_target(
                    signals, position, policy.subagent_target, ("subagent_at_spawn",)
                )
                if position.boundary == "conversation_start"
                else self._anthropic_target(
                    signals, position, policy.subagent_target, ("cold_cache_boundary",)
                )
            )
        else:
            plan = self._anthropic_target(
                signals, position, policy.main_target, ("cold_cache_boundary",)
            )
        self._remember(position.key, position.segment, plan)
        return plan

    def _anthropic_target(
        self,
        signals: RequestSignals,
        position: Position | None,
        target: str | None,
        why: tuple[ReasonCode, ...],
    ) -> Plan:
        if target is None:
            return Plan.stay(*why, "no_target_configured")
        requested = signals.body.model if signals.body else None
        if requested is not None and base_model(requested) == base_model(target):
            return Plan.stay(*why, "already_on_target")
        if not accepts(target, signals):
            return Plan.stay(*why, "target_rejects_request_shape")
        if not fits(target, signals, position):
            return Plan.stay(*why, "context_exceeds_target")
        return Plan.route(Target(kind="anthropic", model=target), *why)

    def _remember(self, key: ConversationKey, segment: int, plan: Plan) -> None:
        self._segments[key] = (segment, plan)
        self._segments.move_to_end(key)
        while len(self._segments) > self._max:
            self._segments.popitem(last=False)


def counterfactual_usd(
    plan: Plan, usage: Usage, position: Position | None, prices: PriceBook
) -> float | None:
    """What the request would have cost had the plan been applied. ``None`` for a stay (the
    actual cost is the answer) or when the target cannot be priced."""
    if plan.action != "route" or plan.target is None:
        return None
    if plan.target.kind == "local":
        return 0.0  # no API spend; local compute is not priced here
    if position is not None and position.boundary == "new_prompt" and position.cache_warm:
        # The target holds nothing of this conversation yet: its cache must be built.
        return prices.cost_cold(plan.target.model, usage, ttl_for(position.ttl_seconds))
    return prices.cost(plan.target.model, usage)


def to_decision(plan: Plan, counterfactual: float | None) -> Decision:
    """The wire decision. Observer mode never applies a decision."""
    return Decision(
        action=plan.action,
        target=plan.target,
        reasons=list(plan.reasons),
        counterfactual_usd=counterfactual,
        applied=False,
    )
