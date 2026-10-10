"""The AI gateway ↔ control plane wire types (gateway.py).

The guards:

* every model round-trips through its JSON wire form unchanged;
* every model rejects unknown keys (a field renamed on one side fails loudly on the other);
* the "no message text" rule: free-form string fields are whitespace-free tokens, so prompt
  text cannot be stored in a record even by mistake;
* a local target is a logical model id, never an engine name (the data-plane rule);
* decisions, batches, configs, and key sets are internally consistent.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest
from pydantic import BaseModel, ValidationError
from theygent_ir.gateway import (
    MAX_EVENT_BATCH,
    Decision,
    FingerprintEntry,
    GatewayConfig,
    GatewayEventAck,
    GatewayEventBatch,
    GatewayHeartbeat,
    GatewayHeartbeatAck,
    GatewayKey,
    GatewayKeySet,
    GatewayRequestRecord,
    GatewayTokenCheck,
    GatewayTokenCheckResult,
    GatewayTotals,
    ModelRates,
    PolicyConfig,
    RequestShape,
    SeatAllowance,
    Target,
)

NOW = datetime(2026, 10, 4, 9, 37, 19, tzinfo=UTC)
DIGEST = "a" * 64


def _record(**overrides: Any) -> dict[str, Any]:
    """A realistic observer-mode record: an Explore subagent turn the policy would send to
    Sonnet 5.5, shaped like the requests captured from Claude Code 2.1.285."""
    record: dict[str, Any] = {
        "id": "greq_01K6ZQ4M8V3X9T2B7N5R1C0D4E",
        "gateway_id": "gw-laptop-1",
        "config_version": 3,
        "started_at": NOW.isoformat(),
        "endpoint": "messages",
        "status": 200,
        "outcome": "complete",
        "developer_id": "usr_01K6ZQ",
        "credential_kind": "oauth",
        "credential_profile": "passthrough",
        "client": "cli",
        "client_version": "2.1.285",
        "session_id": "9c92c12e-b929-40ab-94b5-55e4ff08ba54",
        "agent_id": "aeff30f96c2aec6d4",
        "agent_type": "Explore",
        "prompt_id": "c93a79ca-a73b-4047-b873-e60f304ecf61",
        "request_class": "subagent",
        "shape": {
            "max_tokens": 128000,
            "tool_count": 9,
            "deferred_tool_count": 1,
            "message_count": 7,
            "thinking": "adaptive",
            "effort": "medium",
            "has_mid_conversation_system": True,
            "has_context_management": True,
            "body_bytes": 78500,
        },
        "requested_model": "claude-opus-5-5",
        "served_model": "claude-opus-5-5",
        "stop_reason": "tool_use",
        "tool_use_count": 2,
        "usage": {
            "input_tokens": 2,
            "output_tokens": 188,
            "cache_write_1h_tokens": 22987,
        },
        "timing": {"headers_ms": 910, "first_byte_ms": 916, "total_ms": 2352},
        "seat_allowance": {
            "five_hour_utilization": 0.41,
            "seven_day_utilization": 0.06,
            "five_hour_resets_at": "2026-10-04T12:00:00Z",
            "status": "allowed",
            "representative_claim": "five_hour",
            "overage_status": "rejected",
        },
        "cost_usd": 0.1877,
        "pricing_version": "2026-09-25",
        "decision": {
            "action": "route",
            "target": {"kind": "anthropic", "model": "claude-sonnet-5-5"},
            "reasons": ["subagent_at_spawn"],
            "counterfactual_usd": 0.0612,
        },
    }
    record.update(overrides)
    return record


def _config(**overrides: Any) -> dict[str, Any]:
    config: dict[str, Any] = {
        "version": 3,
        "mode": "observer",
        "policy": {
            "subagent_target": "claude-sonnet-5-5",
            "main_target": "claude-sonnet-5-5",
            "local_target": "aux-local",
        },
        "fingerprints": [
            {"fingerprint": "b0a8480b0d1c2e3f", "status": "observe_only", "label": "session_title"},
            {"fingerprint": "210f65a4019a8b7c", "status": "never_route", "label": "quota_check"},
        ],
        "pricing_overrides": {
            "claude-opus-5-5": {
                "input": 3.6,
                "output": 18.0,
                "cache_write_5m": 4.5,
                "cache_write_1h": 7.2,
                "cache_read": 0.18,
            }
        },
        "updated_at": NOW.isoformat(),
        "updated_by": "usr_admin",
    }
    config.update(overrides)
    return config


EXAMPLES: list[tuple[type[BaseModel], dict[str, Any]]] = [
    (GatewayRequestRecord, _record()),
    (GatewayEventBatch, {"gateway_id": "gw-laptop-1", "records": [_record()]}),
    (GatewayEventAck, {"accepted": 1, "duplicates": 0}),
    (GatewayConfig, _config()),
    (
        GatewayKeySet,
        {
            "version": 7,
            "generated_at": NOW.isoformat(),
            "keys": [
                {
                    "key_id": "key_01K6",
                    "token_sha256": DIGEST,
                    "developer_id": "usr_01K6ZQ",
                    "credential_profile": "org_key",
                }
            ],
        },
    ),
    (
        GatewayHeartbeat,
        {
            "gateway_id": "gw-laptop-1",
            "software_version": "0.1.0",
            "mode": "observer",
            "config_version": 3,
            "key_set_version": 7,
            "started_at": NOW.isoformat(),
            "spool_backlog": 12,
            "upstream_reachable": True,
        },
    ),
    (GatewayHeartbeatAck, {"config_version": 4, "key_set_version": 7}),
    (
        FingerprintEntry,
        {
            "fingerprint": "b0a8480b0d1c2e3f",
            "status": "unreviewed",
            "first_seen": NOW.isoformat(),
            "last_seen": NOW.isoformat(),
            "request_count": 42,
            "client_versions": ["2.1.285"],
            "shape": {"max_tokens": 128000, "has_output_schema": True, "body_bytes": 5200},
            "shares_main_prefix": False,
        },
    ),
]


# ── round trip and strictness ────────────────────────────────────────────────


@pytest.mark.parametrize(("model", "payload"), EXAMPLES, ids=lambda x: getattr(x, "__name__", ""))
def test_round_trips_through_json(model: type[BaseModel], payload: dict[str, Any]) -> None:
    parsed = model.model_validate(payload)
    again = model.model_validate_json(parsed.model_dump_json())
    assert again == parsed


@pytest.mark.parametrize(("model", "payload"), EXAMPLES, ids=lambda x: getattr(x, "__name__", ""))
def test_unknown_keys_are_rejected(model: type[BaseModel], payload: dict[str, Any]) -> None:
    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        model.model_validate({**payload, "prompt_preview": "x"})


def test_nested_unknown_keys_are_rejected() -> None:
    record = _record(usage={"input_tokens": 1, "cache_creation_input_tokens": 5})
    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        GatewayRequestRecord.model_validate(record)


def test_models_are_immutable() -> None:
    record = GatewayRequestRecord.model_validate(_record())
    with pytest.raises(ValidationError):
        record.status = 500  # type: ignore[misc]


def test_wire_is_snake_case() -> None:
    dumped = GatewayRequestRecord.model_validate(_record()).model_dump(mode="json")
    assert "request_class" in dumped and "requestClass" not in dumped


# ── the "no message text" rule ───────────────────────────────────────────────


@pytest.mark.parametrize(
    "field",
    ["requested_model", "error_type", "stop_reason", "client", "agent_type", "session_id"],
)
def test_token_fields_refuse_prose(field: str) -> None:
    with pytest.raises(ValidationError, match="String should match pattern"):
        GatewayRequestRecord.model_validate(_record(**{field: "please fix the failing test"}))


def test_token_fields_refuse_long_values() -> None:
    with pytest.raises(ValidationError, match="at most 128 characters"):
        GatewayRequestRecord.model_validate(_record(requested_model="x" * 129))


def test_shape_carries_structure_not_content() -> None:
    with pytest.raises(ValidationError):
        RequestShape.model_validate({"thinking": "think step by step about the bug"})


def _free_text_fields(schema: dict[str, Any]) -> list[str]:
    """Every string-typed property in a JSON Schema (root and ``$defs``) that is neither a
    pattern-constrained token, a closed vocabulary, nor a timestamp."""

    def free(option: dict[str, Any]) -> bool:
        if option.get("type") == "array":
            return free(option.get("items", {}))
        constrained = {"pattern", "enum", "const"} & option.keys()
        return option.get("type") == "string" and not constrained and "format" not in option

    owners = [("record", schema), *schema.get("$defs", {}).items()]
    return [
        f"{owner}.{name}"
        for owner, obj in owners
        for name, prop in (obj.get("properties") or {}).items()
        if any(free(option) for option in prop.get("anyOf", [prop]))
    ]


def test_record_has_no_field_that_can_hold_text() -> None:
    # The structural form of "no message text crosses this wire": every string on a record, at
    # any depth, is a constrained token or a closed vocabulary.
    assert _free_text_fields(GatewayRequestRecord.model_json_schema()) == []


def test_the_free_text_check_would_catch_a_plain_string() -> None:
    class Leaky(BaseModel):
        note: str

    assert _free_text_fields(Leaky.model_json_schema()) == ["record.note"]


# ── local targets are logical ids, never engine names ────────────────────────


@pytest.mark.parametrize("engine", ["llamacpp", "mlx", "vllm", "openai-compatible"])
def test_local_target_rejects_engine_names(engine: str) -> None:
    with pytest.raises(ValidationError, match="engine name"):
        Target.model_validate({"kind": "local", "model": engine})
    with pytest.raises(ValidationError, match="engine name"):
        PolicyConfig.model_validate({"local_target": engine})


def test_local_target_accepts_a_logical_id() -> None:
    assert Target.model_validate({"kind": "local", "model": "aux-local"}).model == "aux-local"


# ── internal consistency ─────────────────────────────────────────────────────


def test_route_needs_a_target_and_stay_has_none() -> None:
    with pytest.raises(ValidationError, match="names its target"):
        Decision.model_validate({"action": "route", "reasons": ["subagent_at_spawn"]})
    with pytest.raises(ValidationError, match="has no target"):
        Decision.model_validate(
            {
                "action": "stay",
                "target": {"kind": "anthropic", "model": "claude-sonnet-5-5"},
                "reasons": ["compaction"],
            }
        )


def test_every_decision_gives_a_reason() -> None:
    with pytest.raises(ValidationError):
        Decision.model_validate({"action": "stay", "reasons": []})


def test_reasons_are_a_closed_vocabulary() -> None:
    with pytest.raises(ValidationError):
        Decision.model_validate({"action": "stay", "reasons": ["seemed_easy"]})


def test_only_a_route_can_be_applied() -> None:
    with pytest.raises(ValidationError, match="only a route"):
        Decision.model_validate({"action": "stay", "reasons": ["compaction"], "applied": True})


def test_observer_decisions_default_to_not_applied() -> None:
    record = GatewayRequestRecord.model_validate(_record())
    assert record.decision.applied is False


def test_unrecognized_class_keeps_the_raw_value() -> None:
    record = GatewayRequestRecord.model_validate(
        _record(request_class="unrecognized", request_class_raw="speculative")
    )
    assert record.request_class_raw == "speculative"
    with pytest.raises(ValidationError, match="request_class_raw"):
        GatewayRequestRecord.model_validate(_record(request_class="unrecognized"))
    with pytest.raises(ValidationError, match="request_class_raw"):
        GatewayRequestRecord.model_validate(_record(request_class_raw="main"))


def test_timestamps_must_carry_a_timezone() -> None:
    with pytest.raises(ValidationError, match="timezone"):
        GatewayRequestRecord.model_validate(_record(started_at="2026-10-04T09:37:19"))


def test_negative_usage_is_rejected() -> None:
    with pytest.raises(ValidationError):
        GatewayRequestRecord.model_validate(_record(usage={"output_tokens": -1}))


def test_seat_utilization_is_not_capped() -> None:
    # The upstream decides what it reports; a value over 1 must not lose the record.
    assert SeatAllowance.model_validate({"five_hour_utilization": 1.07}).five_hour_utilization


def test_fingerprint_format() -> None:
    with pytest.raises(ValidationError):
        GatewayRequestRecord.model_validate(_record(fingerprint="B0A8480B0D1C2E3F"))
    with pytest.raises(ValidationError):
        GatewayRequestRecord.model_validate(_record(fingerprint="b0a8480b"))


def test_batch_holds_one_gateways_records() -> None:
    with pytest.raises(ValidationError, match="other gateways"):
        GatewayEventBatch.model_validate(
            {"gateway_id": "gw-a", "records": [_record(gateway_id="gw-b")]}
        )


def test_batch_size_is_bounded() -> None:
    with pytest.raises(ValidationError):
        GatewayEventBatch.model_validate({"gateway_id": "gw-laptop-1", "records": []})
    too_many = [_record(id=f"greq_{i}") for i in range(MAX_EVENT_BATCH + 1)]
    with pytest.raises(ValidationError):
        GatewayEventBatch.model_validate({"gateway_id": "gw-laptop-1", "records": too_many})


def test_config_fingerprints_are_unique() -> None:
    rule = {"fingerprint": "b0a8480b0d1c2e3f", "status": "observe_only"}
    with pytest.raises(ValidationError, match="more than once"):
        GatewayConfig.model_validate(_config(fingerprints=[rule, rule]))


def test_config_rejects_negative_rates() -> None:
    with pytest.raises(ValidationError):
        ModelRates.model_validate(
            {"input": -1, "output": 1, "cache_write_5m": 1, "cache_write_1h": 1, "cache_read": 1}
        )


def test_key_set_carries_digests_only() -> None:
    key = {
        "key_id": "key_01K6",
        "token_sha256": "tyg_live_secret_value",
        "developer_id": "usr_1",
        "credential_profile": "org_key",
    }
    with pytest.raises(ValidationError):
        GatewayKey.model_validate(key)


def test_key_set_has_no_duplicates() -> None:
    key = {
        "key_id": "key_01K6",
        "token_sha256": DIGEST,
        "developer_id": "usr_1",
        "credential_profile": "passthrough",
    }
    with pytest.raises(ValidationError, match="more than once"):
        GatewayKeySet.model_validate(
            {"version": 1, "generated_at": NOW.isoformat(), "keys": [key, key]}
        )


def test_safety_traffic_has_its_own_reason() -> None:
    # A request carrying ``safeguards`` (auto mode's safety check) is never routed, and says so.
    decision = Decision.model_validate({"action": "stay", "reasons": ["safety_check"]})
    assert decision.reasons == ["safety_check"]


def test_policy_targets_are_optional_and_additive() -> None:
    # A config written before main_target existed still parses, with no main target.
    policy = PolicyConfig.model_validate({"subagent_target": "claude-sonnet-5-5"})
    assert policy.main_target is None


@pytest.mark.parametrize("reason", ["request_refused", "policy_error"])
def test_an_exchange_without_a_policy_decision_still_records_one(reason: str) -> None:
    # A request refused before the policy ran (team-mode authentication), or one the policy
    # failed on, is still recorded: its decision says why there was nothing to decide.
    decision = Decision.model_validate({"action": "stay", "reasons": [reason]})
    assert decision.reasons == [reason]


# ── totals ───────────────────────────────────────────────────────────────────


def _built(**fields: object) -> GatewayRequestRecord:
    base: dict[str, object] = {
        "id": "r1",
        "gateway_id": "gw-1",
        "started_at": "2026-10-10T09:00:00Z",
        "endpoint": "messages",
        "status": 200,
        "outcome": "complete",
        "credential_kind": "oauth",
        "credential_profile": "passthrough",
        "request_class": "main",
        "decision": {"action": "stay", "reasons": ["mid_conversation"]},
    }
    return GatewayRequestRecord.model_validate({**base, **fields})


def test_a_stay_costs_the_same_with_routing() -> None:
    totals = GatewayTotals.of(
        _built(usage={"input_tokens": 10, "cache_write_1h_tokens": 5}, cost_usd=0.5)
    )
    assert (totals.calls, totals.errors, totals.cost_usd, totals.with_routing_usd) == (
        1,
        0,
        0.5,
        0.5,
    )
    assert totals.cache_write_tokens == 5
    assert totals.saving_usd == 0.0
    assert totals.routed_calls == 0


def test_a_route_costs_its_counterfactual_with_routing() -> None:
    route = {
        "action": "route",
        "target": {"kind": "anthropic", "model": "claude-sonnet-5-5"},
        "reasons": ["subagent_at_spawn"],
        "counterfactual_usd": 0.2,
    }
    totals = GatewayTotals.of(_built(usage={"output_tokens": 100}, cost_usd=0.5, decision=route))
    assert (totals.with_routing_usd, totals.routed_calls, totals.routed_tokens) == (0.2, 1, 100)
    assert totals.saving_usd == pytest.approx(0.3)
    assert totals.routed_local_calls == 0


def test_a_local_route_avoids_an_api_call() -> None:
    local = {
        "action": "route",
        "target": {"kind": "local", "model": "aux-local"},
        "reasons": ["local_candidate"],
        "counterfactual_usd": 0.0,
    }
    totals = GatewayTotals.of(_built(usage={"input_tokens": 40}, cost_usd=0.01, decision=local))
    assert (totals.routed_local_calls, totals.routed_local_tokens, totals.with_routing_usd) == (
        1,
        40,
        0.0,
    )


def test_an_unpriced_call_is_counted_not_free() -> None:
    totals = GatewayTotals.of(_built(usage={"output_tokens": 3}))
    assert (totals.unpriced_calls, totals.cost_usd) == (1, 0.0)
    assert GatewayTotals.of(_built()).unpriced_calls == 0  # nothing used, nothing to price


@pytest.mark.parametrize(
    ("status", "outcome", "error"),
    [
        (200, "complete", 0),
        (429, "complete", 1),
        (200, "upstream_broke", 1),
        (None, "transport_error", 1),
    ],
)
def test_errors(status: int | None, outcome: str, error: int) -> None:
    assert GatewayTotals.of(_built(status=status, outcome=outcome)).errors == error


def test_totals_add_field_by_field() -> None:
    one = GatewayTotals.of(_built(usage={"input_tokens": 1}, cost_usd=0.25))
    total = one + one + GatewayTotals()
    assert (total.calls, total.input_tokens, total.cost_usd) == (2, 2, 0.5)
    assert GatewayTotals.model_validate(total.model_dump()) == total  # a valid wire value


# ── token check ──────────────────────────────────────────────────────────────


def test_a_token_check_never_shows_its_token() -> None:
    check = GatewayTokenCheck(token="tys_secret-session-bearer")
    assert "tys_secret" not in repr(check)
    assert GatewayTokenCheck.model_validate_json(check.model_dump_json()) == check


@pytest.mark.parametrize(
    ("payload", "valid"),
    [
        ({"active": True, "user_id": "usr_1", "role": "admin"}, True),
        ({"active": False}, True),
        ({"active": True, "user_id": "usr_1"}, False),
        ({"active": False, "user_id": "usr_1", "role": "viewer"}, False),
        ({"active": True, "user_id": "usr_1", "role": "owner"}, False),
    ],
)
def test_a_token_check_result_names_an_account_exactly_when_active(
    payload: dict[str, object], valid: bool
) -> None:
    if valid:
        GatewayTokenCheckResult.model_validate(payload)
    else:
        with pytest.raises(ValidationError):
            GatewayTokenCheckResult.model_validate(payload)
