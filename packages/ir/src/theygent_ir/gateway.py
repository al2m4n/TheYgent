"""Wire types between the AI gateway and the control plane.

The AI gateway proxies a coding agent's model traffic (Claude Code → the Anthropic Messages API)
and reports what it saw and what it would have done with each request. These models are the
contract for that report (``GatewayRequestRecord``, sent in a ``GatewayEventBatch``), for the
configuration the control plane serves the gateway (``GatewayConfig``), for the key set the gateway
authenticates developers against (``GatewayKeySet``), for the gateway's heartbeat, and for the
auxiliary-request fingerprint catalog. They travel over the control plane's ``/gateway/*`` API,
which speaks snake_case — so, unlike ``registration.py``, there is no camelCase alias.

The plane boundary is enforced here, in the types. Prompts, code, tool inputs and outputs, error
messages, and credentials stay inside the gateway: a request record carries metadata only, and
every free-form string field is a ``Token`` — at most 128 characters with no whitespace — so a
sentence of prompt text cannot be stored in one even by mistake. Credentials cross only as
SHA-256 digests in the key set.

None of these models is part of an agent's IR, so nothing here touches a ``contentHash``.
"""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import (
    AwareDatetime,
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    model_validator,
)

from theygent_ir.registration import BINDING_NAMES

#: A whitespace-free identifier, model id, version, or vocabulary value. Allows the characters
#: these carry in practice: UUIDs and ULIDs, ``claude-opus-5-5[1m]``, ``2.1.285``, ``sdk-cli``,
#: ``invalid_request_error``. A space anywhere is a validation error — the "no message text" rule.
Token = Annotated[
    str,
    StringConstraints(pattern=r"^[A-Za-z0-9][A-Za-z0-9._:@+/\[\]-]*$", max_length=128),
]
#: An auxiliary-request fingerprint: 16 lowercase hex characters of a SHA-256 digest.
Fingerprint = Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{16}$")]
#: A full SHA-256 hex digest (the only form in which a gateway key crosses the wire).
Sha256Hex = Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{64}$")]

#: Most records a gateway sends in one batch.
MAX_EVENT_BATCH = 500

#: Claude Code's ``x-claude-code-request-class`` vocabulary, plus ``unlabeled`` (the header was
#: absent) and ``unrecognized`` (a value this version does not know; the raw value is kept in
#: ``request_class_raw`` so a new class is visible rather than silently folded into another).
RequestClass = Literal[
    "main", "subagent", "workflow", "compaction", "auxiliary", "unlabeled", "unrecognized"
]
#: Which Anthropic-format endpoint a request hit. Only ``messages`` carries a model turn.
Endpoint = Literal["messages", "count_tokens", "models", "hello", "other"]
#: The credential a request arrived with: a claude.ai login (``oauth``), an Anthropic API key, a
#: TheYgent gateway key, none, or something else.
CredentialKind = Literal["oauth", "api_key", "gateway_key", "none", "other"]
#: How the gateway authenticates upstream: forward the caller's own credential (personal mode and
#: subscription seats) or swap in the organization's Anthropic key (org-billed developers).
CredentialProfile = Literal["passthrough", "org_key"]
#: ``passthrough`` forwards without recording exchanges; ``observer`` records every exchange and
#: what the policy would have done with it, changing nothing; ``router`` also applies the policy's
#: decisions — a routed request's model is the only thing that changes.
GatewayMode = Literal["passthrough", "observer", "router"]
#: How an exchange ended: ``rejected`` means the gateway refused it before forwarding (e.g. an
#: unknown gateway key); ``upstream_broke`` means the upstream stream ended early.
Outcome = Literal[
    "complete", "client_disconnected", "upstream_broke", "transport_error", "rejected"
]
#: Claude Code's compaction trigger values (``x-claude-code-compaction`` and
#: ``x-claude-code-context-compacted``).
CompactionTrigger = Literal["auto", "manual", "reactive"]
#: Review status of an auxiliary-request fingerprint. Anything not reviewed passes through.
FingerprintStatus = Literal[
    "unreviewed", "observe_only", "allow_cheaper", "allow_local", "never_route"
]
#: A TheYgent account's role, as the control plane's identity layer defines it.
AccountRole = Literal["viewer", "editor", "admin"]
DecisionAction = Literal["stay", "route"]
TargetKind = Literal["anthropic", "local"]
#: Why the policy decided what it did. A closed vocabulary: a new reason is a deliberate change
#: to this list, never a free-text string.
ReasonCode = Literal[
    "mode_passthrough",
    "request_refused",
    "policy_error",
    "not_a_message_request",
    "safety_check",
    "unlabeled_request",
    "unrecognized_request_class",
    "compaction",
    "quota_check",
    "shares_main_prefix",
    "fingerprint_unreviewed",
    "fingerprint_observe_only",
    "fingerprint_never_route",
    "fingerprint_allow_cheaper",
    "local_candidate",
    "subagent_at_spawn",
    "mid_conversation",
    "cold_cache_boundary",
    "warm_cache_switch_cost",
    "target_rejects_request_shape",
    "context_exceeds_target",
    "already_on_target",
    "no_target_configured",
    "follows_warm_cache",
    "warm_switch_pays",
    "target_refused",
    "pinned_after_refusal",
]


class _GatewayWire(BaseModel):
    """Base: snake_case on the wire, unknown keys rejected, immutable once built."""

    model_config = ConfigDict(extra="forbid", frozen=True, protected_namespaces=())


def _check_logical_id(model: str | None) -> None:
    if model is not None and model in BINDING_NAMES:
        raise ValueError(
            f"{model!r} is an engine name; a local target is a logical model id "
            "registered on the inference plane"
        )


class Target(_GatewayWire):
    """Where a request would go: an Anthropic model id, or a local logical model id on the
    inference plane (never an engine name)."""

    kind: TargetKind
    model: Token

    @model_validator(mode="after")
    def _local_is_logical(self) -> Target:
        if self.kind == "local":
            _check_logical_id(self.model)
        return self


class Decision(_GatewayWire):
    """The policy's verdict on one request. ``applied`` says whether the gateway acted on it;
    in observer mode it never does."""

    action: DecisionAction
    target: Target | None = None
    reasons: list[ReasonCode] = Field(min_length=1)
    #: Estimated cost (USD, list price) had the decision been applied; ``None`` when unknown, and
    #: always once it was applied — what ran is then the record's actual cost.
    counterfactual_usd: float | None = Field(default=None, ge=0)
    applied: bool = False

    @model_validator(mode="after")
    def _consistent(self) -> Decision:
        if self.action == "route" and self.target is None:
            raise ValueError("a route decision names its target")
        if self.action == "stay" and self.target is not None:
            raise ValueError("a stay decision has no target")
        if self.applied and self.action != "route":
            raise ValueError("only a route decision can be applied")
        if self.applied and self.counterfactual_usd is not None:
            raise ValueError("an applied decision has no counterfactual: its cost is the actual")
        return self


class Usage(_GatewayWire):
    """Token usage as the upstream reported it, with cache writes split by TTL (they are priced
    differently)."""

    input_tokens: int = Field(default=0, ge=0)
    output_tokens: int = Field(default=0, ge=0)
    cache_read_tokens: int = Field(default=0, ge=0)
    cache_write_5m_tokens: int = Field(default=0, ge=0)
    cache_write_1h_tokens: int = Field(default=0, ge=0)


class Timing(_GatewayWire):
    """Milliseconds from the request's arrival at the gateway."""

    headers_ms: int | None = Field(default=None, ge=0)
    first_byte_ms: int | None = Field(default=None, ge=0)
    total_ms: int | None = Field(default=None, ge=0)


class RequestShape(_GatewayWire):
    """Structural facts about a Messages request body, never its content. They drive the
    target-capability checks (e.g. a mid-conversation ``system`` message rules some models out)
    and setup warnings (no deferred tools across many tools means tool search is off)."""

    max_tokens: int | None = Field(default=None, ge=0)
    tool_count: int = Field(default=0, ge=0)
    deferred_tool_count: int = Field(default=0, ge=0)
    message_count: int = Field(default=0, ge=0)
    thinking: Token | None = None
    effort: Token | None = None
    has_output_schema: bool = False
    has_mid_conversation_system: bool = False
    has_context_management: bool = False
    body_bytes: int = Field(default=0, ge=0)


class SeatAllowance(_GatewayWire):
    """A subscription seat's plan usage, read from the ``anthropic-ratelimit-unified-*`` response
    headers. Utilization is a fraction of the window; it is not capped, since the upstream decides
    what values it reports."""

    five_hour_utilization: float | None = Field(default=None, ge=0)
    seven_day_utilization: float | None = Field(default=None, ge=0)
    five_hour_resets_at: AwareDatetime | None = None
    seven_day_resets_at: AwareDatetime | None = None
    status: Token | None = None
    representative_claim: Token | None = None
    overage_status: Token | None = None


class GatewayRequestRecord(_GatewayWire):
    """One exchange the gateway handled: metadata, usage, cost, and the policy's decision.

    Nothing here is content. Error *types* are recorded but never error messages, and the
    identifiers (session, agent, prompt) are the random ids Claude Code sends in its headers.
    """

    id: Token
    gateway_id: Token
    #: The config version the decision was made under; ``None`` before any config arrived.
    config_version: int | None = Field(default=None, ge=1)
    started_at: AwareDatetime
    endpoint: Endpoint
    status: int | None = Field(default=None, ge=100, le=599)
    outcome: Outcome
    error_type: Token | None = None

    developer_id: Token | None = None
    credential_kind: CredentialKind
    credential_profile: CredentialProfile
    client: Token | None = None
    client_version: Token | None = None

    session_id: Token | None = None
    agent_id: Token | None = None
    parent_agent_id: Token | None = None
    agent_type: Token | None = None
    prompt_id: Token | None = None
    request_class: RequestClass
    request_class_raw: Token | None = None
    compaction: CompactionTrigger | None = None
    context_compacted: CompactionTrigger | None = None

    fingerprint: Fingerprint | None = None
    shares_main_prefix: bool | None = None
    shape: RequestShape | None = None

    requested_model: Token | None = None
    served_model: Token | None = None
    stop_reason: Token | None = None
    tool_use_count: int = Field(default=0, ge=0)
    usage: Usage = Field(default_factory=Usage)
    timing: Timing = Field(default_factory=Timing)
    seat_allowance: SeatAllowance | None = None

    #: Estimated cost of what actually ran (USD, list price unless the config overrides rates).
    cost_usd: float | None = Field(default=None, ge=0)
    #: What it would have cost on the model Claude Code asked for, where something else served it
    #: (an applied route). ``None`` means it ran on the requested model: ``cost_usd`` is the answer.
    baseline_usd: float | None = Field(default=None, ge=0)
    pricing_version: Token | None = None
    decision: Decision

    @model_validator(mode="after")
    def _raw_class_only_when_unrecognized(self) -> GatewayRequestRecord:
        if (self.request_class == "unrecognized") != (self.request_class_raw is not None):
            raise ValueError("request_class_raw is set exactly when request_class is unrecognized")
        return self


class GatewayEventBatch(_GatewayWire):
    """Records a gateway sends to ``POST /gateway/events``. Record ids are idempotency keys:
    a batch re-sent after a failed acknowledgement is safe."""

    gateway_id: Token
    records: list[GatewayRequestRecord] = Field(min_length=1, max_length=MAX_EVENT_BATCH)

    @model_validator(mode="after")
    def _one_gateway(self) -> GatewayEventBatch:
        strays = {r.gateway_id for r in self.records} - {self.gateway_id}
        if strays:
            raise ValueError(f"records from other gateways in this batch: {sorted(strays)}")
        return self


class GatewayEventAck(_GatewayWire):
    accepted: int = Field(ge=0)
    duplicates: int = Field(ge=0)


#: Setup problems a report detects from what was recorded:
#:
#: * ``tool_search_off`` — dozens of tools sent with none deferred: the client sends every tool's
#:   schema on every request (a custom base URL turns Claude Code's MCP tool search off unless
#:   ENABLE_TOOL_SEARCH=true);
#: * ``hint_headers_off`` — Claude Code traffic without request labels, so nothing is a routing
#:   candidate (CLAUDE_CODE_GATEWAY_HINT_HEADERS=1);
#: * ``unrecognized_request_class`` — a request class this gateway does not know;
#: * ``unpriced_models`` — calls on models with no known price, left out of the dollar figures.
SetupWarningCode = Literal[
    "tool_search_off", "hint_headers_off", "unrecognized_request_class", "unpriced_models"
]
#: Claude Code sends about a dozen built-in tools, one of them deferred; this many with none
#: deferred means every MCP tool schema went upfront.
TOOL_SEARCH_OFF_MIN_TOOLS = 30


class GatewayTotals(_GatewayWire):
    """What records add up to. One definition, so the gateway's own report and the control
    plane's aggregates agree by construction.

    Three costs: ``cost_usd`` is what ran; ``baseline_usd`` what it would have cost without
    routing (the actual cost, unless an applied route moved it); ``with_routing_usd`` what it
    costs with the policy applied (a record's counterfactual where its decision was a route not
    applied, its actual cost otherwise). The saving is baseline minus with-routing: what routing
    would save in observer mode, what it saved in router mode. A call to a model with no known
    price adds tokens but no cost, and is counted in ``unpriced_calls`` instead of passing as
    free."""

    calls: int = Field(default=0, ge=0)
    #: Exchanges that did not complete with a 2xx/3xx answer.
    errors: int = Field(default=0, ge=0)
    input_tokens: int = Field(default=0, ge=0)
    output_tokens: int = Field(default=0, ge=0)
    cache_read_tokens: int = Field(default=0, ge=0)
    cache_write_5m_tokens: int = Field(default=0, ge=0)
    cache_write_1h_tokens: int = Field(default=0, ge=0)
    cost_usd: float = Field(default=0.0, ge=0)
    unpriced_calls: int = Field(default=0, ge=0)
    baseline_usd: float = Field(default=0.0, ge=0)
    with_routing_usd: float = Field(default=0.0, ge=0)
    routed_calls: int = Field(default=0, ge=0)
    #: Routes the gateway applied (router mode): calls a cheaper model actually served.
    applied_calls: int = Field(default=0, ge=0)
    routed_tokens: int = Field(default=0, ge=0)
    #: Calls a local model would have answered: API calls that would not have been made.
    routed_local_calls: int = Field(default=0, ge=0)
    routed_local_tokens: int = Field(default=0, ge=0)

    @property
    def cache_write_tokens(self) -> int:
        return self.cache_write_5m_tokens + self.cache_write_1h_tokens

    @property
    def saving_usd(self) -> float:
        return self.baseline_usd - self.with_routing_usd

    @classmethod
    def of(cls, record: GatewayRequestRecord) -> GatewayTotals:
        """One record's contribution."""
        usage = record.usage
        tokens = (
            usage.input_tokens
            + usage.output_tokens
            + usage.cache_read_tokens
            + usage.cache_write_5m_tokens
            + usage.cache_write_1h_tokens
        )
        actual = record.cost_usd
        decision = record.decision
        target = decision.target if decision.action == "route" else None
        # An applied decision has no counterfactual (validated): it costs what ran.
        with_routing = (
            decision.counterfactual_usd
            if target is not None and decision.counterfactual_usd is not None
            else actual or 0.0
        )
        baseline = record.baseline_usd if record.baseline_usd is not None else actual or 0.0
        local = target is not None and target.kind == "local"
        failed = record.outcome != "complete" or (
            record.status is not None and record.status >= 400
        )
        return cls.model_construct(
            calls=1,
            errors=int(failed),
            input_tokens=usage.input_tokens,
            output_tokens=usage.output_tokens,
            cache_read_tokens=usage.cache_read_tokens,
            cache_write_5m_tokens=usage.cache_write_5m_tokens,
            cache_write_1h_tokens=usage.cache_write_1h_tokens,
            cost_usd=actual or 0.0,
            unpriced_calls=int(actual is None and tokens > 0),
            baseline_usd=baseline,
            with_routing_usd=with_routing,
            routed_calls=int(target is not None),
            applied_calls=int(target is not None and decision.applied),
            routed_tokens=tokens if target is not None else 0,
            routed_local_calls=int(local),
            routed_local_tokens=tokens if local else 0,
        )

    def __add__(self, other: GatewayTotals) -> GatewayTotals:
        return GatewayTotals.model_construct(
            **{
                name: getattr(self, name) + getattr(other, name)
                for name in GatewayTotals.model_fields
            }
        )


class ModelRates(_GatewayWire):
    """USD per million tokens, for contracted rates that differ from list price."""

    input: float = Field(ge=0)
    output: float = Field(ge=0)
    cache_write_5m: float = Field(ge=0)
    cache_write_1h: float = Field(ge=0)
    cache_read: float = Field(ge=0)


class FingerprintRule(_GatewayWire):
    fingerprint: Fingerprint
    status: FingerprintStatus
    label: Token | None = None


class PolicyConfig(_GatewayWire):
    """Targets the policy may consider. ``None`` means no candidate of that kind."""

    #: An Anthropic model id considered for a subagent at spawn.
    subagent_target: Token | None = None
    #: An Anthropic model id considered for the main conversation where its cache is cold
    #: (conversation start, after compaction, after the cache expired).
    main_target: Token | None = None
    #: A logical model id on the inference plane, for auxiliary requests allowed to run locally.
    local_target: Token | None = None
    #: A conversation whose cache is warm moves (either way) only where staying would cost at
    #: least this many times rebuilding its cached prefix on the other model.
    warm_switch_margin: float = Field(default=1.5, ge=1.0)

    @model_validator(mode="after")
    def _local_is_logical(self) -> PolicyConfig:
        _check_logical_id(self.local_target)
        return self


class GatewayConfig(_GatewayWire):
    """The versioned configuration the control plane serves to every gateway. Versions are
    immutable; each record carries the version it was decided under."""

    version: int = Field(ge=1)
    mode: GatewayMode
    policy: PolicyConfig = Field(default_factory=PolicyConfig)
    fingerprints: list[FingerprintRule] = Field(default_factory=list)
    #: Keyed by Anthropic model id.
    pricing_overrides: dict[Token, ModelRates] = Field(default_factory=dict)
    updated_at: AwareDatetime
    updated_by: Token | None = None

    @model_validator(mode="after")
    def _unique_fingerprints(self) -> GatewayConfig:
        seen = [rule.fingerprint for rule in self.fingerprints]
        if len(seen) != len(set(seen)):
            raise ValueError("a fingerprint appears more than once")
        return self


class GatewayKey(_GatewayWire):
    """A live developer key, as the gateway verifies it: the SHA-256 of the token, never the
    token itself. Revoked keys are absent from the set."""

    key_id: Token
    token_sha256: Sha256Hex
    developer_id: Token
    credential_profile: CredentialProfile


class GatewayKeySet(_GatewayWire):
    version: int = Field(ge=1)
    generated_at: AwareDatetime
    keys: list[GatewayKey] = Field(default_factory=list)

    @model_validator(mode="after")
    def _unique(self) -> GatewayKeySet:
        digests = [k.token_sha256 for k in self.keys]
        ids = [k.key_id for k in self.keys]
        if len(digests) != len(set(digests)) or len(ids) != len(set(ids)):
            raise ValueError("a key id or token digest appears more than once")
        return self


class GatewayTokenCheck(_GatewayWire):
    """A gateway asks the control plane whether a TheYgent bearer (a session or a personal API
    key) presented to the gateway's admin surface is live. The token travels only between the
    gateway and the control plane, and never appears in a repr."""

    token: Token = Field(repr=False)


class GatewayTokenCheckResult(_GatewayWire):
    """The control plane's answer: whose bearer it is and at what role, or ``active`` false for
    anything else (unknown, expired, revoked, a disabled account, a gateway key)."""

    active: bool
    user_id: Token | None = None
    role: AccountRole | None = None

    @model_validator(mode="after")
    def _identity_when_active(self) -> GatewayTokenCheckResult:
        if self.active != (self.user_id is not None and self.role is not None):
            raise ValueError("an active result names the account and its role; others neither")
        return self


class GatewayHeartbeat(_GatewayWire):
    """A gateway instance's periodic report of itself."""

    gateway_id: Token
    software_version: Token
    mode: GatewayMode
    config_version: int | None = Field(default=None, ge=1)
    key_set_version: int | None = Field(default=None, ge=1)
    started_at: AwareDatetime
    #: Records waiting in the local log to be exported.
    spool_backlog: int = Field(default=0, ge=0)
    upstream_reachable: bool | None = None


class GatewayHeartbeatAck(_GatewayWire):
    """The latest versions available, so a gateway pulls config or keys only when they moved."""

    config_version: int | None = Field(default=None, ge=1)
    key_set_version: int | None = Field(default=None, ge=1)


#: An auxiliary request recognised by its shape, which (unlike its fingerprint, a digest of the
#: client's own prompt) survives a client release: the startup quota check, a side request riding
#: the main conversation's cache, the session title.
KnownKind = Literal["quota_check", "main_prefix_side_request", "session_title"]


class FingerprintEntry(_GatewayWire):
    """One auxiliary-request kind in the catalog: its review status and label (rules in the
    config, ``unreviewed`` without one) and what has been observed of it. A rule can exist before
    its kind is first seen, so the sightings are optional."""

    fingerprint: Fingerprint
    status: FingerprintStatus
    label: Token | None = None
    first_seen: AwareDatetime | None = None
    last_seen: AwareDatetime | None = None
    request_count: int = Field(default=0, ge=0)
    client_versions: list[Token] = Field(default_factory=list)
    shape: RequestShape | None = None
    shares_main_prefix: bool | None = None
    #: What the observed shape says this kind is, and the status that suggests. A suggestion
    #: only: the status is an admin's decision.
    known_kind: KnownKind | None = None
    suggested_status: FingerprintStatus | None = None

    @model_validator(mode="after")
    def _seen_together(self) -> FingerprintEntry:
        if (self.first_seen is None) != (self.last_seen is None):
            raise ValueError("first_seen and last_seen are both set or both unset")
        return self
