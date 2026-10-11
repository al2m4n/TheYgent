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

import hashlib
from collections import OrderedDict
from dataclasses import dataclass, fields, replace

from theygent_ir.gateway import (
    TARGET_CAPABILITIES,
    Decision,
    DecisionAction,
    FingerprintStatus,
    GatewayConfig,
    GatewayMode,
    LocalModel,
    LocalShare,
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


#: Shared with the control plane, which refuses a target no request could go to when it is saved.
CAPABILITIES = TARGET_CAPABILITIES


@dataclass(frozen=True)
class Plan:
    """A decision before the response: what, where, why."""

    action: DecisionAction
    reasons: tuple[ReasonCode, ...]
    target: Target | None = None
    #: The target holds none of this conversation's cache yet: moving there means rebuilding it.
    cold: bool = False
    #: A local trial: the local model this conversation's dial would choose, tried on each of its
    #: requests once the real answer is out (``LocalShare.trial``).
    trial: Target | None = None

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
    if shape.speed == "fast" and not capabilities.fast_mode:
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
            return self._side_request(signals, position, policy, rules, prices)
        return self._conversation(signals, position, policy, prices)

    def _side_request(
        self,
        signals: RequestSignals,
        position: Position | None,
        policy: PolicyConfig,
        rules: dict[str, FingerprintStatus],
        prices: PriceBook | None,
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
            dial = policy.local_routing.side_requests
            if policy.local_target is None and not policy.local_models:
                return Plan.stay("local_candidate", "no_target_configured")
            key = (signals.hints.session_id, fingerprint)
            if not in_share(key, dial.share):
                tried = side_request_model(policy, signals) if in_trial(key, dial) else None
                trial = Target(kind="local", model=tried.model) if tried is not None else None
                return replace(Plan.stay("local_candidate"), trial=trial)
            model = side_request_model(policy, signals)
            if model is None:
                return Plan.stay("local_candidate", "no_local_model_fits")
            return Plan.route(Target(kind="local", model=model.model), "local_candidate")
        # allow_cheaper: a reviewed side request may go to the cheaper Anthropic target.
        return self._anthropic_target(
            signals, position, policy.subagent_target, ("fingerprint_allow_cheaper",), prices
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
            plan = self._at_cold_boundary(signals, position, policy, prices)
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
            plan = self._at_cold_boundary(signals, position, policy, prices)
        self._remember(position.key, _Held(plan, requested))
        return plan

    def _at_cold_boundary(
        self,
        signals: RequestSignals,
        position: Position,
        policy: PolicyConfig,
        prices: PriceBook | None,
        *,
        local: bool = True,
    ) -> Plan:
        subagent = position.kind in ("subagent", "workflow")
        why: ReasonCode = (
            "subagent_at_spawn"
            if subagent and position.boundary == "conversation_start"
            else "cold_cache_boundary"
        )
        dial = policy.local_routing.subagents if subagent else policy.local_routing.main
        eligible = (
            not subagent or not dial.agent_types or (signals.hints.agent_type in dial.agent_types)
        )
        unfit: tuple[ReasonCode, ...] = ()
        trial: Target | None = None
        if local and eligible and in_share(position.key, dial.share):
            model = local_model_for(policy, dial.model, signals, tools=True)
            if model is not None:
                return Plan.route(Target(kind="local", model=model.model), why, "local_share")
            unfit = ("no_local_model_fits",)
        elif local and eligible and in_trial(position.key, dial):
            tried = local_model_for(policy, dial.model, signals, tools=True)
            trial = Target(kind="local", model=tried.model) if tried is not None else None
        target = policy.subagent_target if subagent else policy.main_target
        plan = self._anthropic_target(signals, position, target, (why,), prices)
        plan = plan.because(*plan.reasons, *unfit) if unfit else plan
        return replace(plan, trial=trial)

    def after_local_failure(
        self,
        signals: RequestSignals,
        position: Position | None,
        config: GatewayConfig | None,
        prices: PriceBook | None,
        plan: Plan,
    ) -> Plan:
        """A local model could not take a request: the decision Anthropic's side would have made,
        held for the rest of the conversation's segment (its next cold boundary considers a local
        model again). A side request simply stays."""
        refused = Plan.stay("target_refused", *plan.reasons)
        if position is None or position.key is None or config is None:
            return refused
        if signals.hints.request_class not in ("main", "subagent"):
            return refused
        cloud = self._at_cold_boundary(signals, position, config.policy, prices, local=False)
        cloud = cloud.because("target_refused", *cloud.reasons)
        requested = signals.body.model if signals.body else None
        self._remember(position.key, _Held(cloud, requested))
        return cloud

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
        preferred = self._at_cold_boundary(signals, position, policy, prices)
        if preferred.serves(requested) == current.serves(requested):
            if current.action == "route":
                return current.because("follows_warm_cache")
            informative = tuple(r for r in preferred.reasons if r not in _BOUNDARY_REASONS)
            stay = Plan.stay(*(informative or ("follows_warm_cache",)))
            return replace(stay, trial=preferred.trial)
        if self._warm_switch_pays(
            position, policy, prices, current, preferred, requested, fast=signals.fast
        ):
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
        *,
        fast: bool,
    ) -> bool:
        if prices is None or position.cached_prefix_tokens <= 0:
            return False
        cached = Usage(cache_read_tokens=position.cached_prefix_tokens)
        stay = prices.cost(current.serves(requested), cached, fast=fast)
        move = prices.cost_cold(
            preferred.serves(requested), cached, ttl_for(position.ttl_seconds), fast=fast
        )
        if stay is None or move is None:
            return False
        return stay >= policy.warm_switch_margin * move

    def _anthropic_target(
        self,
        signals: RequestSignals,
        position: Position | None,
        target: str | None,
        why: tuple[ReasonCode, ...],
        prices: PriceBook | None,
    ) -> Plan:
        if target is None:
            return Plan.stay(*why, "no_target_configured")
        requested = signals.body.model if signals.body else None
        if requested is not None and base_model(requested) == base_model(target):
            return Plan.stay(*why, "already_on_target")
        if prices is not None and not costs_less(prices, target, requested):
            return Plan.stay(*why, "target_not_cheaper")
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


def in_share(key: object, share: float) -> bool:
    """Whether a conversation falls in a share: a stable hash of its key, so it lands the same way
    every time it is decided."""
    if share >= 1:
        return True
    if share <= 0:
        return False
    digest = hashlib.sha256(repr(key).encode()).digest()
    return int.from_bytes(digest[:8], "big") / 2**64 < share


def in_trial(key: object, dial: LocalShare) -> bool:
    """Whether a conversation falls in a dial's trial: the slice of conversations just above its
    share, so raising the share by the trial serves exactly the conversations tried."""
    return (
        dial.trial > 0 and not in_share(key, dial.share) and in_share(key, dial.share + dial.trial)
    )


def _prompt_tokens(signals: RequestSignals) -> int:
    return signals.body.shape.body_bytes // 4 if signals.body else 0


def local_model_for(
    policy: PolicyConfig, chosen: str | None, signals: RequestSignals, *, tools: bool
) -> LocalModel | None:
    """The listed local model a request goes to: the chosen one if it can take it, else the
    smallest listed that can (the list is small to large)."""
    size = _prompt_tokens(signals)
    for model in policy.local_models:
        if chosen is not None and model.model != chosen:
            continue
        if tools and not model.tools:
            continue
        if size > model.context_tokens:
            continue
        return model
    return None


def side_request_model(policy: PolicyConfig, signals: RequestSignals) -> LocalModel | None:
    """The local model a reviewed side request goes to: the dial's, else ``local_target`` (a
    Phase 2 configuration's one local model), else the smallest listed that fits."""
    dial = policy.local_routing.side_requests
    tools = bool(signals.body and signals.body.shape.tool_count)
    chosen = dial.model or policy.local_target
    if chosen is not None and chosen not in {m.model for m in policy.local_models}:
        # The classic single local model: no declared tools, the default context.
        classic = LocalModel(model=chosen)
        if tools or _prompt_tokens(signals) > classic.context_tokens:
            return None
        return classic
    return local_model_for(policy, chosen, signals, tools=tools)


def capabilities_of(policy: PolicyConfig | None, model: str) -> LocalModel:
    """What a local model can take: as listed, or the classic single local model's defaults."""
    for listed in policy.local_models if policy is not None else []:
        if listed.model == model:
            return listed
    return LocalModel(model=model)


def costs_less(prices: PriceBook, target: str, requested: str | None) -> bool:
    """Whether ``target`` is known to cost less than ``requested``: no rate higher, one lower. A
    route exists to save, so a model without known rates on either side is never moved to: a
    request asking for a cheaper model (a subagent on Haiku) stays where it is."""
    there, here = prices.rates(target), prices.rates(requested)
    if there is None or here is None:
        return False
    pairs = [(getattr(there, f.name), getattr(here, f.name)) for f in fields(there)]
    return all(t <= h for t, h in pairs) and any(t < h for t, h in pairs)


def counterfactual_usd(
    plan: Plan, usage: Usage, position: Position | None, prices: PriceBook, *, fast: bool = False
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
        return prices.cost_cold(plan.target.model, usage, ttl, fast=fast)
    return prices.cost(plan.target.model, usage, fast=fast)


def to_decision(plan: Plan, counterfactual: float | None, *, applied: bool = False) -> Decision:
    """The wire decision. An applied one has no counterfactual: what ran is its cost."""
    return Decision(
        action=plan.action,
        target=plan.target,
        reasons=list(plan.reasons),
        counterfactual_usd=None if applied else counterfactual,
        applied=applied,
    )
