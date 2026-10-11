"""The routing policy: what the gateway would do with a request.

In observer mode a decision is recorded and never applied — the request is forwarded exactly as
it arrived — and the report says what routing would have saved and why each request was or was not
a candidate. Router mode applies the same decisions (see ``routing``).

The rules follow what the spike measured:

* some traffic is never routed: requests carrying ``safeguards`` (auto mode's safety check),
  compaction (it reads the main conversation's cache), the quota check, side requests that ride
  the main conversation's cache, and side requests whose fingerprint is not reviewed;
* a subagent may move at spawn, where its cache is cold anyway;
* the main conversation may move only where its cache is cold (start, after compaction, after
  the cache expired), and only if the config names a main target;
* a decision holds until the conversation's next cold boundary: its tool loops and warm new
  prompts stay on the model its cache is on — routed or not — because moving would rebuild the
  whole cached prefix. A warm move (either way) happens only where staying would cost at least
  ``warm_switch_margin`` times that rebuild. Claude Code switching the requested model itself
  starts afresh;
* a target must accept the request's shape: Claude Code's turns use adaptive thinking, effort,
  128K output and mid-conversation system messages, which Haiku 4.5 rejects.

Decisions are made before forwarding (as a router must); the counterfactual cost is added once
the response's usage is known.
"""

from __future__ import annotations

from collections import OrderedDict
from dataclasses import dataclass, replace

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
DEFAULT_MAX_CONVERSATIONS = 10_000
#: Reasons that name the cold boundary a decision was made at; a warm prompt holding that decision
#: drops them rather than claiming a boundary it is not at.
_BOUNDARY_REASONS: frozenset[ReasonCode] = frozenset({"cold_cache_boundary", "subagent_at_spawn"})


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
    #: The target holds none of this conversation's cache yet: moving there means rebuilding it.
    cold: bool = False

    @classmethod
    def stay(cls, *reasons: ReasonCode) -> Plan:
        return cls("stay", reasons)

    @classmethod
    def route(cls, target: Target, *reasons: ReasonCode) -> Plan:
        return cls("route", reasons, target)

    def because(self, *reasons: ReasonCode) -> Plan:
        """The same decision, explained differently."""
        return replace(self, reasons=reasons, cold=False)

    def serves(self, requested: str | None) -> str | None:
        """The model this plan puts the request on."""
        return self.target.model if self.target is not None else requested


@dataclass(frozen=True)
class _Held:
    """The decision in force for a conversation, and the model Claude Code asked for then."""

    plan: Plan
    requested: str | None


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
    """Decides per request, remembering each conversation's decision until its next cold
    boundary so its requests stay on the model their cache is on."""

    def __init__(self, *, max_conversations: int = DEFAULT_MAX_CONVERSATIONS) -> None:
        self._held: OrderedDict[ConversationKey, _Held] = OrderedDict()
        self._max = max_conversations

    def decide(
        self,
        signals: RequestSignals,
        position: Position | None,
        config: GatewayConfig | None,
        *,
        endpoint_is_messages: bool,
        prices: PriceBook | None = None,
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
        return self._conversation(signals, position, policy, prices)

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
        self,
        signals: RequestSignals,
        position: Position | None,
        policy: PolicyConfig,
        prices: PriceBook | None,
    ) -> Plan:
        if position is None or position.key is None:
            return Plan.stay("unlabeled_request")
        requested = signals.body.model if signals.body else None
        held = self._held.get(position.key)
        if held is not None and held.requested != requested:
            # Claude Code moved the conversation itself (a model switch, its fallback model): the
            # cache is cold on the model it asks for now, so this is a free point to decide afresh.
            plan = self._at_cold_boundary(signals, position, policy)
        elif position.boundary == "continuation":
            if held is None:
                return Plan.stay("mid_conversation")
            return held.plan.because("mid_conversation", *held.plan.reasons)
        elif position.boundary == "new_prompt" and held is not None:
            plan = self._warm_prompt(signals, position, policy, prices, held, requested)
        elif position.boundary == "new_prompt":
            # What the conversation was decided under is forgotten: its cache is on the model it
            # asked for, and leaving it would rebuild that.
            plan = Plan.stay("warm_cache_switch_cost")
        else:
            plan = self._at_cold_boundary(signals, position, policy)
        self._remember(position.key, _Held(plan, requested))
        return plan

    def _at_cold_boundary(
        self, signals: RequestSignals, position: Position, policy: PolicyConfig
    ) -> Plan:
        if position.kind in ("subagent", "workflow"):
            why: ReasonCode = (
                "subagent_at_spawn"
                if position.boundary == "conversation_start"
                else "cold_cache_boundary"
            )
            return self._anthropic_target(signals, position, policy.subagent_target, (why,))
        return self._anthropic_target(
            signals, position, policy.main_target, ("cold_cache_boundary",)
        )

    def _warm_prompt(
        self,
        signals: RequestSignals,
        position: Position,
        policy: PolicyConfig,
        prices: PriceBook | None,
        held: _Held,
        requested: str | None,
    ) -> Plan:
        """A new prompt while the cache is warm: stay on the model the cache is on, unless
        staying would cost at least ``warm_switch_margin`` times rebuilding it elsewhere."""
        current = held.plan
        preferred = self._at_cold_boundary(signals, position, policy)
        if preferred.serves(requested) == current.serves(requested):
            if current.action == "route":
                return current.because("follows_warm_cache")
            informative = tuple(r for r in preferred.reasons if r not in _BOUNDARY_REASONS)
            return Plan.stay(*(informative or ("follows_warm_cache",)))
        if self._warm_switch_pays(position, policy, prices, current, preferred, requested):
            moved = preferred.because("warm_switch_pays")
            return replace(moved, cold=moved.action == "route")
        return current.because("warm_cache_switch_cost")

    @staticmethod
    def _warm_switch_pays(
        position: Position,
        policy: PolicyConfig,
        prices: PriceBook | None,
        current: Plan,
        preferred: Plan,
        requested: str | None,
    ) -> bool:
        if prices is None or position.cached_prefix_tokens <= 0:
            return False
        cached = Usage(cache_read_tokens=position.cached_prefix_tokens)
        stay = prices.cost(current.serves(requested), cached)
        move = prices.cost_cold(preferred.serves(requested), cached, ttl_for(position.ttl_seconds))
        if stay is None or move is None:
            return False
        return stay >= policy.warm_switch_margin * move

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

    def refused(self, position: Position | None, requested: str | None) -> None:
        """The target refused a routed request of this conversation: keep it on the requested
        model until its next cold boundary."""
        if position is not None and position.key is not None:
            self._remember(position.key, _Held(Plan.stay("pinned_after_refusal"), requested))

    def _remember(self, key: ConversationKey, held: _Held) -> None:
        self._held[key] = held
        self._held.move_to_end(key)
        while len(self._held) > self._max:
            self._held.popitem(last=False)


def counterfactual_usd(
    plan: Plan, usage: Usage, position: Position | None, prices: PriceBook
) -> float | None:
    """What the request would have cost had the plan been applied. ``None`` for a stay (the
    actual cost is the answer) or when the target cannot be priced."""
    if plan.action != "route" or plan.target is None:
        return None
    if plan.target.kind == "local":
        return 0.0  # no API spend; local compute is not priced here
    if plan.cold:
        # The target holds nothing of this conversation yet: its cache must be built.
        ttl = ttl_for(position.ttl_seconds if position is not None else 0)
        return prices.cost_cold(plan.target.model, usage, ttl)
    return prices.cost(plan.target.model, usage)


def to_decision(plan: Plan, counterfactual: float | None, *, applied: bool = False) -> Decision:
    """The wire decision. An applied one has no counterfactual: what ran is its cost."""
    return Decision(
        action=plan.action,
        target=plan.target,
        reasons=list(plan.reasons),
        counterfactual_usd=None if applied else counterfactual,
        applied=applied,
    )
